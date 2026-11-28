"""Tests for built-in versioned ADWs, ADW routing, pinning, migration, and validation (#299, #878)."""
from __future__ import annotations

import concurrent.futures
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import adw, api_check, factory, flows
from tests import adw_parity_scenarios, frozen_adws

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_isolated_child(script: str, *args: str, timeout: int = 60) -> dict:
    with tempfile.TemporaryDirectory(prefix="arete-adw-test-") as td:
        root = Path(td)
        env = {
            **api_check.isolated_env(root),
            "ARETE_TEST_FAST_SYNC": "1",
            "PYTHONPATH": str(REPO_ROOT),
        }
        proc = subprocess.run(
            [sys.executable, "-I", "-B", "-c", "from tests import support\n" + script, str(root), *args],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("BEHAVIOR_RESULT=")), "")
        if proc.returncode != 0 or not line:
            raise AssertionError(
                f"isolated child failed (rc={proc.returncode})\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        return json.loads(line.removeprefix("BEHAVIOR_RESULT="))


class AdwFlowParityTests(unittest.TestCase):
    """Exact call-for-call parity between frozen Python ADWs and interpreted TOML ADWs on fakes (#299)."""

    def _run_both(
        self,
        name: str,
        outcomes_by_step: dict[str, list[factory.Outcome]],
        *,
        check_tries: int = 3,
        review_tries: int = 2,
        documents_on: bool = False,
        dry_run: bool = False,
        parts: list | None = None,
    ) -> tuple[list[tuple], tuple]:
        frozen_fn = getattr(frozen_adws, name)
        loaded = adw.load(None, name)
        interpreted_fn = adw.drive(loaded)
        results = []
        for fn in (frozen_fn, interpreted_fn):
            queues = {k: list(v) for k, v in outcomes_by_step.items()}
            calls: list[tuple] = []

            def rec_step(flow, step_name, _queues=queues, _calls=calls, **kw):
                _calls.append(("step", step_name, kw))
                seq = _queues.get(step_name)
                if seq:
                    return seq.pop(0)
                return factory.Outcome("pass", data={"ending": "done", "step": step_name})

            def rec_finish(flow, ending, _calls=calls, **kw):
                _calls.append(("finish", ending, kw))
                return (ending, kw.get("why", ""), "")

            def rec_ended(flow, outcome, _calls=calls):
                _calls.append(("ended", outcome.result, outcome.feedback, outcome.data))
                return (outcome.result, outcome.feedback, "")

            def rec_dry_run(flow, _calls=calls):
                _calls.append(("end_dry_run",))
                return ("dry-run", "dry-run", "")

            def rec_documents(job, _calls=calls):
                _calls.append(("documents",))
                return documents_on

            def settings_of(role):
                if role == flows.CHECK_REPAIR:
                    return SimpleNamespace(tries=check_tries)
                if role == "review":
                    return SimpleNamespace(tries=review_tries)
                return SimpleNamespace(tries=1)

            fake_ctx = SimpleNamespace(
                ws=Path("/fake/ws"),
                run=SimpleNamespace(
                    settings=settings_of,
                    dry_run=dry_run,
                    repo=SimpleNamespace(parts=list(parts or [])),
                ),
            )
            fake_flow = SimpleNamespace(ctx=fake_ctx)
            with (
                mock.patch.object(flows, "step", side_effect=rec_step),
                mock.patch.object(flows, "finish", side_effect=rec_finish),
                mock.patch.object(flows, "ended", side_effect=rec_ended),
                mock.patch.object(flows, "end_dry_run", side_effect=rec_dry_run),
                mock.patch.object(flows, "documents", side_effect=rec_documents),
            ):
                ret = fn(fake_flow)
            results.append((calls, ret))
        self.assertEqual(results[0], results[1], f"parity mismatch for {name}")
        return results[1]

    def test_parity_across_all_scripts_for_feature_revise_repair(self):
        p = factory.Outcome("pass")

        def f(fb: str = "err") -> factory.Outcome:
            return factory.Outcome("fail", feedback=fb, data={"ending": "failed", "why": fb})

        scripts: list[tuple[str, dict[str, list[factory.Outcome]], dict]] = [
            ("prepare-fails", {"prepare": [f("prep failed")]}, {}),
            ("first-step-fails", {"plan": [f("plan failed")], "revise": [f("rev failed")], "repair": [f("rep failed")]}, {}),
            ("first-build-fails", {"implement": [f("build failed")]}, {}),
            ("checks-pass-at-once", {}, {}),
            ("checks-fail-twice-then-pass", {"checks": [f("e1"), f("e2"), p]}, {"check_tries": 3}),
            ("checks-never-pass", {"checks": [f("e1"), f("e2"), f("e3")]}, {"check_tries": 3}),
            ("check-repair-tries-0", {"checks": [f("e1")]}, {"check_tries": 0}),
            ("review-passes", {}, {}),
            ("review-fails-with-feedback-then-passes", {"review": [f("fix bug"), p]}, {"review_tries": 2}),
            ("review-fails-no-feedback", {"review": [f(""), p]}, {"review_tries": 2}),
            ("review-tries-0", {"review": [f("still bad")]}, {"review_tries": 0}),
            ("review-fix-breaks-check-nested-passes",
             {"review": [f("fix bug"), p], "checks": [p, f("broken"), p]},
             {"check_tries": 3, "review_tries": 2}),
            ("review-fix-nested-repair-never-passes",
             {"review": [f("fix bug")], "checks": [p, f("c1"), f("c2"), f("c3")]},
             {"check_tries": 3, "review_tries": 2}),
            ("documents-on", {}, {"documents_on": True}),
            ("documents-off", {}, {"documents_on": False}),
            ("dry-run", {}, {"dry_run": True}),
            ("parts-on", {}, {"parts": [SimpleNamespace(dir="", github="me/repo")]}),
            ("parts-off", {}, {"parts": []}),
            ("pr-fails", {"pr": [f("pr failed")]}, {"parts": [SimpleNamespace(dir="", github="me/repo")]}),
            ("ci-fails", {"ci": [f("ci failed")]}, {"parts": [SimpleNamespace(dir="", github="me/repo")]}),
            ("publish-fails", {"publish": [f("pub failed")]}, {"parts": [SimpleNamespace(dir="", github="me/repo")]}),
        ]

        for adw_name in ("feature", "revise", "repair"):
            for label, outcomes, opts in scripts:
                if label == "first-build-fails" and adw_name != "feature":
                    continue
                with self.subTest(adw=adw_name, script=label):
                    calls, ret = self._run_both(adw_name, outcomes, **opts)
                    if label == "checks-never-pass":
                        self.assertIn(("step", "learn", {"tree": "/fake/ws", "apply": False}), calls)
                        self.assertEqual(calls[-1], (
                            "finish", "failed", {"why": "checks still failing after 3 attempts", "detail": "e3"},
                        ))
                    elif label == "review-fix-breaks-check-nested-passes":
                        self.assertTrue(any(
                            c[0] == "step" and c[1] == "implement" and c[2].get("round") == "check_repair1_2"
                            for c in calls
                        ))
                    elif label == "review-fix-nested-repair-never-passes":
                        self.assertEqual(calls[-1], (
                            "finish", "failed", {"why": "the checks failed after the review fixes", "detail": "c3"},
                        ))
                    elif label == "dry-run":
                        self.assertEqual(calls[-1], ("end_dry_run",))
                        self.assertEqual(ret[0], "dry-run")


_REPLAY_CHILD_SCRIPT = r"""
import json, sys
from pathlib import Path
from arete import factory, paths, replay
from arete.store import db, runs as run_store, tasks as task_store
from tests.adw_parity_scenarios import normalize_run, setup_scenario

root = Path(sys.argv[1])
scenario = sys.argv[2]
repo, layer, answers, flags = setup_scenario(root, scenario)
paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
paths.CONFIG_FILE.write_text(
    'max_parallel = 1\n'
    '[replay]\nall = true\n'
    f'dir = {json.dumps(str(answers))}\n'
    '[steps.check_repair]\ntries = 3\n'
    '[steps.review]\ntries = 2\n'
    '[[repo]]\nname = "replay-sample"\n'
    f'path = {json.dumps(str(repo))}\n'
    f'layer = {json.dumps(str(layer))}\n'
    'tracker = "local"\njev = false\n'
)
db.migrate()
tid = task_store.task_add("replay-sample", f"Add {replay.SAMPLE_WORD}", f"Scenario {scenario}", "feat")
try:
    factory.main(["replay-sample", tid, "--force", *flags])
except SystemExit as e:
    code = e.code
rows = run_store.ticket_runs("replay-sample", tid, limit=1)
assert rows, f"no run created for {scenario}"
data = normalize_run(rows[0]["id"], tid=tid)
data["exit_code"] = code
print("BEHAVIOR_RESULT=" + json.dumps(data))
"""


def _assert_replay_scenarios(case: unittest.TestCase, scenarios: tuple[str, ...]) -> None:
    def run_one(scenario: str) -> tuple[str, dict]:
        return scenario, _run_isolated_child(_REPLAY_CHILD_SCRIPT, scenario)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(scenarios)) as pool:
        results = dict(pool.map(run_one, scenarios))

    for scenario in scenarios:
        with case.subTest(scenario=scenario):
            golden_file = adw_parity_scenarios.GOLDEN_DIR / f"{scenario}.json"
            expected = json.loads(golden_file.read_text())
            case.assertEqual(results[scenario], expected)


