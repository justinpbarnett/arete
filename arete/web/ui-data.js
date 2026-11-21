import { prepareWrite, sendWrite } from "./ui-write.js";
import { webFetch } from "./ui-request.js";
import { cursor } from "./ui-snapshot.js";

const snapshots = new Map();

export function clearSnapshots() { snapshots.clear(); }

export async function snapshotRead(url, signal) {
  const saved = snapshots.get(url);
  const headers = saved?.etag ? { "If-None-Match": saved.etag } : {};
  const response = await webFetch(url, { headers, signal, cache: "no-store" });
  const seq = response.headers.get("Arete-Seq");
  if (response.status === 304 && saved) return { data: saved.data, unchanged: true };
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    return { seq, error: { status: response.status, message: body?.detail ?? body?.error ?? `HTTP ${response.status}` } };
  }
  const body = await response.json().catch(() => null);
  const position = seq ?? body?.seq;
  if (!body || typeof body !== "object" || Array.isArray(body) || !cursor(position)) {
    return { error: { status: response.status, message: "Malformed snapshot" } };
  }
  const data = { ...body, seq: position };
  const received = Date.now();
  if (!signal.aborted) {
    snapshots.delete(url);
    snapshots.set(url, { etag: response.headers.get("ETag"), data });
    if (snapshots.size > 32) snapshots.delete(snapshots.keys().next().value);
  }
  return { data, received };
}

export const screen = (view, id = "") => `/api/v1/screen?${new URLSearchParams(id ? { view, id } : { view })}`;
export const lineStream = (run, after) => `/api/v1/line-stream?${new URLSearchParams({ run, after })}`;
export const sortTasks = (tasks) => [...tasks].sort((a, b) => b.updated.localeCompare(a.updated) || a.id.localeCompare(b.id));

export async function boardTasks(read, readTasks) {
  let result = await read(screen("board"));
  const { error } = result;
  if (error?.status === 404 || error?.status === 400) {
    result = await readTasks();
  }
  const { view, tasks = view?.tasks } = result.data ?? {};
  if (!Array.isArray(tasks) || tasks.some((task) => typeof task?.updated !== "string" || typeof task.id !== "string")) {
    return { error: result.error ?? { status: null, message: "Malformed response" } };
  }
  return { ...result, data: { tasks: sortTasks(tasks) } };
}

export function visit(row) {
  return sendWrite(prepareWrite("/api/usage",{ kind: "visit", ...row }, null, { keepalive: true })).catch(() => null);
}

export const RUN_LIMIT = 500;
export const runs = (limit = RUN_LIMIT) => `/api/runs?limit=${limit}`;

export function run(id) {
  const name = encodeURIComponent(id);
  const root = `/api/runs/${name}`;
  return {
    detail: root,
    feed: (after) => `/api/feed/${name}?after=${after}`,
    log: `/api/log/${name}`,
    events: (from, follow = false) => `${root}/events?from=${from}${follow ? "&follow=1" : ""}`,
    stop: `/api/runs/${name}/stop`,
    resume: `/api/runs/${name}/resume`,
    repair: `/api/runs/${name}/repair`,
    answer: `/api/runs/${name}/answer`,
    steps: (step) => `/api/runs/${name}/steps/${encodeURIComponent(step)}`,
  };
}

export const runHash = (id) => `#run/${encodeURIComponent(id)}`;
export const taskHash = (id, repo = "") => `#task/${encodeURIComponent(id)}${repo ? `/${encodeURIComponent(repo)}` : ""}`;
export const askHash = (id) => `#triage/ask/${encodeURIComponent(id)}`;
export const taskDetail = (id, repo) => `/api/tasks/${encodeURIComponent(id)}${repo ? `?${new URLSearchParams({ repo })}` : ""}`;

export function floor() { return { title: "Floor", subtitle: "The work in flight", queue: "Running work", focus: "Overview" }; }
export function triage() { return { title: "Triage", subtitle: "Work that needs you", queue: "Queue", focus: "Focus" }; }
export function board(route) { return { title: route.active === "my-work" ? "Open work" : "Board", subtitle: "Local tasks and their progress", queue: "Tasks", focus: "Overview" }; }
export function task() { return { title: "Task", subtitle: "Work, activity and runs", queue: "Tasks", focus: "Overview" }; }
export function intake() { return { title: "Intake", subtitle: "Incoming work", queue: "Intake", focus: "Focus" }; }
export function outbox() { return { title: "Outbox", subtitle: "Drafts and deliveries waiting for review", queue: "Delivery queue", focus: "Review" }; }
export function prompts() { return { title: "Prompts", subtitle: "Prompt library", queue: "Prompts", focus: "Editor", extra: "Details" }; }
export function adws() { return { title: "ADWs", subtitle: "Agent development workflows", queue: "ADWs", focus: "Canvas", extra: "Properties" }; }
export function insights() { return { title: "Insights", subtitle: "Work over time", queue: "Charts", focus: "Chart" }; }
export function settings() { return { title: "Settings", subtitle: "Health and configuration", queue: "Sections", focus: "Health" }; }
