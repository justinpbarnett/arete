import contextlib
from dataclasses import asdict
import functools
import http.server
import io
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.request

import arete
from arete import browser, codex, config, paths, pi, pipeline, runner, scratch, util, verify

SOURCE = Path(arete.__file__).resolve().parent.parent
LAUNCH = "(deny lsopen)(deny appleevent-send)(deny job-creation)"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
              "/Support/lsregister")
CONNECT = "import socket, sys; socket.create_connection(('127.0.0.1', int(sys.argv[1])), 3).close(); open(sys.argv[2], 'w').close()"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
GRANTS = pi.WRITABLE_ALWAYS


def setUpModule():
    folder = tempfile.TemporaryDirectory(prefix="arete-no-live-")
    unittest.addModuleCleanup(folder.cleanup)
    root = Path(folder.name).resolve()
    for patch in (mock.patch.object(scratch, "AUTHORITY", root / "authority" / "verification-authority"),
                  mock.patch.object(paths, "SERVER_FILE", root / "state" / "server.json"),
                  *(mock.patch.object(pi, name, root / "state" / getattr(pi, name).name)
                    for name in ("AGENT_DIR", "LOGIN_DIR", "KEY_DIR", "SESSIONS_DIR"))):
        patch.start()
        unittest.addModuleCleanup(patch.stop)


