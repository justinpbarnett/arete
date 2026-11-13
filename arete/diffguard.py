from __future__ import annotations

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
NEVER_STAGED = [":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/.local/state/gh/**", ":(exclude,glob)**/.config/gh/**"]


def denied_paths(layer: config.RepoLayer) -> list[str]:
    return ALWAYS_DENY + layer.deny


def is_lockfile(name: str) -> bool:
    return Path(name).name in LOCKFILES


def add(wt: Path) -> None:
    util.git(["add", "-A", "--", ".", *NEVER_STAGED], wt)


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
    return between(wt, "--cached", against, deny=deny, prefix=prefix)


def between(wt: Path, *revs: str, deny: Sequence[str] = (), prefix: str = "") -> Diff:
    diff = Diff()
    for row in util.git_records(["diff", "--numstat", "--no-renames", *revs], wt):
        added, removed, name = row.split("\t", 2)
        path = prefix + name
        lock = is_lockfile(name)
        diff.counts[path] = 0 if lock or added == "-" else int(added) + int(removed)
        if lock:
            diff.locks.append(path)
        if util.matches_any(name, deny) or util.matches_any(path, deny):
            diff.denied.append(path)
    return diff
