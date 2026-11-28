"""Private Triage evidence requests, validated private uploads, and automatic worker resumption."""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from . import (
    config,
    control,
    external_evidence,
    flows,
    jira,
    outbound,
    paths,
    pipeline,
    principals,
    refusals,
    ticket_media,
    tracker,
    util,
)
from .store import evidence as evidence_store
from .store import orchestration as orchestration_store
from .store import runs as run_store

PRIVATE_EVIDENCE_WAIT = "waiting in private Triage for missing or inaccessible ticket evidence"
ALLOWED_UPLOAD_MEDIA_TYPES = frozenset({
    "application/octet-stream",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
})
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class EvidenceError(refusals.Invalid, ValueError):
    code = "validation"


class UnreadableUpload(EvidenceError):
    pass


class StillInaccessible(EvidenceError):
    pass


class UploadTooLarge(refusals.TooLarge, EvidenceError):
    code = "body_too_large"


class EvidenceNotFound(refusals.NotFound, LookupError):
    code = "not_found"


class EvidenceAlreadyResolved(refusals.Conflict, RuntimeError):
    code = "already_resolved"


def private_dir() -> Path:
    return paths.STATE_DIR / "private-evidence"


def canonical_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    clean = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return ticket_media.sanitize_text(clean)


def media_ref_key(ref: ticket_media.MediaRef) -> str:
    if ref.origin == "jira" and ref.attachment_id:
        return f"jira:attachment:{ref.attachment_id}"
    if ref.url:
        return f"media:{ref.origin}:{canonical_url(ref.url)}"
    return f"media:{ref.origin}:{ref.name or ref.source}"


def external_ref_key(ref: external_evidence.ExternalRef) -> str:
    if ref.provider == "slack":
        return f"slack:{ref.channel}:{ref.thread_ts or ref.message_ts}"
    return f"dropbox:{canonical_url(ref.url)}"


def request_id(repo: str, ticket: str, ref_key: str) -> str:
    raw = f"{repo}\0{ticket}\0{ref_key}".encode()
    return "ev-" + hashlib.sha256(raw).hexdigest()[:16]


def _sanitize_detail(text: str) -> str:
    cleaned = _CONTROL_CHARS_RE.sub("", ticket_media.sanitize_text(text))
    return outbound.redact(cleaned).strip()[:500]


def safe_view(row: dict) -> dict:
    """Return an operator-facing representation without private filesystem paths or credentials."""
    return {
        "id": row["id"],
        "repo": row["repo"],
        "ticket": row["ticket"],
        "ticket_ref": row["ticket_ref"],
        "tracker": row["tracker"],
        "run_id": row["run_id"],
        "ref_key": row["ref_key"],
        "ref_kind": row["ref_kind"],
        "source": row["source"],
        "name": row["name"],
        "original_url": row["original_url"],
        "attachment_id": row["attachment_id"],
        "media_kind": row["media_kind"],
        "failure_status": row["failure_status"],
        "failure_detail": row["failure_detail"],
        "status": row["status"],
        "has_private_copy": bool(row.get("private_path") and row.get("status") == "uploaded"),
        "private_mime": row.get("private_mime", ""),
        "private_name": row.get("private_name", ""),
        "private_size": int(row.get("private_size") or 0),
        "last_error": row.get("last_error", ""),
        "resume_pending": bool(row.get("resume_pending")),
        "resumed_run_id": row.get("resumed_run_id", ""),
        "opened": row["opened"],
        "updated": row["updated"],
        "resolved_at": row.get("resolved_at", ""),
        "resolved_by": row.get("resolved_by", ""),
    }


def safe_private_file(rel_path: str) -> Path | None:
    """Resolve a stored relative path strictly confined under private_dir()."""
    if not rel_path or Path(rel_path).is_absolute():
        return None
    root = private_dir().resolve()
    try:
        candidate = (root / rel_path).resolve()
    except OSError:
        return None
    if candidate == root or not candidate.is_relative_to(root):
        return None
    if not candidate.is_file() or candidate.is_symlink():
        return None
    return candidate


