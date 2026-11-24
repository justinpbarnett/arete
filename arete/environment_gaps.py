from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache

from . import ci, codebase, config, control, factory, gh, outbound, paths, pipeline, run_lines, tracker, util, vault
from .store import db
from .store import runs as run_store
from .store import ticket_progress

STEP_PATHS = ("feature", "patch")
RESUME_PATHS = (*STEP_PATHS, "classic", "orchestrate")
TOOL = r"[a-zA-Z][a-zA-Z0-9_-]{0,63}"
EXIT_127 = r"\bexit(?:ed)?(?:\s+with)?(?:\s+(?:exit\s+)?(?:code|status))?\s*[:=]?\s*127(?![\w.])"
GIT_COMMAND = re.compile(rf"['‘\x60](?P<tool>{TOOL})['’\x60] is not a git command\b", re.IGNORECASE)
MISSING_TOOL = (
    re.compile(rf"{EXIT_127}\s*:[^\n]*?(?<![\w/.-])(?P<tool>{TOOL}): (?:command )?not found\b", re.IGNORECASE),
    re.compile(rf"{EXIT_127}\s*:[^\n]*?command not found:\s*(?P<tool>{TOOL})(?![\w/.-])", re.IGNORECASE),
    re.compile(rf"{EXIT_127}\s+for\s+(?P<quote>['\x22\x60])(?P<tool>{TOOL})(?P=quote)", re.IGNORECASE),
    re.compile(rf"(?P<quote>['\x22\x60])(?P<tool>{TOOL})(?:[ \t]+[^'\x22\x60\n]+)?(?P=quote)\s+{EXIT_127}",
               re.IGNORECASE),
)
PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)/?")


def normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def safe_text(text: str) -> str:
    return outbound.screen(outbound.redact(vault.hidden(text)))


@dataclass(frozen=True)
class Signature:
    error: str
    tool: str

    @property
    def key(self) -> str:
        return digest(f"{self.error}\n{self.tool}")

    @property
    def title(self) -> str:
        if self.error == "is not a git command":
            return f"'{self.tool}' {self.error}"
        return self.error if self.error.startswith(f"{self.tool}:") else f"{self.error} for '{self.tool}'"


def signatures(text: str) -> set[Signature]:
    found = set()
    if re.search(r"\bsandbox_apply:\s*Operation not permitted\b", text, re.IGNORECASE):
        found.add(Signature("sandbox_apply: Operation not permitted", "sandbox_apply"))
    found.update(Signature("is not a git command", m["tool"].casefold()) for m in GIT_COMMAND.finditer(text))
    found.update(Signature("exit 127", m["tool"].casefold()) for pattern in MISSING_TOOL for m in pattern.finditer(text))
    return found


@dataclass(frozen=True)
class Gap:
    signature: Signature
    what: str
    how: str
    saw: str


def check_signature(check: dict) -> Signature | None:
    if normalized(str(check.get("cause") or "")) == "environment":
        matches = signatures(safe_text(str(check.get("saw") or "")))
        if len(matches) == 1:
            return matches.pop()
    return None


def gaps(data: dict) -> list[Gap]:
    if data.get("verdict") != "blocked":
        return []
    failed = [c for c in data.get("checks", []) if not c.get("passed")
              or normalized(str(c.get("cause") or "")) == "environment"]
    found = {}
    for check in failed:
        if not (signature := check_signature(check)):
            return []
        what, how, saw = (safe_text(str(check.get(field) or "")).strip() for field in ("what", "how", "saw"))
        found.setdefault(signature, Gap(signature, what, how, saw))
    return list(found.values())


def last_data(lines: list[dict], step: str) -> dict:
    return next((factory.outcome_of(line).data for line in reversed(lines) if line.get("step") == step), {})


def was_retry(row: dict) -> bool:
    return bool(row.get("environment_retry_of") and not row.get("dry_run") and row.get("status") != "dry-run")


def verification_action(run_id: str) -> str:
    from .store import orchestration

    row = orchestration.find(run_id)
    if not row or row["current_run"] != run_id:
        return ""
    return row["data"].get("evidence", {}).get("verify", {}).get("producer") or ""


