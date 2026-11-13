import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import arete
from arete import config, pipeline, verify

SOURCE = Path(arete.__file__).resolve().parent.parent
PYTHON = shlex.quote(sys.executable)


class VerificationPreflightTests(unittest.TestCase):
    def test_missing_requirement_blocks_before_model_or_app_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, saved = mock.Mock(side_effect=AssertionError("model must not start")), {}
            run = SimpleNamespace(dir=root, repo=None, agent=agent, log=lambda _: None,
                                  save=lambda name, text: saved.__setitem__(name, text))
            v = config.Verify(start="must-not-start", preflight={"runtime": f"{PYTHON} -c 'raise SystemExit(23)'"})
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
            v = config.Verify(preflight={"slow": f"exec {PYTHON} -c 'import time; time.sleep(10)'",
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


class OfflineWorkflowTests(unittest.TestCase):
    def workflow(self, scenario):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("config", "state", "logs", "worktrees", "tmp", "authority-parent"):
                (root / folder).mkdir()
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR")}
            env.update(ARETE_CONFIG_DIR=str(root / "config"), ARETE_STATE_DIR=str(root / "state"),
                       ARETE_LOG_DIR=str(root / "logs"), ARETE_WORKTREES_DIR=str(root / "worktrees"),
                       TMPDIR=str(root / "tmp"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull)
            script = r'''
import importlib.util, json, runpy, sys, urllib.request
from pathlib import Path
from unittest import mock
from arete import accounts, config, factory, gh, paths, pipeline, record, replay, runner, state
root, scenario = Path(sys.argv[1]), sys.argv[2]
def forbidden(*args, **kwargs):
    raise AssertionError("offline workflow attempted an external transport or credential lookup")
urllib.request.urlopen = forbidden
accounts.read = forbidden
gh.gh = forbidden
if importlib.util.find_spec("arete.scratch"):
    from arete import scratch
    probe = [sys.executable, "-V"]
    if scratch.protected_command(probe) != probe:
        scratch.AUTHORITY = root / "authority-parent/verification-authority"
lines = replay.sample(root / "sample", adw=True)
paths.CONFIG_FILE.write_text("max_parallel=1\n\n" + "\n".join(lines) +
    "\n\n[jev]\nenabled=false\n\n[steps.check_repair]\ntries=1\n\n[steps.review]\ntries=" +
    ("2" if scenario == "edited-after-held-pr" else "1") + "\n")
if scenario == "failed-check":
    with (root / "sample/layer/config.toml").open("a") as out:
        out.write('\n[steps.check_repair]\ntries=1\n')
    layer = root / "sample/layer/config.toml"
    layer.write_text(layer.read_text().replace("grep -q kumquat notes.txt", "exit 7"))
elif scenario == "missing-verification":
    (root / "sample/replay/verify.json").unlink()
elif scenario in ("review-rejected", "edited-after-held-pr"):
    file = root / "sample/replay/review.json"
    data = json.loads(file.read_text())
    if scenario == "edited-after-held-pr":
        (root / "sample/replay/review2.json").write_text(file.read_text())
        (root / "sample/replay/verify2.json").write_text((root / "sample/replay/verify.json").read_text())
        (root / "sample/replay/review_fix.json").write_text(json.dumps({"text": "Fixed.", "ok": True}))
    data["structured"] = {"verdict":"request_changes", "summary":"A real behavior is still wrong.",
                           "problems":[{"file":"notes.txt", "problem":"fixture blocker", "severity":"major"}]}
    file.write_text(json.dumps(data))
cfg = config.load()
task = state.task_add("replay-sample", "Add kumquat to the fixture notes", "Add kumquat", "feat")
if scenario == "edited-after-held-pr":
    from arete import lines
    old = record.start("pipeline", repo="replay-sample", issue=task)
    lines.write(old, "step_end", step="prepare", result="pass",
                data={"ticket": {"title": "Add kumquat", "body": "Add kumquat to the fixture notes"}})
    for n in (1, 2, 3):
        lines.write(old, "step_start", step="implement", call=f"review_fix{n}", atom="review_repair", **{"try": n})
    record.settle(old, "pr")
    with state.db() as connection:
        connection.execute("UPDATE tasks SET body = ? WHERE id = ?",
                           ("Add kumquat to the fixture notes\n\n## Follow up\nAlso list it first.", task))
feature = runpy.run_path(str(paths.ADWS_DIR / "feature.py"))["feature"]
try:
    factory.main(feature, ["replay-sample", task, "--force"])
except SystemExit:
    pass
row = state.ticket_runs("replay-sample", task, whole=True)[0]
events = [json.loads(line) for line in (paths.RUNS_DIR / row["id"] / "run.jsonl").read_text().splitlines()]
verification = [e.get("data", {}).get("verified") for e in events
                if e.get("event") == "step_end" and e.get("step") == "verify"]
result = {"status":row["status"], "summary":row["summary"], "steps":[s["step"] for s in state.steps(row["id"])],
          "verified":[v["verdict"] for v in verification if v],
          "origin":pipeline.git(["for-each-ref", "--format=%(refname)", "refs/heads"], root / "sample/origin.git")}
if scenario == "edited-after-held-pr":
    result["old"] = old
    result["log"] = (paths.RUNS_DIR / row["id"] / "run.log").read_text()
    result["budget"] = json.loads((paths.RUNS_DIR / row["id"] / "budget.json").read_text())
if scenario == "pass":
    branches = [b for b in result["origin"].splitlines() if b != "refs/heads/main"]
    assert len(branches) == 1, result
    result["published"] = pipeline.git(["show", branches[0] + ":notes.txt"], root / "sample/origin.git")
print("BEHAVIOR_RESULT=" + json.dumps(result))
'''
            bootstrap = f"import sys;sys.path.insert(0,{str(SOURCE)!r});" + script
            result = subprocess.run([sys.executable, "-I", "-c", bootstrap, str(root), scenario],
                                    cwd=root, env=env, text=True, capture_output=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            output = next((line.removeprefix("BEHAVIOR_RESULT=") for line in result.stdout.splitlines()
                           if line.startswith("BEHAVIOR_RESULT=")), None)
            self.assertIsNotNone(output, result.stdout + result.stderr)
            return json.loads(output)

    def test_complete_local_workflow_publishes_verified_change(self):
        result = self.workflow("pass")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["verified"], ["pass"])
        self.assertIn("kumquat", result["published"])

    def test_behavioral_check_failure_stops_before_verification_or_publication(self):
        result = self.workflow("failed-check")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("verify", result["steps"])
        self.assertEqual(result["origin"].strip(), "refs/heads/main")

    def test_missing_verification_cannot_become_done(self):
        result = self.workflow("missing-verification")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["verified"], ["blocked"])
        self.assertIn("blocked", result["summary"])

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


@unittest.skipUnless(importlib.util.find_spec("arete.document"), "writer is not installed on this branch")
class WriterRegressionTests(unittest.TestCase):
    def write(self, brief, answer, *, voice=""):
        from arete import document, outbound
        from arete.agent import Result
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "voice.txt"
            sample.write_text(voice)
            cfg = config.Config(document={"enabled": True, "voice": (str(sample),) if voice else ()},
                                steps={"document": {"tries": 1}})
            with mock.patch.object(document.runner, "run", return_value=Result(ok=True, structured={"text": answer})), \
                    mock.patch.multiple(document.paths, CONFIG_DIR=root, CONFIG_FILE=root / "config.toml"), \
                    mock.patch.object(outbound, "own_hosts", []):
                return document.write(brief, cfg)

    def test_formatted_warning_and_section_bodies_survive_concise_edits(self):
        from arete import document
        sections = (("**Warning:**", "This command permanently deletes staging records."),
                    ("## Prerequisites", "- Stop the background writer.\n- Back up the fixture database."),
                    ("Warning\n-------", "This command permanently deletes staging records."),
                    ("**Expected output:**", "The terminal prints exactly `ready: true`."))
        for heading, contents in sections:
            with self.subTest(heading=heading):
                draft = f"# Recovery\n\n{heading}\n\n{contents}\n\n## Next step\n\nRun `arete steps`."
                candidate = draft.replace(contents, "")
                brief = document.Brief("doc", ("Explain the recovery command.",), "Shorten the guide.", draft=draft)
                result = self.write(brief, candidate)
                self.assertEqual(result.source, "draft")
                self.assertEqual(result.text, draft)
                self.assertTrue(result.faults)

    def test_code_spans_indentation_and_complete_commands_survive(self):
        from arete import document
        cases = (("Run `arete steps`.", "Run `arete steps --unsupported`."),
                 ("```python\nif ready:\n    run()\n```", "```python\nif ready:\nrun()\n```"),
                 ("Example:\n\n    if ready:\n        run()", "Example:\n\n    if ready:\n    run()"),
                 ("Use ``echo `date` ``.", "Use ``echo `date` --unsupported``."))
        for draft, candidate in cases:
            with self.subTest(draft=draft):
                brief = document.Brief("doc", ("Keep the executable example.",), "Shorten the guide.", draft=draft)
                result = self.write(brief, candidate)
                self.assertEqual(result.source, "draft")
                self.assertEqual(result.text, draft)

    def test_code_can_move_to_a_fence_without_being_rejected(self):
        from arete import document
        brief = document.Brief("doc", ("Run `arete steps`.",), "Give the command.")
        result = self.write(brief, "```sh\narete steps\n```")
        self.assertEqual(result.source, "writer")
        self.assertFalse(result.faults)

    def test_private_voice_examples_do_not_block_original_factual_prose(self):
        from arete import document
        brief = document.Brief("pr", ("Offline checks are enabled.",), "Summarize the change.")
        result = self.write(brief, "Enabled offline checks.", voice="Juniper lanterns guide our release ceremony.")
        self.assertEqual(result.source, "writer")
        self.assertFalse(result.faults)

    def test_supplied_owner_and_deadline_require_complete_values(self):
        from arete import document
        brief = document.Brief("message", ("Offline checks passed.",), "Ask for review.", ask="Please review",
                               owner="Ann", deadline="May 1", draft="Please review, Ann, by May 1.")
        result = self.write(brief, "Please review, Joanne, by May 10. Offline checks passed.")
        self.assertEqual(result.source, "draft")
        self.assertEqual(result.text, brief.draft)

    def test_private_voice_copy_cannot_reach_github_even_with_punctuation_changed(self):
        from arete import document, gh, outbound, record, steps, tracker
        from arete.agent import Result
        sample = "Juniper lanterns guide our midnight release ceremony."
        for candidate in (sample.rstrip("."), sample.lower().replace(".", "!")):
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as directory:
                root, sent = Path(directory), []
                voice = root / "voice.txt"
                voice.write_text(sample)
                cfg = config.Config(document={"enabled": True, "voice": (str(voice),)},
                                    steps={"document": {"tries": 1}})
                setting = steps.resolve(cfg, None, "document")
                repo = config.Repo(name="fixture", path=root, public=True)
                run = SimpleNamespace(id="fixture-privacy", dir=root, repo=repo, feedback=False, repairs="",
                                      settings=lambda _: setting, log=lambda _: None, save=lambda *_: None,
                                      agent=lambda *_, **kw: Result(ok=True, structured={"text": candidate}))
                ticket = tracker.Ticket("1", "#1", "Enable offline checks", "Run the checks offline.")
                verified = verify.Verified("pass", "The offline check passed.",
                                           [{"what":"offline check", "how":"fixture", "saw":"pass", "passed":True}])
                review = {"verdict":"approve", "summary":"Offline behavior is correct.", "problems":[]}
                def transport(args, **kw):
                    sent.append(kw.get("input", ""))
                    return "https://github.com/fixture/repo/pull/1"
                with mock.patch.multiple(document.paths, CONFIG_DIR=root, CONFIG_FILE=root / "config.toml"), \
                        mock.patch.object(outbound, "own_hosts", []), \
                        mock.patch.object(record, "metered_spent", return_value=0), \
                        mock.patch.object(gh, "gh", side_effect=transport):
                    text = document.pr_text(run, ticket, "Enabled offline checks.", review, verified,
                                            "1 file, 1 line", "", cfg, None)
                    body = pipeline.report_body(run, summary="Enabled offline checks.", review=review, verified=verified,
                                                left="", images={}, approval=None, attempts=1, checks="offline fixture",
                                                plan_text="Enable offline checks.", size="1 file, 1 line", written=text)
                    gh.pr_create("fixture/repo", "fixture", "main", "Enable offline checks", body)
                self.assertEqual(len(sent), 1)
                self.assertNotIn(candidate, sent[0])
                self.assertIn("Enabled offline checks.", sent[0])


if __name__ == "__main__":
    unittest.main()
