import { signal } from "./vendor-signals.js";
import * as data from "./ui-data.js";

const endpoints = { sidebar: "/api/sidebar", live: "/api/live" };
const FAST_MS = 5000;
const RUN_MS = 2500;
const SLOW_MS = 30000;
const TIMEOUT_MS = 3000;
const OFFLINE_ROUNDS = 2;
const STALE_SECONDS = 60;
const fastScreens = new Set(["floor", "triage", "board", "task", "run"]);

const cache = { sidebar: null, sidebarAt: 0, live: null, liveAt: 0, liveTried: 0, errors: { sidebar: null, live: null }, failures: 0, settled: false };
const controllers = new Set();
let generation = 0;
let timer = null;
let active = null;
let liveDue = true;

const asksCount = (asks) => {
  if (!asks) return null;
  const lists = ["approvals", "skipped", "ready"].map((part) => asks[part]?.rows ?? []);
  const proposals = Object.values(asks.proposals?.rows ?? {});
  return lists.reduce((sum, rows) => sum + rows.length, 0) + proposals.reduce((sum, rows) => sum + rows.length, 0) + (asks.draft ? 1 : 0);
};

function snapshotOf() {
  const sidebar = cache.sidebar;
  const payload = cache.live;
  const rows = payload?.live?.rows;
  return {
    needsYou: sidebar?.needs_you ?? asksCount(payload?.needs_you),
    running: sidebar?.running ?? null,
    health: sidebar?.health ?? null,
    serve: sidebar?.serve ?? null,
    slots: rows?.slots ?? null,
    capacity: rows?.max_parallel ?? null,
    received: cache.sidebarAt,
    live: payload,
    asks: payload?.needs_you ?? null,
    runs: rows?.runs ?? null,
    liveReceived: cache.liveAt,
    errors: { ...cache.errors },
    fault: cache.errors.live ?? cache.errors.sidebar,
    failures: cache.failures,
    offline: cache.failures >= OFFLINE_ROUNDS,
    pending: !cache.settled,
  };
}

export const snapshot = signal(snapshotOf());

export function connectivity(current, now) {
  if (current.offline) return "OFFLINE";
  const age = current.serve?.age_seconds;
  const valid = Number.isFinite(age) && age >= 0 && current.received > 0;
  return valid && age + Math.max(0, now - current.received) / 1000 > STALE_SECONDS ? "STALE" : "";
}

export function readFault(screen, current) {
  const panel = screen === "floor" ? current.live?.live : screen === "triage"
    ? Object.values(current.asks ?? {}).find((part) => part?.error) : null;
  return panel?.error ? { status: null, message: panel.error } : current.fault;
}

async function refusal(response) {
  const body = await response.json?.()?.catch(() => null);
  return typeof body?.error === "string" ? body.error : `HTTP ${response.status}`;
}

async function read(url, { text = false, say = false } = {}) {
  const controller = new AbortController();
  const expiry = setTimeout(() => controller.abort(), TIMEOUT_MS);
  controllers.add(controller);
  try {
    const response = await fetch(url, { cache: "no-store", signal: controller.signal });
    if (!response.ok) return { error: { status: response.status, message: say ? await refusal(response) : `HTTP ${response.status}` } };
    if (text) return { data: await response.text() };
    const data = await response.json().catch(() => null);
    if (data && typeof data === "object" && !Array.isArray(data)) return { data };
    return { error: { status: response.status, message: "Malformed response" } };
  } catch (error) {
    return { error: { status: null, message: error?.name === "AbortError" ? "Request timed out" : "Network error" } };
  } finally {
    clearTimeout(expiry);
    controllers.delete(controller);
  }
}

function pause() {
  generation += 1;
  clearTimeout(timer);
  timer = null;
  for (const controller of controllers) controller.abort();
  controllers.clear();
}

