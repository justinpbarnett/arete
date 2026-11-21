from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from errno import errorcode
from pathlib import Path

from . import ci, codebase, config, control, factory, gh, outbound, paths, pipeline, run_lines, tracker, util, vault
from .store import runs as run_store
from .verify import Verified

STEP_PATHS = ("feature", "patch")
RESUME_PATHS = (*STEP_PATHS, "classic", "orchestrate")
SANDBOX_DENIAL = re.compile(r"\bsandbox_apply:\s*Operation not permitted\b", re.IGNORECASE)
OPERATIONS = r"access|bind|chmod|chown|connect|execve|fork|getaddrinfo|listen|mkdir|open|read|rename|spawn|stat|unlink|write"
ERROR_CODES = "|".join(sorted(set(errorcode.values()) | {"EAI_AGAIN", "EAI_FAIL", "EAI_NONAME", "ENOTFOUND"}))
OPERATION_ERRORS = re.compile(rf"\b({OPERATIONS})(?:\s*:\s*|\s+)({ERROR_CODES})\b", re.IGNORECASE)
ERROR_INTRO = re.compile(
    r"^(?:(?:the\s+)?(?:command|driver|probe|process|check|verifier)\s+)?"
    r"(?:exit(?:ed)?(?:\s+with)?\s+(?:(?:exit\s+)?(?:status|code)\s+)?-?\d+"
    r"(?:\s+(?:with|and\s+(?:printed|reported)))?\s*[:,-]?\s+"
    r"|failed\s+with\s+(?:(?:the\s+following\s+)?(?:error|output)\b\s*,?\s*:?\s*)?"
    r"|(?:stderr|error output)\s*:\s*)", re.IGNORECASE)


def normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def safe_text(text: str) -> str:
    return outbound.screen(outbound.redact(vault.hidden(text)))


def signatures(text: str) -> tuple[str, ...]:
    errors = {f"{op.lower()} {code.upper()}" for op, code in OPERATION_ERRORS.findall(text)}
    if SANDBOX_DENIAL.search(text):
        errors.add("sandbox_apply: Operation not permitted")
    return tuple(sorted(errors))


def error_text(text: str) -> str:
    text = text.strip()
    while match := ERROR_INTRO.match(text):
        text = text[match.end():].strip()
    if quoted := re.fullmatch(r"([`\"'])(.+)\1[.!]?", text, re.DOTALL):
        text = quoted[2].strip()
    if fenced := re.fullmatch(r"```[^\n]*\n(.+)\n```[.!]?", text, re.DOTALL):
        text = fenced[1].strip()
    return text.removesuffix(".").rstrip()


@dataclass(frozen=True)
class Gap:
    what: str
    how: str
    saw: str

    @property
    def error(self) -> str:
        return ", ".join(signatures(self.saw)) or error_text(self.saw) or self.saw

    @property
    def key(self) -> str:
        return digest(normalized(self.error))

    @property
    def legacy_key(self) -> str:
        return digest(normalized(", ".join(signatures(self.saw)) or self.saw))

    def matches(self, title: str, body: str) -> bool:
        if known := signatures(self.saw):
            return known == signatures(title) or known == signatures(body)
        return normalized(self.error) in normalized(f"{title}\n{body}")

    def repeats(self, previous: Gap) -> bool:
        return self.key == previous.key or normalized(self.what) == normalized(previous.what)


def gaps(data: dict) -> list[Gap]:
    if data.get("verdict") != "blocked":
        return []
    verified = Verified.stored(data)
    failed = verified.failed_checks
    if not failed:
        return []
    if any(c.get("passed") is not False or normalized(str(c.get("cause") or "")) != "environment" for c in failed):
        return []
    found = [Gap(*(safe_text(str(c.get(field) or "")).strip() for field in ("what", "how", "saw"))) for c in failed]
    return list({gap.key: gap for gap in found}.values()) if all(gap.saw and gap.what for gap in found) else []


def last_data(lines: list[dict], step: str) -> dict:
    return next((factory.outcome_of(line).data for line in reversed(lines) if line.get("step") == step), {})


def was_retry(row: dict) -> bool:
    return bool(row.get("environment_retry_of")
                or factory.first(run_lines.read(row["id"]), "execution").get("environment_release"))


