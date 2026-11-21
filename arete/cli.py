from __future__ import annotations

import argparse
import sys

from . import __version__, client, codebase, config, paths, pilot, principals, zte
from .commands import api, costs, hooks, lab, loops, records, research, runs, sessions, setup, tasks
from .commands.common import Parser, serve_line
from .store import db as foundation

__all__ = ["main", "parser", "serve_line"]

REGISTRATIONS = (api.register, runs.register, tasks.register, loops.register, sessions.register, hooks.register, costs.register,
                 records.register, research.register, lab.register, setup.register, pilot.register, zte.register)

PER_CALL_HOOKS = paths.PER_CALL_HOOKS


class ShowVersion(argparse.Action):
    def __init__(self, option_strings, dest, **kw):
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, help="show the version and exit")

    def __call__(self, parser, namespace, values, option_string=None):
        print(f"arete {__version__} {codebase.load()}")
        parser.exit()


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="arete", description="arete: agents that keep running and keep getting better", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action=ShowVersion)
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    for register in REGISTRATIONS:
        register(sub)
    return ap


def main(argv: list[str] | None = None) -> int:
    from . import refusals

    args = parser().parse_args(argv)
    if args.cmd == "api":
        return args.fn(args) or 0
    if args.cmd not in PER_CALL_HOOKS:
        codebase.load()
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
