import json
import os
import re
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import adf, cli, config, emits, enrollment, gh, jira, loop_run, loops, paths, push, serve, tracker, workspace
from arete.commands import setup
from arete.store import loops as loop_store
from support import isolate_live_state
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT


def setUpModule():
    isolate_live_state()


class Trackers:
    def __init__(self):
        self.github = {}
        self.links = set()
        self.jira = {}
        self.project_reads = []
        self.lost_link_reply = False
        self.subtask_types = [{"id": "22", "subtask": True}]
        self.client = jira.Client("https://jira.example.test", "fixture", "fixture")

    def github_call(self, args, *, input=None, write=False, **options):
        if args[:2] == ["label", "create"]:
            return ""
        if args[:2] == ["issue", "create"]:
            slug = args[args.index("--repo") + 1]
            issues = self.github.setdefault(slug, {})
            number = len(issues) + 1
            issues[number] = {"number": number, "id": number + 1000, "html_url": gh.issue_url(slug, number),
                              "body": input, "title": args[args.index("--title") + 1],
                              "labels": [{"name": args[i + 1]} for i, a in enumerate(args) if a == "--label"]}
            return issues[number]["html_url"]
        assert args[0] == "api", args
        match = re.fullmatch(r"repos/([^/]+/[^/]+)/issues(?:/(\d+)(/sub_issues)?)?(?:\?.*)?", args[1])
        assert match, args
        slug, number, subissues = match.groups()
        issues = self.github.get(slug, {})
        if number is None:
            return json.dumps([list(issues.values())])
        number = int(number)
        if not subissues:
            if number not in issues:
                raise gh.GhError("fixture issue not found")
            return json.dumps(issues[number])
        if not write:
            return json.dumps([[row for row in issues.values() if (slug, number, row["id"]) in self.links]])
        assert "POST" in args and json.loads(input)["replace_parent"] is False, args
        self.links.add((slug, number, json.loads(input)["sub_issue_id"]))
        if self.lost_link_reply:
            self.lost_link_reply = False
            raise gh.GhUnsure("fixture lost the response after linking")
        return ""

    def jira_call(self, method, path, params=None, body=None, **options):
        if path == "/rest/api/3/myself":
            return b'{"accountId":"fixture-account"}'
        if key := re.fullmatch(r"/rest/api/3/project/(\w+)", path):
            self.project_reads.append(key[1])
            if key[1] not in {"TEAM", "CHOSEN", "ALT"}:
                raise jira.JiraError("fixture project not found", 404)
            return json.dumps({"key": key[1]}).encode()
        if path.endswith("/issuetypes"):
            return json.dumps({"issueTypes": self.subtask_types}).encode()
        if path == "/rest/api/3/search/jql":
            project = re.search(r'project = "(\w+)"', params["jql"])[1]
            return json.dumps({"issues": [r for k, r in self.jira.items() if k.startswith(project + "-")],
                               "isLast": True}).encode()
        if path.startswith("/rest/api/2/issue/"):
            return json.dumps(self.jira[path.rsplit("/", 1)[-1]]).encode()
        assert (method, path) == ("POST", "/rest/api/3/issue"), (method, path)
        fields = body["fields"]
        project = fields["project"]["key"]
        key = f"{project}-{len(self.jira) + 1}"
        self.jira[key] = {"key": key, "fields": fields}
        return json.dumps({"key": key}).encode()


