from __future__ import annotations

import threading

from . import board, config, gh, paths, pipeline, runner, state, util
from .agent import Result, Session

CONTEXT_FILE = paths.STATE_DIR / "dashboard.json"
OLD_CHAT_FILE = paths.STATE_DIR / "chat.json"
MOVED_CHAT_FILE = paths.STATE_DIR / "chat.json.moved"
BUDGET_USD = 2
TIMEOUT_SECONDS = 900
TITLE_CHARS = 120
RESTARTED = "The dashboard restarted before this answer came back. Send the message again."
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "issue_title": {"type": "string"},
        "issue_body": {"type": "string"},
    },
    "required": ["reply", "issue_title", "issue_body"],
}
filing = threading.Lock()
moving = threading.Lock()


class Busy(RuntimeError):
    pass


class NoDraft(RuntimeError):
    pass


class NoChat(LookupError):
    pass


class BadProject(ValueError):
    pass


def summary(row: dict) -> dict:
    return {"id": row["id"], "title": row["title"], "project": row["project"], "created": row["created"],
            "updated": row["updated"], "spent": row["paid_usd"], "reported_cost": row["spent"],
            "unclassified_messages": row["unclassified_messages"], "busy": bool(row["busy"]),
            "drafted": bool(row["draft_title"])}


def listing() -> dict:
    move_old_chat()
    return {"chats": [summary(r) for r in state.chats()], "projects": [r.name for r in config.load().repos]}


def chat_row(chat_id: int) -> dict:
    row = state.chat_get(chat_id)
    if not row:
        raise NoChat(f"There is no chat {chat_id}")
    return row


def view(chat_id: int) -> dict:
    row = chat_row(chat_id)
    cfg = config.load()
    try:
        repo = cfg.repo(row["project"]) if row["project"] else arete_repo(cfg)
    except config.UnknownRepo:
        repo = None
    return {**summary(row), "folder": folder(row["project"]), "draft": draft_of(row, cfg),
            "metered": runner.metered(cfg.model("chat"), row["runner"], cfg=cfg, repo=repo) if repo else None,
            "messages": state.chat_messages(chat_id)}


def draft_of(row: dict, cfg: config.Config) -> dict | None:
    if not row["draft_title"]:
        return None
    return {"chat": row["id"], "title": row["draft_title"], "body": row["draft_body"], "repo": cfg.arete_repo}


def pulse() -> dict:
    row = state.chat_newest_draft()
    return {"busy": any_busy(), "draft": draft_of(row, config.load()) if row else None}


def folder(project: str) -> str:
    cfg = config.load()
    try:
        return paths.short_path(cfg.repo(project).path if project else arete_repo(cfg).path)
    except config.UnknownRepo:
        return ""


def new(project: str) -> dict:
    project = project.strip()
    if project:
        cfg = config.load()
        try:
            repo = cfg.repo(project)
        except config.UnknownRepo:
            names = [r.name for r in cfg.repos]
            pick = f"pick one of {', '.join(names)}, or none" if names else "the config has no repo, so pick none"
            raise BadProject(f"'{project}' is not a repo in the config; {pick}") from None
        if not repo.path.is_dir():
            raise BadProject(f"{project}'s folder {paths.short_path(repo.path)} is not there")
    return view(state.chat_add(project))


def title_of(text: str, limit: int = TITLE_CHARS) -> str:
    line = util.first_line(text, len(text))
    if len(line) <= limit:
        return line
    return (line[:limit + 1].rpartition(" ")[0] or line[:limit]).rstrip() + "…"


def send(chat_id: int, text: str) -> dict:
    chat_row(chat_id)
    if not state.chat_claim(chat_id, text, title_of(text)):
        raise Busy("This chat is still waiting on its last answer. Wait for it, or start another chat.")
    row = chat_row(chat_id)
    session = Session(row["session"], row["session_spent"], row["runner"])
    threading.Thread(target=answer, args=(chat_id, text, session, row["project"]), daemon=True).start()
    return view(chat_id)


def answer(chat_id: int, text: str, session: Session, project: str) -> None:
    reply, cost, paid, draft = "", 0.0, 0.0, None
    try:
        result = ask(chat_id, text, session, project)
        session, cost, paid = result.session, result.cost_usd, result.paid_usd
        if result.ok:
            reply, draft = reply_and_draft(result)
        else:
            reply = f"The agent stopped: {result.error or result.text}"
    except Exception as e:
        reply = f"The agent stopped: {util.first_line(f'{type(e).__name__}: {e}', 300)}"
    state.chat_say(chat_id, "arete", reply, cost, paid_usd=paid,
                   busy=0, session=session.id, session_spent=session.spent_usd,
                   runner=session.runner, **({"draft_title": draft["title"], "draft_body": draft["body"]} if draft else {}))


