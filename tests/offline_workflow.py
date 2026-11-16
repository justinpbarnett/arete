import contextlib
import hashlib
import importlib.util
import io
import json
import os
import runpy
import select
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import slots, workspace, prompt_loader

from tests.harness import set_task_body


def note(severity, problem, **more):
    return {"file": "notes.txt", "problem": problem, "severity": severity, **more}


BLOCKER = note("blocker", "fixture blocker", category="issue")
SECURITY = note("blocker", "fixture leak", category="security")
REGRESSION = note("blocker", "fixture regression", category="regression")
MAJOR, MINOR, MAJOR_2 = note("major", "fixture major note"), note("minor", "fixture minor note"), note("major", "second major note")
FIXED = {"id": "B1", "status": "fixed", "why": "quince is listed"}
REVIEW_ROUNDS = {
    "review-notes-only": [{"verdict": "request_changes", "summary": "Works; two notes.", "problems": [MAJOR, MINOR]}],
    "review-notes-dry-run": [{"summary": "Works; two notes.", "problems": [MAJOR, MINOR]}],
    "review-converges": [{"summary": "B1 open.", "problems": [BLOCKER, MAJOR]},
                         {"summary": "B1 fixed.", "problems": [MAJOR_2], "earlier": [FIXED]}],
    "review-security-waived": [{"summary": "Leak.", "problems": [SECURITY]},
                               {"summary": "Waived.", "problems": [],
                                "earlier": [{"id": "B1", "status": "waived", "why": "out of scope"}]}],
    "review-new-regression": [{"summary": "B1 open.", "problems": [BLOCKER]},
                              {"summary": "B1 fixed, new break.", "problems": [REGRESSION], "earlier": [FIXED]}],
    "review-severity-slip": [{"summary": "Slip.", "problems": [note("Critical", "fixture slip", category="issue")]},
                             {"summary": "Still.", "problems": [], "earlier": [{"id": "B1", "status": "open", "why": "no"}]}],
    "review-fix-breaks-check": [{"summary": "B1 open.", "problems": [BLOCKER, MAJOR]},
                                {"summary": "B1 fixed.", "problems": [MAJOR_2], "earlier": [FIXED]}],
    "review-problems-missing": [{"verdict": "approve", "summary": "Fine."}],
    "review-earlier-omitted": [{"summary": "B1 open.", "problems": [BLOCKER]},
                               {"summary": "Looks fine.", "problems": []}],
    "review-multiple-fixes": [{"summary": "B1 open.", "problems": [BLOCKER]},
                              {"summary": "B1 fixed, B2 open.", "problems": [REGRESSION], "earlier": [FIXED]},
                              {"summary": "B2 fixed.", "problems": [], "earlier": [FIXED]}],
    "review-final-verify-fails": [{"summary": "B1 open.", "problems": [BLOCKER]},
                                  {"summary": "B1 fixed.", "problems": [], "earlier": [FIXED]}],
    "review-final-verify-recovers": [{"summary": "B1 open.", "problems": [BLOCKER]},
                                     {"summary": "B1 fixed.", "problems": [], "earlier": [FIXED]}],
    "review-no-fix-feedback": [{"verdict": "request_changes", "summary": "Not yet.", "problems": []},
                               {"verdict": "request_changes", "summary": "Still not.", "problems": []},
                               {"summary": "Fine.", "problems": []}],
    "refactor-review-fixes": [{"summary": "B1 open.", "problems": [BLOCKER]},
                              {"summary": "B1 fixed.", "problems": [], "earlier": [FIXED]}],
}
REFACTOR = ("refactor-pass", "refactor-review-fixes", "refactor-dry-run")
BLOCKED_CHECKS = {"blocked-check-environment": "Target page, context or browser has been closed",
                  "blocked-check-guard": "The JEV guard refused the command"}
QUINCE = "--- a/notes.txt\n+++ b/notes.txt\n@@ -1,2 +1,3 @@\n A small file for replay checks.\n kumquat\n+quince\n"
LIME = "--- a/notes.txt\n+++ b/notes.txt\n@@ -1,2 +1,2 @@\n A small file for replay checks.\n-kumquat\n+lime\n"
LESSON_A, LESSON_B = "- `notes.txt`: lesson A from the first run.", "- `notes.txt`: lesson B from the second run."
KILLED_WHILE_WAITING = """
import os, runpy, signal, sys, urllib.request
from unittest import mock
from arete import accounts, factory, gh, paths, pipeline, slots
def forbidden(*args, **kwargs):
    raise AssertionError("offline workflow attempted an external transport or credential lookup")
urllib.request.urlopen = forbidden
accounts.read = forbidden
gh.gh = forbidden
said = pipeline.Run.log
def kill_while_waiting(run, text):
    said(run, text)
    if "waiting for a free run slot" in text:
        os.kill(os.getpid(), signal.SIGKILL)
feature = runpy.run_path(str(paths.ADWS_DIR / "feature.py"))["feature"]
with mock.patch.multiple(slots, runs_in_slots=mock.Mock(return_value=[{}]),
                         slots_free_to_start=mock.Mock(return_value=0)), \\
        mock.patch.object(pipeline.Run, "log", kill_while_waiting):
    factory.main(feature, sys.argv[1:])
"""
SECOND_LEARNING_RUN = """
import runpy, sys, urllib.request
from pathlib import Path
from arete import accounts, factory, gh, paths
def forbidden(*args, **kwargs):
    raise AssertionError("offline workflow attempted an external transport or credential lookup")
urllib.request.urlopen = forbidden
accounts.read = forbidden
gh.gh = forbidden
import importlib.util
if importlib.util.find_spec("arete.scratch"):
    from arete import sandbox
    probe = [sys.executable, "-V"]
    if sandbox.protected_command(probe) != probe:
        sandbox.AUTHORITY = Path(sys.argv[1]) / "authority-parent/verification-authority"
feature = runpy.run_path(str(paths.ADWS_DIR / "feature.py"))["feature"]
try:
    factory.main(feature, sys.argv[2:])
except SystemExit:
    pass
"""


def forbidden(*args, **kwargs):
    raise AssertionError("offline workflow attempted an external transport or credential lookup")


def main(root, scenario):
    from arete import (accounts, alerts, owner_asks, codebase, config, control, environment, factory, flows, gh, run_lines,
                       paths, pipeline, record, replay, runlimits, runner, scrub, state, tasks, vault)
    state.migrate()
    scratch = sandbox = None
    if importlib.util.find_spec("arete.scratch"):
        from arete import sandbox, scratch
    fx = SimpleNamespace(root=Path(root), scenario=scenario, accounts=accounts, alerts=alerts, owner_asks=owner_asks,
                         codebase=codebase, config=config, control=control, environment=environment,
                         factory=factory, flows=flows, gh=gh, run_lines=run_lines, paths=paths, pipeline=pipeline,
                         record=record, replay=replay, runlimits=runlimits, runner=runner, sandbox=sandbox,
                         scratch=scratch, scrub=scrub, state=state, tasks=tasks, vault=vault)
    prohibit_transports(fx)
    redirect_authority(fx)
    create_foreign_record(fx)
    create_replay_config(fx)
    modify_replay(fx)
    create_task(fx)
    instrument_budget(fx)
    result = SCENARIOS[scenario](fx)
    print("BEHAVIOR_RESULT=" + json.dumps(result))


def prohibit_transports(fx):
    urllib.request.urlopen = forbidden
    fx.accounts.read = forbidden
    fx.gh.gh = forbidden


def redirect_authority(fx):
    sandbox = fx.sandbox
    if sandbox is not None:
        probe = [sys.executable, "-V"]
        if sandbox.protected_command(probe) != probe:
            sandbox.AUTHORITY = fx.root / "authority-parent/verification-authority"


