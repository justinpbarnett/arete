from __future__ import annotations

import ipaddress
import json
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import paths, record, state, util

DEVICES_FILE = paths.CONFIG_DIR / "devices.toml"
CACHE_FILE = paths.STATE_DIR / "devices.json"
KINDS = ("local", "ssh", "exe", "proxmox")
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=30", "-o", "ForwardAgent=no"]
SSH_FAILED = 255
SSH_FAILED_BEFORE_SENDING = re.compile(r"Could not resolve hostname|ssh: connect to host|kex_exchange_identification|"
                                       r"banner exchange|Host key verification failed|Permission denied \(")
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
VM_PREFIX = "arete-"
EXE_LOBBY = "exe.dev"
EXE_VM_DOMAIN = ".exe.xyz"
EXE_TAG = "arete"
EXE_EGRESS = ("integrations", "all")
EXE_BOOT_SECONDS = 120
GIT_FORGES = {"github": "GitHub", "gitlab": "GitLab", "bitbucket": "Bitbucket", "forgejo": "Forgejo", "gitea": "Gitea"}
GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "gitea.com", "sr.ht", "dev.azure.com",
             "visualstudio.com", "gitee.com")
READONLY_KINDS = ("github",)
PLAIN_NAME = re.compile(r"[\w.-]+")
FIND_GIT_CREDENTIALS = r"""
config=${XDG_CONFIG_HOME:-$HOME/.config}
{
  for f in ~/.git-credentials "$config/git/credentials" ~/.netrc "${GH_CONFIG_DIR:-$config/gh}/hosts.yml"; do
    test -s "$f" && echo "$f"
  done
  git config --get-regexp '^credential\..*helper$' >/dev/null 2>&1 && echo "a git credential.helper"
  git config --get-regexp '^url\..*\.insteadof$' 2>/dev/null | grep -qE 'https?://[^/ ]+@' &&
    echo "a git url.insteadOf that holds a credential"
  for v in GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN; do
    test -n "$(printenv $v)" && echo "\$$v"
  done
  grep -lE '(GH|GITHUB)(_ENTERPRISE)?_TOKEN=' ~/.profile ~/.bash_profile ~/.bash_login ~/.bashrc ~/.zshenv ~/.zprofile \
    ~/.zshrc /etc/environment /etc/profile /etc/bash.bashrc /etc/profile.d/* 2>/dev/null | sed 's/^/a GitHub token in /'
  test ! -e ~/.ssh || grep -rl "PRIVATE KEY" ~/.ssh 2>/dev/null || test $? = 1 || echo "~/.ssh (arete could not read all of it)"
} | sed "s|$HOME/|~/|g"
"""
PROBE_SANDBOX_MINUTES = 10
CREATE_MAY_FINISH_MINUTES = 10
SWEEP_GIVES_UP_DAYS = 7


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

    @property
    def remote(self) -> bool:
        return self.kind != "local"

    def view(self) -> dict:
        return {"name": self.name, "kind": self.kind, "host": self.host, "dir": self.dir if self.remote else "",
                "template": self.template, "note": self.note}


LOCAL = Device("local", note="this machine")


class DeviceError(RuntimeError):
    pass


class NoAnswer(DeviceError):
    pass


class NeverSent(DeviceError):
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
        if kind == "exe" and is_exe_vm(str(d.get("host") or "")):
            raise DeviceError(f"device {name}: an exe device makes a sandbox per run, so its host is the exe.dev "
                              f"lobby; reach a VM you keep with kind = \"ssh\" and host = \"{d['host']}\"")
        template = str(d.get("template") or "")
        if kind == "proxmox" and not template.isdigit():
            raise DeviceError(f"device {name}: a proxmox device needs template, the VM id to clone")
        egress = str(d.get("egress") or "")
        if kind == "exe" and egress and egress not in EXE_EGRESS:
            raise DeviceError(f"device {name}: egress '{egress}' is not one of {', '.join(EXE_EGRESS)}")
        found.append(Device(name, kind, host=str(d.get("host") or (EXE_LOBBY if kind == "exe" else "")),
                            dir=str(d.get("dir", "~")), port=int(d["port"]) if d.get("port") else None,
                            identity=str(d.get("identity", "")), jump=str(d.get("jump", "")), template=template,
                            user=str(d.get("user", "root")), egress=egress, keep=bool(d.get("keep", False)),
                            exe_sandbox=kind == "exe", note=str(d.get("note", ""))))
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


