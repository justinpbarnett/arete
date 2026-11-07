from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import gh, util

WAIT_SECONDS = 30 * 60
FIRST_CHECK_SECONDS = 3 * 60
SETTLE_SECONDS = 60
POLL_SECONDS = 30
LOG_EXCERPT_CHARS = 8000
LOG_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", re.M)
SECTION_START, SECTION_END = "<!-- arete:ci -->", "<!-- /arete:ci -->"
SECTION = re.compile(rf"\n*{re.escape(SECTION_START)}.*?{re.escape(SECTION_END)}", re.S)
STOPPED = "Arete stopped:"
PAST_TENSE = {"pass": "passed", "fail": "failed", "pending": "was still running", "skipped": "was skipped"}
READ_ERRORS = (gh.GhError, KeyError, TypeError, ValueError)
RED_LINE = {
    "base": "- Failed on the base too: {check}. It {base_result}, so this PR did not cause it.",
    "not caused": "- Failed, not caused by this PR: {check}. It {base_result}. {reason}",
    "caused": "- Failed, caused by this PR: {check}. {reason}",
    "unjudged": "- Failed: {check}. It {base_result}. {reason}",
}
RED_COUNT = {
    "base": "red on the base too",
    "not caused": "red but not caused by the PR",
    "caused": "red from the PR",
    "unjudged": "red and not judged",
}


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
    caused: bool | None = None
    reason: str = ""

    @property
    def label(self) -> str:
        return f"{self.target.pr_name} {self.check.name}"

    @property
    def key(self) -> tuple[str, str, str]:
        return self.target.github, self.target.sha, self.check.name

    @property
    def kind(self) -> str:
        if self.on_base is not None and self.on_base.state == "fail":
            return "base"
        return {True: "caused", False: "not caused", None: "unjudged"}[self.caused]

    @property
    def base_result(self) -> str:
        base = f"`{self.target.base}`"
        if not self.base_sha:
            return f"has no known result on {base}, since arete could not read the checks there"
        if not self.on_base:
            return f"had no result on {base} at `{self.base_sha[:7]}`"
        return f"{PAST_TENSE[self.on_base.state]} on {base} at `{self.base_sha[:7]}` on {self.on_base.updated[:10]}"

    def markdown(self) -> str:
        return RED_LINE[self.kind].format(check=linked(self.check), base_result=self.base_result,
                                          reason=self.reason).rstrip()


@dataclass
class Watched:
    target: Target
    checks: dict[str, gh.Check]
    read: bool = True
    reds: list[Red] = field(default_factory=list)

    def in_state(self, state: str) -> list[gh.Check]:
        return [c for c in self.checks.values() if c.state == state]

    def markdown(self) -> list[str]:
        if not self.read:
            return [f"- Arete could not read the checks within {minutes(WAIT_SECONDS)} of the push."]
        if not self.checks:
            return [f"- No checks reported within {minutes(FIRST_CHECK_SECONDS)} of the push."]
        lines = []
        if passed := self.in_state("pass"):
            lines.append(f"- Passed: {', '.join(map(linked, passed))}")
        if skipped := self.in_state("skipped"):
            lines.append(f"- Skipped: {', '.join(map(linked, skipped))}")
        lines += [r.markdown() for r in self.reds]
        if pending := self.in_state("pending"):
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
        return [r for r in self.reds if r.kind == "caused"]

    @property
    def all_read(self) -> bool:
        return all(w.read for w in self.seen)

    def summary(self) -> str:
        checks = [c for w in self.seen for c in w.checks.values()]
        ran = [c for c in checks if c.state != "skipped"]
        parts = [f"{sum(c.state == 'pass' for c in ran)} of {len(ran)} passed"] if ran else []
        if skipped := len(checks) - len(ran):
            parts.append(f"{skipped} skipped")
        kinds = Counter(r.kind for r in self.reds)
        parts += [f"{kinds[kind]} {words}" for kind, words in RED_COUNT.items() if kinds[kind]]
        if pending := sum(c.state == "pending" for c in checks):
            parts.append(f"{pending} still running after {minutes(WAIT_SECONDS)}")
        if unread := [w.target.pr_name for w in self.seen if not w.read]:
            parts.append(f"could not read the checks of {', '.join(unread)}")
        return ", ".join(parts) or "no checks reported"


