from arete.store import runs as run_store

def batch(*a, **kw):
    return agent.Result(ok=True, calls=[
        agent.Result(ok=True, runner="claude", model="test", cost_usd=1, auth_mode="api_key", call_seconds=1.25),
        agent.Result(ok=True, runner="pi", model="test", cost_usd=1.5, auth_mode="subscription", call_seconds=2.75)])
claude.run = batch
rid = record.start("pipeline")
attempt(rid)
found = rows(rid)
assert [(r["seconds"], r["auth_mode"]) for r in found] == [(1.25, "api_key"), (2.75, "subscription")], found
assert [r["try"] for r in found] == [1, 2]
assert run_store.run_get(rid)["cost_usd"] == marks(rid)[0]["cost_usd"] == 2.5
rid = record.start("pipeline")
with state.db() as c:
    c.execute("CREATE TRIGGER reject_second BEFORE INSERT ON steps WHEN NEW.cost_usd = 1.5 "
              "BEGIN SELECT RAISE(ABORT, 'second insert refused'); END")
try:
    attempt(rid)
    raise AssertionError("batch should fail")
except sqlite3.IntegrityError:
    pass
assert rows(rid) == [] and run_store.run_get(rid)["cost_usd"] == 0
assert marks(rid)[0]["cost_usd"] == 2.5 and "cost is not in steps" in marks(rid)[0]["error"]
