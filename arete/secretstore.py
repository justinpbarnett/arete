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

from . import config, launch_lease, owner_asks, paths, util
from .store import credentials, db, principals, writes

NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
REPO_NAME = re.compile(owner_asks.NAME_PATTERN)
SCOPE = re.compile(r"global|(?:repo|line|device|connector):[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
ACCOUNT_ID = re.compile(r"[a-z0-9][a-z0-9:_-]{0,63}")
KIND = re.compile(r"[a-z][a-z0-9_]{0,63}")
TAG = re.compile(r"[0-9a-f]{8}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
ROLE_PATTERN = re.compile(r"[a-z][a-z0-9_-]*:(?:[A-Za-z0-9_.*-]+|\*)")
SINGLE_LINE = {"api_key", "minted_token", "webhook_secret"}
KEY_ITEM = "global/FINGERPRINT_KEY"
VERIFICATION_KEY_NAME = "VERIFICATION_SIGNING_KEY"
VERIFICATION_KEY_ITEM = f"global/{VERIFICATION_KEY_NAME}"
VERIFICATION_KEY_PATH = "verification-authority/signing.secret"
ACL_TYPE_EXTENDED = 0x100
KEY_REF = "fingerprint_key_ref"
KEY_PROOF = "fingerprint_key_proof"
KEY_STORE_DIR = "fingerprint_key_store_dir"
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
    if not valid_name(scope, name):
        raise ValueError("invalid secret name")


def valid_name(scope: str, name: str) -> bool:
    pattern = REPO_NAME if scope.startswith("repo:") else NAME
    return isinstance(name, str) and pattern.fullmatch(name) is not None


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
    from . import vault

    if not isinstance(kind, str) or not KIND.fullmatch(kind):
        raise ValueError("invalid secret kind")
    if not isinstance(value, str) or len(value) < vault.SHORTEST:
        raise ValueError(f"secret value must be at least {vault.SHORTEST} characters")
    if kind in SINGLE_LINE and (value.splitlines() != [value] or "\0" in value):
        raise ValueError("this secret kind requires a single line")
    try:
        value.encode()
    except UnicodeEncodeError:
        raise ValueError("secret value must be valid UTF-8") from None


def validate_expose(expose) -> list[dict]:
    if isinstance(expose, str):
        try:
            expose = json.loads(expose)
        except ValueError:
            raise ValueError("expose must be a JSON list of {env, roles} entries") from None
    if not isinstance(expose, (list, tuple)):
        raise ValueError("expose must be a list of {env, roles} entries")
    normalized = []
    for entry in expose:
        if not isinstance(entry, dict) or set(entry) != {"env", "roles"}:
            raise ValueError("each expose entry must have env and roles")
        env_name, roles = entry["env"], entry["roles"]
        if not isinstance(env_name, str) or not ENV_NAME.fullmatch(env_name):
            raise ValueError("invalid expose env name")
        if (not isinstance(roles, (list, tuple)) or not roles
                or not all(isinstance(r, str) and ROLE_PATTERN.fullmatch(r) for r in roles)):
            raise ValueError("expose roles must be a non-empty list of role patterns")
        normalized.append({"env": env_name, "roles": list(roles)})
    return normalized


def expose_rules(row: dict) -> list[dict]:
    raw = row.get("expose_rules")
    if raw is None:
        if row["scope"].startswith("repo:"):
            return [{"env": row["name"], "roles": ["step:*", "runner:*"]}]
        return []
    try:
        return validate_expose(raw)
    except ValueError:
        return []


def role_matches(role: str, pattern: str) -> bool:
    return pattern == role or (pattern.endswith("*") and role.startswith(pattern[:-1]))


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


def confined_directory(root: Path, *, create=False, durable=False):
    if paths.ISOLATED and (paths.confined(root) is None or
                           paths.scratch_root(root) != paths.scratch_root(paths.STATE_DIR)):
        raise StoreError("secret item must stay inside the instance's scratch folder")
    return paths.directory_fd(root, create=create, durable=durable)


@contextmanager
def file_parent(path: Path, root: Path, *, create=False, private=False, durable=False):
    durable = create or durable
    with confined_directory(root, create=create, durable=durable) as fd, ExitStack() as stack:
        parent = path.parent.relative_to(root)
        if private:
            require_private_directory(fd)
            for part in parent.parts:
                child = stack.enter_context(paths.directory_fd(Path(part), fd, create=create, durable=durable))
                require_exact_entry(fd, part, writing=create)
                require_private_directory(child)
                fd = child
        else:
            fd = stack.enter_context(paths.directory_fd(parent, fd, create=create, durable=durable))
        yield fd


def private_file(info: os.stat_result) -> bool:
    return stat.S_IMODE(info.st_mode) == 0o600 and launch_lease.plain(info)


def require_private_file(fd: int) -> None:
    info = os.fstat(fd)
    if info.st_nlink == 0:
        raise FileNotFoundError("secret item was removed")
    if not private_file(info):
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


def write_file(path: Path, root: Path, value: bytes, *, private=False, create_only=False) -> None:
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
            if create_only:
                os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.unlink(temporary, dir_fd=parent)
            else:
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
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "")}
    try:
        return subprocess.run([program, *args], input=input, capture_output=True, timeout=10, check=False, env=env)
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


