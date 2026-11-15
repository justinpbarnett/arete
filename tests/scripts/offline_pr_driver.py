import json, os, runpy, signal, subprocess, sys, threading, time, urllib.request
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from arete import accounts, agent, owner_asks, config, control, factory, gh, run_lines, paths, pipeline, record, replay, runlimits, scratch, state, util
root, scenario, role = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
state.migrate()
probe = [sys.executable, "-V"]
if scratch.protected_command(probe) != probe:
    scratch.AUTHORITY = root / "authority-parent/verification-authority"
URL = "https://github.com/octo/widgets/pull/7"
CALLS = root / "calls.jsonl"
def forbidden(*args, **kwargs):
    raise AssertionError("offline workflow attempted an external transport or credential lookup")
urllib.request.urlopen = forbidden
accounts.read = forbidden
gh.gh = forbidden
def fake(name, answer):
    def call(*args, **kw):
        with CALLS.open("a") as out:
            out.write(json.dumps({"call": name, "args": [str(a) for a in args[:2]]}) + "\n")
        return answer(*args, **kw)
    setattr(gh, name, call)
fake("pr_create", lambda repo, *a, **k: f"https://github.com/{repo}/pull/7")
fake("open_pr_url", lambda repo, head: URL if (root / "found").exists() else "")
fake("pr_view", lambda url: {"state": "MERGED" if (root / "merged").exists() else "OPEN",
                             "mergedAt": "2026-01-01T00:00:00Z" if (root / "merged").exists() else None,
                             "closedAt": "2026-01-01T00:00:00Z" if (root / "merged").exists() else None,
                             "isDraft": False, "headRefOid": "", "reviews": [], "comments": []})
for name in ("pr_edit_body", "pr_comment", "pr_set_draft"):
    fake(name, lambda *a, **k: None)
fake("pr_body", lambda url: "")
fake("pr_draft_and_head", lambda url: (False, ""))
fake("closed_pr_heads", lambda *a, **k: set())
fake("commit_checks", lambda *a, **k: {})
fake("branch_sha", lambda *a, **k: "")
fake("me", lambda: "owner")
fake("pr_discussion", lambda url: SimpleNamespace(reviews=[], line_comments=[], comments=[]))
gh.default_branch = lambda r: "main"
feature_of = lambda: runpy.run_path(str(paths.ADWS_DIR / "feature.py"))["feature"]
ASKING = {"questions": [{"header": "Spelling", "question": "How is it spelled?", "kind": "text"}],
          "why": "The CI fix needs it.", "tried": "Read the task."}
def stopping(b, *a, **k):
    thread = threading.Thread(target=control.stop_run, args=(b.run.id,))
    thread.start()
    thread.join()
    time.sleep(30)
    raise AssertionError("the stop never arrived")
def parking(*a, **k):
    raise runlimits.Parked("fixture limit")
def asking(b, *a, **k):
    b.run.owner_ask("implement", [ASKING], agent.NO_SESSION)
def failing_ci(*a, **k):
    raise pipeline.RunFailed("CI failed", "fixture detail")
def raising(error):
    def call(*a, **k):
        raise error
    return call
def killing(*a, **k):
    os.kill(os.getpid(), signal.SIGKILL)
if role == "kill":
    pipeline.watch_ci = killing
    factory.main(feature_of(), ["replay-sample", sys.argv[4], "--force"])
    raise SystemExit(0)
lines_of_config = replay.sample(root / "sample", adw=True)
paths.CONFIG_FILE.write_text("max_parallel=1\n\n" + "\n".join(lines_of_config) + '\ngithub = "octo/widgets"\n\n[jev]\nenabled=false\n')
record.SETTLE_GRACE_SECONDS = 0
cfg = config.load()
task = state.task_add("replay-sample", "Add kumquat to the fixture notes", "Add kumquat", "feat")
feature = feature_of()
pipeline.watch_ci = lambda *a, **k: "CI passed"
def run_ticket(*extra):
    try:
        factory.main(feature, ["replay-sample", task, "--force", *extra])
    except SystemExit:
        pass
    return state.ticket_runs("replay-sample", task, whole=True)[0]
def killed_run():
    subprocess.run([sys.executable, "-I", str(root / "driver.py"), str(root), scenario, "kill", task],
                   env=os.environ, cwd=root, start_new_session=True, timeout=100)
    pipeline.settle_abandoned_runs(cfg)
    return state.ticket_runs("replay-sample", task, whole=True)[0]
