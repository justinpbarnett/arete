import json
import os
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from arete import adf, config, configschema, delivery, emits, gh, idempotency, jira, loops, outbound
from support import isolate_live_state


def setUpModule():
    isolate_live_state()


class ConfiguredTickets(unittest.TestCase):
    def setUp(self):
        self.emit = emits.Ticket()
        self.repo = config.Repo("app", Path("/fixture/app"), github="team/app", tracker="jira",
                                jira_project="APP", jira_label="product-app")
        self.cfg = config.Config(repos=[self.repo], jira=config.JiraConfig(project="OTHER"))
        self.finding = {"title": "Improve feedback", "summary": "Verify the behavior at /Users/operator/build",
                        "fingerprint": self.id(), "repo": "app"}
        self.loop = loops.Loop("spec-retro", Path("/fixture/loop"), "builtin")
        self.loop.trigger.trust = "internal"
        self.spec = {"type": "ticket", "tracker": "configured", "repo": "$finding.repo", "labels": []}
        self.outcome = {"trust": "internal"}
        self.enterContext(mock.patch.dict(os.environ, JIRA_BASE_URL="https://tracker.example.com"))
        self.enterContext(mock.patch.object(outbound, "keep_configured_private"))
        self.enterContext(mock.patch.object(outbound, "own_hosts", []))
        self.enterContext(mock.patch.object(outbound, "extra_hosts", set()))
        self.client = jira.Client("https://tracker.example.com", "fixture", "fixture")
        self.enterContext(mock.patch.object(jira.Client, "from_env", return_value=self.client))
        self.enterContext(mock.patch.object(self.client, "me", return_value={"accountId": "fixture-account"}))

    def item(self):
        target = self.emit.resolve(self.spec, [self.finding], self.outcome, self.cfg)
        return self.emit.render(self.loop, "r-fixture", target, [self.finding], self.outcome)

    def send(self, item, **kwargs):
        return self.emit.execute(item, [self.finding], self.loop.name, "r-fixture", self.cfg, **kwargs)

    def test_jira_creation_and_lost_reply_reconcile_across_approval_keys(self):
        item = self.item()
        self.repo.tracker = "github"
        self.repo.jira_project = "OTHER"
        self.repo.jira_label = "wrong"
        issues = []
        visible = False

        def request(method, path, params=None, body=None):
            if method == "POST":
                self.assertEqual(path, "/rest/api/3/issue")
                fields = body["fields"]
                self.assertEqual(fields["issuetype"], {"name": "Task"})
                self.assertEqual(fields["project"], {"key": "APP"})
                self.assertEqual(fields["assignee"], {"accountId": "fixture-account"})
                self.assertEqual(fields["labels"], ["product-app"])
                self.assertNotIn("/Users/operator", adf.to_text(fields["description"]))
                issues.append({"key": "APP-7", "fields": fields})
                raise jira.JiraUnsure("lost reply")
            self.assertEqual((method, path), ("GET", "/rest/api/3/search/jql"))
            self.assertIn('project = "APP" AND creator = "fixture-account"', params["jql"])
            if issues:
                marker = params["jql"].split('"')[-2]
                self.assertIn(marker, adf.to_text(issues[0]["fields"]["description"]).splitlines())
            return {"issues": issues if visible else [], "isLast": True}

        self.enterContext(mock.patch.object(self.client, "request", side_effect=request))
        with self.assertRaises(jira.JiraUnsure):
            self.send(item)
        queued = json.loads(json.dumps(item))
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            self.send(queued)
        visible = True
        with idempotency.owning("fixture", "approval-1", "a" * 64, "/approval") as ctx:
            approval = idempotency.Publication(ctx, None)
            approval.remember({"token": "fixture"})
            self.assertEqual(self.send(queued, publication=approval), self.client.browse_url("APP-7"))
        self.assertEqual(self.send(queued), self.client.browse_url("APP-7"))
        self.assertEqual(len(issues), 1)

    def test_definite_rejection_retries_same_jira_site(self):
        item = self.item()
        self.enterContext(mock.patch.object(self.client, "find_task_marker", return_value=""))
        post = self.enterContext(mock.patch.object(self.client, "create_task",
                                                   side_effect=[jira.JiraError("rejected", 400), "url"]))
        with self.assertRaises(jira.JiraError):
            self.send(item)
        self.client.base = "https://other.example.com"
        with self.assertRaisesRegex(jira.JiraError, "different Jira site"):
            self.send(item)
        self.client.base = item["destination"]["site"]
        self.assertEqual(self.send(item), "url")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0], post.call_args_list[1])

    def test_configured_github_reconciles_ambiguous_write_at_original_destination(self):
        self.repo.tracker = "github"
        item = self.item()
        self.repo.github = "someone/else"
        written = {}
        visible = False

        def lookup(repo, marker):
            self.assertEqual(repo, "team/app")
            return written.get(marker, "") if visible else ""

        def create(repo, title, body, labels, *, single_attempt):
            self.assertEqual((repo, labels, single_attempt), ("team/app", [], True))
            marker = next(line for line in body.splitlines() if line.startswith("areteemit"))
            written[marker] = gh.issue_url(repo, 8)
            raise gh.GhUnsure("no response")

        self.enterContext(mock.patch.object(gh, "find_issue_marker", side_effect=lookup))
        posted = self.enterContext(mock.patch.object(gh, "issue_create", side_effect=create))
        with self.assertRaises(gh.GhUnsure):
            self.send(item)
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            self.send(json.loads(json.dumps(item)))
        visible = True
        self.assertEqual(self.send(item), gh.issue_url("team/app", 8))
        self.assertEqual(self.send(item), gh.issue_url("team/app", 8))
        posted.assert_called_once()

    def test_configured_github_duplicate_from_a_new_run_replays_the_same_ticket(self):
        self.repo.tracker = "github"
        item = self.item()
        self.enterContext(mock.patch.object(gh, "find_issue_marker", return_value=""))
        create = self.enterContext(mock.patch.object(gh, "issue_create", return_value=9))
        url = self.send(item)
        target = self.emit.resolve(self.spec, [self.finding], self.outcome, self.cfg)
        again = self.emit.render(self.loop, "another-run", target, [self.finding], self.outcome)
        self.assertNotEqual(again["body"], item["body"])
        self.assertEqual(self.send(again), url)
        create.assert_called_once()

    def test_recovery_after_account_change_never_sends_to_another_user(self):
        item = self.item()
        self.enterContext(mock.patch.object(self.client, "find_task_marker", return_value=""))
        post = self.enterContext(mock.patch.object(self.client, "create_task", side_effect=jira.JiraUnsure("lost")))
        with self.assertRaises(jira.JiraUnsure):
            self.send(item)
        self.client.me.return_value = {"accountId": "replacement"}
        with self.assertRaisesRegex(jira.JiraError, "cannot confirm the Jira account"):
            self.send(item)
        post.assert_called_once()

    def test_mine_manifest_and_typed_emit_validate(self):
        manifest = Path(loops.__file__).parent / "loops/mine/loop.toml"
        spec = tomllib.loads(manifest.read_text())
        configschema.validate("loop", spec)
        self.assertIn(self.spec | {"approve": False}, spec["emit"])
        with self.assertRaisesRegex(ValueError, "must be one of"):
            configschema.validate("loop", {"emit": [self.spec | {"tracker": "unconfigured"}]})

    def test_unknown_local_and_untrusted_dynamic_targets_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "internal-or-higher"):
            self.emit.resolve(self.spec, [self.finding], {"trust": "untrusted"}, self.cfg)
        for name in ["not-registered", "outside/unknown"]:
            spec = {**self.spec, "repo": name}
            self.assertTrue(self.emit.problems(spec, self.loop, self.cfg))
            with self.assertRaisesRegex(ValueError, "registered target"):
                self.emit.resolve(spec, [self.finding], self.outcome, self.cfg)
        self.repo.tracker = "local"
        with self.assertRaisesRegex(ValueError, "GitHub or Jira"):
            self.item()

    def test_legacy_github_emits_keep_existing_create_semantics(self):
        self.spec = {"type": "ticket", "repo": "app"}
        item = self.item()
        self.assertNotIn("destination", item)
        self.enterContext(mock.patch.object(gh, "ensure_label"))
        create = self.enterContext(mock.patch.object(gh, "issue_create", return_value=3))
        url = delivery.send({"type": "ticket", "item": item}, self.loop.name, "r-fixture", self.cfg)
        self.assertEqual(url, gh.issue_url("team/app", 3))
        create.assert_called_once_with("team/app", item["title"], item["body"], ["agent"], single_attempt=False)