def write_item(ref: Reference, settings: config.SecretsConfig, value: bytes, *, create_only=False) -> None:
    if ref.store == "keychain":
        update = "" if create_only else " -U"
        command = f"add-generic-password{update} -s {ref.service} -a {ref.item} -w ".encode() + base64.b64encode(value)
        result = security("-i", input=command + b"\n")
        if result.returncode or keychain_read(ref) != value:
            raise StoreError("could not write Keychain item")
    elif ref.store == "pve_store":
        with credential_activation(publishing=True):
            write_file(*file_location(ref, settings), value, private=True, create_only=create_only)
    else:
        raise InternalSecret("This secret is managed by its owning service.")


def external_item(scope, name, *, changing_value=False):
    item = item_path(scope, name)
    if item in (KEY_ITEM, VERIFICATION_KEY_ITEM):
        owner = "its owning service" if changing_value else "Arete"
        raise InternalSecret(f"This secret is managed by {owner}.")
    return item


def require_removable(item, store=None):
    if item in (KEY_ITEM, VERIFICATION_KEY_ITEM) or store == "owner_file":
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


def store_directory(ref, settings):
    if ref.store == "pve_store" and settings.store_dir is not None:
        return str(settings.store_dir.absolute())
    return None


def key_proof(key, ref, directory):
    identity = json.dumps([str(ref), directory]).encode()
    return hmac.digest(key, b"arete/fingerprint-key-provenance/v1\0" + identity, "sha256").hex()


def checked_proof(proof):
    if not isinstance(proof, str) or not re.fullmatch(r"[0-9a-f]{64}", proof):
        raise FingerprintKeyMissing("fingerprint_key_missing")
    return proof


def verify_proof(expected, proof):
    if not hmac.compare_digest(expected, checked_proof(proof)):
        raise StoreError("fingerprint key does not match its recorded provenance")


def verify_key_proof(key, ref, directory, proof):
    verify_proof(key_proof(key, ref, directory), proof)


def referenced_key(meta, settings):
    if settings.store is None or KEY_PROOF not in meta or KEY_STORE_DIR not in meta:
        raise FingerprintKeyMissing("fingerprint_key_missing")
    ref = parse_reference(meta[KEY_REF], meta["instance_tag"])
    if ref.item != KEY_ITEM or ref.store == "owner_file":
        raise FingerprintKeyMissing("fingerprint_key_missing")
    if ref.store != settings.store:
        raise StoreError("fingerprint key belongs to a different store backend")
    directory = store_directory(ref, settings)
    if ref.store == "pve_store" and directory is None:
        raise FingerprintKeyMissing("fingerprint_key_missing")
    if meta[KEY_STORE_DIR] != (directory or ""):
        raise StoreError("fingerprint key belongs to a different store directory")
    try:
        key = checked_key(read_item(ref, settings))
    except StoreMissing:
        raise FingerprintKeyMissing("fingerprint_key_missing") from None
    verify_key_proof(key, ref, directory, meta[KEY_PROOF])
    return key


@contextmanager
def credential_activation(*, publishing: bool = False, wait: bool = False, state: Path | None = None):
    if sys.platform != "linux":
        yield
        return
    root = state or (paths.STATE_DIR if paths.ISOLATED else paths.STATE_DIR.resolve())
    with confined_directory(root, create=True) as parent:
        launch_lease.private_directory(parent)
        with launch_lease.opened_file(parent, "credential-activation.lock", create=True) as fd:
            if not publishing or wait:
                fcntl.flock(fd, fcntl.LOCK_EX if publishing else fcntl.LOCK_SH)
            elif not launch_lease.take(fd):
                raise CredentialActivationBusy("Retry credential publication after the active Linux workers exit.")
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
            cleanup(c)


def retire_committed_local_key(local: Path) -> None:
    if db.in_write():
        db.after_commit(lambda: retire_committed_local_key(local))
        return
    with suppress(OSError, ValueError, RuntimeError):
        settings = config.secret_settings()
        if settings.store == "pve_store":
            retire_local_key(local, settings, wait_for_writer=sys.platform == "linux")


