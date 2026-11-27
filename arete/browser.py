from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from . import paths, sandbox, scratch, util

REGISTRY = "https://registry.npmjs.org/"
PIN = "browser-server.pin"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_FILE = 128 * 1024 * 1024
EXACT = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?")
PACKAGE = re.compile(r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")
FAILURES = (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError, EOFError)


def refuse(reason: str) -> sandbox.ConfinementUnavailable:
    return sandbox.ConfinementUnavailable("pinned browser server: " + reason)


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as reply:
        data = reply.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise refuse("download too large")
    return data


def pinned() -> tuple[str, str]:
    name, _, version = PLAYWRIGHT_MCP.rpartition("@")
    return name, version


def server_folder() -> Path:
    return paths.STATE_DIR / "browser-server" / pinned()[1]


def chrome() -> Path:
    if not CHROME.is_file():
        raise sandbox.ConfinementUnavailable(f"Google Chrome is not installed at {CHROME}")
    return CHROME


def engine_node() -> Path:
    found = shutil.which("node")
    if not found:
        raise refuse("no node in the engine's environment")
    return sandbox.canonical(Path(found))


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


def store_pin(fd: int, key: bytes, packages: dict) -> None:
    scratch.write_atomic(fd, PIN, scratch.encoded({"data": packages, "signature": pin_signature(key, packages)}))


def load_pin(fd: int, key: bytes) -> dict:
    source = os.open(PIN, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(source, "rb") as handle:
        scratch.checked(os.fstat(source), scratch.identity(os.fstat(source)))
        envelope = json.loads(handle.read(MAX_DOWNLOAD))
    if not hmac.compare_digest(envelope["signature"], pin_signature(key, envelope["data"])):
        raise refuse("the pin is not signed by this engine")
    return envelope["data"]


def write_pin(packages: dict) -> None:
    with scratch.registry(True) as (fd, key):
        store_pin(fd, key, packages)


def read_pin() -> dict:
    with scratch.registry() as (fd, key):
        return load_pin(fd, key)


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
    except sandbox.ConfinementUnavailable:
        raise
    except FAILURES as error:
        raise refuse(f"cannot resolve {PLAYWRIGHT_MCP}: {error}") from error
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
    except sandbox.ConfinementUnavailable:
        raise
    except FAILURES as error:
        raise refuse(f"verification failed: {type(error).__name__}: {error}") from error


PLAYWRIGHT_MCP = "@playwright/mcp@0.0.75"
BROWSER_EXEC = (f"import sys; sys.path.insert(0, {str(paths.PACKAGE_DIR.parent)!r}); "
                "from arete import codebase; codebase.load(); "
                "from arete import browser; browser.browser_start(sys.argv[1:])")
BROWSER_MACH = ("com.apple.system.opendirectoryd.libinfo", "com.apple.system.opendirectoryd.membership",
                "com.apple.system.notification_center", "com.apple.system.logger", "com.apple.logd",
                "com.apple.cfprefsd.daemon", "com.apple.cfprefsd.agent", "com.apple.bsd.dirhelper",
                "com.apple.fonts", "com.apple.FontObjectsServer", "com.apple.coreservices.launchservicesd",
                "com.apple.lsd.mapdb", "com.apple.windowserver.active", "com.apple.CARenderServer",
                "com.apple.MTLCompilerService")
BROWSER_MACH_PATTERN = r"^(com\.google\.Chrome|org\.chromium\.Chromium)[A-Za-z.]*\.MachPortRendezvousServer\.[0-9]+$"
BROWSER_HIDDEN = ("Library/Application Support/Google/Chrome", "Library/Keychains", ".claude.json", ".config/gh",
                  ".zshrc", ".zshenv", ".zprofile", ".zlogin", ".bashrc", ".bash_profile", ".bash_login", ".profile",
                  ".config/fish")
BROWSER_GRACE_SECONDS = 3
BROWSER_PREFLIGHT_SECONDS = 30
LISTEN_TRIES = 8
LISTEN_PROBE_SECONDS = 5
BROWSER_SERVE_SECONDS = 60
CHROME_ERROR = re.compile(r"error|fatal|fail|denied|not permitted|abort", re.IGNORECASE)
BROWSER_SOCKETS = "s"
BROWSER_ENV = {"PATH": "/usr/bin:/bin"}


def free_port(avoid: frozenset[int] = frozenset()) -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in avoid:
            return port


def mcp_config(output_dir: Path) -> str:
    args = ["-y", PLAYWRIGHT_MCP, "--headless", "--isolated", "--browser", "chrome", "--output-dir", str(output_dir)]
    return json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": args}}})


def browser_folder(a: scratch.Attempt) -> Path:
    folder = scratch.browser_path(a.run_dir)
    with paths.directory_fd(folder, create=True) as fd:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise sandbox.ConfinementUnavailable("the browser folder is not private")
        for leaf in ("home", "tmp"):
            with paths.directory_fd(Path(leaf), fd, create=True):
                pass
    return folder


def browser_profile(a: scratch.Attempt, port: int, folder: Path, listen: int | None = None) -> list[str]:
    state_default = scratch.home_dir() / ".local/share/arete"
    hidden = (*scratch.hidden_folders(paths.STATE_DIR.resolve() != state_default.resolve()),
              *(scratch.home_dir() / n for n in BROWSER_HIDDEN))
    evidence = scratch.evidence_path(a.run_dir)
    return sandbox.write_confinement(
        folder, [evidence], hidden=hidden, agent_roots=False, peers=paths.WORKTREES_DIR, secrets_denied=True,
        temp_writes=False, state=(paths.STATE_DIR, (evidence, folder, server_folder(), browser_log(a))),
        mach=(BROWSER_MACH, BROWSER_MACH_PATTERN), tcp_port=port, listen_port=listen)


def listen_port(a: scratch.Attempt, app: int, avoid: frozenset[int] = frozenset()) -> int:
    avoid, port = avoid | {app}, 0
    for _ in range(LISTEN_TRIES if sandbox.can_confine(False) else 1):
        port = free_port(avoid)
        with suppress(OSError, sandbox.ConfinementUnavailable):
            folder = browser_folder(a)
            if listens([*browser_profile(a, app, folder, port), "/usr/bin/nc", "-l", "127.0.0.1", str(port)], port,
                       folder):
                return port
    return port


def listens(argv: list[str], port: int, folder: Path) -> bool:
    proc = subprocess.Popen(argv, cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + LISTEN_PROBE_SECONDS
        while proc.poll() is None and time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), .5).close()
                return True
            except OSError:
                time.sleep(.1)
        return False
    finally:
        util.kill_group(proc)
        proc.wait()


def browser_exposure(a: scratch.Attempt, node: Path | None = None) -> None:
    grants = scratch.actor_grants(a)
    for name, tree, actors in (("browser folder", scratch.browser_path(a.run_dir), ("agent", "service")),
                               ("pinned browser server", server_folder(), ("agent", "service", "browser")),
                               ("node", node or engine_node(), ("agent", "service", "browser"))):
        for actor in actors:
            if (root := scratch.exposure(tree, grants[actor])) is not None:
                raise sandbox.ConfinementUnavailable(f"the {name} {tree} can be changed through the {actor} write grant {root}")


def browser_record(a: scratch.Attempt) -> dict:
    with scratch.registry() as (fd, key):
        data = scratch.load(fd, key, a.selector)
    if data["attempt"] != asdict(a):
        raise PermissionError("verification attempt authority revoked")
    return data.get("browser") or {}


def update_browser(a: scratch.Attempt, **fields) -> None:
    with scratch.registry() as (fd, key):
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = scratch.load(fd, key, a.selector)
        if data["attempt"] != asdict(a) or data["state"] not in scratch.LIVE:
            raise PermissionError("verification attempt authority revoked")
        data["browser"] = {**(data.get("browser") or {}), **fields}
        scratch.store(fd, key, data)


def loopback(url: str) -> bool:
    host = urlsplit(url).hostname
    try:
        return host == "localhost" or ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def app_port(a: scratch.Attempt, url: str) -> int:
    parts = urlsplit(url)
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        port = 0
    update_browser(a, port=port, loopback=loopback(url))
    return port


def browser_digest(argv: list[str]) -> str:
    return hashlib.sha256(scratch.encoded(list(argv))).hexdigest()


def browser_env(folder: Path, debug: bool = False) -> list[str]:
    return ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", f"HOME={folder}/home", f"PLAYWRIGHT_SOCKETS_DIR={BROWSER_SOCKETS}",
            f"CFFIXED_USER_HOME={folder}/home", f"TMPDIR={folder}/tmp", f"MAC_CHROMIUM_TMPDIR={folder}/tmp",
            *(["DEBUG=pw:browser*"] if debug else [])]