def create_foreign_record(fx):
    root, scratch, sandbox = fx.root, fx.scratch, fx.sandbox
    fx.foreign = fx.foreign_bytes = None
    if fx.scenario == "foreign-version-record" and sandbox.AUTHORITY.parent == root / "authority-parent":
        import hmac
        sandbox.AUTHORITY = root.resolve() / "authority-parent/verification-authority"
        sandbox.AUTHORITY.mkdir(mode=0o700)
        key = os.urandom(32)
        secret = os.open(sandbox.AUTHORITY / "signing.secret", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(secret, key)
        os.close(secret)
        selector = "ab" * 32
        elsewhere = str(root / "elsewhere")
        data = {"version": 3, "attempt": {"selector": selector, "workspace": elsewhere, "root": elsewhere + "/.arete-scratch-x"},
                "state": "prepared"}
        fx.foreign_bytes = scratch.encoded({"data": data, "signature": hmac.digest(key, scratch.encoded(data), "sha256").hex()})
        fx.foreign = sandbox.AUTHORITY / (selector + ".json")
        fx.foreign.write_bytes(fx.foreign_bytes)


def create_replay_config(fx):
    root, scenario, paths = fx.root, fx.scenario, fx.paths
    sample_lines = fx.replay.sample(root / "sample", adw=True)
    paths.CONFIG_FILE.write_text("max_parallel=1\n\n" + "\n".join(sample_lines) +
        "\n\n[jev]\nenabled=false\n\n[steps.check_repair]\ntries=1\n\n[steps.review]\ntries=" +
        str(max(2, len(REVIEW_ROUNDS.get(scenario, ()))) if scenario == "edited-after-held-pr" or scenario in REVIEW_ROUNDS else 1) + "\n")
    fx.marker = "arete117-marker-value-9f3k2"
    if scenario == "secret-in-patch":
        fx.vault.put("replay-sample", "REPLAY_SECRET", fx.marker)
        (root / "sample/replay/build.patch").write_text(
            "--- a/notes.txt\n+++ b/notes.txt\n@@ -1 +1,2 @@\n A small file for replay checks.\n+kumquat " + fx.marker + "\n")


def modify_replay(fx):
    scenario = fx.scenario
    if scenario == "failed-check":
        failed_check_replay(fx)
    elif scenario == "missing-verification":
        (fx.root / "sample/replay/verify.json").unlink()
    elif scenario in ("verify-environment-fault", "verify-environment-fault-pass", "verify-change-fault",
                      "verify-change-fault-timed", "verify-fix-breaks-check"):
        verify_fault_replay(fx)
    elif scenario in ("verify-start-denied", "verify-start-crash"):
        verify_start_replay(fx)
    elif scenario in ("review-rejected", "edited-after-held-pr"):
        review_rejected_replay(fx)
    elif scenario in REVIEW_ROUNDS:
        review_rounds_replay(fx)
    elif scenario in BLOCKED_CHECKS:
        blocked_check_replay(fx)
    elif scenario == "no-verify-config":
        without_verify_section(fx)


def failed_check_replay(fx):
    with (fx.root / "sample/layer/config.toml").open("a") as out:
        out.write('\n[steps.check_repair]\ntries=1\n')
    layer = fx.root / "sample/layer/config.toml"
    layer.write_text(layer.read_text().replace("grep -q kumquat notes.txt", "exit 7"))


def two_repair_tries(fx):
    fx.paths.CONFIG_FILE.write_text(fx.paths.CONFIG_FILE.read_text().replace(
        "[steps.check_repair]\ntries=1", "[steps.check_repair]\ntries=2"))


def verify_fault_replay(fx):
    scenario, root, pipeline = fx.scenario, fx.root, fx.pipeline
    cause = "change" if scenario.startswith(("verify-change-fault", "verify-fix-breaks")) else "environment"
    answers = root / "sample/replay"
    failing = json.loads((answers / "verify.json").read_text())
    failing["structured"] = {"verdict": "pass" if scenario.endswith("-pass") else "fail", "summary": "The check could not run.", "checks": [
        {"what": "notes.txt holds kumquat", "how": "browser", "saw": "Target page, context or browser has been closed",
         "passed": False, "cause": cause}]}
    (answers / "verify2.json").write_text((answers / "verify.json").read_text())
    (answers / "verify.json").write_text(json.dumps(failing))
    (answers / "verify_fix.json").write_text(json.dumps({"text": "Fixed.", "ok": True}))
    fx.checks_in_verification, real_checks = [], pipeline.run_checks
    def timed_checks(layer, cwd, git_dirs, run, *args, **kw):
        fx.checks_in_verification.append(bool(run.budget and run.budget.verification_active_since))
        return real_checks(layer, cwd, git_dirs, run, *args, **kw)
    pipeline.run_checks = timed_checks
    if scenario == "verify-fix-breaks-check":
        two_repair_tries(fx)
        layer = root / "sample/layer/config.toml"
        layer.write_text(layer.read_text().replace("grep -q kumquat notes.txt", "grep -q kumquat notes.txt && ! grep -q quince notes.txt"))
        (answers / "verify_fix.patch").write_text("--- a/notes.txt\n+++ b/notes.txt\n@@ -1,2 +1,3 @@\n A small file for replay checks.\n kumquat\n+quince\n")
        (answers / "check_repair.json").write_text(json.dumps({"text": "Removed quince.", "ok": True}))
        (answers / "check_repair.patch").write_text("--- a/notes.txt\n+++ b/notes.txt\n@@ -1,3 +1,2 @@\n A small file for replay checks.\n kumquat\n-quince\n")


def verify_start_replay(fx):
    root, scenario = fx.root, fx.scenario
    urllib.request.urlopen = mock.Mock(side_effect=OSError("refused"))
    layer = root / "sample/layer/config.toml"
    denied = ("echo '/bin/bash: sandbox: Operation not permitted'; exit 126" if scenario == "verify-start-denied"
              else "echo 'at usePermissions (src/hooks/usePermissions.ts:12:5)'; exit 1")
    (root / "sample/replay/verify_fix.json").write_text(json.dumps({"text": "Fixed.", "ok": True}))
    layer.write_text(layer.read_text().replace("[verify]\n", "[verify]\nstart = " + json.dumps(denied) + "\n", 1))


def review_rejected_replay(fx):
    root, scenario = fx.root, fx.scenario
    file = root / "sample/replay/review.json"
    data = json.loads(file.read_text())
    if scenario == "edited-after-held-pr":
        (root / "sample/replay/review2.json").write_text(file.read_text())
        (root / "sample/replay/verify2.json").write_text((root / "sample/replay/verify.json").read_text())
        (root / "sample/replay/review_fix.json").write_text(json.dumps({"text": "Fixed.", "ok": True}))
    data["structured"] = {"verdict":"request_changes", "summary":"A real behavior is still wrong.",
                           "problems":[BLOCKER]}
    file.write_text(json.dumps(data))
    if scenario == "edited-after-held-pr":
        second = json.loads((root / "sample/replay/review2.json").read_text())
        second["structured"] = {**second["structured"], "earlier": [FIXED]}
        (root / "sample/replay/review2.json").write_text(json.dumps(second))


def review_rounds_replay(fx):
    root, scenario = fx.root, fx.scenario
    replays = root / "sample/replay"
    sample = json.loads((replays / "review.json").read_text())
    for number, said in enumerate(REVIEW_ROUNDS[scenario], 1):
        (replays / f"review{number if number > 1 else ''}.json").write_text(json.dumps({**sample, "structured": said}))
    if len(REVIEW_ROUNDS[scenario]) > 1:
        (replays / "verify2.json").write_text((replays / "verify.json").read_text())
        (replays / "review_fix.json").write_text(json.dumps({"text": "B1 fixed: quince is listed after kumquat.", "ok": True}))
        (replays / "review_fix.patch").write_text("--- a/notes.txt\n+++ b/notes.txt\n@@ -1,2 +1,3 @@\n A small file for replay checks.\n kumquat\n+quince\n")
    (replays / "verify3.json").write_text((replays / "verify.json").read_text())
    if scenario == "review-multiple-fixes":
        (replays / "review_fix2.json").write_text((replays / "review_fix.json").read_text())
        (replays / "review_fix2.patch").write_text("--- a/notes.txt\n+++ b/notes.txt\n@@ -1,3 +1,4 @@\n A small file for replay checks.\n kumquat\n quince\n+mango\n")
    if scenario.startswith("review-final-verify"):
        final_verify_replay(fx)
    if scenario == "review-fix-breaks-check":
        two_repair_tries(fx)
        layer = root / "sample/layer/config.toml"
        layer.write_text(layer.read_text().replace("grep -q kumquat notes.txt", "grep -q kumquat notes.txt && ! grep -q quince notes.txt"))
        (replays / "check_repair1_2.json").write_text(json.dumps({"text": "Removed quince.", "ok": True}))
        (replays / "check_repair1_2.patch").write_text("--- a/notes.txt\n+++ b/notes.txt\n@@ -1,3 +1,2 @@\n A small file for replay checks.\n kumquat\n-quince\n")


def final_verify_replay(fx):
    replays = fx.root / "sample/replay"
    passing = json.loads((replays / "verify.json").read_text())
    failing = {**passing, "structured": {"verdict": "fail", "summary": "The final check failed.", "checks": [
        {"what": "notes.txt holds kumquat", "how": "grep", "saw": "final tree is wrong", "passed": False,
         "cause": "change"}]}}
    (replays / "verify2.json").write_text(json.dumps(failing))
    (replays / "verify3.json").write_text(json.dumps(failing if fx.scenario == "review-final-verify-fails" else passing))
    (replays / "verify_fix.json").write_text(json.dumps({"text": "Fixed.", "ok": True}))
    fx.paths.CONFIG_FILE.write_text(fx.paths.CONFIG_FILE.read_text() + "\n[steps.verify]\ntries=2\n")


def without_verify_section(fx):
    layer = fx.root / "sample/layer/config.toml"
    head, rest = layer.read_text().split("[verify]\n")
    layer.write_text(head + rest.split("\n\n", 1)[1])


def label_refactor(fx):
    from arete import tracker
    real = tracker.task_ticket
    mock.patch.object(tracker, "task_ticket", lambda *a, **kw: replace_labels(real(*a, **kw))).start()


def replace_labels(ticket):
    ticket.labels = ["agent", "refactor"]
    return ticket


def blocked_check_replay(fx):
    answer = fx.root / "sample/replay/verify.json"
    said = json.loads(answer.read_text())
    said["structured"] = {"verdict": "blocked", "summary": "Could not check it.", "checks": [],
                          "blocked_by": BLOCKED_CHECKS[fx.scenario]}
    answer.write_text(json.dumps(said))


def create_task(fx):
    fx.cfg = fx.config.load()
    fx.task = fx.state.task_add("replay-sample", "Add kumquat to the fixture notes", "Add kumquat", "feat")
    if fx.scenario == "edited-after-held-pr":
        add_held_pr_history(fx)
    if fx.scenario in REFACTOR:
        label_refactor(fx)
    fx.feature = runpy.run_path(str(fx.paths.ADWS_DIR / "feature.py"))["feature"]


def add_held_pr_history(fx):
    old = fx.record.start("pipeline", repo="replay-sample", issue=fx.task)
    fx.run_lines.write(old, "step_end", step="prepare", result="pass",
                   data={"ticket": {"title": "Add kumquat", "body": "Add kumquat to the fixture notes"}})
    for n in (1, 2, 3):
        fx.run_lines.write(old, "step_start", step="implement", call=f"review_fix{n}", atom="review_repair", **{"try": n})
    fx.record.settle(old, "pr")
    with fx.state.db() as connection:
        set_task_body(fx.task, "Add kumquat to the fixture notes\n\n## Follow up\nAlso list it first.", connection=connection)
    fx.old = old


def forced(fx, *extra):
    try:
        fx.factory.main(fx.feature, ["replay-sample", fx.task, "--force", *extra])
    except SystemExit:
        pass
    return fx.state.ticket_runs("replay-sample", fx.task, whole=True)[0]


def continue_ignoring_exit(fx, arguments):
    try:
        fx.factory.main(fx.feature, arguments)
    except SystemExit:
        pass


def run_events(fx, run_id):
    return [json.loads(line) for line in (fx.paths.RUNS_DIR / run_id / "run.jsonl").read_text().splitlines()]


def instrument_budget(fx):
    scenario, pipeline, runner, runlimits = fx.scenario, fx.pipeline, fx.runner, fx.runlimits
    if not ("limit-wait" in scenario or scenario in ("work-over-budget", "failed-again-prepare-sleep")):
        return
    ahead, real_now, real_launch = [0.0], pipeline.util.now, runner.run
    pipeline.util.now = lambda: real_now() + timedelta(seconds=ahead[0])
    if scenario != "failed-again-prepare-sleep":
        runlimits.time = SimpleNamespace(monotonic=lambda: time.monotonic() + ahead[0])
    def jumping(prompt, cwd, **kw):
        if kw.get("waits") and not ahead[0]:
            if scenario in ("work-over-budget", "failed-again-prepare-sleep"):
                ahead[0] = 600.0
            else:
                with kw["waits"]("limit"):
                    ahead[0] = 600.0
        return real_launch(prompt, cwd, **kw)
    runner.run = jumping
    mock.patch.object(runlimits.Policy, "of", return_value=runlimits.Policy(max_run_minutes=1)).start()


def prepare_kept_fixture(fx):
    root, pipeline, paths = fx.root, fx.pipeline, fx.paths
    fx.layer, fx.replays = root / "sample/layer/config.toml", root / "sample/replay"
    fx.src, fx.origin = root / "sample/repo", root / "sample/origin.git"
    fx.plain, fx.stock = fx.layer.read_text(), (fx.replays / "build.patch").read_text()
    fx.ws = paths.WORKTREES_DIR / f"replay-sample-{fx.task}"
    fx.branch = prompt_loader.fill_template(fx.config.RepoLayer.branch, fx.config.RepoLayer.branch, kind="feat", id=fx.task, slug=prompt_loader.slug("Add kumquat"))
    fx.prompts, fx.real_run = [], fx.runner.run
    def spying(prompt, cwd, **kw):
        fx.prompts.append((kw.get("trace"), prompt))
        return fx.real_run(prompt, cwd, **kw)
    fx.runner.run = spying


def og(fx, *args):
    return fx.pipeline.git(list(args), fx.origin).strip()


def commit_in_src(fx, name, message, ref):
    pipeline = fx.pipeline
    (fx.src / name).write_text(name + "\n")
    pipeline.git(["add", name], fx.src)
    pipeline.util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-q", "-m", message], fx.src)
    pipeline.git(["push", "-q", "origin", ref], fx.src)


def edit_spec(fx, text):
    with fx.state.db() as c:
        set_task_body(fx.task, text, connection=c)


def seed_origin_branch(fx):
    fx.pipeline.git(["checkout", "-q", "--detach"], fx.src)
    commit_in_src(fx, "seed.txt", "seed", f"HEAD:refs/heads/{fx.branch}")
    fx.pipeline.git(["checkout", "-q", "main"], fx.src)


def continuation(fx):
    prepare_kept_fixture(fx)
    fail_first_run(fx)
    prepare_continuation(fx)
    if "stepped" in fx.scenario:
        stepped_edits(fx)
    row = final_continuation(fx)
    return continuation_result(fx, row)


def fail_first_run(fx):
    scenario, layer, plain, factory = fx.scenario, fx.layer, fx.plain, fx.factory
    layer.write_text(plain if "late" in scenario or "killed" in scenario else plain.replace("grep -q kumquat", "grep -q quince"))
    if "pushed" in scenario:
        seed_origin_branch(fx)
    if "late" in scenario:
        fx.real_publish = factory.STEPS["publish"]
        factory.STEPS["publish"] = lambda flow: fx.flows.end_as("fail", "failed", why="publishing failed after the push")
    if "killed" in scenario:
        fx.failed = kill_at_verify(fx)
    else:
        fx.failed = forced(fx)
    if "late" in scenario:
        factory.STEPS["publish"] = fx.real_publish
    layer.write_text(plain)


def kill_at_verify(fx):
    pipeline, runner, record, state, runlimits, ws = fx.pipeline, fx.runner, fx.record, fx.state, fx.runlimits, fx.ws
    mock.patch.object(runlimits.Policy, "of", return_value=runlimits.Policy(max_run_minutes=1, max_verify_minutes=1)).start()
    ready, notify = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready)
        def pause_at_verify(prompt, cwd, **kw):
            if kw.get("trace", ("", ""))[1] == "verify":
                os.write(notify, b"ready")
                while True:
                    time.sleep(1)
            return fx.real_run(prompt, cwd, **kw)
        runner.run = pause_at_verify
        forced(fx)
        os._exit(3)
    os.close(notify)
    try:
        assert select.select([ready], [], [], 30)[0], "verify never started"
        assert os.read(ready, 5) == b"ready"
    finally:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        os.close(ready)
    real_now = pipeline.util.now
    pipeline.util.now = lambda: real_now() + timedelta(seconds=600)
    failed = state.ticket_runs("replay-sample", fx.task, whole=True)[0]
    record.interrupt(failed)
    failed = state.run_get(failed["id"])
    pipeline.git(["add", "-A"], ws)
    pipeline.util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-q", "-m",
                                f"wip: edits of arete run {failed['id']}, kept when it ended before pushing\n\nArete-Run: {failed['id']}"], ws)
    workspace.note_made(ws, [workspace.Checkout("", "octo/widgets", fx.src, ws, "main")])
    return failed