def retire_local_key(local, settings, reference=None, *, wait_for_writer=False):
    key = None

    def matches(c):
        nonlocal key
        meta = db.metadata(c)
        current = meta.get(KEY_REF)
        if not local.exists() or not current or (reference is not None and reference != current):
            return False
        try:
            active = config.secret_settings()
        except (OSError, KeyError, TypeError, ValueError, RuntimeError):
            return False
        if active != settings:
            return False
        key = referenced_key(meta, active)
        return read_file(local, local.parent) == key

    def unlink(_c):
        with file_parent(local, local.parent) as parent:
            if sys.platform == "linux":
                fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if read_file_at(parent, local.name) == key:
                os.unlink(local.name, dir_fd=parent)
                os.fsync(parent)

    while True:
        try:
            if not local.exists():
                return
            sync_before_cleanup(matches, unlink, timeout=5 if wait_for_writer else 0)
        except sqlite3.OperationalError as error:
            # The last mount lease has closed. Wait for the writer without holding a lease or transaction.
            if wait_for_writer and (getattr(error, "sqlite_errorcode", 0) & 0xff) == sqlite3.SQLITE_BUSY:
                continue
        except (OSError, ValueError, KeyError, sqlite3.Error, db.NoDatabase, db.IncompatibleSchema):
            pass
        return


def key_initialization_path(*, configured):
    root = pending_root()
    return root / ("key-store" if configured else "key-local"), root


@dataclass(frozen=True)
class KeyInitialization:
    instance_id: str
    reference: str | None = None
    directory: str | None = None

    @property
    def identity(self):
        return [self.instance_id, self.reference, self.directory]

    @property
    def location(self):
        return key_initialization_path(configured=self.reference is not None)

    def proof(self, key):
        return hmac.digest(key, b"arete/fingerprint-key-initialization/v1\0" + json.dumps(self.identity).encode(),
                           "sha256").hex()

    def verify(self, value, proof):
        key = checked_key(value)
        verify_proof(self.proof(key), proof)
        return key

    def receipt(self, proof):
        return json.dumps([self.identity, proof]).encode()

    def pending(self):
        raw = read_file(*self.location, private=True, durable=True)
        if raw is None:
            return None
        try:
            identity, proof = json.loads(raw)
        except (ValueError, TypeError):
            raise StoreError("invalid fingerprint key initialization receipt") from None
        if identity != self.identity:
            raise StoreError("pending fingerprint key initialization belongs to a different installation or store")
        return checked_proof(proof)

    def record(self, key):
        proof = self.proof(key)
        write_file(*self.location, self.receipt(proof), private=True)
        return proof

    def retire(self, key, binding):
        location = self.location
        try:
            if not location[0].exists():
                return
        except OSError:
            return
        raw = self.receipt(self.proof(key))

        def matches(c):
            meta = db.metadata(c)
            return (meta.get("instance_id") == self.instance_id
                    and all(meta.get(field) == value for field, value in binding.items())
                    and read_file(*location, private=True) == raw)

        def committed():
            with suppress(OSError, sqlite3.Error, db.NoDatabase, db.IncompatibleSchema):
                sync_before_cleanup(matches, lambda _c: remove_file(*location, private=True))

        db.after_commit(committed)


def lookup_key(c, settings, local, meta):
    if meta.get(KEY_REF):
        return meta, referenced_key(meta, settings)
    if KEY_REF in meta or KEY_PROOF in meta or KEY_STORE_DIR in meta:
        raise FingerprintKeyMissing("fingerprint_key_missing")
    if KEY_LOCAL in meta:
        if c.execute("SELECT 1 FROM secrets WHERE store != 'owner_file' "
                     "AND fingerprint IS NOT NULL LIMIT 1").fetchone():
            raise FingerprintKeyMissing("fingerprint_key_missing")
        initialization = KeyInitialization(meta["instance_id"])
        value = read_file(local, local.parent)
        if value is None and not paths.ISOLATED and not db.in_write():
            # The WAL reader can outlive publication of the same key in the configured store.
            with closing(db.read_connection()) as latest, db.read_snapshot(latest):
                published = db.metadata(latest)
            if (published.get(KEY_REF) and published.get("instance_id") == meta["instance_id"]
                    and published.get(KEY_LOCAL) == meta[KEY_LOCAL]):
                active = settings if settings.store else config.secret_settings()
                value = referenced_key(published, active)
                return published, initialization.verify(value, meta[KEY_LOCAL])
        return meta, initialization.verify(value, meta[KEY_LOCAL])
    if credentials.has_fingerprints(c):
        raise FingerprintKeyMissing("fingerprint_key_missing")
    return meta, None


