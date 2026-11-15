
import json
import sys
import time
from pathlib import Path

from arete import chat, paths, record, state

root = Path(sys.argv[1])
answers = root / "replay"


def put(name, body):
    (answers / f"{name}.json").write_text(json.dumps(body))


def reply(text):
    return {"text": "", "structured": {"reply": text, "issue_title": "", "issue_body": ""}}


def take(chat_id, text):
    chat.send(chat_id, text)
    deadline = time.monotonic() + 60
    while chat.view(chat_id)["busy"]:
        assert time.monotonic() < deadline, "the chat never finished"
        time.sleep(0.05)
    return chat.view(chat_id)


def header(name):
    return next(record.lines(paths.record_dir("chat") / f"{name}{record.RECORD_SUFFIX}"), {})


def answer_of(n):
    return "chat-1" if n == 1 else f"chat-1{n}"


def record_of(n):
    return "chat-1" if n == 1 else f"chat-1-{n}"


def summary_answer_of(k):
    return "chat-1-summary" if k == 1 else f"chat-1-summary{k}"


def summary_records():
    return sorted(p.name for p in paths.record_dir("chat").glob(f"chat-1-summary*{record.RECORD_SUFFIX}"))


def summary_header(k):
    return header("chat-1-summary" if k == 1 else f"chat-1-summary-{k}")


def result(**fields):
    print("BEHAVIOR_RESULT=" + json.dumps(fields))


state.migrate()
assert chat.new("")["id"] == 1
