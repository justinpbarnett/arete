import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

from .. import contract


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


def config_operations():
    return (op for op in contract.OPERATIONS if op["name"].startswith("config.") and op["method"] == "GET")


def config_read(operation, values):
    from .. import client

    contract.validate(values, operation["input"])
    path = operation["path"]
    query = {}
    for key, value in values.items():
        marker = "{" + key + "}"
        if marker in path:
            path = path.replace(marker, quote(value, safe=""))
        else:
            query[key] = value
    return client.call(operation["method"], path + ("?" + urlencode(query) if query else ""))


def config_command(args):
    from .. import client

    values = {key: vars(args)[key] for key in args.operation["input"]["properties"]
              if vars(args).get(key) is not None}
    client.emit(config_read(args.operation, values))


def config_mcp(_args):
    from .. import client, owner_asks

    tools = {op["name"].replace(".", "_"): op for op in config_operations()}

    def answer(request):
        method, params = request.get("method"), request.get("params") or {}
        if not isinstance(params, dict):
            return {"content": [{"type": "text", "text": "Tool parameters must be an object"}], "isError": True}
        if method == "initialize":
            return {"protocolVersion": params.get("protocolVersion") or "2024-11-05", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "arete-config", "version": contract.VERSION}}
        if method == "tools/list":
            return {"tools": [{"name": name, "description": op["name"], "inputSchema": op["input"]}
                              for name, op in tools.items()]}
        if method == "tools/call":
            name = params.get("name")
            op = tools.get(name) if isinstance(name, str) else None
            try:
                if op is None:
                    raise contract.Problem("invalid_input", "Unknown config tool")
                result = config_read(op, params.get("arguments", {}))
            except (contract.Problem, client.Failed, client.Down) as error:
                return {"content": [{"type": "text", "text": str(error)}], "isError": True}
            return {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False}
        if method == "ping":
            return {}
        raise LookupError(method)

    return owner_asks.serve(sys.stdin, sys.stdout, answer=answer)


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
    parser = sub.add_parser("config", help="read versioned configuration")
    verbs = parser.add_subparsers(dest="config_verb", required=True)
    for op in config_operations():
        verb = verbs.add_parser(op["name"].partition(".")[2])
        shape = op["input"]
        for field, spec in shape["properties"].items():
            pos = field in shape["required"]
            verb.add_argument(field if pos else "--" + field, choices=spec.get("enum"))
        verb.set_defaults(fn=config_command, operation=op)
    sub.add_parser("mcp-config", help="serve config read tools over MCP").set_defaults(fn=config_mcp)
