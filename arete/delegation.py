from __future__ import annotations

import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from . import child, codebase, config, paths, pipeline, record, routing, scratch, util
from .agent import NO_SESSION, Session
from .store.orchestration import digest

MAX_MESSAGE = 2 * 1024 * 1024


def identity_of(pid: int) -> int | None:
    if paths.ON_MAC:
        return scratch.identity_of(pid)
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def live(pid: int | None, identity: int | None) -> bool:
    if not pid:
        return False
    if util.pid_alive(pid) and (identity is None or identity_of(pid) == identity):
        return True
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def send(channel: socket.socket, value: dict) -> None:
    data = json.dumps(value, allow_nan=False).encode() + b"\n"
    if len(data) > MAX_MESSAGE:
        raise ValueError("worker control message is too large")
    channel.sendall(data)


class Worker:
    def __init__(self, run: pipeline.AgentRun, action: str, request: dict):
        self.action, self.buffer, self.messages = action, b"", []
        self.progress, self.sources = [], {}
        parent, peer = socket.socketpair()
        self.channel, self.proc = parent, None
        command = [sys.executable, "-I", "-c",
                   (f"import sys; sys.path.insert(0, {str(paths.PACKAGE_DIR.parent)!r}); "
                    "from arete.delegation import main; main()"), str(peer.fileno())]
        log = run.dir / f"{action}-driver.log"
        try:
            with log.open("ab") as output:
                # The trusted SDK broker applies the runner's sandbox; nesting it prevents native startup on macOS.
                self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, stderr=output,
                                             pass_fds=(peer.fileno(),), start_new_session=True)
            self.proc.stdin.write(json.dumps(request).encode())
            self.proc.stdin.close()
            self.pid = self.proc.pid
            self.identity = identity_of(self.pid)
            self.run_id, self.step = run.id, request["step"]
            parent.setblocking(False)
        except BaseException:
            parent.close()
            if self.proc is not None:
                child.stop(self.proc)
            raise
        finally:
            peer.close()

    def receive(self) -> list[dict]:
        while True:
            try:
                data = self.channel.recv(65536)
            except BlockingIOError:
                break
            if not data:
                break
            self.buffer += data
            if len(self.buffer) > MAX_MESSAGE:
                raise ValueError("worker control message exceeds its limit")
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                self.messages.append(json.loads(line))
        found, self.messages = self.messages, []
        return found

    def allow(self, limits: dict | None = None) -> None:
        send(self.channel, {"allow": True, "limits": limits})

    def observations(self) -> list[dict]:
        for path in record.record_files(self.run_id):
            if not path.name.startswith(record.slug(self.step)):
                continue
            source = self.sources.setdefault(path, record.AppendedLines(path))
            for event in record.entries(source.read()):
                if event.get("arete") or event.get("type") in ("ping", "heartbeat"):
                    continue
                self.progress.append({"type": str(event.get("type") or "progress"),
                                      "excerpt": json.dumps(event, ensure_ascii=False)[:1200]})
        self.progress = self.progress[-8:]
        return self.progress

    def close(self) -> None:
        self.channel.close()
        util.stop_group(self.proc)
        if util.group_alive(self.proc):
            raise RuntimeError("worker producer group did not become quiescent")


def settings_sha(value) -> str:
    return digest(json.loads(json.dumps(asdict(value), default=str)))


def request(job, action: str, prompt: str, setting, tools: tuple, rules: list[str], session=NO_SESSION) -> dict:
    return {"run": job.run.id, "repo": job.run.repo.name, "action": action, "step": f"worker-{action}",
            "cwd": str(job.ws), "layer_dir": str(job.layer.dir), "prompt": prompt, "role": setting.step,
            "overrides": {"runner": setting.runner, "provider": setting.provider, "model": setting.model,
                          "effort": setting.effort, "tools": list(tools), "mcp": [],
                          "session": "resume" if session.id else "fresh"},
            "rules": rules, "kind": job.run.kind, "session": asdict(session),
            "closed_context": job.run.closed_context,
            "config_sha": settings_sha(job.cfg), "layer_sha": settings_sha(job.layer)}


def main() -> None:
    codebase.load()
    channel = socket.socket(fileno=int(sys.argv[1]))
    channel.set_inheritable(False)
    inbox, closing = queue.Queue(), threading.Event()

    def died(*_):
        raise pipeline.Stopped()

    signal.signal(signal.SIGTERM, died)

    def listening():
        try:
            with channel.makefile("rb") as source:
                while line := source.readline(MAX_MESSAGE + 1):
                    if len(line) > MAX_MESSAGE:
                        break
                    inbox.put(json.loads(line))
        finally:
            if not closing.is_set():
                os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=listening, daemon=True).start()
    try:
        raw = sys.stdin.buffer.read(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE:
            raise ValueError("oversized worker launch request")
        launch = json.loads(raw)
        cfg = config.load()
        repo = cfg.repo(launch["repo"])
        layer = config.load_layer(Path(launch["layer_dir"]))
        if settings_sha(cfg) != launch["config_sha"] or settings_sha(layer) != launch["layer_sha"]:
            raise ValueError("worker policy changed after action acceptance")
        run = pipeline.AgentRun(launch["run"], repo, paths.RUNS_DIR / launch["run"], cfg=cfg, layer=layer,
                                kind=launch["kind"], step_overrides={launch["role"]: launch["overrides"]},
                                closed_context=tuple(launch["closed_context"]))
        kept = Session(**launch["session"])
        if kept.id:
            run.sessions[config.INHERITS.get(launch["role"], launch["role"])] = kept

        def admission(step, role, route):
            send(channel, {"event": "launch", "step": step, "role": role, "route": route})
            grant = inbox.get()
            if not isinstance(grant, dict) or grant.get("allow") is not True:
                raise ValueError("worker launch was not admitted")
            return grant.get("limits")

        run.launch_gate = admission
        result = run.agent(launch["step"], launch["prompt"], Path(launch["cwd"]), role=launch["role"],
                           allowed_tools=launch["rules"], disallowed_tools=routing.NO_GIT_WRITES,
                           wait_out_limits=False, fresh_prompt=lambda which: launch["prompt"])
        send(channel, {"event": "result", "ok": result.ok, "text": result.text[:65536],
                       "structured": result.structured, "error": result.error[:2000], "session": asdict(result.session),
                       "cost_usd": run.cost, "paid_usd": run.paid,
                       "runner": result.runner, "model": result.model, "effort": result.effort,
                       "record": result.record, "recovery": pipeline.runner.start_failure(result, launch["role"])})
    except pipeline.Stopped:
        pass
    except Exception as error:
        send(channel, {"event": "error", "error": record.failure(error)})
    finally:
        closing.set()
        channel.close()
