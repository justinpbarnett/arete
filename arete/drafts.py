from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import statistics
from datetime import timedelta
from difflib import unified_diff
from pathlib import Path

from . import configitems, configkinds, configrepo, contract, paths, pins, principals, prompts, record, util
from .store import blobs as blob_store
from .store import config as config_store
from .store import db as foundation
from .store import events as event_store
from .store import inbox as inbox_store
from .store import runs as run_store

STATES = frozenset((
    "draft", "valid", "invalid", "active", "declined", "discarded", "superseded",
))

MOVES: dict[str, frozenset[str]] = {
    "draft": frozenset(("valid", "invalid", "discarded")),
    "invalid": frozenset(("draft", "discarded")),
    "valid": frozenset(("active", "declined", "discarded", "invalid", "superseded")),
    "active": frozenset(("superseded",)),
    "declined": frozenset(),
    "discarded": frozenset(),
    "superseded": frozenset(),
}

LEGACY: dict[str, str] = {
    "pending": "valid",
    "hand": "valid",
    "memory": "valid",
    "approve": "valid",
    "filing": "valid",
    "applied": "valid",
    "merged": "active",
    "rejected": "declined",
    "skipped": "declined",
    "filed": "discarded",
    "superseded": "discarded",
}

GATES = frozenset(("A", "B", "C"))
ID_RE = re.compile(r"^(?:[Cc][Dd]-)?([1-9][0-9]{0,17})$")
WATCH_KINDS = frozenset(("prompt", "adw", "routes", "step_settings"))
WATCH_WINDOW_DAYS = 30
WATCH_RUN_SAMPLE = 10
PASSED_STATUSES = frozenset(("done", "pr", "merged"))
SETTLED_EXCLUDED = frozenset(("queued", "running", "needs-info"))
CONFIG_ASK_TRIES = 3
CONFIG_ASK_LOG = "config-ask.log"
PROMOTABLE_KINDS = frozenset(("prompt", "schema", "adw", "routes", "step_settings"))


class PresenceRequired(Exception):
    def __init__(self, draft: dict, report: dict):
        super().__init__(f"Gate C change on {draft['kind']}/{draft['name']} requires owner presence.")
        self.draft = draft
        self.report = report


def legacy_state(status: str | None) -> str:
    if status is None:
        return "draft"
    return LEGACY.get(status, "draft")


def format_id(numeric_id: int) -> str:
    return f"CD-{numeric_id}"


def parse_id(value: str | int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 0 < value < 2**63 else None
    if not isinstance(value, str):
        return None
    m = ID_RE.fullmatch(value.strip())
    return int(m[1]) if m else None


def available(c: sqlite3.Connection | None = None) -> bool:
    if c is not None:
        return c.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'config_drafts'"
        ).fetchone() is not None
    try:
        with foundation.reader() as conn:
            return available(conn)
    except (foundation.NoDatabase, sqlite3.Error):
        return False


def _sync_inbox(c: sqlite3.Connection) -> None:
    from . import inbox

    inbox.sync(c, "proposals")


def _parse_json(raw: str | None, fallback: object) -> object:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except ValueError:
        return fallback


def decode(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    report = _parse_json(d.get("check_report"), None)
    evidence = _parse_json(d.get("evidence"), {})
    return {
        "id": format_id(int(d["id"])),
        "numeric_id": int(d["id"]),
        "kind": d["kind"],
        "name": d["name"],
        "origin": d["origin"],
        "path": d["path"],
        "base_version": d["base_version"],
        "body": d["body"],
        "diff": d["diff"],
        "author": d["author"],
        "model": d["model"],
        "instruction": d["instruction"],
        "why": d["why"],
        "gate": d["gate"],
        "check_report": report if isinstance(report, dict) else None,
        "validator": d["validator"],
        "state": d["state"],
        "decline_reason": d["decline_reason"],
        "evidence": evidence,
        "activated_commit": d["activated_commit"],
        "legacy_proposal": d["legacy_proposal"],
        "pr_url": d["pr_url"] or None,
        "version": int(d["version"]),
        "created": d["created"],
        "updated": d["updated"],
    }


def public(draft: dict | None) -> dict | None:
    if draft is None:
        return None
    return {k: v for k, v in draft.items() if k != "numeric_id"}


def _validate_state(state: str) -> None:
    if state not in STATES:
        raise ValueError(f"invalid draft state {state!r}; expected one of {', '.join(sorted(STATES))}")


def _validate_move(before: str, after: str) -> None:
    if before not in STATES:
        raise ValueError(f"invalid draft state transition {before!r} -> {after!r}: unknown state {before!r}")
    if after not in STATES:
        raise ValueError(f"invalid draft state transition {before!r} -> {after!r}: unknown state {after!r}")
    if after not in MOVES[before]:
        raise ValueError(f"invalid draft state transition {before!r} -> {after!r}")


def _encode_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def add(
    c: sqlite3.Connection | None = None,
    *,
    kind: str,
    name: str,
    origin: str = "",
    path: str = "",
    base_version: str = "",
    body: str = "",
    diff: str = "",
    author: str = "",
    model: str = "",
    instruction: str = "",
    why: str = "",
    gate: str = "B",
    check_report: dict | str | None = None,
    validator: str = "",
    state: str = "draft",
    decline_reason: str = "",
    evidence: object = None,
    activated_commit: str = "",
    legacy_proposal: int | None = None,
    pr_url: str | None = None,
    sync_inbox: bool = True,
) -> int:
    _validate_state(state)
    if gate not in GATES:
        raise ValueError(f"invalid draft gate {gate!r}; expected A, B, or C")
    now = util.iso()
    report_str = (
        check_report if isinstance(check_report, str)
        else _encode_json(check_report) if check_report is not None else ""
    )
    evidence_str = (
        evidence if isinstance(evidence, str)
        else _encode_json(evidence if evidence is not None else {})
    )
    clean_pr = (pr_url or "").strip() or None

    def _insert(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            "INSERT INTO config_drafts ("
            "kind, name, origin, path, base_version, body, diff, author, model, instruction, why, "
            "gate, check_report, validator, state, decline_reason, evidence, activated_commit, "
            "legacy_proposal, pr_url, version, created, updated"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (
                kind,
                name,
                origin,
                path,
                str(base_version or ""),
                body,
                diff,
                author or principals.current(),
                model,
                instruction,
                why,
                gate,
                report_str,
                validator,
                state,
                decline_reason,
                evidence_str,
                activated_commit,
                legacy_proposal,
                clean_pr,
                now,
                now,
            ),
        )
        draft_id = int(cur.lastrowid)
        if sync_inbox:
            _sync_inbox(conn)
        return draft_id

    if c is not None:
        return _insert(c)
    with foundation.tx() as conn:
        return _insert(conn)


def move(
    first,
    second,
    third,
    fourth=None,
    *,
    reason: str | None = None,
    activated_commit: str | None = None,
    sync_inbox: bool = True,
) -> bool:
    """Move draft state atomically and emit one draft.changed event.

    Supports both:
      move(conn, draft_id, before, after, reason=None)
      move(draft_id, before, after, reason=None)
    """
    if isinstance(first, sqlite3.Connection):
        conn: sqlite3.Connection | None = first
        draft_id = second
        before = third
        after = fourth
    else:
        conn = None
        draft_id = first
        before = second
        after = third
        if fourth is not None and reason is None:
            reason = fourth

    _validate_move(str(before), str(after))
    numeric = parse_id(draft_id)
    if numeric is None:
        return False

    def _run(c: sqlite3.Connection) -> bool:
        now = util.iso()
        sets = ["state = ?", "version = version + 1", "updated = ?"]
        args: list[object] = [after, now]
        if after == "declined" and reason is not None:
            sets.append("decline_reason = ?")
            args.append(str(reason))
        if activated_commit is not None:
            sets.append("activated_commit = ?")
            args.append(activated_commit)
        args.extend((numeric, before))
        cur = c.execute(
            f"UPDATE config_drafts SET {', '.join(sets)} WHERE id = ? AND state = ?",  # noqa: S608
            args,
        )
        if cur.rowcount != 1:
            return False
        row = c.execute("SELECT version FROM config_drafts WHERE id = ?", (numeric,)).fetchone()
        new_ver = int(row["version"]) if row else 1
        event_store.emit(
            "draft.changed",
            f"draft:{numeric}",
            cls="durable",
            ver=new_ver,
            data={"from": before, "to": after, "reason": reason},
            connection=c,
        )
        if sync_inbox:
            _sync_inbox(c)
        return True

    if conn is not None:
        return _run(conn)
    with foundation.tx() as c:
        return _run(c)


def get(draft_id: str | int, connect=foundation.reader) -> dict | None:
    numeric = parse_id(draft_id)
    if numeric is None:
        return None
    with connect() as c:
        if not available(c):
            return None
        row = c.execute("SELECT * FROM config_drafts WHERE id = ?", (numeric,)).fetchone()
        return decode(row)


def list_drafts(
    *,
    state: str | tuple[str, ...] | None = None,
    kind: str | None = None,
    name: str | None = None,
    author: str | None = None,
    limit: int = 200,
    connect=foundation.reader,
) -> list[dict]:
    with connect() as c:
        if not available(c):
            return []
        clauses: list[str] = []
        args: list[object] = []
        if state is not None and state != "all":
            states = (state,) if isinstance(state, str) else tuple(state)
            if len(states) == 1 and "," in states[0]:
                states = tuple(s.strip() for s in states[0].split(",") if s.strip())
            if states:
                clauses.append(f"state IN ({','.join('?' for _ in states)})")
                args.extend(states)
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        if name:
            clauses.append("name = ?")
            args.append(name)
        if author:
            clauses.append("author = ?")
            args.append(author)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        args.append(limit)
        rows = c.execute(
            f"SELECT * FROM config_drafts{where} ORDER BY id DESC LIMIT ?",  # noqa: S608
            args,
        ).fetchall()
        return [decode(r) for r in rows]


def set_pr_url(draft_id: str | int, pr_url: str, *, c: sqlite3.Connection | None = None) -> bool:
    numeric = parse_id(draft_id)
    if numeric is None:
        return False

    def _run(conn: sqlite3.Connection) -> bool:
        now = util.iso()
        ok = conn.execute(
            "UPDATE config_drafts SET pr_url = ?, version = version + 1, updated = ? WHERE id = ?",
            (pr_url or None, now, numeric),
        ).rowcount == 1
        if ok:
            _sync_inbox(conn)
        return ok

    if c is not None:
        return _run(c)
    with foundation.tx() as conn:
        return _run(conn)


def set_evidence(draft_id: str | int, evidence: object, *, c: sqlite3.Connection | None = None) -> bool:
    numeric = parse_id(draft_id)
    if numeric is None:
        return False
    encoded = evidence if isinstance(evidence, str) else _encode_json(evidence)

    def _run(conn: sqlite3.Connection) -> bool:
        return conn.execute(
            "UPDATE config_drafts SET evidence = ?, version = version + 1, updated = ? WHERE id = ?",
            (encoded, util.iso(), numeric),
        ).rowcount == 1

    if c is not None:
        return _run(c)
    with foundation.tx() as conn:
        return _run(conn)


def _update_check_row(c: sqlite3.Connection, numeric_id: int, report: dict) -> None:
    c.execute(
        "UPDATE config_drafts SET gate = ?, check_report = ?, validator = ?, updated = ? WHERE id = ?",
        (
            report["gate"],
            _encode_json(report),
            str(report.get("validator") or ""),
            util.iso(),
            numeric_id,
        ),
    )


def _match_evidence(raw: str, candidate_ids: set[str], out: set[str]) -> None:
    if not raw:
        return
    parsed = _parse_json(raw, None)
    if isinstance(parsed, dict):
        for key in ("run", "run_id"):
            direct = parsed.get(key)
            if isinstance(direct, str) and direct in candidate_ids:
                out.add(direct)
        cites = parsed.get("cites")
        if cites:
            cites_text = cites if isinstance(cites, str) else json.dumps(cites, default=str)
            for rid in candidate_ids:
                if rid in cites_text:
                    out.add(rid)
        for key in ("trial_runs", "runs", "trials"):
            vals = parsed.get(key)
            if isinstance(vals, list):
                for item in vals:
                    if isinstance(item, str) and item in candidate_ids:
                        out.add(item)
    elif isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str) and item in candidate_ids:
                out.add(item)
            elif item is not None:
                text = str(item)
                for rid in candidate_ids:
                    if rid in text:
                        out.add(rid)
    else:
        for rid in candidate_ids:
            if rid in raw:
                out.add(rid)


