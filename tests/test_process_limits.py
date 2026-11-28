import contextlib
import io
import json
import os
import select
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from string import Template
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from adws import feature
from arete import (
    codebase,
    config,
    control,
    dbstats,
    delegation,
    diffguard,
    environment_gaps,
    factory,
    flows,
    gh,
    jira,
    launch_lease,
    loops,
    orchestration,
    outcomes,
    paths,
    pilot,
    pipeline,
    poll,
    principals,
    prompt_loader,
    record,
    repairs,
    requeue,
    run_lines,
    runlimits,
    sandbox,
    scratch,
    slots,
    steps,
    tasks,
    tracker,
    unblock,
    unblock_watch,
    util,
    vault,
    workspace,
    zte,
    zte_issue,
    zte_release,
)
from arete.agent import Result, Session
from arete.lab import workflows
from arete.orchestration_api import MAX_BYTES, OPERATIONS, PROMPTS, PROTOCOL, Catalog, describe_operations
from arete.store import asks as ask_store
from arete.store import db as foundation
from arete.store import insights as insights_store
from arete.store import orchestration as ledger
from arete.store import runs as run_store
from arete.store import tasks as tasks_store
from arete.store import unblock as recovery_store
from arete.store import worker_exchange as exchange
from arete.store import zte as zte_store
from arete.zte_github import Identity, IssueApp
from tests import support
from tests.harness import set_task_body
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT


class PinCommandTests(unittest.TestCase):
    def test_pin_commands(self):
        from tests.test_behavior import CliThroughServeRunTests

        CliThroughServeRunTests().in_subprocess("check_pin_commands")


class Isolated(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="arete-schema-")
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name)
        cls.template = root / "state/arete.db"
        with contextlib.ExitStack() as patches:
            patches.enter_context(patch.object(foundation, "_CHECKED", set()))
            patches.enter_context(patch.object(dbstats.Timing, "finished"))
            for name, value in {"STATE_DIR": root / "state", "DB_FILE": cls.template,
                                "RUNS_DIR": root / "runs", "WORKTREES_DIR": root / "worktrees",
                                "LOCKS_DIR": root / "state/locks", "LOG_DIR": root / "logs"}.items():
                patches.enter_context(patch.object(paths, name, value))
            support.seed_db(root / "state", db_file=cls.template)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="arete-limits-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(foundation, "_CHECKED", set()))
        self.patches.enter_context(patch.object(dbstats.Timing, "finished"))
        self.patches.enter_context(patch.object(tasks, "PENDING", {}))
        for name, value in {"STATE_DIR": self.root / "state", "RUNS_DIR": self.root / "runs",
                            "DB_FILE": self.root / "state/arete.db", "LOCKS_DIR": self.root / "state/locks",
                            "CONFIG_DIR": self.root / "config", "CONFIG_FILE": self.root / "config/config.toml",
                            "LOG_DIR": self.root / "logs", "EVENTS_FILE": self.root / "state/events.jsonl",
                            "SERVER_FILE": self.root / "state/server.json",
                            "WORKTREES_DIR": self.root / "worktrees"}.items():
            self.patches.enter_context(patch.object(paths, name, value))
        paths.CONFIG_DIR.mkdir()
        paths.ensure_dirs()
        shutil.copyfile(self.template, paths.DB_FILE)
        self.repo = config.Repo(name="fixture", path=self.root / "repo", tracker="local", pipeline=True)
        self.repo.path.mkdir()
        self.cfg = config.Config(repos=[self.repo], max_parallel=8, max_parallel_per_repo=8)
        self.ticket = tasks_store.task_add(self.repo.name, "Fixture specification", "Fixture", "feat")
        self.local = tracker.LocalTracker(self.repo)

    def run(self, result=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return super().run(result)

    def made_run(self, previous="", forced=False):
        rid = record.start("pipeline", repo=self.repo.name, issue=self.ticket)
        directory = paths.RUNS_DIR / rid
        directory.mkdir()
        source = tracker.for_ticket(self.cfg, self.repo, self.ticket)
        run = pipeline.Run(rid, self.repo, directory, tracker=source, tid=self.ticket, cfg=self.cfg)
        run.budget = runlimits.Budget(run, runlimits.Policy(), previous, source.get(self.ticket), forced)
        return run


class ZteRepoTests(Isolated):
    delivery_day = datetime(2026, 1, 3, 12, tzinfo=UTC)

    def setUp(self):
        super().setUp()
        self.ordinary_layer = self.root / "ordinary"
        self.zte_layer = self.root / "bounded"
        self.write_layer(self.ordinary_layer, workers=2, minutes=180, repairs=3)
        self.write_layer(self.zte_layer, workers=1, minutes=40, repairs=1)
        identity = Identity("owner/fixture", 1, "repo", 2, 3, 4, "a" * 40, 5, 6)
        self.policy = zte.Policy(identity, "## Before", "## After", True, True, "", True)

    def write_layer(self, directory, *, workers, minutes, repairs):
        directory.mkdir(exist_ok=True)
        (directory / "config.toml").write_text(f'''\
[factory]
path = "orchestrated"
[limits]
max_run_minutes = {minutes}
max_verify_minutes = 15
max_repair_cycles = {repairs}
[orchestration]
max_workers = {workers}
max_decisions = 16
max_actions = 64
[checks]
lint = "sh tools/lint-arete"
compile = "true"
imports = "true"
web = "true"
behavior = "true"
[routing]
payment_modes = ["subscription"]
fallback = false
[steps.orchestrate]
runner = "pi"
model = "openai/gpt-6.2-sol"
effort = "low"
[steps.verify]
runner = "pi"
[verify]
''')

    def write_config(self, *, zte_repo="arete-zte", tracker="local", path=None, github="owner/fixture",
                     alias_first=False):
        selection = "" if zte_repo is None else f"zte_repo = {json.dumps(zte_repo)}\n"
        ordinary = f'''\
[[repo]]
name = "arete"
path = {json.dumps(str(self.repo.path))}
github = "owner/fixture"
layer = {json.dumps(str(self.ordinary_layer))}
pipeline = true
'''
        alias = f'''\
[[repo]]
name = "arete-zte"
path = {json.dumps(str(path or self.repo.path))}
github = {json.dumps(github)}
layer = {json.dumps(str(self.zte_layer))}
tracker = {json.dumps(tracker)}
pipeline = true
'''
        entries = alias + ordinary if alias_first else ordinary + alias
        paths.CONFIG_FILE.write_text(f'{selection}arete_repo = "arete"\n{entries}')

    def test_uses_the_selected_repos_bounded_layer(self):
        self.write_config()
        cfg = config.load()
        repo, layer, _ = zte.settings(cfg, self.policy)

        self.assertEqual(repo, cfg.repo("arete-zte"))
        self.assertEqual(layer.dir, self.zte_layer)
        self.assertEqual((layer.orchestration.max_workers, layer.max_run_minutes, layer.max_repair_cycles), (1, 40, 1))

    def test_defaults_to_the_arete_repo_and_refuses_its_unbounded_layer(self):
        self.write_config(zte_repo=None)
        cfg = config.load()

        self.assertEqual(cfg.zte_repo, "arete")
        with self.assertRaisesRegex(pilot.Held, "ZTE requires the bounded subscription-only PR layer"):
            zte.settings(cfg, self.policy)

    def test_alternate_repo_requires_local_tracker_and_matching_path_and_github(self):
        for mismatch in ({"tracker": "github"}, {"path": self.root / "other"}, {"github": "owner/other"}):
            with self.subTest(**mismatch):
                self.write_config(**mismatch)
                with self.assertRaisesRegex(pilot.Held, "zte_repo 'arete-zte'"):
                    zte.settings(config.load(), self.policy)

    def test_unknown_zte_repo_is_refused_at_config_load(self):
        self.write_config(zte_repo="missing")
        with self.assertRaisesRegex(config.UnknownRepo, "zte_repo.*missing"):
            config.load()

    def test_zte_alias_must_be_listed_after_the_arete_repo(self):
        self.write_config(alias_first=True)
        with self.assertRaisesRegex(ValueError, "zte_repo 'arete-zte'.*arete_repo 'arete'"):
            config.load()

    def test_shared_checkout_resolves_to_the_arete_repo(self):
        self.write_config()
        cfg = config.load()
        checkout = self.repo.path
        ordinary = cfg.repo("arete")

        self.assertIs(cfg.repo_for_path(checkout), ordinary)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(checkout)
        for kind, repo in (("watch", "arete"), ("search", "arete"), ("watch", "")):
            with self.subTest(kind=kind, repo=repo):
                loop = loops.Loop(name="fixture", dir=self.root, origin="local", kind=kind, repo=repo)
                self.assertIs(loop.work_repo(cfg), ordinary)

    def test_selector_edit_preserves_ordinary_worker_and_workflow_policies(self):
        self.write_config(zte_repo=None)
        before = config.load()
        layer = config.load_layer(before.repo("arete").layer_dir)
        run = SimpleNamespace(id="worker", repo=before.repo("arete"), kind="", closed_context=())
        job = SimpleNamespace(run=run, ws=self.repo.path, cfg=before, layer=layer)
        launch = delegation.request(job, "respond", "Fixture", steps.resolve(before, layer, "build"), (), [])

        self.write_config()
        after = config.load()

        delegation.check_policy(launch, after, layer)
        self.assertEqual(delegation.legacy_settings_shas(before), delegation.legacy_settings_shas(after))
        self.assertEqual(workflows.fingerprint(before, layer), workflows.fingerprint(after, layer))
        for resume in (False, True):
            self.assertEqual(orchestration.policy_fingerprint(before, layer, (), resume=resume),
                             orchestration.policy_fingerprint(after, layer, (), resume=resume))

    def test_reserved_digest_binds_the_selector_even_if_both_repos_use_the_same_layer(self):
        self.write_config()
        cfg = config.load()
        cfg.repo("arete").layer = self.zte_layer
        repo, layer, reserved_digest = zte.settings(cfg, self.policy)
        fallback = replace(cfg, zte_repo="arete")
        fallback_repo, fallback_layer, fallback_digest = zte.settings(fallback, self.policy)

        self.assertEqual((repo.name, fallback_repo.name, fallback_layer), ("arete-zte", "arete", layer))
        self.assertEqual(fallback_digest, orchestration.policy_fingerprint(cfg, layer, ()))
        self.assertNotEqual(reserved_digest, fallback_digest)

    def test_removing_zte_repo_invalidates_an_admitted_delivery(self):
        self.write_config()
        cfg = config.load()
        repo, layer, digest = zte.settings(cfg, self.policy)
        deadline = util.iso(util.now() + timedelta(minutes=1))
        base = "b" * 40
        delivery = zte_store.reserve(self.policy.fingerprint, repo.name, base, deadline, pilot.TASK_TITLE,
                                     pilot.TASK_KIND, self.policy.signal(base).task(),
                                     {"config": digest, "engine": codebase.STAMP})
        with principals.acting("rule:poll"):
            run_id = record.start("pipeline", repo=repo.name, issue=delivery.task)
        tasks_store.task_set_state(delivery.task, "running")
        dispatch = zte_store.transition(delivery, "dispatching")
        running = zte_store.transition(dispatch, "running", run=run_id)
        controller = SimpleNamespace(run=SimpleNamespace(tid=delivery.task, closed_context=()),
                                     job=SimpleNamespace(cfg=cfg, layer=layer))
        policy_sha = orchestration.Controller.policy_sha(controller)
        self.assertEqual(zte.config_digest(cfg, policy_sha), digest)

        with (patch.object(zte, "load_policy", return_value=self.policy),
              patch.object(zte, "PAUSE_FILE", self.root / "pause")):
            self.assertEqual(zte.for_run(repo, delivery.task, run_id, "running"), (running, self.policy))

            self.write_config(zte_repo=None)
            with self.assertRaisesRegex(pilot.Held, "^the admitted ZTE config changed$"):
                zte.for_run(repo, delivery.task, run_id, "running")

    def production_policy(self, base, issue_number, *, reconciled_hold=""):
        before, after = zte.PRODUCTION_ISSUE_EDIT
        return replace(self.policy, before=before, after=after, rehearsal=False,
                       issue=zte_issue.Claim(issue_number, f"issue-{issue_number}", "e" * 64), close_after_health=True,
                       issue_app=IssueApp(7, 8, 9), production_issue_base=base, accept_issue_races=True,
                       reconciled_hold=reconciled_hold)

    def installation(self):
        return zte_release.Installation(self.root / "managed", "https://fixture.example:443",
                                        "com.arete.serve", foundation.LATEST)

    def reserve_under_arete(self, policy, *, days_ago):
        created = self.delivery_day - timedelta(days=days_ago)
        deadline = util.iso(created + timedelta(minutes=40))
        base = policy.production_issue_base
        with patch.object(util, "now", return_value=created):
            return zte_store.reserve(policy.fingerprint, "arete", base, deadline, pilot.TASK_TITLE, pilot.TASK_KIND,
                                     policy.signal(base).task(),
                                     {"github": asdict(policy.identity), "issue": asdict(policy.issue),
                                      "approved_installation": self.installation().to_receipt(),
                                      "production_issue_base": base, "close_after_health": True})

    def healthy_predecessor(self):
        delivery = self.reserve_under_arete(self.production_policy("a" * 40, 1), days_ago=2)
        with foundation.tx() as c:
            c.execute("UPDATE zte_deliveries SET phase = 'healthy' WHERE task = ?", (delivery.task,))
        zte_store.note_conforming(zte_store.find(delivery.task), "d" * 40)
        return zte_store.find(delivery.task)

    def find_production_delivery(self, policy, client):
        self.write_config()
        base = policy.production_issue_base
        client.tip.return_value = base
        policy_file = self.root / "zte.toml"
        policy_file.touch()
        with (patch.object(util, "now", return_value=self.delivery_day),
              patch.object(zte, "POLICY_FILE", policy_file),
              patch.object(zte, "PAUSE_FILE", self.root / "pause"),
              patch.object(zte, "load_policy", return_value=policy),
              patch.object(zte.Policy, "client", return_value=client),
              patch.object(zte, "observation", return_value=("eligible", base)),
              patch.object(zte_issue, "current"),
              patch.object(zte_release, "installation", return_value=self.installation()),
              patch.object(zte_release, "selected", return_value=base)):
            zte.finder(config.load())
        return zte_store.latest()

    def test_alias_reserves_next_pinned_production_delivery_after_healthy_arete_delivery(self):
        previous = self.healthy_predecessor()
        policy = self.production_policy("b" * 40, 2)

        delivery = self.find_production_delivery(policy, Mock())

        self.assertEqual((delivery.repo, delivery.phase, delivery.policy), ("arete-zte", "queued", policy.fingerprint))
        self.assertEqual(tasks_store.task_get(delivery.task)["repo"], "arete-zte")
        self.assertEqual(zte_store.find(previous.task), previous)

    def test_alias_recovers_eligible_pre_pr_hold_with_arete_predecessor(self):
        previous = self.healthy_predecessor()
        held_policy = self.production_policy("b" * 40, 2)
        pushed = self.reserve_under_arete(held_policy, days_ago=1)
        for phase in ("dispatching", "running", "prepared", "push_intent"):
            pushed = zte_store.transition(pushed, phase)
        branch, head = f"arete/{pushed.task}-fixture", "f" * 40
        pushed = zte_store.transition(pushed, "pushed", branch=branch, head=head,
                                      push_sent_after=pushed.created)
        held = zte_store.transition(pushed, "held", stopped_at="pushed",
                                    reason="the issue is not in the dedicated delivery queue")
        policy = self.production_policy("c" * 40, 3, reconciled_hold=held.task)
        identity = policy.identity
        client = Mock(identity=identity)
        client.branch_head.return_value = head
        client.branch_prs.return_value = []
        client.read.return_value = {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": held.base}}
        client.issue_snapshot.return_value = {
            "databaseId": identity.number, "id": identity.node, "nameWithOwner": identity.name,
            "owner": {"databaseId": identity.owner}, "defaultBranchRef": {"name": "main"},
            "issue": {"number": held_policy.issue.number, "id": held_policy.issue.node, "state": "OPEN",
                      "timelineItems": {"nodes": []}, "blockedBy": {"totalCount": 0}, "subIssues": {"totalCount": 0}},
        }

        delivery = self.find_production_delivery(policy, client)

        self.assertEqual((delivery.repo, delivery.phase, delivery.policy), ("arete-zte", "queued", policy.fingerprint))
        self.assertEqual(delivery.data["reconciled_hold"],
                         {"task": held.task, "version": held.version, "branch": branch, "head": head})
        self.assertEqual(tasks_store.task_get(delivery.task)["repo"], "arete-zte")
        self.assertEqual(zte_store.find(held.task), held)
        self.assertEqual(zte_store.find(previous.task), previous)


HELPER_GUARD = """\
import os, signal, sys, time
from pathlib import Path
signal.alarm(25)


def abandoned():
    try:
        os.kill(int(sys.argv[1]), 0)
    except ProcessLookupError:
        return True
    return False


"""
SHELL_HELPER_SECONDS = 20


class ConfiguredCheckSlotTests(Isolated):
    sync_timeout = 12

    def setUp(self):
        super().setUp()
        self.host_lock_root = paths.CHECK_LOCKS_DIR
        self.patches.enter_context(patch.object(paths, "CHECK_LOCKS_DIR", self.root / "check-locks"))
        paths.CONFIG_FILE.write_text("max_parallel_checks = 1\n")
        self.cfg.max_parallel_checks = 1

    def held(self, name):
        with paths.directory_fd(paths.CHECK_LOCKS_DIR) as directory, slots.check_lock_file(directory, name) as lock:
            return not util.flock_now(lock)

    def instance_env(self, root, *, default_limit=False):
        state, cfg, temp = root / "state", root / "config", root / "tmp"
        cfg.mkdir(parents=True, exist_ok=True)
        temp.mkdir()
        (cfg / "config.toml").write_text("" if default_limit else "max_parallel_checks = 1\n")
        env = {**os.environ, "ARETE_CONFIG_DIR": str(cfg), "ARETE_STATE_DIR": str(state),
               "ARETE_LOG_DIR": str(root / "logs"), "ARETE_WORKTREES_DIR": str(root / "worktrees"),
               "TMPDIR": str(temp)}
        env.pop(paths.SCRATCH_ENV, None)
        return env

    def test_checks_in_different_repos_share_one_slot_without_charging_the_wait(self):
        first_dir = self.root / "first"
        first_dir.mkdir()
        first = pipeline.AgentRun("first", config.Repo("other", first_dir), first_dir, cfg=self.cfg, writes_lines=False)
        first_layer = config.RepoLayer(first_dir, checks={"behavior": "first"})
        second_layer = config.RepoLayer(self.repo.path, checks={"behavior": "second"},
                                        quick_checks=("behavior",), parallel_checks=True)
        clock = [100.0]
        first_started, finish_first = threading.Event(), threading.Event()
        calls = []

        def execute(command, *args, timeout, **kw):
            calls.append((command, timeout))
            if command == "first":
                first_started.set()
                self.assertTrue(finish_first.wait(self.sync_timeout))
            else:
                second.budget.charge("checks", "checks")
                self.assertEqual(second.budget.run_seconds(), 0)
            return subprocess.CompletedProcess(command, 0, "", "")

        def wait_for_slot(_):
            self.assertEqual(calls, [("first", 3)])
            clock[0] += 65
            finish_first.set()
            self.assertTrue(first_checks.result(timeout=self.sync_timeout)[0])

        with (patch.object(runlimits, "time", SimpleNamespace(monotonic=lambda: clock[0])),
              patch.object(pipeline.environment, "sh", side_effect=execute),
              patch.object(slots, "time", SimpleNamespace(sleep=wait_for_slot)), ThreadPoolExecutor(1) as worker):
            second = self.made_run()
            second.budget.deadline = clock[0] + 60
            first_checks = worker.submit(pipeline.run_checks, first_layer, first_dir, [], first, 3)
            try:
                self.assertTrue(first_started.wait(self.sync_timeout))
                self.assertTrue(pipeline.run_checks(second_layer, self.repo.path, [], second, 3)[0])
            finally:
                finish_first.set()

        self.assertEqual(calls, [("first", 3), ("second", 3)])
        self.assertEqual(second.budget.idled, 65)
        self.assertIn("waiting for a check slot", (second.dir / "run.log").read_text())

    def test_sigkill_releases_the_check_slot(self):
        code = "import signal, sys; signal.alarm(20); print('holding', flush=True); sys.stdin.read()"
        run = SimpleNamespace(config=self.cfg)
        with slots.check_slot(run, parallel=False) as hold_fds:
            child = subprocess.Popen([sys.executable, "-I", "-c", code], pass_fds=hold_fds, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
        with child:
            try:
                self.assertTrue(select.select([child.stdout], [], [], self.sync_timeout)[0])
                self.assertEqual(child.stdout.readline().strip(), "holding")
                self.assertTrue(self.held("checks-0"))
                child.send_signal(signal.SIGKILL)
                self.assertEqual(child.wait(timeout=self.sync_timeout), -signal.SIGKILL)
            finally:
                if child.poll() is None:
                    child.kill()

        self.assertFalse(self.held("checks-0"))
        self.assertFalse(self.held("checks"))
        with slots.check_slot(SimpleNamespace(config=self.cfg), parallel=False):
            self.assertTrue(self.held("checks-0"))

    def start_checks(self, root, command, *, isolated, start_when=None, default_limit=False):
        code = """\
import signal
import sys
import time
from pathlib import Path
from arete import config, paths, pipeline

signal.alarm(25)
host_lock_dir, lock_dir, command, isolated, start_when = sys.argv[1:]
assert paths.ISOLATED == (isolated == "True"), (paths.ISOLATED, paths.STATE_DIR)
assert paths.CHECK_LOCKS_DIR == Path(host_lock_dir), paths.CHECK_LOCKS_DIR
paths.CHECK_LOCKS_DIR = Path(lock_dir)
paths.ensure_dirs()
run_dir = paths.RUNS_DIR / "fixture"
run_dir.mkdir()
run = pipeline.AgentRun("fixture", config.Repo(paths.STATE_DIR.parent.name, run_dir), run_dir, writes_lines=False)
layer = config.RepoLayer(run_dir, checks={"behavior": command}, parallel_checks=paths.ISOLATED)
print("ready", flush=True)
if not sys.stdin.readline():
    sys.exit(0)
while start_when and not Path(start_when).exists():
    time.sleep(0.01)
ok, report = pipeline.run_checks(layer, run_dir, [], run, 15)
if not ok:
    print(report, flush=True)
sys.exit(0 if ok else 1)
"""
        child = subprocess.Popen([sys.executable, "-c", code, str(self.host_lock_root),
                                  str(paths.CHECK_LOCKS_DIR), command, str(isolated), str(start_when or "")],
                                 env=self.instance_env(root, default_limit=default_limit),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True)

        def clean():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=CHILD_HANG_TIMEOUT)

        self.addCleanup(clean)
        return child

    def read_child_line(self, child):
        self.assertTrue(select.select([child.stdout], [], [], self.sync_timeout)[0])
        return child.stdout.readline()

    def wait_for_file(self, file, child):
        deadline = time.monotonic() + self.sync_timeout
        while not file.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                self.fail(child.communicate(timeout=CHILD_HANG_TIMEOUT)[0])
            time.sleep(0.01)
        self.assertTrue(file.exists(), file)

    def test_scratch_waits_for_a_regular_runs_suite_after_pipeline_death(self):
        regular = self.root / "regular"
        scratch = self.root / ".arete-scratch-0000000000000001"
        scratch.mkdir(mode=0o700)
        started, finish, finished, reused = (self.root / name for name in ("started", "finish", "finished", "reused"))
        release_command = self.root / "release-command"
        command_pid = self.root / "command-pid"
        helper = HELPER_GUARD + """\
started, finish, finished = map(Path, sys.argv[2:])
assert os.getsid(0) == os.getpid()
started.touch()
while not finish.exists() and not abandoned():
    time.sleep(0.01)
finished.touch()
"""
        command = HELPER_GUARD + """\
import subprocess
pid_file, release = map(Path, sys.argv[2:4])
pid_file.write_text(str(os.getpid()))
subprocess.Popen([sys.executable, "-I", "-c", sys.argv[4], sys.argv[1], *sys.argv[5:]], start_new_session=True,
                 close_fds=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not release.exists() and not abandoned():
    time.sleep(0.01)
"""
        first = self.start_checks(regular, "exec " + shlex.join(
            [sys.executable, "-I", "-c", command, str(os.getpid()), str(command_pid), str(release_command), helper,
             str(started), str(finish), str(finished)]), isolated=False)
        end, reuse = map(shlex.quote, map(str, (finished, reused)))
        try:
            second = self.start_checks(scratch, f"test -e {end} && : > {reuse}", isolated=True, start_when=started,
                                       default_limit=True)
            for child in (first, second):
                self.assertEqual(self.read_child_line(child).strip(), "ready")
            for child in (first, second):
                child.stdin.write("start\n")
                child.stdin.flush()
            self.wait_for_file(started, first)
            self.assertIn("waiting for a check slot (max_parallel_checks 1)", self.read_child_line(second))
            self.assertFalse(reused.exists())
            first.kill()
            self.assertEqual(first.wait(timeout=self.sync_timeout), -signal.SIGKILL)
            release_command.touch()
            pid = int(command_pid.read_text())
            deadline = time.monotonic() + self.sync_timeout
            while util.pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(util.pid_alive(pid))
            self.assertTrue(self.held("checks"))
            self.assertTrue(self.held("checks-0"))
            self.assertFalse(finished.exists())
        finally:
            release_command.touch()
            finish.touch()
        output, _ = second.communicate(timeout=CHILD_HANG_TIMEOUT)
        self.assertEqual(second.returncode, 0, output)
        self.assertTrue(reused.exists())
        self.assertFalse(self.held("checks-0"))
        self.assertIn("waiting for a check slot", (scratch / "state/runs/fixture/run.log").read_text())

    def test_successful_check_leaves_a_redirected_helper_for_the_next_command(self):
        started, release, finished = (self.root / name for name in ("started", "release", "finished"))
        start, stop, end = map(shlex.quote, map(str, (started, release, finished)))
        keep_waiting = (f'[ ! -e {stop} ] && kill -0 {os.getpid()} 2>/dev/null && [ "$(date +%s)" -lt "$limit" ]')
        checks = {"start": f"(limit=$(( $(date +%s) + {SHELL_HELPER_SECONDS} )); : > {start}; "
                           f"while {keep_waiting}; do sleep .01; done; : > {end}) "
                           "</dev/null >/dev/null 2>&1 &",
                  "use": f"while [ ! -e {start} ]; do sleep .01; done; : > {stop}; "
                         f"while [ ! -e {end} ]; do sleep .01; done"}
        layer = config.RepoLayer(self.repo.path, checks=checks, parallel_checks=True)
        run = pipeline.AgentRun("helpers", self.repo, self.root, cfg=self.cfg, writes_lines=False)
        try:
            ok, report = pipeline.run_checks(layer, self.repo.path, [], run, 3)
            self.assertTrue(ok, report)
            self.assertTrue(finished.exists())
        finally:
            release.touch()

    def test_worker_launch_hash_survives_check_limit_edit(self):
        layer = config.RepoLayer(self.repo.path)
        run = SimpleNamespace(id="worker", repo=self.repo, kind="", closed_context=())
        job = SimpleNamespace(run=run, ws=self.repo.path, cfg=config.load(), layer=layer)
        setting = steps.resolve(job.cfg, layer, "build")
        before = delegation.request(job, "respond", "Fixture", setting, (), [])

        paths.CONFIG_FILE.write_text("max_parallel_checks = 3\n")
        job.cfg = config.load()
        after = delegation.request(job, "respond", "Fixture", setting, (), [])

        self.assertEqual(job.cfg.max_parallel_checks, 3)
        self.assertEqual(before["config_sha"], after["config_sha"])
        delegation.check_policy(before, job.cfg, layer)

    def test_admitted_managed_delivery_survives_only_check_limit_edit(self):
        repo_dir = str(self.repo.path)
        cfg_text = f'''\
max_parallel_checks = {{limit}}
max_parallel = {{runs}}
arete_repo = "fixture"
[[repo]]
name = "fixture"
path = {json.dumps(repo_dir)}
layer = {json.dumps(str(self.root / "layer"))}
github = "owner/fixture"
tracker = "local"
pipeline = true
'''
        paths.CONFIG_FILE.write_text(cfg_text.format(limit=1, runs=1))
        layer = config.RepoLayer(self.root / "layer", verify=config.Verify(),
                                 routing=config.Routing(fallback=False, payment_modes=("subscription",)),
                                 max_run_minutes=40, max_verify_minutes=15, max_repair_cycles=1,
                                 orchestration=config.Orchestration(max_workers=1, max_decisions=16, max_actions=64),
                                 checks={"lint": "sh tools/lint-arete", "compile": "true", "imports": "true",
                                         "web": "true", "behavior": "true"},
                                 steps={"orchestrate": {"runner": "pi", "model": "openai/gpt-6.2-sol", "effort": "low"},
                                        "verify": {"runner": "pi"}})
        identity = Identity("owner/fixture", 1, "repo", 2, 3, 4, "a" * 40, 5, 6)
        policy = zte.Policy(identity, "## Before", "## After", True, True, "", True)
        with (patch.object(config, "load_layer", return_value=layer),
              patch.object(zte, "load_policy", return_value=policy),
              patch.object(zte, "PAUSE_FILE", self.root / "pause")):
            repo, _, digest = zte.settings(config.load(), policy)
            deadline = util.iso(util.now() + timedelta(minutes=1))
            base = "b" * 40
            delivery = zte_store.reserve(policy.fingerprint, repo.name, base, deadline, pilot.TASK_TITLE,
                                         pilot.TASK_KIND, policy.signal(base).task(),
                                         {"config": digest, "engine": codebase.STAMP})
            with principals.acting("rule:poll"):
                run_id = record.start("pipeline", repo=repo.name, issue=delivery.task)
            tasks_store.task_set_state(delivery.task, "running")
            dispatch = zte_store.transition(delivery, "dispatching")
            running = zte_store.transition(dispatch, "running", run=run_id)

            paths.CONFIG_FILE.write_text(cfg_text.format(limit=3, runs=1))
            self.assertEqual(zte.for_run(repo, delivery.task, run_id, "running"), (running, policy))
            self.assertEqual(orchestration.policy_fingerprint(config.load(), layer, ()), running.data["config"])

            paths.CONFIG_FILE.write_text(cfg_text.format(limit=3, runs=2))
            with self.assertRaisesRegex(pilot.Held, "the admitted ZTE config changed"):
                zte.for_run(repo, delivery.task, run_id, "running")

    def test_check_with_redirected_background_helper_returns_zero_within_a_second(self):
        pid_file = self.root / "helper-pid"
        command = (f"sleep {SHELL_HELPER_SECONDS} </dev/null >/dev/null 2>&1 & "
                   f"echo $! > {shlex.quote(str(pid_file))}")
        layer = config.RepoLayer(self.repo.path, checks={"behavior": command}, parallel_checks=True)
        run = pipeline.AgentRun("redirected", self.repo, self.root, cfg=self.cfg, writes_lines=False)
        began = time.monotonic()
        try:
            ok, report = pipeline.run_checks(layer, self.repo.path, [], run, 5)
            elapsed = time.monotonic() - began
        finally:
            if pid_file.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
        self.assertTrue(ok, report)
        self.assertLess(elapsed, 1)
        self.assertNotIn("124", report)

    def test_admission_lock_held_by_another_process_is_a_logged_wait_that_is_not_charged(self):
        with paths.directory_fd(paths.CHECK_LOCKS_DIR, create=True):
            pass
        holder_code = ("import fcntl, os, signal, sys\n"
                       "signal.alarm(20)\n"
                       "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
                       "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                       "print('held', flush=True)\n"
                       "sys.stdin.read()\n")
        holder = subprocess.Popen(
            [sys.executable, "-I", "-c", holder_code, str(paths.CHECK_LOCKS_DIR / slots.CHECK_SLOTS_LOCK)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.kill)
        self.assertTrue(select.select([holder.stdout], [], [], self.sync_timeout)[0])
        self.assertEqual(holder.stdout.readline().strip(), "held")
        layer = config.RepoLayer(self.repo.path, checks={"behavior": "fixture"}, parallel_checks=True)
        clock = [100.0]

        def execute(command, *args, timeout, **kw):
            run.budget.charge("checks", "checks")
            self.assertEqual(run.budget.run_seconds(), 0)
            return subprocess.CompletedProcess(command, 0, "", "")

        def wait_for_admission(_):
            clock[0] += 65
            holder.stdin.close()
            holder.wait(timeout=self.sync_timeout)

        with (patch.object(runlimits, "time", SimpleNamespace(monotonic=lambda: clock[0])),
              patch.object(pipeline.environment, "sh", side_effect=execute),
              patch.object(slots, "time", SimpleNamespace(sleep=wait_for_admission))):
            run = self.made_run()
            run.budget.deadline = clock[0] + 60
            self.assertTrue(pipeline.run_checks(layer, self.repo.path, [], run, 3)[0])

        self.assertEqual(run.budget.idled, 65)
        self.assertIn("waiting for a check slot (another run is admitting checks)", (run.dir / "run.log").read_text())

    LEFT_BEHIND_HELPER = """\
import os, sys, time
from pathlib import Path
started, release = map(Path, sys.argv[1:3])
if os.fork() == 0:
    os.setsid()
    limit = time.monotonic() + 20
    with open(os.devnull, "r+b") as quiet:
        for fd in (0, 1, 2):
            os.dup2(quiet.fileno(), fd)
    pending = started.with_suffix(".tmp")
    pending.write_text(str(os.getpid()))
    pending.replace(started)
    while not release.exists() and time.monotonic() < limit:
        time.sleep(0.01)
    os._exit(0)
"""

    def wait_until_free(self, name):
        deadline = time.monotonic() + self.sync_timeout
        while self.held(name) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.held(name))

    def test_busy_admission_lock_is_a_logged_wait_and_not_a_block(self):
        order, logged, left = [], threading.Event(), threading.Event()

        @contextlib.contextmanager
        def waiting(cause):
            order.append(f"wait {cause}")
            try:
                yield
            finally:
                order.append("done")
                left.set()

        def log(line):
            order.append(line)
            logged.set()

        run = SimpleNamespace(config=self.cfg, log=log, waiting=waiting)
        with paths.directory_fd(paths.CHECK_LOCKS_DIR, create=True) as directory:
            admission = slots.check_lock_file(directory, slots.CHECK_SLOTS_LOCK)
        with (patch.object(slots, "CHECK_SLOT_POLL_SECONDS", 0.01), ThreadPoolExecutor(1) as worker, admission):
            self.assertTrue(util.flock_now(admission))

            def take():
                with slots.check_slot(run, parallel=True):
                    self.assertTrue(left.is_set())
                    self.assertTrue(self.held("checks-0"))

            taking = worker.submit(take)
            self.assertTrue(logged.wait(self.sync_timeout))
            self.assertFalse(left.is_set())
            admission.close()
            taking.result(timeout=self.sync_timeout)
        self.assertEqual(order, ["wait slot", "checks: waiting for a check slot (another run is admitting checks)",
                                 "done"])
        self.assertFalse(self.held("checks-0"))

    def test_reparented_helper_in_its_own_session_keeps_the_slot_after_every_other_holder_dies(self):
        started, release, owners = (self.root / name for name in ("helper-started", "helper-release", "owners"))
        code = self.LEFT_BEHIND_HELPER + """\
else:
    note = Path(sys.argv[3])
    pending = note.with_suffix(".tmp")
    pending.write_text(f"{os.getppid()} {os.getpid()}")
    pending.replace(note)
    time.sleep(20)
"""
        pipeline_stand_in = self.start_checks(
            self.root / "orphan", "exec " + shlex.join([sys.executable, "-I", "-c", code, str(started),
                                                         str(release), str(owners)]), isolated=False)
        try:
            self.assertEqual(self.read_child_line(pipeline_stand_in).strip(), "ready")
            pipeline_stand_in.stdin.write("start\n")
            pipeline_stand_in.stdin.flush()
            self.wait_for_file(started, pipeline_stand_in)
            self.wait_for_file(owners, pipeline_stand_in)
            self.assertTrue(self.held("checks-0"))
            supervisor, command = map(int, owners.read_text().split())
            helper = int(started.read_text())
            self.assertNotEqual(os.getsid(helper), os.getsid(command))
            pipeline_stand_in.kill()
            self.assertEqual(pipeline_stand_in.wait(timeout=self.sync_timeout), -signal.SIGKILL)
            os.kill(supervisor, signal.SIGKILL)
            os.kill(command, signal.SIGKILL)
            deadline = time.monotonic() + self.sync_timeout
            while (util.pid_alive(supervisor) or util.pid_alive(command)) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(util.pid_alive(supervisor) or util.pid_alive(command))
            self.assertTrue(util.pid_alive(helper))
            self.assertTrue(self.held("checks-0"))
            self.assertTrue(self.held("checks"))
        finally:
            release.touch()
        self.wait_until_free("checks-0")
        self.wait_until_free("checks")

    def test_helper_left_behind_by_a_finished_suite_does_not_keep_the_slot(self):
        started, release = (self.root / name for name in ("left-started", "left-release"))
        command = "exec " + shlex.join([sys.executable, "-I", "-c", self.LEFT_BEHIND_HELPER, str(started),
                                         str(release)])
        layer = config.RepoLayer(self.repo.path, checks={"behavior": command}, parallel_checks=True)
        run = pipeline.AgentRun("left-behind", self.repo, self.root, cfg=self.cfg, writes_lines=False)
        try:
            ok, report = pipeline.run_checks(layer, self.repo.path, [], run, 5)
            self.assertTrue(ok, report)
            deadline = time.monotonic() + self.sync_timeout
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(util.pid_alive(int(started.read_text())))
            self.assertFalse(self.held("checks-0"))
        finally:
            release.touch()


class CatchUpWorkspaceRecordTests(Isolated):
    branch = "arete/fixture"
    build_files = (".venv/pyvenv.cfg", "__pycache__/app.pyc")

    def setUp(self):
        super().setUp()
        self.ws = paths.WORKTREES_DIR / f"{self.repo.name}-{self.ticket}"
        self.layer = config.RepoLayer(self.repo.layer_dir, branch=self.branch, base_branch="main", verify=config.Verify())
        for patcher in (patch.object(config, "load", return_value=self.cfg),
                        patch.object(config, "load_layer", return_value=self.layer),
                        patch.object(sandbox, "AUTHORITY", self.root / "authority")):
            self.patches.enter_context(patcher)
        src = self.repo.path
        util.git(["init", "-q", "-b", "main"], src)
        util.git(["remote", "add", "origin", str(src)], src)
        (src / ".gitignore").write_text(".venv/\n__pycache__/\nlocal.txt\n")
        util.git(["add", ".gitignore"], src)
        util.git_as_owner(["commit", "-qm", "Initial base"], src)
        util.add_worktree(src, self.ws, self.branch, "main")
        (self.ws / "feature.txt").write_text("Branch change\n")
        util.git(["add", "feature.txt"], self.ws)
        util.git_as_owner(["commit", "-qm", "Branch change"], self.ws)
        self.head = util.git(["rev-parse", "HEAD"], self.ws).strip()
        self.checkout = workspace.Checkout("", self.repo.name, src, self.ws, "main", lease=self.head, onto=self.head)
        for name in self.build_files:
            path = self.ws / name
            path.parent.mkdir()
            path.write_text("Build output\n")

    def run_ticket(self, drive):
        status = pipeline.run_issue(self.repo.name, self.ticket, force=True, script=pipeline.Script("adw", drive))
        return status, run_store.newest_whole_run(self.repo.name, self.ticket)

    def fail_catch_up(self, incoming, why, *, retained=False):
        src = self.repo.path
        (src / incoming).write_text("Base change\n")
        util.git(["add", "-f", incoming], src)
        util.git_as_owner(["commit", "-qm", "Move the base"], src)
        workspace.fetch(src, "main")
        base = util.git(["rev-parse", "origin/main"], self.ws).strip()

        def drive(flow):
            run = flow.ctx.run
            pipeline.claim_workspace(run, self.ws, [self.checkout])
            build = pipeline.Build(self.cfg, run, self.local.get(self.ticket), self.ws, [self.checkout], self.layer,
                                   5, "Fixture worktree", tools=[], deny=[])
            tree = util.git(["rev-parse", f"{self.head}^{{tree}}"], self.ws).strip()
            prior = pipeline.Review("approve", trees={"": tree})
            pipeline.catch_up_base(build, self.checkout, self.branch, base, "", "", prior,
                                   before_push=self.unexpected_push)

        status, row = self.run_ticket(drive)
        self.assertEqual((status, row["summary"]), (1, why))
        head = util.git(["rev-parse", "HEAD"], self.ws).strip()
        if retained:
            self.assertEqual(head, self.checkout.merge)
            self.assertEqual(util.git(["rev-parse", "HEAD^1", "HEAD^2"], self.ws).splitlines(), [self.head, base])
        else:
            self.assertEqual(head, self.head)
        made = workspace.made_before(self.ws)
        self.assertEqual(workspace.head_recorded(made, self.checkout), head)
        for name in self.build_files:
            self.assertIn(f"{Path(name).parent}/", made)
            self.assertEqual((self.ws / name).read_text(), "Build output\n")
        return made

    def unexpected_push(self, *_):
        self.fail("A failed catch-up reached the push")

    def retry(self):
        def drive(flow):
            prepared = flows.step(flow, "prepare", tree=str(self.ws))
            return flows.ended(flow, flows.step(flow, "plan") if prepared.passed else prepared)

        with patch.object(pipeline.Run, "agent", return_value=Result(ok=False, error="Stop at the first worker")) as agent:
            status, row = self.run_ticket(drive)
        self.assertEqual(status, 1, row["summary"])
        return row["summary"], [call.args[0] for call in agent.call_args_list]

    def test_review_hold_records_build_files_and_retry_starts_planning(self):
        def verified(build, *_):
            tree = util.git(["rev-parse", "HEAD^{tree}"], build.ws).strip()
            return pipeline.verify.Verified("pass", checks=[{"passed": True}], native_trees={"": tree})

        with (patch.object(pipeline.Build, "check", return_value=True),
              patch.object(pipeline, "check_in_use", side_effect=verified),
              patch.object(pipeline, "_review", return_value=pipeline.Review("request_changes"))):
            self.fail_catch_up("incoming.txt", "the published tree lacks a current approving review", retained=True)

        self.assertEqual(self.retry(), ("planning step failed", ["plan"]))

    def test_collision_leaves_the_path_off_record_and_retry_refuses_it(self):
        collision = self.ws / "local.txt"
        collision.write_text("Keep this file\n")

        made = self.fail_catch_up(collision.name, "the merge would overwrite ignored file `local.txt`")

        self.assertNotIn(collision.name, made)
        self.assertEqual(self.retry(), (
            f"the existing worktree {self.ws} of {self.repo.name} holds git-ignored files that arete has no record of "
            "leaving there (local.txt), so arete did not make it again and left them as they are; "
            "move or remove them, then run again", []))
        self.assertEqual(collision.read_text(), "Keep this file\n")


def passed_fixture_checks(flow, **kw):
    job = flow.ctx
    report = "Fixture checks passed."
    return factory.Outcome("pass", data={
        "report": report, "policy": job.policy("checks", kw),
        "checked": {"tree": factory.fingerprint(job.ws), "trees": pipeline.verification_trees(job.b),
                    "commands": dict(job.layer.checks), "report": report},
    })


class ReviewRetryTests(Isolated):
    PR = "https://github.com/example/fixture/pull/1"
    BLOCKER = {"file": "notes.txt", "problem": "fixture blocker", "severity": "blocker", "category": "issue",
               "acceptance": "Inspect notes.txt and confirm the fixture blocker is addressed.",
               "verification": "review", "verification_reason": "This finding is about the wording of the notes."}

    def review_build(self, *replies):
        run = self.made_run()
        run.step_overrides["review"] = {"tries": 2}
        flow = flows.start(self.cfg, run, {})
        job = flow.ctx
        job.ticket = self.local.get(self.ticket)
        with patch.object(pipeline, "workspace_note", return_value="Fixture workspace"):
            job.b.staged = pipeline.Staged(counts={"notes.txt": 1})

        def open_pr(flow, tree):
            return factory.Outcome("pass", data={
                "prs": {}, "opened": [], "changed": [], "pushed": [], "committed_lines": 1,
                "draft_why": flow.ctx.held_reason, "report_body": "", "summary": "",
            })

        with (patch.object(pipeline.runner, "run", side_effect=replies) as agent,
              patch.object(factory, "fingerprint", return_value="fixture-tree"),
              patch.object(pipeline, "verification_trees", return_value={"": "fixture-tree"}),
              patch.object(flows, "documents", return_value=False),
              patch.dict(factory.STEPS, {
                  "checks": passed_fixture_checks,
                  "verify": lambda *a, **kw: factory.Outcome("pass", data={"verified": {"verdict": "pass"}, "names": []}),
                  "learn": lambda *a, **kw: factory.Outcome("pass"),
                  "pr": open_pr,
                  "publish": lambda *a: factory.Outcome("pass", data={"ending": "pr", "pr_url": self.PR}),
                  "respond": lambda _, ending, **kw: factory.Outcome("pass", data={
                      "status": ending, "summary": kw.get("why", ""), "pr_url": "", **kw}),
              })):
            ending = feature.built(flow, [], prepared=True)
        self.assertEqual(ending, ("pr", "", self.PR))
        return flow, agent.call_args_list

    def test_review_retry_approves_and_reaches_publication_without_repair(self):
        for ok in (False, True):
            with self.subTest(ok=ok):
                first = Result(ok=ok, error="" if ok else "fixture call failed")
                flow, calls = self.review_build(first, Result(ok=True, structured={"summary": "Fine.", "problems": []}))
                self.assertEqual([call.kwargs["trace"][1] for call in calls], ["review", "review"])
                self.assertEqual(calls[0].args, calls[1].args)
                self.assertEqual(flow.tries["review"], 1)
                self.assertEqual(flow.ctx.run.budget.repair_calls, 0)
                self.assertTrue(flow.ctx.review.approved())
                self.assertEqual(flow.ctx.draft_why, "")

    def test_review_double_failure_keeps_earlier_blockers_and_the_draft(self):
        changes = Result(ok=True, structured={"summary": "Needs a fix.", "problems": [self.BLOCKER]})
        failed = Result(ok=False, error="fixture call failed")
        flow, calls = self.review_build(changes, Result(ok=True), failed, failed)
        self.assertEqual([call.kwargs["trace"][1] for call in calls], ["review", "review_fix", "review2", "review2"])
        self.assertEqual(flow.tries["review"], 2)
        self.assertEqual(flow.ctx.run.budget.repair_calls, 1)
        self.assertEqual(flow.ctx.review.verdict, "request_changes")
        self.assertTrue(flow.ctx.review.failed)
        self.assertEqual(flow.ctx.review.blocking()[0]["problem"], self.BLOCKER["problem"])
        self.assertIn("1 earlier blocker", flow.ctx.draft_why)

    def test_request_changes_starts_repair_without_retrying_review(self):
        changes = Result(ok=True, structured={"summary": "Needs a fix.", "problems": [self.BLOCKER]})
        approved = Result(ok=True, structured={
            "summary": "Fixed.", "problems": [], "earlier": [{"id": "B1", "status": "fixed", "why": "Rechecked."}],
        })
        flow, calls = self.review_build(changes, Result(ok=True), approved)
        self.assertEqual([call.kwargs["trace"][1] for call in calls], ["review", "review_fix", "review2"])
        self.assertIn(self.BLOCKER["problem"], calls[1].args[0])
        self.assertEqual(flow.ctx.run.budget.repair_calls, 1)
        self.assertTrue(flow.ctx.review.approved())


class OrchestrationLineageTests(Isolated):
    def setUp(self):
        super().setUp()
        self.patches.enter_context(patch.object(pipeline, "workspace_note", return_value="Fixture workspace"))

    def controller(self, previous="", feedback=()):
        run = self.made_run(previous)
        run.path = "orchestrate"
        run.feedback = list(feedback)
        return orchestration.Controller(flows.start(self.cfg, run, {}, continues=previous))

    def admit(self):
        controller = self.controller()
        outcome = factory.Outcome("pass", data={
            "layer": str(self.repo.layer_dir), "checkouts": [], "ticket": asdict(self.local.get(self.ticket)),
            "branch": "fixture", "add_dirs": [], "attachments": "", "expertise": "", "triage": "",
        })

        def prepare_step(*args, **kwargs):
            controller.job.take("prepare", outcome, stored=False)
            return outcome

        with patch.object(controller, "atomic", side_effect=prepare_step):
            self.assertIsNone(controller.prepare())
        return controller

    def accept(self, controller, op, state, args=None, **result):
        lease = controller.lease
        version = ledger.snapshot(lease)["version"]
        turn = ledger.begin(lease, {}, 10)
        action, = ledger.accept(lease, turn, version, {"calls": [{"op": op, "args": args or {}}]}, 10)
        if state != "accepted":
            ledger.transition(lease, action["id"], state, result)
        return action["id"]

    def audits(self, controller):
        return [line for line in run_lines.read(controller.run.id) if line["event"] == "lineage"]

    def test_operations_refresh_reaches_the_next_decision_and_audits_once(self):
        wait = OPERATIONS["run.wait"]
        old_wait = replace(wait, description="Old wait", constraints={"timeout_seconds": {"minimum": 1}})
        with patch.dict(OPERATIONS, {"run.wait": old_wait}):
            prior = self.admit()
        record.move(prior.run.id, "failed", ("running",))
        resumed = self.controller(prior.run.id)
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertIsNone(resumed.prepare())
        catalog_sha = prior.data["catalog_sha"]
        new = ledger.digest(describe_operations())
        audit, = self.audits(resumed)
        self.assertEqual(audit["old"], {"operations_sha": prior.data["operations_sha"]})
        self.assertEqual(audit["new"], {"operations_sha": new})
        self.assertNotEqual(audit["old"], audit["new"])
        self.assertEqual(audit["engine_commit"], "new-engine")
        data = ledger.snapshot(resumed.lease)["data"]
        self.assertEqual(data["operations_sha"], new)
        self.assertEqual(data["catalog_sha"], catalog_sha)

        def decide(job, turn, prompt, *args):
            self.assertIn(wait.description, prompt)
            self.assertIn(f'"catalog_sha": "{catalog_sha}"', prompt)
            response = {"version": PROTOCOL, "decision_id": turn, "rationale": "",
                        "state_version": ledger.snapshot(resumed.lease)["version"],
                        "calls": [{"op": "run.wait", "args": {"timeout_seconds": 0}}]}
            worker.receive.return_value = [{"ok": True, "structured": response}]
            return {}

        with (patch.object(delegation, "request", side_effect=decide),
              patch.object(delegation, "Worker") as launch):
            worker = launch.return_value
            worker.pid, worker.identity = 123, 456
            action, = resumed.decide()
        self.assertEqual(action["request"]["timeout_seconds"], 0)
        resumed.dispatch(action)
        record.move(resumed.run.id, "interrupted", ("running",))
        repeated = self.controller(resumed.run.id)
        self.assertIsNone(repeated.prepare())
        self.assertEqual(repeated.data["operations_sha"], new)
        self.assertEqual(self.audits(resumed), [audit])
        self.assertEqual(self.audits(repeated), [])

    def test_successive_refreshes_in_one_run_each_write_an_audit_line(self):
        wait = OPERATIONS["run.wait"]
        changed_wait = replace(wait, description="Deployed wait")
        prior = self.admit()
        record.move(prior.run.id, "failed", ("running",))
        resumed = self.controller(prior.run.id)
        original = {"operations_sha": prior.data["operations_sha"]}
        with patch.object(codebase, "STAMP", "deploy"):
            for deployed in (changed_wait, wait, changed_wait):
                with patch.dict(OPERATIONS, {"run.wait": deployed}):
                    self.assertIsNone(resumed.prepare())

        changed = {"operations_sha": ledger.digest({**describe_operations(), "run.wait": changed_wait.describe()})}
        self.assertNotEqual(changed, original)
        audits = self.audits(resumed)
        self.assertEqual([(line["old"], line["new"]) for line in audits],
                         [(original, changed), (changed, original), (original, changed)])
        self.assertEqual([line["engine_commit"] for line in audits], ["deploy"] * 3)
        self.assertEqual(ledger.snapshot(resumed.lease)["data"]["operations_sha"], changed["operations_sha"])

    def test_main_format_resumes_an_accepted_action_without_a_lineage_event(self):
        prior = self.admit()
        catalog_sha = ledger.digest(prior.catalog.entries)
        prior.data["catalog_sha"] = catalog_sha
        del prior.data["operations_sha"]
        ledger.save(prior.lease, prior.data)
        action_id = self.accept(prior, "run.finish", "accepted", {"reason": "noop", "evidence_refs": []})
        record.move(prior.run.id, "interrupted", ("running",))

        resumed = self.controller(prior.run.id)
        with patch.object(resumed, "decide", side_effect=AssertionError("the accepted action should finish the run")):
            self.assertEqual(resumed.drive()[0], "needs-info")
        action, = ledger.actions(resumed.lease)
        self.assertEqual((action["id"], action["state"]), (action_id, "succeeded"))
        data = ledger.snapshot(resumed.lease)["data"]
        self.assertEqual(data["catalog_sha"], catalog_sha)
        self.assertEqual(data["operations_sha"], ledger.digest(describe_operations()))
        self.assertEqual(self.audits(resumed), [])

    def test_catalog_audit_survives_interruption_after_append(self):
        prior = self.admit()
        old_entries = {key: entry for key, entry in prior.catalog.entries.items() if key != "prompt:delegate"}
        prior.data["catalog_sha"] = ledger.digest(old_entries)
        ledger.save(prior.lease, prior.data)
        record.move(prior.run.id, "interrupted", ("running",))
        resumed = self.controller(prior.run.id)
        write = run_lines.write

        def interrupt_after_write(run_id, event, **fields):
            line = write(run_id, event, **fields)
            if event == "lineage":
                raise OSError("interrupted after append")
            return line

        with (patch.object(run_lines, "write", side_effect=interrupt_after_write),
              self.assertRaisesRegex(OSError, "interrupted after append")):
            resumed.prepare()
        audit, = self.audits(resumed)
        pending = ledger.snapshot(resumed.lease)["data"]["lineage_event"]
        self.assertEqual(pending["run"], resumed.run.id)
        self.assertEqual(pending["event_id"], audit["event_id"])
        self.assertEqual(pending["old"], audit["old"])
        self.assertEqual(pending["new"], audit["new"])
        record.move(resumed.run.id, "interrupted", ("running",))
        repeated = self.controller(resumed.run.id)
        self.assertIsNone(repeated.prepare())
        self.assertEqual(self.audits(resumed), [audit])
        self.assertEqual(self.audits(repeated), [])
        self.assertNotIn("lineage_event", ledger.snapshot(repeated.lease)["data"])

    def test_lineage_row_missing_its_newline_recovers_in_either_append_order(self):
        fields = {"event_id": "refresh", "old": {"catalog_sha": "old"}, "new": {"catalog_sha": "new"},
                  "engine_commit": "deploy"}
        for error_first in (False, True):
            with self.subTest(error_first=error_first):
                run_id = f"torn-lineage-{error_first}"
                route = run_lines.write(run_id, "route")
                audit = {"ts": route["ts"], "run": run_id, "seq": route["seq"] + 1, "event": "lineage", **fields}
                with run_lines.path(run_id).open("ab") as log:
                    log.write(json.dumps(audit).encode())
                if error_first:
                    error = run_lines.write(run_id, "error", reason="interrupted append")
                self.assertEqual(run_lines.write(run_id, "lineage", **fields), audit)
                if not error_first:
                    self.assertEqual(run_lines.read(run_id), [route, audit])
                    error = run_lines.write(run_id, "error", reason="interrupted append")
                self.assertEqual(error["seq"], audit["seq"] + 1)
                self.assertEqual(run_lines.read(run_id), [route, audit, error])
                self.assertEqual(list(run_lines.follow(run_id, start=audit["seq"], ended=lambda: True)), [error])

    def test_explicit_rerun_uses_the_refreshed_catalog(self):
        checks = replace(OPERATIONS["checks.run"], constraints={"scope": {"enum": ["full"]}})
        for scope in ("quick", "full"):
            with self.subTest(scope=scope):
                prior = self.admit()
                action_id = self.accept(prior, "checks.run", "succeeded", {"scope": scope})
                record.move(prior.run.id, "failed", ("running",))
                resumed = self.controller(prior.run.id)
                resumed.run.action, resumed.run.action_only = action_id, True
                with (patch.dict(OPERATIONS, {"checks.run": checks}),
                      patch.object(resumed, "dispatch", wraps=resumed.dispatch) as dispatch,
                      patch.object(ledger, "accept", wraps=ledger.accept) as accept,
                      patch.object(resumed, "fingerprint", return_value="fixture-tree"),
                      patch.object(resumed, "atomic", return_value=factory.Outcome("pass")) as atomic):
                    if scope == "quick":
                        with self.assertRaisesRegex(factory.Refused, rf"{action_id}.*scope must be one of: full"):
                            resumed.drive()
                        accept.assert_not_called()
                        dispatch.assert_not_called()
                        atomic.assert_not_called()
                    else:
                        self.assertEqual(resumed.drive()[0], "step")
                        accept.assert_called_once()
                        dispatch.assert_called_once()
                        atomic.assert_called_once_with("checks", tree=str(resumed.job.ws), quick=False)
                self.assertEqual(ledger.pending(resumed.lease), [])
                self.assertEqual(len(self.audits(resumed)), 1)
                original, *reruns = ledger.actions(resumed.lease)
                self.assertEqual(original["id"], action_id)
                self.assertEqual(original["request"], {"scope": scope})
                self.assertEqual([(a["state"], a["request"]) for a in reruns],
                                 [("succeeded", {"scope": "full"})] if scope == "full" else [])

    def test_explicit_rerun_accepts_a_near_limit_worker_decision_after_normalization(self):
        prior = self.admit()
        route = "model:build:0"
        effort = prior.catalog.entries[route]["efforts"][0]
        sizes = iter((MAX_BYTES + 1, MAX_BYTES - 1))

        def decide(job, turn, prompt, *settings):
            request = {"goal": "", "route_id": route, "effort": effort}
            response = {"version": PROTOCOL, "decision_id": turn, "rationale": "",
                        "state_version": ledger.snapshot(prior.lease)["version"],
                        "calls": [{"op": "worker.start", "args": request}]}
            size = next(sizes)
            escaped, padding = divmod(size - len(json.dumps(response)), len(json.dumps("\u00e9")) - 2)
            request["goal"] = "\u00e9" * escaped + "x" * padding
            self.assertEqual(len(json.dumps(response).encode()), size)
            worker.receive.return_value = [{"ok": True, "structured": response}]
            return {}

        with (patch.object(delegation, "request", side_effect=decide),
              patch.object(delegation, "Worker") as launch):
            worker = launch.return_value
            worker.pid, worker.identity = 123, 456
            self.assertEqual(prior.decide(), [])
            self.assertEqual(ledger.actions(prior.lease), [])
            action, = prior.decide()
        self.assertEqual(action["request"]["prompt_id"], "delegate")
        ledger.transition(prior.lease, action["id"], "succeeded", {})
        record.move(prior.run.id, "failed", ("running",))

        resumed = self.controller(prior.run.id)
        resumed.run.action, resumed.run.action_only = action["id"], True
        with (patch.object(resumed, "worker_start", return_value={}) as start,
              patch.object(ledger, "accept", wraps=ledger.accept) as accept):
            self.assertEqual(resumed.drive()[0], "step")
        accept.assert_called_once()
        self.assertGreater(len(json.dumps(accept.call_args.args[3]).encode()), MAX_BYTES)
        original, rerun = ledger.actions(resumed.lease)
        self.assertEqual((rerun["state"], rerun["request"]), ("succeeded", original["request"]))
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0]["id"], rerun["id"])

    def test_refresh_refuses_with_live_workers_or_unfinished_actions(self):
        wait = OPERATIONS["run.wait"]
        cases = (
            ("pending", 123, "prior controller inference is still live"),
            ("running", 123, "worker .* is still live"),
            ("running", 321, "unfinished \\(running\\)"),
            ("accepted", None, "unfinished \\(accepted\\)"),
        )
        for state, pid, reason in cases:
            with self.subTest(state=state, pid=pid):
                prior = self.admit()
                if state == "pending":
                    turn = ledger.begin(prior.lease, {}, 10)
                    ledger.turn_started(prior.lease, turn, pid, 456)
                else:
                    self.accept(prior, "worker.start", state, pid=pid, identity=456)
                record.move(prior.run.id, "interrupted", ("running",))
                resumed = self.controller(prior.run.id)
                with (patch.dict(OPERATIONS, {"run.wait": replace(wait, description="Updated wait")}),
                      patch.object(delegation, "live", side_effect=lambda pid, identity: (pid, identity) == (123, 456)),
                      self.assertRaisesRegex(factory.Refused, f"operations_sha changed .*{reason}")):
                    resumed.prepare()
                self.assertEqual(ledger.snapshot(resumed.lease)["data"]["operations_sha"], prior.data["operations_sha"])
                stored = ledger.pending(resumed.lease) if state == "pending" else ledger.actions(resumed.lease)
                self.assertEqual(stored[0]["state"], state)
                self.assertEqual(self.audits(resumed), [])

    def test_refresh_refuses_and_names_authority_changes(self):
        for key in ("policy_sha", "controller_settings", "run_settings"):
            with self.subTest(key=key):
                prior = self.admit()
                prior.data[key] = "old-authority"
                ledger.save(prior.lease, prior.data)
                record.move(prior.run.id, "failed", ("running",))
                resumed = self.controller(prior.run.id)
                with self.assertRaisesRegex(factory.Refused, f"{key} changed"):
                    resumed.prepare()
                self.assertEqual(ledger.snapshot(resumed.lease)["data"][key], "old-authority")
                self.assertEqual(self.audits(resumed), [])

    def test_prompt_refresh_on_owner_answer_and_pr_feedback(self):
        source = orchestration.prompt_loader.template

        def deployed_prompt(name):
            template = source(name)
            return Template(template.template + "\nFollow the deployed prompt.") if name == "orchestrate" else template

        for continuation in ("owner answer", "PR feedback"):
            with self.subTest(continuation=continuation):
                prior = self.admit()
                feedback = []
                if continuation == "owner answer":
                    question = {"header": "Scope", "question": "Which scope?", "kind": "text"}
                    ask_store.ask_add("ask-lineage", prior.run.id, self.repo.name, self.ticket, "orchestrate",
                                      "orchestrate", "", "", [question], {})
                    self.accept(prior, "owner.ask", "succeeded", ask_ids=["ask-lineage"], awaiting_owner=True)
                    record.move(prior.run.id, "needs-info", ("running",))
                    ask_store.ask_close("ask-lineage", "answered", [{"text": "Keep the existing scope"}])
                    context = "Keep the existing scope"
                else:
                    pr = "https://github.com/example/fixture/pull/1"
                    context = "Revise the status message"
                    feedback = [{"pr_url": pr, "kind": "comment", "gh_id": 1, "body": context}]
                    record.move(prior.run.id, "pr", ("running",), pr_url=pr)
                resumed = self.controller(prior.run.id, feedback)
                with (patch.object(orchestration.prompt_loader, "template", side_effect=deployed_prompt),
                      patch.object(gh, "pr_view", return_value={"state": "OPEN"}),
                      patch.object(gh, "pr_body", return_value="")):
                    self.assertIsNone(resumed.prepare())
                audit, = self.audits(resumed)
                self.assertEqual(audit["old"], {"controller_prompt": prior.data["controller_prompt"]})
                self.assertEqual(audit["new"], {"controller_prompt": resumed.data["controller_prompt"]})
                self.assertNotEqual(audit["old"], audit["new"])
                self.assertIn(context, resumed.job.goal)

    def test_saved_preparation_feedback_is_consumed_only_after_child_launch(self):
        prior = self.admit()
        url = "https://github.com/example/fixture/pull/1"
        record.settle(prior.run.id, "pr", pr_url=url)
        insights_store.add_feedback([{"pr_url": url, "repo": self.repo.name, "issue": self.ticket, "kind": "comment",
                                      "gh_id": 1, "body": "Please revise", "pr_state": "OPEN", "status": "new",
                                      "at_once": 0, "created": "2026-01-01T00:00:00Z"}])
        script = pipeline.Script("orchestrate", orchestration.orchestrate, continues=prior.run.id)

        def admit_launch(*args):
            if failure == "model launch refused":
                raise factory.Refused(failure)

        with (patch.object(config, "load", return_value=self.cfg),
              patch.object(pipeline, "run_refusal", return_value=(True, "")),
              patch.object(gh, "pr_view", return_value={"state": "OPEN"}),
              patch.object(gh, "pr_body", return_value=""), patch.object(gh, "pr_comment"),
              patch.object(orchestration.Controller, "atomic") as atomic):
            for failure in ("child failed before launch", "model launch refused", "first model call failed"):
                with self.subTest(failure=failure), patch.object(delegation, "Worker") as worker:
                    child = worker.return_value
                    child.pid, child.identity = 123, 456
                    if failure == "child failed before launch":
                        messages = [{"event": "error", "error": failure}]
                    else:
                        messages = [{"event": "launch", "step": "orchestrate-decision", "role": "orchestrate",
                                     "route": {"runner": "replay"}},
                                    {"event": "result", "ok": False, "error": failure}]
                    child.receive.return_value = messages
                    self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, script=script,
                                                        admission=admit_launch), 1)
                    ended = run_store.newest_whole_run(self.repo.name, self.ticket)
                    self.assertIn(failure, ended["summary"])
                    worker.assert_called_once()
                    atomic.assert_not_called()
                    row, = insights_store.open_feedback([url])
                    launched = failure == "first model call failed"
                    if launched:
                        child.allow.assert_called_once_with(None)
                        self.assertEqual((row["status"], row["run_id"]), ("failed", ended["id"]))
                    else:
                        child.allow.assert_not_called()
                        self.assertEqual((row["status"], row["run_id"]), ("new", None))
                    self.assertEqual(insights_store.feedback_due(self.repo.name, "2026-01-01T00:10:00Z"),
                                     [] if launched else [self.ticket])


class ControllerStallTests(Isolated):
    STALL = {"ok": False, "error": "timed out after 120s", "turns": 0, "cost_usd": 0, "paid_usd": 0}

    def setUp(self):
        super().setUp()
        self.patches.enter_context(patch.object(pipeline, "workspace_note", return_value="Fixture workspace"))
        run = self.made_run()
        run.path = "orchestrate"
        self.controller = orchestration.Controller(flows.start(self.cfg, run, {}, continues=""))
        outcome = factory.Outcome("pass", data={
            "layer": str(self.repo.layer_dir), "checkouts": [], "ticket": asdict(self.local.get(self.ticket)),
            "branch": "fixture", "add_dirs": [], "attachments": "", "expertise": "", "triage": "",
        })

        def prepare_step(*args, **kwargs):
            self.controller.job.take("prepare", outcome, stored=False)
            return outcome

        with patch.object(self.controller, "atomic", side_effect=prepare_step):
            self.assertIsNone(self.controller.prepare())
        first = self.controller.run.settings("orchestrate")
        later = replace(first.chain[0], runner="codex", model="gpt-5.5", provider="openai", position=1,
                        requested=False)
        self.settings = replace(first, chain=(first.chain[0], later))
        self.controller.run.resolved_settings["orchestrate"] = self.settings

    def decision(self, turn):
        response = {"version": PROTOCOL, "decision_id": turn, "rationale": "",
                    "state_version": ledger.snapshot(self.controller.lease)["version"],
                    "calls": [{"op": "run.wait", "args": {"timeout_seconds": 0}}]}
        return {"ok": True, "structured": response, "turns": 1}

    def decide(self, *replies):
        asked = []

        def make(run, turn, request):
            worker = Mock(pid=123, identity=456)
            reply = replies[len(asked)]
            worker.receive.return_value = [reply(turn) if callable(reply) else reply]
            asked.append(turn)
            return worker

        def request(job, turn, prompt, setting, *rest):
            seen.append(setting)
            return {}

        seen = []
        with (patch.object(delegation, "request", side_effect=request),
              patch.object(delegation, "Worker", side_effect=make)):
            try:
                return self.controller.decide(), asked, seen
            except workspace.RunFailed as error:
                return error, asked, seen

    def test_a_stalled_first_turn_asks_again_and_applies_the_decision(self):
        actions, asked, seen = self.decide(self.STALL, self.decision)
        action, = actions
        self.assertEqual(action["request"]["timeout_seconds"], 0)
        self.assertEqual(asked, ["d0001", "d0001"])
        self.assertEqual(seen, [self.settings, self.settings])
        snapshot = ledger.snapshot(self.controller.lease)
        self.assertEqual((snapshot["turns"], snapshot["launches"]), (1, 0))
        self.assertEqual(self.controller.run.budget.repair_calls, 0)
        self.assertNotIn("error", self.controller.data)

    def test_a_stall_on_the_same_route_moves_to_the_next_route_in_the_chain(self):
        actions, asked, seen = self.decide(self.STALL, self.STALL, self.decision)
        self.assertEqual(len(actions), 1)
        self.assertEqual(asked, ["d0001"] * 3)
        self.assertEqual([(s.runner, s.model) for s in seen],
                         [(self.settings.runner, self.settings.model)] * 2 + [("codex", "gpt-5.5")])
        self.assertEqual(ledger.snapshot(self.controller.lease)["turns"], 1)

    def test_a_stall_on_every_route_fails_as_an_environment_failure_that_requeue_clears_later(self):
        error, asked, seen = self.decide(self.STALL, self.STALL, self.STALL)
        self.assertIsInstance(error, workspace.RunFailed)
        self.assertEqual(asked, ["d0001"] * 3)
        self.assertEqual(error.why, "timed out after 120s")
        run = self.controller.run
        pipeline._fail(run, error.why, recovery=error.recovery)
        self.assertEqual(run.failure, {"class": "environment", "reason": "controller-stall"})
        finished = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
        row = {"failure": json.dumps(run.failure), "finished": util.iso(finished)}
        self.assertEqual(requeue.recorded_failure(row), run.failure)
        for minutes, cleared in ((9, False), (10, True)):
            with self.subTest(minutes=minutes), patch.object(util, "now", return_value=finished + timedelta(minutes=minutes)):
                self.assertIs(requeue.cleared(self.cfg, self.repo, config.RepoLayer(dir=Path()),
                                              self.local.get(self.ticket), row, run.failure), cleared)

    def test_a_turn_that_replied_and_then_failed_is_not_asked_again(self):
        error, asked, seen = self.decide({**self.STALL, "turns": 2, "error": "provider closed the stream"})
        self.assertEqual(asked, ["d0001"])
        self.assertIsNone(error.recovery)

    def test_a_stall_that_paid_for_usage_is_not_asked_again(self):
        error, asked, seen = self.decide({**self.STALL, "paid_usd": 0.02})
        self.assertEqual(asked, ["d0001"])
        self.assertIsNone(error.recovery)


class PolicyRestartLineageTests(Isolated):
    PR = "https://github.com/owner/project/pull/1"
    GATES = (("checks.run", "checks"), ("verification.run", "verify"), ("review.run", "review"))
    OLD_POLICY = "0" * 64

    def setUp(self):
        super().setUp()
        for target, name, value in ((pipeline, "workspace_note", "Fixture workspace"), (factory, "fingerprint", "fixture-tree"),
                                    (gh, "pr_merge_details", {"state": "OPEN", "baseRefName": "main"}),
                                    (workspace, "fetch", None), (pipeline, "merge_moved_bases", []),
                                    (workspace, "kept_work", ""), (workspace, "scratch_held", []),
                                    (flows.Job, "reclaim", "")):
            self.patches.enter_context(patch.object(target, name, return_value=value))
        worktree = paths.WORKTREES_DIR / "fixture-revision"
        (worktree / ".git").mkdir(parents=True)
        self.checkout = workspace.Checkout("", "owner/project", self.repo.path, worktree, "main", self.PR, "fixture-head")

    def controller(self, previous=""):
        run = self.made_run(previous)
        run.path = "orchestrate"
        return orchestration.Controller(flows.start(self.cfg, run, {}, continues=previous))

    def action(self, controller, op, args, state="accepted", **result):
        lease = controller.lease
        turn = ledger.begin(lease, {}, 100)
        action, = ledger.accept(lease, turn, ledger.snapshot(lease)["version"],
                                {"calls": [{"op": op, "args": OPERATIONS[op].arguments(args)}]}, 100)
        if state != "accepted":
            ledger.transition(lease, action["id"], state, result)
        return action

    def saved(self, *, policy_changed=True):
        controller = self.controller()
        prepared = factory.Outcome("pass", data={
            "layer": str(self.repo.layer_dir), "checkouts": [self.checkout.to_row()],
            "ticket": asdict(self.local.get(self.ticket)), "branch": "fixture", "add_dirs": [], "attachments": "",
            "expertise": "", "triage": "",
        })

        def prepare_step(*args, **kwargs):
            controller.job.take("prepare", prepared, stored=False)
            return prepared

        with patch.object(controller, "atomic", side_effect=prepare_step):
            self.assertIsNone(controller.prepare())
        evidence, produced = {}, {}
        policy = self.OLD_POLICY if policy_changed else controller.policy_sha()
        tree = "old-tree" if policy_changed else controller.fingerprint()
        args = {"checks": {"scope": "full"}, "verify": {"route_id": "verify", "effort": "low"},
                "review": {"route_id": "review", "effort": "low"}}
        for op, gate in self.GATES:
            evidence[gate] = {"tree": tree, "policy": policy, "result": {"summary": f"old {gate}"},
                              "acceptance": controller.data["acceptance"], "producer": "d0000-0",
                              "observed": "2026-11-20T00:00:00Z"}
            if gate == "checks":
                evidence[gate]["result"]["full"] = True
            details = {"verdict": "pass"} if gate == "verify" else {}
            action = self.action(controller, op, args[gate], "succeeded", text=f"old {gate}", evidence=evidence[gate],
                                 **details)
            produced[gate] = action["id"]
        job = controller.job
        job.review = pipeline.Review.stored({"verdict": "approve", "summary": "old review", "problems": []})
        job.verified, job.reviews, job.ci_result = pipeline.verify.Verified("pass", "old verify"), 1, "old ci"
        controller.data.pop("review_method", None)  # This saved lineage predates the Standards/Spec review.
        controller.data.update(evidence=evidence, checked={"tree": "old-tree"}, report="old checks",
                               policy_sha=policy)
        controller.persist()
        record.move(controller.run.id, "failed", ("running",))
        return controller, produced

    def audits(self, controller):
        return [line for line in run_lines.read(controller.run.id) if line["event"] == "lineage"]

    def rerun_gates(self, controller, results):
        atomic_calls = []

        def atomic(name, **kwargs):
            atomic_calls.append(name)
            controller.job.take(name, results[name], stored=False)
            return results[name]

        with (patch.object(controller, "atomic", side_effect=atomic),
              patch.object(orchestration.Catalog, "route", side_effect=lambda key, effort, roles: controller.run.settings(roles[0]))):
            for (op, _gate), args in zip(self.GATES, ({"scope": "full"}, {"route_id": "model:verify:0", "effort": "low"},
                                                      {"route_id": "review", "effort": "low"}), strict=True):
                done = self.action(controller, op, args)
                self.assertIsNone(controller.dispatch(done))
        return atomic_calls

    def test_legacy_continuation_replaces_only_its_review_and_preserves_the_new_review_on_resume(self):
        prior, produced = self.saved(policy_changed=False)
        resumed = self.controller(prior.run.id)
        self.assertIsNone(resumed.prepare())
        self.assertTrue(resumed.run.code_review)
        self.assertEqual(resumed.data["stale_actions"], [produced["review"]])
        self.assertTrue(resumed.current("checks"))
        self.assertTrue(resumed.current("verify"))
        self.assertFalse(resumed.current("review"))
        self.assertIsNone(resumed.job.review.verdict)
        self.assertEqual(resumed.latest_code_review(), {})
        with self.assertRaisesRegex(ValueError, "publication needs current evidence: review$"):
            resumed.publish()

        report = {"method": "code-review", "verdict": "pending", "summary": "fresh finding", "problems": [],
                  "findings": [{"id": "spec-1", "axis": "Spec", "file": "example.py", "finding": "Missing case"}]}
        outcome = factory.Outcome("pass", data={"review": report, "attempt": 1})
        fresh = self.action(resumed, "review.run", {"route_id": "review", "effort": "low"})

        def review_step(name, **kwargs):
            self.assertTrue(kwargs["code_review"])
            resumed.job.take(name, outcome, stored=False)
            return outcome

        with (patch.object(resumed, "atomic", side_effect=review_step),
              patch.object(orchestration.Catalog, "route", return_value=SimpleNamespace(step="review"))):
            self.assertIsNone(resumed.dispatch(fresh))
        record.move(resumed.run.id, "stopped", ("running",))
        repeated = self.controller(resumed.run.id)
        self.assertIsNone(repeated.prepare())
        self.assertEqual(repeated.data["evidence"]["review"]["producer"], fresh["id"])
        self.assertEqual(repeated.job.review.verdict, "pending")
        self.assertTrue(repeated.current("review"))

    def test_batched_review_waits_for_full_checks_on_the_integrated_candidate(self):
        prior, _ = self.saved()
        ctl = self.controller(prior.run.id)
        self.assertIsNone(ctl.prepare())
        fingerprint = self.enterContext(patch.object(ctl, "fingerprint", return_value="candidate"))
        self.enterContext(patch.object(orchestration.code_review, "bases", return_value={"": "base"}))
        pending = {"method": "code-review", "verdict": "pending", "summary": "Missing edge", "problems": [],
                   "findings": [{"id": "spec-1", "finding": "Missing edge"}], "bases": {"": "base"}}
        reviewer = Mock(return_value=factory.Outcome("pass", data={"review": pending, "attempt": 1}))
        check = Mock()

        def step(name, args, kwargs):
            if name == "review":
                self.assertTrue(kwargs["code_review"])
                return reviewer()
            self.assertEqual(name, "checks")
            return check()

        self.enterContext(patch.object(ctl.flow, "run", side_effect=step))
        review = {"op": "review.run", "args": OPERATIONS["review.run"].arguments(
            {"route_id": "model:review:0", "effort": "low"})}

        def batch(*calls):
            turn = ledger.begin(ctl.lease, {}, 100)
            actions = ledger.accept(ctl.lease, turn, ledger.snapshot(ctl.lease)["version"], {"calls": list(calls)}, 100)
            for action in actions:
                ctl.dispatch(action)
            return ledger.actions(ctl.lease)[-1]

        def checks(scope, status):
            check.return_value = factory.Outcome(status, data={
                "scope": scope, "full": scope == "full" and status == "pass", "report": "configured check result"})
            return {"op": "checks.run", "args": OPERATIONS["checks.run"].arguments(
                {"scope": "quick" if scope == "quick" else "full"})}

        for scope, status in (("full", "fail"), ("quick", "pass"), ("failed", "fail"), ("failed", "pass")):
            with self.subTest(scope=scope, status=status):
                deferred = batch(checks(scope, status), review)
                self.assertEqual(deferred["state"], "failed")
                self.assertTrue(deferred["result"]["review_deferred"])
                reviewer.assert_not_called()
        batch(checks("full", "pass"))
        fingerprint.return_value = "later integration"
        self.assertTrue(batch(review)["result"]["review_deferred"])
        reviewer.assert_not_called()

        first = batch(checks("full", "pass"), review)
        self.assertEqual(first["state"], "succeeded")
        reviewer.assert_called_once()
        fingerprint.return_value = "repair"
        self.assertEqual(batch(review)["result"]["reused_review"], first["id"])
        fingerprint.return_value = "later integration"
        ctl.data["acceptance"] = "new owner requirements"
        self.assertTrue(batch(review)["result"]["review_deferred"])
        ctl.recover()
        self.assertEqual(ctl.data["evidence"]["review"]["producer"], first["id"])
        ctl.executing = ""
        self.assertEqual(ctl.latest_code_review()["id"], first["id"])
        self.assertFalse(ctl.current("review"))
        reviewer.assert_called_once()
        with (patch.object(ctl.run, "environment_release", "live"),
              patch.object(ctl.flow, "run", return_value=reviewer.return_value) as legacy):
            self.assertEqual(batch(review)["state"], "succeeded")
            self.assertNotIn("code_review", legacy.call_args.args[2])

    def test_changed_policy_at_a_decision_boundary_reruns_every_gate_before_the_merge_gate(self):
        prior, produced = self.saved()
        resumed = self.controller(prior.run.id)
        self.assertIsNone(resumed.prepare())

        self.assertEqual(resumed.data["policy_sha"], resumed.policy_sha())
        self.assertNotEqual(resumed.data["policy_sha"], self.OLD_POLICY)
        self.assertEqual(resumed.data["evidence"], {})
        self.assertEqual((resumed.job.review.verdict, resumed.job.verified.verdict), (None, "unverified"))
        self.assertEqual((resumed.job.b.checked, resumed.job.b.report, resumed.job.reviews, resumed.job.ci_result),
                         ({}, "", 0, ""))
        self.assertEqual([c.pr for c in resumed.job.checkouts], [self.PR])
        self.assertTrue((self.checkout.wt / ".git").exists())
        for gate in produced:
            self.assertFalse(resumed.current(gate))
            self.assertNotIn(produced[gate], resumed.catalog.context)
        with self.assertRaisesRegex(ValueError, "publication needs current evidence: checks, review$"):
            resumed.publish()

        fresh = {
            "checks": factory.Outcome("pass", data={"full": True, "report": "fresh checks",
                                                    "checked": {"tree": "fresh-tree", "commands": {}}}),
            "verify": factory.Outcome("pass", data={"verified": {"verdict": "pass", "summary": "fresh verify"},
                                                    "names": []}),
            "review": factory.Outcome("pass", data={"review": {"method": "code-review", "verdict": "approve",
                                                               "summary": "fresh review", "problems": []}, "attempt": 1}),
        }
        self.assertEqual(self.rerun_gates(resumed, fresh), ["checks", "verify", "review"])
        for gate, old_action in produced.items():
            self.assertTrue(resumed.current(gate))
            self.assertNotEqual(resumed.data["evidence"][gate]["producer"], old_action)
        self.assertNotIn("old", json.dumps(resumed.data["evidence"]))

        with (patch.object(pipeline, "automerge_allowed", return_value=True),
              patch.object(pipeline, "try_automerge", return_value=None) as merge,
              patch.object(resumed, "take_merge_lease")):
            self.assertIsNone(resumed.automerge([resumed.job.checkouts[0]]))
        _, checkout, verified, review, _ = merge.call_args.args
        self.assertEqual((checkout.pr, verified.summary, review.summary), (self.PR, "fresh verify", "fresh review"))
        self.assertEqual(resumed.job.b.report, "fresh checks")
        self.assertEqual(resumed.job.b.checked["tree"], "fresh-tree")

    def test_changed_policy_with_a_live_worker_or_unfinished_action_is_still_refused(self):
        for state, live in (("running", True), ("accepted", False)):
            with self.subTest(state=state):
                prior, produced = self.saved()
                self.action(prior, "worker.start", {"route_id": "model:build:0", "effort": "low", "goal": "Revise."},
                            state, pid=4242, identity="fixture")
                resumed = self.controller(prior.run.id)
                with (patch.object(delegation, "live", return_value=live),
                      self.assertRaisesRegex(factory.Refused, r"policy_sha changed \(00000000 → .*; .*; reconcile before resuming")):
                    resumed.prepare()
                self.assertEqual(self.audits(resumed), [])
                data = ledger.snapshot(resumed.lease)["data"]
                self.assertEqual(data["policy_sha"], self.OLD_POLICY)
                self.assertEqual(set(data["evidence"]), set(produced))
                self.assertNotIn("stale_actions", data)

    def test_new_lineage_writes_one_lineage_event_naming_policy_sha(self):
        prior, _ = self.saved()
        resumed = self.controller(prior.run.id)
        self.assertIsNone(resumed.prepare())
        audit, = self.audits(resumed)
        self.assertEqual(audit["old"], {"policy_sha": self.OLD_POLICY})
        self.assertEqual(audit["new"], {"policy_sha": resumed.policy_sha()})
        self.assertTrue(audit["new_lineage"])
        self.assertEqual((resumed.run.dir / "run.log").read_text().count("started a new controller lineage"), 1)
        self.assertNotIn("lineage_event", ledger.snapshot(resumed.lease)["data"])

        record.move(resumed.run.id, "interrupted", ("running",))
        repeated = self.controller(resumed.run.id)
        self.assertIsNone(repeated.prepare())
        self.assertEqual(self.audits(repeated), [])
        self.assertEqual(self.audits(resumed), [audit])


class OrchestrationWaitTests(Isolated):
    def setUp(self):
        super().setUp()
        self.now = 0.0
        self.patches.enter_context(patch.object(orchestration, "time", SimpleNamespace(
            monotonic=lambda: self.now, sleep=self.advance)))
        controller = self.controller = orchestration.Controller.__new__(orchestration.Controller)
        controller.run = self.made_run()
        controller.run.budget = None
        controller.lease = ledger.acquire(controller.run.id, "", {})
        controller.workers, controller.progress, controller.reported = {}, {}, {}
        controller.last_progress = float("-inf")
        controller.policy = config.Orchestration()
        controller.job = flows.Job(self.cfg, controller.run, self.repo.path, ticket=self.local.get(self.ticket))
        controller.job.use_layer(config.RepoLayer(self.repo.layer_dir))
        controller.catalog = Catalog(controller.job)
        controller.data = {"acceptance": "fixture"}
        controller.fingerprint = controller.policy_sha = lambda: "fixture"
        controller.persist = lambda: None

    def advance(self, seconds):
        self.now += seconds

    def accept(self, calls, maximum, *, validate=True):
        lease = self.controller.lease
        version = ledger.snapshot(lease)["version"]
        turn = ledger.begin(lease, {}, maximum)
        ledger.launched(lease, maximum)
        response = {"version": PROTOCOL, "decision_id": turn, "state_version": version,
                    "rationale": "", "calls": calls}
        if validate:
            response = self.controller.catalog.validate(
                response, turn, version, {row["id"]: row for row in ledger.actions(lease)})
        return ledger.accept(lease, turn, version, response, self.controller.policy.max_actions)

    def start_worker(self, deadline, *, finish=None, ok=True):
        controller = self.controller
        action, = self.accept([{"op": "worker.start", "args": {}}], 2, validate=False)
        worker = delegation.Worker.__new__(delegation.Worker)
        worker.action, worker.deadline, worker.writing = action["id"], deadline, False
        worker.run_id, worker.step = controller.run.id, f"worker-{worker.action}"
        worker.effective = {"prompt_id": "investigate"}
        worker.progress, worker.sources, worker.report = [], {}, (0, {})
        worker.proc = SimpleNamespace(poll=lambda: None)
        worker.close = lambda: None
        worker.receive = lambda: ([{"event": "result", "ok": ok}] if finish is not None and self.now >= finish else [])
        controller.workers[worker.action] = worker
        ledger.transition(controller.lease, worker.action, "running", {})
        return worker

    def wait(self, args, maximum=8, extra=()):
        actions = self.accept([{"op": "run.wait", "args": args}, *extra], maximum)
        for action in actions:
            self.controller.dispatch(action)
        return next(row for row in ledger.actions(self.controller.lease) if row["id"] == actions[0]["id"])

    def test_explicit_60_second_wait_keeps_cap_and_long_wait_reaches_worker_result(self):
        worker = self.start_worker(2700, finish=1800)
        capped = self.wait({"timeout_seconds": 60}, maximum=2)
        self.assertEqual((self.now, capped["result"]["active"]), (60, [worker.action]))
        self.assertEqual(ledger.snapshot(self.controller.lease)["turns"], 1)
        completed = self.wait({"timeout_seconds": 0}, maximum=2)
        self.assertLess(self.now - 1800, 0.1)
        self.assertEqual((completed["id"], completed["result"]["active"]), ("d0003-0", []))
        state = ledger.view(self.controller.run.id)
        self.assertEqual((state["decisions"], state["launches"]), (2, 3))
        self.assertEqual(state["actions"][0]["state"], "succeeded")

    def test_idle_waits_return_immediately_and_exhaust_decisions(self):
        for _ in range(2):
            self.assertEqual(self.wait({}, maximum=2)["result"]["active"], [])
        self.assertEqual(self.now, 0)
        with self.assertRaisesRegex(ledger.Conflict, "decision allowance"):
            self.wait({}, maximum=2)

    def test_mixed_decision_keeps_charge_for_unchanged_wait(self):
        worker = self.start_worker(2700)
        steer = {"op": "worker.steer", "args": {"worker_id": worker.action, "message": "Check the log."}}
        settled = self.wait({}, maximum=2, extra=(steer,))
        self.assertEqual((self.now, settled["result"]["active"]), (60, [worker.action]))
        self.assertEqual(self.controller.data["steering"], {worker.action: ["Check the log."]})
        with self.assertRaisesRegex(ledger.Conflict, "decision allowance"):
            self.wait({}, maximum=2)

    def test_wait_ends_at_worker_deadline_and_an_expired_wait_costs_a_decision(self):
        self.start_worker(10)
        self.now = 4
        self.wait({"timeout_seconds": 1800}, maximum=2)
        self.assertEqual(self.now, 10)
        self.assertEqual(ledger.snapshot(self.controller.lease)["turns"], 1)
        self.wait({"timeout_seconds": 0}, maximum=2)
        self.assertEqual(self.now, 10)
        with self.assertRaisesRegex(ledger.Conflict, "decision allowance"):
            self.wait({}, maximum=2)

    def test_run_deadline_interrupts_long_wait(self):
        self.start_worker(2700)
        self.controller.run.budget = SimpleNamespace(run_deadline=lambda: 2, reason=lambda: "run deadline")
        with self.assertRaisesRegex(runlimits.Parked, "run deadline"):
            self.wait({"timeout_seconds": 0})
        self.assertEqual(self.now, 2)

    def test_new_progress_wakes_wait_and_worker_failure_wakes_next_wait(self):
        worker = self.start_worker(300, finish=5, ok=False)
        source = paths.record_dir(worker.run_id) / (worker.step + record.RECORD_SUFFIX)
        source.parent.mkdir(parents=True, exist_ok=True)
        with source.open("ab") as output:
            for event in ({"type": "replay"}, {"type": "heartbeat"},
                          {"type": "item.completed", "item": {"type": "command_execution"}},
                          {"type": "item.completed", "item": {"type": "agent_message", "text": "Checks are running."}}):
                record.append(output.fileno(), json.dumps(event))
        update = self.wait({"timeout_seconds": 0})
        self.assertIn("Checks are running.", update["result"]["progress"][worker.action]["excerpt"])
        self.assertEqual(self.now, 0)
        self.assertEqual(ledger.snapshot(self.controller.lease)["turns"], 2)
        failed = self.wait({"timeout_seconds": 60})
        self.assertEqual(failed["result"]["active"], [])
        self.assertLess(self.now - 5, 0.1)
        self.assertEqual(ledger.view(self.controller.run.id)["actions"][0]["state"], "failed")


class OrchestrationRecoveryFixture(Isolated):
    def setUp(self):
        super().setUp()
        self.cfg.replay = config.ReplayConfig(all=True)
        self.producers = {}
        self.patches.enter_context(patch.object(delegation, "Worker", side_effect=self.spawn))
        self.patches.enter_context(patch.object(pipeline, "workspace_note", return_value="Fixture workspace"))

    def controller(self, previous="", *, builders=None):
        run = self.made_run()
        run.path, run.budget = "orchestrate", None
        layer = config.RepoLayer(self.repo.layer_dir, orchestration=config.Orchestration(max_builders=builders))
        tree = self.repo.path
        checkout = workspace.Checkout("", self.repo.name, tree, tree, "main")
        job = flows.Job(self.cfg, run, tree, ticket=self.local.get(self.ticket), checkouts=[checkout])
        job.use_layer(layer)
        with patch.object(config, "load_layer", return_value=layer):
            controller = orchestration.Controller(SimpleNamespace(ctx=job, continues=previous))
        controller.catalog = Catalog(job, controller.protocol)
        controller.data.update(acceptance="fixture", limits=controller.configured_limits())
        controller.fingerprint = lambda: "fixture-tree"
        return controller

    def restart(self, controller):
        record.move(controller.run.id, "interrupted", ("running",))
        resumed = self.controller(controller.run.id, builders=controller.policy.max_builders)
        resumed.recover()
        return resumed

    def spawn(self, run, action, request):
        self.assertNotIn(action, self.producers)
        worker = SimpleNamespace(pid=123, identity=456, action=action, step=request["step"], deadline=float("inf"),
                                 cwd=Path(request["cwd"]), request=request, allow=Mock(), close=Mock(),
                                 observations=dict, report=(0, {}), proc=SimpleNamespace(poll=lambda: None))
        worker.receipts = [{"event": "launch", "step": worker.step, "role": request["role"], "route": {}}]

        def receive():
            receipts, worker.receipts = worker.receipts, []
            return receipts

        worker.receive = receive
        self.producers[action] = worker
        return worker

    def report_worker_result(self, action, text="Fixture result", *, outcome="done"):
        self.producers[action].receipts.append({"event": "result", "ok": True, "text": text, "structured": {
            "version": 2, "outcome": outcome, "text": text, "ack_ids": []}})

    def finish_worker(self, controller, action, text="Fixture result"):
        self.report_worker_result(action, text)
        controller.collect()

    def route(self, controller, role):
        route = f"model:{role}:0"
        return {"route_id": route, "effort": controller.catalog.entries[route]["efforts"][0]}

    def start(self, controller, **args):
        return {"op": "worker.start", "args": {**self.route(controller, "plan"), "goal": "Fixture work",
                "tool_bundle": "read", "prompt_id": "investigate", **args}}

    def accept(self, controller, *calls, rationale="Fixture replay"):
        lease = controller.lease
        turn = ledger.begin(lease, {}, controller.decision_limit)
        version = ledger.snapshot(lease)["version"]
        known = {a["id"]: a for a in ledger.actions(lease)}
        response = controller.catalog.validate({"version": controller.protocol, "decision_id": turn,
                                                "state_version": version, "rationale": rationale,
                                                "calls": list(calls)}, turn, version, known)
        if controller.exchanged:
            controller.preflight(response["calls"])
        return ledger.accept(lease, turn, version, response, controller.action_limit)

    def send(self, controller, *calls):
        actions = self.accept(controller, *calls)
        for action in actions:
            self.assertIsNone(controller.dispatch(action))
        return [a["id"] for a in actions]

    def receipt(self, controller, action):
        return next(a for a in ledger.actions(controller.lease) if a["id"] == action)

    def commit_base(self):
        tree = self.repo.path
        (tree / "fixture.txt").write_text("base\n")
        util.git(["init", "-q", "-b", "main", "--template="], tree)
        util.git(["add", "fixture.txt"], tree)
        util.git([*util.ARETE_IDENTITY, "commit", "-qm", "Base"], tree)

    def complete_at_launch(self, run, action, request):
        worker = self.spawn(run, action, request)
        self.report_worker_result(action, "Replay result")
        return worker

    def completed_dependent(self, controller):
        prerequisite, = self.send(controller, self.start(controller))
        selected, = self.send(controller, self.start(controller, depends_on=[prerequisite]))
        self.finish_worker(controller, prerequisite)
        controller.resume_queued()
        self.finish_worker(controller, selected)
        return selected


class DependentWorkerRecoveryTests(OrchestrationRecoveryFixture):
    def test_writing_prerequisite_exposes_integrated_tree_across_restart_under_limits(self):
        self.commit_base()
        c = self.controller(builders=2)

        def build_call(path, **args):
            return self.start(c, **self.route(c, "build"), prompt_id="build", tool_bundle="write",
                              write_paths=[path], acceptance_check=f"Inspect {path}", **args)

        writer_a, = self.send(c, build_call("fixture.txt"))
        dependent_b, = self.send(c, build_call("fixture.txt", depends_on=[writer_a]))
        unrelated_c, = self.send(c, build_call("other.txt"))
        self.assertEqual(set(c.workers), {writer_a, unrelated_c})
        self.assertNotIn(dependent_b, self.producers)
        visible = {w["id"]: w for w in c.observe()["workers"]}
        self.assertEqual((visible[dependent_b]["phase"], visible[dependent_b]["depends_on"]), ("queued", [writer_a]))
        self.assertIn(f"waiting for prerequisite {writer_a}", visible[dependent_b]["reason"])
        with self.assertRaisesRegex(ValueError, "overlap an outstanding builder claim"):
            self.accept(c, build_call("fixture.txt"))
        with self.assertRaisesRegex(ValueError, "exceed available worker or builder capacity"):
            self.accept(c, build_call("third.txt"))

        self.finish_worker(c, unrelated_c)
        c.close()
        resumed = self.restart(c)
        self.assertTrue(ledger.queued_start(self.receipt(resumed, dependent_b)))
        self.assertNotIn(dependent_b, self.producers)

        def decisions():
            retry_a = exchange.workers(resumed.lease)[writer_a]["attempt"]
            (self.producers[retry_a].cwd / "fixture.txt").write_text("from-a\n")
            self.report_worker_result(retry_a)
            yield self.accept(resumed, {"op": "run.wait", "args": {}})
            self.assertEqual(exchange.workers(resumed.lease)[writer_a]["phase"], "integrated")
            self.assertEqual(set(resumed.workers), {dependent_b})
            self.assertEqual((self.producers[dependent_b].cwd / "fixture.txt").read_text(), "from-a\n")
            (self.producers[dependent_b].cwd / "fixture.txt").write_text("from-b\n")
            self.finish_worker(resumed, dependent_b)
            yield self.accept(resumed, {"op": "run.finish", "args": {"reason": "noop"}})

        with (patch.object(resumed, "prepare", return_value=None),
              patch.object(resumed, "decide", side_effect=decisions()),
              patch.object(resumed.job.b, "restage", return_value=SimpleNamespace(empty=True)),
              patch.object(flows, "finish", return_value=("nothing", "Fixture noop", ""))):
            self.assertEqual(resumed.drive()[0], "nothing")
        self.assertEqual(exchange.workers(resumed.lease)[dependent_b]["phase"], "integrated")
        self.assertEqual((self.repo.path / "fixture.txt").read_text(), "from-b\n")
        self.producers[dependent_b].allow.assert_called_once()

    def test_admission_interruption_cleans_up_only_the_uncommitted_candidate(self):
        self.commit_base()
        c = self.controller(builders=1)
        a, = self.send(c, self.start(c))
        b, = self.send(c, self.start(c, depends_on=[a]))
        c.resume_queued()
        unrelated, = self.send(c, self.start(c))
        self.assertEqual(set(c.workers), {a, unrelated})
        self.assertNotIn(b, self.producers)
        self.finish_worker(c, unrelated)
        self.finish_worker(c, a)

        @contextlib.contextmanager
        def interrupt_transaction(commit):
            with foundation.tx() as connection:
                yield connection
                if not commit:
                    raise pipeline.Terminated()
            raise pipeline.Terminated()

        candidates = orchestration.builder_workspaces
        existing = set(paths.WORKTREES_DIR.glob("candidate-*"))
        objects = set((paths.STATE_DIR / "builder-objects").iterdir())
        with (patch.object(exchange, "tx", return_value=interrupt_transaction(False)),
              self.assertRaises(pipeline.Terminated)):
            c.resume_queued()
        queued = exchange.workers(c.lease)[b]
        self.assertEqual(queued["phase"], "queued")
        self.assertNotIn("candidate", queued["data"])
        self.assertEqual(set(paths.WORKTREES_DIR.glob("candidate-*")), existing)
        self.assertEqual(set((paths.STATE_DIR / "builder-objects").iterdir()), objects)
        self.assertTrue(ledger.queued_start(self.receipt(c, b)))
        c.close()
        c = self.restart(c)

        with (patch.object(exchange, "tx", return_value=interrupt_transaction(True)),
              self.assertRaises(pipeline.Terminated)):
            c.resume_queued()
        assigned = exchange.workers(c.lease)[b]
        self.assertEqual(assigned["phase"], "assigned")
        candidate = assigned["data"]["candidate"]
        root = candidates.folder(candidate["token"])
        self.assertTrue(root.is_dir())
        self.assertTrue(candidates.repository(candidate["token"], 0).is_dir())
        self.assertNotIn(b, self.producers)
        c.close()
        c = self.restart(c)
        c.resume_queued()
        self.assertEqual(self.producers[b].cwd, root)
        self.finish_worker(c, b)
        c.resume_queued()
        self.producers[b].allow.assert_called_once()
        self.assertEqual(self.receipt(c, b)["state"], "succeeded")

    def test_private_dependent_setup_failure_parks_and_continues_its_candidate(self):
        self.commit_base()
        c = self.controller(builders=1)
        reader, = self.send(c, self.start(c))
        writer, = self.send(c, self.start(c, **self.route(c, "build"), prompt_id="build", tool_bundle="write",
                                           write_paths=["fixture.txt"], acceptance_check="Read fixture.txt",
                                           depends_on=[reader]))
        c.resume_queued()
        self.assertTrue(ledger.queued_start(self.receipt(c, writer)))
        self.finish_worker(c, reader)
        fingerprint = factory.fingerprint

        def fail_after_assignment(tree):
            worker = exchange.workers(c.lease)[writer]
            return "" if worker["phase"] == "assigned" else fingerprint(tree)

        with patch.object(factory, "fingerprint", side_effect=fail_after_assignment):
            c.resume_queued()
        failed = self.receipt(c, writer)
        self.assertEqual(failed["state"], "failed")
        self.assertIn("workspace could not be recorded", failed["result"]["error"])
        self.assertEqual(failed["result"]["effective"], failed["request"])
        self.assertEqual(failed["result"]["worker_id"], writer)
        self.assertFalse(ledger.unlaunched_start(failed))
        parked = exchange.workers(c.lease)[writer]
        self.assertEqual((parked["phase"], parked["attempt"]), ("parked", writer))
        token = parked["data"]["candidate"]["token"]
        c.resume_queued()
        self.assertNotIn(writer, self.producers)

        continued, = self.send(c, {"op": "worker.continue", "args": {"worker_id": writer, "message": "Try again"}})
        producer = self.producers[continued]
        self.assertEqual(producer.cwd, orchestration.builder_workspaces.folder(token))
        (producer.cwd / "fixture.txt").write_text("recovered\n")
        self.finish_worker(c, continued)
        self.assertEqual(exchange.workers(c.lease)[writer]["phase"], "integrated")
        self.assertEqual((self.repo.path / "fixture.txt").read_text(), "recovered\n")
        producer.allow.assert_called_once()


class DependentWorkerRerunTests(OrchestrationRecoveryFixture):
    def test_standalone_dependent_rerun_does_not_dispatch_older_wait(self):
        self.commit_base()
        c = self.controller(builders=1)
        selected = self.completed_dependent(c)
        wait, = self.accept(c, {"op": "run.wait", "args": {}})
        resumed = self.restart(c)
        resumed.run.action, resumed.run.action_only = selected, True
        before = set(self.producers)

        with (patch.object(resumed, "prepare", return_value=None),
              patch.object(resumed, "wait_workers", side_effect=AssertionError("standalone rerun dispatched wait")),
              patch.object(delegation, "Worker", side_effect=self.complete_at_launch) as launch):
            self.assertEqual(resumed.drive()[0], "step")
        launch.assert_called_once()
        rerun, = set(self.producers) - before
        result = self.receipt(resumed, rerun)
        self.assertEqual((result["state"], result["request"]), ("succeeded", self.receipt(resumed, selected)["request"]))
        self.assertEqual(self.receipt(resumed, wait["id"]), wait)
        self.producers[rerun].allow.assert_called_once()

    def test_continuing_dependent_rerun_launches_before_older_failed_finish(self):
        self.commit_base()
        c = self.controller(builders=1)
        selected = self.completed_dependent(c)
        older, = self.accept(c, {"op": "run.finish", "args": {"reason": "failed"}})
        resumed = self.restart(c)
        resumed.run.action = selected
        before = set(self.producers)

        def decide():
            rerun, = resumed.workers
            self.assertEqual(self.receipt(resumed, rerun)["state"], "running")
            receipt = self.receipt(resumed, older["id"])
            self.assertEqual(receipt["state"], "failed")
            self.assertIn("workers are still active", receipt["result"]["error"])
            self.finish_worker(resumed, rerun)
            raise runlimits.Parked("Fixture paused after refusing older finish")

        with (patch.object(resumed, "prepare", return_value=None), patch.object(resumed, "decide", side_effect=decide),
              patch.object(resumed, "report_failed_finish", return_value=contextlib.nullcontext()),
              patch.object(flows, "finish") as finish, self.assertRaisesRegex(runlimits.Parked, "Fixture paused")):
            resumed.drive()
        finish.assert_not_called()
        rerun, = set(self.producers) - before
        result = self.receipt(resumed, rerun)
        self.assertEqual((result["state"], result["request"]), ("succeeded", self.receipt(resumed, selected)["request"]))
        self.producers[rerun].allow.assert_called_once()

    def test_standalone_dependent_rerun_still_checks_prerequisites(self):
        self.commit_base()
        c = self.controller(builders=1)
        prerequisite, = self.send(c, self.start(c))
        self.send(c, {"op": "worker.cancel", "args": {"worker_id": prerequisite, "reason": "Fixture stop"}})
        saved, = self.accept(c, self.start(c, depends_on=[prerequisite]))
        ledger.transition(c.lease, saved["id"], "failed", {"effective": saved["request"], "error": "Saved setup failure"})
        wait, = self.accept(c, {"op": "run.wait", "args": {}})
        resumed = self.restart(c)
        resumed.run.action, resumed.run.action_only = saved["id"], True

        with patch.object(resumed, "prepare", return_value=None):
            self.assertEqual(resumed.drive()[0], "failed")
        rerun, = [a for a in ledger.actions(resumed.lease) if a["run"] == resumed.run.id]
        self.assertEqual((rerun["state"], rerun["result"]["reason"]),
                         ("cancelled", f"prerequisite {prerequisite} was cancelled"))
        self.assertEqual(self.receipt(resumed, wait["id"]), wait)
        self.assertEqual(set(self.producers), {prerequisite})

    def test_serial_worker_rerun_preserves_the_queued_dependency(self):
        for standalone in (True, False):
            with self.subTest(standalone=standalone):
                self.producers.clear()
                c = self.controller()
                prior, = self.send(c, self.start(c))
                dependent, = self.send(c, self.start(c, depends_on=[prior]))
                c.close()
                queued = self.receipt(c, dependent)
                self.assertTrue(ledger.queued_start(queued))
                self.assertEqual(self.receipt(c, prior)["state"], "interrupted")

                resumed = self.restart(c)
                resumed.run.action, resumed.run.action_only = prior, standalone

                def decisions():
                    rerun, = [a for a in ledger.actions(resumed.lease) if a["run"] == resumed.run.id]
                    self.assertEqual(rerun["state"], "succeeded")
                    self.assertEqual(self.receipt(resumed, dependent), queued)
                    self.assertNotIn(dependent, self.producers)
                    yield self.accept(resumed, {"op": "worker.continue", "args": {
                        "worker_id": prior, "message": "Recover the interrupted prerequisite"}})
                    self.assertEqual(self.receipt(resumed, dependent)["state"], "running")
                    yield self.accept(resumed, {"op": "run.wait", "args": {}})
                    yield self.accept(resumed, {"op": "run.finish", "args": {"reason": "noop"}})

                with (patch.object(resumed, "prepare", return_value=None),
                      patch.object(delegation, "Worker", side_effect=self.complete_at_launch),
                      patch.object(resumed, "decide", side_effect=decisions()) as decide,
                      patch.object(resumed.job.b, "restage", return_value=SimpleNamespace(empty=True)),
                      patch.object(flows, "finish", return_value=("nothing", "Fixture noop", "")) as finish):
                    self.assertEqual(resumed.drive()[0], "step" if standalone else "nothing")
                rerun, *_ = [a for a in ledger.actions(resumed.lease) if a["run"] == resumed.run.id]
                self.assertEqual((rerun["request"], rerun["state"]),
                                 (self.receipt(resumed, prior)["request"], "succeeded"))
                self.producers[rerun["id"]].allow.assert_called_once()
                if standalone:
                    self.assertEqual(self.receipt(resumed, dependent), queued)
                    self.assertEqual(set(self.producers), {prior, rerun["id"]})
                    decide.assert_not_called()
                    finish.assert_not_called()
                else:
                    self.assertEqual(self.receipt(resumed, dependent)["state"], "succeeded")
                    self.producers[dependent].allow.assert_called_once()
                    self.assertEqual(decide.call_count, 3)
                    finish.assert_called_once()

    def test_serial_saved_gates_run_without_settling_the_queue(self):
        c = self.controller()
        ops = [("checks.run", "checks"), ("review.run", "review"), ("verification.run", "verify")]
        evidence = {"checks": {"full": True}, "review": {"review": {"verdict": "pass"}},
                    "verify": {"verified": {"verdict": "pass"}}}

        def gate(role, **kwargs):
            return factory.Outcome("pass", data=evidence[role])

        with patch.object(c, "atomic", side_effect=gate):
            originals = self.send(c, *[{"op": op, "args": self.route(c, role) if role != "checks" else {}}
                                       for op, role in ops])
        prior, = self.send(c, self.start(c))
        dependent, = self.send(c, self.start(c, depends_on=[prior]))
        c.close()
        queued = self.receipt(c, dependent)
        self.assertTrue(ledger.queued_start(queued))
        for selected, (op, role) in zip(originals, ops, strict=True):
            for standalone in (True, False):
                with self.subTest(op=op, standalone=standalone):
                    c = self.restart(c)
                    if op == "review.run":
                        c.executing = originals[0]
                        c.stamp("checks", {"full": True})
                    elif op == "verification.run":
                        c.fingerprint = lambda tag=f"rerun-{standalone}": tag
                    c.run.action, c.run.action_only = selected, standalone
                    request = self.receipt(c, selected)["request"]

                    def decisions():
                        yield self.accept(c, {"op": op, "args": request})
                        raise runlimits.Parked("Fixture paused after returning to the controller")

                    with (patch.object(c, "prepare", return_value=None),
                          patch.object(c, "decide", side_effect=decisions()) as decide,
                          patch.object(c, "atomic", side_effect=gate) as atomic):
                        if standalone:
                            self.assertEqual(c.drive()[0], "step")
                            decide.assert_not_called()
                        else:
                            with self.assertRaisesRegex(runlimits.Parked, "Fixture paused"):
                                c.drive()
                    rerun, *later = [a for a in ledger.actions(c.lease) if a["run"] == c.run.id]
                    self.assertEqual((rerun["op"], rerun["state"], rerun["request"]), (op, "succeeded", request), rerun)
                    if not standalone:
                        blocked, = later
                        self.assertEqual(blocked["state"], "failed")
                        self.assertEqual(blocked["result"]["error"], f"outstanding worker work: {dependent} (queued)")
                    atomic.assert_called_once()
                    self.assertEqual(atomic.call_args.args, (role,))
                    self.assertTrue(c.current(role))
                    self.assertEqual(c.data["evidence"][role]["producer"], rerun["id"])
                    self.assertEqual(self.receipt(c, dependent), queued)
                    self.assertEqual(set(self.producers), {prior})

    def test_plain_resume_dispatches_accepted_owner_gates_without_exempting_ordinary_gates(self):
        gates = {"checks.run": ("checks", {"full": True}),
                 "review.run": ("review", {"review": {"verdict": "pass"}}),
                 "verification.run": ("verify", {"verified": {"verdict": "pass"}})}
        for op, (role, evidence) in gates.items():
            for standalone in (True, False):
                with self.subTest(op=op, standalone=standalone):
                    self.producers.clear()
                    c = self.controller()
                    passed = factory.Outcome("pass", data=evidence)
                    call = {"op": op, "args": self.route(c, role) if role != "checks" else {}}
                    if op == "review.run":
                        with patch.object(c, "atomic", return_value=factory.Outcome("pass", data={"full": True})):
                            self.send(c, {"op": "checks.run", "args": {}})
                    with patch.object(c, "atomic", return_value=passed):
                        selected, = self.send(c, call)
                    prior, = self.send(c, self.start(c))
                    dependent, = self.send(c, self.start(c, depends_on=[prior]))
                    c.close()
                    queued = self.receipt(c, dependent)

                    owner = self.restart(c)
                    owner.run.action, owner.run.action_only = selected, standalone
                    if standalone:
                        record.note(owner.run.id, only_step=selected)
                    with (patch.object(owner, "prepare", return_value=None), patch.object(owner, "close"),
                          patch.object(owner, "dispatch_rerun", side_effect=pipeline.Terminated),
                          patch.object(owner, "atomic") as atomic, self.assertRaises(pipeline.Terminated)):
                        owner.drive()
                    atomic.assert_not_called()
                    accepted, = [a for a in ledger.actions(owner.lease) if a["run"] == owner.run.id]
                    self.assertEqual(accepted["state"], "accepted")

                    record.move(owner.run.id, "interrupted", ("running",))
                    source = c.run.id if standalone else owner.run.id
                    with (patch.object(config, "load", return_value=self.cfg), patch.object(tasks, "launch") as launch):
                        tasks.resume(source)
                    launch.assert_called_once()
                    self.assertEqual(launch.call_args.args[3][-2:], ["--continue", source])
                    resumed = self.controller(source)
                    if op == "verification.run":
                        resumed.fingerprint = lambda: "rerun-tree"

                    def decisions():
                        self.assertEqual(self.receipt(resumed, accepted["id"])["state"], "succeeded")
                        yield self.accept(resumed, call, rationale=f"Owner requested rerun of {selected}")
                        raise runlimits.Parked("Fixture paused")

                    with (patch.object(resumed, "prepare", side_effect=resumed.recover),
                          patch.object(resumed, "atomic", return_value=passed) as atomic,
                          patch.object(resumed, "decide", side_effect=decisions()),
                          self.assertRaisesRegex(runlimits.Parked, "Fixture paused")):
                        resumed.drive()
                    atomic.assert_called_once()
                    self.assertEqual(atomic.call_args.args, (role,))
                    self.assertEqual(resumed.data["evidence"][role]["producer"], accepted["id"])
                    blocked, = [a for a in ledger.actions(resumed.lease) if a["run"] == resumed.run.id]
                    self.assertEqual((blocked["state"], blocked["result"]["error"]),
                                     ("failed", f"outstanding worker work: {dependent} (queued)"))
                    self.assertEqual(self.receipt(resumed, dependent), queued)
                    self.assertEqual(set(self.producers), {prior})

    def test_serial_standalone_gate_refuses_a_still_live_worker_at_recovery(self):
        c = self.controller()
        with patch.object(c, "atomic", return_value=factory.Outcome("pass")):
            checks, = self.send(c, {"op": "checks.run", "args": {}})
        prior, = self.send(c, self.start(c))
        dependent, = self.send(c, self.start(c, depends_on=[prior]))
        queued = self.receipt(c, dependent)
        record.move(c.run.id, "interrupted", ("running",))
        resumed = self.controller(c.run.id)
        resumed.run.action, resumed.run.action_only = checks, True
        with (patch.object(resumed, "prepare", side_effect=resumed.recover),
              patch.object(resumed, "atomic") as atomic, patch.object(delegation, "live", return_value=True)):
            with self.assertRaisesRegex(factory.Refused, rf"worker {prior} is still live"):
                resumed.drive()
            atomic.assert_not_called()
        self.assertEqual(self.receipt(resumed, dependent), queued)
        self.assertEqual(set(self.producers), {prior})

    def test_serial_dependent_setup_failure_can_be_rerun_alone(self):
        self.commit_base()
        c = self.controller()
        reader, = self.send(c, self.start(c))
        writer, = self.send(c, self.start(c, **self.route(c, "build"), prompt_id="build", tool_bundle="write",
                                           depends_on=[reader]))
        c.resume_queued()
        self.assertTrue(ledger.queued_start(self.receipt(c, writer)))
        self.finish_worker(c, reader)
        with patch.object(factory, "fingerprint", return_value=""):
            c.resume_queued()
        c.resume_queued()
        self.assertNotIn(writer, self.producers)
        failed = self.receipt(c, writer)
        self.assertEqual(failed["state"], "failed")
        self.assertIn("workspace could not be recorded", failed["result"]["error"])
        self.assertEqual(failed["result"]["effective"], failed["request"])
        self.assertFalse(ledger.unlaunched_start(failed))
        self.assertEqual(tasks.adaptive_action({"id": c.run.id}, writer), failed)

        dependents = self.send(c, self.start(c, depends_on=[reader]), self.start(c, depends_on=[writer]))
        queued = [self.receipt(c, dependent) for dependent in dependents]
        self.assertTrue(all(ledger.queued_start(a) for a in queued))
        resumed = self.restart(c)
        resumed.run.action, resumed.run.action_only = writer, True

        with (patch.object(resumed, "prepare", return_value=None),
              patch.object(delegation, "Worker", side_effect=self.complete_at_launch)):
            self.assertEqual(resumed.drive()[0], "step")
        rerun, = [a for a in ledger.actions(resumed.lease) if a["run"] == resumed.run.id]
        self.assertEqual(self.receipt(resumed, writer), failed)
        self.assertEqual((rerun["state"], rerun["request"]), ("succeeded", failed["request"]))
        self.producers[rerun["id"]].allow.assert_called_once()
        self.assertEqual(set(self.producers), {reader, rerun["id"]})
        self.assertEqual([self.receipt(resumed, dependent) for dependent in dependents], queued)

    def test_private_saved_worker_rerun_settles_terminal_queue_before_acceptance(self):
        self.commit_base()
        for continued in (False, True):
            for standalone in (False, True):
                with self.subTest(continued=continued, standalone=standalone):
                    self.producers.clear()
                    c = self.controller(builders=1)
                    original, = self.send(c, self.start(c))
                    if continued:
                        self.report_worker_result(original, outcome="handoff")
                        c.collect()
                        prior, = self.send(c, {"op": "worker.continue", "args": {
                            "worker_id": original, "message": "Finish the saved work"}})
                    else:
                        prior = original
                    self.finish_worker(c, prior)
                    prerequisite, = self.send(c, self.start(c))
                    dependent, = self.send(c, self.start(c, depends_on=[prerequisite]))
                    tail, = self.send(c, self.start(c, depends_on=[dependent]))
                    self.send(c, {"op": "worker.cancel", "args": {
                        "worker_id": prerequisite, "reason": "Fixture stop"}})
                    c.data["prepared"] = {"checkouts": [co.to_row() for co in c.job.checkouts]}
                    ledger.save(c.lease, c.data)
                    record.move(c.run.id, "interrupted", ("running",))
                    queued = [self.receipt(c, wid) for wid in (dependent, tail)]
                    self.assertTrue(all(ledger.queued_start(a) for a in queued))

                    with (patch.object(config, "load", return_value=self.cfg), patch.object(tasks, "launch") as launch):
                        if standalone:
                            tasks.run_step(c.run.id, prior, {})
                        else:
                            tasks.resume(c.run.id, prior)
                    launch.assert_called_once()
                    command = launch.call_args.args[3]
                    selected = ["--continue", c.run.id, "--action", prior]
                    if standalone:
                        selected.append("--only-action")
                    self.assertEqual(command[command.index("--continue"):], selected)
                    self.assertEqual([self.receipt(c, wid) for wid in (dependent, tail)], queued)

                    resumed = self.controller(c.run.id, builders=1)
                    resumed.run.action = command[command.index("--action") + 1]
                    resumed.run.action_only = "--only-action" in command
                    if resumed.run.action_only:
                        record.note(resumed.run.id, only_step=resumed.run.action)
                    launched = set(self.producers)

                    def complete(run, action, request):
                        self.assertEqual([self.receipt(resumed, wid)["state"] for wid in (dependent, tail)],
                                         ["cancelled", "cancelled"])
                        worker = self.spawn(run, action, request)
                        self.report_worker_result(action)
                        return worker

                    with (patch.object(resumed, "prepare", side_effect=resumed.recover),
                          patch.object(delegation, "Worker", side_effect=complete),
                          patch.object(resumed, "decide", side_effect=lambda: self.accept(
                              resumed, {"op": "run.finish", "args": {"reason": "noop"}})) as decide,
                          patch.object(resumed.job.b, "restage", return_value=SimpleNamespace(empty=True)),
                          patch.object(flows, "finish", return_value=("nothing", "Fixture noop", ""))):
                        self.assertEqual(resumed.drive()[0], "step" if standalone else "nothing")
                    self.assertEqual(decide.call_count, 0 if standalone else 1)
                    rerun, = set(self.producers) - launched
                    result = self.receipt(resumed, rerun)
                    self.assertEqual((result["op"], result["state"], result["request"]),
                                     ("worker.start", "succeeded", self.receipt(resumed, prior)["result"]["effective"]))
                    self.producers[rerun].allow.assert_called_once()
                    for wid, dep in ((dependent, prerequisite), (tail, dependent)):
                        result = self.receipt(resumed, wid)
                        self.assertEqual(result["state"], "cancelled")
                        self.assertEqual(result["result"]["depends_on"], [dep])
                        self.assertEqual(result["result"]["reason"], f"prerequisite {dep} was cancelled")
                        self.assertEqual(exchange.workers(resumed.lease)[wid]["phase"], "cancelled")
                        self.assertNotIn(wid, self.producers)

    def test_private_task_endpoints_refuse_saved_actions_with_live_or_admissible_work(self):
        self.commit_base()
        for live in (True, False):
            with self.subTest(live=live):
                self.producers.clear()
                c = self.controller(builders=1)
                prior, = self.send(c, self.start(c))
                self.finish_worker(c, prior)
                prerequisite, = self.send(c, self.start(c))
                dependent, = self.send(c, self.start(c, depends_on=[prerequisite]))
                if not live:
                    self.finish_worker(c, prerequisite)
                record.move(c.run.id, "interrupted", ("running",))
                before = ledger.actions(c.lease)
                with (patch.object(config, "load", return_value=self.cfg), patch.object(tasks, "launch") as launch):
                    with self.assertRaisesRegex(control.Refused, "settle outstanding workers"):
                        tasks.resume(c.run.id, prior)
                    with self.assertRaisesRegex(control.Refused, "settle outstanding workers"):
                        tasks.run_step(c.run.id, prior, {})
                launch.assert_not_called()
                self.assertEqual(ledger.actions(c.lease), before)
                self.assertNotIn(dependent, self.producers)


class DependentWorkerIntegrationTests(OrchestrationRecoveryFixture):
    def test_restart_attempts_follow_the_writer_without_bypassing_retry_or_integration(self):
        self.commit_base()
        c = self.controller(builders=1)
        writer, = self.send(c, self.start(c, **self.route(c, "build"), prompt_id="build", tool_bundle="write",
                                           write_paths=["fixture.txt"], acceptance_check="Read fixture.txt"))
        continuation = {"op": "worker.continue", "args": {"worker_id": writer, "message": "Try again"}}

        def fail(attempt):
            self.producers[attempt].receipts.append({"event": "error", "error": "Fixture failure"})
            c.collect()

        fail(writer)
        c.resume_ready()
        failed = exchange.workers(c.lease)[writer]["attempt"]
        fail(failed)
        self.assertEqual(exchange.workers(c.lease)[writer]["phase"], "parked")
        legacy, = self.send(c, continuation)
        c.close()
        linked, = self.send(c, continuation)
        with patch.object(delegation, "live", return_value=False):
            c = self.restart(c)
        ready = exchange.workers(c.lease)[writer]
        self.assertEqual((ready["phase"], ready["attempt"], ready["data"]["retry_of"]), ("ready", linked, linked))
        self.assertEqual(self.receipt(c, linked)["state"], "interrupted")

        def fail_setup(run, attempt, request):
            owned = exchange.workers(c.lease)[writer]
            self.assertEqual((owned["phase"], owned["attempt"], owned["generation"]),
                             ("assigned", attempt, c.lease.generation))
            self.assertEqual(self.receipt(c, attempt)["request"], self.receipt(c, linked)["request"])
            raise ValueError("Restart continuation setup failed")

        with patch.object(delegation, "Worker", side_effect=fail_setup) as launch:
            c.resume_ready()
        launch.assert_called_once()
        parked = exchange.workers(c.lease)[writer]
        self.assertEqual(parked["phase"], "parked")
        retry = parked["attempt"]
        receipt = self.receipt(c, retry)
        self.assertEqual((receipt["state"], receipt["result"]["retry_of"], receipt["result"]["worker_id"]),
                         ("failed", linked, writer))
        self.assertEqual(receipt["result"]["error"], "Restart continuation setup failed")
        self.assertNotIn("pid", receipt["result"])
        self.assertNotIn(retry, self.producers)

        rejected, = self.accept(c, continuation)
        with patch.dict(c.catalog.settings, {}, clear=True):
            self.assertIsInstance(c.dispatch(rejected), ValueError)
        refused = self.receipt(c, rejected["id"])
        self.assertEqual(refused["state"], "failed")
        self.assertIn("not approved", refused["result"]["error"])
        self.assertNotIn("worker_id", refused["result"])

        successful, = self.send(c, continuation)
        (self.producers[successful].cwd / "fixture.txt").write_text("integrated\n")
        with patch.object(c, "settle_candidate"):
            self.finish_worker(c, successful)
        dependents = self.send(c, self.start(c, depends_on=[retry]), self.start(c, depends_on=[linked]),
                               self.start(c, depends_on=[failed, legacy]))
        no_launch, = self.send(c, self.start(c, depends_on=[rejected["id"]]))
        c.resume_queued()
        self.assertEqual((self.repo.path / "fixture.txt").read_text(), "base\n")
        for dependent in dependents:
            self.assertIn("(candidate)", self.receipt(c, dependent)["result"]["reason"])
            self.assertNotIn(dependent, self.producers)
        cancelled = self.receipt(c, no_launch)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertTrue(ledger.unlaunched_start(cancelled))
        self.assertNotIn(no_launch, self.producers)

        c.settle_candidate(exchange.workers(c.lease)[writer])
        self.assertEqual(exchange.workers(c.lease)[writer]["phase"], "integrated")
        self.assertEqual((self.repo.path / "fixture.txt").read_text(), "integrated\n")
        c.resume_queued()
        for dependent in dependents:
            self.assertEqual((self.producers[dependent].cwd / "fixture.txt").read_text(), "integrated\n")
            self.finish_worker(c, dependent)
            self.assertEqual(self.receipt(c, dependent)["state"], "succeeded")
            self.producers[dependent].allow.assert_called_once()
        c.resume_queued()
        self.assertNotIn(no_launch, self.producers)

    def test_setup_failed_private_attempts_follow_their_integrated_writer(self):
        self.commit_base()
        c = self.controller(builders=1)
        build = self.start(c, **self.route(c, "build"), prompt_id="build", tool_bundle="write",
                           write_paths=["fixture.txt"], acceptance_check="Read fixture.txt")
        rejected, = self.accept(c, build)
        with patch.object(orchestration.builder_workspaces, "create", side_effect=ValueError("Assignment rejected")):
            self.assertIsInstance(c.dispatch(rejected), ValueError)
        self.assertNotIn(rejected["id"], exchange.workers(c.lease))

        start, = self.accept(c, build)
        writer = start["id"]
        with patch.object(delegation, "Worker", side_effect=ValueError("Start setup failed")):
            self.assertIsInstance(c.dispatch(start), ValueError)
        self.assertEqual(exchange.workers(c.lease)[writer]["phase"], "parked")
        continuation = {"op": "worker.continue", "args": {"worker_id": writer, "message": "Try again"}}
        first, = self.send(c, continuation)
        self.producers[first].receipts.append({"event": "error", "error": "Retryable worker failure"})
        c.collect()
        with patch.object(delegation, "Worker", side_effect=ValueError("Continuation setup failed")):
            c.resume_ready()
        worker = exchange.workers(c.lease)[writer]
        automatic = worker["attempt"]
        self.assertNotEqual(automatic, first)
        self.assertEqual(worker["phase"], "parked")
        for attempt, error in ((writer, "Start setup failed"), (automatic, "Continuation setup failed")):
            receipt = self.receipt(c, attempt)
            self.assertEqual((receipt["state"], receipt["result"]["worker_id"]), ("failed", writer))
            self.assertEqual(receipt["result"]["error"], error)
            self.assertNotIn("pid", receipt["result"])
            self.assertNotIn("retry_of", receipt["result"])
            self.assertNotIn(attempt, self.producers)

        c.close()
        c = self.restart(c)
        successful, = self.send(c, continuation)
        (self.producers[successful].cwd / "fixture.txt").write_text("integrated\n")
        with patch.object(c, "settle_candidate"):
            self.finish_worker(c, successful)
        dependents = self.send(c, self.start(c, depends_on=[writer]), self.start(c, depends_on=[automatic]))
        unusable, = self.send(c, self.start(c, depends_on=[rejected["id"]]))
        c.resume_queued()
        self.assertEqual(self.receipt(c, unusable)["state"], "cancelled")
        self.assertNotIn(unusable, self.producers)
        for dependent in dependents:
            receipt = self.receipt(c, dependent)
            self.assertTrue(ledger.queued_start(receipt))
            self.assertIn("(candidate)", receipt["result"]["reason"])
            self.assertNotIn(dependent, self.producers)

        c.settle_candidate(exchange.workers(c.lease)[writer])
        self.assertEqual(exchange.workers(c.lease)[writer]["phase"], "integrated")
        c.resume_queued()
        for dependent in dependents:
            self.assertEqual((self.producers[dependent].cwd / "fixture.txt").read_text(), "integrated\n")
            self.finish_worker(c, dependent)
            self.assertEqual(self.receipt(c, dependent)["state"], "succeeded")
            self.producers[dependent].allow.assert_called_once()
        c.resume_queued()


class DependentWorkerSchedulingTests(OrchestrationRecoveryFixture):
    def test_batched_wait_keeps_explicit_launch_ahead_of_the_queue(self):
        self.commit_base()
        for builders in (1, None):
            for continuing in (False, True):
                with self.subTest(builders=builders, continuing=continuing):
                    self.producers.clear()
                    c = self.controller(builders=builders)
                    self.assertEqual(c.exchanged, builders is not None)
                    blocker = ("waiting for builder capacity" if c.exchanged
                               else "waiting for the shared workspace mutation lease")

                    def writer(path, **args):
                        claim = {"write_paths": [path], "acceptance_check": f"Inspect {path}"} if c.exchanged else {}
                        return self.start(c, **self.route(c, "build"), prompt_id="build", tool_bundle="write",
                                          **claim, **args)

                    launch = writer("explicit.txt")
                    if continuing:
                        prior, = self.send(c, launch)
                        self.report_worker_result(prior, outcome="handoff" if c.exchanged else "done")
                        c.collect()
                        launch = {"op": "worker.continue", "args": {"worker_id": prior, "message": "Continue"}}
                    reader, = self.send(c, self.start(c))
                    dependent, = self.send(c, writer("fixture.txt", depends_on=[reader]))
                    self.assertEqual(set(c.workers), {reader})
                    self.assertTrue(ledger.queued_start(self.receipt(c, dependent)))

                    def decisions():
                        wait = {"op": "run.wait", "args": {}}
                        waited, explicit = self.accept(c, wait, launch)
                        self.report_worker_result(reader)
                        yield [waited, explicit]
                        self.assertEqual(self.receipt(c, reader)["state"], "succeeded")
                        receipt = self.receipt(c, explicit["id"])
                        self.assertEqual(receipt["state"], "running", receipt)
                        self.assertEqual(self.receipt(c, waited["id"])["result"]["active"], [])
                        self.assertEqual(set(c.workers), {explicit["id"]})
                        self.assertNotIn(dependent, self.producers)
                        blocked = self.receipt(c, dependent)
                        self.assertTrue(ledger.queued_start(blocked))
                        self.assertEqual(blocked["result"]["reason"], blocker)

                        self.report_worker_result(explicit["id"])
                        yield self.accept(c, wait)
                        self.assertEqual(self.receipt(c, explicit["id"])["state"], "succeeded")
                        self.producers[explicit["id"]].allow.assert_called_once()
                        self.assertEqual(set(c.workers), {dependent})
                        self.finish_worker(c, dependent)
                        yield self.accept(c, {"op": "run.finish", "args": {"reason": "noop"}})

                    with (patch.object(c, "prepare", return_value=None),
                          patch.object(c, "decide", side_effect=decisions()),
                          patch.object(c.job.b, "restage", return_value=SimpleNamespace(empty=True)),
                          patch.object(flows, "finish", return_value=("nothing", "Fixture noop", ""))):
                        self.assertEqual(c.drive()[0], "nothing")
                    self.assertEqual(self.receipt(c, dependent)["state"], "succeeded")
                    self.producers[dependent].allow.assert_called_once()

    def test_serial_reader_steer_satisfies_dependency_while_reader_is_active(self):
        c = self.controller()
        reader, = self.send(c, self.start(c))
        steer, = self.send(c, {"op": "worker.steer", "args": {"worker_id": reader, "message": "Read the log"}})
        dependent, = self.send(c, self.start(c, depends_on=[steer]))
        c.resume_queued()
        self.assertEqual(set(c.workers), {reader, dependent})
        self.finish_worker(c, reader)
        self.finish_worker(c, dependent)

    def test_serial_writer_steer_remains_usable_after_cancellation(self):
        self.commit_base()
        c = self.controller()
        writer, = self.send(c, self.start(c, **self.route(c, "build"), prompt_id="build", tool_bundle="write"))
        steer, = self.send(c, {"op": "worker.steer", "args": {"worker_id": writer, "message": "Check the log"}})
        waiting, = self.send(c, self.start(c, depends_on=[steer]))
        c.resume_queued()
        self.assertEqual(self.receipt(c, waiting)["result"]["reason"], "waiting for the shared workspace mutation lease")
        self.assertNotIn(waiting, self.producers)
        self.send(c, {"op": "worker.cancel", "args": {"worker_id": writer, "reason": "Fixture cancellation"}})
        self.assertEqual(self.receipt(c, writer)["state"], "cancelled")
        self.assertEqual(self.receipt(c, steer)["state"], "succeeded")
        dependent, = self.send(c, self.start(c, depends_on=[steer]))
        c.resume_queued()
        self.assertEqual(set(c.workers), {waiting, dependent})
        self.finish_worker(c, waiting)
        self.finish_worker(c, dependent)

    def test_serial_queue_stays_visible_outside_action_page_and_after_restart(self):
        c = self.controller()
        reader, = self.send(c, self.start(c))
        dependent, = self.send(c, self.start(c, depends_on=[reader]))
        for _ in range(3):
            self.send(c, *[{"op": "catalog.list", "args": {"kind": "tools"}}] * 7)

        def assert_visible(controller, phase):
            queued = {"id": dependent, "phase": "queued", "depends_on": [reader],
                      "reason": f"waiting for prerequisite {reader} ({phase})"}

            def check(page, limit):
                self.assertEqual(page["queued_starts"], [queued])
                self.assertLessEqual(len(page["actions"]), limit)
                self.assertNotIn(dependent, {a["id"] for a in page["actions"]})

            cursor = ledger.snapshot(controller.lease)["version"]
            for args in ({}, {"limit": 1, "after": cursor}):
                observed, = self.send(controller, {"op": "run.observe", "args": args})
                check(self.receipt(controller, observed)["result"], args.get("limit", 20))

            def infer(turn, request, retried):
                observation = json.loads(request["prompt"].split("## Current observation\n\n", 1)[1])
                check(observation, 20)
                return {"ok": True, "structured": {"version": PROTOCOL, "decision_id": turn,
                        "state_version": observation["state_version"], "rationale": "Fixture replay",
                        "calls": [{"op": "catalog.list", "args": {"kind": "tools"}}]}}

            with patch.object(controller, "controller_turn", side_effect=infer) as infer_turn:
                controller.dispatch_batch(controller.decide(), fresh=True)
                infer_turn.assert_called_once()

        assert_visible(c, "running")
        c.close()
        c = self.restart(c)
        assert_visible(c, "interrupted; needs continuation")
        checks, = self.accept(c, {"op": "checks.run", "args": {}})
        with patch.object(c, "atomic") as atomic:
            c.dispatch_batch([checks], fresh=True)
            atomic.assert_not_called()
        self.assertEqual(self.receipt(c, checks["id"])["result"]["error"],
                         f"outstanding worker work: {dependent} (queued)")
        self.assertTrue(ledger.queued_start(self.receipt(c, dependent)))
        self.assertNotIn(dependent, self.producers)

    def test_cancelled_prerequisite_chain_settles_before_same_decision_gates(self):
        self.commit_base()
        for builders in (None, 1):
            for op, role in (("checks.run", "checks"), ("verification.run", "verify"), ("review.run", "review"),
                             ("publication.request", None), ("run.finish", None)):
                with self.subTest(builders=builders, op=op):
                    self.producers.clear()
                    c = self.controller(builders=builders)
                    reader, = self.send(c, self.start(c))
                    dependent, = self.send(c, self.start(c, depends_on=[reader]))
                    downstream, = self.send(c, self.start(c, depends_on=[dependent]))
                    args = self.route(c, role) if role in {"verify", "review"} else {}
                    if op == "run.finish":
                        args["reason"] = "noop"
                    if op == "review.run":
                        c.stamp("checks", {"full": True})
                    if op == "publication.request":
                        for gate in ("checks", "verify", "review"):
                            c.stamp(gate, {"full": False} if gate == "checks" else {"verdict": "pass"})
                            self.assertTrue(c.current(gate))
                    cancel, guarded = self.accept(c, {"op": "worker.cancel", "args": {
                        "worker_id": reader, "reason": "Fixture cancellation"}}, {"op": op, "args": args})
                    outcome_data = {"checks": {"full": True}, "verify": {"verified": {"verdict": "pass"}},
                                    "review": {"review": {"verdict": "pass"}}}.get(role, {})
                    with (patch.object(c, "atomic", return_value=factory.Outcome("pass", data=outcome_data)) as atomic,
                          patch.object(flows, "finish", return_value=("nothing", "Fixture noop", "")) as finish):
                        c.dispatch_batch([cancel, guarded], fresh=True)
                    self.assertEqual(self.receipt(c, cancel["id"])["result"]["cancelled"], reader)
                    for worker, prior in ((dependent, reader), (downstream, dependent)):
                        receipt = self.receipt(c, worker)
                        self.assertEqual(receipt["state"], "cancelled")
                        self.assertEqual(receipt["result"]["depends_on"], [prior])
                        self.assertRegex(receipt["result"]["reason"], rf"^prerequisite {prior} .*cancelled$")
                    self.assertEqual(set(self.producers), {reader})
                    result = self.receipt(c, guarded["id"])
                    if op == "publication.request":
                        self.assertEqual(result["state"], "failed")
                        self.assertEqual(result["result"]["error"], "publication requires the full owner check policy")
                        atomic.assert_not_called()
                    else:
                        self.assertEqual(result["state"], "succeeded", result["result"])
                        if role:
                            atomic.assert_called_once()
                            self.assertEqual(atomic.call_args.args[0], role)
                        else:
                            finish.assert_called_once_with(c.flow, "nothing", summary="No change needed.")


class OrchestrationReceiptContextTests(OrchestrationRecoveryFixture):
    def test_large_context_read_advertises_its_structured_receipt(self):
        for surface in ("run.observe", "decision prompt"):
            with self.subTest(surface=surface):
                c = self.controller()
                source = 'prefix\n' + '"quoted row"\n' * 600
                c.catalog.add_context("fixture-page", source)
                page, = self.send(c, {"op": "context.read", "args": {"ref": "fixture-page", "offset": 7, "limit": 5000}})
                original = self.receipt(c, page)["result"]
                self.assertEqual((original["ref"], original["next"]), ("fixture-page", 5007))
                encoded = ledger.encoded(original)

                def follow(observation):
                    advertised = next(item["result"] for item in observation["actions"] if item["id"] == page)
                    self.assertEqual(advertised, {"ref": page, "excerpt": encoded[:2000], "characters": len(encoded)})
                    return {"op": "context.read", "args": {"ref": advertised["ref"], "limit": 16000}}

                if surface == "run.observe":
                    observed, = self.send(c, {"op": "run.observe", "args": {}})
                    read, = self.send(c, follow(self.receipt(c, observed)["result"]))
                else:
                    def infer(turn, request, retried):
                        observation = json.loads(request["prompt"].split("## Current observation\n\n", 1)[1])
                        return {"ok": True, "structured": {"version": PROTOCOL, "decision_id": turn,
                                "state_version": observation["state_version"], "rationale": "Fixture replay",
                                "calls": [follow(observation)]}}

                    with patch.object(c, "controller_turn", side_effect=infer):
                        action, = c.decide()
                    self.assertIsNone(c.dispatch(action))
                    read = action["id"]
                recovered = self.receipt(c, read)["result"]
                self.assertEqual(recovered["characters"], len(encoded))
                self.assertEqual(json.loads(recovered["text"]), original)

    def test_recovered_prose_and_saved_page_reach_all_context_consumers(self):
        c = self.controller()
        page_end = '\n"Final acceptance detail"\n'
        raw = ('"quoted row"\n' * 800)[:8000 - len(page_end)] + page_end
        c.catalog.add_context("fixture-page", raw)
        page, = self.send(c, {"op": "context.read", "args": {"ref": "fixture-page", "limit": 8000}})
        result, = self.send(c, self.start(c))
        prose_end = '\n"Final worker finding"\n'
        prose = ('"worker result"\n' * 800)[:8000 - len(prose_end)] + prose_end
        self.finish_worker(c, result, prose)
        structured = ledger.encoded(self.receipt(c, result)["result"])
        live = next(item["result"] for item in c.observe()["actions"] if item["id"] == result)
        self.assertEqual(live, {"ref": result, "excerpt": structured[:2000], "characters": len(structured)})
        first = c.catalog.read(result, 0, 16000)
        self.assertEqual(first, {"ref": result, "text": structured[:16000],
                                 "characters": len(structured), "next": 16000})
        tail = c.catalog.read(result, first["next"], 16000)
        self.assertEqual(tail, {"ref": result, "text": structured[16000:],
                                "characters": len(structured), "next": None})
        error, = self.accept(c, self.start(c))
        ledger.transition(c.lease, error["id"], "failed", {"error": "Short worker error"})
        with patch.object(c, "atomic", return_value=factory.Outcome("fail", data={"report": "Short check report"})):
            checks, = self.send(c, {"op": "checks.run", "args": {}})
        for _ in range(3):
            self.send(c, *[{"op": "catalog.list", "args": {"kind": "tools"}}] * 7)

        c = self.restart(c)
        self.assertTrue({page, result}.isdisjoint(item["id"] for item in c.observe()["actions"]))
        for ref, expected in ((page, raw), (result, prose)):
            read, = self.send(c, {"op": "context.read", "args": {"ref": ref, "limit": 8000}})
            self.assertEqual(self.receipt(c, read)["result"]["text"], expected)
        refs = [page, result, error["id"], checks]

        def check_context(text):
            for expected in (raw, prose, "Short worker error", "Short check report"):
                self.assertIn(expected, text)

        start, = self.send(c, self.start(c, context_refs=refs))
        check_context(self.producers[start].request["prompt"])
        self.finish_worker(c, start)
        continuation, = self.send(c, {"op": "worker.continue", "args": {"worker_id": start, "message": "Continue"}})
        check_context(self.producers[continuation].request["prompt"])
        self.finish_worker(c, continuation)

        def gate(role, **kw):
            check_context(c.job.goal if role == "verify" else c.job.fix_reply)
            key = "verified" if role == "verify" else "review"
            return factory.Outcome("pass", data={key: {"verdict": "pass"}})

        c.stamp("checks", {"full": True})
        with patch.object(c, "atomic", side_effect=gate) as atomic:
            for op, role, field in (("verification.run", "verify", "goal_refs"), ("review.run", "review", "context_refs")):
                self.send(c, {"op": op, "args": {**self.route(c, role), field: refs}})
            self.assertEqual(atomic.call_count, 2)

        known = {a["id"]: a for a in ledger.actions(c.lease)}
        rerun, = self.accept(c, self.start(c, context_refs=refs))
        c.dispatch_rerun(rerun, known)
        check_context(self.producers[rerun["id"]].request["prompt"])
        self.finish_worker(c, rerun["id"])


class BudgetTests(Isolated):
    def test_monitor_reconsiders_a_park_when_poll_readmission_appears_or_is_spent(self):
        self.cfg.unblock_repos = (self.repo.name,)
        self.cfg.replay = config.ReplayConfig(all=True)
        run = self.made_run()
        run.budget.park("the repair chain used its 2 repair cycles")
        record.settle(run.id, "failed")
        row = run_store.run_get(run.id)

        def inspect():
            case = recovery_store.get(self.repo.name, self.ticket)
            unblock_watch.inspect(self.cfg, self.repo, self.ticket, row, [], case)
            return recovery_store.get(self.repo.name, self.ticket)

        with (patch.object(config, "load", return_value=self.cfg),
              patch.object(unblock, "diagnose", return_value={"action": "wait_for", "evidence": ["run"],
                                                               "reason": "Wait for changed inputs."}) as diagnose):
            first = inspect()
            self.assertNotIn("poll_readmission", diagnose.call_args.args[-2])
            with foundation.db() as connection:
                set_task_body(self.ticket, "Revised specification", connection=connection)
            admitted = inspect()
            observation = diagnose.call_args.args[-2]["poll_readmission"]
            self.assertEqual(observation["parked_run"], run.id)
            self.assertEqual(json.loads(admitted["snapshot"])["poll_readmission"], observation)
            self.assertEqual(inspect()["generation"], admitted["generation"])
            _, proof = runlimits.poll_admission(self.repo, self.ticket, self.local.get(self.ticket), run.budget.policy)
            self.assertFalse(proof.attempted(record=True))
            spent = inspect()
            self.assertNotIn("poll_readmission", diagnose.call_args.args[-2])
            self.assertEqual([c["generation"] for c in (first, admitted, spent)], [1, 2, 3])
            self.assertEqual(diagnose.call_count, 3)

    def test_measured_usage_rejects_corruption_and_zero_overrides_wall_time(self):
        run = self.made_run()
        record.note(run.id, started=util.iso(util.now() - timedelta(minutes=90)))
        record.settle(run.id, "interrupted")
        for field, read in (("active_seconds", runlimits.active_seconds),
                            ("verify_seconds", runlimits.verification_seconds),
                            ("verify_open_seconds", runlimits.verification_seconds),
                            ("repair_calls", lambda rid, row: runlimits.repair_count(rid))):
            for bad in (-1, "unknown"):
                with self.subTest(field=field, bad=bad), self.assertRaises(sqlite3.IntegrityError), \
                        foundation.tx() as conn:
                    conn.execute(f"UPDATE runs SET {field}=? WHERE id=?", (bad, run.id))
            for bad in ((None, float("nan")) if field == "repair_calls" else (None, float("nan"), float("inf"))):
                with foundation.tx() as conn:
                    conn.execute(f"UPDATE runs SET {field}=? WHERE id=?", (bad, run.id))
                with self.assertRaisesRegex(ValueError, run.id):
                    read(run.id, run_store.run_get(run.id))
            with foundation.tx() as conn:
                conn.execute(f"UPDATE runs SET {field}=0 WHERE id=?", (run.id,))
            self.assertEqual(read(run.id, run_store.run_get(run.id)), 0)

    def test_measured_recovery_and_nested_open_idle_waits(self):
        clock = [100.0]
        with patch.object(runlimits, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            run = self.made_run()
            clock[0] += 10
            for cause in ("limit", "slot"):
                with run.budget.idling(cause):
                    clock[0] += 20
                    with run.budget.idling("slot"):
                        clock[0] += 30
                        run.budget.checkpoint()
                        self.assertEqual(run.budget.run_seconds(), 10)
                    clock[0] += 10
                self.assertEqual(run.budget.run_seconds(), 10)
            self.assertEqual(run.budget.idled, 120)
            with run.budget.idling("ci"):
                clock[0] += 5
            self.assertEqual(run.budget.run_seconds(), 15)
            run.budget.persist()
            resumed = runlimits.Budget(run, run.budget.policy, "")
            self.assertEqual(resumed.run_seconds(), 15)
            clock[0] += 5
            self.assertEqual(resumed.run_seconds(), 20)

    def test_checkpoint_tracks_open_verification_without_charging_open_idle_time(self):
        clock = [100.0]
        with patch.object(runlimits, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            run = self.made_run()
            clock[0] += 10
            with patch.object(runlimits.signal, "setitimer"), run.budget.verifying(), run.budget.idling("limit"):
                clock[0] += 20
                run.budget.checkpoint()
                first = run_store.run_get(run.id)
                clock[0] += 30
                run.budget.checkpoint()
                second = run_store.run_get(run.id)
            self.assertEqual(first["active_seconds"], second["active_seconds"])
            self.assertEqual(second["verify_open_seconds"] - first["verify_open_seconds"], 30)
            saved = run_store.run_get(run.id)
            self.assertEqual(saved["verify_seconds"], 50)
            self.assertEqual(saved["verify_open_seconds"], 0)
            self.assertEqual(saved["active_seconds"], 10)

    def test_repair_budget_is_charged_before_launch_and_survives_continuation(self):
        first = self.made_run()
        first.budget.charge("review_fix1", "review_repair")
        first.budget.charge("verify_fix1", "build")
        record.settle(first.id, "failed")
        second = self.made_run(first.id)
        invoked = []
        with patch.object(pipeline.runner, "run", side_effect=lambda *a, **k: invoked.append(True)), \
                self.assertRaises(runlimits.Parked):
            second.agent("repair", "repair again", self.repo.path, role="review_repair")
        self.assertEqual(invoked, [])
        self.assertEqual(runlimits.repair_count(second.id), 0)

    def test_repair_usage_is_durable_even_when_audit_event_cannot_be_written(self):
        first = self.made_run()
        with patch.object(run_lines, "write", return_value={}):
            first.budget.charge("review_fix1", "review_repair")
            first.budget.charge("verify_fix1", "build")
        record.settle(first.id, "failed")
        second = self.made_run(first.id)
        with self.assertRaisesRegex(runlimits.Parked, "repair cycles"):
            second.budget.charge("repair", "review_repair")

    def test_cumulative_time_survives_continuation(self):
        first = self.made_run()
        with foundation.tx() as conn:
            conn.execute("UPDATE runs SET active_seconds=? WHERE id=?", (61 * 60, first.id))
        record.settle(first.id, "interrupted")
        second = self.made_run(first.id)
        with self.assertRaisesRegex(runlimits.Parked, "cumulative 60"):
            second.budget.charge("plan", "plan")

    def test_interrupted_verification_phase_is_counted_on_restart(self):
        first = self.made_run()
        with foundation.tx() as conn:
            conn.execute("UPDATE runs SET verify_open_seconds=? WHERE id=?", (21 * 60, first.id))
        record.settle(first.id, "interrupted")
        second = self.made_run(first.id)
        with self.assertRaisesRegex(runlimits.Parked, "verification"), second.budget.verifying():
            self.fail("unfinished verification phases must count")

    def test_unchanged_parked_input_refuses_retry_changed_spec_requires_explicit_retry(self):
        run = self.made_run()
        with patch.object(runlimits, "heads", return_value={"": "old"}):
            run.budget.park("repair limit")
            record.settle(run.id, "failed", branch="fixture")
            admission = runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, True)
            self.assertIn("parked", admission.reason)
            self.assertFalse(admission.reset_time)
            self.assertFalse(admission.reset_repairs)
            with foundation.db() as connection:
                set_task_body(self.ticket, "A narrower specification", connection=connection)
            self.assertTrue(runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, False).reason)
            self.assertEqual(runlimits.admission(self.repo, self.ticket, self.local, run.budget.policy, True),
                             runlimits.Admission(reset_time=True, reset_repairs=True))

    def test_corrupt_budget_is_not_silently_reset(self):
        run = self.made_run()
        run.budget.charge("review_fix1", "review_repair")
        record.settle(run.id, "failed")
        for invalid in ("broken json", json.dumps({"version": 1, "parents": [], "left_out": "x"})):
            with self.subTest(invalid=invalid):
                (run.dir / "budget.json").write_text(invalid)
                with self.assertRaises(ValueError):
                    self.made_run(run.id)
                self.assertEqual(runlimits.repair_count(run.id), 1)

    def test_individual_step_reruns_cannot_reset_the_ticket_budget(self):
        first = self.made_run()
        first.budget.charge("review_fix1", "review_repair")
        record.settle(first.id, "failed")
        second = self.made_run(first.id)
        second.budget.charge("review_fix2", "review_repair")
        record.note(second.id, only_step="implement")
        record.settle(second.id, "step")
        history = run_store.ticket_runs(self.repo.name, self.ticket)
        script = pipeline.Script("feature", lambda flow: None, continues=first.id)
        parent = pipeline.parent_run(script, run_store.run_get(first.id), history)
        self.assertEqual(parent, second.id)
        next_run = self.made_run(parent)
        self.assertEqual(set(next_run.budget.parents), {first.id, second.id})
        with self.assertRaisesRegex(runlimits.Parked, "repair cycles"):
            next_run.budget.charge("repair", "review_repair")

    def test_forced_run_leaves_out_repairs_spent_under_another_specification(self):
        for forced, edited, recorded in ((True, True, True), (True, False, True),
                                         (False, True, True), (True, True, False)):
            with self.subTest(forced=forced, edited=edited, recorded=recorded):
                with foundation.db() as connection:
                    set_task_body(self.ticket, "Original", connection=connection)
                old = self.made_run()
                old.budget.charge("review_fix1", "review_repair")
                old.budget.charge("verify_fix1", "build")
                if not recorded:
                    old.budget.specification = None
                    old.budget.persist()
                record.settle(old.id, "pr")
                if edited:
                    with foundation.db() as connection:
                        set_task_body(self.ticket, "Edited", connection=connection)
                next_run = self.made_run(old.id, forced=forced)
                if forced and edited and recorded:
                    self.assertEqual((next_run.budget.parents, next_run.budget.left_out), ([], [old.id]))
                    next_run.budget.charge("review_fix", "review_repair")
                    self.assertEqual(runlimits.repair_count(next_run.id), 1)
                    record.settle(next_run.id, "failed")
                    retry = self.made_run(next_run.id, forced=True)
                    self.assertEqual(retry.budget.parents, [next_run.id])
                    self.assertNotIn(old.id, runlimits.family(retry.id))
                else:
                    self.assertEqual((next_run.budget.parents, next_run.budget.left_out), ([old.id], []))
                    with self.assertRaisesRegex(runlimits.Parked, "repair cycles"):
                        next_run.budget.charge("repair", "review_repair")

    def test_forced_run_leaves_out_run_and_verification_minutes_of_another_specification(self):
        for field, seconds in (("active_seconds", 61 * 60), ("verify_seconds", 21 * 60)):
            with self.subTest(field=field):
                with foundation.db() as connection:
                    set_task_body(self.ticket, "Original", connection=connection)
                old = self.made_run()
                with foundation.tx() as conn:
                    conn.execute(f"UPDATE runs SET {field}=? WHERE id=?", (seconds, old.id))
                record.settle(old.id, "pr")
                with foundation.db() as connection:
                    set_task_body(self.ticket, "Edited", connection=connection)
                fresh = self.made_run(old.id, forced=True).budget
                self.assertEqual((fresh.parents, fresh.left_out), ([], [old.id]))
                self.assertEqual((fresh.prior_seconds, fresh.verify_seconds), (0, 0))
                fresh.charge("plan", "plan")
                kept = self.made_run(old.id).budget
                with self.assertRaises(runlimits.Parked):
                    if field == "active_seconds":
                        kept.charge("plan", "plan")
                    else:
                        with kept.verifying():
                            self.fail("verification budget must not reset without an explicit retry")

    def test_environment_release_excludes_only_repairs_once_per_gap_issue_and_ticket(self):
        project = "owner/arete"
        release = replace(self.repo, name=self.cfg.arete_repo, github=project)
        self.cfg.repos.append(release)
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[limits]\nmax_repair_cycles = 3\n')
        head = "a" * 40
        url = f"https://github.com/{project}/pull/1"
        gap = environment_gaps.Gap(environment_gaps.Signature("exit 127", "fixture"), "", "", "")
        hold = environment_gaps.Hold([gap], {url: head})
        observed = []

        def drive(flow):
            budget = flow.ctx.run.budget
            observed.append((budget.prior_repairs, budget.prior_seconds, budget.verify_seconds,
                             next(iter(budget.charged.values()))["repair_calls"]))
            return "pr", "fixture verified", url

        for number, sha in zip((42, 42, 43), (digit * 40 for digit in "bcd"), strict=True):
            parent = self.made_run()
            record.note(parent.id, active_seconds=18 * 60, verify_seconds=5 * 60, repair_calls=3)
            record.settle(parent.id, "pr", pr_url=url)
            run_store.save_environment_hold(parent.id, [(project, 1, url, head)])
            run_store.set_environment_waits(parent.id, {gap.signature.key: (project, number)})
            run_lines.write(parent.id, "execution", path="feature")
            script = pipeline.Script("feature", drive, continues=parent.id, again="verify", environment_release=sha)
            with patch.object(config, "load", return_value=self.cfg), \
                    patch.object(pipeline, "run_refusal", return_value=(True, "")), \
                    patch.object(flows, "check_environment_workspace"), \
                    patch.object(environment_gaps, "live_release", return_value=sha), \
                    patch.object(environment_gaps, "hold_of", return_value=hold), \
                    patch.object(environment_gaps, "discussions", return_value={url: object()}), \
                    patch.object(environment_gaps, "release_contains_fixes", return_value=True) as contains:
                self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, script=script), 0)
            contains.assert_called_once_with(release, [number], sha)
            resumed = run_store.newest_whole_run(self.repo.name, self.ticket)["id"]
            log = (paths.RUNS_DIR / resumed / "run.log").read_text()
            if len(observed) == 2:
                self.assertNotIn("budget: left out", log)
            else:
                self.assertIn(f"because the environment gap #{number} was fixed since they ran", log)
            retry = self.made_run(resumed).budget
            self.assertEqual(retry.prior_repairs, 3)
            record.settle(retry.run.id, "failed")

        self.assertEqual(observed, [(0, 1080, 300, 0), (3, 1080, 300, 3), (0, 1080, 300, 0)])
        other = tasks_store.task_add(self.repo.name, "Another ticket", "Fixture", "feat")
        self.assertEqual(run_store.claim_environment_budget_resets(self.repo.name, other, "other-run", ((project, 42),)),
                         [42])


class PollFixture(Isolated):
    def run_polled_ticket(self, repo, tid, work, script=None, *, force=False):
        _, env = launch_lease.child_environment(())
        with patch.dict(os.environ, env), patch.object(launch_lease, "registered"), \
                patch.object(pipeline, "run_refusal", return_value=(False, "")), \
                patch.object(pipeline, "_run", side_effect=work):
            return pipeline.run_issue(repo, tid, script=script, force=force)

    def poll_with(self, cfg, spawn, *, dry_run=False):
        def launch_pending(repo, tid, launch):
            with launch_lease.acquiring(repo, tid):
                return launch()

        with patch.object(config, "load", return_value=cfg), patch.object(poll, "runnable", return_value=True), \
                patch.object(tasks, "spawn_pending", side_effect=launch_pending), \
                patch.object(poll, "spawn_run", side_effect=spawn), \
                patch.object(poll, "network_up_or_skip", return_value=True), \
                patch.object(poll, "settle_abandoned_runs"), patch.object(outcomes, "sync_outcomes"), \
                patch.object(outcomes, "sync_moves"), patch.object(outcomes, "sync_landings"), \
                patch.object(poll, "retry_scratch"), \
                patch.object(poll, "mark_loop"), \
                patch.object(pipeline, "SKIPPED_FILE", self.root / "skipped.json"):
            return poll.poll(dry_run=dry_run)


class FeedbackRevisionBudgetFixture(PollFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(scratch, "service_labels", side_effect=dict))

    def feedback_ticket(self, path="orchestrate", number=1):
        self.repo.layer_dir.mkdir(exist_ok=True)
        (self.repo.layer_dir / "config.toml").write_text('[limits]\nmax_repair_cycles = 3\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        parent = self.made_run()
        record.note(parent.id, active_seconds=61 * 60, verify_seconds=21 * 60, repair_calls=3)
        url = f"https://github.com/owner/fixture/pull/{number}"
        run_lines.write(parent.id, "execution", path=path)
        record.settle(parent.id, "pr", pr_url=url)
        self.feedback_row = {"pr_url": url, "repo": self.repo.name, "issue": self.ticket, "kind": "comment",
                             "gh_id": 1, "body": "Please revise", "pr_state": "OPEN", "status": "new", "at_once": 0,
                             "created": "2026-01-01T00:00:00Z"}
        insights_store.add_feedback([self.feedback_row])
        return parent


class FeedbackRevisionBudgetTests(FeedbackRevisionBudgetFixture):
    def test_feedback_park_returns_comment_to_poll_and_revision_reaches_first_step(self):
        parent = self.feedback_ticket()
        ran = []

        def prepare(flow, tree):
            run = flow.ctx.run
            ran.append(run.id)
            self.assertEqual(run.budget.parents, [])
            self.assertEqual((run.budget.prior_repairs, run.budget.prior_seconds, run.budget.verify_seconds), (0, 0, 0))
            self.assertEqual(insights_store.open_feedback(run.feedback_prs)[0]["run_id"], run.id)
            return factory.Outcome("pass")

        def drive(flow):
            flow.step("prepare", tree=str(flow.ctx.ws))
            return "noop", "answered", ""

        def spawn(repo, tid, expected):
            _, env = launch_lease.child_environment(())
            with patch.dict(os.environ, env), patch.object(launch_lease, "registered"), \
                    patch.object(pipeline, "run_refusal", return_value=(True, "")), \
                    patch.dict(factory.STEPS, {"prepare": prepare}):
                script = pipeline.Script("orchestrate", drive)
                self.assertEqual(pipeline.run_issue(repo, tid, script=script), expected)

        with patch.object(runlimits.Policy, "of", return_value=runlimits.Policy(max_repair_cycles=0)):
            self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: spawn(repo, tid, 1)), 0)
        parked = run_store.newest_whole_run(self.repo.name, self.ticket)
        self.assertEqual(parked["status"], "failed")
        self.assertIn("used its 0 repair cycles", parked["summary"])
        self.assertEqual(ran, [])
        self.assertEqual(run_lines.last_step(parked["id"]), "")
        self.assertEqual(insights_store.feedback_due(self.repo.name, "2025-12-31T23:59:59Z"), [])
        self.assertEqual(insights_store.feedback_due(self.repo.name, "2026-01-01T00:10:00Z"), [self.ticket])
        log = (paths.RUNS_DIR / parked["id"] / "run.log").read_text()
        self.assertIn("budget: left out 3 earlier repair cycles, 61.0 run minutes and 21.0 verification minutes", log)
        self.assertIn("because the owner commented on the PR since they ran", log)
        self.assertEqual(runlimits.metadata(parked["id"], "budget.json")["left_out"], [parent.id])
        self.assertTrue(any(line["event"] == "budget_chain" and line["charged"] == {}
                            for line in run_lines.read(parked["id"])))

        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: spawn(repo, tid, 0)), 0)
        self.assertEqual(len(ran), 1)
        self.assertEqual(run_lines.last_step(ran[0]), "prepare")
        self.assertEqual(insights_store.feedback_due(self.repo.name, "2026-01-01T00:10:00Z"), [])

    def test_feedback_cleanup_uses_step_execution_and_claim_owner(self):
        self.feedback_ticket()
        other = record.start("pipeline", repo=self.repo.name, issue=self.ticket)
        insights_store.add_feedback([{**self.feedback_row, "gh_id": 2}])
        url = self.feedback_row["pr_url"]
        rows = insights_store.open_feedback([url])
        self.assertEqual(insights_store.take_feedback([row for row in rows if row["gh_id"] == 2], other)[0]["gh_id"], 2)

        def drive(flow):
            flow.step("prepare")
            return "noop", "finished", ""

        def fail(*args, **kwargs):
            raise RuntimeError("fixture error")

        script = pipeline.Script("orchestrate", drive)
        with patch.object(config, "load", return_value=self.cfg), \
                patch.object(pipeline, "run_refusal", return_value=(True, "")), \
                patch.object(gh, "pr_comment"), \
                patch.object(run_store, "ticket_runs_marked_running", return_value=[]):
            for refused, settings_error in ((True, False), (False, True), (False, False)):
                with self.subTest(refused=refused, settings_error=settings_error), \
                        patch.object(flows, "start", wraps=flows.start) as start, \
                        patch.object(flows.Job, "labels", side_effect=fail if settings_error else None, return_value={}), \
                        patch.dict(factory.STEPS, {"prepare": fail}):
                    if refused:
                        start.side_effect = lambda cfg, run, *args: factory.Flow(run.id, None, refusal="fixture refusal")
                    self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, script=script), 1)
                claims = {row["gh_id"]: row for row in insights_store.open_feedback([url])}
                self.assertEqual((claims[2]["status"], claims[2]["run_id"]), ("taken", other))
                if refused or settings_error:
                    self.assertEqual((claims[1]["status"], claims[1]["run_id"]), ("new", None))
                else:
                    self.assertEqual(claims[1]["status"], "failed")

    def test_classic_feedback_cleanup_stops_at_first_launch(self):
        parent = self.feedback_ticket(path="classic")
        record.note(parent.id, active_seconds=12 * 60, repair_calls=0)

        def work(cfg, run, slot):
            run.agent("revise", "Apply the feedback", self.repo.path)
            self.fail("the model call should have failed")

        def fail_call(*args, on_launch, **kwargs):
            if launched:
                on_launch({"runner": "replay"})
            raise RuntimeError("fixture model error")

        with patch.object(config, "load", return_value=self.cfg), patch.object(gh, "pr_comment"), \
                patch.object(pipeline.runner, "run", side_effect=fail_call):
            for launched, expected in ((False, "new"), (True, "failed")):
                with self.subTest(launched=launched):
                    self.assertEqual(self.run_polled_ticket(self.repo.name, self.ticket, work), 1)
                    row = insights_store.open_feedback([self.feedback_row["pr_url"]])[0]
                    newest = run_store.newest_whole_run(self.repo.name, self.ticket)["id"]
                    self.assertEqual((row["status"], row["run_id"]), (expected, newest if launched else None))

    def test_reaping_revision_before_first_step_returns_comment_for_fresh_poll(self):
        parent = self.feedback_ticket()
        url = self.feedback_row["pr_url"]
        self.local.label(self.ticket, add=["pr"])
        interrupted = self.made_run(parent.id)
        record.note(interrupted.id, pid=None)
        run_lines.write(interrupted.id, "execution", path="orchestrate")
        insights_store.take_feedback(insights_store.open_feedback([url]), interrupted.id)
        with patch.object(record, "SETTLE_GRACE_SECONDS", 0):
            reaped, = record.reap(tickets=True)
        self.assertEqual((reaped["id"], reaped["status"]), (interrupted.id, "interrupted"))
        row, = insights_store.open_feedback([url])
        self.assertEqual((row["status"], row["run_id"]), ("new", None))
        self.assertEqual(insights_store.feedback_due(self.repo.name, "2025-12-31T23:59:59Z"), [])
        self.assertEqual(insights_store.feedback_due(self.repo.name, "2026-01-01T00:10:00Z"), [self.ticket])
        insights_store.add_feedback([{**self.feedback_row, "gh_id": 2,
                                      "created": "2026-01-01T00:09:00Z"}])
        reached = []

        def work(cfg, run, slot):
            reached.append(run.id)
            self.assertEqual(run.budget.parents, [])
            self.assertEqual((run.budget.prior_repairs, run.budget.prior_seconds, run.budget.verify_seconds), (0, 0, 0))
            self.assertEqual([row["gh_id"] for row in run.feedback], [1, 2])
            run.step_started = True
            return "noop", "answered", ""

        with patch.object(util, "now", return_value=datetime(2026, 1, 1, 0, 15, tzinfo=UTC)):
            self.assertEqual(self.poll_with(self.cfg, lambda *args: self.fail("feedback is still in its quiet period")),
                             0)
        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_revision(repo, tid, work)), 0)
        self.assertEqual(len(reached), 1)
        self.assertIn(interrupted.id, runlimits.metadata(reached[0], "budget.json")["left_out"])

    def test_interrupted_revision_after_first_step_keeps_spent_budget_until_new_feedback(self):
        self.feedback_ticket()
        url = self.feedback_row["pr_url"]
        interrupted = self.made_run()
        record.note(interrupted.id, active_seconds=1200, verify_seconds=300, repair_calls=3)
        insights_store.take_feedback(insights_store.open_feedback([url]), interrupted.id)
        run_lines.write(interrupted.id, "execution", path="orchestrate")
        run_lines.write(interrupted.id, "step_start", step="revise")
        self.assertEqual(record.interrupt(run_store.run_get(interrupted.id))["status"], "interrupted")
        row, = insights_store.open_feedback([url])
        self.assertEqual((row["status"], row["run_id"]), ("taken", interrupted.id))
        reached = []

        def work(cfg, run, slot):
            if run.budget.prior_repairs:
                run.budget.charge("review_fix4", "review_repair")
            reached.append((run.budget.prior_repairs, run.budget.prior_seconds, run.budget.verify_seconds,
                            sorted(row["gh_id"] for row in run.feedback)))
            run.step_started = True
            return "noop", "answered", ""

        def spawn(repo, tid):
            return self.run_polled_revision(repo, tid, work)

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        parked = run_store.newest_whole_run(self.repo.name, self.ticket)
        self.assertIn("used its 3 repair cycles", parked["summary"])
        self.assertEqual(reached, [])
        charged, = [line["charged"] for line in run_lines.read(parked["id"]) if line["event"] == "budget_chain"]
        usage = charged[interrupted.id]
        self.assertEqual((usage["repair_calls"], usage["run_minutes"], usage["verification_minutes"]), (3, 20, 5))
        self.assertEqual(insights_store.feedback_due(self.repo.name, "2026-01-01T00:10:00Z"), [self.ticket])
        row, = insights_store.open_feedback([url])
        self.assertEqual((row["status"], row["run_id"]), ("new", interrupted.id))
        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(run_store.newest_whole_run(self.repo.name, self.ticket)["id"], parked["id"])

        retry = self.made_run(parked["id"])
        run_lines.write(retry.id, "execution", path="orchestrate")
        record.settle(retry.id, "interrupted")
        self.assertEqual(runlimits.latest_park(self.repo.name, self.ticket)[0]["id"], parked["id"])
        insights_store.add_feedback([{**self.feedback_row, "gh_id": 2}])
        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(reached, [(0, 0, 0, [1, 2])])

    def test_closed_feedback_pr_noop_is_recorded_with_an_open_sibling(self):
        parent = self.feedback_ticket()
        url = self.feedback_row["pr_url"]
        sibling = "https://github.com/owner/fixture/pull/2"
        record.note(parent.id, pr_url=f"{url} {sibling}")
        insights_store.add_feedback([{**self.feedback_row, "pr_url": sibling, "gh_id": 2}])
        self.local.label(self.ticket, add=["pr"])

        def spawn(repo, tid):
            _, env = launch_lease.child_environment(())
            with patch.dict(os.environ, env), patch.object(launch_lease, "registered"), \
                    patch.object(pipeline, "run_refusal", return_value=(False, "")):
                script = pipeline.Script("orchestrate", lambda flow: orchestration.Controller(flow).prepare())
                self.assertEqual(pipeline.run_issue(repo, tid, script=script), 0)

        with (patch.object(gh, "pr_view", side_effect=lambda pr: {"state": "CLOSED" if pr == url else "OPEN"}),
              patch.object(pipeline.runner, "run") as model):
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            noop = run_store.newest_whole_run(self.repo.name, self.ticket)
            self.assertEqual(noop["status"], "noop")
            self.assertIn(url, noop["summary"])
            feedback = {row["pr_url"]: (row["status"], row["run_id"])
                        for row in insights_store.feedback_between("2025-01-01", "9999-01-01")}
            self.assertEqual(feedback, {url: ("recorded", noop["id"]), sibling: ("new", None)})
            self.assertEqual(run_store.open_runs(self.repo.name, self.ticket)[0]["id"], parent.id)
            self.assertEqual(insights_store.feedback_due(self.repo.name, "2026-01-01T00:10:00Z"), [self.ticket])
            model.assert_not_called()

        revised = []

        def work(cfg, run, slot):
            revised.extend(run.feedback_prs)
            run.step_started = True
            return "noop", "answered", ""

        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_revision(repo, tid, work)), 0)
        self.assertEqual(revised, [sibling])

    def test_force_readmission_after_published_head_moves_resets_failed_feedback_budget(self):
        self.feedback_ticket()
        url = self.feedback_row["pr_url"]
        branch = "arete/fixture"
        head = "a" * 40
        reached = []

        def work(cfg, run, slot):
            reached.append(run.id)
            run.agent("revise", "Apply the feedback", self.repo.path)
            self.assertEqual((run.budget.prior_repairs, run.budget.prior_seconds, run.budget.verify_seconds), (0, 0, 0))
            if run.force:
                self.assertEqual([(row["gh_id"], row["status"]) for row in run.feedback], [(1, "failed")])
                return "pr", "revised", url
            record.note(run.id, branch=branch, pr_url=url)
            for i in range(3):
                run.budget.charge(f"review_fix{i + 1}", "review_repair")
            run.budget.check_repairs()
            self.fail("the first revision should have parked")

        def launch(*args, on_launch, **kwargs):
            on_launch({"runner": "replay"})
            return Result(ok=True, runner="replay")

        with (patch.object(config, "load", return_value=self.cfg),
              patch.object(runlimits, "branch_head", side_effect=lambda src, name: head) as published,
              patch.object(pipeline.runner, "run", side_effect=launch)):
            self.assertEqual(self.run_polled_revision(self.repo.name, self.ticket, work), 1)
            parked = run_store.run_get(reached[0])
            self.assertIn("used its 3 repair cycles", parked["summary"])
            self.assertEqual(parked["repair_calls"], 3)
            row, = insights_store.open_feedback([url])
            self.assertEqual((row["status"], row["run_id"]), ("failed", parked["id"]))
            self.assertEqual(self.run_polled_revision(self.repo.name, self.ticket, work, force=True), 2)
            self.assertEqual(reached, [parked["id"]])

            head = "b" * 40
            self.assertEqual(self.run_polled_revision(self.repo.name, self.ticket, work, force=True), 0)
            published.assert_called_with(self.repo.path, branch)

        self.assertEqual(len(reached), 2)
        self.assertEqual(run_lines.last_step(reached[1]), "revise")
        self.assertEqual(insights_store.open_feedback([url]), [])

    def run_polled_revision(self, repo, tid, work, *, force=False):
        script = pipeline.Script("orchestrate", lambda flow: work(self.cfg, flow.ctx.run, None))
        return self.run_polled_ticket(repo, tid, None, script, force=force)


class AdwFeedbackScopeTests(FeedbackRevisionBudgetFixture):
    def test_due_feedback_uses_recorded_custom_feature_without_revise_script(self):
        parent = self.feedback_ticket(path="feature")
        record.note(parent.id, active_seconds=12 * 60)
        self.cfg.adws = self.root / "custom"
        self.cfg.adws.mkdir()
        script = self.cfg.adws / "feature.py"
        script.write_text(
            "def feature(flow):\n"
            "    flow.step('revise')\n"
            "    return 'pr', 'revised', flow.ctx.run.feedback_prs[0]\n"
        )
        reached = []

        def revise(flow):
            run = flow.ctx.run
            reached.append([row["gh_id"] for row in run.feedback])
            self.assertEqual((flow.continues, run.repairs), ("", ""))
            self.assertEqual(run.budget.prior_repairs, 3)
            row, = insights_store.open_feedback(run.feedback_prs)
            self.assertEqual((row["status"], row["run_id"]), ("taken", run.id))
            return factory.Outcome("pass")

        def spawn(repo, tid):
            command = pipeline.run_command(repo, tid)
            self.assertIn(str(script), command)
            _, env = launch_lease.child_environment(())
            with patch.dict(os.environ, env), patch.object(launch_lease, "registered"), \
                    patch.object(pipeline, "run_refusal", return_value=(True, "")), \
                    patch.dict(factory.STEPS, {"revise": revise}):
                with self.assertRaises(SystemExit) as result:
                    factory.main(pipeline.flow_function(self.cfg, "feature"), command[-2:])
                self.assertEqual(result.exception.code, 0)

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        revised = run_store.newest_whole_run(self.repo.name, self.ticket)
        interrupted = self.made_run(revised["id"])
        run_lines.write(interrupted.id, "execution", path="repair")
        run_lines.write(interrupted.id, "repair", source=revised["id"])
        run_lines.write(interrupted.id, "step_start", step="prepare")
        record.settle(interrupted.id, "interrupted")
        insights_store.add_feedback([{**self.feedback_row, "gh_id": 2}])

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(reached, [[1], [2]])
        self.assertEqual(insights_store.open_feedback([self.feedback_row["pr_url"]]), [])

    def test_standalone_verify_with_pending_feedback_keeps_inherited_budget(self):
        parent = self.feedback_ticket(path="feature")
        record.note(parent.id, active_seconds=12 * 60)
        run_lines.write(parent.id, "task", path="feature")
        run_lines.write(parent.id, "step_start", step="verify")
        verified = []

        def verify(flow):
            with flow.ctx.run.budget.verifying():
                verified.append(flow.run_id)
            return factory.Outcome("pass")

        def drive(flow):
            flow.step("verify")
            return "step", "", ""

        with patch.object(config, "load", return_value=self.cfg), \
                patch.object(pipeline, "run_refusal", return_value=(True, "")), \
                patch.dict(factory.STEPS, {"verify": verify}), \
                patch.object(pipeline, "flow_function", return_value=drive):
            fn, argv = factory.earlier([parent.id, "verify"])
            with self.assertRaises(SystemExit) as result:
                factory.main(fn, argv)
            self.assertEqual(result.exception.code, 1)

        newest = run_store.ticket_runs(self.repo.name, self.ticket, limit=1)[0]
        self.assertIn("verification reached its cumulative 20 minute budget", newest["summary"])
        self.assertEqual(verified, [])
        charged = factory.first(run_lines.read(newest["id"]), "budget_chain")["charged"][parent.id]
        self.assertEqual((charged["repair_calls"], charged["run_minutes"], charged["verification_minutes"]), (3, 12, 21))
        row, = insights_store.open_feedback([self.feedback_row["pr_url"]])
        self.assertEqual((row["status"], row["run_id"]), ("new", None))

    def test_interrupted_adw_revision_reuses_saved_outcome_only_for_same_feedback(self):
        for new_feedback in (False, True):
            with self.subTest(new_feedback=new_feedback):
                if new_feedback:
                    self.ticket = tasks_store.task_add(self.repo.name, "Second specification", "Fixture", "feat")
                parent = self.feedback_ticket(path="revise", number=2 if new_feedback else 1)
                record.note(parent.id, active_seconds=12 * 60)
                url = self.feedback_row["pr_url"]
                prompts, goals, prepared = [], [], []

                def prepare(flow, tree):
                    prepared.append(tree)
                    Path(tree).mkdir(exist_ok=True)
                    util.git(["init", "-q", "-b", "revision"], tree)
                    util.git(["-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                              "-c", "commit.gpgSign=false", "commit", "-qm", "initial", "--allow-empty"], tree)
                    checkout = workspace.Checkout("", "owner/fixture", Path(tree), Path(tree), "main",
                                                  revision=util.git(["rev-parse", "HEAD"], tree).strip())
                    return factory.Outcome("pass", data=flows.prepared_fields(
                        self.local.get(self.ticket), "revision", [checkout], config.load_layer(self.repo.layer_dir),
                        [], "", None))

                def agent(run, step, prompt, cwd, **kwargs):
                    self.assertEqual(step, "revise")
                    prompts.append(prompt)
                    return Result(ok=True, structured={"summary": "revised", "replies": [
                        {"comment_id": row["gh_id"], "reply": row["body"]} for row in run.feedback]})

                def revise(flow, tree):
                    outcome = flows.revision(flow, tree)
                    if not new_feedback:
                        outcome.data.pop("feedback_keys", None)
                    return outcome

                def checks(flow, tree):
                    self.assertEqual(flow.ctx.run.budget.prior_repairs, 3)
                    claimed = insights_store.open_feedback([url])
                    self.assertEqual([(row["status"], row["run_id"]) for row in claimed],
                                     [("taken", flow.run_id)] * (2 if new_feedback else 1))
                    return factory.Outcome("pass")

                def drive(flow):
                    tree = str(flow.ctx.ws)
                    flows.step(flow, "prepare", tree=tree)
                    flows.step(flow, "revise", tree=tree)
                    if not flow.continues:
                        raise KeyboardInterrupt
                    goals.append((flow.ctx.goal, [r["comment_id"] for r in flow.ctx.replies]))
                    flow.step("checks", tree=tree)
                    return "pr", "revised", url

                script = pipeline.Script("revise", drive)

                def spawn(repo, tid):
                    self.assertEqual(pipeline.run_command(repo, tid), pipeline.script_command(self.cfg, "revise", repo, tid))
                    _, env = launch_lease.child_environment(())
                    with patch.dict(os.environ, env), patch.object(launch_lease, "registered"):
                        self.assertEqual(pipeline.run_issue(repo, tid, script=script), 0)

                with patch.object(config, "load", return_value=self.cfg), \
                        patch.object(pipeline, "run_refusal", return_value=(True, "")), \
                        patch.object(pipeline.Run, "agent", autospec=True, side_effect=agent), \
                        patch.object(pipeline, "stage", return_value=SimpleNamespace(empty=False)), \
                        patch.object(gh, "pr_body", return_value=""), \
                        patch.object(gh, "pr_view", return_value={"state": "OPEN"}), patch.object(gh, "pr_comment"), \
                        patch.object(workspace, "remote_head", return_value=""), \
                        patch.dict(factory.STEPS, {"prepare": prepare, "revise": revise, "checks": checks}):
                    with self.assertRaises(KeyboardInterrupt):
                        pipeline.run_issue(self.repo.name, self.ticket, script=script)
                    interrupted = run_store.newest_whole_run(self.repo.name, self.ticket)
                    record.interrupt(interrupted)
                    if new_feedback:
                        insights_store.add_feedback([{**self.feedback_row, "gh_id": 2, "body": "Also handle empty input"}])
                    self.assertEqual(self.poll_with(self.cfg, spawn), 0)
                self.assertEqual(len(prepared), 1)
                self.assertEqual(len(prompts), 2 if new_feedback else 1)
                goal, replies = goals[0]
                self.assertIn("Please revise", goal)
                if new_feedback:
                    self.assertIn("Also handle empty input", prompts[-1])
                    self.assertIn("Also handle empty input", goal)
                self.assertEqual(replies, [1, 2] if new_feedback else [1])
                self.assertEqual(insights_store.open_feedback([url]), [])


class ClassicFeedbackBudgetTests(FeedbackRevisionBudgetFixture):
    def test_interrupted_revision_at_repair_limit_can_publish(self):
        self.feedback_ticket(path="classic")
        url = self.feedback_row["pr_url"]
        interrupted = self.made_run()
        record.note(interrupted.id, active_seconds=1200, verify_seconds=300, repair_calls=3)
        insights_store.take_feedback(insights_store.open_feedback([url]), interrupted.id)
        run_lines.write(interrupted.id, "step_start", step="revise")
        record.interrupt(run_store.run_get(interrupted.id))

        def work(cfg, run, slot):
            self.assertEqual((run.budget.prior_repairs, run.budget.prior_seconds, run.budget.verify_seconds),
                             (3, 1200, 300))
            self.assertEqual([row["gh_id"] for row in run.feedback], [1])
            run.budget.charge("verify", "verify")
            run.agent("revise", "Finish the revision", self.repo.path)
            return "pr", "revised", url

        def launch(*args, on_launch, **kwargs):
            on_launch({"runner": "replay"})
            return Result(ok=True, runner="replay")

        with patch.object(pipeline.runner, "run", side_effect=launch):
            self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, work)), 0)
        revised = run_store.newest_whole_run(self.repo.name, self.ticket)
        self.assertEqual((revised["status"], revised["pr_url"], revised["repair_calls"]), ("pr", url, 0))
        self.assertEqual(insights_store.open_feedback([url]), [])


class FeedbackRevisionRegressionTests(FeedbackRevisionBudgetFixture):
    def test_classic_revision_returns_open_pr_feedback_when_another_pr_closes(self):
        parent = self.feedback_ticket(path="classic")
        closed = self.feedback_row["pr_url"]
        opened = "https://github.com/owner/fixture/pull/2"
        record.note(parent.id, pr_url=f"{closed} {opened}", active_seconds=10 * 60)
        insights_store.add_feedback([{**self.feedback_row, "pr_url": opened, "gh_id": 2}])

        with (patch.object(config, "load", return_value=self.cfg),
              patch.object(pipeline, "run_refusal", return_value=(False, "")),
              patch.object(gh, "pr_view", side_effect=lambda url: {"state": "CLOSED" if url == closed else "OPEN"}),
              patch.object(pipeline.runner, "run") as model):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket), 0)

        revision = run_store.newest_whole_run(self.repo.name, self.ticket)
        self.assertEqual(revision["status"], "noop")
        feedback = {row["pr_url"]: (row["status"], row["run_id"])
                    for row in insights_store.feedback_between("2025-01-01", "9999-01-01")}
        self.assertEqual(feedback, {closed: ("recorded", revision["id"]), opened: ("new", None)})
        self.assertEqual(insights_store.feedback_due(self.repo.name, "2026-01-01T00:10:00Z"), [self.ticket])
        model.assert_not_called()

    def test_adw_without_a_revision_step_leaves_feedback_pending(self):
        parent = self.feedback_ticket(path="feature")
        record.note(parent.id, active_seconds=10 * 60)
        url = self.feedback_row["pr_url"]

        def drive(flow):
            run = flow.ctx.run
            self.assertEqual(run.budget.prior_repairs, 3)
            row, = insights_store.open_feedback([url])
            self.assertEqual((row["status"], row["run_id"]), ("new", None))
            flow.step("prepare", tree=str(flow.ctx.ws))
            return "noop", "ended before revise", ""

        with (patch.object(config, "load", return_value=self.cfg),
              patch.object(pipeline, "run_refusal", return_value=(True, "")),
              patch.dict(factory.STEPS, {"prepare": lambda *args, **kw: factory.Outcome("pass")})):
            for path in ("feature", "patch", "revise"):
                with self.subTest(path=path):
                    script = pipeline.Script(path, drive)
                    self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, script=script), 0)
                    row, = insights_store.open_feedback([url])
                    self.assertEqual((row["status"], row["run_id"]), ("new", None))

    def test_interrupted_adw_feature_can_resume_during_the_feedback_quiet_period(self):
        parent = self.feedback_ticket(path="feature")
        interrupted = self.made_run(parent.id)
        run_lines.write(interrupted.id, "execution", path="feature")
        record.settle(interrupted.id, "interrupted")

        with patch.object(util, "now", return_value=datetime(2026, 1, 1, 0, 1, tzinfo=UTC)):
            picked = poll.next_pick(self.repo, self.local, pipeline.SkipLog({}), set())

        self.assertEqual(picked, poll.Pick(self.ticket, "restarting after an interruption"))

    def test_dry_run_keeps_budget_parents_even_when_admission_grants_a_reset(self):
        parent = self.feedback_ticket()
        record.note(parent.id, active_seconds=10 * 60)
        script = pipeline.Script("orchestrate", lambda flow: ("dry-run", "preview finished", ""))
        with (patch.object(config, "load", return_value=self.cfg),
              patch.object(pipeline, "run_refusal", return_value=(True, "")),
              patch.object(runlimits, "admission", return_value=runlimits.Admission(reset_time=True, reset_repairs=True))):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, dry_run=True, force=True, script=script), 0)

        preview = run_store.newest_whole_run(self.repo.name, self.ticket)
        saved = runlimits.metadata(preview["id"], "budget.json")
        self.assertEqual((saved["parents"], saved["repair_parents"]), ([parent.id], [parent.id]))
        self.assertNotIn("left_out", saved)
        self.assertNotIn("repair_left_out", saved)
        row, = insights_store.open_feedback([self.feedback_row["pr_url"]])
        self.assertEqual((row["status"], row["run_id"]), ("new", None))


class BuiltInFeedbackPublicationTests(FeedbackRevisionBudgetFixture):
    def test_feature_does_not_push_if_a_feedback_pr_closes_after_prepare(self):
        parent = self.feedback_ticket(path="feature")
        record.note(parent.id, active_seconds=0, verify_seconds=0)
        url = self.feedback_row["pr_url"]

        def publish(flow, *args, **kwargs):
            self.assertEqual(flow.ctx.run.feedback, [])
            return flows.step(flow, "pr", tree=str(flow.ctx.ws))

        script = pipeline.Script("feature", feature.feature)
        with (patch.object(config, "load", return_value=self.cfg),
              patch.object(pipeline, "run_refusal", return_value=(True, "")),
              patch.object(flows, "local_workspace", return_value=[]),
              patch.object(pipeline, "settle_starts"),
              patch.object(pipeline, "furnish", return_value=""),
              patch.object(workspace, "note_made"),
              patch.object(feature, "built", side_effect=publish),
              patch.object(gh, "pr_view", side_effect=[{"state": "OPEN"}, {"state": "CLOSED"}]) as pr_view,
              patch.object(flows, "settle_unpushed_checkouts") as settle,
              patch.object(pipeline, "commit_and_push") as push):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, script=script), 1)

        self.assertEqual([call.args for call in pr_view.call_args_list], [(url,), (url,)])
        settle.assert_not_called()
        push.assert_not_called()
        revision = run_store.newest_whole_run(self.repo.name, self.ticket)
        self.assertIn("is closed, so arete pushed nothing for PR feedback", revision["summary"])
        row, = insights_store.open_feedback([url])
        self.assertEqual((row["status"], row["run_id"]), ("new", None))


class OwnerAnswerBudgetFixture(PollFixture):
    def spent_question(self, *, structured=False):
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text(
            '[factory]\npath = "classic"\n[limits]\nmax_repair_cycles = 3\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        first = self.made_run()
        first.budget.charge("repair", "review_repair")
        record.settle(first.id, "failed")
        asked = self.made_run(first.id)
        asked.budget = runlimits.Budget(asked, runlimits.Policy(max_repair_cycles=3), first.id)
        for _ in range(2):
            asked.budget.charge("repair", "review_repair")
        question = "Where is the CI log?"
        asks = [{"ask_id": "ask-fixture", "step": "review", "call": "review", "questions": [
            {"kind": "text", "header": "CI log", "question": question}]}] if structured else None
        if structured:
            run_lines.write(asked.id, "execution", path="feature")
        with patch.object(pipeline, "release_workspace"), patch.object(pipeline.alerts, "ask_notice"):
            status, summary, _ = pipeline._ask(asked, self.repo.path, [], question, question, asks,
                                               {} if structured else None)
        self.assertEqual(status, "needs-info")
        record.settle(asked.id, status, summary=summary)
        with foundation.tx():
            run_store.checkpoint(first.id, 60, 20, 0, 1, util.iso())
            run_store.checkpoint(asked.id, 120, 40, 0, 2, util.iso())
        return first, asked

    def assert_answer_budget(self, run, first, asked, preview=None):
        parents = [asked.id, first.id]
        seconds = (180, 60)
        if preview:
            parents.insert(0, preview)
            seconds = (210, 70)
        self.assertEqual((run.budget.prior_seconds, run.budget.verify_seconds), seconds)
        self.assertEqual(run.budget.prior_repairs, 0)
        run.budget.charge("repair", "review_repair")
        self.assertEqual(runlimits.repair_count(run.id), 1)
        audit = next(line for line in (run.dir / "run.log").read_text().splitlines() if "budget chain:" in line)
        _, reason, omitted = audit.partition("because of an owner answer: ")
        self.assertTrue(reason, audit)
        self.assertCountEqual(omitted.split(", "), parents)
        self.assertEqual(runlimits.family(run.id, repairs=True), [run.id])
        self.assertEqual(set(runlimits.family(run.id)), {run.id, *parents})

    def consume_tracker_answers(self, run, expected):
        self.assertEqual({a["id"]: a["answers"][0]["text"] for a in run.answers_due}, expected)
        with patch.object(factory, "inside", return_value=factory.Calls(run.id, "review")), \
                patch.object(pipeline.AgentRun, "agent", return_value=Result(ok=True)) as agent:
            run.agent("review", "Review the change.", self.repo.path)
        for ask_id, text in expected.items():
            self.assertIn(text, agent.call_args.args[1])
            self.assertEqual(ask_store.ask_get(ask_id)["taken_by"], run.id)
        self.assertEqual(run.answers_due, [])

    def run_polled_answer(self, repo, tid, asked, work):
        self.assertEqual(pipeline.run_command(repo, tid)[-2:], ["--continue", asked.id])
        script = pipeline.Script("feature", lambda flow: work(flow.ctx.run), continues=asked.id)
        self.assertEqual(self.run_polled_ticket(repo, tid, lambda *_: self.fail("started an ordinary run"), script), 0)

    def expired_question(self):
        with patch.object(util, "now", return_value=datetime(2026, 1, 1, tzinfo=UTC)):
            first, asked = self.spent_question(structured=True)
        with (patch.object(util, "now", return_value=datetime(2026, 1, 2, tzinfo=UTC)),
              principals.acting(principals.seed_owner("fixture@example.test"))):
            tasks_store.task_comment(self.ticket, principals.current(), "An older comment.")
        with patch.object(util, "now", return_value=datetime(2026, 1, 15, tzinfo=UTC)), \
                patch.object(config, "load", return_value=self.cfg), \
                patch.object(tasks, "kept_folder_removed", return_value=workspace.Release("removed")):
            self.assertEqual(len(tasks.expire_asks(self.cfg)), 1)
        self.assertEqual(ask_store.ask_get("ask-fixture")["status"], "expired")
        self.assertIn("Reply here and remove", self.local.get(self.ticket).comments)
        return first, asked

    def check_tracker_reply_budget(self, *, structured, expired=False, dry_run=False):
        if expired:
            first, asked = self.expired_question()
        else:
            first, asked = self.spent_question(structured=structured)
        preview = None
        if dry_run:
            with patch.object(config, "load", return_value=self.cfg), \
                    patch.object(pipeline, "run_refusal", return_value=(False, "")), \
                    patch.object(pipeline, "_run", return_value=("dry-run", "preview finished", "")):
                self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, dry_run=True), 0)
            preview = run_store.newest_whole_run(self.repo.name, self.ticket)["id"]
            self.assertNotEqual(preview, asked.id)
            with foundation.tx():
                run_store.checkpoint(preview, 30, 10, 0, 0, util.iso())

        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("launched without a reply")), 0)
        replied_at = datetime(2026, 1, 16, tzinfo=UTC) if expired else util.now()
        with patch.object(util, "now", return_value=replied_at), \
                principals.acting(principals.seed_owner("fixture@example.test")):
            tasks_store.task_comment(self.ticket, principals.current(), "See the attached log.")
        if expired:
            self.local.label(self.ticket, remove=["needs-info"])
        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("dry poll launched"), dry_run=True), 0)
        self.assertFalse(pipeline.answer_text(asked.id))
        if structured:
            self.assertEqual(ask_store.ask_get("ask-fixture")["status"], "expired" if expired else "open")

        def work(run):
            self.assert_answer_budget(run, first, asked, preview)
            if structured and not expired:
                self.consume_tracker_answers(run, {"ask-fixture": "See the attached log."})
            else:
                self.assertEqual(run.answering["id"], asked.id)
                self.assertEqual(pipeline.answer_text(asked.id), "See the attached log.")
            return "noop", "fixture finished", ""

        started = []

        def spawn(repo, tid):
            started.append(tid)
            if structured and not expired:
                self.run_polled_answer(repo, tid, asked, work)
                return
            if expired:
                self.assertNotIn("--continue", pipeline.run_command(repo, tid))
            self.assertEqual(self.run_polled_ticket(repo, tid, lambda cfg, run, slot: work(run)), 0)

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(started, [self.ticket])


class AnswerContinuationBudgetTests(OwnerAnswerBudgetFixture):
    def test_cli_answer_gives_the_first_repair_a_fresh_budget_and_resume_keeps_the_cutoff(self):
        first, asked = self.spent_question(structured=True)

        def work(flow):
            run = flow.ctx.run
            self.assertEqual([a["run"] for a in run.answers_due], [asked.id])
            self.assert_answer_budget(run, first, asked)
            flow.route()
            return "interrupted", "stopped before the answer was consumed", ""

        def launch(command, log):
            self.assertEqual(command[-5:], ["--force", "--continue", asked.id, "--restart", "answer"])
            script = pipeline.Script("feature", work, continues=asked.id)
            self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, force=True, script=script), 1)
            return run_store.newest_whole_run(self.repo.name, self.ticket)["id"]

        with patch.object(config, "load", return_value=self.cfg), \
                patch.object(pipeline, "run_refusal", return_value=(True, "")), \
                patch.object(tasks, "spawned", side_effect=lambda repo, tid, spawn: (spawn(), "interrupted")), \
                patch.object(tasks.control, "launch", side_effect=launch):
            started = tasks.answer_ask("ask-fixture", {"answers": [{"text": "See the attached log."}]})
            answered = started["run"]

            def resume(flow):
                run = flow.ctx.run
                self.assertEqual([a["run"] for a in run.answers_due], [asked.id])
                self.assertEqual(run.budget.repair_parents, [answered])
                self.assertEqual(run.budget.prior_repairs, 1)
                for _ in range(2):
                    run.budget.charge("repair", "review_repair")
                with self.assertRaisesRegex(runlimits.Parked, "3 repair cycles"):
                    run.budget.charge("repair", "review_repair")
                return "noop", "fixture finished", ""

            script = pipeline.Script("feature", resume, continues=answered)
            self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, force=True, script=script), 0)

    def test_tracker_reply_to_an_unstructured_question_gives_a_fresh_budget_after_a_dry_run(self):
        self.check_tracker_reply_budget(structured=False, dry_run=True)

    def test_tracker_reply_to_a_structured_question_gives_a_fresh_budget_after_a_dry_run(self):
        self.check_tracker_reply_budget(structured=True, dry_run=True)


class TrackerReplyBudgetTests(OwnerAnswerBudgetFixture):
    def test_tracker_reply_to_a_structured_question_gives_a_fresh_budget(self):
        self.check_tracker_reply_budget(structured=True)

    def test_tracker_reply_to_an_unstructured_question_gives_a_fresh_budget(self):
        self.check_tracker_reply_budget(structured=False)

    def test_tracker_restart_after_an_expired_structured_question_gives_a_fresh_budget(self):
        self.check_tracker_reply_budget(structured=True, expired=True)
        self.assertEqual(ask_store.ask_get("ask-fixture")["status"], "expired")

    def test_tracker_reply_keeps_the_dashboard_answer_written_during_lookup(self):
        asked_at = datetime(2026, 1, 2, tzinfo=UTC)
        with patch.object(util, "now", return_value=asked_at):
            first, asked = self.spent_question(structured=True)
            ask_store.ask_add("ask-second", asked.id, self.repo.name, self.ticket, "review", "review", "", "", [
                {"kind": "text", "header": "Failed job", "question": "Which CI job failed?"}], {})
        owner = principals.seed_owner("fixture@example.test")
        with patch.object(util, "now", return_value=asked_at - timedelta(days=1)), principals.acting(owner):
            tasks_store.task_comment(self.ticket, principals.current(), "An older comment.")
        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("launched with open asks")), 0)
        self.assertEqual(len(ask_store.asks_of(asked.id, "open")), 2)
        with patch.object(util, "now", return_value=asked_at + timedelta(days=1)), principals.acting(owner):
            tasks_store.task_comment(self.ticket, principals.current(), "The lint job failed.")

        reply_since = tracker.LocalTracker.reply_since

        def reply_after_dashboard_answer(source, tid, since):
            self.assertFalse(util.lock_is_held(tasks.STEER_LOCK))
            answer = tasks.answer_ask("ask-fixture", {"answers": [{"text": "The log is on the dashboard."}]})
            self.assertEqual(answer["state"], "waiting")
            self.assertEqual(ask_store.ask_get("ask-second")["status"], "open")
            return reply_since(source, tid, since)

        started = []

        def work(run):
            started.append(run.id)
            self.assert_answer_budget(run, first, asked)
            self.consume_tracker_answers(run, {"ask-fixture": "The log is on the dashboard.",
                                               "ask-second": "The lint job failed."})
            return "noop", "fixture finished", ""

        with patch.object(tracker.LocalTracker, "reply_since", autospec=True, side_effect=reply_after_dashboard_answer):
            self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_answer(repo, tid, asked, work)), 0)
        self.assertEqual(len(started), 1)


class UnansweredRepairBudgetTests(OwnerAnswerBudgetFixture):
    def test_github_label_removal_without_a_reply_admits_a_run_with_spent_repairs(self):
        self.repo = replace(self.repo, name="github-fixture", github="owner/project", tracker="github",
                            parts=[config.Part("", "owner/project")])
        self.cfg.repos = [self.repo]
        self.ticket = "697"
        labels = {tracker.LABEL}
        source = tracker.GitHubTracker(self.repo)

        def issue_view(repo, number):
            return {"number": number, "title": "Fixture issue", "body": "Fixture description", "state": "OPEN",
                    "author": {"login": "owner"}, "labels": [{"name": label} for label in sorted(labels)],
                    "comments": []}

        def issue_list(repo, label, **kwargs):
            return [issue_view(repo, int(self.ticket))] if label in labels else []

        def edit_labels(repo, number, add=(), remove=()):
            labels.difference_update(remove)
            labels.update(add)

        api = support.parentless_github_issue(self.repo.github, self.ticket, issue_view)
        with (patch.object(gh, "issue", side_effect=issue_view), patch.object(gh, "issues", side_effect=issue_list),
              patch.object(gh, "gh_json", side_effect=api), patch.object(gh, "issue_labels", side_effect=edit_labels),
              patch.object(gh, "ensure_label", return_value=""),
              patch.object(gh, "api_create_comment", return_value=("1", "https://github.com/owner/project/issues/697#issuecomment-1")),
              patch.object(gh, "issue_comment")):
            from arete import outbox

            first, asked = self.spent_question(structured=True)
            outbox.send_due()
            self.assertIn(source.label_name("needs-info"), labels)
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("launched before label removal")), 0)
            self.assertEqual(source.label(self.ticket, remove=["needs-info"]), "")
            outbox.send_due()
            started = []

            def work(cfg, run, slot):
                started.append(run.id)
                self.assertEqual(ask_store.ask_get("ask-fixture")["status"], "open")
                self.assertEqual(run.answers_due, [])
                self.assertFalse(pipeline.answer_text(asked.id))
                self.assertEqual(run.budget.repair_left_out, [])
                self.assertCountEqual(run.budget.repair_parents, [first.id, asked.id])
                self.assertEqual(run.budget.prior_repairs, 3)
                with self.assertRaisesRegex(runlimits.Parked, "3 repair cycles"):
                    run.budget.charge("repair", "review_repair")
                return "noop", "fixture finished", ""

            def spawn(repo, tid):
                self.assertNotIn("--continue", pipeline.run_command(repo, tid))
                self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)

            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertEqual(len(started), 1)

    def test_tracker_restart_after_expiry_without_a_qualifying_reply_keeps_spent_repairs(self):
        _, asked = self.expired_question()
        self.local.comment(self.ticket, "An unrelated bot comment after expiry.")
        self.local.label(self.ticket, remove=["needs-info"])
        started = []

        def work(cfg, run, slot):
            started.append(run.id)
            self.assertFalse(pipeline.answer_text(asked.id))
            self.assertEqual(run.budget.prior_repairs, 3)
            with self.assertRaisesRegex(runlimits.Parked, "3 repair cycles"):
                run.budget.charge("repair", "review_repair")
            return "noop", "fixture finished", ""

        def spawn(repo, tid):
            self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(len(started), 1)
class PrAnswerFixture(PollFixture):
    def prepare_pr_fixture(self, checkouts=()):
        return factory.Outcome("pass", data={
            "layer": str(self.repo.layer_dir), "checkouts": [c.to_row() for c in checkouts],
            "ticket": asdict(self.local.get(self.ticket)), "branch": "fixture", "add_dirs": [],
            "attachments": "", "expertise": "", "triage": "",
        })

    def open_pr_ask(self, asked, kind="text", *, step="orchestrate", session=None):
        asked_at = (util.now() - timedelta(minutes=pipeline.FEEDBACK_QUIET_MINUTES + 2)).replace(microsecond=0)
        question = {"kind": kind, "header": "CI log", "question": "Where is the CI log?"}
        if kind == "secret":
            question["name"] = "CI_LOG_TOKEN"
        with patch.object(util, "now", return_value=asked_at):
            ask_store.ask_add("ask-fixture", asked.id, self.repo.name, self.ticket, step, step, "", "",
                              [question], asdict(session) if session else {})
        record.settle(asked.id, "needs-info")
        self.local.label(self.ticket, add=["needs-info"])
        return asked_at

    def add_pr_comment(self, pr, comment, created, gh_id=1):
        insights_store.add_feedback([{"pr_url": pr, "kind": "comment", "gh_id": gh_id, "repo": self.repo.name,
                                      "issue": self.ticket, "body": comment, "created": util.iso(created), "at_once": 0,
                                      "pr_state": "OPEN", "status": "new"}])

    def add_issue_reply(self, text):
        with principals.acting(principals.seed_owner("fixture@example.test")):
            tasks_store.task_comment(self.ticket, principals.current(), text)

    def poll_continuation(self, asked, path):
        exit_codes = []

        def spawn(repo, tid):
            self.assertEqual(pipeline.run_command(repo, tid),
                             pipeline.script_command(self.cfg, path, repo, str(tid), "--continue", asked.id))
            script = pipeline.Script(path, pipeline.flow_function(self.cfg, path), continues=asked.id)
            exit_codes.append(self.run_polled_ticket(repo, tid, lambda *_: self.fail("started an ordinary run"), script))

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        revision = run_store.newest_whole_run(self.repo.name, self.ticket)
        self.assertEqual(exit_codes, [0], revision["summary"])
        return revision

    def assert_feedback_done(self, pr, comments):
        rows = insights_store.feedback_between("", "9999")
        self.assertCountEqual([(row["body"], row["status"]) for row in rows if row["pr_url"] == pr],
                              [(body, "done") for body in comments])


class OrchestratedFeedbackAnswerTests(PrAnswerFixture):
    def waiting_pr_ask(self, kind="text", *, repair=False):
        self.cfg.replay = config.ReplayConfig(all=True)
        self.repo.layer_dir.mkdir()
        pr = "https://github.com/owner/project/pull/1"
        published = self.made_run()
        starting = published
        checkouts = []
        if repair:
            run_lines.write(published.id, run_lines.END, review={"verdict": "request_changes", "problems": [
                {"file": "notes.txt", "text": "Fix the saved finding.", "category": "issue"}]})
            (published.dir / "plan.md").write_text("Implement the issue.")
            record.settle(published.id, "pr", pr_url=pr, branch="fixture")
            starting = self.made_run(published.id)
            starting.repairs = published.id
            run_lines.write(starting.id, "repair", source=published.id)
            wt = paths.WORKTREES_DIR / f"{self.repo.name}-{self.ticket}"
            (wt / ".git").mkdir(parents=True)
            checkouts.append(workspace.Checkout("", "owner/project", self.repo.path, wt, "main", pr, "fixture-head"))
        starting.path = "orchestrate"
        controller = orchestration.Controller(flows.start(self.cfg, starting, {}))
        prepared = self.prepare_pr_fixture(checkouts)

        def prepare_workspace(*args, **kwargs):
            controller.job.take("prepare", prepared, stored=False)
            return prepared

        with (patch.object(controller, "atomic", side_effect=prepare_workspace),
              patch.object(pipeline, "workspace_note", return_value="Fixture workspace"),
              patch.object(repairs, "matched_head", return_value="fixture-head")):
            self.assertIsNone(controller.prepare())
        if repair:
            asked, lease = starting, controller.lease
        else:
            record.settle(published.id, "pr", pr_url=pr)
            asked = self.made_run(published.id)
            lease = ledger.acquire(asked.id, published.id, {})
        turn = ledger.begin(lease, {}, 10)
        action, = ledger.accept(lease, turn, ledger.snapshot(lease)["version"],
                                {"calls": [{"op": "owner.ask", "args": {}}]}, 10)
        ledger.transition(lease, action["id"], "succeeded", {"ask_ids": ["ask-fixture"], "awaiting_owner": True})
        return asked, pr, self.open_pr_ask(asked, kind)

    def check_polled_pr_answer(self, issue_reply="", *, stale_feedback=False, repair=False):
        asked, pr, asked_at = self.waiting_pr_ask(repair=repair)
        comments = []
        if stale_feedback:
            for gh_id, offset in enumerate((-1, 0), start=2):
                stale = f"Please rename button {gh_id}."
                self.add_pr_comment(pr, stale, asked_at + timedelta(seconds=offset), gh_id=gh_id)
                comments.append(stale)
        comment = "The lint job has the log. Please fix the failing check."
        self.add_pr_comment(pr, comment, asked_at + timedelta(seconds=1))
        comments.append(comment)
        if issue_reply:
            self.add_issue_reply(issue_reply)
        expected = issue_reply or comment

        def decide(run, turn, request):
            self.assertIn(f"Answer: {expected}", request["prompt"])
            for body in comments:
                self.assertIn(body, request["prompt"])
            if repair:
                self.assertTrue(run.repairs)
                self.assertEqual(run.repairs, pipeline.repaired_by(asked.id))
            found = ledger.find(run.id)
            response = {"version": PROTOCOL, "decision_id": turn,
                        "state_version": found["version"], "rationale": "Fixture reviewed owner input.",
                        "calls": [{"op": "run.finish", "args": {"reason": "noop"}}]}
            return SimpleNamespace(pid=0, identity=None, proc=SimpleNamespace(poll=lambda: 0),
                                   receive=lambda: [{"ok": True, "structured": response}], close=lambda: None)

        with (patch.object(delegation, "Worker", side_effect=decide) as worker,
              patch.object(pipeline, "workspace_note", return_value="Fixture workspace"),
              patch.object(flows.Job, "reclaim", return_value=""),
              patch.object(workspace, "kept_work", return_value=""),
              patch.object(repairs, "matched_head", return_value="fixture-head"),
              patch.object(pipeline, "stage", return_value=pipeline.Staged()),
              patch.object(pipeline, "nothing_to_build", return_value=("pr", "fixture finished", pr)),
              patch.object(gh, "pr_view", return_value={"state": "OPEN"}),
              patch.object(gh, "pr_body", return_value="")):
            revision = self.poll_continuation(asked, "orchestrate")
        worker.assert_called_once()
        ask = ask_store.ask_get("ask-fixture")
        self.assertEqual((ask["taken_by"], ask["answers"][0]["text"]), (revision["id"], expected))
        data = ledger.find(revision["id"])["data"]
        self.assertEqual([(row["pr_url"], row["body"]) for row in data["feedback"]], [(pr, body) for body in comments])
        self.assertIn(f"Answer: {expected}", data["job"]["goal"])
        for body in comments:
            self.assertIn(body, data["goal_base"])
        self.assertTrue(any(line.get("op") == "owner.ask" and line.get("recovered") and line.get("source") == asked.id
                            for line in run_lines.read(revision["id"])))
        self.assert_feedback_done(pr, comments)

    def test_poll_uses_pr_feedback_as_answer_and_recovers_orchestrated_revision(self):
        self.check_polled_pr_answer()

    def test_poll_leaves_an_ask_open_when_only_earlier_or_equal_second_feedback_is_due(self):
        asked, pr, asked_at = self.waiting_pr_ask()
        earlier, equal = "Please rename the button.", "Please move the icon."
        self.add_pr_comment(pr, earlier, asked_at - timedelta(seconds=1))
        self.add_pr_comment(pr, equal, asked_at, gh_id=2)
        started = []
        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertEqual(started, [])
        self.assertEqual(ask_store.asks_of(asked.id)[0]["status"], "open")
        pending = pipeline.pending_feedback(self.repo.name, self.ticket)
        self.assertEqual([(row["body"], row["status"]) for row in pending], [(earlier, "new"), (equal, "new")])
        name = tracker.ticket_name(self.repo.name, self.local.ref(self.ticket))
        self.assertEqual(util.read_json(self.root / "skipped.json", {})[name]["why"],
                         "its asks are still waiting for an owner answer")

    def test_poll_keeps_earlier_and_equal_second_comments_as_revision_feedback(self):
        self.check_polled_pr_answer(stale_feedback=True)

    def test_poll_prefers_issue_reply_to_due_pr_feedback_as_answer(self):
        self.check_polled_pr_answer("The integration job has the log.")

    def test_poll_delivers_pr_feedback_when_an_orchestrated_repair_recovers_an_issue_answer(self):
        with (patch.object(gh, "pr_merge_details", return_value={"state": "OPEN", "baseRefName": "main"}),
              patch.object(workspace, "fetch", return_value=None),
              patch.object(pipeline, "merge_moved_bases", return_value=[])):
            self.check_polled_pr_answer("The integration job has the log.", repair=True)

    def test_poll_blocks_pr_feedback_when_an_open_ask_holds_a_secret(self):
        asked, pr, asked_at = self.waiting_pr_ask("secret")
        self.add_pr_comment(pr, "The lint job has the log.", asked_at + timedelta(minutes=1))
        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("launched with an open secret ask")), 0)
        self.assertEqual(ask_store.asks_of(asked.id)[0]["status"], "open")
        name = tracker.ticket_name(self.repo.name, self.local.ref(self.ticket))
        self.assertEqual(util.read_json(self.root / "skipped.json", {})[name]["why"], poll.SECRET_ASK_REPLY)


class AdwPrFixture(PrAnswerFixture):
    def configure_adw(self):
        self.cfg.replay = config.ReplayConfig(all=True)
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "adw"\n')

    def adw_pr_steps(self, prepared):
        return {
            "prepare": lambda *a, **kw: prepared,
            "checks": passed_fixture_checks,
            "verify": lambda flow, **kw: factory.Outcome("pass", data={
                "verified": {"verdict": "pass"}, "names": [], "policy": flow.ctx.policy("verify", kw)}),
            "learn": lambda *a, **kw: factory.Outcome("pass"),
            "pr": lambda *a, **kw: factory.Outcome("pass", data={
                "prs": {}, "opened": [], "changed": [], "pushed": [], "committed_lines": 1,
                "draft_why": "", "report_body": "", "summary": ""}),
            "publish": lambda *a, **kw: factory.Outcome("pass", data={"ending": "pr"}),
        }


class AdwRevisionFeedbackTests(AdwPrFixture):
    def check_adw_pr_answer(self, *, new_feedback, asking_step="review", legacy=False, only_review=False):
        self.configure_adw()
        pr = "https://github.com/owner/project/pull/1"
        published = self.made_run()
        run_lines.write(published.id, "execution", path="feature")
        record.settle(published.id, "pr", pr_url=pr)
        asked = self.made_run(published.id)
        asked.path = "revise"
        run_lines.write(asked.id, "execution", path="revise")
        original, fresh = "Please rename the button.", "Please also move the icon."
        self.add_pr_comment(pr, original, util.now() - timedelta(minutes=pipeline.FEEDBACK_QUIET_MINUTES + 3))
        asked.feedback = pipeline.pending_feedback(self.repo.name, self.ticket)
        insights_store.take_feedback(asked.feedback, asked.id)
        insights_store.settle_feedback(asked.id, "failed")
        prepared = self.prepare_pr_fixture()
        session = Session(id="kept-ask-session")
        expected = "The integration job has the log."

        def revise_step(flow, **kwargs):
            outcome = flows.revision(flow, **kwargs)
            if legacy and flow.run_id == asked.id:
                outcome.data.pop("feedback_keys", None)
            return outcome

        with (patch.object(factory, "fingerprint", return_value="fixture-tree"),
              patch.object(pipeline, "verification_trees", return_value={"": "fixture-tree"}),
              patch.object(pipeline, "workspace_note", return_value="Fixture workspace"),
              patch.object(pipeline, "stage", return_value=pipeline.Staged(counts={"notes.txt": 1})),
              patch.object(gh, "pr_body", return_value=""),
              patch.object(pipeline, "record_prs", return_value=pr),
              patch.object(pipeline, "release_workspace"), patch.object(flows, "documents", return_value=False),
              patch.object(pipeline.AgentRun, "agent",
                           return_value=Result(ok=True, structured={"summary": "Fixture", "problems": []})) as agent,
              patch.dict(factory.STEPS, {**self.adw_pr_steps(prepared), "revise": revise_step})):
            prior = flows.start(self.cfg, asked, {})
            tree = str(prior.ctx.ws)
            prior.ctx.ws.mkdir()
            flows.step(prior, "prepare", tree=tree)
            if asking_step == "review":
                flows.step(prior, "revise", tree=tree)
                flows.step(prior, "checks", tree=tree, attempt=1, tries=asked.settings("check_repair").tries)
                flows.step(prior, "verify", tree=tree)
            run_lines.write(asked.id, "step_end", step=asking_step, result="ask")
            asked_at = self.open_pr_ask(asked, step=asking_step, session=session)
            if new_feedback:
                self.add_pr_comment(pr, fresh, asked_at + timedelta(seconds=1), gh_id=2)
            agent.reset_mock()
            if only_review:
                with (patch.object(config, "load", return_value=self.cfg),
                      patch.object(pipeline, "has_origin", return_value=True),
                      self.assertRaises(SystemExit) as result):
                    factory.main(pipeline.flow_function(self.cfg, "revise"),
                                 [self.repo.name, self.ticket, "--continue", asked.id, "--only", "review"])
                rerun = next(row for row in run_store.ticket_runs(self.repo.name, self.ticket) if row["only_step"])
                self.assertEqual(result.exception.code, 0, rerun["summary"])
            else:
                self.add_issue_reply(expected)
                revision = self.poll_continuation(asked, "revise")

        if only_review:
            self.assertEqual(rerun["summary"], "review pass")
            self.assertEqual([call.args[0] for call in agent.call_args_list], ["review"])
            self.assertNotIn(fresh, agent.call_args.args[1])
            pending = pipeline.pending_feedback(self.repo.name, self.ticket)
            self.assertEqual([(row["body"], row["status"], row["run_id"]) for row in pending],
                             [(original, "failed", asked.id), (fresh, "new", None)])
            return

        calls = {call.args[0]: call for call in agent.call_args_list}
        self.assertEqual([call.args[0] for call in agent.call_args_list],
                         ["revise", "review"] if new_feedback else ["review"])
        if new_feedback:
            self.assertIn(fresh, calls["revise"].args[1])
        self.assertEqual(calls[asking_step].kwargs["resume"], session)
        self.assertIn(expected, calls[asking_step].args[1])
        self.assertEqual(ask_store.ask_get("ask-fixture")["taken_by"], revision["id"])
        self.assert_feedback_done(pr, [original, fresh] if new_feedback else [original])

    def test_poll_reruns_saved_adw_revision_for_new_pr_feedback_before_resuming_review(self):
        self.check_adw_pr_answer(new_feedback=True)

    def test_poll_reuses_saved_adw_revision_when_only_the_issue_answer_is_new(self):
        self.check_adw_pr_answer(new_feedback=False, legacy=True)

    def test_poll_delivers_new_pr_feedback_when_resuming_the_adw_revision_agent(self):
        self.check_adw_pr_answer(new_feedback=True, asking_step="revise")

    def test_isolated_adw_review_reuses_saved_revision_and_leaves_new_feedback_pending(self):
        self.check_adw_pr_answer(new_feedback=True, only_review=True, legacy=True)


class AdwRepairContinuationTests(AdwPrFixture):
    def check_polled_adw_repair(self, session, asking_step="repair"):
        self.configure_adw()
        pr = "https://github.com/owner/project/pull/1"
        published = self.made_run()
        run_lines.write(published.id, run_lines.END, review={"verdict": "request_changes", "problems": [
            {"file": "notes.txt", "text": "Fix the saved finding.", "category": "issue",
             "acceptance": "Inspect notes.txt and confirm the requested wording was corrected.",
             "verification": "review", "verification_reason": "This finding is about the wording of the notes."}]})
        (published.dir / "plan.md").write_text("Implement the issue.")
        record.settle(published.id, "pr", pr_url=pr, branch="fixture")
        asked = self.made_run(published.id)
        asked.path, asked.repairs = "repair", published.id
        run_lines.write(asked.id, "execution", path="repair")
        run_lines.write(asked.id, "repair", source=published.id)
        prior = flows.start(self.cfg, asked, {})
        checkout = workspace.Checkout("", "owner/project", self.repo.path, prior.ctx.ws, "main", pr, "fixture-head")
        (prior.ctx.ws / ".git").mkdir(parents=True)
        comment, reply = "Please move the icon.", "The integration job has the log."
        repairs_called = []

        def worker(prompt, cwd, *, trace, **kw):
            if trace[0] != asked.id:
                self.assertNotIn(comment, prompt)
                if trace[1] == asking_step:
                    self.assertIn(f"Answer: {reply}", prompt)
                    self.assertEqual(kw["resume"], session)
                    if session.id:
                        fresh_prompt = kw["fresh_prompt"]("codex")
                        self.assertIn(f"Answer: {reply}", fresh_prompt)
                        self.assertNotIn(comment, fresh_prompt)
                if trace[1] == "repair":
                    repairs_called.append(trace[0])
            return Result(ok=True, text="Fixture repaired.", structured={"summary": "Fixture", "problems": []})

        with (patch.object(factory, "fingerprint", return_value="fixture-tree"),
              patch.object(pipeline, "workspace_note", return_value="Fixture workspace"),
              patch.object(pipeline, "stage", return_value=pipeline.Staged(counts={"notes.txt": 1})),
              patch.object(pipeline, "index_trees", return_value={}),
              patch.object(pipeline, "record_prs", return_value=pr),
              patch.object(pipeline, "release_workspace"), patch.object(flows, "documents", return_value=False),
              patch.object(workspace, "kept_work", return_value=""), patch.object(flows.Job, "reclaim", return_value=""),
              patch.object(repairs, "matched_head", return_value="fixture-head"),
              patch.object(pipeline.runner, "run", side_effect=worker),
              patch.dict(factory.STEPS, self.adw_pr_steps(self.prepare_pr_fixture([checkout])))):
            tree = str(prior.ctx.ws)
            flows.step(prior, "prepare", tree=tree)
            if asking_step == "review":
                self.assertTrue(flows.step(prior, "repair", tree=tree).passed)
                flows.step(prior, "checks", tree=tree, attempt=1, tries=asked.settings("check_repair").tries)
                flows.step(prior, "verify", tree=tree)
            run_lines.write(asked.id, "step_end", step=asking_step, result="ask")
            asked_at = self.open_pr_ask(asked, step=asking_step, session=session)
            self.add_pr_comment(pr, comment, asked_at + timedelta(seconds=1))
            self.add_issue_reply(reply)
            with patch.object(flows, "start", wraps=flows.start) as start:
                repair = self.poll_continuation(asked, "repair")

        self.assertEqual(start.call_args.args[1].feedback, [])
        self.assertEqual(repairs_called, [repair["id"]] if asking_step == "repair" else [])
        self.assertEqual(ask_store.ask_get("ask-fixture")["taken_by"], repair["id"])
        pending = pipeline.pending_feedback(self.repo.name, self.ticket)
        self.assertEqual([(row["body"], row["status"], row["run_id"]) for row in pending], [(comment, "new", None)])

    def test_poll_delivers_only_the_issue_answer_to_resumed_adw_repair_agent(self):
        self.check_polled_adw_repair(Session(id="kept-repair-session", runner="codex"))

    def test_poll_delivers_only_the_issue_answer_to_fresh_adw_repair_agent(self):
        self.check_polled_adw_repair(Session())

    def test_poll_reuses_saved_adw_repair_when_pr_feedback_is_pending(self):
        self.check_polled_adw_repair(Session(id="kept-review-session", runner="codex"), asking_step="review")


class ReadmissionFixture(PollFixture):
    def park_ticket(self, branch=""):
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n')
        run = self.made_run()
        run.budget.charge("review_fix1", "review_repair")
        record.note(run.id, branch=branch)
        run.budget.park("repair limit")
        record.settle(run.id, "failed")
        self.local.label(self.ticket, add=["failed"])
        self.cfg.replay = config.ReplayConfig(all=True)
        return run


class ParkedRetryBudgetTests(Isolated):
    def setUp(self):
        super().setUp()
        self.layer = self.repo.layer_dir / "config.toml"
        self.layer.parent.mkdir()
        self.layer.write_text('[factory]\npath = "classic"\n[limits]\nmax_repair_cycles = 3\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        self.patches.enter_context(patch.object(config, "load", return_value=self.cfg))
        self.patches.enter_context(patch.object(pipeline, "run_refusal", return_value=(False, "")))

    def park_spent_ticket(self, branch=""):
        def spend(cfg, run, slot):
            record.note(run.id, branch=branch)
            for _ in range(run.budget.policy.max_repair_cycles + 1):
                run.budget.charge("repair", "review_repair")

        with patch.object(pipeline, "_run", side_effect=spend):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket), 1)
        parked, saved = runlimits.latest_park(self.repo.name, self.ticket)
        self.assertEqual(saved["reason"], "the repair chain used its 3 repair cycles")
        self.assertEqual(runlimits.repair_count(parked["id"]), 3)

    def assert_fresh_retry(self):
        def repair(cfg, run, slot):
            run.budget.charge("repair", "review_repair")
            return "noop", "fixture finished", ""

        with patch.object(pipeline, "_run", side_effect=repair):
            result = pipeline.run_issue(self.repo.name, self.ticket, force=True)
        retry = run_store.newest_whole_run(self.repo.name, self.ticket)
        self.assertEqual(result, 0, retry["summary"])
        self.assertEqual(runlimits.repair_count(retry["id"]), 1)
        log = (paths.RUNS_DIR / retry["id"] / "run.log").read_text()
        self.assertIn("budget chain: no parents\n", log)

    def test_explicit_retry_after_published_head_changes_resets_spent_repairs(self):
        with patch.object(runlimits, "branch_head", return_value="a" * 40) as head:
            self.park_spent_ticket("arete/fixture")
            head.return_value = "b" * 40
            self.assert_fresh_retry()

    def test_explicit_retry_after_policy_changes_resets_spent_repairs(self):
        self.park_spent_ticket()
        self.layer.write_text(self.layer.read_text() + "max_run_minutes = 90\n")
        self.assert_fresh_retry()


class RepairWorkflowFixture(unittest.TestCase):
    pr_url = "https://github.com/fixture/repo/pull/1"
    branch = "fixture-branch"

    @classmethod
    def setUpClass(cls):
        root = Path(tempfile.mkdtemp(prefix="arete-denied-base-")).resolve()
        cls.addClassCleanup(shutil.rmtree, root, ignore_errors=True)
        with support.workspace_state(root):
            support.seed_db(root / "state")
            cls.workspace_db_seed = paths.DB_FILE.read_bytes()
        cls.base = root / "repo"
        cls.base.mkdir()
        (cls.base / "guarded.txt").write_text("base\n")
        (cls.base / "removed.txt").write_text("base\n")
        util.git(["init", "-q", "-b", "main", "--template="], cls.base)
        util.git(["add", "."], cls.base)
        util.git([*util.ARETE_IDENTITY, "commit", "-qm", "Base"], cls.base)

    def prepare_controller(self, max_repair_cycles=1, *, real_ledger=False):
        root = Path(tempfile.mkdtemp(prefix="arete-denied-")).resolve()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.enterContext(support.workspace_state(root))
        paths.ensure_dirs()
        paths.DB_FILE.write_bytes(self.workspace_db_seed)
        self.wt = root / "repo"
        shutil.copytree(self.base, self.wt)
        repo = config.Repo("fixture/repo", self.wt, tracker="local")
        self.layer = config.RepoLayer(repo.layer_dir, max_repair_cycles=max_repair_cycles,
                                      deny=["guarded.txt", "removed.txt", "blocked[[]1].txt"],
                                      checks={"goal": 'test "$(cat goal.txt)" = reached'})
        cfg = config.Config(replay=config.ReplayConfig(all=True))
        run_dir = paths.RUNS_DIR / "fixture"
        run_dir.mkdir(parents=True)
        self.run_fixture = pipeline.Run(id="fixture", repo=repo, dir=run_dir, tracker=tracker.LocalTracker(repo),
                                        tid="1", path="orchestrate")
        job = flows.Job(cfg, self.run_fixture, self.wt, ticket=tracker.Ticket("1", "#1", "Fixture goal"),
                        checkouts=[workspace.Checkout("", repo.name, repo.path, self.wt, "main")])
        self.run_fixture.workspace = self.wt, job.checkouts
        self.enterContext(patch.object(paths, "CHECK_LOCKS_DIR", root / "check-locks"))
        self.enterContext(patch.object(config, "load_layer", return_value=self.layer))
        self.enterContext(patch.multiple(record, mark=Mock(), note=Mock()))
        self.enterContext(patch.object(run_lines, "write"))
        self.enterContext(patch.object(run_store, "run_get", return_value={
            "measured_at": "fixture", "active_seconds": 0, "repair_calls": 0,
            "verify_seconds": 0, "verify_open_seconds": 0}))
        self.enterContext(patch.object(runlimits.Budget, "checkpoint"))
        self.enterContext(patch.object(self.run_fixture, "enter"))
        self.enterContext(patch.object(util, "flush_fd"))
        self.enterContext(patch.multiple(workspace, note_made=Mock(), kept_work=Mock(return_value=""),
                                         scratch_left=Mock(return_value=[]), drop_untracked_bytecode=Mock()))
        self.stored = self.enterContext(patch.object(vault, "every", return_value=[]))
        if not real_ledger:
            self.enterContext(patch.multiple(ledger, acquire=Mock(return_value="fixture"), actions=Mock(return_value=[]),
                                             save=Mock(), transition=Mock(), snapshot=Mock(return_value={"data": {
                                                 "protocol": PROTOCOL, "mode": "orchestrate", "acceptance": "fixture"}})))
        job.use_layer(self.layer)
        self.run_fixture.budget = runlimits.Budget(self.run_fixture, runlimits.Policy.of(self.layer), "")
        flow = SimpleNamespace(ctx=job, continues="", run=lambda name, _args, kw: factory.STEPS[name](flow, **kw))
        self.controller = orchestration.Controller(flow)
        self.controller.catalog = Catalog(job)

    def git(self, *args):
        return util.git(list(args), self.wt).strip()

    def write(self, name, text):
        target = self.wt / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message, *files, cwd=None):
        folder = cwd or self.wt
        util.git(["add", "--", *files], folder)
        util.git([*util.ARETE_IDENTITY, "commit", "-qm", message], folder)
        return util.git(["rev-parse", "HEAD"], folder).strip()

    def perform(self, op, **args):
        self.controller.executing = op
        return self.controller.perform({"id": op, "op": op, "request": OPERATIONS[op].arguments(args)})

    def worker(self, edit, prompt_id="delegate"):
        controller = self.controller

        def launch(run, action, request):
            expected = prompt_loader.code_list(diffguard.denied_paths(self.layer))
            self.assertIn(f"Denied paths: {expected}.", request["prompt"].splitlines())
            if prompt_id == "delegate":
                self.assertIn("Reach the fixture goal", request["prompt"])
            if feedback := controller.data.get("denied_feedback"):
                self.assertIn(feedback, request["prompt"])

            def messages():
                yield {"event": "launch", "step": request["step"], "role": request["role"], "route": {}}
                edit()
                yield {"event": "result", "ok": True, "text": "Edited the fixture"}

            return SimpleNamespace(pid=123, identity=456, receive=messages, allow=lambda _: None, close=lambda: None)

        with patch.object(delegation, "Worker", side_effect=launch) as worker:
            model = controller.catalog.settings["model:build:0"]
            self.perform("worker.start", goal="Reach the fixture goal", prompt_id=prompt_id,
                         route_id="model:build:0", effort=model.effort or "low")
            self.assertTrue(controller.collect())
            return worker.call_args.args[2]["role"]

class DeniedWorkerEditsTests(RepairWorkflowFixture):
    def test_each_pipeline_writing_prompt_renders_denied_paths(self):
        writing = {name for name, (_, writes) in PROMPTS.items() if writes}
        writing |= {"patch", "repair", "fix", "ci_fix", "review_fix", "verify_fix", "revise", "resolve",
                    "apply", "oncall_repair"}
        denied = "`guarded.txt`, `restricted/**`"
        for name in sorted(writing):
            with self.subTest(prompt=name):
                identifiers = prompt_loader.template(name).get_identifiers()
                inputs = dict.fromkeys(identifiers, "fixture") | {"denied": denied}
                rendered = prompt_loader.prompt(name, **inputs)
                self.assertIn(f"Denied paths: {denied}.", rendered.splitlines())

    def test_failed_test_runs_before_full_suite_on_the_final_candidate(self):
        self.prepare_controller(real_ledger=True)
        ctl = self.controller
        ctl.data["acceptance"] = "fixture"

        def check(**args):
            turn = ledger.begin(ctl.lease, {}, 100)
            action, = ledger.accept(ctl.lease, turn, ledger.snapshot(ctl.lease)["version"], {"calls": [{
                "op": "checks.run", "args": OPERATIONS["checks.run"].arguments(args)}]}, 100)
            self.assertIsNone(ctl.dispatch(action))
            return ledger.actions(ctl.lease)[-1]["result"]

        selector = "test_order.OrderTests.test_rollback"
        script = self.wt.parent / "check.py"
        script.write_text(
            "import sys\nfrom pathlib import Path\n"
            "if Path('goal.txt').read_text().strip() != 'fixed':\n"
            "    if sys.argv[1:] or Path('goal.txt').read_text().strip() != 'class only':\n"
            "        print('test_rollback (test_order.OrderTests.test_rollback)')\n"
            "    print('AssertionError: wrong order')\n"
            "    print('Failed classes: test_order.OrderTests')\n"
            "    print('Ran 3 tests in 0.04s: FAILED')\n"
            "    sys.exit(1)\n"
            "if sys.argv[1:] not in ([], ['test_order.OrderTests.test_rollback']): sys.exit(2)\n")
        self.layer.checks = {"lint": "true", "behavior": shlex.join([sys.executable, str(script)])}
        self.layer.targeted_commands = {"classes": self.layer.checks["behavior"]}

        self.write("goal.txt", "class only")
        failed = check()
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["remaining_checks"], [{"name": "behavior", "selectors": ["test_order.OrderTests"]}])
        self.assertFalse(failed["runner_blocked"])

        narrowed = check()
        self.assertEqual(narrowed["remaining_checks"], [{"name": "behavior", "selectors": [selector]}])
        reproduced = check()
        self.assertEqual((reproduced["scope"], reproduced["checks_executed"]), ("failed", ["behavior"]))
        repeated = check()
        self.assertEqual(repeated["reused_checks"], ledger.actions(ctl.lease)[-2]["id"])
        self.assertEqual(repeated["checks_executed"], [])
        ctl.recover()
        self.assertFalse(ctl.current("checks"))
        self.assertEqual(check()["reused_checks"], repeated["reused_checks"])

        self.write("goal.txt", "fixed")
        first = check()
        self.assertEqual((first["scope"], first["passed"], first["full"]), ("failed", True, False))
        self.assertEqual(first["checks_executed"], ["behavior"])
        self.assertIn(selector, first["report"])
        self.write("another.txt", "later integration")
        self.assertEqual(check()["scope"], "failed")

        final = check(scope="quick")
        self.assertEqual((final["scope"], final["passed"], final["full"]), ("full", True, True))
        self.assertEqual(final["checks_executed"], ["lint", "behavior"])
        self.assertEqual(final["remaining_checks"], [])

        producer = ctl.data["evidence"]["checks"]["producer"]
        self.assertEqual(check()["reused_checks"], producer)
        ctl.recover()
        self.assertTrue(ctl.current("checks"))
        self.assertEqual(check(scope="quick")["reused_checks"], producer)
        with patch.object(ctl.run, "action", producer):
            self.assertEqual(check()["checks_executed"], ["lint", "behavior"])
        ctl.data["acceptance"] = "owner confirmed the runner is ready"
        self.assertEqual(check()["checks_executed"], ["lint", "behavior"])
        ctl.data["ci_failed"] = True
        self.assertEqual(check()["checks_executed"], ["lint", "behavior"])
        ctl.data.pop("ci_failed")
        with patch.object(ctl.run, "environment_release", "new release"):
            self.assertEqual(check()["checks_executed"], ["lint", "behavior"])
        with patch.object(ctl.run, "path", "pilot"):
            self.assertEqual(check()["checks_executed"], ["lint", "behavior"])
        self.layer.checks["new"] = "true"
        self.assertEqual(check()["checks_executed"], ["lint", "behavior", "new"])
        self.write("another.txt", "changed again")
        self.assertEqual(check()["checks_executed"], ["lint", "behavior", "new"])

    def test_second_worker_passes_checks_without_the_denied_paths(self):
        self.prepare_controller()

        def first_edits():
            self.write("guarded.txt", "worker edit\n")
            (self.wt / "removed.txt").unlink()
            self.write("blocked[1].txt", "worker addition\n")
            self.write("blocked1.txt", "allowed addition\n")

        self.assertEqual(self.worker(first_edits, prompt_id="build"), "build")
        failed = self.perform("checks.run", scope="quick")
        self.assertFalse(failed["passed"])
        self.assertIn("Reach the goal without them", failed["report"])
        for name in ("guarded.txt", "removed.txt", "blocked[1].txt"):
            self.assertIn(name, failed["report"])
        self.assertFalse(self.controller.current("checks"))
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "blocked1.txt")
        for name in ("guarded.txt", "removed.txt"):
            self.assertEqual((self.wt / name).read_text(), "base\n")
        self.assertFalse((self.wt / "blocked[1].txt").exists())
        repeated = self.perform("checks.run")
        self.assertFalse(repeated["passed"])
        self.assertEqual(repeated["report"], failed["report"])
        self.assertEqual(self.run_fixture.budget.repair_calls, 0)
        self.assertEqual(self.worker(lambda: self.write("goal.txt", "reached\n")), "check_repair")
        self.assertEqual(self.run_fixture.budget.repair_calls, 1)
        passed = self.perform("checks.run")
        self.assertTrue(passed["passed"], passed["report"])
        self.assertTrue(passed["full"])
        self.assertIn("goal passed", passed["report"])
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "blocked1.txt\ngoal.txt")

    def test_denied_edit_on_last_repair_parks_with_the_path(self):
        self.prepare_controller()
        self.controller.data["writes"] = 1
        self.assertEqual(self.worker(lambda: self.write("guarded.txt", "last attempt\n")), "check_repair")
        with self.assertRaises(runlimits.Parked) as parked:
            self.perform("checks.run")
        status, _, _ = pipeline.park_run(self.run_fixture, str(parked.exception))
        self.assertEqual(status, "failed")
        reason = runlimits.metadata(self.run_fixture.id, "parked.json")["reason"]
        self.assertIn("the repair chain used its 1 repair cycles", reason)
        self.assertIn("guarded.txt", reason)
        self.assertEqual((self.wt / "guarded.txt").read_text(), "base\n")
        self.assertEqual(self.run_fixture.budget.repair_calls, 1)

    def test_staged_secret_fails_with_the_existing_message(self):
        self.prepare_controller()
        sample = "fixture-vault-value-728"
        self.stored.return_value = [("FIXTURE_TOKEN", sample)]
        self.assert_fatal_worker_edit(lambda: self.write("payload.txt", sample + "\n"),
                                      "payload.txt (holds the stored value of FIXTURE_TOKEN)")
        self.assertFalse((self.wt / "payload.txt").exists())
        pipeline.keep_edits(self.run_fixture)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("diff", "HEAD^", "HEAD", "--name-only"), "goal.txt")

    def test_workflow_edit_mixed_with_a_layer_denied_edit_is_fatal(self):
        self.prepare_controller()

        def edit():
            self.write("guarded.txt", "worker edit\n")
            self.write(".github/workflows/fixture.yml", "name: fixture\n")

        self.assert_fatal_worker_edit(edit, ".github/workflows/fixture.yml, guarded.txt")
        self.assertFalse((self.wt / ".github/workflows/fixture.yml").exists())
        self.assertEqual((self.wt / "guarded.txt").read_text(), "base\n")

    def assert_fatal_worker_edit(self, edit, paths_in_message):
        edit()
        self.write("goal.txt", "reached\n")
        with self.assertRaises(workspace.RunFailed) as failed:
            self.perform("checks.run")
        self.assertEqual(failed.exception.why, f"the build and its fixes touched denied paths: {paths_in_message}")
        self.assertEqual(self.run_fixture.budget.repair_calls, 0)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "goal.txt")


class FocusedRollbackTests(RepairWorkflowFixture):
    def test_settings_rollback_regression_returns_to_repair_until_current_pass(self):
        self.prepare_controller(max_repair_cycles=2)
        self.layer.verify = config.Verify(url="")
        self.layer.targeted_commands.update(classes=f"{shlex.quote(sys.executable)} -B -m unittest", unrelated=":")
        test_class = "rollback_probe.SettingsRollbackTests.test_immediate_read"
        settings = """import unittest

class Settings:
    def __init__(self):
        self.value, self.history = 'blue', [(1, 'blue')]
    def rotate(self):
        self.value = 'green'
        self.history.append((2, self.value))
    def rollback(self):
        self.history.pop()

class SettingsRollbackTests(unittest.TestCase):
    def test_immediate_read(self):
        settings = Settings()
        settings.rotate()
        self.assertEqual((settings.value, settings.history[-1]), ('green', (2, 'green')))
        settings.rollback()
        self.assertEqual((settings.value, settings.history), ('blue', [(1, 'blue')]),
                         f'expected blue and only revision 1, got {settings.value}, {settings.history}')
"""
        self.write("goal.txt", "reached\n")
        self.write("rollback_probe.py", settings)
        self.assertTrue(self.perform("checks.run")["full"])
        acceptance = "Rotate blue to green, roll back and immediately read blue with only revision 1."

        def operate(op, role="review"):
            route = f"model:{role}:0"
            return self.perform(op, route_id=route, effort=self.controller.catalog.settings[route].effort or "low")

        def focused():
            return self.perform("checks.run", scope="focused")["receipts"][key]

        def receipts():
            return [c.kwargs for c in record.mark.call_args_list if c.args[1] == "focused_check"]

        review = Mock(return_value=Result(True, structured={"problems": [{
            "file": "rollback_probe.py", "problem": "Rollback leaves the cached settings value green.",
            "severity": "blocker", "category": "regression", "acceptance": acceptance, "verification": "check",
            "check": "obsolete"}]}))
        selectors = iter([{"check": "unrelated"}, {"check": "classes", "test_class": "obsolete.Test"},
                          {"check": "classes", "test_class": test_class}])

        def agent(step, text, cwd, **kwargs):
            if not step.startswith("review_selectors"):
                return review(step, text, cwd, **kwargs)
            self.assertTrue(kwargs["read_only"])
            self.assertIn(acceptance, text)
            return Result(True, structured={"selectors": [dict(id=key, verification="check", **next(selectors))]})

        with patch.object(self.run_fixture, "agent", side_effect=agent):
            self.assertEqual((first := operate("review.run"))["verdict"], "request_changes")
            key = first["problems"][0]["finding"]
            b = self.controller.job.b
            green = focused()
            self.assertEqual((green["outcome"], green["exit"], green["on_reviewed_tree"]), ("not reproduced", 0, True))
            self.assertNotIn(key, b.repair_baselines)
            with self.assertRaisesRegex(ValueError, "current focused repair evidence"):
                operate("review.run")
            self.assertEqual(b.capture_repair_baselines("before-worker"), f"{key} (fail)")
            _, obsolete, red = receipts()
            self.assertEqual((obsolete["outcome"], obsolete["exit"], obsolete["on_reviewed_tree"]),
                             ("selector unavailable", 1, True))
            self.assertEqual((red["exit"], red["outcome"], red["on_reviewed_tree"]), (1, "fail", True))
            self.assertIn("expected blue and only revision 1, got green, [(1, 'blue')]", red["report"])
            self.assertEqual((red["check"], red["test_class"]), ("classes", test_class))
            fixed = settings.replace("self.history.pop()", "self.history.pop()\n        self.value = self.history[-1][1]")
            for change, expected in ((settings + "\n# Incomplete repair.\n", "fail"), (fixed, "pass")):
                self.assertEqual(self.worker(lambda: self.write("rollback_probe.py", change)), "review_repair")
                self.assertEqual(focused()["outcome"], expected)
                if expected == "fail":
                    with self.assertRaisesRegex(ValueError, "current focused repair evidence"):
                        operate("review.run")
            self.write("rollback_probe.py", fixed + "\n# Integrated candidate changed.\n")
            with self.assertRaisesRegex(ValueError, "stale"):
                operate("review.run")
            review.assert_called_once()
            fresh = focused()
            self.assertEqual((fresh["exit"], fresh["outcome"]), (0, "pass"))
            self.assertEqual(len({r["tree"][""] for r in receipts()}), 4)
            self.assertTrue(all(r["finding"] == key and r["command_sha"] == red["command_sha"]
                                and r["after_tree"] == r["tree"] for r in receipts()[2:]))
            self.assertEqual(b.repair_baselines[key], red)
            with self.assertRaisesRegex(ValueError, "checks, review, verify"):
                self.perform("publication.request")
            self.assertTrue(self.perform("checks.run")["full"])
            review.return_value = Result(True, structured={"problems": [], "earlier": [{"id": "B1", "status": "fixed"}]})
            self.assertEqual(operate("review.run")["verdict"], "approve")
            with self.assertRaisesRegex(ValueError, "publication needs current evidence: verify"):
                self.perform("publication.request")
            with patch.object(pipeline, "check_in_use", return_value=pipeline.verify.Verified("pass")) as verifier:
                operate("verification.run", "verify")
            self.assertEqual(pipeline.verification_trees(verifier.call_args.args[0]), fresh["tree"])
            self.assertTrue(all(self.controller.current(gate) for gate in ("checks", "review", "verify")))

        self.assertEqual(receipts()[-1], flows.saved_repair_state(self.run_fixture.id)["receipts"][key])


class RepairVerificationSelectionTests(RepairWorkflowFixture):
    def test_focused_repair_only_runs_approved_names_and_exposes_alternate_evidence(self):
        self.prepare_controller()
        self.layer.targeted_commands["classes"] = f"{shlex.quote(sys.executable)} -B -m unittest"
        acceptance = "Reload the view and observe the saved value."
        unapproved_name = "echo unapproved"
        invalid_class = "fixture.Test; echo unapproved"
        findings = [
            {"problem": "Immediate read shows an old value.", "verification": "check", "check": unapproved_name,
                 "acceptance": "Read the saved value after rollback."},
            {"problem": "After a second rollback the revision is wrong.", "verification": "check", "check": "classes",
                 "test_class": invalid_class},
            {"problem": "Live view keeps an obsolete value.", "verification": "in_use",
                 "verification_reason": "Needs the deployed view.", "acceptance": acceptance},
        ]
        review = pipeline.Review.judged({"problems": [dict(p, severity="blocker") for p in findings]}, [], [], {})
        b = self.controller.job.b
        b.carry_review(review, "review-receipt")

        def select_regressions(step, _prompt, where, *, role, read_only, **_kw):
            self.assertEqual((step, where, role, read_only), ("review_selectors", self.wt, "review", True))
            return Result(True, structured={"selectors": [
                {"id": "review-receipt:B1", "verification": "check", "check": unapproved_name},
                {"id": "review-receipt:B2", "verification": "check", "check": "classes", "test_class": invalid_class},
            ]})

        with (patch.object(self.run_fixture, "agent", side_effect=select_regressions) as selector,
              patch.object(pipeline, "run_checks", side_effect=AssertionError("An unapproved check ran"))):
            focused = self.perform("checks.run", scope="focused")
        selector.assert_called_once()
        self.assertFalse(focused["passed"])
        self.assertEqual({key: receipt["outcome"] for key, receipt in focused["receipts"].items()},
                         {"review-receipt:B1": "blocked", "review-receipt:B2": "blocked"})
        self.assertFalse(b.repair_baselines)
        key = "review-receipt:B3"
        for expected in ("Requires independent in use: Needs the deployed view.", acceptance):
            self.assertIn(expected, b.repair_feedback())
        self.assertEqual(b.in_use_repair_hold(pipeline.verify.Verified("unverified")), key)
        self.layer.verify = config.Verify(url="")
        self.write("revision.txt", "1 blue\n")

        def observe(_step, prompt, where, *, role, **_kw):
            self.assertEqual(role, "verify")
            self.assertIn(f"{key}: {acceptance}", prompt)
            if (path := where / "revision.txt").read_text() == "2 green\n":
                path.write_text("1 blue\n")
            saw = path.read_text().strip()
            return Result(True, runner="pi", structured={"verdict": "pass", "checks": [
                {"what": key, "how": "Read revision.txt", "saw": saw, "passed": saw == "1 blue"}]})

        with (patch.object(self.run_fixture.budget, "verifying", side_effect=contextlib.nullcontext),
              patch.object(scratch, "make", return_value=None), patch.object(paths, "dashboard_ports", return_value=set()),
              patch.object(self.run_fixture, "agent", side_effect=observe)):
            verified = pipeline.check_in_use(b, "Verify the revision", "Repaired the read")
            self.assertEqual((verified.native_trees, verified.repair_observations), ({}, {key: acceptance}))
            self.assertEqual(b.in_use_repair_hold(pipeline.verify.Verified.stored(asdict(verified))), "")
            self.write("revision.txt", "2 green\n")
            self.assertEqual(b.in_use_repair_hold(verified), key)
            edited = pipeline.check_in_use(b, "Verify the revision", "Edited during verification")
            self.assertEqual((edited.verdict, edited.repair_observations, b.in_use_repair_hold(edited)), ("pass", {}, key))


class HeldRepairDraftFixture(RepairWorkflowFixture):
    rollback = ('saw=$(cat revision.txt) || exit 2; echo "saw $saw"; '
                'test "$saw" = "1 blue" || { echo ARETE_REGRESSION_FAILED; exit 1; }')
    acceptance = "Read revision 1 with value blue."
    blocking_problem = {"file": "revision.txt", "problem": "Rollback leaves the wrong revision.", "severity": "blocker",
                        "category": "regression", "verification": "check", "check": "rollback", "acceptance": acceptance}

    def prepare_settings(self, *, max_repair_cycles=1, revision="1 blue"):
        self.prepare_controller(max_repair_cycles)
        self.layer.targeted_commands["rollback"] = self.rollback
        self.write("goal.txt", "reached\n")
        self.write("revision.txt", revision + "\n")
        return self.controller.job

    def prepare_draft_publication(self, *, existing_pr=False, focused=False, sibling=False):
        job = self.prepare_settings(revision="2 green" if focused or existing_pr else "1 blue")
        if sibling:
            path = self.wt.with_name("sibling")
            shutil.copytree(self.base, path)
            shutil.copyfile(self.wt / "revision.txt", path / "revision.txt")
            job.checkouts.append(workspace.Checkout("sibling", "fixture/sibling", path, path, "main"))
        job.run.repo.parts = [config.Part(c.dir, c.github) for c in job.checkouts]
        job.branch = self.branch
        self.enterContext(patch.object(record, "metered_spent", return_value=0.0))
        create = Mock(return_value=self.pr_url, side_effect=AssertionError("Unexpected new PR") if existing_pr else None)
        self.enterContext(patch.multiple(gh, gh=Mock(side_effect=AssertionError("Unexpected GitHub call")),
                                         pr_view=Mock(return_value={"state": "OPEN", "headRefName": job.branch}),
                                         open_pr_url=Mock(return_value=""), pr_create=create,
                                         pr_body=Mock(return_value=""), pr_edit_body=Mock()))
        self.enterContext(patch.object(pipeline, "sync_pr_bodies", return_value=[]))
        for c in job.checkouts:
            self.prepare_branch(c, f"https://github.com/{c.github}/pull/1" if existing_pr else "")
        self.enterContext(patch.object(workspace, "remote_head", side_effect=lambda src, _: next(
            c.lease for c in job.checkouts if c.src == src)))
        if sibling:
            job.b.from_base = True
        if focused:
            self.exhaust_focused_review(job)
        return job

    def observe_file(self, b, file, what, expected):
        trees, saw = pipeline.verification_trees(b), (self.wt / file).read_text().strip()
        check = {"what": what, "how": f"Read {file}", "saw": saw, "passed": saw == expected}
        return pipeline.verify.Verified("pass", native_trees=trees, observed_trees=trees, checks=[check])

    def prepare_branch(self, c, pr="", *, published=("revision.txt",)):
        util.git(["update-ref", f"refs/remotes/{c.origin_base}", "HEAD"], c.wt)
        util.git(["switch", "-qc", self.branch], c.wt)
        if pr:
            c.pr = pr
            c.note_pushed_head(self.commit(f"Published fixture\n\nArete-Run: {self.run_fixture.id}", *published, cwd=c.wt))
            util.git(["update-ref", f"refs/remotes/origin/{self.branch}", c.lease], c.wt)

    def exhaust_focused_review(self, job):
        self.assertTrue(self.perform("checks.run")["full"])
        self.controller.stamp("verify", asdict(job.verified))
        with patch.object(job.run, "agent", return_value=Result(True, structured={"problems": [self.blocking_problem]})):
            self.assertFalse(self.controller.atomic("review", attempt=1, tries=1).passed)
        self.assertTrue(job.review_exhausted)
        self.controller.stamp("review", job.review.asdict())

    def publish_fixture(self, controller=False):
        ctl = self.controller
        if controller:
            with patch.multiple(ctl, publication_ci=Mock(return_value=None), finish_publication=lambda: ctl.data["publication"]):
                return self.perform("publication.request")
        self.assertTrue((outcome := flows.pr(ctl.flow, tree=str(self.wt))).passed)
        return outcome.data

    @contextlib.contextmanager
    def draft_actions(self, states, *, mock_build_push=True):
        actions = Mock()
        actions.read.side_effect = states
        actions.commit.side_effect = lambda *_, check_targets, **_kw: check_targets()
        push = patch.object(pipeline.Build, "push", actions.push) if mock_build_push else contextlib.nullcontext()
        with (push, patch.multiple(gh, pr_draft_and_head=actions.read, pr_set_draft=actions.draft),
              patch.object(pipeline, "commit_and_push", actions.commit)):
            yield actions


class HeldRepairDraftTests(HeldRepairDraftFixture):
    def test_scripted_verification_repair_reaches_review_and_discloses_the_block_in_draft(self):
        job = self.prepare_draft_publication()
        self.layer.verify, job.run.step_overrides["verify"] = config.Verify(), {"tries": 2}
        before = pipeline.verification_trees(job.b)
        job.review, job.reviews = pipeline.Review("approve", trees=before), 1
        gap = pipeline.verify.Verified("blocked", blocked_by="The settings viewer is unavailable.")

        def agent(name, prompt, *_, role, **_kw):
            if (name, role) == ("verify_fix", "check_repair"):
                self.write("settings-config.txt", "revision = 1\n")
                return Result(True)
            self.assertEqual((name, role), ("review2", "review"))
            self.assertTrue(job.b.full_checks_current(pipeline.verification_trees(job.b)))
            self.assertIn(gap.blocked_by, prompt)
            return Result(True, structured={"problems": []})

        flow = factory.Flow(job.run.id, job)
        with (patch.object(job.run, "agent", side_effect=agent),
              patch.object(job.run.budget, "verifying", side_effect=contextlib.nullcontext),
              patch.object(pipeline, "check_in_use", side_effect=[pipeline.verify.Verified("fail"), gap]),
              patch.object(pipeline.Build, "push")):
            self.assertIsNone(feature.final_blocker(flow, str(job.ws), patch=False, fixed=True, skipped=False, review_needed=False))
            self.assertNotEqual(job.review.trees, before)
            self.assertEqual(job.review.trees, pipeline.verification_trees(job.b))
            published = self.publish_fixture()
            self.assertTrue(gh.pr_create.call_args.kwargs["draft"])
        self.assertEqual(published["verified"]["verdict"], "blocked")
        self.assertIn(gap.blocked_by, published["report_body"])

    def test_scripted_and_controller_exhaustion_require_a_held_draft(self):
        for controller, error in ((False, workspace.RunFailed), (True, ValueError)):
            with self.subTest(controller=controller):
                job = self.prepare_draft_publication(focused=True)
                job.review_exhausted = False
                with self.assertRaisesRegex(error, "needs current focused repair evidence"):
                    self.publish_fixture(controller)
                if controller:
                    job.run.budget.repair_calls = self.layer.max_repair_cycles
                    evidence = self.controller.data["evidence"]
                    saved = evidence.pop("verify")
                    with self.assertRaisesRegex(ValueError, "needs current evidence: verify"):
                        self.publish_fixture(controller)
                    evidence["verify"] = saved
                job.review_exhausted = not controller
                with patch.object(pipeline.Build, "push") as push, patch.object(pipeline, "set_draft") as draft:
                    self.assertIn("Focused repair evidence is still required", self.publish_fixture(controller)["draft_why"])
                    self.assertIs(gh.pr_create.call_args.kwargs["draft"], True)
                    gh.pr_create.assert_called_once()
                push.assert_called_once()
                draft.assert_not_called()

    def test_held_update_cannot_push_without_a_confirmed_draft_at_its_head(self):
        job = self.prepare_draft_publication(existing_pr=True, focused=True)
        c, = job.checkouts
        ready, replaced = (False, c.lease), (True, self.git("rev-parse", c.origin_base))
        for controller, states in ((False, [ready, ready]), (False, [ready, replaced]), (True, [replaced])):
            if controller:
                job.review_exhausted = False
                job.run.budget.repair_calls = self.layer.max_repair_cycles
            with (self.subTest(states=states), self.draft_actions(states) as actions,
                  self.assertRaisesRegex(workspace.RunFailed, "not a confirmed draft")):
                self.publish_fixture(controller)
            expected = [call.read(c.pr)] if controller else [call.read(c.pr), call.draft(c.pr, True), call.read(c.pr)]
            self.assertEqual(actions.mock_calls, expected)

    def test_classic_feedback_with_pending_focused_evidence_does_not_update_a_ready_pr(self):
        job = self.prepare_draft_publication(existing_pr=True, focused=True)
        c, = job.checkouts
        job.run.feedback = [{"pr_url": c.pr}]
        self.assertTrue(job.b.focused_hold())
        job.b.restage()
        with (self.draft_actions([(False, c.lease)]) as actions,
              self.assertRaisesRegex(workspace.RunFailed, "not a confirmed draft")):
            pipeline.push_classic_candidate(job.b, job.branch, "Fixture update", require_draft=True)
        self.assertEqual(actions.mock_calls, [call.read(c.pr)])


class HeldSiblingDraftTests(HeldRepairDraftFixture):
    def test_no_push_continuation_holds_rechecked_heads_without_converting_feedback_or_repair_siblings(self):
        job = self.prepare_draft_publication(existing_pr=True, sibling=True)
        c, sibling = job.checkouts
        prior = c.lease
        c.note_pushed_head(self.commit("Published fixture goal", "goal.txt"))
        sibling.revision = sibling.lease
        job.continues = "continued-run"
        self.assertTrue(self.perform("checks.run")["full"])
        job.review = pipeline.Review("request_changes", failed=True, trees=pipeline.verification_trees(job.b))
        for mode, targets in (("ordinary", job.checkouts), ("repair", [c]), ("feedback", []), ("pending", []),
                              ("moved", job.checkouts)):
            c.revision = prior if mode == "repair" else c.lease
            job.run.repairs = "repair-source" if mode == "repair" else ""
            job.run.feedback = [{"pr_url": c.pr}] if mode == "feedback" else []
            job.prepared_feedback_prs = [c.pr] if mode == "pending" else []
            states = [(draft, target.lease) for target in targets for draft in (False, True)]
            if mode == "moved":
                states[-1] = True, self.git("rev-parse", sibling.origin_base)
            with self.subTest(mode=mode), self.draft_actions(states) as actions:
                if mode == "moved":
                    with self.assertRaisesRegex(workspace.RunFailed, "not a confirmed draft at this run's head"):
                        self.publish_fixture()
                else:
                    published = self.publish_fixture()
                    self.assertEqual((published["committed"], published["drafted"]),
                                     (False, [target.github for target in targets]))
                self.assertEqual(actions.mock_calls, [action for target in targets for action in
                                 (call.read(target.pr), call.draft(target.pr, True), call.read(target.pr))])

    def test_held_updates_leave_an_unchanged_sibling_ready(self):
        for mode in ("scripted", "controller", "adopt"):
            with self.subTest(mode=mode):
                job = self.prepare_draft_publication(existing_pr=True, sibling=True)
                c, sibling = job.checkouts
                adopt = mode == "adopt"
                if mode != "scripted":
                    head = self.commit(f"Update\n\nArete-Run: {job.run.id}", "goal.txt")
                if adopt:
                    c.note_pushed_head(head)
                    c.revision, c.pr, sibling.revision = head, "", sibling.lease
                    gh.open_pr_url.return_value = self.pr_url
                self.exhaust_focused_review(job)
                if not adopt:
                    job.run.budget.repair_calls = self.layer.max_repair_cycles
                states = [(False, c.lease)] + [(True, c.lease)] * (1 if adopt else 3)
                with self.draft_actions(states, mock_build_push=mode != "scripted") as actions:
                    published = self.publish_fixture(mode == "controller")
                    gh.pr_create.assert_not_called()
                at_head = call.read(self.pr_url)
                expected = [at_head, call.draft(self.pr_url, True), at_head]
                if adopt:
                    pipeline.sync_pr_bodies.assert_called_once()
                    self.assertIn("Focused repair evidence is still required", published["draft_why"])
                else:
                    self.assertIn(sibling.github, published["changed"])
                    pushed, = actions.commit.call_args_list
                    self.assertEqual(pushed.args[:2], ([c, sibling], [c]))
                    expected += [call.commit(*pushed.args, **pushed.kwargs), at_head, at_head]
                self.assertEqual(actions.mock_calls, expected)
                self.assertEqual((published["found"], published["drafted"], published["pushed"]),
                                 ([c.github] if adopt else [], [c.github], [] if adopt else [c.github]))


class FeedbackInUseDraftTests(HeldRepairDraftFixture):
    def test_feedback_missing_in_use_evidence_requires_the_same_draft_through_push(self):
        job = self.prepare_draft_publication(existing_pr=True)
        c, = job.checkouts
        job.run.feedback = [{"pr_url": c.pr}]
        b = job.b
        finding = {"problem": "The settings page shows the wrong revision.", "severity": "blocker",
                   "verification": "in_use", "acceptance": "Reload settings and read revision 1 with value blue."}
        b.carry_review(pipeline.Review("request_changes", problems=[finding]), "repair")
        self.assertTrue(self.perform("checks.run")["full"])
        job.verified = self.observe_file(b, "goal.txt", "General smoke check", "reached")
        job.review = pipeline.Review("approve", trees=job.verified.observed_trees)
        self.controller.stamp("review", job.review.asdict())
        self.controller.stamp("verify", asdict(job.verified))
        self.assertEqual((b.focused_hold(), b.in_use_repair_hold(job.verified)), ("", "repair:B1"))
        held, ready, moved = (True, c.lease), (False, c.lease), (True, self.git("rev-parse", c.origin_base))
        for states in ([held, ready], [held, moved], [held, held], [ready]):
            job.check_publication_targets = Mock()
            with self.subTest(states=states), self.draft_actions(states) as actions:
                def push(*_, check_targets, **_kw):
                    check_targets()
                    actions.delivered()

                actions.draft.side_effect = AssertionError("Converted a feedback PR")
                actions.push.side_effect = push
                if states == [held, held]:
                    self.assertIn("Independent in-use evidence is still required for repair:B1",
                                  self.publish_fixture()["draft_why"])
                else:
                    with self.assertRaisesRegex(workspace.RunFailed, "not a confirmed draft") as refused:
                        self.publish_fixture(states == [ready])
                    if states == [ready]:
                        self.assertIn("Requires independent in use", refused.exception.detail)
                        actions.push.assert_not_called()
                actions.commit.assert_not_called()
                self.assertEqual(actions.delivered.call_count, int(states == [held, held]))
                self.assertEqual(actions.read.call_args_list, [call(c.pr)] * len(states))
                self.assertEqual(job.check_publication_targets.call_count, len(states))


class FocusedRepairContinuationTests(HeldRepairDraftFixture):
    producer = "repair:B1"

    def carry_rollback(self, b):
        self.write("revision.txt", "2 green\n")
        b.carry_review(pipeline.Review("request_changes", trees=pipeline.verification_trees(b),
                                      problems=[self.blocking_problem]), "repair")
        self.assertEqual(b.check_repair_findings(producer="before-repair"), f"{self.producer} (fail)")
        self.write("revision.txt", "1 blue\n")
        self.assertEqual(b.check_repair_findings(producer="after-repair"), "")

    def test_explicit_repair_reruns_closed_regression_on_changed_candidate(self):
        b = self.prepare_settings().b
        self.write("history.txt", "1 blue\n")
        self.carry_rollback(b)
        previous = {**self.blocking_problem, "finding": self.producer, "status": "fixed"}
        history = {**self.blocking_problem, "file": "history.txt", "problem": "Rollback lost the history.",
                   "check": "history", "acceptance": "Read both revisions after rollback."}
        self.layer.targeted_commands["history"] = self.rollback.replace("revision.txt", "history.txt").replace(
            '"1 blue"', '"1 blue, 2 green"')
        latest = pipeline.Review("request_changes", trees=pipeline.verification_trees(b),
                                 problems=[history], earlier=[previous])
        b.carry_review(latest, "latest")
        self.assertEqual(b.check_repair_findings(producer="latest:before", baseline=True), "latest:B1 (fail)")
        evidence = pipeline.note_publication_review(b.run, "catch_up", latest, pipeline.verify.Verified("unverified"))
        line = {"event": "publication_review", "run": b.run.id, "seq": 1, "evidence": evidence}
        with patch.object(flows, "lineage_runs", return_value={b.run.id: [line]}):
            review, origin, state = repairs.saved_review(b.run.id)
        self.assertIn("publication_review", origin)
        restored = replace(b, repair_findings={}, repair_receipts={}, repair_baselines={})
        self.controller.job.built = restored
        b.run.step_overrides["check_repair"] = {"tries": 1}
        source = repairs.Source(b.run.id, self.branch, [], review, origin, "Repair rollback", "", "", repair_state=state)

        def repair(_step, prompt, _cwd, *, role, **_):
            self.assertEqual(role, "review_repair")
            for text in (self.acceptance, history["acceptance"], "Focused check `history`: fail."):
                self.assertIn(text, prompt)
            self.write("history.txt", "1 blue, 2 green\n")
            self.write("revision.txt", "2 green\n")
            self.assertEqual(restored.focused_status(), {self.producer: "stale", "latest:B1": "stale"})
            return Result(True)

        with (patch.object(b.run, "repairs", source.run_id), patch.object(repairs, "source", return_value=source),
              patch.object(b.run, "agent", side_effect=repair)):
            outcome = flows.repair(self.controller.flow, str(self.wt))
        self.assertEqual(outcome.feedback, "focused review regressions did not pass")
        self.assertEqual(restored.focused_status(), {self.producer: "fail", "latest:B1": "pass"})

    def test_saved_review_restores_legacy_findings_and_preserves_current_receipts(self):
        job = self.prepare_settings()
        b = job.b
        implicit = {k: v for k, v in self.blocking_problem.items() if k != "verification"}
        data = {"review": pipeline.Review("request_changes", problems=[implicit]).asdict()}
        job.take("review", factory.Outcome("fail", data=data), stored=True)
        check, = b.repair_findings
        self.assertEqual(b.repair_findings[check]["acceptance"], self.acceptance)
        self.assertEqual(b.check_repair_findings(producer="before", baseline=True), f"{check} (not run)")
        self.assertEqual(b.check_repair_findings(producer="repair"), f"{check} (baseline unavailable)")
        self.assertEqual(b.repair_receipts[check]["exit"], 0)
        state = b.repair_state()
        state["findings"][check] = {**state["findings"][check], "verification": "review"}
        job.take("review", factory.Outcome("fail", data={**data, "repair_state": state}), stored=True)
        self.assertEqual(b.focused_status(), {check: "baseline unavailable"})
        self.assertEqual(b.repair_receipts, state["receipts"])

    def test_full_and_focused_ci_repairs_share_the_configured_attempts(self):
        b = self.prepare_settings(max_repair_cycles=2).b
        b.checkouts[0].pr = self.pr_url
        b.run.step_overrides["check_repair"] = {"tries": 2}
        self.carry_rollback(b)
        self.layer.checks["ready"] = "test -f ready.txt"

        def repair(step, prompt, *, role):
            self.assertEqual((step, role), ("check_repair", "check_repair"))
            self.assertIn("ready.txt", prompt)
            b.run.budget.charge(step, role)
            self.write("ready.txt", "ready\n")
            self.write("revision.txt", "2 green\n")
            return Result(True)

        with (patch.object(b, "resume", side_effect=repair) as worker, patch.object(b, "push") as push,
              patch.object(b, "check", wraps=b.check) as full, patch.object(pipeline, "_review") as review,
              self.assertRaisesRegex(workspace.RunFailed, "focused review regressions did not pass") as held):
            pipeline.push_ci_fix(b, self.branch, [], "CI fixture", 1)
        self.assertIn("saw 2 green", held.exception.detail)
        self.assertEqual((worker.call_count, full.call_count, review.call_count, push.call_count), (1, 1, 0, 0))


class OrchestratedFinishBudgetTests(PollFixture):
    def setUp(self):
        super().setUp()
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text(
            '[factory]\npath = "classic"\n[limits]\nmax_repair_cycles = 3\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        self.patches.enter_context(patch.object(config, "load", return_value=self.cfg))
        self.patches.enter_context(patch.object(pipeline, "run_refusal", return_value=(False, "")))

    def dispatch_failed_finish(self, controller):
        controller.data["acceptance"] = "fixture"
        lease = controller.lease
        turn = ledger.begin(lease, {}, 1)
        version = ledger.snapshot(lease)["version"]
        action, = ledger.accept(lease, turn, version,
                                {"calls": [{"op": "run.finish", "args": {"reason": "failed"}}]}, 1)
        self.assertIsNone(controller.dispatch(action))
        finished, = ledger.actions(lease)
        self.assertEqual(finished["state"], "succeeded")
        return controller.ending

    def finish_failed(self, repairs):
        def drive(flow):
            controller = orchestration.Controller(flow)
            for _ in range(repairs):
                controller.run.budget.charge("repair", "review_repair")
            return self.dispatch_failed_finish(controller)

        self.assertEqual(pipeline.run_issue(self.repo.name, self.ticket, script=pipeline.Script("orchestrate", drive)), 1)
        self.assertEqual(tasks_store.task_get(self.ticket)["state"], "failed")
        return run_store.newest_whole_run(self.repo.name, self.ticket)

    def test_failed_finish_on_spent_repair_chain_parks_and_ticket_edit_readmits(self):
        prior = self.made_run()
        prior.budget.charge("repair", "review_repair")
        record.settle(prior.id, "failed")
        failed = self.finish_failed(2)
        parked, saved = runlimits.latest_park(self.repo.name, self.ticket)
        self.assertEqual(parked["id"], failed["id"])
        self.assertEqual(saved.get("reason"), "the repair chain used its 3 repair cycles")
        self.assertEqual(saved["specification"], runlimits.specification(self.local.get(self.ticket)))
        with foundation.db() as connection:
            set_task_body(self.ticket, "A narrower specification", connection=connection)

        started = []

        def work(cfg, run, slot):
            started.append(run.id)
            self.assertEqual(run.budget.prior_repairs, 0)
            self.assertEqual(run.budget.repair_parents, [])
            for _ in range(3):
                run.budget.charge("repair", "review_repair")
            return "noop", "fixture finished", ""

        def spawn(repo, tid):
            self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(len(started), 1)
        log = (paths.RUNS_DIR / started[0] / "run.log").read_text()
        self.assertIn("budget chain: no parents\n", log)

    def test_failed_finish_with_repair_cycle_left_does_not_park(self):
        failed = self.finish_failed(2)
        self.assertIn("the orchestrator stopped without delivery", failed["summary"])
        self.assertFalse((paths.RUNS_DIR / failed["id"] / "parked.json").exists())

    def test_reserved_zte_failed_finish_on_spent_budget_fails_task_and_succeeds_in_ledger(self):
        deadline = util.iso(util.now() + timedelta(minutes=10))
        delivery = zte_store.reserve("fixture-policy", self.repo.name, "a" * 40, deadline,
                                     "Fixture ZTE task", "fix", "Fixture ZTE specification", {})
        tid = delivery.task
        rid = record.start("pipeline", repo=self.repo.name, issue=tid)
        delivery = zte_store.transition(delivery, "dispatching")
        zte_store.transition(delivery, "running", run=rid)
        directory = paths.RUNS_DIR / rid
        directory.mkdir()
        run = pipeline.Run(rid, self.repo, directory, tracker=self.local, tid=tid, cfg=self.cfg, path="orchestrate")
        run.enter("running")
        self.assertEqual(tasks_store.task_get(tid)["state"], "running")
        run.budget = runlimits.Budget(run, runlimits.Policy(max_repair_cycles=1), "", self.local.get(tid))
        run.budget.charge("repair", "review_repair")

        status, summary, _ = self.dispatch_failed_finish(orchestration.Controller(flows.start(self.cfg, run, {})))

        self.assertEqual(status, "failed")
        self.assertEqual(summary, "the orchestrator stopped without delivery")
        self.assertEqual(tasks_store.task_get(tid)["state"], "failed")
        self.assertFalse((directory / "parked.json").exists())


class FailedPollBudgetTests(ReadmissionFixture):
    def failed_with_spent_repairs(self, previous="", *, pr_url=""):
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n[limits]\nmax_repair_cycles = 3\n')
        self.cfg.replay = config.ReplayConfig(all=True)
        failed = self.made_run(previous)
        record.note(failed.id, repair_calls=3)
        record.settle(failed.id, "failed", pr_url=pr_url)
        self.local.label(self.ticket, add=["failed"])
        return failed

    def test_poll_keeps_budget_after_failed_run_with_unchanged_specification(self):
        older = self.made_run()
        record.settle(older.id, "failed")
        with foundation.db() as connection:
            set_task_body(self.ticket, "The latest failed run used this specification", connection=connection)
        failed = self.failed_with_spent_repairs(older.id)
        self.local.label(self.ticket, remove=["failed"])

        def work(cfg, run, slot):
            self.assertFalse(run.force)
            self.assertEqual(run.budget.prior_repairs, 3)
            run.budget.charge("repair", "review_repair")
            return "noop", "fixture finished", ""

        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, work)), 0)
        retry = run_store.newest_live_whole_run(self.repo.name, self.ticket)
        self.assertNotEqual(retry["id"], failed.id)
        saved = runlimits.metadata(retry["id"], "budget.json")
        log = (paths.RUNS_DIR / retry["id"] / "run.log").read_text()
        self.assertEqual(saved["repair_parents"], [failed.id, older.id])
        self.assertEqual(saved["repair_left_out"], [])
        self.assertIn("the repair chain used its 3 repair cycles", log)

    def assert_failed_run_charged(self, retry, failed, minutes):
        [chain] = [line for line in run_lines.read(retry["id"]) if line["event"] == "budget_chain"]
        self.assertEqual(chain["charged"][failed.id]["repair_calls"], 3)
        self.assertEqual(chain["charged"][failed.id]["run_minutes"], minutes)
        log = (paths.RUNS_DIR / retry["id"] / "run.log").read_text()
        self.assertIn(f"budget chain: {failed.id}: {minutes:.1f} run minutes, 0.0 verification minutes, "
                      "3 repair calls", log)

    def test_poll_retry_keeps_budget_when_lookup_times_out(self):
        failed = self.failed_with_spent_repairs()
        record.note(failed.id, active_seconds=3599.75)
        self.local.label(self.ticket, remove=["failed"])
        clock = [100.0]
        work = Mock()

        def launch_with_stalled_lookup(repo, tid):
            with patch.object(runlimits, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
                    patch.object(signal, "setitimer") as timer:
                def stalled_lookup(*_):
                    timer.assert_called_once_with(signal.ITIMER_REAL, 3600)
                    self.assertTrue(callable(signal.getsignal(signal.SIGALRM)))
                    clock[0] += 3600
                    signal.raise_signal(signal.SIGALRM)
                    self.fail("the watchdog did not interrupt the lookup")

                with patch.object(tracker.LocalTracker, "get", side_effect=stalled_lookup) as lookup:
                    self.assertEqual(self.run_polled_ticket(repo, tid, work), 1)
                lookup.assert_called_once()

        self.assertEqual(self.poll_with(self.cfg, launch_with_stalled_lookup), 0)
        work.assert_not_called()
        retry = run_store.newest_live_whole_run(self.repo.name, self.ticket)
        parked = runlimits.metadata(retry["id"], "parked.json")
        self.assertEqual(parked.get("reason"), "the run chain reached its cumulative 60 minute budget")
        self.assertEqual(parked["specification"], runlimits.recorded_specification(failed.id))
        saved = runlimits.metadata(retry["id"], "budget.json")
        self.assertEqual(saved["parents"], [failed.id])
        self.assertEqual(saved["repair_parents"], [failed.id])
        self.assertEqual(retry["active_seconds"], 3600)
        self.assert_failed_run_charged(retry, failed, 60.0)

    def test_poll_retry_compares_an_edit_after_the_inherited_deadline_would_expire(self):
        failed = self.failed_with_spent_repairs()
        record.note(failed.id, active_seconds=3599.75)
        with foundation.db() as connection:
            set_task_body(self.ticket, "A narrower specification", connection=connection)
        self.local.label(self.ticket, remove=["failed"])
        ticket = self.local.get(self.ticket)
        clock = [100.0]
        budgets = []

        def slow_lookup(*_):
            clock[0] += 12
            return ticket

        def work(cfg, run, slot):
            self.assertFalse(run.force)
            budgets.append(run.budget)
            run.budget.charge("repair", "review_repair")
            return "noop", "fixture finished", ""

        def launch_with_slow_lookup(repo, tid):
            with patch.object(runlimits, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
                    patch.object(signal, "setitimer"), \
                    patch.object(tracker.LocalTracker, "get", side_effect=slow_lookup) as lookup:
                self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)
                lookup.assert_called_once()

        self.assertEqual(self.poll_with(self.cfg, launch_with_slow_lookup), 0)
        retry = run_store.newest_live_whole_run(self.repo.name, self.ticket)
        self.assertNotEqual(retry["id"], failed.id)
        self.assertEqual(retry["status"], "noop")
        self.assertEqual(retry["active_seconds"], 12)
        [budget] = budgets
        self.assertEqual(budget.parents, [])
        self.assertEqual(budget.run_deadline() - clock[0], 60 * 60 - 12)
        saved = runlimits.metadata(retry["id"], "budget.json")
        self.assertEqual(saved["parents"], [])
        self.assertEqual(saved["repair_left_out"], [failed.id])
        self.assertEqual(runlimits.repair_count(retry["id"]), 1)
        [chain] = [line for line in run_lines.read(retry["id"]) if line["event"] == "budget_chain"]
        self.assertEqual(chain["charged"], {})
        log = (paths.RUNS_DIR / retry["id"] / "run.log").read_text()
        self.assertIn("budget: left out 3 earlier repair cycles, 60.0 run minutes and 0.0 verification minutes "
                      "of 1 earlier run because the ticket specification changed since they ran", log)

    def test_poll_keeps_budget_across_a_failed_ticket_fetch(self):
        failed = self.failed_with_spent_repairs()
        self.local.label(self.ticket, remove=["failed"])
        work = Mock()

        def launch_with_fetch_error(repo, tid):
            with patch.object(tracker.LocalTracker, "get", side_effect=tracker.TaskError("ticket fetch failed")):
                self.assertEqual(self.run_polled_ticket(repo, tid, work), 1)

        self.assertEqual(self.poll_with(self.cfg, launch_with_fetch_error), 0)
        retry = run_store.newest_live_whole_run(self.repo.name, self.ticket)
        self.assertNotEqual(retry["id"], failed.id)
        self.assertEqual(retry["status"], "failed")
        self.assertIn("ticket fetch failed", retry["summary"])
        work.assert_not_called()
        self.assert_failed_run_charged(retry, failed, 0.0)

        self.local.label(self.ticket, remove=["failed"])

        def charge_repair(cfg, run, slot):
            run.budget.charge("repair", "review_repair")
            return "noop", "fixture finished", ""

        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, charge_repair)), 0)
        next_retry = run_store.newest_live_whole_run(self.repo.name, self.ticket)
        self.assertNotEqual(next_retry["id"], retry["id"])
        self.assertIn("the repair chain used its 3 repair cycles", next_retry["summary"])
        self.assertEqual(runlimits.repair_count(next_retry["id"]), 0)
        for key in ("parents", "repair_parents"):
            self.assertEqual(runlimits.metadata(retry["id"], "budget.json")[key], [failed.id])
            self.assertEqual(runlimits.metadata(next_retry["id"], "budget.json")[key], [retry["id"], failed.id])

    def assert_fresh_feedback_logged_on_lookup_failure(self, *, timeout):
        url = "https://github.com/owner/fixture/pull/1"
        failed = self.failed_with_spent_repairs(pr_url=url)
        run_lines.write(failed.id, "execution", path="orchestrate")
        record.note(failed.id, active_seconds=180, verify_seconds=60)
        insights_store.add_feedback([{"pr_url": url, "repo": self.repo.name, "issue": self.ticket,
                                      "kind": "comment", "gh_id": 1, "body": "Please revise", "pr_state": "OPEN",
                                      "status": "new", "at_once": 0, "created": "2026-01-01T00:00:00Z"}])
        with foundation.db() as connection:
            set_task_body(self.ticket, "A narrower specification", connection=connection)
        self.local.label(self.ticket, remove=["failed"])
        clock = [100.0]

        def fail_lookup(*_):
            if timeout:
                clock[0] += 3600
                signal.raise_signal(signal.SIGALRM)
                self.fail("the watchdog did not interrupt the lookup")
            raise tracker.TaskError("ticket fetch failed")

        def spawn(repo, tid):
            command = pipeline.run_command(repo, tid)
            self.assertEqual(command[-4:], [repo, tid, "--continue", failed.id])
            script = pipeline.Script("orchestrate", lambda _: self.fail("started work after lookup failure"),
                                     continues=failed.id)
            with patch.object(runlimits, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
                    patch.object(signal, "setitimer"), \
                    patch.object(tracker.LocalTracker, "get", side_effect=fail_lookup) as lookup:
                self.assertEqual(self.run_polled_ticket(repo, tid, Mock(), script), 1)
                lookup.assert_called_once()

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        retry = run_store.newest_live_whole_run(self.repo.name, self.ticket)
        self.assertNotEqual(retry["id"], failed.id)
        self.assertEqual(retry["status"], "failed")
        self.assertIn("the run chain reached its cumulative 60 minute budget" if timeout else "ticket fetch failed",
                      retry["summary"])
        saved = runlimits.metadata(retry["id"], "budget.json")
        self.assertEqual(saved["parents"], [])
        self.assertEqual(saved["repair_parents"], [])
        self.assertEqual(saved["left_out"], [failed.id])
        self.assertEqual(saved["repair_left_out"], [failed.id])
        self.assertIsNone(saved["specification"])
        [chain] = [line for line in run_lines.read(retry["id"]) if line["event"] == "budget_chain"]
        self.assertEqual(chain["charged"], {})
        log = (paths.RUNS_DIR / retry["id"] / "run.log").read_text()
        self.assertEqual(log.count("budget chain: no parents\n"), 1)
        self.assertEqual([line.split("] ", 1)[-1] for line in log.splitlines() if "budget: left out" in line], [
            "budget: left out 3 earlier repair cycles, 3.0 run minutes and 1.0 verification minutes "
            "of 1 earlier run because the owner commented on the PR since they ran",
        ])

    def test_poll_logs_fresh_feedback_reset_when_lookup_errors(self):
        self.assert_fresh_feedback_logged_on_lookup_failure(timeout=False)

    def test_poll_logs_fresh_feedback_reset_when_lookup_times_out(self):
        self.assert_fresh_feedback_logged_on_lookup_failure(timeout=True)

    def test_poll_continuation_drops_spent_repairs_after_specification_edit(self):
        url = "https://github.com/owner/fixture/pull/1"
        failed = self.failed_with_spent_repairs(pr_url=url)
        run_lines.write(failed.id, "execution", path="orchestrate")
        insights_store.add_feedback([{"pr_url": url, "repo": self.repo.name, "issue": self.ticket,
                                      "kind": "comment", "gh_id": 1, "body": "Please revise", "pr_state": "OPEN",
                                      "status": "new", "at_once": 0, "created": "2026-01-01T00:00:00Z"}])
        insights_store.take_feedback(insights_store.open_feedback([url]), failed.id)
        insights_store.settle_feedback(failed.id, "failed")
        record.note(failed.id, active_seconds=3599.75)
        with foundation.db() as connection:
            set_task_body(self.ticket, "A narrower specification", connection=connection)
        self.local.label(self.ticket, remove=["failed"])
        ticket = self.local.get(self.ticket)
        clock = [100.0]

        def drive(flow):
            run = flow.ctx.run
            self.assertEqual(flow.continues, failed.id)
            self.assertEqual([row["run_id"] for row in run.feedback], [failed.id])
            run.budget.charge("repair", "review_repair")
            return "noop", "fixture finished", ""

        def spawn(repo, tid):
            command = pipeline.run_command(repo, tid)
            self.assertEqual(command[-4:], [repo, tid, "--continue", failed.id])
            script = pipeline.Script("orchestrate", drive, continues=command[-1])
            timer = Mock()

            def slow_lookup(*_):
                timer.assert_called_once_with(signal.ITIMER_REAL, 3600)
                clock[0] += 12
                return ticket

            with patch.object(runlimits, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
                    patch.object(signal, "setitimer", timer), \
                    patch.object(tracker.LocalTracker, "get", side_effect=slow_lookup) as lookup:
                self.assertEqual(self.run_polled_ticket(repo, tid, Mock(), script), 0)
                lookup.assert_called_once()

        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.poll_with(self.cfg, spawn), 0, output.getvalue())
        retry = run_store.newest_live_whole_run(self.repo.name, self.ticket)
        self.assertEqual(retry["status"], "noop")
        self.assertEqual(retry["active_seconds"], 12)
        saved = runlimits.metadata(retry["id"], "budget.json")
        self.assertEqual(saved["parents"], [])
        self.assertEqual(saved["repair_left_out"], [failed.id])
        self.assertEqual(runlimits.repair_count(retry["id"]), 1)
        log = (paths.RUNS_DIR / retry["id"] / "run.log").read_text()
        self.assertIn("budget: left out 3 earlier repair cycles, 60.0 run minutes and 0.0 verification minutes "
                      "of 1 earlier run because the ticket specification changed since they ran", log)

    def test_poll_leaves_unchanged_park_in_place_after_failed_label_removed(self):
        parked = self.park_ticket()
        self.local.label(self.ticket, remove=["failed"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("started without an edit")), 0)
        self.assertIn(f"parked after run {parked.id}: repair limit", output.getvalue())
        self.assertEqual(run_store.newest_live_whole_run(self.repo.name, self.ticket)["id"], parked.id)


class OrchestrationFinishTests(Isolated):
    PR = "https://github.com/owner/project/pull/685"

    def setUp(self):
        super().setUp()
        self.repo = replace(self.repo, github="owner/project", tracker="github",
                            parts=[config.Part("", "owner/project")])
        self.cfg = config.Config(repos=[self.repo], replay=config.ReplayConfig(all=True))
        self.issue = "658"
        self.labels = {"agent"}
        self.comments = []
        self.prs = {self.PR: {"state": "OPEN", "isDraft": False}}
        self.checkout = workspace.Checkout("", self.repo.github, self.repo.path,
                                           paths.WORKTREES_DIR / f"{self.repo.name}-{self.issue}",
                                           "main", pr=self.PR)
        for module, name, func in (
            (config, "load", lambda: self.cfg),
            (gh, "issue", self.issue_view),
            (gh, "issue_labels", self.edit_labels),
            (gh, "issue_comment", self.post_comment),
            (gh, "api_create_comment", self.post_api_comment),
            (gh, "ensure_label", lambda *args, **kw: ""),
            (gh, "default_branch", lambda repo: "main"),
            (gh, "pr_merge_details", self.prs.__getitem__),
            (gh, "pr_set_draft", self.set_draft),
            (scratch, "process_ids", dict),
            (scratch, "service_labels", dict),
            (sandbox, "AUTHORITY", self.root / "authority"),
        ):
            self.patches.enter_context(patch.object(module, name, func))

    def issue_view(self, repo, number):
        return {"number": number, "title": "Fixture issue", "body": "Fixture description", "state": "OPEN",
                "author": {"login": "owner"}, "labels": [{"name": label} for label in sorted(self.labels)]}

    def edit_labels(self, repo, number, add=(), remove=(), **kwargs):
        self.labels.difference_update(remove)
        self.labels.update(add)

    def post_comment(self, repo, number, body):
        drafted = {url for url, pr in self.prs.items() if pr["isDraft"]}
        self.comments.append((body, drafted, self.labels.copy()))

    def post_api_comment(self, repo, number, body):
        self.post_comment(repo, number, body)
        return str(len(self.comments)), f"https://github.com/{repo}/issues/{number}#issuecomment-{len(self.comments)}"

    def set_draft(self, url, draft):
        self.prs[url]["isDraft"] = draft

    def run_controller(self, decide, code):
        def prepare(flow, tree):
            from arete import outbox

            flow.ctx.run.enter("running")
            outbox.send_due()
            return factory.Outcome("pass", data={
                "layer": str(self.repo.layer_dir),
                "checkouts": [replace(self.checkout, pr=url).to_row() for url in self.prs],
                "ticket": asdict(flow.ctx.run.tracker.get(self.issue)), "branch": "arete/fixture",
                "add_dirs": [], "attachments": "", "expertise": "", "triage": "",
            })

        def drive(flow):
            controller = orchestration.Controller(flow)
            with patch.object(controller, "decide", side_effect=lambda: decide(controller)):
                return controller.drive()

        with (patch.dict(factory.STEPS, {"prepare": prepare}),
              patch.object(factory, "fingerprint", return_value="fixture-tree")):
            from arete import outbox

            self.assertEqual(pipeline.run_issue(self.repo.name, self.issue, script=pipeline.Script("orchestrate", drive)),
                             code)
            outbox.send_due()
        return run_store.newest_whole_run(self.repo.name, self.issue)

    def controller_action(self, controller, op, args, rationale):
        version = ledger.snapshot(controller.lease)["version"]
        turn = ledger.begin(controller.lease, {}, controller.policy.max_decisions)
        response = {"version": PROTOCOL, "decision_id": turn, "state_version": version,
                    "rationale": rationale, "calls": [{"op": op, "args": args}]}
        validated = controller.catalog.validate(response, turn, version, {})
        return ledger.accept(controller.lease, turn, version, validated, controller.policy.max_actions)

    def test_failed_finish_comments_with_its_rationale_and_drafts_open_prs(self):
        other = "https://github.com/owner/other/pull/686"
        closed = "https://github.com/owner/closed/pull/687"
        self.prs[other] = {**self.prs[self.PR]}
        self.prs[closed] = {**self.prs[self.PR], "state": "CLOSED"}
        decisions = iter([
            ("catalog.list", {"kind": "context"}, "The earlier decision only listed context."),
            ("run.finish", {"reason": "failed"}, "The merge conflicts remain. See http://127.0.0.1:8000/logs."),
        ])
        row = self.run_controller(lambda controller: self.controller_action(controller, *next(decisions)), code=1)
        self.assertEqual((row["status"], row["summary"]), ("failed", "the orchestrator stopped without delivery"))
        self.assertEqual(self.labels, {"agent", "agent:failed"})
        (comment, drafted, labels), = self.comments
        self.assertIn("The merge conflicts remain. See [private address]/logs.", comment)
        self.assertIn(row["id"], comment)
        self.assertNotIn("The earlier decision", comment)
        self.assertNotIn("127.0.0.1", comment)
        self.assertEqual(drafted, {self.PR, other})
        self.assertEqual(labels, {"agent", "agent:running"})
        directory = paths.RUNS_DIR / row["id"]
        self.assertFalse((directory / "parked.json").exists())
        for text in (str(row), str(run_lines.read(row["id"])), (directory / "run.log").read_text(),
                     str(ledger.view(row["id"]))):
            self.assertNotIn("The merge conflicts remain", text)

    def test_failed_finish_with_unpaired_surrogate_settles_a_local_task(self):
        self.repo = replace(self.repo, tracker="local")
        self.cfg = replace(self.cfg, repos=[self.repo])
        self.issue = self.ticket
        decisions = iter([("run.finish", {"reason": "failed"}, "Échec \ud800 de la fusion.")])

        row = self.run_controller(lambda controller: self.controller_action(controller, *next(decisions)), code=1)

        self.assertEqual((row["status"], row["summary"]), ("failed", "the orchestrator stopped without delivery"))
        self.assertEqual(tasks_store.task_get(self.ticket)["state"], "failed")
        comment, = tasks_store.task_comments(self.ticket)
        self.assertIn(row["id"], comment["body"])
        self.assertIn("Échec \\ud800 de la fusion.", comment["body"])

    def test_delivered_finish_keeps_the_ready_pr_and_ticket_state(self):
        def decide(controller):
            controller.run.enter("pr")
            controller.data["published"] = {"ending": "pr", "summary": "Delivered fixture"}
            controller.data["published_tree"] = controller.fingerprint()
            controller.executing = "fixture-publication"
            for gate in ("checks", "verify", "review"):
                controller.stamp(gate, {})
            return self.controller_action(controller, "run.finish", {"reason": "delivered"}, "Published the change.")

        row = self.run_controller(decide, code=0)
        self.assertEqual((row["status"], row["summary"], row["pr_url"]), ("pr", "Delivered fixture", self.PR))
        self.assertEqual(self.labels, {"agent", "agent:pr"})
        self.assertEqual(self.comments, [])
        self.assertFalse(self.prs[self.PR]["isDraft"])


class BudgetReadmissionTests(ReadmissionFixture):
    def test_specification_edit_readmits_once_and_reparks_on_current_inputs(self):
        parked = self.park_ticket()
        with foundation.db() as connection:
            set_task_body(self.ticket, "A narrower specification", connection=connection)
        started = []

        def work(cfg, run, slot):
            started.append(run.id)
            self.assertFalse(run.force)
            self.assertEqual(run.budget.parents, [])
            raise runlimits.Parked("repair limit again")

        def spawn(repo, tid):
            self.assertEqual(self.run_polled_ticket(repo, tid, work), 1)

        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(len(started), 1)
        self.assertNotEqual(started[0], parked.id)
        self.assertEqual(runlimits.latest_park(self.repo.name, self.ticket)[0]["id"], started[0])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
        self.assertEqual(len(started), 1)
        self.assertIn(f"parked after run {started[0]}: repair limit again", output.getvalue())
        self.assertIn(runlimits.RETRY, output.getvalue())

    def test_raised_layer_budget_readmits_a_failed_ticket_with_fresh_repairs(self):
        self.park_ticket()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n[limits]\nmax_repair_cycles = 3\n')
        started = []

        def work(cfg, run, slot):
            started.append(run.budget.policy.max_repair_cycles)
            self.assertFalse(run.force)
            self.assertEqual(run.budget.parents, [])
            self.assertEqual(run.budget.repair_parents, [])
            self.assertEqual(run.budget.prior_repairs, 0)
            for _ in range(3):
                run.budget.charge("repair", "review_repair")
            with self.assertRaisesRegex(runlimits.Parked, "3 repair cycles"):
                run.budget.charge("repair", "review_repair")
            return "noop", "fixture finished", ""

        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: self.run_polled_ticket(repo, tid, work)), 0)
        self.assertEqual(started, [3])

    def test_child_refuses_a_specification_or_policy_reverted_after_poll(self):
        parked = self.park_ticket()
        original = self.local.get(self.ticket).body
        layer = self.repo.layer_dir / "config.toml"
        original_layer = layer.read_text()

        def set_body(body):
            with foundation.db() as connection:
                set_task_body(self.ticket, body, connection=connection)

        for change, revert in (
                (lambda: set_body("Changed specification"), lambda: set_body(original)),
                (lambda: layer.write_text(original_layer + "[limits]\nmax_repair_cycles = 3\n"),
                 lambda: layer.write_text(original_layer))):
            change()

            def spawn(repo, tid):
                revert()
                self.assertEqual(self.run_polled_ticket(repo, tid, lambda *_: self.fail("stale launch")), 2)

            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertIn(f"parked after run {parked.id}", output.getvalue())
            self.assertEqual([row["id"] for row in run_store.ticket_runs(self.repo.name, self.ticket)], [parked.id])

    def test_poll_does_not_repeat_a_launch_that_exits_before_registration(self):
        self.park_ticket()
        starts = []

        def spawn(repo, tid):
            starts.append(tid)

        for body in ("First edit", "Second edit"):
            with foundation.db() as connection:
                set_task_body(self.ticket, body, connection=connection)
            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertIn("Poll already tried these inputs.", output.getvalue())
            self.assertEqual(runlimits.admission(self.repo, self.ticket, self.local, runlimits.Policy(), True),
                             runlimits.Admission(reset_time=True, reset_repairs=True))
        self.assertEqual(starts, [self.ticket, self.ticket])


class TrackerReadmissionTests(ReadmissionFixture):
    def test_branch_readmission_checks_origin_once_per_poll_and_fails_closed(self):
        branch = "arete/fixture"
        command = ["git", "ls-remote", "origin", f"refs/heads/{branch}"]
        old, new = "a" * 40, "b" * 40
        remote = SimpleNamespace(returncode=0, stdout=f"{old}\trefs/heads/{branch}\n")
        calls, started = [], []
        sh = util.sh

        def read(cmd, *args, **kwargs):
            if cmd == ["git", "remote", "get-url", "origin"]:
                return SimpleNamespace(returncode=0, stdout="/tmp/fixture.git\n")
            if cmd == command:
                calls.append(cmd)
                return remote
            return sh(cmd, *args, **kwargs)

        def work(cfg, run, slot):
            started.append(run.id)
            self.assertFalse(run.force)
            self.assertEqual(run.budget.parents, [])
            self.assertEqual(run.budget.prior_repairs, 0)
            return "noop", "fixture finished", ""

        def spawn(repo, tid):
            self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)

        with patch.object(util, "sh", side_effect=read):
            parked = self.park_ticket(branch)
            unchanged = f"{old}\trefs/heads/{branch}\n{new}\trefs/heads/nested/refs/heads/{branch}\n"
            moved = f"{new}\trefs/heads/{branch}\n"
            cases = ((unchanged, 0, False), (moved, 1, False), ("truncated\n", 0, False), (moved, 0, True))
            for stdout, returncode, should_start in cases:
                with self.subTest(returncode=returncode, stdout=stdout):
                    calls.clear()
                    remote.returncode = returncode
                    remote.stdout = stdout
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(self.poll_with(self.cfg, spawn), 0)
                    self.assertEqual(calls, [command])
                    self.assertEqual(len(started), int(should_start))
                    if not should_start:
                        self.assertIn(f"parked after run {parked.id}: repair limit", output.getvalue())
                        self.assertIn(runlimits.RETRY, output.getvalue())
                    if returncode:
                        self.assertIn("could not confirm the parked branch head", output.getvalue())

    def test_multi_origin_readmission_checks_complete_exact_refs_and_revalidates_origins(self):
        branch = "arete/fixture"
        ref = f"refs/heads/{branch}"
        client, server = "a" * 40, "b" * 40
        self.repo.tracker = "jira"
        self.repo.parts = [config.Part("server", "Acme/server"), config.Part("client", "Acme/client")]
        urls = {"client": "https://github.com/acme/client.git", "server": "git@github.com:acme/server.git"}
        for name in urls:
            (self.repo.path / name).mkdir()
        lookups, calls, started = [], [], []
        sh = util.sh

        def repository(project, oid, name=branch):
            ref_row = {"prefix": "refs/heads/", "name": name, "target": {"oid": oid}} if oid else None
            return {"nameWithOwner": project, "ref": ref_row}

        old_client = repository("Acme/client", client)
        old_server = repository("Acme/server", server)
        deleted = repository("Acme/client", "")
        complete = {"part0": deleted, "part1": old_server}
        reply = {}

        def read(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "ls-remote"]:
                name = kwargs["cwd"].name
                lookups.append(name)
                oid = client if name == "client" else server
                return SimpleNamespace(returncode=0, stdout=f"{oid}\t{ref}\n")
            if cmd == ["git", "remote", "get-url", "origin"]:
                return SimpleNamespace(returncode=0, stdout=urls[kwargs["cwd"].name] + "\n")
            if cmd[:3] == ["gh", "api", "graphql"]:
                self.assertEqual(cmd[3:], ["--hostname", "github.com", "--input", "-"])
                calls.append(json.loads(kwargs["input"]))
                return SimpleNamespace(returncode=0, stdout=json.dumps(reply), stderr="")
            return sh(cmd, *args, **kwargs)

        def work(cfg, run, slot):
            started.append(run.id)
            self.assertEqual(run.budget.parents, [])
            return "noop", "fixture finished", ""

        with patch.object(util, "sh", side_effect=read), patch.object(tracker, "for_repo", return_value=self.local):
            parked = self.park_ticket(branch)
            self.assertCountEqual(lookups, ["client", "server"])
            lookups.clear()
            for response in ({"data": {"part0": old_client, "part1": old_server}},
                             {"data": {"part0": deleted}},
                             {"data": {"part0": deleted, "part1": {"nameWithOwner": "Acme/server"}}},
                             {"data": complete, "errors": [{"message": "partial failure"}]},
                             {"data": {**complete, "part1": repository("Acme/another", server)}},
                             {"data": {**complete, "part1": repository("Acme/server", server, branch + "-other")}}):
                with self.subTest(response=response):
                    reply = response
                    calls.clear()
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("unexpected launch")), 0)
                    self.assertIn(f"parked after run {parked.id}", output.getvalue())
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0]["variables"], {"ref": ref})
                    self.assertIn('name: "client"', calls[0]["query"])
                    self.assertIn('name: "server"', calls[0]["query"])

            reply = {"data": complete}
            urls["server"] = "https://github.com.example/acme/server.git"
            calls.clear()
            self.assertEqual(self.poll_with(self.cfg, lambda *_: self.fail("unverified origin")), 0)
            self.assertEqual(calls, [])

            urls["server"] = "git@github.com:acme/server.git"

            def stale_origin(repo, tid):
                urls["server"] = "https://github.com/acme/wrong.git"
                self.assertEqual(self.run_polled_ticket(repo, tid, work), 2)

            self.assertEqual(self.poll_with(self.cfg, stale_origin), 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(started, [])

            urls["server"] = "git@github.com:acme/server.git"
            reply = {"data": {**complete, "part1": repository("Acme/server", "c" * 40)}}
            calls.clear()

            def spawn(repo, tid):
                self.assertEqual(self.run_polled_ticket(repo, tid, work), 0)

            self.assertEqual(self.poll_with(self.cfg, spawn), 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(lookups, [])
            self.assertEqual(len(started), 1)

    def test_jira_parked_candidate_can_follow_a_page_of_ordinary_failures(self):
        self.repo.jira_project = "FIXTURE"
        source = tracker.JiraTracker(self.repo, self.cfg.jira)
        client = jira.Client("https://example.invalid", "unused", "unused")
        ordinary = [{"key": f"FIXTURE-{i}"} for i in range(1, 21)]
        parked = {"key": "FIXTURE-21", "fields": {"summary": "Parked ticket"}}
        with (patch.object(jira.Client, "from_env", return_value=client) as from_env,
              patch.object(client, "get", side_effect=[
                  {"issues": [], "isLast": True},
                  {"issues": ordinary, "isLast": False, "nextPageToken": "next"},
                  {"issues": [parked], "isLast": True},
              ]) as read):
            self.assertEqual([ticket.id for ticket in source.todo(parked=lambda key: key == parked["key"])],
                             [parked["key"]])
        from_env.assert_called_once_with(self.cfg.jira)
        self.assertEqual(read.call_args.kwargs["nextPageToken"], "next")


class QueueTests(PollFixture):
    def test_interrupted_ticket_rechecks_dependencies_before_starting(self):
        interrupted = self.made_run()
        record.settle(interrupted.id, "interrupted")
        prerequisite = tasks_store.task_add(self.repo.name, "Prerequisite", "Prerequisite", "feat")
        with foundation.db() as connection:
            set_task_body(self.ticket, f"Waits on {prerequisite}", connection=connection)
        self.cfg.replay = config.ReplayConfig(all=True)
        started = []
        self.assertEqual(self.poll_with(self.cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertNotIn(self.ticket, started)
        self.assertIn(prerequisite, started)

    def test_one_poll_fills_capacity_round_robin_without_relying_on_child_rows(self):
        second = config.Repo(name="other", path=self.root / "other", tracker="local", pipeline=True)
        second.path.mkdir()
        for repo in (self.repo, second):
            for i in range(4):
                tasks_store.task_add(repo.name, f"Task {i}", f"Task {i}", "feat")
        cfg = config.Config(repos=[self.repo, second], max_parallel=3, max_parallel_per_repo=2,
                            replay=config.ReplayConfig(all=True))
        started = []
        self.assertEqual(self.poll_with(cfg, lambda repo, tid: started.append((repo, tid))), 0)
        self.assertEqual(len(started), 3)
        self.assertEqual([repo for repo, _ in started[:2]], [self.repo.name, second.name])
        self.assertEqual(len(set(started)), 3)
        self.assertLessEqual(sum(repo == self.repo.name for repo, _ in started), 2)

    def test_dependency_and_parked_ticket_do_not_block_other_ready_work(self):
        prerequisite = tasks_store.task_add(self.repo.name, "Prerequisite", "Prerequisite", "feat")
        blocked = tasks_store.task_add(self.repo.name, f"Waits on {prerequisite}", "Blocked", "feat")
        parked = self.made_run()
        parked.budget.park("repair limit")
        record.settle(parked.id, "failed")
        failed = tasks_store.task_add(self.repo.name, "Ordinary failure", "Ordinary failure", "feat")
        self.local.label(failed, add=["failed"])
        extra = tasks_store.task_add(self.repo.name, "Ready", "Ready", "feat")
        cfg = config.Config(repos=[self.repo], max_parallel=8, max_parallel_per_repo=8,
                            replay=config.ReplayConfig(all=True))
        started = []
        self.assertEqual(self.poll_with(cfg, lambda repo, tid: started.append(tid)), 0)
        self.assertIn(prerequisite, started)
        self.assertIn(extra, started)
        self.assertNotIn(blocked, started)
        self.assertNotIn(failed, started)
        self.assertNotIn(self.ticket, started)

    def test_route_readiness_is_rechecked_after_every_start(self):
        for i in range(3):
            tasks_store.task_add(self.repo.name, str(i), str(i), "feat")
        ready, started = [True], []
        def routes(*args, **kw):
            return [SimpleNamespace(state="ready" if ready[0] else "held", detail="fixture quota hold", until="")]
        def start(repo, tid):
            started.append(tid)
            ready[0] = False
        with patch.object(pipeline.routing, "Discovery", return_value=SimpleNamespace(routes=routes)):
            self.assertEqual(self.poll_with(self.cfg, start), 0)
        self.assertEqual(len(started), 1)


class EnvironmentRequeueTests(Isolated):
    class_timeout = 20

    def setUp(self):
        super().setUp()
        tasks_store.task_set_state(self.ticket, "closed")
        self.repo = config.Repo(name="github-fixture", path=self.repo.path, github="owner/project",
                                parts=[config.Part("", "owner/project")], pipeline=True)
        self.cfg = config.Config(repos=[self.repo], replay=config.ReplayConfig(all=True))
        self.issue = "658"
        self.labels = {"agent"}
        self.comments = []
        self.started = []
        self.time = datetime(2026, 11, 21, 8, tzinfo=UTC)
        self.ws = paths.WORKTREES_DIR / f"{self.repo.name}-{self.issue}"
        self.authority = self.root / "authority"
        api = support.parentless_github_issue(self.repo.github, self.issue, self.issue_view)
        self.patches.enter_context(patch.object(gh, "gh_json", side_effect=api))
        for module, name, func in (
            (config, "load", lambda: self.cfg),
            (codebase, "STAMP", "old-engine"),
            (util, "now", lambda: self.time),
            (gh, "issue", self.issue_view),
            (gh, "issues", self.issue_list),
            (gh, "issue_labels", self.edit_labels),
            (gh, "issue_comment", self.post_comment),
            (gh, "api_create_comment", self.post_api_comment),
            (gh, "find_issue_comment_marker", self.find_comment),
            (gh, "ensure_label", lambda *args, **kw: ""),
            (gh, "default_branch", lambda repo: "main"),
            (poll, "network_up_or_skip", lambda *args: True),
            (poll, "mark_loop", lambda *args: None),
            (outcomes, "sync_outcomes", lambda cfg: None),
            (outcomes, "sync_moves", lambda cfg: None),
            (outcomes, "sync_landings", lambda cfg: None),
            (scratch, "process_ids", dict),
            (scratch, "service_labels", dict),
            (sandbox, "AUTHORITY", self.authority),
            (pipeline, "SKIPPED_FILE", self.root / "skipped.json"),
            (pipeline, "wanted", lambda repo: "classic"),
            (tasks, "spawn_pending", lambda repo, tid, launch: self.started.append(tid)),
        ):
            self.patches.enter_context(patch.object(module, name, func))
        with scratch.authority_fd(create=True) as fd:
            scratch.create_secret(fd, scratch.SECRET)
            self.key = scratch.read_secret(fd)

    def issue_view(self, repo, number):
        return {"number": number, "title": "Fixture issue", "body": "Fixture description", "state": "OPEN",
                "author": {"login": "owner"}, "labels": [{"name": label} for label in sorted(self.labels)]}

    def issue_list(self, repo, label, **kwargs):
        return [self.issue_view(repo, int(self.issue))] if label in self.labels else []

    def edit_labels(self, repo, number, add=(), remove=(), **kwargs):
        self.labels.difference_update(remove)
        self.labels.update(add)

    def post_comment(self, repo, number, body, **kwargs):
        self.comments.append(body)

    def post_api_comment(self, repo, number, body):
        self.comments.append(body)
        return str(len(self.comments)), f"https://github.com/{repo}/issues/{number}#issuecomment-{len(self.comments)}"

    def find_comment(self, repo, number, marker):
        return next(("fixture" for body in self.comments if marker in body.splitlines()), "")

    def run_failure(self, failure=None, script=None):
        from arete import outbox

        with patch.object(pipeline, "_run", side_effect=failure, wraps=pipeline._run):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.issue, script=script), 1)
        outbox.send_due()
        self.assertIn("agent:failed", self.labels)
        return run_store.newest_whole_run(self.repo.name, self.issue)

    def test_authenticated_scratch_settlement_requeues_before_picking(self):
        selector = "a" * 64
        data = {"version": 2, "state": "retained", "source_mutations": False, "diagnostic": {},
                "attempt": {"selector": selector, "workspace": str(self.ws),
                            "root": str(self.ws / (paths.SCRATCH_PREFIX + "fixture"))}}
        with scratch.authority_fd(create=True) as fd:
            scratch.store(fd, self.key, data)
        with patch.object(scratch, "recover_pending", return_value=[]):
            row = self.run_failure()
            self.assertEqual(json.loads(row["failure"])["selectors"], [selector])
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [])
        self.assertIn("agent:failed", self.labels)
        record_path = self.authority / (selector + ".json")
        record_path.unlink()
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [])
        self.assertIn("agent:failed", self.labels)

        def settle():
            with scratch.authority_fd() as fd:
                scratch.store(fd, self.key, {**data, "state": "done"})
                scratch.move_settled(fd, selector + ".json")
            return [data["attempt"]["root"]]

        with patch.object(scratch, "recover_pending", side_effect=settle):
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])
        self.assertNotIn("agent:failed", self.labels)
        self.assertIn("verification scratch settled", self.comments[-1])

    def test_pruned_scratch_requires_an_authenticated_receipt_when_tracker_returns(self):
        self.cfg.retention = config.Retention(days=0)
        selector = "b" * 64
        name = selector + ".json"
        data = {"version": 2, "state": "retained", "source_mutations": False, "diagnostic": {},
                "attempt": {"selector": selector, "workspace": str(self.ws),
                            "root": str(self.ws / (paths.SCRATCH_PREFIX + "fixture"))}}
        with scratch.authority_fd(create=True) as fd:
            scratch.store(fd, self.key, data)
        with patch.object(scratch, "recover_pending", return_value=[]):
            self.run_failure()
            with scratch.authority_fd() as fd:
                scratch.store(fd, self.key, {**data, "state": "done"})
                scratch.move_settled(fd, name)
            settled = self.authority / scratch.SETTLED / name
            os.utime(settled, (1, 1))
            with patch.object(sandbox, "can_confine", return_value=True), \
                    patch.object(poll, "network_up_or_skip", return_value=False):
                self.assertEqual(poll.poll(), 0)
                self.assertEqual(scratch.rotate(), (1, 0))
        self.assertFalse(settled.exists())
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [])
        receipt = self.authority / scratch.RECEIPTS / name
        original = receipt.read_bytes()
        receipt.write_text(json.dumps({"data": {**data, "state": "done"}, "signature": "forged"}))
        self.assertEqual(poll.poll(), 0)
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [])
        receipt.write_bytes(original)
        root = Path(data["attempt"]["root"])
        root.mkdir(parents=True)
        self.assertEqual(poll.poll(), 0)
        self.assertIn("agent:failed", self.labels)
        root.rmdir()
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])

    def test_disappearance_of_an_unidentifiable_scratch_record_does_not_requeue(self):
        unreadable = self.authority / "unidentifiable.json"
        unreadable.write_text("invalid scratch record")
        with patch.object(scratch, "recover_pending", return_value=[]):
            row = self.run_failure()
            unreadable.unlink()
            self.assertEqual(poll.poll(), 0)
        self.assertTrue(json.loads(row["failure"])["unidentifiable_record"])
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [])

    def test_dry_run_rows_cannot_override_live_environment_or_work_failures(self):
        def preview(cfg, run, slot):
            return pipeline._fail(run, "preview work failed")

        self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        with patch.object(pipeline, "_run", side_effect=preview):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.issue, dry_run=True), 1)
        dry = run_store.newest_whole_run(self.repo.name, self.issue)
        self.assertEqual((dry["dry_run"], dry["status"], dry["failure"]), (1, "failed", None))
        abandoned = record.start("pipeline", repo=self.repo.name, issue=self.issue, dry_run=1, pid=None)
        self.time += timedelta(minutes=5)
        comments = list(self.comments)
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(run_store.run_get(abandoned)["status"], "stopped")
        self.assertEqual(self.comments, comments)
        self.assertIn("agent:failed", self.labels)
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])

        self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        self.run_failure(workspace.RunFailed("checks failed"))
        with patch.object(pipeline, "_run", return_value=("dry-run", "preview passed", "")):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.issue, dry_run=True), 0)
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 0)
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [self.issue])

    def test_worktree_rechecks_the_existing_hold_and_dry_run_preserves_retry_state(self):
        self.ws.mkdir()
        held = self.ws / "owner-file"
        held.write_text("keep me")
        def drive(flow):
            return flows.ended(flow, flows.step(flow, "prepare", tree=str(self.ws)))
        row = self.run_failure(script=pipeline.Script("adw", drive))
        self.assertEqual(json.loads(row["failure"])["reason"], "worktree")
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [])
        self.assertEqual(held.read_text(), "keep me")
        held.unlink()
        comments = list(self.comments)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(poll.poll(dry_run=True), 0)
        self.assertIn("would requeue because the worktree is free", output.getvalue())
        self.assertEqual(self.comments, comments)
        self.assertEqual(self.started, [])
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(run_store.environment_requeue(row, util.iso(self.time - timedelta(days=1))), ({}, 0))
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])

    def test_crashes_require_a_changed_running_engine_and_work_or_legacy_stay_failed(self):
        row = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        self.assertEqual(json.loads(row["failure"])["reason"], "engine-crash")
        self.assertEqual(row["engine_sha"], "old-engine")
        self.assertEqual(poll.poll(), 0)
        with patch.object(codebase, "STAMP", ""):
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [])
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 0)
            self.assertEqual(self.started, [self.issue])
            for failure in ("checks failed", "verification failed", "review requested changes"):
                row = self.run_failure(workspace.RunFailed(failure))
                self.assertEqual(json.loads(row["failure"])["class"], "work")
                self.assertEqual(poll.poll(), 0)
                self.assertIn("agent:failed", self.labels)
            legacy = record.start("pipeline", repo=self.repo.name, issue=self.issue)
            record.settle(legacy, "failed", summary="crashed: Expecting value")
            self.assertEqual(poll.poll(), 0)
            self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [self.issue])

    def test_fourth_failure_is_capped_across_polls_and_ages_out(self):
        began = self.time
        for _ in range(3):
            self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
            with patch.object(codebase, "STAMP", "new-engine"):
                self.assertEqual(poll.poll(), 0)
            self.time += timedelta(hours=2)
        capped = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        def lost_reply(repo, number, body, **kwargs):
            self.post_comment(repo, number, body)
            raise gh.GhUnsure("reply lost after posting")
        with patch.object(gh, "issue_comment", side_effect=lost_reply):
            self.assertEqual(poll.poll(), 1)
        self.assertEqual(poll.poll(), 0)
        self.assertEqual(poll.poll(), 0)
        self.assertIn("agent:failed", self.labels)
        self.assertEqual(self.started, [self.issue] * 3)
        notices = [c for c in self.comments if "Leaving this issue failed" in c]
        self.assertEqual(len(notices), 1)
        self.assertIn("Arete engine crash", notices[0])
        entry, count = run_store.environment_requeue(capped, util.iso(self.time - timedelta(days=1)))
        self.assertEqual((entry["cap_notified"], count), (1, 3))
        self.time = began + timedelta(hours=24)
        with patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue] * 4)

    def test_rejected_label_edits_do_not_count_and_lost_replies_reconcile_once(self):
        label = "agent:failed"

        def reject(repo, number, add=(), remove=(), **kwargs):
            self.assertEqual(list(remove), [label])
            raise gh.GhError("label edit rejected")

        def lost_reply(repo, number, add=(), remove=(), **kwargs):
            self.assertTrue(kwargs["single_attempt"])
            self.edit_labels(repo, number, add, remove)
            raise gh.GhUnsure("reply lost after removing the label")

        since = util.iso(self.time - timedelta(days=1))
        for _ in range(3):
            rejected = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
            with patch.object(gh, "issue_labels", side_effect=reject), patch.object(codebase, "STAMP", "new-engine"):
                self.assertEqual(poll.poll(), 1)
            self.assertIn(label, self.labels)
            entry, count = run_store.environment_requeue(rejected, since)
            self.assertEqual((entry["edit_pending"], entry["requeued"], count), (None, 0, 0))
        self.assertFalse(any("environment-requeue" in body for body in self.comments))

        accepted = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        with patch.object(gh, "issue_labels", side_effect=gh.GhUnsure("reply lost before removal")), \
                patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 1)
        self.assertIn(label, self.labels)
        self.assertTrue(run_store.environment_requeue(accepted, since)[0]["edit_pending"])
        with patch.object(gh, "issue_labels", side_effect=lost_reply) as edit, \
                patch.object(codebase, "STAMP", "new-engine"):
            self.assertEqual(poll.poll(), 1)
            pending, _ = run_store.environment_requeue(accepted, since)
            self.assertEqual(pending["requeued"], 0)
            self.assertTrue(pending["edit_pending"])
            self.assertNotIn(label, self.labels)
            self.assertEqual(self.started, [])
            self.assertEqual(poll.poll(dry_run=True), 0)
            self.assertEqual(run_store.environment_requeue(accepted, since), (pending, 0))
            self.assertEqual(poll.poll(), 0)
            self.assertEqual(poll.poll(), 0)
            edit.assert_called_once()

        next_run = self.run_failure(json.JSONDecodeError("Expecting value", "", 0))
        self.assertEqual(run_store.environment_requeue(next_run, since), ({}, 1))
        self.assertEqual(sum("environment-requeue" in body for body in self.comments), 1)
        self.assertIn(self.issue, self.started)

    def test_adaptive_finish_uses_the_blocking_build_result(self):
        startup = Result(ok=False, runner="claude", error="claude not found: fixture")
        recovery = pipeline.runner.start_failure(startup, "build")
        build_start = ("worker.start", "failed", {"writing": True, "event": "result", "recovery": recovery})
        unrelated = ("worker.start", "failed", {"writing": False, "event": "result", "recovery": recovery})
        for actions, expected in (
            ([build_start], {"class": "environment", **recovery}),
            ([build_start, unrelated], {"class": "environment", **recovery}),
            ([unrelated], {"class": "work"}),
            ([build_start, ("worker.continue", "failed", {"writing": True, "event": "result"})], {"class": "work"}),
            ([build_start, ("checks.run", "succeeded", {"passed": False})], {"class": "work"}),
            ([build_start, ("verification.run", "succeeded", {"operation_passed": True, "verdict": "fail"})],
             {"class": "work"}),
        ):
            with self.subTest(actions=actions):
                def drive(flow):
                    controller = orchestration.Controller(flow)
                    lease = controller.lease
                    turn = ledger.begin(lease, {}, 1)
                    version = ledger.snapshot(lease)["version"]
                    accepted = ledger.accept(lease, turn, version,
                                             {"calls": [{"op": op, "args": {}} for op, _, _ in actions]}, 10)
                    for action, (_, state, result) in zip(accepted, actions, strict=True):
                        ledger.transition(lease, action["id"], state, result)
                    controller.perform({"op": "run.finish", "request": {"reason": "failed"}})
                    return controller.ending

                row = self.run_failure(script=pipeline.Script("adw", drive))
                self.assertEqual(json.loads(row["failure"]), expected)

    def test_runner_start_failure_requires_a_successful_probe(self):
        failure = Result(ok=False, runner="claude", error="claude not found: fixture")
        def drive(cfg, run, slot):
            return pipeline._fail(run, "planning step failed",
                                  recovery=pipeline.runner.start_failure(failure, "plan"))
        row = self.run_failure(drive)
        self.assertEqual(json.loads(row["failure"])["reason"], "runner-start")
        with patch.object(pipeline.runner, "started_now", side_effect=[False, True]) as probe:
            self.assertEqual(poll.poll(), 0)
            self.assertIn("agent:failed", self.labels)
            self.assertEqual(poll.poll(), 0)
        self.assertEqual([call.args for call in probe.call_args_list], [("claude",), ("claude",)])
        self.assertEqual(self.started, [self.issue])

    def test_rejected_login_requires_a_ready_route_with_renewed_credentials(self):
        failure = Result(ok=False, runner="claude", model="sonnet", credential="cli_login", turns=1,
                         raw={"api_error": "authentication_failed"}, auth_generation="stale-login")
        def drive(cfg, run, slot):
            return pipeline._fail(run, "the conflict resolution step failed",
                                  recovery=pipeline.runner.start_failure(failure, "build"))
        row = self.run_failure(drive)
        self.assertEqual(json.loads(row["failure"])["reason"], "runner-login")
        route = SimpleNamespace(state="ready", runner="claude", model="sonnet", credential="cli_login")
        with patch.object(pipeline.routing, "Discovery", return_value=SimpleNamespace(routes=lambda _: [route])), \
                patch.object(pipeline.runner, "authentication", side_effect=[
                    SimpleNamespace(generation="stale-login"), SimpleNamespace(generation="new-login")]):
            self.assertEqual(poll.poll(), 0)
            self.assertIn("agent:failed", self.labels)
            self.assertEqual(poll.poll(), 0)
        self.assertEqual(self.started, [self.issue])

    def test_sandbox_start_checks_confinement_before_requeueing(self):
        row = self.run_failure(RuntimeError(pipeline.runner.SANDBOX_START_ERROR))
        self.assertEqual(json.loads(row["failure"])["reason"], "sandbox-start")
        with patch.object(sandbox, "can_confine", return_value=True), \
                patch.object(sandbox, "write_confinement", return_value=["sandbox-exec"]), \
                patch.object(util, "run_whole_tree", side_effect=[
                    SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]) as probe:
            self.assertEqual(poll.poll(), 0)
            self.assertIn("agent:failed", self.labels)
            self.assertEqual(poll.poll(), 0)
        self.assertEqual([call.args[0] for call in probe.call_args_list], [["sandbox-exec", "/usr/bin/true"]] * 2)
        self.assertEqual(self.started, [self.issue])


class RouteABTests(unittest.TestCase):
    def configured(self, **test):
        return config.Config(steps={"plan": {"model": "opus"}}, ab={"cheap-plan": {
            "step": "plan", "kind": "", "share": 0.5, "arm": {"model": "haiku"}, **test}})

    def sides(self, cfg, kind="feat"):
        return [steps.resolve(cfg, None, "plan", kind, ticket=("fixture", str(n))) for n in range(1, 41)]

    def test_share_decides_a_stable_side_per_ticket(self):
        cfg = self.configured()
        first, second = self.sides(cfg), self.sides(cfg)
        self.assertTrue(12 <= sum(s.ab_side == "arm" for s in first) <= 28)
        self.assertEqual([s.ab_side for s in first], [s.ab_side for s in second])
        self.assertEqual({(s.ab_side, s.model) for s in first}, {("arm", "haiku"), ("baseline", "opus")})
        self.assertEqual({s.ab_name for s in self.sides(self.configured(kind="feat"), "fix")}, {""})


if __name__ == "__main__":
    unittest.main()
