import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

from tests import support
from tests.harness import behavior_result, make_folders, run_script, scratch_env

SCRIPTS = Path(__file__).resolve().parent / "scripts"


def setUpModule():
    support.isolate_live_state()


class ScratchLookAlikeTests(unittest.TestCase):
    PRELUDE = (SCRIPTS / "scratch_look_alike_prelude.py").read_text()
    REPO = (SCRIPTS / "scratch_look_alike_repo.py").read_text()

    def run_body(self, body, repo=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            make_folders(root, "config", "state", "logs", "worktrees", "tmp")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            done = run_script(self.PRELUDE + (self.REPO if repo else "") + body, root, str(root), env=env, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            self.assertEqual(done.stdout.strip().splitlines()[-1:], ["ok"], done.stdout)

    def test_replay_of_the_incident_clears_after_note_made(self):
        self.run_body(r'''
done = [{"state": "done", "attempt": {"workspace": str(ws), "root": str(ws / (paths.SCRATCH_PREFIX + secrets.token_hex(8)))}} for i in range(5)]
scratch.survey = lambda: scratch.View(done, [], False)
made = []
for i in range(25):
    root = ws / (paths.SCRATCH_PREFIX + secrets.token_hex(8))
    for d in (root, root / "state", root / "config"):
        d.mkdir(mode=0o700)
    (root / ".gitignore").write_text("*\n")
    (root / ("scenario-" + str(i))).write_text("fake")
    made.append(root)
assert all(paths.valid_scratch(str(r)) for r in made)
assert pipeline.scratch_left(ws, [c]) == []
assert paths.SCRATCH_PREFIX in pipeline.kept_work(ws, [c], src, [])
pipeline.note_made(ws, [c])
assert pipeline.kept_work(ws, [c], src, []) == ""
pipeline.clear_workspace(ws, [c], src, [])
assert not ws.exists()
assert list((t / "state/locks").glob("git-*.lock"))
print("ok")
''')

    def test_recorded_root_keeps_the_workspace_and_note_made_skips_it(self):
        self.run_body(r'''
(t / "via").symlink_to(t)
spelled = t / "via" / "worktrees" / ws.name
link = t / "link"
link.symlink_to(ws)
lc = pipeline.Checkout("", "o/r", src, link, "main")
root = ws / (paths.SCRATCH_PREFIX + secrets.token_hex(8))
look = ws / (paths.SCRATCH_PREFIX + secrets.token_hex(8))
for d in (root, look):
    d.mkdir(mode=0o700)
    (d / "fixture.txt").write_text("fake")
scratch.survey = lambda: scratch.View([{"state": "done", "attempt": {"workspace": str(spelled), "root": str(spelled / root.name)}, "diagnostic": {"replacement_retained": str(spelled / root.name)}}], [], False)
assert pipeline.scratch_left(ws, [c]) == [root], pipeline.scratch_left(ws, [c])
assert pipeline.scratch_left(spelled, [pipeline.Checkout("", "o/r", src, spelled, "main")]) == [spelled / root.name]
assert pipeline.scratch_left(link, [lc]) == [link]
for w, k in ((ws, c), (link, lc)):
    pipeline.note_made(w, [k])
    made = json.loads(pipeline.made_record(w).read_text())
    assert not any(n.startswith(root.name) for n in made), w
    assert any(n.startswith(look.name) for n in made), w
try:
    pipeline.clear_workspace(ws, [c], src, [])
    raise SystemExit("cleared a workspace that holds an engine root")
except pipeline.RunFailed as e:
    assert "unresolved or unknown scratch" in e.why and str(root) in e.why, e.why
assert (root / "fixture.txt").exists() and (look / "fixture.txt").exists()
print("ok")
''')

    def test_unreadable_store_keeps_every_prefixed_entry_held(self):
        self.run_body(r'''
look = ws / (paths.SCRATCH_PREFIX + secrets.token_hex(8))
look.mkdir(mode=0o700)
(look / "fixture.txt").write_text("fake")
def unreadable():
    raise OSError("authority store unreadable")
scratch.survey = unreadable
assert pipeline.scratch_left(ws, [c]) == [look], pipeline.scratch_left(ws, [c])
pipeline.note_made(ws, [c])
made = json.loads(pipeline.made_record(ws).read_text())
assert not any(k.startswith(look.name) for k in made)
try:
    pipeline.clear_workspace(ws, [c], src, [])
    raise SystemExit("cleared a workspace while the store was unreadable")
except pipeline.RunFailed as e:
    assert "unresolved or unknown scratch" in e.why and str(look) in e.why, e.why
assert (look / "fixture.txt").exists()
print("ok")
''')

    def test_look_alike_changed_after_note_made_is_named_and_kept(self):
        self.run_body(r'''
scratch.survey = lambda: scratch.View([], [], False)
look = ws / (paths.SCRATCH_PREFIX + secrets.token_hex(8))
look.mkdir(mode=0o700)
(look / ".gitignore").write_text("*\n")
(look / "fixture.txt").write_text("fake")
pipeline.note_made(ws, [c])
(look / "fixture.txt").write_text("changed by a person")
try:
    pipeline.clear_workspace(ws, [c], src, [])
    raise SystemExit("removed a look alike that changed after the run")
except pipeline.RunFailed as e:
    assert look.name in e.why, e.why
assert (look / "fixture.txt").read_text() == "changed by a person"
print("ok")
''')

    def test_record_that_appears_after_the_first_read_still_holds_its_root(self):
        self.run_body(r'''
root = ws / (paths.SCRATCH_PREFIX + secrets.token_hex(8))
root.mkdir(mode=0o700)
reads = []
def late():
    reads.append(True)
    return scratch.View([] if len(reads) == 1 else [{"state": "done", "attempt": {"workspace": str(ws), "root": str(root)}}], [], False)
scratch.survey = late
assert pipeline.scratch_left(ws, [c]) == [root], pipeline.scratch_left(ws, [c])
print("ok")
''')

    def test_unreadable_folder_inside_a_look_alike_keeps_the_workspace(self):
        self.run_body(r'''
scratch.survey = lambda: scratch.View([], [], False)
look = ws / (paths.SCRATCH_PREFIX + secrets.token_hex(8))
locked =look / "locked"
locked.mkdir(parents=True)
locked.chmod(0)
try:
    assert pipeline.scratch_left(ws, [c]) == [locked], pipeline.scratch_left(ws, [c])
    try:
        pipeline.clear_workspace(ws, [c], src, [])
        raise SystemExit("cleared a workspace that could not be read")
    except pipeline.RunFailed as e:
        assert "unresolved or unknown scratch" in e.why and str(locked) in e.why, e.why
finally:
    locked.chmod(0o700)
assert locked.is_dir()
print("ok")
''')

    def test_truncated_record_is_named(self):
        if not (sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK) and os.getuid() != 0):
            self.skipTest("needs macOS confinement")
        self.run_body(r'''
import contextlib, io
from arete import heartbeat
scratch.holders = lambda *a, **k: []
run_dir = t / "runs" / "run-a"
run_dir.mkdir(parents=True)
scratch.RUN_BASELINES["run-a"] = {}
a = scratch.make("run-a", "verify", ws, run_dir)
scratch.end(a, run_dir)
record = sandbox.AUTHORITY / (a.selector + ".json")
raw = record.read_bytes()
record.write_bytes(raw[: len(raw) // 2])
other = t / "worktrees" / "other"
other.mkdir()
try:
    pipeline.kept_or_cleared(other, [pipeline.Checkout("", "o/r", src, other, "main")], src, [], "branch")
    raise SystemExit("kept a workspace beside an unreadable record")
except pipeline.RunFailed as e:
    assert str(record) in e.why, e.why
out = io.StringIO()
with contextlib.redirect_stdout(out):
    pipeline.retry_scratch(30)
assert str(record) in out.getvalue(), out.getvalue()
checks = heartbeat.check_scratch()
assert any(not c.ok and str(record) in c.detail for c in checks), checks
print("ok")
''')

    def test_no_store_frees_look_alikes(self):
        self.run_body(r'''
look = ws / (paths.SCRATCH_PREFIX + secrets.token_hex(8))
look.mkdir(mode=0o700)
sandbox.can_confine = lambda *a, **k: False
assert pipeline.scratch_left(ws, [c]) == [], pipeline.scratch_left(ws, [c])
sandbox.can_confine = lambda *a, **k: True
assert pipeline.scratch_left(ws, [c]) == [look], pipeline.scratch_left(ws, [c])
print("ok")
''')

    def test_cleanup_removes_only_stale_folders_with_no_file_and_no_recorded_root(self):
        self.run_body(r'''
empty, recorded, full = (paths.WORKTREES_DIR / n for n in ("empty", "recorded", "full"))
look, root, kept = (d / (paths.SCRATCH_PREFIX + secrets.token_hex(8)) for d in (empty, recorded, full))
for d in (look, root, kept):
    d.mkdir(parents=True)
(kept / "fixture.txt").write_text("fake")
scratch.survey = lambda: scratch.View([{"state": "done", "attempt": {"workspace": str(recorded), "root": str(root)}}], [], False)
old = time.time() - 8 * 86400
for d in (empty, recorded, full):
    os.utime(d, (old, old))
pipeline.cleanup_worktrees(7)
assert not empty.exists()
assert root.is_dir()
assert (kept / "fixture.txt").exists()
print("ok")
''', repo=False)


PROMOTION_FLOOR = {"accept_rate": 0.8, "max_drop": 0.2, "min_saving": 0.1}


class CodexHeadroomTests(unittest.TestCase):
    SCRIPT = (SCRIPTS / "codex_headroom.py").read_text()
    slow = True

    def test_codex_runs_under_a_headroom_and_is_capped_by_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp", "authority-parent")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                              GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull)
            script = self.SCRIPT
            done = run_script(script, root, str(root), env=env, timeout=900)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            r = behavior_result(done)
        shown = json.dumps(r, indent=1)
        refusal = ("arm codex: codex reports usage only when its turn ends, so it cannot stop a call at a token or "
                   "spend limit")
        for key in ("bare_dry", "bare_run"):
            self.assertEqual(r[key]["code"], 1, shown)
            self.assertIn(refusal, r[key]["err"], shown)
        self.assertFalse(r["bare_wrote"], shown)
        self.assertEqual(r["dry"]["code"], 0, shown)
        self.assertIn("codex route arm codex (codex gpt-5.4): launched only with at least 20000 tokens left",
                      r["dry"]["out"], shown)
        self.assertEqual(r["run"]["code"], 0, shown)
        self.assertEqual(r["a"], {"baseline": ["done", False], "codex": ["done", False]}, shown)
        self.assertIn("2 of 2 trials done", r["run"]["out"], shown)
        self.assertIn("pairs on validation", r["run"]["out"], shown)
        self.assertEqual(r["tight"]["code"], 3, shown)
        self.assertEqual(r["c"], {"baseline": ["done", "launched"], "codex": ["capped", "never launched"]}, shown)
        self.assertTrue(any("codex_headroom.tokens is 25000 and" in w and "tokens are left" in w
                            for w in r["c_stop"]), shown)
        self.assertEqual(r["over"]["code"], 3, shown)
        self.assertEqual(r["d"][0], "capped", shown)
        self.assertGreater(r["d"][1]["tokens"], 0, shown)
        self.assertIn("spend_usd", r["d"][1], shown)
        self.assertEqual(r["over_again"]["code"], 3, shown)
        self.assertIn("the tokens limit (tokens = 30000) is reached", r["d_stop"][-1], shown)
        self.assertEqual(r["d_new_starts"], 0, shown)
        self.assertEqual(r["changed"]["code"], 1, shown)
        self.assertIn("the design changed since", r["changed"]["err"], shown)
        self.assertEqual(r["raised"]["code"], 0, shown)
        self.assertNotIn("codex_headroom", r["plain_design"], shown)
        self.assertNotIn("codex", r["search_dry"]["out"], shown)


class ExperimentLineageTests(unittest.TestCase):
    slow = True
    SCRIPT = (SCRIPTS / "experiment_lineage.py").read_text()

    def scenario(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp", "authority-parent")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                              GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull)
            done = run_script(self.SCRIPT, root, str(root), mode, env=env, timeout=600)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            self.assertIn("LINEAGE_OK", done.stdout)

    def test_a_prompt_changed_between_trials_stops_before_the_next_trial(self):
        self.scenario("between")

    def test_a_prompt_changed_during_a_trial_is_drifted_and_left_out(self):
        self.scenario("during")

    def test_a_resume_after_an_engine_or_price_change_is_refused(self):
        self.scenario("versions")

    def test_boundary_changes_and_interrupted_observations_are_retained(self):
        self.scenario("boundaries")


class EvalLineageTests(unittest.TestCase):
    PRELUDE = (SCRIPTS / "eval_lineage_prelude.py").read_text()
    REPLAY_REGRESSION = (SCRIPTS / "eval_lineage_replay_regression.py").read_text()
    LEGACY_MIGRATION = (SCRIPTS / "eval_lineage_legacy_migration.py").read_text()
    CONTEXT_AND_EARLY_FAILURES = (SCRIPTS / "eval_lineage_context_and_early_failures.py").read_text()
    slow = True

    def script(self, body):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp", "home", "authority-parent")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), HOME=str(root / "home"), GIT_CONFIG_NOSYSTEM="1",
                              GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath",
                              GIT_CONFIG_VALUE_0=os.devnull)
            prefix = self.PRELUDE
            done = run_script(prefix + body, root, str(root), env=env, timeout=600)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            return behavior_result(done)

    def test_replay_lineage_regression_compare_and_step_ranges(self):
        r = self.script(self.REPLAY_REGRESSION)
        self.assertEqual(r["tasks"], 3)
        self.assertIn("REGRESSED", r["regression"])

    def test_legacy_migration_and_explanations(self):
        r = self.script(self.LEGACY_MIGRATION)
        self.assertIn("context_sha", r["columns"])

    def test_board_uses_only_baselines(self):
        r = self.script(r'''
state.migrate()
state.add_eval("a", "mixed", "one", True, 0, 0, "worktree")
state.add_eval("b", "mixed", "two", False, 0, 0, "worktree with prompts edited")
state.add_eval("c", "prompt-only", "three", False, 0, 0, "worktree with prompts edited")
rows = board.trend_figures()["evals"]
assert len(rows) == 1 and rows[0]["repo"] == "mixed", rows
print("BEHAVIOR_RESULT=" + json.dumps(rows[0]))
''')
        self.assertEqual((r["pass_rate"], r["tasks"]), (1.0, 1))

    def test_context_prompt_selection_and_early_failures(self):
        r = self.script(self.CONTEXT_AND_EARLY_FAILURES)
        self.assertEqual(r["contexts"], 5)


class OptimizeTests(unittest.TestCase):
    SEARCH_SCRIPT = (SCRIPTS / "optimize_search.py").read_text()
    PROMPT_DRIFT_SCRIPT = (SCRIPTS / "optimize_prompt_drift.py").read_text()

    slow = True

    def decision(self, champion, candidate, reference=0.01, charged=0.0, seconds=1.0):
        from arete import experiment, optimize

        def trials(arm, outcomes, cost):
            return [{"event": "trial", "task": "t", "split": "validation", "arm": arm, "repeat": n, "status": "done",
                     "accepted": ok, "charged_usd": charged, "reference_usd": cost, "repairs": 0, "calls": 2,
                     "tokens_in": 1, "tokens_out": 1, "wall_seconds": seconds}
                    for n, ok in enumerate(outcomes, 1)]

        accept = {"baseline": "champ", "split": "validation", "min_pairs": 6, "min_difference": 0.0,
                  "confidence": 0.95}
        rows = trials("champ", champion, 0.01) + trials("cand", candidate, reference)
        done = {experiment.key_of(t): t for t in rows}
        pair = experiment.pairing(done, "champ", "cand", "validation", 0.95)
        verdict = experiment.judge(pair, accept)
        return optimize.decide(experiment.arm_summary("cand", rows, 0.95), experiment.arm_summary("champ", rows, 0.95),
                               pair, verdict, PROMOTION_FLOOR)

    def test_a_clear_quality_win_is_promoted_on_quality(self):
        found = self.decision([False] * 6, [True] * 6)
        self.assertEqual((found["promote"], found["reason"]), (True, "promote on quality"))

    def test_a_cheaper_candidate_under_the_quality_floor_is_not_promoted(self):
        found = self.decision([True] * 6, [True, True, True, False, False, False], reference=0.002)
        self.assertEqual((found["promote"], found["reason"]), (False, "no promotion: under the quality floor"))

    def test_a_noisy_tie_at_equal_cost_is_not_a_win(self):
        found = self.decision([True] * 6, [True] * 6)
        self.assertEqual((found["promote"], found["reason"]), (False, "no promotion: noisy tie"))

    def test_missing_costs_and_tiny_samples_block_promotion(self):
        self.assertEqual(self.decision([False] * 6, [True] * 6, reference=None)["reason"],
                         "no promotion: missing costs")
        self.assertEqual(self.decision([False] * 3, [True] * 3)["reason"], "no promotion: insufficient evidence")

    def test_a_cheaper_candidate_at_held_quality_is_promoted_on_economics(self):
        found = self.decision([True] * 6, [True] * 6, reference=0.005)
        self.assertEqual((found["promote"], found["reason"]), (True, "promote on economics"))
        slower = self.decision([True] * 6, [True] * 6, reference=0.005, seconds=2.0)
        self.assertTrue(slower["promote"])

    def test_a_multiline_list_is_replaced_whole_and_nothing_else_moves(self):
        import tomllib

        from arete import optimize

        text = '[a]\nx = 1\n\n[steps.build]\nmodel = "m"\ntools = [\n  "Read",\n  "Edit",\n]\neffort = "low"\n\n[z]\ny = 2\n'
        after = optimize.edited(text, "steps.build", {"tools": ["Read", "Grep"], "mcp": ["one"]})
        want = tomllib.loads(text)
        want["steps"]["build"].update(tools=["Read", "Grep"], mcp=["one"])
        self.assertEqual(tomllib.loads(after), want)
        lines = after.splitlines(keepends=True)
        optimize.write_key(lines, "steps.build", "tools", None)
        self.assertNotIn("tools", tomllib.loads("".join(lines))["steps"]["build"])
        self.assertEqual(tomllib.loads("".join(lines))["z"], {"y": 2})

    def test_a_winner_that_needs_prompts_is_not_applied_in_part(self):
        from arete import optimize

        end = {"champion": "p", "overrides": {"model": "m", "prompts": "/x"}}
        self.assertIn("prompts", optimize.undeployable(end))
        self.assertEqual(optimize.undeployable({"overrides": {"model": "m"}}), "")

    def test_the_sample_search_promotes_applies_and_rolls_back_one_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp", "authority-parent")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                              GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull)
            script = self.SEARCH_SCRIPT
            done = run_script(script, root, str(root), env=env, timeout=600)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            r = behavior_result(done)
        shown = json.dumps(r, indent=1)
        self.assertEqual(r["prices"], 0, shown)
        self.assertEqual((r["dry"]["code"], r["dry_wrote"]), (0, False), shown)
        self.assertIn("candidate twin", r["dry"]["out"])
        self.assertEqual(r["search"]["code"], 0, shown)
        out = r["search"]["out"]
        self.assertIn("stronger: train fixed 3, broke 0: proposed", out)
        self.assertIn("promoted: stronger", out)
        self.assertIn("validation accepted 1 of 3 against 3 of 3: no promotion: under the quality floor", out)
        self.assertIn("validation accepted 3 of 3 against 3 of 3: no promotion: noisy tie", out)
        self.assertIn("promoted: none", out)
        self.assertIn("result: promote stronger over baseline", out)
        self.assertEqual((r["holdout"], r["holdout_again"]), (6, 6), shown)
        self.assertEqual(r["again"]["code"], 0, shown)
        self.assertIn("-model = \"scratch-a\"", r["preview"]["out"])
        self.assertIn("+model = \"scratch-b\"", r["preview"]["out"])
        self.assertIn("dry run: nothing was written", r["preview"]["out"])
        self.assertTrue(r["preview_same"])
        self.assertEqual(r["failed"]["code"], 1, shown)
        self.assertIn("apply failed", r["failed"]["err"])
        self.assertIn("config.toml is unchanged", r["failed"]["err"])
        self.assertTrue(r["failed_same"])
        self.assertEqual(r["failed_events"], ["failed"])
        self.assertEqual(r["apply"]["code"], 0, shown)
        self.assertEqual(r["changed"], [['model = "scratch-a"', 'model = "scratch-b"']], shown)
        self.assertTrue(r["same_length"])
        self.assertIn("already applied", r["apply_again"]["out"])
        self.assertEqual(r["rollback"]["code"], 0, shown)
        self.assertTrue(r["rolled_back"])
        self.assertEqual(r["rollback_again"]["code"], 1, shown)
        self.assertIn("nothing applied", r["rollback_again"]["err"])

    def test_a_changed_winning_prompt_is_refused_before_any_trial_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp", "authority-parent")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                              GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull)
            script = self.PROMPT_DRIFT_SCRIPT
            done = run_script(script, root, str(root), env=env, timeout=600)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            r = behavior_result(done)
        shown = json.dumps(r, indent=1)
        self.assertEqual((r["champion"], r["recorded"]), ("tuned", True), shown)
        self.assertIn("candidate tuned changed since it was selected", r["next_round"], shown)
        self.assertIn("candidate tuned changed since it was selected", r["holdout"], shown)
        self.assertEqual(r["resume"]["code"], 1, shown)
        self.assertIn("candidate tuned changed since it was selected", r["resume"]["err"], shown)
        self.assertEqual(r["after_refusals"], r["ran"], shown)
        self.assertIn("changed since the search began", r["engine"], shown)
        self.assertEqual(r["after_engine"], r["ran"], shown)
        self.assertEqual(r["proceed"]["code"], 0, shown)
        self.assertIn("holdout (scored once): tuned", r["proceed"]["out"], shown)
        self.assertGreater(r["final"], r["ran"], shown)


@unittest.skipUnless(sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK) and os.getuid() != 0,
                     "needs macOS confinement")
