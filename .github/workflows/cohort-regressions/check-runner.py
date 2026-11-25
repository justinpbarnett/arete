"""The production #975 receipt had split stderr. A real assertion failure must still reach repair."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from arete import check_repair

layer = SimpleNamespace(checks={"behavior": "uv run python -m tests.parallel"}, targeted_commands={})
selector = "test_worker.WorkerTests.test_setup"
original = (
    f"test_setup ({selector})\nTraceback (most recent call last):\n"
    "AssertionError: sandbox subprocess exited with return code 71; stderr:\n"
    "sandbox-exec: sandbox_apply: Operation not permitted\n"
    "Failed classes: test_worker.WorkerTests\nRan 1 tests in 1.00s: FAILED"
)
mixed = original.replace(
    "Failed classes: test_worker.WorkerTests",
    "test_save (test_data.DataTests.test_save)\n"
    "AssertionError: wrong saved value\nFailed classes: test_worker.WorkerTests, test_data.DataTests",
)
unavailable = check_repair.failed_check(layer, "behavior", original, 1).get("capability") == "macos-sandbox-profile"
masked = bool(check_repair.failed_check(layer, "behavior", mixed, 1).get("capability"))
print(
    json.dumps(
        {"pass": unavailable and not masked, "shared_runner_wait": unavailable, "masked_product_failure": masked}
    )
)
