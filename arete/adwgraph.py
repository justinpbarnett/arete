from __future__ import annotations

import copy
import re
from dataclasses import dataclass

from . import adw_library, flows

WIRE_KINDS = frozenset(("pass", "fail", "retry", "data"))
CONDITIONS = frozenset(("documents", "dry_run", "repo.parts", "feedback"))
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TRIES_RE = re.compile(r"^setting:([a-z0-9_]+)\.tries$")
_CONTROL_KEYS = frozenset(("do", "end", "when", "then", "first", "repeat", "until", "min", "else", "finish", "code"))
_PASS_PINS = frozenset(("pass", "done"))
_FAIL_PINS = frozenset(("fail", "feedback"))
FINDING_CODES = frozenset((
    "type_mismatch",
    "flow_to_data",
    "cycle",
    "retry_no_tries",
    "missing_input",
    "dead_wire",
    "unreachable",
    "no_finish",
))


class GraphError(ValueError):
    pass


@dataclass(frozen=True)
class _Wire:
    src_id: str
    src_pin: str
    dst_id: str
    dst_pin: str
    kind: str
    tries: str | None = None
    min: int | None = None

    @property
    def ref(self) -> str:
        return f"{self.src_id}.{self.src_pin} -> {self.dst_id}.{self.dst_pin}"


def is_graph(data: object) -> bool:
    return isinstance(data, dict) and "node" in data and "steps" not in data


def _step_index(library: dict) -> dict[str, dict]:
    if not isinstance(library, dict):
        raise GraphError("library must be a mapping")
    raw = library.get("steps")
    if raw is None:
        raw = library.get("step")
    if not isinstance(raw, list):
        raise GraphError("library must provide a list of steps")
    out: dict[str, dict] = {}
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            out[entry["id"]] = entry
    return out


def _trigger_index(library: dict) -> dict[str, dict]:
    if not isinstance(library, dict):
        return {}
    raw = library.get("triggers")
    if raw is None:
        raw = library.get("trigger")
    if not isinstance(raw, list):
        return {}
    out: dict[str, dict] = {}
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            out[entry["id"]] = entry
    return out


def _trigger_out_pins(
    trig: dict,
    lib_triggers: dict[str, dict],
    source_types: dict[str, str] | None = None,
) -> dict[str, str]:
    ref = trig.get("ref")
    if isinstance(ref, str) and ref in lib_triggers:
        return {p["pin"]: p["type"] for p in lib_triggers[ref].get("out") or ()}
    kind = trig.get("kind")
    if kind == "manual" and "manual" in lib_triggers:
        return {p["pin"]: p["type"] for p in lib_triggers["manual"].get("out") or ()}
    if kind == "schedule" and "schedule" in lib_triggers:
        return {p["pin"]: p["type"] for p in lib_triggers["schedule"].get("out") or ()}
    if kind == "event":
        ev = trig.get("event")
        if isinstance(ev, str) and ev in lib_triggers:
            return {p["pin"]: p["type"] for p in lib_triggers[ev].get("out") or ()}
        if "loop-event" in lib_triggers:
            return {p["pin"]: p["type"] for p in lib_triggers["loop-event"].get("out") or ()}
    if kind == "source":
        sid = trig.get("source")
        stype = (source_types or {}).get(sid) if isinstance(sid, str) else None
        if stype and stype in lib_triggers:
            return {p["pin"]: p["type"] for p in lib_triggers[stype].get("out") or ()}
        if isinstance(sid, str) and sid in lib_triggers:
            return {p["pin"]: p["type"] for p in lib_triggers[sid].get("out") or ()}
        return {"task": "task", "event": "event"}
    return {}


def _parse_triggers(raw_triggers: object, nodes: dict[str, dict]) -> dict[str, dict]:
    if raw_triggers is None:
        return {}
    if not isinstance(raw_triggers, list):
        raise GraphError("trigger must be a list of tables")
    triggers: dict[str, dict] = {}
    for idx, entry in enumerate(raw_triggers):
        if not isinstance(entry, dict):
            raise GraphError(f"trigger[{idx}]: must be a table")
        tid = entry.get("id")
        if not isinstance(tid, str) or not _NODE_ID_RE.fullmatch(tid):
            raise GraphError(f"trigger[{idx}]: invalid trigger id {tid!r}")
        if tid in triggers:
            raise GraphError(f"trigger {tid!r}: duplicate trigger id")
        if tid in nodes:
            raise GraphError(f"trigger {tid!r}: conflicts with node id {tid!r}")
        triggers[tid] = entry
    return triggers


