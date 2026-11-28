from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config, configrepo, paths, util
from .store import db as foundation
from .store import runs as run_store

KEEP_NEWEST = 24
KEEP_DAYS = 14
RESTORE_GAP = 10_000
SNAPSHOT = re.compile(r"^arete-(\d{8}T\d{6})(\d{6})?Z-([a-z][a-z0-9-]*)\.db$")
REASON = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
ARCHIVE = re.compile(r"^config-(\d{4}-\d{2}-\d{2})\.(?:bundle|tar\.gz)$")
SQLITE_MAGIC = b"SQLite format 3\x00"
LAST = "last.json"
LOCK = threading.RLock()
IN_FLIGHT: set[Path] = set()
COPYING: set[str] = set()


class Refused(Exception):
    pass


def stamp_of(p: Path) -> datetime | None:
    if not (found := SNAPSHOT.match(p.name)):
        return None
    return datetime.strptime(found.group(1) + (found.group(2) or "000000"), "%Y%m%dT%H%M%S%f").replace(tzinfo=UTC)


def snapshots(folder: Path | None = None) -> list[Path]:
    folder = folder or paths.backups_dir()
    try:
        found = [p for p in folder.iterdir() if stamp_of(p) and p.is_file()]
    except OSError:
        return []
    return sorted(found, key=lambda p: (stamp_of(p), p.name))


def newest_snapshot() -> Path | None:
    return (snapshots() or [None])[-1]


def read_last() -> dict:
    data = util.read_json(paths.backups_dir() / LAST, {})
    return data if isinstance(data, dict) else {}


@contextmanager
def folder_lock(name: str, wait: bool = True):
    folder = paths.backups_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f".{name}.lock", "a") as f:
        if wait:
            fcntl.flock(f, fcntl.LOCK_EX)
        yield wait or util.flock_now(f)


def update_last(**fields) -> None:
    with LOCK, folder_lock("last"):
        util.write_json(paths.backups_dir() / LAST, {**read_last(), **fields})


def metadata(conn) -> dict:
    return foundation.metadata(conn)


def sequences(conn: sqlite3.Connection) -> dict[str, int]:
    return foundation.sequences(conn)


def copy_database(target: Path) -> None:
    foundation.copy_database(target)


def apply_metadata(target, data):
    return foundation.apply_restore_metadata(target, data)


def inspect(p: Path, check: bool = True) -> dict:
    found = {"quick_check": "", "counts": {}}
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            head = f.read(100)
        if size == 0 or head[:16] != SQLITE_MAGIC:
            found["quick_check"] = "not a database" if size else "empty file"
            return found
        page_size = int.from_bytes(head[16:18], "big") or 65536
        if size < int.from_bytes(head[28:32], "big") * page_size:
            found["quick_check"] = "file is shorter than its header says"
            return found
        found.update(foundation.inspect_database(p, check))
    except (sqlite3.Error, OSError, PermissionError) as e:
        found["quick_check"] = f"{type(e).__name__}: {util.first_line(str(e), 200)}"
    return found


def snapshot(reason: str) -> Path | None:
    if paths.ISOLATED or not paths.DB_FILE.exists():
        return None
    if not REASON.match(reason):
        raise ValueError(f"backup reason {reason!r} is not a file name part")
    if paths.DB_FILE.stat().st_size == 0:
        raise RuntimeError(f"{paths.DB_FILE} is empty, so it is not a snapshot source")
    folder = paths.backups_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with LOCK:
        taken = util.now()
        while True:
            name = f"arete-{taken.strftime('%Y%m%dT%H%M%S%f')}Z-{reason}.db"
            if not (folder / name).exists():
                break
            taken += timedelta(microseconds=1)
        final, draft = folder / name, folder / f".{name}.tmp"
        try:
            copy_database(draft)
            util.flush_file(draft)
            os.rename(draft, final)
            util.flush_folder(folder)
        except BaseException:
            draft.unlink(missing_ok=True)
            raise
        found = inspect(final)
        ok = found["quick_check"] == "ok"
        update_last(file=final.name, time=util.iso(taken), bytes=final.stat().st_size,
                    quick_check=found["quick_check"], counts=found["counts"])
        if not ok:
            final.rename(final.with_name(final.name + ".bad"))
            raise RuntimeError(f"snapshot {final.name} failed its check: {found['quick_check']}")
        retain(folder)
    return final