def key_context(c, settings, local, meta=None):
    if meta is None:
        meta = db.metadata(c)
    pending = pending_journals(meta["instance_tag"], settings)
    meta, key = lookup_key(c, settings, local, meta)
    if pending:
        ref = Reference(settings.store, meta["instance_tag"], KEY_ITEM)
        if not meta.get(KEY_REF):
            stored = checked_key(read_item(ref, settings, durable=db.in_write()))
            if key is not None and not hmac.compare_digest(key, stored):
                raise StoreError("local and configured fingerprint keys differ")
            key = stored
        directory = store_directory(ref, settings)
        for _, receipt in pending.values():
            verify_key_proof(key, ref, directory, receipt.get("key_proof"))
    return meta, key, pending


def ensure_key(c, settings, local, meta, key, *, recovering=False):
    if reference := meta.get(KEY_REF):
        initialization = KeyInitialization(meta["instance_id"], reference, meta[KEY_STORE_DIR] or None)
        initialization.retire(key, {field: meta[field] for field in (KEY_REF, KEY_STORE_DIR, KEY_PROOF)})
        if meta.get(KEY_LOCAL) and local.exists():
            db.after_commit(lambda: retire_local_key(local, settings, reference))
        return key
    local_init = KeyInitialization(meta["instance_id"])
    local_proof = meta[KEY_LOCAL] if KEY_LOCAL in meta else local_init.pending()
    fallback = read_file(local, local.parent, durable=True)
    if fallback is not None and local_proof is None:
        raise StoreError("local fingerprint key has no provenance; operator reconciliation is required")
    if fallback is not None or KEY_LOCAL in meta:
        fallback = local_init.verify(fallback, local_proof)

    binding = {}
    if settings.store:
        ref = Reference(settings.store, meta["instance_tag"], KEY_ITEM)
        directory = store_directory(ref, settings)
        initialization = KeyInitialization(meta["instance_id"], str(ref), directory)
        proof = initialization.pending()
        stored = key if recovering else read_item(ref, settings, durable=True)
        if stored is not None and proof is None and not recovering:
            raise StoreError("configured fingerprint key has no provenance; operator reconciliation is required")
        if stored is not None and fallback is not None and not hmac.compare_digest(stored, fallback):
            raise StoreError("local and configured fingerprint keys differ")
        key = stored if stored is not None else fallback
        if key is None:
            key = secrets.token_bytes(32)
            proof = None
        elif proof is not None:
            key = initialization.verify(key, proof)
        if stored is None:
            if proof is None:
                initialization.record(key)
            write_item(ref, settings, key)
        binding.update({KEY_REF: str(ref), KEY_STORE_DIR: directory or "", KEY_PROOF: key_proof(key, ref, directory)})
        initialization.retire(key, binding.copy())
        if fallback is not None:
            db.after_commit(lambda: retire_local_key(local, settings, str(ref)))
    else:
        if read_file(*key_initialization_path(configured=True), private=True) is not None:
            raise FingerprintKeyMissing("fingerprint_key_missing")
        if fallback is None:
            with credential_activation(publishing=True):
                fallback = secrets.token_bytes(32)
                local_proof = local_init.record(fallback)
                write_file(local, local.parent, fallback)
        key = fallback

    if fallback is not None:
        if KEY_LOCAL not in meta:
            binding[KEY_LOCAL] = local_proof
        local_init.retire(fallback, {KEY_LOCAL: local_proof})
    c.executemany("INSERT INTO schema_meta (key, value) VALUES (?, ?)", binding.items())
    return key


def fingerprint_key(settings: config.SecretsConfig | None = None) -> bytes:
    settings = config.secret_settings() if settings is None else settings
    local = local_key_path()
    migrate_registry()
    pinned = db.transaction_mode() == "read"
    try:
        with ExitStack() as cleanup, db.reader() as c:
            meta = db.metadata(c)
            if reference := meta.get(KEY_REF):
                key = referenced_key(meta, settings)
                if not pinned and meta.get(KEY_LOCAL) and local.exists():
                    if db.in_write():
                        db.after_commit(lambda: retire_local_key(local, settings, reference))
                    else:
                        cleanup.callback(retire_local_key, local, settings, reference)
                return key
            meta, key, _ = key_context(c, settings, local, meta)
            if key is not None and (not settings.store or meta.get(KEY_REF) or (pinned and meta.get(KEY_LOCAL))):
                return key
    except db.NoDatabase:
        if not paths.ISOLATED:
            raise
    with db.tx() as c:
        meta, key, pending = key_context(c, settings, local)
        return ensure_key(c, settings, local, meta, key, recovering=bool(pending))


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
            # Schema 27 also permits arbitrary text in historical event details.
            with suppress(json.JSONDecodeError, KeyError):
                operation = json.loads(detail)
                event.update(kind=operation["kind"], provider=operation["provider"], detail=operation["id"])
    return events


