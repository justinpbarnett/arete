from __future__ import annotations

import os
import plistlib
import re
import shutil
import sys
from pathlib import Path

from . import config, paths, tracker, util

AGENTS_DIR = paths.HOME / "Library" / "LaunchAgents"
UNITS_DIR = paths.HOME / ".config" / "systemd" / "user"
SERVE = "serve"
SERVE_UNIT = "arete-serve.service"
RETIRED = ("heartbeat", "poll", "mine", "audit", "eval", "triage", "dashboard")
PLATFORMS = ("mac", "linux")


def label(name: str) -> str:
    return f"com.arete.{name}"


def nvm_node_bin() -> str | None:
    current = shutil.which("node")
    if current and ".nvm" in current:
        return str(Path(current).parent)
    installed = sorted((paths.HOME / ".nvm" / "versions" / "node").glob("v*"), key=node_version)
    return str(installed[-1] / "bin") if installed else None


def node_version(folder: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", folder.name))


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


def arete_exe() -> str:
    return shutil.which("arete") or str(paths.HOME / ".local" / "bin" / "arete")


def serve_plist() -> dict:
    log = str(paths.LOG_DIR / "serve.log")
    return {
        "Label": label(SERVE),
        "ProgramArguments": [arete_exe(), SERVE],
        "EnvironmentVariables": {"PATH": build_path(), "HOME": str(paths.HOME), "LANG": "en_US.UTF-8"},
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "WorkingDirectory": str(paths.HOME),
        "ProcessType": "Standard",
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "AbandonProcessGroup": True,
    }


def serve_unit() -> str:
    return f"""[Unit]
Description=arete serve: loops, webhooks, sessions and the dashboard
Wants=network-online.target
After=network-online.target

[Service]
ExecStart={arete_exe()} {SERVE}
Restart=always
RestartSec=10
KillMode=process
Environment=PATH={build_path()}
Environment=HOME={paths.HOME}
Environment=LANG=en_US.UTF-8
WorkingDirectory={paths.HOME}

[Install]
WantedBy=default.target
"""


def _domain() -> str:
    return f"gui/{os.getuid()}"


def this_platform() -> str:
    return "mac" if paths.ON_MAC else "linux"


def agent_file(lb: str) -> Path:
    return AGENTS_DIR / f"{lb}.plist"


def serve_file(platform: str) -> Path:
    return agent_file(label(SERVE)) if platform == "mac" else UNITS_DIR / SERVE_UNIT


def serve_installed() -> bool:
    return serve_file(this_platform()).exists()


def retired_labels() -> set[str]:
    return {label(name) for name in RETIRED}


def install(dry_run: bool = False, platform: str = "") -> int:
    platform = platform or this_platform()
    if platform not in PLATFORMS:
        print(f"unknown platform {platform}; use {' or '.join(PLATFORMS)}")
        return 1
    if dry_run:
        print(f"--- {serve_file(platform)}")
        print(plistlib.dumps(serve_plist()).decode() if platform == "mac" else serve_unit(), end="")
        if platform == "mac":
            for lb in (label(name) for name in RETIRED if agent_file(label(name)).exists()):
                print(f"would retire {lb}")
        return 0
    if platform != this_platform():
        print(f"this machine is {this_platform()}; install for {platform} on that machine, or pass --dry-run")
        return 1
    paths.ensure_dirs()
    code = install_launchd() if platform == "mac" else install_systemd()
    prepare_trackers()
    return code


def install_launchd() -> int:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    for name in RETIRED:
        retire(label(name))
    pl = serve_plist()
    dest = serve_file("mac")
    util.sh(["launchctl", "bootout", f"{_domain()}/{pl['Label']}"], timeout=30, keeps=("launch",))
    dest.write_bytes(plistlib.dumps(pl))
    res = util.sh(["launchctl", "bootstrap", _domain(), str(dest)], timeout=30, keeps=("launch",))
    print(f"{pl['Label']}: " + ("loaded" if res.returncode == 0 else f"bootstrap failed: {util.first_line(res.stderr)}"))
    return 0 if res.returncode == 0 else 1


def install_systemd() -> int:
    UNITS_DIR.mkdir(parents=True, exist_ok=True)
    serve_file("linux").write_text(serve_unit())
    for step in (["daemon-reload"], ["enable", "--now", SERVE_UNIT], ["restart", SERVE_UNIT]):
        res = util.sh(["systemctl", "--user", *step], timeout=60)
        if res.returncode != 0:
            print(f"systemctl --user {' '.join(step)} failed: {util.first_line(res.stderr or res.stdout)}")
            return 1
    print(f"{SERVE_UNIT}: enabled and started; `loginctl enable-linger` keeps it running after logout")
    return 0


def remove_agent(lb: str) -> bool:
    if not agent_file(lb).exists():
        return False
    util.sh(["launchctl", "bootout", f"{_domain()}/{lb}"], timeout=30, keeps=("launch",))
    agent_file(lb).unlink()
    return True


def retire(lb: str) -> None:
    if remove_agent(lb):
        print(f"{lb}: retired, its loop now runs under {label(SERVE)}")


def prepare_trackers() -> None:
    cfg = config.load()
    for repo in cfg.repos:
        if repo.pipeline and repo.tracker == "github":
            errors = tracker.for_repo(cfg, repo).prepare()
            print(f"{repo.github}: " + ("; ".join(errors) if errors else f"labels ready, add `{tracker.LABEL}` to approve"))


def uninstall() -> int:
    if not paths.ON_MAC:
        util.sh(["systemctl", "--user", "disable", "--now", SERVE_UNIT], timeout=60)
        serve_file("linux").unlink(missing_ok=True)
        util.sh(["systemctl", "--user", "daemon-reload"], timeout=60)
        print(f"{SERVE_UNIT}: removed")
        return 0
    for lb in (label(name) for name in (SERVE, *RETIRED)):
        if remove_agent(lb):
            print(f"{lb}: removed")
    return 0


SERVE_LOG = "serve.log"


def check_keep(keep: int) -> None:
    if keep < 1:
        raise ValueError(f"keep must be at least 1, not {keep}")


def archive_name(f: Path, n: int) -> Path:
    return f.with_name(f"{f.name}.{n}")


def rotate_by_rename(f: Path, keep: int) -> list[str]:
    step = "shift"
    try:
        for n in range(keep - 1, 0, -1):
            try:
                os.replace(archive_name(f, n), archive_name(f, n + 1))
            except FileNotFoundError:
                continue
        step = "rename"
        os.replace(f, archive_name(f, 1))
    except OSError as e:
        return [f"{f.name}: {step} failed: {e}"]
    return []


def rotate_logs(max_bytes: int = 5_000_000, keep: int = 5) -> list[str]:
    check_keep(keep)
    notes: list[str] = []
    try:
        with util.lock("rotate-logs", wait=False) as held:
            if not held:
                return ["another rotation holds the rotate-logs lock, so no log was rotated"]
            notes = rotate_listed(max_bytes, keep)
    except OSError as e:
        return [*notes, f"rotate-logs.lock: lock failed: {e}"]
    return notes


def rotate_listed(max_bytes: int, keep: int) -> list[str]:
    try:
        found = sorted(n for n in os.listdir(paths.LOG_DIR) if n.endswith(".log"))
    except OSError as e:
        return [f"{paths.LOG_DIR.name}: list failed: {e}"]
    notes: list[str] = []
    for name in found:
        if name == SERVE_LOG:
            continue
        f = paths.LOG_DIR / name
        try:
            size = f.stat().st_size
        except FileNotFoundError:
            continue
        except OSError as e:
            notes.append(f"{name}: inspect failed: {e}")
            continue
        if size > max_bytes:
            notes.extend(rotate_by_rename(f, keep))
    return notes


def rotate_own_log(max_bytes: int = 5_000_000, keep: int = 5) -> list[str]:
    check_keep(keep)
    f = paths.LOG_DIR / SERVE_LOG
    try:
        mine = os.fstat(1)
        current = f.stat()
    except FileNotFoundError:
        return []
    except OSError as e:
        return [f"{f.name}: inspect failed: {e}"]
    if (mine.st_dev, mine.st_ino) != (current.st_dev, current.st_ino) or current.st_size <= max_bytes:
        return []
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except (OSError, ValueError) as e:
        return [f"{f.name}: flush failed: {e}"]
    if notes := rotate_by_rename(f, keep):
        return notes
    try:
        fd = os.open(f, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    except OSError as e:
        return [f"{f.name}: open failed: {e}"]
    notes = []
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    except OSError as e:
        notes.append(f"{f.name}: dup2 failed: {e}")
    try:
        os.close(fd)
    except OSError as e:
        notes.append(f"{f.name}: close failed: {e}")
    return notes
