
import subprocess
mode = {"config": 1}
execs = []
def fake_sh(argv, **kw):
    if argv[-1] == serve.IMPORT_CHECK:
        return subprocess.CompletedProcess(argv, 0, "", "")
    if argv[-1] == serve.CONFIG_CHECK:
        return subprocess.CompletedProcess(argv, mode["config"], "", "ValueError: cannot read")
    if argv == control.command(["migrate", "--no-contracts"]):
        return subprocess.CompletedProcess(argv, 0, "", "")
    return subprocess.CompletedProcess(argv, 1, "", "")
changed = frozenset({("changed", 1, 1)})
def reload_server():
    server, said = quiet_server()
    server.busy_with = lambda: ""
    server.lock = util.lock_file(serve.SERVE_LOCK)
    server.code, server.code_seen = frozenset(), changed
    return server, said
def settle():
    os.environ.pop(serve.SERVE_LOCK_FD, None)
    os.environ.pop(serve.RESTARTED_FOR_NEW_CODE, None)
with mock.patch.object(serve, "code_stamp", lambda: changed), mock.patch.object(util, "sh", fake_sh), \
        mock.patch.object(os, "execv", lambda *a: execs.append(a)):
    server, said = reload_server()
    server.load_new_code()
    server.tick()
    server.load_new_code()
    beat = util.read_json(control.BEAT_FILE, {})
    refused = {"execs": len(execs), "said": len([s for s in said if "new code cannot read config.toml" in s]),
               "beat": beat.get("code_problem", ""), "running": state.runs_marked_running()}
    mode["config"] = 0
    server.load_new_code()
    passed = {"execs": len(execs), "problem": server.code_problem}
    settle()
    execs.clear()
    mode["config"] = 1
    write(BAD)
    broken, _ = reload_server()
    broken.load_new_code()
    escaped = len(execs)
    settle()
    execs.clear()
    write(GOOD)
    server, said = reload_server()
    server.load_new_code()
with mock.patch.object(serve, "code_stamp", lambda: server.code):
    server.load_new_code()
result(refused=refused, passed=passed, escaped=escaped, reverted=server.code_problem)
