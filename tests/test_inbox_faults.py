from __future__ import annotations

import json
import sys
from functools import wraps
from pathlib import Path
from unittest import mock

from arete import (
    accounts,
    agent,
    contract,
    control,
    gh,
    inbox,
    outbox,
    paths,
    pipeline,
    principals,
    record,
    secretstore,
    serve,
    tasks,
    util,
)
from arete.store import db as foundation
from arete.store import devices as device_store
from arete.store import inbox as inbox_store
from arete.store import runs as run_store
from arete.store import tasks as task_store
from tests import support
from tests.test_behavior import _InboxCase


def _fault_test(fn):
    @wraps(fn)
    def run(self):
        self.scratch(fn.__qualname__)

    return run


def _dummy_proc():
    return type(
        "_Proc",
        (),
        {
            "pid": 4242,
            "poll": lambda self: None,
            "terminate": lambda self: None,
            "kill": lambda self: None,
            "wait": lambda self, timeout=None: 0,
        },
    )()


def _insert_missing_required_secret(scope: str, name: str, *, kind: str = "api_key", provider: str | None = None) -> None:
    secretstore.migrate_registry()
    with foundation.tx() as c:
        tag = foundation.metadata(c)["instance_tag"]
        ref = str(secretstore.Reference("pve_store", tag, f"{scope}/{name}"))
        c.execute(
            "INSERT INTO secrets (name, scope, kind, provider, store, reference, required, status, missing_since) "
            "VALUES (?, ?, ?, ?, 'pve_store', ?, 1, 'missing', ?)",
            (name, scope, kind, provider, ref, util.iso()),
        )