def reply_and_draft(result: Result) -> tuple[str, dict | None]:
    drafted = result.structured if isinstance(result.structured, dict) else {}
    title, body = str(drafted.get("issue_title") or "").strip(), str(drafted.get("issue_body") or "").strip()
    return drafted.get("reply") or result.text, {"title": title, "body": body} if title else None


def file_draft(chat_id: int) -> dict:
    with filing:
        row = chat_row(chat_id)
        if not row["draft_title"]:
            raise NoDraft("There is no drafted issue to file.")
        github = arete_repo(config.load()).github
        if not github:
            raise NoDraft("The config names no GitHub repo for arete, so the draft stays unfiled.")
        number = gh.issue_create(github, row["draft_title"], row["draft_body"])
        state.chat_drop_draft(chat_id, row["draft_title"], row["draft_body"])
        state.chat_say(chat_id, "arete", f"Filed {gh.issue_url(github, number)}")
    return view(chat_id)


def drop_draft(chat_id: int) -> dict:
    row = chat_row(chat_id)
    state.chat_drop_draft(chat_id, row["draft_title"], row["draft_body"])
    return view(chat_id)


def move_old_chat() -> None:
    with moving:
        if not OLD_CHAT_FILE.exists():
            return
        old = util.read_json(OLD_CHAT_FILE, {})
        old = old if isinstance(old, dict) else {}
        messages = [m for m in old.get("messages") or [] if isinstance(m, dict)]
        draft = old.get("draft") if isinstance(old.get("draft"), dict) else {}
        if messages or draft.get("title"):
            if old.get("busy"):
                messages.append({"from": "arete", "text": RESTARTED, "ts": util.iso()})
            asked = next((m for m in messages if m.get("from") == "you"), {})
            state.chat_import(
                {"title": title_of(str(asked.get("text") or "")), "project": "",
                 "created": messages[0].get("ts") if messages else util.iso(),
                 "updated": messages[-1].get("ts") if messages else util.iso(),
                 "spent": round(sum(float(m.get("cost") or 0) for m in messages), 2), "busy": 0,
                 "session": old.get("session") or "", "session_spent": float(old.get("spent") or 0),
                 "runner": old.get("runner") or "", "draft_title": draft.get("title") or "",
                 "draft_body": draft.get("body") or ""},
                [(m.get("from") or "arete", str(m.get("text") or ""), float(m.get("cost") or 0), m.get("ts") or util.iso())
                 for m in messages])
        OLD_CHAT_FILE.replace(MOVED_CHAT_FILE)


def recover() -> None:
    move_old_chat()
    for chat_id in state.busy_chats():
        state.chat_say(chat_id, "arete", RESTARTED, busy=0)


def any_busy() -> bool:
    return bool(state.busy_chats())


def arete_repo(cfg: config.Config) -> config.Repo:
    try:
        return cfg.repo(cfg.arete_repo)
    except config.UnknownRepo:
        return config.Repo(name=cfg.arete_repo, path=paths.PACKAGE_DIR.parent)


def project_section(repo: config.Repo) -> str:
    layer = config.load_layer(repo.layer_dir)
    expertise = layer.expertise_file.read_text().strip() if layer.expertise_file.exists() else ""
    return pipeline.prompt("chat_project", repo=repo.name, path=repo.path, layer=layer.dir,
                           checks=pipeline.check_list(layer), expertise=expertise or "(none yet)")


def ask(chat_id: int, text: str, session: Session, project: str) -> Result:
    cfg = config.load()
    model = cfg.model("chat")
    util.write_json(CONTEXT_FILE, {"snapshot": board.snapshot(), "trends": board.trends()})
    arete = arete_repo(cfg)
    work = cfg.repo(project) if project else arete
    github = arete.github
    reads_issues = [f"Bash(gh issue list --repo {github}*)", f"Bash(gh issue view * --repo {github}*)"] if github else []
    return runner.run(
        text, work.path,
        trace=("chat", f"chat {chat_id}"),
        cfg=cfg,
        repo=work,
        model=model,
        tools=[*runner.READ_TOOLS, "Bash"],
        allowed_tools=[*runner.READ_TOOLS, "Bash(arete status*)", "Bash(arete events*)", "Bash(git log*)",
                       "Bash(git show*)", *reads_issues],
        json_schema=REPLY_SCHEMA,
        resume=session,
        system=pipeline.prompt("chat", code=arete.path, workdir=work.path, config=paths.CONFIG_FILE,
                               state=paths.STATE_DIR, context=CONTEXT_FILE, logs=paths.LOG_DIR,
                               github=github or "(no GitHub repo set)",
                               project=project_section(work) if project else ""),
        max_budget_usd=BUDGET_USD,
        timeout=TIMEOUT_SECONDS,
        wait_out_limits=False,
        add_dirs=[paths.CONFIG_DIR, paths.STATE_DIR, paths.LOG_DIR, *([arete.path] if project else [])],
    )