def cited_runs(
    c: sqlite3.Connection | None,
    ids,
    *,
    states: tuple[str, ...] = ("draft", "valid"),
) -> set[str]:
    """Allow-list: return runs cited by config_drafts rows in the given states.

    A draft cites a run via author ('run:<id>/<node>'), evidence, or the run_id/evidence
    of the proposals row its legacy_proposal names.
    """
    candidate_ids = {str(r) for r in ids if r}
    allowed_states = tuple(s for s in states if s in STATES)
    if not candidate_ids or not allowed_states:
        return set()

    def _query(conn: sqlite3.Connection) -> set[str]:
        if not available(conn):
            return set()
        marks = ",".join("?" for _ in allowed_states)
        rows = conn.execute(
            f"SELECT author, evidence, legacy_proposal FROM config_drafts WHERE state IN ({marks})",  # noqa: S608
            allowed_states,
        ).fetchall()
        cited: set[str] = set()
        legacy_ids: set[int] = set()
        for row in rows:
            author = str(row["author"] or "")
            if author.startswith("run:"):
                run_part = author.removeprefix("run:").split("/", 1)[0]
                if run_part in candidate_ids:
                    cited.add(run_part)
            _match_evidence(str(row["evidence"] or ""), candidate_ids, cited)
            if row["legacy_proposal"] is not None:
                legacy_ids.add(int(row["legacy_proposal"]))
        if legacy_ids:
            has_proposals = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = 'proposals'"
            ).fetchone() is not None
            if has_proposals:
                id_list = sorted(legacy_ids)
                pmarks = ",".join("?" for _ in id_list)
                prop_rows = conn.execute(
                    f"SELECT run_id, evidence FROM proposals WHERE id IN ({pmarks})",  # noqa: S608
                    id_list,
                ).fetchall()
                for prow in prop_rows:
                    prun = str(prow["run_id"] or "")
                    if prun in candidate_ids:
                        cited.add(prun)
                    _match_evidence(str(prow["evidence"] or ""), candidate_ids, cited)
        return cited

    if c is not None:
        return _query(c)
    with foundation.reader() as conn:
        return _query(conn)


# ---------------------------------------------------------------------------
# Miner draft helpers (replacing legacy approvals_store proposal functions)
# ---------------------------------------------------------------------------

def miner_view(draft: dict) -> dict:
    """Project a miner config_drafts row into the familiar proposal dict shape."""
    ev = draft.get("evidence") if isinstance(draft.get("evidence"), dict) else {}
    origin = draft.get("origin") or ""
    repo = origin.removeprefix("repo:") if origin.startswith("repo:") else origin
    return {
        "id": draft["numeric_id"],
        "draft_id": draft["id"],
        "run_id": str(ev.get("run") or ""),
        "layer": draft["name"],
        "repo": repo,
        "file": draft["path"],
        "change": draft["body"],
        "why": draft["why"],
        "evidence": json.dumps(ev.get("cites") or []),
        "confidence": float(ev.get("confidence") or 0.0),
        "status": draft["state"],
        "state": draft["state"],
        "pr_url": draft["pr_url"] or "",
        "created": draft["created"],
        "decline_reason": draft["decline_reason"],
    }


def miner_drafts(
    *,
    states: list[str] | tuple[str, ...] | None = None,
    has_pr: bool | None = None,
    limit: int = 200,
    connect=foundation.reader,
) -> list[dict]:
    with connect() as c:
        if not available(c):
            return []
        clauses = ["kind = 'miner'"]
        args: list[object] = []
        if states:
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            args.extend(states)
        if has_pr is True:
            clauses.append("COALESCE(pr_url, '') != ''")
        elif has_pr is False:
            clauses.append("COALESCE(pr_url, '') = ''")
        args.append(limit)
        rows = c.execute(
            f"SELECT * FROM config_drafts WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",  # noqa: S608
            args,
        ).fetchall()
        return [miner_view(decode(r)) for r in rows]


def add_miner_draft(
    run_id: str,
    p: dict,
    *,
    state: str = "valid",
    pr_url: str = "",
    decline_reason: str = "",
    c: sqlite3.Connection | None = None,
) -> int:
    evidence = {
        "run": run_id or "",
        "cites": list(p.get("evidence") or ()),
        "confidence": float(p.get("confidence", 0.0) or 0.0),
    }
    return add(
        c,
        kind="miner",
        name=str(p.get("layer") or ""),
        origin=f"repo:{p.get('repo') or ''}",
        path=str(p.get("file") or ""),
        body=str(p.get("change") or ""),
        why=str(p.get("why") or ""),
        author="miner",
        gate="B",
        state=state,
        decline_reason=decline_reason,
        evidence=evidence,
        pr_url=pr_url or None,
    )


def set_miner_status_by_pr(pr_url: str, target_state: str, *, reason: str | None = None) -> bool:
    if not pr_url:
        return False
    changed = False
    with foundation.tx() as c:
        if not available(c):
            return False
        rows = c.execute(
            "SELECT id, state FROM config_drafts WHERE kind = 'miner' AND pr_url = ? AND state = 'valid'",
            (pr_url,),
        ).fetchall()
        for row in rows:
            if move(c, int(row["id"]), "valid", target_state, reason=reason, sync_inbox=False):
                changed = True
        if changed:
            _sync_inbox(c)
    return changed


