import { batch, signal } from "./vendor-signals.js";
import * as data from "./ui-data.js";
import { newer } from "./ui-snapshot.js";
import { openEvents } from "./ui-events.js";
import { webFetch } from "./ui-request.js";

const TIMEOUT_MS = 3000;
const RETRY_MS = 5000;
const STALE_SECONDS = 60;
const cache = { sidebar: null, live: null, questions: null, at: 0, errors: {}, failures: 0, settled: false };
const emptyRun = (id = "") => ({ id, queue: null, more: false, queueError: null, detail: null, detailError: null,
  events: [], eventsError: null, turns: [], turnsError: null, turnsSource: "", settled: false });
export const screenReads = signal({});
export const runView = signal(emptyRun());
let active;
let feed;
let connected = false;

function snapshotOf() {
  const { sidebar, live, questions, at, errors, failures, settled } = cache;
  const rows = live?.live?.rows;
  return {
    needsYou: sidebar?.needs_you ?? null, questions, running: sidebar?.running ?? null,
    health: sidebar?.health ?? null, serve: sidebar?.serve ?? null, received: at,
    slots: rows?.slots ?? null, capacity: rows?.max_parallel ?? null, live, asks: live?.needs_you ?? null,
    runs: rows?.runs ?? null, errors, fault: errors.live ?? errors.sidebar,
    offline: failures >= 2, pending: !settled,
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
  const resourceError = Object.values(screenReads.value).find((part) => part.error)?.error
    ?? (["triage", "run"].includes(screen) ? current.questions?.error : null);
  return resourceError ?? (panel?.error ? { status: null, message: panel.error } : current.fault);
}

const malformed = () => ({ status: null, message: "Malformed response" });

async function bounded(signal, read) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal.aborted) return { error: { status: null, message: "Request canceled" } };
  signal.addEventListener("abort", abort, { once: true });
  const timeout = setTimeout(abort, TIMEOUT_MS);
  try { return await read(controller.signal); }
  catch (error) {
    return { error: { status: null, message: error?.name === "AbortError" ? "Request timed out" : "Network error" } };
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener("abort", abort);
  }
}

async function legacy(url, signal, asText = false) {
  return bounded(signal, async (signal) => {
    const response = await webFetch(url, { cache: "no-store", signal });
    if (response.ok && asText) return { data: await response.text() };
    const body = await response.json().catch(() => null);
    if (!response.ok) return { error: { status: response.status, message: body?.error ?? `HTTP ${response.status}` } };
    return body && typeof body === "object" ? { data: body, seq: response.headers.get("Arete-Seq") ?? body.seq }
      : { error: malformed() };
  });
}

async function versioned(url, signal, fallback, select = (body) => body) {
  const result = await bounded(signal, (signal) => data.snapshotRead(url, signal));
  if (result.error?.status === 404) return { ...await fallback(), fallback: true };
  if (result.error) return result;
  return { data: select(result.data), seq: result.data.seq };
}

async function shell(signal) {
  const result = await versioned(data.screen("live"), signal, async () => {
    const [sidebar, view, asks] = await Promise.all(
      ["/api/sidebar", "/api/live", "/api/asks"].map((url) => legacy(url, signal)));
    const error = sidebar.error ?? view.error ?? asks.error;
    return error ? { error } : { data: { sidebar: sidebar.data, view: view.data, asks: asks.data }, seq: sidebar.seq };
  });
  const body = result.data;
  if (result.error || (Number.isInteger(body?.sidebar?.running) && body.view?.live && Array.isArray(body.asks?.asks))) return result;
  return { error: malformed() };
}

const projection = (view, path, id) => (signal) => versioned(data.screen(view, id), signal, () => legacy(path, signal), (body) => body.view);

async function runQueue(signal) {
  const result = await projection("runs", data.runs())(signal);
  if (!result.data?.more || !Array.isArray(result.data.runs) || result.fallback) return result;
  const history = await bounded(signal, (signal) => data.snapshotRead("/api/v1/runs", signal));
  if (!Array.isArray(history.data?.runs) || history.data.seq !== result.seq) return result;
  const current = new Set(result.data.runs.map((run) => run.id));
  const tail = history.data.runs.filter((run) => !current.has(run.id)).map((run) => ({
    ...run, ticket: run.issue ? `${run.repo ?? ""}${/^\d+$/.test(run.issue) ? "#" : " "}${run.issue}` : run.loop || run.repo || run.kind,
  }));
  const runs = [...result.data.runs, ...tail];
  return { ...result, data: { runs: runs.slice(0, data.RUN_LIMIT), more: history.data.runs.length >= data.RUN_LIMIT } };
}

