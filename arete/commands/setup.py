from __future__ import annotations

import sys

from .. import __version__, config, paths
from .common import (
    loop_line,
    print_alerts,
    print_checks,
    print_live,
    print_proposals,
    print_recent,
    print_records,
    print_skipped,
    print_triage,
    print_waiting,
    serve_line,
    show,
    steering,
)

DEFAULT_CONFIG = """\
alert_repo = "you/arete-alerts"

global_repo = "dotfiles"

[models]
plan = "opus"
build = "opus"
review = "opus"
learn = "sonnet"
miner = "opus"
apply = "sonnet"
uat = "sonnet"
verify = "opus"
eval_mine = "sonnet"
triage = "haiku"

[miner]
lookback_hours = 24
max_digest_chars = 90000

[[repo]]
name = "dotfiles"
path = "~/dev/dotfiles"
github = "you/dotfiles"
public = true

[[repo]]
name = "web"
path = "~/dev/web"
github = "you/web"
pipeline = true

[[job]]
label = "com.arete.serve"
keepalive = true

[[probe]]
name = "claude"
command = ["claude", "-p", "--model", "haiku", "--no-session-persistence", "Reply with the single word ok"]
expect = "ok"
every_hours = 12

[[probe]]
name = "gh"
command = ["gh", "auth", "status"]
expect = "Logged in"
every_hours = 6
"""


def cmd_init(args) -> int:
    paths.ensure_dirs()
    paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if paths.CONFIG_FILE.exists() and not args.force:
        print(f"{paths.CONFIG_FILE} exists; pass --force to overwrite")
        return 1
    paths.CONFIG_FILE.write_text(DEFAULT_CONFIG)
    print(f"wrote {paths.CONFIG_FILE}")
    print(f"state in {paths.STATE_DIR}, logs in {paths.LOG_DIR}")
    return 0


def cmd_status(args) -> int:
    from .. import board, control, record

    if args.json:
        return status_lines(args.run_id)
    if args.run_id:
        print("arete status --json RUN_ID prints one run's steps", file=sys.stderr)
        return 2
    cfg, problem = config.load_or_problem()
    views = control.loop_views(cfg)
    snap = board.snapshot(recent_runs=args.limit)
    print(f"arete {__version__}  config {paths.CONFIG_FILE}")
    if problem:
        print(f"config does not load: {problem}")
    if cfg.ignored:
        print(f"config names this engine ignores: {', '.join(cfg.ignored)}")
    print(f"max_parallel {cfg.max_parallel}, max_parallel_per_repo {cfg.max_parallel_per_repo}")
    print("models:" + "".join(f"\n  {role:<15} {cfg.model(role) or 'claude CLI'}" for role in config.MODEL_ROLES))
    if cfg.unused_model_keys():
        print(f"  [models] keys arete never reads: {', '.join(cfg.unused_model_keys())}")
    show(snap["checks"], "the heartbeat", print_checks)
    show(snap["serve"], "the serve beat", lambda beat: print(serve_line(beat)))
    marks = {m["name"]: m for m in snap["checks"].get("rows", {}).get("loops", [])}
    for v in views:
        mark = marks.get(v["name"], {})
        print(f"  {loop_line(v)}" + ("" if mark.get("ok", True) else f"  FAILED {mark.get('detail', '')}"))
    show(snap["alerts"], "the alerts", print_alerts)
    show(snap["recent"], "the recent runs", print_recent)
    show(snap["proposals"], "the miner proposals", print_proposals)
    show(board.panel(record.store), "the record store", lambda s: print_records(s, cfg.retention))
    show(snap["skipped"], "the skipped tickets", print_skipped)
    show(snap["waiting"], "the review feedback", print_waiting)
    show(snap["triage"], "triage", print_triage)
    pipeline_repos = [f"{r.name} ({r.tracker})" for r in cfg.repos if r.pipeline]
    print(f"pipeline repos: {', '.join(pipeline_repos) or 'none'}")
    show(snap["live"], "the live runs", print_live)
    return 0


def status_lines(run_id: str) -> int:
    from .. import client
    from .runs import local_lines

    def live() -> int:
        try:
            panel = client.call("GET", "/api/live")["live"]
        except client.Down:
            from .. import board, state

            panel = board.panel(lambda: board.live(state.runs_marked_running(connect=state.reader)))
        if "error" in panel:
            client.emit({"error": panel["error"]})
            return 1
        for row in panel["rows"]["runs"]:
            client.emit(row)
        return 0

    def steps() -> int:
        try:
            read = list(client.events(run_id))
        except client.Down:
            if (found := local_lines(run_id, 0, False)) is None:
                client.emit({"error": f"no run {run_id}"})
                return 1
            read = list(found)
        for line in client.steps_of(run_id, read):
            client.emit(line)
        return 0

    return client.steered(steps if run_id else live)


def cmd_install(args) -> int:
    from .. import install

    return install.install(dry_run=args.dry_run, platform=args.platform)


def cmd_uninstall(args) -> int:
    from .. import install

    return install.uninstall()


def cmd_migrate(args) -> int:
    try:
        from .. import state

        state.migrate(contracts=not args.no_contracts)
    except Exception as e:
        print(f"arete migrate: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(args) -> int:
    from .. import doctor

    results = doctor.run()
    for r in results:
        print(doctor.line(r))
    return 1 if any(r.failing for r in results) else 0


def cmd_dashboard(args) -> int:
    from .. import dashboard

    return dashboard.serve(args.port)


def register(sub) -> None:
    p = sub.add_parser("init", help="write the default global config")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = steering(sub.add_parser("status", help="settings, heartbeat, alerts, recent runs; with --json, the live runs "
                                               "or one run's steps as JSON lines"))
    p.add_argument("run_id", nargs="?", default="", help="with --json: this run's steps, then its report")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true", help="one JSON line per live run, or per step of RUN_ID")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("install", help="run arete serve at login: one launchd agent on a Mac, one systemd user unit on "
                                       "Linux; retires the old per loop agents")
    p.add_argument("--dry-run", action="store_true", help="print the agent or unit, change nothing")
    p.add_argument("--platform", choices=("mac", "linux"), default="", help="with --dry-run: print for this platform")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("uninstall", help="stop arete serve and remove its agent or unit")
    p.set_defaults(fn=cmd_uninstall)

    p = sub.add_parser("migrate", help="apply pending database migrations")
    p.add_argument("--no-contracts", action="store_true", help="stop before the first contract migration")
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("doctor", help="read only check of arete's own state: cost drift, orphan rows, stray folders, "
                                      "stuck work, the database and its backup; it reports and never repairs")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("dashboard", help="serve the dashboard to its owner on the tailnet")
    p.add_argument("--port", type=int, default=None, help="the port to listen on; the config's [dashboard] port by default")
    p.set_defaults(fn=cmd_dashboard)
