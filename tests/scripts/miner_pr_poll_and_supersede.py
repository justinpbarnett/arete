
from types import SimpleNamespace
from arete import pipeline, outcomes
repo = config.Repo("race", root, github="example/race")
cfg = config.Config(repos=[repo])
logs = []
run = SimpleNamespace(repo=repo, tid="R-1", log=logs.append)
first = made("first", "pr", minutes=1, repo=repo.name, issue=run.tid)
second = made("second", "pr", minutes=2, repo=repo.name, issue=run.tid)
record.note(first, pr_url="one two")
record.note(second, pr_url="three")
finished = state.run_get(first)["finished"]
closed = "2099-01-01T00:00:00-05:00"
def view(url):
    if url == "two":
        pipeline.supersede_older_runs(run, ["one", "two"])
    return {"state": "MERGED", "mergedAt": closed, "closedAt": closed}
with mock.patch.object(pipeline.gh, "pr_view", side_effect=view), \
        mock.patch.object(outcomes, "record_feedback"):
    outcomes.sync_outcomes(cfg)
row = state.run_get(first)
assert row["status"] == "superseded" and row["finished"] == finished, row
third = made("third", "pr", minutes=0, repo=repo.name, issue=run.tid)
record.note(third, pr_url="three")
original = state.open_runs
def snapshot(*args):
    rows = original(*args)
    outcomes.sync_outcomes(cfg)
    return rows
logs.clear()
with mock.patch.object(state, "open_runs", side_effect=snapshot), \
        mock.patch.object(pipeline.gh, "pr_view", return_value=view("three")), \
        mock.patch.object(outcomes, "record_feedback"):
    pipeline.supersede_older_runs(run, ["three"])
assert state.run_get(third)["status"] == "merged"
assert state.run_get(third)["finished"] == "2099-01-01T05:00:00Z"
assert not logs, logs
result(status=row["status"], finished=row["finished"], logs=logs)
