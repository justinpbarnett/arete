import json
import os
from datetime import timedelta
from pathlib import Path
import pkgutil
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from types import SimpleNamespace
import unittest
from unittest import mock
import arete

from arete import config, control, paths, pipeline, util, verify, cli

from tests import support
from tests.harness import behavior_result, bootstrap, make_folders, run_script, scratch_env

SCRIPTS = Path(__file__).resolve().parent / "scripts"


def setUpModule():
    support.isolate_live_state()


class VerificationPreflightTests(unittest.TestCase):
    def test_missing_requirement_blocks_before_model_or_app_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, saved = mock.Mock(side_effect=AssertionError("model must not start")), {}
            run = SimpleNamespace(dir=root, repo=None, agent=agent, log=lambda _: None,
                                  save=lambda name, text: saved.__setitem__(name, text))
            v = config.Verify(start="must-not-start", preflight={"runtime": f"{support.PYTHON} -c 'raise SystemExit(23)'"})
            b = SimpleNamespace(run=run, cfg=config.Config(), ws=root, ticket=None,
                                layer=SimpleNamespace(verify=v), next_step=lambda _: "verify")
            with mock.patch.object(verify, "serving", side_effect=AssertionError("app must not start")):
                result = pipeline.use_product(b, "verify", "verify")
            self.assertEqual(result.verdict, "blocked")
            self.assertFalse(result.finished)
            self.assertIn("exit 23", result.blocked_by)
            self.assertFalse(result.checks[0]["passed"])
            self.assertIn("verify-preflight.json", saved)
            self.assertIn("verify-preflight.md", saved)
            agent.assert_not_called()

    def test_successful_prerequisites_do_not_claim_product_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fixture.json").write_text('{"version": 1}')
            script = "import json; assert json.load(open('fixture.json'))['version'] == 1"
            v = config.Verify(preflight={"fixture": shlex.join([sys.executable, "-c", script])})
            result = verify.preflight(v, root, config.Config())
            self.assertEqual(result.verdict, "pass")
            self.assertEqual(result.summary, "Configured verification prerequisites passed.")
            self.assertEqual(len(result.checks), 1)
            self.assertTrue(result.checks[0]["passed"])
            self.assertIsNone(verify.preflight(config.Verify(), root, config.Config()))

    def test_preflight_timeout_is_blocked_and_does_not_execute_later_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            v = config.Verify(preflight={"slow": f"exec {support.PYTHON} -c 'import time; time.sleep(10)'",
                                         "later": "touch must-not-exist"}, preflight_seconds=.05)
            start = time.monotonic()
            result = verify.preflight(v, root, config.Config())
            self.assertEqual(result.verdict, "blocked")
            self.assertIn("timed out", result.blocked_by)
            self.assertLess(time.monotonic() - start, 2)
            self.assertFalse((root / "must-not-exist").exists())

    def test_preflight_commands_share_one_budget(self):
        v = config.Verify(preflight={"first": "first", "second": "second"}, preflight_seconds=2)
        done = subprocess.CompletedProcess("first", 0, stdout="ready", stderr="")
        with mock.patch.object(verify.time, "monotonic", side_effect=[0, .25, 2.1]), \
                mock.patch.object(verify.environment, "sh", return_value=done) as shell:
            result = verify.preflight(v, Path.cwd(), config.Config())
        self.assertEqual(result.verdict, "blocked")
        self.assertIn("budget exhausted", result.blocked_by)
        self.assertEqual(shell.call_count, 1)
        self.assertEqual(shell.call_args.kwargs["timeout"], 1.75)

    def test_invalid_preflight_configuration_is_refused(self):
        cases = ('preflight_seconds = true', 'preflight_seconds = 0', 'preflight_seconds = 301',
                 'preflight_seconds = nan', 'preflight = []', 'preflight = {probe = 1}',
                 'preflight = {probe = ""}')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for entry in cases:
                with self.subTest(entry=entry):
                    (root / "config.toml").write_text("[verify]\n" + entry + "\n")
                    with self.assertRaises((ValueError, TypeError)):
                        config.load_layer(root)
            (root / "config.toml").write_text('[verify]\npreflight_seconds=3\n[verify.preflight]\npython="python3 --version"\n')
            loaded = config.load_layer(root).verify
            self.assertEqual(loaded.preflight, {"python": "python3 --version"})
            self.assertEqual(loaded.preflight_seconds, 3)


class AskConfigTests(unittest.TestCase):
    def test_expire_days_is_a_finite_number_of_at_most_ten_years(self):
        for entry in ("expire_days = inf", "expire_days = nan", "expire_days = 999999", "expire_days = 0",
                      "expire_days = true", 'expire_days = "7"'):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                config.ask_table(tomllib.loads(entry))
        self.assertEqual(config.ask_table({}).expire_days, 7)
        self.assertEqual(config.ask_table(tomllib.loads("expire_days = 0.5")).expire_days, 0.5)
        longest = config.ask_table({"expire_days": config.MAX_ASK_EXPIRE_DAYS}).expire_days
        self.assertTrue(util.iso(util.now() - timedelta(days=longest)))


