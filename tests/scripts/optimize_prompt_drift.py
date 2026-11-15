
import contextlib, importlib.util, io, json, shutil, sys
from pathlib import Path
from arete import cli, experiment, optimize, paths, util
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
experiment.MIN_PAIRS = 3
paths.CONFIG_FILE.write_text("\n".join(optimize.sample(root / "sample")) + "\n")
sample = root / "sample"
call("prices", "import", str(sample / "prices.json"))
for name in ("tuned", "tuned2"):
    shutil.copytree(sample / "replay" / "stronger", sample / "replay" / name)
    shutil.copytree(paths.PROMPTS_DIR, root / name)
text = (sample / "plan.toml").read_text()
head, tail = text.split("[[rounds]]")[0], "[tasks." + text.split("\n[tasks.", 1)[1]
rounds = ('[[rounds]]\nvary = "effort"\n[rounds.candidates.cheap]\neffort = "low"\n\n'
          f'[[rounds]]\nvary = "prompt"\n[rounds.candidates.tuned]\nprompts = "{root / "tuned"}"\n\n'
          f'[[rounds]]\nvary = "prompt"\n[rounds.candidates.tuned2]\nprompts = "{root / "tuned2"}"\n\n')
plan = sample / "plan.toml"
smaller = (head.replace("repeats = 6", "repeats = 3") + rounds
           + tail.replace("rounds = 2", "rounds = 3").replace("min_pairs = 6", "min_pairs = 3\nconfidence = 0.7"))
if "repeats = 3" not in smaller or "confidence = 0.7" not in smaller:
    raise SystemExit("the sample plan no longer holds repeats = 6 and min_pairs = 6")
plan.write_text(smaller)
s = optimize.load(plan)
folder = paths.STATE_DIR / "experiments" / "sample-search"
folder.mkdir(parents=True)
trail = folder / "trail.jsonl"
trials = lambda: sum(line.count('"event": "trial"') for f in folder.glob("*.jsonl") for line in f.read_text().splitlines())
optimize.append(trail, {"event": "search", "digest": s.digest, "design": s.design, "baseline": s.snapshot, "at": util.iso()})
champion = {"name": optimize.BASELINE, "overrides": {}, "settings": s.snapshot["settings"],
            "inheritors": s.snapshot["inheritors"], "round": 0}
for n in (1, 2):
    result = optimize.play(s, folder, n, s.rounds[n - 1], champion)
    optimize.append(trail, result)
    champion = result["champion"]
r = {"champion": champion["name"], "recorded": bool(champion.get("fingerprints")), "ran": trials()}
build = root / "tuned" / "build.md"
original = build.read_text()
build.write_text(original + "\nPrefer another approach.\n")
def refused(action):
    try:
        action()
    except experiment.Refused as e:
        return str(e)
    return ""
r["next_round"] = refused(lambda: optimize.play(s, folder, 3, s.rounds[2], champion))
r["holdout"] = refused(lambda: optimize.score_holdout(s, folder, champion))
r["resume"] = call("experiment", "optimize", str(plan))
r["after_refusals"] = trials()
build.write_text(original)
engine_code = experiment.engine_code
experiment.engine_code = lambda: "changed engine"
r["engine"] = refused(lambda: optimize.play(s, folder, 3, s.rounds[2], champion))
r["after_engine"] = trials()
experiment.engine_code = engine_code
r["proceed"] = call("experiment", "optimize", str(plan))
r["final"] = trials()
print("BEHAVIOR_RESULT=" + json.dumps(r))
