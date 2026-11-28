from __future__ import annotations

import concurrent.futures
import json
import secrets
import threading
import tomllib
from contextlib import suppress

from . import config, events, gh, jira, paths, principals, tracker, util
from .store import db as foundation
from .store import events as event_store
from .store import inbox as inbox_store
from .store import outbox as outbox_store
from .store import tasks as task_store
from .store import triage as triage_store
from .store._rows import validate_columns

_DEFAULT_FIELDS: dict[str, str] = {
    "title": "remote",
    "body": "remote",
    "status": "remote",
    "assignee": "remote",
    "parent": "remote",
    "priority": "local",
    "labels": "remote",
}
_DEFAULT_STATE_LABELS: dict[str, tuple[str, ...]] = {
    "github": tuple(f"agent:{s}" for s in config.RUN_LABELS),
    "jira": tuple(f"agent-{s}" for s in config.RUN_LABELS),
}
_TASK_MAPPED_FIELDS = frozenset(("title", "body", "labels"))
_TASK_REMOTE_COLUMNS = frozenset(("title", "body", "updated"))
_OPEN_OUTBOX_STATES = ("draft", "edited", "approved", "held", "sending")
_SYNC_SEM = threading.Semaphore(4)


def _read_toml_file(path) -> dict:
    if not path.is_file():
        return {}
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def sync_config(system: str) -> dict:
    sys_norm = str(system or "").strip().lower()
    default_data = _read_toml_file(paths.DEFAULTS_DIR / "sync" / f"{sys_norm}.toml")
    override_data = _read_toml_file(paths.CONFIG_DIR / "sync" / f"{sys_norm}.toml")

    fields = dict(_DEFAULT_FIELDS)
    for source in (default_data, override_data):
        raw_fields = source.get("fields")
        if isinstance(raw_fields, dict):
            for k, v in raw_fields.items():
                if v in ("remote", "local"):
                    fields[str(k)] = str(v)

    priority_map: dict[str, int | str] = {}
    for source in (default_data, override_data):
        raw_pm = source.get("priority_map")
        if isinstance(raw_pm, dict):
            priority_map.update(raw_pm)

    state_labels = list(_DEFAULT_STATE_LABELS.get(sys_norm, ()))
    for source in (default_data, override_data):
        raw_sl = source.get("state_labels")
        if isinstance(raw_sl, list):
            state_labels = [str(item) for item in raw_sl if isinstance(item, str) and item.strip()]

    return {
        "fields": fields,
        "priority_map": priority_map,
        "state_labels": state_labels,
    }


def vocabularies_config() -> dict:
    default_data = _read_toml_file(paths.DEFAULTS_DIR / "vocabularies.toml")
    override_data = _read_toml_file(paths.CONFIG_DIR / "vocabularies.toml")

    task_states: dict[str, dict] = {}
    legacy: dict[str, dict] = {}
    for source in (default_data, override_data):
        if isinstance(source.get("task_states"), dict):
            task_states.update(source["task_states"])
        if isinstance(source.get("legacy"), dict):
            legacy.update(source["legacy"])
    return {"task_states": task_states, "legacy": legacy}


def map_legacy_state(status: str, fault: int = 0) -> tuple[str, int]:
    raw = str(status or "").strip()
    vocab = vocabularies_config()
    legacy = vocab.get("legacy") or {}
    entry = legacy.get(raw)
    if isinstance(entry, dict) and isinstance(entry.get("state"), str):
        mapped_fault = 1 if entry.get("fault") else (1 if fault else 0)
        return str(entry["state"]), mapped_fault
    return config.canonical_task_state(raw, fault)


def ref_fields_available(c) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ref_fields'").fetchone() is not None


def ref_fields_for(c, ref_id: int) -> list[dict]:
    if not ref_fields_available(c):
        return []
    rows = c.execute(
        "SELECT field, base, remote, remote_at, remote_by, owner, conflict "
        "FROM ref_fields WHERE ref = ? ORDER BY field ASC",
        (int(ref_id),),
    ).fetchall()
    return [
        {
            "field": str(r["field"]),
            "base": r["base"],
            "remote": r["remote"],
            "remote_at": r["remote_at"],
            "remote_by": r["remote_by"],
            "owner": str(r["owner"]),
            "conflict": int(r["conflict"] or 0),
        }
        for r in rows
    ]


