from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace

from . import config, owner_asks, prompt_loader, routing, steps
from .agent import BUILD_TOOLS, READ_TOOLS, traits_of
from .store.orchestration import digest

PROTOCOL = 1
MAX_BYTES = 64 * 1024
PROMPTS = {"delegate": ("build", True), "investigate": ("plan", False), "plan": ("plan", False),
           "build": ("build", True)}
BUNDLES = {"read": tuple(READ_TOOLS), "write": tuple(BUILD_TOOLS)}
OPTIONAL = object()


@dataclass(frozen=True)
class Operation:
    description: str
    fields: dict[str, tuple[type, object]]
    constraints: dict[str, dict] = field(default_factory=dict)
    schemas: dict[str, dict] = field(default_factory=dict)

    def arguments(self, value) -> dict:
        if not isinstance(value, dict) or value.keys() - self.fields.keys():
            raise ValueError("arguments contain unknown fields or are not an object")
        found = {}
        for name, (kind, default) in self.fields.items():
            if name not in value and default is OPTIONAL:
                raise ValueError(f"missing {name}")
            item = value.get(name, default)
            if type(item) is not kind:
                raise ValueError(f"{name} must be {kind.__name__}")
            if kind is str and (not item.strip() or len(item) > 16000):
                raise ValueError(f"{name} must be nonempty and at most 16000 characters")
            constraints = self.constraints.get(name, {})
            if "enum" in constraints and item not in constraints["enum"]:
                raise ValueError(f"{name} must be one of: {', '.join(constraints['enum'])}")
            if "minimum" in constraints and item < constraints["minimum"]:
                raise ValueError(f"{name} must be at least {constraints['minimum']}")
            if "maximum" in constraints and item > constraints["maximum"]:
                raise ValueError(f"{name} must be at most {constraints['maximum']}")
            found[name] = item
        return found

    def describe(self) -> dict:
        return {"description": self.description, "arguments": {
            name: {"type": kind.__name__, **({"required": True} if default is OPTIONAL else {"default": default}),
                   **self.constraints.get(name, {}), **({"schema": self.schemas[name]} if name in self.schemas else {})}
            for name, (kind, default) in self.fields.items()}}


def required(kind):
    return kind, OPTIONAL


OPERATIONS = {
    "catalog.list": Operation("Page approved entries; kinds are models, prompts, tools, context.",
                              {"kind": required(str), "cursor": (int, 0), "limit": (int, 10)},
                              {"kind": {"enum": ["models", "prompts", "tools", "context"]},
                               "cursor": {"minimum": 0}, "limit": {"minimum": 1, "maximum": 20}}),
    "catalog.describe": Operation("Describe catalog IDs or operation names without loading everything.",
                                  {"ids": required(list)}),
    "context.read": Operation("Read a bounded authorized context reference, never a filesystem path.",
                              {"ref": required(str), "offset": (int, 0), "limit": (int, 8000)},
                              {"offset": {"minimum": 0}, "limit": {"minimum": 1, "maximum": 16000}}),
    "run.observe": Operation("Observe actual action results and progress.", {"after": (int, 0), "limit": (int, 20)},
                             {"after": {"minimum": 0}, "limit": {"minimum": 1, "maximum": 20}}),
    "worker.start": Operation("Delegate a nonblocking goal; read/write capabilities cannot exceed the catalog.", {
        "goal": required(str), "prompt_id": (str, "delegate"), "inputs": (dict, {}), "route_id": required(str),
        "effort": required(str), "tool_bundle": (str, "write"), "context_refs": (list, []), "depends_on": (list, [])}),
    "worker.continue": Operation("Continue one completed worker, with a compatible session or explicit fresh handoff.",
                                 {"worker_id": required(str), "message": required(str), "settings": (dict, {})}),
    "worker.steer": Operation("Durably queue guidance for the next worker turn, not the current tool call.",
                              {"worker_id": required(str), "message": required(str)}),
    "worker.cancel": Operation("Stop a worker and confirm producer cleanup before releasing its lease.",
                               {"worker_id": required(str), "reason": required(str)}),
    "checks.run": Operation("Run owner-configured checks; full checks are required before publication.",
                            {"scope": (str, "full")}, {"scope": {"enum": ["quick", "full"]}}),
    "verification.run": Operation("One independent in-use attempt; never silently repairs implementation.",
                                   {"goal_refs": (list, []), "route_id": required(str), "effort": required(str)}),
    "review.run": Operation("One independent review of the current tree.",
                             {"context_refs": (list, []), "route_id": required(str), "effort": required(str)}),
    "owner.ask": Operation("Ask through the existing owner-question lifecycle. request must match its schema: "
                           "questions holds 1 to 4 questions and why says why you need the answer. "
                           "A rejected request names what to fix; send it again before finishing failed.",
                           {"request": required(dict)}, schemas={"request": owner_asks.SCHEMA}),
    "run.wait": Operation("Wait for worker completion, failure or a new progress report. For active work use a single "
                          "run.wait call with timeout_seconds=0 to follow the worker to its step or run deadline. "
                          "A positive timeout caps the wait further and omission defaults to 60 seconds.",
                          {"after": (int, 0), "timeout_seconds": (int, 60)},
                          {"after": {"minimum": 0}, "timeout_seconds": {"minimum": 0}}),
    "publication.request": Operation("Request existing engine publication; current evidence and authority are required.", {}),
    "run.finish": Operation("Finish failed, noop or delivered; delivered requires engine publication.",
                             {"reason": required(str), "evidence_refs": (list, [])},
                             {"reason": {"enum": ["failed", "noop", "delivered"]}}),
}


