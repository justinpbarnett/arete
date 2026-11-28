from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from arete import adw, adw_library, adwgraph, api_check, config, configcheck, paths

REPO_ROOT = Path(__file__).resolve().parent.parent

CLEAN_GRAPH_TOML = """schema = 1
name = "sample_graph"

[[trigger]]
id = "manual"
kind = "manual"

[[node]]
id = "prepare"
step = "prepare"
x = 160
y = 0

[[node]]
id = "plan"
step = "plan"
x = 320
y = 0

[[node]]
id = "checks"
step = "checks"
x = 480
y = 0

[[node]]
id = "fix"
step = "implement"
x = 480
y = 120
inputs = { round = "fix{try}", asks = "" }

[[node]]
id = "done"
finish = "last"
x = 640
y = 0

[[wire]]
from = "manual.done"
to = "prepare.in"
kind = "pass"

[[wire]]
from = "manual.task"
to = "prepare.task"
kind = "data"

[[wire]]
from = "prepare.pass"
to = "plan.in"
kind = "pass"

[[wire]]
from = "prepare.task"
to = "plan.task"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "plan.tree"
kind = "data"

[[wire]]
from = "plan.pass"
to = "checks.in"
kind = "pass"

[[wire]]
from = "prepare.tree"
to = "checks.tree"
kind = "data"

[[wire]]
from = "checks.fail"
to = "fix.in"
kind = "retry"
tries = "setting:check_repair.tries"

[[wire]]
from = "checks.feedback"
to = "fix.feedback"
kind = "data"

[[wire]]
from = "prepare.tree"
to = "fix.tree"
kind = "data"

[[wire]]
from = "plan.plan"
to = "fix.plan"
kind = "data"

[[wire]]
from = "fix.pass"
to = "checks.in"
kind = "pass"

[[wire]]
from = "checks.pass"
to = "done.in"
kind = "pass"
"""