const blankRuns = () => ({ queue: null, more: false, queueError: null, id: "", detail: null, detailError: null, events: [],
  seqs: new Set(), seq: 0, eventsDone: false, eventsError: null, turns: [], turnsAfter: 0, turnsSource: "", turnsError: null,
  turnsDone: false, stream: null, selection: 0, force: false, settled: false });
const rs = blankRuns();

const viewOf = () => ({ queue: rs.queue, more: rs.more, queueError: rs.queueError, id: rs.id, detail: rs.detail, detailError: rs.detailError,
  events: rs.events, eventsError: rs.eventsError, turns: rs.turns, turnsSource: rs.turnsSource, turnsError: rs.turnsError, settled: rs.settled });

export const runView = signal(viewOf());
const publishRuns = () => { runView.value = viewOf(); };

function dropStream() {
  rs.stream?.abort();
  rs.stream = null;
}

function selectRun(id) {
  dropStream();
  const queue = { queue: rs.queue, more: rs.more, queueError: rs.queueError, settled: rs.settled, selection: rs.selection + 1 };
  Object.assign(rs, blankRuns(), queue, { id });
  publishRuns();
}

function parseEvents(text) {
  const found = [];
  let bad = false;
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    try {
      const event = JSON.parse(line);
      if (event && typeof event === "object" && Number.isFinite(event.seq)) found.push(event);
      else bad = true;
    } catch { bad = true; }
  }
  return { found, bad };
}

function takeEvents(text) {
  const { found, bad } = parseEvents(text);
  const fresh = found.filter((event) => !rs.seqs.has(event.seq) && rs.seqs.add(event.seq));
  if (fresh.length) {
    rs.events = [...rs.events, ...fresh].sort((a, b) => a.seq - b.seq);
    rs.seq = Math.max(rs.seq, ...fresh.map((event) => event.seq));
  }
  rs.eventsError = bad ? { status: null, message: "Malformed events" } : null;
  publishRuns();
}

async function follow(selection) {
  const controller = new AbortController();
  rs.stream = controller;
  let rest = "";
  try {
    const response = await fetch(data.run(rs.id).events(rs.seq, true), { cache: "no-store", signal: controller.signal });
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    for (;;) {
      const { done, value } = await reader.read();
      if (selection !== rs.selection) return;
      rest += decoder.decode(value ?? new Uint8Array(), { stream: !done });
      const cut = done ? rest.length : rest.lastIndexOf("\n") + 1;
      takeEvents(rest.slice(0, cut));
      rest = rest.slice(cut);
      if (done) return;
    }
  } catch (error) {
    if (error?.name !== "AbortError" && selection === rs.selection) {
      rs.eventsError = { status: null, message: "Event stream interrupted" };
      publishRuns();
    }
  } finally {
    if (rs.stream === controller) rs.stream = null;
  }
}

function logTurns(lines) {
  const seen = new Map();
  return lines.map((line) => {
    const text = String(line);
    const nth = (seen.get(text) ?? 0) + 1;
    seen.set(text, nth);
    return { id: `l:${nth}:${text}`, at: "", kind: "log", text };
  });
}

async function readTurns(selection, token, run) {
  const id = rs.id;
  const source = rs.detail.lines_from;
  const found = await read(source === "feed" ? data.run(id).feed(rs.turnsAfter) : data.run(id).log, { say: true });
  if (token !== generation || active !== run || selection !== rs.selection) return false;
  const lines = found.data?.lines;
  if (!Array.isArray(lines)) {
    rs.turnsError = found.error ?? { status: null, message: "Malformed response" };
    return true;
  }
  rs.turnsError = null;
  rs.turnsSource = source;
  if (source === "feed") {
    const fresh = lines.filter((line) => line && Number.isFinite(line.id) && line.id > rs.turnsAfter);
    rs.turns = [...rs.turns, ...fresh.map((line) => ({ id: `f:${line.id}`, at: line.ts ?? "", kind: line.kind ?? "", text: String(line.text ?? "") }))];
    rs.turnsAfter = Math.max(rs.turnsAfter, ...fresh.map((line) => line.id));
  } else rs.turns = logTurns(lines);
  rs.turnsDone = rs.detail.run.status !== "running";
  return true;
}

