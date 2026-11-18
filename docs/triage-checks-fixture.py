import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import urllib.request
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

CHECKS = ["Read notes.txt and see kumquat on its own line.",
          "Read notes.txt and see the original introductory sentence."]
OVERRIDDEN = "Read notes.txt and see kumquat on the first line."
DISPROVED = "Open the notes web page and see a download button."
NEARBY = "Read notes.txt and see no quince."
PLANNER = "1. " + CHECKS[0] + "\n2. " + NEARBY
ANSWER = "Keep the introductory sentence first; kumquat goes on the last line."
DISPROOF = "There is no notes web page or download button: this repository contains only notes.txt."
SCENARIOS = {"approved": ("person", "automatic", "empty-plan"), "patch": ("patch",),
             "overrides": ("overrides",), "legacy": ("unapproved", "empty-checks", "legacy")}


def forbidden(*args, **kwargs):
    raise AssertionError("offline fixture attempted external transport or credential lookup")


def exercise(root, flow, scenario):
    from arete import accounts, factory, gh, paths, pipeline, prompt_loader, record, replay, tracker
    from arete.store import db, principals, runs, tasks, triage

    urllib.request.urlopen = forbidden
    accounts.read = forbidden
    gh.gh = forbidden
    db.migrate()
    owner = principals.seed_owner("fixture@example.invalid", "Fixture owner")
    lines = replay.sample(root / "sample", adw=flow == "adw")
    paths.CONFIG_FILE.write_text("\n".join(lines) + '\ngithub = "offline/triage-fixture"\n\n[jev]\nenabled = false\n')
    if scenario == "patch":
        with paths.CONFIG_FILE.open("a") as out:
            out.write('\n[triage]\nroute_fixes = true\n')
    tid = tasks.task_add("replay-sample", "Add kumquat to notes.txt; preserve its introduction.", "Add kumquat",
                         "fix" if scenario == "patch" else "feat")
    checks = [*CHECKS, OVERRIDDEN, DISPROVED] if scenario == "overrides" else CHECKS
    approval = None
    if scenario != "unapproved":
        approval = {"ref": tracker.ticket_name("replay-sample", tid), "repo": "replay-sample",
                    "route": "build", "title": "Add kumquat", "url": "", "kind": "feat", "size": "small",
                    "plan": [], "assumptions": [] if scenario == "person" else ["Preserve the introduction."],
                    "why": "Offline fixture", "tracker": "local"}
        if scenario != "legacy":
            approval["checks"] = [] if scenario == "empty-checks" else checks
        triage.triage_approve(approval, "triage" if scenario == "automatic" else owner)
    if scenario == "overrides":
        tasks.task_comment(tid, owner, prompt_loader.TRIAGE_ANSWERS + "\n\n" + ANSWER)
    answers = root / "sample/replay"
    plan_file = answers / "plan.json"
    plan = json.loads(plan_file.read_text())
    plan["structured"]["verify"] = "" if scenario == "empty-plan" else PLANNER
    plan["structured"]["assumptions"] = [DISPROOF] if scenario == "overrides" else ["Preserve the introduction."]
    plan["structured"]["plan"] += "\n\n## Assumed\n\n- " + plan["structured"]["assumptions"][0]
    plan_file.write_text(json.dumps(plan, indent=2))
    if scenario == "patch":
        (answers / "build.json").write_text(json.dumps({"text": "Added kumquat.", "structured": {
            "needs_feature": False, "reason": "", "plan": "Append kumquat to notes.txt.",
            "summary": "Added kumquat.", "verify": PLANNER}}))
        plan_file.unlink()
    outcomes = [{"what": check, "how": "Read the disposable notes.txt", "saw": saw, "passed": True}
                for check, saw in [(CHECKS[0], "Outcome A: last line is kumquat."),
                                   (CHECKS[1], "Outcome B: introductory sentence remains first."),
                                   (NEARBY, "Outcome C: quince is absent.")]]
    if scenario == "overrides":
        outcomes += [{"what": OVERRIDDEN, "how": "Read the owner's board answer and notes.txt",
                      "saw": "Superseded by owner: " + ANSWER + " Replacement evidence: first line is the introduction; last is kumquat.",
                      "passed": True},
                     {"what": DISPROVED, "how": "Inspect the disposable repository and try the requested interface",
                      "saw": "Blocked: " + DISPROOF, "passed": False, "cause": "environment"}]
    verification = {"structured": {"verdict": "blocked" if scenario == "overrides" else "pass",
                                    "summary": "Replayed distinct outcomes.", "checks": outcomes}}
    (answers / "verify.json").write_text(json.dumps(verification, indent=2))
    (root / "inputs.json").write_text(json.dumps({"flow": flow, "scenario": scenario, "approval": approval,
                                                  "owner_answer": ANSWER if scenario == "overrides" else ""}, indent=2))
    with mock.patch.object(gh, "open_pr_url", return_value=""), mock.patch.object(gh, "closed_pr_heads", return_value=[]):
        if flow == "adw":
            feature = runpy.run_path(str(paths.ADWS_DIR / "feature.py"))["feature"]
            try:
                factory.main(feature, ["replay-sample", tid, "--force", "--dry-run"])
            except SystemExit as e:
                assert e.code in (None, 0), e.code
        else:
            code = pipeline.run_issue("replay-sample", tid, dry_run=True, force=True)
            assert code == 0, code
    row = runs.ticket_runs("replay-sample", tid, whole=True)[0]
    folder = paths.RUNS_DIR / row["id"]
    notes = (paths.WORKTREES_DIR / f"replay-sample-{tid}" / "notes.txt").read_text()
    assert notes == replay.SAMPLE_TEXT + "kumquat\n", notes
    (root / "notes-after.txt").write_text(notes)
    prompts = {}
    for file in record.record_files(row["id"]):
        header = json.loads(next(record.raw_lines(file)))
        step = file.name.removesuffix(record.RECORD_SUFFIX)
        if step in ("plan", "build", "verify"):
            prompts[step] = header["prompt"]
            (root / (step + "-prompt.md")).write_text(header["prompt"])
    events = (folder / "run.jsonl").read_text()
    if scenario == "patch":
        outcome = next(e["data"] for e in map(json.loads, events.splitlines())
                       if e.get("event") == "step_end" and e.get("step") == "patch")
        (root / "patch-outcome.json").write_text(json.dumps(outcome, indent=2))
    result = {"flow": flow, "scenario": scenario, "run_dir": str(folder), "status": row["status"],
              "prompts": prompts, "verify": (folder / "verify.md").read_text(),
              "pr_body": (folder / "pr_body.md").read_text(),
              "plan": outcome["plan"] if scenario == "patch" else (folder / "plan.md").read_text(),
              "events": events}
    validate(result)
    (root / "evidence.json").write_text(json.dumps(result, indent=2))
    return result


