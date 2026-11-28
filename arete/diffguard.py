from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from . import config, util

CLAUDE_CODE_GUARDED = [".claude/**", "**/.claude/**"]
ALWAYS_DENY = [".agents/config.toml", ".github/workflows/**", ".env", ".env.*", "**/.env", "**/.env.*",
               *CLAUDE_CODE_GUARDED]
LOCKFILES = {"pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb", "uv.lock", "poetry.lock",
             "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock"}
MOVED_MIN_ALNUM = 20
MOVED_CANDIDATES_PER_KEY = 16
MOVED_MAX_UNDER_THRESHOLD = 32
NEVER_STAGED = [":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/.local/state/gh/**", ":(exclude,glob)**/.config/gh/**"]


def denied_paths(layer: config.RepoLayer) -> list[str]:
    return ALWAYS_DENY + layer.deny


def staged_pathspec(deny: Sequence[str] = ()) -> list[str]:
    return [*NEVER_STAGED, *(f":(exclude,glob){p}" for p in deny)]


def denied_pathspec(deny: Sequence[str] = ()) -> list[str]:
    return [f":(glob){p}" for p in deny]


def is_lockfile(name: str) -> bool:
    return Path(name).name in LOCKFILES


def add(wt: Path) -> None:
    util.git(["add", "-A", "--", ".", *staged_pathspec(())], wt)


@dataclass
class Diff:
    counts: dict[str, int] = field(default_factory=dict)
    locks: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)

    @property
    def files(self) -> list[str]:
        return list(self.counts)

    @property
    def lines(self) -> int:
        return sum(self.counts.values())

    def __iadd__(self, other: Diff) -> Self:
        self.counts |= other.counts
        self.locks += other.locks
        self.denied += other.denied
        return self

    def refusal(self, limit: int, since: str = "", earlier: int = 0) -> str:
        if self.denied:
            return f"touched denied paths: {', '.join(self.denied[:5])}"
        total = self.lines + earlier
        if total <= limit:
            return ""
        size = f"{total} lines changed since {since}" if since else f"the diff is {total} lines"
        return f"{size}, over the {limit} line limit"


def stage(wt: Path, deny: Sequence[str] = (), against: str = "HEAD", prefix: str = "") -> Diff:
    add(wt)
    return between(wt, "--cached", against, deny=deny, prefix=prefix, moves=True)


DIFF_FLAGS = frozenset({"--cached", "--staged"})


