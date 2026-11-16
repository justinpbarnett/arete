from __future__ import annotations

import argparse
import sys

from . import __version__, codebase, paths
from .commands import costs, hooks, lab, loops, records, research, runs, sessions, setup, tasks
from .commands.common import Parser, serve_line

__all__ = ["main", "serve_line"]

REGISTRATIONS = (runs.register, tasks.register, loops.register, sessions.register, hooks.register, costs.register,
                 records.register, research.register, lab.register, setup.register)

PER_CALL_HOOKS = paths.PER_CALL_HOOKS


class ShowVersion(argparse.Action):
    def __init__(self, option_strings, dest, **kw):
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, help="show the version and exit")

    def __call__(self, parser, namespace, values, option_string=None):
        print(f"arete {__version__} {codebase.load()}")
        parser.exit()


def main(argv: list[str] | None = None) -> int:
    from . import refusals

    ap = argparse.ArgumentParser(prog="arete", description="arete: agents that keep running and keep getting better", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action=ShowVersion)
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    for register in REGISTRATIONS:
        register(sub)
    args = ap.parse_args(argv)
    if args.cmd not in PER_CALL_HOOKS:
        codebase.load()
    try:
        return args.fn(args) or 0
    except refusals.Refusal as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
