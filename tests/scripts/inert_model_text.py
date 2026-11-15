
import importlib.util, json, runpy, sys, urllib.request
from pathlib import Path
from arete import accounts, config, factory, gh, paths, replay, tracker
from arete import ci as ci_module
root, model, planted, probe = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
def forbidden(*args, **kwargs):
    raise AssertionError("workflow attempted an external transport or credential lookup")
urllib.request.urlopen = forbidden
accounts.read = forbidden
if importlib.util.find_spec("arete.scratch"):
    from arete import scratch
    probe_command = [sys.executable, "-V"]
    if scratch.protected_command(probe_command) != probe_command:
        scratch.AUTHORITY = root / "authority-parent/verification-authority"
ci_module.FIRST_CHECK_SECONDS = ci_module.SETTLE_SECONDS = ci_module.POLL_SECONDS = 0
lines = replay.sample(root / "sample", adw=True)
paths.CONFIG_FILE.write_text("max_parallel=1\n\n" + "\n".join(
    'github = "fixture/repo"' if line == 'tracker = "local"' else line for line in lines) + "\n\n[jev]\nenabled=false\n")
answers = root / "sample/replay"
def rewrite(name, change):
    file = answers / name
    data = json.loads(file.read_text())
    change(data)
    file.write_text(json.dumps(data))
rewrite("build.json", lambda d: d.update(text=model))
rewrite("review.json", lambda d: d["structured"].update(summary=model))
rewrite("document.json", lambda d: d["structured"].update(text=model))
rewrite("plan.json", lambda d: d["structured"].update(plan=d["structured"]["plan"] + "\n\n" + planted))
cfg = config.load()
feature = runpy.run_path(str(paths.ADWS_DIR / "feature.py"))["feature"]
try:
    factory.main(feature, ["replay-sample", "5", "--force"])
except SystemExit:
    pass
gh.pr_comment("https://github.com/fixture/repo/pull/7", probe)
tracker.for_repo(cfg, cfg.repo("replay-sample")).comment("5", probe)
gh.issue_create("fixture/repo", "Report", probe)
