import { batch, signal } from "./vendor-signals.js";
import * as data from "./ui-data.js";
import { coherent, newer } from "./ui-snapshot.js";
import { openEvents } from "./ui-events.js";
import { webFetch } from "./ui-request.js";

const TIMEOUT_MS = 3000;
const RETRY_MS = 5000;
const REPORT_GRACE_MS = 5000;
const OFFLINE_ROUNDS = 2;
const STALE_SECONDS = 60;
const cache = { sidebar: null, live: null, questions: null, sidebarAt: 0, errors: {}, failures: 0, settled: false };
export const screenReads = signal({});
let active;
let feed;
let transport = null;

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
    questions: cache.questions ?? null,
    running: sidebar?.running ?? null,
    health: sidebar?.health ?? null,
    serve: sidebar?.serve ?? null,
    slots: rows?.slots ?? null,
    capacity: rows?.max_parallel ?? null,
    received: cache.sidebarAt,
    live: payload,
    asks: payload?.needs_you ?? null,
    runs: rows?.runs ?? null,
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
  const resourceError = Object.values(screenReads.value).find((part) => part.error)?.error
    ?? (["triage", "run"].includes(screen) ? current.questions?.error : null);
  return resourceError ?? (panel?.error ? { status: null, message: panel.error } : current.fault);
}


async function read(url, { text = false, versioned = false }, signal) {
  const controller = new AbortController();
  if (signal.aborted) return {};
  const abort = () => controller.abort();
  const expiry = setTimeout(abort, TIMEOUT_MS);
  signal.addEventListener("abort", abort, { once: true });
  try {
    if (versioned) {
      const result = await data.snapshotRead(url, controller.signal);
      return { ...result, seq: result.data?.seq ?? result.seq };
    }
    const response = await webFetch(url, { cache: "no-store", signal: controller.signal });
    if (response.ok && text) return { data: await response.text() };
    const body = await response.json().catch(() => null);
    const seq = response.headers.get("Arete-Seq") ?? body?.seq;
    if (!response.ok) return { seq, error: { status: response.status, message: body?.error ?? `HTTP ${response.status}` } };
    if (body && typeof body === "object" && !Array.isArray(body)) return { data: body, seq };
    return { error: { status: response.status, message: "Malformed response" } };
  } catch (error) {
    return { error: { status: null, message: error?.name === "AbortError" ? "Request timed out" : "Network error" } };
  } finally {
    clearTimeout(expiry);
    signal.removeEventListener("abort", abort);
  }
}


async function versioned(url, signal, fallback, select = (body) => body) {
  const result = await read(url, { versioned: true }, signal);
  if (result.error?.status === 404) return fallback();
  if (result.data) return { ...result, data: select(result.data) };
  return result;
}

const legacy = (url, signal, text = false) => read(url, { text }, signal);
const malformed = () => ({ status: null, message: "Malformed response" });

async function shell(signal, allowLegacy = () => true) {
  const result = await versioned(data.screen("live"), signal, async () => {
    if (!allowLegacy()) return null;
    const latest = {};
    const readers = Object.entries({ sidebar: "/api/sidebar", view: "/api/live", asks: "/api/asks" })
      .map(([key, url]) => async () => (latest[key] = await legacy(url, signal)));
    const group = await coherent(readers, (result) => result.seq ? [result.seq] : []);
    const { sidebar, view, asks } = latest;
    const error = sidebar.error ?? view.error ?? asks.error;
    if (error) return { error };
    if (!group && [sidebar, view, asks].every((result) => result.seq)) return { error: { message: "Dashboard is changing. Retrying." } };
    return { data: { sidebar: sidebar.data, view: view.data, asks: asks.data }, seq: sidebar.seq };
  });
  if (!result) return null;
  const body = result.data;
  return result.error || Number.isInteger(body?.sidebar?.running) && body.view?.live && Array.isArray(body.asks?.asks)
    ? result : { error: malformed() };
}

const projection = (view, path, id) => (signal) => versioned(data.screen(view, id), signal, () => legacy(path, signal), (body) => body.view);
const ticket = (run) => run.issue
  ? `${run.repo ?? ""}${/^(?:[A-Z][A-Z0-9_]+-\d+|t-[a-f0-9]+)$/.test(run.issue) ? " " : "#"}${run.issue}`
  : run.loop || run.repo || run.kind;