class TrackerLabelCatchUpTests(unittest.TestCase):
    SCRIPT = (SCRIPTS / "tracker_label_catch_up.py").read_text()

    def run_scenario(self, scenario):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp", "home")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), HOME=str(root / "home"),
                              GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                              GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull)
            done = run_script(self.SCRIPT, root, str(root), scenario, env=env, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            return behavior_result(done)

    def test_owed_label_lands_once_and_a_manual_removal_stays(self):
        r = self.run_scenario("catch-up")
        self.assertEqual(r["before"], ["pr"])
        self.assertEqual(r["labels"], ["agent", "agent:pr"])
        self.assertEqual((r["pending"], r["edits"], r["again"], r["after_removal"]), ("", 1, 1, 1))

    def test_a_ticket_without_its_label_is_not_run_again_until_it_lands(self):
        r = self.run_scenario("blocks-next-ticket")
        self.assertTrue(r["first"])
        self.assertIn("its pr label is not written yet", r["skips"])
        self.assertEqual(r["labels"], ["agent", "agent:pr"])

    def test_a_failed_write_keeps_its_debt_while_another_ticket_lands(self):
        r = self.run_scenario("retains-debt")
        self.assertEqual((r["debt"], r["other"]), ("failed", ""))
        self.assertEqual(r["labels"], ["agent", "agent:pr"])
        self.assertEqual((r["later"], r["failed"]), ("", ["agent", "agent:failed"]))

    def test_nothing_is_written_for_legacy_delivered_interrupted_or_running_rows(self):
        r = self.run_scenario("no-edit")
        self.assertEqual((r["edits"], r["legacy"], r["interrupted"], r["live"]), (0, None, "", "pr"))

    def test_the_newest_marker_governs(self):
        self.assertEqual(self.run_scenario("latest-marker-governs"), {"pending": "", "edits": 0})
        r = self.run_scenario("ties-and-unknown-repo")
        self.assertEqual((r["pending"], r["labels"], r["edits"]), ("pr", ["agent", "agent:pr"], 1))

    def test_a_local_task_gets_its_state(self):
        self.assertEqual(self.run_scenario("local-task"), {"state": "pr", "pending": "", "edits": 0})

    def test_a_held_run_lock_defers_the_write(self):
        r = self.run_scenario("held-lock")
        self.assertEqual((r["held_edits"], r["held_pending"]), (0, "pr"))
        self.assertEqual((r["edits"], r["pending"], r["labels"]), (1, "", ["agent", "agent:pr"]))


class OfflineWorkflowTests(unittest.TestCase):
    def workflow(self, scenario):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp", "authority-parent", "home")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), HOME=str(root / "home"),
                              GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                              GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull)
            script = "import sys, offline_workflow; offline_workflow.main(sys.argv[1], sys.argv[2])"
            result = run_script(script, root, str(root), scenario, env=env, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return behavior_result(result)

    PR_DRIVER = (SCRIPTS / "offline_pr_driver.py").read_text()

    def pr_workflow(self, scenario):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp", "authority-parent", "home")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), HOME=str(root / "home"),
                              GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                              GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull)
            (root / "driver.py").write_text(f"import sys;sys.path.insert(0,{str(support.SOURCE)!r})\n" + self.PR_DRIVER)
            result = subprocess.run([sys.executable, "-I", str(root / "driver.py"), str(root), scenario, "parent"],
                                    cwd=root, env=env, text=True, capture_output=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return behavior_result(result)

    def test_pr_survives_every_ending_after_it_opens(self):
        url = "https://github.com/octo/widgets/pull/7"
        for scenario, status, reading in (("stop", "stopped", True), ("park", "failed", True),
                                          ("automerge-gh-error", "failed", True),
                                          ("automerge-run-failed", "failed", True), ("ask", "needs-info", False),
                                          ("clean", "pr", True), ("ci-failure", "failed", True),
                                          ("partial-create-failure", "failed", True)):
            with self.subTest(scenario):
                got = self.pr_workflow(scenario)
                self.assertEqual((got["status"], got["pr_url"], got["report_prs"], got["reading"]),
                                 (status, url, [url], reading), got)
        self.assertIn("parked", self.pr_workflow("park")["summary"])
        stopped = self.pr_workflow("stop")
        self.assertEqual(set(stopped["after_merge"].values()), {"merged"})
        self.assertIn("pr_view", stopped["calls"])

    def test_killed_run_keeps_its_pr_and_its_restart_lists_it(self):
        url = "https://github.com/octo/widgets/pull/7"
        killed = self.pr_workflow("kill")
        self.assertEqual((killed["status"], killed["pr_url"], killed["report_prs"], killed["open"]),
                         ("interrupted", url, [url], False))
        twice = self.pr_workflow("kill-twice")
        self.assertEqual([r["status"] for r in twice["runs"]], ["interrupted", "failed"])
        self.assertEqual(twice["pr_url"], url)
        restart = self.pr_workflow("restart-stop")
        self.assertEqual([r["status"] for r in restart["runs"]], ["interrupted", "stopped"])
        self.assertGreater(restart["skipped"], 0)
        self.assertNotIn("pr", restart["started_steps"])
        self.assertEqual((restart["pr_url"], restart["report_prs"], restart["reading"]), (url, [url], True))

    def test_repair_stop_and_feedback_on_a_stopped_pr(self):
        url = "https://github.com/octo/widgets/pull/7"
        repair = self.pr_workflow("repair-stop")
        self.assertEqual((repair["status"], repair["pr_url"], repair["report_prs"]), ("stopped", url, [url]))
        self.assertEqual(set(repair["open_before"]), {repair["id"], repair["older"]})
        self.assertEqual(repair["older_before"], "pr")
        self.assertEqual(set(repair["after_merge"].values()), {"merged"})
        self.assertEqual(repair["streak"], 1)
        feedback = self.pr_workflow("stopped-feedback")
        self.assertEqual((feedback["added"], feedback["feedback_due"]), ([1, 0], [feedback["task"]]))

    def test_answered_and_retried_runs_take_the_stored_pr(self):
        url = "https://github.com/octo/widgets/pull/7"
        for scenario, first in (("answer-stop", "needs-info"), ("retry-stop", "failed")):
            with self.subTest(scenario):
                got = self.pr_workflow(scenario)
                self.assertEqual([r["status"] for r in got["runs"]], [first, "stopped"])
                self.assertEqual(got["runs"][0]["reading"], first == "failed")
                self.assertGreater(got["skipped"], 0)
                self.assertNotIn("pr", got["started_steps"])
                self.assertEqual((got["pr_url"], got["report_prs"]), (url, [url]))

    def test_overlapping_runs_keep_each_others_lessons(self):
        result = self.workflow("learn-overlap")
        self.assertEqual(result["child"], [""])
        self.assertEqual((result["status"], result["other"]), ("done", "done"), result["log"])
        self.assertIn("lesson A", result["expertise"])
        self.assertIn("lesson B", result["expertise"])
        self.assertIn("lesson B", result["learn2_prompt"])
        self.assertIn("so the learn step runs again", result["log"])
        self.assertEqual(len([s for s in result["subjects"] if "learn from" in s]), 2)
        self.assertEqual(set(result["names"]), {"expertise.md"})
        self.assertEqual(result["recorded"], result["head"])

    def test_overlapping_run_whose_second_learn_pass_fails_writes_nothing_of_its_own(self):
        result = self.workflow("learn-overlap-no-second-answer")
        self.assertEqual(result["status"], "done", result["log"])
        self.assertIn("lesson B", result["expertise"])
        self.assertNotIn("lesson A", result["expertise"])
        self.assertIn("saved in the run, not applied", result["log"])
        self.assertIn("lesson A", result["run_expertise"])
        self.assertEqual(len([s for s in result["subjects"] if "learn from" in s]), 1)

    def test_learn_commit_leaves_unrelated_layer_files_out(self):
        result = self.workflow("learn-unrelated-file")
        self.assertEqual(result["status"], "done", result["log"])
        self.assertEqual(result["last_commit"], ["expertise.md"])
        self.assertIn(" M config.toml", result["status_short"])
        self.assertIn("?? config.toml.bak", result["status_short"])
        self.assertIn("lesson A", result["expertise"])

    def test_complete_local_workflow_publishes_verified_change(self):
        result = self.workflow("pass")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["verified"], ["pass"])
        self.assertIn("kumquat", result["published"])

    def test_record_of_another_version_does_not_stop_a_replay(self):
        result = self.workflow("foreign-version-record")
        if result["foreign_unchanged"] is None:
            self.skipTest("the authority store is not redirected on this host")
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertTrue(result["foreign_unchanged"])

    def test_run_that_asks_parks_with_its_folder_and_resumes_its_session(self):
        result = self.workflow("asked")
        self.assertEqual(result["parked"], "needs-info")
        self.assertTrue(result["worktree"])
        self.assertEqual((result["open"], result["notices"]), (1, 1))
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["skipped"], 2)
        self.assertTrue(result["same_session"])
        self.assertTrue(result["answered"])
        self.assertEqual(result["taken"], [True])

    def test_secret_answer_reaches_the_check_by_name_and_is_stored_nowhere_else(self):
        result = self.workflow("asked-secret")
        self.assertEqual(result["parked"], "needs-info")
        self.assertEqual(result["status"], "done", result["log"])
        self.assertIn("kumquat", result["published"])
        self.assertEqual((result["mode"], result["names"], result["stored_before"]), ("0o600", ["REPLAY_SECRET"], {}))
        self.assertIn("nothing was stored", result["short"])
        self.assertNotIn("short", result["short"].replace("shortest", ""))
        self.assertIn("arete sets itself", result["reserved"])
        self.assertEqual(result["leaks"], [])
        self.assertEqual(result["answer_lines"], [[{"header": "Token", "kind": "secret", "name": "REPLAY_SECRET",
                                                    "set": True}]])
        self.assertEqual(result["diff_refusal"], ["notes.txt (holds the stored value of REPLAY_SECRET)"])
        self.assertEqual((result["seen"], result["unseen"], result["after_removal"]), (True, False, False))
        self.assertEqual(result["quoted"], ["café.txt"])
        self.assertTrue(result["escaped"])
        self.assertEqual(result["raced"], "[redacted]")
        self.assertEqual([bool(r) for r in result["reserved_names"]], [True, True, False])
        self.assertIn("`REPLAY_SECRET` is set for this repo", result["heard"])
        self.assertIn("secret question", result["reply"])

    def test_declined_secret_resumes_the_step_with_the_owner_declined(self):
        result = self.workflow("asked-declined")
        self.assertEqual(result["status"], "done", result["log"])
        self.assertEqual(result["heard"], ["the owner declined"])
        self.assertEqual(result["answer_lines"], [[{"header": "Token", "kind": "secret", "name": "REPLAY_SECRET",
                                                    "declined": True}]])
        self.assertEqual(result["stored_after"], [])

    def test_patch_holding_a_stored_value_fails_naming_the_variable_only(self):
        result = self.workflow("secret-in-patch")
        self.assertEqual(result["status"], "failed")
        text = result["summary"] + result["log"]
        self.assertIn("holds the stored value of REPLAY_SECRET", text)
        self.assertNotIn("arete117-marker-value-9f3k2", text)
        self.assertEqual(result["leaks"], [])

    def test_answered_run_stopped_while_waiting_for_a_slot_resumes_with_the_answers(self):
        result = self.workflow("asked-stopped-waiting")
        self.assertEqual(result["waited"], "stopped")
        self.assertTrue(result["waited_continues"])
        self.assertEqual(result["untaken"], [""])
        self.assertTrue(result["refusal_names_it"])
        self.assertIn("which is starting with them", result["while_starting"])
        self.assertNotIn("arete resume", result["while_starting"])
        self.assertTrue(result["resumes_it"])
        self.assertEqual((result["status"], result["skipped"]), ("done", 2))
        self.assertTrue(result["same_session"])
        self.assertTrue(result["answered"])
        self.assertEqual(result["taken"], [True])

    def test_answer_after_main_moved_resumes_the_asking_step_without_preparing_again(self):
        result = self.workflow("asked-moved")
        self.assertEqual(result["parked"], "needs-info")
        self.assertEqual(result["status"], "done", result["log"])
        self.assertEqual(result["renewed"], [])
        self.assertNotIn("prepares again", result["log"])
        self.assertEqual(result["skipped"], 2)
        self.assertTrue(result["same_session"])
        self.assertTrue(result["answered"])
        self.assertEqual(result["taken"], [True])
        self.assertIn("kumquat", result["published"])

    def test_answered_run_merges_a_moved_base_before_the_asking_step_resumes(self):
        result = self.workflow("fix-ask-moved")
        self.assertEqual(result["parked"], "needs-info")
        self.assertEqual(result["status"], "done", result["log"])
        self.assertEqual(result["skipped"], 4)
        self.assertEqual(result["renewed"], [])
        self.assertEqual([m["outcome"] for m in result["merges"]], ["clean"])
        self.assertTrue(result["merged_first"])
        self.assertTrue(result["same_session"])
        self.assertTrue(result["answered"])
        self.assertEqual(result["taken"], [result["id"]])
        self.assertIn("kumquat", result["notes"])
        self.assertEqual(result["moved_file"].strip(), "moved")

    def test_answered_run_resumes_the_session_with_the_new_run_folder_and_its_checks_path(self):
        result = self.workflow("fix-ask-still")
        self.assertIn(f"{result['id']}/checks NAME`", result["prompt"])
        self.assertNotIn(result["parked_id"], result["prompt"])

    def test_answered_run_with_an_unmoved_base_writes_no_base_merge(self):
        result = self.workflow("fix-ask-still")
        self.assertEqual(result["status"], "done", result["log"])
        self.assertEqual(result["merges"], [])
        self.assertEqual(result["skipped"], 4)
        self.assertTrue(result["same_session"])
        self.assertEqual(result["taken"], [result["id"]])

    def test_answered_run_that_cannot_merge_a_moved_base_fails_and_keeps_the_answers(self):
        result = self.workflow("fix-ask-conflict")
        self.assertEqual(result["status"], "failed", result["log"])
        self.assertIn("origin/main", result["summary"])
        self.assertIn("notes.txt", result["summary"])
        self.assertEqual(len(result["merges"]), 1)
        self.assertTrue(result["merges"][0]["error"])
        self.assertEqual(result["taken"], [""])
        self.assertEqual(result["head"], result["kept_head"])
        self.assertEqual(result["dirty"], "")
        at = result["resume"].index("--continue")
        self.assertEqual(result["resume"][at:at + 2], ["--continue", result["id"]])

    def test_edits_made_before_an_ask_are_kept_while_it_waits_and_published_after_the_answer(self):
        result = self.workflow("asked-edited")
        self.assertEqual(result["parked"], "needs-info")
        self.assertIn(f"Arete-Run: {result['parked_id']}", result["kept"])
        self.assertEqual(result["dirty"], "")
        self.assertEqual(result["status"], "done", result["log"])
        self.assertEqual(result["skipped"], 2)
        self.assertTrue(result["same_session"])
        self.assertTrue(result["answered"])
        self.assertIn("kumquat", result["published"])

    def test_answer_sent_again_while_another_ask_waits_names_that_ask_not_a_resume(self):
        result = self.workflow("asked-twice")
        self.assertEqual((result["parked"], result["open"]), ("needs-info", 2))
        self.assertIn(f"stored for run {result['parked_id']}, which goes on with them when its 1 other ask is answered",
                      result["resent"])
        self.assertNotIn("arete resume", result["resent"])
        self.assertIn("ended needs-info", result["resumed"])
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["answered"])
        self.assertEqual(result["taken"], [True, True])

    def test_answered_run_killed_while_waiting_for_a_slot_restarts_with_the_answers(self):
        result = self.workflow("asked-killed-waiting")
        self.assertEqual(result["waited"], "interrupted")
        self.assertTrue(result["waited_continues"])
        self.assertEqual(result["untaken"], [""])
        self.assertEqual((result["status"], result["skipped"]), ("done", 2))
        self.assertTrue(result["same_session"])
        self.assertTrue(result["answered"])
        self.assertEqual(result["taken"], [True])

    def test_run_whose_kept_session_is_gone_restarts_the_step_with_the_ask_and_answers(self):
        result = self.workflow("asked-gone")
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["answered"])
        self.assertFalse(result["same_session"])
        self.assertEqual(result["taken"], [True])

    def test_run_parked_with_scratch_held_records_its_work_without_scratch(self):
        result = self.workflow("held-scratch")
        self.assertIn("parked", result["parked"])
        self.assertIn("Arete-Run: ", result["kept"])
        look = ".arete-scratch-0123456789abcdef"
        engine = [n for n in result["held"] if n != look]
        self.assertIn(look, result["held"])
        if engine:
            self.assertTrue(all(n in result["left"] for n in engine))
            self.assertNotIn(look, result["left"])
            self.assertIn(look + "/.gitignore", result["recorded"])
            self.assertFalse([n for n in result["recorded"] if any(n.startswith(e) for e in engine)])
        else:
            self.assertIn(look, result["left"])
            self.assertFalse([n for n in result["recorded"] if ".arete-scratch-" in n])
        self.assertTrue(result["head_recorded"])
        self.assertEqual(result["unpushed"], "2")
        self.assertIn("unresolved or unknown scratch resources", result["while_held"])
        self.assertTrue(result["record_kept"])
        self.assertEqual(result["prepared"], ["pass"])
        self.assertEqual(result["status"], "done")
        self.assertNotIn("no record of", result["summary"])
        self.assertIn("kumquat", result["published"])

    def test_file_edited_after_parked_run_still_stops_forced_run(self):
        result = self.workflow("held-scratch-edited")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["prepared"], ["fail"])
        self.assertIn("no record of leaving there (notes.txt)", result["summary"])
        self.assertTrue(result["edited"])
        self.assertEqual(result["origin"].strip(), "refs/heads/main")

    def assert_parked_with_kept_edits(self, result):
        self.assertIn("parked:", result["parked"])
        self.assertIn("workspace left in place", result["parked"])
        self.assertIn(f"Arete-Run: {result['parked_id']}", result["message"])
        self.assertEqual(result["dirty"], "")
        self.assertEqual(result["recorded"]["kept"], {".": {"head": result["head"], "onto": result["parent"]}})
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertIn("left unpushed", result["build_prompt"])
        self.assertIn(f"Arete-Run: {result['parked_id']}", result["trailers"])
        self.assertIn(f"Arete-Run: {result['id']}", result["trailers"])
        self.assertIn("kumquat", result["published"])
        self.assertIn("quince", result["published"])

    def test_forced_run_after_a_park_continues_from_the_kept_edits(self):
        result = self.workflow("kept-park")
        self.assert_parked_with_kept_edits(result)
        self.assertEqual(result["parent"], result["main"])
        self.assertEqual(result["origin"].strip(), "refs/heads/main")

    def test_run_that_pushes_kept_edits_then_fails_records_no_stale_kept_head(self):
        result = self.workflow("kept-push-then-fail")
        self.assertEqual(result["status"], "failed", result["summary"])
        self.assertIn("publishing failed", result["summary"])
        self.assertIn(f"Arete-Run: {result['parked_id']}", result["trailers"])
        self.assertEqual(result["onto_recorded"], [])

    def test_forced_run_after_a_park_continues_on_top_of_the_pushed_branch(self):
        result = self.workflow("kept-park-pushed")
        self.assert_parked_with_kept_edits(result)
        self.assertEqual(result["parent"], result["seed"])
        self.assertIn("continuing commits already pushed", result["log"])

    def test_forced_run_after_the_owner_removes_the_parked_worktree_starts_from_origin(self):
        result = self.workflow("kept-removed")
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertIn(f"Arete-Run: {result['parked_id']}", result["message"])
        self.assertNotIn(result["parked_id"], result["trailers"])
        self.assertNotIn("left unpushed", result["build_prompt"])
        self.assertIn("kumquat", result["published"])
        self.assertNotIn("quince", result["published"])

    def test_park_without_edits_keeps_nothing(self):
        result = self.workflow("park-no-edits")
        self.assertIn("parked:", result["parked"])
        self.assertNotIn("edits kept", result["parked"])
        self.assertEqual(result["head"], result["main"])
        self.assertEqual(result["recorded"]["kept"], {})
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertNotIn("left unpushed", result["log"])

    def test_resume_after_main_moved_continues_from_the_failed_runs_edits(self):
        result = self.workflow("failed-resume-moved")
        self.assertIn("checks still failing", result["failed"])
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertIn("so this run prepares again, keeps the plan and the build", result["log"])
        self.assertIn("merged `origin/main` into the edits the last run kept", result["log"])
        self.assertNotIn("plan", result["steps"])
        self.assertNotIn("build", result["steps"])
        self.assertIn("kumquat", result["published"])
        self.assertEqual(result["moved"], "moved.txt")
        self.assertIn(result["main"], result["head_parents"])
        self.assertIn(f"Arete-Run: {result['failed_id']}", result["trailers"])

    def test_resume_after_a_renewed_prepare_failed_past_its_merge_continues_from_the_kept_edits(self):
        result = self.workflow("failed-resume-reopened-moved")
        self.assertIn("checks still failing", result["failed"])
        self.assertEqual(result["mid"]["status"], "failed")
        self.assertIn("setup command `exit 3` failed", result["mid"]["summary"])
        self.assertIn("merged `origin/main` into the edits the last run kept", result["mid"]["log"])
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertTrue(result["kept_published"])
        self.assertIn("kumquat", result["published"])
        self.assertIn("quince", result["published"])
        self.assertEqual(result["moved"], "moved.txt")
        self.assertIn(f"Arete-Run: {result['failed_id']}", result["trailers"])
        self.assertIn(f"Arete-Run: {result['mid']['id']}", result["trailers"])

    def test_prepare_run_alone_leaves_kept_edits_for_the_resume_after_main_moved(self):
        result = self.workflow("failed-resume-only-prepare-moved")
        self.assertIn("checks still failing", result["failed"])
        self.assertEqual(result["mid"]["only_step"], "prepare")
        self.assertEqual(result["mid"]["status"], "failed")
        self.assertIn("a prepare run alone would change them outside that chain", result["mid"]["summary"])
        self.assertIn(f"`arete resume {result['failed_id']}` publishes them", result["mid"]["summary"])
        self.assertTrue(result["mid"]["unmoved"])
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertIn("merged `origin/main` into the edits the last run kept", result["log"])
        self.assertTrue(result["kept_published"])
        self.assertIn("kumquat", result["published"])
        self.assertEqual(result["moved"], "moved.txt")
        self.assertIn(result["main"], result["head_parents"])
        self.assertNotIn(f"Arete-Run: {result['mid']['id']}", result["trailers"])

    def assert_stepped_edits_published(self, result):
        self.assertIn("checks still failing", result["failed"])
        self.assertEqual(result["mid"]["only_step"], "implement")
        self.assertEqual(result["mid"]["status"], "step", result["mid"]["summary"])
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertTrue(result["kept_published"])
        self.assertTrue(result["stepped_published"])
        self.assertIn("kumquat", result["published"])
        self.assertIn("quince", result["published"])
        self.assertIn(f"Arete-Run: {result['failed_id']}", result["trailers"])
        self.assertIn(f"Arete-Run: {result['mid']['id']}", result["trailers"])
        self.assertNotIn("plan", result["steps"])
        self.assertNotIn("build", result["steps"])

    def test_resume_after_an_implement_run_alone_publishes_its_kept_edits(self):
        self.assert_stepped_edits_published(self.workflow("failed-resume-stepped"))

    def test_resume_after_an_implement_run_alone_on_a_pushed_branch_publishes_its_kept_edits(self):
        result = self.workflow("failed-resume-stepped-pushed")
        self.assert_stepped_edits_published(result)
        self.assertEqual(result["seed"], "seed.txt")

    def test_resume_after_an_implement_run_alone_validates_the_changed_code_again(self):
        result = self.workflow("failed-resume-stepped-late")
        self.assertIn("publishing failed", result["failed"])
        self.assertEqual(result["mid"]["status"], "step", result["mid"]["summary"])
        self.assertEqual(result["status"], "done", result["summary"])
        for step in ("checks", "verify", "review"):
            self.assertIn(step, result["started"])
        self.assertNotIn("plan", result["started"])
        self.assertNotIn("implement", result["started"])
        self.assertTrue(result["stepped_published"])
        self.assertIn("quince", result["published"])

    def assert_broken_build_not_published(self, result, step, said):
        self.assertIn("publishing failed", result["failed"])
        self.assertEqual(result["mid"]["status"], "step", result["mid"]["summary"])
        self.assertEqual(result["mid"]["later"]["only_step"], step)
        self.assertEqual(result["mid"]["later"]["status"], "step")
        self.assertIn(said, result["mid"]["later"]["summary"])
        self.assertEqual(result["status"], "failed", result["summary"])
        self.assertIn("checks", result["started"])
        self.assertNotIn("pr", result["started"])
        self.assertNotIn("publish", result["started"])
        self.assertFalse(result["stepped_published"])
        self.assertIn("kumquat", result["published"])
        self.assertNotIn("lime", result["published"])

    def test_failed_checks_run_alone_after_a_new_build_leave_the_resume_running_checks(self):
        result = self.workflow("failed-resume-stepped-late-broken-checks")
        self.assert_broken_build_not_published(result, "checks", "checks fail")

    def test_verify_run_alone_after_a_new_build_leaves_the_resume_running_checks(self):
        result = self.workflow("failed-resume-stepped-late-broken-verify")
        self.assert_broken_build_not_published(result, "verify", "verify pass")

    def test_hand_commit_after_an_implement_run_alone_still_stops_the_resume(self):
        result = self.workflow("failed-resume-stepped-hand")
        self.assertEqual(result["status"], "failed")
        self.assertIn("were not made by this run's chain", result["summary"])
        self.assertEqual(result["steps"], [])
        self.assertTrue(result["unmoved"])
        self.assertEqual(result["published"], "")

    def test_forged_trailer_after_an_implement_run_alone_still_stops_the_resume(self):
        result = self.workflow("failed-resume-stepped-forged")
        self.assertEqual(result["status"], "failed")
        self.assertIn("preparing again would drop them", result["summary"])
        self.assertEqual(result["steps"], [])
        self.assertTrue(result["unmoved"])
        self.assertEqual(result["published"], "")

    def assert_prepared_again_on_the_kept_edits(self, result):
        self.assertIn("checks still failing", result["failed"])
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertNotIn("preparing again would drop them", result["summary"])
        self.assertIn("left unpushed", result["log"])
        self.assertIn("plan", result["steps"])
        self.assertTrue(result["kept_published"])
        self.assertIn("kumquat", result["published"])
        self.assertIn("quince", result["published"])
        self.assertIn(f"Arete-Run: {result['failed_id']}", result["trailers"])

    def test_preparing_a_failed_run_again_continues_from_its_kept_edits(self):
        result = self.workflow("failed-again-prepare")
        self.assert_prepared_again_on_the_kept_edits(result)
        self.assertEqual(result["moved"], "")

    def test_preparing_a_failed_run_again_after_main_moved_continues_from_its_kept_edits(self):
        result = self.workflow("failed-again-prepare-moved")
        self.assert_prepared_again_on_the_kept_edits(result)
        self.assertEqual(result["moved"], "moved.txt")
        self.assertIn("merged `origin/main` into the edits the last run kept", result["log"])

    def test_a_limit_wait_longer_than_the_budget_does_not_park_the_run(self):
        result = self.workflow("limit-wait")
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertGreater(result["wall"], 1)
        self.assertAlmostEqual(result["budget"]["wait_minutes_left_out"]["limit"], 10, delta=0.1)
        self.assertEqual(result["parked"], {})

    def test_work_longer_than_the_budget_still_parks_the_run(self):
        result = self.workflow("work-over-budget")
        self.assertEqual(result["status"], "failed")
        self.assertIn("parked: the run chain reached its cumulative 1 minute budget", result["summary"])
        self.assertEqual(result["parked"]["wait_minutes_left_out"], {"limit": 0, "slot": 0})

    def test_a_continuation_leaves_out_the_limit_wait_of_the_earlier_run(self):
        result = self.workflow("failed-again-prepare-limit-wait")
        self.assertIn("checks still failing", result["failed"])
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertEqual(result["budget"]["parents"], [result["failed_id"]])
        self.assertAlmostEqual(result["budget"]["earlier_wait_minutes_left_out"]["limit"], 10, delta=0.1)
        self.assertIn("kumquat", result["published"])
        self.assertIn("quince", result["published"])

    def test_a_continuation_is_not_charged_for_a_machine_sleep_inside_the_earlier_run(self):
        result = self.workflow("failed-again-prepare-sleep")
        self.assertEqual(result["failed_status"], "failed")
        self.assertIn("checks still failing", result["failed"])
        self.assertGreater(result["failed_span"], 600)
        self.assertLess(result["failed_budget"]["run_seconds"], 60)
        self.assertEqual(result["failed_budget"]["wait_minutes_left_out"], {"limit": 0, "slot": 0})
        self.assertEqual(result["budget"]["parents"], [result["failed_id"]])
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertIn("kumquat", result["published"])
        self.assertIn("quince", result["published"])

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork and SIGKILL")
    def test_a_killed_verification_is_not_charged_until_reaped_after_sleep(self):
        result = self.workflow("failed-resume-killed")
        self.assertEqual(result["failed_status"], "interrupted")
        self.assertGreaterEqual(result["failed_span"], 600)
        self.assertIsNotNone(result["failed_budget"]["verification_active_since"])
        self.assertGreaterEqual(result["failed_budget"]["verification_open_seconds"], 0)
        self.assertLess(result["inherited_run"], 60)
        self.assertLess(result["inherited_verify"], 60)
        self.assertEqual(result["budget"]["parents"], [result["failed_id"]])
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertIn("kumquat", result["published"])

    def test_preparing_again_over_a_chain_commit_with_no_kept_record_is_still_held(self):
        result = self.workflow("failed-again-prepare-unrecorded")
        self.assertEqual(result["status"], "failed")
        self.assertIn("preparing again would drop them", result["summary"])
        self.assertEqual(result["steps"], [])
        self.assertTrue(result["unmoved"])
        self.assertEqual(result["published"], "")

    def test_behavioral_check_failure_stops_before_verification_or_publication(self):
        result = self.workflow("failed-check")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("verify", result["steps"])
        self.assertEqual(result["origin"].strip(), "refs/heads/main")

    def test_missing_verification_is_reported_blocked_and_does_not_hold_an_approved_review(self):
        result = self.workflow("missing-verification")
        self.assertEqual(result["verified"], ["blocked"])
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertIn("in use blocked", result["summary"])

    def test_failed_check_caused_by_the_environment_is_blocked_without_a_repair(self):
        result = self.workflow("verify-environment-fault")
        self.assertEqual(result["verified"], ["blocked"])
        self.assertNotIn("verify_fix", result["steps"])
        self.assertIn("blocked", result["summary"])

    def test_passing_verdict_with_an_environment_failed_check_is_blocked_without_a_repair(self):
        result = self.workflow("verify-environment-fault-pass")
        self.assertEqual(result["verified"], ["blocked"])
        self.assertNotIn("verify_fix", result["steps"])

    def test_app_start_crash_naming_a_permissions_identifier_still_runs_the_repair(self):
        result = self.workflow("verify-start-crash")
        self.assertIn("verify_fix", result["steps"])

    def test_failed_check_caused_by_the_change_still_runs_the_repair(self):
        result = self.workflow("verify-change-fault")
        self.assertIn("verify_fix", result["steps"])
        self.assertEqual(result["verified"], ["pass"])

    def test_check_broken_by_a_verify_fix_gets_a_repair_before_the_run_fails(self):
        result = self.workflow("verify-fix-breaks-check")
        self.assertIn("verify_fix", result["steps"])
        self.assertIn("check_repair", result["steps"])
        self.assertNotIn("the checks failed after", result["summary"])
        self.assertEqual(result["status"], "done", result["summary"])

    def test_checks_run_again_after_a_verify_fix_count_as_run_time_not_verification_time(self):
        result = self.workflow("verify-change-fault-timed")
        self.assertIn("verify_fix", result["steps"])
        self.assertEqual(result["verified"], ["pass"])
        self.assertEqual(result["checks_in_verification"], [False, False])

    def test_app_start_denied_by_the_sandbox_is_blocked_without_a_repair(self):
        result = self.workflow("verify-start-denied")
        self.assertEqual(result["verified"], ["blocked"])
        self.assertIn("blocked", result["summary"])
        self.assertNotIn("verify_fix", result["steps"])

    def test_substantive_review_failure_cannot_become_done(self):
        result = self.workflow("review-rejected")
        self.assertEqual(result["status"], "failed")
        self.assertIn("review", result["summary"])
        self.assertIn("requested changes", result["summary"])

    def test_forced_run_after_a_specification_edit_repairs_instead_of_parking(self):
        result = self.workflow("edited-after-held-pr")
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertIn("review_fix", result["steps"])
        self.assertIn("left out 3 earlier repair cycles", result["log"])
        self.assertEqual(result["budget"]["left_out"], [result["old"]])
        self.assertEqual(result["budget"]["parents"], [])

    def test_notes_only_review_finishes_without_a_fix_round(self):
        result = self.workflow("review-notes-only")
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertNotIn("review_fix", result["steps"])
        self.assertEqual(result["review"]["verdict"], "approve")
        self.assertFalse(result["review"]["unresolved"])
        self.assertEqual(result["review"]["problems"], [])
        self.assertEqual(len(result["review"]["notes"]), 2)

    def test_notes_go_in_the_pr_body_and_do_not_hold_it(self):
        result = self.workflow("review-notes-dry-run")
        self.assertIn("Review notes (not blocking)", result["body"])
        self.assertIn("fixture major note", result["body"])
        self.assertIn("fixture minor note", result["body"])
        self.assertNotIn("Held as a draft", result["body"])

    def test_review_loop_converges_on_the_earlier_blocker(self):
        result = self.workflow("review-converges")
        self.assertEqual(result["status"], "done", result["summary"])
        for name in ("review", "review_fix", "review2"):
            self.assertIn(name, result["steps"])
        for name in ("review_fix2", "review3"):
            self.assertNotIn(name, result["steps"])
        for text in ("B1", "[issue]", "quince is listed after kumquat"):
            self.assertIn(text, result["earlier"])
        self.assertIn("+quince", result["fixed"])
        self.assertNotIn("+kumquat", result["fixed"])
        self.assertNotIn("could not read what changed", result["earlier"])
        for text in ("B1", "fixture major note", "[issue]"):
            self.assertIn(text, result["fix_prompt"])
        self.assertEqual(len(result["review"]["notes"]), 1)

    def test_refactor_label_skips_the_check_in_use(self):
        for scenario in ("refactor-pass", "refactor-review-fixes"):
            result = self.workflow(scenario)
            self.assertEqual(result["status"], "done", result["summary"])
            self.assertFalse([s for s in result["steps"] if s.startswith("verify")])
            self.assertNotIn("verify", result["seam"])
            self.assertEqual(result["prepared_labels"], [["agent", "refactor"]])
            for name, prompt in result["prompts"].items():
                if name.startswith("review") and "fix" not in name:
                    self.assertIn("skipped because the ticket is labeled `refactor`", prompt)
        body = self.workflow("refactor-dry-run")["pr_body"]
        self.assertIn("skipped because the ticket is labeled `refactor`", body)

    def test_check_in_use_runs_once_after_the_review_fixes(self):
        result = self.workflow("review-converges")
        self.assertEqual([s for s in result["steps"] if s.startswith("verify")], ["verify", "verify2"])
        self.assertEqual(result["steps"][-3:], ["review2", "verify2", "learn"])
        self.assertIn("predates the review fixes", result["prompt"])
        result = self.workflow("review-multiple-fixes")
        self.assertEqual([s for s in result["steps"] if s.startswith("verify")], ["verify", "verify2"])
        third = next(p for n, p in result["prompts"].items() if n.startswith("review3"))
        self.assertEqual(third.count("predates the review fixes"), 1)
        self.assertEqual([s for s in self.workflow("pass")["steps"] if s.startswith("verify")], ["verify"])

    def test_final_check_in_use_failure_runs_the_repair(self):
        failed = self.workflow("review-final-verify-fails")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("still does not work in use", failed["summary"])
        self.assertEqual(failed["steps"][-4:], ["verify2", "verify_fix", "verify3", "learn"])
        recovered = self.workflow("review-final-verify-recovers")
        self.assertEqual(recovered["status"], "done", recovered["summary"])
        self.assertNotIn("predates the review fixes", recovered["pr_body"])

    def test_an_unconfigured_check_in_use_and_old_tickets_still_work(self):
        result = self.workflow("no-verify-config")
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertNotIn("verify", result["steps"])
        self.assertEqual(result["old_payload_labels"], [])

    def test_check_broken_by_a_review_fix_gets_a_repair_before_the_run_fails(self):
        result = self.workflow("review-fix-breaks-check")
        self.assertIn("check_repair1_2", result["steps"])
        self.assertNotIn("the checks failed after", result["summary"])
        self.assertEqual(result["status"], "done", result["summary"])

    def test_security_blocker_cannot_be_waived(self):
        result = self.workflow("review-security-waived")
        self.assertEqual(result["status"], "failed")
        self.assertIn("requested changes", result["summary"])

    def test_regression_from_the_fix_still_blocks(self):
        result = self.workflow("review-new-regression")
        self.assertEqual(result["status"], "failed")
        self.assertIn("requested changes", result["summary"])

    def test_severity_outside_the_scale_still_blocks(self):
        result = self.workflow("review-severity-slip")
        self.assertEqual(result["status"], "failed")
        self.assertIn("requested changes", result["summary"])

    def test_review_answer_without_a_problems_list_does_not_approve(self):
        result = self.workflow("review-problems-missing")
        self.assertEqual(result["status"], "failed")

    def test_unanswered_earlier_blocker_stays_open(self):
        result = self.workflow("review-earlier-omitted")
        self.assertEqual(result["status"], "failed")
        self.assertIn("requested changes", result["summary"])

    def test_approved_review_is_not_held_by_a_blocked_check_that_is_an_environment_fault(self):
        result = self.workflow("blocked-check-environment")
        self.assertEqual(result["verified"], ["blocked"])
        self.assertEqual(result["status"], "done", result["summary"])
        self.assertIn("in use blocked", result["summary"])

    def test_blocked_check_that_names_the_guard_still_holds_an_approved_review(self):
        result = self.workflow("blocked-check-guard")
        self.assertEqual(result["status"], "failed")
        self.assertIn("blocked", result["summary"])


