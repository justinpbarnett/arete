from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from . import ci, codebase, config, control, factory, gh, outbound, paths, pipeline, run_lines, tracker, util, vault
from .store import runs as run_store

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
        current = gh.issue(release.github, previous)
        state = (current or {}).get("state")
        if (state == "OPEN" and eligible(current)) or (state == "CLOSED"
                and gh.issue_merge_commit(release.github, previous, release_base(release))):
            return previous

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


def notice(url: str, talk: gh.Discussion, event: str, body: str) -> None:
    key = "environment-gap-" + digest(f"{url}\n{event}")
    marker = gh.key_mark(key)
    if not any(marker in (row.get("body") or "").splitlines() for row in talk.comments):
        gh.pr_comment(url, body, key=key, single_attempt=True)


def open_ticket(source: tracker.GitHubTracker, number: int) -> dict:
    ticket = gh.issue(source.repo.github, number) or {}
    labels = {label["name"] for label in ticket.get("labels", [])}
    if (ticket.get("state") == "OPEN" and source.by_owner(ticket) and tracker.LABEL in labels
            and not labels & {tracker.ZTE_LABEL, tracker.BATCH_LABEL}):
        return ticket
    return {}


def ticket_blocker(source: tracker.Tracker, tid: str) -> str | None:
    if isinstance(source, tracker.GitHubTracker):
        if not re.fullmatch(r"[1-9][0-9]*", tid) or not (ticket := open_ticket(source, int(tid))):
            return None
        return source.wait_blocker(tracker.github_waits(ticket.get("body") or "", source.repo.github))
    ticket = source.get(tid, include_environment=False)
    return ticket.blocker if ticket.open else None


def linked_numbers(hold: Hold, waits: dict, repo: str) -> list[int]:
    if any(not (ref := waits.get(gap.signature.key)) or ref[0] != repo.casefold() for gap in hold.gaps):
        return []
    return sorted({waits[gap.signature.key][1] for gap in hold.gaps})


def confirm_retry(cfg: config.Config, repo: config.Repo, row: dict, sha: str) -> bool:
    tid = str(row.get("issue") or "")
    if (not tid or row.get("repo") != repo.name or row.get("status") != "pr"
            or row.get("dry_run") or was_retry(row) or live_release() != sha):
        return False
    run_id = row["id"]
    saved = run_store.environment_hold(run_id)
    release, hold = release_repo(cfg), hold_of(run_id)
    if (not release or not hold or saved.get("released") != 0 or saved["heads"] != hold.heads
            or run_store.environment_retry(run_id)):
        return False
    source = tracker.for_ticket(cfg, repo, tid)
    if ticket_blocker(source, tid) != "" or not discussions(row, hold, source):
        return False
    numbers = linked_numbers(hold, saved["waits"], release.github)
    return release_contains_fixes(release, numbers, sha)


def displayed_refs(repo: str, gaps_repo: str, numbers: list[int]) -> str:
    prefix = "" if repo.casefold() == gaps_repo.casefold() else gaps_repo
    return tracker.joined([f"{prefix}#{n}" for n in numbers])


def finish_retry(row: dict, hold: Hold, talks: dict[str, gh.Discussion], dry_run: bool) -> None:
    if dry_run or row["retry_status"] == "running":
        return
    retry_id = row["retry_id"]
    outcome = last_data(factory.ends(run_lines.read(retry_id)), "verify")
    prior = {gap.signature for gap in hold.gaps}
    checks = (outcome.get("verified") or {}).get("checks", [])
    if outcome.get("ending") == "environment-held" and any(
            check_signature(check) in prior for check in checks):
        for url, talk in talks.items():
            notice(url, talk, f"{retry_id}-retry", f"Verification in run `{retry_id}` hit the same environment gap "
                   "after the automatic retry. The PR stays a draft for a person to check.")
    run_store.release_environment_hold(row["id"])


def handle(cfg: config.Config, repo: config.Repo, row: dict, dry_run: bool, *, can_start: bool) -> bool:
    from . import flows, oncall, pilot, runlimits, slots, tasks, workspace, zte

    run_id, tid = row["id"], str(row["issue"])
    if row.get("files_purged"):
        if not dry_run:
            run_store.release_environment_hold(run_id)
        return False
    release, hold = release_repo(cfg), hold_of(run_id)
    saved = run_store.environment_hold(run_id)
    if not release or not hold or saved.get("heads") != hold.heads:
        return False
    source = tracker.for_ticket(cfg, repo, tid)
    blocker = ticket_blocker(source, tid)
    talks = discussions(row, hold, source) if blocker is not None else {}
    if not talks:
        if not dry_run:
            run_store.release_environment_hold(run_id)
        return False
    if row["retry_id"]:
        finish_retry(row, hold, talks, dry_run)
        return False
    path = pipeline.recorded_path(run_id)
    if (path not in RESUME_PATHS or tid in oncall.parked(repo.name) or pilot.is_pilot_task(tid) or zte.reserved(tid)
            or tasks.pending_start(repo.name, tid)
            or (path in STEP_PATHS and cfg.adws and cfg.adws.resolve() != paths.ADWS_DIR.resolve())):
        return False
    if run_store.later_run(repo.name, tid, run_id, live=True):
        return False
    scope = release.github.casefold()
    own = (repo.github.casefold(), int(tid)) if isinstance(source, tracker.GitHubTracker) else None
    include_check = bool(own and own[0] == scope and all(gh.pr_repo(url).casefold() == scope for url in hold.heads))
    waits = {key: tuple(ref) for key, ref in saved["waits"].items()}
    for gap in hold.gaps:
        key = gap.signature.key
        previous = waits.get(key)
        number = issue_for(release, own, run_id, gap, previous[1] if previous and previous[0] == scope else None,
                           include_check, dry_run)
        updated = (scope, number) if number else None
        if updated != previous:
            waits.pop(key, None)
            if updated:
                waits[key] = updated
            if not dry_run:
                run_store.set_environment_waits(run_id, waits)
    numbers = linked_numbers(hold, waits, scope)
    if not numbers:
        return False
    if not dry_run:
        event = f"{run_id}-wait-{scope}-{','.join(map(str, numbers))}"
        for url, talk in talks.items():
            refs = displayed_refs(gh.pr_repo(url), release.github, numbers)
            notice(url, talk, event, f"Verification is waiting on {refs} for the environment gap in run `{run_id}`. "
                   "This PR stays a draft until verification can run again.")
    if not can_start or blocker:
        return False
    if not (sha := live_release()) or (path == "orchestrate" and not verification_action(run_id)):
        return False
    if not release_contains_fixes(release, numbers, sha):
        return False
    try:
        flows.check_environment_workspace(repo, row, hold.heads)
    except workspace.RunFailed as error:
        print(f"{util.iso()} {row['id']}: verify retry still waiting for its workspace: {error.why}")
        return False
    layer = config.load_layer(repo.layer_dir)
    if runlimits.admission(repo, tid, source, runlimits.Policy.of(layer), False).reason:
        return False
    if not dry_run:
        try:
            tasks.resume(run_id, "verify", row["version"], environment_release=sha)
        except (slots.NoFreeSlot, control.Busy):
            return False
    refs = displayed_refs(repo.github, release.github, numbers)
    print(f"{util.iso()} {repo.name}#{tid}: {'would resume' if dry_run else 'resuming'} {run_id} from verify "
          f"because {refs} merged and live release {sha[:12]} contains the fix")
    return True
