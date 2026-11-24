from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Collection
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from . import diffguard, factory, gh, repairs, tracker, util, vault, workspace
from .store import asks, orchestration, unblock

REASON = "open-pr-recovery"
DECISION_VERSION = "v2:"
GATE_EVIDENCE = "Recovered gate evidence"
PUSH_RESUME = ("If origin and GitHub confirm the exact recorded candidate on the same open PR and base, use Resume "
               "on the latest held run to verify the receipt and run fresh gates. Otherwise, preserve both versions "
               "and reconcile delivery manually in a separate checkout; Arete will not replay the push")
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
        seen = (f"PR head {refs.head or 'missing'}, origin head {refs.origin or 'missing'}, "
                f"PR base {refs.base_branch or 'unknown'}@{refs.base or 'missing'}, "
                f"origin base {refs.origin_base or 'missing'}" if refs else "unconfirmed")
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


def pr_replies(url: str, repo: str, tid: str) -> list[dict]:
    from .outcomes import owner_feedback

    if not gh.me():
        raise gh.GhError("GitHub could not identify the account that handles PR feedback")
    return [row for row in owner_feedback(url, "OPEN", repo, tid) if is_reply(row["body"])]


def answered_asks(run_id: str) -> list[dict]:
    return [a for run in sorted({run_id} | factory.chain_runs(run_id)) for a in asks.asks_of(run) if a["answers"]]


def reply_key(row: dict) -> tuple[str, int]:
    return row["kind"], row["gh_id"]


def reply_keys(rows: list[dict] | None) -> list[list] | None:
    return None if rows is None else sorted(list(reply_key(row)) for row in rows)


def reply_snapshot(held: dict, pr: str) -> set[tuple[str, int]] | None:
    legacy = held.get("seen_replies") if pr == held["pr"] else None
    seen = held.get("seen_replies_by_pr", {}).get(pr, legacy)
    return None if seen is None else {tuple(key) for key in seen}


def held_refs(held: dict) -> dict:
    return held.get("attempted_refs") or held["refs"]


def notice(repo: str, ticket: str, failure: dict) -> bool:
    key = orchestration.digest([repo, ticket, *(failure[k] for k in ("pr", "branch", "accepted", "local")),
                                held_refs(failure), failure["decision"]])
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
                          "Resolve the named files in a separate checkout and preserve both histories. If an authorized "
                          "run lands the resolution on this PR, retry; otherwise finish the delivery manually. "
                          "Do not reset the retained worktree") from error


def unowned(wt: Path, head: str, bases: Collection[str], runs: set[str], *, first_parent: bool = False) -> list[str]:
    args = [head, *(f"^{base}" for base in bases if base)]
    if first_parent:
        return [sha for sha, ids in repairs.run_commits(wt, args) if sha and not runs & ids]
    log = util.git(["log", f"--format={repairs.TRAILER}", *args], wt)
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
    if not saved.names and not remember:
        return saved
    try:
        found = found if found is not None else loose_files(c.wt)
        indexed = indexed_paths(c.wt) if saved.names else set()
    except (RuntimeError, OSError) as error:
        raise path_hold(c, "could not check retained file classifications", saved.names, inputs=inputs) from error
    exposed = set(overlapping_paths(saved.ignored, indexed.union(forced)))
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
    retained = {}
    for c in checkouts:
        paths = (forced or {}).get(c.github, ())
        if paths and (record := retained_record(c.wt)) and record.exists():
            found = loose_files(c.wt)
            private = set(overlapping_paths(found.ignored, paths))
            if private:
                check_retained(c, files=Retained(set(), private), found=found, remember=True)
        retained[c.github] = check_retained(c, forced=paths)
    return retained


def indexed_paths(wt: Path) -> set[str]:
    return set(util.git_records(["ls-files", "--cached"], wt))


def apply_pathspec(wt: Path, args: list[str], excluded: Collection[str], *paths: str) -> None:
    spec = [*(paths or (".",)), *(f":(top,exclude,literal){name}" for name in sorted(excluded))]
    util.git([*args, "--pathspec-from-file=-", "--pathspec-file-nul"], wt, input="\0".join(spec) + "\0")


