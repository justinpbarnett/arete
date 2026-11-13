import json
import re

from . import __version__, outbound, state, util

KINDS = ("idea", "adjustment", "bug")
LIMITS = {"text": 4000, "expected": 2000, "actual": 2000, "steps": 2000}
MOST_CONTEXT_BYTES = 8 * 1024
MOST_ID = 120
VIEWS = ("live", "runs", "sessions", "loops", "triage", "outcomes", "health", "settings", "usage")
ITEM_KINDS = ("run", "session", "loop", "ticket")
RUN_ID = re.compile(r"\w[\w.-]*")
RUN_FACTS = ("kind", "status", "started", "finished")
EMPTY_DRAFT = {"kind": "idea", "text": "", "expected": "", "actual": "", "steps": ""}


class Refused(ValueError):
    pass


def view(connect=state.db) -> dict:
    return {"draft": state.capture_draft(connect), "limits": {**LIMITS, "context_bytes": MOST_CONTEXT_BYTES}, "kinds": list(KINDS)}


def checked_fields(body: dict) -> dict:
    fields = {k: body[k] for k in ("kind", *LIMITS) if k in body}
    if not fields:
        raise Refused(f"Send at least one of kind, {', '.join(LIMITS)}.")
    if "kind" in fields and fields["kind"] not in KINDS:
        raise Refused(f"The kind must be one of {', '.join(KINDS)}.")
    for name, most in LIMITS.items():
        if name not in fields:
            continue
        if not isinstance(fields[name], str):
            raise Refused(f"The {name} must be text.")
        if len(fields[name]) > most:
            raise Refused(f"The {name} is limited to {most:,} characters and this one has {len(fields[name]):,}.")
    return fields


def save(body: dict) -> dict:
    fields = checked_fields(body)
    held = state.capture_draft(state.reader) or EMPTY_DRAFT
    if any(held[k] != v for k, v in fields.items()):
        state.capture_save(**fields)
    return view()


def safe(value) -> str:
    return outbound.screen(outbound.sanitize(str(value or "")))


def run_facts(run_id: str) -> dict:
    run = state.run_get(run_id, connect=state.reader)
    if not run:
        return {}
    rows = state.steps(run_id, connect=state.reader)
    facts = {key: run.get(key) for key in RUN_FACTS}
    facts["step"] = rows[-1]["step"] if rows else ""
    facts["error"] = next((r["error"] for r in reversed(rows) if r.get("error")), "")
    if not facts["error"] and run.get("status") in ("failed", "interrupted"):
        facts["error"] = run.get("summary")
    return {key: safe(value) for key, value in facts.items() if value}


def snapshot(view_name: str, detail, code: str) -> str:
    if view_name not in VIEWS:
        raise Refused(f"The view must be one of {', '.join(VIEWS)}.")
    if detail is not None and not isinstance(detail, dict):
        raise Refused("The open item must be an object with a kind and an id.")
    snap = {"captured": util.iso(), "app": {"version": __version__, "code": code}, "view": view_name}
    if detail:
        kind, item_id = detail.get("kind"), detail.get("id")
        if kind not in ITEM_KINDS:
            raise Refused(f"The open item's kind must be one of {', '.join(ITEM_KINDS)}.")
        if not isinstance(item_id, str) or not item_id or len(item_id) > MOST_ID:
            raise Refused(f"The open item's id must be text of 1 to {MOST_ID} characters.")
        snap["item"] = {"kind": kind, "id": safe(item_id)}
        facts = run_facts(item_id) if kind in ("run", "session") and RUN_ID.fullmatch(item_id) else {}
        if facts:
            snap["run"] = facts
    text = json.dumps(snap, indent=2, ensure_ascii=False)
    if len(text.encode()) > MOST_CONTEXT_BYTES:
        raise Refused("The app state is over 8 KB, so it was not added.")
    return text


def add_context(body: dict, code: str) -> dict:
    text = snapshot(str(body.get("view") or ""), body.get("detail"), code)
    state.capture_save(context=text)
    return view()


def drop_context() -> dict:
    draft = state.capture_draft(state.reader)
    if draft and draft["context"]:
        state.capture_save(context="")
    return view()


def discard() -> dict:
    state.capture_discard()
    return view()
