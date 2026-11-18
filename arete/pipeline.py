from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TextIO

from . import (
    alerts,
    browser,
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
    learning,
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
from .agent import NO_SESSION, Result, Session
from .review import REVIEW_SCHEMA, Review, numbered
from .store import asks as asks_store
from .store import insights as insights_store
from .store import runs as run_store
from .store import sources as source_store
from .store import triage as triage_store
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
CHECKS_LOCK = "checks"
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
RUNS_LOG = "runs.log"
RUN_START_SECONDS = 10
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

    def agent_as_step(self, step: str, text: str, cwd: Path, role: str = "", **kw) -> Result:
        s = self.settings(role) if role else None
        if self.budget:
            self.budget.charge(step, s.step if s else role)
        if s:
            reach = s.agent_kw(self.config, kw.pop("allowed_tools", ()), kw.pop("mcp_config", ""))
            kw = {**s.call, **reach,
                  "resume": self.sessions.get(config.INHERITS.get(s.step, s.step), NO_SESSION)
                  if s.session == "resume" else NO_SESSION, **kw}
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
                        dropped = res
                        self.log(f"{step}: the network is back, the dropped attempt cost {runner.cost_note(res)}")
                        if res.session.id and res.turns > 1 and kw.get("persist", True):
                            res = runner.run(CONTINUE_AFTER_DROP, cwd, trace=(self.id, step),
                                             **{**kw, "resume": res.session, **self.retry_kw()})
                        else:
                            res = runner.run(text, cwd, trace=(self.id, step), **{**kw, **self.retry_kw()})
                        res = runner.with_spent(res, [dropped])
            return res
        finally:
            if s:
                record.label_step(self.id, step, attempt_no, atom=s.step, ab=s.ab_name, ab_side=s.ab_side)

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


@dataclass(kw_only=True)
class Run(AgentRun):
    tracker: tracker.Tracker
    tid: str
    force: bool = False
    feedback: list[dict] = field(default_factory=list)
    answering: dict = field(default_factory=dict)
    path: str = ""
    alone: bool = False
    facts: dict = field(default_factory=dict)
    repairs: str = ""
    action: str = ""
    action_only: bool = False
    workspace: tuple[Path, list[workspace.Checkout]] | None = None
    asks_made: int = 0
    answers_due: list[dict] = field(default_factory=list)
    base_note: str = ""
    kept_note: str = ""

    @property
    def ref(self) -> str:
        return self.tracker.ref(self.tid)

    @property
    def ab_ticket(self) -> tuple[str, str] | None:
        return (self.repo.name, str(self.tid))

    @property
    def asks_owner(self) -> bool:
        return factory.inside(self.id) is not None

    @property
    def owner_note(self) -> str:
        return owner_asks.GUIDE if self.asks_owner else owner_asks.NOBODY

    def agent(self, step: str, text: str, cwd: Path, role: str = "", **kw) -> Result:
        if not self.asks_owner:
            return super().agent(step, text, cwd, role, **kw)
        due = self.due_for(step)
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
            parts = (owner_asks.answers_message(due), self.kept_note, self.base_note,
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

    @property
    def failed_state(self) -> str:
        return "pr" if self.feedback or self.repairs else "failed"

    @property
    def retry_hint(self) -> str:
        if self.feedback:
            return (f"Comment on the PR again to retry: a submitted review starts at once, a comment after "
                    f"{FEEDBACK_QUIET_MINUTES} quiet minutes")
        if self.repairs:
            return f"Continue it with `arete resume {self.id}`"
        return f"Remove `{self.tracker.label_name('failed')}` to retry"

    def enter(self, status: str) -> None:
        if self.dry_run:
            return
        others = [s for s in self.tracker.states if s != status]
        labeled = self.tracker.label(self.tid, add=[status], remove=others)
        record.note(self.id, label_pending=status if labeled else "")
        moved = self.tracker.move(self.tid, status)
        for err in (labeled, moved):
            if err:
                self.log(f"tracker: {util.first_line(err, 300)}")
        if not moved and isinstance(self.tracker, tracker.JiraTracker) and self.tracker.cfg.transitions.get(status):
            record.note(self.id, moved=status)

    def leave(self, status: str) -> None:
        if not self.dry_run and (err := self.tracker.label(self.tid, remove=[status])):
            self.log(f"tracker: {util.first_line(err, 300)}")

    def say(self, body: str) -> str:
        if self.dry_run:
            return ""
        body = public_text(self, body)
        err = self.say_on_prs(body) if self.feedback else self.tracker.comment(self.tid, f"{tracker.ARETE_TAG} {body}")
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

    def give_up(self, why: str) -> None:
        if self.alone:
            return
        self.enter(self.failed_state)
        self.say(f"Run `{self.id}` {why}. {self.retry_hint}.")


def workspace_owner(run: Run, ws: Path, checkouts: list[workspace.Checkout]) -> dict | None:
    earlier = workspace.owner_in(workspace.made_before(ws))
    if run.alone:
        return earlier
    created = earlier.get("created") if earlier else None
    return {"run": run.id, "repo": run.repo.name, "ticket": str(run.tid), "created": created or util.iso(),
            "checkouts": [{"dir": c.dir, "github": c.github, "src": str(c.src), "wt": str(c.wt)} for c in checkouts]}


def release_workspace(run: Run, ws: Path, checkouts: list[workspace.Checkout]) -> bool:
    workspace.note_made(ws, checkouts, workspace_owner(run, ws, checkouts))
    if workspace.remove_workspace(ws, checkouts):
        return True
    names = workspace.blocked_by(ws, checkouts)
    shown = ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")
    run.log(f"workspace kept at {ws}: removal blocked by {shown or 'the folder could not be deleted'}")
    return False


def commit_kept(run_id: str, c: workspace.Checkout, layer: config.RepoLayer | None) -> None:
    head = git(["rev-parse", "HEAD"], c.wt).strip()
    workspace.drop_untracked_bytecode(c.wt)
    diffguard.add(c.wt)
    deny = diffguard.denied_paths(layer) if layer else diffguard.ALWAYS_DENY
    holding = vault.held(git(["diff", *vault.SCAN, "--cached", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/"],
                             c.wt)) if vault.every() else {}
    if denied := [n for n in util.git_records(["diff", "--cached", "--name-only"], c.wt)
                  if util.matches_any(n, deny) or util.matches_any(c.prefix + n, deny) or n in holding]:
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

    @property
    def empty(self) -> bool:
        return not self.files and not self.merged


def stage(checkouts: list[workspace.Checkout], deny: list[str], from_base: bool = False) -> Staged:
    out = Staged()
    for c in checkouts:
        workspace.drop_untracked_bytecode(c.wt)
        against = git(["merge-base", c.origin_base, "HEAD"], c.wt).strip() if from_base else (c.onto or "HEAD")
        diff = diffguard.stage(c.wt, deny, against=against, prefix=c.prefix)
        merged = pending_merge(c)
        if merged and not from_base:
            brought = diffguard.between(c.wt, c.lease, c.merge, prefix=c.prefix).counts
            diff.counts = {name: 0 for name in brought} | diff.counts
        if merged:
            out.merged.append(c)
        elif not diff.counts:
            continue
        shown = checkout_patch(c, diff.locks, "--cached", against)
        resolving = bool(merged and c.resolved and not from_base)
        resolved = resolved_patch(c) if resolving else ""
        if vault.every():
            whole = checkout_patch(c, [], *vault.SCAN, "--cached", against)
            diff.denied += vault.refusals(whole + (resolved_patch(c, *vault.SCAN) if resolving else ""))
        out += diff
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


def lines_by_file(staged: Staged, top: int = 12) -> str:
    rows = sorted(((name, n) for name, n in staged.counts.items() if n), key=lambda r: (-r[1], r[0]))
    out = [f"- `{name}`: {n}" for name, n in rows[:top]]
    if len(rows) > top:
        out.append(f"- {len(rows) - top} more files: {sum(n for _, n in rows[top:])}")
    return "\n".join(out)


def ignored_state(wt: Path) -> dict[str, str]:
    names = util.git_records(["ls-files", "--others", "--ignored", "--exclude-standard"], wt)
    return {n: workspace.fingerprint(wt / n, "") for n in names
            if not (Path(n).parent.name == "__pycache__" and Path(n).suffix == ".pyc")}


def untracked_by_dir(dirs: list[Path]) -> dict[Path, set[str]]:
    return {d: workspace.untracked_files(d) for d in dirs}


def untracked_added_since(before: dict[Path, set[str]]) -> list[tuple[Path, str]]:
    return [(d, name) for d, names in before.items() for name in sorted(workspace.untracked_files(d) - names)]


def check_lanes(layer: config.RepoLayer, run: AgentRun) -> list[str]:
    if not layer.parallel_checks:
        return [CHECKS_LOCK]
    return [f"{CHECKS_LOCK}-{n}" for n in range(run.config.max_parallel_checks)]


def take_free_lane(files: list[TextIO]) -> bool:
    for f in files:
        if util.flock_now(f):
            util.mark_holder(f, os.getpid())
            return True
    return False


@contextmanager
def check_lane(layer: config.RepoLayer, run: AgentRun):
    with ExitStack() as stack:
        files = [stack.enter_context(util.lock_file(name)) for name in check_lanes(layer, run)]
        if not take_free_lane(files):
            run.log("checks: waiting for another run's checks to finish" if len(files) == 1 else
                    f"checks: waiting for a free check lane (max_parallel_checks {len(files)})")
            with run.waiting("slot"):
                while not take_free_lane(files):
                    time.sleep(CHECK_LANE_POLL_SECONDS)
        yield


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


def run_checks(layer: config.RepoLayer, cwd: Path, git_dirs: list[Path], run: AgentRun,
               timeout: float, *, selected: set[str] | None = None) -> tuple[bool, str]:
    commands = layer.checks
    if selected is not None:
        available = layer.checks | layer.targeted_commands
        if missing := selected - available.keys():
            raise ValueError(f"unknown selected checks: {', '.join(sorted(missing))}")
        commands = {name: command for name, command in available.items() if name in selected}
    if not commands:
        return True, "no checks configured"
    quick = [(name, commands[name]) for name in layer.quick_checks if name in commands]
    slow = [(name, command) for name, command in commands.items() if name not in layer.quick_checks]
    untracked_before = untracked_by_dir(git_dirs)
    report, ok = [], True

    def execute(items):
        nonlocal ok
        for name, cmd in items:
            with record.timed(run.id, "check", name=name, command=cmd) as check:
                res = environment.sh(cmd, run.config, run.repo, cwd=cwd, timeout=run.time_for(timeout))
                check["exit"] = res.returncode
            passed = res.returncode == 0
            ok = ok and passed
            run.log(f"check {name}: {'pass' if passed else f'FAIL (exit {res.returncode})'}")
            if not passed:
                report.append(f"### {name} failed: `{cmd}` exit {res.returncode}\n```\n{tail_of_both_streams(res.stdout, res.stderr, 6000)}\n```")
            else:
                report.append(f"### {name} passed: `{cmd}`")

    try:
        with nullcontext() if layer.parallel_checks else check_lane(layer, run):
            execute(quick)
            if slow and ok:
                with check_lane(layer, run) if layer.parallel_checks else nullcontext():
                    execute(slow)
            elif slow:
                report.append("Not run because cheap checks failed: " + ", ".join(name for name, _ in slow))
        if selected is not None and set(commands) != set(layer.checks):
            report.append("Targeted pass only; the full suite is still required before publication.")
    finally:
        for d, name in untracked_added_since(untracked_before):
            workspace.remove_untracked(d, name)
    return ok, "\n\n".join(report)


class RetryNextPoll(Exception):
    pass


class Stopped(BaseException):
    pass


def stop_on_term(run_id: str) -> Callable:
    def stop(*_) -> None:
        if record.stop_asked(run_id):
            raise Stopped(record.STOPPED)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
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
    from_base: bool = False
    plan_text: str = ""
    build_summary: str = ""

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
            return prompt_loader.task_prompt(prompt)

        text = prompt_loader.task_prompt(text)
        if settings.session == "fresh" or (saved.id and saved.runner != settings.runner and settings.explicit_route):
            kw["resume"] = NO_SESSION
        return self.run.agent(step, text, self.ws, role=role, allowed_tools=self.tools,
                              disallowed_tools=routing.NO_GIT_WRITES, fresh_prompt=primed, add_dirs=call_dirs, **kw)

    def check(self, *, quick: bool = False) -> bool:
        previous, self.checked = self.checked, {}
        self.report = "Checks started but did not complete; no passing result from this attempt."
        try:
            self.run.save("checks.md", self.report)
            selected = selected_checks(self.layer, self.restage().files) if quick else None
            full = selected is None or selected == set(self.layer.checks)
            before = factory.fingerprint(self.ws)
            ok, self.report = run_checks(self.layer, self.ws, self.git_dirs, self.run, self.timeout, selected=selected)
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
        except BaseException as e:
            self.checked = {}
            self.report = f"Checks did not complete: {type(e).__name__}: {util.first_line(str(e), 300)}"
            raise
        finally:
            self.run.save("checks.md", self.report)

    def restage(self, *, from_base: bool | None = None) -> Staged:
        self.staged = stage(self.checkouts, self.deny, self.from_base if from_base is None else from_base)
        record.mark(self.run.id, "diff", files=self.staged.files, lines=self.staged.lines, denied=self.staged.denied,
                    patch=self.staged.patch)
        return self.staged

    def require_valid_diff(self, after: str, planned: str = "") -> None:
        staged = self.restage()
        if staged.empty:
            raise workspace.RunFailed(f"{after} undid every change")
        earlier = 0 if self.from_base else self.committed_lines + sum(c.resolved_lines for c in self.checkouts)
        why = staged.refusal(self.layer.max_diff_lines, earlier=earlier)
        if staged.denied:
            raise workspace.RunFailed(f"{after} {why}")
        if why:
            pushed = f"\n- pushed earlier in this run: {earlier}" if earlier else ""
            raise workspace.RunFailed(f"after {after} {why}{planned}; split the work",
                            f"### Lines changed by file\n\n{lines_by_file(staged)}{pushed}")

    def push(self, branch: str, message: str) -> None:
        self.run.tracker.pr_link(self.ticket, whole=not self.left_for_a_person)
        commit_and_push(self.checkouts, self.staged.changed, branch, message)
        self.committed_lines += self.staged.lines

    def check_with_repairs(self, step_name: Callable[[int], str] | None = None) -> tuple[bool, int]:
        step_name = step_name or (lambda n: self.next_step("check_repair"))
        attempts, tries = 1, self.run.settings("check_repair").tries
        ok = self.check()
        while not ok and attempts < tries:
            self.run.story.append(f"Checks failed on attempt {attempts}:\n{util.tail(self.report, 2500)}")
            attempts += 1
            self.resume(step_name(attempts),
                        prompt_loader.prompt("fix", attempt=attempts, max_attempts=tries, failures=self.report,
                               checks=self.check_commands), role="check_repair")
            ok = self.check()
        return ok, attempts

    def recheck(self, after: str) -> None:
        if not self.check_with_repairs()[0]:
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

    @property
    def run_fields(self) -> dict:
        return {"path": self.path, "alone": bool(self.only or self.action_only), "step_overrides": self.overrides,
                "action": self.action, "action_only": self.action_only}


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
                      "from arete import codebase; codebase.load(); "
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


def dispatch_path(repo: config.Repo, tid: str) -> str:
    newest = run_store.newest_whole_run(repo.name, tid)
    if newest and newest["status"] == "interrupted":
        return recorded_path(newest["id"])
    if pending_feedback(repo.name, tid) and (source := next(iter(run_store.open_runs(repo.name, tid)), None)):
        return recorded_path(source["id"])
    return wanted(repo)


def run_command(repo_name: str, tid: str | int, force: bool = False, dry_run: bool = False) -> list[str]:
    cfg = config.load()
    flags = [*(["--force"] if force else []), *(["--dry-run"] if dry_run else [])]
    mode = dispatch_path(cfg.repo(repo_name), str(tid))
    if (asked := answered_run(repo_name, str(tid))) and not dry_run:
        mode = recorded_path(asked["id"])
        if mode != "classic":
            return script_command(cfg, mode, repo_name, str(tid), *flags, "--continue", asked["id"])
    if mode == "classic":
        return control.command(["run", repo_name, str(tid), "--local", *flags])
    if mode in (ORCHESTRATED, "orchestrate"):
        from .store import orchestration

        newest = run_store.newest_whole_run(repo_name, str(tid))
        source = newest if newest and newest["status"] == "interrupted" else (
            next(iter(run_store.open_runs(repo_name, str(tid))), None)
            if not force and pending_feedback(repo_name, str(tid)) else None)
        previous = (orchestration.find(source["id"]) or {}).get("current_run", source["id"]) if source else ""
        return script_command(cfg, "orchestrate", repo_name, str(tid), *flags,
                              *(["--continue", previous] if previous else []))
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
    parent = (script.repairs or script.continues if script else "") or (
        earlier["id"] if earlier and earlier["status"] not in ENDED else "")
    if budget_history and budget_history[0]["status"] not in ENDED:
        parent = budget_history[0]["id"]
    return parent


def park_run(run: Run, reason: str) -> tuple[str, str, str]:
    keep_edits(run)
    held = kept_heads(run)
    status, summary = "failed", (f"parked: {reason}; branch and evidence retained, workspace left in place"
                                 f"{', edits kept in a local commit' if held else ''}. {runlimits.RETRY}")
    kept = run.budget.park(reason, held)
    record.note(run.id, **kept)
    pr_url = kept["pr_url"]
    run.log(summary)
    run.enter("failed")
    return status, summary, pr_url


def settle_run(run: Run, status: str, summary: str, pr_url: str, restarting: bool, flow: factory.Flow | None) -> None:
    if restarting and status in ("skipped", "noop") and not run.feedback:
        run.enter("failed")
        run.say(f"I did not restart this after the interruption: {summary}. Remove "
                f"`{run.tracker.label_name('failed')}` to retry.")
    if run.feedback and not run.dry_run and not run.alone:
        insights_store.settle_feedback(run.id, {"pr": "done", "noop": "recorded"}.get(status, "failed"))
    if status == "failed":
        if flow and flow.matching and flow.stepped:
            flow.route()
        if not run_lines.ended_in_error(run.id, reply="respond" if flow else ""):
            run_lines.write(run.id, "error", step=run_lines.last_step(run.id), message=summary)
    pr_url = pr_url or str((run_store.run_get(run.id) or {}).get("pr_url") or "")
    end_line = f"done: {status} {pr_url or summary}"
    run.log(end_line)
    record.settle(run.id, status, end_line=end_line, summary=summary, pr_url=pr_url)
    write_report(run_store.run_get(run.id) or {"id": run.id, "status": status, "pr_url": pr_url}, flow, run)


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
    from . import flows
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
    if not script:
        adw = dispatch_path(repo, str(tid)) != "classic"
    if adw and not script:
        return exec_script(repo.name, tid, dry_run, force)
    if script and (refusal := script_refusal(cfg, repo, tid, script, dry_run)):
        print(refusal)
        return 2
    name = tracker.ticket_name(repo.name, tk.ref(tid))
    lock = run_store.run_lock("pipeline", repo.name, tid)
    with util.lock(lock) as got:
        if not got:
            print(f"{name} is already running")
            return 0
        try:
            layer = config.load_layer(repo.layer_dir)
            budget_history = run_store.ticket_runs(repo.name, tid, limit=1)
            blocked, reset_budget = runlimits.admission(repo, str(tid), tk, runlimits.Policy.of(layer), force)
        except tracker.ERRORS as e:
            print(f"could not read {name}: {e}")
            return 2
        except (ValueError, OSError) as e:
            print(f"could not validate the process budget: {e}")
            return 2
        if blocked and not dry_run:
            print(blocked)
            return 2
        if script and script.repairs and (later := run_store.later_run(repo.name, tid, script.repairs)):
            print(f"run {later['id']} of {name} came after run {script.repairs}, so it is not repaired twice")
            return 1
        with slots.admitting():
            slot = slots.RunSlot(repo.name, str(tid), cfg.max_parallel)
            only = (script.only or (script.action if script.action_only else "")) if script else ""
            due = asks_store.asks_due([script.continues]) if script and script.continues and not only else []
            if not dry_run:
                if slots.slots_free_to_start(cfg.max_parallel) > 0:
                    slot.hold()
                elif not due:
                    print(f"{name} was not started: {slots.no_free_slot(cfg.max_parallel)}. Wait for a run to "
                          f"finish or raise max_parallel in {paths.short_path(paths.CONFIG_FILE)}.")
                    return 1
            settled = [done for dead_row in run_store.ticket_runs_marked_running(repo.name, tid)
                       if (done := record.interrupt(dead_row))]
            earlier = None if only else run_store.newest_whole_run(repo.name, tid)
            restarting = bool(earlier) and earlier["status"] == "interrupted"
            mending = (script.repairs or repaired_by(script.continues or (earlier["id"] if restarting else ""))
                       ) if script else ""
            run_id = record.start("pipeline", tag=f"{repo.name}-{tid}", lock=lock, repo=repo.name, issue=tid,
                                  only_step=only)
            scratch.RUN_BASELINES[run_id] = producer_baseline
            scratch.RUN_LABELS[run_id] = service_labels
            run = Run(run_id, repo, paths.RUNS_DIR / run_id, dry_run, tracker=tk, tid=tid, force=force,
                      feedback=[] if mending else pending_feedback(repo.name, tid), repairs=mending,
                      answering=earlier if earlier and earlier["status"] == "needs-info" else {},
                      launch_gate=admission, launch_done=launch_done, closed_context=closed_context,
                **(script.run_fields if script else {}))
            run.dir.mkdir(parents=True, exist_ok=True)
            run_lines.write(run.id, "execution", path=run.path or "classic", protocol=1 if run.path == "orchestrate" else 0)
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
        opening_waits(run_id, run.answering,
                      "" if dry_run or run.alone else source_store.take_passed_over(repo.name, str(tid)))
        if restarting:
            run.log(f"restarting after run {earlier['id']} was interrupted")
        flow = (flows.start(cfg, run, earlier if restarting else {}, slot, script.continues, script.only,
                            script.again) if script else None)
        if run.feedback:
            run.log(f"revising {', '.join(run.feedback_prs)} for {util.count(len(run.feedback), 'comment')}")
            if not dry_run and not run.alone:
                insights_store.take_feedback(run.feedback, run_id)
        signal.signal(signal.SIGTERM, stop_on_term(run_id))
        pr_url, passed = "", None
        try:
            if due and not slot.held and not dry_run and not flow.refusal:
                slot.take(run.log, run.waiting, "answer")
            parent = parent_run(script, earlier, budget_history)
            previous = "" if reset_budget else parent
            fresh = bool(previous) and force and not restarting and not run.repairs and not run.alone and not (
                flow and flow.continues)
            current = tk.get(tid) if fresh else None
            run.budget = runlimits.Budget(run, runlimits.Policy.of(layer), previous, current, fresh)
            with run.budget.watching():
                if not (flow and flow.refusal):
                    run.budget.capture(current or tk.get(tid))
                status, summary, pr_url = flows.driven(flow, script.drive) if script else _run(cfg, run, slot)
            if flow and flow.only:
                status, summary, passed = flows.alone(flow)
            elif run.action_only:
                passed = status == "step"
        except runlimits.Parked as e:
            status, summary, pr_url = park_run(run, str(e))
        except factory.OnlyDone:
            status, summary, passed = flows.alone(flow)
        except factory.Refused as e:
            status, summary = "failed", str(e)
        except Stopped:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            status, summary, pr_url = "stopped", record.STOPPED, ""
            run.log(record.STOPPED)
            run.give_up("was stopped by request")
        except workspace.RunFailed as e:
            status, summary, pr_url = _fail(run, e.why, detail=e.detail)
        except tracker.ERRORS as e:
            status, summary, pr_url = _fail(run, f"tracker refused {run.ref}: {e}")
        except Exception as e:
            run.log(f"crashed: {type(e).__name__}: {e}")
            status, summary, pr_url = "failed", f"crashed: {e}"[:300], ""
            run.give_up("crashed before finishing")
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            if run.workspace:
                keep_edits(run)
                workspace.note_made(*run.workspace, workspace_owner(run, *run.workspace))
            slot.free()
        settle_run(run, status, summary, pr_url, restarting, flow)
        if status == "skipped":
            print(f"{name} was not run: {summary}. Pass --force to run it anyway.")
        return 0 if (status in SUCCEEDED if passed is None else passed) else 1


def _fail(run: Run, why: str, builder_said: str = "", detail: str = "") -> tuple[str, str, str]:
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


def opening_waits(run_id: str, answering: dict, passed_over: str) -> None:
    now = util.iso()
    if answering:
        asked = next((line.get("ts") for line in reversed(run_lines.read(answering["id"]))
                      if line.get("event") == "question"), None) or answering.get("finished")
        if asked:
            run_lines.waited(run_id, "person", asked, passed_over or now)
    if passed_over:
        run_lines.waited(run_id, "slot", passed_over, now)


def run_minutes(row: dict) -> float:
    if not (row.get("started") and row.get("finished")):
        return 0
    return round((util.parse_iso(row["finished"]) - util.parse_iso(row["started"])).total_seconds() / 60, 1)


def _ask(run: Run, ws: Path, checkouts: list[workspace.Checkout], comment: str, summary: str,
         asked: list[dict] | None = None, session: dict | None = None) -> tuple[str, str, str]:
    for ask in asked or [{}]:
        run_lines.write(run.id, "question", question=comment, summary=summary[:300], **ask)
    if asked and session is not None and not run.dry_run:
        for ask in asked:
            asks_store.ask_add(ask["ask_id"], run.id, run.repo.name, run.tid, ask["step"], ask["call"],
                          ask.get("why", ""), ask.get("tried", ""), ask["questions"], session)
        run.say(outbound.screen(comment))
        run.enter("needs-info")
        devices.release(run.id, run.log)
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
    repo, tk = run.repo, run.tracker
    try:
        t = intake.route_ticket(cfg, repo, tk, tk.get(run.tid), dry_run=run.dry_run, log=run.log)
    except tracker.ERRORS as e:
        return "failed", f"could not read {run.ref}: {e}", ""
    run_lines.write(run.id, "task", ticket=t.ref, kind=t.kind, repo=repo.name, title=t.title)
    if run.answering:
        run_lines.write(run.id, "answer", asked_in=run.answering["id"], question=run.answering.get("summary") or "",
                    text=answer_text(run.answering["id"]))
    approval = triage_store.triage_approval(run.triage_key)
    if run.feedback:
        closed = [url for url in run.feedback_prs if gh.pr_view(url).get("state") != "OPEN"]
        if closed:
            return "noop", f"the PR closed before arete could revise it: {', '.join(closed)}", ""
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
    branch = open_branch(run) or prompt_loader.fill_template(main_layer.branch, config.RepoLayer.branch, kind=t.kind, id=t.id,
                                               slug=prompt_loader.slug(t.title))
    if not run.dry_run:
        tk.prepare()
    run.enter("running")

    try:
        ws, checkouts = workspace.make_workspace(run.id, repo, main_layer.base_branch, branch, t.id, main_layer.copy)
        run.workspace = ws, checkouts
        for c in checkouts:
            run.log(f"worktree {c.wt} on {branch}, {c.starting_point}")
        run.cfg, run.layer, run.kind = cfg, main_layer, t.kind
        settle_starts(run, checkouts, main_layer, t, branch)
    except workspace.RunFailed as e:
        return _fail(run, e.why, detail=e.detail)
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
                                commands=commands, max_lines=limit)

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
            return _fail(run, "revise step failed", res.text)
        run.facts["left_for_a_person"] = left_for_a_person
        build_summary = res.structured.get("summary", "")
        replies = res.structured.get("replies") or []
        if stage(checkouts, deny).empty:
            return answer_without_changes(run, ws, checkouts, branch, build_summary, replies)
    else:
        res = run.agent("plan", brief.prompt(
            "plan", triage=triage_notes(approval), denied=prompt_loader.code_list(deny), checks=checks),
                        ws, role="plan", allowed_tools=read_rules, json_schema=PLAN_SCHEMA)
        if not res.ok or not isinstance(res.structured, dict):
            return _fail(run, "planning step failed")
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
            return _fail(run, "build step failed", res.text)
        build_summary = res.text
        if not stage(checkouts, deny).files:
            if estimate == 0:
                return nothing_to_build(run, t, ws, checkouts, branch, build_summary, left_for_a_person)
            return _fail(run, f"the builder stopped without changing anything, though the plan estimated "
                              f"{util.count(estimate, 'line')}", build_summary)
    plan_to_review = plan_text
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
                             prompt_loader.prompt("review_fix", problems=problems, notes=review.fix_notes()),
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
    try:
        ci_result = watch_ci(b, slot, branch, staged.changed, with_prs, draft_why)
    except workspace.RunFailed as e:
        if run.feedback:
            raise
        prs = pr_link_list(with_prs)
        status, why, _ = _fail(run, e.why, detail=f"{e.detail}\n\n### PRs\n\n{prs}\n\nReview comments on a PR "
                                                  "start a revision, as on any arete PR.")
        return status, why, record_prs(run, branch, with_prs)
    outcome += f", CI: {ci_result}"
    run.enter("pr")
    if run.feedback:
        answer_feedback(run, replies, "Done in the latest push.")
        run.say(f"Revised for your review.\n\n{pr_report}\n\nCI: {ci_result}.")
    else:
        run.say(f"PRs for this ticket:\n\n{pr_status_lines(with_prs, opened, pushed)}\n\nCI: {ci_result}."
                + published_note(draft_why, verified, opened, drafted))
    automerge(b, approval, draft_why, verified, review, opened + ready_pushed(run, pushed, ci_stopped, draft_why),
              automerge_about(t, build_summary))
    urls = record_prs(run, branch, with_prs)
    release_workspace(run, ws, checkouts)
    return "pr", outcome, urls


def furnish(cfg: config.Config, run: Run, layer: config.RepoLayer, ws: Path) -> str:
    for rel in layer.copy:
        if (src := run.repo.path / rel).exists():
            (dest := ws / rel).parent.mkdir(parents=True, exist_ok=True)
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


def report_body(run: Run, *, summary: str, review: Review, verified: verify.Verified, left: str,
                images: dict[str, str], approval: dict | None, attempts: int, checks: str, plan_text: str,
                size: str, held: str = "", written: str = "") -> str:
    return public_text(run, (
        f"## Summary\n\n{written or summary_of(review, summary)}\n\n"
        + ("" if run.feedback else triage_block(approval))
        + left_block(left)
        + assumed_block(run)
        + f"{CHECKED_IN_USE_HEADING}\n\n{verified.markdown(images)}\n\n"
        + review.markdown()
        + (f"\n\nHeld as a draft: {held}." if held else "")
        + f"\n\n## Checks\n\nAll passed after {util.count(attempts, 'attempt')}: {checks}\n\n"
        + ("" if run.feedback else
           f"<details><summary>Plan</summary>\n\n{cut_at_end(plan_text, PLAN_CHARS)}\n\n</details>\n\n")
        + f"Run `{run.id}`, {size}, {util.paid_note(record.metered_spent(run.id))}."
        + (f" Repairs the review findings of run `{run.repairs}`." if run.repairs else "")
    ))


def automerge(b: Build, approval: dict | None, draft_why: str, verified: verify.Verified, review: Review,
              opened: list[workspace.Checkout], about: str) -> None:
    run = b.run
    if not opened or draft_why or b.layer.trust_level != "automerge" or run.feedback or run.repairs:
        return
    if (approval and approval["sent_by"] == "triage") or intake.AUTO_LABEL in b.ticket.labels:
        run.facts["merge_held"] = "triage approved the ticket, so a person merges it"
        run.log(f"automerge left off: {run.facts['merge_held']}")
        return
    for c in opened:
        try:
            hold = merge_hold(b, c, verified, review, about)
        except gh.GhUnsure:
            run.facts["merge_held"] = "GitHub did not confirm whether it merged the PR"
            run.log(f"automerge: {run.facts['merge_held']} at {c.lease}; check {c.pr} before retrying")
            return
        except (RuntimeError, OSError, KeyError, TypeError, ValueError) as e:
            run.log(f"automerge could not establish eligibility: {public_text(run, util.first_line(str(e), 300))}")
            hold = "the merge gate could not be completed"
        if not hold:
            run.log(f"automerged {c.pr} at {c.lease}")
            continue
        run.facts["merge_held"] = hold
        run.log(f"automerge left {c.pr} for a person: {hold}")
        try:
            gh.pr_comment(c.pr, public_text(run, f"Automerge stays off for this PR: {hold}. A person merges it."))
        except gh.GhError as e:
            run.log(f"automerge could not report the hold on {c.pr}: {e}")


def merge_hold(b: Build, c: workspace.Checkout, verified: verify.Verified, review: Review, about: str) -> str:
    if len(b.checkouts) != 1:
        return "a change spanning repositories cannot be merged atomically"
    if verified.verdict != "pass" or not verified.finished or not verified.checks or verified.failed_checks:
        return "native behavioral verification did not pass"
    head = c.lease
    if not head:
        return "this run has no published head to verify"
    tree = git(["rev-parse", f"{head}^{{tree}}"], c.wt).strip()
    trees = {c.prefix: tree}
    if not tree or verified.native_trees != trees:
        return "there is no native behavioral pass for the published tree"
    if not review.approved() or review.trees != trees:
        return "the published tree lacks a current approving review"
    if not b.layer.checks or b.checked.get("tree") != tree or b.checked.get("commands") != b.layer.checks:
        return "the published tree lacks the full local check result"
    if not jev.ready(b.cfg, [b.run.repo]):
        return "JEV risk screening is unavailable"
    base_sha = git(["rev-parse", c.origin_base], c.wt).strip()
    if not base_sha or git(["merge-base", base_sha, head], c.wt).strip() != base_sha:
        return "the checked head does not contain the target base"
    number = gh.pr_number(c.pr)
    _, hold = gh.merge_readiness(c.github, number, head, c.base, base_sha, wait=gh.MERGE_WAIT_SECONDS)
    if hold:
        return hold
    patch = checkout_patch(c, [], f"{base_sha}..{head}")
    if not patch:
        return "there is no diff to risk screen"
    try:
        risk = jev.risk(vault.hidden(patch), vault.hidden(about), b.cfg, trace=(b.run.id, "automerge risk jev"))
    except jev.Unjudged:
        return "JEV could not judge the diff"
    if risk.hold:
        return risk.hold
    return gh.pr_merge_auto(c.github, number, head, c.base, base_sha)


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
    had, text = gh.split_link(body, (run.tracker.pr_link(t, whole=True), run.tracker.pr_link(t, whole=False)))
    if had and had != wanted:
        run.log(f"{pr} now links the ticket with: {wanted}")
    left_synced = with_left_section(text, left)
    if left_synced != text:
        run.log(f"{pr}: {'wrote' if left else 'removed'} the {LEFT_TITLE} section")
    return (wanted if had else ""), public_text(run, left_synced)


def sync_pr_bodies(run, t, with_prs, opened, standing, pushed, report, left, link, stops=None, committed=True):
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


def commit_and_push(checkouts: list[workspace.Checkout], changed: list[workspace.Checkout], branch: str, message: str) -> None:
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
    for c in changed:
        try:
            git(["push", "-q", f"--force-with-lease={branch}:{c.lease}", "-u", "origin", branch], c.wt, timeout=600)
        except RuntimeError as e:
            done = ", ".join(p.github for p in pushed) or "nothing"
            raise workspace.RunFailed(f"the push to {c.github} failed after arete pushed {done}",
                            f"{e}\n\nA retry builds on whatever reached the branch.") from e
        c.lease = git(["rev-parse", "HEAD"], c.wt).strip()
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
    if stopped_before and not draft_why and last.all_read:
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
    except gh.GhError as e:
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
    not_caused: dict[tuple[str, str, str], str] = {}
    for n in range(1, max_rounds + 1):
        slot.free()
        run.log(slots.FREED_SLOT_FOR_CI)
        with run.waiting("ci"):
            current = ci.Round(n, [ci.compare_with_base(ci.watch(ci_target(c), run.log), run.log) for c in watched])
        judged = [r for r in current.reds if r.kind != "base"]
        for r in judged:
            if r.key in not_caused:
                r.caused, r.reason = False, not_caused[r.key]
        fresh = [r for r in judged if r.key not in not_caused]
        if fresh:
            slot.take(run.log, run.waiting)
        try:
            fix_summary = judge_ci(b, fresh, n, max_rounds, watched, fix) if fresh else ""
        except workspace.RunFailed:
            yield current
            raise
        not_caused.update({r.key: r.reason for r in judged if not r.caused})
        yield current
        caused = current.caused
        if not caused:
            if fresh:
                discard_edits(b)
            return
        names = ", ".join(r.label for r in caused)
        if n == max_rounds:
            raise workspace.RunFailed(f"CI still fails after {util.count(max_rounds, 'round')}: {names}", ci.red_list(caused))
        push_ci_fix(b, branch, caused, names, n)
        watched += [c for c in b.staged.changed if c not in watched]
        current.story = f"{names} failed, caused by this PR, and arete pushed a fix: {fix_summary}".rstrip(": ")
        run.log(f"ci: pushed a fix for {names}")


def push_ci_fix(b: Build, branch: str, caused: list[ci.Red], names: str, n: int) -> None:
    staged = b.restage()
    if staged.empty:
        raise workspace.RunFailed(f"{names} failed, caused by this PR, and the CI fix step changed nothing", ci.red_list(caused))
    if no_pr := [c.github for c in staged.changed if not c.pr]:
        raise workspace.RunFailed(f"the CI fix step changed {', '.join(no_pr)}, where this run has no PR, so arete pushed nothing",
                        ci.red_list(caused))
    if not b.check_with_repairs()[0]:
        report = f"checks-ci-round{n}.md"
        b.run.save(report, b.report)
        raise workspace.RunFailed("the checks failed after the CI fix",
                        f"{ci.red_list(caused)}\n\nThe check output is in `{report}` of run `{b.run.id}`.")
    b.require_valid_diff("the CI fix")
    b.push(branch, ci_fix_message(b, caused))


def ci_target(c: workspace.Checkout) -> ci.Target:
    return ci.Target(c.github, c.pr, c.pr_name, c.lease, c.base)


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
    def __init__(self, why: str, detail: str, paths: list[str]):
        super().__init__(why, detail)
        self.paths = paths


def merge_base_into_kept(run: AgentRun, checkouts: list[workspace.Checkout], layer: config.RepoLayer,
                         ticket: tracker.Ticket) -> list[workspace.Checkout]:
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
                                        reason="since the base moved while the run waited for its answer")
        except workspace.RunFailed as e:
            files = f" in {', '.join(e.paths)}" if isinstance(e, ConflictUnresolved) else ""
            raise workspace.RunFailed(f"merging `{c.origin_base}` into the edits kept in {c.github} failed{files}: {e.why}. "
                            f"The worktree is back at the kept commit and the answers are kept, so "
                            f"`arete resume {run.id}` continues with them", e.detail) from e
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


def merge_as_run(run_id: str, c: workspace.Checkout, commit: str, named: str) -> None:
    with util.repo_lock(c.src):
        util.git_as_owner(["merge", "-q", "--no-edit", "-m", f"Merge {named} at {commit[:12]}",
                           "-m", f"Arete-Run: {run_id}", commit], c.wt)


def merge_moved_bases(run: AgentRun, checkouts: list[workspace.Checkout], layer: config.RepoLayer, ticket: tracker.Ticket,
                      log_prefix: str = "", reason: str = "since the base moved after the last push") -> list[workspace.Checkout]:
    merged = []
    for c in checkouts:
        c.base_commit = git(["rev-parse", c.origin_base], c.wt).strip()
        if git(["rev-list", "--count", f"HEAD..{c.base_commit}"], c.wt).strip() == "0":
            continue
        if edits := workspace.uncommitted(c.wt):
            raise workspace.RunFailed(f"{c.github} has uncommitted edits ({', '.join(edits[:5])}), so arete merged nothing into "
                            "them and left them as they are")
        head = git(["rev-parse", "HEAD"], c.wt).strip()
        with record.timed(run.id, "base_merge", repo=c.github, base=c.base, base_commit=c.base_commit,
                          head=head) as rec:
            try:
                try:
                    merge_as_run(run.id, c, c.base_commit, c.origin_base)
                except RuntimeError as e:
                    conflicts = unmerged_paths(c.wt)
                    if not conflicts:
                        raise workspace.RunFailed(f"git could not merge `{c.origin_base}` into the branch in {c.github}: {e}")
                    rec["conflicts"] = conflicts
                    try:
                        resolve_conflicts(run, c, conflicts, layer, ticket)
                    except workspace.RunFailed as e:
                        raise ConflictUnresolved(e.why, e.detail, conflicts) from e
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
                      ticket: tracker.Ticket, incoming: tuple[str, str] | None = None) -> None:
    deny = diffguard.denied_paths(layer)
    named, commit = incoming or (c.origin_base, c.base_commit)
    if protected := [p for p in conflicts if util.matches_any(p, deny) or util.matches_any(c.prefix + p, deny)]:
        raise workspace.RunFailed(f"merging `{named}` conflicts in protected paths in {c.github} "
                        f"({', '.join(protected)}), which a person must merge")
    own = {p for p in util.git_records(["diff", "--name-only", f"{commit}...HEAD"], c.wt)
           if p not in conflicts and not (util.matches_any(p, deny) or util.matches_any(c.prefix + p, deny))}
    incoming_paths = {p for p in util.git_records(["diff", "--name-only", f"HEAD...{commit}"], c.wt)
                      if p not in conflicts and not (util.matches_any(p, deny) or util.matches_any(c.prefix + p, deny))}
    supplemental = own | incoming_paths
    tries = max(1, run.settings("build").tries)
    feedback = "this is the first attempt"
    for n in range(1, tries + 1):
        untracked_before = workspace.untracked_files(c.wt)
        ignored_before = ignored_state(c.wt)
        previous = run.sessions.get("build")
        text = prompt_loader.prompt("resolve", repo=run.repo.name, github=c.github, base=named,
                      base_commit=commit[:12], head=git(["rev-parse", "HEAD"], c.wt).strip()[:12],
                      ref=ticket.ref, title=ticket.title, body=ticket.body,
                      conflicts="\n".join(f"- `{p}`" for p in conflicts),
                      own="\n".join(f"- `{p}`" for p in sorted(own)) or "- none",
                      branch_log=git(["log", "--oneline", "-n30", "MERGE_HEAD..HEAD"], c.wt),
                      base_log=git(["log", "--oneline", "-n30", "HEAD..MERGE_HEAD"], c.wt),
                      commands=prompt_loader.code_list(layer.builder_bash), attempt=n, tries=tries, feedback=feedback,
                      owner=run.owner_note)
        try:
            res = run.agent(round_name("resolve", n), prompt_loader.task_prompt(text), c.wt, role="build",
                            allowed_tools=prompt_loader.bash_rules(layer.builder_bash), disallowed_tools=routing.NO_GIT_WRITES,
                            json_schema=RESOLVE_SCHEMA, resume=NO_SESSION, extra_env=dict(RESOLVER_SETS))
        finally:
            if previous is None:
                run.sessions.pop("build", None)
            else:
                run.sessions["build"] = previous
        if not res.ok:
            raise workspace.RunFailed(f"the conflict resolution step failed: {res.error}", res.text)
        answer = res.structured if isinstance(res.structured, dict) else {}
        if answer.get("resolved") is not True:
            raise workspace.RunFailed(f"the resolver could not keep both the branch's work and the base's in {c.github}: "
                            f"{answer.get('summary') or 'no reason given'}", res.text)
        for name in workspace.untracked_files(c.wt) - untracked_before:
            if Path(name).parent.name == "__pycache__" and Path(name).suffix == ".pyc":
                workspace.remove_untracked(c.wt, name)
        ignored_after = ignored_state(c.wt)
        changed_ignored = {n for n in ignored_before.keys() | ignored_after.keys()
                           if ignored_before.get(n) != ignored_after.get(n)}
        touched = (set(util.git_records(["diff", "--name-only"], c.wt)) | (workspace.untracked_files(c.wt) - untracked_before)
                   | changed_ignored)
        if outside := sorted(touched - set(conflicts) - supplemental):
            raise workspace.RunFailed(f"the resolver edited files outside the conflict in {c.github}: {', '.join(outside)}")
        if left := marked(c.wt, conflicts):
            feedback = f"conflict markers remain in {', '.join(left)}; remove every one"
            if n == tries:
                raise workspace.RunFailed(f"conflict markers remain in {', '.join(left)} after {util.count(n, 'attempt')} "
                                f"in {c.github}")
            continue
        git(["add", "-A", "--", *conflicts, *sorted(touched & supplemental)], c.wt)
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
    logs = run_folder(run, "ci")
    failures = "\n\n".join(ci.failure_note(r, logs / f"round{n}-{i}-{prompt_loader.slug(r.label)}.log")
                           for i, r in enumerate(reds, 1))
    diff = ", ".join(f"`{c.diff_from_base}`" for c in b.checkouts)
    left = NOT_CAUSED_LEFT_FOR_A_PERSON + b.left_for_a_person if b.left_for_a_person else ""
    name = b.next_step("ci_fix")
    text = prompt_loader.prompt("ci_fix", round=n, max_rounds=max_rounds, failures=failures, diff=diff, checks=b.check_commands,
                  merged_base=merged_base, left_for_a_person=left)
    res = fix(name, text, failures) if fix else b.resume(name, text, role="ci_repair", json_schema=CI_FIX_SCHEMA)
    if not res.ok or not isinstance(res.structured, dict):
        for r in reds:
            r.reason = "The CI fix step failed before it judged this check."
        raise workspace.RunFailed("the CI fix step failed", ci.red_list(reds))
    verdicts = {name_key(v.get("name", "")): v for v in res.structured.get("checks", []) if isinstance(v, dict)}
    for r in reds:
        v = verdicts.get(name_key(r.label)) or verdicts.get(name_key(r.check.name)) or {}
        r.caused = v.get("caused_by_pr") is not False
        r.reason = ci.clip(v.get("reason") or "The fix step gave no verdict for this check.", MAX_CI_REASON_CHARS)
        run.log(f"ci: {r.label} {'caused' if r.caused else 'not caused'} by the PR: {util.first_line(r.reason, 200)}")
    return ci.clip(res.structured.get("summary", ""), MAX_CI_REASON_CHARS)


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
            reply: str = "") -> Review:
    before = before or Review.stored({})
    earlier = before.blocking()
    res = b.run.agent(step, b.brief.prompt(
        "review", plan=plan, checks=b.report, evidence=verified.markdown(), compare=compare_with_fork(b.checkouts),
        diff=util.tail(diffguard.fold_moved(b.staged.patch), MAX_DIFF_IN_PROMPT),
        earlier=earlier_review(earlier, reply, fixed_since(b, before.trees or {}))),
                      b.ws, role="review", allowed_tools=prompt_loader.bash_rules(b.read_bash),
                      json_schema=REVIEW_SCHEMA)
    if not res.ok or not isinstance(res.structured, dict) or not isinstance(res.structured.get("problems"), list):
        return Review.failed_step(earlier, before.waived or [], before.trees or {})
    return Review.judged(res.structured, earlier, before.waived or [], index_trees(b.checkouts))


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
                                               failures=verified.markdown(), checks=b.check_commands),
             role="check_repair")


