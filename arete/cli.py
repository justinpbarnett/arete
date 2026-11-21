from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, codebase, local_paths
from .commands.arguments import Parser, status_arguments

__all__ = ["main", "parser", "serve_line"]


def serve_line(beat: dict) -> str:
    from .commands.common import serve_line as format_serve_line

    return format_serve_line(beat)


class ShowVersion(argparse.Action):
    def __init__(self, option_strings, dest, **kw):
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, help="show the version and exit")

    def __call__(self, parser, namespace, values, option_string=None):
        print(f"arete {__version__} {codebase.load()}")
        parser.exit()


def parser() -> argparse.ArgumentParser:
    from . import pilot, zte
    from .commands import api, costs, hooks, lab, loops, records, research, runs, sessions, setup, tasks, verify_fault

    ap = argparse.ArgumentParser(prog="arete", description="arete: agents that keep running and keep getting better", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action=ShowVersion)
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    for group in (api, runs, tasks, loops, sessions, hooks, costs, records, research, lab, setup, pilot, zte, verify_fault):
        group.register(sub)
    return ap


def status_from_heartbeat(problem: SystemExit) -> int:
    _, state, isolated = local_paths.local_dirs(Path.home())
    print(problem, file=sys.stderr)
    try:
        beat = json.loads(local_paths.read_confined(state / local_paths.SERVE_BEAT_NAME, isolated=isolated))
    except (OSError, ValueError):
        beat = None
    if isinstance(beat, dict) and (revisions := codebase.serve_revisions(beat)):
        print(f"last recorded serve heartbeat at {beat.get('at', 'unknown time')}, {revisions}", file=sys.stderr)
    else:
        print("serve heartbeat has no recorded revisions", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["status"]:
        args = status_arguments(Parser(prog="arete status")).parse_args(argv[1:])
        try:
            codebase.load()
        except SystemExit as problem:
            if args.json or args.run_id or args.offline:
                raise
            return status_from_heartbeat(problem)
        from .commands.setup import cmd_status

        args.cmd, args.fn = "status", cmd_status
    else:
        args = parser().parse_args(argv)
        if args.cmd in ("api", "verify-fault"):
            return args.fn(args) or 0
        from .paths import PER_CALL_HOOKS

        if args.cmd not in PER_CALL_HOOKS:
            codebase.load()
    from . import client, config, paths, principals, refusals
    from .store import db as foundation

    try:
        local_read = getattr(args, "local_read", False)
        if (local_read(args) if callable(local_read) else local_read) and not paths.DB_FILE.is_file():
            raise foundation.NoDatabase
        recovery = args.cmd in ("init", "migrate") or (args.cmd == "backup" and args.restore)
        upload_on_write = args.cmd not in ("doctor", "install") and not getattr(args, "dry_run", False)
        with client.invocation(upload_on_write=upload_on_write), principals.acting(
                "hand" if recovery else principals.cli_actor):
            return args.fn(args) or 0
    except client.Down as e:
        client.emit(e.answer)
        return 1
    except client.Failed as e:
        return client.failed(e)
    except foundation.NoDatabase as e:
        message = str(e) or f"no arete.db at {paths.DB_FILE}; run arete serve or arete migrate"
        client.emit({"error": message, **({"source": "local"} if getattr(args, "offline", False) else {})})
        return 1
    except (config.UnknownRepo, refusals.Refusal) as e:
        print(e, file=sys.stderr)
        return 1
    except (KeyError, TypeError) as e:
        return client.answer_error(e)


if __name__ == "__main__":
    sys.exit(main())
