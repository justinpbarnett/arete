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
import importlib.util, json, os, runpy, sys, urllib.request
from pathlib import Path
from unittest import mock
from arete import accounts, config, factory, gh, paths, pipeline, record, replay, runlimits, runner, state
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
def forced():
    try:
        factory.main(feature, ["replay-sample", task, "--force"])
    except SystemExit:
        pass
    return state.ticket_runs("replay-sample", task, whole=True)[0]
if scenario.startswith("held-scratch"):
    layer, answer = root / "sample/layer/config.toml", root / "sample/replay/verify.json"
    plain, passing = layer.read_text(), answer.read_text()
    prefix = paths.SCRATCH_PREFIX + "0123456789abcdef"
    holding = f"mkdir -p {prefix} && echo '*' > {prefix}/.gitignore && " + replay.SAMPLE_CHECK
    commit = "git -c user.name=fixture -c user.email=fixture@localhost -c commit.gpgsign=false commit -q --allow-empty -m local"
    layer.write_text(plain.replace(json.dumps(replay.SAMPLE_CHECK), json.dumps(holding)) +
                     "\n[setup]\ncommands = [" + json.dumps(commit) + "]\n")
    answer.write_text(json.dumps(json.loads(passing) | {"seconds": 30}))
    with mock.patch.object(runlimits.Policy, "of", return_value=runlimits.Policy(max_verify_minutes=.05)):
        parked = forced()
    layer.write_text(plain)
    answer.write_text(passing)
    ws = paths.WORKTREES_DIR / f"replay-sample-{task}"
    saved = pipeline.made_record(ws).read_text()
    made = json.loads(saved)
    result = {"parked": parked["summary"], "recorded": [k for k in made if not k.startswith("\0")],
              "head_recorded": made.get(pipeline.head_key(pipeline.Checkout("", "", ws, ws, "main"))) == pipeline.git(["rev-parse", "HEAD"], ws).strip(),
              "unpushed": pipeline.git(["rev-list", "--count", "HEAD", "--not", "--remotes=origin"], ws).strip(),
              "held": [p.name for p in ws.glob(paths.SCRATCH_PREFIX + "*")]}
    with state.db() as c:
        c.execute("UPDATE tasks SET body = ? WHERE id = ?", ("Add kumquat on a last line of its own", task))
    refused = forced()
    result["while_held"] = refused["summary"]
    result["record_kept"] = pipeline.made_record(ws).read_text() == saved
    for fd in list(scratch.LEASES.values()):
        os.close(fd)
    scratch.LEASES.clear()
    with mock.patch.object(scratch, "producers_done", return_value=True):
        pipeline.retry_scratch()
    (root / "finalized").mkdir()
    for p in ws.glob(paths.SCRATCH_PREFIX + "*"):
        p.rename(root / "finalized" / p.name)
    if scenario == "held-scratch-edited":
        with (ws / "notes.txt").open("a") as out:
            out.write("edited by hand\n")
    row = forced()
    events = [json.loads(line) for line in (paths.RUNS_DIR / row["id"] / "run.jsonl").read_text().splitlines()]
    result |= {"status": row["status"], "summary": row["summary"],
               "prepared": [e.get("result") for e in events if e.get("event") == "step_end" and e.get("step") == "prepare"],
               "origin": pipeline.git(["for-each-ref", "--format=%(refname)", "refs/heads"], root / "sample/origin.git"),
               "edited": (ws / "notes.txt").exists() and "edited by hand" in (ws / "notes.txt").read_text()}
    branches = [b for b in result["origin"].splitlines() if b != "refs/heads/main"]
    result["published"] = pipeline.git(["show", branches[0] + ":notes.txt"], root / "sample/origin.git") if len(branches) == 1 else ""
    print("BEHAVIOR_RESULT=" + json.dumps(result))
    raise SystemExit(0)
