from __future__ import annotations

import argparse
import functools
import json
import os
import sys

from .. import paths
from .common import Parser, follow, refused

CHAT_SESSION_ACTIONS = ("start", "list", "report", "stop")
CHAT_ONCE = "--chat is given once; the chat is the one the command was allowed for"


class ChatOnce(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error(CHAT_ONCE)
        setattr(namespace, self.dest, values)


def chosen_chat(args) -> int | None:
    if args.chat_before is not None and args.chat is not None:
        args.session_parser.error(CHAT_ONCE)
    return args.chat if args.chat is not None else args.chat_before


def session_errors(handler):
    @functools.wraps(handler)
    def guarded(args) -> int:
        from .. import control

        try:
            return handler(args)
        except (control.NotFound, control.Busy, control.Refused) as e:
            return refused(e)

    return guarded


def chat_routed(handler):
    guarded = session_errors(handler)

    @functools.wraps(handler)
    def routed(args) -> int:
        if (chat := chosen_chat(args)) is not None:
            return chat_session(args, chat)
        return guarded(args)

    return routed


def local_only(handler):
    guarded = session_errors(handler)

    @functools.wraps(handler)
    def routed(args) -> int:
        if chosen_chat(args) is not None:
            print(f"--chat works with arete session {', '.join(CHAT_SESSION_ACTIONS)}", file=sys.stderr)
            return 2
        return guarded(args)

    return routed


def attached(session_id: str) -> int:
    import select
    import threading

    from .. import control, sessions, state

    r = state.run_get(session_id)
    if not r or r["kind"] != "session":
        raise control.NotFound(f"no session {session_id}")
    stop = threading.Event()
    print("following; type a line and press enter to send it, ctrl-c to detach", flush=True)

    def send_typed() -> None:
        if stop.is_set() or not sys.stdin.isatty() and sys.stdin.closed:
            return
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if ready:
            line = sys.stdin.readline()
            if not line:
                stop.set()
            elif line.strip():
                sessions.send(session_id, line)
    try:
        follow(session_id, send=send_typed)
    except KeyboardInterrupt:
        print(f"\ndetached; the session keeps running. `arete session stop {session_id}` ends it")
    return 0


@chat_routed
def session_start(args) -> int:
    from .. import sessions

    tools = [t.strip() for t in args.tools.split(",") if t.strip()] if args.tools is not None else None
    s = sessions.launch(" ".join(args.prompt), device=args.device, model=args.model, run_with=args.runner,
                        tools=tools, repo=args.repo or "", budget_usd=args.budget, task=args.task,
                        draft_pr=args.draft_pr)
    print(f"session {s['id']}: {s['runner']} {s['model']} on {s['device']} in {paths.short_path(s['cwd'])}"
          + (f" on {s['branch']}" if s["branch"] else "") + f"; tools {', '.join(s['tools']) or 'none'}")
    if s["task"] and not args.attach:
        print(f"`arete session report {s['id']}` prints its report when it ends")
        return 0
    if not args.attach:
        print(f"`arete session attach {s['id']}` follows it and sends what you type")
        return 0
    return attached(s["id"])


@local_only
def session_attach(args) -> int:
    return attached(args.id)


@chat_routed
def session_report(args) -> int:
    from .. import control

    print(json.dumps(control.session_report(args.id)))
    return 0


@local_only
def session_send(args) -> int:
    from .. import sessions

    print(json.dumps(sessions.send(args.id, " ".join(args.text))))
    return 0


@chat_routed
def session_stop(args) -> int:
    from .. import control

    print(json.dumps(control.stop_run(args.id)))
    return 0


@local_only
def session_exec(args) -> int:
    from .. import sessions

    return 0 if sessions.execute(args.id) in ("done", "stopped", "busy") else 1


@chat_routed
def session_list(args) -> int:
    from .. import sessions, state, util

    for s in sessions.views():
        print(f"{s['id']:<28} {s['status']:<11} {s['runner'] or '':<6} {s['model'] or '':<24} "
              f"{s['device'] or '':<10} {util.paid_note(s['paid']):<12}  {s['prompt'][:70]}")
    if not state.runs(kind="session", limit=1):
        print("no sessions yet; `arete session start \"what to do\"`")
    return 0


def chat_session(args, chat: int) -> int:
    from urllib.parse import quote

    from .. import chat as chats
    from .. import client, control

    base = f"/api/chats/{chat}/sessions"
    if args.action == "start":
        if args.tools is not None or args.draft_pr or args.attach:
            print("a chat starts task sessions; --tools, --draft-pr and --attach are not for --chat", file=sys.stderr)
            return 2
        request = ("POST", base, {"prompt": " ".join(args.prompt), "device": args.device, "model": args.model,
                                  "runner": args.runner, "budget_usd": args.budget,
                                  **({"repo": args.repo} if args.repo is not None else {})})
    elif args.action == "list":
        request = ("GET", base, None)
    elif args.action == "report":
        request = ("GET", f"{base}/{quote(args.id, safe='')}/report", None)
    else:
        request = ("POST", f"{base}/{quote(args.id, safe='')}/stop", None)
    try:
        try:
            answer = client.call(*request)
        except client.Down as e:
            if args.action in ("start", "stop") and os.environ.get("ARETE_AGENT") == "1":
                print(f"{e}; an agent starts and stops sessions only through the server", file=sys.stderr)
                return 1
            answer = {"start": lambda: chats.start_session(
                          chat, " ".join(args.prompt), repo=args.repo, device=args.device, model=args.model,
                          runner=args.runner, budget_usd=args.budget),
                      "list": lambda: chats.sessions_of(chat),
                      "report": lambda: chats.session_report(chat, args.id),
                      "stop": lambda: chats.stop_session(chat, args.id)}[args.action]()
    except client.Failed as e:
        print(e, file=sys.stderr)
        return 1
    except (control.NotFound, control.Busy, control.Refused, chats.NoChat) as e:
        return refused(e)
    print(json.dumps(answer))
    return 0


def chat_option(p) -> None:
    p.add_argument("--chat", type=int, default=None, action=ChatOnce, help="a dashboard chat's number: start, list, "
                   "report and stop go through the server for that chat's own sessions")


def register(sub) -> None:
    p = sub.add_parser("session", help="start, follow, message and stop agent sessions on any device")
    p.add_argument("--chat", dest="chat_before", metavar="CHAT", type=int, default=None, action=ChatOnce,
                   help="a dashboard chat's number: start, list, report and stop go through the server for that "
                        "chat's own sessions")
    p.set_defaults(session_parser=p)
    ssub = p.add_subparsers(dest="action", required=True, parser_class=Parser)

    a = ssub.add_parser("start", help="start a session on a device")
    a.add_argument("prompt", nargs="+", help="the first message")
    a.add_argument("--device", default="local")
    a.add_argument("--model", default="")
    a.add_argument("--runner", default="", help="claude, pi or codex; by the model when left out")
    a.add_argument("--tools", default=None, help="comma separated; Read,Edit,Write,Glob,Grep,Bash by default")
    a.add_argument("--repo", default=None, help="a repo name from the config; local works in a new worktree of it, another device "
                                                "in a fresh clone whose work arete fetches back and pushes; with --chat, none "
                                                "starts without a repo and the chat's project is the default")
    chat_option(a)
    a.add_argument("--budget", type=float, default=None, help="USD for the whole session")
    a.add_argument("--attach", action="store_true", help="follow it at once")
    a.add_argument("--task", action="store_true", help="run the prompt to the end, save a report, close and release "
                                                       "the device")
    a.add_argument("--draft-pr", action="store_true", help="with --task and --repo: open a draft PR for the pushed "
                                                           "branch")
    a.set_defaults(fn=session_start)

    for name, text, handler in (("attach", "follow a session and send what you type", session_attach),
                                ("stop", "end a session", session_stop),
                                ("exec", "carry out a started session; `arete session start` calls it", session_exec),
                                ("report", "print a finished session's report", session_report)):
        a = ssub.add_parser(name, help=text)
        a.add_argument("id", metavar="ID", help="the session id")
        chat_option(a)
        a.set_defaults(fn=handler)

    a = ssub.add_parser("send", help="send text to a session")
    a.add_argument("id", metavar="ID", help="the session id")
    a.add_argument("text", nargs="+", help="what to send")
    chat_option(a)
    a.set_defaults(fn=session_send)

    a = ssub.add_parser("list", help="every session")
    chat_option(a)
    a.set_defaults(fn=session_list)
