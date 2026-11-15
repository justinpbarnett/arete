
gate.set()
def CP(argv, code, err=""):
    return subprocess.CompletedProcess(argv, code, "", err)
def refusing(import_code, config_code=0):
    def sh(argv, **kw):
        seen.append(dashboard.POSTS_IN_FLIGHT.closed)
        if argv[-1] == serve.IMPORT_CHECK:
            return CP(argv, import_code, "SyntaxError: broken" if import_code else "")
        if argv[-1] == serve.CONFIG_CHECK:
            return CP(argv, config_code, "ValueError: cannot read" if config_code else "")
        return real_sh(argv, **kw)
    return sh
def works():
    return post(approval())["status"]
def count_exec(*a):
    execs.append(1)
def raising(*a):
    execs.append(1)
    raise OSError("fixture refuses to exec")
found = {}
def run(name, sh, execv=count_exec, server=None, around=None):
    global seen, execs
    seen, execs = [], []
    server, said = server or reload_server()
    error = attempt(server, sh, execv)
    found[name] = {"seen": seen, "execs": len(execs), "error": error, "said": " | ".join(said),
                   "closed": dashboard.POSTS_IN_FLIGHT.closed, "works": works()}
run("import", refusing(1))
run("config", refusing(0, 1))
run("execv", passing, raising)
server, said = reload_server()
calls = []
def second_busy_fails():
    calls.append(1)
    if len(calls) == 2:
        raise RuntimeError("fixture busy failure")
    return ""
server.busy_with = second_busy_fails
run("busy", passing, server=(server, said))
with mock.patch.object(serve, "child_pids", mock.Mock(side_effect=RuntimeError("fixture pgrep failure"))):
    run("children", passing)
flag = [False]
def work_starts(argv, **kw):
    flag[0] = argv[-1] == serve.IMPORT_CHECK or flag[0]
    return passing(argv, **kw)
with mock.patch.object(serve.chat, "any_busy", lambda: flag[0]):
    run("chat", work_starts)
flag[0] = False
with mock.patch.object(serve.capture, "checking", lambda: flag[0]):
    run("capture", work_starts)
result(**found)
