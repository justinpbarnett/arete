
import contextlib, importlib.util, io, json, os, sys
from pathlib import Path
from arete import cli, experiment, paths, record, workers
root, mode = Path(sys.argv[1]), sys.argv[2]
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
sample, results = root / "sample", root / "results.jsonl"
manifest = sample / "workers.toml"
if mode == "setup":
    paths.CONFIG_FILE.write_text("\n".join(workers.sample(sample)) + "\n")
    call("prices", "import", str(sample / "prices.json"))
    manifest.write_text(manifest.read_text().replace("repeats = 2", "repeats = 1"))
    raise SystemExit(0)
if mode != "finish":
    finished, original = [0], record.Recorder.finish
    def dies(self, res, seconds):
        finished[0] += 1
        if finished[0] == int(mode):
            os._exit(9)
        original(self, res, seconds)
    record.Recorder.finish = dies
ran = call("benchmark", "run", str(manifest), "--out", str(results))
trials = [e for e in experiment.events(results) if e.get("event") == "trial"]
print("BEHAVIOR_RESULT=" + json.dumps({"run": ran, "trials": trials, "report": workers.report(results)}))
