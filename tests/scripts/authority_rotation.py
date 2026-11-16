
import contextlib, fcntl, hmac, io, json, os, sys
from pathlib import Path
from arete import browser, cli, webhooks, record, sandbox, scratch, state, util
root, mode = Path(sys.argv[1]), sys.argv[2]
state.migrate()
sandbox.AUTHORITY = root / "home/verification-authority"
A = sandbox.AUTHORITY
def rotate():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["rotate-authority"])
    print("BEHAVIOR_RESULT=" + json.dumps({"code": code, "out": out.getvalue(), "err": err.getvalue()}))
if mode == "seed":
    with scratch.registry(True) as (fd, key):
        for i, s in enumerate(sys.argv[3].split(",")):
            scratch.store(fd, key, {"version": 2, "attempt": {"selector": f"{i:064x}"}, "state": s,
                                    "source_mutations": False})
    browser.write_pin({"@playwright/mcp": {"version": "0.0.0", "integrity": "sha512-demo"}})
    webhooks.secret("demo")
elif mode == "rotate":
    rotate()
elif mode == "kill-first-note":
    scratch.note_resigned = lambda *a: os._exit(9)
    rotate()
elif mode == "kill-third-resign":
    real, n = scratch.resign, [0]
    def die(*a):
        n[0] += 1
        if n[0] == 3:
            os._exit(9)
        real(*a)
    scratch.resign = die
    rotate()
elif mode == "kill-swap":
    scratch.swap_secret = lambda *a: os._exit(9)
    rotate()
elif mode == "run":
    record.start("pipeline", tag="x", repo="r", issue="1")
elif mode == "lease":
    lease = os.open(A / (f"{0:064x}" + ".lease"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lease, fcntl.LOCK_EX)
    rotate()
elif mode == "unheld-lease":
    os.close(os.open(A / (f"{0:064x}" + ".lease"), os.O_RDWR | os.O_CREAT, 0o600))
    rotate()
elif mode == "lock":
    with util.lock("poll"):
        rotate()
elif mode == "tamper":
    p = A / (f"{int(sys.argv[3]):064x}" + ".json")
    e = json.loads(p.read_text())
    e["signature"] = "0" * 64
    p.write_text(json.dumps(e))
elif mode == "report":
    old = bytes.fromhex(sys.argv[3])
    envs = [json.loads(p.read_text()) for p in [*A.glob("*.json"), A / browser.PIN]]
    broken = 0
    for s in (f"{i:064x}" for i in range(6)):
        try:
            with scratch.registry() as (fd, key):
                scratch.load(fd, key, s)
        except (OSError, ValueError, KeyError):
            broken += 1
    print("BEHAVIOR_RESULT=" + json.dumps({
        "states": sorted(r["state"] for r in scratch.records(True)) if not broken else None, "broken": broken,
        "pin": sorted(browser.read_pin()) if not broken else None,
        "old": sum(e["signature"] == hmac.digest(old, scratch.encoded(e["data"]), "sha256").hex() for e in envs),
        "mode": os.stat(A / "signing.secret").st_mode & 0o777, "size": os.stat(A / "signing.secret").st_size,
        "folder": os.stat(A).st_mode & 0o777,
        "extra": sorted(p.name for p in A.iterdir() if p.name in ("signing.secret.next", "rotation.marker"))}))
