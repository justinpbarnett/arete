from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import environment, outbound, paths, util
import contextlib

MAX_MEDIA_ITEMS = 6
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 25 * 1024 * 1024
MAX_KEYFRAMES = 6
DOWNLOAD_TIMEOUT = 30
FFMPEG_TIMEOUT = 20

MP4_BRANDS = frozenset({
    b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6",
    b"mp41", b"mp42", b"avc1", b"M4V ", b"dash", b"mmp4",
})
MOV_BRANDS = frozenset({b"qt  "})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".m4v", ".webm", ".mov"})
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

GITHUB_AUTH_HOSTS = frozenset({
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "user-images.githubusercontent.com",
    "private-user-images.githubusercontent.com",
})
GITHUB_MEDIA_HOSTS = GITHUB_AUTH_HOSTS | frozenset({
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})
DROPBOX_HOSTS = frozenset({"dropbox.com", "www.dropbox.com", "dl.dropboxusercontent.com"})
AUTH_REDIRECT_HINTS = ("/login", "/session", "/signin", "id.atlassian.com")

TOKEN_PATTERN = re.compile(
    r"(?:Bearer\s+[A-Za-z0-9._~+/=-]{8,}|xox[baprs]-[A-Za-z0-9-]+|sl\.[A-Za-z0-9._-]{12,})",
    re.IGNORECASE,
)

MD_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>https?://[^\s)]+)(?:\s+[\"'][^\"']*[\"'])?\)")
MD_LINK_RE = re.compile(r"(?<!!)\[(?P<alt>[^\]]*)\]\((?P<url>https?://[^\s)]+)(?:\s+[\"'][^\"']*[\"'])?\)")
HTML_MEDIA_RE = re.compile(
    r"<(?P<tag>img|video|source)\b[^>]*?\bsrc=[\"'](?P<url>https?://[^\"'\s>]+)[\"'][^>]*>",
    re.IGNORECASE,
)
BARE_URL_RE = re.compile(r"(?<![(\"'=])(?P<url>https?://[^\s<>)\"']+)")

MediaStatus = Literal["resolved", "unreadable", "inaccessible", "unsupported"]
MediaKind = Literal["image", "video", "unknown"]


@dataclass(frozen=True)
class MediaRef:
    source: str
    origin: str
    kind: MediaKind
    url: str = ""
    attachment_id: str = ""
    name: str = ""
    mime_type: str = ""
    size: int = 0

    def to_dict(self) -> dict:
        url = ""
        if self.url:
            parts = urllib.parse.urlsplit(self.url)
            url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        return {
            "source": self.source,
            "origin": self.origin,
            "kind": self.kind,
            "url": url,
            "attachment_id": self.attachment_id,
            "name": self.name,
            "mime_type": self.mime_type,
            "size": self.size,
        }


@dataclass
class KeyframeExtraction:
    frames: list[Path] = field(default_factory=list)
    duration_seconds: float | None = None
    warning: str = ""
    error: str = ""


@dataclass
class MediaFetchResult:
    data: bytes = b""
    content_type: str = ""
    filename: str = ""
    final_url: str = ""
    status_code: int = 200
    error_status: str = ""
    detail: str = ""


@dataclass
class MediaOutcome:
    status: MediaStatus
    ref: MediaRef
    ticket_ref: str
    path: Path | None = None
    keyframes: list[Path] = field(default_factory=list)
    media_kind: str = ""
    mime_type: str = ""
    duration_seconds: float | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "resolved"

    @property
    def needs_private_help(self) -> bool:
        return self.status in ("inaccessible", "unreadable")

    @property
    def inspectable_paths(self) -> list[Path]:
        if not self.ok:
            return []
        if self.keyframes:
            return list(self.keyframes)
        return [self.path] if self.path else []

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "ticket_ref": self.ticket_ref,
            "media_kind": self.media_kind,
            "mime_type": self.mime_type,
            "path": str(self.path) if self.path else "",
            "keyframes": [str(p) for p in self.keyframes],
            "duration_seconds": self.duration_seconds,
            "detail": self.detail,
            "ref": self.ref.to_dict(),
        }


def sanitize_text(text: str) -> str:
    return outbound.redact(TOKEN_PATTERN.sub("[redacted]", str(text or "")))