def attempt_for(ws: Path) -> scratch.Attempt:
    return scratch.Attempt("s", "t", "r", "verify", str(ws), str(ws / ".arete-scratch-0123456789abcdef"),
                           str(ws), {}, {})


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.ws = root / "worktrees" / "ws"
        self.ws.mkdir(parents=True)
        (root / "authority").mkdir()
        self.server = root / "server.json"
        for patch in (mock.patch.object(paths, "WORKTREES_DIR", root / "worktrees"),
                      mock.patch.object(scratch, "AUTHORITY", root / "authority" / "verification-authority"),
                      mock.patch.object(paths, "SERVER_FILE", self.server),
                      mock.patch.object(scratch, "sandboxed", return_value=False),
                      mock.patch.object(pi, "can_confine", return_value=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def profiles(self):
        return {"protected": scratch.protected_command(["true"], self.ws),
                "service": scratch.service_command(attempt_for(self.ws), "true"),
                "agent": pi.write_confinement(self.ws, [])}

    def test_the_service_profile_denies_the_browser_port_only_when_given_one(self):
        attempt = attempt_for(self.ws)
        given = scratch.service_command(attempt, "true", 45678)
        self.assertIn('(deny network-outbound (remote tcp "*:45678"))', given[given.index("-p") + 1])
        plain = scratch.service_command(attempt, "true")
        self.assertNotIn("45678", plain[plain.index("-p") + 1])
        for argv in (given, plain):
            if os.access("/usr/bin/sandbox-exec", os.X_OK):
                done = subprocess.run([*argv[:argv.index("-p") + 2], "/usr/bin/true"], capture_output=True, text=True)
                self.assertTrue(done.returncode == 0 or "sandbox_apply" in done.stderr, done.stderr)

    def test_every_profile_denies_launch_routes(self):
        for name, argv in self.profiles().items():
            with self.subTest(name):
                self.assertIn(LAUNCH, argv[argv.index("-p") + 1])

    def test_launch_keep_drops_only_launch_denials(self):
        kept = scratch.protected_command(["true"], self.ws, ("launch",))
        profile = kept[kept.index("-p") + 1]
        self.assertNotIn("lsopen", profile)
        self.assertNotIn("job-creation", profile)
        self.assertIn('(remote tcp "*:', profile)

    def test_named_sockets_are_params_and_agent_can_be_kept(self):
        env = {"SSH_AUTH_SOCK": "/private/tmp/agent.sock", "TMUX_TMPDIR": self.temp.name,
               "TMUX": "/private/tmp/tmux-1/default,1,0", "DOCKER_HOST": "unix:///private/tmp/docker-x.sock"}
        with mock.patch.dict(os.environ, env):
            argv = scratch.protected_command(["true"], self.ws)
            kept = scratch.protected_command(["true"], self.ws, ("ssh-agent",))
        wanted = ["/private/tmp/agent.sock", "/private/tmp/tmux-1/default", "/private/tmp/docker-x.sock",
                  "/private/var/run/docker.sock", f"/private/tmp/tmux-{os.getuid()}"]
        for path in wanted:
            self.assertIn(path, " ".join(argv))
        self.assertTrue(any(a.endswith("=/private/tmp/agent.sock") for a in argv))
        self.assertFalse(any(a.endswith("=/private/tmp/agent.sock") for a in kept))
        self.assertTrue(any(a.endswith("=/private/tmp/docker-x.sock") for a in kept))

    def test_service_profile_confines_unix_sockets_before_named_denials(self):
        service = self.profiles()["service"]
        profile = service[service.index("-p") + 1]
        deny, allow = profile.index('(deny network-outbound (subpath "/"))'), profile.index("(allow network-outbound")
        self.assertLess(deny, allow)
        self.assertLess(allow, profile.index('(param "SF0")'))
        protected = self.profiles()["protected"]
        self.assertNotIn('(deny network-outbound (subpath "/"))', protected[protected.index("-p") + 1])

    def test_dashboard_ports_are_denied_and_never_chosen(self):
        port = 48123
        self.server.write_text(f'{{"port": {port}}}')
        for name, argv in self.profiles().items():
            with self.subTest(name):
                self.assertIn(f'(remote tcp "*:{port}")', argv[argv.index("-p") + 1])
        self.assertIn(port, scratch.dashboard_ports())
        with mock.patch.object(socket, "socket") as make:
            make.return_value.__enter__.return_value.getsockname.side_effect = [("", port), ("", 49000)]
            self.assertEqual(verify.free_port(scratch.dashboard_ports()), 49000)
        v = config.Verify(start="serve", url=f"http://localhost:{port}/x")
        self.assertIn(str(port), verify.port_refusal(v))
        self.assertIn("live dashboard", verify.port_refusal(v))
        self.assertEqual(verify.port_refusal(config.Verify(start="serve", url="http://localhost:49001")), "")
        self.assertEqual(verify.port_refusal(config.Verify(start="", url=f"http://localhost:{port}")), "")

    def test_use_product_blocks_a_dashboard_port_before_any_start(self):
        self.server.write_text('{"port": 48124}')
        run = SimpleNamespace(dir=Path(self.temp.name), repo=None, agent=mock.Mock(), log=lambda _: None)
        v = config.Verify(start="serve", url="http://localhost:48124")
        b = SimpleNamespace(run=run, cfg=config.Config(), ws=self.ws, ticket=None,
                            layer=SimpleNamespace(verify=v), next_step=lambda _: "verify")
        with mock.patch.object(verify, "serving", side_effect=AssertionError("must not start")):
            result = pipeline.use_product(b, "verify", "verify")
        self.assertEqual(result.verdict, "blocked")
        self.assertFalse(result.finished)
        run.agent.assert_not_called()

    def test_git_keeps_the_agent_only_for_network_commands(self):
        with mock.patch.object(scratch, "protected_command", return_value=["true"]) as protect:
            util.git(["push", "origin", "x"], self.ws, check=False)
            self.assertEqual(protect.call_args.args[2], ("ssh-agent",))
            util.git(["status"], self.ws, check=False)
            self.assertEqual(protect.call_args.args[2], ())

    @unittest.skipUnless(sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK),
                         "sandbox-exec is unavailable on this platform")
    def test_every_profile_compiles(self):
        for name, argv in self.profiles().items():
            with self.subTest(name):
                done = subprocess.run([*argv[:argv.index("-p") + 2], "/usr/bin/true"],
                                      capture_output=True, text=True)
                self.assertTrue(done.returncode == 0 or "sandbox_apply" in done.stderr, done.stderr)


def archive(files: dict[str, bytes], extra=()) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo("package/" + name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        for info in extra:
            tar.addfile(info)
    return out.getvalue()


@unittest.skipUnless(sys.platform == "darwin", "the browser profile is macOS only")
class BrowserServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ws = self.root / "worktrees" / "ws"
        self.run_dir = self.root / "state" / "runs" / "r"
        (self.run_dir / "evidence").mkdir(parents=True)
        self.ws.mkdir(parents=True)
        (self.root / "authority").mkdir()
        self.node = self.root / "node"
        self.node.write_text("#!/bin/sh\n")
        self.node.chmod(0o755)
        for patch in (mock.patch.object(paths, "WORKTREES_DIR", self.root / "worktrees"),
                      mock.patch.object(paths, "STATE_DIR", self.root / "state"),
                      mock.patch.object(paths, "SERVER_FILE", self.root / "server.json"),
                      mock.patch.object(scratch, "AUTHORITY", self.root / "authority" / "verification-authority"),
                      mock.patch.object(scratch, "sandboxed", return_value=False),
                      mock.patch.object(pi, "can_confine", return_value=True),
                      mock.patch.dict(os.environ, {"TMPDIR": "/private/var/empty"}),
                      mock.patch.object(pi, "WRITABLE_ALWAYS", ("/private/tmp", "/dev")),
                      mock.patch.object(browser.shutil, "which", return_value=str(self.node))):
            patch.start()
            self.addCleanup(patch.stop)
        self.a = scratch.Attempt("a" * 64, "t", "r", "verify", str(self.ws), str(self.ws / ".arete-scratch-0123456789abcdef"),
                                 str(self.run_dir), {}, {})
        data = {"version": 2, "engine": "e", "attempt": asdict(self.a), "run": [], "evidence": [], "state": "active",
                "diagnostic": "", "archives": [], "producers": {}, "before": [], "source_mutations": False,
                "cleanup_version": 3}
        with scratch.registry(True) as (fd, key):
            scratch.store(fd, key, data)
        scratch.app_port(self.a, "http://127.0.0.1:43210")
        self.evidence = self.run_dir / "evidence"
        self.config = verify.mcp_config(self.evidence)
        self.pkg = {"@playwright/mcp": archive({"package.json": b'{"bin":{"playwright-mcp":"cli.js"}}',
                                                "cli.js": b"1"}),
                    "dep": archive({"index.js": b"2", "lib/x.js": b"3"})}
        self.served = {}
        self.publish()

    def publish(self, dependencies=None, tamper=None):
        version = verify.PLAYWRIGHT_MCP.rpartition("@")[2]
        self.served = {}
        for name, data in self.pkg.items():
            url = browser.REGISTRY + name.replace("/", "-") + ".tgz"
            meta = {"name": name, "version": version if name.startswith("@") else "1.0.0",
                    "dist": {"tarball": url, "integrity": browser.integrity_of(data)}}
            if name.startswith("@"):
                meta["dependencies"] = dependencies or {"dep": "1.0.0"}
            self.served[browser.REGISTRY + browser.quote(name, safe="@") + "/" + meta["version"]] = json.dumps(meta).encode()
            self.served[url] = (tamper or {}).get(name, data)

    def install(self):
        with mock.patch.object(browser, "fetch", side_effect=lambda url: self.served[url]):
            return browser.resolved()

    def prepare(self):
        folder = self.install()
        scratch.update_browser(self.a, words=json.loads(self.config)["mcpServers"]["playwright"]["args"][2:])
        return folder

    def build(self):
        self.prepare()
        return scratch.browser_command(self.a, json.loads(self.config)["mcpServers"]["playwright"]["args"][2:])

    def settings(self, mcp_config):
        with scratch.producing(self.a), mock.patch.object(scratch, "protected_command",
                                                          side_effect=AssertionError("no fallback")):
            return codex.mcp_settings(mcp_config, ["mcp__playwright__browser_navigate"])

    def exact(self):
        server = scratch.browser_server(self.a)
        return json.dumps({"mcpServers": {"playwright": server}}), server

    def test_the_browser_profile_denies_by_default_and_keeps_the_first_three_unchanged(self):
        argv = self.build()
        profile = argv[argv.index("-p") + 1]
        for text in ("(deny mach-lookup)", "(deny network-outbound)", 'localhost:43210', '(literal "/dev/null")',
                     "MachPortRendezvousServer", '(param "STATE")'):
            self.assertIn(text, profile + " ".join(argv))
        for text in ('"/private/tmp"', '"/private/var/folders"', "SecurityServer", "securityd"):
            self.assertNotIn(text, profile + " ".join(scratch.BROWSER_MACH))
        self.assertLess(profile.index("(deny network-outbound)"), profile.index('(param "SF0")'))
        self.assertEqual(argv[-1], "--no-sandbox")
        self.assertNotIn("--no-sandbox", verify.mcp_config(self.evidence))
        agent = pi.write_confinement(self.ws, [])
        self.assertNotIn("MACHRE", agent[agent.index("-p") + 1])
        self.assertIn('(subpath "/private/tmp")', agent[agent.index("-p") + 1])

    @unittest.skipUnless(os.access("/usr/bin/sandbox-exec", os.X_OK), "sandbox-exec is unavailable")
    def test_the_browser_profile_compiles(self):
        argv = self.build()
        done = subprocess.run([*argv[argv.index("/usr/bin/sandbox-exec"):argv.index("-p") + 2], "/usr/bin/true"],
                              capture_output=True, text=True, check=False)
        self.assertTrue(done.returncode == 0 or "sandbox_apply" in done.stderr, done.stderr)

    def test_the_browser_folder_is_private_and_the_tree_leads_its_own_group_script(self):
        argv = self.build()
        folder = self.run_dir / "browser"
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertIn(f"HOME={folder}/home", argv)
        self.assertIn(f"MAC_CHROMIUM_TMPDIR={folder}/tmp", argv)
        self.assertEqual(argv[:4][1:3], ["-I", "-c"])
        self.assertIn("browser_start", argv[3])
        self.assertEqual(argv[argv.index(str(self.node)) + 1].rpartition("/")[2], "cli.js")

    def test_the_playwright_socket_path_fits_whatever_the_run_folder_length(self):
        short = tempfile.TemporaryDirectory(dir="/private/var/tmp", prefix="r")
        self.addCleanup(short.cleanup)
        runs = Path(short.name).resolve()
        for length in (80, 88, 130, 200):
            with self.subTest(length):
                self.run_dir = runs / ("r" * (length - len(str(runs)) - len("//browser")))
                (self.run_dir / "evidence").mkdir(parents=True)
                self.a = scratch.Attempt(f"{length:064d}", "t", "r", "verify", str(self.ws),
                                         str(self.ws / ".arete-scratch-0123456789abcdef"), str(self.run_dir), {}, {})
                with scratch.registry(True) as (fd, key):
                    scratch.store(fd, key, {**scratch.load(fd, key, "a" * 64), "attempt": asdict(self.a)})
                scratch.app_port(self.a, "http://127.0.0.1:43210")
                argv = self.build()
                folder = self.run_dir / "browser"
                self.assertEqual(len(str(folder)), length)
                sockets = next(w.partition("=")[2] for w in argv if w.startswith("PLAYWRIGHT_SOCKETS_DIR="))
                socket_path = f"{sockets}/browser/browser@012345.sock"
                self.assertLess(len(socket_path), 104)
                self.assertTrue((folder / socket_path).resolve().is_relative_to(folder))
                self.assertEqual(scratch.browser_table(self.a, argv)["cwd"], str(folder))

    def test_browser_command_refuses_a_sandboxed_engine_and_a_missing_port(self):
        self.prepare()
        with mock.patch.object(scratch, "sandboxed", return_value=True), self.assertRaises(scratch.ConfinementUnavailable):
            scratch.browser_command(self.a, [])
        with mock.patch.object(pi, "can_confine", return_value=False), self.assertRaises(scratch.ConfinementUnavailable):
            scratch.browser_command(self.a, [])
        scratch.update_browser(self.a, port=0)
        with self.assertRaises(scratch.ConfinementUnavailable):
            scratch.browser_command(self.a, [])

    def test_sandboxed_engine_builds_no_command(self):
        with mock.patch.object(scratch, "sandboxed", return_value=True), self.assertRaises(scratch.ConfinementUnavailable):
            scratch.browser_command(self.a, [])

    def test_only_the_engine_built_server_is_accepted(self):
        self.prepare()
        config_text, server = self.exact()
        add = [self.evidence]
        self.assertTrue(scratch.isolation(self.a, "codex", False, add, config_text))
        self.assertIn("mcp_servers.playwright", " ".join(self.settings(config_text)))
        changed = [{**server, "args": [*server["args"], "--extra"]}, {**server, "cwd": str(self.ws)},
                   {**server, "env": {"A": "b"}}, {**server, "command": "/bin/sh"}]
        for index, table in enumerate(changed):
            with self.subTest(index):
                text = json.dumps({"mcpServers": {"playwright": table}})
                self.assertFalse(scratch.isolation(self.a, "codex", False, add, text))
                with self.assertRaises(ValueError):
                    self.settings(text)
        second = json.dumps({"mcpServers": {"playwright": server, "other": {"command": "x"}}})
        self.assertFalse(scratch.isolation(self.a, "codex", False, add, second))
        with self.assertRaises(ValueError):
            self.settings(second)

    def test_browser_config_replaces_only_the_exact_verify_config(self):
        self.install()
        built = scratch.browser_config(self.a, self.config, [self.evidence])
        self.assertEqual(json.loads(built)["mcpServers"]["playwright"], scratch.browser_server(self.a))
        self.assertTrue(scratch.isolation(self.a, "codex", False, [self.evidence], built))
        other = json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": ["-y", "evil"]}}})
        self.assertEqual(scratch.browser_config(self.a, other, [self.evidence]), other)
        self.assertEqual(scratch.browser_config(self.a, "", [self.evidence]), "")
        scratch.update_browser(self.a, loopback=False)
        self.assertEqual(scratch.browser_config(self.a, self.config, [self.evidence]), self.config)

    def answering(self, family, host):
        class Answer(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        app = type("App", (http.server.ThreadingHTTPServer,), {"address_family": family})((host, 0), Answer)
        threading.Thread(target=app.serve_forever, daemon=True).start()
        self.addCleanup(app.server_close)
        self.addCleanup(app.shutdown)
        return app.server_address[1]

    def test_the_default_localhost_verify_url_keeps_its_origin_and_engages_the_browser(self):
        self.install()
        idle = lambda *args, **kwargs: subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
        for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            with self.subTest(host):
                port = self.answering(family, host)
                foreign = socket.socket()
                self.addCleanup(foreign.close)
                if family == socket.AF_INET6:
                    foreign.bind(("127.0.0.1", port))
                    foreign.listen(1)
                scratch.update_browser(self.a, port=0, loopback=False)
                v = config.Verify(start="app", url=config.Verify.url.replace("{port}", str(port)), ready_seconds=10)
                with mock.patch.object(scratch, "service_command", side_effect=lambda a, command, port=None: command), \
                        mock.patch.object(scratch, "spawn", side_effect=idle), \
                        mock.patch.object(verify.environment, "commands", return_value={}), \
                        verify.serving(v, self.ws, self.root / "server.log", SimpleNamespace(), attempt=self.a) as url:
                    self.assertEqual(url, f"http://localhost:{port}")
                    self.assertEqual(scratch.browser_record(self.a)["port"], port)
                    self.assertIs(scratch.browser_record(self.a)["loopback"], True)
                    built = scratch.browser_config(self.a, self.config, [self.evidence])
                self.assertEqual(json.loads(built)["mcpServers"]["playwright"], scratch.browser_server(self.a))

    def launch(self, mcp_config, said=None, which="codex", allowed=None):
        picked = SimpleNamespace(candidate=SimpleNamespace(runner=which, effort="", instructions=()))
        trip = SimpleNamespace(cfg=SimpleNamespace(jev=SimpleNamespace(guard=False)), repo=None, scratch=self.a,
                               target=SimpleNamespace(remote=None), tokens=None)
        base = {"mcp_config": mcp_config, "add_dirs": [self.evidence], **({"allowed_tools": allowed} if allowed else {})}
        with mock.patch.dict(scratch.LEASES, {self.a.selector: 0}), \
                mock.patch.object(runner.environment, "passed", return_value=[]):
            return runner.launch_kwargs(base, picked, trip, ("r", "s"), False, [].append if said is None else said.append)

    DOM = "echo '<html><head></head><body></body></html>'"
    FATAL = "[1:1] FATAL: sandbox initialization failed: Operation not permitted"
    SERVER = ("import http.server, sys\n"
              "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
              "class H(http.server.BaseHTTPRequestHandler):\n"
              "    def do_POST(self):\n"
              "        self.rfile.read(int(self.headers.get('Content-Length', 0)))\n"
              "        self.send_response(200)\n"
              "        self.send_header('Content-Length', '2')\n"
              "        self.end_headers()\n"
              "        self.wfile.write(b'{}')\n"
              "    def log_message(self, *args):\n"
              "        pass\n"
              "print('fake server listening', flush=True)\n"
              "http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()\n")

    def fake_browser(self, chrome, node=None):
        self.install()
        listen = verify.free_port(scratch.dashboard_ports() | {43210})
        scratch.update_browser(self.a, listen=listen)
        (self.root / "server.py").write_text(self.SERVER)
        for path, body in ((self.root / "chrome", chrome),
                           (self.node, node or f'exec "{sys.executable}" {self.root / "server.py"} "$@"')):
            path.write_text("#!/bin/sh\n" + body + "\n")
            path.chmod(0o755)
        for patch in (mock.patch.object(scratch, "browser_profile", return_value=[]),
                      mock.patch.object(browser, "CHROME", self.root / "chrome")):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(scratch.stop_served, self.a)
        return listen

    def gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)

    def test_the_served_browser_profile_listens_on_one_port_and_the_plain_one_is_unchanged(self):
        self.prepare()
        folder = scratch.browser_folder(self.a)
        served = scratch.browser_profile(self.a, 43210, folder, 45678)
        text = served[served.index("-p") + 1]
        self.assertIn('(deny network-bind network-inbound (local tcp "*:*"))', text)
        self.assertIn('(allow network-bind network-inbound (local tcp "localhost:45678"))', text)
        self.assertNotIn("network-bind", " ".join(scratch.browser_profile(self.a, 43210, folder)))
        plain = self.build()
        self.assertFalse(any(w.startswith(("--port", "DEBUG=")) for w in plain))
        command = scratch.browser_command(self.a, ["--port", "45678"], 45678)
        self.assertIn("DEBUG=pw:browser*", command)
        self.assertEqual(command[command.index("--port") + 1], "45678")
        if os.access("/usr/bin/sandbox-exec", os.X_OK):
            done = subprocess.run([*served[:served.index("-p") + 2], "/usr/bin/true"], capture_output=True, text=True)
            self.assertTrue(done.returncode == 0 or "sandbox_apply" in done.stderr, done.stderr)

    def test_serving_picks_the_browser_port_before_the_app_starts_and_denies_it_to_the_service(self):
        port = self.answering(socket.AF_INET, "127.0.0.1")
        seen = {}

        def idle(*args, **kwargs):
            seen["at spawn"] = scratch.browser_record(self.a).get("listen")
            return subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)

        def service(a, command, browser_port=None):
            seen["denied"] = browser_port
            return command

        v = config.Verify(start="app", url=config.Verify.url.replace("{port}", str(port)), ready_seconds=10)
        with mock.patch.object(scratch, "service_command", side_effect=service), \
                mock.patch.object(scratch, "spawn", side_effect=idle), \
                mock.patch.object(verify.environment, "commands", return_value={}), \
                verify.serving(v, self.ws, self.root / "server.log", SimpleNamespace(), attempt=self.a):
            pass
        listen = seen["at spawn"]
        self.assertIsInstance(listen, int)
        self.assertEqual(seen["denied"], listen)
        self.assertNotEqual(listen, port)
        self.assertNotIn(listen, scratch.dashboard_ports())

    def test_a_claude_launch_gets_one_http_entry_and_the_server_stops_with_the_attempt(self):
        listen = self.fake_browser(self.DOM)
        first = self.launch(self.config, which="claude")
        entry = {"type": "http", "url": f"http://127.0.0.1:{listen}/mcp"}
        self.assertEqual(json.loads(first["mcp_config"]), {"mcpServers": {"playwright": entry}})
        self.assertNotIn("npx", first["mcp_config"])
        self.assertEqual(runner.described_servers(first["mcp_config"]), {"playwright": entry})
        log = (self.run_dir / "verify-browser.log").read_text()
        self.assertLess(log.index("<html"), log.index("fake server listening"))
        proc = scratch.SERVED[self.a.selector].proc
        self.assertEqual(self.launch(self.config, which="claude")["mcp_config"], first["mcp_config"])
        self.assertIs(scratch.SERVED[self.a.selector].proc, proc)
        scratch.stop_served(self.a)
        self.gone(proc.pid)
        self.assertNotIn(self.a.selector, scratch.SERVED)

    def test_a_chrome_that_exits_at_once_leaves_the_verify_without_browser_tools(self):
        counted = self.root / "chrome-runs"
        self.fake_browser(f"echo x >> {counted}; echo '{self.FATAL}' >&2; exit 1")
        ran = self.root / "node-ran"
        self.node.write_text(f"#!/bin/sh\ntouch {ran}\n")
        said = []
        rules = ["mcp__playwright__browser_navigate", "Read"]
        kw = self.launch(self.config, said, "claude", rules)
        self.assertEqual(kw["mcp_config"], "")
        self.assertEqual(kw["allowed_tools"], ["Read"])
        self.assertIn(f"> {self.FATAL}", kw["system"])
        self.assertIn(self.FATAL, (self.run_dir / "verify-browser.log").read_text())
        self.assertTrue(any(self.FATAL in line for line in said))
        self.assertFalse(ran.exists())
        self.launch(self.config, which="claude", allowed=rules)
        self.assertEqual(counted.read_text().count("x"), 1)

    def test_a_chrome_that_sleeps_past_the_limit_is_killed_and_named(self):
        pid = self.root / "chrome-pid"
        self.fake_browser(f"echo $$ > {pid}; exec sleep 30")
        with mock.patch.object(scratch, "BROWSER_PREFLIGHT_SECONDS", 1):
            kw = self.launch(self.config, which="claude")
        self.assertIn("did not dump about:blank within 1 seconds", kw["system"])
        self.gone(int(pid.read_text()))

    def test_a_server_that_exits_gives_its_error_line_and_drops_the_tools(self):
        self.fake_browser(self.DOM, "echo 'Error: listen EADDRINUSE fake' >&2; exit 3")
        kw = self.launch(self.config, which="claude", allowed=["mcp__playwright__browser_snapshot"])
        self.assertEqual(kw["mcp_config"], "")
        self.assertEqual(kw["allowed_tools"], [])
        self.assertIn("the browser server exited 3: Error: listen EADDRINUSE fake", kw["system"])

    def test_a_loopback_less_verify_starts_nothing(self):
        self.fake_browser(self.DOM)
        scratch.update_browser(self.a, loopback=False)
        with mock.patch.object(scratch, "spawn", side_effect=AssertionError("started")):
            kw = self.launch(self.config, which="claude")
        self.assertIn("not a loopback address", kw["system"])
        self.assertEqual(kw["mcp_config"], "")

    def test_a_config_that_is_not_the_verifys_own_comes_back_unchanged(self):
        other = json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": ["-y", "evil"]}}})
        with mock.patch.object(scratch, "spawn", side_effect=AssertionError("started")):
            self.assertEqual(scratch.serve_browser(self.a, other, []), (other, ""))
            self.assertEqual(scratch.serve_browser(self.a, "", []), ("", ""))

    def test_a_codex_launch_after_a_claude_one_stops_the_served_server(self):
        self.fake_browser(self.DOM)
        self.launch(self.config, which="claude")
        proc = scratch.SERVED[self.a.selector].proc
        kw = self.launch(self.config)
        self.gone(proc.pid)
        entry = json.loads(kw["mcp_config"])["mcpServers"]["playwright"]
        self.assertIn("command", entry)
        self.assertNotIn("type", entry)

    def test_a_browser_tree_an_actor_can_write_or_replace_is_refused_before_any_download(self):
        self.prepare()
        config_text, _ = self.exact()
        shared = Path(tempfile.mkdtemp(dir="/private/tmp"))
        self.addCleanup(shutil.rmtree, shared, True)
        (self.root / "linked").symlink_to(shared)
        folder = self.run_dir / "browser"
        cases = (
            ("the service temp grant holds the run folder", lambda: mock.patch.object(pi, "WRITABLE_ALWAYS", GRANTS),
             "browser folder", "service"),
            ("state under /private/tmp", lambda: mock.patch.object(paths, "STATE_DIR", shared / "state"),
             "pinned browser server", "agent"),
            ("state behind a link into /private/tmp", lambda: mock.patch.object(
                paths, "STATE_DIR", self.root / "linked" / "x" / ".." / "state"), "pinned browser server", "agent"),
            ("state inside the workspace", lambda: mock.patch.object(paths, "STATE_DIR", self.ws / "state"),
             "pinned browser server", "agent"),
            ("state inside the evidence", lambda: mock.patch.object(paths, "STATE_DIR", self.evidence / "state"),
             "pinned browser server", "agent"),
            ("state inside the browser folder", lambda: mock.patch.object(paths, "STATE_DIR", folder / "state"),
             "pinned browser server", "browser"),
            ("a temp grant inside the browser folder",
             lambda: mock.patch.dict(os.environ, {"TMPDIR": str(folder / "tmp")}), "browser folder", "agent"),
            ("state under /private/TMP", lambda: mock.patch.object(
                paths, "STATE_DIR", Path(str(shared).replace("/private/tmp", "/private/TMP")) / "state"),
             "pinned browser server", "agent"),
            ("state under /PRIVATE/tmp", lambda: mock.patch.object(
                paths, "STATE_DIR", Path(str(shared).replace("/private/tmp", "/PRIVATE/tmp")) / "state"),
             "pinned browser server", "agent"),
            ("state under the data volume firmlink", lambda: mock.patch.object(
                paths, "STATE_DIR", Path("/System/Volumes/Data" + str(shared)) / "state"),
             "pinned browser server", "agent"),
            ("a temp grant spelled in other case over the state",
             lambda: mock.patch.dict(os.environ, {"TMPDIR": str(self.root / "STATE")}), "browser folder", "agent"),
        )
        for name, layout, tree, actor in cases:
            self.reactivate()
            with self.subTest(name), layout(), mock.patch.object(
                    browser, "resolved", side_effect=AssertionError("downloaded into an exposed tree")):
                with self.assertRaisesRegex(scratch.ConfinementUnavailable, f"the {tree} .* the {actor} write grant"):
                    scratch.browser_config(self.a, self.config, [self.evidence])
                with self.assertRaises(scratch.ConfinementUnavailable):
                    scratch.browser_server(self.a)
                self.assertFalse(scratch.isolation(self.a, "codex", False, [self.evidence], config_text))
                with self.assertRaises(ValueError):
                    self.settings(config_text)
                said = []
                refused = self.launch(self.config, said)
                self.assertIn(f"browser confinement unavailable: the {tree}", refused["budget_error"])
                self.assertEqual(refused["mcp_config"], self.config)
                self.assertIn(scratch.note(None), refused["system"])
                self.assertTrue(any("trusted scratch authority unavailable" in line for line in said))
        with self.linked_run(), self.assertRaisesRegex(scratch.ConfinementUnavailable, "the browser folder .* agent"):
            scratch.browser_server(self.a)
        self.reactivate()
        self.assertTrue(scratch.isolation(self.a, "codex", False, [self.evidence], config_text))
        self.assertIn("mcp_servers.playwright", " ".join(self.settings(config_text)))

    def test_a_workspace_or_authority_under_another_spelling_of_a_temp_grant_is_not_isolated(self):
        self.assertTrue(scratch.isolation(self.a, "codex", False, [self.evidence], ""))
        shared = Path(tempfile.mkdtemp(dir="/private/tmp"))
        self.addCleanup(shutil.rmtree, shared, True)
        layouts = (
            ("workspace under a case variant TMPDIR", {"TMPDIR": str(self.root).upper()}),
            ("workspace under a firmlink TMPDIR", {"TMPDIR": "/System/Volumes/Data" + str(self.root)}),
            ("authority under a case variant TMPDIR", {"TMPDIR": str(self.root / "AUTHORITY")}),
        )
        for name, env in layouts:
            with self.subTest(name), mock.patch.dict(os.environ, env):
                self.assertFalse(scratch.isolation(self.a, "codex", False, [self.evidence], ""))
        for spelling in ("/private/TMP", "/System/Volumes/Data/private/tmp"):
            authority = Path(str(shared).replace("/private/tmp", spelling)) / "verification-authority"
            with self.subTest(spelling), mock.patch.object(scratch, "AUTHORITY", authority):
                self.assertFalse(scratch.isolation(self.a, "codex", False, [self.evidence], ""))
        for temp in ("..", "../..", "scratchtmp", ""):
            with self.subTest(f"relative TMPDIR {temp!r}"), mock.patch.dict(os.environ, {"TMPDIR": temp}):
                self.assertFalse(scratch.isolation(self.a, "codex", False, [self.evidence], ""))
                with self.assertRaisesRegex(scratch.ConfinementUnavailable, "TMPDIR"):
                    scratch.browser_exposure(self.a)

    def test_a_tmpdir_that_climbs_back_through_a_link_is_refused(self):
        self.assertTrue(scratch.isolation(self.a, "codex", False, [self.evidence], ""))
        scratch.browser_exposure(self.a, self.node)
        (self.root / "away" / "deeper").mkdir(parents=True)
        (self.root / "link").symlink_to(self.root / "away" / "deeper")
        for temp in (str(self.root / "link" / ".."), "/var/../opt", "/tmp/../usr/local", "/var/folders/../../Users"):
            with self.subTest(temp), mock.patch.dict(os.environ, {"TMPDIR": temp}):
                self.assertFalse(scratch.isolation(self.a, "codex", False, [self.evidence], ""))
                with self.assertRaisesRegex(scratch.ConfinementUnavailable, "TMPDIR .* free of '..'"):
                    scratch.browser_exposure(self.a, self.node)
                with self.assertRaisesRegex(scratch.ConfinementUnavailable, "TMPDIR"):
                    scratch.actor_grants(self.a)

    def test_an_odd_spelling_or_a_removed_cwd_never_escapes_as_a_raw_error(self):
        nowhere = (Path("/private/var/empty"),)
        spelled = Path("/.resolve/1" + str(self.ws))
        if spelled.is_dir():
            with self.assertRaises(scratch.ConfinementUnavailable):
                scratch.exposure(spelled, nowhere)
            self.assertFalse(scratch.apart(spelled, nowhere))
        gone = self.root / "gone"
        gone.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(gone)
        gone.rmdir()
        self.assertIsNone(scratch.exposure(self.ws, nowhere))
        self.assertEqual(scratch.canonical(self.ws), self.ws)
        with self.assertRaises(scratch.ConfinementUnavailable):
            scratch.exposure(Path("relative"), nowhere)
        self.assertFalse(scratch.apart(Path("relative"), nowhere))

    def reactivate(self):
        with scratch.registry() as (fd, key):
            data = scratch.load(fd, key, self.a.selector)
            data["state"] = "active"
            scratch.store(fd, key, data)

    @contextlib.contextmanager
    def linked_run(self):
        moved = Path(tempfile.mkdtemp(dir="/private/tmp")) / "r"
        self.run_dir.rename(moved)
        self.run_dir.symlink_to(moved)
        try:
            yield
        finally:
            self.run_dir.unlink()
            moved.rename(self.run_dir)
            shutil.rmtree(moved.parent)

    def test_launch_fails_instead_of_falling_back_when_the_browser_cannot_be_activated(self):
        self.install()
        accepted = self.launch(self.config)
        self.assertNotIn("budget_error", accepted)
        self.assertIn("browser_start", accepted["mcp_config"])
        scratch.update_browser(self.a, words=None)
        with mock.patch.object(scratch, "activate", return_value=()):
            failed = self.launch(self.config)
        self.assertIn("browser confinement unavailable", failed["budget_error"])
        self.assertEqual(failed["mcp_config"], self.config)
        second = json.dumps({"mcpServers": {**json.loads(self.config)["mcpServers"], "other": {"command": "x"}}})
        refused = self.launch(second)
        self.assertIn("browser confinement unavailable", refused["budget_error"])
        scratch.update_browser(self.a, loopback=False)
        ordinary = self.launch(self.config)
        self.assertNotIn("budget_error", ordinary)
        self.assertEqual(ordinary["mcp_config"], self.config)

    def test_a_failed_build_is_a_failed_result_and_never_the_protected_fallback(self):
        self.prepare()
        text, _ = self.exact()
        error = scratch.ConfinementUnavailable("pinned browser server: gone")
        with scratch.producing(self.a), mock.patch.object(scratch, "browser_server", side_effect=error), \
                mock.patch.object(codex, "home", return_value=self.root), \
                mock.patch.object(codex.claude, "run_streaming", side_effect=AssertionError("launched")):
            with mock.patch.object(scratch, "protected_command", side_effect=AssertionError("fallback")), \
                    self.assertRaises(ValueError):
                codex.mcp_settings(text, [])
            res = codex._run("p", self.ws, mcp_config=text, allowed_tools=["mcp__playwright__browser_navigate"])
        self.assertFalse(res.ok)
        self.assertIn("browser confinement unavailable", res.error)

    def test_run_turns_a_command_oserror_into_a_failed_result(self):
        with mock.patch.object(codex, "command", side_effect=PermissionError("nope")), \
                mock.patch.object(codex.claude, "run_streaming", side_effect=AssertionError("launched")):
            res = codex._run("p", self.ws)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "nope")

    def test_end_kills_the_browser_group_first_and_retains_when_a_writer_remains(self):
        scratch.update_browser(self.a, groups=[41234])
        order = mock.Mock()
        with mock.patch.object(scratch, "stop_browser", order.stop), \
                mock.patch.object(scratch, "holders", order.held):
            order.held.return_value = [{"pid": 41235, "name": "chrome", "reason": scratch.IN_GROUP}]
            kept = scratch.end(self.a, self.run_dir)[2]
        self.assertEqual([c[0] for c in order.mock_calls[:2]], ["stop", "held"])
        self.assertEqual(kept, [self.a.root])
        self.assertEqual(scratch.records()[0]["state"], "retained")

    def test_stop_browser_signals_only_confined_writers_of_its_groups(self):
        data = {"attempt": asdict(self.a), "browser": {"groups": [7001]}}
        rows = " 501 7001\n 502 7001\n 503 9999\n"
        box = mock.Mock()
        box.sandbox_check.side_effect = lambda pid, op, flag, *path: 1 if op is None else (0 if pid == 501 else 1)
        with mock.patch.object(scratch.ctypes, "CDLL", return_value=box), \
                mock.patch.object(scratch.subprocess, "run", return_value=SimpleNamespace(stdout=rows)), \
                mock.patch.object(scratch.os, "kill") as kill, mock.patch.object(scratch, "BROWSER_GRACE_SECONDS", 0):
            scratch.stop_browser(data)
        self.assertEqual({c.args[0] for c in kill.call_args_list}, {501})

    def test_browser_start_refuses_unregistered_argv_and_a_sandbox(self):
        self.prepare()
        with mock.patch.object(scratch.os, "setsid"), mock.patch.object(scratch.os, "execv") as execv, \
                mock.patch.object(scratch.os, "chdir") as chdir:
            with self.assertRaises(SystemExit):
                scratch.browser_start([self.a.selector, "/usr/bin/sandbox-exec", "-p", "x"])
            with mock.patch.object(scratch, "sandboxed", return_value=True), self.assertRaises(SystemExit):
                scratch.browser_start([self.a.selector, "/usr/bin/sandbox-exec"])
            execv.assert_not_called()
            argv = self.build()
            scratch.update_browser(self.a, argv=scratch.browser_digest(argv[5:]))
            scratch.browser_start([self.a.selector, *argv[5:]])
            execv.assert_called_once()
            chdir.assert_called_once_with(self.run_dir / "browser")
        self.assertEqual(scratch.records()[0]["browser"]["groups"], [os.getpid()])

    def relink(self, path):
        moved = self.root / f"moved-{path.name}"
        shutil.rmtree(moved, ignore_errors=True)
        path.rename(moved)
        path.symlink_to(moved)

    def test_a_tampered_tarball_tree_or_pin_is_refused(self):
        folder = self.install()
        self.assertEqual(browser.checked()[0], str(self.node))
        tarball = next((folder / "tarballs").iterdir())
        installed = folder / "node_modules" / "dep" / "index.js"
        pin = scratch.AUTHORITY / browser.PIN
        cases = {
            "tarball": lambda: tarball.write_bytes(tarball.read_bytes() + b"x"),
            "tree": lambda: installed.write_bytes(b"changed"),
            "extra file": lambda: (installed.parent / "new.js").write_bytes(b"1"),
            "link": lambda: (installed.parent / "link").symlink_to("/etc/hosts"),
            "linked node_modules": lambda: self.relink(folder / "node_modules"),
            "linked tarballs": lambda: self.relink(folder / "tarballs"),
            "pin": lambda: pin.write_bytes(pin.read_bytes().replace(b"1.0.0", b"1.0.1")),
        }
        for name, damage in cases.items():
            with self.subTest(name):
                shutil.rmtree(folder)
                self.install()
                damage()
                with self.assertRaises(scratch.ConfinementUnavailable):
                    browser.checked()

    def test_a_node_any_actor_can_write_is_refused(self):
        self.prepare()
        temp, service = self.root / "agent-temp", self.root / "service"
        layouts = {"worktree": self.ws / "node", "agent temp grant": temp / "node",
                   "agent temp grant spelled in capitals": Path(str(temp / "node").upper()),
                   "service grant": service / "node", "browser folder": self.run_dir / "browser" / "node"}
        for name, planted in layouts.items():
            with self.subTest(name):
                real = scratch.canonical(planted)
                real.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                real.write_text("#!/bin/sh\n")
                real.chmod(0o755)
                with mock.patch.object(browser.shutil, "which", return_value=str(planted)), \
                        mock.patch.dict(os.environ, {"TMPDIR": str(temp)}), \
                        mock.patch.object(pi, "WRITABLE_ALWAYS", ("/private/tmp", "/dev", str(service))), \
                        self.assertRaisesRegex(scratch.ConfinementUnavailable, "node"):
                    scratch.browser_command(self.a, json.loads(self.config)["mcpServers"]["playwright"]["args"][2:])
                if name == "worktree":
                    with mock.patch.object(browser.shutil, "which", return_value=str(planted)), \
                            self.assertRaises(scratch.ConfinementUnavailable):
                        browser.checked()

    def test_the_registry_is_not_trusted_blindly(self):
        for name, kwargs in (("range", {"dependencies": {"dep": "^1.0.0"}}),
                             ("tampered", {"tamper": {"dep": archive({"index.js": b"evil"})}})):
            with self.subTest(name):
                self.publish(**kwargs)
                shutil.rmtree(browser.server_folder(), ignore_errors=True)
                with self.assertRaises(scratch.ConfinementUnavailable):
                    self.install()
                self.assertFalse(browser.server_folder().exists())
        link = tarfile.TarInfo("package/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/hosts"
        self.pkg["dep"] = archive({"index.js": b"2"}, [link])
        self.publish()
        with self.assertRaises(scratch.ConfinementUnavailable):
            self.install()
        self.pkg["dep"] = archive({"index.js": b"2"})
        self.publish()
        self.assertTrue(str(self.install()).endswith("0.0.75"))
        self.assertEqual(stat.S_IMODE(browser.server_folder().stat().st_mode), 0o700)


@unittest.skipUnless(paths.SEARCH_ONLY and os.getuid() != 0, "search-only folders cannot be checked here")
class WalkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.closed = self.root / "worktrees"
        self.scratch_root = self.closed / "ws" / ".arete-scratch-0123456789abcdef"
        for name in ("config", "state"):
            (self.scratch_root / name).mkdir(parents=True)
        self.scratch_root.chmod(0o700)
        self.closed.chmod(0o100)
        self.addCleanup(self.closed.chmod, 0o700)

    def test_the_walk_passes_a_folder_it_may_search_but_not_list(self):
        with self.assertRaises(PermissionError):
            os.listdir(self.closed)
        with paths.directory_fd(self.scratch_root) as fd:
            self.assertEqual(sorted(os.listdir(fd)), ["config", "state"])
        (self.root / "link").symlink_to(self.closed)
        with self.assertRaises(OSError):
            with paths.directory_fd(self.root / "link" / "ws"):
                pass

    def test_isolated_state_opens_below_a_folder_it_cannot_list(self):
        state = self.scratch_root / "state"
        env = {**os.environ, "ARETE_CONFIG_DIR": str(self.scratch_root / "config"), "ARETE_STATE_DIR": str(state),
               "ARETE_LOG_DIR": str(state / "logs"), "ARETE_WORKTREES_DIR": str(state / "worktrees"),
               scratch.ENV_NAME: str(self.scratch_root)}
        boot = f"import sys; sys.path.insert(0, {str(SOURCE)!r}); from arete import state; state.ensure_schema()"
        done = subprocess.run([sys.executable, "-I", "-c", boot], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertTrue((state / "arete.db").exists())
        self.assertTrue((state / "worktrees").is_dir())


@unittest.skipUnless(sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK),
                     "sandbox-exec is unavailable on this platform")
class EscapeTests(unittest.TestCase):
    def setUp(self):
        if scratch.sandboxed():
            self.skipTest("the gate runs under inherited confinement")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ws = self.root / "worktrees" / "ws"
        self.marks = self.ws / "marks"
        self.marks.mkdir(parents=True)
        self.secret = self.root / "authority" / "verification-authority" / "signing.dat"
        self.secret.parent.mkdir(parents=True)
        self.secret.write_text("fixture-secret")
        self.cleanups = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.other = socket.socket()
        self.other.bind(("127.0.0.1", 0))
        self.other.listen()
        (self.root / "server.json").write_text(f'{{"port": {self.port}}}')
        for patch in (mock.patch.object(paths, "WORKTREES_DIR", self.root / "worktrees"),
                      mock.patch.object(scratch, "AUTHORITY", self.secret.parent),
                      mock.patch.object(paths, "STATE_DIR", self.root / "state"),
                      mock.patch.object(paths, "SERVER_FILE", self.root / "server.json")):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.finish)

    def finish(self):
        for step in reversed(self.cleanups):
            step()
        self.listener.close()
        self.other.close()

    def profiles(self):
        attempt = attempt_for(self.ws)
        return {"protected": lambda cmd: scratch.protected_command(cmd, self.ws),
                "service": lambda cmd: scratch.service_command(attempt, shlex.join(cmd)),
                "agent": lambda cmd: pi.write_confinement(self.ws, []) + list(cmd)}

    def browser_attempt(self):
        run_dir = paths.STATE_DIR / "runs" / "r"
        (run_dir / "evidence").mkdir(parents=True, exist_ok=True)
        folder = run_dir / "browser"
        folder.mkdir(mode=0o700, exist_ok=True)
        attempt = scratch.Attempt("s", "t", "r", "verify", str(self.ws), str(self.ws / ".arete-scratch-0123456789abcdef"),
                                  str(run_dir), {}, {})
        return attempt, folder

    def browser_profile(self, port=None):
        attempt, folder = self.browser_attempt()
        port = port or self.other.getsockname()[1]
        return lambda cmd: scratch.browser_profile(attempt, port, folder) + list(cmd)

    def confined(self):
        return {**self.profiles(), "browser": self.browser_profile()}

    def appears(self, marker, wait):
        deadline = time.monotonic() + wait
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.2)
        return marker.exists()

    def outside_and_inside(self, command, marker, wait=15, env=None):
        full = {**os.environ, **(env or {})}
        fresh = command if callable(command) else lambda: command
        subprocess.run(fresh(), cwd=self.ws, env=full, capture_output=True, timeout=60)
        self.assertTrue(self.appears(marker, wait), "the action must succeed outside the profile")
        marker.unlink()
        for name, build in self.confined().items():
            with self.subTest(name):
                try:
                    subprocess.run(build(fresh()), cwd=self.ws, env=full, capture_output=True, timeout=30)
                except subprocess.TimeoutExpired:
                    pass
                time.sleep(2)
                self.assertFalse(marker.exists(), f"{name} profile let the action out")
                marker.unlink(missing_ok=True)

    def payload(self, marker):
        return f"cp {shlex.quote(str(self.secret))} {shlex.quote(str(marker))}"

    def build_app(self, name, marker):
        app = self.ws / f"{name}.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        (app / "Contents/Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
            "<key>CFBundleExecutable</key><string>run</string><key>CFBundleIdentifier</key>"
            f"<string>dev.arete.check.{name}</string><key>CFBundlePackageType</key><string>APPL</string>"
            "<key>LSUIElement</key><true/></dict></plist>")
        run = app / "Contents/MacOS/run"
        run.write_text(f"#!/bin/sh\n{self.payload(marker)}\n")
        run.chmod(0o755)
        self.register(app)
        return app

    def register(self, app):
        self.cleanups.append(lambda: shutil.rmtree(app, ignore_errors=True))
        self.cleanups.append(lambda: subprocess.run([LSREGISTER, "-u", str(app)], capture_output=True))

    def test_open_of_a_built_bundle_fails_inside(self):
        marker, token = self.marks / "open", os.urandom(4).hex()
        app = self.ws / f"Probe{token}.app"
        self.register(app)
        plist = ('<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>CFBundleExecutable</key>'
                 f"<string>run</string><key>CFBundleIdentifier</key><string>dev.arete.check.{token}</string>"
                 "<key>CFBundlePackageType</key><string>APPL</string><key>LSUIElement</key><true/></dict></plist>")
        run = f"{app}/Contents/MacOS/run"
        script = (f"rm -rf {app} && mkdir -p {app}/Contents/MacOS && printf '%s' {shlex.quote(plist)} > "
                  f"{app}/Contents/Info.plist && printf '#!/bin/sh\\n%s\\n' {shlex.quote(self.payload(marker))} > {run} "
                  f"&& chmod +x {run} && open {app}")
        self.outside_and_inside(["/bin/sh", "-c", script], marker)

    def test_open_a_with_arguments_fails_inside(self):
        marker = self.marks / "opena"
        app = self.build_app(f"Probe{os.urandom(4).hex()}", marker)
        self.outside_and_inside(["open", "-n", "-a", str(app), "--args", "x"], marker)

    def test_osascript_fails_inside(self):
        marker = self.marks / "osa"
        app = self.build_app(f"Probe{os.urandom(4).hex()}", marker)
        self.outside_and_inside(["osascript", "-e", f'tell application "{app}" to launch'], marker)

    def test_launchctl_submit_fails_inside(self):
        marker = self.marks / "submit"

        def submit():
            label = f"dev.arete.check.{os.urandom(4).hex()}"
            self.cleanups.append(lambda: subprocess.run(["launchctl", "remove", label], capture_output=True))
            return ["launchctl", "submit", "-l", label, "--", "/bin/sh", "-c", self.payload(marker)]

        self.outside_and_inside(submit, marker)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
    def test_tmux_client_fails_inside(self):
        marker = self.marks / "tmux"
        tmpdir = self.root / "t"
        tmpdir.mkdir()
        env = {"TMUX_TMPDIR": str(tmpdir)}
        clean = {k: v for k, v in {**os.environ, **env}.items() if k != "TMUX"}
        subprocess.run(["tmux", "-L", "c", "-f", "/dev/null", "new-session", "-d", "sleep 600"], env=clean,
                       check=True, capture_output=True)
        self.cleanups.append(lambda: subprocess.run(["tmux", "-L", "c", "kill-server"], env=clean,
                                                    capture_output=True))
        with mock.patch.dict(os.environ, env):
            os.environ.pop("TMUX", None)
            self.outside_and_inside(["tmux", "-L", "c", "run-shell", self.payload(marker)], marker, env=env)

    def test_tcp_to_the_dashboard_port_fails_inside(self):
        marker, control = self.marks / "tcp", self.marks / "tcp-control"
        self.outside_and_inside([sys.executable, "-c", CONNECT, str(self.port), str(marker)], marker)
        for name, build in self.profiles().items():
            with self.subTest(f"{name} control"):
                subprocess.run(build([sys.executable, "-c", CONNECT, str(self.other.getsockname()[1]),
                                      str(control)]), cwd=self.ws, capture_output=True, timeout=30)
                self.assertTrue(control.exists(), f"{name} profile blocks a port that is not the dashboard's")
                control.unlink()

    def test_the_served_browser_profile_listens_on_its_one_port_only(self):
        attempt, folder = self.browser_attempt()
        listen, wrong = verify.free_port(), verify.free_port()
        profile = scratch.browser_profile(attempt, self.other.getsockname()[1], folder, listen)
        server = subprocess.Popen([*profile, "/usr/bin/nc", "-l", "127.0.0.1", str(listen)], cwd=folder,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        self.cleanups.append(lambda: (server.kill(), server.wait()))
        deadline, connected = time.monotonic() + 10, False
        while not connected and time.monotonic() < deadline and server.poll() is None:
            try:
                socket.create_connection(("127.0.0.1", listen), 1).close()
                connected = True
            except OSError:
                time.sleep(.2)
        self.assertTrue(connected, "the profile must let the browser server listen on its port")
        other = subprocess.run([*profile, "/usr/bin/nc", "-l", "127.0.0.1", str(wrong)], cwd=folder,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        self.assertNotEqual(other.returncode, 0)

    def test_tcp_to_the_browser_port_fails_inside_the_service_profile(self):
        attempt = attempt_for(self.ws)
        port = self.other.getsockname()[1]
        marker = self.marks / "browser"
        command = shlex.join([sys.executable, "-c", CONNECT, str(port), str(marker)])
        subprocess.run(scratch.service_command(attempt, command), cwd=self.ws, capture_output=True, timeout=30)
        self.assertTrue(marker.exists(), "without the denial the connection must succeed")
        marker.unlink()
        subprocess.run(scratch.service_command(attempt, command, port), cwd=self.ws, capture_output=True, timeout=30)
        self.assertFalse(marker.exists())

    def test_authority_reads_fail_inside(self):
        resolved = self.secret.resolve()
        (self.ws / "ok").write_text("ok")
        for name, script in (("direct", f"cat {resolved}"),
                             ("data volume", f"cat /System/Volumes/Data{resolved}"),
                             ("hard link", f"ln {resolved} {self.ws}/l && cat {self.ws}/l")):
            command = ["/bin/sh", "-c", script]
            with self.subTest(name):
                outside = subprocess.run(command, cwd=self.ws, capture_output=True, text=True)
                (self.ws / "l").unlink(missing_ok=True)
                self.assertIn("fixture-secret", outside.stdout)
                for profile, build in self.confined().items():
                    if profile != "browser":
                        control = subprocess.run(build(["/bin/cat", str(self.ws / "ok")]), cwd=self.ws,
                                                 capture_output=True, text=True)
                        self.assertEqual(control.stdout, "ok", f"{profile}: {control.stderr}")
                    inside = subprocess.run(build(command), cwd=self.ws, capture_output=True, text=True)
                    (self.ws / "l").unlink(missing_ok=True)
                    self.assertNotIn("fixture-secret", inside.stdout, profile)

    def test_browser_profile_reaches_only_the_apps_port(self):
        run, (_, folder) = self.browser_profile(self.other.getsockname()[1]), self.browser_attempt()
        third = socket.socket()
        third.bind(("127.0.0.1", 0))
        third.listen()
        self.addCleanup(third.close)
        for name, port, reached in (("app", self.other.getsockname()[1], True), ("dashboard", self.port, False),
                                    ("another", third.getsockname()[1], False)):
            marker = folder / name
            marker.unlink(missing_ok=True)
            subprocess.run(run(["/bin/sh", "-c", f"/usr/bin/nc -z 127.0.0.1 {port} && : > {marker}"]), cwd=self.ws,
                           capture_output=True, timeout=30, check=False)
            self.assertEqual(marker.exists(), reached, name)

    def test_browser_profile_writes_only_its_folder_and_the_evidence(self):
        run, (attempt, folder) = self.browser_profile(), self.browser_attempt()
        for target, allowed in ((folder / "w", True), (Path(attempt.run_dir) / "evidence" / "w", True),
                                (self.ws / "w", False), (self.marks / "w", False),
                                (Path(attempt.run_dir) / "w", False)):
            target.unlink(missing_ok=True)
            subprocess.run(run(["/bin/sh", "-c", f": > {target}"]), cwd=self.ws, capture_output=True, timeout=30,
                           check=False)
            self.assertEqual(target.exists(), allowed, str(target))

    def test_browser_profile_hides_the_state_folder_but_for_its_own_evidence(self):
        state = self.root / "state"
        for name, spelled in (("as created", state), ("firmlink", Path("/System/Volumes/Data" + str(state))),
                              ("capitals", Path(str(state).upper()))):
            with self.subTest(name), mock.patch.object(paths, "STATE_DIR", spelled):
                run, (attempt, _) = self.browser_profile(), self.browser_attempt()
                (state / "other.txt").write_text("state-secret")
                (Path(attempt.run_dir) / "evidence" / "shot").write_text("shot")
                inside = subprocess.run(run(["/bin/cat", str(state / "other.txt")]), cwd=self.ws,
                                        capture_output=True, text=True, check=False)
                self.assertNotIn("state-secret", inside.stdout)
                seen = subprocess.run(run(["/bin/cat", str(state / "runs" / "r" / "evidence" / "shot")]), cwd=self.ws,
                                      capture_output=True, text=True, check=False)
                self.assertEqual(seen.stdout, "shot", seen.stderr)

    def test_git_in_a_linked_worktree_works_inside(self):
        main = self.ws / "main"
        subprocess.run(["git", "init", "-q", str(main)], check=True)
        subprocess.run([*GIT, "-C", str(main), "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        builders = {"outside": lambda cmd: list(cmd), **self.profiles()}
        for name, build in builders.items():
            with self.subTest(name):
                tree = self.ws / f"tree-{name}"
                subprocess.run([*GIT, "-C", str(main), "worktree", "add", "-q", "-b", name, str(tree)], check=True)
                script = f"cd {tree} && git status && echo x > f && git add f && {shlex.join(GIT)} commit -q -m f"
                done = subprocess.run(build(["/bin/sh", "-c", script]), cwd=self.ws, capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)

    @unittest.skipUnless(shutil.which("uv"), "uv is not installed")
    def test_uv_run_works_inside(self):
        env = {**os.environ, "UV_CACHE_DIR": str(self.root / "uv")}
        argv = ["uv", "run", "--offline", "--no-sync", "--project", str(SOURCE), "python", "-c", "print('ok')"]
        done = subprocess.run(scratch.protected_command(argv, self.ws), cwd=self.ws, env=env,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ok", done.stdout)

    def test_the_regression_gate_works_inside(self):
        test = "tests.test_behavior.OfflineWorkflowTests.test_complete_local_workflow_publishes_verified_change"
        done = subprocess.run(scratch.protected_command([sys.executable, "-m", "unittest", test], SOURCE),
                              cwd=SOURCE, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])

    def test_the_dashboard_answers_inside_the_service_profile(self):
        scratch_root = self.ws / ".arete-scratch-0123456789abcdef"
        for name in ("config", "state"):
            (scratch_root / name).mkdir(parents=True)
        scratch_root.chmod(0o700)
        fake = self.root / "bin"
        fake.mkdir()
        tailscale = fake / "tailscale"
        tailscale.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "status) echo '{\"BackendState\":\"Running\",\"TailscaleIPs\":[\"127.0.0.1\"],"
            "\"Self\":{\"UserID\":1,\"DNSName\":\"n.\",\"TailscaleIPs\":[\"127.0.0.1\"]},\"User\":{\"1\":{\"LoginName\":\"me@example.com\"}}}';;\n"
            "whois) echo '{\"UserProfile\":{\"LoginName\":\"me@example.com\"}}';;\n"
            "esac\n")
        tailscale.chmod(0o755)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        state = scratch_root / "state"
        env = {**os.environ, "ARETE_CONFIG_DIR": str(scratch_root / "config"), "ARETE_STATE_DIR": str(state),
               "ARETE_LOG_DIR": str(state / "logs"), "ARETE_WORKTREES_DIR": str(state / "worktrees"),
               scratch.ENV_NAME: str(scratch_root), "PATH": f"{fake}:{os.environ['PATH']}"}
        boot = f"import sys; sys.path.insert(0, {str(SOURCE)!r}); from arete.cli import main; raise SystemExit(main())"
        words = [sys.executable, "-I", "-c", boot, "dashboard", "--port", str(port)]
        command = scratch.service_command(attempt_for(self.ws), "exec " + shlex.join(words))
        log = self.root / "dashboard.log"
        with open(log, "w") as out:
            proc = subprocess.Popen(command, cwd=self.ws, env=env, start_new_session=True,
                                    stdout=out, stderr=subprocess.STDOUT)
        self.cleanups.append(lambda: (subprocess.run(["kill", "-TERM", f"-{proc.pid}"], capture_output=True),
                                      proc.wait(10)))
        statuses = {}
        deadline = time.monotonic() + 30
        while len(statuses) < 2 and time.monotonic() < deadline and proc.poll() is None:
            try:
                for route in ("/", "/api/health"):
                    request = urllib.request.Request(f"http://127.0.0.1:{port}{route}",
                                                     headers={"Host": f"127.0.0.1:{port}"})
                    statuses[route] = urllib.request.urlopen(request, timeout=3).status
            except OSError:
                time.sleep(.5)
        self.assertEqual(statuses, {"/": 200, "/api/health": 200}, log.read_text()[-2000:])

    def test_peer_folders_stay_closed_inside_the_service_profile(self):
        peer = self.root / "worktrees" / "peer"
        peer.mkdir()
        (peer / "f").write_text("peer-data")
        service = self.profiles()["service"]
        for name, command in (("list", ["/bin/ls", str(peer.parent)]), ("read", ["/bin/cat", str(peer / "f")])):
            with self.subTest(name):
                outside = subprocess.run(command, cwd=self.ws, capture_output=True, text=True)
                self.assertIn("peer", outside.stdout)
                inside = subprocess.run(service(command), cwd=self.ws, capture_output=True, text=True)
                self.assertNotEqual(inside.returncode, 0)
                self.assertNotIn("peer", inside.stdout)


CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
NPX_CACHE = Path.home() / ".npm" / "_npx"


@functools.cache
def cached_registry() -> dict[str, bytes]:
    name, version = browser.pinned()
    modules = next((m for m in sorted(NPX_CACHE.glob("*/node_modules"))
                    if util.read_json(m / name / "package.json", {}).get("version") == version), None)
    served, queue = {}, [name] if modules else []
    while queue:
        package = queue.pop()
        manifest = json.loads((modules / package / "package.json").read_text())
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            for f in sorted((modules / package).rglob("*")):
                if f.is_file() and not f.is_symlink():
                    tar.add(f, "package/" + str(f.relative_to(modules / package)), recursive=False)
        url = browser.REGISTRY + package.replace("/", "-") + ".tgz"
        meta = {"name": package, "version": manifest["version"], "dependencies": manifest.get("dependencies") or {},
                "dist": {"tarball": url, "integrity": browser.integrity_of(out.getvalue())}}
        served[browser.REGISTRY + browser.quote(package, safe="@") + "/" + manifest["version"]] = json.dumps(meta).encode()
        served[url] = out.getvalue()
        queue += [d for d in meta["dependencies"] if browser.REGISTRY + d.replace("/", "-") + ".tgz" not in served]
    return served


class BrowserRunTests(unittest.TestCase):
    FOLDER_LENGTH = 130

    def setUp(self):
        if scratch.sandboxed() or sys.platform != "darwin":
            self.skipTest("the gate runs under inherited confinement")
        if not shutil.which("node") or not CHROME.exists():
            self.skipTest("node or Google Chrome is not installed")
        if not cached_registry():
            self.skipTest(f"no npx cache of {verify.PLAYWRIGHT_MCP} to install the pinned server offline")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ws = self.root / "worktrees" / "ws"
        runs = self.root / "state" / "runs"
        self.run_dir = runs / ("r" * max(1, self.FOLDER_LENGTH - len(str(runs)) - len("//browser")))
        self.evidence = self.run_dir / "evidence"
        self.evidence.mkdir(parents=True)
        self.ws.mkdir(parents=True)
        (self.root / "authority").mkdir()
        self.site = self.root / "site"
        self.site.mkdir()
        (self.site / "index.html").write_text("<html><body><h1>hello from the app</h1></body></html>")
        for patch in (mock.patch.object(paths, "WORKTREES_DIR", self.root / "worktrees"),
                      mock.patch.object(paths, "STATE_DIR", self.root / "state"),
                      mock.patch.object(paths, "SERVER_FILE", self.root / "server.json"),
                      mock.patch.object(paths, "CONFIG_FILE", self.root / "config.toml"),
                      mock.patch.object(scratch, "AUTHORITY", self.root / "authority" / "verification-authority"),
                      mock.patch.object(browser, "fetch", side_effect=lambda url: cached_registry()[url]),
                      mock.patch.object(pi, "WRITABLE_ALWAYS", ("/private/tmp", "/dev")),
                      mock.patch.dict(os.environ, {"TMPDIR": "/private/var/empty"})):
            patch.start()
            self.addCleanup(patch.stop)
        self.assert_probe_local()
        self.a = scratch.Attempt(os.urandom(32).hex(), "t", "r", "verify", str(self.ws),
                                 str(self.ws / ".arete-scratch-0123456789abcdef"), str(self.run_dir), {}, {})
        data = {"version": 2, "engine": "e", "attempt": asdict(self.a), "run": [], "evidence": [], "state": "active",
                "diagnostic": "", "archives": [], "producers": {}, "before": [], "source_mutations": False,
                "cleanup_version": 3}
        with scratch.registry(True) as (fd, key):
            scratch.store(fd, key, data)
        browser.resolved()
        browser.checked()
        self.port = self.serve("127.0.0.1")
        scratch.app_port(self.a, f"http://127.0.0.1:{self.port}")

    def assert_probe_local(self):
        for path in (scratch.AUTHORITY, paths.STATE_DIR, paths.SERVER_FILE, paths.CONFIG_FILE, browser.server_folder()):
            self.assertTrue(path.is_relative_to(self.root), path)

    def serve(self, host, port=0):
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        class Server(http.server.ThreadingHTTPServer):
            address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

        app = Server((host, port), functools.partial(Quiet, directory=str(self.site)))
        threading.Thread(target=app.serve_forever, daemon=True).start()
        self.addCleanup(app.server_close)
        self.addCleanup(app.shutdown)
        return app.server_address[1]

    def start(self):
        words = json.loads(verify.mcp_config(self.evidence))["mcpServers"]["playwright"]["args"][2:]
        argv = scratch.browser_command(self.a, words)
        rest = argv[5:]
        self.assertEqual(rest[0], "/usr/bin/sandbox-exec")
        scratch.update_browser(self.a, words=words, argv=scratch.browser_digest(rest))
        self.assert_probe_local()
        proc = subprocess.Popen(rest, cwd=scratch.browser_path(self.a.run_dir), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True,
                                env={"PATH": "/usr/bin:/bin", "HOME": str(self.root)})
        self.addCleanup(self.reap, proc)
        return proc

    def reap(self, proc):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                proc.send_signal(sig)
            try:
                proc.wait(10)
                break
            except subprocess.TimeoutExpired:
                pass
        proc.stdin.close()
        proc.stdout.close()

    def rpc(self, proc, ident, method, params, wait=90):
        message = {"jsonrpc": "2.0", "method": method, "params": params, **({"id": ident} if ident else {})}
        proc.stdin.write((json.dumps(message) + "\n").encode())
        deadline = time.monotonic() + wait
        while ident and time.monotonic() < deadline:
            if select.select([proc.stdout], [], [], 1)[0]:
                line = proc.stdout.readline()
                if not line:
                    break
                answer = json.loads(line)
                if answer.get("id") == ident:
                    return answer
        self.assertFalse(ident, f"no answer to {method}")

    def initialize(self, proc):
        first = self.rpc(proc, 1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                 "clientInfo": {"name": "test", "version": "0"}})
        self.assertIn("result", first)
        self.rpc(proc, 0, "notifications/initialized", {})

    def visit(self, url, shot):
        proc = self.start()
        self.initialize(proc)
        for ident, tool, arguments in ((2, "browser_navigate", {"url": url}),
                                       (3, "browser_take_screenshot", {"filename": str(shot)})):
            answer = self.rpc(proc, ident, "tools/call", {"name": tool, "arguments": arguments})
            self.assertFalse(answer["result"].get("isError"), answer)
        self.assertGreater(shot.stat().st_size, 0)
        self.reap(proc)

    def test_headless_chrome_loads_the_app_and_writes_a_screenshot(self):
        self.assertEqual(len(str(scratch.browser_path(self.a.run_dir))), self.FOLDER_LENGTH)
        self.visit(f"http://127.0.0.1:{self.port}/", self.evidence / "shot.png")
        self.assertEqual(stat.S_IMODE((self.run_dir / "browser").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(browser.server_folder().stat().st_mode), 0o700)

    def test_the_engine_started_server_answers_over_http_and_leaves_no_process(self):
        listen = verify.free_port(scratch.dashboard_ports() | {self.port})
        scratch.update_browser(self.a, listen=listen)
        self.addCleanup(scratch.stop_served, self.a)
        built, reason = scratch.serve_browser(self.a, verify.mcp_config(self.evidence), [self.evidence])
        self.assertEqual(reason, "")
        url = json.loads(built)["mcpServers"]["playwright"]["url"]
        proc = scratch.SERVED[self.a.selector].proc
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def post(body, session=None):
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                       **({"Mcp-Session-Id": session} if session else {})}
            with opener.open(urllib.request.Request(url, json.dumps(body).encode(), headers), timeout=90) as reply:
                text = reply.read().decode()
                lines = [text] if text.lstrip().startswith("{") else [
                    line[5:] for line in text.splitlines() if line.startswith("data:")]
                return reply.headers.get("Mcp-Session-Id") or session, [json.loads(line) for line in lines if line.strip()]

        session, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "0"}}})
        post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        shot = self.evidence / "served.png"
        for ident, tool, arguments in ((2, "browser_navigate", {"url": f"http://127.0.0.1:{self.port}/"}),
                                       (3, "browser_take_screenshot", {"filename": str(shot)})):
            _, answers = post({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                               "params": {"name": tool, "arguments": arguments}}, session)
            self.assertTrue(answers and not answers[-1]["result"].get("isError"), answers)
        self.assertGreater(shot.stat().st_size, 0)
        self.assertIn("<html", (self.run_dir / "verify-browser.log").read_text())
        scratch.stop_served(self.a)
        with self.assertRaises(ProcessLookupError):
            os.killpg(proc.pid, 0)

    def test_a_localhost_url_reaches_the_app_on_either_loopback_family(self):
        for host in ("127.0.0.1", "::1"):
            with self.subTest(host):
                port = self.serve(host)
                scratch.app_port(self.a, f"http://localhost:{port}")
                self.visit(f"http://localhost:{port}/", self.evidence / f"{host.count(':')}.png")

    def test_a_planted_node_modules_in_the_workspace_is_not_used(self):
        planted = self.ws / "node_modules" / "@playwright" / "mcp"
        planted.mkdir(parents=True)
        marker = self.ws / "planted-ran"
        (planted / "package.json").write_text('{"name":"@playwright/mcp","version":"0.0.75","bin":"cli.js"}')
        (planted / "cli.js").write_text(f"require('fs').writeFileSync({str(marker)!r}, 'x')")
        proc = self.start()
        self.initialize(proc)
        self.assertFalse(marker.exists())


