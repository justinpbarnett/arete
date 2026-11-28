import { h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { taskHash } from "./ui-data.js";
import { prepareWrite, sendWrite } from "./ui-write.js";
import { notice } from "./ui-notice.js";
import {
  STATES,
  STATE_LABELS,
  canonicalState,
  isFaultTask,
  ownerLabel,
  taskNeedsYou,
} from "./ui-board-projects.js";

const html = htm.bind(h);

export const LAYOUTS = ["list", "board", "table", "timeline"];
export const LAYOUT_LABELS = {
  list: "List",
  board: "Board",
  table: "Table",
  timeline: "Timeline",
};

export const GROUP_OPTIONS = [
  { id: "state", label: "State, as columns" },
  { id: "priority", label: "Priority" },
  { id: "line", label: "Line" },
  { id: "owner", label: "Owner" },
];

export const TABLE_COLUMNS = [
  { id: "select", label: "select", aria: "Select" },
  { id: "id", label: "ID" },
  { id: "title", label: "Title" },
  { id: "state", label: "State" },
  { id: "external", label: "External" },
  { id: "sync", label: "Sync" },
  { id: "source", label: "Source" },
  { id: "line", label: "Line" },
  { id: "owner", label: "Owner" },
  { id: "priority", label: "P" },
  { id: "updated", label: "Updated" },
];

const LAYOUT_PREFIX = "arete:board-layout:";
const FOUR_DAYS_MS = 4 * 86400 * 1000;

export function readViewerLayout(boardId) {
  try {
    const val = globalThis.localStorage?.getItem(LAYOUT_PREFIX + boardId);
    return LAYOUTS.includes(val) ? val : "";
  } catch {
    return "";
  }
}

export function writeViewerLayout(boardId, layout) {
  try {
    if (!layout) globalThis.localStorage?.removeItem(LAYOUT_PREFIX + boardId);
    else globalThis.localStorage?.setItem(LAYOUT_PREFIX + boardId, layout);
  } catch {
    return;
  }
}

export function nextLayout(current, delta = 1) {
  const idx = Math.max(0, LAYOUTS.indexOf(current));
  return LAYOUTS[(idx + delta + LAYOUTS.length) % LAYOUTS.length];
}

export function resolveLayout(boardId, savedView, groupBy, explicitLayout = "") {
  const saved = savedView?.layout && LAYOUTS.includes(savedView.layout) ? savedView.layout : "";
  const stored = readViewerLayout(boardId);
  let chosen = explicitLayout || stored || saved || "board";
  if (!LAYOUTS.includes(chosen)) chosen = "board";
  if (chosen === "board" && groupBy && groupBy !== "state") return "list";
  return chosen;
}

export async function saveBoardLayout(boardId, layout, savedView, onViewUpdated) {
  if (!LAYOUTS.includes(layout)) return;
  if (savedView && (layout === "board" || layout === "table")) {
    writeViewerLayout(boardId, "");
    try {
      const req = prepareWrite(`/api/v1/views/${encodeURIComponent(savedView.id)}`, { layout }, savedView, { method: "PATCH" });
      const res = await sendWrite(req);
      const updated = res?.data ?? { ...savedView, layout, version: (savedView.version ?? 1) + 1 };
      onViewUpdated?.(updated);
    } catch (err) {
      if (err?.conflict && err.current) onViewUpdated?.(err.current);
      else notice.value = `Failed: ${err?.message ?? err}`;
    }
    return;
  }
  writeViewerLayout(boardId, layout);
}

export function priorityLabel(priority) {
  if (priority === 1 || priority === "P1" || priority === "HIGH") return "P1";
  if (priority === 2 || priority === "P2" || priority === "MED") return "P2";
  if (priority === 3 || priority === "P3" || priority === "LOW") return "P3";
  return priority ? `P${priority}` : "P3";
}

export function priorityRank(priority) {
  const lbl = priorityLabel(priority);
  if (lbl === "P1") return 1;
  if (lbl === "P2") return 2;
  if (lbl === "P3") return 3;
  return 4;
}

export function groupBoardTasks(tasks, groupBy = "state") {
  if (groupBy === "state") {
    return STATES.map((s) => [STATE_LABELS[s], tasks.filter((t) => canonicalState(t) === s), s]).filter(([, rows]) => rows.length > 0);
  }
  if (groupBy === "priority") {
    const order = ["P1", "P2", "P3"];
    const keys = [...new Set([...order, ...tasks.map((t) => priorityLabel(t.priority))])];
    return keys
      .map((key) => [key, tasks.filter((t) => priorityLabel(t.priority) === key), key])
      .filter(([, rows]) => rows.length > 0);
  }
  const keyFn = groupBy === "owner"
    ? (t) => ownerLabel(t.owner)
    : (t) => String(t.line || "no line");
  const keys = [...new Set(tasks.map(keyFn))].sort((a, b) => a.localeCompare(b));
  return keys.map((key) => [key, tasks.filter((t) => keyFn(t) === key), key]);
}

export function sortByColumn(tasks, colId, dir, helpers) {
  const mult = dir === "desc" ? -1 : 1;
  const cmp = (a, b) => {
    switch (colId) {
      case "select":
        return (Number(taskNeedsYou(b)) - Number(taskNeedsYou(a))) * mult;
      case "id":
        return String(a.id).localeCompare(String(b.id)) * mult;
      case "title":
        return String(a.title || "").localeCompare(String(b.title || "")) * mult;
      case "state":
        return (STATES.indexOf(canonicalState(a)) - STATES.indexOf(canonicalState(b))) * mult;
      case "external":
        return helpers.externalLabel(a).localeCompare(helpers.externalLabel(b)) * mult;
      case "sync":
        return helpers.syncGlyph(a).localeCompare(helpers.syncGlyph(b)) * mult;
      case "source":
        return helpers.sourceName(a).localeCompare(helpers.sourceName(b)) * mult;
      case "line":
        return String(a.line || "").localeCompare(String(b.line || "")) * mult;
      case "owner":
        return ownerLabel(a.owner).localeCompare(ownerLabel(b.owner)) * mult;
      case "priority":
        return (priorityRank(a.priority) - priorityRank(b.priority)) * mult;
      case "updated":
        return ((Date.parse(a.updated) || 0) - (Date.parse(b.updated) || 0)) * mult;
      default:
        return 0;
    }
  };
  return [...tasks].sort((a, b) => cmp(a, b) || String(a.id).localeCompare(String(b.id)));
}

function formatDayTick(ms) {
  const d = new Date(ms);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${months[d.getMonth()]} ${d.getDate()}`;
}

export function timelineData(tasks, nowMs = Date.now()) {
  const endMs = Number.isFinite(nowMs) ? nowMs : Date.now();
  const startMs = endMs - FOUR_DAYS_MS;
  const span = Math.max(1, endMs - startMs);
  const toPct = (ms) => Math.max(0, Math.min(100, Math.round(((ms - startMs) / span) * 100)));

  const dayStart = new Date(startMs);
  dayStart.setHours(0, 0, 0, 0);
  const ticks = [];
  for (let t = dayStart.getTime(); t <= endMs; t += 86400 * 1000) {
    if (t < startMs) continue;
    const leftPct = Math.max(0, Math.min(96, toPct(t)));
    ticks.push({ ms: t, label: formatDayTick(t), leftPct, leftClass: `tl-l-${leftPct}` });
  }

  const ordered = [...tasks].sort((a, b) => (Date.parse(a.created) || 0) - (Date.parse(b.created) || 0) || String(a.id).localeCompare(String(b.id)));
  const bars = ordered.map((task) => {
    const state = canonicalState(task);
    const createdMs = Date.parse(task.created);
    const sMs = Number.isFinite(createdMs) ? Math.max(startMs, Math.min(endMs, createdMs)) : startMs;
    const doneMs = state === "done" ? Date.parse(task.updated) : endMs;
    const eMs = Number.isFinite(doneMs) ? Math.max(sMs, Math.min(endMs, doneMs)) : endMs;
    const leftPct = Math.max(0, Math.min(99, toPct(sMs)));
    const rightPct = Math.max(leftPct + 1, Math.min(100, toPct(eMs)));
    const widthPct = Math.max(1, Math.min(100 - leftPct, rightPct - leftPct));
    const tone = isFaultTask(task)
      ? "fault"
      : taskNeedsYou(task)
        ? "ny"
        : state === "done"
          ? "ok"
          : (state === "running" || state === "review")
            ? "active"
            : "idle";
    return {
      task,
      leftPct,
      widthPct,
      leftClass: `tl-l-${leftPct}`,
      widthClass: `tl-w-${widthPct}`,
      tone,
      startMs: sMs,
      endMs: eMs,
    };
  });
  return { startMs, endMs, ticks, bars };
}

export function DisplayMenu({ layout, open, onToggle, onPick }) {
  return html`<div class="board-display-wrap">
    <button type="button"
      class="board-display-btn"
      data-act="display"
      aria-haspopup="menu"
      aria-expanded=${open}
      onClick=${onToggle}>
      <span>Display: ${LAYOUT_LABELS[layout] ?? "Board"} ▾</span>
      <kbd>gt</kbd>
    </button>
    ${open ? html`<div class="board-display-pop" role="menu" aria-label="Display layout">
      <div class="board-display-row">
        <span class="board-display-lbl">Layout</span>
        <div class="board-display-seg" role="group" aria-label="Layout options">
          ${LAYOUTS.map((item) => html`<button key=${item}
            type="button"
            role="menuitemradio"
            aria-checked=${item === layout}
            class=${item === layout ? "on" : ""}
            data-act=${`layout-${item}`}
            onClick=${() => onPick(item)}>
            ${LAYOUT_LABELS[item]}
          </button>`)}
        </div>
      </div>
    </div>` : null}
  </div>`;
}

export function BoardTable({ tasks, groupBy, selectedId, sortCol, sortDir, onSort, onSelect, helpers }) {
  const sorted = sortCol ? sortByColumn(tasks, sortCol, sortDir, helpers) : tasks;
  const groups = groupBoardTasks(sorted, groupBy);
  return html`<div class="board-table-wrap">
    <table class="board-tbl board-tasks-tbl">
      <thead>
        <tr>
          ${TABLE_COLUMNS.map((col) => html`<th key=${col.id}
            class=${col.id === "updated" ? "num" : ""}
            aria-label=${col.aria ?? col.label}>
            <button type="button"
              class=${`board-th-btn${sortCol === col.id ? " sorted" : ""}`}
              onClick=${() => onSort(col.id)}>
              ${col.id === "select" ? "" : col.label}
              ${sortCol === col.id ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
            </button>
          </th>`)}
        </tr>
      </thead>
      <tbody>
        ${groups.map(([label, rows, groupKey]) => html`
          <tr key=${`g:${groupKey}`} class="board-tr-group">
            <td colspan="11">
              <span class=${`board-grp-lbl${groupKey === "needs-you" ? " ny" : ""}`}>${label}</span>
              <span class="board-grp-cnt mono muted">${rows.length}</span>
            </td>
          </tr>
          ${rows.map((task) => {
            const st = canonicalState(task);
            const selected = task.id === selectedId;
            return html`<tr key=${task.id}
              class=${`board-tr${selected ? " sel" : ""}`}
              data-row
              data-item=${task.id}
              data-open=${taskHash(task.id)}
              onClick=${() => onSelect(task.id)}
              onDblClick=${() => { location.hash = taskHash(task.id); }}>
              <td class="col-sel">${helpers.markNode(task)}</td>
              <td class="mono muted">${task.id}</td>
              <td class="col-title" title=${task.title}>${task.title || task.id}</td>
              <td>${helpers.stateChip(task, st)}</td>
              <td class="mono muted" title=${helpers.externalLabel(task)}>${helpers.externalLabel(task)}</td>
              <td class="mono">${helpers.syncGlyph(task) || html`<span class="faint">·</span>`}</td>
              <td class="mono">${helpers.sourceName(task)}</td>
              <td class="mono">${task.line || ""}</td>
              <td class="mono">${ownerLabel(task.owner)}</td>
              <td class="mono muted">${priorityLabel(task.priority)}</td>
              <td class="num muted">${helpers.age(task.updated)}</td>
            </tr>`;
          })}
        `)}
      </tbody>
    </table>
  </div>`;
}

export function BoardList({ tasks, groupBy, selectedId, onSelect, helpers }) {
  const groups = groupBoardTasks(tasks, groupBy);
  return html`<div class="board-list-wrap">
    ${groups.map(([label, rows, groupKey]) => html`<div key=${groupKey} class="board-list-group">
      <div class="board-list-grp-h">
        <span class=${`board-grp-lbl${groupKey === "needs-you" ? " ny" : ""}`}>${label}</span>
        <span class="board-grp-cnt mono muted">${rows.length}</span>
      </div>
      ${rows.map((task) => {
        const st = canonicalState(task);
        const selected = task.id === selectedId;
        return html`<div key=${task.id}
          class=${`board-list-row${selected ? " sel" : ""}`}
          data-row
          data-item=${task.id}
          data-open=${taskHash(task.id)}
          onClick=${() => onSelect(task.id)}
          onDblClick=${() => { location.hash = taskHash(task.id); }}>
          <span class="bl-mark">${helpers.markNode(task)}</span>
          <span class="bl-id mono muted">${task.id}</span>
          <span class="bl-pri">${priorityLabel(task.priority)}</span>
          <span class="bl-title" title=${task.title}>${task.title || task.id}</span>
          <span class="bl-state">${helpers.stateChip(task, st)}</span>
          <span class="bl-sync mono">${helpers.syncGlyph(task)}</span>
          <span class="bl-ext mono muted">${helpers.externalLabel(task)}</span>
          <span class="bl-line mono muted">${task.line || ""}</span>
          <span class="bl-owner mono muted">${ownerLabel(task.owner)}</span>
          <span class="bl-age mono muted">${helpers.age(task.updated)}</span>
        </div>`;
      })}
    </div>`)}
  </div>`;
}

export function BoardTimeline({ tasks, nowMs, selectedId, onSelect }) {
  const { ticks, bars } = timelineData(tasks, nowMs);
  return html`<div class="board-tl-wrap">
    <div class="board-tl-axis">
      ${ticks.map((tick) => html`<span key=${tick.ms} class=${`board-tl-tick ${tick.leftClass}`}>${tick.label}</span>`)}
      <span class="board-tl-now-lbl tl-l-100">now</span>
    </div>
    <div class="board-tl-rows">
      ${bars.map(({ task, leftClass, widthClass, tone }) => {
        const selected = task.id === selectedId;
        const doneLabel = canonicalState(task) === "done" ? task.updated : "now";
        return html`<div key=${task.id}
          class=${`board-tl-row${selected ? " sel" : ""}`}
          data-row
          data-item=${task.id}
          data-open=${taskHash(task.id)}
          onClick=${() => onSelect(task.id)}
          onDblClick=${() => { location.hash = taskHash(task.id); }}>
          <div class="board-tl-l">
            <span class="mono muted bl-id">${task.id}</span>
            <span class="board-tl-title" title=${task.title}>${task.title || task.id}</span>
          </div>
          <div class="board-tl-t">
            <div class="board-tl-now tl-l-100"></div>
            <div class=${`board-tl-bar ${tone} ${leftClass} ${widthClass}`}
              title=${`${task.id}: ${task.created || ""} to ${doneLabel}`}></div>
          </div>
        </div>`;
      })}
    </div>
  </div>`;
}