def _has_open_outbox_for_ref(c, ref_id: int) -> bool:
    if not outbox_store.available(c):
        return False
    placeholders = ",".join("?" * len(_OPEN_OUTBOX_STATES))
    row = c.execute(
        f"SELECT 1 FROM outbox WHERE ref_id IN (?, ?) AND state IN ({placeholders}) LIMIT 1",  # noqa: S608
        (str(ref_id), task_store.format_ref_id(ref_id), *_OPEN_OUTBOX_STATES),
    ).fetchone()
    return row is not None


def recompute_ref_sync(c, ref_id: int) -> str:
    row = c.execute("SELECT sync, fetched_at FROM refs WHERE id = ?", (int(ref_id),)).fetchone()
    if row is None:
        return "local"
    if ref_fields_available(c):
        has_conflict = c.execute(
            "SELECT 1 FROM ref_fields WHERE ref = ? AND conflict = 1 LIMIT 1",
            (int(ref_id),),
        ).fetchone() is not None
        if has_conflict:
            return "conflict"
    if _has_open_outbox_for_ref(c, int(ref_id)):
        return "ahead"
    if str(row["sync"] or "") == "off":
        return "off"
    if row["fetched_at"]:
        return "synced"
    return "local"


def _emit_ref_changed(c, ref_id: int, *, actor: str = "hand", idem: str | None = None) -> dict:
    ref_dict = task_store.TaskStore.ref_get(c, int(ref_id))
    if ref_dict is None:
        return {}
    event_actor = actor if actor and actor != "owner" else task_store._db_owner_principal(c)
    data = {k: v for k, v in ref_dict.items() if k != "num"}
    with principals.acting(event_actor):
        events.emit(
            "ref.changed",
            f"ref:{ref_dict['num']}",
            cls="durable",
            ver=ref_dict["version"],
            data=data,
            idem=idem,
            connection=c,
        )
    return ref_dict


def _task_labels_json(c, task_num: int) -> str:
    rows = c.execute("SELECT label FROM task_labels WHERE task = ? ORDER BY label ASC", (int(task_num),)).fetchall()
    return json.dumps([str(r[0]) for r in rows])


def _set_task_labels_from_json(c, task_num: int, raw_json: str) -> None:
    try:
        parsed = json.loads(raw_json)
    except ValueError:
        parsed = []
    labels = sorted({str(x).strip() for x in parsed if isinstance(x, str) and str(x).strip()}) if isinstance(parsed, list) else []
    c.execute("DELETE FROM task_labels WHERE task = ?", (int(task_num),))
    if labels:
        c.executemany(
            "INSERT OR IGNORE INTO task_labels (task, label) VALUES (?, ?)",
            [(int(task_num), lb) for lb in labels],
        )


def _local_field_value(c, task_row: dict, task_num: int, field_name: str) -> str:
    if field_name == "title":
        return str(task_row.get("title") or "")
    if field_name == "body":
        return str(task_row.get("body") or "")
    if field_name == "labels":
        return _task_labels_json(c, task_num)
    return ""


def _write_task_remote_updates(
    c,
    task_row: dict,
    task_num: int,
    updates: dict[str, str],
    *,
    actor: str,
) -> dict:
    if not updates:
        return task_row
    previous: dict[str, object] = {}
    col_updates: dict[str, object] = {}
    labels_changed = False
    if "title" in updates:
        previous["title"] = str(task_row.get("title") or "")
        col_updates["title"] = updates["title"]
    if "body" in updates:
        previous["body"] = str(task_row.get("body") or "")
        col_updates["body"] = updates["body"]
    if "labels" in updates:
        previous["labels"] = _task_labels_json(c, task_num)
        _set_task_labels_from_json(c, task_num, updates["labels"])
        labels_changed = True

    now = util.iso()
    col_updates["updated"] = now
    contracted = task_store.is_contracted(c)
    identity = int(task_row["id"]) if contracted else task_row["id"]
    with principals.acting(actor):
        if col_updates:
            validate_columns(col_updates, _TASK_REMOTE_COLUMNS, "tasks")
            setters = [f"{k} = ?" for k in col_updates]
            setters.append("version = version + 1")
            c.execute(
                f"UPDATE tasks SET {', '.join(setters)} WHERE id = ?",  # noqa: S608
                (*col_updates.values(), identity),
            )
            if "body" in updates and task_store.has_identity_expand(c):
                task_store.TaskStore.sync_body_links(c, task_num, str(updates["body"]))
        refreshed_raw = task_store._select_task_row(c, task_num)
        presented = task_store._present_task_row(c, refreshed_raw, include_relations=False)
        event_data = {
            k: v for k, v in presented.items() if k not in ("raw_id", "num", "refs", "primary_ref", "links")
        }
        event_data["previous"] = previous
        if labels_changed:
            event_data["labels"] = json.loads(_task_labels_json(c, task_num))
        events.emit(
            "task.changed",
            f"task:{task_num}",
            cls="derived",
            ver=presented["version"],
            task=task_store.format_task_id(task_num),
            data=event_data,
            connection=c,
        )
    return presented


