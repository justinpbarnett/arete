from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import zlib
from collections import Counter
import concurrent.futures
from urllib.parse import quote, unquote

from . import (
    api_http, board, codebase, config, configcheck, configitems, configkinds, configrepo, contract, control,
    credential_http, devices, drafts, events, inbox, leases, outbox, paths, pi, record, refusals, secretstore, settings,
    tasks, ticket_media, tool_events, triage, util, webhooks, webpush,
)
from .store import blobs, chats, credentials, insights, principals, runs
from .store import db as foundation
from .store import devices as device_store
from .store import tasks as task_store
from .store import tokens, writes

_PREFER_WAIT = re.compile(r"(?:^|[,\s;])wait=(\d+(?:\.\d+)?)\b", re.IGNORECASE)


def prefer_wait(handler, *, default: float = 2.0, maximum: float = 10.0) -> float:
    if handler is None:
        return default
    for line in handler.headers.get_all("Prefer", []):
        if m := _PREFER_WAIT.search(line):
            try:
                value = float(m.group(1))
            except ValueError:
                continue
            return min(maximum, max(0.0, value))
    return default

VIEWS = {"live": "Live", "runs": "Runs", "sessions": "Sessions", "loops": "Loops", "triage": "Triage",
         "outcomes": "Outcomes", "health": "Health", "incidents": "Incidents", "settings": "Settings", "usage": "Usage",
         "board": "Board"}
BOARD_STATES = ("needs-info", "failed", "running", "pr", "todo", "done", "closed")


def run_object(row):
    return {k: row[k] for k in (*contract.RUN_TEXT, *contract.RUN_NUMBERS) if k in row} if row else None


def serialize(value, schema):
    if isinstance(value, dict) and "properties" in schema:
        additional = schema.get("additionalProperties")
        return {k: serialize(v, schema["properties"].get(k, additional)) for k, v in value.items()
                if k in schema["properties"] or isinstance(additional, dict)}
    if isinstance(value, list) and "items" in schema:
        return [serialize(v, schema["items"]) for v in value]
    return value


def needed(row, identity):
    if row is None:
        raise contract.Problem("not_found", f"No resource {identity}")
    return row


def _vapid_public_key() -> str:
    try:
        return webpush.public_key()
    except webpush.KeyFault:
        return ""


def meta():
    with foundation.reader() as conn:
        data = foundation.metadata(conn)
        stream = events.metadata(conn)
    deprecated = sorted(f"{entry['method']} {entry['path']}" for entry in contract.ALIASES if entry.get("deprecated"))
    return {"api": {"version": contract.VERSION, "minor": contract.MINOR, "contract_sha": contract.digest()},
            "server": {"engine_sha": codebase.STAMP}, "oldest_client": contract.OLDEST_CLIENT,
            "instance": {k: data[f"instance_{k}"] for k in ("id", "tag", "name", "created")},
            "schema": {"version": int(data["schema_version"]),
                       "oldest_writer": int(data["oldest_writer"]) if data["oldest_writer"] is not None else None},
            "stream": stream,
            "features": {"inbox": True, "outbox": True, "run_start_tx": True},
            "deprecated": deprecated,
            "push": {"vapid_public_key": _vapid_public_key()}}


def projection(name, identity=""):
    if name == "board":
        rows = task_rows()
        counts = Counter(row["state"] for row in rows)
        states = dict.fromkeys((*BOARD_STATES, *counts))
        running_ids = [row["id"] for row in runs.runs_marked_running(connect=foundation.reader)]
        return {"tasks": rows, "columns": [{"state": state, "count": counts[state]} for state in states if counts[state]],
                "running_ids": running_ids}
    return provider(None, "get_" + ("run" if name == "run" else name), identity, {})


def provider(handler, name, rest, query):
    from . import dashboard

    return getattr(dashboard, name)(handler, rest, query)


def resources(name, values):
    cfg = config.load_or_problem()[0]
    if name == "run":
        return [run_object(r) for r in runs.runs(limit=500)]
    if name == "project":
        return [{"id": r.name, "name": r.name, "github": r.github, "path": str(r.path)} for r in cfg.repos]
    if name == "line":
        return ([{"id": r.name, "kind": "pipeline", "repo": r.name, "enabled": True} for r in cfg.repos]
                + [{"id": "loop:" + r["name"], "kind": "loop", "repo": "", "enabled": r["enabled"]}
                   for r in control.loop_views()])
    if name == "view":
        return [{"id": k, "label": v} for k, v in VIEWS.items()]
    if name == "step":
        needed(runs.run_get(values["run"]), values["run"])
        inputs_by_step = runs.step_inputs_by_run(values["run"])
        snapshots: dict[str, dict | None] = {}
        out = []
        for r in runs.steps(values["run"]):
            snap_sha = r.get("settings_sha") or None
            if snap_sha and snap_sha not in snapshots:
                snapshots[snap_sha] = runs.settings_snapshot(snap_sha)
            step_inputs = inputs_by_step.get(r["id"])
            out.append({
                "id": r["id"], "run_id": values["run"], "name": r["step"], "model": r["model"] or "",
                "runner": r["runner"] or "", "cost_usd": r["cost_usd"] or 0, "seconds": r["seconds"] or 0,
                "settings_sha": snap_sha,
                "settings": snapshots.get(snap_sha) if snap_sha else None,
                "inputs": step_inputs if step_inputs is not None else [],
                "prompt_version": r.get("prompt_version") or None,
                "prompt_level": r.get("prompt_level") or None,
                "schema_version": r.get("schema_version") or None,
                "node": r.get("node") or r["step"] or "",
                "try": int(r.get("try") or 0),
                "status": r.get("status") or ("done" if r.get("ok") else "fault"),
                "queued_at": r.get("queued_at") or r.get("ts"),
                "waiting_since": r.get("waiting_since"),
                "device": r.get("device"),
                "account": r.get("account_id"),
                "chat_id": r.get("chat_id"),
                "version": int(r.get("version") or 1),
                "runner_version": r.get("runner_version"),
                "unread": int(r.get("unread") or 0),
            })
        return out
    if name == "loop":
        return [{"id": r["name"], "enabled": r["enabled"], "detail": r} for r in control.loop_views()]
    if name == "incident":
        rows = board.incidents_board()["incidents"]
        return [{"id": str(r["id"]), "detail": r} for r in rows]
    if name == "triage":
        return [{"id": r["ref"], "detail": r} for r in triage.view(connect=foundation.reader)["rows"]]
    if name == "research":
        return [{"id": r["ref"], "detail": r} for r in insights.research_rows()]
    if name == "chat":
        return [{"id": str(r["id"]), "detail": r} for r in chats.chats()]
    raise contract.Problem("invalid_input", f"Unknown resource {name}")


def task_rows():
    return [serialize(row, contract.TASK) for row in task_store.tasks()]


