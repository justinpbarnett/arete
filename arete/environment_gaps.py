from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from errno import errorcode
from pathlib import Path

from . import ci, codebase, config, control, factory, gh, outbound, paths, pipeline, run_lines, tracker, util, vault
from .verify import Verified

STEP_PATHS = ("feature", "patch")
RESUME_PATHS = (*STEP_PATHS, "classic", "orchestrate")
SANDBOX_DENIAL = re.compile(r"\bsandbox_apply:\s*Operation not permitted\b", re.IGNORECASE)
ERROR_CODES = re.compile(r"\b(?:" + "|".join(sorted(set(errorcode.values()) |
                         {"EAI_AGAIN", "EAI_FAIL", "EAI_NONAME", "ENOTFOUND"})) + r")\b", re.IGNORECASE)


def normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def safe_text(text: str) -> str:
    return outbound.screen(outbound.redact(vault.hidden(text)))


def signatures(text: str) -> tuple[str, ...]:
    codes = {code.upper() for code in ERROR_CODES.findall(text)}
    if SANDBOX_DENIAL.search(text):
        return ("sandbox_apply: Operation not permitted", *sorted(codes - {"EPERM"}))
    return tuple(sorted(codes))


@dataclass(frozen=True)
class Gap:
    what: str
    how: str
    saw: str

    @property
    def error(self) -> str:
        return ", ".join(signatures(self.saw)) or self.saw

    @property
    def key(self) -> str:
        return digest(normalized(self.error))

    def matches(self, title: str, body: str) -> bool:
        if known := signatures(self.saw):
            return known == signatures(title) or known == signatures(body)
        return normalized(self.error) in normalized(f"{title}\n{body}")

    def repeats(self, previous: Gap) -> bool:
        return self.key == previous.key or normalized(self.what) == normalized(previous.what)


def gaps(data: dict) -> list[Gap]:
    verified = Verified.stored(data)
    failed = verified.failed_checks
    if verified.verdict != "blocked" or not verified.finished or not failed:
        return []
    if any(c.get("cause") != "environment" or not c.get("saw") or not c.get("what") for c in failed):
        return []
    found = [Gap(*(safe_text(str(c.get(field) or "")).strip() for field in ("what", "how", "saw"))) for c in failed]
    return list({gap.key: gap for gap in found if gap.saw and gap.what}.values())


def last_data(lines: list[dict], step: str) -> dict:
    return next((factory.outcome_of(line).data for line in reversed(lines) if line.get("step") == step), {})


def was_retry(run_id: str) -> bool:
    return bool(factory.first(run_lines.read(run_id), "execution").get("environment_release"))


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
    repeated: bool = False
    earlier_gaps: tuple[Gap, ...] = ()


def hold_of(run_id: str) -> Hold | None:
    chain = factory.chain(run_id)
    previous = factory.first(chain[0], "route").get("continues") or ""
    own_verify = last_data(factory.ends(chain[0]), "verify")
    if factory.first(chain[0], "execution").get("environment_release"):
        if not previous or len(chain) <= 1 or own_verify.get("ending") != "environment-held":
            return None
        prior = hold_of(previous)
        found = gaps(own_verify.get("verified") or {})
        if prior and found:
            earlier = (*prior.earlier_gaps, *prior.gaps)
            return Hold(found, prior.heads, any(g.repeats(old) for g in found for old in earlier), earlier)
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


