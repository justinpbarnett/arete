from __future__ import annotations

import json
import shutil
import sys
import textwrap
from urllib.parse import quote

from .. import client, config
from ..store import db as foundation
from .common import Parser, print_lines, steering, when


def task_shown(task_id: str, repo: str, one_line: bool, offline=False) -> int:
    def show(found: dict) -> None:
        if one_line:
            client.emit(found)
        else:
            print(json.dumps(found, ensure_ascii=False, default=str, indent=2), flush=True)

    if offline:
        from .. import control, paths, tasks, tracker

        try:
            if not tracker.is_task_id(task_id.strip()) or not paths.DB_FILE.exists():
                raise control.NotFound(f"no local task {task_id}")
            show({**tasks.view(task_id, repo), "source": "local"})
        except (control.NotFound, foundation.NoDatabase) as e:
            client.emit({"error": str(e), "source": "local"})
            return 1
    else:
        show(client.call("GET", f"/api/tasks/{quote(task_id, safe='')}"
                         + (f"?repo={quote(repo, safe='')}" if repo else "")))
    return 0


def task_add(args) -> int:
    body = " ".join(args.body).strip()
    if not body:
        print('arete task add --repo NAME "what to do"', file=sys.stderr)
        return 2
    answer = client.call("POST", "/api/v1/tasks", {"repo": args.repo, "body": body, "title": args.title,
                                                   "kind": args.kind})
    print(client.field(client.field(answer, "task"), "id", "task.id"))
    return 0


def task_list(args) -> int:
    rows = client.field(client.call("GET", "/api/v1/tasks"), "tasks")
    print_lines(json.dumps(row, ensure_ascii=False) for row in rows
                if (not args.repo or row["repo"] == args.repo) and (not args.state or row["state"] == args.state))
    return 0


def task_show(args) -> int:
    return client.steered(lambda: task_shown(args.id, args.repo, args.json, args.offline))


def task_reply(args) -> int:
    tid = args.id.strip().lower()
    text = " ".join(args.text).strip()
    if not text:
        print('arete task reply ID "your reply"', file=sys.stderr)
        return 2
    client.call("POST", f"/api/v1/tasks/{quote(tid, safe='')}/comments", {"body": text})
    print(f"{tid}: reply saved")
    return 0


def task_close(args) -> int:
    tid = args.id.strip().lower()
    client.call("POST", f"/api/v1/tasks/{quote(tid, safe='')}/close")
    print(f"{tid}: closed")
    return 0


def task_start(args) -> int:
    return client.steered(lambda: client.emit(client.start_task(
        {"repo": args.repo, "prompt": " ".join(args.prompt), "title": args.title, "kind": args.kind}, args.key)) or 0)


def task_run(args) -> int:
    return client.steered(lambda: client.emit(client.start_task({"repo": args.repo, "ticket": args.ticket}, args.key)) or 0)


def cmd_forget(args) -> int:
    client.call("POST", f"/api/proposals/{args.id}/done")
    return 0


def cmd_file_rule(args) -> int:
    if not args.local:
        return client.say("POST", f"/api/proposals/{args.id}/approve")
    from .. import control, miner

    control.require_worker("file-rule", development=True)
    try:
        print(miner.file_rule(args.id))
    except LookupError as e:
        print(e)
        return 1
    return 0


def cmd_reject_rule(args) -> int:
    client.call("POST", f"/api/proposals/{args.id}/reject")
    return 0


def approval_notes(a: dict) -> list[str]:
    failed = [a["failed"], f"`arete approve {a['id']}` tries again, `--reject` drops it"]
    review = a["review"]
    return [*(failed if a["failed"] else []),
            *(note for note in (f"held: {a['held']}" if a["held"] else "",
                                f"review by {review['by']}: {review['said']}" if review else "") if note)]


def cmd_approve(args) -> int:
    from .. import delivery, paths

    if args.id is None:
        pending = delivery.waiting(connect=foundation.reader) if paths.DB_FILE.exists() else []
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
    return client.say("POST", f"/api/approvals/{args.id}", {"approve": not args.reject})


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
    t.add_argument("--offline", action="store_true", help="read a task from the local store")
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
    t.add_argument("--key", default=None, help="reuse an identical task-start request safely")
    t = steering(tsub.add_parser("run", help="start a tracker item or a task through the server, past its waits; one "
                                             "JSON line"))
    t.add_argument("repo")
    t.add_argument("ticket", help="an issue number, a Jira key or a task id")
    t.set_defaults(fn=task_run)
    t.add_argument("--key", default=None, help="reuse an identical task-start request safely")

    p = sub.add_parser("forget", help="mark a memory fact or hand edit from the miner as done")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_forget)

    p = sub.add_parser("file-rule", help="write an approved global rule from the miner into the global CLAUDE.md and push it")
    p.add_argument("id", type=int)
    p.add_argument("--local", action="store_true", help="carry out the server's detached rule filing")
    p.set_defaults(fn=cmd_file_rule)

    p = sub.add_parser("reject-rule", help="drop a global rule from the miner, so it is not proposed again")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_reject_rule)
