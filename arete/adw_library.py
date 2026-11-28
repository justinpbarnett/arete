from __future__ import annotations

import copy
import re
import tomllib
from collections.abc import Collection
from pathlib import Path

from . import configkinds, configschema, paths

_PROMPT_FILE_RE = re.compile(rf"^{configkinds.PROMPT_NAME_PATTERN}\.md$")


class LibraryError(ValueError):
    pass


def _registered_steps() -> frozenset[str]:
    from . import factory, flows, orchestration

    _ = (flows, orchestration)
    return frozenset(factory.STEPS)


def _valid_roles() -> frozenset[str]:
    from . import flows

    return frozenset(flows.SETTABLE.values()) & frozenset(configkinds.ROLE_NAMES)


def _registered_sources() -> frozenset[str]:
    from . import sources

    return frozenset(sources.SOURCES)


def _registered_route_events() -> frozenset[str]:
    from . import adw

    return frozenset(adw.ROUTE_EVENTS)


def _check_pins(pins: list[dict], port_types: set[str], used_ports: set[str], where: str, errors: list[str]) -> None:
    seen: set[str] = set()
    for idx, entry in enumerate(pins):
        pin_name = entry["pin"]
        pin_type = entry["type"]
        if pin_name in seen:
            errors.append(f"{where}[{idx}]: duplicate pin {pin_name!r}")
        else:
            seen.add(pin_name)
        if pin_type not in port_types:
            errors.append(f"{where}[{idx}]: pin {pin_name!r} references undefined port type {pin_type!r}")
        else:
            used_ports.add(pin_type)


def validate(
    data: object,
    *,
    where: str = "arete/adws/library.toml",
    prompts_dir: Path | None = None,
    steps: Collection[str] | None = None,
    roles: Collection[str] | None = None,
    source_ids: Collection[str] | None = None,
    route_events: Collection[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    try:
        configschema.validate("library", data, where=where, allow_read_only=True)
    except ValueError as error:
        errors.append(str(error))
        return errors
    assert isinstance(data, dict)

    port_types: set[str] = set()
    used_ports: set[str] = set()
    for idx, pt in enumerate(data["portType"]):
        pid = pt["id"]
        if pid == "flow":
            errors.append(f"{where}.portType[{idx}]: 'flow' is a flow-pin shape and not a port type")
        elif pid in port_types:
            errors.append(f"{where}.portType[{idx}]: duplicate port type {pid!r}")
        else:
            port_types.add(pid)

    known_steps = frozenset(steps) if steps is not None else _registered_steps()
    known_roles = frozenset(roles) if roles is not None else _valid_roles()
    prompt_root = prompts_dir if prompts_dir is not None else paths.PROMPTS_DIR

    seen_steps: set[str] = set()
    for idx, step in enumerate(data["step"]):
        loc = f"{where}.step[{idx}]"
        sid = step["id"]
        if sid in seen_steps:
            errors.append(f"{loc}: duplicate step {sid!r}")
        else:
            seen_steps.add(sid)
        if sid not in known_steps:
            errors.append(f"{loc}: step {sid!r} is not registered in factory.STEPS")
        kind = step["kind"]
        prompt = step["prompt"]
        role = step["role"]
        if kind == "script":
            if prompt:
                errors.append(f"{loc}: script step {sid!r} must have an empty prompt")
            if role:
                errors.append(f"{loc}: script step {sid!r} must have an empty role")
        if prompt:
            if not _PROMPT_FILE_RE.fullmatch(prompt):
                errors.append(f"{loc}: step {sid!r} has invalid prompt filename {prompt!r}")
            elif not (prompt_root / prompt).is_file():
                errors.append(f"{loc}: step {sid!r} references missing prompt file {prompt!r}")
        if role and role not in known_roles:
            errors.append(f"{loc}: step {sid!r} references unknown role {role!r}")
        if step["band"]:
            errors.append(f"{loc}: step {sid!r} band must be empty in the engine library")
        _check_pins(step["in"], port_types, used_ports, f"{loc}.in", errors)
        _check_pins(step["out"], port_types, used_ports, f"{loc}.out", errors)

    for missing_step in sorted(known_steps - seen_steps):
        errors.append(f"{where}.step: missing registered step {missing_step!r}")

    known_sources = frozenset(source_ids) if source_ids is not None else _registered_sources()
    known_events = frozenset(route_events) if route_events is not None else _registered_route_events()

    seen_triggers: set[str] = set()
    seen_sources: set[str] = set()
    seen_events: set[str] = set()
    for idx, trig in enumerate(data["trigger"]):
        loc = f"{where}.trigger[{idx}]"
        tid = trig["id"]
        if tid in seen_triggers:
            errors.append(f"{loc}: duplicate trigger {tid!r}")
        else:
            seen_triggers.add(tid)
        if trig["kind"] == "source":
            seen_sources.add(tid)
            if tid not in known_sources:
                errors.append(f"{loc}: source trigger {tid!r} is not registered in sources.SOURCES")
        elif trig["kind"] == "event":
            seen_events.add(tid)
        seen_params: set[str] = set()
        for pidx, param in enumerate(trig["params"]):
            pname = param["name"]
            if pname in seen_params:
                errors.append(f"{loc}.params[{pidx}]: duplicate parameter {pname!r}")
            else:
                seen_params.add(pname)
        _check_pins(trig["out"], port_types, used_ports, f"{loc}.out", errors)

    for missing_source in sorted(known_sources - seen_sources):
        errors.append(f"{where}.trigger: missing registered source trigger {missing_source!r}")
    for missing_event in sorted(known_events - seen_events):
        errors.append(f"{where}.trigger: missing route event trigger {missing_event!r}")

    for unused in sorted(port_types - used_ports):
        errors.append(f"{where}.portType: unused port type {unused!r}")

    return errors


def serialize(data: dict) -> dict:
    return {
        "portTypes": copy.deepcopy(data["portType"]),
        "steps": copy.deepcopy(data["step"]),
        "triggers": copy.deepcopy(data["trigger"]),
    }


def load(path: Path | None = None, *, prompts_dir: Path | None = None) -> dict:
    target = path if path is not None else paths.ENGINE_ADWS_DIR / "library.toml"
    raw = target.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise LibraryError(f"{target}: invalid TOML: {error}") from error
    if errors := validate(data, where=str(target), prompts_dir=prompts_dir):
        raise LibraryError("; ".join(errors))
    return serialize(data)


def check_engine(path: Path | None = None, *, prompts_dir: Path | None = None) -> dict:
    target = path if path is not None else paths.ENGINE_ADWS_DIR / "library.toml"
    try:
        raw = target.read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        errors = [f"{target}: {error}"]
    else:
        errors = validate(data, where=str(target), prompts_dir=prompts_dir)
    if errors:
        return {
            "status": "invalid",
            "stage": "schema_and_references",
            "stage_number": 3,
            "errors": errors,
            "stages": {"schema_and_references": {"status": "invalid", "errors": errors}},
        }
    return {
        "status": "valid",
        "stage": None,
        "errors": [],
        "stages": {"schema_and_references": {"status": "valid", "errors": []}},
    }


def check_config_file(rel: str) -> dict:
    errors = [f"{rel}: config kind 'library' is read only"]
    return {
        "status": "invalid",
        "stage": "schema_and_references",
        "stage_number": 3,
        "errors": errors,
        "stages": {"schema_and_references": {"status": "invalid", "errors": errors}},
    }
