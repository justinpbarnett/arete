from __future__ import annotations

import gc
import io
import json
import os
import random
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from arete import (
    codex,
    diffguard,
    environment,
    guard,
    local_paths,
    outbound,
    pi_shell,
    sandbox,
    scratch,
    scrub,
    secretstore,
    sessions,
    util,
    vault,
    workspace,
)

FUZZ_ALPHABET = tuple("abcABC0123/\\.~$*?[]:;=<>&|()\"'`!@#%^+-_\t\n\r\x00\x1b\u00a0\u200b\u0301")


def _rand_text(rng: random.Random, max_len: int = 50) -> str:
    return "".join(rng.choice(FUZZ_ALPHABET) for _ in range(rng.randint(0, max_len)))


class CommandGuardBypassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.closed = self.root / ".ssh"
        self.closed.mkdir()
        (self.closed / "id_rsa").write_text("PRIVATE\n")
        (self.closed / "config").write_text("Host secret\n")
        self.claude_dir = self.root / ".claude"
        self.claude_dir.mkdir()
        (self.claude_dir / "settings.json").write_text("{}\n")
        self.netrc = self.root / ".netrc"
        self.netrc.write_text("machine example password secret\n")
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / ".env").write_text("API_KEY=supersecretvalue12345\n")
        (self.work / "README.md").write_text("hello\n")
        (self.work / "sym_env").symlink_to(self.work / ".env")
        (self.work / "escape_dir").symlink_to(self.closed)
        self.floor = guard.Floor((self.closed, self.claude_dir), (self.netrc,), (self.work,))

    def test_file_tools_block_case_variants_symlinks_null_bytes_and_glob_escapes(self):
        denied_payloads = [
            {"tool_name": "Read", "cwd": str(self.work), "tool_input": {"file_path": str(self.work / ".ENV")}},
            {"tool_name": "Read", "cwd": str(self.work), "tool_input": {"file_path": str(self.work / "sym_env")}},
            {"tool_name": "Read", "cwd": str(self.work), "tool_input": {"file_path": str(self.work / "escape_dir" / "new")}},
            {"tool_name": "Read", "cwd": str(self.work), "tool_input": {"file_path": str(self.root / ".CLAUDE" / "settings.json")}},
            {"tool_name": "Read", "cwd": str(self.work), "tool_input": {"file_path": str(self.root / ".NETRC")}},
            {"tool_name": "Read", "cwd": str(self.work), "tool_input": {"file_path": str(self.work / "a\x00b")}},
            {"tool_name": "Glob", "cwd": str(self.work), "tool_input": {"pattern": "../.ssh/*"}},
            {"tool_name": "Glob", "cwd": str(self.work), "tool_input": {"pattern": str(self.root / ".ss*" / "*")}},
            {"tool_name": "Glob", "cwd": str(self.work), "tool_input": {"pattern": ".env"}},
            {"tool_name": "Glob", "cwd": str(self.work), "tool_input": {"pattern": "a\x00*"}},
        ]
        for payload in denied_payloads:
            with self.subTest(payload=payload):
                self.assertTrue(guard.decide(payload, False, self.floor))

        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertTrue(
                guard.decide({"tool_name": "Glob", "cwd": str(self.work), "tool_input": {"pattern": "~/.ssh/*"}},
                             False, self.floor)
            )
        self.assertEqual(
            guard.decide({"tool_name": "Read", "cwd": str(self.work), "tool_input": {"file_path": str(self.work / "README.md")}},
                         False, self.floor),
            "",
        )

    def test_bash_blocks_nested_shells_flags_ansi_c_globs_unclosed_quotes_and_keychain(self):
        denied_commands = [
            f'bash -c "cat {self.closed / "id_rsa"}"',
            'bash -c "security find-generic-password -w"',
            "echo find-generic-password | security -i",
            f'cat "{self.closed / "id_rsa"}" "',
            f"cat -f{self.closed / 'config'}",
            f"cat $'{self.closed / 'config'}'",
            r"cat $'\x2e\x65\x6e\x76'",
            f"cat {self.root}/.ss*/config",
            "cat .en*",
            "cat sym_env",
            f"python3 -c 'open(\"{self.closed / 'config'}\")'",
            "echo ok\x00; id",
        ]
        for command in denied_commands:
            with self.subTest(command=command):
                self.assertTrue(
                    guard.decide({"tool_name": "Bash", "cwd": str(self.work), "tool_input": {"command": command}},
                                 False, self.floor),
                    command,
                )

    def test_guard_main_fails_closed_and_harmless_commands_remain_allowed(self):
        (self.work / "sym_env").unlink()
        (self.work / "escape_dir").unlink()
        for command in (
            "ls *",
            "cat README.md",
            "git status",
            "security find-certificate -a -p",
            "awk '/security/ { print $1 }' README.md",
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    guard.decide({"tool_name": "Bash", "cwd": str(self.work), "tool_input": {"command": command}},
                                 False, self.floor),
                    "",
                )

        out = io.StringIO()
        with (mock.patch.object(sys, "stdin", io.StringIO("{not-json")), redirect_stdout(out)):
            self.assertEqual(guard.main(), 0)
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")

        codex_out = io.StringIO()
        with (mock.patch.object(sys, "stdin", io.StringIO("{not-json")), redirect_stdout(codex_out)):
            self.assertEqual(codex.guard_main(False, []), 0)
        self.assertEqual(json.loads(codex_out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_deterministic_guard_fuzz_never_crashes(self):
        rng = random.Random(101)
        for _ in range(100):
            sample = _rand_text(rng, 45)
            self.assertIsInstance(self.floor.denies(sample), bool)
            self.assertIsInstance(self.floor.denies_glob(sample), bool)
            self.assertIsInstance(guard.secret_named(sample, str(self.work)), str)
            self.assertIsInstance(
                guard.decide({"tool_name": "Bash", "cwd": str(self.work), "tool_input": {"command": sample}},
                             False, self.floor),
                str,
            )
            self.assertIsInstance(
                guard.decide({"tool_name": "Read", "cwd": str(self.work), "tool_input": {"file_path": sample}},
                             False, self.floor),
                str,
            )


class PiShellPolicyFuzzTests(unittest.TestCase):
    def test_rules_match_blocks_leading_whitespace_and_simple_command_rejects_control_injections(self):
        self.assertTrue(pi_shell.rules_match("  rm -rf /", ("Bash(rm *)",)))
        self.assertTrue(pi_shell.rules_match("\tgit push origin main", ("Bash(git push*)",)))
        self.assertFalse(pi_shell.rules_match("git status", ("Bash(rm *)",)))

        for unsafe in (
            "",
            "   ",
            "git status; id",
            "git status | cat",
            "git status &",
            "git status\nid",
            "echo $(id)",
            "echo `id`",
            "cat <(id)",
            "echo hi > out.txt",
            "echo hi\x00",
            'echo "unclosed',
        ):
            with self.subTest(cmd=unsafe):
                self.assertFalse(pi_shell.simple_command(unsafe))

        self.assertTrue(pi_shell.simple_command("git status --short"))

    def test_command_env_ignores_empty_hidden_and_drops_shell_loader_hooks(self):
        env = pi_shell.command_env(
            ["CUSTOM_SAFE"],
            {
                "CUSTOM_SAFE": "visible",
                "BASH_ENV": "/tmp/evil.sh",
                "bash_env": "/tmp/evil2.sh",
                "BASH_FUNC_ls%%": "() { echo pwn; }",
                "BASHOPTS": "xtrace",
                "LD_PRELOAD": "/tmp/evil.so",
                "CLAUDE_TEST": "secret",
                "SECRET_LEAK": "hidden-value-1234",
                "OTHER_VAR": "contains hidden-value-1234 inside",
            },
            hidden=("", "hidden-value-1234"),
        )
        self.assertEqual(env.get("CUSTOM_SAFE"), "visible")
        for dropped in ("BASH_ENV", "bash_env", "BASH_FUNC_ls%%", "BASHOPTS", "LD_PRELOAD",
                        "CLAUDE_TEST", "SECRET_LEAK", "OTHER_VAR"):
            self.assertNotIn(dropped, env)

    def test_deterministic_pi_shell_fuzz_never_crashes(self):
        rng = random.Random(202)
        for _ in range(200):
            sample = _rand_text(rng, 45)
            self.assertIsInstance(pi_shell.simple_command(sample), bool)
            self.assertIsInstance(pi_shell.rules_match(sample, ("Bash(git *)", "Bash(rm *)")), bool)


class SandboxAndLocalPathsConfinementTests(unittest.TestCase):
    def test_sbpl_quote_prevents_profile_injection_and_protected_command_adds_separator(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            injected = 'com.apple.service")(allow default)(global-name "x'
            profile = sandbox.write_confinement(root, [], agent_roots=False, mach=([injected], "^$"))[-1]
            self.assertIn(f"(global-name {sandbox.sbpl_quote(injected)})", profile)
            self.assertNotIn('com.apple.service")(allow default)', profile)

            with self.assertRaises(sandbox.ConfinementUnavailable):
                sandbox.sbpl_quote("bad\nline")
            with self.assertRaises(sandbox.ConfinementUnavailable):
                sandbox.sbpl_quote("bad\x00byte")

            with (mock.patch.object(sandbox, "can_confine", return_value=True),
                  mock.patch.object(sandbox, "sandboxed", return_value=False)):
                argv = sandbox.protected_command(["-evil-flag", "arg"], root)
            self.assertEqual(argv[-3:], ["--", "-evil-flag", "arg"])

    def test_secret_path_and_local_paths_enforce_case_and_traversal_boundaries(self):
        for secret in (".env", ".ENV", ".Env.local", "ID_RSA", "server.PEM", "CREDENTIALS.JSON",
                       ".ssh", ".SSH/config", "sub/.AWS/credentials"):
            with self.subTest(secret=secret):
                self.assertTrue(sandbox.secret_path(secret))
        for safe in (".env.example", ".ENV.EXAMPLE", "README.md", "src/app.py"):
            with self.subTest(safe=safe):
                self.assertFalse(sandbox.secret_path(safe))

        self.assertTrue(local_paths.inside_scratch(Path("/tmp/.ARETE-SCRATCH-0123456789abcdef.noindex/state")))
        self.assertFalse(local_paths.inside_scratch(Path("/tmp/a\x00b")))
        self.assertTrue(local_paths.is_verify_sandbox_path(".arete-sandbox/tmp/out"))
        self.assertFalse(local_paths.is_verify_sandbox_path(".arete-sandbox/../secret.txt"))
        self.assertFalse(local_paths.is_verify_sandbox_path("/.arete-sandbox/out"))
        self.assertFalse(local_paths.is_verify_sandbox_path(".arete-sandbox/a\x00b"))

    def test_canonical_exposure_and_sandbox_ownership_handle_null_bytes_and_symlinks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with self.assertRaises(sandbox.ConfinementUnavailable):
                sandbox.canonical(Path(f"{root}/bad\x00name"))
            with self.assertRaises(sandbox.ConfinementUnavailable):
                sandbox.real_path(Path(f"{root}/bad\x00name"))
            with self.assertRaises(sandbox.ConfinementUnavailable):
                scratch.exposure(Path(f"{root}/bad\x00name"), [root])
            with self.assertRaises(sandbox.ConfinementUnavailable):
                scratch.exposure(root, [Path(f"{root}/bad\x00grant")])
            self.assertFalse(local_paths.verify_sandbox_owned(Path(f"{root}/bad\x00name")))
            with self.assertRaises(PermissionError):
                with local_paths.directory_fd(Path(f"{root}/bad\x00name")):
                    pass

    def test_deterministic_confinement_fuzz_never_crashes(self):
        rng = random.Random(303)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            for _ in range(100):
                sample = _rand_text(rng, 45)
                self.assertIsInstance(sandbox.secret_path(sample), bool)
                self.assertIsInstance(local_paths.inside_scratch(Path(sample)), bool)
                self.assertIsInstance(local_paths.is_verify_sandbox_path(sample), bool)
                self.assertIsInstance(local_paths.verify_sandbox_owned(Path(sample)), bool)
                try:
                    sandbox.canonical(Path(sample))
                except sandbox.ConfinementUnavailable:
                    pass
                try:
                    scratch.exposure(Path(sample), [root])
                except sandbox.ConfinementUnavailable:
                    pass


class GitDiffGuardAndWorkspaceInjectionTests(unittest.TestCase):
    def test_git_ref_validation_and_option_injection_guards(self):
        for bad_ref in ("", "--upload-pack=evil", "-b", "main\nfoo", "main foo", "a\x00b"):
            with self.subTest(bad_ref=bad_ref):
                with self.assertRaises(ValueError):
                    util.validate_git_ref(bad_ref)

        wt = Path("/tmp/fake-wt")
        with self.assertRaises(ValueError):
            diffguard.between(wt, "--output=/tmp/pwn", "HEAD")
        with self.assertRaises(ValueError):
            workspace.fetch(wt, "--upload-pack=evil")
        with self.assertRaises(ValueError):
            workspace.remote_head(wt, "--all")
        with self.assertRaises(ValueError):
            util.add_worktree(wt, wt / "sub", "-evil", "HEAD")
        with self.assertRaises(ValueError):
            util.add_worktree(wt, wt / "sub", "ok", "--orphan")
        with self.assertRaises(ValueError):
            util.delete_branch(wt, "--all")
        with self.assertRaises(ValueError):
            sessions.clone("remote", "org/repo", "--upload-pack=evil", "main", "/tmp/x", "")

    def test_diffguard_case_insensitive_denies_and_malformed_headers(self):
        for denied in (".ENV", ".Env.prod", ".CLAUDE/settings.json", ".GITHUB/WORKFLOWS/ci.yml", ".AGENTS/config.toml"):
            with self.subTest(denied=denied):
                self.assertTrue(util.matches_any(denied, diffguard.ALWAYS_DENY))

        self.assertEqual(diffguard.header_path("diff --git a/foo.py b/foo.py"), "foo.py")
        self.assertEqual(diffguard.header_path("diff --git a/"), "")
        self.assertEqual(diffguard.header_path("diff --git a/short b/muchlonger"), "")
        self.assertEqual(diffguard.header_path('diff --git "a/quoted" "b/quoted"'), "")

    def test_sessions_spec_file_and_workspace_remove_untracked_block_traversal(self):
        for bad_id in ("", ".", "..", "../escape", "a/b", "/abs", "ses\x00id"):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError):
                    sessions.spec_file(bad_id)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            wt = root / "wt"
            wt.mkdir()
            outside = root / "outside.txt"
            outside.write_text("keep me\n")
            for bad_name in ("../outside.txt", str(outside), "sub/../../outside.txt", "a\x00b"):
                with self.subTest(bad_name=bad_name):
                    with self.assertRaises(ValueError):
                        workspace.remove_untracked(wt, bad_name)
            self.assertTrue(outside.exists())

    def test_deterministic_git_and_vault_fuzz_never_crashes(self):
        rng = random.Random(404)
        self.assertEqual(vault.git_unquoted('"\\1a"'), "1a")
        self.assertEqual(vault.git_unquoted('"\\09"'), "09")
        self.assertEqual(vault.git_unquoted('"\\101"'), "A")
        for _ in range(200):
            sample = _rand_text(rng, 45)
            self.assertIsInstance(diffguard.header_path("diff --git " + sample), str)
            self.assertIsInstance(vault.git_unquoted(sample), str)
            self.assertIsInstance(vault.git_unquoted('"' + sample + '"'), str)
            self.assertIsInstance(util.matches_any(sample, diffguard.ALWAYS_DENY), bool)


class RedactionStreamingAndEnvScopingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(scrub, "load", return_value=scrub.Redactions()))
        self.enterContext(mock.patch.object(scrub, "_record_hits"))

    def test_line_scrubber_redacts_pem_and_tokens_across_random_chunk_splits(self):
        rng = random.Random(505)
        pem = (
            "-----BEGIN EC PRIVATE KEY-----\n"
            "MHcCAQEEISecretBodyLineOne12345\n"
            "SecretBodyLineTwo67890==\n"
            "-----END EC PRIVATE KEY-----"
        )
        token = "ar_test_deadbeef_TokenBody1234567890"
        payload = f"start {token} mid\n{pem}\nend {token}\n"
        for _ in range(80):
            buf = io.StringIO()
            scrubber = scrub.LineScrubber(buf)
            pos = 0
            while pos < len(payload):
                step = rng.randint(1, 20)
                scrubber.write(payload[pos:pos + step])
                pos += step
            scrubber.flush()
            emitted = buf.getvalue()
            self.assertNotIn("SecretBodyLineOne", emitted)
            self.assertNotIn("SecretBodyLineTwo", emitted)
            self.assertNotIn(token, emitted)
            self.assertIn("[token:deadbeef]", emitted)

    def test_scrub_value_redacts_dict_keys_tuples_url_encoding_and_survives_cycles(self):
        secret = "sec+ret/val=123&foo=bar"
        nested = {secret: (secret, [f"url=https://x/?k={secret.replace('+', '%2B').replace('/', '%2F').replace('=', '%3D').replace('&', '%26')}"])}
        cleaned = scrub.value(nested, "log", extra=secret)
        self.assertNotIn(secret, str(cleaned))
        self.assertNotIn("sec%2Bret", str(cleaned))
        self.assertIsInstance(next(iter(cleaned.values())), tuple)

        cyc_list: list = []
        cyc_list.append(cyc_list)
        self.assertEqual(scrub.value(cyc_list, "log"), ["[circular]"])
        cyc_dict: dict = {}
        cyc_dict["self"] = cyc_dict
        self.assertEqual(scrub.value(cyc_dict, "log"), {"self": "[circular]"})

        # Ensure merged_with does not return stale cache if base object is replaced.
        extra_red = scrub.Redactions(["extra_secret_12345678"])
        base1 = scrub.Redactions(["first_secret_12345678"])
        m1 = extra_red.merged_with(base1)
        self.assertIn("first_secret_12345678", m1.held)
        del base1
        gc.collect()
        base2 = scrub.Redactions(["second_secret_12345678"])
        m2 = extra_red.merged_with(base2)
        self.assertIn("second_secret_12345678", m2.held)

    def test_redos_and_foreground_maintenance_stay_linear_on_adversarial_inputs(self):
        t0 = time.monotonic()
        scrub.replaced("-----BEGIN PRIVATE KEY-----" * 10000, ())
        outbound.redact("sk-" * 10000)
        out = util.foreground_maintenance({"GIT_CONFIG_COUNT": "5000000"})
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)
        self.assertEqual(out["GIT_CONFIG_COUNT"], "5000002")

        with mock.patch.object(outbound.paths, "CONFIG_FILE", Path("/nonexistent/config.toml")):
            self.assertEqual(outbound.screen("hello"), "hello")

    def test_environment_build_and_secretstore_role_rules_enforce_scoping(self):
        source = {
            "PATH": "/usr/bin:/bin",
            "SAFE_VAR": "ok",
            "LD_PRELOAD": "/tmp/evil.so",
            "BASH_ENV": "/tmp/evil.sh",
            "BASH_FUNC_ls%%": "() { echo pwn; }",
            "MY_API_KEY": "secret12345678",
            "BAD=KEY": "nope",
            "NULL_VAL": "a\x00b",
        }
        with mock.patch.object(environment, "exposed_env_names", return_value={"REGISTERED_VAR"}):
            env = environment.build(["*"], source={**source, "registered_var": "should-drop"})
        self.assertEqual(env.get("SAFE_VAR"), "ok")
        for dropped in ("LD_PRELOAD", "BASH_ENV", "BASH_FUNC_ls%%", "MY_API_KEY", "BAD=KEY", "NULL_VAL", "registered_var"):
            self.assertNotIn(dropped, env)

        self.assertTrue(environment.reserved("bash_env"))
        self.assertTrue(environment.reserved("BASH_FUNC_ls%%"))
        self.assertTrue(environment.reserved("arete_token"))

        with self.assertRaises(ValueError):
            secretstore.validate_expose([{"env": "OK_VAR", "roles": ["step:a*b"]}])
        self.assertFalse(secretstore.role_matches("", "step:*"))
        self.assertTrue(secretstore.role_matches("step:check", "step:*"))


if __name__ == "__main__":
    unittest.main()
