import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";
import { openEvents } from "../arete/web/ui-events.js";
import { prepareWrite, sendWrite } from "../arete/web/ui-write.js";
import { clientNotice, staleWrite } from "../arete/web/ui-notice.js";
import { parse } from "../arete/web/ui-router.js";
import { TaskQueue } from "../arete/web/ui-work.js";
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
  const now = Date.now;
  globalThis.setTimeout = (fn, ms, ...args) => ms === 1000 ? reportTicks.push(fn) : timeout(fn, ms, ...args);
  let refresh;
  globalThis.setInterval = (fn, ms, ...args) => ms === 5000 ? (refresh = fn, null) : interval(fn, ms, ...args);
  let race;
  let missingShell = false;
  let needsYou = 2;
  let asks = { asks: [] };
  let slots = 3;
  let notedRun = "second";
  let slotNote = "";
  let health = "ok";
  let serve;
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
    runs: running().map(({ status, ...row }) => ({ ...row, slot: row.id === notedRun ? slotNote : "" })) } } });
  const sidebar = () => ({ running: running().length, needs_you: needsYou, health, serve });
  globalThis.fetch = async (path, options) => {
    const url = new URL(path, "http://dashboard.test");
    const view = url.searchParams.get("view");
    const call = { key: view ?? url.pathname, headers: options.headers, body: options.body };
    calls.push(call);
    assert.equal(options.headers.get("X-Arete-Client"), "web/1.1");
    if (url.pathname === "/api/v1/events") return new Promise((respond) => { call.respond = respond; });
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
    last().send({ kind: "serve.beat", at: "2026-11-21T05:00:00Z", age_seconds: 0 }, "serve.beat", "5.44");
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
    last().send({ kind: "serve.beat", at: "2026-11-21T05:01:00Z", age_seconds: 0 }, "serve.beat", seq);
    const acceptedTag = reads("live").at(-1).tag;
    refresh();
    await until(() => reads("live").at(-1).status === 304, "the next refresh is conditional");
    await delay(0);
    assert.equal(reads("live").at(-1).headers.get("If-None-Match"), acceptedTag);
    assert.equal(feed.snapshot.value.serve.at, "2026-11-21T05:01:00Z");
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
    Object.assign(runs[2], { status: "running", version: 3 });
    last().send({ kind: "reset", reason: "epoch" }, "reset");
    await until(() => last().url.searchParams.get("since") === "6.1", "reset opens from a fresh snapshot");
    assert.equal(reads("live").at(-1).headers.get("If-None-Match"), null);

    let clock = now();
    Date.now = () => clock;
    const latestPoll = () => reads("/api/v1/events").at(-1);
    last().open();
    last().send({ kind: "serve.beat", at: "2026-11-21T05:02:00Z", age_seconds: 40 }, "serve.beat", seq);
    for (let i = 0; i < 3; i++) last().onerror();
    const emptyPoll = () => json({ events: [], next: seq, reset: null });
    clock += 25000;
    latestPoll().respond(emptyPoll());
    await until(() => reads("/api/v1/events").length === 2, "long polling continues after an idle response");
    assert.equal(latestPoll().headers.get("Last-Event-ID"), seq);
    assert.equal(ui.connectivity(feed.snapshot.value, clock), "STALE");

    const previousTag = reads("live").at(-1).tag;
    const beforePollRefresh = snapshots();
    const idlePoll = latestPoll();
    needsYou = 5;
    asks = { asks: [{ id: "idle-question" }] };
    slots = 0;
    notedRun = "third";
    slotNote = "waiting for review";
    health = "warn";
    waiting = { rows: { tickets: [{ ticket: "repo#43", comments: 1 }], quiet_minutes: 5 } };
    serve = { at: "2026-11-21T05:03:00Z", age_seconds: 57, alive: true };
    refresh();
    await until(() => feed.snapshot.value.needsYou === 5, "fields refresh while the next long poll is idle");
    assert.equal(latestPoll(), idlePoll);
    assert.equal(reads("live").at(-1).headers.get("If-None-Match"), previousTag);
    assert.equal(snapshots(), beforePollRefresh + 1);
    assert.deepEqual(feed.snapshot.value.questions.data, asks);
    assert.equal(feed.snapshot.value.slots, slots);
    assert.equal(feed.snapshot.value.runs.find((row) => row.id === notedRun).slot, slotNote);
    assert.deepEqual(feed.snapshot.value.live.waiting, waiting);
    assert.equal(feed.snapshot.value.health, health);
    assert.deepEqual(feed.snapshot.value.serve, serve);
    assert.equal(feed.snapshot.value.received, clock);
    assert.equal(ui.connectivity(feed.snapshot.value, clock), "");

    const received = feed.snapshot.value.received;
    const liveBefore304 = reads("live").length;
    clock += 5000;
    refresh();
    await until(() => reads("live").length === liveBefore304 + 1, "the long-poll shell revalidates its snapshot");
    await delay(0);
    assert.equal(reads("live").at(-1).status, 304);
    assert.equal(feed.snapshot.value.received, received);
    assert.equal(ui.connectivity(feed.snapshot.value, clock), "STALE");
    assert.equal(reads("/api/sidebar").length, 0);

    clock += 20000;
    seq = "6.2";
    Object.assign(runs[2], { status: "pr", version: 4 });
    idlePoll.respond(json({ events: [event(seq, "run.ended", runs[2])], next: seq, reset: null }));
    await until(() => reads("/api/v1/events").length === 3, "long polling continues from the run end");
    assert.equal(feed.snapshot.value.running, 0);
    assert.equal(ui.runView.value.detail.run.status, "pr");
    assert.equal(ui.runView.value.queue.length, 30);
    assert.equal(latestPoll().headers.get("Last-Event-ID"), seq);

    clock += 25000;
    const beforeRecovery = streams.length;
    latestPoll().respond(emptyPoll());
    await until(() => streams.length === beforeRecovery + 1, "SSE resumes after idle long polling");
    assert.equal(last().url.searchParams.get("since"), seq);

    missingShell = true;
    last().open();
    refresh();
    await until(() => reads("live").at(-1).status === 404, "connected shell revalidation sees a 404");
    await delay(0);
    assert.equal(reads("/api/sidebar").length, 0);
    assert.equal(feed.snapshot.value.fault, null);
    const settings = ui.live("settings", "health");
    await until(() => ui.screenReads.value.health?.data && !last().url.searchParams.has("turns"),
      "404 falls back to legacy shell and Health while the stream drops the run subscription");
    assert.equal(ui.screenReads.value.health.error, null);
    assert.equal(reads("/api/sidebar").length, 1);
    last().open();
    last().send(event("6.3", "run.started", { id: "new", status: "running", version: 1 }));
    assert.equal(settings.snapshot.value.running, 1);
    last().send({ kind: "serve.beat" }, "serve.beat", "6.3");
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
    Date.now = now;
    globalThis.setTimeout = timeout;
    globalThis.setInterval = interval;
  }
}

