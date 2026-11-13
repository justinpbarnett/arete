from __future__ import annotations

import json
import re
import sys
from typing import TextIO

from . import outbound, paths

TOOL = "ask_owner"
SERVER = "arete"
RULE = f"mcp__{SERVER}__{TOOL}"
SETTLE_AT = 0.9
KINDS = ("choice", "text", "secret", "done")
MAX_QUESTIONS, MAX_HEADER, MAX_STEPS = 4, 12, 10
SENT = ("Sent to the owner. Finish what does not depend on the answer, then end your turn. "
        "This session resumes with the answers.")
NOBODY = "Nobody will answer questions while you work."
GUIDE = ("Ask your owner through the `ask_owner` tool only for what you cannot finish without and cannot settle from the "
         "ticket, the repo or a quick read: a secret or an account, a setup outside the work folder, a choice that is "
         "not safe to assume, or an action only the owner can take. Do all the work that does not depend on the answer "
         "first. Name the recommended option and put it first. The tool replies at once; then end your turn and this "
         "session resumes with the answers.")
NOTE = (f"You have a tool named `{TOOL}` (`{RULE}` where tools carry a server prefix). When the work needs a value only "
        "the owner has, such as a secret that is in no file, do not guess and do not only say so in your answer: call "
        f"`{TOOL}` with the question (kind `secret` and `name` for a variable), finish what does not depend on it, then "
        "end your turn. If your tool list does not show it, load it by searching for its name first.\n\n" + GUIDE)
SETTLE = ("Can this run safely take the first option of every question, each of which is recommended, without asking "
          "its owner? Answer yes only when every choice is easy to undo, small, and fits the ticket.")
DESCRIPTION = ("Ask the owner of this run something only they can answer. " + GUIDE +
               " Each call holds 1 to 4 questions of kind choice (2 to 4 options, the first one recommended and "
               "saying so), text, secret (name is the environment variable it fills) or done (an action the owner "
               "takes outside arete, with the steps).")
NAME_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"
SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array", "minItems": 1, "maxItems": MAX_QUESTIONS,
            "items": {
                "type": "object",
                "properties": {
                    "header": {"type": "string", "maxLength": MAX_HEADER},
                    "question": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "options": {
                        "type": "array", "minItems": 2, "maxItems": 4,
                        "items": {"type": "object",
                                  "properties": {"label": {"type": "string"}, "description": {"type": "string"}},
                                  "required": ["label", "description"]},
                    },
                    "multiple": {"type": "boolean"},
                    "name": {"type": "string", "pattern": f"^{NAME_PATTERN}$"},
                    "steps": {"type": "array", "minItems": 1, "maxItems": MAX_STEPS, "items": {"type": "string"}},
                },
                "required": ["header", "question", "kind"],
            },
        },
        "why": {"type": "string"},
        "tried": {"type": "string"},
    },
    "required": ["questions", "why", "tried"],
}


