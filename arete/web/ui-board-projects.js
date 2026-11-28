import { Component, h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { signal } from "./vendor-signals.js";
import { projects as projectRoutes } from "./ui-data.js";
import { webFetch } from "./ui-request.js";
import { onLiveEvent } from "./ui-live.js";
import { boardTask } from "./ui-events.js";
import { onMyWork, selectItem, syncList } from "./ui-keys.js";
import { Empty, Fault, Loading } from "./ui-states.js";

const html = htm.bind(h);

export const PROJECTS_URL = "/api/v1/projects";
export const VIEWS_URL = "/api/v1/views";
export const BOARD_SCREEN_URL = "/api/v1/screens/board";

export const STATES = ["intake", "ready", "running", "needs-you", "review", "done"];
export const STATE_LABELS = {
  intake: "○ Intake",
  ready: "○ Ready",
  running: "● Running",
  "needs-you": "◆ Needs you",
  review: "● Review",
  done: "✓ Done",
};
export const STATE_NAMES = {
  intake: "Intake",
  ready: "Ready",
  running: "Running",
  "needs-you": "Needs you",
  review: "Review",
  done: "Done",
};

const LEGACY_STATE_MAP = {
  todo: "ready",
  running: "running",
  pr: "review",
  "needs-info": "needs-you",
  failed: "needs-you",
  done: "done",
  closed: "done",
};

export function canonicalState(task) {
  const raw = String(task?.state ?? "").trim().toLowerCase();
  if (STATES.includes(raw)) return raw;
  return LEGACY_STATE_MAP[raw] ?? "ready";
}

export function isFaultTask(task) {
  return Boolean(task?.fault) || String(task?.state ?? "").toLowerCase() === "failed";
}

export function isOpenTask(task) {
  return canonicalState(task) !== "done";
}

export function taskSyncState(task) {
  const sync = task?.primary_ref?.sync ?? task?.sync?.state ?? "local";
  return String(sync || "local").toLowerCase();
}

export function isUnsyncedTask(task) {
  const sync = taskSyncState(task);
  return sync === "ahead" || sync === "behind" || sync === "conflict";
}

export function taskNeedsYou(task) {
  if (Number(task?.needs_you) > 0) return true;
  if (Array.isArray(task?.needs_you_ids) && task.needs_you_ids.length > 0) return true;
  if (task?.needsYou) return true;
  return canonicalState(task) === "needs-you";
}

export function isHumanOwner(owner) {
  const raw = String(owner ?? "").trim();
  if (!raw) return false;
  if (raw.startsWith("u:") || raw === "owner") return true;
  return !raw.startsWith("agent") && !raw.startsWith("a:");
}

export function ownerLabel(owner) {
  const raw = String(owner ?? "").trim();
  if (!raw) return "unassigned";
  if (raw === "owner") return "You";
  if (raw.startsWith("u:")) return raw.slice(2) || "You";
  return raw;
}

export const projectsState = signal({ items: [], loaded: false, error: null });
export const viewsState = signal({ items: [], loaded: false, error: null });
export const allBoardState = signal({ tasks: [], lines: [], columns: [], runningIds: [], andon: null, loaded: false, error: null });
export const inboxState = signal({ items: [], loaded: false, error: null });
export const mineOnOpenWork = signal(false);
export const selectedProjectId = signal("");
export const boardLiveRevision = signal(0);

let sidebarStarted = false;
let liveHooked = false;
let refreshTimer = null;
const mineCallbacks = new Set();

export function onActivateMyWork(fn) {
  mineCallbacks.add(fn);
  return () => mineCallbacks.delete(fn);
}

export function activateMyWork() {
  mineOnOpenWork.value = true;
  for (const fn of mineCallbacks) fn();
}

export function isMyWorkSelected(route) {
  return route?.screen === "board" && route?.path?.[0] === "open-work" && mineOnOpenWork.value;
}

function tokenizeFilter(text) {
  const tokens = [];
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (/\s/.test(ch)) { i += 1; continue; }
    if (ch === "=") { tokens.push(["EQ", "="]); i += 1; continue; }
    if (ch === "!" && text[i + 1] === "=") { tokens.push(["NEQ", "!="]); i += 2; continue; }
    if (ch === "(") { tokens.push(["LP", "("]); i += 1; continue; }
    if (ch === ")") { tokens.push(["RP", ")"]); i += 1; continue; }
    if (ch === ",") { tokens.push(["COMMA", ","]); i += 1; continue; }
    if (ch === '"') {
      let buf = "";
      i += 1;
      while (i < text.length && text[i] !== '"') {
        if (text[i] === "\\" && i + 1 < text.length) { buf += text[i + 1]; i += 2; }
        else { buf += text[i]; i += 1; }
      }
      if (text[i] === '"') i += 1;
      tokens.push(["STR", buf]);
      continue;
    }
    const match = /^[A-Za-z0-9_.:-]+/.exec(text.slice(i));
    if (match) {
      tokens.push(["WORD", match[0]]);
      i += match[0].length;
    } else {
      i += 1;
    }
  }
  return tokens;
}

