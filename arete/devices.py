from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import paths, util

DEVICES_FILE = paths.CONFIG_DIR / "devices.toml"
CACHE_FILE = paths.STATE_DIR / "devices.json"
KINDS = ("local", "ssh", "exe", "proxmox")
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=30"]
PROBE = ("uname -sm; echo \"$HOME\"; for t in claude pi arete ollama; do command -v $t >/dev/null && echo has:$t; "
         "done; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/gpu:/'")
CLONE_BOOT_SECONDS = 180
GUEST_HOST_KEYS = "sh -c 'cat /etc/ssh/ssh_host_*_key.pub'"
HOST_KEY = re.compile(r"^(?:ssh|ecdsa|sk)-[\w@.-]+ [A-Za-z0-9+/]+={0,3}(?:\s|$)")
GUEST_EXEC_REFUSED = re.compile(r"guest-exec\S* has been disabled", re.IGNORECASE)
END_WITH_SESSION = ("( (kill -0 $PPID && while kill -0 $PPID && kill -0 $$; do sleep 1; done && kill -0 $$ && kill -KILL -$$)"
                    " </dev/null >/dev/null 2>&1 & ); exec \"$@\"")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HOME_DIR = re.compile(r"~([a-z_][a-z0-9_.-]*)?")
VM_NAME_CHARS = 60
EXE_LOBBY = "exe.dev"
EXE_VM_DOMAIN = ".exe.xyz"
EXE_TAG = "arete"
EXE_EGRESS = ("integrations", "all")
EXE_BOOT_SECONDS = 120
NOT_DELETED = "could not delete sandbox"


@dataclass(frozen=True)
class Device:
    name: str
    kind: str = "local"
    host: str = ""
    dir: str = "~"
    port: int | None = None
    identity: str = ""
    jump: str = ""
    template: str = ""
    user: str = "root"
    egress: str = ""
    keep: bool = False
    sandbox: bool = False
    note: str = ""
    via: Device | None = None
    known_hosts: str = ""

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
        if kind not in ("local", "exe") and not d.get("host"):
            raise DeviceError(f"device {name}: needs host, the ssh destination")
        template = str(d.get("template") or "")
        if kind == "proxmox" and not template.isdigit():
            raise DeviceError(f"device {name}: a proxmox device needs template, the VM id to clone")
        egress = str(d.get("egress") or "")
        if egress and egress not in EXE_EGRESS:
            raise DeviceError(f"device {name}: egress '{egress}' is not one of {', '.join(EXE_EGRESS)}")
        found.append(Device(name, kind, host=str(d.get("host") or (EXE_LOBBY if kind == "exe" else "")),
                            dir=str(d.get("dir", "~")), port=int(d["port"]) if d.get("port") else None,
                            identity=str(d.get("identity", "")), jump=str(d.get("jump", "")), template=template,
                            user=str(d.get("user", "root")), egress=egress, keep=bool(d.get("keep", False)),
                            sandbox=kind == "exe", note=str(d.get("note", ""))))
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


def exe_vm(host: str) -> bool:
    return host.rpartition("@")[2].endswith(EXE_VM_DOMAIN)


def ssh_argv(d: Device) -> list[str]:
    argv = ["ssh", *SSH_OPTIONS]
    if d.known_hosts:
        argv += ["-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=/dev/null", "-o", "KnownHostsCommand=none",
                 "-o", f"UserKnownHostsFile=\"{d.known_hosts.replace('%', '%%')}\""]
    if exe_vm(d.host):
        # Every exe.dev VM presents exe.dev's own host key, so the known_hosts line for exe.dev verifies them all.
        argv += ["-o", f"HostKeyAlias={EXE_LOBBY}"]
    if d.port:
        argv += ["-p", str(d.port)]
    if d.identity:
        argv += ["-i", str(paths.expand(d.identity))]
    if d.jump:
        argv += ["-J", d.jump]
    if d.via:
        argv += ["-o", f"ProxyCommand={proxy_command(d.via)}"]
    return [*argv, d.host]


def proxy_command(hop: Device) -> str:
    ssh, *reach_hop = ssh_argv(hop)
    return f"{ssh} -W '[%h]:%p' {shlex.join(reach_hop).replace('%', '%%')}"


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
    argv = ["sh", "-c", END_WITH_SESSION, "sh", *argv]
    steps = [f"mkdir -p {remote_dir(m)}" for m in mkdirs]
    steps += [f"cd {remote_dir(cwd or d.dir)}", f"exec {'env ' + exports + ' ' if exports else ''}{shlex.join(argv)}"]
    return [*ssh_argv(d), " && ".join(steps)]


def probe(device: Device | str, timeout: float = 20) -> dict:
    d = resolve(device)
    started = time.monotonic()
    if d.kind == "proxmox":
        res = util.sh([*ssh_argv(d), f"qm config {d.template} | grep -E '^(name|template|memory|cores):'"],
                      timeout=timeout)
        info = {"ok": res.returncode == 0, "detail": util.first_line(", ".join(res.stdout.splitlines())
                                                                      if res.returncode == 0 else res.stderr, 300)}
    elif d.kind == "exe":
        info = probe_sandbox(d, timeout)
    else:
        info = probe_shell(d, timeout)
    info.update(name=d.name, kind=d.kind, host=d.host, seconds=round(time.monotonic() - started, 1), at=util.iso())
    with util.lock("devices", wait=True):
        cache = util.read_json(CACHE_FILE, {})
        cache[d.name] = info
        util.write_json(CACHE_FILE, cache)
    return info


