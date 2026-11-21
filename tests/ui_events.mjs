import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";
import { openEvents } from "../arete/web/ui-events.js";
import { prepareWrite, sendWrite } from "../arete/web/ui-write.js";
import { clientNotice, staleWrite } from "../arete/web/ui-notice.js";
import { effect } from "../arete/web/vendor-signals.js";

const locks = {
  owner: Promise.resolve(),
  request(_name, { signal }, use) {
    this.owner = this.owner.then(() => { if (!signal.aborted) return use(); });
    return this.owner;
  },
};
function leaseDatabase() {
  let lease;
  let committed = Promise.resolve();
  const db = { close() {}, transaction() {
    const request = {};
    const transaction = { objectStore: () => ({
      get: () => request, put: (row) => { lease = row; }, delete: () => { lease = undefined; },
    }) };
    committed = committed.then(() => {
      request.result = lease;
      request.onsuccess();
      transaction.oncomplete();
    });
    return transaction;
  } };
  return { open() {
    const request = { result: db };
    queueMicrotask(() => request.onsuccess());
    return request;
  } };
}

const streams = [];
class Source {
  static CLOSED = 2;
  constructor(url) {
    this.url = new URL(url, "http://dashboard.test");
    this.readyState = 0;
    this.handlers = new Map();
    streams.push(this);
  }
  addEventListener(kind, handler) { this.handlers.set(kind, handler); }
  open() { this.readyState = 1; this.onopen(); }
  close() { this.readyState = Source.CLOSED; }
  send(event, frame = "message", id = event.id) {
    const handle = frame === "message" ? this.onmessage : this.handlers.get(frame);
    handle({ data: JSON.stringify(event), lastEventId: id });
  }
}

Object.defineProperty(globalThis, "navigator", { value: { locks } });
globalThis.indexedDB = leaseDatabase();
globalThis.EventSource = Source;
globalThis.document = new EventTarget();
const page = new EventTarget();
globalThis.addEventListener = page.addEventListener.bind(page);
const ui = await import("../arete/web/ui-live.js?browser");

const event = (id, kind = "run.changed", run = { id: "first", status: "running" }) => ({
  id, kind, subject: `run:${run.id}`, data: { ...run, run: run.id }, ver: run.version,
});
const json = (body, status = 200, headers = {}) => Response.json(body, { status, headers });
async function until(predicate, reason) {
  for (let attempt = 0; attempt < 200; attempt++) {
    if (predicate()) return;
    await delay(1);
  }
  assert.fail(reason);
}

async function transport(webLocks) {
  navigator.locks = webLocks ? locks : undefined;
  streams.length = 0;
  const polls = [];
  globalThis.fetch = (url, options) => new Promise((respond) => {
    polls.push({ url: new URL(url, "http://dashboard.test"), options, respond });
  });
  const first = [];
  const second = [];
  const lead = openEvents((message) => first.push(message));
  const follow = openEvents((message) => second.push(message));
  lead.select("first");
  follow.select("first");
  lead.baseline("2.10");
  follow.baseline("2.10");
  const last = () => streams.at(-1);
  const ids = (messages) => messages.filter((message) => message.kind === "run.changed").map((message) => message.id);
  const now = Date.now;
  let clock = now();
  Date.now = () => clock;
  try {
    await until(() => streams.length === 1, "one leader stream");
    assert.equal(last().url.searchParams.get("since"), "2.10");
    last().open();
    last().send(event("2.11"));
    last().send({ kind: "serve.beat", head: "2.99" }, "serve.beat", "2.11");
    last().send(event("2.12", "future.kind"));
    last().send(event("2.13"));
    await until(() => ids(second).length === 2, "follower receives supported events once");
    assert.equal(streams.length, 1);
    assert.deepEqual(ids(first), ["2.11", "2.13"]);
    lead.close();
    await until(() => streams.length === 2, "follower takes leadership on close");
    assert.equal(streams[0].readyState, Source.CLOSED);
    assert.equal(last().url.searchParams.get("since"), "2.13");
    if (webLocks) return;
    last().open();
    last().onerror();
    last().onerror();
    assert.equal(polls.length, 0);
    last().onerror();
    assert.equal(polls.length, 1);
    assert.equal(polls[0].url.pathname + polls[0].url.search, "/api/v1/events?since=2.13&turns=first&timeout=25");
    assert.equal(polls[0].options.headers.get("Last-Event-ID"), "2.13");
    assert.equal(polls[0].options.headers.get("Accept"), "application/json");
    assert.equal(polls[0].options.headers.get("X-Arete-Client"), "web/1.1");
    clock += 60001;
    polls[0].respond(json({ events: [event("2.13"), event("2.14")], next: "2.15", reset: null }));
    await until(() => streams.length === 3, "recovery attempts SSE again");
    assert.equal(last().url.searchParams.get("since"), "2.15");
    last().send(event("2.14"));
    last().send(event("2.16"));
    assert.deepEqual(ids(second), ["2.11", "2.13", "2.14", "2.16"]);
    last().send({ kind: "reset", reason: "epoch" }, "reset");
    assert.equal(second.at(-1).kind, "reset");
  } finally {
    Date.now = now;
    lead.close();
    follow.close();
  }
}

