from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path

from . import owner_asks, paths, prompt_loader, refusals

NAME_RE = re.compile(r"[a-zA-Z0-9_-]+")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$")
BASE_SHA_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
RENAMED_INPUTS: dict[str, str] = {"workspace": "worktree"}

CALL_ROLES: dict[str, tuple[str, ...]] = {
    "plan": ("plan",),
    "revise": ("build",),
    "repair": ("build", "review_repair"),
    "build": ("build",),
    "fix": ("check_repair",),
    "review_fix": ("review_repair",),
    "resolve": ("build",),
    "ci_fix": ("ci_repair",),
    "review": ("review",),
    "verify_fix": ("check_repair",),
    "verify": ("verify",),
    "grade": ("verify", "eval_grade"),
    "learn": ("learn",),
    "uat": ("uat",),
    "intake_extract": ("intake_extract",),
}

BRIEF_INPUTS: frozenset[str] = frozenset((
    "repo", "worktree", "ref", "title", "body", "owner", "comments", "attachments",
    "triage", "expertise", "commands", "max_lines", "journeys", "denied", "class_check",
    "test_classes", "class_check_guidance",
))

OFFERS: dict[str, frozenset[str]] = {
    "plan": BRIEF_INPUTS | {"checks"},
    "revise": BRIEF_INPUTS | {"feedback", "checks"},
    "repair": BRIEF_INPUTS | {"plan", "source", "findings", "checks"},
    "build": BRIEF_INPUTS | {"plan", "checks", "estimate", "verify"},
    "fix": frozenset(("attempt", "max_attempts", "failures", "checks", "denied", "owner")),
    "review_fix": frozenset(("problems", "notes", "denied", "owner")),
    "resolve": frozenset((
        "repo", "github", "base", "base_commit", "head", "ref", "title", "body",
        "conflicts", "own", "branch_log", "base_log", "commands", "attempt", "tries",
        "feedback", "owner", "denied",
    )),
    "ci_fix": frozenset((
        "round", "max_rounds", "failures", "diff", "checks", "merged_base",
        "left_for_a_person", "denied", "owner",
    )),
    "review": BRIEF_INPUTS | {"plan", "checks", "evidence", "compare", "diff", "earlier"},
    "verify_fix": frozenset(("attempt", "max_attempts", "failures", "checks", "denied", "owner")),
    "verify": BRIEF_INPUTS | {
        "surface", "notes", "scratch", "checks", "env_example", "compare", "goal", "summary", "diff",
    },
    "grade": BRIEF_INPUTS | {"surface", "notes", "scratch", "checks", "env_example", "compare", "goal"},
    "learn": frozenset(("repo", "ref", "title", "story", "expertise", "max_lines", "owner")),
    "uat": frozenset(("ref", "title", "ticket", "prs", "branch", "owner")),
    "intake_extract": frozenset((
        "source", "kind", "title", "author", "origin_url", "raw",
        "rule_guidance", "candidate_kinds", "candidate_actions", "defaults",
    )),
}


class ConfigInvalid(refusals.Invalid, ValueError):
    code = "config_invalid"


class LoadedSchema(dict):
    schema_name: str
    schema_version: str
    schema_level: str
    schema_sha256: str

    def __init__(self, data: dict, *, name: str = "", version: str = "", level: str = "engine",
                 sha256: str = "") -> None:
        super().__init__(data)
        self.schema_name = name
        self.schema_version = version
        self.schema_level = level
        self.schema_sha256 = sha256


@dataclass(frozen=True)
class LayerAppend:
    name: str
    version: str
    level: str
    sha256: str
    text: str
    source: str = ""

    def as_row(self) -> dict[str, str]:
        return {
            "name": self.name,
            "version": self.version,
            "level": self.level,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ResolvedPrompt:
    call: str
    role: str
    name: str
    version: str
    level: str
    sha256: str
    text: str
    source: str = ""
    appends: tuple[LayerAppend, ...] = ()


def _parse_scalar(raw: str) -> object:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1]
    if value == "true":
        return True
    if value == "false":
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _parse_inline_list(raw: str) -> list[object]:
    inner = raw[1:-1].strip()
    if not inner:
        return []
    items: list[object] = []
    cur: list[str] = []
    quote = ""
    escape = False
    for ch in inner:
        if escape:
            cur.append(ch)
            escape = False
            continue
        if quote:
            cur.append(ch)
            if ch == "\\" and quote == '"':
                escape = True
            elif ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == ",":
            token = "".join(cur).strip()
            if token:
                items.append(_parse_scalar(token))
            cur = []
        else:
            cur.append(ch)
    if quote:
        raise ValueError("unterminated quoted string in prompt front matter list")
    tail = "".join(cur).strip()
    if tail:
        items.append(_parse_scalar(tail))
    return items


