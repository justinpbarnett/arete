from __future__ import annotations

import sys
from collections.abc import Iterable

from .. import paths
from ..store import runs as run_store
from .common import print_lines, steering


def cmd_merged(args) -> int:
    from .. import mergecheck

    return mergecheck.command(args)


def cmd_merge_check(args) -> int:
    from .. import mergecheck

    return mergecheck.command(args)


def cmd_run(args) -> int:
    from .. import client, pipeline

    if not (args.local or args.dry_run):
        try:
            started = client.call("POST", "/api/tasks", {"repo": args.repo, "ticket": args.issue, "force": args.force})
        except client.Down:
            started = None
        except client.Failed as e:
            return client.failed(e)
        if started is not None:
            return client.steered(lambda: run_followed(started))
    return pipeline.run_issue(args.repo, args.issue, dry_run=args.dry_run, force=args.force)


def run_followed(started: dict) -> int:
    from .. import client, pipeline, run_lines

    client.emit(started)
    if not (run_id := started.get("run")):
        return 0 if started.get("state") == "queued" else 1
    try:
        for line in client.followed(run_id):
            client.emit(line)
            if line.get("event") == run_lines.END:
                return 0 if line.get("status") in pipeline.SUCCEEDED else 1
    except KeyboardInterrupt:
        print(f"detached; run {run_id} keeps going; `arete stop {run_id}` stops it", file=sys.stderr)
        return 130
    return 1


def local_lines(run_id: str, start: int, follow: bool) -> Iterable[dict] | None:
    from .. import run_lines

    if not run_lines.path(run_id).exists() and not run_store.run_get(run_id):
        return None
    return (run_lines.follow(run_id, start) if follow
            else (line for line in run_lines.read(run_id) if run_lines.seq_of(line) > start))


def cmd_lines(args) -> int:
    from .. import client

    def printed() -> int:
        try:
            found = (client.followed if args.follow else client.events)(args.run_id, args.start)
        except client.Down:
            if (found := local_lines(args.run_id, args.start, args.follow)) is None:
                client.emit({"error": f"no run {args.run_id}"})
                return 1
        for line in found:
            if not args.step or line.get("step") == args.step:
                client.emit(line)
        return 0

    return client.steered(printed)


def cmd_wait(args) -> int:
    from .. import client

    return client.steered(lambda: client.wait(args.run_id))


def cmd_answer(args) -> int:
    from .. import client

    return client.answer(args.run_id, " ".join(args.text), args.json, args.ask)


def cmd_resume(args) -> int:
    from .. import client

    return client.say("POST", client.run_path(args.run_id, "resume"), {"step": args.step} if args.step else {})


def cmd_repair(args) -> int:
    from .. import client

    return client.say("POST", client.run_path(args.run_id, "repair"), {})


def cmd_step(args) -> int:
    from urllib.parse import quote

    from .. import client

    sets = {}
    for setting in args.sets:
        key, eq, value = setting.partition("=")
        if not eq or not key.strip():
            client.emit({"error": f"write each setting as key=value, not '{setting}'"})
            return 1
        sets[key.strip()] = value
    return client.say("POST", client.run_path(args.run_id, f"steps/{quote(args.step, safe='')}"), {"set": sets})


def cmd_stop(args) -> int:
    from .. import client

    return client.say("POST", client.run_path(args.run_id, "stop"))


def cmd_replay(args) -> int:
    from .. import replay

    try:
        lines = replay.sample(paths.expand(args.folder), adw=args.adw)
    except (OSError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1
    print_lines(lines)
    return 0


def register(sub) -> None:
    p = sub.add_parser("merged", help="print newly merged pipeline PRs as JSON events")
    p.add_argument("--since-cursor", action="store_true", help="omit PRs already seen by this command source")
    p.set_defaults(fn=cmd_merged)

    p = sub.add_parser("merge-check", help="use the merged origin base for landed pipeline PRs")
    p.add_argument("repo", nargs="?", default="", help="configured repository; otherwise read events on stdin")
    p.add_argument("--dry-run", action="store_true", help="preview saved checks without starting anything")
    p.set_defaults(fn=cmd_merge_check)

    p = sub.add_parser("run", help="loop 3: run the pipeline on one ticket")
    p.add_argument("repo")
    p.add_argument("issue", help="an issue number (35), a Jira key (ABC-1234) or a task id (t-1a2b)")
    p.add_argument("--dry-run", action="store_true", help="plan and build locally, no push, PR or tracker writes")
    p.add_argument("--force", action="store_true", help="run a ticket the approval rules would skip")
    p.add_argument("--local", action="store_true", help="run in this process even when the server is up; the server "
                                                        "and the poller start runs this way")
    p.set_defaults(fn=cmd_run)

    p = steering(sub.add_parser("resume", help="continue a run that ended interrupted, stopped or failed, through the "
                                               "server"))
    p.add_argument("run_id")
    p.add_argument("--step", default="", help="run this step and every step after it again")
    p.set_defaults(fn=cmd_resume)

    p = steering(sub.add_parser("repair", help="repair the open held PR of a run from its saved internal review, "
                                               "through the server"))
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_repair)

    p = steering(sub.add_parser("step", help="run one step of a run that ended alone, through the server"))
    p.add_argument("run_id")
    p.add_argument("step")
    p.add_argument("sets", nargs="*", metavar="KEY=VALUE", help="override one of the step's settings")
    p.set_defaults(fn=cmd_step)

    p = steering(sub.add_parser("stop", help="stop a running run, through the server"))
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("replay", help="answer agent steps from files; `replay sample FOLDER` writes a fixture repo, its "
                                      "bare origin, a layer and an answer per ticket step")
    p.add_argument("action", choices=("sample",))
    p.add_argument("folder")
    p.add_argument("--adw", action="store_true", help="turn on [factory] path = \"adw\" in the sample layer")
    p.set_defaults(fn=cmd_replay)

    p = steering(sub.add_parser("wait", help="follow a run through the server, printing its step_end, question, "
                                             "error and report lines, until the report or a question"))
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_wait)

    p = steering(sub.add_parser("answer", help="answer the ask of a run that parked needs-info, through the server, "
                                               "which continues the run: per question on a terminal, or with --json "
                                               "BODY such as {\"recommended\": true}; a secret is typed at the prompt "
                                               "or piped, never given with --json; TEXT answers the run's text "
                                               "question the older way"))
    p.add_argument("run_id")
    p.add_argument("text", nargs="*")
    p.add_argument("--json", default="", metavar="BODY", help="the body of POST /api/asks/ID/answer")
    p.add_argument("--ask", default="", metavar="ID", help="the ask to answer when the run holds several")
    p.set_defaults(fn=cmd_answer)

    p = steering(sub.add_parser("lines", help="a run's run.jsonl, one JSON line per event: task, steps, heartbeats, "
                                              "questions, errors and the report"))
    p.add_argument("run_id")
    p.add_argument("-f", "--follow", action="store_true", help="keep printing new lines until the report line")
    p.add_argument("--from", dest="start", type=int, default=0, metavar="SEQ",
                   help="start after the line with this seq")
    p.add_argument("--step", default="", help="print only this step's lines")
    p.set_defaults(fn=cmd_lines)
