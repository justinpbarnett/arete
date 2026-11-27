import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import isolate_live_state

from arete import config, release_lane as lane, serve_guard
from arete.store import specgraphs

isolate_live_state()


class StagingLane(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = config.Repo("product", self.root, github="owner/product", target_branch="staging")
        self.base = "a" * 40
        self.fix = "b" * 40
        self.main = self.enterContext(patch.object(lane.promotion, "branch", return_value=self.base))
        self.marker = self.enterContext(patch.object(lane.proof, "deployed_revision", return_value=self.base))
        self.enterContext(patch.object(lane.release_profile, "exists", return_value=True))
        self.grant = self.enterContext(patch.object(lane.release_profile, "standing_status", return_value={"active": True}))
        profile = {"settings": {"production_branch": "main", "production": "https://app.test"}}
        self.enterContext(patch.object(lane.release_profile, "current", return_value=profile))
        self.enterContext(patch.object(lane.proof, "state_dir", side_effect=lambda _, ticket: self.root / ticket))
        self.enterContext(patch.object(lane.proof, "enrolled", return_value=["21"]))
        self.complete = self.enterContext(patch.object(lane.proof, "completion", return_value=None))

    def test_next_merge_waits_for_prior_promotion_deploy_and_observation(self):
        with patch.object(specgraphs, "membership", return_value=None):
            self.assertTrue(lane.merge_hold(self.repo, "22", self.fix))
        self.main.return_value = self.fix
        self.assertTrue(lane.merge_hold(self.repo, "22", self.fix))
        self.marker.return_value = self.fix
        serve_guard.write(self.root / "21" / "promotion.json", {"phase": "observing_production", "revision": self.fix})
        self.assertTrue(lane.merge_hold(self.repo, "22", self.fix))
        self.complete.return_value = {"status": "verified"}
        self.assertEqual(lane.merge_hold(self.repo, "22", self.fix), "")

    def test_only_verified_siblings_share_the_graphs_staging_release(self):
        graph = specgraphs.GraphKey("github:owner/product", "20")
        serve_guard.write(self.root / "21" / "promotion.json", {"phase": "staged_for_parent", "graph_parent": "20",
                         "revision": self.fix, "staging_receipt": "receipt"})
        with (patch.object(specgraphs, "membership", return_value=graph),
              patch.object(specgraphs, "children", return_value={"first": ("21", "101")}),
              patch.object(lane.proof, "evidence", return_value={"receipt_id": "receipt", "revision": self.fix, "destination": "staging"}),
              patch.object(lane.promotion, "parent", return_value=self.base) as parent):
            self.assertEqual(lane.merge_hold(self.repo, "22", self.fix), "")
            parent.return_value = "c" * 40
            self.assertTrue(lane.merge_hold(self.repo, "22", self.fix))

    def test_manual_ticket_waits_while_existing_standing_release_is_in_progress(self):
        self.grant.return_value = None
        serve_guard.write(self.root / "21" / "promotion.json", {"standing_authority": "prior", "revision": self.fix,
                         "phase": "promotion_intent"})
        with patch.object(specgraphs, "membership", return_value=None):
            self.assertTrue(lane.merge_hold(self.repo, "15", self.fix))

    def test_existing_manual_qa_profile_has_no_new_landing_gate(self):
        self.grant.return_value = None
        self.assertEqual(lane.merge_hold(self.repo, "17", self.fix), "")
        self.main.assert_not_called()
