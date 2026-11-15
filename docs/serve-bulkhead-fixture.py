"""Offline fixture for serve's per-part failure isolation.

    python docs/serve-bulkhead-fixture.py serve --port 8765
    python docs/serve-bulkhead-fixture.py scenario NAME --passes N
    python docs/serve-bulkhead-fixture.py inspect

`serve` runs the real dashboard handler and drives the real Server.run_pass on request. `scenario` and `inspect` talk to it
through files under the sandbox, so no HTTP route is added. Everything lives under .arete-sandbox/serve-bulkhead.
"""
import argparse
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / ".arete-sandbox" / "serve-bulkhead"
AREAS = {name: ROOT / name for name in ("config", "state", "logs", "worktrees", "tmp", "home")}
REQUESTS, RESULTS, INSPECT, SERVING = ROOT / "requests", ROOT / "results", ROOT / "inspect.json", ROOT / "serving.pid"
for variable, area in (("ARETE_CONFIG_DIR", "config"), ("ARETE_STATE_DIR", "state"), ("ARETE_LOG_DIR", "logs"),
                       ("ARETE_WORKTREES_DIR", "worktrees"), ("TMPDIR", "tmp"), ("HOME", "home")):
    AREAS[area].mkdir(parents=True, exist_ok=True)
    os.environ[variable] = str(AREAS[area])
sys.path.insert(0, str(REPO))

GOOD = "[replay]\nall = true\n\n[jev]\nenabled = false\n"
EVENT = {"type": "test", "title": "item", "data": {"value": 1}, "trust": "internal"}
SCENARIOS = {}


def scenario(name):
    def register(fn):
        SCENARIOS[name] = fn
        return fn
    return register


