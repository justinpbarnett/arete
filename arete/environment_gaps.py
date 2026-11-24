from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from . import ci, codebase, config, control, factory, gh, outbound, paths, pipeline, run_lines, tracker, util, vault
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


def is_retry_notice(body: str, retry_id: str) -> bool:
    if not gh.has_arete_mark(body) or not re.search(rf"(?<![\w-]){re.escape(retry_id)}(?![\w-])", body):
        return False
    text = normalized(body)
    return all(phrase in text for phrase in ("verification", "retry", "draft", "owner review"))


def notice(url: str, talk: gh.Discussion, event: str, body: str, *, retry_id: str = "") -> None:
    key = "environment-gap-" + digest(f"{url}\n{event}")
    marker = gh.key_mark(key)
    for row in talk.comments:
        previous = row.get("body") or ""
        if marker in previous.splitlines() or (retry_id and is_retry_notice(previous, retry_id)):
            return
    gh.pr_comment(url, body, key=key, single_attempt=True)


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
    if not release_contains_fixes(release, numbers, sha):
        return ()
    return tuple((release.github.casefold(), number) for number in numbers)


def displayed_refs(repo: str, gaps_repo: str, numbers: list[int]) -> str:
    prefix = "" if repo.casefold() == gaps_repo.casefold() else gaps_repo
    return tracker.joined([f"{prefix}#{n}" for n in numbers])


def prerequisite_reads(release: config.Repo | None, refs: set[tuple[str, int]], follow_up_urls: Iterable[str],
                       sha: str) -> dict:
    follow_up = {(gh.pr_repo(url).casefold(), gh.pr_number(url)) for url in follow_up_urls if PR_URL.fullmatch(url)}
    reads = {}
    for repo, number in sorted(refs):
        ref = f"{repo}#{number}"
        try:
            reads[ref] = [p for p in gh.prerequisite_pulls(repo, number) if (p.repo, p.number) not in follow_up]
        except gh.GhError:
            reads[ref] = None
    base = ""
    scope = release.github.casefold() if release else ""
    if release and sha and any(p.repo == scope and p.merge_commit for pulls in reads.values() for p in pulls or ()):
        try:
            target = release_base(release)
            if gh.branch_contains(release.github, target, sha):
                base = target
        except gh.GhError:
            pass
    deployed = {}
    for pulls in reads.values():
        for p in pulls or ():
            if (release and base and p.repo == scope and p.base == base and p.merge_commit
                    and p.merge_commit not in deployed):
                try:
                    deployed[p.merge_commit] = sha if gh.branch_contains(release.github, sha, p.merge_commit) else ""
                except gh.GhError:
                    deployed[p.merge_commit] = ""
    return {ref: None if pulls is None else [
        {"key": p.key, "url": p.url, "state": p.state, "merge_commit": p.merge_commit,
         "deployed_release": deployed.get(p.merge_commit, "") if p.repo == scope and p.base == base else ""}
        for p in pulls] for ref, pulls in reads.items()}


def observe_dependencies(cfg: config.Config, source: tracker.GitHubTracker) -> None:
    repo = source.repo
    held = {str(row["issue"]) for row in run_store.environment_holds(repo.name)}
    release = release_repo(cfg)
    sha = None
    for ticket in source.dependency_tickets:
        if ticket.id in held:
            continue
        refs = set(tracker.github_waits(ticket.body, repo.github)) - {(repo.github.casefold(), int(ticket.id))}
        if not refs and not ticket_progress.get(repo.name, ticket.id).get("declared_prerequisites"):
            continue
        if sha is None:
            sha = live_release() if release else ""
        newest = run_store.newest_live_whole_run(repo.name, ticket.id) or {}
        reads = prerequisite_reads(release, refs, (newest.get("pr_url") or "").split(), sha)
        ticket_progress.record(repo.name, ticket.id, prerequisite_reads=reads, declared=True)


def repeated_gap(sig: Signature, run_id: str) -> tuple[str, str, str]:
    return sig.key, run_id, f"Verification hit the same blocker again: {sig.title}."


def progress(row: dict, hold: Hold, state: str, next_event: str, *, reads: dict | None = None,
             retry_blockers: set[Signature] | None = None, workspace_blocker: str = "") -> None:
    run_id, retry_id = row["id"], row.get("retry_id") or ""
    signatures = {g.signature for g in hold.gaps}
    current = signatures if state == "waiting" else retry_blockers or set()
    evidence = [repeated_gap(sig, run_id) for sig in sorted(signatures, key=lambda sig: sig.key)]
    evidence += [repeated_gap(sig, retry_id) for sig in sorted(current, key=lambda sig: sig.key) if state == "owner-review"]
    blocker = {"kind": "verification-environment", "causes": {s.key: s.title for s in current}} if current else None
    if workspace_blocker:
        blocker = {"kind": "retry-workspace", "causes": {"workspace": workspace_blocker}}
    ticket_progress.record(row["repo"], row["issue"], {
        "run": run_id, "attempt": retry_id or run_id,
        "blocker": blocker,
        "next_event": next_event,
        "follow_up": {"state": state, "prs": sorted(hold.heads), "draft": True if state != "inactive" else None},
    }, evidence=evidence, prerequisite_reads=reads, only_current=state == "inactive")


