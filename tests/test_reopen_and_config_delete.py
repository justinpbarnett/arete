from __future__ import annotations

import json
import sqlite3
import sys
from datetime import timedelta
from functools import wraps
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from unittest import mock

from arete import (
    accounts,
    configrepo,
    contract,
    devices,
    drafts,
    gh,
    inbox,
    leases,
    paths,
    pipeline,
    record,
    util,
)
from arete.store import approvals as approvals_store
from arete.store import config as config_store
from arete.store import db as foundation
from arete.store import inbox as inbox_store
from arete.store import outbox as outbox_store
from tests.test_behavior import _InboxCase


def _case(fn):
    @wraps(fn)
    def run(self):
        self.scratch(fn.__qualname__)

    return run


class _ReopenAndDeleteCase(_InboxCase):
    PRELUDE = (
        "from tests.test_reopen_and_config_delete import InboxReopenTests, ConfigDeleteAndRollbackTests; "
        "InboxReopenTests.run_case(sys.argv[1])"
    )

    def connect(self, server):
        if server.tls is not None:
            return HTTPSConnection("127.0.0.1", server.server_port, timeout=15, context=self.trust)
        return HTTPConnection("127.0.0.1", server.server_port, timeout=15)

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


class InboxReopenTests(_ReopenAndDeleteCase):
    @_case
    def test_snooze_shelve_and_candidate_reopen_within_ten_seconds_and_refusals(self, _):
        with self.listen() as server:
            cookie, _ = self.pair_and_client(server)

            created, _ = self.http(
                server,
                "POST",
                self.INBOX,
                body={
                    "kind": "alert",
                    "subject": "custom:reopen-snooze",
                    "title": "Snooze me",
                    "priority": 1,
                    "verbs": ["continue"],
                },
                cookie=cookie,
                status=201,
            )
            item = created["item"]
            item_id = item["id"]

            snoozed, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{item_id}/snooze",
                body={"base_version": item["version"], "until": "2099-01-01T00:00:00Z", "until_changed": True},
                cookie=cookie,
            )
            self.assertEqual(snoozed["item"]["state"], "snoozed")
            self.assertEqual(snoozed["item"]["snooze_until"], "2099-01-01T00:00:00Z")
            self.assertTrue(snoozed["item"]["snooze_watch"])
            self.assertGreater(snoozed["item"]["snooze_watch_ver"], 0)

            self.http(
                server,
                "POST",
                f"{self.INBOX}/{item_id}/reopen",
                body={"base_version": item["version"]},
                cookie=cookie,
                status=409,
                code="version_conflict",
            )

            reopen_key = "reopen-snooze-key-1"
            reopened, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{item_id}/reopen",
                body={"base_version": snoozed["item"]["version"]},
                cookie=cookie,
                key=reopen_key,
            )
            self.assertEqual(reopened["item"]["state"], "open")
            self.assertEqual(reopened["item"]["priority"], 1)
            self.assertEqual(reopened["item"]["snooze_until"], "")
            self.assertEqual(reopened["item"]["snooze_watch"], "")
            self.assertEqual(reopened["item"]["snooze_watch_ver"], 0)
            self.assertEqual(reopened["item"]["version"], snoozed["item"]["version"] + 1)

            replayed, hdrs = self.http(
                server,
                "POST",
                f"{self.INBOX}/{item_id}/reopen",
                body={"base_version": snoozed["item"]["version"]},
                cookie=cookie,
                key=reopen_key,
            )
            self.assertEqual(replayed, reopened)
            self.assertEqual(hdrs.get("Idempotent-Replayed"), "true")

            hist, _ = self.http(server, "GET", f"{self.INBOX}/{item_id}/history", cookie=cookie)
            self.assertEqual(hist["events"][-1]["kind"], "inbox.changed")
            self.assertTrue(hist["events"][-1]["data"].get("reopened"))

            snoozed2, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{item_id}/snooze",
                body={"base_version": reopened["item"]["version"], "until": "2099-01-01T00:00:00Z"},
                cookie=cookie,
            )
            future = util.now() + timedelta(seconds=11)
            with mock.patch.object(util, "now", return_value=future):
                self.http(
                    server,
                    "POST",
                    f"{self.INBOX}/{item_id}/reopen",
                    body={"base_version": snoozed2["item"]["version"]},
                    cookie=cookie,
                    status=409,
                    code="too_late",
                )
            still_snoozed, _ = self.http(server, "GET", f"{self.INBOX}/{item_id}", cookie=cookie)
            self.assertEqual(still_snoozed["item"]["state"], "snoozed")

            shelve_created, _ = self.http(
                server,
                "POST",
                self.INBOX,
                body={
                    "kind": "alert",
                    "subject": "custom:reopen-shelve",
                    "title": "Shelve me",
                    "priority": 2,
                    "verbs": ["continue"],
                },
                cookie=cookie,
                status=201,
            )
            sh_id = shelve_created["item"]["id"]
            shelved, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{sh_id}/shelve",
                body={
                    "base_version": shelve_created["item"]["version"],
                    "reason": "Later",
                    "until": "2099-01-01T00:00:00Z",
                },
                cookie=cookie,
            )
            self.assertEqual(shelved["item"]["state"], "shelved")
            self.assertEqual(shelved["item"]["shelves"], 1)
            self.assertEqual(shelved["item"]["shelve_reason"], "Later")

            unshelved, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{sh_id}/reopen",
                body={"base_version": shelved["item"]["version"]},
                cookie=cookie,
            )
            self.assertEqual(unshelved["item"]["state"], "open")
            self.assertEqual(unshelved["item"]["shelve_reason"], "")
            self.assertEqual(unshelved["item"]["shelve_until"], "")
            self.assertEqual(unshelved["item"]["shelves"], 0)
            self.assertEqual(unshelved["item"]["version"], shelved["item"]["version"] + 1)

            resolved_alert, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{sh_id}/resolve",
                body={"base_version": unshelved["item"]["version"], "verb": "continue"},
                cookie=cookie,
            )
            self.http(
                server,
                "POST",
                f"{self.INBOX}/{sh_id}/reopen",
                body={"base_version": resolved_alert["item"]["version"]},
                cookie=cookie,
                status=409,
                code="not_reversible",
            )

    @_case
    def test_candidate_decline_reopen_collision_and_approval_proposal_refusals(self, _):
        skipped_state = {
            "fixture#42": {
                "repo": "fixture",
                "id": "42",
                "title": "Skipped candidate",
                "why": "needs look",
                "since": "2026-11-28T00:00:00Z",
            }
        }
        with mock.patch.object(pipeline, "skipped_tickets", side_effect=lambda: dict(skipped_state)), self.listen() as server:
            cookie, _ = self.pair_and_client(server)
            inbox.sync_all()

            cand = inbox_store.get_open_by_subject("skipped:fixture#42")
            self.assertIsNotNone(cand)
            declined, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{cand['id']}/resolve",
                body={"base_version": cand["version"], "verb": "decline"},
                cookie=cookie,
            )
            self.assertEqual(declined["item"]["state"], "resolved")
            self.assertEqual(declined["item"]["resolution"], "declined")
            self.assertEqual(declined["item"]["answer"], "decline")

            reopened, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{cand['id']}/reopen",
                body={"base_version": declined["item"]["version"]},
                cookie=cookie,
            )
            self.assertEqual(reopened["item"]["state"], "open")
            self.assertEqual(reopened["item"]["answer"], "")
            self.assertEqual(reopened["item"]["answered_by"], "")
            self.assertEqual(reopened["item"]["answered_at"], "")
            self.assertEqual(reopened["item"]["resolution"], "")
            self.assertEqual(reopened["item"]["resolved"], "")
            self.assertEqual(reopened["item"]["priority"], cand["priority"])

            declined2, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{cand['id']}/resolve",
                body={"base_version": reopened["item"]["version"], "verb": "decline"},
                cookie=cookie,
            )
            skipped_state["fixture#42"]["since"] = "2026-11-28T01:00:00Z"
            inbox.sync_all()
            second = inbox_store.get_open_by_subject("skipped:fixture#42")
            self.assertIsNotNone(second)
            self.assertNotEqual(second["id"], cand["id"])

            conflict, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{cand['id']}/reopen",
                body={"base_version": declined2["item"]["version"]},
                cookie=cookie,
                status=409,
                code="already_open",
            )
            self.assertEqual(conflict["item"], second["id"])
            self.assertIn(second["id"], conflict["detail"])

            appr_send_id = approvals_store.add_approval(
                "demo-loop",
                "r-send-1",
                {"type": "push", "item": {"title": "Push 1", "about": "About", "files": ["a.py"]}},
                "Send approval",
            )
            appr_send = inbox_store.get_open_by_subject(f"approval:{appr_send_id}")
            self.assertIsNotNone(appr_send)
            resolved_appr, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{appr_send['id']}/resolve",
                body={"base_version": appr_send["version"], "verb": "approve"},
                cookie=cookie,
            )
            with foundation.tx() as c:
                ob_row = outbox_store.get_by_legacy_approval(c, appr_send_id)
                if ob_row is not None:
                    c.execute(
                        "UPDATE outbox SET state = 'sent', sent_at = ?, sent_ref = 'sent-ref-1' WHERE id = ?",
                        (util.iso(), ob_row["id"]),
                    )
            self.http(
                server,
                "POST",
                f"{self.INBOX}/{appr_send['id']}/reopen",
                body={"base_version": resolved_appr["item"]["version"]},
                cookie=cookie,
                status=409,
                code="already_sent",
            )

            appr_rej_id = approvals_store.add_approval(
                "demo-loop",
                "r-rej-1",
                {"type": "push", "item": {"title": "Push 2", "about": "About", "files": ["b.py"]}},
                "Reject approval",
            )
            appr_rej = inbox_store.get_open_by_subject(f"approval:{appr_rej_id}")
            self.assertIsNotNone(appr_rej)
            rejected_appr, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{appr_rej['id']}/resolve",
                body={"base_version": appr_rej["version"], "verb": "reject"},
                cookie=cookie,
            )
            self.http(
                server,
                "POST",
                f"{self.INBOX}/{appr_rej['id']}/reopen",
                body={"base_version": rejected_appr["item"]["version"]},
                cookie=cookie,
                status=409,
                code="not_reversible",
            )

            cfg_text = "max_parallel = 2\n" + paths.CONFIG_FILE.read_text(encoding="utf-8")
            draft_row, _ = drafts.create_and_check("step_settings", "defaults", cfg_text, author="hand", why="proposal test")
            prop_item = inbox_store.get_open_by_subject(f"draft:{draft_row['numeric_id']}")
            self.assertIsNotNone(prop_item)
            declined_prop, _ = self.http(
                server,
                "POST",
                f"{self.INBOX}/{prop_item['id']}/resolve",
                body={"base_version": prop_item["version"], "verb": "decline"},
                cookie=cookie,
            )
            self.http(
                server,
                "POST",
                f"{self.INBOX}/{prop_item['id']}/reopen",
                body={"base_version": declined_prop["item"]["version"]},
                cookie=cookie,
                status=409,
                code="not_reversible",
            )
            self.assertEqual(drafts.get(draft_row["id"])["state"], "declined")
            self.assertEqual(drafts.MOVES["declined"], frozenset())


