from __future__ import annotations

import argparse
import json
import sys

from . import __version__, codebase
from .commands.common import Parser, serve_line, serve_revisions, status_arguments

__all__ = ["main", "serve_line"]


class ShowVersion(argparse.Action):
    def __init__(self, option_strings, dest, **kw):
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, help="show the version and exit")

    def __call__(self, parser, namespace, values, option_string=None):
        print(f"arete {__version__} {codebase.load()}")
        parser.exit()


def parse_args(argv: list[str]) -> argparse.Namespace:
    from . import pilot, zte
    from .commands import api, costs, hooks, lab, loops, records, research, runs, sessions, setup, tasks

    ap = argparse.ArgumentParser(prog="arete", description="arete: agents that keep running and keep getting better", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action=ShowVersion)
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    for group in (api, runs, tasks, loops, sessions, hooks, costs, records, research, lab, setup, pilot, zte):
        group.register(sub)
    return ap.parse_args(argv)


def status_from_heartbeat(problem: SystemExit) -> int:
    from . import paths

    print(problem, file=sys.stderr)
    try:
        beat = json.loads(paths.read_confined(paths.SERVE_BEAT_FILE))
    except (OSError, ValueError):
        beat = None
    if isinstance(beat, dict) and (revisions := serve_revisions(beat)):
        print(f"serve heartbeat at {beat.get('at', 'unknown time')}, {revisions}")
        return 0
    print("serve heartbeat has no recorded revisions", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["status"]:
        args = status_arguments(Parser(prog="arete status")).parse_args(argv[1:])
        try:
            codebase.load()
        except SystemExit as problem:
            if args.json or args.run_id:
                raise
            return status_from_heartbeat(problem)
        from .commands.setup import cmd_status

        args.cmd, args.fn = "status", cmd_status
    else:
        args = parse_args(argv)
        if args.cmd == "api":
            return args.fn(args) or 0
        from .paths import PER_CALL_HOOKS

        if args.cmd not in PER_CALL_HOOKS:
            codebase.load()
    from . import principals, refusals

    try:
        recovery = args.cmd in ("init", "migrate") or (args.cmd == "backup" and args.restore)
        with principals.acting("hand" if recovery else principals.cli_actor()):
            return args.fn(args) or 0
    except refusals.Refusal as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
