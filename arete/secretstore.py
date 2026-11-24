from __future__ import annotations

import base64
import binascii
import ctypes
import errno
import os
import re
import secrets
import stat
import subprocess
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from . import config, paths, util

NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
SCOPE = re.compile(r"global|(?:repo|line|device|connector):[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
KIND = re.compile(r"[a-z][a-z0-9_]{0,63}")
TAG = re.compile(r"[0-9a-f]{8}")
SINGLE_LINE = {"api_key", "minted_token", "webhook_secret"}
KEY_ITEM = "global/FINGERPRINT_KEY"
ACL_TYPE_EXTENDED = 0x100


class StoreMissing(RuntimeError):
    pass


class StoreError(OSError):
    pass


class InternalSecret(StoreError):
    pass


def validate(scope: str, name: str) -> None:
    if not isinstance(scope, str) or not SCOPE.fullmatch(scope):
        raise ValueError("invalid secret scope")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("invalid secret name")


def address(scope: str, name: str) -> str:
    validate(scope, name)
    return f"secret:{scope}/{name}"


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


def require_private_file(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise StoreError("secret item must be a private regular file with mode 0600")
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
def file_parent(path: Path, root: Path, *, create=False, private=False):
    if paths.ISOLATED and (paths.confined(root) is None or
                           paths.scratch_root(root) != paths.scratch_root(paths.STATE_DIR)):
        raise StoreError("secret item must stay inside the instance's scratch folder")
    with paths.directory_fd(root, create=create) as fd, ExitStack() as stack:
        parent = path.parent.relative_to(root)
        if private:
            require_private_directory(fd)
            for part in parent.parts:
                child = stack.enter_context(paths.directory_fd(Path(part), fd, create=create))
                require_exact_entry(fd, part, writing=create)
                require_private_directory(child)
                fd = child
        else:
            fd = stack.enter_context(paths.directory_fd(parent, fd, create=create))
        yield fd


def read_file(path: Path, root: Path, *, private=False) -> bytes | None:
    try:
        with file_parent(path, root, private=private) as parent:
            if private:
                require_exact_entry(parent, path.name)
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                require_private_file(fd)
                with os.fdopen(fd, "rb", closefd=False) as file:
                    return file.read()
            finally:
                os.close(fd)
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
