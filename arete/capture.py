import hashlib
import json
import math
import os
import re
import shutil
import threading
from pathlib import Path

from . import __version__, outbound, paths, state, tracker, util

MB = 1024 * 1024
MOST_IMAGE_BYTES = 5 * MB
MOST_VIDEO_BYTES = 25 * MB
MOST_ITEMS = 6
MOST_DRAFT_BYTES = 40 * MB
MOST_VIDEO_SECONDS = 30
MOST_NAME = 120
SOURCES = ("screenshot", "recording", "attached")
CAPTURES_DIR = paths.STATE_DIR / "captures"
FILES = threading.Lock()
MP4_BRANDS = {b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"dash", b"mmp4"}
NOT_EVIDENCE = "Evidence must be a PNG, JPEG or WebP image or a WebM or MP4 video, and this file is none of them."

KINDS = ("idea", "adjustment", "bug")
LIMITS = {"text": 4000, "expected": 2000, "actual": 2000, "steps": 2000}
MOST_CONTEXT_BYTES = 8 * 1024
MOST_ID = 120
VIEWS = ("live", "runs", "sessions", "loops", "triage", "outcomes", "health", "settings", "usage")
ITEM_KINDS = ("run", "session", "loop", "ticket")
RUN_ID = re.compile(r"\w[\w.-]*")
RUN_FACTS = ("kind", "status", "started", "finished")
EMPTY_DRAFT = {"kind": "idea", "text": "", "expected": "", "actual": "", "steps": ""}


class Refused(ValueError):
    pass


class TooLarge(Refused):
    pass


class NotStaged(LookupError):
    pass


def view(connect=state.db) -> dict:
    limits = {**LIMITS, "context_bytes": MOST_CONTEXT_BYTES, "image_bytes": MOST_IMAGE_BYTES, "video_bytes": MOST_VIDEO_BYTES,
              "evidence_items": MOST_ITEMS, "evidence_bytes": MOST_DRAFT_BYTES, "video_seconds": MOST_VIDEO_SECONDS}
    return {"draft": state.capture_draft(connect), "limits": limits, "kinds": list(KINDS)}


