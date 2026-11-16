import json, sys, threading
from concurrent.futures import wait
from datetime import timedelta
from pathlib import Path
from unittest import mock
from arete.store import runs as run_store
from arete import alerts, board, cli, config, control, loop_run, explain, heartbeat, loops, oncall, paths, routing, serve, state, tasks, util, webpush

root, mode = Path(sys.argv[1]), sys.argv[2]
real_out = sys.stdout
LOG = paths.LOG_DIR / "serve.log"
GOOD = '[replay]\nall = true\n\n[jev]\nenabled = false\n'
paths.ensure_dirs()
state.migrate()
paths.CONFIG_FILE.write_text(GOOD + ('\n[oncall]\nenabled = true\n' if mode == "oncall" else ''))
loops.SWITCHES_FILE.write_text(json.dumps({p.name: False for p in loops.BUNDLED_LOOPS_DIR.iterdir() if p.is_dir()}))
EVENT = {"type": "test", "title": "item", "data": {"value": 1}, "trust": "internal"}
launched, calls = [], []


def make_loop(name, trigger='every = "1m"'):
    folder = paths.CONFIG_DIR / "loops" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "loop.toml").write_text(f'kind = "command"\n[trigger]\n{trigger}\n[run]\ncommand = "true"\n')


def fake_launch(name, item=None, cfg=None, **kw):
    for fault in calls:
        calls.remove(fault)
        raise fault
    if not state.take_queue_item(item, f"r-{item}"):
        raise control.NotFound("taken")
    launched.append(name)
    return {"id": f"r-{item}"}


def new_server():
    server = serve.Server()
    server.sweep_vms = lambda: None
    server.watch_oncall = lambda: None
    server.pruned = serve.time.monotonic()
    return server


def drain(server):
    wait([f for f in [server.alert_last, server.sweep, *server.jobs.values(), *server.polls.values()] if f])


def run(server, due=True, wait_for=True):
    if due:
        for name in ("aaa", "bbb"):
            state.set_loop_state(name, last_fire=util.iso(util.now() - timedelta(minutes=2)))
    server.run_pass()
    if wait_for:
        drain(server)
    return util.read_json(control.BEAT_FILE, {})


def log_lines():
    return LOG.read_text().splitlines() if LOG.exists() else []


def tick_check():
    return next((c for c in heartbeat.check_serve() if c.key == "serve:tick"), None)


def faulting(server, name="aaa", message="bulkhead probe"):
    real = server.poll

    def poll(loop):
        if loop.name == name:
            raise ValueError(message)
        return real(loop)
    return mock.patch.object(server, "poll", poll)


def done(**out):
    print("RESULT" + json.dumps(out), file=real_out, flush=True)


def finish(*servers):
    for server in servers:
        server.pool.shutdown()
        for pool in server.job_pools.values():
            pool.shutdown()


for name in ("aaa", "bbb"):
    make_loop(name)
sys.stdout = LOG.open("a", buffering=1)
server = new_server()
patches = [mock.patch.object(loop_run, "launch", fake_launch), mock.patch.object(routing, "usage_hold", lambda *a, **k: ""),
           mock.patch.object(webpush, "remind", lambda cfg: []), mock.patch.object(state, "release_settled_claims", lambda: None)]
for p in patches:
    p.start()
healthy = {"checks": {"rows": {"heartbeat": True, "checks": []}}, "alerts": {"rows": {"alerts": []}}}

if mode in ("fault", "oncall"):
    if mode == "oncall":
        LOG.touch()
        oncall.scan(config.load())
    clean = run(server)
    passes = []
    with faulting(server):
        for _ in range(3):
            before = len(launched)
            beat = run(server)
            check = tick_check()
            passes.append({"bbb": launched[before:].count("bbb"), "tick_ok": beat["tick_ok"], "ticks": beat["failing"]["loop:aaa"]["ticks"],
                           "error": beat["failing"]["loop:aaa"]["error"], "severity": check.severity, "ok": check.ok})
        if mode == "oncall":
            oncall.scan(config.load())
            first = [i["id"] for i in state.incidents()]
            run(server)
            oncall.scan(config.load())
            second = [i["id"] for i in state.incidents()]
            texts = [str(i) for i in state.incidents()]
            done(first=first, second=second, texts=texts)
            finish(server)
            sys.exit(0)
        beat = control.serve_beat()
        line = cli.serve_line(beat)
        details = " ".join(c.detail for c in heartbeat.check_serve())
        word = board.health_word(beat, healthy)
    lines = log_lines()
    last = run(server)
    after = tick_check()
    done(clean=clean["tick_ok"], clean_last_ok=clean["last_ok"] == clean["at"], passes=passes, alive=beat["alive"],
         stuck=beat["stuck"], line=line, details=details, word=word,
         failed_logs=len([x for x in lines if "loop:aaa: tick failed:" in x]),
         recovered=len([x for x in log_lines() if "loop:aaa: recovered after 3 ticks" in x]),
         failing=last["failing"], tick_ok=last["tick_ok"], last_ok=last["last_ok"] == last["at"], after_ok=after.ok)
    finish(server)

