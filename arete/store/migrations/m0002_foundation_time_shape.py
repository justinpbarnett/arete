PHASE = "backfill"
TIME_GLOB = '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
TIME_COLUMNS = {
    "runs": ("started", "finished", "files_purged", "mined_until", "stop_asked"),
    "steps": ("ts",),
    "feed": ("ts",),
    "loop_state": ("last_fire", "next_due", "updated", "mark_at"),
    "findings": ("first_seen", "last_seen"),
    "events": ("ts",),
    "seen": ("ts",),
    "cursors": ("polled", "last_ok"),
    "queue": ("enqueued", "dead_at"),
    "metrics": ("ts",),
    "approvals": ("created", "decided", "plain_summary_tried", "review_tried", "claimed_until"),
    "messages": ("ts", "taken"),
    "probes": ("last_run", "last_ok"),
    "holds": ("until", "ts"),
    "alerts": ("opened", "last_seen"),
    "proposals": ("created",),
    "evals": ("ts",),
    "feedback": ("created", "recorded"),
    "mined": ("ts",),
    "triage": ("ts", "closed"),
    "triage_approvals": ("ts",),
    "verdicts": ("at", "outcome_at"),
    "research": ("ts", "delivered"),
    "chats": ("created", "updated"),
    "chat_messages": ("ts",),
    "chat_sessions": ("linked", "told"),
    "usage": ("at",),
    "captures": ("created", "updated", "filed"),
    "capture_checks": ("checked",),
    "capture_evidence": ("added",),
    "vms": ("created", "failing_since"),
    "tasks": ("created", "updated"),
    "task_comments": ("ts",),
    "reports": ("saved",),
    "passed_over": ("since",),
    "incidents": ("first_seen", "last_seen", "decided", "retry_at", "landed", "closed"),
    "incident_notes": ("ts",),
    "incident_repairs": ("started", "ended"),
    "incident_aliases": ("ts",),
    "asks": ("asked", "answered", "reminded"),
    "push_subscriptions": ("created", "last_success"),
    "record_pins": ("pinned",),
    "run_purges": ("queued",),
}


def apply(conn):
    for table, columns in TIME_COLUMNS.items():
        for column in columns:
            bad = f'"{column}" IS NOT NULL AND "{column}" != \'\' AND "{column}" NOT GLOB ?'
            if conn.execute(f'SELECT 1 FROM "{table}" WHERE {bad} '
                            f'AND strftime(\'%Y-%m-%dT%H:%M:%SZ\', "{column}") IS NULL LIMIT 1',
                            (TIME_GLOB,)).fetchone():
                raise ValueError(f"{table}.{column}: unreadable timestamp")
            conn.execute(f'UPDATE "{table}" SET "{column}" = strftime(\'%Y-%m-%dT%H:%M:%SZ\', "{column}") '
                         f'WHERE {bad}', (TIME_GLOB,))
        invalid = " OR ".join(f'(NEW."{c}" IS NOT NULL AND NEW."{c}" != \'\' '
                              f'AND NEW."{c}" NOT GLOB \'{TIME_GLOB}\')' for c in columns)
        updates = ", ".join(f'"{c}"' for c in columns)
        for kind, event in (("insert", "INSERT"), ("update", f"UPDATE OF {updates}")):
            conn.execute(f"""CREATE TRIGGER "{table}_time_{kind}" BEFORE {event} ON "{table}"
                WHEN {invalid} BEGIN SELECT RAISE(ABORT, '{table}: timestamp must have UTC iso shape'); END""")
