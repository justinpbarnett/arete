from __future__ import annotations

import contextlib
import math
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import isolate_live_state

from arete import (
    adf,
    config,
    contract,
    gh,
    jira,
    paths,
    prompt_loader,
    release_proof,
    release_promotion,
    settings,
    spec_amendments,
    spec_tracker,
    ticket_feedback,
    ticket_followups,
    tracker,
)
from arete.store import (
    _rows,
    approvals,
    asks,
    chats,
    db,
    factory,
    feedback as feedback_store,
    gate_metrics,
    incidents,
    insights,
    loops,
    runs,
    tasks as tasks_store,
    unblock,
    writes,
)

isolate_live_state()


class StoreSecurityFuzzTests(unittest.TestCase):
    """Verify dynamic SQL identifier allowlisting and corrupted JSON resilience in arete/store."""

    SQLI_NAMES = (
        "id; DROP TABLE runs;--",
        "status = 'done', id",
        "1=1 OR status",
        "../runs",
        "status\x00",
        "unknown_col",
        "",
        "a.b.c",
        "runs; SELECT 1",
    )

    def test_sql_identifier_and_column_allowlists_reject_injection(self):
        for bad in self.SQLI_NAMES:
            with self.subTest(bad=bad):
                if not _rows.SQL_IDENT.fullmatch(bad):
                    with self.assertRaises(ValueError):
                        _rows.insert_sql(bad, ("a",))
                    with self.assertRaises(ValueError):
                        _rows.insert_sql("runs", (bad,))
                with self.assertRaises(ValueError):
                    _rows.insert_sql("runs", ("id",), verb=bad)
                with self.assertRaises(ValueError):
                    chats.chat_say(1, "you", "hi", **{bad: "x"})
                with self.assertRaises(ValueError):
                    incidents.update_incident(1, **{bad: "x"})
                with self.assertRaises(ValueError):
                    incidents.set_repair(1, **{bad: "x"})
                with self.assertRaises(ValueError):
                    incidents.write_mark(None, 1, {bad: "x"}, "note")
                with self.assertRaises(ValueError):
                    insights.research_keep("ref", **{bad: "x"})
                with self.assertRaises(ValueError):
                    insights.capture_save(**{bad: "x"})
                with self.assertRaises(ValueError):
                    loops.set_loop_state("poll", **{bad: "x"})
                with self.assertRaises(ValueError):
                    runs.label_step("r1", "plan", 1, **{bad: "x"})
                with self.assertRaises(ValueError):
                    writes.fields_for(bad, {})
                with self.assertRaises(ValueError):
                    writes.current(None, bad, "1")
                if bad != "a.b" and not _rows.QUALIFIED_IDENT.fullmatch(bad):
                    with self.assertRaises(ValueError):
                        factory.exclude_retained(None, bad)

    def test_corrupted_json_columns_fuzz_resilience(self):
        rng = random.Random(20261128)
        malformed = [
            None,
            "",
            "   ",
            "{",
            "[1, 2",
            "null",
            "123",
            "true",
            '"just a string"',
            "[]",
            "{}",
            '{"a": \x00}',
            "[" * 40 + "]" * 20,
        ]
        for _ in range(30):
            length = rng.randint(1, 80)
            malformed.append("".join(rng.choice('{}[]"0123:, \x00\n\t\\abc') for _ in range(length)))

        for raw in malformed:
            self.assertIsInstance(_rows.json_dict(raw), dict)
            self.assertIsInstance(_rows.json_list(raw), list)
            row = {"id": 1, "emit": raw, "review": raw, "questions": raw, "session": raw,
                   "answers": raw, "staged_answers": raw, "data": raw, "grant": raw,
                   "decision": raw, "items": raw}
            self.assertIsInstance(_rows.lists_read(row, ("items",))["items"], list)
            self.assertIsInstance(approvals.approval_of(row)["emit"], dict)
            self.assertIsInstance(asks.ask_row(row)["questions"], list)
            self.assertIsInstance(asks.ask_row(row)["staged_answers"], dict)
            self.assertIsInstance(feedback_store._decode(row)["data"], dict)
            self.assertIsInstance(gate_metrics.names(raw), tuple)
            self.assertIsInstance(incidents.parsed(row)["answers"], dict)
            self.assertIsInstance(unblock.decode(row)["grant"], dict)