def add_retained_safe(c: workspace.Checkout, retained: Retained) -> None:
    if not retained.names:
        diffguard.add(c.wt)
        return
    args = ["ls-files", "--others", "--exclude-standard", "--", ".", *diffguard.NEVER_STAGED]
    before = set(util.git_records(args, c.wt))
    if retained.ignored:
        apply_pathspec(c.wt, ["add", "-u"], retained.ignored, ".", *diffguard.NEVER_STAGED)
        if names := before.difference(overlapping_paths(before, retained.ignored, ignore_case=False)):
            literal = [f":(top,literal){name}" for name in sorted(names)]
            apply_pathspec(c.wt, ["add", "-A"], (), *literal, *diffguard.NEVER_STAGED)
    else:
        diffguard.add(c.wt)
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


def check_ignore_changes(inputs: Inputs, wt: Path, files: Retained, changed: list[str]) -> None:
    retained = inputs.checkout.wt
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
        raise path_hold(inputs.checkout, "could not verify the candidate's ignore rules for retained files", names,
                        "Save the named files separately or inspect the ignore rules, then retry", inputs) from error
    flipped = {n for n in names if excludes_changed or under_rules(n, rules) or (n in ignored) != (n in files.ignored)}
    if flipped:
        inputs.recheck(during="during isolated reconciliation")
        why = "; ".join(f"incoming ignore rules can {reason}: {', '.join(map(repr, sorted(paths)))}" for reason, paths in
                        (("hide retained untracked files", flipped & files.visible),
                         ("expose retained ignored files", flipped & files.ignored)) if paths)
        raise inputs.hold(why, "Save the named files separately or have the owner adjust the PR's ignore rules, then retry")


@dataclass(frozen=True)
class Plan:
    inputs: Inputs
    candidate: str
    fingerprint: str
    retained: Retained
    files: dict[str, str]

    def check_files(self, *, installed: bool = False) -> None:
        inputs = replace(self.inputs, local=self.candidate) if installed else self.inputs
        found = loose_files(inputs.checkout.wt)
        check_retained(inputs.checkout, inputs, files=self.retained, found=found)
        current = retained_snapshot(inputs, found)
        if changed := {name for name in current.keys() | self.files.keys() if current.get(name) != self.files.get(name)}:
            raise path_hold(inputs.checkout, "the retained files changed during recovery", changed,
                            "Inspect the named paths before resuming", inputs)

    def check_local(self, ws: Path, src: Path, copies: list[str]) -> None:
        inputs, c = self.inputs, self.inputs.checkout
        self.check_files()
        if (current_ref(c) != f"refs/heads/{inputs.branch}"
                or current_head(c) != inputs.local or factory.fingerprint(c.wt) != self.fingerprint
                or workspace.kept_checkout(ws, c, src, copies, commits=False)):
            raise inputs.hold("the retained worktree changed during recovery", "Save or restore the retained edits and retry",
                              local=current_head(c))
        dirty = set(workspace.uncommitted(c.wt)) | workspace.ignored_file_paths(c.wt)
        changed = util.git_records(["diff", "--no-renames", "--name-only", inputs.local, self.candidate], c.wt)
        ignore_case = util.git(["config", "--bool", "core.ignoreCase"], c.wt, check=False).strip() == "true"
        if paths := overlapping_paths(dirty, changed, ignore_case=ignore_case):
            raise path_hold(c, "incoming changes overlap retained uncommitted or ignored files", paths,
                            "Save those files separately, restore the recorded worktree, and retry", inputs)

    def install(self) -> None:
        inputs, c = self.inputs, self.inputs.checkout
        if self.candidate != inputs.local:
            try:
                util.git([*NO_HOOKS, *MERGE, "--ff-only", self.candidate], c.wt)
            except RuntimeError as error:
                why = util.first_line(vault.hidden(str(error)), 300)
                raise inputs.hold(f"the retained worktree refused candidate {self.candidate} ({why})",
                                  "Inspect the local branch and index, then retry without resetting either version",
                                  local=current_head(c)) from error
        head, on = current_head(c), current_ref(c)
        if head != self.candidate or on != f"refs/heads/{inputs.branch}":
            raise inputs.hold(f"the local checkout moved to {on or 'a detached HEAD'} while installing {self.candidate}",
                              "Inspect the retained branch before retrying", local=head)
        c.recovery_head, c.recovery_base = inputs.refs.head, inputs.refs.base
        c.note_pushed_head(inputs.refs.head)
        c.base_commit, c.resumes = inputs.refs.base, True
        c.onto, c.merge, c.resolved, c.resolved_lines = "", "", [], 0