def browser_command(a: scratch.Attempt, words: list[str], listen: int | None = None) -> list[str]:
    workspace, worktrees = Path(a.workspace), paths.WORKTREES_DIR.resolve()
    if (not sandbox.can_confine(False) or sandbox.sandboxed() or workspace == worktrees
            or not workspace.is_relative_to(worktrees)):
        raise sandbox.ConfinementUnavailable("browser confinement cannot be applied in this process or workspace")
    port = browser_record(a).get("port")
    if not isinstance(port, int) or not 0 < port < 65536:
        raise sandbox.ConfinementUnavailable("no app port is recorded for browser confinement")
    node, cli = checked()
    browser_exposure(a, Path(node))
    folder = browser_folder(a)
    return [str(Path(sys.executable).resolve()), "-I", "-c", BROWSER_EXEC, a.selector,
            *browser_profile(a, port, folder, listen), *browser_env(folder, listen is not None), node, cli,
            *words, "--no-sandbox"]


def browser_table(a: scratch.Attempt, argv: list[str]) -> dict:
    return {"command": argv[0], "args": argv[1:], "cwd": str(scratch.browser_path(a.run_dir))}


def browser_server(a: scratch.Attempt) -> dict:
    words = browser_record(a).get("words")
    if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
        raise ValueError("no browser server was built for this attempt")
    return browser_table(a, browser_command(a, words))


