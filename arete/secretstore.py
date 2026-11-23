from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from . import config, paths, util
from .store import credentials, db

NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
SCOPE = re.compile(r"global|(?:repo|line|device|connector):[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
ACCOUNT_ID = re.compile(r"[a-z0-9][a-z0-9:_-]{0,63}")
KIND = re.compile(r"[a-z][a-z0-9_]{0,63}")
TAG = re.compile(r"[0-9a-f]{8}")
SINGLE_LINE = {"api_key", "minted_token", "webhook_secret"}
KEY_ITEM = "global/FINGERPRINT_KEY"
KEY_REF = "fingerprint_key_ref"
KEY_LOCAL = "fingerprint_key_local"


class StoreMissing(RuntimeError):
    pass


class StoreError(OSError):
    pass


class FingerprintKeyMissing(StoreError):
    pass


class InternalSecret(StoreError):
    pass


def validate(scope: str, name: str) -> None:
    if not isinstance(scope, str) or not SCOPE.fullmatch(scope):
        raise ValueError("invalid secret scope")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("invalid secret name")


def validate_account_id(label: str) -> None:
    if not isinstance(label, str) or not ACCOUNT_ID.fullmatch(label):
        raise ValueError("invalid account id")


def address(scope: str, name: str) -> str:
    validate(scope, name)
    return f"secret:{scope}/{name}"


def validate_actor(actor):
    if not isinstance(actor, str) or not actor or actor == "owner" or "@" in actor:
        raise ValueError("secret events require a principal id")


def validate_value(value: str, kind: str) -> None:
    if not isinstance(kind, str) or not KIND.fullmatch(kind):
        raise ValueError("invalid secret kind")
    if not isinstance(value, str) or len(value) < 8:
        raise ValueError("secret value must be at least 8 characters")
    if kind in SINGLE_LINE and value.splitlines() != [value]:
        raise ValueError("this secret kind requires a single line")
    try:
        value.encode()
    except UnicodeEncodeError:
        raise ValueError("secret value must be valid UTF-8") from None


@dataclass(frozen=True)
class Reference:
    store: str
    tag: str
    item: str

    def __post_init__(self):
        if not TAG.fullmatch(self.tag):
            raise ValueError("invalid secret instance tag")
        if self.store in {"pve_store", "keychain"}:
            scope, _, name = self.item.partition("/")
            validate(scope, name)
        elif (self.store != "owner_file" or not self.item.isprintable()
              or any(part in {"", ".", ".."} for part in self.item.split("/"))):
            raise ValueError("invalid secret reference")

    @property
    def service(self):
        return f"arete/{self.tag}"

    def __str__(self):
        return f"{self.store}:{self.service}/{self.item}"


def parse_reference(text: str, tag: str) -> Reference:
    store, _, location = text.partition(":")
    prefix = f"arete/{tag}/"
    if not location.startswith(prefix):
        raise ValueError("secret reference belongs to another instance")
    return Reference(store, tag, location.removeprefix(prefix))


def registry_reference(text, tag, item):
    ref = parse_reference(text, tag)
    if ref.store != "owner_file" and ref.item != item:
        raise StoreError("secret reference does not match its address")
    return ref


def configured(settings):
    if settings.store is None:
        raise StoreMissing("secret_store_missing")
    return settings.store


@contextmanager
def file_parent(path: Path, root: Path, *, create=False, private=False):
    if paths.ISOLATED and (paths.confined(root) is None or
                           paths.scratch_root(root) != paths.scratch_root(paths.STATE_DIR)):
        raise StoreError("secret item must stay inside the instance's scratch folder")
    with paths.directory_fd(root, create=create) as fd:
        info = os.fstat(fd)
        if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise StoreError("secret store directory must have mode 0700 and belong to this user")
        with paths.directory_fd(path.parent.relative_to(root), fd, create=create) as parent:
            yield parent


def read_file(path: Path, root: Path, *, private=False) -> bytes | None:
    try:
        with file_parent(path, root, private=private) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise StoreError("secret item must be a private regular file with mode 0600")
                with os.fdopen(fd, "rb", closefd=False) as file:
                    return file.read()
            finally:
                os.close(fd)
    except FileNotFoundError:
        return None


def write_file(path: Path, root: Path, value: bytes, *, private=False) -> None:
    with file_parent(path, root, create=True, private=private) as parent:
        temporary = f".item-{secrets.token_hex(16)}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as file:
                os.fchmod(file.fileno(), 0o600)
                file.write(value)
                file.flush()
                util.flush_fd(file.fileno())
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent)


