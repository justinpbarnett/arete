from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from typing import Literal

from . import accounts, alerts, codex, config, devices, install, paths, pi, runner, state, steps, util


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


def check_pi_auth(cfg: config.Config) -> list[Check]:
    from . import loops

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
    for repo in [None, *cfg.repos]:
        try:
            layer = config.load_layer(repo.layer_dir) if repo else None
        except (ValueError, TypeError, OSError):
            continue
        for kind in ("", *config.KINDS):
            for role in config.STEP_ROLES:
                try:
                    step = steps.resolve(cfg, layer, role, kind)
                    if step.runner == "pi":
                        calls.append((step.model, repo, "local", True))
                except ValueError:
                    continue
    for loop in loops.load_all(cfg):
        if not loop.error:
            repo = loop.work_repo(cfg) if loop.repo else None
            calls += [(runner.route(m, loop.runner)[1], repo, loop.device, True) for m in loop.agent_models(cfg)
                      if runner.route(m, loop.runner)[0] == "pi"]
    out = {}
    for model, repo, device, required in calls:
        selected = runner.pi_route(model, cfg, repo, device)
        provider = selected.provider or "unspecified"
        hold = runner.auth_hold(selected.auth, selected.model)
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
    from . import loops

    if cfg.replay.all:
        return False
    layers = [None]
    for repo in cfg.repos:
        try:
            layers.append(config.load_layer(repo.layer_dir))
        except (ValueError, TypeError, OSError):
            continue
    for layer in layers:
        for kind in ("", *config.KINDS):
            for role in config.STEP_ROLES:
                try:
                    if steps.resolve(cfg, layer, role, kind).runner == "codex":
                        return True
                except ValueError:
                    continue
    return any(not loop.error and any(runner.route(model, loop.runner)[0] == "codex"
                                     for model in loop.agent_models(cfg)) for loop in loops.load_all(cfg))


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
        return [Check("serve:beat", True, "not installed yet; the launchd jobs run the loops until serve is installed")]
    if not beat:
        return [Check("serve:beat", False, "arete serve is installed but has never run; see its log")]
    if not beat["alive"]:
        why = "its process is gone" if beat["process_gone"] else f"last beat {beat['age_seconds']}s ago"
        return [Check("serve:beat", False, f"arete serve is down: {why}")]
    out = [Check("serve:beat", True, f"pid {beat['pid']}, {util.count(beat['loops'], 'loop')} on, {beat['pending']} waiting")]
    for name, error in (beat.get("broken") or {}).items():
        out.append(Check(f"loop:{name}:broken", False, error, "warn"))
    for sid, error in (beat.get("poll_errors") or {}).items():
        out.append(Check(f"source:{sid}", False, error, "warn"))
    return out


def check_loops(cfg: config.Config) -> list[Check]:
    from . import loops

    beats = util.read_json(paths.LOOP_MARKS_FILE, {})
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
    beats = util.read_json(paths.LOOP_MARKS_FILE, {})
    beats[name] = {"ts": util.iso(), "ok": ok, "detail": detail[:200]}
    util.write_json(paths.LOOP_MARKS_FILE, beats)


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
        (check_loops, cfg),
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
