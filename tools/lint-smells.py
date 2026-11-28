"""Fast zero-dependency static checks for JavaScript smells and Arete Python security/architecture guardrails."""
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
SUBPROCESS_FUNCS = frozenset({"run", "Popen", "call", "check_call", "check_output"})
ENVIRON_OWNERS = frozenset({"arete/environment.py", "arete/scrub.py"})
# Only store/db.py owns the main DB; cohorts.py owns the separate owner-state cohorts.sqlite;
# serve_guard.py runs before the arete package loads during startup/rollback checks.
SQLITE_CONNECT_OWNERS = frozenset({"arete/store/db.py", "arete/cohorts.py", "arete/serve_guard.py"})
UNSAFE_DESER_CALLS = {
    "pickle": frozenset({"load", "loads"}),
    "marshal": frozenset({"load", "loads"}),
}
SAFE_YAML_LOADERS = frozenset({"SafeLoader", "CSafeLoader", "BaseLoader", "CBaseLoader"})
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
    ("JS008", re.compile(r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|dangerouslySetInnerHTML)\b"),
     "Avoid raw DOM HTML sinks (`innerHTML`/`outerHTML`/`insertAdjacentHTML`/`dangerouslySetInnerHTML`); "
     "use `textContent` or DOM/Preact builders"),
)
JS_JAVASCRIPT_URL = re.compile(r"\bjavascript\s*:", re.IGNORECASE)


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