def parse(data: str | bytes) -> tuple[dict, str]:
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    if not text.startswith(("---\n", "---\r\n")):
        return {}, text
    first_nl = text.find("\n")
    pos = first_nl + 1
    lines: list[str] = []
    closing_end = -1
    while pos <= len(text):
        next_nl = text.find("\n", pos)
        if next_nl == -1:
            line = text[pos:]
            end_pos = len(text)
        else:
            line = text[pos:next_nl]
            end_pos = next_nl + 1
        if line.rstrip("\r") == "---":
            closing_end = end_pos
            break
        lines.append(line.rstrip("\r"))
        if next_nl == -1:
            break
        pos = end_pos
    if closing_end < 0:
        raise ValueError("prompt front matter is not closed with ---")

    meta: dict[str, object] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        match = KEY_RE.match(line)
        if not match:
            raise ValueError(f"invalid prompt front matter line: {line!r}")
        key = match.group(1)
        rest = match.group(2).strip()
        if rest == "":
            items: list[object] = []
            j = i + 1
            while j < len(lines):
                sub = lines[j]
                sub_strip = sub.strip()
                if not sub_strip or sub_strip.startswith("#"):
                    j += 1
                    continue
                if sub_strip.startswith("- ") or sub_strip == "-":
                    items.append(_parse_scalar(sub_strip[1:].strip()))
                    j += 1
                    continue
                break
            meta[key] = items if items or key == "inputs" else ""
            i = j
            continue
        if rest.startswith("[") and rest.endswith("]"):
            meta[key] = _parse_inline_list(rest)
        else:
            meta[key] = _parse_scalar(rest)
        i += 1

    if "schema" in meta and (type(meta["schema"]) is not int or meta["schema"] != 1):
        raise ValueError(f"unsupported prompt front matter schema: {meta['schema']!r}")
    if "inputs" in meta:
        raw_inputs = meta["inputs"]
        if not isinstance(raw_inputs, list) or not all(isinstance(x, str) and IDENT_RE.fullmatch(x) for x in raw_inputs):
            raise ValueError(f"prompt front matter inputs must be a list of identifier names, not {raw_inputs!r}")

    rest_body = text[closing_end:]
    if rest_body.startswith("\r\n"):
        rest_body = rest_body[2:]
    elif rest_body.startswith("\n"):
        rest_body = rest_body[1:]
    return meta, rest_body


def body(path: Path | str) -> str:
    return parse(Path(path).read_bytes())[1]


def check_renamed_inputs(meta: dict, body_text: str) -> list[str]:
    found: set[str] = set()
    if isinstance(meta.get("inputs"), list):
        found.update(str(name) for name in meta["inputs"])
    with suppress(ValueError):
        found.update(prompt_loader.parse_template("prompt", body_text).get_identifiers())
    return sorted(name for name in found if name in RENAMED_INPUTS)


def validate_layer_appends(layer_dir: Path | None) -> list[Path]:
    if layer_dir is None or layer_dir == Path():
        return []
    prompts_dir = layer_dir / "prompts"
    if not prompts_dir.is_dir():
        return []
    validated: list[Path] = []
    for file in sorted(prompts_dir.glob("*.append.md")):
        if not file.is_file():
            continue
        raw = file.read_bytes()
        try:
            meta, _ = parse(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ConfigInvalid(f"config_invalid: {file}: {exc}") from exc
        if "overrides" in meta:
            raise ConfigInvalid(f"config_invalid: {file}: layer prompt fragment may not declare overrides")
        validated.append(file)
    return validated


def load_layer_appends(layer_dir: Path | None, repo_name: str, *,
                       source_base: str = "") -> dict[str, tuple[LayerAppend, ...]]:
    files = validate_layer_appends(layer_dir)
    if not files:
        return {}
    clean_repo = repo_name or (layer_dir.parent.name if layer_dir and layer_dir.name == ".agents" else
                               (layer_dir.name if layer_dir else "layer"))
    grouped: dict[str, list[LayerAppend]] = {}
    for file in files:
        prompt_name = file.name.removesuffix(".append.md")
        if not NAME_RE.fullmatch(prompt_name):
            continue
        raw = file.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        item_name = f"{clean_repo}.{prompt_name}.append"
        src = f"{source_base}/{file.name}" if source_base else str(file)
        grouped.setdefault(prompt_name, []).append(LayerAppend(
            name=item_name,
            version=f"prompt/{item_name}@{sha[:12]}",
            level=f"layer:{clean_repo}",
            sha256=sha,
            text=raw.decode("utf-8"),
            source=src,
        ))
    return {k: tuple(v) for k, v in grouped.items()}


def engine_prompt_names() -> frozenset[str]:
    if not paths.PROMPTS_DIR.is_dir():
        return frozenset()
    return frozenset(p.stem for p in paths.PROMPTS_DIR.glob("*.md") if p.is_file())


def engine_schema_names() -> frozenset[str]:
    if not paths.SCHEMAS_DIR.is_dir():
        return frozenset()
    return frozenset(p.stem for p in paths.SCHEMAS_DIR.glob("*.json") if p.is_file())


def engine_schema(name: str, schemas_dir: Path = paths.SCHEMAS_DIR) -> LoadedSchema:
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"invalid schema name {name!r}; use a schema file stem")
    path = schemas_dir / f"{name}.json"
    raw = path.read_bytes()
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"schema {name}: expected JSON object")
    sha = hashlib.sha256(raw).hexdigest()
    level = "engine" if schemas_dir.resolve() == paths.SCHEMAS_DIR.resolve() else "run"
    return LoadedSchema(parsed, name=name, version=f"schema/{name}@{sha[:12]}", level=level, sha256=sha)


def _bound_name(run, call: str, role: str, kind: str) -> str:
    if run is not None and hasattr(run, "settings") and role:
        try:
            settings = run.settings(role)
            table = getattr(settings, "prompts" if kind == "prompt" else "schemas", None)
            if isinstance(table, dict) and call in table:
                return str(table[call])
        except ValueError:
            pass
    return call


def _repo_name_of(run, pin=None) -> str:
    if run is not None:
        if isinstance(run, dict):
            val = run.get("repo")
            return val if isinstance(val, str) else ""
        repo = getattr(run, "repo", None)
        if repo is not None:
            name = getattr(repo, "name", "")
            if isinstance(name, str) and name:
                return name
    if pin is not None and pin.repo is not None:
        return pin.repo.name
    return ""


def _layer_dir_of(run, pin=None) -> Path | None:
    from . import config

    if run is not None and not isinstance(run, dict):
        layer = getattr(run, "layer", None)
        if isinstance(layer, config.RepoLayer) and layer.dir != Path():
            return layer.dir
    if pin is not None and pin.layer is not None and pin.layer.dir != Path():
        return pin.layer.dir
    return None


