from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from typing import Literal

from . import (
    accounts,
    alerts,
    backup,
    codex,
    config,
    devices,
    install,
    paths,
    pi,
    routing,
    runner,
    state,
    steps,
    tool_events,
    util,
    workspace,
)


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


def check_costs() -> list[Check]:
    drift = state.cost_drift()
    detail = (f"{len(drift)} runs differ from steps by ${sum(abs(r['difference']) for r in drift):.6f}: "
              + ", ".join(r["id"] for r in drift[:5])) if drift else "run costs equal their steps"
    return [Check("costs:drift", not drift, detail)]


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
    ok, detail = runner.claude_login()
    return [Check("claude:auth", ok, detail)]


def check_codex_auth(cfg: config.Config) -> list[Check]:
    if not uses_codex(cfg):
        return []
    ok, detail = runner.codex_login()
    return [Check("codex:auth", ok, detail)]


def check_codex_cleanup(cfg: config.Config) -> list[Check]:
    if not uses_codex(cfg):
        return []
    found = codex.cleanup()
    return [Check("codex:cleanup", found.status == "able", f"{found.status}: {found.detail}", "warn")]


def check_oncall(cfg: config.Config) -> list[Check]:
    if not cfg.oncall.enabled:
        return []
    from . import oncall
    ok, detail = oncall.health()
    return [Check("oncall:scan", ok, detail)]


@dataclass(frozen=True)
class Call:
    runner: str
    model: str
    repo_name: str
    device: str
    repo: config.Repo | None = field(default=None, compare=False)


def configured_calls(cfg: config.Config) -> list[Call]:
    from . import loops

    found = []
    for repo in [None, *cfg.repos]:
        try:
            layer = config.load_layer(repo.layer_dir) if repo else None
        except (ValueError, TypeError, OSError):
            continue
        for kind in ("", *config.KINDS):
            for role in config.STEP_ROLES:
                try:
                    step = steps.resolve(cfg, layer, role, kind)
                except ValueError:
                    continue
                found.append(Call(step.runner, step.model, repo.name if repo else "", "local", repo))
    for loop in loops.load_all(cfg):
        if loop.error:
            continue
        repo = loop.work_repo(cfg) if loop.repo else None
        for model in loop.agent_models(cfg):
            which, name = routing.route(model, loop.runner)
            found.append(Call(which, name, repo.name if repo else "", loop.device, repo))
    return list(dict.fromkeys(found))


def check_pi_auth(cfg: config.Config) -> list[Check]:
    if cfg.replay.all:
        return []
    configured = accounts.mapping(pi.model_configuration().get("providers"))
    stored = accounts.read(pi.owner_dir() / "auth.json")
    names = set(configured) | set(stored) | set(cfg.environment.providers)
    names |= {p for p in pi.KEY_NAMES if os.environ.get(pi.KEY_NAMES[p])}
    keys = util.read_env_file(pi.KEYS_FILE)
    names |= {p for p, n in pi.KEY_NAMES.items() if keys.get(n)}
    if default := pi.default_provider():
        names.add(default)
    calls = [(f"{p}/*", None, "local", False) for p in names]
    calls += [(c.model, c.repo, c.device, True) for c in configured_calls(cfg) if c.runner == "pi"]
    out = {}
    for model, repo, device, required in calls:
        selected = routing.pi_route(model, cfg, repo, device)
        provider = selected.provider or "unspecified"
        hold = routing.auth_hold(selected.auth, selected.model)
        source = f"SDK-managed {selected.managed.kind}" if selected.managed else "key"
        detail = (selected.error if selected.error else "held login; waiting" if hold else
                  f"login-backed ({'subscription' if selected.auth.mode == 'subscription' else 'metered'})"
                  if selected.using_login else
                  f"login held; using {source}" if selected.login_held and selected.key_available else
                  source if selected.managed else "key-only" if selected.key_available else
                  "unauthenticated local")
        if selected.login_refused:
            detail = f"login refused: {selected.login_refused}; {detail}"
        if provider == "anthropic" and selected.key_available and not selected.error:
            detail += "; paid api_key route"
        optional_anthropic = not required and selected.error == pi.ANTHROPIC_KEY_REQUIRED
        key = f"pi:{provider}:auth" + (f":{repo.name}" if repo else "")
        if devices.resolve(device).remote:
            key += f":{devices.resolve(device).name}"
        missing = (f"pi {provider}: no usable login or supported key; checked {selected.owner}, "
                   f"{pi.KEYS_FILE} and Arete environment")
        optional_missing = not required and selected.error == missing
        check = Check(key, not selected.error or optional_anthropic, f"{provider}: {detail}",
                      "warn" if optional_missing else "fail")
        if key not in out or out[key].ok or out[key].severity == "warn" and check.severity == "fail":
            out[key] = check
    return list(out.values())


