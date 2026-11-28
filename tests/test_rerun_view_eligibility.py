import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arete import board, config, control, paths, record, tasks
from tests import support
from arete.orchestration_api import WORKER_PROTOCOL
from arete.store import db as foundation
from arete.store import orchestration as ledger
from arete.store import worker_exchange as exchange
from tests.support import workspace_state

RENDER = """
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { RunFocus } from './arete/web/ui-run.js';

const noop = () => {};
const legacy = { el: noop, cell: noop, table: noop, foldText: noop };
runInNewContext(readFileSync('arete/web/run-page.js', 'utf8'), legacy);

function* elements(node) {
  if (Array.isArray(node)) {
    for (const child of node) yield* elements(child);
  } else if (node && typeof node === 'object') {
    if (typeof node.type === 'function' && !node.type.prototype?.render) {
      yield* elements(node.type(node.props));
    } else {
      yield node;
      yield* elements(node.props?.children);
    }
  }
}

for (const { detail, expected, phase } of JSON.parse(readFileSync(0, 'utf8'))) {
  const mainButtons = [];
  legacy.button = (label, _action, _click, options) => {
    if (label === 'Rerun alone') mainButtons.push(options.of);
  };
  legacy.orchestrationDetail(detail, 'detail');
  assert.deepEqual(mainButtons, expected, `main dashboard: ${phase}`);

  const props = { view: { detail, events: [], turns: [] }, now: Date.now(), retry: noop };
  const focus = new RunFocus(props);
  focus.state.tab = 'actions';
  const rows = [...elements(focus.render(props))].filter(node => node.type === 'tr');
  const uiButtons = rows.filter(row => [...elements(row)].some(node =>
    node.type === 'button' && node.props.children === 'Rerun alone')).map(row => row.key);
  assert.deepEqual(uiButtons, expected, `/ui.html: ${phase}`);
}
"""


class RerunViewEligibilityTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve() / f"{paths.SCRATCH_PREFIX}0123456789abcdef"
        root.mkdir(mode=0o700)
        self.enterContext(workspace_state(root))
        foundation.migrate()
        self.tree = root / "checkout"
        (self.tree / ".git").mkdir(parents=True)
        cfg = config.Config(repos=[config.Repo("fixture", self.tree)])
        self.enterContext(patch.object(config, "load", return_value=cfg))

    def action(self, lease, op, **args):
        turn = ledger.begin(lease, {}, None)
        version = ledger.snapshot(lease)["version"]
        action, = ledger.accept(lease, turn, version, {"calls": [{"op": op, "args": args}]}, None)
        return action

    def stopped_run(self, issue, phase):
        run = support.start_pipeline(run_id=f"run-{phase}", repo="fixture", issue=str(issue))
        lease = ledger.acquire(run, "", {"protocol": WORKER_PROTOCOL,
                                        "prepared": {"checkouts": [{"wt": str(self.tree)}]}})
        check = self.action(lease, "checks.run")["id"]
        ledger.transition(lease, check, "succeeded", {})
        prerequisite = self.action(lease, "worker.start", depends_on=[])
        wid, effective = prerequisite["id"], prerequisite["request"]
        exchange.assign(lease, wid, {"effective": effective, "writing": False})
        exchange.started(lease, wid, wid, {"effective": effective})
        dependent = self.action(lease, "worker.start", depends_on=[wid])
        tail = self.action(lease, "worker.start", depends_on=[dependent["id"]])
        for queued in (dependent, tail):
            exchange.declare_start(lease, queued, "waiting for prerequisite",
                                   {"effective": queued["request"], "writing": False})
        expected = []
        if phase == "admissible":
            exchange.finish(lease, wid, wid, {"effective": effective},
                            {"outcome": "done", "text": "Finished", "ack_ids": []})
        elif phase == "terminal":
            cancel = self.action(lease, "worker.cancel", worker_id=wid, reason="Cancelled")
            exchange.cancel(lease, wid, cancel["id"], "Cancelled", receipt=True)
            expected = [check, wid]
        self.assertTrue(record.move(run, "stopped", ("running",)))
        return lease, check, expected

    def test_both_views_match_standalone_rerun_with_queued_work(self):
        cases = []
        for issue, phase in enumerate(("terminal", "live", "admissible"), 1):
            with self.subTest(phase=phase):
                lease, check, expected = self.stopped_run(issue, phase)
                before = (ledger.actions(lease), exchange.workers(lease))
                detail = board.run_board(lease.run)
                version = detail["run"]["version"]
                with patch.object(tasks, "launch", return_value={"run": "rerun"}) as launch:
                    if expected:
                        self.assertEqual(tasks.run_step(lease.run, check, {}, version), {"run": "rerun"})
                        launch.assert_called_once()
                        self.assertEqual(launch.call_args.args[3][-3:], ["--action", check, "--only-action"])
                    else:
                        with self.assertRaisesRegex(control.Refused, "settle outstanding workers"):
                            tasks.run_step(lease.run, check, {}, version)
                        launch.assert_not_called()
                self.assertEqual((ledger.actions(lease), exchange.workers(lease)), before)
                cases.append({"detail": detail, "expected": expected, "phase": phase})

        result = subprocess.run(["node", "--input-type=module", "--eval", RENDER],
                                cwd=Path(__file__).resolve().parents[1], input=json.dumps(cases),
                                capture_output=True, text=True, timeout=8, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