def safe_filename(name: str, default: str = "media") -> str:
    cleaned = re.sub(r"[^\w.-]+", "-", name or "").strip(".-")
    return cleaned[:100] or default


def _clean_url(raw: str) -> str:
    url = html.unescape(raw.strip())
    return url.rstrip(".,;:!?]>\"'")


def _url_extension(url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path or "")
    return Path(path).suffix.lower()


def _url_filename(url: str, fallback: str = "") -> str:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path or "").rstrip("/")
    name = Path(path).name if path else ""
    return name or fallback


def is_slack_or_dropbox_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in DROPBOX_HOSTS or host == "slack.com" or host.endswith(".slack.com")


def is_github_attachment_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    if host == "github.com":
        if path.startswith(("/user-attachments/assets/", "/user-attachments/files/")):
            return True
        return bool(re.match(r"^/[^/]+/[^/]+/assets/\d+/[A-Za-z0-9-]+", path))
    return host in {
        "user-images.githubusercontent.com",
        "private-user-images.githubusercontent.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }


def kind_from_hint(url: str = "", name: str = "", mime_type: str = "", tag: str = "") -> MediaKind:
    lower_mime = (mime_type or "").lower()
    if lower_mime.startswith("image/"):
        return "image"
    if lower_mime.startswith("video/"):
        return "video"
    lower_tag = (tag or "").lower()
    if lower_tag == "img":
        return "image"
    if lower_tag in ("video", "source"):
        return "video"
    ext = Path(name).suffix.lower() if name else ""
    if not ext and url:
        ext = _url_extension(url)
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "unknown"


def _is_media_candidate_url(url: str, *, explicit_tag: bool) -> bool:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return False
    if is_slack_or_dropbox_url(url):
        return False
    if explicit_tag:
        return True
    if is_github_attachment_url(url):
        return True
    return _url_extension(url) in MEDIA_EXTENSIONS


def extract_media_refs(body: str, comments: str = "", *, origin: str = "github") -> list[MediaRef]:
    refs: list[MediaRef] = []
    seen: set[str] = set()

    def record(raw_url: str, *, source: str, name: str = "", tag: str = "", explicit_tag: bool = False) -> None:
        url = _clean_url(raw_url)
        if not url or url in seen or not _is_media_candidate_url(url, explicit_tag=explicit_tag):
            return
        seen.add(url)
        guessed_name = name.strip() or _url_filename(url)
        kind = kind_from_hint(url=url, name=guessed_name, tag=tag)
        refs.append(MediaRef(source=source, origin=origin, kind=kind, url=url, name=guessed_name))

    for source, text in (("body", body or ""), ("comment", comments or "")):
        if not text:
            continue
        for match in MD_IMAGE_RE.finditer(text):
            record(match.group("url"), source=source, name=match.group("alt"), tag="img", explicit_tag=True)
        for match in HTML_MEDIA_RE.finditer(text):
            tag = match.group("tag").lower()
            record(match.group("url"), source=source, tag=tag, explicit_tag=True)
        for match in MD_LINK_RE.finditer(text):
            record(match.group("url"), source=source, name=match.group("alt"), explicit_tag=False)
        for match in BARE_URL_RE.finditer(text):
            record(match.group("url"), source=source, explicit_tag=False)

    return refs


def sniff_media(data: bytes) -> tuple[Literal["image", "video"], str, str] | None:
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        return "image", "image/png", "png"
    if len(data) >= 4 and data.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg", "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "image/webp", "webp"
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif", "gif"
    if len(data) >= 16 and data.startswith(b"\x1a\x45\xdf\xa3") and (b"webm" in data[:128] or b"matroska" in data[:128]):
        return "video", "video/webm", "webm"
    if len(data) >= 16 and data[4:8] == b"ftyp":
        size = int.from_bytes(data[:4], "big")
        head = data[8:min(max(size, 16), 256)]
        brands = {head[:4], *(head[i:i + 4] for i in range(8, len(head) - 3, 4))}
        if brands & MP4_BRANDS:
            return "video", "video/mp4", "mp4"
        if brands & MOV_BRANDS:
            return "video", "video/quicktime", "mov"
    return None


def _looks_like_html(data: bytes, content_type: str = "") -> bool:
    if "text/html" in (content_type or "").lower():
        return True
    prefix = data[:256].lstrip().lower()
    return prefix.startswith((b"<!doctype html", b"<html"))


def _looks_like_auth_redirect(url: str) -> bool:
    lower = (url or "").lower()
    return any(hint in lower for hint in AUTH_REDIRECT_HINTS)


def github_token() -> str:
    if paths.ISOLATED:
        return ""
    for env_key in ("GH_TOKEN", "GITHUB_TOKEN"):
        if value := os.environ.get(env_key, "").strip():
            return value
    try:
        res = util.sh(["gh", "auth", "token"], timeout=5)
    except OSError:
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


class _SafeMediaRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, allow_http: bool = False):
        super().__init__()
        self.allow_http = allow_http

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        parts = urllib.parse.urlsplit(target)
        allowed_schemes = ("https", "http") if self.allow_http else ("https",)
        host = (parts.hostname or "").lower()
        if parts.scheme.lower() not in allowed_schemes or not host or outbound.private_host(host):
            raise urllib.error.URLError("redirected to disallowed host")
        new = super().redirect_request(req, fp, code, msg, headers, target)
        if new is not None:
            old_parts = urllib.parse.urlsplit(req.full_url)
            if (parts.scheme.lower(), parts.netloc.lower()) != (old_parts.scheme.lower(), old_parts.netloc.lower()):
                new.remove_header("Authorization")
                new.remove_header("Cookie")
        return new


