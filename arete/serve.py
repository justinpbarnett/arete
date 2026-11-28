from __future__ import annotations

import http.client
import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Future, wait
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import NoReturn, TextIO

from . import (
    alerts,
    factory_control,
    backup,
    capture,
    chat,
    codebase,
    config,
    control,
    dashboard,
    dbstats,
    delivery,
    devices,
    environment,
    explain,
    gh,
    hold_review,
    install,
    leases,
    loop_run,
    loops,
    oncall,
    outbox,
    paths,
    pilot,
    principals,
    record,
    release_proof,
    retention,
    routing,
    runner,
    scratch,
    secret_move,
    secretstore,
    serve_guard,
    sessions,
    slots,
    sources,
    startup_excerpt,
    tasks,
    tool_events,
    tracker,
    util,
    webpush,
)
from .loops import Loop
from .store import approvals as approvals_store
from .store import db as foundation
from .store import factory as factory_state
from .store import incidents as incident_store
from .store import loops as loop_store
from .store import retention as retention_store
from .store import runs as run_store
from .store import sources as source_store
from .store import workspaces

TICK_SECONDS = 20
DEPLOY_SECONDS = 120
DEPLOY_API_TIMEOUT = 20
PILOT_LOCK = "pilot-doc-fix"
PRUNE_SECONDS = 3600
ROTATE_SECONDS = 3600
SECRET_MOVE_SECONDS = 3600
SECRET_CHECK_SECONDS = 86400
APPROVAL_RETRY_SECONDS = 900
SWEEP_SECONDS = 300
WORKSPACE_SWEEP_SECONDS = 600
CLOSED_SECONDS = 300
POLL_THREADS = 8


def mergecheck_reset(loop: Loop, item: dict) -> str:
    from . import mergecheck

    payload = loop_store.queue_item(item["id"])
    names = {e.get("repo", "") for e in payload["payload"]} if payload else set()
    return max((mergecheck.held_until(loop.dir, name) for name in names), default="")


IMPORT_CHECK = "import arete.cli, arete.serve, arete.dashboard, arete.loop_run; from arete import codebase; codebase.load()"
CONFIG_CHECK = (
    "import json; from dataclasses import asdict, is_dataclass; from arete import config; "
    "cfg = config.load(); "
    "print(json.dumps([asdict(p) if is_dataclass(p) else str(p) for p in getattr(cfg, 'problems', ())], default=str))"
)
RESTARTED_FOR_NEW_CODE = foundation.INHERITED_SERVE_CHILDREN
SERVE_LOCK = "serve"
SERVE_LOCK_FD = serve_guard.LOCK_FD
ALERT_TICKS = 9
ALERT_KEY = "serve:tick"
ALERT_STAGES = ("tick", "tick:load", "stage:dispatch")


@dataclass(frozen=True)
class ApprovalJob:
    note: str
    while_running: str
    not_written: str
    model_for: Callable[[dict, config.Config], str]
    write: Callable[[dict, config.Config], str]
    tools: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()


APPROVAL_JOBS = (
    ApprovalJob("plain_summary", "a plain summary of an approval is still being written", "no plain summary",
                model_for=lambda approval, cfg: cfg.model("explain"), write=explain.write_plain_summary),
    ApprovalJob("review", "a held push is still being reviewed", "no review",
                model_for=hold_review.model_for, write=hold_review.review,
                tools=tuple(hold_review.REVIEW_TOOLS), denied=tuple(hold_review.REVIEW_DENIED)),
)


