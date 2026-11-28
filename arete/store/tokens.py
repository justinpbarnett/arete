from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import timedelta

from .. import credential_files, util
from . import db, events, principals

TABLES = {"tokens": "kept"}
SESSION_DAYS = 400
TOKEN_ID = credential_files.TAG
LEASE_TOKEN_ID = re.compile(r"L([1-9][0-9]{0,17})")
LEASE_HEADER = re.compile(r"L-([1-9][0-9]{0,17})\.([1-9][0-9]{0,17})")
SESSION_SECONDS = SESSION_DAYS * 86400
TOKEN_ROW = ("SELECT t.*, p.role, p.status AS person_status, p.kind AS person_kind, client.status AS client_status "
             "FROM tokens t JOIN principals p ON p.id = t.principal "
             "LEFT JOIN principals client ON client.id = 'client:' || t.id WHERE t.id = ?")
USE_LOCK = threading.Lock()
PENDING_USES = {}


class InvalidCredential(Exception):
    def __init__(self, code="unauthenticated", **details):
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class Credential:
    id: str
    principal: str
    role: str
    actor: str
    kind: str
    scopes: frozenset[str]
    key_ref: str
    epoch: str
    generation: str
    deadline: str | None
    profile: str | None = None
    commands: frozenset[str] | None = None
    lease_id: int | None = None
    run_id: str | None = None

    def allows(self, scope):
        return ("*" in self.scopes or scope in self.scopes) and (self.role == "owner" or scope == "read")


def new_key(c):
    ref = "file:token-signing-" + secrets.token_hex(16)
    util.write_bytes_atomic(credential_files.key_path(ref), secrets.token_bytes(32))
    c.execute("UPDATE schema_meta SET value = ? WHERE key = 'token_key_ref'", (ref,))
    return ref


def ensure_key(c):
    return db.metadata(c)["token_key_ref"] or new_key(c)


def bump_generation(c):
    c.execute("UPDATE schema_meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'token_generation'")


def invalidate_restore(c, meta, epoch):
    if "token_key_ref" in meta and int(meta["restore_epoch"]) < epoch:
        new_key(c)
        with principals.acting("hand"):
            mark_revoked(c, "restore")


def digest(key, text):
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def kind_of(row):
    return row["kind"] or "token"


def fingerprint(row):
    return hashlib.sha256(f"{row['audience']}:{row['id']}".encode()).hexdigest()[:16]


def public(row):
    if row is None:
        return None
    keys = ("id", "principal", "audience", "label", "created", "expires", "revoked_at", "rotated_from", "last_used_at",
            "last_used_node", "version", "profile", "grace_until", "created_by", "revoke_reason", "spend_cap_nusd")
    return {**{key: row[key] for key in keys}, "kind": kind_of(row), "scopes": json.loads(row["scopes"]),
            "commands": json.loads(row["commands"]) if row["commands"] is not None else None,
            "fingerprint": fingerprint(row)}


def changed(c, row, change):
    fields = {key: row[key] for key in ("id", "label", "principal")}
    events.emit("token.changed", f"token:{row['id']}", cls="durable", ver=row["version"],
                data={**fields, "kind": kind_of(row), "fingerprint": fingerprint(row), "change": change},
                connection=c)


def insert(c, principal, label, scopes, *, kind="token", expires=None, profile=None, commands=None,
           lease_id=None, run_id=None, uses_left=None, spend_cap_nusd=None, rotated_from=None):
    if kind in ("claim", "ask"):
        principal = principals.owner()
    if not c.execute("SELECT 1 FROM principals WHERE id = ? AND kind = 'human' AND status = 'active'",
                     (principal,)).fetchone():
        raise ValueError("A credential needs an active person")
    meta = db.metadata(c)
    key = credential_files.read_key(meta["token_key_ref"])
    identity = secrets.token_hex(4)
    while c.execute("SELECT 1 FROM tokens WHERE id = ?", (identity,)).fetchone():
        identity = secrets.token_hex(4)
    at = util.iso()
    text = f"ar_{meta['instance_tag']}_{identity}_{secrets.token_urlsafe(32)}"
    c.execute("INSERT INTO tokens (id, principal, audience, label, scopes, hash, created, expires, rotated_from, "
              "kind, profile, commands, lease_id, run_id, uses_left, created_by, spend_cap_nusd) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (identity, principal, meta["instance_id"], label, json.dumps(sorted(set(scopes))), digest(key, text),
               at, expires, rotated_from, kind, profile, json.dumps(commands) if commands is not None else None,
               lease_id, run_id, uses_left, principals.current(), spend_cap_nusd))
    if kind in (None, "token"):
        c.execute("INSERT INTO principals (id, kind, name, status, created) VALUES (?, 'client', ?, 'active', ?)",
                  (f"client:{identity}", label, at))
        principals.changed(c, f"client:{identity}")
    row = c.execute("SELECT * FROM tokens WHERE id = ?", (identity,)).fetchone()
    changed(c, row, "rotate" if rotated_from else "issue")
    return public(row), text


