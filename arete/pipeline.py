from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import math
import os
import re
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext, suppress
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import (
    alerts,
    browser,
    check_process,
    check_repair,
    ci,
    config,
    context,
    control,
    devices,
    diffguard,
    document,
    environment,
    factory,
    gh,
    intake,
    jev,
    journeys,
    lanes,
    learning,
    local_paths,
    outbound,
    owner_asks,
    paths,
    principals,
    prompt_loader,
    record,
    routing,
    run_lines,
    runlimits,
    runner,
    sandbox,
    scratch,
    slots,
    steps,
    tracker,
    util,
    vault,
    verify,
    webpush,
    workspace,
)
from .agent import NO_SESSION, READ_ONLY_TOOLS, Result, Session
from .review import CATCH_UP_REVIEW_SCHEMA, REVIEW_SCHEMA, Review, catch_up_problems, numbered, permitted_review_patches
from .store import asks as asks_store
from .store import insights as insights_store
from .store import runs as run_store
from .store import sources as source_store
from .store import specs as spec_store
from .store import triage as triage_store
from .store import workspaces
from .store.db import IncompatibleSchema, NoDatabase
from .util import git

ADW = "adw"
ORCHESTRATED = "orchestrated"
ENDED = workspace.ENDED


MAX_DIFF_IN_PROMPT = 60_000
MAX_FIX_DIFF_IN_PROMPT = 20_000
NO_FIX_REPLY = "The builder gave no reply."
MAX_FIX_REPLY_IN_PROMPT = 4000
NO_FIX_DIFF = "Arete could not read what changed; judge the diff above."
REFUSAL_WORDS = re.compile(r"guard|jev|credential|secret|confine|authority", re.IGNORECASE)
SIBLINGS = re.compile(r"\n*## Other PRs for [^\n]*\n(?:\n- \[[^\n]*)+")
SKIPPED_FILE = paths.STATE_DIR / "skipped.json"
CONTINUE_AFTER_DROP = "The connection dropped mid step. Carry on from where you stopped and finish the step."
CONTINUE_AFTER_BUSY = "The provider was busy mid step. Carry on from where you stopped and finish the step."
BUSY_WAITS = (30, 90, 270)
LEFT_TITLE = "Left for a person"
LEFT_HEADING = f"## {LEFT_TITLE}"
LEFT_START, LEFT_END = "<!-- arete:left -->", "<!-- /arete:left -->"
LEFT_FOR_A_PERSON = re.compile(rf"{re.escape(LEFT_START)}\n{LEFT_HEADING}\n\n(.*?)\n\n{re.escape(LEFT_END)}\n*", re.DOTALL)
MAX_LEFT_FOR_A_PERSON_CHARS = 12_000
LEFT_BY_A_PERSON = "A person makes these changes after the PR, since the builder may not edit those paths."
NOT_CHECKED_LEFT_FOR_A_PERSON = (f"{LEFT_HEADING}\n\n{LEFT_BY_A_PERSON} Do not check them, and do not fail the change "
                                 "because they are missing:\n\n")
NOT_CAUSED_LEFT_FOR_A_PERSON = (f"{LEFT_HEADING}\n\n{LEFT_BY_A_PERSON} A red check that only these changes would fix "
                                "is not caused by the PR: set `caused_by_pr` to false, say in its reason that it waits "
                                "on the change left for a person, and do not make it pass another way.\n\n")
CHECKED_IN_USE_HEADING = "## Checked in use"
UNVERIFIED_REQUIREMENT_HEADING = "## Unverified requirement"
RECORDED_MEASUREMENT_LABEL = "**Recorded measurement**"
MEASUREMENT_START, MEASUREMENT_END = "<!-- arete:measurement -->", "<!-- /arete:measurement -->"
MEASUREMENT_SECTION = re.compile(
    rf"^{re.escape(MEASUREMENT_START)}\r?\n\r?\n{re.escape(UNVERIFIED_REQUIREMENT_HEADING)}\r?\n\r?\n"
    r"(?P<requirement>[^\r\n]+)\r?\n\r?\n"
    rf"{re.escape(RECORDED_MEASUREMENT_LABEL)}\r?\n\r?\n[^\r\n]+\r?\n\r?\n{re.escape(MEASUREMENT_END)}"
    r"(?P<separator>(?:\r?\n){1,2}|\Z)",
    re.MULTILINE,
)
RUNS_LOG = "runs.log"
RUN_START_SECONDS = 10
SLOT_QUEUE_FAILED_EXIT = 75
SUCCEEDED = ("pr", "noop", "needs-info", "dry-run", "done")
ANSWER_FILE = "answer.md"
CHECK_LANE_POLL_SECONDS = 2
CHECKS_SCRIPT = "checks"
NO_CHECKS = "(none configured)"
SUMMARY_CHARS = 3000
PLAN_CHARS = 6000
CUT_MARK = " …"

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {"type": "string"},
        "ambiguous": {"type": "boolean"},
        "question": {"type": "string"},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "estimated_lines": {"type": "integer", "minimum": 0},
        "verify": {"type": "string"},
        "left_for_a_person": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "split": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                "required": ["title", "body"],
            },
        },
    },
    "required": ["plan", "ambiguous", "risk", "estimated_lines", "verify"],
}
LEARN_SCHEMA = learning.SCHEMA
EXPERTISE_MAX_LINES = learning.MAX_LINES
REVISE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "replies": {"type": "array", "items": {
            "type": "object",
            "properties": {"comment_id": {"type": "integer"}, "reply": {"type": "string"}},
            "required": ["comment_id", "reply"],
        }},
    },
    "required": ["summary", "replies"],
}
RESOLVER_SETS = {"RUFF_NO_CACHE": "true"}

RESOLVE_SCHEMA = {
    "type": "object",
    "properties": {"resolved": {"type": "boolean"}, "summary": {"type": "string"}},
    "required": ["resolved", "summary"],
}
CONFLICT_MARKER = re.compile(r"^(<<<<<<<|>>>>>>>|\|\|\|\|\|\|\|)( |$)", re.MULTILINE)
CI_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "caused_by_pr": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["name", "caused_by_pr", "reason"],
        }},
        "summary": {"type": "string"},
    },
    "required": ["checks", "summary"],
}
MAX_CI_REASON_CHARS = 700
AUTOMERGE_CATCH_UPS = 2
FEEDBACK_QUIET_MINUTES = 10


def wanted(repo: config.Repo) -> str:
    return config.load_layer(repo.layer_dir).factory_path


def has_origin(repo: config.Repo) -> bool:
    return repo.path.is_dir() and "origin" in util.git(["remote"], repo.path, check=False).split()


def facts(name: str, data: dict) -> dict:
    if name in ("plan", "patch", "revise", "repair"):
        return {"assumptions": assumptions_of(data), "left_for_a_person": str(data.get("left_for_a_person") or "")}
    if name == "pr":
        return {"held": data.get("draft_why") or ""}
    if name == "verify" and isinstance(data.get("verified"), dict):
        return {"requirements": verify.Verified.stored(data["verified"]).requirements()}
    if name == "review" and isinstance(data.get("review"), dict):
        return {"review": Review.stored(data["review"]).report()}
    return {}


def seam_facts(run_id: str) -> dict:
    found: dict = {}
    for line in factory.history(factory.chain(run_id)):
        if line.get("result") == "pass" or line.get("step") == "review":
            found.update(facts(str(line.get("step") or ""), factory.outcome_of(line).data))
    return found


def public_text(run: AgentRun, text: str) -> str:
    return outbound.redact(text) if run.repo.public else text


def provider_busy(res: Result) -> bool:
    return not (res.ok or res.connection_failed or res.usage_limit) and util.is_provider_busy(res.error)


@dataclass
class AgentRun:
    id: str
    repo: config.Repo
    dir: Path
    dry_run: bool = False
    cost: float = 0.0
    paid: float = 0.0
    add_dirs: list[Path] = field(default_factory=list)
    story: list[str] = field(default_factory=list)
    cfg: config.Config | None = None
    layer: config.RepoLayer | None = None
    kind: str = ""
    overrides: dict = field(default_factory=dict)
    step_overrides: dict[str, dict] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    writes_lines: bool = True
    step_calls: dict[str, int] = field(default_factory=dict)
    step_started: bool = field(default=False, init=False)
    budget: runlimits.Budget | None = None
    ab_held: dict[str, tuple[str, str, dict]] = field(default_factory=dict)
    launch_gate: Callable | None = None
    launch_done: Callable | None = None
    closed_context: tuple[str, ...] = ()
    resolved_settings: dict[str, steps.Settings] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return ""

    @property
    def owner_note(self) -> str:
        return owner_asks.NOBODY

    @property
    def where(self) -> str:
        return self.repo.name

    def log(self, msg: str) -> None:
        line = f"{util.iso()} [{self.where}] {msg}"
        print(line, flush=True)
        with open(self.dir / "run.log", "a") as f:
            f.write(line + "\n")

    def write_line(self, event: str, **fields) -> None:
        if self.writes_lines:
            run_lines.write(self.id, event, **fields)

    @contextmanager
    def waiting(self, cause: str):
        with ExitStack() as stack:
            if self.budget is not None:
                stack.enter_context(self.budget.idling(cause))
            if self.writes_lines:
                stack.enter_context(run_lines.waiting(self.id, cause))
            yield

    def save(self, name: str, data) -> None:
        text = data if isinstance(data, str) else json.dumps(data, indent=2, default=str)
        util.write_durably(self.dir / name, text.encode(), 0o600)

    @property
    def config(self) -> config.Config:
        if self.cfg is None:
            self.cfg = config.load()
        return self.cfg

    @property
    def ab_ticket(self) -> tuple[str, str] | None:
        return None

    def settings(self, role: str) -> steps.Settings:
        role = config.STEP_ALIASES.get(role, role)
        if role in self.resolved_settings:
            return self.resolved_settings[role]
        return steps.resolve(self.config, self.layer, role, self.kind,
                             {**self.overrides, **self.step_overrides.get(role, {})},
                             self.step_overrides.get(config.INHERITS.get(role, ""), {}), ticket=self.ab_ticket,
                             held=self.ab_held.get(role))

    def time_for(self, seconds: float) -> float:
        return seconds

    def wait_for_network(self) -> bool:
        return util.wait_for_network()

    def retry_kw(self) -> dict:
        return {}

    def agent(self, step: str, text: str, cwd: Path, role: str = "", **kw) -> Result:
        with principals.acting(f"run:{self.id}/{step}"):
            return self.agent_as_step(step, text, cwd, role, **kw)

    def agent_as_step(self, step: str, text: str, cwd: Path, role: str = "", *, read_only: bool = False, **kw) -> Result:
        s = self.settings(role) if role else None
        if s and read_only:
            s = replace(s, step=step, tools=tuple(t for t in s.tools if t in READ_ONLY_TOOLS), mcp=(), session="fresh")
        if self.budget:
            self.budget.charge(step, s.step if s else role)
        if s:
            reach = s.agent_kw(self.config, kw.pop("allowed_tools", ()), kw.pop("mcp_config", ""))
            kw = {**s.call, **reach,
                  "resume": self.sessions.get(config.INHERITS.get(s.step, s.step), NO_SESSION)
                  if s.session == "resume" else NO_SESSION, **kw}
        rule = f"Bash({class_check_rule(self.dir)})"
        if rule in (kw.get("allowed_tools") or ()):
            kw["allowed_tools"] = [tool for tool in kw["allowed_tools"] if tool != rule]
            kw["claude_tools"] = (rule,)
        kw["extra_env"] = {**(kw.get("extra_env") or {}), **self.lane_env()}
        kw = self.fit_route(step, text, s, kw)
        run_with = kw.get("runner", "") or kw.get("resume", NO_SESSION).runner
        which, model = routing.chosen(kw.get("model", ""), run_with, kw.get("cfg") or self.config)
        chosen = (which, model, kw.get("effort", ""))
        started, spent = time.monotonic(), self.cost
        kw.setdefault("tier", s.tier if s else "")
        kw.setdefault("kind", self.kind)
        kw.setdefault("waits", self.waiting)
        kw.setdefault("repo", self.repo)
        kw.setdefault("layer", self.layer)
        if self.closed_context:
            kw.setdefault("closed_context", self.closed_context)
        seam = factory.inside(self.id)
        if s:
            kw.setdefault("settings", s)
        if self.writes_lines or self.launch_gate:
            def announce(route: dict) -> dict | None:
                grant = self.launch_gate(step, s.step if s else role, route) if self.launch_gate else None
                if not self.writes_lines:
                    return grant
                fields = {k: route.get(k) for k in ("runner", "model", "provider", "auth_mode", "credential",
                                                    "billing", "effort", "requested_effort", "tier", "position",
                                                    "requested", "env", "skipped", "launch") if k in route}
                if s:
                    fields["atom"] = s.step
                if seam is None:
                    self.write_line("step_start", step=step, kind=kw["kind"], **{"try": attempt_no}, **fields)
                else:
                    self.write_line("step_start", step=seam.step, call=step, **{"try": seam.n}, kind=kw["kind"],
                                    **fields)
                self.step_started = True
                return grant

            kw["on_launch"] = announce
        if s and s.ab_name:
            record.hold_ab(self.id, s.step, s.ab_name, s.ab_side, json.loads(s.ab_arm or "{}"))
        attempt_no = self.step_calls[step] = max(run_store.step_tries(self.id, step), self.step_calls.get(step, 0)) + 1
        kw["try_no"] = attempt_no
        self.log(f"{step}: starting")
        kw.setdefault("say", lambda note: self.log(f"{step}: {note}"))
        res = None
        try:
            res = self.through_drops(step, text, cwd, kw, self.writes_lines and seam is None, s, attempt_no)
            return self.record_step(step, res, s, kw, seam, attempt_no, started, spent, chosen)
        finally:
            if self.launch_done:
                self.launch_done(step, bool(res and (res.ok or res.turns)))

    def lane_env(self) -> dict[str, str]:
        if self.layer is None:
            return {}
        names = check_lanes(self.layer, self)
        with suppress(OSError):
            for name in names:
                util.lock_file(name).close()
        return {lanes.ENV: ",".join(names)}

    def fit_route(self, step: str, text: str, s: steps.Settings | None, kw: dict) -> dict:
        cfg = kw.get("cfg") or self.config
        if not (s and s.requires.declared and not cfg.replay.all):
            return kw
        size = routing.request_tokens(text, kw.get("system", ""), kw.get("json_schema"))
        found = routing.Discovery(cfg, self.repo, kw.get("device", "local"), layer=self.layer)
        servers = json.loads(kw.get("mcp_config") or "{}").get("mcpServers", {})
        bare, denied = routing.reach(kw)
        effective = replace(s, tools=bare, denied=denied, mcp=tuple(servers))
        picked = routing.select(found, effective, request_tokens=size)
        kw = {**kw, "route": routing.described_selection(picked, effective, size)}
        if picked.error:
            kw["refusal"] = picked.error
        elif alternative := picked.route:
            kw.update(model=alternative.model, runner=alternative.runner, effort=alternative.effort,
                      instructions=list(alternative.instructions), mode=alternative.mode)
            self.log(f"{step}: {picked.reason}")
        return kw

    def through_drops(self, step: str, text: str, cwd: Path, kw: dict, lined: bool, s: steps.Settings | None,
                      attempt_no: int) -> Result:
        kw.setdefault("add_dirs", self.add_dirs)
        try:
            with run_lines.beating(self.id, step) if lined else nullcontext():
                res = runner.run(text, cwd, trace=(self.id, step), **kw)
                if res.connection_failed:
                    self.log(f"{step}: lost the connection after {util.count(res.turns, 'turn')}, waiting for the network")
                    if self.wait_for_network():
                        self.log(f"{step}: the network is back, the dropped attempt cost {runner.cost_note(res)}")
                        res = self.again(step, text, cwd, kw, res, CONTINUE_AFTER_DROP)
                for n, wait in enumerate(BUSY_WAITS, 2):
                    if not provider_busy(res) or self.time_for(wait) < wait:
                        break
                    self.log(f"{step}: {res.error.strip().splitlines()[0][:200]}; "
                             f"try {n} of {len(BUSY_WAITS) + 1} in {wait}s")
                    time.sleep(wait)
                    res = self.again(step, text, cwd, kw, res, CONTINUE_AFTER_BUSY)
            return res
        finally:
            if s:
                record.label_step(self.id, step, attempt_no, atom=s.step, ab=s.ab_name, ab_side=s.ab_side)

    def again(self, step: str, text: str, cwd: Path, kw: dict, failed: Result, carry_on: str) -> Result:
        if failed.session.id and failed.turns > 1 and kw.get("persist", True):
            res = runner.run(carry_on, cwd, trace=(self.id, step), **{**kw, "resume": failed.session, **self.retry_kw()})
        else:
            res = runner.run(text, cwd, trace=(self.id, step), **{**kw, **self.retry_kw()})
        return runner.with_spent(res, [failed])

    def record_step(self, step: str, res: Result, s: steps.Settings | None, kw: dict, seam, attempt_no: int,
                    started: float, spent: float, chosen: tuple[str, str, str]) -> Result:
        which, model, effort = chosen
        if s and res.session.id:
            self.sessions[config.INHERITS.get(s.step, s.step)] = res.session
        self.cost += res.cost_usd
        self.paid += res.paid_usd
        usage = {"runner": res.runner or which, "model": res.model or model, "effort": res.effort or effort,
                 "tier": kw["tier"], "credential": res.credential, "auth_mode": ", ".join(res.modes),
                 "provider": res.provider,
                 "kind": kw["kind"], "cost_usd": round(self.cost - spent, 6), "tokens_in": res.tokens_in,
                 "tokens_out": res.tokens_out, "turns": res.turns, "cache_read": res.cache_read,
                 "cache_write": res.cache_write, "api_seconds": round(res.api_seconds, 2)}
        if s:
            usage["atom"] = s.step
        if s and s.ab_name:
            usage.update(ab=s.ab_name, ab_side=s.ab_side)
        if seam is not None:
            seam.add(error="" if res.ok else res.error or "", **usage)
        else:
            self.write_line("step_end", step=step, ok=res.ok, seconds=round(time.monotonic() - started, 1),
                            error=(res.error or "")[:500], **{"try": attempt_no}, **usage)
        self.save(f"{step}.json", {"ok": res.ok, "session": res.session.id, "cost": res.cost_usd,
                                   "auth_mode": usage["auth_mode"], "auth_modes": list(res.modes),
                                   "paid_usd": res.paid_usd,
                                   "turns": res.turns, "text": res.text, "structured": res.structured,
                                   "error": res.error, "runner": usage["runner"], "model": usage["model"],
                                   "skipped": res.skipped})
        self.log(f"{step}: {'ok' if res.ok else 'FAILED ' + util.first_line(res.error)} {runner.cost_note(res)}")
        return res


class CatchUpNeedsInput(workspace.RunFailed):
    pass


class CatchUpGateHold(workspace.RunFailed):
    def __init__(self, gate: str, why: str, head: str, report: str,
                 verified: verify.Verified | None = None, review: Review | None = None):
        super().__init__(why)
        self.gate, self.head, self.report = gate, head, report
        self.verified, self.review = verified, review
        self.candidate_restored = True


class CatchUpPushUnconfirmed(workspace.RunFailed):
    def __init__(self, why: str, detail: str, *, retryable: bool):
        super().__init__(why, detail)
        self.retryable = retryable


class CatchUpPathCollision(workspace.RunFailed):
    def __init__(self, name: str, kind: str):
        super().__init__(f"the merge would overwrite {kind} file `{name}`")
        self.name = name


@dataclass(kw_only=True)
class Run(AgentRun):
    tracker: tracker.Tracker
    tid: str
    force: bool = False
    feedback: list[dict] = field(default_factory=list)
    claim_feedback_on_revise: bool = False
    closed_feedback_prs: tuple[str, ...] = field(default=(), init=False)
    answering: dict = field(default_factory=dict)
    path: str = ""
    alone: bool = False
    facts: dict = field(default_factory=dict)
    catch_up_owns_checkout: bool = field(default=False, init=False)
    catch_up_preserved: set[str] = field(default_factory=set)
    catch_up_cleanup_park: tuple[str, str, str] | None = field(default=None, init=False)
    closed_checkouts: list[workspace.Checkout] = field(default_factory=list)
    repairs: str = ""
    action: str = ""
    action_only: bool = False
    workspace: tuple[Path, list[workspace.Checkout]] | None = None
    parking: bool = field(default=False, init=False)
    failure: dict | None = None
    asks_made: int = 0
    answers_due: list[dict] = field(default_factory=list)
    base_note: str = ""
    kept_note: str = ""
    owner_questions_blocked: bool = False
    environment_release: str = ""
    preview_of: str = ""
    recovery: dict = field(default_factory=dict)
    code_review: bool = False
    spec_root: str = ""
    spec_identity: str = ""

    @property
    def closed_checkout_paths(self) -> set[Path]:
        return {c.wt for c in self.closed_checkouts}

    @property
    def preview_branch(self) -> str:
        return f"arete/dry-run/{self.id}" if self.preview_of else ""

    @property
    def workspace_key(self) -> str:
        return self.id if self.preview_of else str(self.tid)

    @property
    def ref(self) -> str:
        return self.tracker.ref(self.tid)

    @property
    def ab_ticket(self) -> tuple[str, str] | None:
        return (self.repo.name, str(self.tid))

    @property
    def asks_owner(self) -> bool:
        return not self.owner_questions_blocked and factory.inside(self.id) is not None

    @property
    def owner_note(self) -> str:
        return owner_asks.GUIDE if self.asks_owner else owner_asks.NOBODY

    def agent(self, step: str, text: str, cwd: Path, role: str = "", **kw) -> Result:
        if kw.get("read_only"):
            return super().agent(step, text, cwd, role, **kw)
        if self.recovery.get("direction") and role in ("plan", "build", "check_repair", "review_repair", "ci_repair"):
            from . import unblock

            text = unblock.brief(self, text)
        if not self.asks_owner:
            due = self.answers_due if step == "plan" and not self.path else []
            if due:
                text = self.fresh_text(text, due)
            res = super().agent(step, text, cwd, role, **kw)
            if due and res.ok:
                asks_store.asks_taken([a["id"] for a in due], self.id)
                self.answers_due = []
            if self.owner_questions_blocked and res.record and any(
                    event.get("kind") == "tool_call" and owner_asks.is_ask(event.get("name"))
                    for event in record.events(paths.record_dir(self.id) / res.record)):
                raise CatchUpNeedsInput(f"{step} needs owner input")
            return res
        due = self.due_for(step)
        if kw.get("native_observation"):
            if due:
                raise RuntimeError("a pending owner decision precludes unattended native verification")
            return super().agent(step, text, cwd, role, **kw)
        if due:
            text, kw = self.with_answers(due, text, kw)
        res = super().agent(step, text, cwd, role, ask_owner=True, **kw)
        if due and kw["resume"].id and not res.ok and owner_asks.session_gone(res.error):
            self.log(f"{step}: the kept session is gone, so the step starts again with the ask and the answers")
            res = super().agent(step, text, cwd, role, ask_owner=True, **{**kw, "resume": NO_SESSION})
        if due and res.ok:
            asks_store.asks_taken([a["id"] for a in due], self.id)
            self.answers_due = [a for a in self.answers_due if a not in due]
        while res.record and (sent := owner_asks.found(record.events(paths.record_dir(self.id) / res.record))):
            kept = res.session if kw.get("persist", True) else NO_SESSION
            self.owner_ask(step, sent, kept)
            res = super().agent(step, owner_asks.settled_message(sent), cwd, role, ask_owner=True,
                                **{**kw, "resume": kept, "resume_only": True})
        return res

    def due_for(self, call: str) -> list[dict]:
        seam = factory.inside(self.id).step
        here = [a for a in self.answers_due if a["step"] == seam]
        return [a for a in here if a["call"] == call] or here

    def fresh_text(self, text: str, due: list[dict]) -> str:
        return "\n\n".join(part for part in (text, owner_asks.fresh_note(due), self.kept_note, self.base_note)
                           if part)

    def with_answers(self, due: list[dict], text: str, kw: dict) -> tuple[str, dict]:
        session = Session(**due[0]["session"]) if due[0]["session"] else NO_SESSION
        previous = kw.get("fresh_prompt")

        def fresh_prompt(target: str) -> str:
            return self.fresh_text(previous(target) if previous else text, due)

        options = {**kw, "fresh_prompt": fresh_prompt}
        if session.id and kw.get("persist", True):
            feedback = feedback_markdown(self.feedback) if due[0]["step"] == "revise" else ""
            parts = (owner_asks.answers_message(due),
                     f"Address the owner's PR feedback as well:\n\n{feedback}" if feedback else "",
                     self.kept_note, self.base_note,
                     self.moved_folder_note(due[0]["run"]))
            message = "\n\n".join(part for part in parts if part)
            return message, {**options, "resume": session}
        return self.fresh_text(text, due), {**options, "resume": NO_SESSION}

    def moved_folder_note(self, asking_run: str) -> str:
        if not asking_run or asking_run == self.id:
            return ""
        script = self.dir / CHECKS_SCRIPT
        checks = f", for example `{script} NAME` for a check" if script.exists() else ""
        return (f"This run's folder is `{self.dir}`, not the one of the run that started this session. Commands from "
                f"earlier in the conversation that name the earlier run's folder no longer run: use the same commands "
                f"under this run's folder{checks}.")

    def owner_ask(self, step: str, sent: list[dict], session: Session) -> None:
        if self.owner_questions_blocked:
            raise CatchUpNeedsInput(f"{step} needs owner input")
        ask_ids = [f"{self.id}-a{self.asks_made + n}" for n in range(1, len(sent) + 1)]
        self.asks_made += len(sent)
        limit, seam = self.config.ask.max_per_run, factory.inside(self.id).step
        if self.asks_made > limit:
            raise owner_asks.Asked(sent, ask_ids, seam, step, session, limit=limit)
        if (self.config.ask.settle and all(owner_asks.settles(ask) for ask in sent) and session.id
                and jev.ready(self.config, [self.repo]) and self.settled_by_jev(step, sent, ask_ids)):
            return
        raise owner_asks.Asked(sent, ask_ids, seam, step, session)

    def settled_by_jev(self, step: str, sent: list[dict], ask_ids: list[str]) -> bool:
        answers = []
        for ask in sent:
            state_text = (f"Step: {step}\nThe ask, with every question, option and description:\n"
                          f"{json.dumps(ask, indent=1)}")
            reply = jev.ask(state_text, {"settle": jev.yes_no(owner_asks.SETTLE)}, self.config,
                            trace=(self.id, f"{step} ask jev"))
            answer = reply.answers.get("settle")
            if not answer or answer.yes < owner_asks.SETTLE_AT:
                return False
            answers.append(answer)
        for ask_id, ask, answer in zip(ask_ids, sent, answers, strict=True):
            self.log(f"{step}: took the recommended options without asking the owner ({answer.note()})")
            run_lines.write(self.id, "ask_settled", ask_id=ask_id, step=factory.inside(self.id).step, call=step,
                        choices=owner_asks.choices(ask), p=round(answer.yes, 3))
        return True

    @property
    def where(self) -> str:
        return tracker.ticket_name(self.repo.name, self.ref)

    @property
    def triage_key(self) -> str:
        return self.ref if self.repo.tracker == "jira" else self.where

    @property
    def feedback_prs(self) -> list[str]:
        return list(dict.fromkeys(f["pr_url"] for f in self.feedback))

    def feedback_closure(self, prs: list[str] | None = None) -> str:
        prs = self.feedback_prs if prs is None else prs
        self.closed_feedback_prs = tuple(url for url in prs if gh.pr_view(url).get("state") != "OPEN")
        if self.closed_feedback_prs:
            return f"the PR closed before arete could revise it: {', '.join(self.closed_feedback_prs)}"
        return ""

    def claim_feedback(self) -> None:
        if self.feedback and not self.dry_run and not self.alone:
            self.feedback = insights_store.take_feedback(self.feedback, self.id)

    @property
    def failed_state(self) -> str:
        repair_waits_on_pr = self.repairs and (self.failure or {}).get("reason") != "policy-changed"
        return "pr" if self.feedback or repair_waits_on_pr else "failed"

    @property
    def retry_hint(self) -> str:
        if self.feedback:
            return (f"Comment on the PR again to retry: a submitted review starts at once, a comment after "
                    f"{FEEDBACK_QUIET_MINUTES} quiet minutes")
        if self.repairs and self.failed_state == "pr":
            return f"Continue it with `arete resume {self.id}`"
        return f"Remove `{self.tracker.label_name('failed')}` to retry"

    def enter(self, status: str) -> None:
        if self.dry_run:
            return
        others = [s for s in self.tracker.states if s != status]
        labeled = self.tracker.label(self.tid, add=[status], remove=others)
        record.note(self.id, label_pending=status if labeled else "")
        guarded_jira = isinstance(self.tracker, tracker.JiraTracker) and (
            self.spec_root or spec_store.find(self.repo.name, str(self.tid)))
        moved = (self.tracker.move(self.tid, status, keep_open=True) if guarded_jira
                 else self.tracker.move(self.tid, status))
        for err in (labeled, moved):
            if err:
                self.log(f"tracker: {util.first_line(err, 300)}")
        if not moved and isinstance(self.tracker, tracker.JiraTracker) and self.tracker.cfg.transitions.get(status):
            record.note(self.id, moved=status)

    def leave(self, status: str) -> None:
        if not self.dry_run and (err := self.tracker.label(self.tid, remove=[status])):
            self.log(f"tracker: {util.first_line(err, 300)}")

    def say(self, body: str, *, on_ticket: bool = False) -> str:
        if self.dry_run:
            return ""
        body = public_text(self, body)
        if self.feedback and not on_ticket:
            err = self.say_on_prs(body)
        else:
            err = self.tracker.comment(self.tid, f"{tracker.ARETE_TAG} {body}")
        if err:
            self.log(f"tracker: {util.first_line(err, 300)}")
        return err

    def say_on_prs(self, body: str) -> str:
        errors = []
        for url in self.feedback_prs:
            try:
                gh.pr_comment(url, public_text(self, body))
            except gh.GhError as e:
                errors.append(f"{url}: {e}")
        return "; ".join(errors)

    def retry_gate_evidence(self) -> None:
        if self.dry_run or not self.repo.parts:
            return
        for pending in run_store.pr_gate_refreshes(self.repo.name, str(self.tid)):
            pr = pending["pr_url"]
            self.log(f"automerge: retrying gate evidence refresh in {pr}")
            if update_pr_gate_evidence(self, pr, pending["evidence"], expected_head=pending["head"]):
                run_store.clear_pr_gate_refresh(self.repo.name, str(self.tid), pr)

    def give_up(self, why: str) -> None:
        if self.alone:
            return
        self.enter(self.failed_state)
        self.say(f"Run `{self.id}` {why}. {self.retry_hint}.")