def validate_node_references(graph: dict, library: dict | None = None, *, where: str = "adw") -> list[str]:
    errors: list[str] = []
    lib = adw_library.load() if library is None else library
    steps = _step_index(lib)
    raw_nodes = graph.get("node")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        errors.append(f"{where}: graph requires at least one node")
        return errors
    seen: set[str] = set()
    for idx, entry in enumerate(raw_nodes):
        if not isinstance(entry, dict):
            errors.append(f"{where}.node[{idx}]: must be a table")
            continue
        nid = entry.get("id")
        if not isinstance(nid, str) or not _NODE_ID_RE.fullmatch(nid):
            errors.append(f"{where}.node[{idx}]: invalid node id {nid!r}")
            continue
        if nid in seen:
            errors.append(f"{where}: node {nid!r}: duplicate node id")
        else:
            seen.add(nid)
        if "step" in entry:
            sid = entry["step"]
            if not isinstance(sid, str) or sid not in steps:
                errors.append(f"{where}: node {nid!r}: step {sid!r} is not in the library")
            if "why" in entry or "detail" in entry:
                errors.append(f"{where}: node {nid!r}: 'why' and 'detail' are only allowed on finish nodes")
        elif "finish" in entry:
            if "end" in entry:
                errors.append(f"{where}: node {nid!r}: 'end' is not allowed on finish nodes")
        if "inputs" in entry and isinstance(entry["inputs"], dict):
            bad = sorted(set(entry["inputs"]) & _CONTROL_KEYS)
            if bad:
                errors.append(f"{where}: node {nid!r}: disallowed input keys: {', '.join(bad)}")
    return errors


def _split_endpoint(value: object, label: str, wire_ref: str) -> tuple[str, str]:
    if not isinstance(value, str) or value.count(".") != 1:
        raise GraphError(f"wire {wire_ref!r}: {label} must be '<node>.<pin>', got {value!r}")
    node_id, pin = value.split(".", 1)
    if not _NODE_ID_RE.fullmatch(node_id) or not _NODE_ID_RE.fullmatch(pin):
        raise GraphError(f"wire {wire_ref!r}: invalid {label} endpoint {value!r}")
    return node_id, pin


def _parse_nodes(raw_nodes: object, steps: dict[str, dict]) -> dict[str, dict]:
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise GraphError("graph requires at least one node")
    nodes: dict[str, dict] = {}
    for idx, entry in enumerate(raw_nodes):
        if not isinstance(entry, dict):
            raise GraphError(f"node[{idx}]: must be a table")
        nid = entry.get("id")
        if not isinstance(nid, str) or not _NODE_ID_RE.fullmatch(nid):
            raise GraphError(f"node[{idx}]: invalid node id {nid!r}")
        if nid in nodes:
            raise GraphError(f"node {nid!r}: duplicate node id")
        if type(entry.get("x")) is not int or type(entry.get("y")) is not int:
            raise GraphError(f"node {nid!r}: x and y must be integers")
        has_step = "step" in entry
        has_finish = "finish" in entry
        if has_step == has_finish:
            raise GraphError(f"node {nid!r}: must set exactly one of 'step' or 'finish'")
        if has_step:
            sid = entry["step"]
            if not isinstance(sid, str) or sid not in steps:
                raise GraphError(f"node {nid!r}: step {sid!r} is not in the library")
            if "why" in entry or "detail" in entry:
                raise GraphError(f"node {nid!r}: 'why' and 'detail' are only allowed on finish nodes")
        else:
            ending = entry["finish"]
            if not isinstance(ending, str) or not ending.strip():
                raise GraphError(f"node {nid!r}: finish must be a non-empty string")
            if "end" in entry:
                raise GraphError(f"node {nid!r}: 'end' is not allowed on finish nodes")
        if "when" in entry and (not isinstance(entry["when"], str) or entry["when"] not in CONDITIONS):
            raise GraphError(f"node {nid!r}: unknown condition {entry['when']!r}")
        if "end" in entry and type(entry["end"]) is not bool:
            raise GraphError(f"node {nid!r}: 'end' must be a boolean")
        if "inputs" in entry:
            inputs = entry["inputs"]
            if not isinstance(inputs, dict):
                raise GraphError(f"node {nid!r}: 'inputs' must be a table")
            bad = sorted(set(inputs) & _CONTROL_KEYS)
            if bad:
                raise GraphError(f"node {nid!r}: disallowed input keys: {', '.join(bad)}")
        nodes[nid] = entry
    return nodes


