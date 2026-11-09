from __future__ import annotations

import json
import re
import shlex
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, replace

from . import paths, util

DEVICES_FILE = paths.CONFIG_DIR / "devices.toml"
CACHE_FILE = paths.STATE_DIR / "devices.json"
KINDS = ("local", "ssh", "exe", "proxmox")
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=30"]
PROBE = ("uname -sm; echo \"$HOME\"; for t in claude pi arete ollama; do command -v $t >/dev/null && echo has:$t; "
         "done; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/gpu:/'")
CLONE_BOOT_SECONDS = 180
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HOME_DIR = re.compile(r"~([a-z_][a-z0-9_.-]*)?")


@dataclass(frozen=True)
class Device:
    name: str
    kind: str = "local"
    host: str = ""
    dir: str = "~"
    port: int | None = None
    identity: str = ""
    jump: str = ""
    template: int | None = None
    user: str = "root"
    keep: bool = False
    note: str = ""

    @property
    def remote(self) -> bool:
        return self.kind != "local"

    def view(self) -> dict:
        return {"name": self.name, "kind": self.kind, "host": self.host, "dir": self.dir if self.remote else "",
                "template": self.template, "note": self.note}


LOCAL = Device("local", note="this machine")


class DeviceError(RuntimeError):
    pass


def load() -> list[Device]:
    if not DEVICES_FILE.is_file():
        return [LOCAL]
    try:
        return [LOCAL, *parse(tomllib.loads(DEVICES_FILE.read_text()))]
    except (ValueError, TypeError, AttributeError) as e:
        raise DeviceError(f"{paths.short_path(DEVICES_FILE)}: {e}") from e


def parse(data: dict) -> list[Device]:
    found = []
    for name, d in data.get("device", {}).items():
        kind = d.get("kind", "ssh")
        if kind not in KINDS:
            raise DeviceError(f"device {name}: kind '{kind}' is not one of {', '.join(KINDS)}")
        if kind != "local" and not d.get("host"):
            raise DeviceError(f"device {name}: needs host, the ssh destination")
        if kind == "proxmox" and not d.get("template"):
            raise DeviceError(f"device {name}: a proxmox device needs template, the VM id to clone")
        found.append(Device(name, kind, str(d.get("host", "")), str(d.get("dir", "~")),
                            int(d["port"]) if d.get("port") else None, str(d.get("identity", "")),
                            str(d.get("jump", "")), int(d["template"]) if d.get("template") else None,
                            str(d.get("user", "root")), bool(d.get("keep", False)), str(d.get("note", ""))))
    return found


def get(name: str) -> Device:
    if (name or LOCAL.name) == LOCAL.name:
        return LOCAL
    for d in load():
        if d.name == name:
            return d
    raise DeviceError(f"no device named {name}; add it to {paths.short_path(DEVICES_FILE)}")


def resolve(device: Device | str) -> Device:
    return device if isinstance(device, Device) else get(device)


def ssh_argv(d: Device) -> list[str]:
    argv = ["ssh", *SSH_OPTIONS]
    if d.port:
        argv += ["-p", str(d.port)]
    if d.identity:
        argv += ["-i", str(paths.expand(d.identity))]
    if d.jump:
        argv += ["-J", d.jump]
    return [*argv, d.host]


def remote_dir(path) -> str:
    text = str(path)
    home, slash, rest = text.partition("/")
    if not HOME_DIR.fullmatch(home):
        return shlex.quote(text)
    return home + slash + (shlex.quote(rest) if rest else "")


def wrap(device: Device | str, argv: list[str], cwd, env: dict | None = None, mkdirs: tuple = ()) -> list[str]:
    d = resolve(device)
    if not d.remote:
        return argv
    exports = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (env or {}).items() if ENV_NAME.match(k))
    steps = [f"mkdir -p {remote_dir(m)}" for m in mkdirs]
    steps += [f"cd {remote_dir(cwd or d.dir)}", f"exec {'env ' + exports + ' ' if exports else ''}{shlex.join(argv)}"]
    return [*ssh_argv(d), " && ".join(steps)]


