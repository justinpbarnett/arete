from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

__all__ = ["main", "parser", "serve_line"]
_serve_start_checked = False


def serve_line(beat: dict) -> str:
    from .commands.common import serve_line as format_serve_line

    return format_serve_line(beat)


class ShowVersion(argparse.Action):
    def __init__(self, option_strings, dest, **kw):
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, help="show the version and exit")

    def __call__(self, parser, namespace, values, option_string=None):
        from . import __version__, codebase

        print(f"arete {__version__} {codebase.load()}")
        parser.exit()


def parser() -> argparse.ArgumentParser:
    from . import pilot, zte
    from .commands import api, costs, hooks, lab, loops, records, research, runs, sessions, setup, tasks, verify_fault
    from .commands.arguments import Parser

    ap = argparse.ArgumentParser(prog="arete", description="arete: agents that keep running and keep getting better", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action=ShowVersion)
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    for group in (api, runs, tasks, loops, sessions, hooks, costs, records, research, lab, setup, pilot, zte, verify_fault):
        group.register(sub)
    return ap


def status_from_heartbeat(problem: SystemExit) -> int:
    from . import codebase, local_paths

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


def check_serve_start() -> None:
    from . import local_paths, serve_guard

    home = Path.home()
    _, state, isolated = local_paths.local_dirs(home)
    logs = Path(os.environ.get("ARETE_LOG_DIR", home / "Library" / "Logs" / "arete"
                               if sys.platform == "darwin" and not isolated else state / "logs"))
    serve_guard.startup(state, Path(__file__).resolve().parent.parent, logs)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv in (["serve"], ["serve", "--no-dashboard"]) and not _serve_start_checked:
        check_serve_start()
    from . import codebase

    if argv[:1] == ["status"]:
        from .commands.arguments import Parser, status_arguments

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
        with client.invocation(), principals.acting("hand" if recovery else principals.cli_actor):
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