def _write_private_bytes(ev_id: str, filename: str, data: bytes) -> str:
    root = private_dir()
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(root, 0o700)
    folder = root / ticket_media.safe_filename(ev_id, default="evidence")
    folder.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(folder, 0o700)
    safe_name = ticket_media.safe_filename(filename, default="copy")
    target = (folder / safe_name).resolve()
    resolved_root = root.resolve()
    if not target.is_relative_to(folder.resolve()) or target.parent != folder.resolve():
        raise UnreadableUpload("Refused private upload filename outside confined storage.")
    util.write_bytes_atomic(target, data)
    return target.relative_to(resolved_root).as_posix()


def _expects_media(row: dict, filename: str) -> bool:
    if row["ref_kind"] in ("jira_attachment", "media_url"):
        return True
    if row.get("media_kind") in ("image", "video"):
        return True
    orig_ext = Path(row.get("name") or "").suffix.lower()
    if orig_ext in ticket_media.MEDIA_EXTENSIONS:
        return True
    return Path(filename or "").suffix.lower() in ticket_media.MEDIA_EXTENSIONS


def _validate_upload(
    row: dict,
    data: bytes,
    filename: str,
    content_type: str,
    *,
    keyframe_extractor: Callable[[Path, Path], ticket_media.KeyframeExtraction] | None = None,
) -> tuple[bytes, str, str, str, dict]:
    """Validate uploaded private bytes.

    Returns (stored_bytes, stored_filename, mime_type, media_kind, metadata).
    Raises UploadTooLarge or UnreadableUpload on failure.
    """
    if not data:
        raise UnreadableUpload("Uploaded file is empty.")
    if len(data) > ticket_media.MAX_VIDEO_BYTES:
        raise UploadTooLarge(
            f"Uploaded file exceeds the {ticket_media.MAX_VIDEO_BYTES // (1024 * 1024)} MB maximum size."
        )
    if ticket_media._looks_like_html(data, content_type):
        raise UnreadableUpload("Uploaded file is an HTML page rather than readable evidence.")

    sniffed = ticket_media.sniff_media(data)
    if sniffed is not None:
        kind, mime_type, ext = sniffed
        cap = ticket_media.MAX_IMAGE_BYTES if kind == "image" else ticket_media.MAX_VIDEO_BYTES
        if len(data) > cap:
            raise UploadTooLarge(f"Uploaded {kind} exceeds the {cap // (1024 * 1024)} MB cap.")
        effective_name = filename or row.get("name") or f"private.{ext}"
        ref = ticket_media.MediaRef(
            source=row.get("source") or "body",
            origin="private_triage",
            kind=kind,
            attachment_id=row.get("attachment_id") or "",
            name=effective_name,
            mime_type=mime_type,
            size=len(data),
        )
        with tempfile.TemporaryDirectory(prefix="arete-priv-validate-") as tmp:
            outcome = ticket_media.materialize_media_bytes(
                ref,
                data,
                Path(tmp),
                ticket_ref=row.get("ticket_ref") or row["ticket"],
                prefix="check",
                content_type=mime_type,
                keyframe_extractor=keyframe_extractor,
            )
            if not outcome.ok:
                raise UnreadableUpload(_sanitize_detail(outcome.detail or "Uploaded media could not be read."))
            stem = ticket_media.safe_filename(Path(effective_name).stem, default=kind)
            meta: dict[str, object] = {"verified_kind": kind}
            if outcome.duration_seconds is not None:
                meta["duration_seconds"] = outcome.duration_seconds
            if outcome.keyframes:
                meta["keyframes"] = len(outcome.keyframes)
            return data, f"{stem}.{ext}", mime_type, kind, meta

    if _expects_media(row, filename):
        raise UnreadableUpload("Uploaded file is unreadable or not a recognized image/video format.")

    if len(data) > external_evidence.MAX_TEXT_BYTES:
        raise UploadTooLarge(
            f"Uploaded text file exceeds the {external_evidence.MAX_TEXT_BYTES // (1024 * 1024)} MB cap."
        )
    if b"\x00" in data[:4096] or b"\x00" in data:
        raise UnreadableUpload("Uploaded file is binary and not a recognized image, video, or UTF-8 text file.")
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnreadableUpload("Uploaded file is not valid UTF-8 text or supported media.") from exc
    cleaned = _CONTROL_CHARS_RE.sub("", ticket_media.sanitize_text(decoded))
    if not cleaned.strip():
        raise UnreadableUpload("Uploaded file contains no readable text.")
    safe_name = ticket_media.safe_filename(filename or row.get("name") or "evidence.txt", default="evidence.txt")
    if "." not in safe_name:
        safe_name = f"{safe_name}.txt"
    return cleaned.encode("utf-8"), safe_name, "text/plain; charset=utf-8", "text", {"verified_kind": "text"}


