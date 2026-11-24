from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Collection
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from . import diffguard, factory, gh, repairs, tracker, util, vault, workspace
from .store import asks, orchestration, unblock

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

    def aligned(self) -> bool:
        return (self.head == self.origin and self.base == self.origin_base
                and all(OID.fullmatch(oid) for oid in (self.head, self.base)))


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


def current_ref(c: workspace.Checkout) -> str:
    return util.git(["symbolic-ref", "-q", "HEAD"], c.wt, check=False).strip()


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

    def moved(self, refs: Refs, *, during: str, pushed: dict[str, str] | None = None, updated: bool = False) -> Hold:
        was = self.refs
        change = (f"the PR or origin moved {during} (expected head {was.head}, origin head {was.origin}, "
                  f"base {was.base_branch}@{was.base}, origin base {was.origin_base}; now {refs.state or 'unknown'} "
                  f"on {refs.branch or 'unknown'} targeting {refs.base_branch}@{refs.base or 'missing'}, "
                  f"origin base {refs.origin_base or 'missing'})")
        if updated and not pushed:
            return self.publication_unconfirmed(f"{change} after a PR update was attempted", refs, {})
        action = (f"{pushed_note(self.branch, pushed)} Reconcile the partial publication before resuming" if pushed
                  else "Inspect the new head and retry against it; no push was made by this attempt")
        error = self.hold(change, action, observed=refs)
        error.recovery.update(expected_refs=asdict(was), **({"pushed": dict(pushed)} if pushed else {}))
        return error

    def matches(self, refs: Refs) -> bool:
        return refs == self.refs and refs.target_matches(self.checkout, self.branch)

    def recheck(self, *, during: str, pushed: dict[str, str] | None = None) -> None:
        if not self.matches(refs := observe(self.checkout, self.branch)):
            raise self.moved(refs, during=during, pushed=pushed)


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


def reply_key(row: dict) -> tuple[str, int]:
    return row["kind"], row["gh_id"]


def reply_keys(rows: list[dict] | None) -> list[list] | None:
    return None if rows is None else sorted(list(reply_key(row)) for row in rows)


def reply_snapshot(held: dict, pr: str) -> set[tuple[str, int]] | None:
    legacy = held.get("seen_replies") if pr == held["pr"] else None
    seen = held.get("seen_replies_by_pr", {}).get(pr, legacy)
    return None if seen is None else {tuple(key) for key in seen}


def captured_reply_keys(held: dict) -> set[tuple[str, int]]:
    return reply_snapshot(held, held["pr"]) or set()


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


def unowned(wt: Path, head: str, bases: Collection[str], runs: set[str]) -> list[str]:
    log = util.git(["log", f"--format={repairs.TRAILER}", head, *(f"^{base}" for base in bases if base)], wt)
    return [sha for sha, _, tags in (row.partition("\t") for row in log.splitlines())
            if not runs.intersection(t.strip() for t in tags.split(","))]


def overlapping_paths(names: Collection[str], changed: Collection[str], *, ignore_case: bool = True) -> list[str]:
    def path_of(name: str) -> Path:
        return Path(name.casefold() if ignore_case else name)

    targets = {path_of(name) for name in changed}
    parents = {parent for target in targets for parent in target.parents}
    return sorted(name for name in names if (path := path_of(name)) in targets
                  or path in parents or not targets.isdisjoint(path.parents))


def rule_dirs(names: Collection[str]) -> set[Path]:
    return {p.parent for n in names if (p := Path(n.casefold())).name == ".gitignore"}


def under_rules(name: str, dirs: set[Path]) -> bool:
    return not dirs.isdisjoint(Path(name.casefold()).parents)


@dataclass(frozen=True)
class Retained:
    visible: set[str]
    ignored: set[str]

    @property
    def names(self) -> set[str]:
        return self.visible | self.ignored


def loose_files(wt: Path) -> Retained:
    return Retained(workspace.untracked_files(wt), workspace.ignored_file_paths(wt))


def path_hold(c: workspace.Checkout, why: str, paths: Collection[str], action: str = "Inspect the named paths and retry",
              inputs: Inputs | None = None) -> workspace.RunFailed:
    why = f"{why}: {', '.join(map(repr, sorted(paths)))}"
    return inputs.hold(why, action) if inputs else workspace.RunFailed(vault.hidden(f"{c.github}: {why}. {action}"))