def secret_view(row):
    return {"subject": secretstore.address(row["scope"], row["name"]), "name": row["name"],
            "scope": row["scope"], "service": row["provider"], "fingerprint": row["fingerprint"],
            "present": row["status"] == "set", "reference_kind": row["store"], "expose": bool(row["expose"]),
            "uses": [], "last_checked": row["checked_at"], "rotated": row["rotated_at"],
            "check_ok": None if row["check_ok"] is None else bool(row["check_ok"])}


def secret_result(scope, name, row):
    return {"secret": secret_view(needed(row, secretstore.address(scope, name)))}


def secret_read(operation, values):
    if operation == "secret.list":
        return {"secrets": [secret_view(row) for row in secretstore.items()]}
    scope, name = values["scope"], values["name"]
    result = secret_result(scope, name, secretstore.get(scope, name))
    if operation == "secret.history":
        result["history"] = [serialize({**event, "ok": bool(event["ok"])}, contract.SECRET_HISTORY)
                             for event in secretstore.history(scope, name)]
    return result


def secret_set_options(scope, name):
    row = secretstore.get(scope, name)
    secretstore.require_removable(f"{scope}/{name}", row["store"] if row else None)
    kind, provider = "api_key", None
    if scope == "global":
        if name == "GITHUB_APP_PRIVATE_KEY":
            kind, provider = "app_private_key", "github"
        elif name == "JIRA_API_TOKEN":
            provider = "jira"
        else:
            provider = next((p for p, key in pi.KEY_NAMES.items() if key == name), None)
    elif scope.startswith(("line:", "connector:loop.")) and name == "WEBHOOK_SECRET":
        kind, provider = "webhook_secret", "webhook"
    elif scope.startswith("repo:"):
        kind = "repo_answer"
    return row["kind"] if row else kind, provider


def secret_input(validate, *args):
    try:
        validate(*args)
    except ValueError as error:
        raise contract.Problem("invalid_input", str(error)) from None


def set_secret(handler, op, target):
    scope, name = target["scope"], target["name"]
    try:
        value = api_http.read_body(handler.rfile, handler.content_size, api_http.MAX_JSON_BYTES).decode("utf-8")
    except UnicodeDecodeError:
        raise contract.Problem("invalid_input", "The value must be valid UTF-8.") from None
    kind, _ = secret_set_options(scope, name)
    secret_input(secretstore.validate_set, scope, name, value, kind)
    fp = secretstore.fingerprint(value)
    if getattr(handler, "awaiting_presence", False):
        return inbox.defer_presence(handler, op, target, target, hash_body={"fingerprint": fp},
                                    secret_value=value, fingerprint=fp)

    def change():
        kind, provider = secret_set_options(scope, name)
        row = secretstore.put(scope, name, value, kind, principals.current(), provider=provider)
        return with_seq(secret_result(scope, name, row))

    return handler.keyed_action(op["method"], op["path"], target, change, atomic=True, status=op["success_status"],
                                hash_body={"fingerprint": fp})


def upload_inbox_evidence(handler, op, target):
    item_id = target["item"]
    raw_name = (handler.headers.get("X-Evidence-Filename") or "").strip()
    content_type = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    data = api_http.read_body(handler.rfile, handler.content_size, ticket_media.MAX_VIDEO_BYTES)
    digest = hashlib.sha256(data).hexdigest()

    def change():
        return with_seq(inbox.upload_evidence(
            item_id,
            data,
            filename=raw_name,
            content_type=content_type,
            actor=principals.current(),
        ))

    return handler.keyed_action(
        op["method"],
        op["path"],
        target,
        change,
        atomic=False,
        status=op["success_status"],
        hash_body={"sha256": digest, "filename": raw_name, "size": len(data)},
    )


def put_blob(handler, op, target):
    expected_sha = target["sha256"]
    data = api_http.read_body(handler.rfile, handler.content_size, blobs.MAX_BLOB_BYTES)
    actual_sha = blobs.digest(data)
    if actual_sha != expected_sha:
        raise contract.Problem("validation", f"Blob sha256 mismatch: expected {expected_sha}, got {actual_sha}")

    def change():
        row = blobs.put_verified(expected_sha, data)
        return with_seq({"blob": {k: row[k] for k in ("sha256", "kind", "bytes")}})

    return handler.keyed_action(op["method"], op["path"], target, change, atomic=True, status=op["success_status"],
                                hash_body={"sha256": expected_sha})


def prepare(name, values):
    op = needed(contract.BY_NAME.get(name), name)
    if name.startswith("config.") and "kind" in values and isinstance(values["kind"], str) and values["kind"] not in configkinds.REGISTRY:
        raise contract.Problem("not_found", f"Unknown config kind {values['kind']}")
    if name == "settings.update":
        try:
            contract.validate(values, op["input"])
        except contract.Problem as error:
            if error.code == "invalid_input":
                raise contract.Problem("invalid_fields", str(error)) from None
            raise
    else:
        contract.validate(values, op["input"])
    if name.startswith("secret."):
        credential_http.secret_access(values.get("scope", ""))
        if "name" in values:
            secret_input(secretstore.validate, values["scope"], values["name"])
    if name == "webhook.rotate":
        credential_http.secret_access("")
    if name == "hook.ingest":
        for i, event in enumerate(values["events"]):
            event_timestamp(event["ts"], f"input.events[{i}].ts")
    if name == "batch.run":
        requests = values["requests"]
        if not 1 <= len(requests) <= 50:
            raise contract.Problem("invalid_input", "A batch takes 1 to 50 reads")
        for request in requests:
            selected = needed(contract.BY_NAME.get(request["operation"]), request["operation"])
            if selected["method"] != "GET":
                raise contract.Problem("invalid_input", "A read batch takes only GET operations")
            prepare(selected["name"], request.get("input", {}))
    return op


def event_timestamp(value, field):
    try:
        return util.parse_iso(value).timestamp()
    except (ValueError, OverflowError, OSError):
        raise contract.Problem("invalid_input", f"{field} must be a timestamp") from None


def execute(name, values, handler=None):
    prepare(name, values)
    if name == "batch.run" and handler is not None:
        for request in values["requests"]:
            credential_http.require_scope(handler.credential, contract.BY_NAME[request["operation"]]["scope"])
    if name == "usage.record":
        from . import dashboard

        return dashboard.record_usage(handler, values)
    reads = values["requests"] if name == "batch.run" else ()
    requested = {name, *(r["operation"] for r in reads)}
    if any(credential_http.registry_operation(op) for op in requested) or (
            "loop.get" in requested and config.secret_settings().store):
        secretstore.migrate_registry()
    with foundation.one_read() as conn:
        seq = events.metadata(conn)["head"]
        if name == "batch.run":
            results = [{"seq": seq, **read(r["operation"], r.get("input", {}), handler)} for r in reads]
            return {"seq": seq, "results": results}
        return {"seq": seq, **read(name, values, handler)}


