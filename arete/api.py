from __future__ import annotations

from collections import Counter
from urllib.parse import unquote

from . import (
    board, codebase, config, contract, control, credential_http, events, record, refusals,
    settings, tasks, tool_events, triage, util,
)
from .store import chats, insights, runs
from .store import db as foundation
from .store import tasks as task_store
from .store import tokens, writes

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


def meta():
    with foundation.reader() as conn:
        data = foundation.metadata(conn)
        stream = events.metadata(conn)
    return {"api": {"version": contract.VERSION, "minor": contract.MINOR, "contract_sha": contract.digest()},
            "server": {"engine_sha": codebase.STAMP}, "oldest_client": contract.OLDEST_CLIENT,
            "instance": {k: data[f"instance_{k}"] for k in ("id", "tag", "name", "created")},
            "schema": {"version": int(data["schema_version"]),
                       "oldest_writer": int(data["oldest_writer"]) if data["oldest_writer"] is not None else None},
            "stream": stream}


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
        return [{"id": r["id"], "run_id": values["run"], "name": r["step"], "model": r["model"] or "",
                 "runner": r["runner"] or "", "cost_usd": r["cost_usd"] or 0, "seconds": r["seconds"] or 0}
                for r in runs.steps(values["run"])]
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


def prepare(name, values):
    op = needed(contract.BY_NAME.get(name), name)
    contract.validate(values, op["input"])
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
    if name == "usage.record":
        return provider(handler, "post_usage", "", {})
    with foundation.one_read() as conn:
        seq = events.metadata(conn)["head"]
        result = read(name, values, handler)
        return {"seq": seq, **result}


def read(name, values, handler):
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
    if name == "batch.run":
        return {"results": [execute(r["operation"], r.get("input", {}), handler) for r in values["requests"]]}
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
    if name == "health.get":
        return {"health": board.health_board()}
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
        if not after.isdecimal():
            raise contract.Problem("invalid_input", "after must be a nonnegative decimal cursor")
        found = control.feed(values["run"], int(after))
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
    if name == "task.list":
        return {"tasks": task_rows()}
    resource, action = name.split(".")
    if action == "get" and resource == "run":
        return {"run": run_object(projection("run", identity)["run"])}
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


def dispatch(handler, op, path_values, query):
    name, method = op["name"], op["method"]
    identity = {k: unquote(v) for k, v in path_values.items()}
    values = {**query, **identity}
    write = contract.keyed_write(op)
    if method != "GET":
        body = handler.body()
        if write and (query or identity.keys() & body.keys()):
            raise contract.Problem("invalid_input", "Send writable fields in the JSON body and the target in the URL")
        values = {**body, **identity}
    try:
        if write:
            prepare(name, values)
            if name == "pairing.start":
                handler.success_status = op["success_status"]
                return credential_http.start_pairing(handler)
            result = handler.keyed_action(method, op["path"], path_values, lambda: mutation(name, values, handler),
                                          atomic=name not in ("run.stop", "serve.install"),
                                          status=op["success_status"], redact=name in ("token.issue", "token.rotate"))
            if name == "pairing.claim":
                credential_http.finish_pairing_claim(handler)
            return result
        return execute(name, values, handler)
    except refusals.Refusal as error:
        if not getattr(error, "code", None) and error.status in (400, 404):
            raise contract.Problem("not_found" if error.status == 404 else "invalid_input", str(error)) from error
        raise


def mutation(name, values, handler):
    from . import dashboard

    if name.startswith(("token.", "pairing.")) or name in ("principal.revoke", "session.end"):
        result = credential_http.write_operation(name, values, handler)
    elif name.startswith("task.") or name == "comment.create":
        result = {"task": change_task(name, values)}
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
    elif name == "serve.install":
        result = dashboard.install_serve(handler.server.inside_serve)
    elif name == "hook.ingest":
        result = {"received": tool_events.ingest(values["origin"], values.get("stream", ""),
                                                 values["offset"], values["events"])}
    else:
        raise contract.Problem("not_found", f"Unknown operation {name}")
    with foundation.reader() as conn:
        return {"seq": events.metadata(conn)["head"], **result}


def change_task(name, values):
    from . import chat

    body = values.get("body", "").strip()
    kind = values.get("kind", "feat")
    if ("kind" in values and kind not in config.KINDS) or ("body" in values and not body):
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