async function snapshots() {
  const calls = [];
  let seq = "5.40";
  const runs = Array.from({ length: 30 }, (_, i) => ({
    id: ["first", "second", "third"][i] ?? `past-${i}`, kind: "pipeline",
    status: i < 3 ? "running" : "pr", version: 1,
  }));
  const log = [];
  const reportTicks = [];
  const { setTimeout: timeout, setInterval: interval } = globalThis;
  globalThis.setTimeout = (fn, ms, ...args) => ms === 1000 ? reportTicks.push(fn) : timeout(fn, ms, ...args);
  let refresh;
  globalThis.setInterval = (fn, ms, ...args) => ms === 5000 ? (refresh = fn, null) : interval(fn, ms, ...args);
  let race;
  let missingShell = false;
  let needsYou = 2;
  let asks = { asks: [] };
  let slots = 3;
  let slotNote = "";
  let health = "ok";
  let waiting = { rows: { tickets: [], quiet_minutes: 5 } };
  let detailUnavailable = false;
  let models = [];
  let paid = 0;
  let holdDetail;
  let finishDetail;
  let holdShell;
  let finishShell;
  let writeConflict = { code: "version_conflict", error: "This object changed; refresh it before trying again.", current: { version: 3 } };
  const reads = (key) => calls.filter((call) => call.key === key);
  const snapshots = () => reads("live").length + reads("runs").length + reads("run").length + reads("/api/v1/runs").length;
  const running = () => runs.filter((run) => run.status === "running");
  const live = () => ({ waiting, live: { rows: { slots, max_parallel: 3,
    runs: running().map(({ status, ...row }) => ({ ...row, slot: row.id === "second" ? slotNote : "" })) } } });
  const sidebar = () => ({ running: running().length, needs_you: needsYou, health });
  globalThis.fetch = async (path, options) => {
    const url = new URL(path, "http://dashboard.test");
    const view = url.searchParams.get("view");
    const call = { key: view ?? url.pathname, headers: options.headers, body: options.body };
    calls.push(call);
    assert.equal(options.headers.get("X-Arete-Client"), "web/1.1");
    if (url.pathname.endsWith("/stop")) return json(writeConflict, 409);
    if (view === "health" || view === "live" && missingShell) {
      call.status = 404;
      return json({ error: "Missing" }, 404);
    }
    let position = seq;
    let payload;
    if (view === "live") payload = { sidebar: sidebar(), asks, view: live() };
    else if (view === "runs") {
      payload = { view: { runs: runs.slice(0, 25).map(({ version, ...row }) => row), more: true } };
    } else if (view === "run") {
      const run = runs.find((row) => row.id === url.searchParams.get("id"));
      if (detailUnavailable) return json({ error: "Temporarily unavailable" }, 503);
      if (race) {
        position = seq = race;
        race = null;
        Object.assign(run, { status: "pr", version: 2 });
      }
      const usage = run.id === "first" ? { paid, models } : { paid: 0, models: [] };
      payload = { sidebar: sidebar(), asks, view: { run: { ...run, paid: usage.paid }, models: usage.models, lines_from: "log" } };
    } else if (url.pathname === "/api/v1/runs") {
      position = seq === "5.40" ? "5.41" : seq;
      payload = { runs };
    } else if (url.pathname === "/api/sidebar") return json(sidebar(), 200, { "Arete-Seq": seq });
    else if (url.pathname === "/api/live") return json(live(), 200, { "Arete-Seq": seq });
    else if (url.pathname === "/api/asks") return json(asks, 200, { "Arete-Seq": seq });
    else if (url.pathname === "/api/health") return json({ serve: {}, checks: { rows: [] } });
    else if (url.pathname.startsWith("/api/log/")) return json({ lines: [] });
    else if (url.pathname.endsWith("/events")) {
      const from = Number(url.searchParams.get("from"));
      const found = url.pathname === "/api/runs/first/events" ? log.filter((event) => event.seq > from) : [];
      return new Response(found.map((event) => JSON.stringify(event)).join("\n"));
    } else assert.fail(`Unexpected request ${path}`);
    const tag = `"${Buffer.from(JSON.stringify([position, payload])).toString("base64url")}"`;
    call.status = options.headers.get("If-None-Match") === tag ? 304 : 200;
    call.tag = tag;
    const headers = { "Arete-Seq": position, ETag: tag };
    const response = call.status === 304 ? new Response(null, { status: 304, headers }) : json({ seq: position, ...payload }, 200, headers);
    if (view === "live" && holdShell) await new Promise((resolve) => { finishShell = resolve; });
    if (view === "run" && holdDetail) await new Promise((resolve) => { finishDetail = resolve; });
    return response;
  };

  const feed = ui.live("run", "first");
  const last = () => streams.at(-1);
  try {
    await until(() => ui.runView.value.turnsSource === "log" && last().url.searchParams.get("since") === "5.40", "initial snapshots settle");
    last().open();
    assert.equal(feed.snapshot.value.running, 3);
    assert.equal(ui.runView.value.queue.length, 30, "retain the history tail across a cursor mismatch");
    assert.equal(ui.runView.value.more, false);
    const initialReads = snapshots();
    const other = Object.assign(runs[2], { summary: "Other run", version: 2 });
    last().send(event("5.42", "run.changed", other));
    assert.equal(ui.runView.value.queue.find((row) => row.id === "third").summary, "Other run");
    assert.equal(ui.runView.value.detail.run.version, 1);

    const step = { run: "first", step: "build", try: 1 };
    const sendStep = (id, phase, data = step) => last().send({
      id, kind: `step.${phase}`, subject: `step:first/build/${data.try}`, data,
    });
    sendStep("5.43", "started");
    seq = "5.44";
    paid = 0.15;
    models = [{ tokens_in: 100, tokens_out: 25 }];
    sendStep(seq, "ended", { ...step, result: "pass" });
    assert.equal(ui.runView.value.events.at(-1).result, "pass");
    assert.equal(feed.snapshot.value.runs.find((row) => row.id === "first").step, "build");
    assert.equal(ui.runView.value.detail.run.paid, 0);
    await until(() => ui.runView.value.detail.run.paid === 0.15, "step completion refreshes paid cost");
    assert.deepEqual(ui.runView.value.detail.models, models, "step completion refreshes model totals");
    assert.equal(snapshots(), initialReads + 1, "the usage refresh reads only run detail");
    log.push({ ...step, event: "step_start", seq: 1 }, { ...step, event: "step_end", seq: 2, result: "pass" });
    last().send({ kind: "serve.beat", at: "now", age_seconds: 0 }, "serve.beat", "5.44");
    await until(() => ui.runView.value.events.at(-1).seq === 2, "canonical log replaces streamed steps");
    assert.equal(ui.runView.value.events.length, 2);
    assert.equal(snapshots(), initialReads + 1);

    holdDetail = true;
    seq = "5.45";
    paid = 0.2;
    models = [{ tokens_in: 150, tokens_out: 35 }];
    sendStep(seq, "ended", { ...step, try: 2, result: "pass" });
    await until(() => finishDetail, "a usage response is in flight");
    last().send(event("5.46", "run.changed", Object.assign(runs[0], { version: 2, tokens_in: 220 })));
    assert.equal(ui.runView.value.detail.run.version, 2);
    seq = "5.47";
    paid = 0.35;
    models = [{ tokens_in: 220, tokens_out: 50 }];
    Object.assign(runs[0], { status: "pr", version: 3 });
    holdDetail = false;
    finishDetail();
    const stopWatchingUsage = effect(() => {
      assert.equal(ui.runView.value.detail.run.status, "running");
      assert.equal(ui.runView.value.detail.run.version, 2);
      assert.notEqual(ui.runView.value.detail.run.paid, 0.2, "discard usage behind a newer run event");
    });
    try {
      await until(() => ui.runView.value.detail.run.paid === 0.35, "a newer usage read supersedes the late response");
    } finally { stopWatchingUsage(); }
    assert.deepEqual(ui.runView.value.detail.models, models);
    assert.equal(feed.snapshot.value.running, 3, "usage ahead of the stream preserves the shell count");
    last().send(event(seq, "run.ended", runs[0]));
    assert.equal(feed.snapshot.value.running, 2);
    assert.equal(ui.runView.value.detail.run.status, "pr");
    assert.equal(ui.runView.value.queue.find((row) => row.id === "first").status, "pr");
    await until(() => reportTicks.length, "the first terminal log read misses the report");
    log.push({ ...step, try: 2, event: "step_end", seq: 3, result: "pass" }, { event: "report", status: "pr", seq: 4 });
    reportTicks.shift()();
    await until(() => ui.runView.value.events.at(-1).event === "report", "catch-up finds a late report without a heartbeat");
    assert.equal(reportTicks.length, 0);
    await assert.rejects(sendWrite(prepareWrite("/api/runs/first/stop", {}, ui.runView.value.detail.run)),
      { status: 409, conflict: true, current: { version: 3 } });
    assert.equal(JSON.parse(calls.at(-1).body).base_version, 3);
    assert.match(staleWrite.value, /item changed/);
    staleWrite.value = "";
    writeConflict = { error: "The run is busy." };
    await assert.rejects(sendWrite(prepareWrite("/api/runs/first/stop", {}, ui.runView.value.detail.run)),
      { status: 409, conflict: false, message: "The run is busy." });
    assert.equal(staleWrite.value, "");

    const firstTag = reads("live").at(-1).tag;
    const beforeShell = snapshots();
    seq = "5.48";
    needsYou = 3;
    asks = { asks: [{ id: "new-question" }] };
    slots = 2;
    slotNote = "waiting on CI without a run slot";
    waiting = { rows: { tickets: [{ ticket: "repo#42", comments: 2 }], quiet_minutes: 5 } };
    health = "fault";
    Object.assign(runs[2], { status: "pr", version: 3 });
    refresh();
    await until(() => feed.snapshot.value.needsYou === 3, "a shell-only update reaches the dashboard without a stream event");
    assert.deepEqual(feed.snapshot.value.questions.data, asks);
    assert.equal(feed.snapshot.value.slots, 2);
    assert.equal(feed.snapshot.value.runs.find((row) => row.id === "second").slot, slotNote);
    assert.deepEqual(feed.snapshot.value.live.waiting, waiting);
    assert.equal(feed.snapshot.value.health, "fault");
    assert.equal(reads("live").at(-1).headers.get("If-None-Match"), firstTag);
    assert.equal(snapshots(), beforeShell + 1, "the cadence reads only the shell snapshot");
    assert.equal(feed.snapshot.value.running, 2, "a shell-only read leaves the run state coherent");
    assert.equal(ui.runView.value.queue.find((row) => row.id === "third").status, "running");
    last().send(event(seq, "run.ended", runs[2]));
    assert.equal(feed.snapshot.value.running, 1);
    last().send({ kind: "serve.beat", at: "latest heartbeat", age_seconds: 0 }, "serve.beat", seq);
    const acceptedTag = reads("live").at(-1).tag;
    refresh();
    await until(() => reads("live").at(-1).status === 304, "the next refresh is conditional");
    await delay(0);
    assert.equal(reads("live").at(-1).headers.get("If-None-Match"), acceptedTag);
    assert.equal(feed.snapshot.value.serve.at, "latest heartbeat");
    assert.equal(feed.snapshot.value.running, 1);
    assert.equal(ui.runView.value.queue.find((row) => row.id === "third").status, "pr");

    holdShell = true;
    needsYou = 4;
    slots = 1;
    refresh();
    await until(() => finishShell, "wait for a changed shell response to be in flight");
    Object.assign(runs[2], { status: "running", version: 4 });
    last().send(event("5.49", "run.started", runs[2]));
    holdShell = false;
    finishShell();
    await delay(0);
    assert.equal(feed.snapshot.value.needsYou, 3, "a shell response behind a newer event is discarded");
    assert.equal(feed.snapshot.value.slots, 2);
    assert.equal(feed.snapshot.value.running, 2);

    seq = "5.49";
    race = "5.50";
    const stopObserving = effect(() => {
      const status = ui.runView.value.detail?.run;
      if (status?.id === "second" && status.status === "pr") assert.equal(feed.snapshot.value.running, 1);
    });
    const second = ui.live("run", "second");
    try {
      await until(() => ui.runView.value.turnsSource === "log" && ui.runView.value.id === "second", "retry incoherent shell and detail");
    } finally { stopObserving(); }
    assert.ok(reads("live").length >= 3);
    await second.retry();
    assert.equal(reads("live").at(-1).status, 304);
    assert.equal(feed.snapshot.value.running, 1);

    detailUnavailable = true;
    ui.live("run", "third");
    await until(() => ui.runView.value.detailError?.status === 503
      && last().url.searchParams.get("turns") === "third", "the selected run detail fails while connected");
    last().open();
    assert.equal(ui.runView.value.detail, null);
    const detailReads = reads("run").length;
    detailUnavailable = false;
    race = "5.51";
    const stopWatchingRecovery = effect(() => {
      if (ui.runView.value.detail?.run.id === "third") {
        assert.equal(ui.runView.value.detail.run.status, "pr");
        assert.equal(feed.snapshot.value.running, 0);
      }
    });
    try {
      refresh();
      await until(() => ui.runView.value.detail?.run.id === "third", "the next connected refresh retries run detail");
    } finally { stopWatchingRecovery(); }
    assert.equal(ui.runView.value.detailError, null);
    assert.equal(reads("run").length, detailReads + 1);
    assert.equal(reads("/api/sidebar").length, 0);

    page.dispatchEvent(new Event("pagehide"));
    page.dispatchEvent(new Event("pageshow"));
    await until(() => last().url.searchParams.get("since") === "5.51", "304 keeps the cached snapshot cursor");
    seq = "6.1";
    last().send({ kind: "reset", reason: "epoch" }, "reset");
    await until(() => last().url.searchParams.get("since") === "6.1", "reset opens from a fresh snapshot");
    assert.equal(reads("live").at(-1).headers.get("If-None-Match"), null);

    missingShell = true;
    last().open();
    refresh();
    await until(() => reads("live").at(-1).status === 404, "connected shell revalidation sees a 404");
    await delay(0);
    assert.equal(reads("/api/sidebar").length, 0);
    assert.equal(feed.snapshot.value.fault, null);
    const settings = ui.live("settings", "health");
    await until(() => ui.screenReads.value.health?.data, "404 falls back to legacy shell and Health");
    assert.equal(ui.screenReads.value.health.error, null);
    assert.equal(reads("/api/sidebar").length, 1);
    last().open();
    last().send(event("6.2", "run.started", { id: "new", status: "running", version: 1 }));
    assert.equal(settings.snapshot.value.running, 1);
    last().send({ kind: "serve.beat" }, "serve.beat", "6.2");
    const shellReads = reads("live").length;
    refresh();
    await until(() => reads("live").length > shellReads, "connected legacy shell still probes v1");
    await delay(0);
    assert.equal(reads("live").at(-1).status, 404);
    assert.equal(reads("/api/sidebar").length, 1);

    globalThis.fetch = async () => json({ code: "client_too_old", error: "Too old" }, 400);
    await settings.retry();
    await until(() => clientNotice.value, "client_too_old shows a notice");
    assert.match(clientNotice.value, /Reload the page/);
  } finally {
    ui.stop();
    globalThis.setTimeout = timeout;
    globalThis.setInterval = interval;
  }
}

await transport(false);
await transport(true);
await snapshots();