def _parse_wires(
    raw_wires: object,
    nodes: dict[str, dict],
    steps: dict[str, dict],
    triggers: dict[str, dict] | None = None,
    lib_triggers: dict[str, dict] | None = None,
    source_types: dict[str, str] | None = None,
) -> list[_Wire]:
    if raw_wires is None:
        return []
    if not isinstance(raw_wires, list):
        raise GraphError("wire must be a list of tables")
    trigs = triggers or {}
    lib_trigs = lib_triggers or {}
    wires: list[_Wire] = []
    for idx, entry in enumerate(raw_wires):
        if not isinstance(entry, dict):
            raise GraphError(f"wire[{idx}]: must be a table")
        raw_from = entry.get("from")
        raw_to = entry.get("to")
        wire_ref = f"{raw_from} -> {raw_to}"
        src_id, src_pin = _split_endpoint(raw_from, "from", wire_ref)
        dst_id, dst_pin = _split_endpoint(raw_to, "to", wire_ref)
        if src_id not in nodes and src_id not in trigs:
            raise GraphError(f"wire {wire_ref!r}: unknown source node {src_id!r}")
        if dst_id not in nodes:
            raise GraphError(f"wire {wire_ref!r}: unknown target node {dst_id!r}")
        kind = entry.get("kind")
        if not isinstance(kind, str) or kind not in WIRE_KINDS:
            raise GraphError(f"wire {wire_ref!r}: unknown wire kind {kind!r}")
        tries: str | None = None
        min_tries: int | None = None
        if kind == "retry":
            raw_tries = entry.get("tries")
            match = _TRIES_RE.fullmatch(raw_tries) if isinstance(raw_tries, str) else None
            if not match or match.group(1) not in flows.SETTABLE:
                raise GraphError(f"wire {wire_ref!r}: retry wire requires valid tries setting, got {raw_tries!r}")
            tries = raw_tries
            if "min" in entry:
                if type(entry["min"]) is not int or entry["min"] < 0:
                    raise GraphError(f"wire {wire_ref!r}: min must be a non-negative integer")
                min_tries = entry["min"]
        elif "tries" in entry or "min" in entry:
            raise GraphError(f"wire {wire_ref!r}: 'tries' and 'min' are only valid on retry wires")

        dst_node = nodes[dst_id]
        if src_id in trigs:
            if kind == "data":
                src_pins = set(_trigger_out_pins(trigs[src_id], lib_trigs, source_types))
                if src_pin not in src_pins:
                    raise GraphError(f"wire {wire_ref!r}: trigger {src_id!r} has no output pin {src_pin!r}")
                if "step" in dst_node:
                    dst_pins = {p["pin"] for p in steps[dst_node["step"]]["in"]}
                    if dst_pin not in dst_pins:
                        raise GraphError(f"wire {wire_ref!r}: node {dst_id!r} has no input pin {dst_pin!r}")
                elif dst_pin == "in" or dst_pin in _CONTROL_KEYS:
                    raise GraphError(f"wire {wire_ref!r}: invalid finish input pin {dst_pin!r} on node {dst_id!r}")
            else:
                if dst_pin != "in":
                    raise GraphError(f"wire {wire_ref!r}: flow wire target pin must be 'in', got {dst_pin!r}")
                if kind != "pass" or src_pin not in _PASS_PINS:
                    raise GraphError(f"wire {wire_ref!r}: trigger {src_id!r} only supports pass wires from 'pass' or 'done'")
        else:
            src_node = nodes[src_id]
            if kind == "data":
                if "finish" in src_node:
                    raise GraphError(f"wire {wire_ref!r}: finish node {src_id!r} has no data output pins")
                src_pins = {p["pin"] for p in steps[src_node["step"]]["out"]}
                if src_pin not in src_pins:
                    raise GraphError(f"wire {wire_ref!r}: node {src_id!r} has no output pin {src_pin!r}")
                if "step" in dst_node:
                    dst_pins = {p["pin"] for p in steps[dst_node["step"]]["in"]}
                    if dst_pin not in dst_pins:
                        raise GraphError(f"wire {wire_ref!r}: node {dst_id!r} has no input pin {dst_pin!r}")
                elif dst_pin == "in" or dst_pin in _CONTROL_KEYS:
                    raise GraphError(f"wire {wire_ref!r}: invalid finish input pin {dst_pin!r} on node {dst_id!r}")
            else:
                if dst_pin != "in":
                    raise GraphError(f"wire {wire_ref!r}: flow wire target pin must be 'in', got {dst_pin!r}")
                if "finish" in src_node:
                    if "when" not in src_node:
                        raise GraphError(f"wire {wire_ref!r}: unconditional finish node {src_id!r} cannot have outgoing wires")
                    if kind != "pass" or src_pin not in _PASS_PINS:
                        raise GraphError(f"wire {wire_ref!r}: conditional finish node {src_id!r} only supports pass wires")
                else:
                    outcomes = set(steps[src_node["step"]]["outcomes"])
                    if src_pin not in outcomes:
                        raise GraphError(f"wire {wire_ref!r}: node {src_id!r} has no outcome pin {src_pin!r}")
                    if kind == "pass" and src_pin not in _PASS_PINS:
                        raise GraphError(f"wire {wire_ref!r}: pass wire must leave 'pass' or 'done', got {src_pin!r}")
                    if kind in ("fail", "retry") and src_pin not in _FAIL_PINS:
                        raise GraphError(f"wire {wire_ref!r}: {kind} wire must leave 'fail' or 'feedback', got {src_pin!r}")

        wires.append(_Wire(src_id=src_id, src_pin=src_pin, dst_id=dst_id, dst_pin=dst_pin,
                           kind=kind, tries=tries, min=min_tries))
    return wires