def plain_config(a: scratch.Attempt, config_json: str) -> list[str] | None:
    try:
        servers = json.loads(config_json or "{}").get("mcpServers")
        words = servers["playwright"]["args"][2:] if servers else None
        output = Path(words[words.index("--output-dir") + 1]) if words else None
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        return None
    if (not servers or output is None or output.resolve() != scratch.evidence_path(a.run_dir).resolve()
            or config_json != mcp_config(output)):
        return None
    return words


def has_servers(config_json: str) -> bool:
    try:
        return bool(json.loads(config_json or "{}").get("mcpServers"))
    except (ValueError, AttributeError):
        return True


def browser_config(a: scratch.Attempt, config_json: str, add_dirs) -> str:
    words = plain_config(a, config_json)
    if words is None:
        return config_json
    try:
        loopback = browser_record(a).get("loopback")
    except (OSError, ValueError, KeyError, TypeError):
        return config_json
    if loopback is not True or not scratch.isolation(a, "codex", False, add_dirs, ""):
        return config_json
    browser_exposure(a)
    resolved()
    argv = browser_command(a, words)
    update_browser(a, words=words, argv=browser_digest(argv[5:]))
    return json.dumps({"mcpServers": {"playwright": browser_table(a, argv)}})


def browser_start(argv: list[str]) -> None:
    selector, rest = argv[0], argv[1:]
    if sandbox.sandboxed() or not rest or rest[0] != "/usr/bin/sandbox-exec":
        raise SystemExit("browser server refused: it must start outside any sandbox with the engine's profile")
    try:
        os.setsid()
    except PermissionError:
        os.setpgid(0, 0)
    with scratch.registry() as (fd, key):
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = scratch.load(fd, key, selector)
        recorded = (data.get("browser") or {}).get("argv")
        if data["state"] != "active" or not isinstance(recorded, str) or not hmac.compare_digest(
                recorded, browser_digest(rest)):
            raise SystemExit("browser server refused: the command is not the one the engine built")
        data["browser"]["groups"] = [*data["browser"].get("groups", []), os.getpid()]
        data["producers"][f"browser-{os.getpid()}"] = scratch.producer_entry(os.getpid())
        scratch.store(fd, key, data)
    os.chdir(scratch.browser_path(data["attempt"]["run_dir"]))
    os.execv(rest[0], rest)


def stop_browser(data: dict) -> None:
    groups = {g for g in (data.get("browser") or {}).get("groups", []) if isinstance(g, int) and g > 1}
    if not groups:
        return
    folder = ctypes.c_char_p(os.fsencode(scratch.browser_path(data["attempt"]["run_dir"])))
    box = ctypes.CDLL(None)
    box.sandbox_check.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)

    def writers() -> list[int]:
        try:
            rows = subprocess.run(["/bin/ps", "-U", str(os.getuid()), "-o", "pid=,pgid="], check=False,
                                  capture_output=True, text=True, timeout=30).stdout.split("\n")
        except (OSError, subprocess.SubprocessError):
            return []
        found = []
        for row in rows:
            pid, _, group = row.strip().partition(" ")
            if (pid.isdigit() and group.strip().isdigit() and int(group) in groups and int(pid) != os.getpid()
                    and box.sandbox_check(int(pid), None, 0) == 1
                    and box.sandbox_check(int(pid), b"file-write-create", 1, folder) == 0):
                found.append(int(pid))
        return found

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not (pids := writers()):
            return
        for pid in pids:
            with suppress(OSError):
                os.kill(pid, sig)
        deadline = time.monotonic() + BROWSER_GRACE_SECONDS
        while time.monotonic() < deadline and writers():
            time.sleep(0.1)