def retain(folder: Path) -> None:
    every = snapshots(folder)
    pinned = ""
    if paths.DB_FILE.exists():
        pinned = foundation.latest_migration_backup()
    keep = set(every[-KEEP_NEWEST:])
    first_day = (util.now() - timedelta(days=KEEP_DAYS - 1)).date()
    newest_of_day = {stamp_of(p).date(): p for p in every}
    keep |= {p for day, p in newest_of_day.items() if day >= first_day}
    old_archives = [p for p in folder.iterdir() if (m := ARCHIVE.match(p.name))
                    and datetime.strptime(m.group(1), "%Y-%m-%d").date() < first_day]
    for p in [p for p in every if p not in keep] + old_archives:
        if p not in IN_FLIGHT and p.name != pinned:
            p.unlink(missing_ok=True)


def forbidden_name(name: str) -> bool:
    name = name.lower()
    return (name in ("verification-authority", ".env", "pi.env", "hooks", "secrets")
            or name.startswith(".env.") or name.endswith((".env", ".secret", ".secrets")))


def _check_config_history(root: Path) -> None:
    from . import environment

    if root.is_symlink() or (root / ".git").is_symlink() or not (root / ".git").is_dir():
        raise RuntimeError("config folder is linked or has no plain .git folder, so it is not bundled")
    env = environment.build(())
    listed = subprocess.run(["git", "log", "--all", "--root", "-m", "--no-renames", "--raw", "--format=", "-z"],
                            check=False, cwd=root, capture_output=True, timeout=300, env=env)
    if listed.returncode != 0:
        raise RuntimeError(f"listing config history failed ({listed.returncode})")
    records = iter(listed.stdout.decode(errors="replace").split("\0"))
    for header in records:
        header = header.lstrip("\n")
        if not header:
            continue
        modes = re.fullmatch(r":([0-7]{6}) ([0-7]{6}) [0-9a-f]+ [0-9a-f]+ [A-Z][0-9]*", header)
        if modes is None:
            raise RuntimeError("config history has an unrecognized raw header; the bundle was not made")
        path = next(records, None)
        if not path:
            raise RuntimeError("config history has a missing pathname; the bundle was not made")
        if "120000" in modes.groups():
            raise RuntimeError("config history holds a symlink; the bundle was not made")
        if any(forbidden_name(part) for part in path.split("/")):
            raise RuntimeError(f"config history holds {path}, which never leaves the machine; the bundle was not made")


def build_bundle(day: str) -> tuple[Path, str]:
    from . import environment

    root = paths.CONFIG_DIR.resolve()
    configrepo.ensure_repo()
    _check_config_history(root)
    folder = paths.backups_dir()
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"config-{day}.bundle"
    handle, name = tempfile.mkstemp(dir=folder, prefix=f".config-{day}-", suffix=".bundle.tmp")
    os.close(handle)
    draft = Path(name)
    env = environment.build(())
    try:
        res = subprocess.run(["git", "bundle", "create", str(draft), "--all"], check=False, cwd=root,
                             capture_output=True, timeout=300, env=env)
        if res.returncode != 0 or not draft.is_file() or draft.stat().st_size == 0:
            raise RuntimeError(f"git bundle create failed ({res.returncode}): "
                               f"{util.first_line(res.stderr.decode(errors='replace'), 200)}")
        verified = subprocess.run(["git", "bundle", "verify", str(draft)], check=False, cwd=root,
                                  capture_output=True, timeout=300, env=env)
        if verified.returncode != 0:
            raise RuntimeError(f"git bundle verify failed ({verified.returncode}): "
                               f"{util.first_line(verified.stderr.decode(errors='replace'), 200)}")
        util.flush_file(draft)
        os.replace(draft, final)
        util.flush_folder(folder)
    except BaseException:
        draft.unlink(missing_ok=True)
        raise
    live = configrepo.live_sha(root)
    return final, live


