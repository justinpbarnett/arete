
with mock.patch.object(serve, "Server", side_effect=RuntimeError("construction failed")):
    try:
        serve.run(False)
    except RuntimeError:
        pass
assert not util.lock_is_held(serve.SERVE_LOCK)
s = serve.Server()
s.lock = serve.take_serve_lock()
s.code = frozenset()
s.code_seen = frozenset({"changed"})
with mock.patch.object(serve, "code_stamp", return_value=s.code_seen), \
     mock.patch.object(serve, "child_pids", return_value=set()), \
     mock.patch.object(serve.os, "execv", side_effect=OSError("exec failed")):
    try:
        s.load_new_code()
        raise AssertionError("exec did not fail")
    except OSError:
        pass
assert not os.get_inheritable(s.lock.fileno())
assert serve.SERVE_LOCK_FD not in os.environ
assert str(os.getpid()) in refused()
with mock.patch.object(serve, "code_stamp", return_value=s.code_seen), \
     mock.patch.object(serve.util, "sh", return_value=subprocess.CompletedProcess([], 1, "", "bad import")), \
     mock.patch.object(serve.os, "execv", side_effect=AssertionError("exec after failed import")):
    s.load_new_code()
assert "keeps the code it started with" in s.code_note
assert str(os.getpid()) in refused()
s.lock.close()
result(ok=True)
