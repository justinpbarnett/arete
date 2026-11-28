import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from arete import config, dbstats, environment_gaps, flows, gh, oncall, pilot, pipeline
from arete import paths, poll, routing, run_lines, slots, tasks, tracker, util, vault, zte
from arete.store import db
from arete.store import orchestration as ledger
from arete.store import runs, sources, ticket_progress
from tests.support import seed_db, workspace_state

REPO, TICKET = "arete", 964
PARENT, RETRY = "pip-source", "pip-retry"
GITHUB = "owner/arete"
DRAFT = f"https://github.com/{GITHUB}/pull/50"
FOLLOW_UP, READY, INTERRUPTED = 965, 966, 967
STARTED, FINISHED = "2026-11-24T02:00:00Z", "2026-11-24T02:10:00Z"
HEAD, RELEASE, MERGE = (letter * 40 for letter in "abc")
SIG = environment_gaps.Signature("exit 127", "fixture")
HELD_VERIFY = ("verify", "stop", {"ending": "environment-held", "verified": {"checks": [
    {"cause": "environment", "saw": "exit 127 for 'fixture'"}]}})


def poll_fixture(root, state, *, dry_run=False, can_start=False, preflight=None, dependencies=False, at_capacity=False, held=None):
    repo = config.Repo(REPO, root, github=GITHUB, parts=[config.Part("", GITHUB)])
    cfg = config.Config(repos=[repo])
    number = FOLLOW_UP if dependencies else TICKET
    labels = (tracker.LABEL, "agent:pr") if at_capacity and held is None else (tracker.LABEL,)
    ticket = {"number": number, "title": "Follow-up", "state": "OPEN", "author": {"login": "owner"}, "createdAt": FINISHED,
                  "labels": [{"name": label} for label in labels], "body": "" if state == "removed" else "Waits on #41\nUnrelated: #99"}
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
        if number == FOLLOW_UP and state == "closed":
            return {"number": number, "state": "CLOSED"}
        return listed.get(number, {"number": number, "state": "OPEN" if dependencies else "CLOSED"})

    unreadable = gh.GhError("temporarily unreadable closure")
    closed = Mock(return_value=MERGE, side_effect={"closure-unreadable": unreadable,
                                                   "release-unreadable": [MERGE, unreadable]}.get(state))

    def pulls(_, number):
        assert number in ((41,) if dependencies else (40, 41)), number
        found = {(GITHUB, 49): ("open", "main", "") if state == "open" else ("merged", "main", MERGE)}
        return found if dependencies else {**found, (GITHUB, 50): ("open", "main", "")}

    def gh_json(args, **_kw):
        if args and args[0] == "api":
            path = args[1]
            if path.endswith("/parent"):
                raise gh.GhError("HTTP 404: Not Found")
            num = int(path.rsplit("/", 1)[-1])
            row = issue(GITHUB, num)
            return {"id": num, "number": num, "title": row.get("title", ""), "body": row.get("body", ""),
                    "labels": row.get("labels", []), "state": (row.get("state") or "OPEN").lower(),
                    "repository_url": f"{gh.GITHUB_API}/repos/{GITHUB}"}
        return {"state": "OPEN", "isDraft": state != "ready", "headRefOid": HEAD}

    hold = environment_gaps.Hold([environment_gaps.Gap(SIG, "", "", "")], {DRAFT: HEAD})
    linked = Mock(side_effect=pulls)
    with (
        patch.multiple(environment_gaps, hold_of=lambda _: hold, release_base=lambda _: "main",
                       live_release=lambda: "" if state in ("open", "merged", "removed") else RELEASE),
        patch.multiple(gh, issue=issue, me=lambda: "owner", pr_comment=comment,
                       issue_merge_commit=closed, prerequisite_pulls=linked,
                       gh_json=gh_json,
                       pr_discussion=lambda _: gh.Discussion([], [], comments),
                       branch_contains=lambda _, base, head: (base, head) in (("main", RELEASE), (RELEASE, MERGE))),
        patch.object(flows, "check_environment_workspace", side_effect=preflight),
        patch.object(pipeline, "recorded_path", return_value="classic"),
        patch.object(oncall, "parked", return_value=set()), patch.object(pilot, "is_pilot_task", return_value=False),
        patch.object(zte, "reserved", return_value=False), patch.object(tasks, "pending_start", return_value=False),
    ):
        if not dependencies:
            if at_capacity:
                errors = []
                queue = SimpleNamespace(repo=repo, tracker=tracker.GitHubTracker(repo), selected=set())
                assert not poll.resume_environment_holds(cfg, [queue], dry_run, errors, Counter(), 0)
                assert not errors, errors
                return False
            row = next(iter(runs.environment_holds(REPO)), None)
            return bool(row and environment_gaps.handle(cfg, repo, row, dry_run, can_start=can_start))
        layer = config.RepoLayer(root)
        queue = poll.RepoQueue(repo, tracker.GitHubTracker(repo), set(), layer,
                               routing.Discovery(cfg, repo, priced=False, layer=layer))
        skips, errors = pipeline.SkipLog({}), []
        tickets = [] if state in ("partial", "closed") else list(listed.values())
        with (patch.object(gh, "issues", side_effect=lambda _, label, **kw: tickets if label == tracker.LABEL else []),
              patch.object(poll, "spawn_unparked", side_effect=AssertionError("unexpected start"))):
            assert not poll.fill_slots(cfg, [queue], skips, dry_run, errors, Counter(), 0 if at_capacity else 1)
        assert not errors, errors
        assert queue.selected == (set() if held is None else {str(held)})
        assert linked.call_count == (not dry_run and state in ("open", "merged", "deployed"))
        return skips.current


