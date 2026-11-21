import sys
from contextlib import ExitStack


def run(args):
    from .. import verify_faults

    with ExitStack() as scope:
        try:
            directory = scope.enter_context(verify_faults.scratch_state(verify_faults.state_path()))
        except OSError:
            print(verify_faults.REFUSAL, file=sys.stderr)
            return 1
        try:
            verify_faults.update(directory, args)
        except (OSError, ValueError) as error:
            print(f"verify-fault: {error}", file=sys.stderr)
            return 1
    print({"arm": "Response fault armed.", "drop-streams": "Stream drop queued.",
           "event": "Event queued.", "clear": "Response faults and pending events cleared."}[args.fault_command])
    return 0


def register(sub):
    parser = sub.add_parser("verify-fault", help="arm faults for a verification scratch dashboard")
    commands = parser.add_subparsers(dest="fault_command", required=True)
    parser.set_defaults(fn=run)

    arm = commands.add_parser("arm", help="replace the next responses on a route")
    arm.add_argument("--route", required=True)
    arm.add_argument("--status", type=int, required=True)
    arm.add_argument("--body", default="")
    arm.add_argument("--count", type=int, default=1)

    commands.add_parser("drop-streams", help="close the dashboard's open event streams")
    event = commands.add_parser("event", help="emit an event for an existing scratch run")
    event.add_argument("--kind", required=True)
    event.add_argument("--run", required=True)
    commands.add_parser("clear", help="clear response faults and pending events")