class AdwReplayParityCoreTests(unittest.TestCase):
    """End-to-end replay parity against golden files (core scenarios, #299)."""

    def test_core_replay_scenarios_match_golden_files(self):
        _assert_replay_scenarios(self, adw_parity_scenarios.SCENARIOS[:4])


class AdwReplayParityReviewTests(unittest.TestCase):
    """End-to-end replay parity against golden files (review/verify scenarios, #299)."""

    def test_review_replay_scenarios_match_golden_files(self):
        _assert_replay_scenarios(self, adw_parity_scenarios.SCENARIOS[4:])


class AdwMigrationBackfillTests(unittest.TestCase):
    """Verify m0049_runs_adw backfills runs.adw along continuation chains and keeps classic NULL (#299)."""

    def test_backfill_sets_adw_along_chain_and_leaves_classic_null(self):
        script = r"""
import json, sys
from pathlib import Path
from arete import paths, run_lines
from arete.store import db, migrations, runs as run_store

root = Path(sys.argv[1])
paths.ensure_dirs()
# Migrate up to m0048 (right before m0049_runs_adw)
db.migrate(through=48)
with db.tx() as c:
    c.execute("INSERT INTO runs (id, kind, repo, issue, status, started, continues, chain, seq) VALUES "
              "('r-revise', 'pipeline', 'sample', '1', 'done', '2026-01-01T00:00:01Z', NULL, 'r-revise', 1)")
    c.execute("INSERT INTO runs (id, kind, repo, issue, status, started, continues, chain, seq) VALUES "
              "('r-feat-root', 'pipeline', 'sample', '2', 'interrupted', '2026-01-01T00:00:02Z', NULL, 'r-feat-root', 2)")
    c.execute("INSERT INTO runs (id, kind, repo, issue, status, started, continues, chain, seq) VALUES "
              "('r-feat-cont', 'pipeline', 'sample', '2', 'done', '2026-01-01T00:00:03Z', 'r-feat-root', 'r-feat-root', 3)")
    c.execute("INSERT INTO runs (id, kind, repo, issue, status, started, continues, chain, seq) VALUES "
              "('r-classic', 'pipeline', 'sample', '3', 'done', '2026-01-01T00:00:04Z', NULL, 'r-classic', 4)")
    c.execute("INSERT INTO runs (id, kind, repo, issue, status, started, continues, chain, seq) VALUES "
              "('r-loop', 'loop', '', '', 'done', '2026-01-01T00:00:05Z', NULL, 'r-loop', 5)")

run_lines.write('r-revise', 'task', ticket='#1', kind='feat', repo='sample', title='T1', path='revise')
run_lines.write('r-feat-root', 'task', ticket='#2', kind='feat', repo='sample', title='T2', path='feature')
run_lines.write('r-feat-cont', 'task', ticket='#2', kind='feat', repo='sample', title='T2')
run_lines.write('r-classic', 'task', ticket='#3', kind='feat', repo='sample', title='T3')

db.migrate()
rows = {rid: run_store.run_get(rid) for rid in ('r-revise', 'r-feat-root', 'r-feat-cont', 'r-classic', 'r-loop')}
out = {rid: {k: row[k] for k in ('adw', 'adw_version', 'adw_rule')} for rid, row in rows.items()}
print("BEHAVIOR_RESULT=" + json.dumps(out))
"""
        result = _run_isolated_child(script)
        self.assertEqual(result["r-revise"], {"adw": "revise", "adw_version": None, "adw_rule": None})
        self.assertEqual(result["r-feat-root"], {"adw": "feature", "adw_version": None, "adw_rule": None})
        self.assertEqual(result["r-feat-cont"], {"adw": "feature", "adw_version": None, "adw_rule": None})
        self.assertEqual(result["r-classic"], {"adw": None, "adw_version": None, "adw_rule": None})
        self.assertEqual(result["r-loop"], {"adw": None, "adw_version": None, "adw_rule": None})


