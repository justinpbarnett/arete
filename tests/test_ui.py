import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import arete

from arete import paths, state, util

from tests import support
from tests.harness import bootstrap, make_folders, scratch_env

SCRIPTS = Path(__file__).resolve().parent / "scripts"


def setUpModule():
    support.isolate_live_state()


class NewUIShellTests(unittest.TestCase):
    def test_action_write_boundary(self):
        web = support.SOURCE / "arete" / "web"
        names = ("ui-actions.js", "ui-undo.js", "ui-notice.js", "ui-write.js")
        for name in names:
            source = (web / name).read_text()
            self.assertNotIn("innerHTML", source)
            self.assertNotIn("eval", source)
            if name != "ui-write.js":
                for method in re.findall(r"\bmethod\s*:\s*([^,}\n]+)", source):
                    self.assertIn(method.strip(), ('"GET"', "'GET'"))
        write = (web / "ui-write.js").read_text()
        for field in ("X-Arete", "Idempotency-Key", "base_version"):
            self.assertIn(field, write)
        for path in web.glob("ui-*.js"):
            if path.name != "ui-write.js":
                for method in re.findall(r"\bmethod\s*:\s*([^,}\n]+)", path.read_text()):
                    self.assertIn(method.strip(), ('"GET"', "'GET'"), path.name)

    def test_action_fixture_http_controls(self):
        script = (SCRIPTS / "ui_action_fixture_http.py").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "log", "worktrees")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL"),
                              ARETE_LOG_DIR=str(root / "log"), ARETE_WORKTREES_DIR=str(root / "worktrees"))
            command = [sys.executable, "-I", "-B", "-c", bootstrap(script)]
            done = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_actions_offline_behavior(self):
        script = (SCRIPTS / "ui_actions_offline.mjs").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text('{"type":"module"}')
            web = support.SOURCE / "arete" / "web"
            for path in [*web.glob("ui-*.js"), *web.glob("vendor-*.js")]:
                shutil.copyfile(path, root / path.name)
            (root / "check.mjs").write_text(script)
            done = subprocess.run(["node", str(root / "check.mjs")], cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_serve_status_names_a_failing_tick_in_every_serve_line(self):
        script = (SCRIPTS / "serve_status_ui.mjs").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text('{"type":"module"}')
            web = support.SOURCE / "arete" / "web"
            for path in [*web.glob("ui-*.js"), *web.glob("vendor-*.js"), web / "loops.js", web / "refresh.js", web / "health.js"]:
                shutil.copyfile(path, root / path.name)
            (root / "check.mjs").write_text(script)
            done = subprocess.run(["node", str(root / "check.mjs")], cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_palette_source_contract(self):
        web = support.SOURCE / "arete" / "web"
        palette = (web / "ui-palette.js").read_text()
        for name in ("steps", "triggers", "prompts", "roles", "drop", "wires", "create"):
            self.assertIn(name, palette)
        self.assertIn("ui-fuzzy.js", palette)
        for source in (palette, (web / "ui-fuzzy.js").read_text()):
            for banned in ("innerHTML", "insertAdjacentHTML", "eval"):
                self.assertNotIn(banned, source)

    def test_feedback_dialog_source_contract(self):
        from arete import dashboard
        web = support.SOURCE / "arete" / "web"
        source = (web / "ui-feedback.js").read_text()
        _, served = dashboard.web_snapshot()
        self.assertEqual(served["/ui-feedback.js"][0], source.encode())
        self.assertTrue(served["/ui-feedback.js"][1].startswith("text/javascript"))
        self.assertEqual(served["/ui-feedback.css"][0], (web / "ui-feedback.css").read_bytes())
        for name in ("/api/capture", "/api/capture/submit", "/api/capture/evidence", "Send feedback"):
            self.assertIn(name, source)
        self.assertIsNone(re.search(r"style\s*=", source))
        self.assertNotIn("innerHTML", source)

    def test_context_carries_screen_and_keeps_the_legacy_shape(self):
        from arete import capture
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(paths, "STATE_DIR", Path(folder)), \
                mock.patch.object(paths, "DB_FILE", Path(folder) / "arete.db"), \
                mock.patch.object(util, "iso", return_value="2026-01-02T03:04:05Z"):
            state.ensure_schema()

            def stored(body):
                capture.add_context(body, "code123")
                return state.capture_draft()["context"]

            legacy = {"captured": "2026-01-02T03:04:05Z", "app": {"version": arete.__version__, "code": "code123"}, "view": "live"}
            self.assertEqual(stored({"view": "live"}), json.dumps(legacy, indent=2, ensure_ascii=False))
            with_screen = json.loads(stored({"screen": "#settings/health"}))
            self.assertEqual(with_screen["screen"], "#settings/health")
            self.assertEqual(with_screen["app"]["code"], "code123")
            self.assertNotIn("view", with_screen)
            longest = "#" + "a " * 99 + "a"
            self.assertEqual(len(longest), 200)
            self.assertEqual(json.loads(stored({"screen": longest}))["screen"], longest)
            before = state.capture_draft()["context"]
            for body in ({"screen": "settings"}, {"screen": None}, {"screen": 4}, {"screen": longest + "a"},
                         {"screen": "#floor", "view": "floor"}, {"screen": "#floor", "view": None}):
                with self.assertRaises(capture.Refused, msg=str(body)):
                    capture.add_context(body, "code123")
            self.assertEqual(state.capture_draft()["context"], before)

    def test_palette_offline_behavior(self):
        script = (SCRIPTS / "ui_palette_offline.mjs").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text('{"type":"module"}')
            for name in ("ui-palette.js", "ui-fuzzy.js", "ui-hooks.js", "ui-keys.js", "ui-router.js", "ui-menu.js", "ui-list.js", "ui-states.js", "ui-actions.js", "ui-data.js", "ui-undo.js", "ui-notice.js", "ui-write.js", "vendor-preact.js", "vendor-signals.js"):
                shutil.copyfile(support.SOURCE / "arete" / "web" / name, root / name)
            (root / "check.mjs").write_text(script)
            done = subprocess.run(["node", str(root / "check.mjs")], cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_shared_list_source_contract(self):
        web = support.SOURCE / "arete" / "web"
        source = (web / "ui-list.js").read_text()
        header = source.split("export function ListHeader", 1)[1].split("export function ListRow", 1)[0]
        for name in ("group", "filter", "search", "sort", "count", "new"):
            self.assertIn(name, header.lower())
        self.assertRegex((web / "ui-list.css").read_text(), r"--list-row:\s*28px")
        self.assertIn("`${n}/${total}`", source)
        for banned in ("innerHTML", "insertAdjacentHTML", "eval"):
            self.assertNotIn(banned, source)

    def test_shared_lists_offline_behavior(self):
        script = (SCRIPTS / "ui_shared_lists_offline.mjs").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text('{"type":"module"}')
            for name in ("ui-list.js", "ui-states.js", "ui-menu.js", "ui-keys.js", "ui-router.js", "ui-palette.js", "ui-fuzzy.js", "ui-actions.js", "ui-data.js", "ui-undo.js", "ui-notice.js", "ui-write.js", "vendor-preact.js", "vendor-signals.js"):
                shutil.copyfile(support.SOURCE / "arete" / "web" / name, root / name)
            (root / "check.mjs").write_text(script)
            done = subprocess.run(["node", str(root / "check.mjs")], cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def run_module_script(self, script, names):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text('{"type":"module"}')
            for name in (*names, "vendor-preact.js", "vendor-signals.js"):
                shutil.copyfile(support.SOURCE / "arete" / "web" / name, root / name)
            (root / "check.mjs").write_text(script)
            done = subprocess.run(["node", str(root / "check.mjs")], cwd=root, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_live_source_contract(self):
        source = (support.SOURCE / "arete" / "web" / "ui-live.js").read_text()
        self.assertEqual(source.count("/api/sidebar"), 1)
        self.assertEqual(source.count("/api/live"), 1)
        self.assertNotIn("/api/asks", source)
        self.assertIn("OFFLINE", source)
        self.assertIn("STALE", source)
        self.assertTrue("document.hidden" in source or "visibilityState" in source)
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("eval", source)

    def test_live_function_polls_and_recovers_offline(self):
        script = (SCRIPTS / "ui_live_poll_recovery.mjs").read_text()
        self.run_module_script(script, ("ui-live.js", "ui-data.js", "ui-write.js"))

    def test_state_drawings_use_glyphs_words_and_escaped_text(self):
        script = (SCRIPTS / "ui_state_drawings.mjs").read_text()
        self.run_module_script(script, ("ui-states.js",))

    def test_read_retry_key_leaves_item_retry_and_insert_alone(self):
        script = (SCRIPTS / "ui_read_retry_key.mjs").read_text()
        self.run_module_script(script, ("ui-keys.js", "ui-router.js", "ui-menu.js", "ui-palette.js", "ui-fuzzy.js", "ui-actions.js", "ui-data.js",
                                         "ui-undo.js", "ui-notice.js", "ui-write.js", "ui-list.js", "ui-states.js"))

    def test_live_poll_keeps_cursor_scroll_and_filter(self):
        script = (SCRIPTS / "ui_live_cursor_scroll.mjs").read_text()
        self.run_module_script(script, ("ui-live.js", "ui-data.js", "ui-list.js", "ui-states.js", "ui-keys.js", "ui-router.js", "ui-menu.js", "ui-palette.js",
                                         "ui-fuzzy.js", "ui-actions.js", "ui-undo.js", "ui-notice.js", "ui-write.js"))

    def test_live_fixture_serves_and_records_data_reads(self):
        script = (SCRIPTS / "ui_live_fixture_reads.py").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            command = [sys.executable, "-I", "-B", "-c", f"import sys; sys.path.insert(0, {str(support.SOURCE)!r});" + script]
            done = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_run_screen_sources_name_groups_routes_and_avoid_unsafe_html(self):
        web = support.SOURCE / "arete" / "web"
        page, data = (web / "ui-run.js").read_text(), (web / "ui-data.js").read_text()
        for group in ("Running", "Waiting on you", "Fault", "Done"):
            self.assertIn(group, page)
        for verb in ("stop", "resume", "repair"):
            self.assertIn(f"/api/runs/${{name}}/{verb}", data)
        self.assertIn("follow=1", data)
        for source in (page, data):
            for banned in ("innerHTML", "insertAdjacentHTML", "eval"):
                self.assertNotIn(banned, source)

    def test_run_page_groups_steps_and_turn_folding(self):
        script = r'''
import assert from 'node:assert/strict';
import { reduceSteps, groupOf, wordOf, applyQueue, RunSubtitle, RunFocus, GROUPS } from './ui-run.js';
assert.deepEqual(GROUPS, ['Running', 'Waiting on you', 'Fault', 'Done']);
assert.equal(groupOf('needs-info'), 'Waiting on you');
assert.equal(groupOf('interrupted'), 'Fault');
assert.equal(groupOf('pr'), 'Done');
assert.equal(wordOf('stopped'), '■ STOPPED');
const events = [
  {seq: 1, event: 'step_start', step: 'build', try: 1, model: 'm'},
  {seq: 2, event: 'step_end', step: 'build', try: 1, result: 'fail', ok: false, seconds: 3, cost_usd: .1, tokens_in: 5, tokens_out: 2, turns: 2, error: 'x'},
  {seq: 3, event: 'error', step: 'build', message: '<b>boom</b>'},
  {seq: 4, event: 'step_start', step: 'build', try: 2},
  {seq: 5, event: 'heartbeat', step: 'build', seconds: 9},
  {seq: 6, event: 'step_end', step: 'build', try: 2, result: 'pass', ok: true, seconds: 9},
  {seq: 7, event: 'step_end', step: 'ghost', try: 1, result: 'pass', carried: true},
  {seq: 8, event: 'report', status: 'pr'},
];
const rows = reduceSteps(events);
assert.deepEqual(rows.map((row) => row.kind), ['step', 'error', 'step', 'report']);
assert.deepEqual(rows.filter((row) => row.kind === 'step').map((row) => [row.attempt, row.state, row.seconds, row.label]), [[1, 'fail', 3, 'm'], [2, 'pass', 9, '']]);
assert.equal(rows[1].text, '<b>boom</b>');
assert.deepEqual(reduceSteps([]), []);
const replaced = [];
globalThis.location = {hash: '#run', replace: (hash) => replaced.push(hash)};
const run = (id, status) => ({id, status, ticket: `T-${id}`, summary: `s ${id}`, started: '2026-01-01T00:00:00Z', seconds: 5, repo: 'arete', pr_url: ''});
applyQueue({queue: [run('a', 'failed'), run('b', 'pr'), run('c', 'running'), run('d', 'needs-info'), run('e', 'stopped'), run('f', 'interrupted'), run('g', 'running')], detail: null, id: ''});
assert.deepEqual(replaced, ['#run/c']);
assert.equal(new RunSubtitle().render(), '2 running · 1 waiting on you · 3 fault');
applyQueue({queue: [], detail: null, id: ''});
assert.equal(new RunSubtitle().render(), '0 running · 0 waiting on you · 0 fault');
const focus = new RunFocus();
focus.props = {view: {detail: {run: {id: 'r1'}}}};
focus.setState = (change) => Object.assign(focus.state, change);
assert.equal(focus.fold(), false);
focus.toggle('r1|f:1');
assert.equal(focus.state.open.has('r1|f:1'), true);
assert.equal(focus.fold(), true);
assert.equal(focus.state.open.size, 0);
'''
        self.run_module_script(script, ("ui-run.js", "ui-list.js", "ui-states.js", "ui-keys.js", "ui-router.js", "ui-menu.js", "ui-palette.js",
                                         "ui-fuzzy.js", "ui-actions.js", "ui-data.js", "ui-undo.js", "ui-notice.js", "ui-write.js", "vendor-htm.js"))

    def test_run_actions_post_the_right_routes_once_and_say_refusals(self):
        script = r'''
import assert from 'node:assert/strict';
import { dispatch, actions, runPageContext, repairThisRun } from './ui-actions.js';
import { notice } from './ui-notice.js';
const posts = [];
let refuse = false;
globalThis.location = {hash: '#run/r1'};
globalThis.fetch = async (url, options) => {
  posts.push({url, options});
  await new Promise((resolve) => setTimeout(resolve, 10));
  return refuse ? {ok: false, status: 400, json: async () => ({error: 'run r1 is a miner run, not the run of a ticket'})}
    : {ok: true, status: 200, json: async () => ({run: 'r2', stopped: true})};
};
let refreshed = 0;
let went = '';
const context = (status) => runPageContext({id: 'r1', status}, {refresh: () => refreshed++, go: (id) => { went = id; }, openLink() {}});
const failed = context('failed');
assert.equal(failed.run.retry.name, 'retry');
for (const status of ['running', 'needs-info', 'pr', 'done']) assert.equal(context(status).run.retry, null);
assert.equal(await dispatch(failed.run.retry, failed), true);
assert.equal(posts[0].url, '/api/runs/r1/resume');
assert.equal(posts[0].options.body, '{}');
assert.equal(posts[0].options.headers['X-Arete'], '1');
assert.equal(went, 'r2');
assert.equal(refreshed, 1);
await dispatch(repairThisRun, failed);
assert.equal(posts[1].url, '/api/runs/r1/repair');
const running = context('running');
const first = dispatch(actions.stop, running);
const second = dispatch(actions.stop, running);
assert.equal(await second, false);
assert.equal(await first, true);
assert.equal(posts.filter((post) => post.url === '/api/runs/r1/stop').length, 1);
assert.match(notice.value, /Asked r1 to stop/);
assert.match(notice.value, /cannot be undone/);
refuse = true;
await dispatch(failed.run.retry, failed);
assert.equal(notice.value, 'Failed: run r1 is a miner run, not the run of a ticket');
'''
        self.run_module_script(script, ("ui-actions.js", "ui-data.js", "ui-undo.js", "ui-notice.js", "ui-write.js"))

    def test_run_live_reads_follow_turns_and_ignore_stale_selection(self):
        script = r'''
import assert from 'node:assert/strict';
const handlers = {};
globalThis.document = {hidden: false, addEventListener(name, fn) { handlers[name] = fn; }, removeEventListener() {}};
globalThis.crypto ??= (await import('node:crypto')).webcrypto;
const calls = [];
const encoder = new TextEncoder();
let feedRows = [{id: 1, ts: '2026-01-01T00:00:01Z', kind: 'note', text: 'a\nb'}];
let status = 'running';
let logTail = ['x', 'y'];
let slow = null;
let queueFault = null;
const reply = (body, code = 200) => ({ok: code < 400, status: code, json: async () => body, text: async () => typeof body === 'string' ? body : JSON.stringify(body)});
const row = (id, state) => ({id, status: state, kind: 'loop', ticket: '', started: '2026-01-01T00:00:00Z', finished: null, seconds: 1, paid: 0, pr_url: '', summary: '', loop: '', repo: 'arete'});
globalThis.fetch = async (url, options) => {
  calls.push(url);
  if (url === '/api/sidebar') return reply({needs_you: 0, running: 1, health: 'ok', serve: {alive: true, age_seconds: 1}});
  if (url === '/api/live') return reply({live: {rows: {runs: [], slots: 0, max_parallel: 1}}, needs_you: {}});
  if (url.startsWith('/api/runs?')) return queueFault ? reply({error: 'queue down'}, 503) : reply({runs: [row('r1', status), row('r2', 'done')], more: true});
  if (url === '/api/runs/r1') return slow ? slow.then(() => reply({run: row('r1', status), models: [], lines_from: 'feed'})) : reply({run: row('r1', status), models: [], lines_from: 'feed'});
  if (url === '/api/runs/r2') return reply({run: row('r2', 'done'), models: [], lines_from: 'log'});
  if (url === '/api/runs/zz') return reply({error: 'nothing has the id zz'}, 404);
  if (url === '/api/runs/bad') return reply({});
  if (url.startsWith('/api/feed/r1?after=')) return reply({run: row('r1', status), lines: feedRows.filter((line) => line.id > Number(url.split('=')[1]))});
  if (url === '/api/log/r2') return reply({id: 'r2', lines: logTail});
  if (url.startsWith('/api/runs/r2/events?')) return reply('{"seq":1,"event":"step_start","step":"build","try":1}\n{"seq":2,"event":"step_end","step":"build","try":1,"result":"pass"}\n');
  if (url.includes('/api/runs/r1/events?') && url.includes('follow=1')) {
    const from = Number(/from=(\d+)/.exec(url)[1]);
    const parts = from === 0 ? ['{"seq":1,"event":"step_start","ste', 'p":"build","try":1}\n{"seq":2,"event":"heartbeat","step":"build","seconds":2}\n{"seq":1,"event":"step_start","step":"build","try":1}\n']
      : ['{"seq":3,"event":"step_end","step":"build","try":1,"result":"pass"}\n'];
    return {ok: true, status: 200, body: new ReadableStream({start(controller) { for (const part of parts) controller.enqueue(encoder.encode(part)); controller.close(); }})};
  }
  if (url.startsWith('/api/runs/r1/events?')) return reply('');
  throw new Error(`unexpected ${url}`);
};
const { live, runView } = await import('./ui-live.js');
const settle = () => new Promise((resolve) => setTimeout(resolve, 60));
let feed = live('run', 'r1');
await settle();
assert.equal(runView.value.queue.length, 2);
assert.equal(runView.value.more, true);
assert.equal(runView.value.turns.length, 1);
assert.equal(runView.value.turns[0].text, 'a\nb');
assert.deepEqual(runView.value.events.map((event) => event.seq), [1, 2]);
feedRows = [...feedRows, {id: 2, ts: '2026-01-01T00:00:02Z', kind: 'note', text: 'c'}];
await feed.retry();
await settle();
await feed.retry();
await settle();
assert.deepEqual(runView.value.turns.map((turn) => turn.text), ['a\nb', 'c']);
assert.ok(calls.includes('/api/feed/r1?after=1'));
assert.ok(calls.includes('/api/feed/r1?after=2'));
assert.deepEqual(runView.value.events.map((event) => event.seq), [1, 2, 3]);
assert.ok(calls.some((url) => url.includes('follow=1') && url.includes('from=2')));
status = 'done';
await feed.retry();
await settle();
assert.equal(runView.value.detail.run.status, 'done');
const finite = calls.filter((url) => url.includes('/api/runs/r1/events?') && !url.includes('follow=1'));
assert.ok(finite.length >= 1);
feed.stop();
feed = live('run', 'r2');
await settle();
assert.ok(calls.includes('/api/runs/r2/events?from=0'));
assert.equal(runView.value.events.length, 2);
assert.deepEqual(runView.value.turns.map((turn) => turn.text), ['x', 'y']);
logTail = ['y', 'z'];
await feed.retry();
assert.deepEqual(runView.value.turns.map((turn) => turn.text), ['y', 'z']);
feed.stop();
let release;
slow = new Promise((resolve) => { release = resolve; });
status = 'running';
feed = live('run', 'r1');
const other = live('run', 'r2');
release();
await settle();
assert.equal(runView.value.id, 'r2');
assert.equal(runView.value.detail.run.id, 'r2');
other.stop();
slow = null;
feed = live('run', 'zz');
await settle();
assert.equal(runView.value.detailError.status, 404);
assert.equal(runView.value.detailError.message, 'nothing has the id zz');
assert.equal(runView.value.queue.length, 2);
feed.stop();
feed = live('run', 'bad');
await settle();
assert.equal(runView.value.detailError.message, 'Malformed response');
queueFault = true;
await feed.retry();
assert.equal(runView.value.queueError.message, 'queue down');
assert.equal(runView.value.queue.length, 2);
feed.stop();
feed = live('run', 'r1');
await settle();
const seen = calls.length;
document.hidden = true;
handlers.visibilitychange();
await settle();
assert.equal(calls.length, seen);
feed.stop();
'''
        self.run_module_script(script, ("ui-live.js", "ui-data.js", "ui-write.js"))

    def test_run_fixture_serves_runs_stops_and_refuses(self):
        script = r'''
import json, threading, time, unittest, urllib.request, urllib.error
from tests.ui_action_fixture import server, close_producers
check = unittest.TestCase()
instance = server(runs=True)
threading.Thread(target=instance.serve_forever, daemon=True).start()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = f'http://127.0.0.1:{instance.server_port}'
def request(path, body=None, guard=True):
    headers = {'Content-Type': 'application/json', 'Idempotency-Key': 'k'}
    if guard:
        headers['X-Arete'] = '1'
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        response = opener.open(req, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read().decode())
try:
    ids = instance.ids
    check.assertEqual(sorted(ids), ['failed', 'feed', 'finished', 'gone', 'interrupted', 'plain', 'running', 'stopped', 'waiting'])
    rows = {row['id']: row['status'] for row in request('/api/runs?limit=500')[1]['runs']}
    check.assertEqual(rows[ids['finished']], 'pr')
    check.assertEqual(rows[ids['waiting']], 'needs-info')
    check.assertEqual(rows[ids['running']], 'running')
    check.assertIn('paid', request('/api/runs/' + ids['finished'])[1]['run'])
    events = [json.loads(line) for line in urllib.request.urlopen(base + '/api/runs/' + ids['finished'] + '/events?from=0').read().decode().splitlines()]
    check.assertEqual(len([e for e in events if e['event'] == 'step_start']), 3)
    before = len(request('/api/feed/' + ids['feed'] + '?after=0')[1]['lines'])
    check.assertEqual(request('/__ui_runs/control', {'action': 'append-feed', 'text': 'marker'}, guard=False)[0], 403)
    check.assertEqual(request('/__ui_runs/control', {'action': 'append-feed', 'text': 'marker'})[0], 200)
    check.assertEqual(len(request('/api/feed/' + ids['feed'] + '?after=0')[1]['lines']), before + 1)
    status, body = request('/api/runs/' + ids['plain'] + '/resume', {})
    check.assertEqual(status, 400)
    check.assertIn('not the run of a ticket', body['error'])
    status, body = request('/api/runs/' + ids['failed'] + '/resume', {})
    check.assertEqual(status, 200)
    check.assertEqual(request('/api/runs/' + body['run'])[1]['run']['status'], 'running')
    status, body = request('/api/runs/' + ids['running'] + '/stop', {})
    check.assertEqual(status, 200)
    for _ in range(50):
        if request('/api/runs/' + ids['running'])[1]['run']['status'] == 'stopped':
            break
        time.sleep(0.1)
    check.assertEqual(request('/api/runs/' + ids['running'])[1]['run']['status'], 'stopped')
    check.assertEqual(request('/api/runs/' + ids['running'] + '/stop', {})[0], 400)
    request('/__ui_runs/control', {'action': 'fault', 'target': 'queue', 'mode': 'error', 'status': 503})
    check.assertEqual(request('/api/runs?limit=500')[0], 503)
    log = request('/__ui_runs/requests')[1]
    stops = [entry for entry in log if entry['url'].endswith('/stop') and entry['method'] == 'POST']
    check.assertEqual(stops[0]['guard'], '1')
    check.assertTrue(stops[0]['idempotency_key'])
finally:
    instance.shutdown()
    instance.server_close()
    close_producers(instance)
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("config", "state", "logs", "worktrees", "tmp"):
                (root / name).mkdir()
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR")}
            env.update(ARETE_CONFIG_DIR=str(root / "config"), ARETE_STATE_DIR=str(root / "state"),
                       ARETE_LOG_DIR=str(root / "logs"), ARETE_WORKTREES_DIR=str(root / "worktrees"),
                       TMPDIR=str(root / "tmp"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            command = [sys.executable, "-I", "-B", "-c", f"import sys; sys.path.insert(0, {str(support.SOURCE)!r});" + script]
            done = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_shell_and_declared_assets_are_served_with_page_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_folders(root, "config", "state", "logs", "worktrees", "tmp")
            env = scratch_env(root, keep=("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"),
                              ARETE_WORKTREES_DIR=str(root / "worktrees"),
                              TMPDIR=str(root / "tmp"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            script = (SCRIPTS / "ui_shell_assets.py").read_text()
            command = [sys.executable, "-I", "-B", "-c", bootstrap(script)]
            done = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)

    def test_keyboard_files_hold_every_screen_key_and_every_replay_hook(self):
        web = support.SOURCE / "arete" / "web"
        keys, hooks = (web / "ui-keys.js").read_text(), (web / "ui-hooks.js").read_text()
        routes = {"f": "#floor", "t": "#triage", "w": "#board/open-work", "b": "#board", "r": "#run", "n": "#intake",
                  "o": "#outbox", "p": "#prompts", "a": "#adws", "v": "#insights", ",": "#settings/health"}
        for letter, route in routes.items():
            self.assertRegex(keys, rf'\["{re.escape(letter)}", "[a-z-]+", "[^"]+", "{re.escape(route)}"\]')
        for name in ("keys", "clicks", "drag", "wheel"):
            self.assertIn(f'"{name}"', hooks)
        for source in (keys, hooks):
            for banned in ("innerHTML", "insertAdjacentHTML", "eval"):
                self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
