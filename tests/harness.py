from contextlib import nullcontext

from arete.store import db as foundation


def set_task_body(task_id, body, *, connection=None):
    with (foundation.db() if connection is None else nullcontext(connection)) as db:
        db.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, task_id))
