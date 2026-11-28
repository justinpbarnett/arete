from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

from . import paths


@cache
def _document(path: Path) -> dict:
    return json.loads(path.read_text())


def resolve(shape: dict, folder: Path, filename: str) -> tuple[dict, str]:
    seen = set()
    while "$ref" in shape:
        source, _, fragment = shape["$ref"].partition("#")
        filename = source or filename
        if Path(filename).name != filename or not filename.endswith(".json"):
            raise ValueError(f"invalid schema reference {shape['$ref']!r}")
        target = (filename, fragment)
        if target in seen:
            raise ValueError(f"circular schema reference {shape['$ref']!r}")
        seen.add(target)
        shape = _document(folder / filename)
        if fragment:
            if not fragment.startswith("/"):
                raise ValueError(f"invalid schema fragment {fragment!r}")
            for key in fragment[1:].split("/"):
                shape = shape[key.replace("~1", "/").replace("~0", "~")]
    return shape, filename


def schema(name: str, *path: str) -> dict:
    folder, filename = paths.CONFIG_SCHEMAS_DIR, f"{name}.json"
    found = _document(folder / filename)
    for key in path:
        found, filename = resolve(found, folder, filename)
        found = found[key]
    return resolve(found, folder, filename)[0]


def keys(shape: dict) -> tuple[str, ...]:
    return tuple(shape["properties"])


def enum(name: str, *path: str) -> tuple:
    return tuple(schema(name, *path)["enum"])


def fields_of_type(shape: dict, kind: str) -> tuple[str, ...]:
    return tuple(key for key, field in shape["properties"].items() if field.get("type") == kind)


TYPES = {"object": (dict,), "array": (list, tuple), "integer": (int,), "number": (int, float),
         "boolean": (bool,), "string": (str,), "null": (type(None),)}


def validate(name: str, value, *, where: str | None = None, allow_read_only: bool = False) -> None:
    folder, filename = paths.CONFIG_SCHEMAS_DIR, f"{name}.json"
    shape = _document(folder / filename)
    where = where or name
    if domains := shape.get("x-arete-rule-domains"):
        domain = value.get("domain") if isinstance(value, dict) else None
        if not isinstance(domain, str) or domain not in domains:
            raise ValueError(f"{where}.domain must be one of {', '.join(domains)}")
        filename = domains[domain]
        try:
            shape = _document(folder / filename)
        except FileNotFoundError:
            raise ValueError(f"{where}: {domain} rules are unavailable for writes") from None
        resolved, _ = resolve(shape, folder, filename)
        if (resolved.get("type") != "object" or resolved.get("additionalProperties") is not False
                or not resolved.get("properties")):
            raise ValueError(f"{where}: {domain} rules need a strict schema for writes")
    check(value, shape, folder, filename, where, allow_read_only=allow_read_only)


def check(value, shape: dict, folder: Path, filename: str, where: str, *, allow_read_only: bool = False) -> None:
    shape, filename = resolve(shape, folder, filename)
    if shape.get("x-arete-read-only") and not allow_read_only:
        raise ValueError(f"{where} is unavailable for writes")
    if types := shape.get("type"):
        types = [types] if isinstance(types, str) else types
        if not any(type(value) in TYPES[kind] for kind in types):
            raise ValueError(f"{where} must be {' or '.join(types)}")
    if "enum" in shape and not any(type(value) is type(choice) and value == choice for choice in shape["enum"]):
        raise ValueError(f"{where} must be one of {', '.join(map(repr, shape['enum']))}")
    if isinstance(value, str):
        if "minLength" in shape and len(value) < shape["minLength"]:
            raise ValueError(f"{where} must be at least {shape['minLength']} characters")
        if "maxLength" in shape and len(value) > shape["maxLength"]:
            raise ValueError(f"{where} must be at most {shape['maxLength']} characters")
        if "pattern" in shape and not re.fullmatch(shape["pattern"], value):
            raise ValueError(f"{where} does not match pattern {shape['pattern']!r}")
    elif type(value) in (int, float):
        if "minimum" in shape and value < shape["minimum"]:
            raise ValueError(f"{where} must be >= {shape['minimum']}")
        if "maximum" in shape and value > shape["maximum"]:
            raise ValueError(f"{where} must be <= {shape['maximum']}")
    if isinstance(value, dict):
        properties = shape.get("properties", {})
        additional = shape.get("additionalProperties", True)
        if missing := set(shape.get("required", ())) - value.keys():
            raise ValueError(f"{where} requires {', '.join(sorted(missing))}")
        if additional is False and (unknown := value.keys() - properties.keys()):
            raise ValueError(f"{where}: unknown key {', '.join(sorted(unknown))}")
        for key, entry in value.items():
            field = properties.get(key, additional)
            if isinstance(field, dict):
                check(entry, field, folder, filename, f"{where}.{key}", allow_read_only=allow_read_only)
    elif isinstance(value, (list, tuple)):
        if "minItems" in shape and len(value) < shape["minItems"]:
            raise ValueError(f"{where} must contain at least {shape['minItems']} items")
        if shape.get("uniqueItems"):
            seen_items: list[object] = []
            for item in value:
                if any(type(prev) is type(item) and prev == item for prev in seen_items):
                    raise ValueError(f"{where} must not contain duplicate items")
                seen_items.append(item)
        if "items" in shape:
            for index, item in enumerate(value):
                check(item, shape["items"], folder, filename, f"{where}[{index}]", allow_read_only=allow_read_only)