def retained_snapshot(inputs: Inputs, files: Retained) -> dict[str, str]:
    c = inputs.checkout
    try:
        snapshot = {name: workspace.fingerprint(c.wt / name, "") for name in files.names}
    except OSError as error:
        raise path_hold(c, "could not verify the retained files", files.names, inputs=inputs) from error
    if missing := {name for name, digest in snapshot.items() if digest == "missing"}:
        raise path_hold(c, "could not verify the retained files", missing, inputs=inputs)
    return snapshot


def retained_excludes(wt: Path) -> Path | None:
    result = util.sh(["git", "config", "--null", "--path", "--get", "core.excludesFile"], cwd=wt)
    if result.returncode not in (0, 1):
        raise RuntimeError("could not read the retained worktree's Git exclude settings")
    value = result.stdout.removesuffix("\0")
    return wt.absolute() / value if value else None


def retained_record(wt: Path) -> Path | None:
    dotgit = wt / ".git"
    if not dotgit.is_dir():
        try:
            raw = dotgit.read_bytes()
        except FileNotFoundError:
            return None
        if not raw.startswith(b"gitdir: "):
            return None
        dotgit = wt / os.fsdecode(raw.removeprefix(b"gitdir: ").rstrip(b"\r\n"))
    return dotgit / "arete-retained-ignored.json"


def read_retained(wt: Path) -> Retained:
    try:
        data = json.loads(path.read_bytes()) if (path := retained_record(wt)) else {}
    except FileNotFoundError:
        data = {}
    if isinstance(data, list):
        data = {"ignored": data}
    if not isinstance(data, dict) or data.keys() - {"visible", "ignored"}:
        raise ValueError("the retained file record is invalid")
    visible, ignored = data.get("visible", []), data.get("ignored", [])
    for names in (visible, ignored):
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError("the retained file record is invalid")
    return Retained(set(visible), set(ignored))


def existing_paths(c: workspace.Checkout, names: set[str], inputs: Inputs | None = None) -> set[str]:
    present = set()
    for name in sorted(names):
        try:
            (c.wt / name).lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            raise path_hold(c, "could not inspect retained files", [name], inputs=inputs) from error
        present.add(name)
    return present


def check_retained(c: workspace.Checkout, inputs: Inputs | None = None, *, files: Retained | None = None,
                   remember: bool = False, forced: Collection[str] = (), found: Retained | None = None) -> Retained:
    saved = read_retained(c.wt)
    if files is not None:
        saved = Retained(saved.visible | files.visible, saved.ignored | files.ignored)
    if not saved.names:
        return saved
    try:
        found = found if found is not None else loose_files(c.wt)
        indexed = indexed_paths(c.wt)
    except (RuntimeError, OSError) as error:
        raise path_hold(c, "could not check retained file classifications", saved.names, inputs=inputs) from error
    exposed = (saved.ignored & indexed) | saved.ignored.intersection(forced)
    exposed |= existing_paths(c, saved.ignored - found.ignored, inputs)
    if exposed:
        raise path_hold(c, "staging would expose retained ignored files", exposed,
                        "Save the named files separately or restore their ignore rules, then retry", inputs)
    if hidden := saved.visible & found.ignored:
        raise path_hold(c, "ignore rules now hide retained untracked files", hidden,
                        "Save the named files separately or restore their visibility, then retry", inputs)
    if unclear := existing_paths(c, saved.visible - found.visible - indexed, inputs):
        raise path_hold(c, "could not account for retained untracked files", unclear, inputs=inputs)
    if remember:
        try:
            if not (path := retained_record(c.wt)):
                raise OSError("missing worktree gitdir")
            util.write_json(path, {"visible": sorted(saved.visible), "ignored": sorted(saved.ignored)})
        except OSError as error:
            raise path_hold(c, "could not record retained files", saved.names,
                            "Inspect the checkout and retry", inputs) from error
    return saved


def check_staging(checkouts: list[workspace.Checkout], forced: dict[str, list[str]] | None = None) -> dict[str, Retained]:
    return {c.github: check_retained(c, forced=(forced or {}).get(c.github, ())) for c in checkouts}


def indexed_paths(wt: Path) -> set[str]:
    return set(util.git_records(["ls-files", "--cached"], wt))


def apply_pathspec(wt: Path, args: list[str], excluded: Collection[str], *paths: str) -> None:
    spec = [".", *paths, *(f":(top,exclude,literal){name}" for name in sorted(excluded))]
    util.git([*args, "--pathspec-from-file=-", "--pathspec-file-nul"], wt, input="\0".join(spec) + "\0")


