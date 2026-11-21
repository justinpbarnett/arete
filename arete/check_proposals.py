from __future__ import annotations

import ast
import hashlib
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath
from textwrap import indent

from . import config, util

DIRECTORY = "check-proposals"
PYTHON_SYNTAX_WARNINGS = "python-compile-syntax-warnings"
MAX_EXAMPLE_CHARS = 8192
IDENTITY_DIAGNOSTIC = re.compile(r""""is(?: not)?" with (?:a|'[a-z]+') literal\. (Did you mean "([!=]=)"\?)""")
COMPARISON_OPERATORS = {"==": (ast.Is, ast.Eq), "!=": (ast.IsNot, ast.NotEq)}
COMPILE_ARGS = ("-I", "-S", "-B", "-Werror::SyntaxWarning", "-c",
                "import sys; compile(sys.stdin.buffer.read(), sys.argv[1], 'exec', dont_inherit=True)")
ADOPTION_CHANGE = "Integrate the verified compiler command into the repository's check gate for Python source files."
GITHUB_REPO = re.compile(r"[a-z0-9-]+/[a-z0-9_.-]+", re.IGNORECASE | re.ASCII)
GITHUB_PR = re.compile(rf"https://github\.com/({GITHUB_REPO.pattern})/pull/([1-9][0-9]*)/?", GITHUB_REPO.flags)
type ReviewComment = tuple[str, str, str, int]

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


def review_comment(url: str, kind: str, comment_id: int) -> ReviewComment | None:
    if match := GITHUB_PR.fullmatch(url):
        return match[1].lower(), match[2], kind, comment_id
    return None


def target_github(repo: config.Repo, root: Path, target: Path) -> str:
    resolved = target.resolve()
    if resolved.is_dir():
        return ""
    owners = []
    for part in repo.parts or [config.Part("", repo.github)]:
        if part.dir and not repo_path(part.dir):
            return ""
        checkout = root / part.dir
        actual_checkout = checkout.resolve()
        contains_target = checkout in target.parents
        if contains_target != (actual_checkout in resolved.parents):
            return ""
        if contains_target:
            if (not actual_checkout.is_relative_to(root) or not actual_checkout.is_dir()
                    or not GITHUB_REPO.fullmatch(part.github)):
                return ""
            owners.append(part.github.lower())
    return owners[0] if len(owners) == 1 else ""


def validate(p: dict, repo: config.Repo, recent: dict[str, ReviewComment],
             earlier: dict[str, ReviewComment]) -> tuple[str, str]:
    rule = p["enforcement"]
    if not isinstance(rule, dict) or rule.get("kind") not in ("architecture", "check"):
        return "", "choose architecture or check for enforcement"
    if not all(has_text(v) for v in (rule.get("behavior"), p.get("why"), p.get("change"))):
        return "", "name the prevented behavior, why it is deterministic and what should change"
    cleanup = rule.get("cleanup")
    if not isinstance(cleanup, list) or not all(map(repo_path, [p.get("file"), *cleanup])):
        return "", "name the target and any cleanup examples with repository-relative paths"
    root = repo.path.resolve()
    github = target_github(repo, root, root / p["file"])
    if not github:
        return "", "the target file must belong to one configured GitHub repository"
    if any(not path.is_file() or target_github(repo, root, path) != github for path in
           (root / example for example in cleanup)):
        return "", "cleanup examples must identify existing files in the target repository"
    if rule["kind"] == "check":
        if not all(has_text(rule.get(k)) for k in ("known_bad", "allowed", "diagnostic")):
            return "", "give a failing example, a passing example and an actionable diagnostic"
        if any(len(rule[k]) > MAX_EXAMPLE_CHARS for k in ("known_bad", "allowed")):
            return "", "the proposed check requires small, self-contained examples"
        if rule["known_bad"].strip() == rule["allowed"].strip():
            return "", "the failing and passing examples must differ"
    elif any(k in rule for k in ("evaluator", "known_bad", "allowed", "diagnostic")):
        return "", "executable examples require an executable check proposal"
    evidence = p.get("evidence")
    if not isinstance(evidence, list) or not all(map(has_text, evidence)):
        return "", "cite at least two distinct owner review comments"
    comments = {recent.get(e, earlier.get(e)) for e in evidence}
    if (not any(e in recent for e in evidence)
            or any(comment is None or comment[0] != github for comment in comments)):
        return "", "cite feedback from this repository with at least one comment from the current window"
    if len(comments) < 2:
        return "", "cite at least two distinct owner review comments"
    return github, ""


def compile_example(source: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *COMPILE_ARGS, "<proposal example>"],
        input=source, encoding="utf-8", capture_output=True, timeout=5, cwd="/",
    )