def _draft_file_bytes(draft_id: str, rel_path: str) -> bytes | None:
    if not draft_id:
        return None
    from .store import db

    try:
        with db.reader() as c:
            row = c.execute(
                "SELECT body FROM config_drafts WHERE id = ? AND path = ?",
                (draft_id, rel_path),
            ).fetchone()
    except Exception:
        return None
    if not row or row["body"] is None:
        return None
    val = row["body"]
    return val if isinstance(val, bytes) else str(val).encode("utf-8")


def _arm_file_bytes(arm: object, kind_dir: str, filename: str) -> tuple[bytes, str] | None:
    if not arm:
        return None
    if isinstance(arm, dict):
        folder_key = f"{kind_dir}_dir"
        if folder := arm.get(folder_key) or (arm.get("prompts") if kind_dir == "prompts" else None):
            candidate = Path(str(folder)) / filename
            if candidate.is_file():
                return candidate.read_bytes(), f"arm:{arm.get('name') or 'arm'}/{filename}"
        files_map = arm.get(kind_dir)
        if isinstance(files_map, dict) and filename in files_map:
            raw = files_map[filename]
            return (raw if isinstance(raw, bytes) else str(raw).encode("utf-8")), f"arm:{arm.get('name') or 'arm'}/{filename}"
    return None


def _str_attr(run, name: str) -> str:
    if run is None:
        return ""
    val = run.get(name) if isinstance(run, dict) else getattr(run, name, "")
    return val if isinstance(val, str) else ""


def _pin_of(run):
    from . import pins

    if run is not None and not isinstance(run, dict):
        candidate = getattr(run, "pin", None)
        if isinstance(candidate, pins.Resolution):
            return candidate
    return pins.current()


def resolve(run, call: str, role: str) -> ResolvedPrompt:
    from . import pins

    name = _bound_name(run, call, role, "prompt")
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"invalid prompt name {name!r}; use a template's file stem")

    pin = _pin_of(run)
    repo_name = _repo_name_of(run, pin)
    run_layer_dir = _layer_dir_of(run, pin)

    if pin is not None:
        if name not in pin.prompt_texts:
            raise KeyError(f"prompt {name!r} is missing from the active run pin")
        raw_text = pin.prompt_texts[name]
        meta = pin.prompts.get(name, {})
        version = str(meta.get("id") or "")
        level = str(meta.get("level") or "engine")
        sha = str(meta.get("sha256") or "")
        source = str(meta.get("from") or "")
        pin_layer_dir = pin.layer.dir if pin.layer is not None and pin.layer.dir != Path() else None
        if run_layer_dir is not None and (pin_layer_dir is None or run_layer_dir.resolve() != pin_layer_dir.resolve()):
            extra = load_layer_appends(run_layer_dir, repo_name)
            appends = extra.get(name, ())
        else:
            appends = pin.prompt_appends.get(name, ())
        return ResolvedPrompt(
            call=call, role=role, name=name, version=version, level=level,
            sha256=sha, text=raw_text, source=source, appends=appends,
        )

    # Unpinned resolution (e.g. direct/row lookup or standalone call outside pins.use):
    draft_id = _str_attr(run, "draft_id")
    raw_arm = run.get("arm") if isinstance(run, dict) else getattr(run, "arm", None)
    arm = raw_arm if isinstance(raw_arm, dict) else None
    config_commit = _str_attr(run, "config_commit")
    engine_sha = _str_attr(run, "engine_sha")

    raw: bytes | None = None
    level = "engine"
    source = ""

    if (draft_raw := _draft_file_bytes(draft_id, f"prompts/{name}.md")) is not None:
        raw, level, source = draft_raw, "run", f"draft:{draft_id}/prompts/{name}.md"
    elif (arm_found := _arm_file_bytes(arm, "prompts", f"{name}.md")) is not None:
        raw, source = arm_found
        level = "arm"
    else:
        config_dir = pins.config_checkout(config_commit) if config_commit else paths.CONFIG_DIR.resolve()
        candidate = config_dir / "prompts" / f"{name}.md"
        if name in engine_prompt_names() and candidate.is_file():
            raw = candidate.read_bytes()
            level = "config"
            source = f"prompts/{name}.md@{config_commit}" if config_commit else f"prompts/{name}.md"
        else:
            engine_file = paths.PROMPTS_DIR / f"{name}.md"
            raw = engine_file.read_bytes()
            level = "engine"
            source = f"arete/prompts/{name}.md@{engine_sha}" if engine_sha else f"arete/prompts/{name}.md"

    sha = hashlib.sha256(raw).hexdigest()
    version = f"prompt/{name}@{sha[:12]}"
    appends = load_layer_appends(run_layer_dir, repo_name).get(name, ())
    return ResolvedPrompt(
        call=call, role=role, name=name, version=version, level=level,
        sha256=sha, text=raw.decode("utf-8"), source=source, appends=appends,
    )


def resolve_schema(run, call: str, role: str) -> LoadedSchema:
    from . import pins

    name = _bound_name(run, call, role, "schema")
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"invalid schema name {name!r}; use a schema file stem")

    pin = _pin_of(run)

    if pin is not None and name in pin.schema_objects:
        meta = pin.schemas.get(name, {})
        return LoadedSchema(
            copy.deepcopy(pin.schema_objects[name]),
            name=name,
            version=str(meta.get("id") or ""),
            level=str(meta.get("level") or "engine"),
            sha256=str(meta.get("sha256") or ""),
        )

    draft_id = _str_attr(run, "draft_id")
    raw_arm = run.get("arm") if isinstance(run, dict) else getattr(run, "arm", None)
    arm = raw_arm if isinstance(raw_arm, dict) else None
    config_commit = _str_attr(run, "config_commit")

    raw: bytes | None = None
    level = "engine"
    if (draft_raw := _draft_file_bytes(draft_id, f"schemas/{name}.json")) is not None:
        raw, level = draft_raw, "run"
    elif (arm_found := _arm_file_bytes(arm, "schemas", f"{name}.json")) is not None:
        raw = arm_found[0]
        level = "arm"
    else:
        config_dir = pins.config_checkout(config_commit) if config_commit else paths.CONFIG_DIR.resolve()
        candidate = config_dir / "schemas" / f"{name}.json"
        if name in engine_schema_names() and candidate.is_file():
            raw, level = candidate.read_bytes(), "config"
        else:
            return engine_schema(name)

    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"schema {name}: expected JSON object")
    sha = hashlib.sha256(raw).hexdigest()
    return LoadedSchema(parsed, name=name, version=f"schema/{name}@{sha[:12]}", level=level, sha256=sha)