def add_retained_safe(c: workspace.Checkout, retained: Retained) -> None:
    if not retained.names:
        diffguard.add(c.wt)
        return
    args = ["ls-files", "--others", "--exclude-standard", "--", ".", *diffguard.NEVER_STAGED]
    before = set(util.git_records(args, c.wt)) if retained.visible else set()
    apply_pathspec(c.wt, ["add", "-A"], retained.ignored, *diffguard.NEVER_STAGED)
    check_retained(c)
    if retained.visible and (missing := ((before | set(util.git_records(args, c.wt))) & retained.visible)
                             - indexed_paths(c.wt)):
        raise path_hold(c, "staging could not preserve retained untracked files", missing)


def cleanup_paths(c: workspace.Checkout) -> set[str]:
    saved = read_retained(c.wt)
    if not saved.names:
        return set()
    clean = set(util.git_records(["ls-tree", "-r", "--name-only", "HEAD"], c.wt)) - set(workspace.uncommitted(c.wt))
    return existing_paths(c, saved.names - (clean - saved.ignored))


def ignored_by_candidate(wt: Path, git_dir: Path, paths: list[str], override: list[str]) -> set[str]:
    result = util.sh(["git", f"--git-dir={git_dir}", f"--work-tree={wt}", *override,
                     "check-ignore", "--no-index", "-z", "--stdin"], cwd=wt, input="\0".join(paths) + "\0")
    if result.returncode not in (0, 1):
        raise RuntimeError("could not prove which retained files the candidate ignores")
    return set(result.stdout.split("\0")) - {""}


def check_ignore_changes(i: Inputs, wt: Path, files: Retained, changed: list[str]) -> None:
    retained = i.checkout.wt
    excludes = retained_excludes(retained)
    paths = [str(root / name) for root in {retained.absolute(), retained.resolve()} for name in changed]
    excludes_changed = excludes is not None and bool(overlapping_paths([str(excludes), str(excludes.resolve())], paths))
    dirs = rule_dirs(changed)
    names = sorted(n for n in files.names if excludes_changed or under_rules(n, dirs))
    if not names:
        return
    rules = rule_dirs(set(workspace.uncommitted(retained)) | files.ignored)
    git_dir = util.git(["rev-parse", "--absolute-git-dir"], retained).rstrip("\n")
    override = ["-c", f"core.excludesFile={excludes}"] if excludes is not None else []
    try:
        ignored = ignored_by_candidate(wt, Path(git_dir), names, override)
    except (OSError, RuntimeError) as error:
        raise path_hold(i.checkout, "could not verify the candidate's ignore rules for retained files", names,
                        "Save the named files separately or inspect the ignore rules, then retry", i) from error
    flipped = {n for n in names if excludes_changed or under_rules(n, rules) or (n in ignored) != (n in files.ignored)}
    if flipped:
        i.recheck(during="during isolated reconciliation")
        why = "; ".join(f"incoming ignore rules can {reason}: {', '.join(map(repr, sorted(paths)))}" for reason, paths in
                        (("hide retained untracked files", flipped & files.visible),
                         ("expose retained ignored files", flipped & files.ignored)) if paths)
        raise i.hold(why, "Save the named files separately or have the owner adjust the PR's ignore rules, then retry")