def check_proposal_revisions(layer: str, repo: str, family: str, revisions: str) -> list[dict]:
    with foundation.reader() as c:
        if not available(c):
            return []
        rows = c.execute(
            "SELECT id, path, evidence, state FROM config_drafts "
            "WHERE kind = 'miner' AND name = ? AND origin = ? AND (path = ? OR path GLOB ?)",
            (layer, f"repo:{repo}", family, revisions),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            ev = _parse_json(r["evidence"], {})
            cites = ev.get("cites", []) if isinstance(ev, dict) else []
            out.append({
                "id": int(r["id"]),
                "file": r["path"],
                "evidence": json.dumps(cites),
                "status": r["state"],
                "state": r["state"],
            })
        return out


def record_check_proposal(
    run_id: str,
    p: dict,
    family: str,
    revisions: str,
    legacy_ids: list[int],
) -> None:
    origin = f"repo:{p['repo']}"
    layer = p["layer"]
    with foundation.tx() as c:
        if c.execute(
            "SELECT 1 FROM config_drafts WHERE kind = 'miner' AND name = ? AND origin = ? AND path = ? LIMIT 1",
            (layer, origin, p["file"]),
        ).fetchone():
            return
        prior = c.execute(
            "SELECT id FROM config_drafts WHERE kind = 'miner' AND name = ? AND origin = ? AND state = 'valid' "
            "AND (path = ? OR path GLOB ?)",
            (layer, origin, family, revisions),
        ).fetchall()
        to_supersede = {int(r["id"]) for r in prior} | {int(pid) for pid in legacy_ids}
        for pid in sorted(to_supersede):
            move(c, pid, "valid", "discarded", reason="superseded by revised check proposal", sync_inbox=False)
        add_miner_draft(run_id, p, state="valid", c=c)


# ---------------------------------------------------------------------------
# Config item path resolution, drafting, checking, activation, rollback, try
# ---------------------------------------------------------------------------

def default_path(kind: str, name: str) -> str:
    if kind not in configkinds.REGISTRY:
        raise contract.Problem("not_found", f"Unknown config kind {kind}")
    if kind == "step_settings":
        if name == "defaults":
            return "config.toml"
        if name == configkinds.DEFAULTS_LAYER_ADDRESS:
            return "layers/defaults/config.toml"
        return f"layers/{name}/config.toml"
    if kind == "attention":
        return "config.toml"
    if kind == "retention":
        return "retention.toml"
    if kind == "prompt":
        if name.endswith(".append") and "." in name[:-7]:
            repo, _, prompt_name = name[:-7].partition(".")
            return f"layers/{repo}/prompts/{prompt_name}.append.md"
        return f"prompts/{name}.md"
    if kind == "loop":
        if "." in name:
            # Handle escaped repo name if present, else split on first single dot.
            parts = name.split(".")
            if len(parts) == 2:
                return f"layers/{parts[0]}/loops/{parts[1]}/loop.toml"
        return f"loops/{name}/loop.toml"
    entry = configkinds.REGISTRY[kind]
    first = entry.home[0]
    return first.pattern.replace("{name}", name)


def _unified_diff(path: str, old_bytes: bytes | None, new_text: str) -> str:
    old_text = old_bytes.decode("utf-8", errors="replace") if old_bytes is not None else ""
    return "".join(unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ))


def _live_ver_for(kind: str, name: str, path: str, conn=None) -> int:
    lookup_kind = "step_settings" if kind == "attention" else kind
    row = config_store.live_item(lookup_kind, name, conn) or config_store.live_by_path(path, conn)
    if row is not None and row.get("ver") is not None:
        return int(row["ver"])
    return 0


def _prepare_prompt_draft(
    name: str,
    body: str,
    *,
    path: str = "",
    level: str | None = None,
) -> tuple[str, str, str]:
    clean_level = (level or "").strip()
    is_layer = False
    if clean_level:
        if clean_level == "config":
            is_layer = False
        elif clean_level.startswith("layer:"):
            repo_name = clean_level.removeprefix("layer:").strip()
            if not repo_name or not re.fullmatch(configkinds.NAME_PATTERN, repo_name):
                raise contract.Problem("validation", f"Invalid layer level {clean_level!r}.", path="level")
            is_layer = True
            if not path:
                if name.endswith(".append") and "." in name[:-7]:
                    _, _, stem = name[:-7].partition(".")
                    path = f"layers/{repo_name}/prompts/{stem}.append.md"
                else:
                    path = f"layers/{repo_name}/prompts/{name}.append.md"
                    name = f"{repo_name}.{name}.append"
        else:
            raise contract.Problem("validation", f"Unsupported config draft level {clean_level!r}.", path="level")

    rel = path or default_path("prompt", name)
    if is_layer or rel.startswith("layers/") or name.endswith(".append"):
        try:
            fm, _ = prompts.parse(body)
        except ValueError:
            fm = {}
        if "overrides" in fm:
            raise contract.Problem(
                "validation",
                "Layer prompt fragment may not declare overrides.",
                path="bytes",
            )
        return name, rel, body

    prepared = prompts.fill_override_front_matter(name, body)
    return name, rel, prepared


def create_and_check(
    kind: str,
    name: str,
    body: str,
    *,
    author: str | None = None,
    path: str = "",
    base_version: str | int | None = None,
    why: str = "",
    instruction: str = "",
    model: str = "",
    evidence: object = None,
    diff: str | None = None,
    level: str | None = None,
    run_check: bool = True,
) -> tuple[dict, dict | None]:
    from . import configcheck

    if kind not in configkinds.REGISTRY:
        raise contract.Problem("not_found", f"Unknown config kind {kind}")
    if kind == "prompt":
        name, rel, body = _prepare_prompt_draft(name, body, path=path, level=level)
    else:
        rel = path or default_path(kind, name)
    actor = author or principals.current()
    root = paths.CONFIG_DIR.resolve()
    old_bytes = configrepo.live_bytes(rel, root) if configrepo.is_repo(root) else (
        (root / rel).read_bytes() if (root / rel).is_file() else None
    )
    diff_text = diff if diff is not None else _unified_diff(rel, old_bytes, body)
    live_ver = _live_ver_for(kind, name, rel)
    base_str = str(live_ver if base_version is None else base_version)
    ev = evidence if evidence is not None else []

    if run_check:
        report = configcheck.check(kind, name, rel, body, author=actor, config_dir=root, old_bytes=old_bytes)
        gate = report["gate"]
    else:
        report = None
        entry = configkinds.REGISTRY[kind]
        gate = "A" if entry.gate == "check" else "B"
        if actor.startswith("run:") or actor in ("miner", "optimize"):
            gate = "B"

    with foundation.tx() as c:
        draft_id = add(
            c,
            kind=kind,
            name=name,
            origin=configrepo.origin_of(rel),
            path=rel,
            base_version=base_str,
            body=body,
            diff=diff_text,
            author=actor,
            model=model,
            instruction=instruction,
            why=why,
            gate=gate,
            check_report=report,
            validator=str(report["validator"]) if report else "",
            state="draft",
            evidence=ev,
            sync_inbox=False,
        )
        if report is not None:
            if report["ok"]:
                if not (gate == "C" and actor.startswith("run:")):
                    move(c, draft_id, "draft", "valid", sync_inbox=False)
            else:
                move(c, draft_id, "draft", "invalid", sync_inbox=False)
        _sync_inbox(c)
        saved = decode(c.execute("SELECT * FROM config_drafts WHERE id = ?", (draft_id,)).fetchone())
    return saved, report


def recheck(draft_id: str | int) -> tuple[dict, dict]:
    from . import configcheck

    existing = get(draft_id)
    if existing is None:
        raise contract.Problem("not_found", f"No config draft {draft_id}")
    if existing["kind"] == "miner":
        raise contract.Problem("invalid_state", f"Miner draft {existing['id']} cannot run configcheck.")
    root = paths.CONFIG_DIR.resolve()
    old_bytes = configrepo.live_bytes(existing["path"], root) if configrepo.is_repo(root) else None
    report = configcheck.check(
        existing["kind"],
        existing["name"],
        existing["path"],
        existing["body"],
        author=existing["author"],
        config_dir=root,
        old_bytes=old_bytes,
    )
    numeric = existing["numeric_id"]
    with foundation.tx() as c:
        cur_row = decode(c.execute("SELECT * FROM config_drafts WHERE id = ?", (numeric,)).fetchone())
        if cur_row is None:
            raise contract.Problem("not_found", f"No config draft {draft_id}")
        _update_check_row(c, numeric, report)
        cur_state = cur_row["state"]
        is_run_c = report["gate"] == "C" and cur_row["author"].startswith("run:")
        if report["ok"]:
            if is_run_c:
                if cur_state == "invalid":
                    move(c, numeric, "invalid", "draft", sync_inbox=False)
            elif cur_state == "draft":
                move(c, numeric, "draft", "valid", sync_inbox=False)
            elif cur_state == "invalid":
                move(c, numeric, "invalid", "draft", sync_inbox=False)
                move(c, numeric, "draft", "valid", sync_inbox=False)
        else:
            if cur_state in ("draft", "valid"):
                move(c, numeric, cur_state, "invalid", sync_inbox=False)
        _sync_inbox(c)
        updated = decode(c.execute("SELECT * FROM config_drafts WHERE id = ?", (numeric,)).fetchone())
    return updated, report


