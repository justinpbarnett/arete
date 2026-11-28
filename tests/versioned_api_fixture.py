import json
import shutil
import subprocess
import tempfile
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arete import api_check, contract
from arete import contract_evolution as evolution
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT

SOURCE = Path(__file__).resolve().parents[1]
ROUTE = "GET /api/v1/settings"
CHANGE = "\nBY_NAME['settings.get']['result']['properties'].pop('settings')\n"
CHANGE += "BY_NAME['settings.get']['result']['required'].remove('settings')\n"
ADDITION = "\nBY_NAME['settings.get']['result']['properties']['profile'] = STRING\n"
REMOVE_READ = """
OPERATIONS.remove(BY_NAME['settings.get'])
reads = [op for op in OPERATIONS if op['method'] == 'GET']
batch = BY_NAME['batch.run']
batch['result']['properties']['results']['items']['anyOf'] = [op['result'] for op in reads]
batch['input']['properties']['requests']['items']['properties']['operation']['enum'] = [op['name'] for op in reads]
"""


def run(root, command, env):
    return subprocess.run(command, cwd=root, env=env, capture_output=True, text=True,
                          timeout=CHILD_HANG_TIMEOUT, check=False)


def git(root, env, *args):
    done = run(root, ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                      "-c", "commit.gpgsign=false", *args], env)
    api_check.require(done.returncode == 0, f"scratch git {args[0]}: {done.stderr}")
    return done.stdout.strip()


def exhibit(result, label, status, *expected):
    seen = result.stdout + result.stderr
    api_check.require(result.returncode == status and all(value in seen for value in expected),
                      f"{label}: wanted exit {status} and {expected}, got exit {result.returncode}\n{seen}")
    print(f"{label}: exit {status}" + (f" ({', '.join(expected)})" if expected else ""))
    return seen