function taskFieldValue(task, field) {
  switch (field) {
    case "state": return canonicalState(task);
    case "kind": return String(task?.kind ?? "");
    case "priority": return task?.priority == null ? "" : String(task.priority);
    case "fault": return isFaultTask(task) ? "1" : "0";
    case "project": return String(task?.project ?? "");
    case "line": return String(task?.line ?? "");
    case "repo": return String(task?.repo ?? "");
    case "owner": return String(task?.owner ?? "");
    case "delegate": return String(task?.delegate ?? "");
    default: return "";
  }
}

export function evaluateViewFilter(filterText, task) {
  const trimmed = String(filterText ?? "").trim();
  if (!trimmed) return true;
  const tokens = tokenizeFilter(trimmed);
  let pos = 0;
  const peek = () => tokens[pos] ?? null;
  const next = () => tokens[pos++] ?? null;

  function parseOr() {
    let left = parseAnd();
    while (peek()?.[0] === "WORD" && peek()[1].toLowerCase() === "or") {
      next();
      const right = parseAnd();
      left = left || right;
    }
    return left;
  }

  function parseAnd() {
    let left = parseNot();
    while (peek()?.[0] === "WORD" && peek()[1].toLowerCase() === "and") {
      next();
      const right = parseNot();
      left = left && right;
    }
    return left;
  }

  function parseNot() {
    if (peek()?.[0] === "WORD" && peek()[1].toLowerCase() === "not") {
      next();
      return !parseNot();
    }
    return parseAtom();
  }

  function parseAtom() {
    if (peek()?.[0] === "LP") {
      next();
      const val = parseOr();
      if (peek()?.[0] === "RP") next();
      return val;
    }
    const fieldTok = next();
    if (!fieldTok) return true;
    const field = fieldTok[1].toLowerCase();
    const opTok = peek();
    if (!opTok) return true;
    if (opTok[0] === "EQ" || opTok[0] === "NEQ") {
      next();
      const valTok = next();
      const expected = String(valTok?.[1] ?? "");
      const actual = taskFieldValue(task, field);
      const equal = field === "state"
        ? actual === expected.toLowerCase() || String(task?.state ?? "").toLowerCase() === expected.toLowerCase()
        : actual.toLowerCase() === expected.toLowerCase();
      return opTok[0] === "EQ" ? equal : !equal;
    }
    if (opTok[0] === "WORD" && opTok[1].toLowerCase() === "in") {
      next();
      const values = [];
      if (peek()?.[0] === "LP") {
        next();
        while (peek() && peek()[0] !== "RP") {
          const item = next();
          if (item[0] !== "COMMA") values.push(String(item[1]).toLowerCase());
        }
        if (peek()?.[0] === "RP") next();
      }
      const actual = taskFieldValue(task, field).toLowerCase();
      return values.includes(actual);
    }
    return true;
  }

  try {
    return Boolean(parseOr());
  } catch {
    return true;
  }
}

