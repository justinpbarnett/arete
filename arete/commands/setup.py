from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from .. import __version__, client, config, configrepo, paths, principals, retention, routing, steps
from ..store import db as foundation
from ..store import runs as run_store
from .common import (
    loop_line,
    print_alerts,
    print_checks,
    print_live,
    print_proposals,
    print_recent,
    print_records,
    print_skipped,
    print_ticket_progress,
    print_triage,
    print_waiting,
    serve_line,
    show,
    status_arguments,
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
    configrepo.init()
    _, base_blob = configrepo.read("config.toml")
    configrepo.commit(
        {"config.toml": DEFAULT_CONFIG},
        {"config.toml": base_blob},
        actor=principals.current(),
        source="engine",
        note="arete init",
    )
    print(f"wrote {paths.CONFIG_FILE}")
    print(f"state in {paths.STATE_DIR}, logs in {paths.LOG_DIR}")
    return 0


def cmd_status(args) -> int:
    from .. import board, control, record

    if args.json:
        return status_lines(args.run_id, args.offline)
    if args.run_id:
        print("arete status --json RUN_ID prints one run's steps", file=sys.stderr)
        return 2
    cfg, problem = config.load_or_problem()
    if args.offline:
        with foundation.one_read():
            views = control.loop_views(cfg, connect=foundation.reader)
            snap = board.snapshot(recent_runs=args.limit)
            records = board.panel(record.store)
        server_url = ""
    else:
        answer = client.call("GET", f"/api/v1/status?limit={args.limit}")
        views, snap, records = (client.field(answer, key) for key in ("loops", "snapshot", "records"))
        server_url = client.address(client.server())
    rendered = StringIO()
    with redirect_stdout(rendered):
        print_status(cfg, problem, views, snap, records, server_url)
    if args.offline:
        for line in rendered.getvalue().splitlines():
            client.emit({"message": line, "source": "local"})
    else:
        print(rendered.getvalue(), end="")
    return 0


def print_status(cfg, problem, views, snap, records, server_url):
    print(f"arete {__version__}  local config {paths.CONFIG_FILE}")
    if problem:
        print(f"config does not load: {problem}")
    elif cfg.problems:
        print(f"config problems: {'; '.join(p.text() for p in cfg.problems)}")
    if cfg.ignored:
        print(f"config names this engine ignores: {', '.join(cfg.ignored)}")
    print(f"max_parallel {cfg.max_parallel}, max_parallel_per_repo {cfg.max_parallel_per_repo}")
    print("models:" + "".join(f"\n  {role:<15} {cfg.model(role) or 'claude CLI'}" for role in config.MODEL_ROLES))
    if cfg.unused_model_keys():
        print(f"  [models] keys arete never reads: {', '.join(cfg.unused_model_keys())}")
    pipeline_repos = [f"{r.name} ({r.tracker})" for r in cfg.repos if r.pipeline]
    print(f"pipeline repos: {', '.join(pipeline_repos) or 'none'}")
    print_delivery(cfg)
    if server_url:
        print(f"serve at {server_url}")
    show(snap["checks"], "the heartbeat", print_checks)
    show(snap["serve"], "the serve beat", lambda beat: print(serve_line(beat)))
    marks = {m["name"]: m for m in snap["checks"].get("rows", {}).get("loops", [])}
    for v in views:
        mark = marks.get(v["name"], {})
        print(f"  {loop_line(v)}" + ("" if mark.get("ok", True) else f"  FAILED {mark.get('detail', '')}"))
    show(snap["alerts"], "the alerts", print_alerts)
    show(snap["recent"], "the recent runs", print_recent)
    show(snap["proposals"], "the miner proposals", print_proposals)
    show(records, "the record store", lambda s: print_records(s, None if server_url else retention.load_live().records))
    show(snap["skipped"], "the skipped tickets", print_skipped)
    show(snap["waiting"], "the review feedback", print_waiting)
    if recovery := snap.get("recovery"):
        show(recovery, "automatic recovery", print_recovery)
    show(snap["triage"], "triage", print_triage)
    show(snap["live"], "the live runs", print_live)
    if progress := snap.get("ticket_progress"):
        show(progress, "ticket progress", print_ticket_progress)


def print_recovery(rows):
    if rows:
        print("automatic recovery:")
    for row in rows:
        print(f"  {row['repo']} {row['ticket']}: {row['phase']} ({row['cause']}) {row['detail']}")



def print_delivery(cfg: config.Config) -> None:
    repos = [repo for repo in cfg.repos if repo.pipeline]
    if not repos:
        return
    print("delivery configuration (availability not checked):")
    for repo in repos:
        try:
            layer = config.load_layer(repo.layer_dir)
            steps.check(cfg, layer)
            detail = layer.factory_path
            if layer.factory_path == "orchestrated":
                setting = steps.resolve(cfg, layer, "orchestrate")
                provider = setting.provider or routing.identity(setting.runner, setting.model)[0]
                model = setting.model or "(runner default)"
                qualified = model if "/" in model else f"{provider}/{model}"
                detail += (f"; controller runner={setting.runner} model={qualified}"
                           f" effort={setting.effort_on(setting.runner) or 'default'}")
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            detail = (f"configuration problem ({type(error).__name__}); "
                      f"check {paths.short_path(repo.layer_dir / 'config.toml')}")
        print(f"  {repo.name}: {detail}")


def status_lines(run_id: str, offline: bool) -> int:
    from .runs import local_lines

    def emit(row):
        client.emit({**row, "source": "local"} if offline else row)

    def live() -> int:
        if offline:
            from .. import board

            panel = board.panel(lambda: board.live(run_store.runs_marked_running(connect=foundation.reader)))
        else:
            panel = client.field(client.call("GET", "/api/live"), "live")
        if "error" in panel:
            emit({"error": panel["error"]})
            return 1
        for row in client.field(client.field(panel, "rows"), "runs", "live.rows.runs"):
            emit(row)
        return 0

    def steps() -> int:
        if offline:
            if (found := local_lines(run_id, 0, False)) is None:
                emit({"error": f"no run {run_id}"})
                return 1
            read = list(found)
        else:
            read = list(client.events(run_id))
        for line in client.steps_of(run_id, read):
            emit(line)
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
        if args.retry_quarantined_workspace is not None:
            from ..store import workspaces

            if args.workspace_snapshots is None:
                raise ValueError("retry requires --workspace-snapshots DIR")
            workspaces.retry_quarantine(args.retry_quarantined_workspace, args.workspace_snapshots)
            print(f"Restored snapshot for {args.retry_quarantined_workspace}")
            return 0
        if args.retire_quarantined_workspace is not None:
            from ..store import workspaces

            workspaces.retire_quarantine(args.retire_quarantined_workspace)
            print(f"Retired quarantine for {args.retire_quarantined_workspace}")
            return 0
        waiting = foundation.migrate(contracts=not args.no_contracts, workspace_snapshots=args.workspace_snapshots)
    except Exception as e:
        print(f"arete migrate: {e}", file=sys.stderr)
        return 1
    return int(waiting and args.workspace_snapshots is not None)


def cmd_doctor(args) -> int:
    from .. import doctor

    results = doctor.run()
    for r in results:
        print(doctor.line(r))
    return 1 if any(r.failing for r in results) else 0


def cmd_prompts(args) -> int:
    from .. import prompt_loader

    names = [args.name] if args.name else sorted(p.stem for p in paths.PROMPTS_DIR.glob("*.md"))
    failed = not names
    if not names:
        print("no prompt templates found", file=sys.stderr)
    for name in names:
        try:
            inputs = prompt_loader.template(name).get_identifiers()
            row = {"name": name, "valid": True, "inputs": inputs}
        except (OSError, ValueError) as error:
            row = {"name": name, "valid": False, "error": str(error)}
            failed = True
        if args.json:
            print(json.dumps(row))
        elif row["valid"]:
            print(f"{name}: {', '.join(row['inputs']) or '(no inputs)'}")
        else:
            print(row["error"], file=sys.stderr)
    return int(failed)


def cmd_dashboard(args) -> int:
    from .. import dashboard

    return dashboard.serve(args.port)


def choose_jira_project() -> str:
    print("Jira project key for this repo (blank to cancel): ", end="", flush=True, file=sys.stderr)
    return sys.stdin.readline().strip()


def cmd_repo(args) -> int:
    from .. import enrollment, gh, jira, outbound, release_profile, ticket_publication

    try:
        if args.repo_action == "enroll":
            repo = enrollment.enroll(args.name, args.path, target_branch=args.target_branch, landing=args.landing,
                                     unattended_environment=args.unattended_environment, destination=args.tracker,
                                     jira_project=args.jira_project, jira_label=args.jira_label, pipeline=args.pipeline,
                                     choose_project=choose_jira_project if sys.stdin.isatty() else None,
                                     staging_profile=args.staging_profile, commenters=args.commenter)
            cfg = config.load_required()
        elif args.repo_action == "publish-pair":
            cfg = config.load_required()
            raw = sys.stdin.read() if args.spec_file == "-" else Path(args.spec_file).read_text()
            payload = json.loads(raw)
            entries = payload["specs"] if isinstance(payload, dict) and "specs" in payload else payload
            result = ticket_publication.plan_pair(cfg, entries, args.key, ready_only=args.ready_only)
            print(json.dumps(result, indent=None if args.json else 2))
            return 0
        else:
            cfg = config.load_required()
            repo = cfg.repo(args.name)
        if args.repo_action == "staging-profile":
            release_profile.install(repo, args.policy_file)
        if args.repo_action == "standing-production":
            if args.promise_file:
                release_profile.set_standing(repo, args.after_issue, args.promise_file, args.observer_file, args.production_checks)
            else:
                if args.after_issue is not None or args.observer_file or args.production_checks:
                    raise ValueError("use --promise-file to install a standing production grant")
                if args.revoke:
                    release_profile.revoke_standing(repo)
            status = release_profile.standing_status(repo, release_profile.current(repo)) or {"active": False}
            print(json.dumps(status, indent=None if args.json else 2))
        elif args.repo_action == "publish":
            body = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text()
            url = ticket_publication.plan(cfg, repo, args.kind, args.title, body, args.key,
                                          parent=args.parent, source=args.source, draft=args.draft)
            print(url)
        else:
            print(json.dumps(enrollment.show(cfg, repo), indent=None if args.json else 2))
    except (OSError, ValueError, gh.GhError, jira.JiraError) as error:
        print(f"arete repo {args.repo_action}: {outbound.sanitize(str(error), 300)}", file=sys.stderr)
        return 1
    return 0


def repo_commands(sub) -> None:
    group = sub.add_parser("repo", help="enroll a checkout and publish to its saved tracker")
    actions = group.add_subparsers(dest="repo_action", required=True)
    p = actions.add_parser("enroll", help="save tracker and delivery policy together in config.toml")
    p.add_argument("name")
    p.add_argument("path", type=Path, help="root of the Git checkout")
    p.add_argument("--target-branch", required=True)
    p.add_argument("--landing", choices=config.LANDING_METHODS, default="pr",
                   help="pr forbids direct pushes; direct-push allows existing guarded push deliveries")
    p.add_argument("--unattended-environment", choices=config.DEPLOYMENT_ENVIRONMENTS, default="none",
                   help="deployment ceiling, not permission to deploy (default: none)")
    p.add_argument("--tracker", choices=("github", "jira"), default="",
                   help="override GitHub for personal repos or Jira for [jira] github_owner")
    p.add_argument("--jira-project", default="", help="select Jira and use this existing project instead of [jira] project")
    p.add_argument("--jira-label", default="", help="override the repo's Jira routing label")
    p.add_argument("--pipeline", action="store_true", help="enable the repo's existing factory queue")
    p.add_argument("--staging-profile", type=Path, help="owner-approved reusable staging destinations, checks and QA authority")
    p.add_argument("--commenter", action="append", default=[], help="GitHub username allowed to direct work in ticket comments")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_repo)

    p = actions.add_parser("staging-profile", help="set the protected staging defaults of an existing repository")
    p.add_argument("name")
    p.add_argument("--policy-file", type=Path, required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_repo)

    p = actions.add_parser("standing-production", help="inspect, grant or revoke automatic promotion for future GitHub issues")
    p.add_argument("name")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--promise-file", type=Path, help="install the default protected ticket promise")
    mode.add_argument("--revoke", action="store_true", help="withdraw the repository's standing production approval")
    p.add_argument("--after-issue", type=int, help="apply only to issue numbers greater than this number")
    p.add_argument("--observer-file", type=Path, help="observer program matching the promise's SHA-256")
    p.add_argument("--production-checks", type=Path, help="production job policy JSON, defaults to the profile's staging checks")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_repo)

    p = actions.add_parser("show", help="read back tracker, branch, landing and deployment settings")
    p.add_argument("name")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_repo)

    p = actions.add_parser("publish", help="publish to the enrolled tracker; a published spec enters its enabled pipeline")
    p.add_argument("name", help="enrolled destination repo name")
    p.add_argument("kind", choices=("spec", "child", "follow-up"))
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True, help="Markdown file, or - for stdin")
    p.add_argument("--key", required=True, help="stable request key; use the same key to reconcile a retry")
    p.add_argument("--draft", action="store_true", help="file a spec without authorizing pickup")
    p.add_argument("--parent", default="", help="child's parent in the same destination: GitHub number or Jira key")
    p.add_argument("--source", default="", help="follow-up's source issue URL")
    p.set_defaults(fn=cmd_repo)

    p = actions.add_parser("publish-pair", help="publish two linked specs together or only the ready one")
    p.add_argument("--spec-file", required=True, help="JSON file describing the two specs, or - for stdin")
    p.add_argument("--key", required=True, help="stable request key; reuse to reconcile a partial publish")
    p.add_argument("--ready-only", default="", help="publish only the named role or repo from the pair")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_repo)