class Rig:
    def __init__(self, oncall=False, real_launch=False, build=True):
        from unittest import mock
        from arete import config, loop_run, explain, loops, paths, runner, state, tasks, util, webpush, alerts
        self.mock, self.state, self.util, self.loops, self.paths, self.config = mock, state, util, loops, paths, config
        self.wipe()
        paths.ensure_dirs()
        state.ensure_schema()
        self.config_text = GOOD + ("\n[oncall]\nenabled = true\n" if oncall else "")
        paths.CONFIG_FILE.write_text(self.config_text)
        loops.SWITCHES_FILE.write_text(json.dumps({p.name: False for p in loops.BUNDLED_LOOPS_DIR.iterdir() if p.is_dir()}))
        self.log = paths.LOG_DIR / "serve.log"
        sys.stdout = self.log.open("w", buffering=1)
        self.beats, self.launched, self.claims, self.alert_calls, self.asks, self.faults, self.errors = [], [], [], [], [], [], []
        self.gate = None
        self.expire = tasks.expire_asks
        self.patches = [
            mock.patch.object(runner, "usage_hold", lambda *a, **k: ""), mock.patch.object(webpush, "remind", lambda cfg: []),
            mock.patch.object(state, "release_settled_claims", lambda: None),
            mock.patch.object(explain, "plain_summary", lambda a, c: "fixed plain summary"),
            mock.patch.object(tasks, "expire_asks", self.spy_asks),
            mock.patch.object(alerts, "raise_alert", self.fake_raise), mock.patch.object(alerts, "clear_alert", self.fake_clear)]
        if not real_launch:
            self.patches.append(mock.patch.object(loop_run, "launch", self.fake_launch))
        for p in self.patches:
            p.start()
        self.loop_run = loop_run
        self.pending_faults = []
        for name in ("aaa", "bbb"):
            self.make_loop(name)
        self.server = self.new_server() if build else None

    def wipe(self):
        for area in ("state", "logs", "config", "tmp"):
            for child in AREAS[area].iterdir():
                shutil.rmtree(child) if child.is_dir() else child.unlink()

    def make_loop(self, name, trigger='every = "1m"', command="true"):
        folder = self.paths.CONFIG_DIR / "loops" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "loop.toml").write_text(f'kind = "command"\n[trigger]\n{trigger}\n[run]\ncommand = "{command}"\n')

    def spy_asks(self, cfg):
        self.asks.append(len(self.beats))
        return self.expire(cfg)

    def fake_raise(self, repo, key, title, status):
        if self.gate:
            self.gate.wait(20)
        self.alert_calls.append({"call": "raise", "key": key, "pass": len(self.beats), "title": title})
        return "raised"

    def fake_clear(self, repo, key):
        self.alert_calls.append({"call": "clear", "key": key, "pass": len(self.beats)})
        return "cleared"

    def fake_launch(self, name, item=None, cfg=None, **kw):
        for fault in self.pending_faults[:1]:
            self.pending_faults.remove(fault)
            raise fault
        if not self.state.take_queue_item(item, f"r-{item}"):
            raise self.loop_run.control.NotFound("taken")
        self.launched.append(name)
        self.claims.append({"item": item, "loop": name, "run": f"r-{item}"})
        return {"id": f"r-{item}"}

    def new_server(self):
        from arete import serve
        server = serve.Server()
        server.sweep_vms = lambda: None
        server.watch_oncall = lambda: None
        server.pruned = serve.time.monotonic()
        self.serve = serve
        return server

    def fault(self, target, name, replacement):
        patch = self.mock.patch.object(target, name, replacement)
        patch.start()
        self.faults.append(patch)

    def clear_faults(self):
        for patch in self.faults:
            patch.stop()
        self.faults.clear()
        self.paths.CONFIG_FILE.write_text(self.config_text)

    def poll_fault(self, name="aaa", message="bulkhead probe"):
        real = self.server.poll

        def poll(loop):
            if loop.name == name:
                raise ValueError(message)
            return real(loop)
        self.fault(self.server, "poll", poll)

    def due(self):
        for name in ("aaa", "bbb"):
            self.state.set_loop_state(name, last_fire=self.util.iso(self.util.now() - __import__("datetime").timedelta(minutes=2)))

    def drain(self, server=None):
        from concurrent.futures import wait
        server = server or self.server
        wait([f for f in [server.alert_last, server.sweep, *server.jobs.values(), *server.polls.values()] if f])

    def step(self, due=True, drain=True, server=None):
        server = server or self.server
        if due:
            self.due()
        server.run_pass()
        if drain:
            self.drain(server)
        from arete import control
        beat = self.util.read_json(control.BEAT_FILE, {})
        self.beats.append(beat)
        return beat

    def log_lines(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def tick_check(self):
        from arete import heartbeat
        return next((c for c in heartbeat.check_serve() if c.key == "serve:tick"), None)

    def readers(self):
        from arete import board, cli, control, heartbeat
        beat = control.serve_beat()
        healthy = {"checks": {"rows": {"heartbeat": True, "checks": []}}, "alerts": {"rows": {"alerts": []}}}
        check = self.tick_check()
        return {"alive": beat.get("alive"), "stuck": beat.get("stuck"), "cli_line": cli.serve_line(beat),
                "health_word": board.health_word(beat, healthy),
                "heartbeat": [{"key": c.key, "severity": c.severity, "ok": c.ok, "detail": c.detail} for c in heartbeat.check_serve()],
                "serve_tick": {"severity": check.severity, "ok": check.ok, "detail": check.detail} if check else None}

    def checks_per_pass(self, store):
        check = self.tick_check()
        store.append({"pass": len(self.beats), "severity": check.severity if check else None, "ok": check.ok if check else None})

    def snapshot(self):
        state = self.state
        return {"beats": self.beats, "launches": self.launched, "claims": self.claims,
                "queue": [{"id": i["id"], "loop": i["loop"], "grp": i["grp"]} for i in state.pending()],
                "approvals": {i: {"status": a["status"], "plain_summary": a.get("plain_summary")} for i in range(1, 6) if (a := state.approval(i))},
                "housekeeping": {"ask_expiry_passes": self.asks, "passes": len(self.beats)},
                "alerts": self.alert_calls, "readers": self.readers(), "log": self.log_lines(),
                "runs": [{"id": r["id"], "loop": r.get("loop"), "status": r.get("status")} for r in state.runs(limit=50, firings=True)]}


rig = None


def fresh(**kw):
    global rig
    if rig:
        rig.clear_faults()
        for p in rig.patches:
            p.stop()
        rig.server.pool.shutdown()
        for pool in rig.server.job_pools.values():
            pool.shutdown()
    rig = Rig(**kw)
    return rig


def failing_of(beat):
    return {k: {"error": v["error"], "ticks": v["ticks"]} for k, v in (beat.get("failing") or {}).items()}


@scenario("clean")
def clean(passes):
    r = fresh()
    for _ in range(passes):
        r.step()
    return {"launches": r.launched, "failing": r.beats[-1]["failing"], "tick_ok": r.beats[-1]["tick_ok"],
            "last_ok_equals_at": r.beats[-1]["last_ok"] == r.beats[-1]["at"]}


@scenario("loop")
def loop(passes):
    r = fresh()
    base = r.step()
    r.poll_fault()
    per_pass = []
    for _ in range(passes):
        before = len(r.launched)
        beat = r.step()
        r.checks_per_pass(per_pass)
        per_pass[-1].update(bbb=r.launched[before:].count("bbb"), failing=failing_of(beat), last_ok_unchanged=beat["last_ok"] == base["last_ok"])
    return {"passes": per_pass, "tick_failed_lines": len([x for x in r.log_lines() if "loop:aaa: tick failed:" in x])}


@scenario("recover")
def recover(passes):
    r = rig or fresh()
    for approval in r.state.approvals():
        if approval["status"] == "pending" and approval["emit"].get("type") == "renamed_emit":
            with r.state.db() as c:
                c.execute("UPDATE approvals SET status = 'rejected' WHERE id = ?", (approval["id"],))
    r.clear_faults()
    r.pending_faults.clear()
    for _ in range(passes):
        beat = r.step()
        r.checks_per_pass(r.errors)
    return {"recovered_lines": [x for x in r.log_lines() if "recovered after" in x], "failing": beat["failing"], "tick_ok": beat["tick_ok"],
            "last_ok_equals_at": beat["last_ok"] == beat["at"], "serve_tick": r.errors[-1]}


@scenario("oncall")
def oncall(passes):
    from arete import config, oncall as oncall_module
    r = fresh(oncall=True)
    r.log.touch()
    oncall_module.scan(config.load())
    r.step()
    r.poll_fault()
    for _ in range(passes):
        r.step()
    oncall_module.scan(config.load())
    first = [str(i) for i in r.state.incidents()]
    r.step()
    oncall_module.scan(config.load())
    second = [str(i) for i in r.state.incidents()]
    return {"incidents_after_scan": len(first), "incidents_after_fourth_pass": len(second),
            "naming_loop_aaa": len([i for i in second if "loop:aaa" in i]), "incident_naming_loop_aaa": [i for i in second if "loop:aaa" in i][:1]}


@scenario("approvals")
def approvals(passes):
    r = fresh()
    r.state.add_approval("aaa", "r1", {"type": "push", "item": {"repo": "x"}}, "one")
    bad = r.state.add_approval("aaa", "r2", {"type": "renamed_emit", "item": {}}, "two")
    for _ in range(passes):
        r.step(due=False)
        r.checks_per_pass(r.errors)
    return {"bad_approval": bad, "failing": failing_of(r.beats[-1]), "valid_summary": r.state.approval(1)["plain_summary"],
            "ask_expiry_passes": len(r.asks), "severity": r.errors[-1]["severity"]}


@scenario("queue")
def queue(passes):
    from arete import loops
    r = fresh()
    r.step(due=False)
    one = r.state.enqueue("aaa", "aaa", "", "event", [EVENT])
    two = r.state.enqueue("bbb", "bbb", "", "event", [EVENT])

    def hold(self, cfg, connect=None):
        if self.name == "aaa":
            raise RuntimeError("usage unreadable")
        return ""
    r.fault(loops.Loop, "usage_hold", hold)
    for _ in range(passes):
        r.step(due=False)
    return {"aaa_item": one, "bbb_item": two, "aaa_item_unclaimed": r.state.queue_item(one)["run_id"] is None,
            "bbb_item_claimed": r.state.queue_item(two)["run_id"], "launched": r.launched, "failing": failing_of(r.beats[-1])}


@scenario("waits")
def waits(passes):
    from arete import control, loops
    r = fresh()
    r.step(due=False)
    out = {}

    def queued(*groups, loop="aaa"):
        return [r.state.enqueue(loop, loop, g, "event", [{**EVENT, "title": f"{g}{i}"}]) for i, g in enumerate(groups)]

    def clear():
        for item in r.state.pending():
            r.state.drop_queue_item(item["id"])
        r.launched.clear()
    one, two = queued("g", "g")
    r.step(due=False)
    out["group"] = {"launches": r.launched.count("aaa"), "wait_lines": [x for x in r.log_lines() if f"queue item {two} waits, group g takes one at a time" in x]}
    clear()
    for fault in (r.loop_run.NeverBegan("did not begin"), control.Refused("not now")):
        one, two = queued("g", "g")
        r.pending_faults.append(fault)
        r.step(due=False)
        out[type(fault).__name__] = {"launches": r.launched.count("aaa"), "first_run": r.state.queue_item(one)["run_id"], "second_run": r.state.queue_item(two)["run_id"]}
        clear()
    one, two = queued("g", "g")
    tries = []

    def hold(self, cfg, connect=None):
        tries.append(1)
        if len(tries) == 1:
            raise RuntimeError("unhandled item failure")
        return ""
    with r.mock.patch.object(loops.Loop, "usage_hold", hold):
        beat = r.step(due=False)
    out["unhandled"] = {"launches": r.launched.count("aaa"), "failing": list(beat["failing"]), "next_member_ran": r.state.queue_item(two)["run_id"] is not None}
    clear()
    r.step(due=False)
    item, = queued("")
    r.pending_faults.extend([control.Busy("aaa"), control.Busy("aaa")])
    r.step(due=False)
    r.step(due=False)
    out["busy_lines"] = [x for x in r.log_lines() if f"queue item {item} waits" in x]
    clear()
    item, = queued("")

    def never(self, cfg, connect=None):
        raise AssertionError("usage_hold asked after a spent limit")
    with r.mock.patch.object(r.loop_run, "spent_limit", lambda loop, cfg: "daily budget"), r.mock.patch.object(loops.Loop, "usage_hold", never):
        beat = r.step(due=False)
    out["spent_before_usage_hold"] = {"lines": [x for x in r.log_lines() if f"queue item {item} waits, its daily budget is spent" in x], "failing": list(beat["failing"])}
    clear()
    gone = r.state.enqueue("ghost", "ghost", "", "event", [EVENT])
    r.step(due=False)
    out["dropped"] = [x for x in r.log_lines() if f"ghost: dropped queue item {gone}" in x]
    cfg = r.config.load()
    mine = type("L", (), {})()
    mine.name, mine.queue_name = "y", "q"
    row = {"kind": "loop", "queue": "q", "loop": "x", "grp": "g"}
    slots = r.serve.QueueSlots([row])
    out["precedence"] = {"group": slots.why_full({"grp": "g"}, mine, cfg), "queue_full": slots.why_full({"grp": ""}, mine, cfg)}
    mine.name = "x"
    out["precedence"]["busy_before_full"] = r.serve.QueueSlots([row]).why_full({"grp": ""}, mine, cfg)
    return out


def alert_run(r, passes, gate=False):
    r.state.add_approval("aaa", "r1", {"type": "push", "item": {"repo": "x"}}, "one")
    r.fault(r.server, "dispatch", r.mock.Mock(side_effect=RuntimeError("dispatch down")))
    seen = []
    for _ in range(passes):
        r.step(due=False, drain=not gate)
        seen.append(len([c for c in r.alert_calls if c["call"] == "raise"]))
    return seen


@scenario("alerts")
def alerts_scenario(passes):
    r = fresh()
    seen = alert_run(r, passes)
    return {"raises_after_each_pass": seen, "ask_expiry_passes": len(r.asks), "summary": r.state.approval(1)["plain_summary"],
            "failing": failing_of(r.beats[-1]), "note": "run `scenario recover --passes 1` for the one clear"}


@scenario("fresh-alert")
def fresh_alert(passes):
    r = fresh(build=False)
    r.state.alert_set("serve:tick", None)
    r.server = r.new_server()
    for _ in range(max(passes, 2)):
        r.step(due=False)
    out = {"clears_on_fresh_server": len([c for c in r.alert_calls if c["call"] == "clear"])}
    slow = fresh()
    slow.gate = threading.Event()
    alert_run(slow, 10, gate=True)
    slow.clear_faults()
    slow.step(due=False, drain=False)
    slow.gate.set()
    slow.drain()
    out["slow_raise_order"] = [c["call"] for c in slow.alert_calls]
    return out


@scenario("bad-config")
def bad_config(passes):
    r = fresh()
    first = r.step()
    r.due()
    before = [r.state.loop_states(), [i["id"] for i in r.state.pending()]]
    r.paths.CONFIG_FILE.write_text("[replay\n")
    for _ in range(passes):
        beat = r.step(due=False)
    after = [r.state.loop_states(), [i["id"] for i in r.state.pending()]]
    return {"failing": failing_of(beat), "loops_off_before": [first["loops"], first["off"]], "loops_off_after": [beat["loops"], beat["off"]],
            "at_advanced": beat["at"] > first["at"], "last_fire_and_queue_unchanged": before == after, "config_problem": beat.get("config_problem"),
            "launches_after": len(r.launched), "note": "run `scenario recover --passes 1` to restore config.toml"}


@scenario("states")
def states(passes):
    r = fresh()
    r.make_loop("ccc", "run_at_load = true")
    r.state.set_loop_state("ccc", metric_on=1)
    r.due()
    read_states = r.state.loop_states
    stamp = read_states()
    r.fault(r.state, "loop_states", r.mock.Mock(side_effect=OSError("busy")))
    for _ in range(passes):
        beat = r.step(due=False)
    loaded = r.server.loaded
    r.checks_per_pass(r.errors)
    return {"failing": failing_of(beat), "serve_tick": r.errors[-1], "queue_rows": len(r.state.pending()), "launched": r.launched,
            "loaded_after_failed_pass": loaded, "loop_state_unchanged": read_states() == stamp, "note": "run `scenario recover --passes 1` for the load fire and due schedules"}


@scenario("beat")
def beat_scenario(passes):
    from arete import control, util
    r = fresh()
    with r.mock.patch.object(r.state, "pending", r.mock.Mock(side_effect=OSError("busy"))):
        first = r.step(due=False)
    with r.mock.patch.object(r.state, "runs_marked_running", r.mock.Mock(side_effect=OSError("busy"))):
        second = r.step(due=False)
    real = util.write_json

    def once(path, data, *a, **k):
        if path == control.BEAT_FILE and not once.fired:
            once.fired = True
            raise OSError("disk")
        return real(path, data, *a, **k)
    once.fired = False
    with r.mock.patch.object(util, "write_json", once):
        r.server.run_pass()
    failed = [x for x in r.log_lines() if "beat failed:" in x]
    third = r.step(due=False)
    out = {"pending_running_when_pending_fails": [first["pending"], first["running"]], "when_running_fails": [second["pending"], second["running"]],
           "beat_failed_lines": failed, "ticks_around_failed_write": [second["ticks"], third["ticks"]]}
    stale = dict(third, at=util.iso(util.now() - __import__("datetime").timedelta(minutes=10)))
    real(control.BEAT_FILE, stale)
    out["stale_presentation"] = {k: v for k, v in r.readers().items() if k in ("alive", "cli_line", "health_word")}
    r.step(due=False)
    return out


@scenario("guard")
def guard(passes):
    from unittest import mock
    r = fresh()
    order, errors = [], ["same", "same", "other"]

    def boom(label):
        def fn(*a):
            order.append(label)
            raise RuntimeError(errors[min(order.count(label), 3) - 1] if label == "tick" else "x")
        return fn
    for name, label in (("load_new_code", "code"), ("tick", "tick"), ("reap_inherited", "inherited")):
        r.fault(r.server, name, boom(label))
    for _ in range(max(passes, 3)):
        r.step(due=False)
    lines = r.log_lines()
    out = {"order": order[:3], "failing": failing_of(r.beats[-1]), "tick_failed_lines": [x for x in lines if "tick: tick failed:" in x]}
    r.clear_faults()
    out["after_recovery"] = failing_of(r.step(due=False))
    out["recovered_lines"] = [x for x in r.log_lines() if "recovered after" in x]
    with mock.patch.dict(os.environ, {r.serve.RESTARTED_FOR_NEW_CODE: ""}):
        restarted = r.new_server()
    out["reloaded"] = [r.step(due=False)["reloaded"], r.step(due=False, server=restarted)["reloaded"]]
    restarted.pool.shutdown()
    return out


@scenario("retirement")
def retirement(passes):
    from arete import loops
    r = fresh()
    r.step()
    r.poll_fault()
    bad = r.state.add_approval("aaa", "r2", {"type": "renamed_emit", "item": {}}, "two")
    item = r.state.enqueue("bbb", "bbb", "", "event", [EVENT])

    def hold(self, cfg, connect=None):
        if self.name == "bbb":
            raise RuntimeError("usage unreadable")
        return ""
    r.fault(loops.Loop, "usage_hold", hold)
    out = {}
    for _ in range(max(passes, 2)):
        beat = r.step(due=False)
    out["failing"] = failing_of(beat)
    loops.SWITCHES_FILE.write_text(json.dumps({**loops.switches(), "aaa": False}))
    out["loop_off"] = list(r.step(due=False)["failing"])
    r.state.drop_queue_item(item)
    out["item_gone"] = list(r.step(due=False)["failing"])
    with r.state.db() as c:
        c.execute("UPDATE approvals SET status = 'rejected' WHERE id = ?", (bad,))
    out["approval_decided"] = list(r.step(due=False)["failing"])
    loops.SWITCHES_FILE.write_text(json.dumps({**loops.switches(), "aaa": True}))
    for _ in range(2):
        r.step(due=False)
    counted = failing_of(r.beats[-1]).get("loop:aaa")
    r.fault(r.state, "loop_states", r.mock.Mock(side_effect=OSError("busy")))
    skipped = r.step(due=False)
    out["parent_stage_failed"] = {"before": counted, "after": failing_of(skipped).get("loop:aaa"), "stage": failing_of(skipped).get("stage:states")}
    return out


@scenario("command")
def command(passes):
    r = fresh(real_launch=True)
    r.make_loop("real", command="true")
    for name in ("aaa", "bbb"):
        r.loops.set_enabled(next(x for x in r.loops.load_all(r.config.load()) if x.name == name), False)
    r.state.set_loop_state("real", last_fire=r.util.iso(r.util.now() - __import__("datetime").timedelta(minutes=2)))
    for _ in range(passes):
        r.step(due=False)
    deadline = time.time() + 60
    while time.time() < deadline and not [x for x in r.state.runs(loop="real", firings=True) if x["status"] not in ("running", "starting")]:
        time.sleep(0.5)
    runs = [{"id": x["id"], "status": x["status"]} for x in r.state.runs(loop="real", firings=True)]
    return {"runs": runs, "queue_left": len(r.state.pending()), "failing": failing_of(r.beats[-1])}


def run_request(request):
    try:
        notes = SCENARIOS[request["name"]](request["passes"])
        ok = True
    except Exception as e:
        import traceback
        notes, ok = {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()}, False
    record = {"scenario": request["name"], "ok": ok, "notes": notes, **(rig.snapshot() if rig else {})}
    INSPECT.write_text(json.dumps(record, indent=2, default=str))
    return {"scenario": request["name"], "ok": ok, "notes": notes}


def serve(port):
    from arete import dashboard
    for folder in (REQUESTS, RESULTS):
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
    fresh()
    node = dashboard.Node(("127.0.0.1",), frozenset({"127.0.0.1"}), "fixture@example.com", "")
    owner = type("Owner", (), {"of": lambda self, address: node.owner})()
    server = dashboard.Server("127.0.0.1", port, node, owner, False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    SERVING.write_text(str(os.getpid()))
    print(f"fixture serving on http://127.0.0.1:{port}  (Ctrl-C stops it)", file=sys.__stdout__, flush=True)
    try:
        while True:
            for path in sorted(REQUESTS.glob("*.json")):
                request = json.loads(path.read_text())
                path.unlink()
                (RESULTS / path.name).write_text(json.dumps(run_request(request), indent=2, default=str))
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        SERVING.unlink(missing_ok=True)
        server.shutdown()


def alive():
    try:
        os.kill(int(SERVING.read_text()), 0)
        return True
    except (OSError, ValueError):
        return False


def ask(name, passes):
    if not alive():
        sys.exit("the fixture is not serving; start it with: python docs/serve-bulkhead-fixture.py serve --port 8765")
    key = f"{time.time_ns()}.json"
    (REQUESTS / (key + ".tmp")).write_text(json.dumps({"name": name, "passes": passes}))
    (REQUESTS / (key + ".tmp")).rename(REQUESTS / key)
    deadline = time.time() + 180
    while time.time() < deadline:
        if (RESULTS / key).exists():
            print((RESULTS / key).read_text())
            return
        time.sleep(0.2)
    sys.exit("the scenario did not finish in 180 s")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve").add_argument("--port", type=int, default=8765)
    run = sub.add_parser("scenario")
    run.add_argument("name", choices=sorted(SCENARIOS))
    run.add_argument("--passes", type=int, default=1)
    sub.add_parser("inspect")
    args = parser.parse_args()
    if args.command == "serve":
        serve(args.port)
    elif args.command == "scenario":
        ask(args.name, args.passes)
    else:
        print(INSPECT.read_text() if INSPECT.exists() else "{}")


if __name__ == "__main__":
    main()