function readers(screen, id, repo) {
  const requests = { shell };
  if (screen === "run") {
    requests.queue = runQueue;
    if (id) requests.detail = projection("run", data.run(id).detail, id);
  }
  if (screen === "board" || screen === "task") {
    requests.tasks = (signal) => versioned("/api/v1/tasks", signal, () => legacy("/api/tasks", signal), (body) => ({
      tasks: Array.isArray(body.tasks) ? [...body.tasks].sort((a, b) => b.updated.localeCompare(a.updated) || a.id.localeCompare(b.id)) : null,
    }));
    if (screen === "task" && id) {
      requests.task = projection("task", data.taskDetail(id, repo), /^t-[a-f0-9]+$/i.test(id) ? id.toLowerCase() : id);
    }
  }
  if (screen === "settings") requests.health = projection("health", "/api/health");
  return requests;
}

function publishShell(result) {
  if (result.data) {
    cache.sidebar = result.data.sidebar;
    cache.live = result.data.view;
    cache.questions = { data: result.data.asks };
    cache.at = Date.now();
  } else cache.questions = { data: cache.questions?.data, error: result.error };
  cache.errors = { sidebar: result.error ?? null, live: result.error ?? null };
  cache.failures = result.error ? cache.failures + 1 : 0;
  cache.settled = true;
  snapshot.value = snapshotOf();
}

function publishRun(key, result) {
  const run = runView.peek();
  if (key === "queue") {
    const valid = Array.isArray(result.data?.runs);
    runView.value = { ...run, settled: true, queue: valid ? result.data.runs : run.queue,
      more: valid ? Boolean(result.data.more) : run.more, queueError: valid ? null : result.error ?? malformed() };
  } else {
    const valid = result.data?.run?.id === run.id;
    runView.value = { ...run, settled: true, detail: valid ? result.data : run.detail,
      detailError: valid ? null : result.error ?? malformed() };
  }
}

function publish(key, result) {
  if (key === "shell") publishShell(result);
  else if (key === "queue" || key === "detail") publishRun(key, result);
  else {
    const previous = screenReads.peek();
    const body = result.data;
    const valid = key === "tasks" ? Array.isArray(body?.tasks) : key === "task" ? typeof body?.id === "string" : body?.serve && body?.checks;
    screenReads.value = { ...previous, [key]: { data: valid ? body : previous[key]?.data, error: valid ? null : result.error ?? malformed() } };
  }
}