@dataclass(frozen=True)
class Hold:
    gaps: list[Gap]
    heads: dict[str, str]


def decide_hold(publication: dict, review: dict) -> Hold | None:
    if not publication.get("draft_why") or publication.get("ci_stopped") or review.get("verdict") != "approve":
        return None
    found = gaps(publication.get("verified") or {})
    heads = {url: (publication.get("heads") or {}).get(repo) for repo, url in (publication.get("prs") or {}).items()}
    if found and heads and all(head and PR_URL.fullmatch(url) for url, head in heads.items()):
        return Hold(found, heads)
    return None


def hold_of(run_id: str) -> Hold | None:
    chain = factory.chain(run_id)
    own = factory.ends(chain[0])
    classic = factory.first(chain[0], "classic_hold")
    publication = classic.get("publication") or last_data(own, "pr")
    review = classic.get("review") or last_data(factory.history(chain), "review").get("review") or {}
    return decide_hold(publication, review)


def save_hold(run_id: str, pr_urls: str) -> None:
    if (hold := hold_of(run_id)) and set(pr_urls.split()) == hold.heads.keys():
        run_store.save_environment_hold(run_id, [
            (gh.pr_repo(url).casefold(), gh.pr_number(url), url, head) for url, head in hold.heads.items()])


def owned_drafts(row: dict, hold: Hold) -> bool:
    if set(row["pr_url"].split()) != hold.heads.keys():
        return False
    for url, head in hold.heads.items():
        pr = gh.gh_json(["pr", "view", url, "--json", "state,isDraft,headRefOid,body"]) or {}
        if (pr.get("state") != "OPEN" or pr.get("isDraft") is not True or pr.get("headRefOid") != head
                or ci.stopped(pr.get("body") or "")):
            return False
    return True


def owner_discussion(talk: gh.Discussion, owners: set[str], since: str) -> bool:
    reviews = [r for r in talk.reviews if r.get("state") != "PENDING"]
    pending = {r["id"] for r in talk.reviews if r.get("state") == "PENDING"}
    comments = [c for c in talk.line_comments if c.get("pull_request_review_id") not in pending] + talk.comments
    return any(((item.get("user") or {}).get("login") or "").casefold() in owners
               and not gh.has_arete_mark(item.get("body"))
               and (item.get("submitted_at") or item.get("created_at") or "") >= since
               for item in [*reviews, *comments])


def owner_intervened(row: dict, source: tracker.Tracker, talks: dict[str, gh.Discussion]) -> bool:
    owner = gh.me().casefold()
    if not owner:
        raise gh.GhError("GitHub could not identify the account that handles PR feedback")
    for url, talk in talks.items():
        owners = {owner, gh.pr_repo(url).split("/")[0].casefold()}
        if isinstance(source, tracker.GitHubTracker):
            owners.add(source.owner.casefold())
        if owner_discussion(talk, owners, row["started"]):
            return True
    return False


def discussions(row: dict, hold: Hold, source: tracker.Tracker) -> dict[str, gh.Discussion]:
    if pipeline.pending_feedback(row["repo"], str(row["issue"])) or not owned_drafts(row, hold):
        return {}
    talks = {url: gh.pr_discussion(url) for url in hold.heads}
    if owner_intervened(row, source, talks):
        return {}
    return talks


def live_release() -> str:
    beat = control.serve_beat()
    sha = beat.get("release") or ""
    return sha if (re.fullmatch(r"[0-9a-f]{40}", sha) and beat.get("alive") and beat.get("tick_ok")
                   and beat.get("engine") == codebase.STAMP == sha[:12] and codebase.RELEASE == sha
                   and beat.get("checkout_head") == codebase.checkout_head() == sha[:12]
                   and not any(beat.get(key) for key in ("code_problem", "config_problem", "migration_error"))) else ""


def release_repo(cfg: config.Config) -> config.Repo | None:
    return next((repo for repo in cfg.repos if repo.name == cfg.arete_repo and repo.github), None)


def release_base(repo: config.Repo) -> str:
    return config.load_layer(repo.layer_dir).base_branch or gh.default_branch(repo.github)


