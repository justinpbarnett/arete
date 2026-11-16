from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from arete import config

from tests import support
from tests.harness import behavior_result, make_folders, scratch_env

SCRIPTS = Path(__file__).resolve().parent / "scripts"


def setUpModule():
    support.isolate_live_state()


class ResearchRetentionTests(unittest.TestCase):
    SCRIPT = (SCRIPTS / "research_retention.py").read_text()

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "replay", "home")
            (root / "config/config.toml").write_text('[replay]\nall = true\n\n[jev]\nenabled = false\n')
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_REPLAY_DIR=str(root / "replay"),
                              TMPDIR=str(root), HOME=str(root / "home"), PYTHONPATH=str(support.SOURCE))
            done = subprocess.run([sys.executable, "-c", cls.SCRIPT, str(root)], env=env, cwd=support.SOURCE,
                                  capture_output=True, text=True, timeout=120, check=False)
            if done.returncode:
                raise AssertionError(done.stdout[-2000:] + done.stderr[-3000:])
            cls.out = behavior_result(done, marker="RESULT")

    def test_window_eviction_keeps_report_cost_metadata_and_answered_link(self):
        self.assertEqual(self.out["first"], "done")
        w = self.out["window"]
        self.assertTrue(w["absent"])
        self.assertTrue(w["same_row"])
        self.assertEqual(w["row"]["status"], "done")
        self.assertEqual(w["row"]["cost"], .75)
        self.assertEqual(w["shown"], w["text"])
        self.assertIn(w["text"], w["linked"])
        self.assertNotIn("T-1 ", w["listing"])
        for result in (self.out["initial_cli"], w["cli"], w["linked_cli"]):
            self.assertEqual(result[0], 0)
            self.assertIn(w["text"], result[1])

    def test_return_and_timestamp_change_keep_the_report_without_another_paid_call(self):
        self.assertNotIn("T-1", self.out["return"]["due"])
        self.assertEqual(self.out["return"]["outcome"], "kept")
        self.assertEqual(self.out["changed_stamp"], "kept")
        self.assertEqual(self.out["calls"].count("research T-1"), 1)

    def test_source_removal_keeps_rows_and_restoration_rebuilds_only_triage(self):
        self.assertEqual(self.out["off"]["board"], [])
        self.assertTrue(self.out["off"]["same_rows"])
        self.assertCountEqual(self.out["off"]["refs"], ["T-1", "T-3"])
        self.assertEqual(self.out["on"]["calls"], ["triage T-1"])
        self.assertNotIn("T-1", self.out["on"]["due"])
        self.assertEqual(self.out["on"]["shown"], self.out["window"]["text"])
        self.assertEqual(self.out["on"]["cli"][0], 0)
        self.assertIn(self.out["window"]["text"], self.out["on"]["cli"][1])

    def test_orphans_are_adopted_once_and_interrupted_runs_still_fail(self):
        o = self.out["orphans"]
        for ref, cost in (("T-9", .5), ("T-10", .4), ("T-11", 0), ("T-12", 0)):
            row = o["rows"][ref]
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["cost"], cost)
            self.assertEqual(row["report"], o["files"][ref])
            self.assertNotIn(None, row.values())
            for field in ("updated", "source", "ticket", "basis", "answered_by", "filed_path", "filed_url",
                          "moved", "delivered", "error", "run_id"):
                self.assertEqual(row[field], "")
        self.assertEqual(o["rows"]["T-9"]["title"], "Title")
        self.assertEqual(o["rows"]["T-9"]["url"], "https://example.test/T-9")
        self.assertEqual(o["rows"]["T-9"]["summary"], "S")
        for field in ("url", "summary"):
            self.assertEqual(o["rows"]["T-12"][field], "")
        for ref in ("notes", "T-8", "T-13"):
            self.assertIsNone(o["rows"][ref])
        self.assertIsNone(o["wrong_ref"])
        self.assertEqual(o["shown"], o["files"]["T-9"])
        for result, ref in ((o["cli"], "T-9"), (o["current_cli"], "T-10")):
            self.assertEqual(result[0], 0)
            self.assertIn(o["files"][ref], result[1])
        self.assertNotEqual(o["unknown_cli"][0], 0)
        self.assertIn("no research", o["unknown_cli"][1])
        self.assertEqual(o["running"]["status"], "failed")
        self.assertEqual(o["running"]["error"], "the run stopped before it finished")
        self.assertTrue(self.out["idempotent"])