def remove_file(path: Path, root: Path, *, private=False) -> None:
    with suppress(FileNotFoundError), file_parent(path, root, private=private) as parent:
        os.unlink(path.name, dir_fd=parent)
        os.fsync(parent)


def security(*args, input=None):
    if paths.ISOLATED:
        raise StoreError("the Keychain is unavailable in isolation")
    try:
        return subprocess.run(["security", *args], input=input, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        raise StoreError("Keychain command failed") from None


def keychain_read(ref: Reference) -> bytes | None:
    result = security("find-generic-password", "-s", ref.service, "-a", ref.item, "-w")
    if result.returncode == 44 or (result.returncode and b"could not be found in the keychain" in result.stderr):
        return None
    if result.returncode:
        raise StoreError("could not read Keychain item")
    try:
        return base64.b64decode(result.stdout.strip(), validate=True)
    except binascii.Error:
        raise StoreError("invalid Keychain item encoding") from None


def local_key_path() -> Path:
    return paths.FINGERPRINT_KEY_FILE.parent.resolve() / paths.FINGERPRINT_KEY_FILE.name


def file_location(ref: Reference, settings: config.SecretsConfig) -> tuple[Path, Path]:
    if ref.store == "owner_file":
        root = paths.STATE_DIR.resolve()
        path = root / ref.item
        if str(path).casefold() == str(local_key_path()).casefold():
            raise InternalSecret("This secret is managed by Arete.")
        return path, root
    root = settings.store_dir
    if root is None:
        raise StoreMissing("pve_store is not configured")
    config.validate_store_location(root, paths.CONFIG_FILE.parent)
    return root / ref.tag / ref.item, root


def read_item(ref: Reference, settings: config.SecretsConfig) -> bytes | None:
    if ref.store == "keychain":
        return keychain_read(ref)
    return read_file(*file_location(ref, settings), private=ref.store == "pve_store")


def write_item(ref: Reference, settings: config.SecretsConfig, value: bytes) -> None:
    if ref.store == "keychain":
        command = f"add-generic-password -U -s {ref.service} -a {ref.item} -w ".encode() + base64.b64encode(value)
        result = security("-i", input=command + b"\n")
        if result.returncode or keychain_read(ref) != value:
            raise StoreError("could not write Keychain item")
    elif ref.store == "pve_store":
        write_file(*file_location(ref, settings), value, private=True)
    else:
        raise InternalSecret("This secret is managed by its owning service.")


def require_removable(item, store=None):
    if item == KEY_ITEM or store == "owner_file":
        raise InternalSecret("This secret is managed by its owning service.")


def remove_item(ref: Reference, settings: config.SecretsConfig) -> None:
    require_removable(ref.item, ref.store)
    if ref.store == "keychain":
        if read_item(ref, settings) is not None:
            result = security("delete-generic-password", "-s", ref.service, "-a", ref.item)
            if result.returncode and read_item(ref, settings) is not None:
                raise StoreError("could not remove Keychain item")
    else:
        remove_file(*file_location(ref, settings), private=True)


def checked_key(value: bytes | None) -> bytes:
    if value is None or len(value) != 32:
        raise FingerprintKeyMissing("fingerprint_key_missing")
    return value


def referenced_key(meta, settings):
    ref = parse_reference(meta[KEY_REF], meta["instance_tag"])
    if ref.item != KEY_ITEM or ref.store == "owner_file":
        raise FingerprintKeyMissing("invalid fingerprint key reference")
    try:
        return checked_key(read_item(ref, settings))
    except StoreMissing:
        raise FingerprintKeyMissing("fingerprint_key_missing") from None


def retire_local_key(local, key, reference):
    with suppress(OSError, sqlite3.Error, db.NoDatabase):
        if read_file(local, local.parent) == key and db.schema_metadata().get(KEY_REF) == reference:
            remove_file(local, local.parent)


def ensure_key(c, settings):
    meta = db.metadata(c)
    local = local_key_path()
    if reference := meta.get(KEY_REF):
        key = referenced_key(meta, settings)
        if meta.get(KEY_LOCAL) and local.exists():
            db.after_commit(lambda: retire_local_key(local, key, reference))
        return key
    fallback = read_file(local, local.parent)
    if meta.get(KEY_LOCAL):
        checked_key(fallback)
    elif c.execute("""
        SELECT EXISTS (SELECT 1 FROM accounts UNION ALL SELECT 1 FROM secrets WHERE fingerprint IS NOT NULL)
        """).fetchone()[0]:
        raise FingerprintKeyMissing("fingerprint_key_missing")
    ref = Reference(settings.store, meta["instance_tag"], KEY_ITEM) if settings.store else None
    stored = read_item(ref, settings) if ref else None
    if fallback is not None and stored is not None and fallback != stored:
        raise StoreError("local and configured fingerprint keys differ")
    key = stored if stored is not None else fallback
    if key is None:
        key = secrets.token_bytes(32)
    key = checked_key(key)
    if ref:
        if stored is None:
            write_item(ref, settings, key)
        c.execute("INSERT INTO schema_meta (key, value) VALUES (?, ?)", (KEY_REF, str(ref)))
        if fallback is not None:
            db.after_commit(lambda: retire_local_key(local, key, str(ref)))
    else:
        if fallback is None:
            write_file(local, local.parent, key)
        c.execute("INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, '1')", (KEY_LOCAL,))
    return key


def fingerprint_key(settings: config.SecretsConfig | None = None) -> bytes:
    settings = config.secret_settings() if settings is None else settings
    local = local_key_path()
    try:
        with db.reader() as c:
            meta = db.metadata(c)
            if meta.get(KEY_REF):
                key = referenced_key(meta, settings)
                if not (meta.get(KEY_LOCAL) and local.exists()):
                    return key
            elif not settings.store and meta.get(KEY_LOCAL):
                if (key := read_file(local, local.parent)) is not None:
                    return checked_key(key)
    except db.NoDatabase:
        if not paths.ISOLATED:
            raise
    with db.tx() as c:
        return ensure_key(c, settings)


def digest(key: bytes, *values) -> str:
    return hmac.digest(key, json.dumps(values, sort_keys=True, default=str).encode(), "sha256").hex()


def fingerprint(*values) -> str:
    return digest(fingerprint_key(), *values)


def item_digest(key: bytes, value: bytes) -> str:
    try:
        return digest(key, value.decode())
    except UnicodeDecodeError:
        return digest(key, value)


def get(scope, name):
    validate(scope, name)
    with db.reader() as c:
        return credentials.secret(c, scope, name)


def items():
    with db.reader() as c:
        return credentials.secrets(c)


def history(scope, name):
    validate(scope, name)
    with db.reader() as c:
        row = credentials.secret(c, scope, name)
        return credentials.history(c, row["id"]) if row else []


def read(scope, name) -> bytes | None:
    validate(scope, name)
    item = f"{scope}/{name}"
    if item == KEY_ITEM:
        raise InternalSecret("This secret is managed by Arete.")
    settings = config.secret_settings()
    configured(settings)
    with db.reader() as c:
        row = credentials.secret(c, scope, name)
        if row and row["status"] == "set":
            ref = registry_reference(row["reference"], db.metadata(c)["instance_tag"], item)
            return read_item(ref, settings)
    return None


@contextmanager
def registry_write():
    with db.tx() as c:
        c.execute("SAVEPOINT secret_registry")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK TO secret_registry")
            raise
        finally:
            c.execute("RELEASE secret_registry")


def pending_path(item):
    root = paths.STATE_DIR.resolve() / "secret-writes"
    return root / item, root


def pending_write(item):
    raw = read_file(*pending_path(item), private=True)
    saved = json.loads(raw) if raw is not None else {}
    return raw, saved.get("sets", []), saved.get("removal")


def matching_set(pending, ref, fingerprint):
    return next((record for record in pending if record["reference"] == str(ref)
                 and record["fingerprint"] == fingerprint), None)


def retire_pending(item, raw, expected):
    if raw is None:
        return
    scope, name = item.split("/")

    def committed():
        with suppress(OSError, sqlite3.Error, db.NoDatabase, db.IncompatibleSchema), db.tx() as c:
            if credentials.secret(c, scope, name) == expected and read_file(*pending_path(item), private=True) == raw:
                remove_file(*pending_path(item), private=True)

    db.after_commit(committed)


def save_set(c, scope, name, previous, ref, change):
    actor, at = change["actor"], change["at"]
    rotated = change.get("rotated", bool(previous and previous["status"] == "set"))
    row = credentials.save_secret(c, scope, name, change["kind"], ref.store, str(ref), change["fingerprint"],
                                  actor, at, change["provider"], rotated=rotated)
    credentials.secret_event(c, row, "rotated" if rotated else "set", actor, at)
    credentials.secret_changed(c, row, actor)
    return row


def save_removal(c, scope, name, previous, ref, change):
    actor, at = change["actor"], change["at"]
    row = (credentials.mark_removed(c, previous, at) if previous else
           credentials.removed_secret(c, scope, name, change["kind"], ref.store, str(ref), at))
    credentials.secret_event(c, row, "removed", actor, at, detail=change["id"])
    credentials.secret_changed(c, row, actor)
    return row


def unresolved_removal(c, row, removal):
    return removal is not None and (row is None or not credentials.removal_recorded(c, row["id"], removal["id"]))


def put(scope: str, name: str, value: str, kind: str, actor: str, *, provider=None) -> dict:
    validate(scope, name)
    item = f"{scope}/{name}"
    require_removable(item)
    validate_actor(actor)
    validate_value(value, kind)
    settings = config.secret_settings()
    store = configured(settings)
    key = fingerprint_key(settings)
    with registry_write() as c:
        ref = Reference(store, db.metadata(c)["instance_tag"], item)
        row = credentials.secret(c, scope, name)
        if row:
            require_removable(item, row["store"])
            if row["reference"] != str(ref):
                raise StoreError("secret is registered in a different store")
        _, pending, removal = pending_write(item)
        if unresolved_removal(c, row, removal):
            raise StoreError("secret removal is pending; retry the removal before setting a new value")
        stored = read_item(ref, settings)
        previous = matching_set(pending, ref, item_digest(key, stored)) if pending and stored is not None else None
        change = dict(reference=str(ref), kind=kind, actor=actor, at=util.iso(), fingerprint=digest(key, value),
                      provider=provider, rotated=stored is not None)
        recoverable = [change, previous] if previous and previous["fingerprint"] != change["fingerprint"] else [change]
        raw = json.dumps({"id": secrets.token_hex(16), "sets": recoverable}).encode()
        write_file(*pending_path(item), raw, private=True)
        write_item(ref, settings, value.encode())
        row = save_set(c, scope, name, row, ref, change)
        retire_pending(item, raw, row)
        return row


def remove(scope, name, actor):
    validate(scope, name)
    item = f"{scope}/{name}"
    require_removable(item)
    validate_actor(actor)
    settings = config.secret_settings()
    configured(settings)
    with registry_write() as c:
        row = credentials.secret(c, scope, name)
        if row:
            require_removable(item, row["store"])
        raw, pending, removal = pending_write(item)
        target = row or removal or (pending[0] if pending else None)
        if target is None:
            return None
        ref = registry_reference(target["reference"], db.metadata(c)["instance_tag"], item)
        require_removable(item, ref.store)
        if not unresolved_removal(c, row, removal):
            if (row and row["status"] == "missing" and credentials.last_lifecycle(c, row["id"]) == "removed"
                    and read_item(ref, settings) is None):
                retire_pending(item, raw, row)
                return row
            removal = dict(id=secrets.token_hex(16), reference=str(ref), kind=target["kind"], actor=actor, at=util.iso())
            raw = json.dumps({"sets": pending, "removal": removal}).encode()
            write_file(*pending_path(item), raw, private=True)
        remove_item(ref, settings)
        row = save_removal(c, scope, name, row, ref, removal)
        retire_pending(item, raw, row)
        return row


def check(scope, name, actor="hand"):
    validate(scope, name)
    item = f"{scope}/{name}"
    if item == KEY_ITEM:
        raise InternalSecret("This secret is managed by Arete.")
    validate_actor(actor)
    settings = config.secret_settings()
    configured(settings)
    with registry_write() as c:
        row = credentials.secret(c, scope, name)
        raw, pending, removal = pending_write(item)
        target = row or removal or (pending[0] if pending else None)
        if target is None:
            return None
        ref = registry_reference(target["reference"], db.metadata(c)["instance_tag"], item)
        value = read_item(ref, settings)
        removing = unresolved_removal(c, row, removal)
        if removing and value is None:
            row = save_removal(c, scope, name, row, ref, removal)
            removing = False
        sha = item_digest(fingerprint_key(settings), value) if value is not None else None
        change = matching_set(pending, ref, sha)
        recovered = change is not None and (row is None or row["fingerprint"] != sha or row["status"] != "set")
        if recovered:
            row = save_set(c, scope, name, row, ref, change)
        if row is None and sha is not None:
            raise StoreError("secret item does not match its recovery record")
        if row is not None:
            ok = sha is not None and row["fingerprint"] == sha
            at = util.iso()
            row = credentials.mark_checked(c, row, ok, at, recovered=recovered)
            credentials.secret_event(c, row, "checked", actor, at, ok=ok)
        if not removing and (sha is None or change or row and row["fingerprint"] == sha):
            retire_pending(item, raw, row)
        return row
