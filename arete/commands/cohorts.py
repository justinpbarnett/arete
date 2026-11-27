"""Owner-local measurement commands. Reports and suggestions are read-only."""

import json
import sqlite3
import sys


def register(sub):
    root = sub.add_parser("cohort", help="declare an arrival cohort, record observations, or report delivery")
    actions = root.add_subparsers(dest="action", required=True)
    declare = actions.add_parser("declare", help="freeze eligible work, arrival window, destination and deadline")
    declare.add_argument("name")
    declare.add_argument("--repo", required=True)
    declare.add_argument("--destination", required=True)
    declare.add_argument("--work-class", required=True)
    declare.add_argument("--opens", default="")
    declare.add_argument("--closes", required=True)
    declare.add_argument("--deadline-hours", type=int, required=True)
    source = declare.add_mutually_exclusive_group(required=True)
    source.add_argument("--ticket", action="append", default=[])
    source.add_argument("--label", default="")
    actions.add_parser("sync", help="catch up all registered GitHub cohorts without starting work")
    for name in ("report", "candidates"):
        actions.add_parser(name).add_argument("name")
    link = actions.add_parser("replacement", help="carry the original goal forward to a successor ticket")
    link.add_argument("name")
    link.add_argument("ticket")
    link.add_argument("successor")
    note = actions.add_parser("note", help="record a necessary intervention, audit, terminal failure or escaped defect")
    note.add_argument("name")
    note.add_argument("ticket")
    note.add_argument("kind", choices=["touch", "audit", "failed", "regression", "pending"])
    note.add_argument("--evidence", required=True)
    note.add_argument("--detail", required=True)
    note.add_argument("--at", default="")
    root.set_defaults(fn=command)


def command(args):
    from .. import cohorts, config, gh

    try:
        if args.action == "declare":
            repo = config.load_required().repo(args.repo)
            if args.label and (repo.tracker != "github" or not repo.github):
                raise ValueError("continuous label cohorts require a configured GitHub tracker")
            cohorts.declare(
                args.name,
                repo.name,
                args.destination,
                args.work_class,
                slug=repo.github,
                label=args.label,
                tickets=args.ticket,
                opens=args.opens,
                closes=args.closes,
                hours=args.deadline_hours,
            )
        elif args.action == "sync":
            cohorts.sync()
            return 0
        elif args.action == "replacement":
            cohorts.replacement(args.name, args.ticket, args.successor)
        elif args.action == "note":
            cohorts.note(args.name, args.ticket, args.kind, args.evidence, args.detail, args.at)
        result = cohorts.candidates(args.name) if args.action == "candidates" else cohorts.report(args.name)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, sqlite3.Error, OSError, gh.GhError) as error:
        print(f"arete cohort: {error}", file=sys.stderr)
        return 1