@runlimits.verification
def check_in_use(b: Build, goal: str, summary: str) -> verify.Verified:
    return use_product(b, "verify", "verify", goal=goal, summary=summary.strip()[:4000],
                       diff=util.tail(diffguard.fold_moved(b.staged.patch), MAX_DIFF_IN_PROMPT))


def use_product(b: TicketWork, step_name: str, prompt_name: str, role: str = "verify",
                require_released: bool = False, **values) -> verify.Verified:
    run, v = b.run, verify.with_free_ports(b.layer.verify)
    merge_trees = (verification_trees(b) if prompt_name == role == "verify" and b.layer.trust_level == "automerge"
                   else {})
    step = b.next_step(step_name)
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
    evidence = run_folder(run, "evidence")
    untracked_before = untracked_by_dir(b.git_dirs)
    attempt = None
    try:
        attempt = scratch.make(run.id, step, b.ws, run.dir)
    except (OSError, ValueError) as e:
        run.log(f"{step}: trusted scratch unavailable: {util.first_line(str(e), 300)}")
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
            probes = (verify.run_probes(replace(v, probe_seconds=run.time_for(v.probe_seconds)), b.ws, attempt)
                      if v.probes else None)
            if probes:
                run.save(f"{step}-probes.json", json.dumps(asdict(probes), indent=2))
                run.log(f"{step}: configured probes {probes.verdict}")
                if probes.verdict == "pass":
                    v = replace(v, notes=v.notes + "\n\nArete already executed these owner-configured probes in a "
                                "confined disposable workspace. Use their observed results as evidence; do not repeat "
                                "these commands. Check only acceptance criteria not covered by them.\n\n" + probes.markdown())
            if not probes or probes.verdict == "pass":
                agent_started = True
                res = run.agent(step, b.brief.prompt(
                    prompt_name, surface=surface_note(v, url, evidence), notes=v.notes, scratch=scratch.note(attempt),
                    commands=prompt_loader.code_list(bash), checks=check_list(b.layer),
                    compare=compare_with_fork(b.checkouts), owner=run.owner_note, **values),
                                b.ws, role=role,
                                allowed_tools=prompt_loader.bash_rules(bash) + ([f"mcp__playwright__{t}" for t in verify.PLAYWRIGHT_TOOLS] if url else []),
                                disallowed_tools=routing.NO_GIT_WRITES, json_schema=verify.SCHEMA,
                                mcp_config=browser.mcp_config(evidence) if url else "",
                                scratch=attempt)
                agent_finished = True
    except owner_asks.Asked:
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
                                              "the start log stays in the run folder", finished=False)
        return verify.Verified("fail", f"The app did not start: {e}")
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
            restore_worktrees(untracked_before, evidence)
    if probes and probes.verdict != "pass":
        verified = probes
    elif not res.ok or not isinstance(res.structured, dict):
        return verify.Verified("blocked", "The check in use did not finish.", finished=False,
                               blocked_by=util.first_line(res.error or "the verifier returned no verdict", 300))
    else:
        verified = verify.Verified.from_agent(res.structured)
        if probes:
            verified.checks = probes.checks + verified.checks
    if fixture_blocker:
        verified.verdict, verified.blocked_by, verified.finished = "blocked", fixture_blocker, False
    if (verified.verdict == "pass" and merge_trees and agent_finished and res.runner in {"pi", "codex", "claude"}
            and merge_trees == verification_trees(b)):
        verified.native_trees = merge_trees
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
            "- Send API requests with curl, shaped the way the app's clients send them, and read the status and body.")