async function runQueue(signal) {
  const result = await projection("runs", data.runs())(signal);
  if (!result.data?.more || !Array.isArray(result.data.runs) || result.data.runs.length >= data.RUN_LIMIT) return result;
  const history = await versioned("/api/v1/runs", signal, () => legacy(data.runs(), signal));
  if (!Array.isArray(history.data?.runs)) return { error: history.error ?? malformed() };
  const restored = result.seq && history.seq && result.seq.split(".")[0] !== history.seq.split(".")[0];
  const head = restored ? [] : result.data.runs;
  const current = new Set(head.map((run) => run.id));
  const tail = history.data.runs.filter((run) => !current.has(run.id)).map((run) => ({ ...run, ticket: ticket(run) }));
  const runs = [...head, ...tail];
  return { seq: restored ? history.seq : result.seq, data: { runs: runs.slice(0, data.RUN_LIMIT),
    more: Boolean(history.data.more) || runs.length > data.RUN_LIMIT || history.data.runs.length >= data.RUN_LIMIT } };
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
    cache.sidebarAt = Date.now();
  } else cache.questions = { data: cache.questions?.data, error: result.error };
  cache.errors = { sidebar: result.error ?? null, live: result.error ?? null };
  cache.failures = result.error ? cache.failures + 1 : 0;
  cache.settled = true;
  snapshot.value = snapshotOf();
}

function publishShellRefresh(run, result) {
  const polling = transport === "poll";
  if (!result?.data || (result.unchanged && !polling) || newer(run.positions.get("shell"), result.seq)) return;
  const { sidebar, view, asks } = result.data;
  const rows = cache.live.live.rows;
  const projected = view.live.rows;
  const byId = new Map(projected?.runs?.map((row) => [row.id, row]));
  const runs = rows?.runs?.map((row) => {
    const current = byId.get(row.id);
    return current ? { ...row, slot: current.slot, paid: current.paid, seconds: current.seconds } : row;
  }) ?? (result.seq === run.positions.get("shell") ? projected?.runs : undefined);
  cache.sidebar = { ...sidebar, running: cache.sidebar.running, serve: polling ? sidebar.serve : cache.sidebar.serve };
  if (polling) cache.sidebarAt = Date.now();
  cache.live = { ...view, live: { ...view.live, rows: rows || projected ? { ...rows, ...projected, runs } : undefined } };
  cache.questions = { data: asks };
  snapshot.value = snapshotOf();
}

function publishRun(key, { data, error }, usageOnly) {
  if (key === "queue") {
    if (Array.isArray(data?.runs)) { rs.queue = data.runs; rs.more = Boolean(data.more); rs.queueError = null; }
    else rs.queueError = error ?? malformed();
  } else if (data?.run?.id === rs.id) {
    rs.detail = usageOnly ? { ...rs.detail, models: data.models, run: { ...rs.detail.run, paid: data.run.paid } } : data;
    rs.detailError = null;
  } else rs.detailError = error ?? malformed();
  rs.settled = true;
}

function publish(key, result, usageOnly) {
  if (key === "shell") publishShell(result);
  else if (key === "queue" || key === "detail") publishRun(key, result, usageOnly);
  else {
    const previous = screenReads.peek();
    const body = result.data;
    const valid = key === "tasks" ? Array.isArray(body?.tasks) : key === "task" ? typeof body?.id === "string" : body?.serve && body?.checks;
    screenReads.value = { ...previous, [key]: { data: valid ? body : previous[key]?.data, error: valid ? null : result.error ?? malformed() } };
  }
}

const blankRuns = () => ({ queue: null, more: false, queueError: null, id: "", detail: null, detailError: null, events: [],
  seqs: new Set(), seq: 0, steps: new Map(), eventsError: null, turns: [], turnsAfter: 0, turnsEpoch: null, turnsSource: "", turnsError: null, settled: false });
const rs = blankRuns();

const viewOf = () => ({ queue: rs.queue, more: rs.more, queueError: rs.queueError, id: rs.id, detail: rs.detail, detailError: rs.detailError,
  events: [...rs.events, ...rs.steps.values()], eventsError: rs.eventsError, turns: rs.turns, turnsSource: rs.turnsSource, turnsError: rs.turnsError, settled: rs.settled });

export const runView = signal(viewOf());
const publishRuns = () => { runView.value = viewOf(); };

function selectRun(id) {
  const queue = { queue: rs.queue, more: rs.more, queueError: rs.queueError, settled: rs.settled };
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
    for (const event of fresh) rs.steps.delete(stepKey(event));
  }
  rs.eventsError = bad ? { status: null, message: "Malformed events" } : null;
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