def prepare_continuation(fx):
    scenario, pipeline, ws, failed, layer, plain = fx.scenario, fx.pipeline, fx.ws, fx.failed, fx.layer, fx.plain
    fx.kept_head, fx.last, fx.mid = pipeline.git(["rev-parse", "HEAD"], ws).strip(), failed, {}
    if "unrecorded" in scenario:
        (ws / "hand.txt").write_text("hand\n")
        pipeline.git(["add", "hand.txt"], ws)
        pipeline.util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-q", "-m", "hand\n\nArete-Run: " + failed["id"]], ws)
    fx.before = pipeline.git(["rev-parse", "HEAD"], ws).strip()
    if "moved" in scenario:
        pipeline.git(["checkout", "-q", "main"], fx.src)
        commit_in_src(fx, "moved.txt", "move main", "HEAD:main")
    if "reopened" in scenario:
        layer.write_text(plain + '\n[setup]\ncommands = ["exit 3"]\n')
        fx.last = fx.mid = forced(fx, "--continue", failed["id"])
        layer.write_text(plain)
        fx.mid["log"] = (fx.paths.RUNS_DIR / fx.mid["id"] / "run.log").read_text()
    if "only-prepare" in scenario:
        continue_ignoring_exit(fx, ["replay-sample", fx.task, "--continue", failed["id"], "--only", "prepare"])
        fx.mid = fx.state.ticket_runs("replay-sample", fx.task)[0]
        fx.mid["unmoved"] = pipeline.git(["rev-parse", "HEAD"], ws).strip() == fx.before