def read(scope, name) -> bytes | None:
    return read_record(scope, name)[1]


def read_record(scope, name) -> tuple[dict | None, bytes | None]:
    item = external_item(scope, name)
    settings = config.secret_settings()
    with registry() as c:
        row = credentials.secret(c, scope, name)
        meta = db.metadata(c)
        key = None
        if row and row["fingerprint"] is not None:
            _, key = lookup_key(c, settings, local_key_path(), meta)
        require_store(settings)
        if key is None or row["status"] != "set":
            return row, None
        ref = registry_reference(c, row, item)
        if ref.store not in (settings.store, "owner_file"):
            raise StoreError("secret is registered in a different store")
        value = read_item(ref, settings)
        if value is not None and item_digest(key, value) == row["fingerprint"]:
            return row, value
        return row, None


def configured_registry():
    return paths.DB_FILE.is_file() and bool(config.secret_settings().store)


def lookup(scope, name) -> tuple[dict | None, str]:
    if not configured_registry():
        return None, ""
    row, value = read_record(scope, name)
    return row, value.decode() if value is not None else ""


def registered(scope, name):
    return get(scope, name) if configured_registry() else None


def rows():
    return items() if configured_registry() else []


def env(names, *, source=None, fingerprints=None, legacy=None) -> dict[str, str]:
    from . import secret_move

    source = os.environ if source is None else source
    wanted = set(names)
    found = {name: source[name] for name in wanted if source.get(name)}
    missing = wanted - found.keys()
    if missing:
        indexed = {row["name"]: row for row in rows() if row["scope"] == "global"}
        for name in (missing & indexed.keys()) - {"FINGERPRINT_KEY"}:
            if indexed[name]["status"] == "set":
                row, value = read_record("global", name)
                if value is not None:
                    found[name] = value.decode()
                    if fingerprints is not None:
                        fingerprints[name] = row["fingerprint"]
        unregistered = missing - indexed.keys()
        if unregistered:
            old = secret_move.pi_env() if legacy is None else legacy
            found.update({name: old[name] for name in unregistered if old.get(name)})
    return found


def env_status(names, *, source=None, fingerprints=None, legacy=None) -> dict[str, str]:
    from . import secret_move

    source = os.environ if source is None else source
    indexed = {row["name"]: row for row in rows() if row["scope"] == "global"}
    old = secret_move.pi_env() if legacy is None else legacy
    found = {}
    for name in set(names):
        if source.get(name):
            found[name] = source[name]
        elif name in indexed:
            row = indexed[name]
            if row["status"] == "set" and row["fingerprint"]:
                found[name] = row["fingerprint"]
                if fingerprints is not None:
                    fingerprints[name] = row["fingerprint"]
        elif old.get(name):
            found[name] = old[name]
    return found


def set_names(names) -> set[str]:
    return set(env_status(names))


def values():
    for _, value in entries():
        yield value


def entries():
    for row in rows():
        if row["status"] != "set":
            continue
        with suppress(InternalSecret, StoreError, StoreMissing, OSError, ValueError):
            if (value := read(row["scope"], row["name"])) is not None:
                yield row, value.decode(errors="replace")


def read_for_expose(row: dict) -> tuple[str | None, str]:
    from . import vault

    scope, name = row["scope"], row["name"]
    item = item_path(scope, name)
    if item in (KEY_ITEM, VERIFICATION_KEY_ITEM):
        return None, "internal"
    settings = config.secret_settings()
    try:
        require_store(settings)
        with registry() as c:
            meta = db.metadata(c)
            key = None
            if row.get("fingerprint") is not None:
                _, key = lookup_key(c, settings, local_key_path(), meta)
            ref = registry_reference(c, row, item)
        if ref.store not in (settings.store, "owner_file"):
            return None, "store_mismatch"
        raw = read_item(ref, settings)
    except (StoreError, StoreMissing, FingerprintKeyMissing, OSError, ValueError, sqlite3.Error,
            db.NoDatabase, db.IncompatibleSchema):
        return None, "missing"
    if raw is None:
        return None, "missing"
    text = raw.decode(errors="replace")
    if len(text) < vault.SHORTEST:
        return None, "below_floor"
    if key is None or item_digest(key, raw) != row["fingerprint"]:
        return None, "fingerprint_mismatch"
    return text, ""


