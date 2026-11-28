from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from arete import api_check
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_isolated(script: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="arete-money-test-") as tmp:
        root = Path(tmp).resolve()
        env = api_check.isolated_env(root)
        cmd = api_check.child_command(REPO_ROOT, script)
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=CHILD_HANG_TIMEOUT,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"child failed ({proc.returncode})\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("MONEY_RESULT="):
                return json.loads(line.removeprefix("MONEY_RESULT="))
        raise AssertionError(f"missing MONEY_RESULT\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


class MoneyExpandAndWritersTests(unittest.TestCase):
    def test_writers_jev_provenance_and_mirror_triggers(self) -> None:
        script = r"""
import json
import sqlite3
from unittest import mock

from arete import agent, api_check, guard, jev, paths, prices, record, util
from arete.store import chats as chats_store
from arete.store import db as foundation
from arete.store import insights as insights_store
from arete.store import runs as run_store
from arete.store import tasks as task_store
from arete.store import triage as triage_store

root = paths.CONFIG_DIR.resolve()
lines, _ = api_check.prepare_replay(root)
paths.CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

# Migrate through expand (m0072) to test mirror triggers for older writers.
foundation.migrate(through=foundation.MONEY_EXPAND_SCHEMA)
tid_label = task_store.task_add("replay-sample", "Seed task", title="Seed task")
tid_num = int(task_store.task_get(tid_label)["num"])
with foundation.reader() as c:
    ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert ver == foundation.MONEY_EXPAND_SCHEMA
    row = c.execute(
        "SELECT name FROM schema_migrations WHERE n = ?",
        (foundation.MONEY_EXPAND_SCHEMA,),
    ).fetchone()
    assert row is not None and row[0] == "m0072_runs_money_expand.py"

# Simulate an older writer writing only REAL columns across expanded tables.
with foundation.tx() as c:
    c.execute(
        "INSERT INTO runs (id, kind, status, started, cost_usd, task_id) "
        "VALUES ('old-r1', 'pipeline', 'done', '2026-11-28T00:00:00Z', 1.25, ?)",
        (tid_num,),
    )
    c.execute(
        "INSERT INTO steps (run_id, step, runner, ok, cost_usd, ts) "
        "VALUES ('old-r1', 'plan', 'claude', 1, 0.5, '2026-11-28T00:00:01Z')"
    )
    c.execute("UPDATE runs SET cost_usd = 2.75 WHERE id = 'old-r1'")
    c.execute(
        "INSERT INTO evals (run_id, repo, task, passed, seconds, cost_usd, ts) "
        "VALUES ('old-r1', 'r', 't1', 1, 1.0, 0.3, '2026-11-28T00:00:02Z')"
    )
    c.execute(
        "INSERT INTO triage_verdicts (ref, at, cost) VALUES ('PROJ-1', '2026-11-28T00:00:03Z', 0.12)"
    )
    c.execute("INSERT INTO research (ref, status, cost) VALUES ('PROJ-2', 'done', 0.45)")
    cid = c.execute("INSERT INTO chats (project, spent) VALUES ('r', 0.2)").lastrowid
    c.execute(
        "INSERT INTO chat_messages (chat_id, sender, text, cost, paid_usd, ts) "
        "VALUES (?, 'arete', 'hi', 0.2, 0.15, '2026-11-28T00:00:04Z')",
        (cid,),
    )
    cap_id = c.execute("INSERT INTO captures (status, jev_cost_usd, inspect_cost_usd) VALUES ('draft', 0.1, 0.2)").lastrowid
    assert c.execute("SELECT cost_nusd FROM runs WHERE id = 'old-r1'").fetchone()[0] == 2_750_000_000
    assert c.execute("SELECT cost_nusd FROM steps WHERE run_id = 'old-r1'").fetchone()[0] == 500_000_000
    assert c.execute("SELECT cost_nusd FROM evals WHERE run_id = 'old-r1'").fetchone()[0] == 300_000_000
    assert c.execute("SELECT cost_nusd FROM triage_verdicts WHERE ref = 'PROJ-1'").fetchone()[0] == 120_000_000
    assert c.execute("SELECT cost_nusd FROM research WHERE ref = 'PROJ-2'").fetchone()[0] == 450_000_000
    assert c.execute("SELECT spent_nusd FROM chats WHERE id = ?", (cid,)).fetchone()[0] == 200_000_000
    msg_row = c.execute("SELECT cost_nusd, paid_nusd FROM chat_messages WHERE chat_id = ?", (cid,)).fetchone()
    assert tuple(msg_row) == (200_000_000, 150_000_000)
    cap_row = c.execute("SELECT jev_cost_nusd, inspect_cost_nusd FROM captures WHERE id = ?", (cap_id,)).fetchone()
    assert tuple(cap_row) == (100_000_000, 200_000_000)

# Now complete migration to LATEST.
foundation.migrate()
with foundation.reader() as c:
    assert c.execute("PRAGMA user_version").fetchone()[0] == foundation.LATEST

# JEV provider usage.cost vs built-in fallback rate provenance
run_id = record.start("pipeline", repo="replay-sample", issue_key="1", task_id=tid_num)
prov_reply = jev.parse_usage({"model": "jev-m", "usage": {"input_tokens": 500, "cost": 0.0025}})
assert prov_reply.price_version == "provider"
record.step(run_id, "jev-prov", jev.result([prov_reply]), 0.1)

fb_reply = jev.parse_usage({"model": "jev-m", "usage": {"input_tokens": 348}})
assert fb_reply.price_version == prices.JEV_BUILTIN_PRICE_VERSION
record.step(run_id, "jev-fb", jev.result([fb_reply]), 0.1)

# Guard metering write boundary
with mock.patch.dict("os.environ", {"ARETE_RUN_ID": run_id}):
    guard.kept_out("Read", ".env")

steps = run_store.steps(run_id)
by_step = {s["step"]: s for s in steps}
assert by_step["jev-prov"]["auth_mode"] == "api_key"
assert by_step["jev-prov"]["price_version"] == "provider"
assert by_step["jev-prov"]["cost_nusd"] == 2_500_000
assert by_step["jev-prov"]["paid_nusd"] == 2_500_000

assert by_step["jev-fb"]["auth_mode"] == "api_key"
assert by_step["jev-fb"]["price_version"] == prices.JEV_BUILTIN_PRICE_VERSION
assert by_step["jev-fb"]["cost_nusd"] == 14_616
assert by_step["jev-fb"]["paid_nusd"] == 14_616

assert by_step["guard"]["cost_nusd"] == 0
assert by_step["guard"]["paid_nusd"] == 0
assert by_step["guard"]["price_version"] == "guard"

run_after = run_store.run_get(run_id)
assert run_after["cost_nusd"] == 2_514_616

# Transaction rollback keeps totals coherent
try:
    with foundation.tx() as c:
        record.step(run_id, "rolled", agent.Result(ok=True, runner="claude", cost_usd=1.0, auth_mode="api_key"), 0.1)
        raise RuntimeError("boom")
except RuntimeError:
    pass
assert run_store.run_get(run_id)["cost_nusd"] == 2_514_616
assert len(run_store.steps(run_id)) == 3

# Other write boundaries: chat_say, add_eval, record_verdict, research_set/keep, capture_check_done/inspected
chat_id = chats_store.chat_add("replay-sample")
chats_store.chat_say(chat_id, "arete", "hello", 0.1, paid_usd=0.1)
chats_store.chat_say(chat_id, "arete", "unclassified", 0.2, paid_usd=None)
with foundation.reader() as c:
    assert c.execute("SELECT spent_nusd FROM chats WHERE id = ?", (chat_id,)).fetchone()[0] == 300_000_000
    mrows = c.execute("SELECT cost_nusd, paid_nusd FROM chat_messages WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()
    assert [tuple(r) for r in mrows] == [(100_000_000, 100_000_000), (200_000_000, None)]

insights_store.add_eval(run_id, "replay-sample", "t-eval", True, 2.0, 0.123)
with foundation.reader() as c:
    assert c.execute("SELECT cost_nusd FROM evals WHERE task = 't-eval'").fetchone()[0] == 123_000_000

triage_store.record_verdict({"ref": "T-99", "source": "s", "tracker": "github", "cost": 0.05})
with foundation.reader() as c:
    assert c.execute("SELECT cost_nusd FROM triage_verdicts WHERE ref = 'T-99'").fetchone()[0] == 50_000_000

insights_store.research_set("T-99", status="done", cost=0.25)
insights_store.research_keep("T-99", cost=0.35)
with foundation.reader() as c:
    assert c.execute("SELECT cost_nusd FROM research WHERE ref = 'T-99'").fetchone()[0] == 350_000_000

with foundation.tx() as c:
    cap_id2 = c.execute("INSERT INTO captures (status) VALUES ('checking')").lastrowid
    ev_id = c.execute(
        "INSERT INTO capture_evidence (capture, name, kind, type, bytes, sha256, source, seconds, added) "
        "VALUES (?, 'shot.png', 'image', 'image/png', 10, 'abc', 'ui', 0, '2026-11-28T00:00:00Z')",
        (cap_id2,),
    ).lastrowid
for i in range(10):
    assert insights_store.capture_begin_checks(cap_id2, {"dup": f"p{i}"})
    assert insights_store.capture_check_done(cap_id2, "dup", "pass", "ok", {}, 0.1)
assert insights_store.capture_inspected(ev_id, "abc", {"ok": True}, 0.25)
with foundation.reader() as c:
    row = c.execute("SELECT jev_cost_nusd, inspect_cost_nusd FROM captures WHERE id = ?", (cap_id2,)).fetchone()
    assert tuple(row) == (1_000_000_000, 250_000_000)

print("MONEY_RESULT=" + json.dumps({"ok": True}))
"""
        self.assertTrue(_run_isolated(script)["ok"])

    def test_pinned_reference_billing_isolation_and_interrupted_recovery(self) -> None:
        script = r"""
import json
from datetime import UTC, datetime
from pathlib import Path

from arete import agent, api_check, config, configrepo, drafts, paths, pins, principals, prices, record, runner, util
from arete.store import db as foundation
from arete.store import runs as run_store
from arete.store import tasks as task_store

root = paths.CONFIG_DIR.resolve()
lines, _ = api_check.prepare_replay(root)
paths.CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
foundation.migrate()
configrepo.init()
principals.seed_owner(api_check.LOGIN)
owner_id = principals.owner()
tid_label = task_store.task_add("replay-sample", "Seed task", title="Seed task")
tid_num = int(task_store.task_get(tid_label)["num"])

# 1. Migrate legacy snapshot to prices/models.toml and materialize prices/billing.toml
snap_v1 = prices.parse({
    "schema": 1,
    "source": "catalog-v1",
    "observed": "2026-11-20T00:00:00Z",
    "routes": [{
        "route": "openrouter/model-a",
        "kind": "marketplace",
        "provider": "openrouter",
        "model": "model-a",
        "currency": "USD",
        "unit": "per_million_tokens",
        "rates": [{"input": 2.0, "output": 4.0}],
    }],
})
prices.store(snap_v1)
models_v1_id = prices.migrate_snapshot()
assert models_v1_id and models_v1_id.startswith("prices/models@")
# Idempotent second migration
assert prices.migrate_snapshot() == models_v1_id

billing_v1_toml = prices.render_billing_toml(
    {
        "openrouter/model-a": (10.0, 20.0),
        "pi:openrouter/model-b": (5.0, 15.0),
    },
    local_providers=["ollama"],
)
with configrepo.writer_lock() as fence:
    _, base_blob = configrepo.read(prices.BILLING_REL_PATH)
    configrepo.commit(
        {prices.BILLING_REL_PATH: billing_v1_toml},
        {prices.BILLING_REL_PATH: base_blob},
        actor=owner_id,
        source="hand",
        note="seed billing v1",
        fence=fence,
    )
billing_v1 = prices.load_billing()
assert billing_v1.content_version.startswith("prices/billing@")

# Pin run 1 against v1 rates
pin1 = pins.resolve("pipeline", repo="replay-sample")
assert pin1.prices["id"] == models_v1_id
assert pin1.billing_prices["id"] == billing_v1.content_version

# Now activate DOUBLED reference rates and DOUBLED billing rates BEFORE pin1 runs usage!
snap_v2 = prices.parse({
    "schema": 1,
    "source": "catalog-v2",
    "observed": "2026-11-25T00:00:00Z",
    "routes": [{
        "route": "openrouter/model-a",
        "kind": "marketplace",
        "provider": "openrouter",
        "model": "model-a",
        "currency": "USD",
        "unit": "per_million_tokens",
        "rates": [{"input": 4.0, "output": 8.0}],
    }],
})
imported_v2 = prices.import_models(snap_v2, actor=owner_id)
models_v2_id = imported_v2["content_version"]
assert models_v2_id != models_v1_id

billing_v2_toml = prices.render_billing_toml(
    {
        "openrouter/model-a": (20.0, 40.0),
        "pi:openrouter/model-b": (10.0, 30.0),
    },
    local_providers=["ollama", "openrouter"],
)
live_row = drafts._current_ver_row("prices", "billing", prices.BILLING_REL_PATH)
base_ver = int(live_row["ver"]) if live_row else 0
draft_obj, report = drafts.create_and_check("prices", "billing", billing_v2_toml, author=owner_id, why="double billing", base_version=base_ver)
assert report and report["ok"]
drafts.activate(draft_obj["id"], actor=owner_id, base_version=base_ver)
billing_v2 = prices.load_billing()
assert billing_v2.content_version != billing_v1.content_version

# Verify invalid draft is refused and mirror stays at v2
bad_snap = dict(snap_v2)
bad_snap["observed"] = "2099-01-01T00:00:00Z"
try:
    prices.import_models(bad_snap, actor=owner_id)
    raise AssertionError("expected refusal for future observation")
except ValueError:
    pass
assert prices.load_snapshot_file()["version"] == snap_v2["version"]

# Execute pricing under pin1 -> MUST use original v1 rates for BOTH reference quote and billing!
at = datetime(2026, 11, 28, 12, 0, 0, tzinfo=UTC)
with pins.use(pin1):
    res1 = agent.Result(ok=True, runner="pi", model="openrouter/model-a", provider="openrouter",
                        auth_mode="api_key", tokens_in=1_000_000, tokens_out=1_000_000, cost_usd=999.0)
    out1 = runner.price_result(res1, pin1.cfg)
    assert out1.cost_nusd == 30_000_000_000  # 10 + 20 from billing v1
    assert out1.paid_nusd == 30_000_000_000
    assert out1.price_version == billing_v1.content_version
    runner.quote_calls(res1, "pi", "openrouter/model-a", at)
    assert res1.reference_usd == 6.0  # 2 + 4 from models v1
    assert res1.reference_price_version == models_v1_id

    # runner:model override
    res_rm = agent.Result(ok=True, runner="pi", model="openrouter/model-b", provider="openrouter",
                          auth_mode="api_key", tokens_in=1_000_000, tokens_out=0, cost_usd=1.0)
    out_rm = runner.price_result(res_rm, pin1.cfg)
    assert out_rm.cost_nusd == 5_000_000_000 and out_rm.price_version == billing_v1.content_version

    # pi subscription keeps provider amount, paid=0
    res_ps = agent.Result(ok=True, runner="pi", model="openrouter/model-a", provider="openrouter",
                          auth_mode="subscription", tokens_in=1_000_000, tokens_out=0, cost_usd=1.5)
    out_ps = runner.price_result(res_ps, pin1.cfg)
    assert out_ps.cost_nusd == 1_500_000_000 and out_ps.paid_nusd == 0 and out_ps.price_version == "provider"

    # selected-local -> 0/0/local
    out_loc = runner.price_result(res1, pin1.cfg, selected_local=True)
    assert out_loc.cost_nusd == 0 and out_loc.paid_nusd == 0 and out_loc.price_version == "local"

    # local pi provider without override -> 0/0/local
    res_ol = agent.Result(ok=True, runner="pi", model="ollama/llama3", provider="ollama",
                          auth_mode="api_key", tokens_in=1000, tokens_out=500, cost_usd=0.5)
    out_ol = runner.price_result(res_ol, pin1.cfg)
    assert out_ol.cost_nusd == 0 and out_ol.paid_nusd == 0 and out_ol.price_version == "local"

    # generic replay retains recorded amount, paid=0, price_version='replay'
    res_rp = agent.Result(ok=True, runner="replay", model="openrouter/model-a", auth_mode="api_key", cost_usd=0.42)
    out_rp = runner.price_result(res_rp, pin1.cfg)
    assert out_rp.cost_nusd == 420_000_000 and out_rp.paid_nusd == 0 and out_rp.price_version == "replay"

# New pin2 uses doubled v2 rates!
pin2 = pins.resolve("pipeline", repo="replay-sample")
assert pin2.prices["id"] == models_v2_id
assert pin2.billing_prices["id"] == billing_v2.content_version
with pins.use(pin2):
    res2 = agent.Result(ok=True, runner="pi", model="openrouter/model-a", provider="openrouter",
                        auth_mode="api_key", tokens_in=1_000_000, tokens_out=1_000_000, cost_usd=999.0)
    out2 = runner.price_result(res2, pin2.cfg)
    assert out2.cost_nusd == 60_000_000_000  # 20 + 40 from billing v2
    assert out2.price_version == billing_v2.content_version
    runner.quote_calls(res2, "pi", "openrouter/model-a", at)
    assert res2.reference_usd == 12.0  # 4 + 8 from models v2
    assert res2.reference_price_version == models_v2_id

# 2. SIGKILL / interrupted recovery from turn_index with frozen billing_context
# Even after live billing policy changed to v2 (with openrouter marked local!),
# interrupted recovery of a run started under billing v1 rebuilds exact v1 charges!
killed_run = record.start("pipeline", repo="replay-sample", issue_key="2", task_id=tid_num)
sid = run_store.queue_step(killed_run, "build", 1)
bctx = prices.billing_context_for(
    runner="pi",
    model="openrouter/model-a",
    provider="openrouter",
    auth_mode="api_key",
    account_id=None,
    payer=owner_id,
    selected_local=False,
    policy=billing_v1,
    reference_meta=pin1.prices,
)
run_store.start_step(sid, account_id=None, device="local", runner="pi", model="openrouter/model-a",
                     provider="openrouter", auth_mode="api_key")
run_store.set_step_billing_context(sid, json.dumps(bctx, sort_keys=True, separators=(",", ":")))

# Write partial pi turn directly to record file (simulating SIGKILL before Result/record.step)
rec = record.Recorder(
    killed_run,
    "build",
    {"runner": "pi", "model": "openrouter/model-a", "provider": "openrouter",
     "auth_mode": "api_key", "billing_context": bctx},
    step_id=sid,
)
rec.line(json.dumps({
    "type": "message_end",
    "message": {
        "role": "assistant",
        "provider": "openrouter",
        "model": "model-a",
        "content": [{"type": "text", "text": "halfway"}],
        "usage": {"input": 500_000, "output": 250_000, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 99.0}},
    },
}))
rec.close()

# Wipe turn_index (simulating index loss before restart), then rebuild + settle!
run_store.clear_turn_index(run_id=killed_run)
assert run_store.step_turns(sid) == []
settled = record.settle(killed_run, "interrupted", end_line="interrupted")
assert settled is not None

step_after = next(s for s in run_store.steps(killed_run) if s["id"] == sid)
# Under frozen billing v1: 0.5M * $10 + 0.25M * $20 = $10 = 10_000_000_000 nUSD
assert step_after["cost_nusd"] == 10_000_000_000
assert step_after["paid_nusd"] == 10_000_000_000
assert step_after["auth_mode"] == "api_key"
assert step_after["price_version"] == billing_v1.content_version
assert run_store.run_get(killed_run)["cost_nusd"] == 10_000_000_000

# Repeating settle and rebuild_turn_index is strictly idempotent
ver_before = run_store.run_get(killed_run)["version"]
assert record.settle(killed_run, "interrupted") is None
record.rebuild_turn_index(killed_run)
assert run_store.run_get(killed_run)["cost_nusd"] == 10_000_000_000
assert run_store.run_get(killed_run)["version"] == ver_before

# Interrupted step with zero usage adds zero cost
zero_run = record.start("pipeline", repo="replay-sample", issue_key="3", task_id=tid_num)
zsid = run_store.queue_step(zero_run, "plan", 1)
run_store.start_step(zsid, account_id=None, device="local", runner="claude", model="sonnet",
                     provider="anthropic", auth_mode="subscription")
assert record.settle(zero_run, "interrupted") is not None
zstep = next(s for s in run_store.steps(zero_run) if s["id"] == zsid)
assert zstep["cost_nusd"] == 0 and zstep["paid_nusd"] == 0
assert run_store.run_get(zero_run)["cost_nusd"] in (0, None)

print("MONEY_RESULT=" + json.dumps({"ok": True}))
"""
        self.assertTrue(_run_isolated(script)["ok"])


class MoneyBackfillAndInsightTests(unittest.TestCase):
    def test_backfill_freezes_paid_classification_and_legacy_corrections(self) -> None:
        script = r"""
import importlib
import json
from datetime import timedelta

from arete import agent, api_check, board, control, paths, util
from arete.store import db as foundation
from arete.store import runs as run_store
from arete.store import tasks as task_store

root = paths.CONFIG_DIR.resolve()
lines, _ = api_check.prepare_replay(root)
lines.extend(["", "[pricing]", 'local_providers = ["ollama"]'])
paths.CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

# Migrate only through m0071 (before money expand/backfill)
foundation.migrate(through=foundation.MONEY_EXPAND_SCHEMA - 1)
tid_label = task_store.task_add("replay-sample", "Seed task", title="Seed task")
tid_num = int(task_store.task_get(tid_label)["num"])
tid_legacy_label = task_store.task_add("replay-sample", "Legacy task", title="Legacy task")
tid_legacy_num = int(task_store.task_get(tid_legacy_label)["num"])

now = util.now()
ts_recent = util.iso(now - timedelta(hours=2))
ts_legacy = util.iso(now - timedelta(days=3))

with foundation.tx() as c:
    # A loop run + pipeline run with empty auth_mode across runners
    c.execute(
        "INSERT INTO runs (id, kind, repo, issue_key, status, started, finished, cost_usd, task_id) "
        "VALUES ('r-mix', 'pipeline', 'replay-sample', '10', 'merged', ?, ?, 1.0, ?)",
        (ts_recent, ts_recent, tid_num),
    )
    c.execute(
        "INSERT INTO runs (id, kind, repo, loop, status, started, finished, cost_usd) "
        "VALUES ('r-loop', 'loop', 'replay-sample', 'scout', 'done', ?, ?, 0.25)",
        (ts_recent, ts_recent),
    )
    # Legacy run that started before any steps row
    c.execute(
        "INSERT INTO runs (id, kind, repo, issue_key, status, started, finished, cost_usd, task_id) "
        "VALUES ('r-legacy', 'pipeline', 'replay-sample', '11', 'done', ?, ?, 3.5, ?)",
        (ts_legacy, ts_legacy, tid_legacy_num),
    )
    c.executemany(
        "INSERT INTO steps (run_id, step, node, runner, provider, model, auth_mode, ok, status, cost_usd, ts, ab, ab_side, atom) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'done', ?, ?, ?, ?, ?)",
        [
            ("r-mix", "plan", "plan", "claude", "anthropic", "claude-sonnet", "", 0.40, ts_recent, "exp1", "arm", "plan"),
            ("r-mix", "build", "build", "codex", "chatgpt", "gpt-5", "", 0.20, ts_recent, "exp1", "arm", "build"),
            ("r-mix", "local", "local", "pi", "ollama", "ollama/qwen", "", 0.05, ts_recent, None, None, None),
            ("r-mix", "or1", "or1", "pi", "openrouter", "openrouter/m1", "", 0.15, ts_recent, None, None, None),
            ("r-mix", "jev", "jev", "jev", "openrouter", "jev-m", "", 0.05, ts_recent, None, None, None),
            ("r-mix", "guard", "guard", "guard", "", "", "", 0.0, ts_recent, None, None, None),
            ("r-mix", "or2", "or2", "pi", "openrouter", "openrouter/m2", "api_key", 0.15, ts_recent, None, None, None),
            ("r-loop", "loop", "loop", "pi", "openrouter", "openrouter/m1", "", 0.25, ts_recent, None, None, None),
        ],
    )

since = util.iso(now - timedelta(days=1))
before_paid_by_run = run_store.paid_by_run(["r-mix", "r-loop"])
before_loop_spent = run_store.loop_spent_since(since, "scout")
before_paid_since = control.paid_since(since)["total_usd"]
before_route = control.step_costs("route", days=1)
before_ab = control.ab_report("exp1", days=7)
before_run_board = board.run_board("r-mix")["models"]

# Apply m0070 + m0071
foundation.migrate()
assert foundation.MONEY_INTEGER_READERS_CAPABILITY in foundation.PROCESS_CAPABILITIES

with foundation.reader() as c:
    # Every step now has non-NULL paid_nusd and price_version
    missing = c.execute("SELECT COUNT(*) FROM steps WHERE paid_nusd IS NULL OR price_version IS NULL").fetchone()[0]
    assert missing == 0
    # Legacy run got a single legacy step with price_version='legacy'
    leg = c.execute("SELECT cost_nusd, paid_nusd, price_version, auth_mode FROM steps WHERE run_id = 'r-legacy'").fetchall()
    assert len(leg) == 1
    assert tuple(leg[0]) == (3_500_000_000, 0, "legacy", "subscription")
    # Guard row kept its empty auth_mode
    g_auth = c.execute("SELECT auth_mode, paid_nusd, price_version FROM steps WHERE run_id = 'r-mix' AND runner = 'guard'").fetchone()
    assert tuple(g_auth) == ("", 0, "inferred")

# Idempotent second legacy backfill adds nothing
assert run_store.backfill_legacy_cost() == 0

# Verify --before FILE produces legacy + signed correction rows and is idempotent
ts_earlier = util.iso(now - timedelta(days=45))
with foundation.tx() as c:
    c.execute(
        "INSERT INTO runs (id, kind, repo, issue_key, status, started, finished, cost_usd, cost_nusd, task_id) "
        "VALUES ('r-repriced', 'pipeline', 'replay-sample', '12', 'done', ?, ?, 2.0, 2000000000, ?)",
        (ts_earlier, ts_earlier, tid_num),
    )
added = run_store.backfill_legacy_cost({"r-repriced": 5.0})
assert added == 2
assert run_store.backfill_legacy_cost({"r-repriced": 5.0}) == 0
with foundation.reader() as c:
    rp_steps = [dict(r) for r in c.execute("SELECT runner, cost_nusd, price_version FROM steps WHERE run_id = 'r-repriced' ORDER BY id")]
    assert [(s["runner"], s["cost_nusd"], s["price_version"]) for s in rp_steps] == [
        ("legacy", 5_000_000_000, "legacy"),
        ("correction", -3_000_000_000, "correction"),
    ]

# Compare post-migration values against pre-migration values
assert abs(run_store.paid_by_run(["r-mix", "r-loop"])["r-mix"] - before_paid_by_run["r-mix"]) < 1e-9
assert abs(run_store.paid_by_run(["r-mix", "r-loop"])["r-loop"] - before_paid_by_run["r-loop"]) < 1e-9
assert abs(run_store.loop_spent_since(since, "scout") - before_loop_spent) < 1e-9
assert abs(control.paid_since(since)["total_usd"] - before_paid_since) < 1e-9
after_route = control.step_costs("route", days=1)
assert abs(after_route["paid_usd"] - before_route["paid_usd"]) < 1e-9
assert abs(after_route["subscription_usd"] - before_route["subscription_usd"]) < 1e-9
after_ab = control.ab_report("exp1", days=7)
assert abs(after_ab[0]["sides"]["arm"]["paid_usd"] - before_ab[0]["sides"]["arm"]["paid_usd"]) < 1e-9
assert abs(after_ab[0]["sides"]["arm"]["subscription_usd"] - before_ab[0]["sides"]["arm"]["subscription_usd"]) < 1e-9
after_rb = board.run_board("r-mix")["models"]
assert {(m["model"], round(m["paid"], 6)) for m in after_rb} == {(m["model"], round(m["paid"], 6)) for m in before_run_board}

# Now empty SUBSCRIPTION_RUNNERS & KEY_PROVIDERS, reload modules, and mark openrouter local -> numbers MUST NOT CHANGE!
agent.SUBSCRIPTION_RUNNERS = frozenset()
agent.KEY_PROVIDERS = frozenset()
paths.CONFIG_FILE.write_text(
    paths.CONFIG_FILE.read_text(encoding="utf-8").replace(
        'local_providers = ["ollama"]',
        'local_providers = ["ollama", "openrouter"]',
    ),
    encoding="utf-8",
)
importlib.reload(run_store)
importlib.reload(control)
importlib.reload(board)

assert abs(run_store.paid_by_run(["r-mix", "r-loop"])["r-mix"] - before_paid_by_run["r-mix"]) < 1e-9
assert abs(run_store.loop_spent_since(since, "scout") - before_loop_spent) < 1e-9
assert abs(control.paid_since(since)["total_usd"] - before_paid_since) < 1e-9
after_route2 = control.step_costs("route", days=1)
assert abs(after_route2["paid_usd"] - before_route["paid_usd"]) < 1e-9
assert abs(after_route2["subscription_usd"] - before_route["subscription_usd"]) < 1e-9
after_ab2 = control.ab_report("exp1", days=7)
assert abs(after_ab2[0]["sides"]["arm"]["paid_usd"] - before_ab[0]["sides"]["arm"]["paid_usd"]) < 1e-9
assert abs(after_ab2[0]["sides"]["arm"]["subscription_usd"] - before_ab[0]["sides"]["arm"]["subscription_usd"]) < 1e-9
after_rb2 = board.run_board("r-mix")["models"]
assert {(m["model"], round(m["paid"], 6)) for m in after_rb2} == {(m["model"], round(m["paid"], 6)) for m in before_run_board}

print("MONEY_RESULT=" + json.dumps({"ok": True}))
"""
        self.assertTrue(_run_isolated(script)["ok"])

    def test_exact_order_independence_insights_routes_etag_and_repricing(self) -> None:
        script = r"""
import io
import json
import threading
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from arete import (
    agent,
    api_check,
    api_http,
    board,
    cli,
    codebase,
    configrepo,
    credential_files,
    dashboard,
    insights,
    jev,
    paths,
    prices,
    principals,
    record,
    tailnet,
)
from arete.store import db as foundation
from arete.store import runs as run_store
from arete.store import tasks as task_store
from arete.store import tokens

root = paths.CONFIG_DIR.resolve()
lines, _ = api_check.prepare_replay(root)
paths.CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
foundation.migrate()
configrepo.init()
principals.seed_owner(api_check.LOGIN)
tokens.init_owner()
token = credential_files.read_client(foundation.schema_metadata()["instance_tag"])
codebase.load()
tid_label = task_store.task_add("replay-sample", "Seed task", title="Seed task")
tid_num = int(task_store.task_get(tid_label)["num"])

# Record the exact #588 sequence in forward order and in reverse order
jev_reply = jev.parse_usage({"model": "jev-m", "usage": {"input_tokens": 348}})
jev_res = jev.result([jev_reply])
amounts = [
    *[agent.Result(ok=True, runner="pi", model="openrouter/m", provider="openrouter", auth_mode="api_key", cost_usd=0.1,
                   tokens_in=100_000, tokens_out=0) for _ in range(10)],
    agent.Result(ok=True, runner="pi", model="openrouter/m", provider="openrouter", auth_mode="api_key",
                 cost_usd=10.790286799999999, tokens_in=100_000, tokens_out=0),
    agent.Result(ok=True, runner="pi", model="openrouter/m", provider="openrouter", auth_mode="api_key",
                 cost_usd=0.00040698000000000004, tokens_in=100_000, tokens_out=0),
    jev_res,
]

r_fwd = record.start("pipeline", repo="replay-sample", issue_key="100", task_id=tid_num)
for i, item in enumerate(amounts):
    record.step(r_fwd, f"s{i}", item, 1.0)

r_rev = record.start("pipeline", repo="replay-sample", issue_key="101", task_id=tid_num)
for i, item in enumerate(reversed(amounts)):
    record.step(r_rev, f"s{i}", item, 1.0)

expected_nusd = 11_790_708_396
assert run_store.run_get(r_fwd)["cost_nusd"] == expected_nusd
assert run_store.run_get(r_rev)["cost_nusd"] == expected_nusd
with foundation.reader() as c:
    assert c.execute("SELECT SUM(cost_nusd) FROM steps WHERE run_id = ?", (r_fwd,)).fetchone()[0] == expected_nusd
    assert c.execute("SELECT SUM(cost_nusd) FROM steps WHERE run_id = ?", (r_rev,)).fetchone()[0] == expected_nusd

# Import reference catalogue v1 and v2 (doubled) via `arete prices import FILE`
snap1 = {
    "schema": 1,
    "source": "ref-v1",
    "observed": "2026-11-20T00:00:00Z",
    "routes": [{
        "route": "openrouter/m",
        "kind": "marketplace",
        "provider": "openrouter",
        "model": "m",
        "currency": "USD",
        "unit": "per_million_tokens",
        "rates": [{"input": 1.0, "output": 2.0}],
    }],
}
snap2 = {
    "schema": 1,
    "source": "ref-v2",
    "observed": "2026-11-25T00:00:00Z",
    "routes": [{
        "route": "openrouter/m",
        "kind": "marketplace",
        "provider": "openrouter",
        "model": "m",
        "currency": "USD",
        "unit": "per_million_tokens",
        "rates": [{"input": 2.0, "output": 4.0}],
    }],
}
p1 = root / "snap1.json"
p2 = root / "snap2.json"
p1.write_text(json.dumps(snap1), encoding="utf-8")
p2.write_text(json.dumps(snap2), encoding="utf-8")

buf1 = io.StringIO()
with redirect_stdout(buf1):
    assert cli.main(["prices", "import", str(p1)]) == 0
imp1 = json.loads(buf1.getvalue().strip().splitlines()[-1])
pv1 = imp1["content_version"]
assert pv1.startswith("prices/models@")

buf2 = io.StringIO()
with redirect_stdout(buf2):
    assert cli.main(["prices", "import", str(p2)]) == 0
imp2 = json.loads(buf2.getvalue().strip().splitlines()[-1])
pv2 = imp2["content_version"]
assert pv2.startswith("prices/models@") and pv2 != pv1

# Start isolated HTTP server and exercise /api/v1/insights/{chart}, alias, ETag, repricing, Step.paid_usd, and CLI
node = SimpleNamespace(names={api_check.ADDRESS})
logins = tailnet.Logins(tailnet.ScratchHost())
logins.of = lambda _: api_check.LOGIN
handler = type("InsightHandler", (dashboard.handlers().open,), {"disable_nagle_algorithm": True})
with mock.patch.object(api_http, "log_request"), mock.patch("socket.getfqdn", return_value=api_check.ADDRESS):
    server = tailnet.Server(api_check.ADDRESS, 0, node, logins, False, handler, dashboard.Redirect)
    with server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            api = api_check.Check(server.server_port, root, [], token)

            # All 8 charts answer 200 with {chart, series, derived_seq, stale} + ETag + 304
            for chart in insights.CHARTS:
                headers, raw = api.request("GET", f"/api/v1/insights/{chart}")
                data = json.loads(raw)
                assert data["chart"] == chart
                assert isinstance(data["derived_seq"], int)
                # Without derived providers registered, stale MUST be True
                assert data["stale"] is True
                etag = headers["ETag"]
                api.request("GET", f"/api/v1/insights/{chart}", headers={"If-None-Match": etag}, status=304)
                # Unversioned alias
                alias_h, alias_raw = api.request("GET", f"/api/insights/{chart}")
                assert json.loads(alias_raw)["chart"] == chart
                assert alias_h["ETag"] == etag

            # Stored cost matches across GET /api/v1/insights/cost, GET /api/runs/<id>, board.outcomes, and `arete costs --json`
            cost_resp = api.read(f"/api/v1/insights/cost?run_id={r_fwd}")
            run_resp = api.read(f"/api/runs/{r_fwd}")
            expected_usd = expected_nusd / 1_000_000_000
            assert cost_resp["series"]["cost_usd"] == expected_usd
            assert run_resp["run"]["cost_usd"] == expected_usd
            assert board.outcomes()["total_paid"] == (expected_nusd * 2) / 1_000_000_000

            steps_resp = api.read(f"/api/v1/runs/{r_fwd}/steps")
            assert len(steps_resp["steps"]) == len(amounts)
            assert all(s["paid_usd"] is not None for s in steps_resp["steps"])

            # CLI `arete costs --json` returns the exact same dollars
            buf_costs = io.StringIO()
            with redirect_stdout(buf_costs):
                assert cli.main(["costs", "--json"]) == 0
            cost_lines = [json.loads(line) for line in buf_costs.getvalue().strip().splitlines()]
            assert cost_lines[-1]["paid_usd"] == (expected_nusd * 2) / 1_000_000_000

            # Read-only repricing: pv2 (doubled rates) returns EXACTLY 2x pv1, and stored charge is untouched
            rp1 = api.read(f"/api/v1/insights/cost?run_id={r_fwd}&price_version={pv1}")["series"]
            rp2 = api.read(f"/api/v1/insights/cost?run_id={r_fwd}&price_version={pv2}")["series"]
            assert rp1["cost_nusd"] > 0
            assert rp2["cost_nusd"] == rp1["cost_nusd"] * 2
            assert rp2["cost_usd"] == rp1["cost_usd"] * 2
            # Stored charge remains unchanged
            assert api.read(f"/api/v1/insights/cost?run_id={r_fwd}")["series"]["cost_nusd"] == expected_nusd

            # CLI `arete prices recompute RUN_ID STEP --price-version` & `arete jev report --json`
            buf_rc = io.StringIO()
            with redirect_stdout(buf_rc):
                assert cli.main(["prices", "recompute", r_fwd, "s0", "--price-version", pv2]) == 0
            rc_data = json.loads(buf_rc.getvalue().strip())
            assert rc_data["run"] == r_fwd and rc_data["step"] == "s0" and rc_data["cost_usd"] == 0.2

            buf_jev = io.StringIO()
            with redirect_stdout(buf_jev):
                assert cli.main(["jev", "report", "--json"]) == 0
            jev_data = json.loads(buf_jev.getvalue())
            assert jev_data["cost_nusd"] == 29_232  # 2 x 14_616
        finally:
            server.shutdown()
            thread.join(timeout=5)

print("MONEY_RESULT=" + json.dumps({"ok": True}))
"""
        self.assertTrue(_run_isolated(script)["ok"])


if __name__ == "__main__":
    unittest.main()
