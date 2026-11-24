from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from . import config, diffguard, factory, gh, repairs, tracker, util, vault, workspace
from .store import asks, orchestration, runs, unblock

REASON = "open-pr-recovery"
DECISION_VERSION = "v2:"
OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
NO_HOOKS = ["-c", "core.hooksPath=/dev/null", "-c", "rerere.enabled=false"]
MERGE = ["merge", "--no-autostash", "--no-overwrite-ignore"]


def ancestor(wt: Path, a: str, b: str) -> bool:
    return util.sh(["git", "merge-base", "--is-ancestor", a, b], cwd=wt).returncode == 0


@dataclass(frozen=True)
class Refs:
    url: str
    state: str
    branch: str
    head: str
    base_branch: str
    base: str
    cross_repo: bool | None
    origin: str
    origin_base: str

    def target_matches(self, c: workspace.Checkout, branch: str) -> bool:
        return (self.state == "OPEN" and self.cross_repo is False and self.url == c.pr
                and gh.pr_repo(self.url).casefold() == c.github.casefold()
                and self.branch == branch and self.base_branch == c.base)


def observe(c: workspace.Checkout, branch: str) -> Refs:
    pr = gh.pr_merge_details(c.pr, timeout=30, retry=False, identity=True)
    head_ref, base_ref = f"refs/heads/{branch}", f"refs/heads/{c.base}"
    out = util.git(["ls-remote", "--refs", "origin", head_ref, base_ref], c.src, timeout=30, retry=False)
    remote = {ref: sha for sha, ref in (line.split() for line in out.splitlines())}
    return Refs(pr.get("url") or "", pr.get("state") or "", pr.get("headRefName") or "",
                pr.get("headRefOid") or "", pr.get("baseRefName") or "", pr.get("baseRefOid") or "",
                pr.get("isCrossRepository"), remote.get(head_ref, ""), remote.get(base_ref, ""))


def current_head(c: workspace.Checkout) -> str:
    return util.git(["rev-parse", "--verify", "HEAD"], c.wt).strip()


@dataclass(frozen=True)
class Inputs:
    checkout: workspace.Checkout
    branch: str
    accepted: str
    local: str
    refs: Refs

    def hold(self, why: str, action: str, *, observed: Refs | None = None, local: str = "") -> Hold:
        c = self.checkout
        refs = self.refs if observed is None else observed
        summary = (f"{c.pr} ({c.github}): {why}. Accepted head {self.accepted or 'unrecorded'}; "
                   f"retained head {local or self.local}; PR head {refs.head or 'missing'}; "
                   f"origin/{self.branch} {refs.origin or 'missing'}; PR base {refs.base_branch}@{refs.base or 'missing'}. "
                   f"Arete did not overwrite either version. {action}")
        return Hold(vault.hidden(summary), recovery=self.record(refs, local))

    def record(self, refs: Refs | None, local: str = "") -> dict:
        c = self.checkout
        return {"reason": REASON, "pr": c.pr, "github": c.github, "branch": self.branch,
                "accepted": self.accepted, "local": local or self.local, "refs": asdict(refs) if refs else {}}

    def publication_unconfirmed(self, why: str, refs: Refs | None, pushed: dict[str, str]) -> Hold:
        seen = f"PR head {refs.head or 'missing'}, origin head {refs.origin or 'missing'}" if refs else "unconfirmed"
        done = f" {pushed_note(self.branch, pushed)}" if pushed else ""
        summary = (f"{self.checkout.pr} ({self.checkout.github}): {why} (expected head {self.refs.origin or 'missing'}, "
                   f"candidate head {self.local}, observed {seen}).{done} Reconcile the publication before resuming")
        failure = {**self.record(refs), "expected_refs": asdict(self.refs), "pushed": dict(pushed),
                   "publication_unconfirmed": True}
        return Hold(vault.hidden(summary), recovery=failure)

    def moved(self, refs: Refs, *, during: str, local: str = "", pushed: dict[str, str] | None = None) -> Hold:
        was = self.refs
        action = (f"{pushed_note(self.branch, pushed)} Reconcile the partial publication before resuming" if pushed
                  else "Inspect the new head and retry against it; no push was made by this attempt")
        error = self.hold(f"the PR or origin moved {during} (expected head {was.head}, origin head {was.origin}, "
                          f"base {was.base_branch}@{was.base}, origin base {was.origin_base}; "
                          f"now {refs.state or 'unknown'} on {refs.branch or 'unknown'}, "
                          f"origin base {refs.origin_base or 'missing'})", action, observed=refs, local=local)
        error.recovery["expected_refs"] = asdict(was)
        if pushed:
            error.recovery["pushed"] = dict(pushed)
        return error

    def recheck(self, *, during: str, local: str = "", pushed: dict[str, str] | None = None) -> None:
        refs = observe(self.checkout, self.branch)
        if refs != self.refs or not refs.target_matches(self.checkout, self.branch):
            raise self.moved(refs, during=during, local=local, pushed=pushed)