@dataclass(frozen=True)
class Plan:
    inputs: Inputs
    candidate: str
    fingerprint: str
    retained: Retained
    files: dict[str, str]

    def check_files(self, *, installed: bool = False) -> None:
        i = replace(self.inputs, local=self.candidate) if installed else self.inputs
        found = loose_files(i.checkout.wt)
        check_retained(i.checkout, i, files=self.retained, found=found)
        current = retained_snapshot(i, found)
        if changed := {name for name in current.keys() | self.files.keys() if current.get(name) != self.files.get(name)}:
            raise path_hold(i.checkout, "the retained files changed during recovery", changed,
                            "Inspect the named paths before resuming", i)

    def check_local(self, ws: Path, src: Path, copies: list[str]) -> None:
        i, c = self.inputs, self.inputs.checkout
        self.check_files()
        if (current_ref(c) != f"refs/heads/{i.branch}"
                or current_head(c) != i.local or factory.fingerprint(c.wt) != self.fingerprint
                or workspace.kept_checkout(ws, c, src, copies, commits=False)):
            raise i.hold("the retained worktree changed during recovery", "Save or restore the retained edits and retry",
                         local=current_head(c))
        dirty = set(workspace.uncommitted(c.wt)) | workspace.ignored_file_paths(c.wt)
        changed = util.git_records(["diff", "--no-renames", "--name-only", i.local, self.candidate], c.wt)
        ignore_case = util.git(["config", "--bool", "core.ignoreCase"], c.wt, check=False).strip() == "true"
        if paths := overlapping_paths(dirty, changed, ignore_case=ignore_case):
            raise path_hold(c, "incoming changes overlap retained uncommitted or ignored files", paths,
                            "Save those files separately, restore the recorded worktree, and retry", i)

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
        head, on = current_head(c), current_ref(c)
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
    if refs.origin != remote or not refs.aligned():
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
    if foreign := unowned(c.wt, local, (refs.head, refs.base), runs):
        raise i.hold(f"the retained history includes unowned commits: {', '.join(foreign)}",
                     "Set those commits aside or have their owner reconcile them explicitly on the PR")
    retained = loose_files(c.wt)
    check_retained(c, i, found=retained)
    files = retained_snapshot(i, retained)
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
            changed = util.git_records(["diff", "--no-renames", "--name-only", local, candidate], wt)
            check_ignore_changes(i, wt, retained, changed)
        finally:
            with util.repo_lock(c.src):
                util.git([*NO_HOOKS, "worktree", "remove", "--force", str(wt)], c.src)
    return Plan(i, candidate, fingerprint, retained, files)


def pending_receipt(data: dict) -> dict:
    return (data.get("publication_intent") or {}).get("recovery_receipt") or {}


def check_no_pr_refs(c: workspace.Checkout, branch: str) -> None:
    observed = workspace.remote_head(c.src, branch)
    if observed != c.lease:
        raise factory.Refused(vault.hidden(f"{c.github}: origin/{branch} moved while settling publication "
                                           f"(expected {c.lease or 'absent'}, observed {observed or 'absent'}); "
                                           "reconcile the sibling branch before resuming"))
    if url := gh.open_pr_url(c.github, branch):
        raise factory.Refused(vault.hidden(f"{c.github}: found {url} on the sibling branch without a PR in the receipt; "
                                           "reconcile that PR before resuming"))


def settle(checkouts: list[workspace.Checkout], branch: str, made: dict, intent: dict, owners: set[str]) -> None:
    saved = intent["recovery_receipt"]
    repos, targets = {c.github for c in checkouts}, {c.github for c in checkouts if c.pr}
    attempts, pushed = intent.get("attempts", {}), intent.get("pushed", {})
    if (not branch or not targets or len(repos) != len(checkouts) or branch != saved["branch"]
            or targets != saved["targets"].keys() or repos != saved["trees"].keys()
            or orchestration.digest(saved["trees"]) != intent["tree"] or not (pushed.keys() | attempts.keys()) <= targets
            or any(e["kind"] == "PR creation" or e["github"] not in targets for e in intent.get("pr_effects", ()))):
        raise factory.Refused("the interrupted publication lacks a matching receipt for its checkouts and PR effects; "
                             "inspect its effects manually")
    verified, without_pr = [], []
    for c in checkouts:
        head, tree = current_head(c), saved["trees"][c.github]
        target = saved["targets"].get(c.github)
        i = Inputs(c, branch, target["accepted"], head, Refs(**target["refs"])) if target else None
        check_retained(c, i)
        if (not OID.fullmatch(head) or workspace.head_recorded(made, c) != head
                or current_ref(c) != f"refs/heads/{branch}" or factory.fingerprint(c.wt) != tree):
            raise factory.Refused(vault.hidden(f"{c.pr or c.github}: retained head {head} does not match the gated "
                                               "publication receipt; save any new edits and restore the recorded candidate "
                                               "before resuming"))
        if i is None:
            base = c.base_commit or c.revision
            if (not OID.fullmatch(base) or not ancestor(c.wt, base, head)
                    or (c.lease and (not OID.fullmatch(c.lease) or not ancestor(c.wt, c.lease, head)))
                    or any(util.git(["rev-parse", f"{oid}^{{tree}}"], c.wt).strip() != tree for oid in (head, base))
                    or workspace.uncommitted(c.wt) or unowned(c.wt, head, (c.lease, base), owners)):
                raise factory.Refused(vault.hidden(f"{c.github}: retained head {head} is not an unchanged sibling without "
                                                   "PR effects; reconcile it against the recorded base before resuming"))
            check_no_pr_refs(c, branch)
            without_pr.append(c)
            continue
        expected = i.refs
        confirmed = pushed.get(c.github, expected.head)
        attempted = attempts.get(c.github, confirmed)
        if (not expected.target_matches(c, branch) or not expected.aligned()
                or not all(OID.fullmatch(oid) for oid in (confirmed, attempted, i.accepted))
                or (c.github in attempts and attempted != head)
                or not all(ancestor(c.wt, oid, head) for oid in (expected.head, expected.base, confirmed, i.accepted))
                or unowned(c.wt, head, (expected.head, expected.base), owners)):
            raise factory.Refused(vault.hidden(f"{c.pr}: retained head {head} does not match the gated publication "
                                               f"receipt (expected {expected.head}, attempted {attempted}); save any "
                                               "new edits and restore the recorded candidate before resuming"))
        seen = observe(c, branch)
        if not seen.target_matches(c, branch) or not seen.aligned():
            raise i.moved(seen, during="while settling the publication", pushed=pushed)
        fetch_inputs(replace(i, refs=seen))
        if not ancestor(c.wt, confirmed, seen.head):
            raise i.moved(seen, during="while settling the publication", pushed=pushed)
        if not ancestor(c.wt, attempted, seen.head):
            raise i.hold(f"the attempted publication {attempted} is not in the observed PR history",
                         "Inspect the attempted push and reconcile that commit onto the PR before resuming; "
                         "Arete will not resend it", observed=seen)
        verified.append((replace(i, refs=seen), attempted, expected.base))
    for c in without_pr:
        check_no_pr_refs(c, branch)
    for i, _, _ in verified:
        i.recheck(during="while settling the publication", pushed=pushed)
    for i, accepted, base in verified:
        c = i.checkout
        c.note_pushed_head(accepted)
        c.recovery_head, c.recovery_base = accepted, base