class AuthorityRotationTests(unittest.TestCase):
    STATES = ("initializing", "prepared", "active", "pending", "retained", "done")
    SCRIPT = (SCRIPTS / "authority_rotation.py").read_text()

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        make_folders(self.root, "home", "config", "state", "logs", "worktrees")
        self.env = scratch_env(self.root, keep=("PATH", "LANG", "LC_ALL"),
                          ARETE_WORKTREES_DIR=str(self.root / "worktrees"))
        self.authority = self.root / "home/verification-authority"
        self.hook = self.root / "config/hooks/demo.secret"
        self.run_mode("seed", ",".join(self.STATES))
        self.old = self.secret()

    def run_mode(self, mode, *extra):
        done = run_script(self.SCRIPT, self.root, str(self.root), mode, *extra, env=self.env, timeout=300)
        result = behavior_result(done) if any(
            line.startswith("BEHAVIOR_RESULT=") for line in done.stdout.splitlines()) else {}
        result["exit"] = done.returncode
        result["stderr"] = done.stderr
        return result

    def secret(self):
        return (self.authority / "signing.secret").read_bytes()

    def files(self):
        return {p.name: p.read_bytes() for p in self.authority.glob("*.json")}

    def report(self):
        return self.run_mode("report", self.old.hex())

    def assertRotated(self):
        shown = self.report()
        self.assertEqual(shown["broken"], 0, shown)
        self.assertEqual(sorted(shown["states"]), sorted(self.STATES), shown)
        self.assertEqual(shown["pin"], ["@playwright/mcp"], shown)
        self.assertEqual(shown["old"], 0, shown)
        self.assertEqual(shown["extra"], [], shown)
        self.assertNotEqual(self.secret(), self.old)

    def test_a_clean_rotation_re_signs_every_record_and_the_pin(self):
        hook = self.hook.read_bytes()
        done = self.run_mode("rotate")
        self.assertEqual((done["code"], done["out"]), (0, "re-signed records: 6, pins: 1\n"), done)
        self.assertRotated()
        shown = self.report()
        self.assertEqual((shown["mode"], shown["size"], shown["folder"]), (0o600, 32, 0o700), shown)
        self.assertEqual(self.hook.read_bytes(), hook)
        for key in (self.old, self.secret()):
            self.assertNotIn(key.hex(), done["out"] + done["err"])

    def test_a_live_run_lease_or_engine_lock_refuses_and_changes_nothing(self):
        cases = (("run", "pipeline"), ("lease", "attempt lease"), ("lock", "engine lock poll is held"))
        self.assertEqual(self.run_mode("unheld-lease")["code"], 0)
        self.assertRotated()
        for mode, text in cases:
            with self.subTest(mode=mode):
                if mode == "run":
                    self.run_mode("run")
                before = (self.secret(), self.files())
                done = self.run_mode("rotate" if mode == "run" else mode)
                self.assertEqual(done["code"], 1, done)
                self.assertIn("refused:", done["err"])
                if mode != "run":
                    self.assertIn(text, done["err"])
                self.assertEqual((self.secret(), self.files()), before)
                self.assertEqual(self.report()["extra"], [])

    def test_a_killed_rotation_finishes_when_run_again(self):
        for mode in ("kill-first-note", "kill-third-resign", "kill-swap"):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_mode(mode)["exit"], 9)
                self.assertEqual(self.secret(), self.old)
                self.assertTrue((self.authority / "signing.secret.next").exists())
                marker = self.authority / "rotation.marker"
                if mode != "kill-first-note":
                    names = json.loads(marker.read_text())["resigned"]
                    self.assertTrue(names and all(n.endswith((".json", ".pin")) for n in names), names)
                done = self.run_mode("rotate")
                self.assertEqual(done["code"], 0, done)
                self.assertRotated()
                self.old = self.secret()

    def test_a_file_that_verifies_under_neither_secret_stops_before_the_swap(self):
        self.run_mode("tamper", "2")
        name = f"{2:064x}.json"
        done = self.run_mode("rotate")
        self.assertEqual(done["code"], 1, done)
        self.assertIn(name, done["err"])
        self.assertEqual(self.secret(), self.old)
        self.assertFalse((self.authority / "signing.secret.next").exists())
        self.assertEqual(self.report()["broken"], 1)

    def test_a_tampered_file_after_a_partial_rotation_still_stops_before_the_swap(self):
        self.assertEqual(self.run_mode("kill-third-resign")["exit"], 9)
        self.run_mode("tamper", "5")
        done = self.run_mode("rotate")
        self.assertEqual(done["code"], 1, done)
        self.assertEqual(self.secret(), self.old)


if __name__ == "__main__":
    unittest.main()