def evolution_exercise(root):
    env = api_check.isolated_env(root)
    seed = root / "seed"
    (seed / "arete").mkdir(parents=True)
    (seed / "docs").mkdir()
    original = (SOURCE / evolution.SOURCE).read_text()
    other_producers = set(evolution.PRODUCERS) - {evolution.SOURCE}
    for path in sorted(other_producers):
        (seed / path).parent.mkdir(parents=True, exist_ok=True)
        (seed / path).write_bytes((SOURCE / path).read_bytes())
    git(seed, env, "init", "-q", "-b", "main")

    def base(name, *, deprecated=False, sunset=None, poison=False):
        registry = deepcopy(contract.publish())
        settings = next(op for op in registry["operations"] if op["name"] == "settings.get")
        if deprecated:
            settings["deprecated"] = True
        if sunset:
            settings["sunset"] = sunset
        additions = f"\nBY_NAME['settings.get'].update(deprecated={deprecated!r}, sunset={sunset!r})\n" if deprecated else ""
        source = (original + additions + ("\nraise RuntimeError('reference code executed')\n" if poison else "")).encode()
        (seed / evolution.SOURCE).write_bytes(source)
        hashes = evolution.sources(lambda path: (seed / path).read_bytes())
        doc = evolution.openapi(registry, contract.MINOR, hashes, contract.OLDEST_CLIENT)
        (seed / evolution.OPENAPI).write_text(evolution.document_json(doc) + "\n")
        git(seed, env, "add", "--", *evolution.PRODUCERS, evolution.OPENAPI)
        git(seed, env, "commit", "-qm", name)
        git(seed, env, "tag", name)

    base("original")
    git(seed, env, "switch", "-qc", "variants")
    past, future = "2000-01-01T00:00:00Z", "2999-01-01T00:00:00Z"
    base("future", deprecated=True, sunset=future)
    base("expired", deprecated=True, sunset=past)
    base("inert", poison=True)
    (seed / evolution.SOURCE).write_text(original + "\n# stale artifact fixture\n")
    git(seed, env, "commit", "-qam", "Snapshot does not match source")
    git(seed, env, "tag", "stale")
    stale_producers = {f"stale-{Path(path).stem}": path for path in sorted(other_producers)}
    for tag, path in stale_producers.items():
        git(seed, env, "switch", "-q", "--detach", "inert")
        suffix = b" \n" if path.endswith(".json") else b"\n# Producer snapshot fixture\n"
        (seed / path).write_bytes((SOURCE / path).read_bytes() + suffix)
        git(seed, env, "commit", "-qam", "Producer no longer matches snapshot")
        git(seed, env, "tag", tag)
    git(seed, env, "switch", "-q", "main")
    clone = root / "clone"
    git(root, env, "clone", "-q", str(seed), str(clone))
    shutil.copytree(SOURCE / "arete", clone / "arete", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".state"))

    def diff(*args):
        entrypoint = ("import argparse; from arete.commands import api; parser = argparse.ArgumentParser(); "
                      "api.register(parser.add_subparsers(required=True)); args = parser.parse_args(); "
                      "sys.exit(args.fn(args))")
        command = api_check.child_command(clone, entrypoint, "api", "diff", *args)
        return run(clone, command, env)

    def check_evidence(cases):
        entrypoint = """import argparse
import json
from contextlib import redirect_stderr, redirect_stdout
from functools import cache
from io import StringIO
from arete import contract_evolution
from arete.commands import api

# Evidence cases share immutable Git refs in this scratch checkout.
contract_evolution.git = cache(contract_evolution.git)
parser = argparse.ArgumentParser()
api.register(parser.add_subparsers(required=True))
results = []
for arguments in json.loads(sys.argv[1]):
    args = parser.parse_args(arguments)
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = args.fn(args)
    results.append((status, out.getvalue(), err.getvalue()))
print(json.dumps(results))
"""
        commands = [["api", "diff", *args] for args, *_ in cases]
        command = api_check.child_command(clone, entrypoint, json.dumps(commands))
        done = run(clone, command, env)
        api_check.require(done.returncode == 0, f"API evidence checks failed to run:\n{done.stdout}{done.stderr}")
        for (args, *expected), (status, out, err) in zip(cases, json.loads(done.stdout), strict=True):
            exhibit(subprocess.CompletedProcess(args, status, out, err), *expected)

    def candidate(text, *, bump=True):
        working = original.replace(f"MINOR = {contract.MINOR}\n", f"MINOR = {contract.MINOR + 1}\n", 1) if bump else original
        (clone / evolution.SOURCE).write_text(working + text)

    cli = api_check.child_command(clone, "from arete.cli import main; sys.exit(main())", "api", "diff")
    exhibit(run(clone, cli, env), "default origin/main", 0, "compatibility check passed")
    exhibit(diff("inert"), "reference code stayed inert", 0)
    exhibit(diff("stale"), "stale reference artifact", 1, "is stale")
    for tag, path in stale_producers.items():
        exhibit(diff(tag), f"stale snapshot after change in {path}", 1, "is stale", path)
    candidate(ADDITION, bump=False)
    exhibit(diff("original"), "optional result addition without a minor bump", 1, "advance the API minor")
    candidate(ADDITION)
    exhibit(diff("original"), "optional result addition with a minor bump", 0)
    artifact = (clone / evolution.OPENAPI).read_bytes()
    exhibit(diff("original"), "repeat generation", 0)
    api_check.require(artifact == (clone / evolution.OPENAPI).read_bytes(), "OpenAPI bytes changed between runs")
    version = json.loads(artifact)["info"]["version"]
    api_check.require(version == f"1.{contract.MINOR + 1}", f"incorrect generated version: {version}")
    candidate("\nOPERATIONS.remove(BY_NAME['view.get'])\n")
    exhibit(diff(), "removed operation", 1, "view.get (GET /api/v1/views/{id}): route removed")
    candidate("\nALIASES[:] = [a for a in ALIASES if a['path'] != '/api/meta']\n")
    exhibit(diff(), "removed alias", 1, "alias GET /api/meta: route removed")
    candidate("\nBY_NAME['screen.get']['input']['required'].append('view')\n")
    exhibit(diff(), "tightened input", 1, "input.view requiredness changed")
    candidate("\nOLDEST_CLIENT['cli'] = '1.1'\n")
    exhibit(diff(), "raised client minimum", 1, "oldest_client.cli changed")
    candidate(CHANGE)
    broken = exhibit(diff("original"), "removed result field", 1, "settings.get (GET /api/v1/settings): result.settings removed",
                     "route is not deprecated", "no valid Sunset", "missing seven-day usage", "missing explicit owner approval")
    digest = broken.split("working contract ", 1)[1].split(",", 1)[0]
    commits = {ref: git(clone, env, "rev-parse", ref) for ref in ("original", "future", "expired")}
    evidence_count = 0

    def evidence(ref, *, count=0, batch_count=0, days=8, approved=True, routes=(ROUTE,)):
        nonlocal evidence_count
        evidence_count += 1
        usage_path = root / f"usage-{evidence_count}.json"
        approval_path = root / f"approval-{evidence_count}.json"
        now = datetime.now(UTC)
        usage = {"from": (now - timedelta(days=days)).isoformat(), "through": now.isoformat(), "requests": count}
        if batch_count is not None:
            usage["batch_requests"] = batch_count
        usage_path.write_text(json.dumps({ROUTE: usage}))
        approval_path.write_text(json.dumps({"approved": approved, "routes": routes, "contract_sha": digest,
                                             "base_commit": commits[ref]}))
        return ("--usage", str(usage_path), "--owner-approval", str(approval_path))

    check_evidence((
        (("original", *evidence("original")), "deprecation required", 1, "route is not deprecated"),
        (("future", *evidence("future")), "future Sunset", 1, "Sunset has not passed"),
        (("expired", *evidence("expired", count=1)), "recent direct route use", 1, "shows 1 direct request(s)"),
        (("expired", *evidence("expired", batch_count=1)), "recent batch route use", 1, "shows 1 batch request(s)"),
        (("expired", *evidence("expired", batch_count=None)), "missing batch usage evidence", 1, "batch_requests"),
        (("expired", *evidence("expired", days=1)), "incomplete usage window", 1, "does not cover the last seven days"),
        (("expired", *evidence("expired")[2:]), "missing usage evidence", 1, "missing seven-day usage evidence"),
        (("expired", *evidence("expired", approved=False)), "missing approval", 1, "missing explicit owner approval"),
        (("expired", *evidence("expired", routes=("GET /api/meta",))), "approval for another route", 1,
         "missing explicit owner approval"),
        (("expired", *evidence("expired")), "all field removal gates satisfied", 0, "Approved: settings.get"),
    ))
    candidate(REMOVE_READ)
    broken = exhibit(diff("expired"), "removed read operation", 1, "settings.get (GET /api/v1/settings): route removed")
    digest = broken.split("working contract ", 1)[1].split(",", 1)[0]
    check_evidence((
        (("expired", *evidence("expired", batch_count=1)), "removed read still called through batch", 1,
         "shows 1 batch request(s)"),
        (("expired", *evidence("expired")), "all read removal gates satisfied", 0, "Approved: settings.get"),
    ))


def evolution_temp():
    with tempfile.TemporaryDirectory(prefix="arete-api-evolution-") as temp:
        evolution_exercise(Path(temp).resolve())