def restore_worktrees(untracked_before: dict[Path, set[str]], evidence: Path) -> None:
    view = workspace.scratch_view()
    for d, name in untracked_added_since(untracked_before):
        f = d / name
        if workspace.record_holds(view, d) or any(p.startswith(paths.SCRATCH_PREFIX) for p in Path(name).parts):
            continue
        if f.suffix == ".png" and not (evidence / f.name).exists():
            shutil.move(f, evidence / f.name)
        else:
            workspace.remove_untracked(d, name)
    for d in untracked_before:
        if not workspace.record_holds(view, d):
            git(["checkout", "-q", "--", "."], d)


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
    if verified.verdict == "blocked" and (not approved or refused):
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


def check_script(folder: Path, layer: config.RepoLayer, cwd: Path) -> tuple[list[str], list[str]]:
    if not layer.checks:
        return [], []
    script = folder / CHECKS_SCRIPT
    arms = "".join(f"{shlex.quote(name)}) exec sh -c {shlex.quote(cmd)} ;;\n" for name, cmd in layer.checks.items())
    usage = shlex.quote(f"usage: {script} NAME, where NAME is one of: {', '.join(layer.checks)}")
    folder.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!/bin/sh\ncd {shlex.quote(str(cwd))} || exit 1\ncase \"${{1-}}\" in\n{arms}"
                      f"*) echo {usage} >&2\n   exit 2 ;;\nesac\n")
    script.chmod(0o755)
    return [shlex.join([str(script), name]) for name in layer.checks], [f"{shlex.quote(str(script))} *"]


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
    for root in workspace.finalize_scratch():
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
        for failure in scratch.settle(days):
            print(f"verification scratch settle failed: {failure}")
    except sandbox.ConfinementUnavailable:
        pass
    except OSError as e:
        print(f"verification scratch settle failed: {e}")


def tell_ticket_run_died(cfg: config.Config, row: dict) -> None:
    repo = cfg.repo(row["repo"])
    run = Run(row["id"], repo, paths.RUNS_DIR / row["id"], tracker=tracker.for_ticket(cfg, repo, str(row["issue"])),
              tid=str(row["issue"]), feedback=pending_feedback(repo.name, str(row["issue"])),
              alone=bool(row.get("only_step")))
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


def spawn_run(repo_name: str, tid: str, force: bool = False) -> subprocess.Popen:
    return control.launch(run_command(repo_name, tid, force), RUNS_LOG)


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
            if state == "exited":
                raise control.Refused(f"{name} ended before it showed as running; the end of "
                                      f"{paths.short_path(paths.LOG_DIR / RUNS_LOG)} says why")
    return {"ticket": name, "repo": entry["repo"], "id": entry["id"]}


def answered_run(repo_name: str, tid: str) -> dict | None:
    newest = run_store.newest_whole_run(repo_name, tid)
    if (newest and newest["status"] == "needs-info" and asks_store.asks_due([newest["id"]])
            and not asks_store.asks_of(newest["id"], "open")):
        return newest
    return None


def skipped_tickets() -> dict:
    return util.read_json(SKIPPED_FILE, {})
