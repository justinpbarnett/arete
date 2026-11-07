from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from typing import Literal

from . import alerts, claude, config, install, paths, state, util


@dataclass
class Check:
    key: str
    ok: bool
    detail: str
    severity: Literal["fail", "warn"] = "fail"


def launchd_table() -> dict[str, tuple[str, str]]:
    res = util.sh(["launchctl", "list"], timeout=20)
    table = {}
    for line in res.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3:
            table[parts[2]] = (parts[0], parts[1])
    return table


def check_jobs(cfg: config.Config) -> list[Check]:
    if not paths.ON_MAC:
        return []
    table = launchd_table()
    skipped = {install.label("heartbeat")} | (install.retired_labels() if install.serve_installed() else set())
    out = []
    for job in (j for j in cfg.jobs if j.label not in skipped):
        key = f"job:{job.label}"
        if job.label not in table:
            out.append(Check(key, False, "not loaded in launchd"))
            continue
        pid, status = table[job.label]
        running = pid not in ("-", "")
        if job.keepalive:
            ok = running
            detail = f"running pid {pid}" if running else f"not running, last exit {status}"
        else:
            ok = status == "0"
            detail = "last exit 0" if ok else f"last exit {status}"
        if ok and job.log and job.max_age_hours:
            if not job.log.exists():
                ok, detail = False, "log file missing"
            else:
                age = util.hours_awake_since(job.log.stat().st_mtime)
                if age > job.max_age_hours:
                    ok, detail = False, f"log stale: {age:.0f}h old, limit {job.max_age_hours:.0f}h"
                else:
                    detail += f", log {age:.1f}h old"
        out.append(Check(key, ok, detail))
    return out


def probe_due(p: config.Probe, prev: dict | None) -> bool:
    if not prev or not prev.get("last_run") or not prev.get("ok"):
        return True
    hours_since = (util.now() - util.parse_iso(prev["last_run"])).total_seconds() / 3600
    return hours_since >= p.every_hours


def check_probes(cfg: config.Config, force: bool = False) -> list[Check]:
    out = []
    for p in cfg.probes:
        key = f"probe:{p.name}"
        prev = state.probe_get(p.name)
        if not force and not probe_due(p, prev):
            out.append(Check(key, True, f"ok at {prev['last_run']} (cached)"))
            continue
        res = util.sh([paths.expand(c).as_posix() if c.startswith("~") else c for c in p.command], timeout=p.timeout)
        text = (res.stdout or "") + (res.stderr or "")
        ok = res.returncode == 0 and p.expect.lower() in text.lower()
        detail = "ok" if ok else f"exit {res.returncode}: {util.last_line(res.stderr) or util.last_line(res.stdout) or 'no output'}"
        state.probe_set(p.name, ok, detail)
        out.append(Check(key, ok, detail))
    return out


def check_claude_auth() -> list[Check]:
    st = claude.auth_status()
    if not st.get("loggedIn"):
        return [Check("claude:auth", False, "claude CLI is not logged in")]
    exp = st.get("loginExpiresAt")
    if not exp:
        return [Check("claude:auth", True, "logged in")]
    days = (util.parse_iso(exp) - util.now()).total_seconds() / 86400
    if days < 0:
        return [Check("claude:auth", False, "login expired; run `claude auth login`")]
    if days < 7:
        return [Check("claude:auth", False, f"login expires in {days:.1f} days; run `claude auth login`")]
    return [Check("claude:auth", True, f"login good for {days:.0f} days")]