class FailedPrepareHistoryTests(unittest.TestCase):
    @staticmethod
    def end(step, result="pass", **extra):
        return {"event": "step_end", "step": step, "result": result, **extra}

    def test_failed_prepare_run_adds_nothing(self):
        a = [self.end("prepare"), self.end("plan"), self.end("verify", "fail")]
        b = [{"event": "route", "skipped": 0}, self.end("prepare", "fail"), self.end("respond")]
        history = arete.factory.history([b, a])
        self.assertEqual([line["step"] for line in history], ["prepare", "plan", "verify"])
        self.assertTrue(arete.factory.reopening([b, a]))
        self.assertFalse(arete.factory.reopening([a]))

    def test_redone_join_puts_own_prepare_first(self):
        a = [self.end("prepare", n=1), self.end("plan"), self.end("implement")]
        c = [{"event": "route", "skipped": 2, "redone": 1}, self.end("prepare", n=2), self.end("verify")]
        history = arete.factory.history([c, a])
        self.assertEqual([line["step"] for line in history], ["prepare", "plan", "implement", "verify"])
        self.assertEqual(history[0]["n"], 2)

    def test_failed_prepare_is_not_takable_but_other_fails_are(self):
        flow = object.__new__(arete.factory.Flow)
        flow.earlier, flow.redone, flow.skipped = [self.end("prepare", "fail"), self.end("verify", "fail"), {}], 0, 0
        self.assertFalse(flow.takable(flow.earlier[0]))
        flow.skipped = 1
        self.assertTrue(flow.takable(flow.earlier[1]))