def comparable_examples(known_bad: str, allowed: str, correction: str) -> bool:
    identity, equality = COMPARISON_OPERATORS[correction]
    bad_tree = ast.parse(known_bad.encode())
    allowed_tree = ast.parse(allowed.encode())
    changed = False
    for bad_node, allowed_node in zip(ast.walk(bad_tree), ast.walk(allowed_tree)):
        if not isinstance(bad_node, ast.Compare) or not isinstance(allowed_node, ast.Compare):
            continue
        for index, (before, after) in enumerate(zip(bad_node.ops, allowed_node.ops)):
            if isinstance(before, identity) and isinstance(after, equality):
                if changed:
                    return False
                bad_node.ops[index] = after
                changed = True
    return changed and ast.dump(bad_tree) == ast.dump(allowed_tree)


def verify(p: dict) -> tuple[str, str]:
    rule = p["enforcement"]
    if rule["kind"] == "architecture":
        return "", ""
    if rule.get("evaluator") != PYTHON_SYNTAX_WARNINGS:
        return "the proposed check has no supported evaluator", ""
    if p["change"].strip() != ADOPTION_CHANGE:
        return "the proposed implementation must adopt the verified compiler command", ""
    try:
        bad = compile_example(rule["known_bad"])
        allowed = compile_example(rule["allowed"])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return "the isolated compiler check could not complete", ""
    if bad.returncode == 0:
        return "the proposed check accepted the known-bad example", ""
    diagnostic = bad.stderr.rstrip().rpartition("\n")[2].removeprefix("SyntaxError: ")
    match = IDENTITY_DIAGNOSTIC.fullmatch(diagnostic)
    if bad.returncode != 1 or bad.stdout or not match:
        return "the known-bad example did not produce a supported corrective diagnostic", ""
    if rule["diagnostic"].strip() not in (diagnostic, match[1]):
        return "the observed diagnostic did not match the proposal", ""
    if allowed.returncode != 0 or allowed.stdout or allowed.stderr:
        return "the proposed check did not accept the allowed example", ""
    try:
        comparable = comparable_examples(rule["known_bad"], rule["allowed"], match[2])
    except (SyntaxError, ValueError, RecursionError):
        comparable = False
    if not comparable:
        return "the allowed example must differ only by the comparison operator in the corrective diagnostic", ""
    return "", diagnostic


def family_name(p: dict, github: str | None) -> Path:
    behavior = " ".join(p["enforcement"]["behavior"].casefold().split())
    scope = (p["repo"], github) if github is not None else (p["repo"],)
    key = hashlib.sha256("\0".join((*scope, behavior)).encode()).hexdigest()[:20]
    return Path(DIRECTORY) / f"{key}.md"


def revision_name(family: Path, report: bytes) -> Path:
    revision = hashlib.sha256(report).hexdigest()[:20]
    return family.with_stem(f"{family.stem}-{revision}")


def revision_glob(family: Path) -> str:
    return str(family.with_stem(f"{family.stem}-*"))


def evidence_matches_github(evidence: str, github: str) -> bool:
    try:
        citations = json.loads(evidence)
    except (TypeError, ValueError):
        return False
    if not isinstance(citations, list) or not citations:
        return False
    for citation in citations:
        fields = citation.split() if isinstance(citation, str) else []
        if len(fields) != 4 or fields[0] != "feedback" or not fields[3].isascii() or not fields[3].isdecimal():
            return False
        match = GITHUB_PR.fullmatch(fields[1])
        if not match or match[1].lower() != github:
            return False
    return True


def review_text(p: dict, diagnostic: str) -> str:
    rule = p["enforcement"]
    layer = "API or architecture" if rule["kind"] == "architecture" else "Executable check"
    sections = ["# Proposed repository enforcement", "Review and implement through the repository's usual change process.",
                "## Prevented behavior", rule["behavior"], "## Why it is deterministic", p["why"],
                f"## Recommended layer\n\n{layer}"]
    if rule["kind"] == "check":
        command = shlex.join(["python", *COMPILE_ARGS, "path/to/source.py"])
        sections += [f"Integration target `{p['file']}`\n\n{ADOPTION_CHANGE}",
                     f"```sh\n{command} < path/to/source.py\n```",
                     "Use the repository's Python interpreter and substitute each source path in both positions. "
                     "The command compiles source without running it and rejects syntax warnings and syntax errors."]
    else:
        sections += [f"Target file `{p['file']}`\n\n{p['change']}"]
    sections += ["## Existing examples to clean up", "\n".join(f"- `{f}`" for f in sorted(set(rule["cleanup"])))
                 or "No examples identified."]
    if rule["kind"] == "check":
        sections += ["## Observed verification",
                     "The command above rejected a literal identity comparison and accepted the corrected comparison.",
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
    if not target.is_file():
        try:
            util.write_durably(target, report, 0o600, replace=False)
        except FileExistsError:
            if target.is_symlink() or not target.is_file():
                raise
    return target