async function boardSnapshots() {
  const calls = [];
  const task = { id: "t-1234", repo: "sample", title: "Board card", state: "todo", updated: "1", version: 1 };
  const closed = { ...task, id: "t-9999", state: "closed" };
  let rows = [task, closed];
  let seq = "9.10";
  let boardStatus = 200;
  let taskListStatus = 200;
  let taskListSequenced = true;
  let holdBoard = false;
  let releaseBoard;
  const { setInterval: interval, Date: { now } } = globalThis;
  let clock = now();
  Date.now = () => clock;
  let refresh;
  globalThis.setInterval = (fn, ms, ...args) => ms === 5000 ? (refresh = fn, null) : interval(fn, ms, ...args);
  globalThis.fetch = async (path, options) => {
    const key = new URL(path, "http://dashboard.test").searchParams.get("view") ?? path;
    const tag = `"${key}-${seq}"`;
    const validator = options.headers.get("If-None-Match");
    const configured = key === "board" ? boardStatus : key === "/api/v1/tasks" ? taskListStatus : 200;
    const status = configured !== 200 ? configured : validator === tag ? 304 : 200;
    calls.push({ key, status, validator });
    if (status >= 400) return json(key === "board" ? { error: "Board unavailable" }
      : { detail: "Task list unavailable" }, status);
    const headers = { ETag: tag, "Arete-Seq": seq };
    if (key === "board" && holdBoard) await new Promise((resolve) => { releaseBoard = resolve; });
    if (status === 304) return new Response(null, { status, headers });
    if (key === "board") return json({ sidebar: { running: 17, serve: { at: "2099-01-01T00:00:00Z", age_seconds: 0 } },
      asks: { asks: [] }, view: { tasks: rows, running_ids: [] } }, 200, headers);
    if (key === "/api/v1/tasks") return json({ tasks: rows }, 200, taskListSequenced ? headers : {});
    if (key === "/api/tasks") return json({ tasks: rows });
    assert.equal(key, "live");
    const runs = seq === "9.10" ? [{ id: "already-running" }] : [];
    const sidebar = { running: runs.length, needs_you: runs.length ? 0 : 1,
      serve: { at: "2026-11-21T05:04:00Z", age_seconds: 0 } };
    return json({ sidebar, asks: { asks: [] }, view: { live: { rows: { runs } } } }, 200, headers);
  };
  const open = (hash) => { const route = parse(hash); return ui.live(route.screen, ...route.path); };
  const last = () => streams.at(-1);
  const boardCalls = () => calls.filter(({ key }) => key === "board");
  const taskListCalls = () => calls.filter(({ key }) => key === "/api/v1/tasks" || key === "/api/tasks");
  const tasks = () => ui.screenReads.value.tasks;
  const state = () => tasks().data.tasks.find((row) => row.id === task.id).state;
  const groups = (hash) => {
    const queue = new TaskQueue().render({ route: parse(hash), resource: tasks() }, { query: "" });
    return queue.props.children(tasks().data).props.children[1].filter(Boolean);
  };
  const columns = (hash) => {
    return Object.fromEntries(groups(hash).map((group) => {
      const [label, , count] = group.props.children[0].props.children;
      return [label, count.props.children];
    }));
  };
  const sendRun = (id, kind, status, row, subject = "run:board-run") => last().send({ id, kind, subject,
    data: { id: "board-run", issue: task.id, repo: task.repo, status, board_task: row } });
  try {
    open("#board");
    await until(() => last().url.searchParams.get("since") === "9.10", "Board keeps its own snapshot cursor");
    last().open();
    assert.equal(state(), "todo");
    assert.deepEqual(calls.map(({ key }) => key), ["live", "board"]);
    assert.equal(ui.snapshot.value.running, 1);
    last().send(event("9.11", "run.changed", { id: "already-running", status: "running" }));
    assert.equal(ui.snapshot.value.running, 1, "a change to an active run leaves the badge unchanged");
    last().send(event("9.12", "run.ended", { id: "already-running", status: "done" }));
    assert.equal(ui.snapshot.value.running, 0, "ending the pre-existing run clears the badge");
    clock += 61000;
    const staleReceipt = ui.snapshot.value.received;
    assert.equal(ui.connectivity(ui.snapshot.value, clock), "STALE");
    holdBoard = true;
    open("#board/open-work");
    await until(() => releaseBoard, "Open work reads the Board projection once");
    assert.deepEqual(calls.map(({ key }) => key), ["live", "board", "board"]);
    assert.deepEqual(boardCalls().at(-1), { key: "board", status: 304, validator: '"board-9.10"' });

    const running = { ...task, state: "running", updated: "2", version: 2 };
    sendRun("9.13", "run.started", "running", task);
    assert.equal(state(), "todo", "a registered run can precede the stored task transition");
    last().send({ id: "9.14", kind: "task.changed", subject: `task:${task.id}`, data: running });
    holdBoard = false;
    releaseBoard();
    await until(() => state() === "running", "an event during the conditional read moves the cached card");
    assert.equal(ui.connectivity(ui.snapshot.value, clock), "STALE", "the Board 304 preserves heartbeat age");
    assert.equal(ui.snapshot.value.received, staleReceipt);
    assert.equal(ui.snapshot.value.running, 1, "the Board 304 preserves the event-driven badge");
    last().send({ kind: "serve.beat", at: "2026-11-21T05:04:00Z", age_seconds: 0 }, "serve.beat", "9.14");
    assert.equal(ui.connectivity(ui.snapshot.value, clock), "STALE");
    last().send({ kind: "serve.beat", at: "2026-11-21T05:03:00Z", age_seconds: 0 }, "serve.beat", "9.14");
    assert.equal(ui.connectivity(ui.snapshot.value, clock), "", "a fresh beat after a backward clock correction clears STALE");
    assert.deepEqual(columns("#board"), { Running: 1, Closed: 1 });
    assert.deepEqual(columns("#board/open-work"), { Running: 1 });
    const review = { ...task, state: "pr", updated: "3", version: 3 };
    sendRun("9.15", "run.changed", "pr", review, "run:other-run");
    assert.equal(state(), "running");
    sendRun("9.16", "run.changed", "running", review);
    assert.equal(state(), "pr", "run events apply the task snapshot even if run status differs");
    const done = { ...task, state: "done", updated: "4", version: 4 };
    sendRun("9.17", "run.ended", "pr", done);
    assert.equal(state(), "done");
    seq = "9.18";
    assert.equal(ui.snapshot.value.running, 0);
    assert.equal(tasks().data.tasks.find((row) => row.id === closed.id).state, "closed");
    refresh();
    await until(() => ui.snapshot.value.needsYou === 1, "shell refresh is independent");
    assert.equal(ui.snapshot.value.running, 0, "refresh leaves the finished run off the badge");
    assert.equal(calls.filter(({ key }) => key === "live").length, 2);
    assert.equal(boardCalls().length, 2);
    assert.equal(taskListCalls().length, 0);

    seq = "9.19";
    const boardStart = calls.length;
    let board = open("#board");
    await until(() => state() === "todo", "Board reads separately from the shared shell");
    assert.deepEqual(calls.slice(boardStart).map(({ key }) => key), ["board"]);
    assert.equal(ui.snapshot.value.running, 0, "a fresh Board snapshot leaves the badge intact");
    assert.equal(ui.snapshot.value.serve.at, "2026-11-21T05:03:00Z");
    seq = "9.20";
    rows = [done, closed];
    last().send({ id: seq, kind: "principal.changed", subject: "principal:u:1234", data: { id: "u:1234" } });
    await until(() => state() === "done", "principal changes revalidate Board access");
    assert.equal(boardCalls().length, 4);
    assert.equal(boardCalls().at(-1).validator, '"board-9.19"');
    boardStatus = 503;
    await board.retry();
    assert.equal(tasks().error?.status, 503, "service errors surface without falling back");
    assert.equal(taskListCalls().length, 0);

    open("#floor");
    assert.equal(tasks(), undefined);
    boardStatus = 404;
    rows = [{ ...done, title: "Finished fallback card" }, { ...closed, title: "Closed fallback card" }];
    const firstFallback = calls.length;
    const messages = [];
    const { error, warn } = console;
    console.error = (...args) => messages.push(["error", ...args]);
    console.warn = (...args) => messages.push(["warn", ...args]);
    try {
      board = open("#board");
      await until(() => tasks()?.data, "Board renders the v1 task list on its first read");
      assert.deepEqual(calls.slice(firstFallback).filter(({ key }) => key !== "live").map(({ key, status }) => [key, status]),
        [["board", 404], ["/api/v1/tasks", 200]]);
      assert.equal(tasks().error, null);
      assert.deepEqual(columns("#board"), { Done: 1, Closed: 1 });
      const cards = groups("#board").flatMap((group) => {
        const list = group.props.children[1];
        return list.type(list.props).props.children;
      });
      assert.deepEqual(cards.map((card) => [card.props["data-item"], card.props.children[0].props.children[0].props.children]),
        [[task.id, "Finished fallback card"], [closed.id, "Closed fallback card"]]);
      assert.deepEqual(messages, [], "a missing Board view emits no console errors or warnings");
    } finally {
      console.error = error;
      console.warn = warn;
    }

    boardStatus = 400;
    taskListSequenced = false;
    rows = [task, closed];
    await board.retry();
    assert.equal(boardCalls().at(-1).status, 400);
    assert.deepEqual(taskListCalls().slice(1).map(({ key, status }) => [key, status]), [["/api/v1/tasks", 200]]);
    assert.equal(tasks().error, null);
    assert.equal(state(), "todo", "an unsequenced v1 task list replaces a sequenced Board snapshot");
    sendRun("9.21", "run.changed", "running", task);

    boardStatus = 200;
    seq = "9.19";
    rows = [running, closed];
    await board.retry();
    assert.equal(state(), "running", "the next sequenced answer restarts the guard");
    seq = "9.18";
    rows = [task, closed];
    await board.retry();
    assert.equal(state(), "running", "an older sequenced answer cannot replace a newer one");

    boardStatus = 404;
    taskListStatus = 404;
    const fallbackStart = calls.length;
    rows = [review, closed];
    await board.retry();
    assert.deepEqual(calls.slice(fallbackStart).filter(({ key }) => key !== "live").map(({ key, status }) => [key, status]),
      [["board", 404], ["/api/v1/tasks", 404], ["/api/tasks", 200]]);
    assert.equal(tasks().error, null);
    assert.deepEqual(columns("#board"), { "In review": 1, Closed: 1 });
    assert.deepEqual(columns("#board/open-work"), { "In review": 1 });

    taskListStatus = 503;
    const start = taskListCalls().length;
    await board.retry();
    assert.equal(tasks().error?.message, "Task list unavailable", "a v1 task list service failure surfaces");
    assert.deepEqual(taskListCalls().slice(start).map(({ key, status }) => [key, status]), [["/api/v1/tasks", 503]]);

    taskListSequenced = true;
    for (const [status, position] of [[404, "9.22"], [200, "9.23"]]) {
      taskListStatus = status;
      open("#floor");
      assert.equal(tasks(), undefined);
      rows = [running, closed];
      open("#board");
      await until(() => tasks()?.data, "Board renders the fallback list on its first read");
      assert.deepEqual(columns("#board"), { Running: 1, Closed: 1 });

      assert.equal(last().readyState, 1);
      rows = [review, closed];
      sendRun(position, "run.ended", "pr");
      refresh();
      const path = status === 404 ? "/api/tasks" : "/api/v1/tasks";
      await until(() => state() === "pr", `a connected Board refreshes ${path} after an end event without a task update`);
      assert.deepEqual(columns("#board/open-work"), { "In review": 1 });
    }
  } finally {
    ui.stop();
    Date.now = now;
    globalThis.setInterval = interval;
  }
}

await transport(false);
await transport(true);
await snapshots();
await boardSnapshots();