def discard(draft_id: str | int, *, reason: str | None = None) -> dict:
    existing = get(draft_id)
    if existing is None:
        raise contract.Problem("not_found", f"No config draft {draft_id}")
    cur_state = existing["state"]
    if "discarded" not in MOVES.get(cur_state, ()):
        raise contract.Problem("invalid_state", f"Draft {existing['id']} in state {cur_state!r} cannot be discarded.")
    ok = move(existing["numeric_id"], cur_state, "discarded", reason=reason)
    if not ok:
        raise contract.Problem("conflict", f"Draft {existing['id']} state changed concurrently.")
    return get(existing["numeric_id"])


def finalize_activated_draft(conn: sqlite3.Connection, draft_id_str: str, commit_sha: str) -> None:
    """Idempotently finalize draft publication in SQLite (used on normal activation & crash recovery)."""
    if not available(conn):
        return
    numeric = parse_id(draft_id_str)
    if numeric is None:
        return
    row = decode(conn.execute("SELECT * FROM config_drafts WHERE id = ?", (numeric,)).fetchone())
    if row is None:
        return
    if row["state"] == "active" and row["activated_commit"] == commit_sha:
        return
    # Supersede any prior active drafts for the same (kind, name).
    prior_active = conn.execute(
        "SELECT id FROM config_drafts WHERE kind = ? AND name = ? AND state = 'active' AND id != ?",
        (row["kind"], row["name"], numeric),
    ).fetchall()
    for prev in prior_active:
        move(conn, int(prev["id"]), "active", "superseded", reason=f"superseded by {format_id(numeric)}", sync_inbox=False)
    cur_state = row["state"]
    if cur_state == "draft":
        move(conn, numeric, "draft", "valid", sync_inbox=False)
        cur_state = "valid"
    if cur_state == "valid":
        move(conn, numeric, "valid", "active", activated_commit=commit_sha, sync_inbox=False)
    elif cur_state == "active":
        conn.execute(
            "UPDATE config_drafts SET activated_commit = ?, updated = ? WHERE id = ?",
            (commit_sha, util.iso(), numeric),
        )
    _sync_inbox(conn)


def _matches_base_version(base_version: str | int, live_ver: int, live_row: dict | None) -> bool:
    if isinstance(base_version, int) and not isinstance(base_version, bool):
        return base_version == live_ver
    text = str(base_version).strip()
    if text.isascii() and text.isdecimal():
        return int(text) == live_ver
    return live_row is not None and text in (live_row.get("version"), live_row.get("commit_sha"))


def _arm_activation_spec(draft: dict, root: Path) -> tuple[str, str, list[str]] | None:
    ev = draft.get("evidence")
    if not isinstance(ev, dict) or not ev.get("arm_commit"):
        return None
    arm_commit = configrepo.resolve_ref(str(ev["arm_commit"]), root)
    if not arm_commit:
        raise contract.Problem("validation", f"Arm commit {ev['arm_commit']!r} is missing from the config repo.")
    arm_base = str(ev.get("arm_base") or configrepo.parent_sha(arm_commit, root))
    raw_files = ev.get("arm_files")
    arm_files = (
        [str(p) for p in raw_files if isinstance(p, str) and p]
        if isinstance(raw_files, list) and raw_files
        else configrepo.commit_changed_paths(arm_commit, root, base=arm_base)
    )
    if not arm_files:
        raise contract.Problem("validation", f"Arm commit {arm_commit[:12]} changes no files.")
    return arm_commit, arm_base, arm_files


def _validate_base_and_check_for_activation(
    draft: dict,
    actor: str,
    base_version: str | int,
    *,
    root: Path,
) -> tuple[dict, int]:
    from . import configcheck

    if actor.startswith("run:"):
        raise contract.Problem("gate_refused", f"Actor {actor!r} cannot activate config drafts.")
    if draft["kind"] == "miner":
        raise contract.Problem("invalid_state", f"Miner draft {draft['id']} cannot be activated via config.activate.")
    if draft["gate"] == "C" and draft["author"].startswith("run:"):
        raise contract.Problem(
            "gate_refused",
            f"Run-authored gate C draft {draft['id']} cannot be activated directly; accept it to create an owner draft.",
        )
    if draft["state"] not in ("draft", "valid"):
        raise contract.Problem("invalid_state", f"Draft {draft['id']} in state {draft['state']!r} cannot be activated.")

    rel = draft["path"] or default_path(draft["kind"], draft["name"])
    lookup_kind = "step_settings" if draft["kind"] == "attention" else draft["kind"]
    live_row = config_store.live_item(lookup_kind, draft["name"]) or config_store.live_by_path(rel)
    live_ver = int(live_row["ver"]) if live_row and live_row.get("ver") is not None else 0
    if not _matches_base_version(base_version, live_ver, live_row):
        raise contract.Problem(
            "version_conflict",
            f"Config item {draft['kind']}/{draft['name']} is at version {live_ver}, not {base_version}.",
            current={"kind": draft["kind"], "name": draft["name"], "version": live_ver},
        )

    arm_spec = _arm_activation_spec(draft, root)
    if arm_spec is not None:
        arm_commit, arm_base, arm_files = arm_spec
        for arm_rel in arm_files:
            live_blob = configrepo._live_blob(root, arm_rel)
            base_blob = configrepo.blob_at(arm_base, arm_rel, root)
            if live_blob != base_blob:
                raise contract.Problem(
                    "version_conflict",
                    f"Config item {arm_rel} moved since arm {arm_commit[:12]} was cut.",
                    current={"path": arm_rel, "live_blob": live_blob, "arm_base": arm_base},
                )
        if len(arm_files) > 1 or draft["kind"] == "experiment":
            worst_gate = "B"
            all_stages: list[dict] = []
            for arm_rel in arm_files:
                file_bytes = configrepo.commit_bytes(arm_commit, arm_rel, root)
                if file_bytes is None:
                    continue
                f_kind, f_name = configrepo.classify_path(arm_rel)
                if f_kind not in configkinds.REGISTRY:
                    continue
                old_f = configrepo.live_bytes(arm_rel, root) if configrepo.is_repo(root) else None
                sub = configcheck.check(
                    f_kind,
                    f_name,
                    arm_rel,
                    file_bytes.decode("utf-8"),
                    author=draft["author"],
                    config_dir=root,
                    old_bytes=old_f,
                )
                all_stages.extend(sub.get("stages", ()))
                if not sub["ok"]:
                    with foundation.tx() as c:
                        _update_check_row(c, draft["numeric_id"], sub)
                        if draft["state"] in ("draft", "valid"):
                            move(c, draft["numeric_id"], draft["state"], "invalid", sync_inbox=False)
                        _sync_inbox(c)
                    problems = [p for st in sub["stages"] for p in st.get("problems", ())]
                    raise contract.Problem("validation", "; ".join(problems) or f"Draft {draft['id']} failed configcheck.")
                if configcheck.GATE_ORDER[sub["gate"]] > configcheck.GATE_ORDER[worst_gate]:
                    worst_gate = sub["gate"]
            report = {
                "ok": True,
                "gate": worst_gate,
                "validator": configcheck.VALIDATOR,
                "stages": all_stages,
                "exposes": [],
                "warnings": [],
            }
            return report, live_ver

    old_bytes = configrepo.live_bytes(rel, root) if configrepo.is_repo(root) else None
    report = configcheck.check(
        draft["kind"],
        draft["name"],
        rel,
        draft["body"],
        author=draft["author"],
        config_dir=root,
        old_bytes=old_bytes,
    )
    if not report["ok"]:
        with foundation.tx() as c:
            _update_check_row(c, draft["numeric_id"], report)
            if draft["state"] in ("draft", "valid"):
                move(c, draft["numeric_id"], draft["state"], "invalid", sync_inbox=False)
            _sync_inbox(c)
        problems = [p for st in report["stages"] for p in st.get("problems", ())]
        raise contract.Problem("validation", "; ".join(problems) or f"Draft {draft['id']} failed configcheck.")

    if report["gate"] == "C" and draft["author"].startswith("run:"):
        raise contract.Problem(
            "gate_refused",
            f"Run-authored gate C draft {draft['id']} cannot be activated directly.",
        )
    return report, live_ver


