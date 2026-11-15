
rid = record.start("pipeline")
with state.db() as c:
    c.execute("CREATE TRIGGER reject_call BEFORE INSERT ON steps BEGIN SELECT RAISE(ABORT, 'refused'); END")
def interrupted(*a, **kw):
    kw["on_line"](json.dumps({"type": "assistant", "message": {"id": "one", "usage": {
        "input_tokens": 100, "output_tokens": 50}}}))
    raise KeyboardInterrupt()
cfg.pricing = {"test": (2, 4)}
claude.run = interrupted
try:
    attempt(rid)
    raise AssertionError("write must fail")
except sqlite3.IntegrityError:
    pass
assert rows(rid) == [] and state.run_get(rid)["cost_usd"] == 0
mark, = marks(rid)
assert abs(mark["cost_usd"] - .0004) < 1e-12 and "cost is not in steps" in mark["error"], mark
with state.db() as c:
    c.execute("DROP TRIGGER reject_call")
cfg.pricing = {}
claude.run = lambda *a, **kw: result()
rid = record.start("pipeline")
attempt(rid)
assert len(rows(rid)) == len(marks(rid)) == 1 and marks(rid)[0]["cost_usd"] == 2.5
rid = record.start("pipeline")
def broken_finish(*a):
    raise RuntimeError("timeline failure after commit")
record.Recorder.finish = broken_finish
try:
    attempt(rid)
    raise AssertionError("finish must fail")
except RuntimeError:
    pass
r, = rows(rid)
assert r["cost_usd"] == state.run_get(rid)["cost_usd"] == 2.5