def stepped_edits(fx):
    scenario, pipeline, ws, failed, mid = fx.scenario, fx.pipeline, fx.ws, fx.failed, fx.mid
    (fx.replays / "build.patch").write_text(LIME if "broken" in scenario else QUINCE)
    continue_ignoring_exit(fx, ["replay-sample", fx.task, "--continue", failed["id"], "--only", "implement"])
    fx.mid = mid = fx.state.ticket_runs("replay-sample", fx.task)[0]
    mid["head"] = pipeline.git(["rev-parse", "HEAD"], ws).strip()
    if "broken-" in scenario:
        continue_ignoring_exit(fx, ["replay-sample", fx.task, "--continue", failed["id"], "--only",
                                    scenario.rsplit("-", 1)[1]])
        later = fx.state.ticket_runs("replay-sample", fx.task)[0]
        mid["later"] = {k: later.get(k) for k in ("only_step", "status", "summary")}
    if scenario.endswith(("-hand", "-forged")):
        trailer = "\n\nArete-Run: " + mid["id"] if scenario.endswith("-forged") else ""
        (ws / "hand.txt").write_text("hand\n")
        pipeline.git(["add", "hand.txt"], ws)
        pipeline.util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-q", "-m", "hand" + trailer], ws)
    fx.before = pipeline.git(["rev-parse", "HEAD"], ws).strip()


def final_continuation(fx):
    scenario = fx.scenario
    resuming = "stepped" in scenario or scenario in ("failed-resume-moved", "failed-resume-only-prepare-moved")
    if not resuming:
        (fx.replays / "build.patch").write_text(QUINCE)
    again = (["--from", "prepare"] if "again-prepare" in scenario else ["--from", "pr"] if "late" in scenario
             else ["--from", "checks"] if resuming else [])
    return forced(fx, "--continue", fx.last["id"], *again)


def continuation_result(fx, row):
    scenario, pipeline, runlimits, state, ws, failed, mid = fx.scenario, fx.pipeline, fx.runlimits, fx.state, fx.ws, fx.failed, fx.mid
    branch, origin, before, kept_head = fx.branch, fx.origin, fx.before, fx.kept_head
    log = (fx.paths.RUNS_DIR / row["id"] / "run.log").read_text()
    published = "refs/heads/" + branch in og(fx, "for-each-ref", "--format=%(refname)", "refs/heads")
    return {"failed": failed["summary"], "status": row["status"], "summary": row["summary"], "log": log,
            "kept": pipeline.git(["log", "-1", "--format=%B"], ws).strip() if ws.exists() else "",
            "steps": [s["step"] for s in state.steps(row["id"])],
            "started": [l["step"] for l in fx.run_lines.read(row["id"]) if l.get("event") == "step_start"],
            "failed_id": failed["id"], "failed_status": failed["status"],
            "failed_span": (pipeline.util.parse_iso(failed["finished"]) - pipeline.util.parse_iso(failed["started"])).total_seconds(),
            "failed_budget": runlimits.metadata(failed["id"], "budget.json"),
            "inherited_run": runlimits.active_seconds(failed["id"], failed),
            "inherited_verify": runlimits.verification_seconds(failed["id"], failed),
            "mid": {k: mid.get(k) for k in ("id", "status", "summary", "log", "only_step", "unmoved", "head", "later")},
            "stepped_published": published and bool(mid.get("head")) and pipeline.util.sh(["git", "merge-base", "--is-ancestor", mid["head"], branch], cwd=origin).returncode == 0,
            "seed": og(fx, "show", f"{branch}:seed.txt") if "pushed" in scenario and published else "",
            "unmoved": ws.exists() and pipeline.git(["rev-parse", "HEAD"], ws).strip() == before,
            "moved": og(fx, "show", f"{branch}:moved.txt") if "moved" in scenario and published else "",
            "published": og(fx, "show", f"{branch}:notes.txt") if published else "",
            "kept_published": published and pipeline.util.sh(["git", "merge-base", "--is-ancestor", kept_head, branch], cwd=origin).returncode == 0,
            "head_parents": og(fx, "log", "-1", "--format=%P", branch).split() if published else [], "main": og(fx, "rev-parse", "main"),
            "budget": runlimits.metadata(row["id"], "budget.json"),
            "trailers": og(fx, "log", "--format=%B", f"main..{branch}") if published else "",
            "prompts": [t[1] for t, _ in fx.prompts if t and t[0] == row["id"]]}


def kept(fx):
    prepare_kept_fixture(fx)
    result = park_first_run(fx)
    return resume_parked(fx, result)


def park_first_run(fx):
    scenario, pipeline, runlimits, layer, plain, replays, ws, branch = (
        fx.scenario, fx.pipeline, fx.runlimits, fx.layer, fx.plain, fx.replays, fx.ws, fx.branch)
    fx.seed = ""
    if scenario in ("kept-park-pushed", "kept-removed"):
        seed_origin_branch(fx)
        fx.seed = og(fx, "rev-parse", f"refs/heads/{branch}")
    if scenario == "park-no-edits":
        fx.plan = replays / "plan.json"
        fx.plan_answer = fx.plan.read_text()
        fx.plan.write_text(json.dumps(json.loads(fx.plan_answer) | {"seconds": 30}))
        policy = runlimits.Policy(max_run_minutes=.03)
    else:
        two_repair_tries(fx)
        layer.write_text(plain.replace("grep -q kumquat", "grep -q quince"))
        policy = runlimits.Policy(max_repair_cycles=0)
    with mock.patch.object(runlimits.Policy, "of", return_value=policy):
        parked = forced(fx)
    layer.write_text(plain)
    head = pipeline.git(["rev-parse", "HEAD"], ws).strip()
    result = {"parked": parked["summary"], "head": head, "main": og(fx, "rev-parse", "main"), "seed": fx.seed,
              "message": pipeline.git(["log", "-1", "--format=%B"], ws).strip(),
              "parent": pipeline.git(["rev-parse", "-q", "--verify", "HEAD~1"], ws, check=False).strip(), "dirty": pipeline.git(["status", "--short"], ws).strip(),
              "parked_id": parked["id"], "recorded": json.loads(runlimits.artifact(parked["id"], "parked.json").read_text()),
              "origin": og(fx, "for-each-ref", "--format=%(refname)", "refs/heads")}
    if scenario == "kept-removed":
        pipeline.git(["worktree", "remove", "--force", str(ws)], fx.src)
        pipeline.git(["branch", "-D", branch], fx.src)
    return result


def resume_parked(fx, result):
    scenario, pipeline, ws, branch = fx.scenario, fx.pipeline, fx.ws, fx.branch
    edit_spec(fx, "Add kumquat on a last line of its own, and quince")
    if scenario == "kept-push-then-fail":
        fx.factory.STEPS["publish"] = lambda flow: fx.flows.end_as("fail", "failed", why="publishing failed after the push")
    if scenario in ("kept-park", "kept-park-pushed", "kept-push-then-fail"):
        (fx.replays / "build.patch").write_text("--- a/notes.txt\n+++ b/notes.txt\n@@ -1,2 +1,3 @@\n A small file for replay checks.\n kumquat\n+quince\n")
    if scenario == "park-no-edits":
        fx.plan.write_text(fx.plan_answer)
    row = forced(fx)
    log = (fx.paths.RUNS_DIR / row["id"] / "run.log").read_text()
    result |= {"status": row["status"], "summary": row["summary"], "id": row["id"], "log": log,
               "trailers": og(fx, "log", "--format=%B", f"main..{branch}") if "refs/heads/" + branch in og(fx, "for-each-ref", "--format=%(refname)", "refs/heads") else "",
               "published": og(fx, "show", f"{branch}:notes.txt"),
               "onto_recorded": [k for k in workspace.made_before(ws) if k.startswith("\0onto")],
               "build_prompt": next((p for t, p in fx.prompts if t and t[0] == row["id"] and t[1] == "build"), "")}
    return result


def held_scratch(fx):
    result = park_with_held_scratch(fx)
    return release_held_scratch(fx, result)


def park_with_held_scratch(fx):
    root, paths, pipeline, replay, runlimits = fx.root, fx.paths, fx.pipeline, fx.replay, fx.runlimits
    layer, answer = root / "sample/layer/config.toml", root / "sample/replay/verify.json"
    plain, passing = layer.read_text(), answer.read_text()
    fx.prefix = paths.SCRATCH_PREFIX + "0123456789abcdef"
    holding = f"mkdir -p {fx.prefix} && echo '*' > {fx.prefix}/.gitignore && " + replay.SAMPLE_CHECK
    commit = "git -c user.name=fixture -c user.email=fixture@localhost -c commit.gpgsign=false commit -q --allow-empty -m local"
    layer.write_text(plain.replace(json.dumps(replay.SAMPLE_CHECK), json.dumps(holding)) +
                     "\n[setup]\ncommands = [" + json.dumps(commit) + "]\n")
    answer.write_text(json.dumps(json.loads(passing) | {"seconds": 30}))
    with mock.patch.object(runlimits.Policy, "of", return_value=runlimits.Policy(max_verify_minutes=.05)):
        parked = forced(fx)
    layer.write_text(plain)
    answer.write_text(passing)
    fx.ws = ws = paths.WORKTREES_DIR / f"replay-sample-{fx.task}"
    fx.saved = workspace.made_record(ws).read_text()
    made = json.loads(fx.saved)
    return {"parked": parked["summary"], "recorded": [k for k in made if not k.startswith("\0")],
            "kept": pipeline.git(["log", "-1", "--format=%B"], ws).strip(),
            "head_recorded": made.get(workspace.head_key(workspace.Checkout("", "", ws, ws, "main"))) == pipeline.git(["rev-parse", "HEAD"], ws).strip(),
            "unpushed": pipeline.git(["rev-list", "--count", "HEAD", "--not", "--remotes=origin"], ws).strip(),
            "held": [p.name for p in ws.glob(paths.SCRATCH_PREFIX + "*")],
            "left": [p.name for p in workspace.scratch_left(ws, [])]}