def workspace_owner(run: Run, ws: Path, checkouts: list[workspace.Checkout]) -> workspace.WorkspaceOwner:
    row = workspaces.open_for(ws.name)
    earlier = workspaces.owner_of(row) if row and row["owner_run"] else None
    if run.alone and earlier:
        return earlier
    return workspace.WorkspaceOwner(run.id, run.repo.name, str(run.tid), checkouts)


def claim_workspace(run: Run, ws: Path, checkouts: list[workspace.Checkout]) -> None:
    owner = workspace_owner(run, ws, checkouts)
    workspaces.claim(ws, owner.repo, owner.ticket, owner.run, checkouts=owner.checkouts)
    run.workspace = ws, checkouts


def release_workspace(run: Run, ws: Path, checkouts: list[workspace.Checkout]) -> bool:
    try:
        if run.closed_checkouts or run.catch_up_preserved:
            workspace.note_made(ws, checkouts, workspace_owner(run, ws, checkouts), preserve=run.catch_up_preserved,
                                excluded=run.closed_checkout_paths)
            why = ("it contains closed PR worktrees" if run.closed_checkouts
                   else "it contains files that existed before catch-up")
            run.log(f"workspace kept at {ws}: {why}")
            return False
        release = workspace.remove_workspace(ws, checkouts, by=run.id)
    except (OSError, sqlite3.Error) as e:
        run.log(f"workspace kept at {ws}: removal failed: {util.first_line(str(e), 300)}")
        return False
    if release.state == "removed":
        return True
    names = release.held_by
    shown = ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")
    run.log(f"workspace kept at {ws}: removal blocked by {shown or 'the folder could not be deleted'}")
    return False


WORKFLOW_DENY = [".github/workflows/**"]


def workflow_denied(name: str, prefix: str) -> bool:
    return util.matches_any(name, WORKFLOW_DENY) or util.matches_any(prefix + name, WORKFLOW_DENY)


def restore_paths(wt: Path, source: str, paths: list[str]) -> None:
    git(["restore", "--source", source, "--staged", "--worktree", "--", *paths],
        wt, env={"GIT_LITERAL_PATHSPECS": "1"})


def commit_kept(run_id: str, c: workspace.Checkout, layer: config.RepoLayer | None) -> None:
    head = git(["rev-parse", "HEAD"], c.wt).strip()
    workspace.drop_untracked_bytecode(c.wt)
    diffguard.add(c.wt)
    deny = diffguard.denied_paths(layer) if layer else diffguard.ALWAYS_DENY
    holding = vault.held(git(["diff", *vault.SCAN, "--cached", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/"],
                             c.wt)) if vault.every() else {}
    changed = util.git_records(["diff", "--cached", "--no-renames", "--name-only"], c.wt)
    fatal = [n for n in changed if n in holding or workflow_denied(n, c.prefix)]
    if fatal:
        restore_paths(c.wt, head, fatal)
    if denied := [n for n in changed if n not in fatal
                  and (util.matches_any(n, deny) or util.matches_any(c.prefix + n, deny))]:
        git(["reset", "-q", "--", *denied], c.wt)
    if git(["diff", "--cached", "--name-only"], c.wt).strip():
        message = f"wip: edits of arete run {run_id}, kept when it ended before pushing\n\nArete-Run: {run_id}"
        util.git_as_owner(["commit", "-q", "-m", message], c.wt)
        c.onto = c.onto or head


def keep_edits(run: Run) -> None:
    if run.dry_run or not run.workspace:
        return
    for c in run.workspace[1]:
        if not (c.wt / ".git").exists():
            continue
        try:
            commit_kept(run.id, c, run.layer)
        except (RuntimeError, OSError) as e:
            run.log(f"could not commit the edits in {c.github}, so they stay uncommitted in {c.wt}: {e}")
            try:
                c.onto = c.onto or git(["rev-parse", "HEAD"], c.wt).strip()
            except (RuntimeError, OSError):
                pass


def kept_heads(run: Run) -> dict:
    if not run.workspace:
        return {}
    try:
        return {c.prefix or ".": {"head": git(["rev-parse", "HEAD"], c.wt).strip(), "onto": c.onto}
                for c in run.workspace[1] if c.onto and (c.wt / ".git").exists()}
    except (RuntimeError, OSError):
        return {}


def stale_base_note(c: workspace.Checkout) -> str:
    return (f"Compare with `origin/<base>`, as in `{c.diff_from_base}`, never with the local base branch: arete "
            "fetched `origin`, but the local base branch can be days old.")


def workspace_note(branch: str, checkouts: list[workspace.Checkout]) -> str:
    if len(checkouts) == 1 and not checkouts[0].dir:
        c = checkouts[0]
        return (f"You are in a worktree on branch `{branch}`, {c.starting_point}. PRs target `{c.base}`. "
                f"{stale_base_note(c)}")
    rows = [f"- `{c.prefix}`: {c.github}, {c.starting_point}; PRs target `{c.base}`" for c in checkouts]
    return "\n".join([
        f"The working directory holds one git worktree per repo, each on branch `{branch}`:", "", *rows, "",
        ("The working directory itself is not a git repo, so run git as `git -C <dir> ...`. A change can touch one "
         f"repo or several; arete commits each changed repo and opens a PR in it. {stale_base_note(checkouts[0])}"),
    ])


def compare_with_fork(checkouts: list[workspace.Checkout]) -> str:
    return ", ".join(f"`{c.diff_from_fork}`" for c in checkouts)


def git_reads(checkouts: list[workspace.Checkout]) -> list[str]:
    return [f"git -C {c.dir} {cmd}*" for c in checkouts if c.dir for cmd in ("status", "diff", "log", "show")]


@dataclass
class Staged(diffguard.Diff):
    patch: str = ""
    changed: list[workspace.Checkout] = field(default_factory=list)
    merged: list[workspace.Checkout] = field(default_factory=list)
    bases: dict[str, str] = field(default_factory=dict)
    restores: dict[str, tuple[str, list[str]]] = field(default_factory=dict)
    fatal_restores: dict[str, list[str]] = field(default_factory=dict)
    collisions: list[str] = field(default_factory=list)
    earlier_denied: list[str] = field(default_factory=list)
    fatal_denied: bool = False

    @property
    def empty(self) -> bool:
        return not self.files and not self.merged


def base_of_head(c: workspace.Checkout, head: str = "HEAD", *, check: bool = True) -> str:
    return git(["merge-base", c.origin_base, head], c.wt, check=check).strip()


def has_unpublished_base(c: workspace.Checkout) -> bool:
    published = c.publication_baseline or c.onto
    if not published:
        return False
    base = base_of_head(c, check=False)
    return bool(base) and git(["merge-base", base, published], c.wt, check=False).strip() not in ("", base)


def comparison_label(c: workspace.Checkout, against: str, from_base: bool) -> str:
    sha = git(["rev-parse", "--short=7", against], c.wt).strip()
    name = c.base if from_base else ("HEAD" if against == "HEAD" else "the starting commit")
    return f"{name} at {sha}"


def stageable_untracked(wt: Path, *, ignored: bool = False) -> set[str]:
    flags = ["--ignored"] if ignored else []
    return set(util.git_records(["ls-files", "--others", *flags, "--exclude-standard", "--", ".",
                                 *diffguard.NEVER_STAGED], wt))


def changed_tree_paths(wt: Path, before: str, after: str) -> set[str]:
    return set(util.git_records(["diff", "--no-renames", "--ignore-submodules=none", "--name-only",
                                 before, after], wt))


def stage(checkouts: list[workspace.Checkout], deny: list[str], from_base: bool = False, *,
          worker_trees: dict[str, str] | None = None, ignored_denied_paths: dict[str, list[str]] | None = None) -> Staged:
    out = Staged()
    for c in checkouts:
        workspace.drop_untracked_bytecode(c.wt)
        against = base_of_head(c) if from_base else (c.onto or "HEAD")
        if retained := (ignored_denied_paths or {}).get(c.github):
            if names := stageable_untracked(c.wt, ignored=True).intersection(retained):
                git(["add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"], c.wt,
                    input="\0".join(sorted(names)) + "\0", env={"GIT_LITERAL_PATHSPECS": "1"})
        diff = diffguard.stage(c.wt, deny, against=against, prefix=c.prefix)
        merged = pending_merge(c)
        if merged and not from_base:
            brought = diffguard.between(c.wt, c.lease, c.merge, prefix=c.prefix).counts
            diff.counts = {name: 0 for name in brought} | diff.counts
        out.bases[c.github] = against
        if merged:
            out.merged.append(c)
        restore_from = against if worker_trees is None else worker_trees.get(c.github, "HEAD")
        worker_diff = diff if restore_from == against else diffguard.between(
            c.wt, "--cached", restore_from, deny=deny, prefix=c.prefix)
        worker_denied = worker_diff.denied
        workflows = {name for name in diff.denied + worker_denied
                     if workflow_denied(name.removeprefix(c.prefix), c.prefix)}
        out.earlier_denied.extend(name for name in diff.denied if name not in worker_denied and name not in workflows)
        diff.denied = sorted(set(worker_denied) | workflows)
        if not merged and not diff.counts and not worker_diff.files:
            continue
        shown = checkout_patch(c, diff.locks, "--cached", against)
        resolving = bool(merged and c.resolved and not from_base)
        resolved = resolved_patch(c) if resolving else ""
        fatal = workflows.copy()
        if vault.every():
            whole = checkout_patch(c, [], *vault.SCAN, "--cached", against)
            scans = [whole + (resolved_patch(c, *vault.SCAN) if resolving else "")]
            if restore_from != against:
                scans.append(checkout_patch(c, [], *vault.SCAN, "--cached", restore_from))
            refusals = {}
            for patch in scans:
                for name, keys in vault.held(patch).items():
                    fatal.add(name)
                    refusals[f"{vault.hidden(name)} (holds the stored value of {', '.join(keys)})"] = None
            diff.denied.extend(refusals)
        if fatal:
            out.fatal_denied = True
            if paths := [name.removeprefix(c.prefix) for name in diff.files if name in fatal]:
                out.fatal_restores[c.github] = paths
        if worker_denied:
            allowed = set(worker_diff.files).difference(worker_denied, fatal)
            allowed.update(c.prefix + name for name in workspace.ignored_file_paths(c.wt))
            paths = []
            for name in worker_denied:
                if name in fatal:
                    continue
                if any(name.startswith(path + "/") or path.startswith(name + "/") for path in allowed):
                    out.collisions.append(name)
                else:
                    paths.append(name.removeprefix(c.prefix))
            if paths:
                out.restores[c.github] = restore_from, paths
        out += diff
        if merged or diff.counts:
            out.changed.append(c)
        out.patch += shown + resolved
    out.patch = vault.hidden(out.patch)
    return out


def resolved_patch(c: workspace.Checkout, *flags: str) -> str:
    return git(["diff", "--no-renames", *flags, f"--src-prefix=a/{c.prefix}", f"--dst-prefix=b/{c.prefix}",
                c.base_commit, "HEAD", "--", *c.resolved], c.wt)


def pending_merge(c: workspace.Checkout) -> bool:
    return bool(c.merge and c.lease) and git(["rev-list", "--count", f"{c.lease}..{c.merge}"], c.wt).strip() != "0"


def checkout_patch(c: workspace.Checkout, locks: list[str], *revs: str) -> str:
    patch = git(["diff", "--no-renames", f"--src-prefix=a/{c.prefix}", f"--dst-prefix=b/{c.prefix}", *revs,
                 "--", ".", *(f":(exclude){name.removeprefix(c.prefix)}" for name in locks)], c.wt)
    return patch + (f"\n(lockfile changes not shown: {', '.join(locks)})\n" if locks else "")


def lines_by_file(staged: diffguard.Diff, top: int = 12) -> str:
    rows = sorted(((name, n) for name, n in staged.counts.items() if n), key=lambda r: (-r[1], r[0]))
    out = [f"- `{name}`: {n}" for name, n in rows[:top]]
    if len(rows) > top:
        out.append(f"- {len(rows) - top} more files: {sum(n for _, n in rows[top:])}")
    return "\n".join(out)


VERIFY_SCRATCH_ROOT = Path(".arete-sandbox")
VERIFY_SCRATCH_MARKER = ".arete-owned"
VERIFY_SCRATCH_CONTENT = b"arete-verification-scratch-v1"


def names_verification_wrapper(rule: str) -> bool:
    try:
        words = shlex.split(rule)
    except ValueError:
        return False
    return bool(words) and verify.trusted_wrapper(words[0])


def initialize_resolver_scratch(wt: Path, bash_rules: list[str]) -> None:
    if not any(names_verification_wrapper(rule) for rule in bash_rules):
        return
    with local_paths.directory_fd(wt.resolve()) as wt_fd:
        try:
            os.mkdir(VERIFY_SCRATCH_ROOT, 0o700, dir_fd=wt_fd)
        except FileExistsError:
            return
        with local_paths.directory_fd(VERIFY_SCRATCH_ROOT, wt_fd) as folder:
            for name, content in ((".gitignore", b"*\n"), (VERIFY_SCRATCH_MARKER, VERIFY_SCRATCH_CONTENT + b"\n")):
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=folder)
                with os.fdopen(fd, "wb") as file:
                    file.write(content)


def resolver_scratch_paths(names: set[str], roots: set[Path]) -> set[str]:
    return {n for n in names if roots and not roots.isdisjoint(Path(n).parents)}


def restorable_scratch_paths(wt: Path, incoming: str, roots: set[Path]) -> set[str]:
    if not roots:
        return set()
    deletions = ["diff", "--cached", "--no-renames", "--ignore-submodules=none", "--name-only", "--diff-filter=D"]
    deleted = {path for parent in ("HEAD", incoming) for path in util.git_records([*deletions, parent], wt)}
    return resolver_scratch_paths(deleted, roots)


def resolver_file_metadata(path: Path) -> tuple[int, ...] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def resolver_scratch_metadata(wt: Path, name: str) -> tuple[int, ...] | dict[Path, tuple[int, ...] | None] | None:
    path = wt / name
    metadata = resolver_file_metadata(path)
    if not name.endswith("/") or path.is_symlink():
        return metadata
    contents = {path: metadata}
    for root, dirs, files in os.walk(path):
        for child in dirs + files:
            entry = Path(root, child)
            contents[entry] = resolver_file_metadata(entry)
    return contents


def resolver_ignored_paths(wt: Path, parent_tracked: set[str]) -> set[str]:
    return {n for n in workspace.ignored_file_paths(wt)
            if n in parent_tracked or not (Path(n).parent.name == "__pycache__" and Path(n).suffix == ".pyc")}