def verification_action(run_id: str) -> str:
    from .store import orchestration

    row = orchestration.find(run_id)
    if row.get("current_run") != run_id:
        return ""
    return (row["data"].get("evidence", {}).get("verify", {}).get("producer") or "")


@dataclass(frozen=True)
class Hold:
    gaps: list[Gap]
    heads: dict[str, str]
    retried: bool = False
    repeated: bool = False


def hold_of(run_id: str) -> Hold | None:
    chain = factory.chain(run_id)
    row = run_store.run_get(run_id) or {"id": run_id}
    if was_retry(row):
        previous = row.get("environment_retry_of") or factory.first(chain[0], "route").get("continues")
        own_verify = last_data(factory.ends(chain[0]), "verify")
        if not previous or previous == run_id or own_verify.get("ending") != "environment-held":
            return None
        prior = hold_of(previous)
        found = gaps(own_verify.get("verified") or {})
        if prior and found:
            return Hold(found, prior.heads, retried=True, repeated=any(g.repeats(old) for g in found for old in prior.gaps))
        return None
    history = factory.history(chain)
    classic = factory.first(chain[0], "classic_hold")
    publication = classic.get("publication") or last_data(history, "pr")
    if not publication.get("draft_why") or publication.get("ci_stopped"):
        return None
    if (classic.get("review") or last_data(history, "review").get("review") or {}).get("verdict") != "approve":
        return None
    found = gaps(publication.get("verified") or last_data(history, "verify").get("verified") or {})
    heads = {url: (publication.get("heads") or {}).get(repo) for repo, url in (publication.get("prs") or {}).items()}
    if found and heads and all(heads.values()):
        return Hold(found, heads)
    return None


def owned_drafts(row: dict, hold: Hold) -> bool:
    if set(row["pr_url"].split()) != hold.heads.keys():
        return False
    for url, head in hold.heads.items():
        pr = gh.gh_json(["pr", "view", url, "--json", "state,isDraft,headRefOid,body"]) or {}
        if (pr.get("state") != "OPEN" or pr.get("isDraft") is not True or pr.get("headRefOid") != head
                or ci.stopped(pr.get("body") or "")):
            return False
    return True


def live_release() -> str:
    beat = control.serve_beat()
    sha = beat.get("release") or ""
    return sha if (re.fullmatch(r"[0-9a-f]{40}", sha) and beat.get("alive") and beat.get("tick_ok")
                   and beat.get("engine") == codebase.STAMP == sha[:12] and codebase.RELEASE == sha
                   and not any(beat.get(key) for key in ("code_problem", "config_problem", "migration_error"))) else ""


def release_repo(cfg: config.Config) -> config.Repo | None:
    return next((repo for repo in cfg.repos if repo.name == cfg.arete_repo and repo.github), None)


