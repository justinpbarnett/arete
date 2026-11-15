
for rid, started, usd, tin in (("A", "2020-01-01T00:00:00Z", 9, 900), ("B", "2020-01-02T00:00:00Z", 3, 300),
                               ("C", "2020-01-04T00:00:00Z", 2, 200)):
    record.start("pipeline", run_id=rid, started=started, cost_usd=usd, tokens_in=tin, tokens_out=tin // 10)
with state.db() as c:
    for rid, usd, ts in (("B", 1, "2020-01-03T00:00:00Z"), ("C", 1.5, "2020-01-04T00:00:00Z")):
        c.execute("INSERT INTO steps (run_id, runner, cost_usd, tokens_in, tokens_out, ts) VALUES (?, 'claude', ?, 100, 10, ?)",
                  (rid, usd, ts))
paid = state.paid_by_run(["A", "B", "C"])
args = ["costs", "--backfill"]
if use_before:
    f = root / "before.json"
    f.write_text(json.dumps({"A": 12}))
    args += ["--before", str(f)]
code, out, err = call(*args)
assert code == 0, (out, err)
a = rows("A")
assert [(r["step"], r["cost_usd"]) for r in a] == ([("legacy", 12), ("correction", -3)] if use_before else [("legacy", 9)]), a
assert all(r["auth_mode"] == "subscription" and r["ts"] == "2020-01-01T00:00:00Z" for r in a)
assert (a[0]["tokens_in"], a[0]["tokens_out"]) == (900, 90)
b = rows("B")[-1]
assert (b["step"], b["cost_usd"], b["tokens_in"], b["auth_mode"], b["ts"]) == ("legacy", 2, 200, "subscription", "2020-01-02T00:00:00Z"), b
assert len(rows("C")) == 1
assert "backfilled 0 steps" in call(*args)[1]
assert state.paid_by_run(["A", "B", "C"]) == paid
assert [state.run_get(r)["cost_usd"] for r in ("A", "B", "C")] == [9, 3, 2]
check, = heartbeat.check_costs()
assert not check.ok and "1 runs" in check.detail and "$0.500000" in check.detail and check.detail.endswith("C"), check
assert state.cost_drift() == [{"id": "C", "run_usd": 2, "steps_usd": 1.5, "difference": .5}]
checked = []
def fake_check(name):
    def check(*a):
        checked.append(name)
        return []
    return check
for name in ("jobs", "probes", "claude_auth", "codex_auth", "codex_cleanup", "pi_auth", "hook", "disk", "serve",
             "backups", "loops", "oncall", "workspaces", "scratch"):
    setattr(heartbeat, "check_" + name, fake_check(name))
heartbeat.install.rotate_logs = lambda: None
heartbeat.network_up_or_skip = lambda *a: True
alerts = []
heartbeat.alerts.raise_alert = lambda repo, key, *a: alerts.append(key)
heartbeat.alerts.clear_alert = lambda *a: None
with contextlib.redirect_stdout(io.StringIO()):
    assert heartbeat.run(dry_run=True) == 1
assert alerts == ["costs:drift"] and {"backups", "workspaces", "scratch"} <= set(checked), (alerts, checked)
assert call("costs")[0] == 0
code, out, err = call("costs", "--by", "step", "kind", "model", "route", "--days", "36500")
assert code == 0 and "legacy" in out, (out, err)
code, out, err = call("costs", "--by", "route", "--days", "36500", "--json")
assert code == 0 and "subscription" in out, (out, err)