elif mode == "approvals":
    state.add_approval("aaa", "r1", {"type": "push", "item": {"repo": "x"}}, "one")
    state.add_approval("aaa", "r2", {"type": "renamed_emit", "item": {}}, "two")
    asks = []
    real_asks = tasks.expire_asks
    with mock.patch.object(explain, "plain_summary", lambda a, c: "fixed text"), \
            mock.patch.object(tasks, "expire_asks", lambda cfg: asks.append(1) or real_asks(cfg)):
        beats = [run(server, due=False) for _ in range(3)]
    check = tick_check()
    failing = beats[-1]["failing"]
    summary = state.approval(1)["plain_summary"]
    with state.db() as c:
        c.execute("UPDATE approvals SET status = 'rejected' WHERE id = 2")
    with mock.patch.object(explain, "plain_summary", lambda a, c: "fixed text"):
        after = run(server, due=False)
    done(ticks=[b["ticks"] for b in beats], failing={k: v["error"] for k, v in failing.items()}, summary=summary,
         asks=len(asks), severity=check.severity, after=after["failing"])
    finish(server)

elif mode == "queue":
    run(server, due=False)
    one = state.enqueue("aaa", "aaa", "", "event", [EVENT])
    two = state.enqueue("bbb", "bbb", "", "event", [EVENT])

    def hold(self, cfg, connect=None):
        if self.name == "aaa":
            raise RuntimeError("usage unreadable")
        return ""
    with mock.patch.object(loops.Loop, "usage_hold", hold):
        first = run(server, due=False)
        again = run(server, due=False)
    pending = [i["id"] for i in state.pending()]
    kept = {k: v["ticks"] for k, v in again["failing"].items()}
    with mock.patch.object(state, "pending", mock.Mock(side_effect=OSError("busy"))):
        unread = run(server, due=False)
    state.drop_queue_item(one)
    cleared = run(server, due=False)
    done(one=one, two=two, launched=launched, pending=pending, first=list(first["failing"]), kept=kept,
         unread={k: v["ticks"] for k, v in unread["failing"].items()}, unread_pending=unread["pending"],
         cleared=list(cleared["failing"]))
    finish(server)

elif mode == "waits":
    run(server, due=False)
    out = {}

    def queued(*groups, loop="aaa"):
        return [state.enqueue(loop, loop, g, "event", [{**EVENT, "title": f"{g}{i}"}]) for i, g in enumerate(groups)]

    def clear():
        for item in state.pending():
            state.drop_queue_item(item["id"])
    one, two = queued("g", "g")
    run(server, due=False)
    out["group"] = [launched.count("aaa"), len([x for x in log_lines() if f"queue item {two} waits, group g takes one at a time" in x])]
    clear()
    launched.clear()
    for fault in (loop_run.NeverBegan("did not begin"), control.Refused("not now")):
        one, two = queued("g", "g")
        calls.append(fault)
        run(server, due=False)
        out[type(fault).__name__] = [launched.count("aaa"), state.queue_item(one)["run_id"], state.queue_item(two)["run_id"]]
        clear()
        launched.clear()
    item, = queued("")
    calls.extend([control.Busy("aaa"), control.Busy("aaa")])
    run(server, due=False)
    run(server, due=False)
    out["busy"] = len([x for x in log_lines() if f"queue item {item} waits" in x])
    clear()
    item, = queued("")

    def never(self, cfg, connect=None):
        raise AssertionError("usage_hold asked after a spent limit")
    with mock.patch.object(loop_run, "spent_limit", lambda loop, cfg: "daily budget"), mock.patch.object(loops.Loop, "usage_hold", never):
        beat = run(server, due=False)
    out["spent"] = [len([x for x in log_lines() if f"queue item {item} waits, its daily budget is spent" in x]), list(beat["failing"])]
    clear()
    gone = state.enqueue("ghost", "ghost", "", "event", [EVENT])
    run(server, due=False)
    out["dropped"] = len([x for x in log_lines() if f"ghost: dropped queue item {gone}, the loop is off or gone" in x])
    done(**out)
    finish(server)

