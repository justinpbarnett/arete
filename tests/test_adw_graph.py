from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import adw, adw_library, adwgraph, api_check, configcheck, configschema, factory, flows, paths

REPO_ROOT = Path(__file__).resolve().parent.parent

FEATURE_GRAPH_TOML = """schema = 1
name = "feature"

[[node]]
id = "prepare"
step = "prepare"
name = "Prepare"
x = 160
y = 0

[[node]]
id = "plan"
step = "plan"
name = "Plan"
model = "plan"
timeout = 15
x = 320
y = 0

[[node]]
id = "implement"
step = "implement"
name = "Implement"
model = "build"
timeout = 45
x = 480
y = 0
inputs = { round = "build" }

[[node]]
id = "checks"
step = "checks"
name = "Checks"
x = 640
y = 0
inputs = { attempt = "{try}", tries = "{tries}" }

[[node]]
id = "fix_checks"
step = "implement"
name = "Fix checks"
x = 640
y = 120
inputs = { round = "fix{try}", attempt = "{try}", tries = "{tries}" }

[[node]]
id = "learn_failed"
step = "learn"
name = "Learn (failed run)"
x = 800
y = 120
inputs = { apply = false }

[[node]]
id = "fail_checks"
finish = "failed"
name = "Checks failed"
x = 960
y = 120
why = "checks still failing after {try} attempts"

[[node]]
id = "verify"
step = "verify"
name = "Verify"
x = 800
y = 0

[[node]]
id = "review"
step = "review"
name = "Review"
x = 960
y = 0
inputs = { attempt = "{try}", tries = "{tries}" }

[[node]]
id = "review_fix"
step = "implement"
name = "Review fix"
x = 960
y = 120
inputs = { round = "review_fix{prev?}" }

[[node]]
id = "review_checks"
step = "checks"
name = "Review checks"
x = 1120
y = 120
inputs = { after = "the review fixes" }

[[node]]
id = "review_check_fix"
step = "implement"
name = "Fix review checks"
x = 1120
y = 240
inputs = { round = "check_repair{outer.prev}_{try}", attempt = "{try}", tries = "{tries}" }

[[node]]
id = "fail_review_checks"
finish = "failed"
name = "Review checks failed"
x = 1280
y = 240
why = "the checks failed after the review fixes"

[[node]]
id = "learn"
step = "learn"
name = "Learn"
x = 1120
y = 0
inputs = { apply = true }

[[node]]
id = "document"
step = "document"
name = "Document"
when = "documents"
x = 1280
y = 0

[[node]]
id = "dry_run_end"
finish = "dry_run"
name = "Dry run"
when = "dry_run"
x = 1440
y = 0

[[node]]
id = "pr"
step = "pr"
name = "Open PR"
x = 1600
y = 0

[[node]]
id = "ci"
step = "ci"
name = "CI"
when = "repo.parts"
x = 1760
y = 0

[[node]]
id = "publish"
step = "publish"
name = "Publish"
x = 1920
y = 0

[[node]]
id = "done"
finish = "last"
name = "Shipped"
x = 2080
y = 0

[[wire]]
from = "prepare.pass"
to = "plan.in"
kind = "pass"

[[wire]]
from = "plan.pass"
to = "implement.in"
kind = "pass"

[[wire]]
from = "implement.pass"
to = "checks.in"
kind = "pass"

[[wire]]
from = "checks.pass"
to = "verify.in"
kind = "pass"
label = "checks pass"

[[wire]]
from = "checks.fail"
to = "fix_checks.in"
kind = "retry"
tries = "setting:check_repair.tries"
label = "fix rounds"

[[wire]]
from = "fix_checks.pass"
to = "checks.in"
kind = "pass"

[[wire]]
from = "checks.fail"
to = "learn_failed.in"
kind = "fail"
label = "still failing after the last try"

[[wire]]
from = "learn_failed.done"
to = "fail_checks.in"
kind = "pass"

[[wire]]
from = "verify.pass"
to = "review.in"
kind = "pass"

[[wire]]
from = "review.pass"
to = "learn.in"
kind = "pass"
label = "review pass"

[[wire]]
from = "review.feedback"
to = "review_fix.in"
kind = "retry"
tries = "setting:review.tries"
min = 1
label = "review fix"

[[wire]]
from = "review_fix.pass"
to = "review_checks.in"
kind = "pass"

[[wire]]
from = "review_checks.fail"
to = "review_check_fix.in"
kind = "retry"
tries = "setting:check_repair.tries"
label = "fix review checks"

[[wire]]
from = "review_check_fix.pass"
to = "review_checks.in"
kind = "pass"

[[wire]]
from = "review_checks.fail"
to = "fail_review_checks.in"
kind = "fail"
label = "checks failed after review fixes"

[[wire]]
from = "review_checks.pass"
to = "review.in"
kind = "pass"

[[wire]]
from = "learn.done"
to = "document.in"
kind = "pass"

[[wire]]
from = "document.done"
to = "dry_run_end.in"
kind = "pass"

[[wire]]
from = "dry_run_end.pass"
to = "pr.in"
kind = "pass"

[[wire]]
from = "pr.pass"
to = "ci.in"
kind = "pass"

[[wire]]
from = "ci.pass"
to = "publish.in"
kind = "pass"

[[wire]]
from = "publish.pass"
to = "done.in"
kind = "pass"

[[wire]]
from = "prepare.tree"
to = "plan.tree"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "implement.tree"
kind = "data"

[[wire]]
from = "plan.plan"
to = "implement.plan"
kind = "data"

[[wire]]
from = "checks.feedback"
to = "fix_checks.feedback"
kind = "data"

[[wire]]
from = "checks.feedback"
to = "fail_checks.detail"
kind = "data"

[[wire]]
from = "implement.diff"
to = "review.diff"
kind = "data"

[[wire]]
from = "review.feedback"
to = "review_fix.feedback"
kind = "data"

[[wire]]
from = "review_checks.feedback"
to = "review_check_fix.feedback"
kind = "data"

[[wire]]
from = "review_checks.feedback"
to = "fail_review_checks.detail"
kind = "data"

[[wire]]
from = "pr.pr"
to = "ci.pr"
kind = "data"

[[wire]]
from = "pr.pr"
to = "publish.pr"
kind = "data"
"""