def issue(principal, label, scopes, **grants):
    with db.tx() as c:
        return insert(c, principal, label, scopes, **grants)


def row_status(row, now):
    revoked = row["revoked_at"] is not None
    if revoked and row["revoke_reason"] != "expired":
        return "unauthenticated"
    if row["expires"] and row["expires"] <= now:
        return "token_expired"
    if row["grace_until"] and row["grace_until"] <= now:
        return "token_rotated"
    return "unauthenticated" if revoked else None


def deadline_of(row):
    return min((value for value in (row["grace_until"], row["expires"]) if value is not None), default=None)


def active(row, kind):
    return (row is not None and row["person_status"] == "active" and row["person_kind"] == "human"
            and (kind != "token" or row["client_status"] == "active"))


def _resolve_lease_token(text: str, tag: str, lease_num: int, *, lease_header: str | None) -> Credential:
    with db.reader() as c:
        meta = db.metadata(c)
        if tag != meta.get("instance_tag") or not meta.get("token_key_ref"):
            raise InvalidCredential()
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'leases'").fetchone():
            raise InvalidCredential()
        lease_row = c.execute("SELECT * FROM leases WHERE id = ?", (lease_num,)).fetchone()
        if lease_row is None:
            raise InvalidCredential()
        dead_rows = c.execute(
            "SELECT token_hash, dead_at FROM lease_tokens WHERE lease_id = ?",
            (lease_num,),
        ).fetchall()
    expected = digest(credential_files.read_key(meta["token_key_ref"]), text)
    active_hash = lease_row["token_hash"] or ""
    matches_active = bool(active_hash) and hmac.compare_digest(expected, active_hash)
    if not matches_active:
        if any(r["dead_at"] is not None and hmac.compare_digest(expected, r["token_hash"]) for r in dead_rows):
            raise InvalidCredential("lease_fenced", epoch=int(lease_row["epoch"]))
        raise InvalidCredential()
    if lease_row["released"] is not None:
        raise InvalidCredential("lease_fenced", epoch=int(lease_row["epoch"]))
    if not isinstance(lease_header, str) or not (hdr := LEASE_HEADER.fullmatch(lease_header.strip())):
        raise InvalidCredential()
    hdr_lease, hdr_epoch = int(hdr.group(1)), int(hdr.group(2))
    if hdr_lease != lease_num:
        raise InvalidCredential()
    if hdr_epoch != int(lease_row["epoch"]):
        raise InvalidCredential("lease_fenced", epoch=int(lease_row["epoch"]))
    if lease_row["expires"] < util.iso():
        raise InvalidCredential("lease_expired", epoch=int(lease_row["epoch"]))
    return Credential(
        id=f"L-{lease_num}",
        principal=f"lease:{lease_num}",
        role="lease",
        actor=f"lease:{lease_num}",
        kind="lease",
        scopes=frozenset({"read"}),
        key_ref=meta["token_key_ref"],
        epoch=meta["restore_epoch"],
        generation=meta["token_generation"],
        deadline=lease_row["expires"],
        lease_id=lease_num,
        run_id=lease_row["run_id"],
    )