def copied_today(day: str) -> bool:
    copied = read_last().get("copied_at", "")
    return bool(copied) and util.parse_iso(copied).astimezone(UTC).strftime("%Y-%m-%d") == day


def run_copy(snapshot_file: Path, copy_to: str, day: str) -> str:
    try:
        with folder_lock("copy", wait=False) as mine:
            if not mine or (copy_to and copied_today(day)):
                return ""
            bundle, live = build_bundle(day)
            update_last(config_bundle=bundle.name, config_live_sha=live, live_sha=live)
            if not copy_to:
                return ""
            with LOCK:
                IN_FLIGHT.update((snapshot_file, bundle))
            res = util.sh(["rsync", "-t", str(snapshot_file), str(bundle), copy_to], timeout=3600)
            fields = {"copy_exit": res.returncode, "copy_attempted_at": util.iso(),
                      "config_bundle": bundle.name, "config_live_sha": live, "live_sha": live}
            if res.returncode == 0:
                fields.update(copied_at=util.iso(), copied_file=snapshot_file.name)
            update_last(**fields)
        return "" if res.returncode == 0 else f"rsync exited {res.returncode}: {util.first_line(res.stderr or res.stdout, 200)}"
    except Exception as e:
        update_last(copy_exit=1, copy_attempted_at=util.iso(), copy_error=util.first_line(str(e), 300))
        return f"{type(e).__name__}: {util.first_line(str(e), 300)}"
    finally:
        with LOCK:
            IN_FLIGHT.difference_update((snapshot_file, paths.backups_dir() / f"config-{day}.bundle"))
            COPYING.discard(day)


def daily_copy(snapshot_file: Path, copy_to: str, submit=None):
    day = util.now().strftime("%Y-%m-%d")
    with LOCK:
        archived = (paths.backups_dir() / f"config-{day}.bundle").exists()
        if day in COPYING or (copy_to and copied_today(day)) or (not copy_to and archived):
            return None
        COPYING.add(day)
        IN_FLIGHT.add(snapshot_file)

    def job():
        return run_copy(snapshot_file, copy_to, day)

    try:
        return submit(job) if submit else job()
    except BaseException:
        with LOCK:
            COPYING.discard(day)
            IN_FLIGHT.discard(snapshot_file)
        raise


SIDECARS = ("", "-journal", "-wal", "-shm")


@contextmanager
def private_database(source: Path):
    with tempfile.TemporaryDirectory(prefix="arete-probe-") as folder:
        target = Path(folder) / "arete.db"
        for suffix in SIDECARS:
            original = Path(str(source) + suffix)
            if original.exists():
                shutil.copyfile(original, Path(str(target) + suffix))
        conn = foundation.connect_db(target)
        try:
            foundation.check_readable(conn)
            yield conn
        finally:
            conn.close()


def probe_current() -> dict | None:
    try:
        with private_database(paths.DB_FILE) as conn:
            meta = metadata(conn)
            runs = run_store.running_runs(conn)
            return {"sequences": sequences(conn), "meta": meta, "runs": runs}
    except (sqlite3.Error, OSError):
        return None


def lock_holder(name):
    try:
        return (paths.LOCKS_DIR / f"{name}.lock").read_text().strip()
    except OSError:
        return "unknown"


def live_runs(current=None) -> list[str]:
    current = probe_current() if current is None else current
    busy = []
    for row in (current or {}).get("runs", []):
        held = row.get("lock") and util.lock_is_held(row["lock"])
        if held or util.pid_alive(row.get("pid")):
            busy.append(f"{row['id']} pid {lock_holder(row['lock']) if held else row['pid']}")
    for file in paths.LOCKS_DIR.glob("*.lock"):
        if file.stem not in ("serve", "migrate") and util.lock_is_held(file.stem):
            busy.append(f"{file.stem} pid {lock_holder(file.stem)}")
    return busy