@dataclass
class Served:
    proc: subprocess.Popen | None
    entry: dict
    reason: str


SERVED: dict[str, Served] = {}


def browser_log(a: scratch.Attempt) -> Path:
    return Path(a.run_dir) / f"{a.step}-browser.log"


def log_since(log: Path, offset: int) -> str:
    try:
        with open(log, "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def add_browser_group(a: scratch.Attempt, pid: int) -> None:
    with scratch.registry() as (fd, key):
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = scratch.load(fd, key, a.selector)
        if data["attempt"] != asdict(a):
            raise PermissionError("verification attempt authority revoked")
        data["browser"] = {**(data.get("browser") or {}),
                           "groups": [*(data.get("browser") or {}).get("groups", []), pid]}
        scratch.store(fd, key, data)


def chrome_error(text: str, code: int | None) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    found = next((line for line in lines if CHROME_ERROR.search(line)), lines[0] if lines else
                 f"headless Chrome exited {code} and printed nothing")
    return found[:300]


def browser_preflight(a: scratch.Attempt, port: int, listen: int, folder: Path, log: Path) -> str:
    scratch_profile = folder / "tmp" / f"preflight-{secrets.token_hex(8)}"
    argv = [*browser_profile(a, port, folder, listen), *browser_env(folder), str(chrome()), "--headless",
            "--no-sandbox", f"--user-data-dir={scratch_profile}", "--dump-dom", "about:blank"]
    offset = log.stat().st_size if log.exists() else 0
    with open(log, "ab") as out, scratch.producing(a):
        proc = scratch.spawn(argv, actor_confined=True, cwd=folder, stdin=subprocess.DEVNULL, stdout=out,
                     stderr=subprocess.STDOUT, env=BROWSER_ENV)
    try:
        add_browser_group(a, proc.pid)
        try:
            proc.wait(BROWSER_PREFLIGHT_SECONDS)
        except subprocess.TimeoutExpired:
            return f"headless Chrome did not dump about:blank within {BROWSER_PREFLIGHT_SECONDS} seconds"
        output = log_since(log, offset)
        return "" if proc.returncode == 0 and "<html" in output else chrome_error(output, proc.returncode)
    finally:
        util.kill_group(proc)
        proc.wait()
        shutil.rmtree(scratch_profile, ignore_errors=True)


def browser_answers(proc: subprocess.Popen, listen: int, log: Path, offset: int) -> str:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "arete", "version": "1"}}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + BROWSER_SERVE_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return f"the browser server exited {proc.returncode}: {chrome_error(log_since(log, offset), proc.returncode)}"
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{listen}/mcp", body, headers)
            with opener.open(request, timeout=5) as reply:
                if reply.status == 200 and proc.poll() is None:
                    return ""
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    return f"the browser server did not answer on 127.0.0.1:{listen} within {BROWSER_SERVE_SECONDS} seconds"


def failed_server(a: scratch.Attempt, proc: subprocess.Popen | None, reason: str) -> Served:
    if proc is not None:
        util.stop_group(proc)
    with suppress(OSError), open(browser_log(a), "ab") as out:
        out.write(f"\n{reason}\n".encode())
    return Served(None, {}, reason)


def start_served(a: scratch.Attempt, words: list[str]) -> Served:
    proc = None
    try:
        record = browser_record(a)
        listen = record.get("listen")
        if record.get("loopback") is not True:
            raise sandbox.ConfinementUnavailable("the verify URL is not a loopback address, so the confined browser cannot reach it")
        if not isinstance(listen, int) or not 0 < listen < 65536:
            raise sandbox.ConfinementUnavailable("no browser port is recorded for the confined browser")
        browser_exposure(a)
        resolved()
        served = [*words, "--host", "127.0.0.1", "--port", str(listen), "--allowed-hosts", f"127.0.0.1:{listen}"]
        argv = browser_command(a, served, listen)[5:]
        folder, log = browser_folder(a), browser_log(a)
        reason = browser_preflight(a, record["port"], listen, folder, log)
        if not reason:
            offset = log.stat().st_size
            with open(log, "ab") as out, scratch.producing(a):
                proc = scratch.spawn(argv, actor_confined=True, cwd=scratch.browser_path(a.run_dir), stdin=subprocess.DEVNULL,
                             stdout=out, stderr=subprocess.STDOUT, env=BROWSER_ENV)
            add_browser_group(a, proc.pid)
            reason = browser_answers(proc, listen, log, offset)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        reason = str(error) or type(error).__name__
    if reason:
        return failed_server(a, proc, reason)
    return Served(proc, {"type": "http", "url": f"http://127.0.0.1:{listen}/mcp"}, "")


