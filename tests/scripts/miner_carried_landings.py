
from types import SimpleNamespace
from arete import pipeline, outcomes
repo = config.Repo("landing", root, tracker="jira")
cfg = config.Config(repos=[repo])
cfg.jira.landed = {"staging": "Testing", "main": "Done"}
old = made("old", "merged", minutes=2, repo=repo.name, issue="R-1")
new = made("new", "merged", minutes=1, repo=repo.name, issue="R-1")
record.note(old, pr_url="one", landed=landed)
record.note(new, pr_url="two one" if wider else " one  ")
other_ticket = made("other-ticket", "merged", minutes=3, repo=repo.name, issue="R-2")
other_repo = made("other-repo", "merged", minutes=3, repo="elsewhere", issue="R-1")
record.note(other_ticket, pr_url="one two", landed="main")
record.note(other_repo, pr_url="one two", landed="main")
record.move(other_ticket, "done", expect=("merged",))
assert outcomes.streak(repo.name) == 1
assert outcomes.streak(repo.name, since="9999") == 0
comments, moves = [], []
tk = SimpleNamespace(move_to=lambda *args: moves.append(args) or "")
with mock.patch.object(pipeline.tracker, "for_repo", return_value=tk), \
        mock.patch.object(outcomes, "furthest_branch", return_value="staging") as branch, \
        mock.patch.object(outcomes, "uat_steps", return_value="test here"), \
        mock.patch.object(pipeline.Run, "say", lambda run, text: comments.append((run.id, text)) or ""):
    outcomes.sync_landings(cfg)
    expected = int(wider or not landed)
    assert len(comments) == len(moves) == expected, (comments, moves)
    assert all(rid == new for rid, _ in comments), comments
    outcomes.sync_landings(cfg)
    assert len(comments) == len(moves) == expected
    branch.return_value = "main"
    outcomes.sync_landings(cfg)
    assert len(comments) == expected and len(moves) == expected + 1
    outcomes.sync_landings(cfg)
    assert len(moves) == expected + 1
rows = state.runs(kind="pipeline", repo=repo.name)
assert outcomes.carried_by_newer(rows, 1)
for field, value in (("repo", "other"), ("issue", "R-2"), ("pr_url", "")):
    changed = [rows[0], rows[1] | {field: value}]
    assert not outcomes.carried_by_newer(changed, 1), changed
for status in ("rejected", "failed"):
    boundary = made(status, status, minutes=1.5, repo=repo.name, issue="R-2")
    assert outcomes.streak(repo.name) == 1
    record.move(boundary, "done", expect=(status,))
result(streak=outcomes.streak(repo.name), comments=comments, moves=moves)
