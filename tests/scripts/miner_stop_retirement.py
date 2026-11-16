
from arete import control, pipeline, tasks
repo = config.Repo("stopping", root)
cfg = config.Config(repos=[repo])
rid = made("stop", "needs-info", repo=repo.name, issue="R-1")
row = state.run_get(rid)
state.ask_add("ask", rid, repo.name, "R-1", "build", "call", "why", "tried", [], {})
folder = root / "kept"
folder.mkdir()
assert record.move(rid, "merged", expect=("needs-info",))
with mock.patch.object(tasks, "kept_folder_removed") as remove:
    try:
        tasks.let_go(cfg, repo, row, "stopped")
    except control.Refused:
        pass
    else:
        raise AssertionError("stale stop accepted")
    remove.assert_not_called()
assert state.run_get(rid)["status"] == "merged"
assert state.ask_get("ask")["status"] == "open" and folder.exists()
assert record.move(rid, "needs-info", expect=("merged",))
def remove(*args):
    folder.rmdir()
    return True
with mock.patch.object(tasks, "kept_folder_removed", side_effect=remove), \
        mock.patch.object(pipeline.tracker, "for_ticket"), \
        mock.patch.object(pipeline.Run, "give_up"), mock.patch.object(pipeline, "write_report"):
    answer = tasks.let_go(cfg, repo, row, "stopped")
assert answer["state"] == state.run_get(rid)["status"] == "stopped"
assert state.ask_get("ask")["status"] == "stopped" and not folder.exists()
result(status=answer["state"])