poll_dependencies = partial(poll_fixture, dependencies=True)


def admit(lease, *calls):
    turn = ledger.begin(lease, {}, None)
    return ledger.accept(lease, turn, ledger.snapshot(lease)["version"], {"calls": list(calls)}, None)


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
        seed_db(self.root / "state")

    def run_row(self, run_id, status="running", **fields):
        runs.insert_run(run_id, {"kind": "pipeline", "repo": REPO, "issue": self.issue, "status": status, "started": self.started} | fields)

    def fields(self, row, **expected):
        self.assertEqual({key: row[key] for key in expected}, expected)
        return row

    def events(self, ticket):
        with db.reader() as c:
            return [json.loads(row["data"]) for row in reversed(list(ticket_progress.rows(c, REPO, ticket)))]


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

    def assert_restart(self, producer="hold"):
        def snapshot():
            return self.progress(), self.comments.read_bytes() if self.comments.exists() else None

        before = snapshot()
        self.restart("deployed", producer)
        self.assertEqual(snapshot(), before)

    def start_retry(self):
        self.run_row(RETRY, environment_retry_of=PARENT)
        self.poll_hold("deployed")
        self.progress(active=True, blocker=None, next_event="Automatic verification finishes")

    def settle_retry(self, *steps, status="pr", summary=""):
        if not runs.run_get(RETRY):
            self.run_row(RETRY, environment_retry_of=PARENT)
        for step, result, *data in steps:
            run_lines.write(RETRY, "step_end", step=step, result=result, data=data[0] if data else {})
        self.assertIsNotNone(runs.settle(RETRY, {"status": status, "pr_url": DRAFT if status == "pr" else "",
                                               "finished": FINISHED, "summary": summary}))

    def progress(self, ticket=TICKET, **expected):
        return self.fields(ticket_progress.get(REPO, ticket), **expected)

    def hold(self, **expected):
        return self.fields(runs.environment_hold(PARENT), **expected)

    def updates(self, ticket=TICKET):
        return [u for row in self.events(ticket) for u in row["updates"]]

    def assert_pr_update(self, ticket=TICKET):
        update, = self.updates(ticket)
        return self.fields(update, kind="prerequisite-pr", pr=f"{GITHUB}#49")

    def test_unreadable_closure_keeps_the_wait_notice_and_saved_links(self):
        self.poll_hold("deployed")
        saved, pulls = self.hold(), self.progress()["prerequisites"]
        self.comments.unlink()
        with patch.object(tasks, "resume") as resume, patch.object(gh, "issues", return_value=[]) as search:
            for state in ("closure-unreadable", "release-unreadable"):
                self.assertFalse(self.poll_hold(state, can_start=True))
            self.assertEqual((resume.call_count, search.call_count, self.hold()), (0, 0, saved))
        self.progress(prerequisites=pulls, next_event="The prerequisite PR state can be confirmed")
        self.assertIn("Verification is waiting on #40", self.comments.read_text())

    def test_same_cause_requires_a_retry_and_deduplicates_after_lost_reply(self):
        for _ in range(2):
            self.poll_hold("deployed")
        first = self.assert_pr_update()
        self.start_retry()
        self.settle_retry(HELD_VERIFY)
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
        self.assertEqual(ticket_progress.owner_review_started_at(REPO, TICKET, PARENT, RETRY), released)
        self.assertIsNone(ticket_progress.owner_review_started_at(REPO, TICKET, PARENT, PARENT))

    def assert_review_blocker(self, causes, **expected):
        progress = self.progress(active=True, **expected)
        self.fields(progress["follow_up"], state="owner-review")
        self.fields(progress["blocker"], causes=causes)
        self.assertIn("Owner reviews", progress["next_event"])

    def test_retry_reports_the_latest_failed_gate_without_reposting_the_legacy_notice(self):
        self.settle_retry(("verify", "pass"), ("checks", "fail"))
        blocker, _ = environment_gaps.retry_blocker(runs.run_get(RETRY))
        self.fields(blocker, kind="configured-checks", causes={"checks": "Configured checks did not pass"})
        why = "the restored draft did not pass review"
        run_lines.write(RETRY, "step_end", step="checks", result="pass")
        run_lines.write(RETRY, "step_end", step="review", result="fail")
        run_lines.write(RETRY, "report", status="pr", held=why)
        key = "environment-gap-" + hashlib.sha256(f"{DRAFT}\n{RETRY}-retry".encode()).hexdigest()
        legacy = json.dumps([{"body": gh.outgoing("", mark=True, key=key)}])
        self.comments.write_text(legacy)
        self.poll_hold("deployed")
        self.assert_review_blocker({"review": why})
        self.assertEqual(self.comments.read_text(), legacy)

    def test_retry_that_requires_input_names_the_owner_decision_and_keeps_the_draft(self):
        why = "verification retry requires owner input"
        self.settle_retry(("verify", "pass"), summary=why)
        self.poll_hold("deployed")
        self.assert_review_blocker({"owner": why},
                                   next_event="Owner reviews the draft and decides whether to provide the missing input or take over")
        self.hold(release_state="owner-review")
        self.assertIn(why, self.comments.read_text())

    def test_interrupted_retry_keeps_the_draft_and_owner_action_across_restart(self):
        self.start_retry()
        self.settle_retry(HELD_VERIFY, status="interrupted")
        with patch.object(tasks, "resume") as resume:
            self.assertFalse(self.poll_hold("deployed", can_start=True))
        resume.assert_not_called()
        self.hold(released=0, release_state=None)
        next_event = "Owner reviews the draft and decides whether to verify it manually or take over the PR"
        saved = self.progress(active=True, run=PARENT, attempt=RETRY, next_event=next_event,
                              follow_up={"state": "owner-review", "prs": [DRAFT], "draft": True})
        self.fields(saved["blocker"], kind="retry-interrupted",
                    causes={"retry": "Automatic verification retry was interrupted"})
        self.poll_hold("deployed")
        self.assertEqual(self.progress(), saved)
        self.assert_restart()
        self.poll_hold("ready", dry_run=True)
        self.hold(released=0)
        self.poll_hold("ready")
        self.hold(released=1, release_state="inactive")
        self.progress(active=False, prerequisites=saved["prerequisites"])

    def test_unsettled_verification_evidence_does_not_override_stopped_retry(self):
        self.run_row(RETRY, environment_retry_of=PARENT, stop_asked=STARTED)
        self.settle_retry(HELD_VERIFY, status="stopped")
        self.poll_hold("deployed")
        self.assert_review_blocker({"retry": "Automatic verification retry was stopped"})
        self.assert_pr_update()

    def test_ready_retry_reports_run_capacity_and_starts_when_a_slot_frees(self):
        no_slot = slots.no_free_slot(config.Config().max_parallel)
        race = "another run took the slot"
        with patch.object(tasks, "resume", side_effect=[slots.NoFreeSlot(race), {"state": "queued"}, {"state": "starting"}]) as resume:
            self.assertFalse(self.poll_hold("merged", can_start=True))
            self.fields(self.progress()["prerequisites"][0], state="merged", deployed_release="",
                        issues=[f"{GITHUB}#40", f"{GITHUB}#41"])
            for reason in (no_slot, race, tasks.NO_SLOT_AT_START):
                capacity = reason == no_slot
                self.assertFalse(self.poll_hold("deployed", at_capacity=capacity, can_start=not capacity))
                saved = self.progress(active=True, next_event="A run slot becomes available",
                                      follow_up={"state": "waiting", "prs": [DRAFT], "draft": True},
                                      blocker={"kind": "retry-slot", "causes": {"slot": reason}})
                if capacity:
                    resume.assert_not_called()
                    self.fields(saved["prerequisites"][0], deployed_release=RELEASE)
                    with patch.object(poll, "MAX_STARTS_PER_POLL", 0):
                        self.poll_hold("deployed", at_capacity=True)
                    self.assertEqual(self.progress(), saved)
                    self.assert_restart("hold-capacity")
            self.assertTrue(self.poll_hold("deployed", can_start=True))
        self.assertEqual(resume.call_args.args[:2], (PARENT, "verify"))
        self.fields(resume.call_args.kwargs, environment_release=RELEASE)
        self.assertNotEqual(self.progress()["blocker"]["kind"], "retry-slot")
        self.assert_pr_update()

    def test_retry_preflight_posts_the_wait_notice_first_and_reports_the_workspace_blocker(self):
        check_workspace = flows.check_environment_workspace

        def preflight(*args):
            self.assertIn("Verification is waiting on #40", self.comments.read_text())
            check_workspace(*args)

        with patch.object(tasks, "resume") as resume:
            self.assertFalse(self.poll_hold("deployed", at_capacity=True, preflight=preflight))
        resume.assert_not_called()
        saved = self.progress(active=True)
        self.fields(saved["blocker"], kind="retry-workspace", causes={"workspace": "the held PRs do not match the saved branch"})
        self.assertIn("workspace becomes restorable", saved["next_event"])

    def assert_capacity_scan(self, held):
        options = {"at_capacity": True, "held": held}
        self.assertEqual(poll_dependencies(self.root, "open", dry_run=True, **options), {})
        self.assertEqual((self.progress(FOLLOW_UP), sources.tickets_passed_over()), ({}, []))
        skips = poll_dependencies(self.root, "open", **options)
        self.assertIn("waits on #41", skips[f"{REPO}#{FOLLOW_UP}"]["why"])
        self.assertEqual([row["issue"] for row in sources.tickets_passed_over()], [str(held)])
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


if __name__ == "__main__":
    root, state, producer = Path(sys.argv[1]), *sys.argv[2:]
    with progress_workspace(root):
        if producer.isdecimal():
            poll_dependencies(root, state, at_capacity=True, held=int(producer))
        else:
            poll_fixture(root, state, at_capacity=producer == "hold-capacity")
