import ctypes
import os
import select
import signal
import subprocess
import sys
from contextlib import closing, suppress
from functools import cache

POSIX_SPAWN_SETPGROUP = 0x0002
NOTE_EXITSTATUS = 0x04000000
MONITOR_NAME = "verification producer monitor"


@cache
def responsibility_lookup():
    lookup = ctypes.CDLL(None).responsibility_get_uniqueid_responsible_for_pid
    lookup.argtypes = (ctypes.c_int,)
    lookup.restype = ctypes.c_int64
    return lookup


def responsible_identity(pid):
    try:
        return responsibility_lookup()(pid)
    except AttributeError as error:
        raise OSError("verification process responsibility is unavailable") from error


def spawn_monitor(args):
    lib = ctypes.CDLL(None)
    attributes = ctypes.c_void_p()
    attr = ctypes.byref(attributes)
    pid = ctypes.c_int()
    argv = (ctypes.c_char_p * (len(args) + 1))(*map(os.fsencode, args))
    env = (ctypes.c_char_p * (len(os.environb) + 1))(
        *(name + b"=" + value for name, value in os.environb.items()))
    error = lib.posix_spawnattr_init(attr)
    if error:
        raise OSError(error, os.strerror(error))
    try:
        discard = lib.responsibility_spawnattrs_setdisclaim
        discard.argtypes = (ctypes.POINTER(ctypes.c_void_p), ctypes.c_int)
        for error in (lib.posix_spawnattr_setflags(attr, POSIX_SPAWN_SETPGROUP),
                      lib.posix_spawnattr_setpgroup(attr, 0), discard(attr, 1)):
            if error:
                raise OSError(error, os.strerror(error))
        error = lib.posix_spawn(ctypes.byref(pid), argv[0], None, attr, argv, env)
        if error:
            raise OSError(error, os.strerror(error))
    finally:
        lib.posix_spawnattr_destroy(attr)
    return pid.value


def watch_exit(queue, pid, status=False):
    queue.control([select.kevent(pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                fflags=select.KQ_NOTE_EXIT | (NOTE_EXITSTATUS if status else 0))], 0)


def wait_for_command(group, command):
    with closing(select.kqueue()) as queue:
        watch_exit(queue, group, status=True)
        with subprocess.Popen(command, process_group=group, close_fds=False) as proc:
            try:
                watch_exit(queue, proc.pid)
            except ProcessLookupError:
                return proc.wait()
            event = queue.control(None, 1)[0]
            if event.ident == group:
                proc.send_signal(os.WTERMSIG(event.data) if os.WIFSIGNALED(event.data) else signal.SIGKILL)
            return proc.wait()


def monitor(group, gate, status, passed, command):
    if os.read(gate, 1) != b"1":
        return 125
    os.close(gate)
    os.set_inheritable(status, False)
    try:
        code = wait_for_command(group, command)
    except OSError as error:
        print(f"{MONITOR_NAME}: {error}", file=sys.stderr)
        code = 125
    for fd in (*passed, 0, 1, 2):
        os.close(fd)
    os.chdir("/")
    with suppress(BrokenPipeError):
        os.write(status, code.to_bytes(2, "little", signed=True))
    os.close(status)
    os.execve(sys.executable, [sys.executable, "-I", "-c", "import signal; signal.pause()"], {})


def detached_monitor(args):
    result, report = os.pipe()
    try:
        launcher = os.fork()
        if launcher == 0:
            try:
                monitor_pid = spawn_monitor(args)
                os.write(report, str(monitor_pid).encode())
            except (OSError, AttributeError) as error:
                print(f"{MONITOR_NAME}: {error}", file=sys.stderr, flush=True)
                os._exit(125)
            os._exit(0)
        os.waitpid(launcher, 0)
    finally:
        os.close(report)
    with os.fdopen(result, "rb") as source:
        return source.read()


def relay(gate, registration, passed, command):
    result, status = os.pipe()
    os.set_inheritable(status, True)
    group = os.getpid()
    os.set_inheritable(registration, False)
    monitor_pid = detached_monitor([sys.executable, "-I", __file__, "monitor", str(group), str(gate), str(status),
                                    passed, *command])
    os.close(status)
    os.close(gate)
    os.write(registration, monitor_pid)
    for fd in (*descriptors(passed), registration, 0, 1, 2):
        os.close(fd)
    finished = os.read(result, 2)
    os.close(result)
    if not finished:
        return 125
    code = int.from_bytes(finished, "little", signed=True)
    if code < 0:
        signum = -code
        if signum != signal.SIGKILL:
            signal.signal(signum, signal.SIG_DFL)
        os.kill(group, signum)
    return code


def descriptors(passed):
    return map(int, passed.split(",")) if passed else ()


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    if mode == "monitor":
        group, gate, status, passed, *command = args
        sys.exit(monitor(int(group), int(gate), int(status), descriptors(passed), command))
    gate, registration, passed, *command = args
    sys.exit(relay(int(gate), int(registration), passed, command))
