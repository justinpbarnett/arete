
import threading
from types import SimpleNamespace
from arete.store import runs as run_store
from arete.store import asks as ask_store
from arete import owner_asks, control, pipeline, tasks, poll
repo = config.Repo("asking", root)
cfg = config.Config(repos=[repo])
rid = made("asking", "needs-info", repo=repo.name, issue="R-1")
asks_store.ask_add("ask", rid, repo.name, "R-1", "build", "call", "why", "tried",
              [{"kind": "text", "header": "Choice", "question": "Which?"}], {})
row = run_store.run_get(rid)
fetched, threads, errors, launches = threading.Event(), [], [], []
lock_requested, tracker_closes = threading.Event(), []
original_lock, original_close = util.lock, ask_store.ask_close
def lock(name, *args, **kwargs):
    if name == owner_asks.STEER_LOCK and threading.current_thread() is not threading.main_thread():
        lock_requested.set()
    return original_lock(name, *args, **kwargs)
def close(*args, **kwargs):
    if threading.current_thread() is not threading.main_thread():
        tracker_closes.append(args)
    return original_close(*args, **kwargs)
def reply(*args):
    fetched.set()
    return "tracker text"
tk = SimpleNamespace(reply_since=reply)
def tracker_thread():
    try:
        poll.tracker_reply(repo, tk, "R-1", False)
    except BaseException as e:
        errors.append(repr(e))
def continuing(*args):
    if in_flight:
        thread = threading.Thread(target=tracker_thread, daemon=True)
        threads.append(thread)
        thread.start()
        assert fetched.wait(5), "tracker did not fetch"
        assert lock_requested.wait(5), "tracker did not take the steering lock"
    else:
        ask = asks_store.ask_get("ask")
        ask_store.ask_close("ask", "answered", owner_asks.text_answers(ask, "tracker text"))
    return ["resume"]
def launch(*args, **kwargs):
    launches.append(args)
    return {"state": "started"}
with mock.patch.object(util, "lock", side_effect=lock), mock.patch.object(ask_store, "ask_close", side_effect=close), \
        mock.patch.object(tasks, "run_of", return_value=(cfg, repo, row)), \
        mock.patch.object(tasks, "no_later_run"), mock.patch.object(tasks, "live_run", return_value=None), \
        mock.patch.object(tasks, "free_to_start"), mock.patch.object(tasks, "continuing", side_effect=continuing), \
        mock.patch.object(tasks, "launch", side_effect=launch):
    try:
        answer = tasks.answer_ask("ask", {"answers": [{"text": "dashboard text"}]})
        assert in_flight and answer["state"] == "started", answer
    except control.Refused as e:
        assert not in_flight and "was answered from the tracker a moment ago" in str(e), str(e)
        assert e.status == 409 and e.code == "already_resolved"
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive(), "tracker deadlocked"
assert not errors, errors
assert not tracker_closes, tracker_closes
stored = asks_store.ask_get("ask")
expected = "dashboard text" if in_flight else "tracker text"
assert expected in json.dumps(stored["answers"]), stored
assert len(launches) == int(in_flight)
result(answers=stored["answers"], launches=len(launches))
