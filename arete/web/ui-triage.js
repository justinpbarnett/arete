import { Component, h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { batch, effect, signal } from "./vendor-signals.js";
import * as data from "./ui-data.js";
import { Menu } from "./ui-menu.js";
import { webFetch } from "./ui-request.js";
import { prepareWrite, sendWrite } from "./ui-write.js";
import { failed, notice, staleWrite, succeeded } from "./ui-notice.js";
import { pushUndo } from "./ui-undo.js";
import { refreshKeybar, selectItem, syncList } from "./ui-keys.js";
import { onInboxEvent } from "./ui-live.js";
import { Empty, Fault, Loading } from "./ui-states.js";

const html = htm.bind(h);

export const TRIAGE_SCREEN_URL = "/api/v1/screens/triage";
export const INBOX_BASE_URL = "/api/v1/inbox";
export const BATCH_URL = "/api/v1/batch";

export const KIND_ORDER = [
  "continue",
  "choice",
  "stop",
  "outbox",
  "approval",
  "freeform",
  "fault",
  "conflict",
  "proposal",
];

export const KIND_LABELS = {
  continue: "Continue",
  choice: "Choice",
  stop: "Stop",
  outbox: "Outbound",
  approval: "Approval",
  freeform: "Freeform",
  fault: "Fault",
  conflict: "Conflict",
  proposal: "Proposal",
  alert: "Alert",
  candidate: "Candidate",
  workspace: "Workspace",
  evidence: "Evidence",
  config_invalid: "Config",
  "queue.dead": "Dead queue",
  watch: "Watch",
  drift: "Drift",
};

const kindRenderers = new Map();
const subjectRenderers = [];

export function registerKind(kind, renderer) {
  kindRenderers.set(kind, renderer);
}

export function registerSubjectRenderer(prefix, renderer) {
  const existing = subjectRenderers.findIndex(([p]) => p === prefix);
  if (existing >= 0) subjectRenderers[existing] = [prefix, renderer];
  else subjectRenderers.push([prefix, renderer]);
}

export function rendererFor(item) {
  if (!item) return null;
  const subject = String(item.subject || "");
  for (const [prefix, renderer] of subjectRenderers) {
    if (subject.startsWith(prefix)) return renderer;
  }
  return kindRenderers.get(item.kind) ?? null;
}

export const triageState = signal({
  open: [],
  parked: [],
  counts: { open: 0, snoozed: 0, shelved: 0, paused: 0, resolved: 0 },
  andon: null,
  loaded: false,
  error: null,
  selectedId: "",
  groupBy: "priority",
  sortBy: "waiting",
  kindFilter: [],
  query: "",
  parkedOpen: false,
  shelveOpen: false,
  reasonPrompt: null,
  busyId: "",
  inlineStatus: {},
});

export const triageAndonCount = signal(null);

const taskCache = new Map();
const taskRevision = signal(0);
const itemKeyHandlers = { current: null };
let lastAction = null;
let reloadTimer = null;
let activeAbort = null;
let inboxSubscribed = false;

export function numericId(id) {
  const match = /(\d+)$/.exec(String(id || ""));
  return match ? Number(match[1]) : 0;
}

export function isParked(item) {
  return item?.state === "snoozed" || item?.state === "shelved";
}

export function priorityLabel(priority) {
  if (priority === 1 || priority === "HIGH" || priority === "high") return "HIGH";
  if (priority === 2 || priority === "MED" || priority === "medium") return "MED";
  return "LOW";
}

export function priorityRank(priority) {
  if (priority === 1 || priority === "HIGH" || priority === "high") return 1;
  if (priority === 2 || priority === "MED" || priority === "medium") return 2;
  return 3;
}

export function priorityGlyph(item) {
  if (isParked(item)) return "■";
  const rank = priorityRank(item?.priority);
  if (rank === 1) return "◆";
  if (rank === 2) return "◇";
  return "○";
}

export function kindLabel(kind) {
  return KIND_LABELS[kind] ?? (kind ? String(kind).charAt(0).toUpperCase() + String(kind).slice(1) : "Item");
}

export function formatAge(iso, nowMs = Date.now()) {
  if (!iso) return "";
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "";
  const sec = Math.max(0, Math.floor((nowMs - ts) / 1000));
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.floor(min / 60);
  if (hr < 48) return `${hr}h`;
  return `${Math.floor(hr / 24)}d`;
}

export function formatStamp(iso) {
  if (!iso) return "";
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return String(iso);
  const d = new Date(ts);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function tomorrowNineIso(base = new Date()) {
  const d = new Date(base.getTime());
  d.setDate(d.getDate() + 1);
  d.setHours(9, 0, 0, 0);
  return d.toISOString();
}

export function nextMondayNineIso(base = new Date()) {
  const d = new Date(base.getTime());
  const day = d.getDay();
  const delta = day === 0 ? 1 : day === 1 ? 7 : 8 - day;
  d.setDate(d.getDate() + delta);
  d.setHours(9, 0, 0, 0);
  return d.toISOString();
}

export function plusHoursIso(hours, base = new Date()) {
  return new Date(base.getTime() + hours * 3600 * 1000).toISOString();
}

export function wakeLabel(item) {
  if (!item) return "";
  if (item.state === "snoozed") {
    if (item.snooze_until) return `until ${formatStamp(item.snooze_until)}`;
    if (item.snooze_watch) return "until changed";
    return "snoozed";
  }
  if (item.state === "shelved") {
    const parts = [];
    if (item.shelve_reason) parts.push(item.shelve_reason);
    if (item.shelve_until) parts.push(`returns ${formatStamp(item.shelve_until)}`);
    return parts.join(" · ") || "shelved";
  }
  return "";
}

export function computeAndon(openItems, serverAndon = null) {
  const items = Array.isArray(openItems) ? openItems : [];
  const high = items.filter((item) => priorityRank(item.priority) === 1).length;
  const med = items.filter((item) => priorityRank(item.priority) === 2).length;
  const low = items.filter((item) => priorityRank(item.priority) === 3).length;
  const standing = high + med + low;
  const count = high + med;
  const limit = Number.isFinite(serverAndon?.limit) ? serverAndon.limit : 10;
  const aim = Number.isFinite(serverAndon?.aim) ? serverAndon.aim : 5;
  let zone = serverAndon?.zone ?? "STABLE";
  if (!serverAndon || serverAndon.standing !== standing) {
    if (standing >= 2 * limit) zone = "OVERLOADED";
    else if (standing >= limit) zone = "REACTIVE";
    else if (standing < aim) zone = "ROBUST";
    else zone = "STABLE";
  }
  return { count, high, med, low, standing, limit, aim, zone, why: serverAndon?.why ?? "" };
}

export function formatHeadLine(andon) {
  if (!andon) return "Loading triage…";
  return `${andon.count} need you · ${andon.high} high · ${andon.med} med · ${andon.low} low apart · ${andon.standing} standing, limit ${andon.limit} · zone ${andon.zone}`;
}

export function sortItems(items, sortBy = "waiting") {
  const copy = [...items];
  const cmp = (a, b) => {
    const ta = Date.parse(a.raised || "") || 0;
    const tb = Date.parse(b.raised || "") || 0;
    if (ta !== tb) return ta - tb;
    return numericId(a.id) - numericId(b.id);
  };
  copy.sort(sortBy === "newest" ? (a, b) => cmp(b, a) : cmp);
  return copy;
}

export function matchesSearch(item, query) {
  const needle = String(query || "").trim().toLowerCase();
  if (!needle) return true;
  const hay = [
    item.id,
    item.title,
    item.kind,
    kindLabel(item.kind),
    item.line,
    item.task_id,
    item.run_id,
    item.subject,
    priorityLabel(item.priority),
    item.detail?.error,
    item.detail?.name,
    item.detail?.signature,
  ].filter(Boolean).join(" ").toLowerCase();
  return hay.includes(needle);
}

export function filterItems(items, { kindFilter = [], query = "" } = {}) {
  return items.filter((item) => {
    if (kindFilter.length && !kindFilter.includes(item.kind)) return false;
    return matchesSearch(item, query);
  });
}

export function groupItems(openItems, { groupBy = "priority", sortBy = "waiting", kindFilter = [], query = "" } = {}) {
  const visible = sortItems(filterItems(openItems, { kindFilter, query }), sortBy);
  if (groupBy === "kind") {
    const extraKinds = [...new Set(visible.map((item) => item.kind))].filter((k) => !KIND_ORDER.includes(k)).sort();
    return [...KIND_ORDER, ...extraKinds]
      .map((kind) => [kindLabel(kind), visible.filter((item) => item.kind === kind), kind])
      .filter(([, rows]) => rows.length > 0);
  }
  const high = visible.filter((item) => priorityRank(item.priority) === 1);
  const med = visible.filter((item) => priorityRank(item.priority) === 2);
  const low = visible.filter((item) => priorityRank(item.priority) === 3);
  return [
    ["◆ HIGH", high, "high"],
    ["◇ MED", med, "med"],
    ["○ LOW", low, "low"],
  ].filter(([, rows]) => rows.length > 0);
}

export function flatVisibleOpen(state) {
  return groupItems(state.open, {
    groupBy: state.groupBy,
    sortBy: state.sortBy,
    kindFilter: state.kindFilter,
    query: state.query,
  }).flatMap(([, rows]) => rows);
}

export function defaultSelectedId(openItems, parkedItems = []) {
  const highs = sortItems(openItems.filter((item) => priorityRank(item.priority) === 1), "waiting");
  if (highs.length) return highs[0].id;
  const open = sortItems(openItems, "waiting");
  if (open.length) return open[0].id;
  const parked = sortItems(parkedItems, "waiting");
  return parked[0]?.id ?? "";
}

export function nextAfterAction(visibleOpen, actedId, fallbackParked = []) {
  const idx = visibleOpen.findIndex((item) => item.id === actedId);
  if (idx >= 0) {
    const remaining = visibleOpen.filter((item) => item.id !== actedId);
    if (!remaining.length) return fallbackParked[0]?.id ?? "";
    return (remaining[idx] ?? remaining[0]).id;
  }
  const remaining = visibleOpen.filter((item) => item.id !== actedId);
  return remaining[0]?.id ?? fallbackParked[0]?.id ?? "";
}

export function resolveHashItem(route, openItems, parkedItems) {
  const all = [...openItems, ...parkedItems];
  const path = route?.path ?? [];
  if (!path.length || !path[0]) return null;
  if (path[0] === "ask" && path[1]) {
    const subject = `ask:${path[1]}`;
    return all.find((item) => item.subject === subject) ?? null;
  }
  const wanted = String(path[0]).toUpperCase();
  return all.find((item) => String(item.id).toUpperCase() === wanted
    || `TR-${path[0]}`.toUpperCase() === String(item.id).toUpperCase()) ?? null;
}

export function selectedTriageItem(state = triageState.value, route = null) {
  const all = [...state.open, ...state.parked];
  if (!all.length) return null;
  if (state.selectedId) {
    const found = all.find((item) => item.id === state.selectedId);
    if (found) return found;
  }
  const fromHash = route ? resolveHashItem(route, state.open, state.parked) : null;
  if (fromHash) return fromHash;
  const defId = defaultSelectedId(state.open, state.parked);
  return all.find((item) => item.id === defId) ?? all[0] ?? null;
}

function syncHashFor(id) {
  const target = data.triageHash(id);
  try {
    if (globalThis.location && globalThis.location.hash !== target && globalThis.location.hash.startsWith("#triage")) {
      globalThis.history?.replaceState?.(null, "", target);
    }
  } catch {
    return;
  }
}

export function setSelectedTriage(id, { updateHash = true } = {}) {
  const cur = triageState.peek();
  const isNowParked = cur.parked.some((item) => item.id === id);
  const nextParkedOpen = cur.parkedOpen || isNowParked;
  if (cur.selectedId !== id || cur.parkedOpen !== nextParkedOpen || cur.shelveOpen || cur.reasonPrompt) {
    triageState.value = {
      ...cur,
      selectedId: id,
      parkedOpen: nextParkedOpen,
      shelveOpen: false,
      reasonPrompt: null,
    };
  }
  if (updateHash && id) syncHashFor(id);
  refreshKeybar();
}

function updateAndonSignal(open, serverAndon) {
  const andon = computeAndon(open, serverAndon);
  triageAndonCount.value = andon.count;
  return andon;
}

async function readJson(url, signal) {
  const response = await webFetch(url, { cache: "no-store", signal });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    return { error: { status: response.status, message: body?.detail ?? body?.error ?? `HTTP ${response.status}` } };
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return { error: { status: response.status, message: "Malformed response" } };
  }
  return { data: body };
}

export async function loadTriage({ preserveSelection = true, route = null } = {}) {
  activeAbort?.abort();
  const controller = activeAbort = new AbortController();
  const { signal: abortSignal } = controller;
  try {
    const [screenRes, openRes, parkedRes] = await Promise.all([
      readJson(data.triageScreen(), abortSignal),
      readJson(data.inboxOpen(), abortSignal),
      readJson(data.inboxParked(), abortSignal),
    ]);
    if (abortSignal.aborted) return;
    if (screenRes.error && openRes.error) {
      const prev = triageState.peek();
      triageState.value = { ...prev, loaded: true, error: screenRes.error };
      return;
    }
    const open = Array.isArray(openRes.data?.items)
      ? openRes.data.items
      : Array.isArray(screenRes.data?.items)
        ? screenRes.data.items
        : [];
    const parked = Array.isArray(parkedRes.data?.items) ? parkedRes.data.items : [];
    const serverAndon = screenRes.data?.andon ?? null;
    const andon = updateAndonSignal(open, serverAndon);
    const counts = screenRes.data?.counts ?? {
      open: open.length,
      snoozed: parked.filter((i) => i.state === "snoozed").length,
      shelved: parked.filter((i) => i.state === "shelved").length,
      paused: 0,
      resolved: 0,
    };
    const prev = triageState.peek();
    const all = [...open, ...parked];
    const fromHash = route ? resolveHashItem(route, open, parked) : null;
    let selectedId = fromHash?.id ?? prev.selectedId;
    if (!preserveSelection || !selectedId || !all.some((item) => item.id === selectedId)) {
      selectedId = fromHash?.id ?? defaultSelectedId(open, parked);
    }
    const parkedSelected = parked.some((item) => item.id === selectedId);
    triageState.value = {
      ...prev,
      open,
      parked,
      counts,
      andon,
      loaded: true,
      error: null,
      selectedId,
      parkedOpen: prev.parkedOpen || parkedSelected,
    };
    refreshKeybar();
  } catch (error) {
    if (abortSignal.aborted) return;
    const prev = triageState.peek();
    triageState.value = { ...prev, loaded: true, error: { status: null, message: error?.message || "Network error" } };
  }
}

export async function refreshViaBatch() {
  try {
    const res = await sendWrite(prepareWrite(BATCH_URL, {
      requests: [
        { operation: "triage_screen.get", input: {} },
        { operation: "inbox.list", input: { state: "snoozed,shelved" } },
      ],
    }));
    const [screenRes, parkedRes] = res?.results ?? [];
    if (!screenRes?.items) return loadTriage({ preserveSelection: true });
    const open = screenRes.items;
    const parked = Array.isArray(parkedRes?.items) ? parkedRes.items : triageState.peek().parked;
    const andon = updateAndonSignal(open, screenRes.andon ?? null);
    const prev = triageState.peek();
    const all = [...open, ...parked];
    const selectedId = all.some((item) => item.id === prev.selectedId)
      ? prev.selectedId
      : defaultSelectedId(open, parked);
    triageState.value = {
      ...prev,
      open,
      parked,
      counts: screenRes.counts ?? prev.counts,
      andon,
      loaded: true,
      error: null,
      selectedId,
      parkedOpen: prev.parkedOpen || parked.some((item) => item.id === selectedId),
    };
    refreshKeybar();
  } catch {
    return loadTriage({ preserveSelection: true });
  }
}

export function handleInboxEvent(event) {
  if (!event) return;
  if (event.kind === "reset" || event.kind?.startsWith("inbox.")) {
    clearTimeout(reloadTimer);
    reloadTimer = setTimeout(() => {
      reloadTimer = null;
      loadTriage({ preserveSelection: true });
    }, 60);
  }
}

function ensureInboxStream() {
  if (inboxSubscribed) return;
  inboxSubscribed = true;
  onInboxEvent(handleInboxEvent);
}

async function ensureTaskMeta(taskId) {
  if (!taskId || taskCache.has(taskId)) return;
  taskCache.set(taskId, { loading: true });
  try {
    const res = await readJson(`/api/v1/tasks/${encodeURIComponent(taskId)}`);
    if (res.data?.task) {
      taskCache.set(taskId, { task: res.data.task });
    } else {
      taskCache.set(taskId, { task: null });
    }
  } catch {
    taskCache.set(taskId, { task: null });
  }
  taskRevision.value += 1;
}

function applyItemTransition(item, updatedItem, { removed = false, toParked = false, toOpen = false } = {}) {
  const cur = triageState.peek();
  const visibleBefore = flatVisibleOpen(cur);
  let open = cur.open.filter((x) => x.id !== item.id);
  let parked = cur.parked.filter((x) => x.id !== item.id);
  if (toParked && updatedItem) {
    parked = [...parked, updatedItem];
  } else if (toOpen && updatedItem) {
    open = [...open, updatedItem];
  } else if (!removed && updatedItem) {
    if (isParked(updatedItem)) parked = [...parked, updatedItem];
    else if (updatedItem.state === "open") open = [...open, updatedItem];
  }
  const nextSelected = removed || toParked
    ? nextAfterAction(visibleBefore, item.id, parked)
    : toOpen
      ? updatedItem.id
      : cur.selectedId;
  const andon = updateAndonSignal(open, cur.andon);
  const counts = {
    ...cur.counts,
    open: open.length,
    snoozed: parked.filter((x) => x.state === "snoozed").length,
    shelved: parked.filter((x) => x.state === "shelved").length,
  };
  batch(() => {
    triageState.value = {
      ...cur,
      open,
      parked,
      counts,
      andon,
      selectedId: nextSelected,
      shelveOpen: false,
      reasonPrompt: null,
      busyId: "",
      parkedOpen: cur.parkedOpen || parked.some((x) => x.id === nextSelected),
    };
  });
  if (nextSelected) syncHashFor(nextSelected);
  refreshKeybar();
}

export async function reopenItem(itemId, baseVersion, label = `Reopened ${itemId}`) {
  const req = prepareWrite(data.inboxReopen(itemId), {}, { version: baseVersion });
  const res = await sendWrite(req);
  const reopened = res.item;
  if (reopened) {
    applyItemTransition({ id: itemId }, reopened, { toOpen: true });
    setSelectedTriage(reopened.id);
  } else {
    await loadTriage({ preserveSelection: false });
  }
  notice.value = label;
  return reopened;
}

function registerInboxUndo(item, updatedItem, label) {
  if (!updatedItem?.version) return;
  const targetId = item.id;
  const undoVersion = updatedItem.version;
  pushUndo({
    label,
    inverse: async () => {
      await reopenItem(targetId, undoVersion, `Undid: ${label}`);
    },
    restore: () => {
      setSelectedTriage(targetId);
    },
  });
}

function handleWriteError(item, error) {
  if (error?.conflict && error.current?.id) {
    const cur = triageState.peek();
    const updated = error.current;
    const open = cur.open.map((x) => x.id === updated.id ? updated : x);
    const parked = cur.parked.map((x) => x.id === updated.id ? updated : x);
    triageState.value = { ...cur, open, parked, busyId: "" };
    staleWrite.value = "This item changed. Review the latest version before trying again.";
  } else {
    const cur = triageState.peek();
    triageState.value = { ...cur, busyId: "" };
  }
  failed(error);
}

export async function snoozeItem(item, until = tomorrowNineIso()) {
  if (!item) return false;
  triageState.value = { ...triageState.peek(), busyId: item.id };
  try {
    const req = prepareWrite(data.inboxSnooze(item.id), { until }, { version: item.version });
    const res = await sendWrite(req);
    const updated = res.item ?? { ...item, state: "snoozed", snooze_until: until, version: item.version + 1 };
    const label = `Snoozed ${item.id} until tomorrow 09:00`;
    applyItemTransition(item, updated, { toParked: true });
    registerInboxUndo(item, updated, label);
    lastAction = { kind: "snooze", until };
    succeeded(label, true);
    return true;
  } catch (error) {
    handleWriteError(item, error);
    return false;
  }
}

export async function wakeItem(item, nowIso = new Date().toISOString()) {
  if (!item) return false;
  triageState.value = { ...triageState.peek(), busyId: item.id };
  try {
    const req = prepareWrite(data.inboxSnooze(item.id), { until: nowIso }, { version: item.version });
    const res = await sendWrite(req);
    const updated = { ...(res.item ?? item), state: "open", snooze_until: "", shelve_until: "", shelve_reason: "" };
    const label = `Woke ${item.id}`;
    applyItemTransition(item, updated, { toOpen: true });
    lastAction = { kind: "wake" };
    succeeded(label, false);
    return true;
  } catch (error) {
    handleWriteError(item, error);
    return false;
  }
}

export async function shelveItem(item, reason, until = nextMondayNineIso()) {
  if (!item) return false;
  const cleanReason = String(reason || "").trim();
  if (!cleanReason) {
    notice.value = "Shelving requires a reason";
    return false;
  }
  triageState.value = { ...triageState.peek(), busyId: item.id };
  try {
    const body = until ? { reason: cleanReason, until } : { reason: cleanReason };
    const req = prepareWrite(data.inboxShelve(item.id), body, { version: item.version });
    const res = await sendWrite(req);
    const updated = res.item ?? { ...item, state: "shelved", shelve_reason: cleanReason, shelve_until: until, version: item.version + 1 };
    const label = `Shelved ${item.id}`;
    applyItemTransition(item, updated, { toParked: true });
    registerInboxUndo(item, updated, label);
    lastAction = { kind: "shelve", reason: cleanReason, until };
    succeeded(label, true);
    return true;
  } catch (error) {
    handleWriteError(item, error);
    return false;
  }
}

export async function resolveItem(item, verb, payload = {}, { label = "", undoable = false, keepOpen = false } = {}) {
  if (!item) return null;
  triageState.value = { ...triageState.peek(), busyId: item.id };
  try {
    const req = prepareWrite(data.inboxResolve(item.id), { verb, ...payload }, { version: item.version });
    const res = await sendWrite(req);
    const updated = res.item;
    const msg = label || `${verb.charAt(0).toUpperCase() + verb.slice(1)} ${item.id}`;
    const stillOpen = keepOpen || (updated && updated.state !== "resolved");
    if (stillOpen && updated) {
      applyItemTransition(item, updated, { removed: false });
    } else {
      applyItemTransition(item, updated, { removed: true });
    }
    if (undoable && updated) registerInboxUndo(item, updated, msg);
    lastAction = { kind: "resolve", verb, payload, label: msg };
    succeeded(msg, undoable);
    return res;
  } catch (error) {
    handleWriteError(item, error);
    return null;
  }
}

export function copyItemId(item) {
  if (!item) return;
  const href = globalThis.location ? `${globalThis.location.origin}${globalThis.location.pathname}${data.triageHash(item.id)}` : data.triageHash(item.id);
  const text = `${item.id} ${href}`;
  try {
    globalThis.navigator?.clipboard?.writeText?.(text)?.catch?.(() => {});
  } catch {
    notice.value = `Copied ${item.id}`;
    return;
  }
  notice.value = `Copied ${item.id}`;
}

export function setInlineStatus(itemId, text, error = false) {
  const cur = triageState.peek();
  triageState.value = { ...cur, inlineStatus: { ...cur.inlineStatus, [itemId]: { text, error } } };
}

export function openReasonDrawer(title, placeholder, onConfirm, { allowEmpty = true } = {}) {
  const cur = triageState.peek();
  triageState.value = { ...cur, shelveOpen: false, reasonPrompt: { title, placeholder, text: "", allowEmpty, onConfirm } };
}

export function openShelveDrawer() {
  const cur = triageState.peek();
  triageState.value = { ...cur, reasonPrompt: null, shelveOpen: true };
}

export function closeDrawers() {
  const cur = triageState.peek();
  if (!cur.shelveOpen && !cur.reasonPrompt) return false;
  triageState.value = { ...cur, shelveOpen: false, reasonPrompt: null };
  return true;
}

export async function repeatLastTriageAction(item) {
  if (!lastAction || !item) {
    notice.value = "No compatible action to repeat";
    return false;
  }
  if (lastAction.kind === "snooze" && !isParked(item)) return snoozeItem(item, lastAction.until);
  if (lastAction.kind === "wake" && isParked(item)) return wakeItem(item);
  if (lastAction.kind === "shelve") return shelveItem(item, lastAction.reason, lastAction.until);
  if (lastAction.kind === "resolve") {
    const verbs = Array.isArray(item.verbs) ? item.verbs : [];
    if (!verbs.includes(lastAction.verb)) {
      notice.value = `Cannot repeat ${lastAction.verb} on ${item.id}`;
      return false;
    }
    return Boolean(await resolveItem(item, lastAction.verb, lastAction.payload));
  }
  notice.value = "No compatible action to repeat";
  return false;
}

function buildTriageContext(item) {
  const handlers = itemKeyHandlers.current?.itemId === item?.id ? itemKeyHandlers.current : null;
  const parked = isParked(item);
  const hints = [
    [["j", "k"], "move"],
    [["/"], "search"],
  ];
  if (handlers?.hints) {
    for (const entry of handlers.hints) hints.push(entry);
  }
  hints.push([["s"], parked ? "wake" : "snooze"]);
  hints.push([["⇧s"], "shelve"]);
  hints.push([["y"], "copy"]);
  hints.push([["u"], "undo"]);
  hints.push([[","], "settings"]);

  return {
    triage: true,
    item,
    hints,
    submit: () => {
      const cur = triageState.peek();
      if (cur.shelveOpen) {
        globalThis.document?.querySelector?.(".tr-drawer.ops-form")?.requestSubmit?.();
        return;
      }
      if (cur.reasonPrompt) {
        const rp = cur.reasonPrompt;
        if (!rp.allowEmpty && !rp.text.trim()) {
          notice.value = "Enter a reason first";
          return;
        }
        const text = rp.text.trim();
        closeDrawers();
        rp.onConfirm(text);
        return;
      }
      handlers?.submit?.();
    },
    pick: handlers?.pick ?? null,
    remember: handlers?.remember ?? null,
    retry: handlers?.retry ?? null,
    destructive: handlers?.destructive ?? null,
    openLink: handlers?.openLink ?? null,
    snooze: item ? () => (parked ? wakeItem(item) : snoozeItem(item)) : null,
    shelve: item ? () => openShelveDrawer() : null,
    copy: item ? () => copyItemId(item) : null,
    repeat: item ? () => repeatLastTriageAction(item) : null,
  };
}

export class TriageSubtitle extends Component {
  state = { rev: 0 };
  componentDidMount() {
    this.unsub = effect(() => {
      triageState.value;
      this.setState({ rev: this.state.rev + 1 });
    });
  }
  componentWillUnmount() { this.unsub?.(); }
  render() {
    const st = triageState.value;
    const andon = st.andon ?? (st.loaded ? computeAndon(st.open) : null);
    return html`<span class="tr-sub">${formatHeadLine(andon)}</span>`;
  }
}

export class TriageAndon extends Component {
  state = { rev: 0 };
  componentDidMount() {
    this.unsub = effect(() => {
      triageState.value;
      this.setState({ rev: this.state.rev + 1 });
    });
  }
  componentWillUnmount() { this.unsub?.(); }
  render() {
    const st = triageState.value;
    const andon = st.andon ?? (st.loaded ? computeAndon(st.open) : null);
    if (!andon) return null;
    const zoneClass = `tr-zone tr-zone-${String(andon.zone || "stable").toLowerCase()}`;
    return html`<span class=${`tr-andon${andon.count > 0 ? "" : " tr-andon-clear"}`} aria-label="Triage andon">
      ${andon.count > 0 ? `◆ ${andon.count} need you` : "✓ 0 need you"}
    </span><span class=${zoneClass}>${andon.zone}</span>`;
  }
}

function menuChoice(label, checked, action, count = null) {
  return html`<button type="button" role="menuitemcheckbox" aria-checked=${checked ? "true" : "false"} onClick=${action}>
    <span>${checked ? "✓" : "　"} ${label}</span>${count == null ? null : html`<b>${count}</b>`}
  </button>`;
}

export class TriageQueue extends Component {
  state = { rev: 0 };
  scrollEl = null;
  anchorBefore = null;

  componentDidMount() {
    ensureInboxStream();
    this.unsub = effect(() => {
      triageState.value;
      this.captureAnchor();
      this.setState({ rev: this.state.rev + 1 });
    });
    if (!triageState.peek().loaded) {
      loadTriage({ preserveSelection: false, route: this.props.route });
    } else {
      const fromHash = resolveHashItem(this.props.route, triageState.peek().open, triageState.peek().parked);
      if (fromHash && fromHash.id !== triageState.peek().selectedId) {
        setSelectedTriage(fromHash.id, { updateHash: false });
      }
      loadTriage({ preserveSelection: true, route: this.props.route });
    }
    this.attachListHooks();
  }

  componentDidUpdate(prevProps) {
    const prevPath = prevProps.route?.path?.join("/") ?? "";
    const nextPath = this.props.route?.path?.join("/") ?? "";
    if (prevPath !== nextPath) {
      const cur = triageState.peek();
      const fromHash = resolveHashItem(this.props.route, cur.open, cur.parked);
      if (fromHash && fromHash.id !== cur.selectedId) {
        setSelectedTriage(fromHash.id, { updateHash: false });
      }
    }
    this.attachListHooks();
    this.restoreAnchor();
  }

  componentWillUnmount() {
    this.unsub?.();
  }

  captureAnchor() {
    const el = this.scrollEl;
    if (!el) return;
    const selectedId = triageState.peek().selectedId;
    const row = selectedId ? el.querySelector(`[data-item="${selectedId}"]`) : null;
    this.anchorBefore = {
      scrollTop: el.scrollTop,
      selectedId,
      offsetTop: row ? row.offsetTop : null,
    };
  }

  restoreAnchor() {
    const el = this.scrollEl;
    const before = this.anchorBefore;
    this.anchorBefore = null;
    if (!el || !before) return;
    const curId = triageState.peek().selectedId;
    if (before.selectedId && before.selectedId === curId && before.offsetTop != null) {
      const row = el.querySelector(`[data-item="${curId}"]`);
      if (row) {
        const delta = row.offsetTop - before.offsetTop;
        if (delta !== 0) el.scrollTop = Math.max(0, before.scrollTop + delta);
        return;
      }
    }
    el.scrollTop = before.scrollTop;
  }

  attachListHooks() {
    if (!this.base) return;
    this.base.uiSelect = (id) => {
      if (id && id !== triageState.peek().selectedId) setSelectedTriage(id);
    };
    this.base.uiContext = () => buildTriageContext(selectedTriageItem(triageState.peek(), this.props.route));
    this.base.uiDismiss = () => closeDrawers();
    const sel = selectedTriageItem(triageState.peek(), this.props.route);
    if (sel) selectItem("triage", sel.id, "queue");
    syncList(this.base);
  }

  setGroup = (groupBy) => {
    triageState.value = { ...triageState.peek(), groupBy };
  };

  setSort = (sortBy) => {
    triageState.value = { ...triageState.peek(), sortBy };
  };

  toggleKind = (kind) => {
    const cur = triageState.peek();
    const next = cur.kindFilter.includes(kind)
      ? cur.kindFilter.filter((k) => k !== kind)
      : [...cur.kindFilter, kind];
    triageState.value = { ...cur, kindFilter: next };
  };

  setQuery = (query) => {
    triageState.value = { ...triageState.peek(), query };
  };

  toggleParked = () => {
    const cur = triageState.peek();
    triageState.value = { ...cur, parkedOpen: !cur.parkedOpen };
  };

  render({ route, now, retry }) {
    const st = triageState.value;
    const current = selectedTriageItem(st, route);
    const groups = groupItems(st.open, {
      groupBy: st.groupBy,
      sortBy: st.sortBy,
      kindFilter: st.kindFilter,
      query: st.query,
    });
    const visibleOpenCount = groups.reduce((acc, [, rows]) => acc + rows.length, 0);
    const filteredParked = sortItems(filterItems(st.parked, { kindFilter: st.kindFilter, query: st.query }), st.sortBy);
    const parkedExpanded = st.parkedOpen || (current && isParked(current));

    const kindCounts = new Map();
    for (const item of st.open) {
      if (matchesSearch(item, st.query)) {
        kindCounts.set(item.kind, (kindCounts.get(item.kind) ?? 0) + 1);
      }
    }
    const extraKinds = [...kindCounts.keys()].filter((k) => !KIND_ORDER.includes(k)).sort();
    const filterKinds = [...KIND_ORDER, ...extraKinds];

    const renderRow = (item) => {
      const parked = isParked(item);
      const selected = current?.id === item.id;
      const rank = priorityRank(item.priority);
      const glyphClass = parked ? "tr-pg tr-pg-low"
        : item.kind === "fault" ? "tr-pg tr-pg-fault"
          : rank === 1 ? "tr-pg tr-pg-high"
            : rank === 2 ? "tr-pg tr-pg-med" : "tr-pg tr-pg-low";
      const rightMeta = parked ? wakeLabel(item) : (item.line || "");
      return html`<a key=${item.id} class=${`tr-row${parked ? " parked" : ""}${selected ? " sel" : ""}`}
        data-row="" data-item=${item.id} href=${data.triageHash(item.id)}
        aria-current=${selected ? "page" : undefined}
        title=${`${item.id} · ${item.title}`}
        onClick=${(event) => {
          if (!event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
            setSelectedTriage(item.id);
          }
        }}>
        <span class=${glyphClass} aria-hidden="true">${priorityGlyph(item)}</span>
        <span class="tr-rk">${kindLabel(item.kind)}</span>
        <span class="tr-rt"><span class="tr-rid">${item.id}</span>${item.title}</span>
        <span class="tr-rl">${rightMeta}</span>
        <span class="tr-ra">${formatAge(item.raised, now)}</span>
      </a>`;
    };

    return html`<div class="ui-list tr-queue-list" data-list="triage">
      <div class="ui-list-header">
        <${Menu} label=${`Group: ${st.groupBy === "kind" ? "Kind" : "Priority"}`} title="Group">
          ${(close) => [
            menuChoice("Priority", st.groupBy === "priority", () => { this.setGroup("priority"); close(); }),
            menuChoice("Kind", st.groupBy === "kind", () => { this.setGroup("kind"); close(); }),
          ]}
        <//>
        <${Menu} label="Filter" title="Filter by kind">
          ${() => html`<div>
            <div class="ui-menu-label">Kind</div>
            ${filterKinds.map((kind) => menuChoice(
              kindLabel(kind),
              st.kindFilter.includes(kind),
              () => this.toggleKind(kind),
              kindCounts.get(kind) ?? 0,
            ))}
          </div>`}
        <//>
        ${st.kindFilter.length ? html`<div class="ui-list-chips">
          ${st.kindFilter.map((kind) => html`<button key=${kind} type="button" class="ui-list-chip"
            title=${`Remove ${kindLabel(kind)}`} aria-label=${`Remove ${kindLabel(kind)}`}
            onClick=${() => this.toggleKind(kind)}><span>${kindLabel(kind)}</span><span aria-hidden="true">×</span></button>`)}
        </div>` : null}
        <input type="search" data-search="" aria-label="Search triage" placeholder="/ search"
          value=${st.query} spellcheck="false" autoComplete="off"
          onInput=${(event) => this.setQuery(event.currentTarget.value)} />
        <span class="ui-list-count" title=${`${visibleOpenCount} of ${st.open.length}`}>${visibleOpenCount}/${st.open.length}</span>
        <${Menu} label="⇅" title=${`Sort: ${st.sortBy === "newest" ? "Newest first" : "Waiting longest"}`}>
          ${(close) => [
            menuChoice("Waiting longest", st.sortBy === "waiting", () => { this.setSort("waiting"); close(); }),
            menuChoice("Newest first", st.sortBy === "newest", () => { this.setSort("newest"); close(); }),
          ]}
        <//>
      </div>
      <div class="ui-list-scroll" ref=${(el) => { this.scrollEl = el; }}>
        ${st.error ? html`<${Fault} what="Triage" fault=${st.error} retry=${() => { loadTriage({ preserveSelection: true }); retry?.(); }} />` : null}
        ${!st.loaded ? html`<${Loading} what="triage" />` : null}
        ${st.loaded && !groups.length && !filteredParked.length
          ? html`<${Empty} what=${st.open.length || st.parked.length ? "No matching items. Clear search or remove a filter." : "Nothing needs you"} />`
          : null}
        ${groups.map(([label, rows, key]) => html`<div key=${key}>
          <div class="tr-grp"><span>${label}</span><span class="tr-grp-n">${rows.length}</span></div>
          ${rows.map(renderRow)}
        </div>`)}
        ${st.loaded ? html`<div>
          <button type="button" class="tr-grp tr-grp-btn" aria-expanded=${parkedExpanded ? "true" : "false"}
            onClick=${this.toggleParked}>
            <span>${parkedExpanded ? "▾" : "▸"} ■ Snoozed and shelved</span>
            <span class="tr-grp-n">${filteredParked.length}</span>
          </button>
          ${parkedExpanded ? filteredParked.map(renderRow) : null}
        </div>` : null}
      </div>
    </div>`;
  }
}

class ShelveDrawer extends Component {
  state = { reason: "", preset: "monday", customUntil: "" };
  componentDidMount() {
    this.input?.focus();
  }
  untilIso() {
    if (this.state.preset === "tomorrow") return tomorrowNineIso();
    if (this.state.preset === "monday") return nextMondayNineIso();
    if (this.state.preset === "week") return plusHoursIso(24 * 7);
    if (this.state.preset === "custom" && this.state.customUntil.trim()) {
      const parsed = Date.parse(this.state.customUntil.trim());
      if (Number.isFinite(parsed)) return new Date(parsed).toISOString();
    }
    return nextMondayNineIso();
  }
  submit = async (event) => {
    event?.preventDefault();
    if (!this.state.reason.trim()) {
      notice.value = "Shelving requires a reason";
      this.input?.focus();
      return;
    }
    await shelveItem(this.props.item, this.state.reason, this.untilIso());
  };
  render({ item, busy }, { reason, preset, customUntil }) {
    return html`<form class="tr-drawer ops-form" onSubmit=${this.submit} aria-label="Shelve item">
      <div class="tr-lbl">Shelve ${item.id}</div>
      <div class="tr-drawer-row">
        <input ref=${(el) => { this.input = el; }} class="tr-inp tr-drawer-input" type="text"
          placeholder="Reason for shelving (required)" value=${reason} disabled=${busy}
          onInput=${(e) => this.setState({ reason: e.currentTarget.value })} />
        <select class="tr-drawer-select" aria-label="Return time" value=${preset} disabled=${busy}
          onChange=${(e) => this.setState({ preset: e.currentTarget.value })}>
          <option value="tomorrow">Tomorrow 09:00</option>
          <option value="monday">Monday 09:00</option>
          <option value="week">In 7 days</option>
          <option value="custom">Custom ISO date</option>
        </select>
        ${preset === "custom" ? html`<input class="tr-inp" type="text" placeholder="2026-12-01T09:00:00Z"
          value=${customUntil} onInput=${(e) => this.setState({ customUntil: e.currentTarget.value })} />` : null}
      </div>
      <div class="tr-acts">
        <button type="submit" class="tr-btn tr-btn-primary" disabled=${busy}>Shelve <kbd class="tr-cap">Cmd ↵</kbd></button>
        <button type="button" class="tr-btn tr-btn-ghost" disabled=${busy} onClick=${closeDrawers}>Cancel <kbd class="tr-cap">Esc</kbd></button>
      </div>
    </form>`;
  }
}

class ReasonDrawer extends Component {
  componentDidMount() {
    this.input?.focus();
  }
  submit = (event) => {
    event?.preventDefault();
    const rp = triageState.peek().reasonPrompt;
    if (!rp) return;
    if (!rp.allowEmpty && !rp.text.trim()) {
      notice.value = "Enter a reason first";
      return;
    }
    const text = rp.text.trim();
    closeDrawers();
    rp.onConfirm(text);
  };
  render({ prompt, busy }) {
    return html`<form class="tr-drawer ops-form" onSubmit=${this.submit} aria-label=${prompt.title}>
      <div class="tr-lbl">${prompt.title}</div>
      <input ref=${(el) => { this.input = el; }} class="tr-inp" type="text"
        placeholder=${prompt.placeholder || "Reason (optional)"} value=${prompt.text} disabled=${busy}
        onInput=${(e) => {
          const cur = triageState.peek();
          if (cur.reasonPrompt) triageState.value = { ...cur, reasonPrompt: { ...cur.reasonPrompt, text: e.currentTarget.value } };
        }} />
      <div class="tr-acts">
        <button type="submit" class="tr-btn tr-btn-primary" disabled=${busy}>Confirm <kbd class="tr-cap">Cmd ↵</kbd></button>
        <button type="button" class="tr-btn tr-btn-ghost" disabled=${busy} onClick=${closeDrawers}>Cancel <kbd class="tr-cap">Esc</kbd></button>
      </div>
    </form>`;
  }
}

function syncGlyphFor(task) {
  const sync = task?.sync_state || task?.sync?.state || "";
  if (sync === "conflict") return html`<span class="tr-sync tr-sync-ny">↕ conflict</span>`;
  if (sync === "ahead" || sync === "dirty") return html`<span class="tr-sync">↑ local ahead</span>`;
  if (sync === "synced" || sync === "clean") return html`<span class="tr-sync">⇄ in sync</span>`;
  return html`<span class="tr-sync">○ local</span>`;
}

function GenericItemBody({ item, busy, helpers, registerItemKeys }) {
  const verbs = Array.isArray(item.verbs) ? item.verbs : [];
  const primary = verbs[0] || "";
  const secondary = verbs[1] || "";
  const summary = item.detail?.summary || item.detail?.why || item.detail?.detail || "";
  registerItemKeys({
    itemId: item.id,
    submit: primary ? () => helpers.resolve(primary, {}) : null,
    destructive: secondary ? () => helpers.resolve(secondary, {}) : null,
    hints: [
      ...(primary ? [[["Cmd Enter"], primary]] : []),
      ...(secondary ? [[["dd"], secondary]] : []),
    ],
  });
  return html`
    ${summary ? html`<div class="tr-sec"><div class="tr-para">${summary}</div></div>` : null}
    ${verbs.length ? html`<div class="tr-acts">
      ${verbs.map((verb, index) => html`<button key=${verb} type="button"
        class=${`tr-btn${index === 0 ? " tr-btn-primary" : ""}`} disabled=${busy}
        onClick=${() => helpers.resolve(verb, {})}>${verb.replace(/_/g, " ")}</button>`)}
    </div>` : null}
  `;
}

export class TriageFocus extends Component {
  state = { rev: 0 };

  componentDidMount() {
    this.unsub = effect(() => {
      triageState.value;
      taskRevision.value;
      this.setState({ rev: this.state.rev + 1 });
    });
  }

  componentWillUnmount() {
    this.unsub?.();
    itemKeyHandlers.current = null;
  }

  render({ route, now }) {
    const st = triageState.value;
    if (!st.loaded) return html`<${Loading} what="item" />`;
    const item = selectedTriageItem(st, route);
    if (!item) return html`<${Empty} what="Nothing waiting in triage" />`;

    if (item.task_id && !taskCache.has(item.task_id)) {
      ensureTaskMeta(item.task_id);
    }
    const taskMeta = item.task_id ? taskCache.get(item.task_id)?.task : null;
    const step = item.detail?.step || item.detail?.node || "";
    const parked = isParked(item);
    const busy = st.busyId === item.id;
    const rank = priorityRank(item.priority);
    const chipClass = rank === 1 ? "tr-chip tr-chip-high" : rank === 2 ? "tr-chip tr-chip-med" : "tr-chip tr-chip-low";
    const statusMsg = st.inlineStatus[item.id];

    itemKeyHandlers.current = null;
    const registerItemKeys = (handlers) => {
      itemKeyHandlers.current = handlers;
    };

    const helpers = {
      resolve: (verb, payload, opts) => resolveItem(item, verb, payload, opts),
      refresh: () => loadTriage({ preserveSelection: true }),
      advance: () => applyItemTransition(item, null, { removed: true }),
      setNotice: (msg) => { notice.value = msg; },
      succeeded,
      failed,
      openReason: openReasonDrawer,
      setInlineStatus: (text, err = false) => setInlineStatus(item.id, text, err),
      openHash: (hash) => {
        if (hash && globalThis.location) globalThis.location.hash = hash;
      },
      formatAge: (iso) => formatAge(iso, now),
      formatStamp,
    };

    const BodyRenderer = rendererFor(item) ?? GenericItemBody;
    const extKey = taskMeta?.external_key || taskMeta?.ref_key || item.detail?.key || "";
    const taskState = taskMeta?.state || item.detail?.task_state || "";

    return html`<article class="tr-detail" key=${item.id}>
      <header>
        <div class="tr-dmeta">
          <span class=${chipClass}>${priorityGlyph(item)} ${priorityLabel(item.priority)}</span>
          <span class="tr-lbl">${kindLabel(item.kind)}</span>
          <span>${item.id}</span>
          <span>age ${formatAge(item.raised, now)}</span>
          ${item.line ? html`<span>line ${item.line}</span>` : null}
          ${item.task_id ? html`<a class="tr-link" href=${data.taskHash(item.task_id)}>${item.task_id}</a>` : null}
          ${item.run_id ? html`<a class="tr-link" href=${data.runHash(item.run_id)}>run ${item.run_id}${step ? ` · ${step}` : ""}</a>` : null}
        </div>
        <h2 class="tr-dtitle">${item.title}</h2>
        ${(taskMeta || extKey || taskState) ? html`<div class="tr-task-row">
          ${taskState ? html`<span>${taskState}</span>` : null}
          <span class="tr-vs" aria-hidden="true">⇄</span>
          <span>${extKey || "local only"}</span>
          ${syncGlyphFor(taskMeta)}
          ${taskMeta?.title ? html`<span>${taskMeta.title}</span>` : null}
        </div>` : null}
      </header>

      ${parked ? html`<div class="tr-panel tr-pad tr-parked-banner" role="status">
        <span class="tr-st tr-st-paused">■ ${item.state === "snoozed" ? "SNOOZED" : "SHELVED"}</span>
        <span>${wakeLabel(item)}</span>
      </div>` : null}

      <${BodyRenderer} key=${`${item.id}:${item.version}`} item=${item} busy=${busy} helpers=${helpers}
        registerItemKeys=${registerItemKeys} now=${now} />

      ${statusMsg ? html`<p class=${`tr-status${statusMsg.error ? " tr-status-err" : ""}`} role="status">${statusMsg.text}</p>` : null}
      ${st.shelveOpen ? html`<${ShelveDrawer} item=${item} busy=${busy} />` : null}
      ${st.reasonPrompt ? html`<${ReasonDrawer} prompt=${st.reasonPrompt} busy=${busy} />` : null}

      <div class="tr-ctl">
        <span class="tr-lbl">Item</span>
        ${parked
          ? html`<button type="button" class="tr-btn" disabled=${busy} onClick=${() => wakeItem(item)}>Wake now <kbd class="tr-cap">s</kbd></button>`
          : html`<button type="button" class="tr-btn" disabled=${busy} onClick=${() => snoozeItem(item)}>Snooze until tomorrow 09:00 <kbd class="tr-cap">s</kbd></button>`}
        <button type="button" class="tr-btn" disabled=${busy} onClick=${openShelveDrawer}>Shelve <kbd class="tr-cap">⇧s</kbd></button>
        <button type="button" class="tr-btn tr-btn-ghost" onClick=${() => copyItemId(item)}>Copy id <kbd class="tr-cap">y</kbd></button>
        <span class="tr-sp"></span>
        <span class="tr-created">created ${formatStamp(item.raised)}</span>
      </div>
    </article>`;
  }
}
