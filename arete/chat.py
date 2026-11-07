from __future__ import annotations

import threading

from . import board, claude, config, gh, paths, pipeline, util

CHAT_FILE = paths.STATE_DIR / "chat.json"
CONTEXT_FILE = paths.STATE_DIR / "dashboard.json"
BUDGET_USD = 2
TIMEOUT_SECONDS = 900
RESTARTED = "The dashboard restarted before this answer came back. Send the message again."
EMPTY = {"session": "", "spent": 0.0, "messages": [], "busy": False}
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "issue_title": {"type": "string"},
        "issue_body": {"type": "string"},
    },
    "required": ["reply", "issue_title", "issue_body"],
}
turn = threading.Lock()


class Busy(RuntimeError):
    pass


def history() -> dict:
    return {**EMPTY, **util.read_json(CHAT_FILE, {})}


def view() -> dict:
    h = history()
    return {"messages": h["messages"], "busy": h["busy"]}


def message(sender: str, text: str, cost: float = 0.0) -> dict:
    return {"from": sender, "text": text, "ts": util.iso(), "cost": round(cost, 2)}


def settle_after_restart() -> None:
    with turn:
        h = history()
        if h["busy"]:
            util.write_json(CHAT_FILE, {**h, "busy": False, "messages": [*h["messages"], message("arete", RESTARTED)]})


def send(text: str) -> dict:
    with turn:
        h = history()
        if h["busy"]:
            raise Busy("The last message is still running. Wait for its answer.")
        h = {**h, "busy": True, "messages": [*h["messages"], message("you", text)]}
        util.write_json(CHAT_FILE, h)
    threading.Thread(target=answer, args=(text, claude.Session(h["session"], h["spent"])), daemon=True).start()
    return view()


def new() -> dict:
    with turn:
        if history()["busy"]:
            raise Busy("The last message is still running. Wait for its answer, then start a new chat.")
        util.write_json(CHAT_FILE, EMPTY)
    return view()


def answer(text: str, session: claude.Session) -> None:
    reply, cost = "", 0.0
    try:
        result = ask(text, session)
        session, cost = result.session if result.session.id else session, result.cost_usd
        reply = file_issue_if_drafted(result) if result.ok else f"The agent stopped: {result.error or result.text}"
    except Exception as e:
        reply = f"The agent stopped: {util.first_line(f'{type(e).__name__}: {e}', 300)}"
    with turn:
        h = history()
        util.write_json(CHAT_FILE, {**h, "session": session.id, "spent": session.spent_usd, "busy": False,
                                    "messages": [*h["messages"], message("arete", reply, cost)]})


def arete_repo(cfg: config.Config) -> config.Repo:
    try:
        return cfg.repo(cfg.arete_repo)
    except config.UnknownRepo:
        return config.Repo(name=cfg.arete_repo, path=paths.PACKAGE_DIR.parent)


def ask(text: str, session: claude.Session) -> claude.Result:
    cfg = config.load()
    util.write_json(CONTEXT_FILE, {"snapshot": board.snapshot(), "trends": board.trends()})
    repo = arete_repo(cfg)
    github = repo.github
    reads_issues = [f"Bash(gh issue list --repo {github}*)", f"Bash(gh issue view * --repo {github}*)"] if github else []
    return claude.run(
        text, repo.path,
        model=cfg.model("chat"),
        tools=[*pipeline.READ_TOOLS, "Bash"],
        allowed_tools=[*pipeline.READ_TOOLS, "Bash(arete status*)", "Bash(arete events*)", "Bash(git log*)",
                       "Bash(git show*)", *reads_issues],
        json_schema=REPLY_SCHEMA,
        resume=session,
        system=pipeline.prompt("chat", code=repo.path, config=paths.CONFIG_FILE, state=paths.STATE_DIR,
                               context=CONTEXT_FILE, logs=paths.LOG_DIR, github=github or "(no GitHub repo set)"),
        max_budget_usd=BUDGET_USD,
        timeout=TIMEOUT_SECONDS,
        add_dirs=[paths.CONFIG_DIR, paths.STATE_DIR, paths.LOG_DIR],
    )


def file_issue_if_drafted(result: claude.Result) -> str:
    drafted = result.structured if isinstance(result.structured, dict) else {}
    reply = drafted.get("reply") or result.text
    title, body = drafted.get("issue_title", "").strip(), drafted.get("issue_body", "").strip()
    if not title:
        return reply
    github = arete_repo(config.load()).github
    if not github:
        return f"{reply}\n\nI did not file the issue: the config names no GitHub repo for arete."
    try:
        number = gh.issue_create(github, title, body)
    except gh.GhError as e:
        return f"{reply}\n\nFiling the issue failed: {e}"
    return f"{reply}\n\nFiled: https://github.com/{github}/issues/{number}"