def probe_shell(d: Device, timeout: float) -> dict:
    res = util.sh(PROBE if not d.remote else [*ssh_argv(d), PROBE], timeout=timeout)
    lines = res.stdout.splitlines()
    return {"ok": res.returncode == 0 and bool(lines), "system": lines[0] if lines else "",
            "home": lines[1] if len(lines) > 1 else "",
            "tools": [line[4:] for line in lines if line.startswith("has:")],
            "gpus": [line[4:].strip() for line in lines if line.startswith("gpu:")],
            "detail": "" if res.returncode == 0 else util.first_line(res.stderr or res.stdout, 300)}


def probe_sandbox(d: Device, timeout: float) -> dict:
    said: list[str] = []
    started = time.monotonic()
    try:
        with sandbox(replace(d, keep=False), f"probe-{d.name}-{util.now():%Y%m%d-%H%M%S}-{os.urandom(2).hex()}",
                     said.append) as vm:
            up = time.monotonic() - started
            info = probe_shell(vm, timeout)
    except DeviceError as e:
        info, up = {"ok": False, "detail": str(e)}, 0.0
    if leaked := [line for line in said if line.startswith(NOT_DELETED)]:
        return {**info, "ok": False, "detail": util.first_line("; ".join([info["detail"], *leaked]).lstrip("; "), 300)}
    if info["detail"]:
        return info
    made = f"a copy of {d.template}" if d.template else "a fresh sandbox"
    return {**info, "detail": f"{made} answered in {up:.1f} s and was deleted"}


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


def exe(d: Device, *command: str, timeout: float = 120) -> dict:
    res = util.sh([*ssh_argv(d), shlex.join([*command, "--json"])], timeout=timeout)
    lines = res.stdout.strip().splitlines()
    try:
        reply = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        reply = None
    if res.returncode == 0 and isinstance(reply, dict) and not reply.get("error"):
        return reply
    why = (reply.get("error") if isinstance(reply, dict) else "") or \
        util.first_line(res.stderr or res.stdout or f"exit {res.returncode}", 300)
    raise DeviceError(f"{d.name}: `ssh {d.host} {command[0]}` failed: {why}")


def vm_name(label: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
    return "arete-" + slug[len("arete-") - VM_NAME_CHARS:].lstrip("-")


@dataclass(frozen=True)
class BootingClone:
    proxmox: Device
    vmid: int
    seconds: int = CLONE_BOOT_SECONDS
    started: float = field(default_factory=time.monotonic)

    @property
    def late(self) -> bool:
        return time.monotonic() >= self.started + self.seconds

    def fault(self, text: str) -> DeviceError:
        return DeviceError(f"{self.proxmox.name}: clone {self.vmid} {text}")

    def guest(self, verb: str, args: str, timeout: float) -> subprocess.CompletedProcess:
        return util.sh([*ssh_argv(self.proxmox), f"qm guest {verb} {self.vmid} {args}"], timeout=timeout)

    def reply(self, output: str, shape: type):
        try:
            parsed = json.loads(output) if output.strip() else shape()
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, shape):
            raise self.fault(f"gave a guest agent reply arete cannot read: {util.first_line(output, 300)}")
        return parsed


def clone_address(boot: BootingClone) -> str:
    while not boot.late:
        res = boot.guest("cmd", "network-get-interfaces", 30)
        if res.returncode == 0:
            for iface in boot.reply(res.stdout, list):
                for a in iface.get("ip-addresses", []):
                    if a.get("ip-address-type") == "ipv4" and (address := routable_ipv4(a.get("ip-address"))):
                        return address
        time.sleep(5)
    raise boot.fault(f"reported no address in {boot.seconds}s")


def routable_ipv4(text: object) -> str:
    try:
        address = ipaddress.IPv4Address(str(text))
    except ValueError:
        return ""
    unroutable = (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast
                  or address.is_reserved)
    return "" if unroutable else str(address)


def guest_host_keys(boot: BootingClone) -> list[str]:
    res = boot.guest("exec", f"-- {GUEST_HOST_KEYS}", 60)
    if GUEST_EXEC_REFUSED.search(res.stderr + res.stdout):
        raise boot.fault("has guest-exec disabled in its guest agent, so arete cannot read its ssh host keys to check "
                         "them; allow guest-exec in the template's qemu-guest-agent")
    if res.returncode != 0:
        return []
    out = str(boot.reply(res.stdout, dict).get("out-data", ""))
    return [" ".join(line.split()[:2]) for line in out.splitlines() if HOST_KEY.match(line)]