def _is_plain_finish_last(node: dict) -> bool:
    return (
        node.get("finish") == "last"
        and "when" not in node
        and not node.get("inputs")
        and "why" not in node
        and "detail" not in node
    )


def _find_start(
    nodes: dict[str, dict],
    in_flow: dict[str, list[_Wire]],
    trigger_targets: list[str] | None = None,
) -> str:
    if trigger_targets:
        unique = list(dict.fromkeys(trigger_targets))
        if len(unique) == 1:
            return unique[0]
        raise GraphError(f"graph triggers target multiple start nodes: {', '.join(unique)}")
    zero_all = [nid for nid in nodes if not in_flow[nid]]
    if len(zero_all) == 1:
        return zero_all[0]
    if len(zero_all) > 1:
        raise GraphError(f"graph has multiple start nodes: {', '.join(zero_all)}")
    zero_non_retry = [nid for nid in nodes if not any(w.kind != "retry" for w in in_flow[nid])]
    if len(zero_non_retry) == 1:
        return zero_non_retry[0]
    if len(zero_non_retry) > 1:
        raise GraphError(f"graph has multiple start nodes: {', '.join(zero_non_retry)}")
    raise GraphError(f"graph has no start node (flow cycle involving {next(iter(nodes))!r})")


def compile(graph: dict, library: dict | None = None, *, source_types: dict[str, str] | None = None) -> list[dict]:
    if not isinstance(graph, dict):
        raise GraphError("graph must be a table")
    if "steps" in graph:
        raise GraphError("graph cannot include both 'node' and 'steps'")
    lib = adw_library.load() if library is None else library
    steps = _step_index(lib)
    lib_triggers = _trigger_index(lib)
    nodes = _parse_nodes(graph.get("node"), steps)
    triggers = _parse_triggers(graph.get("trigger"), nodes)
    wires = _parse_wires(graph.get("wire"), nodes, steps, triggers, lib_triggers, source_types)

    pass_out: dict[str, _Wire] = {}
    fail_out: dict[str, _Wire] = {}
    retry_out: dict[str, _Wire] = {}
    in_flow: dict[str, list[_Wire]] = {nid: [] for nid in nodes}
    in_data: dict[str, list[_Wire]] = {nid: [] for nid in nodes}
    trigger_targets: list[str] = []

    for wire in wires:
        if wire.kind == "data":
            in_data[wire.dst_id].append(wire)
            continue
        if wire.src_id in triggers:
            trigger_targets.append(wire.dst_id)
            continue
        in_flow[wire.dst_id].append(wire)
        table = pass_out if wire.kind == "pass" else fail_out if wire.kind == "fail" else retry_out
        if wire.src_id in table:
            raise GraphError(f"node {wire.src_id!r}: multiple outgoing {wire.kind!r} wires")
        table[wire.src_id] = wire

    start_id = _find_start(nodes, in_flow, trigger_targets)
    visited_all: set[str] = set()

    def build_args(node: dict, repeat_head: str | None) -> dict[str, object]:
        args: dict[str, object] = {}
        if "inputs" in node:
            for key, value in node["inputs"].items():
                args[key] = copy.deepcopy(value)
        if "why" in node:
            args["why"] = node["why"]
        if "detail" in node:
            args["detail"] = node["detail"]
        for wire in in_data[node["id"]]:
            if repeat_head is not None and wire.src_id == repeat_head and wire.src_pin == "feedback":
                if wire.dst_pin in args and args[wire.dst_pin] != "@last":
                    raise GraphError(f"wire {wire.ref!r}: conflicts with fixed input {wire.dst_pin!r} on node {node['id']!r}")
                args[wire.dst_pin] = "@last"
        return args

    def build_do(node: dict, *, repeat_head: str | None, is_repeat_first: bool, force_end: bool = False) -> dict:
        entry: dict[str, object] = {}
        if not is_repeat_first and "when" in node:
            entry["when"] = node["when"]
        entry["do"] = node["step"]
        entry.update(build_args(node, repeat_head))
        if not is_repeat_first:
            if force_end:
                entry["end"] = True
            elif "end" in node:
                if node["end"]:
                    entry["end"] = True
            elif "fail" in steps[node["step"]]["outcomes"]:
                entry["end"] = True
        return entry

    def build_finish(node: dict, *, repeat_head: str | None) -> dict:
        entry: dict[str, object] = {}
        if "when" in node:
            entry["when"] = node["when"]
        entry["finish"] = node["finish"]
        entry.update(build_args(node, repeat_head))
        return entry

    def compile_chain(
        current: str | None,
        *,
        stop_at: str | None,
        active_heads: frozenset[str],
        repeat_head: str | None,
    ) -> tuple[list[dict], bool]:
        entries: list[dict] = []
        seen: set[str] = set()
        while current is not None:
            if current == stop_at:
                return entries, True
            if current in seen:
                raise GraphError(f"node {current!r}: cycle in flow wires without a retry wire")
            seen.add(current)
            visited_all.add(current)
            node = nodes[current]

            if current in retry_out:
                if current in active_heads:
                    raise GraphError(f"node {current!r}: cycle back to active repeat head")
                rwire = retry_out[current]
                if rwire.dst_id == current:
                    raise GraphError(f"wire {rwire.ref!r}: retry wire cannot target its own node {current!r}")
                first_entry = build_do(node, repeat_head=current, is_repeat_first=True)
                next_active = active_heads | {current}
                body, closed = compile_chain(
                    rwire.dst_id,
                    stop_at=current,
                    active_heads=next_active,
                    repeat_head=current,
                )
                if not closed:
                    raise GraphError(f"wire {rwire.ref!r}: retry path from {rwire.dst_id!r} does not return to {current!r}")
                if not body:
                    raise GraphError(f"wire {rwire.ref!r}: retry body is empty")
                repeat_list: list[dict] = (
                    [{"when": "feedback", "then": body}] if rwire.src_pin == "feedback" else body
                )
                repeat_entry: dict[str, object] = {
                    "until": "passed",
                    "tries": rwire.tries,
                }
                if rwire.min is not None:
                    repeat_entry["min"] = rwire.min
                if "when" in node:
                    repeat_entry["when"] = node["when"]
                repeat_entry["first"] = first_entry
                repeat_entry["repeat"] = repeat_list
                next_after = pass_out[current].dst_id if current in pass_out else None
                if current in fail_out:
                    fwire = fail_out[current]
                    else_body, _ = compile_chain(
                        fwire.dst_id,
                        stop_at=next_after,
                        active_heads=next_active,
                        repeat_head=current,
                    )
                    if not else_body:
                        raise GraphError(f"wire {fwire.ref!r}: fail branch is empty")
                    repeat_entry["else"] = else_body
                entries.append(repeat_entry)
                current = next_after
                continue

            if "finish" in node:
                entries.append(build_finish(node, repeat_head=repeat_head))
                current = pass_out[current].dst_id if ("when" in node and current in pass_out) else None
                continue

            force_end = False
            if current in fail_out:
                fwire = fail_out[current]
                ftarget = nodes[fwire.dst_id]
                if _is_plain_finish_last(ftarget):
                    force_end = True
                    visited_all.add(fwire.dst_id)
                else:
                    raise GraphError(
                        f"wire {fwire.ref!r}: fail branch from non-retry node {current!r} cannot compile to step grammar"
                    )
            entries.append(build_do(node, repeat_head=repeat_head, is_repeat_first=False, force_end=force_end))
            current = pass_out[current].dst_id if current in pass_out else None
        return entries, False

    compiled, _ = compile_chain(start_id, stop_at=None, active_heads=frozenset(), repeat_head=None)
    if unreachable := [nid for nid in nodes if nid not in visited_all]:
        raise GraphError(f"node {unreachable[0]!r}: unreachable from start node {start_id!r}")
    return compiled


