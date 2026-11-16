
capture.save({"text": "A feedback check"})
def begin():
    draft = state.capture_draft()
    assert state.capture_move(draft["id"], draft["revision"], "draft", "checking")
    assert state.capture_begin_checks(draft["id"], {"ready": "fingerprint"})
begin()
held = serve.take_serve_lock()
try:
    dashboard_start()
    draft = state.capture_draft()
    assert draft["status"] == "checking" and draft["delivery"] == ""
    assert draft["checks"]["ready"]["result"] == "running"
finally:
    held.close()
dashboard_start()
draft = state.capture_draft()
assert draft["status"] == "draft"
assert draft["checks"]["ready"]["result"] == "interrupted"
assert draft["checks"]["ready"]["reason"] == capture.INTERRUPTED
assert state.capture_move(draft["id"], draft["revision"], "draft", "filing")
with serve.take_serve_lock():
    dashboard_start()
    assert state.capture_draft()["delivery"] == ""
dashboard_start()
draft = state.capture_draft()
assert draft["delivery"] == capture.RESTARTED
assert state.capture_move(draft["id"], draft["revision"], "filing", "draft")
begin()
with serve.take_serve_lock(), \
     mock.patch.object(tailnet.Tailscale, "node", return_value=None), \
     mock.patch.object(tailnet.time, "sleep", side_effect=RuntimeError("stop dashboard")):
    try:
        dashboard.serve(inside_serve=True)
    except RuntimeError as e:
        assert str(e) == "stop dashboard"
assert state.capture_draft()["status"] == "draft"
result(ok=True)
