import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[2]

SSH = r'''
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(__file__).resolve().parent.parent
script = sys.argv[-1]
role = 'attach' if 'offset=' in script else 'launch' if 'set -m' in script else 'control'
def log(**kw):
    with (root / 'ssh.jsonl').open('a') as f:
        f.write(json.dumps(kw) + '\n')
if (root / 'refuse').exists():
    print('ssh: connect to host box.test port 22: Connection refused', file=sys.stderr)
    sys.exit(255)
upload_mode = (root / 'upload-mode').read_text().strip() if (root / 'upload-mode').exists() else ''
if role == 'launch' and upload_mode == 'slow':
    chunks = []
    while chunk := sys.stdin.buffer.read(65536):
        chunks.append(chunk)
        time.sleep(.2)
    data = b''.join(chunks)
else:
    data = sys.stdin.buffer.read()
if role == 'launch' and upload_mode == 'truncate':
    data = data[:len(data) // 2]
read, write = os.pipe()
server = os.fork()
if server == 0:
    os.close(read)
    os.setsid()
    log(role=role, half='server', pid=os.getpid(), client=os.getppid(), script=script)
    shell_file = root / 'command-shell'
    shell = shell_file.read_text().strip() if shell_file.exists() else 'sh'
    proc = subprocess.Popen([shell, '-c', script], cwd=root / 'device', stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    proc.stdin.write(data)
    proc.stdin.close()
    while True:
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            break
        try:
            os.write(write, chunk)
        except BrokenPipeError:
            pass
    code = proc.wait()
    os.close(write)
    os._exit(code)
os.close(write)
log(role=role, half='client', pid=os.getpid(), server=server, owner=os.getppid())
while True:
    chunk = os.read(read, 65536)
    if not chunk:
        break
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
_, status = os.waitpid(server, 0)
sys.exit(os.waitstatus_to_exitcode(status))
'''

CLAUDE = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(__file__).resolve().parent.parent
early = (root / 'early').exists()
resist = (root / 'resist').exists()
sid = sys.argv[sys.argv.index('--session-id') + 1] if '--session-id' in sys.argv else 'resumed'
if resist:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if not (root / 'local').exists():
    published = [int(p.read_text()) for p in (root / 'device').rglob('pgid')]
    assert os.getpgrp() in published, (os.getpid(), os.getpgrp(), published)
    tool = subprocess.Popen(['sleep', '300'])
    with (root / 'agents.jsonl').open('a') as f:
        f.write(json.dumps({'pid': os.getpid(), 'tool': tool.pid, 'pgid': os.getpgrp(), 'session': sid}) + '\n')
(root / 'prompt.txt').write_text(sys.stdin.read())
(root / 'cwd.txt').write_text(os.getcwd())
if (root / 'check-cwd').exists():
    Path('remote-product-file').write_text('kept')
if (root / 'answered').exists():
    settings = json.loads((root / 'answered').read_text())
    event = {'type': 'result', 'session_id': sid, 'total_cost_usd': 1.25, 'result': 'complete'}
    if settings.get('structured', True):
        event['structured_output'] = {'answer': 'complete'}
    print(json.dumps(event), flush=True)
    while True:
        if settings.get('active'):
            print(json.dumps({'type': 'system', 'subtype': 'status'}), flush=True)
        time.sleep(.1)
for i in range(1, 41):
    print(json.dumps({'type': 'assistant', 'message': {'id': 'msg-' + str(i),
          'model': 'claude-sonnet-4-6', 'content': [{'type': 'text', 'text': 'turn ' + str(i)}],
          'usage': {'input_tokens': 100, 'output_tokens': 100}}}), flush=True)
    if i == 5 and early:
        os._exit(0)
    if i == 10 and (root / 'gate').exists():
        while not (root / 'released').exists():
            time.sleep(.01)
    time.sleep(.1)
