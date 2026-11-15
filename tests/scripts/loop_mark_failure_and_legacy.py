
cfg = config.load()
planted = paths.STATE_DIR / "loops.json"
def plant(ok, detail):
    planted.write_text(json.dumps({"poll": {"ts": util.iso(), "ok": ok, "detail": detail}}))
def poll_check():
    return next(c for c in heartbeat.check_loops(cfg) if c.key == "loop:poll")
rid = record.start("loop", loop="poll", loop_kind="command")
record.settle(rid, "done")
plant(True, "legacy ok")
heartbeat.mark_loop("poll", False, "gh: HTTP 502")
failed = poll_check()
heartbeat.mark_loop("poll", True, "recovered")
plant(False, "legacy failed")
cleared = poll_check()
def empty(name):
    def check(*a, **k):
        return []
    check.__name__ = name
    return check
stubs = [mock.patch.object(heartbeat, n, empty(n)) for n in (
    "check_jobs", "check_probes", "check_claude_auth", "check_codex_auth", "check_codex_cleanup", "check_pi_auth",
    "check_hook", "check_disk", "check_serve", "check_backups", "check_costs", "check_oncall", "check_workspaces",
    "check_scratch")]
def no_wait(*a, **k):
    raise AssertionError("waited for the network")
raised, cleared_keys = [], []
runs = []
with contextlib.ExitStack() as stack:
    for s in stubs:
        stack.enter_context(s)
    stack.enter_context(mock.patch.object(util, "online", return_value=True))
    stack.enter_context(mock.patch.object(util, "wait_for_network", no_wait))
    stack.enter_context(mock.patch.object(heartbeat.install, "rotate_logs"))
    stack.enter_context(mock.patch.object(heartbeat.alerts, "raise_alert", lambda repo, key, *a: raised.append(key)))
    stack.enter_context(mock.patch.object(heartbeat.alerts, "clear_alert", lambda repo, key, *a: cleared_keys.append(key)))
    heartbeat.mark_loop("poll", False, "gh: HTTP 502")
    plant(True, "legacy ok")
    runs.append([heartbeat.run(quiet=True), planted.exists(), list(raised)])
    heartbeat.mark_loop("poll", True, "fine")
    plant(False, "legacy failed")
    runs.append([heartbeat.run(quiet=True), planted.exists(), "loop:poll" in cleared_keys])
print("RESULT:" + json.dumps({"failed": [failed.ok, failed.detail], "cleared": [cleared.ok, cleared.detail], "runs": runs}))