row = forced()
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
                                    cwd=root, env=env, text=True, capture_output=True, timeout=120)
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

    def test_run_parked_with_scratch_held_records_its_work_without_scratch(self):
        result = self.workflow("held-scratch")
        self.assertIn("parked", result["parked"])
        self.assertIn("notes.txt", result["recorded"])
        self.assertFalse([n for n in result["recorded"] if ".arete-scratch-" in n])
        self.assertTrue(result["held"])
        self.assertTrue(result["head_recorded"])
        self.assertEqual(result["unpushed"], "1")
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


@unittest.skipUnless(sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK) and os.getuid() != 0,
                     "needs macOS confinement")
class VerificationOutputTests(unittest.TestCase):
    def setUp(self):
        import ctypes
        import struct
        self.ctypes, self.struct = ctypes, struct
        from arete import scratch
        self.scratch = scratch
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        if scratch.filesystem(os.open(self.root, os.O_RDONLY)) != "apfs":
            self.skipTest("needs APFS")
        (self.root / "home").mkdir()
        for patch in (mock.patch.object(scratch, "AUTHORITY", self.root / "home/verification-authority"),
                      mock.patch.object(scratch, "producers_done", return_value=True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.ws = self.root / "worktrees/ticket"
        (self.ws / "owner-dir").mkdir(parents=True)
        (self.ws / "owner.txt").write_text("owner data\n")
        (self.ws / "owner-dir/kept.txt").write_text("kept\n")
        self.made = []
        self.addCleanup(self.release)

    def release(self):
        for a in self.made:
            fd = self.scratch.LEASES.pop(a.selector, None)
            if fd is not None:
                os.close(fd)
            self.scratch.RUN_BASELINES.pop(a.run_id, None)

    def attempt(self):
        run_id = f"run-{len(self.made)}"
        run_dir = self.root / "runs" / run_id
        run_dir.mkdir(parents=True)
        self.scratch.RUN_BASELINES[run_id] = {}
        a = self.scratch.make(run_id, "verify", self.ws, run_dir)
        self.made.append(a)
        return a

    def record(self, a):
        return next(r for r in self.scratch.records() if r["attempt"]["selector"] == a.selector)

    def write(self, a, name, text="{}"):
        target = Path(a.root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def test_created_entries_are_archived_and_workspace_is_free(self):
        a = self.attempt()
        for name in ("config/predictions.jsonl", "fixture-a/nested/case.json", "out/scenario-1/result.json"):
            self.write(a, name)
        os.symlink(self.ws / "owner.txt", Path(a.root) / "fixture-a/link-to-file")
        os.symlink(self.ws / "owner-dir", Path(a.root) / "fixture-a/link-to-dir")
        self.assertEqual(self.scratch.end(a, Path(a.run_dir))[2], [])
        data = self.record(a)
        self.assertEqual((data["state"], data["diagnostic"]), ("done", ""))
        tree = self.scratch.AUTHORITY / a.selector / data["final_tree"]
        for name in ("config/predictions.jsonl", "fixture-a/nested/case.json", "out/scenario-1/result.json"):
            self.assertTrue((tree / name).exists())
        self.assertTrue((tree / "fixture-a/link-to-file").is_symlink())
        self.assertTrue((tree / "fixture-a/link-to-dir").is_symlink())
        self.assertEqual((self.ws / "owner.txt").read_text(), "owner data\n")
        self.assertEqual((self.ws / "owner-dir/kept.txt").read_text(), "kept\n")
        self.assertFalse(self.scratch.pending(self.ws))
        self.assertEqual(pipeline.scratch_left(self.ws, []), [])
        self.attempt()

    def retained(self, a, name):
        self.assertEqual(self.scratch.end(a, Path(a.run_dir))[2], [a.root])
        data = self.record(a)
        self.assertEqual(data["state"], "pending")
        return data["diagnostic"]

    def test_owner_file_moved_to_root_is_retained(self):
        a = self.attempt()
        os.rename(self.ws / "owner.txt", Path(a.root) / "owner.txt")
        diagnostic = self.retained(a, "owner.txt")
        self.assertEqual(diagnostic["unknown_entries"], ["owner.txt"])
        self.assertEqual(diagnostic["reasons"]["owner.txt"], self.scratch.EXISTED)
        self.assertEqual((Path(a.root) / "owner.txt").read_text(), "owner data\n")
        self.assertTrue(pipeline.scratch_left(self.ws, []))
        with self.assertRaises(OSError) as raised:
            self.attempt()
        self.assertEqual(raised.exception.errno, 16)

    def test_owner_file_in_created_folder_is_named(self):
        a = self.attempt()
        self.write(a, "fixture-a/nested/case.json")
        os.rename(self.ws / "owner.txt", Path(a.root) / "fixture-a/nested/owner.txt")
        self.assertEqual(self.retained(a, "")["unknown_entries"], ["fixture-a/nested/owner.txt"])

    def test_hard_link_is_retained(self):
        a = self.attempt()
        os.link(self.ws / "owner.txt", Path(a.root) / "config/linked.txt")
        diagnostic = self.retained(a, "")
        self.assertEqual(diagnostic["reasons"], {"config/linked.txt": self.scratch.LINKED})

    def test_forged_birth_time_is_retained(self):
        a = self.attempt()
        target = Path(a.root) / "fixture-a/owner.txt"
        target.parent.mkdir()
        os.rename(self.ws / "owner.txt", target)
        later = int(time.time()) + 3600
        libc = self.ctypes.CDLL(None, use_errno=True)
        libc.setattrlist.argtypes = (self.ctypes.c_char_p, self.ctypes.c_char_p, self.ctypes.c_char_p,
                                     self.ctypes.c_size_t, self.ctypes.c_uint)
        attrs = self.struct.pack("HHIIIII", 5, 0, 0x200, 0, 0, 0, 0)
        self.assertEqual(libc.setattrlist(os.fsencode(target), attrs, self.struct.pack("ll", later, 0), 16, 0), 0)
        self.assertEqual(int(os.lstat(target).st_birthtime), later)
        self.assertEqual(self.retained(a, "")["unknown_entries"], ["fixture-a/owner.txt"])

    def test_record_from_before_the_change_finalizes_on_poll(self):
        a = self.attempt()
        self.write(a, "config/predictions.jsonl")
        self.write(a, "fixture-a/case.json")
        with self.scratch.registry() as (fd, key):
            data = self.scratch.load(fd, key, a.selector)
            data["state"] = "pending"
            data["diagnostic"] = {"retained_root": a.root, "unknown_entries": ["config/predictions.jsonl", "fixture-a"]}
            self.scratch.store(fd, key, data)
        os.close(self.scratch.LEASES.pop(a.selector))
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pipeline.retry_scratch()
        self.assertIn(a.root, out.getvalue())
        data = self.record(a)
        self.assertEqual(data["state"], "done")
        self.assertTrue((self.scratch.AUTHORITY / a.selector / data["final_tree"] / "fixture-a/case.json").exists())

    def test_unclassifiable_filesystem_is_retained(self):
        a = self.attempt()
        self.write(a, "config/new.json")
        with mock.patch.object(self.scratch, "filesystem", return_value="hfs"):
            diagnostic = self.retained(a, "")
        self.assertTrue(diagnostic["reasons"]["config/new.json"].startswith(self.scratch.UNCLASSIFIED))
        st = SimpleNamespace(st_dev=2, st_ino=9, st_nlink=1, st_mode=0o100600)
        self.assertEqual(self.scratch.refusal(st, 1, 5), self.scratch.ELSEWHERE)

    def test_layout_is_still_protected(self):
        self.assertEqual(self.scratch.end(self.attempt(), Path(self.made[0].run_dir))[2], [])
        a = self.attempt()
        os.unlink(Path(a.root) / "out/output.txt")
        self.assertEqual(self.scratch.end(a, Path(a.run_dir))[2], [a.root])
        self.assertEqual(self.record(a)["state"], "pending")
        self.assertTrue(Path(a.root).exists())


if __name__ == "__main__":
    unittest.main()