def text_of(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def option_problems(at: str, options) -> tuple[list[dict], list[str]]:
    if not isinstance(options, list) or not 2 <= len(options) <= 4:
        return [], [f"{at}.options needs 2 to 4 options"]
    found, problems = [], []
    for j, option in enumerate(options, 1):
        label = text_of(option.get("label")) if isinstance(option, dict) else ""
        about = text_of(option.get("description")) if isinstance(option, dict) else ""
        if not label or not about:
            problems.append(f"{at}.options[{j}] needs a label and a description")
        found.append({"label": label, "description": about})
    if not problems and "recommended" not in f"{found[0]['label']} {found[0]['description']}".lower():
        problems.append(f"{at}.options[1] is the first option and must say it is recommended")
    return found, problems


def question_problems(i: int, raw) -> tuple[dict, list[str]]:
    at = f"questions[{i}]"
    if not isinstance(raw, dict):
        return {}, [f"{at} must be an object"]
    header, question, kind = text_of(raw.get("header")), text_of(raw.get("question")), raw.get("kind")
    problems = []
    if not header or len(header) > MAX_HEADER:
        problems.append(f"{at}.header must be 1 to {MAX_HEADER} characters")
    if not question:
        problems.append(f"{at}.question must not be empty")
    found = {"header": header, "question": question, "kind": kind}
    if kind not in KINDS:
        return found, [*problems, f"{at}.kind must be one of {', '.join(KINDS)}"]
    if kind == "choice":
        found["options"], more = option_problems(at, raw.get("options"))
        problems += more
        if not isinstance(multiple := raw.get("multiple", False), bool):
            problems.append(f"{at}.multiple must be true or false")
        found["multiple"] = multiple is True
    elif kind == "secret":
        found["name"] = text_of(raw.get("name"))
        if not re.fullmatch(NAME_PATTERN, found["name"]):
            problems.append(f"{at}.name must be the environment variable the secret fills")
    elif kind == "done":
        steps = raw.get("steps")
        found["steps"] = [text_of(s) for s in steps] if isinstance(steps, list) else []
        if not 1 <= len(found["steps"]) <= MAX_STEPS or not all(found["steps"]):
            problems.append(f"{at}.steps needs 1 to {MAX_STEPS} non-empty steps")
    return found, problems


def validate(value) -> tuple[dict, list[str]]:
    if not isinstance(value, dict):
        return {}, ["the input must be an object"]
    raw = value.get("questions")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_QUESTIONS:
        return {}, [f"questions needs 1 to {MAX_QUESTIONS} questions"]
    found, problems = [], []
    for i, item in enumerate(raw, 1):
        one, more = question_problems(i, item)
        found.append(one)
        problems += more
    if not text_of(value.get("why")):
        problems.append("why must say why you need the answer")
    if not isinstance(tried := value.get("tried", ""), str):
        problems.append("tried must be a string")
    return {"questions": found, "why": text_of(value.get("why")), "tried": text_of(tried)}, problems


def reply(value) -> tuple[str, bool]:
    _, problems = validate(value)
    return ("Not sent: " + "; ".join(problems), True) if problems else (SENT, False)


def answer_for(request: dict) -> dict:
    method, params = request.get("method"), request.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion") or "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER, "version": "1"}}
    if method == "tools/list":
        return {"tools": [{"name": TOOL, "description": DESCRIPTION, "inputSchema": SCHEMA}]}
    if method == "tools/call":
        if params.get("name") != TOOL:
            return {"content": [{"type": "text", "text": f"No tool {params.get('name')}"}], "isError": True}
        text, bad = reply(params.get("arguments"))
        return {"content": [{"type": "text", "text": text}], "isError": bad}
    if method == "ping":
        return {}
    raise LookupError(method)


def serve(stdin: TextIO, stdout: TextIO) -> int:
    for raw in stdin:
        if not raw.strip():
            continue
        try:
            request = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(request, dict) or "id" not in request:
            continue
        try:
            body = {"result": answer_for(request)}
        except LookupError:
            body = {"error": {"code": -32601, "message": f"no method {request.get('method')}"}}
        stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], **body}) + "\n")
        stdout.flush()
    return 0


def check(stdin: TextIO, stdout: TextIO) -> int:
    try:
        value = json.loads(stdin.read())
    except ValueError:
        value = None
    text, bad = reply(value)
    stdout.write(text + "\n")
    return 1 if bad else 0


def server_command() -> list[str]:
    entrypoint = (f"import sys; sys.path.insert(0, {str(paths.PACKAGE_DIR.parent)!r}); "
                  "from arete.cli import main; raise SystemExit(main())")
    return [sys.executable, "-I", "-B", "-c", entrypoint, "mcp-ask"]


def with_server(mcp_config: str) -> str:
    found = json.loads(mcp_config) if mcp_config else {}
    command = server_command()
    servers = {**(found.get("mcpServers") or {}), SERVER: {"command": command[0], "args": command[1:]}}
    return json.dumps({**found, "mcpServers": servers})