def release_held_scratch(fx, result):
    root, paths, pipeline, scratch, ws = fx.root, fx.paths, fx.pipeline, fx.scratch, fx.ws
    with fx.state.db() as c:
        set_task_body(fx.task, "Add kumquat on a last line of its own", connection=c)
    refused = forced(fx)
    result["while_held"] = refused["summary"]
    result["record_kept"] = workspace.made_record(ws).read_text() == fx.saved
    for fd in list(scratch.LEASES.values()):
        os.close(fd)
    scratch.LEASES.clear()
    with mock.patch.object(scratch, "holders", return_value=[]):
        pipeline.retry_scratch()
    if result["held"] == [fx.prefix]:
        (root / "finalized").mkdir()
        for p in ws.glob(paths.SCRATCH_PREFIX + "*"):
            p.rename(root / "finalized" / p.name)
    (root / "sample/replay/build.patch").unlink()
    if fx.scenario == "held-scratch-edited":
        with (ws / "notes.txt").open("a") as out:
            out.write("edited by hand\n")
    row = forced(fx)
    events = run_events(fx, row["id"])
    result |= {"status": row["status"], "summary": row["summary"],
               "prepared": [e.get("result") for e in events if e.get("event") == "step_end" and e.get("step") == "prepare"],
               "origin": pipeline.git(["for-each-ref", "--format=%(refname)", "refs/heads"], root / "sample/origin.git"),
               "edited": (ws / "notes.txt").exists() and "edited by hand" in (ws / "notes.txt").read_text()}
    branches = [b for b in result["origin"].splitlines() if b != "refs/heads/main"]
    result["published"] = pipeline.git(["show", branches[0] + ":notes.txt"], root / "sample/origin.git") if len(branches) == 1 else ""
    return result


def fix_ask(fx):
    fix_ask_park(fx)
    fix_ask_base(fx)
    row = fix_ask_continue(fx)
    result = fix_ask_result(fx, row)
    fix_ask_published(fx, result)
    if fx.scenario == "fix-ask-conflict":
        fix_ask_conflict(fx, row, result)
    return result


def fix_ask_park(fx):
    root, paths, pipeline, state = fx.root, fx.paths, fx.pipeline, fx.state
    fx.alerts.notify = lambda title, message: None
    fx.src, fx.origin, fx.answers = root / "sample/repo", root / "sample/origin.git", root / "sample/replay"
    two_repair_tries(fx)
    layer = root / "sample/layer/config.toml"
    layer.write_text(layer.read_text().replace("grep -q kumquat notes.txt", "grep -q kumquat notes.txt && test -f moved.txt"))
    question = {"questions": [{"header": "Fix", "question": "Does main fix it?", "kind": "text"}],
                "why": "The check needs a file.", "tried": "Read the check."}
    (fx.answers / "fix2.json").write_text(json.dumps({"text": "Asked.", "ok": True, "tool_calls": [{"name": "ask_owner", "input": question}]}))
    fx.parked = forced(fx)
    fx.ws = paths.WORKTREES_DIR / f"replay-sample-{fx.task}"
    fx.kept_head = pipeline.git(["rev-parse", "HEAD"], fx.ws).strip()
    fx.first = run_events(fx, fx.parked["id"])
    fx.waiting = state.asks_of(fx.parked["id"], "open")


def commit_on_main(fx, name, text, message):
    pipeline = fx.pipeline
    pipeline.git(["checkout", "-q", "main"], fx.src)
    with (fx.src / name).open("a") as out:
        out.write(text)
    pipeline.git(["add", name], fx.src)
    pipeline.util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-q", "-m", message], fx.src)
    pipeline.git(["push", "-q", "origin", "HEAD:main"], fx.src)


def fix_ask_base(fx):
    if fx.scenario == "fix-ask-moved":
        commit_on_main(fx, "moved.txt", "moved\n", "move main")
    if fx.scenario == "fix-ask-conflict":
        commit_on_main(fx, "notes.txt", "quince\n", "conflict on main")
    if fx.scenario == "fix-ask-still":
        (fx.answers / "fix2.patch").write_text("--- /dev/null\n+++ b/moved.txt\n@@ -0,0 +1 @@\n+moved\n")


def fix_ask_continue(fx):
    state, runlimits = fx.state, fx.runlimits
    for ask in fx.waiting:
        state.ask_close(ask["id"], "answered", fx.owner_asks.text_answers(ask, "Main now has the fix."))
    (fx.answers / "fix2.json").write_text(json.dumps({"text": "Fixed.", "ok": True}))
    with mock.patch.object(runlimits.Policy, "of", return_value=runlimits.Policy(max_repair_cycles=5)):
        continue_ignoring_exit(fx, ["replay-sample", fx.task, "--force", "--continue", fx.parked["id"]])
    return state.ticket_runs("replay-sample", fx.task, whole=True)[0]


def fix_ask_result(fx, row):
    paths, record, state, parked, first = fx.paths, fx.record, fx.state, fx.parked, fx.first
    events = run_events(fx, row["id"])
    marks = [json.loads(line) for line in record.raw_lines(paths.record_dir(row["id"]) / record.TIMELINE)]
    merges = [m for m in marks if m.get("kind") == "base_merge"]
    kinds = [(m.get("kind"), m.get("step")) for m in marks]
    merged_first = ("base_merge", None) in kinds and ("agent", "fix2") in kinds and kinds.index(("base_merge", None)) < kinds.index(("agent", "fix2"))
    session = lambda read: next((e["session"]["id"] for e in read if e.get("event") == "step_end" and e.get("step") == "implement" and e.get("session")), "")
    headers = [f for f in record.record_files(row["id"]) if "fix2" in f.name]
    prompt = str(json.loads(next(record.raw_lines(headers[-1]))).get("prompt")) if headers else ""
    return {"parked": parked["status"], "status": row["status"], "id": row["id"], "summary": row["summary"],
            "log": (paths.RUNS_DIR / row["id"] / "run.log").read_text(),
            "skipped": next((e["skipped"] for e in events if e.get("event") == "route" and "skipped" in e), None),
            "renewed": [e.get("renew") for e in events if e.get("event") == "route" and e.get("renew")],
            "merges": [{k: m.get(k) for k in ("outcome", "error")} for m in merges], "merged_first": merged_first,
            "same_session": bool(session(first)) and session(first) == session(events),
            "answered": "Main now has the fix." in prompt, "prompt": prompt, "parked_id": parked["id"],
            "taken": [a["taken_by"] for a in state.asks_of(parked["id"])]}


def fix_ask_published(fx, result):
    pipeline, origin = fx.pipeline, fx.origin
    branches = [b for b in pipeline.git(["for-each-ref", "--format=%(refname)", "refs/heads"], origin).splitlines() if b != "refs/heads/main"]
    if branches:
        show = lambda name: pipeline.git(["show", f"{branches[0]}:{name}"], origin, check=False)
        result |= {"notes": show("notes.txt"), "moved_file": show("moved.txt"),
                   "parents": len(pipeline.git(["log", "-1", "--format=%P", branches[0]], origin).split()),
                   "trailers": pipeline.git(["log", "-1", "--format=%B", branches[0]], origin)}


def fix_ask_conflict(fx, row, result):
    pipeline, tasks = fx.pipeline, fx.tasks
    result |= {"kept_head": fx.kept_head, "head": pipeline.git(["rev-parse", "HEAD"], fx.ws).strip(),
               "dirty": pipeline.git(["status", "--short"], fx.ws).strip()}
    with mock.patch.object(tasks, "launch", side_effect=lambda cfg, repo, tid, command, ahead=False: command):
        result["resume"] = tasks.resume(row["id"])


def asked(fx):
    asked_setup(fx)
    result = asked_park(fx)
    asked_answers(fx, result)
    continuing = restore_replay(fx)
    if fx.scenario.endswith("-waiting"):
        continuing = slot_wait(fx, result, continuing)
    row, events = asked_continue(fx, result, continuing)
    asked_extras(fx, result, events)
    asked_published(fx, result)
    return result


