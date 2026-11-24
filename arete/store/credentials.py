import json

from .. import events, principals, util

TABLES = {"secrets": "kept", "secret_events": "kept", "accounts": "kept"}


def secret(c, scope, name):
    row = c.execute("SELECT * FROM secrets WHERE scope = ? AND name = ?", (scope, name)).fetchone()
    return dict(row) if row else None


def secrets(c):
    return [dict(row) for row in c.execute("SELECT * FROM secrets ORDER BY scope, name")]


def history(c, secret_id):
    return [dict(row) for row in c.execute(
        "SELECT id, event, actor, ok, fingerprint_after, detail, at FROM secret_events WHERE secret_id = ? ORDER BY id",
        (secret_id,))]


def last_lifecycle(c, secret_id):
    row = c.execute("""
        SELECT event FROM secret_events WHERE secret_id = ? AND event IN ('set', 'rotated', 'removed')
        ORDER BY id DESC LIMIT 1
        """, (secret_id,)).fetchone()
    return row["event"] if row else None


def removal_recorded(c, secret_id, receipt):
    return c.execute("SELECT 1 FROM secret_events WHERE secret_id = ? AND event = 'removed' AND detail = ?",
                     (secret_id, receipt)).fetchone() is not None


def save_secret(c, scope, name, kind, store, reference, fingerprint, actor, at, provider, *, rotated=False):
    return dict(c.execute("""
        INSERT INTO secrets (scope, name, kind, store, reference, fingerprint, status, set_by, set_at, provider, rotated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'set', ?, ?, ?, ?)
        ON CONFLICT (name, scope) DO UPDATE SET
            kind = excluded.kind, fingerprint = excluded.fingerprint, status = 'set', missing_since = NULL,
            set_by = excluded.set_by, set_at = excluded.set_at, version = secrets.version + 1,
            provider = COALESCE(excluded.provider, secrets.provider),
            rotated_at = COALESCE(excluded.rotated_at, secrets.rotated_at)
        RETURNING *
        """, (scope, name, kind, store, reference, fingerprint, actor, at, provider, at if rotated else None)).fetchone())


def mark_checked(c, row, ok, at, *, recovered=False):
    return dict(c.execute("""
        UPDATE secrets SET checked_at = ?, check_ok = ?, status = ?,
            missing_since = CASE WHEN ? THEN NULL ELSE COALESCE(missing_since, ?) END,
            version = version + ? WHERE id = ? RETURNING *
        """, (at, ok, "set" if ok else "missing", ok, at, int(not recovered), row["id"])).fetchone())


def mark_removed(c, row, at):
    return dict(c.execute("""
        UPDATE secrets SET status = 'missing', missing_since = COALESCE(missing_since, ?), version = version + 1
        WHERE id = ? RETURNING *
        """, (at, row["id"])).fetchone())


def removed_secret(c, scope, name, kind, store, reference, at):
    return dict(c.execute("""
        INSERT INTO secrets (scope, name, kind, store, reference, status, missing_since)
        VALUES (?, ?, ?, ?, ?, 'missing', ?) RETURNING *
        """, (scope, name, kind, store, reference, at)).fetchone())


def secret_event(c, row, event, actor, at, *, ok=True, detail=None):
    c.execute("""
        INSERT INTO secret_events (secret_id, event, actor, ok, fingerprint_after, reference_after, detail, at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (row["id"], event, actor, ok, row["fingerprint"], row["reference"], detail, at))


def secret_changed(c, row, actor):
    with principals.acting(actor):
        events.emit("credential.changed", f"secret:{row['scope']}/{row['name']}", cls="durable", ver=row["version"],
                    data={k: row[k] for k in ("name", "scope", "fingerprint")}, connection=c)


def account_rows(c):
    return [dict(row) for row in c.execute("""
        SELECT a.id, a.provider AS service, a.mode, a.usage, a.holder, s.name AS secret_name
        FROM accounts a LEFT JOIN secrets s ON s.id = a.secret_id ORDER BY a.id
        """)]


def reconcile_logins(c, provider, mode, identities, holder):
    rows = c.execute("""
        SELECT id, identity, fingerprint, version FROM accounts WHERE provider = ? AND mode = ? AND secret_id IS NULL
          AND identity IN (SELECT value FROM json_each(?)) ORDER BY created, rowid
        """, (provider, mode, json.dumps(list(identities)))).fetchall()
    if not rows:
        return None
    kept, *duplicates = rows
    for duplicate in duplicates:
        changed = c.execute("UPDATE secrets SET account_id = ?, version = version + 1 "
                            "WHERE account_id = ? RETURNING *", (kept["id"], duplicate["id"])).fetchall()
        for secret in changed:
            secret_changed(c, secret, holder)
        c.execute("DELETE FROM accounts WHERE id = ?", (duplicate["id"],))
        events.emit("credential.changed", f"account:{duplicate['id']}", cls="durable", ver=duplicate["version"] + 1,
                    data={"id": duplicate["id"], "fingerprint": None}, connection=c)
    return kept


def save_account(c, label, provider, mode, holder, usage, identity, fingerprint, secret_id):
    from .. import secretstore

    previous = c.execute("SELECT * FROM accounts WHERE provider = ? AND mode = ? AND identity = ?",
                         (provider, mode, identity)).fetchone()
    fields = (holder, fingerprint, secret_id)
    if previous:
        if tuple(previous[field] for field in ("holder", "fingerprint", "secret_id")) == fields:
            return dict(previous)
        row = c.execute("""
            UPDATE accounts SET holder = ?, fingerprint = ?, secret_id = ?, version = version + 1
            WHERE id = ? RETURNING *
            """, (*fields, previous["id"])).fetchone()
    else:
        ids = {row[0] for row in c.execute("""
            SELECT id FROM accounts WHERE id = ? OR id GLOB ?
            UNION SELECT substr(subject, 9) FROM events_log
            WHERE kind = 'credential.changed' AND (subject = ? OR subject GLOB ?)
            """, (label, f"{label}:[0-9]*", f"account:{label}", f"account:{label}:[0-9]*"))}
        label = next(candidate for candidate in (label, *(f"{label}:{i}" for i in range(2, len(ids) + 2)))
                     if candidate not in ids)
        secretstore.validate_account_id(label)
        row = c.execute("""
            INSERT INTO accounts (id, provider, identity, mode, holder, usage, fingerprint, secret_id, created)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *
            """, (label, provider, identity, mode, holder, usage, fingerprint, secret_id, util.iso())).fetchone()
    if not previous or previous["fingerprint"] != fingerprint:
        events.emit("credential.changed", f"account:{row['id']}", cls="durable", ver=row["version"],
                    data={"id": row["id"], "fingerprint": fingerprint}, connection=c)
    return dict(row)