def pi_tool() -> dict:
    return {"name": TOOL, "description": DESCRIPTION, "parameters": SCHEMA, "check": [*server_command(), "--check"]}


def is_ask(name) -> bool:
    return isinstance(name, str) and (name == TOOL or name.endswith(f"__{TOOL}"))


def found(events) -> dict | None:
    calls, failed = [], set()
    for event in events:
        if event.get("kind") == "tool_call" and is_ask(event.get("name")):
            calls.append(event)
        elif event.get("kind") == "tool_result" and event.get("error"):
            failed.add(event.get("id"))
    last = None
    for call in calls:
        if call.get("id") in failed:
            continue
        ask, problems = validate(call.get("input"))
        if not problems:
            last = ask
    return last


class Asked(Exception):
    def __init__(self, ask: dict, ask_id: str, step: str, call: str, session, limit: int | None = None) -> None:
        super().__init__(summary(ask))
        self.ask, self.ask_id, self.step, self.call, self.session, self.limit = ask, ask_id, step, call, session, limit


def summary(ask: dict) -> str:
    questions = [q["question"] for q in ask["questions"]]
    return (questions[0] if len(questions) == 1 else " | ".join(questions))[:300]


def question_text(i: int, q: dict) -> str:
    head = f"{i}. **{outbound.plain(q['header'], 40)}**: {outbound.plain(q['question'], 1000)}"
    if q["kind"] == "choice":
        pick = "You may pick several." if q.get("multiple") else "Pick one."
        options = "\n".join(f"   - {outbound.plain(o['label'], 200)}: {outbound.plain(o['description'], 400)}"
                            + (" (recommended)" if n == 0 and "recommended" not in o["label"].lower() else "")
                            for n, o in enumerate(q["options"]))
        return f"{head}\n   {pick}\n{options}"
    if q["kind"] == "secret":
        return (f"{head}\n   Do not post the value here. Pass the variable `{q['name']}` through the layer's "
                f"`[environment] pass`.")
    if q["kind"] == "done":
        steps = "\n".join(f"   {n}. {outbound.plain(s, 400)}" for n, s in enumerate(q["steps"], 1))
        return f"{head}\n   Do this outside arete:\n{steps}"
    return head


def comment(ask: dict, info_label: str, limit: int | None = None) -> str:
    opening = (f"This run reached its limit of {limit} asks (`[ask] max_per_run`), so it stops here. "
               if limit is not None else "")
    questions = "\n\n".join(question_text(i, q) for i, q in enumerate(ask["questions"], 1))
    tried = f"\n\nTried: {outbound.plain(ask['tried'], 1000)}" if ask.get("tried") else ""
    return (f"{opening}Before I can finish this I need {'an answer' if len(ask['questions']) == 1 else 'answers'}:"
            f"\n\n{questions}\n\nWhy: {outbound.plain(ask['why'], 1000)}{tried}\n\n"
            f"Reply here, then remove the `{info_label}` label and I will pick it up.")


def settles(ask: dict) -> bool:
    return all(q["kind"] == "choice" for q in ask["questions"])


def choices(ask: dict) -> list[str]:
    return [f'{q["header"]}: took the recommended option "{q["options"][0]["label"]}" for "{q["question"]}"'
            for q in ask["questions"]]


def settled_message(ask: dict) -> str:
    taken = "\n".join(f"- {q['header']}: {q['options'][0]['label']}" for q in ask["questions"])
    return (f"The owner was not asked. Arete took the recommended option of each question:\n{taken}\n\n"
            "Carry on with these and finish the step.")


def text_ask(question: str) -> dict:
    return {"questions": [{"header": "Plan", "question": question, "kind": "text"}],
            "why": "The plan found the ticket ambiguous.", "tried": "Read the ticket and the code."}


def assumed(reads: list[list[dict]]) -> list[str]:
    return [c for read in reads for line in read if line.get("event") == "ask_settled"
            for c in line.get("choices") or []]