export function taskBelongsToProject(task, project) {
  if (!project) return false;
  if (task?.project) return task.project === project.id;
  return Boolean(project.repo && task?.repo === project.repo);
}

export function projectStateCounts(tasks, project) {
  const counts = Object.fromEntries(STATES.map((s) => [s, 0]));
  for (const task of tasks) {
    if (!taskBelongsToProject(task, project)) continue;
    const st = canonicalState(task);
    counts[st] = (counts[st] ?? 0) + 1;
  }
  counts.open = STATES.filter((s) => s !== "done").reduce((sum, s) => sum + counts[s], 0);
  counts.total = counts.open + counts.done;
  return counts;
}

export function progressSegments(counts) {
  const entries = STATES.map((state) => ({ state, count: Number(counts?.[state] ?? 0) })).filter((item) => item.count > 0);
  const total = entries.reduce((sum, item) => sum + item.count, 0);
  if (!total) return [];
  const raw = entries.map((item) => ({ ...item, exact: (item.count / total) * 100 }));
  const floored = raw.map((item) => ({ ...item, pct: Math.max(1, Math.floor(item.exact)) }));
  let used = floored.reduce((sum, item) => sum + item.pct, 0);
  if (used > 100) {
    floored.sort((a, b) => b.pct - a.pct);
    for (let i = 0; used > 100 && i < floored.length; i += 1) {
      const trim = Math.min(used - 100, Math.max(0, floored[i].pct - 1));
      floored[i].pct -= trim;
      used -= trim;
    }
  } else if (used < 100) {
    const byFrac = [...floored].sort((a, b) => (b.exact - Math.floor(b.exact)) - (a.exact - Math.floor(a.exact)));
    for (let i = 0; used < 100 && i < byFrac.length; i += 1) {
      byFrac[i].pct += 1;
      used += 1;
    }
  }
  const byState = new Map(floored.map((item) => [item.state, item]));
  return entries.map(({ state, count }) => {
    const pct = Math.max(1, Math.min(100, byState.get(state).pct));
    return { state, count, pct, pctClass: `pct-w-${pct}` };
  });
}

export function viewMatchesTask(view, task) {
  if (!view) return true;
  if (view.project && task?.project !== view.project && task?.repo !== view.project) return false;
  if (view.filter) return evaluateViewFilter(view.filter, task);
  if (view.id === "open-work") return isOpenTask(task);
  if (view.id === "needs-you") return taskNeedsYou(task);
  if (view.id === "unsynced") return isUnsyncedTask(task);
  return true;
}

export function viewTaskCount(tasks, view) {
  return tasks.filter((task) => viewMatchesTask(view, task)).length;
}

async function fetchJson(url) {
  try {
    const res = await webFetch(url, { cache: "no-store" });
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      return { error: { status: res.status, message: body?.detail ?? body?.error ?? `HTTP ${res.status}` } };
    }
    return { data: body };
  } catch (err) {
    return { error: { status: null, message: err?.message ?? "Network error" } };
  }
}

export async function refreshProjectsAndViews() {
  const routes = projectRoutes();
  const [projRes, viewRes] = await Promise.all([
    fetchJson(routes.list || PROJECTS_URL),
    fetchJson(routes.views || VIEWS_URL),
  ]);
  if (Array.isArray(projRes.data?.projects)) {
    projectsState.value = { items: projRes.data.projects, loaded: true, error: null };
  } else if (projRes.error) {
    projectsState.value = { ...projectsState.peek(), loaded: true, error: projRes.error };
  }
  if (Array.isArray(viewRes.data?.views)) {
    viewsState.value = { items: viewRes.data.views, loaded: true, error: null };
  } else if (viewRes.error) {
    viewsState.value = { ...viewsState.peek(), loaded: true, error: viewRes.error };
  }
}

