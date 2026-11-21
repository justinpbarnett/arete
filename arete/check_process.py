import ctypes
import os
import signal
import socket
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from contextlib import contextmanager, suppress

POLL_SECONDS = 0.02 if sys.platform == "darwin" else 0.1
PR_SET_CHILD_SUBREAPER = 36
PROC_PIDTBSDINFO = 3
SZOMB = 5


class BSDProcessInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32), ("status", ctypes.c_uint32), ("exit_status", ctypes.c_uint32),
        ("pid", ctypes.c_uint32), ("parent", ctypes.c_uint32), ("user_and_group", ctypes.c_uint32 * 7),
        ("names", ctypes.c_char * 48), ("files", ctypes.c_uint32), ("group", ctypes.c_uint32),
        ("terminal_and_nice", ctypes.c_uint32 * 4), ("seconds", ctypes.c_uint64), ("microseconds", ctypes.c_uint64),
    ]

    @property
    def started(self):
        return self.seconds, self.microseconds


class DarwinProcesses:
    def __init__(self):
        self.lib = ctypes.CDLL(None)
        self.lib.proc_pidinfo.argtypes = (
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.POINTER(BSDProcessInfo), ctypes.c_int)

    def info(self, pid):
        info = BSDProcessInfo()
        size = ctypes.sizeof(info)
        if self.lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), size) == size:
            return info
        return None

    def listed(self, query, pid):
        count = 16
        while True:
            pids = (ctypes.c_int * count)()
            found = query(pid, pids, ctypes.sizeof(pids))
            if found < count:
                return pids[:max(0, found)]
            count *= 2

    def members(self, group, known):
        pending = [(pid, None, None) for pid in self.listed(self.lib.proc_listpgrppids, group)]
        pending.extend((pid, started, None) for pid, started in known.items())
        found = {}
        while pending:
            pid, started, parent = pending.pop()
            if pid in found or (info := self.info(pid)) is None or info.status == SZOMB:
                continue
            if started is not None:
                matches = info.started == started
            else:
                matches = info.group == group if parent is None else info.parent == parent
            if matches:
                found[pid] = info.started
                pending.extend((child, None, pid) for child in self.listed(self.lib.proc_listchildpids, pid))
        return found


@contextmanager
def suite(leases):
    watcher, owner = socket.socketpair()
    with watcher, owner:
        yield (*leases, watcher.fileno())
        owner.send(b"done")


@contextmanager
def launch(words, timeout, suite_fds):
    reader, cancel = socket.socketpair()
    with reader, cancel:
        fds = (*suite_fds, reader.fileno())
        command = [sys.executable, "-I", __file__, str(timeout), ",".join(map(str, fds)), *words]
        yield command, fds, cancel


def peek(channel):
    try:
        return channel.recv(1, socket.MSG_PEEK)
    except BlockingIOError:
        return None


def posix_members(group, known, adopted_by):
    output = subprocess.check_output(["/bin/ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,lstart="],
                                     text=True, timeout=1, stderr=subprocess.DEVNULL)
    live, children, pending = {}, defaultdict(list), []
    for line in output.splitlines():
        pid, parent, pgid, state, started = line.split(maxsplit=4)
        pid, parent, pgid = int(pid), int(parent), int(pgid)
        if state.startswith("Z"):
            continue
        live[pid] = started
        children[parent].append(pid)
        if pgid == group or known.get(pid) == started or parent == adopted_by:
            pending.append(pid)
    found = {}
    while pending:
        pid = pending.pop()
        if pid not in found:
            found[pid] = live[pid]
            pending.extend(children[pid])
    return found


def reap_adopted():
    with suppress(ChildProcessError):
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass


def supervise(command, timeout, owner, cancel, complete):
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    owner.setblocking(False)
    cancel.setblocking(False)
    adopted_by = (os.getpid() if sys.platform == "linux"
                  and ctypes.CDLL(None).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0 else None)
    if sys.platform == "darwin":
        members = DarwinProcesses().members
    else:
        def members(group, known):
            return posix_members(group, known, adopted_by)
    deadline = time.monotonic() + timeout if timeout is not None else float("inf")
    try:
        proc = subprocess.Popen(command, start_new_session=True)
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        complete(127, False)
        return
    known = {}
    timed_out = reported = False
    while True:
        try:
            known = members(proc.pid, known)
            code = proc.poll()
            if code is not None and not reported:
                known = members(proc.pid, known)
            inspected = True
        except (OSError, subprocess.SubprocessError):
            code = proc.poll()
            inspected = False
        if code is not None:
            known.pop(proc.pid, None)
            if adopted_by is not None:
                reap_adopted()
        lifetime = peek(owner)
        expired = time.monotonic() >= deadline
        if expired and (code is None or lifetime == b""):
            timed_out = True
        stopping = stopping or bool(peek(cancel)) or timed_out
        if stopping:
            if code is None and proc.pid not in known:
                proc.kill()
            for pid in known if inspected else ():
                with suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        if code is not None and not reported:
            if timed_out:
                print(f"\n[arete] timed out after {timeout}s", file=sys.stderr)
            complete(124 if timed_out else code, not inspected or bool(known))
            reported = True
        if reported and ((inspected and not known) or (lifetime and not stopping)):
            return
        time.sleep(POLL_SECONDS)


def run(command, timeout, fds):
    status, finished = os.pipe()
    monitor = os.fork()
    if monitor == 0:
        os.setsid()
        os.close(status)

        def complete(code, watching):
            with suppress(BrokenPipeError):
                os.write(finished, f"{code} {int(watching)}".encode())
            os.close(finished)
            with open(os.devnull, "wb") as quiet:
                os.dup2(quiet.fileno(), 1)
                os.dup2(quiet.fileno(), 2)

        try:
            with socket.socket(fileno=fds[-2]) as owner, socket.socket(fileno=fds[-1]) as cancel:
                supervise(command, timeout, owner, cancel, complete)
        except Exception:
            traceback.print_exc()
        finally:
            os._exit(0)

    def forward(signum, _frame):
        with suppress(ProcessLookupError):
            os.kill(monitor, signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    os.close(finished)
    for fd in fds:
        os.close(fd)
    with os.fdopen(status, "rb") as receipt:
        code, watching = map(int, receipt.read().split() or (1, 0))
    if not watching:
        os.waitpid(monitor, 0)
    if code < 0:
        signum = -code
        with suppress(OSError, ValueError):
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    return code


if __name__ == "__main__":
    timeout, fds, *command = sys.argv[1:]
    sys.exit(run(command, None if timeout == "None" else float(timeout), tuple(map(int, fds.split(",")))))
