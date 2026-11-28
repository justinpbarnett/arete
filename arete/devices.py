from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable
from contextlib import contextmanager, suppress
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

from . import events, exedev, leases, paths, principals, proxmox, record, ssh, util
from .ssh import LOCAL, Device, DeviceError, NeverSent, NoAnswer
from .store import config as config_store
from .store import db as foundation
from .store import devices as device_store
from .store import events as event_store
from .store import inbox as inbox_store

KINDS = ("local", "ssh", "exe", "proxmox")
RUNNERS = frozenset(("claude", "pi", "codex"))
PROBE = ("uname -sm; echo \"$HOME\"; for t in claude pi codex arete ollama; do command -v $t >/dev/null && echo has:$t; "
         "done; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/gpu:/'")
FACTORY_HOLD = "its session is paused by the factory switch"
VM_NAME_CHARS = 60
VM_PREFIX = "arete-"
EXE_EGRESS = ("integrations", "all")
CREATE_MAY_FINISH_MINUTES = 10
SWEEP_GIVES_UP_DAYS = 7
VMID_RETRY_MAX = 5

_ORIG_CLONE_ADDRESS = proxmox.clone_address
_ORIG_TRUSTED_CLONE = proxmox.trusted_clone
clone_address = proxmox.clone_address
trusted_clone = proxmox.trusted_clone

_PROBE_SEM = threading.BoundedSemaphore(4)


def default_local_file() -> Path:
    return paths.DEFAULTS_DIR / "devices" / "local.toml"


def devices_dir() -> Path:
    return paths.CONFIG_DIR / "devices"


def _legacy_devices_file() -> Path:
    return paths.CONFIG_DIR / "devices.toml"


