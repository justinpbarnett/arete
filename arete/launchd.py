from __future__ import annotations

import ctypes
import os
import plistlib
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property

LAUNCHCTL = "/bin/launchctl"
MAX_PID_DOMAINS = 256
MISSING_DOMAIN = 112
MISSING_SERVICE = 113


class Unreadable(Exception):
    pass


@dataclass(frozen=True)
class Job:
    domain: str
    label: str
    path: str
    pid: int


def printed(target: str, *, strict: bool = False) -> str | None:
    from . import environment

    try:
        done = subprocess.run([LAUNCHCTL, "print", target], check=False, capture_output=True, text=True, errors="replace",
                              timeout=10, env=environment.build(()))
    except (OSError, subprocess.SubprocessError):
        if strict:
            raise
        return None
    if done.returncode == 0:
        return done.stdout
    if strict and done.returncode not in (MISSING_DOMAIN, MISSING_SERVICE):
        done.check_returncode()
    return None


def block(text: str, name: str) -> list[str] | None:
    lines = text.splitlines()
    for start, line in enumerate(lines):
        if line.strip() == f"{name} = {{":
            rows = []
            for row in lines[start + 1:]:
                if row.strip() == "}":
                    return rows
                rows.append(row)
            return rows
    return None


def running(text: str) -> dict[int, list[str]] | None:
    rows = block(text, "services")
    if rows is None:
        return None
    found: dict[int, list[str]] = {}
    for row in rows:
        parts = row.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) > 0:
            found.setdefault(int(parts[0]), []).append(parts[2].strip())
    return found


def listed(text: str) -> list[str] | None:
    rows = block(text, "services")
    if rows is None:
        return None
    found = set()
    for row in rows:
        parts = row.split(None, 2)
        if len(parts) == 3:
            found.add(parts[2].strip())
    return sorted(found)


def labels(uid: int) -> dict[str, list[str]] | None:
    found = {}
    for kind in ("gui", "user"):
        text = printed(f"{kind}/{uid}")
        names = None if text is None else listed(text)
        if names is None:
            return None
        found[kind] = names
    return found


def pid_domains(text: str) -> list[str]:
    return [row.strip() for row in block(text, "subdomains") or [] if re.fullmatch(r"pid/\d+", row.strip())]


def fields(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("\t") and not line.startswith("\t\t") and " = " in line:
            key, value = line.strip().split(" = ", 1)
            found.setdefault(key, value)
    return found


def sealed(path: str) -> bool:
    if not path or not os.path.isabs(path):
        return False
    try:
        real = os.path.realpath(path)
        return (os.stat(real).st_dev == os.stat("/").st_dev
                and bool(os.statvfs(real).f_flag & os.ST_RDONLY))
    except OSError:
        return False


def declared_program(path: str) -> str:
    try:
        if path.endswith(".plist"):
            with open(path, "rb") as handle:
                data = plistlib.load(handle)
            return str(data.get("Program") or data["ProgramArguments"][0])
        if path.endswith(".xpc"):
            with open(os.path.join(path, "Contents", "Info.plist"), "rb") as handle:
                name = plistlib.load(handle)["CFBundleExecutable"]
            return os.path.join(path, "Contents", "MacOS", str(name))
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, plistlib.InvalidFileException):
        return ""
    return ""


def loaded_label(job: Job, labels: Iterable[str]) -> bool:
    if job.label in labels:
        return True
    base, _, instance = job.label.rpartition(".")
    if not base or not instance or not job.path.endswith(".plist"):
        return False
    if base not in labels and not any(label.rpartition(".")[0] == base and not label.endswith(".") for label in labels):
        return False
    try:
        with open(job.path, "rb") as handle:
            return plistlib.load(handle).get("Label") == base
    except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
        return False


def runs(path: str, executable: str) -> bool:
    program = declared_program(path)
    return bool(program and executable) and os.path.realpath(program) == os.path.realpath(executable)


def bundled(executable: str) -> bool:
    return ".xpc/Contents/MacOS/" in executable and sealed(executable)


class Table:
    def __init__(self, uid: int, pids: Iterable[int] = ()):
        self.uid = uid
        self.pids = tuple(pids)
        self.prints: dict[str, str | None] = {}
        self.failed_prints: set[str] = set()

    def text(self, target: str) -> str | None:
        if target not in self.prints:
            if len(self.prints) >= MAX_PID_DOMAINS:
                raise Unreadable("too many launchctl prints")
            try:
                self.prints[target] = printed(target, strict=True)
            except (OSError, subprocess.SubprocessError):
                self.prints[target] = None
                self.failed_prints.add(target)
        return self.prints[target]

    @cached_property
    def responsible_pids(self) -> dict[int, int]:
        try:
            responsible = ctypes.CDLL(None).responsibility_get_pid_responsible_for_pid
        except (AttributeError, OSError):
            return {}
        responsible.argtypes = (ctypes.c_int,)
        responsible.restype = ctypes.c_int
        owners = {}
        for pid in self.pids:
            owner = responsible(pid)
            if owner is not None and owner > 0:
                owners[pid] = owner
        return owners

    def job(self, pid: int, bundled: bool = False) -> Job | None:
        domains, unreadable, texts = [], False, []
        for domain in (f"gui/{self.uid}", f"user/{self.uid}"):
            text = self.text(domain)
            if text is None or running(text) is None:
                unreadable = True
            else:
                texts.append(text)
                domains.append(domain)
        extra = {}
        if bundled:
            extra = dict.fromkeys(name for text in texts for name in pid_domains(text))
            for name in extra:
                text = self.text(name)
                if text is not None and running(text) is not None:
                    texts.append(text)
                    domains.append(name)
                elif text is not None or name in self.failed_prints:
                    unreadable = True
        found = [(domain, label) for domain, text in zip(domains, texts)
                 for label in (running(text) or {}).get(pid, [])]
        if bundled and not found and pid in self.pids:
            owners = self.responsible_pids
            responsible = owners.get(pid)
            if responsible is None:
                raise Unreadable("responsible pid unavailable")
            for other in self.pids:
                domain = f"pid/{other}"
                if domain in extra:
                    continue
                owner = owners.get(other)
                if owner is None:
                    unreadable = True
                elif owner == responsible:
                    text = self.text(domain)
                    if text is not None:
                        services = running(text)
                        if services is None:
                            unreadable = True
                        else:
                            found.extend((domain, label) for label in services.get(pid, []))
                    elif domain in self.failed_prints:
                        unreadable = True
        if len(found) > 1:
            raise Unreadable("pid listed under more than one service")
        if not found:
            if unreadable:
                raise Unreadable("launchd domain unreadable")
            return None
        domain, label = found[0]
        detail = self.text(f"{domain}/{label}")
        values = fields(detail) if detail is not None else {}
        if "path" not in values or values.get("pid") != str(pid):
            raise Unreadable("service print does not match")
        return Job(domain, label, values["path"], pid)
