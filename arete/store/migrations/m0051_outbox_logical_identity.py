from __future__ import annotations

import json
from pathlib import Path

PHASE = "backfill"

_HOST_KEYS = ("workdir", "file", "env")
_FINISHED_STATES = frozenset(("sent", "rejected", "discarded"))


def _strip_host_keys(data: dict) -> dict:
    cleaned = {k: v for k, v in data.items() if k not in _HOST_KEYS}
    if isinstance(cleaned.get("item"), dict):
        cleaned["item"] = {k: v for k, v in cleaned["item"].items() if k not in _HOST_KEYS}
    return cleaned


def _parse_dict(raw: str | None) -> dict | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _resolve_head(workdir: str, branch: str, base: str) -> tuple[str, str]:
    from arete import util

    folder = Path(workdir) if workdir else None
    if folder is None or not folder.is_dir() or not branch:
        return "", ""
    try:
        head = util.git(["rev-parse", "--verify", f"{branch}^{{commit}}"], folder, check=True).strip()
    except (RuntimeError, OSError):
        return "", ""
    start = ""
    if base:
        try:
            start = util.git(["merge-base", f"origin/{base}", head], folder, check=False).strip()
        except (RuntimeError, OSError):
            start = ""
        if not start:
            try:
                start = util.git(["merge-base", base, head], folder, check=False).strip()
            except (RuntimeError, OSError):
                start = ""
    return head, start


def _logical_push_payload(src: dict, *, head: str, start: str, loop_name: str) -> dict:
    cleaned = _strip_host_keys(src)
    payload: dict[str, object] = {
        **cleaned,
        "mode": str(cleaned.get("mode") or "land"),
        "repo": str(cleaned.get("repo") or ""),
        "base": str(cleaned.get("base") or ""),
        "branch": str(cleaned.get("branch") or ""),
        "start": str(cleaned.get("start") or start),
        "head": str(cleaned.get("head") or head),
    }
    if loop_name and not payload.get("loop"):
        payload["loop"] = loop_name
    return _strip_host_keys(payload)


def _logical_pr_payload(src: dict, *, head: str) -> dict:
    cleaned = _strip_host_keys(src)
    payload: dict[str, object] = {
        **cleaned,
        "action": str(cleaned.get("action") or "create"),
        "repo": str(cleaned.get("repo") or ""),
        "name": str(cleaned.get("name") or cleaned.get("repo") or ""),
        "base": str(cleaned.get("base") or ""),
        "branch": str(cleaned.get("branch") or ""),
        "head": str(cleaned.get("head") or head),
        "lease": str(cleaned.get("lease") or ""),
    }
    if payload["action"] == "create" and "push_branch" not in payload:
        payload["push_branch"] = True
    return _strip_host_keys(payload)


def _target_for_kind(kind: str, current: str) -> str:
    if current and current != "legacy":
        return current
    if kind == "push":
        return "git"
    if kind in ("pr", "issue", "comment", "reply", "label", "status"):
        return "github"
    if kind == "loop":
        return "loop"
    return current or "legacy"