def register(sub) -> None:
    repo_commands(sub)
    p = sub.add_parser("init", help="write the default global config")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = status_arguments(sub.add_parser("status", help="settings, heartbeat, alerts, recent runs; with --json, "
                                                       "the live runs or one run's steps as JSON lines"))
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("install", help="run arete serve at login: one launchd agent on a Mac, one systemd user unit on "
                                       "Linux; retires the old per loop agents")
    p.add_argument("--dry-run", action="store_true", help="print the agent or unit, change nothing")
    p.add_argument("--platform", choices=("mac", "linux"), default="", help="with --dry-run: print for this platform")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("uninstall", help="stop arete serve and remove its agent or unit")
    p.set_defaults(fn=cmd_uninstall)

    p = sub.add_parser("migrate", help="apply pending database migrations")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--no-contracts", action="store_true", help="stop before the first contract migration")
    p.add_argument("--workspace-snapshots", type=Path, metavar="DIR",
                   help="hand off or retry legacy workspace snapshots from this directory")
    mode.add_argument("--retire-quarantined-workspace", metavar="NAME",
                      help="retire a workspace quarantine after preserving and moving the workspace aside")
    mode.add_argument("--retry-quarantined-workspace", metavar="NAME",
                      help="retry a repaired legacy snapshot for an idle quarantined workspace")
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("prompts", help="validate prompt templates and list their required inputs, without model calls")
    p.add_argument("name", nargs="?", default="", help="one template's file stem; omit to inspect every template")
    p.add_argument("--json", action="store_true", help="one JSON object per template, including validation errors")
    p.set_defaults(fn=cmd_prompts, local_read=True)

    p = sub.add_parser("doctor", help="read only check of arete's own state: cost drift, orphan rows, stray folders, "
                                      "stuck work, the database and its backup; it reports and never repairs")
    p.set_defaults(fn=cmd_doctor, local_read=True)

    p = sub.add_parser("dashboard", help="serve the dashboard to its owner on the tailnet")
    p.add_argument("--port", type=int, default=None, help="the port to listen on; the config's [dashboard] port by default")
    p.set_defaults(fn=cmd_dashboard)