def activate(
    draft_id: str | int,
    actor: str,
    base_version: str | int,
    idem: str = "",
    *,
    presence_passed: bool = False,
    source: str = "draft",
    fence=None,
    write_files: bool = True,
) -> dict:
    if actor.startswith("run:"):
        raise contract.Problem("gate_refused", f"Actor {actor!r} cannot activate config drafts.")
    draft = get(draft_id)
    if draft is None:
        raise contract.Problem("not_found", f"No config draft {draft_id}")
    if draft["gate"] == "C" and draft["author"].startswith("run:"):
        raise contract.Problem(
            "gate_refused",
            f"Run-authored gate C draft {draft['id']} cannot be activated directly; accept it to create an owner draft.",
        )

    root = paths.CONFIG_DIR.resolve()
    with configrepo.writer_lock(fence) as owned:
        configrepo.ensure_repo(fence=owned)
        configrepo.reconcile(fence=owned)
        draft = get(draft["numeric_id"])
        if draft is None:
            raise contract.Problem("not_found", f"No config draft {draft_id}")
        report, _ = _validate_base_and_check_for_activation(draft, actor, base_version, root=root)
        gate = report["gate"]
        with foundation.tx() as c:
            _update_check_row(c, draft["numeric_id"], report)
        if gate == "C" and not presence_passed:
            refreshed = get(draft["numeric_id"]) or draft
            raise PresenceRequired(refreshed, report)

        rel = draft["path"] or default_path(draft["kind"], draft["name"])
        arm_spec = _arm_activation_spec(draft, root)
        if arm_spec is not None:
            arm_commit, _, arm_files = arm_spec
            changes = {arm_rel: configrepo.commit_bytes(arm_commit, arm_rel, root) for arm_rel in arm_files}
            bases = {arm_rel: configrepo._live_blob(root, arm_rel) for arm_rel in arm_files}
            commit_sha = configrepo.commit(
                changes,
                bases,
                actor=actor,
                source=source,
                idem=idem,
                note=draft["why"] or f"activate {draft['id']}",
                draft_id=draft["id"],
                activate_c=(gate == "C" and presence_passed),
                fence=owned,
                write_files=write_files,
                merge_parent=arm_commit,
            )
        else:
            live_blob = configrepo._live_blob(root, rel)
            body_bytes = draft["body"].encode("utf-8")
            commit_sha = configrepo.commit(
                {rel: body_bytes},
                {rel: live_blob},
                actor=actor,
                source=source,
                idem=idem,
                note=draft["why"] or f"activate {draft['id']}",
                draft_id=draft["id"],
                activate_c=(gate == "C" and presence_passed),
                fence=owned,
                write_files=write_files,
            )
        # Ensure draft publication is finalized even on no-op or idempotent replay.
        with foundation.tx() as c, principals.acting(actor):
            finalize_activated_draft(c, draft["id"], commit_sha)
        new_ver = _live_ver_for(draft["kind"], draft["name"], rel)
        updated = get(draft["numeric_id"])
    return {"draft": public(updated), "commit": commit_sha, "version": new_ver}


def _find_merge_draft_for_rollback(kind: str, name: str, to: str | int, root: Path) -> tuple[dict, str] | None:
    candidates = list_drafts(state="active", kind=kind, name=name, limit=5)
    to_text = str(to).strip()
    for draft in candidates:
        act_sha = str(draft.get("activated_commit") or "")
        if not act_sha:
            continue
        parents = configrepo.parents_of(act_sha, root)
        if len(parents) < 2:
            continue
        first_parent = parents[0]
        if to_text in (first_parent, "0", "absent", str(draft.get("base_version") or "").strip()):
            return draft, first_parent
        rel = draft["path"] or default_path(kind, name)
        lookup_kind = "step_settings" if kind == "attention" else kind
        row = config_store.by_selector(lookup_kind, name, to_text)
        if row is not None and row.get("commit_sha") == first_parent:
            return draft, first_parent
        hist = configrepo.version_history(lookup_kind, name, rel)
        for entry in hist:
            if entry["commit_sha"] == first_parent and to_text in (
                str(entry.get("version")),
                str(entry.get("content_version") or ""),
                first_parent,
            ):
                return draft, first_parent
    return None


def rollback(
    kind: str,
    name: str,
    to: str | int,
    base_version: str | int,
    actor: str,
    idem: str = "",
    *,
    presence_passed: bool = False,
    fence=None,
) -> dict:
    if actor.startswith("run:"):
        raise contract.Problem("gate_refused", f"Actor {actor!r} cannot roll back config.")
    root = paths.CONFIG_DIR.resolve()
    merge_target = _find_merge_draft_for_rollback(kind, name, to, root) if configrepo.is_repo(root) else None
    if merge_target is not None:
        active_draft, first_parent = merge_target
        rel = active_draft["path"] or default_path(kind, name)
        lookup_kind = "step_settings" if kind == "attention" else kind
        with configrepo.writer_lock(fence) as owned:
            configrepo.ensure_repo(fence=owned)
            configrepo.reconcile(fence=owned)
            live_row = config_store.live_item(lookup_kind, name) or config_store.live_by_path(rel)
            live_ver = int(live_row["ver"]) if live_row and live_row.get("ver") is not None else 0
            if not _matches_base_version(base_version, live_ver, live_row):
                raise contract.Problem(
                    "version_conflict",
                    f"Config item {kind}/{name} is at version {live_ver}, not {base_version}.",
                    current={"kind": kind, "name": name, "version": live_ver},
                )
            act_sha = active_draft["activated_commit"]
            touched = configrepo.commit_changed_paths(act_sha, root, base=first_parent)
            if not touched:
                raise contract.Problem("validation", f"Merge commit {act_sha[:12]} changed no files to revert.")
            changes = {t_rel: configrepo.commit_bytes(first_parent, t_rel, root) for t_rel in touched}
            bases = {t_rel: configrepo._live_blob(root, t_rel) for t_rel in touched}
            revert_diff = configrepo.commit_diff(act_sha, first_parent, root)
            first_body = changes.get(rel)
            draft_num = add(
                kind=kind,
                name=name,
                origin=configrepo.origin_of(rel),
                path=rel,
                base_version=str(base_version),
                body=first_body.decode("utf-8") if isinstance(first_body, bytes) else "",
                diff=revert_diff,
                author=actor,
                why=f"rollback to {to}",
                gate="B",
                state="valid",
            )
            revert_id = format_id(draft_num)
            commit_sha = configrepo.commit(
                changes,
                bases,
                actor=actor,
                source="revert",
                idem=idem,
                note=f"rollback {kind}/{name} to {to}",
                draft_id=revert_id,
                activate_c=presence_passed,
                fence=owned,
            )
            with foundation.tx() as c, principals.acting(actor):
                finalize_activated_draft(c, revert_id, commit_sha)
            new_ver = _live_ver_for(kind, name, rel)
            updated = get(draft_num)
        return {"draft": public(updated), "commit": commit_sha, "version": new_ver}

    found = configitems.item(kind, name)
    if str(to).strip() in ("0", "absent"):
        rel = found["path"]
        with configrepo.writer_lock(fence) as owned:
            configrepo.ensure_repo(fence=owned)
            configrepo.reconcile(fence=owned)
            lookup_kind = "step_settings" if kind == "attention" else kind
            live_row = config_store.live_item(lookup_kind, name) or config_store.live_by_path(rel)
            live_ver = int(live_row["ver"]) if live_row and live_row.get("ver") is not None else 0
            if not _matches_base_version(base_version, live_ver, live_row):
                raise contract.Problem(
                    "version_conflict",
                    f"Config item {kind}/{name} is at version {live_ver}, not {base_version}.",
                    current={"kind": kind, "name": name, "version": live_ver},
                )
            live_blob = configrepo._live_blob(root, rel)
            draft_num = add(
                kind=kind,
                name=name,
                origin=configrepo.origin_of(rel),
                path=rel,
                base_version=str(base_version),
                body="",
                diff="",
                author=actor,
                why=f"rollback to {to}",
                gate="B",
                state="valid",
            )
            revert_id = format_id(draft_num)
            commit_sha = configrepo.commit(
                {rel: None},
                {rel: live_blob},
                actor=actor,
                source="revert",
                idem=idem,
                note=f"rollback {kind}/{name} to {to}",
                draft_id=revert_id,
                activate_c=presence_passed,
                fence=owned,
            )
            with foundation.tx() as c, principals.acting(actor):
                finalize_activated_draft(c, revert_id, commit_sha)
            new_ver = _live_ver_for(kind, name, rel)
            updated = get(draft_num)
        return {"draft": public(updated), "commit": commit_sha, "version": new_ver}

    target = configitems.read(found, version=str(to))
    content = target["content"]
    if not isinstance(content, str):
        raise contract.Problem("validation", f"Cannot roll back non-text config item {kind}/{name}.")
    draft, report = create_and_check(
        kind,
        name,
        content,
        author=actor,
        path=found["path"],
        base_version=base_version,
        why=f"rollback to {to}",
        run_check=True,
    )
    if not report or not report["ok"]:
        problems = [p for st in (report or {}).get("stages", ()) for p in st.get("problems", ())]
        raise contract.Problem("validation", "; ".join(problems) or f"Rollback draft {draft['id']} failed check.")
    return activate(
        draft["id"],
        actor,
        base_version,
        idem=idem,
        presence_passed=presence_passed,
        source="revert",
        fence=fence,
    )