def _run_isolated(script: str, timeout: int = 30) -> dict:
    with tempfile.TemporaryDirectory(prefix="arete-adw-509-") as td:
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
            raise AssertionError(
                f"isolated child failed (rc={proc.returncode})\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        return json.loads(line.removeprefix("RESULT="))


class AdwGraphCheckUnitTests(unittest.TestCase):
    def _graph_stage(self, body: str, config_dir: Path | None = None) -> tuple[dict, dict]:
        if config_dir is not None:
            report = configcheck.check("adw", "sample_graph", "adws/sample_graph.toml", body, config_dir=config_dir)
        else:
            with tempfile.TemporaryDirectory(prefix="arete-gchk-") as td:
                report = configcheck.check("adw", "sample_graph", "adws/sample_graph.toml", body, config_dir=Path(td))
        stage = next(st for st in report["stages"] if st["name"] == "graph")
        return report, stage

    def test_clean_graph_passes_with_zero_findings_and_outline(self):
        report, graph_stage = self._graph_stage(CLEAN_GRAPH_TOML)
        self.assertTrue(report["ok"], report)
        self.assertTrue(graph_stage["ok"])
        self.assertEqual(graph_stage["findings"], [])
        stage_names = [st["name"] for st in report["stages"]]
        self.assertEqual(stage_names, ["parse", "schema", "references", "graph", "floor", "resolve", "dry_run"])
        self.assertIn("outline", report["dry_run"])
        self.assertTrue(report["dry_run"]["outline"])

    def test_all_eight_graph_finding_codes(self):
        type_mismatch = CLEAN_GRAPH_TOML.replace(
            'from = "plan.plan"\nto = "fix.plan"\nkind = "data"',
            'from = "prepare.tree"\nto = "fix.plan"\nkind = "data"',
        )
        rep, st = self._graph_stage(type_mismatch)
        self.assertFalse(rep["ok"])
        self.assertFalse(st["ok"])
        tm = [f for f in st["findings"] if f["code"] == "type_mismatch"]
        self.assertEqual(len(tm), 1, st["findings"])
        self.assertIn("prepare.tree", tm[0]["message"])
        self.assertIn("fix.plan", tm[0]["message"])
        self.assertIn("tree", tm[0]["message"])
        self.assertIn("plan", tm[0]["message"])

        flow_to_data_src = CLEAN_GRAPH_TOML.replace(
            'from = "prepare.pass"\nto = "plan.in"\nkind = "pass"',
            'from = "prepare.tree"\nto = "plan.in"\nkind = "pass"',
        )
        rep, st = self._graph_stage(flow_to_data_src)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(f["code"] == "flow_to_data" for f in st["findings"]), st["findings"])

        data_to_flow_dst = CLEAN_GRAPH_TOML + '\n[[wire]]\nfrom = "prepare.tree"\nto = "plan.in"\nkind = "data"\n'
        rep, st = self._graph_stage(data_to_flow_dst)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(f["code"] == "flow_to_data" for f in st["findings"]), st["findings"])

        retry_no_tries = CLEAN_GRAPH_TOML.replace('tries = "setting:check_repair.tries"\n', "")
        rep, st = self._graph_stage(retry_no_tries)
        self.assertFalse(rep["ok"])
        rnt = [f for f in st["findings"] if f["code"] == "retry_no_tries"]
        self.assertEqual(len(rnt), 1, st["findings"])
        self.assertEqual(rnt[0]["node"], "checks")
        self.assertEqual(rnt[0]["pin"], "fail")

        cycle_body = CLEAN_GRAPH_TOML.replace(
            'from = "checks.fail"\nto = "fix.in"\nkind = "retry"\ntries = "setting:check_repair.tries"',
            'from = "checks.pass"\nto = "fix.in"\nkind = "pass"',
        )
        rep, st = self._graph_stage(cycle_body)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(f["code"] == "cycle" for f in st["findings"]), st["findings"])

        missing_in = CLEAN_GRAPH_TOML.replace(
            '[[wire]]\nfrom = "prepare.tree"\nto = "plan.tree"\nkind = "data"\n',
            "",
        )
        rep, st = self._graph_stage(missing_in)
        self.assertFalse(rep["ok"])
        mi = [f for f in st["findings"] if f["code"] == "missing_input"]
        self.assertEqual(len(mi), 1, st["findings"])
        self.assertEqual(mi[0]["node"], "plan")
        self.assertEqual(mi[0]["pin"], "tree")

        dead_wire = CLEAN_GRAPH_TOML.replace('to = "done.in"', 'to = "removed_node.in"')
        rep, st = self._graph_stage(dead_wire)
        self.assertFalse(rep["ok"])
        dw = [f for f in st["findings"] if f["code"] == "dead_wire"]
        self.assertTrue(dw and any(f["node"] == "removed_node" for f in dw), st["findings"])

        unreachable = CLEAN_GRAPH_TOML + '\n[[node]]\nid = "orphan"\nstep = "learn"\nx = 800\ny = 200\ninputs = { tree = "" }\n'
        rep, st = self._graph_stage(unreachable)
        self.assertFalse(rep["ok"])
        un = [f for f in st["findings"] if f["code"] == "unreachable"]
        self.assertEqual(len(un), 1, st["findings"])
        self.assertEqual(un[0]["node"], "orphan")

        no_finish = CLEAN_GRAPH_TOML.replace(
            '[[node]]\nid = "done"\nfinish = "last"\nx = 640\ny = 0\n',
            '[[node]]\nid = "done"\nfinish = "dry_run"\nwhen = "dry_run"\nx = 640\ny = 0\n',
        )
        rep, st = self._graph_stage(no_finish)
        self.assertFalse(rep["ok"])
        nf = [f for f in st["findings"] if f["code"] == "no_finish"]
        self.assertEqual(len(nf), 1, st["findings"])
        self.assertEqual(nf[0]["node"], "done")

    def test_trigger_checks_refusals_and_inactive_warnings(self):
        with tempfile.TemporaryDirectory(prefix="arete-trig-chk-") as td:
            cfg_dir = Path(td)
            (cfg_dir / "sources").mkdir()
            (cfg_dir / "sources" / "intake.toml").write_text('type = "pipeline"\n', encoding="utf-8")
            (cfg_dir / "adws").mkdir()
            (cfg_dir / "adws" / "other.toml").write_text(
                'schema = 1\nname = "other"\n\n[[trigger]]\nid = "t1"\nkind = "source"\nsource = "intake"\nrule = "shared-rule"\n\n[[steps]]\nfinish = "last"\n',
                encoding="utf-8",
            )

            missing_src = (
                'schema = 1\nname = "demo"\n\n[[trigger]]\nid = "t1"\nkind = "source"\nsource = "no-such-source"\n\n'
                '[[steps]]\nfinish = "last"\n'
            )
            rep = configcheck.check("adw", "demo", "adws/demo.toml", missing_src, config_dir=cfg_dir)
            self.assertFalse(rep["ok"])
            ref_probs = next(st for st in rep["stages"] if st["name"] == "references")["problems"]
            self.assertTrue(any("no-such-source" in p for p in ref_probs), ref_probs)

            at_rule = (
                'schema = 1\nname = "demo"\n\n[[trigger]]\nid = "t1"\nkind = "source"\nsource = "intake"\nrule = "@reserved"\n\n'
                '[[steps]]\nfinish = "last"\n'
            )
            rep = configcheck.check("adw", "demo", "adws/demo.toml", at_rule, config_dir=cfg_dir)
            self.assertFalse(rep["ok"])
            ref_probs = next(st for st in rep["stages"] if st["name"] == "references")["problems"]
            self.assertTrue(any("@reserved" in p for p in ref_probs), ref_probs)

            dup_same = (
                'schema = 1\nname = "demo"\n\n'
                '[[trigger]]\nid = "t1"\nkind = "source"\nsource = "intake"\nrule = "dup"\n\n'
                '[[trigger]]\nid = "t2"\nkind = "source"\nsource = "intake"\nrule = "dup"\n\n'
                '[[steps]]\nfinish = "last"\n'
            )
            rep = configcheck.check("adw", "demo", "adws/demo.toml", dup_same, config_dir=cfg_dir)
            self.assertFalse(rep["ok"])
            ref_probs = next(st for st in rep["stages"] if st["name"] == "references")["problems"]
            self.assertTrue(any("dup" in p for p in ref_probs), ref_probs)

            dup_cross = (
                'schema = 1\nname = "demo"\n\n'
                '[[trigger]]\nid = "t1"\nkind = "source"\nsource = "intake"\nrule = "shared-rule"\n\n'
                '[[steps]]\nfinish = "last"\n'
            )
            rep = configcheck.check("adw", "demo", "adws/demo.toml", dup_cross, config_dir=cfg_dir)
            self.assertFalse(rep["ok"])
            ref_probs = next(st for st in rep["stages"] if st["name"] == "references")["problems"]
            self.assertTrue(any("shared-rule" in p for p in ref_probs), ref_probs)

            inactive_body = (
                'schema = 1\nname = "demo"\n\n'
                '[[trigger]]\nid = "t_sched"\nkind = "schedule"\nevery = "15m"\n\n'
                '[[trigger]]\nid = "t_ev"\nkind = "event"\nevent = "feedback"\n\n'
                '[[steps]]\nfinish = "last"\n'
            )
            rep = configcheck.check("adw", "demo", "adws/demo.toml", inactive_body, config_dir=cfg_dir)
            self.assertTrue(rep["ok"], rep)
            codes = [w["code"] for w in rep["warnings"]]
            self.assertEqual(codes, ["inactive", "inactive"])
            ref_notes = next(st for st in rep["stages"] if st["name"] == "references")["notes"]
            self.assertEqual(len([n for n in ref_notes if "inactive" in n]), 2)

    def test_routes_when_source_matches_only_that_source_and_engine_default_unchanged(self):
        engine_routes_text = (paths.ENGINE_ROUTES_DIR / "adw.toml").read_text(encoding="utf-8")
        self.assertNotIn("source", engine_routes_text)

        routes_data = {
            "schema": 1,
            "rule": [
                {"id": "from-intake", "when": {"source": "intake"}, "adw": "patch"},
                {"id": "from-multi", "when": {"source": ["github", "jira"]}, "adw": "repair"},
                {"id": "default", "adw": "feature"},
            ],
        }
        self.assertEqual(adw.validate_routes(routes_data), [])
        routes = adw.parse_routes_data(routes_data, path=Path("routes/adw.toml"), level="config", raw=b"x")
        fake_cfg = config.Config()
        fake_repo = SimpleNamespace(name="demo", github="", tracker="", payer="")

        d_intake = adw._match_routes(
            fake_cfg, fake_repo, "T-1", routes, kind="feat", source="intake", newest={"status": "done"}, feedback=[], has_repair=False
        )
        self.assertEqual((d_intake.adw, d_intake.rule), ("patch", "from-intake"))

        d_jira = adw._match_routes(
            fake_cfg, fake_repo, "T-2", routes, kind="feat", source="jira", newest={"status": "done"}, feedback=[], has_repair=False
        )
        self.assertEqual((d_jira.adw, d_jira.rule), ("repair", "from-multi"))

        d_other = adw._match_routes(
            fake_cfg, fake_repo, "T-3", routes, kind="feat", source="other", newest={"status": "done"}, feedback=[], has_repair=False
        )
        self.assertEqual((d_other.adw, d_other.rule), ("feature", "default"))

        d_none = adw._match_routes(
            fake_cfg, fake_repo, "T-4", routes, kind="feat", source="", newest={"status": "done"}, feedback=[], has_repair=False
        )
        self.assertEqual((d_none.adw, d_none.rule), ("feature", "default"))

        self.assertTrue(adwgraph.check({"node": [{"id": "a", "step": "prepare", "x": 0, "y": 0}]}, adw_library.load()))


