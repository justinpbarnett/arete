from __future__ import annotations

import operator
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import NoReturn

from . import (
    chat,
    config,
    control,
    dashboard,
    engine,
    loops,
    paths,
    record,
    sources,
    state,
    util,
)
from .loops import Loop

TICK_SECONDS = 20
PRUNE_SECONDS = 3600
POLL_THREADS = 8
IMPORT_CHECK = "import arete.cli, arete.serve, arete.dashboard, arete.engine"
RESTARTED_FOR_NEW_CODE = "ARETE_SERVE_RESTARTED_FOR_NEW_CODE"
COMPARE = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "==": operator.eq,
           "!=": operator.ne}


def next_due(loop: Loop, after: datetime) -> str:
    t = loop.trigger
    if t.every:
        return util.iso(after + timedelta(seconds=t.every))
    return util.iso(t.cron.next_fire(after).astimezone(after.tzinfo)) if t.cron else ""


def code_stamp() -> frozenset:
    stamp = set()
    for p in paths.PACKAGE_DIR.rglob("*.py"):
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        stamp.add((str(p), st.st_mtime_ns, st.st_size))
    return frozenset(stamp)


def reap_children() -> None:
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass


def child_pids() -> set[int]:
    found = util.sh(["pgrep", "-P", str(os.getpid())], timeout=10)
    return {int(p) for p in found.stdout.split()} if found.returncode == 0 else set()


def is_due(loop: Loop, last_fire: str, now: datetime) -> bool:
    last = util.parse_iso(last_fire)
    if loop.trigger.every:
        return (now - last).total_seconds() >= loop.trigger.every
    return loop.trigger.cron.next_fire(last) <= now.astimezone()


