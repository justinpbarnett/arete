import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arete import (
    config,
    dbstats,
    environment_gaps,
    factory,
    gh,
    oncall,
    orchestration,
    pilot,
    pipeline,
    poll,
    routing,
    run_lines,
    scrub,
    tasks,
    tracker,
    util,
    vault,
    zte,
)
from arete.store import db, events
from arete.store import orchestration as ledger
from arete.store import runs, ticket_progress, unblock
from tests.support import workspace_state

REPO = "arete"
TICKET = 964
PARENT = "pip-source"
RETRY = "pip-retry"
GITHUB = "owner/arete"
DRAFT = f"https://github.com/{GITHUB}/pull/50"
HEAD = "a" * 40
RELEASE = "b" * 40
MERGE = "c" * 40
SIG = environment_gaps.Signature("exit 127", "fixture")


def poll_hold(root, state, *, dry_run=False, can_start=False, confirm_release=False):
    repo = config.Repo(REPO, root, github=GITHUB)
    cfg = config.Config(repos=[repo])
    if not (rows := runs.environment_holds(REPO)):
        return False
    row, = rows
    hold = environment_gaps.Hold([environment_gaps.Gap(SIG, "", "", "")], {DRAFT: HEAD})
    posted = root / "github-comments.jsonl"
    comments = [json.loads(line) for line in posted.read_text().splitlines()] if posted.exists() else []
    talk = gh.Discussion([], [], comments)

    def comment(url, body, *, key, single_attempt):
        assert url == DRAFT and single_attempt, body
        with posted.open("a") as stream:
            stream.write(json.dumps({"body": gh.outgoing(body, mark=True, key=key)}) + "\n")
        if state == "lost-reply" and "needs owner review" in body:
            raise gh.GhUnsure("response lost after comment was accepted")

    def issue(repo, number):
        if number == TICKET:
            return {"state": "OPEN", "body": "Waits on #41\nAn unrelated mention: #99", "author": {"login": "owner"},
                    "labels": [{"name": "agent"}]}
        return {"state": "CLOSED"}

    closure_calls = 0
    closure_fails = state in ("closure-unreadable", "release-unreadable")
    live = RELEASE if closure_fails or state in ("deployed", "unreadable", "lost-reply") else ""

    def linked(args):
        nonlocal closure_calls
        number, = [int(a.split("=")[1]) for a in args if a.startswith("number=")]
        if "--paginate" not in args:
            assert number == 40
            closure_calls += 1
            closer = {"__typename": "PullRequest", "merged": True, "baseRefName": "main",
                      "repository": {"nameWithOwner": GITHUB}, "mergeCommit": {"oid": MERGE}}
            result = {"data": {"repository": {"issue": {"state": "CLOSED", "stateReason": "COMPLETED",
                                                        "timelineItems": {"nodes": [{"closer": closer}]}}}}}
            if state == "closure-unreadable" or (state == "release-unreadable" and closure_calls == 2):
                result["errors"] = [{"message": "temporarily unreadable closure"}]
            return result
        if state == "unreadable":
            raise gh.GhError("temporary unreadable prerequisite")
        if number not in (40, 41):
            raise AssertionError(f"unexpected prerequisite lookup for {number}")
        name = "Owner/Arete" if number == 40 else GITHUB
        nodes = [{"number": 49, "repository": {"nameWithOwner": name}, "baseRefName": "main",
                  "state": "OPEN" if state == "open" else "MERGED",
                  "mergeCommit": None if state == "open" else {"oid": MERGE}},
                 {"number": 50, "repository": {"nameWithOwner": name}, "baseRefName": "main", "state": "OPEN"}]
        return [{"data": {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": nodes}}}}}]

    with (
        patch.object(environment_gaps, "hold_of", return_value=hold),
        patch.object(environment_gaps, "discussions", return_value={} if state == "owner-took-over" else {DRAFT: talk}),
        nullcontext() if closure_fails else patch.object(environment_gaps, "issue_for", return_value=40),
        patch.object(environment_gaps, "live_release", return_value=live),
        patch.object(environment_gaps, "release_base", return_value="main"),
        patch.object(gh, "gh_json", side_effect=linked),
        patch.object(gh, "issue", side_effect=issue),
        patch.object(gh, "branch_contains", side_effect=lambda repo, base, head: (
            (base, head) in {("main", RELEASE), (RELEASE, MERGE)})),
        nullcontext() if closure_fails else patch.object(gh, "issue_merge_commit",
                                                        return_value=MERGE if confirm_release else ""),
        patch.object(gh, "pr_comment", side_effect=comment),
        patch.object(pipeline, "recorded_path", return_value="classic"),
        patch.object(oncall, "parked", return_value=()),
        patch.object(pilot, "is_pilot_task", return_value=False),
        patch.object(zte, "reserved", return_value=False),
        patch.object(tasks, "pending_start", return_value=False),
    ):
        return environment_gaps.handle(cfg, repo, row, dry_run, can_start=can_start)


def poll_dependencies(root, state, *, dry_run=False, publish_unblock=False, at_capacity=False):
    repo = config.Repo(REPO, root, github=GITHUB, parts=[config.Part("", GITHUB)])
    cfg = config.Config(repos=[repo], unblock_repos=(REPO,) if publish_unblock else ())
    layer = config.RepoLayer(root)
    queue = poll.RepoQueue(repo, tracker.GitHubTracker(repo), set(), layer,
                           routing.Discovery(cfg, repo, priced=False, layer=layer))
    ticket = {"number": 965, "title": "Ordinary follow-up", "body": "Waits on #41\nUnrelated: #99",
              "author": {"login": "owner"}, "labels": [{"name": "agent"}]}
    if at_capacity:
        ticket["labels"].append({"name": "agent:pr"})
    if state == "removed":
        ticket["body"] = "No prerequisite"
    pull = {"number": 49, "repository": {"nameWithOwner": GITHUB}, "baseRefName": "main",
            "state": "MERGED" if state == "deployed" else "OPEN",
            "mergeCommit": {"oid": MERGE} if state == "deployed" else None}
    pages = [{"data": {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": [pull]}}}}}]
    skips = pipeline.SkipLog({})
    errors = []
    with (
        patch.object(gh, "issues", side_effect=lambda repo, label, **_: [ticket] if label == tracker.LABEL else []),
        patch.object(gh, "issue", return_value={"state": "OPEN"}),
        patch.object(gh, "gh_json", return_value=pages) as linked,
        patch.object(environment_gaps, "live_release", return_value=RELEASE if state == "deployed" else ""),
        patch.object(environment_gaps, "release_base", return_value="main"),
        patch.object(gh, "branch_contains", return_value=True),
        patch.object(poll, "start_pick") as start,
    ):
        selected = poll.fill_slots(cfg, [queue], skips, dry_run, errors, Counter(), 0 if at_capacity else 1)
        assert not errors, errors
        assert not selected
        start.assert_not_called()
        if dry_run or state == "removed":
            linked.assert_not_called()
        else:
            linked.assert_called_once()
            assert "number=41" in linked.call_args.args[0]
    return skips.current


def check_action(root, run_id, command, code, *, previous=""):
    repo = config.Repo(REPO, root)
    layer = config.RepoLayer(root, checks={"unit": command})
    run = SimpleNamespace(id=run_id, config=config.Config(repos=[repo]), repo=repo, time_for=lambda limit: limit,
                          log=lambda _: None)
    controller = object.__new__(orchestration.Controller)
    controller.run = run
    controller.job = SimpleNamespace(b=SimpleNamespace(), ws=root)
    controller.data = {"acceptance": "fixture"}
    saved = ledger.find(run_id)
    controller.lease = (ledger.Lease(saved["root"], saved["current_run"], saved["generation"])
                        if saved else ledger.acquire(run_id, previous, {}))
    version = ledger.snapshot(controller.lease)["version"]
    turn = ledger.begin(controller.lease, {}, None)
    action, = ledger.accept(controller.lease, turn, version,
                            {"calls": [{"op": "checks.run", "args": {"scope": "full"}}]}, None)

    def atomic(_name, **_kwargs):
        ok, report = pipeline.run_checks(layer, root, [], run, 1)
        return factory.Outcome("pass" if ok else "fail", data={"report": report})

    with (
        patch.object(controller, "atomic", side_effect=atomic),
        patch.object(controller, "barrier"),
        patch.object(controller, "fingerprint", return_value="tree"),
        patch.object(controller, "persist"),
        patch.object(controller, "policy_sha", return_value="policy"),
        patch.object(controller, "stamp"),
        patch.object(pipeline, "check_lane", return_value=nullcontext()),
        patch.object(scrub, "secrets", return_value=()),
        patch.object(pipeline.environment, "sh", return_value=subprocess.CompletedProcess(
            [], code, "### unrelated failed: `fake command` exit 8", "")),
    ):
        controller.dispatch(action)
        settled = next(item for item in ledger.actions(controller.lease) if item["id"] == action["id"])
        before = ticket_progress.get(REPO, TICKET)
        controller.dispatch(settled)
        assert ticket_progress.get(REPO, TICKET) == before
        return settled


def restarted_poll(root, state, producer):
    with workspace_state(root), patch.object(dbstats.Timing, "finished"), patch.object(vault, "values", return_value=set()):
        if producer in ("dependencies", "capacity"):
            poll_dependencies(root, state, at_capacity=producer == "capacity")
        elif producer == "checks":
            check_action(root, "pip-check-resume", "unit fixture", 1, previous="pip-check-root")
        else:
            poll_hold(root, state, can_start=producer in ("workspace", "closure"),
                      confirm_release=producer == "workspace")


class TicketProgressTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="arete-ticket-progress-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.enterContext(workspace_state(self.root))
        self.enterContext(patch.object(dbstats.Timing, "finished"))
        self.enterContext(patch.object(vault, "values", return_value=set()))
        db.migrate()
        runs.insert_run(PARENT, {"kind": "pipeline", "repo": REPO, "issue": TICKET, "status": "pr",
                                 "pr_url": DRAFT, "branch": "fixture", "started": "2026-11-24T01:00:00Z"})
        runs.save_environment_hold(PARENT, [(GITHUB, 50, DRAFT, HEAD)])
        runs.set_environment_waits(PARENT, {SIG.key: (GITHUB, 40)})

    def restart(self, state, producer="hold"):
        child = subprocess.run([sys.executable, "-c", "from pathlib import Path; import sys; "
                                "from tests.test_ticket_progress import restarted_poll; "
                                "restarted_poll(Path(sys.argv[1]), sys.argv[2], sys.argv[3])", str(self.root), state, producer],
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)

    def updates(self, ticket=TICKET):
        selected = events.filters({"class": "durable", "kinds": ticket_progress.KIND,
                                   "subject": ticket_progress.subject(REPO, ticket)})
        with db.reader() as c:
            _, _, rows, reason, _ = events.scan(c, None, 0, selected)
        self.assertIsNone(reason)
        return [u for row in rows for u in json.loads(row["data"])["updates"]]

    def test_discovery_deduplicates_repeated_polls_and_restart_and_keeps_delivery_separate(self):
        self.assertFalse(poll_hold(self.root, "open", dry_run=True))
        self.assertEqual(ticket_progress.get(REPO, TICKET), {})
        self.assertFalse(poll_hold(self.root, "open"))
        opened = ticket_progress.get(REPO, TICKET)
        self.assertTrue(opened["active"])
        self.assertEqual(opened["follow_up"], {"state": "waiting", "prs": [DRAFT], "draft": True})
        self.assertEqual(opened["blocker"]["causes"], {SIG.key: SIG.title})
        pull, = opened["prerequisites"]
        self.assertEqual((pull["key"], pull["state"], pull["deployed_release"]), (f"{GITHUB}#49", "open", ""))
        self.assertEqual(pull["issues"], [f"{GITHUB}#40", f"{GITHUB}#41"])
        poll_hold(self.root, "open")
        self.restart("open")
        self.assertEqual(ticket_progress.get(REPO, TICKET), opened)

        poll_hold(self.root, "merged")
        merged = ticket_progress.get(REPO, TICKET)
        self.assertEqual(merged["next_event"], "The live release is confirmed to contain the fixes")
        self.assertEqual(merged["prerequisites"][0]["state"], "merged")
        self.assertEqual(merged["prerequisites"][0]["deployed_release"], "")
        poll_hold(self.root, "deployed")
        deployed = ticket_progress.get(REPO, TICKET)
        self.assertEqual(deployed["next_event"], "Automatic verification retry starts when eligible")
        self.assertEqual(deployed["prerequisites"][0]["deployed_release"], RELEASE)
        with patch.object(tasks, "resume") as resume:
            self.assertFalse(poll_hold(self.root, "unreadable", can_start=True, confirm_release=True))
        resume.assert_not_called()
        unread = ticket_progress.get(REPO, TICKET)
        self.assertEqual(unread["prerequisites"], deployed["prerequisites"])
        self.assertEqual(unread["prerequisite_issues"], {f"{GITHUB}#40": False, f"{GITHUB}#41": False})
        update, = self.updates()
        self.assertEqual((update["kind"], update["pr"]), ("prerequisite-pr", f"{GITHUB}#49"))

    def test_unreadable_closure_keeps_the_wait_notice_and_saved_links(self):
        poll_hold(self.root, "deployed")
        before = ticket_progress.get(REPO, TICKET)
        comments = self.root / "github-comments.jsonl"
        comments.unlink()
        with patch.object(tasks, "resume") as resume, patch.object(gh, "issues") as search:
            self.assertFalse(poll_hold(self.root, "closure-unreadable", can_start=True))
            self.assertFalse(poll_hold(self.root, "release-unreadable", can_start=True))
        resume.assert_not_called()
        search.assert_not_called()
        held = runs.environment_hold(PARENT)
        self.assertEqual(held["waits"], {SIG.key: [GITHUB, 40]})
        self.assertEqual(held["heads"], {DRAFT: HEAD})
        self.assertFalse(held["released"])
        self.assertIsNone(held["retry_id"])
        unread = ticket_progress.get(REPO, TICKET)
        self.assertEqual(unread["prerequisites"], before["prerequisites"])
        self.assertEqual(unread["next_event"], "The prerequisite PR state can be confirmed")
        comment, = comments.read_text().splitlines()
        body = json.loads(comment)["body"]
        self.assertIn(f"Verification is waiting on #40 for the environment gap in run `{PARENT}`.", body)
        self.restart("release-unreadable", producer="closure")
        self.assertEqual(comments.read_text(), comment + "\n")
        self.assertEqual(ticket_progress.get(REPO, TICKET), unread)

    def test_manual_release_without_retry_persists_end_and_preserves_links(self):
        poll_hold(self.root, "deployed")
        before = ticket_progress.get(REPO, TICKET)
        at = "2026-11-24T02:00:00Z"
        with patch.object(util, "now", return_value=util.parse_iso(at)):
            self.assertFalse(poll_hold(self.root, "owner-took-over", dry_run=True))
            self.assertFalse(runs.environment_hold(PARENT)["released"])
            self.assertFalse(poll_hold(self.root, "owner-took-over"))
        hold = runs.environment_hold(PARENT)
        self.assertEqual((hold["released"], hold["released_at"], hold["release_state"]), (1, at, "inactive"))
        self.assertEqual((hold["retry_id"], hold["retry_started_at"], hold["owner_review_started_at"],
                          hold["owner_review_ended_at"]), (None, None, None, None))
        self.assertEqual(hold["waits"], {SIG.key: [GITHUB, 40]})
        self.assertEqual(hold["heads"], {DRAFT: HEAD})
        self.assertEqual(runs.environment_waits(REPO, TICKET), [])
        self.assertEqual(runs.environment_holds(REPO), [])
        self.assertEqual(runs.environment_owner_reviews(REPO), [])
        after = ticket_progress.get(REPO, TICKET)
        self.assertFalse(after["active"])
        self.assertEqual(after["prerequisites"], before["prerequisites"])
        self.restart("owner-took-over")
        runs.release_environment_hold(PARENT)
        self.assertEqual(runs.environment_hold(PARENT), hold)

    def test_same_cause_requires_a_new_retry_and_survives_restart_during_owner_review(self):
        poll_hold(self.root, "deployed")
        poll_hold(self.root, "deployed")
        self.assertEqual([u["kind"] for u in self.updates()], ["prerequisite-pr"])
        started, finished = "2026-11-24T02:00:00Z", "2026-11-24T02:10:00Z"
        released, ended = "2026-11-24T02:15:00Z", "2026-11-24T04:00:00Z"
        runs.insert_run(RETRY, {"kind": "pipeline", "repo": REPO, "issue": TICKET, "status": "running",
                                "environment_retry_of": PARENT, "started": started})
        self.assertEqual(runs.environment_hold(PARENT)["retry_started_at"], started)
        poll_hold(self.root, "deployed")
        running = ticket_progress.get(REPO, TICKET)
        self.assertIsNone(running["blocker"])
        self.assertEqual(running["next_event"], "Automatic verification finishes")
        outcome = {"ending": "environment-held", "verified": {"checks": [
            {"cause": "environment", "saw": "exit 127 for 'fixture'"}]}}
        run_lines.write(RETRY, "step_end", result="stop", step="verify", data=outcome)
        runs.move(RETRY, "pr", expect=("running",), pr_url=DRAFT, finished=finished)
        with patch.object(util, "now", return_value=util.parse_iso(released)):
            with self.assertRaises(gh.GhUnsure):
                poll_hold(self.root, "lost-reply")
        repeated = ticket_progress.get(REPO, TICKET)
        self.assertEqual(repeated["attempt"], RETRY)
        self.assertEqual(repeated["blocker"]["causes"], {SIG.key: SIG.title})
        self.assertEqual(repeated["follow_up"]["state"], "owner-review")
        self.assertIn("Owner reviews", repeated["next_event"])
        self.assertEqual(repeated["prerequisites"][0]["deployed_release"], RELEASE)
        hold = runs.environment_hold(PARENT)
        self.assertEqual((hold["released_at"], hold["release_state"], hold["owner_review_started_at"],
                          hold["owner_review_ended_at"]), (released, "owner-review", finished, None))
        review, = runs.environment_owner_reviews(REPO)
        self.assertEqual((review["id"], review["retry_id"], review["retry_started_at"]), (PARENT, RETRY, started))
        comments = self.root / "github-comments.jsonl"
        posted = comments.read_text()
        waiting, owner_review = [json.loads(line)["body"] for line in posted.splitlines()]
        self.assertIn("Verification is waiting on #40", waiting)
        self.assertIn("needs owner review", owner_review)
        self.restart("deployed")
        self.assertEqual(runs.environment_hold(PARENT), hold)
        self.assertEqual(ticket_progress.get(REPO, TICKET), repeated)
        self.assertEqual(comments.read_text(), posted)
        self.assertEqual([(u["kind"], u.get("attempt")) for u in self.updates()],
                         [("prerequisite-pr", None), ("recurring-blocker", RETRY)])
        with patch.object(util, "now", return_value=util.parse_iso(ended)):
            poll_hold(self.root, "owner-took-over")
        self.assertEqual(runs.environment_owner_reviews(REPO), [])
        self.assertEqual(runs.environment_holds(REPO), [])
        self.assertEqual(runs.environment_hold(PARENT), {**hold, "owner_review_ended_at": ended})
        self.assertFalse(ticket_progress.get(REPO, TICKET)["active"])

    def test_retry_preflight_reports_the_workspace_blocker_without_duplicate_wait_events(self):
        poll_hold(self.root, "deployed")
        before = ticket_progress.get(REPO, TICKET)
        with patch.object(tasks, "resume", side_effect=AssertionError("must not retry without a workspace")):
            self.assertFalse(poll_hold(self.root, "deployed", can_start=True, confirm_release=True, dry_run=True))
            self.assertEqual(ticket_progress.get(REPO, TICKET), before)
            self.assertFalse(poll_hold(self.root, "deployed", can_start=True, confirm_release=True))
        blocked = ticket_progress.get(REPO, TICKET)
        self.assertTrue(blocked["active"])
        self.assertEqual(blocked["blocker"], {"kind": "retry-workspace", "causes": {
            "workspace": "the held PRs do not match the saved branch"}})
        self.assertIn("workspace becomes restorable", blocked["next_event"])
        self.assertEqual(blocked["prerequisites"][0]["deployed_release"], RELEASE)
        self.assertFalse(poll_hold(self.root, "deployed", can_start=True, confirm_release=True))
        self.restart("deployed", producer="workspace")
        self.assertEqual(ticket_progress.get(REPO, TICKET), blocked)
        self.assertEqual(runs.environment_holds(REPO)[0]["id"], PARENT)

    def test_fill_slots_discovers_declared_dependency_without_duplicate_updates(self):
        poll_dependencies(self.root, "open", dry_run=True, publish_unblock=True)
        self.assertEqual(ticket_progress.get(REPO, 965), {})
        self.assertEqual(self.updates(965), [])
        self.assertEqual(unblock.queue(REPO), {})

        skips = poll_dependencies(self.root, "open")
        self.assertIn("waits on #41", skips[f"{REPO}#965"]["why"])
        opened = ticket_progress.get(REPO, 965)
        self.assertFalse(opened["active"])
        self.assertEqual(opened["prerequisite_issues"], {f"{GITHUB}#41": True})
        self.assertEqual(opened["prerequisites"][0]["state"], "open")
        update, = self.updates(965)
        self.assertEqual((update["kind"], update["pr"]), ("prerequisite-pr", f"{GITHUB}#49"))

        poll_dependencies(self.root, "open")
        self.restart("open", producer="dependencies")
        self.assertEqual(ticket_progress.get(REPO, 965), opened)
        self.assertEqual(self.updates(965), [update])

        poll_dependencies(self.root, "deployed", publish_unblock=True)
        deployed = ticket_progress.get(REPO, 965)["prerequisites"][0]
        self.assertEqual((deployed["state"], deployed["deployed_release"]), ("merged", RELEASE))
        self.assertEqual(unblock.queue(REPO)["github"]["graph"], {"965": ["41"]})
        self.assertEqual(self.updates(965), [update])

    def test_declared_dependency_discovery_at_capacity_needs_no_run_or_skip(self):
        self.assertEqual(poll_dependencies(self.root, "open", at_capacity=True), {})
        self.assertIsNone(runs.newest_live_whole_run(REPO, 965))
        self.assertEqual(ticket_progress.declared_prerequisite_tickets(REPO), ["965"])
        self.assertEqual(ticket_progress.declared_prerequisite_tickets(f"{REPO}-other"), [])
        before = ticket_progress.get(REPO, 965)
        update, = self.updates(965)
        self.restart("open", producer="capacity")
        self.assertEqual(ticket_progress.get(REPO, 965), before)
        self.assertEqual(self.updates(965), [update])
        self.assertEqual(poll_dependencies(self.root, "removed", at_capacity=True), {})
        self.assertEqual(ticket_progress.declared_prerequisite_tickets(REPO), [])

    def test_real_check_action_results_record_recurrence_across_restart_and_continuation(self):
        first, retry = "pip-check-root", "pip-check-resume"
        runs.insert_run(first, {"kind": "pipeline", "repo": REPO, "issue": TICKET, "status": "running",
                                "started": "2026-11-24T03:00:00Z"})
        action = check_action(self.root, first, "unit fixture", 1)
        self.assertIs(action["result"]["passed"], False)
        check_action(self.root, first, "unit other", 1)
        check_action(self.root, first, "unit fixture", 0)
        self.assertEqual(self.updates(), [])
        runs.move(first, "failed", expect=("running",))
        runs.insert_run(retry, {"kind": "pipeline", "repo": REPO, "issue": TICKET, "status": "running",
                                "started": "2026-11-24T04:00:00Z"})
        self.restart("", producer="checks")
        update, = self.updates()
        self.assertEqual(update["kind"], "recurring-blocker")
        self.assertEqual(update["attempt"], f"{first}/d0004-0")
        self.assertEqual(update["message"], "Check unit failed again (exit 1).")
