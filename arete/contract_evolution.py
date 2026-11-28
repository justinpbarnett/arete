from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

from . import contract

SOURCE = "arete/contract.py"
REGISTRY_INPUTS = (SOURCE, "arete/refusals.py")
REQUIRED_PRODUCERS = (*REGISTRY_INPUTS, "arete/contract_evolution.py")
CONFIG_REGISTRY = "arete/configkinds.py"
PRODUCERS = (*REQUIRED_PRODUCERS, CONFIG_REGISTRY)
OPENAPI = "docs/openapi.json"
BOOTSTRAP = "arete/contract_v1_baseline.json"
BOOTSTRAP_SOURCES = {
    SOURCE: "ab207c2a825c3e437a8789ea0d1c45306cd6f7767d4b87bff59210b5ff114d16",
    "arete/refusals.py": "1ecf93a2ed4d349a8b93cf4b5682f1711d7050c652c012ba6d25ba748227d714",
}
BOOTSTRAP_DATA_SHA = "573982524c52d71684edd5fb1d079c8ddc2b54dee2bc6fff33dd6043053ce7de"
BOOTSTRAP_OLDEST_CLIENT = dict.fromkeys(("cli", "mcp", "web"), "1.0")
RESERVED_PATHS = {"/api/v1/events", "/api/v1/principals", "/api/v1/principals/{id}"}
RESERVED_FIELDS = {"seq", "event_seq", "subject", "ver"}


def wire_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def document_json(value, depth=0):
    if depth == 3 or not isinstance(value, dict) or not value:
        return wire_json(value)
    indent = "  " * depth
    entries = [f"{indent}  {wire_json(k)}: {document_json(v, depth + 1)}" for k, v in sorted(value.items())]
    return "{\n" + ",\n".join(entries) + "\n" + indent + "}"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sources(read, paths=PRODUCERS):
    return {path: sha(read(path).replace(b"\r\n", b"\n")) for path in paths}


def ref(name):
    return {"$ref": "#/components/schemas/" + name}


def result_schema(op, operations):
    result = op["result"]
    if op["name"] == "batch.run":
        reads = [read for read in operations if read["method"] == "GET"]
        items = result.get("properties", {}).get("results", {}).get("items")
        if items == {"anyOf": [read["result"] for read in reads]}:
            result = deepcopy(result)
            result["properties"]["results"]["items"] = {"anyOf": [ref(read["name"] + ".result") for read in reads]}
    return result


def snapshot(registry, minor, oldest_client):
    operations = registry["operations"]
    return {"minor": minor, "oldest_client": oldest_client, "operations": {op["name"]: {
        **{key: value for key, value in op.items() if key not in ("example", "scope", "success_status")},
        "result": result_schema(op, operations),
    } for op in operations}, "aliases": {route(alias): alias for alias in registry["aliases"]},
        "errors": {e["code"]: {k: e[k] for k in ("status", "schema")} for e in registry["errors"]}}


def route(entry):
    return f"{entry['method']} {entry['path']}"


def registry_problems(registry):
    names, routes = set(), set()
    for operation in registry["operations"]:
        name, path = operation["name"], operation["path"]
        if name in names or route(operation) in routes:
            yield f"duplicate operation name or route {name} ({route(operation)})"
        names.add(name)
        routes.add(route(operation))
        if path in RESERVED_PATHS or path == "/api/v1" or not path.startswith("/api/v1/"):
            yield f"{name} uses reserved or invalid path {path}"
        for field in sorted(RESERVED_FIELDS & operation["input"].get("properties", {}).keys()):
            if name == "inbox.raise" and field == "subject":
                continue
            yield f"{name} ({route(operation)}): input.{field} is reserved"
    if len({route(alias) for alias in registry["aliases"]}) != len(registry["aliases"]):
        yield "duplicate legacy alias route"
    errors = [error["code"] for error in registry["errors"]]
    if len(set(errors)) != len(errors):
        yield "duplicate problem code"
    for op in registry["operations"]:
        if missing := set(op["errors"]) - set(errors):
            yield f"{op['name']} ({route(op)}): unknown errors {', '.join(sorted(missing))}"


