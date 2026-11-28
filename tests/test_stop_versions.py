import signal
import unittest
from unittest import mock

from arete import agent, api, contract, control, record
from arete.store import runs, tasks, writes
from tests import support


def setUpModule():
    support.isolate_live_state()


class StopVersionTests(unittest.TestCase):
    def assert_stop_conflict(self, run, base_version):
        current = runs.run_get(run)
        for stop in (control.stop_run, runs.ask_stop):
            with self.subTest(stop=stop.__module__), mock.patch.object(record, "dead", side_effect=AssertionError):
                with self.assertRaises(writes.VersionConflict) as raised:
                    stop(run, base_version)
                self.assertEqual(str(raised.exception), "This object changed; refresh it before trying again.")
                self.assertEqual(raised.exception.current, current)
        self.assertEqual(runs.run_get(run), current)

    def test_stop_accepts_version_before_progress_updates(self):
        pid = 12345
        run = support.start_pipeline(pid=pid)
        before = runs.run_get(run)
        measured_at = "2030-01-02T03:04:05Z"
        runs.checkpoint(run, 1, 0, 0, 0, measured_at)
        record.step(run, "build", agent.Result(ok=True, cost_usd=0.25), 0)
        progress = runs.run_get(run)
        self.assertEqual(progress["version"], before["version"] + 2)
        self.assertEqual((progress["measured_at"], progress["cost_usd"]), (measured_at, 0.25))

        with (mock.patch.object(record.util, "pid_alive", return_value=True),
              mock.patch.object(control.os, "killpg") as signal_group):
            self.assertEqual(control.stop_run(run, before["version"], public=True),
                             {"id": run, "stopped": True, "pid": pid})

        signal_group.assert_called_once_with(pid, signal.SIGTERM)
        self.assertTrue(runs.run_get(run)["stop_asked"])

    def test_stop_refuses_version_before_run_ended(self):
        run = support.start_pipeline()
        before = runs.run_get(run)["version"]
        self.assertTrue(record.settle(run, "done"))
        self.assert_stop_conflict(run, before)

    def test_stop_refuses_version_before_launcher_recorded_pid(self):
        run = support.start_pipeline(pid=None)
        before = runs.run_get(run)["version"]
        record.note(run, pid=12345)
        self.assert_stop_conflict(run, before)

    def test_task_update_still_refuses_stale_version(self):
        task = tasks.task_add("sample", "Make a change")
        version = tasks.task_get(task)["version"]
        edit = {"id": task, "base_version": version, "title": "Edited"}
        api.mutation("task.update", edit, None)
        with self.assertRaises(contract.Problem) as raised:
            api.mutation("task.update", {**edit, "title": "Stale"}, None)
        self.assertEqual(raised.exception.code, "task_version_conflict")
        self.assertEqual(tasks.task_get(task)["title"], "Edited")
