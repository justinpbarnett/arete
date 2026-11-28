from __future__ import annotations

import contextlib
import json
from dataclasses import asdict, replace
import os
import re
import selectors
import shlex
import subprocess
import threading
import time
import uuid
from collections.abc import Callable

from . import child, scratch, ssh
from .store import db, factory

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
        self.output = ""


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
    from . import environment

    proc = scratch.spawn([*ssh.ssh_argv(device), script], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment.build(()))
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


def launch_script(path, argv, cwd, env, timeout, merge_stderr, input_size, retain=True, fence=""):
    admitted = f"test ! -f {fence} || exit 1" if fence else ":"
    if any(not ENV_NAME.fullmatch(k) for k in env):
        raise ValueError("invalid remote environment name")
    command = shlex.join(["env", *(f"{k}={v}" for k, v in env.items()), *argv])
    grace = max(0, int(child.STOP_GRACE_SECONDS))
    job = f"""SECONDS=0
trap '' HUP
trap ':' TERM
turn=$1
working=$2
waiting=0
while ! test -s "$turn/owner"; do
  test -d "$turn" && test "$waiting" -lt 40 || exit 1
  sleep .1; waiting=$((waiting + 1))
done
group=$(ps -o pgid= -p $$) || exit 1
set -- $group
if test "$#" -ne 1 || test "$1" -ne "$$" || test "$(cat "$turn/owner")" != "$$"; then
  echo 'remote turn launcher did not create a separate process group' >> "$turn/err.log"
  exit 1
fi
echo "$$" > "$turn/pgid.tmp" && mv "$turn/pgid.tmp" "$turn/pgid" || exit 1
(
  while ! test -s "$turn/watchdog"; do
    test -d "$turn" && test "$SECONDS" -lt {min(WINDOW_SECONDS, max(1, int(timeout)))} || exit 1
    sleep .1
  done
  test ! -f "$turn/stop" || exit 1
  {admitted}
  cd "$working" && {command} < "$turn/in" >> "$turn/out" {'2>&1' if merge_stderr else '2>> "$turn/err.log"'}
  code=$?; echo "$code" > "$turn/exit.tmp" && mv "$turn/exit.tmp" "$turn/exit"
) &
echo ready > "$turn/watchdog"
last_beat=$SECONDS; previous=0
while :; do
  reason=
  test -d "$turn" || reason=stop
  test -f "$turn/exit" && break
  test -f "$turn/stop" && reason=stop
  current=$(cat "$turn/heartbeat" 2>/dev/null)
  elapsed=$SECONDS
  if test "$current" != "$previous"; then last_beat=$elapsed; previous=$current; fi
  idle=$((elapsed - last_beat))
  test "$idle" -ge {WINDOW_SECONDS} && reason=heartbeat
  test "$elapsed" -ge {max(1, int(timeout))} && reason=deadline
  if test -n "$reason"; then
    kill -TERM -"$$"
    sleep {grace}
    if test -d "$turn" && ! test -f "$turn/exit"; then
      echo "killed: $reason" > "$turn/exit.tmp" && mv "$turn/exit.tmp" "$turn/exit"
    fi
    {":" if retain else 'test "$reason" != heartbeat || touch "$turn/discard"'}
    break
  fi
  delay=1
  if test "$(({max(1, int(timeout))} - elapsed))" -le {grace + CONTROL_SECONDS + 2} || test "$(({WINDOW_SECONDS} - idle))" -le 1; then delay=.1; fi
  sleep "$delay"
done
touch "$turn/stopping"
kill -KILL -"$$"
"""
    bash_job = ('set -m; ( exec bash -c "$1" bash "$2" "$3" ) & '
                'worker=$!; echo "$worker" > "$2/owner"; wait "$worker"')
    return f"""umask 077
{admitted}
working=$(cd {ssh.remote_dir(cwd)} && pwd -P) || exit 1
mkdir -p -m 0700 "$(dirname {path})" || exit 1
mkdir -m 0700 {path} || exit 2
turn=$(cd {path} && pwd -P) || exit 1
cd "$turn" || exit 1
{":" if retain else "trap 'rm -rf \"$turn\"' 0"}
cat > in || exit 1
size=$(wc -c < in) || exit 1
if test "$size" -ne {input_size}; then
  echo 'remote turn input was truncated' >&2
  exit 1
fi
: > out; : > err.log; echo 0 > heartbeat
(
  trap '' HUP
  if ! command -v bash >/dev/null 2>&1; then
    echo 'remote turns need bash to measure elapsed time' >> err.log
    exit 1
  fi
  if command -v setsid >/dev/null 2>&1; then
    setsid bash -c {shlex.quote(job)} bash "$turn" "$working" &
    worker=$!
    echo "$worker" > owner
  elif command -v bash >/dev/null 2>&1; then
    bash -c {shlex.quote(bash_job)} bash {shlex.quote(job)} "$turn" "$working" &
    worker=$!
  else
    echo 'remote turns need setsid or bash to create a process group' >> err.log
    exit 1
  fi
  wait "$worker"
  if test -f stopping || ! test -s pgid; then
    : > stopped
    {":" if retain else 'rm -f in'}
    if test -f discard; then cd /; rm -rf "$turn"; fi
  fi
) </dev/null >/dev/null 2>&1 &
trap - 0
tries=0
while ! test -s watchdog; do
  if test -f stopped || test "$tries" -ge 40; then
    touch stop
    cat err.log >&2
    exit 1
  fi
  sleep .1; tries=$((tries + 1))
done
printf 'started: %s\\n' "$(cat pgid)"
"""


