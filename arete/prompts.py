from __future__ import annotations

import copy
import hashlib
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from . import owner_asks, paths, prompt_loader, refusals

NAME_RE = re.compile(r"[a-zA-Z0-9_-]+")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$")
RENAMED_INPUTS: dict[str, str] = {"workspace": "worktree"}

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