def resolve(text, *, kinds=("token", "session", "claim", "ask"), step=None, lease_header=None):
    if not isinstance(text, str) or len(text) > 512:
        raise InvalidCredential()
    parts = text.split("_", 3)
    if len(parts) != 4 or parts[0] != "ar":
        raise InvalidCredential()
    _, tag, identity, _ = parts
    if lease_match := LEASE_TOKEN_ID.fullmatch(identity):
        if "lease" not in kinds:
            raise InvalidCredential()
        return _resolve_lease_token(text, tag, int(lease_match.group(1)), lease_header=lease_header)
    if not TOKEN_ID.fullmatch(identity):
        raise InvalidCredential()
    with db.reader() as c:
        meta = db.metadata(c)
        if tag != meta["instance_tag"]:
            raise InvalidCredential()
        row = c.execute(TOKEN_ROW, (identity,)).fetchone()
    if row is None or not meta["token_key_ref"]:
        raise InvalidCredential()
    expected = digest(credential_files.read_key(meta["token_key_ref"]), text)
    if not hmac.compare_digest(expected, row["hash"]) or row["audience"] != meta["instance_id"]:
        raise InvalidCredential()
    kind = kind_of(row)
    if kind not in kinds or not active(row, kind):
        raise InvalidCredential()
    if problem := row_status(row, util.iso()):
        raise InvalidCredential(problem)
    person, role = row["principal"], row["role"]
    if kind in ("ask", "claim"):
        if not row["run_id"] or not step:
            raise InvalidCredential()
        actor = f"run:{row['run_id']}/{step}"
    else:
        actor = f"client:{identity}" if kind == "token" else person
    return Credential(identity, person, role, actor, kind, frozenset(json.loads(row["scopes"])),
                      meta["token_key_ref"], meta["restore_epoch"],
                      meta["token_generation"], deadline_of(row), row["profile"],
                      frozenset(json.loads(row["commands"])) if row["commands"] is not None else None)


def rotation_lineage(credential):
    with db.reader() as c:
        rows = c.execute("WITH RECURSIVE lineage(id, parent) AS ("
                         "SELECT id, rotated_from FROM tokens WHERE id = ? UNION "
                         "SELECT t.id, t.rotated_from FROM tokens t JOIN lineage l ON t.id = l.parent "
                         "WHERE t.principal = ? AND coalesce(t.kind, 'token') = 'token' "
                         "AND t.audience = (SELECT value FROM schema_meta WHERE key = 'instance_id')) "
                         "SELECT id, parent FROM lineage", (credential.id, credential.principal)).fetchall()
    lineage = dict(rows)
    root = credential.id
    while (parent := lineage.pop(root, None)) in lineage:
        root = parent
    return root, frozenset(row["id"] for row in rows)


def init_owner():
    with db.tx() as c:
        meta = db.metadata(c)
        person = principals.owner()
        if person == "hand":
            raise OSError("An active owner is required before the dashboard can listen")
        ensure_key(c)
        tag = meta["instance_tag"]
        if text := credential_files.read_client(tag):
            try:
                current = resolve(text, kinds=("token",))
                if current.principal == person and current.allows("*"):
                    return
            except InvalidCredential:
                pass
        with principals.acting(person):
            _, text = insert(c, person, "Owner CLI", ["*"], profile="full")
        credential_files.write_client(tag, text)


def owned_row(c, identity, person):
    return c.execute("SELECT * FROM tokens WHERE id = ? AND principal = ? AND coalesce(kind, 'token') = 'token'",
                     (identity, person)).fetchone()


def get(identity, person):
    with db.reader() as c:
        return public(owned_row(c, identity, person))


def listing(person):
    with db.reader() as c:
        rows = c.execute("SELECT * FROM tokens WHERE principal = ? AND coalesce(kind, 'token') = 'token' "
                         "ORDER BY created DESC, id", (person,))
        return [public(row) for row in rows]


def rotate(identity, person, grace_seconds):
    with db.tx() as c:
        row = owned_row(c, identity, person)
        if row is None:
            return None
        if row["grace_until"] is not None or row_status(row, util.iso()):
            raise InvalidCredential("token_inactive")
        c.execute("UPDATE tokens SET grace_until = ?, version = version + 1 WHERE id = ?",
                  (util.iso(util.now() + timedelta(seconds=grace_seconds)), identity))
        changed(c, owned_row(c, identity, person), "rotate")
        bump_generation(c)
        return insert(c, person, row["label"], json.loads(row["scopes"]), expires=row["expires"],
                      profile=row["profile"], commands=json.loads(row["commands"]) if row["commands"] else None,
                      spend_cap_nusd=row["spend_cap_nusd"], rotated_from=identity)


