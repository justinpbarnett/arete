import { h, Component } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { signal } from "./vendor-signals.js";
import { openPalette } from "./ui-palette.js";
import { onStreamEvent } from "./ui-live.js";
import { webFetch } from "./ui-request.js";

const html = htm.bind(h);

function initialTopbarState() {
  return {
    andon: null,
    lines: [],
    inbox: {},
    liveRuns: {},
    serve: null,
    serveAt: 0,
    loaded: false,
  };
}

export const selectedLine = signal("");
export const topbarState = signal(initialTopbarState());

export function selectLine(id) {
  selectedLine.value = String(id ?? "");
}

export function resetTopbarState() {
  selectedLine.value = "";
  topbarState.value = initialTopbarState();
}

function duration(seconds) {
  if (!Number.isFinite(seconds)) return "";
  const value = Math.max(0, Math.floor(seconds));
  return value >= 3600 ? `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m`
    : value >= 60 ? `${Math.floor(value / 60)}m ${value % 60}s` : `${value}s`;
}

function normalizePriority(value) {
  if (value === 1 || value === "1" || value === "high" || value === "HIGH") return 1;
  if (value === 2 || value === "2" || value === "med" || value === "medium" || value === "MED") return 2;
  if (value === 3 || value === "3" || value === "low" || value === "LOW") return 3;
  return 0;
}

function shortRunId(id) {
  const text = String(id ?? "");
  const parts = text.split("-");
  return parts.length > 2 ? parts.at(-1) : text;
}

function zoneFromCounts(standing, limit, aim, prev) {
  const lim = Number.isFinite(limit) && limit > 0 ? limit : 10;
  const target = Number.isFinite(aim) && aim > 0 ? aim : 5;
  if (standing >= 2 * lim) return ["OVERLOADED", `${standing} standing, at or above twice limit ${lim}`];
  if (standing >= lim) return ["REACTIVE", `${standing} standing, limit ${lim}`];
  if (standing < target) {
    if (prev?.zone === "STABLE" && standing === prev.standing) return [prev.zone, prev.why ?? ""];
    return ["ROBUST", `${standing} standing, under aim ${target}`];
  }
  return ["STABLE", `${standing} standing, under limit ${lim}`];
}

function inboxKey(event) {
  const raw = event?.data?.id ?? event?.subject ?? "";
  return String(raw).replace(/^inbox:/, "");
}

function inboxLine(item, liveRuns) {
  if (item?.line) return String(item.line);
  const runId = item?.run_id || (typeof item?.subject === "string" && item.subject.startsWith("run:")
    ? item.subject.slice(4) : typeof item?.subject === "string" && item.subject.startsWith("step:")
      ? item.subject.slice(5).split("/")[0] : "");
  if (runId && liveRuns?.[runId]?.line) return String(liveRuns[runId].line);
  return "";
}

export function lineStatus(line, inboxMap, liveRuns) {
  const id = String(line?.id ?? "");
  const repo = String(line?.repo ?? "");
  const runs = Object.values(liveRuns ?? {}).filter((run) =>
    run && (run.line === id || (!run.line && repo && run.repo === repo)));
  const items = Object.values(inboxMap ?? {}).filter((item) => item && item.state === "open");
  const openNeed = items.find((item) => {
    const prio = normalizePriority(item.priority);
    if (prio !== 1 && prio !== 2) return false;
    const itemLine = inboxLine(item, liveRuns);
    return itemLine === id || (repo && itemLine === repo);
  });
  const stations = Array.isArray(line?.stations) ? line.stations : [];
  if (openNeed || stations.some((s) => s.state === "waiting")
      || runs.some((r) => r.status === "waiting" || r.status === "needs-info")) {
    return { status: "needs-you", cls: "ny", question: openNeed?.title ?? "" };
  }
  if (stations.some((s) => s.state === "fault")
      || runs.some((r) => r.status === "failed" || r.status === "interrupted")) {
    return { status: "fault", cls: "fault", question: "" };
  }
  if ((line?.runs_going ?? 0) > 0 || stations.some((s) => s.state === "running")
      || runs.some((r) => r.status === "running")) {
    return { status: "running", cls: "run", question: "" };
  }
  return { status: line?.paused ? "paused" : "idle", cls: "idle", question: "" };
}

