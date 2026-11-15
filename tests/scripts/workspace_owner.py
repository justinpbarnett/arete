
import json
import os
import runpy
import shutil
import sys
import time
from pathlib import Path

from arete import ci, config, factory, gh, heartbeat, paths, pipeline, record, replay, scratch, state, util

root, scenario = Path(sys.argv[1]), sys.argv[2]
state.migrate()
probe = [sys.executable, "-V"]
if scratch.protected_command(probe) != probe:
    scratch.AUTHORITY = root / "authority-parent/verification-authority"
SETUP = sys.argv[3]
def forbidden(*args, **kwargs):
    raise AssertionError("offline workflow attempted an external transport")
gh.gh = forbidden
lines = replay.sample(root / "sample", adw=True)
paths.CONFIG_FILE.write_text("max_parallel=1\n\n" + "\n".join(lines) + '\ngithub = "octo/widgets"\n\n[jev]\nenabled=false\n')
with (root / "sample/layer/config.toml").open("a") as out:
    out.write("\n[setup]\ncommands = [" + json.dumps(SETUP) + "]\n")
src = root / "sample/repo"
def registered():
    return [l for l in pipeline.git(["worktree", "list", "--porcelain"], src).splitlines() if l.startswith("worktree ")]
def records():
    return sorted(p.name for p in (paths.STATE_DIR / "workspaces").glob("*.json")) if (paths.STATE_DIR / "workspaces").exists() else []
result = {}
if scenario == "pr":
    made = []
    def pr_create(*args, **kw):
        made.append(args)
        return "https://github.com/octo/widgets/pull/7"
    gh.open_pr_url = lambda repo, head: "https://github.com/octo/widgets/pull/7" if made else ""
    gh.closed_pr_heads, gh.default_branch, gh.pr_create = lambda *a, **k: set(), lambda r: "main", pr_create
    gh.pr_body, gh.pr_edit_body, gh.pr_comment = lambda url: "", lambda *a, **k: None, lambda *a, **k: None
    gh.pr_view = lambda *a, **k: {"state": "OPEN", "isDraft": False, "headRefOid": "", "reviews": [], "comments": []}
    gh.pr_draft_and_head, gh.pr_set_draft = lambda url: (False, ""), lambda *a, **k: None
    gh.commit_checks, gh.branch_sha = lambda *a, **k: {}, lambda *a, **k: ""
    ci.FIRST_CHECK_SECONDS = ci.SETTLE_SECONDS = ci.POLL_SECONDS = 0
    task = state.task_add("replay-sample", "Add kumquat to the fixture notes", "Add kumquat", "feat")
    feature = runpy.run_path(str(paths.ADWS_DIR / "feature.py"))["feature"]
    try:
        factory.main(feature, ["replay-sample", task, "--force"])
    except SystemExit:
        pass
    row = state.ticket_runs("replay-sample", task, whole=True)[0]
    log = (paths.RUNS_DIR / row["id"] / "run.log").read_text()
    result |= {"status": row["status"], "pr_url": row["pr_url"], "folder": (paths.WORKTREES_DIR / f"replay-sample-{task}").exists(),
               "records": records(), "registered": len(registered()), "kept": "workspace kept" in log, "log": log[-1500:]}