def flush_uses(c, pending, where="1", params=()):
    ref = db.metadata(c)["token_key_ref"]
    c.executemany(f"UPDATE tokens SET last_used_at = ?, last_used_node = ? "  # noqa: S608
                  f"WHERE id = ? AND revoked_at IS NULL AND ({where}) AND (last_used_at IS NULL OR last_used_at <= ?)",
                  ((at, node, identity, *params, at) for (key, identity), (at, node) in pending.items() if key == ref))


def mark_revoked(c, reason, where="1", params=()):
    with USE_LOCK:
        flush_uses(c, PENDING_USES, where, params)
        rows = c.execute(f"UPDATE tokens SET revoked_at = ?, revoke_reason = ?, version = version + 1 "  # noqa: S608
                         f"WHERE revoked_at IS NULL AND ({where}) RETURNING *", (util.iso(), reason, *params)).fetchall()
    for row in rows:
        changed(c, row, "expiry" if reason == "expired" else "revoke")
    if rows:
        bump_generation(c)
    return rows


def revoke(identity, person):
    with db.tx() as c:
        row = owned_row(c, identity, person)
        if row is not None:
            updated = mark_revoked(c, "revoked", "id = ?", (identity,))
            return public(updated[0] if updated else row)


def revoke_principal(identity):
    with db.tx() as c:
        target = c.execute("SELECT kind FROM principals WHERE id = ?", (identity,)).fetchone()
        if target is None or target["kind"] not in ("human", "client"):
            return None
        field, value = ("id", identity.removeprefix("client:")) if target["kind"] == "client" else ("principal", identity)
        return len(mark_revoked(c, "principal_revoked", f"{field} = ?", (value,)))


def live_session(c):
    return c.execute("SELECT 1 FROM tokens t JOIN principals p ON p.id = t.principal "
                     "WHERE t.kind = 'session' AND t.revoked_at IS NULL AND t.expires > ? "
                     "AND p.status = 'active' LIMIT 1", (util.iso(),)).fetchone() is not None


def session_lifetime(at):
    return util.iso(at + timedelta(days=SESSION_DAYS))


def end_session(identity):
    with db.tx() as c:
        mark_revoked(c, "logout", "id = ? AND kind = 'session'", (identity,))


def stream_status(credential, marker):
    now = util.iso()
    try:
        with db.one_read(timeout=.1) as c:
            meta = db.metadata(c)
            if meta["token_key_ref"] != credential.key_ref or meta["restore_epoch"] != credential.epoch:
                return "unauthenticated", marker
            generation, deadline = marker
            current = meta["token_generation"]
            if current == generation and (deadline is None or now < deadline):
                return None, marker
            row = c.execute(TOKEN_ROW, (credential.id,)).fetchone()
    except (sqlite3.Error, OSError):
        return "unauthenticated", marker
    if not active(row, credential.kind) or row["role"] != credential.role:
        return "unauthenticated", marker
    return row_status(row, now), (current, deadline_of(row))


def note_use(credential, peer):
    at = util.now()
    expires = session_lifetime(at) if credential.kind == "session" else None
    if expires is not None:
        if credential.deadline is None:
            raise InvalidCredential()
        if expires > credential.deadline:
            with db.tx() as c:
                renewed = c.execute("UPDATE tokens SET expires = max(expires, ?) "
                                    "WHERE id = ? AND revoked_at IS NULL AND expires > ? "
                                    "AND (SELECT value FROM schema_meta WHERE key = 'token_key_ref') = ?",
                                    (expires, credential.id, util.iso(at), credential.key_ref))
                if not renewed.rowcount:
                    raise InvalidCredential()
    with USE_LOCK:
        PENDING_USES[credential.key_ref, credential.id] = util.iso(), peer


def maintain():
    with principals.acting("hand"), db.tx() as c:
        with USE_LOCK:
            pending = PENDING_USES.copy()
        flush_uses(c, pending)
        mark_revoked(c, "expired", "expires <= ?", (util.iso(),))
    with USE_LOCK:
        for identity, used in pending.items():
            if PENDING_USES.get(identity) is used:
                del PENDING_USES[identity]
