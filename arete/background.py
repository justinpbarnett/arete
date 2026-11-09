from __future__ import annotations

import json
import shlex
import sys

HOOK_TIMEOUT_SECONDS = 30
NOTE = ("A command you run in the background keeps this session open after your final answer until it ends, and your "
        "answer is lost while one still runs. Before you answer, stop with TaskStop each background command you "
        "started that does not end on its own, such as a server or a watcher, even when the task asks you to keep it "
        "running. Leave the stopping out of your answer, unless the task asked you to keep one running: then say that "
        "you stopped it and why.")


def with_stop_hook(settings: dict | None) -> dict:
    settings = settings or {}
    command = shlex.join([sys.executable, "-I", "-m", "arete.cli", "background"])
    hooks = settings.get("hooks", {})
    stop = [*hooks.get("Stop", []), {"hooks": [{"type": "command", "command": command,
                                                "timeout": HOOK_TIMEOUT_SECONDS}]}]
    return {**settings, "hooks": {**hooks, "Stop": stop}}


def main() -> int:
    try:
        reason = block_reason(json.load(sys.stdin))
    except Exception:
        return 0
    if reason:
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def block_reason(payload: dict) -> str:
    if payload.get("stop_hook_active"):
        return ""
    running = [task for task in payload.get("background_tasks") or [] if task.get("status") == "running"]
    if not running:
        return ""
    named = "; ".join(f"`{task.get('command') or task.get('description')}` (task {task.get('id')})" for task in running)
    return (f"Commands you started in the background still run: {named}. This session cannot end, and your answer "
            "reaches no one, until each one ends. Stop with TaskStop each one that does not end on its own, such as a "
            "server or a watcher, even when the task asked you to keep it running. Then give your whole answer again, "
            "because only your last message reaches the reader. If you are waiting for one that ends on its own, "
            "such as a build or a test run, end your turn again: you are woken when it ends.")
