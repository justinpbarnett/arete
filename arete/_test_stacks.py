import faulthandler
import fcntl
import os
import signal
from _thread import get_ident

PREFIX_ENV = "ARETE_TEST_STACK_PREFIX"
DUMP_SIGNAL = signal.SIGURG
REPORT_START = "=== Test class timeout ==="
RECOVERED_REPORT_START = "=== Recovered test class timeout ==="
REPORT_END = "=== End test class timeout ==="
TIMEOUT_REMEDIATION = (
    "Move the tests this change adds into their own TestCase class, or split the class, "
    "so each class finishes alone well within its deadline. Raise class_timeout only for a class "
    "whose tests are slow on purpose."
)
_stack_fd = None


def _register():
    global _stack_fd
    prefix = os.environ.get(PREFIX_ENV)
    if not prefix:
        return
    path = prefix + str(os.getpid())
    pending = path + ".pending"
    try:
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, f"0x{get_ident():x}\n".encode())
        faulthandler.register(DUMP_SIGNAL, fd, all_threads=True, chain=False)
        try:
            os.replace(pending, path)
        except OSError:
            faulthandler.unregister(DUMP_SIGNAL)
            raise
    except (OSError, RuntimeError, ValueError):
        os.close(fd)
    else:
        _stack_fd = fd


def _reopen_after_fork():
    global _stack_fd
    if _stack_fd is not None:
        faulthandler.unregister(DUMP_SIGNAL)
        os.close(_stack_fd)
        _stack_fd = None
    _register()


def arm():
    if _stack_fd is None:
        _register()
        if _stack_fd is not None:
            os.register_at_fork(after_in_child=_reopen_after_fork)


def bootstrap():
    if os.environ.get(PREFIX_ENV):
        return f"import runpy; runpy.run_path({os.path.abspath(__file__)!r})['arm'](); "
    return ""