def check_hook() -> list[Check]:
    try:
        settings = json.loads(paths.CLAUDE_SETTINGS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return [Check("hook:registered", False, "~/.claude/settings.json unreadable")]
    registered = "arete" in json.dumps(settings.get("hooks", {}))
    out = [Check("hook:registered", registered, "arete hook registered" if registered else "arete hook missing from settings")]
    if registered:
        if not paths.EVENTS_FILE.exists():
            out.append(Check("hook:events", False, "no events logged yet", "warn"))
        else:
            age = util.hours_awake_since(paths.EVENTS_FILE.stat().st_mtime)
            ok = age < 72
            out.append(Check("hook:events", ok, f"last event {age:.0f}h ago", "fail" if not ok else "warn"))
    return out


def check_disk() -> list[Check]:
    free_gb = shutil.disk_usage(str(paths.HOME)).free / 1e9
    return [Check("disk:home", free_gb > 15, f"{free_gb:.0f} GB free")]


def check_serve() -> list[Check]:
    from . import control

    beat = control.serve_beat()
    if not beat and not install.serve_installed():
        return [Check("serve:beat", True, "not installed yet; the launchd jobs run the loops until `arete install`")]
    if not beat:
        return [Check("serve:beat", False, "arete serve is installed but has never run; see its log")]
    if not beat["alive"]:
        why = "its process is gone" if not control.pid_alive(beat.get("pid")) else f"last beat {beat['age_seconds']}s ago"
        return [Check("serve:beat", False, f"arete serve is down: {why}")]
    out = [Check("serve:beat", True, f"pid {beat['pid']}, {util.count(beat['loops'], 'loop')} on, {beat['pending']} waiting")]
    for name, error in (beat.get("broken") or {}).items():
        out.append(Check(f"loop:{name}:broken", False, error, "warn"))
    for sid, error in (beat.get("poll_errors") or {}).items():
        out.append(Check(f"source:{sid}", False, error, "warn"))
    return out


def check_loops(cfg: config.Config) -> list[Check]:
    from . import loops

    beats = util.read_json(paths.STATE_DIR / "loops.json", {})
    out = []
    for loop in loops.load_all(cfg):
        if not loop.enabled or loop.error or not loop.stale_hours or loop.name == os.environ.get("ARETE_LOOP"):
            continue
        last = next((r for r in state.runs(kind="loop", loop=loop.name, limit=10, firings=True)
                     if r["status"] != "running"), None)
        if not last:
            continue
        age = util.hours_awake_since(util.parse_iso(last["finished"] or last["started"]).timestamp())
        marked = beats.get(loop.name) or {}
        ok = age <= loop.stale_hours and last["status"] in ("done", "skipped") and marked.get("ok", True)
        detail = f"last run {age:.1f}h ago, {last['status']}, limit {loop.stale_hours:g}h"
        if not marked.get("ok", True):
            detail += f", failed: {marked.get('detail', '')}"
        elif last["status"] not in ("done", "skipped"):
            detail += f": {util.first_line(last['summary'] or '', 160)}"
        out.append(Check(f"loop:{loop.name}", ok, detail))
    return out


def mark_loop(name: str, ok: bool, detail: str = "") -> None:
    f = paths.STATE_DIR / "loops.json"
    beats = util.read_json(f, {})
    beats[name] = {"ts": util.iso(), "ok": ok, "detail": detail[:200]}
    util.write_json(f, beats)


def network_up_or_skip(loop: str, dry_run: bool) -> bool:
    if util.online():
        return True
    print(f"{util.iso()} offline, waiting up to {util.WAKE_NETWORK_WAIT_MINUTES} minutes for the network", flush=True)
    if util.wait_for_network(util.WAKE_NETWORK_WAIT_MINUTES):
        return True
    print(f"{util.iso()} still offline, skipped this {loop}")
    if not dry_run:
        mark_loop(loop, True, "offline")
    return False


def run(dry_run: bool = False, force_probes: bool = False, quiet: bool = False) -> int:
    paths.ensure_dirs()
    install.rotate_logs()
    if not network_up_or_skip("heartbeat", dry_run):
        return 0
    cfg = config.load()
    checks: list[Check] = []
    for fn in (
        lambda: check_jobs(cfg),
        lambda: check_probes(cfg, force_probes),
        check_claude_auth,
        check_hook,
        check_disk,
        check_serve,
        lambda: check_loops(cfg),
    ):
        try:
            checks += fn()
        except Exception as e:
            checks.append(Check(f"heartbeat:{getattr(fn, '__name__', 'check')}", False, f"check crashed: {e}"))

    actions = []
    for c in checks:
        if c.ok:
            msg = alerts.clear_alert(cfg.alert_repo, c.key, dry_run)
        elif c.severity == "fail":
            msg = alerts.raise_alert(cfg.alert_repo, c.key, f"{c.key} failing", c.detail, dry_run)
        else:
            msg = None
        if msg:
            actions.append(msg)

    failing = [c for c in checks if not c.ok and c.severity == "fail"]
    if not dry_run:
        util.write_json(
            paths.HEARTBEAT_FILE,
            {"ts": util.iso(), "ok": not failing, "checks": [asdict(c) for c in checks], "actions": actions},
        )
    if quiet:
        names = ", ".join(c.key for c in failing)
        print(f"{util.iso()} heartbeat {'ok' if not failing else f'{len(failing)} failing: {names}'}")
        for a in actions:
            print(f"  -> {a}")
    else:
        for c in checks:
            mark = "ok  " if c.ok else ("WARN" if c.severity == "warn" else "FAIL")
            print(f"{mark}  {c.key:<40} {c.detail}")
        for a in actions:
            print(f"  -> {a}")
    return 1 if failing else 0
