
for mode in ("subscription", "api_key", "local"):
    rid = record.start("pipeline")
    record.step(rid, "build", agent.Result(ok=True, cost_usd=5, auth_mode="api_key"), 1)
    args = ["costs", "--correct", rid, "-1.25", "--why", "repriced"]
    if mode != "subscription":
        args += ["--auth-mode", mode]
    assert call(*args)[0] == 0
    r = rows(rid)[-1]
    assert (r["step"], r["kind"], r["runner"], r["auth_mode"], r["error"], r["cost_usd"]) == (
        "correction", "correction", "correction", mode, "repriced", -1.25), r
    assert state.run_get(rid)["cost_usd"] == 3.75
    assert state.paid_by_run([rid])[rid] == (3.75 if mode == "api_key" else 5)
assert heartbeat.check_costs()[0].ok
snapshot = rows(rid), state.run_get(rid)
bad = root / "bad.json"
bad.write_text('{bad')
for args in (["--correct", rid, "1"], ["--correct", "absent", "1", "--why", "x"],
             ["--correct", rid, "nan", "--why", "x"], ["--correct", rid, "inf", "--why", "x"],
             ["--correct", rid, "1", "--why", "x", "--by", "step"], ["--why", "x"],
             ["--auth-mode", "api_key"], ["--before", str(bad)], ["--backfill", "--before", str(bad)],
             ["--correct", rid, "1", "--why", "x", "--backfill"]):
    assert call("costs", *args)[0] != 0, args
    assert (rows(rid), state.run_get(rid)) == snapshot
for value in (None, [], {"absent": 12}, {rid: "12"}, {rid: float("inf")}, {rid: True}):
    bad.write_text(json.dumps(value))
    assert call("costs", "--backfill", "--before", str(bad))[0] != 0
    assert (rows(rid), state.run_get(rid)) == snapshot
record.start("pipeline", run_id="null", cost_usd=None)
record.start("pipeline", run_id="small", cost_usd=.0000009)
for i in range(7):
    record.start("pipeline", run_id=f"drift-{i}", cost_usd=.0000011 if i == 0 else i)
found = state.cost_drift()
assert [r["id"] for r in found] == [f"drift-{i}" for i in range(7)], found
check, = heartbeat.check_costs()
assert "7 runs" in check.detail and "$21.000001" in check.detail and "drift-4" in check.detail
assert "drift-5" not in check.detail