def uses_codex(cfg: config.Config) -> bool:
    return not cfg.replay.all and any(call.runner == "codex" for call in configured_calls(cfg))


def check_hook() -> list[Check]:
    try:
        settings = json.loads(paths.CLAUDE_SETTINGS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return [Check("hook:registered", False, "~/.claude/settings.json unreadable")]
    registered = "arete" in json.dumps(settings.get("hooks", {}))
    out = [Check("hook:registered", registered, "arete hook registered" if registered else "arete hook missing from settings")]
    if registered:
        files = tool_events.event_stats()
        if not files:
            out.append(Check("hook:events", False, "no events logged yet", "warn"))
        else:
            age = util.hours_awake_since(max(m for _, m, _ in files))
            ok = age < 72
            out.append(Check("hook:events", ok, f"last event {age:.0f}h ago", "fail" if not ok else "warn"))
    return out


def check_disk() -> list[Check]:
    free_gb = shutil.disk_usage(str(paths.HOME)).free / 1e9
    return [Check("disk:home", free_gb > 15, f"{free_gb:.0f} GB free")]


def size_words(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def age_words(since: str) -> str:
    try:
        hours = (util.now() - util.parse_iso(since.replace("Z", "+00:00"))).total_seconds() / 3600
    except ValueError:
        return "of unknown age"
    return f"{hours / 24:.0f} days ago" if hours >= 48 else f"{max(hours, 0):.0f} h ago"


def blocked_words(row: dict) -> str:
    names = row.get("names") or []
    if names:
        return f"held by {', '.join(names)}"
    why = row.get("why") or ""
    if row.get("path") and row.get("name"):
        why = why.replace(row["path"], row["name"])
    return util.first_line(why, 160)


def check_workspaces() -> list[Check]:
    held = workspace.held_summary()
    if not held["count"]:
        return [Check("workspaces:held", True, "none held", "warn")]
    detail = f"{held['count']} held, {size_words(held['bytes'])}, oldest {age_words(held['oldest'])} ({held['oldest_name']})"
    blocked = [r for r in held["rows"] if r.get("blocked")]
    if blocked:
        detail += f"; {len(blocked)} blocked: {blocked[0]['name']}: {blocked_words(blocked[0])}"
    return [Check("workspaces:held", not blocked, detail, "warn")]


def check_scratch() -> list[Check]:
    from . import scratch

    try:
        view = scratch.survey()
    except OSError as e:
        return [Check("scratch:records", False, f"the verification record store cannot be read: {e}")]
    total = len(view.good) + len(view.unreadable)
    out = []
    if view.unreadable:
        first = view.unreadable[0]
        out.append(Check("scratch:records", False, f"{len(view.unreadable)} verification records cannot be read; "
                         f"first {first.path}: {util.first_line(first.reason, 160)}"))
    if total > MAX_HOT_RECORDS:
        out.append(Check("scratch:count", False, f"{total} verification records are still in the hot folder", "warn"))
    return out or [Check("scratch:records", True, f"{total} verification records, all readable")]


def check_serve() -> list[Check]:
    from . import control

    beat = control.serve_beat()
    if not beat and not install.serve_installed():
        return [Check("serve:beat", True, "not installed yet; the launchd jobs run the loops until serve is installed")]
    if not beat:
        return [Check("serve:beat", False, "arete serve is installed but has never run; see its log")]
    if not beat["alive"]:
        why = "its process is gone" if beat["process_gone"] else f"last beat {beat['age_seconds']}s ago"
        return [Check("serve:beat", False, f"arete serve is down: {why}")]
    waiting = "unknown" if beat["pending"] is None else beat["pending"]
    out = [Check("serve:beat", True, f"pid {beat['pid']}, {util.count(beat['loops'], 'loop')} on, {waiting} waiting")]
    if failing := beat.get("failing"):
        detail = "; ".join(f"{key}: {f['error']} (since {f['since']}, {util.count(f['ticks'], 'pass', 'passes')})"
                           for key, f in sorted(failing.items()))
        fails = any(control.serious_failure(key) and f["ticks"] >= control.STUCK_TICKS for key, f in failing.items())
        out.append(Check("serve:tick", False, detail, "fail" if fails else "warn"))
    else:
        out.append(Check("serve:tick", True, "every tick part runs"))
    for name, error in (beat.get("broken") or {}).items():
        out.append(Check(f"loop:{name}:broken", False, error, "warn"))
    for sid, error in (beat.get("poll_errors") or {}).items():
        out.append(Check(f"source:{sid}", False, error, "warn"))
    return out


BACKUP_STALE_HOURS = 3
OFFSITE_STALE_HOURS = 48
MAX_HOT_RECORDS = 500


def check_backups(cfg: config.Config) -> list[Check]:
    return [check_backup_db(), check_backup_offsite(cfg)]


def check_backup_db() -> Check:
    newest = backup.newest_snapshot()
    installed = install.serve_installed()
    if newest is None:
        return Check("backup:db", not installed, "no snapshot of arete.db yet" if installed
                     else "no snapshot yet; serve is not installed")
    found = backup.inspect(newest)
    recorded = backup.read_last().get("quick_check", "ok")
    if found["quick_check"] != "ok":
        return Check("backup:db", False, f"{newest.name} failed its check: {found['quick_check']}")
    if recorded != "ok":
        return Check("backup:db", False, f"the last snapshot failed its check: {recorded}")
    hours = util.hours_awake_since(newest.stat().st_mtime)
    if installed and hours > BACKUP_STALE_HOURS:
        return Check("backup:db", False, f"newest snapshot {newest.name} is {hours:.1f} awake hours old")
    return Check("backup:db", True, f"{newest.name}, {hours:.1f} awake hours old")


def check_backup_offsite(cfg: config.Config) -> Check:
    if not cfg.backup.copy_to:
        return Check("backup:offsite", False, "no copy leaves the machine until the owner sets [backup] copy_to "
                     "in config.toml", "warn")
    copied = backup.read_last().get("copied_at", "")
    if not copied:
        return Check("backup:offsite", False, f"no copy to {cfg.backup.copy_to} has succeeded yet")
    hours = util.hours_awake_since(util.parse_iso(copied).timestamp())
    if hours > OFFSITE_STALE_HOURS:
        return Check("backup:offsite", False, f"the last good copy is {hours:.0f} awake hours old")
    return Check("backup:offsite", True, f"last good copy {hours:.1f} awake hours ago")


def check_loops(cfg: config.Config) -> list[Check]:
    from . import loops

    beats = state.loop_marks()
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
        held = loop.usage_hold(cfg)
        ok = (age <= loop.stale_hours or bool(held)) and last["status"] in ("done", "skipped") and marked.get("ok", True)
        detail = f"last run {age:.1f}h ago, {last['status']}, limit {loop.stale_hours:g}h"
        if held:
            detail += f", {held}"
        if not marked.get("ok", True):
            detail += f", failed: {marked.get('detail', '')}"
        elif last["status"] not in ("done", "skipped"):
            detail += f": {util.first_line(last['summary'] or '', 160)}"
        out.append(Check(f"loop:{loop.name}", ok, detail))
    return out


def mark_loop(name: str, ok: bool, detail: str = "") -> None:
    state.set_loop_state(name, mark_ok=int(ok), mark_detail=detail[:200], mark_at=util.iso())


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
    (paths.STATE_DIR / "loops.json").unlink(missing_ok=True)
    if not network_up_or_skip("heartbeat", dry_run):
        return 0
    cfg = config.load()
    checks: list[Check] = []
    ran_fine = set()
    for fn, *args in (
        (check_jobs, cfg),
        (check_probes, cfg, force_probes),
        (check_claude_auth,),
        (check_codex_auth, cfg),
        (check_codex_cleanup, cfg),
        (check_pi_auth, cfg),
        (check_hook,),
        (check_disk,),
        (check_serve,),
        (check_backups, cfg),
        (check_costs,),
        (check_loops, cfg),
        (check_oncall, cfg),
        (check_workspaces,),
        (check_scratch,),
    ):
        crash_key = f"heartbeat:{fn.__name__}"
        try:
            checks += fn(*args)
            ran_fine.add(crash_key)
        except Exception as e:
            checks.append(Check(crash_key, False, f"check crashed: {e}"))

    actions = []
    for c in checks:
        if c.ok or c.severity == "warn":
            msg = alerts.clear_alert(cfg.alert_repo, c.key, dry_run)
        elif c.severity == "fail":
            msg = alerts.raise_alert(cfg.alert_repo, c.key, f"{c.key} failing", c.detail, dry_run)
        else:
            msg = None
        if msg:
            actions.append(msg)
    for crash_key in sorted(ran_fine - {c.key for c in checks}):
        msg = alerts.clear_alert(cfg.alert_repo, crash_key, dry_run)
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
