from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import gh, util

WAIT_SECONDS = 30 * 60
FIRST_CHECK_SECONDS = 3 * 60
SETTLE_SECONDS = 60
POLL_SECONDS = 30
LOG_EXCERPT_CHARS = 8000
LOG_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", re.M)
SECTION_START, SECTION_END = "<!-- arete:ci -->", "<!-- /arete:ci -->"
SECTION = re.compile(rf"\n*{re.escape(SECTION_START)}.*?{re.escape(SECTION_END)}", re.S)
PAST_TENSE = {"pass": "passed", "fail": "failed", "pending": "was still running"}


@dataclass
class Target:
    github: str
    pr: str
    pr_name: str
    sha: str
    base: str
    pushed_at: float = field(default_factory=time.monotonic)


@dataclass
class Red:
    target: Target
    check: gh.Check
    on_base: gh.Check | None
    base_sha: str
    caused: bool = True
    reason: str = ""

    @property
    def label(self) -> str:
        return f"{self.target.pr_name} {self.check.name}"

    @property
    def key(self) -> tuple[str, str, str]:
        return self.target.github, self.target.sha, self.check.name

    @property
    def red_on_base(self) -> bool:
        return self.on_base is not None and self.on_base.state == "fail"

    @property
    def base_result(self) -> str:
        base = f"`{self.target.base}`"
        if not self.base_sha:
            return f"unknown, since arete could not read the checks on {base}"
        if not self.on_base:
            return f"no result on {base} at `{self.base_sha[:7]}`"
        return f"{PAST_TENSE[self.on_base.state]} on {base} at `{self.base_sha[:7]}` on {self.on_base.when[:10]}"


@dataclass
class Watched:
    target: Target
    checks: dict[str, gh.Check]
    reds: list[Red] = field(default_factory=list)

    def named(self, state: str) -> list[gh.Check]:
        return [c for c in self.checks.values() if c.state == state]

    def markdown(self) -> list[str]:
        if not self.checks:
            return [f"- No checks reported within {minutes(FIRST_CHECK_SECONDS)} of the push."]
        lines = []
        if passed := self.named("pass"):
            lines.append(f"- Passed: {', '.join(map(linked, passed))}")
        for r in self.reds:
            if r.red_on_base:
                lines.append(f"- Failed on the base too: {linked(r.check)} {r.base_result}, so this PR did not cause it.")
            elif r.caused:
                lines.append(f"- Failed, caused by this PR: {linked(r.check)}. {r.reason}".rstrip())
            else:
                lines.append(f"- Failed, not caused by this PR: {linked(r.check)}. On the base it {r.base_result}. "
                             f"{r.reason}".rstrip())
        if pending := self.named("pending"):
            lines.append(f"- Still running after {minutes(WAIT_SECONDS)}: {', '.join(map(linked, pending))}")
        return lines


@dataclass
class Round:
    n: int
    seen: list[Watched]
    story: str = ""

    @property
    def reds(self) -> list[Red]:
        return [r for w in self.seen for r in w.reds]

    @property
    def caused(self) -> list[Red]:
        return [r for r in self.reds if r.caused and not r.red_on_base]

    def summary(self) -> str:
        checks = [c for w in self.seen for c in w.checks.values()]
        if not checks:
            return "no checks reported"
        parts = [f"{sum(c.state == 'pass' for c in checks)} of {len(checks)} passed"]
        on_base = sum(r.red_on_base for r in self.reds)
        not_caused = sum(not r.red_on_base and not r.caused for r in self.reds)
        pending = sum(c.state == "pending" for c in checks)
        if on_base:
            parts.append(f"{on_base} red on the base too")
        if not_caused:
            parts.append(f"{not_caused} red but not caused by the PR")
        if self.caused:
            parts.append(f"{len(self.caused)} red from the PR")
        if pending:
            parts.append(f"{pending} still running after {minutes(WAIT_SECONDS)}")
        return ", ".join(parts)


def minutes(seconds: int) -> str:
    return util.count(seconds // 60, "minute")


def linked(c: gh.Check) -> str:
    return f"[{c.name}]({c.link})" if c.link else f"`{c.name}`"


def watch(target: Target, log: Callable[[str], None]) -> Watched:
    log(f"ci: waiting on the checks of {target.pr_name} at {target.sha[:7]}")
    checks: dict[str, gh.Check] = {}
    waiting_on: list[str] | None = None
    while True:
        try:
            checks = gh.commit_checks(target.github, target.sha)
        except gh.GhError as e:
            log(f"ci: reading the checks of {target.pr_name} failed, trying again: {util.first_line(str(e), 200)}")
        waited = time.monotonic() - target.pushed_at
        pending = sorted(c.name for c in checks.values() if c.state == "pending")
        if checks and pending != waiting_on:
            log(f"ci: {target.pr_name} {len(checks) - len(pending)} of {util.count(len(checks), 'check')} done"
                + (f", waiting on {', '.join(pending)}" if pending else ""))
            waiting_on = pending
        if checks and not pending and waited >= SETTLE_SECONDS:
            return Watched(target, checks)
        if not checks and waited >= FIRST_CHECK_SECONDS:
            log(f"ci: no checks on {target.pr_name} within {minutes(FIRST_CHECK_SECONDS)} of the push")
            return Watched(target, checks)
        if waited >= WAIT_SECONDS:
            log(f"ci: {target.pr_name} still waiting on {', '.join(pending)} after {minutes(WAIT_SECONDS)}")
            return Watched(target, checks)
        time.sleep(POLL_SECONDS)


def compare_with_base(w: Watched, log: Callable[[str], None]) -> Watched:
    failed = w.named("fail")
    if not failed:
        return w
    t = w.target
    try:
        base_sha, _ = gh.branch_head(t.github, t.base)
        on_base = gh.commit_checks(t.github, base_sha)
    except (gh.GhError, KeyError, TypeError) as e:
        log(f"ci: could not read the checks on {t.base} of {t.github}: {util.first_line(str(e), 200)}")
        base_sha, on_base = "", {}
    w.reds = [Red(t, c, on_base.get(c.name), base_sha) for c in failed]
    for r in w.reds:
        log(f"ci: {r.label} failed; on the base it {r.base_result.replace('`', '')}")
    return w


def clip(text: str, most: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= most:
        return text
    cut = text[:most]
    sentence_end = cut.rfind(". ")
    if sentence_end > most // 2:
        return cut[:sentence_end + 1]
    return cut.rsplit(" ", 1)[0] + " (cut)"


def excerpt(log_text: str) -> str:
    text = LOG_TIMESTAMP.sub("", log_text)
    last_error = text.rfind("##[error]")
    if last_error >= 0:
        line_end = text.find("\n", last_error)
        text = text[:line_end] if line_end >= 0 else text
    return util.tail(text, LOG_EXCERPT_CHARS)


def section(rounds: list[Round], max_rounds: int, ending: str = "") -> str:
    last = rounds[-1]
    lines = [SECTION_START, "## CI", ""]
    for w in last.seen:
        lines += [f"[{w.target.pr_name}]({w.target.pr}) at `{w.target.sha[:7]}`, round {last.n} of {max_rounds}:", "",
                  *w.markdown(), ""]
    if ending:
        lines += [ending, ""]
    if len(rounds) > 1:
        lines += ["Earlier rounds:", "", *(f"- Round {r.n}: {r.story}" for r in rounds[:-1]), ""]
    return "\n".join([*lines, SECTION_END])


def with_section(body: str, ci_section: str) -> str:
    return f"{SECTION.sub('', body).rstrip()}\n\n{ci_section}"
