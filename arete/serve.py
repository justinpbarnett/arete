from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import NoReturn, TextIO

from . import (
    alerts,
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
    loop_run,
    loops,
    oncall,
    paths,
    pilot,
    principals,
    record,
    routing,
    runner,
    sessions,
    sources,
    store,
    tasks,
    tool_events,
    util,
    webpush,
)
from .loops import Loop
from .store import approvals as approvals_store
from .store import db as foundation
from .store import incidents as incident_store
from .store import loops as loop_store
from .store import runs as run_store
from .store import sources as source_store

TICK_SECONDS = 20
DEPLOY_SECONDS = 120
DEPLOY_API_TIMEOUT = 20
PILOT_LOCK = "pilot-doc-fix"
PRUNE_SECONDS = 3600
ROTATE_SECONDS = 3600
APPROVAL_RETRY_SECONDS = 900
SWEEP_SECONDS = 300
CLOSED_SECONDS = 300
POLL_THREADS = 8


def mergecheck_reset(loop: Loop, item: dict) -> str:
    from . import mergecheck

    payload = loop_store.queue_item(item["id"])
    names = {e.get("repo", "") for e in payload["payload"]} if payload else set()
    return max((mergecheck.held_until(loop.dir, name) for name in names), default="")


IMPORT_CHECK = "import arete.cli, arete.serve, arete.dashboard, arete.loop_run; from arete import codebase; codebase.load()"
CONFIG_CHECK = "from arete import config; config.load()"
RESTARTED_FOR_NEW_CODE = "ARETE_SERVE_RESTARTED_FOR_NEW_CODE"
SERVE_LOCK = "serve"
SERVE_LOCK_FD = "ARETE_SERVE_LOCK_FD"
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
    for p in [*paths.PACKAGE_DIR.rglob("*.py"), *paths.ADWS_DIR.glob("*.py"), *dashboard.web_files()]:
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        stamp.add((str(p), st.st_mtime_ns, st.st_size))
    if head := codebase.checkout_head():
        stamp.add(("HEAD", head))
    return frozenset(stamp)


def deploy_refusal(src: Path, base: str, tip: str) -> str | None:
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


def reap_children() -> None:
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass


def child_pids() -> set[int]:
    found = util.sh(["pgrep", "-P", str(os.getpid())], timeout=10)
    return {int(p) for p in found.stdout.split()} if found.returncode == 0 else set()


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


