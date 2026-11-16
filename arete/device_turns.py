from __future__ import annotations

import contextlib
import os
import re
import selectors
import shlex
import subprocess
import threading
import time
from collections.abc import Callable

from . import child, scratch, ssh
from .store import db

HEARTBEAT_SECONDS = 30
BACKOFF = (1, 2, 4, 10)
WINDOW_SECONDS = 600
CONTROL_SECONDS = 5
CONTRACT = 10
COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class Lost(RuntimeError):
    def __init__(self, offset, why, started):
        super().__init__(why)
        self.offset, self.why, self.started = offset, why, started


class Answered(Exception):
    pass


def component(value):
    if not COMPONENT.fullmatch(value):
        raise ValueError(f"invalid remote turn component: {value!r}")
    return value


def base(device):
    return component(device.turns or f"{db.schema_metadata()['instance_tag']}-pid{os.getpid()}")


def folder(device, leaf=""):
    path = f"{device.dir.rstrip('/')}/.arete/turns/{base(device)}"
    return ssh.remote_dir(path + (f"/{component(leaf)}" if leaf else ""))


def control(device, script, stdin="", timeout=CONTROL_SECONDS):
    proc = scratch.spawn([*ssh.ssh_argv(device), script], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(stdin.encode(), timeout=max(.01, timeout))
        return subprocess.CompletedProcess(proc.args, proc.returncode, out.decode(errors="replace"),
                                           err.decode(errors="replace"))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()


def launch_script(path, argv, cwd, env, timeout, merge_stderr, input_size):
    if any(not ENV_NAME.fullmatch(k) for k in env):
        raise ValueError("invalid remote environment name")
    command = shlex.join(["env", *(f"{k}={v}" for k, v in env.items()), *argv])
    grace = max(0, int(child.STOP_GRACE_SECONDS))
    job = f"""trap '' HUP
turn=$1
working=$2
group=$(ps -o pgid= -p $$) || exit 1
set -- $group
if test "$#" -ne 1 || test "$1" -ne "$$"; then
  echo 'remote turn launcher did not create a separate process group' >> "$turn/err.log"
  exit 1
fi
echo "$$" > "$turn/pgid.tmp" && mv "$turn/pgid.tmp" "$turn/pgid" || exit 1
cd "$working" && {command} < "$turn/in" >> "$turn/out" {'2>&1' if merge_stderr else '2>> "$turn/err.log"'}
code=$?; echo "$code" > "$turn/exit.tmp" && mv "$turn/exit.tmp" "$turn/exit"
"""
    bash_job = 'set -m; ( exec sh -c "$1" sh "$2" "$3" ) & wait "$!"'
    return f"""umask 077
working=$(cd {ssh.remote_dir(cwd)} && pwd -P) || exit 1
mkdir -p -m 0700 "$(dirname {path})" || exit 1
mkdir -m 0700 {path} || exit 2
turn=$(cd {path} && pwd -P) || exit 1
cd "$turn" || exit 1
cat > in || exit 1
size=$(wc -c < in) || exit 1
if test "$size" -ne {input_size}; then
  echo 'remote turn input was truncated' >&2
  exit 1
fi
: > out; : > err.log; echo 0 > heartbeat
if command -v setsid >/dev/null 2>&1; then
  setsid sh -c {shlex.quote(job)} sh "$turn" "$working" </dev/null >/dev/null 2>&1 &
elif command -v bash >/dev/null 2>&1; then
  bash -c {shlex.quote(bash_job)} bash {shlex.quote(job)} "$turn" "$working" </dev/null >/dev/null 2>&1 &
else
  echo 'remote turns need setsid or bash to create a process group' >&2
  exit 1
fi
launcher=$!
tries=0
while ! test -s pgid; do
  if ! kill -0 "$launcher" 2>/dev/null || test "$tries" -ge 4; then
    kill -TERM "$launcher" 2>/dev/null
    cat err.log >&2
    exit 1
  fi
  sleep 1; tries=$((tries + 1))
done
pgid=$(cat pgid)
( trap '' HUP
  elapsed=0; idle=0; previous=0
  while :; do
    reason=
    if test -f "$turn/exit"; then kill -KILL -"$pgid" 2>/dev/null; exit; fi
    test -f "$turn/stop" && reason=stop
    test "$elapsed" -ge {max(1, int(timeout))} && reason=deadline
    current=$(cat "$turn/heartbeat" 2>/dev/null)
    if test "$current" != "$previous"; then idle=0; previous=$current; fi
    test "$idle" -ge {WINDOW_SECONDS} && reason=heartbeat
    if test -n "$reason"; then
      kill -TERM -"$pgid" 2>/dev/null
      sleep {grace}
      kill -KILL -"$pgid" 2>/dev/null
      if test -d "$turn" && ! test -f "$turn/exit"; then
        echo "killed: $reason" > "$turn/watchdog.tmp" && mv "$turn/watchdog.tmp" "$turn/exit"
      fi
      exit
    fi
    sleep 1; elapsed=$((elapsed + 1)); idle=$((idle + 1))
  done
) </dev/null >/dev/null 2>&1 &
echo "$!" > watchdog
printf 'started: %s\\n' "$pgid"
"""


def attach_script(path, offset):
    return f"""turn=$(cd {path} && pwd -P) || exit 3
offset={offset}
while :; do
  test -d "$turn" || exit 3
  size=$(wc -c < "$turn/out") || exit 3
  if test "$size" -gt "$offset"; then
    tail -c +$((offset + 1)) "$turn/out" | head -c $((size - offset))
    offset=$size
  fi
  if test -f "$turn/exit"; then
    size=$(wc -c < "$turn/out")
    test "$size" -eq "$offset" && exit 0
  fi
  sleep 1
done
"""


def stop(device, path):
    grace = max(0, int(child.STOP_GRACE_SECONDS))
    with contextlib.suppress(Exception):
        control(device, f"test -d {path} || exit 0; cd {path} || exit 1; "
                f"touch stop; test -s pgid || exit 0; pgid=$(cat pgid); kill -TERM -\"$pgid\" 2>/dev/null; "
                f"sleep {grace}; kill -KILL -\"$pgid\" 2>/dev/null; true",
                timeout=grace + CONTROL_SECONDS)


def finish(device, path, timeout=CONTROL_SECONDS):
    return control(device, f"turn=$(cd {path} && pwd -P) || exit 3; cd \"$turn\" || exit 3; "
                   "cat exit; tail -c 6000 err.log; "
                   "pgid=$(cat pgid); kill -KILL -\"$pgid\" 2>/dev/null; "
                   "watchdog=$(cat watchdog); kill -TERM \"$watchdog\" 2>/dev/null; "
                   'cd /; rm -rf "$turn"', timeout=timeout)


def run(device: ssh.Device, argv: list[str], cwd, env: dict[str, str], stdin: str, *, timeout: float,
        on_line: Callable[[str], None], on_mark: Callable[[str, int], None], leaf: str,
        merge_stderr: bool = False, settle_answer: bool = False) -> subprocess.CompletedProcess:
    if not db.migration_applied(CONTRACT):
        raise ssh.DeviceError("remote turns wait for the remote-turn contract; stop older runs, loops, sessions "
                              "and serve processes, then run arete migrate")
    path = folder(device, leaf)
    deadline = time.monotonic() + timeout
    started = False
    attempted = False
    offset = 0
    output = []
    finished = threading.Event()
    heartbeat = None
    answer = child.Answer()

    def lost(why):
        on_mark("lost", offset)
        raise Lost(offset, why, started)

    def left():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if settle_answer and answer.given:
                raise Answered
            raise subprocess.TimeoutExpired(argv, timeout)
        return remaining

    def beat():
        counter = 0
        while not finished.wait(HEARTBEAT_SECONDS):
            counter += 1
            with contextlib.suppress(Exception):
                control(device, f"test -d {path} && printf '%s\\n' {counter} > {path}/heartbeat",
                        timeout=min(CONTROL_SECONDS, HEARTBEAT_SECONDS))

    def deliver(data):
        nonlocal offset
        line = data.decode(errors="replace")
        output.append(line)
        offset += len(data)
        if settle_answer:
            answer.saw(line)
        on_line(line)

    try:
        script = launch_script(path, argv, cwd, env, timeout, merge_stderr, len(stdin.encode()))
        attempted = True
        try:
            launched = control(device, script, stdin, timeout=left())
        except (OSError, subprocess.SubprocessError):
            left()
            launched = None
        if launched and launched.returncode == 2:
            attempted = False
            raise ssh.DeviceError(f"remote turn already exists: {leaf}")
        started = bool(launched and launched.stdout.startswith("started: "))
        if not started:
            try:
                status = control(device, f"test -s {path}/pgid && cat {path}/pgid", timeout=min(CONTROL_SECONDS, left()))
                started = status.returncode == 0 and status.stdout.strip().isdigit()
            except (OSError, subprocess.SubprocessError):
                left()
            if not started:
                lost("lost the device before remote turn launch was acknowledged")
        heartbeat = threading.Thread(target=beat, daemon=True)
        heartbeat.start()
        broken_since = None
        retries = 0

        def reconnect():
            nonlocal broken_since, retries
            broken_since = broken_since or time.monotonic()
            remaining = WINDOW_SECONDS - (time.monotonic() - broken_since)
            if remaining <= 0:
                lost("lost the device while attaching to remote turn")
            on_mark("attach", offset)
            finished.wait(min(BACKOFF[min(retries, len(BACKOFF) - 1)], remaining, left()))
            retries += 1

        while True:
            pending = b""
            try:
                proc = scratch.spawn([*ssh.ssh_argv(device), attach_script(path, offset)], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                reconnect()
                continue
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ)
                    while True:
                        if settle_answer and answer.given and answer.quiet_for() >= child.ANSWERED_QUIET_SECONDS:
                            raise Answered
                        if broken_since is not None and time.monotonic() - broken_since >= WINDOW_SECONDS:
                            lost("lost the device while attaching to remote turn")
                        if not selector.select(min(.25, left())):
                            continue
                        data = os.read(proc.stdout.fileno(), 65536)
                        if not data:
                            break
                        broken_since = None
                        retries = 0
                        pending += data
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            deliver(line + b"\n")
                code = proc.wait(timeout=min(CONTROL_SECONDS, left()))
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                proc.stdout.close()
            if code == 3:
                lost("lost the device turn folder")
            if code == 0:
                if pending:
                    deliver(pending)
                break
            reconnect()
        finished.set()
        heartbeat.join(CONTROL_SECONDS + 1)
        try:
            result = finish(device, path, timeout=min(CONTROL_SECONDS, left()))
        except (OSError, subprocess.SubprocessError):
            left()
            lost("lost the device while finishing remote turn")
        status, _, err = result.stdout.partition("\n")
        if result.returncode or not (status.isdigit() or status.startswith("killed: ")):
            lost("lost the device turn exit status")
        if status == "killed: deadline":
            raise subprocess.TimeoutExpired(argv, timeout)
        completed = subprocess.CompletedProcess(argv, int(status) if status.isdigit() else 137, "".join(output), err)
        completed.started = started
        return completed
    except Answered:
        finished.set()
        if heartbeat:
            heartbeat.join(CONTROL_SECONDS + 1)
        stop(device, path)
        err = ""
        with contextlib.suppress(Exception):
            result = finish(device, path)
            _, _, err = result.stdout.partition("\n")
        completed = subprocess.CompletedProcess(argv, 0, "".join(output), err)
        completed.started = started
        return completed
    except BaseException:
        finished.set()
        if heartbeat:
            heartbeat.join(CONTROL_SECONDS + 1)
        if attempted:
            stop(device, path)
        raise
    finally:
        finished.set()
