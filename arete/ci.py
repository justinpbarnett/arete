from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import _test_stacks, gh, util

WAIT_SECONDS = 30 * 60
FIRST_CHECK_SECONDS = 3 * 60
SETTLE_SECONDS = 60
POLL_SECONDS = 30
CONFLICT_SNAPSHOT_TIMEOUT = 5
LOG_EXCERPT_CHARS = 8000
MIN_TIMEOUT_REPORT_CHARS = 160
LOG_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", re.MULTILINE)
TEST_TIMEOUT = re.compile(rf"^{re.escape(_test_stacks.REPORT_START)}\r?\n.*?\r?\n"
                          rf"^{re.escape(_test_stacks.REPORT_END)}\r?$", re.DOTALL | re.MULTILINE)
PYTHON_STACK = re.compile(r"^(Python (?:class|child) stack \(PID \d+\))\r?\n", re.MULTILINE)
TIMEOUT_OMISSION = re.compile(r"\.\.\. \d+ timeout reports omitted")
SECTION_START, SECTION_END = "<!-- arete:ci -->", "<!-- /arete:ci -->"
SECTION = re.compile(rf"\n*^{re.escape(SECTION_START)}\r?$.*?^{re.escape(SECTION_END)}\r?$", re.DOTALL | re.MULTILINE)
STOPPED = "Arete stopped:"
PAST_TENSE = {"pass": "passed", "fail": "failed", "pending": "was still running", "skipped": "was skipped"}
READ_ERRORS = (gh.GhError, OSError, KeyError, TypeError, ValueError, AttributeError)
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
    rerun_reason: str = ""

    @property
    def label(self) -> str:
        return f"{self.target.pr_name} {self.check.name}"

    @property
    def key(self) -> tuple[str, str, gh.CheckKey]:
        return self.target.github, self.target.sha, self.check.key

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
    checks: dict[gh.CheckKey, gh.Check]
    read: bool = True
    reds: list[Red] = field(default_factory=list)
    conflicting: bool = False
    rerun_reds: set[gh.CheckKey] = field(default_factory=set)
    rerun_reasons: dict[gh.CheckKey, str] = field(default_factory=dict)
    previous_failure: bool = False
    red_workflows: set[int] = field(default_factory=set)

    def in_state(self, state: str) -> list[gh.Check]:
        return [c for c in self.checks.values() if c.state == state]

    def markdown(self) -> list[str]:
        lines = []
        if self.conflicting:
            lines.append("- Arete stopped waiting because GitHub reports a merge conflict with the base.")
        if not self.read:
            when = "when Arete stopped waiting" if self.conflicting else f"within {minutes(WAIT_SECONDS)} of the push"
            lines.append(f"- Arete could not read the checks {when}.")
        if not self.checks:
            return lines or [f"- No checks reported within {minutes(FIRST_CHECK_SECONDS)} of the push."]
        if passed := self.in_state("pass"):
            lines.append(f"- Passed: {', '.join(map(linked, passed))}")
        if skipped := self.in_state("skipped"):
            lines.append(f"- Skipped: {', '.join(map(linked, skipped))}")
        lines += [r.markdown() for r in self.reds]
        if pending := self.in_state("pending"):
            when = "when Arete stopped waiting" if self.conflicting else f"after {minutes(WAIT_SECONDS)}"
            lines.append(f"- Still running {when}: {', '.join(map(linked, pending))}")
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

    @property
    def passed(self) -> bool:
        return self.all_read and not self.reds

    def summary(self) -> str:
        checks = [c for w in self.seen for c in w.checks.values()]
        ran = [c for c in checks if c.state != "skipped"]
        parts = [f"{sum(c.state == 'pass' for c in ran)} of {len(ran)} passed"] if ran else []
        if skipped := len(checks) - len(ran):
            parts.append(f"{skipped} skipped")
        kinds = Counter(r.kind for r in self.reds)
        parts += [f"{kinds[kind]} {words}" for kind, words in RED_COUNT.items() if kinds[kind]]
        if pending := sum(len(w.in_state("pending")) for w in self.seen if not w.conflicting):
            parts.append(f"{pending} still running after {minutes(WAIT_SECONDS)}")
        if unread := [w.target.pr_name for w in self.seen if not w.read]:
            parts.append(f"could not read the checks of {', '.join(unread)}")
        if conflicts := [w.target.pr_name for w in self.seen if w.conflicting]:
            parts.append(f"GitHub reports merge conflicts on {', '.join(conflicts)}")
        return ", ".join(parts) or "no checks reported"


