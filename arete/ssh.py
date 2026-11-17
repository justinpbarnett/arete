from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from . import paths, refusals, util

SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=30", "-o", "ForwardAgent=no"]
SSH_FAILED = 255
SSH_FAILED_BEFORE_SENDING = re.compile(r"Could not resolve hostname|ssh: connect to host|kex_exchange_identification|"
                                       r"banner exchange|Host key verification failed|Permission denied \(")
HOME_DIR = re.compile(r"~([a-z_][a-z0-9_.-]*)?")
EXE_LOBBY = "exe.dev"
EXE_VM_DOMAIN = ".exe.xyz"
EXE_VMS_PRESENT_HOST_KEY_OF = EXE_LOBBY


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
    exe_sandbox: bool = False
    note: str = ""
    via: Device | None = None
    known_hosts: str = ""
    turns: str = ""

    @property
    def remote(self) -> bool:
        return self.kind != "local"

    def view(self) -> dict:
        return {"name": self.name, "kind": self.kind, "host": self.host, "dir": self.dir if self.remote else "",
                "template": self.template, "note": self.note}


LOCAL = Device("local", note="this machine")


class DeviceError(refusals.Invalid, RuntimeError):
    pass


class NoAnswer(DeviceError):
    pass


class NeverSent(DeviceError):
    pass


def is_exe_vm(host: str) -> bool:
    return host.rpartition("@")[2].endswith(EXE_VM_DOMAIN)


def ssh_argv(d: Device) -> list[str]:
    argv = ["ssh", *SSH_OPTIONS]
    if d.known_hosts:
        argv += ["-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=/dev/null", "-o", "KnownHostsCommand=none",
                 "-o", f"UserKnownHostsFile=\"{d.known_hosts.replace('%', '%%')}\""]
    if is_exe_vm(d.host):
        argv += ["-o", f"HostKeyAlias={EXE_VMS_PRESENT_HOST_KEY_OF}"]
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


def failure(res) -> str:
    return util.first_line(res.stderr or res.stdout or f"exit {res.returncode}", 300)