def _materialize_private_media(
    saved: dict,
    original_ref: ticket_media.MediaRef,
    dest: Path,
    *,
    ticket_ref: str,
    prefix: str,
    keyframe_extractor: Callable[[Path, Path], ticket_media.KeyframeExtraction] | None = None,
) -> ticket_media.MediaOutcome | None:
    path = safe_private_file(saved.get("private_path") or "")
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    outcome = ticket_media.materialize_media_bytes(
        original_ref,
        data,
        dest,
        ticket_ref=ticket_ref,
        prefix=prefix,
        content_type=saved.get("private_mime") or "",
        keyframe_extractor=keyframe_extractor,
    )
    if not outcome.ok:
        return None
    note = "supplied via private Triage copy for original reference"
    outcome.detail = f"{outcome.detail}; {note}" if outcome.detail else note
    return outcome


def _materialize_private_external(
    saved: dict,
    original_ref: external_evidence.ExternalRef,
    dest: Path,
    *,
    ticket_ref: str,
    index: int,
    keyframe_extractor: Callable[[Path, Path], ticket_media.KeyframeExtraction] | None = None,
) -> external_evidence.EvidenceOutcome | None:
    path = safe_private_file(saved.get("private_path") or "")
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    title = saved.get("name") or original_ref.filename or f"{original_ref.provider} evidence"
    if ticket_media.sniff_media(data) is not None:
        media_ref = ticket_media.MediaRef(
            source=original_ref.source,
            origin=original_ref.provider,
            kind=ticket_media.kind_from_hint(
                url=original_ref.url,
                name=saved.get("private_name") or title,
                mime_type=saved.get("private_mime") or "",
            ),
            url=original_ref.url,
            name=saved.get("private_name") or title,
            mime_type=saved.get("private_mime") or "",
            size=len(data),
        )
        media_outcome = ticket_media.materialize_media_bytes(
            media_ref,
            data,
            dest,
            ticket_ref=ticket_ref,
            prefix=f"private-ext-{index}",
            content_type=saved.get("private_mime") or "",
            keyframe_extractor=keyframe_extractor,
        )
        if not media_outcome.ok:
            return None
        return external_evidence.EvidenceOutcome(
            status="resolved",
            ref=original_ref,
            ticket_ref=ticket_ref,
            title=title,
            paths=media_outcome.inspectable_paths,
            media_outcome=media_outcome,
            detail="supplied via private Triage copy for original reference",
        )
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    cleaned = _CONTROL_CHARS_RE.sub("", ticket_media.sanitize_text(decoded)).strip()
    if not cleaned:
        return None
    dest.mkdir(parents=True, exist_ok=True)
    safe_name = ticket_media.safe_filename(saved.get("private_name") or title, default="evidence.txt")
    out_path = dest / f"private-ext-{index}-{safe_name}"
    out_path.write_text(cleaned + "\n", encoding="utf-8")
    excerpt = cleaned
    if len(excerpt) > external_evidence.MAX_TEXT_EXCERPT_CHARS:
        excerpt = excerpt[:external_evidence.MAX_TEXT_EXCERPT_CHARS].rstrip() + (
            "\n... (truncated; open saved file for full content)"
        )
    return external_evidence.EvidenceOutcome(
        status="resolved",
        ref=original_ref,
        ticket_ref=ticket_ref,
        title=title,
        excerpt=excerpt,
        paths=[out_path],
        detail="supplied via private Triage copy for original reference",
    )