def stop_hint() -> str:
    if paths.ON_MAC:
        return "stop it first: launchctl bootout gui/$(id -u)/<the arete serve job>, or quit the terminal running `arete serve`"
    return "stop it first: stop the `arete serve` process or its service"


def current_sequences() -> dict[str, int] | None:
    found = probe_current()
    return found["sequences"] if found is not None else None


def raise_sequences(staged: Path, current: dict[str, int] | None) -> None:
    foundation.raise_sequences(staged, current, RESTORE_GAP)


def restore_record() -> Path:
    return paths.backups_dir() / "restore-install.json"


def pending_restore() -> dict:
    file = restore_record()
    if not file.exists():
        return {}
    try:
        data = json.loads(file.read_text())
        live = paths.DB_FILE
        valid = (data["state"] in ("prepared", "installed", "rolling-back", "complete")
                 and data["archive"].startswith(live.name + ".before-restore-")
                 and Path(data["archive"]).name == data["archive"]
                 and Path(data["stage"]).name == data["stage"]
                 and data["stage"].startswith(live.name + ".restoring-")
                 and set(data["inventory"]) <= set(SIDECARS)
                 and set(data["originals"]) == set(data["inventory"])
                 and all(isinstance(v, list) and len(v) == 2 and all(isinstance(n, int) for n in v)
                         for v in data["originals"].values())
                 and isinstance(data["epoch"], int) and data["epoch"] > 0
                 and isinstance(data["sequences"], dict)
                 and all(isinstance(k, str) and isinstance(v, int) and v >= 0 for k, v in data["sequences"].items())
                 and isinstance(data["identity"], dict)
                 and all(k.startswith("instance_") and isinstance(v, str) for k, v in data["identity"].items())
                 and isinstance(data["inode"], int))
        if not valid:
            raise ValueError("invalid installation fields")
        return {} if data["state"] == "complete" else data
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise Refused(f"{file}: corrupt restore recovery metadata; preserve database, stage and archives: {e}") from e


def save_restore(data, **fields):
    data.update(fields)
    util.write_json(restore_record(), data)


def receipt_epoch():
    file = paths.backups_dir() / "restore-receipt.json"
    if not file.exists():
        return 0
    try:
        epoch = int(json.loads(file.read_text())["epoch"])
        if epoch < 0:
            raise ValueError("negative generation")
        return epoch
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as e:
        raise Refused(f"{file}: unreadable generation receipt; preserve it for recovery") from e


def finish_restore():
    data = pending_restore()
    if not data:
        return
    if data["state"] != "installed":
        raise Refused("interrupted restore requires arete serve before migration")
    if not paths.DB_FILE.exists() or paths.DB_FILE.stat().st_ino != data["inode"]:
        raise Refused("installed restore database was replaced; preserve files for recovery")
    validate_archives(data)
    if not apply_metadata(paths.DB_FILE, data):
        return
    util.flush_file(paths.DB_FILE)
    if data["identity"]:
        util.write_json(paths.backups_dir() / "instance.json", data["identity"])
    util.write_json(paths.backups_dir() / "restore-receipt.json", {"epoch": data["epoch"]})
    save_restore(data, state="complete")


def restore(source: Path) -> str:
    from . import control

    with util.lock("serve") as mine:
        if not mine:
            raise Refused(f"arete serve pid {lock_holder('serve')} holds the startup lock; {stop_hint()}")
        with util.lock("migrate", wait=True):
            recover_restore()
            if pending_restore():
                raise Refused("previous restore needs normal serve startup completion")
            beat = control.database_beat()
            if util.pid_alive(beat.get("pid")):
                raise Refused(f"arete serve pid {beat.get('pid')} is running; {stop_hint()}")
            current = probe_current()
            if busy := live_runs(current):
                raise Refused(f"runs still running ({', '.join(busy)}); {stop_hint()}")
            return prepare_restore(source, current)