def release_contains_fixes(repo: config.Repo, numbers: list[int], sha: str) -> bool:
    base = release_base(repo)
    fixes = [gh.issue_merge_commit(repo.github, n, base) for n in numbers]
    return bool(fixes and all(fixes) and gh.branch_contains(repo.github, base, sha)
                and all(gh.branch_contains(repo.github, sha, fix) for fix in fixes))


def indented(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())


def issue_body(gap: Gap, run_id: str, include_check: bool) -> str:
    signature = gap.signature
    body = (f"Verification was blocked by the environment in run `{run_id}`.\n\n"
            f"Signature: `{signature.error}`\n\nTool: `{signature.tool}`")
    if include_check:
        body += "\n\n" + "\n\n".join(f"{name}\n\n{indented(value)}" for name, value in (
            ("What", gap.what), ("How", gap.how), ("Saw", gap.saw)))
    return body


class LinkedIssueUnreadable(gh.GhError):
    pass


def issue_for(release: config.Repo, own: tuple[str, int] | None, run_id: str, gap: Gap, previous: int | None,
              include_check: bool, dry_run: bool) -> int | None:
    repo = release.github.casefold()
    signature = gap.signature

    def eligible(issue: dict) -> bool:
        number = issue.get("number")
        return (type(number) is int and number > 0 and (repo, number) != own
                and own not in tracker.github_waits(issue.get("body") or "", repo)
                and any(label.get("name") == tracker.LABEL for label in issue.get("labels", [])))

    if previous:
        try:
            current = gh.issue(release.github, previous) or {}
            state = current.get("state")
            if state not in ("OPEN", "CLOSED"):
                raise gh.GhError("Missing issue state")
            if (state == "OPEN" and eligible(current)) or (state == "CLOSED"
                    and gh.issue_merge_commit(release.github, previous, release_base(release))):
                return previous
        except (gh.GhError, json.JSONDecodeError) as error:
            raise LinkedIssueUnreadable(f"Could not confirm {repo}#{previous}") from error

    def matches(issue: dict) -> bool:
        title, body = issue.get("title") or "", issue.get("body") or ""
        recorded = f"Signature: `{signature.error}`\n\nTool: `{signature.tool}`"
        return (eligible(issue) and (signature in signatures(title) or recorded in body
                                     or signature in signatures(body)))

    opened = gh.issues(release.github, tracker.LABEL, limit=tracker.MAX_AGENT_ISSUES)
    if number := min((i["number"] for i in opened if matches(i)), default=None):
        return number
    if dry_run:
        return None
    day = util.now().date().isoformat()
    key = f"environment-gap-{signature.key}-{day}"
    if url := gh.find_issue_marker(release.github, gh.key_mark(key)):
        issue = gh.issue(release.github, int(url.rstrip("/").rsplit("/", 1)[-1]))
        return issue["number"] if issue and issue.get("state") == "OPEN" and matches(issue) else None
    if not run_store.claim_environment_gap(repo, signature.key, day):
        return None
    try:
        return gh.issue_create(release.github, f"Verification environment gap — {signature.title}",
                               issue_body(gap, run_id, include_check), labels=[tracker.LABEL],
                               key=key, single_attempt=True)
    except gh.GhUnsure:
        raise
    except gh.GhError:
        run_store.unclaim_environment_gap(repo, signature.key, day)
        raise


def notice(url: str, talk: gh.Discussion, event: str, body: str, *, legacy: str = "") -> None:
    keys = ["environment-gap-" + digest(f"{url}\n{item}") for item in (event, legacy) if item]
    markers = {gh.key_mark(key) for key in keys}
    if not any(markers.intersection((row.get("body") or "").splitlines()) for row in talk.comments):
        gh.pr_comment(url, body, key=keys[0], single_attempt=True)


def open_ticket(source: tracker.GitHubTracker, number: int) -> dict:
    ticket = gh.issue(source.repo.github, number) or {}
    labels = {label["name"] for label in ticket.get("labels", [])}
    if (ticket.get("state") == "OPEN" and source.by_owner(ticket) and tracker.LABEL in labels
            and not labels & {tracker.ZTE_LABEL, tracker.BATCH_LABEL}):
        return ticket
    return {}