else:
    def make(label, branch):
        task = state.task_add("replay-sample", label, label, "feat")
        ws = paths.WORKTREES_DIR / f"replay-sample-{task}"
        c = pipeline.Checkout("", "octo/widgets", src, ws, "main")
        pipeline.add_worktree(c, branch, "main")
        (ws / ".venv").mkdir()
        (ws / ".venv/.gitignore").write_text("*\n")
        (ws / ".venv/pyvenv.cfg").write_text("home\n")
        rid = record.start("pipeline", repo="replay-sample", issue=task)
        return task, ws, c, rid
    def owner(task, rid, ws):
        return {"run": rid, "repo": "replay-sample", "ticket": task, "created": util.iso(),
                "checkouts": [{"dir": "", "github": "octo/widgets", "src": str(src), "wt": str(ws)}]}
    a_task, a, a_c, a_run = make("a", "arete/a")
    pipeline.note_made(a, [a_c])
    record.settle(a_run, "merged", started=util.iso(util.now() - __import__("datetime").timedelta(days=30)),
                finished=util.iso(util.now() - __import__("datetime").timedelta(days=30)))
    old = time.time() - 30 * 86400
    os.utime(a, (old, old))
    b_task, b, b_c, b_run = make("b", "arete/b")
    pipeline.note_made(b, [b_c], owner(b_task, b_run, b))
    held_lock = util.hold_lock(state.run_lock("pipeline", "replay-sample", b_task))
    c_task, cw, c_c, c_run = make("c", "arete/c")
    record.settle(c_run, "merged", finished=util.iso())
    pipeline.note_made(cw, [c_c], owner(c_task, c_run, cw))
    (cw / "hand.txt").write_text("mine\n")
    gone = paths.WORKTREES_DIR / "gone"
    pipeline.add_worktree(pipeline.Checkout("", "octo/widgets", src, gone, "main"), "arete/gone", "main")
    shutil.rmtree(gone)
    pipeline.cleanup_worktrees(cfg=config.load())
    held = json.loads((paths.STATE_DIR / "held-workspaces.json").read_text())
    check = heartbeat.check_workspaces()[0]
    result |= {"a": [a.exists(), (paths.STATE_DIR / "workspaces" / f"{a.name}.json").exists(), str(a) in "\n".join(registered())],
               "b": b.exists(), "c": cw.exists(), "held": held, "gone": str(gone) in "\n".join(registered()),
               "check": [check.ok, check.detail]}
    (cw / "hand.txt").unlink()
    e_task, e, e_c, e_run = make("e", "arete/e")
    record.settle(e_run, "merged", finished=util.iso())
    pipeline.note_made(e, [e_c], owner(e_task, e_run, e))
    with (e / replay.SAMPLE_FILE).open("a") as out:
        out.write("by hand\n")
    pipeline.cleanup_worktrees(cfg=config.load())
    held_e = json.loads((paths.STATE_DIR / "held-workspaces.json").read_text())
    check = heartbeat.check_workspaces()[0]
    result |= {"c_after": cw.exists(), "c_record_after": (paths.STATE_DIR / "workspaces" / f"{cw.name}.json").exists(),
               "e": e.exists(), "held_e": held_e, "check_e": [check.ok, check.detail]}
    pipeline.git(["checkout", "--", replay.SAMPLE_FILE], e)
    pipeline.cleanup_worktrees(cfg=config.load())
    check = heartbeat.check_workspaces()[0]
    result |= {"e_after": e.exists(), "check_after": [check.ok, check.detail]}
    g = paths.WORKTREES_DIR / "replay-sample-42"
    g_c = pipeline.Checkout("", "octo/widgets", src, g, "main")
    pipeline.add_worktree(g_c, "arete/g", "main")
    g_run = record.start("pipeline", repo="replay-sample", issue="42")
    record.settle(g_run, "failed", finished=util.iso())
    pipeline.note_made(g, [g_c], owner("42", g_run, g))
    import types
    pipeline.tracker.for_repo = lambda cfg, repo: types.SimpleNamespace(get=lambda tid: types.SimpleNamespace(open=False))
    f_task, f, f_c, f_run = make("f", "arete/f")
    record.settle(f_run, "failed", finished=util.iso())
    pipeline.note_made(f, [f_c], owner(f_task, f_run, f))
    (f / "stray.txt").write_text("x\n")
    pipeline.cleanup_worktrees(cfg=config.load())
    held_f = json.loads((paths.STATE_DIR / "held-workspaces.json").read_text())
    check = heartbeat.check_workspaces()[0]
    result |= {"g": [g.exists(), (paths.STATE_DIR / "workspaces" / f"{g.name}.json").exists(), str(g) in "\n".join(registered())]}
    result |= {"f": f.exists(), "held_f": held_f, "check_f": [check.ok, check.detail]}
print("BEHAVIOR_RESULT=" + json.dumps(result))