def _drive_record(
    adw_obj: adw.Adw,
    outcomes_by_step: dict[str, list[factory.Outcome]],
    *,
    check_tries: int = 3,
    review_tries: int = 2,
    documents_on: bool = False,
    dry_run: bool = False,
    parts: list | None = None,
) -> tuple[list[tuple], tuple]:
    queues = {k: list(v) for k, v in outcomes_by_step.items()}
    calls: list[tuple] = []

    def rec_step(flow, step_name, **kw):
        calls.append(("step", step_name, kw))
        seq = queues.get(step_name)
        if seq:
            return seq.pop(0)
        return factory.Outcome("pass", data={"ending": "done", "step": step_name})

    def rec_finish(flow, ending, **kw):
        calls.append(("finish", ending, kw))
        return (ending, kw.get("why", ""), "")

    def rec_ended(flow, outcome):
        calls.append(("ended", outcome.result, outcome.feedback, outcome.data))
        return (outcome.result, outcome.feedback, "")

    def rec_dry_run(flow):
        calls.append(("end_dry_run",))
        return ("dry-run", "dry-run", "")

    def rec_documents(job):
        calls.append(("documents",))
        return documents_on

    def settings_of(role):
        if role == flows.CHECK_REPAIR:
            return SimpleNamespace(tries=check_tries)
        if role == "review":
            return SimpleNamespace(tries=review_tries)
        return SimpleNamespace(tries=1)

    fake_ctx = SimpleNamespace(
        ws=Path("/fake/ws"),
        run=SimpleNamespace(
            settings=settings_of,
            dry_run=dry_run,
            repo=SimpleNamespace(parts=list(parts or [])),
        ),
    )
    fake_flow = SimpleNamespace(ctx=fake_ctx)
    with (
        mock.patch.object(flows, "step", side_effect=rec_step),
        mock.patch.object(flows, "finish", side_effect=rec_finish),
        mock.patch.object(flows, "ended", side_effect=rec_ended),
        mock.patch.object(flows, "end_dry_run", side_effect=rec_dry_run),
        mock.patch.object(flows, "documents", side_effect=rec_documents),
    ):
        ret = adw.drive(adw_obj)(fake_flow)
    return calls, ret


