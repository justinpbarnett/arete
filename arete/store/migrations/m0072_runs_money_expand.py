from __future__ import annotations

PHASE = "expand"

_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("steps", "cost_nusd", "INTEGER"),
    ("steps", "paid_nusd", "INTEGER"),
    ("steps", "price_version", "TEXT"),
    ("steps", "billing_context", "TEXT"),
    ("runs", "cost_nusd", "INTEGER"),
    ("evals", "cost_nusd", "INTEGER"),
    ("triage_verdicts", "cost_nusd", "INTEGER"),
    ("research", "cost_nusd", "INTEGER"),
    ("chats", "spent_nusd", "INTEGER"),
    ("chat_messages", "cost_nusd", "INTEGER"),
    ("chat_messages", "paid_nusd", "INTEGER"),
    ("captures", "jev_cost_nusd", "INTEGER"),
    ("captures", "inspect_cost_nusd", "INTEGER"),
    ("turn_index", "paid_nusd", "INTEGER"),
    ("turn_index", "auth_mode", "TEXT"),
    ("turn_index", "price_version", "TEXT"),
    ("turn_index", "cache_write", "INTEGER NOT NULL DEFAULT 0"),
    ("turn_index", "billing_context", "TEXT"),
)

_MIRRORS: tuple[tuple[str, str, str, str], ...] = (
    ("steps", "cost_usd", "cost_nusd", "id"),
    ("runs", "cost_usd", "cost_nusd", "id"),
    ("evals", "cost_usd", "cost_nusd", "rowid"),
    ("triage_verdicts", "cost", "cost_nusd", "id"),
    ("research", "cost", "cost_nusd", "ref"),
    ("chats", "spent", "spent_nusd", "id"),
    ("chat_messages", "cost", "cost_nusd", "id"),
    ("chat_messages", "paid_usd", "paid_nusd", "id"),
    ("captures", "jev_cost_usd", "jev_cost_nusd", "id"),
    ("captures", "inspect_cost_usd", "inspect_cost_nusd", "id"),
)


def apply(conn) -> None:
    cols_by_table: dict[str, set[str]] = {}
    for table, col, ddl in _COLUMNS:
        if table not in cols_by_table:
            cols_by_table[table] = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols_by_table[table]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            cols_by_table[table].add(col)

    for table, real_col, nusd_col, pk in _MIRRORS:
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_{nusd_col}_mirror_ins "  # noqa: S608
            f"AFTER INSERT ON {table} "
            f"WHEN NEW.{nusd_col} IS NULL AND NEW.{real_col} IS NOT NULL "
            f"BEGIN "
            f"  UPDATE {table} SET {nusd_col} = CAST(round(NEW.{real_col} * 1e9) AS INTEGER) "
            f"  WHERE {pk} = NEW.{pk}; "
            f"END"
        )
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_{nusd_col}_mirror_upd "  # noqa: S608
            f"AFTER UPDATE OF {real_col} ON {table} "
            f"WHEN NEW.{nusd_col} IS OLD.{nusd_col} "
            f"AND (NEW.{real_col} IS NOT OLD.{real_col} "
            f"OR (NEW.{nusd_col} IS NULL AND NEW.{real_col} IS NOT NULL)) "
            f"BEGIN "
            f"  UPDATE {table} SET {nusd_col} = CASE WHEN NEW.{real_col} IS NULL THEN NULL "
            f"  ELSE CAST(round(NEW.{real_col} * 1e9) AS INTEGER) END "
            f"  WHERE {pk} = NEW.{pk}; "
            f"END"
        )