class AdwRoutingAndRecordingTests(unittest.TestCase):
    """Test ADW routing under lock, reserved rule ids, API fields, pinning, and custom config routes (#299, #878)."""

    class_timeout = 30

    def test_routing_rules_reserved_ids_lock_api_and_config_override(self):
        script = r"""
import hashlib, json, sys
from pathlib import Path
from unittest import mock

from arete import adw, api, board, config, configrepo, control, factory, paths, pipeline, record, replay, run_lines, tasks, util
from arete.store import asks as ask_store, db, insights as insights_store, runs as run_store, tasks as task_store

root = Path(sys.argv[1])
sample_dir = root / "sample"
replay.sample(sample_dir, adw=True)
repo_dir = sample_dir / "repo"
import shutil
layer_dir = paths.CONFIG_DIR / "layers" / "replay-sample"
shutil.copytree(sample_dir / "layer", layer_dir)
answers_dir = sample_dir / "replay"

paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
paths.CONFIG_FILE.write_text(
    'max_parallel = 1\n'
    '[replay]\nall = true\n'
    f'dir = {json.dumps(str(answers_dir))}\n'
    '[steps.check_repair]\ntries = 3\n'
    '[steps.review]\ntries = 2\n'
    '[factory]\nadws = "/legacy/path"\n'
    '[[repo]]\nname = "replay-sample"\n'
    f'path = {json.dumps(str(repo_dir))}\n'
    f'layer = {json.dumps(str(layer_dir))}\n'
    'tracker = "local"\njev = false\n'
)
# Check [factory] adws is ignored in non-strict config.load
cfg_ignored = config.load(strict=False).ignored
assert "[factory] adws" in cfg_ignored, cfg_ignored
# Rewrite without legacy key so configrepo strict commit succeeds
paths.CONFIG_FILE.write_text(
    'max_parallel = 1\n'
    '[replay]\nall = true\n'
    f'dir = {json.dumps(str(answers_dir))}\n'
    '[steps.check_repair]\ntries = 3\n'
    '[steps.review]\ntries = 2\n'
    '[[repo]]\nname = "replay-sample"\n'
    f'path = {json.dumps(str(repo_dir))}\n'
    f'layer = {json.dumps(str(layer_dir))}\n'
    'tracker = "local"\njev = false\n'
)
db.migrate()
configrepo.init()

# 1. First run records adw=feature, adw_rule=default, adw_version hashing engine feature.toml,
#    and adw.route runs WHILE the ticket run lock is held!
tid = task_store.task_add("replay-sample", f"Add {replay.SAMPLE_WORD}", "First task", "feat")
lock_held_during_route = []
real_route = adw.route
def spy_route(cfg, repo, t, **kw):
    from arete.store import workspaces
    lock_name = run_store.run_lock("pipeline", task=task_store.parse_task_num(t))
    lock_held_during_route.append((lock_name in workspaces.TICKET_LOCKS.get(), util.lock_is_held(lock_name)))
    return real_route(cfg, repo, t, **kw)

with mock.patch.object(adw, "route", side_effect=spy_route):
    try:
        factory.main(["replay-sample", tid, "--force"])
    except SystemExit as e:
        assert e.code == 0, e.code

r1 = run_store.newest_whole_run("replay-sample", tid)
expected_feat_ver = "adw/feature@" + hashlib.sha256((paths.ENGINE_ADWS_DIR / "feature.toml").read_bytes()).hexdigest()[:12]
assert r1["adw"] == "feature", r1
assert r1["adw_rule"] == "default", r1
assert r1["adw_version"] == expected_feat_ver, (r1["adw_version"], expected_feat_ver)
assert lock_held_during_route == [(True, True)], lock_held_during_route

# Check resolved.json and API responses (both /api/runs/{id} via board and /api/v1/runs/{id} via api.run_object)
resolved_doc = json.loads((paths.RUNS_DIR / r1["id"] / "resolved.json").read_text())
assert resolved_doc["adw"] == "feature"
assert resolved_doc["adw_version"] == expected_feat_ver
assert resolved_doc["adw_rule"] == "default"

board_run = board.run_board(r1["id"])["run"]
assert (board_run["adw"], board_run["adw_version"], board_run["adw_rule"]) == ("feature", expected_feat_ver, "default")
board_list = board.runs_board({})["runs"]
assert board_list[0]["adw"] == "feature" and board_list[0]["adw_rule"] == "default"
v1_run = api.run_object(r1)
assert (v1_run["adw"], v1_run["adw_version"], v1_run["adw_rule"]) == ("feature", expected_feat_ver, "default")

# 2. Seed PR feedback on an open PR -> next run routes to revise / feedback-revise
pr_url = "https://github.com/owner/sample/pull/1"
assert record.move(r1["id"], "pr", ("done",), pr_url=pr_url)
insights_store.add_feedback([{
    "pr_url": pr_url, "repo": "replay-sample", "issue": tid, "kind": "comment",
    "gh_id": 1, "body": "Please tweak", "pr_state": "OPEN", "status": "new",
    "at_once": 0, "created": "2026-01-01T00:00:00Z",
}])
with mock.patch.object(adw, "drive", return_value=lambda fl: ("done", "revised", "")):
    try:
        factory.main(["replay-sample", tid, "--force"])
    except SystemExit as e:
        assert e.code == 0, e.code
r_rev = run_store.newest_whole_run("replay-sample", tid)
assert (r_rev["adw"], r_rev["adw_rule"]) == ("revise", "feedback-revise"), r_rev

# 3. Interrupted run with a repair source -> repair / interrupted-repair
int_id = support.start_pipeline(repo="replay-sample", issue=tid, adw="repair")
run_lines.write(int_id, "repair", source=r1["id"])
record.settle(int_id, "interrupted")
with mock.patch.object(adw, "drive", return_value=lambda fl: ("done", "repaired", "")):
    try:
        factory.main(["replay-sample", tid, "--force"])
    except SystemExit as e:
        assert e.code == 0, e.code
r_int_rep = run_store.newest_whole_run("replay-sample", tid)
assert (r_int_rep["adw"], r_int_rep["adw_rule"]) == ("repair", "interrupted-repair"), r_int_rep

# 4. Answered ask run -> @continue with the asked run's ADW
tid_ask = task_store.task_add("replay-sample", "Ask test", "Ask body", "feat")
ask_run = support.start_pipeline(repo="replay-sample", issue=tid_ask, adw="revise")
record.settle(ask_run, "needs-info")
ask_store.ask_add("ask-1", ask_run, "replay-sample", tid_ask, "plan", "plan", "why", "",
                  [{"kind": "text", "header": "Q", "question": "Which?"}], {})
ask_store.ask_close("ask-1", "answered", [{"text": "yes"}])
with mock.patch.object(adw, "drive", return_value=lambda fl: ("done", "answered", "")):
    try:
        factory.main(["replay-sample", tid_ask, "--force"])
    except SystemExit as e:
        assert e.code == 0, e.code
r_ans = run_store.newest_whole_run("replay-sample", tid_ask)
assert (r_ans["adw"], r_ans["adw_rule"], r_ans["continues"]) == ("revise", "@continue", ask_run), r_ans

# 5. --repair -> @repair, and tasks.repair builds --repair
tid_rep = task_store.task_add("replay-sample", "Repair test", "Repair body", "feat")
held_run = support.start_pipeline(repo="replay-sample", issue=tid_rep, adw="feature")
record.settle(held_run, "pr", pr_url="https://github.com/owner/sample/pull/2")
with mock.patch.object(adw, "drive", return_value=lambda fl: ("done", "repaired", "")):
    try:
        factory.main(["replay-sample", tid_rep, "--force", "--repair", held_run])
    except SystemExit as e:
        assert e.code == 0, e.code
r_rep = run_store.newest_whole_run("replay-sample", tid_rep)
assert (r_rep["adw"], r_rep["adw_rule"]) == ("repair", "@repair"), r_rep

# 6. tasks.run_step refuses run with NULL adw, and --continue --only gives @continue
classic_id = support.start_pipeline(repo="replay-sample", issue=tid_rep)
record.settle(classic_id, "done")
try:
    tasks.run_step(classic_id, "plan", {})
    raise AssertionError("run_step should have refused NULL adw")
except control.Refused as e:
    assert "no recorded ADW" in str(e), str(e)

run_lines.write(r1["id"], "step_start", step="plan")
captured_step_cmd = []
with (mock.patch.object(tasks, "removed_tree", return_value=""),
      mock.patch.object(tasks, "launch", side_effect=lambda cfg, repo, t, cmd, **kw: captured_step_cmd.extend(cmd) or {"state": "running"})):
    tasks.run_step(r1["id"], "plan", {})
assert "--continue" in captured_step_cmd and r1["id"] in captured_step_cmd and "--only" in captured_step_cmd, captured_step_cmd

def only_plan_drive(fl):
    fl.step("plan")
    return "step", "plan pass", ""

with (mock.patch.dict(factory.STEPS, {"plan": lambda fl: factory.Outcome("pass")}),
      mock.patch.object(adw, "drive", return_value=only_plan_drive)):
    try:
        factory.main(["replay-sample", tid, "--continue", r1["id"], "--only", "plan"])
    except SystemExit as e:
        assert e.code == 0, e.code
r_step = run_store.runs(kind="pipeline", limit=1)[0]
assert (r_step["adw"], r_step["adw_rule"]) == ("feature", "@continue"), r_step

# 7. --adw revise gives @asked
tid_asked = task_store.task_add("replay-sample", "Explicit ADW", "Body", "feat")
with mock.patch.object(adw, "drive", return_value=lambda fl: ("done", "asked", "")):
    try:
        factory.main(["replay-sample", tid_asked, "--force", "--adw", "revise"])
    except SystemExit as e:
        assert e.code == 0, e.code
r_explicit = run_store.newest_whole_run("replay-sample", tid_asked)
assert (r_explicit["adw"], r_explicit["adw_rule"]) == ("revise", "@asked"), r_explicit

# 8. Activated config repo routes/adw.toml + adws/quick.toml
quick_toml = (
    'schema = 1\nname = "quick"\n'
    '[[steps]]\ndo = "prepare"\nend = true\n'
    '[[steps]]\ndo = "implement"\nround = "build"\nend = true\n'
    '[[steps]]\nuntil = "passed"\ntries = "setting:review.tries"\nmin = 1\n'
    '  [steps.first]\n  do = "review"\n  attempt = "{try}"\n  tries = "{tries}"\n'
    '  [[steps.repeat]]\n  do = "implement"\n  round = "review_fix{prev?}"\n  feedback = "@last"\n  end = true\n'
    '[[steps]]\nfinish = "last"\n'
)
routes_toml = (
    'schema = 1\n'
    '[[rule]]\nid = "chore-quick"\nwhen = { kind = "chore" }\nadw = "quick"\nset = { "review.tries" = 1 }\n'
    '[[rule]]\nid = "default"\nadw = "feature"\n'
)
configrepo.commit(
    {"adws/quick.toml": quick_toml, "routes/adw.toml": routes_toml},
    {"adws/quick.toml": configrepo.ABSENT, "routes/adw.toml": configrepo.ABSENT},
    actor="u:owner",
    source="hand",
    note="add quick adw and chore route",
)
expected_quick_ver = "adw/quick@" + hashlib.sha256(quick_toml.encode()).hexdigest()[:12]

tid_chore = task_store.task_add("replay-sample", "Small chore", "Chore body", "chore")
seen_review_tries = []
def spy_quick_drive(loaded):
    def _fn(flow):
        seen_review_tries.append(flow.ctx.run.settings("review").tries)
        return "done", "quick done", ""
    return _fn

with mock.patch.object(adw, "drive", side_effect=spy_quick_drive):
    try:
        factory.main(["replay-sample", tid_chore, "--force"])
    except SystemExit as e:
        assert e.code == 0, e.code
r_chore = run_store.newest_whole_run("replay-sample", tid_chore)
assert r_chore["adw"] == "quick", r_chore
assert r_chore["adw_rule"] == "chore-quick", r_chore
assert r_chore["adw_version"] == expected_quick_ver, (r_chore["adw_version"], expected_quick_ver)
assert seen_review_tries == [1], seen_review_tries

tid_feat = task_store.task_add("replay-sample", "Normal feat", "Feat body", "feat")
with mock.patch.object(adw, "drive", return_value=lambda fl: ("done", "feat done", "")):
    try:
        factory.main(["replay-sample", tid_feat, "--force"])
    except SystemExit as e:
        assert e.code == 0, e.code
r_feat = run_store.newest_whole_run("replay-sample", tid_feat)
assert (r_feat["adw"], r_feat["adw_rule"]) == ("feature", "default"), r_feat

# 7. Mid-run config edits do not affect a pinned in-flight run, only subsequent runs
tid_pin1 = task_store.task_add("replay-sample", "Pin test 1", "Chore pin body", "chore")
mid_run_checks = []
quick_v2 = (
    'schema = 1\nname = "quick"\n'
    '[[steps]]\ndo = "prepare"\nend = true\n'
    '[[steps]]\ndo = "plan"\nend = true\n'
    '[[steps]]\nfinish = "last"\n'
)
routes_v2 = (
    'schema = 1\n'
    '[[rule]]\nid = "chore-revise"\nwhen = { kind = "chore" }\nadw = "revise"\n'
    '[[rule]]\nid = "default"\nadw = "feature"\n'
)
def capture_and_mutate_mid_run(loaded_adw):
    def _run(fl):
        run_id = fl.ctx.run.id
        res = json.loads((paths.RUNS_DIR / run_id / "resolved.json").read_text())
        pinned_commit = res["config_commit"]
        # Commit updated quick.toml & routes/adw.toml while this run is in flight
        configrepo.commit(
            {"adws/quick.toml": quick_v2, "routes/adw.toml": routes_v2},
            {"adws/quick.toml": configrepo.read("adws/quick.toml")[1], "routes/adw.toml": configrepo.read("routes/adw.toml")[1]},
            actor="u:owner", source="hand", note="update quick and routes mid-run",
        )
        # Inside the running run, pins.current() still points to the pinned checkout (v1),
        # while reading directly from live paths.CONFIG_DIR sees the newly committed v2.
        pinned_loaded = adw.load(config.load(), "quick")
        live_loaded = adw.load(config.load(), "quick", config_dir=paths.CONFIG_DIR)
        mid_run_checks.append((loaded_adw.version, pinned_loaded.version, live_loaded.version, res["adw_version"]))
        return ("done", "pin1 done", "")
    return _run

with mock.patch.object(adw, "drive", side_effect=capture_and_mutate_mid_run):
    try:
        factory.main(["replay-sample", tid_pin1, "--force"])
    except SystemExit as e:
        assert e.code == 0, e.code
assert len(mid_run_checks) == 1, mid_run_checks
v_in_flight, v_pinned, v_live, v_res = mid_run_checks[0]
assert v_in_flight == v_pinned == v_res == expected_quick_ver, mid_run_checks
assert v_live != expected_quick_ver, mid_run_checks

# Next chore ticket picks up the new routes/adw.toml (chore-revise -> revise)
tid_pin2 = task_store.task_add("replay-sample", "Pin test 2", "Chore pin body 2", "chore")
with mock.patch.object(adw, "drive", return_value=lambda fl: ("done", "pin2 done", "")):
    try:
        factory.main(["replay-sample", tid_pin2, "--force"])
    except SystemExit as e:
        assert e.code == 0, e.code
r_pin2 = run_store.newest_whole_run("replay-sample", tid_pin2)
assert (r_pin2["adw"], r_pin2["adw_rule"]) == ("revise", "chore-revise"), r_pin2

print("BEHAVIOR_RESULT=" + json.dumps({"ok": True}))
"""
        result = _run_isolated_child(script)
        self.assertTrue(result["ok"])