async function readTurns(signal) {
  const id = rs.id;
  const source = rs.detail.lines_from;
  const foundLines = (after) => source === "feed"
    ? versioned(data.lineStream(id, after), signal, () => legacy(data.run(id).feed(after), signal))
    : legacy(data.run(id).log, signal);
  const epoch = (found) => String(found.data?.restore_epoch ?? found.seq?.split(".")[0] ?? "");
  let found = await foundLines(rs.turnsAfter);
  if (signal.aborted) return;
  if (source === "feed" && found.data && rs.turnsEpoch != null && epoch(found) !== rs.turnsEpoch) {
    rs.turns = [];
    rs.turnsAfter = 0;
    rs.turnsEpoch = epoch(found);
    found = await foundLines(0);
    if (signal.aborted) return;
  }
  const lines = found.data?.lines;
  if (!Array.isArray(lines)) {
    rs.turnsError = found.error ?? { status: null, message: "Malformed response" };
    return;
  }
  rs.turnsError = null;
  rs.turnsSource = source;
  if (source === "feed") {
    rs.turnsEpoch = epoch(found);
    const fresh = lines.filter((line) => line && Number.isFinite(line.id) && line.id > rs.turnsAfter);
    rs.turns = [...rs.turns, ...fresh.map((line) => ({ id: `f:${line.id}`, at: line.ts ?? "", kind: line.kind ?? "", text: String(line.text ?? "") }))];
    rs.turnsAfter = Math.max(rs.turnsAfter, ...fresh.map((line) => line.id));
  } else rs.turns = logTurns(lines);
}

const stepKey = (event) => `${event.event}:${event.step}/${event.try ?? 1}`;

async function readTranscript(run, signal) {
  if (!rs.detail?.lines_from) return;
  clearTimeout(run.reportTimer);
  const [, events] = await Promise.all([readTurns(signal), legacy(data.run(rs.id).events(rs.seq), signal, true)]);
  if (signal.aborted) return;
  if (events.error) rs.eventsError = events.error;
  else takeEvents(events.data);
  publishRuns();
  if (!rs.events.some((event) => event.event === "report") && Date.now() < run.reportUntil) {
    run.reportTimer = setTimeout(() => invalidate(run, ["transcript"]), 1000);
  }
}

function schedule(run) {
  if (run === active && !run.busy && run.timer == null && run.dirty.size) {
    run.timer = setTimeout(() => { run.timer = null; flush(run); }, 0);
  }
}

function invalidate(run, keys, refreshOnly = false) {
  for (const key of keys) {
    if ((run.reads[key] || key === "transcript" && run.reads.detail) && (!refreshOnly || !run.dirty.has(key))) {
      run.dirty.set(key, refreshOnly);
    }
  }
  schedule(run);
}

