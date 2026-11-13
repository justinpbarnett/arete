import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.request

import arete
from arete import config, paths, pi, pipeline, scratch, util, verify

SOURCE = Path(arete.__file__).resolve().parent.parent
LAUNCH = "(deny lsopen)(deny appleevent-send)(deny job-creation)"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
              "/Support/lsregister")
CONNECT = "import socket, sys; socket.create_connection(('127.0.0.1', int(sys.argv[1])), 3).close(); open(sys.argv[2], 'w').close()"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


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
        for name, build in self.profiles().items():
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
                for profile, build in self.profiles().items():
                    control = subprocess.run(build(["/bin/cat", str(self.ws / "ok")]), cwd=self.ws,
                                             capture_output=True, text=True)
                    self.assertEqual(control.stdout, "ok", f"{profile}: {control.stderr}")
                    inside = subprocess.run(build(command), cwd=self.ws, capture_output=True, text=True)
                    (self.ws / "l").unlink(missing_ok=True)
                    self.assertNotIn("fixture-secret", inside.stdout, profile)

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


if __name__ == "__main__":
    unittest.main()