def record_refused(row: dict, role: str, env_name: str, reason: str = "below_floor") -> None:
    actor = principals.current()
    detail = json.dumps({"role": role, "env": env_name, "reason": reason})

    def write():
        with suppress(OSError, sqlite3.Error, db.NoDatabase, db.IncompatibleSchema, RuntimeError):
            with db.tx() as c:
                credentials.secret_event(c, row, "refused", actor, util.iso(), ok=False, detail=detail)

    if db.transaction_mode() is not None:
        db.after_transaction(write)
    elif not db.isolated_busy():
        write()


def set_expose(scope: str, name: str, expose, actor: str) -> dict | None:
    from . import scrub

    external_item(scope, name, changing_value=True)
    validate_actor(actor)
    rules = validate_expose(expose)
    with registry(write=True) as c:
        row = credentials.set_expose(c, scope, name, rules)
        if row is not None:
            credentials.secret_changed(c, row, actor)
            db.after_commit(scrub.invalidate)
        return row


def move_files():
    from . import secret_move

    return secret_move.run()


def pending_root():
    return paths.STATE_DIR.resolve() / "secret-writes"


def pending_path(item):
    root = pending_root()
    return root / item, root


def pending_journals(tag, settings):
    journals = {}
    with ExitStack() as stack:
        try:
            parent = stack.enter_context(confined_directory(pending_root()))
        except FileNotFoundError:
            return journals
        require_private_directory(parent)
        for scope in os.listdir(parent):
            if not SCOPE.fullmatch(scope):
                continue
            with paths.directory_fd(Path(scope), parent) as directory:
                require_private_directory(directory)
                for name in os.listdir(directory):
                    if not valid_name(scope, name):
                        continue
                    try:
                        raw = read_file_at(directory, name)
                    except FileNotFoundError:
                        continue
                    if settings.store is None:
                        raise FingerprintKeyMissing("fingerprint_key_missing")
                    item = f"{scope}/{name}"
                    receipt = json.loads(raw)
                    require_pending_backend(receipt, Reference(settings.store, tag, item), settings)
                    journals[item] = raw, receipt
    return journals


def require_pending_backend(receipt, ref, settings):
    if not receipt:
        return
    store_dir = store_directory(ref, settings)
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


def save_pending(item, record, *, create_only=False):
    raw = json.dumps(record).encode()
    write_file(*pending_path(item), raw, private=True, create_only=create_only)
    return raw


def pending_binding(ref, settings, key):
    directory = store_directory(ref, settings)
    key_ref = Reference(ref.store, ref.tag, KEY_ITEM)
    return {"store_dir": directory, "key_proof": key_proof(key, key_ref, directory)}


def retire_pending(item, raw, expected):
    if raw is None:
        return
    scope, name = item.split("/")

    def matches(c):
        return credentials.secret(c, scope, name) == expected and read_file(*pending_path(item), private=True) == raw

    def committed():
        with suppress(OSError, sqlite3.Error, db.NoDatabase, db.IncompatibleSchema):
            sync_before_cleanup(matches, lambda _c: remove_file(*pending_path(item), private=True))

    db.after_commit(committed)


def history_detail(kind, provider, operation_id=None):
    return json.dumps({"id": operation_id, "kind": kind, "provider": provider})


def save_change(c, scope, name, previous, ref, change):
    from . import scrub

    actor, at = change["actor"], change["at"]
    if change.get("removed"):
        row = (credentials.mark_removed(c, previous, at) if previous else
               credentials.removed_secret(c, scope, name, change["kind"], ref.store, str(ref), at))
        event = "removed"
    else:
        rotated = change.get("rotated", bool(previous and previous["status"] == "set"))
        row = credentials.save_secret(c, scope, name, change["kind"], ref.store, str(ref), change["fingerprint"],
                                      actor, at, change["provider"], rotated=rotated,
                                      managed_by=change.get("managed_by"), expose=change.get("expose"))
        event = "moved" if change.get("moved") else "rotated" if rotated else "set"
    kind, provider = row["kind"], row["provider"]
    if event == "removed" and "provider" in change:
        kind, provider = change["kind"], change["provider"]
    credentials.secret_event(c, row, event, actor, at, detail=history_detail(kind, provider, change["id"]))
    credentials.secret_changed(c, row, actor)
    db.after_commit(scrub.invalidate)
    return row


def verification_key_row(c):
    ref = Reference("owner_file", db.metadata(c)["instance_tag"], VERIFICATION_KEY_PATH)
    row = credentials.secret(c, "global", VERIFICATION_KEY_NAME)
    if row and (row["kind"], row["store"], row["reference"]) != ("signing_key", ref.store, str(ref)):
        raise StoreError(f"{VERIFICATION_KEY_ITEM} conflicts with the verification authority's registry entry")
    return row, ref