async function flush(run) {
  if (run !== active || run.busy || !run.dirty.size
      || run.positions.has("shell") && document.activeElement?.matches?.("form.ops-form :is(input, textarea, select, [contenteditable=true])")) return;
  run.busy = true;
  const { signal } = run.controller;
  const keys = [...run.dirty.keys()];
  const periodicShell = run.dirty.get("shell");
  const connectedAtStart = Boolean(transport);
  const missingDetail = keys.includes("detail") && !rs.detail;
  const refreshShellOnly = periodicShell && connectedAtStart && !run.incoherent && run.positions.has("shell") && !missingDetail;
  const refreshDetailOnly = run.dirty.get("detail") && connectedAtStart && !run.incoherent && !missingDetail;
  run.dirty.clear();
  const latest = new Map();
  const readShell = () => shell(signal, () => !periodicShell || !(connectedAtStart || transport));
  const read = async (key) => {
    const result = key === "shell" ? await readShell() : await run.reads[key](signal);
    if (!result) return null;
    latest.set(key, result);
    return result.data && result.seq ? result : null;
  };
  const grouped = keys.filter((key) => key === "shell" ? !refreshShellOnly
    : key === "queue" || key === "detail" && !refreshDetailOnly);
  const fullDetail = grouped.includes("detail");
  const behind = (key, seq) => newer(run.positions.get(key), seq) || key === "detail"
    && (newer(run.positions.get("usage"), seq) || seq && newer(run.usageNeeded, seq));
  run.duringRead = grouped.length ? [] : null;
  try {
    const [group, shellRefresh] = await Promise.all([
      grouped.length && coherent(grouped.map((key) => () => read(key)), (result) => result ? [result.seq] : []),
      refreshShellOnly && readShell(),
      ...keys.filter((key) => run.reads[key] && key !== "shell" && !grouped.includes(key)).map(read),
    ]);
    if (signal.aborted) return;
    const missingShell = grouped.includes("shell") && !latest.has("shell") && !(missingDetail && run.positions.has("shell"));
    const mismatch = grouped.some((key) => {
      const result = latest.get(key);
      return result?.data && behind(key, result.seq);
    }) || !group && (grouped.filter((key) => latest.get(key)?.data && latest.get(key)?.seq).length > 1 || missingShell);
    if (grouped.includes("shell")) run.incoherent = Boolean(mismatch || latest.get("shell")?.error);
    if (latest.has("queue")) run.queueError = latest.get("queue").error;
    batch(() => {
      if (refreshShellOnly) publishShellRefresh(run, shellRefresh);
      for (const [key, result] of latest) {
        const usageOnly = key === "detail" && refreshDetailOnly;
        if (grouped.includes(key) && result.data && (mismatch || key === "detail" && latest.get("shell")?.error)) continue;
        if (usageOnly && result.data && behind(key, result.seq)) continue;
        if (result.data && !usageOnly) run.positions.set(key, result.seq);
        if (key === "detail" && result.data?.run?.id === rs.id) run.positions.set("usage", result.seq ?? run.usageNeeded);
        if (key === "shell" && result.data) run.states.clear();
        publish(key, result, usageOnly);
      }
      for (const event of run.duringRead ?? []) applyEvent(run, event);
      if (grouped.length || refreshDetailOnly) publishRuns();
    });
    run.duringRead = null;
    feed?.baseline(latest.get("shell")?.seq ?? (fullDetail ? latest.get("detail")?.seq : null) ?? latest.get("queue")?.seq);
    const detail = rs.detail?.run;
    if (fullDetail && detail?.kind === "pipeline" && detail.status !== "running") {
      run.reportUntil = Math.max(run.reportUntil, Date.parse(detail.finished) + REPORT_GRACE_MS || 0);
    }
    if (keys.includes("transcript") || fullDetail && detail && !mismatch) await readTranscript(run, signal);
  } finally {
    run.duringRead = null;
    run.busy = false;
    schedule(run);
  }
}

function all(run) { return [...Object.keys(run.reads), "transcript"]; }

function floorRuns(runs) {
  const panel = cache.live.live;
  cache.live = { ...cache.live, live: { ...panel, rows: { ...panel.rows, runs } } };
}

function refreshUsage(run, seq) {
  if (!newer(seq, run.usageNeeded) || !newer(seq, run.positions.get("usage"))) return;
  run.usageNeeded = seq;
  if (rs.detail) invalidate(run, ["detail"], true);
}

function applyRun(run, event) {
  const id = event.subject?.match(/^run:(.+)$/)?.[1];
  const row = event.data;
  if (!id || row?.id !== id || typeof row.status !== "string") return;
  const fresh = (key) => run.positions.has(key) && newer(event.id, run.positions.get(key));
  const before = (rows) => rows?.find((item) => item.id === id);
  const replace = (rows, patch) => before(rows) ? rows.map((item) => item.id === id ? { ...item, ...patch } : item) : [patch, ...rows];
  const running = row.status === "running";
  if (fresh("shell")) {
    const rows = cache.live?.live?.rows;
    const was = run.states.get(id) ?? (rows?.runs ? Boolean(before(rows.runs)) :
      before(rs.queue)?.status === "running" || rs.detail?.run.id === id && rs.detail.run.status === "running"
        || event.kind === "run.ended");
    cache.sidebar = { ...cache.sidebar, running: Math.max(0, cache.sidebar.running + Number(running) - Number(was)) };
    if (!rows?.runs) run.states.set(id, running);
    if (rows?.runs) floorRuns(running ? replace(rows.runs, { ...row, ticket: ticket(row), stopping: Boolean(row.stop_asked) })
      : rows.runs.filter((item) => item.id !== id));
    run.positions.set("shell", event.id);
    snapshot.value = snapshotOf();
  }
  if (fresh("queue") && rs.queue) {
    const previous = before(rs.queue);
    if ((!previous?.version || row.version > previous.version) && (previous || event.kind === "run.started" || running)) {
      const rows = replace(rs.queue, { ...row, ticket: ticket(row), seconds: null });
      rs.queue = rows.slice(0, data.RUN_LIMIT);
      rs.more ||= rows.length > data.RUN_LIMIT;
    }
    run.positions.set("queue", event.id);
  }
  if (fresh("detail") && rs.detail?.run.id === id) {
    const current = rs.detail.run;
    rs.detail = { ...rs.detail, run: { ...current, ...row, ticket: ticket(row) } };
    run.positions.set("detail", event.id);
    if (current.status === "running" && !running) {
      if (row.kind === "pipeline") run.reportUntil = Date.now() + REPORT_GRACE_MS;
      invalidate(run, ["transcript"]);
    }
  }
  if (id === rs.id) refreshUsage(run, event.id);
}