function applyInboxEvent(state, event) {
  const data = event?.data ?? {};
  const id = inboxKey(event);
  if (!id) return state;
  const prevMap = state.inbox ?? {};
  const prevItem = prevMap[id];
  const nextState = event.kind === "inbox.resolved" ? "resolved"
    : String(data.state ?? (event.kind === "inbox.raised" ? "open" : prevItem?.state ?? "open"));
  const nextPrio = normalizePriority(data.priority ?? prevItem?.priority);
  const nextMap = { ...prevMap };
  if (nextState === "open") {
    nextMap[id] = {
      id,
      priority: nextPrio,
      state: "open",
      line: data.line ?? prevItem?.line ?? "",
      run_id: data.run_id ?? prevItem?.run_id ?? "",
      subject: data.subject ?? prevItem?.subject ?? "",
      title: data.title ?? prevItem?.title ?? "",
    };
  } else {
    delete nextMap[id];
  }

  const base = state.andon ?? { count: 0, high: 0, med: 0, low: 0, standing: 0, limit: 10, aim: 5, zone: "ROBUST", why: "" };
  let high = base.high ?? 0;
  let med = base.med ?? 0;
  let low = base.low ?? 0;
  const adjust = (prio, delta) => {
    if (prio === 1) high = Math.max(0, high + delta);
    else if (prio === 2) med = Math.max(0, med + delta);
    else if (prio === 3) low = Math.max(0, low + delta);
  };
  const wasOpen = prevItem?.state === "open";
  const isOpen = nextState === "open";
  if (wasOpen) adjust(normalizePriority(prevItem.priority), -1);
  if (isOpen) adjust(nextPrio, 1);
  else if (!wasOpen && event.kind === "inbox.resolved") adjust(nextPrio, -1);

  const count = high + med;
  const standing = high + med + low;
  const [zone, why] = zoneFromCounts(standing, base.limit, base.aim, base);
  return {
    ...state,
    inbox: nextMap,
    andon: { ...base, count, high, med, low, standing, zone, why },
  };
}

function applyLineEvent(state, event) {
  const id = event?.data?.id ?? (typeof event?.subject === "string" && event.subject.startsWith("line:")
    ? event.subject.slice(5) : "");
  if (!id) return state;
  const fields = event?.data?.fields;
  const patch = {};
  if (fields && typeof fields === "object") {
    for (const [key, val] of Object.entries(fields)) {
      patch[key] = val && typeof val === "object" && "after" in val ? val.after : val;
    }
  } else if (event?.data && typeof event.data === "object") {
    Object.assign(patch, event.data);
  }
  const exists = state.lines.some((line) => line.id === id);
  const lines = exists
    ? state.lines.map((line) => line.id === id ? { ...line, ...patch } : line).filter((line) => !line.archived)
    : patch.archived ? state.lines : [...state.lines, { id, name: patch.name ?? id, ...patch }];
  return { ...state, lines };
}

function applyRunEvent(state, event) {
  const row = event?.data;
  const id = row?.id ?? (typeof event?.subject === "string" && event.subject.startsWith("run:")
    ? event.subject.slice(4) : "");
  if (!id || typeof row?.status !== "string") return state;
  const nextRuns = { ...state.liveRuns };
  const active = ["running", "waiting", "needs-info", "queued"].includes(row.status);
  if (active || ["failed", "interrupted"].includes(row.status)) {
    nextRuns[id] = { ...(nextRuns[id] ?? {}), ...row, id };
  } else {
    delete nextRuns[id];
  }
  const lines = state.lines.map((line) => {
    const runningOnLine = Object.values(nextRuns).filter((r) =>
      r.status === "running" && (r.line === line.id || (!r.line && line.repo && r.repo === line.repo))).length;
    return { ...line, runs_going: runningOnLine };
  });
  return { ...state, liveRuns: nextRuns, lines };
}