class EnrollAndPublish(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=paths.STATE_DIR)))
        self.trackers = Trackers()
        self.enterContext(mock.patch.object(gh, "gh", side_effect=self.trackers.github_call))
        self.enterContext(mock.patch.object(jira.Client, "from_env", return_value=self.trackers.client))
        self.enterContext(mock.patch.object(self.trackers.client, "_send", side_effect=self.trackers.jira_call))
        self.enterContext(mock.patch.object(tracker, "_clients", {}))
        self.enterContext(mock.patch("sys.stdin", StringIO()))
        self.enterContext(mock.patch.dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1"))
        self.body = self.root / "body.md"
        self.body.write_text("Verify that saving shows the new status.\n")
        self.config_for("TEAM")
        self.parser = cli.parser()

    def config_for(self, project):
        paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        paths.CONFIG_FILE.write_text(f'[jira]\ngithub_owner = "Example-Team"\nproject = "{project}"\n')

    def git_checkout(self, name, origin):
        checkout = self.root / name
        subprocess.run(["git", "init", "-q", str(checkout)],
                       check=True, capture_output=True, timeout=CHILD_HANG_TIMEOUT)
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", f"git@github.com:{origin}.git"],
                       check=True, capture_output=True, timeout=CHILD_HANG_TIMEOUT)
        return checkout

    def call(self, *argv, status=0):
        output, errors = StringIO(), StringIO()
        args = self.parser.parse_args(["repo", *map(str, argv)])
        with redirect_stdout(output), redirect_stderr(errors):
            result = args.fn(args)
        self.assertEqual(result, status, errors.getvalue())
        return output.getvalue().strip(), errors.getvalue().strip()

    def enroll(self, name, origin, *options):
        checkout = self.git_checkout(name, origin)
        out, _ = self.call("enroll", name, checkout, "--target-branch", "release/v2", "--pipeline", "--json", *options)
        return json.loads(out)

    def publish(self, repo, kind, *options, key=None, status=0):
        key = key or f"{self.id().split('.')[-1]}/{repo}/{kind}"
        return self.call("publish", repo, kind, "--title", f"A {kind}", "--body-file", self.body,
                         "--key", key, *options, status=status)

    def test_defaults_overrides_and_policy_round_trip(self):
        personal = self.enroll("personal", "me/blog", "--landing", "direct-push", "--unattended-environment", "staging")
        self.assertEqual(personal["destination"], {"tracker": "github", "github": "me/blog"})
        self.assertEqual(personal["target_branch"], {"source": "repo", "value": "release/v2"})
        self.assertEqual(personal["landing"], {"source": "repo", "value": "direct-push"})
        self.assertEqual(personal["unattended_environment"], {"source": "repo", "value": "staging"})
        team = self.enroll("team", "example-team/team")
        self.assertEqual(team["destination"], {"tracker": "jira", "project": "TEAM", "route": "repo-team",
                                               "site": self.trackers.client.base})
        self.assertEqual(team["target_branch"]["value"], "release/v2")
        self.assertEqual(team["landing"]["value"], "pr")
        self.assertEqual(team["unattended_environment"]["value"], "none")
        self.assertEqual(self.enroll("switched", "example-team/switched", "--tracker", "github")["destination"],
                         {"tracker": "github", "github": "example-team/switched"})
        overridden = self.enroll("alternate", "me/alternate", "--jira-project", "ALT", "--jira-label", "api")
        self.assertEqual((overridden["destination"]["project"], overridden["destination"]["route"]), ("ALT", "api"))
        self.assertEqual(self.trackers.project_reads, ["TEAM", "ALT"])
        out, _ = self.call("show", "personal", "--json")
        self.assertEqual(json.loads(out), personal)
        self.assertEqual(config.load_required().repo("personal").target_branch, "release/v2")

    def test_missing_jira_requires_resolution_before_save_and_never_prompts_later(self):
        self.config_for("")
        repo = self.git_checkout("chosen", "example-team/chosen")
        argv = ("enroll", "chosen", repo, "--target-branch", "main")
        before = paths.CONFIG_FILE.read_bytes()
        _, error = self.call(*argv, status=1)
        self.assertIn("--jira-project", error)
        _, error = self.call(*argv, "--jira-project", "ABSENT", status=1)
        self.assertIn("not found", error)
        self.assertEqual(paths.CONFIG_FILE.read_bytes(), before)
        tty = StringIO("chosen\n")
        with mock.patch.object(tty, "isatty", return_value=True), mock.patch("sys.stdin", tty):
            _, prompt = self.call(*argv)
        self.assertIn("Jira project key", prompt)
        paths.CONFIG_FILE.write_text(paths.CONFIG_FILE.read_text().replace('project = ""', 'project = "TEAM"', 1))
        cfg = config.load_required()
        self.assertEqual(tracker.for_repo(cfg, cfg.repo("chosen")).project, "CHOSEN")
        reads = list(self.trackers.project_reads)
        with mock.patch.object(setup, "choose_jira_project", side_effect=AssertionError("prompted after enrollment")):
            url, _ = self.publish("chosen", "spec")
        self.assertEqual(url, self.trackers.client.browse_url("CHOSEN-1"))
        self.assertEqual(self.trackers.project_reads, reads)

    def test_published_spec_enters_enabled_queue_and_draft_stays_unadmitted(self):
        self.enroll("arete", "me/arete", "--pipeline")
        self.enroll("app", "example-team/app", "--pipeline")
        self.publish("arete", "spec")
        self.publish("app", "spec")
        self.publish("app", "spec", "--draft", key="draft-spec")
        self.assertEqual([v["name"] for v in self.trackers.github["me/arete"][1]["labels"]], ["spec", "agent"])
        self.assertEqual(self.trackers.jira["TEAM-1"]["fields"]["labels"], ["spec", "agent", "repo-app"])
        self.assertEqual(self.trackers.jira["TEAM-2"]["fields"]["labels"], ["spec", "repo-app"])

    def test_planning_and_cross_repo_factory_findings_land_at_same_saved_destinations(self):
        self.enroll("arete", "me/arete")
        self.enroll("app", "example-team/app")
        team_spec, _ = self.publish("app", "spec")
        team_child, _ = self.publish("app", "child", "--parent", "TEAM-1")
        self.assertEqual(team_spec, self.trackers.client.browse_url("TEAM-1"))
        self.assertEqual(team_child, self.trackers.client.browse_url("TEAM-2"))
        self.assertEqual(self.trackers.jira["TEAM-1"]["fields"]["labels"], ["spec", "agent", "repo-app"])
        child = self.trackers.jira["TEAM-2"]["fields"]
        self.assertEqual(child["parent"], {"key": "TEAM-1"})
        self.assertEqual(child["issuetype"], {"id": "22"})
        self.assertEqual(child["labels"], ["repo-app"])
        self.assertEqual(self.publish("arete", "spec")[0], "https://github.com/me/arete/issues/1")
        manual, _ = self.publish("arete", "follow-up", "--source", "https://github.com/me/arete/issues/1")
        self.assertEqual(manual, "https://github.com/me/arete/issues/2")
        self.assertIn("https://github.com/me/arete/issues/1", self.trackers.github["me/arete"][2]["body"])
        cfg = config.load_required()
        cfg.jev.enabled = False
        folder = Path(loops.__file__).parent / "loops/mine"
        loop = loops.load(folder, "arete", "", cfg)
        loop.repo = "app"
        rows = [{"repo": "app", "title": "Product follow-up", "summary": "Product evidence",
                 "fingerprint": self.id() + "/app", "evidence": [team_spec]},
                {"repo": "arete", "title": "Factory follow-up", "summary": "One review step repeated",
                 "fingerprint": self.id() + "/arete"}]
        run = SimpleNamespace(loop=loop, cfg=cfg, id="fixture-mine", trial=False, log=mock.Mock())
        loop_run.finish_watch(run, [{"trust": "internal"}], rows, loop_run.new_findings(run, rows), "Reviewed team spec")
        app = self.trackers.jira["TEAM-3"]["fields"]
        self.assertEqual(app["labels"], ["repo-app"])
        self.assertIn(team_spec, adf.to_text(app["description"]))
        factory = self.trackers.github["me/arete"][3]
        self.assertEqual(factory["title"], "Factory follow-up")
        self.assertNotIn(team_spec, factory["body"])
        self.assertEqual(factory["labels"], [])
        self.assertEqual({f["url"] for f in loop_store.findings("mine")},
                         {self.trackers.client.browse_url("TEAM-3"), gh.issue_url("me/arete", 3)})

    def test_pinned_jira_site_prevents_publication_to_another_connection(self):
        self.enroll("app", "example-team/app")
        self.trackers.client.base = "https://other.example.test"
        _, error = self.publish("app", "spec", status=1)
        self.assertIn("different Jira site", error)
        self.assertEqual(self.trackers.jira, {})

    def test_merge_check_preserves_github_admission_without_admitting_jira_findings(self):
        self.enroll("app", "example-team/app")
        self.enroll("personal", "me/app")
        cfg = config.load_required()
        folder = Path(loops.__file__).parent / "loops/merge-check"
        spec, = tomllib.loads((folder / "loop.toml").read_text())["emit"]
        loop = loops.Loop("merge-check", folder, "arete")
        emit = emits.Ticket()
        for name, expected in (("app", []), ("personal", ["agent"])):
            findings = [{"repo": name, "title": "Merged regression", "summary": "Check failed", "fingerprint": name}]
            outcome = {"trust": "internal"}
            target = emit.resolve(spec, findings, outcome, cfg)
            item = emit.render(loop, "fixture", target, findings, outcome)
            self.assertEqual(item["labels"], expected)
            self.assertEqual(item["destination"]["tracker"], cfg.repo(name).tracker)

    def test_github_child_reconciles_link_without_duplicate_issue(self):
        self.enroll("app", "me/app")
        spec, _ = self.publish("app", "spec")
        self.assertEqual(spec, gh.issue_url("me/app", 1))
        _, error = self.publish("app", "child", "--parent", "2", key="nonexistent-parent", status=1)
        self.assertIn("fixture issue not found", error)
        self.assertEqual(len(self.trackers.github["me/app"]), 1)
        self.trackers.lost_link_reply = True
        self.publish("app", "child", "--parent", "1", status=1)
        url, _ = self.publish("app", "child", "--parent", "1")
        self.assertEqual(url, gh.issue_url("me/app", 2))
        self.assertEqual(len(self.trackers.github["me/app"]), 2)
        self.assertEqual(self.trackers.links, {("me/app", 1, 1002)})

    def test_unsupported_jira_hierarchy_does_not_create_unlinked_child(self):
        self.enroll("app", "example-team/app")
        self.publish("app", "spec")
        self.trackers.subtask_types = []
        _, error = self.publish("app", "child", "--parent", "TEAM-1", status=1)
        self.assertIn("exactly one available subtask type", error)
        self.assertEqual(list(self.trackers.jira), ["TEAM-1"])

    def test_legacy_settings_remain_independent_and_policy_is_checked_at_delivery(self):
        checkout = self.git_checkout("old", "example-team/old")
        layer = checkout / ".agents"
        layer.mkdir()
        (layer / "config.toml").write_text('[git]\nbase = "old-base"\n[trust]\nlevel = "automerge"\n')
        with paths.CONFIG_FILE.open("a") as f:
            f.write(f'\n[[repo]]\nname = "old"\npath = {json.dumps(str(checkout))}\ngithub = "example-team/old"\n')
        legacy = json.loads(self.call("show", "old", "--json")[0])
        self.assertEqual(legacy["destination"]["tracker"], "github")
        self.assertEqual(legacy["target_branch"], {"source": "legacy", "value": "old-base"})
        self.assertEqual(legacy["landing"], {"source": "legacy", "value": None, "trust": "automerge"})
        self.assertEqual(legacy["unattended_environment"], {"source": "legacy", "value": None})
        self.enroll("new", "me/new", "--unattended-environment", "staging")
        cfg = config.load_required()
        old, new = cfg.repo("old"), cfg.repo("new")
        policy = config.policy_dict(cfg)
        self.assertNotIn("github_owner", policy["jira"])
        for key in ("jira_site", "target_branch", "landing", "unattended_environment"):
            self.assertNotIn(key, policy["repos"][0])
        self.assertEqual(policy["repos"][1]["unattended_environment"], "staging")
        self.assertTrue(old.allows_unattended_deploy("production"))
        self.assertTrue(new.allows_unattended_deploy("staging"))
        self.assertFalse(new.allows_unattended_deploy("production"))
        item = {"repo": new.name, "workdir": str(new.path), "base": new.target_branch, "check": "true"}
        with mock.patch.object(push.util, "git") as git:
            with self.assertRaisesRegex(RuntimeError, "requires a PR"):
                push.Push().execute(item, [], "fixture", "fixture", cfg)
        git.assert_not_called()
        self.assertEqual(push.policy_refusal(old, "old-base"), "")
        c = workspace.Checkout("", new.github, new.path, self.root / "worktree", "old-base")
        with self.assertRaisesRegex(workspace.RunFailed, "release/v2"):
            workspace.require_target_branch(new, [c])
        c.base, c.pr = new.target_branch, "https://github.com/me/new/pull/10"
        with mock.patch.object(gh, "pr_view", return_value={"baseRefName": "old-base"}):
            with self.assertRaisesRegex(workspace.RunFailed, "release/v2"):
                workspace.require_target_branch(new, [c])
        with mock.patch.object(workspace, "kept_or_cleared"), mock.patch.object(workspace, "place_checkouts") as place:
            with self.assertRaisesRegex(workspace.RunFailed, "separate work branch"):
                workspace.make_workspace("fixture-run", new, "old-base", new.target_branch, "fixture", [])
            place.assert_not_called()
            _, (checkout,) = workspace.make_workspace("fixture-run", new, "old-base", "feature", "fixture", [])
        self.assertEqual(checkout.base, "release/v2")
        with mock.patch.object(serve.paths, "PACKAGE_DIR", new.path), mock.patch.object(serve.util, "git") as git:
            serve.Server.deploy(mock.Mock(), cfg)
        git.assert_not_called()
        self.assertEqual(enrollment.show(cfg, cfg.repo("old")), legacy)
