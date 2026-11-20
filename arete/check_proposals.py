from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from textwrap import indent

from . import config, util

DIRECTORY = "check-proposals"
PYTHON_SYNTAX_WARNINGS = "python-compile-syntax-warnings"
MAX_EXAMPLE_CHARS = 8192
IDENTITY_DIAGNOSTIC = re.compile(r""""is(?: not)?" with (?:a|'[a-z]+') literal\. Did you mean "[!=]="\?""")
COMPILE_EXAMPLE = """\
import sys

try:
    compile(sys.stdin.buffer.read().decode("utf-8"), "<proposal example>", "exec", dont_inherit=True)
except SyntaxError as error:
    print(error.msg)
    sys.exit(1)
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["architecture", "check"]},
        "behavior": {"type": "string"},
        "cleanup": {"type": "array", "items": {"type": "string"}},
        "evaluator": {"type": "string", "enum": [PYTHON_SYNTAX_WARNINGS]},
        "known_bad": {"type": "string"},
        "allowed": {"type": "string"},
        "diagnostic": {"type": "string"},
    },
    "required": ["kind", "behavior", "cleanup"],
}


def has_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def repo_path(value) -> bool:
    if not has_text(value) or any(c in value for c in "\\\x00\n\r"):
        return False
    path = PurePosixPath(value)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts


def refusal(p: dict, repo: config.Repo, recent: dict[str, str], earlier: dict[str, str]) -> str:
    rule = p["enforcement"]
    if not isinstance(rule, dict) or rule.get("kind") not in ("architecture", "check"):
        return "choose architecture or check for enforcement"
    if not all(has_text(v) for v in (rule.get("behavior"), p.get("why"), p.get("change"))):
        return "name the prevented behavior, why it is deterministic and what should change"
    cleanup = rule.get("cleanup")
    if not isinstance(cleanup, list) or not all(map(repo_path, [p.get("file"), *cleanup])):
        return "name the target and any cleanup examples with repository-relative paths"
    root = repo.path.resolve()
    if not (root / p["file"]).resolve().is_relative_to(root):
        return "the target path must stay in the repository"
    if any(not path.is_relative_to(root) or not path.is_file() for path in
           ((root / example).resolve() for example in cleanup)):
        return "cleanup examples must identify existing files in the target repository"
    if rule["kind"] == "check":
        if not all(has_text(rule.get(k)) for k in ("known_bad", "allowed", "diagnostic")):
            return "give a failing example, a passing example and an actionable diagnostic"
        if any(len(rule[k]) > MAX_EXAMPLE_CHARS for k in ("known_bad", "allowed")):
            return "the proposed check requires small, self-contained examples"
        if rule["known_bad"].strip() == rule["allowed"].strip():
            return "the failing and passing examples must differ"
    elif any(k in rule for k in ("evaluator", "known_bad", "allowed", "diagnostic")):
        return "executable examples require an executable check proposal"
    evidence = p.get("evidence")
    if not isinstance(evidence, list) or not all(map(has_text, evidence)) or len(set(evidence)) < 2:
        return "cite at least two distinct owner review comments"
    if (not any(e in recent for e in evidence)
            or any(recent.get(e, earlier.get(e)) != repo.name for e in evidence)):
        return "cite feedback from this repository with at least one comment from the current window"
    return ""


def compile_example(source: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-Werror::SyntaxWarning", "-c", COMPILE_EXAMPLE],
        input=source, encoding="utf-8", capture_output=True, timeout=5, cwd="/",
    )


def verify(rule: dict) -> tuple[str, str]:
    if rule["kind"] == "architecture":
        return "", ""
    if rule.get("evaluator") != PYTHON_SYNTAX_WARNINGS:
        return "the proposed check has no supported evaluator", ""
    try:
        bad = compile_example(rule["known_bad"])
        allowed = compile_example(rule["allowed"])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return "the isolated compiler check could not complete", ""
    diagnostic = bad.stdout.strip()
    if bad.returncode == 0:
        return "the proposed check accepted the known-bad example", ""
    if bad.returncode != 1 or bad.stderr or not IDENTITY_DIAGNOSTIC.fullmatch(diagnostic):
        return "the known-bad example did not produce a supported corrective diagnostic", ""
    if rule["diagnostic"].strip() not in diagnostic:
        return "the observed diagnostic did not match the proposal", ""
    if allowed.returncode != 0 or allowed.stdout or allowed.stderr:
        return "the proposed check did not accept the allowed example", ""
    return "", diagnostic


def filename(p: dict) -> Path:
    behavior = " ".join(p["enforcement"]["behavior"].casefold().split())
    key = hashlib.sha256(f"{p['repo']}\0{behavior}".encode()).hexdigest()[:20]
    return Path(DIRECTORY) / f"{key}.md"


def revision_name(family: Path, report: bytes) -> Path:
    revision = hashlib.sha256(report).hexdigest()[:20]
    return family.with_stem(f"{family.stem}-{revision}")


def review_text(p: dict, diagnostic: str) -> str:
    rule = p["enforcement"]
    layer = "API or architecture" if rule["kind"] == "architecture" else "Executable check"
    sections = ["# Proposed repository enforcement", "Review and implement through the repository's usual change process.",
                "## Prevented behavior", rule["behavior"], "## Why it is deterministic", p["why"],
                f"## Recommended layer\n\n{layer}", f"Target file `{p['file']}`\n\n{p['change']}",
                "## Existing examples to clean up", "\n".join(f"- `{f}`" for f in sorted(set(rule["cleanup"])))
                or "No examples identified."]
    if rule["kind"] == "check":
        sections += ["## Observed verification",
                     "An isolated Python compiler treated syntax warnings as errors.",
                     "### Known-bad example rejected", indent(rule["known_bad"].strip("\n"), "    "),
                     "### Observed diagnostic", indent(diagnostic, "    "),
                     "### Allowed example accepted", indent(rule["allowed"].strip("\n"), "    ")]
    sections += ["## Owner review evidence", "\n".join(f"- {e}" for e in sorted(set(p["evidence"])))]
    return "\n\n".join(sections) + "\n"


def save(repo: config.Repo, relative: Path, report: bytes) -> Path:
    layer = repo.layer_dir.resolve()
    if repo.layer is None and layer != repo.path.resolve() / ".agents":
        raise OSError("the default repo layer must stay at .agents in the target repository")
    folder = layer / DIRECTORY
    target = layer / relative
    if folder.is_symlink() or target.is_symlink():
        raise OSError("the proposal destination must not be a symlink")
    if not target.is_file() or target.read_bytes() != report:
        util.write_bytes_atomic(target, report)
    return target