def read(name, values, handler):
    if name == "factory.get":
        from .store import factory
        return {"factory": factory.view()}
    if name == "inbox.list":
        prio = values.get("priority")
        if prio is not None:
            if not (prio.isascii() and prio.isdecimal() and int(prio) in (1, 2, 3)):
                raise contract.Problem("invalid_input", "priority must be 1, 2, or 3")
            prio = int(prio)
        return {"items": inbox.list(state=values.get("state", "open"), kind=values.get("kind"),
                                    priority=prio, task=values.get("task"), run=values.get("run"),
                                    line=values.get("line"))}
    if name == "inbox.get":
        return inbox.get(values["item"])
    if name == "inbox.history":
        return {"events": inbox.history(values["item"])}
    if name == "triage_screen.get":
        return inbox.triage_screen()
    if name == "push.list":
        return {"subscriptions": webpush.list_v1()}
    if name == "device.list":
        return {"devices": devices.device_views()}
    if name == "device.get":
        return {"device": needed(devices.device_get_view(values["device"]), values["device"])}
    if name == "lease.list":
        only_open: bool | None = None
        if "open" in values:
            raw_open = values["open"].strip().lower()
            if raw_open in ("1", "true", "yes"):
                only_open = True
            elif raw_open in ("0", "false", "no"):
                only_open = False
            else:
                raise contract.Problem("invalid_input", "open must be true or false")
        rows = device_store.leases_list(
            run_id=values.get("run"),
            device=values.get("device"),
            only_open=only_open,
        )
        return {"leases": [leases.view(row) for row in rows]}
    if name == "lease.get":
        lease_num = leases.parse_id(values["lease"])
        row = device_store.lease_get(lease_num) if lease_num is not None else None
        return {"lease": leases.view(needed(row, values["lease"]))}
    if name == "run.sandbox":
        run_id = values["run"]
        run_row = needed(runs.run_get(run_id), run_id)
        lease_row = None
        if run_row.get("lease_id"):
            lease_row = device_store.lease_get(int(run_row["lease_id"]))
        if lease_row is None:
            lease_row = device_store.latest_lease_for_run(run_id)
        device_name = lease_row["device"] if lease_row else (run_row.get("device") or "local")
        probe_row = device_store.latest_device_probe(device_name)
        facts = probe_row["facts"] if probe_row else None
        return {
            "device": device_name,
            "lease": f"L-{lease_row['id']}" if lease_row else None,
            "address": lease_row["address"] if lease_row else "",
            "facts": facts,
        }
    if name == "outbox.list":
        return {"items": outbox.list_items(state=values.get("state"), task=values.get("task"), run=values.get("run"))}
    if name == "outbox.get":
        return outbox.get(values["item"])
    if name == "outbox.preview":
        return outbox.preview(values["item"])
    if name == "outbox.history":
        return {"events": outbox.history(values["item"])}
    if name == "cohort.get":
        from . import cohorts

        if not cohorts.location().is_file():
            raise contract.Problem("not_found", "No cohort has been declared")
        try:
            return {"report": cohorts.report(values["name"])}
        except cohorts.UnknownCohort as error:
            raise contract.Problem("not_found", str(error)) from error
    if name.startswith("secret."):
        return secret_read(name, values)
    if name == "account.list":
        with foundation.reader() as c:
            return {"accounts": credentials.account_rows(c)}
    if name == "token.list":
        return {"tokens": tokens.listing(handler.person)}
    if name == "token.get":
        return {"token": needed(tokens.get(values["token"], handler.person), values["token"])}
    if name == "meta.get":
        return meta()
    if name == "contract.get":
        return {**contract.publish(), "contract_sha": contract.digest()}
    if name == "error.list":
        return {"errors": contract.ERRORS}
    if name == "screen.get":
        view = values.get("view", "live")
        if view not in VIEWS and view not in ("run", "task", "chat", "loop", "incident"):
            raise contract.Problem("not_found" if view == "board" else "invalid_input", f"Unknown view {view}")
        if view == "task":
            needed(task_store.task_get(values.get("id", "")), values.get("id", ""))
        return {"sidebar": projection("sidebar"), "asks": tasks.asks_view(),
                "view": projection(view, values.get("id", ""))}
    if name == "settings.get":
        return {"settings": settings.current()}
    if name == "settings.resolved":
        try:
            return settings.resolved(
                repo=values.get("repo", ""),
                task_kind=values.get("task_kind", ""),
                node=values.get("node", ""),
            )
        except config.UnknownRepo as error:
            raise contract.Problem("not_found", str(error)) from None
        except ValueError as error:
            raise contract.Problem("invalid_input", str(error)) from None
    if name == "blob.get":
        meta_row = blobs.meta(values["sha256"])
        if meta_row is None:
            raise contract.Problem("not_found", f"No blob {values['sha256']}")
        if purged := meta_row.get("purged_at"):
            raise contract.Problem(
                "files_purged",
                f"blob {values['sha256']} was removed on {purged}",
                purged_at=purged,
            )
        raw = blobs.read(values["sha256"])
        if raw is None:
            raise contract.Problem("not_found", f"No blob {values['sha256']}")
        try:
            encoded, encoding = raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            encoded, encoding = base64.b64encode(raw).decode("ascii"), "base64"
        return {"blob": {
            "sha256": meta_row["sha256"],
            "kind": meta_row["kind"],
            "bytes": meta_row["bytes"],
            "data": encoded,
            "encoding": encoding,
        }}
    if name.startswith("config."):
        return read_config(name.removeprefix("config."), values)
    if name == "draft.list":
        rows = drafts.list_drafts(
            state=values.get("state"),
            kind=values.get("kind"),
            name=values.get("name"),
            author=values.get("author"),
        )
        return {"drafts": [drafts.public(r) for r in rows]}
    if name == "draft.get":
        return {"draft": drafts.public(needed(drafts.get(values["draft"]), values["draft"]))}
    if name == "health.get":
        return {"health": board.health_board(include_held=False)}
    if name == "hook_event.list":
        since = values.get("since")
        cutoff = event_timestamp(since, "since") if since is not None else None
        return {"events": tool_events.read_events(since_ts=cutoff)}
    if name == "status.get":
        limit = values.get("limit", str(board.RECENT_RUNS))
        if not limit.isascii() or not limit.isdecimal() or len(limit) > 6:
            raise contract.Problem("invalid_input", "limit must be a nonnegative integer")
        return {"snapshot": board.snapshot(int(limit)), "loops": control.loop_views(), "records": board.panel(record.store)}
    if name == "line_stream.read":
        after = values.get("after", "0")
        if not after.isascii() or not after.isdecimal() or len(after) > 18:
            raise contract.Problem("invalid_input", "after must be a nonnegative decimal cursor")
        found = control.feed(values["run"], int(after))
        if purged := found["run"].get("files_purged"):
            raise contract.Problem(
                "files_purged",
                f"run {values['run']} files were removed on {purged}",
                purged_at=purged,
            )
        return {"lines": found["lines"], "after": max([int(after), *[r["id"] for r in found["lines"]]])}
    identity = values.get("id", "")
    if name == "run.diff":
        found = needed(record.read_run(identity), identity)
        diff = next((r["patch"] for r in reversed(found["marks"])
                     if r.get("kind") == "diff" and isinstance(r.get("patch"), str)), None)
        if diff is None:
            raise contract.Problem("unavailable", "This run has no retained diff")
        return {"diff": diff}
    if name == "task.history":
        task = needed(task_store.task_get(identity), identity)
        return {"comments": task_store.versioned_comments(identity),
                "runs": [run_object(r) for r in runs.ticket_runs(task["repo"], identity, limit=500)]}
    if name == "turn.list":
        raw_try = values["try"]
        if not raw_try.isascii() or not raw_try.isdecimal() or int(raw_try) < 1:
            raise contract.Problem("invalid_input", "try must be a positive integer")
        run_row = runs.run_get(values["run"])
        if run_row is not None and (purged := run_row.get("records_purged") or run_row.get("files_purged")):
            raise contract.Problem(
                "files_purged",
                f"run {values['run']} records were removed on {purged}",
                purged_at=purged,
            )
        matched_steps, turn_rows = runs.run_step_turns(values["run"], values["node"], int(raw_try))
        if not matched_steps:
            raise contract.Problem("not_found", f"No step {values['node']} try {raw_try} on run {values['run']}")
        return {"turns": [serialize(row, contract.TURN) for row in turn_rows]}
    if name == "turn.get":
        raw_try, raw_n = values["try"], values["n"]
        if not raw_try.isascii() or not raw_try.isdecimal() or int(raw_try) < 1:
            raise contract.Problem("invalid_input", "try must be a positive integer")
        if not raw_n.isascii() or not raw_n.isdecimal() or int(raw_n) < 1:
            raise contract.Problem("invalid_input", "n must be a positive integer")
        run_row = runs.run_get(values["run"])
        if run_row is not None and (purged := run_row.get("records_purged") or run_row.get("files_purged")):
            raise contract.Problem(
                "files_purged",
                f"run {values['run']} records were removed on {purged}",
                purged_at=purged,
            )
        matched_steps, turn_rows = runs.run_step_turns(values["run"], values["node"], int(raw_try))
        if not matched_steps:
            raise contract.Problem("not_found", f"No step {values['node']} try {raw_try} on run {values['run']}")
        target_n = int(raw_n)
        turn_row = next((row for row in turn_rows if int(row["n"]) == target_n), None)
        if turn_row is None:
            raise contract.Problem("not_found", f"No turn {target_n} on step {values['node']} try {raw_try}")
        try:
            decoded = record.decode_turn(turn_row)
        except (OSError, ValueError, zlib.error) as error:
            raise contract.Problem("unavailable", f"Turn {target_n} cannot be decoded: {error}") from None
        return {"turn": serialize(decoded, contract.TURN_DETAIL)}
    if name == "task.list":
        return {"tasks": task_rows()}
    resource, action = name.split(".")
    if action == "get" and resource == "run":
        proj = projection("run", identity)
        run_view = run_object(proj["run"])
        steps = [serialize(s, contract.RUN_STEP_SUMMARY) for s in proj.get("steps", ())]
        return {"run": run_view, "steps": steps}
    if action == "get" and resource == "task":
        needed(task_store.task_get(identity), identity)
        return {"task": serialize(projection("task", identity), contract.TASK)}
    rows = [serialize(row, contract.RESOURCE[resource]) for row in resources(resource, values)]
    if action == "list":
        return {contract.PLURALS[resource]: rows}
    row = needed(next((r for r in rows if str(r["id"]) == identity), None), identity)
    if resource in ("loop", "incident", "chat"):
        detail = projection(resource, identity)
        row = {**row, "detail": detail["incident"] if resource == "incident" else detail}
    if resource == "view":
        row = {**row, "data": projection(identity)}
    return {resource: serialize(row, contract.RESOURCE[resource])}


