from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import timedelta

from . import config, devices, emits, loops, paths, record, sources, state, util
from .loops import Loop

BEAT_FILE = paths.STATE_DIR / "serve.json"
BEAT_STALE_SECONDS = 120
DETACHED_LOG = "loops.log"
COST_DAYS = 7
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


def detach(args: list[str], log_name: str = DETACHED_LOG, keep: tuple[int, ...] = ()) -> subprocess.Popen:
    paths.ensure_dirs()
    with open(paths.LOG_DIR / log_name, "a") as log:
        return subprocess.Popen([sys.executable, "-m", "arete.cli", *args], stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True, pass_fds=keep)


def find_loop(name: str, cfg: config.Config | None = None) -> Loop:
    try:
        return loops.find(name, cfg)
    except loops.LoopError as e:
        raise NotFound(str(e)) from e


def stop_run(run_id: str) -> dict:
    r = state.run_get(run_id)
    if not r:
        raise NotFound(f"no run {run_id}")
    if r["status"] != "running":
        raise Refused(f"{run_id} is not running; it ended {r['status']}")
    with record.dead(r) as gone:
        if gone:
            r = record.ask_stop(run_id)
            if r["kind"] not in record.NO_TICKET_KINDS:
                return {"id": run_id, "stopped": True, "was": "gone", "ticket": "told by the next pipeline poll"}
            record.interrupt(r)
            return {"id": run_id, "stopped": True, "was": "gone"}
    if not r["pid"]:
        raise Refused(f"{run_id} is still starting; stop it again in a moment")
    record.ask_stop(run_id)
    pid = int(r["pid"])
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    return {"id": run_id, "stopped": True, "pid": pid}


def serve_beat() -> dict:
    data = util.read_json(BEAT_FILE, {})
    if data.get("at"):
        data["age_seconds"] = round((util.now() - util.parse_iso(data["at"])).total_seconds())
        data["alive"] = util.pid_alive(data.get("pid")) and data["age_seconds"] < BEAT_STALE_SECONDS
    return data


def day_ago() -> str:
    return util.iso(util.now() - timedelta(days=1))


def loop_views(cfg: config.Config | None = None) -> list[dict]:
    cfg = cfg or config.load()
    every_loop = loops.load_all(cfg)
    states = state.loop_states()
    running = {r["loop"]: r["id"] for r in state.runs_marked_running() if r["kind"] == "loop"}
    waiting = Counter(item["loop"] for item in state.pending())
    spent: dict[str, float] = defaultdict(float)
    for s in state.steps_since(day_ago()):
        spent[s.get("loop") or ""] += s["cost_usd"] or 0
    out = []
    for loop in every_loop:
        last = next(iter(state.runs(kind="loop", loop=loop.name, limit=1, firings=True)), None)
        st = states.get(loop.name) or {}
        out.append({**loop.view(), "running": running.get(loop.name, ""), "pending": waiting[loop.name],
                    "next_due": st.get("next_due") if loop.enabled else None, "last_fire": st.get("last_fire"),
                    "spent_24h": round(spent[loop.name], 4),
                    "last": {k: last[k] for k in ("id", "status", "started", "finished", "summary", "cost_usd",
                                                  "trigger")} if last else None})
    return out


def loop_detail(name: str) -> dict:
    cfg = config.load()
    loop = find_loop(name, cfg)
    view = next(v for v in loop_views(cfg) if v["name"] == loop.name)
    source_ids = {sources.source_id(loop, i) for i in range(len(loop.sources))}
    return {**view, "prompt": loops.prompt_text(loop), "state": state.loop_state(loop.name)["data"],
            "runs": state.runs(kind="loop", loop=loop.name, limit=25, firings=True),
            "findings": state.findings(loop.name, 40),
            "waiting": [i for i in state.pending() if i["loop"] == loop.name],
            "cursors": [c for c in state.cursors() if c["source"] in source_ids],
            "webhook_path": f"/hook/{loop.name}" if loop.trigger.webhook else "",
            "digests": digests(loop), "metrics": loop_metrics(loop)}


def digests(loop: Loop) -> list[dict]:
    folder = emits.DIGESTS_DIR / loop.name.replace("/", ".")
    files = sorted(folder.glob("*.md"), reverse=True)[:DIGEST_DAYS] if folder.is_dir() else []
    return [{"day": f.stem, "text": f.read_text()[-DIGEST_CHARS:]} for f in files]


def loop_metrics(loop: Loop) -> list[dict]:
    latest: dict[str, dict] = {}
    for m in state.metrics_since(util.iso(util.now() - timedelta(days=METRIC_DAYS))):
        if m["loop"] == loop.name:
            seen = latest.setdefault(m["name"], {"name": m["name"], "points": 0})
            seen.update(value=m["value"], ts=m["ts"], points=seen["points"] + 1)
    return sorted(latest.values(), key=lambda m: m["name"])


def switch(name: str, enabled: bool) -> dict:
    loop = find_loop(name)
    if loop.error and enabled:
        raise Refused(f"{name} is broken: {loop.error}")
    loops.set_enabled(loop, enabled)
    return {"name": loop.name, "enabled": enabled}


def feed(run_id: str, after: int = 0) -> dict:
    r = state.run_get(run_id)
    if not r:
        raise NotFound(f"no run {run_id}")
    return {"run": r, "lines": state.feed(run_id, after)}


def approvals() -> list[dict]:
    return [{**a, "action": emits.EMITS[a["emit"]["type"]].describe(a["emit"]["item"])} for a in state.approvals("pending")]


def decide(approval_id: int, yes: bool) -> dict:
    if not state.approval(approval_id):
        raise NotFound(f"no approval {approval_id}")
    try:
        result = emits.approve(approval_id, yes)
    except ValueError as e:
        raise Refused(str(e)) from e
    return {"id": approval_id, "approved": yes, "result": result}


def fleet() -> list[dict]:
    try:
        return devices.views()
    except devices.DeviceError as e:
        return [{**devices.LOCAL.view(), "probe": None, "error": str(e)}]


def costs(days: float = COST_DAYS) -> dict:
    since = util.iso(util.now() - timedelta(days=days))
    groups: dict[str, dict[str, dict]] = {"model": {}, "runner": {}, "loop": {}, "day": {}}
    total = 0.0
    for s in state.steps_since(since):
        cost = s["cost_usd"] or 0
        total += cost
        keys = {"model": s["model"] or "?", "runner": s["runner"] or "?",
                "loop": s.get("loop") or s.get("kind") or s.get("run_id") or "?",
                "day": util.parse_iso(s["ts"]).astimezone().strftime("%Y-%m-%d")}
        for group, key in keys.items():
            row = groups[group].setdefault(key, {"name": key, "cost_usd": 0.0, "steps": 0, "tokens_in": 0,
                                                 "tokens_out": 0})
            row["cost_usd"] += cost
            row["steps"] += 1
            row["tokens_in"] += s["tokens_in"] or 0
            row["tokens_out"] += s["tokens_out"] or 0
    return {"days": days, "since": since, "total_usd": round(total, 4),
            **{f"by_{group}": sorted(({**r, "cost_usd": round(r["cost_usd"], 4)} for r in rows.values()),
                                     key=lambda r: (-r["cost_usd"], r["name"])) for group, rows in groups.items()}}
