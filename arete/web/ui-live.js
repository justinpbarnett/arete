import { signal } from "./vendor-signals.js";

const endpoints = { sidebar: "/api/sidebar", live: "/api/live" };
const FAST_MS = 5000;
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

async function read(url) {
  const controller = new AbortController();
  const expiry = setTimeout(() => controller.abort(), TIMEOUT_MS);
  controllers.add(controller);
  try {
    const response = await fetch(url, { cache: "no-store", signal: controller.signal });
    if (!response.ok) return { error: { status: response.status, message: `HTTP ${response.status}` } };
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

async function round(run) {
  pause();
  if (document.hidden || active !== run) return;
  const token = generation;
  const began = Date.now();
  const delay = fastScreens.has(run.screen) ? FAST_MS : SLOW_MS;
  const wantLive = liveDue || delay <= FAST_MS || began - cache.liveTried >= delay - 500;
  const [sidebar, live] = await Promise.all([read(endpoints.sidebar), wantLive ? read(endpoints.live) : null]);
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
  timer = setTimeout(() => round(run), Math.max(0, FAST_MS - (Date.now() - began)));
}

export function stop() {
  const run = active;
  active = null;
  pause();
  run?.release();
}

export function live(screen) {
  stop();
  liveDue = true;
  const run = { screen };
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
      return round(run);
    },
  };
}