class Hold(workspace.RunFailed):
    pass


def expected_inputs(c: workspace.Checkout, branch: str, local: str) -> Inputs:
    base = c.recovery_base or c.base_commit
    refs = Refs(c.pr, "OPEN", branch, c.lease, c.base, base, False, c.lease, base)
    return Inputs(c, branch, c.recovery_head or c.lease, local, refs)


def pushed_note(branch: str, pushed: dict[str, str]) -> str:
    done = ", ".join(f"{repo}:{branch}@{head}" for repo, head in pushed.items())
    return f"Already pushed {done}."


def blocked(row: dict) -> dict:
    try:
        found = json.loads(row.get("failure") or "{}")
    except (TypeError, ValueError):
        return {}
    return found if isinstance(found, dict) and found.get("reason") == REASON else {}


def owner_discussion(comments: str) -> str:
    return re.sub(r"^### Arete(?: on \d{4}-\d{2}-\d{2})?\n\n.*?(?=^### |\Z)", "", comments,
                  flags=re.MULTILINE | re.DOTALL).strip().removeprefix("## Comments").strip()


def is_reply(body: str) -> bool:
    return bool(body.strip()) and not tracker.from_arete(body) and not gh.has_arete_mark(body)


def ticket_replies(source: tracker.Tracker, tid: str, since: str = "") -> list[tuple[int, str]] | str:
    if isinstance(source, tracker.GitHubTracker):
        comments = gh.api_list(f"repos/{source.repo.github}/issues/{int(tid)}/comments")
        return sorted((c["id"], body) for c in comments if source.by_owner({"author": c.get("user")})
                      and (not since or (c.get("created_at") or "") >= since)
                      and is_reply(body := (c.get("body") or "").strip()))
    return source.reply_since(tid, since or "1970-01-01T00:00:00Z")


def pr_replies(url: str, repo: str, tid: str) -> list[dict]:
    from .outcomes import owner_feedback

    if not gh.me():
        raise gh.GhError("GitHub could not identify the account that handles PR feedback")
    return [row for row in owner_feedback(url, "OPEN", repo, tid) if is_reply(row["body"])]


def answered_asks(run_id: str) -> list[dict]:
    return [a for run in sorted({run_id} | factory.chain_runs(run_id)) for a in asks.asks_of(run) if a["answers"]]


def decision_for(run_id: str, source: tracker.Tracker, tid: str, pr: str, *, replies: list[dict] | None = None) -> str:
    if replies is None:
        replies = pr_replies(pr, source.repo.name, tid)
    review = sorted((row["kind"], row["gh_id"], row["body"]) for row in replies)
    answers = [[a["id"], a["answers"]] for a in answered_asks(run_id)]
    return DECISION_VERSION + orchestration.digest([ticket_replies(source, tid), review, answers])


def reply_keys_since(rows: list[dict], since: str) -> list[list]:
    return sorted([r["kind"], r["gh_id"]] for r in rows if (r.get("created") or "") >= since)


def new_owner_input(run_id: str, source: tracker.Tracker, tid: str, pr: str, since: str) -> bool:
    return bool(reply_keys_since(pr_replies(pr, source.repo.name, tid), since)
                or ticket_replies(source, tid, since)
                or any((a.get("answered") or "") >= since for a in answered_asks(run_id)))


