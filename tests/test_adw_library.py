from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from arete import adw, adw_library, api_check, configkinds, configschema, factory, flows, orchestration, paths, sources

REPO_ROOT = Path(__file__).resolve().parent.parent


def _raw_library() -> dict:
    return tomllib.loads((paths.ENGINE_ADWS_DIR / "library.toml").read_text(encoding="utf-8"))


def _run_isolated(script: str, timeout: int = 30) -> dict:
    with tempfile.TemporaryDirectory(prefix="arete-adw-lib-") as td:
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


class AdwLibraryValidationTests(unittest.TestCase):
    def test_engine_library_validates_and_covers_registries_bidirectionally(self):
        _ = orchestration
        report = adw_library.check_engine()
        self.assertEqual(report["status"], "valid", report)
        self.assertEqual(report["errors"], [])

        body = adw_library.load()
        self.assertEqual(set(body), {"portTypes", "steps", "triggers"})

        port_ids = [p["id"] for p in body["portTypes"]]
        self.assertEqual(len(port_ids), len(set(port_ids)))
        self.assertNotIn("flow", port_ids)
        self.assertIn("object", port_ids)
        self.assertIn("any", port_ids)
        for pt in body["portTypes"]:
            self.assertTrue(pt["glyph"])
            self.assertTrue(pt["word"])

        step_ids = [s["id"] for s in body["steps"]]
        self.assertEqual(set(step_ids), set(factory.STEPS))
        self.assertIn("orchestrate_ask", step_ids)
        canonical_roles = set(flows.SETTABLE.values()) & set(configkinds.ROLE_NAMES)
        for step in body["steps"]:
            self.assertEqual(step["band"], [])
            self.assertTrue(step["about"])
            self.assertTrue(set(step["outcomes"]) <= {"done", "pass", "fail", "feedback"})
            if step["kind"] == "script":
                self.assertEqual(step["prompt"], "")
                self.assertEqual(step["role"], "")
            else:
                if step["prompt"]:
                    self.assertTrue((paths.PROMPTS_DIR / step["prompt"]).is_file(), step)
            if step["role"]:
                self.assertIn(step["role"], canonical_roles)
            for pin in (*step["in"], *step["out"]):
                self.assertIn(pin["type"], port_ids)

        source_trigs = {t["id"] for t in body["triggers"] if t["kind"] == "source"}
        event_trigs = {t["id"] for t in body["triggers"] if t["kind"] == "event"}
        self.assertEqual(source_trigs, set(sources.SOURCES))
        self.assertTrue(adw.ROUTE_EVENTS <= event_trigs)
        for trig in body["triggers"]:
            for param in trig["params"]:
                self.assertTrue(param["name"])
                self.assertTrue(param["hint"])
            for pin in trig["out"]:
                self.assertIn(pin["type"], port_ids)

    def test_removing_prompt_from_scratch_copy_fails_check_naming_step_and_file(self):
        with tempfile.TemporaryDirectory(prefix="arete-prompts-scratch-") as td:
            scratch = Path(td) / "prompts"
            shutil.copytree(paths.PROMPTS_DIR, scratch)
            (scratch / "plan.md").unlink()
            report = adw_library.check_engine(prompts_dir=scratch)
            self.assertEqual(report["status"], "invalid")
            self.assertEqual(report["stage"], "schema_and_references")
            self.assertEqual(report["stage_number"], 3)
            joined = " ".join(report["errors"])
            self.assertIn("plan", joined)
            self.assertIn("plan.md", joined)
            with self.assertRaisesRegex(adw_library.LibraryError, r"plan.*plan\.md"):
                adw_library.load(prompts_dir=scratch)

    def test_stage3_rejects_unknown_step_missing_step_bad_role_bad_ports_and_flow_port(self):
        base = _raw_library()

        unknown_step = copy.deepcopy(base)
        unknown_step["step"].append({
            "id": "ghost_step",
            "kind": "script",
            "prompt": "",
            "role": "",
            "in": [],
            "out": [],
            "outcomes": ["done"],
            "band": [],
            "about": "Nonexistent step.",
        })
        errs = adw_library.validate(unknown_step)
        self.assertTrue(any("ghost_step" in e and "factory.STEPS" in e for e in errs), errs)

        missing_step = copy.deepcopy(base)
        missing_step["step"] = [s for s in missing_step["step"] if s["id"] != "orchestrate_ask"]
        errs = adw_library.validate(missing_step)
        self.assertTrue(any("orchestrate_ask" in e for e in errs), errs)

        bad_script_prompt = copy.deepcopy(base)
        next(s for s in bad_script_prompt["step"] if s["id"] == "checks")["prompt"] = "plan.md"
        errs = adw_library.validate(bad_script_prompt)
        self.assertTrue(any("checks" in e and "empty prompt" in e for e in errs), errs)

        bad_role = copy.deepcopy(base)
        next(s for s in bad_role["step"] if s["id"] == "implement")["role"] = "implement"
        errs = adw_library.validate(bad_role)
        self.assertTrue(any("implement" in e and "unknown role" in e for e in errs), errs)

        bad_step_pin = copy.deepcopy(base)
        next(s for s in bad_step_pin["step"] if s["id"] == "plan")["in"].append({"pin": "mystery", "type": "bogus"})
        errs = adw_library.validate(bad_step_pin)
        self.assertTrue(any("mystery" in e and "bogus" in e for e in errs), errs)

        bad_trig_pin = copy.deepcopy(base)
        next(t for t in bad_trig_pin["trigger"] if t["id"] == "manual")["out"].append({"pin": "bad", "type": "nope"})
        errs = adw_library.validate(bad_trig_pin)
        self.assertTrue(any("bad" in e and "nope" in e for e in errs), errs)

        flow_port = copy.deepcopy(base)
        flow_port["portType"].append({"id": "flow", "glyph": "▶", "word": "flow"})
        errs = adw_library.validate(flow_port)
        self.assertTrue(any("flow" in e for e in errs), errs)

        unused_port = copy.deepcopy(base)
        unused_port["portType"].append({"id": "orphan", "glyph": "?", "word": "orphan"})
        errs = adw_library.validate(unused_port)
        self.assertTrue(any("orphan" in e for e in errs), errs)

        missing_source = copy.deepcopy(base)
        missing_source["trigger"] = [t for t in missing_source["trigger"] if t["id"] != "closed-specs"]
        errs = adw_library.validate(missing_source)
        self.assertTrue(any("closed-specs" in e for e in errs), errs)

        missing_event = copy.deepcopy(base)
        missing_event["trigger"] = [t for t in missing_event["trigger"] if t["id"] != "feedback"]
        errs = adw_library.validate(missing_event)
        self.assertTrue(any("feedback" in e for e in errs), errs)

    def test_library_kind_is_read_only_in_schema_and_check_helper(self):
        self.assertEqual(configkinds.classify("libraries/adw.toml"), ("library", "adw"))
        with self.assertRaisesRegex(ValueError, "unavailable for writes"):
            configschema.validate("library", {})
        report = adw_library.check_config_file("libraries/adw.toml")
        self.assertEqual(report["status"], "invalid")
        self.assertEqual(report["stage_number"], 3)
        self.assertTrue(any("read only" in e for e in report["errors"]), report)


