import json
import shutil
import struct
import subprocess
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

from arete import config, jira, pipeline, prompt_loader, ticket_media, tracker
from tests import support


def setUpModule():
    support.isolate_live_state()


def make_png(rgb: tuple[int, int, int] = (200, 10, 10), size: int = 2) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * size
    idat = zlib.compress(row * size)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def make_mp4_stub() -> bytes:
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp41\x00\x00\x00\x08free"


def stub_keyframes(video_path: Path, frames_dir: Path) -> ticket_media.KeyframeExtraction:
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for idx, (ts, color) in enumerate(((0.0, (255, 0, 0)), (1.5, (0, 255, 0)), (2.8, (0, 0, 255))), 1):
        out = frames_dir / f"frame-{idx:02d}-{ts:.2f}s.png"
        out.write_bytes(make_png(color))
        frames.append(out)
    return ticket_media.KeyframeExtraction(frames=frames, duration_seconds=3.0)


class _StubJiraClient:
    base = "https://example.atlassian.net"

    def __init__(self, issue_data: dict, comments_data: list[dict], blobs: dict[str, bytes | Exception]):
        self._issue = issue_data
        self._comments = comments_data
        self._blobs = blobs

    def me(self) -> dict:
        return {"accountId": "owner-1", "displayName": "Owner"}

    def issue(self, key: str, fields: list[str]) -> dict:
        return self._issue

    def changelog(self, key: str) -> list[dict]:
        return []

    def comments(self, key: str) -> list[dict]:
        return self._comments

    def browse_url(self, key: str) -> str:
        return f"{self.base}/browse/{key}"

    def attachment(self, attachment_id: str) -> bytes:
        val = self._blobs[attachment_id]
        if isinstance(val, Exception):
            raise val
        return val


class GitHubTicketMediaTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        self.repo = config.Repo(name="demo", path=self.root, github="owner/demo", tracker="github")
        self.tk = tracker.GitHubTracker(self.repo)

    def test_extracts_and_downloads_body_and_comment_screenshot_and_video_for_worker(self):
        issue = {
            "number": 42,
            "title": "Fix badge alignment",
            "body": (
                "Broken state is only shown here:\n"
                "![broken-badge](https://github.com/user-attachments/assets/aaaaaaaa-1111-2222-3333-444444444444)\n"
            ),
            "labels": [{"name": "agent"}],
            "url": "https://github.com/owner/demo/issues/42",
            "createdAt": "2026-05-01T10:00:00Z",
            "updatedAt": "2026-05-01T10:05:00Z",
            "state": "OPEN",
            "author": {"login": "owner"},
            "blockedBy": {"nodes": [], "totalCount": 0},
            "comments": [
                {
                    "author": {"login": "owner"},
                    "createdAt": "2026-05-01T10:02:00Z",
                    "body": (
                        "Recording where failure happens midway:\n"
                        '<video src="https://github.com/user-attachments/assets/bbbbbbbb-1111-2222-3333-444444444444"></video>'
                    ),
                },
                {
                    "author": {"login": "stranger"},
                    "createdAt": "2026-05-01T10:03:00Z",
                    "body": "![ignore-me](https://cdn.example.com/untrusted.png)",
                },
            ],
        }
        ticket = self.tk.get("42", issue=issue)
        self.assertEqual([img.source for img in ticket.images], ["body", "comment"])
        self.assertFalse(any("untrusted.png" in img.url for img in ticket.images))

        payloads = {
            "https://github.com/user-attachments/assets/aaaaaaaa-1111-2222-3333-444444444444": ticket_media.MediaFetchResult(
                data=make_png(), content_type="image/png", filename="broken-badge.png",
            ),
            "https://github.com/user-attachments/assets/bbbbbbbb-1111-2222-3333-444444444444": ticket_media.MediaFetchResult(
                data=make_mp4_stub(), content_type="video/mp4", filename="midway-failure.mp4",
            ),
        }
        saved = self.tk.save_images(
            ticket,
            self.root / "images",
            fetcher=lambda ref, cap: payloads[ref.url],
            keyframe_extractor=stub_keyframes,
        )
        self.assertEqual(len(saved.outcomes), 2)
        self.assertTrue(all(o.ok and o.ticket_ref == "#42" for o in saved.outcomes))
        self.assertEqual(saved.outcomes[0].media_kind, "image")
        self.assertEqual(saved.outcomes[0].ref.source, "body")
        self.assertEqual(saved.outcomes[1].media_kind, "video")
        self.assertEqual(saved.outcomes[1].ref.source, "comment")
        self.assertEqual(len(saved.outcomes[1].keyframes), 3)
        # 1 screenshot + 3 extracted video keyframes are all inspectable local paths.
        self.assertEqual(len(saved.paths), 4)

        note = pipeline.images_note(saved, ticket_ref=ticket.ref)
        brief = prompt_loader.Brief("demo", "ws", ticket.ref, ticket.title, ticket.body,
                                    comments=ticket.comments, attachments=note, expertise="", commands="", max_lines=100)
        rendered = brief.prompt("build", plan="Fix it", checks="pytest", verify="check", estimate=10)
        self.assertIn("Media on #42", rendered)
        self.assertIn("broken-badge.png", rendered)
        self.assertIn("frame-02-1.50s.png", rendered)
        self.assertIn("from ticket description", rendered)
        self.assertIn("from ticket comment", rendered)

    def test_distinguishes_unreadable_and_inaccessible_media_from_inspected_media(self):
        ticket = tracker.Ticket(
            "43",
            "#43",
            "Media outcomes",
            body="![good](https://cdn.example.com/good.png)\n![corrupt](https://cdn.example.com/corrupt.png)",
            owner_comments="See private clip: https://github.com/user-attachments/assets/cccccccc-1111-2222-3333-444444444444",
        )
        responses = {
            "https://cdn.example.com/good.png": ticket_media.MediaFetchResult(data=make_png(), content_type="image/png"),
            "https://cdn.example.com/corrupt.png": ticket_media.MediaFetchResult(data=b"not-a-png", content_type="image/png"),
            "https://github.com/user-attachments/assets/cccccccc-1111-2222-3333-444444444444": ticket_media.MediaFetchResult(
                status_code=403, error_status="inaccessible", detail="HTTP 403 access denied",
            ),
        }
        saved = self.tk.save_images(ticket, self.root / "images", fetcher=lambda ref, cap: responses[ref.url])
        by_url = {o.ref.url: o for o in saved.outcomes}
        self.assertEqual(by_url["https://cdn.example.com/good.png"].status, "resolved")
        self.assertEqual(by_url["https://cdn.example.com/corrupt.png"].status, "unreadable")
        self.assertTrue(by_url["https://cdn.example.com/corrupt.png"].needs_private_help)
        self.assertEqual(
            by_url["https://github.com/user-attachments/assets/cccccccc-1111-2222-3333-444444444444"].status,
            "inaccessible",
        )
        self.assertEqual(len(saved.paths), 1)

        note = pipeline.images_note(saved, ticket_ref=ticket.ref)
        self.assertIn("Unreadable or inaccessible media", note)
        self.assertIn("`unreadable` (from ticket description)", note)
        self.assertIn("`inaccessible` (from ticket comment)", note)

    def test_refuses_private_hosts_and_strips_auth_off_github_hosts(self):
        refused = ticket_media.fetch_url("https://127.0.0.1/secret.png", token="ghp_secret123456")
        self.assertEqual(refused.error_status, "inaccessible")
        self.assertNotIn("ghp_secret123456", refused.detail)

        seen_headers = {}

        class _FakeResponse:
            status = 200
            headers = {"Content-Type": "image/png"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def geturl(self):
                return "https://cdn.example.com/pic.png"

            def read(self, n=-1):
                return make_png()

        class _FakeOpener:
            def open(self, req, timeout=0):
                seen_headers.update(dict(req.header_items()))
                return _FakeResponse()

        res = ticket_media.fetch_url("https://cdn.example.com/pic.png", token="ghp_secret123456", opener=_FakeOpener())
        self.assertEqual(res.error_status, "")
        self.assertNotIn("Authorization", seen_headers)

        import urllib.request

        handler = ticket_media._SafeMediaRedirectHandler()
        orig = urllib.request.Request(
            "https://github.com/user-attachments/assets/abc",
            headers={"Authorization": "Bearer ghp_secret123456", "Cookie": "sid=1"},
        )
        redirected = handler.redirect_request(
            orig, None, 302, "Found", {}, "https://objects.githubusercontent.com/signed-blob",
        )
        self.assertIsNotNone(redirected)
        self.assertFalse(redirected.has_header("Authorization"))
        self.assertFalse(redirected.has_header("Cookie"))


class JiraTicketMediaTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        self.repo = config.Repo(name="app", path=self.root, github="owner/app", tracker="jira", jira_project="APP")
        self.tk = tracker.JiraTracker(self.repo, config.JiraConfig(project="APP"))

    def test_collects_body_and_comment_images_and_videos_and_manifest(self):
        issue = {
            "id": "1001",
            "key": "APP-12",
            "fields": {
                "summary": "Fix modal flicker",
                "description": "See attached screenshot !desc-shot.png! for the wrong state.",
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": ["agent", "repo-app"],
                "issuetype": {"name": "Bug"},
                "created": "2026-05-01T10:00:00.000+0000",
                "updated": "2026-05-01T10:05:00.000+0000",
                "creator": {"accountId": "owner-1"},
                "assignee": {"accountId": "owner-1"},
                "attachment": [
                    {
                        "id": "att-1",
                        "filename": "desc-shot.png",
                        "size": 120,
                        "mimeType": "image/png",
                        "author": {"accountId": "owner-1"},
                        "created": "2026-05-01T10:00:00.000+0000",
                    },
                    {
                        "id": "att-2",
                        "filename": "repro.mp4",
                        "size": 240,
                        "mimeType": "video/mp4",
                        "author": {"accountId": "owner-1"},
                        "created": "2026-05-01T10:02:00.000+0000",
                    },
                    {
                        "id": "att-3",
                        "filename": "denied.png",
                        "size": 100,
                        "mimeType": "image/png",
                        "author": {"accountId": "owner-1"},
                        "created": "2026-05-01T10:03:00.000+0000",
                    },
                    {
                        "id": "att-4",
                        "filename": "corrupt.png",
                        "size": 100,
                        "mimeType": "image/png",
                        "author": {"accountId": "owner-1"},
                        "created": "2026-05-01T10:04:00.000+0000",
                    },
                ],
            },
        }
        comments = [
            {
                "author": {"accountId": "owner-1", "displayName": "Owner"},
                "created": "2026-05-01T10:02:00.000+0000",
                "body": "Uploaded recording [attachment att-2] showing failure partway through.",
            },
        ]
        blobs = {
            "att-1": make_png(),
            "att-2": make_mp4_stub(),
            "att-3": jira.JiraError("jira GET /rest/api/2/attachment/content/att-3: HTTP 403 Forbidden", 403),
            "att-4": b"corrupted-bytes",
        }
        client = _StubJiraClient(issue, comments, blobs)
        self.tk.__dict__["client"] = client  # shadow property isn't used; patch via subclass helper
        orig_prop = type(self.tk).client
        type(self.tk).client = property(lambda _self: client)
        self.addCleanup(setattr, type(self.tk), "client", orig_prop)

        ticket = self.tk.get("APP-12", include_environment=False)
        by_id = {img.id: img for img in ticket.images}
        self.assertEqual(by_id["att-1"].source, "body")
        self.assertEqual(by_id["att-1"].kind, "image")
        self.assertEqual(by_id["att-2"].source, "comment")
        self.assertEqual(by_id["att-2"].kind, "video")

        orig_save = self.tk.save_images
        self.tk.save_images = lambda t, dest: orig_save(t, dest, keyframe_extractor=stub_keyframes)
        attachments, add_dirs, manifest = pipeline.collect_ticket_attachments(self.tk, ticket, self.root / "run")

        self.assertIn(self.root / "run" / "images", add_dirs)
        self.assertEqual(manifest["ticket"], "APP-12")
        statuses = {row["ref"]["attachment_id"]: row["status"] for row in manifest["media"]}
        self.assertEqual(statuses, {"att-1": "resolved", "att-2": "resolved", "att-3": "inaccessible", "att-4": "unreadable"})
        self.assertIn("Media on APP-12", attachments)
        self.assertIn("desc-shot.png", attachments)
        self.assertIn("frame-02-1.50s.png", attachments)
        self.assertIn("`inaccessible`", attachments)
        self.assertIn("`unreadable`", attachments)
        manifest_path = self.root / "run" / "evidence-manifest.json"
        self.assertTrue(manifest_path.exists())
        saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_manifest["ticket"], "APP-12")


class VideoKeyframeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))

    def test_ffmpeg_unavailable_falls_back_gracefully(self):
        video = self.root / "clip.mp4"
        video.write_bytes(make_mp4_stub())
        res = ticket_media.extract_video_keyframes(
            video, self.root / "frames", probe_duration=lambda _p: 4.0, ffmpeg_bin="",
        )
        self.assertEqual(res.frames, [])
        self.assertEqual(res.error, "")
        self.assertIn("ffmpeg is unavailable", res.warning)

    def test_corrupt_video_with_extractor_error_is_marked_unreadable(self):
        ref = ticket_media.MediaRef(source="comment", origin="github", kind="video", name="bad.mp4")
        outcome = ticket_media.materialize_media_bytes(
            ref,
            make_mp4_stub(),
            self.root / "images",
            ticket_ref="#99",
            prefix="m1",
            keyframe_extractor=lambda _v, _d: ticket_media.KeyframeExtraction(error="could not decode frames"),
        )
        self.assertEqual(outcome.status, "unreadable")
        self.assertTrue(outcome.needs_private_help)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not installed")
    def test_real_ffmpeg_extracts_sequence_capturing_midway_failure(self):
        video = self.root / "sequence.mp4"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=red:s=32x32:d=1:r=5",
                "-f", "lavfi", "-i", "color=c=green:s=32x32:d=1:r=5",
                "-f", "lavfi", "-i", "color=c=blue:s=32x32:d=1:r=5",
                "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0",
                "-pix_fmt", "yuv420p", str(video),
            ],
            check=True,
        )
        res = ticket_media.extract_video_keyframes(video, self.root / "frames", max_frames=4)
        self.assertEqual(res.error, "")
        self.assertGreaterEqual(len(res.frames), 3)
        self.assertIsNotNone(res.duration_seconds)
        self.assertTrue(all(f.read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for f in res.frames))
        # Verify at least one frame falls partway through the 3s recording (in the middle segment).
        timestamps = [float(f.stem.rsplit("-", 1)[1].rstrip("s")) for f in res.frames]
        self.assertTrue(any(0.5 < ts < 2.5 for ts in timestamps), f"unexpected timestamps: {timestamps}")