def apply_private_copies_and_record_missing(
    repo_name: str,
    ticket: tracker.Ticket,
    images: tracker.SavedImages,
    evidence: external_evidence.ExternalEvidenceBundle,
    run_dir: Path,
    *,
    tracker_name: str = "",
    run_id: str = "",
    keyframe_extractor: Callable[[Path, Path], ticket_media.KeyframeExtraction] | None = None,
    record_missing: bool = True,
) -> list[dict]:
    """Resolve failed evidence using uploaded private copies, close restored requests, and record still-missing items."""
    ticket_id = str(ticket.id)
    ticket_ref = ticket.ref or ticket_id
    store_ready = bool(repo_name) and evidence_store.available()
    missing: list[dict] = []

    for idx, outcome in enumerate(list(images.outcomes), 1):
        ref_key = media_ref_key(outcome.ref)
        saved = evidence_store.get_by_ref(repo_name, ticket_id, ref_key) if store_ready else None
        if outcome.ok:
            if saved and saved["status"] == "open":
                evidence_store.record_access_restored(saved["id"], actor="worker", resume_pending=False)
            continue
        if not outcome.needs_private_help:
            continue
        if saved and saved["status"] == "uploaded" and saved.get("private_path"):
            replaced = _materialize_private_media(
                saved,
                outcome.ref,
                run_dir / "images",
                ticket_ref=ticket_ref,
                prefix=f"private-m{idx}",
                keyframe_extractor=keyframe_extractor,
            )
            if replaced is not None:
                images.outcomes[idx - 1] = replaced
                for path in replaced.inspectable_paths:
                    if path not in images.paths:
                        images.paths.append(path)
                continue

        ev_id = request_id(repo_name or "unknown", ticket_id, ref_key)
        ref_kind = "jira_attachment" if (outcome.ref.origin == "jira" and outcome.ref.attachment_id) else "media_url"
        name = outcome.ref.name or outcome.ref.attachment_id or canonical_url(outcome.ref.url) or f"attachment-{idx}"
        detail = _sanitize_detail(outcome.detail or f"{outcome.status} media attachment")
        if store_ready and record_missing:
            evidence_store.upsert_missing(
                ev_id,
                repo=repo_name,
                ticket=ticket_id,
                ticket_ref=ticket_ref,
                tracker=tracker_name or outcome.ref.origin,
                run_id=run_id,
                ref_key=ref_key,
                ref_kind=ref_kind,
                source=outcome.ref.source or "body",
                name=name,
                original_url=canonical_url(outcome.ref.url),
                attachment_id=outcome.ref.attachment_id,
                media_kind=outcome.media_kind or outcome.ref.kind,
                failure_status=outcome.status,
                failure_detail=detail,
                data={"ref": outcome.ref.to_dict()},
            )
        missing.append({
            "id": ev_id,
            "ref_key": ref_key,
            "ref_kind": ref_kind,
            "name": name,
            "status": outcome.status,
            "detail": detail,
        })

    for idx, outcome in enumerate(list(evidence.outcomes), 1):
        ref_key = external_ref_key(outcome.ref)
        saved = evidence_store.get_by_ref(repo_name, ticket_id, ref_key) if store_ready else None
        if outcome.ok:
            if saved and saved["status"] == "open":
                evidence_store.record_access_restored(saved["id"], actor="worker", resume_pending=False)
            continue
        if not outcome.needs_private_help:
            continue
        if saved and saved["status"] == "uploaded" and saved.get("private_path"):
            replaced_ext = _materialize_private_external(
                saved,
                outcome.ref,
                run_dir / "evidence",
                ticket_ref=ticket_ref,
                index=idx,
                keyframe_extractor=keyframe_extractor,
            )
            if replaced_ext is not None:
                evidence.outcomes[idx - 1] = replaced_ext
                for path in replaced_ext.paths:
                    if path not in evidence.paths:
                        evidence.paths.append(path)
                continue

        ev_id = request_id(repo_name or "unknown", ticket_id, ref_key)
        ref_kind = outcome.ref.provider
        name = outcome.title or outcome.ref.filename or canonical_url(outcome.ref.url) or f"{ref_kind}-{idx}"
        detail = _sanitize_detail(outcome.detail or f"{outcome.status} {ref_kind} evidence")
        media_kind = (outcome.media_outcome.media_kind if outcome.media_outcome else "") or (
            ticket_media.kind_from_hint(url=outcome.ref.url, name=name) if ref_kind == "dropbox" else "text"
        )
        if store_ready and record_missing:
            evidence_store.upsert_missing(
                ev_id,
                repo=repo_name,
                ticket=ticket_id,
                ticket_ref=ticket_ref,
                tracker=tracker_name,
                run_id=run_id,
                ref_key=ref_key,
                ref_kind=ref_kind,
                source=outcome.ref.source or "body",
                name=name,
                original_url=canonical_url(outcome.ref.url),
                attachment_id="",
                media_kind=media_kind,
                failure_status=outcome.status,
                failure_detail=detail,
                data={"ref": outcome.ref.to_dict()},
            )
        missing.append({
            "id": ev_id,
            "ref_key": ref_key,
            "ref_kind": ref_kind,
            "name": name,
            "status": outcome.status,
            "detail": detail,
        })

    if not missing and store_ready and run_id:
        evidence_store.mark_resumed(repo_name, ticket_id, run_id)
    return missing