def asked_setup(fx):
    root, scenario = fx.root, fx.scenario
    fx.notices = []
    fx.alerts.notify = lambda title, message: fx.notices.append(title)
    fx.answers = answers = root / "sample/replay"
    fx.passing, fx.patch = (answers / "build.json").read_text(), (answers / "build.patch").read_text()
    asking = {"questions": [{"header": "Spelling", "question": "How is it spelled?", "kind": "text"}],
              "why": "The task leaves it open.", "tried": "Read the task."}
    placing = {**asking, "questions": [{"header": "Place", "question": "Where does it go?", "kind": "text"}]}
    if scenario == "asked-secret":
        layer_file = root / "sample/layer/config.toml"
        layer_file.write_text(layer_file.read_text().replace(
            "[checks]\n", "[checks]\nsecret = \"test ${#REPLAY_SECRET} -eq " + str(len(fx.marker)) + "\"\n"))
    if scenario in ("asked-secret", "asked-declined"):
        asking = {"questions": [{"header": "Token", "question": "What is the token?", "kind": "secret",
                                 "name": "REPLAY_SECRET"}], "why": "The check needs it.", "tried": "Read the task."}
    calls = [asking, placing] if scenario == "asked-twice" else [asking]
    (answers / "build.json").write_text(json.dumps({"text": "Asked.", "ok": True,
                                                    "tool_calls": [{"name": "ask_owner", "input": a} for a in calls]}))
    fx.edited = scenario == "asked-edited"
    if not fx.edited:
        (answers / "build.patch").unlink()


def asked_park(fx):
    pipeline = fx.pipeline
    fx.parked = forced(fx)
    fx.ws = ws = fx.paths.WORKTREES_DIR / f"replay-sample-{fx.task}"
    fx.waiting = fx.state.asks_of(fx.parked["id"], "open")
    fx.first = run_events(fx, fx.parked["id"])
    return {"parked": fx.parked["status"], "worktree": ws.is_dir(), "open": len(fx.waiting), "notices": len(fx.notices),
            "parked_id": fx.parked["id"], "kept": pipeline.git(["log", "-1", "--format=%B"], ws).strip() if ws.is_dir() else "",
            "dirty": pipeline.git(["status", "--short"], ws).strip() if ws.is_dir() else ""}


def asked_answers(fx, result):
    scenario, state, tasks, waiting = fx.scenario, fx.state, fx.tasks, fx.waiting
    if scenario == "asked-moved":
        fx.src = fx.root / "sample/repo"
        fx.pipeline.git(["checkout", "-q", "main"], fx.src)
        (fx.src / "moved.txt").write_text("moved\n")
        fx.pipeline.git(["add", "moved.txt"], fx.src)
        fx.pipeline.util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-q", "-m", "move main"], fx.src)
        fx.pipeline.git(["push", "-q", "origin", "HEAD:main"], fx.src)
    if scenario == "asked-twice":
        answer_twice(fx, result)
    if scenario == "asked-secret":
        answer_secret(fx, result)
    elif scenario == "asked-declined":
        with mock.patch.object(tasks, "continue_asked", return_value={}):
            tasks.answer_ask(waiting[0]["id"], {"answers": [{"declined": True}]})
    else:
        for ask in waiting:
            state.ask_close(ask["id"], "answered", fx.owner_asks.text_answers(ask, "Spelled with a K, as in kumquat-7."))


def answer_twice(fx, result):
    control, tasks, waiting, parked = fx.control, fx.tasks, fx.waiting, fx.parked
    tasks.answer_ask(waiting[0]["id"], {"answers": [{"text": "Spelled with a K, as in kumquat-7."}]})
    for name, call in (("resent", lambda: tasks.answer_ask(waiting[0]["id"], {"answers": [{"text": "again"}]})),
                       ("resumed", lambda: tasks.resume(parked["id"]))):
        try:
            result[name] = call()
        except control.Refused as e:
            result[name] = str(e)


def answer_secret(fx, result):
    cfg, control, environment, tasks, vault, runner, marker = fx.cfg, fx.control, fx.environment, fx.tasks, fx.vault, fx.runner, fx.marker
    waiting = fx.waiting
    other = fx.config.Repo(name="replay-other", path=fx.root / "sample/repo")
    try:
        tasks.reply_to_asks(cfg, cfg.repo("replay-sample"), fx.parked, waiting, "a reply on the ticket")
    except control.Refused as e:
        result["reply"] = str(e)
    try:
        tasks.answer_ask(waiting[0]["id"], {"answers": [{"secret": "short"}]})
    except control.Refused as e:
        result["short"] = str(e)
    result["reserved"] = runner.reserved("PATH")
    result["stored_before"] = vault.read("replay-sample")
    with mock.patch.object(tasks, "continue_asked", return_value={}):
        tasks.answer_ask(waiting[0]["id"], {"answers": [{"secret": marker}]})
    result["mode"] = oct(vault.file_of("replay-sample").stat().st_mode & 0o777)
    result["names"] = sorted(vault.read("replay-sample"))
    result["seen"] = "REPLAY_SECRET" in environment.commands(cfg, cfg.repo("replay-sample"))
    result["unseen"] = "REPLAY_SECRET" in environment.commands(cfg, other)
    result["reserved_names"] = [runner.reserved(n) for n in ("ARETE_RUN_ID", "CLAUDE_CODE_OAUTH_TOKEN", "REPLAY_SECRET")]
    result["diff_refusal"] = vault.refusals(f"diff --git a/notes.txt b/notes.txt\n+++ b/notes.txt\n@@ -1 +1 @@\n+kumquat {marker}\n")
    quoted = 'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\n--- "a/caf\\303\\251.txt"\n+++ "b/caf\\303\\251.txt"\n@@ -1 +1 @@\n+' + marker + '\n'
    result["quoted"] = sorted(vault.held(quoted))
    result["escaped"] = "caf\\u00e9-token-123" in fx.scrub.secrets(given=["café-token-123"])


def restore_replay(fx):
    answers = fx.answers
    gone = fx.scenario == "asked-gone"
    (answers / "build.json").write_text(json.dumps({"ok": False, "error": "No conversation found with session ID x"})
                                        if gone else fx.passing)
    if gone:
        (answers / "build2.json").write_text(fx.passing)
    if fx.edited:
        (answers / "build.patch").unlink()
    else:
        (answers / "build.patch").write_text(fx.patch)
    return ["replay-sample", fx.task, "--force", "--continue", fx.parked["id"]]


def slot_wait(fx, result, continuing):
    scenario, state, tasks, control, parked, waiting = fx.scenario, fx.state, fx.tasks, fx.control, fx.parked, fx.waiting
    if scenario == "asked-stopped-waiting":
        stop_while_waiting_for_slot(fx, result, continuing)
    else:
        kill_while_waiting_for_slot(fx, continuing)
    waited = state.ticket_runs("replay-sample", fx.task, limit=1, whole=True)[0]
    lines_of = run_events(fx, waited["id"])
    result |= {"waited": waited["status"],
               "waited_continues": [e.get("continues") for e in lines_of if e.get("event") == "route"] == [parked["id"]],
               "untaken": [a["taken_by"] for a in state.asks_of(parked["id"])]}
    if scenario != "asked-stopped-waiting":
        return continuing[:3]
    try:
        tasks.answer_ask(waiting[0]["id"], {"answers": [{"text": "again"}]})
    except control.Refused as e:
        result["refusal_names_it"] = f"arete resume {waited['id']}" in str(e)
    with mock.patch.object(tasks, "launch", side_effect=lambda cfg, repo, tid, command, ahead=False: command):
        command = tasks.resume(waited["id"])
    result["resumes_it"] = command[-2:] == ["--continue", waited["id"]] and "feature.py" in " ".join(command)
    return [*continuing[:-1], waited["id"]]


def stop_while_waiting_for_slot(fx, result, continuing):
    pipeline, record, tasks, control, waiting = fx.pipeline, fx.record, fx.tasks, fx.control, fx.waiting
    full = lambda: mock.patch.multiple(slots, runs_in_slots=mock.Mock(return_value=[{}]),
                                       slots_free_to_start=mock.Mock(return_value=0))
    said = pipeline.Run.log
    def stop_while_waiting(run, text):
        said(run, text)
        if "waiting for a free run slot" in text:
            try:
                tasks.answer_ask(waiting[0]["id"], {"answers": [{"text": "again"}]})
            except control.Refused as e:
                result["while_starting"] = str(e)
            record.ask_stop(run.id)
            os.kill(os.getpid(), signal.SIGTERM)
    with full(), mock.patch.object(pipeline.Run, "log", stop_while_waiting):
        continue_ignoring_exit(fx, continuing)


def kill_while_waiting_for_slot(fx, continuing):
    source = str(Path(fx.pipeline.__file__).resolve().parent.parent)
    subprocess.run([sys.executable, "-I", "-c", f"import sys;sys.path.insert(0,{source!r});" + KILLED_WHILE_WAITING,
                    *continuing], start_new_session=True, timeout=60)
    fx.record.interrupt(fx.state.ticket_runs("replay-sample", fx.task, limit=1, whole=True)[0])


