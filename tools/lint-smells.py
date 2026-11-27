"""Fast zero-dependency static checks for JavaScript smells and Arete Python conventions."""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", "node_modules", ".venv", ".git"}
FORBIDDEN_DB_IMPORTS = frozenset({"db", "tx", "reader", "one_read"})
CONFLICT_MARKER = re.compile(r"^(?:<{7} |>{7} )")

REGEX_PRECEDERS = frozenset("=([{,;?:!&|~+-*%^<>")
REGEX_KEYWORDS = frozenset({
    "return", "case", "throw", "in", "of", "typeof", "void",
    "delete", "yield", "await", "else", "do", "new",
})

JS_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("JS001", re.compile(r"\bdebugger\b"), "Remove leftover `debugger` statement"),
    ("JS002", re.compile(r"(?<!\.)\beval\s*\("), "Avoid `eval()`"),
    ("JS003", re.compile(r"\bnew\s+Function\s*\("), "Avoid `new Function()`"),
    ("JS004", re.compile(r"(?:^|[;\s({])var\s+[A-Za-z_$]"), "Use `const` or `let` instead of `var`"),
    ("JS005", re.compile(r"(?<![=!<>])([=!]=)(?!=)(?!\s*null\b)"),
     "Use strict equality (`===`/`!==`); loose equality is only allowed for `== null`/`!= null`"),
    ("JS006", re.compile(r"\bdocument\.write\s*\("), "Avoid `document.write()`"),
    ("JS007", re.compile(r"(?<!\.)\bwith\s*\("), "Avoid `with (...)` statements"),
)


def _prev_token_allows_regex(out: list[str]) -> bool:
    """Return True if a `/` at the current position begins a regex literal rather than division."""
    text = "".join(out).rstrip()
    if not text:
        return True
    last = text[-1]
    if last in REGEX_PRECEDERS:
        return True
    m = re.search(r"([A-Za-z_$][A-Za-z0-9_$]*)$", text)
    return bool(m and m.group(1) in REGEX_KEYWORDS)


def _strip_js_segment(src: str, start: int, *, stop_at_brace: bool = False) -> tuple[str, int]:
    """Replace comments, string/regex literals, and template text with spaces while preserving line numbers."""
    out: list[str] = []
    i = start
    n = len(src)
    brace_depth = 0
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if stop_at_brace:
            if c == "{":
                brace_depth += 1
            elif c == "}":
                if brace_depth == 0:
                    return "".join(out), i
                brace_depth -= 1

        if c == "/" and nxt == "/":
            out.append("  ")
            i += 2
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
        elif c == "/" and nxt == "*":
            out.append("  ")
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
        elif c in ("'", '"'):
            quote = c
            out.append(quote)
            i += 1
            while i < n and src[i] != quote:
                if src[i] == "\\" and i + 1 < n:
                    out.append("\n" if src[i + 1] == "\n" else "  ")
                    i += 2
                else:
                    out.append("\n" if src[i] == "\n" else " ")
                    i += 1
            if i < n:
                out.append(quote)
                i += 1
        elif c == "`":
            out.append("`")
            i += 1
            while i < n and src[i] != "`":
                if src[i] == "\\" and i + 1 < n:
                    out.append("\n" if src[i + 1] == "\n" else "  ")
                    i += 2
                elif src[i] == "$" and i + 1 < n and src[i + 1] == "{":
                    out.append("${")
                    expr, i = _strip_js_segment(src, i + 2, stop_at_brace=True)
                    out.append(expr)
                    if i < n and src[i] == "}":
                        out.append("}")
                        i += 1
                else:
                    out.append("\n" if src[i] == "\n" else " ")
                    i += 1
            if i < n:
                out.append("`")
                i += 1
        elif c == "/" and _prev_token_allows_regex(out):
            out.append("/")
            i += 1
            in_class = False
            while i < n and (in_class or src[i] != "/") and src[i] != "\n":
                if src[i] == "\\" and i + 1 < n and src[i + 1] != "\n":
                    out.append("  ")
                    i += 2
                    continue
                if src[i] == "[":
                    in_class = True
                elif src[i] == "]":
                    in_class = False
                out.append(" ")
                i += 1
            if i < n and src[i] == "/":
                out.append("/")
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out), i


def strip_js(src: str) -> str:
    stripped, _ = _strip_js_segment(src, 0)
    return stripped


def check_js_files() -> list[str]:
    errors: list[str] = []
    web_js = sorted(p for p in (ROOT / "arete" / "web").glob("*.js") if not p.name.startswith("vendor"))
    test_mjs = sorted((ROOT / "tests").glob("*.mjs"))

    for path in (*web_js, *test_mjs):
        rel = path.relative_to(ROOT)
        raw = path.read_text(encoding="utf-8")
        stripped = strip_js(raw)
        for lineno, line in enumerate(stripped.splitlines(), 1):
            for code, pattern, message in JS_RULES:
                for match in pattern.finditer(line):
                    if code == "JS005" and re.search(r"\bnull\s*$", line[:match.start()]):
                        continue
                    errors.append(f"{rel}:{lineno}: {code} {message}")

    node = shutil.which("node")
    if node:
        for path in test_mjs:
            proc = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                rel = path.relative_to(ROOT)
                detail = (proc.stderr or proc.stdout).strip().splitlines()[0] if (proc.stderr or proc.stdout).strip() else "syntax error"
                errors.append(f"{rel}:1: JS000 node --check failed: {detail}")
    return errors


def _iter_source_files(roots: tuple[str, ...], suffixes: frozenset[str]) -> list[Path]:
    found: list[Path] = []
    for name in roots:
        base = ROOT / name
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.is_file() and path.suffix in suffixes and not path.name.startswith("vendor"):
                found.append(path)
    return found


def check_python_and_markers() -> list[str]:
    errors: list[str] = []
    for path in _iter_source_files(("arete", "adws", "tests", "tools"), frozenset({".py", ".js", ".mjs", ".css", ".html"})):
        rel = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if CONFLICT_MARKER.match(line):
                errors.append(f"{rel}:{lineno}: ARETE001 Unresolved merge conflict marker")

    for path in _iter_source_files(("arete",), frozenset({".py"})):
        rel = path.relative_to(ROOT)
        if len(rel.parts) >= 2 and rel.parts[1] == "store":
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "") in ("store.db", "arete.store.db"):
                bad = [alias.name for alias in node.names if alias.name in FORBIDDEN_DB_IMPORTS]
                if bad:
                    names = ", ".join(bad)
                    errors.append(
                        f"{rel}:{node.lineno}: ARETE002 Import `db` module (`from .store import db`) "
                        f"instead of binding `{names}` directly from `store.db`",
                    )
    return errors


def main() -> int:
    errors = [*check_js_files(), *check_python_and_markers()]
    for err in errors:
        print(err)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
