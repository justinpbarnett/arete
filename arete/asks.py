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
WITHHELD = (f"The `{TOOL}` tool is not available in this call, because the call holds trusted scratch. {NOBODY} "
            "When the work needs something only the owner has, do not guess: say what is missing in your answer.")
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


def found(events) -> list[dict]:
    calls, failed = [], set()
    for event in events:
        if event.get("kind") == "tool_call" and is_ask(event.get("name")):
            calls.append(event)
        elif event.get("kind") == "tool_result" and event.get("error"):
            failed.add(event.get("id"))
    sent = []
    for call in calls:
        ask, problems = validate(call.get("input"))
        if call.get("id") not in failed and not problems and ask not in sent:
            sent.append(ask)
    return sent


class Asked(Exception):
    def __init__(self, sent: list[dict], ask_ids: list[str], step: str, call: str, session,
                 limit: int | None = None) -> None:
        super().__init__(summary(sent))
        self.sent, self.ask_ids, self.step, self.call = sent, ask_ids, step, call
        self.session, self.limit = session, limit


def summary(sent: list[dict]) -> str:
    return " | ".join(q["question"] for ask in sent for q in ask["questions"])[:300]


def question_text(i: int, q: dict) -> str:
    head = f"{i}. **{outbound.plain(q['header'], 40)}**: {outbound.plain(q['question'], 1000)}"
    if q["kind"] == "choice":
        pick = "You may pick several." if q.get("multiple") else "Pick one."
        options = "\n".join(f"   - {outbound.plain(o['label'], 200)}: {outbound.plain(o['description'], 400)}"
                            + (" (recommended)" if n == 0 and "recommended" not in o["label"].lower() else "")
                            for n, o in enumerate(q["options"]))
        return f"{head}\n   {pick}\n{options}"
    if q["kind"] == "secret":
        return (f"{head}\n   Do not post the value here. `arete answer` stores `{q['name']}` for this repo without "
                f"showing it.")
    if q["kind"] == "done":
        steps = "\n".join(f"   {n}. {outbound.plain(s, 400)}" for n, s in enumerate(q["steps"], 1))
        return f"{head}\n   Do this outside arete:\n{steps}"
    return head


SESSION_GONE = re.compile(r"no conversation found|no rollout found|kept session cannot continue|"
                          r"(session|thread|conversation)\b.{0,60}(not found|does not exist|no longer exists|expired|"
                          r"unavailable|unknown)", re.IGNORECASE)


def session_gone(error) -> bool:
    return bool(SESSION_GONE.search(str(error or "")))


def has_secret(questions: list[dict]) -> bool:
    return any(q.get("kind") == "secret" for q in questions)


def comment(sent: list[dict], info_label: str, limit: int | None = None, run_id: str = "") -> str:
    opening = (f"This run reached its limit of {limit} asks (`[ask] max_per_run`), so it stops here. "
               if limit is not None else "")
    blocks, count = [], 0
    for ask in sent:
        questions = "\n\n".join(question_text(count + i, q) for i, q in enumerate(ask["questions"], 1))
        tried = f"\n\nTried: {outbound.plain(ask['tried'], 1000)}" if ask.get("tried") else ""
        blocks.append(f"{questions}\n\nWhy: {outbound.plain(ask['why'], 1000)}{tried}")
        count += len(ask["questions"])
    need = "an answer" if count == 1 else "answers"
    head = f"{opening}Before I can finish this I need {need}:\n\n" + "\n\n".join(blocks)
    if not run_id:
        return head + f"\n\nReply here, then remove the `{info_label}` label and I will pick it up."
    secret = any(has_secret(ask["questions"]) for ask in sent)
    how = ("A reply here cannot answer this ask, because it holds a secret question, so answer on the dashboard."
           if secret else f"A reply here also counts as the answer to every question: reply, then remove the "
                          f"`{info_label}` label.")
    return head + f"\n\nAnswer on the arete dashboard or with `arete answer {run_id}`. {how}"


ANSWER_KEYS = ("answers", "recommended")
MAX_TEXT = 20000
DECLINED = "the owner declined"


def options_picked(q: dict, value) -> tuple[list[str], str]:
    values = value if isinstance(value, list) else [value]
    labels = [o["label"] for o in q["options"]]
    if not values or len(values) > 1 and not q.get("multiple"):
        return [], "pick one option" if not q.get("multiple") else "pick at least one option"
    picked = []
    for v in values:
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= len(labels):
            picked.append(labels[v - 1])
        elif isinstance(v, str) and v.strip().lower() in [x.lower() for x in labels]:
            picked.append(labels[[x.lower() for x in labels].index(v.strip().lower())])
        else:
            return [], "that is not one of the options"
    return picked, ""