def is_exe_vm(host: str) -> bool:
    return host.rpartition("@")[2].endswith(EXE_VM_DOMAIN)


def ssh_argv(d: Device) -> list[str]:
    argv = ["ssh", *SSH_OPTIONS]
    if d.known_hosts:
        argv += ["-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=/dev/null", "-o", "KnownHostsCommand=none",
                 "-o", f"UserKnownHostsFile=\"{d.known_hosts.replace('%', '%%')}\""]
    if is_exe_vm(d.host):
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
    sandbox = Sandbox(d, vm_name(record.new_id(f"probe-{d.name}")))
    started = time.monotonic()
    try:
        vm = create_sandbox(sandbox, None, lambda text: None)
    except DeviceError as e:
        return probe_fault(d, e)
    try:
        boot_sandbox(sandbox, vm)
        seconds_to_ssh = time.monotonic() - started
        info = probe_shell(vm, timeout)
    except DeviceError as e:
        info = probe_fault(d, e)
    finally:
        not_deleted = delete_or_say_why(sandbox, lambda text: None)
    if not_deleted:
        return {**info, "ok": False, "detail": "; ".join(filter(None, [info["detail"], not_deleted]))}
    if info["detail"]:
        return info
    sandbox_words = f"a copy of {d.template}" if d.template else "a fresh sandbox"
    return {**info, "detail": f"{sandbox_words} was up in {seconds_to_ssh:.1f} s and was deleted"}


def probe_fault(d: Device, e: DeviceError) -> dict:
    return {"ok": False, "detail": str(e).removeprefix(f"{d.name}: ")}


def cached() -> dict:
    return util.read_json(CACHE_FILE, {})


def home(device: Device | str) -> str:
    d = resolve(device)
    if not d.remote:
        return str(paths.HOME)
    entry = cached().get(d.name, {})
    known = entry.get("home") if entry.get("host") == d.host else ""
    return known or probe(d).get("home") or "~"


def failure(res) -> str:
    return util.first_line(res.stderr or res.stdout or f"exit {res.returncode}", 300)


def pve(d: Device, command: str, timeout: float = 300) -> str:
    res = util.sh([*ssh_argv(d), command], timeout=timeout)
    if res.returncode != 0:
        raise (NoAnswer if res.returncode == util.TIMED_OUT else DeviceError)(
            f"{d.name}: `{command}` failed: {failure(res)}")
    return res.stdout


def exe(d: Device, *command: str, timeout: float = 120) -> dict:
    return exe_reply(d, command, timeout, "an object", lambda reply: isinstance(reply, dict))


def exe_list(d: Device, *command: str) -> list[dict]:
    return exe_reply(d, command, 120, "a list of objects",
                     lambda reply: isinstance(reply, list) and all(isinstance(row, dict) for row in reply))


def exe_reply(d: Device, command: tuple[str, ...], timeout: float, shape: str, has_shape: Callable[[object], bool]):
    res = util.sh([*ssh_argv(d), shlex.join([*command, "--json"])], timeout=timeout)
    reply = json_reply(res.stdout)
    failed = f"{d.name}: `ssh {d.host} {command[0]}` failed: "
    if isinstance(reply, dict) and reply.get("error"):
        raise DeviceError(failed + util.one_line(str(reply["error"])))
    if has_shape(reply):
        return reply
    if reply is not None:
        raise DeviceError(f"{failed}its answer is not {shape}")
    never_sent = res.returncode == SSH_FAILED and SSH_FAILED_BEFORE_SENDING.search(res.stderr or "")
    raise (NeverSent if never_sent else NoAnswer)(failed + failure(res))


def json_reply(stdout: str):
    for text in (stdout, *stdout.strip().splitlines()[-1:]):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return None


