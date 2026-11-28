from __future__ import annotations

import atexit
import glob
import json
import math
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NoReturn

from . import codebase, config, credential_http, paths, principals, util
from .store import db, tokens

CERT_SECONDS = 120
RENEW_SECONDS = 7 * 24 * 3600
CERT_RETRY_SECONDS = 3600
HANDSHAKE_SECONDS = 10
TAILSCALE_APPS = ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                  "/Applications/*/Tailscale.app/Contents/MacOS/Tailscale")
TAILSCALE_AS_CLI = {"TAILSCALE_BE_CLI": "1"}
RETRY_SECONDS = 10
WATCH_SECONDS = 30
WHOIS_SECONDS = 60
PROXY_SECONDS = 20
TLS_HANDSHAKE = b"\x16\x03"


def tailscale_exe() -> str:
    return shutil.which("tailscale") or next((m for pattern in TAILSCALE_APPS for m in sorted(glob.glob(pattern))), "")


def tailscale_run(*args: str, timeout: float) -> subprocess.CompletedProcess | None:
    from . import environment

    exe = tailscale_exe()
    if not exe:
        return None
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, errors="replace", timeout=timeout,
                              check=False, env=environment.build((), TAILSCALE_AS_CLI))
    except subprocess.TimeoutExpired:
        return None


def tailscale(*args: str) -> dict:
    proc = tailscale_run(*args, timeout=10)
    try:
        return json.loads(proc.stdout) if proc and proc.returncode == 0 else {}
    except json.JSONDecodeError:
        return {}


@dataclass(frozen=True)
class Node:
    addresses: tuple[str, ...]
    names: frozenset[str]
    name: str


class CertError(Exception):
    pass


def serve_entries(status: dict) -> dict[int, dict[str, str]]:
    found: dict[int, dict[str, str]] = {}
    for scope in (status, *((status.get("Foreground") or {}).values())):
        if not isinstance(scope, dict):
            continue
        for hostport, web in (scope.get("Web") or {}).items():
            try:
                port = int(str(hostport).rsplit(":", 1)[1])
            except (IndexError, ValueError):
                continue
            found.setdefault(port, {})
            for mount, handler in ((web or {}).get("Handlers") or {}).items():
                target = (handler or {}).get("Proxy") if isinstance(handler, dict) else None
                found.setdefault(port, {})[f"{hostport}{mount}"] = str(target or handler)
        for key, tcp in (scope.get("TCP") or {}).items():
            if str(key).isdecimal():
                found.setdefault(int(key), {})["tcp"] = str(tcp)
    return found


class Tailscale:
    def node(self) -> Node | None:
        status = tailscale("status", "--json")
        me = status.get("Self") or {}
        addresses = tuple(me.get("TailscaleIPs") or ())
        dns_name = (me.get("DNSName") or "").rstrip(".").lower()
        if status.get("BackendState") != "Running" or not addresses:
            return None
        return Node(addresses, frozenset({*addresses, dns_name, dns_name.split(".")[0]} - {""}), dns_name)

    def seed_identity(self, login):
        status = tailscale("status", "--json")
        users = list((status.get("User") or {}).values())
        me = ((status.get("User") or {}).get(str((status.get("Self") or {}).get("UserID"))) or {})
        login = login or me.get("LoginName", "")
        profile = next((u for u in users if u.get("LoginName") == login), {})
        return login, profile.get("DisplayName") or login.split("@")[0]

    def whois(self, address: str) -> tuple[str, str]:
        found = tailscale("whois", "--json", address)
        node = found.get("Node") or {}
        return (found.get("UserProfile") or {}).get("LoginName", ""), str(node.get("StableID") or node.get("ID") or "")

    def certificate(self, name: str) -> str:
        proc = tailscale_run("cert", "--cert-file", "-", "--key-file", "-", name, timeout=CERT_SECONDS)
        if proc is None:
            raise CertError("tailscale is missing or did not finish in time")
        if proc.returncode != 0:
            raise CertError(util.first_line(proc.stderr or "", 200) or f"tailscale cert exited {proc.returncode}")
        return proc.stdout

    def served(self) -> dict[int, dict[str, str]] | None:
        proc = tailscale_run("serve", "status", "--json", timeout=PROXY_SECONDS)
        if proc is None or proc.returncode != 0:
            return None
        try:
            status = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        return serve_entries(status) if isinstance(status, dict) else None