def without_browser(config_json: str) -> str:
    try:
        others = {k: v for k, v in json.loads(config_json or "{}").get("mcpServers", {}).items() if k != "playwright"}
    except (ValueError, AttributeError):
        return ""
    return json.dumps({"mcpServers": others}) if others else ""


def serve_browser(a: scratch.Attempt, config_json: str, add_dirs) -> tuple[str, str]:
    try:
        servers = json.loads(config_json or "{}").get("mcpServers") or {}
        words = servers["playwright"]["args"][2:]
        output = Path(words[words.index("--output-dir") + 1])
        own = json.loads(mcp_config(output))["mcpServers"]["playwright"]
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        return config_json, ""
    if servers["playwright"] != own or output.resolve() != scratch.evidence_path(a.run_dir).resolve():
        return config_json, ""
    served = SERVED.get(a.selector)
    if served is None:
        served = SERVED[a.selector] = start_served(a, words)
    elif served.proc is not None and served.proc.poll() is not None:
        code = served.proc.returncode
        reason = f"the browser server exited {code}: {chrome_error(log_since(browser_log(a), 0), code)}"
        served = SERVED[a.selector] = failed_server(a, served.proc, reason)
    if served.reason:
        return without_browser(config_json), served.reason
    others = {k: v for k, v in servers.items() if k != "playwright"}
    return json.dumps({"mcpServers": {"playwright": served.entry, **others}}), ""


def stop_served(a: scratch.Attempt) -> None:
    served = SERVED.pop(a.selector, None)
    if served is not None and served.proc is not None:
        util.stop_group(served.proc)


def browser_blocked_note(reason: str) -> str:
    return ("## Browser unavailable\n\n"
            "Arete could not start the confined browser for this verify. Its reason:\n\n"
            f"> {reason}\n\n"
            "No `mcp__playwright__` tool is loaded, and none may be called or searched for. This replaces the browser "
            "instructions above. Record each browser check as not passed, with `saw` starting `blocked: "
            f"{reason}`. Every other check (curl, CLI) still runs. The verdict is `blocked`, with that line in "
            "`blocked_by`, unless another check fails.")


def servers_allowed(a: scratch.Attempt, mcp_config: str) -> bool:
    try:
        servers = json.loads(mcp_config or "{}").get("mcpServers")
        return not servers or servers == {"playwright": browser_server(a)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def with_note(kw: dict, text: str) -> None:
    kw["system"] = "\n\n".join(filter(None, [kw.get("system", ""), text]))


def confine(kw: dict, attempt: scratch.Attempt, which: str, remote: bool,
            say: Callable[[str], None]) -> tuple[dict, tuple]:
    original = kw.get("mcp_config", "")
    if which != "claude" or remote:
        stop_served(attempt)
    if which == "codex" and not remote:
        try:
            kw["mcp_config"] = browser_config(attempt, original, kw.get("add_dirs", ()))
        except (OSError, ValueError, KeyError, TypeError) as error:
            kw["budget_error"] = f"browser confinement unavailable: {error}"
    try:
        hook = scratch.activate(attempt, which, bool(remote), kw.get("add_dirs", ()), kw.get("mcp_config", ""))
    except (OSError, ValueError, KeyError, TypeError):
        hook = ()
    if (not hook and which == "codex" and not remote and has_servers(original)
            and not kw.get("budget_error")
            and (kw.get("mcp_config", "") != original or plain_config(attempt, original) is None)):
        kw["budget_error"] = "browser confinement unavailable: the trusted browser could not be activated"
    if not hook:
        if "mcp_config" in kw:
            kw["mcp_config"] = original
        say("trusted scratch authority unavailable for this confinement; fixture-dependent checks may be blocked")
        with_note(kw, scratch.note(None))
    if which == "claude" and not remote:
        try:
            config_json, reason = serve_browser(attempt, original, kw.get("add_dirs", ()))
        except (OSError, ValueError, KeyError, TypeError) as error:
            config_json = without_browser(original)
            reason = str(error) or type(error).__name__
        if reason:
            kw["allowed_tools"] = [t for t in kw.get("allowed_tools") or [] if not t.startswith("mcp__playwright")]
            with_note(kw, browser_blocked_note(reason))
            say(f"the confined browser is unavailable for this verify: {reason}")
        if "mcp_config" in kw:
            kw["mcp_config"] = config_json
    kw["verification_attempt"] = attempt
    return kw, hook
