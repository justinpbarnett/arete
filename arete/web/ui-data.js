import { prepareWrite, sendWrite } from "./ui-write.js";

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