def _finding(
    code: str,
    node: str,
    pin: str | None,
    message: str,
    *,
    from_node: str | None = None,
    from_pin: str | None = None,
    to_node: str | None = None,
    to_pin: str | None = None,
    wire: str | None = None,
) -> dict[str, object]:
    out: dict[str, object] = {"code": code, "node": node, "pin": pin, "message": message}
    if from_node is not None:
        out["from_node"] = from_node
    if from_pin is not None:
        out["from_pin"] = from_pin
    if to_node is not None:
        out["to_node"] = to_node
    if to_pin is not None:
        out["to_pin"] = to_pin
    if wire is not None:
        out["wire"] = wire
    return out


def _find_non_retry_cycles(nodes: dict[str, dict], adj: dict[str, list[str]]) -> list[list[str]]:
    color: dict[str, int] = dict.fromkeys(nodes, 0)
    stack: list[str] = []
    cycles: list[list[str]] = []
    seen_sets: set[frozenset[str]] = set()

    def dfs(u: str) -> None:
        color[u] = 1
        stack.append(u)
        for v in adj[u]:
            if color[v] == 1:
                idx = stack.index(v)
                cyc = [*stack[idx:], v]
                key = frozenset(cyc[:-1])
                if key not in seen_sets:
                    seen_sets.add(key)
                    cycles.append(cyc)
            elif color[v] == 0:
                dfs(v)
        stack.pop()
        color[u] = 2

    for nid in nodes:
        if color[nid] == 0:
            dfs(nid)
    return cycles