def _resolve_try_step(spec: object) -> tuple[str, str, str, int | None]:
    """Return (repo_name, step_name, source_run_id, source_step_id) for one step selector in config.try."""
    if isinstance(spec, dict):
        raw_sid = spec.get("step_id")
        if isinstance(raw_sid, int) and not isinstance(raw_sid, bool):
            return _resolve_try_step(raw_sid)
        if isinstance(raw_sid, str) and raw_sid.strip().isdecimal():
            return _resolve_try_step(int(raw_sid.strip()))
        run_id = str(spec.get("run") or spec.get("run_id") or "").strip()
        step_name = str(spec.get("step") or spec.get("node") or "").strip()
        repo = str(spec.get("repo") or "").strip()
        if not repo and run_id:
            row = run_store.run_get(run_id)
            if row:
                repo = str(row.get("repo") or "")
        if not step_name:
            raise contract.Problem("validation", "Each step in config.try requires a step or node name.")
        return repo, step_name, run_id, None
    if isinstance(spec, int) and not isinstance(spec, bool):
        with foundation.reader() as c:
            row = c.execute(
                "SELECT s.id, s.run_id, s.step, r.repo FROM steps s LEFT JOIN runs r ON r.id = s.run_id WHERE s.id = ?",
                (spec,),
            ).fetchone()
        if row is None:
            raise contract.Problem("not_found", f"No step {spec}")
        return str(row["repo"] or ""), str(row["step"] or ""), str(row["run_id"] or ""), int(row["id"])
    if isinstance(spec, str):
        text = spec.strip()
        if not text:
            raise contract.Problem("validation", "Step selector cannot be empty.")
        if ":" in text:
            run_id, _, step_name = text.partition(":")
            row = run_store.run_get(run_id)
            repo = str(row.get("repo") or "") if row else ""
            return repo, step_name, run_id, None
        if text.isdecimal():
            return _resolve_try_step(int(text))
        return "", text, "", None
    raise contract.Problem("validation", f"Unsupported step selector {spec!r}")


def try_draft(kind: str, name: str, draft_id: str | int, steps_input: list, actor: str) -> dict:
    if kind == "retention":
        raise contract.Problem("validation", "Retention policy cannot be tried with config.try.")
    if not steps_input:
        raise contract.Problem("validation", "config.try requires at least one step.")
    draft = get(draft_id)
    if draft is None:
        raise contract.Problem("not_found", f"No config draft {draft_id}")
    if draft["kind"] != kind or draft["name"] != name:
        raise contract.Problem("validation", f"Draft {draft['id']} belongs to {draft['kind']}/{draft['name']}, not {kind}/{name}.")
    if draft["state"] not in ("draft", "valid"):
        raise contract.Problem("invalid_state", f"Draft {draft['id']} in state {draft['state']!r} cannot be tried.")

    resolved_steps = [_resolve_try_step(spec) for spec in steps_input]
    for _, step_name, source_run_id, source_step_id in resolved_steps:
        if source_run_id:
            source_run = run_store.run_get(source_run_id)
            if source_run is not None and (purged := source_run.get("files_purged")):
                raise contract.Problem(
                    "files_purged",
                    f"run {source_run_id} files were removed on {purged}",
                    purged_at=purged,
                )
        if purged_blob := blob_store.purged_input_for_step(source_run_id, step_name, step_id=source_step_id):
            blob_sha, purged_at = purged_blob
            raise contract.Problem(
                "files_purged",
                f"input blob {blob_sha} for run {source_run_id} step {step_name} was removed on {purged_at}",
                purged_at=purged_at,
            )

    rel = draft["path"] or default_path(kind, name)
    raw = draft["body"].encode("utf-8")
    ref_name, draft_commit = configrepo.commit_draft_ref(draft["id"], rel, raw, actor)
    content_sha = hashlib.sha256(raw).hexdigest()
    content_ver = f"{kind}/{name}@{content_sha[:12]}"

    started_runs: list[str] = []
    for repo_name, step_name, source_run_id, _ in resolved_steps:
        pin_res = pins.resolve(
            "trial",
            repo=repo_name or None,
            config_commit=draft_commit,
            draft_id=draft["id"],
        )
        run_id = record.start(
            "trial",
            repo=repo_name or None,
            only_step=step_name,
            continues=source_run_id or None,
            restart="trial",
            started_by=actor,
            resolution=pin_res,
            summary=f"trial of {draft['id']} ({step_name})",
        )
        # Record step + step_inputs pinned to the draft commit (delivers nothing).
        s_sha = pin_res.settings_sha_for(step_name)
        step_inputs = [
            {
                "role": kind,
                "name": name,
                "version": content_ver,
                "level": "run",
                "sha256": content_sha,
                "call": 1,
            },
        ]
        if kind != "step_settings":
            cfg_bytes = configrepo.commit_bytes(draft_commit, "config.toml")
            if cfg_bytes is not None:
                cfg_sha = hashlib.sha256(cfg_bytes).hexdigest()
                step_inputs.append({
                    "role": "step_settings",
                    "name": "defaults",
                    "version": f"step_settings/defaults@{cfg_sha[:12]}",
                    "level": "config",
                    "sha256": cfg_sha,
                    "call": 1,
                })
        from . import runner

        run_store.insert_steps(
            run_id,
            step_name,
            [(runner.Result(ok=True, text="", runner="", model=""), 0.0)],
            kind="trial",
            try_no=1,
            inputs=step_inputs,
            settings_sha=s_sha,
            settings_snapshots=pin_res.settings_snapshots,
        )
        record.settle(run_id, "done", summary=f"trial of {draft['id']} ({step_name})")
        started_runs.append(run_id)

    # Record trial run IDs in draft.evidence.
    existing_ev = draft["evidence"]
    if isinstance(existing_ev, list):
        updated_ev: object = [*existing_ev, *(r for r in started_runs if r not in existing_ev)]
    elif isinstance(existing_ev, dict):
        prior = list(existing_ev.get("trial_runs") or ())
        updated_ev = {**existing_ev, "trial_runs": [*prior, *(r for r in started_runs if r not in prior)]}
    else:
        updated_ev = list(started_runs)
    set_evidence(draft["numeric_id"], updated_ev)
    updated_draft = get(draft["numeric_id"])
    return {
        "draft": public(updated_draft),
        "ref": ref_name,
        "commit": draft_commit,
        "runs": started_runs,
    }


# ---------------------------------------------------------------------------
# Watch (serve tick comparison of 10 runs after vs 10 runs before)
# ---------------------------------------------------------------------------

def _run_duration(row: dict) -> float:
    active = row.get("active_seconds")
    if active is not None and float(active) > 0:
        return float(active)
    started, finished = row.get("started") or "", row.get("finished") or ""
    if started and finished:
        try:
            return max(0.0, (util.parse_iso(finished) - util.parse_iso(started)).total_seconds())
        except (ValueError, TypeError):
            pass
    return 0.0


def _summarize_runs(rows: list[dict]) -> dict:
    passes = sum(1 for r in rows if r.get("status") in PASSED_STATUSES)
    costs = [float(r.get("cost_usd") or 0.0) for r in rows]
    durations = [_run_duration(r) for r in rows]
    repairs = [float(r.get("repair_calls") or 0.0) for r in rows]
    return {
        "count": len(rows),
        "passes": passes,
        "median_cost": round(statistics.median(costs), 6) if costs else 0.0,
        "median_seconds": round(statistics.median(durations), 3) if durations else 0.0,
        "median_repairs": round(statistics.median(repairs), 3) if repairs else 0.0,
        "runs": [r["id"] for r in rows],
    }


