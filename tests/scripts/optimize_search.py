
import contextlib, importlib.util, io, json, os, shutil, sys
from pathlib import Path
from arete import cli, experiment, optimize, paths
root = Path(sys.argv[1])
if importlib.util.find_spec("arete.scratch"):
    from arete import scratch
    probe = [sys.executable, "-V"]
    if scratch.protected_command(probe) != probe:
        scratch.AUTHORITY = root / "authority-parent/verification-authority"
def call(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return {"code": code, "out": out.getvalue(), "err": err.getvalue()}
paths.CONFIG_FILE.write_text("\n".join(optimize.sample(root / "sample")) + "\n")
plan, config_file = str(root / "sample/plan.toml"), paths.CONFIG_FILE
experiment.MIN_PAIRS = 3
smaller = Path(plan).read_text()
for old, new in (("repeats = 6", "repeats = 3"), ("min_pairs = 6", "min_pairs = 3\nconfidence = 0.7")):
    if smaller.count(old) != 1:
        raise SystemExit(f"the sample plan no longer holds {old!r} once")
    smaller = smaller.replace(old, new)
Path(plan).write_text(smaller)
shutil.rmtree(root / "sample/replay/cheap/word-validation/3")
before = config_file.read_bytes()
folder = paths.STATE_DIR / "experiments" / "sample-search"
trail = lambda: [json.loads(line) for line in (folder / "trail.jsonl").read_text().splitlines()]
holdout = lambda: sum('"event": "trial"' in line for line in (folder / "holdout.jsonl").read_text().splitlines())
r = {"prices": call("prices", "import", str(root / "sample/prices.json"))["code"]}
r["dry"] = call("experiment", "optimize", plan, "--dry-run")
r["dry_wrote"] = (paths.STATE_DIR / "experiments").exists()
r["search"] = call("experiment", "optimize", plan)
r["holdout"] = holdout()
r["again"] = call("experiment", "optimize", plan)
r["holdout_again"] = holdout()
r["preview"] = call("experiment", "promote", str(folder))
r["preview_same"] = config_file.read_bytes() == before
os.chmod(config_file.parent, 0o500)
r["failed"] = call("experiment", "promote", str(folder), "--apply")
os.chmod(config_file.parent, 0o700)
r["failed_same"] = config_file.read_bytes() == before
r["failed_events"] = [e.get("status") for e in trail() if e["event"] == "apply"]
r["apply"] = call("experiment", "promote", str(folder), "--apply")
after = config_file.read_text().splitlines()
r["changed"] = [[a, b] for a, b in zip(before.decode().splitlines(), after) if a != b]
r["same_length"] = len(after) == len(before.decode().splitlines())
r["apply_again"] = call("experiment", "promote", str(folder), "--apply")
r["rollback"] = call("experiment", "promote", str(folder), "--rollback")
r["rolled_back"] = config_file.read_bytes() == before
r["rollback_again"] = call("experiment", "promote", str(folder), "--rollback")
print("BEHAVIOR_RESULT=" + json.dumps(r))
