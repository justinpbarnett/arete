from __future__ import annotations

import base64
import binascii
import ctypes
import errno
import fcntl
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
from contextlib import ExitStack, closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from . import config, launch_lease, local_paths, paths, util
from .store import credentials, db, writes

NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
SCOPE = re.compile(r"global|(?:repo|line|device|connector):[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
ACCOUNT_ID = re.compile(r"[a-z0-9][a-z0-9:_-]{0,63}")
KIND = re.compile(r"[a-z][a-z0-9_]{0,63}")
TAG = re.compile(r"[0-9a-f]{8}")
SINGLE_LINE = {"api_key", "minted_token", "webhook_secret"}
KEY_ITEM = "global/FINGERPRINT_KEY"
ACL_TYPE_EXTENDED = 0x100
KEY_REF = "fingerprint_key_ref"
KEY_LOCAL = "fingerprint_key_local"
REGISTRY_SYNC = "secret_registry_sync"


class StoreMissing(RuntimeError):
    pass


class StoreError(OSError):
    pass


class CredentialActivationBusy(StoreError):
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


def item_path(scope, name):
    validate(scope, name)
    return f"{scope}/{name}"


def address(scope: str, name: str) -> str:
    return "secret:" + item_path(scope, name)


def validate_actor(actor):
    if not isinstance(actor, str) or not actor or actor == "owner" or "@" in actor:
        raise ValueError("secret events require a principal id")
    try:
        str.encode(actor)
    except UnicodeEncodeError:
        raise ValueError("secret actor must be valid UTF-8") from None


def validate_provider(provider: str | None) -> None:
    if provider is None:
        return
    if not isinstance(provider, str):
        raise ValueError("invalid secret provider")
    try:
        provider.encode()
    except UnicodeEncodeError:
        raise ValueError("secret provider must be valid UTF-8") from None


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


def registry_reference(c, row, item):
    ref = parse_reference(row["reference"], db.metadata(c)["instance_tag"])
    if ref.store != "owner_file" and ref.item != item:
        raise StoreError("secret reference does not match its address")
    return ref


def require_store(settings: config.SecretsConfig) -> None:
    if settings.store is None:
        raise StoreMissing("secret_store_missing")


def store_settings():
    settings = config.secret_settings()
    require_store(settings)
    return settings


def migrate_registry():
    db.migrate_on_demand(db.SECRET_REGISTRY_SCHEMA)


def registry(*, write=False):
    migrate_registry()
    return writes.atomic() if write else db.reader()


def require_no_macos_acl(fd: int) -> None:
    if not paths.ON_MAC:
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        get_acl, free_acl = libc.acl_get_fd_np, libc.acl_free
        get_acl.argtypes = (ctypes.c_int, ctypes.c_int)
        get_acl.restype = ctypes.c_void_p
        free_acl.argtypes = (ctypes.c_void_p,)
        free_acl.restype = ctypes.c_int
    except (AttributeError, OSError):
        raise StoreError("could not verify macOS ACL privacy for the secret store") from None
    ctypes.set_errno(0)
    acl = get_acl(fd, ACL_TYPE_EXTENDED)
    if acl is not None:
        free_acl(acl)
        raise StoreError("secret store files and directories must not have macOS ACLs")
    if ctypes.get_errno() != errno.ENOENT:
        raise StoreError("could not verify macOS ACL privacy for the secret store")


def require_private_directory(fd: int) -> None:
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise StoreError("secret store directory must have mode 0700 and belong to this user")
    require_no_macos_acl(fd)


def require_exact_entry(parent: int, name: str, *, writing=False) -> None:
    if name in os.listdir(parent):
        return
    if writing:
        raise StoreError("secret store entry conflicts with a differently spelled name")
    raise FileNotFoundError(name)


def require_writable_entry(parent: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    require_exact_entry(parent, name, writing=True)


@contextmanager
def durable_directory_fd(path: Path, parent: int | None = None, *, create=False):
    if ".." in path.parts or (parent is not None and path.is_absolute()):
        raise PermissionError("directory traversal refused")
    with ExitStack() as stack:
        if parent is None:
            parent = stack.enter_context(paths.directory_fd(Path("/")))
            parts = path.absolute().parts[1:]
        else:
            parts = path.parts
        for index, part in enumerate(parts):
            created = False
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=parent)
                    created = True
                except FileExistsError:
                    pass
            if created or os.access(".", os.W_OK, dir_fd=parent):
                fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            parent = local_paths.open_directory(part, parent, index < len(parts) - 1)
            stack.callback(os.close, parent)
        yield parent


@contextmanager
def file_parent(path: Path, root: Path, *, create=False, private=False, durable=False):
    if paths.ISOLATED and (paths.confined(root) is None or
                           paths.scratch_root(root) != paths.scratch_root(paths.STATE_DIR)):
        raise StoreError("secret item must stay inside the instance's scratch folder")
    directory = durable_directory_fd if create or durable else paths.directory_fd
    with directory(root, create=create) as fd, ExitStack() as stack:
        parent = path.parent.relative_to(root)
        if private:
            require_private_directory(fd)
            for part in parent.parts:
                child = stack.enter_context(directory(Path(part), fd, create=create))
                require_exact_entry(fd, part, writing=create)
                require_private_directory(child)
                fd = child
        else:
            fd = stack.enter_context(directory(parent, fd, create=create))
        yield fd


def private_file(info: os.stat_result) -> bool:
    return stat.S_IMODE(info.st_mode) == 0o600 and launch_lease.plain(info)


def require_private_file(fd: int) -> None:
    if not private_file(os.fstat(fd)):
        raise StoreError("secret item must be a private regular file with mode 0600")
    require_no_macos_acl(fd)


def read_file_at(parent: int, name: str, *, durable=False) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        require_private_file(fd)
        if durable:
            util.flush_fd(fd)
        with os.fdopen(fd, "rb", closefd=False) as file:
            return file.read()
    finally:
        os.close(fd)


def read_file(path: Path, root: Path, *, private=False, durable=False) -> bytes | None:
    try:
        with file_parent(path, root, private=private, durable=durable) as parent:
            value = None
            with suppress(FileNotFoundError):
                if private:
                    require_exact_entry(parent, path.name)
                value = read_file_at(parent, path.name, durable=durable)
            if durable:
                os.fsync(parent)
            return value
    except FileNotFoundError:
        return None


def write_file(path: Path, root: Path, value: bytes, *, private=False) -> None:
    with file_parent(path, root, create=True, private=private) as parent:
        if private:
            require_writable_entry(parent, path.name)
        temporary = f".item-{secrets.token_hex(16)}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as file:
                os.fchmod(file.fileno(), 0o600)
                require_private_file(file.fileno())
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
        if private:
            require_exact_entry(parent, path.name)
        os.unlink(path.name, dir_fd=parent)
        os.fsync(parent)


def security(*args, input=None):
    program = config.keychain_program()
    if not program:
        raise StoreError("the Keychain is unavailable for this installation")
    try:
        return subprocess.run([program, *args], input=input, capture_output=True, timeout=10, check=False)
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
    config.validate_store_location(root, paths.CONFIG_FILE)
    return root / ref.tag / ref.item, root


def read_item(ref: Reference, settings: config.SecretsConfig, *, durable=False) -> bytes | None:
    if ref.store == "keychain":
        return keychain_read(ref)
    private = ref.store == "pve_store"
    return read_file(*file_location(ref, settings), private=private, durable=private and durable)


def write_item(ref: Reference, settings: config.SecretsConfig, value: bytes) -> None:
    if ref.store == "keychain":
        command = f"add-generic-password -U -s {ref.service} -a {ref.item} -w ".encode() + base64.b64encode(value)
        result = security("-i", input=command + b"\n")
        if result.returncode or keychain_read(ref) != value:
            raise StoreError("could not write Keychain item")
    elif ref.store == "pve_store":
        with credential_activation(publishing=True):
            write_file(*file_location(ref, settings), value, private=True)
    else:
        raise InternalSecret("This secret is managed by its owning service.")


def external_item(scope, name, *, changing_value=False):
    item = item_path(scope, name)
    if item == KEY_ITEM:
        owner = "its owning service" if changing_value else "Arete"
        raise InternalSecret(f"This secret is managed by {owner}.")
    return item


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


def referenced_key(meta, settings, *, expected_store=None):
    ref = (Reference(expected_store, meta.get("instance_tag", ""), KEY_ITEM) if expected_store else
           parse_reference(meta[KEY_REF], meta["instance_tag"]))
    if ref.item != KEY_ITEM or ref.store == "owner_file" or (expected_store and str(ref) != meta[KEY_REF]):
        raise FingerprintKeyMissing("invalid fingerprint key reference")
    try:
        return checked_key(read_item(ref, settings))
    except StoreMissing:
        raise FingerprintKeyMissing("fingerprint_key_missing") from None


@contextmanager
def credential_activation(*, publishing: bool = False, wait: bool = False):
    if sys.platform != "linux":
        yield
        return
    with paths.directory_fd(paths.STATE_DIR.resolve(), create=True) as parent:
        launch_lease.private_directory(parent)
        with launch_lease.opened_file(parent, "credential-activation.lock", create=True) as fd:
            if not publishing or wait:
                fcntl.flock(fd, fcntl.LOCK_EX if publishing else fcntl.LOCK_SH)
            elif not launch_lease.take(fd):
                raise CredentialActivationBusy("Retry credential publication after the local Claude run exits.")
            yield


@contextmanager
def local_key_mount_lease(local: Path):
    with file_parent(local, local.parent) as parent:
        fcntl.flock(parent, fcntl.LOCK_SH)
        info = None
        try:
            with suppress(FileNotFoundError):
                info = os.stat(local.name, dir_fd=parent, follow_symlinks=False)
            yield info
        finally:
            fcntl.flock(parent, fcntl.LOCK_UN)
            if info is not None:
                retire_committed_local_key(local)


def sync_before_cleanup(matches, cleanup, *, timeout=0):
    with closing(db.checked_connection(timeout=timeout)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=EXTRA")
        c.execute("PRAGMA fullfsync=ON")
        c.execute("BEGIN IMMEDIATE")
        if not matches(c):
            return
        c.execute("INSERT INTO schema_meta (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (REGISTRY_SYNC, secrets.token_hex(16)))
        c.execute("COMMIT")
        c.execute("BEGIN IMMEDIATE")
        if matches(c):
            cleanup()


def retire_committed_local_key(local: Path) -> None:
    if db.in_write():
        db.after_commit(lambda: retire_committed_local_key(local))
        return
    with suppress(OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
        if not local.exists():
            return
        with db.reader(streaming=True) as c:
            meta = db.metadata(c)
        if reference := meta.get(KEY_REF):
            settings = config.secret_settings()
            if settings.store == "pve_store":
                key = referenced_key(meta, settings, expected_store="pve_store")
                retire_local_key(local, key, reference, timeout=30 if sys.platform == "linux" else 0)


def retire_local_key(local, key, reference, *, timeout=0):
    def matches(c):
        return local.exists() and db.metadata(c).get(KEY_REF) == reference

    def unlink():
        with file_parent(local, local.parent) as parent:
            if sys.platform == "linux":
                fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if read_file_at(parent, local.name) == key:
                os.unlink(local.name, dir_fd=parent)
                os.fsync(parent)

    with suppress(OSError, sqlite3.Error, db.NoDatabase, db.IncompatibleSchema):
        sync_before_cleanup(matches, unlink, timeout=timeout)


def lookup_key(c, settings, local):
    meta = db.metadata(c)
    if meta.get(KEY_REF):
        return meta, referenced_key(meta, settings)
    if meta.get(KEY_LOCAL):
        return meta, checked_key(read_file(local, local.parent))
    if credentials.has_fingerprints(c):
        raise FingerprintKeyMissing("fingerprint_key_missing")
    return meta, None


def ensure_key(c, settings, local, meta, key, *, recovering=False):
    if reference := meta.get(KEY_REF):
        if meta.get(KEY_LOCAL) and local.exists():
            db.after_commit(lambda: retire_local_key(local, key, reference))
        return key
    fallback = key if key is not None else read_file(local, local.parent, durable=True)
    ref = Reference(settings.store, meta["instance_tag"], KEY_ITEM) if settings.store else None
    stored = read_item(ref, settings, durable=True) if ref else None
    if fallback is not None and stored is not None and fallback != stored:
        raise StoreError("local and configured fingerprint keys differ")
    key = stored if stored is not None else fallback
    if key is None and not recovering:
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
            with credential_activation(publishing=True):
                write_file(local, local.parent, key)
        c.execute("INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, '1')", (KEY_LOCAL,))
    return key


def fingerprint_key(settings: config.SecretsConfig | None = None) -> bytes:
    settings = config.secret_settings() if settings is None else settings
    local = local_key_path()
    migrate_registry()
    try:
        with db.reader() as c:
            meta, key = lookup_key(c, settings, local)
            if meta.get(KEY_REF):
                if not meta.get(KEY_LOCAL) or not local.exists():
                    return key
            elif key is not None and not settings.store:
                return key
    except db.NoDatabase:
        if not paths.ISOLATED:
            raise
    with db.tx() as c:
        meta, key = lookup_key(c, settings, local)
        return ensure_key(c, settings, local, meta, key)


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
    with registry() as c:
        return credentials.secret(c, scope, name)


def items():
    with registry() as c:
        return credentials.secrets(c)


def history(scope, name):
    validate(scope, name)
    with registry() as c:
        row = credentials.secret(c, scope, name)
        events = credentials.history(c, row["id"]) if row else []
    for event in events:
        if (detail := event["detail"]) and detail.startswith("{"):
            operation = json.loads(detail)
            event.update(kind=operation["kind"], provider=operation["provider"], detail=operation["id"])
    return events


def read(scope, name) -> bytes | None:
    item = external_item(scope, name)
    settings = store_settings()
    with registry() as c:
        row = credentials.secret(c, scope, name)
        if row and row["status"] == "set":
            return read_item(registry_reference(c, row, item), settings)
    return None


def pending_path(item):
    root = paths.STATE_DIR.resolve() / "secret-writes"
    return root / item, root


def load_pending(item):
    raw = read_file(*pending_path(item), private=True)
    return raw, json.loads(raw) if raw is not None else {}


def pending_store_dir(ref, settings):
    return str(settings.store_dir) if ref.store == "pve_store" else None


def require_pending_backend(receipt, ref, settings):
    if not receipt:
        return
    store_dir = pending_store_dir(ref, settings)
    if store_dir is not None and "store_dir" not in receipt:
        raise StoreError("pending secret operation has no recorded store directory")
    if receipt.get("store_dir") != store_dir:
        raise StoreError("pending secret operation belongs to a different store directory")
    removal = receipt.get("removal")
    operations = [*receipt.get("confirmed", ()), *receipt.get("sets", ())]
    if removal is not None:
        operations.append(removal)
    for operation in operations:
        if operation["reference"] != str(ref):
            raise StoreError("pending secret operation belongs to another reference")
        validate_actor(operation["actor"])


def save_pending(item, record):
    raw = json.dumps(record).encode()
    write_file(*pending_path(item), raw, private=True)
    return raw


def retire_pending(item, raw, expected):
    if raw is None:
        return
    scope, name = item.split("/")

    def matches(c):
        return credentials.secret(c, scope, name) == expected and read_file(*pending_path(item), private=True) == raw

    def committed():
        with suppress(OSError, sqlite3.Error, db.NoDatabase, db.IncompatibleSchema):
            sync_before_cleanup(matches, lambda: remove_file(*pending_path(item), private=True))

    db.after_commit(committed)


def history_detail(kind, provider, operation_id=None):
    return json.dumps(dict(id=operation_id, kind=kind, provider=provider))


def change_recorded(c, secret_id, operation_id):
    return c.execute("""
        SELECT 1 FROM secret_events WHERE secret_id = ? AND event IN ('set', 'rotated', 'removed')
        AND (detail = ? OR CASE WHEN json_valid(detail) THEN json_extract(detail, '$.id') END = ?)
        """, (secret_id, operation_id, operation_id)).fetchone() is not None


def save_change(c, scope, name, previous, ref, change):
    actor, at = change["actor"], change["at"]
    if change.get("removed"):
        row = (credentials.mark_removed(c, previous, at) if previous else
               credentials.removed_secret(c, scope, name, change["kind"], ref.store, str(ref), at))
        event = "removed"
    else:
        rotated = change.get("rotated", bool(previous and previous["status"] == "set"))
        row = credentials.save_secret(c, scope, name, change["kind"], ref.store, str(ref), change["fingerprint"],
                                      actor, at, change["provider"], rotated=rotated)
        event = "rotated" if rotated else "set"
    kind, provider = row["kind"], row["provider"]
    if event == "removed" and "provider" in change:
        kind, provider = change["kind"], change["provider"]
    credentials.secret_event(c, row, event, actor, at, detail=history_detail(kind, provider, change["id"]))
    credentials.secret_changed(c, row, actor)
    return row


def recover_changes(c, scope, name, row, ref, receipt, fingerprint, *, absent):
    confirmed = list(receipt.get("confirmed", ()))

    def recorded(operation):
        return row is not None and change_recorded(c, row["id"], operation["id"])

    for change in receipt.get("sets", ()):
        if change["fingerprint"] == fingerprint or recorded(change):
            confirmed.append(change)
            break
    removal = receipt.get("removal")
    if removal and (absent or recorded(removal)):
        confirmed.append(removal)
        removal = None
    recovered = False
    for operation in confirmed:
        if not recorded(operation):
            row = save_change(c, scope, name, row, ref, operation)
            recovered = True
    return row, confirmed, removal, recovered


def put(scope: str, name: str, value: str, kind: str, actor: str, *, provider: str | None = None) -> dict:
    item = external_item(scope, name, changing_value=True)
    validate_actor(actor)
    validate_value(value, kind)
    validate_provider(provider)
    settings = config.secret_settings()
    local = local_key_path()
    with registry(write=True) as c:
        meta, key = lookup_key(c, settings, local)
        require_store(settings)
        ref = Reference(settings.store, meta["instance_tag"], item)
        row = credentials.secret(c, scope, name)
        if row:
            require_removable(item, row["store"])
            if row["reference"] != str(ref):
                raise StoreError("secret is registered in a different store")
        _, receipt = load_pending(item)
        require_pending_backend(receipt, ref, settings)
        key = ensure_key(c, settings, local, meta, key, recovering=bool(receipt))
        stored = read_item(ref, settings, durable=bool(receipt))
        fingerprint = item_digest(key, stored) if receipt.get("sets") and stored is not None else None
        row, confirmed, removal, _ = recover_changes(
            c, scope, name, row, ref, receipt, fingerprint, absent=stored is None)
        if removal:
            raise StoreError("secret removal is pending; retry the removal before setting a new value")
        change = dict(id=secrets.token_hex(16), reference=str(ref), kind=kind, actor=actor, at=util.iso(),
                      fingerprint=digest(key, value), provider=provider, rotated=stored is not None)
        # Keep earlier operations recoverable until the outer SQLite transaction commits.
        raw = save_pending(item, dict(store_dir=pending_store_dir(ref, settings), confirmed=confirmed, sets=[change]))
        data = value.encode()
        if stored != data:
            write_item(ref, settings, data)
        row = save_change(c, scope, name, row, ref, change)
        retire_pending(item, raw, row)
        return row


def remove(scope, name, actor):
    return reconcile(scope, name, actor, deleting=True)


def check(scope, name, actor="hand"):
    return reconcile(scope, name, actor)


def reconcile(scope, name, actor, *, deleting=False):
    item = external_item(scope, name, changing_value=deleting)
    validate_actor(actor)
    settings = config.secret_settings()
    local = local_key_path()
    with registry(write=True) as c:
        meta, key = lookup_key(c, settings, local)
        require_store(settings)
        row = credentials.secret(c, scope, name)
        if deleting and row:
            require_removable(item, row["store"])
        raw, receipt = load_pending(item)
        pending = receipt.get("sets", [])
        target = row or receipt.get("removal") or (pending[0] if pending else None)
        if target is None:
            return None
        ref = registry_reference(c, target, item)
        if deleting:
            require_removable(item, ref.store)
        if ref.store not in (settings.store, "owner_file"):
            raise StoreError("secret is registered in a different store")
        require_pending_backend(receipt, ref, settings)
        key = ensure_key(c, settings, local, meta, key, recovering=bool(receipt))
        value = read_item(ref, settings, durable=bool(receipt))
        sha = item_digest(key, value) if value is not None and (pending or not deleting) else None
        row, confirmed, removal, recovered = recover_changes(
            c, scope, name, row, ref, receipt, sha, absent=value is None)
        if deleting:
            if removal is None and not (row and row["status"] == "missing" and value is None and
                                        credentials.last_lifecycle(c, row["id"]) == "removed"):
                metadata = row or target
                removal = dict(id=secrets.token_hex(16), reference=str(ref), kind=metadata["kind"],
                               provider=metadata.get("provider"), actor=actor, at=util.iso(), removed=True)
                raw = save_pending(item, dict(store_dir=pending_store_dir(ref, settings), confirmed=confirmed,
                                              removal=removal))
            if removal is not None:
                remove_item(ref, settings)
                row = save_change(c, scope, name, row, ref, removal)
            retire_pending(item, raw, row)
            return row
        if row is None and sha is not None:
            raise StoreError("secret item does not match its recovery record")
        if row is not None:
            ok = sha is not None and row["fingerprint"] == sha
            at = util.iso()
            row = credentials.mark_checked(c, row, ok, at, recovered=recovered)
            credentials.secret_event(c, row, "checked", actor, at, ok=ok,
                                     detail=history_detail(row["kind"], row["provider"]))
        if removal is None and (not pending or sha is None or recovered or row and row["fingerprint"] == sha):
            retire_pending(item, raw, row)
        return row