class Server:
    def __init__(self) -> None:
        self.cfg, self.config_problem = config.load_or_problem()
        self.code_problem = ""
        self.reported_problem = ""
        self.reported_ignored: tuple[str, ...] = ()
        self.started = util.now()
        self.polls: dict[str, Future] = {}
        self.sweep: Future | None = None
        self.oncall: Future | None = None
        self.deployment: Future | None = None
        self.deploy_at = time.monotonic() - DEPLOY_SECONDS
        self.deploy_refusals: set[tuple[str, str]] = set()
        self.checkout_update = threading.Lock()
        self.zte_work: Future | None = None
        self.zte_at = 0.0
        self.oncall_error = ""
        self.tried: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.waiting: dict[int, str] = {}
        self.pool = ThreadPoolExecutor(POLL_THREADS, thread_name_prefix="poll")
        self.db_future: Future | None = None
        self.db_lock = threading.Lock()
        self.db_health = {**dbstats.empty_collection(), "status": "initial", "requested_at": None,
                          "finished_at": None, "error": None}
        restarted_with = os.environ.pop(RESTARTED_FOR_NEW_CODE, None)
        self.loaded = restarted_with is not None
        self.inherited = {int(p) for p in (restarted_with or "").split(",") if p}
        self.pruned = self.swept = self.rotated = 0.0
        self.checked_closed = 0.0
        self.ticks = 0
        self.reloaded = restarted_with is not None
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
        self.code = self.code_seen
        if codebase.RELEASE:
            self.code = frozenset(entry for entry in self.code if entry[0] != "HEAD") | {
                ("HEAD", codebase.RELEASE[:12])}
        self.code_note = ""
        self.migration_stamp = None
        self.migration_error = ""
        self.contracts_waiting = False
        self.job_pools = {job.note: ThreadPoolExecutor(1, thread_name_prefix=job.note) for job in APPROVAL_JOBS}
        self.jobs: dict[str, Future] = {}

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
            raise ValueError(problem)
        every_loop = loops.load_all(cfg)
        self.every_loop = every_loop
        self.live = [x for x in every_loop if x.enabled and not x.error]
        self.config_good = True
        return True

    def tick(self) -> None:
        if self.contracts_waiting:
            if not paths.DB_FILE.is_file() or not paths.DB_FILE.stat().st_size:
                raise foundation.missing_database()
            self.contracts_waiting = foundation.migrate()
        foundation.ensure_schema()
        if not self.guard("tick:load", self.load) or self.code_problem:
            return
        self.guard("stage:reap", self.reap)
        states = self.guard("stage:states", loop_store.loop_states)
        if states is not None:
            now = util.now()
            for loop in self.live:
                self.guard(f"loop:{loop.name}", self.step_loop, loop, states.get(loop.name) or {}, now)
            self.settle("loop:", {x.name for x in self.live})
            self.loaded = True
        self.guard("stage:forget", self.forget_unpolled_errors, self.live)
        self.guard("stage:oncall", self.watch_oncall)
        self.guard("stage:zte", self.zte_when_due)
        self.guard("stage:dispatch", self.dispatch, {x.name: x for x in reversed(self.every_loop)})
        self.guard("stage:approvals", self.approvals)
        self.guard("stage:rotate", self.rotate_when_due)
        self.guard("stage:prune", self.prune_when_due)
        self.guard("stage:sweep", self.sweep_when_due)
        self.guard("stage:asks", self.expire_asks)
        self.guard("stage:closed", self.release_closed_when_due)
        self.guard("stage:remind", self.remind)

    def reap(self) -> None:
        for row in record.reap(tickets=False):
            self.say(f"{row['id']}: {row['status']}, {record.INTERRUPTED}")
            self.swept = 0.0
        sessions.tell_chats()

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

    def approvals(self) -> None:
        for released in delivery.release_lapsed():
            self.say(f"approval #{released}: {delivery.cut_off('claim expired')}")
        self.start_approval_jobs()

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

    def expire_asks(self) -> None:
        for note in tasks.expire_asks(self.cfg):
            self.say(note)

    def release_closed_when_due(self) -> None:
        if time.monotonic() - self.checked_closed > CLOSED_SECONDS:
            self.checked_closed = time.monotonic()
            for note in tasks.release_closed(self.cfg):
                self.say(note)

    def remind(self) -> None:
        for note in webpush.remind(self.cfg):
            self.say(note)

    def run_pass(self) -> None:
        if self.checkout_update.acquire(blocking=False):
            try:
                self.guard("stage:code", self.load_new_code)
            finally:
                self.checkout_update.release()
        self.guard("tick", self.tick)
        self.guard("stage:deploy", self.deploy_when_due)
        self.guard("stage:inherited", self.reap_inherited)
        self.ticks += 1
        self.guard("stage:alerts", self.watch_alerts)
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
        if now - self.deploy_at >= DEPLOY_SECONDS and not self.config_problem:
            self.deploy_at = now
            self.deployment = self.pool.submit(self.deploy, self.cfg)

    def deploy(self, cfg: config.Config) -> None:
        if not (repo := cfg.repo_for_path(paths.PACKAGE_DIR)):
            return
        src = repo.path.resolve()
        root = util.git(["rev-parse", "--show-toplevel"], paths.PACKAGE_DIR, check=False, timeout=10).strip()
        if not root or Path(root).resolve() != src:
            return
        required = config.load_layer(repo.layer_dir).required_check
        if required is None and repo.name == cfg.arete_repo:
            required = pilot.CHECK
        if required is not None:
            self.deploy_checkout(repo, src, required)

    def deploy_checkout(self, repo: config.Repo, src: Path, required: dict) -> None:
        with util.repo_lock(src):
            if not (github := loops.github_remote(src) or repo.github):
                return
            if not (base := gh.default_branch(github, fallback="", timeout=DEPLOY_API_TIMEOUT, retry=False)):
                raise RuntimeError(f"could not determine the default branch of {github}")
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
                self.deploy_tip(src, base, ref, tip)

    def deploy_tip(self, src: Path, base: str, ref: str, tip: str) -> None:
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
            try:
                util.deploy_fast_forward(src, tip)
            except util.DeployStillRunning as error:
                self.deploy_refused(src, tip, str(error))
                return
            except (OSError, RuntimeError) as error:
                self.restore_deploy(src, base, tip, recovery, error)
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
        limits = self.cfg.retention
        try:
            store.prune(lines_days=limits.lines_days, keep_days=self.cfg.oncall.keep_days)
        except Exception as e:
            self.say(f"state prune failed: {type(e).__name__}: {util.first_line(str(e), 300)}")
        try:
            purged = record.purge_folders()
            if purged.folders:
                self.say(f"runs: removed {util.count(purged.folders, 'folder')} ({record.size_text(purged.size)})")
            for error in purged.errors:
                self.say(f"folder purge failed: {util.first_line(error, 300)}")
        except Exception as e:
            self.say(f"folder purge failed: {type(e).__name__}: {util.first_line(str(e), 300)}")
        try:
            found = record.prune(limits.days, limits.max_gb)
        except Exception as e:
            self.say(f"record prune failed: {type(e).__name__}: {util.first_line(str(e), 300)}")
        else:
            if found.removed:
                self.say(f"records: removed {util.count(len(found.removed), 'record')} "
                         f"({record.size_text(sum(r.size for r in found.removed))}), "
                         f"{found.left} left; runs/ uses {record.size_text(found.left_bytes)}")
            if found.over:
                self.say(f"runs/: {record.size_text(found.left_bytes)} is over max_gb {limits.max_gb:g}; records "
                         "left are pinned, cited by a kept proposal, running or written in the last hour; "
                         "bytes outside record/ stay until folder expiry")
        try:
            tool_events.rotate_events(limits.lines_days)
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
        except Exception as e:
            self.say(f"sweeping left-over VMs failed: {type(e).__name__}: {util.first_line(str(e), 300)}")

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
        slots = QueueSlots(run_store.runs_marked_running())
        pending = loop_store.pending()
        ids = {item["id"] for item in pending}
        self.waiting = {i: why for i, why in self.waiting.items() if i in ids}
        self.settle("queue:", {str(i) for i in ids})
        for item in pending:
            self.guard(f"queue:{item['id']}", self.dispatch_queued, item, by_name, slots)

    def dispatch_queued(self, item: dict, by_name: dict[str, Loop], slots: QueueSlots) -> None:
        with principals.acting(f"rule:{item['loop']}"):
            return self.dispatch_queued_as_rule(item, by_name, slots)

    def dispatch_queued_as_rule(self, item: dict, by_name: dict[str, Loop], slots: QueueSlots) -> None:
        loop = by_name.get(item["loop"])
        if not loop or not loop.enabled or loop.error:
            loop_store.drop_queue_item(item["id"])
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
        except (control.NotFound, control.Refused):
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
        if self.oncall and not self.oncall.done():
            return "an on-call scan is still running"
        if self.sweeping():
            return "a sweep of left-over VMs is still running"
        if dashboard.POSTS_IN_FLIGHT.active():
            return "the dashboard is still answering a request"
        if chat.any_busy():
            return "a chat message is still running"
        if capture.checking():
            return "a feedback request is still being checked or filed"
        return ""

    def load_new_code(self) -> None:
        stamp = code_stamp()
        steady, self.code_seen = stamp == self.code_seen, stamp
        if stamp == self.code:
            self.code_problem = ""
        if stamp == self.code or not steady:
            return
        entrypoint = f"import sys; sys.path.insert(0, {str(paths.PACKAGE_DIR.parent)!r}); "
        check = util.sh([sys.executable, "-I", "-c", entrypoint + IMPORT_CHECK], timeout=120)
        if check.returncode != 0:
            self.note_once("new code does not import, so serve keeps the code it started with: "
                           + util.last_line(check.stderr or check.stdout, 300))
            return
        candidate = util.sh([sys.executable, "-I", "-c", entrypoint + CONFIG_CHECK], timeout=120)
        if candidate.returncode != 0 and not config.load_or_problem()[1]:
            self.code_problem = ("new code cannot read config.toml, so serve keeps the code it started with: "
                                 + util.last_line(candidate.stderr or candidate.stdout, 300))
            self.note_once(self.code_problem)
            return
        if stamp != self.migration_stamp:
            migrated = util.sh(control.command(["migrate", "--no-contracts"]), timeout=120)
            self.migration_stamp = stamp
            self.migration_error = ((util.last_line(migrated.stderr or migrated.stdout, 300)
                                     or f"migrate exited {migrated.returncode}") if migrated.returncode else "")
        if self.migration_error:
            self.note_once("new code migration failed, so serve keeps its code: " + self.migration_error)
            return
        if busy := self.busy_with():
            self.note_once(f"new code waits to load: {busy}")
            return
        self.code_problem = ""
        dashboard.POSTS_IN_FLIGHT.close()
        try:
            if busy := self.busy_with():
                self.note_once(f"new code waits to load: {busy}")
                return
            reap_children()
            still_running = child_pids()
            self.say(f"new code: serve {os.getpid()} restarts itself" + (
                f"; it leaves {util.count(len(still_running), 'process', 'processes')} running" if still_running else ""))
            os.environ[RESTARTED_FOR_NEW_CODE] = ",".join(map(str, sorted(still_running)))
            fd = self.lock.fileno()
            os.set_inheritable(fd, True)
            os.environ[SERVE_LOCK_FD] = str(fd)
            try:
                os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
            except BaseException:
                os.set_inheritable(fd, False)
                os.environ.pop(SERVE_LOCK_FD, None)
                raise
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
        if not failing:
            self.last_ok = at
        try:
            pending = len(loop_store.pending())
        except Exception:
            pending = None
        try:
            running = sum(r["kind"] == "loop" for r in run_store.runs_marked_running())
        except Exception:
            running = None
        util.write_json(control.BEAT_FILE, {
            "release": codebase.RELEASE,
            "schema": foundation.LATEST,
            "db": db,
            "pid": os.getpid(), "engine": codebase.STAMP, "checkout_head": codebase.checkout_head(),
            "at": at, "started": util.iso(self.started), "ticks": self.ticks,
            "tick_seconds": TICK_SECONDS, "loops": len(live), "off": len(every_loop) - len(live),
            "broken": {x.name: x.error for x in every_loop if x.error}, "pending": pending,
            "running": running, "poll_errors": dict(self.errors),
            "tick_ok": not failing, "last_ok": self.last_ok, "failing": failing, "reloaded": self.reloaded,
            "config_ignored": list(self.cfg.ignored),
            **({"config_problem": self.config_problem} if self.config_problem else {}),
            **({"code_problem": self.code_problem} if self.code_problem else {}),
        })