def minutes(seconds: int) -> str:
    return util.count(seconds // 60, "minute")


def linked(c: gh.Check) -> str:
    return f"[{c.name}]({c.link})" if c.link else f"`{c.name}`"


def watch(target: Target, log: Callable[[str], None]) -> Watched:
    log(f"ci: waiting on the checks of {target.pr_name} at {target.sha[:7]}")
    checks: dict[str, gh.Check] = {}
    read = False
    waiting_on: list[str] | None = None
    while True:
        try:
            checks = gh.commit_checks(target.github, target.sha)
            read = True
        except READ_ERRORS as e:
            log(f"ci: reading the checks of {target.pr_name} failed, trying again: {util.first_line(str(e), 200)}")
        waited = time.monotonic() - target.pushed_at
        pending = sorted(c.name for c in checks.values() if c.state == "pending")
        if checks and pending != waiting_on:
            log(f"ci: {target.pr_name} {len(checks) - len(pending)} of {util.count(len(checks), 'check')} done"
                + (f", waiting on {', '.join(pending)}" if pending else ""))
            waiting_on = pending
        if checks and not pending and waited >= SETTLE_SECONDS:
            return Watched(target, checks)
        if read and not checks and waited >= FIRST_CHECK_SECONDS:
            log(f"ci: no checks on {target.pr_name} within {minutes(FIRST_CHECK_SECONDS)} of the push")
            return Watched(target, checks)
        if waited >= WAIT_SECONDS:
            log(f"ci: {target.pr_name} still waiting on {', '.join(pending)} after {minutes(WAIT_SECONDS)}" if read
                else f"ci: could not read the checks of {target.pr_name} within {minutes(WAIT_SECONDS)}")
            return Watched(target, checks, read)
        time.sleep(POLL_SECONDS)


def compare_with_base(w: Watched, log: Callable[[str], None]) -> Watched:
    failed = w.in_state("fail")
    if not failed:
        return w
    t = w.target
    try:
        base_sha = gh.branch_sha(t.github, t.base)
        on_base = gh.commit_checks(t.github, base_sha)
    except READ_ERRORS as e:
        log(f"ci: could not read the checks on {t.base} of {t.github}: {util.first_line(str(e), 200)}")
        base_sha, on_base = "", {}
    w.reds = [Red(t, c, on_base.get(c.name), base_sha) for c in failed]
    for r in w.reds:
        log(f"ci: {r.label} failed; it {r.base_result.replace('`', '')}")
    return w


def failure_note(r: Red, log_file: Path) -> str:
    lines = [f"### {r.label}", "", f"- Link: {r.check.link or 'none'}", f"- This check {r.base_result}."]
    if r.check.description:
        lines.append(f"- GitHub says: {r.check.description}")
    if not r.check.job:
        return "\n".join([*lines, "- This check has no Actions log. Judge it from its name, what GitHub says, and the diff."])
    try:
        text = gh.job_log(r.target.github, r.check.job)
    except gh.GhError as e:
        return "\n".join([*lines, f"- Arete could not read its log: {util.first_line(str(e), 300)}"])
    log_file.write_text(text)
    return "\n".join([*lines, f"- Full log: `{log_file}`", "", "The log up to its last error:", "", "````",
                      excerpt(text), "````"])


def red_list(reds: list[Red]) -> str:
    return "### CI\n\n" + "\n".join(f"- [{r.target.pr_name}]({r.target.pr}): {linked(r.check)}. {r.reason}".rstrip()
                                     for r in reds)


def clip(text: str, max_chars: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    sentence_end = cut.rfind(". ")
    if sentence_end > max_chars // 2:
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
    lines = [SECTION_START, "## CI", ""]
    for last in rounds[-1:]:
        for w in last.seen:
            lines += [f"[{w.target.pr_name}]({w.target.pr}) at `{w.target.sha[:7]}`, round {last.n} of {max_rounds}:",
                      "", *w.markdown(), ""]
    if ending:
        lines += [ending, ""]
    if len(rounds) > 1:
        lines += ["Earlier rounds:", "", *(f"- Round {r.n}: {r.story}" for r in rounds[:-1]), ""]
    return "\n".join([*lines, SECTION_END])


def with_section(body: str, ci_section: str) -> str:
    return f"{SECTION.sub('', body).rstrip()}\n\n{ci_section}"


def stopped(body: str) -> bool:
    found = SECTION.search(body or "")
    return bool(found) and STOPPED in found.group(0)