def _disposition_filename(header: str) -> str:
    if not header:
        return ""
    if match := re.search(r"filename\*=UTF-8''([^;\s]+)", header, re.IGNORECASE):
        return urllib.parse.unquote(match.group(1)).strip("\"'")
    if match := re.search(r'filename="?([^";\r\n]+)"?', header, re.IGNORECASE):
        return match.group(1).strip()
    return ""


def fetch_url(
    url: str,
    *,
    max_bytes: int = MAX_VIDEO_BYTES,
    token: str = "",
    allow_http: bool = False,
    opener=None,
) -> MediaFetchResult:
    parts = urllib.parse.urlsplit(url)
    allowed_schemes = ("https", "http") if allow_http else ("https",)
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() not in allowed_schemes or not host or outbound.private_host(host):
        return MediaFetchResult(error_status="inaccessible", detail="refused private or unsupported media URL")
    headers = {"Accept": "image/*,video/*,*/*;q=0.8", "User-Agent": "arete-media/1"}
    if token and host in GITHUB_AUTH_HOSTS:
        headers["Authorization"] = f"Bearer {token}"
    req = util.http_request(url, method="GET", headers=headers, schemes=allowed_schemes)
    client = opener or urllib.request.build_opener(_SafeMediaRedirectHandler(allow_http=allow_http))
    try:
        with client.open(req, timeout=DOWNLOAD_TIMEOUT) as response:
            final_url = response.geturl() or url
            content_type = response.headers.get("Content-Type", "")
            filename = _disposition_filename(response.headers.get("Content-Disposition", ""))
            status_code = getattr(response, "status", 200) or 200
            if _looks_like_auth_redirect(final_url):
                return MediaFetchResult(
                    final_url=final_url,
                    status_code=status_code,
                    error_status="inaccessible",
                    detail="media URL redirected to an authentication page",
                )
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                return MediaFetchResult(
                    content_type=content_type,
                    filename=filename,
                    final_url=final_url,
                    status_code=status_code,
                    error_status="unsupported",
                    detail=f"media exceeds the {max_bytes // (1024 * 1024)} MB download cap",
                )
            return MediaFetchResult(
                data=data,
                content_type=content_type,
                filename=filename,
                final_url=final_url,
                status_code=status_code,
            )
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            detail = f"HTTP {e.code} access denied"
        elif e.code in (404, 410):
            detail = f"HTTP {e.code} not found"
        else:
            detail = f"HTTP {e.code}"
        return MediaFetchResult(status_code=e.code, error_status="inaccessible", detail=detail)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return MediaFetchResult(error_status="inaccessible", detail=sanitize_text(str(e)))