class Catalog:
    def __init__(self, job):
        self.job, self.entries, self.settings = job, {}, {}
        for role in ("plan", "build", "check_repair", "review_repair", "ci_repair", "verify", "review"):
            setting = job.run.settings(role)
            for n, link in enumerate(setting.chain):
                overrides = {**job.run.overrides, **job.run.step_overrides.get(role, {}),
                             "runner": link.runner, "provider": link.provider, "model": link.model,
                             "effort": link.effort}
                try:
                    selected = steps.resolve(job.cfg, job.layer, role, job.run.kind, overrides,
                                             job.run.step_overrides.get(config.INHERITS.get(role, ""), {}))
                except ValueError:
                    if link.requested:
                        raise
                    continue
                if role in ("verify", "review"):
                    selected = replace(selected, requires=replace(selected.requires, gated_launches=True))
                key = f"model:{role}:{n}"
                self.settings[key] = selected
                self.entries[key] = {"id": key, "kind": "models", "role": role, "runner": selected.runner,
                                     "model": selected.model, "efforts": list(traits_of(selected.runner).efforts),
                                     "fallback": selected.fallback, "price": None,
                                     "operations": ["verification.run" if role == "verify" else "review.run"]
                                     if role in ("verify", "review") else ["worker.start", "worker.continue"],
                                     "prompt_ids": [f"prompt:{name}" for name, (prompt_role, _) in PROMPTS.items()
                                                    if prompt_role == ("build" if role.endswith("_repair") else role)]}
        for name, (role, writing) in PROMPTS.items():
            template = prompt_loader.template("delegate" if name == "investigate" else name)
            key = f"prompt:{name}"
            self.entries[key] = {"id": key, "kind": "prompts", "role": role, "writing": writing,
                                 "inputs": template.get_identifiers(), "sha": digest(template.template)}
        for name, tools in BUNDLES.items():
            key = f"tools:{name}"
            self.entries[key] = {"id": key, "kind": "tools", "tools": list(tools)}
        self.context = {"task": f"{job.ticket.title}\n\n{job.ticket.body}\n\n{job.ticket.comments or ''}",
                        "expertise": job.expertise, "attachments": job.attachments,
                        "feedback": "\n".join(job.run.story)}
        self.fingerprint = digest(self.entries)

    def add_context(self, name: str, text: str) -> None:
        self.context[name] = text

    def list(self, kind: str, cursor: int, limit: int) -> dict:
        OPERATIONS["catalog.list"].arguments({"kind": kind, "cursor": cursor, "limit": limit})
        entries = ([{"id": ref, "kind": "context", "characters": len(text)} for ref, text in self.context.items()]
                   if kind == "context" else [entry for entry in self.entries.values() if entry["kind"] == kind])
        return {"entries": entries[cursor:cursor + limit],
                "next": cursor + limit if cursor + limit < len(entries) else None}

    def describe(self, ids: list) -> list[dict]:
        if not 1 <= len(ids) <= 10 or any(type(key) is not str for key in ids):
            raise ValueError("describe takes between 1 and 10 IDs")
        found = []
        for key in ids:
            if key in OPERATIONS:
                found.append({"id": key, **OPERATIONS[key].describe()})
            elif key in self.entries:
                entry = dict(self.entries[key])
                if entry["kind"] == "models":
                    setting = self.settings[key]
                    routes = [] if self.job.cfg.replay.all else routing.Discovery(
                        self.job.cfg, self.job.run.repo, "local", layer=self.job.layer).routes(setting)
                    entry["routes"] = [{"runner": r.runner, "model": r.model, "state": r.state,
                                        "detail": r.detail, "price": asdict(r.price) if r.price else None,
                                        "capabilities": r.capabilities} for r in routes]
                found.append(entry)
            else:
                raise ValueError(f"unknown catalog ID {key}")
        return found

    def refs(self, refs: list) -> None:
        if len(refs) > 20 or any(type(ref) is not str or ref not in self.context for ref in refs):
            raise ValueError("unknown context reference or too many references")

    def read(self, ref: str, offset: int, limit: int) -> dict:
        self.refs([ref])
        OPERATIONS["context.read"].arguments({"ref": ref, "offset": offset, "limit": limit})
        text = self.context[ref]
        return {"ref": ref, "text": text[offset:offset + limit], "characters": len(text),
                "next": offset + limit if offset + limit < len(text) else None}

    def route(self, key: str, effort: str, roles: tuple[str, ...]) -> steps.Settings:
        if key not in self.settings or self.settings[key].step not in roles:
            approved = [name for name, setting in self.settings.items() if setting.step in roles]
            raise ValueError(f"route {key!r} is not approved for roles {', '.join(roles)}; "
                             f"approved route IDs: {', '.join(approved) or 'none'}")
        setting = self.settings[key]
        if effort not in traits_of(setting.runner).efforts:
            raise ValueError("unsupported reasoning effort")
        resolved = steps.resolve(self.job.cfg, self.job.layer, setting.step, self.job.run.kind,
                                 {**self.job.run.overrides, **self.job.run.step_overrides.get(setting.step, {}),
                                  "runner": setting.runner, "provider": setting.provider, "model": setting.model,
                                  "effort": effort},
                                 self.job.run.step_overrides.get(config.INHERITS.get(setting.step, ""), {}))
        return replace(resolved, requires=replace(resolved.requires, gated_launches=setting.requires.gated_launches))

    def worker(self, request: dict) -> tuple[steps.Settings, str, bool]:
        name = request["prompt_id"].removeprefix("prompt:")
        if name not in PROMPTS or request["tool_bundle"] not in BUNDLES:
            raise ValueError("unknown worker prompt or tool bundle")
        role, writing = PROMPTS[name]
        if request["tool_bundle"] == "write" and not writing:
            raise ValueError("read-only prompts cannot receive writing tools")
        self.refs(request["context_refs"])
        if set(request["inputs"]) - {"plan", "verify", "estimate"}:
            raise ValueError("worker inputs cannot replace the issue, owner or engine permissions")
        if any(type(v) not in (str, int) for v in request["inputs"].values()):
            raise ValueError("worker inputs must be strings or integers")
        roles = (role, "check_repair", "review_repair", "ci_repair") if writing else (role,)
        return self.route(request["route_id"], request["effort"], roles), name, writing

    def validate(self, response, turn: str, version: int, known: dict[str, dict]) -> dict:
        if (not isinstance(response, dict) or set(response) != {"version", "decision_id", "state_version", "rationale", "calls"}
                or type(response["version"]) is not int or response["version"] != PROTOCOL
                or response["decision_id"] != turn or type(response["state_version"]) is not int
                or response["state_version"] != version or type(response["rationale"]) is not str
                or len(response["rationale"]) > 2000 or type(response["calls"]) is not list
                or not 1 <= len(response["calls"]) <= 8 or len(json.dumps(response)) > MAX_BYTES):
            raise ValueError("invalid decision envelope or stale state version")
        calls = []
        for call in response["calls"]:
            if (not isinstance(call, dict) or set(call) != {"op", "args"} or type(call["op"]) is not str
                    or call["op"] not in OPERATIONS):
                raise ValueError("unknown operation or malformed call")
            op = call["op"]
            args = OPERATIONS[op].arguments(call["args"])
            if op == "worker.start":
                args["tool_bundle"] = args["tool_bundle"].removeprefix("tools:")
                self.worker(args)
                if (len(args["depends_on"]) > 20 or any(type(dep) is not str or dep not in known
                                                        for dep in args["depends_on"])):
                    raise ValueError("dependencies must name existing actions, not future or cyclic work")
            if op in ("verification.run", "review.run"):
                self.route(args["route_id"], args["effort"], ("verify" if op.startswith("verification") else "review",))
                self.refs(args.get("context_refs", args.get("goal_refs", [])))
            if op.startswith("worker.") and op != "worker.start":
                prior = known.get(args["worker_id"], {})
                if prior.get("op") not in ("worker.start", "worker.continue"):
                    raise ValueError("unknown worker")
                if op == "worker.continue":
                    if (set(args["settings"]) - {"route_id", "effort"}
                            or any(type(value) is not str for value in args["settings"].values())):
                        raise ValueError("continuation settings may select only an approved route and effort")
                    if prior["state"] not in ("succeeded", "failed", "cancelled", "interrupted"):
                        raise ValueError("continue requires a quiescent worker")
                    effective = prior["result"].get("effective")
                    if not effective:
                        raise ValueError("the prior worker has no recoverable invocation settings")
                    self.worker({**effective, **args["settings"], "goal": args["message"]})
            if op == "catalog.describe":
                ids = args["ids"]
                if (not 1 <= len(ids) <= 10 or any(type(key) is not str or key not in self.entries
                                                and key not in OPERATIONS for key in ids)):
                    raise ValueError("describe needs between 1 and 10 known catalog or operation IDs")
            if op == "context.read":
                self.read(**args)
            if op == "owner.ask":
                _, errors = owner_asks.validate(args["request"])
                if errors:
                    raise ValueError("; ".join(errors))
            if op == "run.finish":
                self.refs(args["evidence_refs"])
            calls.append({"op": op, "args": args})
        return {**response, "calls": calls}


def help_text() -> str:
    return json.dumps({name: operation.describe() for name, operation in OPERATIONS.items()}, separators=(",", ":"))