def attach_script(path, offset):
    return f"""turn=$(cd {path} && pwd -P) || exit 3
offset={offset}
printf 'attached\\n'
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


def cleanup_script(path, *, stop=False, discard=False, base=False, remove=True):
    ticks = (max(0, int(child.STOP_GRACE_SECONDS)) + CONTROL_SECONDS) * 10
    return f"""test -d {path} || exit 0
root=$(cd {path} && pwd -P) || exit 1
{'set -- "$root"/*' if base else 'set -- "$root"'}
for turn do
  test -d "$turn" || continue
  {('touch "$turn/stop" || exit 1') if stop else ':'}
  {('touch "$turn/discard" || exit 1') if discard else ':'}
done
tries=0
for turn do
  test -d "$turn" || continue
  while test -s "$turn/pgid" && ! test -f "$turn/stopped"; do
    test -d "$turn" || break
    test "$tries" -lt {ticks} || exit 1
    sleep .1; tries=$((tries + 1))
  done
done
{'cd / && rm -rf "$root"' if remove else ':'}
"""


def cleanup(device, path, *, stop=False, discard=False, base=False, remove=True, retry=True, deadline=None):
    duration = max(0, int(child.STOP_GRACE_SECONDS)) + CONTROL_SECONDS + 2
    end = time.monotonic() + duration
    if deadline is not None:
        end = min(end, deadline)
    while True:
        try:
            result = control(device, cleanup_script(path, stop=stop, discard=discard, base=base, remove=remove),
                             timeout=max(.01, end - time.monotonic()))
            if result.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
        if not retry or time.monotonic() >= end:
            raise RuntimeError("remote turn cleanup was not acknowledged")
        time.sleep(min(.1, max(0, end - time.monotonic())))


def finish(device, path, timeout=CONTROL_SECONDS):
    return control(device, f"turn=$(cd {path} && pwd -P) || exit 3; cd \"$turn\" || exit 3; "
                   "test -f stopped && test -f exit || exit 4; cat exit; tail -c 6000 err.log", timeout=timeout)


def saved_device(data):
    return ssh.Device(**{**data, "via": saved_device(data["via"]) if data.get("via") else None})


def stop_fence(device, key):
    return ssh.remote_dir(f"{device.dir.rstrip('/')}/.arete/factory-stops/{component(key)}")


def confirm_factory_stop(process):
    entry = json.loads(process["detail"])
    device = saved_device(entry["device"])
    path = folder(device, entry["leaf"])
    fence = stop_fence(device, process["id"])
    script = (f'umask 077; mkdir -p "$(dirname {fence})" && touch {fence} || exit 1; '
              f"mkdir -p {path} && touch {path}/stop || exit 1; "
              f"test ! -s {path}/pgid || test -f {path}/stopped")
    return control(device, script).returncode == 0


def run(device: ssh.Device, argv: list[str], cwd, env: dict[str, str], stdin: str, *, timeout: float,
        on_line: Callable[[str], None], on_mark: Callable[[str, int], None], leaf: str,
        merge_stderr: bool = False, settle_answer: bool = False) -> subprocess.CompletedProcess:
    if not db.migration_applied(CONTRACT):
        raise ssh.DeviceError("remote turns wait for the remote-turn contract; stop older runs, loops, sessions "
                              "and serve processes, then run arete migrate")
    retain = bool(device.turns)
    leaf = leaf if retain else f"{leaf}-{uuid.uuid4().hex}"
    device = replace(device, turns=base(device))
    path = folder(device, leaf)
    key = f"remote-{uuid.uuid4().hex}"
    with db.tx() as c:
        version = factory.admit(c)
        factory.register(c, key, os.getpid(), "remote", version=version,
                         detail=json.dumps({"device": asdict(device), "leaf": leaf}))
        fence = stop_fence(device, key) if factory.installed(c) else ""
    settled = False
    deadline = time.monotonic() + timeout
    started = False
    attempted = False
    awaiting_status = False
    offset = 0
    output = []
    finished = threading.Event()
    heartbeat = None
    answer = child.Answer()
    pending = b""
    acknowledged = False
    settling = False

    def lost(why):
        on_mark("lost", offset)
        error = Lost(offset, why, started)
        error.output = "".join(output)
        raise error

    def left():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout, output="".join(output))
        return remaining

    def beat():
        counter = 0
        while not finished.wait(HEARTBEAT_SECONDS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            counter += 1
            with contextlib.suppress(Exception):
                control(device, f"test -d {path} && printf '%s\\n' {counter} > {path}/heartbeat",
                        timeout=min(CONTROL_SECONDS, HEARTBEAT_SECONDS, remaining))

    def deliver(data):
        nonlocal offset
        line = data.decode(errors="replace")
        output.append(line)
        offset += len(data)
        if settle_answer:
            answer.saw(line)
        on_line(line)

    def read_status():
        end = min(deadline, time.monotonic() + WINDOW_SECONDS)
        while True:
            try:
                result = finish(device, path, timeout=min(CONTROL_SECONDS, left(),
                                                         max(.01, end - time.monotonic())))
            except (OSError, subprocess.SubprocessError):
                left()
                result = None
            status, _, err = result.stdout.partition("\n") if result else ("", "", "")
            if result and result.returncode == 0 and (status.isdigit() or status.startswith("killed: ")):
                return status, err
            if time.monotonic() >= end:
                lost("lost the device turn exit status")
            time.sleep(min(.1, left(), max(0, end - time.monotonic())))

    try:
        script = launch_script(path, argv, cwd, env, timeout, merge_stderr, len(stdin.encode()), retain=retain,
                               fence=fence)
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
            header = b""
            acknowledged = False
            try:
                from . import environment

                proc = scratch.spawn([*ssh.ssh_argv(device), attach_script(path, offset + len(pending))], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment.build(()))
            except OSError:
                reconnect()
                continue
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = left()
                        if settle_answer and answer.given and (
                                answer.quiet_for() >= child.ANSWERED_QUIET_SECONDS
                                or remaining <= child.STOP_GRACE_SECONDS + CONTROL_SECONDS + 2):
                            settling = True
                            break
                        if broken_since is not None and time.monotonic() - broken_since >= WINDOW_SECONDS:
                            lost("lost the device while attaching to remote turn")
                        if not selector.select(min(.25, left())):
                            continue
                        data = os.read(proc.stdout.fileno(), 65536)
                        if not data:
                            break
                        if not acknowledged:
                            header += data
                            if b"\n" not in header:
                                continue
                            ack, data = header.split(b"\n", 1)
                            if ack != b"attached":
                                break
                            acknowledged = True
                            broken_since = None
                            retries = 0
                        pending += data
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            deliver(line + b"\n")
                code = None if settling else proc.wait(timeout=min(CONTROL_SECONDS, left()))
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                proc.stdout.close()
            if settling:
                if pending:
                    deliver(pending)
                    pending = b""
                break
            if code == 3:
                lost("lost the device turn folder")
            if code == 0 and acknowledged:
                if pending:
                    deliver(pending)
                    pending = b""
                awaiting_status = True
                break
            reconnect()
        finished.set()
        heartbeat.join(max(0, min(CONTROL_SECONDS + 1, deadline - time.monotonic())))
        if settling:
            try:
                cleanup(device, path, stop=True, remove=False, deadline=deadline)
            except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                lost(str(error))
        status, err = read_status()
        try:
            left()
            cleanup(device, path, deadline=deadline)
            left()
        except (OSError, RuntimeError):
            lost("remote turn cleanup was not acknowledged")
        if status == "killed: deadline":
            raise subprocess.TimeoutExpired(argv, timeout, output="".join(output), stderr=err)
        settled = True
        completed = subprocess.CompletedProcess(argv, 0 if settling else int(status) if status.isdigit() else 137,
                                                "".join(output), err)
        completed.started = started
        return completed
    except BaseException as error:
        finished.set()
        if heartbeat:
            heartbeat.join(max(0, min(CONTROL_SECONDS + 1, deadline - time.monotonic())))
        if pending:
            output.append(pending.decode(errors="replace"))
        if isinstance(error, subprocess.TimeoutExpired | Lost):
            error.output = "".join(output)
        if attempted:
            try:
                remove = not retain and not awaiting_status
                cleanup(device, path, stop=True, discard=remove, remove=remove, retry=False, deadline=deadline)
            except (OSError, subprocess.SubprocessError, RuntimeError) as cleanup_error:
                error.add_note(str(cleanup_error))
            else:
                settled = True
        else:
            settled = True
        raise
    finally:
        finished.set()
        if settled:
            with db.tx() as c:
                if not factory.interrupted(key, c):
                    factory.forget(key)