class ScratchHost(Tailscale):
    def certificate(self, name: str) -> str:
        raise CertError("scratch state asks for no certificate")

    def served(self) -> dict[int, dict[str, str]] | None:
        return None


def host_services() -> Tailscale:
    return ScratchHost() if paths.ISOLATED else Tailscale()


def this_node(host: Tailscale | None = None) -> Node | None:
    return (host or host_services()).node()


def seed_owner(host, cfg):
    if principals.needs_owner_login():
        principals.seed_owner(*host.seed_identity(cfg.dashboard_owner))


@dataclass
class Logins:
    host: Tailscale
    seen: dict[str, tuple[float, str, str]] = field(default_factory=dict)
    guard: threading.Lock = field(default_factory=threading.Lock)
    credentials: credential_http.Traffic = field(default_factory=credential_http.Traffic)

    def of(self, address: str) -> str:
        with self.guard:
            seen = self.seen.get(address)
        if seen and time.monotonic() - seen[0] < WHOIS_SECONDS:
            return seen[1]
        found = self.host.whois(address)
        login, node = (found, "") if isinstance(found, str) else found
        if login or node:
            with self.guard:
                self.seen[address] = (time.monotonic(), login, node)
        return login

    def node_key(self, address: str) -> str:
        with self.guard:
            seen = self.seen.get(address)
            return seen[2] if seen and seen[2] else address


PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z ]+)-----.*?-----END \1-----\n?", re.DOTALL)


def pem_parts(text: str) -> tuple[bytes, bytes]:
    blocks = [(m[1], m[0]) for m in PEM_BLOCK.finditer(text)]
    key = "".join(b for label, b in blocks if "PRIVATE KEY" in label)
    chain = "".join(b for label, b in blocks if "CERTIFICATE" in label)
    return chain.encode(), key.encode()


def store_private(path: Path, content: bytes) -> bool:
    try:
        current = path.read_bytes()
    except OSError:
        current = None
    if current == content:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            path.chmod(0o600)
        return False
    util.write_bytes_atomic(path, content)
    return True


def fetch_certificate(host: Tailscale, name: str) -> bool:
    if not name:
        raise CertError("this machine has no tailnet name")
    chain, key = pem_parts(host.certificate(name))
    if not chain or not key:
        raise CertError("tailscale cert did not give a certificate and a key")
    try:
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        changed = store_private(paths.TLS_KEY_FILE, key)
        return store_private(paths.TLS_CERT_FILE, chain) or changed
    except OSError as e:
        raise CertError(f"cannot store the certificate: {e.strerror or e}") from None


def tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(paths.TLS_CERT_FILE, paths.TLS_KEY_FILE)
    return context


def host_name(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


@dataclass(frozen=True)
class Handlers:
    open: type[BaseHTTPRequestHandler]
    redirect: type[BaseHTTPRequestHandler]


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: str, port: int, node: Node, logins: Logins, inside_serve: bool,
                 handler: type[BaseHTTPRequestHandler], redirect: type[BaseHTTPRequestHandler],
                 tls: ssl.SSLContext | None = None):
        self.address_family = socket.AF_INET6 if ":" in address else socket.AF_INET
        self.node, self.logins, self.inside_serve, self.tls = node, logins, inside_serve, tls
        self.redirect = redirect
        self.pending_secrets: dict[tuple[str, str], str] = {}
        from . import inbox

        inbox.on_server_start(self.pending_secrets)
        super().__init__((address, port), handler)

    def finish_request(self, request, client_address) -> None:
        if self.tls is None:
            return super().finish_request(request, client_address)
        request.settimeout(HANDSHAKE_SECONDS)
        try:
            first = request.recv(1, socket.MSG_PEEK)
            if first != b"\x16":
                request.settimeout(None)
                return self.redirect(request, client_address, self)
            secure = self.tls.wrap_socket(request, server_side=True)
        except OSError:
            return
        try:
            secure.settimeout(None)
            self.RequestHandlerClass(secure, client_address, self)
        finally:
            try:
                secure.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            secure.close()


