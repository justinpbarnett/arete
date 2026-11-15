
adopt = root / "adopt.py"
adopt.write_text(f"import sys;sys.path.insert(0,{str(paths.PACKAGE_DIR.parent)!r});" + r"""
import json, os, subprocess, time
from arete import serve
held = serve.take_serve_lock()
st = os.fstat(held.fileno())
sleeper = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(120)"], close_fds=False)
print("ADOPTED=" + json.dumps({"pid": os.getpid(), "inode": [st.st_dev, st.st_ino],
      "inheritable": os.get_inheritable(held.fileno()), "env": serve.SERVE_LOCK_FD in os.environ,
      "sleeper": sleeper.pid}), flush=True)
time.sleep(120)
""")
code = r"""
import json, os, sys
from arete import serve
s = serve.Server()
s.lock = serve.take_serve_lock()
st = os.fstat(s.lock.fileno())
print("ORIGINAL=" + json.dumps({"pid": os.getpid(), "inode": [st.st_dev, st.st_ino]}), flush=True)
serve.code_stamp = lambda: frozenset({"changed"})
real_exec = os.execv
serve.os.execv = lambda *a: real_exec(sys.executable, [sys.executable, "-I", ADOPT])
s.load_new_code()
s.load_new_code()
""".replace("ADOPT", repr(str(adopt)))
p = child(code)
sleeper = None
try:
    output = wait_output(p, "ADOPTED=")
    original = json.loads(next(line.split("=", 1)[1] for line in output.splitlines() if line.startswith("ORIGINAL=")))
    adopted = json.loads(next(line.split("=", 1)[1] for line in output.splitlines() if line.startswith("ADOPTED=")))
    sleeper = adopted["sleeper"]
    assert original["pid"] == adopted["pid"] == p.pid
    assert original["inode"] == adopted["inode"]
    assert not adopted["inheritable"] and not adopted["env"]
    assert str(p.pid) in refused()
    p.kill()
    p.wait(10)
    os.kill(sleeper, 0)
    with serve.take_serve_lock() as held:
        assert os.fstat(held.fileno()).st_ino == adopted["inode"][1]
finally:
    stop(p)
    if sleeper is not None:
        try:
            os.kill(sleeper, signal.SIGKILL)
        except ProcessLookupError:
            pass
result(ok=True)
