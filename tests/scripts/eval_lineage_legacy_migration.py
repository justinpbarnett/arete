
with sqlite3.connect(paths.DB_FILE) as c:
    c.execute("CREATE TABLE evals (run_id TEXT, repo TEXT, task TEXT, passed INTEGER, seconds REAL, "
              "cost_usd REAL, ts TEXT, source TEXT DEFAULT '')")
    for n in range(2):
        c.execute("INSERT INTO evals VALUES (?, 'replay-sample', 'word-train', 1, 2, 3, ?, 'worktree')",
                  (f"old-{n}", f"2020-01-0{n + 1}"))
state.migrate()
for _ in range(2):
    with state.db() as c:
        columns = {r[1] for r in c.execute("PRAGMA table_info(evals)")}
        assert set(state.EVAL_LINEAGE) <= columns
        assert "evals_key" in {r[1] for r in c.execute("PRAGMA index_list(evals)")}
        old = [dict(r) for r in c.execute("SELECT * FROM evals")]
        assert len(old) == 2
        for row in old:
            assert [row[k] for k in state.EVAL_LINEAGE] == [""] * 6 + [0, 0], row
            assert row["passed"] == 1 and row["seconds"] == 2 and row["cost_usd"] == 3
replies = sample()
state.add_eval("old-2", "replay-sample", "word-train", True, 0, 0, "worktree")
with state.db() as c:
    c.execute("UPDATE evals SET ts = '2020-01-03T00:00:00Z' WHERE run_id = 'old-2'")
comparison = evals.compare("replay-sample", "main")
assert "the baseline predates lineage" in comparison, comparison
(replies / "build.patch").unlink()
code, out = call("word-train")
assert "REGRESSED: the last pass predates lineage" in out, out
print("BEHAVIOR_RESULT=" + json.dumps({"columns": sorted(columns)}))
