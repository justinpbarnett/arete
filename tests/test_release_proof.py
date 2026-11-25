import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from support import isolate_live_state

from arete import cli, codebase, config, paths, release_proof as proof, serve, serve_guard

isolate_live_state()


class RunningRelease(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=paths.STATE_DIR))).resolve()
        self.state = self.root / "state"
        self.checkout = self.root / "repo"
        self.checkout.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.checkout)], check=True)
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "test@example.test")
        self.previous = self.commit("good")
        self.tip = self.commit("wrong")
        self.enterContext(mock.patch.multiple(paths, PACKAGE_DIR=self.checkout / "arete", STATE_DIR=self.state,
                                               SERVER_FILE=self.state / "server.json", CONFIG_DIR=self.root / "config",
                                               LOG_DIR=self.root / "logs"))
        self.enterContext(mock.patch.object(codebase, "RELEASE", self.tip))
        self.cfg = config.Config(arete_repo="arete", repos=[config.Repo("arete", self.checkout)])
        self.enterContext(mock.patch.object(config, "load_required", return_value=self.cfg))
        server = self

        class Route(BaseHTTPRequestHandler):
            def do_GET(self):
                revision = server.git("rev-parse", "HEAD")
                self.send_response(200)
                self.send_header("Arete-Instance", "a" * 32)
                self.send_header("X-Arete-Engine", revision[:12])
                self.end_headers()
                count = 1 if revision == server.previous or server.fixed else 0
                self.wfile.write(json.dumps({"report": {"name": "fixture", "members": [{"overdue": True}],
                                                        "pending_overdue": count}}).encode())

            def log_message(self, *args):
                pass

        self.fixed = False
        http = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), Route))
        t = threading.Thread(target=http.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(http.shutdown)
        serve_guard.write(paths.SERVER_FILE, {"pid": os.getpid(), "port": http.server_port, "addresses": ["127.0.0.1"],
                                             "host": "127.0.0.1", **paths.state_and_config()})
        self.policy = {"destination": "arete-serve", "revision": self.tip, "path": "/api/v1/fixture",
                       "assertions": [{"field": "/report/pending_overdue", "count": "/report/members",
                                       "where": {"/overdue": True}}],
                       "recovery": [{"field": "/report/name", "equals": "fixture"}]}
        self.file = self.root / "policy.json"
        self.file.write_text(json.dumps(self.policy))
        args = cli.parser().parse_args(["release", "enroll", "arete", "12", "--policy-file", str(self.file)])
        self.assertEqual(args.fn(args), 0)
        self.checks = proof.on_tip(self.cfg.repos[0], "main", self.tip)
        self.receipt = serve_guard.Receipt(str(self.checkout), "main", self.previous, self.tip, "org/example", 1,
                                           starts=1, release_checks=self.checks)
        from dataclasses import asdict
        serve_guard.write(self.state / serve_guard.RECEIPT, asdict(self.receipt))
        self.engine = object.__new__(serve.Server)
        self.engine.cfg, self.engine.started_head, self.engine.with_dashboard = self.cfg, self.tip[:12], False
        self.engine.busy_with = lambda: ""
        self.engine.say = self.engine.note_once = lambda text: None

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.checkout), *args], text=True).strip()

    def commit(self, value):
        (self.checkout / "feature").write_text(value)
        self.git("add", "feature")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-qm", value)
        return self.git("rev-parse", "HEAD")

    def test_healthy_process_cannot_acknowledge_wrong_behavior_and_rechecks_safe_restore(self):
        self.engine.acknowledge_deploy(True)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.previous)
        self.assertIsNone(proof.evidence("arete", "12", self.tip))
        self.engine.started_head = self.previous[:12]
        with mock.patch.object(codebase, "RELEASE", self.previous):
            self.engine.acknowledge_deploy(True)
        self.assertEqual(serve_guard.read(self.state / serve_guard.RECEIPT)["restore_status"], "verified")

    def test_correct_behavior_proves_enrolled_revision_and_policy(self):
        self.fixed = True
        self.engine.acknowledge_deploy(True)
        found = proof.evidence("arete", "12", self.tip)
        self.assertEqual(found["status"], "verified")
        self.assertEqual(found["observed"][0]["field"], "/report/pending_overdue")
        self.assertTrue(serve_guard.read(self.state / serve_guard.RECEIPT)["healthy"])
        self.assertIsNone(proof.evidence("arete", "12", self.previous))
        self.policy["assertions"] = [{"field": "/report/name", "equals": "new"}]
        self.file.write_text(json.dumps(self.policy))
        proof.install(self.cfg.repos[0], "12", self.file)
        self.assertIsNone(proof.evidence("arete", "12", self.tip))

    def test_dirty_checkout_holds_failed_release_and_keeps_operator_edit(self):
        (self.checkout / "feature").write_text("operator edit")
        self.engine.acknowledge_deploy(True)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tip)
        self.assertEqual((self.checkout / "feature").read_text(), "operator edit")
        held = serve_guard.read(self.state / serve_guard.RECEIPT)
        self.assertEqual(held["settled"], "release_held")
        self.assertIn("tracked changes", held["restore_status"])