def read_config(verb, values):
    if verb == "kinds":
        return {"kinds": configitems.kinds()}
    kind = values["kind"]
    if verb == "list":
        return {"items": configitems.listing(kind)}
    found = configitems.item(kind, values["name"], level=values.get("level"))
    if verb == "read":
        return {"item": configitems.read(found, version=values.get("version"), level=values.get("level"),
                                         from_ref=values.get("from"), file=values.get("file"))}
    if verb == "versions":
        shas, entries = configitems.versions(found)
        return {"versions": shas, "entries": entries}
    diff = configitems.diff(found, values.get("from"), values.get("to"))
    if diff is None:
        raise contract.Problem("unavailable", "No config Git diff is available")
    return {"diff": diff}


def _resolve_activate_draft_id(values: dict) -> str:
    if values.get("draft"):
        return str(values["draft"])
    found = drafts.list_drafts(state="valid", kind=values["kind"], name=values["name"], limit=1)
    if not found:
        raise contract.Problem("not_found", f"No valid draft for {values['kind']}/{values['name']}")
    return found[0]["id"]


def _needs_config_presence(name: str, values: dict, handler=None) -> dict | None:
    """Return extra_detail dict if this config/settings write requires Gate C presence, else None."""
    actor = _config_actor(handler)
    root = paths.CONFIG_DIR.resolve()
    if name == "config.activate":
        draft_id = _resolve_activate_draft_id(values)
        draft_row = drafts.get(draft_id)
        if draft_row is None:
            raise contract.Problem("not_found", f"No config draft {draft_id}")
        report, _ = drafts._validate_base_and_check_for_activation(
            draft_row, actor, values["base_version"], root=root
        )
        if report["gate"] == "C":
            return {"draft_id": draft_row["id"], "gate": "C", "check_report": report}
        return None
    if name == "config.rollback":
        found = configitems.item(values["kind"], values["name"])
        target = configitems.read(found, version=str(values["to"]))
        content = target["content"]
        if not isinstance(content, str):
            return None
        old_bytes = configrepo.live_bytes(found["path"], root) if configrepo.is_repo(root) else None
        report = configcheck.check(
            values["kind"], values["name"], found["path"], content,
            author=actor, config_dir=root, old_bytes=old_bytes,
        )
        if report["ok"] and report["gate"] == "C":
            return {"kind": values["kind"], "name": values["name"], "to": values["to"],
                    "gate": "C", "check_report": report}
        return None
    if name == "settings.update":
        import tomllib

        vals = values["values"]
        if not isinstance(vals, dict):
            return None
        if "models" in vals and "roles" in vals:
            return None
        try:
            wanted_limits = {key: settings.run_limit_from(key, vals[key]) for key in settings.RUN_LIMITS if key in vals}
            wanted_models: dict[str, str] = {}
            if "models" in vals:
                models = vals.get("models") or {}
                if not isinstance(models, dict):
                    return None
                wanted_models = {role: settings.model_from(role, m) for role, m in models.items()}
            role_patches = settings._validate_role_patches(vals["roles"]) if "roles" in vals else []
        except settings.Refused:
            return None
        text, _ = configrepo.read("config.toml")
        lines = text.splitlines(keepends=True)
        for key, limit in wanted_limits.items():
            settings.set_key(lines, None, key, str(limit))
        for role, model in wanted_models.items():
            if model:
                settings.set_key(lines, "models", role, json.dumps(model, ensure_ascii=False))
            else:
                settings.drop_key(lines, "models", role)
        try:
            if role_patches:
                settings._apply_role_patches(lines, role_patches)
            updated = "".join(lines)
            tomllib.loads(updated)
        except Exception:
            return None
        old_bytes = configrepo.live_bytes("config.toml", root) if configrepo.is_repo(root) else text.encode("utf-8")
        report = configcheck.check(
            "step_settings", "defaults", "config.toml", updated,
            author=actor, config_dir=root, old_bytes=old_bytes,
        )
        if report["ok"] and report["gate"] == "C":
            # Verify version conflict and strict settings check before deferring presence.
            live_ver = settings.current_version()
            if values["base_version"] != live_ver:
                raise writes.VersionConflict(settings.current())
            try:
                settings.check_draft(updated, strict_steps=bool(role_patches))
            except settings.Refused as error:
                raise contract.Problem("invalid_fields", str(error)) from error
            return {"kind": "step_settings", "name": "defaults", "gate": "C", "check_report": report}
        return None
    return None