def _render_fragment(name: str, text: str, offered: dict[str, object]) -> tuple[str, dict[str, str]]:
    meta, body_text = parse(text)
    tmpl = prompt_loader.parse_template(name, body_text)
    body_names = set(tmpl.get_identifiers())
    if "inputs" in meta:
        declared = set(meta["inputs"])
        if undeclared := sorted(body_names - declared):
            raise ValueError(f"prompt {name}: undeclared inputs in body: {', '.join(undeclared)}")
    else:
        declared = body_names

    local = dict(offered)
    for old_name, new_name in RENAMED_INPUTS.items():
        if old_name in declared and old_name not in local and new_name in local:
            local[old_name] = local[new_name]
        elif new_name in declared and new_name not in local and old_name in local:
            local[new_name] = local[old_name]

    if missing := sorted(declared - local.keys()):
        raise prompt_loader.MissingInputs(f"prompt {name}: missing inputs: {', '.join(missing)}")

    used = {k: ("" if local[k] is None else str(local[k])) for k in sorted(declared)}
    return tmpl.substitute(used), used


def _prepare_values(values: dict[str, object]) -> dict[str, object]:
    offered = dict(values)
    offered.setdefault("owner", owner_asks.NOBODY)
    class_check = offered.get("class_check")
    offered.setdefault("test_classes", class_check or "uv run python -m tests.parallel test_x.SomeTests")
    offered.setdefault("class_check_guidance", (
        f"Run the test classes for the files you changed by name, for example `{class_check}`.\n" if class_check else ""
    ))
    return offered


def render(resolved: ResolvedPrompt | str, *, name: str = "", **values) -> prompt_loader.Rendered:
    if isinstance(resolved, str):
        raw_bytes = resolved.encode("utf-8")
        sha = hashlib.sha256(raw_bytes).hexdigest()
        pname = name or "prompt"
        resolved = ResolvedPrompt(
            call=pname, role="", name=pname, version=f"prompt/{pname}@{sha[:12]}",
            level="engine", sha256=sha, text=resolved,
        )
    offered = _prepare_values(values)
    base_text, used_values = _render_fragment(resolved.name, resolved.text, offered)

    append_texts: list[str] = []
    append_rows: list[dict[str, str]] = []
    for app in resolved.appends:
        frag_text, frag_values = _render_fragment(app.name, app.text, offered)
        used_values.update(frag_values)
        append_rows.append(app.as_row())
        if frag_text:
            append_texts.append(frag_text)

    tail = "\n\n".join(append_texts)
    full_text = f"{base_text}\n\n{tail}" if tail else base_text
    return prompt_loader.Rendered(
        full_text,
        name=resolved.name,
        version=resolved.version,
        level=resolved.level,
        values=used_values,
        sha256=resolved.sha256,
        appends=tuple(append_rows),
        append_tail=tail,
    )


def render_engine(name: str, prompts_dir: Path = paths.PROMPTS_DIR, **values) -> prompt_loader.Rendered:
    from . import pins

    if not NAME_RE.fullmatch(name):
        raise ValueError(f"invalid prompt name {name!r}; use a template's file stem")
    is_engine_dir = prompts_dir.resolve() == paths.PROMPTS_DIR.resolve()
    if not is_engine_dir and (pin := pins.current()) is not None and name in pin.prompt_texts:
        raw_text = pin.prompt_texts[name]
        meta = pin.prompts.get(name, {})
        sha = str(meta.get("sha256") or hashlib.sha256(raw_text.encode("utf-8")).hexdigest())
        version = str(meta.get("id") or f"prompt/{name}@{sha[:12]}")
        level = str(meta.get("level") or "run")
    else:
        cached = prompt_loader.TEMPLATES.get(prompts_dir.absolute(), {}).get(name)
        if cached is not None:
            raw_text = cached
            raw_bytes = cached.encode("utf-8")
        else:
            raw_bytes = (prompts_dir / f"{name}.md").read_bytes()
            raw_text = raw_bytes.decode("utf-8")
        sha = hashlib.sha256(raw_bytes).hexdigest()
        version = f"prompt/{name}@{sha[:12]}"
        level = "engine" if is_engine_dir else "run"
    resolved = ResolvedPrompt(
        call=name, role="", name=name, version=version, level=level, sha256=sha, text=raw_text,
    )
    return render(resolved, **values)


@dataclass(frozen=True)
class PromptProblem:
    code: str
    prompt: str
    path: str
    scope: str
    message: str
    call: str = ""
    role: str = ""
    input_name: str = ""
    has_override: bool = False


def normalize_base_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    m = BASE_SHA_RE.fullmatch(value.strip())
    return m.group(1) if m else None


def has_front_matter(text: str) -> bool:
    return text.startswith(("---\n", "---\r\n"))