function applyStep(run, event) {
  const step = event.data;
  if (!step?.run || event.subject !== `step:${step.run}/${step.step}/${step.try}`) return;
  const rows = cache.live?.live?.rows;
  if (run.positions.has("shell") && newer(event.id, run.positions.get("shell")) && rows?.runs?.some((row) => row.id === step.run)) {
    floorRuns(rows.runs.map((row) => row.id === step.run ? { ...row, step: step.step } : row));
    run.positions.set("shell", event.id);
    snapshot.value = snapshotOf();
  }
  if (step.run !== rs.id) return;
  const line = { ...step, event: event.kind === "step.started" ? "step_start" : "step_end", seq: event.id };
  const key = stepKey(line);
  if (!rs.events.some((row) => stepKey(row) === key)) rs.steps.set(key, line);
  if (event.kind === "step.ended") refreshUsage(run, event.id);
}

function applyEvent(run, event) {
  if (event.kind.startsWith("run.")) applyRun(run, event);
  else applyStep(run, event);
}

function update(event) {
  const run = active;
  if (!run) return;
  if (event.kind === "reset") {
    data.clearSnapshots();
    run.controller.abort();
    run.controller = new AbortController();
    run.positions.clear();
    run.usageNeeded = null;
    run.reportUntil = 0;
    clearTimeout(run.reportTimer);
    Object.assign(rs, blankRuns(), { id: rs.id });
    publishRuns();
    invalidate(run, all(run));
  } else if (event.kind === "transport") {
    transport = event.transport;
  } else if (event.kind === "serve.beat") {
    if (event.at && cache.sidebar) {
      cache.sidebar = { ...cache.sidebar, serve: event };
      cache.sidebarAt = Date.now();
      snapshot.value = snapshotOf();
    }
    if (rs.detail?.run.status === "running") invalidate(run, ["transcript"]);
  } else if (event.kind.startsWith("run.") || event.kind.startsWith("step.")) {
    run.duringRead?.push(event);
    batch(() => { applyEvent(run, event); publishRuns(); });
  } else if (event.kind === "turn.appended" && event.data?.run === run.id) invalidate(run, ["transcript"]);
}

function connect(run) {
  feed ??= openEvents(update);
  feed.select(run.screen === "run" ? run.id : "");
}

function disconnect() {
  feed?.close();
  feed = null;
  transport = null;
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
  clearTimeout(run.reportTimer);
  document.removeEventListener("visibilitychange", run.visibility);
}

export function stop() {
  stopReaders();
  disconnect();
}

export function live(screen, id = "", repo = "") {
  stopReaders();
  selectRun(screen === "run" ? id : "");
  const previous = screenReads.peek();
  const reads = readers(screen, id, repo);
  screenReads.value = Object.fromEntries(["tasks", "health"].filter((key) => reads[key] && previous[key]).map((key) => [key, previous[key]]));
  const run = active = { screen, id, reads, externalTask: reads.task && !/^t-[a-f0-9]+$/i.test(id),
    positions: new Map(), dirty: new Map(), states: new Map(), reportUntil: 0,
    controller: new AbortController(), busy: false, timer: null };
  connect(run);
  const retry = () => {
    for (const key of all(run)) run.dirty.set(key, false);
    return flush(run);
  };
  const refresh = (transcript = false) => {
    if (document.hidden) return;
    const keys = ["shell", "tasks", "health"];
    const needsDetail = rs.detailError || newer(run.usageNeeded, run.positions.get("usage"));
    if (!run.externalTask || !screenReads.peek().task?.data) keys.push("task");
    if (run.queueError || needsDetail && !rs.detail) keys.push("queue");
    if (needsDetail) keys.push("detail");
    if (transcript && rs.detail?.run.status === "running") keys.push("transcript");
    invalidate(run, transport && !run.incoherent ? keys : all(run), true);
  };
  run.visibility = () => refresh(true);
  document.addEventListener("visibilitychange", run.visibility);
  run.interval = setInterval(refresh, RETRY_MS);
  retry();
  return { snapshot, stop, retry };
}
