from __future__ import annotations

import json
import os
import sys

from .. import config, paths


def piped_events() -> list[dict]:
    if not os.environ.get("ARETE_LOOP") or sys.stdin is None:
        return []
    return [json.loads(line) for line in sys.stdin if line.strip()]


def cmd_jira_check(args) -> int:
    from .. import jira, poll, tracker

    cfg = config.load()
    try:
        client = jira.Client.from_env(cfg.jira.env_file)
        me = client.me()
        print(f"jira {client.base} as {me.get('displayName') or me.get('emailAddress') or '?'}")
        routes = poll.jira_routes_by_project(cfg)
        targets = [(f"[jira.transitions] {k}", k, v) for k, v in cfg.jira.transitions.items()]
        targets += [(f"[jira.research] {k}", f"research {k}", v) for k, v in cfg.jira.research.items()]
        targets += [(f"[jira.landed] {k}", f"landed on {k}", v) for k, v in cfg.jira.landed.items()]
        for project, labels in sorted(routes.items()):
            jql = tracker.candidates_jql(project)
            n = len(client.search(jql, ["labels"], limit=100))
            print(f"  {project}: {n} labeled `{tracker.LABEL}` and waiting; routes {', '.join(sorted(labels))}")
            known = {s.lower() for s in client.statuses(project)}
            for key, _, name in targets:
                if name.lower() not in known:
                    print(f"  {project}: no status named '{name}' for {key}; it moves a ticket only where a "
                          "transition has that name")
            if targets:
                print(f"  {project}: moves " + ", ".join(f"{when} to {name}" for _, when, name in targets))
    except jira.JiraError as e:
        print(f"jira failed: {e}")
        return 1
    print("jira ok")
    return 0


def cmd_poll(args) -> int:
    from .. import poll

    return poll.poll(dry_run=args.dry_run)


def cmd_mine(args) -> int:
    from .. import miner

    return miner.run(hours=args.hours, dry_run=args.dry_run, apply=not args.no_apply)


def cmd_audit(args) -> int:
    from .. import auditor

    return auditor.run(dry_run=args.dry_run)


def cmd_eval(args) -> int:
    from .. import eval_mine, evals

    words = args.target
    if words[:1] == ["mine"] and len(words) <= 2:
        return eval_mine.run(words[1] if len(words) == 2 else "", limit=args.limit, dry_run=args.dry_run)
    if len(words) > 1:
        print("usage: arete eval [REPO] or arete eval mine [REPO]", file=sys.stderr)
        return 2
    if not words:
        if given := [flag for flag, value in (("--task", args.task), ("--ref", args.ref), ("--prompts", args.prompts)) if value]:
            print(f"arete eval {' '.join(given)} needs a REPO", file=sys.stderr)
            return 2
        return evals.run_all(dry_run=args.dry_run)
    prompts_dir = paths.expand(args.prompts).resolve() if args.prompts else None
    prompts = evals.Prompts(prompts_dir, paths.short_path(prompts_dir)) if prompts_dir else None
    return evals.run(words[0], task=args.task, ref=args.ref, dry_run=args.dry_run, prompts=prompts)


def cmd_triage(args) -> int:
    from .. import triage

    if args.refresh or args.dry_run:
        code = triage.run(dry_run=args.dry_run)
        if args.dry_run:
            return code
    else:
        code = 0
    if args.ref:
        shown = triage.ticket(args.ref)
        if shown is None:
            print(f"no triage row for {args.ref}; `arete triage` lists them", file=sys.stderr)
            return 1
        print(shown)
    else:
        print(triage.board())
    return code


def cmd_research(args) -> int:
    from .. import research, research_search

    if args.ref and args.evidence:
        found = research_search.shown(args.ref)
        if found is None:
            print(f"no search evidence for {args.ref}", file=sys.stderr)
            return 1
        print(found)
        return 0
    if args.next or (args.dry_run and not args.ref):
        return research.next_due(dry_run=args.dry_run, asked=piped_events())
    if args.ref and args.run:
        return research.run_now(args.ref, force=args.force, dry_run=args.dry_run)
    if args.ref:
        shown = research.shown(args.ref)
        if shown is None:
            print(f"no research for {args.ref}; `arete research` lists them", file=sys.stderr)
            return 1
        print(shown)
        return 0
    print(research.listing())
    return 0


def register(sub) -> None:
    p = sub.add_parser("triage", help="loop 5: the triage board of your open tickets without the agent label")
    p.add_argument("ref", nargs="?", default="", metavar="REF", help="show one ticket in full")
    p.add_argument("--refresh", action="store_true", help="triage what changed, then show the board")
    p.add_argument("--dry-run", action="store_true", help="list what a refresh would triage; no model calls, no writes")
    p.set_defaults(fn=cmd_triage)

    p = sub.add_parser("research", help="the reports arete writes for research tickets on the triage board")
    p.add_argument("ref", nargs="?", default="", metavar="REF", help="print one ticket's report")
    p.add_argument("--evidence", action="store_true", help="with REF: print the sources its search workers found")
    p.add_argument("--run", action="store_true", help="with REF: research it now")
    p.add_argument("--force", action="store_true", help="with --run: skip the JEV injection screen")
    p.add_argument("--next", action="store_true", help="research the next ticket due; what the research loop runs")
    p.add_argument("--dry-run", action="store_true",
                   help="say which ticket is due next, or with REF --run whether it would run; no model calls, no writes")
    p.set_defaults(fn=cmd_research)

    p = sub.add_parser("poll", help="loop 3: run the pipeline on the oldest ready ticket in each pipeline repo")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_poll)

    p = sub.add_parser("mine", help="loop 1: mine transcripts and recorded runs into harness PRs")
    p.add_argument("--hours", type=float, default=None,
                   help="window for every input, never moves the checkpoint; without it every input is read since the last good mine")
    p.add_argument("--dry-run", action="store_true", help="print proposals, change nothing")
    p.add_argument("--no-apply", action="store_true", help="record proposals but open no PRs")
    p.set_defaults(fn=cmd_mine)

    p = sub.add_parser("audit", help="loop 2: deterministic harness audit")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("jira-check", help="read Jira with the configured token and count waiting tickets")
    p.set_defaults(fn=cmd_jira_check)

    p = sub.add_parser("eval", help="loop 4: run eval tasks (all repos with evals when REPO is left out); "
                                    "`eval mine [REPO]` writes tasks from merged arete PRs")
    p.add_argument("target", nargs="*", metavar="REPO | mine [REPO]")
    p.add_argument("--task", default="")
    p.add_argument("--ref", default="", help="take the harness and tasks from this git ref, not the working copy")
    p.add_argument("--prompts", default="", metavar="DIR",
                   help="run the plan, build and fix steps with the prompts in DIR; the grader keeps the installed one")
    p.add_argument("--limit", type=int, default=3, help="with mine: the most merged PRs to mine in one go")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_eval)