def asked_continue(fx, result, continuing):
    paths, record, state, parked, first = fx.paths, fx.record, fx.state, fx.parked, fx.first
    continue_ignoring_exit(fx, continuing)
    row = state.ticket_runs("replay-sample", fx.task, whole=True)[0]
    events = run_events(fx, row["id"])
    session = lambda read: next(e["session"]["id"] for e in read if e.get("event") == "step_end" and e.get("step") == "implement" and e.get("session"))
    header = json.loads(next(record.raw_lines([f for f in record.record_files(row["id"]) if "build" in f.name][-1])))
    prompts = str(header.get("prompt"))
    result |= {"status": row["status"], "id": row["id"], "again": row["id"] != parked["id"],
               "skipped": next((e["skipped"] for e in events if e.get("event") == "route" and "skipped" in e), None),
               "same_session": session(first) == session(events),
               "answered": "Spelled with a K, as in kumquat-7." in prompts,
               "taken": [a["taken_by"] == row["id"] for a in state.asks_of(parked["id"])],
               "renewed": [e.get("renew") for e in events if e.get("event") == "route" and e.get("renew")],
               "log": (paths.RUNS_DIR / row["id"] / "run.log").read_text(),
               "heard": [w for w in ("`REPLAY_SECRET` is set for this repo", fx.owner_asks.DECLINED) if w in prompts]}
    return row, events


def asked_extras(fx, result, events):
    vault = fx.vault
    if fx.scenario == "asked-declined":
        result["answer_lines"] = [e.get("answers") for e in events if e.get("event") == "answer"]
        result["stored_after"] = sorted(vault.read("replay-sample"))
    if fx.scenario == "asked-secret":
        environment, cfg, paths = fx.environment, fx.cfg, fx.paths
        def removing(cmd, **kw):
            vault.remove("replay-sample", "REPLAY_SECRET")
            return subprocess.CompletedProcess(cmd, 0, stdout=kw["env"]["REPLAY_SECRET"], stderr="")

        with mock.patch.object(environment.util, "sh", side_effect=removing):
            result["raced"] = environment.sh("true", cfg, cfg.repo("replay-sample")).stdout
        result["after_removal"] = "REPLAY_SECRET" in environment.commands(cfg, cfg.repo("replay-sample"))
        result["answer_lines"] = [e.get("answers") for e in events if e.get("event") == "answer"]
        result["leaks"] = [str(p) for base in (paths.STATE_DIR, paths.LOG_DIR, paths.WORKTREES_DIR) for p in base.rglob("*")
                           if p.is_file() and not p.is_symlink() and fx.marker.encode() in p.read_bytes()]


def asked_published(fx, result):
    origin = fx.root / "sample/origin.git"
    branches = [b for b in fx.pipeline.git(["for-each-ref", "--format=%(refname)", "refs/heads"], origin).splitlines()
                if b != "refs/heads/main"]
    result["published"] = fx.pipeline.git(["show", branches[0] + ":notes.txt"], origin) if len(branches) == 1 else ""


def learning(*lessons):
    added = [l.split(": ", 1)[1] for l in lessons]
    return json.dumps({"text": "Learned.", "structured": {"expertise": "# Expertise\n\n## Notes\n" + "\n".join(lessons),
                                                          "added": added, "removed": []}})


def learn(fx):
    learn_setup(fx)
    start_overlapping_run(fx)
    row = forced(fx)
    return learn_result(fx, row)


def learn_setup(fx):
    root, scenario, pipeline = fx.root, fx.scenario, fx.pipeline
    fx.paths.CONFIG_FILE.write_text(fx.paths.CONFIG_FILE.read_text().replace("max_parallel=1", "max_parallel=2").replace(
        "[steps.check_repair]\ntries=1", "[steps.check_repair]\ntries=2"))
    fx.layer_dir = layer_dir = root / "sample/layer"
    (layer_dir / "config.toml").write_text((layer_dir / "config.toml").read_text().replace("grep -q kumquat", "grep -q quince"))
    answers = root / "sample/replay"
    (answers / "fix2.json").write_text(json.dumps({"text": "Fixed.", "ok": True}))
    (answers / "fix2.patch").write_text("--- a/notes.txt\n+++ b/notes.txt\n@@ -1,2 +1,3 @@\n A small file for replay checks.\n kumquat\n+quince\n")
    fx.other_answers = root / "sample/replay-b"
    shutil.copytree(answers, fx.other_answers)
    (answers / "learn.json").write_text(learning(LESSON_A))
    (fx.other_answers / "learn.json").write_text(learning(LESSON_B))
    fx.overlapping = scenario.startswith("learn-overlap")
    if scenario == "learn-overlap":
        (answers / "learn2.json").write_text(learning(LESSON_A, LESSON_B))
    elif scenario == "learn-unrelated-file":
        pipeline.git(["init", "-q"], layer_dir)
        pipeline.git(["add", "config.toml"], layer_dir)
        pipeline.util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-q", "-m", "layer"], layer_dir)
        with (layer_dir / "config.toml").open("a") as out:
            out.write("\n# edited by hand\n")
        (layer_dir / "config.toml.bak").write_text("backup\n")


def start_overlapping_run(fx):
    root, runner = fx.root, fx.runner
    fx.other, fx.child = None, []
    if not fx.overlapping:
        return
    fx.other = other = fx.state.task_add("replay-sample", "Add kumquat again", "Add kumquat again", "feat")
    source = str(Path(fx.pipeline.__file__).resolve().parent.parent)
    real_launch, started = runner.run, []
    def overlapping_launch(prompt, cwd, **kw):
        trace = kw.get("trace") or ()
        if not started and len(trace) > 1 and trace[1] == "learn":
            started.append(True)
            done = subprocess.run([sys.executable, "-I", "-c", f"import sys;sys.path.insert(0,{source!r});" + SECOND_LEARNING_RUN,
                                   str(root), "replay-sample", other, "--force"],
                                  env={**os.environ, "ARETE_REPLAY_DIR": str(fx.other_answers)}, timeout=90,
                                  capture_output=True, text=True)
            fx.child.append(done.stderr[-2000:] if done.returncode else "")
        return real_launch(prompt, cwd, **kw)
    runner.run = overlapping_launch


def learn_result(fx, row):
    paths, pipeline, record, layer_dir, other = fx.paths, fx.pipeline, fx.record, fx.layer_dir, fx.other
    gitlog = lambda *args: pipeline.git(["log", *args], layer_dir) if (layer_dir / ".git").exists() else ""
    result = {"status": row["status"], "child": fx.child, "log": (paths.RUNS_DIR / row["id"] / "run.log").read_text(),
              "expertise": (layer_dir / "expertise.md").read_text() if (layer_dir / "expertise.md").exists() else "",
              "run_expertise": (paths.RUNS_DIR / row["id"] / "expertise.md").read_text() if (paths.RUNS_DIR / row["id"] / "expertise.md").exists() else "",
              "subjects": gitlog("--format=%s").splitlines(), "names": gitlog("--format=", "--name-only").split(),
              "head": pipeline.git(["rev-parse", "HEAD"], layer_dir).strip() if (layer_dir / ".git").exists() else "",
              "recorded": (paths.RUNS_DIR / row["id"] / "expertise-commit.txt").read_text().strip()
                          if (paths.RUNS_DIR / row["id"] / "expertise-commit.txt").exists() else "",
              "other": fx.state.ticket_runs("replay-sample", other, whole=True)[0]["status"] if other else "",
              "learn2_prompt": ""}
    for file in record.record_files(row["id"]):
        if "learn2" in file.name:
            result["learn2_prompt"] = str(json.loads(next(record.raw_lines(file))).get("prompt"))
    if fx.scenario == "learn-unrelated-file":
        result["last_commit"] = pipeline.git(["show", "--name-only", "--format=", "HEAD"], layer_dir).split()
        result["status_short"] = pipeline.git(["status", "--short"], layer_dir).splitlines()
    return result


def instrument_future_step(fx):
    paths, runner = fx.paths, fx.runner
    planned, fx.appended, before_edit = [], [], runner.run
    def edit_after_plan(prompt, cwd, **kw):
        if planned and not fx.appended:
            fx.appended.append(True)
            with paths.CONFIG_FILE.open("a") as out:
                out.write("\n[steps.future_step]\ntries=1\n")
        done = before_edit(prompt, cwd, **kw)
        if (kw.get("trace") or ("", ""))[1:2] == ("plan",):
            planned.append(True)
        return done
    runner.run = edit_after_plan


