from arete.store import runs as run_store

cfg.pricing = {"test": (2, 4)}
def streamed(*a, **kw):
    for ident in ("one", "two"):
        line = json.dumps({"type": "assistant", "message": {"id": ident,
                          "usage": {"input_tokens": 100, "output_tokens": 50}}})
        kw["on_line"](line)
        kw["on_line"](line)
    raise KeyboardInterrupt()
claude.run = streamed
rid = record.start("pipeline")
try:
    attempt(rid)
    raise AssertionError("must interrupt")
except KeyboardInterrupt:
    pass
r, = rows(rid)
assert (r["turns"], r["tokens_in"], r["tokens_out"], r["ok"], r["error"]) == (2, 200, 100, 0, "stopped by request"), r
assert abs(r["cost_usd"] - .0008) < 1e-12 and run_store.run_get(rid)["cost_usd"] == r["cost_usd"]
assert marks(rid)[0]["error"] == r["error"] and marks(rid)[0]["cost_usd"] == r["cost_usd"]
rid = record.start("pipeline")
res = attempt(rid, max_tokens=150)
r, = rows(rid)
assert not res.ok and r["turns"] == 1 and "tokens left under the limit" in r["error"], r
def stopped(*a, **kw):
    raise KeyboardInterrupt()
codex.run = stopped
rid = record.start("pipeline")
try:
    attempt(rid, "codex")
except KeyboardInterrupt:
    pass
r, = rows(rid)
assert (r["ok"], r["error"], r["turns"], r["cost_usd"]) == (0, "stopped by request", 0, 0), r
assert marks(rid)[0]["error"] == r["error"]
def crashed(*a, **kw):
    kw["on_line"](json.dumps({"type": "message_end", "message": {"role": "assistant", "usage": {
        "input": 300, "output": 100, "cost": {"total": .25}}}}))
    raise RuntimeError("parked by chain budget")
pi.run = crashed
rid = record.start("pipeline")
try:
    attempt(rid, "pi")
    raise AssertionError("must crash")
except RuntimeError:
    pass
r, = rows(rid)
assert (r["turns"], r["tokens_in"], r["tokens_out"]) == (1, 300, 100), r
assert abs(r["cost_usd"] - .001) < 1e-12
assert r["error"] == marks(rid)[0]["error"] and "parked by chain budget" in r["error"]
def parked(*a, **kw):
    raise RuntimeError("parked before usage")
claude.run = parked
rid = record.start("pipeline")
try:
    attempt(rid)
    raise AssertionError("must park")
except RuntimeError:
    pass
r, = rows(rid)
assert r["cost_usd"] == 0 and r["error"] == marks(rid)[0]["error"] and "crashed:" in r["error"]
rid = record.start("pipeline")
record.ask_stop(rid)
try:
    attempt(rid)
except RuntimeError:
    pass
r, = rows(rid)
assert r["error"] == marks(rid)[0]["error"] == "stopped by request"
assert heartbeat.check_costs()[0].ok
