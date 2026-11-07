from __future__ import annotations

import os
import plistlib
import shutil
from pathlib import Path

from . import config, paths, tracker, util

AGENTS_DIR = paths.HOME / "Library" / "LaunchAgents"

JOBS = {
    "heartbeat": {"args": ["heartbeat", "--quiet"], "interval": 3600, "run_at_load": True},
    "poll": {"args": ["poll"], "interval": 300, "abandon_children": True, "process_type": "Standard"},
    "mine": {"args": ["mine"], "calendar": {"Hour": 23, "Minute": 50}},
    "audit": {"args": ["audit"], "calendar": {"Hour": 4, "Minute": 30}},
    "eval": {"args": ["eval"], "calendar": {"Weekday": 0, "Hour": 2, "Minute": 10}, "process_type": "Standard"},
    "triage": {"args": ["triage", "--refresh"], "interval": 3600},
    "dashboard": {"args": ["dashboard"], "run_at_load": True, "keep_alive": True},
}


def label(name: str) -> str:
    return f"com.arete.{name}"


def nvm_node_bin() -> str | None:
    current = shutil.which("node")
    if current and ".nvm" in current:
        return str(Path(current).parent)
    installed = sorted((paths.HOME / ".nvm" / "versions" / "node").glob("v*"), key=lambda p: p.name)
    return str(installed[-1] / "bin") if installed else None


def build_path() -> str:
    parts = [str(paths.HOME / ".local" / "bin")]
    if node_bin := nvm_node_bin():
        parts.append(node_bin)
    parts += ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    seen, out = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return ":".join(out)


def plist_for(name: str, spec: dict) -> dict:
    exe = shutil.which("arete") or str(paths.HOME / ".local" / "bin" / "arete")
    log = str(paths.LOG_DIR / f"{name}.log")
    pl = {
        "Label": label(name),
        "ProgramArguments": [exe, *spec["args"]],
        "EnvironmentVariables": {"PATH": build_path(), "HOME": str(paths.HOME), "LANG": "en_US.UTF-8"},
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "WorkingDirectory": str(paths.HOME),
        "ProcessType": spec.get("process_type", "Background"),
    }
    if "interval" in spec:
        pl["StartInterval"] = spec["interval"]
    if "calendar" in spec:
        pl["StartCalendarInterval"] = spec["calendar"]
    if spec.get("run_at_load"):
        pl["RunAtLoad"] = True
    if spec.get("abandon_children"):
        pl["AbandonProcessGroup"] = True
    if spec.get("keep_alive"):
        pl["KeepAlive"] = True
        pl["ThrottleInterval"] = 10
    return pl


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(only: list[str] | None = None, dry_run: bool = False) -> int:
    paths.ensure_dirs()
    names = only or list(JOBS)
    unknown = [n for n in names if n not in JOBS]
    if unknown:
        print(f"unknown jobs: {', '.join(unknown)}; known: {', '.join(JOBS)}")
        return 1
    for name in names:
        pl = plist_for(name, JOBS[name])
        dest = AGENTS_DIR / f"{pl['Label']}.plist"
        if dry_run:
            print(f"--- {dest}")
            print(plistlib.dumps(pl).decode())
            continue
        AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        util.sh(["launchctl", "bootout", f"{_domain()}/{pl['Label']}"], timeout=30)
        dest.write_bytes(plistlib.dumps(pl))
        res = util.sh(["launchctl", "bootstrap", _domain(), str(dest)], timeout=30)
        status = "loaded" if res.returncode == 0 else f"bootstrap failed: {util.first_line(res.stderr)}"
        print(f"{pl['Label']}: {status}")
    if not dry_run:
        prepare_trackers()
    return 0


def prepare_trackers() -> None:
    cfg = config.load()
    for repo in cfg.repos:
        if repo.pipeline and repo.tracker == "github":
            errors = tracker.for_repo(cfg, repo).prepare()
            print(f"{repo.github}: " + ("; ".join(errors) if errors else f"labels ready, add `{tracker.LABEL}` to approve"))


def uninstall() -> int:
    for name in JOBS:
        lb = label(name)
        util.sh(["launchctl", "bootout", f"{_domain()}/{lb}"], timeout=30)
        f = AGENTS_DIR / f"{lb}.plist"
        if f.exists():
            f.unlink()
        print(f"{lb}: removed")
    return 0


def rotate_logs(max_bytes: int = 5_000_000) -> None:
    for f in paths.LOG_DIR.glob("*.log"):
        try:
            if f.stat().st_size > max_bytes:
                f.replace(f.with_suffix(".log.1"))
        except OSError:
            pass
