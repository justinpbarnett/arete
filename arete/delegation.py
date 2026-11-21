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
from datetime import date, datetime, time as time_of_day
from hashlib import sha256
from math import inf
from pathlib import Path

from . import child, codebase, config, journeys, paths, pipeline, record, routing, scratch, util
from .agent import NO_SESSION, Session
from .store.orchestration import digest

MAX_MESSAGE = 2 * 1024 * 1024
SETTINGS_TAG = "$type"
SETTINGS_TYPES = {cls.__name__: cls for cls in (
    config.Config, config.RepoLayer, config.Part, config.Repo, config.Job, config.Probe, config.JiraConfig,
    config.TriageConfig, config.JevConfig, config.GithubApp, config.ReplayConfig, config.Environment,
    config.PaidRoutes, config.AskConfig, config.Retention, config.BackupConfig, config.ChatConfig,
    config.OncallConfig, config.OncallSource, config.PatchPolicy, config.Verify, config.Orchestration, journeys.Journey,
)}
SETTINGS_DATES = {cls.__name__: cls for cls in (date, datetime, time_of_day)}


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
        env = {**os.environ, config.SNAPSHOT_PATH: request["snapshot_path"], config.SNAPSHOT_SHA: request["snapshot_sha"]}
        log = run.dir / f"{action}-driver.log"
        try:
            with log.open("ab") as output:
                # The trusted SDK broker applies the runner's sandbox; nesting it prevents native startup on macOS.
                self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, stderr=output,
                                             env=env, pass_fds=(peer.fileno(),), start_new_session=True)
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
    pass


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


def settings_sha(value) -> str:
    return digest(json.loads(json.dumps(explicit(value), default=str)))


def settings_shas(value) -> set[str]:
    return {settings_sha(value), digest(json.loads(json.dumps(asdict(value), default=str)))}


def pack_settings(value):
    if type(value).__name__ in SETTINGS_TYPES:
        data = {f.name: pack_settings(getattr(value, f.name)) for f in fields(value)}
        return {SETTINGS_TAG: type(value).__name__, "value": data}
    if isinstance(value, Path):
        return {SETTINGS_TAG: "path", "value": str(value)}
    if isinstance(value, tuple):
        return {SETTINGS_TAG: "tuple", "value": [pack_settings(item) for item in value]}
    if isinstance(value, list):
        return [pack_settings(item) for item in value]
    if isinstance(value, dict):
        data = {key: pack_settings(item) for key, item in value.items()}
        return {SETTINGS_TAG: "dict", "value": list(data.items())} if SETTINGS_TAG in data else data
    if type(value).__name__ in SETTINGS_DATES:
        return {SETTINGS_TAG: type(value).__name__, "value": value.isoformat()}
    return value


def unpack_settings(data):
    if SETTINGS_TAG not in data:
        return data
    kind, value = data[SETTINGS_TAG], data["value"]
    if kind == "path":
        return Path(value)
    if kind == "tuple":
        return tuple(value)
    if kind == "dict":
        return dict(value)
    if kind in SETTINGS_DATES:
        return SETTINGS_DATES[kind].fromisoformat(value)
    return SETTINGS_TYPES[kind](**value)


def freeze_settings(cfg: config.Config, run: pipeline.AgentRun, layer: config.RepoLayer) -> dict[str, str]:
    if cfg.settings_snapshot is None:
        path = (run.dir / "settings-snapshot.json").absolute()
        # Routing uses the source layer, while step settings can come from the fetched checkout.
        repo = run.repo
        source_layer = layer if layer.dir == repo.layer_dir else repo.configured_layer()
        raw = json.dumps(pack_settings((cfg, layer, source_layer))).encode()
        sha = sha256(raw).hexdigest()
        util.write_bytes_atomic(path, raw)
        cfg.settings_snapshot = path, sha
        repo.pinned_layer = source_layer
    path, sha = cfg.settings_snapshot
    return {"path": str(path), "sha": sha}


def read_snapshot(path: Path, sha: str) -> tuple[config.Config, config.RepoLayer, config.RepoLayer]:
    try:
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != sha:
            raise PolicyChanged("worker policy changed after action acceptance")
        cfg, layer, source_layer = json.loads(raw, object_hook=unpack_settings)
    except PolicyChanged:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise PolicyChanged("worker policy changed after action acceptance") from error
    if not (isinstance(cfg, config.Config) and isinstance(layer, config.RepoLayer)
            and isinstance(source_layer, config.RepoLayer)):
        raise PolicyChanged("worker policy changed after action acceptance")
    cfg.settings_snapshot = path, sha
    for repo in cfg.repos:
        if repo.layer_dir == source_layer.dir:
            repo.pinned_layer = source_layer
    return cfg, layer, source_layer


def request(job, action: str, prompt: str, setting, tools: tuple, rules: list[str], session=NO_SESSION) -> dict:
    snapshot, sha = job.cfg.settings_snapshot or ("", "")
    return {"run": job.run.id, "repo": job.run.repo.name, "action": action, "step": f"worker-{action}",
            "cwd": str(job.ws), "layer_dir": str(job.layer.dir), "prompt": prompt, "role": setting.step,
            "overrides": {"runner": setting.runner, "provider": setting.provider, "model": setting.model,
                          "effort": setting.effort, "tools": list(tools), "mcp": [],
                          "timeout_minutes": setting.timeout_minutes,
                          "session": "resume" if session.id else "fresh"},
            "rules": rules, "kind": job.run.kind, "session": asdict(session),
            "closed_context": job.run.closed_context,
            "config_sha": settings_sha(job.cfg), "layer_sha": settings_sha(job.layer),
            "snapshot_path": str(snapshot), "snapshot_sha": sha}


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
        raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
        if len(raw.rstrip(b"\n")) > MAX_MESSAGE:
            raise ValueError("oversized worker launch request")
        launch = json.loads(raw)
        directory = paths.RUNS_DIR / launch["run"]
        cfg, layer, source_layer = read_snapshot(Path(launch["snapshot_path"]), launch["snapshot_sha"])
        if launch["config_sha"] not in settings_shas(cfg) or launch["layer_sha"] not in settings_shas(layer):
            raise PolicyChanged("worker policy changed after action acceptance")
        repo = cfg.repo(launch["repo"])
        repo.pinned_layer = source_layer
        run = pipeline.AgentRun(launch["run"], repo, directory, cfg=cfg, layer=layer,
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
        with config.using_settings(cfg):
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
