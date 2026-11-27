import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support

from arete import config, delivery, emits, gh, jira, loop_run, loops, miner, paths, sources, spec_retro, util
from arete.store import insights, runs as run_store

setUpModule = support.isolate_live_state


class SpecRetroTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = config.Repo("product", self.folder, "org/product", pipeline=True)
        self.factory = config.Repo("arete", self.folder, "org/arete", pipeline=True, public=True)
        self.cfg = config.Config(repos=[self.repo, self.factory])
        self.loop = loops.load(loops.BUNDLED_LOOPS_DIR / "mine", "arete", "", self.cfg)
        self.run = loop_run.LoopRun(self.loop, "retro-test", self.cfg)
        self.event = spec_retro.closed_event("mine#github-specs", self.repo, "12", "SSO onboarding", "Private spec",
                                             "https://github.com/org/product/issues/12", "2026-11-24T12:00:00Z")

    def test_real_source_queue_baselines_then_claims_closure_once_across_repoll_and_reopen(self):
        def page(number, parent=None, owner="owner"):
            return {"number": number, "parent": parent, "author": {"login": owner}, "title": "private spec",
                    "url": f"https://github.com/org/product/issues/{number}", "body": "body",
                    "closedAt": "2026-11-24T12:00:00Z"}

        self.cfg.repos = [self.repo]
        notes = [page(1)]
        response = [{"data": {"repository": {"issues": {"nodes": notes}}}}]
        with (mock.patch.object(gh, "me", return_value="Owner"),
              mock.patch.object(gh, "gh_json", return_value=response)):
            self.assertEqual(sources.poll(self.loop, 0, self.cfg), ([], []))
            notes.extend([page(2), page(3, parent={"id": "12"}), page(4, owner="contributor")])
            events, items = sources.poll(self.loop, 0, self.cfg)
            self.assertEqual([e["key"] for e in events], ["org/product#2"])
            self.assertEqual(len(items), 1)
            notes.clear()
            self.assertEqual(sources.poll(self.loop, 0, self.cfg), ([], []))
            notes.extend([page(1), page(2)])
            reloaded = loops.load(self.loop.dir, "arete", "", self.cfg)
            self.assertEqual(sources.poll(reloaded, 0, self.cfg), ([], []))

    def test_jira_done_parent_requires_owner_and_routes_within_shared_project(self):
        first = config.Repo("app", self.folder, tracker="jira", pipeline=True, jira_project="PROJ")
        other = config.Repo("api", self.folder, tracker="jira", pipeline=True, jira_project="PROJ")
        client = mock.Mock(spec=jira.Client)
        fields = {"summary": "title", "description": "specific", "issuetype": {"subtask": False},
                  "labels": ["repo-api", "spec"]}
        client.search.return_value = [{"key": "PROJ-9", "fields": fields},
                                      {"key": "PROJ-10", "fields": {**fields, "parent": {"key": "PROJ-9"}}},
                                      {"key": "PROJ-11", "fields": {**fields, "issuetype": {"subtask": True}}},
                                      {"key": "PROJ-12", "fields": {**fields, "labels": ["spec"]}}]
        client.issue.return_value = {"fields": {"labels": ["spec"]}}
        client.browse_url.side_effect = lambda ref: f"https://jira.test/browse/{ref}"
        with mock.patch("arete.tracker.JiraTracker.client", new_callable=mock.PropertyMock, return_value=client):
            events = list(spec_retro.jira_specs([first, other], "mine#jira-specs", self.cfg))
        self.assertEqual([(e["repo"], e["key"]) for e in events], [("api", "PROJ-9")])
        query = client.search.call_args.args[0]
        self.assertIn('labels = "spec"', query)
        self.assertIn("creator = currentUser()", query)
        self.assertIn("statusCategory = Done", query)

    def test_cross_repo_children_contribute_counts_without_sibling_private_text(self):
        other = config.Repo("api", self.folder, "org/api", pipeline=True)
        self.cfg.repos.append(other)
        def records(name, ref, **kwargs):
            return [{"id": f"{name}-{ref}", "finished": "now"}]
        def read(rid):
            return {"id": rid, "issue": "12", "signals": [], "repo": rid.split("-")[0],
                    "calls": [{"step": "verify", "ok": False}], "failed_steps": []}
        with (mock.patch.object(gh, "api_list", return_value=[
                  {"number": 15, "repository_url": "https://api.github.com/repos/org/api"},
                  {"number": 16, "repository_url": "https://api.github.com/repos/unknown/org"}]),
              mock.patch.object(run_store, "ticket_runs", side_effect=records) as lookup,
              mock.patch.object(spec_retro.record, "read_run", side_effect=read),
              mock.patch.object(miner, "signals_of", return_value=["failed"]),
              mock.patch.object(miner, "run_digest", side_effect=lambda run: {"items": [f"observation {run['id']}"]}),
              mock.patch.object(insights, "ticket_feedback", side_effect=lambda name, ref: [{"body": f"{name} words"}]),
              mock.patch.object(miner, "render_feedback", side_effect=lambda reviews: (reviews[0]["body"], {}))):
            rows, totals, text, omitted = spec_retro.observations(self.cfg, self.repo, self.event)
        self.assertEqual([a.args for a in lookup.call_args_list], [("product", "12"), ("api", "15")])
        self.assertEqual(len(rows), 2)
        self.assertEqual(totals["owner_review_comments"], 2)
        self.assertEqual(totals["runs_with_failed_stage"], {"verify": 2})
        self.assertEqual(omitted, 0)
        self.assertIn("product words", text)
        self.assertIn("observation product-12", text)
        self.assertNotIn("api words", text)
        self.assertNotIn("api-15", text)

    def test_public_and_private_product_review_both_receive_digest_factory_receives_only_typed_facts(self):
        self.event["data"]["description"] = "private project name"
        prompts = {}
        def respond(run, role, prompt, cwd, **kwargs):
            prompts[role] = prompt
            self.assertEqual(kwargs["tools"], [])
            self.assertEqual(kwargs["instructions"], [])
            self.assertEqual(cwd.name, role.removeprefix("retro-"))
            return SimpleNamespace(ok=True, structured={"findings": [
                {"repo": "rogue", "title": "Improve delivery", "summary": "Check the generated artifact",
                 "fingerprint": "artifact-validation", "evidence": ["invented link"]}]})
        observations = ([{"session": "example", "issue": "12"}],
                        {"signals": {"retry": 2}, "finished_runs": 2, "runs_with_failed_stage": {"verify": 2},
                         "owner_review_comments": 0}, "owner observed a timeout", 0)
        for public in (True, False):
            with (self.subTest(public=public), mock.patch.object(gh, "repo_private", return_value=not public),
                  mock.patch.object(spec_retro, "observations", return_value=observations),
                  mock.patch.object(loop_run.LoopRun, "agent", autospec=True, side_effect=respond)):
                findings = spec_retro.assess(self.run, self.event, self.repo, self.folder, {})
            self.assertIn("owner observed a timeout", prompts["retro-product"])
            self.assertIn("private project name", prompts["retro-product"])
            self.assertNotIn("private project name", prompts["retro-factory"])
            self.assertNotIn("owner observed a timeout", prompts["retro-factory"])
            self.assertIn('"verify": 2', prompts["retro-factory"])
            self.assertEqual([row["repo"] for row in findings], ["product", "arete"])
            self.assertEqual(findings[0]["evidence"], [self.event["url"]])
            self.assertNotIn("evidence", findings[1])

    def test_watch_uses_actual_run_and_feedback_and_emits_once_across_retries(self):
        # Tracker reads and model responses are fixtures; discovery, SQL lookups and loop dedup use real code.
        run_store.insert_run("sample-delivery", {"kind": "pipeline", "repo": self.repo.name, "issue": 12,
                                                 "status": "merged", "summary": "Repeated rendering in both views",
                                                 "started": "2026-11-23T14:00:00Z", "finished": "2026-11-23T14:30:00Z"})
        insights.add_feedback([{"repo": self.repo.name, "issue": "12", "pr_url": "https://github.com/org/product/pull/18",
                                "kind": "review", "gh_id": 18, "body": "Both views should render the same way",
                                "created": "2026-11-23T15:00:00Z", "pr_state": "MERGED", "status": "recorded"}])
        prompts = []
        def reply(run, role, prompt, folder, **kwargs):
            prompts.append((role, prompt))
            found = ([{"title": "Share the rendering logic", "summary": "Run both view checks",
                       "fingerprint": "render-logic"}] if role == "retro-product" else [])
            return SimpleNamespace(ok=True, structured={"findings": found})

        self.run.events = [self.event]
        with (mock.patch.object(gh, "api_list", return_value=[]), mock.patch.object(gh, "repo_private", return_value=True),
              mock.patch.object(util, "lock", side_effect=lambda *a, **k: nullcontext()),
              mock.patch.object(loop_run.LoopRun, "agent", autospec=True, side_effect=reply),
              mock.patch.object(loop_run.LoopRun, "log"), mock.patch.object(loop_run.delivery, "deliver") as emit):
            self.assertEqual(loop_run.watch_loop(self.run)[0], "done")
            self.run.id = "restarted-retro"
            self.assertEqual(loop_run.watch_loop(self.run)[0], "done")
        self.assertEqual([role for role, _ in prompts], ["retro-product", "retro-factory"])
        self.assertIn("Repeated rendering in both views", prompts[0][1])
        self.assertIn("Both views should render the same way", prompts[0][1])
        self.assertNotIn("Both views", prompts[1][1])
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[2][0]["repo"], self.repo.name)

    def test_reviewed_jira_target_cannot_change_before_queue_render(self):
        self.factory.tracker = "jira"
        self.factory.jira_project = "SAME"
        finding = {"title": "One", "summary": "A change", "repo": "arete", "fingerprint": "arete/one"}
        with (mock.patch.object(paths, "STATE_DIR", self.folder),
              mock.patch.dict(os.environ, JIRA_BASE_URL="https://first.example.com/"),
              mock.patch.object(util, "lock", side_effect=lambda *a, **k: nullcontext()),
              mock.patch.object(spec_retro, "assess", return_value=[finding]) as assess,
              mock.patch.object(emits.Ticket, "execute") as publish):
            findings = spec_retro.review(self.run, self.event, self.folder, {})
            saved = json.loads(next(self.folder.rglob("*.json")).read_text())
            self.assertEqual(saved["destinations"]["arete"]["site"], "https://first.example.com")
            self.assertEqual(spec_retro.review(self.run, self.event, self.folder, {}), findings)
            os.environ["JIRA_BASE_URL"] = "https://other.example.com"
            with self.assertRaisesRegex(ValueError, "destination changed"):
                delivery.deliver(self.loop, self.run.id, findings, {"trust": "internal"}, trial=False,
                                 log=lambda *_: None, cfg=self.cfg)
            with self.assertRaisesRegex(ValueError, "destination changed"):
                spec_retro.review(self.run, self.event, self.folder, {})
            publish.assert_not_called()
            assess.assert_called_once()

    def test_validated_findings_are_saved_before_delivery_and_reused_after_failure(self):
        outcomes = [{"title": "One", "repo": "product", "fingerprint": "product/one"}]
        with (mock.patch.object(paths, "STATE_DIR", self.folder),
              mock.patch.object(util, "lock", side_effect=lambda *a, **k: nullcontext()),
              mock.patch.object(spec_retro, "assess", side_effect=[RuntimeError("not complete"), outcomes]) as assess):
            with self.assertRaisesRegex(RuntimeError, "not complete"):
                spec_retro.review(self.run, self.event, self.folder, {})
            self.assertEqual(list(self.folder.rglob("*.json")), [])
            reviewed = [{**outcomes[0], "_reviewed_destination": {"tracker": "github", "github": self.repo.github}}]
            self.assertEqual(spec_retro.review(self.run, self.event, self.folder, {}), reviewed)
            self.run.id = "retry"
            self.assertEqual(spec_retro.review(self.run, self.event, self.folder, {}), reviewed)
            self.assertEqual(assess.call_count, 2)
            self.assertEqual(json.loads(next(self.folder.rglob("*.json")).read_text())["findings"], outcomes)

    def test_worker_roles_numbered_attempts_and_recovery_across_runs_on_one_child(self):
        scope = [("product", "17"), ("product", "18")]
        rows = {
            "17": [{"id": "later", "status": "merged", "finished": "yes", "repair_calls": 1},
                   {"id": "first", "status": "failed", "finished": "yes", "repair_calls": 0}],
            "18": [{"id": "another", "status": "merged", "finished": "yes", "repair_calls": 0}],
        }
        def entry(name, ok=True, role="", attempt=1):
            return {"step": name, "atom": role, "try": attempt, "ok": ok}
        ledger = {"first": [entry("worker-a1", False, "build"), entry("worker-a1", True, "build", 2),
                            entry("verify", False), entry("verify2"), entry("review"), entry("review2")],
                  "later": [entry("worker-a2", True, "build"), entry("review_fix2", True, "review_repair"),
                            entry("orchestrate-t1", True, "orchestrate"), entry("verify")],
                  "another": [entry("review"), entry("review2")]}
        def read(rid):
            return {"id": rid, "issue": "17", "marks": [], "calls": ledger[rid], "failed_steps": []}
        with (mock.patch.object(spec_retro, "ticket_scopes", return_value=scope),
              mock.patch.object(run_store, "ticket_runs", side_effect=lambda n, ref, **kw: rows[ref]),
              mock.patch.object(run_store, "steps", side_effect=lambda rid: ledger[rid]),
              mock.patch.object(spec_retro.record, "read_run", side_effect=read),
              mock.patch.object(miner, "run_digest", return_value={"items": []}),
              mock.patch.object(miner, "signals_of", return_value=["retry"]),
              mock.patch.object(insights, "ticket_feedback", return_value=[])):
            _, totals, _, _ = spec_retro.observations(self.cfg, self.repo, self.event)
        self.assertEqual(totals["runs_with_failed_stage"], {"build": 1, "verify": 1})
        self.assertEqual(totals["runs_retrying_failed_call"], {"build": 1, "verify": 1})
        self.assertEqual(totals["tickets_revisiting_failed_stage_in_later_run"], {"build": 1, "verify": 1})
        self.assertEqual(totals["runs_with_repeated_stage"]["review"], 2)
        self.assertEqual(totals["tickets_with_stage_in_multiple_runs"], {"build": 1, "verify": 1})
        self.assertEqual(totals["tickets_rerun_after_failure"], 1)
        self.assertEqual(totals["recorded_repair_calls"], 1)
        self.assertEqual(totals["signals"], {"retry": 2})
        self.assertEqual(spec_retro.stage_of(entry("worker-a91")), "delegate")
        self.assertEqual(spec_retro.stage_of({"step": "publish", "call": "verify_fix2", "atom": "check_repair"}), "repair")
        self.assertEqual(spec_retro.stage_of(entry("private-branch-name")), "")

    def test_remote_role_directories_and_layer_questions_stay_separate(self):
        from arete import devices

        self.repo.layer = self.folder / "product-layer"
        self.factory.layer = self.folder / "factory-layer"
        self.repo.layer.mkdir()
        self.factory.layer.mkdir()
        (self.repo.layer / "retro.md").write_text("Which private UI checks repeated?\n" + "x" * 8192 + "past-the-limit")
        (self.factory.layer / "retro.md").write_text("Which verification stage stalled?")
        self.run.device = devices.Device("build", "ssh", host="example.invalid", dir="~/agent work")
        prompts = {}
        def respond(run, step, prompt, cwd, **kwargs):
            prompts[step] = prompt, cwd
            return SimpleNamespace(ok=True, structured={"findings": []})
        with (mock.patch.object(loop_run, "run_command", return_value=(0, "")) as remote,
              mock.patch.object(gh, "repo_private", return_value=True),
              mock.patch.object(spec_retro, "observations", return_value=([], {"signals": {"retry": 1},
                                                                               "owner_review_comments": 0}, "", 0)),
              mock.patch.object(loop_run.LoopRun, "agent", autospec=True, side_effect=respond)):
            self.assertEqual(spec_retro.assess(self.run, self.event, self.repo, self.run.device.dir, {}), [])
        self.assertEqual(remote.call_count, 2)
        for call in remote.call_args_list:
            self.assertEqual(call.args[0][:4], ["mkdir", "-p", "-m", "0700"])
            self.assertEqual(call.args[1], "~/agent work")
            self.assertIs(call.kwargs["device"], self.run.device)
        product, product_dir = prompts["retro-product"]
        factory, factory_dir = prompts["retro-factory"]
        self.assertNotEqual(product_dir, factory_dir)
        self.assertTrue(factory_dir.startswith("~/agent work/.arete-retro-"))
        self.assertIn("Which private UI checks repeated?", product)
        self.assertNotIn("past-the-limit", product)
        self.assertNotIn("private UI checks", factory)
        self.assertIn("Which verification stage stalled?", factory)
        self.assertNotIn("verification stage stalled", product)
        self.assertIn("supported by this spec", product)
        self.assertIn("supported by this spec", factory)
