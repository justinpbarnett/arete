import re
from dataclasses import asdict, replace
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from arete import config, factory, gh, pipeline, pr_recovery, unblock, util, workspace
from arete.store import orchestration

BASE, ACCEPTED, LOCAL, REMOTE, FOREIGN, CANDIDATE, LATER, TREE = (digit * 40 for digit in "abcdef12")
OWNER = "earlier-run"
BRANCH = "topic"
PR = "https://github.com/example/first/pull/961"


def checkout():
    return workspace.Checkout("first", "example/first", Path("source/first"), Path("workspace/first"),
                              "main", PR, ACCEPTED, True, revision=BASE, base_commit=BASE)


def refs(head):
    return pr_recovery.Refs(PR, "OPEN", BRANCH, head, "main", BASE, False, head, BASE)


class ReconciledGateTests(TestCase):
    def test_passing_command_that_changes_one_checkout_cannot_supply_check_evidence(self):
        first = checkout()
        second = workspace.Checkout("second", "example/second", Path("source/second"),
                                    Path("workspace/second"), "main")
        for mutated in (False, True):
            with self.subTest(mutated=mutated):
                build = pipeline.Build(config.Config(), Mock(), Mock(), first.wt.parent, [first, second],
                                       config.RepoLayer(dir=Path("policy"), checks={"local": "check"}), 10, "fixture",
                                       tools=[], deny=[], from_base=True)
                build.checked = {"tree": BASE, "commands": dict(build.layer.checks)}
                trees = {first.wt: BASE, second.wt: TREE}
                git_state = {c.github: (LOCAL, f"refs/heads/{BRANCH}", TREE) for c in build.checkouts}

                def run_checks(*args, **kwargs):
                    if mutated:
                        trees[second.wt] = CANDIDATE
                    return True, "command passed"

                with (patch.object(factory, "fingerprint", side_effect=lambda path: trees.get(Path(path), "")),
                      patch.object(pipeline, "check_git_state", return_value=git_state),
                      patch.object(pr_recovery, "check_staging"),
                      patch.object(build, "restage", return_value=pipeline.Staged()),
                      patch.object(pipeline, "run_checks", side_effect=run_checks) as check):
                    self.assertEqual(build.check(), not mutated)
                check.assert_called_once()
                self.assertEqual(bool(build.checked.get("tree")), not mutated)
                if mutated:
                    self.assertNotEqual(build.report, "command passed")


class RecoveryOwnershipTests(TestCase):
    ownership_blocker = re.compile(r"unowned|not owned|lineage|run's chain")

    def attempt(self, *, local, observed, initial, foreign_tip):
        c = checkout()

        def git(args, cwd, **kw):
            if args[0] != "log" or cwd not in (c.src, c.wt):
                raise AssertionError(f"unexpected Git command: {args}")
            tip, *excluded = [arg for arg in args[1:] if not arg.startswith("--")]
            if tip != foreign_tip:
                return f"{tip}\t{OWNER}\n"
            merge = f"{local}\t{OWNER}\n" if tip == local else ""
            if f"^{FOREIGN}" in excluded or (merge and "--first-parent" in args):
                return merge
            return merge + f"{FOREIGN}\tother-run\n"

        with (patch.object(pr_recovery, "observe", return_value=observed),
              patch.object(pr_recovery, "fetch_inputs"), patch.object(pr_recovery, "ancestor", return_value=True),
              patch.object(util, "git", side_effect=git),
              patch.object(util, "repo_lock", side_effect=AssertionError("unowned history reached worktree setup")),
              patch.object(pr_recovery, "loose_files", return_value=pr_recovery.Retained(set(), set())),
              patch.object(pr_recovery, "check_retained"), patch.object(factory, "fingerprint", return_value=TREE),
              self.assertRaises(pr_recovery.Hold) as held):
            pr_recovery.plan(c, BRANCH, ACCEPTED, local, initial, {OWNER}, "resumed", local, [])
        return held.exception

    def test_remote_commits_outside_the_run_are_not_authorized_by_additivity(self):
        error = self.attempt(local=LOCAL, observed=refs(FOREIGN), initial=FOREIGN, foreign_tip=FOREIGN)
        self.assertRegex(error.why, self.ownership_blocker)
        self.assertIn(FOREIGN, error.why)

    def test_owned_merge_does_not_authorize_a_foreign_local_parent(self):
        error = self.attempt(local=CANDIDATE, observed=refs(REMOTE), initial=REMOTE, foreign_tip=CANDIDATE)
        self.assertRegex(error.why, self.ownership_blocker)
        self.assertIn(FOREIGN, error.why)

    def test_converged_lookup_race_does_not_park_against_the_new_head_unchecked(self):
        observed = refs(FOREIGN)
        error = self.attempt(local=LOCAL, observed=observed, initial=REMOTE, foreign_tip=FOREIGN)
        if self.ownership_blocker.search(error.why):
            self.assertEqual(error.recovery["refs"]["head"], FOREIGN)
        else:
            poll = {"refs": asdict(observed), "target": True, "local": LOCAL, "decision": "", "sibling_replies": ""}
            self.assertIn(REMOTE, error.why)
            self.assertTrue(unblock.recovery_changed(error.recovery, poll), error.why)


