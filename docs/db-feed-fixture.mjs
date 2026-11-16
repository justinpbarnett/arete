import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";

const root = process.argv[2];
const line = (id, text = `line-${id}`) => ({ id, text, ts: "2026-01-01T00:00:00Z", kind: "note" });
const answer = (epoch, lines) => ({ restore_epoch: epoch, lines, run: { status: "running" } });
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function requests() {
  const pending = [];
  const call = (url) => {
    if (url.startsWith("/api/runs/")) return Promise.resolve({ lines_from: "feed" });
    const item = { url, ...deferred() };
    pending.push(item);
    return item.promise;
  };
  return { pending, call };
}

async function legacy() {
  const queue = requests();
  const context = vm.createContext({ call: queue.call, clock: () => "00:00" });
  const source = fs.readFileSync(path.join(root, "arete/web/run-page.js"), "utf8");
  vm.runInContext(source + "\nglobalThis.api = { loadRunDetail, mergeFeed, state: () => feedOf };", context);
  const api = context.api;
  const initial = api.loadRunDetail("a");
  await flush();
  queue.pending.shift().resolve(answer(0, [line(15000, "old")]));
  api.mergeFeed("a", (await initial).feed);
  assert.equal(api.state().after, 15000);
  const old = api.loadRunDetail("a");
  await flush();
  const oldRequest = queue.pending.shift();
  const newer = api.loadRunDetail("a");
  await flush();
  queue.pending.shift().resolve(answer(1, []));
  await flush();
  assert.equal(api.state().after, 0);
  assert.equal(api.state().lines.length, 0);
  const reset = queue.pending.shift();
  assert.match(reset.url, /after=0$/);
  reset.resolve(answer(1, [line(2, "restored")]));
  api.mergeFeed("a", (await newer).feed);
  oldRequest.resolve(answer(0, [line(16000, "superseded")]));
  api.mergeFeed("a", (await old).feed);
  assert.equal(api.state().after, 2);
  assert.equal(api.state().lines.length, 1);
  assert.match(api.state().lines[0], /restored/);
  const first = api.loadRunDetail("a");
  const second = api.loadRunDetail("a");
  await flush();
  const [firstRequest, secondRequest] = queue.pending.splice(0);
  secondRequest.resolve(answer(1, [line(2), line(3)]));
  api.mergeFeed("a", (await second).feed);
  firstRequest.resolve(answer(1, [line(2), line(3)]));
  api.mergeFeed("a", (await first).feed);
  assert.equal(api.state().after, 3);
  assert.equal(api.state().lines.length, 2);
  const leaving = api.loadRunDetail("a");
  await flush();
  const leavingRequest = queue.pending.shift();
  const arriving = api.loadRunDetail("b");
  await flush();
  queue.pending.shift().resolve(answer(1, [line(4, "run-b")]));
  api.mergeFeed("b", (await arriving).feed);
  leavingRequest.resolve(answer(1, [line(99, "run-a")]));
  api.mergeFeed("a", (await leaving).feed);
  assert.equal(api.state().id, "b");
  assert.equal(api.state().after, 4);
  const bulk = api.loadRunDetail("b");
  await flush();
  queue.pending.shift().resolve(answer(1, Array.from({ length: 2100 }, (_, i) => line(i + 5))));
  api.mergeFeed("b", (await bulk).feed);
  assert.equal(api.state().lines.length, 2000);
  return { stale_generation_discarded: true, reset_cursor: 0, navigation_guard: true, duplicate_guard: true, cache_lines: 2000 };
}

async function live() {
  const queue = requests();
  const data = { run: (id) => ({ feed: (after) => `/api/feed/${id}?after=${after}`, log: `/api/log/${id}` }) };
  const context = vm.createContext({
    signal: (value) => ({ value }), data, AbortController,
    document: { hidden: false, addEventListener() {}, removeEventListener() {} },
    setTimeout: () => 1, clearTimeout() {},
    fetch: async (url) => ({ ok: true, json: async () => queue.call(url) }),
  });
  const source = fs.readFileSync(path.join(root, "arete/web/ui-live.js"), "utf8")
    .replace(/^import .*;\n/gm, "").replace(/\bexport /g, "");
  vm.runInContext(source + `
    globalThis.api = {
      select(id) { selectRun(id); active = { id }; rs.detail = { lines_from: "feed", run: { status: "running" } }; return active; },
      read(run) { return readTurns(rs.selection, generation, run); },
      state: () => rs,
      log() { rs.detail.lines_from = "log"; }, takeEvents,
    };`, context);
  const api = context.api;
  const run = api.select("a");
  const initial = api.read(run);
  await flush();
  queue.pending.shift().resolve(answer(0, [line(15000)]));
  await initial;
  const old = api.read(run);
  await flush();
  const oldRequest = queue.pending.shift();
  const newer = api.read(run);
  await flush();
  queue.pending.shift().resolve(answer(1, []));
  await flush();
  assert.equal(api.state().turnsAfter, 0);
  assert.equal(api.state().turns.length, 0);
  const reset = queue.pending.shift();
  assert.match(reset.url, /after=0$/);
  reset.resolve(answer(1, [line(1, "restored")]));
  await newer;
  oldRequest.resolve(answer(0, [line(16000)]));
  await old;
  assert.equal(api.state().turnsAfter, 1);
  assert.equal(api.state().turns[0].text, "restored");
  const first = api.read(run);
  const second = api.read(run);
  await flush();
  const [one, two] = queue.pending.splice(0);
  two.resolve(answer(1, [line(1), line(2)]));
  await second;
  one.resolve(answer(1, [line(1), line(2)]));
  await first;
  assert.equal(api.state().turns.length, 2);
  const leaving = api.read(run);
  await flush();
  const leavingRequest = queue.pending.shift();
  const other = api.select("b");
  const arriving = api.read(other);
  await flush();
  queue.pending.shift().resolve(answer(1, [line(4, "run-b")]));
  await arriving;
  leavingRequest.resolve(answer(1, [line(99, "run-a")]));
  await leaving;
  assert.equal(api.state().turnsAfter, 4);
  assert.equal(api.state().turns[0].text, "run-b");
  api.log();
  const log = api.read(other);
  await flush();
  queue.pending.shift().resolve({ lines: ["plain log"] });
  await log;
  assert.equal(api.state().turns[0].kind, "log");
  api.takeEvents('{"seq":1,"type":"fixture"}\n');
  assert.equal(api.state().seq, 1);
  return { stale_generation_discarded: true, reset_cursor: 0, navigation_guard: true, duplicate_guard: true, log_and_events: true };
}

console.log(JSON.stringify({ legacy: await legacy(), live: await live() }));