export async function refreshAllBoard() {
  const routes = projectRoutes();
  const res = await fetchJson(routes.board || BOARD_SCREEN_URL);
  if (Array.isArray(res.data?.tasks)) {
    allBoardState.value = {
      tasks: res.data.tasks,
      lines: Array.isArray(res.data.lines) ? res.data.lines : [],
      columns: Array.isArray(res.data.columns) ? res.data.columns : [],
      runningIds: Array.isArray(res.data.running_ids) ? res.data.running_ids : [],
      andon: res.data.andon ?? null,
      loaded: true,
      error: null,
    };
    boardLiveRevision.value += 1;
  } else if (res.error) {
    allBoardState.value = { ...allBoardState.peek(), loaded: true, error: res.error };
  }
}

export async function refreshInbox() {
  const res = await fetchJson("/api/v1/inbox?state=open");
  if (Array.isArray(res.data?.items)) {
    inboxState.value = { items: res.data.items, loaded: true, error: null };
  } else if (res.error) {
    inboxState.value = { ...inboxState.peek(), loaded: true, error: res.error };
  }
}

function scheduleBackgroundRefresh(includeMeta = false) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    refreshAllBoard();
    refreshInbox();
    if (includeMeta) refreshProjectsAndViews();
  }, 150);
}

export function applyTaskPatchToAllBoard(patch) {
  if (!patch?.id) return;
  const cur = allBoardState.peek();
  if (!cur.loaded) return;
  const existing = cur.tasks.find((item) => item.id === patch.id);
  if (existing && patch.version != null && existing.version > patch.version) return;
  const nextTasks = existing
    ? cur.tasks.map((item) => (item.id === patch.id ? { ...item, ...patch } : item))
    : [patch, ...cur.tasks];
  allBoardState.value = { ...cur, tasks: nextTasks };
  boardLiveRevision.value += 1;
}

export function startBoardSidebarSync() {
  if (typeof globalThis.fetch !== "function" || typeof globalThis.document === "undefined") return;
  if (!liveHooked) {
    liveHooked = true;
    onMyWork(() => activateMyWork());
    onLiveEvent((event) => {
      if (!event?.kind) return;
      if (event.kind === "reset") {
        scheduleBackgroundRefresh(true);
        return;
      }
      if (event.kind.startsWith("task.") || event.kind.startsWith("run.")) {
        const patch = boardTask(event);
        if (patch) applyTaskPatchToAllBoard(patch);
        scheduleBackgroundRefresh(false);
        return;
      }
      if (event.kind === "project.changed" || event.kind === "line.changed" || event.kind === "view.changed") {
        scheduleBackgroundRefresh(true);
        return;
      }
      if (event.kind.startsWith("inbox.") || event.kind === "ref.changed" || event.kind === "principal.changed") {
        scheduleBackgroundRefresh(false);
      }
    });
  }
  if (!sidebarStarted) {
    sidebarStarted = true;
    refreshProjectsAndViews();
    refreshAllBoard();
    refreshInbox();
  }
}

