from __future__ import annotations

import hashlib
import os

from . import sources

MAX_READ = 1_000_000
TAIL_BYTES = 256


def digest(name: str, end: int) -> str | None:
    if end <= 0:
        return ""
    try:
        with open(name, "rb") as f:
            f.seek(max(0, end - TAIL_BYTES))
            return hashlib.sha1(f.read(min(end, TAIL_BYTES))).hexdigest()
    except OSError:
        return None


def drain(name: str, pending: list) -> tuple[list[str], list | None, int | None]:
    older = name + ".1"
    offset, tail = pending
    try:
        size = os.stat(older).st_size
        if size < offset or digest(older, offset) != tail:
            return [], None, None
        end = min(size, offset + MAX_READ)
        with open(older, "rb") as f:
            f.seek(offset)
            data = f.read(end - offset)
    except OSError:
        return [], None, None
    if end < size:
        data = data[:data.rfind(b"\n") + 1 or len(data)]
        end = offset + len(data)
    return data.decode("utf-8", "replace").splitlines(), ([end, digest(older, end)] if end < size else None), end


def stamp(name: str) -> list | None:
    try:
        info = os.stat(name + ".1")
    except OSError:
        return None
    return [info.st_ino, info.st_size, info.st_mtime_ns]


def whole_digest(name: str, end: int) -> str | None:
    try:
        with open(name, "rb") as f:
            h, left = hashlib.sha1(), end
            while left > 0 and (chunk := f.read(min(left, 1 << 20))):
                h.update(chunk)
                left -= len(chunk)
    except OSError:
        return None
    return h.hexdigest() if left == 0 else None


def still_holds(name: str, size: int, older: list, inode: int, same: int) -> bool:
    head = min(older[1], MAX_READ)
    return (inode == same and size >= older[1]
            and whole_digest(name, head) == whole_digest(name + ".1", head)
            and digest(name, older[1]) == digest(name + ".1", older[1]))


def untouched(info: os.stat_result, older: list) -> bool:
    return info.st_mtime_ns < older[2] or (info.st_mtime_ns == older[2] and info.st_size == older[1])


def parts(mark: list, now: list | None) -> tuple[list | None, list | None]:
    return (mark[3] if len(mark) > 3 else None), (mark[4] if len(mark) > 4 else now)


def gone(name: str, mark: list | None) -> tuple[list[str], list[str], list | None]:
    if mark is None:
        return [], [], mark
    now = stamp(name)
    pending, seen = parts(mark, now)
    if not pending and now:
        if mark[0] == 0:
            remainder = now != seen
        else:
            remainder = digest(name + ".1", mark[0]) == mark[2]
        if remainder:
            pending, mark = [mark[0], mark[2]], [0, mark[1], "", None]
    older, pending, _ = drain(name, pending) if pending else ([], None, None)
    return older, [], [mark[0], mark[1], mark[2], pending, now]


def fresh(name: str, mark: list | None, baseline: bool) -> tuple[list[str], list[str], list | None]:
    try:
        info = os.stat(name)
    except OSError:
        return gone(name, mark)
    size, inode = info.st_size, info.st_ino
    now = stamp(name)
    if baseline:
        return [], [], [size, inode, digest(name, size) or "", None, now]
    offset, pending, seen, noted = 0, None, now, now
    if mark is not None:
        offset = mark[0]
        pending, seen = parts(mark, now)
        if sources.log_start(mark[:2], size, inode) != offset or digest(name, offset) != mark[2]:
            pending, offset = ([offset, mark[2]] if offset > 0 else pending), 0
        elif now and now != seen and not pending and now[1] > offset and digest(name + ".1", offset) == mark[2]:
            copied = still_holds(name, size, now, inode, mark[1])
            if copied and untouched(info, now):
                noted = seen
            else:
                pending, offset = [offset, mark[2]], 0
        elif (now and seen and not pending and now[0] == seen[0] != inode and now[1] > seen[1]
              and not (offset > 0 and digest(name + ".1", offset) == mark[2])):
            pending = [seen[1], digest(name + ".1", seen[1]) or ""]
    end = min(size, offset + MAX_READ)
    if (got := sources.read_lines(name, offset, end)) is None:
        return [], [], mark
    rows, upto = got
    if upto is None:
        upto = end if end - offset >= MAX_READ else offset
    older, pending, drained = drain(name, pending) if pending else ([], None, None)
    if drained is not None and pending is None and now:
        noted = [now[0], drained, now[2]]
    return older, rows, [upto, inode, digest(name, upto) or "", pending, noted]


def undrained(name: str, mark: list) -> bool:
    if parts(mark, None)[0]:
        return True
    try:
        more = sources.read_lines(name, mark[0], min(os.stat(name).st_size, mark[0] + MAX_READ))
    except OSError:
        return False
    return bool(more and more[1] is not None)