since = util.iso(util.now() - timedelta(days=30))
def snapshot(row):
    row = state.run_get(row["id"])
    read = run_lines.read(row["id"])
    reports = [e for e in read if e.get("event") == "report"]
    routes = [e for e in read if e.get("event") == "route"]
    return {"id": row["id"], "status": row["status"], "summary": row["summary"], "pr_url": row["pr_url"],
            "report_prs": reports[-1]["prs"] if reports else None,
            "reading": row["id"] in [r["id"] for r in state.runs_reading_pr_comments(since)],
            "open": row["id"] in [r["id"] for r in state.open_runs("replay-sample", task)],
            "skipped": routes[-1].get("skipped") if routes else None,
            "started_steps": [e["step"] for e in read if e.get("event") == "step_start"]}
snaps = []
def take(row):
    snaps.append(snapshot(row))
    return row
extra = {}
if scenario in ("stop", "stopped-feedback", "repair-stop"):
    pipeline.watch_ci = stopping
elif scenario == "park":
    pipeline.watch_ci = parking
elif scenario == "automerge-gh-error":
    pipeline.automerge = raising(gh.GhError("fixture gh failure"))
elif scenario == "automerge-run-failed":
    pipeline.automerge = raising(pipeline.RunFailed("fixture merge failure", "detail"))
elif scenario in ("ask", "answer-stop"):
    pipeline.watch_ci = asking
elif scenario in ("ci-failure", "retry-stop"):
    pipeline.watch_ci = failing_ci
elif scenario == "partial-create-failure":
    real_note = pipeline.note_prs
    def note_then_refuse(run, branch, with_prs):
        real_note(run, branch, with_prs)
        raise gh.GhError("the second repository refused its PR")
    pipeline.note_prs = note_then_refuse
if scenario in ("kill", "kill-twice", "restart-stop"):
    take(killed_run())
    if scenario == "kill-twice":
        take(killed_run())
    if scenario == "restart-stop":
        pipeline.watch_ci = stopping
        take(run_ticket())
elif scenario == "repair-stop":
    branch = pipeline.fill_template(config.RepoLayer.branch, config.RepoLayer.branch, kind="feat", id=task,
                                    slug=pipeline.slug("Add kumquat"))
    older = record.start("pipeline", repo="replay-sample", issue=task)
    record.note(older, branch=branch, pr_url=URL)
    record.settle(older, "pr", summary="older", pr_url=URL)
    pipeline.git(["push", "-q", "origin", f"HEAD:refs/heads/{branch}"], root / "sample/repo")
    (root / "found").write_text("")
    take(run_ticket())
    extra["open_before"] = [r["id"] for r in state.open_runs("replay-sample", task)]
    extra["older"] = older
    extra["older_before"] = state.run_get(older)["status"]
elif scenario == "answer-stop":
    first = take(run_ticket())
    for ask in state.asks_of(first["id"], "open"):
        state.ask_close(ask["id"], "answered", owner_asks.text_answers(ask, "Spelled with a K."))
    pipeline.watch_ci = stopping
    take(run_ticket("--continue", first["id"]))
elif scenario == "retry-stop":
    first = take(run_ticket())
    pipeline.watch_ci = stopping
    take(run_ticket("--continue", first["id"], "--from", "ci"))
else:
    take(run_ticket())
if scenario in ("stop", "repair-stop"):
    (root / "merged").write_text("")
    pipeline.sync_outcomes(cfg)
    extra["after_merge"] = {r["id"]: state.run_get(r["id"])["status"] for r in state.runs(kind="pipeline", limit=10)}
    extra["streak"] = pipeline.streak("replay-sample")
if scenario == "stopped-feedback":
    comment = {"pr_url": URL, "kind": "comment", "gh_id": 11, "repo": "replay-sample", "issue": task,
               "body": "Please rename it.", "at_once": 1, "created": util.iso(), "pr_state": "OPEN", "status": "new"}
    extra["added"] = [state.add_feedback([comment]), state.add_feedback([comment])]
    extra["feedback_due"] = state.feedback_due("replay-sample", util.iso())
    extra["task"] = task
calls = [json.loads(line) for line in CALLS.read_text().splitlines()] if CALLS.exists() else []
print("BEHAVIOR_RESULT=" + json.dumps({"scenario": scenario, "runs": snaps,
                                       "calls": [c["call"] for c in calls], **snaps[-1], **extra}))
