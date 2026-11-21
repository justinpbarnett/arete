import subprocess
import sys
from pathlib import Path


def check(args):
    from .. import api_check

    return api_check.launch()


def diff(args):
    from .. import contract_evolution as evolution

    try:
        root = Path.cwd()
        if root != Path(evolution.__file__).resolve().parent.parent:
            raise ValueError("run from the root of the checkout supplying this arete executable")
        refused, allowed, commit, candidate = evolution.inspect_diff(
            root, args.ref, evolution.evidence(args.usage), evolution.evidence(args.owner_approval),
            check_artifact=args.check_artifact)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"api diff: {error}", file=sys.stderr)
        return 1
    print(f"Base {commit}, working contract {candidate}, OpenAPI {evolution.OPENAPI}")
    for item in allowed:
        print(item)
    for item in refused:
        print(item, file=sys.stderr)
    if not refused:
        print("API compatibility check passed")
    return int(bool(refused))


def register(sub):
    parser = sub.add_parser("api", help="check the versioned API contract")
    commands = parser.add_subparsers(dest="api_command", required=True)
    commands.add_parser("check", help="exercise an isolated API on loopback").set_defaults(fn=check)
    compare = commands.add_parser("diff", help="compare with a git ref and regenerate OpenAPI")
    compare.add_argument("ref", default="origin/main", nargs="?")
    compare.add_argument("--check-artifact", action="store_true", help="refuse stale OpenAPI instead of rewriting it")
    compare.add_argument("--usage", metavar="FILE", help="complete per-route usage evidence in JSON")
    compare.add_argument("--owner-approval", metavar="FILE", help="explicit owner decision in JSON")
    compare.set_defaults(fn=diff)
