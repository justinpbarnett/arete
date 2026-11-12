from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from collections import Counter, defaultdict
from datetime import timedelta

from . import (
    config,
    delivery,
    devices,
    emits,
    hooks,
    lines,
    loops,
    paths,
    record,
    sources,
    state,
    util,
)
from .loops import Loop

BEAT_FILE = paths.STATE_DIR / "serve.json"
BEAT_STALE_SECONDS = 120
DETACHED_LOG = "loops.log"
COST_DAYS = 7
COST_GROUPS = ("step", "kind", "model")
STEP_FIELD_OF = {"step": "step", "kind": "task_kind", "model": "model"}
TOKEN_FIELDS = ("tokens_in", "cache_read", "cache_write", "tokens_out")
DIGEST_DAYS = 3
DIGEST_CHARS = 20000
METRIC_DAYS = 30


class NotFound(LookupError):
    pass


class Busy(RuntimeError):
    def __init__(self, name: str) -> None:
        super().__init__(f"{name} is running; stop it or wait")


class Refused(ValueError):
    pass


class KeptFailure(Refused):
    pass


def detach(args: list[str], log_name: str = DETACHED_LOG, keep: tuple[int, ...] = ()) -> subprocess.Popen:
    return launch([sys.executable, "-m", "arete.cli", *args], log_name, keep)


def launch(command: list[str], log_name: str = DETACHED_LOG, keep: tuple[int, ...] = ()) -> subprocess.Popen:
    paths.ensure_dirs()
    with open(paths.LOG_DIR / log_name, "a") as log:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True, pass_fds=keep)
    threading.Thread(target=proc.wait, name=f"reap-{proc.pid}", daemon=True).start()
    return proc


def find_loop(name: str, cfg: config.Config | None = None) -> Loop:
    try:
        return loops.find(name, cfg)
    except loops.LoopError as e:
        raise NotFound(str(e)) from e


def working_loop(name: str, cfg: config.Config | None = None) -> Loop:
    loop = find_loop(name, cfg)
    if loop.error:
        raise Refused(f"{loop.name} is broken: {loop.error}")
    return loop


def stop_run(run_id: str) -> dict:
    r = state.run_get(run_id)
    if not r:
        raise NotFound(f"no run {run_id}")
    if r["status"] != "running":
        raise Refused(f"{run_id} is not running; it ended {r['status']}")
    with record.dead(r) as gone:
        if gone:
            if not (r := record.ask_stop(run_id)):
                raise NotFound(f"no run {run_id}")
            devices.release(run_id, lambda text: state.add_feed(run_id, "device", text))
            if r["kind"] in record.TICKET_KINDS:
                return {"id": run_id, "stopped": True, "was": "gone", "ticket": "told by the next pipeline poll"}
            record.interrupt(r, devices.left_up(run_id))
            return {"id": run_id, "stopped": True, "was": "gone"}
    if not r["pid"]:
        raise Refused(f"{run_id} is still starting; stop it again in a moment")
    record.ask_stop(run_id)
    pid = int(r["pid"])
    for kill in (os.killpg, os.kill):
        try:
            kill(pid, signal.SIGTERM)
            break
        except (ProcessLookupError, PermissionError):
            continue
    return {"id": run_id, "stopped": True, "pid": pid}


def serve_beat() -> dict:
    data = util.read_json(BEAT_FILE, {})
    if data.get("at"):
        data["age_seconds"] = round((util.now() - util.parse_iso(data["at"])).total_seconds())
        data["process_gone"] = not util.pid_alive(data.get("pid"))
        data["alive"] = not data["process_gone"] and data["age_seconds"] < BEAT_STALE_SECONDS
    return data


def day_ago() -> str:
    return util.iso(util.now() - timedelta(days=1))