def vm_name(label: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
    room = VM_NAME_CHARS - len(VM_PREFIX)
    return VM_PREFIX + slug[-room:].lstrip("-")


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


def wait_for_ssh(vm: Device) -> None:
    deadline = time.monotonic() + EXE_BOOT_SECONDS
    while True:
        res = util.sh([*ssh_argv(vm), "true"], timeout=30)
        if res.returncode == 0:
            return
        if time.monotonic() > deadline:
            raise DeviceError(f"{vm.host} did not answer ssh in {EXE_BOOT_SECONDS}s: {failure(res)}")
        time.sleep(2)


@contextmanager
def lease(device: Device | str, run_id: str, log=lambda text: None):
    d = resolve(device)
    if d.kind == "proxmox":
        with proxmox_clone(d, run_id, log) as clone:
            record.mark(run_id, "lease", device=d.name, device_kind=d.kind, address=clone.host)
            yield clone
    elif d.kind == "exe":
        with fresh_sandbox(d, run_id, log) as vm:
            record.mark(run_id, "lease", device=d.name, device_kind=d.kind, address=vm.host)
            yield vm
    else:
        record.mark(run_id, "lease", device=d.name, device_kind=d.kind)
        yield d


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
        found = util.sh([*ssh_argv(self.device), f"qm config {self.vmid}"], timeout=60)
        if found.returncode == 0 and f"name: {self.name}" in found.stdout.splitlines():
            res = util.sh([*ssh_argv(self.device), f"qm stop {self.vmid} --skiplock 1; qm destroy {self.vmid} --purge 1"],
                          timeout=300)
        elif found.returncode == 0 or "does not exist" in found.stderr:
            # The clone is gone, and proxmox may have given its vmid to another VM since.
            return ""
        else:
            res = found
        if res.returncode == 0:
            log(f"destroyed {self}")
            return ""
        return f"could not destroy {self} on {self.device.name}: {failure(res)}"


@dataclass(frozen=True)
class Sandbox:
    device: Device
    name: str

    def __str__(self) -> str:
        return f"sandbox {self.name}"

    def delete_command(self) -> str:
        return f"`ssh {self.device.host} rm {self.name}`"

    def delete(self, log) -> str:
        try:
            reply = exe(self.device, "rm", self.name)
        except DeviceError as e:
            return f"could not delete {self}: {e}"
        if self.name in (reply.get("deleted") or []):
            log(f"deleted {self}")
            return ""
        if self.name in (reply.get("not_found") or []):
            return ""
        errors = reply.get("errors")
        why = errors.get(self.name) if isinstance(errors, dict) else errors
        return f"could not delete {self}: {util.one_line(str(why or 'exe.dev gave no reason'))}"


@contextmanager
def proxmox_clone(d: Device, run_id: str, log):
    clone = Clone(d, vm_name(run_id), int(pve(d, "pvesh get /cluster/nextid").strip()))
    log(f"cloning template {d.template} to {clone} on {d.name}")
    with creating(clone, run_id, log), record.timed(run_id, "clone", device=d.name, vmid=clone.vmid,
                                                    template=d.template):
        pve(d, f"qm clone {d.template} {clone.vmid} --name {clone.name} --full 0")
    with kept_or_deleted(clone, log):
        with record.timed(run_id, "boot", device=d.name, vmid=clone.vmid) as booted:
            pve(d, f"qm start {clone.vmid}")
            boot = BootingClone(d, clone.vmid)
            booted["address"] = address = clone_address(boot)
        log(f"{clone} is up at {address}")
        with tempfile.TemporaryDirectory(prefix="arete-clone-") as trust_dir:
            vm = trusted_clone(boot, address, Path(trust_dir) / "known_hosts")
            log(f"{clone} answers ssh with a host key its guest agent reported")
            yield vm


@contextmanager
def fresh_sandbox(d: Device, run_id: str, log):
    sandbox = Sandbox(d, vm_name(run_id))
    started = time.monotonic()
    with record.timed(run_id, "create", device=sandbox.device.name, sandbox=sandbox.name):
        vm = create_sandbox(sandbox, run_id, log)
    try:
        with record.timed(run_id, "boot", device=sandbox.device.name, sandbox=sandbox.name):
            boot_sandbox(sandbox, vm)
    except BaseException:
        delete_or_say_why(sandbox, log)
        raise
    log(f"{sandbox} is up at {vm.host} in {time.monotonic() - started:.1f} s")
    with kept_or_deleted(sandbox, log):
        yield vm


@contextmanager
def creating(vm: Clone | Sandbox, run_id: str | None, log):
    state.add_vm(run_id=run_id, **asdict(vm))
    try:
        yield
    except NeverSent:
        state.forget_vm(vm.name)
        raise
    except BaseException as e:
        if isinstance(e, DeviceError) and not isinstance(e, NoAnswer):
            state.note_vm_answered(vm.name)
        delete_or_say_why(vm, log)
        raise
    state.note_vm_answered(vm.name)


@contextmanager
def kept_or_deleted(vm: Clone | Sandbox, log):
    if vm.device.keep:
        state.forget_vm(vm.name)
    try:
        yield
    finally:
        if vm.device.keep:
            log(f"keeping {vm} up until {vm.delete_command()} deletes it")
        else:
            delete_or_say_why(vm, log)


def create_sandbox(sandbox: Sandbox, run_id: str | None, log) -> Device:
    d, name = sandbox.device, sandbox.name
    egress = [f"--egress={d.egress}"] if d.egress else []
    with creating(sandbox, run_id, log):
        if d.template:
            log(f"copying {d.template} to {sandbox} on exe.dev")
            created = exe(d, "cp", d.template, name, "--sandbox", *egress)
        else:
            log(f"creating {sandbox} on exe.dev")
            created = exe(d, "new", f"--name={name}", "--sandbox", "--no-email", f"--tag={EXE_TAG}", *egress)
    return replace(d, name=f"{d.name} sandbox", kind="ssh", host=created.get("ssh_dest") or f"{name}{EXE_VM_DOMAIN}",
                   port=None)


def boot_sandbox(sandbox: Sandbox, vm: Device) -> None:
    refuse_pushing_integrations(sandbox, sandbox_tags(sandbox))
    wait_for_ssh(vm)
    refuse_git_credentials(sandbox, vm)


def sandbox_tags(sandbox: Sandbox) -> list[str]:
    if not sandbox.device.template:
        return [EXE_TAG]
    tags = exe(sandbox.device, "tag", sandbox.name, EXE_TAG).get("tags")
    if isinstance(tags, list) and EXE_TAG in tags:
        return tags
    raise DeviceError(f"exe.dev did not say which tags {sandbox} carries, so arete cannot tell which integrations "
                      f"reach it")


def refuse_pushing_integrations(sandbox: Sandbox, tags: list[str]) -> None:
    for integration in exe_list(sandbox.device, "integrations", "ls"):
        if (push := how_it_pushes(integration)) and (through := reached_through(integration, sandbox, tags)):
            name = integration.get("name") or "NAME"
            fix = "attach it only to VMs you keep"
            if kind_of(integration) in READONLY_KINDS:
                fix = f"make it read-only with `ssh {sandbox.device.host} integrations edit {name} --readonly`, or {fix}"
            raise DeviceError(f"{sandbox} could {push}: the integration {name} reaches it through "
                              f"{', '.join(through)}; {fix}")


def how_it_pushes(integration: dict) -> str | None:
    kind = kind_of(integration)
    if kind in READONLY_KINDS and read_only(integration):
        return None
    if forge := next((GIT_FORGES[s] for s in services_of(integration) if s in GIT_FORGES), None):
        return f"push to {forge}"
    if kind == "http-proxy":
        return how_a_proxy_pushes(integration.get("target"))
    if kind == "ssh-key":
        return "push over ssh with a key exe.dev holds"
    if kind == "exe-dev":
        return "push through a GitHub integration it attaches to itself, using exe.dev as you"
    return None


def kind_of(integration: dict) -> str:
    return normal_name(integration.get("type"))


def services_of(integration: dict) -> set[str]:
    return {kind_of(integration), normal_name(integration.get("catalogService")),
            normal_name(integration_config(integration).get("service"))}


def normal_name(value) -> str:
    return re.sub(r"[\W_]+", "-", str(value or "").lower()).strip("-")


def how_a_proxy_pushes(target) -> str | None:
    if not target:
        return None
    host = proxied_host(target)
    if not host:
        return "push through a proxy target arete cannot read"
    if any(host == h or host.endswith(f".{h}") for h in GIT_HOSTS):
        return f"push to {host}"
    return None


def proxied_host(target) -> str | None:
    if not isinstance(target, str):
        return None
    try:
        host = urlsplit(target if "://" in target else f"//{target}").hostname
    except ValueError:
        return None
    return host.rstrip(".") if host else None


def reached_through(integration: dict, sandbox: Sandbox, tags: list[str]) -> list[str]:
    attachments = integration.get("attachments") or []
    if not isinstance(attachments, list) or not all(isinstance(a, str) for a in attachments):
        return ["attachments arete cannot read"]
    if not attachments and is_true(integration.get("isTeam")):
        return ["the team, which exe.dev lists no attachments for"]
    return sorted(a for a in attachments if reaches(a, sandbox.name, tags))


def reaches(attachment: str, vm_name: str, tags: list[str]) -> bool:
    kind, _, target = attachment.partition(":")
    if kind == "vm" and PLAIN_NAME.fullmatch(target):
        return target == vm_name
    if kind == "tag" and PLAIN_NAME.fullmatch(target):
        return target in tags
    return True


def read_only(integration: dict) -> bool:
    # exe.dev does not document the field that marks a github integration read-only, so any key spelled readonly counts.
    settings = {**integration, **integration_config(integration)}
    return any(normal_name(key).replace("-", "") == "readonly" and is_true(value) for key, value in settings.items())


def integration_config(integration: dict) -> dict:
    settings = integration.get("config")
    return settings if isinstance(settings, dict) else {}


def is_true(value) -> bool:
    return str(value).lower() == "true"


def refuse_git_credentials(sandbox: Sandbox, vm: Device) -> None:
    res = util.sh([*ssh_argv(vm), f"sh -c {shlex.quote(FIND_GIT_CREDENTIALS)}"], timeout=30)
    if res.returncode != 0:
        raise DeviceError(f"could not check {sandbox} for git credentials: {failure(res)}")
    if found := [line for line in res.stdout.splitlines() if line.strip()]:
        source = f"the template {sandbox.device.template}" if sandbox.device.template else "exe.dev's image"
        raise DeviceError(f"{sandbox} holds git credentials ({', '.join(found)}), so an agent there could push; "
                          f"take them off {source}")


def delete_listed(vm: Clone | Sandbox, log) -> str:
    with ignoring_stops():
        not_deleted = vm.delete(log)
    if not_deleted:
        state.note_delete_failed(vm.name)
    else:
        state.forget_gone_vm(vm.name, util.iso(util.now() - timedelta(minutes=CREATE_MAY_FINISH_MINUTES)))
    return not_deleted


def delete_or_say_why(vm: Clone | Sandbox, log) -> str:
    if not_deleted := delete_listed(vm, log):
        not_deleted += f"; arete serve tries again, and {vm.delete_command()} deletes it now"
        log(not_deleted)
    return not_deleted


@contextmanager
def ignoring_stops():
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    before = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, before)