class AdwGraphCheckAndTriggersHttpTests(unittest.TestCase):
    def test_http_check_409_activate_gate_b_and_source_trigger_routing(self):
        script = f"""
import json, threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import (
    adw, api_check, api_http, codebase, config, configrepo, credential_files,
    dashboard, paths, pins, principals, record, tailnet, verify_faults,
)
from arete.store import db, runs as run_store, tasks as task_store, tokens

CLEAN_GRAPH = {CLEAN_GRAPH_TOML!r}
WRONG_TYPE = CLEAN_GRAPH.replace(
    'from = "plan.plan"\\nto = "fix.plan"\\nkind = "data"',
    'from = "prepare.tree"\\nto = "fix.plan"\\nkind = "data"',
)
SCRATCH_WITH_TRIGGER = '''schema = 1
name = "scratch_intake"

[[trigger]]
id = "t_intake"
kind = "source"
source = "intake"
label = "incoming"
every = "5m"

[[steps]]
do = "prepare"
end = true

[[steps]]
finish = "last"
'''
SCRATCH_WITHOUT_TRIGGER = '''schema = 1
name = "scratch_intake"

[[steps]]
do = "prepare"
end = true

[[steps]]
finish = "last"
'''

root = Path(paths.CONFIG_DIR).parent
(root / ".arete-owned").write_bytes(verify_faults.MARKER + b"\\n")
repo_dir = root / "demo-repo"
repo_dir.mkdir()
paths.CONFIG_FILE.write_text(
    'max_parallel = 2\\n\\n[[repo]]\\nname = "demo"\\npath = "' + str(repo_dir) + '"\\n',
    encoding="utf-8",
)
(paths.CONFIG_DIR / "sources").mkdir(parents=True, exist_ok=True)
(paths.CONFIG_DIR / "sources" / "intake.toml").write_text('type = "pipeline"\\n', encoding="utf-8")
db.migrate()
configrepo.init()
owner = principals.seed_owner(api_check.LOGIN)
tokens.init_owner()
token = credential_files.read_client(db.schema_metadata()["instance_tag"])
_, run_token = tokens.issue(owner, "run-actor", ["read", "config:draft", "config:activate"], run_id="pip-42")
codebase.load()

node = SimpleNamespace(names={{api_check.ADDRESS}})
logins = tailnet.Logins(tailnet.ScratchHost())
logins.of = lambda _: api_check.LOGIN
handler = type("CheckTrigHandler", (dashboard.handlers().open,), {{"disable_nagle_algorithm": True}})
with mock.patch.object(api_http, "log_request"), mock.patch("socket.getfqdn", return_value=api_check.ADDRESS):
    server = tailnet.Server(api_check.ADDRESS, 0, node, logins, False, handler, dashboard.Redirect)
with server:
    thread = threading.Thread(target=server.serve_forever, kwargs={{"poll_interval": 0.01}})
    thread.start()
    try:
        api = api_check.Check(server.server_port, root, [], token)
        run_api = api_check.Check(server.server_port, root, [], run_token)

        _, raw_chk_bad = api.request(
            "POST", "/api/v1/config/adw/sample_graph/check",
            body={{"body": WRONG_TYPE}}, headers={{"Idempotency-Key": "chk-bad-1"}}, status=200,
        )
        chk_bad = json.loads(raw_chk_bad)["report"]

        _, raw_bad_draft = api.request(
            "PUT", "/api/v1/config/adw/sample_graph/draft",
            body={{"body": WRONG_TYPE}}, headers={{"Idempotency-Key": "drf-bad-1"}}, status=201,
        )
        bad_draft = json.loads(raw_bad_draft)
        _, raw_act_bad = api.request(
            "POST", "/api/v1/config/adw/sample_graph/activate",
            body={{"draft": bad_draft["draft"]["id"], "base_version": 0}},
            headers={{"Idempotency-Key": "act-bad-1"}}, status=409,
        )
        act_bad = json.loads(raw_act_bad)

        _, raw_unchecked = api.request(
            "PUT", "/api/v1/config/adw/sample_graph/draft",
            body={{"body": WRONG_TYPE, "check": False}},
            headers={{"Idempotency-Key": "drf-unchk-1"}}, status=201,
        )
        unchk_id = json.loads(raw_unchecked)["draft"]["id"]
        _, raw_act_unchk = api.request(
            "POST", "/api/v1/config/adw/sample_graph/activate",
            body={{"draft": unchk_id, "base_version": 0}},
            headers={{"Idempotency-Key": "act-unchk-1"}}, status=409,
        )
        act_unchk = json.loads(raw_act_unchk)

        _, raw_run_draft = run_api.request(
            "PUT", "/api/v1/config/adw/sample_graph/draft",
            body={{"body": CLEAN_GRAPH}}, headers={{"Idempotency-Key": "run-drf-1"}}, status=201,
        )
        run_draft = json.loads(raw_run_draft)
        _, raw_run_act = run_api.request(
            "POST", "/api/v1/config/adw/sample_graph/activate",
            body={{"draft": run_draft["draft"]["id"], "base_version": 0}},
            headers={{"Idempotency-Key": "run-act-1"}}, status=403,
        )
        run_act_err = json.loads(raw_run_act)

        _, raw_ok_act = api.request(
            "POST", "/api/v1/config/adw/sample_graph/activate",
            body={{"draft": run_draft["draft"]["id"], "base_version": 0}},
            headers={{"Idempotency-Key": "own-act-1"}}, status=200,
        )
        ok_act = json.loads(raw_ok_act)

        _, raw_s1 = api.request(
            "PUT", "/api/v1/config/adw/scratch_intake/draft",
            body={{"body": SCRATCH_WITH_TRIGGER}}, headers={{"Idempotency-Key": "scr-drf-1"}}, status=201,
        )
        s1 = json.loads(raw_s1)
        _, raw_s1_act = api.request(
            "POST", "/api/v1/config/adw/scratch_intake/activate",
            body={{"draft": s1["draft"]["id"], "base_version": 0}},
            headers={{"Idempotency-Key": "scr-act-1"}}, status=200,
        )
        s1_ver = json.loads(raw_s1_act)["version"]

        listed = api.read("/api/v1/config/adw")["items"]
        listed_scratch = next(it for it in listed if it["name"] == "scratch_intake")
        got_scratch = api.read("/api/v1/config/adw/scratch_intake")["item"]
        stats_scratch = api.read("/api/v1/config/adw/scratch_intake/stats")

        _, raw_t1 = api.request(
            "POST", "/api/v1/tasks",
            body={{"repo": "demo", "title": "Intake item 1", "body": "From intake"}},
            headers={{"Idempotency-Key": "task-in-1"}}, status=200,
        )
        t1_id = json.loads(raw_t1)["task"]["id"]
        api.request(
            "POST", f"/api/v1/tasks/{{t1_id}}/refs",
            body={{"system": "intake", "key": "IN-1", "role": "origin", "connector": "intake"}},
            headers={{"Idempotency-Key": "ref-in-1"}}, status=201,
        )

        cfg = config.load()
        repo = cfg.repo("demo")
        routed1 = adw.route(cfg, repo, t1_id)
        pin1 = pins.resolve("pipeline", repo="demo", adw_name=routed1.adw, adw_version=routed1.version, adw_rule=routed1.rule, cfg=cfg)
        rid1 = record.start(
            "pipeline", tag=f"demo-{{t1_id}}", repo="demo", issue=t1_id,
            task_id=task_store.parse_task_num(t1_id), issue_key=t1_id,
            resolution=pin1, adw=pin1.adw, adw_version=pin1.adw_version, adw_rule=pin1.adw_rule,
        )
        run1 = api.read(f"/api/v1/runs/{{rid1}}")["run"]

        _, raw_s2 = api.request(
            "PUT", "/api/v1/config/adw/scratch_intake/draft",
            body={{"body": SCRATCH_WITHOUT_TRIGGER}}, headers={{"Idempotency-Key": "scr-drf-2"}}, status=201,
        )
        s2 = json.loads(raw_s2)
        api.request(
            "POST", "/api/v1/config/adw/scratch_intake/activate",
            body={{"draft": s2["draft"]["id"], "base_version": s1_ver}},
            headers={{"Idempotency-Key": "scr-act-2"}}, status=200,
        )

        _, raw_t2 = api.request(
            "POST", "/api/v1/tasks",
            body={{"repo": "demo", "title": "Intake item 2", "body": "After removing trigger"}},
            headers={{"Idempotency-Key": "task-in-2"}}, status=200,
        )
        t2_id = json.loads(raw_t2)["task"]["id"]
        api.request(
            "POST", f"/api/v1/tasks/{{t2_id}}/refs",
            body={{"system": "intake", "key": "IN-2", "role": "origin", "connector": "intake"}},
            headers={{"Idempotency-Key": "ref-in-2"}}, status=201,
        )
        routed2 = adw.route(cfg, repo, t2_id)
        pin2 = pins.resolve("pipeline", repo="demo", adw_name=routed2.adw, adw_version=routed2.version, adw_rule=routed2.rule, cfg=cfg)
        rid2 = record.start(
            "pipeline", tag=f"demo-{{t2_id}}", repo="demo", issue=t2_id,
            task_id=task_store.parse_task_num(t2_id), issue_key=t2_id,
            resolution=pin2, adw=pin2.adw, adw_version=pin2.adw_version, adw_rule=pin2.adw_rule,
        )
        run2 = api.read(f"/api/v1/runs/{{rid2}}")["run"]
    finally:
        server.shutdown()
        thread.join()

graph_stage = next(st for st in chk_bad["stages"] if st["name"] == "graph")
out = {{
    "chk_bad_ok": chk_bad["ok"],
    "chk_bad_findings": graph_stage["findings"],
    "act_bad_code": act_bad["code"],
    "act_bad_findings": act_bad.get("findings"),
    "act_unchk_code": act_unchk["code"],
    "act_unchk_findings": act_unchk.get("findings"),
    "run_draft_gate": run_draft["draft"]["gate"],
    "run_act_code": run_act_err["code"],
    "ok_act_version": ok_act["version"],
    "listed_triggers": listed_scratch.get("triggers"),
    "got_triggers": got_scratch.get("triggers"),
    "got_stats_runs7d": got_scratch.get("runs7d"),
    "stats_runs": stats_scratch.get("runs"),
    "run1_adw": run1.get("adw"),
    "run1_rule": run1.get("adw_rule"),
    "run2_adw": run2.get("adw"),
    "run2_rule": run2.get("adw_rule"),
}}
print("RESULT=" + json.dumps(out))
"""
        res = _run_isolated(script)
        self.assertFalse(res["chk_bad_ok"])
        self.assertEqual([f["code"] for f in res["chk_bad_findings"]], ["type_mismatch"])
        self.assertEqual(res["act_bad_code"], "invalid_state")
        self.assertEqual([f["code"] for f in res["act_bad_findings"]], ["type_mismatch"])
        self.assertEqual(res["act_unchk_code"], "invalid_state")
        self.assertEqual([f["code"] for f in res["act_unchk_findings"]], ["type_mismatch"])
        self.assertEqual(res["run_draft_gate"], "B")
        self.assertEqual(res["run_act_code"], "gate_refused")
        self.assertEqual(res["ok_act_version"], 1)
        self.assertEqual(len(res["listed_triggers"]), 1)
        self.assertEqual(res["listed_triggers"][0]["source"], "intake")
        self.assertEqual(res["listed_triggers"][0]["rule"], "scratch_intake-t_intake")
        self.assertEqual(res["listed_triggers"][0]["params"], {"label": "incoming", "every": "5m"})
        self.assertEqual(res["got_triggers"], res["listed_triggers"])
        self.assertEqual(res["got_stats_runs7d"], 0)
        self.assertEqual(res["stats_runs"], 0)
        self.assertEqual(res["run1_adw"], "scratch_intake")
        self.assertEqual(res["run1_rule"], "scratch_intake-t_intake")
        self.assertEqual(res["run2_adw"], "feature")
        self.assertEqual(res["run2_rule"], "default")