def plan(c: workspace.Checkout, branch: str, accepted: str, local: str, remote: str, runs: set[str], run_id: str,
         recorded: str, deny: list[str]) -> Plan:
    refs = observe(c, branch)
    inputs = Inputs(c, branch, accepted, local, refs)
    if not refs.target_matches(c, branch):
        raise inputs.hold(f"expected the same open, non-fork PR from {branch} to {c.base}",
                          "Restore the saved PR target or reconcile the PR manually before resuming")
    if refs.origin != remote or not refs.aligned():
        if refs.aligned():
            why = f"origin/{branch} moved from {remote or 'missing'} during the initial PR lookup"
            action = "Retry against the newly observed head"
        elif refs.origin:
            why = (f"GitHub and origin did not agree with the observed refs (initial origin head {remote or 'missing'}, "
                   f"origin base {refs.origin_base or 'missing'})")
            action = "Wait for the refs to agree, then retry"
        else:
            why, action = f"origin/{branch} is missing", "Restore the published branch before resuming"
        error = inputs.hold(why, action)
        error.recovery["attempted_refs"] = asdict(replace(refs, origin=remote))
        raise error
    if not OID.fullmatch(accepted):
        raise inputs.hold("no previously accepted PR head was recorded", "Reconcile the PR and retained work manually")
    try:
        fetch_inputs(inputs)
    except RuntimeError:
        inputs.recheck(during="while fetching the observed commits")
        raise
    if not ancestor(c.wt, accepted, refs.head):
        raise inputs.hold(f"origin/{branch} no longer contains {accepted}",
                          "Restore additive history containing the accepted head or reconcile the rewrite manually")
    if foreign := unowned(c.wt, refs.head, (accepted, c.revision), runs, first_parent=True):
        raise inputs.hold(f"origin/{branch} includes commits this run's chain did not make: {', '.join(foreign)}",
                          "Continue from the run that owns those commits, or reconcile them and the retained work "
                          "manually in a separate checkout; this continuation cannot adopt them")
    if recorded != local:
        raise inputs.hold(f"the retained commit does not match the workspace record ({recorded or 'missing'})",
                          "Save any local work and restore the recorded retained head before resuming")
    if not (ancestor(c.wt, accepted, local) or ancestor(c.wt, local, accepted)):
        raise inputs.hold("the retained history does not contain the accepted history", "Reconcile the local history manually")
    if foreign := unowned(c.wt, local, (refs.head, refs.base), runs):
        raise inputs.hold(f"the retained history includes unowned commits: {', '.join(foreign)}",
                          "Set those commits aside or have their owner reconcile them explicitly on the PR")
    retained = loose_files(c.wt)
    check_retained(c, inputs, found=retained)
    files = retained_snapshot(inputs, retained)
    if not (fingerprint := factory.fingerprint(c.wt)):
        raise inputs.hold("the retained index and files could not be read", "Inspect the retained worktree and retry")
    with TemporaryDirectory(prefix="arete-pr-recovery-") as folder:
        wt = Path(folder) / "candidate"
        with util.repo_lock(c.src):
            util.git([*NO_HOOKS, "worktree", "add", "--detach", str(wt), local], c.src)
        try:
            merge(inputs, wt, refs.head, "PR head", run_id)
            merge(inputs, wt, refs.base, "PR base", run_id)
            candidate = util.git(["rev-parse", "HEAD"], wt).strip()
            if not all(ancestor(wt, oid, candidate) for oid in (refs.head, refs.base, local)):
                raise inputs.hold("the candidate does not preserve all inputs", "Reconcile the histories manually")
            diff = diffguard.between(wt, refs.base, candidate, deny=deny, prefix=c.prefix)
            if diff.denied:
                raise inputs.hold(f"the candidate differs from the PR base in protected files: {', '.join(map(repr, diff.denied))}",
                                  "Have the owner reconcile the protected paths on the PR before resuming")
            changed = util.git_records(["diff", "--no-renames", "--name-only", local, candidate], wt)
            check_ignore_changes(inputs, wt, retained, changed)
        finally:
            with util.repo_lock(c.src):
                util.git([*NO_HOOKS, "worktree", "remove", "--force", str(wt)], c.src)
    return Plan(inputs, candidate, fingerprint, retained, files)