class ConfigDeleteAndRollbackTests(_ReopenAndDeleteCase):
    @_case
    def test_device_delete_versions_tombstone_refusal_and_exact_rollback(self, _):
        configrepo.init()
        dev_path = paths.CONFIG_DIR / "devices" / "scratch-box.toml"
        dev_path.parent.mkdir(parents=True, exist_ok=True)
        original_bytes = b'kind = "ssh"\nhost = "scratch.example.invalid"\nuser = "builder"\n'
        dev_path.write_bytes(original_bytes)
        configrepo.commit_hand_edits()

        with self.listen() as server:
            cookie, token = self.pair_and_client(
                server,
                scopes=("read", "config:draft", "config:activate", "inbox:resolve"),
            )
            bearer = token["text"]

            before, _ = self.http(server, "GET", "/api/v1/config/device/scratch-box", bearer=bearer)
            pre_ver = before["item"]["version"]
            pre_cv = before["item"]["content_version"]
            self.assertEqual(pre_ver, 1)

            self.http(
                server,
                "DELETE",
                "/api/v1/config/device/scratch-box",
                body={"base_version": 99},
                bearer=bearer,
                status=409,
                code="version_conflict",
            )
            self.http(
                server,
                "DELETE",
                "/api/v1/config/device/scratch-box",
                body={"base_version": pre_ver, "unexpected": True},
                bearer=bearer,
                status=422,
                code="unknown_field",
            )

            del_key = "delete-device-key-1"
            deleted, _ = self.http(
                server,
                "DELETE",
                "/api/v1/config/device/scratch-box",
                body={"base_version": pre_ver, "why": "remove scratch"},
                bearer=bearer,
                key=del_key,
            )
            contract.validate(deleted, contract.BY_NAME["config.delete"]["result"])
            self.assertIsNone(deleted["draft"]["body"])
            self.assertEqual(deleted["draft"]["state"], "active")
            self.assertEqual(deleted["previous_version"], pre_ver)
            self.assertEqual(deleted["previous_content_version"], pre_cv)
            self.assertEqual(deleted["tombstone"], "device/scratch-box@deleted")
            self.assertEqual(deleted["version"], pre_ver + 1)
            self.assertFalse(dev_path.exists())

            replayed, hdrs = self.http(
                server,
                "DELETE",
                "/api/v1/config/device/scratch-box",
                body={"base_version": pre_ver, "why": "remove scratch"},
                bearer=bearer,
                key=del_key,
            )
            self.assertEqual(replayed, deleted)
            self.assertEqual(hdrs.get("Idempotent-Replayed"), "true")

            self.http(server, "GET", "/api/v1/config/device/scratch-box", bearer=bearer, status=404, code="not_found")

            vers, _ = self.http(server, "GET", "/api/v1/config/device/scratch-box/versions", bearer=bearer)
            self.assertEqual(len(vers["entries"]), 2)
            self.assertEqual(vers["entries"][0]["version"], deleted["version"])
            self.assertEqual(vers["entries"][0]["content_version"], "device/scratch-box@deleted")
            self.assertFalse(vers["entries"][0]["live"])
            self.assertEqual(vers["entries"][1]["version"], pre_ver)
            self.assertEqual(vers["entries"][1]["content_version"], pre_cv)
            self.assertFalse(vers["entries"][1]["live"])

            before_rebuild = config_store.all_rows()
            with foundation.tx() as c:
                c.execute("DELETE FROM config_index")
            configrepo.rebuild_index()
            self.assertEqual(config_store.all_rows(), before_rebuild)

            self.http(
                server,
                "POST",
                "/api/v1/config/device/scratch-box/rollback",
                body={"to": deleted["version"], "base_version": deleted["version"]},
                bearer=bearer,
                status=422,
                code="validation",
            )
            self.http(
                server,
                "POST",
                "/api/v1/config/device/scratch-box/rollback",
                body={"to": deleted["tombstone"], "base_version": deleted["version"]},
                bearer=bearer,
                status=422,
                code="validation",
            )
            self.assertFalse(dev_path.exists())

            self.http(
                server,
                "POST",
                "/api/v1/config/device/scratch-box/rollback",
                body={"to": pre_cv, "base_version": pre_ver},
                bearer=bearer,
                status=409,
                code="version_conflict",
            )

            restored, _ = self.http(
                server,
                "POST",
                "/api/v1/config/device/scratch-box/rollback",
                body={"to": pre_cv, "base_version": deleted["version"]},
                bearer=bearer,
                key="rollback-device-key-1",
            )
            self.assertEqual(restored["version"], deleted["version"] + 1)
            self.assertTrue(dev_path.is_file())
            self.assertEqual(dev_path.read_bytes(), original_bytes)

            vers_after, _ = self.http(server, "GET", "/api/v1/config/device/scratch-box/versions", bearer=bearer)
            self.assertEqual(len(vers_after["entries"]), 3)
            self.assertEqual(vers_after["entries"][0]["version"], restored["version"])
            self.assertEqual(vers_after["entries"][0]["content_version"], pre_cv)
            self.assertTrue(vers_after["entries"][0]["live"])

            with foundation.reader() as c:
                ev_rows = c.execute(
                    "SELECT ver, data FROM events_log WHERE kind = 'config.device.changed' "
                    "AND subject = 'config:device/scratch-box' ORDER BY seq"
                ).fetchall()
            self.assertGreaterEqual(len(ev_rows), 3)
            deleted_ev = json.loads(ev_rows[-2]["data"])
            self.assertTrue(deleted_ev.get("deleted"))
            self.assertEqual(deleted_ev.get("version"), deleted["version"])


