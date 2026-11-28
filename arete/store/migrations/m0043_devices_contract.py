from __future__ import annotations

import json
from dataclasses import fields

PHASE = "contract"


def _legacy_vm(row: dict):
    from arete import exedev, proxmox
    from arete.ssh import Device

    saved = json.loads(row["device"]) if isinstance(row["device"], str) else row["device"]
    d = Device(**{f.name: saved[f.name] for f in fields(Device) if f.name in saved})
    vm = proxmox.Clone(d, row["name"], row["vmid"]) if d.kind == "proxmox" else exedev.Sandbox(d, row["name"])
    return d, vm


def _matches_device(saved, candidate) -> bool:
    if candidate.name != saved.name or candidate.kind != saved.kind:
        return False
    if candidate.host != saved.host:
        return False
    return candidate.kind != "proxmox" or candidate.template == saved.template


def validate_vms_rows(conn, available_devices: dict) -> list[tuple[dict, object]]:
    is_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vms'"
    ).fetchone()
    if not is_table:
        return []
    matched = []
    for raw in conn.execute("SELECT * FROM vms ORDER BY created, name"):
        row = dict(raw)
        saved_dev, vm = _legacy_vm(row)
        candidate = available_devices.get(saved_dev.name)
        if candidate is None or not _matches_device(saved_dev, candidate):
            raise ValueError(f"vms row {row['name']} matches no device file; delete it with {vm.delete_command()}")
        matched.append((row, candidate))
    return matched


def apply(conn) -> None:
    from arete import devices

    by_name = {d.name: d for d in devices.load()}
    matched = validate_vms_rows(conn, by_name)
    for row, candidate in matched:
        version = devices.version_of(candidate.name, conn=conn)
        failing = row.get("failing_since")
        conn.execute(
            "INSERT INTO leases "
            "(run_id, device, device_version, epoch, token_hash, created, renewed, expires, released, "
            " outcome, kept_reason, vm_name, vmid, ready, address, host_key_fingerprint, "
            " sweep_attempts, failing_since, last_sweep, last_sweep_error, sweep_gave_up, version) "
            "VALUES (?, ?, ?, 1, '', ?, '', ?, NULL, '', '', ?, ?, '', '', '', ?, ?, ?, '', 0, 1)",
            (
                row["run_id"],
                candidate.name,
                version,
                row["created"],
                row["created"],
                row["name"],
                row["vmid"],
                1 if failing else 0,
                failing,
                failing or "",
            ),
        )
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vms'").fetchone():
        conn.execute("DROP TABLE vms")
    conn.execute("DROP VIEW IF EXISTS vms")
    conn.execute(
        "CREATE VIEW vms AS "
        "SELECT vm_name AS name, run_id, device, vmid, created, failing_since "
        "FROM leases "
        "WHERE vm_name != '' AND (released IS NULL OR outcome = 'delete_failed')"
    )
    conn.execute(
        "UPDATE schema_meta SET value = CASE WHEN CAST(COALESCE(value, '0') AS INTEGER) < 43 "
        "THEN '43' ELSE value END WHERE key = 'oldest_writer'"
    )