class AdwFirstRolesTests(unittest.TestCase):
    """Test poll.step_without_route follows ADW first_roles (#878)."""

    def test_first_roles_follow_adw_and_config_override(self):
        script = r"""
import json, sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import config, configrepo, paths, poll, record, replay, routing, run_lines, tracker
from arete.store import db, insights as insights_store, tasks as task_store

root = Path(sys.argv[1])
sample_dir = root / "sample"
replay.sample(sample_dir, adw=True)
repo_dir = sample_dir / "repo"
import shutil
layer_dir = paths.CONFIG_DIR / "layers" / "replay-sample"
shutil.copytree(sample_dir / "layer", layer_dir)
(layer_dir / "config.toml").write_text('[factory]\npath = "adw"\n')

paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
paths.CONFIG_FILE.write_text(
    'max_parallel = 1\n'
    '[steps.plan]\nrunner = "claude"\nmodel = "sonnet"\n'
    '[steps.build]\nrunner = "claude"\nmodel = "sonnet"\n'
    '[steps.review]\nrunner = "claude"\nmodel = "sonnet"\n'
    '[steps.review_repair]\nrunner = "claude"\nmodel = "sonnet"\n'
    '[[repo]]\nname = "replay-sample"\n'
    f'path = {json.dumps(str(repo_dir))}\n'
    f'layer = {json.dumps(str(layer_dir))}\n'
    'tracker = "local"\njev = false\n'
)
db.migrate()
configrepo.init()
cfg = config.load()
repo = cfg.repo("replay-sample")
layer = config.load_layer(repo.layer_dir)

def fake_discovery(unroutable_role: str):
    class FakeDiscovery:
        def routes(self, setting):
            if setting.step == unroutable_role:
                return [SimpleNamespace(state="policy", detail=f"{unroutable_role} blocked")]
            return [SimpleNamespace(state="ready", detail="")]
    return FakeDiscovery()

# 1. Feature ticket blocked on plan when plan is unroutable
t_feat = task_store.task_add("replay-sample", "Feat", "Body", "feat")
ticket_feat = tracker.Ticket(t_feat, t_feat, "Feat", kind="feat")
blk_feat = poll.step_without_route(cfg, repo, ticket_feat, fake_discovery("plan"), layer)
assert blk_feat.startswith("plan has no eligible route"), blk_feat

# 2. Revise ticket (with pending feedback) NOT blocked on plan
t_rev = task_store.task_add("replay-sample", "Rev", "Body", "feat")
r_rev = support.start_pipeline(repo="replay-sample", issue=t_rev, adw="feature")
record.settle(r_rev, "pr", pr_url="https://github.com/owner/sample/pull/10")
insights_store.add_feedback([{
    "pr_url": "https://github.com/owner/sample/pull/10", "repo": "replay-sample", "issue": t_rev,
    "kind": "comment", "gh_id": 1, "body": "Fix", "pr_state": "OPEN", "status": "new",
    "at_once": 0, "created": "2026-01-01T00:00:00Z",
}])
ticket_rev = tracker.Ticket(t_rev, t_rev, "Rev", kind="feat")
blk_rev = poll.step_without_route(cfg, repo, ticket_rev, fake_discovery("plan"), layer)
assert blk_rev == "", blk_rev

# 3. Interrupted ticket with a repair source blocked on review_repair
t_rep = task_store.task_add("replay-sample", "Rep", "Body", "feat")
r_src = support.start_pipeline(repo="replay-sample", issue=t_rep, adw="feature")
record.settle(r_src, "pr", pr_url="https://github.com/owner/sample/pull/11")
r_int = support.start_pipeline(repo="replay-sample", issue=t_rep, adw="repair")
run_lines.write(r_int, "repair", source=r_src)
record.settle(r_int, "interrupted")
ticket_rep = tracker.Ticket(t_rep, t_rep, "Rep", kind="feat")
blk_rep = poll.step_without_route(cfg, repo, ticket_rep, fake_discovery("review_repair"), layer)
assert blk_rep.startswith("review_repair has no eligible route"), blk_rep

# 4. Config ADW whose first entry is build implement is NOT blocked on plan
quick_toml = (
    'schema = 1\nname = "quick"\n'
    '[[steps]]\ndo = "implement"\nround = "build"\nend = true\n'
    '[[steps]]\ndo = "review"\nend = true\n'
    '[[steps]]\nfinish = "last"\n'
)
routes_toml = (
    'schema = 1\n'
    '[[rule]]\nid = "chore-quick"\nwhen = { kind = "chore" }\nadw = "quick"\n'
    '[[rule]]\nid = "default"\nadw = "feature"\n'
)
configrepo.commit(
    {"adws/quick.toml": quick_toml, "routes/adw.toml": routes_toml},
    {"adws/quick.toml": configrepo.ABSENT, "routes/adw.toml": configrepo.ABSENT},
    actor="u:owner", source="hand", note="chore quick",
)
t_chore = task_store.task_add("replay-sample", "Chore", "Body", "chore")
ticket_chore = tracker.Ticket(t_chore, t_chore, "Chore", kind="chore")
blk_chore = poll.step_without_route(cfg, repo, ticket_chore, fake_discovery("plan"), layer)
assert blk_chore == "", blk_chore

print("BEHAVIOR_RESULT=" + json.dumps({"ok": True}))
"""
        result = _run_isolated_child(script)
        self.assertTrue(result["ok"])


