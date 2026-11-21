from __future__ import annotations

import json
import sys
from collections import Counter

from .. import client, config, paths
from ..store import db as foundation
from ..store import incidents as incident_store
from .common import when


def cmd_events(args) -> int:
    from .. import tool_events

    evs = tool_events.read_events(args.hours)
    if args.raw:
        for e in evs[max(len(evs) - args.limit, 0):]:
            print(json.dumps(e))
        return 0
    c = Counter(f"{e.get('kind')}/{e.get('cls')}" for e in evs)
    print(f"{len(evs)} events in the last {args.hours:g}h")
    for k, n in c.most_common(30):
        print(f"  {n:>5}  {k}")
    return 0


def cmd_heartbeat(args) -> int:
    from .. import heartbeat

    return heartbeat.run(dry_run=args.dry_run, force_probes=args.force_probes, quiet=args.quiet)


def oncall_scan(args) -> int:
    from .. import oncall

    summary = oncall.scan(config.load())
    if summary is None:
        print(f"on-call is off; set [oncall] enabled = true in {paths.CONFIG_FILE}")
    elif "skipped" in summary:
        print(summary["skipped"])
    else:
        print(json.dumps(summary) if args.json else f"on-call: {oncall.said(summary)}")
    return 0


def oncall_toggle(args) -> int:
    client.call("POST", "/api/incidents/switch", {"on": args.action == "on"})
    print(f"on-call wakes and rechecks are {args.action}")
    return 0


def oncall_incident_action(args) -> int:
    if not args.id.isdigit():
        print(f"no incident {args.id}" if args.id else f"{args.action} needs an incident number", file=sys.stderr)
        return 1
    number = int(args.id)
    done = client.call("POST", f"/api/incidents/{number}/{args.action}")
    if args.action == "cancel":
        stopped = f"stopped run {done['stopped']}" if done["stopped"] else "no run was live"
        print(f"cancelled incident {number}: {stopped}; cooling down until {done['cooldown_until'] or '-'}")
    elif args.action == "dismiss":
        print(f"dismissed incident {number}")
    else:
        print(f"incident {number} is re-armed; the next scan may start a repair")
    return 0


