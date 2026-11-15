import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import arete

from arete import paths, state, util

from tests import support
from tests.harness import bootstrap, make_folders, scratch_env

SCRIPTS = Path(__file__).resolve().parent / "scripts"


def setUpModule():
    support.isolate_live_state()


class NewUIShellTests(unittest.TestCase):
    def test_action_write_boundary(self):
        web = support.SOURCE / "arete" / "web"
        names = ("ui-actions.js", "ui-undo.js", "ui-notice.js", "ui-write.js")
        for name in names:
            source = (web / name).read_text()
            self.assertNotIn("innerHTML", source)
            self.assertNotIn("eval", source)
            if name != "ui-write.js":
                for method in re.findall(r"\bmethod\s*:\s*([^,}\n]+)", source):
                    self.assertIn(method.strip(), ('"GET"', "'GET'"))
        write = (web / "ui-write.js").read_text()
        for field in ("X-Arete", "Idempotency-Key", "base_version"):
            self.assertIn(field, write)
        for path in web.glob("ui-*.js"):
            if path.name != "ui-write.js":
                for method in re.findall(r"\bmethod\s*:\s*([^,}\n]+)", path.read_text()):
                    self.assertIn(method.strip(), ('"GET"', "'GET'"), path.name)

    def test_action_fixture_http_controls(self):
        script = (SCRIPTS / "ui_action_fixture_http.py").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "log", "worktrees")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL"),
                              ARETE_LOG_DIR=str(root / "log"), ARETE_WORKTREES_DIR=str(root / "worktrees"))
            command = [sys.executable, "-I", "-B", "-c", bootstrap(script)]
            done = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_actions_offline_behavior(self):
        script = (SCRIPTS / "ui_actions_offline.mjs").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text('{"type":"module"}')
            web = support.SOURCE / "arete" / "web"
            for path in [*web.glob("ui-*.js"), *web.glob("vendor-*.js")]:
                shutil.copyfile(path, root / path.name)
            (root / "check.mjs").write_text(script)
            done = subprocess.run(["node", str(root / "check.mjs")], cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_palette_source_contract(self):
        web = support.SOURCE / "arete" / "web"
        palette = (web / "ui-palette.js").read_text()
        for name in ("steps", "triggers", "prompts", "roles", "drop", "wires", "create"):
            self.assertIn(name, palette)
        self.assertIn("ui-fuzzy.js", palette)
        for source in (palette, (web / "ui-fuzzy.js").read_text()):
            for banned in ("innerHTML", "insertAdjacentHTML", "eval"):
                self.assertNotIn(banned, source)

    def test_feedback_dialog_source_contract(self):
        from arete import dashboard
        web = support.SOURCE / "arete" / "web"
        source = (web / "ui-feedback.js").read_text()
        _, served = dashboard.web_snapshot()
        self.assertEqual(served["/ui-feedback.js"][0], source.encode())
        self.assertTrue(served["/ui-feedback.js"][1].startswith("text/javascript"))
        self.assertEqual(served["/ui-feedback.css"][0], (web / "ui-feedback.css").read_bytes())
        for name in ("/api/capture", "/api/capture/submit", "/api/capture/evidence", "Send feedback"):
            self.assertIn(name, source)
        self.assertIsNone(re.search(r"style\s*=", source))
        self.assertNotIn("innerHTML", source)

    def test_context_carries_screen_and_keeps_the_legacy_shape(self):
        from arete import capture
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(paths, "STATE_DIR", Path(folder)), \
                mock.patch.object(paths, "DB_FILE", Path(folder) / "arete.db"), \
                mock.patch.object(util, "iso", return_value="2026-01-02T03:04:05Z"):
            state.ensure_schema()

            def stored(body):
                capture.add_context(body, "code123")
                return state.capture_draft()["context"]

            legacy = {"captured": "2026-01-02T03:04:05Z", "app": {"version": arete.__version__, "code": "code123"}, "view": "live"}
            self.assertEqual(stored({"view": "live"}), json.dumps(legacy, indent=2, ensure_ascii=False))
            with_screen = json.loads(stored({"screen": "#settings/health"}))
            self.assertEqual(with_screen["screen"], "#settings/health")
            self.assertEqual(with_screen["app"]["code"], "code123")
            self.assertNotIn("view", with_screen)
            longest = "#" + "a " * 99 + "a"
            self.assertEqual(len(longest), 200)
            self.assertEqual(json.loads(stored({"screen": longest}))["screen"], longest)
            before = state.capture_draft()["context"]
            for body in ({"screen": "settings"}, {"screen": None}, {"screen": 4}, {"screen": longest + "a"},
                         {"screen": "#floor", "view": "floor"}, {"screen": "#floor", "view": None}):
                with self.assertRaises(capture.Refused, msg=str(body)):
                    capture.add_context(body, "code123")
            self.assertEqual(state.capture_draft()["context"], before)

    def test_palette_offline_behavior(self):
        script = (SCRIPTS / "ui_palette_offline.mjs").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text('{"type":"module"}')
            for name in ("ui-palette.js", "ui-fuzzy.js", "ui-hooks.js", "ui-keys.js", "ui-router.js", "ui-menu.js", "ui-list.js", "ui-states.js", "ui-actions.js", "ui-undo.js", "ui-notice.js", "ui-write.js", "vendor-preact.js", "vendor-signals.js"):
                shutil.copyfile(support.SOURCE / "arete" / "web" / name, root / name)
            (root / "check.mjs").write_text(script)
            done = subprocess.run(["node", str(root / "check.mjs")], cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_shared_list_source_contract(self):
        web = support.SOURCE / "arete" / "web"
        source = (web / "ui-list.js").read_text()
        header = source.split("export function ListHeader", 1)[1].split("export function ListRow", 1)[0]
        for name in ("group", "filter", "search", "sort", "count", "new"):
            self.assertIn(name, header.lower())
        self.assertRegex((web / "ui-list.css").read_text(), r"--list-row:\s*28px")
        self.assertIn("`${n}/${total}`", source)
        for banned in ("innerHTML", "insertAdjacentHTML", "eval"):
            self.assertNotIn(banned, source)

    def test_shared_lists_offline_behavior(self):
        script = (SCRIPTS / "ui_shared_lists_offline.mjs").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text('{"type":"module"}')
            for name in ("ui-list.js", "ui-states.js", "ui-menu.js", "ui-keys.js", "ui-router.js", "ui-palette.js", "ui-fuzzy.js", "ui-actions.js", "ui-undo.js", "ui-notice.js", "ui-write.js", "vendor-preact.js", "vendor-signals.js"):
                shutil.copyfile(support.SOURCE / "arete" / "web" / name, root / name)
            (root / "check.mjs").write_text(script)
            done = subprocess.run(["node", str(root / "check.mjs")], cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def run_module_script(self, script, names):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text('{"type":"module"}')
            for name in (*names, "vendor-preact.js", "vendor-signals.js"):
                shutil.copyfile(support.SOURCE / "arete" / "web" / name, root / name)
            (root / "check.mjs").write_text(script)
            done = subprocess.run(["node", str(root / "check.mjs")], cwd=root, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_live_source_contract(self):
        source = (support.SOURCE / "arete" / "web" / "ui-live.js").read_text()
        self.assertEqual(source.count("/api/sidebar"), 1)
        self.assertEqual(source.count("/api/live"), 1)
        self.assertNotIn("/api/asks", source)
        self.assertIn("OFFLINE", source)
        self.assertIn("STALE", source)
        self.assertTrue("document.hidden" in source or "visibilityState" in source)
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("eval", source)

    def test_live_function_polls_and_recovers_offline(self):
        script = (SCRIPTS / "ui_live_poll_recovery.mjs").read_text()
        self.run_module_script(script, ("ui-live.js",))

    def test_state_drawings_use_glyphs_words_and_escaped_text(self):
        script = (SCRIPTS / "ui_state_drawings.mjs").read_text()
        self.run_module_script(script, ("ui-states.js",))

    def test_read_retry_key_leaves_item_retry_and_insert_alone(self):
        script = (SCRIPTS / "ui_read_retry_key.mjs").read_text()
        self.run_module_script(script, ("ui-keys.js", "ui-router.js", "ui-menu.js", "ui-palette.js", "ui-fuzzy.js", "ui-actions.js",
                                         "ui-undo.js", "ui-notice.js", "ui-write.js", "ui-list.js", "ui-states.js"))

    def test_live_poll_keeps_cursor_scroll_and_filter(self):
        script = (SCRIPTS / "ui_live_cursor_scroll.mjs").read_text()
        self.run_module_script(script, ("ui-live.js", "ui-list.js", "ui-states.js", "ui-keys.js", "ui-router.js", "ui-menu.js", "ui-palette.js",
                                         "ui-fuzzy.js", "ui-actions.js", "ui-undo.js", "ui-notice.js", "ui-write.js"))

    def test_live_fixture_serves_and_records_data_reads(self):
        script = (SCRIPTS / "ui_live_fixture_reads.py").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            command = [sys.executable, "-I", "-B", "-c", f"import sys; sys.path.insert(0, {str(support.SOURCE)!r});" + script]
            done = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_shell_and_declared_assets_are_served_with_page_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            script = (SCRIPTS / "ui_shell_assets.py").read_text()
            command = [sys.executable, "-I", "-B", "-c", bootstrap(script)]
            done = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_keyboard_files_hold_every_screen_key_and_every_replay_hook(self):
        web = support.SOURCE / "arete" / "web"
        keys, hooks = (web / "ui-keys.js").read_text(), (web / "ui-hooks.js").read_text()
        routes = {"f": "#floor", "t": "#triage", "w": "#board/open-work", "b": "#board", "r": "#run", "n": "#intake",
                  "o": "#outbox", "p": "#prompts", "a": "#adws", "v": "#insights", ",": "#settings/health"}
        for letter, route in routes.items():
            self.assertRegex(keys, rf'\["{re.escape(letter)}", "[a-z-]+", "[^"]+", "{re.escape(route)}"\]')
        for name in ("keys", "clicks", "drag", "wheel"):
            self.assertIn(f'"{name}"', hooks)
        for source in (keys, hooks):
            for banned in ("innerHTML", "insertAdjacentHTML", "eval"):
                self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()