def apply_primary_remote_fields(c, task_num: int, ref_id: int, *, actor: str = "hand") -> bool:
    if not ref_fields_available(c):
        return False
    rows = ref_fields_for(c, ref_id)
    if not rows:
        return False
    task_raw = task_store._select_task_row(c, task_num)
    if task_raw is None:
        return False
    task_dict = dict(task_raw)
    updates: dict[str, str] = {}
    for entry in rows:
        if entry["owner"] != "remote" or entry["remote"] is None:
            continue
        field_name = entry["field"]
        remote_val = str(entry["remote"])
        c.execute(
            "UPDATE ref_fields SET base = ?, conflict = 0 WHERE ref = ? AND field = ?",
            (remote_val, int(ref_id), field_name),
        )
        if field_name in _TASK_MAPPED_FIELDS:
            local_val = _local_field_value(c, task_dict, task_num, field_name)
            if local_val != remote_val:
                updates[field_name] = remote_val
    if updates:
        _write_task_remote_updates(c, task_dict, task_num, updates, actor=actor or "hand")
    new_sync = recompute_ref_sync(c, ref_id)
    c.execute(
        "UPDATE refs SET sync = ?, version = version + 1, updated = ? WHERE id = ?",
        (new_sync, util.iso(), int(ref_id)),
    )
    _emit_ref_changed(c, ref_id, actor=actor or "hand")
    return bool(updates)


def draft_remote_field_edits(
    c,
    task_num: int,
    old_task: dict,
    edited_fields: dict,
    *,
    actor: str = "hand",
) -> list[int]:
    if not task_store.has_identity_expand(c) or not outbox_store.available(c):
        return []
    primary = c.execute(
        "SELECT r.* FROM task_refs tr JOIN refs r ON r.id = tr.ref_id "
        "WHERE tr.task_id = ? AND tr.role = 'primary' LIMIT 1",
        (int(task_num),),
    ).fetchone()
    if primary is None:
        return []
    system = str(primary["system"] or "")
    if system not in ("github", "jira"):
        return []
    scfg = sync_config(system)
    field_owners = scfg["fields"]
    ref_id = int(primary["id"])
    ref_key = str(primary["key"])
    obj = f"{system}:{ref_key}"
    base_remote_updated = primary["remote_updated"]
    task_label = task_store.format_task_id(task_num)

    from . import outbox

    drafted: list[int] = []
    for field_name, new_val in edited_fields.items():
        if field_name not in ("title", "body"):
            continue
        if field_owners.get(field_name) != "remote":
            continue
        old_val = str(old_task.get(field_name) or "")
        new_str = str(new_val or "")
        if new_str == old_val:
            continue
        payload = json.dumps({
            "action": "edit",
            "field": field_name,
            "value": new_str,
            "ref_id": ref_id,
        })
        item_id = outbox.draft(
            c,
            target=system,
            kind="issue",
            object=obj,
            body=payload,
            drafted_by=actor or "hand",
            slot=f"sync:ref:{ref_id}:field:{field_name}:{secrets.token_hex(6)}",
            task_id=task_label,
            ref_id=str(ref_id),
            base_remote_updated=base_remote_updated,
            summary=f"update {field_name} on {ref_key}",
            hold_as_draft=True,
        )
        drafted.append(item_id)

    if drafted:
        new_sync = recompute_ref_sync(c, ref_id)
        c.execute(
            "UPDATE refs SET sync = ?, version = version + 1, updated = ? WHERE id = ?",
            (new_sync, util.iso(), ref_id),
        )
        _emit_ref_changed(c, ref_id, actor=actor or "hand")
    return drafted


