from __future__ import annotations

import contextvars
import fnmatch
import json
import math
import os
import re
import selectors
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import environment, paths, sandbox, scratch, util

if TYPE_CHECKING:
    from . import guard

MAX_REQUEST = 65536
MAX_OUTPUT = 32768
MAX_SECONDS = 120
CREDENTIAL = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL", re.IGNORECASE)


def rules_match(command: str, rules: tuple[str, ...]) -> bool:
    return "Bash" in rules or any(fnmatch.fnmatchcase(command, rule[5:-1]) for rule in rules
                                  if rule.startswith("Bash(") and rule.endswith(")"))


def simple_command(command: str) -> bool:
    if "$(" in command or "`" in command:
        return False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
    lexer.commenters = ""
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        return all(not word or any(c not in ";&|()<>\n" for c in word) for word in lexer)
    except ValueError:
        return False


SYSTEM_READ_ROOTS = tuple(Path(name) for name in (
    "/System", "/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/share",
    "/private/etc", "/Library/Apple", "/Library/Developer/CommandLineTools", "/dev/fd",
    *sandbox.WRITABLE_DEVICES,
))


def runtime_roots() -> tuple[Path, ...]:
    roots = [paths.PACKAGE_DIR, Path(sys.prefix), Path(sys.base_prefix), Path(sys._base_executable).parent.parent]
    for name in ("node", "uv"):
        if found := shutil.which(name):
            binary = Path(found).resolve()
            roots.extend((Path(found), binary.parent.parent if name == "node" and binary.parent.name == "bin" else binary))
    return tuple(dict.fromkeys(roots))


def command_env(passing, extra, hidden=()) -> dict[str, str]:
    source = environment.build(passing or [], extra)
    return {name: value for name, value in source.items()
            if not CREDENTIAL.search(name) and name not in environment.SHELL_HOOKS
            and not name.upper().startswith(environment.LOADER_HOOKS)
            and not name.startswith(("ARETE_PI_", "PI_", "CLAUDE", "CODEX"))
            and not any(secret in value for secret in hidden)}


