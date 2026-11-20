import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";
import { openEvents } from "../arete/web/ui-events.js";
import { prepareWrite, sendWrite } from "../arete/web/ui-write.js";
import { clientNotice, staleWrite } from "../arete/web/ui-notice.js";

const channels = new Set();
class Channel {
  constructor(name) { this.name = name; channels.add(this); }
  postMessage(data) {
    for (const peer of channels) {
      if (peer !== this && peer.name === this.name) {
        queueMicrotask(() => { if (channels.has(peer)) peer.onmessage({ data }); });
      }
    }
  }
  close() { channels.delete(this); }
}

const locks = {
  owner: Promise.resolve(),
  request(_name, { signal }, use) {
    this.owner = this.owner.then(() => { if (!signal.aborted) return use(); });
    return this.owner;
  },
};
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
globalThis.BroadcastChannel = Channel;
globalThis.EventSource = Source;
globalThis.document = new EventTarget();
const page = new EventTarget();
globalThis.addEventListener = page.addEventListener.bind(page);
const ui = await import("../arete/web/ui-live.js?browser");

const event = (id, kind = "run.changed", run = "first") => ({
  id, kind, subject: `run:${run}`, data: { id: run, run },
});
const json = (body, status = 200, headers = {}) => Response.json(body, { status, headers });
async function until(predicate, reason) {
  for (let attempt = 0; attempt < 200; attempt++) {
    if (predicate()) return;
    await delay(1);
  }
  assert.fail(reason);
}

async function transport() {
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
    assert.equal(last().url.searchParams.get("turns"), "first");
    last().open();
    last().send(event("2.11"));
    last().send({ kind: "serve.beat", head: "2.99" }, "serve.beat", "2.11");
    last().send(event("2.12", "future.kind"));
    last().send(event("2.13"));
    await until(() => ids(second).length === 2, "follower receives supported events once");
    assert.equal(streams.length, 1);
    assert.deepEqual(ids(first), ["2.11", "2.13"]);
    lead.close();
    await until(() => streams.length === 2, "follower takes the lock on close");
    assert.equal(streams[0].readyState, Source.CLOSED);
    assert.equal(last().url.searchParams.get("since"), "2.13");
    last().open();
    last().onerror();
    last().onerror();
    assert.equal(polls.length, 0);
    last().onerror();
    assert.equal(polls.length, 1);
    assert.equal(polls[0].url.pathname, "/api/v1/events");
    assert.equal(polls[0].url.searchParams.get("timeout"), "25");
    assert.equal(polls[0].url.searchParams.get("since"), "2.13");
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
    assert.equal(last().readyState, Source.CLOSED);
    follow.baseline("3.2");
    assert.equal(last().url.searchParams.get("since"), "3.2");
  } finally {
    Date.now = now;
    lead.close();
    follow.close();
  }
}

async function snapshots() {
  const calls = [];
  let seq = "5.40";
  let count = 3;
  let version = 4;
  let rejectsClient = false;
  const tag = '"snapshot"';
  const running = ["first", "second", "third"].map((id) => ({ id, status: "running" }));
  const read = (view) => calls.filter((call) => call.view === view);
  globalThis.fetch = async (path, options) => {
    const url = new URL(path, "http://dashboard.test");
    const view = url.searchParams.get("view");
    const headers = { "Arete-Seq": seq, ETag: tag };
    calls.push({ path: url.pathname, view, headers: options.headers, body: options.body });
    assert.equal(options.headers.get("X-Arete-Client"), "web/1.1");
    if (url.pathname.endsWith("/stop")) return json({ code: "version_conflict", error: "Changed" }, 409);
    if (rejectsClient) return json({ code: "client_too_old", error: "Too old" }, 400);
    if (view === "live") {
      if (["5.41", "5.43"].includes(seq)) return new Response(null, { status: 304, headers });
      return json({ seq, sidebar: { running: count, needs_you: 2 }, asks: { asks: [] },
        view: { live: { rows: { runs: running } } } }, 200, headers);
    }
    if (view === "runs") return json({ seq, view: { runs: running } }, 200, headers);
    if (view === "run") return json({ seq, view: { run: { ...running[0], version }, lines_from: "feed" } }, 200, headers);
    if (url.pathname === "/api/v1/line-stream") return json({ seq, lines: [], after: 0 }, 200, headers);
    if (url.pathname === "/api/runs/first/events") return new Response("");
    if (view === "health") return json({ code: "not_found", error: "Missing" }, 404);
    if (url.pathname === "/api/health") return json({ serve: {}, checks: { rows: [] } });
    return json({ error: "Unexpected request" }, 500);
  };

  const feed = ui.live("run", "first");
  try {
    await until(() => ui.runView.value.turnsSource === "feed", "run uses snapshot and versioned line reader");
    const stream = streams.at(-1);
    stream.open();
    assert.equal(stream.url.searchParams.get("since"), "5.40");
    assert.equal(feed.snapshot.value.running, 3);
    assert.equal(ui.runView.value.queue.length, 3);
    assert.equal(ui.runView.value.detail.run.version, 4);

    seq = "5.41";
    stream.send(event(seq, "run.changed", "second"));
    await until(() => read("live").length === 2, "other run invalidates shell");
    assert.equal(read("live").at(-1).headers.get("If-None-Match"), tag);
    assert.equal(read("run").length, 1);
    await until(() => read("runs").length === 2, "other run invalidates queue");
    assert.equal(feed.snapshot.value.running, 3);
    assert.equal(feed.snapshot.value.fault, null);

    seq = "5.42";
    version = 5;
    count = 2;
    stream.send(event(seq, "run.ended"));
    await until(() => ui.runView.value.detail.run.version === 5, "selected run updates on its event");
    assert.equal(feed.snapshot.value.running, 2);
    const write = prepareWrite("/api/runs/first/stop", {}, ui.runView.value.detail.run);
    await assert.rejects(sendWrite(write), { status: 409, conflict: true });
    assert.equal(JSON.parse(calls.at(-1).body).base_version, 5);
    assert.match(staleWrite.value, /item changed/);

    seq = "5.43";
    page.dispatchEvent(new Event("pagehide"));
    page.dispatchEvent(new Event("pageshow"));
    await until(() => streams.at(-1).url.searchParams.get("since") === "5.43", "304 supplies the next stream cursor");
    assert.equal(read("live").at(-1).headers.get("If-None-Match"), tag);
    assert.equal(feed.snapshot.value.running, 2);

    seq = "6.1";
    streams.at(-1).send({ kind: "reset", reason: "epoch" }, "reset");
    await until(() => streams.at(-1).url.searchParams.get("since") === "6.1", "reset opens SSE from fresh snapshot");
    assert.equal(read("live").at(-1).headers.get("If-None-Match"), null);

    const settings = ui.live("settings", "health");
    await until(() => ui.screenReads.value.health?.data, "404 uses the existing Health route");
    assert.equal(ui.screenReads.value.health.error, null);
    assert.ok(calls.some((call) => call.path === "/api/health"));
    assert.ok(calls.every((call) => call.path !== "/api/sidebar"));

    rejectsClient = true;
    await settings.retry();
    await until(() => clientNotice.value, "client_too_old shows a notice");
    assert.match(clientNotice.value, /Reload the page/);
  } finally {
    ui.stop();
    page.dispatchEvent(new Event("pagehide"));
  }
}

await transport();
await snapshots();