class ConfigDeleteGuardsAndPresenceTests(_ReopenAndDeleteCase):
    @_case
    def test_held_device_lease_referenced_source_engine_default_and_gate_c_presence(self, _):
        configrepo.init()
        held_path = paths.CONFIG_DIR / "devices" / "held-box.toml"
        held_path.parent.mkdir(parents=True, exist_ok=True)
        held_bytes = b'kind = "ssh"\nhost = "held.example.invalid"\n'
        held_path.write_bytes(held_bytes)

        src_path = paths.CONFIG_DIR / "sources" / "feed.toml"
        src_path.parent.mkdir(parents=True, exist_ok=True)
        src_path.write_text('type = "github"\nname = "feed"\n', encoding="utf-8")

        rule_path = paths.CONFIG_DIR / "rules" / "feed-rule.toml"
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        rule_path.write_text('domain = "intake"\nsource = "feed"\n', encoding="utf-8")

        target_path = paths.CONFIG_DIR / "targets" / "custom-gh.toml"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_bytes = (
            b'connector = "github"\n'
            b'kinds = ["comment"]\n'
            b'audience = "self"\n'
            b'posts_as = "self"\n'
            b'send_window = 10\n'
        )
        configrepo.commit_hand_edits()
        configrepo.commit(
            {"targets/custom-gh.toml": target_bytes},
            {"targets/custom-gh.toml": configrepo.ABSENT},
            actor="u:owner001",
            source="dashboard",
            note="create custom-gh target",
            activate_c=True,
        )

        run_id = record.start("session", device="held-box")
        active_lease = leases.claim("held-box", devices.version_of("held-box"), run_id=run_id, start_renewer=False)

        with self.listen() as server:
            cookie, token = self.pair_and_client(
                server,
                scopes=("read", "config:draft", "config:activate", "inbox:resolve"),
            )
            bearer = token["text"]

            held_meta, _ = self.http(server, "GET", "/api/v1/config/device/held-box", bearer=bearer)
            held_ver = held_meta["item"]["version"]
            held_cv = held_meta["item"]["content_version"]

            refused, _ = self.http(
                server,
                "DELETE",
                "/api/v1/config/device/held-box",
                body={"base_version": held_ver},
                bearer=bearer,
                status=409,
                code="referenced",
            )
            self.assertIn(active_lease.label.replace("L-", "lease:L-"), refused["references"])
            self.assertIn(f"run:{run_id}", refused["references"])
            self.assertTrue(held_path.is_file())

            active_lease.close("", fenced=False)
            record.settle(run_id, "done")

            deleted_held, _ = self.http(
                server,
                "DELETE",
                "/api/v1/config/device/held-box",
                body={"base_version": held_ver},
                bearer=bearer,
            )
            self.assertFalse(held_path.exists())
            restored_held, _ = self.http(
                server,
                "POST",
                "/api/v1/config/device/held-box/rollback",
                body={"to": held_cv, "base_version": deleted_held["version"]},
                bearer=bearer,
            )
            self.assertEqual(restored_held["version"], deleted_held["version"] + 1)
            self.assertEqual(held_path.read_bytes(), held_bytes)

            src_meta, _ = self.http(server, "GET", "/api/v1/config/source/feed", bearer=bearer)
            src_refused, _ = self.http(
                server,
                "DELETE",
                "/api/v1/config/source/feed",
                body={"base_version": src_meta["item"]["version"]},
                bearer=bearer,
                status=409,
                code="referenced",
            )
            self.assertIn("rule:feed-rule", src_refused["references"])
            self.assertTrue(src_path.is_file())

            for kind, name in (("device", "local"), ("target", "github"), ("library", "adw")):
                self.http(
                    server,
                    "DELETE",
                    f"/api/v1/config/{kind}/{name}",
                    body={"base_version": 0},
                    bearer=bearer,
                    status=409,
                    code="engine_default",
                )

            tgt_meta, _ = self.http(server, "GET", "/api/v1/config/target/custom-gh", bearer=bearer)
            tgt_ver = tgt_meta["item"]["version"]
            tgt_cv = tgt_meta["item"]["content_version"]
            del_key = "gate-c-delete-key-1"
            deferred, _ = self.http(
                server,
                "DELETE",
                "/api/v1/config/target/custom-gh",
                body={"base_version": tgt_ver},
                bearer=bearer,
                key=del_key,
                status=202,
            )
            self.assertEqual(deferred["state"], "awaiting_presence")
            self.assertTrue(target_path.is_file())

            pres_item, _ = self.http(server, "GET", f"{self.INBOX}/{deferred['item']}", cookie=cookie)
            self.http(
                server,
                "POST",
                f"{self.INBOX}/{deferred['item']}/resolve",
                body={"base_version": pres_item["item"]["version"], "verb": "approve"},
                cookie=cookie,
            )
            replayed_del, _ = self.http(
                server,
                "DELETE",
                "/api/v1/config/target/custom-gh",
                body={"base_version": tgt_ver},
                bearer=bearer,
                key=del_key,
            )
            self.assertFalse(target_path.exists())
            self.assertEqual(replayed_del["previous_content_version"], tgt_cv)

            rb_key = "gate-c-rollback-key-1"
            rb_deferred, _ = self.http(
                server,
                "POST",
                "/api/v1/config/target/custom-gh/rollback",
                body={"to": tgt_cv, "base_version": replayed_del["version"]},
                bearer=bearer,
                key=rb_key,
                status=202,
            )
            self.assertEqual(rb_deferred["state"], "awaiting_presence")
            self.assertFalse(target_path.exists())

            rb_pres, _ = self.http(server, "GET", f"{self.INBOX}/{rb_deferred['item']}", cookie=cookie)
            self.http(
                server,
                "POST",
                f"{self.INBOX}/{rb_deferred['item']}/resolve",
                body={"base_version": rb_pres["item"]["version"], "verb": "approve"},
                cookie=cookie,
            )
            replayed_rb, _ = self.http(
                server,
                "POST",
                "/api/v1/config/target/custom-gh/rollback",
                body={"to": tgt_cv, "base_version": replayed_del["version"]},
                bearer=bearer,
                key=rb_key,
            )
            self.assertEqual(replayed_rb["version"], replayed_del["version"] + 1)
            self.assertEqual(target_path.read_bytes(), target_bytes)

            with self.assertRaises(contract.Problem) as ctx:
                drafts.delete_item("device", "held-box", restored_held["version"], "run:pip-1")
            self.assertEqual(ctx.exception.code, "forbidden")