def prepare_restore(source, current):
    live = paths.DB_FILE
    token = uuid.uuid4().hex
    staged = live.with_name(live.name + ".restoring-" + token)
    prepared = False
    try:
        if not source.is_file():
            raise Refused(f"{source} is not a file")
        with private_database(source) as conn:
            if foundation.quick_check(conn) != ["ok"]:
                raise Refused(f"{source} failed its check")
            snap_meta, held = metadata(conn), sequences(conn)
            foundation.vacuum_into(conn, staged)
        previous = (current or {}).get("sequences", {})
        floors = {name: max(held.get(name, 0), previous.get(name, 0)) if current is not None
                  else held[name] + RESTORE_GAP for name in set(held) | set(previous)}
        identity = {k: v for k, v in (current or {}).get("meta", {}).items() if k.startswith("instance_")}
        if not identity:
            from .store.migrations.m0001_foundation_ledger import identity as new_identity

            try:
                identity = new_identity()
            except ValueError as e:
                raise Refused(str(e)) from e
        epoch = 1 + max(int(snap_meta.get("restore_epoch", 0)),
                        int((current or {}).get("meta", {}).get("restore_epoch", 0)), receipt_epoch())
        data = {"state": "prepared", "stage": staged.name, "inode": staged.stat().st_ino,
                "archive": live.name + ".before-restore-" + token,
                "inventory": [s for s in SIDECARS if Path(str(live) + s).exists()],
                "sequences": floors, "identity": identity, "epoch": epoch}
        data["originals"] = {s: file_identity(Path(str(live) + s)) for s in data["inventory"]}
        apply_metadata(staged, data)
        if inspect(staged)["quick_check"] != "ok":
            raise Refused("prepared restore failed its check")
        util.flush_file(staged)
        util.write_json(paths.backups_dir() / "instance.json", identity)
        save_restore(data)
        prepared = True
        return install_staged(staged)
    finally:
        if not prepared or not staged.exists():
            for suffix in SIDECARS:
                Path(str(staged) + suffix).unlink(missing_ok=True)


def install_staged(staged: Path) -> str:
    live, data = paths.DB_FILE, pending_restore()
    old = live.with_name(data["archive"])
    for suffix in data["inventory"]:
        here, there = Path(str(live) + suffix), Path(str(old) + suffix)
        if there.exists():
            if file_identity(there) != data["originals"][suffix] or here.exists():
                raise Refused(f"ambiguous restore archive {there}; preserve files for recovery")
        elif not here.exists() or file_identity(here) != data["originals"][suffix]:
            raise Refused(f"restore original {here} was replaced; preserve files for recovery")
    try:
        for suffix in data["inventory"]:
            here, there = Path(str(live) + suffix), Path(str(old) + suffix)
            if not there.exists():
                os.rename(here, there)
                util.flush_folder(live.parent)
        if staged.exists():
            os.rename(staged, live)
        util.flush_file(live)
        util.flush_folder(live.parent)
        save_restore(data, state="installed")
        finish_restore()
    except BaseException:
        rollback_install(data)
        raise
    return str(old) if data["inventory"] else "no earlier file"


def file_identity(file):
    stat = file.stat()
    return [stat.st_dev, stat.st_ino]


def validate_archives(data):
    for suffix in data["inventory"]:
        archive = paths.DB_FILE.with_name(data["archive"] + suffix)
        if not archive.exists() or file_identity(archive) != data["originals"][suffix]:
            raise Refused(f"restore archive {archive} is missing or replaced; preserve files for recovery")