def waiting_summary(missing: list[dict]) -> str:
    names = [str(m.get("name") or m.get("ref_key") or "evidence") for m in missing[:3]]
    joined = ", ".join(names)
    if len(missing) > 3:
        joined += f" (+{len(missing) - 3} more)"
    return f"Waiting in private Triage for missing evidence: {joined}"[:300]


def _verify_original_access(
    row: dict,
    *,
    tk: tracker.Tracker | None = None,
    keyframe_extractor: Callable[[Path, Path], ticket_media.KeyframeExtraction] | None = None,
    external_kwargs: dict | None = None,
) -> tuple[bool, str]:
    """Re-verify that the original ticket-linked reference is now accessible and readable."""
    ref_kind = row["ref_kind"]
    ticket_ref = row.get("ticket_ref") or row["ticket"]
    with tempfile.TemporaryDirectory(prefix="arete-priv-probe-") as tmp:
        dest = Path(tmp)
        if ref_kind == "jira_attachment":
            if tk is None:
                cfg = config.load()
                repo = cfg.repo(row["repo"])
                tk = tracker.for_ticket(cfg, repo, row["ticket"])
            if not isinstance(tk, tracker.JiraTracker):
                return False, "Jira tracker is not configured for this repo"
            try:
                data = tk.client.attachment(row["attachment_id"])
            except (jira.JiraError, OSError, ValueError) as exc:
                return False, _sanitize_detail(str(exc))
            ref = ticket_media.MediaRef(
                source=row.get("source") or "body",
                origin="jira",
                kind=row.get("media_kind") if row.get("media_kind") in ("image", "video") else "unknown",
                attachment_id=row.get("attachment_id") or "",
                name=row.get("name") or "",
            )
            outcome = ticket_media.materialize_media_bytes(
                ref,
                data,
                dest,
                ticket_ref=ticket_ref,
                prefix="probe",
                keyframe_extractor=keyframe_extractor,
            )
            return (True, "") if outcome.ok else (False, _sanitize_detail(outcome.detail or "attachment is still unreadable"))

        if ref_kind == "media_url":
            ref_dict = (row.get("data") or {}).get("ref") or {}
            origin = str(ref_dict.get("origin") or row.get("tracker") or "github")
            url = str(row.get("original_url") or ref_dict.get("url") or "")
            if not url:
                return False, "original media URL is unavailable"
            token = ticket_media.github_token() if origin == "github" else ""
            fetched = ticket_media.fetch_url(url, max_bytes=ticket_media.MAX_VIDEO_BYTES, token=token)
            if fetched.error_status:
                return False, _sanitize_detail(fetched.detail or f"media is still {fetched.error_status}")
            ref = ticket_media.MediaRef(
                source=row.get("source") or "body",
                origin=origin,
                kind=row.get("media_kind") if row.get("media_kind") in ("image", "video") else "unknown",
                url=url,
                name=fetched.filename or row.get("name") or "",
                mime_type=fetched.content_type,
                size=len(fetched.data),
            )
            outcome = ticket_media.materialize_media_bytes(
                ref,
                fetched.data,
                dest,
                ticket_ref=ticket_ref,
                prefix="probe",
                content_type=fetched.content_type,
                keyframe_extractor=keyframe_extractor,
            )
            return (True, "") if outcome.ok else (False, _sanitize_detail(outcome.detail or "media is still unreadable"))

        if ref_kind in ("dropbox", "slack"):
            ref_dict = (row.get("data") or {}).get("ref") or {}
            ext_ref = external_evidence.ExternalRef(
                provider=ref_kind,
                url=str(row.get("original_url") or ref_dict.get("url") or ""),
                source=row.get("source") or "body",
                ticket_ref=ticket_ref,
                channel=str(ref_dict.get("channel") or ""),
                message_ts=str(ref_dict.get("message_ts") or ""),
                thread_ts=str(ref_dict.get("thread_ts") or ""),
                filename=str(ref_dict.get("filename") or row.get("name") or ""),
            )
            kwargs = dict(external_kwargs or {})
            if ref_kind == "slack":
                tok = kwargs.get("slack_token_value")
                if tok is None:
                    tok = external_evidence.slack_token()
                out = external_evidence.resolve_slack_thread(
                    ext_ref, dest, ticket_ref=ticket_ref, token=tok, api_call=kwargs.get("slack_api"),
                )
            else:
                tok = kwargs.get("dropbox_token_value")
                if tok is None:
                    tok = external_evidence.dropbox_token()
                out = external_evidence.resolve_dropbox_file(
                    ext_ref,
                    dest,
                    ticket_ref=ticket_ref,
                    token=tok,
                    api_fetch=kwargs.get("dropbox_fetch"),
                    keyframe_extractor=keyframe_extractor or kwargs.get("keyframe_extractor"),
                    allow_public=bool(kwargs.get("allow_public_dropbox", False)),
                )
            return (True, "") if out.ok else (False, _sanitize_detail(out.detail or f"{ref_kind} is still {out.status}"))

    return False, f"unsupported evidence kind {ref_kind}"


