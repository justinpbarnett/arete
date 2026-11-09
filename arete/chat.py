from __future__ import annotations

import threading

from . import board, config, gh, paths, pipeline, runner, util
from .agent import NO_SESSION, Result, Session

CHAT_FILE = paths.STATE_DIR / "chat.json"
CONTEXT_FILE = paths.STATE_DIR / "dashboard.json"
BUDGET_USD = 2
TIMEOUT_SECONDS = 900
RESTARTED = "The dashboard restarted before this answer came back. Send the message again."
EMPTY = {"session": "", "spent": 0.0, "messages": [], "busy": False, "draft": None}
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "issue_title": {"type": "string"},
        "issue_body": {"type": "string"},
    },
    "required": ["reply", "issue_title", "issue_body"],
}
chat_file_lock = threading.Lock()


class Busy(RuntimeError):
    pass


class NoDraft(RuntimeError):
    pass


def history() -> dict:
    return {**EMPTY, **util.read_json(CHAT_FILE, {})}


def view() -> dict:
    h = history()
    return {"messages": h["messages"], "busy": h["busy"], "draft": h["draft"]}


def message(sender: str, text: str, cost: float = 0.0) -> dict:
    return {"from": sender, "text": text, "ts": util.iso(), "cost": round(cost, 2)}


def settle_after_restart() -> None:
    with chat_file_lock:
        h = history()
        if h["busy"]:
            util.write_json(CHAT_FILE, {**h, "busy": False, "messages": [*h["messages"], message("arete", RESTARTED)]})


def send(text: str) -> dict:
    with chat_file_lock:
        h = history()
        if h["busy"]:
            raise Busy("The last message is still running. Wait for its answer.")
        h = {**h, "busy": True, "messages": [*h["messages"], message("you", text)]}
        util.write_json(CHAT_FILE, h)
    threading.Thread(target=answer, args=(text, Session(h["session"], h["spent"], h.get("runner", ""))), daemon=True).start()
    return view()


def new() -> dict:
    with chat_file_lock:
        if history()["busy"]:
            raise Busy("The last message is still running. Wait for its answer, then start a new chat.")
        util.write_json(CHAT_FILE, EMPTY)
    return view()


def answer(text: str, session: Session) -> None:
    reply, cost, draft = "", 0.0, None
    try:
        result = ask(text, session)
        session, cost = result.session, result.cost_usd
        if result.ok:
            reply, draft = reply_and_draft(result)
        else:
            reply = f"The agent stopped: {result.error or result.text}"
    except Exception as e:
        reply = f"The agent stopped: {util.first_line(f'{type(e).__name__}: {e}', 300)}"
    with chat_file_lock:
        h = history()
        util.write_json(CHAT_FILE, {**h, "session": session.id, "spent": session.spent_usd,
                                    "runner": session.runner, "busy": False,
                                    "draft": draft or h["draft"],
                                    "messages": [*h["messages"], message("arete", reply, cost)]})


def reply_and_draft(result: Result) -> tuple[str, dict | None]:
    drafted = result.structured if isinstance(result.structured, dict) else {}
    title, body = str(drafted.get("issue_title") or "").strip(), str(drafted.get("issue_body") or "").strip()
    return drafted.get("reply") or result.text, {"title": title, "body": body} if title else None


def file_draft() -> dict:
    with chat_file_lock:
        h = history()
        if not h["draft"]:
            raise NoDraft("There is no drafted issue to file.")
        github = arete_repo(config.load()).github
        if not github:
            raise NoDraft("The config names no GitHub repo for arete, so the draft stays unfiled.")
        number = gh.issue_create(github, h["draft"]["title"], h["draft"]["body"])
        util.write_json(CHAT_FILE, {**h, "draft": None,
                                    "messages": [*h["messages"], message("arete", f"Filed {gh.issue_url(github, number)}")]})
    return view()


def drop_draft() -> dict:
    with chat_file_lock:
        util.write_json(CHAT_FILE, {**history(), "draft": None})
    return view()


def arete_repo(cfg: config.Config) -> config.Repo:
    try:
        return cfg.repo(cfg.arete_repo)
    except config.UnknownRepo:
        return config.Repo(name=cfg.arete_repo, path=paths.PACKAGE_DIR.parent)


def ask(text: str, session: Session) -> Result:
    cfg = config.load()
    model = cfg.model("chat")
    if session.runner and session.runner != runner.route(model)[0]:
        session = NO_SESSION
    util.write_json(CONTEXT_FILE, {"snapshot": board.snapshot(), "trends": board.trends()})
    repo = arete_repo(cfg)
    github = repo.github
    reads_issues = [f"Bash(gh issue list --repo {github}*)", f"Bash(gh issue view * --repo {github}*)"] if github else []
    return runner.run(
        text, repo.path,
        trace=("chat", "chat"),
        cfg=cfg,
        model=model,
        tools=[*runner.READ_TOOLS, "Bash"],
        allowed_tools=[*runner.READ_TOOLS, "Bash(arete status*)", "Bash(arete events*)", "Bash(git log*)",
                       "Bash(git show*)", *reads_issues],
        json_schema=REPLY_SCHEMA,
        resume=session,
        system=pipeline.prompt("chat", code=repo.path, config=paths.CONFIG_FILE, state=paths.STATE_DIR,
                               context=CONTEXT_FILE, logs=paths.LOG_DIR, github=github or "(no GitHub repo set)"),
        max_budget_usd=BUDGET_USD,
        timeout=TIMEOUT_SECONDS,
        wait_out_limits=False,
        add_dirs=[paths.CONFIG_DIR, paths.STATE_DIR, paths.LOG_DIR],
    )