class StagedResearchSynthesisTests(unittest.TestCase):
    SCRIPT = (SCRIPTS / "staged_research_synthesis.py").read_text()

    @classmethod
    def setUpClass(cls):
        gh_state = support.SOURCE / ".local/state/gh"
        had_gh_state = gh_state.exists()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "replay", "home")
            (root / "config/config.toml").write_text(
                '[replay]\nall = true\n\n[research]\nplanner = true\nsearch = true\nsynthesis = true\n\n'
                '[jev]\nenabled = false\n')
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_REPLAY_DIR=str(root / "replay"),
                              TMPDIR=str(root), HOME=str(root / "home"), PYTHONPATH=str(support.SOURCE))
            done = subprocess.run([sys.executable, "-c", cls.SCRIPT, str(root)], env=env, cwd=support.SOURCE,
                                  capture_output=True, text=True, timeout=120, check=False)
            cls.out = behavior_result(done, marker="RESULT")
            cls.home = str(root / "home")
        cls.wrote_gh_state = not had_gh_state and gh_state.exists()

    def names(self, which):
        return [c[0] for c in self.out[which]["calls"]]

    def test_refused_id_is_retried_and_conflict_and_gap_round_reach_the_report(self):
        a = self.out["a"]
        self.assertTrue(a["ok"], a["error"])
        report = a["structured"]["report"]
        heads = [report.index(f"## {h}") for h in
                 ("Supported conclusions", "Inference", "Unresolved contradictions", "Gaps", "Sources")]
        self.assertEqual(heads, sorted(heads))
        self.assertEqual(self.names("a").count("research A synthesis"), 2)
        self.assertNotIn("S9", report)
        self.assertNotIn("outside.example", report)
        self.assertIn("Cited [S1](https://example.org/a)", report)
        self.assertIn("answer holds '[', ']', '\\x0c'", a["evidence"]["synthesis"]["rounds"][0]["refused"][0])
        contradictions = report[heads[2]:heads[3]]
        self.assertIn("[S1](https://example.org/a)", contradictions)
        self.assertIn("[S2](https://example.net/b)", contradictions)
        self.assertIn("research A task 4 search", self.names("a"))
        self.assertIn("[S4](https://example.org/d)", report)
        ids = {s["url"]: s["id"] for s in a["evidence"]["sources"]}
        self.assertEqual(ids["https://example.org/a"], "S1")
        self.assertEqual(ids["https://example.org/d"], "S4")
        self.assertEqual(a["evidence"]["sources"][0]["tasks"], [1, 4])
        self.assertEqual(a["evidence"]["workers"][3]["round"], 1)
        self.assertEqual(a["evidence"]["synthesis"]["cited"], ["S1", "S2", "S3", "S4"])

    def test_collator_never_gets_web_tools(self):
        for which in "ab":
            synthesis = [c for c in self.out[which]["calls"] if "synthesis" in c[0]]
            self.assertTrue(synthesis)
            for _, tools in synthesis:
                self.assertEqual(tools, ["Read", "Glob", "Grep"])
            self.assertTrue(all("WebSearch" in c[1] for c in self.out[which]["calls"] if c[0].endswith("search")))
        self.assertIn("WebSearch cannot load on research_synthesis", self.out["web"])

    def test_extra_requests_become_explicit_gaps(self):
        b = self.out["b"]
        self.assertTrue(b["ok"], b["error"])
        self.assertIn("both extra search rounds were used", b["structured"]["report"])
        self.assertNotIn("research B task 6 search", self.names("b"))
        self.assertNotIn("research B gap 3 synthesis", self.names("b"))
        c = self.out["c"]
        self.assertTrue(c["ok"], c["error"])
        self.assertIn("the research budget left no room for another search round", c["structured"]["report"])
        self.assertNotIn("research C task 4 search", self.names("c"))

    def test_synthesis_off_keeps_the_report_call(self):
        d = self.out["d"]
        self.assertTrue(d["ok"], d["error"])
        self.assertEqual(d["structured"]["report"], "Plain report.")
        self.assertIn("research D report", self.names("d"))
        self.assertFalse([n for n in self.names("d") if "synthesis" in n])
        self.assertNotIn("synthesis", d["evidence"])

    def setUp(self):
        self.known = {"S1": "https://example.org/a", "S2": "https://example.net/b"}

    @staticmethod
    def synthesis(field="answer", text="The limit is 10 [S1]."):
        answer = {"summary": "Short.", "name": "", "answer": "The limit is 10 [S1].",
                  "conclusions": [{"claim": "Documented", "sources": ["S1"], "critical": True}],
                  "inferences": [{"claim": "Likely", "basis": "Stated", "sources": ["S1"], "critical": False}],
                  "contradictions": [{"issue": "Limit differs", "sources": ["S1", "S2"]}],
                  "gaps": [{"question": "Which limit applies?", "why": "Unstated"}],
                  "searches": [{"goal": "Find the limit", "queries": ["limit terms"], "max_sources": 2}]}
        for item in (answer, *(entry for key in ("conclusions", "inferences", "contradictions", "gaps", "searches")
                               for entry in answer[key])):
            if field in item:
                item[field] = text
        return answer

    @staticmethod
    def evidence(*urls, title="Vendor"):
        return {"sources": [{"id": f"S{n}", "url": url, "title": title, "published": "2025-01-02", "event_date": ""}
                            for n, url in enumerate(urls, 1)]}

    def test_model_text_holding_link_syntax_is_refused_in_every_field(self):
        from arete import research_synthesis as rs
        far = "//outside.example/"
        self.assertEqual(rs.faults(self.synthesis(), self.known), [])
        for text in (f"See [the note](\f{far}).", f"See [the note](\v{far}).", f"See ![x](\f{far}p.png).",
                     "See \fhttp:outside.example/ now.", f"Notes:\n\n- [ ] [r]: {far}",
                     "Notes:\n\n- [x] [r]: http:outside.example/", f"See [x][r].\n\n> [r]: {far}",
                     f"See [S1][r].\n\n[r]: {far}", "See \\[S1] now.", "See [S3].", "See <b>x</b>.", "Q&A here.",
                     "Mail admin@outside.example.", "See www.outside.example now.", "See https://outside.example/.",
                     "Write mailto:admin now.", "Write XMPP:admin now.", "Run javascript:alert now.",
                     "Load data:text/html now.", "Line\rbreak.", "Null\x00here."):
            for field in ("answer", "claim", "basis", "issue", "question", "why", "goal"):
                found = rs.faults(self.synthesis(field, text), self.known)
                self.assertTrue(found, (field, text))
        found = rs.faults(self.synthesis("answer", f"See [the note](\f{far})."), self.known)
        self.assertEqual(found, ["answer holds '[', ']', '\\x0c'; " + rs.PLAIN])

    def test_summary_cites_no_source_because_it_is_written_raw(self):
        from arete import research_synthesis as rs
        self.assertTrue(rs.faults(self.synthesis("summary", "Limit 10 [S1]."), self.known))
        self.assertTrue(rs.faults(self.synthesis("summary", "See [S1]( //outside.example/)."), self.known))
        self.assertEqual(rs.faults(self.synthesis("summary", "Limit 10, per the vendor."), self.known), [])

    def test_cites_in_lists_quotes_and_plain_punctuation_pass_and_render_inline(self):
        from arete import research_synthesis as rs
        text = ("Key points:\n\n- [S1]: the limit is 10\n- [S2]: the limit is 12\n\n> [S1]: dated 2025\n\n"
                "See [S1]'s note, ([S1]), [S1](//outside.example/) and ![S2] at UTC+2:00 with **Note:** the `timeout:` "
                "key, std::vector, 50%. The token needs the read:org and repo:status scopes, s3:GetObject and "
                "nginx:alpine, a 9 to 1 read:write ratio, an M:N relationship, key:value pairs, a:hover, sha256:abc123, "
                "C:/ProgramData and app:web. Raw data: 10 rows; config file: x.")
        answer = self.synthesis("answer", text)
        self.assertEqual(rs.faults(answer, self.known), [])
        report, used = rs.assemble(answer, self.evidence(*self.known.values()), [])
        self.assertEqual(used, ["S1", "S2"])
        self.assertIn("- [S1](https://example.org/a): the limit is 10\n- [S2](https://example.net/b): the limit is 12"
                      "\n\n> [S1](https://example.org/a): dated 2025", report)
        self.assertIn("See [S1](https://example.org/a)'s note, ([S1](https://example.org/a)), "
                      "[S1](https://example.org/a)(//outside.example/) and \\![S2](https://example.net/b) at", report)
        self.assertNotRegex(report, r"(?m)^[\s>*+-]*(\[[ xX]\]\s*)?\[S\d+\]:")

    def test_source_urls_are_rebuilt_percent_encoded_without_user_details(self):
        from arete import research_synthesis as rs
        evidence = self.evidence("https://user:pw@example.org/a|[x](//outside.example/)?q=1&r=2#f",
                                 "https://en.wikipedia.org/wiki/Mercury_(planet)")
        answer = self.synthesis("answer", "| Source | Limit |\n| --- | --- |\n| [S1] | 10 |\n| [S2] | 12 |")
        report, _ = rs.assemble(answer, evidence, [])
        self.assertIn("| [S1](https://example.org/a%7C%5Bx%5D%28//outside.example/%29?q=1&amp;r=2#f) | 10 |\n"
                      "| [S2](https://en.wikipedia.org/wiki/Mercury%5F%28planet%29) | 12 |", report)
        self.assertNotIn("user", report)
        self.assertEqual(rs.written("https://example.org/a b\"'<>\\`*_\x01|()[]~"),
                         "https://example.org/a%20b%22%27%3C%3E%5C%60%2A%5F%01%7C%28%29%5B%5D~")
        self.assertEqual(rs.written("https://docs.python.org&commat;outside.example/library/?a=1&sol;b#&num;c"),
                         "https://docs.python.org&amp;commat;outside.example/library/?a=1&amp;sol;b#&amp;num;c")

    def test_source_titles_are_escaped_or_written_as_code(self):
        from arete import research_synthesis as rs
        self.assertEqual(rs.inert_text("Q&A: <b>R&D</b> *notes* | [x]"),
                         "Q\\&A\\: \\<b\\>R\\&D\\<\\/b\\> \\*notes\\* \\| \\[x\\]")
        self.assertEqual(rs.inert_text("Vendor [docs](https://outside.example/)"),
                         "`Vendor [docs](https://outside.example/)`")
        for title in ("Mail admin@outside.example", "See WWW.outside.example", "Write mailto:x", "Ask xmpp:x"):
            self.assertEqual(rs.inert_text(title), f"`{title}`")
        self.assertEqual(rs.inert_text("`a` www.x"), "`` `a` www.x ``")
        self.assertEqual(rs.inert_text("Pin nginx:alpine"), "Pin nginx\\:alpine")
        self.assertEqual(rs.inert_text("a\fb\x00 c\n"), "a b c")
        report, _ = rs.assemble(self.synthesis(), self.evidence("https://example.org/a", title="Q&A [x]"), [])
        self.assertIn("- [S1](https://example.org/a) Q\\&A \\[x\\]; published 2025-01-02", report)

    def test_research_subprocess_keeps_gh_state_out_of_the_worktree(self):
        self.assertEqual(self.out["home"], self.home)
        self.assertFalse(self.wrote_gh_state)

    def test_synthesis_needs_search(self):
        with self.assertRaisesRegex(ValueError, "synthesis needs search = true"):
            config.research_table({"planner": True, "synthesis": True})


