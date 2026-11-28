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
        from .. import control, tasks, tracker

        try:
            if not tracker.is_task_id(task_id.strip()):
                raise control.NotFound(f"no local task {task_id}")
            client.emit({**tasks.view(task_id, repo), "source": "local"})
        except control.NotFound as e:
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
    lines = [json.dumps(row, ensure_ascii=False) for row in rows
             if (not args.repo or row["repo"] == args.repo) and (not args.state or row["state"] == args.state)]
    print_lines(lines)
    return 0


def task_show(args) -> int:
    return client.steered(lambda: task_shown(args.id, args.repo, args.json, args.offline))


def task_reply(args) -> int:
    tid = args.id.strip()
    text = " ".join(args.text).strip()
    if not text:
        print('arete task reply ID "your reply"', file=sys.stderr)
        return 2
    client.call("POST", f"/api/v1/tasks/{quote(tid, safe='')}/comments", {"body": text})
    print(f"{tid}: reply saved")
    return 0


def task_close(args) -> int:
    tid = args.id.strip()
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


def outbox_notes(item: dict) -> list[str]:
    notes: list[str] = []
    if item["state"] == "failed":
        if item.get("error"):
            notes.append(str(item["error"]))
        notes.append(f"`arete approve {item['id']}` tries again, `--reject` drops it")
    if item.get("review"):
        notes.append(f"review: {item['review']}")
    return notes


def _print_outbox_item(item: dict) -> None:
    cols = shutil.get_terminal_size().columns
    source = item.get("loop") or item.get("target") or "outbox"
    where = item.get("object") or "none"
    summary = item.get("plain_summary") or item.get("summary") or f"{item['kind']} on {where}"
    print(textwrap.fill(f"{item['id']:<6} {source:<20} {where:<24} {when(item['created'])}  {summary}",
                        cols, subsequent_indent=" " * 7, break_on_hyphens=False, max_lines=4, placeholder=" …"))
    for line in outbox_notes(item):
        print(textwrap.fill(line, cols, initial_indent=" " * 7, subsequent_indent=" " * 9, break_on_hyphens=False))


def _decide_outbox(item_id: str, *, reject: bool, reason: str) -> int:
    detail = client.call("GET", f"/api/v1/outbox/{quote(item_id, safe='')}")
    item = client.field(detail, "item")
    oid = quote(item["id"], safe="")
    if reject:
        body: dict[str, object] = {"base_version": item["version"]}
        if reason:
            body["reason"] = reason
        return client.say("POST", f"/api/v1/outbox/{oid}/reject", body)
    if item["state"] == "failed":
        return client.say("POST", f"/api/v1/outbox/{oid}/retry", {"base_version": item["version"]})
    if item["audience"] != "self" and not item.get("opened_at") and item.get("legacy_approval") is None:
        opened = client.call("POST", f"/api/v1/outbox/{oid}/open", {"base_version": item["version"]})
        item = client.field(opened, "item")
    return client.say("POST", f"/api/v1/outbox/{oid}/approve", {"base_version": item["version"], "rev": item["rev"]})


def cmd_approve(args) -> int:
    from .. import outbox

    if args.id is None:
        pending = outbox.list_items(state=("draft", "edited"), connect=foundation.reader)
        failed = outbox.list_items(state="failed", connect=foundation.reader)
        for item in (*pending, *failed):
            _print_outbox_item(item)
        if not pending and not failed:
            print("nothing waiting for approval")
        return 0
    raw_id = str(args.id).strip()
    if raw_id.upper().startswith("OB-"):
        return _decide_outbox(raw_id.upper(), reject=args.reject, reason=args.reason)
    if raw_id.isascii() and raw_id.isdecimal():
        try:
            return client.say("POST", f"/api/approvals/{int(raw_id)}", {"approve": not args.reject})
        except client.Failed as err:
            if err.code == 404:
                return _decide_outbox(f"OB-{int(raw_id)}", reject=args.reject, reason=args.reason)
            raise
    return _decide_outbox(raw_id, reject=args.reject, reason=args.reason)


def register(sub) -> None:
    p = sub.add_parser("approve", help="list emits waiting for approval, or approve one by id; "
                                       "approving a failed one tries the delivery again")
    p.add_argument("id", nargs="?", default=None)
    p.add_argument("--reject", action="store_true")
    p.add_argument("--reason", default="", help="reason when rejecting an outbox item")
    p.set_defaults(fn=cmd_approve, local_read=lambda args: args.id is None)

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
    t.add_argument("--state", default="", choices=config.ALL_TASK_STATES)
    t.set_defaults(fn=task_list)
    t = steering(tsub.add_parser("show", help="a task or a tracker item with its runs and children, as indented JSON"))
    t.add_argument("id")
    t.add_argument("--repo", default="", help="the repo of a tracker item no run names yet")
    t.add_argument("--json", action="store_true", help="print it as one JSON line")
    t.add_argument("--offline", action="store_true", help="read a task from the local store")
    t.set_defaults(fn=task_show, local_read=lambda args: args.offline)
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