@dataclass(frozen=True)
class Shell:
    cwd: Path
    add_dirs: tuple[Path, ...]
    floor: guard.Floor
    allowed: tuple[str, ...]
    denied: tuple[str, ...]
    env: dict[str, str]
    hidden: tuple[Path, ...]
    judge: bool
    deadline: float

    def aliases(self, roots: tuple[Path, ...], stopped: threading.Event, until: float) -> tuple[Path, ...]:
        linked = []

        def failed(error):
            raise error

        def inspect(path):
            if stopped.is_set():
                raise ValueError("shell stopped")
            if time.monotonic() >= until:
                raise ValueError("shell timed out")
            try:
                item = path.lstat()
            except FileNotFoundError:
                return
            # Path exclusions alone cannot protect other names for the same inode.
            if stat.S_ISREG(item.st_mode) and item.st_nlink > 1:
                linked.append(path)

        for root in roots:
            if root.is_file():
                inspect(root)
                continue
            for base, _dirs, files in os.walk(root, onerror=failed):
                if stopped.is_set():
                    raise ValueError("shell stopped")
                if time.monotonic() >= until:
                    raise ValueError("shell timed out")
                for name in files:
                    inspect(Path(base, name))
        return tuple(linked)

    def profile(self, stopped: threading.Event, until: float) -> list[str]:
        closed = tuple(path for path in self.floor.closed if path not in (paths.STATE_DIR, paths.WORKTREES_DIR))
        runtimes = runtime_roots()
        profile = sandbox.write_confinement(
            self.cwd, list(self.add_dirs), agent_roots=False, secrets_denied=True, unix_confined=True,
            state=(paths.STATE_DIR, self.floor.open), peers=paths.WORKTREES_DIR, private_temporary=True,
            read_roots=(*SYSTEM_READ_ROOTS, *runtimes),
            hidden=(*closed, *self.floor.files, *self.hidden,
                    *self.aliases((self.cwd, *self.add_dirs, *runtimes), stopped, until)))
        profile[-1] += ("(deny file-link)(deny process-info*)(allow process-info* (target self) (target children))"
                        '(deny sysctl-read (sysctl-name-prefix "kern.proc"))'
                        "(deny signal)(allow signal (target self) (target children))")
        return profile

    def execute(self, request: dict, stopped: threading.Event) -> dict:
        from . import codex, guard

        if type(request) is not dict or request.keys() - {"command", "timeout"}:
            raise ValueError("shell accepts only command and timeout")
        command, timeout = request.get("command"), request.get("timeout", MAX_SECONDS)
        if not isinstance(command, str) or not command.strip() or len(command.encode()) > MAX_REQUEST:
            raise ValueError("shell needs a bounded nonempty command")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_SECONDS:
            raise ValueError(f"shell timeout must be positive and at most {MAX_SECONDS} seconds")
        if (not rules_match(command, self.allowed) or rules_match(command, self.denied)
                or ("Bash" not in self.allowed and not simple_command(command))):
            raise ValueError("command is outside this step's Bash permissions; patterned grants need one simple command")
        if why := codex.git_write(command, list(self.denied)):
            raise ValueError(f"Arete owns publication and git mutation: {why}")
        if why := guard.decide({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.cwd)},
                               self.judge, self.floor):
            raise ValueError(why)
        until = min(self.deadline, time.monotonic() + timeout)
        if stopped.is_set() or until <= time.monotonic():
            raise ValueError("shell invocation stopped or its deadline expired")
        profile = self.profile(stopped, until)
        if stopped.is_set() or time.monotonic() >= until:
            raise ValueError("shell invocation stopped or its deadline expired")
        proc = scratch.spawn([*profile, "/bin/bash", "--noprofile", "--norc", "-c", command],
                             cwd=self.cwd, env={**self.env, **dict.fromkeys(("TMPDIR", "TMP", "TEMP"), str(self.cwd))},
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, start_new_session=True, actor_confined=True)
        output, reason = bytearray(), ""
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if stopped.is_set() or time.monotonic() >= until:
                        reason = "shell stopped" if stopped.is_set() else "shell timed out"
                        break
                    for key, _ in selector.select(0.05):
                        data = os.read(key.fileobj.fileno(), 8192)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(data)
                        if len(output) > MAX_OUTPUT:
                            del output[MAX_OUTPUT:]
                            reason = f"shell output exceeded {MAX_OUTPUT} bytes; command stopped"
                            break
                    if reason:
                        break
                if not reason:
                    while proc.poll() is None and not stopped.is_set() and time.monotonic() < until:
                        stopped.wait(0.05)
                    if proc.poll() is None:
                        reason = "shell stopped" if stopped.is_set() else "shell timed out"
        finally:
            util.kill_group(proc)
            proc.wait()
            proc.stdout.close()
        return {"output": output.decode(errors="replace"), "exit_code": proc.returncode, "error": reason}


@contextmanager
def channel(shell: Shell | None, env: dict):
    if shell is None:
        yield ()
        return
    if not sandbox.can_confine(False):
        raise ValueError(sandbox.NO_CONFINEMENT_REASON)
    parent, child = socket.socketpair()
    stopped = threading.Event()
    context = contextvars.copy_context()

    def receive():
        try:
            with parent.makefile("rb") as source:
                while not stopped.is_set():
                    raw = source.readline(MAX_REQUEST + 1)
                    if not raw:
                        break
                    if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
                        break
                    try:
                        reply = shell.execute(json.loads(raw), stopped)
                    except Exception as error:
                        reply = {"output": "", "exit_code": -1, "error": str(error)[:4096]}
                    parent.sendall(json.dumps(reply).encode() + b"\n")
        except (OSError, ValueError):
            pass
        finally:
            stopped.set()

    thread = threading.Thread(target=lambda: context.run(receive), daemon=True)
    thread.start()
    env["ARETE_PI_SHELL_FD"] = str(child.fileno())
    try:
        yield (child.fileno(),)
    finally:
        stopped.set()
        child.close()
        with suppress(OSError):
            parent.shutdown(socket.SHUT_RDWR)
        thread.join()
        parent.close()
        env.pop("ARETE_PI_SHELL_FD", None)