def ticket_gate(source: tracker.Tracker, tid: str) -> tuple[str | None, list[tuple[str, int]]]:
    if isinstance(source, tracker.GitHubTracker):
        if not re.fullmatch(r"[1-9][0-9]*", tid) or not (ticket := open_ticket(source, int(tid))):
            return None, []
        refs = tracker.github_waits(ticket.get("body") or "", source.repo.github)
        return source.wait_blocker(refs), refs
    ticket = source.get(tid, include_environment=False)
    return (ticket.blocker if ticket.open else None), []


def linked_numbers(hold: Hold, waits: dict, repo: str) -> list[int]:
    if any(not (ref := waits.get(gap.signature.key)) or ref[0] != repo.casefold() for gap in hold.gaps):
        return []
    return sorted({waits[gap.signature.key][1] for gap in hold.gaps})


def confirmed_retry_gaps(cfg: config.Config, repo: config.Repo, row: dict, sha: str) -> tuple[tuple[str, int], ...]:
    tid = str(row.get("issue") or "")
    if (not tid or row.get("repo") != repo.name or row.get("status") != "pr"
            or row.get("dry_run") or was_retry(row) or live_release() != sha):
        return ()
    run_id = row["id"]
    saved = run_store.environment_hold(run_id)
    release, hold = release_repo(cfg), hold_of(run_id)
    if (not release or not hold or saved.get("released") != 0 or saved["heads"] != hold.heads
            or run_store.environment_retry(run_id)):
        return ()
    source = tracker.for_ticket(cfg, repo, tid)
    if ticket_gate(source, tid)[0] != "" or not discussions(row, hold, source):
        return ()
    numbers = linked_numbers(hold, saved["waits"], release.github)
    try:
        if not release_contains_fixes(release, numbers, sha):
            return ()
    except gh.GhError:
        return ()
    return tuple((release.github.casefold(), number) for number in numbers)


def displayed_refs(repo: str, gaps_repo: str, numbers: list[int]) -> str:
    prefix = "" if repo.casefold() == gaps_repo.casefold() else gaps_repo
    return tracker.joined([f"{prefix}#{n}" for n in numbers])


def prerequisite_reads(release: config.Repo | None, refs: set[tuple[str, int]], follow_up_urls: Iterable[str],
                       sha: str) -> dict:
    @cache
    def live_base() -> str:
        try:
            base = release_base(release)
            return base if gh.branch_contains(release.github, base, sha) else ""
        except gh.GhError:
            return ""

    @cache
    def deployed(repo: str, base: str, commit: str) -> str:
        if release and sha and commit and repo == release.github.casefold() and base and base == live_base():
            try:
                return sha if gh.branch_contains(release.github, sha, commit) else ""
            except gh.GhError:
                pass
        return ""

    follow_up = {(gh.pr_repo(url).casefold(), gh.pr_number(url)) for url in follow_up_urls if PR_URL.fullmatch(url)}

    def read(repo: str, number: int) -> list[dict] | None:
        try:
            pulls = gh.prerequisite_pulls(repo, number)
        except gh.GhError:
            return None
        return [dict(key=f"{name}#{n}", url=f"https://github.com/{name}/pull/{n}", state=state,
                     merge_commit=commit, deployed_release=deployed(name, base, commit))
                for (name, n), (state, base, commit) in sorted(pulls.items()) if (name, n) not in follow_up]

    return {f"{repo}#{number}": read(repo, number) for repo, number in sorted(refs)}


def observe_dependencies(cfg: config.Config, source: tracker.GitHubTracker) -> None:
    repo = source.repo
    held = {str(row["issue"]) for row in run_store.environment_holds(repo.name) if not row["released"]}
    release = release_repo(cfg)
    saved = set(ticket_progress.declared_prerequisite_tickets(repo.name))
    for tid in sorted(saved - held - {ticket.id for ticket in source.open_agent_tickets}):
        if source.is_open(int(tid)) is False:
            ticket_progress.record(repo.name, tid, prerequisite_reads={}, declared=True)
    sha = None
    for ticket in source.dependency_tickets:
        if ticket.id in held:
            continue
        refs = set(tracker.github_waits(ticket.body, repo.github)) - {(repo.github.casefold(), int(ticket.id))}
        if not refs and ticket.id not in saved:
            continue
        if sha is None:
            sha = live_release() if release else ""
        newest = run_store.newest_live_whole_run(repo.name, ticket.id) or {}
        reads = prerequisite_reads(release, refs, (newest.get("pr_url") or "").split(), sha)
        ticket_progress.record(repo.name, ticket.id, prerequisite_reads=reads, declared=True)


