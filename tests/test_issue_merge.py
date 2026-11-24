import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arete import config, gh, issue_goal, jev, merge_review, paths, pipeline, record, tracker, unblock, unblock_watch, util
from arete import verify, workspace
from arete.store import runs, tasks, unblock as ledger
from tests import support


def setUpModule():
    support.isolate_live_state()


class IssueMergeTests(unittest.TestCase):
    def test_late_ci_and_lost_merge_response_use_one_verified_decision(self):
        src = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        remote = src / "origin.git"
        util.git(["init", "--bare", str(remote)], src)
        util.git(["init", "-b", "main"], src)
        layer_dir = src / ".agents"
        layer_dir.mkdir()
        (layer_dir / "config.toml").write_text('[trust]\nlevel="automerge"\n[checks]\nread="cat doc"\n')
        doc = src / "doc"
        doc.write_text("before\n")
        util.git(["add", "doc", ".agents"], src)
        commit = ["-c", "user.name=Fixture", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit"]
        util.git([*commit, "-qm", "base"], src)
        base = util.git(["rev-parse", "HEAD"], src).strip()
        util.git(["remote", "add", "origin", str(remote)], src)
        util.git(["push", "-u", "origin", "main"], src)
        util.git(["switch", "-c", "topic"], src)
        doc.write_text("after\n")
        util.git([*commit, "-qam", "update doc"], src)
        head = util.git(["rev-parse", "HEAD"], src).strip()
        tree = util.git(["rev-parse", "HEAD^{tree}"], src).strip()
        util.git(["push", "-u", "origin", "topic"], src)
        paths.CONFIG_DIR.mkdir(exist_ok=True)
        paths.CONFIG_FILE.write_text(f'[[repo]]\nname="sample"\npath="{src}"\ngithub="example/sample"\n'
                                     'tracker="local"\npipeline=true\n[unblock]\nenabled=true\nrepos=["sample"]\n')
        cfg = config.load()
        repo = cfg.repo("sample")
        tid = tasks.task_add(repo.name, "Change doc to after.", "Update doc")
        ticket = tracker.for_ticket(cfg, repo, tid).get(tid)
        url = "https://github.com/example/sample/pull/1"
        run_id = record.start("pipeline", repo=repo.name, issue=tid, status="pr", pr_url=url, branch="topic")
        run = SimpleNamespace(id=run_id, repo=repo, budget=SimpleNamespace(issue_goal=True))
        checkout = workspace.Checkout("", repo.github, src, src, "main", url, head)
        layer = config.load_layer(layer_dir)
        build = SimpleNamespace(cfg=cfg, run=run, layer=replace(layer, dir=src / "wt/.agents"), ticket=ticket,
                                checkouts=[checkout], checked={"tree": tree, "commands": layer.checks},
                                ci_rerun_requested_heads=set())
        passed = verify.Verified("pass", "Read after from doc.", [{"what": "updated file", "passed": True, "saw": "after"}],
                                 native_trees={"": tree})
        review = pipeline.Review("approve", "Scoped to doc.", trees={"": tree})
        pr = {"state": "OPEN", "isDraft": False, "headRefOid": head, "baseRefOid": base, "baseRefName": "main",
              "mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE", "reviewDecision": ""}
        self.enterContext(patch.object(paths, "ISOLATED", False))
        self.enterContext(patch.object(issue_goal, "exists", return_value=True))
        finish = self.enterContext(patch.object(issue_goal, "lifecycle"))
        self.enterContext(patch.object(jev, "ready", return_value=True))
        self.enterContext(patch.object(jev, "risk", return_value=jev.Risk(.3, .95, {}, "Persisted change")))
        self.enterContext(patch.object(gh, "pr_merge_details", side_effect=lambda *args, **kw: pr.copy()))
        ci = self.enterContext(patch.object(gh, "merge_ci_passed", return_value=False))

        def diagnose(cfg, repo, row, attempt, evidence, limit):
            self.assertEqual(evidence["merge_candidate"]["risk"]["hold"], "Persisted change")
            return unblock.decision_of({"action": "merge", "head": head, "base": base, "reason": "Verified the change.",
                                        "evidence": ["merge_candidate"]}, evidence, limit)

        def merge(args, *, input, write):
            self.assertEqual(args[2:4], ["PUT", "repos/example/sample/pulls/1/merge"])
            self.assertEqual(json.loads(input), {"sha": head, "merge_method": "squash"})
            self.assertTrue(write)
            pr["state"] = "MERGED"
            raise gh.GhUnsure("merge response lost")

        vote = self.enterContext(patch.object(unblock, "diagnose", side_effect=diagnose))
        put = self.enterContext(patch.object(gh, "gh", side_effect=merge))

        def inspect():
            case = ledger.get(repo.name, tid)
            ledger.mark(case, case["phase"], case["detail"])
            unblock_watch.inspect(cfg, repo, tid, runs.run_get(run_id), [], ledger.get(repo.name, tid))

        self.assertEqual(pipeline.merge_hold(build, checkout, passed, review, ticket.title), merge_review.PENDING)
        ledger.follow(repo.name, tid, run_id)
        inspect()
        vote.assert_not_called()
        ci.return_value = True
        inspect()
        case = ledger.get(repo.name, tid)
        grant = ledger.latest_attempt(case)
        self.assertEqual(grant["decision"]["action"], "merge")
        put.assert_not_called()
        proof = json.loads(merge_review.candidate_path(repo.name, tid).read_text())
        for field, stale in (("headRefOid", "a" * 40), ("baseRefOid", "b" * 40), ("reviewDecision", "CHANGES_REQUESTED")):
            with self.subTest(field=field), patch.dict(pr, {field: stale}):
                unblock_watch.land(repo, tid, case, grant, proof)
                put.assert_not_called()
        with self.assertRaises(gh.GhUnsure):
            inspect()
        inspect()
        inspect()
        self.assertEqual(ledger.get(repo.name, tid)["phase"], "resolved")
        vote.assert_called_once()
        put.assert_called_once()
        finish.assert_called_once_with(repo.name, tid, "finish")
