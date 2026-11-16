from __future__ import annotations

import fcntl
import io
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config, paths, util

KEEP_NEWEST = 24
KEEP_DAYS = 14
RESTORE_GAP = 10_000
SNAPSHOT = re.compile(r"^arete-(\d{8}T\d{6})(\d{6})?Z-([a-z][a-z0-9-]*)\.db$")
REASON = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
ARCHIVE = re.compile(r"^config-(\d{4}-\d{2}-\d{2})\.tar\.gz$")
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


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


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
        conn = paths.connect_db(p, readonly=True)
        try:
            if check:
                found["quick_check"] = "; ".join(r[0] for r in conn.execute("PRAGMA quick_check")) or "no answer"
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
            found["counts"] = {t: conn.execute(f"SELECT count(*) FROM {quoted(t)}").fetchone()[0] for t in tables}
        finally:
            conn.close()
    except (sqlite3.Error, OSError, PermissionError) as e:
        found["quick_check"] = f"{type(e).__name__}: {util.first_line(str(e), 200)}"
    return found


def copy_database(target: Path) -> None:
    conn = paths.connect_db(paths.DB_FILE, readonly=True)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()


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
        conn = paths.connect_db(paths.DB_FILE, readonly=True)
        try:
            row = conn.execute("SELECT backup FROM schema_migrations ORDER BY n DESC LIMIT 1").fetchone()
            pinned = row[0] if row else ""
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
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
    return name == "verification-authority" or name == "pi.env" or name.endswith(".env")


def add_tree(tar: tarfile.TarFile, root: Path, arcname: str) -> None:
    if root.is_symlink() or not root.is_dir():
        return
    tar.add(root, arcname=arcname, recursive=False)
    for here, folders, files in os.walk(root, followlinks=False):
        folders[:] = sorted(d for d in folders if not forbidden_name(d) and not (Path(here) / d).is_symlink())
        for name in sorted(files):
            p = Path(here) / name
            if not forbidden_name(name) and not p.is_symlink() and p.is_file():
                tar.add(p, arcname=f"{arcname}/{p.relative_to(root)}", recursive=False)
        for d in folders:
            tar.add(Path(here) / d, arcname=f"{arcname}/{(Path(here) / d).relative_to(root)}", recursive=False)


def layers_bundle() -> bytes | None:
    layers = paths.CONFIG_DIR / "layers"
    if not (layers / ".git").exists() and not layers.is_symlink():
        return None
    if layers.is_symlink() or (layers / ".git").is_symlink() or not (layers / ".git").is_dir():
        raise RuntimeError("layers is linked or has no plain .git folder, so it is not bundled")
    listed = subprocess.run(["git", "log", "--all", "-m", "--no-renames", "--name-only", "--format=", "-z"],
                            check=False, cwd=layers, capture_output=True, timeout=300)
    if listed.returncode != 0:
        raise RuntimeError(f"listing layers history failed ({listed.returncode})")
    for path in listed.stdout.decode(errors="replace").split("\0"):
        path = path.strip("\n")
        if path and any(forbidden_name(part) for part in path.split("/")):
            raise RuntimeError(f"layers history holds {path}, which never leaves the machine; the bundle was not made")
    res =subprocess.run(["git", "bundle", "create", "-", "--all"], check=False, cwd=layers, capture_output=True, timeout=300)
    if res.returncode != 0 or not res.stdout:
        raise RuntimeError(f"git bundle of layers failed ({res.returncode}): "
                           f"{util.first_line(res.stderr.decode(errors='replace'), 200)}")
    return res.stdout


def build_archive(day: str) -> Path:
    folder = paths.backups_dir()
    final = folder / f"config-{day}.tar.gz"
    handle, name = tempfile.mkstemp(dir=folder, prefix=f".config-{day}-", suffix=".tar.gz.tmp")
    os.close(handle)
    draft = Path(name)
    try:
        with tarfile.open(draft, "w:gz") as tar:
            for name in ("config.toml", "loop-switches.json"):
                p = paths.CONFIG_DIR / name
                if p.is_file() and not p.is_symlink():
                    tar.add(p, arcname=name, recursive=False)
            add_tree(tar, paths.CONFIG_DIR / "loops", "loops")
            if (bundle := layers_bundle()) is not None:
                member = tarfile.TarInfo("layers.bundle")
                member.size, member.mtime, member.mode = len(bundle), int(util.now().timestamp()), 0o600
                tar.addfile(member, io.BytesIO(bundle))
        util.flush_file(draft)
        os.replace(draft, final)
        util.flush_folder(folder)
    except BaseException:
        draft.unlink(missing_ok=True)
        raise
    return final


def copied_today(day: str) -> bool:
    copied = read_last().get("copied_at", "")
    return bool(copied) and util.parse_iso(copied).astimezone(UTC).strftime("%Y-%m-%d") == day


