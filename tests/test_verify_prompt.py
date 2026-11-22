import unittest
from pathlib import Path
from unittest import mock

from arete import config, guard, jev, paths, pi_shell, pipeline, prompt_loader, scratch, tracker
from arete.agent import Result


class VerifyPromptEnvTests(unittest.TestCase):
    HARNESS = "/runtime/verification-runtime/harness/sandbox"

    def test_verify_prompt_shows_env_after_the_first_allowed_harness(self):
        prompt = self.render_prompt([f"{self.HARNESS} *", "sandbox *"])
        self.assertIn(f"`{self.HARNESS} env ARETE_SERVER=<url> uv run arete task list`", prompt)
        self.assertEqual(prompt.count("env ARETE_SERVER="), 1)
        self.assertIn("`<allowed command> env API_KEY=arete-placeholder-1 <program>`", prompt)
        self.assertNotRegex(prompt, r"`[A-Z_]+=[^`]+ [^`]+`")
        self.assertIn("solely because it starts with `NAME=value`, retry once", prompt)
        self.assertIn("For any other refusal, or if the retry is refused", prompt)

    def test_verify_prompt_prefers_a_configured_relative_sandbox(self):
        for rule in ("./sandbox *", "./sandbox env *"):
            with self.subTest(rule=rule):
                prompt = self.render_prompt(["sandbox uv *", rule, f"{self.HARNESS} *"])
                self.assertIn("`./sandbox env ARETE_SERVER=<url> uv run arete task list`", prompt)
                self.assertEqual(prompt.count("env ARETE_SERVER="), 1)

    def test_verify_prompt_prefers_the_first_configured_wrapper_path(self):
        prompt = self.render_prompt(["./verify-wrapper *", f"{self.HARNESS} *"])
        self.assertIn("`./verify-wrapper env ARETE_SERVER=<url> uv run arete task list`", prompt)
        self.assertEqual(prompt.count("env ARETE_SERVER="), 1)

    def test_verify_prompt_preserves_the_first_wrapper_shell_spelling(self):
        for wrapper in (f"/qa+tools{self.HARNESS}", f"'/qa tools{self.HARNESS}'", f'"/qa tools{self.HARNESS}"',
                        r"/qa\ tools" + self.HARNESS, "~/verification-runtime/harness/sandbox"):
            with self.subTest(wrapper=wrapper):
                prompt = self.render_prompt([f"{wrapper} *", f"{self.HARNESS} *"])
                self.assertIn(f"For example, run `{wrapper} env ARETE_SERVER=<url> uv run arete task list`", prompt)
                self.assertEqual(prompt.count("env ARETE_SERVER="), 1)

    def test_verify_prompt_prefers_the_first_bare_wrapper(self):
        prompt = self.render_prompt(["verify-wrapper *", f"{self.HARNESS} *"])
        self.assertIn("`verify-wrapper env ARETE_SERVER=<url> uv run arete task list`", prompt)
        self.assertEqual(prompt.count("env ARETE_SERVER="), 1)

    def test_verify_prompt_prefers_the_first_wrapper_with_options(self):
        for wrapper in ("verify-wrapper --", f"{self.HARNESS} -i --"):
            with self.subTest(wrapper=wrapper):
                rule = f"{wrapper} *"
                prompt = self.render_prompt([rule, f"{self.HARNESS} *"])
                example = f"{wrapper} env ARETE_SERVER=<url> uv run arete task list"
                self.assertIn(f"For example, run `{example}`", prompt)
                self.assertEqual(prompt.count("For example, run"), 1)
                command = example.replace("<url>", "http://127.0.0.1:51390")
                self.assertTrue(pi_shell.rules_match(command, (f"Bash({rule})",)))

    def test_verify_prompt_skips_env_executables_and_uses_the_next_wrapper(self):
        for rule in ("/usr/bin/env -u ARETE_REVIEW_UNUSED -- *", "/usr/bin/env -i -- *", "env *",
                     "'/usr/bin/env' *", "env env ARETE_SERVER=* uv run arete task list"):
            with self.subTest(rule=rule):
                prompt = self.render_prompt([rule, "verify-wrapper *"])
                self.assertIn("For example, run `verify-wrapper env ARETE_SERVER=<url> uv run arete task list`", prompt)
                self.assertEqual(prompt.count("For example, run"), 1)

    def test_verify_prompt_skips_an_env_rule_with_a_joined_terminal_wildcard(self):
        rule = f"{self.HARNESS} env ARETE_SERVER=* uv run arete task list*"
        prompt = self.render_prompt([rule])
        self.assertIn("`<allowed command> env NAME=value <program> …`", prompt)
        self.assertNotIn("For example, run", prompt)

        prompt = self.render_prompt([rule, "verify-wrapper *"])
        self.assertIn("For example, run `verify-wrapper env ARETE_SERVER=<url> uv run arete task list`", prompt)

    def test_verify_prompt_fills_wildcards_in_an_explicit_env_rule(self):
        for wrapper, assignments, example_assignments in (
            (self.HARNESS, "ARETE_SERVER=*", "ARETE_SERVER=http://127.0.0.1:51390"),
            (self.HARNESS, 'ARETE_SERVER="*"', 'ARETE_SERVER="http://127.0.0.1:51390"'),
            (self.HARNESS, "ARETE_SERVER=http://127.0.0.1:*", "ARETE_SERVER=http://127.0.0.1:51390"),
            (self.HARNESS, "ARETE_SERVER='http://localhost:*'", "ARETE_SERVER='http://localhost:51390'"),
            (f"/qa+tools{self.HARNESS}", "ARETE_SERVER=*", "ARETE_SERVER=http://127.0.0.1:51390"),
            (f"'/qa tools{self.HARNESS}'", "ARETE_SERVER=* API_KEY=*",
             "ARETE_SERVER=http://127.0.0.1:51390 API_KEY=arete-placeholder-1"),
            (self.HARNESS, 'LABEL="qa tools" ARETE_SERVER=* API_KEY=*',
             'LABEL="qa tools" ARETE_SERVER=http://127.0.0.1:51390 API_KEY=arete-placeholder-1'),
            (self.HARNESS, r"LABEL=qa\ tools EMPTY= ARETE_SERVER=* LABEL2='other tools'",
             r"LABEL=qa\ tools EMPTY= ARETE_SERVER=http://127.0.0.1:51390 LABEL2='other tools'"),
        ):
            with self.subTest(wrapper=wrapper, assignments=assignments):
                rule = f"{wrapper} env {assignments} uv run arete task list"
                prompt = self.render_prompt([rule])
                example = f"{wrapper} env {example_assignments} uv run arete task list"
                self.assertIn(f"For example, run `{example}`", prompt)
                self.assertTrue(pi_shell.rules_match(example, (f"Bash({rule})",)))

    def test_verify_prompt_keeps_generic_guidance_without_an_env_rule(self):
        prompt = self.render_prompt(["sandbox uv *"])
        self.assertIn("`<allowed command> env NAME=value <program> …`", prompt)
        self.assertNotIn("For example, run", prompt)

    def render_prompt(self, allow_bash):
        root = Path.cwd()
        layer = config.RepoLayer(root, verify=config.Verify(url="", allow_bash=allow_bash))
        run = mock.Mock(tid=1, repo=config.Repo("example", root), dir=root, add_dirs=[], owner_note="")
        run.agent.return_value = Result(ok=True, structured={"verdict": "pass", "checks": []})
        ticket = tracker.Ticket("1", "#1", "List tasks")
        work = pipeline.TicketWork(config.Config(), run, ticket, root, [], layer, 60, "test workspace")

        with (mock.patch.object(paths, "dashboard_ports", return_value=frozenset()),
              mock.patch.object(scratch, "make", side_effect=ValueError("no scratch authority")),
              mock.patch.object(pipeline, "run_folder", return_value=root / "evidence"),
              mock.patch.object(pipeline, "restore_worktrees")):
            pipeline.use_product(work, "verify", "verify", goal="List tasks.", summary="", diff="")

        call = run.agent.call_args
        self.assertEqual(call.kwargs["allowed_tools"], prompt_loader.bash_rules(work.read_bash + layer.verify.bash))
        return call.args[1]

    def test_guard_passes_env_placeholder_and_blocks_visible_token(self):
        token = "ghp_000000000000000000000000000000000000"
        suffix = "uv run arete task list"

        def judge(prompt, *_args, **_kwargs):
            exposes_token = token in prompt
            return jev.Reply({"secrets": jev.Answer(exposes_token, 1.0, {"yes": float(exposes_token)})})

        with (mock.patch.object(config, "load", return_value=config.Config()),
              mock.patch.object(jev, "ready", return_value=True),
              mock.patch.object(jev, "ask", side_effect=judge) as ask,
              mock.patch.object(guard, "note")):
            for value in ("arete-placeholder-1", token):
                with self.subTest(value=value):
                    command = f"{self.HARNESS} env API_KEY={value} {suffix}"
                    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
                    refusal = guard.decide(payload, judge=True, boundary=guard.Floor((), (), ()))
                    judged_command = ask.call_args.args[0].split("\nCommand:\n", 1)[1]
                    if value == token:
                        self.assertEqual(judged_command, command)
                        self.assertIn("touching credentials", refusal)
                    else:
                        self.assertEqual(judged_command, f"{self.HARNESS} env {suffix}")
                        self.assertEqual(refusal, "")