def fresh_feedback_after_hold(row: dict, held: dict, feedback: list[dict]) -> bool:
    since = held.get("held_at") or row.get("finished")
    if not since:
        return False
    fresh = [r for r in feedback if r["pr_url"] == held["pr"] and r["pr_state"] == "OPEN"
             and r["status"] == "new" and r["run_id"] is None and is_reply(r["body"] or "")]
    return any(key not in held.get("seen_replies", ()) for key in reply_keys_since(fresh, since))


def decision(run_id: str, _display_comments: str) -> str:
    row = runs.run_get(run_id) or {}
    if not (held := blocked(row)):
        raise ValueError("the saved PR hold changed before reading its owner discussion")
    cfg = config.load()
    tid = str(row["issue"])
    source = tracker.for_ticket(cfg, cfg.repo(row["repo"]), tid)
    return decision_for(run_id, source, tid, held["pr"])


def notice(repo: str, ticket: str, failure: dict) -> bool:
    key = orchestration.digest([repo, ticket, *(failure[k] for k in
                               ("pr", "branch", "accepted", "local", "refs", "decision"))])
    return unblock.notice(f"open-pr-recovery:{key}")


def fetch_inputs(inputs: Inputs) -> None:
    c, refs = inputs.checkout, inputs.refs
    with util.repo_lock(c.src):
        util.git(["fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", "origin",
                  f"refs/heads/{inputs.branch}", f"refs/heads/{c.base}"], c.src, timeout=60, retry=False)
    for oid in (refs.head, refs.base):
        util.git(["cat-file", "-e", f"{oid}^{{commit}}"], c.src)


def merge(inputs: Inputs, wt: Path, head: str, label: str, run_id: str) -> None:
    if ancestor(wt, head, util.git(["rev-parse", "HEAD"], wt).strip()):
        return
    try:
        util.git_as_owner([*NO_HOOKS, "-c", "commit.gpgsign=false", *MERGE, "--no-edit", "-m",
                           f"Reconcile {label} for the open PR\n\nArete-Run: {run_id}", head], wt)
    except RuntimeError as error:
        conflicts = util.git_records(["diff", "--name-only", "--diff-filter=U"], wt)
        if not conflicts:
            raise
        inputs.recheck(during="during isolated reconciliation")
        paths = ", ".join(repr(p) for p in sorted(conflicts))
        raise inputs.hold(f"merging {label} {head} into the candidate conflicts in {paths}",
                          "Resolve the named files in a separate checkout, preserving both commit histories on the "
                          "PR branch, then retry without resetting the retained worktree") from error


def unowned(c: workspace.Checkout, head: str, remote: str, base: str, runs: set[str]) -> list[str]:
    log = util.git(["log", f"--format={repairs.TRAILER}", head, f"^{remote}", f"^{base}"], c.wt)
    commits = [row.partition("\t") for row in log.splitlines()]
    return [sha for sha, _, tags in commits if not runs.intersection(t.strip() for t in tags.split(","))]


def colliding_paths(c: workspace.Checkout, before: str, candidate: str) -> list[str]:
    dirty = set(workspace.uncommitted(c.wt)) | workspace.ignored_file_paths(c.wt)
    ignore_case = util.git(["config", "--bool", "core.ignoreCase"], c.wt, check=False).strip() == "true"

    def path_of(name: str) -> Path:
        return Path(name.casefold() if ignore_case else name)

    changed = {path_of(name) for name in util.git_records(["diff", "--no-renames", "--name-only", before, candidate], c.wt)}
    dirs = {parent for path in changed for parent in path.parents}
    return sorted(name for name in dirty if (path := path_of(name)) in changed
                  or path in dirs or not changed.isdisjoint(path.parents))


def rule_dirs(names: set[str] | list[str]) -> set[Path]:
    return {Path(n.casefold()).parent for n in names if Path(n).name.casefold() == ".gitignore"}


def incoming_ignore_dirs(wt: Path, *revs: str) -> set[Path]:
    return rule_dirs(util.git_records(["diff", "--no-renames", "--name-only", *revs], wt))


def under_rules(name: str, dirs: set[Path]) -> bool:
    return not dirs.isdisjoint(Path(name.casefold()).parents)