def ordinary(fx):
    scenario, paths, pipeline, state = fx.scenario, fx.paths, fx.pipeline, fx.state
    if scenario == "future-step-appended":
        instrument_future_step(fx)
    dry = scenario in ("review-notes-dry-run", "refactor-dry-run")
    row = forced(fx, "--dry-run") if dry else forced(fx)
    events = run_events(fx, row["id"])
    verification = [e.get("data", {}).get("verified") for e in events
                    if e.get("event") == "step_end" and e.get("step") == "verify"]
    result = {"status":row["status"], "summary":row["summary"], "steps":[s["step"] for s in state.steps(row["id"])],
              "verified":[v["verdict"] for v in verification if v],
              "origin":pipeline.git(["for-each-ref", "--format=%(refname)", "refs/heads"], fx.root / "sample/origin.git")}
    if scenario == "secret-in-patch":
        result["log"] = (paths.RUNS_DIR / row["id"] / "run.log").read_text()
        result["leaks"] = [str(p) for base in (paths.STATE_DIR, paths.LOG_DIR) for p in base.rglob("*")
                           if p.is_file() and not p.is_symlink() and fx.marker.encode() in p.read_bytes()]
    if scenario == "verify-change-fault-timed":
        result["checks_in_verification"] = fx.checks_in_verification
    if scenario == "edited-after-held-pr":
        result["old"] = fx.old
        result["log"] = (paths.RUNS_DIR / row["id"] / "run.log").read_text()
        result["budget"] = json.loads((paths.RUNS_DIR / row["id"] / "budget.json").read_text())
    if scenario in REVIEW_ROUNDS:
        review_result(fx, row, events, result)
    inspection(fx, row, events, result)
    ordinary_extras(fx, row, events, result)
    return result


def inspection(fx, row, events, result):
    from arete import tracker
    run_dir = fx.paths.RUNS_DIR / row["id"]
    prompts = {}
    for file in fx.record.record_files(row["id"]):
        if file.name.startswith(("verify", "review")):
            prompts[file.name] = str(json.loads(next(fx.record.raw_lines(file))).get("prompt"))
    prepared = [e["data"]["ticket"] for e in events if e.get("event") == "step_end" and e.get("step") == "prepare"
                and e.get("data")]
    result |= {"run_dir": str(run_dir), "prompts": prompts, "prepared_labels": [t.get("labels") for t in prepared],
               "seam": [e["step"] for e in events if e.get("event") == "step_start" and "call" not in e],
               "pr_body": (run_dir / "pr_body.md").read_text() if (run_dir / "pr_body.md").exists() else "",
               "verify_text": [e["data"]["verified"]["summary"] for e in events
                               if e.get("event") == "step_end" and e.get("step") == "verify" and e.get("data")]}
    if fx.scenario == "no-verify-config":
        old = {k: getattr(tracker.Ticket("t", "t", "T"), k) for k in fx.flows.TICKET_FIELDS if k != "labels"}
        result["old_payload_labels"] = tracker.Ticket(**old).labels


def review_result(fx, row, events, result):
    paths, record = fx.paths, fx.record
    report = [e for e in events if e.get("event") == "report"][-1]
    result["review"] = report["review"]
    result["prompt"] = result["fix_prompt"] = ""
    for file in record.record_files(row["id"]):
        if "review2" in file.name:
            result["prompt"] = str(json.loads(next(record.raw_lines(file))).get("prompt"))
        if "review_fix" in file.name:
            result["fix_prompt"] = str(json.loads(next(record.raw_lines(file))).get("prompt"))
    earlier = result["prompt"].split("## Earlier review", 1)[1] if "## Earlier review" in result["prompt"] else ""
    result["earlier"] = earlier
    result["fixed"] = earlier.split("What changed since that review:")[-1].split("Check these first:")[0]
    if fx.scenario == "review-notes-dry-run":
        result["body"] = (paths.RUNS_DIR / row["id"] / "pr_body.md").read_text()


def database_fingerprint(paths):
    stat = paths.DB_FILE.stat()
    return [hashlib.sha256(paths.DB_FILE.read_bytes()).hexdigest(), stat.st_mtime_ns]


def doctor_clean(fx, result):
    from arete import backup, cli
    fx.alerts.notify = lambda title, message: None
    if backup.snapshot("hand") is None:
        raise AssertionError("the replay home took no backup")
    before = database_fingerprint(fx.paths)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(["doctor"])
    result |= {"doctor_code": code, "doctor_output": out.getvalue(),
               "doctor_unchanged": database_fingerprint(fx.paths) == before}


def ordinary_extras(fx, row, events, result):
    scenario, paths, pipeline, runlimits, record, state = fx.scenario, fx.paths, fx.pipeline, fx.runlimits, fx.record, fx.state
    if scenario in ("limit-wait", "work-over-budget"):
        wall = (runlimits.parsed(row["finished"]) - runlimits.parsed(row["started"])).total_seconds() / 60
        result |= {"budget": runlimits.metadata(row["id"], "budget.json"),
                   "parked": runlimits.metadata(row["id"], "parked.json"), "wall": wall}
    if scenario == "code-identity":
        codebase = fx.codebase
        second = (f"import sys; sys.path.insert(0, {str(Path(codebase.__file__).parent.parent)!r}); "
                  "from arete import codebase, run_lines; codebase.STAMP = 'second-engine'; "
                  "run_lines.write(sys.argv[1], 'first'); run_lines.write(sys.argv[1], 'second')")
        subprocess.run([sys.executable, "-I", "-c", second, row["id"]], env=os.environ, check=True, capture_output=True)
        result["identity"] = {
            "stamp": codebase.STAMP, "row": state.run_get(row["id"])["engine_sha"],
            "first": events[0].get("engine"),
            "headers": [json.loads(next(record.raw_lines(f))).get("engine") for f in record.record_files(row["id"])],
            "later": [e.get("engine") for e in events[1:]],
            "second": [json.loads(line) for line in (paths.RUNS_DIR / row["id"] / "run.jsonl").read_text().splitlines()][-2:]}
    if scenario == "future-step-appended":
        result["appended"] = bool(fx.appended)
    if scenario in ("pass", "doctor-clean"):
        assert state.run_get(row["id"])["schema_version"] == state.LATEST
        branches = [b for b in result["origin"].splitlines() if b != "refs/heads/main"]
        assert len(branches) == 1, result
        result["published"] = pipeline.git(["show", branches[0] + ":notes.txt"], fx.root / "sample/origin.git")
    if scenario == "doctor-clean":
        doctor_clean(fx, result)
    if scenario == "foreign-version-record":
        result["foreign_unchanged"] = None if fx.foreign is None else fx.foreign.read_bytes() == fx.foreign_bytes


SCENARIOS = {
    "failed-resume-moved": continuation,
    "failed-resume-reopened-moved": continuation,
    "failed-resume-only-prepare-moved": continuation,
    "failed-resume-stepped": continuation,
    "failed-resume-stepped-pushed": continuation,
    "failed-resume-stepped-late": continuation,
    "failed-resume-stepped-late-broken-checks": continuation,
    "failed-resume-stepped-late-broken-verify": continuation,
    "failed-resume-stepped-hand": continuation,
    "failed-resume-stepped-forged": continuation,
    "failed-resume-killed": continuation,
    "failed-again-prepare": continuation,
    "failed-again-prepare-moved": continuation,
    "failed-again-prepare-limit-wait": continuation,
    "failed-again-prepare-sleep": continuation,
    "failed-again-prepare-unrecorded": continuation,
    "kept-park": kept,
    "kept-park-pushed": kept,
    "kept-push-then-fail": kept,
    "kept-removed": kept,
    "park-no-edits": kept,
    "held-scratch": held_scratch,
    "held-scratch-edited": held_scratch,
    "fix-ask-moved": fix_ask,
    "fix-ask-still": fix_ask,
    "fix-ask-conflict": fix_ask,
    "asked": asked,
    "asked-secret": asked,
    "asked-declined": asked,
    "asked-stopped-waiting": asked,
    "asked-killed-waiting": asked,
    "asked-moved": asked,
    "asked-edited": asked,
    "asked-twice": asked,
    "asked-gone": asked,
    "learn-overlap": learn,
    "learn-overlap-no-second-answer": learn,
    "learn-unrelated-file": learn,
    "pass": ordinary,
    "doctor-clean": ordinary,
    "foreign-version-record": ordinary,
    "secret-in-patch": ordinary,
    "limit-wait": ordinary,
    "work-over-budget": ordinary,
    "failed-check": ordinary,
    "missing-verification": ordinary,
    "verify-environment-fault": ordinary,
    "verify-environment-fault-pass": ordinary,
    "verify-start-crash": ordinary,
    "verify-start-denied": ordinary,
    "verify-change-fault": ordinary,
    "verify-change-fault-timed": ordinary,
    "verify-fix-breaks-check": ordinary,
    "review-rejected": ordinary,
    "edited-after-held-pr": ordinary,
    "review-notes-only": ordinary,
    "review-notes-dry-run": ordinary,
    "review-converges": ordinary,
    "review-fix-breaks-check": ordinary,
    "review-security-waived": ordinary,
    "review-new-regression": ordinary,
    "review-severity-slip": ordinary,
    "review-problems-missing": ordinary,
    "review-earlier-omitted": ordinary,
    "blocked-check-environment": ordinary,
    "blocked-check-guard": ordinary,
    "code-identity": ordinary,
    "future-step-baseline": ordinary,
    "future-step-appended": ordinary,
    "refactor-pass": ordinary,
    "refactor-review-fixes": ordinary,
    "refactor-dry-run": ordinary,
    "review-multiple-fixes": ordinary,
    "review-final-verify-fails": ordinary,
    "review-final-verify-recovers": ordinary,
    "review-no-fix-feedback": ordinary,
    "no-verify-config": ordinary,
}
