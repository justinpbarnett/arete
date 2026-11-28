from contextlib import nullcontext

from arete.store import db as foundation
from arete.store import tasks as task_store


def set_task_body(task_id, body, *, connection=None):
    with (foundation.db() if connection is None else nullcontext(connection)) as db:
        num = task_store.parse_task_num(task_id, db)
        if task_store.is_contracted(db) and num is not None:
            db.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, num))
            task_store.TaskStore.sync_body_links(db, num, body)
        else:
            db.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, task_id))
            if num is not None and task_store.has_identity_expand(db):
                task_store.TaskStore.sync_body_links(db, num, body)