def validate(result):
    scenario, prompts = result["scenario"], result["prompts"]
    if scenario == "patch":
        validate_patch(result)
        return
    assert set(prompts) == {"plan", "build", "verify"}, prompts.keys()
    goal_start = "The planner gave no steps: check what the issue asks for." if scenario == "empty-plan" else PLANNER
    for step in ("build", "verify"):
        assert goal_start in prompts[step], (step, prompts[step])
    active = scenario not in ("unapproved", "empty-checks", "legacy")
    for step in ("plan", "build", "verify"):
        assert ("## Triage checks" in prompts[step]) == active, (scenario, step)
    if active:
        checks = [*CHECKS, OVERRIDDEN, DISPROVED] if scenario == "overrides" else CHECKS
        for step in ("plan", "build", "verify"):
            assert "drop or weaken" in prompts[step]
            assert prompts[step].count("## Triage checks\n\n") == 1
            section = prompts[step].split("## Triage checks\n\n", 1)[1]
            positions = [section.index(f"{n}. {check}") for n, check in enumerate(checks, 1)]
            assert positions == sorted(positions)
            if step != "plan":
                assert prompts[step].index(goal_start) < prompts[step].index("## Triage checks\n\n")
                assert "explicit outcome" in prompts[step]
        pr_checks = result["pr_body"].split("### Triage checks\n\n", 1)[1]
        positions = [pr_checks.index(f"{n}. {check}") for n, check in enumerate(checks, 1)]
        assert positions == sorted(positions)
        for n, check in enumerate(checks, 1):
            assert f"{n}. {check}" in result["pr_body"]
            assert check in result["verify"]
    else:
        assert "Triage checks" not in result["pr_body"]
    for outcome in ("Outcome A", "Outcome B", "Outcome C"):
        assert outcome in result["verify"]
    assert NEARBY in result["pr_body"]
    if scenario == "automatic":
        assert "Approved by triage, not by a person" in prompts["plan"]
        assert "approved this ticket on its own" in result["pr_body"]
    if scenario == "person":
        assert "Sent from the triage board" in result["pr_body"]
        assert "Triage assumed:" not in result["pr_body"]
    if scenario != "unapproved":
        assert "Preserve the introduction." in result["pr_body"]
    if scenario == "overrides":
        for step in ("plan", "verify"):
            assert ANSWER in prompts[step]
            assert "overrides contradictory" in prompts[step]
        assert DISPROOF in result["plan"]
        assert "Superseded by owner" in result["verify"]
        assert "Replacement evidence" in result["verify"]
        assert "Blocked:" in result["verify"]
    if result["flow"] == "adw":
        events = [json.loads(line) for line in result["events"].splitlines()]
        plan = next(e["data"] for e in events if e.get("event") == "step_end" and e.get("step") == "plan")
        assert goal_start in plan["goal"]
        assert ("## Triage checks" in plan["goal"]) == active


