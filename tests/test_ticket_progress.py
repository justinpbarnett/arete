import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import contextmanager, nullcontext
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from arete import config, dbstats, environment_gaps, flows, gh, oncall, pilot, pipeline
from arete import paths, poll, routing, run_lines, scrub, tasks, tracker, util, vault, zte
from arete.store import db
from arete.store import orchestration as ledger
from arete.store import runs, sources, ticket_progress, unblock
from tests.support import workspace_state

REPO, TICKET = "arete", 964
PARENT, RETRY = "pip-source", "pip-retry"
GITHUB = "owner/arete"
DRAFT = f"https://github.com/{GITHUB}/pull/50"
FOLLOW_UP, READY, INTERRUPTED = 965, 966, 967
STARTED, FINISHED = "2026-11-24T02:00:00Z", "2026-11-24T02:10:00Z"
HEAD, RELEASE, MERGE = (letter * 40 for letter in "abc")
SIG = environment_gaps.Signature("exit 127", "fixture")


def issue_page(**fields):
    return {"data": {"repository": {"issue": fields}}}


def poll_fixture(root, state, *, dry_run=False, can_start=False, preflight=None,
                 dependencies=False, publish_unblock=False, at_capacity=False, held=None):
    repo = config.Repo(REPO, root, github=GITHUB, parts=[config.Part("", GITHUB)])
    cfg = config.Config(repos=[repo], unblock_repos=(REPO,) if publish_unblock else ())
    labels = [tracker.LABEL, "agent:pr"] if at_capacity and held is None else [tracker.LABEL]
    number = FOLLOW_UP if dependencies else TICKET
    ticket = dict(number=number, title="Follow-up", state="OPEN", author={"login": "owner"},
                  labels=[{"name": label} for label in labels], createdAt=FINISHED,
                  body="" if state == "removed" else "Waits on #41\nUnrelated: #99")
    listed = {number: ticket}
    if held is not None:
        listed[READY] = dict(ticket, number=READY, title="Ready", body="", createdAt=STARTED)
    posted = root / "github-comments.json"
    comments = json.loads(posted.read_text()) if posted.exists() else []
    if state == "owner-took-over":
        comments.append({"body": "I'll take over", "user": {"login": "owner"}, "created_at": STARTED})

    def comment(url, body, *, key, single_attempt):
        assert url == DRAFT and single_attempt, body
        comments.append({"body": gh.outgoing(body, mark=True, key=key)})
        posted.write_text(json.dumps(comments))
        if state == "lost-reply" and "needs owner review" in body:
            raise gh.GhUnsure("response lost after comment was accepted")

    def issue(_, number):
        if dependencies and number == FOLLOW_UP and state == "issue-unreadable":
            raise gh.GhError("cannot confirm ticket state")
        if dependencies and number == FOLLOW_UP and state == "closed":
            return {"state": "CLOSED"}
        opened = dependencies or (number == 41 and state == "tracker-wait")
        return listed.get(number, {"state": "OPEN" if opened else "CLOSED"})

    def issues(_, label, **_kw):
        if label != tracker.LABEL:
            return []
        if state == "list-failed":
            raise gh.GhError("cannot list tickets")
        return [] if state in ("partial", "issue-unreadable", "closed") else list(listed.values())

    closure_calls = 0

    def github(args):
        nonlocal closure_calls
        if args[:2] == ["pr", "view"]:
            assert args[2] == DRAFT
            return {"state": "OPEN", "isDraft": state != "ready", "headRefOid": HEAD}
        number, = [int(a.split("=")[1]) for a in args if a.startswith("number=")]
        pr = {"repository": {"nameWithOwner": "Owner/Arete" if number == 40 else GITHUB}, "baseRefName": "main"}
        error = {"errors": [{"message": "temporarily unreadable prerequisite"}]}
        if "--paginate" not in args:
            assert number == 40
            closure_calls += 1
            unreadable = state == "closure-unreadable" or (state == "release-unreadable" and closure_calls == 2)
            closer = dict(pr, __typename="PullRequest", merged=True, mergeCommit={"oid": MERGE})
            page = issue_page(state="CLOSED", stateReason="COMPLETED", timelineItems={"nodes": [{"closer": closer}]})
            return {**page, **error} if unreadable else page
        if state == "unreadable":
            return [{"data": {"repository": ["malformed linked PR response"]}}]
        if state in ("diverged", "tracker-wait"):
            return [error]
        assert number in ((41,) if dependencies else (40, 41)), number
        opened = state == "open"
        pulls = [dict(pr, number=49, state="OPEN" if opened else "MERGED", mergeCommit=None if opened else {"oid": MERGE})]
        if not dependencies:
            pulls.append(dict(pr, number=50, state="OPEN"))
        return [issue_page(closedByPullRequestsReferences={"nodes": pulls})]

    hold = environment_gaps.Hold([environment_gaps.Gap(SIG, "", "", "")], {DRAFT: HEAD})
    linked = Mock(side_effect=github)
    with (
        patch.multiple(environment_gaps, hold_of=lambda _: hold, release_base=lambda _: "main",
                       live_release=lambda: "" if state in ("open", "merged", "removed") else RELEASE),
        patch.multiple(gh, issue=issue, me=lambda: "owner", pr_comment=comment, gh_json=linked,
                       pr_discussion=lambda _: gh.Discussion([], [], comments),
                       branch_contains=lambda _, base, head: (base, head) == ("main", RELEASE)
                       or (state != "diverged" and (base, head) == (RELEASE, MERGE))),
        patch.object(flows, "check_environment_workspace", side_effect=preflight),
        patch.object(pipeline, "recorded_path", return_value="classic"),
        patch.object(oncall, "parked", return_value=set()), patch.object(pilot, "is_pilot_task", return_value=False),
        patch.object(zte, "reserved", return_value=False), patch.object(tasks, "pending_start", return_value=False),
    ):
        if not dependencies:
            for row in runs.environment_holds(REPO):
                return environment_gaps.handle(cfg, repo, row, dry_run, can_start=can_start)
            return False
        layer = config.RepoLayer(root)
        queue = poll.RepoQueue(repo, tracker.GitHubTracker(repo), set(), layer,
                               routing.Discovery(cfg, repo, priced=False, layer=layer))
        skips, errors = pipeline.SkipLog({}), []
        start, spawn = Mock(wraps=poll.start_pick), Mock()
        with patch.object(gh, "issues", issues), patch.multiple(poll, start_pick=start, spawn_unparked=spawn):
            selected = poll.fill_slots(cfg, [queue], skips, dry_run, errors, Counter(), 0 if at_capacity else 1)
        assert not selected and bool(errors) == (state == "list-failed"), errors
        assert [call.args[2].tid for call in start.call_args_list] == ([] if held is None else [str(held)])
        spawn.assert_not_called()
        assert linked.call_count == (not dry_run and state in ("open", "merged", "deployed"))
        return skips.current