def rollback_install(data):
    live = paths.DB_FILE
    if data["state"] != "rolling-back":
        try:
            save_restore(data, state="rolling-back")
        except OSError:
            pass
    for suffix in data["inventory"]:
        archive, original = live.with_name(data["archive"] + suffix), Path(str(live) + suffix)
        held = archive if archive.exists() else original
        if not held.exists() or file_identity(held) != data["originals"][suffix]:
            raise Refused(f"restore original {held} was replaced; preserve files for recovery")
    installed = live.exists() and live.stat().st_ino == data["inode"]
    if installed:
        for suffix in SIDECARS:
            Path(str(live) + suffix).unlink(missing_ok=True)
    for suffix in reversed(data["inventory"]):
        archive = live.with_name(data["archive"] + suffix)
        if archive.exists():
            if file_identity(archive) != data["originals"][suffix]:
                raise Refused(f"restore archive {archive} was replaced; preserve files for recovery")
            os.rename(archive, Path(str(live) + suffix))
    for suffix in SIDECARS:
        Path(str(live.with_name(data["stage"])) + suffix).unlink(missing_ok=True)
    util.flush_folder(live.parent)
    restore_record().unlink(missing_ok=True)
    util.flush_folder(paths.backups_dir())


def recover_restore():
    data = pending_restore()
    if not data:
        return
    live = paths.DB_FILE
    if data["state"] == "rolling-back":
        rollback_install(data)
        return
    if data["state"] == "installed":
        if not live.exists() or live.stat().st_ino != data["inode"]:
            raise Refused("installed restore database was replaced; preserve files for recovery")
        validate_archives(data)
        return
    staged = live.with_name(data["stage"])
    if staged.exists() and staged.stat().st_ino == data["inode"]:
        install_staged(staged)
    elif live.exists() and live.stat().st_ino == data["inode"]:
        validate_archives(data)
        util.flush_file(live)
        util.flush_folder(live.parent)
        save_restore(data, state="installed")
    else:
        raise Refused("interrupted restore has no matching staged database; preserve files for recovery")


def newest_line() -> str:
    newest = newest_snapshot()
    return str(newest) if newest else "none"


def counts_table(left: dict, right: dict) -> str:
    names = sorted(set(left) | set(right))
    width = max([len(n) for n in names] + [5])
    rows = [f"{'table':<{width}}  {'snapshot':>9}  {'live':>9}"]
    rows += [f"{n:<{width}}  {left.get(n, '-'):>9}  {right.get(n, '-'):>9}" for n in names]
    return "\n".join(rows)


def command(check: bool, restore_from: str | None) -> int:
    if restore_from:
        try:
            old = restore(Path(restore_from).expanduser())
        except Refused as e:
            print(f"arete backup: {e}")
            return 1
        except (OSError, sqlite3.Error) as e:
            print(f"arete backup: restore failed: {type(e).__name__}: {util.first_line(str(e), 200)}")
            return 1
        print(f"restored {restore_from}; the replaced file is {old}")
        if pending_restore():
            print("normal arete serve startup must finish migration and restore metadata before the API opens")
        return 0
    if check:
        newest = newest_snapshot()
        if newest is None:
            print(f"arete backup: no snapshot in {paths.backups_dir()}")
            return 1
        found = inspect(newest)
        live = inspect(paths.DB_FILE, check=False) if paths.DB_FILE.exists() else {"counts": {}, "quick_check": "missing"}
        print(f"{newest}: quick_check {found['quick_check']}")
        print(counts_table(found["counts"], live["counts"]))
        if live["quick_check"] not in ("", "ok"):
            print(f"live file: {live['quick_check']}")
        return 0 if found["quick_check"] == "ok" else 1
    try:
        made = snapshot("hand")
    except Exception as e:
        print(f"arete backup: {type(e).__name__}: {util.first_line(str(e), 300)}")
        return 1
    if made is None:
        print("arete backup: no snapshot taken; there is no arete.db here or the state is a scratch copy")
        return 0
    print(f"{made}  {made.stat().st_size} bytes  quick_check {read_last().get('quick_check', '')}")
    try:
        if failure := daily_copy(made, config.load().backup.copy_to):
            print(f"arete backup: copy off the machine failed: {failure}")
    except Exception as e:
        print(f"arete backup: copy off the machine failed: {type(e).__name__}: {util.first_line(str(e), 300)}")
    return 0