export function SidebarGroups({ route }) {
  startBoardSidebarSync();
  const projects = projectsState.value.items.filter((p) => !p.archived);
  const views = viewsState.value.items;
  const tasks = allBoardState.value.tasks;
  const slug = route?.screen === "board" ? (route.path?.[0] ?? "") : "";
  const isViewSlug = views.some((v) => v.id === slug);
  const myWorkOn = isMyWorkSelected(route);

  return html`<div class="side-groups">
    <div class="side-group-label">Projects</div>
    <a data-row data-item="proj-all" data-act="go-projects"
      class=${`nav-row sub${slug === "projects" ? " selected" : ""}`}
      href="#board/projects"
      aria-current=${slug === "projects" ? "page" : undefined}>
      <span class="key-slot"></span><span class="glyph" aria-hidden="true">▣</span>
      <span class="nav-label">All projects</span>
      <span class="count">${projects.length || null}</span>
    </a>
    ${projects.map((project) => {
      const openCount = projectStateCounts(tasks, project).open;
      const selected = route?.screen === "board" && slug === project.id && !isViewSlug;
      return html`<a key=${project.id} data-row data-item=${`proj-${project.id}`}
        class=${`nav-row sub${selected ? " selected" : ""}`}
        href=${`#board/${encodeURIComponent(project.id)}`}
        aria-current=${selected ? "page" : undefined}>
        <span class="key-slot"></span>
        <span class="nav-label mono">${project.name}</span>
        <span class="count">${openCount}</span>
      </a>`;
    })}
    <div class="side-group-label">Views</div>
    ${views.map((view) => {
      const count = viewTaskCount(tasks, view);
      const selected = route?.screen === "board" && slug === view.id && !(view.id === "open-work" && myWorkOn);
      return html`<a key=${view.id} data-row data-item=${`view-${view.id}`}
        class=${`nav-row sub${selected ? " selected" : ""}`}
        href=${`#board/${encodeURIComponent(view.id)}`}
        aria-current=${selected ? "page" : undefined}>
        <span class="key-slot"></span>
        <span class="nav-label">${view.name || view.label || view.id}</span>
        <span class="count">${count}</span>
      </a>`;
    })}
  </div>`;
}

function projectLines(lines, project) {
  return lines.filter((line) => !line.archived && (line.project === project.id || (!line.project && line.repo === project.repo)));
}

function projectSources(tasks, project) {
  const seen = new Set();
  for (const task of tasks) {
    if (!taskBelongsToProject(task, project)) continue;
    const sys = task?.primary_ref?.system;
    if (sys) seen.add(sys);
    else if (task?.origin) {
      const part = String(task.origin).split(":")[1];
      if (part) seen.add(part);
    }
  }
  return [...seen];
}

export class ProjectsQueue extends Component {
  componentDidMount() {
    startBoardSidebarSync();
    this.syncHost();
  }
  componentDidUpdate() {
    this.syncHost();
  }
  syncHost() {
    const host = this.base;
    if (!host) return;
    const projects = projectsState.peek().items.filter((p) => !p.archived);
    host.uiMove = (delta, relative) => {
      if (!projects.length) return false;
      const cur = selectedProjectId.peek() || projects[0].id;
      const idx = Math.max(0, projects.findIndex((p) => p.id === cur));
      const nextIdx = relative
        ? Math.max(0, Math.min(projects.length - 1, idx + delta))
        : (delta < 0 ? projects.length - 1 : 0);
      const next = projects[nextIdx];
      if (!next) return false;
      selectedProjectId.value = next.id;
      selectItem("board-projects", next.id, "queue");
      return true;
    };
    syncList(host);
  }
  render({ retry }) {
    const pState = projectsState.value;
    const bState = allBoardState.value;
    const projects = pState.items.filter((p) => !p.archived);
    const tasks = bState.tasks;
    const lines = bState.lines;

    if (pState.error && !projects.length) {
      return html`<${Fault} what="Projects" fault=${pState.error} retry=${() => { refreshProjectsAndViews(); retry?.(); }} />`;
    }
    if (!pState.loaded && !projects.length) {
      return html`<${Loading} what="projects" />`;
    }
    if (!projects.length) {
      return html`<${Empty} what="No projects configured" />`;
    }

    const activeId = projects.some((p) => p.id === selectedProjectId.value)
      ? selectedProjectId.value
      : projects[0].id;
    if (selectedProjectId.peek() !== activeId) selectedProjectId.value = activeId;

    return html`<div class="board-projects-wrap" data-list="board-projects">
      <table class="board-tbl board-projects-tbl">
        <thead>
          <tr>
            <th>Project</th>
            <th>Repo</th>
            <th>Progress</th>
            ${STATES.map((s) => html`<th key=${s} class="num">${s === "needs-you" ? "◆ you" : s}</th>`)}
            <th>Lines</th>
          </tr>
        </thead>
        <tbody>
          ${projects.map((project) => {
            const counts = projectStateCounts(tasks, project);
            const segs = progressSegments(counts);
            const pLines = projectLines(lines, project).map((l) => l.name || l.id);
            const isSelected = project.id === activeId;
            return html`<tr key=${project.id}
              class=${`board-tr${isSelected ? " sel" : ""}`}
              data-row
              data-item=${project.id}
              data-open=${`#board/${encodeURIComponent(project.id)}`}
              onClick=${() => { selectedProjectId.value = project.id; selectItem("board-projects", project.id, "queue"); }}
              onDblClick=${() => { location.hash = `#board/${encodeURIComponent(project.id)}`; }}>
              <td class="mono">${project.name}</td>
              <td class="mono muted">${project.repo || project.github || "—"}</td>
              <td>
                <span class="proj-bar" aria-label="Progress by state">
                  ${segs.map((seg) => html`<span key=${seg.state}
                    class=${`proj-seg st-${seg.state} ${seg.pctClass}`}
                    title=${`${STATE_NAMES[seg.state]}: ${seg.count}`}></span>`)}
                </span>
              </td>
              ${STATES.map((s) => html`<td key=${s} class=${`num${s === "needs-you" && counts[s] > 0 ? " ny-num" : " muted"}`}>${counts[s] || 0}</td>`)}
              <td class="mono muted" title=${pLines.join(", ")}>${pLines.join(", ") || "—"}</td>
            </tr>`;
          })}
        </tbody>
      </table>
    </div>`;
  }
}