elif mode in ("alerts", "alert_order"):
    state.add_approval("aaa", "r1", {"type": "push", "item": {"repo": "x"}}, "one")
    order, asks = [], []
    gate = threading.Event()

    def raise_alert(repo, key, title, status):
        if mode == "alert_order":
            gate.wait(20)
        order.append(("raise", key))
        return "raised"

    def clear_alert(repo, key):
        order.append(("clear", key))
        return "cleared"
    real_asks = tasks.expire_asks
    broken = mock.Mock(side_effect=RuntimeError("dispatch down"))
    with mock.patch.object(alerts, "raise_alert", raise_alert), mock.patch.object(alerts, "clear_alert", clear_alert), \
            mock.patch.object(explain, "plain_summary", lambda a, c: "fixed text"), \
            mock.patch.object(tasks, "expire_asks", lambda cfg: asks.append(1) or real_asks(cfg)):
        seen, beats = [], []
        with mock.patch.object(server, "dispatch", broken):
            for i in range(10):
                beats.append(run(server, due=False, wait_for=mode == "alerts"))
                seen.append(len([o for o in order if o[0] == "raise"]) if mode == "alerts" else 0)
        calm = run(server, due=False, wait_for=mode == "alerts")
        gate.set()
        drain(server)
        if mode == "alerts":
            closed = len([o for o in order if o[0] == "clear"])
            expired = len(asks)
            fresh = new_server()
            state.alert_set("serve:tick", None)
            run(fresh, due=False)
            run(fresh, due=False)
            done(seen=seen, ticks=[b["ticks"] for b in beats], tick_ok=calm["tick_ok"], closed=closed, asks=expired,
                 fresh=len([o for o in order if o[0] == "clear"]) - closed, summary=state.approval(1)["plain_summary"])
            finish(fresh)
        else:
            done(order=[o[0] for o in order])
    finish(server)

elif mode == "bad_config":
    first = run(server)
    for name in ("aaa", "bbb"):
        state.set_loop_state(name, last_fire=util.iso(util.now() - timedelta(minutes=2)))
    before = [state.loop_states(), state.pending()]
    paths.CONFIG_FILE.write_text("[replay\n")
    beat = run(server, due=False)
    after = [state.loop_states(), state.pending()]
    line = cli.serve_line(control.serve_beat())
    paths.CONFIG_FILE.write_text(GOOD)
    fired = len(launched)
    back = run(server, due=False)
    done(failing=list(beat["failing"]), loops=[first["loops"], beat["loops"]], off=[first["off"], beat["off"]],
         ticks=[first["ticks"], beat["ticks"]], same=before == after, problem=bool(beat.get("config_problem")),
         line=line, recovered=list(back["failing"]), fires=[fired, len(launched)])
    finish(server)

elif mode == "states":
    make_loop("ccc", "run_at_load = true")
    state.set_loop_state("ccc", metric_on=1)
    for name in ("aaa", "bbb"):
        state.set_loop_state(name, last_fire=util.iso(util.now() - timedelta(minutes=2)))
    stamp = state.loop_states()
    with mock.patch.object(state, "loop_states", mock.Mock(side_effect=OSError("busy"))):
        beat = run(server, due=False)
        check = tick_check()
        loaded = server.loaded
    unchanged = state.loop_states() == stamp
    queued = len(state.pending()) + len(launched)
    back = run(server, due=False)
    done(failing=list(beat["failing"]), severity=check.severity, ok=check.ok, loaded=[loaded, server.loaded], unchanged=unchanged,
         queued=queued, launched=sorted(launched), beat_back=list(back["failing"]), ccc=state.loop_states()["ccc"]["metric_on"])
    finish(server)