class ConfigDraftNullBodyMigrationTests(_ReopenAndDeleteCase):
    @_case
    def test_m0061_migration_allows_null_body_and_preserves_existing_drafts(self, _):
        for suffix in ("", "-wal", "-shm", "-journal"):
            paths.DB_FILE.with_name(paths.DB_FILE.name + suffix).unlink(missing_ok=True)
        foundation._CHECKED.clear()
        empty_snaps = Path.cwd() / "empty-snaps"
        empty_snaps.mkdir(exist_ok=True)
        chain = foundation.migrations.load()
        with mock.patch.object(foundation.migrations, "load", return_value=chain[:60]):
            foundation.migrate(workspace_snapshots=empty_snaps)
        did = drafts.add(kind="device", name="keep", body='kind = "local"\n', author="hand", gate="B")
        with foundation.tx() as c, self.assertRaises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO config_drafts (kind, name, body, gate, created, updated) VALUES (?,?,NULL,'B',?,?)",
                ("device", "fail", util.iso(), util.iso()),
            )
        foundation.migrate(workspace_snapshots=empty_snaps)
        kept = drafts.get(did)
        self.assertIsNotNone(kept)
        self.assertEqual(kept["body"], 'kind = "local"\n')
        null_id = drafts.add(kind="device", name="gone", body=None, author="hand", gate="B")
        null_row = drafts.get(null_id)
        self.assertIsNotNone(null_row)
        self.assertIsNone(null_row["body"])
