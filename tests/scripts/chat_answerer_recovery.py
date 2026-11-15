
for mode in ("kill", "exec"):
    chat_id = 1 if mode == "kill" else chat.new("")["id"]
    put(f"chat-{chat_id}", reply("Recovered reply."))
    code = r"""
import os, signal, sys, threading, time
from pathlib import Path
from arete import chat
root = Path(ROOT)
def waiting(*args, **kw):
    print("ANSWER_READY", flush=True)
    threading.Event().wait(120)
chat.ask = waiting
chat.send(CHAT_ID, "Interrupted")
while not (root / "go-exec").exists():
    time.sleep(0.02)
os.execv(sys.executable, [sys.executable, "-I", "-u", "-c", ADOPT])
"""
    adopt = (f"import sys;sys.path.insert(0,{str(paths.PACKAGE_DIR.parent)!r});"
             "from arete import chat; chat.recover(); chat.recover(); print('RECOVERED', flush=True)")
    code = code.replace("ROOT", repr(str(root))).replace("CHAT_ID", str(chat_id)).replace("ADOPT", repr(adopt))
    p = child(code)
    wait_output(p, "ANSWER_READY")
    assert chat.any_busy() and chat.view(chat_id)["busy"]
    if mode == "kill":
        p.kill()
        p.wait(10)
        assert not chat.any_busy()
        assert chat.view(chat_id)["busy"]
    else:
        (root / "go-exec").touch()
        wait_output(p, "RECOVERED")
        assert p.wait(30) == 0
        assert not chat.any_busy()
    chat.recover()
    chat.recover()
    messages = chat.view(chat_id)["messages"]
    assert [m["text"] for m in messages] == ["Interrupted", chat.RESTARTED]
    assert take(chat_id, "Try again")["messages"][-1]["text"] == "Recovered reply."
result(ok=True)
