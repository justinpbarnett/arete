from __future__ import annotations

import os
import plistlib
import re
import subprocess
from dataclasses import dataclass

LAUNCHCTL = "/bin/launchctl"
MAX_PID_DOMAINS = 256


class Unreadable(Exception):
    pass


@dataclass(frozen=True)
class Job:
    domain: str
    label: str
    path: str
    pid: int


def printed(target: str) -> str | None:
    try:
        done = subprocess.run([LAUNCHCTL, "print", target], check=False, capture_output=True, text=True, errors="replace",
                              timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


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


def runs(path: str, executable: str) -> bool:
    program = declared_program(path)
    return bool(program and executable) and os.path.realpath(program) == os.path.realpath(executable)


def bundled(executable: str) -> bool:
    return ".xpc/Contents/MacOS/" in executable and sealed(executable)


class Table:
    def __init__(self, uid: int):
        self.uid = uid
        self.prints: dict[str, str | None] = {}

    def text(self, target: str) -> str | None:
        if target not in self.prints:
            self.prints[target] = printed(target)
        return self.prints[target]

    def job(self, pid: int, bundled: bool = False) -> Job | None:
        domains, unreadable, texts = [], False, []
        for domain in (f"gui/{self.uid}", f"user/{self.uid}"):
            text = self.text(domain)
            if text is None or running(text) is None:
                unreadable = True
            else:
                texts.append(text)
                domains.append(domain)
        if bundled:
            extra = list(dict.fromkeys(name for text in texts for name in pid_domains(text)))
            if len(extra) > MAX_PID_DOMAINS:
                raise Unreadable("too many pid domains")
            for name in extra:
                text = self.text(name)
                if text is not None and running(text) is not None:
                    texts.append(text)
                    domains.append(name)
        found = [(domain, label) for domain, text in zip(domains, texts)
                 for label in (running(text) or {}).get(pid, [])]
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