class RecoverySettlementTests(TestCase):
    def settle(self, c, observed, *, tree=TREE, retained=CANDIDATE, effects=()):
        trees = {c.github: TREE}
        receipt = {"action": "publication", "branch": BRANCH, "trees": trees, "leases": {c.github: REMOTE},
                   "targets": {c.github: {"accepted": ACCEPTED, "refs": asdict(refs(REMOTE))}}}
        intent = {"tree": orchestration.digest(trees), "attempt": [c.github, CANDIDATE], "pushed": {},
                  "pr_effects": list(effects), "recovery_receipt": receipt}
        c.note_pushed_head(REMOTE)
        made = {workspace.head_key(c): retained}

        def git(args, cwd, **kw):
            if args[0] == "log":
                return f"{retained}\t{OWNER}\n"
            if args[0] == "rev-parse" and args[-1] in ("HEAD^{tree}", f"{CANDIDATE}^{{tree}}", f"{retained}^{{tree}}"):
                return TREE
            raise AssertionError(f"unexpected Git command: {args}")

        with (patch.object(pr_recovery, "current_head", return_value=retained),
              patch.object(pr_recovery, "current_ref", return_value=f"refs/heads/{BRANCH}"),
              patch.object(pr_recovery, "observe", return_value=observed), patch.object(pr_recovery, "fetch_inputs"),
              patch.object(pr_recovery, "ancestor", return_value=True), patch.object(pr_recovery, "check_retained"),
              patch.object(factory, "fingerprint", return_value=tree), patch.object(util, "git", side_effect=git),
              patch.object(util, "sh", side_effect=AssertionError("settlement ran a Git subprocess")),
              patch.object(gh, "gh", side_effect=AssertionError("settlement replayed a PR effect"))):
            return pr_recovery.settle([c], BRANCH, made, intent, {OWNER})

    def test_unconfirmed_push_can_settle_at_the_exact_gated_candidate(self):
        c = checkout()
        self.assertEqual(self.settle(c, refs(CANDIDATE)), [])
        self.assertEqual((c.lease, c.recovery_head, c.recovery_base), (CANDIDATE, CANDIDATE, BASE))

    def test_uncertain_push_does_not_settle_nonmatching_refs_or_gates(self):
        seen = refs(CANDIDATE)
        effect = {"kind": pr_recovery.GATE_EVIDENCE, "github": checkout().github, "pr": PR, "head": CANDIDATE}
        scenarios = {
            "not pushed": {"observed": refs(REMOTE)},
            "descendant is not the receipt": {"observed": refs(LATER)},
            "GitHub lag": {"observed": replace(seen, head=REMOTE)},
            "base moved": {"observed": replace(seen, base=LATER, origin_base=LATER)},
            "retained tree changed": {"tree": LATER},
            "retained SHA changed with the same tree": {"retained": LATER},
            "PR write not confirmed": {"effects": [effect]},
        }
        for scenario, change in scenarios.items():
            with self.subTest(scenario=scenario):
                c = checkout()
                with self.assertRaises((factory.Refused, pr_recovery.Hold)):
                    self.settle(c, **({"observed": seen} | change))
                self.assertEqual((c.lease, c.recovery_head), (REMOTE, ""))