def dispatch(handler, op, path_values, query):
    name, method = op["name"], op["method"]
    identity = {k: unquote(v) for k, v in path_values.items()}
    values = {**query, **identity}
    write = contract.keyed_write(op)
    if method != "GET":
        body = {} if name in ("secret.set", "inbox.upload_evidence", "blob.put") else handler.body()
        if write and (query or identity.keys() & body.keys()):
            raise contract.Problem("invalid_input", "Send the target in the URL and writable fields in the body")
        values = {**body, **identity}
    try:
        if write:
            prepare(name, values)
            if credential_http.registry_operation(name):
                secretstore.migrate_registry()
            if name == "secret.set":
                return set_secret(handler, op, identity)
            if (name in ("outbox.approve", "outbox.retry", "outbox.reject")
                    and (handler.credential is None or handler.credential.kind != "session")
                    and outbox.needs_presence(name, identity["item"])):
                if handler.credential is None or "Idempotency-Key" not in handler.headers:
                    credential_http.require_presence(handler.credential)
                return inbox.defer_presence(
                    handler,
                    op,
                    values,
                    path_values,
                    before_defer=lambda: outbox.validate_for_presence(
                        name,
                        identity["item"],
                        base_version=values.get("base_version"),
                        rev=values.get("rev"),
                    ),
                )
            if name == "inbox.upload_evidence":
                return upload_inbox_evidence(handler, op, identity)
            if name == "blob.put":
                return put_blob(handler, op, identity)
            if (name in ("config.activate", "config.rollback", "settings.update")
                    and (handler.credential is None or handler.credential.kind != "session")):
                extra_detail = _needs_config_presence(name, values, handler)
                if extra_detail is not None:
                    if handler.credential is None or "Idempotency-Key" not in handler.headers:
                        credential_http.require_presence(handler.credential)
                    return inbox.defer_presence(
                        handler,
                        op,
                        values,
                        path_values,
                        extra_detail=extra_detail,
                    )
            if getattr(handler, "awaiting_presence", False):
                if name == "secret.remove":
                    secret_set_options(identity["scope"], identity["name"])
                elif name == "token.issue":
                    credential_http.issue_grants(values)
                elif name == "push.subscribe":
                    try:
                        webpush.checked_endpoint(values.get("endpoint"))
                        keys = values.get("keys") or {}
                        for v in (keys.get("p256dh"), keys.get("auth")):
                            if not isinstance(v, str) or not webpush.KEY_FIELD.fullmatch(v):
                                raise webpush.Refused("The subscription's keys are missing or not valid.")
                    except webpush.Refused as error:
                        raise contract.Problem("validation", str(error)) from None
                return inbox.defer_presence(handler, op, values, identity if name.startswith("secret.") else path_values)
            result = command(handler, op, values, identity if name.startswith("secret.") else path_values)
            if name == "pairing.claim":
                credential_http.finish_pairing_claim(handler)
            elif name == "session.end":
                handler.set_cookie = credential_http.session_cookie("", 0)
            return result
        return execute(name, values, handler)
    except secretstore.InternalSecret as error:
        raise contract.Problem("secret_internal", str(error)) from None
    except secretstore.StoreMissing:
        raise credential_http.problem("secret_store_missing") from None
    except refusals.Refusal as error:
        if getattr(error, "code", None) == "factory_off":
            raise contract.Problem("conflict", str(error)) from error
        if not getattr(error, "code", None) and error.status in (400, 404):
            raise contract.Problem("not_found" if error.status == 404 else "invalid_input", str(error)) from error
        raise


_NON_ATOMIC_COMMANDS = frozenset((
    "run.stop", "serve.install", "inbox.resolve", "inbox.retry", "settings.update", "loop.enable", "loop.disable",
    "device.probe", "outbox.approve", "outbox.retry", "outbox.reject", "chat.file", "triage.send",
    "task.create", "task.run", "session.start", "run.resume", "run.repair", "run.step",
    "loop.start", "loop.try", "loop.draft", "chat.create", "chat.send", "chat.session_start",
    "triage.refresh", "triage.research", "health.check", "incident.scan",
    "config.draft", "config.check", "config.activate", "config.rollback", "config.try", "draft.discard",
))


def command(handler, op, values, path_values, *, claim=None):
    name = op["name"]
    read_only = op["method"] == "GET" or name == "batch.run"
    action = (lambda: mutation(name, values, handler)) if contract.keyed_write(op) else (
        lambda: execute(name, values, handler))
    return handler.keyed_action(op["method"], op["path"], path_values, action,
                                atomic=not read_only and name not in _NON_ATOMIC_COMMANDS,
                                status=op["success_status"],
                                redact=name in ("token.issue", "token.rotate", "webhook.rotate"),
                                claim=claim, read_only=read_only)


def _set_run_location(handler, run_id: str, *, created: bool) -> None:
    if handler is None:
        return
    if created and run_id:
        handler.success_status = 201
        handler.location = f"/api/v1/runs/{quote(run_id, safe='')}"
    else:
        handler.success_status = 200
        handler.location = None