def loop_views(cfg: config.Config | None = None, connect=state.db) -> list[dict]:
    cfg = cfg or config.load()
    every_loop = loops.load_all(cfg)
    states = state.loop_states(connect)
    running = {r["loop"]: r["id"] for r in state.runs_marked_running(connect) if r["kind"] == "loop"}
    waiting = Counter(item["loop"] for item in state.pending(connect))
    spent: dict[str, float] = defaultdict(float)
    for s in state.steps_since(day_ago(), connect, paid_only=True):
        spent[s.get("loop") or ""] += s["cost_usd"] or 0
    out = []
    for loop in every_loop:
        last = next(iter(state.runs(kind="loop", loop=loop.name, limit=1, firings=True, connect=connect)), None)
        st = states.get(loop.name) or {}
        out.append({**loop.view(), "running": running.get(loop.name, ""), "pending": waiting[loop.name],
                    "next_due": st.get("next_due") if loop.enabled else None, "last_fire": st.get("last_fire"),
                    "spent_24h": round(spent[loop.name], 4),
                    "last": {k: last[k] for k in ("id", "status", "started", "finished", "summary", "cost_usd",
                                                  "trigger")} if last else None})
    return out


def loop_detail(name: str, connect=state.db) -> dict:
    cfg = config.load()
    loop = find_loop(name, cfg)
    view = next(v for v in loop_views(cfg, connect) if v["name"] == loop.name)
    source_ids = {sources.source_id(loop, i) for i in range(len(loop.sources))}
    runs = state.runs(kind="loop", loop=loop.name, limit=25, firings=True, connect=connect)
    paid = state.paid_by_run([r["id"] for r in runs], connect)
    return {**view, "prompt": loops.prompt_text(loop), "state": state.loop_state(loop.name, connect)["data"],
            "runs": [{**r, "paid": paid.get(r["id"], 0)} for r in runs],
            "findings": state.findings(loop.name, 40, connect),
            "waiting": [i for i in state.pending(connect) if i["loop"] == loop.name],
            "cursors": [c for c in state.cursors(connect) if c["source"] in source_ids],
            "webhook_path": hooks.hook_path(loop.name) if loop.trigger.webhook else "",
            "digests": digests(loop), "metrics": loop_metrics(loop, connect),
            "model": loop.model_for(cfg) if loop.agent and not loop.error else "",
            "metered": loop.meters_spend(cfg) if loop.agent else None, "held": loop.usage_hold(cfg, connect)}


def digests(loop: Loop) -> list[dict]:
    folder = emits.digest_folder(loop.name)
    files = sorted(folder.glob("*.md"), reverse=True)[:DIGEST_DAYS] if folder.is_dir() else []
    return [{"day": f.stem, "text": f.read_text()[-DIGEST_CHARS:]} for f in files]


def loop_metrics(loop: Loop, connect=state.db) -> list[dict]:
    latest: dict[str, dict] = {}
    for m in state.metrics_since(util.iso(util.now() - timedelta(days=METRIC_DAYS)), connect):
        if m["loop"] == loop.name:
            seen = latest.setdefault(m["name"], {"name": m["name"], "points": 0})
            seen.update(value=m["value"], ts=m["ts"], points=seen["points"] + 1)
    return sorted(latest.values(), key=lambda m: m["name"])


def switch(name: str, enabled: bool) -> dict:
    loop = working_loop(name) if enabled else find_loop(name)
    loops.set_enabled(loop, enabled)
    return {"name": loop.name, "enabled": enabled, "serve_up": bool(serve_beat().get("alive"))}


def feed(run_id: str, after: int = 0) -> dict:
    # a run settles in the same write as its end line, so reading the lines first never pairs that line with "running"
    lines = state.feed(run_id, after, connect=state.reader)
    r = state.run_get(run_id, connect=state.reader)
    if not r:
        raise NotFound(f"no run {run_id}")
    return {"run": r, "lines": lines}


def decide(approval_id: int, yes: bool) -> dict:
    try:
        return {"id": approval_id, "approved": yes, "result": delivery.decide(approval_id, yes)}
    except LookupError as e:
        raise NotFound(str(e)) from e
    except delivery.KeptFailure as e:
        raise KeptFailure(str(e)) from e
    except (ValueError, delivery.Failed) as e:
        raise Refused(str(e)) from e