def _extract_actor(obj: object) -> str:
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        for key in ("login", "accountId", "name", "displayName"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _extract_github_payload(ref_dict: dict, payload: dict, state_labels: set[str]) -> dict:
    title = str(payload.get("title") or "").strip() if "title" in payload else None
    body = str(payload.get("body") or "") if "body" in payload else None
    url = str(payload.get("url") or ref_dict.get("url") or "")
    ext_state = str(payload.get("state") or ref_dict.get("ext_state") or "").strip().lower()
    raw_updated = str(payload.get("updatedAt") or payload.get("createdAt") or "")
    remote_updated = triage_store.iso_updated(raw_updated) if raw_updated else ref_dict.get("remote_updated")

    actor = ""
    for candidate_key in ("updatedBy", "actor", "lastEditor", "remote_by", "editor", "author"):
        if candidate_key in payload:
            actor = _extract_actor(payload.get(candidate_key))
            if actor:
                break

    raw_labels: list[str] | None = None
    if "labels" in payload and isinstance(payload.get("labels"), list):
        raw_labels = []
        for lb in payload["labels"]:
            if isinstance(lb, dict) and isinstance(lb.get("name"), str):
                raw_labels.append(lb["name"])
            elif isinstance(lb, str):
                raw_labels.append(lb)

    fields: dict[str, str] = {}
    if title is not None:
        fields["title"] = title
    if body is not None:
        fields["body"] = body
    if "state" in payload:
        fields["status"] = ext_state
    if "assignees" in payload and isinstance(payload.get("assignees"), list):
        logins = [_extract_actor(a) for a in payload["assignees"]]
        fields["assignee"] = ",".join(x for x in logins if x)
    elif "assignee" in payload:
        fields["assignee"] = _extract_actor(payload.get("assignee"))
    if raw_labels is not None:
        non_state = sorted({lb for lb in raw_labels if lb not in state_labels})
        fields["labels"] = json.dumps(non_state)

    facts_patch: dict[str, object] = {}
    if raw_labels is not None:
        facts_patch["labels"] = sorted(set(raw_labels))
    if ext_state:
        facts_patch["state"] = ext_state

    return {
        "title": title if title is not None else str(ref_dict.get("title") or ""),
        "url": url,
        "ext_state": ext_state,
        "remote_updated": remote_updated,
        "remote_actor": actor,
        "raw_labels": raw_labels,
        "fields": fields,
        "facts_patch": facts_patch,
    }


def _extract_jira_payload(
    ref_dict: dict,
    payload: dict,
    state_labels: set[str],
    *,
    changelog: list[dict] | None = None,
    browse_url: str = "",
) -> dict:
    f = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    title = str(f.get("summary") or "").strip() if "summary" in f else None
    body = str(f.get("description") or "") if "description" in f else None
    url = str(browse_url or payload.get("url") or ref_dict.get("url") or "")
    status_obj = f.get("status") if isinstance(f.get("status"), dict) else {}
    ext_state = str(status_obj.get("name") or "") if "status" in f else str(ref_dict.get("ext_state") or "")
    raw_updated = str(f.get("updated") or f.get("created") or "")
    remote_updated = triage_store.iso_updated(raw_updated) if raw_updated else ref_dict.get("remote_updated")

    actor = _extract_actor(payload.get("remote_by")) or _extract_actor(f.get("updateAuthor"))
    if not actor and changelog:
        newest = max(changelog, key=lambda h: str(h.get("created") or ""), default=None)
        if isinstance(newest, dict):
            actor = _extract_actor(newest.get("author"))
    if not actor:
        actor = _extract_actor(f.get("reporter")) or _extract_actor(f.get("creator")) or _extract_actor(f.get("assignee"))

    raw_labels: list[str] | None = None
    if "labels" in f and isinstance(f.get("labels"), list):
        raw_labels = [str(lb) for lb in f["labels"] if isinstance(lb, str)]

    fields: dict[str, str] = {}
    if title is not None:
        fields["title"] = title
    if body is not None:
        fields["body"] = body
    if "status" in f:
        fields["status"] = ext_state
    if "assignee" in f:
        fields["assignee"] = _extract_actor(f.get("assignee"))
    if "parent" in f:
        parent_obj = f.get("parent") if isinstance(f.get("parent"), dict) else {}
        fields["parent"] = str(parent_obj.get("key") or "")
    if "priority" in f:
        prio_obj = f.get("priority") if isinstance(f.get("priority"), dict) else {}
        fields["priority"] = str(prio_obj.get("name") or "")
    if raw_labels is not None:
        non_state = sorted({lb for lb in raw_labels if lb not in state_labels})
        fields["labels"] = json.dumps(non_state)

    facts_patch: dict[str, object] = {}
    if raw_labels is not None:
        facts_patch["labels"] = sorted(set(raw_labels))
    if "parent" in f:
        facts_patch["parent"] = fields["parent"]
    if status_obj:
        cat = (status_obj.get("statusCategory") or {}).get("key") if isinstance(status_obj.get("statusCategory"), dict) else ""
        if cat:
            facts_patch["status_category"] = str(cat)

    return {
        "title": title if title is not None else str(ref_dict.get("title") or ""),
        "url": url,
        "ext_state": ext_state,
        "remote_updated": remote_updated,
        "remote_actor": actor,
        "raw_labels": raw_labels,
        "fields": fields,
        "facts_patch": facts_patch,
    }


def _fetch_remote_object(ref_dict: dict, *, cfg=None) -> tuple[dict | None, list[dict] | None, str]:
    system = str(ref_dict["system"])
    key = str(ref_dict["key"])
    if system == "github":
        if "#" not in key:
            raise ValueError(f"invalid github ref key {key!r}")
        owner_repo, num_str = key.rsplit("#", 1)
        issue = gh.issue(owner_repo, int(num_str))
        return (issue if isinstance(issue, dict) else {}), None, ""
    if system == "jira":
        if cfg is None:
            loaded, _ = config.load_or_problem()
            cfg = loaded or config.Config()
        client = jira.Client.from_env(cfg.jira)
        issue = client.issue(key, tracker.JIRA_FIELDS)
        history: list[dict] | None = None
        with suppress(Exception):
            history = client.changelog(key)
        browse = ""
        with suppress(Exception):
            browse = client.browse_url(key)
        return (issue if isinstance(issue, dict) else {}), history, browse
    return None, None, ""


def _maybe_retry_on_label_removal(
    c,
    *,
    system: str,
    ref_key: str,
    task_num: int,
    task_row: dict,
    raw_labels: list[str] | None,
    state_labels: set[str],
    remote_actor: str,
) -> bool:
    if raw_labels is None or system not in ("github", "jira") or not outbox_store.available(c):
        return False
    if tracker.LABEL not in raw_labels:
        return False
    cur_state = str(task_row.get("state") or "")
    cur_fault = int(task_row.get("fault") or 0)
    if cur_state == "ready" and cur_fault == 0:
        return False

    obj = f"{system}:{ref_key}"
    placeholders = ",".join("?" * len(_OPEN_OUTBOX_STATES))
    open_label = c.execute(
        f"SELECT 1 FROM outbox WHERE object = ? AND kind = 'label' AND state IN ({placeholders}) LIMIT 1",  # noqa: S608
        (obj, *_OPEN_OUTBOX_STATES),
    ).fetchone()
    if open_label is not None:
        return False

    newest = c.execute(
        "SELECT id, rev, state FROM outbox "
        "WHERE object = ? AND kind = 'label' AND state NOT IN ('discarded', 'rejected') "
        "ORDER BY id DESC LIMIT 1",
        (obj,),
    ).fetchone()
    if newest is None or newest["state"] != "sent":
        return False

    rev_row = outbox_store.latest_revision(c, int(newest["id"]), int(newest["rev"]))
    if rev_row is None:
        return False
    try:
        payload = json.loads(rev_row["body"] or "{}")
    except ValueError:
        return False
    added = payload.get("add") if isinstance(payload, dict) else None
    removed = payload.get("remove") if isinstance(payload, dict) else None
    added_state = {str(lb) for lb in added if str(lb) in state_labels} if isinstance(added, list) else set()
    removed_state = {str(lb) for lb in removed if str(lb) in state_labels} if isinstance(removed, list) else set()
    if added_state:
        if not added_state.isdisjoint(raw_labels):
            return False
    elif removed_state:
        if not state_labels.isdisjoint(raw_labels):
            return False
    else:
        return False

    actor = f"ext:{system}:{remote_actor or 'unknown'}"
    with principals.acting(actor):
        task_store.task_set_state(task_num, "ready", 0)
    return True


def _sync_conflict_inbox(
    c,
    *,
    ref_id: int,
    ref_dict: dict,
    task_num: int,
    task_dict: dict,
) -> None:
    if not inbox_store.available(c) or not ref_fields_available(c):
        return
    conflicted = [r for r in ref_fields_for(c, ref_id) if r["conflict"] == 1]
    subject = f"ref:{ref_id}"
    if not conflicted:
        return
    field_details = [
        {
            "field": entry["field"],
            "base": entry["base"],
            "local": _local_field_value(c, task_dict, task_num, entry["field"]),
            "remote": entry["remote"],
            "remote_at": entry["remote_at"],
            "remote_by": entry["remote_by"],
        }
        for entry in conflicted
    ]
    names = ", ".join(entry["field"] for entry in conflicted)
    task_label = task_store.format_task_id(task_num)
    detail = {
        "ref_id": int(ref_id),
        "ref": task_store.format_ref_id(ref_id),
        "system": ref_dict["system"],
        "key": ref_dict["key"],
        "task": task_label,
        "fields": field_details,
    }
    title = f"Sync conflict on {ref_dict['key']} ({names})"
    item, _, created = inbox_store.raise_item(
        c,
        kind="conflict",
        subject=subject,
        priority=2,
        title=title,
        detail=detail,
        verbs=["keep_local", "take_remote"],
        task_id=task_label,
    )
    if not created and item is not None:
        numeric = inbox_store.parse_id(item["id"])
        if numeric is not None:
            inbox_store.update_item(
                c,
                numeric,
                title=title,
                detail=detail,
                priority=2,
                verbs=["keep_local", "take_remote"],
                options=[],
                task_id=task_label,
                run_id="",
                line="",
            )


def fetch(
    ref_id: int | str,
    payload: dict | None = None,
    *,
    idem: str | None = None,
    cfg=None,
) -> dict:
    with foundation.reader() as c:
        ref_dict = task_store.TaskStore.ref_get(c, ref_id)
    if ref_dict is None:
        raise LookupError(f"no ref {ref_id}")
    numeric_ref_id = int(ref_dict["num"])
    system = str(ref_dict["system"])
    scfg = sync_config(system)
    field_owners = scfg["fields"]
    state_labels = set(scfg["state_labels"])

    changelog: list[dict] | None = None
    browse_url = ""
    if payload is None:
        payload, changelog, browse_url = _fetch_remote_object(ref_dict, cfg=cfg)

    extracted: dict | None = None
    if payload is not None:
        if system == "github":
            extracted = _extract_github_payload(ref_dict, payload, state_labels)
        elif system == "jira":
            extracted = _extract_jira_payload(
                ref_dict, payload, state_labels, changelog=changelog, browse_url=browse_url,
            )

    now = util.iso()
    with foundation.tx() as c:
        cur_ref = task_store.TaskStore.ref_get(c, numeric_ref_id)
        if cur_ref is None:
            raise LookupError(f"no ref {ref_id}")

        if extracted is None:
            c.execute(
                "UPDATE refs SET fetched_at = ?, version = version + 1, updated = ? WHERE id = ?",
                (now, now, numeric_ref_id),
            )
            new_sync = recompute_ref_sync(c, numeric_ref_id)
            c.execute("UPDATE refs SET sync = ? WHERE id = ?", (new_sync, numeric_ref_id))
            return _emit_ref_changed(c, numeric_ref_id, actor=principals.current() or "hand", idem=idem)

        merged_facts = {**(cur_ref.get("facts") or {}), **extracted["facts_patch"]}
        ref_cols = {r[1] for r in c.execute("PRAGMA table_info(refs)")}
        if "remote_actor" in ref_cols:
            c.execute(
                "UPDATE refs SET title = ?, url = ?, ext_state = ?, facts = ?, "
                "remote_updated = ?, remote_actor = ?, fetched_at = ?, version = version + 1, updated = ? "
                "WHERE id = ?",
                (
                    extracted["title"],
                    extracted["url"],
                    extracted["ext_state"],
                    json.dumps(merged_facts),
                    extracted["remote_updated"],
                    extracted["remote_actor"] or None,
                    now,
                    now,
                    numeric_ref_id,
                ),
            )
        else:
            c.execute(
                "UPDATE refs SET title = ?, url = ?, ext_state = ?, facts = ?, "
                "remote_updated = ?, fetched_at = ?, version = version + 1, updated = ? WHERE id = ?",
                (
                    extracted["title"],
                    extracted["url"],
                    extracted["ext_state"],
                    json.dumps(merged_facts),
                    extracted["remote_updated"],
                    now,
                    now,
                    numeric_ref_id,
                ),
            )

        primary_row = c.execute(
            "SELECT task_id FROM task_refs WHERE ref_id = ? AND role = 'primary' LIMIT 1",
            (numeric_ref_id,),
        ).fetchone()
        primary_task_num = int(primary_row[0]) if primary_row is not None else None
        task_raw = task_store._select_task_row(c, primary_task_num) if primary_task_num is not None else None
        task_dict = dict(task_raw) if task_raw is not None else None

        remote_at = extracted["remote_updated"] or now
        remote_by = extracted["remote_actor"] or ""
        ext_actor = f"ext:{system}:{remote_by or 'unknown'}"

        task_updates: dict[str, str] = {}
        if ref_fields_available(c):
            for field_name, remote_val in extracted["fields"].items():
                owner = field_owners.get(field_name, "remote")
                existing_rf = c.execute(
                    "SELECT base, remote, conflict FROM ref_fields WHERE ref = ? AND field = ?",
                    (numeric_ref_id, field_name),
                ).fetchone()

                if task_dict is None or primary_task_num is None:
                    base_val = existing_rf["base"] if existing_rf is not None and existing_rf["base"] is not None else remote_val
                    c.execute(
                        "INSERT INTO ref_fields (ref, field, base, remote, remote_at, remote_by, owner, conflict) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 0) "
                        "ON CONFLICT(ref, field) DO UPDATE SET "
                        "base = COALESCE(ref_fields.base, excluded.base), "
                        "remote = excluded.remote, remote_at = excluded.remote_at, "
                        "remote_by = excluded.remote_by, owner = excluded.owner, conflict = 0",
                        (numeric_ref_id, field_name, base_val, remote_val, remote_at, remote_by, owner),
                    )
                    continue

                if owner == "local" or field_name not in _TASK_MAPPED_FIELDS:
                    base_val = remote_val if owner == "remote" else (
                        existing_rf["base"] if existing_rf is not None else remote_val
                    )
                    c.execute(
                        "INSERT INTO ref_fields (ref, field, base, remote, remote_at, remote_by, owner, conflict) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 0) "
                        "ON CONFLICT(ref, field) DO UPDATE SET "
                        "base = excluded.base, remote = excluded.remote, remote_at = excluded.remote_at, "
                        "remote_by = excluded.remote_by, owner = excluded.owner, conflict = 0",
                        (numeric_ref_id, field_name, base_val, remote_val, remote_at, remote_by, owner),
                    )
                    continue

                local_val = _local_field_value(c, task_dict, primary_task_num, field_name)
                base_val = existing_rf["base"] if existing_rf is not None else None
                if base_val is None:
                    # Initial baseline on primary ref: adopt remote value into task, base = remote.
                    if local_val != remote_val:
                        task_updates[field_name] = remote_val
                    c.execute(
                        "INSERT INTO ref_fields (ref, field, base, remote, remote_at, remote_by, owner, conflict) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 0) "
                        "ON CONFLICT(ref, field) DO UPDATE SET "
                        "base = excluded.base, remote = excluded.remote, remote_at = excluded.remote_at, "
                        "remote_by = excluded.remote_by, owner = excluded.owner, conflict = 0",
                        (numeric_ref_id, field_name, remote_val, remote_val, remote_at, remote_by, owner),
                    )
                else:
                    remote_changed = remote_val != base_val
                    local_changed = local_val != base_val
                    if remote_changed and not local_changed:
                        task_updates[field_name] = remote_val
                        c.execute(
                            "UPDATE ref_fields SET base = ?, remote = ?, remote_at = ?, remote_by = ?, "
                            "owner = ?, conflict = 0 WHERE ref = ? AND field = ?",
                            (remote_val, remote_val, remote_at, remote_by, owner, numeric_ref_id, field_name),
                        )
                    elif remote_changed and local_changed and remote_val != local_val:
                        c.execute(
                            "UPDATE ref_fields SET remote = ?, remote_at = ?, remote_by = ?, "
                            "owner = ?, conflict = 1 WHERE ref = ? AND field = ?",
                            (remote_val, remote_at, remote_by, owner, numeric_ref_id, field_name),
                        )
                    elif remote_changed and local_changed and remote_val == local_val:
                        c.execute(
                            "UPDATE ref_fields SET base = ?, remote = ?, remote_at = ?, remote_by = ?, "
                            "owner = ?, conflict = 0 WHERE ref = ? AND field = ?",
                            (remote_val, remote_val, remote_at, remote_by, owner, numeric_ref_id, field_name),
                        )
                    else:
                        c.execute(
                            "UPDATE ref_fields SET remote = ?, remote_at = ?, remote_by = ?, "
                            "owner = ?, conflict = 0 WHERE ref = ? AND field = ?",
                            (remote_val, remote_at, remote_by, owner, numeric_ref_id, field_name),
                        )

        if task_updates and task_dict is not None and primary_task_num is not None:
            task_dict = _write_task_remote_updates(c, task_dict, primary_task_num, task_updates, actor=ext_actor)

        if task_dict is not None and primary_task_num is not None:
            _maybe_retry_on_label_removal(
                c,
                system=system,
                ref_key=str(cur_ref["key"]),
                task_num=primary_task_num,
                task_row=task_dict,
                raw_labels=extracted["raw_labels"],
                state_labels=state_labels,
                remote_actor=remote_by,
            )
            _sync_conflict_inbox(
                c,
                ref_id=numeric_ref_id,
                ref_dict=cur_ref,
                task_num=primary_task_num,
                task_dict=task_dict,
            )

        new_sync = recompute_ref_sync(c, numeric_ref_id)
        c.execute("UPDATE refs SET sync = ? WHERE id = ?", (new_sync, numeric_ref_id))
        return _emit_ref_changed(c, numeric_ref_id, actor=ext_actor if extracted else (principals.current() or "hand"), idem=idem)


def fetch_async(
    ref_id: int | str,
    *,
    idem: str | None = None,
    actor: str | None = None,
    via: str | None = None,
) -> concurrent.futures.Future:
    chosen_actor = actor or principals.current()
    chosen_via = via if via is not None else events.provenance()
    future: concurrent.futures.Future = concurrent.futures.Future()

    def _run() -> None:
        with _SYNC_SEM:
            try:
                with principals.acting(chosen_actor), event_store.request_scope(chosen_via):
                    result = fetch(ref_id, idem=idem)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    thread = threading.Thread(target=_run, name=f"arete-sync-{ref_id}", daemon=True)
    thread.start()
    return future


def _discard_open_field_drafts(c, ref_id: int, field_name: str, now_iso: str) -> list[int]:
    if not outbox_store.available(c):
        return []
    placeholders = ",".join("?" * len(("draft", "edited", "approved", "held")))
    rows = c.execute(
        f"SELECT id, rev, slot FROM outbox WHERE ref_id IN (?, ?) AND kind = 'issue' AND state IN ({placeholders})",  # noqa: S608
        (str(ref_id), task_store.format_ref_id(ref_id), "draft", "edited", "approved", "held"),
    ).fetchall()
    discarded: list[int] = []
    for row in rows:
        ob_id = int(row["id"])
        rev = outbox_store.latest_revision(c, ob_id, int(row["rev"]))
        matched = f":field:{field_name}:" in str(row["slot"] or "")
        if not matched and rev is not None:
            with suppress(ValueError):
                parsed = json.loads(rev["body"] or "{}")
                if isinstance(parsed, dict) and parsed.get("field") == field_name:
                    matched = True
        if matched:
            outbox_store.discard_item(c, ob_id, now_iso)
            discarded.append(ob_id)
    return discarded


def resolve_conflict(c, ref_id: int, verb: str, *, actor: str = "hand") -> dict:
    if verb not in ("keep_local", "take_remote"):
        raise ValueError(f"unsupported conflict verb {verb!r}")
    ref_dict = task_store.TaskStore.ref_get(c, int(ref_id))
    if ref_dict is None:
        raise LookupError(f"no ref {ref_id}")
    primary_row = c.execute(
        "SELECT task_id FROM task_refs WHERE ref_id = ? AND role = 'primary' LIMIT 1",
        (int(ref_id),),
    ).fetchone()
    task_num = int(primary_row[0]) if primary_row is not None else None
    conflicted = [r for r in ref_fields_for(c, int(ref_id)) if r["conflict"] == 1]
    now = util.iso()
    discarded_drafts: list[int] = []

    if verb == "take_remote":
        task_raw = task_store._select_task_row(c, task_num) if task_num is not None else None
        task_dict = dict(task_raw) if task_raw is not None else None
        updates: dict[str, str] = {}
        for entry in conflicted:
            field_name = entry["field"]
            remote_val = str(entry["remote"] if entry["remote"] is not None else "")
            if field_name in _TASK_MAPPED_FIELDS:
                updates[field_name] = remote_val
            c.execute(
                "UPDATE ref_fields SET base = ?, conflict = 0 WHERE ref = ? AND field = ?",
                (remote_val, int(ref_id), field_name),
            )
            discarded_drafts.extend(_discard_open_field_drafts(c, int(ref_id), field_name, now))
        if updates and task_dict is not None and task_num is not None:
            _write_task_remote_updates(c, task_dict, task_num, updates, actor=actor or "hand")
    else:
        for entry in conflicted:
            field_name = entry["field"]
            remote_val = str(entry["remote"] if entry["remote"] is not None else "")
            c.execute(
                "UPDATE ref_fields SET base = ?, conflict = 0 WHERE ref = ? AND field = ?",
                (remote_val, int(ref_id), field_name),
            )

    new_sync = recompute_ref_sync(c, int(ref_id))
    c.execute(
        "UPDATE refs SET sync = ?, version = version + 1, updated = ? WHERE id = ?",
        (new_sync, now, int(ref_id)),
    )
    updated_ref = _emit_ref_changed(c, int(ref_id), actor=actor or "hand")
    return {
        "ref": updated_ref,
        "verb": verb,
        "resolved_fields": [e["field"] for e in conflicted],
        "discarded_drafts": [outbox_store.format_id(ob) for ob in discarded_drafts],
    }