@dataclass(frozen=True)
class Front:
    scheme: str
    servers: list[Server]
    tls: ssl.SSLContext | None = None
    why: str = ""
    mapping: str = ""


def say(text: str) -> None:
    print(f"{util.iso()} {text}", flush=True)


def close_all(servers: list[Server]) -> None:
    for s in servers:
        s.server_close()


def stop_listener(server: Server | None) -> None:
    if server is not None:
        server.shutdown()
        server.server_close()


def open_servers(addresses: tuple[str, ...], port: int, node: Node, logins: Logins, inside_serve: bool,
                 handlers: Handlers) -> list[Server]:
    servers: list[Server] = []
    try:
        for address in addresses:
            servers.append(Server(address, port, node, logins, inside_serve, handlers.open, handlers.redirect))
    except OSError:
        close_all(servers)
        raise
    return servers


def serve_status(host: Tailscale) -> dict[int, dict[str, str]] | None:
    try:
        status = host.served()
    except (OSError, subprocess.SubprocessError):
        return None
    return None if status is None else {int(p): dict(e) for p, e in status.items()}


def open_front(host: Tailscale, node: Node, port: int, logins: Logins, inside_serve: bool, handlers: Handlers,
               secure: bool = False) -> Front:
    status = serve_status(host)
    mapping = (f"tailscale serve has a mapping on port {port}, which takes this port's tailnet traffic; "
               f"turn it off (for an HTTPS mapping, `tailscale serve --https={port} off`)") \
        if status is not None and port in status else ""
    opened = open_servers(node.addresses, port, node, logins, inside_serve, handlers)
    try:
        tls, reason = None, ""
        if secure:
            try:
                fetch_certificate(host, node.name)
                tls = tls_context()
            except (CertError, ssl.SSLError, OSError) as e:
                reason = f"no certificate ({util.first_line(str(e), 200)})"
        for s in opened:
            s.tls = tls
        return Front("https" if tls else "http", opened, tls=tls, why=reason, mapping=mapping)
    except BaseException:
        close_all(opened)
        raise


def record_server(node: Node, port: int, front: Front) -> None:
    fields = {"pid": os.getpid(), "engine": codebase.STAMP, "addresses": list(node.addresses), "port": port,
              "scheme": front.scheme, "host": node.name,
              "default_token_dir": paths.TOKEN_CLIENT_DIR == paths.DEFAULT_TOKEN_CLIENT_DIR}
    if fields["default_token_dir"] and paths.DB_FILE.is_file():
        fields["instance"] = db.schema_metadata()["instance_id"]
    try:
        util.write_json(paths.SERVER_FILE, {**fields, **paths.state_and_config(), "at": util.iso()})
    except OSError as e:
        say(f"cannot write the server file ({e.strerror or e}); the CLI will not find this dashboard")