def sniffed(data: bytes) -> tuple[str, str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg", "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "image/webp", "webp"
    if data.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in data[:64]:
        return "video", "video/webm", "webm"
    if data[4:8] == b"ftyp":
        size = int.from_bytes(data[:4], "big")
        head = data[8:min(max(size, 16), 256)]
        brands = {head[:4], *(head[i:i + 4] for i in range(8, len(head) - 3, 4))}
        if brands & MP4_BRANDS:
            return "video", "video/mp4", "mp4"
    raise Refused(NOT_EVIDENCE)


def megabytes(size: int) -> str:
    return f"{size / MB:.1f} MB"


def checked_seconds(raw: str) -> float | None:
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or not 0 <= value <= 86400:
        raise Refused("The length must be a number of seconds from 0 to 86,400.")
    return round(value, 1)


def stage(data: bytes, name: str, source: str, seconds: str) -> dict:
    source = source or "attached"
    if source not in SOURCES:
        raise Refused(f"The source must be one of {', '.join(SOURCES)}.")
    length = checked_seconds(seconds)
    kind, mime, extension = sniffed(data)
    most, label = (MOST_IMAGE_BYTES, "An image") if kind == "image" else (MOST_VIDEO_BYTES, "A video")
    if len(data) > most:
        raise TooLarge(f"{label} is limited to {most // MB} MB and this one has {megabytes(len(data))}.")
    digest = hashlib.sha256(data).hexdigest()
    row = {"name": tracker.safe_filename(name)[:MOST_NAME] if name.strip() else f"{source}.{extension}", "kind": kind,
           "type": mime, "bytes": len(data), "sha256": digest, "source": source,
           "seconds": length if kind == "video" else None}

    def admit(held: list[dict]) -> None:
        if len(held) >= MOST_ITEMS:
            raise Refused(f"A draft holds at most {MOST_ITEMS} items and this one has {len(held)}. Remove one first.")
        used = sum(r["bytes"] for r in held)
        if used + len(data) > MOST_DRAFT_BYTES:
            raise TooLarge(f"A draft holds at most {MOST_DRAFT_BYTES // MB} MB of evidence; it has {megabytes(used)} "
                           f"and this file adds {megabytes(len(data))}.")

    def keep(draft_id: int) -> None:
        folder = CAPTURES_DIR / str(draft_id)
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / f".{digest}.{os.getpid()}.part"
        temp.write_bytes(data)
        os.replace(temp, folder / digest)

    with FILES:
        staged, existing = state.capture_stage(row, admit, keep)
    return {**view(), "evidence": staged, "existing": existing}


def staged_file(evidence_id: int) -> tuple[Path, dict]:
    row = state.capture_evidence(evidence_id, state.reader)
    path = CAPTURES_DIR / str(row["capture"]) / row["sha256"] if row else None
    if not path or not path.is_file():
        raise NotStaged(f"No staged evidence has the id {evidence_id}.")
    return path, row


def drop_evidence(evidence_id: int) -> dict:
    with FILES:
        row = state.capture_drop_evidence(evidence_id)
        if row:
            (CAPTURES_DIR / str(row["capture"]) / row["sha256"]).unlink(missing_ok=True)
    return view()


def checked_fields(body: dict) -> dict:
    fields = {k: body[k] for k in ("kind", *LIMITS) if k in body}
    if not fields:
        raise Refused(f"Send at least one of kind, {', '.join(LIMITS)}.")
    if "kind" in fields and fields["kind"] not in KINDS:
        raise Refused(f"The kind must be one of {', '.join(KINDS)}.")
    for name, most in LIMITS.items():
        if name not in fields:
            continue
        if not isinstance(fields[name], str):
            raise Refused(f"The {name} must be text.")
        if len(fields[name]) > most:
            raise Refused(f"The {name} is limited to {most:,} characters and this one has {len(fields[name]):,}.")
    return fields


def save(body: dict) -> dict:
    fields = checked_fields(body)
    held = state.capture_draft(state.reader) or EMPTY_DRAFT
    if any(held[k] != v for k, v in fields.items()):
        state.capture_save(**fields)
    return view()


def safe(value) -> str:
    return outbound.screen(outbound.sanitize(str(value or "")))


def run_facts(run_id: str) -> dict:
    run = state.run_get(run_id, connect=state.reader)
    if not run:
        return {}
    rows = state.steps(run_id, connect=state.reader)
    facts = {key: run.get(key) for key in RUN_FACTS}
    facts["step"] = rows[-1]["step"] if rows else ""
    facts["error"] = next((r["error"] for r in reversed(rows) if r.get("error")), "")
    if not facts["error"] and run.get("status") in ("failed", "interrupted"):
        facts["error"] = run.get("summary")
    return {key: safe(value) for key, value in facts.items() if value}


def snapshot(view_name: str, detail, code: str) -> str:
    if view_name not in VIEWS:
        raise Refused(f"The view must be one of {', '.join(VIEWS)}.")
    if detail is not None and not isinstance(detail, dict):
        raise Refused("The open item must be an object with a kind and an id.")
    snap = {"captured": util.iso(), "app": {"version": __version__, "code": code}, "view": view_name}
    if detail:
        kind, item_id = detail.get("kind"), detail.get("id")
        if kind not in ITEM_KINDS:
            raise Refused(f"The open item's kind must be one of {', '.join(ITEM_KINDS)}.")
        if not isinstance(item_id, str) or not item_id or len(item_id) > MOST_ID:
            raise Refused(f"The open item's id must be text of 1 to {MOST_ID} characters.")
        snap["item"] = {"kind": kind, "id": safe(item_id)}
        facts = run_facts(item_id) if kind in ("run", "session") and RUN_ID.fullmatch(item_id) else {}
        if facts:
            snap["run"] = facts
    text = json.dumps(snap, indent=2, ensure_ascii=False)
    if len(text.encode()) > MOST_CONTEXT_BYTES:
        raise Refused("The app state is over 8 KB, so it was not added.")
    return text


def add_context(body: dict, code: str) -> dict:
    text = snapshot(str(body.get("view") or ""), body.get("detail"), code)
    state.capture_save(context=text)
    return view()


def drop_context() -> dict:
    draft = state.capture_draft(state.reader)
    if draft and draft["context"]:
        state.capture_save(context="")
    return view()


def discard() -> dict:
    with FILES:
        draft_id = state.capture_discard()
        if draft_id is not None:
            shutil.rmtree(CAPTURES_DIR / str(draft_id), ignore_errors=True)
    return view()