class AdwLibraryApiAndConfigRepoTests(unittest.TestCase):
    def test_config_repo_refuses_library_and_http_serves_etag_and_prompt(self):
        script = r"""
import json, threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arete import (
    adw_library, api_check, api_http, codebase, configitems, configkinds, configrepo,
    credential_files, dashboard, paths, principals, tailnet, verify_faults,
)
from arete.store import db, tokens

root = Path(paths.CONFIG_DIR).parent
(root / ".arete-owned").write_bytes(verify_faults.MARKER + b"\n")
paths.CONFIG_FILE.write_text("max_parallel = 2\n")
db.migrate()
configrepo.init()
principals.seed_owner(api_check.LOGIN)
tokens.init_owner()
token = credential_files.read_client(db.schema_metadata()["instance_tag"])
codebase.load()

commit_err = ""
try:
    configrepo.commit(
        {"libraries/adw.toml": "schema = 1\n"},
        {"libraries/adw.toml": configrepo.ABSENT},
        "hand",
        "hand",
    )
except configrepo.ConfigRepoError as error:
    commit_err = str(error)

stray = paths.CONFIG_DIR / "libraries" / "adw.toml"
stray.parent.mkdir(parents=True, exist_ok=True)
stray.write_text("schema = 1\n")
tip_before = configrepo.live_sha()
hand_commits = configrepo.commit_hand_edits()
tip_after = configrepo.live_sha()

node = SimpleNamespace(names={api_check.ADDRESS})
logins = tailnet.Logins(tailnet.ScratchHost())
logins.of = lambda _: api_check.LOGIN
handler = type("LibHandler", (dashboard.handlers().open,), {"disable_nagle_algorithm": True})
with mock.patch.object(api_http, "log_request"), mock.patch("socket.getfqdn", return_value=api_check.ADDRESS):
    server = tailnet.Server(api_check.ADDRESS, 0, node, logins, False, handler, dashboard.Redirect)
with server:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        api = api_check.Check(server.server_port, root, [], token)
        kinds = api.read("/api/v1/config")["kinds"]
        lib_kind = next((k for k in kinds if k["kind"] == "library"), None)
        listed = api.read("/api/v1/config/library")["items"]
        hdrs, raw = api.request("GET", "/api/v1/config/library/adw")
        etag = hdrs.get("ETag")
        api.request("GET", "/api/v1/config/library/adw", headers={"If-None-Match": etag}, status=304)
        lib_item = json.loads(raw)["item"]
        api.request("GET", "/api/v1/config/library/adw?level=config", status=404)
        api.request("GET", "/api/v1/config/library/adw?file=feature.toml", status=404)
        api.request("GET", "/api/v1/config/adw/library?level=engine", status=404)
        plan_step = next(s for s in lib_item["content"]["steps"] if s["id"] == "plan")
        prompt_name = Path(plan_step["prompt"]).stem
        prompt_item = api.read(f"/api/v1/config/prompt/{prompt_name}")["item"]
    finally:
        server.shutdown()
        thread.join()

out = {
    "commit_err": commit_err,
    "hand_commits": hand_commits,
    "tip_unchanged": tip_before == tip_after,
    "lib_kind": lib_kind,
    "listed_levels": [x["level"] for x in listed],
    "listed_names": [x["name"] for x in listed],
    "etag_present": bool(etag),
    "item_level": lib_item["level"],
    "item_path": lib_item["path"],
    "content_matches": lib_item["content"] == adw_library.load(),
    "prompt_level": prompt_item["level"],
    "prompt_matches": prompt_item["content"] == (paths.PROMPTS_DIR / plan_step["prompt"]).read_text(encoding="utf-8"),
}
print("RESULT=" + json.dumps(out))
"""
        res = _run_isolated(script)
        self.assertIn("read only", res["commit_err"].lower())
        self.assertEqual(res["hand_commits"], [])
        self.assertTrue(res["tip_unchanged"])
        self.assertEqual(res["lib_kind"]["schema"], "arete/config_schemas/library.json")
        self.assertEqual(res["listed_levels"], ["engine"])
        self.assertEqual(res["listed_names"], ["adw"])
        self.assertTrue(res["etag_present"])
        self.assertEqual(res["item_level"], "engine")
        self.assertEqual(res["item_path"], "arete/adws/library.toml")
        self.assertTrue(res["content_matches"])
        self.assertEqual(res["prompt_level"], "engine")
        self.assertTrue(res["prompt_matches"])