def _resolve_events_input(raw_events):
    if raw_events is None:
        return None
    if isinstance(raw_events, list):
        return raw_events
    if isinstance(raw_events, str):
        meta_row = blobs.meta(raw_events)
        if meta_row is None:
            raise contract.Problem("not_found", f"No blob {raw_events}")
        if purged := meta_row.get("purged_at"):
            raise contract.Problem("files_purged", f"blob {raw_events} was removed on {purged}", purged_at=purged)
        raw = blobs.read(raw_events)
        if raw is None:
            raise contract.Problem("not_found", f"No blob {raw_events}")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise contract.Problem("invalid_input", f"Events blob {raw_events} is not valid JSON: {error}") from None
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
            return parsed
        raise contract.Problem("invalid_input", f"Events blob {raw_events} must be a JSON object or list of objects")
    raise contract.Problem("invalid_input", "events must be a list of objects or a blob sha256")


def _task_placeholder(repo: str, tid: str, kind: str = "feat") -> dict:
    now = util.iso()
    return {
        "id": str(tid), "repo": repo, "title": "", "body": "", "kind": kind or "feat",
        "state": "todo", "parent": "", "created": now, "updated": now, "version": 1,
    }


def remove_secret(scope, name):
    prepare("secret.remove", {"scope": scope, "name": name})
    return secret_result(scope, name, secretstore.remove(scope, name, principals.current()))


def _config_actor(handler) -> str:
    cred = getattr(handler, "credential", None) if handler is not None else None
    if cred is not None:
        if cred.actor.startswith("run:"):
            return cred.actor
        if cred.principal.startswith(("u:", "run:")):
            return cred.principal
        return cred.actor
    return principals.current()


