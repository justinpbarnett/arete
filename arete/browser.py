from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import stat
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from . import paths, scratch, verify

REGISTRY = "https://registry.npmjs.org/"
PIN = "browser-server.pin"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_FILE = 128 * 1024 * 1024
EXACT = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?")
PACKAGE = re.compile(r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")
FAILURES = (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError, EOFError)


def refuse(reason: str) -> scratch.ConfinementUnavailable:
    return scratch.ConfinementUnavailable("pinned browser server: " + reason)


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as reply:
        data = reply.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise refuse("download too large")
    return data


def pinned() -> tuple[str, str]:
    name, _, version = verify.PLAYWRIGHT_MCP.rpartition("@")
    return name, version


def server_folder() -> Path:
    return paths.STATE_DIR / "browser-server" / pinned()[1]


def chrome() -> Path:
    if not CHROME.is_file():
        raise scratch.ConfinementUnavailable(f"Google Chrome is not installed at {CHROME}")
    return CHROME


def engine_node() -> Path:
    found = shutil.which("node")
    if not found:
        raise refuse("no node in the engine's environment")
    return scratch.canonical(Path(found))


def tarball_name(name: str, version: str) -> str:
    return f"{name.replace('/', '__')}-{version}.tgz"


def integrity_of(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def metadata(name: str, version: str) -> dict:
    return json.loads(fetch(REGISTRY + quote(name, safe="@") + "/" + version))


def download(root: str, version: str) -> dict[str, tuple[str, str, bytes]]:
    found: dict[str, tuple[str, str, bytes]] = {}
    queue = [(root, version)]
    while queue:
        name, wanted = queue.pop()
        if not PACKAGE.fullmatch(name) or not EXACT.fullmatch(wanted):
            raise refuse(f"{name}@{wanted} is not an exact registry version")
        if name in found:
            if found[name][0] != wanted:
                raise refuse(f"{name} is needed at two versions")
            continue
        info = metadata(name, wanted)
        dist = info["dist"]
        if info["name"] != name or info["version"] != wanted:
            raise refuse(f"registry answered with another package for {name}@{wanted}")
        if not str(dist["tarball"]).startswith(REGISTRY) or not str(dist["integrity"]).startswith("sha512-"):
            raise refuse(f"{name}@{wanted} has no registry tarball with a sha512 integrity")
        data = fetch(dist["tarball"])
        if not hmac.compare_digest(integrity_of(data), dist["integrity"]):
            raise refuse(f"{name}@{wanted} does not match its published integrity")
        found[name] = (wanted, dist["integrity"], data)
        queue += list((info.get("dependencies") or {}).items())
    return found


def entries(data: bytes) -> tuple[set[str], dict[str, tuple[bytes, bool]]]:
    folders: set[str] = set()
    files: dict[str, tuple[bytes, bool]] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != "package" or member.name.startswith("/") or ".." in parts:
                raise refuse(f"unsafe archive member {member.name!r}")
            relative = "/".join(parts[1:])
            if not relative:
                continue
            if member.isdir():
                folders.add(relative)
            elif member.isfile() and member.size <= MAX_FILE:
                files[relative] = (archive.extractfile(member).read(), bool(member.mode & 0o111))
            else:
                raise refuse(f"archive member {member.name!r} is not a plain file or folder")
    for relative in files:
        folders.update(str(p) for p in PurePosixPath(relative).parents if str(p) != ".")
    return folders, files


def extract(data: bytes, target: Path) -> None:
    folders, files = entries(data)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative in sorted(folders):
        (target / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative, (content, executable) in files.items():
        out = os.open(target / relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o700 if executable else 0o600)
        with os.fdopen(out, "wb") as handle:
            handle.write(content)


def pin_signature(key: bytes, packages: dict) -> str:
    return hmac.digest(key, scratch.encoded(packages), "sha256").hex()


def write_pin(packages: dict) -> None:
    with scratch.registry(True) as (fd, key):
        temporary = secrets.token_hex(16) + ".new"
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, "wb") as target:
            target.write(scratch.encoded({"data": packages, "signature": pin_signature(key, packages)}))
            target.flush()
            os.fsync(out)
        os.rename(temporary, PIN, src_dir_fd=fd, dst_dir_fd=fd)


def read_pin() -> dict:
    with scratch.registry() as (fd, key):
        source = os.open(PIN, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(source, "rb") as handle:
            scratch.checked(os.fstat(source), scratch.identity(os.fstat(source)))
            envelope = json.loads(handle.read(MAX_DOWNLOAD))
    if not hmac.compare_digest(envelope["signature"], pin_signature(key, envelope["data"])):
        raise refuse("the pin is not signed by this engine")
    return envelope["data"]


def resolved() -> Path:
    name, version = pinned()
    folder = server_folder()
    try:
        with paths.directory_fd(folder.parent, create=True) as parent:
            fcntl.flock(parent, fcntl.LOCK_EX)
            try:
                os.lstat(folder.name, dir_fd=parent)
                return folder
            except FileNotFoundError:
                pass
            packages = download(name, version)
            pin = {n: {"version": v, "integrity": i} for n, (v, i, _) in packages.items()}
            build = folder.parent / f".build-{secrets.token_hex(8)}"
            try:
                build.mkdir(mode=0o700)
                (build / "tarballs").mkdir(mode=0o700)
                (build / "node_modules").mkdir(mode=0o700)
                for n, (v, _i, data) in packages.items():
                    (build / "tarballs" / tarball_name(n, v)).write_bytes(data)
                    extract(data, build / "node_modules" / n)
                write_pin(pin)
                os.rename(build, folder)
            finally:
                shutil.rmtree(build, ignore_errors=True)
    except scratch.ConfinementUnavailable:
        raise
    except FAILURES as error:
        raise refuse(f"cannot resolve {verify.PLAYWRIGHT_MCP}: {error}") from error
    return folder


def listing(root: Path) -> tuple[set[str], dict[str, tuple[bytes, bool]]]:
    folders: set[str] = set()
    files: dict[str, tuple[bytes, bool]] = {}

    def walk(folder: Path, prefix: str) -> None:
        with os.scandir(folder) as scan:
            found = list(scan)
        for entry in found:
            relative = prefix + entry.name
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                folders.add(relative)
                walk(Path(entry.path), relative + "/")
            elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size <= MAX_FILE:
                descriptor = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as handle:
                    files[relative] = (handle.read(), bool(st.st_mode & 0o111))
            else:
                raise refuse(f"{relative} is not a plain file or folder")

    walk(root, "")
    return folders, files


def read_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise refuse(f"{path.name} is not a regular file")
        return handle.read(MAX_DOWNLOAD + 1)


def checked() -> tuple[str, str]:
    name, version = pinned()
    folder = server_folder()
    try:
        pin = read_pin()
        if pin.get(name, {}).get("version") != version:
            raise refuse("the pin names another version")
        for private in (folder.parent, folder, folder / "node_modules", folder / "tarballs"):
            st = os.lstat(private)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise refuse(f"{private.name} is not a private folder of the engine")
        want_folders: set[str] = set()
        want_files: dict[str, tuple[bytes, bool]] = {}
        for n, info in pin.items():
            data = read_file(folder / "tarballs" / tarball_name(n, info["version"]))
            if not hmac.compare_digest(integrity_of(data), info["integrity"]):
                raise refuse(f"the tarball of {n} changed")
            tree_folders, tree_files = entries(data)
            top = PurePosixPath(n)
            want_folders.update(str(p) for p in top.parents if str(p) != ".")
            want_folders.add(n)
            want_folders.update(f"{n}/{f}" for f in tree_folders)
            want_files.update({f"{n}/{f}": v for f, v in tree_files.items()})
        have_folders, have_files = listing(folder / "node_modules")
        if have_folders != want_folders or have_files != want_files:
            raise refuse("the installed tree differs from its tarballs")
        if {e.name for e in os.scandir(folder)} != {"node_modules", "tarballs"}:
            raise refuse("the server folder holds extra entries")
        if {e.name for e in os.scandir(folder / "tarballs")} != {tarball_name(n, i["version"]) for n, i in pin.items()}:
            raise refuse("the tarball folder holds extra entries")
        manifest = json.loads(want_files[f"{name}/package.json"][0])
        bin_entry = manifest["bin"]
        cli = bin_entry if isinstance(bin_entry, str) else next(iter(bin_entry.values())) if len(bin_entry) == 1 else ""
        relative = PurePosixPath(f"{name}/{cli}")
        if not cli or ".." in relative.parts or str(relative) not in want_files:
            raise refuse("the package has no single command")
        found = engine_node()
        if not os.access(found, os.X_OK) or scratch.exposure(found, (paths.WORKTREES_DIR,)) is not None:
            raise refuse("no trusted node in the engine's environment")
        return str(found), str(folder / "node_modules" / relative)
    except scratch.ConfinementUnavailable:
        raise
    except FAILURES as error:
        raise refuse(f"verification failed: {type(error).__name__}: {error}") from error