def _scan_js_segment(src: str, start: int, strings_out: list[tuple[int, str]], line_no: int, *,
                     stop_at_brace: bool = False) -> tuple[str, int, int]:
    """Replace comments, string/regex literals, and template text with spaces while preserving line numbers.

    Also records (lineno, text) for string and template literal chunks so `javascript:` URLs can be checked.
    """
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
                    return "".join(out), i, line_no
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
                if src[i] == "\n":
                    out.append("\n")
                    line_no += 1
                else:
                    out.append(" ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
        elif c in ("'", '"'):
            quote = c
            out.append(quote)
            i += 1
            chunk_start_line = line_no
            chunk: list[str] = []
            while i < n and src[i] != quote:
                if src[i] == "\\" and i + 1 < n:
                    if src[i + 1] == "\n":
                        out.append("\n")
                        line_no += 1
                    else:
                        out.append("  ")
                        chunk.append(src[i + 1])
                    i += 2
                else:
                    if src[i] == "\n":
                        out.append("\n")
                        line_no += 1
                    else:
                        out.append(" ")
                        chunk.append(src[i])
                    i += 1
            if chunk:
                strings_out.append((chunk_start_line, "".join(chunk)))
            if i < n:
                out.append(quote)
                i += 1
        elif c == "`":
            out.append("`")
            i += 1
            chunk_start_line = line_no
            chunk = []
            while i < n and src[i] != "`":
                if src[i] == "\\" and i + 1 < n:
                    if src[i + 1] == "\n":
                        out.append("\n")
                        line_no += 1
                    else:
                        out.append("  ")
                        chunk.append(src[i + 1])
                    i += 2
                elif src[i] == "$" and i + 1 < n and src[i + 1] == "{":
                    if chunk:
                        strings_out.append((chunk_start_line, "".join(chunk)))
                        chunk = []
                    out.append("${")
                    expr, i, line_no = _scan_js_segment(src, i + 2, strings_out, line_no, stop_at_brace=True)
                    out.append(expr)
                    if i < n and src[i] == "}":
                        out.append("}")
                        i += 1
                    chunk_start_line = line_no
                else:
                    if src[i] == "\n":
                        if chunk:
                            strings_out.append((chunk_start_line, "".join(chunk)))
                            chunk = []
                        out.append("\n")
                        line_no += 1
                        chunk_start_line = line_no
                    else:
                        out.append(" ")
                        chunk.append(src[i])
                    i += 1
            if chunk:
                strings_out.append((chunk_start_line, "".join(chunk)))
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
            if c == "\n":
                line_no += 1
            out.append(c)
            i += 1
    return "".join(out), i, line_no


def strip_js(src: str) -> str:
    stripped, _, _ = _scan_js_segment(src, 0, [], 1)
    return stripped


def check_js_source(rel: str, raw: str) -> list[str]:
    errors: list[str] = []
    strings: list[tuple[int, str]] = []
    stripped, _, _ = _scan_js_segment(raw, 0, strings, 1)
    for lineno, line in enumerate(stripped.splitlines(), 1):
        for code, pattern, message in JS_RULES:
            for match in pattern.finditer(line):
                if code == "JS005" and re.search(r"\bnull\s*$", line[:match.start()]):
                    continue
                errors.append(f"{rel}:{lineno}: {code} {message}")
    for lineno, text in strings:
        if JS_JAVASCRIPT_URL.search(text):
            errors.append(f"{rel}:{lineno}: JS008 Avoid `javascript:` URLs")
    return errors


def check_js_files() -> list[str]:
    errors: list[str] = []
    web_js = sorted(p for p in (ROOT / "arete" / "web").glob("*.js") if not p.name.startswith("vendor"))
    test_mjs = sorted((ROOT / "tests").glob("*.mjs"))

    for path in (*web_js, *test_mjs):
        rel = path.relative_to(ROOT).as_posix()
        errors.extend(check_js_source(rel, path.read_text(encoding="utf-8")))

    node = shutil.which("node")
    if node:
        for path in test_mjs:
            proc = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                rel = path.relative_to(ROOT).as_posix()
                out = (proc.stderr or proc.stdout).strip()
                detail = out.splitlines()[0] if out else "syntax error"
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


def _is_os_environ(node: ast.AST, environ_aliases: set[str]) -> bool:
    if isinstance(node, ast.Name) and node.id in environ_aliases:
        return True
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _safe_yaml_loader(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id in SAFE_YAML_LOADERS
    if isinstance(node, ast.Attribute):
        return node.attr in SAFE_YAML_LOADERS
    return False


def check_python_source(rel: str, text: str) -> list[str]:
    errors: list[str] = []
    tree = ast.parse(text, filename=rel)
    parts = Path(rel).parts
    in_store = len(parts) >= 2 and parts[1] == "store"

    # Track direct imports so `from subprocess import run` / `from os import environ` / etc. are caught too.
    subproc_aliases: dict[str, str] = {}
    environ_aliases: set[str] = set()
    sqlite_connect_aliases: set[str] = set()
    unsafe_deser_aliases: dict[str, str] = {}
    yaml_load_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if not in_store and mod in ("store.db", "arete.store.db"):
                bad = [alias.name for alias in node.names if alias.name in FORBIDDEN_DB_IMPORTS]
                if bad:
                    names = ", ".join(bad)
                    errors.append(
                        f"{rel}:{node.lineno}: ARETE002 Import `db` module (`from .store import db`) "
                        f"instead of binding `{names}` directly from `store.db`",
                    )
            if mod == "subprocess":
                for alias in node.names:
                    if alias.name in SUBPROCESS_FUNCS:
                        subproc_aliases[alias.asname or alias.name] = alias.name
            elif mod == "os":
                for alias in node.names:
                    if alias.name == "environ":
                        environ_aliases.add(alias.asname or alias.name)
            elif mod == "sqlite3":
                for alias in node.names:
                    if alias.name == "connect":
                        sqlite_connect_aliases.add(alias.asname or alias.name)
            elif mod in UNSAFE_DESER_CALLS:
                for alias in node.names:
                    if alias.name in UNSAFE_DESER_CALLS[mod]:
                        unsafe_deser_aliases[alias.asname or alias.name] = f"{mod}.{alias.name}"
            elif mod == "yaml":
                for alias in node.names:
                    if alias.name == "load":
                        yaml_load_aliases.add(alias.asname or alias.name)

    # Walk with enclosing function name so scratch.spawn's validated **kwargs wrapper is recognized.
    def visit(node: ast.AST, func_name: str = "") -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_name = node.name

        if isinstance(node, ast.Call):
            fn = node.func
            # ARETE003: subprocess.* must not use shell=True and must pass explicit env=
            subproc_name: str | None = None
            if (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                    and fn.value.id == "subprocess" and fn.attr in SUBPROCESS_FUNCS):
                subproc_name = f"subprocess.{fn.attr}"
            elif isinstance(fn, ast.Name) and fn.id in subproc_aliases:
                subproc_name = f"subprocess.{subproc_aliases[fn.id]}"
            if subproc_name:
                kws = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
                has_starstar = any(kw.arg is None for kw in node.keywords)
                shell_val = kws.get("shell")
                if isinstance(shell_val, ast.Constant) and shell_val.value is True:
                    errors.append(f"{rel}:{node.lineno}: ARETE003 `{subproc_name}(..., shell=True)` is forbidden")
                if "env" not in kws and not (has_starstar and rel == "arete/scratch.py" and func_name == "spawn"):
                    errors.append(
                        f"{rel}:{node.lineno}: ARETE003 `{subproc_name}` must pass an explicit scoped `env=`",
                    )

            # ARETE004: os.environ.copy() / os.environ.items() / dict(os.environ) outside environment.py & scrub.py
            if rel not in ENVIRON_OWNERS:
                if (isinstance(fn, ast.Attribute) and fn.attr in ("copy", "items")
                        and _is_os_environ(fn.value, environ_aliases)):
                    errors.append(
                        f"{rel}:{node.lineno}: ARETE004 `os.environ.{fn.attr}()` is forbidden outside "
                        "`arete/environment.py` and `arete/scrub.py`; build a scoped env instead",
                    )
                elif (isinstance(fn, ast.Name) and fn.id == "dict"
                      and node.args and _is_os_environ(node.args[0], environ_aliases)):
                    errors.append(
                        f"{rel}:{node.lineno}: ARETE004 `dict(os.environ)` is forbidden outside "
                        "`arete/environment.py` and `arete/scrub.py`; build a scoped env instead",
                    )

            # ARETE005: sqlite3.connect outside store/db.py (and documented standalone owners)
            if rel not in SQLITE_CONNECT_OWNERS and (
                (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                 and fn.value.id == "sqlite3" and fn.attr == "connect")
                or (isinstance(fn, ast.Name) and fn.id in sqlite_connect_aliases)
            ):
                errors.append(
                    f"{rel}:{node.lineno}: ARETE005 Direct `sqlite3.connect()` is forbidden outside "
                    "`arete/store/db.py`; use `store.db.tx()` or `store.db.reader()`",
                )

            # ARETE006: pickle/marshal deserialization, eval(), exec(), unsafe yaml.load()
            if isinstance(fn, ast.Name) and fn.id in ("eval", "exec"):
                errors.append(f"{rel}:{node.lineno}: ARETE006 `{fn.id}()` is forbidden in `arete/`")
            elif isinstance(fn, ast.Name) and fn.id in unsafe_deser_aliases:
                errors.append(
                    f"{rel}:{node.lineno}: ARETE006 `{unsafe_deser_aliases[fn.id]}()` is forbidden in `arete/`",
                )
            elif (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                  and fn.value.id in UNSAFE_DESER_CALLS and fn.attr in UNSAFE_DESER_CALLS[fn.value.id]):
                errors.append(
                    f"{rel}:{node.lineno}: ARETE006 `{fn.value.id}.{fn.attr}()` is forbidden in `arete/`",
                )
            elif (
                (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                 and fn.value.id == "yaml" and fn.attr == "load")
                or (isinstance(fn, ast.Name) and fn.id in yaml_load_aliases)
            ):
                loader = next((kw.value for kw in node.keywords if kw.arg == "Loader"), None)
                if loader is None and len(node.args) >= 2:
                    loader = node.args[1]
                if not _safe_yaml_loader(loader):
                    errors.append(
                        f"{rel}:{node.lineno}: ARETE006 `yaml.load()` without `SafeLoader` is forbidden; "
                        "use `yaml.safe_load()`",
                    )

        # ARETE004: {**os.environ, ...}
        if isinstance(node, ast.Dict) and rel not in ENVIRON_OWNERS:
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None and _is_os_environ(value, environ_aliases):
                    errors.append(
                        f"{rel}:{node.lineno}: ARETE004 Unpacking `**os.environ` is forbidden outside "
                        "`arete/environment.py` and `arete/scrub.py`; build a scoped env instead",
                    )

        for child in ast.iter_child_nodes(node):
            visit(child, func_name)

    visit(tree)
    return errors


def check_python_and_markers() -> list[str]:
    errors: list[str] = []
    for path in _iter_source_files(("arete", "adws", "tests", "tools"), frozenset({".py", ".js", ".mjs", ".css", ".html"})):
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if CONFLICT_MARKER.match(line):
                errors.append(f"{rel}:{lineno}: ARETE001 Unresolved merge conflict marker")

    for path in _iter_source_files(("arete",), frozenset({".py"})):
        rel = path.relative_to(ROOT).as_posix()
        errors.extend(check_python_source(rel, path.read_text(encoding="utf-8")))
    return errors


def main() -> int:
    errors = [*check_js_files(), *check_python_and_markers()]
    for err in errors:
        print(err)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
