from __future__ import annotations

import os
import re
import stat
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import paths, sandbox

NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
REFERENCE = re.compile(r"(?<![\w-])journey:([a-z][a-z0-9-]*)(?![\w-])")
MAX_MAP_BYTES = 32 * 1024


@dataclass(frozen=True)
class Journey:
    steps: tuple[str, ...]
    outcome: str
    driver: str = ""


def linked_map(value: str, source_dir: Path, read_file: Callable[[str], bytes | None] | None = None) -> dict:
    path = Path(value)
    target = source_dir.resolve() / path
    if (path.is_absolute() or path.suffix != ".toml" or ".." in path.parts or ".git" in path.parts
            or sandbox.secret_path(target)):
        raise ValueError("[verify] feature_map must name a local TOML file inside the layer directory")
    try:
        if read_file:
            data = read_file(path.as_posix())
            if data is None:
                raise ValueError("expected a regular tracked file")
        else:
            with paths.directory_fd(target.parent) as parent:
                fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("expected a regular file with one link")
                data = source.read(MAX_MAP_BYTES + 1)
        if len(data) > MAX_MAP_BYTES:
            raise ValueError(f"exceeds {MAX_MAP_BYTES} bytes")
        return tomllib.loads(data.decode())
    except (OSError, ValueError) as error:
        raise ValueError(f"[verify] cannot read feature_map {value!r}: {error}") from error


def load(value, source_dir: Path, bindings: dict[str, str], *,
         read_file: Callable[[str], bytes | None] | None = None) -> dict[str, Journey]:
    entries = linked_map(value, source_dir, read_file) if isinstance(value, str) else value
    if not isinstance(entries, dict):
        raise ValueError("[verify] feature_map must be a table or a relative TOML filename")
    journeys = {}
    for name, entry in entries.items():
        where = f"[verify.feature_map.{name}]"
        if not NAME.fullmatch(name):
            raise ValueError(f"{where} use a lowercase, hyphen-separated journey name")
        if not isinstance(entry, dict) or entry.keys() - {"steps", "outcome"}:
            raise ValueError(f"{where} expected steps and outcome; bind drivers in [verify.journey_drivers]")
        steps, outcome = entry.get("steps"), entry.get("outcome")
        if not isinstance(steps, list) or not steps or any(not isinstance(s, str) or not s.strip() for s in steps):
            raise ValueError(f"{where} steps must be a nonempty list of nonempty strings")
        if not isinstance(outcome, str) or not outcome.strip():
            raise ValueError(f"{where} outcome must be a nonempty string")
        journeys[name] = Journey(tuple(step.strip() for step in steps), outcome.strip(), bindings.get(name, ""))
    if unknown := bindings.keys() - journeys.keys():
        raise ValueError(f"[verify.journey_drivers] unknown journeys: {', '.join(sorted(unknown))}")
    return journeys


def requested(entries: dict[str, Journey], title: str, body: str) -> list[str]:
    return sorted(set(REFERENCE.findall(title + "\n" + body))) if entries else []


def guidance(entries: dict[str, Journey], *, planning: bool = False) -> str:
    if not entries:
        return ""
    if planning:
        instruction = ("Use relevant entries by their stable names in your verification steps. An exact reference "
                       "in the issue title or body schedules any attached configured driver.")
    else:
        instruction = ("Use the relevant route and the outcome the issue requires. Name the journey in `what` and "
                       "record the actual steps in `how` and observed results in `saw`. Use captured driver "
                       "results from this attempt and exercise any remaining behavior through the approved tools.")
    lines = ["## Product feature map", "", instruction]
    for name, journey in entries.items():
        driver = " (configured driver available)" if journey.driver else ""
        lines.extend(("", f"- `journey:{name}`{driver}"))
        lines.extend(f"  {i}. {' '.join(step.split())}" for i, step in enumerate(journey.steps, 1))
        lines.append(f"  - Expected outcome: {' '.join(journey.outcome.split())}")
    return "\n".join(lines) + "\n\n"