class InboxFaultsTests(_InboxCase):
    PRELUDE = "from tests.test_inbox_faults import InboxFaultsTests; InboxFaultsTests.run_case(sys.argv[1])"

    @staticmethod
    def run_case(case):
        with (
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network request")),
            mock.patch.object(accounts, "read", side_effect=AssertionError("Unexpected accounts.read")),
            mock.patch.object(gh, "gh", side_effect=AssertionError("Unexpected gh.gh")),
        ):
            name, fixture_method = case.split(".")
            fixture = globals()[name]()
            fixture.prepare()
            check = getattr(type(fixture), fixture_method)
            result = getattr(check, "__wrapped__", check)(fixture, json.load(sys.stdin))
        print("BEHAVIOR_RESULT=" + json.dumps(result))

    @_fault_test
    def test_continue_missing_secret_grouping_atomicity_and_resume(self, _):
        _insert_missing_required_secret("global", "ASANA_TOKEN")
        launched_cmds = []

        def fake_launch(cmd, log_name):
            launched_cmds.append(list(cmd))
            return _dummy_proc()

        with mock.patch.object(control, "launch", side_effect=fake_launch), self.listen() as server:
            cookie, _ = self.pair_and_client(server)

            before, _ = self.http(server, "GET", self.INBOX, cookie=cookie)
            self.assertEqual([i for i in before["items"] if i["kind"] == "continue"], [])

            t1 = task_store.task_add("fixture", "First task", "First task", "feat")
            t2 = task_store.task_add("fixture", "Second task", "Second task", "feat")
            t3 = task_store.task_add("fixture", "Third task", "Third task", "feat")

            r1_resp, _ = self.http(server, "POST", f"/api/v1/tasks/{t1}/run", body={}, cookie=cookie, status=201)
            r1 = r1_resp["run"]["id"]
            self.assertEqual(launched_cmds, [])

            listed1, _ = self.http(server, "GET", self.INBOX, cookie=cookie)
            cont_items = [i for i in listed1["items"] if i["kind"] == "continue"]
            self.assertEqual(len(cont_items), 1)
            item = cont_items[0]
            self.assertEqual(item["subject"], "secret:global/ASANA_TOKEN")
            self.assertEqual(item["priority"], 1)
            self.assertEqual(item["verbs"], ["continue"])
            self.assertEqual(item["detail"]["name"], "ASANA_TOKEN")
            self.assertEqual(item["detail"]["scope"], "global")
            self.assertEqual(item["detail"]["group_key"], "continue:global/ASANA_TOKEN")
            self.assertEqual(item["detail"]["group_members"], [f"run:{r1}"])
            self.assertEqual(item["detail"]["waiting"], [f"run:{r1}"])
            contract.validate(item["detail"], contract.INBOX_DETAIL_SHAPES["continue"])
            self.assertEqual(len(device_store.open_waits_for_subject(f"run:{r1}")), 1)

            screen, _ = self.http(server, "GET", "/api/v1/screens/triage", cookie=cookie)
            self.assertIn(item["id"], [i["id"] for i in screen["by_priority"]["high"]])

            self.assertEqual(serve.recover_queued(min_age=0), [])
            self.assertEqual(launched_cmds, [])

            r2_resp, _ = self.http(server, "POST", f"/api/v1/tasks/{t2}/run", body={}, cookie=cookie, status=201)
            r3_resp, _ = self.http(server, "POST", f"/api/v1/tasks/{t3}/run", body={}, cookie=cookie, status=201)
            r2, r3 = r2_resp["run"]["id"], r3_resp["run"]["id"]

            listed3, _ = self.http(server, "GET", self.INBOX, cookie=cookie)
            cont_items3 = [i for i in listed3["items"] if i["kind"] == "continue"]
            self.assertEqual(len(cont_items3), 1)
            item3 = cont_items3[0]
            self.assertEqual(item3["detail"]["group_members"], [f"run:{r1}", f"run:{r2}", f"run:{r3}"])
            self.assertEqual(len(item3["detail"]["group_members"]), 3)

            with foundation.tx() as c:
                ob_id = outbox.draft(
                    c,
                    target="github",
                    kind="comment",
                    object="github:fixture/repo#7",
                    body="Update status",
                    drafted_by="hand",
                    approved_by="hand",
                    opened_at=util.iso(),
                    slot="github-wait-test",
                )
            outbox.deliver_now(ob_id)
            self.assertEqual(len(device_store.open_waits_for_subject(f"outbox:{ob_id}")), 1)
            listed_ob, _ = self.http(server, "GET", self.INBOX, cookie=cookie)
            cont_ob = [i for i in listed_ob["items"] if i["kind"] == "continue"]
            self.assertEqual(len(cont_ob), 1)
            self.assertIn(f"outbox:{ob_id}", cont_ob[0]["detail"]["group_members"])
            item_cur = cont_ob[0]

            still_err, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{item_cur['id']}/resolve",
                body={"base_version": item_cur["version"], "verb": "continue"},
                cookie=cookie,
                status=409,
                code="still_missing",
            )
            self.assertEqual(still_err["code"], "still_missing")
            after_check = inbox_store.get(item_cur["id"])
            self.assertEqual(after_check["state"], "open")
            self.assertIs(after_check["detail"]["last_check"]["ok"], False)

            with mock.patch.object(inbox, "resolve_secret_continue_in_tx", side_effect=RuntimeError("injected fault")):
                self.http(
                    server,
                    "PUT",
                    "/api/v1/secrets/global/ASANA_TOKEN/value",
                    raw_body=b"asana-real-secret-value-1234",
                    media="text/plain",
                    cookie=cookie,
                    status=500,
                )
            self.assertEqual(inbox_store.get(item_cur["id"])["state"], "open")
            self.assertEqual(len(device_store.open_waits_for_subject(f"run:{r1}")), 1)
            self.assertEqual(len(device_store.open_waits_for_subject(f"run:{r2}")), 1)
            self.assertEqual(len(device_store.open_waits_for_subject(f"run:{r3}")), 1)
            self.assertEqual(launched_cmds, [])

            self.http(
                server,
                "PUT",
                "/api/v1/secrets/global/ASANA_TOKEN/value",
                raw_body=b"asana-real-secret-value-1234",
                media="text/plain",
                cookie=cookie,
                status=200,
            )
            resolved_item = inbox_store.get(item_cur["id"])
            self.assertEqual(resolved_item["state"], "resolved")
            self.assertEqual(resolved_item["resolution"], "continued")
            self.assertEqual(resolved_item["answer"], "continue")
            for subj in (f"run:{r1}", f"run:{r2}", f"run:{r3}", f"outbox:{ob_id}"):
                self.assertEqual(device_store.open_waits_for_subject(subj), [])
            self.assertEqual(len(launched_cmds), 3)
            with foundation.reader() as c:
                ev = c.execute(
                    "SELECT kind, data FROM events_log WHERE subject = ? AND kind = 'inbox.resolved' ORDER BY seq DESC LIMIT 1",
                    (f"inbox:{inbox_store.parse_id(item_cur['id'])}",),
                ).fetchone()
                self.assertIsNotNone(ev)

            inbox.sync_all()
            self.assertEqual(inbox.list(state="open", kind="continue"), [])

    @_fault_test
    def test_continue_verb_resolves_when_secret_check_passes(self, _):
        secretstore.put("global", "CHECK_KEY", "initial-valid-secret-value", "api_key", "hand")
        with foundation.tx() as c:
            c.execute(
                "UPDATE secrets SET required = 1, status = 'missing', missing_since = ? "
                "WHERE scope = 'global' AND name = 'CHECK_KEY'",
                (util.iso(),),
            )
        launched = []
        with (
            mock.patch.object(control, "launch", side_effect=lambda cmd, log: launched.append(list(cmd)) or _dummy_proc()),
            self.listen() as server,
        ):
            cookie, _ = self.pair_and_client(server)
            tid = task_store.task_add("fixture", "Check task", "Check task", "feat")
            started, _ = self.http(server, "POST", f"/api/v1/tasks/{tid}/run", body={}, cookie=cookie, status=201)
            rid = started["run"]["id"]
            item = inbox_store.get_open_by_kind_and_subject("continue", "secret:global/CHECK_KEY")
            self.assertIsNotNone(item)
            self.assertEqual(launched, [])

            res, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{item['id']}/resolve",
                body={"base_version": item["version"], "verb": "continue"},
                cookie=cookie,
                status=200,
            )
            self.assertEqual(res["item"]["state"], "resolved")
            self.assertEqual(res["item"]["resolution"], "continued")
            self.assertEqual(device_store.open_waits_for_subject(f"run:{rid}"), [])
            self.assertEqual(len(launched), 1)

    @_fault_test
    def test_fault_escalation_to_stop_redirect_needs_info_and_retry(self, _):
        with self.listen() as server:
            cookie, _ = self.pair_and_client(server)

            tid = task_store.task_add("fixture", "Verify task", "Verify task", "feat")
            with principals.acting(principals.owner()):
                rid = support.start_pipeline(repo="fixture", issue=tid, status="failed")
            log_path = paths.RUNS_DIR / rid / "run.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("\n".join(f"log-line-{i}" for i in range(1, 31)) + "\n")

            s1 = run_store.queue_step(rid, "verify", 1)
            run_store.fail_step(s1, "AssertionError: scratch cleanup refused")

            listed1, _ = self.http(server, "GET", self.INBOX, cookie=cookie)
            fault_items = [i for i in listed1["items"] if i["kind"] == "fault"]
            self.assertEqual(len(fault_items), 1)
            fault_item = fault_items[0]
            self.assertEqual(fault_item["subject"], f"step:{rid}/verify")
            self.assertEqual(fault_item["priority"], 2)
            self.assertEqual(fault_item["verbs"], ["retry", "dismiss"])
            self.assertEqual(fault_item["detail"]["node"], "verify")
            self.assertEqual(fault_item["detail"]["attempts"], 1)
            self.assertEqual(fault_item["detail"]["error"], "AssertionError: scratch cleanup refused")
            self.assertEqual(fault_item["detail"]["group_key"], "fault:AssertionError")
            self.assertEqual(len(fault_item["detail"]["last_lines"]), 20)
            self.assertEqual(fault_item["detail"]["last_lines"][0], "log-line-11")
            self.assertEqual(fault_item["detail"]["last_lines"][-1], "log-line-30")
            contract.validate(fault_item["detail"], contract.INBOX_DETAIL_SHAPES["fault"])

            shelve_until = "2099-01-01T09:00:00Z"
            shelved, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{fault_item['id']}/shelve",
                body={"base_version": fault_item["version"], "reason": "Waiting on sandbox fix", "until": shelve_until},
                cookie=cookie,
                status=200,
            )
            self.assertEqual(shelved["item"]["detail"]["shelved_reason"], "Waiting on sandbox fix")
            self.assertEqual(shelved["item"]["detail"]["shelve_until"], shelve_until)

            s2 = run_store.queue_step(rid, "verify", 2)
            run_store.fail_step(s2, "AssertionError: scratch cleanup refused")

            escalated_fault = inbox_store.get(fault_item["id"])
            self.assertEqual(escalated_fault["state"], "resolved")
            self.assertEqual(escalated_fault["resolution"], "escalated")

            listed2, _ = self.http(server, "GET", self.INBOX, cookie=cookie)
            stop_items = [i for i in listed2["items"] if i["kind"] == "stop"]
            self.assertEqual(len(stop_items), 1)
            stop_item = stop_items[0]
            self.assertEqual(stop_item["subject"], f"run:{rid}")
            self.assertEqual(stop_item["priority"], 1)
            self.assertEqual(stop_item["verbs"], ["redirect", "retry", "stop"])
            self.assertEqual(stop_item["detail"]["node"], "verify")
            self.assertEqual(stop_item["detail"]["group_key"], "stop:AssertionError")
            self.assertEqual(len(stop_item["detail"]["last_lines"]), 20)
            jidoka = stop_item["detail"]["jidoka"]
            self.assertEqual(set(jidoka), {"detect", "stop", "fix", "prevent"})
            self.assertTrue(all(isinstance(v, str) and v for v in jidoka.values()))
            contract.validate(stop_item["detail"], contract.INBOX_DETAIL_SHAPES["stop"])

            s3 = run_store.queue_step(rid, "verify", 3)
            run_store.fail_step(s3, "AssertionError: scratch cleanup refused")
            self.assertEqual(len(inbox.list(state="open", kind="stop")), 1)
            stop_item = inbox_store.get(stop_item["id"])

            edited_fix = "Clean scratch via the wrapper script before verify"
            edited_prevent = "Probe scratch cleanup before starting verify"
            with mock.patch.object(control, "launch", return_value=_dummy_proc()) as mock_launch:
                redirected, _ = self.http(
                    server,
                    "POST",
                    f"{self.INBOX}/{stop_item['id']}/resolve",
                    body={
                        "base_version": stop_item["version"],
                        "verb": "redirect",
                        "fix": edited_fix,
                        "prevent": edited_prevent,
                    },
                    cookie=cookie,
                    status=200,
                )
            self.assertEqual(redirected["item"]["state"], "resolved")
            self.assertEqual(redirected["item"]["resolution"], "redirected")
            self.assertEqual(redirected["item"]["detail"]["jidoka"]["fix"], edited_fix)
            self.assertEqual(redirected["item"]["detail"]["jidoka"]["prevent"], edited_prevent)
            self.assertEqual((paths.RUNS_DIR / rid / pipeline.ANSWER_FILE).read_text(), edited_fix)
            mock_launch.assert_called_once()

            tid_ni = task_store.task_add("fixture", "Needs info task", "Needs info task", "feat")
            with principals.acting(principals.owner()):
                rid_ni = support.start_pipeline(repo="fixture", issue=tid_ni, status="needs-info")
            ni_s1 = run_store.queue_step(rid_ni, "plan", 1)
            run_store.fail_step(ni_s1, "ValueError: missing spec detail")
            ni_s2 = run_store.queue_step(rid_ni, "plan", 2)
            run_store.fail_step(ni_s2, "ValueError: missing spec detail")
            inbox.sync_all()
            self.assertIsNone(inbox_store.get_open_by_kind_and_subject("stop", f"run:{rid_ni}"))
            self.assertIsNotNone(inbox_store.get_open_by_kind_and_subject("freeform", f"run:{rid_ni}"))

            tid_f = task_store.task_add("fixture", "Fault retry task", "Fault retry task", "feat")
            with principals.acting(principals.owner()):
                rid_f = support.start_pipeline(repo="fixture", issue=tid_f, status="failed")
            fs1 = run_store.queue_step(rid_f, "build", 1)
            run_store.fail_step(fs1, "TimeoutError: build timed out")
            fs2 = run_store.queue_step(rid_f, "build", 2)
            run_store.fail_step(fs2, "RuntimeError: different failure on retry")
            f_items = [i for i in inbox.list(state="open", kind="fault") if i["subject"] == f"step:{rid_f}/build"]
            self.assertEqual(len(f_items), 1)
            f_item = f_items[0]
            self.assertEqual(f_item["detail"]["attempts"], 2)
            self.assertEqual(f_item["detail"]["group_key"], "fault:RuntimeError")

            with principals.acting(principals.owner()):
                rid_f_cont = pipeline.queue_run("fixture", tid_f, continues=rid_f, restart="resume")
            with mock.patch.object(
                tasks,
                "resume",
                return_value=tasks.answer(tid_f, "fixture", rid_f_cont, "running", "started"),
            ) as mock_resume:
                retried, _ = self.http(
                    server,
                    "POST",
                    f"{self.INBOX}/{f_item['id']}/retry",
                    body={},
                    cookie=cookie,
                    status=200,
                )
            mock_resume.assert_called_once_with(rid_f, "build")
            self.assertEqual(retried["item"]["state"], "open")
            self.assertEqual(retried["item"]["answer"], "retry")

            record.step(rid_f_cont, "build", agent.Result(ok=True, runner="replay"), 0.2)
            after_pass = inbox_store.get(f_item["id"])
            self.assertEqual(after_pass["state"], "resolved")
            self.assertEqual(after_pass["resolution"], "passed")

            tid_s = task_store.task_add("fixture", "Stop verbs task", "Stop verbs task", "feat")
            with principals.acting(principals.owner()):
                rid_s = support.start_pipeline(repo="fixture", issue=tid_s, status="failed")
            for tno in (1, 2):
                st = run_store.queue_step(rid_s, "checks", tno)
                run_store.fail_step(st, "OSError: disk full")
            stop_v = inbox_store.get_open_by_kind_and_subject("stop", f"run:{rid_s}")
            self.assertIsNotNone(stop_v)
            with mock.patch.object(control, "stop_run", return_value={"id": rid_s, "stopped": True}) as mock_stop:
                stopped_resp, _ = self.http(
                    server,
                    "POST",
                    f"{self.INBOX}/{stop_v['id']}/resolve",
                    body={"base_version": stop_v["version"], "verb": "stop"},
                    cookie=cookie,
                    status=200,
                )
            mock_stop.assert_called_once_with(rid_s)
            self.assertEqual(stopped_resp["item"]["state"], "resolved")
            self.assertEqual(stopped_resp["item"]["resolution"], "stopped")

            tid_d = task_store.task_add("fixture", "Dismiss fault task", "Dismiss fault task", "feat")
            with principals.acting(principals.owner()):
                rid_d = support.start_pipeline(repo="fixture", issue=tid_d, status="failed")
            ds = run_store.queue_step(rid_d, "plan", 1)
            run_store.fail_step(ds, "502 Bad Gateway from tracker")
            d_item = inbox_store.get_open_by_kind_and_subject("fault", f"step:{rid_d}/plan")
            self.assertEqual(d_item["detail"]["group_key"], "fault:http_502")
            dismissed, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{d_item['id']}/resolve",
                body={"base_version": d_item["version"], "verb": "dismiss"},
                cookie=cookie,
                status=200,
            )
            self.assertEqual(dismissed["item"]["state"], "resolved")
            self.assertEqual(dismissed["item"]["resolution"], "dismissed")
            inbox.sync_all()
            self.assertIsNone(inbox_store.get_open_by_kind_and_subject("fault", f"step:{rid_d}/plan"))

            for bad_subject in ("secret:global/FAKE", "step:r1/plan"):
                self.http(
                    server,
                    "POST",
                    self.INBOX,
                    body={"kind": "fault", "subject": bad_subject, "title": "manual", "priority": 2, "verbs": ["dismiss"]},
                    cookie=cookie,
                    status=422,
                    code="validation",
                )

    def test_openapi_lists_detail_shapes_and_retry_route(self):
        doc = json.loads((Path(__file__).resolve().parents[1] / "docs/openapi.json").read_text())
        schemas = doc["components"]["schemas"]
        for name in ("inbox.detail.continue", "inbox.detail.stop", "inbox.detail.fault", "problem.still_missing"):
            self.assertIn(name, schemas)
        self.assertIn("/api/v1/inbox/{item}/retry", doc["paths"])
        self.assertIn("post", doc["paths"]["/api/v1/inbox/{item}/retry"])
        for route in ("/api/v1/inbox", "/api/v1/inbox/{item}", "/api/v1/screens/triage"):
            shapes = doc["paths"][route]["get"].get("x-arete-detail-shapes") or {}
            self.assertEqual(set(shapes), {"continue", "stop", "fault"})