def finish_retry(row: dict, hold: Hold, reads: dict, talks: dict[str, gh.Discussion]) -> None:
    if row["retry_status"] == "running":
        progress(row, hold, "verifying", "Automatic verification finishes", reads=reads)
        return
    completed = [line for line in factory.ends(run_lines.read(row["retry_id"])) if not line.get("carried")]
    outcome = last_data(completed, "verify")
    if outcome.get("ending") == "environment-held":
        checks = (outcome.get("verified") or {}).get("checks", [])
        blockers = {sig for check in checks if (sig := check_signature(check))}
        progress(row, hold, "owner-review", "Owner reviews the draft and decides how to unblock verification",
                 retry_blockers=blockers, reads=reads)
        retry_id = row["retry_id"]
        for url, talk in talks.items():
            notice(url, talk, f"{retry_id}-still-held", f"Automatic verification retry `{retry_id}` did not pass on "
                   "the live release. This PR stays a draft and needs owner review.", retry_id=retry_id)
    else:
        progress(row, hold, "retry-finished", "", reads=reads)
    run_store.release_environment_hold(row["id"])


def next_hold_event(blocker: str, release: config.Repo, numbers: list[int], reads: dict) -> str:
    if blocker:
        return f"Tracker prerequisite is resolved ({safe_text(blocker)})"
    if not numbers:
        return "A prerequisite issue is linked to each verification gap"
    required = [reads[f"{release.github.casefold()}#{n}"] for n in numbers]
    if any(group is None for group in required):
        return "The prerequisite PR state can be confirmed"
    if not all(any(pr["state"] == "merged" for pr in group) for group in required):
        return "A fix for each verification gap is confirmed merged"
    if not all(any(pr["deployed_release"] for pr in group) for group in required):
        return "The live release is confirmed to contain the fixes"
    return "Automatic verification retry starts when eligible"


def observe_wait(row: dict, hold: Hold, release: config.Repo, declared: list[tuple[str, int]], waits: dict,
                 numbers: list[int], blocker: str, sha: str, *, eligible: bool = True,
                 workspace_blocker: str = "") -> None:
    reads = prerequisite_reads(release, set(declared) | set(waits.values()), hold.heads, sha)
    if workspace_blocker:
        event = "The saved workspace becomes restorable or the owner takes over the draft"
    elif eligible:
        event = next_hold_event(blocker, release, numbers, reads)
    else:
        event = "The hold becomes eligible for recovery"
    progress(row, hold, "waiting", event, reads=reads, workspace_blocker=workspace_blocker)


def handle(cfg: config.Config, repo: config.Repo, row: dict, dry_run: bool, *, can_start: bool) -> bool:
    from . import flows, oncall, pilot, runlimits, slots, tasks, workspace, zte

    run_id, tid = row["id"], str(row["issue"])
    if row.get("files_purged"):
        if not dry_run:
            saved = run_store.environment_hold(run_id)
            progress(row, Hold([], saved.get("heads", {})), "inactive", "")
            run_store.release_environment_hold(run_id)
        return False
    release, hold = release_repo(cfg), hold_of(run_id)
    saved = run_store.environment_hold(run_id)
    if not release or not hold or saved.get("heads") != hold.heads:
        return False
    source = tracker.for_ticket(cfg, repo, tid)
    blocker, declared = ticket_gate(source, tid)
    talks = discussions(row, hold, source) if blocker is not None else {}
    if not talks:
        if not dry_run:
            progress(row, hold, "inactive", "")
            run_store.release_environment_hold(run_id)
        return False
    if row["retry_id"]:
        if not dry_run:
            refs = set(declared) | {tuple(ref) for ref in saved["waits"].values()}
            finish_retry(row, hold, prerequisite_reads(release, refs, hold.heads, live_release()), talks)
        return False
    if run_store.later_run(repo.name, tid, run_id, live=True):
        return False
    scope = release.github.casefold()
    waits = {key: tuple(ref) for key, ref in saved["waits"].items()}
    path = pipeline.recorded_path(run_id)
    if (path not in RESUME_PATHS or tid in oncall.parked(repo.name) or pilot.is_pilot_task(tid) or zte.reserved(tid)
            or tasks.pending_start(repo.name, tid)
            or (path in STEP_PATHS and cfg.adws and cfg.adws.resolve() != paths.ADWS_DIR.resolve())):
        if not dry_run:
            observe_wait(row, hold, release, declared, waits, linked_numbers(hold, waits, scope), blocker,
                         live_release(), eligible=False)
        return False
    own = (repo.github.casefold(), int(tid)) if isinstance(source, tracker.GitHubTracker) else None
    include_check = bool(own and own[0] == scope and all(gh.pr_repo(url).casefold() == scope for url in hold.heads))
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
    sha = live_release() if not dry_run or can_start else ""
    ready = (numbers and not blocker and sha and (path != "orchestrate" or verification_action(run_id))
             and release_contains_fixes(release, numbers, sha))
    workspace_blocker = ""
    if ready:
        try:
            flows.check_environment_workspace(repo, row, hold.heads)
        except workspace.RunFailed as error:
            workspace_blocker = util.first_line(safe_text(error.why), 300) or "The saved workspace cannot be restored"
            print(f"{util.iso()} {run_id}: verify retry still waiting for its workspace: {workspace_blocker}")
    if not dry_run:
        observe_wait(row, hold, release, declared, waits, numbers, blocker, sha, workspace_blocker=workspace_blocker)
        if numbers:
            event = f"{run_id}-wait-{scope}-{','.join(map(str, numbers))}"
            for url, talk in talks.items():
                refs = displayed_refs(gh.pr_repo(url), release.github, numbers)
                notice(url, talk, event, f"Verification is waiting on {refs} for the environment gap in run `{run_id}`. "
                       "This PR stays a draft until verification can run again.")
    if not can_start or not ready or workspace_blocker:
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