def progress(row: dict, hold: Hold, state: str, next_event: str, *, reads: dict | None = None,
             declared: Iterable[tuple[str, int]] | None = None, retry_gaps: set[Signature] | None = None,
             blocker: dict | None = None) -> None:
    run_id, retry_id = row["id"], row.get("retry_id") or ""
    signatures = {g.signature for g in hold.gaps}
    current = signatures if state == "waiting" else retry_gaps or set()
    evidence = [(sig.key, attempt, f"Verification hit the same blocker again: {sig.title}.")
                for attempt, found in ((run_id, signatures), (retry_id, retry_gaps or ()))
                for sig in sorted(found, key=lambda sig: sig.key)]
    if current and not blocker:
        blocker = {"kind": "verification-environment", "causes": {s.key: s.title for s in current}}
    ticket_progress.record(row["repo"], row["issue"], {
        "run": run_id, "attempt": retry_id or run_id,
        "blocker": blocker,
        "next_event": next_event,
        "follow_up": {"state": state, "prs": sorted(hold.heads), "draft": True if state != "inactive" else None},
    }, evidence=evidence, prerequisite_reads=reads, only_current=state == "inactive",
        declared_refs=None if declared is None else {f"{repo}#{number}" for repo, number in declared})


def retry_notice(row: dict, talks: dict[str, gh.Discussion]) -> None:
    retry_id = row["retry_id"]
    status = ticket_progress.get(row["repo"], row["issue"])
    blocker = status.get("blocker") if status.get("attempt") == retry_id else None
    reason = "; ".join((blocker or {}).get("causes", {}).values())
    detail = f" {reason.rstrip('.')}." if reason else ""
    body = (f"Automatic verification retry `{retry_id}` did not clear the draft on the live release.{detail} "
            "This PR stays a draft and needs owner review.")
    for url, talk in talks.items():
        notice(url, talk, f"{retry_id}-still-held", body, legacy=f"{retry_id}-retry")


def retry_blocker(retry: dict) -> tuple[dict | None, set[Signature]]:
    reason = util.first_line(safe_text(retry.get("summary") or ""), 300)
    if reason == "verification retry requires owner input":
        return {"kind": "owner-input", "causes": {"owner": reason}}, set()
    lines, finished = run_lines.read(retry["id"]), {}
    for line in reversed(factory.ends(lines)):
        step = line.get("step")
        if not line.get("carried") and step in ("verify", "checks", "review", "pr") and step not in finished:
            finished[step] = factory.outcome_of(line)
            if step == "verify":
                break
    verification = finished.get("verify")
    if verification and verification.data.get("ending") == "environment-held":
        checks = (verification.data.get("verified") or {}).get("checks", [])
        found = {sig for check in checks if (sig := check_signature(check))}
        return (None if found else dict(kind="verification", causes={"verify": "Verification did not pass"})), found
    if not verification or not verification.passed:
        return None, set()
    if (checks := finished.get("checks")) and not checks.passed:
        return {"kind": "configured-checks", "causes": {"checks": "Configured checks did not pass"}}, set()
    if (review := finished.get("review")) and (
            not review.passed or (review.data.get("review") or {}).get("verdict") not in (None, "approve")):
        publication = finished.get("pr")
        held = factory.first(lines, "report").get("held") or (publication and publication.data.get("draft_why"))
        detail = util.first_line(safe_text(held), 300) if held else reason
        return {"kind": "review", "causes": {"review": detail or "Review did not approve the draft"}}, set()
    return None, set()