def _run_isolated(script: str, timeout: int = 30) -> dict:
    with tempfile.TemporaryDirectory(prefix="arete-adw-graph-") as td:
        root = Path(td)
        env = {
            **api_check.isolated_env(root),
            "ARETE_TEST_FAST_SYNC": "1",
            "PYTHONPATH": str(REPO_ROOT),
        }
        proc = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(root)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT=")), "")
        if proc.returncode != 0 or not line:
            raise AssertionError(f"isolated child failed (rc={proc.returncode})\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        return json.loads(line.removeprefix("RESULT="))


class AdwGraphCompilerTests(unittest.TestCase):
    def test_feature_graph_compiles_entry_for_entry_and_drives_identically(self):
        engine_steps = tomllib.loads((paths.ENGINE_ADWS_DIR / "feature.toml").read_text(encoding="utf-8"))["steps"]
        graph_data = tomllib.loads(FEATURE_GRAPH_TOML)
        library = adw_library.load()

        configschema.validate("adw", graph_data)
        compiled = adwgraph.compile(graph_data, library)
        self.assertEqual(len(compiled), len(engine_steps))
        for idx, (got, want) in enumerate(zip(compiled, engine_steps, strict=True)):
            self.assertEqual(got, want, f"step[{idx}] mismatch")

        with tempfile.TemporaryDirectory(prefix="arete-adw-cfg-") as td:
            cfg_dir = Path(td)
            (cfg_dir / "adws").mkdir()
            (cfg_dir / "adws" / "feature.toml").write_text(FEATURE_GRAPH_TOML, encoding="utf-8")
            graph_adw = adw.load(None, "feature", config_dir=cfg_dir)
            engine_adw = adw.load(None, "feature", config_dir=cfg_dir / "empty")

        self.assertEqual(graph_adw.steps, engine_adw.steps)
        self.assertEqual(adw.outline(graph_adw), adw.outline(engine_adw))

        p = factory.Outcome("pass")

        def f(fb: str = "err") -> factory.Outcome:
            return factory.Outcome("fail", feedback=fb, data={"ending": "failed", "why": fb})

        scenarios = [
            ({}, {}),
            ({"checks": [f("e1"), f("e2"), p]}, {"check_tries": 3}),
            ({"checks": [f("e1"), f("e2"), f("e3")]}, {"check_tries": 3}),
            ({"review": [f("fix bug"), p], "checks": [p, f("broken"), p]}, {"check_tries": 3, "review_tries": 2}),
            ({}, {"documents_on": True, "parts": [SimpleNamespace(dir="", github="me/repo")]}),
            ({}, {"dry_run": True}),
        ]
        for outcomes, opts in scenarios:
            self.assertEqual(
                _drive_record(graph_adw, outcomes, **opts),
                _drive_record(engine_adw, outcomes, **opts),
            )

    def test_schema_enforces_mutual_exclusion_between_steps_and_node(self):
        step_only = tomllib.loads((paths.ENGINE_ADWS_DIR / "feature.toml").read_text(encoding="utf-8"))
        graph_only = tomllib.loads(FEATURE_GRAPH_TOML)
        configschema.validate("adw", step_only)
        configschema.validate("adw", graph_only)

        both = {**step_only, "node": graph_only["node"]}
        with self.assertRaises(ValueError):
            configschema.validate("adw", both)
        self.assertTrue(adw.validate_adw(both))

        steps_and_wire = {**step_only, "wire": graph_only["wire"]}
        with self.assertRaises(ValueError):
            configschema.validate("adw", steps_and_wire)

        neither = {"schema": 1, "name": "empty"}
        with self.assertRaises(ValueError):
            configschema.validate("adw", neither)

        tries_on_pass = copy.deepcopy(graph_only)
        tries_on_pass["wire"][0]["tries"] = "setting:check_repair.tries"
        with self.assertRaises(ValueError):
            configschema.validate("adw", tries_on_pass)

        bad_pos = copy.deepcopy(graph_only)
        bad_pos["node"][0]["x"] = 12.5
        with self.assertRaises(ValueError):
            configschema.validate("adw", bad_pos)

    def test_retry_wire_compiles_to_repeat_with_tries_for_back_edge_and_branch(self):
        library = adw_library.load()
        back_edge_graph = {
            "schema": 1,
            "name": "loopback",
            "node": [
                {"id": "prepare", "step": "prepare", "x": 0, "y": 0},
                {"id": "implement", "step": "implement", "x": 160, "y": 0, "inputs": {"round": "build"}},
                {"id": "checks", "step": "checks", "x": 320, "y": 0},
                {"id": "done", "finish": "last", "x": 480, "y": 0},
            ],
            "wire": [
                {"from": "prepare.pass", "to": "implement.in", "kind": "pass"},
                {"from": "implement.pass", "to": "checks.in", "kind": "pass"},
                {"from": "checks.fail", "to": "implement.in", "kind": "retry", "tries": "setting:check_repair.tries"},
                {"from": "checks.feedback", "to": "implement.feedback", "kind": "data"},
                {"from": "checks.pass", "to": "done.in", "kind": "pass"},
            ],
        }
        compiled = adwgraph.compile(back_edge_graph, library)
        self.assertEqual(compiled[1], {"do": "implement", "round": "build", "end": True})
        self.assertEqual(compiled[2], {
            "until": "passed",
            "tries": "setting:check_repair.tries",
            "first": {"do": "checks"},
            "repeat": [{"do": "implement", "round": "build", "feedback": "@last", "end": True}],
        })
        self.assertEqual(compiled[3], {"finish": "last"})
        self.assertEqual(adw.validate_adw(back_edge_graph), [])

    def test_unknown_library_step_fails_compile_and_check_report_names_node(self):
        bad_graph = """schema = 1
name = "custom"

[[node]]
id = "ghost_step_node"
step = "not_in_library"
x = 100
y = 200

[[node]]
id = "done"
finish = "last"
x = 260
y = 200

[[wire]]
from = "ghost_step_node.pass"
to = "done.in"
kind = "pass"
"""
        parsed = tomllib.loads(bad_graph)
        with self.assertRaisesRegex(adwgraph.GraphError, "ghost_step_node"):
            adwgraph.compile(parsed, adw_library.load())

        with tempfile.TemporaryDirectory(prefix="arete-adw-chk-") as td:
            report = configcheck.check("adw", "custom", "adws/custom.toml", bad_graph, config_dir=Path(td))
        self.assertFalse(report["ok"])
        ref_stage = next(st for st in report["stages"] if st["name"] == "references")
        self.assertFalse(ref_stage["ok"])
        self.assertTrue(any("ghost_step_node" in p for p in ref_stage["problems"]), ref_stage)

    def test_compile_errors_name_offending_node_or_wire(self):
        library = adw_library.load()
        cases = [
            (
                "missing-retry-tries",
                {
                    "node": [
                        {"id": "c", "step": "checks", "x": 0, "y": 0},
                        {"id": "f", "step": "implement", "x": 0, "y": 80, "inputs": {"round": "fix"}},
                        {"id": "d", "finish": "last", "x": 160, "y": 0},
                    ],
                    "wire": [
                        {"from": "c.fail", "to": "f.in", "kind": "retry"},
                        {"from": "f.pass", "to": "c.in", "kind": "pass"},
                        {"from": "c.pass", "to": "d.in", "kind": "pass"},
                    ],
                },
                "c.fail -> f.in",
            ),
            (
                "flow-cycle-no-retry",
                {
                    "node": [
                        {"id": "a", "step": "prepare", "x": 0, "y": 0},
                        {"id": "b", "step": "plan", "x": 100, "y": 0},
                        {"id": "c", "step": "verify", "x": 200, "y": 0},
                    ],
                    "wire": [
                        {"from": "a.pass", "to": "b.in", "kind": "pass"},
                        {"from": "b.pass", "to": "c.in", "kind": "pass"},
                        {"from": "c.pass", "to": "b.in", "kind": "pass"},
                    ],
                },
                "b",
            ),
            (
                "dead-wire-node",
                {
                    "node": [
                        {"id": "a", "step": "prepare", "x": 0, "y": 0},
                        {"id": "d", "finish": "last", "x": 100, "y": 0},
                    ],
                    "wire": [
                        {"from": "a.pass", "to": "missing_target.in", "kind": "pass"},
                    ],
                },
                "missing_target",
            ),
            (
                "retry-does-not-return",
                {
                    "node": [
                        {"id": "c", "step": "checks", "x": 0, "y": 0},
                        {"id": "f", "step": "implement", "x": 0, "y": 80, "inputs": {"round": "fix"}},
                        {"id": "d", "finish": "last", "x": 160, "y": 0},
                    ],
                    "wire": [
                        {"from": "c.fail", "to": "f.in", "kind": "retry", "tries": "setting:check_repair.tries"},
                        {"from": "c.pass", "to": "d.in", "kind": "pass"},
                    ],
                },
                "c.fail -> f.in",
            ),
        ]
        for label, graph, expected_token in cases:
            with self.subTest(case=label):
                with self.assertRaises(adwgraph.GraphError) as ctx:
                    adwgraph.compile(graph, library)
                self.assertIn(expected_token, str(ctx.exception))


CLEAN_FEATURE_GRAPH_TOML = FEATURE_GRAPH_TOML.replace(
    'name = "feature"\n',
    'name = "feature"\n\n[[trigger]]\nid = "manual"\nkind = "manual"\n',
    1,
).replace(
    'inputs = { round = "build" }',
    'inputs = { round = "build", feedback = "", asks = "" }',
    1,
).replace(
    'inputs = { round = "fix{try}", attempt = "{try}", tries = "{tries}" }',
    'inputs = { round = "fix{try}", attempt = "{try}", tries = "{tries}", asks = "" }',
    1,
).replace(
    'inputs = { round = "review_fix{prev?}" }',
    'inputs = { round = "review_fix{prev?}", asks = "" }',
    1,
).replace(
    'inputs = { round = "check_repair{outer.prev}_{try}", attempt = "{try}", tries = "{tries}" }',
    'inputs = { round = "check_repair{outer.prev}_{try}", attempt = "{try}", tries = "{tries}", asks = "" }',
    1,
) + """
[[wire]]
from = "manual.done"
to = "prepare.in"
kind = "pass"

[[wire]]
from = "manual.task"
to = "prepare.task"
kind = "data"

[[wire]]
from = "prepare.task"
to = "plan.task"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "fix_checks.tree"
kind = "data"

[[wire]]
from = "plan.plan"
to = "fix_checks.plan"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "review_fix.tree"
kind = "data"

[[wire]]
from = "plan.plan"
to = "review_fix.plan"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "review_check_fix.tree"
kind = "data"

[[wire]]
from = "plan.plan"
to = "review_check_fix.plan"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "checks.tree"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "review_checks.tree"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "learn_failed.tree"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "verify.tree"
kind = "data"

[[wire]]
from = "plan.plan"
to = "verify.plan"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "review.tree"
kind = "data"

[[wire]]
from = "plan.plan"
to = "review.plan"
kind = "data"

[[wire]]
from = "verify.report"
to = "review.report"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "learn.tree"
kind = "data"

[[wire]]
from = "prepare.task"
to = "document.task"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "document.tree"
kind = "data"

[[wire]]
from = "review.verdict"
to = "document.verdict"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "pr.tree"
kind = "data"

[[wire]]
from = "implement.diff"
to = "pr.diff"
kind = "data"

[[wire]]
from = "document.text"
to = "pr.text"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "ci.tree"
kind = "data"

[[wire]]
from = "ci.report"
to = "publish.report"
kind = "data"
"""


class AdwGraphDraftVersionsAndLayoutTests(unittest.TestCase):
    def test_draft_activate_layout_move_versions_diff_and_same_run_steps(self):
        script = f"""
import json, threading, tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import (
    adw, api_check, api_http, codebase, configrepo, credential_files,
    dashboard, paths, principals, tailnet, verify_faults,
)
from arete.store import db, tokens

GRAPH_V1 = {CLEAN_FEATURE_GRAPH_TOML!r}
GRAPH_V2 = GRAPH_V1.replace(
    'id = "plan"\\nstep = "plan"\\nname = "Plan"\\nmodel = "plan"\\ntimeout = 15\\nx = 320\\ny = 0',
    'id = "plan"\\nstep = "plan"\\nname = "Plan"\\nmodel = "plan"\\ntimeout = 15\\nx = 360\\ny = 40',
)
assert GRAPH_V1 != GRAPH_V2

root = Path(paths.CONFIG_DIR).parent
(root / ".arete-owned").write_bytes(verify_faults.MARKER + b"\\n")
paths.CONFIG_FILE.write_text("max_parallel = 2\\n", encoding="utf-8")
db.migrate()
configrepo.init()
principals.seed_owner(api_check.LOGIN)
tokens.init_owner()
token = credential_files.read_client(db.schema_metadata()["instance_tag"])
codebase.load()

node = SimpleNamespace(names={{api_check.ADDRESS}})
logins = tailnet.Logins(tailnet.ScratchHost())
logins.of = lambda _: api_check.LOGIN
handler = type("GraphHandler", (dashboard.handlers().open,), {{"disable_nagle_algorithm": True}})
with mock.patch.object(api_http, "log_request"), mock.patch("socket.getfqdn", return_value=api_check.ADDRESS):
    server = tailnet.Server(api_check.ADDRESS, 0, node, logins, False, handler, dashboard.Redirect)
with server:
    thread = threading.Thread(target=server.serve_forever, kwargs={{"poll_interval": 0.01}})
    thread.start()
    try:
        api = api_check.Check(server.server_port, root, [], token)
        _, raw_d1 = api.request(
            "PUT",
            "/api/v1/config/adw/feature/draft",
            body={{"body": GRAPH_V1, "why": "save feature graph"}},
            headers={{"Idempotency-Key": "graph-d1"}},
            status=201,
        )
        d1 = json.loads(raw_d1)
        draft1_id = d1["draft"]["id"]
        fetched_d1 = api.read(f"/api/v1/drafts/{{draft1_id}}")["draft"]
        listed_d1 = api.read("/api/v1/drafts?kind=adw&name=feature")["drafts"][0]

        _, raw_act1 = api.request(
            "POST",
            "/api/v1/config/adw/feature/activate",
            body={{"draft": draft1_id, "base_version": 0}},
            headers={{"Idempotency-Key": "graph-act1"}},
            status=200,
        )
        act1 = json.loads(raw_act1)
        live1 = api.read("/api/v1/config/adw/feature")["item"]
        loaded1 = adw.load(None, "feature")

        _, raw_d2 = api.request(
            "PUT",
            "/api/v1/config/adw/feature/draft",
            body={{"body": GRAPH_V2, "why": "move plan by 40px"}},
            headers={{"Idempotency-Key": "graph-d2"}},
            status=201,
        )
        d2 = json.loads(raw_d2)
        draft2_id = d2["draft"]["id"]
        _, raw_act2 = api.request(
            "POST",
            "/api/v1/config/adw/feature/activate",
            body={{"draft": draft2_id, "base_version": act1["version"]}},
            headers={{"Idempotency-Key": "graph-act2"}},
            status=200,
        )
        act2 = json.loads(raw_act2)
        live2 = api.read("/api/v1/config/adw/feature")["item"]
        loaded2 = adw.load(None, "feature")

        versions_res = api.read("/api/v1/config/adw/feature/versions")
        diff_res = api.read("/api/v1/config/adw/feature/diff?from=1&to=2")

        bad_body = GRAPH_V1.replace('step = "plan"', 'step = "missing_step_xyz"', 1)
        _, raw_bad = api.request(
            "POST",
            "/api/v1/config/adw/feature/check",
            body={{"body": bad_body}},
            headers={{"Idempotency-Key": "graph-bad-check"}},
            status=200,
        )
        bad_report = json.loads(raw_bad)["report"]
    finally:
        server.shutdown()
        thread.join()

v1_parsed = tomllib.loads(GRAPH_V1)
v1_coords = {{n["id"]: (n["x"], n["y"]) for n in v1_parsed["node"]}}
v1_wires = v1_parsed["wire"]
draft_parsed = tomllib.loads(fetched_d1["body"])
draft_coords = {{n["id"]: (n["x"], n["y"]) for n in draft_parsed["node"]}}
listed_coords = {{n["id"]: (n["x"], n["y"]) for n in tomllib.loads(listed_d1["body"])["node"]}}
live1_parsed = tomllib.loads(live1["content"])
live1_coords = {{n["id"]: (n["x"], n["y"]) for n in live1_parsed["node"]}}
live2_plan = next(n for n in tomllib.loads(live2["content"])["node"] if n["id"] == "plan")

diff_changed_lines = [
    ln[1:].strip()
    for ln in diff_res["diff"].splitlines()
    if (ln.startswith("+") or ln.startswith("-")) and not ln.startswith(("+++", "---"))
]

out = {{
    "d1_state": d1["draft"]["state"],
    "d1_ok": d1["report"]["ok"],
    "draft_coords_match": draft_coords == v1_coords and listed_coords == v1_coords,
    "live1_coords_match": live1_coords == v1_coords,
    "wires_match": draft_parsed["wire"] == v1_wires and live1_parsed["wire"] == v1_wires,
    "live2_plan_xy": [live2_plan["x"], live2_plan["y"]],
    "versions_count": len(versions_res["entries"]),
    "ver1": act1["version"],
    "ver2": act2["version"],
    "adw_versions_differ": loaded1.version != loaded2.version,
    "compiled_steps_equal": loaded1.steps == loaded2.steps,
    "diff_changed_lines": diff_changed_lines,
    "bad_ok": bad_report["ok"],
    "bad_problems": [p for st in bad_report["stages"] for p in st.get("problems", ())],
}}
print("RESULT=" + json.dumps(out))
"""
        res = _run_isolated(script)
        self.assertEqual(res["d1_state"], "valid")
        self.assertTrue(res["d1_ok"])
        self.assertTrue(res["draft_coords_match"])
        self.assertTrue(res["live1_coords_match"])
        self.assertTrue(res["wires_match"])
        self.assertEqual(res["live2_plan_xy"], [360, 40])
        self.assertEqual(res["versions_count"], 2)
        self.assertEqual(res["ver1"], 1)
        self.assertEqual(res["ver2"], 2)
        self.assertTrue(res["adw_versions_differ"])
        self.assertTrue(res["compiled_steps_equal"])
        self.assertEqual(
            res["diff_changed_lines"],
            ["x = 320", "y = 0", "x = 360", "y = 40"],
        )
        self.assertFalse(res["bad_ok"])
        self.assertTrue(any("plan" in p for p in res["bad_problems"]), res["bad_problems"])