def probe(device: Device | str, timeout: float = 20) -> dict:
    d = resolve(device)
    started = time.monotonic()
    if d.kind == "proxmox":
        res = util.sh([*ssh_argv(d), f"qm config {d.template} | grep -E '^(name|template|memory|cores):'"],
                      timeout=timeout)
        info = {"ok": res.returncode == 0, "detail": util.first_line(res.stdout.replace("\n", ", ")
                                                                      if res.returncode == 0 else res.stderr, 300)}
    else:
        res = util.sh(PROBE if not d.remote else [*ssh_argv(d), PROBE], timeout=timeout)
        lines = res.stdout.splitlines()
        info = {"ok": res.returncode == 0 and bool(lines), "system": lines[0] if lines else "",
                "home": lines[1] if len(lines) > 1 else "",
                "tools": [line[4:] for line in lines if line.startswith("has:")],
                "gpus": [line[4:].strip() for line in lines if line.startswith("gpu:")],
                "detail": "" if res.returncode == 0 else util.first_line(res.stderr or res.stdout, 300)}
    info.update(name=d.name, kind=d.kind, host=d.host, seconds=round(time.monotonic() - started, 1), at=util.iso())
    with util.lock("devices", wait=True):
        cache = util.read_json(CACHE_FILE, {})
        cache[d.name] = info
        util.write_json(CACHE_FILE, cache)
    return info


def cached() -> dict:
    return util.read_json(CACHE_FILE, {})


def home(device: Device | str) -> str:
    d = resolve(device)
    if not d.remote:
        return str(paths.HOME)
    entry = cached().get(d.name, {})
    known = entry.get("home") if entry.get("host") == d.host else ""
    return known or probe(d).get("home") or "~"


def pve(d: Device, command: str, timeout: float = 300) -> str:
    res = util.sh([*ssh_argv(d), command], timeout=timeout)
    if res.returncode != 0:
        raise DeviceError(f"{d.name}: `{command}` failed: {util.first_line(res.stderr or res.stdout, 300)}")
    return res.stdout


def clone_address(d: Device, vmid: int) -> str:
    deadline = time.monotonic() + CLONE_BOOT_SECONDS
    while time.monotonic() < deadline:
        res = util.sh([*ssh_argv(d), f"qm guest cmd {vmid} network-get-interfaces"], timeout=30)
        if res.returncode == 0:
            for iface in json.loads(res.stdout or "[]"):
                for a in iface.get("ip-addresses", []):
                    if a.get("ip-address-type") == "ipv4" and not a["ip-address"].startswith("127."):
                        return a["ip-address"]
        time.sleep(5)
    raise DeviceError(f"{d.name}: clone {vmid} reported no address in {CLONE_BOOT_SECONDS}s")


@contextmanager
def lease(device: Device | str, label: str, log=lambda text: None):
    d = resolve(device)
    if d.kind != "proxmox":
        yield d
        return
    vmid = int(pve(d, "pvesh get /cluster/nextid").strip())
    name = re.sub(r"[^a-z0-9-]+", "-", f"arete-{label}".lower())[:60].strip("-")
    log(f"cloning template {d.template} to VM {vmid} on {d.name}")
    pve(d, f"qm clone {d.template} {vmid} --name {name} --full 0")
    try:
        pve(d, f"qm start {vmid}")
        address = clone_address(d, vmid)
        log(f"VM {vmid} is up at {address}")
        yield replace(d, name=f"{d.name} clone", kind="ssh", host=f"{d.user}@{address}", port=None,
                      jump=",".join(filter(None, [d.jump, f"{d.host}:{d.port}" if d.port else d.host])))
    finally:
        if d.keep:
            log(f"keeping VM {vmid}")
        else:
            res = util.sh([*ssh_argv(d), f"qm stop {vmid} --skiplock 1; qm destroy {vmid} --purge 1"], timeout=300)
            log(f"destroyed VM {vmid}" if res.returncode == 0 else
                f"could not destroy VM {vmid} on {d.name}, so it is still there: "
                f"{util.first_line(res.stderr or res.stdout or f'exit {res.returncode}', 300)}")


def known() -> list[Device]:
    try:
        return load()
    except DeviceError:
        return [LOCAL]


def views() -> list[dict]:
    known = cached()
    return [{**d.view(), "probe": known.get(d.name)} for d in load()]