async function readRun(run, token) {
  const selection = rs.selection;
  const id = run.id;
  const wantDetail = id && (rs.force || !rs.detail || rs.detail.run?.status === "running");
  const [queue, detail] = await Promise.all([read(data.runs(), { say: true }), wantDetail ? read(data.run(id).detail, { say: true }) : null]);
  const stale = () => token !== generation || active !== run || selection !== rs.selection;
  if (stale()) return;
  if (Array.isArray(queue.data?.runs)) { rs.queue = queue.data.runs; rs.more = Boolean(queue.data.more); rs.queueError = null; }
  else rs.queueError = queue.error ?? { status: null, message: "Malformed response" };
  if (detail) {
    if (detail.data?.run && typeof detail.data.run === "object") { rs.detail = detail.data; rs.detailError = null; }
    else rs.detailError = detail.error ?? { status: null, message: "Malformed response" };
  }
  rs.settled = true;
  publishRuns();
  if (!rs.detail) return;
  const running = rs.detail.run.status === "running";
  if (running) rs.turnsDone = false;
  if ((!rs.turnsDone || rs.force) && !await readTurns(selection, token, run)) return;
  publishRuns();
  if (running) {
    if (!rs.stream) follow(selection);
  } else {
    dropStream();
    if (!rs.eventsDone || rs.force) {
      const found = await read(data.run(id).events(rs.seq), { text: true, say: true });
      if (stale()) return;
      if (found.data != null) { takeEvents(found.data); rs.eventsDone = !rs.eventsError; }
      else rs.eventsError = found.error;
    }
  }
  rs.force = false;
  publishRuns();
}

async function round(run) {
  pause();
  if (document.hidden || active !== run) { dropStream(); return; }
  const token = generation;
  const began = Date.now();
  const delay = fastScreens.has(run.screen) ? FAST_MS : SLOW_MS;
  const wantLive = liveDue || delay <= FAST_MS || began - cache.liveTried >= delay - 500;
  const [sidebar, live] = await Promise.all([read(endpoints.sidebar), wantLive ? read(endpoints.live) : null,
    run.screen === "run" ? readRun(run, token) : null]);
  if (token !== generation || active !== run) return;
  const finished = Date.now();
  if (sidebar.data) { cache.sidebar = sidebar.data; cache.sidebarAt = finished; }
  cache.errors.sidebar = sidebar.error ?? null;
  if (live) {
    cache.liveTried = began;
    if (live.data) { cache.live = live.data; cache.liveAt = finished; }
    cache.errors.live = live.error ?? null;
  }
  const failed = Boolean(sidebar.error || live?.error);
  cache.failures = failed ? cache.failures + 1 : 0;
  cache.settled = true;
  liveDue = failed;
  snapshot.value = snapshotOf();
  const every = run.screen === "run" ? RUN_MS : FAST_MS;
  timer = setTimeout(() => round(run), Math.max(0, every - (Date.now() - began)));
}

export function stop() {
  const run = active;
  active = null;
  pause();
  dropStream();
  run?.release();
}

export function live(screen, id = "") {
  stop();
  selectRun(screen === "run" ? id : "");
  liveDue = true;
  const run = { screen, id };
  const visibility = () => { liveDue = true; round(run); };
  document.addEventListener("visibilitychange", visibility);
  run.release = () => document.removeEventListener("visibilitychange", visibility);
  active = run;
  round(run);
  return {
    snapshot,
    stop,
    retry() {
      if (active !== run) return Promise.resolve();
      liveDue = true;
      rs.force = true;
      return round(run);
    },
  };
}