class ParserAndReDoSFuzzTests(unittest.TestCase):
    """Fuzz untrusted parsers for ReDoS, recursion crashes, template injection, and malformed payloads."""

    def test_adf_depth_node_limit_task_items_and_inline_redos(self):
        # Deep nesting raises ValueError rather than RecursionError
        node: dict = {"type": "paragraph", "content": [{"type": "text", "text": "leaf"}]}
        for _ in range(80):
            node = {"type": "blockquote", "content": [node]}
        with self.assertRaises(ValueError):
            adf.to_text({"type": "doc", "version": 1, "content": [node]})

        # Wide empty tree exceeds node limit cleanly
        wide = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": []} for _ in range(105_000)]}
        with self.assertRaises(ValueError):
            adf.to_text(wide)

        # Malformed ADF payloads in task_items never raise KeyError/IndexError/TypeError
        malformed_docs = [
            None,
            "plain",
            {},
            {"content": [{}]},
            {"type": "doc", "content": None},
            {"type": "doc", "content": ["str", 123, None, {"type": "listItem", "content": []}]},
            {"type": "doc", "content": [{"type": "listItem", "content": [None, {}]}]},
            {"type": "doc", "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": []}]}]},
        ]
        for doc in malformed_docs:
            self.assertIsInstance(adf.task_items(doc), tuple)

        # Quadratic ReDoS on unclosed markdown link brackets is fixed (O(N))
        t0 = time.monotonic()
        adf.from_markdown("[a" * 20_000)
        self.assertLess(time.monotonic() - t0, 0.25)

    def test_ticket_feedback_and_followups_redos_and_malformed_fuzz(self):
        ctx = ticket_feedback.ThreadContext("demo", "1", "github")
        # Cubic backtracking on repeated "either or" is fixed (O(N))
        t0 = time.monotonic()
        evil = ticket_feedback.ThreadComment("1", "alice", "alice", "either or " * 4_000 + "end", "", "")
        res = ticket_feedback.classify_intent(evil, ctx)
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertEqual(res.intent, "correction")

        # Real ambiguity still detected accurately
        real_amb = ticket_feedback.ThreadComment("2", "alice", "alice",
                                                 "Either show all rows or show empty state, whichever you think.", "", "")
        self.assertEqual(ticket_feedback.classify_intent(real_amb, ctx).intent, "product_question")

        # Unicode superscript digit comment_id does not crash _after_clarification
        uni_comment = ticket_feedback.ThreadComment("²", "alice", "alice", "Use empty state.", "", "2026-11-27T12:00:00Z")
        self.assertIsInstance(
            ticket_feedback._after_clarification(uni_comment, {"comment_url": "https://github.com/o/r/issues/1#issuecomment-5"}),
            bool,
        )

        # Malformed ADF in Jira comment does not crash thread normalization
        repo = config.Repo("demo", Path("/tmp/demo"), tracker="jira", jira_project="APP")
        tk = tracker.JiraTracker(repo, config.JiraConfig(base_url="https://jira.example.test"))
        tk._client = jira.Client("https://jira.example.test", "bot@example.test", "tok")
        normalized = ticket_feedback.normalize_jira_comment(
            tk, "APP-1", {"id": "7", "author": None, "body": {"type": "doc", "version": 99}}
        )
        self.assertEqual(normalized.body, "")

        # Follow-ups: quadratic "instead of", whitespace runs, and deep compound clauses stay fast
        delivered = ticket_followups.DeliveredOutcome("demo", "1", "github", title="Timezone selector")
        t0 = time.monotonic()
        ticket_followups.classify_request("instead of " * 4_000 + "end", delivered)
        ticket_followups.split_requests("\n   " * 4_000 + "Please fix login crash")
        ticket_followups.split_requests("Please fix a" + "; please fix b" * 400)
        self.assertLess(time.monotonic() - t0, 0.4)
        self.assertEqual(
            ticket_followups.classify_request("Instead of showing UTC, default to local time.", delivered),
            "change_request",
        )

        # Seeded deterministic fuzzing of split_requests / classify_intent
        rng = random.Random(20261128)
        alphabet = ["fix ", "broken ", "either ", "or ", "instead of ", "and also ", "; please ", "\n  \n",
                    "<!-- arete -->", "[arete] ", "?", "thanks!", "@bot ", "\x00", "²"]
        for _ in range(40):
            sample = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40)))
            c = ticket_feedback.ThreadComment("9", "alice", "alice", sample, "", "")
            self.assertIn(ticket_feedback.classify_intent(c, ctx).intent,
                          ("correction", "product_question", "production_instruction", "neutral"))
            self.assertIsInstance(ticket_followups.split_requests(sample), list)

    def test_prompt_loader_prevents_template_and_format_injection(self):
        # string.Template is single-pass: $var / ${var} in untrusted values are NOT expanded
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp)
            (pdir / "probe.md").write_text("Owner=$owner Title=$title Body=$body")
            out = prompt_loader.prompt(
                "probe",
                prompts_dir=pdir,
                owner="real-owner",
                title="$owner ${body} $nonexistent",
                body="${owner} $$ $test_classes",
            )
            self.assertIn("Title=$owner ${body} $nonexistent", out)
            self.assertIn("Body=${owner} $$ $test_classes", out)

        # fill_template blocks attribute traversal, item lookup, and format-spec memory DoS
        for evil_tpl in (
            "{kind.__class__.__init__.__globals__}",
            "{kind.__class__}",
            "{kind[0]}",
            "{kind:999999999}",
            "{kind!r}",
        ):
            result = prompt_loader.fill_template(evil_tpl, "{kind}/{id}", kind="feat", id="42")
            self.assertEqual(result, "feat/42")
        self.assertEqual(prompt_loader.fill_template("{kind}: {title} (#{id})", "{kind}", kind="fix", id="7", title="Ok"),
                         "fix: Ok (#7)")

    def test_contract_settings_config_release_proof_hardening(self):
        # contract.validate rejects NaN and Inf (cannot bypass minimum/maximum)
        for bad_num in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(contract.Problem):
                contract.validate(bad_num, {"type": "number", "minimum": 0, "maximum": 10})

        # contract.validate rejects excessive nesting depth cleanly
        deep: dict = {}
        cur = deep
        for _ in range(80):
            cur["nested"] = {}
            cur = cur["nested"]
        recursive_schema: dict = {"type": "object", "properties": {}}
        recursive_schema["properties"]["nested"] = recursive_schema
        with self.assertRaises(contract.Problem):
            contract.validate(deep, recursive_schema)

        # settings.inline_attention rejects malformed TOML cleanly with Refused
        with self.assertRaises(settings.Refused):
            settings.inline_attention("ask = { ", 6, {"a": 1})
        t0 = time.monotonic()
        self.assertIsNone(settings.ASSIGNMENT.search("a " * 5_000))
        self.assertIsNotNone(settings.TOML_TRIVIA.match(" " * 5_000 + "!"))
        self.assertLess(time.monotonic() - t0, 0.1)

        # Unicode digits rejected in run_limit_from and github_app_table
        with self.assertRaises(settings.Refused):
            settings.run_limit_from("max_parallel", "٢")
        with self.assertRaises(ValueError):
            config.github_app_table({"app_id": "1", "installation_id": "²"})

        # release_proof hardening
        with self.assertRaises(release_proof.Held):
            release_proof.enrolled("../../tmp")
        with self.assertRaises(release_proof.Held):
            release_proof.server_url("https://example.com:99999")
        with self.assertRaises(release_proof.Held):
            release_proof.validate({
                "destination": "arete-serve",
                "path": "/api/meta",
                "assertions": [{"field": "/a", "equals": 1}],
                "recovery": [{"field": "/a", "equals": 1}],
                "observer_sha256": 12345,
            })
        with self.assertRaises(AssertionError):
            release_proof.lookup([1, 2], "/" + "9" * 5000)

        # release_promotion: malformed Jira ADF in comment does not crash comments(); parent rejects non-SHA
        repo = config.Repo("demo", Path("/tmp/demo"), github="owner/demo", tracker="jira", jira_project="APP")
        tk = tracker.JiraTracker(repo, config.JiraConfig(base_url="https://jira.example.test"))
        fake_client = mock.Mock()
        fake_client.comments.return_value = [{"id": "1", "author": {"accountId": "stranger"},
                                              "body": {"type": "doc", "version": 99}, "created": "2026-11-27T00:00:00Z"}]
        tk._client = fake_client
        rows = release_promotion.comments(repo, "APP-1", tk)
        self.assertEqual(rows[0][1], "")
        with self.assertRaises(release_proof.Held):
            release_promotion.parent(repo, "../evil?x=1")

        # spec_amendments.original handles missing claim/ledger cleanly
        with mock.patch.object(spec_amendments.specs, "find", return_value=None), \
             mock.patch.object(spec_amendments.ledger, "find", return_value=None):
            self.assertEqual(
                spec_amendments.original({"repo": "demo", "data": {"ticket_version": "v1"}},
                                         mock.Mock(parent="1")),
                {},
            )

        # spec_tracker.GitHub.item rejects non-dict / invalid labels cleanly
        gh_spec = spec_tracker.GitHub("owner/demo")
        with self.assertRaises(spec_tracker.Held):
            gh_spec.item({"number": 1, "state": "open", "repository_url": f"{gh.GITHUB_API}/repos/owner/demo",
                          "labels": "not-a-list"})
        with self.assertRaises(spec_tracker.Held):
            gh_spec.address("../evil#1")


