
sample()
cfg = config.load()
repo = cfg.repo("replay-sample")
src = evals.Source(repo)
harness = repo.path / "CLAUDE.md"
harness.write_text("first\n")
link = repo.path / "AGENTS.md"
link.symlink_to("CLAUDE.md")
def context():
    code, out = call("word-train")
    row = insights_store.eval_rows(latest())[0]
    assert row["passed"], (row, out)
    return row["context_sha"]
digests = [context()]
harness.write_text("second\n")
digests.append(context())
link.unlink()
link.symlink_to("missing.md")
digests.append(context())
with (repo.layer / "config.toml").open("a") as f:
    f.write("\n# changed layer\n")
digests.append(context())
layer = config.load_layer(repo.layer)
layer.expertise_file.parent.mkdir(parents=True, exist_ok=True)
layer.expertise_file.write_text("A learned fact.\n")
digests.append(context())
assert len(set(digests)) == len(digests), digests
folder = root / "prompts"
shutil.copytree(paths.PROMPTS_DIR, folder)
p = evals.Prompts(folder, "folder")
original = evals.prompts_digest(p)
(folder / "review.md").write_text("unused override")
assert evals.prompts_digest(p) == original
with (folder / "fix.md").open("a") as f:
    f.write("\nchanged\n")
assert evals.prompts_digest(p) != original
assert original == evals.prompts_digest(evals.Prompts())
files = {p.name: p for p in paths.PROMPTS_DIR.glob("*.md")}
assert original == util.digest(*(part for name, p in sorted(files.items()) for part in (name, p.read_bytes())))
exported = evals.Prompts(paths.RUNS_DIR / "exported/main", "origin/main", "a" * 40)
assert evals.prompts_from(exported) == "origin/main@aaaaaaaaaaaa exported/main"
assert experiment.sha is util.digest
assert experiment.sha("a", b"bc") == util.digest("a", b"bc")
assert util.digest("a", "bc") != util.digest("ab", "c")
assert util.digest("") != util.digest()
table = evals.results_md("repo", "worktree", [evals.TaskResult("t", False, "a|b", seconds=0, paid=0, regressed=True,
                                                               changed=["build model a|b to c"])])
assert r"a\|b" in table and r"build model a\|b to c" in table, table
def eval_run(task):
    return evals.EvalRun(id=task.name, repo=repo, dir=paths.RUNS_DIR / task.name / task.name, task=task.name)
task = evals.Task("no-check", "No check", "Nothing")
result = evals.run_task(cfg, src, task, evals.Prompts(), eval_run(task))
assert [getattr(result, k) for k in ("base_commit", "context_sha", "settings")] == ["", "", ""], result
task = evals.Task("bad-base", "Bad base", "Nothing", check="true", base="does-not-exist")
result = evals.run_task(cfg, src, task, evals.Prompts(), eval_run(task))
assert [getattr(result, k) for k in ("base_commit", "context_sha", "settings")] == ["", "", ""], result
with (repo.layer / "config.toml").open("a") as f:
    f.write('\n[setup]\ncommands = ["false"]\n')
task = evals.Task("bad-setup", "Bad setup", "Nothing", check="true")
result = evals.run_task(cfg, src, task, evals.Prompts(), eval_run(task))
assert result.base_commit and result.context_sha == result.settings == "", result
assert "setup" in result.why, result
(repo.layer / "evals/no-check.toml").write_text('title = "No check"\nprompt = "Nothing"\n')
code, out = call("no-check")
row = insights_store.eval_rows(latest())[0]
assert not row["passed"] and [row[k] for k in ("base_commit", "context_sha", "settings")] == ["", "", ""]
before = insights_store.eval_history(repo.name)
(folder / "build.md").unlink()
code, out = call("word-train", prompts=p)
assert code == 1 and "build.md" in out and "all need a prompt" in out, out
assert insights_store.eval_history(repo.name) == before
print("BEHAVIOR_RESULT=" + json.dumps({"contexts": len(set(digests))}))