def fleet() -> list[dict]:
    try:
        return devices.views()
    except devices.DeviceError as e:
        return [{**devices.LOCAL.view(), "probe": None, "error": str(e)}]


def cost_window(days: float | None) -> tuple[float, str]:
    days = COST_DAYS if days is None else days
    try:
        return days, util.iso(util.now() - timedelta(days=days))
    except (OverflowError, ValueError) as e:
        raise Refused(f"cannot look back {days:g} days: {e}") from e


def costs(days: float | None = None, connect=state.db) -> dict:
    days, since = cost_window(days)
    return {"days": days, **paid_since(since, connect)}


def step_costs(by: str, days: float | None = None, connect=state.db) -> dict:
    if by not in COST_GROUPS:
        raise Refused(f"cannot group costs by {by}; pick one of {', '.join(COST_GROUPS)}")
    days, since = cost_window(days)
    groups: dict[str, dict] = {}
    for s in state.steps_since(since, connect):
        name = s[STEP_FIELD_OF[by]] or f"no {by}"
        row = groups.setdefault(name, {"by": by, "name": name, "runs": set(), "calls": 0, "cost_usd": 0.0,
                                       **dict.fromkeys(TOKEN_FIELDS, 0), "minutes": 0.0, "api_minutes": 0.0})
        row["runs"].add(s["run_id"])
        row["calls"] += 1
        row["cost_usd"] += s["cost_usd"] or 0
        for f in TOKEN_FIELDS:
            row[f] += s.get(f) or 0
        row["minutes"] += (s["seconds"] or 0) / 60
        row["api_minutes"] += (s.get("api_seconds") or 0) / 60
    out = [{**r, "runs": len(r["runs"]), "cost_usd": round(r["cost_usd"], 4), "minutes": round(r["minutes"], 2),
            "api_minutes": round(r["api_minutes"], 2)} for r in groups.values()]
    waits = dict.fromkeys(lines.WAIT_CAUSES, 0.0)
    for r in state.runs_since(since, connect):
        try:
            recorded = json.loads(r.get("wait_minutes") or "{}")
        except ValueError:
            continue
        if not isinstance(recorded, dict):
            continue
        for cause in waits:
            if type(recorded.get(cause)) in (int, float):
                waits[cause] += recorded[cause]
    return {"days": days, "since": since, "by": by,
            "groups": sorted(out, key=lambda r: (-r["cost_usd"], -r["calls"], r["name"])),
            "wait_minutes": {cause: round(minutes, 2) for cause, minutes in waits.items()}}


def paid_since(since: str, connect=state.db) -> dict:
    groups: dict[str, dict[str, dict]] = {"model": {}, "runner": {}, "loop": {}, "day": {}}
    total = 0.0
    for s in state.steps_since(since, connect, paid_only=True):
        cost = s["cost_usd"] or 0
        total += cost
        keys = {"model": s["model"] or "no model", "runner": s["runner"] or "no runner",
                "loop": s.get("loop") or s.get("kind") or s.get("run_id") or "no loop",
                "day": util.parse_iso(s["ts"]).astimezone().strftime("%Y-%m-%d")}
        for group, key in keys.items():
            row = groups[group].setdefault(key, {"name": key, "cost_usd": 0.0, "steps": 0, "tokens_in": 0,
                                                 "tokens_out": 0})
            row["cost_usd"] += cost
            row["steps"] += 1
            row["tokens_in"] += s["tokens_in"] or 0
            row["tokens_out"] += s["tokens_out"] or 0
    return {"since": since, "total_usd": round(total, 4),
            **{f"by_{group}": sorted(({**r, "cost_usd": round(r["cost_usd"], 4)} for r in rows.values()),
                                     key=lambda r: (-r["cost_usd"], r["name"])) for group, rows in groups.items()}}
