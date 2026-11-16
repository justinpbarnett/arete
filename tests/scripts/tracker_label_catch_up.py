
import json, os, subprocess, sys, time, urllib.request
from pathlib import Path
from arete.store import runs as run_store
from arete import accounts, config, gh, paths, pipeline, record, replay, state, tracker, util, poll
root, scenario = Path(sys.argv[1]), sys.argv[2]
state.migrate()
def forbidden(*args, **kwargs):
    raise AssertionError("offline workflow attempted an external transport or credential lookup")
urllib.request.urlopen = forbidden
accounts.read = forbidden
gh.gh = forbidden
lines = replay.sample(root / "sample", adw=True)
paths.CONFIG_FILE.write_text("\n".join(lines).replace('tracker = "local"', 'tracker = "github"\ngithub = "fixture/repo"')
                             + "\n\n[jev]\nenabled=false\n")
cfg = config.load()
REPO = "replay-sample"
ISSUES, EDITS, REFUSE = {}, [], set()
def issue_labels(repo, number, add=(), remove=()):
    EDITS.append((number, list(add), list(remove)))
    if REFUSE & set(add):
        raise gh.GhError("HTTP 502: Bad Gateway")
    ISSUES[number] = (ISSUES[number] - set(remove)) | set(add)
gh.issue_labels = issue_labels
gh.issues = lambda repo, label="", **kw: [
    {"number": n, "title": f"t{n}", "body": "", "url": "", "createdAt": f"2026-01-0{n}T00:00:00Z", "author": {"login": "fixture"},
     "labels": [{"name": x} for x in sorted(names)]} for n, names in sorted(ISSUES.items()) if not label or label in names]
def row(issue, status, pending, started="2026-01-01T00:00:00Z"):
    run = record.start("pipeline", repo=REPO, issue=issue, started=started)
    if pending is not None:
        record.note(run, label_pending=pending)
    if status != "running":
        record.settle(run, status)
    return run
def pending(run):
    return run_store.run_get(run)["label_pending"]
result = {}
if scenario == "catch-up":
    ISSUES[5] = {"agent", "agent:running"}
    run = row(5, "pr", "pr")
    result["before"] = [run_store.pending_label(REPO, 5)]
    pipeline.sync_labels(cfg)
    result |= {"labels": sorted(ISSUES[5]), "pending": pending(run), "edits": len(EDITS)}
    pipeline.sync_labels(cfg)
    result["again"] = len(EDITS)
    ISSUES[5].discard("agent:pr")
    pipeline.sync_labels(cfg)
    result["after_removal"] = len(EDITS)
elif scenario == "blocks-next-ticket":
    ISSUES[5] = {"agent"}
    run = row(5, "pr", "pr")
    skips = pipeline.SkipLog({})
    repo = cfg.repo(REPO)
    result["first"] = poll.next_ticket(repo, tracker.for_repo(cfg, repo), skips) is None
    result["skips"] = json.dumps(skips.current)
    pipeline.sync_labels(cfg)
    result["labels"] = sorted(ISSUES[5])
    ISSUES[5].discard("agent:pr")
    result["released"] = poll.next_ticket(repo, tracker.for_repo(cfg, repo), pipeline.SkipLog({})) is None
elif scenario == "retains-debt":
    ISSUES[5], ISSUES[6] = {"agent", "agent:running"}, {"agent", "agent:running"}
    REFUSE.add("agent:failed")
    first, second = row(5, "failed", "failed"), row(6, "pr", "pr")
    pipeline.sync_labels(cfg)
    result |= {"debt": pending(first), "other": pending(second), "labels": sorted(ISSUES[6])}
    REFUSE.clear()
    pipeline.sync_labels(cfg)
    result |= {"later": pending(first), "failed": sorted(ISSUES[5])}
elif scenario == "no-edit":
    ISSUES[5], ISSUES[6], ISSUES[7] = {"agent"}, {"agent"}, {"agent"}
    legacy, delivered, interrupted = row(5, "pr", None), row(6, "pr", ""), row(7, "interrupted", "running")
    live = row(8, "running", "pr")
    ISSUES[8] = {"agent"}
    pipeline.sync_labels(cfg)
    result |= {"edits": len(EDITS), "legacy": pending(legacy), "interrupted": pending(interrupted), "live": pending(live)}
elif scenario == "latest-marker-governs":
    ISSUES[5] = {"agent"}
    row(5, "failed", "failed", "2026-01-01T00:00:00Z")
    row(5, "pr", "", "2026-01-02T00:00:00Z")
    row(5, "pr", None, "2026-01-03T00:00:00Z")
    result["pending"] = run_store.pending_label(REPO, 5)
    pipeline.sync_labels(cfg)
    result["edits"] = len(EDITS)
elif scenario == "ties-and-unknown-repo":
    ISSUES[5] = {"agent"}
    row(5, "failed", "failed", "2026-01-01T00:00:00Z")
    row(5, "pr", "pr", "2026-01-01T00:00:00Z")
    gone = record.start("pipeline", repo="gone", issue=9)
    record.note(gone, label_pending="pr")
    record.settle(gone, "pr")
    result["pending"] = run_store.pending_label(REPO, 5)
    pipeline.sync_labels(cfg)
    result |= {"labels": sorted(ISSUES[5]), "edits": len(EDITS)}
elif scenario == "local-task":
    task = state.task_add(REPO, "Local", "Local body", "feat")
    run = row(task, "pr", "pr")
    pipeline.sync_labels(cfg)
    result |= {"state": state.task_get(task)["state"], "pending": pending(run), "edits": len(EDITS)}
elif scenario == "held-lock":
    ISSUES[5] = {"agent"}
    run = row(5, "pr", "pr")
    ready = root / "ready"
    holder = subprocess.Popen([sys.executable, "-I", "-c",
        "import sys,time;from pathlib import Path;from arete.store import runs as run_store; from arete import state, util\n"
        "with util.lock(run_store.run_lock('pipeline', sys.argv[1], 5)):\n"
        "    Path(sys.argv[2]).write_text('x')\n"
        "    sys.stdin.readline()", REPO, str(ready)], stdin=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists()
    pipeline.sync_labels(cfg)
    result |= {"held_edits": len(EDITS), "held_pending": pending(run)}
    holder.communicate("\n", timeout=30)
    pipeline.sync_labels(cfg)
    result |= {"edits": len(EDITS), "pending": pending(run), "labels": sorted(ISSUES[5])}
print("BEHAVIOR_RESULT=" + json.dumps(result))