class CodeIdentityTests(unittest.TestCase):
    CLAIM = (SCRIPTS / "code_identity_claim.py").read_text()
    HEADERS = (SCRIPTS / "code_identity_headers.py").read_text()
    BAD_RELOAD = (SCRIPTS / "code_identity_bad_reload.py").read_text()

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        make_folders(self.root, "config", "state", "logs", "worktrees", "tmp", "home")
        self.env = scratch_env(self.root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                          ARETE_WORKTREES_DIR=str(self.root / "worktrees"),
                          TMPDIR=str(self.root / "tmp"), HOME=str(self.root / "home"),
                          GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.run_child(support.SOURCE, "from arete import state; state.migrate()")

    def tree(self, name="tree") -> Path:
        copy = self.root / name
        copy.mkdir()
        for folder in ("arete", "adws"):
            shutil.copytree(support.SOURCE / folder, copy / folder, ignore=shutil.ignore_patterns("__pycache__"))
        return copy

    def run_child(self, tree, script, *args, **env):
        argv = [sys.executable, "-I", "-B", "-c", f"import sys; sys.path.insert(0, {str(tree)!r});" + script, *args]
        done = subprocess.run(argv, env={**self.env, **env}, cwd=self.root, text=True, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        return done.stdout.strip()

    def stamp_in(self, tree):
        return self.run_child(tree, "from arete import codebase; print(codebase.load())")

    def edit_during_import(self, tree, hook, target, text):
        marker = self.root / f"{tree.name}.edited"
        module = tree / "arete" / f"{hook}.py"
        module.write_text(module.read_text() + (
            f"\nimport os as _os\nif not _os.path.exists({str(marker)!r}):\n    open({str(marker)!r}, 'w').close()\n"
            f"    open({str(tree / 'arete' / target)!r}, 'a').write({text!r})\n"))

    def test_a_child_keeps_the_code_it_started_with(self):
        tree = self.tree()
        entry = control.command([])[3].replace(repr(str(paths.PACKAGE_DIR.parent)), repr(str(tree)))
        entry = entry.split("raise SystemExit")[0] + "print('READY', flush=True); sys.stdin.readline(); "
        entry += "import arete.auditor as a; print('present' if hasattr(a, 'MARK') else 'absent')"
        child = subprocess.Popen([sys.executable, "-I", "-B", "-c", entry], env=self.env, cwd=self.root, text=True,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(child.stdout.readline().strip(), "READY")
            with open(tree / "arete/auditor.py", "a") as f:
                f.write('\nMARK = "new"\n')
            out, err = child.communicate("go\n", timeout=60)
        finally:
            child.kill()
        self.assertEqual((child.returncode, out.strip()), (0, "absent"), err)

    def test_code_that_moves_during_load_loads_again(self):
        first = next(m.name for m in pkgutil.iter_modules([str(support.SOURCE / "arete")]) if not m.ispkg)
        show = "from arete import codebase; codebase.load(); print(os.environ.get('ARETE_CODE_RELOADS'), codebase.STAMP)"
        tree = self.tree()
        self.edit_during_import(tree, first, "auditor.py", "\n# moved\n")
        out = self.run_child(tree, "import os; " + show)
        digest = self.run_child(tree, "from arete import codebase; print(codebase.inventory()[0][:12])")
        self.assertEqual(out, f"1 code.{digest}")

        tree = self.tree("late")
        marker = self.root / "late.edited"
        late = (f"import os, arete.cli; from pathlib import Path; m = Path({str(marker)!r})\n"
                f"if not m.exists():\n    m.touch(); open({str(tree / 'arete/config.py')!r}, 'a').write('\\n# late\\n')\n" + show)
        self.assertTrue(self.run_child(tree, late).startswith("1 code."))

        tree = self.tree("limit")
        self.edit_during_import(tree, first, "auditor.py", "\n# moved\n")
        out = self.run_child(tree, "import os; " + show, ARETE_CODE_RELOADS="5")
        self.assertRegex(out, r"^5 code\.[0-9a-f]{12}\+moving$")

    def test_a_dirty_checkout_gets_its_own_stamp(self):
        tree = self.tree()
        self.assertRegex(self.stamp_in(tree), r"^code\.[0-9a-f]{12}$")

        def git(*args):
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(tree), *args],
                                  env=self.env, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("add", "arete", "adws")
        git("commit", "-q", "-m", "tree")
        head = git("rev-parse", "--short=12", "HEAD")
        self.assertEqual(self.stamp_in(tree), head)
        stamps = []
        for text in ("\n# one\n", "\n# two\n"):
            git("checkout", "-q", "--", "arete/auditor.py")
            with open(tree / "arete/auditor.py", "a") as f:
                f.write(text)
            stamps.append(self.stamp_in(tree))
        self.assertTrue(all(re.fullmatch(head + r"\+dirty\.[0-9a-f]{8}", s) for s in stamps), stamps)
        self.assertNotEqual(*stamps)

    def test_every_record_names_the_code_that_wrote_it(self):
        found = OfflineWorkflowTests.workflow(self, "code-identity")["identity"]
        stamp = found["stamp"]
        self.assertTrue(stamp)
        self.assertEqual((found["row"], found["first"]), (stamp, stamp))
        self.assertEqual(set(found["headers"]), {stamp})
        self.assertEqual([line.get("engine") for line in found["second"]], ["second-engine", None])
        self.assertEqual(found["second"][1]["seq"], found["second"][0]["seq"] + 1)

    def test_a_child_claims_the_row_with_its_own_code(self):
        for claimed in json.loads(self.run_child(support.SOURCE, self.CLAIM)):
            self.assertEqual(claimed, ["b", True])

    def test_serve_and_dashboard_name_their_code(self):
        found = json.loads(self.run_child(support.SOURCE, self.HEADERS))
        self.assertTrue(found["stamp"])
        self.assertEqual((found["beat"], found["file"]), (found["stamp"], found["stamp"]))
        self.assertEqual(found["heads"], [[found["stamp"]]] * 2)

    def test_serve_keeps_its_code_when_new_code_cannot_load_whole(self):
        tree = self.tree()
        with open(tree / "arete/benchmark.py", "a") as f:
            f.write("\ndef broken(:\n")
        notes = json.loads(self.run_child(support.SOURCE, self.BAD_RELOAD, str(tree)))
        self.assertEqual(len(notes), 1)
        self.assertIn("new code does not import", notes[0])
        self.assertIn("arete.benchmark", notes[0])


class ConfigForwardCompatTests(unittest.TestCase):
    PRELUDE = (SCRIPTS / "config_forward_compat_prelude.py").read_text()
    DASHBOARD_BAD_CONFIG = (SCRIPTS / "config_dashboard_bad_config.py").read_text()
    RELOAD_BAD_CONFIG = (SCRIPTS / "config_reload_bad_config.py").read_text()

    def environment(self, root):
        make_folders(root, "config", "state", "logs", "worktrees", "tmp", "home")
        env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                          ARETE_WORKTREES_DIR=str(root / "worktrees"),
                          TMPDIR=str(root / "tmp"), HOME=str(root / "home"),
                          GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                          GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull)
        return env

    def program(self, body):
        return bootstrap(self.PRELUDE + body)

    def case(self, body, timeout=120):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.environment(root)
            done = subprocess.run([sys.executable, "-I", "-c", self.program(body), str(root)],
                                  cwd=root, env=env, text=True, capture_output=True, timeout=timeout)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            return behavior_result(done)

    def test_names_from_a_newer_engine_are_ignored_once(self):
        found = self.case(r'''
write(NEWER)
cfg = config.load()
result(ignored=list(cfg.ignored), max_per_run=cfg.ask.max_per_run, days=cfg.retention.days,
       weights=sorted(cfg.jev.risk_weights), engine=sorted(config.JevConfig().risk_weights),
       strict=refused(NEWER, strict=True))
''')
        self.assertEqual(sorted(found["ignored"]), sorted(["[steps] future_step", "[kinds] chore", "[ask] future_option",
                                                           "[retention] future", "[jev.risk_weights] future_weight"]))
        self.assertEqual((found["max_per_run"], found["days"]), (5, 10))
        self.assertEqual(found["weights"], found["engine"])
        self.assertIn("future_step", found["strict"])

    def test_other_tolerant_readers_and_strict_direct_calls(self):
        found = self.case(r'''
more = GOOD.replace("[replay]\n", "[replay]\nfuture = 1\n") + """
[chat]
future = 1
[research]
future = 1
[document]
future = 1
[kinds.feat]
future = 1
[kinds.feat.steps.future_step]
tries = 1
[jev.screen_lines]
future_question = 0.5
"""
write(more)
cfg = config.load()
direct = {}
for name, call in (("ask", lambda: config.ask_table({"future": 1})), ("steps", lambda: config.step_tables({"future_step": {}}, "steps")),
                   ("known", lambda: config.known({"x": 1}, ("y",), "t", None))):
    try:
        call()
    except ValueError as e:
        direct[name] = str(e)
kept = config.known({"x": 1, "y": 2}, ("y",), "t", (collected := []))
result(ignored=sorted(cfg.ignored), direct=direct, kept=kept, collected=collected,
       invalid=refused(GOOD + "\n[ask]\nfuture = 1\nmax_per_run = -1\n"), screen=sorted(cfg.jev.screen_lines))
''')
        self.assertEqual(found["ignored"], sorted([
            "[replay] future", "[chat] future", "[research] future", "[document] future", "[kinds.feat] future",
            "[kinds.feat.steps] future_step", "[jev.screen_lines] future_question"]))
        self.assertEqual(sorted(found["direct"]), ["ask", "known", "steps"])
        self.assertEqual((found["kept"], found["collected"]), ({"y": 2}, ["[t] x"]))
        self.assertIn("max_per_run", found["invalid"])
        self.assertNotIn("future_question", found["screen"])

    def test_reach_tables_stay_strict(self):
        found = self.case(r'''
cases = {
    "steps.build": "[steps.build]\nfuture_key = 1\n",
    "mcp.x": "[mcp.x]\ncommand = 'x'\nfuture_key = 1\n",
    "tiers": "[tiers.x]\nfast = { future_key = '1' }\n",
    "routing": "[routing]\nfuture_key = 1\n",
    "environment": "[environment]\nfuture_key = 1\n",
    "capture": "[capture]\nfuture_key = 1\n",
    "ab.x": "[ab.x]\nfuture_key = 1\n",
    "oncall": "[oncall]\nfuture_key = 1\n",
    "oncall.source": "[[oncall.source]]\nfuture_key = 1\n",
    "session": "[steps.build]\nsession = 'fork'\n",
    "payment_modes": "[routing]\npayment_modes = ['future_mode']\n",
}
result(errors={name: refused(GOOD + "\n" + text) for name, text in cases.items()})
''')
        for name, error in found["errors"].items():
            self.assertTrue(error, name)

    def test_settings_save_still_refuses_unknown_names(self):
        found = self.case(r'''
write(NEWER)
before = paths.CONFIG_FILE.read_bytes()
try:
    settings.save({"max_parallel": 2})
    refusal = ""
except settings.Refused as e:
    refusal = str(e)
unchanged = paths.CONFIG_FILE.read_bytes() == before
write(GOOD)
settings.save({"max_parallel": 2})
result(refusal=refusal, unchanged=unchanged, saved=config.load().max_parallel)
''')
        self.assertIn("future", found["refusal"])
        self.assertTrue(found["unchanged"])
        self.assertEqual(found["saved"], 2)

    def test_layer_ignores_new_step_names(self):
        found = self.case(r'''
layer = root / "sample/layer"
with (layer / "config.toml").open("a") as out:
    out.write("\n[steps.future_step]\ntries = 1\n\n[kinds.chore]\n")
loaded = config.load_layer(layer)
try:
    config.load_layer(layer, strict=True)
    strict = ""
except ValueError as e:
    strict = str(e)
result(ignored=list(loaded.ignored), strict=strict)
''')
        self.assertEqual(found["ignored"], ["[steps] future_step", "[kinds] chore"])
        self.assertTrue(found["strict"])

    def test_fallback_keeps_dashboard_fields_and_never_raises(self):
        found = self.case(r'''
write(BAD + "\n[dashboard]\nport = 4321\nhttps = true\nowner = 'me@example.com'\n")
cfg, problem = config.load_or_problem()
write("[[[ not toml")
broken, why = config.load_or_problem()
write(GOOD + "\n[dashboard]\nport = 'x'\n")
bad_port, bad_port_why = config.load_or_problem()
write(GOOD + "\n[dashboard]\nport = 'x'\nhttps = true\nowner = 'me@example.com'\n")
mixed = config.load_or_problem()[0]
write(GOOD + "\n[dashboard]\nport = 4321\nhttps = 'yes'\n")
kept_port = config.load_or_problem()[0].dashboard_port
result(mixed=[mixed.dashboard_https, mixed.dashboard_owner], kept_port=kept_port,
       port=cfg.dashboard_port, https=cfg.dashboard_https, owner=cfg.dashboard_owner, problem=problem,
       broken=[broken.dashboard_port == config.DASHBOARD_PORT, why], bad_port=[bad_port.dashboard_port, bad_port_why])
''')
        self.assertEqual((found["port"], found["https"], found["owner"]), (4321, True, "me@example.com"))
        self.assertTrue(found["problem"].startswith("ValueError: [routing]"))
        self.assertTrue(found["broken"][0] and found["broken"][1].startswith("TOMLDecodeError"))
        self.assertTrue(found["bad_port"][1])
        self.assertEqual((found["mixed"], found["kept_port"]), ([True, "me@example.com"], 4321))

    def test_serve_stays_up_on_a_reach_key(self):
        found = self.case(r'''
server, said = quiet_server()
server.tick()
write(BAD)
server.tick()
server.tick()
beat = util.read_json(control.BEAT_FILE, {})
paused = {"said": [s for s in said if "does not load" in s], "problem": beat.get("config_problem", ""),
          "ticks": beat.get("ticks"), "running": state.runs_marked_running()}
write(GOOD)
server.tick()
again = util.read_json(control.BEAT_FILE, {})
write(BAD)
second = serve.Server()
write(NEWER)
server.tick()
server.tick()
result(paused=paused, recovered=[s for s in said if "loads again" in s], again_problem="config_problem" in again,
       again_ticks=again.get("ticks"), second=second.config_problem, ignored=[s for s in said if "ignores them" in s])
''')
        self.assertEqual(len(found["paused"]["said"]), 1)
        self.assertIn("future_key", found["paused"]["problem"])
        self.assertEqual((found["paused"]["ticks"], found["paused"]["running"]), (3, []))
        self.assertEqual(len(found["recovered"]), 1)
        self.assertFalse(found["again_problem"])
        self.assertEqual(found["again_ticks"], 4)
        self.assertIn("future_key", found["second"])
        self.assertEqual(len(found["ignored"]), 1)

    def test_serve_process_lives_through_a_bad_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.environment(root)
            body = "write(BAD)\nserve.run(with_dashboard=False)\n"
            child = subprocess.Popen([sys.executable, "-I", "-u", "-c", self.program(body), str(root)], cwd=root, env=env,
                                     text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                time.sleep(6)
                alive = child.poll() is None
            finally:
                if child.poll() is None:
                    child.terminate()
                output, _ = child.communicate(timeout=30)
        self.assertTrue(alive, output)
        self.assertEqual(output.count("does not load"), 1, output)
        self.assertNotIn("Traceback", output)

    def test_dashboard_answers_while_config_does_not_load(self):
        found = self.case(self.DASHBOARD_BAD_CONFIG, timeout=150)
        self.assertEqual(found["status"], {"/": 200, "/api/health": 200})
        self.assertEqual(found["state"], "fault")
        self.assertIn("future_key", found["problem"])

    def test_new_code_that_cannot_read_config_is_not_started(self):
        found = self.case(self.RELOAD_BAD_CONFIG)
        self.assertEqual((found["refused"]["execs"], found["refused"]["said"]), (0, 1))
        self.assertIn("cannot read", found["refused"]["beat"])
        self.assertEqual(found["refused"]["running"], [])
        self.assertEqual((found["passed"]["execs"], found["passed"]["problem"]), (1, ""))
        self.assertEqual(found["escaped"], 1)
        self.assertEqual(found["reverted"], "")

    def test_old_run_child_survives_a_new_step_name(self):
        baseline = OfflineWorkflowTests.workflow(self, "future-step-baseline")
        edited = OfflineWorkflowTests.workflow(self, "future-step-appended")
        self.assertTrue(edited["appended"])
        self.assertEqual(baseline["status"], "done")
        self.assertEqual((edited["status"], edited["steps"]), (baseline["status"], baseline["steps"]))

    def test_the_serve_line_names_each_pause(self):
        beat = {"alive": True, "pid": 1, "loops": 0, "off": 0, "running": 0, "pending": 0}
        self.assertNotIn("does not load", cli.serve_line(beat))
        line = cli.serve_line({**beat, "config_problem": "ValueError: [routing]: unknown key x",
                               "code_problem": "new code cannot read config.toml"})
        self.assertIn("serve: up", line)
        self.assertIn("config does not load: ValueError: [routing]: unknown key x", line)
        self.assertIn("new code cannot read config.toml", line)


class ServeReloadTests(unittest.TestCase):
    PRELUDE = ConfigForwardCompatTests.PRELUDE + (SCRIPTS / "serve_reload_prelude.py").read_text()
    environment = ConfigForwardCompatTests.environment
    program = ConfigForwardCompatTests.program
    case = ConfigForwardCompatTests.case

    def test_a_post_during_the_import_check_holds_the_restart(self):
        found = self.case((SCRIPTS / "serve_reload_post_during_probe.py").read_text())
        self.assertEqual(found["held"], {"execs": 0, "closed": False, "status": "approved", "error": "", "said": [
            found["held"]["said"][0]]})
        self.assertTrue(found["held"]["said"][0].endswith("new code waits to load: the dashboard is still answering a request"))
        self.assertEqual((found["answer"]["status"], found["answer"]["retry"]), (200, None))
        self.assertIn("sent", json.dumps(found["answer"]["body"]))
        self.assertEqual(found["after"], {"status": "done", "count": 0})
        self.assertIn("OSError", found["second"])
        self.assertEqual(found["execs"], [{"count": 0, "closed": True, "status": "done"}])
        self.assertFalse(found["closed"])
        self.assertFalse(found["inheritable"])

    def test_a_closed_gauge_answers_503_and_keeps_the_approval_for_a_second_try(self):
        found = self.case((SCRIPTS / "serve_reload_closed_gauge.py").read_text())
        self.assertEqual((found["refused"]["status"], found["refused"]["retry"]), (503, "3"))
        self.assertEqual(found["refused"]["body"], {"error": found["text"]})
        self.assertEqual(found["during"], {"status": "pending", "listed": True, "count": 0})
        self.assertEqual((found["foreign"], found["stranger"], found["unmarked"]), (421, 403, 403))
        self.assertEqual(found["lengths"], {"missing": {"status": 503, "retry": True},
                                            "invalid": {"status": 503, "retry": True}})
        self.assertEqual(found["through"], {"owner": 503, "stranger": 403})
        self.assertEqual(found["reading"], 200)
        self.assertEqual((found["again"]["status"], found["again"]["retry"]), (200, None))
        self.assertEqual((found["status"], found["count"]), ("done", 0))

    def test_a_restart_that_does_not_happen_leaves_posts_open(self):
        found = self.case((SCRIPTS / "serve_reload_failed_restart.py").read_text(), timeout=180)
        self.assertEqual(found["import"]["seen"], [False])
        self.assertIn("new code does not import", found["import"]["said"])
        self.assertIn("new code cannot read config.toml", found["config"]["said"])
        self.assertEqual(set(found["config"]["seen"]), {False})
        self.assertIn("OSError", found["execv"]["error"])
        self.assertIn("RuntimeError", found["busy"]["error"])
        self.assertIn("RuntimeError", found["children"]["error"])
        self.assertIn("a chat message is still running", found["chat"]["said"])
        self.assertIn("a feedback request is still being checked or filed", found["capture"]["said"])
        for name in ("import", "config", "busy", "children", "chat", "capture"):
            self.assertEqual(found[name]["execs"], 0, name)
        self.assertEqual(found["execv"]["execs"], 1)
        for name, one in found.items():
            self.assertEqual((one["closed"], one["works"]), (False, 200), name)

    def test_the_client_sends_a_restart_503_again_and_fails_everything_else_at_once(self):
        found = self.case((SCRIPTS / "serve_reload_client_retry.py").read_text(), timeout=180)
        self.assertEqual((found["again"]["answer"], found["again"]["requests"], found["again"]["same"]), ({"ok": True}, 2, True))
        self.assertEqual((found["slow"]["answer"], found["slow"]["requests"]), ({"ok": True}, 2))
        self.assertGreaterEqual(found["slow"]["elapsed"], 0.9)
        for name, code in (("none", 503), ("invalid", 503), ("other", 409)):
            self.assertEqual((found[name]["requests"], found[name]["failed"]["code"]), (1, code), name)
        self.assertEqual(found["other"]["failed"]["text"], "Busy.")
        self.assertEqual(found["none"]["failed"]["text"], "Arete is loading new code. Send this again in a few seconds.")
        self.assertEqual(found["repeated"]["failed"], {"code": 503, "text": "Arete is loading new code. Send this again in a few seconds."})
        self.assertEqual((found["repeated"]["requests"], found["repeated"]["slept"]), (3, [10, 10, 10]))
        self.assertEqual((found["expiry"]["requests"], found["expiry"]["slept"]), (3, [10, 10, 10]))
        self.assertEqual(found["expiry"]["failed"]["code"], 503)
        self.assertNotIn("answer", found["expiry"])
        self.assertEqual((found["dropped"]["answer"], found["dropped"]["slept"]), ({"ok": True}, [1, 2]))
        self.assertEqual(found["never"]["failed"]["code"], 503)
        self.assertEqual(sum(found["never"]["slept"]), 30)
        self.assertEqual((found["initial"], found["initial_sleeps"]), ("down", []))


if __name__ == "__main__":
    unittest.main()