def fill_override_front_matter(name: str, body_text: str) -> str:
    if has_front_matter(body_text):
        return body_text
    if not NAME_RE.fullmatch(name):
        return body_text
    engine_file = paths.PROMPTS_DIR / f"{name}.md"
    if not engine_file.is_file():
        return body_text
    try:
        tmpl = prompt_loader.parse_template(f"prompts/{name}.md", body_text)
    except ValueError:
        return body_text
    engine_sha = hashlib.sha256(engine_file.read_bytes()).hexdigest()
    used = sorted(set(tmpl.get_identifiers()))
    fm = (
        "---\n"
        "schema: 1\n"
        f"overrides: arete/prompts/{name}.md\n"
        f"base: sha256:{engine_sha}\n"
        f"inputs: [{', '.join(used)}]\n"
        "---\n"
    )
    return fm + body_text


def rebase_override_base(raw_text: str, new_engine_sha: str) -> str:
    clean_sha = normalize_base_sha(new_engine_sha) or new_engine_sha.strip()
    base_line = f"base: sha256:{clean_sha}"
    if not has_front_matter(raw_text):
        return raw_text
    first_nl = raw_text.find("\n")
    pos = first_nl + 1
    lines: list[str] = []
    closing_start = -1
    while pos <= len(raw_text):
        next_nl = raw_text.find("\n", pos)
        if next_nl == -1:
            line = raw_text[pos:]
            end_pos = len(raw_text)
        else:
            line = raw_text[pos:next_nl]
            end_pos = next_nl + 1
        if line.rstrip("\r") == "---":
            closing_start = pos
            break
        lines.append(line.rstrip("\r"))
        if next_nl == -1:
            break
        pos = end_pos
    if closing_start < 0:
        return raw_text
    replaced = False
    new_lines: list[str] = []
    for line in lines:
        m = KEY_RE.match(line)
        if m and m.group(1) == "base":
            new_lines.append(base_line)
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(base_line)
    return "---\n" + "\n".join(new_lines) + "\n" + raw_text[closing_start:]


def _merged_prompt_table(
    role: str,
    task_kind: str,
    cfg_steps: dict[str, dict],
    cfg_kinds: dict[str, dict[str, dict]],
) -> dict[str, str]:
    from . import config, steps

    defaults, built_in = steps.engine_defaults()
    table: dict[str, str] = {}
    base = config.INHERITS.get(role, "")
    if base:
        table.update(_merged_prompt_table(base, task_kind, cfg_steps, cfg_kinds))
        if isinstance(built_in.get(role, {}).get("prompt"), dict):
            table.update(built_in[role]["prompt"])
    else:
        if isinstance(defaults.get("prompt"), dict):
            table.update(defaults["prompt"])
        if isinstance(built_in.get(role, {}).get("prompt"), dict):
            table.update(built_in[role]["prompt"])
    for source in (cfg_steps.get(role), cfg_kinds.get(task_kind, {}).get(role)):
        if isinstance(source, dict) and isinstance(source.get("prompt"), dict):
            table.update({str(k): str(v) for k, v in source["prompt"].items()})
    return table


def _steps_and_kinds(cfg) -> tuple[dict[str, dict], dict[str, dict[str, dict]]]:
    if cfg is None:
        return {}, {}
    return getattr(cfg, "steps", None) or {}, getattr(cfg, "kinds", None) or {}


def bound_calls_for_prompt(
    cfg,
    prompt_name: str,
    *,
    cfg_steps: dict[str, dict] | None = None,
    cfg_kinds: dict[str, dict[str, dict]] | None = None,
) -> list[tuple[str, str]]:
    from . import config

    s_map, k_map = _steps_and_kinds(cfg) if cfg_steps is None else (cfg_steps, cfg_kinds or {})
    found: set[tuple[str, str]] = set()
    for call, roles in CALL_ROLES.items():
        for role in roles:
            for tk in ("", *config.KINDS):
                merged = _merged_prompt_table(role, tk, s_map, k_map)
                if merged.get(call, call) == prompt_name:
                    found.add((call, role))
    # Also inspect any explicit binding tables in config for calls in OFFERS.
    for role in config.STEP_ROLES:
        for tk in ("", *config.KINDS):
            for source in (s_map.get(role), k_map.get(tk, {}).get(role)):
                if isinstance(source, dict) and isinstance(source.get("prompt"), dict):
                    for call, target in source["prompt"].items():
                        if str(target) == prompt_name and str(call) in OFFERS:
                            found.add((str(call), role))
    return sorted(found)


def _prompt_declared_inputs(name: str, raw: bytes, where: str) -> tuple[set[str] | None, list[str]]:
    problems: list[str] = []
    try:
        meta, body_text = parse(raw)
        tmpl = prompt_loader.parse_template(where, body_text)
    except (UnicodeDecodeError, ValueError) as exc:
        return None, [f"front_matter: {where}: {exc}"]
    body_ids = set(tmpl.get_identifiers())
    if "inputs" in meta and isinstance(meta["inputs"], list):
        declared = {str(x) for x in meta["inputs"]}
        if undeclared := sorted(body_ids - declared):
            problems.append(f"input_undeclared: {where}: undeclared input(s) {', '.join(undeclared)}")
        return declared, problems
    return body_ids, problems


