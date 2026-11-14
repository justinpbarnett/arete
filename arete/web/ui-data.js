import { prepareWrite, sendWrite } from "./ui-write.js";

const endpoints = { sidebar: "/api/sidebar", live: "/api/live", usage: "/api/usage" };

async function read(name) {
  try {
    const response = await fetch(endpoints[name], { cache: "no-store" });
    return response.ok ? await response.json() : null;
  } catch { return null; }
}

export async function shell() {
  const [sidebar, live] = await Promise.all([read("sidebar"), read("live")]);
  const rows = live?.live?.rows;
  return {
    needsYou: sidebar?.needs_you ?? null,
    running: sidebar?.running ?? null,
    health: sidebar?.health ?? null,
    serve: sidebar?.serve ?? null,
    slots: rows?.slots ?? null,
    capacity: rows?.max_parallel ?? null,
    received: Date.now(),
  };
}

export function visit(row) {
  return sendWrite(prepareWrite(endpoints.usage, { kind: "visit", ...row }, null, { keepalive: true })).catch(() => null);
}

export function floor() { return { title: "Floor", subtitle: "The work in flight", queue: "Lines", focus: "Focus" }; }
export function triage() { return { title: "Triage", subtitle: "Work that needs you", queue: "Queue", focus: "Focus" }; }
export function board(route) { return { title: route.active === "my-work" ? "My work" : "Board", subtitle: "Tasks and their progress", queue: "Columns", focus: "Task" }; }
export function task() { return { title: "Task", subtitle: "Task overview", queue: "Tasks", focus: "Overview", extra: "Properties", tabs: true }; }
export function run() { return { title: "Runs", subtitle: "Run overview", queue: "Runs", focus: "Overview", extra: "Waterfall", tabs: true }; }
export function intake() { return { title: "Intake", subtitle: "Incoming work", queue: "Intake", focus: "Focus" }; }
export function outbox() { return { title: "Outbox", subtitle: "Work ready to send", queue: "Outbox", focus: "Focus" }; }
export function prompts() { return { title: "Prompts", subtitle: "Prompt library", queue: "Prompts", focus: "Editor", extra: "Details" }; }
export function adws() { return { title: "ADWs", subtitle: "Agent development workflows", queue: "ADWs", focus: "Canvas", extra: "Properties" }; }
export function insights() { return { title: "Insights", subtitle: "Work over time", queue: "Charts", focus: "Chart" }; }
export function settings() { return { title: "Settings", subtitle: "Health and configuration", queue: "Sections", focus: "Settings" }; }