class AdwValidatorAndGateTests(unittest.TestCase):
    """Test stage 3 validation, stage 6 outline & routes dry-run, config items, and gate B (#878)."""

    def test_adw_and_routes_validation_stage6_and_gate_b(self):
        script = r"""
import json, sys, tomllib
from pathlib import Path

from arete import adw, config, configitems, configkinds, configrepo, paths, record, replay
from arete.store import db, tasks as task_store

root = Path(sys.argv[1])
sample_dir = root / "sample"
replay.sample(sample_dir, adw=True)
repo_dir = sample_dir / "repo"
import shutil
layer_dir = paths.CONFIG_DIR / "layers" / "replay-sample"
shutil.copytree(sample_dir / "layer", layer_dir)
paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
paths.CONFIG_FILE.write_text(
    'max_parallel = 1\n'
    '[[repo]]\nname = "replay-sample"\n'
    f'path = {json.dumps(str(repo_dir))}\n'
    f'layer = {json.dumps(str(layer_dir))}\n'
    'tracker = "local"\njev = false\n'
)
db.migrate()
configrepo.init()
cfg = config.load()

# 1. Invalid ADW drafts refused at stage 3 (schema_and_references)
invalid_adws = {
    "unknown_do": {"schema": 1, "name": "bad", "steps": [{"do": "nope"}, {"finish": "last"}]},
    "unknown_when": {"schema": 1, "name": "bad", "steps": [{"do": "prepare", "when": "sunny"}, {"finish": "last"}]},
    "bad_placeholder": {
        "schema": 1, "name": "bad",
        "steps": [{
            "until": "passed", "tries": "setting:check_repair.tries",
            "first": {"do": "checks"},
            "repeat": [{"do": "implement", "round": "{nope}"}],
        }, {"finish": "last"}],
    },
    "bad_tries": {
        "schema": 1, "name": "bad",
        "steps": [{
            "until": "passed", "tries": "setting:nope.tries",
            "first": {"do": "checks"},
            "repeat": [{"do": "implement", "round": "fix{try}"}],
        }, {"finish": "last"}],
    },
    "code_key": {"schema": 1, "name": "bad", "code": "def f(): pass", "steps": [{"finish": "last"}]},
    "no_finish": {"schema": 1, "name": "bad", "steps": [{"do": "prepare", "end": True}]},
}
for label, payload in invalid_adws.items():
    rep = adw.check_draft("adw", payload, cfg=cfg, actor="run:pip-1")
    assert rep["status"] == "invalid" and rep["stage"] == "schema_and_references", (label, rep)

# 2. Invalid routes drafts refused at stage 3 (schema_and_references)
invalid_routes = {
    "missing_adw": {"schema": 1, "rule": [{"id": "default", "adw": "nope"}]},
    "reserved_id": {"schema": 1, "rule": [{"id": "@x", "adw": "feature"}, {"id": "default", "adw": "feature"}]},
    "duplicate_id": {"schema": 1, "rule": [
        {"id": "dup", "when": {"kind": "chore"}, "adw": "revise"},
        {"id": "dup", "adw": "feature"},
    ]},
    "last_rule_with_when": {"schema": 1, "rule": [
        {"id": "only", "when": {"kind": "chore"}, "adw": "revise"},
    ]},
    "bad_set": {"schema": 1, "rule": [
        {"id": "badset", "when": {"kind": "chore"}, "adw": "feature", "set": {"nope.tries": 1}},
        {"id": "default", "adw": "feature"},
    ]},
}
for label, payload in invalid_routes.items():
    rep = adw.check_draft("routes", payload, cfg=cfg, actor="run:pip-1")
    assert rep["status"] == "invalid" and rep["stage"] == "schema_and_references", (label, rep)

# 3. configrepo.commit refuses invalid ADW and routes files
try:
    configrepo.commit(
        {"adws/bad.toml": 'schema = 1\nname = "bad"\n[[steps]]\ndo = "nope"\n[[steps]]\nfinish = "last"\n'},
        {"adws/bad.toml": configrepo.ABSENT},
        actor="u:owner", source="hand", note="bad adw",
    )
    raise AssertionError("configrepo.commit should reject invalid adw")
except configrepo.ConfigRepoError:
    pass

try:
    configrepo.commit(
        {"routes/adw.toml": 'schema = 1\n[[rule]]\nid = "@bad"\nadw = "feature"\n'},
        {"routes/adw.toml": configrepo.ABSENT},
        actor="u:owner", source="hand", note="bad routes",
    )
    raise AssertionError("configrepo.commit should reject invalid routes")
except configrepo.ConfigRepoError:
    pass

# 4. Valid ADW draft stage 6 lists its outline nodes
feat_data = tomllib.loads((paths.ENGINE_ADWS_DIR / "feature.toml").read_text())
adw_rep = adw.check_draft("adw", feat_data, cfg=cfg, actor="run:pip-1")
assert adw_rep["status"] == "valid", adw_rep
assert adw_rep["requires_owner_accept"] is True
nodes = adw_rep["stages"]["dry_run"]["outline"]
assert nodes[0]["step"] == "prepare" and nodes[1]["step"] == "plan" and nodes[2]["round"] == "build", nodes

# 5. Valid routes draft stage 6 lists ONLY seeded chore tasks whose ADW changes
t_chore = task_store.task_add("replay-sample", "Chore task", "Body", "chore")
t_feat = task_store.task_add("replay-sample", "Feat task", "Body", "feat")
r_c = support.start_pipeline(repo="replay-sample", issue=t_chore, adw="feature")
record.settle(r_c, "done")
r_f = support.start_pipeline(repo="replay-sample", issue=t_feat, adw="feature")
record.settle(r_f, "done")

draft_routes = {
    "schema": 1,
    "rule": [
        {"id": "chore-revise", "when": {"kind": "chore"}, "adw": "revise"},
        {"id": "default", "adw": "feature"},
    ],
}
routes_rep = adw.check_draft("routes", draft_routes, cfg=cfg, actor="run:pip-1")
assert routes_rep["status"] == "valid", routes_rep
assert routes_rep["requires_owner_accept"] is True
changes = routes_rep["stages"]["dry_run"]["changes"]
assert len(changes) == 1 and changes[0]["issue"] == t_chore, changes
assert changes[0]["before"]["adw"] == "feature" and changes[0]["after"]["adw"] == "revise"

# 6. Gate B: run: actor cannot auto-activate; owner can; configkinds keeps gate="review"
assert configkinds.REGISTRY["adw"].gate == "review"
assert configkinds.REGISTRY["routes"].gate == "review"
assert not adw.can_auto_activate("adw", "run:pip-123")
assert not adw.can_auto_activate("routes", "run:pip-123")
assert adw.can_auto_activate("adw", "u:owner")
assert adw.can_auto_activate("routes", "u:owner")

# 7. configitems serves engine defaults and config overrides for adw and routes
eng_feat = configitems.item("adw", "feature", level="engine")
assert eng_feat["level"] == "engine" and "name = \"feature\"" in configitems.read(eng_feat)["content"]
eng_routes = configitems.item("routes", "adw", level="engine")
assert eng_routes["level"] == "engine" and "interrupted-repair" in configitems.read(eng_routes)["content"]

print("BEHAVIOR_RESULT=" + json.dumps({"ok": True}))
"""
        result = _run_isolated_child(script)
        self.assertTrue(result["ok"])