def _src_pins(
    endpoint_id: str,
    nodes: dict[str, dict],
    triggers: dict[str, dict],
    steps: dict[str, dict],
    lib_triggers: dict[str, dict],
    source_types: dict[str, str] | None,
) -> tuple[set[str], dict[str, str]]:
    if endpoint_id in triggers:
        return set(_PASS_PINS), _trigger_out_pins(triggers[endpoint_id], lib_triggers, source_types)
    node = nodes[endpoint_id]
    if "finish" in node:
        return (set(_PASS_PINS) if "when" in node else set()), {}
    step_def = steps.get(node.get("step", ""))
    if not step_def:
        return set(), {}
    return set(step_def["outcomes"]), {p["pin"]: p["type"] for p in step_def["out"]}


def _dst_pins(endpoint_id: str, nodes: dict[str, dict], steps: dict[str, dict]) -> tuple[set[str], dict[str, str] | None]:
    node = nodes[endpoint_id]
    if "finish" in node:
        return {"in"}, None
    step_def = steps.get(node.get("step", ""))
    if not step_def:
        return {"in"}, {}
    return {"in"}, {p["pin"]: p["type"] for p in step_def["in"]}


def check(
    graph: dict,
    library: dict | None = None,
    *,
    source_types: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    lib = adw_library.load() if library is None else library
    steps = _step_index(lib)
    lib_triggers = _trigger_index(lib)
    nodes = {
        n["id"]: n
        for n in (graph.get("node") or ())
        if isinstance(n, dict) and isinstance(n.get("id"), str)
    }
    triggers = {
        t["id"]: t
        for t in (graph.get("trigger") or ())
        if isinstance(t, dict) and isinstance(t.get("id"), str)
    }
    findings: list[dict[str, object]] = []
    valid_flow_wires: list[_Wire] = []
    wired_data_inputs: dict[str, set[str]] = {nid: set() for nid in nodes}

    for entry in graph.get("wire") or ():
        if not isinstance(entry, dict):
            continue
        raw_from = entry.get("from")
        raw_to = entry.get("to")
        wire_ref = f"{raw_from} -> {raw_to}"
        if not isinstance(raw_from, str) or raw_from.count(".") != 1 or not isinstance(raw_to, str) or raw_to.count(".") != 1:
            findings.append(_finding("dead_wire", str(raw_from or ""), None, f"wire {wire_ref}: malformed endpoint", wire=wire_ref))
            continue
        src_id, src_pin = raw_from.split(".", 1)
        dst_id, dst_pin = raw_to.split(".", 1)
        wmeta = {"from_node": src_id, "from_pin": src_pin, "to_node": dst_id, "to_pin": dst_pin, "wire": wire_ref}
        src_exists = src_id in nodes or src_id in triggers
        dst_exists = dst_id in nodes
        if not src_exists:
            findings.append(_finding("dead_wire", src_id, src_pin, f"wire {wire_ref}: source node {src_id!r} does not exist", **wmeta))
            continue
        if not dst_exists:
            findings.append(_finding("dead_wire", dst_id, dst_pin, f"wire {wire_ref}: target node {dst_id!r} does not exist", **wmeta))
            continue

        kind = str(entry.get("kind") or "")
        if kind == "retry":
            raw_tries = entry.get("tries")
            match = _TRIES_RE.fullmatch(raw_tries) if isinstance(raw_tries, str) else None
            if not match or match.group(1) not in flows.SETTABLE:
                findings.append(_finding(
                    "retry_no_tries", src_id, src_pin,
                    f"wire {wire_ref}: retry wire requires a valid 'tries' setting", **wmeta,
                ))

        src_flow_outs, src_data_outs = _src_pins(src_id, nodes, triggers, steps, lib_triggers, source_types)
        dst_flow_ins, dst_data_ins = _dst_pins(dst_id, nodes, steps)

        if kind == "data":
            src_type: str | None = None
            dst_type: str | None = None
            if src_pin in src_data_outs:
                src_type = src_data_outs[src_pin]
            elif src_pin in src_flow_outs:
                findings.append(_finding(
                    "flow_to_data", src_id, src_pin,
                    f"wire {wire_ref}: data wire cannot start at flow pin {src_id}.{src_pin}", **wmeta,
                ))
            else:
                findings.append(_finding(
                    "dead_wire", src_id, src_pin,
                    f"wire {wire_ref}: node {src_id!r} has no output pin {src_pin!r}", **wmeta,
                ))

            if dst_pin in dst_flow_ins:
                findings.append(_finding(
                    "flow_to_data", dst_id, dst_pin,
                    f"wire {wire_ref}: data wire cannot target flow pin {dst_id}.{dst_pin}", **wmeta,
                ))
            elif dst_data_ins is None:
                if dst_pin in _CONTROL_KEYS:
                    findings.append(_finding(
                        "dead_wire", dst_id, dst_pin,
                        f"wire {wire_ref}: finish node {dst_id!r} has no input pin {dst_pin!r}", **wmeta,
                    ))
                else:
                    dst_type = "any"
            elif dst_pin in dst_data_ins:
                dst_type = dst_data_ins[dst_pin]
                wired_data_inputs[dst_id].add(dst_pin)
            else:
                findings.append(_finding(
                    "dead_wire", dst_id, dst_pin,
                    f"wire {wire_ref}: node {dst_id!r} has no input pin {dst_pin!r}", **wmeta,
                ))

            if src_type is not None and dst_type is not None and src_type != dst_type and src_type != "any" and dst_type != "any":
                findings.append(_finding(
                    "type_mismatch", dst_id, dst_pin,
                    f"wire {wire_ref}: type mismatch between {src_id}.{src_pin} ({src_type}) and {dst_id}.{dst_pin} ({dst_type})",
                    **wmeta,
                ))
            continue

        pin_ok = True
        if src_pin in src_flow_outs:
            if kind == "pass" and src_pin not in _PASS_PINS:
                findings.append(_finding(
                    "dead_wire", src_id, src_pin,
                    f"wire {wire_ref}: pass wire must leave 'pass' or 'done', got {src_pin!r}", **wmeta,
                ))
                pin_ok = False
            elif kind in ("fail", "retry") and src_pin not in _FAIL_PINS:
                findings.append(_finding(
                    "dead_wire", src_id, src_pin,
                    f"wire {wire_ref}: {kind} wire must leave 'fail' or 'feedback', got {src_pin!r}", **wmeta,
                ))
                pin_ok = False
        elif src_pin in src_data_outs:
            findings.append(_finding(
                "flow_to_data", src_id, src_pin,
                f"wire {wire_ref}: {kind} flow wire cannot start at data pin {src_id}.{src_pin}", **wmeta,
            ))
            pin_ok = False
        else:
            findings.append(_finding(
                "dead_wire", src_id, src_pin,
                f"wire {wire_ref}: node {src_id!r} has no outcome pin {src_pin!r}", **wmeta,
            ))
            pin_ok = False

        if dst_pin in dst_flow_ins:
            pass
        elif dst_data_ins is not None and dst_pin in dst_data_ins:
            findings.append(_finding(
                "flow_to_data", dst_id, dst_pin,
                f"wire {wire_ref}: {kind} flow wire cannot target data pin {dst_id}.{dst_pin}", **wmeta,
            ))
            pin_ok = False
        else:
            findings.append(_finding(
                "dead_wire", dst_id, dst_pin,
                f"wire {wire_ref}: node {dst_id!r} has no flow input pin {dst_pin!r}", **wmeta,
            ))
            pin_ok = False

        if pin_ok:
            valid_flow_wires.append(_Wire(
                src_id=src_id, src_pin=src_pin, dst_id=dst_id, dst_pin=dst_pin,
                kind=kind, tries=entry.get("tries") if isinstance(entry.get("tries"), str) else None,
                min=entry.get("min") if type(entry.get("min")) is int else None,
            ))

    for nid, node in nodes.items():
        if "step" not in node:
            continue
        step_def = steps.get(node["step"])
        if not step_def:
            continue
        fixed = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        for pin_entry in step_def["in"]:
            pin_name = pin_entry["pin"]
            if pin_name not in wired_data_inputs[nid] and pin_name not in fixed:
                findings.append(_finding(
                    "missing_input", nid, pin_name,
                    f"node {nid!r}: input pin {pin_name!r} ({pin_entry['type']}) has no wire and no fixed value",
                ))

    non_retry_adj: dict[str, list[str]] = {nid: [] for nid in nodes}
    all_flow_adj: dict[str, list[str]] = {nid: [] for nid in nodes}
    pass_adj: dict[str, list[str]] = {nid: [] for nid in nodes}
    node_in_non_retry: dict[str, int] = dict.fromkeys(nodes, 0)
    for wire in valid_flow_wires:
        if wire.src_id in nodes and wire.dst_id in nodes:
            all_flow_adj[wire.src_id].append(wire.dst_id)
            if wire.kind != "retry":
                non_retry_adj[wire.src_id].append(wire.dst_id)
                node_in_non_retry[wire.dst_id] += 1
            if wire.kind == "pass":
                pass_adj[wire.src_id].append(wire.dst_id)

    cycles = _find_non_retry_cycles(nodes, non_retry_adj)
    for cyc in cycles:
        findings.append(_finding(
            "cycle", cyc[0], "in",
            f"cycle in flow wires without a retry wire: {' -> '.join(cyc)}",
        ))

    trigger_targets = [w.dst_id for w in valid_flow_wires if w.src_id in triggers and w.dst_id in nodes]
    if trigger_targets:
        roots = list(dict.fromkeys(trigger_targets))
    else:
        zero_in = [nid for nid in nodes if node_in_non_retry[nid] == 0]
        if len(zero_in) == 1:
            roots = zero_in
        elif len(zero_in) > 1:
            connected = [nid for nid in zero_in if all_flow_adj[nid] or "finish" in nodes[nid]]
            roots = [connected[0]] if connected else [zero_in[0]]
        else:
            roots = []

    reachable: set[str] = set()
    queue = list(roots)
    while queue:
        cur = queue.pop(0)
        if cur in reachable:
            continue
        reachable.add(cur)
        queue.extend(all_flow_adj[cur])

    for nid in nodes:
        if nid not in reachable:
            findings.append(_finding(
                "unreachable", nid, "in",
                f"node {nid!r} is unreachable from any trigger or flow wire",
            ))

    def is_uncond_finish(nid: str) -> bool:
        return "finish" in nodes[nid] and "when" not in nodes[nid]

    no_finish_emitted = False
    for nid in nodes:
        if nid in reachable and not is_uncond_finish(nid) and not pass_adj[nid]:
            findings.append(_finding(
                "no_finish", nid, "pass",
                f"node {nid!r}: path can end here without a finish node",
            ))
            no_finish_emitted = True

    if not no_finish_emitted and roots and not cycles:
        for root in roots:
            seen_spine: set[str] = set()
            cur: str | None = root
            finished = False
            while cur is not None and cur not in seen_spine:
                seen_spine.add(cur)
                if is_uncond_finish(cur):
                    finished = True
                    break
                cur = pass_adj[cur][0] if pass_adj[cur] else None
            if not finished:
                findings.append(_finding(
                    "no_finish", root, "pass",
                    f"node {root!r}: path can end without a finish node",
                ))

    return findings