elif mode == "extras":
    clean = run(server)
    out = {"last_ok_start": clean["last_ok"]}
    with faulting(server):
        first = run(server)
        second = run(server)
        out["last_ok_kept"] = [first["last_ok"], second["last_ok"]] == [clean["last_ok"]] * 2
        with mock.patch.object(state, "loop_states", mock.Mock(side_effect=OSError("busy"))):
            skipped = run(server, due=False)
        out["skipped"] = {k: v["ticks"] for k, v in skipped["failing"].items()}
        loops.SWITCHES_FILE.write_text(json.dumps({**loops.switches(), "aaa": False}))
        removed = run(server)
    out["removed"] = list(removed["failing"])

    def queued(group):
        return state.enqueue("bbb", "bbb", group, "event", [{**EVENT, "title": group}])
    one, two = queued("g"), queued("g")
    tries = []
    real_hold = loops.Loop.usage_hold

    def hold(self, cfg, connect=None):
        tries.append(1)
        if len(tries) == 1:
            raise RuntimeError("usage unreadable")
        return real_hold(self, cfg, connect)
    launched.clear()
    with mock.patch.object(loops.Loop, "usage_hold", hold):
        grouped = run(server, due=False)
    out["released"] = [launched.count("bbb"), list(grouped["failing"]) == [f"queue:{one}"], state.queue_item(two)["run_id"] is not None]

    cfg = config.load()
    mine = type("L", (), {})()
    mine.name, mine.queue_name = "y", "q"
    row = {"kind": "loop", "queue": "q", "loop": "x", "grp": "g"}
    slots = serve.QueueSlots([row])
    out["capacity"] = [slots.why_full({"grp": "g"}, mine, cfg), slots.why_full({"grp": ""}, mine, cfg), slots.why_full({"grp": "h"}, mine, cfg)]
    mine.name = "x"
    out["busy_first"] = serve.QueueSlots([row]).why_full({"grp": ""}, mine, cfg)
    mine.name, mine.queue_name = "z", "other"
    free = serve.QueueSlots([row])
    out["free"] = free.why_full({"grp": ""}, mine, cfg)
    free.took(mine, {})
    out["taken"] = free.why_full({"grp": ""}, mine, cfg)
    with mock.patch.dict(serve.os.environ, {serve.RESTARTED_FOR_NEW_CODE: ""}):
        reloaded = new_server()
    out["reloaded"] = [run(server, due=False)["reloaded"], run(reloaded, due=False)["reloaded"]]
    finish(reloaded)
    done(**out)
    finish(server)

elif mode == "beat":
    with mock.patch.object(state, "pending", mock.Mock(side_effect=OSError("busy"))):
        first = run(server, due=False)
    with mock.patch.object(run_store, "runs_marked_running", mock.Mock(side_effect=OSError("busy"))):
        second = run(server, due=False)
    real = util.write_json

    def once(path, data, *a, **k):
        if path == control.BEAT_FILE and not once.fired:
            once.fired = True
            raise OSError("disk")
        return real(path, data, *a, **k)
    once.fired = False
    with mock.patch.object(util, "write_json", once):
        server.run_pass()
    failed = [x for x in log_lines() if "beat failed:" in x]
    third = run(server, due=False)
    done(first=[first["pending"], first["running"]], second=[second["pending"], second["running"]], failed=len(failed),
         ticks=[second["ticks"], third["ticks"]])
    finish(server)

elif mode == "guard":
    order = []
    errors = ["same", "same", "other"]

    def boom(label, raises):
        def fn(*a):
            order.append(label)
            if raises:
                raise RuntimeError(errors[len([o for o in order if o == label]) - 1] if label == "tick" else "x")
        return fn
    with mock.patch.object(server, "load_new_code", boom("code", True)), mock.patch.object(server, "tick", boom("tick", True)), \
            mock.patch.object(server, "reap_inherited", boom("inherited", True)):
        beats = [run(server, due=False) for _ in range(3)]
    failing = beats[-1]["failing"]
    lines = log_lines()
    clean = run(server, due=False)
    done(order=order[:3], keys=sorted(failing), tick=failing["tick"], ticks=[b["ticks"] for b in beats],
         tick_logs=len([x for x in lines if "tick: tick failed:" in x]), code_ticks=failing["stage:code"]["ticks"],
         clean=clean["failing"], recovered=len([x for x in log_lines() if "tick: recovered after 3 ticks" in x]))
    finish(server)

for p in patches:
    p.stop()