def binding_problems(
    cfg,
    *,
    config_dir: Path | None = None,
    cfg_steps: dict[str, dict] | None = None,
    cfg_kinds: dict[str, dict[str, dict]] | None = None,
) -> list[PromptProblem]:
    from . import config

    root = (config_dir or paths.CONFIG_DIR).resolve()
    s_map, k_map = _steps_and_kinds(cfg) if cfg_steps is None else (cfg_steps, cfg_kinds or {})
    engine_names = engine_prompt_names()
    prompts_cfg_dir = root / "prompts"
    override_names = (
        {p.stem for p in prompts_cfg_dir.glob("*.md") if p.is_file()}
        if prompts_cfg_dir.is_dir()
        else set()
    )
    has_custom_bindings = any(
        isinstance(tbl, dict) and "prompt" in tbl
        for tbl in (
            *s_map.values(),
            *(role_tbl for k_tbl in k_map.values() if isinstance(k_tbl, dict) for role_tbl in k_tbl.values()),
        )
    )
    if not override_names and not has_custom_bindings and all(call in engine_names for call in CALL_ROLES):
        return []

    out: list[PromptProblem] = []
    recorded_keys: set[tuple[str, str, str, str, str]] = set()
    parsed_cache: dict[tuple[str, bool], tuple[set[str] | None, list[str]]] = {}

    def _check_binding(call: str, role: str, tk: str, target: str) -> None:
        scope = f"step:{role}:{tk}" if tk else f"step:{role}"
        has_override = target in override_names
        if target == call and target in engine_names and not has_override:
            return
        if target not in engine_names:
            key = ("prompt_missing", target, call, scope, "")
            if key not in recorded_keys:
                recorded_keys.add(key)
                where = f"prompts/{target}.md" if has_override else "config.toml"
                out.append(PromptProblem(
                    code="prompt_missing",
                    prompt=target,
                    path=where,
                    scope=scope,
                    message=f"prompt_missing: call {call} (role {role}) binds to missing prompt {target!r}",
                    call=call,
                    role=role,
                    has_override=has_override,
                ))
            return
        where = f"prompts/{target}.md" if has_override else f"arete/prompts/{target}.md"
        cache_key = (target, has_override)
        if cache_key not in parsed_cache:
            src_file = (prompts_cfg_dir / f"{target}.md") if has_override else (paths.PROMPTS_DIR / f"{target}.md")
            try:
                raw = src_file.read_bytes()
            except OSError as exc:
                parsed_cache[cache_key] = (None, [f"front_matter: {where}: {exc}"])
            else:
                parsed_cache[cache_key] = _prompt_declared_inputs(target, raw, where)
        declared, parse_probs = parsed_cache[cache_key]
        for msg in parse_probs:
            code = msg.partition(":")[0]
            key = (code, target, call, scope, msg)
            if key not in recorded_keys:
                recorded_keys.add(key)
                out.append(PromptProblem(
                    code=code,
                    prompt=target,
                    path=where if has_override else "config.toml",
                    scope=scope,
                    message=msg,
                    call=call,
                    role=role,
                    has_override=has_override,
                ))
        if declared is None or call not in OFFERS:
            return
        offered = OFFERS[call]
        for inp in sorted(declared):
            canonical = RENAMED_INPUTS.get(inp, inp)
            if canonical not in offered:
                key = ("input_not_offered", target, call, scope, inp)
                if key not in recorded_keys:
                    recorded_keys.add(key)
                    out.append(PromptProblem(
                        code="input_not_offered",
                        prompt=target,
                        path=where if has_override else "config.toml",
                        scope=scope,
                        message=(
                            f"input_not_offered: {where}: call {call} does not offer {inp} "
                            f"(prompt {target})"
                        ),
                        call=call,
                        role=role,
                        input_name=inp,
                        has_override=has_override,
                    ))

    for call, roles in CALL_ROLES.items():
        for role in roles:
            for tk in ("", *config.KINDS):
                merged = _merged_prompt_table(role, tk, s_map, k_map)
                target = str(merged.get(call, call))
                _check_binding(call, role, tk, target)

    if has_custom_bindings:
        for role in config.STEP_ROLES:
            for tk in ("", *config.KINDS):
                for source in (s_map.get(role), k_map.get(tk, {}).get(role)):
                    if isinstance(source, dict) and isinstance(source.get("prompt"), dict):
                        for call, target in source["prompt"].items():
                            _check_binding(str(call), role, tk, str(target))
    return out