def affected_retained(wt: Path, dirs: set[Path]) -> dict[str, bool]:
    if not dirs:
        return {}
    return {name: ignored for ignored, names in ((False, workspace.untracked_files(wt)),
                                                  (True, workspace.ignored_file_paths(wt)))
            for name in names if under_rules(name, dirs)}


def check_ignore_changes(i: Inputs, wt: Path, affected: dict[str, bool]) -> None:
    if not affected:
        return
    retained = i.checkout.wt
    retained_rules = rule_dirs(set(workspace.uncommitted(retained)) | workspace.ignored_file_paths(retained))
    result = util.sh(["git", "check-ignore", "--no-index", "-z", "--stdin"], cwd=wt,
                     input="\0".join(sorted(affected)) + "\0")
    ignored = set(result.stdout.split("\0")) - {""}
    hidden, exposed = [], []
    for name, was_ignored in sorted(affected.items()):
        if (result.returncode not in (0, 1) or under_rules(name, retained_rules) or (name in ignored) != was_ignored):
            (exposed if was_ignored else hidden).append(name)
    if hidden or exposed:
        i.recheck(during="during isolated reconciliation")
        changes = []
        if hidden:
            changes.append(f"incoming ignore rules can hide retained untracked files: {', '.join(map(repr, hidden))}")
        if exposed:
            changes.append(f"incoming ignore rules can expose retained ignored files: {', '.join(map(repr, exposed))}")
        raise i.hold("; ".join(changes),
                     "Save the named files separately or have the owner adjust the PR's ignore rules, then retry")


def check_base_ignore_changes(c: workspace.Checkout, branch: str, head: str) -> None:
    dirs = incoming_ignore_dirs(c.wt, f"{head}...{c.base_commit}")
    if affected := affected_retained(c.wt, dirs):
        inputs = expected_inputs(c, branch, head)
        inputs.recheck(during="before merging the PR base")
        raise inputs.hold(f"incoming base ignore rules can change the visibility of retained files: "
                          f"{', '.join(map(repr, sorted(affected)))}",
                          "Save the named files separately or have the owner adjust the PR's ignore rules, then retry")


@dataclass(frozen=True)
class Plan:
    inputs: Inputs
    candidate: str
    fingerprint: str
    ignore_dirs: set[Path]
    affected: dict[str, bool]

    def check_local(self, ws: Path, src: Path, copies: list[str]) -> None:
        i, c = self.inputs, self.inputs.checkout
        if (util.git(["symbolic-ref", "-q", "HEAD"], c.wt, check=False).strip() != f"refs/heads/{i.branch}"
                or current_head(c) != i.local or affected_retained(c.wt, self.ignore_dirs) != self.affected
                or factory.fingerprint(c.wt) != self.fingerprint
                or workspace.kept_checkout(ws, c, src, copies, commits=False)):
            raise i.hold("the retained worktree changed during recovery", "Save or restore the retained edits and retry",
                         local=current_head(c))
        if paths := colliding_paths(c, i.local, self.candidate):
            raise i.hold(f"incoming changes overlap retained uncommitted or ignored files: {', '.join(map(repr, paths))}",
                         "Save those files separately, restore the recorded worktree, and retry")

    def install(self) -> None:
        i, c = self.inputs, self.inputs.checkout
        if self.candidate != i.local:
            try:
                util.git([*NO_HOOKS, *MERGE, "--ff-only", self.candidate], c.wt)
            except RuntimeError as error:
                why = util.first_line(vault.hidden(str(error)), 300)
                raise i.hold(f"the retained worktree refused candidate {self.candidate} ({why})",
                             "Inspect the local branch and index, then retry without resetting either version",
                             local=current_head(c)) from error
        head = current_head(c)
        on = util.git(["symbolic-ref", "-q", "HEAD"], c.wt, check=False).strip()
        if head != self.candidate or on != f"refs/heads/{i.branch}":
            raise i.hold(f"the local checkout moved to {on or 'a detached HEAD'} while installing {self.candidate}",
                         "Inspect the retained branch before retrying", local=head)
        c.recovery_head, c.recovery_base = i.refs.head, i.refs.base
        c.note_pushed_head(i.refs.head)
        c.base_commit, c.resumes = i.refs.base, True
        c.onto, c.merge, c.resolved, c.resolved_lines = "", "", [], 0