def _runs_pinning_version(
    c: sqlite3.Connection,
    kind: str,
    name: str,
    version_tokens: set[str],
    sha256: str,
) -> list[dict]:
    if not version_tokens and not sha256:
        return []
    role_names = (kind, "prompt_append") if kind == "prompt" else (kind,)
    role_marks = ",".join("?" for _ in role_names)
    conds: list[str] = []
    args: list[object] = [*role_names]
    if version_tokens:
        tok_list = sorted(version_tokens)
        conds.append(f"si.version IN ({','.join('?' for _ in tok_list)})")
        args.extend(tok_list)
    if sha256:
        conds.append("si.sha256 = ?")
        args.append(sha256)
    name_clause = ""
    if kind == "step_settings":
        name_clause = "AND si.name IN (?, 'config.toml', '')"
        args.append(name)
    else:
        name_clause = "AND si.name = ?"
        args.append(name)
    excl_marks = ",".join("?" for _ in SETTLED_EXCLUDED)
    args.extend(sorted(SETTLED_EXCLUDED))
    sql = (
        "SELECT DISTINCT r.id, r.status, r.cost_usd, r.active_seconds, r.repair_calls, "  # noqa: S608
        "r.started, r.finished, r.seq "
        "FROM runs r "
        "JOIN steps s ON s.run_id = r.id "
        "JOIN step_inputs si ON si.step_id = s.id "
        f"WHERE si.role IN ({role_marks}) "
        f"AND ({' OR '.join(conds)}) "
        f"{name_clause} "
        f"AND r.status NOT IN ({excl_marks}) "
        "ORDER BY COALESCE(r.finished, r.started) ASC, r.id ASC"
    )
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def _previous_and_new_version_info(draft: dict) -> tuple[dict | None, dict | None]:
    kind, name = draft["kind"], draft["name"]
    rel = draft["path"] or default_path(kind, name)
    lookup_kind = "step_settings" if kind == "attention" else kind
    history = configrepo.version_history(lookup_kind, name, rel)
    activated_sha = draft["activated_commit"]
    new_entry = None
    prev_entry = None
    for idx, entry in enumerate(history):
        if entry["commit_sha"] == activated_sha:
            new_entry = entry
            if idx + 1 < len(history):
                prev_entry = history[idx + 1]
            break
    if new_entry is None and history:
        new_entry = history[0]
        if len(history) > 1:
            prev_entry = history[1]
    if new_entry is None:
        raw = draft["body"].encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        new_entry = {
            "version": None,
            "content_version": f"{kind}/{name}@{digest[:12]}",
            "commit_sha": activated_sha,
            "sha256": digest,
        }
    if prev_entry is None and draft["base_version"]:
        row = config_store.by_selector(lookup_kind, name, draft["base_version"])
        if row is not None:
            prev_entry = {
                "version": row.get("ver"),
                "content_version": row.get("version") or "",
                "commit_sha": row.get("commit_sha") or "",
                "sha256": row.get("sha256") or "",
            }
    return prev_entry, new_entry


def _tokens_for(entry: dict) -> tuple[set[str], str]:
    tokens: set[str] = set()
    if entry.get("content_version"):
        tokens.add(str(entry["content_version"]))
    if entry.get("version") is not None:
        tokens.add(str(entry["version"]))
    if entry.get("commit_sha"):
        tokens.add(str(entry["commit_sha"]))
    return tokens, str(entry.get("sha256") or "")


def watch_tick() -> list[dict]:
    """Compare settled runs after vs before for activated drafts; raise watch items on regression."""
    if not paths.DB_FILE.exists():
        return []
    cutoff = util.iso(util.now() - timedelta(days=WATCH_WINDOW_DAYS))
    results: list[dict] = []
    with foundation.tx() as c:
        if not available(c) or not inbox_store.available(c):
            return []
        has_si = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'step_inputs'"
        ).fetchone() is not None
        if not has_si:
            return []
        kinds_list = sorted(WATCH_KINDS)
        candidates = c.execute(
            f"SELECT * FROM config_drafts WHERE kind IN ({','.join('?' for _ in kinds_list)}) "  # noqa: S608
            "AND state = 'active' AND activated_commit != '' AND updated >= ? ORDER BY id ASC",
            (*kinds_list, cutoff),
        ).fetchall()
        for raw_row in candidates:
            draft = decode(raw_row)
            subj = f"draft:{draft['numeric_id']}"
            already = c.execute(
                "SELECT 1 FROM events_log WHERE kind = 'draft.changed' AND subject = ? "
                "AND json_extract(data, '$.watch') IS NOT NULL LIMIT 1",
                (subj,),
            ).fetchone()
            if already is not None:
                continue
            prev_info, new_info = _previous_and_new_version_info(draft)
            if prev_info is None or new_info is None:
                continue
            new_tokens, new_sha = _tokens_for(new_info)
            prev_tokens, prev_sha = _tokens_for(prev_info)
            after_runs = _runs_pinning_version(c, draft["kind"], draft["name"], new_tokens, new_sha)
            if len(after_runs) < WATCH_RUN_SAMPLE:
                continue
            after_sample = after_runs[:WATCH_RUN_SAMPLE]
            after_ids = {r["id"] for r in after_sample}
            before_all = [
                r for r in _runs_pinning_version(c, draft["kind"], draft["name"], prev_tokens, prev_sha)
                if r["id"] not in after_ids
            ]
            if not before_all:
                continue
            before_sample = before_all[-WATCH_RUN_SAMPLE:]
            before_stats = _summarize_runs(before_sample)
            after_stats = _summarize_runs(after_sample)

            reasons: list[str] = []
            if after_stats["passes"] <= before_stats["passes"] - 3:
                reasons.append(f"passes dropped from {before_stats['passes']} to {after_stats['passes']}")
            if before_stats["median_cost"] > 0 and after_stats["median_cost"] >= 1.5 * before_stats["median_cost"]:
                reasons.append(
                    f"median cost rose from {before_stats['median_cost']:.4f} to {after_stats['median_cost']:.4f}"
                )
            if (before_stats["median_seconds"] > 0
                    and after_stats["median_seconds"] >= 1.5 * before_stats["median_seconds"]):
                reasons.append(
                    f"median time rose from {before_stats['median_seconds']:.1f}s to {after_stats['median_seconds']:.1f}s"
                )
            if (before_stats["median_repairs"] > 0
                    and after_stats["median_repairs"] >= 1.5 * before_stats["median_repairs"]):
                reasons.append(
                    f"median repairs rose from {before_stats['median_repairs']:.1f} to {after_stats['median_repairs']:.1f}"
                )

            outcome = "drop" if reasons else "clear"
            prev_selector = (
                str(prev_info["version"]) if prev_info.get("version") is not None
                else prev_info.get("content_version") or prev_info.get("commit_sha") or ""
            )
            if outcome == "drop":
                inbox_store.raise_item(
                    c,
                    kind="watch",
                    subject=subj,
                    priority=2,
                    title=f"Watch regression on {draft['kind']}/{draft['name']} ({draft['id']}): {'; '.join(reasons)}",
                    detail={
                        "draft_id": draft["id"],
                        "kind": draft["kind"],
                        "name": draft["name"],
                        "previous_version": prev_selector,
                        "new_version": new_info.get("content_version") or "",
                        "before": before_stats,
                        "after": after_stats,
                        "reasons": reasons,
                        "stamp": f"watch:{draft['numeric_id']}:{draft['activated_commit']}",
                    },
                    verbs=["roll_back", "continue"],
                    options=[],
                    line=draft["name"],
                )
            event_store.emit(
                "draft.changed",
                subj,
                cls="durable",
                ver=draft["version"],
                data={
                    "watch": outcome,
                    "before": before_stats,
                    "after": after_stats,
                    "reasons": reasons,
                },
                connection=c,
            )
            results.append({"draft": draft["id"], "watch": outcome, "reasons": reasons})
    return results


# ---------------------------------------------------------------------------
# config.ask (config_ask role) and config.promote
# ---------------------------------------------------------------------------

def start_config_ask(
    kind: str,
    name: str,
    *,
    instruction: str,
    base_version: str | int,
    actor: str = "",
    level: str | None = None,
) -> dict:
    from . import control

    if kind not in configkinds.REGISTRY:
        raise contract.Problem("not_found", f"Unknown config kind {kind}")
    clean_instruction = str(instruction or "").strip()
    if not clean_instruction:
        raise contract.Problem("validation", "instruction is required for config.ask.")
    # Verify target config item exists (or resolves from engine).
    configitems.item(kind, name)

    who = actor or principals.current()
    run_id = record.start(
        "draft",
        trigger="manual",
        status="queued",
        pid=None,
        started_by=who,
        summary=f"config.ask {kind}/{name}: {util.first_line(clean_instruction, 160)}",
    )
    args = [
        "config", "ask-run",
        "--run-id", run_id,
        "--kind", kind,
        "--name", name,
        "--base-version", str(base_version),
        "--instruction", clean_instruction,
    ]
    if level:
        args.extend(["--level", level])
    record.write_launch(run_id, control.command(args), CONFIG_ASK_LOG)
    with record.failed_on_crash(run_id):
        control.detach(args, CONFIG_ASK_LOG)
    return run_store.run_get(run_id)


def _live_state_for_base_check(kind: str, name: str, rel: str) -> tuple[int, dict | None]:
    lookup_kind = "step_settings" if kind == "attention" else kind
    live_row = config_store.live_item(lookup_kind, name) or config_store.live_by_path(rel)
    live_ver = int(live_row["ver"]) if live_row and live_row.get("ver") is not None else 0
    return live_ver, live_row