class MarkerSpoofingAndExternalClientTests(unittest.TestCase):
    """Verify marker-spoofing defenses, CLI/URL injection blocks, and tracker path traversal defenses."""

    def test_clarification_and_followup_markers_resist_spoofing(self):
        repo = config.Repo("demo", Path("/tmp/demo"), github="owner/demo", tracker="github", pipeline=True)
        tk = tracker.GitHubTracker(repo)

        # Untrusted outsider posting a spoofed marker is ignored by _find_marker_url and _find_marker_comment_url
        spoofed = ticket_feedback.ThreadComment(
            "999", "attacker", "attacker",
            "<!-- arete -->\narete-clarify-clarify-0123456789abcdef-nonce",
            "https://github.com/owner/demo/issues/1#issuecomment-999",
            "2026-11-27T12:00:00Z",
        )
        legit = ticket_feedback.ThreadComment(
            "1000", "owner", "owner",
            "[arete] Question\n\narete-clarify-clarify-0123456789abcdef-nonce",
            "https://github.com/owner/demo/issues/1#issuecomment-1000",
            "2026-11-27T12:00:01Z",
        )
        with mock.patch.object(gh, "me", return_value="owner"):
            self.assertEqual(
                ticket_feedback._find_marker_url(repo, tk, "1", "arete-clarify-clarify-0123456789abcdef-nonce", [spoofed]),
                "",
            )
            self.assertEqual(
                ticket_feedback._find_marker_url(repo, tk, "1", "arete-clarify-clarify-0123456789abcdef-nonce", [spoofed, legit]),
                legit.url,
            )

        with mock.patch.object(ticket_feedback, "fetch_thread_comments", return_value=[spoofed]), \
             mock.patch.object(gh, "me", return_value="owner"):
            self.assertEqual(
                ticket_followups._find_marker_comment_url(repo, tk, "1", "arete-clarify-clarify-0123456789abcdef-nonce"),
                "",
            )

        # Follow-up markers include unpredictable random nonces across distinct plans
        delivered = ticket_followups.DeliveredOutcome("demo", "1", "github", title="Title")
        planned = [ticket_followups.PlannedRequest("bug", "Label", "Fix: x", "sum", "ev", ("check",), "fp1")]
        comment_row = {"ticket": "1", "comment_id": "10", "author": "owner", "body": "fix"}
        f1, i1 = ticket_followups._build_plan_rows("fup-1", repo, tk, delivered, comment_row, planned)
        f2, i2 = ticket_followups._build_plan_rows("fup-1", repo, tk, delivered, comment_row, planned)
        self.assertNotEqual(f1["reply_marker"], f2["reply_marker"])
        self.assertNotEqual(i1[0]["marker"], i2[0]["marker"])

        # gh.publication_url & jira.Client.find_comment_marker enforce author when provided
        with mock.patch.object(gh, "gh_json", return_value=[[
            {"user": {"login": "attacker"}, "body": "marker-x", "html_url": "https://github.com/o/r/issues/1#issuecomment-1"},
            {"user": {"login": "arete-bot"}, "body": "marker-x", "html_url": "https://github.com/o/r/issues/1#issuecomment-2"},
        ]]):
            self.assertEqual(
                gh.find_issue_comment_marker("owner/demo", 1, "marker-x", author="arete-bot"),
                "https://github.com/o/r/issues/1#issuecomment-2",
            )
            self.assertEqual(
                gh.find_issue_comment_marker("owner/demo", 1, "marker-x", author="nobody"),
                "",
            )

        client = jira.Client("https://jira.example.test", "bot@example.test", "tok")
        with mock.patch.object(client, "comments", return_value=[
            {"id": "10", "author": {"accountId": "attacker"}, "body": "marker-x"},
            {"id": "11", "author": {"accountId": "bot-acct"}, "body": "marker-x"},
        ]):
            self.assertEqual(
                client.find_comment_marker("APP-1", "marker-x", author="bot-acct"),
                "https://jira.example.test/browse/APP-1?focusedCommentId=11",
            )

    def test_gh_and_jira_cli_url_and_path_injection_defenses(self):
        # ensure_label puts `--` before flag-like label names
        with mock.patch.object(gh, "gh", return_value="") as call:
            gh.ensure_label("owner/demo", "--evil-label")
            argv = call.call_args.args[0]
            self.assertEqual(argv[-2:], ["--", "--evil-label"])

        # Reject path traversal, query injection, and flag injection in gh helpers
        for bad_repo in ("../evil", "owner/..", "-flag/repo", "owner/repo?x=1", "owner/repo#frag"):
            with self.assertRaises(gh.GhError):
                gh.ensure_branch(bad_repo, "main")
        for bad_ref in ("../evil", "main?foo=bar", "main#frag", "-flag", "a/../b"):
            with self.assertRaises(gh.GhError):
                gh.ensure_branch("owner/demo", bad_ref)
            with self.assertRaises(gh.GhError):
                gh.branch_sha("owner/demo", bad_ref)
        for bad_path in ("../secret", "/etc/passwd", "a/../b", "file?ref=x"):
            with self.assertRaises(gh.GhError):
                gh.file_sha("owner/demo", bad_path, "main")
            with self.assertRaises(gh.GhError):
                gh.put_file("owner/demo", bad_path, b"x", "main", "msg")
        with self.assertRaises(gh.GhError):
            gh.pr_view("--help")
        with self.assertRaises(gh.GhError):
            gh.pr_close("-f")
        self.assertEqual(gh.pr_repo("https://github.com/../../admin/pull/1"), "")
        self.assertEqual(gh.pr_number("https://github.com/owner/demo/pull/42?w=1#discussion"), 42)
        self.assertTrue(gh.token_overreach({"permissions": {"contents": "read"}, "repositories": [{"full_name": "o/r"}, "bad"]}, "o/r"))

        # Jira Client validates base_url in __init__, blocks host-confusion paths, URL-encodes browse_url, handles NaN Retry-After
        with self.assertRaises(jira.JiraError):
            jira.Client("file:///etc/passwd", "a", "b")
        with self.assertRaises(jira.JiraError):
            jira.Client("https://user:pass@jira.example.test", "a", "b")
        client = jira.Client("https://jira.example.test", "a", "b")
        for bad_api_path in ("@evil.com/x", "//evil.com/x", "rest/api/2/myself"):
            with self.assertRaises(jira.JiraError):
                client._send("GET", bad_api_path)
        self.assertEqual(client.browse_url("APP-1/../../admin"), "https://jira.example.test/browse/APP-1%2F..%2F..%2Fadmin")
        fake_err = mock.Mock(headers={"Retry-After": "nan"})
        self.assertTrue(math.isfinite(jira._retry_after(fake_err, 1)))

    def test_tracker_save_images_traversal_jql_escaping_and_waits_hardening(self):
        # JiraTracker.save_images sanitizes img.id so traversal cannot escape dest
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "images"
            outside = Path(tmp) / "escaped.png"
            repo = config.Repo("demo", Path(tmp), tracker="jira", jira_project="APP")
            tk = tracker.JiraTracker(repo, config.JiraConfig(base_url="https://jira.example.test"))
            tk._client = mock.Mock()
            tk._client.attachment.return_value = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 8
            t = tracker.Ticket("APP-1", "APP-1", "Title", images=[tracker.Image("../../escaped", "shot.png", 24)])
            saved = tk.save_images(t, dest)
            self.assertFalse(outside.exists())
            self.assertEqual(len(saved.paths), 1)
            self.assertEqual(saved.paths[0].resolve().parent, dest.resolve())

        # candidates_jql escapes quotes in project/route
        jql = tracker.candidates_jql('APP" OR project != "X')
        self.assertIn(r'project = "APP\" OR project != \"X"', jql)

        # github_waits rejects traversal, leading '-', #0, and runs linearly on adversarial input
        self.assertEqual(tracker.github_waits("Waits on ../../etc#0, -flag/repo#1, owner/..#2, owner/ok#5", "o/r"),
                         [("owner/ok", 5)])
        t0 = time.monotonic()
        self.assertEqual(tracker.github_waits("Waits on " + "1." * 10_000, "o/r"), [])
        self.assertLess(time.monotonic() - t0, 0.1)

        # batch_ids raises TaskError cleanly on non-numeric ticket IDs; normalize rejects 0 and unicode digits
        with self.assertRaises(tracker.TaskError):
            tracker.batch_ids(tracker.Ticket("APP-1", "APP-1", "t", body="Batch issues: #1", labels=["agent:batch"]))
        gh_tk = tracker.GitHubTracker(config.Repo("demo", Path("/tmp/demo"), github="owner/demo"))
        for bad_tid in ("0", "#0", "²", "-1"):
            with self.assertRaises(ValueError):
                gh_tk.normalize(bad_tid)

    def test_local_followup_task_creation_regression(self):
        # Regression: _ensure_local_followup_task previously passed body twice and unsupported state= kwarg
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state = root / "state"
            with contextlib.ExitStack() as stack:
                for name, value in {
                    "STATE_DIR": state,
                    "DB_FILE": state / "arete.db",
                    "CONFIG_DIR": root / "config",
                    "CONFIG_FILE": root / "config" / "config.toml",
                    "RUNS_DIR": state / "runs",
                    "WORKTREES_DIR": state / "worktrees",
                    "LOCKS_DIR": state / "locks",
                    "LOG_DIR": state / "logs",
                }.items():
                    stack.enter_context(mock.patch.object(paths, name, value))
                stack.enter_context(mock.patch.object(db, "_CHECKED", set()))
                paths.ensure_dirs()
                db.migrate()
                repo = config.Repo("demo", root, tracker="local", pipeline=True)
                item = {"kind": "bug", "title": "Fix crash", "body": "Details", "marker": "arete-followup-task-1-abc"}
                tid, ref, url = ticket_followups._ensure_local_followup_task(repo, item)
                self.assertEqual(tid, ref)
                self.assertEqual(url, f"local:{tid}")
                saved = tasks_store.task_get(tid)
                self.assertIsNotNone(saved)
                self.assertEqual(saved["title"], "Fix crash")
                self.assertIn("arete-followup-task-1-abc", saved["body"])
                # Replay reconciles via marker without duplicating
                tid2, _, _ = ticket_followups._ensure_local_followup_task(repo, item)
                self.assertEqual(tid2, tid)

    def test_new_main_modules_hardening(self):
        from arete import configrepo, cross_repo, external_evidence, ticket_media
        from arete.store import config as config_store

        # 1. ticket_media: linear-time extraction on adversarial inputs & malformed IPv6 URLs
        t0 = time.monotonic()
        refs = ticket_media.extract_media_refs(
            "![a" * 8000 + "\n<img " * 4000 + "\nhttps://[invalid-ipv6/pic.png\n",
            "[link" * 8000,
        )
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(refs, [])
        self.assertEqual(ticket_media.fetch_url("https://[invalid-ipv6/a.png").error_status, "inaccessible")

        # 2. external_evidence: malformed IPv6 URLs never crash extraction
        self.assertEqual(external_evidence.extract_external_refs("see https://[bad-url/archives/C123456789/p1"), [])
        self.assertEqual(external_evidence.display_url("https://[bad-url"), "")

        # 3. cross_repo: reject flag-like or traversal repo slugs
        self.assertIsNone(cross_repo.GH_REF_RE.search("-evil/repo#1"))
        self.assertIsNone(cross_repo.GH_REF_RE.search("org/..#1"))
        self.assertIsNotNone(cross_repo.GH_REF_RE.search("org/repo#1"))

        # 4. store/config.by_selector: Unicode digits and huge digit strings never crash int()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state = root / "state"
            with contextlib.ExitStack() as stack:
                for name, value in {
                    "STATE_DIR": state,
                    "DB_FILE": state / "arete.db",
                    "CONFIG_DIR": root / "config",
                    "CONFIG_FILE": root / "config" / "config.toml",
                    "RUNS_DIR": state / "runs",
                    "WORKTREES_DIR": state / "worktrees",
                    "LOCKS_DIR": state / "locks",
                    "LOG_DIR": state / "logs",
                }.items():
                    stack.enter_context(mock.patch.object(paths, name, value))
                stack.enter_context(mock.patch.object(db, "_CHECKED", set()))
                paths.ensure_dirs()
                db.migrate()
                self.assertIsNone(config_store.by_selector("repo", "demo", "٢"))
                self.assertIsNone(config_store.by_selector("repo", "demo", "9" * 5000))
                # 5. configrepo: traversal loop name in loop-switches.json must not write outside CONFIG_DIR
                (state / "loop-switches.json").write_text('{"../../escaped": true}', encoding="utf-8")
                with mock.patch.object(configrepo, "_git"):
                    configrepo.init()
                self.assertFalse((root / "escaped").exists())


if __name__ == "__main__":
    unittest.main()