poll_dependencies = partial(poll_fixture, dependencies=True)


def admit(lease, *calls):
    turn = ledger.begin(lease, {}, None)
    return ledger.accept(lease, turn, ledger.snapshot(lease)["version"], {"calls": list(calls)}, None)


def check_action(root, lease, command, code):
    repo = config.Repo(REPO, root)
    layer = config.RepoLayer(root, checks={"unit": command})
    run = SimpleNamespace(id=lease.run, config=config.Config(repos=[repo]), repo=repo, time_for=lambda limit: limit,
                          log=lambda _: None)
    action, = admit(lease, {"op": "checks.run", "args": {"scope": "full"}})
    ledger.transition(lease, action["id"], "running", {})
    result = subprocess.CompletedProcess([], code, "### unrelated failed: `fake command` exit 8", "")
    with (patch.object(pipeline, "check_lane", return_value=nullcontext()),
          patch.object(scrub, "secrets", return_value=()), patch.object(pipeline.environment, "sh", return_value=result)):
        passed, _ = pipeline.run_checks(layer, root, [], run, 1)
    return ledger.transition(lease, action["id"], "succeeded", {"passed": passed})


@contextmanager
def progress_workspace(root):
    with workspace_state(root), patch.object(dbstats.Timing, "finished"), patch.object(vault, "values", return_value=set()):
        yield


class ProgressFixture(unittest.TestCase):
    issue, started = TICKET, STARTED
    def setUp(self):
        parent = paths.scratch_root(paths.STATE_DIR) if paths.ISOLATED else None
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=parent))).resolve()
        self.enterContext(progress_workspace(self.root))
        db.migrate()

    def run_row(self, run_id, status="running", **fields):
        runs.insert_run(run_id, dict(kind="pipeline", repo=REPO, issue=self.issue, status=status, started=self.started) | fields)

    def fields(self, row, **expected):
        self.assertEqual({key: row[key] for key in expected}, expected)
        return row