def validate_patch(result):
    prompts = result["prompts"]
    assert set(prompts) == {"build", "verify"}, prompts.keys()
    events = list(map(json.loads, result["events"].splitlines()))
    assert not any(e.get("step") == "plan" for e in events)
    patch = next(e for e in events if e.get("event") == "step_end" and e.get("step") == "patch")
    assert patch["result"] == "pass" and not patch["data"]["escalate"], patch
    assert PLANNER in patch["data"]["goal"]
    assert prompts["verify"].index(PLANNER) < prompts["verify"].index("## Triage checks")
    for step in ("build", "verify"):
        assert "drop or weaken" in prompts[step]
        assert prompts[step].count("## Triage checks\n\n") == 1
        section = prompts[step].split("## Triage checks\n\n", 1)[1]
        for n, check in enumerate(CHECKS, 1):
            assert f"{n}. {check}" in section
            assert f"{n}. {check}" in patch["data"]["goal"]
            assert check in result["verify"]
            assert f"{n}. {check}" in result["pr_body"]
    for outcome in ("Outcome A", "Outcome B", "Outcome C"):
        assert outcome in result["verify"]
    assert NEARBY in result["pr_body"]


def child(root, flow, scenario):
    root.mkdir()
    for name in ("config", "state", "logs", "worktrees", "tmp", "home"):
        (root / name).mkdir()
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL")}
    env.update(ARETE_CONFIG_DIR=str(root / "config"), ARETE_STATE_DIR=str(root / "state"),
               ARETE_LOG_DIR=str(root / "logs"), ARETE_WORKTREES_DIR=str(root / "worktrees"),
               TMPDIR=str(root / "tmp"), HOME=str(root / "home"), GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath",
               GIT_CONFIG_VALUE_0=os.devnull)
    done = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "child", str(root), flow, scenario],
                          cwd=root, env=env, capture_output=True, text=True, timeout=120)
    (root / "stdout.txt").write_text(done.stdout)
    (root / "stderr.txt").write_text(done.stderr)
    assert done.returncode == 0, done.stdout + done.stderr
    return str(root / "evidence.json")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        exercise(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=[*SCENARIOS, "inspect"])
    action = parser.parse_args().action
    sandbox = SOURCE / ".arete-sandbox"
    assert not sandbox.is_symlink() and (sandbox / ".arete-owned").read_text().strip() == "arete-verification-scratch-v1"
    base = sandbox / "triage-checks-fixture"
    assert not base.is_symlink()
    base.mkdir(exist_ok=True)
    if action == "inspect":
        retained = [str(p) for p in sorted(base.glob("*/*/evidence.json"))]
        assert retained, "run an exercise first"
        for file in retained:
            validate(json.loads(Path(file).read_text()))
        print(json.dumps({"retained": retained}, indent=2))
        return
    root = Path(tempfile.mkdtemp(prefix=action + "-", dir=base))
    evidence = [child(root / f"{flow}-{scenario}", flow, scenario)
                for flow in (("adw",) if action == "patch" else ("classic", "adw")) for scenario in SCENARIOS[action]]
    print(json.dumps({"artifacts": evidence, "limitation": "Replay proves prompt propagation and report persistence, "
                      "not that a live model obeyed the instructions."}, indent=2))


if __name__ == "__main__":
    main()
