from __future__ import annotations

import operator
import os
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import NoReturn

from . import config, control, dashboard, loops, sources, state, util
from .loops import Loop

TICK_SECONDS = 20
SETTLE_GRACE_SECONDS = 60
PRUNE_SECONDS = 3600
POLL_THREADS = 8
INTERRUPTED = "the process stopped before the run finished"
COMPARE = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "==": operator.eq,
           "!=": operator.ne}


def age_seconds(stamp: str) -> float:
    return (util.now() - util.parse_iso(stamp)).total_seconds()


def next_due(loop: Loop, after: datetime) -> str:
    t = loop.trigger
    if t.every:
        return util.iso(after + timedelta(seconds=t.every))
    return util.iso(t.cron.next_after(after.astimezone()).astimezone(after.tzinfo)) if t.cron else ""


def is_due(loop: Loop, last_fire: str, now: datetime) -> bool:
    last = util.parse_iso(last_fire)
    if loop.trigger.every:
        return (now - last).total_seconds() >= loop.trigger.every
    return loop.trigger.cron.next_after(last.astimezone()) <= now.astimezone()


def group_of(loop: Loop, e: dict) -> str:
    field = loop.trigger.group
    return str(e.get(field) or (e.get("data") or {}).get(field) or "") if field else ""


def settle_dead_runs() -> list[str]:
    settled = []
    for r in state.runs_marked_running():
        if r["kind"] not in ("loop", "session", "draft") or control.pid_alive(r.get("pid")):
            continue
        if age_seconds(r["started"]) < SETTLE_GRACE_SECONDS:
            continue
        control.end_group(r.get("pid"))
        state.finish_run(r["id"], "interrupted", summary=INTERRUPTED)
        state.add_feed(r["id"], "end", f"interrupted: {INTERRUPTED}")
        settled.append(r["id"])
    return settled


