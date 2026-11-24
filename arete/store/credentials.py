import json

from .. import events, principals, util

TABLES = {"secrets": "kept", "secret_events": "kept", "accounts": "kept"}


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
