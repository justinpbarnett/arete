import { signal, batch } from "./vendor-signals.js";
import { parse } from "./ui-router.js";
import { dismissMenu } from "./ui-menu.js";
import { paletteKey, paletteRouteChanged } from "./ui-palette.js";
import { notice } from "./ui-notice.js";
import { actions, perform, repeat, reasonBox, cancelReason, confirmReason } from "./ui-actions.js";
import { undo } from "./ui-undo.js";
export { notice };

export const screenKeys = [
  ["f", "floor", "Floor", "#floor"],
  ["t", "triage", "Triage", "#triage"],
  ["w", "my-work", "My work", "#board/open-work"],
  ["b", "board", "Board", "#board"],
  ["r", "runs", "Runs", "#run"],
  ["n", "intake", "Intake", "#intake"],
  ["o", "outbox", "Outbox", "#outbox"],
  ["p", "prompts", "Prompts", "#prompts"],
  ["a", "adws", "ADWs", "#adws"],
  ["v", "insights", "Insights", "#insights"],
  [",", "settings", "Settings", "#settings/health"],
];

export const sheetGroups = [
  ["Go to", screenKeys.map(([key, , label]) => [[key], label])],
  ["Move", [
    [["j", "k"], "next, previous item"], [["gg", "G"], "first, last item"],
    [["h", "l"], "pane left, right; board column; follow a wire"], [["Enter"], "open the selected item"],
    [["-"], "up one level"], [["[", "]"], "previous, next run"],
    [["gt", "gT"], "next, previous tab or display"], [["/"], "search the list; Esc closes, Esc again clears"],
    [["="], "fit the whole graph in view"], [["zz"], "center the selected node at 100%"],
  ]],
  ["Act", [
    [["i"], "type in the first box"], [["Cmd Enter"], "the one submit key"],
    [["1", "2", "3", "4"], "pick an option"], [["c"], "create an item, or a graph node"],
    [["s"], "snooze until tomorrow 09:00, or hold; again resumes"], [["⇧s"], "shelve, with a reason"],
    [["dd"], "reject, discard, stop a run, remove"], [["⇧r"], "retry"],
    [["m"], "approve and remember"], [["gx"], "open the link behind the item, or a node's prompt"],
    [["y"], "copy id and link"], [["u"], "undo"],
    [["."], "repeat on the next item"],
  ]],
  ["Anywhere", [
    [[":", "Cmd K"], "command palette"], [["?"], "this sheet"],
    [["Esc"], "leave insert, close; never changes screens"],
  ]],
  ["Palette only", [[[":"], "snooze 1 hour or to Monday, redirect, batch send, revert a prompt, merge, reply, show done, display"]]],
];

const panes = ["sidebar", "queue", "focus"];
const textTypes = ["", "text", "search", "url", "email", "tel", "password", "number"];
const tabSelector = '[role="tab"]:not([disabled]):not([aria-disabled="true"])';
const fixed = { "Enter": "open", "-": "up", "[": "previous", "]": "next", "/": "search", "i": "insert", "G": "last", "j": "down", "k": "up-row", "h": "left", "l": "right" };

export const mode = signal("NORMAL");
export const pane = signal("queue");
export const pending = signal("");
export const sheet = signal(false);
export const connection = signal("");
export const readRetry = signal(null);

const cursors = new Map();
export const selectedItem = (id) => cursors.get(id);
let listId = "";
let searched = null;
let lastTab = null;
let sheetOpener = null;

export function writable(el) {
  if (!el || el.disabled || el.readOnly) return false;
  if (el.tagName === "TEXTAREA") return true;
  if (el.tagName === "INPUT") return textTypes.includes(el.type);
  return el.isContentEditable === true;
}

const canonical = (name) => name === "auxiliary" ? "focus" : name;
const usable = (el) => !el.hidden && !el.disabled && el.getAttribute("aria-disabled") !== "true" && el.getClientRects().length > 0;
const itemId = (row) => row.dataset.item ?? row.dataset.row;
const listRows = (list) => [...list.querySelectorAll("[data-row]")].filter((row) => usable(row)
  || (list.dataset.list === "run-queue" && !row.hidden && !row.disabled && row.getAttribute("aria-disabled") !== "true"));
