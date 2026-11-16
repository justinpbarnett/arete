import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from tests.scripts.run_budget_usage import BAD_BUDGETS


WORKFLOWS = {
    "sleep": ["failed-again-prepare-sleep"],
    "forced-columns": ["failed-again-columns-high", "failed-again-columns-low"],
    "row-precedence": ["failed-again-row-precedence"],
    "killed-verify": ["failed-resume-killed"],
    "waits": ["limit-wait", "work-over-budget", "failed-again-prepare-limit-wait", "budget-waits-admission",
              "checkpoint-errors"],
}
ACTIONS = (*WORKFLOWS, "migrations", "migration-errors", "checkpoint-errors", "inspect")


def validate(scenario, result):
    if scenario == "failed-again-prepare-sleep":
        import re

        assert result["failed_status"] == "failed" and "checks still failing" in result["failed"]
        assert result["failed_span"] > 600
        assert result["failed_row"]["active_seconds"] < 60
        assert result["failed_row"]["verify_open_seconds"] == 0
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", result["failed_row"]["measured_at"])
    if scenario in ("failed-again-prepare-sleep", "failed-again-prepare-limit-wait", "failed-resume-killed"):
        assert result["status"] == "done", result["summary"]
        assert result["budget"]["parents"] == [result["failed_id"]]
        assert "kumquat" in result["published"]
    if scenario == "failed-resume-killed":
        import signal

        row = result["failed_row"]
        assert result["killed_signal"] == signal.SIGKILL and result["settlement_unchanged"]
        assert row["verify_open_seconds"] > 0.5
        assert row["verify_seconds"] + row["verify_open_seconds"] < 31
        assert row["active_seconds"] < 60
        charge = result["charged"][result["failed_id"]]
        assert charge["run_minutes"] < 1 and charge["verification_minutes"] < 1
    if scenario == "limit-wait":
        assert result["status"] == "done" and result["parked"] == {}
        assert abs(result["budget"]["wait_minutes_left_out"]["limit"] - 10) < .1
    if scenario == "work-over-budget":
        assert "parked: the run chain reached its cumulative 1 minute budget" in result["summary"]


def child(root, scenario, workflow):
    root.mkdir()
    for name in ("config", "state", "logs", "worktrees", "tmp", "home", "authority-parent"):
        (root / name).mkdir()
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR")}
    env.update(ARETE_CONFIG_DIR=str(root / "config"), ARETE_STATE_DIR=str(root / "state"),
               ARETE_LOG_DIR=str(root / "logs"), ARETE_WORKTREES_DIR=str(root / "worktrees"),
               TMPDIR=str(root / "tmp"), HOME=str(root / "home"), GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath",
               GIT_CONFIG_VALUE_0=os.devnull)
    module = "tests.offline_workflow" if workflow else "tests.scripts.run_budget_usage"
    code = f"import sys;sys.path.insert(0,{str(SOURCE)!r});from {module} import main;main(sys.argv[1],sys.argv[2])"
    result = subprocess.run([sys.executable, "-I", "-c", code, str(root), scenario], cwd=root,
                            env=env, capture_output=True, text=True, timeout=180)
    (root / "stdout.txt").write_text(result.stdout)
    (root / "stderr.txt").write_text(result.stderr)
    assert result.returncode == 0, result.stderr + result.stdout
    lines = [line.removeprefix("BEHAVIOR_RESULT=") for line in result.stdout.splitlines()
             if line.startswith("BEHAVIOR_RESULT=")]
    assert len(lines) == 1, result.stdout
    evidence = json.loads(lines[0])
    validate(scenario, evidence)
    (root / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def main():
    assert len(sys.argv) == 2 and sys.argv[1] in ACTIONS, f"choose one of {ACTIONS}"
    action = sys.argv[1]
    sandbox = SOURCE / ".arete-sandbox"
    assert not sandbox.is_symlink() and (sandbox / ".arete-owned").read_text().strip() == "arete-verification-scratch-v1"
    base = sandbox / "run-budget-fixture"
    assert not base.is_symlink()
    base.mkdir(exist_ok=True)
    if action == "inspect":
        results = {str(p.relative_to(base)): json.loads(p.read_text()) for p in sorted(base.glob("*/evidence.json"))}
        assert results, "run an exercise first"
        print("BEHAVIOR_RESULT=" + json.dumps({"retained": results}))
        return
    root = Path(tempfile.mkdtemp(prefix=action + "-", dir=base))
    scenarios = WORKFLOWS.get(action, ["migration-error-" + case for case in BAD_BUDGETS]
                             if action == "migration-errors" else [action])
    evidence = {scenario: child(root / scenario, scenario, action in WORKFLOWS and scenario != "checkpoint-errors")
                for scenario in scenarios}
    result = {"action": action, "evidence_dir": str(root), "scenarios": evidence}
    (root / "evidence.json").write_text(json.dumps(result, indent=2) + "\n")
    print("BEHAVIOR_RESULT=" + json.dumps(result))


if __name__ == "__main__":
    main()