class StagedResearchAuditTests(unittest.TestCase):
    SCRIPT = (SCRIPTS / "staged_research_audit.py").read_text()

    @classmethod
    def setUpClass(cls):
        gh_state = support.SOURCE / ".local/state/gh"
        had_gh_state = gh_state.exists()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "replay", "home")
            (root / "config/config.toml").write_text(
                '[replay]\nall = true\n\n[research]\nplanner = true\nsearch = true\nsynthesis = true\naudit = true\n\n'
                '[jev]\nenabled = false\n')
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_REPLAY_DIR=str(root / "replay"),
                              TMPDIR=str(root), HOME=str(root / "home"), PYTHONPATH=str(support.SOURCE))
            done = subprocess.run([sys.executable, "-c", cls.SCRIPT, str(root)], env=env, cwd=support.SOURCE,
                                  capture_output=True, text=True, timeout=120, check=False)
            cls.out = behavior_result(done, marker="RESULT")
        cls.wrote_gh_state = not had_gh_state and gh_state.exists()

    def names(self, which):
        return [c[0] for c in self.out[which]["calls"]]

    def test_audit_corrects_qualifies_and_gaps_claims_and_writes_the_lead_from_audited_claims(self):
        a = self.out["a"]
        self.assertTrue(a["ok"], a["error"])
        report = a["structured"]["report"]
        self.assertEqual(self.names("a").count("research A batch 1 audit"), 2)
        self.assertEqual(self.names("a").count("research A batch 2 audit"), 1)
        self.assertEqual(self.names("a").count("research A revision"), 0)
        self.assertIn("quote is not in a retained passage of S3", a["evidence"]["audit"]["calls"][0]["refused"][0])
        self.assertTrue(report.startswith("**Audit note:**"))
        lead = report[:report.index("## Supported conclusions")]
        self.assertIn("The limit was 10 requests per second as of March 2021", lead)
        self.assertIn("(https://example.org/old)", lead)
        self.assertIn("(https://example.net/new)", lead)
        self.assertIn("1 decision-critical claim did not pass the audit", lead)
        self.assertNotIn("The limit is 10 requests per second [", lead)
        conclusions = report[report.index("## Supported conclusions"):report.index("## Inference")]
        self.assertRegex(conclusions, r"\*\*Decision-critical:\*\* \*\*Corrected by the audit:\*\* .*example\.org/old"
                                      r".*example\.net/new")
        self.assertNotIn("The limit is 10 requests per second.", conclusions)
        self.assertNotIn("Throttling is fully resolved.", conclusions)
        inference = report[report.index("## Inference"):report.index("## Unresolved contradictions")]
        self.assertIn("**Qualified by the audit:** The vendor will raise the limit again.", inference)
        contradictions = report[report.index("## Unresolved contradictions"):report.index("## Gaps")]
        self.assertIn("(https://example.org/old)", contradictions)
        self.assertIn("(https://example.net/new)", contradictions)
        gaps = report[report.index("## Gaps"):report.index("## Audit")]
        self.assertIn("Throttling is fully resolved.", gaps)
        self.assertIn("unverifiable", gaps)
        audit = report[report.index("## Audit"):report.index("## Sources")]
        self.assertIn("Audit complete: 6 claims checked against their sources; 3 supported, 1 corrected, "
                      "1 qualified as inference, 1 left as gaps.", audit)
        self.assertEqual(audit.count("\n- **"), 6)
        sources = report[report.index("## Sources"):]
        self.assertIn("published 2021-03-01", sources)
        self.assertIn("published 2025-06-01", sources)
        summary = a["structured"]["summary"]
        self.assertTrue(summary.startswith("The limit was 10 requests per second as of March 2021"))
        self.assertNotIn("[", summary)
        self.assertIn(" Audit complete:", summary)
        self.assertEqual(a["evidence"]["audit"]["status"], "complete")
        self.assertEqual(len(a["evidence"]["audit"]["claims"]), 6)

    def test_auditor_fetches_only_the_hosts_of_cited_sources(self):
        audits = [c for c in self.out["a"]["calls"] if "audit" in c[0]]
        self.assertEqual(len(audits), 3)
        for _, tools, allowed in audits:
            self.assertEqual(tools, ["Read", "Glob", "Grep", "WebFetch"])
            self.assertNotIn("WebFetch", allowed)
            self.assertNotIn("WebSearch", allowed)
        self.assertEqual(sorted(a for a in audits[0][2] if a.startswith("WebFetch")),
                         ["WebFetch(domain:example.com)", "WebFetch(domain:example.net)", "WebFetch(domain:example.org)"])
        self.assertEqual([a for a in audits[2][2] if a.startswith("WebFetch")], ["WebFetch(domain:example.net)"])
        for _, tools, _ in (c for c in self.out["a"]["calls"] if "synthesis" in c[0]):
            self.assertEqual(tools, ["Read", "Glob", "Grep"])
        self.assertEqual(self.out["hosts"], {"example.org": ["S2"]})
        self.assertIn("WebSearch cannot load on research_audit", self.out["web"])

    def test_budget_limited_audit_is_incomplete_and_marks_unaudited_claims(self):
        b = self.out["b"]
        self.assertTrue(b["ok"], b["error"])
        report = b["structured"]["report"]
        self.assertEqual([n for n in self.names("b") if "audit" in n or "revision" in n], ["research B batch 1 audit"])
        self.assertIn("Audit incomplete:", report)
        self.assertIn("the research budget left no room for the audit", report)
        self.assertIn("1 of 6 claims was not audited", report)
        self.assertIn("**Not audited:** Capacity planning", report)
        self.assertTrue(report.startswith("**Audit note:**"))
        self.assertNotIn("The limit is 10 requests per second", b["structured"]["summary"])
        self.assertNotIn("Audit complete", report)
        self.assertTrue(b["structured"]["summary"].count("Audit incomplete:"))
        self.assertNotIn("Audit complete", b["structured"]["summary"])
        self.assertEqual(b["evidence"]["audit"]["status"], "incomplete")

    def test_malformed_audit_is_a_failure_not_a_pass(self):
        c = self.out["c"]
        self.assertTrue(c["ok"], c["error"])
        report = c["structured"]["report"]
        self.assertEqual([n for n in self.names("c") if "audit" in n],
                         ["research C batch 1 audit", "research C batch 1 audit", "research C batch 2 audit"])
        self.assertIn("Audit failed:", report)
        self.assertNotIn("Audit complete", report)
        self.assertEqual(report.count("**Not audited:**"), 5)
        self.assertIn("Audit failed:", c["structured"]["summary"])
        self.assertEqual(c["evidence"]["audit"]["status"], "failed")

    def test_audit_off_leaves_the_report_alone(self):
        d = self.out["d"]
        self.assertTrue(d["ok"], d["error"])
        report = d["structured"]["report"]
        self.assertFalse([n for n in self.names("d") if "audit" in n or "revision" in n])
        self.assertNotIn("## Audit", report)
        self.assertNotIn("Not audited", report)
        self.assertNotIn("audit", d["evidence"])
        self.assertIn("The limit is 10 requests per second.", report)

    def test_a_claim_only_in_the_summary_or_lead_is_not_published(self):
        e = self.out["e"]
        self.assertTrue(e["ok"], e["error"])
        report, summary = e["structured"]["report"], e["structured"]["summary"]
        self.assertEqual(e["evidence"]["audit"]["status"], "complete")
        self.assertIn("Audit complete: 2 claims checked", report)
        self.assertNotIn("99.99", report)
        self.assertNotIn("99.99", summary)
        self.assertTrue(report.startswith("**Audit note:**"))
        self.assertIn("The limit was raised in June 2025", report[:report.index("## Supported conclusions")])
        self.assertIn("The limit was raised in June 2025", summary)
        self.assertFalse([n for n in self.names("e") if "revision" in n])

    def test_audit_needs_synthesis(self):
        with self.assertRaisesRegex(ValueError, "audit needs synthesis = true"):
            config.research_table({"planner": True, "search": True, "audit": True})
        self.assertFalse(self.wrote_gh_state)


if __name__ == "__main__":
    unittest.main()