def pending_receipt(data: dict) -> dict:
    return (data.get("publication_intent") or {}).get("recovery_receipt") or {}


def check_no_pr_refs(c: workspace.Checkout, branch: str, expected: str, base: str = "", *,
                     during: str = "while settling publication") -> None:
    refs = {branch: expected}
    if base:
        refs[c.base] = base
    for ref, wanted in refs.items():
        if (observed := workspace.remote_head(c.src, ref)) != wanted:
            raise factory.Refused(vault.hidden(f"{c.github}: origin/{ref} moved {during} "
                                               f"(expected {wanted or 'absent'}, observed {observed or 'absent'}); "
                                               "reconcile the sibling branch before resuming"))
    if url := gh.open_pr_url(c.github, branch):
        raise factory.Refused(vault.hidden(f"{c.github}: found {url} on the sibling branch without a PR in the receipt; "
                                           "reconcile that PR before resuming"))


def unconfirmed_pushes(intent: dict) -> dict[str, str]:
    attempts = intent.get("attempts", {}) | dict([intent["attempt"]] if intent.get("attempt") else [])
    pushed = intent.get("pushed", {})
    return {repo: head for repo, head in attempts.items() if pushed.get(repo) != head}


def manual_publication_hold(intent: dict, *, verify_pushes: bool = False) -> str:
    saved = intent["recovery_receipt"]
    targets = saved["targets"]
    effects = intent.get("pr_effects", ())
    pushed = intent.get("pushed", {})
    detail = [f"{e['kind']} on {e['pr'] or e['github']} at {e['head']} "
              f"({'confirmed' if e.get('confirmed') else 'unconfirmed'})"
              for e in effects if e.get("confirmed") is not True or e["kind"] != GATE_EVIDENCE]
    detail.extend(f"{repo}:{saved['branch']}@{head} (pushed)" for repo, head in pushed.items() if repo not in targets)
    if phase := intent.get("phase"):
        detail.append(f"{phase} for gated tree {intent['tree']}")
    if attempt := intent.get("attempt"):
        repo, head = attempt
        if (other := intent.get("attempts", {}).get(repo, head)) != head:
            detail.append(f"{repo}:{saved['branch']} has two recorded candidates ({other}, {head})")
    pending = unconfirmed_pushes(intent)
    can_verify = bool(pending) and not detail and all(
        repo in targets and repo not in pushed and OID.fullmatch(head) for repo, head in pending.items())
    if verify_pushes and can_verify:
        return ""
    detail.extend(f"{repo}:{saved['branch']}@{head} (unconfirmed push)" for repo, head in pending.items())
    action = (PUSH_RESUME if can_verify else "Preserve the retained candidate and reconcile the named branches or PRs "
                                          "manually in a separate checkout; Arete will not replay these effects")
    return vault.hidden(f"publication was interrupted; reconcile before resuming: {'; '.join(detail)}. {action}") if detail else ""