def mutation(name, values, handler):
    from . import dashboard

    if name == "factory.set":
        from . import factory_control
        result = {"factory": factory_control.request(values["enabled"], values["base_version"])}
    elif name == "settings.update":
        key = handler.headers.get("Idempotency-Key", "") if handler else ""
        presence_ok = bool(handler and handler.credential is not None and handler.credential.kind == "session")
        try:
            saved = settings.save(values["values"], base_version=values["base_version"],
                                  actor=principals.current(), source="dashboard", idem=key,
                                  presence_passed=presence_ok)
        except drafts.PresenceRequired:
            raise contract.Problem("forbidden", "The request was not authorized.", reason="presence_required") from None
        except (settings.Refused, ValueError, configrepo.ConfigRepoError) as error:
            raise contract.Problem("invalid_fields", str(error)) from error
        result = {"settings": saved}
    elif name == "config.draft":
        draft_row, report = drafts.create_and_check(
            values["kind"],
            values["name"],
            values["body"],
            author=_config_actor(handler),
            path=values.get("path", ""),
            base_version=values.get("base_version"),
            why=values.get("why", ""),
            instruction=values.get("instruction", ""),
            model=values.get("model", ""),
            run_check=values.get("check", True),
        )
        result = {"draft": drafts.public(draft_row), "report": report}
    elif name == "config.check":
        if values.get("draft"):
            draft_row, report = drafts.recheck(values["draft"])
            result = {"report": report, "draft": drafts.public(draft_row)}
        elif "body" in values:
            rel = drafts.default_path(values["kind"], values["name"])
            report = configcheck.check(
                values["kind"], values["name"], rel, values["body"], author=_config_actor(handler)
            )
            result = {"report": report, "draft": None}
        else:
            raise contract.Problem("validation", "Provide draft or body to config.check.")
    elif name == "config.activate":
        key = handler.headers.get("Idempotency-Key", "") if handler else ""
        presence_ok = bool(handler and handler.credential is not None and handler.credential.kind == "session")
        draft_id = _resolve_activate_draft_id(values)
        try:
            result = drafts.activate(
                draft_id,
                _config_actor(handler),
                values["base_version"],
                idem=key,
                presence_passed=presence_ok,
            )
        except drafts.PresenceRequired:
            raise contract.Problem("forbidden", "The request was not authorized.", reason="presence_required") from None
    elif name == "config.rollback":
        key = handler.headers.get("Idempotency-Key", "") if handler else ""
        presence_ok = bool(handler and handler.credential is not None and handler.credential.kind == "session")
        try:
            result = drafts.rollback(
                values["kind"],
                values["name"],
                values["to"],
                values["base_version"],
                _config_actor(handler),
                idem=key,
                presence_passed=presence_ok,
            )
        except drafts.PresenceRequired:
            raise contract.Problem("forbidden", "The request was not authorized.", reason="presence_required") from None
    elif name == "config.try":
        result = drafts.try_draft(
            values["kind"],
            values["name"],
            values["draft"],
            values["steps"],
            _config_actor(handler),
        )
    elif name == "draft.discard":
        result = {"draft": drafts.public(drafts.discard(values["draft"], reason=values.get("reason")))}
    elif name in ("loop.enable", "loop.disable"):
        key = handler.headers.get("Idempotency-Key", "") if handler else ""
        control.switch(values["loop"], name == "loop.enable", idem=key)
        result = {"loop": serialize(control.loop_detail(values["loop"]), contract.LOOP)}
    elif name == "secret.remove":
        result = remove_secret(values["scope"], values["name"])
    elif name == "secret.check":
        scope, secret = values["scope"], values["name"]
        result = secret_result(scope, secret, secretstore.check(scope, secret, principals.current()))
    elif name == "webhook.rotate":
        result = {"text": webhooks.rotate(values["loop"], principals.current())}
    elif name.startswith(("token.", "pairing.")) or name in ("principal.revoke", "session.end"):
        result = credential_http.write_operation(name, values, handler)
    elif name == "task.create" and values.get("start"):
        ticket = (values.get("ticket") or "").strip()
        body = (values.get("body") or "").strip()
        started = tasks.start(
            values["repo"],
            prompt=body,
            title=values.get("title", ""),
            kind=values.get("kind", ""),
            ticket=ticket,
            force=values.get("force") is not False,
        )
        run_id = started.get("run", "")
        run_row = runs.run_get(run_id) if run_id else None
        tid = started.get("task") or ticket or ""
        repo_name = started.get("repo") or values["repo"]
        task_row = (task_store.task_get(tid) if tid else None) or _task_placeholder(
            repo_name, tid, values.get("kind") or "feat")
        _set_run_location(handler, run_id, created=bool(run_id) and started.get("why") != "already running")
        result = {
            "task": serialize(task_row, contract.TASK),
            "run": run_object(run_row),
            "state": started.get("state", ""),
            "why": started.get("why", ""),
        }
    elif name == "task.run":
        tid = values["task"].strip()
        repo_name = (values.get("repo") or "").strip()
        if not repo_name:
            found_task = task_store.task_get(tid.lower())
            if found_task is None:
                raise control.NotFound(f"no task {tid}")
            repo_name, tid = found_task["repo"], found_task["id"]
        started = tasks.start(repo_name, ticket=tid, force=values.get("force") is not False)
        run_id = started.get("run", "")
        run_row = runs.run_get(run_id) if run_id else None
        _set_run_location(handler, run_id, created=bool(run_id) and started.get("why") != "already running")
        result = {
            "run": run_object(run_row), "task": started["task"], "repo": started["repo"],
            "state": started["state"], "why": started["why"],
        }
    elif name.startswith("task.") or name == "comment.create":
        result = {"task": change_task(name, values)}
    elif name == "session.start":
        from . import sessions

        sess = sessions.launch(
            values["prompt"],
            device=values.get("device") or "local",
            model=values.get("model", ""),
            run_with=values.get("runner", ""),
            tools=values.get("tools"),
            repo=values.get("repo", ""),
            budget_usd=values.get("budget_usd"),
            task=bool(values.get("task", False)),
            draft_pr=bool(values.get("draft_pr", False)),
        )
        _set_run_location(handler, sess["id"], created=True)
        result = {"run": run_object(runs.run_get(sess["id"])), "session": sess}
    elif name == "run.resume":
        started = tasks.resume(values["run"], values.get("node") or values.get("step") or "",
                               values.get("base_version"))
        _set_run_location(handler, started["run"], created=True)
        result = {"run": run_object(runs.run_get(started["run"]))}
    elif name == "run.repair":
        started = tasks.repair(values["run"], values.get("base_version"))
        _set_run_location(handler, started["run"], created=started.get("state") == "queued")
        result = {"run": run_object(runs.run_get(started["run"]))}
    elif name == "run.step":
        started = tasks.run_step(values["run"], values["node"], values.get("set") or {},
                                 values.get("base_version"))
        _set_run_location(handler, started["run"], created=True)
        result = {"run": run_object(runs.run_get(started["run"]))}
    elif name == "run.import":
        run_id = record.start(
            values["kind"],
            status="queued",
            pid=None,
            repo=values.get("repo") or None,
            issue=values.get("issue"),
            summary=values.get("summary") or "",
            branch=values.get("branch") or None,
            pr_url=values.get("pr_url") or None,
        )
        _set_run_location(handler, run_id, created=True)
        result = {"run": run_object(runs.run_get(run_id))}
    elif name == "loop.start":
        from . import loop_run

        ev_list = _resolve_events_input(values.get("events"))
        row = loop_run.launch(values["loop"], events=ev_list, wait=False)
        _set_run_location(handler, row["id"], created=True)
        result = {"run": run_object(row)}
    elif name == "loop.try":
        from . import scaffold

        ev_list = _resolve_events_input(values.get("events"))
        row = scaffold.start_try(values["loop"], events=ev_list, wait=False)
        _set_run_location(handler, row["id"], created=True)
        result = {"run": run_object(row)}
    elif name == "loop.draft":
        from . import scaffold

        row = scaffold.start_draft(scaffold.DraftRequest(
            values["sentence"], values.get("name", ""), values.get("repo", ""), values.get("model", ""),
        ))
        _set_run_location(handler, row["id"], created=True)
        result = {"run": run_object(row)}
    elif name == "chat.create":
        from . import chat

        shown = chat.new(values.get("project", ""))
        run_row = None
        if text := (values.get("text") or "").strip():
            shown, run_row, created = chat.send_v1(shown["id"], text, queue_if_busy=True)
            _set_run_location(handler, run_row["id"] if run_row else "", created=created)
        elif handler is not None:
            handler.success_status = 201
        result = {"chat": serialize(shown, contract.CHAT), "run": run_object(run_row)}
    elif name == "chat.send":
        from . import chat

        cid = dashboard.whole_number(values["chat"])
        text = values["text"].strip()
        if not text:
            raise contract.Problem("invalid_input", "Write a message first.")
        try:
            shown, run_row, created = chat.send_v1(cid, text, queue_if_busy=True)
        except chat.NoChat as error:
            raise contract.Problem("not_found", str(error)) from None
        except chat.Busy as error:
            raise contract.Problem("conflict", str(error)) from None
        _set_run_location(handler, run_row["id"] if run_row else "", created=created)
        result = {"chat": serialize(shown, contract.CHAT), "run": run_object(run_row)}
    elif name == "chat.session_start":
        from . import chat

        cid = dashboard.whole_number(values["chat"])
        try:
            sess = chat.start_session(
                cid,
                values["prompt"],
                repo=values.get("repo"),
                device=values.get("device", ""),
                model=values.get("model", ""),
                runner=values.get("runner", ""),
                budget_usd=values.get("budget_usd"),
            )
        except chat.NoChat as error:
            raise contract.Problem("not_found", str(error)) from None
        _set_run_location(handler, sess["id"], created=True)
        result = {"run": run_object(runs.run_get(sess["id"])), "session": sess}
    elif name == "triage.refresh":
        code = triage.run(dry_run=bool(values.get("dry_run", False)))
        result = {"ok": code == 0, "code": code, "run": None}
    elif name == "triage.research":
        from . import research

        if values.get("next"):
            code = research.next_due(dry_run=bool(values.get("dry_run", False)))
            result = {"ref": values["ticket"], "code": code, "run": None}
        else:
            found_row = research.ticket(values["ticket"])
            if not found_row:
                raise control.NotFound(f"{values['ticket']} is not a research ticket on the triage board")
            code = research.run_now(found_row["ref"], force=bool(values.get("force", False)),
                                    dry_run=bool(values.get("dry_run", False)))
            result = {"ref": found_row["ref"], "code": code, "run": None}
    elif name == "health.check":
        from . import heartbeat

        code = heartbeat.run(
            dry_run=bool(values.get("dry_run", False)),
            force_probes=bool(values.get("force_probes", False)),
            quiet=bool(values.get("quiet", True)),
        )
        result = {"ok": code == 0, "code": code}
    elif name == "incident.scan":
        from . import oncall

        result = {"summary": oncall.scan(config.load())}
    elif name == "run.stop":
        result = dashboard.stop_run(handler, values["run"], values["base_version"], public=True)
    elif name == "run.steer":
        result = dashboard.steer_run(values["run"], values["text"])
    elif name in ("run.pin", "run.unpin"):
        identity = values["run"]
        try:
            (record.pin if name == "run.pin" else record.unpin)(identity)
        except LookupError as error:
            raise control.NotFound(str(error)) from error
        result = {"id": identity}
    elif name == "chat.drop":
        result = {"chat": dashboard.drop_chat(values["chat"])}
    elif name == "chat.file":
        from . import chat as chat_mod

        raw_chat = str(values["chat"]).strip()
        if not (raw_chat.isascii() and raw_chat.isdigit()) or int(raw_chat) <= 0:
            raise contract.Problem("validation", "Chat id must be a positive whole number.")
        draft_fp = values.get("draft")
        if draft_fp is not None and not isinstance(draft_fp, str):
            raise contract.Problem("validation", "draft must be a string.")
        try:
            item = chat_mod.file_draft(
                int(raw_chat),
                draft=draft_fp,
                repo=values.get("repo"),
                title=values.get("title"),
                return_item=True,
            )
        except chat_mod.NoChat as error:
            raise contract.Problem("not_found", str(error)) from None
        except chat_mod.NoDraft as error:
            raise contract.Problem("invalid_state", str(error)) from None
        except refusals.Invalid as error:
            raise contract.Problem("validation", str(error)) from None
        result = {"item": item}
    elif name == "serve.install":
        result = dashboard.install_serve(handler.server.inside_serve)
    elif name == "hook.ingest":
        result = {"received": tool_events.ingest(values["origin"], values.get("stream", ""),
                                                 values["offset"], values["events"])}
    elif name == "inbox.raise":
        result = {"item": inbox.raise_manual(values, credential=handler.credential)}
    elif name == "inbox.snooze":
        result = {"item": inbox.snooze(values["item"], base_version=values["base_version"],
                                       until=values.get("until"), until_changed=bool(values.get("until_changed")))}
    elif name == "inbox.shelve":
        result = {"item": inbox.shelve(values["item"], base_version=values["base_version"],
                                       reason=values["reason"], until=values.get("until"))}
    elif name == "inbox.resolve":
        result = inbox.resolve(
            values["item"],
            base_version=values["base_version"],
            verb=values["verb"],
            option=values.get("option"),
            answer=values.get("answer"),
            answers=values.get("answers"),
            reason=values.get("reason"),
            fix=values.get("fix"),
            prevent=values.get("prevent"),
            credential=handler.credential,
            pending_secrets=inbox.server_secrets(handler.server),
        )
    elif name == "inbox.retry":
        result = inbox.retry_item(
            values["item"],
            base_version=values.get("base_version"),
            credential=handler.credential,
            pending_secrets=inbox.server_secrets(handler.server),
        )
    elif name == "device.probe":
        device_name = values["device"]
        try:
            devices.get(device_name)
        except devices.DeviceError as error:
            raise contract.Problem("not_found", str(error)) from None
        key = (handler.headers.get("Idempotency-Key") if handler else None) or secrets.token_hex(12)
        wait_seconds = prefer_wait(handler, default=2.0, maximum=10.0)
        future = devices.probe_async(device_name, idem=key)
        try:
            future.result(timeout=wait_seconds)
        except concurrent.futures.TimeoutError:
            if handler is not None:
                handler.success_status = 202
            result = {
                "accepted": True,
                "watch": f"/api/v1/events?idem={quote(key, safe='')}",
                "device": device_name,
            }
        else:
            dev_view = needed(devices.device_get_view(device_name), device_name)
            result = {"device": dev_view, "probe": dev_view["last_probe"]}
    elif name == "push.subscribe":
        result = {"subscription": webpush.subscribe_v1(values, principals.contact(handler.person))}
    elif name == "push.remove":
        result = webpush.remove_v1(values["subscription"])
    elif name == "outbox.edit":
        result = {"item": outbox.edit(values["item"], body=values["body"], base_rev=values["base_rev"],
                                      base_version=values["base_version"])}
    elif name == "outbox.open":
        result = {"item": outbox.open(values["item"], base_version=values["base_version"])}
    elif name == "outbox.approve":
        result = {"item": outbox.approve(values["item"], rev=values["rev"], base_version=values["base_version"])}
    elif name == "outbox.undo":
        result = {"item": outbox.undo(values["item"], base_version=values["base_version"])}
    elif name == "outbox.hold":
        result = {"item": outbox.hold(values["item"], base_version=values["base_version"])}
    elif name == "outbox.release":
        result = {"item": outbox.release(values["item"], base_version=values["base_version"])}
    elif name == "outbox.reject":
        result = {"item": outbox.reject(values["item"], reason=values.get("reason", ""),
                                        base_version=values["base_version"])}
    elif name == "outbox.retry":
        result = {"item": outbox.retry(values["item"], base_version=values["base_version"])}
    elif name == "outbox.discard":
        result = {"item": outbox.discard(values["item"], base_version=values["base_version"])}
    elif name == "triage.send":
        try:
            item = triage.send(
                config.load_or_problem()[0],
                values["ticket"],
                values["questions"],
                values["answers"],
                str(values.get("repo") or ""),
                base_version=values["base_version"],
                return_item=True,
            )
        except triage.NotOnBoard as error:
            raise contract.Problem("not_found", str(error)) from None
        except triage.NotSendable as error:
            raise contract.Problem("invalid_state", str(error)) from None
        except triage.BadAnswers as error:
            raise contract.Problem("validation", str(error)) from None
        except refusals.Upstream as error:
            raise contract.Problem("upstream_failed", str(error)) from None
        result = {"data": item}
    else:
        raise contract.Problem("not_found", f"Unknown operation {name}")
    return with_seq(result)