def openapi(registry, minor, source_hashes, oldest_client):
    from . import credential_http

    schemas, paths, errors, problems = {}, {}, {}, {}
    for error in registry["errors"]:
        code = error["code"]
        schemas[f"problem.{code}"] = error["schema"]
        errors[code] = {k: error[k] for k in ("status", "description", "example")}
    for kind, detail_schema in sorted(contract.INBOX_DETAIL_SHAPES.items()):
        schemas[f"inbox.detail.{kind}"] = detail_schema
    inbox_detail_refs = {kind: ref(f"inbox.detail.{kind}") for kind in sorted(contract.INBOX_DETAIL_SHAPES)}
    for op in registry["operations"]:
        name, method, path = (op[k] for k in ("name", "method", "path"))
        schemas[f"{name}.input"] = op["input"]
        schemas[f"{name}.result"] = result_schema(op, registry["operations"])
        example = op["example"]
        statuses = defaultdict(list)
        for code in op["errors"]:
            statuses[errors[code]["status"]].append(code)
        success = {"description": "Successful result", "content": {"application/json": {
            "schema": ref(f"{name}.result"), "example": example["result"]}}}
        responses = {str(op.get("success_status", 200)): success}
        for status, codes in sorted(statuses.items()):
            key = ".".join(sorted(codes))
            if key not in problems:
                problems[key] = {"description": HTTPStatus(status).phrase, "content": {"application/problem+json": {
                    "schema": {"oneOf": [ref(f"problem.{code}") for code in sorted(codes)]},
                    "examples": {code: {"value": errors[code]["example"]} for code in codes}}}}
            responses[str(status)] = {"$ref": f"#/components/responses/{key}"}
        entry = {"operationId": name, "responses": responses, "x-arete-errors": op["errors"]}
        if name in ("inbox.list", "inbox.get", "triage_screen.get"):
            entry["x-arete-detail-shapes"] = inbox_detail_refs
        if name in credential_http.PRESENCE:
            entry["x-arete-presence"] = True
        if "deprecated" in op:
            entry["deprecated"] = op["deprecated"]
        if "sunset" in op:
            entry["x-arete-sunset"] = op["sunset"]
        inputs = op["input"]
        fields = inputs.get("properties", {})
        required = inputs.get("required", [])
        path_fields = set(re.findall(r"\{(\w+)\}", path))
        entry["parameters"] = [{"name": field, "in": "path" if field in path_fields else "query",
                                "required": field in required, "schema": schema,
                                **({"example": example["input"][field]} if field in example["input"] else {})}
                               for field, schema in sorted(fields.items()) if method == "GET" or field in path_fields]
        if method == "GET":
            responses["304"] = {"description": "The representation matches If-None-Match"}
        else:
            entry["parameters"].append({"name": "X-Arete", "in": "header", "required": False,
                                        "description": "Required for writes using a browser session.",
                                        "schema": {"type": "string", "const": "1"}})
            if contract.keyed_write(op):
                entry["parameters"].append({"name": "Idempotency-Key", "in": "header", "required": True,
                                            "schema": {"type": "string", "minLength": 8, "maxLength": 128}})
            if media := op.get("body_media"):
                entry["requestBody"] = {"required": True, "content": {media: {"schema": {"type": "string"}}}}
            elif method != "DELETE" or fields.keys() - path_fields:
                body = inputs if not path_fields else {**inputs, "properties": {k: v for k, v in fields.items()
                         if k not in path_fields}, "required": [k for k in required if k not in path_fields]}
                entry["requestBody"] = {"required": True, "content": {"application/json": {
                    "schema": ref(f"{name}.input") if not path_fields else body,
                    "example": {k: v for k, v in example["input"].items() if k not in path_fields}}}}
        paths.setdefault(path, {})[method.lower()] = entry
    meta = paths["/api/v1/meta"]["get"]["responses"]
    identity = {"Arete-Instance": {"schema": {"type": "string", "pattern": "^[0-9a-f]{32}$"}}}
    meta["200"]["headers"] = {**identity, "Arete-Session": {"schema": {"const": "paired"}}}
    forbidden = meta["403"]["$ref"].rsplit("/", 1)[-1]
    meta["403"] = {**problems[forbidden], "headers": identity}
    return {"openapi": "3.1.0", "info": {"title": "Arete API", "version": f"1.{minor}"}, "paths": paths,
            "components": {"schemas": schemas, "responses": problems}, "x-arete-contract-sources-sha256": source_hashes,
            "x-arete-oldest-client": oldest_client, "x-arete-aliases": {route(alias): alias for alias in registry["aliases"]},
            "x-arete-errors": errors}