const paneElements = (name) => [...document.querySelectorAll("[data-pane]")].filter((el) => canonical(el.dataset.pane) === name);
const lists = (name) => paneElements(name).flatMap((el) => [...el.querySelectorAll("[data-list]"), ...(el.matches("[data-list]") ? [el] : [])]);

function paneOf(target) {
  const el = target?.closest?.("[data-pane]");
  return el ? canonical(el.dataset.pane) : null;
}

const narrowRun = () => globalThis.matchMedia?.("(width < 900px)").matches && parse(location.hash).screen === "run";

export function activatePane(name) {
  pane.value = name;
  marks();
  if (narrowRun()) {
    const target = paneElements(name)[0];
    const row = name !== "focus" ? target?.querySelector('[data-cursor], [aria-current="page"]') : null;
    (row ?? target)?.focus({ preventScroll: true });
  }
}

function focusedList() {
  const found = lists(pane.value);
  return found.find((el) => el.dataset.list === listId) ?? found[0] ?? null;
}

export function selectItem(id, item, name = "queue") {
  listId = id;
  if (item == null) cursors.delete(id);
  else cursors.set(id, item);
  pane.value = name;
  marks();
}

export function actionContext() {
  const list = focusedList();
  const row = list && cursorRow(list);
  return list?.uiContext?.(row && itemId(row));
}

function destructive() {
  const context = actionContext();
  if (context?.item) perform(actions[context.item.verb ?? "remove"], context);
}

function cursorRow(list) {
  const id = cursors.get(list.dataset.list);
  return listRows(list).find((row) => itemId(row) === id) ?? null;
}

function currentIndex(list) {
  const rows = listRows(list);
  const at = rows.indexOf(cursorRow(list));
  return at >= 0 ? at : rows.findIndex((row) => row.getAttribute("aria-current") === "page");
}

function place(list, row) {
  listId = list.dataset.list;
  cursors.set(listId, itemId(row));
  marks();
  row.scrollIntoView({ block: "nearest" });
}

function move(index, relative) {
  const list = focusedList();
  if (!list) return;
  const rows = listRows(list);
  if (!rows.length) return;
  const active = document.activeElement;
  if (active?.tagName === "BUTTON" && list.contains(active) && !active.closest("[data-row]")) active.blur();
  const base = relative ? currentIndex(list) + index : index < 0 ? rows.length - 1 : index;
  place(list, rows[Math.max(0, Math.min(rows.length - 1, base))]);
}

export function syncList(list) {
  const rows = listRows(list);
  if (!rows.some((row) => itemId(row) === cursors.get(list.dataset.list))) {
    if (rows.length) cursors.set(list.dataset.list, itemId(rows[0]));
    else cursors.delete(list.dataset.list);
  }
  marks();
}

export function marks() {
  const app = narrowRun() ? document.querySelector('.ui-app[data-screen="run"]') : null;
  if (app) {
    if (pane.value !== "sidebar") app.dataset.runPane = pane.value;
    else app.dataset.runPane ||= "queue";
    const active = document.activeElement?.closest?.('[data-pane="queue"], [data-pane="focus"]');
    if (active && active.dataset.pane !== app.dataset.runPane) {
      paneElements(app.dataset.runPane)[0]?.focus({ preventScroll: true });
    }
  }
  for (const el of document.querySelectorAll("[data-pane]")) {
    el.toggleAttribute("data-focus", el.dataset.pane !== "auxiliary" && el.dataset.pane === pane.value);
  }
  for (const list of document.querySelectorAll("[data-list]")) {
    const row = cursorRow(list);
    for (const other of list.querySelectorAll("[data-cursor]")) if (other !== row) other.removeAttribute("data-cursor");
    row?.setAttribute("data-cursor", "");
  }
}

function go(hash) {
  if (hash && location.hash !== hash) location.hash = hash;
}

function openRow(row) {
  const link = row.matches("a[href]") ? row : row.querySelector("a[href]");
  const target = row.dataset.open ?? (link ? link.getAttribute("href") : null);
  if (target?.startsWith("#")) go(target);
  else if (target === null) row.click();
  else link?.click();
  if (narrowRun() && row.closest?.('[data-list="run-queue"]')) activatePane("focus");
}

function open() {
  const list = focusedList();
  const row = list && cursorRow(list);
  if (row) openRow(row);
}