def _load_default_local() -> Device:
    path = default_local_file()
    if path.is_file():
        try:
            return parse_device("local", tomllib.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return LOCAL


def parse_device(name: str, d: dict) -> Device:
    if not isinstance(d, dict):
        raise DeviceError(f"device {name}: expected a TOML table")
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
    port_raw = d.get("port")
    if isinstance(port_raw, bool):
        raise DeviceError(f"device {name}: port must be an integer")
    port = int(port_raw) if port_raw else None
    return Device(
        name,
        kind,
        host=str(d.get("host") or (ssh.EXE_LOBBY if kind == "exe" else "")),
        dir=str(d.get("dir", "~")),
        port=port,
        identity=str(d.get("identity", "")),
        jump=str(d.get("jump", "")),
        template=template,
        user=str(d.get("user", "root")),
        egress=egress,
        keep=bool(d.get("keep", False)),
        exe_sandbox=kind == "exe",
        note=str(d.get("note", "")),
    )


def parse(data: dict) -> list[Device]:
    found = []
    for name, d in data.get("device", {}).items():
        found.append(parse_device(name, d))
    return found


def load() -> list[Device]:
    folder = devices_dir()
    by_name: dict[str, Device] = {}
    if folder.is_dir():
        for path in sorted(folder.glob("*.toml")):
            if not path.is_file():
                continue
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                by_name[path.stem] = parse_device(path.stem, data)
            except DeviceError:
                raise
            except (OSError, ValueError, TypeError, AttributeError) as e:
                raise DeviceError(f"{paths.short_path(path)}: {e}") from e
    local_dev = by_name.pop(LOCAL.name, None) or _load_default_local()
    return [local_dev, *(by_name[k] for k in sorted(by_name))]


def projected_after_split() -> dict[str, Device]:
    found = {d.name: d for d in load()}
    legacy = _legacy_devices_file()
    if legacy.is_file():
        for d in parse(tomllib.loads(legacy.read_text(encoding="utf-8"))):
            found[d.name] = d
    return found


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def _serialize_device_toml(raw: dict) -> str:
    lines: list[str] = []
    for key, value in raw.items():
        if value is None:
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def split_devices_toml() -> str | None:
    from . import configrepo

    legacy = _legacy_devices_file()
    if not legacy.is_file():
        return None
    raw_text = legacy.read_text(encoding="utf-8")
    data = tomllib.loads(raw_text)
    parsed_devices = parse(data)
    raw_table = data.get("device", {})
    changes: dict[str, str | bytes | None] = {}
    base: dict[str, str] = {}
    for d in parsed_devices:
        rel = f"devices/{d.name}.toml"
        _, current_blob = configrepo.read(rel)
        changes[rel] = _serialize_device_toml(raw_table[d.name])
        base[rel] = current_blob
    _, legacy_blob = configrepo.read("devices.toml")
    changes["devices.toml"] = None
    base["devices.toml"] = legacy_blob
    configrepo.ensure_repo()
    return configrepo.commit(
        changes,
        base,
        actor="hand",
        source="engine",
        note="split devices.toml into devices/<name>.toml",
    )


def get(name: str) -> Device:
    wanted = name or LOCAL.name
    for d in load():
        if d.name == wanted:
            return d
    raise DeviceError(f"no device named {wanted}; add it to {paths.short_path(devices_dir() / f'{wanted}.toml')}")


def resolve(device: Device | str) -> Device:
    return device if isinstance(device, Device) else get(device)


def version_of(name: str, conn=None) -> str:
    file_path = devices_dir() / f"{name}.toml"
    if file_path.is_file():
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()[:12]
        return f"device/{name}@{digest}"
    if name == LOCAL.name and default_local_file().is_file():
        digest = hashlib.sha256(default_local_file().read_bytes()).hexdigest()[:12]
        return f"device/local@{digest}"
    if row := config_store.live_item("device", name, conn):
        return str(row["version"])
    return ""


def device_at_version(device_name: str, device_version: str) -> Device:
    if device_version:
        from . import configitems, configrepo

        if configrepo.is_repo():
            row = config_store.by_selector("device", device_name, device_version)
            if row:
                raw_text = configitems.revision_text(paths.CONFIG_DIR.resolve(), row["commit_sha"], row["path"])
                if raw_text is not None:
                    return parse_device(device_name, tomllib.loads(raw_text))
            with suppress(Exception):
                item = configitems.item("device", device_name)
                read = configitems.read(item, version=device_version)
                if isinstance(read.get("content"), str):
                    return parse_device(device_name, tomllib.loads(read["content"]))
        if device_name == LOCAL.name and default_local_file().is_file() and version_of(LOCAL.name) == device_version:
            return _load_default_local()
    return get(device_name)


def probe(device: Device | str, timeout: float = 20, *, idem: str | None = None) -> dict:
    d = resolve(device)
    dev_ver = version_of(d.name)
    started = time.monotonic()
    if d.kind == "proxmox":
        res = util.sh([*ssh.ssh_argv(d), f"qm config {d.template} | grep -E '^(name|template|memory|cores):'"],
                      timeout=timeout)
        info = {"ok": res.returncode == 0, "system": "", "home": "", "tools": [], "gpus": [],
                "detail": util.first_line(", ".join(res.stdout.splitlines()) if res.returncode == 0 else res.stderr, 300)}
    elif d.kind == "exe":
        info = probe_sandbox(d, timeout)
    else:
        info = probe_shell(d, timeout)
    at = util.iso()
    seconds = round(time.monotonic() - started, 1)
    info.update(name=d.name, kind=d.kind, host=d.host, device_version=dev_ver, seconds=seconds, at=at)
    with foundation.tx(fenced=False) as c:
        device_store.insert_device_probe(
            c,
            device=d.name,
            device_version=dev_ver,
            at=at,
            ok=bool(info["ok"]),
            seconds=seconds,
            system=str(info.get("system") or ""),
            home=str(info.get("home") or ""),
            tools=list(info.get("tools") or ()),
            gpus=list(info.get("gpus") or ()),
            detail=str(info.get("detail") or ""),
            facts=info,
        )
        if info["ok"]:
            device_store.clear_waits_for_blocker(c, f"device:{d.name}")
        events.emit(
            "device.changed",
            f"device:{d.name}",
            cls="derived",
            data={"device": d.name, "device_version": dev_ver, "ok": bool(info["ok"]), "probe": info},
            idem=idem,
            connection=c,
        )
    return info


def probe_async(
    device_name: str,
    *,
    idem: str | None = None,
    actor: str | None = None,
    via: str | None = None,
) -> concurrent.futures.Future:
    resolve(device_name)
    chosen_actor = actor or principals.current()
    chosen_via = via if via is not None else events.provenance()
    future: concurrent.futures.Future = concurrent.futures.Future()

    def run_probe() -> None:
        with _PROBE_SEM:
            try:
                with principals.acting(chosen_actor), event_store.request_scope(chosen_via):
                    result = probe(device_name, idem=idem)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    thread = threading.Thread(target=run_probe, name=f"arete-probe-{device_name}", daemon=True)
    thread.start()
    return future


def probe_shell(d: Device, timeout: float) -> dict:
    res = util.sh(PROBE if not d.remote else [*ssh.ssh_argv(d), PROBE], timeout=timeout)
    lines = res.stdout.splitlines()
    return {"ok": res.returncode == 0 and bool(lines), "system": lines[0] if lines else "",
            "home": lines[1] if len(lines) > 1 else "",
            "tools": [line[4:] for line in lines if line.startswith("has:")],
            "gpus": [line[4:].strip() for line in lines if line.startswith("gpu:")],
            "detail": "" if res.returncode == 0 else util.first_line(res.stderr or res.stdout, 300)}


def probe_sandbox(d: Device, timeout: float) -> dict:
    active = leases.claim(d.name, version_of(d.name), run_id=None)
    sandbox = exedev.Sandbox(d, vm_name(record.new_id(f"probe-{d.name}")))
    started = time.monotonic()
    try:
        try:
            vm = create_sandbox(sandbox, active, lambda text: None)
        except DeviceError as e:
            return probe_fault(d, e)
        try:
            exedev.boot_sandbox(sandbox, vm)
            active.update(ready=util.iso(), address=vm.host)
            seconds_to_ssh = time.monotonic() - started
            info = probe_shell(vm, timeout)
        except DeviceError as e:
            info = probe_fault(d, e)
        finally:
            not_deleted = _delete_and_close(sandbox, active, lambda text: None, fenced=False)
        if not_deleted:
            return {**info, "ok": False, "detail": "; ".join(filter(None, [info.get("detail", ""), not_deleted]))}
        if info.get("detail"):
            return info
        sandbox_words = f"a copy of {d.template}" if d.template else "a fresh sandbox"
        return {**info, "detail": f"{sandbox_words} was up in {seconds_to_ssh:.1f} s and was deleted"}
    finally:
        if not active.closed:
            active.close("", fenced=False)


def probe_fault(d: Device, e: DeviceError) -> dict:
    return {"ok": False, "system": "", "home": "", "tools": [], "gpus": [],
            "detail": str(e).removeprefix(f"{d.name}: ")}


def _probe_row_to_cached(row: dict) -> dict:
    facts = row.get("facts") if isinstance(row.get("facts"), dict) else {}
    return {
        "name": row["device"],
        "kind": facts.get("kind", ""),
        "host": facts.get("host", ""),
        "device_version": row["device_version"],
        "ok": bool(row["ok"]),
        "seconds": row["seconds"],
        "at": row["at"],
        "system": row["system"],
        "home": row["home"],
        "tools": list(row["tools"]),
        "gpus": list(row["gpus"]),
        "detail": row["detail"],
    }


def cached() -> dict[str, dict]:
    return {name: _probe_row_to_cached(row) for name, row in device_store.latest_device_probes().items()}


def home(device: Device | str) -> str:
    d = resolve(device)
    if not d.remote:
        return str(paths.HOME)
    entry = device_store.latest_device_probe(d.name)
    if entry and entry.get("device_version") == version_of(d.name) and entry.get("home"):
        return str(entry["home"])
    return probe(d).get("home") or "~"


def vm_name(label: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
    room = VM_NAME_CHARS - len(VM_PREFIX)
    return VM_PREFIX + slug[-room:].lstrip("-")


def is_vmid_collision(err: BaseException, vmid: int) -> bool:
    if not isinstance(err, DeviceError) or isinstance(err, NoAnswer):
        return False
    text = str(err)
    if f"unable to create VM {vmid}: config file already exists" in text:
        return True
    return bool(re.search(rf"can't lock file '[^']*?/lock-{vmid}\.conf' - got timeout", text))


def _host_key_fingerprint(known_hosts_path: str) -> str:
    if not known_hosts_path:
        return ""
    try:
        lines = Path(known_hosts_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        parts = line.split()
        if len(parts) >= 3:
            try:
                blob = base64.b64decode(parts[2].encode("ascii"), validate=True)
                digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
                return f"SHA256:{digest}"
            except ValueError:
                return f"SHA256:{hashlib.sha256(line.encode('utf-8')).hexdigest()[:32]}"
    return ""


def _call_clone_address(boot: proxmox.BootingClone) -> str:
    fn = clone_address if clone_address is not _ORIG_CLONE_ADDRESS else proxmox.clone_address
    return fn(boot)


def _call_trusted_clone(boot: proxmox.BootingClone, address: str, known_hosts: Path) -> Device:
    fn = trusted_clone if trusted_clone is not _ORIG_TRUSTED_CLONE else proxmox.trusted_clone
    return fn(boot, address, known_hosts)


def _delete_and_close(
    vm: proxmox.Clone | exedev.Sandbox,
    active: leases.ActiveLease,
    log,
    *,
    fenced: bool = True,
) -> str:
    with ignoring_stops():
        not_deleted = vm.delete(log)
    if not_deleted:
        now_iso = util.iso()
        active.close(
            "delete_failed",
            sweep_attempts=1,
            failing_since=now_iso,
            last_sweep=now_iso,
            last_sweep_error=not_deleted,
            fenced=fenced,
        )
        msg = f"{not_deleted}; arete serve tries again, and {vm.delete_command()} deletes it now"
        log(msg)
        return msg
    active.close("deleted", fenced=fenced)
    return ""


def _close_for_create_error(
    vm: proxmox.Clone | exedev.Sandbox,
    active: leases.ActiveLease,
    err: BaseException,
    log,
    *,
    fenced: bool,
) -> None:
    if isinstance(err, leases.LeaseFenced):
        active.abort_fenced()
        return
    if isinstance(err, NeverSent):
        active.close("", fenced=fenced)
        return
    if isinstance(err, NoAnswer):
        with ignoring_stops():
            vm.delete(log)
        active.close("never_answered", fenced=fenced)
        return
    _delete_and_close(vm, active, log, fenced=fenced)


@contextmanager
def lease(device: Device | str, run_id: str, log=lambda text: None, hold=lambda: ""):
    from . import device_turns
    from .store import db, factory

    d = resolve(device)
    dev_ver = version_of(d.name)
    turns = device_turns.component(f"{db.schema_metadata()['instance_tag']}-{run_id}-1") if d.remote else ""

    if d.kind in ("proxmox", "exe"):
        directory = paths.RUNS_DIR / run_id
        saved = directory / "factory-device.json"
        resume = os.getenv(factory.CONTINUATION) == run_id
        if resume:
            data = util.read_json(saved, None)
            if not data or data["source"] != asdict(d):
                raise DeviceError("The original device lease is unavailable or its configuration changed")
            leased = device_turns.saved_device(data["leased"])
            try:
                yield replace(leased, turns=f"{turns}-{factory.admit()}")
            finally:
                if not d.keep and not factory.interrupted(run_id) and not hold():
                    release(run_id, log)
            return

        def preserve() -> str:
            return FACTORY_HOLD if factory.interrupted(run_id) else hold()

        allocate = proxmox_clone if d.kind == "proxmox" else fresh_sandbox
        with allocate(d, dev_ver, run_id, log, preserve) as vm:
            if vm.known_hosts:
                pinned = directory / "factory-known-hosts"
                util.write_durably(pinned, Path(vm.known_hosts).read_bytes(), 0o600)
                vm = replace(vm, known_hosts=str(pinned))
            leased = replace(vm, turns=turns)
            util.write_durably(saved, json.dumps({"source": asdict(d), "leased": asdict(leased)}).encode(), 0o600)
            yield leased
        return

    active = leases.claim(d.name, dev_ver, run_id=run_id)
    token_ctx = leases._ACTIVE_LEASE.set(active)
    if d.remote and os.getenv(factory.CONTINUATION) == run_id:
        turns = f"{turns}-{factory.admit()}"
    leased = replace(d, turns=turns)
    fenced_exc = False
    try:
        yield leased
    except leases.LeaseFenced:
        fenced_exc = True
        active.abort_fenced()
        raise
    finally:
        leases._ACTIVE_LEASE.reset(token_ctx)
        if not fenced_exc:
            if d.remote and not factory.interrupted(run_id):
                try:
                    device_turns.cleanup(leased, device_turns.folder(leased), stop=True, discard=True, base=True)
                except (OSError, subprocess.SubprocessError, RuntimeError) as e:
                    log(f"remote turn cleanup: {e}")
            if not active.closed:
                if active.lost:
                    active.abort_fenced()
                    raise leases.LeaseFenced("lease_fenced", lease_id=active.id, epoch=active.epoch, run_id=run_id)
                active.close("")


@contextmanager
def proxmox_clone(d: Device, dev_ver: str, run_id: str, log, hold=lambda: ""):
    active = leases.claim(d.name, dev_ver, run_id=run_id)
    token_ctx = leases._ACTIVE_LEASE.set(active)
    clone: proxmox.Clone | None = None
    fenced_exc = False
    try:
        for attempt in range(1, VMID_RETRY_MAX + 1):
            try:
                vmid = int(proxmox.pve(d, "pvesh get /cluster/nextid").strip())
            except leases.LeaseFenced:
                fenced_exc = True
                active.abort_fenced()
                raise
            except BaseException:
                if not active.closed:
                    active.close("")
                raise
            clone = proxmox.Clone(d, vm_name(run_id), vmid)
            log(f"cloning template {d.template} to {clone} on {d.name}")
            active.update(vm_name=clone.name, vmid=clone.vmid)
            try:
                with record.timed(run_id, "clone", device=d.name, vmid=clone.vmid, template=d.template):
                    proxmox.pve(d, f"qm clone {d.template} {clone.vmid} --name {clone.name} --full 0")
                break
            except BaseException as e:
                if is_vmid_collision(e, clone.vmid):
                    with ignoring_stops():
                        clone.delete(log)
                    active.close("collided")
                    if attempt >= VMID_RETRY_MAX:
                        raise DeviceError(
                            f"vmid collision on {d.name} after {VMID_RETRY_MAX} tries: {e}"
                        ) from e
                    jitter = 0.5 + secrets.randbelow(1501) / 1000.0
                    time.sleep(jitter)
                    active = leases.claim(d.name, dev_ver, run_id=run_id)
                    leases._ACTIVE_LEASE.set(active)
                    continue
                _close_for_create_error(clone, active, e, log, fenced=True)
                if isinstance(e, leases.LeaseFenced):
                    fenced_exc = True
                raise

        assert clone is not None
        try:
            with record.timed(run_id, "boot", device=d.name, vmid=clone.vmid) as booted:
                proxmox.pve(d, f"qm start {clone.vmid}")
                boot = proxmox.BootingClone(d, clone.vmid)
                booted["address"] = address = _call_clone_address(boot)
            log(f"{clone} is up at {address}")
            with tempfile.TemporaryDirectory(prefix="arete-clone-") as trust_dir:
                vm = _call_trusted_clone(boot, address, Path(trust_dir) / "known_hosts")
                fp = _host_key_fingerprint(vm.known_hosts)
                active.update(ready=util.iso(), address=address, host_key_fingerprint=fp)
                log(f"{clone} answers ssh with a host key its guest agent reported")
                yield vm
        except leases.LeaseFenced:
            fenced_exc = True
            active.abort_fenced()
            raise
        finally:
            if not fenced_exc and not active.closed:
                if active.lost:
                    active.abort_fenced()
                    raise leases.LeaseFenced("lease_fenced", lease_id=active.id, epoch=active.epoch, run_id=run_id)
                if d.keep:
                    log(f"keeping {clone} up until {clone.delete_command()} deletes it")
                    active.close("kept", kept_reason="device keep")
                elif reason := hold():
                    log(f"keeping {clone} up because {reason}; {clone.delete_command()} deletes it")
                    active.close("kept", kept_reason=reason)
                else:
                    _delete_and_close(clone, active, log)
    finally:
        leases._ACTIVE_LEASE.reset(token_ctx)
        if not active.closed:
            if fenced_exc:
                active.abort_fenced()
            else:
                active.close("")


@contextmanager
def fresh_sandbox(d: Device, dev_ver: str, run_id: str, log, hold=lambda: ""):
    active = leases.claim(d.name, dev_ver, run_id=run_id)
    token_ctx = leases._ACTIVE_LEASE.set(active)
    sandbox = exedev.Sandbox(d, vm_name(run_id))
    started = time.monotonic()
    fenced_exc = False
    try:
        with record.timed(run_id, "create", device=sandbox.device.name, sandbox=sandbox.name):
            vm = create_sandbox(sandbox, active, log)
        try:
            with record.timed(run_id, "boot", device=sandbox.device.name, sandbox=sandbox.name):
                exedev.boot_sandbox(sandbox, vm)
            active.update(ready=util.iso(), address=vm.host)
            log(f"{sandbox} is up at {vm.host} in {time.monotonic() - started:.1f} s")
            yield vm
        except leases.LeaseFenced:
            fenced_exc = True
            active.abort_fenced()
            raise
        finally:
            if not fenced_exc and not active.closed:
                if active.lost:
                    active.abort_fenced()
                    raise leases.LeaseFenced("lease_fenced", lease_id=active.id, epoch=active.epoch, run_id=run_id)
                if d.keep:
                    log(f"keeping {sandbox} up until {sandbox.delete_command()} deletes it")
                    active.close("kept", kept_reason="device keep")
                elif reason := hold():
                    log(f"keeping {sandbox} up because {reason}; {sandbox.delete_command()} deletes it")
                    active.close("kept", kept_reason=reason)
                else:
                    _delete_and_close(sandbox, active, log)
    except leases.LeaseFenced:
        fenced_exc = True
        active.abort_fenced()
        raise
    finally:
        leases._ACTIVE_LEASE.reset(token_ctx)
        if not active.closed:
            if fenced_exc:
                active.abort_fenced()
            else:
                active.close("")


def create_sandbox(sandbox: exedev.Sandbox, active: leases.ActiveLease, log) -> Device:
    d, name = sandbox.device, sandbox.name
    egress = [f"--egress={d.egress}"] if d.egress else []
    fenced = bool(active.run_id)
    active.update(vm_name=name)
    try:
        if d.template:
            log(f"copying {d.template} to {sandbox} on exe.dev")
            created = exedev.exe(d, "cp", d.template, name, "--sandbox", *egress)
        else:
            log(f"creating {sandbox} on exe.dev")
            created = exedev.exe(d, "new", f"--name={name}", "--sandbox", "--no-email", f"--tag={exedev.EXE_TAG}", *egress)
    except BaseException as e:
        _close_for_create_error(sandbox, active, e, log, fenced=fenced)
        raise
    return replace(d, name=f"{d.name} sandbox", kind="ssh", host=created.get("ssh_dest") or f"{name}{ssh.EXE_VM_DOMAIN}",
                   port=None)


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


def _delete_lease_vm(row: dict, vm: proxmox.Clone | exedev.Sandbox, log, *, give_up_before: str | None = None) -> str:
    if vm.device.keep:
        with foundation.tx(fenced=False) as c:
            updated = device_store.update_lease_fields(c, int(row["id"]), {"outcome": "kept", "kept_reason": "device keep"})
            if updated is not None:
                leases.emit_changed(c, updated)
        return ""
    with ignoring_stops():
        not_deleted = vm.delete(log)
    now_iso = util.iso()
    if not not_deleted:
        with foundation.tx(fenced=False) as c:
            updated = device_store.update_lease_fields(
                c,
                int(row["id"]),
                {"outcome": "deleted", "last_sweep": now_iso, "last_sweep_error": ""},
            )
            if updated is not None:
                leases.emit_changed(c, updated)
        return ""
    attempts = int(row.get("sweep_attempts") or 0) + 1
    failing_since = row.get("failing_since") or now_iso
    giving_up = bool(give_up_before is not None and row["created"] < give_up_before)
    with foundation.tx(fenced=False) as c:
        fields: dict[str, object] = {
            "outcome": "delete_failed",
            "sweep_attempts": attempts,
            "failing_since": failing_since,
            "last_sweep": now_iso,
            "last_sweep_error": not_deleted,
        }
        if giving_up:
            fields["sweep_gave_up"] = 1
        updated = device_store.update_lease_fields(c, int(row["id"]), fields)
        if updated is not None:
            leases.emit_changed(c, updated)
        if giving_up and inbox_store.available(c):
            inbox_store.raise_item(
                c,
                kind="alert",
                subject=f"lease:{row['id']}",
                priority=2,
                title=f"Could not delete {vm} on {vm.device.name}",
                detail={
                    "lease_id": int(row["id"]),
                    "device": vm.device.name,
                    "vm": str(vm),
                    "vm_name": row["vm_name"],
                    "vmid": row["vmid"],
                    "delete_command": vm.delete_command(),
                    "error": not_deleted,
                },
                verbs=["continue"],
                run_id=row["run_id"] or "",
                line=vm.device.name,
            )
    if giving_up:
        log(f"{not_deleted}; arete serve stops trying after {SWEEP_GIVES_UP_DAYS} days, and "
            f"{vm.delete_command()} deletes it if it is still there")
    else:
        log(f"{not_deleted}; arete serve tries again, and {vm.delete_command()} deletes it now")
    return not_deleted


def release(run_id: str, log=lambda text: None) -> None:
    for row in device_store.leases_of_run_for_release(run_id):
        if row["released"] is None:
            now_iso = util.iso()
            with foundation.tx(fenced=False) as c:
                live = c.execute("SELECT * FROM leases WHERE id = ? AND released IS NULL", (row["id"],)).fetchone()
                if live is not None:
                    c.execute(
                        "UPDATE leases SET epoch = epoch + 1, released = ?, token_hash = '', version = version + 1 "
                        "WHERE id = ?",
                        (now_iso, row["id"]),
                    )
                    device_store.kill_lease_tokens(c, int(row["id"]), now_iso)
                    updated = dict(c.execute("SELECT * FROM leases WHERE id = ?", (row["id"],)).fetchone())
                    leases.emit_changed(c, updated)
                    row = updated
        if row["vm_name"]:
            _delete_lease_vm(row, listed_vm(row), log)


def sweep(log_for: Callable[[str | None], Callable[[str], None]]) -> None:
    from .store import factory

    now = util.now()
    lapsed_before = util.iso(now - timedelta(minutes=leases.LAPSE_GRACE_MINUTES))
    unanswered_before = util.iso(now - timedelta(minutes=CREATE_MAY_FINISH_MINUTES))
    give_up_before = util.iso(now - timedelta(days=SWEEP_GIVES_UP_DAYS))
    for row in device_store.sweepable_leases(lapsed_before, unanswered_before):
        if row["run_id"] and factory.retained(row["run_id"]):
            continue
        vm = listed_vm(row)
        log = log_for(row["run_id"])
        _delete_lease_vm(row, vm, log, give_up_before=give_up_before if row["outcome"] == "delete_failed" else None)


def probe_waiting_devices(say: Callable[[str], None] = lambda text: None) -> None:
    for name in device_store.open_device_wait_blockers():
        try:
            probe(name)
        except Exception as e:
            say(f"probing waiting device {name} failed: {type(e).__name__}: {util.first_line(str(e), 300)}")


def left_up(run_id: str) -> list[str]:
    vms = [listed_vm(row) for row in device_store.delete_failed_leases_of_run(run_id)]
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
    d = device_at_version(row["device"], row.get("device_version") or "")
    vm_label = row.get("vm_name") or row.get("name") or ""
    return proxmox.Clone(d, vm_label, row["vmid"]) if d.kind == "proxmox" else exedev.Sandbox(d, vm_label)


def known() -> list[Device]:
    try:
        return load()
    except DeviceError:
        return [_load_default_local()]


def views() -> list[dict]:
    known_probes = cached()
    return [{**d.view(), "probe": known_probes.get(d.name)} for d in load()]


def _format_tools(raw_tools: list) -> list[str]:
    out: list[str] = []
    for item in raw_tools:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            ver = str(item.get("version") or "").strip()
            if name:
                out.append(f"{name} {ver}".strip())
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _probe_projection(row: dict) -> tuple[dict, str | None, list[str], list[str]]:
    facts = row.get("facts") if isinstance(row.get("facts"), dict) else {}
    system = str(row.get("system") or facts.get("system") or "").strip()
    gpus = [str(g).strip() for g in (row.get("gpus") or facts.get("gpus") or ()) if str(g).strip()]
    raw_tools = row.get("tools") if row.get("tools") is not None else (facts.get("tools") or [])
    tools_list = _format_tools(list(raw_tools))
    runners = [t.split()[0] for t in tools_list if t.split()[0] in RUNNERS]
    spec_parts = [p for p in (system, *gpus) if p]
    spec = ", ".join(spec_parts) if spec_parts else None
    last_probe = {
        "at": row["at"],
        "ok": bool(row["ok"]),
        "seconds": float(row["seconds"]),
        "device_version": row["device_version"],
        "system": system,
        "home": str(row.get("home") or ""),
        "tools": tools_list,
        "gpus": gpus,
        "detail": str(row.get("detail") or ""),
    }
    return last_probe, spec, tools_list, runners


def device_view(
    d: Device,
    *,
    probe_row: dict | None = None,
    open_leases: list[str] | None = None,
    version: str | None = None,
) -> dict:
    if probe_row is None:
        status = "unprobed"
        last_probe = None
        spec = None
        tools_list = None
        runners: list[str] = []
    else:
        status = "ok" if probe_row["ok"] else "fault"
        last_probe, spec, tools_list, runners = _probe_projection(probe_row)
    return {
        "id": d.name,
        "subject": f"device:{d.name}",
        "version": version_of(d.name) if version is None else version,
        "kind": d.kind,
        "pool": None,
        "status": status,
        "last_probe": last_probe,
        "leases": list(open_leases or ()),
        "runners": runners,
        "spec": spec,
        "tools": tools_list,
        "host": d.host,
        "note": d.note,
    }


def device_views() -> list[dict]:
    probes_by_device = device_store.latest_device_probes()
    leases_by_device = device_store.open_lease_ids_by_device()
    return [
        device_view(
            d,
            probe_row=probes_by_device.get(d.name),
            open_leases=leases_by_device.get(d.name, []),
        )
        for d in load()
    ]


def device_get_view(name: str) -> dict | None:
    try:
        d = get(name)
    except DeviceError:
        return None
    probe_row = device_store.latest_device_probe(d.name)
    open_leases = [f"L-{row['id']}" for row in device_store.leases_list(device=d.name, only_open=True)]
    return device_view(d, probe_row=probe_row, open_leases=open_leases)


def leased_device(lease_row: dict | None) -> Device | None:
    if lease_row is None or lease_row.get("released"):
        return None
    try:
        d = device_at_version(lease_row["device"], lease_row.get("device_version") or "")
    except DeviceError:
        return None
    if not d.remote:
        return d
    address = str(lease_row.get("address") or "").strip()
    if d.kind in ("exe", "proxmox"):
        if not address:
            return None
        return replace(d, host=address, port=None)
    if address:
        return replace(d, host=address)
    return d


def takeover_command(device: Device | None, run: dict) -> str | None:
    if device is None or not device.remote or device.kind not in ("ssh", "exe", "proxmox") or not device.host:
        return None
    argv = ssh.ssh_argv(device)
    remote = ssh.remote_dir(device.dir or "~")
    command = f"cd {remote} && exec $SHELL"
    return shlex.join([*argv[:-1], "-t", argv[-1], command])