def settle(checkouts: list[workspace.Checkout], branch: str, made: dict, intent: dict, owners: set[str]) -> list[list[str]]:
    if why := manual_publication_hold(intent, verify_pushes=True):
        raise factory.Refused(why)
    saved = intent["recovery_receipt"]
    targets, trees, leases = (saved[k] for k in ("targets", "trees", "leases"))
    repos = {c.github for c in checkouts}
    if (not branch or not targets or len(repos) != len(checkouts) or branch != saved["branch"]
            or repos != trees.keys() or repos != leases.keys() or not targets.keys() <= repos
            or orchestration.digest(trees) != intent["tree"]):
        raise factory.Refused("the interrupted publication lacks a matching receipt for its checkouts; inspect it manually")
    pending = unconfirmed_pushes(intent)
    acknowledged = intent.get("pushed", {})
    pushed = acknowledged | pending
    heads = leases | pushed
    reports, verified, passive = [], [], []
    for e in intent.get("pr_effects", ()):
        refs = targets.get(e["github"], {}).get("refs", {})
        if e["pr"] != refs.get("url") or e["head"] != heads.get(e["github"]):
            raise factory.Refused("the gate report does not match the publication receipt; inspect it manually")
        reports.append([e["pr"], e["head"], refs["base"]])
    for c in checkouts:
        head, tree, accepted = current_head(c), trees[c.github], heads[c.github]
        check_retained(c)
        target = targets.get(c.github)
        refusal = factory.Refused(vault.hidden(
            f"{c.pr or c.github}: the retained checkout at {head} does not match the gated publication receipt "
            f"(expected {branch}@{accepted or 'absent'} with tree {tree}); "
            "save any new edits separately and restore the recorded candidate"))
        if (workspace.head_recorded(made, c) != head or current_ref(c) != f"refs/heads/{branch}"
                or factory.fingerprint(c.wt) != tree):
            raise refusal
        if not target or c.github in pushed:
            if head != (accepted or c.base_commit or c.revision) or util.git(["rev-parse", "HEAD^{tree}"], c.wt).strip() != tree:
                raise refusal
        if not target:
            if c.pr:
                raise refusal
            passive.append((c, accepted))
            continue
        refs = Refs(**target["refs"])
        inputs = Inputs(c, branch, target["accepted"], head, refs)
        if (leases[c.github] != refs.head or not refs.target_matches(c, branch) or not refs.aligned()
                or not all(OID.fullmatch(oid) and ancestor(c.wt, oid, head) for oid in (inputs.accepted, accepted, refs.base))
                or not ancestor(c.wt, refs.head, accepted)):
            raise refusal
        if foreign := unowned(c.wt, head, (refs.head, refs.base), owners):
            raise factory.Refused(vault.hidden(f"{c.pr}: the retained candidate {head} includes unowned commits: "
                                               f"{', '.join(foreign)}. Preserve both versions and reconcile manually"))
        seen = observe(c, branch)
        current = replace(inputs, refs=seen)
        if c.github in pending:
            exact = replace(refs, head=accepted, origin=accepted)
            if accepted == refs.head or seen != exact:
                raise factory.Refused(vault.hidden(
                    f"{c.github}:{branch}@{accepted} (unconfirmed push) cannot be verified. Expected {c.pr} OPEN on "
                    f"{branch} targeting {refs.base_branch}@{refs.base}; observed {seen.url or 'unknown PR'} "
                    f"{seen.state or 'unknown'} on {seen.branch or 'unknown'} targeting {seen.base_branch}@{seen.base}, "
                    f"PR head {seen.head or 'missing'}, origin head {seen.origin or 'missing'}, "
                    f"origin base {seen.origin_base or 'missing'}. {PUSH_RESUME}"))
        else:
            if not (seen.target_matches(c, branch) and seen.aligned()):
                raise inputs.moved(seen, during="while settling the publication", pushed=acknowledged)
            fetch_inputs(current)
            if not ancestor(c.wt, accepted, seen.head):
                raise inputs.moved(seen, during="while settling the publication", pushed=acknowledged)
        c.note_pushed_head(accepted)
        c.recovery_head, c.recovery_base = accepted, refs.base
        verified.append(current)
    for c, head in passive:
        check_no_pr_refs(c, branch, head)
    for inputs in verified:
        inputs.recheck(during="while settling the publication", pushed=pushed)
    return reports


