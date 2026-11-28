import { Component, h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { signal } from "./vendor-signals.js";
import { board as boardRoutes, taskHash } from "./ui-data.js";
import { webFetch } from "./ui-request.js";
import { prepareWrite, sendWrite } from "./ui-write.js";
import { notice, staleWrite } from "./ui-notice.js";
import { onLiveEvent } from "./ui-live.js";
import { boardTask } from "./ui-events.js";
import { Menu, dismissMenu } from "./ui-menu.js";
import { register as registerPalette } from "./ui-palette.js";
import { selectItem, syncList } from "./ui-keys.js";
import { Empty, Fault, Loading } from "./ui-states.js";
import {
  BoardList,
  BoardTable,
  BoardTimeline,
  DisplayMenu,
  GROUP_OPTIONS,
  LAYOUTS,
  LAYOUT_LABELS,
  nextLayout,
  priorityLabel,
  priorityRank,
  resolveLayout,
  saveBoardLayout,
  timelineData,
} from "./ui-board-layouts.js";
import {
  ProjectsFocus,
  ProjectsQueue,
  STATES,
  STATE_LABELS,
  STATE_NAMES,
  allBoardState,
  applyTaskPatchToAllBoard,
  boardLiveRevision,
  canonicalState,
  inboxState,
  isFaultTask,
  isHumanOwner,
  isOpenTask,
  isUnsyncedTask,
  mineOnOpenWork,
  onActivateMyWork,
  ownerLabel,
  projectsState,
  refreshAllBoard,
  selectedProjectId,
  startBoardSidebarSync,
  taskBelongsToProject,
  taskNeedsYou,
  taskSyncState,
  viewsState,
} from "./ui-board-projects.js";

const html = htm.bind(h);

export const BOARD_API_URL = "/api/v1/screens/board";
export const TASKS_API_URL = "/api/v1/tasks";

const SOURCE_TAGS = {
  jira: "JIRA",
  github: "GH",
  slack: "SLACK",
  asana: "ASANA",
  email: "EMAIL",
  meeting: "MEET",
  meetings: "MEET",
  llm: "LLM",
  paste: "PASTE",
  intake: "PASTE",
  loop: "ADW",
  adw: "ADW",
  local: "ADW",
};

const SYNC_GLYPHS = {
  ahead: "↑",
  behind: "↓",
  conflict: "↕",
  synced: "✓",
  off: "×",
  local: "",
};

const FILTER_DEFS = [
  { id: "mine", label: "Mine", hidden: false },
  { id: "ny", label: "◆ Needs you", hidden: false },
  { id: "unsynced", label: "↑ Unsynced", hidden: false },
  { id: "agent", label: "Agent", hidden: true },
  { id: "human", label: "Human", hidden: true },
];

const SORT_OPTIONS = [
  { id: "priority", label: "Priority" },
  { id: "updated", label: "Recently updated" },
  { id: "id", label: "Id" },
];

export const boardViewState = signal({
  slug: "",
  tasks: null,
  savedView: null,
  lines: [],
  loaded: false,
  error: null,
});

export const boardUiState = signal({
  filtersByBoard: {},
  groupByBoard: {},
  sortByBoard: {},
  layoutByBoard: {},
  tableSort: { col: "", dir: "asc" },
  query: "",
  showDone: false,
  displayOpen: false,
  selectedByBoard: {},
  peekTab: "overview",
  draftOpen: false,
  draftTitle: "",
  draftSaving: false,
  pinned: null,
  history: { id: "", comments: [], runs: [], events: [], loaded: false, error: null },
});

let boardLiveHooked = false;
let lastLoadedSlug = null;
let viewRefreshTimer = null;
let paletteDispose = null;

function patchUi(change) {
  boardUiState.value = { ...boardUiState.peek(), ...change };
}

export function setBoardFilter(boardId, filterId, enabled) {
  const ui = boardUiState.peek();
  const prev = ui.filtersByBoard[boardId] ?? [];
  const next = enabled
    ? (prev.includes(filterId) ? prev : [...prev, filterId])
    : prev.filter((item) => item !== filterId);
  patchUi({ filtersByBoard: { ...ui.filtersByBoard, [boardId]: next } });
  if (boardId === "open-work" && filterId === "mine") {
    mineOnOpenWork.value = Boolean(enabled);
  }
}

export function toggleBoardFilter(boardId, filterId) {
  const prev = boardUiState.peek().filtersByBoard[boardId] ?? [];
  setBoardFilter(boardId, filterId, !prev.includes(filterId));
}

export function toggleShowDone() {
  patchUi({ showDone: !boardUiState.peek().showDone });
}

export function openDisplayMenu(open = true) {
  patchUi({ displayOpen: Boolean(open) });
}

export function setBoardGroup(boardId, group) {
  const ui = boardUiState.peek();
  patchUi({ groupByBoard: { ...ui.groupByBoard, [boardId]: group } });
}

export function setBoardSort(boardId, sort) {
  const ui = boardUiState.peek();
  patchUi({ sortByBoard: { ...ui.sortByBoard, [boardId]: sort }, tableSort: { col: "", dir: "asc" } });
}

export function setBoardLayout(boardId, layout, savedView = null) {
  if (!LAYOUTS.includes(layout)) return;
  const ui = boardUiState.peek();
  patchUi({
    layoutByBoard: { ...ui.layoutByBoard, [boardId]: layout },
    displayOpen: false,
  });
  saveBoardLayout(boardId, layout, savedView, (updatedView) => {
    const vs = viewsState.peek();
    viewsState.value = {
      ...vs,
      items: vs.items.map((v) => (v.id === updatedView.id ? updatedView : v)),
    };
    const bvs = boardViewState.peek();
    if (bvs.savedView?.id === updatedView.id) {
      boardViewState.value = { ...bvs, savedView: updatedView };
    }
  });
}

export function sourceName(task) {
  const sys = task?.primary_ref?.system;
  if (sys) return String(sys).toLowerCase();
  const origin = String(task?.origin ?? "");
  if (origin.startsWith("ref:")) return origin.split(":")[1] || "local";
  return "local";
}

export function sourceTag(task) {
  const src = sourceName(task);
  return SOURCE_TAGS[src] ?? src.toUpperCase();
}

export function sourceKey(task) {
  const ref = task?.primary_ref;
  if (ref?.key) return String(ref.key).replace(/^gh:/i, "");
  const origin = String(task?.origin ?? "");
  if (origin.startsWith("ref:")) return origin.split(":").slice(2).join(":");
  return task?.repo || "";
}

export function externalLabel(task) {
  const ref = task?.primary_ref;
  if (!ref) return "local only";
  const st = String(ref.ext_state || "open").split(" · ")[0];
  return `${ref.system} · ${st}`;
}

export function externalShortState(task) {
  const ref = task?.primary_ref;
  if (!ref) return "local only";
  return String(ref.ext_state || "open").split(" · ")[0];
}

export function syncGlyph(task) {
  return SYNC_GLYPHS[taskSyncState(task)] ?? "";
}

export function needsYouKind(task, inboxItems) {
  if (isFaultTask(task)) return "fault";
  if (task?.needsYouKind) return String(task.needsYouKind);
  const ids = Array.isArray(task?.needs_you_ids) ? task.needs_you_ids : [];
  const match = inboxItems.find((item) => ids.includes(item.id) || item.task_id === task?.id);
  if (match?.kind) return String(match.kind);
  return taskNeedsYou(task) ? "you" : "";
}

export function formatAge(iso, nowMs = Date.now()) {
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "";
  const sec = Math.max(0, Math.floor((nowMs - ts) / 1000));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h`;
  return `${Math.floor(sec / 86400)}d`;
}

function formatStamp(iso) {
  const ts = Date.parse(iso);
  return Number.isFinite(ts) ? new Date(ts).toLocaleString() : "";
}

export function resolveBoardContext(route) {
  const slug = route?.path?.[0] ?? "";
  const views = viewsState.value.items;
  const projects = projectsState.value.items.filter((p) => !p.archived);
  const savedView = views.find((v) => v.id === slug) ?? null;
  const project = !savedView ? projects.find((p) => p.id === slug) ?? null : null;
  if (slug === "projects") {
    return { kind: "projects", id: "projects", name: "Projects", filter: "", savedView: null, project: null };
  }
  if (savedView) {
    return {
      kind: "view",
      id: savedView.id,
      name: savedView.name || savedView.label || savedView.id,
      filter: savedView.filter || (savedView.project ? `project = ${savedView.project}` : "all open tasks"),
      savedView,
      project: savedView.project ? projects.find((p) => p.id === savedView.project) ?? null : null,
    };
  }
  if (project) {
    return {
      kind: "project",
      id: project.id,
      name: project.name,
      filter: `project = ${project.id}`,
      savedView: null,
      project,
    };
  }
  if (slug === "open-work" || !slug) {
    return {
      kind: "open-work",
      id: "open-work",
      name: "Open work",
      filter: "state in intake, ready, running, needs-you, review",
      savedView: null,
      project: null,
    };
  }
  return {
    kind: "view",
    id: slug,
    name: slug,
    filter: `view = ${slug}`,
    savedView: null,
    project: null,
  };
}

export function observeBoardSignals() {
  projectsState.value;
  viewsState.value;
  allBoardState.value;
  inboxState.value;
  mineOnOpenWork.value;
  selectedProjectId.value;
  boardLiveRevision.value;
  boardViewState.value;
  boardUiState.value;
}

async function fetchBoardForContext(ctx) {
  const routes = boardRoutes(ctx.id);
  const candidates = ctx.kind === "open-work"
    ? [BOARD_API_URL]
    : ctx.kind === "project"
      ? [routes.boardProject(ctx.id), BOARD_API_URL]
      : [routes.boardView(ctx.id), routes.boardProject(ctx.id), BOARD_API_URL];
  let lastError = null;
  for (const url of candidates) {
    try {
      const res = await webFetch(url, { cache: "no-store" });
      const body = await res.json().catch(() => null);
      if (res.status === 404 && url !== BOARD_API_URL) continue;
      if (!res.ok) {
        lastError = { status: res.status, message: body?.detail ?? body?.error ?? `HTTP ${res.status}` };
        break;
      }
      boardViewState.value = {
        slug: ctx.id,
        tasks: Array.isArray(body?.tasks) ? body.tasks : [],
        savedView: body?.saved_view ?? ctx.savedView ?? null,
        lines: Array.isArray(body?.lines) ? body.lines : [],
        loaded: true,
        error: null,
      };
      if (url === BOARD_API_URL && Array.isArray(body?.tasks)) {
        allBoardState.value = {
          ...allBoardState.peek(),
          tasks: body.tasks,
          lines: Array.isArray(body.lines) ? body.lines : allBoardState.peek().lines,
          columns: Array.isArray(body.columns) ? body.columns : allBoardState.peek().columns,
          runningIds: Array.isArray(body.running_ids) ? body.running_ids : allBoardState.peek().runningIds,
          andon: body.andon ?? allBoardState.peek().andon,
          loaded: true,
          error: null,
        };
      }
      return;
    } catch (err) {
      lastError = { status: null, message: err?.message ?? "Network error" };
      break;
    }
  }
  boardViewState.value = {
    ...boardViewState.peek(),
    slug: ctx.id,
    loaded: true,
    error: lastError,
  };
}

async function fetchTaskHistory(taskId) {
  if (!taskId) return;
  const routes = boardRoutes();
  patchUi({ history: { id: taskId, comments: [], runs: [], events: [], loaded: false, error: null } });
  try {
    const res = await webFetch(routes.history(taskId), { cache: "no-store" });
    const body = await res.json().catch(() => null);
    if (boardUiState.peek().history.id !== taskId) return;
    if (!res.ok) {
      patchUi({ history: { id: taskId, comments: [], runs: [], events: [], loaded: true, error: { status: res.status, message: body?.detail ?? body?.error ?? `HTTP ${res.status}` } } });
      return;
    }
    patchUi({
      history: {
        id: taskId,
        comments: Array.isArray(body?.comments) ? body.comments : [],
        runs: Array.isArray(body?.runs) ? body.runs : [],
        events: Array.isArray(body?.events) ? body.events : [],
        loaded: true,
        error: null,
      },
    });
  } catch (err) {
    if (boardUiState.peek().history.id !== taskId) return;
    patchUi({ history: { id: taskId, comments: [], runs: [], events: [], loaded: true, error: { status: null, message: err?.message ?? "Network error" } } });
  }
}

function applyTaskPatchToCurrentBoard(patch, fromExternal = false) {
  if (!patch?.id) return;
  const bvs = boardViewState.peek();
  if (Array.isArray(bvs.tasks)) {
    const existing = bvs.tasks.find((item) => item.id === patch.id);
    if (existing && patch.version != null && existing.version > patch.version) return;
    const prevState = existing ? canonicalState(existing) : null;
    const nextState = canonicalState({ ...existing, ...patch });
    if (fromExternal && existing && prevState !== nextState) {
      const ui = boardUiState.peek();
      const selectedId = ui.selectedByBoard[bvs.slug];
      if (selectedId === patch.id) {
        const prevCol = bvs.tasks.filter((t) => canonicalState(t) === prevState);
        const rowIndex = Math.max(0, prevCol.findIndex((t) => t.id === patch.id));
        const pin = ui.pinned?.id === patch.id ? ui.pinned : { id: patch.id, column: prevState, index: rowIndex };
        patchUi({ pinned: pin });
        notice.value = `moved to ${STATE_NAMES[nextState] ?? nextState}`;
      }
    }
    const nextTasks = existing
      ? bvs.tasks.map((item) => (item.id === patch.id ? { ...item, ...patch } : item))
      : [patch, ...bvs.tasks];
    boardViewState.value = { ...bvs, tasks: nextTasks };
  }
}

function ensureBoardLiveHooks() {
  startBoardSidebarSync();
  if (boardLiveHooked) return;
  boardLiveHooked = true;
  onActivateMyWork(() => {
    setBoardFilter("open-work", "mine", true);
  });
  onLiveEvent((event) => {
    if (!event?.kind) return;
    if (event.kind.startsWith("task.") || event.kind.startsWith("run.")) {
      const patch = boardTask(event);
      if (patch) applyTaskPatchToCurrentBoard(patch, true);
      clearTimeout(viewRefreshTimer);
      viewRefreshTimer = setTimeout(() => {
        if (lastLoadedSlug && lastLoadedSlug !== "projects") {
          const cur = resolveBoardContext({ screen: "board", path: lastLoadedSlug === "open-work" ? [] : [lastLoadedSlug] });
          fetchBoardForContext(cur);
        }
      }, 200);
    } else if (event.kind === "reset" || event.kind === "view.changed" || event.kind === "project.changed" || event.kind.startsWith("inbox.") || event.kind === "ref.changed") {
      clearTimeout(viewRefreshTimer);
      viewRefreshTimer = setTimeout(() => {
        if (lastLoadedSlug && lastLoadedSlug !== "projects") {
          const cur = resolveBoardContext({ screen: "board", path: lastLoadedSlug === "open-work" ? [] : [lastLoadedSlug] });
          fetchBoardForContext(cur);
        }
      }, 200);
    }
  });
}

function scopeTasksForContext(ctx) {
  const bvs = boardViewState.value;
  const all = allBoardState.value.tasks;
  if (ctx.kind === "view" && bvs.slug === ctx.id && Array.isArray(bvs.tasks)) {
    return bvs.tasks;
  }
  if (ctx.kind === "project" && ctx.project) {
    const base = bvs.slug === ctx.id && Array.isArray(bvs.tasks) ? bvs.tasks : all;
    return base.filter((t) => taskBelongsToProject(t, ctx.project));
  }
  if (bvs.slug === ctx.id && Array.isArray(bvs.tasks)) return bvs.tasks;
  return all;
}

export function filterAndSortTasks(tasks, { filters = [], query = "", sort = "priority", showDone = false, layout = "board" }) {
  let out = tasks.slice();
  if (filters.includes("mine")) out = out.filter((t) => isHumanOwner(t.owner));
  if (filters.includes("ny")) out = out.filter((t) => taskNeedsYou(t));
  if (filters.includes("unsynced")) out = out.filter((t) => isUnsyncedTask(t));
  if (filters.includes("agent")) out = out.filter((t) => !isHumanOwner(t.owner));
  if (filters.includes("human")) out = out.filter((t) => isHumanOwner(t.owner));
  const needle = String(query ?? "").trim().toLowerCase();
  if (needle) {
    out = out.filter((t) => `${t.id} ${t.title} ${t.repo} ${t.line || ""} ${t.owner || ""} ${sourceKey(t)}`.toLowerCase().includes(needle));
  }
  if (layout !== "board" && !showDone) {
    out = out.filter((t) => isOpenTask(t));
  }
  if (sort === "updated") {
    out.sort((a, b) => (Date.parse(b.updated) || 0) - (Date.parse(a.updated) || 0) || String(a.id).localeCompare(String(b.id)));
  } else if (sort === "id") {
    out.sort((a, b) => String(a.id).localeCompare(String(b.id)));
  } else {
    out.sort((a, b) => priorityRank(a.priority) - priorityRank(b.priority) || (Date.parse(b.updated) || 0) - (Date.parse(a.updated) || 0) || String(a.id).localeCompare(String(b.id)));
  }
  return out;
}

function defaultSelection(tasks) {
  return tasks.find((t) => taskNeedsYou(t))?.id ?? tasks[0]?.id ?? "";
}

function selectBoardTask(boardId, taskId, focusList = true) {
  const ui = boardUiState.peek();
  const clearPin = ui.pinned && ui.pinned.id !== taskId ? null : ui.pinned;
  patchUi({
    selectedByBoard: { ...ui.selectedByBoard, [boardId]: taskId },
    pinned: clearPin,
  });
  if (focusList && taskId && typeof globalThis.document !== "undefined") selectItem("board-tasks", taskId, "queue");
  if (ui.peekTab === "activity" && taskId && ui.history.id !== taskId) {
    fetchTaskHistory(taskId);
  }
}

export async function createBoardTask(title, ctx) {
  const text = String(title ?? "").trim();
  if (!text) return null;
  const projects = projectsState.peek().items.filter((p) => !p.archived);
  const targetProject = ctx.project ?? projects[0] ?? null;
  const repo = targetProject?.repo || targetProject?.id || "";
  if (!repo) {
    notice.value = "Failed: No repository configured for new task";
    return null;
  }
  patchUi({ draftSaving: true });
  try {
    const payload = { repo, title: text, body: text };
    if (targetProject?.id) payload.project = targetProject.id;
    const createReq = prepareWrite(TASKS_API_URL, payload, null, { method: "POST" });
    const createdRes = await sendWrite(createReq);
    let task = createdRes?.task;
    if (task && canonicalState(task) !== "intake") {
      try {
        const moveReq = prepareWrite(`${TASKS_API_URL}/${encodeURIComponent(task.id)}`, { state: "intake" }, task, { method: "PATCH" });
        const movedRes = await sendWrite(moveReq);
        if (movedRes?.task) task = movedRes.task;
      } catch {
        task = createdRes.task;
      }
    }
    if (task) {
      applyTaskPatchToAllBoard(task);
      applyTaskPatchToCurrentBoard(task, false);
      selectBoardTask(ctx.id, task.id, true);
      notice.value = `Created ${task.id}`;
    }
    patchUi({ draftOpen: false, draftTitle: "", draftSaving: false });
    refreshAllBoard();
    return task ?? null;
  } catch (err) {
    patchUi({ draftSaving: false });
    notice.value = `Failed: ${err?.message ?? err}`;
    return null;
  }
}

export async function patchBoardTask(task, fields, ctx) {
  if (!task?.id) return null;
  const url = `${TASKS_API_URL}/${encodeURIComponent(task.id)}`;
  try {
    const req = prepareWrite(url, fields, task, { method: "PATCH" });
    const res = await sendWrite(req);
    const updated = res?.task;
    if (updated) {
      patchUi({ pinned: null });
      applyTaskPatchToAllBoard(updated);
      applyTaskPatchToCurrentBoard(updated, false);
      if (fields.state) notice.value = `moved to ${STATE_NAMES[canonicalState(updated)] ?? updated.state}`;
      else if (fields.priority != null) notice.value = `Updated priority to ${priorityLabel(updated.priority)}`;
    }
    return updated ?? null;
  } catch (err) {
    if (err?.conflict || err?.status === 409) {
      if (!staleWrite.peek()) {
        staleWrite.value = "This item changed. Review the latest version before trying again.";
      }
      if (err.current) {
        applyTaskPatchToAllBoard(err.current);
        applyTaskPatchToCurrentBoard(err.current, false);
      } else if (ctx) {
        fetchBoardForContext(ctx);
      }
      return null;
    }
    notice.value = `Failed: ${err?.message ?? err}`;
    return null;
  }
}

function buildKanbanColumns(tasks, showDone, pinned) {
  const byState = Object.fromEntries(STATES.map((s) => [s, []]));
  const realCounts = Object.fromEntries(STATES.map((s) => [s, 0]));
  let pinnedTask = null;

  for (const task of tasks) {
    const st = canonicalState(task);
    realCounts[st] = (realCounts[st] ?? 0) + 1;
    if (pinned && task.id === pinned.id && pinned.column !== st) {
      pinnedTask = task;
    } else {
      byState[st].push(task);
    }
  }
  if (pinnedTask && byState[pinned.column]) {
    const at = Math.max(0, Math.min(byState[pinned.column].length, pinned.index));
    byState[pinned.column].splice(at, 0, pinnedTask);
  }
  return STATES.map((state) => {
    const rows = byState[state];
    const count = realCounts[state];
    const collapsed = (state === "done" && !showDone) || (!rows.length && state !== "done");
    return { state, label: STATE_LABELS[state], rows, count, collapsed };
  });
}

function renderMarkNode(task) {
  if (!taskNeedsYou(task) && !isFaultTask(task)) return null;
  return isFaultTask(task)
    ? html`<span class="pg fault">▲</span>`
    : html`<span class="pg ny">◆</span>`;
}

function renderStateChip(task, state) {
  if (isFaultTask(task)) return html`<span class="chip fault">▲ fault</span>`;
  if (state === "needs-you") return html`<span class="chip ny">◆ needs you</span>`;
  return html`<span class="st-txt mono">${STATE_LABELS[state] ?? state}</span>`;
}

function renderCard(task, selected, inboxItems, onSelect) {
  const kind = needsYouKind(task, inboxItems);
  const fault = isFaultTask(task);
  const unsynced = isUnsyncedTask(task);
  const sync = syncGlyph(task);
  const srcTag = sourceTag(task);
  const srcK = sourceKey(task);
  return html`<div key=${task.id}
    class=${`board-card${selected ? " sel" : ""}`}
    data-row
    data-item=${task.id}
    data-open=${taskHash(task.id)}
    onClick=${() => onSelect(task.id)}
    onDblClick=${() => { location.hash = taskHash(task.id); }}>
    <div class="c1">
      <span class="mono muted card-id">${task.id}</span>
      ${fault
        ? html`<span class="chip fault">▲ fault</span>`
        : kind ? html`<span class="chip ny">◆ ${kind}</span>` : null}
      ${unsynced ? html`<span class="chip plain">↑ unsynced</span>` : null}
      <span class="tp">${priorityLabel(task.priority)}</span>
    </div>
    <div class="ct" title=${task.title}>${task.title || task.id}</div>
    <div class="csrc" title=${`${sourceName(task)} ${srcK}`.trim()}>
      <span class="tp">${srcTag}</span>
      <span class="ell">${srcK || "local"}</span>
    </div>
    <div class="c2">
      <span class="ext" title=${externalLabel(task)}>${externalShortState(task)}</span>
      ${sync ? html`<span class="sync mono">${sync}</span>` : null}
    </div>
    <div class="c3">
      <span>${task.line || "no line"}</span>
      <span>${ownerLabel(task.owner)}</span>
    </div>
  </div>`;
}

export function BoardHead({ route }) {
  ensureBoardLiveHooks();
  boardLiveRevision.value;
  const ctx = resolveBoardContext(route);
  const ui = boardUiState.value;
  const views = viewsState.value.items;

  if (ctx.kind === "projects") {
    const projects = projectsState.value.items.filter((p) => !p.archived);
    return html`<div class="board-head">
      <div class="board-head-left">
        <h1>Projects</h1>
        <span class="board-head-meta mono muted">${`${projects.length} ${projects.length === 1 ? "project" : "projects"}`}</span>
        <nav class="board-tabs" aria-label="Saved views">
          ${views.map((v) => html`<a key=${v.id} class="board-tab" href=${`#board/${encodeURIComponent(v.id)}`}>${v.name || v.label || v.id}</a>`)}
          <a class="board-tab sel" href="#board/projects" aria-current="page">Projects</a>
        </nav>
      </div>
    </div>`;
  }

  const savedView = boardViewState.value.slug === ctx.id ? (boardViewState.value.savedView ?? ctx.savedView) : ctx.savedView;
  const groupBy = ui.groupByBoard[ctx.id] ?? savedView?.group_by ?? "state";
  const sort = ui.sortByBoard[ctx.id] ?? "priority";
  const filters = ui.filtersByBoard[ctx.id] ?? [];
  const layout = resolveLayout(ctx.id, savedView, groupBy, ui.layoutByBoard[ctx.id] ?? "");
  const scoped = scopeTasksForContext(ctx);
  const shown = filterAndSortTasks(scoped, { filters, query: ui.query, sort, showDone: ui.showDone, layout });

  return html`<div class="board-head">
    <div class="board-head-left">
      <h1>${ctx.name}</h1>
      <span class="board-head-meta mono muted">${`${shown.length} ${shown.length === 1 ? "task" : "tasks"}`}${ctx.filter ? ` · ${ctx.filter}` : ""}</span>
      <nav class="board-tabs" aria-label="Saved views">
        ${views.map((v) => {
          const active = v.id === ctx.id;
          return html`<a key=${v.id}
            class=${`board-tab${active ? " sel" : ""}`}
            href=${`#board/${encodeURIComponent(v.id)}`}
            aria-current=${active ? "page" : undefined}>
            ${v.name || v.label || v.id}
          </a>`;
        })}
      </nav>
    </div>
    <${DisplayMenu}
      layout=${layout}
      open=${ui.displayOpen}
      onToggle=${() => openDisplayMenu(!ui.displayOpen)}
      onPick=${(choice) => setBoardLayout(ctx.id, choice, savedView)} />
  </div>`;
}

export class BoardQueue extends Component {
  syncContext(route) {
    ensureBoardLiveHooks();
    const ctx = resolveBoardContext(route);
    if (ctx.kind === "projects") {
      lastLoadedSlug = "projects";
      return;
    }
    if (lastLoadedSlug !== ctx.id) {
      lastLoadedSlug = ctx.id;
      fetchBoardForContext(ctx);
    }
  }
  syncPalette(route, tasks, ctx, savedView) {
    if (ctx.kind === "projects") {
      paletteDispose?.();
      paletteDispose = null;
      return;
    }
    const items = tasks.slice(0, 80).map((t) => ({
      id: `task-${t.id}`,
      title: `${t.id} ${t.title || ""}`.trim(),
      description: `${STATE_NAMES[canonicalState(t)] ?? t.state}${t.line ? ` · ${t.line}` : ""}`,
      href: taskHash(t.id),
    }));
    const cmds = [
      { id: "board-show-done", title: "Show done column", description: "Toggle the board Done column", run: () => toggleShowDone() },
      { id: "board-display-menu", title: "Display menu", description: "Choose List, Board, Table or Timeline", run: () => openDisplayMenu(true) },
      ...LAYOUTS.map((lay) => ({
        id: `board-layout-${lay}`,
        title: `Board as ${lay}`,
        description: `Switch board layout to ${LAYOUT_LABELS[lay]}`,
        run: () => setBoardLayout(ctx.id, lay, savedView),
      })),
    ];
    paletteDispose?.();
    paletteDispose = registerPalette("board", items, cmds);
  }
  componentDidMount() {
    this.syncContext(this.props.route);
    this.bindHost();
  }
  componentDidUpdate(prevProps) {
    if (prevProps.route?.hash !== this.props.route?.hash) {
      this.syncContext(this.props.route);
    }
    this.bindHost();
    if (boardUiState.peek().draftOpen && !this.wasDraft) {
      this.base?.querySelector(".board-draft-input")?.focus();
    }
    this.wasDraft = boardUiState.peek().draftOpen;
  }
  componentWillUnmount() {
    paletteDispose?.();
    paletteDispose = null;
  }
  bindHost() {
    const host = this.base?.querySelector('[data-list="board-tasks"]');
    if (!host) return;
    const { route, now } = this.props;
    const ctx = resolveBoardContext(route);
    const ui = boardUiState.peek();
    const savedView = boardViewState.peek().slug === ctx.id ? (boardViewState.peek().savedView ?? ctx.savedView) : ctx.savedView;
    const groupBy = ui.groupByBoard[ctx.id] ?? savedView?.group_by ?? "state";
    const sort = ui.sortByBoard[ctx.id] ?? "priority";
    const filters = ui.filtersByBoard[ctx.id] ?? [];
    const layout = resolveLayout(ctx.id, savedView, groupBy, ui.layoutByBoard[ctx.id] ?? "");
    const scoped = scopeTasksForContext(ctx);
    const shown = filterAndSortTasks(scoped, { filters, query: ui.query, sort, showDone: ui.showDone, layout });
    const columns = buildKanbanColumns(shown, ui.showDone, ui.pinned);
    const activeId = shown.some((t) => t.id === ui.selectedByBoard[ctx.id])
      ? ui.selectedByBoard[ctx.id]
      : defaultSelection(shown);

    host.uiCreate = () => patchUi({ draftOpen: true, displayOpen: false });
    host.uiDismiss = () => {
      const cur = boardUiState.peek();
      if (cur.displayOpen) { patchUi({ displayOpen: false }); return true; }
      if (cur.draftOpen) { patchUi({ draftOpen: false, draftTitle: "" }); return true; }
      return false;
    };
    host.uiCycleDisplay = (delta) => {
      const next = nextLayout(layout, delta);
      setBoardLayout(ctx.id, next, savedView);
      return true;
    };
    host.uiMove = (delta, relative) => {
      if (!shown.length) return false;
      if (layout === "board") {
        const visibleCols = columns.filter((col) => !col.collapsed && col.rows.length > 0);
        if (!visibleCols.length) return false;
        let col = visibleCols.find((c) => c.rows.some((t) => t.id === activeId)) ?? visibleCols[0];
        const idx = Math.max(0, col.rows.findIndex((t) => t.id === activeId));
        const nextIdx = relative
          ? Math.max(0, Math.min(col.rows.length - 1, idx + delta))
          : (delta < 0 ? col.rows.length - 1 : 0);
        const nextTask = col.rows[nextIdx];
        if (!nextTask) return false;
        selectBoardTask(ctx.id, nextTask.id, true);
        return true;
      }
      const listOrder = layout === "timeline"
        ? timelineData(shown, now).bars.map((b) => b.task)
        : shown;
      const idx = Math.max(0, listOrder.findIndex((t) => t.id === activeId));
      const nextIdx = relative
        ? Math.max(0, Math.min(listOrder.length - 1, idx + delta))
        : (delta < 0 ? listOrder.length - 1 : 0);
      const nextTask = listOrder[nextIdx];
      if (!nextTask) return false;
      selectBoardTask(ctx.id, nextTask.id, true);
      return true;
    };
    host.uiStep = (delta) => {
      if (layout === "board") {
        const visibleCols = columns.filter((col) => !col.collapsed && col.rows.length > 0);
        if (!visibleCols.length) return false;
        const ci = visibleCols.findIndex((c) => c.rows.some((t) => t.id === activeId));
        if (ci < 0) return false;
        const ni = ci + delta;
        if (ni < 0 || ni >= visibleCols.length) return false;
        const curIdx = Math.max(0, visibleCols[ci].rows.findIndex((t) => t.id === activeId));
        const targetRows = visibleCols[ni].rows;
        const nextTask = targetRows[Math.min(curIdx, targetRows.length - 1)];
        if (!nextTask) return false;
        selectBoardTask(ctx.id, nextTask.id, true);
        return true;
      }
      if (layout === "timeline") {
        const ordered = timelineData(shown, now).bars.map((b) => b.task);
        const idx = ordered.findIndex((t) => t.id === activeId);
        if (idx < 0) return false;
        const ni = idx + delta;
        if (ni < 0 || ni >= ordered.length) return false;
        selectBoardTask(ctx.id, ordered[ni].id, true);
        return true;
      }
      return false;
    };
    syncList(host);
  }
  render({ route, now, retry }) {
    ensureBoardLiveHooks();
    boardLiveRevision.value;
    const ctx = resolveBoardContext(route);
    if (ctx.kind === "projects") {
      return html`<${ProjectsQueue} route=${route} retry=${retry} />`;
    }

    const ui = boardUiState.value;
    const bvs = boardViewState.value;
    const allState = allBoardState.value;
    const inboxItems = inboxState.value.items;
    const savedView = bvs.slug === ctx.id ? (bvs.savedView ?? ctx.savedView) : ctx.savedView;
    const groupBy = ui.groupByBoard[ctx.id] ?? savedView?.group_by ?? "state";
    const sort = ui.sortByBoard[ctx.id] ?? "priority";
    const filters = ui.filtersByBoard[ctx.id] ?? [];
    const layout = resolveLayout(ctx.id, savedView, groupBy, ui.layoutByBoard[ctx.id] ?? "");

    const scoped = scopeTasksForContext(ctx);
    const shown = filterAndSortTasks(scoped, { filters, query: ui.query, sort, showDone: ui.showDone, layout });
    const doneCount = scoped.filter((t) => canonicalState(t) === "done").length;

    const fault = (bvs.slug === ctx.id ? bvs.error : null) ?? allState.error;
    const pending = !allState.loaded && !(bvs.slug === ctx.id && bvs.loaded);

    const activeId = shown.some((t) => t.id === ui.selectedByBoard[ctx.id])
      ? ui.selectedByBoard[ctx.id]
      : defaultSelection(shown);
    if (activeId && ui.selectedByBoard[ctx.id] !== activeId) {
      boardUiState.value = { ...boardUiState.peek(), selectedByBoard: { ...ui.selectedByBoard, [ctx.id]: activeId } };
    }

    this.syncPalette(route, shown, ctx, savedView);

    const filterCounts = {
      mine: scoped.filter((t) => isHumanOwner(t.owner)).length,
      ny: scoped.filter((t) => taskNeedsYou(t)).length,
      unsynced: scoped.filter((t) => isUnsyncedTask(t)).length,
      agent: scoped.filter((t) => !isHumanOwner(t.owner)).length,
      human: scoped.filter((t) => isHumanOwner(t.owner)).length,
    };
    const groupLabel = GROUP_OPTIONS.find((g) => g.id === groupBy)?.label ?? "State";
    const sortLabel = SORT_OPTIONS.find((s) => s.id === sort)?.label ?? "Priority";

    const helpers = {
      markNode: renderMarkNode,
      stateChip: renderStateChip,
      externalLabel,
      syncGlyph,
      sourceName,
      age: (iso) => formatAge(iso, now),
    };

    const columns = buildKanbanColumns(shown, ui.showDone, ui.pinned);

    const onTableSort = (colId) => {
      const prev = ui.tableSort;
      const dir = prev.col === colId && prev.dir === "asc" ? "desc" : "asc";
      patchUi({ tableSort: { col: colId, dir } });
    };

    const submitDraft = (event) => {
      event.preventDefault();
      if (!ui.draftSaving) createBoardTask(ui.draftTitle, ctx);
    };

    const onDraftKeyDown = (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey || !event.shiftKey)) {
        event.preventDefault();
        if (!ui.draftSaving) createBoardTask(ui.draftTitle, ctx);
      } else if (event.key === "Escape") {
        event.preventDefault();
        patchUi({ draftOpen: false, draftTitle: "" });
      }
    };

    return html`<div class="ui-list board-shell" data-list="board-tasks">
      <div class="ui-list-header board-header">
        <${Menu} label=${`Group: ${groupBy}`} title=${`Group: ${groupLabel}`}>${(close) => [
          ...GROUP_OPTIONS.map((opt) => html`<button key=${opt.id}
            type="button"
            role="menuitemcheckbox"
            aria-checked=${groupBy === opt.id}
            onClick=${() => { setBoardGroup(ctx.id, opt.id); close(); }}>
            <span>${groupBy === opt.id ? "✓" : "　"} ${opt.label}</span>
          </button>`),
          html`<div key="sep" class="ui-menu-label">Columns</div>`,
          html`<button key="showdone"
            type="button"
            role="menuitemcheckbox"
            aria-checked=${ui.showDone}
            onClick=${() => { toggleShowDone(); close(); }}>
            <span>${ui.showDone ? "✓" : "　"} Show done column</span>
            <b>${doneCount}</b>
          </button>`,
        ]}<//>

        <${Menu} label="Filter" title="Filter">${() => FILTER_DEFS.map((def) => html`<button key=${def.id}
          type="button"
          role="menuitemcheckbox"
          aria-checked=${filters.includes(def.id)}
          hidden=${def.hidden}
          class=${def.hidden ? "board-filter-hidden" : ""}
          onClick=${() => toggleBoardFilter(ctx.id, def.id)}>
          <span>${filters.includes(def.id) ? "✓" : "　"} ${def.label}</span>
          <b>${filterCounts[def.id] ?? 0}</b>
        </button>`)}<//>

        ${filters.length ? html`<div class="ui-list-chips">
          ${filters.map((fid) => {
            const def = FILTER_DEFS.find((item) => item.id === fid);
            return html`<button key=${fid}
              type="button"
              class="ui-list-chip"
              title=${`Remove ${def?.label ?? fid}`}
              aria-label=${`Remove ${def?.label ?? fid}`}
              onClick=${() => setBoardFilter(ctx.id, fid, false)}>
              <span>${def?.label ?? fid}</span>
              <span aria-hidden="true">×</span>
            </button>`;
          })}
        </div>` : null}

        <button type="button"
          class=${`board-done-toggle${ui.showDone ? " on" : ""}`}
          data-act="show-done"
          aria-pressed=${ui.showDone}
          onClick=${() => toggleShowDone()}>
          ${ui.showDone ? `✓ Showing done ${doneCount}` : `○ Show done ${doneCount}`}
        </button>

        <input type="search"
          data-search=""
          aria-label="Search tasks"
          placeholder="/ search"
          value=${ui.query}
          spellcheck=${false}
          autoComplete="off"
          onInput=${(e) => patchUi({ query: e.currentTarget.value })} />

        <span class="ui-list-count" title=${`${shown.length} of ${scoped.length}`}>${`${shown.length}/${scoped.length}`}</span>

        <${Menu} label="⇅" title=${`Sort: ${sortLabel}`}>${(close) => SORT_OPTIONS.map((opt) => html`<button key=${opt.id}
          type="button"
          role="menuitemcheckbox"
          aria-checked=${sort === opt.id}
          onClick=${() => { setBoardSort(ctx.id, opt.id); close(); }}>
          <span>${sort === opt.id ? "✓" : "　"} ${opt.label}</span>
        </button>`)}<//>

        <button type="button"
          class="board-new-btn"
          data-act="new-task"
          onClick=${() => { dismissMenu(); patchUi({ draftOpen: true, displayOpen: false }); }}>
          [c] New task
        </button>
      </div>

      ${ui.draftOpen ? html`<form class="ops-form board-draft" onSubmit=${submitDraft}>
        <label class="board-draft-lbl" for="board-new-task-input">New task</label>
        <input id="board-new-task-input"
          class="board-draft-input"
          type="text"
          placeholder="Task title (Cmd+Enter to create in Intake, Esc to cancel)"
          value=${ui.draftTitle}
          disabled=${ui.draftSaving}
          onInput=${(e) => patchUi({ draftTitle: e.currentTarget.value })}
          onKeyDown=${onDraftKeyDown} />
        <button type="submit" disabled=${ui.draftSaving || !ui.draftTitle.trim()}>Create</button>
        <button type="button" onClick=${() => patchUi({ draftOpen: false, draftTitle: "" })}>Cancel</button>
      </form>` : null}

      <div class="ui-list-scroll board-body">
        ${fault && !scoped.length
          ? html`<${Fault} what="Board" fault=${fault} retry=${() => { fetchBoardForContext(ctx); refreshAllBoard(); retry?.(); }} />`
          : pending
            ? html`<${Loading} what="board" />`
            : !shown.length && layout !== "board"
              ? html`<${Empty} what="No matching tasks" />`
              : layout === "board"
                ? html`<div class="board-cols">
                    ${columns.map((col) => {
                      if (col.collapsed) {
                        const isDone = col.state === "done";
                        return html`<button key=${col.state}
                          type="button"
                          class=${`board-col shut${isDone ? " done-shut" : ""}`}
                          data-state=${col.state}
                          title=${isDone ? "Show done" : `${col.label}: 0`}
                          onClick=${isDone ? () => toggleShowDone() : undefined}>
                          <span class="vt">${isDone ? `✓ Done · ${col.count}` : `${col.label} · 0`}</span>
                        </button>`;
                      }
                      return html`<section key=${col.state} class="board-col" data-state=${col.state} aria-label=${col.label}>
                        <div class="board-col-h">
                          <span class=${`board-col-lbl${col.state === "needs-you" ? " ny" : ""}`}>${col.label}</span>
                          <span class="board-col-n mono muted">${col.count}</span>
                          ${col.state === "done"
                            ? html`<span class="board-spacer"></span><button type="button" class="board-col-hide mono muted" onClick=${() => toggleShowDone()}>hide</button>`
                            : null}
                        </div>
                        <div class="board-col-cards">
                          ${col.rows.length
                            ? col.rows.map((task) => renderCard(task, task.id === activeId, inboxItems, (id) => selectBoardTask(ctx.id, id, true)))
                            : html`<div class="mono muted board-col-empty">none</div>`}
                        </div>
                      </section>`;
                    })}
                  </div>`
                : layout === "table"
                  ? html`<${BoardTable}
                      tasks=${shown}
                      groupBy=${groupBy}
                      selectedId=${activeId}
                      sortCol=${ui.tableSort.col}
                      sortDir=${ui.tableSort.dir}
                      onSort=${onTableSort}
                      onSelect=${(id) => selectBoardTask(ctx.id, id, true)}
                      helpers=${helpers} />`
                  : layout === "list"
                    ? html`<${BoardList}
                        tasks=${shown}
                        groupBy=${groupBy}
                        selectedId=${activeId}
                        onSelect=${(id) => selectBoardTask(ctx.id, id, true)}
                        helpers=${helpers} />`
                    : html`<${BoardTimeline}
                        tasks=${shown}
                        nowMs=${now}
                        selectedId=${activeId}
                        onSelect=${(id) => selectBoardTask(ctx.id, id, true)} />`}
      </div>
    </div>`;
  }
}

export function BoardFocus({ route, now }) {
  ensureBoardLiveHooks();
  boardLiveRevision.value;
  const ctx = resolveBoardContext(route);
  if (ctx.kind === "projects") {
    return html`<${ProjectsFocus} route=${route} />`;
  }

  const ui = boardUiState.value;
  const bvs = boardViewState.value;
  const inboxItems = inboxState.value.items;
  const savedView = bvs.slug === ctx.id ? (bvs.savedView ?? ctx.savedView) : ctx.savedView;
  const groupBy = ui.groupByBoard[ctx.id] ?? savedView?.group_by ?? "state";
  const sort = ui.sortByBoard[ctx.id] ?? "priority";
  const filters = ui.filtersByBoard[ctx.id] ?? [];
  const layout = resolveLayout(ctx.id, savedView, groupBy, ui.layoutByBoard[ctx.id] ?? "");
  const scoped = scopeTasksForContext(ctx);
  const shown = filterAndSortTasks(scoped, { filters, query: ui.query, sort, showDone: ui.showDone, layout });
  const activeId = shown.some((t) => t.id === ui.selectedByBoard[ctx.id])
    ? ui.selectedByBoard[ctx.id]
    : defaultSelection(shown);
  const task = shown.find((t) => t.id === activeId) ?? null;

  if (!task) {
    return html`<div class="board-peek-empty muted">No task matches these filters.</div>`;
  }

  const state = canonicalState(task);
  const sync = syncGlyph(task);
  const triageIds = Array.isArray(task.needs_you_ids) ? task.needs_you_ids : [];
  const triageItems = inboxItems.filter((item) => triageIds.includes(item.id) || item.task_id === task.id);
  const history = ui.history.id === task.id ? ui.history : null;
  const activityRows = history
    ? [
        ...history.comments.map((c) => ({ at: c.ts || c.created || "", who: c.author || "owner", text: c.body || "" })),
        ...history.events.map((e) => ({ at: e.at || "", who: e.actor || "system", text: e.kind || "event" })),
      ].sort((a, b) => (Date.parse(b.at) || 0) - (Date.parse(a.at) || 0))
    : [];

  const switchTab = (tabName) => {
    patchUi({ peekTab: tabName });
    if (tabName === "activity" && ui.history.id !== task.id) {
      fetchTaskHistory(task.id);
    }
  };

  return html`<div class="board-peek">
    <div class="board-peek-meta">
      <span class="mono">${task.id}</span>
      <span class="tp">${priorityLabel(task.priority)}</span>
      ${sync ? html`<span class="sync mono">${sync}</span>` : null}
      <span class="board-spacer"></span>
      <a class="board-open-btn primary" data-primary-candidate="" href=${taskHash(task.id)}>Open <kbd>↵</kbd></a>
    </div>
    <h2 class="board-peek-title">${task.title || task.id}</h2>
    <div class="board-peek-tabs" role="tablist" aria-label="Task peek tabs">
      <button type="button"
        role="tab"
        aria-selected=${ui.peekTab === "overview"}
        class=${`board-peek-tab${ui.peekTab === "overview" ? " sel" : ""}`}
        onClick=${() => switchTab("overview")}>
        Overview
      </button>
      <button type="button"
        role="tab"
        aria-selected=${ui.peekTab === "activity"}
        class=${`board-peek-tab${ui.peekTab === "activity" ? " sel" : ""}`}
        onClick=${() => switchTab("activity")}>
        Activity ${history?.loaded ? html`<span class="mono muted">${activityRows.length}</span>` : null}
      </button>
    </div>

    ${ui.peekTab === "activity"
      ? html`<div class="board-activity">
          ${!history || !history.loaded
            ? html`<${Loading} what="activity" />`
            : history.error
              ? html`<${Fault} what="Activity" fault=${history.error} retry=${() => fetchTaskHistory(task.id)} />`
              : activityRows.length
                ? activityRows.map((ev, idx) => html`<div key=${idx} class="board-ev">
                    <span class="board-ev-at mono muted">${formatStamp(ev.at)}</span>
                    <span class="board-ev-who">${ev.who}</span>
                    <span class="board-ev-txt">${ev.text}</span>
                  </div>`)
                : html`<div class="muted board-peek-note">No activity yet.</div>`}
        </div>`
      : html`<div class="board-overview">
          ${triageItems.map((item) => html`<a key=${item.id} class="board-triage-block" href=${`#triage/${encodeURIComponent(item.id)}`}>
            <div class="board-triage-meta">
              <span class="chip ny">${item.priority === 1 ? "◆ HIGH" : item.priority === 2 ? "◇ MED" : "LOW"}</span>
              <span class="mono muted">${String(item.kind || "").toUpperCase()}</span>
              <span class="mono muted">${item.id}</span>
              <span class="mono muted">${formatAge(item.raised, now)}</span>
            </div>
            <div class="board-triage-title">${item.title}</div>
          </a>`)}

          <div class="board-props">
            <div class="board-prop">
              <span class="board-prop-lbl">Internal</span>
              <div class="board-prop-val">
                ${renderStateChip(task, state)}
                <select class="board-inline-select"
                  aria-label="Change task state"
                  value=${state}
                  onChange=${(e) => patchBoardTask(task, { state: e.currentTarget.value }, ctx)}>
                  ${STATES.map((s) => html`<option key=${s} value=${s}>${STATE_NAMES[s]}</option>`)}
                </select>
              </div>
            </div>
            <div class="board-prop">
              <span class="board-prop-lbl">External</span>
              <span class="board-prop-val mono">${externalLabel(task)}</span>
            </div>
            <div class="board-prop">
              <span class="board-prop-lbl">Sync</span>
              <span class="board-prop-val mono">${taskSyncState(task)}${sync ? ` ${sync}` : ""}</span>
            </div>
            <div class="board-prop">
              <span class="board-prop-lbl">Project</span>
              <span class="board-prop-val mono">
                ${task.project
                  ? html`<a class="board-link" href=${`#board/${encodeURIComponent(task.project)}`}>${task.project}</a>`
                  : (task.repo || "—")}
              </span>
            </div>
            <div class="board-prop">
              <span class="board-prop-lbl">Priority</span>
              <div class="board-prop-val">
                <select class="board-inline-select"
                  aria-label="Change task priority"
                  value=${String(priorityRank(task.priority))}
                  onChange=${(e) => patchBoardTask(task, { priority: Number(e.currentTarget.value) }, ctx)}>
                  <option value="1">P1</option>
                  <option value="2">P2</option>
                  <option value="3">P3</option>
                </select>
              </div>
            </div>
            <div class="board-prop">
              <span class="board-prop-lbl">Line</span>
              <span class="board-prop-val mono">${task.line || "none"}</span>
            </div>
            <div class="board-prop">
              <span class="board-prop-lbl">Owner</span>
              <span class="board-prop-val mono">${ownerLabel(task.owner)}</span>
            </div>
            <div class="board-prop">
              <span class="board-prop-lbl">Source</span>
              <span class="board-prop-val mono">${sourceName(task)}${sourceKey(task) ? ` · ${sourceKey(task)}` : ""}</span>
            </div>
            <div class="board-prop">
              <span class="board-prop-lbl">Updated</span>
              <span class="board-prop-val mono">${formatStamp(task.updated) || "—"}</span>
            </div>
          </div>
        </div>`}
  </div>`;
}