def finish_retry(row: dict, hold: Hold, reads: dict, declared: list[tuple[str, int]],
                 talks: dict[str, gh.Discussion]) -> None:
    if row["retry_status"] == "running":
        progress(row, hold, "verifying", "Automatic verification finishes", reads=reads, declared=declared)
        return
    retry = run_store.run_get(row["retry_id"]) or {}
    if (ending := retry.get("status")) in ticket_progress.INTERRUPTED_RETRY_STATUSES:
        newest = run_store.newest_live_whole_run(row["repo"], row["issue"]) or {}
        if newest.get("id") == row["retry_id"]:
            blocker = {"kind": "retry-interrupted", "causes": {"retry": f"Automatic verification retry was {ending}"}}
            event = "Owner reviews the draft and decides whether to verify it manually or take over the PR"
            progress(row, hold, "owner-review", event, blocker=blocker, reads=reads, declared=declared)
        return
    owner_review = (retry.get("status") == "pr" and set((retry.get("pr_url") or "").split()) == hold.heads.keys()
                    and run_store.environment_owner_review_active(row))
    state = "owner-review" if owner_review else "retry-finished"
    blocker, found = retry_blocker(retry) if owner_review else (None, set())
    event = "Owner reviews the draft and decides how to proceed" if owner_review else ""
    if blocker and blocker["kind"] == "owner-input":
        event = "Owner reviews the draft and decides whether to provide the missing input or take over"
    with db.tx():
        progress(row, hold, state, event, blocker=blocker, retry_gaps=found, reads=reads, declared=declared)
        run_store.release_environment_hold(row["id"], state)
    if owner_review:
        retry_notice(row, talks)


def close_hold(row: dict, heads: dict[str, str]) -> None:
    with db.tx():
        if row["released"]:
            run_store.end_environment_owner_review(row["id"])
        else:
            run_store.release_environment_hold(row["id"])
        progress(row, Hold([], heads), "inactive", "")


def next_hold_event(blocker: str, scope: str, numbers: list[int], reads: dict, ready: bool | None) -> str:
    if blocker:
        return f"Tracker prerequisite is resolved ({safe_text(blocker)})"
    if ready:
        return "Automatic verification retry starts when eligible"
    required = [reads[f"{scope}#{n}"] for n in numbers]
    if ready is None or None in required:
        return "The prerequisite PR state can be confirmed"
    if not required:
        return "A prerequisite issue is linked to each verification gap"
    if not all(any(pr["state"] == "merged" for pr in group) for group in required):
        return "A fix for each verification gap is confirmed merged"
    if not all(any(pr["deployed_release"] for pr in group) for group in required):
        return "The live release is confirmed to contain the fixes"
    return "Closure by fixes on the live release can be confirmed"