function up() {
  const list = focusedList();
  const scope = (list && cursorRow(list)) ?? paneElements(pane.value)[0];
  const marked = scope?.closest("[data-up]") ?? document.querySelector("[data-up]");
  const route = parse(location.hash);
  const parent = marked?.dataset.up ?? (route.screen === "task" ? "#board" : route.screen === "run" ? "#floor" : "");
  if (parent.startsWith("#")) go(parent);
}

function neighbour(delta) {
  if (parse(location.hash).screen !== "run") return;
  const list = lists("queue").find((el) => listRows(el).length);
  if (!list) return;
  const rows = listRows(list);
  const shown = rows.findIndex((row) => row.getAttribute("aria-current") === "page");
  const row = rows[Math.max(0, Math.min(rows.length - 1, (shown >= 0 ? shown : currentIndex(list)) + delta))];
  place(list, row);
  openRow(row);
}

function tab(delta) {
  if (narrowRun() && pane.value !== "focus") activatePane("focus");
  const tabs = [...document.querySelectorAll(tabSelector)].filter(usable);
  if (!tabs.length) return;
  const selected = tabs.findIndex((el) => el.getAttribute("aria-selected") === "true");
  const at = selected >= 0 ? selected : tabs.indexOf(lastTab);
  lastTab = tabs[((at < 0 ? (delta > 0 ? -1 : 0) : at) + delta + tabs.length) % tabs.length];
  lastTab.click();
}

function search() {
  const scopes = paneElements(pane.value);
  const field = scopes.flatMap((el) => [...el.querySelectorAll("[data-search]")]).find(writable)
    ?? [...document.querySelectorAll("[data-search]")].find(writable);
  if (field && narrowRun()) activatePane(paneOf(field));
  field?.focus();
}

function insert() {
  const list = focusedList();
  const scope = list && cursorRow(list);
  const box = scope && [...scope.querySelectorAll("textarea, input, [contenteditable]")].find((el) => writable(el) && usable(el));
  box?.focus();
}

function step(delta) {
  const available = panes.filter((name) => paneElements(name).length);
  const at = available.indexOf(pane.value);
  activatePane(available[Math.max(0, Math.min(available.length - 1, (at < 0 ? 0 : at) + delta))] ?? pane.value);
}

function closeSheet() {
  sheet.value = false;
  const back = sheetOpener;
  sheetOpener = null;
  if (back?.isConnected) back.focus();
}

export function toggleSheet() {
  if (sheet.value) return closeSheet();
  sheetOpener = document.activeElement;
  pending.value = "";
  sheet.value = true;
  document.querySelector(".key-sheet")?.focus();
}

function escape() {
  if (sheet.value) return closeSheet();
  if (pending.value) { pending.value = ""; return; }
  const field = focusedList()?.querySelector("[data-search]") ?? searched;
  if (field?.isConnected && field.value) {
    field.value = "";
    field.dispatchEvent(new Event("input", { bubbles: true }));
  }
  searched = null;
}

function chord(key) {
  const prefix = pending.value;
  pending.value = "";
  if (prefix === "g" && key === "g") { move(0, false); return true; }
  if (prefix === "g" && key === "t") { tab(1); return true; }
  if (prefix === "g" && key === "T") { tab(-1); return true; }
  if (prefix === "g" && key === "x") { actionContext()?.run?.open?.(); return true; }
  if (prefix === "d" && key === "d") { destructive(); return true; }
  return prefix === "z" && key === "z";
}

function normal(event) {
  const key = event.key;
  if (sheet.value) {
    if (key === "?" || key === "Escape") closeSheet();
    return true;
  }
  if (key === "Escape") { escape(); return true; }
  if (pending.value && chord(key)) return true;
  if (key === "?") { toggleSheet(); return true; }
  if (key === "u") { undo(); return true; }
  if (key === ".") { repeat(actionContext()); return true; }
  if (["s", "S", "R"].includes(key)) {
    const context = actionContext();
    const action = context?.run ? (key === "R" ? context.run.retry : null)
      : context?.item && (context.setAside ?? actions[key === "s" ? "snooze" : key === "S" ? "shelve" : "repair"]);
    if (action) { perform(action, context); return true; }
    if (key === "R" && readRetry.peek()) { readRetry.peek()(); return true; }
  }
  if (key === "c" && focusedList()?.uiCreate) { focusedList().uiCreate(); return true; }
  if (key === "g" || key === "d" || key === "z") { pending.value = key; return true; }
  const screen = screenKeys.find(([letter]) => letter === key);
  if (screen) { go(screen[3]); return true; }
  const action = fixed[key];
  if (action === "down") move(1, true);
  else if (action === "up-row") move(-1, true);
  else if (action === "last") move(-1, false);
  else if (action === "left") step(-1);
  else if (action === "right") step(1);
  else if (action === "open") open();
  else if (action === "up") up();
  else if (action === "previous") neighbour(-1);
  else if (action === "next") neighbour(1);
  else if (action === "search") search();
  else if (action === "insert") insert();
  else return false;
  return true;
}