def plan(c: workspace.Checkout, branch: str, accepted: str, local: str, remote: str, runs: set[str], run_id: str,
         recorded: str, deny: list[str]) -> Plan:
    refs = observe(c, branch)
    i = Inputs(c, branch, accepted, local, refs)
    if not refs.target_matches(c, branch):
        raise i.hold(f"expected the same open, non-fork PR from {branch} to {c.base}",
                     "Restore the saved PR target or reconcile the PR manually before resuming")
    if not refs.origin:
        raise i.hold(f"origin/{branch} is missing", "Restore the published branch before resuming")
    if (refs.origin != remote or refs.head != refs.origin or refs.base != refs.origin_base
            or not all(OID.fullmatch(oid) for oid in (refs.head, refs.base))):
        raise i.hold(f"GitHub and origin did not agree with the observed refs (initial origin head {remote or 'missing'}, "
                     f"origin base {refs.origin_base or 'missing'})", "Wait for the refs to agree, then retry")
    if not OID.fullmatch(accepted):
        raise i.hold("no previously accepted PR head was recorded", "Reconcile the PR and retained work manually")
    try:
        fetch_inputs(i)
    except RuntimeError:
        i.recheck(during="while fetching the observed commits")
        raise
    if not ancestor(c.wt, accepted, refs.head):
        raise i.hold(f"origin/{branch} no longer contains {accepted}",
                     "Restore additive history containing the accepted head or reconcile the rewrite manually")
    if recorded != local:
        raise i.hold(f"the retained commit does not match the workspace record ({recorded or 'missing'})",
                     "Save any local work and restore the recorded retained head before resuming")
    if not (ancestor(c.wt, accepted, local) or ancestor(c.wt, local, accepted)):
        raise i.hold("the retained history does not contain the accepted history", "Reconcile the local history manually")
    if foreign := unowned(c, local, refs.head, refs.base, runs):
        raise i.hold(f"the retained history includes unowned commits: {', '.join(foreign)}",
                     "Set those commits aside or have their owner reconcile them explicitly on the PR")
    if not (fingerprint := factory.fingerprint(c.wt)):
        raise i.hold("the retained index and files could not be read", "Inspect the retained worktree and retry")
    with TemporaryDirectory(prefix="arete-pr-recovery-") as folder:
        wt = Path(folder) / "candidate"
        with util.repo_lock(c.src):
            util.git([*NO_HOOKS, "worktree", "add", "--detach", str(wt), local], c.src)
        try:
            merge(i, wt, refs.head, "PR head", run_id)
            merge(i, wt, refs.base, "PR base", run_id)
            candidate = util.git(["rev-parse", "HEAD"], wt).strip()
            if not all(ancestor(wt, oid, candidate) for oid in (refs.head, refs.base, local)):
                raise i.hold("the candidate does not preserve all inputs", "Reconcile the histories manually")
            diff = diffguard.between(wt, refs.base, candidate, deny=deny, prefix=c.prefix)
            if diff.denied:
                raise i.hold(f"the candidate differs from the PR base in protected files: {', '.join(map(repr, diff.denied))}",
                             "Have the owner reconcile the protected paths on the PR before resuming")
            dirs = incoming_ignore_dirs(wt, local, candidate)
            affected = affected_retained(c.wt, dirs)
            check_ignore_changes(i, wt, affected)
        finally:
            with util.repo_lock(c.src):
                util.git([*NO_HOOKS, "worktree", "remove", "--force", str(wt)], c.src)
    return Plan(i, candidate, fingerprint, dirs, affected)


