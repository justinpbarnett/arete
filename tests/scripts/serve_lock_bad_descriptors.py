
for value in ("not-a-descriptor", "-1", "999999"):
    p = child('from arete import cli, serve; '
              'serve.Server = lambda: (_ for _ in ()).throw(AssertionError("constructed Server")); '
              'cli.main(["serve", "--no-dashboard"])',
              env={**os.environ, serve.SERVE_LOCK_FD: value})
    assert p.wait(30) == 1
    output = outputs[p].read_text()
    assert "cannot adopt ARETE_SERVE_LOCK_FD" in output and "constructed Server" not in output, output
with serve.take_serve_lock():
    pass
p = child(r"""
import os
from arete import serve, util
wrong = util.lock_file("wrong")
fd = wrong.fileno()
os.environ[serve.SERVE_LOCK_FD] = str(fd)
try:
    serve.take_serve_lock()
    raise AssertionError("adopted wrong inode")
except SystemExit as e:
    assert e.code == 1
assert serve.SERVE_LOCK_FD not in os.environ
try:
    os.fstat(fd)
    raise AssertionError("leaked rejected descriptor")
except OSError:
    pass
print("REJECTED")
""")
assert p.wait(30) == 0, outputs[p].read_text()
assert "REJECTED" in outputs[p].read_text()
result(ok=True)