class TicketProgressTests(ProgressFixture):
    def setUp(self):
        super().setUp()
        self.comments = self.root / "github-comments.json"
        self.run_row(PARENT, status="pr", pr_url=DRAFT, branch="fixture", started="2026-11-24T01:00:00Z")
        runs.save_environment_hold(PARENT, [(GITHUB, 50, DRAFT, HEAD)])
        runs.set_environment_waits(PARENT, {SIG.key: (GITHUB, 40)})

    def restart(self, state, producer="hold"):
        child = subprocess.run([sys.executable, "-m", "tests.test_ticket_progress", str(self.root), state, producer],
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)

    def poll_hold(self, state, **options):
        return poll_fixture(self.root, state, **options)

    def assert_restart(self):
        before = self.progress(), self.comments.read_text()
        self.restart("deployed")
        self.assertEqual((self.progress(), self.comments.read_text()), before)

    def settle_retry(self, *steps, status="pr", started=False, summary=""):
        if not started:
            self.run_row(RETRY, environment_retry_of=PARENT)
        for step, result, *data in steps:
            run_lines.write(RETRY, "step_end", step=step, result=result, data=data[0] if data else {})
        self.assertIsNotNone(runs.settle(RETRY, {"status": status, "pr_url": DRAFT, "finished": FINISHED, "summary": summary}))

    def progress(self, ticket=TICKET, **expected):
        return self.fields(ticket_progress.get(REPO, ticket), **expected)

    def hold(self, **expected):
        return self.fields(runs.environment_hold(PARENT), **expected)

    def updates(self, ticket=TICKET):
        with db.reader() as c:
            rows = list(ticket_progress.rows(c, REPO, ticket))
        return [u for row in reversed(rows) for u in json.loads(row["data"])["updates"]]

    def assert_pr_update(self, ticket=TICKET):
        update, = self.updates(ticket)
        return self.fields(update, kind="prerequisite-pr", pr=f"{GITHUB}#49")

    def test_held_prerequisite_discovery_and_admission(self):
        for state, pr_state, release in (("open", "open", ""), ("merged", "merged", ""), ("deployed", "merged", RELEASE)):
            self.poll_hold(state)
            pull, = self.progress(follow_up={"state": "waiting", "prs": [DRAFT], "draft": True})["prerequisites"]
            self.fields(pull, key=f"{GITHUB}#49", state=pr_state, deployed_release=release,
                        issues=[f"{GITHUB}#40", f"{GITHUB}#41"])
        pulls = self.progress(next_event="Automatic verification retry starts when eligible")["prerequisites"]
        preflight = Mock()
        with patch.object(tasks, "resume") as resume:
            for state in ("diverged", "tracker-wait"):
                self.assertFalse(self.poll_hold(state, can_start=True, preflight=preflight), state)
            preflight.assert_not_called()
            self.assertTrue(self.poll_hold("unreadable", can_start=True, preflight=preflight))
            saved = self.hold()
            self.run_row("pip-force-start", status="noop")
            self.assertFalse(self.poll_hold("deployed", can_start=True))
            self.assertEqual(self.hold(), saved)
        self.assertEqual((resume.call_count, resume.call_args.args[:2]), (1, (PARENT, "verify")))
        self.fields(resume.call_args.kwargs, environment_release=RELEASE)
        self.assertEqual(preflight.call_args.args[2], {DRAFT: HEAD})
        self.progress(prerequisites=pulls)
        self.assert_pr_update()

    def test_unreadable_closure_keeps_the_wait_notice_and_saved_links(self):
        self.poll_hold("deployed")
        saved, pulls = self.hold(), self.progress()["prerequisites"]
        self.comments.unlink()
        resume, search = Mock(), Mock()
        with patch.object(tasks, "resume", resume), patch.object(gh, "issues", search):
            for state in ("closure-unreadable", "release-unreadable"):
                self.assertFalse(self.poll_hold(state, can_start=True))
        self.assertEqual((resume.call_count, search.call_count, self.hold()), (0, 0, saved))
        self.progress(prerequisites=pulls, next_event="The prerequisite PR state can be confirmed")
        self.assertIn("Verification is waiting on #40", self.comments.read_text())

    def test_manual_release_without_retry_persists_end_and_preserves_links(self):
        self.poll_hold("deployed")
        saved, pulls = self.hold(), self.progress()["prerequisites"]
        with patch.object(util, "now", return_value=util.parse_iso(STARTED)):
            self.assertFalse(self.poll_hold("owner-took-over", dry_run=True))
            self.assertEqual(self.hold(), saved)
            self.assertFalse(self.poll_hold("owner-took-over"))
        self.assertEqual(self.hold(), {**saved, "released": 1, "released_at": STARTED, "release_state": "inactive"})
        self.progress(active=False, prerequisites=pulls)

    def test_same_cause_requires_a_retry_and_deduplicates_after_lost_reply(self):
        for _ in range(2):
            self.poll_hold("deployed")
        first = self.assert_pr_update()
        self.run_row(RETRY, environment_retry_of=PARENT)
        self.poll_hold("deployed")
        self.progress(blocker=None, next_event="Automatic verification finishes")
        outcome = {"ending": "environment-held", "verified": {"checks": [
            {"cause": "environment", "saw": "exit 127 for 'fixture'"}]}}
        self.settle_retry(("verify", "stop", outcome), started=True)
        released, ended = "2026-11-24T02:15:00Z", "2026-11-24T04:00:00Z"
        with patch.object(util, "now", return_value=util.parse_iso(released)), self.assertRaises(gh.GhUnsure):
            self.poll_hold("lost-reply")
        self.assert_review_blocker({SIG.key: SIG.title}, attempt=RETRY)
        saved = self.hold(released_at=released, release_state="owner-review", retry_started_at=STARTED,
                          owner_review_started_at=FINISHED, owner_review_ended_at=None)
        self.assert_restart()
        self.assertEqual(self.hold(), saved)
        first_after, repeat = self.updates()
        self.assertEqual(first_after, first)
        self.fields(repeat, kind="recurring-blocker", attempt=RETRY)
        with patch.object(util, "now", return_value=util.parse_iso(ended)):
            self.poll_hold("owner-took-over")
        self.hold(owner_review_ended_at=ended)
        self.progress(active=False)

    def assert_review_blocker(self, causes, **expected):
        progress = self.progress(active=True, **expected)
        self.fields(progress["follow_up"], state="owner-review")
        self.fields(progress["blocker"], causes=causes)
        self.assertIn("Owner reviews", progress["next_event"])

    def test_held_retry_reports_checks_without_reposting_the_legacy_notice(self):
        self.settle_retry(("verify", "pass"), ("checks", "fail"))
        key = "environment-gap-" + hashlib.sha256(f"{DRAFT}\n{RETRY}-retry".encode()).hexdigest()
        legacy = json.dumps([{"body": gh.outgoing("", mark=True, key=key)}])
        self.comments.write_text(legacy)
        self.poll_hold("deployed")
        self.assert_review_blocker({"checks": "Configured checks did not pass"})
        self.assertEqual(self.comments.read_text(), legacy)
        self.assert_restart()

    def test_held_retry_reports_the_recorded_final_review_reason_after_checks_pass(self):
        why = "the restored draft did not pass review"
        self.settle_retry(("verify", "pass"), ("checks", "fail"), ("checks", "pass"),
                          ("review", "fail", {"review": {"verdict": "request_changes"}}), summary="Follow-up draft")
        run_lines.write(RETRY, "report", status="pr", held=why)
        self.poll_hold("deployed")
        self.assert_review_blocker({"review": why})
        self.assertIn(why, self.comments.read_text())
        self.poll_hold("ready")
        self.progress(active=False)

    def test_retry_that_requires_input_names_the_owner_decision_and_keeps_the_draft(self):
        why = "verification retry requires owner input"
        self.settle_retry(("verify", "pass"), summary=why)
        self.poll_hold("deployed")
        self.assert_review_blocker({"owner": why},
                                   next_event="Owner reviews the draft and decides whether to provide the missing input or take over")
        self.hold(release_state="owner-review")
        self.assertIn(why, self.comments.read_text())
        self.assert_restart()

    def test_unsettled_verification_evidence_does_not_override_stopped_retry(self):
        self.settle_retry(("verify", "stop", {"ending": "environment-held"}), status="stopped")
        self.poll_hold("deployed")
        self.fields(self.progress(active=False)["follow_up"], state="retry-finished")
        self.assertFalse(self.comments.exists())

    def test_retry_preflight_posts_the_wait_notice_first_and_reports_the_workspace_blocker(self):
        preflight = flows.check_environment_workspace
        with patch.object(tasks, "resume") as resume:
            self.assertFalse(self.poll_hold("deployed", can_start=True, dry_run=True, preflight=preflight))
            self.assertEqual((self.progress(), self.comments.exists()), ({}, False))
            with self.assertRaisesRegex(OSError, "preflight interrupted"):
                self.poll_hold("deployed", can_start=True, preflight=OSError("preflight interrupted"))
            posted = self.comments.read_text()
            self.assertIn("Verification is waiting on #40", posted)
            self.assertFalse(self.poll_hold("deployed", can_start=True, preflight=preflight))
        resume.assert_not_called()
        blocked = self.progress(active=True)
        self.fields(blocked["blocker"], kind="retry-workspace", causes={"workspace": "the held PRs do not match the saved branch"})
        self.assertIn("workspace becomes restorable", blocked["next_event"])
        self.assertEqual(self.comments.read_text(), posted)

    def assert_capacity_scan(self, held):
        options = dict(at_capacity=True, held=held, publish_unblock=True)
        self.assertEqual(poll_dependencies(self.root, "open", dry_run=True, **options), {})
        self.assertEqual((self.progress(FOLLOW_UP), self.updates(FOLLOW_UP), unblock.queue(REPO)), ({}, [], {}))
        self.assertEqual(sources.tickets_passed_over(), [])
        skips = poll_dependencies(self.root, "open", **options)
        self.assertIn("waits on #41", skips[f"{REPO}#{FOLLOW_UP}"]["why"])
        self.assertEqual([row["issue"] for row in sources.tickets_passed_over()], [str(held)])
        self.assertEqual(unblock.queue(REPO)["github"]["graph"][str(FOLLOW_UP)], ["41"])
        opened = self.progress(FOLLOW_UP, active=False, prerequisite_issues={f"{GITHUB}#41": True})
        self.fields(opened["prerequisites"][0], state="open")
        update = self.assert_pr_update(FOLLOW_UP)
        poll_dependencies(self.root, "open", **options)
        self.restart("open", producer=str(held))
        self.assertEqual((self.progress(FOLLOW_UP), self.updates(FOLLOW_UP)), (opened, [update]))
        return update

    def test_capacity_scans_behind_an_interrupted_pick(self):
        self.run_row("pip-interrupted", issue=INTERRUPTED, status="interrupted")
        self.assert_capacity_scan(INTERRUPTED)

    def test_capacity_scans_behind_a_fresh_pick(self):
        update = self.assert_capacity_scan(READY)
        self.assertIn("waits on #41", poll_dependencies(self.root, "deployed")[f"{REPO}#{FOLLOW_UP}"]["why"])
        self.fields(self.progress(FOLLOW_UP)["prerequisites"][0], state="merged", deployed_release=RELEASE)
        self.assertEqual(self.updates(FOLLOW_UP), [update])

    def test_real_check_action_results_record_recurrence_across_restart_and_continuation(self):
        first = "pip-check-root"
        self.run_row(first)
        lease = ledger.acquire(first, "", {})
        for command, code in (("unit fixture", 1), ("unit other", 1), ("unit fixture", 0)):
            action = check_action(self.root, lease, command, code)
            self.assertIs(action["result"]["passed"], code == 0)
        self.assertEqual(self.updates(), [])
        runs.move(first, "failed", expect=("running",))
        self.run_row("pip-check-resume", started=FINISHED)
        self.restart("", producer="checks")
        update, = self.updates()
        self.fields(update, kind="recurring-blocker", attempt=f"{first}/d0004-0", message="Check unit failed again (exit 1).")


if __name__ == "__main__":
    root, state, producer = Path(sys.argv[1]), *sys.argv[2:]
    with progress_workspace(root):
        if producer.isdecimal():
            poll_dependencies(root, state, at_capacity=True, held=int(producer))
        elif producer == "checks":
            check_action(root, ledger.acquire("pip-check-resume", "pip-check-root", {}), "unit fixture", 1)
        else:
            poll_fixture(root, state)