def oncall_show(args) -> int:
    from .. import oncall

    found = incident_store.incident(int(args.id), connect=foundation.reader) if args.id.isdigit() else None
    if not found:
        print(f"no incident {args.id}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(found))
        return 0
    print(f"#{found['id']}  {found['state']}  {found['class']}  {found['source']}  x{found['count']}  "
          f"{found['first_seen']} to {found['last_seen']}")
    print(f"decision: {oncall.decision_line(found)}")
    if line := oncall.repair_line(found):
        print(f"repair: {line}")
    cfg = config.load()
    for n, r in enumerate(found["repairs"], 1):
        print(f"  attempt {n} of {cfg.oncall.max_attempts}: task {r['task'] or '-'}, run {oncall.last_run_id(r)}, "
              f"{r['started']}, {r['outcome'] or ('running' if r['task'] else 'starting')}")
    for name, a in found["answers"].items():
        print(f"  {name}: {'yes' if a['value'] else 'no'}, confidence {a['confidence']:.2f}")
    print(f"{found['signature']}\nevidence:\n{found['evidence']}\nnotes:")
    for note in found["notes"]:
        print(f"{note['ts']}  {note['note']}")
    return 0


def oncall_list(args) -> int:
    from .. import oncall

    rows = incident_store.incidents(connect=foundation.reader)
    if args.json:
        for row in rows:
            print(json.dumps(row))
        return 0
    for r in rows:
        print(f"#{r['id']:<4} {r['phase'] or r['state']:<7} {r['class']:<13} {when(r['first_seen'])}  "
              f"{when(r['last_seen'])}  x{r['count']:<4} {r['signature']}")
        print(f"      {oncall.decision_line(r)}")
        if line := oncall.repair_line(r):
            print(f"      repair: {line}")
    cfg = config.load()
    now = oncall.status(cfg, connect=foundation.reader)
    if not now["enabled"]:
        print(f"on-call is off; `[oncall] enabled = true` in {paths.CONFIG_FILE} turns it on")
    else:
        last = "no scan yet"
        if now["last_scan"]:
            last = f"last scan {when(now['last_scan'])}" + ("" if now["ok"] else f" failed: {now['detail']}")
        print(f"on-call is on; {last}; {now['expected']} expected events counted")
    if now["wakes"] == "off":
        print("wakes and rechecks are off; `arete oncall on` turns them on")
    return 0


ONCALL_ACTIONS = {"scan": oncall_scan, "off": oncall_toggle, "on": oncall_toggle, "cancel": oncall_incident_action,
                  "dismiss": oncall_incident_action, "retry": oncall_incident_action, "show": oncall_show,
                  "list": oncall_list}


def cmd_oncall(args) -> int:
    return ONCALL_ACTIONS[args.action](args)


def cmd_hook(args) -> int:
    from .. import tool_events

    return tool_events.main()


def cmd_guard(args) -> int:
    from .. import guard, scratch

    scratch.HOOK.update(root=args.scratch, id=args.scratch_id, saves=args.scratch_saves)
    guard.LOGIN_FOLDERS = (*guard.LOGIN_FOLDERS, *(paths.expand(path) for path in getattr(args, "closed", ())))
    return guard.main(args.judge, args.open)


def cmd_upload_guard(args) -> int:
    try:
        from .. import uploads

        return uploads.main()
    except Exception:
        print("Arete's upload guard failed, so it refused the upload.", file=sys.stderr)
        return 2


def scratch_hook(p) -> None:
    p.add_argument("--scratch", default="", help="the verification attempt's scratch folder, set by arete")
    p.add_argument("--scratch-id", default="", help="selector for protected harness authority; never proves ownership")
    p.add_argument("--scratch-saves", default="", help="must match the authenticated evidence destination")


def cmd_codex_guard(args) -> int:
    from .. import codex, routing, scratch

    scratch.HOOK.update(root=args.scratch, id=args.scratch_id, saves=args.scratch_saves)
    return codex.guard_main(args.judge, routing.NO_GIT_WRITES, args.open)


def cmd_mcp_ask(args) -> int:
    from .. import owner_asks

    return owner_asks.check(sys.stdin, sys.stdout) if args.check else owner_asks.serve(sys.stdin, sys.stdout)


def cmd_background(args) -> int:
    from .. import background

    return background.main()


def cmd_ask(args) -> int:
    from .. import jev_ask

    return jev_ask.main(args)


def cmd_jev(args) -> int:
    if args.action != "report":
        from ..lab import calibrate

        return calibrate.main(args)

    from .. import jev_verdicts

    data = jev_verdicts.summary(args.days, config.load())
    print(json.dumps(data, indent=2) if args.json else jev_verdicts.render(data))
    return 0


def register(sub) -> None:
    p = sub.add_parser("hook", help="Claude Code event hook (reads a payload on stdin)")
    p.set_defaults(fn=cmd_hook)

    p = sub.add_parser("guard", help="PreToolUse hook for arete's agents: enforces the directory floor and secret rules; "
                                     "with --judge, "
                                     "stops shell commands git cannot undo (reads a payload on stdin)")
    p.add_argument("--judge", action="store_true")
    p.add_argument("--open", action="append", default=[])
    p.add_argument("--closed", action="append", default=[], help="add a protected grading-context folder")
    scratch_hook(p)
    p.set_defaults(fn=cmd_guard)

    p = sub.add_parser("upload-guard", help="PreToolUse hook for the verifier's browser_file_upload: allows only files "
                                            "in the run's evidence or browser folder (reads a payload on stdin)")
    p.set_defaults(fn=cmd_upload_guard)

    p = sub.add_parser("codex-guard", help="PreToolUse hook for arete's codex agents: enforces the directory floor, "
                                           "git write and secret rules; with --judge, asks JEV (reads a payload on stdin)")
    p.add_argument("--judge", action="store_true")
    p.add_argument("--open", action="append", default=[])
    scratch_hook(p)
    p.set_defaults(fn=cmd_codex_guard)

    p = sub.add_parser("mcp-ask", help="stdio MCP server that gives arete's agents the ask_owner tool; with --check, "
                                       "validates one call read from stdin")
    p.add_argument("--check", action="store_true")
    p.set_defaults(fn=cmd_mcp_ask, local_read=lambda args: args.check)

    p = sub.add_parser("background", help="Stop hook for arete's claude agents: an agent that answers while commands it "
                                          "started in the background still run is told to stop them and answer again "
                                          "(reads a payload on stdin)")
    p.set_defaults(fn=cmd_background)

    p = sub.add_parser("ask", help="put a yes or no question, a choice or a rating to JEV over piped text or files")
    p.add_argument("question")
    p.add_argument("paths", nargs="*", help="files, folders or globs; each file gets its own answer (default: stdin)")
    p.add_argument("--choice", nargs="+", metavar="LABEL[=ABOUT]", help="answer with one of these labels")
    p.add_argument("--score", nargs="+", metavar="LEVEL", help="rate on these levels, lowest first")
    p.add_argument("-r", "--recursive", action="store_true", help="look inside folders all the way down")
    p.add_argument("--only", metavar="ANSWER", help="print only the rows with this answer, and any errors")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("jev", help="what came of JEV's verdicts: holds by question, close holds, near misses, reverts; "
                                   "compare decision models offline")
    p.add_argument("action", choices=("report", "compare", "sample"))
    p.add_argument("target", nargs="?", default="", help="compare: the spec file; sample: a new empty folder")
    p.add_argument("--out", metavar="DIR", help="compare: where to write proposal.json and predictions.jsonl "
                                                "(default: out next to the spec)")
    p.add_argument("--days", type=float, default=7, help="how far back to read (default 7)")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(fn=cmd_jev, local_read=lambda args: args.action == "report")

    p = sub.add_parser("heartbeat", help="loop 0: check jobs, probes, auth, hook, disk")
    p.add_argument("--dry-run", action="store_true", help="print actions instead of opening or closing alerts")
    p.add_argument("--force-probes", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_heartbeat)

    p = sub.add_parser("events", help="summarize hook events")
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--raw", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_events, local_read=True)

    p = sub.add_parser("oncall", help="the on-call watcher: incidents found in logs, run files and the heartbeat")
    p.add_argument("action", nargs="?", default="list", choices=("list", "show", "scan", "cancel", "dismiss", "retry", "off", "on"))
    p.add_argument("id", nargs="?", default="", help="with show, cancel, dismiss or retry: the incident number")
    p.add_argument("--json", action="store_true", help="one JSON line per incident")
    p.set_defaults(fn=cmd_oncall, local_read=lambda args: args.action in ("list", "show"))
