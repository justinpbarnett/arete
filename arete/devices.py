from __future__ import annotations

import re
import signal
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from datetime import timedelta
from pathlib import Path

from . import exedev, paths, proxmox, record, ssh, util
from .ssh import LOCAL, Device, DeviceError, NeverSent, NoAnswer
from .store import devices as device_store

DEVICES_FILE = paths.CONFIG_DIR / "devices.toml"
CACHE_FILE = paths.STATE_DIR / "devices.json"
KINDS = ("local", "ssh", "exe", "proxmox")
PROBE = ("uname -sm; echo \"$HOME\"; for t in claude pi codex arete ollama; do command -v $t >/dev/null && echo has:$t; "
         "done; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/gpu:/'")
VM_NAME_CHARS = 60
VM_PREFIX = "arete-"
EXE_EGRESS = ("integrations", "all")
PROBE_SANDBOX_MINUTES = 10
CREATE_MAY_FINISH_MINUTES = 10
SWEEP_GIVES_UP_DAYS = 7


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
        if kind == "exe" and ssh.is_exe_vm(str(d.get("host") or "")):
            raise DeviceError(f"device {name}: an exe device makes a sandbox per run, so its host is the exe.dev "
                              f"lobby; reach a VM you keep with kind = \"ssh\" and host = \"{d['host']}\"")
        template = str(d.get("template") or "")
        if kind == "proxmox" and not template.isdigit():
            raise DeviceError(f"device {name}: a proxmox device needs template, the VM id to clone")
        egress = str(d.get("egress") or "")
        if kind == "exe" and egress and egress not in EXE_EGRESS:
            raise DeviceError(f"device {name}: egress '{egress}' is not one of {', '.join(EXE_EGRESS)}")
        found.append(Device(name, kind, host=str(d.get("host") or (ssh.EXE_LOBBY if kind == "exe" else "")),
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


def probe(device: Device | str, timeout: float = 20) -> dict:
    d = resolve(device)
    started = time.monotonic()
    if d.kind == "proxmox":
        res = util.sh([*ssh.ssh_argv(d), f"qm config {d.template} | grep -E '^(name|template|memory|cores):'"],
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
    res = util.sh(PROBE if not d.remote else [*ssh.ssh_argv(d), PROBE], timeout=timeout)
    lines = res.stdout.splitlines()
    return {"ok": res.returncode == 0 and bool(lines), "system": lines[0] if lines else "",
            "home": lines[1] if len(lines) > 1 else "",
            "tools": [line[4:] for line in lines if line.startswith("has:")],
            "gpus": [line[4:].strip() for line in lines if line.startswith("gpu:")],
            "detail": "" if res.returncode == 0 else util.first_line(res.stderr or res.stdout, 300)}


def probe_sandbox(d: Device, timeout: float) -> dict:
    sandbox = exedev.Sandbox(d, vm_name(record.new_id(f"probe-{d.name}")))
    started = time.monotonic()
    try:
        vm = create_sandbox(sandbox, None, lambda text: None)
    except DeviceError as e:
        return probe_fault(d, e)
    try:
        exedev.boot_sandbox(sandbox, vm)
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


def vm_name(label: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
    room = VM_NAME_CHARS - len(VM_PREFIX)
    return VM_PREFIX + slug[-room:].lstrip("-")


@contextmanager
def lease(device: Device | str, run_id: str, log=lambda text: None, hold=lambda: ""):
    from . import device_turns
    from .store import db

    d = resolve(device)
    turns = device_turns.component(f"{db.schema_metadata()['instance_tag']}-{run_id}-1") if d.remote else ""
    if d.kind == "proxmox":
        with proxmox_clone(d, run_id, log, hold) as clone:
            record.mark(run_id, "lease", device=d.name, device_kind=d.kind, address=clone.host)
            yield replace(clone, turns=turns)
    elif d.kind == "exe":
        with fresh_sandbox(d, run_id, log, hold) as vm:
            record.mark(run_id, "lease", device=d.name, device_kind=d.kind, address=vm.host)
            yield replace(vm, turns=turns)
    else:
        record.mark(run_id, "lease", device=d.name, device_kind=d.kind)
        leased = replace(d, turns=turns)
        try:
            yield leased
        finally:
            if d.remote:
                try:
                    res = device_turns.control(leased, f"rm -rf {device_turns.folder(leased)}")
                    if res.returncode:
                        log(f"remote turn cleanup: {ssh.failure(res)}")
                except Exception as e:
                    log(f"remote turn cleanup: {e}")


@contextmanager
def proxmox_clone(d: Device, run_id: str, log, hold=lambda: ""):
    clone = proxmox.Clone(d, vm_name(run_id), int(proxmox.pve(d, "pvesh get /cluster/nextid").strip()))
    log(f"cloning template {d.template} to {clone} on {d.name}")
    with creating(clone, run_id, log), record.timed(run_id, "clone", device=d.name, vmid=clone.vmid,
                                                    template=d.template):
        proxmox.pve(d, f"qm clone {d.template} {clone.vmid} --name {clone.name} --full 0")
    with kept_or_deleted(clone, log, hold):
        with record.timed(run_id, "boot", device=d.name, vmid=clone.vmid) as booted:
            proxmox.pve(d, f"qm start {clone.vmid}")
            boot = proxmox.BootingClone(d, clone.vmid)
            booted["address"] = address = proxmox.clone_address(boot)
        log(f"{clone} is up at {address}")
        with tempfile.TemporaryDirectory(prefix="arete-clone-") as trust_dir:
            vm = proxmox.trusted_clone(boot, address, Path(trust_dir) / "known_hosts")
            log(f"{clone} answers ssh with a host key its guest agent reported")
            yield vm


@contextmanager
def fresh_sandbox(d: Device, run_id: str, log, hold=lambda: ""):
    sandbox = exedev.Sandbox(d, vm_name(run_id))
    started = time.monotonic()
    with record.timed(run_id, "create", device=sandbox.device.name, sandbox=sandbox.name):
        vm = create_sandbox(sandbox, run_id, log)
    try:
        with record.timed(run_id, "boot", device=sandbox.device.name, sandbox=sandbox.name):
            exedev.boot_sandbox(sandbox, vm)
    except BaseException:
        delete_or_say_why(sandbox, log)
        raise
    log(f"{sandbox} is up at {vm.host} in {time.monotonic() - started:.1f} s")
    with kept_or_deleted(sandbox, log, hold):
        yield vm


@contextmanager
def creating(vm: proxmox.Clone | exedev.Sandbox, run_id: str | None, log):
    device_store.add_vm(run_id=run_id, **asdict(vm))
    try:
        yield
    except NeverSent:
        device_store.forget_vm(vm.name)
        raise
    except BaseException as e:
        if isinstance(e, DeviceError) and not isinstance(e, NoAnswer):
            device_store.note_vm_answered(vm.name)
        delete_or_say_why(vm, log)
        raise
    device_store.note_vm_answered(vm.name)


@contextmanager
def kept_or_deleted(vm: proxmox.Clone | exedev.Sandbox, log, hold=lambda: ""):
    if vm.device.keep:
        device_store.forget_vm(vm.name)
    try:
        yield
    finally:
        if vm.device.keep:
            log(f"keeping {vm} up until {vm.delete_command()} deletes it")
        elif reason := hold():
            device_store.forget_vm(vm.name)
            log(f"keeping {vm} up because {reason}; {vm.delete_command()} deletes it")
        else:
            delete_or_say_why(vm, log)


def create_sandbox(sandbox: exedev.Sandbox, run_id: str | None, log) -> Device:
    d, name = sandbox.device, sandbox.name
    egress = [f"--egress={d.egress}"] if d.egress else []
    with creating(sandbox, run_id, log):
        if d.template:
            log(f"copying {d.template} to {sandbox} on exe.dev")
            created = exedev.exe(d, "cp", d.template, name, "--sandbox", *egress)
        else:
            log(f"creating {sandbox} on exe.dev")
            created = exedev.exe(d, "new", f"--name={name}", "--sandbox", "--no-email", f"--tag={exedev.EXE_TAG}", *egress)
    return replace(d, name=f"{d.name} sandbox", kind="ssh", host=created.get("ssh_dest") or f"{name}{ssh.EXE_VM_DOMAIN}",
                   port=None)


def delete_listed(vm: proxmox.Clone | exedev.Sandbox, log) -> str:
    with ignoring_stops():
        not_deleted = vm.delete(log)
    if not_deleted:
        device_store.note_delete_failed(vm.name)
    else:
        device_store.forget_gone_vm(vm.name, util.iso(util.now() - timedelta(minutes=CREATE_MAY_FINISH_MINUTES)))
    return not_deleted


def delete_or_say_why(vm: proxmox.Clone | exedev.Sandbox, log) -> str:
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
    for row in device_store.vms_of_run(run_id):
        delete_or_say_why(listed_vm(row), log)


def sweep(log_for: Callable[[str | None], Callable[[str], None]]) -> None:
    now = util.now()
    give_up_before = util.iso(now - timedelta(days=SWEEP_GIVES_UP_DAYS))
    for row in device_store.vms_left_over(util.iso(now - timedelta(minutes=PROBE_SANDBOX_MINUTES))):
        vm, log = listed_vm(row), log_for(row["run_id"])
        if not row["failing_since"]:
            delete_or_say_why(vm, log)
        elif (not_deleted := delete_listed(vm, log)) and row["failing_since"] < give_up_before:
            device_store.forget_vm(vm.name)
            log(f"{not_deleted}; arete serve stops trying after {SWEEP_GIVES_UP_DAYS} days, and "
                f"{vm.delete_command()} deletes it if it is still there")


def left_up(run_id: str) -> list[str]:
    vms = [listed_vm(row) for row in device_store.vms_of_run(run_id) if row["failing_since"]]
    return [f"{vm} is still up until {vm.delete_command()} deletes it" for vm in vms]


def with_left_up(summary: str, run_id: str) -> str:
    return "; ".join(filter(None, [summary, *left_up(run_id)]))


def outcome_of(e: Exception, run_id: str) -> tuple[str, str]:
    if isinstance(e, record.Stopped):
        return "stopped", record.STOPPED if record.stop_asked(run_id) else "stopped by a signal"
    if isinstance(e, DeviceError):
        return "failed", util.one_line(str(e), 500)
    return "failed", f"{type(e).__name__}: {util.first_line(str(e), 300)}"


def listed_vm(row: dict) -> proxmox.Clone | exedev.Sandbox:
    saved = row["device"]
    d = Device(**{f.name: saved[f.name] for f in fields(Device) if f.name in saved})
    return proxmox.Clone(d, row["name"], row["vmid"]) if d.kind == "proxmox" else exedev.Sandbox(d, row["name"])


def known() -> list[Device]:
    try:
        return load()
    except DeviceError:
        return [LOCAL]


def views() -> list[dict]:
    known = cached()
    return [{**d.view(), "probe": known.get(d.name)} for d in load()]