def run_config_ask(
    kind: str,
    name: str,
    *,
    instruction: str,
    base_version: str | int,
    run_id: str = "",
    level: str | None = None,
) -> dict:
    from . import configcheck, runner, steps

    pin_res = pins.resolve("draft")
    cfg = pin_res.cfg
    clean_instruction = str(instruction or "").strip()
    if run_id:
        record.claim(run_id, pid=os.getpid(), resolution=pin_res)
    else:
        run_id = record.start(
            "draft",
            trigger="manual",
            summary=f"config.ask {kind}/{name}: {util.first_line(clean_instruction, 160)}",
            resolution=pin_res,
        )

    def tell(feed_kind: str, text: str) -> None:
        run_store.add_feed(run_id, feed_kind, text)

    with pins.use(pin_res), record.stoppable(), record.failed_on_crash(run_id):
        found = configitems.item(kind, name)
        loaded = configitems.read(found)
        current_content = loaded["content"]
        current_text = current_content if isinstance(current_content, str) else json.dumps(current_content, indent=2)
        target_name = name
        rel = default_path(kind, name)
        if kind == "prompt" and level and level.startswith("layer:"):
            repo_name = level.removeprefix("layer:").strip()
            if not name.endswith(".append"):
                rel = f"layers/{repo_name}/prompts/{name}.append.md"
                target_name = f"{repo_name}.{name}.append"

        live_ver, live_row = _live_state_for_base_check(kind, target_name, rel)
        if not _matches_base_version(base_version, live_ver, live_row):
            msg = f"version_conflict: {kind}/{target_name} is at version {live_ver}, not {base_version}"
            record.settle(run_id, "failed", end_line=f"failed: {msg}", summary=msg[:500])
            return {"run": run_id, "status": "failed", "code": "version_conflict", "error": msg}

        step_cfg = steps.resolve(cfg, None, "config_ask")
        ask_schema = prompts.engine_schema("config_ask")
        root = paths.CONFIG_DIR.resolve()
        old_bytes = configrepo.live_bytes(rel, root) if configrepo.is_repo(root) else (
            (root / rel).read_bytes() if (root / rel).is_file() else None
        )
        actor = f"run:{run_id}/config_ask"
        work = paths.draft_dir(run_id)
        work.mkdir(parents=True, exist_ok=True)
        feedback = ""
        candidate_body = ""
        why_text = ""
        used_model = step_cfg.model
        last_problems: list[str] = []
        valid = False
        try:
            for attempt in range(1, CONFIG_ASK_TRIES + 1):
                tell("config_ask", f"drafting {kind}/{target_name}, try {attempt} of {CONFIG_ASK_TRIES}")
                rendered = prompts.render_engine(
                    "config_ask",
                    kind=kind,
                    item_name=target_name,
                    path=rel,
                    instruction=clean_instruction,
                    current=current_text,
                    feedback=feedback,
                )
                res = runner.run(
                    rendered,
                    work,
                    model=step_cfg.model,
                    runner=step_cfg.runner,
                    effort=step_cfg.effort_on(step_cfg.runner),
                    trace=(run_id, "config_ask"),
                    cfg=cfg,
                    tools=[],
                    json_schema=ask_schema,
                    persist=False,
                    timeout=step_cfg.timeout_minutes * 60,
                    wait_out_limits=False,
                    atom="config_ask",
                    try_no=attempt,
                )
                used_model = res.model or step_cfg.model
                record.note(run_id, model=used_model, runner=res.runner)
                if not res.ok:
                    last_problems = [f"the model failed: {util.first_line(res.error, 300)}"]
                    tell("config_ask", f"  {last_problems[0]}")
                    if res.usage_limit:
                        break
                    feedback = (
                        "## Check problems from previous try\n\n"
                        + "\n".join(f"- {p}" for p in last_problems)
                        + "\n\nFix these problems and return the whole updated body and why."
                    )
                    continue
                answer = res.structured if isinstance(res.structured, dict) else {}
                raw_body = answer.get("body")
                why_text = str(answer.get("why") or "").strip()
                if not isinstance(raw_body, str) or not raw_body.strip():
                    last_problems = ["response body must be a non-empty string"]
                    tell("config_ask", f"  {last_problems[0]}")
                    feedback = (
                        "## Check problems from previous try\n\n"
                        "- response `body` must be a non-empty string.\n\n"
                        "Fix this and return the whole updated body and why."
                    )
                    continue
                try:
                    if kind == "prompt":
                        _, rel, candidate_body = _prepare_prompt_draft(target_name, raw_body, path=rel, level=level)
                    else:
                        candidate_body = raw_body
                except contract.Problem as exc:
                    last_problems = [str(exc)]
                    tell("config_ask", f"  validation failed: {last_problems[0]}")
                    feedback = (
                        "## Check problems from previous try\n\n"
                        f"- {last_problems[0]}\n\n"
                        "Fix this and return the whole updated body and why."
                    )
                    continue
                report = configcheck.check(
                    kind,
                    target_name,
                    rel,
                    candidate_body,
                    author=actor,
                    config_dir=root,
                    old_bytes=old_bytes,
                )
                if report["ok"]:
                    valid = True
                    break
                last_problems = [p for st in report["stages"] for p in st.get("problems", ())] or ["configcheck failed"]
                tell("config_ask", f"  check failed: {'; '.join(last_problems)}")
                feedback = (
                    "## Check problems from previous try\n\n"
                    + "\n".join(f"- {p}" for p in last_problems)
                    + "\n\nPrevious body:\n\n```\n"
                    + candidate_body
                    + "\n```\n\nFix all problems and return the whole updated body and why."
                )
        finally:
            shutil.rmtree(work, ignore_errors=True)

        if not valid:
            msg = f"no valid draft after {CONFIG_ASK_TRIES} tries: {'; '.join(last_problems)}"
            record.settle(run_id, "failed", end_line=f"failed: {msg}", summary=msg[:500])
            return {"run": run_id, "status": "failed", "error": msg}

        live_ver, live_row = _live_state_for_base_check(kind, target_name, rel)
        if not _matches_base_version(base_version, live_ver, live_row):
            msg = f"version_conflict: {kind}/{target_name} moved to version {live_ver}, expected {base_version}"
            record.settle(run_id, "failed", end_line=f"failed: {msg}", summary=msg[:500])
            return {"run": run_id, "status": "failed", "code": "version_conflict", "error": msg}

        draft_row, _ = create_and_check(
            kind,
            target_name,
            candidate_body,
            author=actor,
            path=rel,
            base_version=base_version,
            why=why_text,
            instruction=clean_instruction,
            model=used_model,
            level=level,
            run_check=True,
        )
        summary = f"drafted {draft_row['id']} for {kind}/{target_name}"
        record.settle(run_id, "done", end_line=f"done: {summary}", summary=summary)
        return {"run": run_id, "status": "done", "draft": public(draft_row)}


def promote(kind: str, name: str, *, actor: str = "") -> dict:
    from . import api, config, outbox
    from .store import outbox as outbox_store

    if kind not in PROMOTABLE_KINDS:
        raise contract.Problem("not_found", f"Config kind {kind!r} does not support promote.")
    features = (api.meta().get("features") or {})
    if not features.get("outbox"):
        raise contract.Problem("outbox_missing", "This instance has no outbox configured.")
    with foundation.reader() as c:
        if not outbox_store.available(c):
            raise contract.Problem("outbox_missing", "This instance has no outbox configured.")

    found = configitems.item(kind, name)
    loaded = configitems.read(found)
    content = loaded["content"]
    content_text = content if isinstance(content, str) else json.dumps(content, indent=2)
    digest = hashlib.sha256(content_text.encode("utf-8")).hexdigest()

    cfg, _ = config.load_or_problem()
    engine_repo = next((r.github for r in cfg.repos if r.name == cfg.arete_repo and r.github), "")
    if not engine_repo:
        engine_repo = "justinpbarnett/arete"

    engine_path = (
        f"arete/prompts/{name}.md" if kind == "prompt"
        else f"arete/schemas/{name}.json" if kind == "schema"
        else f"arete/adws/{name}.toml" if kind == "adw"
        else f"arete/routes/{name}.toml" if kind == "routes"
        else "arete/defaults/steps.toml"
    )
    payload = {
        "action": "create",
        "repo": engine_repo,
        "title": f"Promote config {kind}/{name} to engine",
        "body": f"Promote `{kind}/{name}` (`{loaded['path']}`) to `{engine_path}`.\n\nContent version: `{loaded['content_version']}`",
        "kind": kind,
        "name": name,
        "engine_path": engine_path,
        "content": content_text,
    }
    item_id = outbox.draft(
        target="github",
        kind="pr",
        object=f"github:{engine_repo}",
        audience="public",
        body=json.dumps(payload),
        drafted_by=actor or principals.current(),
        slot=f"config-promote:{kind}:{name}:{digest[:12]}",
        summary=f"Promote {kind}/{name} to engine",
        hold_as_draft=True,
    )
    return outbox_store.get(item_id)