def save_verification_key(c, row, ref, value: bytes, actor: str):
    sha = item_digest(fingerprint_key(), value)
    if row and row["fingerprint"] == sha:
        return row
    return save_change(c, "global", VERIFICATION_KEY_NAME, row, ref,
                       dict(id=secrets.token_hex(16), kind="signing_key", actor=actor,
                            at=util.iso(), fingerprint=sha, provider=None, rotated=row is not None))


def register_verification_key(value: bytes, *, rotated: bool = False):
    if not rotated:
        with registry() as c:
            if verification_key_row(c)[0] is not None:
                return
    with registry(write=True) as c:
        row, ref = verification_key_row(c)
        if row is None or rotated:
            actor = principals.current()
            validate_actor(actor)
            save_verification_key(c, row, ref, value, actor)


def check_verification_key(actor: str):
    from . import scratch

    validate_actor(actor)
    with registry(write=True) as c:
        previous, ref = verification_key_row(c)
        value = None
        with suppress(FileNotFoundError), scratch.authority_fd() as fd:
            value = scratch.read_secret(fd)
        row = previous if value is None else save_verification_key(c, previous, ref, value, actor)
        if row is not None:
            at = util.iso()
            ok = value is not None
            row = credentials.mark_checked(c, row, ok, at, recovered=row is not previous)
            credentials.secret_event(c, row, "checked", actor, at, ok=ok, detail=history_detail(row["kind"], None))
        return row


def recover_changes(c, scope, name, row, ref, receipt, fingerprint, *, absent):
    confirmed = list(receipt.get("confirmed", ()))

    def recorded(operation):
        return row is not None and credentials.change_recorded(c, row["id"], operation["id"])

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


def validate_set(scope, name, value, kind):
    external_item(scope, name, changing_value=True)
    validate_value(value, kind)
    if scope.startswith("repo:") and kind in {"api_key", "repo_answer"}:
        from . import vault

        if why := vault.unfit(value):
            raise ValueError(why)


def adopt(scope, name, value, kind, *, provider=None, managed_by=None, owner_file=None, settings=None, expose=None):
    validate_set(scope, name, value, kind)
    validate_provider(provider)
    rules = validate_expose(expose) if expose is not None else None
    item = item_path(scope, name)
    active = config.secret_settings()
    if settings is not None and settings != active:
        raise StoreError("the configured store changed during the move")
    require_store(active)
    settings = active
    local = local_key_path()
    with registry(write=True) as c:
        meta, key, journals = key_context(c, settings, local)
        ref = Reference("owner_file" if owner_file else settings.store, meta["instance_tag"], owner_file or item)
        row = credentials.secret(c, scope, name)
        raw, receipt = journals.get(item, (None, {}))
        pending = receipt.get("sets", [])
        if raw is not None and not (len(pending) == 1 and pending[0].get("moved")
                                    and pending[0]["reference"] == str(ref) and not receipt.get("removal")):
            raise StoreError("secret operation is pending")
        if row is not None and (row["status"] != "set" or row["reference"] != str(ref)
                                or row["kind"] != kind or row["provider"] != provider
                                or row["managed_by"] != managed_by
                                or credentials.last_lifecycle(c, row["id"]) != "moved"):
            raise StoreError("registry already contains this address")
        if owner_file and row is None and read_item(Reference(settings.store, ref.tag, item), settings) is not None:
            raise StoreError("store already contains this address")
        stored = read_item(ref, settings, durable=True)
        data = value.encode()
        if stored is not None and (stored != data or not (row or pending or owner_file)):
            raise StoreError("store already contains this address")
        key = ensure_key(c, settings, local, meta, key, recovering=bool(journals))
        fingerprint = item_digest(key, data)
        if row is not None:
            if stored is None or row["fingerprint"] != fingerprint:
                raise StoreError("existing moved item does not match its fingerprint")
            if pending and not credentials.change_recorded(c, row["id"], pending[0]["id"]):
                raise StoreError("secret operation is pending")
            retire_pending(item, raw, row)
            return row
        change = {"id": secrets.token_hex(16), "reference": str(ref), "kind": kind, "actor": "hand", "at": util.iso(),
                  "fingerprint": fingerprint, "provider": provider, "moved": True, "rotated": False, "managed_by": managed_by,
                  **({"expose": rules} if rules is not None else {})}
        if pending:
            change = pending[0]
            if (change["fingerprint"], change["kind"], change["provider"], change.get("managed_by")) != (
                    fingerprint, kind, provider, managed_by):
                raise StoreError("legacy input differs from its pending move")
        elif not owner_file:
            raw = save_pending(item, dict(pending_binding(ref, settings, key), sets=[change]), create_only=True)
        if stored is None and not owner_file:
            write_item(ref, settings, data, create_only=True)
        got = read_item(ref, settings, durable=True)
        if got is None or item_digest(key, got) != fingerprint:
            raise StoreError("store readback failed the fingerprint check")
        row = save_change(c, scope, name, None, ref, change)
        retire_pending(item, raw, row)
        return row