def with_seq(result):
    with foundation.reader() as conn:
        return {"seq": events.metadata(conn)["head"], **result}


def change_task(name, values):
    from . import chat

    body = values.get("body", "").strip()
    kind = values.get("kind", "feat")
    if ("kind" in values and kind not in config.KINDS) or (name == "task.create" and not body) or ("body" in values and not body):
        raise contract.Problem("invalid_input", "A task needs a body and a supported kind")
    if name == "task.create":
        repo = tasks.repo_named(config.load(), values["repo"])
        identity = task_store.task_add(repo.name, body, title=values.get("title", "").strip() or chat.title_of(body),
                                       kind=kind)
    else:
        identity = values.get("task", values.get("id", "")).strip().lower()
        if name == "task.close":
            open_states = tuple(state for state in config.TASK_STATES if state not in config.TASK_FINISHED)
            if not task_store.task_set_state(identity, "closed", from_states=open_states):
                raise contract.Problem("invalid_state", f"Task {identity} cannot be closed")
        elif name == "comment.create":
            if not task_store.task_comment(identity, "owner", body):
                raise control.NotFound(f"no task {identity}")
        else:
            fields = {key: value for key, value in values.items() if key not in ("id", "base_version")}
            if not fields or ("state" in fields and fields["state"] not in config.TASK_STATES):
                raise contract.Problem("invalid_input", "Send at least one editable field and a supported state")
            try:
                changed = task_store.task_update(identity, values["base_version"], fields)
            except writes.VersionConflict as error:
                raise contract.Problem("task_version_conflict", str(error),
                                       current=serialize(error.current, contract.TASK)) from error
            return serialize(needed(changed, identity), contract.TASK)
    return serialize(task_store.task_get(identity), contract.TASK)