export function applyTopbarEvent(state, event, now = Date.now()) {
  if (!event?.kind) return state;
  if (event.kind === "inbox.raised" || event.kind === "inbox.changed" || event.kind === "inbox.resolved") {
    return applyInboxEvent(state, event);
  }
  if (event.kind === "line.changed") return applyLineEvent(state, event);
  if (event.kind === "run.started" || event.kind === "run.changed" || event.kind === "run.ended") {
    return applyRunEvent(state, event);
  }
  if (event.kind === "serve.beat" && event.at) {
    return { ...state, serve: { ...(state.serve ?? {}), ...event }, serveAt: now };
  }
  return state;
}

export function breadcrumbs(route, page, runs) {
  const screen = route?.screen ?? "floor";
  const path = route?.path ?? [];
  const crumbs = [["Arete", "#floor"]];
  if (screen === "floor") {
    crumbs.push(["Floor", "#floor"]);
  } else if (screen === "triage") {
    crumbs.push(["Triage", "#triage"]);
    const item = path[1] || path[0];
    if (item) crumbs.push([item, route.hash ?? "#triage"]);
  } else if (screen === "run") {
    crumbs.push(["Runs", "#run"]);
    const id = runs?.id || path[0];
    if (id) {
      const ticket = runs?.detail?.run?.ticket || runs?.detail?.run?.issue || "";
      const label = ticket ? `${shortRunId(id)} · ${ticket}` : shortRunId(id);
      crumbs.push([label, route.hash ?? "#run"]);
    }
  } else if (screen === "board") {
    crumbs.push([route.active === "my-work" ? "My work" : "Board", route.hash ?? "#board"]);
  } else if (screen === "task") {
    crumbs.push(["Board", "#board"]);
    if (path[0]) crumbs.push([path[0], route.hash ?? "#board"]);
  } else if (screen === "settings") {
    crumbs.push(["Settings", "#settings"]);
    if (page?.focus) crumbs.push([page.focus, route.hash ?? "#settings"]);
  } else {
    crumbs.push([page?.title || screen, route.hash ?? `#${screen}`]);
    if (path[0]) crumbs.push([path[0], route.hash ?? `#${screen}`]);
  }
  return crumbs;
}

async function readJson(url, signal) {
  try {
    const response = await webFetch(url, { cache: "no-store", signal });
    if (!response.ok) return null;
    const body = await response.json().catch(() => null);
    return body && typeof body === "object" ? body : null;
  } catch {
    return null;
  }
}

export async function loadTopbar(signal) {
  const [shell, floor, inboxBody] = await Promise.all([
    readJson("/api/v1/screens/shell", signal),
    readJson("/api/v1/screens/floor", signal),
    readJson("/api/v1/inbox?state=open", signal),
  ]);
  if (signal?.aborted) return topbarState.peek();
  let lines = Array.isArray(floor?.lines) ? floor.lines : null;
  if (!lines) {
    const rawLines = await readJson("/api/v1/lines", signal);
    if (signal?.aborted) return topbarState.peek();
    lines = Array.isArray(rawLines?.lines) ? rawLines.lines : topbarState.peek().lines;
  }
  const prev = topbarState.peek();
  const inbox = {};
  if (Array.isArray(inboxBody?.items)) {
    for (const item of inboxBody.items) {
      if (item?.id) inbox[item.id] = item;
    }
  } else {
    Object.assign(inbox, prev.inbox);
  }
  const liveRuns = {};
  if (Array.isArray(floor?.live)) {
    for (const run of floor.live) {
      if (run?.id) liveRuns[run.id] = run;
    }
  } else {
    Object.assign(liveRuns, prev.liveRuns);
  }
  const andon = shell?.andon ?? floor?.andon ?? prev.andon;
  const next = { ...prev, andon, lines, inbox, liveRuns, loaded: true };
  topbarState.value = next;
  return next;
}

