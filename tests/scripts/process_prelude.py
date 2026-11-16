
import atexit, os, signal, subprocess, threading
from unittest import mock
from arete import capture, cli, dashboard, serve, util

children = []
outputs = {}


def stop(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(10)


def cleanup():
    for p in reversed(children):
        stop(p)


atexit.register(cleanup)


def child(body, env=None):
    output = root / f"child-{len(children)}.txt"
    code = f"import sys;sys.path.insert(0,{str(paths.PACKAGE_DIR.parent)!r});" + body
    with output.open("w") as stream:
        p = subprocess.Popen([sys.executable, "-I", "-u", "-c", code], env=env,
                             stdout=stream, stderr=subprocess.STDOUT)
    children.append(p)
    outputs[p] = output
    return p


def wait_output(p, text):
    deadline = time.monotonic() + 30
    while True:
        output = outputs[p].read_text()
        if text in output:
            return output
        assert p.poll() is None and time.monotonic() < deadline, output
        time.sleep(0.02)


def dashboard_start():
    p = child('from arete import cli, dashboard; '
              'dashboard.Tailscale.node = lambda *a: None; cli.main(["dashboard"])')
    try:
        wait_output(p, "cannot listen yet")
    finally:
        stop(p)


def refused():
    built = root / "server-constructed"
    p = child('from pathlib import Path; from arete import cli, serve; '
              f'serve.Server = lambda: Path({str(built)!r}).write_text("constructed Server"); '
              'cli.main(["serve", "--no-dashboard"])')
    assert p.wait(30) == 1, outputs[p].read_text()
    output = outputs[p].read_text()
    assert not built.exists(), output
    assert str(paths.STATE_DIR) in output, output
    return output
