from __future__ import annotations

import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import MISSING, asdict, fields, is_dataclass
from math import inf
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
        self.report = (0, {})
        seconds = request["overrides"]["timeout_minutes"] * 60
        self.deadline = time.monotonic() + seconds if seconds else inf
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
                observed = {"type": str(event.get("type") or "progress"),
                            "excerpt": json.dumps(event, ensure_ascii=False)[:1200]}
                self.progress.append(observed)
                if reports_progress(event):
                    self.report = (self.report[0] + 1, observed)
        self.progress = self.progress[-8:]
        return self.progress

    def close(self) -> None:
        self.channel.close()
        util.stop_group(self.proc)
        if util.group_alive(self.proc):
            raise RuntimeError("worker producer group did not become quiescent")


def reports_progress(event: dict) -> bool:
    kind = event.get("type")
    if kind == "progress":
        return True
    if kind == "item.completed":
        item = event.get("item")
        return isinstance(item, dict) and item.get("type") == "agent_message" and bool(item.get("text"))
    message = event.get("message")
    return (isinstance(message, dict)
            and (kind == "assistant" or (kind == "message_end" and message.get("role") == "assistant"))
            and any(block.get("type") == "text" and block.get("text")
                    for block in record.blocks(message.get("content"))))


class PolicyChanged(ValueError):
    def __init__(self, message: str = "worker policy changed after action acceptance"):
        super().__init__(message)


def at_default(f, value) -> bool:
    if f.default is not MISSING:
        return value == f.default
    return f.default_factory is not MISSING and value == f.default_factory()


def explicit(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: explicit(getattr(value, f.name)) for f in fields(value)
                if not at_default(f, getattr(value, f.name))}
    if isinstance(value, (list, tuple)):
        return [explicit(v) for v in value]
    if isinstance(value, dict):
        return {k: explicit(v) for k, v in value.items()}
    return value


def settings_digest(data) -> str:
    return digest(json.loads(json.dumps(data, default=str)))


def worker_settings_digest(value, serialize) -> str:
    data = serialize(value)
    if isinstance(value, config.Config):
        for name in ("max_parallel", "max_parallel_per_repo"):
            data.pop(name, None)
        if "jev" in data:
            for name in ("risk_hold", "irreversible_hold", "secrets_hold"):
                data["jev"].pop(name, None)
            if not data["jev"]:
                del data["jev"]
    return settings_digest(data)


def settings_sha(value) -> str:
    return worker_settings_digest(value, explicit)


def settings_shas(value) -> set[str]:
    return {settings_sha(value), worker_settings_digest(value, asdict)}


def legacy_settings_shas(cfg: config.Config) -> set[str]:
    return {settings_digest(explicit(cfg)), settings_digest(asdict(cfg))}


def request(job, action: str, prompt: str, setting, tools: tuple, rules: list[str], session=NO_SESSION) -> dict:
    return {"run": job.run.id, "repo": job.run.repo.name, "action": action, "step": f"worker-{action}",
            "cwd": str(job.ws), "layer_dir": str(job.layer.dir), "prompt": prompt, "role": setting.step,
            "overrides": {"runner": setting.runner, "provider": setting.provider, "model": setting.model,
                          "effort": setting.effort, "tools": list(tools), "mcp": [],
                          "timeout_minutes": setting.timeout_minutes,
                          "session": "resume" if session.id else "fresh"},
            "rules": rules, "kind": job.run.kind, "session": asdict(session),
            "closed_context": job.run.closed_context,
            "excluded_worktrees": [str(wt) for wt in sorted(getattr(job.run, "excluded_worktrees", ()))],
            "config_sha": settings_sha(job.cfg), "layer_sha": settings_sha(job.layer)}


def check_policy(launch: dict, cfg: config.Config, layer: config.RepoLayer) -> None:
    config_sha = launch["config_sha"]
    if ((config_sha not in settings_shas(cfg) and config_sha not in legacy_settings_shas(cfg))
            or launch["layer_sha"] not in settings_shas(layer)):
        raise PolicyChanged()


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
        check_policy(launch, cfg, layer)
        run = pipeline.AgentRun(launch["run"], repo, paths.RUNS_DIR / launch["run"], cfg=cfg, layer=layer,
                                kind=launch["kind"], step_overrides={launch["role"]: launch["overrides"]},
                                closed_context=tuple(launch["closed_context"]),
                                excluded_worktrees={Path(wt) for wt in launch.get("excluded_worktrees", ())})
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
    except PolicyChanged as error:
        send(channel, {"event": "error", "error": record.failure(error), "recovery": {"reason": "engine-crash"}})
    except Exception as error:
        send(channel, {"event": "error", "error": record.failure(error)})
    finally:
        closing.set()
        channel.close()