export function connectTopbar() {
  return onStreamEvent((event) => {
    if (event.kind === "reset") {
      loadTopbar();
      return;
    }
    const prev = topbarState.peek();
    const next = applyTopbarEvent(prev, event);
    if (next !== prev) topbarState.value = next;
    if (event.kind === "line.changed") {
      const id = event?.data?.id ?? (typeof event?.subject === "string" ? event.subject.replace(/^line:/, "") : "");
      if (id && !prev.lines.some((line) => line.id === id)) loadTopbar();
    }
  });
}

export class TopBar extends Component {
  componentDidMount() {
    this.controller = new AbortController();
    this.unsubscribe = connectTopbar();
    loadTopbar(this.controller.signal);
  }
  componentWillUnmount() {
    this.controller?.abort();
    this.unsubscribe?.();
  }
  render({ route, snapshot, now, runs, page }) {
    const state = topbarState.value;
    const sel = selectedLine.value;
    const onFloor = route?.screen === "floor";
    const crumbs = breadcrumbs(route, page, runs);
    const andon = state.andon;
    const zone = andon?.zone ?? "";
    const why = andon?.why ?? "";
    const count = andon?.count ?? snapshot?.needsYou ?? 0;
    const high = andon?.high ?? 0;
    const med = andon?.med ?? 0;
    const running = snapshot?.slots ?? snapshot?.running ?? Object.values(state.liveRuns).filter((r) => r.status === "running").length;
    const capacity = snapshot?.capacity ?? null;
    const serve = state.serve ?? snapshot?.serve;
    const received = state.serveAt || snapshot?.received || 0;
    const age = Number.isFinite(serve?.age_seconds)
      ? duration(serve.age_seconds + (received && Number.isFinite(now) ? Math.max(0, now - received) / 1000 : 0))
      : "";
    const lines = state.lines;
    return html`<header class="top-bar topbar" aria-label="Status bar">
      <nav class="topbar-crumbs" aria-label="Breadcrumb">${crumbs.map(([label, href], index) =>
        index === crumbs.length - 1
          ? html`<span key=${index} class="topbar-cur">${label}</span>`
          : html`<span key=${index} class="topbar-crumb"><a href=${href}>${label}</a><span class="topbar-sep" aria-hidden="true">/</span></span>`
      )}</nav>
      <div class="topbar-right">
        ${zone ? html`<span class="topbar-lbl">Zone</span><span class="topbar-zone" data-zone=${zone} title=${why}>${zone}</span>` : null}
        ${lines.length ? html`<span class="topbar-lbl">Lines</span>
          <span class="topbar-lstrip" title="One cell per line, lit when a line needs you">${lines.map((line) => {
            const info = lineStatus(line, state.inbox, state.liveRuns);
            const tip = `${line.name || line.id}: ${info.status === "needs-you" ? "needs you" : info.status}${info.question ? ` · ${info.question}` : ""}`;
            const cur = onFloor && sel === line.id ? " cur" : "";
            return html`<a key=${line.id} class=${`topbar-cell ${info.cls}${cur}`} href="#floor" data-act="strip" data-line=${line.id}
              title=${tip} aria-label=${tip} onClick=${() => selectLine(line.id)}></a>`;
          })}</span>` : null}
        ${running != null ? html`<span class="topbar-slots mono" data-slots="">● ${capacity != null ? `${running} of ${capacity} running` : `${running} running`}</span>` : null}
        ${age ? html`<span class="topbar-beat mono muted" data-beat="">beat ${age}</span>` : null}
        <button type="button" class="topbar-cmd" data-act="palette" onClick=${() => openPalette()}>Command <kbd class="topbar-kbd">⌘K</kbd></button>
        <a class=${`topbar-andon${count === 0 ? " quiet" : ""}`} href="#triage" title="Open triage" data-act="andon">◆ ${count} need you · ${high} high · ${med} med</a>
      </div>
    </header>`;
  }
}
