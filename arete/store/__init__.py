
def prune(days: float = 14, keep_days: float = 30, lines_days: float = 90) -> None:
    from .. import events
    from . import db, incidents, loops, runs, writes

    with db.db() as c:
        events.trim()
        writes.prune()
        runs.prune(c, days, lines_days)
        loops.prune(c, days)
        incidents.prune(c, days, keep_days)
