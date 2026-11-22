from __future__ import annotations

import argparse
import json
import sys
from typing import NoReturn


class Parser(argparse.ArgumentParser):
    json_errors = False

    def parse_known_args(self, args=None, namespace=None):
        found, extra = super().parse_known_args(args, namespace)
        if self.json_errors and extra:
            self.error(f"unrecognized arguments: {' '.join(extra)}")
        return found, extra

    def error(self, message: str) -> NoReturn:
        if not self.json_errors:
            super().error(message)
        print(json.dumps({"error": message, "usage": self.format_usage().strip()}), flush=True)
        sys.exit(1)


def steering(p: Parser) -> Parser:
    p.json_errors = True
    return p


def status_arguments(p: Parser) -> Parser:
    steering(p)
    p.add_argument("run_id", nargs="?", default="", help="with --json: this run's steps, then its report")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true", help="one JSON line per live run, or per step of RUN_ID")
    p.add_argument("--offline", action="store_true", help="read local status and run lines")
    p.set_defaults(local_read=lambda args: args.offline)
    return p
