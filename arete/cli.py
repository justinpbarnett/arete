from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from . import __version__, config, paths

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
probe = "haiku"

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
label = "com.arete.heartbeat"

[[job]]
label = "com.arete.poll"

[[job]]
label = "com.arete.mine"

[[job]]
label = "com.arete.audit"

[[job]]
label = "com.arete.eval"

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
    from . import pipeline, state, tracker, util

    cfg = config.load()
    hb = util.read_json(paths.HEARTBEAT_FILE, {})
    print(f"arete {__version__}  config {paths.CONFIG_FILE}")
    if hb:
        bad = [c for c in hb.get("checks", []) if not c["ok"]]
        print(f"heartbeat {hb['ts']}: {'all ok' if not bad else f'{len(bad)} failing'}")
        for c in bad:
            print(f"  {c['severity'].upper():<5} {c['key']:<38} {c['detail']}")
    else:
        print("heartbeat: never run")
    loops = util.read_json(paths.STATE_DIR / "loops.json", {})
    for name, v in sorted(loops.items()):
        print(f"loop {name:<8} {v['ts']}  {'ok' if v.get('ok', True) else 'FAILED ' + v.get('detail', '')}")
    al = state.alerts()
    if al:
        print("open alerts:")
        for a in al:
            print(f"  {a['key']}  issue #{a['issue'] or '-'}  since {a['opened']}")
    rs = state.runs(limit=args.limit)
    if rs:
        print("recent runs:")
        for r in rs:
            where = tracker.run_ticket_name(r) if r["issue"] else (r["repo"] or "")
            print(f"  {r['id']:<22} {r['kind']:<9} {where:<18} {r['status']:<9} {r['pr_url'] or r['summary'] or ''}"[:160])
    mem = state.proposals(status=["memory"], limit=10)
    if mem:
        print("memory facts the miner noticed (file them by hand, then `arete forget ID`):")
        for p in mem:
            print(f"  {p['id']:<4} {p['change']}"[:200])
    skipped = pipeline.skipped_tickets()
    if skipped:
        print("labeled tickets the pipeline will not run (`arete run REPO ID --force` runs one anyway):")
        for name, v in sorted(skipped.items()):
            print(f"  {name:<24} {v['why']}  since {v.get('since', '')}"[:220])
    pipeline_repos = [f"{r.name} ({r.tracker})" for r in cfg.repos if r.pipeline]
    print(f"pipeline repos: {', '.join(pipeline_repos) or 'none'}")
    return 0


def cmd_forget(args) -> int:
    from . import state

    with state.db() as c:
        c.execute("UPDATE proposals SET status = 'filed' WHERE id = ? AND status = 'memory'", (args.id,))
    return 0


def cmd_events(args) -> int:
    from . import hook

    evs = hook.read_events(args.hours)
    if args.raw:
        for e in evs[-args.limit:]:
            print(json.dumps(e))
        return 0
    c = Counter(f"{e.get('kind')}/{e.get('cls')}" for e in evs)
    print(f"{len(evs)} events in the last {args.hours:g}h")
    for k, n in c.most_common(30):
        print(f"  {n:>5}  {k}")
    return 0


def cmd_heartbeat(args) -> int:
    from . import heartbeat

    return heartbeat.run(dry_run=args.dry_run, force_probes=args.force_probes, quiet=args.quiet)


def cmd_hook(args) -> int:
    from . import hook

    return hook.main()


def cmd_install(args) -> int:
    from . import install

    return install.install(only=args.only, dry_run=args.dry_run)


def cmd_uninstall(args) -> int:
    from . import install

    return install.uninstall()


def cmd_run(args) -> int:
    from . import pipeline

    return pipeline.run_issue(args.repo, args.issue, dry_run=args.dry_run, force=args.force)


def cmd_jira_check(args) -> int:
    from . import jira, pipeline, tracker

    cfg = config.load()
    try:
        client = jira.Client.from_env(cfg.jira.env_file)
        me = client.me()
        print(f"jira {client.base} as {me.get('displayName') or me.get('emailAddress') or '?'}")
        routes = pipeline.jira_routes_by_project(cfg)
        for project, labels in sorted(routes.items()):
            jql = tracker.candidates_jql(project)
            n = len(client.search(jql, ["labels"], limit=100))
            print(f"  {project}: {n} labeled `{tracker.LABEL}` and waiting; routes {', '.join(sorted(labels))}")
    except jira.JiraError as e:
        print(f"jira failed: {e}")
        return 1
    print("jira ok")
    return 0


def cmd_poll(args) -> int:
    from . import pipeline

    return pipeline.poll(dry_run=args.dry_run)


def cmd_mine(args) -> int:
    from . import miner

    return miner.run(hours=args.hours, dry_run=args.dry_run, apply=not args.no_apply)


def cmd_audit(args) -> int:
    from . import auditor

    return auditor.run(dry_run=args.dry_run)


def cmd_eval(args) -> int:
    from . import evals

    if not args.repo:
        return evals.run_all(dry_run=args.dry_run)
    return evals.run(args.repo, task=args.task, ref=args.ref, dry_run=args.dry_run)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arete", description="arete: agents that keep running and keep getting better", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"arete {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write the default global config")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("status", help="heartbeat, alerts, recent runs")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("events", help="summarize hook events")
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--raw", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_events)

    p = sub.add_parser("heartbeat", help="loop 0: check jobs, probes, auth, hook, disk")
    p.add_argument("--dry-run", action="store_true", help="print actions instead of opening or closing alerts")
    p.add_argument("--force-probes", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_heartbeat)

    p = sub.add_parser("hook", help="Claude Code event hook (reads a payload on stdin)")
    p.set_defaults(fn=cmd_hook)

    p = sub.add_parser("install", help="write and load the launchd agents")
    p.add_argument("--only", nargs="*", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("uninstall", help="unload and remove the launchd agents")
    p.set_defaults(fn=cmd_uninstall)

    p = sub.add_parser("run", help="loop 3: run the pipeline on one ticket")
    p.add_argument("repo")
    p.add_argument("issue", help="an issue number (35) or a Jira key (ABC-1234)")
    p.add_argument("--dry-run", action="store_true", help="plan and build locally, no push, PR or tracker writes")
    p.add_argument("--force", action="store_true", help="run a ticket the approval rules would skip")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("poll", help="loop 3: run the pipeline on the oldest ready ticket in each pipeline repo")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_poll)

    p = sub.add_parser("mine", help="loop 1: mine transcripts into harness PRs")
    p.add_argument("--hours", type=float, default=None)
    p.add_argument("--dry-run", action="store_true", help="print proposals, change nothing")
    p.add_argument("--no-apply", action="store_true", help="record proposals but open no PRs")
    p.set_defaults(fn=cmd_mine)

    p = sub.add_parser("forget", help="mark a memory fact from the miner as filed")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_forget)

    p = sub.add_parser("jira-check", help="read Jira with the configured token and count waiting tickets")
    p.set_defaults(fn=cmd_jira_check)

    p = sub.add_parser("audit", help="loop 2: deterministic harness audit")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("eval", help="loop 4: run eval tasks (all repos with evals when REPO is left out)")
    p.add_argument("repo", nargs="?")
    p.add_argument("--task", default="")
    p.add_argument("--ref", default="", help="take the harness and tasks from this git ref, not the working copy")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_eval)

    args = ap.parse_args(argv)
    try:
        return args.fn(args) or 0
    except config.UnknownRepo as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