class Publication:
    def __init__(self, checkouts: list[workspace.Checkout], branch: str, runs: set[str], *,
                 record_pushes: Callable[[dict[str, str]], None]):
        self.checkouts, self.branch, self.runs = checkouts, branch, runs
        self.trees = {c.github: util.git(["write-tree"], c.wt).strip() for c in checkouts}
        self.targets = {c.github: expected_inputs(c, branch, "") for c in checkouts if c.pr}
        self.leases = {c.github: c.lease for c in checkouts}
        self.pushed: dict[str, str] = {}
        self.record_pushes = record_pushes

    @property
    def effected(self) -> bool:
        return bool(self.pushed)

    def bound(self, c: workspace.Checkout) -> bool:
        return c.github in self.targets

    def expected_head(self, c: workspace.Checkout) -> str:
        return self.pushed.get(c.github, self.leases[c.github])

    def inputs(self, c: workspace.Checkout, head: str) -> Inputs:
        target = self.targets[c.github]
        expected = self.expected_head(c)
        return replace(target, local=head, refs=replace(target.refs, head=expected, origin=expected))

    def failure(self, why: str, detail: str = "") -> workspace.RunFailed:
        if self.pushed:
            why = f"{why}. {pushed_note(self.branch, self.pushed)} Reconcile the partial publication before resuming"
        return workspace.RunFailed(vault.hidden(why), detail)

    def recheck(self, during: str = "before publication") -> None:
        for c in self.checkouts:
            if self.bound(c):
                inputs = self.inputs(c, current_head(c))
                try:
                    inputs.recheck(during=during, pushed=self.pushed)
                except (RuntimeError, OSError, ValueError) as error:
                    if not self.pushed:
                        raise
                    raise inputs.publication_unconfirmed(f"could not confirm the refs {during}", None, self.pushed) from error
            else:
                expected, observed = self.expected_head(c), workspace.remote_head(c.src, self.branch)
                if observed != expected:
                    raise self.failure(f"{c.github}: origin/{self.branch} moved {during} "
                                       f"(expected head {expected or 'missing'}, observed head {observed or 'missing'})")

    def push(self, c: workspace.Checkout) -> None:
        head, expected = current_head(c), self.expected_head(c)
        if (util.git(["rev-parse", f"{head}^{{tree}}"], c.wt).strip() != self.trees[c.github]
                or (expected and not ancestor(c.wt, expected, head))):
            raise self.failure(f"{c.github}: {head} is not the gated candidate tree on top of {expected}")
        if c.recovery_head:
            if not ancestor(c.wt, c.recovery_base, head):
                raise self.failure(f"{c.github}: {head} no longer contains the observed PR base {c.recovery_base}")
            if foreign := unowned(c, head, expected, c.recovery_base, self.runs):
                why = f"the candidate includes unowned commits: {', '.join(foreign)}"
                if self.pushed:
                    raise self.failure(f"{c.github}: {why}")
                raise self.inputs(c, head).hold(why, "Restore the recorded run-owned history before retrying")
        ref = f"refs/heads/{self.branch}"
        args = ["push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", f"--force-with-lease={ref}:{expected}",
                "origin", f"{head}:{ref}"]
        self.recheck(during="immediately before the leased push")
        res = util.sh(["git", *args], cwd=c.wt, timeout=600, keeps=("ssh-agent",))
        if res.returncode:
            rejected = res.returncode == 1 and any(
                flag == "!" and refs.endswith(f":{ref}") and reason.startswith(("[rejected]", "[remote rejected]"))
                for flag, refs, reason in (line.split("\t", 2) for line in res.stdout.splitlines() if line.count("\t") >= 2))
            if self.bound(c):
                inputs = self.inputs(c, head)
                try:
                    refs = observe(c, self.branch)
                except (RuntimeError, OSError, ValueError):
                    refs = None
                if rejected and refs is not None and refs != inputs.refs:
                    raise inputs.moved(refs, during="during the leased push", pushed=self.pushed)
                raise inputs.publication_unconfirmed("the leased push did not confirm publication", refs, self.pushed)
            try:
                observed = workspace.remote_head(c.src, self.branch) or "missing"
            except (RuntimeError, OSError, ValueError):
                observed = "unconfirmed"
            raise self.failure(f"the leased push to {c.github} failed (expected head {expected or 'missing'}, "
                               f"attempted head {head}, observed {observed}); inspect the PR and branch before retrying",
                               util.git_error(vault.hidden(res.stderr or res.stdout)))
        self.pushed[c.github] = head
        c.note_pushed_head(head)
        c.onto = ""
        self.record_pushes(dict(self.pushed))