class Server:
    def __init__(self) -> None:
        self.cfg = config.load()
        self.started = util.now()
        self.polls: dict[str, Future] = {}
        self.tried: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.children: dict[str, tuple[subprocess.Popen, str, str, str]] = {}
        self.pool = ThreadPoolExecutor(POLL_THREADS, thread_name_prefix="poll")
        self.loaded = False
        self.pruned = 0.0
        self.ticks = 0

    def say(self, text: str) -> None:
        print(f"{util.iso()} {text}", flush=True)

    def tick(self) -> None:
        self.cfg = config.load()
        every_loop = loops.load_all(self.cfg)
        live = [x for x in every_loop if x.enabled and not x.error]
        for run_id in settle_dead_runs():
            self.say(f"{run_id}: {INTERRUPTED}")
        states = state.loop_states()
        now = util.now()
        for loop in live:
            st = states.get(loop.name) or {}
            self.schedule(loop, st, now)
            self.watch_metric(loop, st)
            self.poll(loop)
        self.loaded = True
        self.dispatch({x.name: x for x in every_loop})
        if time.monotonic() - self.pruned > PRUNE_SECONDS:
            state.prune()
            self.pruned = time.monotonic()
        self.ticks += 1
        self.beat(every_loop, live)

    def schedule(self, loop: Loop, st: dict, now: datetime) -> None:
        t = loop.trigger
        if not self.loaded and t.run_at_load:
            self.fire(loop, "load", now)
            return
        if not (t.cron or t.every):
            return
        if not st.get("last_fire"):
            state.set_loop_state(loop.name, last_fire=util.iso(now), next_due=next_due(loop, now))
            return
        if is_due(loop, st["last_fire"], now):
            self.fire(loop, "cron" if t.cron else "every", now)
        elif st.get("next_due") != (due := next_due(loop, util.parse_iso(st["last_fire"]))):
            state.set_loop_state(loop.name, next_due=due)

    def fire(self, loop: Loop, trigger: str, now: datetime) -> None:
        state.enqueue(loop.name, loop.queue_name, "", trigger)
        state.set_loop_state(loop.name, last_fire=util.iso(now), next_due=next_due(loop, now))
        self.say(f"{loop.name}: fired by {trigger}")

    def watch_metric(self, loop: Loop, st: dict) -> None:
        if not loop.trigger.metric:
            return
        name, op, threshold = loop.trigger.metric
        value = state.latest_metric(name)
        if value is None:
            return
        on = COMPARE[op](value, threshold)
        if on and not st.get("metric_on"):
            state.enqueue(loop.name, loop.queue_name, "", "metric", [{
                "type": "metric", "source": f"{loop.name}#metric", "key": f"{name}@{util.iso()}",
                "title": f"{name} = {value:g}, {op} {threshold:g}", "trust": "internal",
                "data": {"metric": name, "value": value, "rule": f"{op} {threshold:g}"}}])
            state.set_loop_state(loop.name, metric_on=1)
            self.say(f"{loop.name}: fired by metric {name} = {value:g}")
        elif not on and st.get("metric_on"):
            state.set_loop_state(loop.name, metric_on=0)

    def poll(self, loop: Loop) -> None:
        for i, source in enumerate(loop.sources):
            sid = sources.source_id(loop, i)
            running = self.polls.get(sid)
            if running and not running.done():
                continue
            if time.monotonic() - self.tried.get(sid, -1e9) < sources.every(source):
                continue
            self.tried[sid] = time.monotonic()
            self.polls[sid] = self.pool.submit(self.poll_source, loop, i)

    def poll_source(self, loop: Loop, index: int) -> None:
        sid = sources.source_id(loop, index)
        try:
            found = sources.poll(loop, index, self.cfg)
        except Exception as e:
            text = f"{type(e).__name__}: {util.first_line(str(e), 300)}"
            if (c := state.cursor(sid)) is not None:
                state.set_cursor(sid, c["value"], False, text)
            if self.errors.get(sid) != text:
                self.say(f"{sid}: poll failed: {text}")
            self.errors[sid] = text
            return
        self.errors.pop(sid, None)
        batches: dict[str, list[dict]] = {}
        for e in found:
            state.add_event(loop.name, e)
            batches.setdefault(group_of(loop, e), []).append(e)
        for group, batch in batches.items():
            state.enqueue(loop.name, loop.queue_name, group, "event", batch)
        if found:
            self.say(f"{sid}: {util.count(len(found), 'new event')}")

    def running(self) -> dict[str, tuple[str, str, str]]:
        out = {r["id"]: (r["loop"], r["queue"] or r["loop"], r["grp"] or "")
               for r in state.runs_marked_running() if r["kind"] == "loop"}
        for run_id, (proc, name, queue, group) in list(self.children.items()):
            if proc.poll() is not None:
                del self.children[run_id]
            else:
                out.setdefault(run_id, (name, queue, group))
        return out

    def dispatch(self, by_name: dict[str, Loop]) -> None:
        running = self.running()
        busy_loops = {name for name, _, _ in running.values()}
        per_queue = Counter(queue for _, queue, _ in running.values())
        held_groups = {(queue, group) for _, queue, group in running.values() if group}
        for item in state.pending():
            loop = by_name.get(item["loop"])
            if not loop or not loop.enabled or loop.error:
                state.drop_queue_item(item["id"])
                self.say(f"{item['loop']}: dropped queue item {item['id']}, the loop is off or gone")
                continue
            key = (item["queue"], item["grp"])
            full = per_queue[item["queue"]] >= self.cfg.queues.get(item["queue"], 1)
            if item["loop"] in busy_loops or full or (item["grp"] and key in held_groups):
                if item["grp"]:
                    held_groups.add(key)
                continue
            run_id = state.new_loop_run_id(loop.name)
            state.take_queue_item(item["id"], run_id)
            proc = control.detach(["loop", "exec", loop.name, "--fire", str(item["id"]), "--run-id", run_id])
            self.children[run_id] = (proc, loop.name, item["queue"], item["grp"])
            busy_loops.add(loop.name)
            per_queue[item["queue"]] += 1
            if item["grp"]:
                held_groups.add(key)
            self.say(f"{loop.name}: started {run_id} ({item['trigger']})")

    def beat(self, every_loop: list[Loop], live: list[Loop]) -> None:
        util.write_json(control.BEAT_FILE, {
            "pid": os.getpid(), "at": util.iso(), "started": util.iso(self.started), "ticks": self.ticks,
            "tick_seconds": TICK_SECONDS, "loops": len(live), "off": len(every_loop) - len(live),
            "broken": {x.name: x.error for x in every_loop if x.error}, "pending": len(state.pending()),
            "running": len(self.running()), "poll_errors": dict(self.errors),
        })


def run(with_dashboard: bool = True) -> NoReturn:
    server = Server()
    server.say(f"arete serve {os.getpid()}: tick every {TICK_SECONDS}s")
    if with_dashboard:
        threading.Thread(target=dashboard.serve, name="dashboard", daemon=True).start()
    while True:
        started = time.monotonic()
        try:
            server.tick()
        except Exception as e:
            server.say(f"tick failed: {type(e).__name__}: {e}")
        time.sleep(max(1.0, TICK_SECONDS - (time.monotonic() - started)))