def apply(conn) -> None:
    from arete import util

    has_outbox = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outbox'").fetchone()
    if not has_outbox:
        return
    tag_row = conn.execute("SELECT value FROM schema_meta WHERE key = 'instance_tag'").fetchone()
    tag = str(tag_row[0]) if tag_row and tag_row[0] else "local"
    has_approvals = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = 'approvals'"
    ).fetchone() is not None
    now = util.iso()

    rows = [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY id ASC").fetchall()]
    for row in rows:
        item_id = int(row["id"])
        kind = str(row["kind"] or "")
        state = str(row["state"] or "")
        legacy_id = row.get("legacy_approval")
        revs = [
            dict(r)
            for r in conn.execute(
                "SELECT rev, body FROM outbox_revisions WHERE item = ? ORDER BY rev ASC",
                (item_id,),
            ).fetchall()
        ]
        legacy_emit: dict | None = None
        if legacy_id is not None and has_approvals:
            arow = conn.execute("SELECT emit FROM approvals WHERE id = ?", (legacy_id,)).fetchone()
            if arow is not None:
                legacy_emit = _parse_dict(arow[0])

        latest_body = revs[-1]["body"] if revs else ""
        parsed_body = _parse_dict(latest_body)
        source_item: dict | None = None
        if legacy_emit is not None:
            inner = legacy_emit.get("item")
            source_item = dict(inner) if isinstance(inner, dict) else dict(legacy_emit)
        elif parsed_body is not None:
            inner = parsed_body.get("item")
            source_item = dict(inner) if isinstance(inner, dict) and "type" in parsed_body else dict(parsed_body)

        new_target = _target_for_kind(kind, str(row.get("target") or ""))
        new_marker = row.get("marker") or f"arete:{tag}:ob-{item_id}"

        if kind in ("push", "pr"):
            if state in _FINISHED_STATES:
                for rev_row in revs:
                    rev_dict = _parse_dict(rev_row["body"])
                    if rev_dict is not None:
                        cleaned = _strip_host_keys(rev_dict)
                        conn.execute(
                            "UPDATE outbox_revisions SET body = ? WHERE item = ? AND rev = ?",
                            (json.dumps(cleaned), item_id, rev_row["rev"]),
                        )
                if legacy_emit is not None and not parsed_body and revs:
                    cleaned = _strip_host_keys(source_item or {})
                    conn.execute(
                        "UPDATE outbox_revisions SET body = ? WHERE item = ? AND rev = ?",
                        (json.dumps(cleaned), item_id, revs[-1]["rev"]),
                    )
                if new_target != row.get("target") or not row.get("marker"):
                    conn.execute(
                        "UPDATE outbox SET target = ?, marker = ? WHERE id = ?",
                        (new_target, new_marker, item_id),
                    )
            elif source_item is not None:
                workdir = str(source_item.get("workdir") or "")
                branch = str(source_item.get("branch") or "")
                base = str(source_item.get("base") or "")
                existing_head = str(source_item.get("head") or "")
                existing_start = str(source_item.get("start") or "")
                head = existing_head
                start = existing_start
                missing_error = ""
                if not head and (workdir or branch):
                    head, resolved_start = _resolve_head(workdir, branch, base)
                    start = start or resolved_start
                    if not head:
                        missing_error = (
                            f"backfill failed: folder {workdir or '(missing)'} or branch {branch or '(missing)'} is gone"
                        )
                if kind == "push":
                    logical = _logical_push_payload(
                        source_item, head=head, start=start, loop_name=str(row.get("loop") or "")
                    )
                else:
                    logical = _logical_pr_payload(source_item, head=head)
                for rev_row in revs[:-1]:
                    rev_dict = _parse_dict(rev_row["body"])
                    if rev_dict is not None:
                        conn.execute(
                            "UPDATE outbox_revisions SET body = ? WHERE item = ? AND rev = ?",
                            (json.dumps(_strip_host_keys(rev_dict)), item_id, rev_row["rev"]),
                        )
                if revs:
                    conn.execute(
                        "UPDATE outbox_revisions SET body = ? WHERE item = ? AND rev = ?",
                        (json.dumps(logical), item_id, revs[-1]["rev"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO outbox_revisions (item, rev, base_rev, by, body, at) VALUES (?, 1, NULL, ?, ?, ?)",
                        (item_id, row["drafted_by"], json.dumps(logical), row["created"]),
                    )
                if missing_error:
                    conn.execute(
                        "UPDATE outbox SET target = ?, marker = ?, state = 'failed', send_at = NULL, "
                        "error = ?, version = version + 1, updated = ? WHERE id = ?",
                        (new_target, new_marker, missing_error, now, item_id),
                    )
                    conn.execute(
                        "UPDATE delivery_attempts SET ended = ?, outcome = 'refused', error = ? "
                        "WHERE item = ? AND ended IS NULL",
                        (now, missing_error, item_id),
                    )
                else:
                    obj = row.get("object") or (
                        f"git:{logical['repo']}" if kind == "push" and logical.get("repo")
                        else f"github:{logical['repo']}" if kind == "pr" and logical.get("repo")
                        else ""
                    )
                    conn.execute(
                        "UPDATE outbox SET target = ?, marker = ?, object = ? WHERE id = ?",
                        (new_target, new_marker, obj, item_id),
                    )
                    if state == "sending" and head:
                        conn.execute(
                            "UPDATE delivery_attempts SET external_id = COALESCE(external_id, ?) "
                            "WHERE item = ? AND ended IS NULL",
                            (head, item_id),
                        )
        elif legacy_id is not None:
            payload = _strip_host_keys(source_item or legacy_emit or {})
            if legacy_emit is not None and not parsed_body and revs:
                conn.execute(
                    "UPDATE outbox_revisions SET body = ? WHERE item = ? AND rev = ?",
                    (json.dumps(payload), item_id, revs[-1]["rev"]),
                )
            obj = str(row.get("object") or "")
            if not obj and isinstance(payload, dict):
                if kind == "issue":
                    dest = payload.get("destination") if isinstance(payload.get("destination"), dict) else {}
                    if dest.get("tracker") == "jira" and dest.get("project"):
                        new_target = "jira"
                        obj = f"jira:{dest['project']}"
                    else:
                        slug = str(dest.get("github") or "")
                        if not slug:
                            raw_repo = str(payload.get("repo") or "").strip()
                            if "/" in raw_repo:
                                slug = raw_repo
                            elif raw_repo:
                                try:
                                    from arete import config, emits

                                    cfg_repo = emits.config_repo(config.load(), raw_repo)
                                    slug = cfg_repo.github if cfg_repo is not None else ""
                                except Exception:
                                    slug = ""
                        if slug:
                            obj = f"github:{slug}"
                elif kind == "loop" and payload.get("loop"):
                    obj = f"loop:{payload['loop']}"
            if new_target != row.get("target") or not row.get("marker") or obj != str(row.get("object") or ""):
                conn.execute(
                    "UPDATE outbox SET target = ?, marker = ?, object = ? WHERE id = ?",
                    (new_target, new_marker, obj, item_id),
                )
