from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import ssh, util
from .ssh import Device, DeviceError, NoAnswer

CLONE_BOOT_SECONDS = 180
GUEST_EXEC_REFUSED = re.compile(r"guest-exec\S* has been disabled", re.IGNORECASE)
HOST_KEY = re.compile(r"^(?:ssh|ecdsa|sk)-[\w@.-]+ [A-Za-z0-9+/]+={0,3}(?:\s|$)")
GUEST_HOST_KEYS = "sh -c 'cat /etc/ssh/ssh_host_*_key.pub'"


def pve(d: Device, command: str, timeout: float = 300) -> str:
    res = util.sh([*ssh.ssh_argv(d), command], timeout=timeout)
    if res.returncode != 0:
        raise (NoAnswer if res.returncode == util.TIMED_OUT else DeviceError)(
            f"{d.name}: `{command}` failed: {ssh.failure(res)}")
    return res.stdout


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
        return util.sh([*ssh.ssh_argv(self.proxmox), f"qm guest {verb} {self.vmid} {args}"], timeout=timeout)

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
        elif (res := util.sh([*ssh.ssh_argv(clone), "true"], timeout=30)).returncode == 0:
            return clone
        else:
            why = util.last_line(res.stderr or res.stdout or f"exit {res.returncode}", 300)
        if boot.late:
            raise boot.fault(f"did not answer ssh with a host key its guest agent reported in {boot.seconds}s; "
                             f"the last try said: {why}")
        time.sleep(5)


@dataclass(frozen=True)
class Clone:
    device: Device
    name: str
    vmid: int

    def __str__(self) -> str:
        return f"VM {self.vmid} ({self.name})"

    def delete_command(self) -> str:
        return f"`qm stop {self.vmid}; qm destroy {self.vmid} --purge 1` on {self.device.host}"

    def delete(self, log) -> str:
        found = util.sh([*ssh.ssh_argv(self.device), f"qm config {self.vmid}"], timeout=60)
        vmid_still_names_this_clone = found.returncode == 0 and f"name: {self.name}" in found.stdout.splitlines()
        clone_gone_and_vmid_maybe_reused = found.returncode == 0 or "does not exist" in found.stderr
        if vmid_still_names_this_clone:
            res = util.sh([*ssh.ssh_argv(self.device), f"qm stop {self.vmid} --skiplock 1; qm destroy {self.vmid} --purge 1"],
                          timeout=300)
        elif clone_gone_and_vmid_maybe_reused:
            return ""
        else:
            res = found
        if res.returncode == 0:
            log(f"destroyed {self}")
            return ""
        return f"could not destroy {self} on {self.device.name}: {ssh.failure(res)}"