def _probe_video_duration(video_path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        res = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT,
            check=False,
            env=environment.build(()),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    try:
        duration = float(res.stdout.strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def sample_timestamps(duration: float, max_frames: int = MAX_KEYFRAMES) -> list[float]:
    if duration <= 0 or max_frames <= 0:
        return [0.0]
    if max_frames == 1:
        return [round(duration * 0.5, 2)]
    ratios = [i / max_frames for i in range(max_frames - 1)] + [0.95]
    cap = max(0.0, duration - 0.05)
    stamps: list[float] = []
    for ratio in ratios:
        ts = round(min(cap, duration * ratio), 2)
        if not stamps or ts - stamps[-1] >= 0.05:
            stamps.append(ts)
    return stamps or [0.0]


def extract_video_keyframes(
    video_path: Path,
    frames_dir: Path,
    *,
    max_frames: int = MAX_KEYFRAMES,
    probe_duration: Callable[[Path], float | None] | None = None,
    ffmpeg_bin: str | None = None,
) -> KeyframeExtraction:
    duration = (probe_duration or _probe_video_duration)(video_path)
    ffmpeg = ffmpeg_bin if ffmpeg_bin is not None else shutil.which("ffmpeg")
    if not ffmpeg:
        return KeyframeExtraction(
            duration_seconds=duration,
            warning="ffmpeg is unavailable; saved recording without extracted keyframes",
        )
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    if duration is not None and duration > 0:
        for idx, ts in enumerate(sample_timestamps(duration, max_frames=max_frames), 1):
            out_path = frames_dir / f"frame-{idx:02d}-{ts:.2f}s.png"
            try:
                res = subprocess.run(
                    [
                        ffmpeg,
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-ss",
                        f"{ts:.3f}",
                        "-i",
                        str(video_path),
                        "-frames:v",
                        "1",
                        "-y",
                        str(out_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=FFMPEG_TIMEOUT,
                    check=False,
                    env=environment.build(()),
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if res.returncode == 0 and out_path.exists() and out_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                frames.append(out_path)
    else:
        pattern = str(frames_dir / "frame-%02d.png")
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(video_path),
                    "-vf",
                    "fps=1",
                    "-frames:v",
                    str(max_frames),
                    "-y",
                    pattern,
                ],
                capture_output=True,
                text=True,
                timeout=FFMPEG_TIMEOUT,
                check=False,
                env=environment.build(()),
            )
        for candidate in sorted(frames_dir.glob("frame-*.png")):
            if candidate.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                frames.append(candidate)
    if not frames:
        return KeyframeExtraction(
            duration_seconds=duration,
            error="could not decode any readable frames from the recording",
        )
    return KeyframeExtraction(frames=frames, duration_seconds=duration)


def _with_extension(name: str, ext: str) -> str:
    stem = safe_filename(Path(name).stem if name else "", default="media")
    return f"{stem}.{ext}"


def materialize_media_bytes(
    ref: MediaRef,
    data: bytes,
    dest: Path,
    *,
    ticket_ref: str,
    prefix: str,
    content_type: str = "",
    keyframe_extractor: Callable[[Path, Path], KeyframeExtraction] | None = None,
) -> MediaOutcome:
    if not data:
        return MediaOutcome(
            status="unreadable",
            ref=ref,
            ticket_ref=ticket_ref,
            detail="downloaded media file is empty",
        )
    if _looks_like_html(data, content_type):
        return MediaOutcome(
            status="inaccessible",
            ref=ref,
            ticket_ref=ticket_ref,
            detail="received an HTML page instead of media (authentication may be required)",
        )
    sniffed = sniff_media(data)
    if sniffed is None:
        lower_ct = (content_type or ref.mime_type or "").split(";")[0].strip().lower()
        if lower_ct and not lower_ct.startswith(("image/", "video/", "application/octet-stream")):
            return MediaOutcome(
                status="unsupported",
                ref=ref,
                ticket_ref=ticket_ref,
                mime_type=lower_ct,
                detail=f"unsupported content type {lower_ct}",
            )
        return MediaOutcome(
            status="unreadable",
            ref=ref,
            ticket_ref=ticket_ref,
            mime_type=lower_ct,
            detail="media bytes are corrupted or not a recognized image/video format",
        )
    kind, mime_type, ext = sniffed
    cap = MAX_IMAGE_BYTES if kind == "image" else MAX_VIDEO_BYTES
    if len(data) > cap:
        return MediaOutcome(
            status="unsupported",
            ref=ref,
            ticket_ref=ticket_ref,
            media_kind=kind,
            mime_type=mime_type,
            detail=f"{kind} exceeds the {cap // (1024 * 1024)} MB cap",
        )
    dest.mkdir(parents=True, exist_ok=True)
    filename = _with_extension(ref.name or ref.attachment_id or kind, ext)
    out_path = dest / f"{safe_filename(prefix, default='media')}-{filename}"
    out_path.write_bytes(data)
    if kind == "video":
        extract = keyframe_extractor or extract_video_keyframes
        frames_dir = dest / f"{out_path.stem}-frames"
        extraction = extract(out_path, frames_dir)
        if extraction.error:
            return MediaOutcome(
                status="unreadable",
                ref=ref,
                ticket_ref=ticket_ref,
                path=out_path,
                media_kind=kind,
                mime_type=mime_type,
                duration_seconds=extraction.duration_seconds,
                detail=extraction.error,
            )
        return MediaOutcome(
            status="resolved",
            ref=ref,
            ticket_ref=ticket_ref,
            path=out_path,
            keyframes=extraction.frames,
            media_kind=kind,
            mime_type=mime_type,
            duration_seconds=extraction.duration_seconds,
            detail=extraction.warning,
        )
    return MediaOutcome(
        status="resolved",
        ref=ref,
        ticket_ref=ticket_ref,
        path=out_path,
        media_kind=kind,
        mime_type=mime_type,
    )


def resolve_url_media(
    refs: list[MediaRef],
    dest: Path,
    *,
    ticket_ref: str,
    fetcher: Callable[[MediaRef, int], MediaFetchResult] | None = None,
    keyframe_extractor: Callable[[Path, Path], KeyframeExtraction] | None = None,
    token: str = "",
    start_count: int = 0,
) -> tuple[list[Path], list[str], list[MediaOutcome]]:
    paths_out: list[Path] = []
    left_out: list[str] = []
    outcomes: list[MediaOutcome] = []
    accepted = start_count

    for idx, ref in enumerate(refs, start_count + 1):
        label = ref.name or _url_filename(ref.url, fallback=f"media-{idx}")
        if accepted >= MAX_MEDIA_ITEMS:
            left_out.append(label)
            outcomes.append(MediaOutcome(
                status="unsupported",
                ref=ref,
                ticket_ref=ticket_ref,
                detail=f"over the {MAX_MEDIA_ITEMS} media item cap",
            ))
            continue
        cap = MAX_IMAGE_BYTES if ref.kind == "image" else MAX_VIDEO_BYTES
        if fetcher is not None:
            fetched = fetcher(ref, cap)
        else:
            fetched = fetch_url(ref.url, max_bytes=cap, token=token)
        if fetched.error_status:
            status: MediaStatus = (
                fetched.error_status
                if fetched.error_status in ("unreadable", "inaccessible", "unsupported")
                else "inaccessible"
            )
            if status == "unsupported":
                left_out.append(label)
            outcomes.append(MediaOutcome(
                status=status,
                ref=ref,
                ticket_ref=ticket_ref,
                detail=sanitize_text(fetched.detail),
            ))
            continue
        effective_ref = MediaRef(
            source=ref.source,
            origin=ref.origin,
            kind=ref.kind,
            url=ref.url,
            attachment_id=ref.attachment_id,
            name=fetched.filename or ref.name or _url_filename(fetched.final_url or ref.url, fallback=f"media-{idx}"),
            mime_type=fetched.content_type or ref.mime_type,
            size=len(fetched.data),
        )
        outcome = materialize_media_bytes(
            effective_ref,
            fetched.data,
            dest,
            ticket_ref=ticket_ref,
            prefix=f"m{idx}",
            content_type=fetched.content_type,
            keyframe_extractor=keyframe_extractor,
        )
        outcomes.append(outcome)
        if outcome.ok:
            accepted += 1
            paths_out.extend(outcome.inspectable_paths)
        elif outcome.status == "unsupported":
            left_out.append(effective_ref.name or label)

    return paths_out, left_out, outcomes
