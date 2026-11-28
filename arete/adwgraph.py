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


def _parse_wires(raw_wires: object, nodes: dict[str, dict], steps: dict[str, dict]) -> list[_Wire]:
    if raw_wires is None:
        return []
    if not isinstance(raw_wires, list):
        raise GraphError("wire must be a list of tables")
    wires: list[_Wire] = []
    for idx, entry in enumerate(raw_wires):
        if not isinstance(entry, dict):
            raise GraphError(f"wire[{idx}]: must be a table")
        raw_from = entry.get("from")
        raw_to = entry.get("to")
        wire_ref = f"{raw_from} -> {raw_to}"
        src_id, src_pin = _split_endpoint(raw_from, "from", wire_ref)
        dst_id, dst_pin = _split_endpoint(raw_to, "to", wire_ref)
        if src_id not in nodes:
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

        src_node = nodes[src_id]
        dst_node = nodes[dst_id]
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


def _find_start(nodes: dict[str, dict], in_flow: dict[str, list[_Wire]]) -> str:
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


def compile(graph: dict, library: dict | None = None) -> list[dict]:
    if not isinstance(graph, dict):
        raise GraphError("graph must be a table")
    if "steps" in graph:
        raise GraphError("graph cannot include both 'node' and 'steps'")
    steps = _step_index(adw_library.load() if library is None else library)
    nodes = _parse_nodes(graph.get("node"), steps)
    wires = _parse_wires(graph.get("wire"), nodes, steps)

    pass_out: dict[str, _Wire] = {}
    fail_out: dict[str, _Wire] = {}
    retry_out: dict[str, _Wire] = {}
    in_flow: dict[str, list[_Wire]] = {nid: [] for nid in nodes}
    in_data: dict[str, list[_Wire]] = {nid: [] for nid in nodes}

    for wire in wires:
        if wire.kind == "data":
            in_data[wire.dst_id].append(wire)
            continue
        in_flow[wire.dst_id].append(wire)
        table = pass_out if wire.kind == "pass" else fail_out if wire.kind == "fail" else retry_out
        if wire.src_id in table:
            raise GraphError(f"node {wire.src_id!r}: multiple outgoing {wire.kind!r} wires")
        table[wire.src_id] = wire

    start_id = _find_start(nodes, in_flow)
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