def resume_ticket(repo_name: str, tid: str, run_id: str = "") -> dict:
    """Automatically resume affected ticket work once all of its private evidence requests are resolved."""
    from . import owner_asks, tasks

    tid = str(tid)
    if evidence_store.has_open(repo_name, tid):
        remaining = len(evidence_store.list_for_ticket(repo_name, tid, status="open"))
        return tasks.answer(
            tid,
            repo_name,
            "",
            "waiting",
            f"{util.count(remaining, 'more evidence item')} still wait for private upload or access",
        )
    with util.lock(owner_asks.STEER_LOCK, wait=True):
        if pending := tasks.pending_start(repo_name, tid):
            evidence_store.mark_resumed(repo_name, tid, pending.get("run") or "")
            return pending
        if running := tasks.live_run(repo_name, tid):
            evidence_store.mark_resumed(repo_name, tid, running["id"])
            return tasks.answer(tid, repo_name, running["id"], "running", "already running")
        try:
            cfg = config.load()
            repo = cfg.repo(repo_name)
        except (config.UnknownRepo, ValueError, OSError) as exc:
            return tasks.answer(tid, repo_name, "", "queued", f"resumption queued: {exc}")

        row = (run_store.run_get(run_id) if run_id else None) or run_store.newest_live_whole_run(repo_name, tid)
        has_prepared = bool(
            row
            and row["status"] == "needs-info"
            and (
                flows.prepared_data(row["id"])
                or ((orch := orchestration_store.find(row["id"])) and (orch.get("data") or {}).get("prepared"))
            )
        )
        try:
            if has_prepared and row is not None and pipeline.recorded_path(row["id"]) != "classic":
                result = tasks.continue_asked(cfg, repo, row)
            else:
                result = tasks.begin(cfg, repo, tid, force=True, poller_picks=repo.pipeline)
        except (control.Refused, control.Busy, control.NotFound, OSError, ValueError) as exc:
            return tasks.answer(tid, repo_name, "", "queued", f"resumption queued: {exc}")
        if result.get("state") in ("running", "starting"):
            evidence_store.mark_resumed(repo_name, tid, result.get("run") or "")
        return result


def upload_copy(
    ev_id: str,
    data: bytes,
    *,
    filename: str = "",
    content_type: str = "",
    actor: str = "",
    keyframe_extractor: Callable[[Path, Path], ticket_media.KeyframeExtraction] | None = None,
    auto_resume: bool = True,
    resumer: Callable[[str, str, str], dict] | None = None,
) -> dict:
    """Validate and store a private replacement copy in Triage, then resume affected work automatically."""
    row = evidence_store.get(ev_id)
    if row is None:
        raise EvidenceNotFound(f"No private evidence request {ev_id}.")
    if row["status"] != "open":
        raise EvidenceAlreadyResolved(f"Private evidence request {ev_id} is already {row['status']}.")

    try:
        stored_bytes, stored_name, mime_type, _kind, meta = _validate_upload(
            row,
            data,
            filename,
            content_type,
            keyframe_extractor=keyframe_extractor,
        )
    except EvidenceError as exc:
        evidence_store.record_failure(ev_id, _sanitize_detail(str(exc)))
        raise

    rel_path = _write_private_bytes(ev_id, stored_name, stored_bytes)
    sha256 = hashlib.sha256(stored_bytes).hexdigest()
    who = actor or principals.current()
    updated = evidence_store.record_upload(
        ev_id,
        private_path=rel_path,
        sha256=sha256,
        mime_type=mime_type,
        filename=stored_name,
        size=len(stored_bytes),
        actor=who,
        data=meta,
    )
    if updated is None:
        raise EvidenceAlreadyResolved(f"Private evidence request {ev_id} was already resolved.")

    resume_result: dict = {"state": "waiting", "why": "stored private copy"}
    if auto_resume:
        resume_fn = resumer or resume_ticket
        resume_result = resume_fn(updated["repo"], updated["ticket"], updated.get("run_id") or "")
    return {"evidence": safe_view(updated), "resumed": resume_result}