def run_copy(snapshot_file: Path, copy_to: str, day: str) -> str:
    try:
        with folder_lock("copy", wait=False) as mine:
            if not mine or (copy_to and copied_today(day)):
                return ""
            archive = build_archive(day)
            if not copy_to:
                return ""
            with LOCK:
                IN_FLIGHT.update((snapshot_file, archive))
            res = util.sh(["rsync", "-t", str(snapshot_file), str(archive), copy_to], timeout=3600)
            fields = {"copy_exit": res.returncode, "copy_attempted_at": util.iso()}
            if res.returncode == 0:
                fields.update(copied_at=util.iso(), copied_file=snapshot_file.name)
            update_last(**fields)
        return "" if res.returncode == 0 else f"rsync exited {res.returncode}: {util.first_line(res.stderr or res.stdout, 200)}"
    except Exception as e:
        update_last(copy_exit=1, copy_attempted_at=util.iso(), copy_error=util.first_line(str(e), 300))
        return f"{type(e).__name__}: {util.first_line(str(e), 300)}"
    finally:
        with LOCK:
            IN_FLIGHT.difference_update((snapshot_file, paths.backups_dir() / f"config-{day}.tar.gz"))
            COPYING.discard(day)


def daily_copy(snapshot_file: Path, copy_to: str, submit=None):
    day = util.now().strftime("%Y-%m-%d")
    with LOCK:
        archived = (paths.backups_dir() / f"config-{day}.tar.gz").exists()
        if day in COPYING or (copy_to and copied_today(day)) or (not copy_to and archived):
            return None
        COPYING.add(day)
        IN_FLIGHT.add(snapshot_file)
    job = (lambda: run_copy(snapshot_file, copy_to, day))
    try:
        return submit(job) if submit else job()
    except BaseException:
        with LOCK:
            COPYING.discard(day)
            IN_FLIGHT.discard(snapshot_file)
        raise


def live_runs() -> list[str]:
    if not paths.DB_FILE.exists():
        return []
    try:
        conn = sqlite3.connect(paths.DB_FILE, timeout=30)
        try:
            rows = conn.execute("SELECT id, pid FROM runs WHERE status = 'running'").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows if util.pid_alive(r[1])]


def stop_hint() -> str:
    if paths.ON_MAC:
        return "stop it first: launchctl bootout gui/$(id -u)/<the arete serve job>, or quit the terminal running `arete serve`"
    return "stop it first: stop the `arete serve` process or its service"


def sequences(conn: sqlite3.Connection) -> dict[str, int]:
    try:
        return {name: seq for name, seq in conn.execute("SELECT name, seq FROM sqlite_sequence")}
    except sqlite3.OperationalError:
        return {}


def current_sequences() -> dict[str, int] | None:
    if not paths.DB_FILE.exists():
        return None
    try:
        conn = sqlite3.connect(paths.DB_FILE, timeout=30)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return sequences(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def raise_sequences(staged: Path, current: dict[str, int] | None) -> None:
    from . import dbstats, state

    timing = dbstats.Timing(staged, "write")
    with state.owning_transaction(lambda: sqlite3.connect(staged, timeout=30), timing=timing) as conn:
        held = sequences(conn)
        for name in set(held) | set(current or {}):
            wanted = max(held.get(name, 0), (current or {}).get(name, 0)) if current is not None else held[name] + RESTORE_GAP
            if name in held:
                conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (wanted, name))
            elif held or conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
                conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (name, wanted))


def restore(source: Path) -> str:
    from . import control

    if control.serve_beat().get("alive"):
        raise Refused(f"arete serve is running; {stop_hint()}")
    if busy := live_runs():
        raise Refused(f"{util.count(len(busy), 'run')} still running ({', '.join(busy[:3])}); wait for them to end "
                      f"or stop them, and {stop_hint()}")
    if not source.is_file():
        raise Refused(f"{source} is not a file")
    if (found := inspect(source))["quick_check"] != "ok":
        raise Refused(f"{source} failed its check: {found['quick_check']}")
    live = paths.DB_FILE
    staged = live.with_name(live.name + ".restoring")
    staged.unlink(missing_ok=True)
    try:
        shutil.copyfile(source, staged)
        if (found := inspect(staged))["quick_check"] != "ok":
            raise Refused(f"the staged copy failed its check: {found['quick_check']}")
        raise_sequences(staged, current_sequences())
        util.flush_file(staged)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return install_staged(staged)


def install_staged(staged: Path) -> str:
    live = paths.DB_FILE
    journal = live.with_name(live.name + "-journal")
    taken = util.now().strftime("%Y%m%dT%H%M%S%fZ")
    old = live.with_name(f"{live.name}.before-restore-{taken}")
    old_journal = live.with_name(f"{old.name}-journal")
    moved: list[tuple[Path, Path]] = []
    try:
        for here, there in ((live, old), (journal, old_journal)):
            if here.exists():
                os.rename(here, there)
                moved.append((there, here))
        os.rename(staged, live)
        util.flush_folder(live.parent)
    except BaseException:
        for there, here in reversed(moved):
            if not here.exists():
                os.rename(there, here)
        staged.unlink(missing_ok=True)
        raise
    return str(old) if moved else "no earlier file"


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