def release(run_id: str, log=lambda text: None) -> None:
    for row in state.vms_of_run(run_id):
        delete_or_say_why(listed_vm(row), log)


def sweep(log_for: Callable[[str | None], Callable[[str], None]]) -> None:
    now = util.now()
    give_up_before = util.iso(now - timedelta(days=SWEEP_GIVES_UP_DAYS))
    for row in state.vms_left_over(util.iso(now - timedelta(minutes=PROBE_SANDBOX_MINUTES))):
        vm, log = listed_vm(row), log_for(row["run_id"])
        if not row["failing_since"]:
            delete_or_say_why(vm, log)
        elif (not_deleted := delete_listed(vm, log)) and row["failing_since"] < give_up_before:
            state.forget_vm(vm.name)
            log(f"{not_deleted}; arete serve stops trying after {SWEEP_GIVES_UP_DAYS} days, and "
                f"{vm.delete_command()} deletes it if it is still there")


def left_up(run_id: str) -> list[str]:
    vms = [listed_vm(row) for row in state.vms_of_run(run_id) if row["failing_since"]]
    return [f"{vm} is still up until {vm.delete_command()} deletes it" for vm in vms]


def with_left_up(summary: str, run_id: str) -> str:
    return "; ".join(filter(None, [summary, *left_up(run_id)]))


def listed_vm(row: dict) -> Clone | Sandbox:
    saved = row["device"]
    d = Device(**{f.name: saved[f.name] for f in fields(Device) if f.name in saved})
    return Clone(d, row["name"], row["vmid"]) if d.kind == "proxmox" else Sandbox(d, row["name"])


def known() -> list[Device]:
    try:
        return load()
    except DeviceError:
        return [LOCAL]


def views() -> list[dict]:
    known = cached()
    return [{**d.view(), "probe": known.get(d.name)} for d in load()]