def verification_marker(wt_fd: int, root: Path) -> tuple[int, int, int] | None:
    try:
        with ExitStack() as opened:
            folder = opened.enter_context(local_paths.directory_fd(root, wt_fd))
            fd = os.open(VERIFY_SCRATCH_MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            opened.callback(os.close, fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None
            with os.fdopen(fd, "rb", closefd=False) as file:
                if file.read(len(VERIFY_SCRATCH_CONTENT) + 2).removesuffix(b"\n") == VERIFY_SCRATCH_CONTENT:
                    return info.st_dev, info.st_ino, info.st_ctime_ns
    except OSError:
        return None
    return None


def verification_scratch_markers(wt: Path, candidates: set[Path] | None = None) -> dict[Path, tuple[int, int, int]]:
    if candidates is None:
        markers = ["--exclude-standard", "--", f":(glob)**/{VERIFY_SCRATCH_MARKER}"]
        visible = util.git_records(["ls-files", "--cached", "--others", *markers], wt)
        ignored = util.git_records(["ls-files", "--ignored", "--others", *markers], wt)
        candidates = {Path(n).parent for n in visible + ignored} - {Path(".")}
        candidates.add(VERIFY_SCRATCH_ROOT)
    with local_paths.directory_fd(wt.resolve()) as wt_fd:
        return {root: marker for root in candidates if (marker := verification_marker(wt_fd, root)) is not None}


def untracked_by_dir(dirs: list[Path]) -> dict[Path, set[str]]:
    return {d: workspace.untracked_files(d) for d in dirs}


def untracked_added_since(before: dict[Path, set[str]]) -> list[tuple[Path, str]]:
    return [(d, name) for d, names in before.items() for name in sorted(workspace.untracked_files(d) - names)]


def check_lanes(layer: config.RepoLayer, run: AgentRun) -> list[str]:
    return lanes.names(layer.parallel_checks, run.config.max_parallel_checks)


def tail_of_both_streams(stdout: str, stderr: str, n: int) -> str:
    stdout_room = n - min(len(stderr), n // 2)
    stderr_room = n - min(len(stdout), n // 2)
    return util.tail(stdout, stdout_room) + util.tail(stderr, stderr_room)


def selected_checks(layer: config.RepoLayer, files: list[str]) -> set[str]:
    selected = set(layer.quick_checks) & layer.checks.keys()
    for pattern, names in layer.targeted_checks.items():
        if any(util.matches_any(name, [pattern]) for name in files):
            selected.update(names)
    return selected or set(layer.checks)


@contextmanager
def worker_suite_lane(layer: config.RepoLayer, run: AgentRun):
    with ExitStack() as stack:
        files = [stack.enter_context(util.lock_file(name)) for name in check_lanes(layer, run)]
        if not lanes.take_free(files):
            run.log("checks: waiting for an agent's full suite run to finish")
            with run.waiting("slot"):
                while not lanes.take_free(files):
                    time.sleep(CHECK_LANE_POLL_SECONDS)
        yield


@contextmanager
def check_lane(layer: config.RepoLayer, run: AgentRun):
    with (slots.check_slot(run, parallel=layer.parallel_checks) as lease, check_process.suite(lease) as hold_fds,
          worker_suite_lane(layer, run)):
        yield hold_fds


def run_checks(layer: config.RepoLayer, cwd: Path, git_dirs: list[Path], run: AgentRun,
               timeout: float, *, selected: set[str] | None = None,
               defer_failed_cleanup: bool = False, focused: dict[str, list[str]] | None = None,
               results: list[dict] | None = None) -> tuple[bool, str]:
    commands = layer.checks
    if selected is not None:
        available = layer.checks | layer.targeted_commands
        if missing := selected - available.keys():
            raise ValueError(f"unknown selected checks: {', '.join(sorted(missing))}")
        commands = {name: command for name, command in available.items() if name in selected}
    if focused:
        available = layer.checks | layer.targeted_commands
        commands = {}
        for name, selectors in focused.items():
            if name not in available:
                raise ValueError(f"unknown selected check: {name}")
            if selectors:
                prefix = check_repair.classes_command(layer, name)
                if not prefix or not all(isinstance(s, str) and check_repair.SELECTOR.fullmatch(s) for s in selectors):
                    raise ValueError("focused tests require a configured runner and dotted test names")
                commands[name] = shlex.join([*prefix, *selectors])
            else:
                commands[name] = available[name]
    if not commands:
        return True, "no checks configured"
    quick = [(name, commands[name]) for name in layer.quick_checks if name in commands]
    slow = [(name, command) for name, command in commands.items() if name not in layer.quick_checks]
    untracked_before = untracked_by_dir(git_dirs)
    report, ok = [], True
    cleanup_untracked = not defer_failed_cleanup

    def execute(items, hold_fds):
        nonlocal ok
        held = {"hold_fds": hold_fds} if hold_fds else {}
        for name, cmd in items:
            with record.timed(run.id, "check", name=name, command=cmd) as check:
                res = environment.sh(cmd, run.config, run.repo, cwd=cwd, timeout=run.time_for(timeout), **held)
                check["exit"] = res.returncode
            passed = res.returncode == 0
            if results is not None:
                detail = ({"name": name} if passed else check_repair.failed_check(
                    layer, name, res.stdout + "\n" + res.stderr, res.returncode))
                results.append({**detail, "passed": passed})
            ok = ok and passed
            run.log(f"check {name}: {'pass' if passed else f'FAIL (exit {res.returncode})'}")
            if not passed:
                report.append(f"### {name} failed: `{cmd}` exit {res.returncode}\n```\n{tail_of_both_streams(res.stdout, res.stderr, 6000)}\n```")
            else:
                report.append(f"### {name} passed: `{cmd}`")

    try:
        with check_lane(layer, run) as hold_fds:
            execute(quick, hold_fds)
            if slow and ok:
                execute(slow, hold_fds)
            elif slow:
                report.append("Not run because cheap checks failed: " + ", ".join(name for name, _ in slow))
        if focused or (selected is not None and set(commands) != set(layer.checks)):
            report.append("Targeted pass only; the full suite is still required before publication.")
        cleanup_untracked = cleanup_untracked or ok
    finally:
        if cleanup_untracked:
            for d, name in untracked_added_since(untracked_before):
                workspace.remove_untracked(d, name)
    return ok, "\n\n".join(report)


class RetryNextPoll(Exception):
    pass


class Stopped(BaseException):
    pass


class Terminated(Stopped):
    pass


_termination: Terminated | None = None


def stop_on_term(run: Run, *, zte_grant: bool, finalizing: Callable[[], bool]) -> Callable:
    def stop(*_) -> None:
        global _termination
        finishing = finalizing()
        if not finishing and record.stop_asked(run.id):
            raise Stopped(record.STOPPED)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        if not finishing and not zte_grant and not run.parking and run.workspace:
            _termination = Terminated()
            workspace.termination_pending = True
            raise _termination
        os.kill(os.getpid(), signal.SIGTERM)
    return stop


@dataclass
class TicketWork:
    cfg: config.Config
    run: AgentRun
    ticket: tracker.Ticket
    ws: Path
    checkouts: list[workspace.Checkout]
    layer: config.RepoLayer
    timeout: float
    workspace: str
    attachments: str = ""
    steps: Counter = field(default_factory=Counter)

    @property
    def brief(self) -> prompt_loader.Brief:
        t = self.ticket
        return prompt_loader.Brief(self.run.repo.name, self.workspace, t.ref, t.title, t.body,
                                   comments=t.comments, attachments=self.attachments)

    @property
    def git_dirs(self) -> list[Path]:
        return [c.wt for c in self.checkouts]

    @property
    def read_bash(self) -> list[str]:
        return config.DEFAULT_ALLOW_BASH + git_reads(self.checkouts)

    def next_step(self, name: str) -> str:
        self.steps[name] += 1
        return round_name(name, self.steps[name])


@dataclass(kw_only=True)
class Build(TicketWork):
    tools: list[str]
    deny: list[str]
    report: str = ""
    staged: Staged = field(default_factory=Staged)
    committed_lines: int = 0
    left_for_a_person: str = ""
    check_commands: str = NO_CHECKS
    checked: dict = field(default_factory=dict)
    check_results: list[dict] = field(default_factory=list)
    from_base: bool = False
    plan_text: str = ""
    build_summary: str = ""
    catch_ups: int = 0
    ci_rerun_requested_heads: set[tuple[str, str]] = field(default_factory=set)
    worker_trees: dict[str, str] = field(default_factory=dict)
    ignored_denied_paths: dict[str, list[str]] = field(default_factory=dict)
    denied_feedback: str = ""
    on_denied_edit: Callable[[str], None] | None = None

    def resume(self, step: str, text: str, role: str = "build", **kw) -> Result:
        saved = self.run.sessions.get("build", NO_SESSION)
        settings = self.run.settings(role)
        feedback = text
        base_dirs = tuple(self.run.add_dirs)
        call_dirs = list(base_dirs)

        def primed(which: str) -> str:
            plan = self.run.dir / "plan.md"
            previous = util.read_json(self.run.dir / "build.json", {})
            task = (f"# Issue {self.ticket.ref}: {self.ticket.title}\n\n{self.ticket.body}\n\n"
                    f"## Owner comments\n\n{self.ticket.comments or ''}\n\n"
                    f"## Attachments\n\n{self.attachments}\n\n"
                    f"## Left for a person\n\n{self.left_for_a_person}\n")
            prompt, folder = context.builder_handoff(
                self.run.dir, run_id=self.run.id, step=step, runner=which,
                previous={"runner": saved.runner, "id": saved.id}, workspace=self.ws, roots=self.git_dirs,
                task=task, plan=self.plan_text or (plan.read_text() if plan.exists() else ""),
                summary=self.build_summary or previous.get("text", ""), feedback=feedback, checks=self.check_commands,
                secret_files=runner.secret_files(self.git_dirs or [self.ws], devices.LOCAL, self.cfg))
            call_dirs[:] = [*base_dirs, folder]
            return prompt_loader.task_prompt(f"{prompt}\nDenied paths: {prompt_loader.code_list(self.deny)}.")

        text = prompt_loader.task_prompt(text)
        if settings.session == "fresh" or (saved.id and saved.runner != settings.runner and settings.explicit_route):
            kw["resume"] = NO_SESSION
        return self.run.agent(step, text, self.ws, role=role, allowed_tools=self.tools,
                              disallowed_tools=routing.NO_GIT_WRITES, fresh_prompt=primed, add_dirs=call_dirs, **kw)

    def check(self, *, quick: bool = False, after: str = "the build and its fixes",
              staged: Staged | None = None, defer_failed_cleanup: bool = False,
              focused: dict[str, list[str]] | None = None) -> bool:
        previous, self.checked = self.checked, {}
        self.check_results = []
        self.report = "Checks started but did not complete; no passing result from this attempt."
        try:
            self.run.save("checks.md", self.report)
            staged = staged if staged is not None else self.restage()
            if staged.denied:
                self.report = self.restore_denied_edits(staged, after)
            elif self.denied_feedback:
                self.report = self.denied_feedback
            else:
                return self.check_commands_on_tree(staged, quick, previous, defer_failed_cleanup=defer_failed_cleanup,
                                                   focused=focused)
        except BaseException as e:
            self.checked = {}
            self.report = f"Checks did not complete: {type(e).__name__}: {util.first_line(str(e), 300)}"
            raise
        finally:
            self.run.save("checks.md", self.report)
        if self.run.budget and (reason := self.run.budget.repair_limit_reason()):
            raise runlimits.Parked(f"{reason}. {self.report}")
        return False

    def check_commands_on_tree(self, staged: Staged, quick: bool, previous: dict, *,
                               defer_failed_cleanup: bool = False, focused: dict[str, list[str]] | None = None) -> bool:
        selected = selected_checks(self.layer, staged.files) if quick else None
        full = not focused and (selected is None or selected == set(self.layer.checks))
        before = factory.fingerprint(self.ws)
        ok, self.report = run_checks(self.layer, self.ws, self.git_dirs, self.run, self.timeout, selected=selected,
                                     defer_failed_cleanup=defer_failed_cleanup, focused=focused, results=self.check_results)
        if ok:
            after = factory.fingerprint(self.ws)
            if before and after and before != after:
                self.report += "\n\nChecks modified the source tree; rerun the full suite on the resulting tree."
                return False
            if full:
                self.checked = {"tree": after, "commands": dict(self.layer.checks), "report": self.report}
            elif before and before == after == previous.get("tree") and previous.get("commands") == self.layer.checks:
                self.checked = previous
        return ok

    def restore_denied_edits(self, staged: Staged, after: str) -> str:
        if staged.fatal_denied:
            self.fail_denied_edits(staged, after)
        failure = f"{after} touched denied paths: {prompt_loader.code_list(staged.denied)}."
        instruction = "Reach the goal without them."
        if self.on_denied_edit:
            self.on_denied_edit(f"{failure} {instruction}")
        self.restore_staged_paths(staged)
        remaining = self.restage()
        if remaining.fatal_denied:
            self.fail_denied_edits(remaining, after)
        details = "Arete restored those paths to their starting state."
        if remaining.denied:
            detail = " would overwrite allowed edits" if remaining.collisions else " was incomplete"
            kept = " Arete restored the other denied paths." if staged.restores else ""
            details = f"Restoring {prompt_loader.code_list(remaining.denied)}{detail}.{kept}"
        return f"{failure} {details} {instruction}"

    def restore_staged_paths(self, staged: Staged) -> None:
        for c in self.checkouts:
            if restore := staged.restores.get(c.github):
                restore_paths(c.wt, *restore)
            if paths := staged.fatal_restores.get(c.github):
                restore_paths(c.wt, staged.bases[c.github], paths)
        if staged.restores or staged.fatal_restores:
            workspace.note_made(self.ws, self.checkouts, excluded=self.run.closed_checkout_paths)

    def fail_denied_edits(self, staged: Staged, after: str) -> None:
        self.restore_staged_paths(staged)
        raise workspace.RunFailed(f"{after} {staged.refusal(self.layer.max_diff_lines)}")

    def restage(self, *, from_base: bool | None = None) -> Staged:
        self.staged = stage(self.checkouts, self.deny, self.from_base if from_base is None else from_base,
                            worker_trees=self.worker_trees, ignored_denied_paths=self.ignored_denied_paths)
        record.mark(self.run.id, "diff", files=self.staged.files, lines=self.staged.lines, denied=self.staged.denied,
                    patch=self.staged.patch)
        return self.staged

    def require_valid_diff(self, after: str, planned: str = "", *, from_base: bool | None = None) -> None:
        self.validate_diff(self.restage(from_base=from_base), after, planned, from_base=from_base)

    def validate_diff(self, staged: Staged, after: str, planned: str = "", *, from_base: bool | None = None) -> None:
        if staged.fatal_denied:
            self.fail_denied_edits(staged, after)
        if staged.empty:
            raise workspace.RunFailed(f"{after} undid every change")
        if staged.denied:
            raise workspace.RunFailed(f"{after} {staged.refusal(self.layer.max_diff_lines)}")
        if staged.earlier_denied:
            raise workspace.RunFailed(f"{after} touched denied paths: {', '.join(staged.earlier_denied[:5])}")
        whole_diff = self.from_base if from_base is None else from_base
        counts, bases = staged, staged.bases
        earlier = 0
        if any(has_unpublished_base(c) for c in self.checkouts):
            whole_diff = True
            counts, bases, total = diffguard.Diff(), {}, 0
            for c in self.checkouts:
                against = bases[c.github] = base_of_head(c)
                diff = diffguard.between(c.wt, "--cached", against, prefix=c.prefix, moves=True)
                counts += diff
                total += diff.lines
        else:
            if not whole_diff:
                earlier = self.committed_lines + sum(c.resolved_lines for c in self.checkouts)
            total = staged.lines + earlier
        limit = self.layer.max_diff_lines
        if total > limit:
            comparisons = []
            for c in self.checkouts:
                label = comparison_label(c, bases[c.github], whole_diff)
                comparisons.append(f"{c.github} {label}" if len(bases) > 1 else label)
            against = ", ".join(comparisons)
            why = f"the diff is {total} lines against {against}, over the {limit} line limit"
            pushed = f"\n- pushed earlier in this run: {earlier}" if earlier else ""
            raise workspace.RunFailed(f"after {after} {why}{planned}; split the work",
                            f"### Lines changed by file\n\n{lines_by_file(counts)}{pushed}")

    def push(self, branch: str, message: str, *, before_push: Callable[[workspace.Checkout], str] | None = None,
             check_targets: Callable[[], None] | None = None) -> None:
        self.run.tracker.pr_link(self.ticket, whole=not self.left_for_a_person)
        commit_and_push(self.checkouts, self.staged.changed, branch, message, before_push=before_push,
                        check_targets=check_targets)
        self.committed_lines += self.staged.lines

    def check_with_repairs(self, step_name: Callable[[int], str] | None = None, *,
                           after: str = "the build and its fixes") -> tuple[bool, int]:
        step_name = step_name or (lambda n: self.next_step("check_repair"))
        attempts, tries = 1, self.run.settings("check_repair").tries
        ok = self.check(after=after)
        while not ok and attempts < tries:
            self.run.story.append(f"Checks failed on attempt {attempts}:\n{util.tail(self.report, 2500)}")
            attempts += 1
            self.resume(step_name(attempts),
                        prompt_loader.prompt("fix", attempt=attempts, max_attempts=tries, failures=self.report,
                               checks=self.check_commands, denied=prompt_loader.code_list(self.deny)), role="check_repair")
            ok = self.check(after=after)
        return ok, attempts

    def recheck(self, after: str) -> None:
        if not self.check_with_repairs(after=after)[0]:
            raise workspace.RunFailed(f"the checks failed after {after}", self.report)
        self.require_valid_diff(after)


def split_comment(estimate: int, limit: int, split: list[dict], queue_hint: str, info_label: str,
                  config_file: Path) -> str:
    parts = [(f"The plan comes to about {estimate} changed lines, over this repo's {limit} line limit, so I stopped "
              "before building.")]
    if split:
        parts.append("A split where each part fits and leaves the repo working, in build order:")
        parts += [f"### {n}. {s.get('title', '').strip()}\n\n{s.get('body', '').strip()}" for n, s in enumerate(split, 1)]
    parts.append(f"File the parts as {queue_hint}, or narrow this one, then remove `{info_label}`. To build it whole, "
                 f"raise `max_diff_lines` in `{paths.short_path(config_file)}`.")
    return "\n\n".join(parts)


@dataclass
class Script:
    path: str
    drive: Callable
    continues: str = ""
    only: str = ""
    overrides: dict[str, dict] = field(default_factory=dict)
    again: str = ""
    repairs: str = ""
    action: str = ""
    action_only: bool = False
    environment_release: str = ""

    @property
    def run_fields(self) -> dict:
        return {"path": self.path, "alone": bool(self.only or self.action_only), "step_overrides": self.overrides,
                "action": self.action, "action_only": self.action_only, "environment_release": self.environment_release}


def script_file(cfg: config.Config, name: str) -> Path:
    folder = paths.ADWS_DIR if name in ("orchestrate", "single_worker") else cfg.adws or paths.ADWS_DIR
    if not folder.is_dir():
        raise FileNotFoundError(f"adws folder {folder} does not exist")
    if not (found := folder / f"{name}.py").is_file():
        raise FileNotFoundError(f"no adw script {found}")
    return found


def flow_function(cfg: config.Config, name: str) -> Callable:
    found = script_file(cfg, name)
    if str(found.parent) not in sys.path:
        sys.path.insert(0, str(found.parent))
    spec = importlib.util.spec_from_file_location(f"adws_{name}", found)
    module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if (fn := getattr(module, name, None)) is None:
        raise AttributeError(f"{found} defines no function {name}, which a script in adws/ names after its file")
    return fn


def script_command(cfg: config.Config, name: str, *args: str) -> list[str]:
    script = script_file(cfg, name).resolve()
    if script.parent == paths.ADWS_DIR.resolve():
        entrypoint = (f"import sys, runpy; sys.path[:0] = {[str(paths.PACKAGE_DIR.parent), str(script.parent)]!r}; "
                      "from arete import codebase; codebase.load(templates=True); "
                      f"runpy.run_path({str(script)!r}, run_name='__main__')")
        return [sys.executable, "-I", "-c", entrypoint, *args]
    return sandbox.protected_command([sys.executable, str(script), *args])


def answer_text(asked_id: str) -> str:
    try:
        return (paths.RUNS_DIR / asked_id / ANSWER_FILE).read_text()
    except OSError:
        return ""


def recorded_path(run_id: str) -> str:
    from .store import orchestration

    if found := orchestration.find(run_id):
        return found["data"].get("mode", "orchestrate")
    for read in factory.chain(run_id):
        if execution := factory.first(read, "execution"):
            return str(execution.get("path") or "classic")
        if path := factory.first(read, "task").get("path"):
            return str(path)
    return "classic"


def custom_revision_feature(cfg: config.Config) -> bool:
    return bool(cfg.adws and cfg.adws.resolve() != paths.ADWS_DIR.resolve()
                and (cfg.adws / "feature.py").is_file() and not (cfg.adws / "revise.py").is_file())


def custom_feedback_fallback(cfg: config.Config, mode: str, feedback: list[dict]) -> bool:
    return (custom_revision_feature(cfg) and insights_store.has_new_feedback(feedback)
            and (mode == ADW or mode == "repair" and not (cfg.adws / "repair.py").is_file()))


def dispatch_path(repo: config.Repo, tid: str) -> str:
    newest = run_store.newest_whole_run(repo.name, tid)
    if newest and newest["status"] == "interrupted":
        return recorded_path(newest["id"])
    if pending_feedback(repo.name, tid) and (source := next(iter(run_store.open_runs(repo.name, tid)), None)):
        return recorded_path(source["id"])
    return wanted(repo)


def run_command(repo_name: str, tid: str | int, force: bool = False, dry_run: bool = False, *,
                spec: bool = False) -> list[str]:
    cfg = config.load()
    flags = [*(["--force"] if force else []), *(["--dry-run"] if dry_run else [])]
    repo = cfg.repo(repo_name)
    if not tracker.is_task_id(tid) and (spec or spec_store.find(repo.name, str(tid))):
        return script_command(cfg, "orchestrate", repo.name, str(tid), *flags)
    mode = dispatch_path(repo, str(tid))
    if (asked := answered_run(repo_name, str(tid))) and not dry_run:
        mode = recorded_path(asked["id"])
        if mode != "classic":
            return script_command(cfg, mode, repo_name, str(tid), *flags, "--continue", asked["id"])
    if mode == "classic":
        return control.command(["run", repo_name, str(tid), "--local", *flags])
    if mode in (ORCHESTRATED, "orchestrate"):
        from . import requeue
        from .store import orchestration

        newest = run_store.newest_whole_run(repo_name, str(tid))
        source = newest if newest and newest["status"] == "interrupted" else (
            next(iter(run_store.open_runs(repo_name, str(tid))), None)
            if not force and pending_feedback(repo_name, str(tid)) else None)
        if (requeue.policy_retry(run_store.newest_live_whole_run(repo_name, str(tid)))
                or dry_run and run_store.held_environment_branch(repo_name, str(tid))):
            source = None
        previous = (orchestration.find(source["id"]) or {}).get("current_run", source["id"]) if source else ""
        return script_command(cfg, "orchestrate", repo_name, str(tid), *flags,
                              *(["--continue", previous] if previous else []))
    if mode in (ADW, "repair") and custom_feedback_fallback(cfg, mode, pending_feedback(repo_name, str(tid))):
        return script_command(cfg, "feature", repo_name, str(tid), *flags)
    if mode != ADW:
        return script_command(cfg, mode, repo_name, str(tid), *flags)
    newest = run_store.newest_whole_run(repo_name, str(tid))
    if newest and newest["status"] == "interrupted" and repaired_by(newest["id"]):
        return script_command(cfg, "repair", repo_name, str(tid), *flags)
    name = "revise" if pending_feedback(repo_name, str(tid)) else "feature"
    return script_command(cfg, name, repo_name, str(tid), *flags)


def exec_script(repo_name: str, tid: str, dry_run: bool, force: bool) -> int:
    try:
        command = run_command(repo_name, tid, force, dry_run)
    except FileNotFoundError as e:
        print(e)
        return 1
    sys.stdout.flush()
    os.execv(command[0], command)


def script_refusal(cfg: config.Config, repo: config.Repo, tid: str, script: Script, dry_run: bool = False) -> str:
    if script.environment_release:
        from . import environment_gaps

        reruns_verify = (script.again == "verify" if script.path in (*environment_gaps.STEP_PATHS, "classic") else
                        script.path == "orchestrate" and bool(script.action)
                        and script.action == environment_gaps.verification_action(script.continues))
        if (not reruns_verify or not script.continues or dry_run or script.only or script.action_only
                or script.repairs or pending_feedback(repo.name, tid)
                or environment_gaps.live_release() != script.environment_release
                or recorded_path(script.continues) != script.path
                or (script.path in environment_gaps.STEP_PATHS and cfg.adws
                    and cfg.adws.resolve() != paths.ADWS_DIR.resolve())):
            return "the verification retry requires its selected release to be live and the PR to be awaiting verification"
    if script.action and (script.path != "orchestrate" or not script.continues or script.only or script.again):
        return "--action needs an orchestrated --continue without --only or --from"
    if script.action_only and not script.action:
        return "--only-action needs --action"
    if script.repairs:
        if script.continues or script.only or script.again or dry_run:
            return "--repair does not go with --continue, --only, --from or --dry-run"
        row = run_store.run_get(script.repairs) or {}
        if (row.get("repo"), str(row.get("issue"))) != (repo.name, str(tid)):
            return f"--repair {script.repairs}: no run of {repo.name} {tid} has that id"
    if script.again and not script.continues:
        return "--from needs --continue"
    if script.again and script.only:
        return "--from and --only do not go together"
    if script.continues:
        row = run_store.run_get(script.continues) or {}
        if (row.get("repo"), str(row.get("issue"))) != (repo.name, str(tid)):
            return f"--continue {script.continues}: no run of {repo.name} {tid} has that id"
    try:
        layer = config.load_layer(repo.layer_dir)
    except (ValueError, TypeError, OSError):
        layer = None
    for role in config.STEP_ROLES:
        inherited = script.overrides.get(config.INHERITS.get(role, ""), {})
        own = script.overrides.get(role, {})
        if own or inherited:
            try:
                steps.resolve(cfg, layer, role, "", own, inherited)
            except ValueError as e:
                return f"--set: {e}"
    return ""


def run_refusal(repo: config.Repo, script: bool = False) -> tuple[bool, str]:
    try:
        path, broken = wanted(repo), ""
    except (ValueError, TypeError, OSError) as e:
        path, broken = "classic", f"; {paths.short_path(repo.layer_dir / 'config.toml')}: {e}"
    adw = path in (ADW, ORCHESTRATED) or script
    if repo.parts or adw and has_origin(repo):
        return adw, ""
    return adw, f"{repo.name} has no github remote configured" + (
        " and no git remote origin, which a scripted factory needs" if adw else broken)


def runnable(repo: config.Repo) -> bool:
    return bool(repo.parts) or not run_refusal(repo)[1]


def parent_run(script: Script | None, earlier: dict | None, budget_history: list[dict]) -> str:
    if script and script.environment_release:
        return script.continues
    parent = (script.repairs or script.continues if script else "") or (
        earlier["id"] if earlier and earlier["status"] not in ENDED else "")
    if budget_history and budget_history[0]["status"] not in ENDED:
        parent = budget_history[0]["id"]
    return parent


def park_run(run: Run, reason: str, *, comment: bool = True) -> tuple[str, str, str]:
    run.parking = True
    keep_edits(run)
    return record_park(run, reason, kept_heads(run), comment=comment)


def record_park(run: Run, reason: str, held: dict, *, comment: bool = True) -> tuple[str, str, str]:
    status, summary = "failed", (f"parked: {reason}; branch and evidence retained, workspace left in place"
                                 f"{', edits kept in a local commit' if held else ''}. {runlimits.RETRY}")
    kept = run.budget.park(reason, held)
    record.note(run.id, **kept)
    pr_url = kept["pr_url"]
    run.log(summary)
    run.enter("failed")
    if comment:
        body = [f"Parked: {reason}."]
        if review_lines := parked_review_lines(run.dir):
            body.extend(["", *review_lines, ""])
        body.append("Change this ticket's title or body to retry; the next poll starts it again on the kept branch. "
                    f"Run `{run.id}`.")
        try:
            run.say("\n".join(body), on_ticket=True)
        except (OSError, sqlite3.Error, NoDatabase, IncompatibleSchema) as error:
            run.log(f"tracker: {util.first_line(str(error), 300)}")
    return status, summary, pr_url


def parked_review_lines(folder: Path) -> list[str]:
    rounds = []
    for path in folder.glob("review*.json"):
        if match := re.fullmatch(r"review([2-9]|[1-9][0-9]+)?\.json", path.name):
            rounds.append((int(match[1] or 1), path))

    lines = []
    for round_number, path in sorted(rounds):
        try:
            if path.is_symlink():
                return []
            problems = json.loads(path.read_text(encoding="utf-8"))["structured"]["problems"]
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            return []
        if not isinstance(problems, list):
            return []
        for problem in problems:
            if not isinstance(problem, dict):
                return []
            severity = problem.get("severity")
            if severity in ("blocker", "major"):
                file = problem.get("file", "")
                if not isinstance(file, str):
                    return []
                location = f"`{file}`" if file else "(no file)"
                lines.append(f"- Review {round_number} held on {location} ({severity})")
    return lines


def settle_run(run: Run, status: str, summary: str, pr_url: str, restarting: bool, flow: factory.Flow | None,
               *, echo_end: bool = True) -> None:
    failure = run.failure if run.failure is not None else {"class": "work"}
    if restarting and status in ("skipped", "noop") and not (run.feedback or run.closed_feedback_prs):
        run.enter("failed")
        run.say(f"I did not restart this after the interruption: {summary}. Remove "
                f"`{run.tracker.label_name('failed')}` to retry.")
    if run.feedback and not (run.dry_run or run.alone or run.claim_feedback_on_revise):
        if (status == "noop" and run.closed_feedback_prs) or (
                status == "failed" and failure.get("reason") == "policy-changed"):
            insights_store.release_feedback(run.id, closed_prs=run.closed_feedback_prs)
        elif run.step_started or (flow and flow.work_started):
            insights_store.settle_feedback(run.id, {"pr": "done", "noop": "recorded"}.get(status, "failed"))
        else:
            insights_store.release_feedback(run.id)
    if status == "failed":
        if flow and flow.matching and flow.stepped:
            flow.route()
        if not run_lines.ended_in_error(run.id, reply="respond" if flow else ""):
            run_lines.write(run.id, "error", step=run_lines.last_step(run.id), message=summary)
    pr_url = pr_url or str((run_store.run_get(run.id) or {}).get("pr_url") or "")
    end_line = f"done: {status} {pr_url or summary}"
    if echo_end:
        run.log(end_line)
    classified = {"failure": json.dumps(failure)} if status == "failed" and failure and not run.dry_run else {}
    if status == "pr" and pr_url and not (run.environment_release or run.dry_run or run.alone or run.feedback or run.repairs):
        from . import environment_gaps

        environment_gaps.save_hold(run.id, pr_url)
    record.settle(run.id, status, end_line=end_line, summary=summary, pr_url=pr_url,
                  **classified)
    write_report(run_store.run_get(run.id) or {"id": run.id, "status": status, "pr_url": pr_url}, flow, run)


def worker_policy_changed() -> str:
    from .delegation import PolicyChanged

    return record.failure(PolicyChanged("worker policy changed after action acceptance"))


def settle_zte_failure(repo: str, tid: str, slot: slots.RunSlot, run_id: str, error: BaseException, *,
                       committed: bool, failure: dict | None = None) -> int:
    from . import launch_lease
    from .store import db

    slot.free()
    if run_id:
        try:
            if not committed:
                with db.one_read(timeout=0):
                    committed = bool(run_store.run_get(run_id))
            if committed:
                try:
                    launch_lease.registered(repo, tid)
                finally:
                    outcome = "stopped" if isinstance(error, Stopped) else "failed"
                    fields = {"status": outcome, "finished": util.iso(), "summary": str(error)}
                    if failure:
                        fields.update(failure=json.dumps(failure), summary=f"crashed: {error}"[:300])
                    with db.tx(timeout=0):
                        run_store.settle(run_id, fields)
        except (OSError, RuntimeError, sqlite3.Error):
            pass  # The scheduled reaper settles abandoned registered runs.
    return 2


def crash_failure(error: Exception) -> dict:
    if runner.SANDBOX_START_ERROR in str(error).lower():
        return {"class": "environment", "reason": "sandbox-start"}
    traceback = error.__traceback__
    while traceback and traceback.tb_next:
        traceback = traceback.tb_next
    external_command = (traceback and traceback.tb_frame.f_globals.get("__name__") == util.__name__
                        and traceback.tb_frame.f_code.co_name in ("git", "sh"))
    if (isinstance(error, (AttributeError, AssertionError, LookupError, NameError, TypeError, ValueError, RuntimeError))
            and not external_command):
        return {"class": "environment", "reason": "engine-crash"}
    return {}


def run_issue(repo_name: str, tid: str | int, dry_run: bool = False, force: bool = False,
              script: Script | None = None, admission: Callable | None = None,
              launch_done: Callable | None = None, closed_context: tuple[str, ...] = ()) -> int:
    try:
        producer_baseline = scratch.process_ids()
    except OSError:
        producer_baseline = None
    service_labels = scratch.service_labels()
    paths.ensure_dirs()
    cfg = config.load()
    repo = cfg.repo(repo_name)
    from . import flows, launch_lease, pilot, unblock, zte
    from .store import db, orchestration
    from .store import zte as zte_store

    adw, refusal = run_refusal(repo, script is not None)
    if refusal:
        print(refusal)
        return 1
    tk = tracker.for_ticket(cfg, repo, tid)
    try:
        tid = tk.normalize(tid)
    except ValueError as e:
        print(f"{repo_name}: {e}")
        return 1
    try:
        db.require_workspace_contract()
    except db.IncompatibleSchema as e:
        print(str(e))
        return 1
    if not script:
        recovery_child = launch_lease.recovery(repo.name, str(tid))
        adw = not recovery_child and dispatch_path(repo, str(tid)) != "classic"
    if adw and not script:
        return exec_script(repo.name, tid, dry_run, force)
    if script and (refusal := script_refusal(cfg, repo, tid, script, dry_run)):
        print(refusal)
        return 2
    name = tracker.ticket_name(repo.name, tk.ref(tid))
    lock = run_store.run_lock("pipeline", repo.name, tid)
    with workspaces.ticket_lock(repo.name, tid) as got:
        if not got:
            print(f"{name} is already running")
            return 0
        retry_of = {}
        fixed_gaps = ()
        spec_ticket = None
        spec_claim, spec_previous, spec_identity = {}, "", ""
        jira_at_capacity, jira_slots = False, None
        preview_of = run_store.held_environment_branch(repo.name, str(tid)) if dry_run else ""
        if preview_of and script and script.continues:
            print(f"{name}: run the dry run without --continue to preview the held draft in its own workspace")
            return 2
        if script and script.environment_release:
            from . import environment_gaps

            source = run_store.run_get(script.continues) or {}
            if ((source.get("repo"), str(source.get("issue"))) != (repo.name, str(tid))
                    or run_store.later_run(repo.name, tid, script.continues, live=True)
                    or not (fixed_gaps := environment_gaps.confirmed_retry_gaps(cfg, repo, source,
                                                                                script.environment_release))):
                print(f"{name}: the held draft or its release evidence changed before verification could restart")
                return 2
            try:
                flows.check_environment_workspace(repo, source, run_store.environment_hold(source["id"])["heads"])
            except workspace.RunFailed as error:
                print(f"{name}: verify retry still waiting for its workspace: {error.why}")
                return 2
            retry_of = {"environment_retry_of": source["id"], "pr_url": source["pr_url"], "branch": source["branch"]}
        only = (script.only or (script.action if script.action_only else "")) if script else ""
        reconciling_catch_up = (script and script.path == "orchestrate" and script.continues
                                and bool(flows.pending_catch_up(script.continues)))
        try:
            layer = config.load_layer(repo.layer_dir)
            wall_deadline = math.inf
            zte_grant = None
            if zte.reserved(str(tid)):
                pilot.require(script and script.path == "orchestrate" and not any(
                    (dry_run, force, script.continues, script.only, script.again, script.repairs, script.action,
                     script.overrides, closed_context)), "ZTE requires its original scheduled orchestration")
                zte_grant = zte.admit_run(cfg, repo, str(tid))
                wall_deadline = zte.wall_deadline(zte_grant)
            if pilot.is_pilot_task(str(tid)):
                from .orchestration import policy_fingerprint

                permit = pilot.task_admission(str(tid), repo.name)
                pilot.require(principals.current() == "rule:poll" and script and script.path == "orchestrate"
                              and not any((dry_run, force, script.continues, script.only, script.again, script.repairs,
                                           script.action, script.overrides, closed_context))
                              and permit["policy"] == policy_fingerprint(cfg, layer, ())
                              and layer.factory_path == ORCHESTRATED,
                              "the one-shot task requires the approved first scheduled orchestration")
                wall_deadline = permit["deadline"]
            issue_state = None
            if isinstance(tk, tracker.GitHubTracker):
                spec_identity = tracker.github_issue_identity(repo.github, tid)
                issue = gh.issue(repo.github, int(tid)) or {}
                labels = issue.get("labels")
                if any(label["name"] == tracker.ZTE_LABEL for label in labels or ()):
                    raise tracker.TaskError(tracker.ZTE_QUEUE)
                if labels is not None:
                    issue_state = issue.get("state")
                if any(label["name"] == tracker.SPEC_LABEL for label in labels or ()):
                    spec_ticket = tk.get(tid, issue=issue)
            elif isinstance(tk, tracker.JiraTracker):
                if not dry_run and (jira_slots := slots.slots_free_to_start(cfg.max_parallel)) <= 0:
                    known = spec_store.find(repo.name, tid)
                    previous = orchestration.find(known["root"]).get("current_run", "") if known else ""
                    due_from = previous or (script.continues if script else "")
                    due = asks_store.asks_due([due_from]) if due_from and not only else []
                    jira_at_capacity = not due or zte_store.activation_pending()
                if not jira_at_capacity:
                    client = tk.client
                    issue = client.issue(tid, ["labels"])
                    spec_identity = tracker.jira_issue_identity(getattr(client, "base", ""), issue.get("id"))
                    if tracker.SPEC_LABEL in ((issue.get("fields") or {}).get("labels") or []):
                        spec_ticket = tk.get(tid)
            if not jira_at_capacity and not isinstance(tk, tracker.LocalTracker):
                spec_claim = spec_store.find(repo.name, tid, identity=spec_identity)
            if spec_ticket or spec_claim:
                if not spec_ticket:
                    raise tracker.TaskError("the spec's publication label was removed")
                if why := tracker.spec_blocker(cfg, tk, spec_ticket):
                    raise tracker.TaskError(why)
                if spec_ticket.spec_identity != spec_identity:
                    raise tracker.TaskError("the spec tracker identity changed during admission")
                if not script or script.path != "orchestrate" or any(
                        (dry_run, only, script.again, script.repairs, script.environment_release, closed_context)):
                    raise tracker.TaskError("a spec must run through its whole-ticket orchestrator")
                if spec_claim:
                    if (spec_claim["repo"], spec_claim["issue"], spec_claim["identity"]) != (
                            repo.name, tid, spec_ticket.spec_identity):
                        raise tracker.TaskError("this spec's delivery lineage is already bound to another checkout or key")
                    spec_previous = orchestration.find(spec_claim["root"]).get("current_run", "")
                    if spec_claim["state"] in ("evidenced", "complete"):
                        raise tracker.TaskError("this spec already has delivery evidence")
                    if script.continues and script.continues not in (spec_claim["run"], spec_previous):
                        raise tracker.TaskError("continue must name this spec's current delivery lineage")
                elif script.continues or run_store.newest_live_whole_run(repo.name, tid):
                    raise tracker.TaskError("a spec cannot adopt the ordinary builder's history")
            budget_history = run_store.ticket_runs(repo.name, tid, limit=1, live=bool(retry_of or preview_of))
            latest = budget_history[0] if budget_history else None
            custom_feature = bool(script and script.path == "feature" and custom_revision_feature(cfg))
            may_revise = not (zte_grant or retry_of or reconciling_catch_up or only) and (
                script is None or not script.repairs and (script.path in ("orchestrate", "revise") or custom_feature))
            feedback = pending_feedback(repo.name, tid) if may_revise else []
            if spec_claim.get("state") == "published" and not insights_store.has_new_feedback(feedback):
                raise tracker.TaskError("this spec is already awaiting delivery evidence")
            if spec_claim and (previous_run := run_store.run_get(spec_claim["run"])):
                if previous_run["status"] == "needs-info" and asks_store.asks_of(previous_run["id"], "open"):
                    raise tracker.TaskError("the spec is still waiting for an owner answer")
            feedback_readmits = (not dry_run and script and script.path == "orchestrate"
                                 and insights_store.has_fresh_feedback(feedback))
            recovery, recovery_case = ({}, {}) if spec_ticket else unblock.child_grant(
                cfg, repo, tk, str(tid), force=force, script=script, dry_run=dry_run)
            readmission = None if force else runlimits.Readmission.decode(launch_lease.readmission(repo.name, str(tid)))
            if recovery or (feedback_readmits and not (force or readmission)):
                budget_admission = runlimits.Admission()
            else:
                budget_admission = runlimits.admission(repo, str(tid), tk, runlimits.Policy.of(layer), force,
                                                       readmission=readmission)
            poll_failure = (latest["id"] if not (dry_run or force or retry_of or readmission) and latest
                            and latest["status"] == "failed" and not latest["dry_run"]
                            and launch_lease.from_poll(repo.name, str(tid)) else "")
        except tracker.ERRORS as e:
            print(f"could not read {name}: {e}")
            return 2
        except pilot.Held as e:
            print(f"could not admit the reserved README task: {e}")
            return 2
        except (ValueError, OSError) as e:
            print(f"could not validate the process budget: {e}")
            return 2
        if budget_admission.reason and not dry_run and not (force and feedback_readmits):
            print(budget_admission.reason)
            return 2
        if script and script.repairs and (later := run_store.later_run(repo.name, tid, script.repairs)):
            print(f"run {later['id']} of {name} came after run {script.repairs}, so it is not repaired twice")
            return 1
        if zte_grant:
            try:
                zte.require_launch_base(zte_grant)
            except (OSError, RuntimeError, ValueError) as error:
                print(f"could not start the reserved README task: {error}")
                return 2
        if jira_at_capacity:
            if recovery and jira_slots == 0:
                print(f"{name}: recovery is waiting for a run slot")
                return SLOT_QUEUE_FAILED_EXIT
            print(f"{name} was not started: {slots.no_free_slot(cfg.max_parallel)}. "
                  f"Wait for a run to finish or raise max_parallel in {paths.short_path(paths.CONFIG_FILE)}.")
            return 1
        with ExitStack() as slot_admission:
            if not slot_admission.enter_context(slots.admitting(wait=zte_grant is None)):
                print("could not start the reserved README task: slot admission is busy")
                return 2
            slot = slots.RunSlot(repo.name, str(tid), cfg.max_parallel)
            due_from = spec_previous or (script.continues if script else "")
            due = asks_store.asks_due([due_from]) if due_from and not only else []
            if recovery and slots.slots_free_to_start(cfg.max_parallel) == 0:
                print(f"{name}: recovery is waiting for a run slot")
                return SLOT_QUEUE_FAILED_EXIT
            if not dry_run:
                if slots.slots_free_to_start(cfg.max_parallel) > 0:
                    slot.hold()
                elif not due or zte_store.activation_pending():
                    slot_admission.close()
                    can_queue = not zte_grant and issue_state in ("OPEN", "CLOSED", "MERGED")
                    if can_queue and issue_state != "OPEN":
                        prepare_reads_feedback = (script and script.path in ("feature", "patch")
                                                  and not (script.repairs or only))
                        can_queue = not (feedback or (prepare_reads_feedback and pending_feedback(repo.name, tid)))
                    refusal = f"{name} was not started: {slots.no_free_slot(cfg.max_parallel)}."
                    if can_queue:
                        try:
                            with db.tx(timeout=0.5):
                                source_store.pass_over(repo.name, str(tid))
                        except sqlite3.OperationalError:
                            print(f"{refusal} Ticket could not be queued for the next poll because the database "
                                  "was busy. Start the ticket again.")
                            return SLOT_QUEUE_FAILED_EXIT
                        else:
                            print(f"{refusal} Ticket queued for the next poll.")
                    else:
                        print(f"{refusal} Wait for a run to finish or raise max_parallel in "
                              f"{paths.short_path(paths.CONFIG_FILE)}.")
                    return 1
            settled = [done for dead_row in run_store.ticket_runs_marked_running(repo.name, tid)
                       if (done := record.interrupt(dead_row))]
            if settled and may_revise:
                feedback = pending_feedback(repo.name, tid)
            earlier = None if only or preview_of else run_store.newest_live_whole_run(repo.name, tid)
            restarting = bool(earlier) and earlier["status"] == "interrupted"
            mending = (script.repairs or repaired_by(script.continues or (earlier["id"] if restarting else ""))
                       ) if script else ""
            revising_over_repair = (mending and custom_feature and not (only or script.continues)
                                    and insights_store.has_new_feedback(feedback))
            if revising_over_repair:
                mending = ""
            if pilot.is_pilot_task(str(tid)):
                try:
                    pilot.require(pilot.task_admission(str(tid), repo.name) == permit,
                                  "the authorization changed before the run was started")
                except pilot.Held as e:
                    print(f"could not admit the one-shot task: {e}")
                    slot.free()
                    return 2
            def start():
                return record.start("pipeline", tag=f"{repo.name}-{tid}", lock=lock, repo=repo.name, issue=tid,
                                    only_step=only, dry_run=int(dry_run), **retry_of)

            def check_start_cutoff():
                pilot.require(time.monotonic() < wall_deadline, "ZTE deadline expired during run startup")

            def check_handoff():
                check_start_cutoff()
                pilot.require(not zte.PAUSE_FILE.exists(), "ZTE was stopped before the flow started")
                check_start_cutoff()

            @contextmanager
            def handoff_io():
                with ExitStack() as stack:
                    if zte_grant:
                        stack.enter_context(db.nonblocking_transactions(wall_deadline, check_handoff))
                        stack.enter_context(run_lines.nonblocking_appends(check_handoff))
                    yield

            if zte_grant:
                run_id = ""
                committed = False
                try:
                    with util.lock(zte.INTERLOCK) as got_interlock:
                        pilot.require(got_interlock, "ZTE run registration is interlocked")
                        check_start_cutoff()
                        with db.one_read(timeout=0):
                            fresh_grant = zte.admit_run(config.load(), repo, str(tid))
                        pilot.require(fresh_grant == zte_grant, "ZTE authorization changed before run start")
                        readme_rule = zte.active(fresh_grant).signal(fresh_grant.base)
                        check_start_cutoff()
                        with db.tx(timeout=0, before_commit=check_start_cutoff):
                            check_start_cutoff()
                            pilot.require(zte.task_version(fresh_grant, readme_rule, "todo") == fresh_grant.data["task_version"],
                                          "the reserved task was edited before ZTE launch")
                            check_start_cutoff()
                            run_id = start()
                            zte_store.transition(fresh_grant, "running", run=run_id)
                        committed = True
                        launch_lease.registered(repo.name, str(tid))
                        check_start_cutoff()
                except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                    return settle_zte_failure(repo.name, str(tid), slot, run_id, error, committed=committed)
            elif recovery:
                try:
                    run_id = unblock.register(repo, recovery, recovery_case, start)
                except ValueError as error:
                    slot.free()
                    print(f"{name}: {error}")
                    return 2
            elif spec_ticket:
                try:
                    run_id = spec_store.bind(spec_ticket.spec_identity, repo.name, tid, spec_claim.get("run", ""), start)
                    launch_lease.registered(repo.name, str(tid))
                except (orchestration.Conflict, db.IncompatibleSchema) as error:
                    slot.free()
                    print(f"{name}: {error}")
                    return 2
            else:
                run_id = start()
            scratch.RUN_BASELINES[run_id] = producer_baseline
            scratch.RUN_LABELS[run_id] = service_labels
            repair_with_feedback = script and script.path == "orchestrate" and script.continues and not script.action
            run = Run(run_id, repo, paths.RUNS_DIR / run_id, dry_run, tracker=tk, tid=tid, force=force,
                      feedback=[] if mending and not repair_with_feedback else feedback,
                      claim_feedback_on_revise=custom_feature or bool(script and script.path == "revise"),
                      repairs=mending,
                      answering=earlier if earlier and earlier["status"] == "needs-info" else {},
                      launch_gate=admission, launch_done=launch_done, closed_context=closed_context, preview_of=preview_of,
                      recovery=recovery.get("grant", {}),
                      spec_root=(spec_claim.get("root") or run_id) if spec_ticket else "",
                      spec_identity=spec_ticket.spec_identity if spec_ticket else "",
                **(script.run_fields if script else {}))
            if run.recovery:
                run_lines.write(run.id, "recovery", grant=recovery["id"], source=recovery_case["source"],
                                allowance=run.recovery.get("allowance", {}), action=recovery["decision"]["action"],
                                direction=run.recovery.get("direction", ""))
            if not script and run.answering and not dry_run:
                run.answers_due = asks_store.asks_due([run.answering["id"]])
            try:
                with handoff_io():
                    run.dir.mkdir(parents=True, exist_ok=True)
                    run_lines.write(run.id, "execution", path=run.path or "classic",
                                    **({} if run.path == "orchestrate" else {"protocol": 0}),
                                    **({"environment_release": run.environment_release} if run.environment_release else {}))
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                if not zte_grant:
                    slot.free()
                    raise
                return settle_zte_failure(repo.name, str(tid), slot, run_id, error, committed=True)
            if held_from := earlier["id"] if restarting or run.answering else (
                    script.continues or script.repairs if script else ""):
                run.ab_held = run_store.ab_held(held_from)
                for role, (ab, side, arm) in run.ab_held.items():
                    record.hold_ab(run_id, role, ab, side, arm)
            if mending:
                run_lines.write(run_id, "repair", source=mending)
                run.log(f"repairing the review findings of run {mending}")
        for done in settled:
            if only and not dry_run:
                tell_ticket_run_died(cfg, done)
                continue
            if done["status"] != "stopped":
                run_lines.write(done["id"], "error", step=run_lines.last_step(done["id"]), message=done.get("summary") or "")
            write_report(done)
        try:
            with handoff_io():
                if zte_grant:
                    with util.lock(zte.INTERLOCK) as got:
                        pilot.require(got, "ZTE startup accounting is interlocked")
                        check_handoff()
                        with db.tx():
                            passed_over = source_store.take_passed_over(repo.name, str(tid))
                    check_handoff()
                else:
                    passed_over = "" if dry_run or run.alone else source_store.take_passed_over(repo.name, str(tid))
                opening_waits(run_id, run.answering, passed_over)
                if zte_grant:
                    check_handoff()
                if restarting:
                    run.log(f"restarting after run {earlier['id']} was interrupted")
                if zte_grant:
                    check_handoff()
                flow = (flows.start(cfg, run, (earlier or {}) if not revising_over_repair else {},
                                    slot, spec_previous if spec_ticket else script.continues,
                                    script.only, script.again) if script else None)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            if not zte_grant:
                raise
            return settle_zte_failure(repo.name, str(tid), slot, run_id, error, committed=True)
        status, pr_url, passed, drive_entered, retaining_catch_up = "failed", "", None, False, False

        def check_work_log():
            pilot.require(time.monotonic() < wall_deadline, "the ZTE authorization expired")
            pilot.require(not zte.PAUSE_FILE.exists(), "ZTE was stopped")
            pilot.require(time.monotonic() < wall_deadline, "the ZTE authorization expired")

        def checkpoint_allowed():
            return time.monotonic() < wall_deadline and not zte.PAUSE_FILE.exists()

        def keep_edits_blocker() -> str:
            if run.catch_up_preserved:
                return "catch-up left files in place"
            if retaining_catch_up:
                return "the catch-up checkout is retained for reconciliation"
            if run.environment_release and not ("requirements" in run.facts and flows.retry_passed(flow.ctx.verified)):
                return "the automatic verification retry has not passed"
            return ""

        def termination_keep_blocker(error: BaseException | None) -> str:
            if error is not _termination:
                return "the termination exception did not survive unwinding"
            if blocked := keep_edits_blocker():
                return blocked
            if run.catch_up_owns_checkout:
                return "publication catch-up still owns the checkout"
            if run.path == "orchestrate" and flows.pending_catch_up(run.id):
                return "the catch-up checkout is retained for reconciliation"
            try:
                while os.waitpid(-1, os.WNOHANG)[0]:
                    pass
            except ChildProcessError:
                pass
            else:
                return "a child process is still alive"
            for checkout in run.workspace[1]:
                if not (checkout.wt / ".git").exists():
                    continue
                git_dir = Path(git(["rev-parse", "--absolute-git-dir"], checkout.wt).rstrip("\n"))
                states = ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD", "sequencer")
                if any((git_dir / state).exists() for state in states):
                    return f"a Git operation is in progress in {checkout.wt}"
            return ""

        finalizing = False
        try:
            signal.signal(signal.SIGTERM, stop_on_term(run, zte_grant=zte_grant is not None,
                                                        finalizing=lambda: finalizing))
            if not run.claim_feedback_on_revise:
                run.claim_feedback()
            if run.feedback:
                run.log(f"revising {', '.join(run.feedback_prs)} for {util.count(len(run.feedback), 'comment')}")
            if due and not slot.held and not dry_run and not flow.refusal:
                slot.take(run.log, run.waiting, "answer")
            parent = recovery_case["source"] if recovery else parent_run(script, earlier, budget_history)
            if restarting and not (dry_run or only or run.recovery or zte_grant or spec_ticket):
                run.recovery = unblock.inherited(earlier, lambda: runlimits.specification(tk.get(tid)))
            allow_reset = not (dry_run or retry_of or run.recovery)
            previous = "" if allow_reset and budget_admission.reset_time else parent
            repair_parent = "" if allow_reset and budget_admission.reset_repairs else parent
            can_reset_specs = bool(previous) and allow_reset and not restarting and not run.repairs and not run.alone
            poll_retry = can_reset_specs and previous == poll_failure and not (flow and flow.refusal)
            reset_changed_specs = can_reset_specs and force and not (flow and flow.continues)
            current = tk.get(tid) if reset_changed_specs else None
            answered_runs = ()
            if not dry_run and not only and not (flow and flow.refusal):
                answered_runs = tuple(a["run"] for a in run.answers_due if not owner_asks.supervisor_answered([a]))
                if run.answering and not run.recovery and answer_text(run.answering["id"]).strip():
                    answered_runs += (run.answering["id"],)
            issue_goal = (not dry_run and not run.alone and not run.closed_context and not spec_ticket
                          and unblock.active(cfg, repo.name, str(tid)) and not unblock.exclude(repo.name, str(tid)))
            if issue_goal:
                from . import issue_goal as goal_store

                enrolled = unblock.ledger.follow(repo.name, str(tid), run.id)
                if enrolled and not cfg.replay.all:
                    goal_store.lifecycle(repo.name, tid, "start")
            new_budget = partial(runlimits.Budget, run, runlimits.Policy.of(layer), previous,
                                 repair_previous=repair_parent, answered_runs=answered_runs, issue_goal=issue_goal,
                                 wall_deadline=wall_deadline, checkpoint_timeout=0 if zte_grant else None,
                                 checkpoint_gate=checkpoint_allowed if zte_grant else None, fixed_gaps=fixed_gaps)
            budget_started = None
            if poll_retry:
                with handoff_io():
                    run.budget = new_budget(pending_specification=True)
                try:
                    with run.budget.watching(admit=check_handoff if zte_grant else None, periodic=not zte_grant):
                        current = tk.get(tid)
                except (Exception, runlimits.Parked, Stopped):
                    with handoff_io():
                        run.budget.log_chain()
                    raise
                budget_started = run.budget.started
                reset_changed_specs = runlimits.specification_changed(
                    runlimits.metadata(poll_failure, "budget.json"), runlimits.specification(current))
            with handoff_io():
                run.budget = new_budget(current, reset_changed_specs, started=budget_started)
            with run.budget.watching(admit=check_handoff if zte_grant else None,
                                     work_admitted=(lambda: drive_entered and checkpoint_allowed()) if zte_grant else None,
                                     periodic=not zte_grant):
                with handoff_io():
                    if not (flow and flow.refusal):
                        run.budget.capture(current or tk.get(tid))
                with run_lines.nonblocking_appends(check_work_log) if zte_grant else nullcontext():
                    if not (only or zte_grant or (flow and flow.refusal)):
                        run.retry_gate_evidence()
                    drive_entered = True
                    status, summary, pr_url = flows.driven(flow, script.drive) if script else _run(cfg, run, slot)
            if zte_grant:
                check_work_log()
            if flow and flow.only:
                status, summary, passed = flows.alone(flow)
            elif run.action_only:
                passed = status == "step"
        except runlimits.Parked as e:
            if _termination is not None:
                raise
            if zte_grant:
                return settle_zte_failure(repo.name, str(tid), slot, run_id, e, committed=True)
            status, summary, pr_url = run.catch_up_cleanup_park or park_run(run, str(e))
        except factory.OnlyDone as e:
            if _termination is not None:
                raise
            if zte_grant:
                return settle_zte_failure(repo.name, str(tid), slot, run_id, e, committed=True)
            status, summary, passed = flows.alone(flow)
        except factory.Refused as e:
            if _termination is not None:
                raise
            retaining_catch_up = reconciling_catch_up
            if zte_grant:
                return settle_zte_failure(repo.name, str(tid), slot, run_id, e, committed=True)
            if run.environment_release:
                status, summary, pr_url = environment_retry_held(run, script.continues, str(e))
            else:
                status, summary = "failed", str(e)
        except Terminated:
            raise
        except Stopped as e:
            if _termination is not None:
                raise
            if zte_grant:
                return settle_zte_failure(repo.name, str(tid), slot, run_id, e, committed=True)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            status, summary, pr_url = "stopped", record.STOPPED, ""
            run.log(record.STOPPED)
            run.give_up("was stopped by request")
        except CatchUpPushUnconfirmed as e:
            if _termination is not None:
                raise
            retaining_catch_up = True
            status = "interrupted" if e.retryable and run.path == "orchestrate" else "failed"
            summary = f"{e.why}. {e.detail}"
            if status == "interrupted":
                summary += " The next poll can reconcile the recorded push."
            run.log(summary)
        except workspace.RunFailed as e:
            if _termination is not None:
                raise
            if zte_grant:
                return settle_zte_failure(repo.name, str(tid), slot, run_id, e, committed=True)
            if run.environment_release:
                status, summary, pr_url = environment_retry_held(run, script.continues, e.why)
            else:
                status, summary, pr_url = _fail(run, e.why, detail=e.detail, recovery=e.recovery)
        except tracker.ERRORS as e:
            if _termination is not None:
                raise
            if zte_grant:
                return settle_zte_failure(repo.name, str(tid), slot, run_id, e, committed=True)
            why = f"tracker refused {run.ref}: {e}"
            if run.environment_release:
                status, summary, pr_url = environment_retry_held(run, script.continues, why)
            else:
                status, summary, pr_url = _fail(run, why)
        except Exception as e:
            if _termination is not None:
                raise
            if zte_grant:
                failure = crash_failure(e) if isinstance(e, prompt_loader.MissingInputs) else None
                return settle_zte_failure(repo.name, str(tid), slot, run_id, e, committed=True, failure=failure)
            run.log(f"crashed: {type(e).__name__}: {e}")
            run.failure = crash_failure(e)
            status, summary, pr_url = "failed", f"crashed: {e}"[:300], ""
            if run.environment_release:
                status, summary, pr_url = environment_retry_held(run, script.continues, summary)
            else:
                run.give_up("crashed before finishing")
        finally:
            finalizing = True
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            if _termination is not None:
                try:
                    if blocked := termination_keep_blocker(sys.exception()):
                        run.log(f"SIGTERM: did not keep work because {blocked}")
                    else:
                        keep_edits(run)
                        workspace.note_made(*run.workspace, workspace_owner(run, *run.workspace),
                                            preserve=run.catch_up_preserved, excluded=run.closed_checkout_paths,
                                            during_termination=True)
                except Exception as error:
                    run.log(f"SIGTERM: could not keep work: {error}")
                finally:
                    os.kill(os.getpid(), signal.SIGTERM)
            try:
                if run.workspace and not zte_grant:
                    if not keep_edits_blocker():
                        keep_edits(run)
                    workspace.note_made(*run.workspace, workspace_owner(run, *run.workspace),
                                        preserve=run.catch_up_preserved, excluded=run.closed_checkout_paths)
                    workspaces.keep(run.workspace[0], status)
            except (OSError, sqlite3.Error) as e:
                run.log(f"could not finalize workspace {run.workspace[0]}: {util.first_line(str(e), 300)}")
            finally:
                slot.free()
        if run.catch_up_cleanup_park and (held := kept_heads(run)):
            try:
                if parked := runlimits.metadata(run.id, "parked.json"):
                    util.write_json(runlimits.artifact(run.id, "parked.json"), {**parked, "kept": held})
            except (OSError, ValueError) as e:
                run.log(f"could not record retained heads for the catch-up park: {e}")
        try:
            with handoff_io():
                settle_run(run, status, summary, pr_url, restarting, flow, echo_end=not zte_grant)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as e:
            if not zte_grant:
                raise
            return settle_zte_failure(repo.name, str(tid), slot, run_id, e, committed=True)
        if status == "skipped" and not zte_grant:
            print(f"{name} was not run: {summary}. Pass --force to run it anyway.")
        return 0 if (status in SUCCEEDED if passed is None else passed) else 1


def environment_retry_held(run: Run, continues: str, why: str) -> tuple[str, str, str]:
    source = run_store.run_get(continues) or {}
    own = run_store.run_get(run.id) or {}
    pr_url = own.get("pr_url") or source.get("pr_url") or ""
    record.note(run.id, branch=own.get("branch") or source.get("branch") or "", pr_url=pr_url)
    run.facts["held"] = why
    run.log(f"verification retry held: {why}")
    run.enter("pr")
    return "pr", why, pr_url


def _fail(run: Run, why: str, builder_said: str = "", detail: str = "",
          *, recovery: dict | None = None) -> tuple[str, str, str]:
    run.failure = {"class": "environment", **recovery} if recovery else {"class": "work"}
    run.log(f"failed: {why}")
    run.enter(run.failed_state)
    extra = f"\n\n{detail.strip()}" if detail.strip() else ""
    if builder_said.strip():
        extra += f"\n\n### What the builder said\n\n{builder_said.strip()[:4000]}"
    task = ("apply your feedback" if run.feedback else "repair the review findings" if run.repairs
            else "finish this one")
    run.say(f"Could not {task}: {why}{extra}\n\nRun `{run.id}`; details in `arete status` on the "
            f"Mac. {run.retry_hint}.")
    return "failed", why, ""


def write_report(row: dict, flow: factory.Flow | None = None, run: Run | None = None) -> None:
    found = run.facts if run else seam_facts(row["id"])
    fields = report_fields(row, found)
    record.note(row["id"], wait_minutes=json.dumps(fields["wait_minutes"]))
    if flow:
        flow.respond(**fields)
    else:
        run_lines.write(row["id"], "report", **fields)


def report_fields(row: dict, facts: dict) -> dict:
    read = run_lines.read(row["id"])
    asked = next((line for line in reversed(read) if line.get("event") == "question"), {})
    return {"task": str(row.get("issue") or ""), "status": row.get("status") or "",
            "prs": (row.get("pr_url") or "").split(), "summary": row.get("summary") or "",
            "requirements": facts.get("requirements") or [],
            "assumptions": [*(facts.get("assumptions") or []), *owner_asks.assumed(factory.chain(row["id"]))],
            "left_for_a_person": facts.get("left_for_a_person") or "", "held": facts.get("held") or "",
            "merge_held": facts.get("merge_held") or "",
            "review": facts.get("review") or Review.stored({}).report(), "question": str(asked.get("summary") or ""),
            "steps": factory.tally(row["id"]), "minutes": run_minutes(row),
            "cost_usd": round(float(row.get("cost_usd") or 0), 4), "wait_minutes": run_lines.wait_minutes(row["id"])}


def round_name(name: str, n: int) -> str:
    return name if n == 1 else f"{name}{n}"


def repaired_by(run_id: str) -> str:
    return str(factory.first(run_lines.read(run_id), "repair").get("source") or "") if run_id else ""


def note_review(run: Run, review: Review, verified: verify.Verified) -> None:
    run.facts.update(requirements=verified.requirements(), review=review.report())


def assumptions_of(plan: dict) -> list[str]:
    listed = plan.get("assumptions")
    return [a.strip() for a in listed if isinstance(a, str) and a.strip()] if isinstance(listed, list) else []


def question_time(run: dict) -> str:
    return next((line["ts"] for line in reversed(run_lines.read(run["id"]))
                 if line.get("event") == "question" and line.get("ts")), None) or run.get("finished") or ""


def opening_waits(run_id: str, answering: dict, passed_over: str) -> None:
    now = util.iso()
    if answering:
        questions = run_lines.read(answering["id"])
        asked = question_time(answering)
        if asked:
            until = passed_over or now
            if any(line.get("event") == "question" and line.get("recovery") for line in questions):
                from .store import unblock as unblock_store

                owner_at = unblock_store.notified_at(answering["id"])
                handled = min(owner_at, until) if owner_at else until
                run_lines.waited(run_id, "unblock", asked, handled)
                if owner_at and owner_at < until:
                    run_lines.waited(run_id, "person", owner_at, until)
            else:
                run_lines.waited(run_id, "person", asked, until)
    if passed_over:
        run_lines.waited(run_id, "slot", passed_over, now)


def run_minutes(row: dict) -> float:
    if not (row.get("started") and row.get("finished")):
        return 0
    return round((util.parse_iso(row["finished"]) - util.parse_iso(row["started"])).total_seconds() / 60, 1)


def _ask(run: Run, ws: Path, checkouts: list[workspace.Checkout], comment: str, summary: str,
         asked: list[dict] | None = None, session: dict | None = None) -> tuple[str, str, str]:
    from . import unblock

    shared_runner = bool(asked) and all(a.get("call") == owner_asks.CHECK_RUNNER_CALL for a in asked)
    recover = (not shared_runner and not run.dry_run and not run.alone and not run.spec_root
               and unblock.active(run.config, run.repo.name, str(run.tid))
               and not unblock.exclude(run.repo.name, str(run.tid)))
    if recover and not asked:
        question = owner_asks.text_ask(summary)
        asked = [{**question, "step": "plan", "call": "plan", "ask_id": f"{run.id}-a1"}]
        session = {}
    for ask in asked or [{}]:
        run_lines.write(run.id, "question", question=comment, summary=summary[:300], recovery=recover, **ask)
    if asked and session is not None and not run.dry_run:
        notify_owner = not shared_runner
        for ask in asked:
            first = asks_store.ask_add(ask["ask_id"], run.id, run.repo.name, run.tid, ask["step"], ask["call"],
                          ask.get("why", ""), ask.get("tried", ""), ask["questions"], session,
                          recovery_route="unblock" if recover else "owner")
            notify_owner = notify_owner or first
        if not recover and notify_owner:
            run.say(outbound.screen(comment))
        run.enter("needs-info")
        devices.release(run.id, run.log)
        if not recover and notify_owner:
            alerts.ask_notice(run.where, summary)
            for note in webpush.ask_opened(run.config, [a["ask_id"] for a in asked]):
                run.log(note)
        run.log("needs-info: kept the work folder and the step's session for the answer")
        return "needs-info", summary[:300], ""
    run.say(comment)
    run.enter("needs-info")
    if not any(c.onto for c in checkouts):
        release_workspace(run, ws, checkouts)
    return "needs-info", summary[:300], ""


def checked_layer(cfg: config.Config, layer_dir: Path, *, source_file: Path | None = None) -> config.RepoLayer:
    try:
        layer = config.load_layer(layer_dir, source_file=source_file)
        steps.check(cfg, layer)
    except (ValueError, TypeError) as e:
        raise workspace.RunFailed(f"{paths.short_path(layer_dir / 'config.toml')}: {e}") from e
    return layer


def resolve_layer(cfg: config.Config, run: Run, ws: Path) -> config.RepoLayer:
    repo = run.repo
    if repo.layer:
        return checked_layer(cfg, repo.layer)
    if (ws / ".agents" / "config.toml").exists():
        return checked_layer(cfg, ws / ".agents")
    run.log(f"layer: {paths.short_path(repo.path / '.agents')} is not committed; lessons stay in the main checkout")
    return checked_layer(cfg, repo.path / ".agents")


def triage_checks(approval: dict | None) -> str:
    checks = (approval or {}).get("checks")
    if not isinstance(checks, list):
        return ""
    return "\n".join(f"{n}. {check}" for n, check in enumerate(checks, 1))


def triage_goal(goal: str, approval: dict | None) -> str:
    if not (checks := triage_checks(approval)):
        return goal
    return (f"{goal}\n\n## Triage checks\n\n{checks}\n\n"
            "Report an explicit outcome and evidence for every triage check, including any omitted from the planner's "
            "steps. Do not drop or weaken a check. An owner's comment starting "
            f"\"{prompt_loader.TRIAGE_ANSWERS}\" overrides contradictory checks and assumptions. Keep the original "
            "check identifiable, mark it superseded, and explain the replacement behavior and its evidence. "
            "A check disputed by the plan still needs an outcome. Follow the existing blocked-result rules when a "
            "check cannot be performed; never invent passing evidence.")


def triage_notes(approval: dict | None) -> str:
    if not approval:
        return ""
    assumed = "".join(f"\n- {a}" for a in approval["assumptions"]) or "\n- nothing it named"
    assumption_rule = ("Keep an assumption only where the ticket still leaves it open. End `plan` with an \"Assumed\" list "
                       "of every choice the ticket leaves open, these and your own, so the owner can check them in the "
                       "pull request.")
    if checks := triage_checks(approval):
        assumption_rule += (f"\n\n## Triage checks\n\n{checks}\n\n"
                            "Your `verify` must cover every triage check. You may add steps, but may not drop or weaken "
                            "a check. If the code shows a check is wrong, explain why in the plan's assumptions. "
                            f"An owner's comment starting \"{prompt_loader.TRIAGE_ANSWERS}\" overrides contradictory "
                            "checks and assumptions.")
    if principals.is_person(approval["sent_by"]):
        return (f"## Sent by its owner after triage\n\nIts owner sent this ticket to the pipeline from arete's triage "
                f"board at {approval['ts']}, after reading triage's questions and assumptions. Any answers they gave are "
                f"in their own comment that starts \"{prompt_loader.TRIAGE_ANSWERS}\", and an answer wins over what triage assumed. "
                f"Triage assumed:{assumed}\n\n{assumption_rule}")
    return (f"## Approved by triage, not by a person\n\nArete's triage approved this ticket on its own at "
            f"{approval['ts']}, so its owner has not read it closely. Triage assumed:{assumed}\n\n{assumption_rule}")


def triage_block(approval: dict | None) -> str:
    if not approval:
        return ""
    assumed = "".join(f"\n- {a}" for a in approval["assumptions"])
    checks = triage_checks(approval)
    checks_block = f"### Triage checks\n\n{checks}\n\n" if checks else ""
    if principals.is_person(approval["sent_by"]):
        if not assumed and not checks:
            return ""
        return ("## Sent from the triage board\n\nIts owner sent this ticket from arete's triage board."
                + (f" Triage assumed:{assumed}" if assumed else "") + "\n\n" + checks_block)
    return ("## Approved by triage\n\nArete's triage approved this ticket on its own, so check the choices it made"
            + (f". It assumed:{assumed}" if assumed else "; the plan below lists them.") + "\n\n" + checks_block)


def _run(cfg: config.Config, run: Run, slot: slots.RunSlot) -> tuple[str, str, str]:
    from . import environment_gaps, flows

    repo, tk = run.repo, run.tracker
    try:
        t = intake.route_ticket(cfg, repo, tk, tk.get(run.tid), dry_run=run.dry_run, log=run.log)
    except tracker.ERRORS as e:
        return "failed", f"could not read {run.ref}: {e}", ""
    if t.is_spec:
        return "skipped", "a spec must run through its whole-ticket orchestrator", ""
    run_lines.write(run.id, "task", ticket=t.ref, kind=t.kind, repo=repo.name, title=t.title)
    if run.answering:
        run_lines.write(run.id, "answer", asked_in=run.answering["id"], question=run.answering.get("summary") or "",
                    text=answer_text(run.answering["id"]))
    approval = triage_store.triage_approval(run.triage_key)
    if run.feedback:
        if closed := run.feedback_closure():
            return "noop", closed, ""
    elif not t.open:
        return "noop", "ticket is not open", ""
    elif t.blocker and not run.force:
        return "skipped", t.blocker, ""
    try:
        images = tk.save_images(t, run.dir / "images")
    except tracker.ERRORS as e:
        return "failed", f"could not download the images on {run.ref}: {e}", ""
    if images.paths:
        run.add_dirs.append(run.dir / "images")
        run.log(f"images: saved {util.count(len(images.paths), 'image')} from {run.ref}")
    if images.left_out:
        run.log(f"images: left out {', '.join(images.left_out)}")
    attachments = images_note(images)

    main_layer = checked_layer(cfg, repo.layer_dir)
    branch = run.preview_branch or open_branch(run) or prompt_loader.fill_template(
        main_layer.branch, config.RepoLayer.branch, kind=t.kind, id=t.id, slug=prompt_loader.slug(t.title))
    if not run.dry_run:
        tk.prepare()
    run.enter("running")

    try:
        ws, checkouts = workspace.make_workspace(run.id, repo, main_layer.base_branch, branch, run.workspace_key,
                                                 main_layer.copy, preview_of=run.preview_of)
        claim_workspace(run, ws, checkouts)
        for c in checkouts:
            run.log(f"worktree {c.wt} on {branch}, {c.starting_point}")
        run.cfg, run.layer, run.kind = cfg, main_layer, t.kind
        settle_starts(run, checkouts, main_layer, t, branch)
        for c in checkouts:
            c.revision = git(["rev-parse", "HEAD"], c.wt).strip()
    except workspace.RunFailed as e:
        return _fail(run, e.why, detail=e.detail, recovery=e.recovery)
    layer = resolve_layer(cfg, run, ws)
    run.cfg, run.layer, run.kind = cfg, layer, t.kind
    if why := furnish(cfg, run, layer, ws):
        return _fail(run, why)
    workspace.note_made(ws, checkouts, workspace_owner(run, ws, checkouts))

    expertise = layer.expertise_file.read_text() if layer.expertise_file.exists() else "(none yet)"
    checks = check_list(layer)
    check_commands, check_rules = check_script(run.dir, layer, ws)
    runs_checks = check_calls(check_commands)
    reads = git_reads(checkouts)
    builder_bash = layer.builder_bash + reads + check_rules
    commands = prompt_loader.code_list(layer.builder_bash + reads + check_commands)
    workspace_text = workspace_note(branch, checkouts)
    read_rules = prompt_loader.bash_rules(config.DEFAULT_ALLOW_BASH + reads)
    build_rules = prompt_loader.bash_rules(builder_bash)
    deny = diffguard.denied_paths(layer)
    limit = layer.max_diff_lines
    info = tk.label_name("needs-info")
    replies: list[dict] = []

    brief = prompt_loader.Brief(repo.name, workspace_text, t.ref, t.title, t.body,
                                comments=t.comments, attachments=attachments, expertise=expertise,
                                commands=commands, max_lines=limit, denied=prompt_loader.code_list(deny),
                                class_check=class_check_example(run.dir, layer))

    if run.feedback:
        feedback = feedback_markdown(run.feedback)
        plan_text = f"Revise the open PR for the owner's feedback:\n\n{feedback}"
        left_for_a_person = left_on_prs(run.feedback_prs)
        goal = ("Check each change the owner's feedback asks for, and that what the issue asks for still works.\n\n"
                + feedback)
        estimate = 0
        run.story.append(f"The owner reviewed the PR and left this feedback:\n\n{feedback}")
        res = run.agent("revise", brief.prompt("revise", feedback=feedback, checks=runs_checks),
                        ws, role="build", allowed_tools=build_rules, disallowed_tools=routing.NO_GIT_WRITES,
                        json_schema=REVISE_SCHEMA)
        if not res.ok or not isinstance(res.structured, dict):
            return _fail(run, "revise step failed", res.text, recovery=runner.start_failure(res, "build"))
        run.facts["left_for_a_person"] = left_for_a_person
        build_summary = res.structured.get("summary", "")
        replies = res.structured.get("replies") or []
        if stage(checkouts, deny).empty:
            return answer_without_changes(run, ws, checkouts, branch, build_summary, replies)
    else:
        res = run.agent("plan", brief.prompt(
            "plan", triage=triage_notes(approval), checks=checks,
            journeys=journeys.guidance(layer.verify.feature_map, planning=True) if layer.verify else ""),
                        ws, role="plan", allowed_tools=read_rules, json_schema=PLAN_SCHEMA)
        if not res.ok or not isinstance(res.structured, dict):
            return _fail(run, "planning step failed", recovery=runner.start_failure(res, "plan"))
        plan = res.structured
        plan_text = plan.get("plan", "")
        left_for_a_person = left_section(run, (plan.get("left_for_a_person") or "").strip())
        run.save("plan.md", plan_text)
        estimate = int(plan.get("estimated_lines") or 0)
        goal = triage_goal(plan.get("verify", "").strip() or "The planner gave no steps: check what the issue asks for.",
                           approval)
        if plan.get("ambiguous") and plan.get("question"):
            run.log("plan: ambiguous, asking the author")
            return _ask(run, ws, checkouts, f"Before I build this I need one answer:\n\n{plan['question']}\n\n"
                        f"Reply here, then remove the `{info}` label and I will pick it up.", plan["question"])
        if estimate > limit:
            split = [s for s in plan.get("split") or [] if isinstance(s, dict) and s.get("title")]
            run.log(f"plan: about {estimate} lines, over the {limit} line limit; proposing a {len(split)} part split")
            run.save("split.json", split)
            return _ask(run, ws, checkouts,
                        split_comment(estimate, limit, split, tk.queue_hint, info, layer.config_file),
                        f"plan estimates {estimate} lines, over the {limit} line limit; split proposed")
        run.facts.update(assumptions=assumptions_of(plan), left_for_a_person=left_for_a_person)

        res = run.agent("build", brief.prompt(
            "build", plan=plan_text, checks=runs_checks, estimate=estimate, verify=goal),
                        ws, role="build", allowed_tools=build_rules, disallowed_tools=routing.NO_GIT_WRITES)
        if not res.ok:
            return _fail(run, "build step failed", res.text, recovery=runner.start_failure(res, "build"))
        build_summary = res.text
        if not stage(checkouts, deny).files:
            if estimate == 0:
                return nothing_to_build(run, t, ws, checkouts, branch, build_summary, left_for_a_person)
            return _fail(run, f"the builder stopped without changing anything, though the plan estimated "
                              f"{util.count(estimate, 'line')}", build_summary)
    plan_to_review = plan_text
    verify_goal = goal
    if left_for_a_person:
        not_checked = NOT_CHECKED_LEFT_FOR_A_PERSON + left_for_a_person
        goal, plan_to_review = f"{goal}\n\n{not_checked}", f"{plan_text}\n\n{not_checked}"
    b = Build(cfg=cfg, run=run, ticket=t, ws=ws, checkouts=checkouts, layer=layer, timeout=layer.timeout_minutes * 60,
              workspace=workspace_text, attachments=attachments, tools=build_rules, deny=deny,
              left_for_a_person=left_for_a_person, check_commands=runs_checks,
              plan_text=plan_text, build_summary=build_summary)

    ok, attempts = b.check_with_repairs(lambda n: f"fix{n}")
    record.note(run.id, attempts=attempts)
    if not ok:
        run.story.append("Checks still failing after the last attempt.")
        _learn(run, ws, layer.expertise_file, t.title, apply=False)
        return _fail(run, f"checks still failing after {attempts} attempts")

    b.require_valid_diff("the build and its fixes", f" (the plan estimated {estimate})" if estimate else "")

    verified = verify_in_use(b, goal, build_summary)
    reviews, n = max(1, run.settings("review").tries), 1
    review, reply = _review(b, plan_to_review, verified), ""
    note_review(run, review, verified)
    while review.verdict == "request_changes" and n < reviews:
        if problems := review.findings():
            run.story.append(f"Review requested changes:\n{problems}")
            reply = b.resume(round_name("review_fix", n),
                             prompt_loader.prompt("review_fix", problems=problems, notes=review.fix_notes(),
                                                  denied=brief.denied),
                             role="review_repair").text
            b.recheck("the review fixes")
            verified = verify_in_use(b, goal, build_summary)
        n += 1
        review = _review(b, plan_to_review, verified, round_name("review", n), review, reply)
        note_review(run, review, verified)
    draft_why = draft_reason(review, verified, n)
    note_review(run, review, verified)
    run.facts["held"] = draft_why
    _learn(run, ws, layer.expertise_file, t.title, apply=True)
    staged = b.restage()
    size = change_size(staged)
    outcome = f"{size}, review {review.verdict}, in use {verified.verdict}"
    written = document.pr_text(run, t, summary_of(review, build_summary), review, verified, size, draft_why, cfg, layer)

    if run.dry_run:
        run.save("diff.patch", staged.patch)
        run.save("pr_body.md", gh.outgoing(report_body(
            run, summary=build_summary, review=review, verified=verified, left=left_for_a_person, images={},
            approval=approval, attempts=attempts, checks=checks, plan_text=plan_text, size=size, held=draft_why,
            written=written)))
        run.log(f"dry run: {outcome}; worktree kept at {ws}")
        return "dry-run", outcome, ""

    subject = prompt_loader.fill_template(layer.subject, config.RepoLayer.subject, kind=t.kind, id=t.id, title=t.title)
    pr_report = report_body(run, summary=build_summary, review=review, verified=verified, left=left_for_a_person,
                            images=evidence_images(run, staged.changed[0], verified), approval=approval,
                            attempts=attempts, checks=checks, plan_text=plan_text, size=size,
                            held=draft_why, written=written)
    revised = "Revised for the review feedback on the PR.\n\n" if run.feedback else ""
    link = tk.pr_link(t, whole=not left_for_a_person)
    message = f"{tracker.with_ref(subject, t.ref)}\n\n{revised}Arete-Run: {run.id}"
    workspace.require_target_branch(repo, checkouts, work_branch=branch)
    b.push(branch, message)
    opened: list[workspace.Checkout] = []
    for c in staged.changed:
        if not c.pr:
            c.pr = gh.pr_create(c.github, branch, c.base, tk.pr_title(subject, t), pr_report,
                                labels=tk.pr_labels, draft=bool(draft_why), cwd=c.wt, link=link)
            opened.append(c)
            note_prs(run, branch, [x for x in checkouts if x.pr])
            run.log(f"opened {c.pr}{' as draft' if draft_why else ''}")
        else:
            run.log(f"pushed to {c.pr}")
    pushed = [c for c in staged.changed if c not in opened]
    with_prs = [c for c in checkouts if c.pr]
    note_prs(run, branch, with_prs)
    ci_stopped = sync_pr_bodies(run, t, with_prs, opened, pushed, pushed, pr_report, left_for_a_person, link)
    drafted = hold_pushed(run, pushed, ci_stopped, draft_why)
    heads = {c.github: c.lease for c in staged.changed}
    try:
        ci_result = watch_ci(b, slot, branch, staged.changed, with_prs, draft_why)
    except workspace.RunFailed as e:
        if run.feedback:
            raise
        prs = pr_link_list(with_prs)
        status, why, _ = _fail(run, e.why, detail=f"{e.detail}\n\n### PRs\n\n{prs}\n\nReview comments on a PR "
                                                  "start a revision, as on any arete PR.")
        return status, why, record_prs(run, branch, with_prs)
    flows.enter_publication(run)
    ready = opened + ready_pushed(run, pushed, ci_stopped, draft_why)
    ci_result = automerge(b, approval, draft_why, verified, review, ready, automerge_about(t, build_summary),
                          slot=slot, branch=branch, goal=goal, plan=plan_to_review) or ci_result
    outcome += f", CI: {ci_result}"
    if run.feedback:
        answer_feedback(run, replies, "Done in the latest push.")
        run.say(f"Revised for your review.\n\n{pr_report}\n\nCI: {ci_result}.")
    else:
        run.say(f"PRs for this ticket:\n\n{pr_status_lines(with_prs, opened, pushed)}\n\nCI: {ci_result}."
                + published_note(draft_why, verified, opened, drafted))
    if (not run.feedback and draft_why and review.approved()
            and not ci_stopped and environment_gaps.gaps(asdict(verified))):
        run_lines.write(run.id, "classic_hold",
                        prepared=flows.prepared_fields(t, branch, checkouts, layer, run.add_dirs, attachments, approval),
                        plan={"plan": plan_text, "goal": verify_goal, "estimate": estimate,
                              "left_for_a_person": left_for_a_person},
                        build={"summary": build_summary}, review=review.asdict(),
                        publication={"draft_why": draft_why, "prs": {c.github: c.pr for c in with_prs},
                                     "heads": heads, "verified": asdict(verified)})
    urls = record_prs(run, branch, with_prs)
    release_workspace(run, ws, checkouts)
    return "pr", outcome, urls


def furnish(cfg: config.Config, run: Run, layer: config.RepoLayer, ws: Path) -> str:
    for rel in layer.copy:
        dest = ws / rel
        if any(dest.resolve().is_relative_to(wt.resolve()) for wt in run.closed_checkout_paths):
            continue
        if (src := run.repo.path / rel).exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    for cmd in layer.setup:
        with record.timed(run.id, "setup", command=cmd) as setup:
            res = environment.sh(cmd, cfg, run.repo, cwd=ws, timeout=1200)
            setup["exit"] = res.returncode
        run.log(f"setup `{cmd}`: exit {res.returncode}")
        if res.returncode != 0:
            run.save("setup.log", res.stdout + res.stderr)
            return f"setup command `{cmd}` failed"
    return ""


def pr_link_list(with_prs) -> str:
    return "\n".join(f"- [{c.pr_name}]({c.pr})" for c in with_prs)


def pr_status_lines(with_prs, opened, pushed) -> str:
    return "\n".join(f"- {'Opened' if c in opened else 'Pushed to' if c in pushed else 'Unchanged'} [{c.pr_name}]({c.pr})"
                     for c in with_prs)


def cut_summary(text: str) -> str:
    return text.strip()[:SUMMARY_CHARS]


def automerge_about(t: tracker.Ticket, build_summary: str) -> str:
    return f"{t.title}\n\n{cut_summary(build_summary)}"


def change_size(staged: Staged) -> str:
    return f"{len(staged.files)} files, {staged.lines} lines"


def summary_of(review: Review, build_summary: str) -> str:
    return cut_at_end(str(review.changes or "").strip() or build_summary, SUMMARY_CHARS)


def unverified_measurement_section(disclosure: dict) -> str:
    requirement = verify.one_line(disclosure["requirement"])
    measurement = verify.one_line(disclosure["measurement"])
    return (f"{MEASUREMENT_START}\n\n{UNVERIFIED_REQUIREMENT_HEADING}\n\n{requirement}\n\n"
            f"{RECORDED_MEASUREMENT_LABEL}\n\n{measurement}\n\n{MEASUREMENT_END}\n\n")


def markdown_fences(body: str) -> Iterator[tuple[int, int]]:
    start, offset, closing = 0, 0, None
    for line in body.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        if closing:
            if closing.fullmatch(bare):
                yield start, offset + len(line)
                closing = None
        elif opening := outbound.FENCE_OPEN.match(bare):
            start = offset
            fence = opening[2]
            closing = re.compile(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*")
        offset += len(line)
    if closing:
        yield start, offset


def with_unverified_measurement(body: str, section: str, *, observed_requirements: list[str] | None = None,
                                existing_only: bool = False) -> str:
    found = MEASUREMENT_SECTION.search(body)
    if found:
        for start, end in markdown_fences(body):
            if not found or found.start() < start:
                break
            if found.start() < end:
                found = MEASUREMENT_SECTION.search(body, end)
    if found:
        if not section and observed_requirements is not None and found["requirement"].casefold() not in observed_requirements:
            return body
        replacement = section.rstrip() + found["separator"] if section else ""
        return body[:found.start()] + replacement + body[found.end():]
    if section and not existing_only:
        return f"{body}\n\n{section}" if body else section
    return body


def observed_measurements(run: Run, verified: verify.Verified | None) -> list[str]:
    if verified is None:
        return []
    return sorted({gh.outgoing(public_text(run, requirement)).casefold()
                   for requirement in verified.observed_requirements()})


def report_body(run: Run, *, summary: str, review: Review, verified: verify.Verified, left: str,
                images: dict[str, str], approval: dict | None, attempts: int, checks: str, plan_text: str,
                size: str, held: str = "", written: str = "", measurement_section: str = "") -> str:
    return public_text(run, (
        f"## Summary\n\n{written or summary_of(review, summary)}\n\n"
        + ("" if run.feedback else triage_block(approval))
        + left_block(left)
        + assumed_block(run)
        + f"{CHECKED_IN_USE_HEADING}\n\n{verified.markdown(images)}\n\n"
        + measurement_section
        + review.markdown()
        + (f"\n\nHeld as a draft: {held}." if held else "")
        + f"\n\n## Checks\n\nAll passed after {util.count(attempts, 'attempt')}: {checks}\n\n"
        + ("" if run.feedback else
           f"<details><summary>Plan</summary>\n\n{cut_at_end(plan_text, PLAN_CHARS)}\n\n</details>\n\n")
        + f"Run `{run.id}`, {size}, {util.paid_note(record.metered_spent(run.id))}."
        + (f" Repairs the review findings of run `{run.repairs}`." if run.repairs else "")
    ))


def automerge(b: Build, approval: dict | None, draft_why: str, verified: verify.Verified, review: Review,
              opened: list[workspace.Checkout], about: str, *, slot: slots.RunSlot, branch: str,
              goal: str, plan: str) -> str:
    if not opened or not automerge_allowed(b, approval, draft_why):
        return ""
    run = b.run
    ci_result = ""
    for c in opened:
        with slots.take_merge_lease(run, c.github, slot=slot) if len(b.checkouts) == 1 else nullcontext():
            while True:
                hold = try_automerge(b, c, verified, review, about, track_moved_base=True)
                if hold is None:
                    return ci_result
                if not isinstance(hold, gh.MovedBaseHold):
                    break
                try:
                    base_sha, hold = catch_up_target(b, c, slot)
                    if hold:
                        break
                    b.catch_ups += 1
                    verified, review = catch_up_base(b, c, branch, base_sha, goal, plan, review)
                    note_review(run, review, verified)
                    publish_gate_evidence(run, b.ticket, c, verified, review)
                    max_rounds = run.settings("ci_repair").tries
                    rounds = watch_publication_ci(b, slot, [c], max_rounds)
                    current = rounds[-1]
                    ci_result = current.summary()
                    comment_catch_up_ci(run, c, ci.section(rounds, max_rounds), verified, review)
                except CatchUpPushUnconfirmed:
                    raise
                except (workspace.RunFailed, RuntimeError, OSError) as e:
                    reason = e.why if isinstance(e, workspace.RunFailed) else util.first_line(str(e), 300)
                    hold = f"catch-up to `{c.base}` did not complete: {public_text(run, reason)}"
                    break
                if not current.passed:
                    if c.lease == current.seen[0].target.sha and own_draft_state(run, c) is False:
                        set_draft(run, c, True)
                    hold = f"catch-up CI held: {ci_result}"
                    break
                run.retry_gate_evidence()
            report_automerge(run, c, hold)
    return ci_result


def automerge_allowed(b: Build, approval: dict | None, draft_why: str) -> bool:
    from . import merge_review

    run = b.run
    if draft_why or b.layer.trust_level != "automerge" or run.feedback or run.repairs:
        return False
    triaged = (approval and approval["sent_by"] == "triage") or intake.AUTO_LABEL in b.ticket.labels
    if triaged and not merge_review.enrolled(run):
        run.facts["merge_held"] = "triage approved the ticket, so a person merges it"
        run.log(f"automerge left off: {run.facts['merge_held']}")
        return False
    return True


def try_automerge(b: Build, c: workspace.Checkout, verified: verify.Verified, review: Review, about: str, *,
                  track_moved_base: bool = False) -> str | None:
    run = b.run
    try:
        return merge_hold(b, c, verified, review, about, track_moved_base=track_moved_base)
    except gh.GhUnsure:
        run.facts["merge_held"] = "GitHub did not confirm whether it merged the PR"
        run.log(f"automerge: {run.facts['merge_held']} at {c.lease}; check {c.pr} before retrying")
        return None
    except (RuntimeError, OSError, KeyError, TypeError, ValueError) as e:
        run.log(f"automerge could not establish eligibility: {public_text(run, util.first_line(str(e), 300))}")
        return "the merge gate could not be completed"


def report_automerge(run: Run, c: workspace.Checkout, hold: str) -> None:
    from . import merge_review

    if not hold:
        run.log(f"automerged {c.pr} at {c.lease}")
        return
    run.facts["merge_held"] = hold
    if hold == merge_review.PENDING or (merge_review.enrolled(run) and hold != merge_review.MULTIPLE):
        run.log(f"{c.pr}: {hold}")
        return
    run.log(f"automerge left {c.pr} for a person: {hold}")
    try:
        gh.pr_comment(c.pr, public_text(run, f"Automerge stays off for this PR: {hold}. A person merges it."))
    except gh.GhError as e:
        run.log(f"automerge could not report the hold on {c.pr}: {e}")


def catch_up_target(b: Build, c: workspace.Checkout, slot: slots.RunSlot) -> tuple[str, str]:
    if b.catch_ups < AUTOMERGE_CATCH_UPS:
        slot.take(b.run.log, b.run.waiting, "automerge")
    row = gh.pr_merge_details(c.pr)
    if hold := gh.merge_ref_hold(row, c.lease, c.base):
        return "", hold
    base_sha = fetch_merge_base(c) or row.get("baseRefOid")
    if not base_sha:
        return "", "GitHub did not report the current base commit"
    if b.catch_ups >= AUTOMERGE_CATCH_UPS:
        return base_sha, f"the base `{c.base}` moved to `{base_sha}` after {b.catch_ups} catch-ups"
    return base_sha, ""


def log_catch_up_failure(run: AgentRun, action: str, error: BaseException) -> None:
    with suppress(Exception):
        reason = util.first_line(str(error), 300) or type(error).__name__
        run.log(public_text(run, f"automerge: catch-up {action}: {reason}"))


def catch_up_paths(wt: Path) -> set[str]:
    return workspace.untracked_files(wt) | workspace.ignored_file_paths(wt)


def remove_new_paths(run: AgentRun, wt: Path, before: set[str], after: set[str], untracked: set[str]) -> bool:
    restored = True
    directories: dict[Path, bool] = {}
    with local_paths.directory_fd(wt) as root:
        for name in sorted(after - before):
            path = Path(name)
            try:
                if not path.parts or path.is_absolute() or ".." in path.parts:
                    raise PermissionError("path is not inside the worktree")
                if name.endswith("/"):
                    directories[path] = True
                    continue
                try:
                    with local_paths.directory_fd(path.parent, root) as parent:
                        os.unlink(path.name, dir_fd=parent)
                except FileNotFoundError:
                    pass
                if name in untracked:
                    for ancestor in path.parents:
                        if ancestor.parts:
                            directories.setdefault(ancestor, False)
            except Exception as failed:
                restored = False
                log_catch_up_failure(run, f"cleanup could not remove {name!r}", failed)
        for path in sorted(directories, key=lambda p: (len(p.parts), p.as_posix()), reverse=True):
            record = directories[path]
            try:
                with local_paths.directory_fd(path.parent, root) as parent:
                    os.rmdir(path.name, dir_fd=parent)
            except FileNotFoundError:
                continue
            except Exception as failed:
                occupied = isinstance(failed, OSError) and failed.errno in (errno.ENOTEMPTY, errno.EEXIST)
                if record or not occupied:
                    restored = False
                    log_catch_up_failure(run, f"cleanup could not remove {path.as_posix() + '/'!r}", failed)
    return restored


def catch_up_base(b: Build, c: workspace.Checkout, branch: str, base_sha: str,
                  goal: str, plan: str, prior_review: Review, *,
                  before_push: Callable[[str, verify.Verified, Review], None] | None = None,
                  check_targets: Callable[[], None] | None = None) -> tuple[verify.Verified, Review]:
    run = b.run
    run.catch_up_owns_checkout = True
    if git(["rev-parse", "HEAD"], c.wt).strip() != c.lease or workspace.uncommitted(c.wt, tracked_only=True):
        raise workspace.RunFailed("the worktree differs from the published head")
    tree = git(["rev-parse", f"{c.lease}^{{tree}}"], c.wt).strip()
    if not tree or not prior_review.approved() or prior_review.trees != {c.prefix: tree}:
        raise workspace.RunFailed("the published tree lacks a current approving review")
    before = replace(c)
    checked, report, staged = b.checked, b.report, b.staged
    wt = c.wt.resolve()
    worktree_files = catch_up_paths(c.wt)
    check_files = None
    blocked = run.owner_questions_blocked
    run.owner_questions_blocked = True
    try:
        if not merge_moved_bases(run, [c], b.layer, b.ticket, "automerge: ",
                                bases={c.github: base_sha}, allow_untracked=True):
            raise workspace.RunFailed("the published head already contains the fetched base")
        head = c.merge
        if workspace.uncommitted(c.wt):
            raise workspace.RunFailed("the worktree has uncommitted files after merging the moved base")
        b.require_valid_diff("catching up the moved base", from_base=True)
        check_files = catch_up_paths(c.wt)
        if getattr(run, "code_review", False) is True:
            raise CatchUpGateHold("review", "the base changed; run the configured checks and review this candidate",
                                  head, "")
        if not b.check(staged=b.staged, defer_failed_cleanup=True):
            raise CatchUpGateHold("checks", "the layer's local checks failed", head, b.report)
        if not b.layer.verify:
            raise CatchUpGateHold("verify", "the layer has no behavioral verification configured", head, b.report)
        verified = check_in_use(b, goal, b.build_summary)
        review = _review(b, plan, verified, step=b.next_step("review"), before=prior_review, catch_up=c)
        if problem := merge_evidence_problem(b, c, verified, review, head):
            gate, why = problem
            raise CatchUpGateHold(gate, why, head, b.report, verified, review)
        if git(["rev-parse", "HEAD"], c.wt).strip() != head or workspace.uncommitted(c.wt):
            raise workspace.RunFailed("the worktree changed while checking the merge")
        if hold := gh.merge_ref_hold(gh.pr_merge_details(c.pr), c.lease, c.base):
            raise workspace.RunFailed(hold)
        if before_push is not None:
            before_push(head, verified, review)
    except BaseException as error:
        log_catch_up_failure(run, "failed", error)
        gate_hold = error if isinstance(error, CatchUpGateHold) else None
        if isinstance(error, CatchUpPathCollision):
            worktree_files.add(error.name)
            run.catch_up_preserved.add(c.prefix + error.name)
        tracked_restored = paths_restored = False
        try:
            abort_merge(c.wt)
            restore_head = gate_hold.head if gate_hold else before.lease
            if (git(["rev-parse", "HEAD"], c.wt).strip() != restore_head
                    or workspace.uncommitted(c.wt, tracked_only=True)):
                git(["reset", "--hard", restore_head], c.wt)
            tracked_restored = True
        except Exception as failed:
            log_catch_up_failure(run, "cleanup could not restore the worktree head", failed)
        try:
            baseline = worktree_files if check_files is None else check_files
            paths_restored = remove_new_paths(run, wt, baseline, catch_up_paths(c.wt),
                                              workspace.untracked_files(c.wt))
        except Exception as failed:
            log_catch_up_failure(run, f"cleanup could not clean {str(wt)!r}", failed)
        if gate_hold:
            gate_hold.candidate_restored = tracked_restored and paths_restored
        else:
            c.__dict__.update(vars(before))
            b.checked, b.report, b.staged = checked, report, staged
        if tracked_restored and paths_restored and _termination is None:
            run.catch_up_owns_checkout = False
        if isinstance(error, owner_asks.Asked):
            raise CatchUpNeedsInput("a catch-up worker needs owner input") from error
        raise
    finally:
        run.owner_questions_blocked = blocked
    target_check = {"check_targets": check_targets} if check_targets else {}
    push_caught_up_head(b, c, branch, head, **target_check)
    run.catch_up_owns_checkout = False
    return verified, review


def push_caught_up_head(b: Build, c: workspace.Checkout, branch: str, head: str, *,
                        check_targets: Callable[[], None] | None = None) -> None:
    b.run.log(f"automerge: pushing catch-up {b.catch_ups} to {c.pr} at {head}, after {c.lease}")
    candidate = replace(c)
    pending_push = True
    why = f"the catch-up push to `{branch}` could not be confirmed"
    detail = f"Previous head `{c.lease}`, attempted head `{head}`, PR {c.pr}."

    def exact_head(_):
        if check_targets:
            check_targets()
        return head

    for attempt in range(4):
        try:
            row = gh.pr_merge_details(c.pr)
        except ci.READ_ERRORS:
            row = {}
        remote = row.get("headRefOid")
        if remote == head:
            c.note_pushed_head(head)
            c.onto = ""
            return
        if remote and (hold := gh.merge_ref_hold(row, c.lease, c.base)):
            raise CatchUpPushUnconfirmed(f"{why}: {hold}", f"{detail} GitHub reports `{remote}`.", retryable=False)
        if attempt == 3:
            break
        if pending_push and remote == c.lease:
            try:
                commit_and_push([candidate], [candidate], branch, "", before_push=exact_head)
                pending_push = False
            except (workspace.RunFailed, RuntimeError, OSError) as failed:
                b.run.log(f"automerge: catch-up push failed: {public_text(b.run, util.first_line(str(failed), 300))}")
        if attempt or not remote:
            time.sleep(gh.MERGE_POLL_SECONDS)
    raise CatchUpPushUnconfirmed(why, detail, retryable=True)


def merge_evidence_hold(b: Build, c: workspace.Checkout, verified: verify.Verified, review: Review, head: str) -> str:
    problem = merge_evidence_problem(b, c, verified, review, head)
    return problem[1] if problem else ""


def merge_evidence_problem(b: Build, c: workspace.Checkout, verified: verify.Verified,
                           review: Review, head: str) -> tuple[str, str] | None:
    from . import code_review

    review_only = (getattr(b.run, "code_review", False) is True and code_review.completed(review)
                   and not code_review.requires_behavior(b) and (verified == code_review.unverified()
                   or verified.verdict == "blocked" and bool(verified.nonblocking_reason)))
    if not review_only and (verified.verdict != "pass" or not verified.finished or not verified.checks or verified.failed_checks):
        return "verify", "native behavioral verification did not pass"
    if not head:
        return "verify", "this run has no published head to verify"
    tree = git(["rev-parse", f"{head}^{{tree}}"], c.wt).strip()
    trees = {c.prefix: tree}
    if not tree or (not review_only and verified.native_trees != trees) or (
            verified.nonblocking_reason and verified.nonblocking_trees != trees):
        return "verify", "there is no native behavioral pass for the published tree"
    if not review.approved() or review.trees != trees:
        return "review", "the published tree lacks a current approving review"
    if not b.layer.checks or b.checked.get("tree") != tree or b.checked.get("commands") != b.layer.checks:
        return "checks", "the published tree lacks the full local check result"
    return None


def merge_hold(b: Build, c: workspace.Checkout, verified: verify.Verified, review: Review, about: str, *,
               track_moved_base: bool = False) -> str:
    from . import merge_review

    if len(b.checkouts) != 1:
        return merge_review.MULTIPLE
    head = c.lease
    if hold := merge_evidence_hold(b, c, verified, review, head):
        return hold
    if not jev.ready(b.cfg, [b.run.repo]):
        return "JEV risk screening is unavailable"
    base_sha = fetch_merge_base(c)
    if not has_merge_base(c, head, base_sha):
        hold = "the checked head does not contain the target base"
        return gh.MovedBaseHold(hold) if track_moved_base else hold
    if merge_review.enrolled(b.run):
        if b.cfg.replay.all or paths.ISOLATED:
            return "Live issue merge decisions require a live delivery"
        return merge_review.nominate(b, c, verified, review, base_sha, about)
    number = gh.pr_number(c.pr)
    _, hold = gh.merge_readiness(c.github, number, head, c.base, base_sha, wait=gh.MERGE_WAIT_SECONDS,
                                 track_moved_base=track_moved_base)
    if not hold and (c.github, head) in b.ci_rerun_requested_heads:
        hold = rerun_head_hold(b, c, head)
    if not hold:
        hold = screen_and_merge(b, c, number, head, base_sha, about, track_moved_base=track_moved_base)
    if isinstance(hold, gh.BaseTipChanged) and not has_merge_base(c, head, fetch_merge_base(c)):
        return gh.MovedBaseHold("the checked head does not contain the target base")
    return hold


def rerun_head_hold(b: Build, c: workspace.Checkout, head: str) -> str:
    try:
        if gh.publication_ci_passed(c.github, head):
            return ""
    except ci.READ_ERRORS as e:
        b.run.log(f"ci: could not refresh the checks of {c.pr_name}: {util.first_line(str(e), 300)}")
    return "GitHub has no fully passing CI result for the verified head"


def fetch_merge_base(c: workspace.Checkout) -> str:
    workspace.fetch(c.src, c.base)
    return git(["rev-parse", c.origin_base], c.wt).strip()


def has_merge_base(c: workspace.Checkout, head: str, base_sha: str) -> bool:
    return bool(base_sha) and git(["merge-base", base_sha, head], c.wt).strip() == base_sha


def screen_and_merge(b: Build, c: workspace.Checkout, number: int, head: str, base_sha: str, about: str, *,
                     track_moved_base: bool) -> str:
    patch = checkout_patch(c, [], f"{base_sha}..{head}")
    if not patch:
        return "there is no diff to risk screen"
    try:
        risk = jev.risk(vault.hidden(patch), vault.hidden(about), b.cfg, trace=(b.run.id, "automerge risk jev"))
    except jev.Unjudged:
        return "JEV could not judge the diff"
    if risk.hold:
        return risk.hold
    return gh.pr_merge_auto(c.github, number, head, c.base, base_sha, track_moved_base=track_moved_base)


def nothing_to_build(run: Run, t: tracker.Ticket, ws: Path, checkouts: list[workspace.Checkout], branch: str, summary: str,
                     left: str) -> tuple[str, str, str]:
    with_prs = [c for c in checkouts if c.pr]
    why = f"the open PRs already cover {t.ref}" if with_prs else "the plan found no code change to make"
    run.log(f"build: nothing to build, since {why}")
    if run.dry_run:
        release_workspace(run, ws, checkouts)
        return "dry-run", f"nothing to build: {why}", ""
    said = cut_summary(summary) + (f"\n\n{LEFT_HEADING}\n\n{left}" if left else "")
    if not with_prs:
        return _ask(run, ws, checkouts,
                    f"I made no change, since {why}.\n\n{said}\n\nClose this if it is done. If it is not, say here "
                    f"what is missing, then remove the `{run.tracker.label_name('needs-info')}` label and I will "
                    "pick it up.", f"nothing to build: {why}")
    sync_pr_bodies(run, t, with_prs, [], [], [], "", left, "")
    run.enter("pr")
    prs = pr_link_list(with_prs)
    run.say(f"Nothing more to build, since the open PRs already cover this:\n\n{prs}\n\n{said}")
    urls = record_prs(run, branch, with_prs)
    release_workspace(run, ws, checkouts)
    return "pr", f"nothing to build: {why}", urls


def synced_pr_body(run: Run, pr: str, body: str, t: tracker.Ticket, left: str) -> tuple[str, str]:
    wanted = run.tracker.pr_link(t, whole=not left)
    links = (run.tracker.pr_link(t, whole=True), run.tracker.pr_link(t, whole=False))
    if t.is_spec and isinstance(run.tracker, tracker.GitHubTracker):
        links += (f"Closes #{t.id}",)
    had, text = gh.split_link(body, links)
    if had and had != wanted:
        run.log(f"{pr} now links the ticket with: {wanted}")
    left_synced = with_left_section(text, left)
    if left_synced != text:
        run.log(f"{pr}: {'wrote' if left else 'removed'} the {LEFT_TITLE} section")
    return (wanted if had else ""), public_text(run, left_synced)


def publish_unverified_measurement(run: Run, t: tracker.Ticket, pr: str, section: str, *,
                                   observed_requirements: list[str]) -> None:
    try:
        body = gh.pr_body(pr)
    except ci.READ_ERRORS as error:
        run.log(f"ci: could not read the unverified requirement in {pr}: {util.first_line(str(error), 300)}")
        return
    links = (run.tracker.pr_link(t, whole=True), run.tracker.pr_link(t, whole=False))
    kept, text = gh.split_link(body, links)
    updated = with_unverified_measurement(text, public_text(run, section),
                                         observed_requirements=observed_requirements, existing_only=True)
    if updated != text and gh.outgoing(updated, kept) != body:
        gh.pr_edit_body(pr, updated, link=kept)


def sync_pr_bodies(run, t, with_prs, opened, standing, pushed, report, left, link, stops=None, committed=True, *,
                   measurement_section: str = ""):
    stops = stops or {}
    ci_stopped = []
    for c in with_prs:
        others = sibling_section(t.ref, c, with_prs)
        if c in opened:
            if others:
                text = f"{report}{others}"
                stop = stops.get(c.github, "")
                gh.pr_edit_body(c.pr, ci.with_section(text, stop) if stop else text, link=link)
            continue
        body = gh.pr_body(c.pr)
        if c in standing and ci.stopped(body):
            ci_stopped.append(c)
        kept, text = synced_pr_body(run, c.pr, body, t, left)
        if opened:
            text = with_siblings(text, others)
        if c in standing:
            text = with_unverified_measurement(text, public_text(run, measurement_section))
        if gh.outgoing(text, kept) != body:
            gh.pr_edit_body(c.pr, text, link=kept)
        if c in pushed and c.pr not in run.feedback_prs:
            gh.pr_comment(c.pr, f"Pushed more work for {t.ref}.\n\n{report}")
        elif not committed and c in standing and c.pr not in run.feedback_prs:
            gh.pr_comment(c.pr, f"Verified and reviewed {t.ref} again; nothing new was pushed.\n\n{report}")
    return ci_stopped


def left_section(run: Run, text: str) -> str:
    if len(text) <= MAX_LEFT_FOR_A_PERSON_CHARS:
        return gh.outgoing(text)
    return gh.outgoing(
        f"{text[:MAX_LEFT_FOR_A_PERSON_CHARS]}\n\n(Cut at {MAX_LEFT_FOR_A_PERSON_CHARS} characters. The whole "
        f"change is `left_for_a_person` in `plan.json` of run `{run.id}`.)")


def assumed_block(run: Run) -> str:
    taken = owner_asks.assumed(factory.chain(run.id))
    if not taken:
        return ""
    return "## Assumed\n\nArete took these recommended options without asking:\n" + "".join(f"- {c}\n" for c in taken) + "\n"


def left_block(left: str) -> str:
    return f"{LEFT_START}\n{LEFT_HEADING}\n\n{left}\n\n{LEFT_END}\n\n" if left else ""


def with_left_section(body: str, left: str) -> str:
    found = LEFT_FOR_A_PERSON.search(body)
    if (found.group(1) if found else "") == left:
        return body
    if found:
        return body[:found.start()] + left_block(left) + body[found.end():]
    checked = body.find(f"\n{CHECKED_IN_USE_HEADING}\n")
    if checked < 0:
        return f"{body.rstrip()}\n\n{left_block(left).rstrip()}"
    return body[:checked + 1] + left_block(left) + body[checked + 1:]


def left_on_prs(urls: list[str]) -> str:
    for url in urls:
        if found := LEFT_FOR_A_PERSON.search(gh.pr_body(url)):
            return found.group(1)
    return ""


def record_prs(run: Run, branch: str, with_prs: list[workspace.Checkout]) -> str:
    urls = [c.pr for c in with_prs]
    supersede_older_runs(run, urls)
    record.note(run.id, branch=branch)
    return " ".join(urls)


def note_prs(run: Run, branch: str, with_prs: list[workspace.Checkout]) -> None:
    record.note(run.id, branch=branch, pr_url=" ".join(c.pr for c in with_prs))


def commit_and_push(checkouts: list[workspace.Checkout], changed: list[workspace.Checkout], branch: str, message: str,
                    *, before_push: Callable[[workspace.Checkout], str] | None = None,
                    check_targets: Callable[[], None] | None = None) -> None:
    for c in changed:
        if not git(["diff", "--cached", "--name-only"], c.wt).strip():
            continue
        before = git(["rev-parse", "HEAD"], c.wt).strip()
        try:
            util.git_as_owner(["commit", "-q", "-m", message], c.wt)
        except RuntimeError as e:
            raise workspace.RunFailed(f"the commit in {c.github} failed, so arete pushed nothing", str(e)) from e
        c.onto = c.onto or before
    moved = [c.github for c in checkouts if workspace.remote_head(c.src, branch) != c.lease]
    if moved:
        raise workspace.RunFailed(f"`{branch}` moved on {', '.join(moved)} while this run worked, so arete pushed nothing",
                        "Someone pushed to that branch after this run started. A retry builds on top of their work.")
    pushed: list[workspace.Checkout] = []
    attempt = {"before_attempt": check_targets} if check_targets else {}
    for c in changed:
        if before_push:
            head = before_push(c)
            ref = f"refs/heads/{branch}"
            argv = ["push", "-q", "--no-follow-tags", "--recurse-submodules=no", f"--force-with-lease={ref}:{c.lease}",
                    "origin", f"{head}:{ref}"]
        else:
            head = ""
            argv = ["push", "-q", f"--force-with-lease={branch}:{c.lease}", "-u", "origin", branch]
        try:
            git(argv, c.wt, timeout=600, retry=not before_push, **attempt)
        except RuntimeError as e:
            done = ", ".join(p.github for p in pushed) or "nothing"
            aftermath = ("The pilot will not retry the push." if before_push
                         else "A retry builds on whatever reached the branch.")
            raise workspace.RunFailed(f"the push to {c.github} failed after arete pushed {done}", f"{e}\n\n{aftermath}") from e
        c.note_pushed_head(head or git(["rev-parse", "HEAD"], c.wt).strip())
        c.onto = ""
        pushed.append(c)


CiFix = Callable[[str, str, str], Result]


def watch_ci(b: Build, slot: slots.RunSlot, branch: str, pushed: list[workspace.Checkout], with_prs: list[workspace.Checkout],
             draft_why: str, fix: CiFix | None = None) -> str:
    run, max_rounds = b.run, b.run.settings("ci_repair").tries
    stopped_before = [c for c in pushed if stopped_by_ci(run, c)]
    rounds: list[ci.Round] = []
    try:
        for current in ci_rounds(b, slot, branch, pushed, max_rounds, fix):
            rounds.append(current)
            publish_ci(run, b.ticket, pushed, ci.section(rounds, max_rounds))
    except (owner_asks.Asked, tracker.TaskError):
        raise
    except Exception as e:
        failed = e if isinstance(e, workspace.RunFailed) else workspace.RunFailed(f"the CI watch crashed: {type(e).__name__}: {e}")
        drafted = [c for c in with_prs if set_draft(run, c, True)]
        ending = f"{ci.STOPPED} {failed.why}. {drafted_note(drafted, with_prs)}"
        publish_ci(run, b.ticket, with_prs, ci.section(rounds, max_rounds, ending))
        if failed is e:
            raise
        raise failed from e
    last = rounds[-1]
    if stopped_before and not draft_why and all(w.read or w.previous_failure for w in last.seen):
        for c in stopped_before:
            set_draft(run, c, False)
    elif stopped_before:
        why = draft_why or "arete could not read every check"
        ending = f"{ci.STOPPED} an earlier CI watch made this PR a draft. It stays a draft, since in this run {why}."
        publish_ci(run, b.ticket, stopped_before, ci.section(rounds, max_rounds, ending))
    return last.summary()


def stopped_by_ci(run: AgentRun, c: workspace.Checkout) -> bool:
    try:
        return ci.stopped(gh.pr_body(c.pr))
    except gh.GhError as e:
        run.log(f"ci: could not read the body of {c.pr}: {util.first_line(str(e), 300)}")
        return False


def drafted_note(drafted: list[workspace.Checkout], with_prs: list[workspace.Checkout]) -> str:
    if drafted != with_prs:
        return "Arete could not turn every PR to a draft."
    return "The PR is now a draft." if len(with_prs) == 1 else "The PRs are now drafts."


def set_draft(run: AgentRun, c: workspace.Checkout, draft: bool, by: str = "ci") -> bool:
    now = "a draft" if draft else "ready for review"
    try:
        gh.pr_set_draft(c.pr, draft)
    except gh.GhError as e:
        run.log(f"{by}: could not mark {c.pr} {now}: {util.first_line(str(e), 300)}")
        return False
    run.log(f"{by}: marked {c.pr} {now}")
    return True


def own_draft_state(run: AgentRun, c: workspace.Checkout) -> bool | None:
    try:
        draft, head = gh.pr_draft_and_head(c.pr)
    except ci.READ_ERRORS as e:
        run.log(f"pr: left {c.pr} as it is: arete could not read its state: {util.first_line(str(e), 300)}")
        return None
    if not head or not c.lease:
        run.log(f"pr: left {c.pr} as it is: arete could not compare its head with this run's push")
        return None
    if head != c.lease:
        run.log(f"pr: left {c.pr} as it is: its head is {head[:12]}, not the {c.lease[:12]} this run pushed or "
                "checked again, so someone changed it after the run started")
        return None
    return draft


def hold_pushed(run: AgentRun, pushed: list[workspace.Checkout], ci_stopped: list[workspace.Checkout],
                draft_why: str) -> list[workspace.Checkout]:
    if not draft_why or run.repairs or run.feedback:
        return []
    held = []
    for c in pushed:
        if c in ci_stopped:
            continue
        draft = own_draft_state(run, c)
        if draft is None:
            continue
        if draft or set_draft(run, c, True, "pr"):
            held.append(c)
    return held


def ready_pushed(run: AgentRun, pushed: list[workspace.Checkout], ci_stopped: list[workspace.Checkout],
                 draft_why: str) -> list[workspace.Checkout]:
    if draft_why or run.repairs or run.feedback:
        return []
    ready = []
    for c in pushed:
        if c in ci_stopped:
            continue
        draft = own_draft_state(run, c)
        if draft is None:
            continue
        if not draft or set_draft(run, c, False, "pr"):
            ready.append(c)
    return ready


def published_note(draft_why: str, verified: verify.Verified, opened: list[workspace.Checkout],
                   drafted: list[workspace.Checkout]) -> str:
    if drafted:
        return draft_note(draft_why, verified, pushed=True)
    return draft_note(draft_why, verified) if opened else ""


def ci_rounds(b: Build, slot: slots.RunSlot, branch: str, pushed: list[workspace.Checkout], max_rounds: int,
              fix: CiFix | None = None) -> Iterator[ci.Round]:
    run = b.run
    watched = list(pushed)
    not_caused: dict[tuple[str, str, gh.CheckKey], tuple[str, int, str]] = {}
    reruns = classic_ci_reruns(b)
    for n in range(1, max_rounds + 1):
        slot.free()
        run.log(slots.FREED_SLOT_FOR_CI)
        with run.waiting("ci"):
            current = ci.Round(n, [watch_checkout_ci(run, c, reruns.for_checkout(c)) for c in watched])
        reruns.keep_failure_evidence(current)
        reds = current.reds
        judged = [r for r in reds if r.kind != "base"]
        for r in judged:
            target = r.target
            if (r.kind == "unjudged" and r.key in not_caused
                    and (target.github, target.sha, target.pr) not in reruns.reruns):
                r.caused = False
                r.reason, judged_job, rerun_reason = not_caused[r.key]
                if r.check.job == judged_job:
                    r.rerun_reason = rerun_reason
        fresh = [r for r in judged if r.kind == "unjudged"]
        if fresh:
            slot.take(run.log, run.waiting)
        try:
            fix_summary = judge_ci(b, fresh, n, max_rounds, watched, fix) if fresh else ""
        except workspace.RunFailed:
            yield current
            raise
        not_caused.update({r.key: (r.reason, r.check.job, r.rerun_reason) for r in judged if not r.caused})
        retried = n < max_rounds and reruns.retry(run, current, reruns.candidate(current))
        yield current
        caused = current.caused
        if not caused:
            if fresh:
                discard_edits(b)
            if retried:
                continue
            return
        names = ", ".join(r.label for r in caused)
        if n == max_rounds:
            raise workspace.RunFailed(f"CI still fails after {util.count(max_rounds, 'round')}: {names}", ci.red_list(caused))
        push_ci_fix(b, branch, caused, names, n)
        watched += [c for c in b.staged.changed if c not in watched]
        current.story = f"{names} failed, caused by this PR, and arete pushed a fix: {fix_summary}".rstrip(": ")
        run.log(f"ci: pushed a fix for {names}")


def classic_ci_reruns(b: Build) -> AutomaticCIReruns:
    run = b.run
    b.ci_rerun_requested_heads.update(
        (line["repo"], line["head"]) for read in factory.chain(run.id) for line in read
        if line.get("event") == "ci_rerun_request")

    def remember(repo: str, head: str) -> None:
        run.write_line("ci_rerun_request", event_id=f"{repo}@{head}", repo=repo, head=head)

    return AutomaticCIReruns(b.ci_rerun_requested_heads, remember)


class AutomaticCIReruns:
    def __init__(self, requested_heads: set[tuple[str, str]], remember: Callable[[str, str], None] | None = None):
        self.requested_heads = requested_heads
        self.remember = remember
        self.reruns: dict[tuple[str, str, str], list[gh.WorkflowRerun]] = {}
        self.before: dict[tuple[str, str, str], ci.Watched] = {}

    def for_checkout(self, c: workspace.Checkout):
        return self.reruns.get((c.github, c.lease, c.pr), ())

    def keep_failure_evidence(self, current: ci.Round) -> None:
        for i, seen in enumerate(current.seen):
            target = seen.target
            before = self.before.get((target.github, target.sha, target.pr))
            if before and not seen.read and not seen.rerun_reds:
                current.seen[i] = replace(before, read=False, conflicting=seen.conflicting, previous_failure=True)

    def candidate(self, current: ci.Round) -> tuple[str, str, str, int] | None:
        if not current.reds or any(0 in w.red_workflows for w in current.seen):
            return None
        keys = {(w.target.github, w.target.sha, w.target.pr, workflow)
                for w in current.seen for workflow in w.red_workflows}
        for red in current.reds:
            target = red.target
            workflow = red.check.actions_workflow(target.github)
            if not target.pr or not workflow or (target.github, target.sha) in self.requested_heads:
                return None
            keys.add((target.github, target.sha, target.pr, workflow))
        return keys.pop() if len(keys) == 1 else None

    def retry(self, run: Run, current: ci.Round, candidate: tuple[str, str, str, int] | None) -> bool:
        checks = current.reds
        if not candidate or not current.all_read or any(
                r.kind != "base" and (r.kind != "not caused" or not r.rerun_reason) for r in checks):
            return False
        repo, head, pr, workflow = candidate
        names = ", ".join(r.label for r in checks)
        try:
            row = gh.workflow_run(repo, head, workflow, pr=pr, allow_head_branch=True)
            if not row.get("conclusion") or gh.check_state(row) != "fail":
                return False
            failed_jobs = gh.failed_workflow_jobs(repo, head, workflow, row["run_attempt"])
            judged_jobs = {r.check.job: r.check.name for r in checks if type(r.check.job) is int}
            if len(judged_jobs) != len(checks) or failed_jobs != judged_jobs:
                run.log(f"ci: could not account for all failed jobs in workflow run {workflow}; keeping the CI hold")
                return False
            pull = gh.pr_merge_details(pr)
            if pull.get("state") != "OPEN" or pull.get("headRefOid") != head:
                return False
        except ci.READ_ERRORS as e:
            run.log(f"ci: could not identify the workflow for {names}: {util.first_line(str(e), 300)}")
            return False
        if self.remember:
            self.remember(repo, head)
        self.requested_heads.add((repo, head))
        unsure = None
        failed = False
        try:
            gh.rerun_failed_jobs(repo, workflow)
            current.story = f"Arete reran failed jobs for {names}. GitHub accepted the rerun request."
        except gh.GhUnsure as e:
            unsure = util.first_line(str(e), 300)
            current.story = f"Arete requested a rerun for {names} and did not receive confirmation."
        except ci.READ_ERRORS as e:
            failed = True
            current.story = f"Arete requested a rerun for {names}. The request failed."
            run.log(f"ci: could not rerun failed jobs for {names}: {util.first_line(str(e), 300)}")
        for red in checks:
            reason = red.base_result if red.kind == "base" else red.rerun_reason
            reason = util.first_line(reason.replace("`", ""), 200)
            if unsure is not None:
                run.log(f"ci: rerun request for {red.label} was unconfirmed ({unsure}): {reason}")
            elif failed:
                run.log(f"ci: requested a rerun for {red.label}: {reason}")
            else:
                run.log(f"ci: reran failed jobs for {red.label}: {reason}")
        if failed:
            return False
        jobs = {r.check.name: r.check.job for r in checks}
        reasons = {r.check.name: f"It {r.base_result}." if r.kind == "base" else r.rerun_reason for r in checks}
        self.reruns[(repo, head, pr)] = [gh.WorkflowRerun(workflow, row["run_attempt"] + 1, jobs, reasons)]
        self.before[(repo, head, pr)] = next(w for w in current.seen if w.target is checks[0].target)
        return True


def push_ci_fix(b: Build, branch: str, caused: list[ci.Red], names: str, n: int) -> None:
    staged = b.restage()
    if staged.empty:
        raise workspace.RunFailed(f"{names} failed, caused by this PR, and the CI fix step changed nothing", ci.red_list(caused))
    if no_pr := [c.github for c in staged.changed if not c.pr]:
        raise workspace.RunFailed(f"the CI fix step changed {', '.join(no_pr)}, where this run has no PR, so arete pushed nothing",
                        ci.red_list(caused))
    if not b.check_with_repairs(after="the CI fix")[0]:
        report = f"checks-ci-round{n}.md"
        b.run.save(report, b.report)
        raise workspace.RunFailed("the checks failed after the CI fix",
                        f"{ci.red_list(caused)}\n\nThe check output is in `{report}` of run `{b.run.id}`.")
    b.require_valid_diff("the CI fix")
    b.push(branch, ci_fix_message(b, caused))


def ci_target(c: workspace.Checkout) -> ci.Target:
    return ci.Target(c.github, c.pr, c.pr_name, c.lease, c.base)


def watch_checkout_ci(run: Run, checkout: workspace.Checkout, reruns=()) -> ci.Watched:
    options = {"reruns": reruns} if reruns else {}
    return ci.compare_with_base(ci.watch(ci_target(checkout), run.log, **options), run.log)


def watch_publication_ci(b: Build, slot: slots.RunSlot, pushed: list[workspace.Checkout], max_rounds: int, *,
                         automatic: AutomaticCIReruns | None = None,
                         reruns: dict[str, list[gh.WorkflowRerun]] | None = None) -> list[ci.Round]:
    run = b.run
    automatic = automatic if automatic is not None else classic_ci_reruns(b)
    rounds = []
    n = 1 if reruns is None else 2
    while True:
        slot.free()
        try:
            with run.waiting("ci"):
                current = ci.Round(n, [watch_checkout_ci(
                    run, c, automatic.for_checkout(c) if reruns is None else reruns.get(c.github, ())) for c in pushed])
        finally:
            slot.take(run.log, run.waiting)
        rounds.append(current)
        automatic.keep_failure_evidence(current)
        if (reruns is not None or not current.all_read or not (candidate := automatic.candidate(current))
                or n >= max_rounds):
            return rounds
        unjudged = [r for r in current.reds if r.kind == "unjudged"]
        if unjudged:
            judge_publication_ci(b, pushed, unjudged, n)
        if not automatic.retry(run, current, candidate):
            return rounds
        n += 1


def unmerged_paths(wt: Path) -> list[str]:
    return util.git_records(["diff", "--name-only", "--diff-filter=U"], wt)


def marked(wt: Path, names: list[str]) -> list[str]:
    return [n for n in names if (wt / n).is_file()
            and CONFLICT_MARKER.search((wt / n).read_text(errors="replace"))]


def abort_merge(wt: Path) -> None:
    if git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], wt, check=False).strip():
        git(["merge", "--abort"], wt, check=False)


def resolution_diff(c: workspace.Checkout) -> diffguard.Diff:
    if not c.resolved:
        return diffguard.Diff()
    return diffguard.between(c.wt, c.base_commit, "HEAD", "--", *c.resolved, prefix=c.prefix, moves=True)


def note_fresh_bases(checkouts: list[workspace.Checkout]) -> None:
    for c in checkouts:
        if not c.resumes:
            c.base_commit = git(["rev-parse", c.origin_base if c.onto else "HEAD"], c.wt).strip()


def note_kept_head(c: workspace.Checkout) -> None:
    c.kept = git(["rev-parse", "HEAD"], c.wt).strip()


def commit_edits_before_merge(run: AgentRun, checkouts: list[workspace.Checkout], layer: config.RepoLayer,
                              committed: Callable[[workspace.Checkout], None] = lambda c: None) -> None:
    try:
        for c in checkouts:
            workspace.drop_untracked_bytecode(c.wt)
            if workspace.uncommitted(c.wt):
                commit_kept(run.id, c, layer)
                committed(c)
                if left := workspace.uncommitted(c.wt):
                    raise workspace.RunFailed(f"{c.github} holds edits to protected paths that arete does not keep "
                                    f"({', '.join(left[:5])}), so it merged nothing into them and left them as they are")
    except RuntimeError as e:
        raise workspace.RunFailed(f"the commit of the kept edits failed, so arete merged nothing into them: {e}") from e


class ConflictUnresolved(workspace.RunFailed):
    def __init__(self, error: workspace.RunFailed, paths: list[str]):
        super().__init__(error.why, error.detail, recovery=error.recovery)
        self.paths = paths


def merge_base_into_kept(run: AgentRun, checkouts: list[workspace.Checkout], layer: config.RepoLayer,
                         ticket: tracker.Ticket, *,
                         before_resolve: Callable[[], None] | None = None) -> list[workspace.Checkout]:
    merged = []
    for c in checkouts:
        try:
            workspace.fetch(c.src, c.base)
        except RuntimeError as e:
            run.log(f"could not fetch `{c.origin_base}` in {c.github}, so the step resumes on the base it has: {e}")
            continue
        try:
            commit_edits_before_merge(run, [c], layer)
            merged += merge_moved_bases(run, [c], layer, ticket,
                                        reason="since the base moved while the run waited for its answer",
                                        before_resolve=before_resolve)
        except workspace.RunFailed as e:
            files = f" in {', '.join(e.paths)}" if isinstance(e, ConflictUnresolved) else ""
            raise workspace.RunFailed(f"merging `{c.origin_base}` into the edits kept in {c.github} failed{files}: {e.why}. "
                            f"The worktree is back at the kept commit and the answers are kept, so "
                            f"`arete resume {run.id}` continues with them", e.detail, recovery=e.recovery) from e
    for c in merged:
        if not c.resumes:
            c.onto = c.base_commit
    return merged


def kept_edits_note(held: list[workspace.Checkout]) -> str:
    if not held:
        return ""
    ranges = ", ".join(f"`{c.onto[:12]}..HEAD` in `{c.prefix or './'}`" for c in held)
    return (f"When the run stopped to wait for the answer, arete committed the edits made so far, so `git status` "
            f"lists none of them and `HEAD` is that commit: `git log` and `git diff` over {ranges} show them. "
            "The owner made no edits in the worktree, since arete refuses edits it did not make there. To put a "
            "file back as it was before those edits, read it with `git show` at the commit before the range and "
            "write it yourself.")


def base_merge_note(merged: list[workspace.Checkout]) -> str:
    if not merged:
        return ""
    names = ", ".join(f"`{c.origin_base}` into `{c.prefix or './'}`" for c in merged)
    fixed = "".join(f" Arete resolved merge conflicts in {', '.join(f'`{p}`' for p in c.resolved)} of "
                    f"`{c.prefix or './'}`." for c in merged if c.resolved)
    return (f"Before this step arete merged {names}, since the base moved while the run waited for the owner's "
            f"answer.{fixed} The worktree now holds the base's changes, so read a file again before relying on what "
            "it held.")


def settle_starts(run: AgentRun, checkouts: list[workspace.Checkout], layer: config.RepoLayer, ticket: tracker.Ticket,
                  branch: str) -> None:
    moved = [c for c in checkouts if c.kept]
    commit_edits_before_merge(run, moved, layer, note_kept_head)
    try:
        for c in moved:
            git(["reset", "-q", "--hard", workspace.start_of(c, branch)], c.wt)
        note_fresh_bases(checkouts)
        merge_moved_bases(run, [c for c in checkouts if c.resumes], layer, ticket)
        for c in moved:
            merge_kept(run, c, layer, ticket, workspace.start_of(c, branch))
    except BaseException:
        for c in moved:
            if c.kept:
                abort_merge(c.wt)
                git(["reset", "-q", "--hard", c.kept], c.wt, check=False)
        raise


def merge_kept(run: AgentRun, c: workspace.Checkout, layer: config.RepoLayer, ticket: tracker.Ticket, start: str) -> None:
    arrived = git(["rev-parse", "HEAD"], c.wt).strip()
    git(["reset", "-q", "--hard", c.kept], c.wt)
    if git(["rev-list", "--count", f"HEAD..{arrived}"], c.wt).strip() != "0":
        with record.timed(run.id, "kept_merge", repo=c.github, kept=c.kept, onto=arrived):
            try:
                merge_as_run(run.id, c, arrived, start)
            except RuntimeError as e:
                conflicts = unmerged_paths(c.wt)
                if not conflicts:
                    raise workspace.RunFailed(f"git could not merge `{start}` into the edits the last run kept in {c.github}: "
                                    f"{e}") from e
                resolve_conflicts(run, c, conflicts, layer, ticket, incoming=(start, arrived))
        c.onto = arrived
        run.log(f"merged `{start}` into the edits the last run kept in {c.github}")
    c.kept = ""


def check_merge_paths(wt: Path, commit: str) -> None:
    ignored = workspace.ignored_file_paths(wt)
    existing = workspace.untracked_files(wt) | ignored
    if not existing:
        return
    tree = git(["merge-tree", "--write-tree", "--no-messages", "HEAD", commit], wt, check=False).partition("\n")[0]
    if not tree:
        raise workspace.RunFailed("Git could not preview the merge to check for worktree file collisions")
    diff = ["diff", "--no-renames", "--ignore-submodules=none", "--name-only", "--diff-filter=A"]
    added = util.git_records([*diff, f"HEAD...{commit}"], wt) + util.git_records([*diff, "HEAD", tree], wt)
    ignore_case = git(["config", "--bool", "core.ignoreCase"], wt, check=False).strip() == "true"
    targets = {Path(name.casefold() if ignore_case else name) for name in added}
    directories = {parent for path in targets for parent in path.parents}
    for name in sorted(existing):
        path = Path(name.casefold() if ignore_case else name)
        if path in targets or path in directories or not targets.isdisjoint(path.parents):
            kind = "ignored" if name in ignored else "untracked"
            raise CatchUpPathCollision(name, kind)


def merge_as_run(run_id: str, c: workspace.Checkout, commit: str, named: str, *, overwrite_ignore: bool = True) -> None:
    with util.repo_lock(c.src):
        options = []
        if not overwrite_ignore:
            check_merge_paths(c.wt, commit)
            options = ["--no-autostash", "--no-overwrite-ignore", "-s", "ort"]
        util.git_as_owner(["merge", "-q", "--no-edit", *options, "-m", f"Merge {named} at {commit[:12]}",
                           "-m", f"Arete-Run: {run_id}", commit], c.wt)


def parked_tracked_edits(ws: Path, c: workspace.Checkout, head: str, edits: list[str],
                         layer: config.RepoLayer) -> dict:
    made = workspace.made_before(ws)
    if workspace.head_recorded(made, c) != head:
        return {}
    deny = diffguard.denied_paths(layer)
    if not all((util.matches_any(name, deny) or util.matches_any(c.prefix + name, deny))
               and workspace.recorded(made, c.wt, name, c.prefix) for name in edits):
        return {}
    status = util.git_records(["status", "--porcelain", "--no-renames", "--untracked-files=no",
                               "--ignore-submodules=all", "--", ".", *diffguard.NEVER_STAGED], c.wt)
    if all(entry[:3] in (" M ", " D ", " T ") for entry in status) and {entry[3:] for entry in status} == set(edits):
        return made
    return {}


def merge_moved_bases(run: AgentRun, checkouts: list[workspace.Checkout], layer: config.RepoLayer, ticket: tracker.Ticket,
                      log_prefix: str = "", reason: str = "since the base moved after the last push", *,
                      bases: dict[str, str] | None = None, allow_untracked: bool = False,
                      environment_retry: bool = False, retained_workspace: Path | None = None,
                      before_merge: Callable[[workspace.Checkout, str, list[str]], None] | None = None,
                      before_resolve: Callable[[], None] | None = None) -> list[workspace.Checkout]:
    merged = []
    for c in checkouts:
        base = (bases or {}).get(c.github) or git(["rev-parse", c.origin_base], c.wt).strip()
        if environment_retry and (not c.base_commit or
                                  git(["merge-base", c.base_commit, base], c.wt).strip() != c.base_commit):
            raise workspace.RunFailed(f"the base for {c.github} no longer contains the saved base")
        if git(["rev-list", "--count", f"HEAD..{base}"], c.wt).strip() == "0":
            if not pending_merge(c):
                c.base_commit = base
            continue
        c.base_commit = base
        head = git(["rev-parse", "HEAD"], c.wt).strip()
        edits = workspace.uncommitted(c.wt, tracked_only=allow_untracked)
        parked = parked_tracked_edits(retained_workspace, c, head, edits, layer) if edits and retained_workspace else {}
        if edits and not parked:
            raise workspace.RunFailed(f"{c.github} has uncommitted edits ({', '.join(edits[:5])}), so arete merged nothing into "
                            "them and left them as they are")
        if before_merge:
            before_merge(c, head, edits)
        with record.timed(run.id, "base_merge", repo=c.github, base=c.base, base_commit=c.base_commit,
                          head=head) as rec:
            try:
                try:
                    merge_as_run(run.id, c, c.base_commit, c.origin_base, overwrite_ignore=not allow_untracked)
                except RuntimeError as e:
                    conflicts = unmerged_paths(c.wt)
                    if not conflicts:
                        raise workspace.RunFailed(f"git could not merge `{c.origin_base}` into the branch in {c.github}: {e}")
                    if environment_retry:
                        raise workspace.RunFailed(f"merging `{c.origin_base}` into the held draft conflicts in {c.github}")
                    rec["conflicts"] = conflicts
                    if before_resolve:
                        before_resolve()
                    try:
                        retained = {"parked": (parked, edits)} if parked else {}
                        resolve_conflicts(run, c, conflicts, layer, ticket, **retained)
                    except workspace.RunFailed as e:
                        raise ConflictUnresolved(e, conflicts) from e
                else:
                    conflicts = []
                rec["outcome"] = "resolved" if conflicts else "clean"
                rec["merge"] = c.merge = git(["rev-parse", "HEAD"], c.wt).strip()
            except BaseException:
                abort_merge(c.wt)
                raise
        c.merged_base, c.resolved = True, conflicts
        c.resolved_lines = resolution_diff(c).lines
        run.log(f"{log_prefix}merged `{c.origin_base}` into the branch in {c.github}, "
                + (f"and resolved conflicts in {', '.join(conflicts)}" if conflicts
                   else reason))
        merged.append(c)
    return merged


def resolve_conflicts(run: AgentRun, c: workspace.Checkout, conflicts: list[str], layer: config.RepoLayer,
                      ticket: tracker.Ticket, incoming: tuple[str, str] | None = None, *,
                      parked: tuple[dict, list[str]] | None = None) -> None:
    deny = diffguard.denied_paths(layer)
    named, commit = incoming or (c.origin_base, c.base_commit)

    def denied(path: str) -> bool:
        return util.matches_any(path, deny) or util.matches_any(c.prefix + path, deny)

    if protected := [p for p in conflicts if denied(p)]:
        raise workspace.RunFailed(f"merging `{named}` conflicts in protected paths in {c.github} "
                        f"({', '.join(protected)}), which a person must merge")
    conflict_paths = set(conflicts)
    own = {p for p in util.git_records(["diff", "--name-only", f"{commit}...HEAD"], c.wt)
           if p not in conflict_paths and not denied(p)}
    incoming_paths = {p for p in util.git_records(["diff", "--name-only", f"HEAD...{commit}"], c.wt)
                      if p not in conflict_paths and not denied(p)}
    supplemental = own | incoming_paths
    added_paths = ["diff", "--no-renames", "--name-only", "--diff-filter=A"]
    branch_additions = set(util.git_records([*added_paths, f"{commit}...HEAD"], c.wt))
    branch_additions.intersection_update(util.git_records([*added_paths, commit, "HEAD"], c.wt))
    move_sources = {p for p in branch_additions & (own | conflict_paths) if os.path.lexists(c.wt / p)}
    bash_rules = layer.builder_bash
    initialize_resolver_scratch(c.wt, bash_rules)
    scratch_markers = verification_scratch_markers(c.wt)
    parent_tracked = restorable_scratch_paths(c.wt, commit, set(scratch_markers))
    literal_paths = {"GIT_LITERAL_PATHSPECS": "1"}
    tries = max(1, run.settings("build").tries)
    feedback = "this is the first attempt"
    restorations: set[str] = set()
    move_destinations: set[str] = set()
    for n in range(1, tries + 1):
        scratch_roots = set(scratch_markers)
        untracked_before = workspace.untracked_files(c.wt)
        ignored_before = resolver_ignored_paths(c.wt, parent_tracked)
        scratch_before = resolver_scratch_paths((untracked_before | ignored_before) - parent_tracked, scratch_roots)
        scratch_metadata = {name: resolver_scratch_metadata(c.wt, name) for name in scratch_before}
        watched_before = (ignored_before | (untracked_before & parent_tracked)) - scratch_before
        fingerprints = {n: workspace.fingerprint(c.wt / n, "") for n in watched_before}
        previous = run.sessions.get("build")
        text = prompt_loader.prompt("resolve", repo=run.repo.name, github=c.github, base=named,
                      base_commit=commit[:12], head=git(["rev-parse", "HEAD"], c.wt).strip()[:12],
                      ref=ticket.ref, title=ticket.title, body=ticket.body,
                      conflicts="\n".join(f"- `{p}`" for p in conflicts),
                      own="\n".join(f"- `{p}`" for p in sorted(own)) or "- none",
                      branch_log=git(["log", "--oneline", "-n30", "MERGE_HEAD..HEAD"], c.wt),
                      base_log=git(["log", "--oneline", "-n30", "HEAD..MERGE_HEAD"], c.wt),
                      commands=prompt_loader.code_list(bash_rules), attempt=n, tries=tries, feedback=feedback,
                      owner=run.owner_note, denied=prompt_loader.code_list(deny))
        try:
            res = run.agent(round_name("resolve", n), prompt_loader.task_prompt(text), c.wt, role="build",
                            allowed_tools=prompt_loader.bash_rules(bash_rules), disallowed_tools=routing.NO_GIT_WRITES,
                            json_schema=RESOLVE_SCHEMA, resume=NO_SESSION, extra_env=dict(RESOLVER_SETS))
        finally:
            if previous is None:
                run.sessions.pop("build", None)
            else:
                run.sessions["build"] = previous
        if not res.ok:
            raise workspace.RunFailed(f"the conflict resolution step failed: {res.error}", res.text,
                                      recovery=runner.start_failure(res, "build"))
        answer = res.structured if isinstance(res.structured, dict) else {}
        if answer.get("resolved") is not True:
            raise workspace.RunFailed(f"the resolver could not keep both the branch's work and the base's in {c.github}: "
                            f"{answer.get('summary') or 'no reason given'}", res.text)
        untracked_after = workspace.untracked_files(c.wt)
        ignored_after = resolver_ignored_paths(c.wt, parent_tracked)
        scratch_after = resolver_scratch_paths((untracked_after | ignored_after) - parent_tracked, scratch_roots)
        watched_after = (ignored_after | (untracked_after & parent_tracked)) - scratch_after
        new_untracked = untracked_after - untracked_before - scratch_after - parent_tracked
        for name in new_untracked.copy():
            if Path(name).parent.name == "__pycache__" and Path(name).suffix == ".pyc":
                workspace.remove_untracked(c.wt, name)
                new_untracked.remove(name)
        scratch_markers = {root: marker for root, marker in verification_scratch_markers(c.wt, scratch_roots).items()
                           if marker == scratch_markers[root]}
        changed_watched = watched_before ^ watched_after
        changed_watched.update(n for n in watched_before & watched_after
                               if fingerprints[n] != workspace.fingerprint(c.wt / n, ""))
        restorations = (restorations | changed_watched) & watched_after & parent_tracked
        invalidated_roots = scratch_roots - scratch_markers.keys()
        invalidated_before = resolver_scratch_paths(scratch_before, invalidated_roots)
        invalidated_after = resolver_scratch_paths(scratch_after, invalidated_roots)
        invalidated_edits = invalidated_before ^ invalidated_after
        invalidated_edits.update(name for name in invalidated_before & invalidated_after
                                 if scratch_metadata[name] != resolver_scratch_metadata(c.wt, name))
        tracked_edits = set(util.git_records(["diff", "--name-only"], c.wt))
        if parked:
            made, names = parked
            tracked_edits.difference_update(names)
            tracked_edits.update(n for n in names if not workspace.recorded(made, c.wt, n, c.prefix))
        move_candidates = (new_untracked | move_destinations) & (untracked_after - scratch_after - parent_tracked)
        move_candidates -= conflict_paths | supplemental | watched_before
        deleted_in_folder = Counter(Path(p).parent for p in tracked_edits & move_sources
                                    if not os.path.lexists(c.wt / p))
        move_destinations = set()
        for path in sorted(move_candidates):
            folder = Path(path).parent
            if deleted_in_folder[folder] and not path.endswith("/") and not denied(path):
                move_destinations.add(path)
                deleted_in_folder[folder] -= 1
        touched = tracked_edits | new_untracked | changed_watched | restorations | move_candidates
        if outside := sorted((touched - conflict_paths - supplemental - move_destinations) | invalidated_edits):
            raise workspace.RunFailed(f"the resolver edited files outside the conflict in {c.github}: {', '.join(outside)}")
        if left := marked(c.wt, [*conflicts, *sorted(move_destinations)]):
            feedback = f"conflict markers remain in {', '.join(left)}; remove every one"
            if n == tries:
                raise workspace.RunFailed(f"conflict markers remain in {', '.join(left)} after {util.count(n, 'attempt')} "
                                f"in {c.github}")
            continue
        git(["add", "-u", "--", *conflicts, *sorted(tracked_edits & supplemental)], c.wt, env=literal_paths)
        additions = (touched & supplemental) - tracked_edits - (parent_tracked - restorations)
        additions.update(move_destinations)
        ignored_restorations = additions & restorations & ignored_after
        if ignored_restorations:
            git(["add", "-f", "--", *sorted(ignored_restorations)], c.wt, env=literal_paths)
        if additions := additions - ignored_restorations:
            git(["add", "-A", "--", *sorted(additions)], c.wt, env=literal_paths)
        if unmerged_paths(c.wt):
            raise workspace.RunFailed(f"files are still unmerged in {c.github} after the resolver ran")
        with util.repo_lock(c.src):
            util.git_as_owner(["commit", "-q", "--no-edit"], c.wt)
        return


def merge_moved_bases_for_ci_fix(b: Build, watched: list[workspace.Checkout]) -> str:
    fetched = []
    for c in watched:
        try:
            workspace.fetch(c.src, c.base)
        except RuntimeError as e:
            b.run.log(f"ci: could not fetch `{c.origin_base}` in {c.github}, so the CI fix works on the branch as it "
                      f"is: {e}")
            continue
        fetched.append(c)
    merged = merge_moved_bases(b.run, fetched, b.layer, b.ticket, "ci: ")
    if not merged:
        return ""
    names = ", ".join(f"`{c.origin_base}` into `{c.prefix or './'}`" for c in merged)
    fixed = "".join(f" Arete resolved merge conflicts in {', '.join(f'`{p}`' for p in c.resolved)} of "
                    f"`{c.prefix or './'}`." for c in merged if c.resolved)
    return (f"Before this step arete merged {names}, since the base moved after the last push.{fixed} CI tested the PR "
            "merged with that base, so the worktree now holds what CI ran, and files the base added are yours to "
            "edit like any other.")


def run_folder(run: Run, name: str) -> Path:
    folder = run.dir / name
    folder.mkdir(exist_ok=True)
    if folder not in run.add_dirs:
        run.add_dirs.append(folder)
    return folder


def judge_ci(b: Build, reds: list[ci.Red], n: int, max_rounds: int, watched: list[workspace.Checkout],
             fix: CiFix | None = None) -> str:
    run = b.run
    try:
        merged_base = merge_moved_bases_for_ci_fix(b, watched)
    except workspace.RunFailed as e:
        for r in reds:
            r.reason = "Arete could not merge the moved base before judging this check."
        raise workspace.RunFailed(e.why, ci.red_list(reds))
    failures = ci_failure_notes(run, reds, f"round{n}")
    diff = ", ".join(f"`{c.diff_from_base}`" for c in b.checkouts)
    left = NOT_CAUSED_LEFT_FOR_A_PERSON + b.left_for_a_person if b.left_for_a_person else ""
    name = b.next_step("ci_fix")
    text = prompt_loader.prompt("ci_fix", round=n, max_rounds=max_rounds, failures=failures, diff=diff, checks=b.check_commands,
                  merged_base=merged_base, left_for_a_person=left, denied=prompt_loader.code_list(b.deny))
    res = fix(name, text, failures) if fix else b.resume(name, text, role="ci_repair", json_schema=CI_FIX_SCHEMA)
    if not res.ok or not isinstance(res.structured, dict):
        for r in reds:
            r.reason = "The CI fix step failed before it judged this check."
        raise workspace.RunFailed("the CI fix step failed", ci.red_list(reds))
    apply_ci_verdicts(run, reds, res, assume_caused=True)
    return ci.clip(res.structured.get("summary", ""), MAX_CI_REASON_CHARS)


def judge_publication_ci(b: Build, pushed: list[workspace.Checkout], reds: list[ci.Red], n: int) -> None:
    run = b.run
    try:
        patch = "\n".join(checkout_patch(c, [], f"{c.origin_base}...{c.lease}") for c in pushed)
        failures = ci_failure_notes(run, reds, f"publish-judge{n}")
        diff = util.both_ends(vault.hidden(patch), MAX_FIX_DIFF_IN_PROMPT)
        text = ("Judge whether the published PR caused each CI failure. Compare the failure evidence with the "
                "published diff below. Unrelated failures can include flaky tests, unavailable services, or failures "
                "also present on the base. Report a check as unrelated only when the evidence supports that verdict. "
                "Use the exact check headings below and give the reason for each verdict. Make no edits.\n\n"
                f"{failures}\n\n## Published diff\n\n```diff\n{diff}\n```")
        res = run.agent(b.next_step("ci_judge"), prompt_loader.task_prompt(text), b.ws, role="ci_repair",
                        read_only=True, persist=False, json_schema=CI_FIX_SCHEMA)
    except (RuntimeError, OSError, ValueError) as e:
        run.log(f"ci: could not judge the published checks: {util.first_line(str(e), 300)}")
        return
    if res.ok and isinstance(res.structured, dict):
        apply_ci_verdicts(run, reds, res)


def ci_failure_notes(run: Run, reds: list[ci.Red], prefix: str) -> str:
    logs = run_folder(run, "ci")
    return "\n\n".join(ci.failure_note(r, logs / f"{prefix}-{i}-{prompt_loader.slug(r.label)}.log")
                       for i, r in enumerate(reds, 1))


def apply_ci_verdicts(run: Run, reds: list[ci.Red], res: Result, *, assume_caused=False) -> None:
    checks = res.structured.get("checks", [])
    if not isinstance(checks, list):
        checks = []
    named = [v for v in checks if isinstance(v, dict) and isinstance(v.get("name"), str)]
    exact = exact_ci_verdicts(reds, named) if len(named) == len(checks) else {}
    repair = {name_key(v["name"]): v for v in named} if assume_caused else {}
    for i, r in enumerate(reds):
        rerun_verdict = exact.get(i, {})
        reason = rerun_verdict.get("reason")
        if rerun_verdict.get("caused_by_pr") is False and isinstance(reason, str) and reason.strip():
            r.rerun_reason = ci.clip(reason, MAX_CI_REASON_CHARS)
        v = rerun_verdict
        if assume_caused:
            v = repair.get(name_key(r.label)) or repair.get(name_key(r.check.name)) or {}
        verdict, reason = v.get("caused_by_pr"), v.get("reason")
        if not assume_caused and (type(verdict) is not bool or not isinstance(reason, str) or not reason.strip()):
            continue
        r.caused = verdict is not False
        r.reason = ci.clip(reason or "The fix step gave no verdict for this check.", MAX_CI_REASON_CHARS)
        run.log(f"ci: {r.label} {'caused' if r.caused else 'not caused'} by the PR: {util.first_line(r.reason, 200)}")


def exact_ci_verdicts(reds: list[ci.Red], verdicts: list[dict]) -> dict[int, dict]:
    exact: dict[str, set[int]] = {}
    for i, red in enumerate(reds):
        for name in (red.label, red.check.name):
            exact.setdefault(name, set()).add(i)
    matched = {}
    for verdict in verdicts:
        matches = exact.get(verdict["name"])
        if not matches:
            continue
        if len(matches) != 1:
            return {}
        index, = matches
        if index in matched:
            return {}
        matched[index] = verdict
    return matched


def name_key(name: str) -> str:
    return " ".join(name.lower().split())


def discard_edits(b: Build) -> None:
    for c in b.restage().changed:
        git(["reset", "-q", "--hard"], c.wt)
        b.run.log(f"ci: threw away the fix step's edits in {c.github}, since the PR caused no failure")


def ci_fix_message(b: Build, caused: list[ci.Red]) -> str:
    names = ", ".join(dict.fromkeys(r.check.name for r in caused))
    subject = prompt_loader.fill_template(b.layer.subject, config.RepoLayer.subject, kind="fix", id=b.ticket.id,
                            title=f"make {names} pass in CI")
    return f"{tracker.with_ref(subject, b.ticket.ref)}\n\nArete-Run: {b.run.id}"


def publish_ci(run: AgentRun, t: tracker.Ticket, pushed: list[workspace.Checkout], ci_section: str) -> None:
    links = (run.tracker.pr_link(t, whole=True), run.tracker.pr_link(t, whole=False))
    for c in pushed:
        try:
            kept, text = gh.split_link(gh.pr_body(c.pr), links)
            gh.pr_edit_body(c.pr, public_text(run, ci.with_section(text, ci_section)), link=kept)
        except gh.GhError as e:
            run.log(f"ci: could not write the CI section on {c.pr}: {util.first_line(str(e), 300)}")


def publish_gate_evidence(run: Run, t: tracker.Ticket, c: workspace.Checkout,
                          verified: verify.Verified | None, review: Review | None, *, held: str = "") -> None:
    checked = verified.markdown() if verified is not None else "Verification did not run."
    if held:
        checked = f"{held}\n\n{checked}"
    reviewed = review.markdown() if review is not None else "## Review\n\nReview did not run."
    evidence = {
        "checked": public_text(run, outbound.screen(checked)),
        "review": public_text(run, outbound.screen(reviewed)),
    }
    if observed := observed_measurements(run, verified):
        evidence["observed_requirements"] = observed
    target = run.repo.name, str(t.id), c.pr
    retrying = run_store.save_pr_gate_refresh(*target, run.id, c.lease, evidence)
    if retrying:
        run.log(f"automerge: retrying gate evidence refresh in {c.pr}")
    links = None if retrying else (run.tracker.pr_link(t, whole=True), run.tracker.pr_link(t, whole=False))
    if update_pr_gate_evidence(run, c.pr, evidence, links=links):
        run_store.clear_pr_gate_refresh(*target)


def update_pr_gate_evidence(run: Run, pr: str, evidence: dict, *, expected_head: str = "",
                            links: tuple[str, str] | None = None) -> bool:
    sections = ((CHECKED_IN_USE_HEADING, f"{CHECKED_IN_USE_HEADING}\n\n{evidence['checked']}"),
                ("## Review", evidence["review"]))
    try:
        if expected_head:
            _, head = gh.pr_draft_and_head(pr)
            if not head:
                raise gh.GhError("could not confirm the PR head")
            if head != expected_head:
                run.log(f"automerge: discarded pending gate evidence in {pr} because its head changed")
                return True
        body = gh.pr_body(pr)
        exact = links is None
        if links is not None:
            kept, body = gh.split_link(body, links)
        for heading, section in sections:
            if exact:
                section = gh.outgoing(public_text(run, section))
            body = replace_gate_section(body, heading, section.rstrip(), exact=exact)
        if observed := evidence.get("observed_requirements"):
            body = with_unverified_measurement(body, "", observed_requirements=observed)
        if exact:
            gh.pr_write_body(pr, gh.Outgoing(body))
        else:
            gh.pr_edit_body(pr, public_text(run, body.rstrip()), link=kept)
    except gh.GhError as e:
        run.log(f"automerge: could not update gate evidence in {pr}: {util.first_line(str(e), 300)}")
        return False
    return True


def replace_gate_section(body: str, heading: str, section: str, *, exact: bool = False) -> str:
    marker = "|".join(map(re.escape, (MEASUREMENT_START, ci.SECTION_START)))
    boundary = rf"^(?:## |(?:{marker})\r?$)"
    pattern = re.compile(rf"^{re.escape(heading)}\n.*?(?={boundary}|\Z)", re.M | re.S)
    start = 0
    for fence_start, fence_end in markdown_fences(body):
        if UNVERIFIED_REQUIREMENT_HEADING in body[fence_start:fence_end]:
            if found := pattern.search(body, start, fence_start):
                break
            start = fence_end
    else:
        found = pattern.search(body, start)
    if found:
        separator = "" if exact and found.end() == len(body) else "\n\n"
        return body[:found.start()] + section.rstrip() + separator + body[found.end():]
    if exact:
        return f"{body}\n\n{section}" if body else section
    return f"{body.rstrip()}\n\n{section}"


def comment_catch_up_ci(run: AgentRun, c: workspace.Checkout, ci_section: str,
                        verified: verify.Verified, review: Review) -> None:
    key = "catch-up-evidence"
    text = (f"## Catch-up evidence for head `{c.lease}`\n\n"
            f"{CHECKED_IN_USE_HEADING}\n\n{verified.markdown()}\n\n{review.markdown()}\n\n{ci_section}")
    try:
        body = public_text(run, text)
        comment = gh.find_own_pr_comment(c.github, gh.pr_number(c.pr), gh.key_mark(key))
        if comment:
            gh.pr_edit_comment(c.github, int(comment["id"]), body, key=key)
        else:
            gh.pr_comment(c.pr, body, key=key)
    except gh.GhError as e:
        run.log(f"ci: could not comment on {c.pr} for head {c.lease}: {util.first_line(str(e), 300)}")


def images_note(images: tracker.SavedImages) -> str:
    rows = []
    if images.paths:
        rows += ["## Images on the issue", "", "Open each one with Read; they show what the issue describes.", ""]
        rows += [f"- `{p}`" for p in images.paths]
    if images.left_out:
        rows += ["", (f"Not downloaded, over the {tracker.MAX_IMAGES} image or "
                      f"{tracker.MAX_IMAGE_BYTES // 2**20} MB cap: {', '.join(images.left_out)}")]
    return "\n".join(rows).strip()


def open_branch(run: Run) -> str:
    return next((r["branch"] for r in run_store.open_runs(run.repo.name, run.tid) if r.get("branch")), "")


def with_siblings(body: str, section: str) -> str:
    return SIBLINGS.sub("", body).rstrip() + section


def sibling_section(ref: str, c: workspace.Checkout, with_prs: list[workspace.Checkout]) -> str:
    others = [o for o in with_prs if o is not c]
    if not others:
        return ""
    return f"\n\n## Other PRs for {ref}\n\n" + "\n".join(f"- [{o.pr_name}]({o.pr})" for o in others)


def supersede_older_runs(run: Run, urls: list[str]) -> None:
    for r in run_store.open_runs(run.repo.name, run.tid):
        older = (r.get("pr_url") or "").split()
        if older and set(older) <= set(urls) and record.move(r["id"], "superseded", expect=(r["status"],)):
            run.log(f"run {r['id']} superseded: its PRs carry on in this run")


def pending_feedback(repo_name: str, tid: str) -> list[dict]:
    return insights_store.open_feedback([url for r in run_store.open_runs(repo_name, tid) for url in (r.get("pr_url") or "").split()])


def feedback_key(row: dict) -> tuple[str, str, int]:
    return row["pr_url"], row["kind"], row["gh_id"]


def feedback_markdown(rows: list[dict]) -> str:
    out = []
    for url in dict.fromkeys(f["pr_url"] for f in rows):
        out.append(f"### On {url}")
        for f in (f for f in rows if f["pr_url"] == url):
            if f["kind"] == "line":
                where = f"`{f['path']}`" + (f" line {f['line']}" if f.get("line") else "")
                in_thread = f", a reply in thread {f['thread']}" if f["thread"] != f["gh_id"] else ""
                out.append(f"#### Line comment {f['gh_id']} on {where}{in_thread}\n\n```diff\n{f['hunk']}\n```\n\n"
                           f"{f['body'].strip()}")
            elif f["kind"] == "review":
                out.append(f"#### Review summary\n\n{f['body'].strip()}")
            else:
                out.append(f"#### Comment on the PR\n\n{f['body'].strip()}")
    return "\n\n".join(out)


def answer_feedback(run: Run, replies: list[dict], fallback: str) -> None:
    by_comment = {r["comment_id"]: r["reply"].strip() for r in replies if r.get("reply", "").strip()}
    threads: dict[tuple[str, int], str] = {}
    for f in run.feedback:
        if f["kind"] == "line":
            key = (f["pr_url"], f["thread"])
            threads[key] = by_comment.get(f["gh_id"]) or by_comment.get(f["thread"]) or threads.get(key) or fallback
    for (url, thread), text in threads.items():
        try:
            gh.pr_reply(url, thread, public_text(run, text))
        except gh.GhError as e:
            run.log(f"reply to comment {thread} on {url} failed: {util.first_line(str(e), 300)}")


def answer_without_changes(run: Run, ws: Path, checkouts: list[workspace.Checkout], branch: str, summary: str,
                           replies: list[dict]) -> tuple[str, str, str]:
    if run.dry_run:
        run.log("revise: no code changes; a dry run leaves the review unanswered")
        release_workspace(run, ws, checkouts)
        return "dry-run", "no code changes for the review", ""
    run.log("revise: no code changes, answering the review")
    answer_feedback(run, replies, "No code change for this one; see the overall reply.")
    run.say(f"No code changes for your review.\n\n{cut_summary(summary)}")
    run.enter("pr")
    urls = record_prs(run, branch, [c for c in checkouts if c.pr])
    release_workspace(run, ws, checkouts)
    return "pr", "answered the review without code changes", urls


def _review(b: Build, plan: str, verified: verify.Verified, step: str = "review", before: Review | None = None,
            reply: str = "", *, catch_up: workspace.Checkout | None = None,
            skill: bool = False, spec: str = "") -> Review:
    if skill:
        from . import code_review

        return code_review.run_axes(b, util.tail(diffguard.fold_moved(b.staged.patch), MAX_DIFF_IN_PROMPT),
                                    index_trees(b.checkouts), spec)
    before = before or Review.stored({})
    if catch_up is None:
        earlier = before.blocking()
        schema = REVIEW_SCHEMA
        text = b.brief.prompt(
            "review", plan=plan, checks=b.report, evidence=verified.markdown(), compare=compare_with_fork(b.checkouts),
            diff=util.tail(diffguard.fold_moved(b.staged.patch), MAX_DIFF_IN_PROMPT),
            earlier=earlier_review(earlier, reply, fixed_since(b, before.trees or {})))
        context = nullcontext((text, {}))
    else:
        earlier = []
        schema = CATCH_UP_REVIEW_SCHEMA
        context = catch_up_review_prompt(b, catch_up, plan, verified, before)
    allowed_tools = prompt_loader.bash_rules(b.read_bash)
    with context as (text, patches):
        for _ in range(2):
            res = b.run.agent(step, text, b.ws, role="review", allowed_tools=allowed_tools, json_schema=schema)
            if res.ok and isinstance(res.structured, dict) and isinstance(res.structured.get("problems"), list):
                said = res.structured
                if catch_up is not None:
                    said = {**said, "problems": catch_up_problems(said["problems"], patches)}
                return Review.judged(said, earlier, before.waived or [], index_trees(b.checkouts))
    return Review.failed_step(earlier, before.waived or [], before.trees or {})


@contextmanager
def catch_up_review_prompt(b: Build, c: workspace.Checkout, plan: str, verified: verify.Verified,
                           before: Review) -> Iterator[tuple[str, dict[str, str]]]:
    fork = git(["merge-base", c.lease, c.base_commit], c.wt).strip()
    diff_config = ["-c", "diff.suppressBlankEmpty=false"]
    flags = ("--no-color", "--no-ext-diff", "--no-textconv", "--submodule=short", "--unified=3", "--no-relative",
             "--src-prefix=a/", "--dst-prefix=b/", "--find-renames", "-l0")
    comparisons = {
        "approved": ["diff", *flags, fork, c.lease, "--"],
        "base": ["diff", *flags, fork, c.base_commit, "--"],
        "resolution": ["show", "--remerge-diff", "--format=", *flags, c.merge, "--"] if c.resolved else [],
    }
    raw = {part: util.git_bytes(diff_config + args, c.wt).decode(errors="replace") if args else ""
           for part, args in comparisons.items()}
    patches = permitted_review_patches(raw, diffguard.denied_paths(b.layer), c.prefix)
    with ExitStack() as files:
        excerpts, sources = {}, []
        for part, patch in patches.items():
            redacted = vault.hidden(patch)
            excerpts[part] = util.both_ends(redacted, MAX_FIX_DIFF_IN_PROMPT) or "None."
            if len(redacted) <= MAX_FIX_DIFF_IN_PROMPT:
                continue
            patch_file = files.enter_context(NamedTemporaryFile(mode="w", encoding="utf-8", dir=b.ws,
                                                                prefix=f".arete-{part}-", suffix=".diff"))
            patch_file.write(redacted)
            patch_file.flush()
            sources.append(f"The {part} excerpt is truncated. Read the full patch at "
                           f"`{Path(patch_file.name).name}` using the read-only tools.")
        text = b.brief.prompt("review_catch_up", plan=plan, checks=b.report, evidence=verified.markdown(),
                              checkout=c.dir or ".", approved_head=c.lease, fork=fork, base_head=c.base_commit,
                              merged_head=c.merge,
                              earlier=vault.hidden(json.dumps(before.asdict(), indent=2)), sources="\n".join(sources),
                              **excerpts)
        yield text, patches


def earlier_review(earlier: list[dict], reply: str, fixed: str) -> str:
    if not earlier:
        return ""
    return prompt_loader.prompt("review_earlier", blockers=numbered(earlier), reply=util.tail(reply.strip(), MAX_FIX_REPLY_IN_PROMPT) or NO_FIX_REPLY,
                  fixed=fixed or NO_FIX_DIFF)


def index_trees(checkouts: list[workspace.Checkout]) -> dict[str, str]:
    try:
        return {c.prefix: git(["write-tree"], c.wt).strip() for c in checkouts}
    except RuntimeError:
        return {}


def fixed_since(b: Build, trees: dict[str, str]) -> str:
    parts = []
    for c in b.checkouts:
        if not trees.get(c.prefix):
            continue
        try:
            parts.append(checkout_patch(c, [n for n in b.staged.locks if n.startswith(c.prefix)], "--cached",
                                        trees[c.prefix]))
        except RuntimeError:
            continue
    return util.tail(vault.hidden("".join(parts)), MAX_FIX_DIFF_IN_PROMPT)


def verify_in_use(b: Build, goal: str, summary: str) -> verify.Verified:
    if not b.layer.verify:
        b.run.log("verify: the layer has no [verify] section, so nothing checks the change in use")
        return verify.not_configured(b.layer.dir, b.ws)
    tries = b.run.settings("verify").tries
    for attempt in range(1, tries + 1):
        verified = check_in_use(b, goal, summary)
        if verified.verdict != "fail":
            return verified
        b.run.story.append(f"The check in use failed on attempt {attempt}:\n{util.tail(verified.markdown(), 2500)}")
        if attempt == tries:
            break
        repair_in_use(b, attempt + 1, tries, verified)
        b.recheck("fixing what the check in use found")
    b.run.story.append("Still failing in use after the last attempt.")
    _learn(b.run, b.ws, b.layer.expertise_file, b.ticket.title, apply=False)
    raise workspace.RunFailed(f"it still does not work in use after {util.count(tries, 'attempt')}",
                    verified.markdown())


@runlimits.verification
def repair_in_use(b: Build, attempt: int, tries: int, verified: verify.Verified) -> None:
    b.resume(b.next_step("verify_fix"), prompt_loader.prompt("verify_fix", attempt=attempt, max_attempts=tries,
                                               failures=verified.markdown(), checks=b.check_commands,
                                               denied=prompt_loader.code_list(b.deny)),
             role="check_repair")


@runlimits.verification
def check_in_use(b: Build, goal: str, summary: str, *, covered_commands: tuple[str, ...] = ()) -> verify.Verified:
    return use_product(b, "verify", "verify", goal=goal, summary=summary.strip()[:4000], covered_commands=covered_commands,
                       diff=util.tail(diffguard.fold_moved(b.staged.patch), MAX_DIFF_IN_PROMPT))


def use_product(b: TicketWork, step_name: str, prompt_name: str, role: str = "verify",
                require_released: bool = False, *, replay_drivers: set[str] | None = None,
                covered_commands: tuple[str, ...] = (), **values) -> verify.Verified:
    from . import pilot, readme_verify, zte

    run, configured = b.run, b.layer.verify
    replay_only = replay_drivers is not None
    delivery_verification = prompt_name == role == "verify"
    tid = str(run.tid) if delivery_verification else ""
    zte_verify = delivery_verification and zte.reserved(tid)
    readme_task = (delivery_verification and pilot.is_pilot_task(tid)) or zte_verify
    feature_map = configured.feature_map if delivery_verification and not readme_task else {}
    requested = journeys.requested(feature_map, b.ticket.title, b.ticket.body)
    step = b.next_step(step_name)
    if unknown := [name for name in requested if name not in feature_map]:
        return save_verification(run, step, verify.Verified(
            "blocked", "An issue references a journey absent from the configured feature map.", finished=False,
            blocked_by=", ".join(f"journey:{name}" for name in unknown)))
    selected = {name: feature_map[name] for name in requested}
    if covered_commands:
        configured = replace(configured, probes={name: cmd for name, cmd in configured.probes.items()
                                                 if name not in covered_commands})
        selected = {name: journey for name, journey in selected.items() if f"journey:{name}" not in covered_commands}
    drivers = {name: journey.driver for name, journey in selected.items() if journey.driver}
    replay = {name: journey.driver for name in replay_drivers or ()
              if (journey := configured.feature_map.get(name)) and journey.driver in configured.drivers}
    commands = {name: configured.drivers[name] for name in (*drivers.values(), *replay.values())}
    v = verify.with_free_ports(replace(configured, drivers=commands))
    merge_candidate = b.layer.trust_level == "automerge" or readme_task
    checked_trees = verification_trees(b) if delivery_verification and merge_candidate else {}
    if refusal := verify.port_refusal(v):
        run.log(f"{step}: {refusal}")
        return verify.Verified("blocked", "Verification cannot use a port the live dashboard serves.",
                               blocked_by=refusal, finished=False)
    if ready := verify.preflight(v, b.ws, b.cfg, run.repo):
        run.save(f"{step}-preflight.json", json.dumps(asdict(ready), indent=2))
        run.log(f"{step}: preflight {ready.verdict}")
        if ready.verdict == "blocked":
            ready.saved = os.path.abspath(run.dir / f"{step}-preflight.md")
            run.save(f"{step}-preflight.md", ready.markdown())
            return ready
    if readme_task and not (v.start or v.probes):
        try:
            if zte_verify:
                grant, policy = zte.for_run(run.repo, tid, run.id, "running")
                signal = policy.signal(grant.base)
            else:
                signal = pilot.Signal(**pilot.live_receipt(tid, run.repo.name)["signal"])
            pilot.require(checked_trees.keys() == {""}, "the README verifier requires one exact candidate tree")
            verified = readme_verify.check(run, b.ws, step, signal, checked_trees[""])
            if verified.verdict == "pass":
                pilot.require(checked_trees == verification_trees(b), "the candidate tree changed during verification")
                verified.native_trees = checked_trees
        except pilot.Held as error:
            verified = verify.Verified("blocked", "The exact README could not be verified.", finished=False,
                                       blocked_by=str(error))
        return save_verification(run, step, verified)
    if replay_only:
        evidence = run.dir / f"{step}-evidence"
        evidence.mkdir(exist_ok=True)
    else:
        evidence = run_folder(run, "evidence")
    untracked_before = untracked_by_dir(b.git_dirs)
    attempt = None
    try:
        attempt = scratch.make(run.id, step, b.ws, run.dir)
    except (OSError, ValueError) as e:
        reason = util.first_line(str(e), 300)
        run.log(f"{step}: trusted scratch unavailable: {reason}")
        if drivers:
            names = ", ".join(f"journey:{name}" for name in drivers)
            return save_verification(run, step, verify.Verified(
                "blocked", f"Configured drivers require trusted scratch confinement for {names}.",
                blocked_by=reason, finished=False))
    agent_started, agent_finished = False, False
    lifetime = {"stopped": True}
    fixture_blocker = ""
    probes = None
    try:
        if attempt:
            run.log(f"{step}: scratch folder {attempt.root}")
            v = verify.with_scratch(v, attempt.root)
        else:
            run.log(f"{step}: ordinary verification without trusted scratch authority")
            if verify.SCRATCH_FIELD in v.start:
                fixture_blocker = "Required service needs unavailable scratch fixtures for start command: " + json.dumps(v.start)
                v = replace(v, start="", url="", notes=v.notes + "\n" + fixture_blocker)
        bash = b.read_bash + v.bash
        with verify.serving(
                v, b.ws, run.dir / f"{step}-server.log", b.cfg, run.repo, lifetime=lifetime,
                attempt=attempt) as url:
            if url:
                run.log(f"{step}: the app answers at {url}")
            if v.probes or drivers:
                probe_config = replace(v, probe_seconds=run.time_for(v.probe_seconds))
                probes = verify.run_probes(probe_config, b.ws, attempt, journey_drivers=drivers)
            if probes:
                run.save(f"{step}-probes.json", json.dumps(asdict(probes), indent=2))
                label = "configured verification commands" if drivers else "configured probes"
                run.log(f"{step}: {label} {probes.verdict}")
                if probes.verdict == "pass":
                    name = "verification commands" if drivers else "probes"
                    instructions = ("Check any remaining acceptance criteria. Arete adds the configured results to the "
                                    "report. Return only the checks you performed.\n\n"
                                    if drivers else "Check only acceptance criteria not covered by them.\n\n")
                    v = replace(v, notes=v.notes + f"\n\nArete already executed these owner-configured {name} in a "
                                "confined disposable workspace. Use their observed results as evidence; do not repeat "
                                "these commands. " + instructions + probes.markdown())
            probes_passed = not probes or probes.verdict == "pass"
            if replay and probes_passed:
                driver_config = replace(v, probes={}, probe_seconds=run.time_for(v.probe_seconds))
                observed = verify.run_probes(driver_config, b.ws, attempt, journey_drivers=replay,
                                             stop_on_driver_failure=False)
                run.save(f"{step}-replay-drivers.json", asdict(observed))
            if not replay_only and probes_passed:
                agent_started = True
                fault_controls = verify.fault_note(v) if url and delivery_verification else ""
                res = run.agent(step, b.brief.prompt(
                    prompt_name, surface=surface_note(v, url, evidence) + fault_controls, notes=v.notes,
                    scratch=scratch.note(attempt),
                    commands=prompt_loader.code_list(bash), checks=check_list(b.layer),
                    env_example=verify.env_example(v.allow_bash, url) if prompt_name == "verify" else "",
                    compare=compare_with_fork(b.checkouts), owner=run.owner_note,
                    journeys=journeys.guidance(selected if requested or covered_commands else feature_map), **values),
                                b.ws, role=role,
                                allowed_tools=prompt_loader.bash_rules(bash) + ([f"mcp__playwright__{t}" for t in verify.PLAYWRIGHT_TOOLS] if url else []),
                                disallowed_tools=routing.NO_GIT_WRITES, json_schema=verify.SCHEMA,
                                mcp_config=browser.mcp_config(evidence) if url else "",
                                scratch=attempt)
                agent_finished = True
    except (owner_asks.Asked, CatchUpNeedsInput):
        agent_finished = True
        raise
    except sandbox.ConfinementUnavailable as e:
        return verify.Verified("blocked", "Trusted fixture verification could not start.", finished=False,
                               blocked_by=util.first_line(str(e), 300))
    except verify.ServerError as e:
        run.log(f"{step}: the app did not start")
        if fault := verify.environment_fault(str(e)):
            return verify.Verified("blocked", "The app could not start in the verifier's sandbox.",
                                   blocked_by=f"the app could not start in the verifier's sandbox ({fault}); "
                                              "the start log stays in the run folder", finished=False,
                                   behavior_reached=False)
        return verify.Verified("fail", f"The app did not start: {e}", behavior_reached=False)
    finally:
        try:
            if require_released and not lifetime["stopped"]:
                run.save(f"{step}-retained.json", {"reason": "service process ownership is unresolved"})
            if not attempt:
                archived, moved, kept = "", [], []
            elif not lifetime["stopped"] or (agent_started and not agent_finished):
                scratch.retain(attempt)
                archived, moved, kept = "", [], [attempt.root]
            else:
                archived, moved, kept = scratch.end(attempt, run.dir)
            if moved:
                run.log(f"{step}: preserved {util.count(len(moved), 'fixture')} from the scratch folder at "
                        f"{archived}")
            if kept:
                fixture_blocker = "Verification scratch resources remain unresolved; the workspace is retained."
                run.log(retained_line(step, attempt))
        finally:
            removed = restore_worktrees(untracked_before, evidence)
    if probes and probes.verdict != "pass":
        verified = probes
    elif replay_only:
        verified = verify.Verified("pass", "No ordinary steps to replay.")
    elif not res.ok or not isinstance(res.structured, dict):
        verified = verify.Verified("blocked", "The check in use did not finish.", finished=False,
                                   blocked_by=util.first_line(res.error or "the verifier returned no verdict", 300))
        if not drivers:
            return save_verification(run, step, verified, removed) if removed else verified
    else:
        verified = verify.Verified.from_agent(res.structured)
    if probes and probes.verdict == "pass" and not replay_only:
        verified.checks = probes.checks + verified.checks
    if fixture_blocker:
        verified.verdict, verified.blocked_by, verified.finished = "blocked", fixture_blocker, False
    measured = verified.verdict == "pass" or (getattr(run, "code_review", False) is True and verified.verdict == "blocked"
                                            and any(c.get("passed") is True for c in verified.checks))
    if (measured and checked_trees and agent_finished and res.runner in {"pi", "codex", "claude"}
            and checked_trees == verification_trees(b)):
        verified.native_trees = checked_trees
    return save_verification(run, step, verified, removed if agent_started else 0)


def save_verification(run: Run, step: str, verified: verify.Verified, removed: int = 0) -> verify.Verified:
    if removed:
        cleanup = (f"Arete removed {util.count(removed, 'untracked file')} that verification added to the worktree, "
                   "after verification ended. None is left behind.")
        verified.summary = "\n\n".join(filter(None, (verified.summary, cleanup)))
    verified.saved = os.path.abspath(run.dir / f"{step}.md")
    run.save(f"{step}.md", verified.markdown())
    run.log(f"{step}: {verified.verdict}, {len(verified.checks) - len(verified.failed_checks)} of "
            f"{util.count(len(verified.checks), 'check')} passed")
    return verified


def verification_trees(b: TicketWork) -> dict[str, str]:
    trees = {c.prefix: factory.fingerprint(c.wt) for c in b.checkouts}
    return trees if len(trees) == len(b.checkouts) and all(trees.values()) else {}


def surface_note(v: config.Verify, url: str, evidence: Path) -> str:
    if not url:
        return ("Arete started no app for this repo. Run the commands its user runs, with real arguments, and read "
                "the output and the exit code.")
    return (f"Arete started the app with `{v.start}` and it answers at {url}. Arete stops it when you finish. The "
            "browser opens only that address and refuses every other port, so a server you start yourself answers "
            "curl but never loads in the browser: give a browser check the state it needs through the app at that "
            "address.\n\n"
            "- Do in the browser what a person does in the browser, with these `mcp__playwright__` tools (load "
            f"them with ToolSearch first): {', '.join(f'`{t}`' for t in verify.PLAYWRIGHT_TOOLS)}. Read the page "
            "with `browser_snapshot`; no tool runs your own script in the page.\n"
            "- After each state you check, wait until the page settles (loading done, entrance animations finished) "
            "so the shot shows what a person sees, then call `browser_take_screenshot` with `filename` set to an "
            f"absolute path in `{evidence}`, such as `{evidence}/01-list.png`.\n"
            "- For a file chooser, click the control that opens it, then call `browser_file_upload` with `paths` set "
            f"to absolute paths of files in `{evidence}`; empty `paths` closes the chooser. Arete's guard refuses a "
            "path outside that folder, a path that leaves it through a symlink and any secret file, and the browser "
            f"server reads only its own folders, so copy a worktree file into `{evidence}` first. A screenshot you "
            "saved there also serves as a fixture.\n"
            "- Read `browser_console_messages` after each state. Name each console error and each dev error overlay "
            "or badge in `saw`, and say whether the change causes it.\n"
            "- For ordinary API checks, send requests with curl, shaped the way the app's clients send them, "
            "and read the status and body.")


def restore_worktrees(untracked_before: dict[Path, set[str]], evidence: Path) -> int:
    view = workspace.scratch_view()
    removed = 0
    for d, name in untracked_added_since(untracked_before):
        f = d / name
        if workspace.record_holds(view, d) or any(p.startswith(paths.SCRATCH_PREFIX) for p in Path(name).parts):
            continue
        if f.suffix == ".png" and not (evidence / f.name).exists():
            shutil.move(f, evidence / f.name)
        else:
            existed = f.exists(follow_symlinks=False)
            workspace.remove_untracked(d, name)
            removed += existed
    for d in untracked_before:
        if not workspace.record_holds(view, d):
            git(["checkout", "-q", "--", "."], d)
    return removed


def evidence_images(run: Run, c: workspace.Checkout, verified: verify.Verified) -> dict[str, str]:
    if run.layer and run.layer.verify and not run.layer.verify.publish_evidence:
        run.log("evidence: publication disabled by the layer; screenshots stay in the run folder")
        return {}
    shots = verified.screenshots(run.dir / "evidence")
    if not shots:
        return {}
    try:
        with util.repo_lock(c.src):
            images = verify.push_evidence(c.src, c.github, run.id, shots)
    except RuntimeError as e:
        run.log(f"evidence: the push failed, so the PR shows no screenshots: {util.first_line(str(e), 300)}")
        return {}
    run.log(f"evidence: pushed {util.count(len(shots), 'screenshot')} to {verify.EVIDENCE_BRANCH} on {c.github}")
    return images


def draft_reason(review: Review, verified: verify.Verified, reviews: int = 0) -> str:
    approved = review.approved()
    refused = REFUSAL_WORDS.search(f"{verified.blocked_by} {verified.summary}")
    if verified.verdict == "blocked" and (not approved or refused and not verified.nonblocking_reason):
        return "the check in use was blocked"
    if approved:
        return ""
    if review.verdict == "request_changes" and reviews:
        left = len(review.blocking())
        if review.failed:
            return (f"the last review failed after {util.count(reviews, 'review')}, with {util.count(left, 'earlier blocker')} "
                    f"not rechecked")
        return (f"the review still requested changes after {util.count(reviews, 'review')}, the most the review "
                f"step's tries allow, so {util.count(left, 'blocker')} {'is' if left == 1 else 'are'} left unresolved")
    return "the review did not approve it"


def draft_note(draft_why: str, verified: verify.Verified, kept: bool = False, pushed: bool = False) -> str:
    blocked_by = f" {verified.blocked_by}" if verified.verdict == "blocked" else ""
    lead = "The PR stays a draft" if kept else "The PRs are drafts" if pushed else "The new PRs are drafts"
    return f"\n\n{lead}: {draft_why}.{blocked_by}".rstrip() if draft_why else ""


def check_list(layer: config.RepoLayer) -> str:
    return ", ".join(f"`{c}`" for c in layer.checks.values()) or NO_CHECKS


def check_calls(commands: list[str]) -> str:
    return prompt_loader.code_list(commands) or NO_CHECKS


def class_check_example(folder: Path, layer: config.RepoLayer) -> str:
    if "classes" not in layer.targeted_commands:
        return ""
    return shlex.join([str(folder / CHECKS_SCRIPT), "classes", "test_x.SomeTests"])


def class_check_rule(folder: Path) -> str:
    return f"{shlex.quote(str(folder / CHECKS_SCRIPT))} classes *"


def check_script(folder: Path, layer: config.RepoLayer, cwd: Path) -> tuple[list[str], list[str]]:
    classes = layer.targeted_commands.get("classes")
    if not layer.checks and classes is None:
        return [], []
    script = folder / CHECKS_SCRIPT
    names = list(layer.checks)
    arms = "".join(f"{shlex.quote(name)}) exec sh -c {shlex.quote(cmd)} ;;\n" for name, cmd in layer.checks.items())
    if classes is not None:
        names.append("classes")
        command = shlex.join(config.class_command_words(classes))
        class_usage = shlex.quote(f"usage: {script} classes CLASS [CLASS ...]")
        arms += ("classes)\n"
                 f"    usage() {{ echo {class_usage} >&2; exit 2; }}\n"
                 '    shift\n'
                 '    [ "$#" -gt 0 ] || usage\n'
                 '    for arg in "$@"; do\n'
                 '        case "$arg" in ""|-*) usage ;; esac\n'
                 '    done\n'
                 f'    exec {command} "$@" ;;\n')
    usage = shlex.quote(f"usage: {script} NAME, where NAME is one of: {', '.join(names)}")
    folder.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!/bin/sh\ncd {shlex.quote(str(cwd))} || exit 1\ncase \"${{1-}}\" in\n{arms}"
                      f"*) echo {usage} >&2\n   exit 2 ;;\nesac\n")
    script.chmod(0o755)
    rule = f"{shlex.quote(str(script))} *" if layer.checks else class_check_rule(folder)
    return [shlex.join([str(script), name]) for name in layer.checks], [rule]


def cut_at_end(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    room = max(0, limit - len(CUT_MARK))
    head = text[:room + 1]
    ends = (head.rfind("\n\n"), max(head.rfind(f"{p}{s}") + 1 for p in ".!?" for s in " \n"), head.rfind(" "))
    at = next((at for at in ends if room // 2 < at <= room), room)
    return head[:at].rstrip() + CUT_MARK


Learned = learning.Rewrite


def layer_lock(layer_dir: Path) -> str:
    return "layer-" + hashlib.sha256(str(layer_dir.resolve()).encode()).hexdigest()[:16]


def expertise_now(expertise_file: Path) -> str:
    return expertise_file.read_text().strip() if expertise_file.exists() else ""


def learned(run: AgentRun, wt: Path, title: str, expertise: str, call: str, **kw) -> Learned | None:
    res = run.agent(call, prompt_loader.prompt("learn", repo=run.repo.name, ref=run.ref, title=title,
                                  story="\n\n".join(run.story), expertise=expertise or "(none yet)",
                                  max_lines=EXPERTISE_MAX_LINES),
                    wt, role="learn", json_schema=LEARN_SCHEMA, persist=False, **kw)
    found = res.structured if res.ok and isinstance(res.structured, dict) else {}
    try:
        return learning.parse(found, expertise)
    except (TypeError, ValueError) as error:
        run.log(f"learn: {error}; kept the old file")
        return None


def _learn(run: AgentRun, wt: Path, expertise_file: Path, title: str, apply: bool) -> str:
    if not run.story:
        run.log("learn: clean run, nothing to learn")
        return ""
    base = expertise_now(expertise_file)
    first = learned(run, wt, title, base, "learn")
    if first is None:
        return ""
    if not apply:
        if first.rewrite == expertise_now(expertise_file):
            run.log("learn: expertise unchanged")
            return ""
        run.save("expertise-report.md", first.report)
        run.save("expertise.md", first.rewrite + "\n")
        run.log(f"learn: rewrite saved in the run, not applied ({first.added} added, {first.removed} removed)")
        return ""
    return apply_learned(run, wt, expertise_file, title, base, first)


def apply_learned(run: AgentRun, wt: Path, expertise_file: Path, title: str, base: str, first: Learned) -> str:
    with util.lock(layer_lock(expertise_file.parent), wait=True):
        current = expertise_now(expertise_file)
        chosen = first
        if current != base:
            run.log("learn: expertise.md changed since this run read it, so the learn step runs again on the current file")
            chosen = learned(run, wt, title, current, round_name("learn", 2), wait_out_limits=False)
            if chosen is None:
                run.save("expertise-report.md", first.report)
                run.save("expertise.md", first.rewrite + "\n")
                run.log("learn: the second pass gave no usable file, so the first rewrite is saved in the run, not applied")
                return ""
        if chosen.rewrite == current:
            run.log("learn: expertise unchanged")
            return ""
        run.save("expertise-report.md", chosen.report)
        expertise_file.parent.mkdir(parents=True, exist_ok=True)
        expertise_file.write_text(chosen.rewrite + "\n")
        run.log(f"learn: {chosen.added} added, {chosen.removed} removed, {len(chosen.rewrite.splitlines())} lines in "
                f"{paths.short_path(expertise_file)}")
        if not run.repo.layer:
            return ""
        try:
            sha = commit_written(expertise_file.parent, f"{run.repo.name}: learn from {run.ref} ({run.id})\n\n{chosen.report}",
                                 [expertise_file])
        except RuntimeError as e:
            run.log(f"learn: wrote the file but the layer commit failed: {util.first_line(str(e), 300)}")
            return ""
        if sha:
            run.save("expertise-commit.txt", sha + "\n")
            run.log(f"learn: committed {sha[:12]} in {paths.short_path(expertise_file.parent)}")
        return sha


def commit_written(layer_dir: Path, message: str, written: list[Path]) -> str:
    if git(["rev-parse", "--is-inside-work-tree"], layer_dir, check=False).strip() != "true":
        git(["init", "-q"], layer_dir)
    names = [str(p.relative_to(layer_dir)) for p in written]
    git(["add", "--", *names], layer_dir)
    if not util.sh(["git", "diff", "--cached", "--quiet", "--", *names], cwd=layer_dir).returncode:
        return ""
    util.git_as_owner(["commit", "-q", "-m", message, "--", *names], layer_dir)
    return git(["rev-parse", "HEAD"], layer_dir).strip()


def commit_layer(layer_dir: Path, message: str, written: list[Path]) -> str:
    with util.lock(layer_lock(layer_dir), wait=True):
        return commit_written(layer_dir, message, written)


def sync_labels(cfg: config.Config) -> None:
    seen: set[tuple[str, str]] = set()
    for r in run_store.runs(kind="pipeline", limit=200):
        if r.get("label_pending") is None:
            continue
        key = (r["repo"] or "", str(r["issue"]))
        if key in seen:
            continue
        seen.add(key)
        want = r["label_pending"]
        if not want or r["status"] == "running" or util.lock_is_held(run_store.run_lock("pipeline", *key)):
            continue
        if r["status"] == "interrupted":
            record.note(r["id"], label_pending="")
            continue
        try:
            repo = cfg.repo(r["repo"])
        except KeyError:
            continue
        tk = tracker.for_ticket(cfg, repo, key[1])
        err = tk.label(key[1], add=[want], remove=[s for s in tk.states if s != want])
        if err:
            print(f"{util.iso()} {tracker.run_ticket_name(r)}: could not label it {want}, trying next poll: "
                  f"{util.first_line(err, 300)}")
            continue
        record.note(r["id"], label_pending="")
        print(f"{util.iso()} {tracker.run_ticket_name(r)}: labeled {want}")


class SkipLog:
    def __init__(self, before: dict):
        self.before = before
        self.current: dict[str, dict] = {}

    def note(self, name: str, t: tracker.Ticket, blocker: str, repo: str = "") -> None:
        old = self.before.get(name) or {}
        same = old.get("why") == blocker
        self.current[name] = {"why": blocker, "title": t.title, "url": t.url, "repo": repo, "id": str(t.id),
                              "since": old.get("since") if same else util.iso()}
        if not same:
            print(f"{util.iso()} {name}: not run, {blocker}")

    def keep_repo(self, repo: str) -> None:
        self.keep_earlier(lambda name, v: v.get("repo") == repo)

    def keep_project(self, project: str) -> None:
        self.keep_earlier(lambda name, v: not v.get("repo") and name.startswith(f"{project}-"))

    def keep_earlier(self, belongs: Callable[[str, dict], bool]) -> None:
        for name, v in self.before.items():
            if belongs(name, v):
                self.current.setdefault(name, v)


def settle_abandoned_runs(cfg: config.Config) -> None:
    record.reap(tickets=True, then=lambda row: tell_ticket_run_died(cfg, row))


def retained_line(step: str, attempt: scratch.Attempt) -> str:
    line = f"{step}: retained unresolved scratch at {attempt.root}, and with it the worktree"
    held = scratch.named_holders(scratch.holding(attempt))
    return f"{line}; held by {held}" if held else line


def retry_scratch(days: float = 30) -> None:
    pending_refresh: set[str] = set()
    for root in workspace.finalize_scratch(pending_refresh=pending_refresh):
        print(f"preserved and finalized verification scratch {root}")
    for data in scratch.records():
        if data["state"] in ("pending", "retained") and data["diagnostic"]:
            print("verification scratch retained: " + json.dumps({"run": data["attempt"]["run_id"],
                  "attempt": data["attempt"]["selector"], "diagnostic": data["diagnostic"]}, ensure_ascii=True))
    try:
        for u in scratch.survey().unreadable:
            print(f"verification scratch record unreadable: {u.path}: {u.reason}")
    except OSError as e:
        print(f"verification scratch store unreadable: {e}")
    try:
        for failure in scratch.settle(days, pending_refresh=pending_refresh):
            print(f"verification scratch settle failed: {failure}")
    except sandbox.ConfinementUnavailable:
        pass
    except OSError as e:
        print(f"verification scratch settle failed: {e}")


def tell_ticket_run_died(cfg: config.Config, row: dict) -> None:
    from . import environment_gaps, zte

    if zte.reserved(str(row["issue"])):
        try:
            with run_lines.nonblocking_appends(lambda: None):
                run_lines.write(row["id"], "error", message="Reserved README run ended without an active child; it will not be retried.")
        except (OSError, run_lines.AppendRefused):
            pass
        return
    repo = cfg.repo(row["repo"])
    run = Run(row["id"], repo, paths.RUNS_DIR / row["id"], tracker=tracker.for_ticket(cfg, repo, str(row["issue"])),
              tid=str(row["issue"]), feedback=pending_feedback(repo.name, str(row["issue"])),
              alone=bool(row.get("only_step")), dry_run=bool(row.get("dry_run")),
              spec_root=spec_store.find(repo.name, str(row["issue"])).get("root", ""))
    run.dir.mkdir(parents=True, exist_ok=True)
    if row["status"] != "stopped":
        run_lines.write(row["id"], "error", step=run_lines.last_step(row["id"]), message=row.get("summary") or "")
    write_report(row)
    if run.alone:
        run.log(f"{record.STOPPED if row['status'] == 'stopped' else record.INTERRUPTED}, a run of "
                f"{row['only_step']} alone, so the ticket stays as it was")
    elif row["status"] == "stopped":
        run.log(record.STOPPED)
        run.give_up("was stopped by request")
    elif row["status"] == "failed":
        run.log(f"{record.INTERRUPTED}, the second interruption in a row")
        run.give_up(f"{record.INTERRUPTED}, the second interruption in a row")
    elif environment_gaps.was_retry(row):
        run.enter("pr")
        run.say(f"Run `{run.id}` was interrupted during the automatic verification retry. Check the PR before resuming it.")
    else:
        run.log(record.INTERRUPTED)
        run.leave("running")
        run.say(f"Run `{run.id}` {record.INTERRUPTED}; I will start it again.")


def live_pipeline_runs() -> list[dict]:
    return slots.holding(run_store.runs_marked_running(), "pipeline")


def held_back(cfg: config.Config, repo_name: str, going: Counter, free_slots: int) -> str:
    if going[repo_name] >= cfg.max_parallel_per_repo:
        return (f"{repo_name} already has {util.count(going[repo_name], 'run')} going "
                f"(max_parallel_per_repo {cfg.max_parallel_per_repo})")
    return slots.no_free_slot(cfg.max_parallel) if free_slots <= 0 else ""


def spawn_run(repo_name: str, tid: str, force: bool = False, *, spec: bool = False) -> subprocess.Popen:
    return control.launch(run_command(repo_name, tid, force, **({"spec": True} if spec else {})), RUNS_LOG)


def run_skipped(name: str) -> dict:
    from . import tasks

    entry = skipped_tickets().get(name)
    if not entry:
        raise control.NotFound(f"{name} is not on the skipped list; the last poll either ran it or no longer skips it")
    if not entry.get("repo"):
        raise control.Refused(f"{name} names no repo to run in; a ticket the pipeline routes to a repo gets one at "
                              "the next poll")
    cfg = config.load()
    with tasks.PENDING_LOCK, util.lock(tasks.START_LOCK, wait=True):
        if run_store.issue_running(entry["repo"], entry["id"]):
            raise control.Busy(name)
        if not tasks.pending_start(entry["repo"], str(entry["id"])):
            if slots.slots_free_to_start(cfg.max_parallel) <= 0:
                raise slots.NoFreeSlot(f"{name} was not started: {slots.no_free_slot(cfg.max_parallel)}; wait for a run "
                                      "to finish or raise max_parallel in Settings")
            _, state = tasks.spawned(entry["repo"], str(entry["id"]),
                                     lambda: spawn_run(entry["repo"], entry["id"], force=True))
            if state == tasks.SLOT_QUEUE_FAILED:
                repo = cfg.repo(entry["repo"])
                return {"ticket": name, "id": entry["id"],
                        **tasks.queued(repo, str(entry["id"]), tasks.NO_SLOT_AT_START, repo.pipeline)}
            if state == "exited":
                raise control.Refused(f"{name} ended before it showed as running; the end of "
                                      f"{paths.short_path(paths.LOG_DIR / RUNS_LOG)} says why")
    return {"ticket": name, "repo": entry["repo"], "id": entry["id"]}


def answered_run(repo_name: str, tid: str) -> dict | None:
    newest = run_store.newest_live_whole_run(repo_name, tid)
    if (newest and newest["status"] == "needs-info" and asks_store.asks_due([newest["id"]])
            and not asks_store.asks_of(newest["id"], "open")):
        return newest
    return None


def skipped_tickets() -> dict:
    return util.read_json(SKIPPED_FILE, {})