def one_answer(i: int, q: dict, entry, recommended: bool) -> tuple[dict, str]:
    at = f"question {i} ({q['header']})"
    base = {"header": q["header"], "kind": q["kind"], **({"name": q["name"]} if q["kind"] == "secret" else {})}
    if entry is None:
        if recommended and q["kind"] == "choice":
            return {**base, "picked": [q["options"][0]["label"]]}, ""
        return {}, f"{at} needs an answer"
    if not isinstance(entry, dict):
        return {}, f"{at}: give an object such as {{\"text\": \"...\"}} or {{\"declined\": true}}"
    if entry == {"declined": True}:
        return {**base, "declined": True}, ""
    if q["kind"] == "secret":
        if set(entry) == {"secret"} and isinstance(entry["secret"], str):
            return {**base, "set": True}, ""
        return {}, (f"{at}: give {{\"secret\": \"...\"}} with the value of {q['name']}, or {{\"declined\": true}}")
    if q["kind"] == "choice" and set(entry) == {"choice"}:
        picked, why = options_picked(q, entry["choice"])
        return ({**base, "picked": picked}, "") if not why else ({}, f"{at}: {why}")
    if q["kind"] == "done" and entry == {"done": True}:
        return {**base, "done": True}, ""
    text = entry.get("text")
    if set(entry) == {"text"} and isinstance(text, str) and text.strip():
        if len(text) > MAX_TEXT:
            return {}, f"{at}: the answer is {len(text)} characters; the most is {MAX_TEXT}"
        return {**base, "text": text.strip()}, ""
    return {}, f"{at}: that is not an answer for a {q['kind']} question"


def answers_for(ask: dict, body) -> list[dict]:
    questions = ask["questions"]
    if not isinstance(body, dict):
        raise TypeError("the answer must be an object")
    problems = ["the body takes only answers and recommended"] if any(k not in ANSWER_KEYS for k in body) else []
    recommended = body.get("recommended", False)
    if not isinstance(recommended, bool):
        problems.append("recommended must be true or false")
    given = body.get("answers")
    if given is None and recommended is True:
        given = [None] * len(questions)
    if not isinstance(given, list) or len(given) != len(questions):
        problems.append(f"answers needs one entry for each of the {len(questions)} questions")
        raise ValueError("; ".join(problems))
    found = []
    for i, (q, entry) in enumerate(zip(questions, given, strict=True), 1):
        one, why = one_answer(i, q, entry, recommended is True)
        found.append(one)
        if why:
            problems.append(why)
    if problems:
        raise ValueError("; ".join(problems))
    return found


def secret_values(ask: dict, body: dict) -> dict[str, str]:
    given = body.get("answers")
    if not isinstance(given, list):
        return {}
    return {q["name"]: entry["secret"] for q, entry in zip(ask["questions"], given, strict=False)
            if q["kind"] == "secret" and isinstance(entry, dict) and isinstance(entry.get("secret"), str)}


def text_answers(ask: dict, text: str) -> list[dict]:
    return [{"header": q["header"], "kind": q["kind"], "text": text} for q in ask["questions"]]


def answer_words(q: dict, a: dict) -> str:
    if q["kind"] == "secret":
        return f"`{q['name']}` is set for this repo" if a.get("set") else DECLINED
    if a.get("declined"):
        return DECLINED
    if a.get("picked"):
        return ", ".join(a["picked"])
    return "done" if a.get("done") else str(a.get("text") or "")


def answered_lines(rows: list[dict], why: bool = False) -> str:
    out, n = [], 0
    for row in rows:
        for q, a in zip(row["questions"], row["answers"], strict=True):
            n += 1
            out.append(f"{n}. {q['header']}: {q['question']}\n   Answer: {answer_words(q, a)}")
        if why and row.get("why"):
            out.append(f"   Why you asked: {row['why']}")
    return "\n".join(out)


def answers_message(rows: list[dict]) -> str:
    return ("Your owner answered what you asked:\n\n" + answered_lines(rows) +
            "\n\nCarry on with these and finish the step.")


def fresh_note(rows: list[dict]) -> str:
    return ("## Earlier in this step you asked your owner\n\n" + answered_lines(rows, why=True) +
            "\n\nThe owner has answered. Use the answers and finish the step without asking again.")


def settles(ask: dict) -> bool:
    return all(q["kind"] == "choice" for q in ask["questions"])


def choices(ask: dict) -> list[str]:
    return [f'{q["header"]}: took the recommended option "{q["options"][0]["label"]}" for "{q["question"]}"'
            for q in ask["questions"]]


def settled_message(sent: list[dict]) -> str:
    taken = "\n".join(f"- {q['header']}: {q['options'][0]['label']}" for ask in sent for q in ask["questions"])
    return (f"The owner was not asked. Arete took the recommended option of each question:\n{taken}\n\n"
            "Carry on with these and finish the step.")


def text_ask(question: str) -> dict:
    return {"questions": [{"header": "Plan", "question": question, "kind": "text"}],
            "why": "The plan found the ticket ambiguous.", "tried": "Read the ticket and the code."}


def assumed(reads: list[list[dict]]) -> list[str]:
    return [c for read in reads for line in read if line.get("event") == "ask_settled"
            for c in line.get("choices") or []]