def between(wt: Path, *revs: str, deny: Sequence[str] = (), prefix: str = "", moves: bool = False) -> Diff:
    if "--" in revs:
        sep = revs.index("--")
        rev_args, path_args = revs[:sep], revs[sep + 1:]
    else:
        rev_args, path_args = revs, ()
    for rev in rev_args:
        if rev not in DIFF_FLAGS:
            util.validate_git_ref(rev, "git revision")
    args = [*rev_args, "--", *path_args]
    diff = Diff()
    for row in util.git_records(["diff", "--numstat", "--no-renames", *args], wt):
        added, removed, name = row.split("\t", 2)
        path = prefix + name
        lock = is_lockfile(name)
        diff.counts[path] = 0 if lock or added == "-" else int(added) + int(removed)
        if lock:
            diff.locks.append(path)
        if util.matches_any(name, deny) or util.matches_any(path, deny):
            diff.denied.append(path)
    if moves and any(diff.counts.values()):
        options = ["--no-renames", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", "-U0"]
        patch = util.git(["diff", *options, *args], wt)
        for name, moved in moved_by_file(patch).items():
            path = prefix + name
            if path in diff.counts and path not in diff.locks:
                diff.counts[path] = max(0, diff.counts[path] - moved)
    return diff


def line_key(line: str) -> str:
    return line[1:].strip()


def change_runs(lines: list[str]) -> list[tuple[str, list[int]]]:
    runs: list[tuple[str, list[int]]] = []
    in_hunk = False
    sign = ""
    for i, line in enumerate(lines):
        if line.startswith("diff --git "):
            in_hunk, sign = False, ""
        elif line.startswith("@@"):
            in_hunk, sign = True, ""
        elif line.startswith("\\"):
            continue
        elif in_hunk and line[:1] in ("+", "-"):
            if line[0] == sign:
                runs[-1][1].append(i)
            else:
                sign = line[0]
                runs.append((sign, [i]))
        else:
            sign = ""
    return runs


def longest_match(keys: list[str], weights: list[int], added: list[int], start: int, removed: list[int], at: int,
                  used: set[int]) -> int:
    size = 0
    alnum = 0
    while (start + size < len(added) and at + size < len(removed) and removed[at + size] not in used
           and keys[added[start + size]] == keys[removed[at + size]]):
        alnum += weights[added[start + size]]
        size += 1
        if alnum < MOVED_MIN_ALNUM and size >= MOVED_MAX_UNDER_THRESHOLD:
            return 0
    while size and not keys[added[start + size - 1]]:
        size -= 1
    return size


def alnum_weight(key: str) -> int:
    return sum(ch.isalnum() for ch in key)


def moved_lines(patch: str) -> set[int]:
    lines = patch.split("\n")
    keys = [line_key(line) for line in lines]
    weights = [alnum_weight(key) for key in keys]
    runs = change_runs(lines)
    removed_runs = [indices for sign, indices in runs if sign == "-"]
    by_key: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for run_number, indices in enumerate(removed_runs):
        for at, i in enumerate(indices):
            by_key[keys[i]].append((run_number, at))
    heads: dict[str, int] = defaultdict(int)
    moved: set[int] = set()
    for sign, added in runs:
        if sign != "+":
            continue
        start = 0
        while start < len(added):
            key = keys[added[start]]
            candidates = by_key[key] if key else []
            head = heads[key]
            while head < len(candidates) and removed_runs[candidates[head][0]][candidates[head][1]] in moved:
                head += 1
            heads[key] = head
            best = (0, -1, -1)
            for run_number, at in candidates[head:head + MOVED_CANDIDATES_PER_KEY]:
                if removed_runs[run_number][at] in moved:
                    continue
                size = longest_match(keys, weights, added, start, removed_runs[run_number], at, moved)
                best = max(best, (size, run_number, at))
            size, run_number, at = best
            if size and sum(weights[i] for i in added[start:start + size]) >= MOVED_MIN_ALNUM:
                moved.update(added[start:start + size])
                moved.update(removed_runs[run_number][at:at + size])
                start += size
            else:
                start += 1
    return moved


def header_path(line: str) -> str:
    rest = line.removeprefix("diff --git ")
    if rest.startswith('"') or not rest.startswith("a/") or len(rest) < 5 or (len(rest) - 5) % 2:
        return ""
    width = (len(rest) - 5) // 2
    if rest[2 + width:2 + width + 3] != " b/":
        return ""
    return rest[2:2 + width]


def moved_by_file(patch: str) -> dict[str, int]:
    lines = patch.split("\n")
    moved = moved_lines(patch)
    counts: dict[str, int] = {}
    path = ""
    for i, line in enumerate(lines):
        if line.startswith("diff --git "):
            path = header_path(line)
        elif i in moved and path:
            counts[path] = counts.get(path, 0) + 1
    return counts


def fold_moved(patch: str) -> str:
    lines = patch.split("\n")
    moved = moved_lines(patch)
    out: list[str] = []
    folded = 0
    for i, line in enumerate(lines):
        if i in moved and line.startswith("-"):
            folded += 1
            continue
        if folded:
            out.append(f"[{folded} deleted lines moved verbatim, shown where they were added]")
            folded = 0
        out.append(line)
    if folded:
        out.append(f"[{folded} deleted lines moved verbatim, shown where they were added]")
    return "\n".join(out)