def put(scope: str, name: str, value: str, kind: str, actor: str, *, provider: str | None = None,
        managed_by: str | None = None, expose=None) -> dict:
    item = external_item(scope, name, changing_value=True)
    validate_actor(actor)
    validate_set(scope, name, value, kind)
    validate_provider(provider)
    rules = validate_expose(expose) if expose is not None else None
    settings = config.secret_settings()
    local = local_key_path()
    with registry(write=True) as c:
        meta, key, journals = key_context(c, settings, local)
        require_store(settings)
        ref = Reference(settings.store, meta["instance_tag"], item)
        row = credentials.secret(c, scope, name)
        if row:
            require_removable(item, row["store"])
            if row["reference"] != str(ref):
                raise StoreError("secret is registered in a different store")
        _, receipt = journals.get(item, (None, {}))
        key = ensure_key(c, settings, local, meta, key, recovering=bool(journals))
        stored = read_item(ref, settings, durable=bool(receipt))
        fingerprint = item_digest(key, stored) if receipt.get("sets") and stored is not None else None
        row, confirmed, removal, _ = recover_changes(
            c, scope, name, row, ref, receipt, fingerprint, absent=stored is None)
        if removal:
            raise StoreError("secret removal is pending; retry the removal before setting a new value")
        change = {"id": secrets.token_hex(16), "reference": str(ref), "kind": kind, "actor": actor, "at": util.iso(),
                  "fingerprint": digest(key, value), "provider": provider, "rotated": stored is not None,
                  "managed_by": managed_by, **({"expose": rules} if rules is not None else {})}
        # Keep earlier operations recoverable until the outer SQLite transaction commits.
        raw = save_pending(item, dict(pending_binding(ref, settings, key), confirmed=confirmed, sets=[change]))
        data = value.encode()
        if stored != data:
            write_item(ref, settings, data)
        got = read_item(ref, settings, durable=True)
        if got is None or item_digest(key, got) != change["fingerprint"]:
            raise StoreError("store readback failed the fingerprint check")
        row = save_change(c, scope, name, row, ref, change)
        retire_pending(item, raw, row)
        return row


def remove(scope, name, actor):
    return reconcile(scope, name, actor, deleting=True)


def check(scope, name, actor="hand"):
    if item_path(scope, name) == VERIFICATION_KEY_ITEM:
        return check_verification_key(actor)
    return reconcile(scope, name, actor)


def reconcile(scope, name, actor, *, deleting=False):
    item = external_item(scope, name, changing_value=deleting)
    validate_actor(actor)
    settings = config.secret_settings()
    local = local_key_path()
    with registry(write=True) as c:
        row = credentials.secret(c, scope, name)
        if deleting and row and settings.store is not None:
            require_removable(item, row["store"])
        meta, key, journals = key_context(c, settings, local)
        require_store(settings)
        raw, receipt = journals.get(item, (None, {}))
        pending = receipt.get("sets", [])
        target = row or receipt.get("removal") or (pending[0] if pending else None)
        legacy = None
        if deleting and scope.startswith("repo:"):
            from . import secret_move

            legacy = secret_move.repo_removal(scope.removeprefix("repo:"), name)
            if legacy and target is None:
                ref = Reference(settings.store, meta["instance_tag"], item)
                target = {"reference": str(ref), "kind": "repo_answer"}
        if target is None:
            return None
        ref = registry_reference(c, target, item)
        if deleting:
            require_removable(item, ref.store)
        if ref.store not in (settings.store, "owner_file"):
            raise StoreError("secret is registered in a different store")
        require_pending_backend(receipt, ref, settings)
        key = ensure_key(c, settings, local, meta, key, recovering=bool(journals))
        value = read_item(ref, settings, durable=bool(receipt))
        sha = item_digest(key, value) if value is not None and (pending or not deleting) else None
        row, confirmed, removal, recovered = recover_changes(
            c, scope, name, row, ref, receipt, sha, absent=value is None)
        if deleting:
            if removal is None and not (row and row["status"] == "missing" and value is None and
                                        credentials.last_lifecycle(c, row["id"]) == "removed"):
                metadata = row or target
                removal = {"id": secrets.token_hex(16), "reference": str(ref), "kind": metadata["kind"],
                               "provider": metadata.get("provider"), "actor": actor, "at": util.iso(), "removed": True}
                raw = save_pending(item, dict(pending_binding(ref, settings, key), confirmed=confirmed,
                                              removal=removal))
            if legacy is not None:
                legacy()
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
