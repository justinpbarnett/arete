from pathlib import Path
import unittest

from arete import paths, sandbox

from tests import support


def setUpModule():
    support.isolate_live_state()


class NoLiveStateTests(unittest.TestCase):
    def test_no_test_here_points_at_the_live_authority_or_state(self):
        live = (Path.home() / ".config" / "arete", Path.home() / ".local" / "share" / "arete")
        for path in (sandbox.AUTHORITY, paths.SERVER_FILE, paths.RUNS_DIR, sandbox.AGENT_DIR, sandbox.LOGIN_DIR,
                     sandbox.KEY_DIR, sandbox.SESSIONS_DIR):
            self.assertFalse(any(path.resolve().is_relative_to(r.resolve()) for r in live), path)


if __name__ == "__main__":
    unittest.main()
