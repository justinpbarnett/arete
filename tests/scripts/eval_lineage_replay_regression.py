
replies = sample()
head = util.git(["rev-parse", "HEAD"], root / "sample/repo").strip()
rows = []
for _ in range(3):
    code, out = call("word-train")
    row = state.eval_rows(latest())[0]
    assert row["passed"] and row["base_commit"] == head, (row, out)
    assert head[:12] in out, out
    with state.db() as c:
        engine = c.execute("SELECT engine_sha FROM runs WHERE id = ?", (row["run_id"],)).fetchone()[0]
    assert row["engine_sha"] == engine == codebase.STAMP
    assert row["prompts_from"] == ""
    rows.append(row)
assert len({r["prompts_sha"] for r in rows}) == 1
assert {json.loads(r["settings"])["models"]["build"] for r in rows} == {"scratch-a"}
with state.db() as c:
    c.execute("UPDATE evals SET context_sha = 'older harness' WHERE source = 'worktree'")
comparison = evals.compare("replay-sample", "main")
assert "since that run" not in comparison.splitlines()[0], comparison
with state.db() as c:
    for row in rows:
        c.execute("UPDATE evals SET context_sha = ? WHERE run_id = ?", (row["context_sha"], row["run_id"]))
text = paths.CONFIG_FILE.read_text()
paths.CONFIG_FILE.write_text(text.replace('build = "scratch-a"', 'build = "scratch-b"'))
comparison = evals.compare("replay-sample", "main")
assert "build model scratch-a to scratch-b" in comparison.splitlines()[0], comparison
assert "harness" not in comparison.splitlines()[0], comparison
folder = root / "edited"
shutil.copytree(paths.PROMPTS_DIR, folder)
with (folder / "build.md").open("a") as f:
    f.write("\nAn edited builder prompt.\n")
(replies / "build.patch").unlink()
code, out = call("word-train", prompts=evals.Prompts(folder, "edited"))
fourth = state.eval_rows(latest())[0]
md = (paths.RUNS_DIR / latest() / "results.md").read_text()
assert not fourth["passed"], (fourth, out)
for phrase in ("REGRESSED", "build model scratch-a to scratch-b", "prompts"):
    assert phrase in out and phrase in md, (out, md)
assert fourth["prompts_from"] == str(folder)
assert fourth["prompts_sha"] != rows[0]["prompts_sha"]
assert json.loads(fourth["settings"])["models"]["build"] == "scratch-b"
assert fourth["engine_sha"] == codebase.STAMP and fourth["base_commit"] == head
assert evals.lineage_changes(fourth, fourth) == []
assert evals.lineage_changes(fourth, {**fourth, "context_sha": "different"}, skip=("harness",)) == []
paths.CONFIG_FILE.write_text(text)
code, out = call("word-train")
assert "nothing recorded changed" in out, out
shutil.copyfile(replies / "build2.patch", replies / "build.patch")
code, out = call()
run_id = latest()
suite = state.eval_rows(run_id)
assert len(suite) == 3 and all(r["passed"] for r in suite), (suite, out)
with state.db() as c:
    steps = [dict(s) for s in c.execute("SELECT * FROM steps WHERE run_id = ? ORDER BY id", (run_id,))]
covered = []
for row in suite:
    own = [s for s in steps if row["after_step"] < s["id"] <= row["last_step"]]
    assert {s["step"] for s in own} >= {"plan", "build"}, own
    assert [s["step"] for s in own][:2] == ["plan", "build"], own
    covered.extend(s["id"] for s in own)
assert len(covered) == len(set(covered)) and sorted(covered) == [s["id"] for s in steps]
try:
    state.add_eval(run_id, suite[0]["repo"], suite[0]["task"], False, 0, 0)
except sqlite3.IntegrityError:
    pass
else:
    raise AssertionError("duplicate eval accepted")
assert state.eval_rows(run_id) == suite
history = state.eval_history("replay-sample")
code, out = call("word-train", dry_run=True)
assert "word-train" in out and state.eval_history("replay-sample") == history
assert state.last_step_id(run_id) == steps[-1]["id"]
print("BEHAVIOR_RESULT=" + json.dumps({"tasks": len(suite), "regression": md}))