def shared_audience(repo: config.Repo, release: config.Repo, hold: Hold) -> bool:
    pulls = [re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/[1-9][0-9]*/?", url) for url in hold.heads]
    if not all(pulls):
        return False
    repos = {name.casefold() for name in (repo.github, release.github, *(part.github for part in repo.parts),
                                        *(pull[1] for pull in pulls))}
    return all(re.fullmatch(r"[\w.-]+/[\w.-]+", name) for name in repos) and (
        len(repos) == 1 or all(gh.repo_public(name) for name in sorted(repos)))


def release_base(repo: config.Repo) -> str:
    return config.load_layer(repo.layer_dir).base_branch or gh.default_branch(repo.github)


def release_contains_fixes(repo: config.Repo, numbers: list[int], sha: str) -> bool:
    base = release_base(repo)
    fixes = [gh.issue_merge_commit(repo.github, n, base) for n in numbers]
    return bool(fixes and all(fixes) and gh.branch_contains(repo.github, base, sha)
                and all(gh.branch_contains(repo.github, sha, fix) for fix in fixes))


class Journal:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except FileNotFoundError:
            self.data = {}
        self.holds = self.data.setdefault("holds", {})
        self.creations = self.data.setdefault("creations", {})
        self.notices = self.data.setdefault("notices", {})

    def save(self) -> None:
        util.write_json(self.path, self.data)

    def linked_issue(self, repo: str, run_id: str, gap: Gap) -> int | None:
        links = self.holds.get(run_id, {})
        return links.get(f"{repo.casefold()}:{gap.key}") or links.get(f"{repo.casefold()}:{gap.legacy_key}")

    def issue_for(self, repo: str, base: str, own: tuple[str, int], run_id: str, gap: Gap,
                  waits: list[tuple[str, int]], dry_run: bool) -> int | None:
        scope = repo.casefold()
        issue_key = f"{scope}:{gap.key}"

        def linkable(number: int | None) -> bool:
            return type(number) is int and number > 0 and (scope, number) != own

        today = util.now().date().isoformat()
        key = f"environment-gap-{gap.key}-{today}"
        legacy_key = f"environment-gap-{gap.legacy_key}-{today}"
        creation = self.creations.get(issue_key) or self.creations.get(f"{scope}:{gap.legacy_key}") or {}
        markers = {gh.key_mark(key), gh.key_mark(legacy_key)}

        def eligible(issue: dict) -> bool:
            return (linkable(issue.get("number")) and own not in tracker.github_waits(issue.get("body") or "", repo)
                    and any(label.get("name") == tracker.LABEL for label in issue.get("labels", [])))

        def matches(issue: dict) -> bool:
            return eligible(issue) and gap.matches(issue.get("title") or "", issue.get("body") or "")

        previous = self.linked_issue(repo, run_id, gap)
        current = {}
        if linkable(previous):
            current = gh.issue(repo, previous) or {}
            if eligible(current):
                if current.get("state") == "OPEN" or (current.get("state") == "CLOSED"
                        and gh.issue_merge_commit(repo, previous, base)):
                    return previous
            if current.get("state") != "CLOSED":
                return None
        else:
            previous = None
        linked = [gh.issue(repo, n) or {} for ref, n in waits if ref == scope and (ref, n) != own and n != previous]
        if previous:
            closed = gh.issues(repo, tracker.LABEL, state="closed", limit=tracker.MAX_AGENT_ISSUES)
            replacements = [i for i in linked if i.get("state") == "CLOSED" and matches(i)]
            replacements.extend(i for i in closed if matches(i) and i["number"] > previous)
            for number in sorted({i["number"] for i in replacements}, reverse=True):
                if gh.issue_merge_commit(repo, number, base):
                    return number
        number = next((i["number"] for i in linked if i.get("state") == "OPEN" and matches(i)), None)
        if not number:
            opened = gh.issues(repo, tracker.LABEL, limit=tracker.MAX_AGENT_ISSUES)
            number = min((i["number"] for i in opened if matches(i)), default=None)
        if number or previous:
            return number or (previous if eligible(current) else None)
        if creation.get("day") == today:
            number = creation.get("number")
            if not number:
                for marker in sorted(markers):
                    if url := gh.find_issue_marker(repo, marker):
                        number = int(url.rsplit("/", 1)[-1])
                        break
            if not linkable(number) or not eligible(gh.issue(repo, number) or {}):
                return None
        if not number and not dry_run:
            recent = gh.issues(repo, state="all", limit=tracker.MAX_AGENT_ISSUES, search=f"created:{today}")
            if marked := [i for i in recent if markers.intersection((i.get("body") or "").splitlines())]:
                number = next((i["number"] for i in marked if eligible(i)), None)
            else:
                self.creations[issue_key] = {"day": today}
                self.save()
                checks = "\n\n".join(f"{name}\n\n{indented(value)}" for name, value in (
                    ("What", gap.what), ("How", gap.how), ("Saw", gap.saw)))
                try:
                    number = gh.issue_create(repo, "Verification environment gap — " + util.first_line(gap.error, 120),
                                             f"Verification was blocked by the environment in run `{run_id}`.\n\n{checks}",
                                             labels=[tracker.LABEL], key=key, single_attempt=True)
                except gh.GhUnsure:
                    raise
                except gh.GhError:
                    del self.creations[issue_key]
                    self.save()
                    raise
                self.creations[issue_key]["number"] = number
                self.save()
        return number if linkable(number) else None

    def notice(self, url: str, event: str, message: str, dry_run: bool = False,
               *, ticket: tuple[str, int] | None = None) -> bool:
        key = notice_key(url, event)
        state = self.notices.get(key)
        if state == "sent":
            return True
        repo, number = ticket or (gh.pr_repo(url), gh.pr_number(url))
        found = state in ("pending", "uncertain") and gh.find_issue_comment_marker(repo, number, gh.key_mark(key))
        if state == "uncertain" and not found:
            return False
        if not found and not dry_run:
            self.notices[key] = "pending"
            self.save()
            try:
                if ticket:
                    gh.issue_comment(repo, number, message, key=key, single_attempt=True)
                else:
                    gh.pr_comment(url, message, key=key, single_attempt=True)
            except gh.GhUnsure:
                self.notices[key] = "uncertain"
                self.save()
                raise
            except gh.GhError:
                del self.notices[key]
                self.save()
                raise
        if not dry_run:
            self.notices[key] = "sent"
            self.save()
        return True


def notice_key(url: str, event: str) -> str:
    return "environment-gap-" + digest(f"{url}\n{event}")


def indented(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())


def open_ticket(source: tracker.GitHubTracker, number: int) -> dict:
    ticket = gh.issue(source.repo.github, number) or {}
    labels = {label["name"] for label in ticket.get("labels", [])}
    if (ticket.get("state") == "OPEN" and source.by_owner(ticket) and tracker.LABEL in labels
            and not labels & {tracker.ZTE_LABEL, tracker.BATCH_LABEL}):
        return ticket
    return {}


def ticket_wait_event(run_id: str, release: str, numbers: list[int]) -> str:
    return f"{run_id}-ticket-wait-{release.casefold()}-{','.join(map(str, sorted(numbers)))}"


def ticket_wait_published(journal: Journal, repo: str, number: int, key: str) -> bool:
    return journal.notices.get(key) == "sent" and bool(gh.find_issue_comment_marker(repo, number, gh.key_mark(key)))


def confirm_retry(cfg: config.Config, repo: config.Repo, row: dict, sha: str) -> bool:
    tid = str(row.get("issue") or "")
    if (repo.tracker != "github" or not re.fullmatch(r"[1-9][0-9]*", tid)
            or row.get("status") != "pr" or not row.get("pr_url") or was_retry(row) or live_release() != sha
            or pipeline.pending_feedback(repo.name, tid)):
        return False
    if (not (release := release_repo(cfg)) or not (hold := hold_of(row["id"]))
            or not shared_audience(repo, release, hold) or not owned_drafts(row, hold)):
        return False
    source = tracker.GitHubTracker(repo)
    if not (ticket := open_ticket(source, int(tid))):
        return False
    run_id = row["id"]
    waits = tracker.github_waits(ticket.get("body") or "", repo.github)
    journal = Journal(paths.STATE_DIR / "environment-gaps.json")
    linked = [journal.linked_issue(release.github, run_id, gap) for gap in hold.gaps]
    if not linked or any(type(n) is not int or n <= 0 for n in linked):
        return False
    numbers = list(dict.fromkeys(linked))
    scope = release.github.casefold()
    required = [(scope, n) for n in numbers]
    key = notice_key(gh.issue_url(repo.github, tid), ticket_wait_event(run_id, release.github, numbers))
    return ((repo.github.casefold(), int(tid)) not in required
            and (all(ref in waits for ref in required) or ticket_wait_published(journal, repo.github, int(tid), key))
            and not source.wait_blocker([ref for ref in waits if ref not in required])
            and release_contains_fixes(release, numbers, sha))


def displayed_refs(repo: str, gaps_repo: str, numbers: list[int]) -> list[str]:
    prefix = "" if repo.casefold() == gaps_repo.casefold() else gaps_repo
    return [f"{prefix}#{n}" for n in numbers]


def handle(cfg: config.Config, repo: config.Repo, row: dict, journal: Journal, dry_run: bool, *, can_start: bool) -> bool:
    from . import oncall, pilot, runlimits, slots, tasks, zte

    run_id, tid = row["id"], str(row["issue"])
    if (not re.fullmatch(r"[1-9][0-9]*", tid) or tid in oncall.parked(repo.name)
            or pilot.is_pilot_task(tid) or zte.reserved(tid) or tasks.pending_start(repo.name, tid)
            or ((path := pipeline.recorded_path(run_id)) not in RESUME_PATHS and not row.get("environment_retry_of"))
            or (path in STEP_PATHS and cfg.adws and cfg.adws.resolve() != paths.ADWS_DIR.resolve())):
        return False
    if (not (release := release_repo(cfg)) or not (hold := hold_of(run_id))
            or not shared_audience(repo, release, hold) or not owned_drafts(row, hold)):
        return False
    source = tracker.GitHubTracker(repo)
    if not (ticket := open_ticket(source, int(tid))):
        return False
    if hold.retried:
        if not dry_run:
            gap = "the same environment gap again" if hold.repeated else "another environment gap"
            for url in hold.heads:
                journal.notice(url, f"{run_id}-retry", f"Verification in run `{run_id}` hit {gap} "
                               "after the automatic retry. The PR stays a draft for a person to check.")
        return False
    waits = tracker.github_waits(ticket.get("body") or "", repo.github)
    own = repo.github.casefold(), int(tid)
    scope = release.github.casefold()
    base = release_base(release)
    needed = [journal.issue_for(release.github, base, own, run_id, gap, waits, dry_run) for gap in hold.gaps]
    if not all(needed):
        return False
    numbers = list(dict.fromkeys(needed))
    required = [(scope, n) for n in numbers]
    if not dry_run:
        if not (ticket := open_ticket(source, int(tid))):
            return False
        waits = tracker.github_waits(ticket.get("body") or "", repo.github)
    previous = {journal.linked_issue(release.github, run_id, gap) for gap in hold.gaps}
    superseded = sorted(previous - set(numbers) - {None})
    ticket_url = gh.issue_url(repo.github, tid)
    event = ticket_wait_event(run_id, release.github, numbers)
    missing = any(ref not in waits for ref in required)
    if superseded or missing:
        message = "Waits on " + ", ".join(displayed_refs(repo.github, release.github, numbers))
        if superseded:
            old_refs = tracker.joined(displayed_refs(repo.github, release.github, superseded))
            message += f"\n\nSupersedes the environment gap dependency on {old_refs}."
        if not journal.notice(ticket_url, event, message, dry_run, ticket=(repo.github, int(tid))):
            return False
    if not dry_run:
        links = journal.holds.setdefault(run_id, {})
        updated = {f"{scope}:{gap.key}": n for gap, n in zip(hold.gaps, needed, strict=True)}
        if any(links.get(key) != n for key, n in updated.items()):
            links.update(updated)
            journal.save()
    refs = tracker.joined(displayed_refs(repo.github, release.github, numbers))
    for url in hold.heads:
        links = tracker.joined(displayed_refs(gh.pr_repo(url), release.github, numbers))
        if not journal.notice(url, f"{run_id}-wait", f"Verification is waiting on {links} for the environment "
                              f"gap in run `{run_id}`. This PR stays a draft until verification can run again.", dry_run):
            return False
    if not can_start or source.wait_blocker([ref for ref in waits if ref not in required]):
        return False
    if not (sha := live_release()) or pipeline.pending_feedback(repo.name, tid):
        return False
    if path == "orchestrate" and not verification_action(run_id):
        return False
    layer = config.load_layer(repo.layer_dir)
    if not release_contains_fixes(release, numbers, sha):
        return False
    key = notice_key(ticket_url, event)
    if missing and (not dry_run or journal.notices.get(key) == "sent"):
        if not ticket_wait_published(journal, repo.github, int(tid), key):
            return False
    if runlimits.admission(repo, tid, source, runlimits.Policy.of(layer), False)[0]:
        return False
    if not dry_run:
        try:
            tasks.resume(run_id, "verify", row["version"], environment_release=sha)
        except (slots.NoFreeSlot, control.Busy):
            return False
    print(f"{util.iso()} {repo.name}#{tid}: {'would resume' if dry_run else 'resuming'} {run_id} from verify "
          f"because {refs} {'merged' if len(numbers) == 1 else 'all merged'} and live release {sha[:12]} contains "
          f"{'the fix' if len(numbers) == 1 else 'the fixes'}")
    return True
