import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from arete import api_check
from tests import versioned_api_fixture as fixture


def main():
    parser = argparse.ArgumentParser(description="Exercise the versioned API with disposable state and repositories")
    parser.add_argument("mode", choices=("exercise",))
    parser.add_argument("scenario", choices=("contract-check", "contract-evolution", "nearby"))
    parser.add_argument("--root", type=Path, help="retain contract-evolution evidence in a new directory")
    args = parser.parse_args()
    if args.root and args.scenario != "contract-evolution":
        parser.error("--root requires contract-evolution")
    if args.scenario == "contract-check":
        return api_check.launch()
    if args.scenario == "contract-evolution":
        if args.root:
            args.root.mkdir(mode=0o700)
            fixture.evolution_exercise(args.root.resolve())
        else:
            fixture.evolution_temp()
        return 0
    tests = "tests.test_behavior.VersionedApiTests"
    names = ("test_keyed_replay", "test_keyed_concurrency", "test_versioned_read_surface",
             "test_transport_and_publication_compatibility", "test_restart_drains_without_discarding_an_admitted_publication",
             "test_publication_recovers_remote_urls")
    with tempfile.TemporaryDirectory(prefix="arete-api-nearby-") as temp:
        root = Path(temp).resolve()
        command = api_check.child_command(SOURCE, "import unittest; unittest.main(module=None)", "-v",
                                          *(f"{tests}.{name}" for name in names))
        return subprocess.run(command, cwd=root, env=api_check.isolated_env(root), timeout=60, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