def minutes(seconds: int) -> str:
    return util.count(seconds // 60, "minute")


def linked(c: gh.Check) -> str:
    return f"[{c.name}]({c.link})" if c.link else f"`{c.name}`"


def conflicting(target: Target, log: Callable[[str], None]) -> bool:
    try:
        row = gh.pr_merge_details(target.pr, timeout=CONFLICT_SNAPSHOT_TIMEOUT, retry=False)
        return (row.get("state") == "OPEN" and row.get("headRefOid") == target.sha
                and (row.get("mergeable") == "CONFLICTING" or row.get("mergeStateStatus") == "DIRTY"))
    except READ_ERRORS as e:
        log(f"ci: could not read mergeability for {target.pr_name}: {util.first_line(str(e), 200)}")
        return False


def watch(target: Target, log: Callable[[str], None], *, reruns: Sequence[gh.WorkflowRerun] = ()) -> Watched:
    completed: dict[int, set[gh.CheckKey]] = {}
    exact: dict[gh.CheckKey, gh.Check] = {}
    rerun_reasons: dict[gh.CheckKey, str] = {}
    red_workflows: set[int] = set()

    def superseded_jobs(checks, newer_attempts, pr_runs, options):
        replaced = {r.run: r.check_keys | completed.get(r.run, set()) for r in reruns}
        keys = set().union(*replaced.values())
        job_ids = {job for r in reruns for job in r.jobs.values()} | {c.job for c in exact.values() if c.job}
        attempts = {r.run: r.attempt for r in reruns}
        for key in keys & checks.keys():
            check = checks[key]
            if check.job in job_ids:
                yield key
                continue
            if not check.job:
                continue
            try:
                job = gh.actions_job(target.github, target.sha, check.job, **options)
                owner = job.get("run_id")
                if job["name"] != check.name or type(owner) is not int or owner <= 0:
                    continue
                if owner in replaced:
                    attempt = job.get("run_attempt")
                    if key in replaced[owner] and type(attempt) is int and (
                            attempt < attempts[owner] or attempt <= newer_attempts.get(owner, 0)):
                        yield key
                elif owner not in pr_runs:
                    run = gh.workflow_run(target.github, target.sha, owner, **options)
                    pulls = run.get("pull_requests") or []
                    if run.get("event") == "pull_request" and pulls and all(
                            type(pull.get("number")) is int and pull["number"] != gh.pr_number(target.pr) for pull in pulls):
                        yield key
            except READ_ERRORS:
                continue

    def snapshot(*, timeout: float = 120, retry: bool = True):
        options = {"timeout": timeout, "retry": retry}
        checks = gh.commit_checks(target.github, target.sha, red_workflows=red_workflows, **options)
        runs = []
        for rerun in reruns:
            run = gh.workflow_run(target.github, target.sha, rerun.run, pr=target.pr,
                                  allow_head_branch=bool(rerun.unrelated), **options)
            runs.append((rerun, run))
            if rerun.run not in completed:
                attempt = gh.rerun_checks(target.github, target.pr, target.sha, rerun, run, **options)
                if attempt:
                    completed[rerun.run] = set(attempt)
                    for check in attempt.values():
                        if check.job:
                            check = replace(check, workflow_run_id=rerun.run)
                        exact[check.key] = check
                        if check.state == "fail" and rerun.unrelated:
                            if check.source == f"workflow:{rerun.run}":
                                rerun_reasons[check.key] = " ".join(dict.fromkeys(rerun.unrelated.values()))
                            elif check.name in rerun.unrelated:
                                rerun_reasons[check.key] = rerun.unrelated[check.name]
        awaiting_rerun = len(completed) < len(reruns)
        newer_attempts, pr_runs = {}, set()
        if reruns and not awaiting_rerun and not any(check.state == "fail" for check in exact.values()):
            latest = gh.latest_pr_workflows(target.github, target.pr, target.sha, **options)
            pr_runs = {run["id"] for run in latest.values()}
            for rerun, run in runs:
                listed = latest.get(run["workflow_id"], run)
                newest = max((run, listed), key=lambda row: (row["run_number"], row["run_attempt"]))
                if (newest["run_number"], newest["run_attempt"]) > (run["run_number"], rerun.attempt):
                    check = gh.workflow_check(newest)
                    if check.state != "pass":
                        checks[check.key] = check
                        newer_attempts[newest["id"]] = newest["run_attempt"]
        if reruns:
            for key in superseded_jobs(checks, newer_attempts, pr_runs, options):
                del checks[key]
            exact_jobs = {c.job for c in exact.values()}
            checks = {key: c for key, c in checks.items() if not c.actions_only or c.job not in exact_jobs}
        return checks | exact, awaiting_rerun

    def watched(checks: dict[gh.CheckKey, gh.Check], *, read=True, conflict=False) -> Watched:
        return Watched(target, checks | exact, read=read, conflicting=conflict,
                       rerun_reds={key for key, check in exact.items() if check.state == "fail"},
                       rerun_reasons=rerun_reasons, red_workflows=red_workflows)

    log(f"ci: waiting on the checks of {target.pr_name} at {target.sha[:7]}")
    checks: dict[gh.CheckKey, gh.Check] = {}
    awaiting_rerun = bool(reruns)
    waiting_on: list[str] | None = None
    while True:
        if conflicting(target, log):
            log(f"ci: stopped waiting on {target.pr_name} because GitHub reports a merge conflict with the base")
            try:
                latest, awaiting_rerun = snapshot(timeout=CONFLICT_SNAPSHOT_TIMEOUT, retry=False)
            except READ_ERRORS as e:
                log(f"ci: reading the checks of {target.pr_name} failed: {util.first_line(str(e), 200)}")
                return watched({}, read=False, conflict=True)
            pending = bool(reruns) and any(c.state == "pending" for c in latest.values())
            return watched(latest, read=not awaiting_rerun and not pending, conflict=True)
        try:
            checks, awaiting_rerun = snapshot()
            just_read = True
        except READ_ERRORS as e:
            just_read = False
            log(f"ci: reading the checks of {target.pr_name} failed, trying again: {util.first_line(str(e), 200)}")
        waited = time.monotonic() - target.pushed_at
        pending = sorted(c.name for c in checks.values() if c.state == "pending")
        if checks and pending != waiting_on:
            log(f"ci: {target.pr_name} {len(checks) - len(pending)} of {util.count(len(checks), 'check')} done"
                + (f", waiting on {', '.join(pending)}" if pending else ""))
            waiting_on = pending
        if just_read and checks and not pending and not awaiting_rerun and waited >= SETTLE_SECONDS:
            return watched(checks)
        if just_read and not reruns and not checks and waited >= FIRST_CHECK_SECONDS:
            log(f"ci: no checks on {target.pr_name} within {minutes(FIRST_CHECK_SECONDS)} of the push")
            return Watched(target, checks)
        if waited >= WAIT_SECONDS:
            if just_read and awaiting_rerun:
                log(f"ci: {target.pr_name} did not report completed rerun checks after {minutes(WAIT_SECONDS)}")
                return watched(checks, read=False)
            log(f"ci: {target.pr_name} still waiting on {', '.join(pending)} after {minutes(WAIT_SECONDS)}" if just_read
                else f"ci: could not read the checks of {target.pr_name} within {minutes(WAIT_SECONDS)}")
            return watched(checks, read=just_read and not (reruns and pending))
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
    w.reds = [Red(t, c, on_base.get(c.context_key), base_sha) for c in failed]
    for r in w.reds:
        if r.check.key in w.rerun_reasons:
            r.caused, r.reason = False, w.rerun_reasons[r.check.key]
        log(f"ci: {r.label} failed; it {r.base_result.replace('`', '')}")
    return w


def failure_note(r: Red, log_file: Path) -> str:
    lines = [f"### {r.label}", "", f"- Link: {r.check.link or 'none'}", f"- This check {r.base_result}."]
    if r.kind == "not caused":
        lines.append(f"- Arete judged this failure unrelated to the PR. {r.reason}")
    if r.check.description:
        lines.append(f"- GitHub says: {r.check.description}")
    if not r.check.job:
        if r.kind == "unjudged":
            lines.append("- This check has no Actions log. Judge it from its name, what GitHub says, and the diff.")
        return "\n".join(lines)
    try:
        text = gh.job_log(r.target.github, r.check.job)
    except gh.GhError as e:
        return "\n".join([*lines, f"- Arete could not read its log: {util.first_line(str(e), 300)}"])
    log_file.write_text(text)
    snippet, has_timeout_report = _log_excerpt(text)
    heading = "Log excerpt:" if has_timeout_report else "The log up to its last error:"
    return "\n".join([*lines, f"- Full log: `{log_file}`", "", heading, "", "````", snippet, "````"])


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


def timeout_excerpt(report: str, limit: int) -> str:
    if len(report) <= limit:
        return report
    body = report[report.index("\n") + 1:report.rindex("\n")]
    identity, *stacks = PYTHON_STACK.split(body)
    sections = [util.both_ends(identity.rstrip(), limit)]
    remaining = limit - len(sections[0])
    traces = []
    for heading, stack in zip(stacks[::2], stacks[1::2], strict=True):
        thread = stack.replace("\r\n", "\n").partition("\n\n")[0]
        frames = [line for line in thread.splitlines() if line.startswith("  File ")][:6]
        if frames:
            traces.append((heading, frames))
    traces = traces[:max(1, remaining // 140)]
    for index, (heading, frames) in enumerate(traces):
        room = remaining // (len(traces) - index) - len(heading) - 2
        if room < 8:
            break
        preview = [util.both_ends(frames[0], min(room, 220))]
        room -= len(preview[0])
        for frame in frames[1:]:
            frame = util.both_ends(frame, 220)
            if len(frame) + 1 > room:
                break
            preview.append(frame)
            room -= len(frame) + 1
        sections.append("\n".join([heading, *preview]))
        remaining -= len(sections[-1]) + 1
    return "\n".join(sections)


def timeout_summary(reports: list[str]) -> str:
    summary = "\n\n".join(reports)
    if len(summary) <= LOG_EXCERPT_CHARS:
        return summary
    remaining = LOG_EXCERPT_CHARS + 2
    summaries = [""] * len(reports)
    for index in sorted(range(len(reports)), key=lambda index: len(reports[index])):
        size = len(reports[index]) + 2
        if size > remaining:
            break
        summaries[index] = reports[index]
        remaining -= size

    overflow = [index for index, report in enumerate(summaries) if not report]
    notice = ""
    minimum = MIN_TIMEOUT_REPORT_CHARS + 2
    if remaining < len(overflow) * minimum:
        notice_budget = len(f"... {len(overflow)} timeout reports omitted") + 2
        if remaining < notice_budget:
            index = max(range(len(summaries)), key=lambda index: len(summaries[index]))
            report = summaries[index]
            summaries[index] = util.both_ends(report, len(report) - (notice_budget - remaining))
            remaining += len(report) - len(summaries[index])
        remaining -= notice_budget
        count = remaining // minimum
        notice = f"... {len(overflow) - count} timeout reports omitted"
        overflow = overflow[len(overflow) - count:]

    for offset, index in enumerate(overflow):
        limit = remaining // (len(overflow) - offset) - 2
        summaries[index] = timeout_excerpt(reports[index], limit)
        remaining -= len(summaries[index]) + 2
    return "\n\n".join(filter(None, (*summaries, notice)))


def excerpt(log_text: str) -> str:
    return _log_excerpt(log_text)[0]


def _log_excerpt(log_text: str) -> tuple[str, bool]:
    text = LOG_TIMESTAMP.sub("", log_text)
    reports = TEST_TIMEOUT.findall(text)
    if reports and len(text) <= LOG_EXCERPT_CHARS:
        return text, True
    surrounding = TEST_TIMEOUT.sub("", text).strip() if reports else text
    last_error = surrounding.rfind("##[error]")
    if last_error >= 0:
        line_end = surrounding.find("\n", last_error)
        if line_end >= 0:
            surrounding = surrounding[:line_end]
    if not reports:
        if len(surrounding) > LOG_EXCERPT_CHARS:
            surrounding = util.tail(surrounding, LOG_EXCERPT_CHARS - len("...\n"))
        return surrounding, False
    summary = timeout_summary(reports)
    remaining = LOG_EXCERPT_CHARS - len(summary) - 2
    if remaining < 8:
        return summary, True
    if last_error >= 0 and len(surrounding) - last_error > remaining:
        surrounding = util.both_ends(surrounding[last_error:], remaining)
    elif len(surrounding) > remaining:
        surrounding = util.tail(surrounding, remaining - len("...\n"))
    kept_reports, _, notice = summary.rpartition("\n\n")
    if TIMEOUT_OMISSION.fullmatch(notice):
        return "\n\n".join(filter(None, (kept_reports, surrounding, notice))), True
    return "\n\n".join(filter(None, (summary, surrounding))), True


def section(rounds: list[Round], max_rounds: int, ending: str = "") -> str:
    lines = [SECTION_START, "## CI", ""]
    for last in rounds[-1:]:
        for w in last.seen:
            lines += [f"[{w.target.pr_name}]({w.target.pr}) at `{w.target.sha[:7]}`, round {last.n} of {max_rounds}:",
                      "", *w.markdown(), ""]
        if last.story:
            lines += [last.story, ""]
    if ending:
        lines += [ending, ""]
    if len(rounds) > 1:
        lines += ["Earlier rounds:", "", *(f"- Round {r.n}: {r.story}" for r in rounds[:-1]), ""]
    return "\n".join([*lines, SECTION_END])


def with_section(body: str, ci_section: str) -> str:
    return f"{SECTION.sub('', body).rstrip()}\n\n{ci_section}"


def stopped_section(body: str) -> str:
    found = SECTION.search(body or "")
    return found.group(0).strip() if found and STOPPED in found.group(0) else ""


def stopped(body: str) -> bool:
    return bool(stopped_section(body))