class Publication:
    def __init__(self, checkouts: list[workspace.Checkout], branch: str, runs: set[str], *,
                 record: Callable[..., None], reported_gates: Collection[list[str]] = ()):
        self.checkouts, self.branch, self.runs = checkouts, branch, runs
        self.trees = {c.github: util.git(["write-tree"], c.wt).strip() for c in checkouts}
        self.targets = {c.github: expected_inputs(c, branch, "") for c in checkouts if c.pr}
        self.leases = {c.github: c.lease for c in checkouts}
        self.record, self.reported_gates = record, reported_gates
        self.pushed: dict[str, str] = {}
        self.pr_effects: list[dict] = []

    @property
    def effected(self) -> bool:
        return bool(self.pushed or self.pr_effects)

    def receipt(self, action: str) -> dict:
        targets = {repo: {"accepted": inputs.accepted, "refs": asdict(inputs.refs)} for repo, inputs in self.targets.items()}
        return {"branch": self.branch, "leases": self.leases, "trees": self.trees, "targets": targets, "action": action}

    def bound(self, c: workspace.Checkout) -> bool:
        return c.github in self.targets

    def checked_base(self, c: workspace.Checkout) -> str:
        return self.targets[c.github].refs.base

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

    def pr_write(self, c: workspace.Checkout, kind: str, during: str, after: str, **details):
        head = self.expected_head(c)
        creating = kind == "PR creation"
        if kind == "draft state" or creating:
            self.recheck(during=during)
        else:
            self.recheck_pr(c, during)
        if creating:
            check_no_pr_refs(c, self.branch, head, c.base_commit or c.revision, during=during)
        return self.track_pr_write(c, {"kind": kind, "github": c.github, "pr": c.pr, "head": head, **details}, after)

    @contextmanager
    def track_pr_write(self, c: workspace.Checkout, effect: dict, after: str):
        kind, creating = effect["kind"], effect["kind"] == "PR creation"
        self.pr_effects.append(effect)
        self.record(pr_effects=list(self.pr_effects))
        try:
            yield
        except RuntimeError as error:
            if isinstance(error, gh.GhUnsure) or kind == "screenshot upload":
                why = f"GitHub did not confirm {kind.lower()}"
                if creating:
                    raise self.failure(f"{why} for {c.github}:{self.branch}@{effect['head']}") from error
                self.recheck_pr(c, after)
                raise self.ref_hold(c, after, self.inputs(c, "").refs, why=why) from error
            if isinstance(error, gh.GhError) and not creating:
                self.pr_effects.remove(effect)
                self.record(pr_effects=list(self.pr_effects))
            raise
        if creating:
            self.targets[c.github] = expected_inputs(c, self.branch, "")
        effect.update(pr=c.pr, confirmed=True)
        self.record(pr_effects=list(self.pr_effects))

    def comment(self, c: workspace.Checkout, body: str, *, kind: str, during: str) -> None:
        head = self.expected_head(c)
        if kind == GATE_EVIDENCE and [c.pr, head, self.checked_base(c)] in self.reported_gates:
            self.recheck_pr(c, during)
            return
        with self.pr_write(c, kind, during, f"after posting {kind.lower()}"):
            gh.pr_comment(c.pr, f"{kind} for head `{head}`\n\n{body}", single_attempt=True)

    def push(self, c: workspace.Checkout) -> None:
        head, expected = current_head(c), self.expected_head(c)
        if (util.git(["rev-parse", f"{head}^{{tree}}"], c.wt).strip() != self.trees[c.github]
                or (expected and not ancestor(c.wt, expected, head))):
            raise self.failure(f"{c.github}: {head} is not the gated candidate tree on top of {expected}")
        bound = self.bound(c)
        base = self.checked_base(c) if bound else (c.base_commit or c.revision)
        if not OID.fullmatch(base) or not ancestor(c.wt, base, head):
            raise self.failure(f"{c.github}: {head} no longer contains the recorded base {base}")
        if foreign := unowned(c.wt, head, (expected, base), self.runs):
            why = f"the candidate includes unowned commits: {', '.join(foreign)}"
            if self.pushed or not bound:
                raise self.failure(f"{c.github}: {why}")
            raise self.inputs(c, head).hold(why, "Restore the recorded run-owned history before retrying")
        ref = f"refs/heads/{self.branch}"
        args = ["push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", f"--force-with-lease={ref}:{expected}",
                "origin", f"{head}:{ref}"]
        self.recheck(during="immediately before the leased push")
        if head == expected:
            c.onto = ""
            return
        if not bound:
            check_no_pr_refs(c, self.branch, expected, base, during="immediately before the leased push")
        self.record(attempt=(c.github, head))
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
                    self.record(attempt=None)
                    if refs != inputs.refs:
                        raise inputs.moved(refs, during="during the leased push", pushed=self.pushed)
                    raise inputs.hold("Git rejected the leased push", "Inspect the remote branch protection and retry")
                raise inputs.publication_unconfirmed("the leased push did not confirm publication", refs, self.pushed)
            try:
                observed = workspace.remote_head(c.src, self.branch) or "missing"
            except (RuntimeError, OSError, ValueError):
                observed = "unconfirmed"
            if rejected and observed == (expected or "missing"):
                self.record(attempt=None)
            raise self.failure(f"the leased push to {c.github} failed (expected head {expected or 'missing'}, "
                               f"attempted head {head}, observed {observed}); inspect the PR and branch before retrying",
                               util.git_error(vault.hidden(res.stderr or res.stdout)))
        self.pushed[c.github] = head
        c.note_pushed_head(head)
        c.onto = ""
        self.record(pushed=dict(self.pushed), attempt=None)