def release_contains_fixes(repo: config.Repo, numbers: list[int], sha: str) -> bool:
    base = config.load_layer(repo.layer_dir).base_branch or gh.default_branch(repo.github)
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

    def issue_for(self, repo: str, own: tuple[str, int], run_id: str, gap: Gap,
                  waits: list[tuple[str, int]], dry_run: bool) -> int | None:
        issue_key = f"{repo.casefold()}:{gap.key}"
        if number := self.holds.get(run_id, {}).get(issue_key):
            return number
        today = util.now().date().isoformat()
        key = f"environment-gap-{gap.key}-{today}"
        creation = self.creations.get(issue_key) or {}
        scope = repo.casefold()

        def matches(issue: dict) -> bool:
            body = issue.get("body") or ""
            return ((scope, issue.get("number")) != own and own not in tracker.github_waits(body, repo)
                    and gap.matches(issue.get("title") or "", body))

        linked = (gh.issue(repo, n) or {} for ref, n in waits if ref == scope and (ref, n) != own)
        number = next((i["number"] for i in linked if matches(i)
                       and any(label.get("name") == tracker.LABEL for label in i.get("labels", []))), None)
        if not number:
            opened = gh.issues(repo, tracker.LABEL, limit=tracker.MAX_AGENT_ISSUES)
            number = min((i["number"] for i in opened if matches(i)), default=None)
        if not number and creation.get("day") == today:
            number = creation.get("number")
            if not number and (url := gh.find_issue_marker(repo, gh.key_mark(key))):
                number = int(url.rsplit("/", 1)[-1])
            if not number:
                return None
        if not number and not dry_run:
            recent = gh.issues(repo, state="all", limit=tracker.MAX_AGENT_ISSUES, search=f"created:{today}")
            number = next((i["number"] for i in recent if gh.key_mark(key) in (i.get("body") or "").splitlines()), None)
            if not number:
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
        if number and not dry_run:
            self.holds.setdefault(run_id, {})[issue_key] = number
            self.save()
        return number

    def notice(self, url: str, key: str, message: str, dry_run: bool = False) -> bool:
        key = "environment-gap-" + digest(f"{url}\n{key}")
        state = self.notices.get(key)
        if state == "sent":
            return True
        if state == "pending":
            if not gh.find_issue_comment_marker(gh.pr_repo(url), gh.pr_number(url), gh.key_mark(key)):
                return False
        elif not dry_run:
            self.notices[key] = "pending"
            self.save()
            try:
                gh.pr_comment(url, message, key=key, single_attempt=True)
            except gh.GhUnsure:
                raise
            except gh.GhError:
                del self.notices[key]
                self.save()
                raise
        if not dry_run:
            self.notices[key] = "sent"
            self.save()
        return True


def indented(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())


def open_ticket(source: tracker.GitHubTracker, number: int) -> dict:
    ticket = gh.issue(source.repo.github, number) or {}
    labels = {label["name"] for label in ticket.get("labels", [])}
    if (ticket.get("state") == "OPEN" and source.by_owner(ticket) and tracker.LABEL in labels
            and not labels & {tracker.ZTE_LABEL, tracker.BATCH_LABEL}):
        return ticket
    return {}


def confirm_retry(cfg: config.Config, repo: config.Repo, row: dict, sha: str) -> bool:
    tid = str(row.get("issue") or "")
    if (repo.tracker != "github" or not re.fullmatch(r"[1-9][0-9]*", tid)
            or row.get("status") != "pr" or not row.get("pr_url") or live_release() != sha
            or pipeline.pending_feedback(repo.name, tid)):
        return False
    if not (release := release_repo(cfg)) or not (hold := hold_of(row["id"])) or hold.repeated or not owned_drafts(row, hold):
        return False
    source = tracker.GitHubTracker(repo)
    ticket = open_ticket(source, int(tid))
    waits = tracker.github_waits(ticket.get("body") or "", repo.github)
    journal = Journal(paths.STATE_DIR / "environment-gaps.json")
    links = journal.holds.get(row["id"], {})
    scope = release.github.casefold()
    numbers = list(dict.fromkeys(links.get(f"{scope}:{gap.key}") for gap in hold.gaps))
    required = [(scope, n) for n in numbers]
    return (all(ref in waits for ref in required)
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
            or (path := pipeline.recorded_path(run_id)) not in RESUME_PATHS
            or (path in STEP_PATHS and cfg.adws and cfg.adws.resolve() != paths.ADWS_DIR.resolve())):
        return False
    if not (release := release_repo(cfg)) or not (hold := hold_of(run_id)) or not owned_drafts(row, hold):
        return False
    source = tracker.GitHubTracker(repo)
    if not (ticket := open_ticket(source, int(tid))):
        return False
    if hold.repeated:
        if not dry_run:
            for url in hold.heads:
                journal.notice(url, f"{run_id}-retry", f"Verification in run `{run_id}` hit the same environment "
                               "gap again after the automatic retry. The PR stays a draft for a person to check.")
        return False
    waits = tracker.github_waits(ticket.get("body") or "", repo.github)
    own = repo.github.casefold(), int(tid)
    needed = [journal.issue_for(release.github, own, run_id, gap, waits, dry_run) for gap in hold.gaps]
    numbers = list(dict.fromkeys(n for n in needed if n))
    required = [(release.github.casefold(), n) for n in numbers]
    if not dry_run:
        if not (ticket := open_ticket(source, int(tid))):
            return False
        body = ticket.get("body") or ""
        waits = tracker.github_waits(body, repo.github)
        missing = [n for ref, n in required if (ref, n) not in waits]
        if missing:
            if not gh.issue_add_waits(repo.github, int(tid), body, displayed_refs(repo.github, release.github, missing)):
                return False
            waits.extend((release.github.casefold(), n) for n in missing)
    if not all(needed) or any(ref not in waits for ref in required):
        return False
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