function editing(event) {
  if (event.key === "Escape") {
    const field = event.target;
    searched = field.matches?.("[data-search]") ? field : null;
    field.blur();
    mode.value = "NORMAL";
    return true;
  }
  if (event.key === "Enter" && event.target.tagName === "INPUT") return true;
  return false;
}

function onKey(event) {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.target.closest?.("dialog.ui-feedback")) return;
  if (paletteKey(event, writable(event.target))) return;
  if (reasonBox.peek()) {
    if (event.key === "Escape") { cancelReason(); event.target.blur?.(); mode.value = "NORMAL"; event.preventDefault(); }
    else if (event.key === "Enter") {
      if (event.metaKey || event.ctrlKey) confirmReason().then(() => { if (!reasonBox.peek()) mode.value = "NORMAL"; });
      event.preventDefault();
    }
    return;
  }
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    const context = actionContext();
    if (context?.primary) perform(context.primary, context);
    event.preventDefault(); return;
  }
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "Escape" && !sheet.value && (dismissMenu() || focusedList()?.uiDismiss?.())) {
    if (writable(event.target)) event.target.blur();
    mode.value = "NORMAL";
    event.preventDefault();
    return;
  }
  if (event.key === "Enter" && event.target.closest?.("button")) return;
  if (event.key === "Tab" && sheet.value) return;
  const handled = mode.value === "INSERT" ? editing(event) : normal(event);
  if (handled) event.preventDefault();
}

function touch(event) {
  const name = paneOf(event.target);
  const row = event.target.closest?.("[data-row]");
  const list = row?.closest("[data-list]");
  batch(() => {
    if (name) pane.value = name;
    if (list && row) place(list, row);
  });
  marks();
  if (event.type === "focusin" && writable(event.target)) mode.value = "INSERT";
}

function release(event) {
  if (writable(event.target) && !writable(event.relatedTarget)) mode.value = "NORMAL";
}

export function startKeys() {
  document.addEventListener("keydown", onKey);
  document.addEventListener("focusin", touch);
  document.addEventListener("focusout", release);
  document.addEventListener("pointerdown", touch, true);
  document.addEventListener("click", touch, true);
  window.addEventListener("blur", () => { pending.value = ""; });
  const routed = () => {
    if (narrowRun()) activatePane(parse(location.hash).path[0] ? "focus" : "queue");
    else marks();
  };
  window.addEventListener("hashchange", () => { pending.value = ""; paletteRouteChanged(); routed(); });
  window.addEventListener("resize", marks);
  routed();
}

export function hints() {
  if (mode.value === "INSERT") return [[["Esc"], "leave"], [["Cmd Enter"], "submit"], [["Tab"], "next box"]];
  const list = focusedList();
  const row = list && cursorRow(list);
  const found = [[["j", "k"], "move"], [["gg", "G"], "first, last"], [["h", "l"], "pane"], [["Enter"], "open"]];
  if (row && [...row.querySelectorAll("textarea, input, [contenteditable]")].some(writable)) found.push([["i"], "type"]);
  if (document.querySelector("[data-search]")) found.push([["/"], "search"]);
  if (document.querySelector(tabSelector)) found.push([["gt", "gT"], "tab"]);
  const run = list?.uiContext ? actionContext()?.run : null;
  if (run) found.push([["[", "]"], "run"], [["dd"], "stop"], [["gx"], "open"]);
  else if (list?.uiContext) found.push([["dd"], "act"], [["u"], "undo"], [["."], "repeat"]);
  if (run?.retry || readRetry.value) found.push([["⇧r"], "retry"]);
  return found;
}