def start_notes(cfg: config.Config) -> list[str]:
    notes = []
    if dropped := environment.dropped(cfg):
        notes.append(f"these server variables no longer reach agents or the commands that run a repo's code: "
                     f"{', '.join(dropped)}; add a name to [environment] pass in "
                     f"{paths.short_path(paths.CONFIG_FILE)} or a repo's layer to pass it (a trailing * matches a "
                     f"prefix, [\"*\"] passes all); a claude agent still gets its login and a pi agent its provider's "
                     f"keys")
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


def run(with_dashboard: bool = True) -> NoReturn:
    codebase.load()
    rotation_notes = install.rotate_own_log()
    with principals.acting("hand"):
        held = take_serve_lock()
        try:
            beat = util.read_json(control.BEAT_FILE, {})
            if beat.get("pid") == os.getpid():
                util.write_json(control.BEAT_FILE, {**beat, "schema": foundation.LATEST,
                                                  "db": beat.get("db") or {
                                                      **dbstats.empty_collection(), "status": "initial",
                                                      "requested_at": None, "finished_at": None, "error": None}})
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
                raise SystemExit(1) from e
            server = Server()
            server.contracts_waiting = contracts_waiting
            server.lock = held
        except BaseException:
            held.close()
            raise
        server.say(f"arete serve {os.getpid()}: tick every {TICK_SECONDS}s")
        for note in rotation_notes:
            server.say(note)
        server.report_config()
        for note in start_notes(server.cfg):
            server.say(note)
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