print(json.dumps({'type': 'result', 'session_id': sid, 'total_cost_usd': 1.25,
                 'result': 'done', 'num_turns': 40}), flush=True)
'''


def forbid(*a, **kw):
    raise AssertionError("offline fixture attempted an account or network read")


def wait_for(test, seconds=10):
    deadline = time.monotonic() + seconds
    while not test():
        assert time.monotonic() < deadline, "fixture condition did not arrive"
        time.sleep(.05)


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Fixture:
    def __init__(self, root):
        from arete import accounts, child, config, device_turns, devices, gh
        from arete.store import db

        self.root = root
        self.observations = []
        (root / "config/config.toml").write_text("")
        urllib.request.urlopen = accounts.read = gh.gh = forbid
        device_turns.WINDOW_SECONDS = 3
        device_turns.HEARTBEAT_SECONDS = 1
        child.STOP_GRACE_SECONDS = 1
        db.migrate()
        for name in ("bin", "device"):
            (root / name).mkdir()
        for name, code in (("ssh", SSH), ("claude", CLAUDE)):
            path = root / "bin" / name
            path.write_text(f"#!{sys.executable}\n" + code)
            path.chmod(0o755)
        os.environ["PATH"] = str(root / "bin") + os.pathsep + os.environ["PATH"]
        self.box = devices.Device("box", "ssh", host="dev@box.test", dir=str(root / "device"))
        self.cfg = config.Config()
        self.lines = []
        self.faulted = False

    def agents(self):
        return rows(self.root / "agents.jsonl")

    def gone(self, pgid):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        return False

    def assert_gone(self):
        for agent in self.agents():
            wait_for(lambda: self.gone(agent["pgid"]), 8)
            self.observations.append({**agent, "group_probe": "ProcessLookupError"})

    def fault(self, mode):
        self.faulted_at = time.monotonic()
        connections = rows(self.root / "ssh.jsonl")
        clients = [r for r in connections if r["role"] == "attach" and r["half"] == "client"]
        assert clients, connections
        target = clients[-1]
        if mode == "lost-folder":
            for pgid in (self.root / "device/.arete/turns").glob("*/*/pgid"):
                shutil.rmtree(pgid.parent)
        else:
            if mode in ("half-open", "lost-refused"):
                (self.root / "refuse").touch()
            os.kill(target["pid"], signal.SIGKILL)
            if mode != "half-open":
                os.kill(target["server"], signal.SIGKILL)
        self.observations.append({"fault": mode, "target": target, "at": time.monotonic()})
        (self.root / "released").touch()

    def line(self, line, mode=""):
        self.lines.append(line)
        with contextlib.suppress(ValueError):
            event = json.loads(line)
            if event.get("type") == "assistant":
                n = int(event["message"]["content"][0]["text"].split()[-1])
                if n == 10 and mode and not self.faulted:
                    self.faulted = True
                    self.fault(mode)
                if n == 5 and mode == "stop":
                    from arete import record
                    raise record.Stopped()

    def claude(self, mode="", **kw):
        from arete import claude, record

        with record.Recorder("direct", "agent", {"runner": "claude", "model": "claude-sonnet-4-6"}, ()) as rec:
            self.record_path = rec.path

            def line(text):
                rec.line(text)
                self.line(text, mode)

            result = claude._run("fixture prompt", self.box.dir, remote=self.box, on_line=line,
                                 timeout=kw.pop("timeout", 20), **kw)
            rec.finish(result, rec.elapsed())
            return result

    def saved(self, lines=None):
        from arete import record
        raw = (list(record.lines(self.record_path)) if hasattr(self, "record_path") else
               [{"arete": "header", "runner": "claude"}] + [json.loads(s) for s in (lines or self.lines)])
        (self.root / "raw.jsonl").write_text("".join(json.dumps(s) + "\n" for s in raw))
        normalized = list(record.normalized(raw))
        (self.root / "normalized.json").write_text(json.dumps(normalized, indent=2))
        return normalized

    def runner(self, mode):
        from arete import claude, devices, paths, record, runner
        from arete.store import runs

        rid = record.start("session", run_id=record.new_id("fixture-" + mode))
        real = claude._run

        def observed(*a, **kw):
            callback = kw["on_line"]

            def line(text):
                callback(text)
                self.line(text, mode if mode.startswith("lost-") else "")

            kw["on_line"] = line
            return real(*a, **kw)

        with devices.lease(self.box, rid) as box, mock.patch.object(claude, "_run", observed):
            result = runner.run("fixture prompt", self.box.dir, runner="claude", model="claude-sonnet-4-6",
                                cfg=self.cfg, device=box, trace=(rid, "agent"), timeout=20,
                                remaining_tokens=(lambda: 1000) if mode == "cut" else None,
                                fallback=False, ask_jev=False)
        step_rows = runs.steps(rid)
        self.record_path = next(p for p in paths.record_dir(rid).glob("*.jsonl.gz") if p.name != record.TIMELINE)
        (self.root / "steps.json").write_text(json.dumps(step_rows, indent=2))
        assert not result.ok, result
        if mode == "cut":
            assert "stopped at the 1000 tokens" in result.error, result
        assert step_rows and step_rows[-1]["tokens_in"] > 0 and step_rows[-1]["tokens_out"] > 0, step_rows
        record.settle(rid, "failed")
        if mode.startswith("lost-"):
            assert result.session.id == self.agents()[-1]["session"], result
            assert any(e["kind"] == "lost" for e in self.saved())
            assert step_rows[-1]["tokens_out"] == 1000, step_rows
        return vars(result)

    def cleanup(self):
        for agent in self.agents():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(agent["pgid"], signal.SIGKILL)
        for r in rows(self.root / "ssh.jsonl"):
            if r["half"] == "server":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(r["pid"], signal.SIGKILL)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(r["pid"], signal.SIGKILL)


def reset(f, mode):
    (f.root / "gate").touch()
    result = f.claude(mode)
    if mode == "reset":
        assert result.ok and result.session.spent_usd == 1.25 and result.session.id == f.agents()[-1]["session"], result
        turns = [json.loads(s) for s in f.lines if json.loads(s).get("type") == "assistant"]
        assert [e["message"]["id"] for e in turns] == [f"msg-{i}" for i in range(1, 41)]
        markers = [e for e in f.saved() if e["kind"] == "attach"]
        expected = sum(len(s.encode()) for s in f.lines[:10])
        assert len(markers) == 1 and markers[0]["offset"] == expected, markers
        launches = [r for r in rows(f.root / "ssh.jsonl") if r["role"] == "launch" and r["half"] == "client"]
        assert len(launches) == 1
    else:
        assert not result.ok
        assert any(e["kind"] == "lost" for e in f.saved())
    f.assert_gone()
    if mode == "half-open":
        assert time.monotonic() - f.faulted_at <= 8
        heartbeats = [r for r in rows(f.root / "ssh.jsonl")
                      if r["half"] == "server" and "&& printf" in r["script"]]
        clients = {r["pid"]: r for r in rows(f.root / "ssh.jsonl") if r["half"] == "client"}
        assert all(clients[r["client"]]["owner"] == os.getpid() for r in heartbeats)
        f.observations.append({"heartbeat_writers": heartbeats})
    return {"ok": result.ok, "session": vars(result.session), "markers": [e for e in f.saved() if e["kind"] in ("attach", "lost")]}


def stops(f, mode):
    results = []
    for early in (False, True):
        agents_before = len(f.agents())
        if early:
            (f.root / "early").touch()
        if mode == "stop":
            from arete import record
            try:
                result = f.claude("stop")
            except record.Stopped:
                result = "Stopped"
            assert result == "Stopped", result
        elif mode == "timeout":
            result = f.claude(timeout=2)
            assert not result.ok
            if not early:
                assert "timed out" in result.error, result
        else:
            result = f.runner("cut")
        assert len(f.agents()) == agents_before + 1, "each variant must launch its own agent"
        f.assert_gone()
        results.append({"early": early, "result": str(result)})
    return results


def loop(f, reset_connection=True):
    from arete import loop_run
    if reset_connection:
        (f.root / "gate").touch()
    callbacks = []

    def line(text):
        callbacks.append(text)
        if text == "n10" and reset_connection:
            f.fault("reset")

    command = 'cat; echo "$FIXTURE_ENV"; echo stderr >&2; for i in $(seq 1 30); do echo n$i; '
    command += f'if test "$i" = 10 && test -f {f.root}/gate; then while ! test -f {f.root}/released; do sleep .05; done; fi; '
    command += 'sleep .1; done'
    output = f.root / "loop.txt"
    code, text = loop_run.run_command(["sh", "-c", command], f.box.dir, timeout=60,
                                      env={"FIXTURE_ENV": "environment"}, stdin="input events\n", device=f.box,
                                      output=output, on_line=line)
    assert code == 0, (code, text)
    assert [s for s in text.splitlines() if s.startswith("n")] == [f"n{i}" for i in range(1, 31)], text
    assert text.splitlines() == callbacks and output.read_text() == text
    assert all(s in text for s in ("input events", "environment", "stderr"))
    return {"code": code, "output": str(output), "callbacks": callbacks}


def cleanup(f):
    from arete import devices, record
    assert f.claude().ok
    loop(f, False)
    assert not list((f.root / "device/.arete/turns").glob("*/*"))
    rid = record.start("loop", run_id="cleanup")
    logs = []
    with devices.lease(f.box, rid, logs.append) as box:
        base = f.root / "device/.arete/turns" / box.turns
        base.mkdir(parents=True, exist_ok=True)
        (base / "leftover").mkdir()
    assert not base.exists()
    with devices.lease(f.box, rid, logs.append):
        (f.root / "refuse").touch()
    assert logs and "cleanup" in logs[-1], logs
    forbidden = "END_WITH_" + "SESSION"
    assert not [p for p in (SOURCE / "arete").rglob("*.py") if forbidden in p.read_text() or "devices.wrap(" in p.read_text()]
    record.settle(rid, "done")
    f.assert_gone()
    return {"no_turn_folders": True, "lease_base_removed": True, "cleanup_logs": logs}


def bytes_case(f):
    from arete import agent, claude, device_turns
    marks, lines = [], []
    command = f"import os,time;os.write(1,'é\\n'.encode());os.write(1,b'\\xe2');time.sleep(.5);os.write(1,b'\\x82\\xacpartial');time.sleep(2);os.write(1,b'\\nlast')"

    def line(text):
        lines.append(text)
        if len(lines) == 1:
            f.fault("reset")

    result = device_turns.run(f.box, [sys.executable, "-c", command], f.box.dir, {}, "", timeout=20,
                              on_line=line, on_mark=lambda *a: marks.append(a), leaf="bytes")
    assert result.stdout == "é\n€partial\nlast" and lines == ["é\n", "€partial\n", "last"], (lines, result)
    assert marks == [("attach", 3)], marks
    result = device_turns.run(f.box, ["sh", "-c", "echo detail >&2; exit 7"], f.box.dir, {}, "", timeout=20,
                              on_line=lines.append, on_mark=lambda *a: None, leaf="exit")
    assert result.returncode == 7 and result.stderr == "detail\n", result
    (f.root / "refuse").touch()
    resume = agent.Session("original", 0, "claude")
    refused = claude._run("test", f.box.dir, remote=f.box, resume=resume)
    assert not refused.ok and refused.session == resume
    return {"lines": lines, "marks": marks, "stderr": result.stderr, "returncode": result.returncode,
            "resume": vars(refused.session)}


def contract(f):
    from arete import device_turns, paths, record, ssh
    from arete.store import db, migrations
    from tests.scripts.store_contract import historical_chain, ledger

    other = f.root / "pre-upgrade/arete.db"
    other.parent.mkdir()
    chain = migrations.load()
    old = historical_chain(f.root, "HEAD", [m.name for m in chain if m.n < device_turns.CONTRACT])
    with mock.patch.object(paths, "DB_FILE", other):
        with mock.patch.object(migrations, "FOLDER", old):
            db.migrate()
        original = ledger(db)
        holders = []
        try:
            for kind in ("pipeline", "loop", "session"):
                proc = subprocess.Popen(["sleep", "300"])
                holders.append(proc)
                record.start(kind, run_id="old-" + kind, pid=proc.pid)
            with db.db() as conn:
                conn.execute("UPDATE runs SET schema_version = CASE WHEN kind='session' THEN NULL ELSE 9 END")
            assert db.migrate()
            try:
                device_turns.run(f.box, ["true"], f.box.dir, {}, "", timeout=10,
                                on_line=lambda s: None, on_mark=lambda *a: None, leaf="blocked")
            except ssh.DeviceError as e:
                assert "contract" in str(e)
            else:
                raise AssertionError("remote turn launched while contract waits")
            assert ledger(db) == original
        finally:
            for proc in holders:
                proc.terminate()
                proc.wait()
        assert not db.migrate()
        upgraded = ledger(db)
        assert upgraded[:-1] == original and upgraded[-1][0] == device_turns.CONTRACT
        db.migrate()
        assert ledger(db) == upgraded
        assert device_turns.run(f.box, ["true"], f.box.dir, {}, "", timeout=10,
                                on_line=lambda s: None, on_mark=lambda *a: None, leaf="allowed").returncode == 0
        db._CHECKED.clear()
        with mock.patch.object(db, "LATEST", 9):
            try:
                with db.db():
                    pass
            except db.IncompatibleSchema:
                pass
            else:
                raise AssertionError("obsolete writer accepted")
    return {"old_ledger": original, "new_ledger": upgraded, "applied_once": True, "obsolete_writer_refused": True}


def watchdog(f):
    results = []
    for variant in ("ordinary", "resistant", "early-resistant", "deleted"):
        if variant != "ordinary":
            (f.root / "resist").touch()
        if variant == "early-resistant":
            (f.root / "early").touch()
        else:
            (f.root / "early").unlink(missing_ok=True)
        before = len(f.agents())
        code = f"import sys;sys.path.insert(0,{str(SOURCE)!r});from arete import child,device_turns,devices;child.STOP_GRACE_SECONDS=1;device_turns.WINDOW_SECONDS=3;device_turns.HEARTBEAT_SECONDS=1;device_turns.run(devices.Device('box','ssh',host='dev@box.test',dir={f.box.dir!r}),['claude'],{f.box.dir!r},{{}},'',timeout=100,on_line=lambda s:None,on_mark=lambda *a:None,leaf={variant!r})"
        proc = subprocess.Popen([sys.executable, "-I", "-c", code])
        try:
            wait_for(lambda: len(f.agents()) > before)
            proc.kill()
            proc.wait()
            if variant == "deleted":
                for path in (f.root / "device/.arete/turns").glob("*/*"):
                    shutil.rmtree(path)
            f.assert_gone()
            results.append({"variant": variant, "orchestrator": proc.pid, "gone": True})
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
    return results


def local(f):
    from arete import claude, codex, devices, loop_run, pi, routing, util
    (f.root / "local").touch()
    result = claude._run("local prompt", f.box.dir, timeout=10)
    assert result.ok and result.session.spent_usd == 1.25, result
    code, text = loop_run.run_command(["sh", "-c", 'cat; echo "$VALUE"; echo err >&2'], f.box.dir,
                                      timeout=10, env={"VALUE": "env"}, stdin="in\n", device=devices.LOCAL)
    assert code == 0 and text == "in\nenv\nerr\n", (code, text)
    code, text = loop_run.run_command(["sleep", "10"], f.box.dir, timeout=.1, env={}, device=devices.LOCAL)
    assert code == util.TIMED_OUT and "stopped" in text
    for which in (pi.run, codex._run):
        result = which("test", f.box.dir, remote=f.box)
        assert not result.ok
    box = devices.Device("exe", "exe", exe_sandbox=True)
    assert routing.device_refusal("", "claude", box)
    assert not (f.root / "device/.arete/turns").exists()
    return {"local_io": True, "local_timeout": True, "remote_refusals": True}


def groups(f):
    from arete import agent

    shell = shutil.which("dash") or "sh"
    (f.root / "command-shell").write_text(shell)
    launcher = f.root / "bin/setsid"
    launcher.write_text(f"#!{sys.executable}\nimport os,sys\nos.setsid()\nos.execvp(sys.argv[1],sys.argv[1:])\n")
    launcher.chmod(0o755)
    results = {}
    for mode in ("stop", "timeout", "cut"):
        (f.root / "early").unlink(missing_ok=True)
        results[mode] = stops(f, mode)
    (f.root / "early").unlink(missing_ok=True)
    results["watchdog"] = watchdog(f)
    (f.root / "early").unlink(missing_ok=True)
    before = len(f.agents())
    launcher.write_text(f"#!{sys.executable}\nimport os,sys\nos.execvp(sys.argv[1],sys.argv[1:])\n")
    resume = agent.Session("original", 0, "claude")
    refused = f.claude(resume=resume)
    assert not refused.ok and refused.session == resume and len(f.agents()) == before
    return {"shell": shell, "verified_groups": f.agents(), "cases": results, "unsafe_launcher_refused": True}


def relative(f):
    from dataclasses import replace

    from arete import claude, devices, loop_run, record

    (f.root / "check-cwd").touch()
    login = f.root / "device"
    evidence = []
    for directory in ("work", "space work"):
        (login / directory).mkdir()
        box = replace(f.box, dir=directory)
        for cwd in (directory, "."):
            expected = (login / cwd).resolve()
            code, text = loop_run.run_command(["sh", "-c", 'pwd -P; cat; echo "$VALUE"; echo kept > loop-product'],
                                              cwd, timeout=20, env={"VALUE": "env"}, stdin="input\n", device=box)
            assert code == 0 and text == f"{expected}\ninput\nenv\n", (code, text, expected)
            result = claude._run("relative prompt", cwd, remote=box, timeout=20)
            assert result.ok, result
            assert (f.root / "cwd.txt").read_text() == str(expected)
            assert (expected / "remote-product-file").read_text() == "kept"
            assert (expected / "loop-product").read_text() == "kept\n"
            assert not list((login / directory / ".arete/turns").glob("*/*"))
            evidence.append({"dir": directory, "cwd": cwd, "actual": str(expected), "cleaned": True})
        rid = record.start("session", run_id=record.new_id("relative"))
        with devices.lease(box, rid) as leased:
            base = login / directory / ".arete/turns" / leased.turns
            base.mkdir(parents=True)
            (base / "leftover").write_text("discard")
        assert not base.exists()
        record.settle(rid, "done")
    f.assert_gone()
    return evidence


def answered(f):
    from arete import child, loop_run, util

    results = []
    for active in (False, True):
        (f.root / "answered").write_text(json.dumps({"active": active}))
        with mock.patch.object(child, "ANSWERED_QUIET_SECONDS", .3 if not active else 60):
            started = time.monotonic()
            result = f.claude(timeout=20 if not active else 3)
        assert result.ok and result.structured == {"answer": "complete"}, result
        assert result.session.id == f.agents()[-1]["session"] and result.cost_usd == 1.25
        f.assert_gone()
        assert not list((f.root / "device/.arete/turns").glob("*/*"))
        elapsed = time.monotonic() - started
        assert active or elapsed < 10, elapsed
        results.append({"active": active, "elapsed": elapsed, "result": vars(result)})
    (f.root / "answered").write_text(json.dumps({"structured": False}))
    result = f.claude(timeout=3)
    assert not result.ok and "timed out" in result.error
    f.assert_gone()
    command = 'echo \'{"type":"result","structured_output":{"answer":"command"}}\'; sleep 10'
    code, text = loop_run.run_command(["sh", "-c", command], f.box.dir, timeout=2, env={}, device=f.box)
    assert code == util.TIMED_OUT and "stopped" in text, (code, text)
    return {"settled": results, "unstructured_timed_out": True, "command_timed_out": True}


def upload(f):
    from arete import agent, claude, loop_run, ssh, util

    prompt = "é" * (1024 * 1024)
    expected = prompt.encode()
    (f.root / "upload-mode").write_text("slow")
    started = time.monotonic()
    result = claude._run(prompt, f.box.dir, remote=f.box, timeout=40)
    assert result.ok and (f.root / "prompt.txt").read_bytes() == expected, result
    elapsed = time.monotonic() - started
    assert elapsed > 5, elapsed
    f.assert_gone()
    received = f.root / "loop-input"
    command = ["sh", "-c", f'cat > "{received}"; echo complete']
    code, text = loop_run.run_command(command, f.box.dir, timeout=40, env={}, stdin=prompt, device=f.box)
    assert code == 0 and text == "complete\n" and received.read_bytes() == expected, (code, text)
    received.unlink()
    before = len(f.agents())
    (f.root / "upload-mode").write_text("truncate")
    resume = agent.Session("original", 0, "claude")
    result = claude._run(prompt, f.box.dir, remote=f.box, timeout=20, resume=resume)
    assert not result.ok and result.session == resume and len(f.agents()) == before, result
    code, text = loop_run.run_command(command, f.box.dir, timeout=20, env={}, stdin=prompt, device=f.box)
    assert code == ssh.SSH_FAILED and not received.exists(), (code, text)
    partials = list((f.root / "device/.arete/turns").glob("*/*/in"))
    assert len(partials) == 2 and all(len(p.read_bytes()) == len(expected) // 2 for p in partials)
    assert all(not (p.parent / "pgid").exists() for p in partials)
    (f.root / "upload-mode").write_text("slow")
    result = claude._run(prompt, f.box.dir, remote=f.box, timeout=2)
    assert not result.ok and "timed out after 2s" in result.error and len(f.agents()) == before, result
    code, text = loop_run.run_command(command, f.box.dir, timeout=2, env={}, stdin=prompt, device=f.box)
    assert code == util.TIMED_OUT and not received.exists(), (code, text)
    return {"bytes": len(expected), "slow_claude_elapsed": elapsed, "slow_command_complete": True,
            "truncated_requests_refused": len(partials), "upload_deadlines_preserved": True, "agents": f.agents()}


def main():
    root, mode = Path(sys.argv[1]), sys.argv[2]
    fixture = Fixture(root)
    try:
        if mode in ("reset", "half-open"):
            result = reset(fixture, mode)
        elif mode in ("stop", "timeout", "cut"):
            result = stops(fixture, mode)
        elif mode.startswith("lost-"):
            (root / "gate").touch()
            result = fixture.runner(mode)
            fixture.assert_gone()
        else:
            result = {"loop": loop, "cleanup": cleanup, "bytes": bytes_case, "contract": contract,
                      "watchdog": watchdog, "local": local, "groups": groups, "relative": relative,
                      "answered": answered, "upload": upload}[mode](fixture)
        print("BEHAVIOR_RESULT=" + json.dumps({"mode": mode, "result": result,
                                               "observations": fixture.observations}, default=str))
    finally:
        fixture.cleanup()


if __name__ == "__main__":
    main()