def inherited_lock() -> TextIO | None:
    value = os.environ.pop(SERVE_LOCK_FD, None)
    if value is None:
        return None
    held = None
    try:
        held = os.fdopen(int(value), "a")
        os.set_inheritable(held.fileno(), False)
        inherited = os.fstat(held.fileno())
        expected = (paths.LOCKS_DIR / f"{SERVE_LOCK}.lock").stat()
        if (inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("descriptor does not name the serve lock")
    except (OSError, OverflowError, ValueError) as e:
        if held is not None:
            held.close()
        print(f"cannot adopt {SERVE_LOCK_FD}={value!r}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    return held


def take_serve_lock() -> TextIO:
    if (held := inherited_lock()) is not None:
        return held
    held = util.lock_file(SERVE_LOCK)
    try:
        if util.take_lock(held, wait=False):
            return held
        holder = (paths.LOCKS_DIR / f"{SERVE_LOCK}.lock").read_text().strip()
        print(f"arete serve {holder} already holds state folder {paths.STATE_DIR}", file=sys.stderr)
        raise SystemExit(1)
    except BaseException:
        held.close()
        raise


def next_due(loop: Loop, after: datetime) -> str:
    t = loop.trigger
    if t.every:
        return util.iso(after + timedelta(seconds=t.every))
    return util.iso(t.cron.next_fire(after).astimezone(after.tzinfo)) if t.cron else ""


def code_stamp() -> frozenset:
    stamp = set()
    for p in [*paths.PACKAGE_DIR.rglob("*.py"), *dashboard.web_files(),
              *codebase.config_data_files()]:
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        stamp.add((str(p), st.st_mtime_ns, st.st_size))
    if head := codebase.checkout_head():
        stamp.add(("HEAD", head))
    return frozenset(stamp)


def deploy_refusal(src: Path, base: str, tip: str) -> str | None:
    if serve_guard.hold_path(paths.STATE_DIR, tip).exists():
        return "held after startup rollback"
    head = util.git(["rev-parse", "--verify", "HEAD"], src, timeout=10, retry=False).strip()
    if head == tip:
        return None
    if util.git(["branch", "--show-current"], src, timeout=10, retry=False).strip() != base:
        return "on another branch"
    if util.git(["--no-replace-objects", "status", "--porcelain", "--untracked-files=no",
                 "--ignore-submodules=untracked"], src, timeout=10, retry=False):
        return "dirty"
    ancestor = util.git(["--no-replace-objects", "merge-base", head, tip], src,
                        check=False, timeout=10, retry=False).strip()
    return "" if ancestor == head else "diverged"


def pilot_deploy_refusal() -> str:
    try:
        tasks = list((paths.STATE_DIR / PILOT_LOCK).iterdir())
    except FileNotFoundError:
        return ""
    except OSError:
        return "pilot receipt unavailable"
    for task in tasks:
        try:
            receipt = json.loads(paths.read_confined(task / "receipt.json"))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, ValueError):
            return "pilot receipt unavailable"
        if not isinstance(receipt, dict) or receipt.get("phase") not in ("held", "healthy", "spent"):
            return "live pilot receipt"
    return ""


def dashboard_answers() -> bool:
    server = util.read_json(paths.SERVER_FILE, {})
    if (not isinstance(server, dict) or server.get("pid") != os.getpid() or server.get("engine") != codebase.STAMP
            or server.get("scheme") not in ("http", "https") or not server.get("addresses")
            or {k: server.get(k) for k in ("state", "config")} != paths.state_and_config()):
        return False
    try:
        host, port = server["addresses"][0], server["port"]
        conn = (util.NamedTlsConnection(host, port, server["host"], 2) if server["scheme"] == "https"
                else http.client.HTTPConnection(host, port, timeout=2))
        with closing(conn):
            conn.request("GET", dashboard.READY_PATH, headers={dashboard.READY_HEADER: dashboard.READY_TOKEN})
            with conn.getresponse() as response:
                return response.status == 204 and response.getheader("X-Arete-Engine") == codebase.STAMP
    except (OSError, http.client.HTTPException, IndexError, KeyError, TypeError, ValueError):
        return False


def report_rollbacks(src: Path, say: Callable[[str], None]) -> None:
    holds = sorted((paths.STATE_DIR / serve_guard.HOLDS).glob("*.json"))
    if not holds or not (head := codebase.checkout_head()):
        return
    for path in holds:
        try:
            held = serve_guard.read(path)
            if (not held or held["checkout"] != str(src) or held.get("issue")
                    or held["tip"][:12] == head):
                continue
            repo, tip, previous = held["github"], held["tip"], held["previous"]
            if not all(serve_guard.SHA.fullmatch(sha) for sha in (tip, previous)):
                continue
            key = f"serve-rollback-{tip}"
            issue = None
            if held.get("report_started"):
                issue = next((row["number"] for row in gh.recent_issues(repo, limit=20)
                              if gh.key_mark(key) in (row.get("body") or "")), None)
            else:
                held["report_started"] = True
                serve_guard.write(path, held)
            if issue is None:
                excerpt = held["log_tail"]
                if excerpt != serve_guard.MISSING_TAIL:
                    tracked = serve_guard.git(src, "ls-tree", "-r", "--name-only", tip).splitlines()
                    excerpt = startup_excerpt.redacted(excerpt, str(src), frozenset(tracked), (tip, previous))
                body = (f"Arete rolled back self-deploy code from `{tip[:12]}` to `{previous[:12]}` because "
                        f"{serve_guard.report_reason(held)}. Deploys of `{tip[:12]}` are held.\n\n"
                        f"Redacted serve.log tail\n\n```text\n{excerpt}\n```\n\n{gh.key_mark(key)}")
                issue = gh.issue_create(repo, f"{tracker.ARETE_TAG} Serve self-deploy {tip[:12]} rolled back",
                                        body, [tracker.LABEL])
            serve_guard.write(path, {**held, "issue": issue})
            say(f"deploy: reported rollback of {tip} in {gh.issue_url(repo, issue)}")
        except (OSError, RuntimeError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as error:
            say(f"deploy: could not report rollback: {util.first_line(str(error), 300)}")


def reap_children() -> None:
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass


def child_pids() -> set[int]:
    found = util.sh(["pgrep", "-P", str(os.getpid())], timeout=10)
    return {int(p) for p in found.stdout.split()} if found.returncode == 0 else set()


def restart_children() -> foundation.ServeChildren:
    reap_children()
    children = set()
    for pid in child_pids():
        identity = foundation.process_identity(pid)
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == 0:
                confirmed = foundation.process_identity(pid)
                children.add((pid, identity if identity == confirmed else None))
        except ChildProcessError:
            continue
    return children


def is_due(loop: Loop, last_fire: str, now: datetime) -> bool:
    last = util.parse_iso(last_fire)
    if loop.trigger.every:
        return (now - last).total_seconds() >= loop.trigger.every
    return loop.trigger.cron.next_fire(last) <= now.astimezone()


class QueueSlots:
    def __init__(self, marked_running: list[dict]) -> None:
        running = [r for r in marked_running if r["kind"] == "loop"]
        self.per_queue = Counter(r["queue"] or r["loop"] for r in running)
        self.held_groups = {(r["queue"] or r["loop"], r["grp"]) for r in running if r["grp"]}
        self.busy = {r["loop"] for r in running}

    def why_full(self, item: dict, loop: Loop, cfg: config.Config) -> str:
        queue = loop.queue_name
        size = cfg.queues.get(queue, 1)
        if item["grp"] and (queue, item["grp"]) in self.held_groups:
            return f"group {item['grp']} takes one at a time"
        if loop.name in self.busy:
            return str(control.Busy(loop.name))
        if self.per_queue[queue] >= size:
            return f"queue {queue} is full at {util.count(size, 'run')}"
        return ""

    def took(self, loop: Loop, item: dict) -> None:
        self.busy.add(loop.name)
        self.per_queue[loop.queue_name] += 1


def _problem_identity(entry: object) -> tuple[str, str]:
    if isinstance(entry, config.Problem):
        key = json.dumps([entry.path, entry.section, entry.name, entry.value, entry.scope],
                         sort_keys=True, default=str)
        return key, entry.text()
    if isinstance(entry, dict):
        problem = config.Problem(
            str(entry.get("path", "")),
            str(entry.get("section", "")),
            str(entry.get("name", "")),
            entry.get("value"),
            str(entry.get("scope", "")),
        )
        key = json.dumps([problem.path, problem.section, problem.name, problem.value, problem.scope],
                         sort_keys=True, default=str)
        return key, problem.text()
    text = str(entry)
    return text, text


def candidate_config_problem(candidate, running_cfg: config.Config, running_fatal: str) -> str:
    if running_fatal:
        return ""
    stderr = getattr(candidate, "stderr", "") or ""
    stdout = getattr(candidate, "stdout", "") or ""
    if candidate.returncode != 0:
        return ("new code cannot read config.toml, so serve keeps the code it started with: "
                + util.last_line(stderr or stdout, 300))
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return ""
    try:
        parsed = json.loads(lines[-1])
    except ValueError:
        return "new code returned unreadable config check output, so serve keeps the code it started with"
    if not isinstance(parsed, list):
        return "new code returned unreadable config check output, so serve keeps the code it started with"
    running_problems = getattr(running_cfg, "problems", ())
    running_ids = {_problem_identity(p)[0] for p in running_problems}
    running_texts = {p.text() for p in running_problems}
    added: list[str] = []
    for entry in parsed:
        key, text = _problem_identity(entry)
        if key not in running_ids and text not in running_texts:
            added.append(text)
    if not added:
        return ""
    return ("new code reports config reach problems this engine does not have, "
            "so serve keeps the code it started with: " + "; ".join(added))


class Server:
    def __init__(self) -> None:
        self.cfg, self.config_problem = config.load_or_problem()
        self.code_problem = ""
        self.reported_problem = ""
        self.reported_problems: tuple[config.Problem, ...] = ()
        self.reported_ignored: tuple[str, ...] = ()
        self.lock: TextIO | None = None
        self.started = util.now()
        self.polls: dict[str, Future] = {}
        self.sweep: Future | None = None
        self.workspace_sweep: Future | None = None
        self.oncall: Future | None = None
        self.deployment: Future | None = None
        self.deploy_at = time.monotonic() - DEPLOY_SECONDS
        self.with_dashboard = True
        self.deploy_refusals: set[tuple[str, str]] = set()
        self.checkout_update = threading.Lock()
        self.zte_work: Future | None = None
        self.unblock_work: Future | None = None
        self.unblock_at = 0.0
        self.zte_at = 0.0
        self.oncall_error = ""
        self.tried: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.waiting: dict[int, str] = {}
        self.pool = factory_control.WorkPool(POLL_THREADS, thread_name_prefix="poll")
        self.db_future: Future | None = None
        self.db_lock = threading.Lock()
        self.db_health = {**dbstats.empty_collection(), "status": "initial", "requested_at": None,
                          "finished_at": None, "error": None}
        restarted = RESTARTED_FOR_NEW_CODE in os.environ
        self.schedule_resumed = ""
        self.loaded = restarted
        self.workspace_contract_children = foundation.live_serve_children(foundation.inherited_serve_children())
        self.inherited = {pid for pid, _ in self.workspace_contract_children}
        os.environ.pop(RESTARTED_FOR_NEW_CODE, None)
        os.environ.pop(foundation.INHERITED_SERVE_IDENTITIES, None)
        self.pruned = self.swept = self.workspaces_swept = self.rotated = 0.0
        self.checked_closed = 0.0
        self.secret_moved_at: float | None = None
        self.secrets_checked_at: float | None = None
        self.secret_migration: dict = {}
        self.ticks = 0
        self.reloaded = restarted
        self.failing: dict[str, dict] = {}
        self.logged: dict[str, str] = {}
        self.every_loop: list[Loop] = []
        self.live: list[Loop] = []
        self.config_good = False
        self.last_ok: str | None = None
        self.alerted: dict[str, str] = {}
        self.alert_open = False
        self.alert_checked = False
        self.alert_last: Future | None = None
        self.code_seen = code_stamp()
        self.started_head = next((entry[1] for entry in self.code_seen if entry[0] == "HEAD"), "")
        self.code = self.code_seen
        if codebase.RELEASE and codebase.RELEASE[:12] != self.started_head:
            receipt = serve_guard.pending(paths.STATE_DIR, paths.PACKAGE_DIR.parent)
            if receipt is None or not receipt.starts or receipt.tip[:12] != self.started_head:
                self.code = frozenset(entry for entry in self.code if entry[0] != "HEAD") | {
                    ("HEAD", codebase.RELEASE[:12])}
        self.code_note = ""
        self.migration_stamp = None
        self.migration_error = ""
        self.authority_problem = ""
        self.factory_suspended = False
        self.contracts_waiting = False
        self.derived_started = False
        self.job_pools = {job.note: factory_control.WorkPool(1, thread_name_prefix=job.note) for job in APPROVAL_JOBS}
        self.jobs: dict[str, Future] = {}
        self.outbox_pool = factory_control.WorkPool(1, thread_name_prefix="outbox")
        self.outbox_job: Future | None = None

    def say(self, text: str) -> None:
        print(f"{util.iso()} {text}", flush=True)

    def rotate(self) -> None:
        for note in install.rotate_logs():
            self.say(note)

    def read_config(self) -> None:
        self.cfg, self.config_problem = config.load_or_problem()
        self.report_config()

    def report_config(self) -> None:
        if self.config_problem != self.reported_problem:
            if self.config_problem:
                self.say(f"config.toml does not load, so no loop fires until it does: {self.config_problem}")
            else:
                self.say("config.toml loads again")
            self.reported_problem = self.config_problem
        cfg_problems = getattr(self.cfg, "problems", ())
        if cfg_problems != self.reported_problems:
            if cfg_problems and not self.config_problem:
                self.say("config has reach problems, pausing affected launches: "
                         + "; ".join(p.text() for p in cfg_problems))
            elif self.reported_problems and not self.config_problem:
                self.say("config reach problems cleared")
            self.reported_problems = cfg_problems
        if self.cfg.ignored != self.reported_ignored:
            if self.cfg.ignored:
                self.say(f"config.toml has names this engine does not know, so it ignores them: "
                         f"{', '.join(self.cfg.ignored)}")
            self.reported_ignored = self.cfg.ignored

    def guard(self, key: str, fn: Callable, *args):
        try:
            with principals.acting(f"rule:{key[5:]}" if key.startswith("loop:") else "hand"):
                result = fn(*args)
        except foundation.NoDatabase:
            raise
        except Exception as e:
            error = f"{type(e).__name__}: {util.first_line(str(e), 300)}"
            known = self.failing.get(key)
            self.failing[key] = {"error": error, "since": known["since"] if known else util.iso(),
                                 "ticks": (known["ticks"] if known else 0) + 1}
            if self.logged.get(key) != error:
                self.logged[key] = error
                self.say(f"{key}: tick failed: {error}")
            return None
        if known := self.failing.pop(key, None):
            self.logged.pop(key, None)
            self.say(f"{key}: recovered after {known['ticks']} ticks")
        return result

    def settle(self, prefix: str, present: set[str]) -> None:
        for key in [k for k in self.failing if k.startswith(prefix) and k[len(prefix):].split(":")[0] not in present]:
            self.failing.pop(key)
            self.logged.pop(key, None)

    def load(self) -> bool:
        cfg, problem = config.load_or_problem()
        self.config_problem = problem
        if not problem or not self.config_good:
            self.cfg = cfg
        self.report_config()
        if problem:
            self.guard("stage:inbox-config", self.sync_config_inbox)
            raise ValueError(problem)
        every_loop = loops.load_all(cfg)
        self.every_loop = every_loop
        self.live = [x for x in every_loop if x.enabled and not x.error]
        self.config_good = True
        return True

    def start_derived(self) -> list[dict]:
        from . import derived

        rebuilt = derived.rebuild_stale_on_startup(contracts_waiting=self.contracts_waiting, say=self.say)
        self.derived_started = True
        return rebuilt

    def tick(self) -> bool:
        if self.contracts_waiting:
            if not paths.DB_FILE.is_file() or not paths.DB_FILE.stat().st_size:
                raise foundation.missing_database()
            self.contracts_waiting = foundation.migrate()
        foundation.ensure_schema()
        if not self.contracts_waiting and not self.derived_started:
            self.guard("stage:derived-start", self.start_derived)
        if not self.guard("tick:load", self.load) or self.code_problem:
            return False
        if self.contracts_waiting:
            return False
        state = factory_state.view() if factory_state.available() else {"phase": "on", "version": 1}
        self.factory_suspended = state["phase"] != "on"
        if self.factory_suspended:
            return True
        with factory_control.activity("scheduler", state["version"]):
            return self.run_scheduled_work(state)

    def run_scheduled_work(self, state) -> bool:
        if state["version"] > 1 and self.schedule_resumed != state["version"]:
            resumed = util.parse_iso(state["changed"])
            for loop in self.live:
                if loop.trigger.cron or loop.trigger.every:
                    old = loop_store.loop_state(loop.name)
                    if not old.get("last_fire") or util.parse_iso(old["last_fire"]) < resumed:
                        loop_store.set_loop_state(loop.name, last_fire=state["changed"], next_due=next_due(loop, resumed))
            self.loaded = True
            self.schedule_resumed = state["version"]
        self.guard("stage:secret-move", self.move_secrets_when_due)
        self.guard("stage:secret-check", self.check_secrets_when_due)
        self.guard("stage:leases", self.lapse_leases)
        self.guard("stage:reap", self.reap)
        self.guard("stage:claims", self.release_claims)
        states = self.guard("stage:states", loop_store.loop_states)
        if states is not None:
            now = util.now()
            for loop in self.live:
                self.guard(f"loop:{loop.name}", self.step_loop, loop, states.get(loop.name) or {}, now)
            self.settle("loop:", {x.name for x in self.live})
            self.loaded = True
        stages = (
            ("forget", self.forget_unpolled_errors, (self.live,)),
            ("oncall", self.watch_oncall, ()), ("zte", self.zte_when_due, ()),
            ("unblock", self.unblock_when_due, ()),
            ("dispatch", self.dispatch, ({x.name: x for x in reversed(self.every_loop)},)),
            ("approvals", self.approvals, ()), ("rotate", self.rotate_when_due, ()),
            ("prune", self.prune_when_due, ()), ("sweep", self.sweep_when_due, ()),
            ("workspaces", self.sweep_workspaces_when_due, ()),
            ("asks", self.expire_asks, ()), ("closed", self.release_closed_when_due, ()),
            ("inbox", self.sync_inbox, ()),
            ("watch", self.watch_drafts, ()),
            ("remind", self.remind, ()),
        )
        for name, action, args in stages:
            factory_state.admit()
            self.guard(f"stage:{name}", action, *args)
        tick_stages = {"stage:leases", "stage:reap", "stage:claims", "stage:secret-move", "stage:secret-check",
                       *(f"stage:{name}" for name, _, _ in stages)}
        return states is not None and not any(key in tick_stages or key.startswith(("loop:", "secret-check:"))
                                              for key in self.failing)

    def move_secrets_when_due(self) -> None:
        now = time.monotonic()
        if not config.secret_settings().store:
            self.secret_moved_at = None
            self.secret_migration = {}
            return
        if self.secret_moved_at is not None and now - self.secret_moved_at < SECRET_MOVE_SECONDS:
            return
        prior = self.secret_migration
        if prior.get("retired") and not prior.get("pending") and (
                prior.get("push_registered") or not paths.PUSH_KEY_FILE.exists()):
            result = dict(prior)
        else:
            if not self.checkout_update.acquire(blocking=False):
                return
            try:
                if serve_guard.pending(paths.STATE_DIR, paths.PACKAGE_DIR.parent) is not None:
                    return
                result = secretstore.move_files()
            finally:
                self.checkout_update.release()
            if result is None:
                return
        if result.get("retired"):
            try:
                result["ignored"] = secret_move.ignored_notes(config.read_data())
            except FileNotFoundError:
                result["ignored"] = []
        previous = {entry for key in ("notes", "problems", "ignored") for entry in prior.get(key, ())}
        for key in ("notes", "problems", "ignored"):
            for entry in result.get(key, ()):
                if entry not in previous:
                    self.say(entry)
        self.secret_moved_at = now
        self.secret_migration = result

    def check_secrets_when_due(self) -> None:
        now = time.monotonic()
        if not config.secret_settings().store:
            self.secrets_checked_at = None
            self.settle("secret-check:", set())
            return
        if self.secrets_checked_at is not None and now - self.secrets_checked_at < SECRET_CHECK_SECONDS:
            return
        rows = [row for row in secretstore.items() if row["status"] == "set"]
        self.settle("secret-check:", {str(row["id"]) for row in rows})
        for row in rows:
            self.guard(f"secret-check:{row['id']}", secretstore.check, row["scope"], row["name"])
        self.secrets_checked_at = now

    def lapse_leases(self) -> None:
        lapsed = leases.lapse()
        if lapsed:
            self.say(f"lapsed {util.count(len(lapsed), 'expired lease')}")

    def reap(self) -> None:
        recover_queued(self.say)
        for row in record.reap(tickets=False, release_claims=False):
            self.say(f"{row['id']}: {row['status']}, {record.INTERRUPTED}")
            self.swept = 0.0
        sessions.tell_chats()

    def release_claims(self) -> None:
        with foundation.tx() as c:
            loop_store.release_settled_claims(c)

    def step_loop(self, loop: Loop, st: dict, now: datetime) -> None:
        self.schedule(loop, st, now)
        self.poll(loop)

    def zte_when_due(self) -> None:
        from . import zte, zte_delivery

        if self.zte_work is not None:
            if not self.zte_work.done():
                return
            finished, self.zte_work = self.zte_work, None
            finished.result()
        if time.monotonic() - self.zte_at >= zte.INTERVAL:
            self.zte_at = time.monotonic()
            self.zte_work = self.pool.submit(zte_delivery.tick, self.cfg)

    def unblock_when_due(self) -> None:
        from . import unblock, unblock_watch

        if self.unblock_work is not None:
            if not self.unblock_work.done():
                return
            finished, self.unblock_work = self.unblock_work, None
            finished.result()
        if time.monotonic() - self.unblock_at >= unblock.INTERVAL:
            self.unblock_at = time.monotonic()
            self.unblock_work = self.pool.submit(unblock_watch.watch, self.cfg)

    def approvals(self) -> None:
        for released in delivery.release_lapsed():
            self.say(f"approval #{released}: {delivery.cut_off('claim expired')}")
        self.start_approval_jobs()
        self.start_outbox_job()

    def rotate_when_due(self) -> None:
        if time.monotonic() - self.rotated > ROTATE_SECONDS:
            self.rotated = time.monotonic()
            self.rotate()

    def prune_when_due(self) -> None:
        if time.monotonic() - self.pruned > PRUNE_SECONDS:
            self.prune()
            self.pruned = time.monotonic()

    def sweep_when_due(self) -> None:
        if time.monotonic() - self.swept > SWEEP_SECONDS and not self.sweeping():
            self.sweep = self.pool.submit(self.sweep_vms)
            self.swept = time.monotonic()

    def sweep_workspaces_when_due(self) -> None:
        now = time.monotonic()
        if now - self.workspaces_swept > WORKSPACE_SWEEP_SECONDS and not self.sweeping_workspaces():
            self.workspace_sweep = self.pool.submit(self.sweep_workspaces)
            self.workspaces_swept = now

    def expire_asks(self) -> None:
        for note in tasks.expire_asks(self.cfg):
            self.say(note)

    def release_closed_when_due(self) -> None:
        if time.monotonic() - self.checked_closed > CLOSED_SECONDS:
            self.checked_closed = time.monotonic()
            for note in tasks.release_closed(self.cfg):
                self.say(note)

    def sync_inbox(self) -> None:
        from . import inbox

        inbox.tick()

    def watch_drafts(self) -> None:
        from . import drafts

        drafts.watch_tick()

    def sync_config_inbox(self) -> None:
        from . import inbox

        inbox.sync_source("config")

    def remind(self) -> None:
        for note in webpush.remind_inbox(self.cfg):
            self.say(note)

    def run_pass(self) -> None:
        if self.checkout_update.acquire(blocking=False):
            try:
                self.guard("stage:code", self.load_new_code)
            finally:
                self.checkout_update.release()
        completed = self.guard("tick", self.tick)
        self.guard("stage:inherited", self.reap_inherited)
        self.ticks += 1
        if self.contracts_waiting or self.factory_suspended:
            self.beat()
            return
        self.guard("stage:alerts", self.watch_alerts)
        if self.checkout_update.acquire(blocking=False):
            try:
                self.guard("stage:deploy-health", self.acknowledge_deploy, completed)
            finally:
                self.checkout_update.release()
        self.guard("stage:deploy", self.deploy_when_due)
        try:
            self.beat()
        except Exception as e:
            self.say(f"beat failed: {type(e).__name__}: {e}")

    def deploy_when_due(self) -> None:
        if self.deployment is not None:
            if not self.deployment.done():
                return
            finished, self.deployment = self.deployment, None
            finished.result()
        now = time.monotonic()
        if now - self.deploy_at >= DEPLOY_SECONDS and not self.config_problem and not self.contracts_waiting:
            self.deploy_at = now
            self.deployment = self.pool.submit(self.deploy_if_ready, self.cfg)

    def acknowledge_deploy(self, completed: bool | None) -> None:
        checkout = paths.PACKAGE_DIR.parent
        receipt = serve_guard.pending(paths.STATE_DIR, checkout)
        if receipt is None:
            if completed:
                release_proof.confirm_restore(self.cfg.arete_repo, checkout, self.started_head, completed)
            return
        if (not receipt.starts or self.started_head != receipt.tip[:12]
                or serve_guard.hold_path(paths.STATE_DIR, receipt.tip).exists()
                or not serve_guard.at_tip(checkout, receipt)):
            return
        if completed and (not self.with_dashboard or dashboard_answers()):
            if (paths.STATE_DIR / "release-checks").is_dir():
                repo = self.cfg.repo_for_path(checkout)
                if repo and (held := release_proof.reconcile_running(repo, receipt)):
                    self.note_once(f"deploy: {receipt.tip} waiting for its omitted protected check ({held})")
                    return
            if receipt.release_checks:
                verdict = release_proof.assess(self.cfg.arete_repo, receipt)
                if verdict != "verified":
                    if verdict == "failed" and not self.busy_with():
                        release_proof.restore_or_hold(checkout, receipt, self.say)
                    else:
                        self.note_once(f"deploy: {receipt.tip} waiting for protected release evidence ({verdict})")
                    return
            serve_guard.write(paths.STATE_DIR / serve_guard.RECEIPT, {**serve_guard.original(paths.STATE_DIR, receipt), "healthy": True})
            self.say(f"deploy: {receipt.tip} is healthy")
            if codebase.RELEASE == receipt.tip:
                install.refresh_serve_guard(checkout, receipt.tip)
        elif receipt.expired():
            if reason := serve_guard.rollback_schema_refusal(paths.STATE_DIR, checkout, receipt.previous):
                self.note_once(f"deploy: code-only rollback refused because {reason}")
            else:
                self.restart_for_new_code()

    def deploy_if_ready(self, cfg: config.Config) -> None:
        try:
            repo = cfg.repo_for_path(paths.PACKAGE_DIR)
            if repo and repo.allows_unattended_deploy("production") and (held := release_proof.reconcile_running(repo)):
                self.note_once(f"deploy: keeping the current release for its protected observation ({held})")
                return
            self.deploy(cfg)
        finally:
            report_rollbacks(paths.PACKAGE_DIR.parent, self.say)
            release_proof.advance(cfg)

    def deploy(self, cfg: config.Config) -> None:
        if not (repo := cfg.repo_for_path(paths.PACKAGE_DIR)) or not repo.allows_unattended_deploy("production"):
            return
        src = repo.path.resolve()
        root = util.git(["rev-parse", "--show-toplevel"], paths.PACKAGE_DIR, check=False, timeout=10).strip()
        if not root or Path(root).resolve() != src:
            return
        required = pilot.required_check(cfg, repo, config.load_layer(repo.layer_dir))
        if required is not None:
            self.deploy_checkout(repo, src, required)

    def deploy_checkout(self, repo: config.Repo, src: Path, required: dict) -> None:
        with util.repo_lock(src):
            if not (github := loops.github_remote(src) or repo.github):
                return
            if not (base := gh.default_branch(github, fallback="", timeout=DEPLOY_API_TIMEOUT, retry=False)):
                raise RuntimeError(f"could not determine the default branch of {github}")
            if repo.target_branch and repo.target_branch != base:
                return
            ref = f"refs/remotes/origin/{base}"
            util.git(["fetch", "-q", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                      "origin", f"+refs/heads/{base}:{ref}"], src,
                     timeout=45, env={"GIT_TERMINAL_PROMPT": "0"}, retry=False)
            with self.checkout_update:
                tip = util.git(["rev-parse", "--verify", ref], src, timeout=10, retry=False).strip()
                if not self.deploy_ready(src, base, tip):
                    return
                if reason := pilot_deploy_refusal() or gh.deployment_ci_refusal(
                    github, tip, required, timeout=DEPLOY_API_TIMEOUT,
                ):
                    self.deploy_refused(src, tip, reason)
                    return
                self.deploy_tip(src, base, ref, tip, github, repo)

    def deploy_tip(self, src: Path, base: str, ref: str, tip: str, github: str, repo: config.Repo) -> None:
        if util.git(["rev-parse", "--verify", ref], src, timeout=10, retry=False).strip() != tip:
            return
        if not self.deploy_ready(src, base, tip):
            return
        subject = util.git(["--no-replace-objects", "show", "-s", "--format=%s", tip], src,
                           timeout=10, retry=False).strip()
        with util.lock(PILOT_LOCK) as free:
            reason = pilot_deploy_refusal() if free else "pilot is active"
            if reason:
                self.deploy_refused(src, tip, reason)
                return
            if util.git(["rev-parse", "--verify", ref], src, timeout=10, retry=False).strip() != tip:
                return
            if not self.deploy_ready(src, base, tip):
                return
            try:
                recovery = util.deploy_recovery(src, base, tip)
            except (OSError, RuntimeError) as error:
                self.deploy_refused(src, tip, str(error))
                return
            hooks_path = util.git(["config", "--null", "--get", "core.hooksPath"], src, timeout=10,
                                  check=False, retry=False)
            if hooks_path.removesuffix("\0"):
                self.say(f"deploy: {paths.short_path(src)} Git hooks disabled for fast forward to {tip}")
            prior = serve_guard.pending(paths.STATE_DIR, src)
            previous = prior.previous if prior and prior.branch == base and prior.tip == recovery.head else recovery.head
            if prior and prior.release_checks:
                self.deploy_refused(src, tip, "an earlier protected release is still awaiting its decision")
                return
            # A still-pending build must not be allowed to replace its own rollback guard.
            if not prior and codebase.RELEASE == recovery.head:
                install.refresh_serve_guard(src, recovery.head)
            previous_receipt = serve_guard.original(paths.STATE_DIR, prior) if prior else None
            checks = release_proof.on_tip(repo, base, tip, previous)
            if checks and not release_proof.known_good(repo.name, previous, checks):
                self.deploy_refused(src, tip, "previous release did not pass its protected recovery check")
                return
            receipt = serve_guard.Receipt(str(src), base, previous, tip, github, time.time(), release_checks=checks)
            serve_guard.write(paths.STATE_DIR / serve_guard.RECEIPT, asdict(receipt))
            try:
                util.deploy_fast_forward(src, tip)
            except util.DeployStillRunning as error:
                self.deploy_refused(src, tip, str(error))
                return
            except (OSError, RuntimeError) as error:
                self.restore_deploy(src, base, tip, recovery, error)
                if previous_receipt and serve_guard.at_tip(src, prior):
                    serve_guard.write(paths.STATE_DIR / serve_guard.RECEIPT, previous_receipt)
                elif serve_guard.at_tip(src, replace(receipt, tip=recovery.head)):
                    serve_guard.write(paths.STATE_DIR / serve_guard.RECEIPT,
                                      asdict(replace(receipt, settled="fast_forward_failed")))
                self.deploy_refused(src, tip, str(error))
                return
        self.say(f"deploy: {paths.short_path(src)} fast forwarded to {tip} ({subject})")

    def restore_deploy(self, src: Path, base: str, tip: str, recovery: util.DeployRecovery, error: Exception) -> None:
        checkout = paths.short_path(src)
        try:
            problems = util.restore_deploy_paths(src, base, recovery)
        except (OSError, RuntimeError) as restore_error:
            problems = [str(restore_error)]
        try:
            if remaining := util.deploy_porcelain(src):
                problems.append(f"remaining paths: {remaining}")
        except (OSError, RuntimeError) as status_error:
            problems.append(f"could not read remaining paths: {status_error}")
        details = " | ".join([str(error), *problems])
        self.say(f"deploy: {checkout} recovery for {tip} ({details})")

    def deploy_ready(self, src: Path, base: str, tip: str) -> bool:
        reason = deploy_refusal(src, base, tip)
        if reason:
            self.deploy_refused(src, tip, reason)
        return reason == ""

    def deploy_refused(self, src: Path, tip: str, reason: str) -> None:
        key = (tip, reason)
        if key not in self.deploy_refusals:
            self.deploy_refusals.add(key)
            self.say(f"deploy: {paths.short_path(src)} left alone for {tip} ({reason})")

    def watch_alerts(self) -> None:
        due = {k: v for k, v in self.failing.items()
               if v["ticks"] >= ALERT_TICKS and (k in ALERT_STAGES or k.startswith("loop:"))}
        self.alerted = {k: e for k, e in self.alerted.items() if k in self.failing}
        fresh = [k for k, v in due.items() if self.alerted.get(k) != v["error"]]
        if fresh:
            self.alerted.update({k: due[k]["error"] for k in fresh})
            self.alert_open = True
            detail = "; ".join(f"{k}: {v['error']} (since {v['since']})" for k, v in sorted(due.items()))
            self.send_alert(alerts.raise_alert, self.cfg.alert_repo, ALERT_KEY,
                            "arete serve keeps failing a tick part", detail)
        if self.failing or fresh:
            return
        if self.alert_open or not self.alert_checked:
            was_open = self.alert_open or bool(incident_store.alert_get(ALERT_KEY))
            self.alert_open = False
            self.alert_checked = True
            if was_open:
                self.send_alert(alerts.clear_alert, self.cfg.alert_repo, ALERT_KEY)

    def send_alert(self, call: Callable, *args) -> None:
        self.alert_last = self.pool.submit(self.alert_work, self.alert_last, call, *args)

    def alert_work(self, prior: Future | None, call: Callable, *args) -> None:
        if prior:
            wait([prior])
        try:
            if said := call(*args):
                self.say(said)
        except Exception as e:
            self.say(f"{ALERT_KEY}: alert failed: {type(e).__name__}: {util.first_line(str(e), 300)}")

    def prune(self) -> None:
        try:
            self.prune_data()
        finally:
            self.collect_db()

    def collect_db(self) -> None:
        with self.db_lock:
            self.finish_db()
            if self.db_future is not None:
                return
            self.db_health.update(status="pending", requested_at=util.iso(), error=None)
            try:
                self.db_future = self.pool.submit(dbstats.collect)
            except Exception as e:
                self.fail_db(e)

    def fail_db(self, error: Exception) -> None:
        self.db_health.update(status="failed", finished_at=util.iso(),
                              error=f"{type(error).__name__}: {util.first_line(str(error), 300)}")
        self.say(f"db collection failed: {self.db_health['error']}")

    def finish_db(self) -> None:
        if self.db_future is None or not self.db_future.done():
            return
        done, self.db_future = self.db_future, None
        try:
            values = done.result()
        except Exception as e:
            self.fail_db(e)
        else:
            self.db_health.update(values, status="complete", finished_at=util.iso(), error=None)
            self.say("db " + json.dumps(values, sort_keys=True))

    def prune_data(self) -> None:
        try:
            made = backup.snapshot("prune")
        except Exception as e:
            self.say(f"backup failed, so nothing was pruned: {type(e).__name__}: {util.first_line(str(e), 300)}")
            return
        if made:
            self.copy_off_machine(made)
        try:
            policy = retention.load()
        except Exception as e:
            self.say(f"retention policy failed, so nothing was pruned: {type(e).__name__}: {util.first_line(str(e), 300)}")
            return
        try:
            removed = dbstats.prune()
        except Exception as e:
            self.say(f"dbstats prune failed: {type(e).__name__}: {util.first_line(str(e), 300)}")
        else:
            if removed > 0:
                self.say(f"dbstats: removed {removed} files")
        measured = None
        try:
            pool = getattr(self, "pool", None)
            measured = pool.submit(retention_store.measure).result() if pool is not None else retention_store.measure()
        except Exception as e:
            self.say(f"retention measure failed: {type(e).__name__}: {util.first_line(str(e), 300)}")
        rows_plan = None
        if measured is not None:
            try:
                rows_plan = retention_store.prune_rows(policy, measured)
            except Exception as e:
                self.say(f"retention rows prune failed: {type(e).__name__}: {util.first_line(str(e), 300)}")
        file_result = None
        try:
            file_result = retention_store.prune_files()
        except Exception as e:
            self.say(f"retention file prune failed: {type(e).__name__}: {util.first_line(str(e), 300)}")
        else:
            for error in file_result.errors:
                self.say(f"file purge failed: {util.first_line(error, 300)}")
        rows_removed = rows_plan.rows if rows_plan is not None else 0
        folders_removed = file_result.folders if file_result is not None else 0
        records_removed = file_result.records if file_result is not None else 0
        blobs_removed = file_result.blobs if file_result is not None else 0
        bytes_removed = file_result.size if file_result is not None else 0
        if rows_removed or folders_removed or records_removed or blobs_removed or bytes_removed:
            self.say(
                f"prune: removed {util.count(rows_removed, 'row')}, "
                f"{util.count(folders_removed, 'folder')}, "
                f"{util.count(records_removed, 'record')}, "
                f"{util.count(blobs_removed, 'blob')} ({record.size_text(bytes_removed)})"
            )
        if rows_plan is not None and rows_plan.over_budget:
            self.say(
                f"runs/: {record.size_text(rows_plan.remaining_bytes)} is over max_gb {policy.records.max_gb:g}; "
                "records left are pinned, cited by a kept proposal, running, pr, needs-info, waiting on an ask, "
                "or written in the last hour; bytes outside record/ stay until folder expiry"
            )
        try:
            tool_events.rotate_events(policy.events_file.days)
        except Exception as e:
            self.say(f"event rotation failed: {type(e).__name__}: {util.first_line(str(e), 300)}")

    def copy_off_machine(self, made: Path) -> None:
        try:
            copying = backup.daily_copy(made, self.cfg.backup.copy_to, self.pool.submit)
        except Exception as e:
            self.say(f"backup copy not started: {type(e).__name__}: {util.first_line(str(e), 300)}")
            return
        if copying:
            copying.add_done_callback(self.copy_finished)

    def copy_finished(self, done: Future) -> None:
        if failure := done.exception() or done.result():
            self.say(f"backup copy failed: {failure}")

    def schedule(self, loop: Loop, st: dict, now: datetime) -> None:
        t = loop.trigger
        if not self.loaded and t.run_at_load:
            self.fire(loop, "load", now)
            return
        if not (t.cron or t.every):
            return
        if not st.get("last_fire"):
            loop_store.set_loop_state(loop.name, last_fire=util.iso(now), next_due=next_due(loop, now))
            return
        if is_due(loop, st["last_fire"], now):
            self.fire(loop, "cron" if t.cron else "every", now)
        elif st.get("next_due") != (due := next_due(loop, util.parse_iso(st["last_fire"]))):
            loop_store.set_loop_state(loop.name, next_due=due)

    def fire(self, loop: Loop, trigger: str, now: datetime) -> None:
        from . import configrepo

        if configrepo.is_repo():
            configrepo.commit_hand_edits()
        with foundation.tx() as c:
            factory_state.admit(c)
            queued = sources.fire(loop, trigger)
            loop_store.set_loop_state(loop.name, last_fire=util.iso(now), next_due=next_due(loop, now))
        self.say(f"{loop.name}: fired by {trigger}" if queued else f"{loop.name}: {trigger} fire joins the one that waits")

    def poll(self, loop: Loop) -> None:
        for i, source in enumerate(loop.sources):
            sid = sources.source_id(loop, i)
            running = self.polls.get(sid)
            if running and not running.done():
                continue
            if source.get("name") and self.slot_polling(loop):
                continue
            if time.monotonic() - self.tried.get(sid, -1e9) < sources.every(source):
                continue
            self.tried[sid] = time.monotonic()
            self.polls[sid] = future = self.pool.submit(self.poll_source, loop, i)
            future.add_done_callback(lambda done, sid=sid: self.poll_ended(sid, done))

    def slot_polling(self, loop: Loop) -> bool:
        return any(sources.is_slot(loop, sid) and not future.done() for sid, future in self.polls.items())

    def poll_ended(self, sid: str, done: Future) -> None:
        if done.cancelled():
            return
        if error := done.exception():
            self.say(f"{sid}: poll failed: {type(error).__name__}: {util.first_line(str(error), 300)}")

    def forget_unpolled_errors(self, live: list[Loop]) -> None:
        polled = {sources.source_id(loop, i) for loop in live for i in range(len(loop.sources))}
        for sid in set(self.errors) - polled:
            self.errors.pop(sid, None)

    def watch_oncall(self) -> None:
        if not self.cfg.oncall.enabled or (self.oncall and not self.oncall.done()):
            return
        if self.code_problem or config.paused(self.cfg, oncall=True):
            return
        self.oncall = self.pool.submit(self.scan_oncall, self.cfg)

    def scan_oncall(self, cfg: config.Config) -> None:
        try:
            found = oncall.scan(cfg)
        except Exception as e:
            text = f"{type(e).__name__}: {util.first_line(str(e), 300)}"
            if self.oncall_error != text:
                self.say(f"on-call scan failed: {text}")
            self.oncall_error = text
            return
        self.oncall_error = ""
        if found and (found.get("opened") or found.get("decisions") or found.get("repairs")):
            self.say(f"on-call: {oncall.said(found)}")

    def sweeping(self) -> bool:
        return bool(self.sweep) and not self.sweep.done()

    def sweep_vms(self) -> None:
        try:
            devices.sweep(self.sweep_log)
            devices.probe_waiting_devices(self.say)
        except Exception as e:
            self.say(f"sweeping left-over VMs failed: {type(e).__name__}: {util.first_line(str(e), 300)}")

    def sweeping_workspaces(self) -> bool:
        return bool(self.workspace_sweep) and not self.workspace_sweep.done()

    def sweep_workspaces(self) -> None:
        try:
            workspaces.sweep(self.cfg)
        except Exception as e:
            self.say(f"sweeping workspaces failed: {type(e).__name__}: {util.first_line(str(e), 300)}")

    def sweep_log(self, run_id: str | None) -> Callable[[str], None]:
        if not run_id:
            return self.say

        def log(text: str) -> None:
            run_store.add_feed(run_id, "device", f"arete serve: {text}")
            self.say(f"{run_id}: {text}")
        return log

    def poll_source(self, loop: Loop, index: int) -> None:
        with principals.acting(f"rule:{loop.name}"):
            return self.poll_source_as_rule(loop, index)

    def poll_source_as_rule(self, loop: Loop, index: int) -> None:
        sid = sources.source_id(loop, index)
        try:
            found, items = sources.poll(loop, index, self.cfg, say=self.say)
        except Exception as e:
            text = f"{type(e).__name__}: {util.first_line(str(e), 300)}"
            if isinstance(e, factory_state.Off):
                return
            with foundation.tx() as connection:
                factory_state.admit(connection)
                if (c := source_store.cursor(sid)) is not None:
                    source_store.set_cursor(sid, c["value"], False, text)
            if self.errors.get(sid) != text:
                self.say(f"{sid}: poll failed: {text}")
            self.errors[sid] = text
            return
        self.errors.pop(sid, None)
        if items:
            self.say(f"{sid}: {util.count(len(found), 'new event')}, {util.count(len(items), 'queue item')}")

    def dispatch(self, by_name: dict[str, Loop]) -> None:
        if self.code_problem:
            return
        slots = QueueSlots(run_store.runs_marked_running())
        pending = loop_store.pending()
        ids = {item["id"] for item in pending}
        self.waiting = {i: why for i, why in self.waiting.items() if i in ids}
        self.settle("queue:", {str(i) for i in ids})
        for item in pending:
            factory_state.admit()
            self.guard(f"queue:{item['id']}", self.dispatch_queued, item, by_name, slots)

    def dispatch_queued(self, item: dict, by_name: dict[str, Loop], slots: QueueSlots) -> None:
        with principals.acting(f"rule:{item['loop']}"):
            return self.dispatch_queued_as_rule(item, by_name, slots)

    def dispatch_queued_as_rule(self, item: dict, by_name: dict[str, Loop], slots: QueueSlots) -> None:
        loop = by_name.get(item["loop"])
        if not loop or not loop.enabled or loop.error:
            with foundation.tx() as c:
                factory_state.admit(c)
                loop_store.drop_queue_item(c, item["id"])
            self.say(f"{item['loop']}: dropped queue item {item['id']}, the loop is off or gone")
            return
        why = self.dispatch_item(item, loop, slots)
        if why is None:
            return
        if item["grp"]:
            slots.held_groups.add((loop.queue_name, item["grp"]))
        if not why:
            self.waiting.pop(item["id"], None)
        elif self.waiting.get(item["id"]) != why:
            self.waiting[item["id"]] = why
            self.say(f"{loop.name}: queue item {item['id']} waits, {why}")

    def dispatch_item(self, item: dict, loop: Loop, slots: QueueSlots) -> str | None:
        from .store import devices as device_store

        if open_waits := device_store.open_waits_for_subject(f"config:loop/{loop.name}"):
            blockers = ", ".join(w["blocker"].removeprefix("device:") for w in open_waits)
            return f"waiting on {blockers}"
        if why := slots.why_full(item, loop, self.cfg):
            return why
        if spent := loop_run.spent_limit(loop, self.cfg):
            return f"its {spent} is spent"
        if held := loop.usage_hold(self.cfg):
            return held
        if (loop.name == "merge-check" and loop.command == ["arete", "merge-check"]
                and (reset := mergecheck_reset(loop, item))):
            return f"merge-check quota hold until {reset}"
        try:
            run = loop_run.launch(loop.name, item=item["id"], cfg=self.cfg)
        except control.Busy as e:
            slots.busy.add(loop.name)
            return str(e)
        except loop_run.NeverBegan as e:
            self.retry_once(item, str(e))
            return None
        except (control.NotFound, control.Refused, config.Paused):
            return None
        slots.took(loop, item)
        self.say(f"{loop.name}: started {run['id']} ({item['trigger']})")
        return ""

    def start_approval_jobs(self) -> None:
        retry_from = util.iso(util.now() - timedelta(seconds=APPROVAL_RETRY_SECONDS))
        decidable = approvals_store.approvals()
        self.settle("approval:", {str(a["id"]) for a in decidable})
        for job in APPROVAL_JOBS:
            if self.job_running(job):
                continue
            for approval in decidable:
                if self.guard(f"approval:{approval['id']}:{job.note}", self.approval_due, job, approval, retry_from):
                    approvals_store.set_approval_note(approval["id"], f"{job.note}_tried", util.iso())
                    self.jobs[job.note] = self.job_pools[job.note].submit(self.do_approval_job, job, approval)
                    break

    def approval_due(self, job: ApprovalJob, approval: dict, retry_from: str) -> bool:
        model = job.model_for(approval, self.cfg)
        return bool(model and not approval[job.note] and approval[f"{job.note}_tried"] <= retry_from
                    and not routing.usage_hold(model, cfg=self.cfg, repo=delivery.repo_for(approval, self.cfg),
                                               tools=list(job.tools), denied=list(job.denied)))

    def job_running(self, job: ApprovalJob) -> bool:
        return job.note in self.jobs and not self.jobs[job.note].done()

    def do_approval_job(self, job: ApprovalJob, approval: dict) -> None:
        try:
            self.say(f"approval #{approval['id']}: {job.write(approval, self.cfg)}")
        except Exception as e:
            approvals_store.set_approval_note(approval["id"], f"{job.note}_tried", util.iso())
            self.say(f"approval #{approval['id']}: {job.not_written} yet, it tries again in "
                     f"{APPROVAL_RETRY_SECONDS // 60} minutes: {util.first_line(str(e), 300)}")

    def start_outbox_job(self) -> None:
        if self.outbox_job is not None and not self.outbox_job.done():
            return
        self.outbox_job = None
        with foundation.tx() as c:
            outbox.sweep(c, self.started)
        self.outbox_job = self.outbox_pool.submit(self.do_outbox_job)

    def do_outbox_job(self) -> None:
        try:
            outbox.send_due()
        except Exception as e:
            self.say(f"outbox send failed: {type(e).__name__}: {util.first_line(str(e), 300)}")

    def retry_once(self, item: dict, why: str) -> None:
        self.say(f"{item['loop']}: queue item {item['id']} failed to start, {why}; "
                 "the recovery sweep handles its claim")

    def reap_inherited(self) -> None:
        for pid in list(self.inherited):
            try:
                ended = os.waitpid(pid, os.WNOHANG)[0]
            except ChildProcessError:
                ended = pid
            if ended:
                self.inherited.discard(pid)

    def busy_with(self) -> str:
        if sum(not f.done() for f in self.polls.values()):
            return "a source poll is still running"
        if running := [job.while_running for job in APPROVAL_JOBS if self.job_running(job)]:
            return running[0]
        if (job := getattr(self, "outbox_job", None)) and not job.done():
            return "an outbox delivery is still running"
        if self.oncall and not self.oncall.done():
            return "an on-call scan is still running"
        if self.sweeping():
            return "a sweep of left-over VMs is still running"
        if self.sweeping_workspaces():
            return "a sweep of workspaces is still running"
        if dashboard.POSTS_IN_FLIGHT.active():
            return "the dashboard is still answering a request"
        if chat.any_busy():
            return "a chat message is still running"
        if capture.checking():
            return "a feedback request is still being checked or filed"
        return ""

    def record_failed_start(self, receipt: serve_guard.Receipt, attempted_at: float) -> None:
        if serve_guard.at_tip(paths.PACKAGE_DIR.parent, receipt):
            serve_guard.record_start(paths.STATE_DIR, receipt, attempted_at)

    def rollback_unhealthy_deploy(self, receipt: serve_guard.Receipt) -> bool:
        if not serve_guard.rollback_if_unhealthy(paths.STATE_DIR, paths.PACKAGE_DIR.parent, paths.LOG_DIR, receipt):
            return False
        self.code_problem = ""
        if codebase.RELEASE == receipt.previous and codebase.head_and_dirt() == (receipt.previous[:12], False):
            self.code = self.code_seen = code_stamp()
        return True

    def load_new_code(self) -> None:
        stamp = code_stamp()
        steady, self.code_seen = stamp == self.code_seen, stamp
        if stamp == self.code:
            self.code_problem = ""
        if stamp == self.code or not steady:
            return
        receipt = serve_guard.pending(paths.STATE_DIR, paths.PACKAGE_DIR.parent)
        if receipt is not None and ("HEAD", receipt.tip[:12]) not in stamp:
            receipt = None
        if receipt is not None:
            if busy := self.busy_with():
                self.note_once(f"new code waits to load: {busy}")
                return
            if self.rollback_unhealthy_deploy(receipt):
                return
        entrypoint = f"import sys; sys.path.insert(0, {str(paths.PACKAGE_DIR.parent)!r}); "
        attempted_at = time.time()
        check = util.sh([sys.executable, "-I", "-c", entrypoint + f"sys.argv[:] = {sys.argv!r}; " + IMPORT_CHECK],
                        timeout=120)
        if check.returncode != 0:
            problem = util.last_line(check.stderr or check.stdout, 300)
            if receipt is not None:
                self.note_once("deploy: new code does not import: " + problem)
                self.record_failed_start(receipt, attempted_at)
            else:
                self.note_once("new code does not import, so serve keeps the code it started with: " + problem)
            return
        candidate = util.sh([sys.executable, "-I", "-c", entrypoint + CONFIG_CHECK], timeout=120)
        running_cfg, running_fatal = config.load_or_problem()
        if problem := candidate_config_problem(candidate, running_cfg, running_fatal):
            self.code_problem = problem
            self.note_once(self.code_problem)
            if receipt is not None:
                self.record_failed_start(receipt, attempted_at)
            return
        self.code_problem = ""
        if stamp != self.migration_stamp:
            migrated = util.sh(control.command(["migrate", "--no-contracts"]), timeout=120)
            self.migration_stamp = stamp
            self.migration_error = ((util.last_line(migrated.stderr or migrated.stdout, 300)
                                     or f"migrate exited {migrated.returncode}") if migrated.returncode else "")
            if self.migration_error and receipt is not None:
                self.record_failed_start(receipt, attempted_at)
        if self.migration_error:
            self.note_once("new code migration failed, so serve keeps its code: " + self.migration_error)
            return
        self.restart_for_new_code()

    def restart_for_new_code(self) -> None:
        if busy := self.busy_with():
            self.note_once(f"new code waits to load: {busy}")
            return
        self.code_problem = ""
        dashboard.POSTS_IN_FLIGHT.close()
        try:
            if busy := self.busy_with():
                self.note_once(f"new code waits to load: {busy}")
                return
            still_running = restart_children()
            children = foundation.live_serve_children(self.workspace_contract_children | still_running)
            self.workspace_contract_children = children
            beat = util.read_json(control.BEAT_FILE, {})
            if not isinstance(beat, dict):
                beat = {}
            util.write_json(control.BEAT_FILE, {**beat, **foundation.workspace_child_fields(children)})
            self.say(f"new code: serve {os.getpid()} restarts itself" + (
                f"; it leaves {util.count(len(still_running), 'process', 'processes')} running" if still_running else ""))
            fields = foundation.workspace_child_fields(still_running)
            fd = self.lock.fileno() if self.lock is not None else None
            if fd is not None:
                os.set_inheritable(fd, True)
                os.environ[SERVE_LOCK_FD] = str(fd)
            os.environ[RESTARTED_FOR_NEW_CODE] = ",".join(map(str, fields[foundation.WORKSPACE_CHILD_PIDS]))
            os.environ[foundation.INHERITED_SERVE_IDENTITIES] = json.dumps(fields[foundation.WORKSPACE_CHILD_IDENTITIES])
            try:
                os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
            finally:
                if fd is not None:
                    os.set_inheritable(fd, False)
                for name in (SERVE_LOCK_FD, RESTARTED_FOR_NEW_CODE, foundation.INHERITED_SERVE_IDENTITIES):
                    os.environ.pop(name, None)
        finally:
            dashboard.POSTS_IN_FLIGHT.reopen()

    def note_once(self, text: str) -> None:
        if self.code_note != text:
            self.code_note = text
            self.say(text)

    def beat(self) -> None:
        with self.db_lock:
            self.finish_db()
            db = dict(self.db_health)
        every_loop, live = self.every_loop, self.live
        failing = {key: dict(found) for key, found in self.failing.items()}
        at = util.iso()
        if not failing and not self.contracts_waiting:
            self.last_ok = at
        try:
            pending = len(loop_store.pending())
        except Exception:
            pending = None
        running = None
        try:
            rows = run_store.runs_marked_running()
            running = sum(r["kind"] == "loop" for r in rows)
            tickets = len(slots.holding(rows, "pipeline"))
        except Exception:
            tickets = None
        self.workspace_contract_children = foundation.live_serve_children(self.workspace_contract_children)
        authority_problem = "; ".join(dict.fromkeys(filter(None, (
            self.authority_problem, scratch.ignored_legacy_authority()))))
        cfg_problems = getattr(self.cfg, "problems", ())
        first_problem = self.config_problem or (cfg_problems[0].text() if cfg_problems else "")
        util.write_json(control.BEAT_FILE, {
            "release": codebase.RELEASE,
            "schema": foundation.LATEST,
            "db": db,
            "pid": os.getpid(), "engine": codebase.STAMP, "checkout_head": codebase.checkout_head(),
            "at": at, "started": util.iso(self.started), "ticks": self.ticks,
            "tick_seconds": TICK_SECONDS, "loops": len(live), "off": len(every_loop) - len(live),
            "broken": {x.name: x.error for x in every_loop if x.error}, "pending": pending,
            "running": running, "tickets": tickets, "poll_errors": dict(self.errors),
            "tick_ok": not failing and not self.contracts_waiting, "last_ok": self.last_ok,
            "failing": failing, "reloaded": self.reloaded,
            "workspace_contract_waiting": self.contracts_waiting,
            **foundation.workspace_child_fields(self.workspace_contract_children),
            "config_ignored": list(self.cfg.ignored),
            "config_problems": [p.text() for p in cfg_problems],
            **({"authority_problem": authority_problem} if authority_problem else {}),
            "secret_migration": self.secret_migration,
            **({"config_problem": first_problem} if first_problem else {}),
            **({"code_problem": self.code_problem} if self.code_problem else {}),
        })


def start_notes(cfg: config.Config) -> list[str]:
    notes = []
    if dropped := environment.dropped(cfg):
        creds = [name for name in dropped if paths.CREDENTIAL_NAME.search(name)]
        plain = [name for name in dropped if not paths.CREDENTIAL_NAME.search(name)]
        if plain:
            notes.append(f"these server variables no longer reach agents or the commands that run a repo's code: "
                         f"{', '.join(plain)}; add a name to [environment] pass in "
                         f"{paths.short_path(paths.CONFIG_FILE)} or a repo's layer to pass it (a trailing * matches a "
                         f"prefix, [\"*\"] passes all)")
        if creds:
            notes.append(f"these credential variables never pass from the parent environment to children: "
                         f"{', '.join(creds)}; register each secret with expose instead of adding it to "
                         f"[environment] pass")
    if judge := runner.judge_note(cfg):
        notes.append(judge)
    return notes


def cleanup_legacy() -> None:
    for name in ("state.db", "chat.json.moved"):
        file = paths.STATE_DIR / name
        try:
            info = file.lstat()
            if stat.S_ISREG(info.st_mode) and (name != "state.db" or info.st_size == 0):
                file.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def startup_beat() -> foundation.ServeChildren:
    beat, seen = util.read_json_snapshot(control.BEAT_FILE, {})
    if not isinstance(beat, dict):
        beat = {}
    inherited = foundation.heartbeat_serve_children(beat, seen) | foundation.inherited_serve_children()
    children = foundation.live_serve_children(inherited)
    if beat.get("pid") != os.getpid():
        beat = {}
    util.write_json(control.BEAT_FILE, {**beat, "pid": os.getpid(), "schema": foundation.LATEST,
                                       **foundation.workspace_child_fields(children),
                                       "db": beat.get("db") or {
                                           **dbstats.empty_collection(), "status": "initial",
                                           "requested_at": None, "finished_at": None, "error": None}})
    return children


def recover_queued(say: Callable[[str], None] = lambda text: None, *, min_age: float | None = None) -> list[dict]:
    from . import pipeline
    from .store import devices as device_store

    cutoff = pipeline.RUN_START_SECONDS if min_age is None else min_age
    handled: list[dict] = []
    for row in run_store.runs_queued():
        run_id = row["id"]
        if device_store.open_waits_for_subject(f"run:{run_id}"):
            continue
        kind = row.get("kind") or ""
        try:
            age = (util.now() - util.parse_iso(row["started"])).total_seconds()
        except (TypeError, ValueError):
            age = cutoff
        if age < cutoff:
            continue
        if kind in ("chat", "import"):
            if settled := record.settle(run_id, "interrupted", end_line=f"interrupted: {record.INTERRUPTED}",
                                        summary=record.INTERRUPTED):
                say(f"{run_id}: interrupted (unclaimed queued {kind})")
                handled.append({"id": run_id, "action": "interrupted", "status": settled["status"]})
            continue
        saved = record.read_launch(run_id)
        cmd = saved.get("command") if isinstance(saved.get("command"), list) and saved.get("command") else None
        log_name = str(saved.get("log") or "")
        try:
            if cmd is None:
                if kind == "session" and sessions.spec_file(run_id).is_file():
                    cmd = control.command(["session", "exec", run_id])
                    log_name = sessions.SESSION_LOG
                elif kind == "loop" and row.get("loop"):
                    cmd = control.command(["loop", "exec", "--run-id", run_id])
                    log_name = control.DETACHED_LOG
                elif kind == "pipeline" and row.get("repo") and row.get("issue"):
                    cmd = pipeline.run_command(row["repo"], str(row["issue"]), run_id=run_id)
                    log_name = pipeline.RUNS_LOG
            if not cmd:
                if settled := record.settle(run_id, "interrupted", end_line=f"interrupted: {record.INTERRUPTED}",
                                            summary=record.INTERRUPTED):
                    say(f"{run_id}: interrupted (unclaimed queued {kind} has no relaunch)")
                    handled.append({"id": run_id, "action": "interrupted", "status": settled["status"]})
                continue
            if kind == "pipeline":
                tasks.spawn_queued(row["repo"], str(row["issue"]),
                                   lambda c=cmd, log=log_name or pipeline.RUNS_LOG: control.launch(c, log))
            else:
                control.launch(cmd, log_name or control.DETACHED_LOG)
            say(f"{run_id}: relaunched unclaimed queued {kind}")
            handled.append({"id": run_id, "action": "relaunched", "kind": kind})
        except Exception as e:
            if settled := record.settle(run_id, "interrupted", end_line=f"interrupted: {record.INTERRUPTED}",
                                        summary=f"relaunch failed: {util.first_line(str(e), 200)}"):
                say(f"{run_id}: interrupted (relaunch failed: {e})")
                handled.append({"id": run_id, "action": "interrupted", "status": settled["status"]})
    return handled


def run(with_dashboard: bool = True) -> NoReturn:
    codebase.load()
    rotation_notes = install.rotate_own_log()
    with principals.acting("hand"):
        held = take_serve_lock()
        try:
            inherited = startup_beat()
            try:
                with util.lock("migrate", wait=True):
                    backup.recover_restore()
                contracts_waiting = foundation.migrate()
                if backup.pending_restore():
                    raise backup.Refused("restore metadata could not finish migration; API startup refused")
                if not paths.ISOLATED:
                    foundation.enable_wal()
                cleanup_legacy()
            except Exception as e:
                print(str(e), file=sys.stderr)
                report_rollbacks(paths.PACKAGE_DIR.parent, lambda line: print(line, file=sys.stderr))
                raise SystemExit(1) from e
            authority_problem = scratch.start_authority()
            server = Server()
            server.authority_problem = authority_problem
            server.workspace_contract_children = inherited
            server.contracts_waiting = contracts_waiting
            server.lock = held
            server.with_dashboard = with_dashboard
        except BaseException:
            held.close()
            raise
        server.say(f"arete serve {os.getpid()}: tick every {TICK_SECONDS}s")
        if authority_problem:
            server.say(authority_problem)
        for note in rotation_notes:
            server.say(note)
        server.report_config()
        if not server.contracts_waiting:
            from . import configrepo, pins

            if not server.config_problem:
                server.guard("stage:secret-move", server.move_secrets_when_due)
            try:
                configrepo.init()
                pins.ensure_automation_payer()
            except configrepo.ConfigRepoError as e:
                server.say(f"config repo init deferred: {e}")
            server.read_config()
            server.guard("stage:derived-start", server.start_derived)
            if not server.config_problem:
                server.guard("stage:secret-move", server.move_secrets_when_due)
                from . import inbox

                server.guard("stage:inbox-start", inbox.on_server_start)
                server.guard("stage:queued-start", lambda: recover_queued(server.say))
            else:
                server.guard("stage:inbox-config", server.sync_config_inbox)
        for note in start_notes(server.cfg):
            server.say(note)
        factory_control.notify()
        if with_dashboard:
            threading.Thread(target=dashboard.serve, kwargs={"inside_serve": True}, name="dashboard", daemon=True).start()
        while True:
            started = time.monotonic()
            try:
                server.run_pass()
            except foundation.NoDatabase as e:
                server.say(str(e))
                held.close()
                raise SystemExit(1) from e
            time.sleep(max(1.0, TICK_SECONDS - (time.monotonic() - started)))