def validate_override(
    name: str,
    path: str,
    body_text: str,
    *,
    cfg=None,
    config_dir: Path | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    where = path or f"prompts/{name}.md"
    problems: list[str] = []
    warnings: list[dict[str, str]] = []

    if not has_front_matter(body_text):
        problems.append(
            f"front_matter: {where}: config prompt requires front matter with schema, overrides, base, and inputs"
        )
        return problems, warnings
    try:
        meta, parsed_body = parse(body_text)
    except ValueError as exc:
        problems.append(f"front_matter: {where}: {exc}")
        return problems, warnings

    if meta.get("schema") != 1:
        problems.append(f"front_matter: {where}: front matter requires schema: 1")
    expected_overrides = f"arete/prompts/{name}.md"
    engine_file = paths.PROMPTS_DIR / f"{name}.md"
    if meta.get("overrides") != expected_overrides:
        problems.append(f"front_matter: {where}: front matter overrides must be {expected_overrides!r}")
    elif not engine_file.is_file():
        problems.append(f"front_matter: {where}: engine prompt {expected_overrides} does not exist")
    if normalize_base_sha(meta.get("base")) is None:
        problems.append(f"front_matter: {where}: front matter base must be sha256:<64 hex>")
    if "inputs" not in meta or not isinstance(meta["inputs"], list):
        problems.append(f"front_matter: {where}: front matter requires inputs list")

    try:
        tmpl = prompt_loader.parse_template(where, parsed_body)
        body_ids = set(tmpl.get_identifiers())
    except ValueError as exc:
        problems.append(f"front_matter: {where}: {exc}")
        return problems, warnings

    declared = {str(x) for x in meta["inputs"]} if isinstance(meta.get("inputs"), list) else set()
    if isinstance(meta.get("inputs"), list):
        if undeclared := sorted(body_ids - declared):
            problems.append(f"input_undeclared: {where}: undeclared input(s) {', '.join(undeclared)}")

    if cfg is None:
        from . import config

        cfg, _ = config.load_or_problem()
    bound = bound_calls_for_prompt(cfg, name)
    if not bound:
        problems.append(
            f"prompt_unbound: {where}: prompt {name!r} is only read via render_engine and has no bound step calls"
        )
    else:
        seen_unoffered: set[tuple[str, str]] = set()
        for call, _role in bound:
            offered = OFFERS.get(call, frozenset())
            for inp in sorted(declared):
                canonical = RENAMED_INPUTS.get(inp, inp)
                if canonical not in offered and (call, inp) not in seen_unoffered:
                    seen_unoffered.add((call, inp))
                    problems.append(f"input_not_offered: {where}: call {call} does not offer {inp}")

    for old_name in check_renamed_inputs(meta, parsed_body):
        warnings.append({
            "code": "input_renamed",
            "path": where,
            "message": f"{where}: input {old_name!r} was renamed to {RENAMED_INPUTS[old_name]!r}",
        })
    return problems, warnings


def engine_base_bytes(name: str, base_hex: str) -> bytes | None:
    if not base_hex or not NAME_RE.fullmatch(name):
        return None
    engine_root = paths.PACKAGE_DIR.parent
    if not (engine_root / ".git").exists():
        return None
    from . import environment

    rel = f"arete/prompts/{name}.md"
    env = environment.build((), {"GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    try:
        log = subprocess.run(
            ["git", "--no-pager", "--no-replace-objects", "log", "-n", "100", "--format=%H", "--", rel],
            cwd=engine_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if log.returncode != 0:
        return None
    for sha in log.stdout.splitlines():
        sha = sha.strip()
        if not sha:
            continue
        try:
            blob = subprocess.run(
                ["git", "--no-pager", "--no-replace-objects", "cat-file", "blob", f"{sha}:{rel}"],
                cwd=engine_root,
                env=env,
                capture_output=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if blob.returncode == 0 and hashlib.sha256(blob.stdout).hexdigest() == base_hex:
            return blob.stdout
    return None


def _text_diff(before: str, after: str, fromfile: str, tofile: str) -> str:
    return "".join(unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=fromfile,
        tofile=tofile,
    ))


def drift_spec(name: str, override_bytes: bytes, engine_bytes: bytes, *, base_hex: str) -> dict:
    engine_sha = hashlib.sha256(engine_bytes).hexdigest()
    override_sha = hashlib.sha256(override_bytes).hexdigest()
    engine_text = engine_bytes.decode("utf-8", errors="replace")
    override_text = override_bytes.decode("utf-8", errors="replace")
    base_bytes = engine_base_bytes(name, base_hex) if base_hex else None
    diff_engine_to_override = _text_diff(engine_text, override_text, f"engine/{name}.md", f"override/{name}.md")
    if base_bytes is not None:
        base_text = base_bytes.decode("utf-8", errors="replace")
        diff_base_to_engine = _text_diff(base_text, engine_text, f"base/{name}.md", f"engine/{name}.md")
        diff_base_to_override = _text_diff(base_text, override_text, f"base/{name}.md", f"override/{name}.md")
        combined = (
            "=== base -> engine ===\n" + diff_base_to_engine
            + "\n=== base -> override ===\n" + diff_base_to_override
        )
        detail: dict[str, object] = {
            "name": name,
            "path": f"prompts/{name}.md",
            "overrides": f"arete/prompts/{name}.md",
            "base_sha": base_hex,
            "engine_sha": engine_sha,
            "override_sha": override_sha,
            "mode": "three_way",
            "base_known": True,
            "base": base_text,
            "engine": engine_text,
            "override": override_text,
            "diff_base_to_engine": diff_base_to_engine,
            "diff_base_to_override": diff_base_to_override,
            "diff_engine_to_override": diff_engine_to_override,
            "diff": combined,
            "stamp": f"drift:{name}:{base_hex}:{engine_sha}:{override_sha}",
        }
    else:
        note = "base is unknown (no engine git commit holds base sha256)"
        detail = {
            "name": name,
            "path": f"prompts/{name}.md",
            "overrides": f"arete/prompts/{name}.md",
            "base_sha": base_hex,
            "engine_sha": engine_sha,
            "override_sha": override_sha,
            "mode": "two_way",
            "base_known": False,
            "note": note,
            "base": None,
            "engine": engine_text,
            "override": override_text,
            "diff_engine_to_override": diff_engine_to_override,
            "diff": f"# {note}\n{diff_engine_to_override}",
            "stamp": f"drift:{name}:{base_hex}:{engine_sha}:{override_sha}",
        }
    return {
        "kind": "drift",
        "subject": f"config:prompt/{name}",
        "task_id": "",
        "run_id": "",
        "line": "",
        "priority": 2,
        "title": f"Engine prompt {name} changed under config override",
        "detail": detail,
        "verbs": ["keep_mine", "take_engine", "continue"],
        "options": [],
    }


def prompt_invalid_specs(cfg, *, config_dir: Path | None = None) -> list[dict]:
    from . import util

    problems = binding_problems(cfg, config_dir=config_dir)
    if not problems:
        return []
    grouped: dict[str, list[PromptProblem]] = {}
    for prob in problems:
        grouped.setdefault(prob.prompt, []).append(prob)
    specs: list[dict] = []
    for prompt_name, items in sorted(grouped.items()):
        has_override = any(p.has_override for p in items)
        entries = [
            {
                "code": p.code,
                "path": p.path,
                "call": p.call,
                "role": p.role,
                "input": p.input_name,
                "scope": p.scope,
                "pauses": [p.scope],
                "text": p.message,
            }
            for p in items
        ]
        summary = "; ".join(p.message for p in items)
        verbs = ["take_engine", "continue"] if has_override else ["continue"]
        specs.append({
            "kind": "config_invalid",
            "subject": f"config:prompt/{prompt_name}",
            "task_id": "",
            "run_id": "",
            "line": "",
            "priority": 1,
            "title": f"Invalid prompt config for {prompt_name}: {items[0].code}",
            "detail": {
                "name": prompt_name,
                "path": f"prompts/{prompt_name}.md" if has_override else "config.toml",
                "has_override": has_override,
                "problems": entries,
                "summary": summary,
                "stamp": util.digest(json.dumps(entries, sort_keys=True, default=str)),
            },
            "verbs": verbs,
            "options": [],
        })
    return specs


def _sync_inbox_specs(c: sqlite3.Connection, kind: str, pattern: str, specs: list[dict]) -> None:
    from .store import inbox as inbox_store

    if not inbox_store.available(c):
        return
    wanted = {(spec["kind"], spec["subject"]): spec for spec in specs}
    existing_rows = inbox_store.unresolved_for_pattern(c, pattern, kinds=(kind,))
    present: set[tuple[str, str]] = set()
    for row in existing_rows:
        key = (row["kind"], row["subject"])
        spec = wanted.get(key)
        if spec is None:
            inbox_store.resolve_item(c, row["id"], resolution="cleared")
            continue
        present.add(key)
        inbox_store.update_item(
            c,
            row["id"],
            title=spec["title"],
            detail=spec["detail"],
            priority=spec["priority"],
            verbs=spec["verbs"],
            options=spec["options"],
            task_id=spec["task_id"],
            run_id=spec["run_id"],
            line=spec["line"],
        )
    for key, spec in wanted.items():
        if key in present:
            continue
        stamp = (spec["detail"] or {}).get("stamp")
        if stamp:
            prev = inbox_store.newest_resolved_by_subject(c, spec["subject"], kind=spec["kind"])
            if prev and prev["resolution"] != "cleared" and prev["detail"].get("stamp") == stamp:
                continue
        inbox_store.raise_item(
            c,
            kind=spec["kind"],
            subject=spec["subject"],
            priority=spec["priority"],
            title=spec["title"],
            detail=spec["detail"],
            verbs=spec["verbs"],
            options=spec["options"],
            task_id=spec["task_id"],
            run_id=spec["run_id"],
            line=spec["line"],
        )


def _live_prompt_overrides(root: Path) -> list[tuple[str, str, bytes]]:
    from . import configrepo

    prompts_dir = root / "prompts"
    if not prompts_dir.is_dir():
        return []
    use_repo = configrepo.is_repo(root)
    overrides: list[tuple[str, str, bytes]] = []
    for file in sorted(prompts_dir.glob("*.md")):
        if not file.is_file() or not NAME_RE.fullmatch(file.stem):
            continue
        rel = f"prompts/{file.name}"
        raw = configrepo.live_bytes(rel, root) if use_repo else file.read_bytes()
        if raw is None:
            continue
        overrides.append((file.stem, rel, raw))
    return overrides


def _delete_merged_override(root: Path, rel: str, name: str) -> None:
    from . import configrepo

    if configrepo.is_repo(root):
        blob = configrepo._live_blob(root, rel)
        if blob != configrepo.ABSENT:
            configrepo.commit(
                {rel: None},
                {rel: blob},
                actor="engine",
                source="merge",
                note=f"remove prompt/{name} override matching engine",
            )
    else:
        with suppress(FileNotFoundError):
            (root / rel).unlink()


def sync_overrides(c: sqlite3.Connection | None = None) -> dict[str, list[str]]:
    from . import config
    from .store import db as foundation
    from .store import inbox as inbox_store

    root = paths.CONFIG_DIR.resolve()
    cfg, _ = config.load_or_problem()
    overrides = _live_prompt_overrides(root)
    to_delete: list[tuple[str, str]] = []
    drift_specs: list[dict] = []

    for name, rel, override_bytes in overrides:
        engine_file = paths.PROMPTS_DIR / f"{name}.md"
        if not engine_file.is_file():
            continue
        engine_bytes = engine_file.read_bytes()
        try:
            ov_meta, ov_body = parse(override_bytes)
            eng_meta, eng_body = parse(engine_bytes)
        except (UnicodeDecodeError, ValueError):
            continue
        ov_inputs = sorted(str(x) for x in ov_meta.get("inputs", [])) if isinstance(ov_meta.get("inputs"), list) else []
        eng_inputs = sorted(str(x) for x in eng_meta.get("inputs", [])) if isinstance(eng_meta.get("inputs"), list) else []
        if ov_body.replace("\r\n", "\n") == eng_body.replace("\r\n", "\n") and ov_inputs == eng_inputs:
            to_delete.append((name, rel))
            continue
        base_hex = normalize_base_sha(ov_meta.get("base")) or ""
        engine_sha = hashlib.sha256(engine_bytes).hexdigest()
        if base_hex != engine_sha:
            drift_specs.append(drift_spec(name, override_bytes, engine_bytes, base_hex=base_hex))

    deleted: list[str] = []
    if to_delete:
        if foundation.in_write():
            for name, rel in to_delete:
                deleted.append(name)
                foundation.after_commit(lambda r=root, p=rel, n=name: _delete_merged_override(r, p, n))
        else:
            for name, rel in to_delete:
                _delete_merged_override(root, rel, name)
                deleted.append(name)

    invalid_specs = prompt_invalid_specs(cfg, config_dir=root)
    if deleted and foundation.in_write():
        deleted_subjects = {f"config:prompt/{name}" for name in deleted}
        invalid_specs = [s for s in invalid_specs if s["subject"] not in deleted_subjects]

    if c is not None:
        _sync_inbox_specs(c, "drift", "config:prompt/%", drift_specs)
        _sync_inbox_specs(c, "config_invalid", "config:prompt/%", invalid_specs)
    elif paths.DB_FILE.exists():
        try:
            with foundation.tx() as conn:
                if inbox_store.available(conn):
                    _sync_inbox_specs(conn, "drift", "config:prompt/%", drift_specs)
                    _sync_inbox_specs(conn, "config_invalid", "config:prompt/%", invalid_specs)
        except (foundation.NoDatabase, foundation.IncompatibleSchema):
            pass

    return {
        "deleted": deleted,
        "drift": [s["subject"] for s in drift_specs],
        "invalid": [s["subject"] for s in invalid_specs],
    }
