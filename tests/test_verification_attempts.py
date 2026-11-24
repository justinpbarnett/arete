import unittest
from contextlib import nullcontext
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock, patch

from arete import code_review, config, factory, orchestration, verification_attempts
from arete.verify import Verified


def check(name, **values):
    return {"what": name, "how": "Use the feature", "saw": "Observed result", "passed": True, **values}


def blocked(name="wheel gesture", *, advisory=False, tools=("Wheel",)):
    return Verified("blocked", "One gesture could not be observed.", [check("page loads", probe=True)],
                    blocked_by="Missing input support", missing_capability="Mouse wheel input",
                    missing_tools=list(tools), unmeasured_requirements=[name], advisory_gaps=[name] if advisory else [])


class VerificationAttemptsTests(unittest.TestCase):
    def setUp(self):
        self.host = self.enterContext(patch.object(verification_attempts, "host", return_value="fixture-host"))
        self.ctrl = ctrl = orchestration.Controller.__new__(orchestration.Controller)
        self.layer = config.RepoLayer(dir=None, verify=config.Verify())
        self.ticket = SimpleNamespace(title="Show the board", body="")
        build = SimpleNamespace(layer=self.layer, read_bash=[], ticket=self.ticket)
        ctrl.job = SimpleNamespace(b=build, ws="/checkout", ticket=self.ticket, goal=self.ticket.title,
                                   checkouts=[], requires_behavior_on_block=False)
        ctrl.data = {"acceptance": "owner-issue", "review_method": "code-review"}
        ctrl.run = SimpleNamespace(environment_release=None)
        ctrl.fingerprint = Mock(return_value="candidate")
        ctrl.policy_sha = Mock(return_value="policy")
        ctrl.matches_policy = lambda saved, current: saved == current
        ctrl.selected = lambda setting: nullcontext()
        ctrl.catalog = Mock()
        ctrl.catalog.settings = {key: SimpleNamespace(step="verify", runner="claude", tools=tools, mcp=[], denied=[])
                                 for key, tools in [("primary", []), ("other-model", []), ("gesture", ["Wheel"])]}
        ctrl.catalog.route.side_effect = lambda key, effort, roles: ctrl.catalog.settings[key]
        ctrl.atomic = Mock()
        self.attempts = []
        self.action = 0

    def run_verifier(self, result=None, *, route="primary", needs=()):
        self.action += 1
        self.ctrl.executing = f"action-{self.action}"

        def measure(_, **kwargs):
            self.attempts.append((self.ctrl.job.goal, kwargs))
            self.assertIsNotNone(result, "unnecessary verifier launch")
            return factory.Outcome("pass", data={"verified": asdict(result)})

        self.ctrl.atomic.side_effect = measure
        return self.ctrl.verification_attempt({"route_id": route, "effort": "low", "goal_refs": [],
                                               "requirements": list(needs)})

    def test_repeated_block_uses_host_capabilities_and_routes_to_an_approved_capable_verifier(self):
        first = self.run_verifier(blocked())
        self.assertEqual(first["capable_routes"], ["gesture"])
        self.assertEqual(first["next_action"], "verification.run")
        for route in ("primary", "other-model"):
            reuse = self.run_verifier(route=route, needs=["wheel gesture"])
            self.assertTrue(reuse["retry_blocked"])
            self.assertEqual(reuse["reused_verification"], "action-1")
        result = self.run_verifier(Verified("pass", checks=[check("wheel gesture")]), route="gesture")
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["observed_requirements"], ["page loads", "wheel gesture"])
        self.assertEqual(result["verification_requirements"], ["wheel gesture"])
        self.assertEqual(self.attempts[-1][1]["covered_commands"], ("page loads",))
        self.assertEqual(len(self.attempts), 2)

    def test_unchanged_partial_baseline_accepts_an_independent_scenario_and_keeps_gap_for_disposition(self):
        self.run_verifier(blocked(advisory=True, tools=()))
        result = self.run_verifier(Verified("pass", checks=[check("fixed clock")]), needs=["fixed clock"])
        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["remaining_requirements"], ["wheel gesture"])
        self.assertEqual(result["missing_capability"], "Mouse wheel input")
        self.assertEqual(result["next_action"], "verification.decide")
        self.assertEqual(self.run_verifier(needs=["fixed clock"])["reused_verification"], "action-2")
        with self.assertRaises(ValueError):
            self.ctrl.decide_verification({"verification_id": "action-1", "reason": "stale"})
        with patch.object(code_review, "candidate_trees", return_value={"": "candidate"}):
            decided = self.ctrl.decide_verification({"verification_id": "action-2", "reason": "Optional input."})
        self.assertEqual(decided["verdict"], "blocked")
        self.assertEqual(decided["nonblocking_trees"], {"": "candidate"})
        self.assertIn("Optional input.", self.ctrl.job.verified.markdown())

    def test_names_scope_a_new_observation_without_reserving_a_second_baseline(self):
        self.run_verifier(Verified("pass", checks=[check("dashboard"), check("login")]))
        self.assertEqual(self.run_verifier()["verdict"], "pass")
        self.assertEqual(self.run_verifier(needs=["Dashboard"])["verdict"], "pass")
        result = self.run_verifier(Verified("pass", checks=[check("fixed clock")]), needs=["dashboard", "fixed clock"])
        self.assertEqual(result["observed_requirements"], ["dashboard", "fixed clock", "login"])
        self.assertEqual(result["verification_requirements"], ["fixed clock"])
        self.assertEqual(self.run_verifier(needs=["fixed clock"])["reused_verification"], "action-4")
        self.assertEqual(len(self.attempts), 2)

    def test_missing_named_observation_cannot_clear_a_required_gap(self):
        self.run_verifier(blocked())
        result = self.run_verifier(Verified("pass", checks=[check("page loads")]), route="gesture")
        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["remaining_requirements"], ["wheel gesture"])
        self.assertEqual(result["next_action"], "owner.ask")
        with self.assertRaises(ValueError):
            self.ctrl.decide_verification({"verification_id": "action-2", "reason": "skip"})

    def test_owner_answer_or_host_change_permits_a_fresh_baseline_after_an_external_fixture_fix(self):
        for change in ("acceptance", "host", "candidate", "policy", "configured tool"):
            with self.subTest(change=change):
                self.ctrl.data.pop("evidence", None)
                first = self.run_verifier(blocked(tools=()))
                self.assertEqual(first["next_action"], "owner.ask")
                if change == "acceptance":
                    # prepare includes the owner's fixture-repair answer in the acceptance digest.
                    self.ctrl.data["acceptance"] = "owner confirmed a repaired fixture"
                elif change == "host":
                    self.host.return_value = "newly provisioned runner"
                elif change == "candidate":
                    self.ctrl.fingerprint.return_value = "repaired candidate"
                elif change == "policy":
                    self.ctrl.policy_sha.return_value = "new policy"
                else:
                    self.ctrl.catalog.settings["primary"].tools.append("GrantedFixture")
                self.assertFalse(self.ctrl.current("verify"))
                self.assertEqual(self.run_verifier(Verified("pass", checks=[check("wheel gesture")]))["verdict"], "pass")

    def test_legacy_partial_pass_keeps_required_gap_on_resume_and_after_a_fixture_repair(self):
        old = Verified("pass", "Dashboard loaded, timeline was not attempted.", [check("dashboard")],
                       unmeasured_requirements=["parallel builders timeline"])
        stamp = {"tree": "candidate", "policy": "policy", "acceptance": "owner-issue", "producer": "old",
                 "result": asdict(old)}
        self.ctrl.data["evidence"] = {"verify": stamp}
        self.ctrl.restore_verification({**asdict(old), "evidence": stamp})
        self.assertEqual(old.verdict, "pass")
        self.assertEqual(self.ctrl.job.verified.verdict, "blocked")
        self.assertIn("verify", self.ctrl.required_gates())
        self.ctrl.data["acceptance"] = "owner fixed the clock fixture"
        self.assertIn("verify", self.ctrl.required_gates())
        self.assertFalse(self.ctrl.current("verify"))
        first = self.run_verifier(Verified("pass", checks=[check("dashboard")]))
        self.assertEqual(first["verdict"], "blocked")
        self.assertEqual(first["unattempted_requirements"], ["parallel builders timeline"])
        self.assertEqual(first["next_action"], "verification.run")
        result = self.run_verifier(Verified("pass", checks=[check("parallel builders timeline")]),
                                   needs=["parallel builders timeline"])
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["observed_requirements"], ["dashboard", "parallel builders timeline"])
        self.assertEqual(self.run_verifier(needs=["parallel builders timeline"])["reused_verification"], "action-2")
        self.assertNotIn("verify", self.ctrl.required_gates())

    def test_product_failure_survives_owner_answer_without_allowing_unrelated_fresh_pass(self):
        self.ctrl.catalog.settings["primary"].tools = ["OriginalInput"]
        self.run_verifier(Verified("fail", missing_tools=["Wheel"],
                                   checks=[check("drag card", passed=False, cause="change")]))
        self.assertEqual(self.run_verifier(route="gesture")["reused_verification"], "action-1")
        self.assertEqual(len(self.attempts), 1)
        self.ctrl.data["acceptance"] = "owner answered"
        self.assertIn("verify", self.ctrl.required_gates())
        result = self.run_verifier(Verified("pass", checks=[check("board loads")]))
        self.assertEqual(result["unattempted_requirements"], ["drag card"])
        self.assertEqual(result["verdict"], "blocked")

    def test_capable_incompatible_route_runs_its_own_baseline_and_does_not_reuse_native_probes(self):
        self.ctrl.catalog.settings["primary"].tools = ["OriginalInput"]
        self.run_verifier(blocked())
        first = self.run_verifier(Verified("pass", checks=[check("wheel gesture")]), route="gesture",
                                  needs=["wheel gesture"])
        self.assertEqual(first["verification_requirements"], [])
        self.assertEqual(first["observed_requirements"], ["wheel gesture"])
        self.assertEqual(self.attempts[-1][1]["covered_commands"], ())

    def test_second_route_cannot_send_the_same_block_back_to_the_original_route(self):
        self.ctrl.catalog.settings["primary"].tools = ["OriginalInput"]
        self.run_verifier(blocked())
        retry = self.run_verifier(blocked(tools=["OriginalInput"]), route="gesture")
        self.assertEqual(retry["capable_routes"], [])
        self.assertEqual(retry["next_action"], "owner.ask")
        self.assertTrue(self.run_verifier(route="primary")["retry_blocked"])
        self.assertEqual(len(self.attempts), 2)

    def test_observed_required_and_advisory_gaps_can_be_settled_independently(self):
        report = blocked(advisory=True)
        report.unmeasured_requirements.append("required card move")
        self.run_verifier(report)
        retry = self.run_verifier(Verified("pass", checks=[check("required card move")]), route="gesture",
                                  needs=["required card move"])
        self.assertEqual(retry["remaining_requirements"], ["wheel gesture"])
        self.assertFalse(retry["verification_required"])
        self.assertEqual(retry["next_action"], "verification.decide")

    def test_required_inputs_and_real_failures_cannot_be_waived(self):
        for source in ("explicit ticket", "ticket acceptance", "owner answer", "probe", "sensitive", "failure"):
            with self.subTest(source=source):
                self.ticket.body = {"explicit ticket": "**Done when:** in verify, use the keyboard",
                                    "ticket acceptance": "Acceptance criteria: wheel gesture"}.get(source, "")
                self.ctrl.data = {"acceptance": "owner-issue", "review_method": "code-review"}
                if source == "owner answer":
                    self.ctrl.data["owner_answers"] = ["1. Scope\n   Answer: Verify that this works in use."]
                self.layer.verify.probes = {"probe": "run-probe"} if source == "probe" else {}
                self.ctrl.job.requires_behavior_on_block = source == "sensitive"
                report = blocked(advisory=True, tools=())
                if source == "failure":
                    report = Verified.from_agent({**asdict(report), "checks": [
                        check("wheel gesture", passed=False, cause="change")]})
                    self.assertEqual(report.verdict, "fail")
                actual = self.run_verifier(report)
                if source != "failure":
                    self.assertEqual(actual["next_action"], "owner.ask")
                with self.assertRaises(ValueError):
                    self.ctrl.decide_verification({"verification_id": self.ctrl.executing, "reason": "skip"})