async function feedLines(run, after, signal) {
  const result = await versioned(data.lineStream(run.id, after), signal, () => legacy(data.run(run.id).feed(after), signal));
  if (result.data) result.epoch = String(result.data.restore_epoch ?? result.seq?.split(".")[0] ?? "");
  return result;
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

async function readTurns(run, source, signal) {
  if (source !== "feed") {
    const result = await legacy(data.run(run.id).log, signal);
    return Array.isArray(result.data?.lines) ? { turns: logTurns(result.data.lines), turnsError: null }
      : { turnsError: result.error ?? malformed() };
  }
  let result = await feedLines(run, run.after, signal);
  const reset = result.data && run.epoch !== null && run.epoch !== result.epoch;
  const after = reset ? 0 : run.after;
  if (reset) result = await feedLines(run, 0, signal);
  if (signal.aborted) return {};
  if (!Array.isArray(result.data?.lines)) return { turnsError: result.error ?? malformed() };
  const lines = result.data.lines.filter((line) => Number.isFinite(line?.id) && line.id > after);
  run.epoch = result.epoch;
  run.after = lines.reduce((max, line) => Math.max(max, line.id), after);
  return { turns: [...(reset ? [] : runView.peek().turns), ...lines.map((line) => ({
    id: `f:${line.id}`, at: line.ts ?? "", kind: line.kind ?? "", text: String(line.text ?? ""),
  }))], turnsError: null };
}

async function readLogEvents(run, signal) {
  const result = await legacy(data.run(run.id).events(run.from), signal, true);
  if (signal.aborted) return {};
  if (result.error) return { eventsError: result.error };
  const events = new Map();
  let bad = false;
  for (const line of result.data.split("\n")) {
    if (!line.trim()) continue;
    try {
      const event = JSON.parse(line);
      if (!Number.isFinite(event?.seq)) bad = true;
      else if (event.seq > run.from) events.set(event.seq, event);
    } catch { bad = true; }
  }
  const fresh = [...events.values()].sort((a, b) => a.seq - b.seq);
  if (fresh.length) run.from = fresh.at(-1).seq;
  return { events: [...runView.peek().events, ...fresh], eventsError: bad ? { status: null, message: "Malformed events" } : null };
}

async function readTranscript(run, signal) {
  const source = runView.peek().detail?.lines_from;
  if (!source) return;
  const [turns, events] = await Promise.all([readTurns(run, source, signal), readLogEvents(run, signal)]);
  if (!signal.aborted) runView.value = { ...runView.peek(), ...turns, ...events, turnsSource: source };
}

function schedule(run) {
  if (run === active && !run.busy && run.timer == null && run.dirty.size) {
    run.timer = setTimeout(() => { run.timer = null; flush(run); }, 0);
  }
}

function invalidate(run, keys, seq) {
  for (const key of keys) {
    if ((run.reads[key] || key === "transcript" && run.reads.detail)
      && (!seq || newer(seq, run.positions.get(key)))) run.dirty.add(key);
  }
  schedule(run);
}

async function flush(run) {
  if (run !== active || run.busy || !run.dirty.size
      || run.positions.has("shell") && document.activeElement?.matches?.("form.ops-form :is(input, textarea, select, [contenteditable=true])")) return;
  run.busy = true;
  const { signal } = run.controller;
  const keys = [...run.dirty];
  run.dirty.clear();
  const read = async (key) => {
    const result = await run.reads[key](signal);
    if (signal.aborted) return;
    run.positions.set(key, result.seq);
    if (result.error || result.fallback && (key !== "task" || !run.externalTask)) run.recheck.add(key);
    else run.recheck.delete(key);
    if (key === "shell") feed?.baseline(result.seq);
    return [key, result];
  };
  try {
    const reads = keys.filter((key) => key !== "transcript");
    const first = reads.includes("shell") && !run.positions.has("shell") ? [await read("shell")] : [];
    const results = [...first, ...await Promise.all(reads.filter((key) => !first.length || key !== "shell").map(read))];
    if (signal.aborted) return;
    batch(() => { for (const result of results) if (result) publish(...result); });
    if (keys.includes("detail") || keys.includes("transcript")) await readTranscript(run, signal);
  } finally {
    run.busy = false;
    schedule(run);
  }
}

function all(run) { return [...Object.keys(run.reads), "transcript"]; }

function update(event) {
  const run = active;
  if (!run) return;
  if (event.kind === "reset") {
    data.clearSnapshots();
    run.controller.abort();
    run.controller = new AbortController();
    run.positions.clear();
    run.from = 0;
    run.after = 0;
    run.epoch = null;
    runView.value = emptyRun(runView.peek().id);
    invalidate(run, all(run));
  } else if (event.kind === "transport") {
    connected = event.connected;
  } else if (event.kind === "serve.beat") {
    const running = runView.peek().detail?.run.status === "running";
    invalidate(run, ["shell", "tasks", ...(!run.externalTask ? ["task"] : []), "health", ...(running ? ["transcript"] : [])]);
  } else if (event.kind.startsWith("run.") || event.kind.startsWith("step.")) {
    const id = event.kind.startsWith("run.") ? event.subject?.slice(4) : event.data?.run;
    invalidate(run, ["shell", "queue", ...(id === run.id ? ["detail"] : [])], event.id);
  } else if (event.kind === "turn.appended" && event.data?.run === run.id) {
    invalidate(run, ["transcript"]);
  }
}

function connect(run) {
  feed ??= openEvents(update);
  feed.select(run.screen === "run" ? run.id : "");
}

function disconnect() {
  feed?.close();
  feed = null;
  connected = false;
}

globalThis.addEventListener?.("pagehide", disconnect);
globalThis.addEventListener?.("pageshow", () => {
  if (active && !feed) {
    connect(active);
    active.positions.clear();
    invalidate(active, all(active));
  }
});

function stopReaders() {
  const run = active;
  active = null;
  if (!run) return;
  run.controller.abort();
  clearInterval(run.interval);
  clearTimeout(run.timer);
  document.removeEventListener("visibilitychange", run.visibility);
}

export function stop() {
  stopReaders();
  disconnect();
}

export function live(screen, id = "", repo = "") {
  stopReaders();
  const next = screen === "run" ? emptyRun(id) : emptyRun();
  if (screen === "run") Object.assign(next, { queue: runView.peek().queue, more: runView.peek().more });
  runView.value = next;
  const previous = screenReads.peek();
  const reads = readers(screen, id, repo);
  screenReads.value = Object.fromEntries(["tasks", "health"].filter((key) => reads[key] && previous[key]).map((key) => [key, previous[key]]));
  const run = active = { screen, id, reads, externalTask: reads.task && !/^t-[a-f0-9]+$/i.test(id),
    positions: new Map(), dirty: new Set(), recheck: new Set(),
    controller: new AbortController(), from: 0, after: 0, epoch: null, busy: false, timer: null };
  connect(run);
  const retry = () => {
    for (const key of all(run)) run.dirty.add(key);
    return flush(run);
  };
  run.visibility = () => { if (!document.hidden) retry(); };
  document.addEventListener("visibilitychange", run.visibility);
  run.interval = setInterval(() => {
    if (!document.hidden) invalidate(run, connected ? run.recheck : all(run));
  }, RETRY_MS);
  retry();
  return { snapshot, stop, retry };
}
