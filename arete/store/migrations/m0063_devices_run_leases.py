from __future__ import annotations

PHASE = "expand"
FOREIGN_KEYS_OFF = True


def apply(conn) -> None:
    vms_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'vms'"
    ).fetchone()
    vms_sql = str(vms_row[0]) if vms_row and vms_row[0] else ""
    if vms_sql:
        conn.execute("DROP VIEW vms")

    conn.execute("""
        CREATE TABLE leases_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            device TEXT NOT NULL,
            device_version TEXT NOT NULL DEFAULT '',
            epoch INTEGER NOT NULL DEFAULT 1,
            token_hash TEXT NOT NULL DEFAULT '',
            created TEXT NOT NULL,
            renewed TEXT NOT NULL DEFAULT '',
            expires TEXT NOT NULL,
            released TEXT,
            outcome TEXT NOT NULL DEFAULT '' CHECK (
                outcome IN (
                    '', 'deleted', 'delete_failed', 'kept', 'never_answered',
                    'expired', 'collided', 'released', 'revoked'
                )
            ),
            kept_reason TEXT NOT NULL DEFAULT '',
            vm_name TEXT NOT NULL DEFAULT '',
            vmid INTEGER,
            ready TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            host_key_fingerprint TEXT NOT NULL DEFAULT '',
            sweep_attempts INTEGER NOT NULL DEFAULT 0,
            failing_since TEXT,
            last_sweep TEXT NOT NULL DEFAULT '',
            last_sweep_error TEXT NOT NULL DEFAULT '',
            sweep_gave_up INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            engine_sha TEXT NOT NULL DEFAULT '',
            schema_version INTEGER,
            line_shape TEXT NOT NULL DEFAULT ''
        )
    """)
    cols = (
        "id, run_id, device, device_version, epoch, token_hash, created, renewed, expires, "
        "released, outcome, kept_reason, vm_name, vmid, ready, address, host_key_fingerprint, "
        "sweep_attempts, failing_since, last_sweep, last_sweep_error, sweep_gave_up, version"
    )
    conn.execute(f"INSERT INTO leases_new ({cols}) SELECT {cols} FROM leases")  # noqa: S608
    conn.execute("DROP TABLE leases")
    conn.execute("ALTER TABLE leases_new RENAME TO leases")
    conn.execute("CREATE INDEX leases_open ON leases (device, expires) WHERE released IS NULL")
    conn.execute("CREATE INDEX leases_run ON leases (run_id) WHERE run_id IS NOT NULL")
    if vms_sql:
        conn.execute(vms_sql)

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        sample = ", ".join(f"{r[0]}#{r[1]}->{r[2]}" for r in violations[:5])
        raise RuntimeError(f"foreign_key_check failed with {len(violations)} violation(s): {sample}")
