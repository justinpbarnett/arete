from __future__ import annotations

import json
import shutil
import signal
import sys
import textwrap
from typing import NoReturn

from .. import config
from .common import Parser, print_lines, steering, when


def task_shown(task_id: str, repo: str, one_line: bool) -> int:
    from urllib.parse import quote

    from .. import client

    def show(found: dict) -> None:
        if one_line:
            client.emit(found)
        else:
            print(json.dumps(found, ensure_ascii=False, default=str, indent=2), flush=True)

    try:
        show(client.call("GET", f"/api/tasks/{quote(task_id, safe='')}"
                         + (f"?repo={quote(repo, safe='')}" if repo else "")))
        return 0
    except client.Down:
        from .. import control, gh, jira, tasks

        try:
            show(tasks.view(task_id, repo))
        except (control.NotFound, control.Refused, gh.GhError, jira.JiraError) as e:
            client.emit({"error": str(e)})
            return 1
    return 0


def task_add(args) -> int:
    from .. import chat
    from ..store import tasks as tasks_store

    body = " ".join(args.body).strip()
    if not body:
        print('arete task add --repo NAME "what to do"', file=sys.stderr)
        return 2
    config.load().repo(args.repo)
    print(tasks_store.task_add(args.repo, body, title=args.title.strip() or chat.title_of(body), kind=args.kind))
    return 0


def task_list(args) -> int:
    from ..store import tasks as tasks_store

    print_lines(json.dumps(row, ensure_ascii=False) for row in tasks_store.tasks(args.repo, args.state))
    return 0


def task_show(args) -> int:
    from .. import client

    return client.steered(lambda: task_shown(args.id, args.repo, args.json))


def existing_task(args) -> str | None:
    from ..store import tasks as tasks_store

    tid = args.id.strip().lower()
    if tasks_store.task_get(tid) is None:
        print(f"no task {tid}", file=sys.stderr)
        return None
    return tid


def task_reply(args) -> int:
    from ..store import tasks as tasks_store

    if (tid := existing_task(args)) is None:
        return 1
    text = " ".join(args.text).strip()
    if not text:
        print('arete task reply ID "your reply"', file=sys.stderr)
        return 2
    tasks_store.task_comment(tid, "owner", text)
    print(f"{tid}: reply saved")
    return 0


def task_close(args) -> int:
    from ..store import tasks as tasks_store

    if (tid := existing_task(args)) is None:
        return 1
    tasks_store.task_set_state(tid, "closed")
    print(f"{tid}: closed")
    return 0


def task_start(args) -> int:
    from .. import client

    return client.say("POST", "/api/tasks", {"repo": args.repo, "prompt": " ".join(args.prompt),
                                             "title": args.title, "kind": args.kind})


def task_run(args) -> int:
    from .. import client

    return client.say("POST", "/api/tasks", {"repo": args.repo, "ticket": args.ticket})


def cmd_forget(args) -> int:
    from ..store import approvals as approvals_store

    if not approvals_store.forget_proposal(args.id):
        print(f"no open proposal #{args.id}")
        return 1
    return 0


def cmd_file_rule(args) -> int:
    from .. import miner

    try:
        print(miner.file_rule(args.id))
    except LookupError as e:
        print(e)
        return 1
    return 0


def cmd_reject_rule(args) -> int:
    from ..store import approvals as approvals_store

    if not approvals_store.move_proposal(args.id, ("approve",), "rejected"):
        print(f"#{args.id} is not a rule waiting for approval")
        return 1
    return 0


def approval_notes(a: dict) -> list[str]:
    failed = [a["failed"], f"`arete approve {a['id']}` tries again, `--reject` drops it"]
    review = a["review"]
    return [*(failed if a["failed"] else []),
            *(note for note in (f"held: {a['held']}" if a["held"] else "",
                                f"review by {review['by']}: {review['said']}" if review else "") if note)]


def stop_on_sigterm(signum, frame) -> NoReturn:
    raise SystemExit(128 + signal.SIGTERM)


def cmd_approve(args) -> int:
    from .. import control, delivery

    if args.id is None:
        pending = delivery.waiting()
        for a in pending:
            print(textwrap.fill(f"#{a['id']:<4} {a['loop']:<20} {a['repo'] or 'none':<12} {when(a['created'])}  "
                                f"{a['plain_summary'] or a['what']}", shutil.get_terminal_size().columns,
                                subsequent_indent=" " * 6, break_on_hyphens=False, max_lines=4, placeholder=" …"))
            for line in approval_notes(a):
                print(textwrap.fill(line, shutil.get_terminal_size().columns, initial_indent=" " * 6,
                                    subsequent_indent=" " * 8, break_on_hyphens=False))
        if not pending:
            print("nothing waiting for approval")
        return 0
    before = signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        print(json.dumps(control.decide(args.id, not args.reject)))
    finally:
        signal.signal(signal.SIGTERM, before)
    return 0


def register(sub) -> None:
    p = sub.add_parser("approve", help="list emits waiting for approval, or approve one by id; "
                                       "approving a failed one tries the delivery again")
    p.add_argument("id", type=int, nargs="?", default=None)
    p.add_argument("--reject", action="store_true")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("task", help="local tasks, which have no tracker issue: add, list, show, reply to and close them; "
                                    "start one from a prompt or a ticket through the server")
    tsub = p.add_subparsers(dest="action", required=True, parser_class=Parser)
    t = tsub.add_parser("add", help="add a task to a repo and print its id")
    t.add_argument("--repo", required=True, help="a repo name from the config")
    t.add_argument("--title", default="", help="the body's first line by default")
    t.add_argument("--kind", default="feat", choices=config.KINDS)
    t.add_argument("body", nargs="+", help="what to do")
    t.set_defaults(fn=task_add)
    t = tsub.add_parser("list", help="one JSON line per task, oldest first")
    t.add_argument("--repo", default="")
    t.add_argument("--state", default="", choices=config.TASK_STATES)
    t.set_defaults(fn=task_list)
    t = steering(tsub.add_parser("show", help="a task or a tracker item with its runs and children, as indented JSON"))
    t.add_argument("id")
    t.add_argument("--repo", default="", help="the repo of a tracker item no run names yet")
    t.add_argument("--json", action="store_true", help="print it as one JSON line")
    t.set_defaults(fn=task_show)
    t = tsub.add_parser("reply", help="answer a task's question as its owner")
    t.add_argument("id")
    t.add_argument("text", nargs="+")
    t.set_defaults(fn=task_reply)
    t = tsub.add_parser("close", help="close a task")
    t.add_argument("id")
    t.set_defaults(fn=task_close)
    t = steering(tsub.add_parser("start", help="add a task from a prompt and start it through the server; one JSON "
                                               "line"))
    t.add_argument("--repo", required=True, help="a repo name from the config")
    t.add_argument("--kind", default="feat", choices=config.KINDS)
    t.add_argument("--title", default="", help="the prompt's first line by default")
    t.add_argument("prompt", nargs="+", help="what to do")
    t.set_defaults(fn=task_start)
    t = steering(tsub.add_parser("run", help="start a tracker item or a task through the server, past its waits; one "
                                             "JSON line"))
    t.add_argument("repo")
    t.add_argument("ticket", help="an issue number, a Jira key or a task id")
    t.set_defaults(fn=task_run)

    p = sub.add_parser("forget", help="mark a memory fact or hand edit from the miner as done")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_forget)

    p = sub.add_parser("file-rule", help="write an approved global rule from the miner into the global CLAUDE.md and push it")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_file_rule)

    p = sub.add_parser("reject-rule", help="drop a global rule from the miner, so it is not proposed again")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_reject_rule)