def trusted_clone(boot: BootingClone, address: str, known_hosts: Path) -> Device:
    proxmox = boot.proxmox
    clone = replace(proxmox, name=f"{proxmox.name} clone", kind="ssh", host=f"{proxmox.user}@{address}", port=None,
                    jump="", via=proxmox, known_hosts=str(known_hosts))
    while True:
        keys = guest_host_keys(boot)
        known_hosts.write_text("".join(f"{address} {key}\n" for key in keys))
        if not keys:
            why = "its guest agent reported no ssh host key"
        elif (res := util.sh([*ssh_argv(clone), "true"], timeout=30)).returncode == 0:
            return clone
        else:
            why = util.last_line(res.stderr or res.stdout or f"exit {res.returncode}", 300)
        if boot.late:
            raise boot.fault(f"did not answer ssh with a host key its guest agent reported in {boot.seconds}s; "
                             f"the last try said: {why}")
        time.sleep(5)


def answers_ssh(vm: Device) -> None:
    deadline = time.monotonic() + EXE_BOOT_SECONDS
    while True:
        res = util.sh([*ssh_argv(vm), "true"], timeout=30)
        if res.returncode == 0:
            return
        if time.monotonic() > deadline:
            raise DeviceError(f"{vm.host} did not answer ssh in {EXE_BOOT_SECONDS}s: "
                              f"{util.first_line(res.stderr or res.stdout or f'exit {res.returncode}', 300)}")
        time.sleep(2)


@contextmanager
def lease(device: Device | str, label: str, log=lambda text: None):
    d = resolve(device)
    if d.kind == "proxmox":
        with proxmox_clone(d, label, log) as clone:
            yield clone
    elif d.kind == "exe":
        with sandbox(d, label, log) as vm:
            yield vm
    else:
        yield d


@contextmanager
def proxmox_clone(d: Device, label: str, log):
    vmid = int(pve(d, "pvesh get /cluster/nextid").strip())
    log(f"cloning template {d.template} to VM {vmid} on {d.name}")
    pve(d, f"qm clone {d.template} {vmid} --name {vm_name(label)} --full 0")
    try:
        pve(d, f"qm start {vmid}")
        boot = BootingClone(d, vmid)
        address = clone_address(boot)
        log(f"VM {vmid} is up at {address}")
        with tempfile.TemporaryDirectory(prefix="arete-clone-") as trust_dir:
            clone = trusted_clone(boot, address, Path(trust_dir) / "known_hosts")
            log(f"VM {vmid} answers ssh with a host key its guest agent reported")
            yield clone
    finally:
        if d.keep:
            log(f"keeping VM {vmid}")
        else:
            res = util.sh([*ssh_argv(d), f"qm stop {vmid} --skiplock 1; qm destroy {vmid} --purge 1"], timeout=300)
            log(f"destroyed VM {vmid}" if res.returncode == 0 else
                f"could not destroy VM {vmid} on {d.name}, so it is still there: "
                f"{util.first_line(res.stderr or res.stdout or f'exit {res.returncode}', 300)}")


@contextmanager
def sandbox(d: Device, label: str, log):
    name = vm_name(label)
    egress = [f"--egress={d.egress}"] if d.egress else []
    started = time.monotonic()
    try:
        if d.template:
            log(f"copying {d.template} to sandbox {name} on exe.dev")
            made = exe(d, "cp", d.template, name, "--sandbox", *egress)
            exe(d, "tag", name, EXE_TAG)
        else:
            log(f"creating sandbox {name} on exe.dev")
            made = exe(d, "new", f"--name={name}", "--sandbox", "--no-email", f"--tag={EXE_TAG}", *egress)
        vm = replace(d, kind="ssh", host=made.get("ssh_dest") or f"{name}{EXE_VM_DOMAIN}", port=None)
        answers_ssh(vm)
        log(f"sandbox {name} is up at {vm.host} in {time.monotonic() - started:.1f} s")
        yield vm
    finally:
        if d.keep:
            log(f"keeping sandbox {name}, which is billed until `ssh {d.host} rm {name}` deletes it")
        else:
            drop_sandbox(d, name, log)


def drop_sandbox(d: Device, name: str, log) -> None:
    try:
        gone = exe(d, "rm", name)
    except DeviceError as e:
        log(f"{NOT_DELETED} {name}, so it is still there and still billed: {e}")
        return
    if name in gone.get("deleted", []):
        log(f"deleted sandbox {name}")
    elif name not in gone.get("not_found", []):
        log(f"{NOT_DELETED} {name}, so it is still there and still billed: "
            f"{(gone.get('errors') or {}).get(name) or 'exe.dev gave no reason'}")


def release(device: str, label: str, log=lambda text: None) -> None:
    try:
        d = get(device)
    except DeviceError as e:
        log(f"arete cannot check whether this run left a sandbox: {e}")
        return
    if d.kind == "exe" and not d.keep:
        drop_sandbox(d, vm_name(label), log)


def known() -> list[Device]:
    try:
        return load()
    except DeviceError:
        return [LOCAL]


def views() -> list[dict]:
    known = cached()
    return [{**d.view(), "probe": known.get(d.name)} for d in load()]
