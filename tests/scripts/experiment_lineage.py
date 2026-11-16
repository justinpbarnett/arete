
import contextlib, importlib.util, io, json, shutil, sys
from pathlib import Path
from unittest import mock
from arete.store import runs as run_store
from arete import cli, evals, paths, record, state, util
from arete.lab import experiment, optimize
root, mode = Path(sys.argv[1]), sys.argv[2]
state.migrate()
if importlib.util.find_spec("arete.scratch"):
    from arete import sandbox, scratch
    probe = [sys.executable, "-V"]
    if sandbox.protected_command(probe) != probe:
        sandbox.AUTHORITY = root / "authority-parent/verification-authority"
def call(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return {"code": code, "out": out.getvalue(), "err": err.getvalue()}
def expect(result, code, words=""):
    assert result["code"] == code, result
    assert words in result["out"] + result["err"], result
paths.CONFIG_FILE.write_text("\n".join(optimize.sample(root / "sample")) + "\n")
expect(call("prices", "import", str(root / "sample/prices.json")), 0)
shutil.copytree(paths.PROMPTS_DIR, root / "tuned")
head = util.git(["rev-parse", "HEAD"], root / "sample/repo").strip()
manifest, result = root / "manifest.toml", root / "results.jsonl"
manifest.write_text(f"""
name = "lineage"
repo = "replay-sample"
atom = "build"
repeats = 2
[arms.baseline]
model = "scratch-a"
[arms.stronger]
model = "scratch-b"
prompts = "{root / 'tuned'}"
[tasks.word-validation]
base = "{head}"
split = "validation"
[accept]
baseline = "baseline"
[limits]
spend_usd = 5
minutes = 60
calls = 2000
tokens = 5000000
""")
build = root / "tuned/build.md"
original = build.read_bytes()
marker = b"\nLINEAGE_MARKER_264\n"
def run():
    return call("experiment", "run", str(manifest), "--out", str(result))
def rows(event):
    return [e for e in experiment.events(result) if e["event"] == event]
def clean():
    expect(run(), 0)
    trials = rows("trial")
    design = rows("manifest")[0]["design"]
    assert len([t for t in trials if t["status"] == "done"]) == 4, trials
    assert all(t["fingerprints"] == design["arms"][t["arm"]]["fingerprints"]
               for t in trials if t["status"] == "done"), trials
    assert all(t["engine"] == design["engine"] for t in trials if t["status"] == "done")
    assert experiment.summary(result)["pairs"]["validation"][0]["pairs"] == 2
    assert all("engine" in e for e in rows("manifest") + rows("session"))
    expect(run(), 0, "4 of 4 trials already done")
if mode == "between":
    real = experiment.run_trial
    def mutate(*args, **kw):
        why = real(*args, **kw)
        if len(rows("trial")) == 1:
            build.write_bytes(original + marker)
        return why
    with mock.patch.object(experiment, "run_trial", mutate):
        expect(run(), 3, "prompt changed since the design")
    assert [(t["arm"], t["repeat"], t["status"]) for t in rows("trial")] == [("baseline", 1, "done")]
    assert not any(e["arm"] == "stronger" for e in rows("start"))
    assert "prompt" in rows("stop")[-1]["why"]
    expect(run(), 1, "the design changed")
    assert len(rows("trial")) == 1
    build.write_bytes(original)
    clean()
elif mode == "during":
    real = evals.run_task
    def mutate(*args, **kw):
        if kw["run"].trial.startswith("stronger-"):
            build.write_bytes(original + marker)
        return real(*args, **kw)
    with mock.patch.object(evals, "run_task", mutate):
        expect(run(), 3, "prompt changed since the design")
    trial = next(t for t in rows("trial") if t["arm"] == "stronger")
    assert trial["status"] == "drifted" and not trial["accepted"], trial
    assert "prompt" in trial["drift"], trial
    assert run_store.run_get(trial["run"])["status"] == "stopped"
    assert trial["fingerprints"]["prompt"] != rows("manifest")[0]["design"]["arms"]["stronger"]["fingerprints"]["prompt"]
    headers = [experiment.framed(f)[0] for f in record.record_files(trial["run"])]
    assert any(marker.decode().strip() in h.get("prompt", "") for h in headers if h.get("atom") == "build"), headers
    summary = experiment.summary(result)
    assert summary["arms"]["stronger"]["done"] == 0 and summary["drifted"] == 1, summary
    assert summary["pairs"]["validation"][0]["pairs"] == 0
    expect(call("experiment", "summary", str(result)), 0, "1 drifted trial left out")
    total = experiment.totals_of(experiment.events(result))
    assert trial["calls"] > 0 and total.calls == sum(t["calls"] for t in rows("trial"))
    assert total.tokens == sum(t["tokens_in"] + t["tokens_out"] for t in rows("trial"))
    build.write_bytes(original)
    clean()
    assert len([t for t in rows("trial") if t["arm"] == "baseline" and t["repeat"] == 1]) == 1
    assert [t["attempt"] for t in rows("trial") if t["arm"] == "stronger" and t["repeat"] == 1] == [1, 2]
elif mode == "versions":
    package = root / "package"
    shutil.copytree(paths.PACKAGE_DIR, package, ignore=shutil.ignore_patterns("__pycache__"))
    def change(file):
        before = experiment.engine_code(package)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes((file.read_bytes() if file.exists() else b"") + b"\nchanged\n")
        return before != experiment.engine_code(package)
    assert change(package / "pipeline.py")
    assert change(package / "lineage.mjs")
    assert change(package / "prompts/build.md")
    assert not change(package / "web/lineage.js")
    assert not change(package / "__pycache__/ignored.py")
    clean()
    expect(call("experiment", "summary", str(result), "--json"), 0, '"drifted": 0')
    with mock.patch.object(experiment, "engine_code", return_value="another engine"):
        expect(run(), 1, "the design changed")
    prices = root / "sample/prices.json"
    changed = json.loads(prices.read_text())
    changed["routes"][0]["rates"][0]["input"] += 1
    other = root / "other-prices.json"
    other.write_text(json.dumps(changed))
    expect(call("prices", "import", str(other)), 0)
    expect(run(), 1, "the design changed")
    assert len(rows("trial")) == 4
    expect(call("prices", "import", str(prices)), 0)
    clean()
elif mode == "boundaries":
    exp = experiment.load(manifest)
    layer_file = exp.layer.config_file
    layer_bytes = layer_file.read_bytes()
    layer_file.write_text("[invalid")
    assert "context" in experiment.drift(exp, "stronger", experiment.observed(exp, "stronger"))
    layer_file.write_bytes(layer_bytes)
    grade_dir = root / "grade-prompts"
    shutil.copytree(paths.PROMPTS_DIR, grade_dir)
    (grade_dir / "grade.md").write_text("changed grader")
    with mock.patch.object(paths, "PROMPTS_DIR", grade_dir):
        assert "grader" in experiment.drift(exp, "stronger", experiment.observed(exp, "stronger"))
    before = paths.CONFIG_FILE.read_text()
    paths.CONFIG_FILE.write_text(before + '\n[pricing]\nlocal_providers = ["scratch"]\n')
    assert experiment.load(manifest).digest != exp.digest
    paths.CONFIG_FILE.write_text(before)
    for key in ("context", "grader", "prices"):
        result = root / f"{key}.jsonl"
        real = experiment.run_trial
        prompt_dir = paths.PROMPTS_DIR
        price_file = root / "sample/prices.json"
        def boundary(*args, **kw):
            why = real(*args, **kw)
            if key == "context":
                layer_file.write_text("[invalid")
            elif key == "grader":
                paths.PROMPTS_DIR = grade_dir
            else:
                changed = json.loads(price_file.read_text())
                changed["routes"][0]["rates"][0]["input"] += 1
                other = root / "boundary-prices.json"
                other.write_text(json.dumps(changed))
                expect(call("prices", "import", str(other)), 0)
            return why
        try:
            with mock.patch.object(experiment, "run_trial", boundary):
                expect(run(), 3, f"the {key} changed")
        finally:
            layer_file.write_bytes(layer_bytes)
            paths.PROMPTS_DIR = prompt_dir
            expect(call("prices", "import", str(price_file)), 0)
        assert len(rows("start")) == len(rows("trial")) == 1
    result = root / "interrupted.jsonl"
    real = evals.run_task
    def interrupt(*args, **kw):
        outcome = real(*args, **kw)
        if kw["run"].trial.startswith("stronger-"):
            build.write_bytes(original + marker)
            raise KeyboardInterrupt
        return outcome
    with mock.patch.object(evals, "run_task", interrupt):
        expect(run(), 130)
    first = rows("trial")[-1]
    assert first["status"] == "interrupted"
    assert first["fingerprints"] == rows("start")[-1]["seen"]["fingerprints"]
    result = root / "recovered.jsonl"
    begun = [e for e in experiment.events(root / "interrupted.jsonl") if e["event"] == "start"][-1]
    experiment.append(result, begun)
    exp.design["arms"]["stronger"]["fingerprints"]["prompt"] = "changed design"
    experiment.settle_interrupted(exp, result)
    recovered = rows("trial")[0]
    assert recovered["fingerprints"] == begun["seen"]["fingerprints"]
    assert recovered["engine"] == begun["seen"]["engine"] and recovered["calls"] == first["calls"] > 0
    assert not experiment.completed(experiment.events(result))
print("LINEAGE_OK")