class Server:
    def __init__(self) -> None:
        self.cfg = config.load()
        self.started = util.now()
        self.polls: dict[str, Future] = {}
        self.tried: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.waiting: dict[int, str] = {}
        self.restarted: Counter[int] = Counter()
        self.pool = ThreadPoolExecutor(POLL_THREADS, thread_name_prefix="poll")
        restarted_with = os.environ.pop(RESTARTED_FOR_NEW_CODE, None)
        self.loaded = restarted_with is not None
        self.inherited = {int(p) for p in (restarted_with or "").split(",") if p}
        self.pruned = 0.0
        self.ticks = 0
        self.code = self.code_seen = code_stamp()
        self.code_note = ""

    def say(self, text: str) -> None:
        print(f"{util.iso()} {text}", flush=True)

    def tick(self) -> None:
        self.cfg = config.load()
        every_loop = loops.load_all(self.cfg)
        live = [x for x in every_loop if x.enabled and not x.error]
        for row in record.reap(tickets=False):
            self.say(f"{row['id']}: {row['status']}, {record.INTERRUPTED}")
        states = state.loop_states()
        now = util.now()
        for loop in live:
            st = states.get(loop.name) or {}
            self.schedule(loop, st, now)
            self.watch_metric(loop, st)
            self.poll(loop)
        self.forget_unpolled_errors(live)
        self.loaded = True
        self.dispatch({x.name: x for x in reversed(every_loop)})
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
        queued = sources.fire(loop, trigger)
        state.set_loop_state(loop.name, last_fire=util.iso(now), next_due=next_due(loop, now))
        self.say(f"{loop.name}: fired by {trigger}" if queued else f"{loop.name}: {trigger} fire joins the one that waits")

    def watch_metric(self, loop: Loop, st: dict) -> None:
        if not loop.trigger.metric:
            return
        name, op, threshold = loop.trigger.metric
        value = state.latest_metric(name)
        if value is None:
            return
        on = COMPARE[op](value, threshold)
        if on and not st.get("metric_on"):
            if sources.fire(loop, "metric", [sources.metric_event(loop, value)]):
                self.say(f"{loop.name}: fired by metric {name} = {value:g}")
            state.set_loop_state(loop.name, metric_on=1)
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

    def forget_unpolled_errors(self, live: list[Loop]) -> None:
        polled = {sources.source_id(loop, i) for loop in live for i in range(len(loop.sources))}
        for sid in set(self.errors) - polled:
            self.errors.pop(sid, None)

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
        if items := sources.fire(loop, "event", found):
            self.say(f"{sid}: {util.count(len(found), 'new event')}, {util.count(len(items), 'queue item')}")

    def dispatch(self, by_name: dict[str, Loop]) -> None:
        running = [r for r in state.runs_marked_running() if r["kind"] == "loop"]
        per_queue = Counter(r["queue"] or r["loop"] for r in running)
        held_groups = {(r["queue"] or r["loop"], r["grp"]) for r in running if r["grp"]}
        busy = {r["loop"] for r in running}
        pending = state.pending()
        self.waiting = {i: why for i, why in self.waiting.items() if i in {item["id"] for item in pending}}
        for item in pending:
            loop = by_name.get(item["loop"])
            if not loop or not loop.enabled or loop.error:
                state.drop_queue_item(item["id"])
                self.say(f"{item['loop']}: dropped queue item {item['id']}, the loop is off or gone")
                continue
            key = (item["queue"], item["grp"])
            size = self.cfg.queues.get(item["queue"], 1)
            if item["grp"] and key in held_groups:
                why = f"group {item['grp']} takes one at a time"
            elif loop.name in busy:
                why = str(control.Busy(loop.name))
            elif per_queue[item["queue"]] >= size:
                why = f"queue {item['queue']} is full at {util.count(size, 'run')}"
            elif spent := engine.spent_limit(loop, self.cfg):
                why = f"its {spent} is spent"
            elif held := loop.usage_hold(self.cfg):
                why = held
            else:
                try:
                    run = engine.launch(loop.name, item=item["id"], cfg=self.cfg)
                except control.Busy as e:
                    busy.add(loop.name)
                    why = str(e)
                except engine.NeverBegan as e:
                    self.retry_once(item, str(e))
                    continue
                except (control.NotFound, control.Refused):
                    continue
                else:
                    self.waiting.pop(item["id"], None)
                    busy.add(loop.name)
                    per_queue[item["queue"]] += 1
                    why = ""
                    self.say(f"{loop.name}: started {run['id']} ({item['trigger']})")
            if item["grp"]:
                held_groups.add(key)
            if why and self.waiting.get(item["id"]) != why:
                self.waiting[item["id"]] = why
                self.say(f"{loop.name}: queue item {item['id']} waits, {why}")

    def retry_once(self, item: dict, why: str) -> None:
        if self.restarted[item["id"]]:
            state.drop_queue_item(item["id"])
            self.say(f"{item['loop']}: dropped queue item {item['id']}, {why}, the second time")
            return
        self.restarted[item["id"]] += 1
        state.take_queue_item(item["id"], None)
        self.say(f"{item['loop']}: queue item {item['id']} goes back in the queue, {why}")

    def reap_inherited(self) -> None:
        for pid in list(self.inherited):
            try:
                ended = os.waitpid(pid, os.WNOHANG)[0]
            except ChildProcessError:
                ended = pid
            if ended:
                self.inherited.discard(pid)

    def busy_with(self) -> str:
        if sum(not f.done() for f in self.polls.values()):
            return "a source poll is still running"
        if dashboard.POSTS_IN_FLIGHT.count:
            return "the dashboard is still answering a request"
        if chat.history()["busy"]:
            return "a chat message is still running"
        return ""

    def load_new_code(self) -> None:
        stamp = code_stamp()
        steady, self.code_seen = stamp == self.code_seen, stamp
        if stamp == self.code or not steady:
            return
        if busy := self.busy_with():
            self.note_once(f"new code waits to load: {busy}")
            return
        check = util.sh([sys.executable, "-c", IMPORT_CHECK], timeout=120)
        if check.returncode != 0:
            self.note_once("new code does not import, so serve keeps the code it started with: "
                           + util.last_line(check.stderr or check.stdout, 300))
            return
        reap_children()
        still_running = child_pids()
        self.say(f"new code: serve {os.getpid()} restarts itself" + (
            f"; it leaves {util.count(len(still_running), 'process', 'processes')} running" if still_running else ""))
        os.environ[RESTARTED_FOR_NEW_CODE] = ",".join(map(str, sorted(still_running)))
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])

    def note_once(self, text: str) -> None:
        if self.code_note != text:
            self.code_note = text
            self.say(text)

    def beat(self, every_loop: list[Loop], live: list[Loop]) -> None:
        util.write_json(control.BEAT_FILE, {
            "pid": os.getpid(), "at": util.iso(), "started": util.iso(self.started), "ticks": self.ticks,
            "tick_seconds": TICK_SECONDS, "loops": len(live), "off": len(every_loop) - len(live),
            "broken": {x.name: x.error for x in every_loop if x.error}, "pending": len(state.pending()),
            "running": sum(r["kind"] == "loop" for r in state.runs_marked_running()), "poll_errors": dict(self.errors),
        })


def run(with_dashboard: bool = True) -> NoReturn:
    server = Server()
    server.say(f"arete serve {os.getpid()}: tick every {TICK_SECONDS}s")
    if with_dashboard:
        threading.Thread(target=dashboard.serve, kwargs={"inside_serve": True}, name="dashboard", daemon=True).start()
    while True:
        started = time.monotonic()
        try:
            server.load_new_code()
            server.tick()
            server.reap_inherited()
        except Exception as e:
            server.say(f"tick failed: {type(e).__name__}: {e}")
        time.sleep(max(1.0, TICK_SECONDS - (time.monotonic() - started)))