class Publication:
    def __init__(self, checkouts: list[workspace.Checkout], branch: str, runs: set[str], *,
                 record: Callable[..., None], record_attempt: Callable[[str, str], None],
                 settled: dict | None = None, settled_effects: Collection[dict] = ()):
        self.checkouts, self.branch, self.runs = checkouts, branch, runs
        self.trees = {c.github: util.git(["write-tree"], c.wt).strip() for c in checkouts}
        self.targets = {c.github: expected_inputs(c, branch, "") for c in checkouts if c.pr}
        self.leases = {c.github: c.lease for c in checkouts}
        self.record, self.record_attempt = record, record_attempt
        self.settled_effects = settled_effects
        self.pushed: dict[str, str] = {}
        self.pr_effects: list[dict[str, str]] = []
        if settled:
            if branch != settled["branch"] or self.leases.keys() != settled["heads"].keys():
                raise factory.Refused("the retained workspace no longer matches the recovered publication; reconcile it first")
            self.leases = dict(settled["heads"])
            self.pushed = dict(settled["pushed"])
            self.pr_effects = list(settled["pr_effects"])
            self.targets = {c.github: Inputs(c, branch, target["accepted"], "", Refs(**target["refs"]))
                            for c in checkouts if (target := settled["targets"].get(c.github))}

    @property
    def effected(self) -> bool:
        return bool(self.pushed or self.pr_effects)

    def snapshot(self) -> dict:
        return {"branch": self.branch, "heads": {c.github: self.expected_head(c) for c in self.checkouts},
                "targets": {repo: {"accepted": i.accepted, "refs": asdict(self.inputs(i.checkout, "").refs)}
                            for repo, i in self.targets.items()},
                "pushed": dict(self.pushed), "pr_effects": list(self.pr_effects)}

    def bind(self, c: workspace.Checkout) -> None:
        if not self.bound(c):
            self.targets[c.github] = expected_inputs(c, self.branch, "")

    def bound(self, c: workspace.Checkout) -> bool:
        return c.github in self.targets

    def expected_head(self, c: workspace.Checkout) -> str:
        return self.pushed.get(c.github, self.leases[c.github])

    def inputs(self, c: workspace.Checkout, head: str) -> Inputs:
        target = self.targets[c.github]
        expected = self.expected_head(c)
        return replace(target, local=head, refs=replace(target.refs, head=expected, origin=expected))

    def failure(self, why: str, detail: str = "") -> workspace.RunFailed:
        if self.effected:
            done = pushed_note(self.branch, self.pushed) if self.pushed else "A PR update was attempted;"
            why = f"{why}. {done} Reconcile the partial publication before resuming"
        return workspace.RunFailed(vault.hidden(why), detail)

    def recheck(self, during: str = "before publication") -> None:
        check_staging(self.checkouts)
        for c in self.checkouts:
            if self.bound(c):
                self.recheck_pr(c, during)
            else:
                expected, observed = self.expected_head(c), workspace.remote_head(c.src, self.branch)
                if observed != expected:
                    raise self.failure(f"{c.github}: origin/{self.branch} moved {during} "
                                       f"(expected head {expected or 'missing'}, observed head {observed or 'missing'})")

    def recheck_pr(self, c: workspace.Checkout, during: str) -> None:
        try:
            seen = observe(c, self.branch)
        except (RuntimeError, OSError, ValueError) as error:
            if not self.effected:
                raise
            raise self.ref_hold(c, during, None, why=f"could not confirm the refs {during}") from error
        if not self.inputs(c, "").matches(seen):
            raise self.ref_hold(c, during, seen)

    def ref_hold(self, c: workspace.Checkout, during: str, seen: Refs | None, *, why: str = "") -> Hold:
        inputs = self.inputs(c, current_head(c))
        error = (inputs.publication_unconfirmed(why, seen, self.pushed) if why or seen is None
                 else inputs.moved(seen, during=during, pushed=self.pushed, updated=bool(self.pr_effects)))
        if self.pr_effects:
            error.recovery["pr_effects"] = list(self.pr_effects)
        return error

    def before_pr_write(self, c: workspace.Checkout, kind: str, *, during: str) -> bool:
        if self.bound(c):
            self.recheck_pr(c, during)
        else:
            self.recheck(during=during)
        effect = {"kind": kind, "github": c.github, "pr": c.pr, "head": self.expected_head(c)}
        if effect in self.settled_effects:
            return False
        self.pr_effects.append(effect)
        self.record(pr_effects=list(self.pr_effects))
        return True

    def unconfirmed_pr_write(self, c: workspace.Checkout, *, during: str, why: str) -> Hold:
        self.recheck_pr(c, during)
        return self.ref_hold(c, during, self.inputs(c, "").refs, why=why)

    def comment(self, c: workspace.Checkout, body: str, *, kind: str, during: str) -> None:
        if not self.before_pr_write(c, kind, during=during):
            return
        head = self.expected_head(c)
        try:
            gh.pr_comment(c.pr, f"{kind} for head `{head}`\n\n{body}", single_attempt=True)
        except gh.GhUnsure as error:
            raise self.unconfirmed_pr_write(c, during=f"after posting {kind.lower()}",
                                            why=f"GitHub did not confirm the {kind.lower()} comment") from error

    def push(self, c: workspace.Checkout) -> None:
        head, expected = current_head(c), self.expected_head(c)
        if (util.git(["rev-parse", f"{head}^{{tree}}"], c.wt).strip() != self.trees[c.github]
                or (expected and not ancestor(c.wt, expected, head))):
            raise self.failure(f"{c.github}: {head} is not the gated candidate tree on top of {expected}")
        if self.bound(c):
            base = self.targets[c.github].refs.base
            if not ancestor(c.wt, base, head):
                raise self.failure(f"{c.github}: {head} no longer contains the observed PR base {base}")
            if foreign := unowned(c.wt, head, (expected, base), self.runs):
                why = f"the candidate includes unowned commits: {', '.join(foreign)}"
                if self.pushed:
                    raise self.failure(f"{c.github}: {why}")
                raise self.inputs(c, head).hold(why, "Restore the recorded run-owned history before retrying")
        ref = f"refs/heads/{self.branch}"
        args = ["push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", f"--force-with-lease={ref}:{expected}",
                "origin", f"{head}:{ref}"]
        self.recheck(during="immediately before the leased push")
        self.record_attempt(c.github, head)
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
                if rejected and refs is not None:
                    self.record_attempt(c.github, "")
                    if refs != inputs.refs:
                        raise inputs.moved(refs, during="during the leased push", pushed=self.pushed)
                    raise inputs.hold("Git rejected the leased push", "Inspect the remote branch protection and retry")
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
        self.record(pushed=dict(self.pushed))