def handle(cfg: config.Config, repo: config.Repo, row: dict, dry_run: bool, *, can_start: bool,
           slot_blocker: str = "") -> bool:
    from . import flows, oncall, pilot, runlimits, slots, tasks, workspace, zte

    run_id, tid = row["id"], str(row["issue"])
    saved = run_store.environment_hold(run_id)
    if not saved:
        return False
    reviewing = saved["released"]
    if reviewing:
        if dry_run or saved["release_state"] != "owner-review" or saved["owner_review_ended_at"]:
            return False
        inactive = not run_store.environment_owner_review_active(row)
    else:
        inactive = row.get("files_purged") or (not row["retry_id"] and row["status"] != "pr")
    if inactive:
        if not dry_run:
            close_hold(row, saved["heads"])
        return False
    release, hold = (None, Hold([], saved["heads"])) if reviewing else (release_repo(cfg), hold_of(run_id))
    if not hold or saved["heads"] != hold.heads or not (reviewing or release):
        return False
    source = tracker.for_ticket(cfg, repo, tid)
    blocker, declared = ticket_gate(source, tid)
    talks = discussions(row, hold, source) if blocker is not None else {}
    if not talks:
        if not dry_run:
            close_hold(row, saved["heads"])
        return False
    if reviewing:
        if saved["released_at"]:
            retry_notice(row, talks)
        return False
    retry_id = row["retry_id"]
    if not retry_id and run_store.later_run(repo.name, tid, run_id, live=True):
        return False
    waits = {key: tuple(ref) for key, ref in saved["waits"].items()}
    path = None if retry_id else pipeline.recorded_path(run_id)
    if (retry_id or path not in RESUME_PATHS or tid in oncall.parked(repo.name) or pilot.is_pilot_task(tid)
            or zte.reserved(tid)
            or tasks.pending_start(repo.name, tid)
            or (path in STEP_PATHS and cfg.adws and cfg.adws.resolve() != paths.ADWS_DIR.resolve())):
        if not dry_run:
            reads = prerequisite_reads(release, set(declared) | set(waits.values()), hold.heads, live_release())
            if retry_id:
                finish_retry(row, hold, reads, declared, talks)
            else:
                progress(row, hold, "waiting", "The hold becomes eligible for recovery", reads=reads, declared=declared)
        return False
    scope = release.github.casefold()
    own = (repo.github.casefold(), int(tid)) if isinstance(source, tracker.GitHubTracker) else None
    include_check = bool(own and own[0] == scope and all(gh.pr_repo(url).casefold() == scope for url in hold.heads))
    ready: bool | None = False
    for gap in hold.gaps:
        key = gap.signature.key
        previous = waits.get(key)
        try:
            number = issue_for(release, own, run_id, gap, previous[1] if previous and previous[0] == scope else None,
                               include_check, dry_run)
        except LinkedIssueUnreadable:
            ready = None
            continue
        updated = (scope, number) if number else None
        if updated != previous:
            waits.pop(key, None)
            if updated:
                waits[key] = updated
            if not dry_run:
                run_store.set_environment_waits(run_id, waits)
    numbers = linked_numbers(hold, waits, scope)
    if not dry_run and numbers:
        event = f"{run_id}-wait-{scope}-{','.join(map(str, numbers))}"
        for url, talk in talks.items():
            refs = displayed_refs(gh.pr_repo(url), release.github, numbers)
            notice(url, talk, event, f"Verification is waiting on {refs} for the environment gap in run `{run_id}`. "
                   "This PR stays a draft until verification can run again.")
    sha = live_release() if not dry_run or can_start else ""
    if numbers and not blocker and sha and ready is False and (path != "orchestrate" or verification_action(run_id)):
        try:
            ready = release_contains_fixes(release, numbers, sha)
        except gh.GhError:
            ready = None
    gate, reason = "workspace", ""
    if ready:
        try:
            flows.check_environment_workspace(repo, row, hold.heads)
        except workspace.RunFailed as error:
            reason = util.first_line(safe_text(error.why), 300) or "The saved workspace cannot be restored"
            print(f"{util.iso()} {run_id}: verify retry still waiting for its workspace: {reason}")
    reads = {} if dry_run else prerequisite_reads(release, set(declared) | set(waits.values()), hold.heads, sha)
    try:
        if not ready or reason or not (can_start or slot_blocker):
            return False
        layer = config.load_layer(repo.layer_dir)
        gate, reason = "admission", runlimits.admission(repo, tid, source, runlimits.Policy.of(layer), False).reason
        if reason:
            return False
        gate, reason = "slot", "" if can_start else slot_blocker
        if reason:
            return False
        if not dry_run and tasks.resume(run_id, "verify", row["version"], environment_release=sha).get("state") == "queued":
            reason = tasks.NO_SLOT_AT_START
            return False
    except slots.NoFreeSlot as error:
        gate, reason = "slot", str(error) or slots.no_free_slot(cfg.max_parallel)
        return False
    except control.Busy:
        return False
    finally:
        if not dry_run:
            if reason:
                event = {"slot": "A run slot becomes available",
                         "workspace": "The saved workspace becomes restorable or the owner takes over the draft",
                         "admission": "The run budget hold clears or the owner takes over the draft"}[gate]
                detail = {"kind": f"retry-{gate}", "causes": {gate: reason if gate == "workspace" else safe_text(reason)}}
            else:
                event, detail = next_hold_event(blocker, scope, numbers, reads, ready), None
            progress(row, hold, "waiting", event, reads=reads, declared=declared, blocker=detail)
    refs = displayed_refs(repo.github, release.github, numbers)
    print(f"{util.iso()} {repo.name}#{tid}: {'would resume' if dry_run else 'resuming'} {run_id} from verify "
          f"because {refs} merged and live release {sha[:12]} contains the fix")
    return True