export function ProjectsFocus() {
  const projects = projectsState.value.items.filter((p) => !p.archived);
  const tasks = allBoardState.value.tasks;
  const lines = allBoardState.value.lines;
  const project = projects.find((p) => p.id === selectedProjectId.value) ?? projects[0] ?? null;

  if (!project) {
    return html`<div class="board-peek-empty muted">No project selected.</div>`;
  }
  const counts = projectStateCounts(tasks, project);
  const pLines = projectLines(lines, project);
  const sources = projectSources(tasks, project);
  const concurrency = pLines.reduce((sum, l) => sum + (Number(l.concurrency) || 1), 0) || 1;
  const href = `#board/${encodeURIComponent(project.id)}`;

  return html`<div class="board-peek">
    <div class="board-peek-meta">
      <span class="muted mono">project</span>
      <span class="board-spacer"></span>
      <a class="board-open-btn primary" data-primary-candidate="" href=${href}>Open board <kbd>↵</kbd></a>
    </div>
    <h2 class="board-peek-title mono">${project.name}</h2>
    <div class="board-props">
      <div class="board-prop"><span class="board-prop-lbl">Repo</span><span class="board-prop-val mono">${project.repo || project.github || "—"}</span></div>
      <div class="board-prop"><span class="board-prop-lbl">Lines</span><span class="board-prop-val mono">${pLines.map((l) => l.name || l.id).join(", ") || "none"}</span></div>
      <div class="board-prop"><span class="board-prop-lbl">Sources</span><span class="board-prop-val mono">${sources.join(", ") || "local"}</span></div>
      <div class="board-prop"><span class="board-prop-lbl">Concurrency</span><span class="board-prop-val mono">${`${concurrency} ${concurrency === 1 ? "run" : "runs"} at once`}</span></div>
      <div class="board-prop"><span class="board-prop-lbl">Priority</span><span class="board-prop-val mono">${project.priority != null ? `#${project.priority}` : "—"}</span></div>
    </div>
    <div class="board-props">
      <div class="board-prop-head">Tasks by state</div>
      ${STATES.map((s) => html`<div key=${s} class="board-prop">
        <span class="board-prop-lbl">${s === "needs-you" ? "needs you" : s}</span>
        <span class="board-prop-val mono">${counts[s] || 0}</span>
      </div>`)}
    </div>
  </div>`;
}
