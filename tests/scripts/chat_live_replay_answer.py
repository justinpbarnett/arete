
put("chat-1", reply("The answer survived."))
put("chat-2", reply("Independent answer."))
server = serve.Server()
server.lock = serve.take_serve_lock()
entered, release = threading.Event(), threading.Event()
real_ask = chat.ask
def waiting(chat_id, *args, **kw):
    if chat_id == 1:
        entered.set()
        assert release.wait(60)
    return real_ask(chat_id, *args, **kw)
chat.ask = waiting
try:
    chat.send(1, "Hello")
    assert entered.wait(30)
    dashboard_start()
    assert chat.view(1)["busy"] and chat.any_busy()
    try:
        chat.send(1, "Duplicate")
        raise AssertionError("accepted another message")
    except chat.Busy:
        pass
    assert len(chat.view(1)["messages"]) == 1
    other = chat.new("")["id"]
    assert take(other, "Another chat")["messages"][-1]["text"] == "Independent answer."
    server.code = frozenset()
    server.code_seen = frozenset({"changed"})
    with mock.patch.object(serve, "code_stamp", return_value=server.code_seen), \
         mock.patch.object(serve.os, "execv", side_effect=AssertionError("reloaded a live chat")):
        server.load_new_code()
    assert "a chat message is still running" in server.code_note
finally:
    release.set()
deadline = time.monotonic() + 60
while chat.view(1)["busy"]:
    assert time.monotonic() < deadline
    time.sleep(0.02)
chat.recover()
messages = chat.view(1)["messages"]
assert [(m["sender"], m["text"]) for m in messages] == [("you", "Hello"), ("arete", "The answer survived.")]
server.lock.close()
result(ok=True)