def from_document(document):
    schemas = document["components"]["schemas"]
    operations = {}
    for path, methods in document["paths"].items():
        for method, entry in methods.items():
            name = entry["operationId"]
            if name in operations:
                raise ValueError(f"duplicate operation {name}")
            operations[name] = {"name": name, "path": path, "method": method.upper(),
                                "input": schemas[f"{name}.input"], "result": schemas[f"{name}.result"],
                                "errors": entry["x-arete-errors"],
                                **{k: entry[v] for k, v in (("deprecated", "deprecated"), ("sunset", "x-arete-sunset"))
                                   if v in entry}}
            content = entry.get("requestBody", {}).get("content", {})
            if content.keys() - {"application/json"}:
                operations[name]["body_media"], = content
    version = document["info"]["version"]
    if not re.fullmatch(r"1\.\d+", version):
        raise ValueError(f"invalid v1 version {version!r}")
    return {"minor": int(version[2:]), "oldest_client": document["x-arete-oldest-client"],
            "operations": operations, "aliases": document["x-arete-aliases"],
            "errors": {code: {"status": item["status"], "schema": schemas[f"problem.{code}"]}
                       for code, item in document["x-arete-errors"].items()}}


def git(root, *args):
    from . import environment

    env = environment.build((), {"GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_OPTIONAL_LOCKS": "0"})
    done = subprocess.run(["git", "--no-replace-objects", *args], cwd=root, env=env, capture_output=True, timeout=15)
    if done.returncode:
        raise ValueError(done.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return done.stdout


def baseline(root, revision):
    commit = git(root, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()

    def read(path):
        return git(root, "cat-file", "blob", f"{commit}:{path}")

    tracked = {entry.split(b"\t", 1)[1].decode() for entry in
               git(root, "ls-tree", "-z", commit, "--", OPENAPI, *PRODUCERS).split(b"\0") if entry}
    if OPENAPI not in tracked and sources(read, REGISTRY_INPUTS) == BOOTSTRAP_SOURCES:
        frozen = (root / BOOTSTRAP).read_bytes()
        if sha(frozen) != BOOTSTRAP_DATA_SHA:
            raise ValueError(f"pre-OpenAPI baseline {BOOTSTRAP} was modified")
        return commit, {**json.loads(frozen)["registry"], "oldest_client": BOOTSTRAP_OLDEST_CLIENT}
    try:
        document = json.loads(read(OPENAPI))
        source_hashes = sources(read, PRODUCERS if CONFIG_REGISTRY in tracked else REQUIRED_PRODUCERS)
        saved = document["x-arete-contract-sources-sha256"]
        if saved != source_hashes:
            changed = sorted(path for path in source_hashes if saved.get(path) != source_hashes[path])
            raise ValueError(f"{OPENAPI} is stale at {commit}: {', '.join(changed) or 'producer list changed'}")
        return commit, from_document(document)
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f"cannot read the contract at {revision}: {error}") from None


def types(schema):
    result = schema.get("type", ["object", "array", "string", "number", "boolean", "null"])
    result = {result} if isinstance(result, str) else set(result)
    return result | ({"integer"} if "number" in result else set())


def schema_breaks(old, new, at, *, result=False):
    if old == new:
        return
    earlier, later = (new, old) if result else (old, new)
    if "anyOf" in earlier or "anyOf" in later:
        for index, shape in enumerate(earlier.get("anyOf", [earlier])):
            location = f"{at}.anyOf[{index}]"
            rejections = []
            for candidate in later.get("anyOf", [later]):
                before, after = (candidate, shape) if result else (shape, candidate)
                problems = list(schema_breaks(before, after, location, result=result))
                if not problems:
                    break
                rejections.append((shape.get("type") != candidate.get("type"), problems))
            else:
                if rejections:
                    yield from min(rejections, key=lambda item: (item[0], len(item[1])))[1]
                else:
                    yield f"{location} no longer has a compatible shape"
        return
    if "$ref" in earlier or "$ref" in later:
        yield f"{at} schema reference changed"
        return
    if not types(earlier) <= types(later):
        yield f"{at} type changed from {old.get('type')} to {new.get('type')}"
    if "enum" in later and ("enum" not in earlier or
                            {wire_json(v) for v in earlier["enum"]} - {wire_json(v) for v in later["enum"]}):
        yield f"{at} enum changed incompatibly"
    if "pattern" in later and earlier.get("pattern") != later["pattern"]:
        yield f"{at} pattern changed incompatibly"
    for field in ("minimum", "minItems", "maximum", "maxItems"):
        minimum = field.startswith("min")
        default = float("-inf" if minimum else "inf")
        before, after = earlier.get(field, default), later.get(field, default)
        if (before < after if minimum else before > after):
            yield f"{at}.{field} changed incompatibly"
    if "array" in types(earlier):
        yield from schema_breaks(old.get("items", {}), new.get("items", {}), at + "[]", result=result)
    if "object" not in types(earlier):
        return
    previous, current = old.get("properties", {}), new.get("properties", {})
    old_required, new_required = set(old.get("required", [])), set(new.get("required", []))
    required = (old_required - new_required) if result else (new_required - old_required)
    if result:
        required |= (new_required - previous.keys())
    for field in sorted(required):
        yield f"{at}.{field} requiredness changed incompatibly"
    old_extra, new_extra = old.get("additionalProperties", True), new.get("additionalProperties", True)
    for field in sorted(previous.keys() | current.keys()):
        path = f"{at}.{field}"
        if field not in current and (result or new_extra is False):
            yield f"{path} removed"
        elif field in previous and field in current:
            yield from schema_breaks(previous[field], current[field], path, result=result)
        elif not result and field not in previous and old_extra is not False:
            yield from schema_breaks(old_extra if isinstance(old_extra, dict) else {}, current[field], path)
        elif not result and field not in current and isinstance(new_extra, dict):
            yield from schema_breaks(previous[field], new_extra, path)
        elif result and field not in previous and isinstance(old_extra, dict):
            yield from schema_breaks(old_extra, current[field], path, result=True)
    extra_from, extra_to = (new_extra, old_extra) if result else (old_extra, new_extra)
    if extra_from is not False:
        if extra_to is False:
            yield f"{at}.additionalProperties changed incompatibly"
        elif isinstance(extra_to, dict):
            before = old_extra if isinstance(old_extra, dict) else {}
            after = new_extra if isinstance(new_extra, dict) else {}
            yield from schema_breaks(before, after, at + ".additionalProperties", result=result)


def batch_io(op, operations):
    inputs, result = op["input"], op["result"]
    reads = {name for name, entry in operations.items() if entry["method"] == "GET"}
    select = inputs.get("properties", {}).get("requests", {}).get("items", {}).get("properties", {}).get("operation", {})
    if "enum" in select and set(select["enum"]) == reads:
        inputs = deepcopy(inputs)
        inputs["properties"]["requests"]["items"]["properties"]["operation"].pop("enum")
    items = result.get("properties", {}).get("results", {}).get("items", {})
    if "anyOf" in items and {wire_json(s) for s in items["anyOf"]} == {
            wire_json(ref(name + ".result")) for name in reads}:
        result = deepcopy(result)
        result["properties"]["results"].pop("items")
    return inputs, result


def _config_kinds_enum(operations):
    enum = operations.get("config.list", {}).get("input", {}).get("properties", {}).get("kind", {}).get("enum")
    return list(enum) if isinstance(enum, list) else None


def config_result(name, op, operations):
    result = op["result"]
    kinds = _config_kinds_enum(operations)
    if not kinds:
        return result
    props = result.get("properties", {})
    if name == "config.kinds":
        kind_schema = props.get("kinds", {}).get("items", {}).get("properties", {}).get("kind", {})
        if kind_schema.get("enum") == kinds:
            result = deepcopy(result)
            result["properties"]["kinds"]["items"]["properties"]["kind"].pop("enum")
    elif name == "config.list":
        kind_schema = props.get("items", {}).get("items", {}).get("properties", {}).get("kind", {})
        if kind_schema.get("enum") == kinds:
            result = deepcopy(result)
            result["properties"]["items"]["items"]["properties"]["kind"].pop("enum")
    elif name == "config.read":
        kind_schema = props.get("item", {}).get("properties", {}).get("kind", {})
        if kind_schema.get("enum") == kinds:
            result = deepcopy(result)
            result["properties"]["item"]["properties"]["kind"].pop("enum")
    return result


def _roles_enum(operations):
    roles = (
        operations.get("settings.resolved", {})
        .get("result", {})
        .get("properties", {})
        .get("roles", {})
        .get("items", {})
        .get("properties", {})
        .get("role", {})
        .get("enum")
    )
    return list(roles) if isinstance(roles, list) else None


def settings_result(name, op, operations):
    result = op["result"]
    roles = _roles_enum(operations)
    if not roles:
        return result
    result = deepcopy(result)
    props = result.get("properties", {})
    if name == "settings.resolved":
        role_schema = props.get("roles", {}).get("items", {}).get("properties", {}).get("role", {})
        if role_schema.get("enum") == roles:
            role_schema.pop("enum")
    elif name in ("settings.get", "settings.update"):
        s_props = props.get("settings", {}).get("properties", {})
        for holder in (s_props, s_props.get("values", {}).get("properties", {})):
            role_schema = holder.get("roles", {}).get("items", {}).get("properties", {}).get("role", {})
            if role_schema.get("enum") == roles:
                role_schema.pop("enum")
    return result


def compare(old, new):
    old_kinds, new_kinds = _config_kinds_enum(old["operations"]), _config_kinds_enum(new["operations"])
    additive_config_kinds = bool(old_kinds and new_kinds and set(old_kinds) <= set(new_kinds))
    old_roles, new_roles = _roles_enum(old["operations"]), _roles_enum(new["operations"])
    additive_roles = bool(old_roles and new_roles and set(old_roles) <= set(new_roles))
    for name, before in sorted(old["operations"].items()):
        after = new["operations"].get(name)
        identity = f"{name} ({route(before)})"
        if after is None or route(before) != route(after):
            yield before, f"{identity}: route removed" if after is None else f"{identity}: moved to {route(after)}"
            continue
        old_input, old_result = before["input"], before["result"]
        new_input, new_result = after["input"], after["result"]
        if name == "batch.run":
            old_input, old_result = batch_io(before, old["operations"])
            new_input, new_result = batch_io(after, new["operations"])
        elif additive_config_kinds and name in ("config.kinds", "config.list", "config.read"):
            old_result = config_result(name, before, old["operations"])
            new_result = config_result(name, after, new["operations"])
        elif additive_roles and name in ("settings.get", "settings.resolved", "settings.update"):
            old_result = settings_result(name, before, old["operations"])
            new_result = settings_result(name, after, new["operations"])
        changes = [*schema_breaks(old_input, new_input, "input"),
                   *schema_breaks(old_result, new_result, "result", result=True)]
        if before.get("body_media") != after.get("body_media"):
            changes.append("request body media type changed")
        for code in sorted(set(before["errors"]) | set(after["errors"])):
            error = f"errors.{code}"
            if code not in before["errors"]:
                if code in after["errors"] and code in new["errors"] and code not in old["errors"]:
                    continue
                changes.append(f"{error} declaration changed")
                continue
            if code not in after["errors"] or code not in new["errors"]:
                changes.append(f"{error} declaration changed")
                continue
            previous, current = old["errors"][code], new["errors"][code]
            if previous["status"] != current["status"]:
                changes.append(f"{error}.status changed from {previous['status']} to {current['status']}")
            changes.extend(schema_breaks(previous["schema"], current["schema"], error, result=True))
        for change in changes:
            yield before, f"{identity}: {change}"
    for key, before in sorted(old["aliases"].items()):
        after = new["aliases"].get(key)
        if after is None:
            yield before, f"alias {key}: route removed"
            continue
        for field in ("provider", "adapter", "transport"):
            if before.get(field) != after.get(field):
                yield before, f"alias {key}: {field} changed"
        for change in schema_breaks(before["input"], after["input"], "input"):
            yield before, f"alias {key}: {change}"
        for action, schema in sorted(before.get("actions", {}).items()):
            if action not in after.get("actions", {}):
                yield before, f"alias {key}: actions.{action} removed"
            else:
                for change in schema_breaks(schema, after["actions"][action], f"actions.{action}.input"):
                    yield before, f"alias {key}: {change}"


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed


def removal_blocks(entry, usage, approval, commit, candidate, now, *, batch_read):
    key = route(entry)
    if entry.get("deprecated") is not True:
        yield "route is not deprecated at REF"
    try:
        if timestamp(entry.get("sunset", "")) >= now:
            yield "route Sunset has not passed"
    except (TypeError, ValueError):
        yield "route has no valid Sunset at REF"
    record = usage.get(key, {})
    try:
        start, end = timestamp(record["from"]), timestamp(record["through"])
        if not start <= now - timedelta(days=7) or not now - timedelta(minutes=1) <= end <= now:
            yield "usage evidence does not cover the last seven days (refresh the observation through the current minute)"
        counts = [("requests", "direct")]
        if batch_read:
            counts.append(("batch_requests", "batch"))
        for field, label in counts:
            count = record.get(field)
            if type(count) is not int or count < 0:
                yield f"usage evidence needs an explicit {label} request count ({field})"
            elif count:
                yield f"usage evidence shows {count} {label} request(s)"
    except (KeyError, TypeError, ValueError):
        yield "missing seven-day usage evidence"
    routes = approval.get("routes")
    if (approval.get("approved") is not True or approval.get("base_commit") != commit
            or approval.get("contract_sha") != candidate or not isinstance(routes, list) or key not in routes):
        yield "missing explicit owner approval for this route, base commit and contract hash"


def evidence(file):
    data = json.loads(Path(file).read_bytes()) if file else {}
    if not isinstance(data, dict):
        raise ValueError(f"{file}: evidence must be a JSON object")
    return data


def inspect_diff(root, revision, usage, approval, *, check_artifact=False):
    registry = contract.publish()
    if invalid := list(registry_problems(registry)):
        raise ValueError("\n".join(invalid))
    hashes = sources(lambda path: (root / path).read_bytes())
    commit, old = baseline(root, revision)
    new = snapshot(registry, contract.MINOR, contract.OLDEST_CLIENT)
    artifact = (document_json(openapi(registry, contract.MINOR, hashes, contract.OLDEST_CLIENT)) + "\n").encode()
    path = root / OPENAPI
    if path.is_symlink() or not path.resolve().is_relative_to(root) or (path.exists() and not path.is_file()):
        raise ValueError(f"{OPENAPI} must be a regular file inside the checkout")
    refused, allowed = [], []
    present = path.read_bytes() if path.is_file() else b""
    if not check_artifact:
        present = present.replace(b"\r\n", b"\n")
    if present != artifact:
        if check_artifact:
            refused.append(f"Stage {OPENAPI} regenerated from the staged API contract.")
        else:
            path.write_bytes(artifact)
    now = datetime.now(UTC)
    candidate = contract.digest()
    if old["oldest_client"] != new["oldest_client"]:
        for client in sorted(old["oldest_client"].keys() | new["oldest_client"].keys()):
            before, after = old["oldest_client"].get(client), new["oldest_client"].get(client)
            if before != after:
                refused.append(f"oldest_client.{client} changed from {before!r} to {after!r}; v1 client minima are frozen")
    for entry, change in compare(old, new):
        batch_read = entry["method"] == "GET" and entry.get("name") in old["operations"]
        blocks = list(removal_blocks(entry, usage, approval, commit, candidate, now, batch_read=batch_read))
        if blocks:
            refused.append(f"{change}\n  {'; '.join(blocks)}")
        else:
            allowed.append(f"Approved: {change}")
    if new["minor"] < old["minor"]:
        refused.append(f"API minor cannot decrease from {old['minor']} to {new['minor']}")
    elif new["minor"] == old["minor"] and any(new[k] != old[k] for k in ("operations", "aliases", "errors")):
        refused.append(f"advance the API minor above {old['minor']} for this contract change")
    return refused, allowed, commit, candidate