@unittest.skipUnless(sys.platform == "darwin", "the browser profile is macOS only")
class BrowserRunFixtureTests(unittest.TestCase):
    def test_the_browser_run_fixture_opens_only_its_own_authority_and_state(self):
        opened, real = [], paths.directory_fd

        def watched(path, parent=None, create=False):
            if parent is None:
                opened.append(Path(path).absolute())
            return real(path, parent, create)

        case = BrowserRunTests("test_headless_chrome_loads_the_app_and_writes_a_screenshot")
        with mock.patch.object(paths, "directory_fd", watched), mock.patch.object(scratch, "sandboxed", return_value=False), \
                mock.patch.object(pi, "can_confine", return_value=True), \
                mock.patch.object(shutil, "which", return_value="/bin/sh"), \
                mock.patch(f"{__name__}.CHROME", Path("/bin/sh")), \
                mock.patch(f"{__name__}.cached_registry", return_value={"x": b""}), \
                mock.patch.object(browser, "resolved"), mock.patch.object(browser, "checked"):
            case.setUp()
            root = case.root
            case.doCleanups()
        self.assertTrue(opened)
        self.assertEqual([p for p in opened if not p.is_relative_to(root)], [])


class NoLiveStateTests(unittest.TestCase):
    def test_no_test_here_points_at_the_live_authority_or_state(self):
        live = (Path.home() / ".config" / "arete", Path.home() / ".local" / "share" / "arete")
        for path in (scratch.AUTHORITY, paths.SERVER_FILE, pi.AGENT_DIR, pi.LOGIN_DIR, pi.KEY_DIR, pi.SESSIONS_DIR):
            self.assertFalse(any(path.resolve().is_relative_to(r.resolve()) for r in live), path)


if __name__ == "__main__":
    unittest.main()