def watch(host: Tailscale, node: Node, front: Front, secure: bool = False) -> str:
    certificate_due = time.monotonic() + (RENEW_SECONDS if front.tls else CERT_RETRY_SECONDS) \
        if secure else math.inf
    renewal_failed = False
    reload_pending = False
    while True:
        now = host.node()
        if now != node:
            return "the tailnet address or name changed"
        time.sleep(WATCH_SECONDS)
        if now is None or time.monotonic() < certificate_due:
            continue
        if not secure:
            continue
        try:
            reload_pending = fetch_certificate(host, node.name) or reload_pending
            if front.tls and reload_pending:
                fresh = tls_context()
                for s in front.servers:
                    s.tls = fresh
                reload_pending = False
        except (CertError, ssl.SSLError, OSError) as e:
            certificate_due = time.monotonic() + CERT_RETRY_SECONDS
            if front.tls and not renewal_failed:
                say(f"the certificate was not renewed ({util.first_line(str(e), 200)}); keeping the old one")
            renewal_failed = True
            continue
        renewal_failed = False
        if not front.tls:
            return "a certificate is ready"
        certificate_due = time.monotonic() + RENEW_SECONDS


def serve(handlers: Handlers, cfg: config.Config, port: int, inside_serve: bool = False,
          host: Tailscale | None = None) -> NoReturn:
    host = host or host_services()
    logins = Logins(host)
    hold = util.lock_file(f"dashboard-{port}")
    held = False
    waiting_for = ""
    last_line = ""
    atexit.register(forget_server)
    try:
        while True:
            node = host.node()
            try:
                if node is None:
                    raise OSError("tailscale is not running or reports no address")
                if not held:
                    held = util.flock_now(hold)
                    if held:
                        try:
                            (paths.STATE_DIR / f"dashboard-proxy-{port}.json").unlink(missing_ok=True)
                        except OSError as e:
                            say(f"cannot remove the old dashboard proxy record ({e.strerror or e})")
                if not held:
                    raise OSError(f"another dashboard on this state holds port {port}")
                seed_owner(host, cfg)
                tokens.init_owner()
                front = open_front(host, node, port, logins, inside_serve, handlers, cfg.dashboard_https)
            except OSError as e:
                if str(e) != waiting_for:
                    say(f"cannot listen yet ({e}); trying again every {RETRY_SECONDS} s")
                    waiting_for = str(e)
                time.sleep(RETRY_SECONDS)
                continue
            waiting_for = ""
            for s in front.servers:
                threading.Thread(target=s.serve_forever, daemon=True).start()
            last_line = announce(node, port, front, last_line)
            reason = watch(host, node, front, cfg.dashboard_https)
            say(f"{reason}; listening again")
            for s in front.servers:
                stop_listener(s)
            forget_server()
    finally:
        forget_server()


def announce(node: Node, port: int, front: Front, last_line: str) -> str:
    line = listening_line(node, port, front)
    if line != last_line:
        say(line)
    record_server(node, port, front)
    return line


def listening_line(node: Node, port: int, front: Front) -> str:
    if front.tls:
        line = f"dashboard listening on port {port} at {', '.join(node.addresses)} over HTTPS as {node.name}"
    else:
        line = f"dashboard listening on port {port} at {', '.join(node.addresses)} over plain HTTP"
    if front.why:
        line += f"; {front.why}; trying again every hour"
    if front.mapping:
        line += f"; {front.mapping}"
    return line


def url(node: Node | None, port: int) -> str:
    found = util.read_json(paths.SERVER_FILE, None)
    mine = paths.state_and_config()
    recorded = isinstance(found, dict) and {k: found.get(k) for k in mine} == mine
    fallback = "https" if config.load_or_problem()[0].dashboard_https else "http"
    scheme = found["scheme"] if recorded and found.get("scheme") in ("http", "https") else fallback
    if node is None and scheme == "http":
        return f"http://<this machine's tailnet address>:{port}"
    if node is None:
        return f"{scheme}://<this machine's tailnet name>:{port}"
    return f"{scheme}://{node.name if scheme == 'https' else node.addresses[0]}:{port}"


def forget_server() -> None:
    found = util.read_json(paths.SERVER_FILE, None)
    if isinstance(found, dict) and found.get("pid") == os.getpid():
        paths.SERVER_FILE.unlink(missing_ok=True)