def confirm_access_restored(
    ev_id: str,
    *,
    actor: str = "",
    tk: tracker.Tracker | None = None,
    verifier: Callable[[dict], tuple[bool, str]] | None = None,
    keyframe_extractor: Callable[[Path, Path], ticket_media.KeyframeExtraction] | None = None,
    external_kwargs: dict | None = None,
    auto_resume: bool = True,
    resumer: Callable[[str, str, str], dict] | None = None,
) -> dict:
    """Re-verify access to the original ticket reference; resume if readable, keep open if still inaccessible/unreadable."""
    row = evidence_store.get(ev_id)
    if row is None:
        raise EvidenceNotFound(f"No private evidence request {ev_id}.")
    if row["status"] != "open":
        raise EvidenceAlreadyResolved(f"Private evidence request {ev_id} is already {row['status']}.")

    if verifier is not None:
        ok, detail = verifier(row)
    else:
        try:
            ok, detail = _verify_original_access(
                row,
                tk=tk,
                keyframe_extractor=keyframe_extractor,
                external_kwargs=external_kwargs,
            )
        except (config.UnknownRepo, ValueError, OSError) as exc:
            ok, detail = False, _sanitize_detail(str(exc))

    if not ok:
        reason = _sanitize_detail(detail or "Original evidence is still inaccessible or unreadable.")
        evidence_store.record_failure(ev_id, reason)
        raise StillInaccessible(reason)

    who = actor or principals.current()
    updated = evidence_store.record_access_restored(ev_id, actor=who, resume_pending=True)
    if updated is None:
        raise EvidenceAlreadyResolved(f"Private evidence request {ev_id} was already resolved.")

    resume_result: dict = {"state": "waiting", "why": "access restored"}
    if auto_resume:
        resume_fn = resumer or resume_ticket
        resume_result = resume_fn(updated["repo"], updated["ticket"], updated.get("run_id") or "")
    return {"evidence": safe_view(updated), "resumed": resume_result}


def dismiss(ev_id: str, *, actor: str = "") -> dict:
    row = evidence_store.get(ev_id)
    if row is None:
        raise EvidenceNotFound(f"No private evidence request {ev_id}.")
    if row["status"] != "open":
        raise EvidenceAlreadyResolved(f"Private evidence request {ev_id} is already {row['status']}.")
    updated = evidence_store.dismiss(ev_id, actor=actor or principals.current())
    if updated is None:
        raise EvidenceAlreadyResolved(f"Private evidence request {ev_id} was already resolved.")
    return {"evidence": safe_view(updated), "dismissed": True}


def probe_restored_for_ticket(
    repo_name: str,
    tk: tracker.Tracker,
    ticket: tracker.Ticket,
    *,
    dry_run: bool = False,
    keyframe_extractor: Callable[[Path, Path], ticket_media.KeyframeExtraction] | None = None,
    external_kwargs: dict | None = None,
) -> bool:
    """Re-check open private evidence items for a waiting ticket.

    Returns True iff all open items for this ticket are now accessible and readable.
    Items that remain inaccessible or unreadable stay open.
    """
    open_rows = evidence_store.list_for_ticket(repo_name, str(ticket.id), status="open")
    if not open_rows:
        return True
    all_restored = True
    for row in open_rows:
        ok, detail = _verify_original_access(
            row,
            tk=tk,
            keyframe_extractor=keyframe_extractor,
            external_kwargs=external_kwargs,
        )
        if ok:
            if not dry_run:
                evidence_store.record_access_restored(row["id"], actor="poll", resume_pending=True)
        else:
            all_restored = False
            if not dry_run and detail:
                evidence_store.record_failure(row["id"], detail)
    return all_restored
