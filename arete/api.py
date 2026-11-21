from __future__ import annotations

from collections import Counter
from urllib.parse import unquote

from . import board, codebase, config, contract, control, events, record, refusals, settings, tasks, triage
from .store import chats, insights, runs
from .store import db as foundation
from .store import tasks as task_store

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


def execute(name, values, handler=None):
    prepare(name, values)
    if name == "usage.record":
        return provider(handler, "post_usage", "", {})
    with foundation.one_read() as conn:
        seq = events.metadata(conn)["head"]
        result = read(name, values, handler)
        return {"seq": seq, **result}


def read(name, values, handler):
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
    values = {**query, **{k: unquote(v) for k, v in path_values.items()}}
    if op["method"] == "POST":
        values = handler.body()
    try:
        return execute(op["name"], values, handler)
    except refusals.Refusal as error:
        if not getattr(error, "code", None) and error.status in (400, 404):
            raise contract.Problem("not_found" if error.status == 404 else "invalid_input", str(error)) from error
        raise
