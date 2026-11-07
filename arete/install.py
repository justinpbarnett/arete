from __future__ import annotations

import os
import plistlib
import shutil
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


def serve_installed() -> bool:
    return (AGENTS_DIR / f"{label(SERVE)}.plist" if paths.ON_MAC else UNITS_DIR / SERVE_UNIT).exists()


def retired_labels() -> set[str]:
    return {label(name) for name in RETIRED}


def install(dry_run: bool = False, platform: str = "") -> int:
    platform = platform or this_platform()
    if platform not in PLATFORMS:
        print(f"unknown platform {platform}; use {' or '.join(PLATFORMS)}")
        return 1
    if dry_run:
        dest = (AGENTS_DIR / f"{label(SERVE)}.plist") if platform == "mac" else UNITS_DIR / SERVE_UNIT
        print(f"--- {dest}")
        print(plistlib.dumps(serve_plist()).decode() if platform == "mac" else serve_unit(), end="")
        if platform == "mac":
            for lb in (label(name) for name in RETIRED if (AGENTS_DIR / f"{label(name)}.plist").exists()):
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
    dest = AGENTS_DIR / f"{pl['Label']}.plist"
    util.sh(["launchctl", "bootout", f"{_domain()}/{pl['Label']}"], timeout=30)
    dest.write_bytes(plistlib.dumps(pl))
    res = util.sh(["launchctl", "bootstrap", _domain(), str(dest)], timeout=30)
    print(f"{pl['Label']}: " + ("loaded" if res.returncode == 0 else f"bootstrap failed: {util.first_line(res.stderr)}"))
    return 0 if res.returncode == 0 else 1


def install_systemd() -> int:
    UNITS_DIR.mkdir(parents=True, exist_ok=True)
    (UNITS_DIR / SERVE_UNIT).write_text(serve_unit())
    for step in (["daemon-reload"], ["enable", "--now", SERVE_UNIT], ["restart", SERVE_UNIT]):
        res = util.sh(["systemctl", "--user", *step], timeout=60)
        if res.returncode != 0:
            print(f"systemctl --user {' '.join(step)} failed: {util.first_line(res.stderr or res.stdout)}")
            return 1
    print(f"{SERVE_UNIT}: enabled and started; `loginctl enable-linger` keeps it running after logout")
    return 0


def retire(lb: str) -> None:
    f = AGENTS_DIR / f"{lb}.plist"
    if not f.exists():
        return
    util.sh(["launchctl", "bootout", f"{_domain()}/{lb}"], timeout=30)
    f.unlink()
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
        (UNITS_DIR / SERVE_UNIT).unlink(missing_ok=True)
        util.sh(["systemctl", "--user", "daemon-reload"], timeout=60)
        print(f"{SERVE_UNIT}: removed")
        return 0
    for name in (SERVE, *RETIRED):
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
