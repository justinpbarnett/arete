
def prune(days: float = 14, keep_days: float = 30, lines_days: float = 90) -> None:
    from . import db, incidents, loops, runs

    with db.db() as c:
        runs.prune(c, days, lines_days)
        loops.prune(c, days)
        incidents.prune(c, days, keep_days)
