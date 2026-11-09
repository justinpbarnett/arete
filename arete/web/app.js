"use strict";

const VIEWS = {
  live: { label: "Live", sub: "What runs now, and what waits for you." },
  runs: { label: "Runs", sub: "Every run, the running ones first, with the one you open beside the list." },
  sessions: { label: "Sessions", sub: "Agents you talk to, on the device and model you pick." },
  loops: { label: "Loops", sub: "The jobs serve fires on a schedule, from a source, or from a webhook." },
  triage: { label: "Triage", sub: "Every open ticket, grouped by what it needs before it can be built." },
  outcomes: { label: "Outcomes", sub: "What shipped, what went without you, and what you paid." },
  health: { label: "Health", sub: "Alerts and failures first, then serve, the devices, the checks and the loops." },
  settings: { label: "Settings", sub: "How many runs go at once, and the model for each role." },
  usage: { label: "Usage", sub: "Which views you open, how you reach them, and what you press." },
};
const ITEM_LABELS = { run: "Run", session: "Session", loop: "Loop", ticket: "Ticket" };
const DETAIL_VIEW = { run: "runs", session: "sessions", loop: "loops", ticket: "triage" };
const OLD_HASHES = { launch: "#sessions", fleet: "#health", trends: "#outcomes", chat: "#live" };
const GO_KEYS = { l: "live", r: "runs", s: "sessions", o: "loops", t: "triage", m: "outcomes", h: "health", e: "settings", u: "usage" };
const WAYS_IN = {
  start: "Opening the dashboard", sidebar: "The sidebar", link: "A link or a row on a page", palette: "Find",
  key: "G then a letter", back: "Back or Forward", close: "Closing an open item", step: "Previous or Next",
  return: "Coming back to the tab",
};
const ACTION_WORDS = {
  approve: "Approve or Try again", reject: "Reject", stop: "Stop a run", "send-ready": "Send a ready ticket",
  "run-anyway": "Run a skipped ticket anyway", done: "Mark done", "file-draft": "File a drafted issue from Live",
  "drop-draft": "Drop a drafted issue from Live", "open-console": "Open the console from Live",
  jump: "Jump to a section", close: "Close an open item", previous: "Previous", next: "Next",
  "filter-status": "Runs by status", "filter-kind": "Runs by kind", "filter-repo": "Runs by repo", "filter-loop": "Runs by loop",
  "runs-more": "Show more runs", "launch-toggle": "New session", launch: "Launch a session", "session-send": "Send to a session",
  "loop-new-toggle": "New loop", "draft-loop": "Draft a loop", start: "Start a loop", try: "Try a loop", switch: "Turn a loop on or off",
  "serve-install": "Install or restart serve", "filter-groups": "Triage group", "triage-refresh": "Refresh triage",
  research: "Research a ticket", "ticket-send": "Send a ticket to the pipeline", probe: "Probe a device", "checks-run": "Run the checks",
  "settings-save": "Save the settings", "filter-window": "Usage window", "console-open": "Open the console",
  "chat-send": "Send in the console", "chat-new": "New chat", "draft-file": "File a drafted issue from the console",
  "draft-drop": "Drop a drafted issue from the console", find: "Find", "go-key": "G then a letter",
};
const USAGE_WINDOWS = ["7", "30", "90"];
const DEFAULT_USAGE_WINDOW = "30";
const LIVE_MS = 5000;
const SLOW_MS = 30000;
const CHAT_BUSY_MS = 3000;
const PAGE_SIZE = 25;
const FIND_FRESH_MS = 30000;
const GO_WAIT_MS = 1500;
const FEED_KEEP_LINES = 2000;
const PALETTE_MOST = 40;
const DAY_MS = 24 * 60 * 60 * 1000;
const FIRST_MESSAGE_CHARS = 160;
const PHONE = matchMedia("(pointer: coarse) and (max-width: 1040px)");
const COARSE = matchMedia("(pointer: coarse)");
const SVG = "http://www.w3.org/2000/svg";
const URL_IN_TEXT = /(https:\/\/[^\s<>"')\]]*[^\s<>"')\].,;:!?])/g;
const RUN_LIMITS = [["max-parallel", "max_parallel"], ["max-parallel-per-repo", "max_parallel_per_repo"]];
const HELD = Symbol("held");
const MARKS = "[data-focus], h2, h3";
const RUN_STATE = { done: "ok", pr: "ok", merged: "ok", noop: "ok", skipped: "ok", "dry-run": "ok", failed: "fault", interrupted: "fault", "needs-info": "caution", stopped: "caution" };

let route = null;
const lastQuery = {};
let timer = 0;
let refreshCount = 0;
let pendingWay = "";
let restoreAfterDraw = null;
let focusAfterDraw = "";
let pendingDetailFocus = false;
let shownNeighbors = [null, null];
let feedOf = { id: "", after: 0, lines: [] };
let outcomesData = null;
let settingsData = null;
let chatSeen = "";
let chatLength = -1;
let chatBusy = false;
let chatTimer = 0;
let chatCount = 0;
let consoleReturn = null;
let findData = null;
let findAt = 0;
let findError = "";
let paletteReturn = null;
let paletteShown = [];
let paletteActive = 0;
let keysReturn = null;
let goPending = 0;
let forceTriageDraw = false;
let launchKey = "";
let loopNewKey = "";
let remoteDevices = [];
let sessionSending = false;
let stint = null;
let mouseAt = null;
const lastGood = new Map();
const openQuestions = new Set();
const drafts = new Map();
const talkDrafts = new Map();
const sendSaid = new Map();
const sending = new Set();
const lastSaid = new Map();
const handlersOf = new WeakMap();
const TAB = [...crypto.getRandomValues(new Uint8Array(8))].map((b) => b.toString(16).padStart(2, "0")).join("");

function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else if (key === "on") listen(node, value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
  return node;
}

function svg(tag, attrs = {}, text) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

// A redraw that changes nothing keeps the shown node and hands it the fresh handlers,
// so a handler must reach its node through the event, never through a closure.
function listen(node, handlers) {
  handlersOf.set(node, handlers);
  for (const event of Object.keys(handlers)) node.addEventListener(event, (e) => handlersOf.get(node)[event](e));
}

function asNode(value) {
  return value instanceof Node ? value : document.createTextNode(String(value));
}

function elementsIn(nodes, selector = "*") {
  return nodes.flatMap((node) => (node instanceof Element ? [...(node.matches(selector) ? [node] : []), ...node.querySelectorAll(selector)] : []));
}

function tickLiveAgos(times) {
  for (const time of times) {
    const text = ago(time.dateTime);
    if (time.textContent !== text) time.textContent = text;
  }
}

function listenedEvents(node) {
  return Object.keys(handlersOf.get(node) || {}).join();
}

function sameLiveState(shownNode, freshNode) {
  return shownNode.value === freshNode.value && shownNode.checked === freshNode.checked
    && listenedEvents(shownNode) === listenedEvents(freshNode);
}

function keepShownIfUnchanged(shown, fresh) {
  if (shown.length !== fresh.length || shown.some((node, i) => !node.isEqualNode(fresh[i]))) return false;
  const shownElements = elementsIn(shown);
  const freshElements = elementsIn(fresh);
  if (!shownElements.every((node, i) => sameLiveState(node, freshElements[i]))) return false;
  shownElements.forEach((node, i) => {
    if (handlersOf.has(freshElements[i])) handlersOf.set(node, handlersOf.get(freshElements[i]));
  });
  return true;
}

function placeKey(node) {
  return node.dataset.focus || node.textContent;
}

class Marks {
  constructor(nodes) {
    this.nodes = elementsIn(nodes, MARKS);
    const seen = new Map();
    this.ids = this.nodes.map((node) => {
      const key = placeKey(node);
      seen.set(key, (seen.get(key) ?? -1) + 1);
      return `${key}#${seen.get(key)}`;
    });
  }

  idOf(node) {
    return this.ids[this.nodes.indexOf(node)];
  }

  nodeOf(id) {
    return this.nodes[this.ids.indexOf(id)];
  }
}

function idsKeptInOrder(before, after) {
  let start = 0;
  while (start < before.length && start < after.length && before[start] === after[start]) start++;
  let beforeEnd = before.length;
  let afterEnd = after.length;
  while (beforeEnd > start && afterEnd > start && before[beforeEnd - 1] === after[afterEnd - 1]) {
    beforeEnd--;
    afterEnd--;
  }
  const kept = new Set([...before.slice(0, start), ...before.slice(beforeEnd)]);
  const changedBefore = before.slice(start, beforeEnd);
  const changedAfter = after.slice(start, afterEnd);
  const width = changedAfter.length + 1;
  const longestFrom = new Uint32Array((changedBefore.length + 1) * width);
  for (let i = changedBefore.length - 1; i >= 0; i--) {
    for (let j = changedAfter.length - 1; j >= 0; j--) {
      longestFrom[i * width + j] = changedBefore[i] === changedAfter[j]
        ? longestFrom[(i + 1) * width + j + 1] + 1
        : Math.max(longestFrom[(i + 1) * width + j], longestFrom[i * width + j + 1]);
    }
  }
  for (let i = 0, j = 0; i < changedBefore.length && j < changedAfter.length;) {
    if (changedBefore[i] === changedAfter[j]) {
      kept.add(changedBefore[i]);
      i++;
      j++;
    } else if (longestFrom[(i + 1) * width + j] >= longestFrom[i * width + j + 1]) i++;
    else j++;
  }
  return kept;
}

function markHolding(node) {
  const element = node?.nodeType === Node.TEXT_NODE ? node.parentElement : node;
  return element?.closest?.(MARKS) ?? null;
}

function marksInUse() {
  const selection = getSelection();
  return [document.activeElement, selection.isCollapsed ? null : selection.anchorNode].map(markHolding).filter(Boolean);
}

function scrollingPane(node) {
  const pane = node.closest(".detail");
  return pane && getComputedStyle(pane).overflowY === "auto" ? pane : null;
}

function placeIn(box, marks) {
  const pane = scrollingPane(box);
  const section = box.closest("main > section");
  if (!section) return { pane: null, x: scrollX, y: scrollY, anchors: [] };
  const scope = pane ?? section;
  const panes = pane ? [] : [...section.querySelectorAll(".detail")].filter((p) => getComputedStyle(p).overflowY === "auto");
  const { top: shownTop, bottom: shownBottom } = pane ? pane.getBoundingClientRect() : { top: 0, bottom: innerHeight };
  const y = pane ? pane.scrollTop : scrollY;
  const laidOut = [...scope.querySelectorAll(MARKS)]
    .filter((node) => !panes.some((p) => p.contains(node)) && node.checkVisibility())
    .map((node) => {
      const { top, bottom } = node.getBoundingClientRect();
      return { node, top, bottom };
    });
  const inUse = marksInUse();
  const hovered = mouseAt && document.elementFromPoint(mouseAt.x, mouseAt.y);
  const hovering = Boolean(hovered && scope.contains(hovered) && !panes.some((p) => p.contains(hovered)));
  const fromMouse = ({ top, bottom }) => Math.abs((top + bottom) / 2 - mouseAt.y);
  const inView = laidOut.filter(({ top, bottom }) => bottom > shownTop && top < shownBottom)
    .sort(hovering ? (a, b) => fromMouse(a) - fromMouse(b) : (a, b) => inUse.includes(b.node) - inUse.includes(a.node));
  const anchors = !y && !hovering ? [] : inView.length ? inView : laidOut.filter(({ bottom }) => bottom <= shownTop).slice(-1);
  return { pane, x: scrollX, y, anchors: anchors.map(({ node, top }) => ({ node, id: box.contains(node) ? marks.idOf(node) : null, top })) };
}

function followAnchor(anchor, freshMarks, keptIds) {
  const node = anchor.id ? freshMarks.nodeOf(anchor.id) : anchor.node.isConnected ? anchor.node : null;
  if (!node?.checkVisibility()) return null;
  return { drift: node.getBoundingClientRect().top - anchor.top, stayed: !anchor.id || keptIds.has(anchor.id) };
}

function keepPlace(place, shownMarks, freshMarks) {
  const keptIds = idsKeptInOrder(shownMarks.ids, freshMarks.ids);
  const followed = place.anchors.map((anchor) => followAnchor(anchor, freshMarks, keptIds)).filter(Boolean);
  const anchor = followed.find(({ stayed }) => stayed) ?? followed[0];
  if (place.pane) {
    const y = anchor ? place.pane.scrollTop + anchor.drift : place.y;
    if (place.pane.scrollTop !== y) place.pane.scrollTop = y;
    return;
  }
  const y = anchor ? scrollY + anchor.drift : place.y;
  if (scrollX !== place.x || scrollY !== y) scrollTo(place.x, y);
}

function textPoint(box, marks, node, offset) {
  const point = document.createRange();
  point.setStart(node, offset);
  const mark = marks.nodes.findLast((m) => point.comparePoint(m, 0) <= 0);
  const before = document.createRange();
  before.setStart(mark ?? box, 0);
  before.setEnd(node, offset);
  return { id: mark ? marks.idOf(mark) : null, chars: before.toString().length };
}

function textPointNode(box, marks, point) {
  const from = point.id ? marks.nodeOf(point.id) : box;
  if (!from) return null;
  const texts = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
  texts.currentNode = from;
  let left = point.chars;
  for (let text = texts.nextNode(); text; text = texts.nextNode()) {
    if (left <= text.length) return [text, left];
    left -= text.length;
  }
  return null;
}

function selectionIn(box, marks) {
  const selection = getSelection();
  if (selection.isCollapsed || !box.contains(selection.anchorNode) || !box.contains(selection.focusNode)) return null;
  return {
    text: selection.toString(),
    anchor: textPoint(box, marks, selection.anchorNode, selection.anchorOffset),
    focus: textPoint(box, marks, selection.focusNode, selection.focusOffset),
  };
}

function keepSelection(box, kept, marks) {
  const anchor = kept && textPointNode(box, marks, kept.anchor);
  const focus = kept && textPointNode(box, marks, kept.focus);
  if (!anchor || !focus) return;
  const selection = getSelection();
  selection.setBaseAndExtent(...anchor, ...focus);
  if (selection.toString() !== kept.text) selection.removeAllRanges();
}

function caretIn(field) {
  try {
    return field?.matches("input, textarea") && field.selectionStart !== null
      ? [field.selectionStart, field.selectionEnd, field.selectionDirection] : null;
  } catch {
    return null;
  }
}

function keepCaret(field, caret) {
  try {
    if (caret && field?.matches("input, textarea")) field.setSelectionRange(...caret);
  } catch {
    // a field type without a caret, such as a number, keeps whatever the browser gave it
  }
}

function scrollersByPlace(box) {
  const found = new Map();
  let under = "";
  for (const node of box.querySelectorAll("h2, h3, summary, .wide, .stack, pre")) {
    if (!node.matches(".wide, .stack, pre")) {
      under = placeKey(node);
      continue;
    }
    let place = `${under}|${node.className}`;
    while (found.has(place)) place += "+";
    found.set(place, node);
  }
  return found;
}

function fill(id, ...nodes) {
  const box = document.getElementById(id);
  const fresh = nodes.flat(Infinity).filter(Boolean).map(asNode);
  const shown = [...box.childNodes];
  tickLiveAgos(elementsIn([...shown, ...fresh], "time.ago"));
  if (keepShownIfUnchanged(shown, fresh)) return;
  const shownMarks = new Marks(shown);
  const place = placeIn(box, shownMarks);
  const focusedId = shownMarks.idOf(document.activeElement);
  const caret = focusedId ? caretIn(document.activeElement) : null;
  const selected = selectionIn(box, shownMarks);
  const scrolled = [...scrollersByPlace(box)].filter(([, node]) => node.scrollLeft || node.scrollTop)
    .map(([key, node]) => [key, node.scrollLeft, node.scrollTop]);
  box.append(...fresh);
  const freshMarks = new Marks(fresh);
  // Focus moves to the copy while the old element is still there, so a keyboard focus ring carries over.
  const refocused = freshMarks.nodeOf(focusedId);
  refocused?.focus({ preventScroll: true });
  keepCaret(refocused, caret);
  for (const node of shown) node.remove();
  const rebuilt = scrolled.length ? scrollersByPlace(box) : new Map();
  for (const [key, left, top] of scrolled) rebuilt.get(key)?.scrollTo(left, top);
  keepSelection(box, selected, freshMarks);
  keepPlace(place, shownMarks, freshMarks);
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function heading(n, title) {
  return el("h2", { "data-jump": title, tabindex: "-1" }, el("span", { class: "n", text: pad(n) }), title);
}

function stateChip(state) {
  const word = { ok: "OK", caution: "Caution", fault: "Fault" }[state];
  return word ? el("span", { class: `state ${state}`, text: word }) : null;
}

function sayParts(state, sentence) {
  const chip = stateChip(state);
  const words = typeof sentence === "string" ? withLinks(sentence) : [sentence].flat();
  return [chip, chip ? " " : null, ...words].filter((part) => part !== null && part !== undefined && part !== "");
}

function sayIn(line, state, sentence) {
  line.replaceChildren(...sayParts(state, sentence));
  line.hidden = false;
}

function saidLine(place) {
  const said = lastSaid.get(place);
  return el("p", { class: "said", "aria-live": "polite", tabindex: "-1", "data-focus": `said:${place}`, "data-said": place, hidden: !said },
    said ? sayParts(...said) : null);
}

function sayAt(place, state, sentence, { focus = true } = {}) {
  lastSaid.set(place, [state, sentence]);
  const line = currentSection().querySelector(`[data-said="${CSS.escape(place)}"]`);
  if (!line) return;
  sayIn(line, state, sentence);
  if (focus) line.focus();
}

function say(id, state, sentence) {
  sayIn(document.getElementById(id), state, sentence);
}

function fault(sentence) {
  return el("p", {}, sayParts("fault", sentence));
}

function quiet(...parts) {
  return el("p", { class: "muted" }, ...parts);
}

function link(href, text) {
  return /^https:\/\//.test(href || "") ? el("a", { href, rel: "noreferrer", target: "_blank", "data-focus": `link:${href}`, text }) : document.createTextNode(text);
}

function withLinks(text) {
  return text.split(URL_IN_TEXT).map((part, i) => (i % 2 ? link(part, part) : document.createTextNode(part)));
}

function wholeWords(text) {
  return text.split(" ").flatMap((word, i) => [i ? " " : null, el("span", { class: "nb", text: word })]).filter(Boolean);
}

function pullLinks(urls) {
  return urls.split(/\s+/).filter(Boolean).flatMap((url, i) => [i ? el("br") : null, link(url, url.replace("https://github.com/", ""))]).filter(Boolean);
}

function table(caption, heads, rows, { stack = false } = {}) {
  const head = el("tr", {}, heads.map(([label, cls]) => el("th", { class: cls, scope: "col", text: label })));
  const carried = heads.map(([, cls]) => {
    const classes = (cls || "").split(" ");
    const short = classes.includes("c-time") || classes.includes("c-status");
    return [...classes.filter((c) => c === "x" || c === "beside"), ...(short ? ["nb"] : [])];
  });
  for (const row of [rows].flat()) {
    [...row.children].forEach((td, i) => {
      if (carried[i]?.length) td.classList.add(...carried[i]);
      if (stack && heads[i]?.[0]) td.dataset.label = heads[i][0];
    });
  }
  return el("div", { class: stack ? "stack" : "wide" },
    el("table", {}, caption ? el("caption", {}, withFigures(caption)) : null, el("thead", {}, head), el("tbody", {}, rows)));
}

function withFigures(text) {
  return String(text).split(/(\d[\d.,:]*)/).map((part, i) => (i % 2 ? figure(part) : part)).filter(Boolean);
}

function cell(content, cls) {
  const td = el("td", { class: cls });
  td.append(...[content].flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false).map(asNode));
  return td;
}

function linkedRow(href, ...cells) {
  return el("tr", { "data-href": href }, cells);
}

function rowLink(kind, id, text) {
  const open = route?.detail?.kind === kind && route.detail.id === id;
  return el("a", { href: detailHash(kind, id), "data-focus": `row:${kind}:${id}`, "data-row": kind, "data-id": id, "aria-current": open ? "true" : null, text });
}

function itemLink(kind, id, text, focus) {
  return el("a", { href: detailHash(kind, id), "data-focus": focus, text });
}

function dateWords(d) {
  return `${d.getFullYear()} ${pad(d.getMonth() + 1)} ${pad(d.getDate())}`;
}

function clock(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return d.toDateString() === new Date().toDateString() ? time : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${time}`;
}

function zonedClock(ts) {
  if (!ts) return "";
  const zone = new Date(ts).toLocaleTimeString("en-US", { timeZoneName: "short" }).split(" ").pop();
  return `${clock(ts)} ${zone}`;
}

function ago(ts) {
  if (!ts) return "never";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(ts)) / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} h ago`;
  return `${Math.round(minutes / 1440)} d ago`;
}

function liveAgo(ts) {
  return ts ? el("time", { class: "ago", datetime: ts, text: ago(ts) }) : "never";
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${pad(minutes % 60)} min`;
}

function money(n) {
  const value = Number(n || 0);
  return value > 0 && value < 0.005 ? "<$0.01" : `$${value.toFixed(2)}`;
}

function paidMoney(n) {
  return n > 0 ? money(n) : "";
}

function count(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

function percent(rate) {
  return rate === null || rate === undefined ? "" : `${Math.round(rate * 100)}%`;
}

async function call(path, body) {
  const options = body === undefined ? { cache: "no-store" }
    : { method: "POST", headers: { "Content-Type": "application/json", "X-Arete": "1" }, body: JSON.stringify(body) };
  const response = await fetch(path, options).catch(() => { throw new Error("The connection to arete failed."); });
  const data = await response.json().catch(() => ({ error: `The server answered ${response.status} with no JSON.` }));
  if (!response.ok) throw new Error(data.error || `The server answered ${response.status}.`);
  return data;
}

function settle(promise) {
  return promise.then((value) => ({ value }), (e) => ({ error: e.message }));
}

function panelParts(panel, what, draw) {
  return panel && "rows" in panel ? draw(panel.rows) : fault(`${what} could not be read: ${panel?.error || "no answer"}.`);
}

function button(text, focus, onClick, disabled = false) {
  return el("button", { type: "button", "data-focus": focus, disabled, text, on: { click: (e) => {
    trackAction(focus.split(":")[0]);
    onClick(e.currentTarget);
  } } });
}

function seg(label, choices, current, onPick) {
  return el("div", { class: "seg", role: "group", "aria-label": label },
    choices.map(([value, words]) => el("button", { type: "button", "aria-pressed": String(value === current), "data-focus": `seg:${label}:${value}`,
      on: { click: () => {
        trackAction(`filter-${label.toLowerCase()}`);
        onPick(value);
      } } }, words)));
}

function figure(n) {
  return el("span", { class: "digits", text: String(n) });
}

function picker(label, id, values, current, onPick) {
  const select = el("select", { id, "data-focus": id, on: { change: (e) => {
    trackAction(`filter-${label.toLowerCase()}`);
    onPick(e.target.value);
  } } }, el("option", { value: "", text: `Any ${label.toLowerCase()}` }), values.map((v) => el("option", { value: v, text: v })));
  select.value = values.includes(current) ? current : "";
  return el("div", { class: "field" }, el("label", { for: id, text: label }), select);
}

function jumps(titles) {
  return el("nav", { class: "jumps", "aria-label": "Sections" }, titles.map((title) => button(title, `jump:${title}`, () => jumpTo(title))));
}

function jumpTo(title) {
  const target = currentSection().querySelector(`[data-jump="${CSS.escape(title)}"]`);
  if (!target) return;
  const head = target.closest(".detail")?.querySelector(".detail-head");
  target.style.scrollMarginTop = `${(head?.offsetHeight ?? 0) + 12}px`;
  target.scrollIntoView({ block: "start" });
  target.focus({ preventScroll: true });
}

function fact(name, value) {
  return value === "" || value === null || value === undefined ? null : [el("dt", { text: name }), el("dd", {}, value)];
}

function fold(key, summary, ...content) {
  return el("details", {
    open: openQuestions.has(key),
    on: { toggle: (e) => (e.target.open ? openQuestions.add(key) : openQuestions.delete(key)) },
  }, el("summary", { "data-focus": `fold:${key}` }, withFigures(summary)), ...content);
}

function foldText(key, summary, text) {
  return fold(key, summary, el("pre", { class: "fold-text", "data-focus": `text:${key}`, text }));
}

function runStatus(status) {
  return sayParts(RUN_STATE[status], status);
}

function secondsBetween(start, end) {
  return start ? Math.max(0, Math.round(((end ? new Date(end) : new Date()) - new Date(start)) / 1000)) : null;
}

function feedLine(line) {
  return `${clock(line.ts).padStart(5)}  ${line.kind.padEnd(7)}  ${line.text}`;
}

function stopWords(what, r, after) {
  if (r.was !== "gone") return `Asked ${what} to stop. ${after}`;
  return r.ticket ? `${what} had already ended; the next pipeline poll marks it stopped and tells its ticket.` : `${what} had already ended, so it is marked stopped.`;
}

async function act(btn, place, path, body, saying) {
  btn.disabled = true;
  try {
    sayAt(place, "ok", saying(await call(path, body)));
  } catch (e) {
    sayAt(place, "fault", e.message);
  }
  refresh();
}

/* Routing */

function viewHash(view, query = {}) {
  const kept = Object.entries(query).filter(([, value]) => value !== "" && value !== undefined && value !== null);
  return `#${view}${kept.length ? `?${new URLSearchParams(kept)}` : ""}`;
}

function detailHash(kind, id) {
  return `#${kind}/${encodeURIComponent(id)}`;
}

function listHash(view) {
  return viewHash(view, lastQuery[view] || {});
}

function parseRoute(hash) {
  const raw = (hash || "").replace(/^#/, "") || "live";
  const cut = raw.indexOf("?");
  const path = cut < 0 ? raw : raw.slice(0, cut);
  if (VIEWS[path]) {
    const query = Object.fromEntries(new URLSearchParams(cut < 0 ? "" : raw.slice(cut + 1)));
    return { view: path, query, detail: null, hash: viewHash(path, query) };
  }
  const slash = path.indexOf("/");
  const kind = path.slice(0, slash);
  if (slash < 0 || !DETAIL_VIEW[kind]) return null;
  let id;
  try {
    id = decodeURIComponent(path.slice(slash + 1));
  } catch {
    return null;
  }
  if (!id) return null;
  return { view: DETAIL_VIEW[kind], query: {}, detail: { kind, id }, hash: detailHash(kind, id) };
}

function knownRoute(hash) {
  const old = OLD_HASHES[(hash || "").replace(/^#/, "")];
  return parseRoute(old ?? hash) ?? parseRoute("#live");
}

function go(hash, way, { replace = false } = {}) {
  const next = parseRoute(hash);
  if (!next) return;
  if (next.hash === route.hash) {
    refresh();
    return;
  }
  history.replaceState({ ...history.state, focus: document.activeElement?.dataset?.focus || "", scroll: scrollY }, "");
  if (replace) history.replaceState({ from: history.state?.from ?? "" }, "", next.hash);
  else history.pushState({ from: route.hash }, "", next.hash);
  applyRoute(next, way);
}

function onHistory(event) {
  const state = event.type === "popstate" ? event.state : history.state;
  const asked = location.hash.replace(/^#/, "");
  const next = knownRoute(location.hash);
  if (next.hash !== location.hash) history.replaceState(state ?? { from: "" }, "", next.hash);
  if (asked === "chat") setConsole(true, { focus: true });
  const way = pendingWay || (state ? "back" : "link");
  pendingWay = "";
  if (next.hash === route.hash) return;
  if (way !== "link" && state && "scroll" in state) restoreAfterDraw = { scroll: state.scroll, focus: state.focus };
  applyRoute(next, way);
}

function applyRoute(next, way) {
  const viewChanged = !route || next.view !== route.view;
  if (!next.detail) lastQuery[next.view] = next.query;
  route = next;
  if (stint?.name !== stintName()) {
    endStint();
    startStint(way);
  }
  show(viewChanged, way);
}

function currentSection() {
  return document.querySelector(`main > section[data-view="${route.view}"]`);
}

function currentPane() {
  return currentSection().querySelector(".detail");
}

function show(viewChanged, way) {
  const { view, detail } = route;
  const meta = VIEWS[view];
  document.title = detail ? `Arete · ${ITEM_LABELS[detail.kind]} ${detail.id}` : `Arete · ${meta.label}`;
  document.getElementById("title").textContent = meta.label;
  document.getElementById("sub").textContent = meta.sub;
  for (const a of document.querySelectorAll("nav.side a[data-view]")) {
    a.href = listHash(a.dataset.view);
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  for (const section of document.querySelectorAll("main > section")) section.hidden = section.dataset.view !== view;
  for (const primary of document.querySelectorAll("[data-act]")) primary.classList.toggle("act", primary.closest("section")?.dataset.view === view);
  const section = currentSection();
  const split = section.querySelector(".split");
  let detailOpened = false;
  if (split) {
    split.classList.toggle("has-detail", Boolean(detail));
    const pane = split.querySelector(".detail");
    pane.hidden = !detail;
    const shows = detail ? `${detail.kind}:${detail.id}` : "";
    if (pane.dataset.shows !== shows) {
      detailOpened = Boolean(detail) && !pane.dataset.shows;
      pane.dataset.shows = shows;
      if (!detail) document.getElementById(`${pane.id}-head`).replaceChildren();
      document.getElementById(`${pane.id}-body`).replaceChildren(detail ? quiet("Loading") : "");
      document.getElementById(`${pane.id}-talk`).replaceChildren();
      pane.scrollTop = 0;
      pendingDetailFocus = Boolean(detail) && way !== "start";
    }
  }
  for (const a of section.querySelectorAll("a[data-row]")) {
    const open = detail?.kind === a.dataset.row && detail.id === a.dataset.id;
    if (open) a.setAttribute("aria-current", "true");
    else a.removeAttribute("aria-current");
  }
  const listGone = split && getComputedStyle(split.querySelector(".list")).display === "none";
  if (!restoreAfterDraw && (viewChanged || (detailOpened && listGone))) scrollTo(0, 0);
  refresh();
}

function closeDetail() {
  if (!route.detail) return;
  const { kind, id } = route.detail;
  focusAfterDraw = `row:${kind}:${id}`;
  const from = parseRoute(history.state?.from || "");
  if (from && from.view === route.view && !from.detail) {
    pendingWay = "close";
    history.back();
  } else {
    go(listHash(route.view), "close");
  }
}

function step(delta) {
  const id = shownNeighbors[delta < 0 ? 0 : 1];
  if (id && route.detail) go(detailHash(route.detail.kind, id), "step", { replace: true });
}

function rowIds(kind) {
  const list = currentSection().querySelector(".list");
  return list ? [...new Set([...list.querySelectorAll(`a[data-row="${kind}"]`)].map((a) => a.dataset.id))] : [];
}

function neighborsIn(ids, id) {
  const at = ids.indexOf(id);
  return at < 0 ? [null, null] : [ids[at - 1] ?? null, ids[at + 1] ?? null];
}

function detailHead(pane, kind, id, title, extra = [], neighbors = neighborsIn(rowIds(kind), id)) {
  shownNeighbors = neighbors;
  return [
    el("div", { class: "kicker", text: ITEM_LABELS[kind] }),
    el("h2", { id: `${pane}-title`, "data-focus": "detail-title", tabindex: "-1", text: title }),
    id !== title ? el("p", { class: "item-id muted", text: id }) : null,
    el("div", { class: "actions row-buttons" },
      button("Close", "close", closeDetail),
      button("Previous", "previous", () => step(-1), !neighbors[0]),
      button("Next", "next", () => step(1), !neighbors[1]),
      extra),
  ];
}

function focusByKey(key) {
  const node = key === "main" ? null : currentSection().querySelector(`[data-focus="${CSS.escape(key)}"]`)
    ?? document.querySelector(`nav.side [data-focus="${CSS.escape(key)}"]`);
  const target = node && node.checkVisibility() ? node : document.getElementById("main");
  target.focus({ preventScroll: true });
  const { top, bottom } = target.getBoundingClientRect();
  if (target !== document.getElementById("main") && (bottom < 0 || top > innerHeight)) target.scrollIntoView({ block: "center" });
}

function focusMark(node) {
  return node && node !== document.body ? { node, key: node.dataset?.focus ?? "" } : null;
}

function refocus(mark, fallback = null) {
  const again = mark?.node.isConnected ? mark.node
    : mark?.key ? [...document.querySelectorAll(`[data-focus="${CSS.escape(mark.key)}"]`)].find((node) => node.checkVisibility()) : null;
  (again ?? fallback)?.focus();
}

function afterDraw() {
  if (restoreAfterDraw) {
    const { scroll, focus } = restoreAfterDraw;
    restoreAfterDraw = null;
    if (typeof scroll === "number") scrollTo(0, scroll);
    if (!focusAfterDraw && focus) focusAfterDraw = focus;
  }
  if (pendingDetailFocus) {
    pendingDetailFocus = false;
    const pane = currentPane();
    if (pane && !pane.hidden && !pane.contains(document.activeElement) && !focusAfterDraw) focusByKey("detail-title");
  }
  if (focusAfterDraw) {
    const key = focusAfterDraw;
    focusAfterDraw = "";
    focusByKey(key);
  }
}

/* Refresh */

function schedule(ms) {
  clearTimeout(timer);
  if (ms) timer = setTimeout(refresh, ms);
}

function status(state, sentence) {
  document.getElementById("status").replaceChildren(`${dateWords(new Date())} · `, ...sayParts(state, sentence));
}

async function refresh() {
  clearTimeout(timer);
  const mine = ++refreshCount;
  if (document.hidden) return;
  const at = route;
  const label = VIEWS[at.view].label;
  try {
    const [draw, frame] = await Promise.all([LOADERS[at.view](at), call("/api/frame").catch(() => null)]);
    if (mine !== refreshCount) return;
    if (frame) drawFrame(frame);
    const drawn = draw();
    const held = drawn === HELD;
    const delay = held ? SLOW_MS : drawn || SLOW_MS;
    if (held) tickLiveAgos(currentSection().querySelectorAll("time.ago"));
    else lastGood.set(at.view, new Date());
    const shown = held ? "held while you edit" : `updated ${new Date().toLocaleTimeString([], { hour12: false })}`;
    status("", `${label} · ${shown} · refreshes every ${delay / 1000} s`);
    document.querySelectorAll(".readout").forEach((r) => r.classList.remove("stale"));
    afterDraw();
    schedule(delay);
  } catch (e) {
    if (mine !== refreshCount) return;
    const shownAt = clock(lastGood.get(at.view));
    status("fault", `Arete did not answer: ${e.message}${shownAt ? ` What shows came at ${shownAt}.` : ""}`);
    document.querySelectorAll(".readout").forEach((r) => r.classList.add("stale"));
    schedule(SLOW_MS);
  }
}

function setCount(view, n, words) {
  const box = document.getElementById(`count-${view}`);
  const a = box.closest("a");
  box.textContent = n ? String(n) : "";
  if (n) a.setAttribute("aria-label", `${VIEWS[view].label}, ${words(n)}`);
  else a.removeAttribute("aria-label");
}

function drawFrame(f) {
  setCount("live", f.needs_you, (n) => `${n} ${n === 1 ? "needs" : "need"} you`);
  setCount("runs", f.running, (n) => `${n} running`);
  setCount("triage", f.questions, (n) => `${count(n, "ticket", "tickets")} with questions`);
  const health = document.getElementById("count-health");
  const chip = f.health === "ok" ? null : stateChip(f.health);
  if (health.textContent !== (chip?.textContent ?? "")) health.replaceChildren(...(chip ? [chip] : []));
  const consoleWord = f.chat.busy ? "working" : f.chat.draft ? "draft" : "";
  const consoleCount = document.getElementById("count-console");
  if (consoleCount.textContent !== consoleWord) consoleCount.textContent = consoleWord;
  const line = document.getElementById("serve-line");
  const parts = serveShort(f.serve);
  if (line.textContent !== parts.map((p) => (p instanceof Node ? p.textContent : p)).join("")) line.replaceChildren(...parts);
}

function serveShort(serve) {
  if (!serve.at) return [stateChip("fault"), " Serve has never run"];
  if (!serve.alive) return [stateChip("fault"), ` Serve down, last beat ${ago(serve.at)}`];
  const troubled = Object.keys(serve.broken || {}).length + Object.keys(serve.poll_errors || {}).length;
  return [stateChip(troubled ? "caution" : "ok"), " Serve up · ", el("span", { class: "nb", text: `${count(serve.loops, "loop", "loops")} on` })];
}

/* Live */

function drawLive(d) {
  fill("live",
    heading(1, "Running now"),
    panelParts(d.live, "The live runs", runningParts),
    panelParts(d.waiting, "The review feedback", upNextParts),
    heading(2, "Needs you"),
    needsParts(d.needs_you),
    saidLine("needs"));
  return LIVE_MS;
}

function runningParts(live) {
  if (!live.runs.length) return [quiet("Nothing is running."), saidLine("running")];
  const anyPaid = live.runs.some((r) => r.paid > 0);
  return [
    el("p", { class: "readout" }, String(live.slots), el("small", { text: `of ${live.max_parallel} pipeline slots busy${live.waiting ? `, ${live.waiting} waiting for one` : ""}` })),
    table("", [["Work", "c-work"], ["Kind", "c-kind x"], ["Started", "c-time"], ["Last step"], ["Time", "num c-time x"], ...(anyPaid ? [["Paid", "num c-cost"]] : []), ["", "c-run-buttons"]],
      live.runs.map((r) => linkedRow(detailHash("run", r.id),
        cell(rowLink("run", r.id, r.ticket || r.id)), cell(r.kind), cell(clock(r.started), "nb"), cell(r.slot || r.step || "starting", "muted-cell"),
        cell(duration(r.seconds), "num"), anyPaid ? cell(paidMoney(r.paid), "num") : null,
        cell(button(r.stopping ? "Stopping" : "Stop", `stop:${r.id}`, (b) => stopLiveRun(r, b), r.stopping)))), { stack: true }),
    saidLine("running"),
  ];
}

function upNextParts(waiting) {
  if (!waiting.tickets.length) return null;
  return [
    el("h3", { text: "Up next" }),
    el("p", {}, `Review feedback runs ${waiting.quiet_minutes} min after the last comment, or at once when a review is submitted.`),
    table("", [["Ticket"], ["Comments", "num c-n"], ["When"]],
      waiting.tickets.map((w) => el("tr", {}, cell(w.ticket), cell(w.comments, "num"),
        cell(w.at_once ? `review submitted ${clock(w.submitted)}` : `last comment ${clock(w.last)}`))), { stack: true }),
  ];
}

function stopLiveRun(run, btn) {
  return act(btn, "running", `/api/runs/${encodeURIComponent(run.id)}/stop`, {},
    (r) => stopWords(run.ticket, r, "It leaves this list once it has."));
}

function kindTag(text) {
  return el("span", { class: "tag", text });
}

function needsParts(n) {
  const rows = [];
  const faults = [];
  const take = (panel, what, toRows) => {
    if (panel && "rows" in panel) rows.push(...toRows(panel.rows));
    else faults.push(fault(`${what} could not be read: ${panel?.error || "no answer"}.`));
  };
  take(n.approvals, "The approvals", (approvals) => approvals.map((a) => approvalRow(a, "needs")));
  take(n.ready, "The ready tickets", (ready) => ready.map(readyRow));
  take(n.skipped, "The skipped tickets", (skipped) => skipped.map(skippedRow));
  if (n.draft) rows.push(draftRow(n.draft));
  let notes = [];
  take(n.proposals, "The fixes to make by hand", (p) => {
    notes = [
      p.hand.length ? quiet("By hand: Claude Code will not let an agent write under .claude/, so the miner leaves these harness fixes to you. Make each one, then mark it done.") : null,
      p.memory.length ? quiet("Memory: facts the miner noticed for an agent's memory. File each one by hand, then mark it done.") : null,
    ];
    return [...p.hand.map((x) => proposalRow(x, "By hand", `${x.repo}/${x.file}`, `The fix to ${x.repo}/${x.file}`)),
      ...p.memory.map((x) => proposalRow(x, "Memory", x.repo, `The ${x.repo} fact`))];
  });
  return [
    faults,
    rows.length
      ? table("", [["Kind", "c-kind"], ["What"], ["Since", "c-time"], ["", "c-decide"]], rows, { stack: true })
      : faults.length ? null : el("p", {}, stateChip("ok"), " Nothing needs you."),
    notes,
  ];
}

function approvalRow(a, place) {
  return el("tr", {},
    cell(kindTag(a.failed ? "Failed" : "Approval")),
    cell([el("div", {}, el("span", { class: "nb", text: a.loop }), `: ${a.what}`),
      a.failed ? el("div", {}, stateChip("fault"), ` Delivery failed: ${a.failed}`) : null,
      a.detail ? foldText(`approval:${a.id}`, "Show the body", a.detail) : null,
      a.run_id && place === "needs" ? el("div", {}, itemLink("run", a.run_id, "Open the run that asked", `asked:${a.id}`)) : null]),
    cell(liveAgo(a.created), "nb"),
    cell(el("div", { class: "row-buttons" },
      button(a.failed ? "Try again" : "Approve", `approve:${a.id}`, (b) => decide(a.id, true, b, place)),
      button("Reject", `reject:${a.id}`, (b) => decide(a.id, false, b, place)))));
}

function decide(id, yes, btn, place) {
  if (yes) sayAt(place, null, `Approving #${id}. A push rebases and runs its check again before it lands, so it can take a minute.`);
  return act(btn, place, `/api/approvals/${id}`, { approve: yes },
    (r) => (yes ? `Approved #${id}: ${r.result}.` : `Rejected #${id}. Nothing was sent.`));
}

function readyRow(r) {
  return el("tr", {},
    cell(kindTag("Ready")),
    cell([el("div", {}, rowLink("ticket", r.ref, r.ref.replace(/^[^/#\s]+\//, "")), `: ${r.title}`), el("div", { class: "muted", text: r.verdict })]),
    cell(""),
    cell(el("div", { class: "row-buttons" }, button("Send", `send-ready:${r.ref}`, (b) => sendReady(r, b)))));
}

function sendReady(r, btn) {
  sayAt("needs", null, `Sending ${r.ref}. It screens the ticket and writes to it, so it can take a few seconds.`);
  return act(btn, "needs", `/api/triage/${encodeURIComponent(r.ref)}/send`,
    { questions: r.questions, answers: r.questions.map(() => ""), repo: r.repo }, (d) => d.said);
}

function skippedRow(s) {
  return el("tr", {},
    cell(kindTag("Skipped")),
    cell([el("div", {}, link(s.url, s.ticket)), el("div", { class: "muted", text: s.why })]),
    cell(liveAgo(s.since), "nb"),
    cell(s.can_run ? el("div", { class: "row-buttons" }, button("Run anyway", `run-anyway:${s.ticket}`, (b) => runSkipped(s.ticket, b)))
      : el("span", { class: "muted", text: "no repo to run in yet" })));
}

function runSkipped(ticket, btn) {
  return act(btn, "needs", `/api/skipped/${encodeURIComponent(ticket)}/run`, {},
    () => `Started ${ticket} past its blocker at ${clock(new Date())}. It shows under Running now while it runs.`);
}

function draftRow(draft) {
  return el("tr", {},
    cell(kindTag("Draft")),
    cell([el("div", { text: draft.title }), el("div", { class: "muted", text: "The console drafted this issue on arete. Nothing reaches GitHub until you file it." }),
      foldText("draft:body", "Show the body", draft.body)]),
    cell(""),
    cell(el("div", { class: "row-buttons" },
      button("File this issue", "file-draft", fileDraft),
      button("Drop", "drop-draft", dropDraft),
      button("Open the console", "open-console", () => setConsole(true, { focus: true })))));
}

function fileDraft(btn) {
  return act(btn, "needs", "/api/chat/file", {}, (view) => {
    drawChat(view);
    return view.messages.at(-1)?.text || "Filed the issue.";
  });
}

function dropDraft(btn) {
  return act(btn, "needs", "/api/chat/drop", {}, (view) => {
    drawChat(view);
    return "Dropped the drafted issue. Nothing reached GitHub.";
  });
}

function proposalRow(x, kind, where, what) {
  return el("tr", {},
    cell(kindTag(kind)),
    cell([el("div", { class: "wrap-cell", text: where }), el("div", { class: "muted", text: x.change })]),
    cell(""),
    cell(el("div", { class: "row-buttons" }, button("Mark done", `done:${x.id}`, (b) => markDone(x.id, what, b)))));
}

function markDone(id, what, btn) {
  return act(btn, "needs", `/api/proposals/${id}/done`, {}, () => `${what} is marked done, so it leaves this list.`);
}

/* Run and session pages */

async function loadRunDetail(id) {
  const board = await call(`/api/runs/${encodeURIComponent(id)}`);
  if (board.lines_from === "feed") {
    const after = feedOf.id === id ? feedOf.after : 0;
    return { board, feed: { data: await call(`/api/feed/${encodeURIComponent(id)}?after=${after}`), asked: after } };
  }
  return { board, log: await call(`/api/log/${encodeURIComponent(id)}`) };
}

function mergeFeed(id, { data, asked }) {
  if (feedOf.id !== id) feedOf = { id, after: 0, lines: [] };
  if (asked === feedOf.after) {
    const fresh = data.lines.filter((line) => line.id > feedOf.after);
    if (fresh.length) {
      feedOf.lines = [...feedOf.lines, ...fresh.map(feedLine)].slice(-FEED_KEEP_LINES);
      feedOf.after = fresh.at(-1).id;
    }
  }
  return feedOf.lines;
}

function paidWords(r, models) {
  if (r.paid > 0) return money(r.paid);
  return models.length ? "nothing, it ran on the subscription" : "nothing";
}

function modelTable(models) {
  return table("", [["Model"], ["Runner", "c-kind"], ["Calls", "num c-n"], ["Tokens in", "num c-rate x"], ["Tokens out", "num c-rate x"], ["Paid", "num c-cost"]],
    models.map((m) => el("tr", {}, cell(m.model), cell(m.runner), cell(m.steps, "num"), cell(m.tokens_in.toLocaleString(), "num"),
      cell(m.tokens_out.toLocaleString(), "num"), cell(m.paid ? money(m.cost_usd) : "subscription", "num"))), { stack: true });
}

function drawRunDetail(pane, result, kind) {
  const id = route.detail.id;
  const place = `detail:${kind}:${id}`;
  if (result.error) {
    fill(`${pane}-head`, detailHead(pane, kind, id, id));
    fill(`${pane}-body`, fault(`This ${kind} could not be read: ${result.error}`));
    fill(`${pane}-talk`);
    return SLOW_MS;
  }
  const { board, feed, log } = result.value;
  const r = board.run;
  const running = r.status === "running";
  const lines = feed ? mergeFeed(id, feed) : log.lines;
  const shownLines = document.querySelector(`#${pane}-body pre.lines`);
  const stick = !shownLines || shownLines.scrollTop + shownLines.clientHeight >= shownLines.scrollHeight - 4;
  fill(`${pane}-head`, detailHead(pane, kind, id, r.ticket || id,
    running ? button(r.stop_asked ? "Stopping" : "Stop", `stop:${id}`, (b) => stopRun(id, place, b), Boolean(r.stop_asked)) : null));
  const s = board.session;
  fill(`${pane}-body`,
    saidLine(place),
    el("dl", { class: "w" },
      fact("Status", runStatus(r.status)), fact("Kind", r.kind), fact("Repo", r.repo), fact("Loop", r.loop),
      fact("Model", r.model ? `${r.model}${r.runner ? ` on ${r.runner}` : ""}` : ""), fact("Device", r.device),
      fact("Started", r.started ? [zonedClock(r.started), " · ", liveAgo(r.started)] : ""),
      fact("Time", duration(secondsBetween(r.started, r.finished))),
      fact("Paid", paidWords(r, board.models)),
      fact("Pull request", r.pr_url ? pullLinks(r.pr_url) : ""),
      fact("Summary", r.summary ? withLinks(r.summary) : "")),
    board.approvals.length ? [el("h3", { text: "Waiting for you" }),
      table("", [["Kind", "c-kind"], ["What"], ["Since", "c-time"], ["", "c-decide"]], board.approvals.map((a) => approvalRow(a, place)), { stack: true })] : null,
    s ? [el("h3", { text: "Session" }), el("dl", { class: "w" },
      fact("Folder", s.cwd ? el("code", { text: s.cwd }) : ""), fact("Repo", s.repo), fact("Tools", s.tools.join(", ") || "none")),
    s.prompt ? foldText(`prompt:${id}`, "Show the first message", s.prompt) : null] : null,
    board.models.length ? [el("h3", { text: "Models" }), modelTable(board.models)] : null,
    el("h3", { text: feed ? "Feed" : "Log" }),
    el("pre", { class: "lines", tabindex: "0", "data-focus": "lines", "aria-label": feed ? "The feed" : "The log",
      text: lines.length ? lines.join("\n") : feed ? "Nothing in the feed yet." : "This run has no log yet." }));
  if (stick) {
    const fresh = document.querySelector(`#${pane}-body pre.lines`);
    fresh.scrollTop = fresh.scrollHeight;
  }
  fill(`${pane}-talk`, r.kind === "session" && running ? talkForm(pane, id) : null);
  if (r.kind === "session" && running) return CHAT_BUSY_MS;
  return running ? LIVE_MS : SLOW_MS;
}

function stopRun(id, place, btn) {
  return act(btn, place, `/api/runs/${encodeURIComponent(id)}/stop`, {}, (r) => stopWords(id, r, "The feed says when it has."));
}

function talkForm(pane, id) {
  const textId = `${pane}-text`;
  const text = el("textarea", { id: textId, rows: "3", spellcheck: "true", "data-focus": textId, on: {
    input: (e) => talkDrafts.set(id, e.target.value),
    keydown: (e) => e.key === "Enter" && (e.metaKey || e.ctrlKey) && e.target.form.requestSubmit(),
  } });
  text.value = talkDrafts.get(id) || "";
  return el("form", { class: "talk", novalidate: true, "aria-label": "Send the session a message", on: { submit: (e) => sendSession(e, id) } },
    el("div", { class: "field" }, el("label", { for: textId, text: "Message" }), text),
    el("div", { class: "actions" },
      el("button", { type: "submit", "data-focus": `${pane}-send`, disabled: sessionSending, text: "Send" }),
      el("span", { class: "muted keys", text: "Ctrl or Cmd with Enter sends" })),
    saidLine(`talk:${id}`));
}

async function sendSession(event, id) {
  event.preventDefault();
  if (sessionSending) return;
  const form = event.currentTarget;
  const box = form.querySelector("textarea");
  const text = box.value.trim();
  if (!text) return sayAt(`talk:${id}`, "caution", "Write a message first.", { focus: false });
  trackAction("session-send");
  sessionSending = true;
  form.querySelector("button[type=submit]").disabled = true;
  try {
    await call(`/api/sessions/${encodeURIComponent(id)}/send`, { text });
    talkDrafts.delete(id);
    box.value = "";
    sayAt(`talk:${id}`, "ok", "Sent. The answer shows in the feed when it comes.", { focus: false });
  } catch (e) {
    sayAt(`talk:${id}`, "fault", e.message, { focus: false });
  } finally {
    sessionSending = false;
  }
  refresh();
}

/* Runs */

function drawRuns(data, q) {
  const limit = Number(q.limit) || PAGE_SIZE;
  const filter = (key, value) => go(viewHash("runs", { ...q, [key]: value, limit: "" }), "link", { replace: true });
  const filtered = ["status", "kind", "repo", "loop"].some((key) => q[key]);
  const anyPaid = data.runs.some((r) => r.paid > 0);
  fill("runs-list",
    el("div", { class: "filters" },
      seg("Status", [["", "All"], ["running", "Running"], ["failed", "Failed"]], q.status || "", (v) => filter("status", v)),
      seg("Kind", [["", "Every kind"], ...data.facets.kind.map((k) => [k, k])], q.kind || "", (v) => filter("kind", v)),
      el("div", { class: "fields" },
        picker("Repo", "runs-repo", data.facets.repo, q.repo, (v) => filter("repo", v)),
        picker("Loop", "runs-loop", data.facets.loop, q.loop, (v) => filter("loop", v)))),
    data.runs.length
      ? table("Running first, then newest first",
        [["Work", "c-work"], ["Kind", "c-kind x"], ["Status", "c-status"], ["Started", "c-time"], ["Time", "num c-time x"], ...(anyPaid ? [["Paid", "num c-cost x"]] : []), ["Result", "c-result x"]],
        data.runs.map((r) => linkedRow(detailHash("run", r.id),
          cell(rowLink("run", r.id, r.ticket || r.id)), cell(r.kind), cell(runStatus(r.status), "nb"), cell(clock(r.started), "nb"),
          cell(duration(r.seconds), "num"), anyPaid ? cell(paidMoney(r.paid), "num") : null,
          cell(r.pr_url ? pullLinks(r.pr_url) : r.summary, "muted-cell"))))
      : quiet(filtered ? "No run matches these filters." : "No run has started yet."),
    data.more ? el("div", { class: "actions" },
      button(`Show ${PAGE_SIZE} more`, "runs-more", () => go(viewHash("runs", { ...q, limit: String(limit + PAGE_SIZE) }), "link", { replace: true }))) : null);
  return data.runs.some((r) => r.status === "running") ? LIVE_MS : SLOW_MS;
}

function runsQuery(q) {
  const wanted = Object.fromEntries(["status", "kind", "repo", "loop"].map((key) => [key, q[key] || ""]).filter(([, v]) => v));
  return new URLSearchParams({ ...wanted, limit: String(Number(q.limit) || PAGE_SIZE) });
}

/* Sessions */

function launchValues() {
  return {
    device: document.getElementById("launch-device").value,
    runner: document.getElementById("launch-runner").value,
    repo: document.getElementById("launch-repo").value,
    tools: [...document.querySelectorAll("#launch-tools input:checked")].map((i) => i.value),
  };
}

function options(id, first, names, keep) {
  fill(id, first ? el("option", { value: "", text: first }) : null, names.map((n) => el("option", { value: n, text: n })));
  const box = document.getElementById(id);
  if (keep !== undefined && [...box.options].some((o) => o.value === keep)) box.value = keep;
}

function fillChoices(c, reset = false) {
  const key = JSON.stringify(c);
  if (key === launchKey && !reset) return;
  const keep = launchKey && !reset ? launchValues() : { device: "local", runner: "", repo: "", tools: c.default_tools };
  launchKey = key;
  remoteDevices = c.remote_devices;
  options("launch-device", "", c.devices, keep.device);
  options("launch-runner", "By the model", c.runners, keep.runner);
  options("launch-repo", "None", c.repos, keep.repo);
  fill("launch-models", c.models.map((m) => el("option", { value: m })));
  document.getElementById("launch-model").placeholder = `default: ${c.default_model}`;
  fill("launch-tools", el("legend", { text: "Tools" }), c.tools.map((t) => el("label", { class: "check" },
    el("input", { type: "checkbox", value: t, checked: keep.tools.includes(t) }), t)));
  document.getElementById("launch-about").textContent = "A session is an agent you talk to. It runs on the device and model you pick, " +
    `takes each message you send, and closes after ${c.idle_hours} hours with no message. A session on a repo works in a worktree of its own, which stays when it ends.`;
  repoFollowsDevice();
}

function repoFollowsDevice() {
  const repo = document.getElementById("launch-repo");
  const remote = remoteDevices.includes(document.getElementById("launch-device").value);
  if (remote) repo.value = "";
  repo.disabled = remote;
}

function firstMessage(text) {
  const flat = (text || "").replace(/\s+/g, " ").trim();
  return flat.length > FIRST_MESSAGE_CHARS ? `${flat.slice(0, FIRST_MESSAGE_CHARS - 1).trimEnd()}…` : flat || "no message";
}

function drawSessions(data) {
  fillChoices(data.choices);
  const anyPaid = data.sessions.some((s) => s.paid > 0);
  fill("sessions-list",
    data.sessions.length
      ? table("Newest first", [["First message"], ["Status", "c-status"], ["Started", "c-time"], ["Model", "c-model x"], ...(anyPaid ? [["Paid", "num c-cost x"]] : [])],
        data.sessions.map((s) => linkedRow(detailHash("session", s.id),
          cell([rowLink("session", s.id, firstMessage(s.prompt)), s.repo ? el("div", { class: "muted", text: `${s.repo}${s.branch ? ` · ${s.branch}` : ""}` }) : null], "wrap-cell"),
          cell(runStatus(s.status), "nb"), cell(clock(s.started), "nb"),
          cell([s.model || "", el("div", { class: "muted", text: [s.runner, s.device].filter(Boolean).join(" on ") })]),
          anyPaid ? cell(paidMoney(s.paid), "num") : null)), { stack: true })
      : quiet("No session yet. New session starts one."));
  return data.sessions.some((s) => s.status === "running") ? LIVE_MS : SLOW_MS;
}

function toggleForm(toggleId, formId, focusId, open) {
  const toggle = document.getElementById(toggleId);
  const form = document.getElementById(formId);
  const opening = open ?? form.hidden;
  form.hidden = !opening;
  toggle.setAttribute("aria-expanded", String(opening));
  toggle.textContent = opening ? "Close the form" : toggle.dataset.label;
  if (opening) document.getElementById(focusId).focus();
}

function openLaunch() {
  toggleForm("launch-toggle", "launch", "launch-prompt", true);
}

function openLoopNew() {
  toggleForm("loop-new-toggle", "loop-new", "loop-new-sentence", true);
}

async function launch(event) {
  event.preventDefault();
  const prompt = document.getElementById("launch-prompt");
  const budget = document.getElementById("launch-budget").value.trim();
  if (!prompt.value.trim()) return say("launch-result", "caution", "Write the first message first.");
  if (budget && !(Number(budget) > 0)) return say("launch-result", "caution", "The budget is a number of dollars above zero, such as 0.50, or empty for no limit.");
  trackAction("launch");
  const values = launchValues();
  const btn = document.getElementById("launch-go");
  btn.disabled = true;
  try {
    const s = await call("/api/sessions", { prompt: prompt.value.trim(), device: values.device, model: document.getElementById("launch-model").value.trim(),
      runner: values.runner, repo: values.repo, tools: values.tools, budget_usd: budget ? Number(budget) : null });
    lastSaid.set(`detail:session:${s.id}`, ["ok", `Launched: ${s.runner} ${s.model} on ${s.device}. Its feed shows below as it works.`]);
    prompt.value = "";
    document.getElementById("launch-result").hidden = true;
    toggleForm("launch-toggle", "launch", "launch-prompt", false);
    go(detailHash("session", s.id), "link");
  } catch (e) {
    say("launch-result", "fault", e.message);
    document.getElementById("launch-result").focus();
  } finally {
    btn.disabled = false;
  }
}

/* Loops */

function serveState(serve) {
  if (!serve.at) return el("p", {}, stateChip("fault"), " Serve has never run on this machine, so no loop fires on its own. Installing it starts it and keeps it running.");
  if (!serve.alive) return el("p", {}, stateChip("fault"), " Serve last beat ", liveAgo(serve.at), ` (${clock(serve.at)}), so no loop fires on its own until it runs again.`);
  const broken = Object.keys(serve.broken || {}).length;
  const errors = Object.keys(serve.poll_errors || {}).length;
  return el("p", {}, stateChip(broken || errors ? "caution" : "ok"),
    ` Serve is up since ${clock(serve.started)}: ${serve.loops} loops on, ${serve.off} off, ${serve.running} running, ${serve.pending} waiting.` +
    (broken ? ` ${count(broken, "loop does", "loops do")} not load.` : "") + (errors ? ` ${count(errors, "source fails", "sources fail")} to poll.` : ""));
}

function serveLine(serve) {
  return [serveState(serve), !serve.at || !serve.alive
    ? el("div", { class: "actions" }, button(serve.at ? "Restart serve" : "Install serve", "serve-install", installServe)) : null,
  saidLine("serve")];
}

function installServe(btn) {
  sayAt("serve", null, "Installing serve. It loads the background job and readies each repo's labels, so it can take a minute.");
  return act(btn, "serve", "/api/serve/install", {}, (r) => (r.restarting
    ? "Serve is restarting. This page stops answering for a few seconds and comes back once serve is up."
    : `Installed serve${r.said ? `: ${r.said}` : ""}.`));
}

function loopLastRun(l) {
  if (l.running) return ["running", el("br"), itemLink("run", l.running, "Open the run", `running:${l.name}`)];
  if (!l.last) return "never";
  return [...runStatus(l.last.status), el("br"), el("a", { href: detailHash("run", l.last.id), "data-focus": `last:${l.name}`, class: "muted-link" }, liveAgo(l.last.started))];
}

function loopButtons(l, place) {
  return [
    button("Start", `start:${l.name}`, (b) => loopAction(l.name, "start", b, place), Boolean(l.error || l.running)),
    button("Try", `try:${l.name}`, (b) => loopAction(l.name, "try", b, place), Boolean(l.error || l.running)),
    button(l.enabled ? "Turn off" : "Turn on", `switch:${l.name}`, (b) => loopAction(l.name, l.enabled ? "disable" : "enable", b, place), Boolean(l.error && !l.enabled)),
  ];
}

function loopRow(l) {
  return linkedRow(detailHash("loop", l.name),
    cell([rowLink("loop", l.name, l.name), el("div", { class: "muted", text: l.error || l.description })]),
    cell(l.error ? stateChip("fault") : l.enabled ? "on" : "off", "nb"),
    cell(l.kind),
    cell(l.trigger, "nb"),
    cell(loopLastRun(l), "nb"),
    cell(l.next_due ? clock(l.next_due) : "", "nb"),
    cell(paidMoney(l.spent_24h), "num"),
    cell(el("div", { class: "loop-buttons" }, loopButtons(l, "loops"))));
}

function drawLoops(data) {
  fill("loops-list",
    serveLine(data.serve),
    saidLine("loops"),
    data.loops.length
      ? table("", [["Loop", "c-loop"], ["On", "c-on"], ["Kind", "x"], ["Trigger", "x"], ["Last run", "c-status"], ["Next", "c-time x"], ["Paid 24 h", "num x"], ["", "c-loop-buttons beside"]],
        data.loops.map(loopRow), { stack: true })
      : quiet("No loop is installed. New loop drafts one from a sentence."));
  fillLoopNew(data.draft);
  return data.loops.some((l) => l.running) ? LIVE_MS : SLOW_MS;
}

function fillLoopNew(d) {
  const key = JSON.stringify(d);
  if (key === loopNewKey) return;
  loopNewKey = key;
  options("loop-new-repo", "None", d.repos, document.getElementById("loop-new-repo").value);
  fill("loop-new-models", d.models.map((m) => el("option", { value: m })));
  document.getElementById("loop-new-model").placeholder = `default: ${d.default_model}`;
}

async function draftLoop(event) {
  event.preventDefault();
  const value = (id) => document.getElementById(`loop-new-${id}`).value.trim();
  if (!value("sentence")) {
    say("loop-new-result", "caution", "Say in a sentence what the loop should do first.");
    return document.getElementById("loop-new-sentence").focus();
  }
  trackAction("draft-loop");
  const btn = document.getElementById("loop-new-go");
  btn.disabled = true;
  try {
    const r = await call("/api/loops/new", { sentence: value("sentence"), name: value("name"), repo: value("repo"), model: value("model") });
    lastSaid.set(`detail:run:${r.id}`, ["ok", `Started a draft at ${clock(r.started)}. Its feed shows below. The loop lands turned off, so try it before you turn it on.`]);
    for (const id of ["sentence", "name"]) document.getElementById(`loop-new-${id}`).value = "";
    document.getElementById("loop-new-result").hidden = true;
    toggleForm("loop-new-toggle", "loop-new", "loop-new-sentence", false);
    go(detailHash("run", r.id), "link");
  } catch (e) {
    say("loop-new-result", "fault", e.message);
    document.getElementById("loop-new-result").focus();
  } finally {
    btn.disabled = false;
  }
}

async function loopAction(name, action, btn, place) {
  btn.disabled = true;
  try {
    const r = await call(`/api/loops/${encodeURIComponent(name)}/${action}`, {});
    if (action === "start" || action === "try") {
      lastSaid.set(`detail:run:${r.id}`, ["ok", action === "start" ? `Started ${name} at ${clock(r.started)}.`
        : `Started a try of ${name} at ${clock(r.started)}. The feed shows what each emit would do, and none is sent.`]);
      go(detailHash("run", r.id), "link");
      return;
    }
    if (r.enabled && !r.serve_up) sayAt(place, "caution", `${name} is on, but serve is down, so it fires once serve runs again.`);
    else sayAt(place, "ok", `${name} is ${r.enabled ? "on. Serve fires it from its next tick" : "off. A run already going finishes"}.`);
  } catch (e) {
    sayAt(place, "fault", e.message);
  }
  refresh();
}

function budgetWords(d) {
  if (d.metered === false) return "none, claude runs on the subscription";
  return [d.budget_usd ? `${money(d.budget_usd)} a run` : "", d.daily_budget_usd ? `${money(d.daily_budget_usd)} a day` : ""].filter(Boolean).join(", ") || "none";
}

function loopFacts(d) {
  return el("dl", { class: "w" },
    fact("On", d.enabled ? "on" : "off"), fact("Kind", d.kind), fact("Folder", el("code", { text: d.dir })), fact("Trigger", d.trigger),
    fact("Webhook", d.webhook_path ? el("code", { text: `POST ${d.webhook_path}` }) : ""),
    fact("Repo", d.repo), fact("Model", d.model ? `${d.model}${d.runner ? ` on ${d.runner}` : ""}` : ""),
    fact("Device", d.device), fact("Queue", d.queue === d.name ? "its own" : d.queue), fact("Budget", budgetWords(d)),
    fact("Paid 24 h", paidMoney(d.spent_24h)),
    fact("Emits", d.emits.join(", ") || "nothing"),
    fact("Next", d.next_due ? `${clock(d.next_due)}` : d.enabled ? "" : "off, so never on its own"),
    fact("Waiting", d.waiting.length ? `${d.waiting.length} in line` : ""), fact("Held", d.held));
}

function loopRuns(d) {
  if (!d.runs.length) return quiet("It has not run yet.");
  const anyPaid = d.runs.some((r) => r.paid > 0);
  return table("Newest first", [["Started", "c-time"], ["Trigger", "c-kind x"], ["Status", "c-status"], ["Time", "num c-time x"], ...(anyPaid ? [["Paid", "num c-cost"]] : []), ["Result", "c-result x"]],
    d.runs.map((r) => linkedRow(detailHash("run", r.id),
      cell(itemLink("run", r.id, clock(r.started), `loop-run:${r.id}`), "nb"), cell(r.trigger || ""), cell(runStatus(r.status), "nb"),
      cell(duration(secondsBetween(r.started, r.finished)), "num"), anyPaid ? cell(paidMoney(r.paid), "num") : null,
      cell(r.pr_url ? pullLinks(r.pr_url) : r.summary || "", "muted-cell"))));
}

function loopSources(d) {
  return d.cursors.length
    ? table("", [["Source"], ["Polled", "c-time"], ["State", "c-status"], ["Detail"]],
      d.cursors.map((c) => el("tr", {}, cell(c.source, "muted-cell"), cell(liveAgo(c.polled), "nb"),
        cell(stateChip(c.ok ? "ok" : "fault")), cell(c.detail || (c.ok ? "reading" : "")))))
    : quiet(d.sources.length ? "Serve has not polled its sources yet." : "It has no source.");
}

function loopDigests(d) {
  return d.digests.length
    ? d.digests.map((g) => foldText(`digest:${d.name}:${g.day}`, g.day, g.text))
    : quiet("No digest yet.");
}

function subheading(title) {
  return el("h3", { "data-jump": title, tabindex: "-1", text: title });
}

function drawLoopDetail(result) {
  const name = route.detail.id;
  const place = `detail:loop:${name}`;
  if (result.error) {
    fill("loops-detail-head", detailHead("loops-detail", "loop", name, name));
    fill("loops-detail-body", fault(`The loop could not be read: ${result.error}`));
    return SLOW_MS;
  }
  const d = result.value;
  const memory = Object.keys(d.state || {}).length ? JSON.stringify(d.state, null, 2) : "";
  const sections = [
    ["Runs", loopRuns(d)],
    ["Findings", d.findings.length
      ? table("Each finding is reported once, by its fingerprint", [["Finding"], ["Seen", "num c-n"], ["First", "c-time x"], ["Last", "c-time"]],
        d.findings.map((f) => el("tr", {}, cell(f.url ? link(f.url, f.title || f.fingerprint) : f.title || f.fingerprint), cell(f.seen, "num"),
          cell(clock(f.first_seen), "nb"), cell(clock(f.last_seen), "nb"))))
      : quiet("No finding yet.")],
    ["Sources", loopSources(d)],
    d.metrics.length ? ["Metrics", table("The last 30 days", [["Metric"], ["Latest", "num c-cost"], ["When", "c-time"], ["Points", "num c-n x"]],
      d.metrics.map((m) => el("tr", {}, cell(m.name), cell(Number(m.value).toLocaleString(undefined, { maximumFractionDigits: 20 }), "num"), cell(clock(m.ts), "nb"), cell(m.points, "num"))))] : null,
    d.emits.some((e) => e.startsWith("digest")) ? ["Digests", loopDigests(d)] : null,
    ["Memory", memory ? foldText(`memory:${d.name}`, "Show what it keeps between runs", memory) : quiet("It keeps nothing between runs yet.")],
    d.prompt ? ["Prompt", foldText(`prompt:${d.name}`, "Show the prompt", d.prompt)] : null,
  ].filter(Boolean);
  fill("loops-detail-head", detailHead("loops-detail", "loop", name, d.name, loopButtons(d, place)));
  fill("loops-detail-body",
    saidLine(place),
    d.error ? fault(`It does not load: ${d.error}`) : el("p", { text: d.description }),
    loopFacts(d),
    jumps(sections.map(([title]) => title)),
    sections.map(([title, body]) => [subheading(title), body]));
  return d.running ? LIVE_MS : SLOW_MS;
}

/* Triage */

function refreshTriage(btn) {
  return act(btn, "triage-refresh", "/api/triage/refresh", {},
    () => `Started triage at ${clock(new Date())}. It reads every open ticket, so it can take a few minutes; the verdicts here change when it ends.`);
}

function researchTicket(r, btn) {
  return act(btn, `research:${r.ref}`, `/api/triage/${encodeURIComponent(r.ref)}/research`, { force: r.research.force },
    () => `Asked the research loop at ${clock(new Date())} to research ${r.ref}. Its verdict follows the research.`);
}

function researchOffer(r, researching) {
  return [
    r.research ? el("div", { class: "actions offer" },
      button(r.research.label, `research:${r.ref}`, (b) => researchTicket(r, b), researching),
      r.research.force ? el("span", { class: "muted", text: "This skips JEV's screen for instructions aimed at an agent, so read the ticket first." }) : null) : null,
    r.research || lastSaid.has(`research:${r.ref}`) ? saidLine(`research:${r.ref}`) : null,
  ];
}

function draftKey(ref, part) {
  return `${ref}\n${part}`;
}

function isRepoQuestion(r, q) {
  return q === r.send.repo_question;
}

function pickedRepo(r) {
  return r.send.pick ? drafts.get(draftKey(r.ref, "repo")) || "" : r.repo;
}

function sendWords(r) {
  const choice = r.send.choices.find((c) => c.repo === pickedRepo(r));
  const labels = choice ? `${choice.labels.length === 1 ? "the label" : "the labels"} ${choice.labels.join(" and ")}` : "the labels of the repo you pick";
  return r.questions.some((q) => !isRepoQuestion(r, q))
    ? `Send posts your answers to the ticket as one comment from you, leaving out blank ones. Then it adds ${labels} and starts a poll to build it.`
    : `Send adds ${labels} to the ticket and starts a poll to build it.`;
}

function sendForm(r) {
  const id = (part) => `send:${r.ref}:${part}`;
  const field = (control, label) => el("div", { class: "field" }, el("label", { for: control.id, text: label }), control);
  const repoPicker = el("select", { id: id("repo"), "data-focus": id("repo"), on: { change: (e) => {
    drafts.set(draftKey(r.ref, "repo"), e.target.value);
    e.target.form.querySelector(".send-about").textContent = sendWords(r);
  } } }, el("option", { value: "", text: "Pick a repo" }), r.send.choices.map((c) => el("option", { value: c.repo, text: c.repo })));
  repoPicker.value = pickedRepo(r);
  const box = (q, i) => {
    const answer = el("textarea", { id: id(i), "data-focus": id(i), on: {
      input: (e) => drafts.set(draftKey(r.ref, q), e.target.value),
      keydown: (e) => e.key === "Enter" && (e.metaKey || e.ctrlKey) && e.target.form.requestSubmit(),
    } });
    answer.value = drafts.get(draftKey(r.ref, q)) || "";
    return answer;
  };
  const fields = r.questions.map((q, i) => field(isRepoQuestion(r, q) ? repoPicker : box(q, i), q));
  if (r.send.pick && !r.send.repo_question) fields.push(field(repoPicker, "The repo to build it in"));
  const said = sendSaid.get(r.ref);
  return el("form", { class: "send", "aria-label": `Send ${r.ref} to the pipeline`, on: { submit: (e) => sendTicket(e, r) } },
    fields,
    el("p", { class: "muted send-about", text: sendWords(r) }),
    el("div", { class: "actions" }, el("button", { type: "submit", "data-focus": id("go"), disabled: sending.has(r.ref), text: "Send to the pipeline" })),
    el("p", { class: "send-said", "aria-live": "polite", tabindex: "-1", "data-focus": id("said"), hidden: !said }, said ? sayParts(...said) : null));
}

function sendSay(form, r, state, sentence) {
  sendSaid.set(r.ref, [state, sentence]);
  const line = form.querySelector(".send-said");
  sayIn(line, state, sentence);
  line.focus();
}

async function sendTicket(event, r) {
  event.preventDefault();
  const form = event.currentTarget;
  const repo = pickedRepo(r);
  if (!repo) {
    sendSay(form, r, "caution", "Pick the repo it is for first.");
    return form.querySelector("select").focus();
  }
  trackAction("ticket-send");
  sending.add(r.ref);
  sendSay(form, r, null, `Sending ${r.ref}. It screens the ticket and writes to it, so it can take a few seconds.`);
  form.querySelector("button[type=submit]").disabled = true;
  try {
    const answers = r.questions.map((q) => (isRepoQuestion(r, q) ? "" : drafts.get(draftKey(r.ref, q)) || ""));
    const done = await call(`/api/triage/${encodeURIComponent(r.ref)}/send`, { questions: r.questions, answers, repo });
    for (const key of drafts.keys()) if (key.startsWith(draftKey(r.ref, ""))) drafts.delete(key);
    sendSaid.delete(r.ref);
    lastSaid.set(`ticket:${r.ref}`, ["ok", done.said]);
    focusAfterDraw = `said:ticket:${r.ref}`;
  } catch (e) {
    sendSay(form, r, "fault", e.message);
  } finally {
    sending.delete(r.ref);
    form.querySelector("button[type=submit]").disabled = false;
  }
  forceTriageDraw = true;
  refresh();
}

function groupSlug(standing) {
  return standing.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function defaultGroup(groups) {
  const withRows = (slug) => groups.find((g) => g.slug === slug && g.rows.length);
  return withRows("ready-to-build") ?? withRows("with-questions") ?? groups.find((g) => g.rows.length) ?? groups[0];
}

function shortRef(ref) {
  return ref.replace(/^[^/#\s]+\//, "");
}

function drawTriage(panel, r) {
  if (!("rows" in panel)) {
    fill("triage-list", fault(`Triage could not be read: ${panel.error || "no answer"}.`));
    if (r.detail) {
      fill("triage-detail-head", detailHead("triage-detail", "ticket", r.detail.id, r.detail.id, [], [null, null]));
      fill("triage-detail-body", fault(`Triage could not be read: ${panel.error || "no answer"}.`));
    }
    return SLOW_MS;
  }
  const t = panel.rows;
  const groups = t.groups.map((g) => ({ ...g, slug: groupSlug(g.standing) }));
  const q = r.detail ? lastQuery.triage || {} : r.query;
  const picked = groups.find((g) => g.slug === q.group) ?? defaultGroup(groups);
  const lastRan = t.refreshed ? ["Triage last ran ", liveAgo(t.refreshed), ` (${zonedClock(t.refreshed)}).`] : ["Triage has not run yet."];
  fill("triage-list",
    quiet(...lastRan, " It reads every open ticket, says what kind it is and what it needs, sends ready code tickets to the pipeline and research tickets to the research agent."),
    el("div", { class: "actions" }, button(t.refreshing ? "Triage running" : "Refresh triage now", "triage-refresh", refreshTriage, t.refreshing)),
    saidLine("triage-refresh"),
    t.researching ? quiet("The research loop is running, so the research buttons wait until it ends.") : null,
    t.approved?.length ? quiet(`Sent to the pipeline on the last run: ${t.approved.join(", ")}.`) : null,
    Object.entries(t.held || {}).map(([ref, why]) => quiet(`Not sent this time: ${ref}: ${why}`)),
    seg("Groups", groups.map((g) => [g.slug, [g.standing, " ", figure(g.rows.length)]]), picked?.slug ?? "",
      (slug) => go(viewHash("triage", { group: slug === defaultGroup(groups)?.slug ? "" : slug }), "link", { replace: true })),
    picked ? [heading(groups.indexOf(picked) + 1, picked.standing),
      picked.rows.length
        ? table(count(picked.rows.length, "ticket", "tickets"), [["Ticket", "c-ref"], ["Title"], ["Verdict", "c-verdict x"]],
          picked.rows.map((row) => linkedRow(detailHash("ticket", row.ref),
            cell(rowLink("ticket", row.ref, shortRef(row.ref)), "nb"),
            cell([row.title, row.summary ? el("div", { class: "muted", text: row.summary }) : null]),
            cell(row.prs ? [...wholeWords(row.verdict), el("br"), ...pullLinks(row.prs)] : wholeWords(row.verdict)))))
        : quiet("None.")] : quiet("Triage has no groups yet."));
  if (r.detail) drawTicket(groups, r.detail.id, t.researching);
  return t.refreshing || t.researching ? LIVE_MS : SLOW_MS;
}

function drawTicket(groups, ref, researching) {
  const group = groups.find((g) => g.rows.some((row) => row.ref === ref));
  const r = group?.rows.find((row) => row.ref === ref);
  if (!r) {
    fill("triage-detail-head", detailHead("triage-detail", "ticket", ref, ref, [], [null, null]));
    fill("triage-detail-body", saidLine(`ticket:${ref}`), fault(`Triage has no open ticket ${ref}. It may have closed since.`));
    return;
  }
  const list = (items) => el("ol", { class: "questions" }, items.map((item) => el("li", { text: item })));
  const answerable = r.send && r.questions.length;
  fill("triage-detail-head", detailHead("triage-detail", "ticket", ref, r.title || ref, [], neighborsIn(group.rows.map((row) => row.ref), ref)));
  fill("triage-detail-body",
    saidLine(`ticket:${ref}`),
    el("dl", { class: "w" },
      fact("Ticket", link(r.url, ref)), fact("Group", group.standing), fact("Repo", r.repo), fact("Kind", r.kind), fact("Size", r.size),
      fact("Verdict", r.verdict), fact("Pull requests", r.prs ? pullLinks(r.prs) : "")),
    r.summary ? el("p", { text: r.summary }) : null,
    r.questions.length && !answerable ? [subheading("Questions"), list(r.questions)] : null,
    r.send ? [subheading(answerable ? "Answer and send" : "Send"), sendForm(r)] : null,
    r.assumptions.length ? fold(`triage:a:${ref}`, "Show what the build assumes", list(r.assumptions)) : null,
    r.plan.length ? fold(`triage:p:${ref}`, "Show the plan", list(r.plan)) : null,
    r.report ? foldText(`triage:r:${ref}`, "Show the report", r.report) : null,
    researchOffer(r, researching));
}

/* Outcomes */

function niceMax(value, whole) {
  if (value <= 0) return 1;
  const stepSize = 10 ** Math.floor(Math.log10(value));
  const nice = [1, 2, 2.5, 5, 10].map((m) => m * stepSize).find((m) => m >= value && (!whole || Number.isInteger(m)));
  return nice ?? Math.ceil(value);
}

function dayLabel(day) {
  return day.slice(5).replace("-", " ");
}

function dayChart(days, width, height, top, label, dayTickGap) {
  const frame = { left: 58, right: width - 16, top, bottom: height - 32 };
  frame.pitch = (frame.right - frame.left) / days.length;
  frame.X = (i) => frame.left + i * frame.pitch;
  frame.chart = svg("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": label });
  days.forEach((d, i) => {
    if (i % 7 !== (days.length - 1) % 7) return;
    const x = frame.X(i) + frame.pitch / 2, y = frame.bottom + dayTickGap;
    frame.chart.append(svg("line", { class: "tick", x1: x, y1: y, x2: x, y2: y + 5 }));
    frame.chart.append(svg("text", { class: "fig", x, y: y + 20, "text-anchor": i === days.length - 1 ? "end" : "middle" }, dayLabel(d.day)));
  });
  return frame;
}

function valueTicks(frame, values, Y, words) {
  for (const v of values) {
    frame.chart.append(svg("line", { class: "tick", x1: frame.left - 5, y1: Y(v), x2: frame.left, y2: Y(v) }));
    frame.chart.append(svg("text", { class: "fig", x: frame.left - 9, y: Y(v) + 4, "text-anchor": "end" }, words(v)));
  }
}

function bar(frame, i, cls, y, height) {
  frame.chart.append(svg("rect", { class: cls, x: frame.X(i) + frame.pitch * 0.18, y, width: Math.max(1, frame.pitch * 0.64), height }));
}

function paidChart(days, width) {
  const frame = dayChart(days, width, 230, 30, `Paid per day for the last ${days.length} days, with today's bar filled`, 0);
  const { chart, left, right, top, bottom, pitch, X } = frame;
  const max = niceMax(Math.max(...days.map((d) => d.paid)), false);
  const Y = (v) => bottom - (v / max) * (bottom - top);
  valueTicks(frame, [0, max / 2, max], Y, (v) => money(v).replace(".00", ""));
  days.forEach((d, i) => d.paid > 0 && bar(frame, i, i === days.length - 1 ? "frame this" : "frame", Y(d.paid), bottom - Y(d.paid)));
  const today = days[days.length - 1];
  const label = `Today ${money(today.paid)}`;
  const under = days.slice(-Math.ceil((label.length * 7.4) / pitch) - 1);
  const clear = Math.min(...under.map((d) => Y(d.paid)));
  const labelY = Math.max(top - 14, clear - 8);
  chart.append(svg("text", { class: "fig", x: X(days.length - 1) + pitch * 0.82, y: labelY, "text-anchor": "end" }, label));
  const todayX = X(days.length - 1) + pitch / 2, todayTop = Y(today.paid) - 3;
  if (todayTop - labelY > 12) chart.append(svg("line", { class: "tick", x1: todayX, y1: labelY + 5, x2: todayX, y2: todayTop }));
  chart.append(svg("line", { class: "axis", x1: left, y1: top - 6, x2: left, y2: bottom }));
  chart.append(svg("line", { class: "axis", x1: left, y1: bottom, x2: right, y2: bottom }));
  return chart;
}

function outcomeChart(days, width) {
  const frame = dayChart(days, width, 240, 22, `Runs shipped above the line and runs failed below it, per day for the last ${days.length} days`, 6);
  const { chart, left, right, top, bottom } = frame;
  const max = niceMax(Math.max(1, ...days.map((d) => Math.max(d.shipped, d.failed))), true);
  const mid = (top + bottom) / 2;
  const Y = (v) => mid - (v / max) * (mid - top);
  valueTicks(frame, [-max, 0, max], Y, (v) => String(Math.abs(v)));
  days.forEach((d, i) => {
    if (d.shipped) bar(frame, i, "frame", Y(d.shipped), mid - Y(d.shipped));
    if (d.failed) bar(frame, i, "frame below", mid, Y(-d.failed) - mid);
  });
  chart.append(svg("line", { class: "axis", x1: left, y1: top - 4, x2: left, y2: bottom + 4 }));
  chart.append(svg("line", { class: "axis", x1: left, y1: mid, x2: right, y2: mid }));
  chart.append(svg("text", { class: "name", x: left + 8, y: top + 2 }, "SHIPPED"));
  chart.append(svg("text", { class: "name", x: left + 8, y: bottom - 2 }, "FAILED"));
  return chart;
}

function decisionParts(d, days) {
  const p = d.pushes;
  const own = p.auto + d.tickets.triage;
  const byYou = p.approved + p.rejected + d.tickets.owner;
  const total = own + byYou;
  return [
    total ? [
      el("p", { class: "readout" }, percent(own / total), el("small", { text: "went without you" })),
      el("p", {}, `${own} of ${total} decisions in ${days} days needed nothing from you: ${count(p.auto, "push", "pushes")} landed without an approval, ` +
        `and triage sent ${count(d.tickets.triage, "ticket", "tickets")} to the pipeline itself. You approved ${count(p.approved, "push", "pushes")}, ` +
        `rejected ${p.rejected}, and sent ${count(d.tickets.owner, "ticket", "tickets")} yourself.` +
        (p.waiting ? ` ${count(p.waiting, "push waits", "pushes wait")} for you on Live.` : "") +
        (p.other ? ` ${count(p.other, "more push", "more pushes")} ended another way, such as failing its check.` : "")),
    ] : quiet(`Nothing was pushed or sent in ${days} days.`),
    table("", [["What"], ["Without you", "num c-rate"], ["By you", "num c-n"], ["Rejected", "num c-n"], ["Waiting", "num c-n"]], [
      el("tr", {}, cell("Pushes"), cell(p.auto, "num"), cell(p.approved, "num"), cell(p.rejected, "num"), cell(p.waiting, "num")),
      el("tr", {}, cell("Tickets sent to the pipeline"), cell(d.tickets.triage, "num"), cell(d.tickets.owner, "num"), cell("", "num"), cell("", "num")),
    ], { stack: true }),
  ];
}

function paidTable(rows) {
  return rows.length ? table("", [["Name"], ["Calls", "num c-n"], ["Tokens in", "num c-rate x"], ["Tokens out", "num c-rate x"], ["Paid", "num c-cost"]],
    rows.map((r) => el("tr", {}, cell(r.name), cell(r.steps, "num"), cell(r.tokens_in.toLocaleString(), "num"),
      cell(r.tokens_out.toLocaleString(), "num"), cell(money(r.cost_usd), "num")))) : quiet("No paid model call in this window.");
}

function paidParts(t, width) {
  const today = t.days[t.days.length - 1];
  return [
    el("p", { class: "readout" }, money(t.total_paid), el("small", { text: `paid in ${t.days.length} days` })),
    t.total_paid > 0 ? [
      el("p", {}, `Only model calls you pay for count here. A run on the claude CLI rides your subscription, so it adds nothing. Today so far: ${money(today.paid)}.`),
      paidChart(t.days, width),
      el("h3", { text: "By model" }), paidTable(t.by_model),
      el("h3", { text: "By runner" }), paidTable(t.by_runner),
      el("h3", { text: "By loop or kind" }), paidTable(t.by_loop),
    ] : quiet(`Nothing was paid in ${t.days.length} days: every model call ran on the subscription.`),
  ];
}

function drawOutcomes() {
  const panel = outcomesData;
  if (!panel) return SLOW_MS;
  if (!("rows" in panel)) {
    fill("outcomes", fault(`Outcomes could not be read: ${panel.error || "no answer"}.`));
    return SLOW_MS;
  }
  const t = panel.rows;
  const width = Math.max(300, Math.floor(document.getElementById("outcomes").clientWidth || 300));
  const shipped = t.days.reduce((n, d) => n + d.shipped, 0);
  const failed = t.days.reduce((n, d) => n + d.failed, 0);
  const other = t.days.reduce((n, d) => n + d.other, 0);
  const anyPaid = t.repos.some((r) => r.paid > 0);
  const titles = ["Outcomes per day", "Without you or by you", "What you paid", "By repo", "Evals"];
  fill("outcomes",
    jumps(titles),
    heading(1, titles[0]),
    el("p", {}, `${shipped} pipeline runs shipped a pull request and ${failed} failed in ${t.days.length} days.` +
      (other ? ` ${other} more ended another way, such as needing information.` : "")),
    outcomeChart(t.days, width),
    heading(2, titles[1]),
    decisionParts(t.decisions, t.days.length),
    heading(3, titles[2]),
    paidParts(t, width),
    heading(4, titles[3]),
    t.repos.length ? table("",
      [["Repo"], ["Runs", "num c-n"], ["Shipped", "num c-n"], ["Failed", "num c-n x"], ["Pass rate", "num c-rate"], ["Median time", "num c-time x"], ...(anyPaid ? [["Paid", "num c-cost x"]] : [])],
      t.repos.map((r) => el("tr", {}, cell(r.repo), cell(r.runs, "num"), cell(r.shipped, "num"), cell(r.failed, "num"),
        cell(percent(r.pass_rate), "num"), cell(duration(r.median_seconds), "num"), anyPaid ? cell(paidMoney(r.paid), "num") : null)))
      : quiet("No pipeline run finished in this window."),
    heading(5, titles[4]),
    t.evals.length ? table("Mined eval tasks", [["Repo"], ["Tasks", "num c-n"], ["Passed", "num c-n"], ["Pass rate", "num c-rate"], ["Last", "c-time x"]],
      t.evals.map((e) => el("tr", {}, cell(e.repo), cell(e.tasks, "num"), cell(e.passed, "num"), cell(percent(e.pass_rate), "num"), cell(clock(e.last)))))
      : quiet("No eval ran in this window."));
  return SLOW_MS;
}

/* Health */

function checksSummary(h) {
  if (!h.heartbeat) return el("p", {}, stateChip("caution"), " The heartbeat has not run yet, so no check has a result.");
  const failing = h.checks.filter((c) => !c.ok).length;
  const state = !failing ? "ok" : h.checks.some((c) => c.state === "fault") ? "fault" : "caution";
  return el("p", {}, stateChip(state), " ",
    `${h.checks.length} checks, ${failing} failing. The last heartbeat came `, liveAgo(h.heartbeat), ` (${clock(h.heartbeat)}).`);
}

function runChecks(btn) {
  return act(btn, "checks", "/api/checks/run", {},
    () => `Started the checks at ${clock(new Date())}. Their results show here when they end.`);
}

function probeWords(p) {
  if (!p) return "";
  return [p.system, p.detail].filter(Boolean).join(" · ");
}

function deviceRow(d) {
  const p = d.probe;
  const host = d.host || "this machine";
  return el("tr", {},
    cell([el("span", { class: "nb", text: d.name }), d.note && d.note !== host ? el("div", { class: "muted", text: d.note }) : null]),
    cell(d.kind), cell(host, "muted-cell"),
    cell(p ? [stateChip(p.ok ? "ok" : "fault"), el("br"), el("span", { class: "muted" }, liveAgo(p.at))] : "not probed", "nb"),
    cell(probeWords(p) || d.error || "", "muted-cell"),
    cell([p ? (p.tools || []).join(", ") || "none found" : "", p?.gpus?.length ? el("div", { text: p.gpus.join(", ") }) : null]),
    cell(button("Probe", `probe:${d.name}`, (b) => probe(d.name, b)), "probe-cell"));
}

async function probe(name, btn) {
  btn.disabled = true;
  btn.textContent = "Probing";
  try {
    const p = await call(`/api/devices/${encodeURIComponent(name)}/probe`, {});
    sayAt("devices", p.ok ? "ok" : "fault", p.ok ? `${name} answered in ${p.seconds} s${p.system ? `: ${p.system}` : ""}.` : `${name} did not answer: ${p.detail || "no detail"}.`);
  } catch (e) {
    sayAt("devices", "fault", e.message);
  }
  refresh();
}

function failures(h) {
  const rows = [];
  const add = (state, what, name, detail) => rows.push(el("tr", {}, cell(stateChip(state)), cell([el("span", { class: "muted", text: `${what} ` }), name]), cell(detail || "", "muted-cell")));
  for (const a of h.alerts.rows?.alerts ?? []) add("fault", "Alert", a.key, [`opened ${clock(a.opened)}, last seen `, liveAgo(a.last_seen), a.url ? [" · ", link(a.url, `#${a.issue}`)] : null]);
  for (const c of h.checks.rows?.checks ?? []) if (c.state !== "ok") add(c.state, "Check", c.key, c.detail);
  for (const p of h.alerts.rows?.probes ?? []) if (!p.ok) add("fault", "Probe", p.name, p.detail);
  for (const l of h.checks.rows?.loops ?? []) if (!l.ok) add("fault", "Loop", l.name, l.detail);
  for (const [name, why] of Object.entries(h.serve.broken || {})) add("fault", "Loop file", name, why);
  for (const [source, error] of Object.entries(h.serve.poll_errors || {})) add("caution", "Source", source, error);
  for (const d of h.devices.rows ?? []) if (d.probe && !d.probe.ok) add("fault", "Device", d.name, probeWords(d.probe));
  return rows;
}

function healthSummary(h, rows) {
  const unread = [h.checks, h.alerts, h.devices].filter((p) => !("rows" in p)).length;
  const serveDown = !h.serve.at ? "serve has never run" : !h.serve.alive ? "serve is down" : "";
  const parts = [serveDown, rows.length ? `${count(rows.length, "thing fails", "things fail")}` : "", unread ? `${count(unread, "part", "parts")} could not be read` : ""].filter(Boolean);
  return el("p", { class: "summary" }, stateChip(h.state), parts.length ? ` ${parts.join(", ")}.`.replace(/^ ./, (s) => s.toUpperCase()) : " Every alert, check, probe, device and loop answers.");
}

function tally(failing, what, after = "") {
  return el("p", {}, stateChip(failing ? "fault" : "ok"), ` ${what}, ${failing} failing.${after}`);
}

function drawHealth(h) {
  const failed = failures(h);
  const s = h.serve;
  const titles = ["Alerts and failures", "Serve", "Devices", "Checks", "Loops", "Probes"];
  fill("health",
    healthSummary(h, failed),
    jumps(titles),
    heading(1, titles[0]),
    failed.length ? table("Worst first", [["State", "c-status"], ["What"], ["Detail"]], failed, { stack: true })
      : el("p", {}, stateChip("ok"), " No alert is open and nothing fails."),
    heading(2, titles[1]),
    serveLine(s),
    s.at ? el("dl", { class: "w" }, fact("Process", String(s.pid)), fact("Last beat", [liveAgo(s.at), ` (${clock(s.at)})`]),
      fact("Ticks", `${s.ticks}, one every ${s.tick_seconds} s`), fact("Started", clock(s.started))) : null,
    heading(3, titles[2]),
    el("p", { text: "Loops and sessions run on these. A probe checks the device answers and lists its tools; devices.toml in the config folder adds one." }),
    saidLine("devices"),
    panelParts(h.devices, "The devices", (devices) => table("", [["Device", "c-device"], ["Kind", "c-kind x"], ["Host", "c-ref x"], ["State", "c-status"], ["System", "x"], ["Tools", "x"], ["", "c-probe"]], devices.map(deviceRow), { stack: true })),
    heading(4, titles[3]),
    panelParts(h.checks, "The heartbeat", (c) => [checksSummary(c),
      el("div", { class: "actions" }, button(c.running ? "Checks running" : "Run the checks now", "checks-run", runChecks, c.running)),
      saidLine("checks"),
      c.checks.length ? fold("health:checks", `Show all ${c.checks.length} checks`, table("", [["Check"], ["State", "c-status"], ["Detail"]],
        c.checks.map((x) => el("tr", {}, cell(x.key), cell(stateChip(x.state)), cell(x.detail || ""))))) : null]),
    heading(5, titles[4]),
    panelParts(h.checks, "The loops", (c) => (c.loops.length
      ? [tally(c.loops.filter((l) => !l.ok).length, count(c.loops.length, "loop", "loops"), " Each one reports on its own runs."),
        fold("health:loops", `Show all ${c.loops.length} loops`, table("", [["Loop"], ["State", "c-status"], ["Last run", "c-time"], ["Detail", "x"]],
          c.loops.map((l) => el("tr", {}, cell(l.name), cell(stateChip(l.ok ? "ok" : "fault")), cell(liveAgo(l.ts)), cell(l.detail || "")))))]
      : quiet("No loop has reported yet."))),
    heading(6, titles[5]),
    panelParts(h.alerts, "The probes", (a) => (a.probes.length
      ? [tally(a.probes.filter((p) => !p.ok).length, count(a.probes.length, "probe", "probes")),
        fold("health:probes", `Show all ${a.probes.length} probes`, table("", [["Probe"], ["State", "c-status"], ["Last run", "c-time"], ["Last good", "c-time x"], ["Detail", "x"]],
          a.probes.map((p) => el("tr", {}, cell(p.name), cell(stateChip(p.ok ? "ok" : "fault")), cell(liveAgo(p.last_run)), cell(liveAgo(p.last_ok)), cell(p.detail || "")))))]
      : quiet("No probe has run yet."))));
  return h.checks.rows?.running ? LIVE_MS : SLOW_MS;
}

/* Settings */

function defaultModel(data, role) {
  return data.defaults[role] || "claude CLI";
}

function drawSettings(data) {
  settingsData = data;
  for (const [id, key] of RUN_LIMITS) {
    const input = document.getElementById(id);
    if (input.value !== String(data[key])) input.value = data[key];
  }
  fill("model-hint", `Each role takes ${data.aliases.join(", ")}, or a full claude model id. An empty role uses its default.`);
  fill("model-names", data.aliases.map((alias) => el("option", { value: alias })));
  fill("model-fields", Object.entries(data.models).map(([role, model]) => el("div", { class: "field" },
    el("label", { for: `model-${role}`, text: role.replace("_", " ") }),
    el("input", { id: `model-${role}`, "data-role": role, "data-focus": `model-${role}`, type: "text", value: model,
      placeholder: `default: ${defaultModel(data, role)}`, list: "model-names", autocomplete: "off", spellcheck: "false" }))));
}

function settingsChanges() {
  const changes = {};
  for (const [id, key] of RUN_LIMITS) {
    const value = document.getElementById(id).value.trim();
    if (value !== String(settingsData[key])) changes[key] = value;
  }
  const models = {};
  for (const input of document.querySelectorAll("#model-fields input")) {
    if (input.value.trim() !== settingsData.models[input.dataset.role]) models[input.dataset.role] = input.value.trim();
  }
  if (Object.keys(models).length) changes.models = models;
  return changes;
}

async function saveSettings(event) {
  event.preventDefault();
  const changes = settingsChanges();
  if (!Object.keys(changes).length) return say("settings-result", "ok", "Nothing changed, so nothing was saved.");
  trackAction("settings-save");
  const saveButton = document.getElementById("settings-save");
  saveButton.disabled = true;
  try {
    const saved = await call("/api/settings", changes);
    drawSettings(saved);
    const words = [changes.max_parallel ? `runs at once ${saved.max_parallel}` : "",
      changes.max_parallel_per_repo ? `runs per repo ${saved.max_parallel_per_repo}` : "",
      ...Object.keys(changes.models || {}).map((role) => (saved.models[role] ? `${role} ${saved.models[role]}`
        : `${role} back to its default (${defaultModel(saved, role)})`))].filter(Boolean);
    say("settings-result", "ok", `Saved ${words.join(", ")}. The pipeline takes it at its next poll.`);
  } catch (e) {
    say("settings-result", "fault", e.message);
  } finally {
    saveButton.disabled = false;
  }
}

/* Usage */

function viewWords(name) {
  return VIEWS[name]?.label ?? (ITEM_LABELS[name] ? `${ITEM_LABELS[name]} pages` : name);
}

function msWords(ms) {
  return duration(Math.round(ms / 1000));
}

function drawUsage(u) {
  const windowDays = String(u.days);
  fill("usage",
    seg("Window", USAGE_WINDOWS.map((d) => [d, [figure(d), " days"]]), windowDays,
      (d) => go(viewHash("usage", { days: d === DEFAULT_USAGE_WINDOW ? "" : d }), "link", { replace: true })),
    quiet(u.first ? ["Counted since ", zonedClock(u.first), `, over the last ${u.days} days. A visit is one stay on a view or an open item; time counts only while the tab shows.`]
      : `Nothing was counted in the last ${u.days} days.`),
    heading(1, "Views"),
    u.views.length ? table("Most visited first", [["View"], ["Visits", "num c-n"], ["Desktop", "num c-n"], ["Phone", "num c-n"], ["Time", "num c-time x"], ["Median stay", "num c-time"]],
      u.views.map((v) => el("tr", {}, cell(viewWords(v.view)), cell(v.visits, "num"), cell(v.desktop, "num"), cell(v.phone, "num"),
        cell(msWords(v.ms), "num"), cell(msWords(v.median_ms), "num"))))
      : quiet("No visit yet."),
    heading(2, "Ways in"),
    u.ways_in.length ? table("", [["Way in"], ["Visits", "num c-n"]],
      u.ways_in.map((w) => el("tr", {}, cell(WAYS_IN[w.via] ?? w.via), cell(w.visits, "num"))))
      : quiet("No visit yet."),
    heading(3, "Actions"),
    u.actions.length ? table("Buttons and keys you pressed", [["Action"], ["Count", "num c-n"], ["Desktop", "num c-n"], ["Phone", "num c-n"]],
      u.actions.map((a) => el("tr", {}, cell(ACTION_WORDS[a.name] ?? el("code", { text: a.name })), cell(a.count, "num"), cell(a.desktop, "num"), cell(a.phone, "num"))))
      : quiet("No action yet."),
    quiet("Usage keeps view and button names with counts and times only, never what you type or what a page shows, and drops anything older than 90 days."));
  return SLOW_MS;
}

/* Tracking */

function deviceKind() {
  return PHONE.matches ? "phone" : "desktop";
}

function stintName() {
  return route.detail?.kind ?? route.view;
}

function report(row) {
  fetch("/api/usage", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json", "X-Arete": "1" },
    body: JSON.stringify({ tab: TAB, device: deviceKind(), ...row }) }).catch(() => {});
}

function startStint(via) {
  stint = { name: stintName(), via, at: performance.now() };
}

function endStint() {
  if (!stint) return;
  report({ kind: "visit", view: stint.name, via: stint.via, ms: Math.min(DAY_MS, Math.round(performance.now() - stint.at)) });
  stint = null;
}

function trackAction(name) {
  if (route) report({ kind: "action", name, view: stintName() });
}

/* Console */

function setConsole(open, { focus = false, track = true } = {}) {
  const panel = document.getElementById("console");
  const opener = document.getElementById("console-open");
  if (panel.hidden === !open) {
    if (open && focus) focusConsole();
    return;
  }
  panel.hidden = !open;
  document.body.classList.toggle("console-open", open);
  opener.setAttribute("aria-expanded", String(open));
  try {
    localStorage.setItem("arete.console", open ? "open" : "");
  } catch {
    // a browser that keeps no storage opens the console closed next time
  }
  if (open) {
    if (track) trackAction("console-open");
    consoleReturn = focusMark(document.activeElement);
    if (focus) focusConsole();
    pollChat();
    return;
  }
  clearTimeout(chatTimer);
  chatCount++;
  if (panel.contains(document.activeElement) || document.activeElement === document.body) {
    refocus(consoleReturn, opener);
  }
}

function focusConsole() {
  const target = COARSE.matches ? document.getElementById("console-title") : document.getElementById("chat-text");
  target.focus();
}

async function pollChat() {
  clearTimeout(chatTimer);
  const mine = ++chatCount;
  if (document.getElementById("console").hidden || document.hidden) return;
  try {
    const view = await call("/api/chat");
    if (mine !== chatCount) return;
    drawChat(view);
  } catch (e) {
    if (mine !== chatCount) return;
    say("chat-result", "fault", `The chat could not be read: ${e.message}`);
  }
  chatTimer = setTimeout(pollChat, chatBusy ? CHAT_BUSY_MS : SLOW_MS);
}

function drawChat(data) {
  chatBusy = data.busy;
  const seen = `${JSON.stringify(data.messages)}:${data.busy}`;
  document.getElementById("chat-send").disabled = data.busy;
  document.getElementById("chat-new").disabled = data.busy;
  const working = document.getElementById("working");
  working.hidden = !data.busy;
  document.getElementById("draft").hidden = !data.draft;
  if (!data.draft) document.getElementById("draft-result").hidden = true;
  if (data.draft) {
    document.getElementById("draft-title").textContent = data.draft.title;
    document.getElementById("draft-body").textContent = data.draft.body;
  }
  if (data.busy) {
    const asked = [...data.messages].reverse().find((m) => m.from === "you");
    working.textContent = `Working on it · sent ${clock(asked?.ts)} · the answer shows here when it comes`;
  }
  if (seen === chatSeen) return;
  const grew = data.messages.length !== chatLength;
  chatSeen = seen;
  chatLength = data.messages.length;
  fill("messages", data.messages.length ? data.messages.map((m) => el("div", { class: `msg from-${m.from}` },
    el("div", { class: "who" }, withFigures([m.from === "you" ? "You" : "Arete", clock(m.ts), data.paid && m.cost ? money(m.cost) : ""].filter(Boolean).join(" · "))),
    el("p", {}, withLinks(m.text))))
    : quiet("No messages yet. Ask what is running, why a run failed, or what to change."));
  if (grew) {
    const body = document.getElementById("console-body");
    body.scrollTop = body.scrollHeight;
  }
}

async function sendChat(event) {
  event.preventDefault();
  const box = document.getElementById("chat-text");
  const text = box.value.trim();
  if (!text) return say("chat-result", "caution", "Write a message first.");
  trackAction("chat-send");
  document.getElementById("chat-result").hidden = true;
  try {
    drawChat(await call("/api/chat", { text }));
    box.value = "";
    clearTimeout(chatTimer);
    chatTimer = setTimeout(pollChat, CHAT_BUSY_MS);
  } catch (e) {
    say("chat-result", "fault", e.message);
  }
}

async function chatAction(path, btn, resultId, name) {
  trackAction(name);
  document.getElementById(resultId).hidden = true;
  btn.disabled = true;
  const answered = await call(path, {}).catch((e) => say(resultId, "fault", e.message));
  btn.disabled = false;
  if (answered) {
    drawChat(answered);
    if (path.endsWith("/file")) say(resultId, "ok", answered.messages.at(-1)?.text || "Filed the issue.");
  }
}

/* Find */

function paletteItems() {
  const views = Object.entries(VIEWS).map(([view, meta]) => ({ kind: "View", text: meta.label, note: meta.sub, view: true, run: () => go(listHash(view), "palette") }));
  const actions = [
    { kind: "Action", text: "New session", note: "Open the form on Sessions", run: () => {
      go(listHash("sessions"), "palette");
      openLaunch();
    } },
    { kind: "Action", text: "New loop", note: "Draft a loop from a sentence", run: () => {
      go(listHash("loops"), "palette");
      openLoopNew();
    } },
    { kind: "Action", text: "Run the checks now", note: "Health", run: () => paletteAct("/api/checks/run", "health", "checks",
      () => `Started the checks at ${clock(new Date())}. Their results show here when they end.`) },
    { kind: "Action", text: "Refresh triage now", note: "Triage", run: () => paletteAct("/api/triage/refresh", "triage", "triage-refresh",
      () => `Started triage at ${clock(new Date())}. It reads every open ticket, so it can take a few minutes; the verdicts here change when it ends.`) },
    { kind: "Action", text: "Open the console", note: "Ask arete about runs, health or cost", run: () => setConsole(true, { focus: true }) },
  ];
  const found = findData ?? { loops: [], runs: [], sessions: [], tickets: [] };
  return [
    ...views, ...actions,
    ...found.loops.map((l) => ({ kind: "Loop", text: l.name, note: l.description, run: () => go(detailHash("loop", l.name), "palette") })),
    ...found.runs.map((r) => ({ kind: "Run", text: r.ticket || r.id, note: `${r.kind} · ${r.status} · ${r.id}`, run: () => go(detailHash("run", r.id), "palette") })),
    ...found.sessions.map((s) => ({ kind: "Session", text: firstMessage(s.prompt).slice(0, 90), note: `${s.status} · ${s.id}`, run: () => go(detailHash("session", s.id), "palette") })),
    ...found.tickets.map((t) => ({ kind: "Ticket", text: `${shortRef(t.ref)} ${t.title}`, note: t.standing, run: () => go(detailHash("ticket", t.ref), "palette") })),
  ];
}

async function paletteAct(path, view, place, saying) {
  try {
    await call(path, {});
    lastSaid.set(place, ["ok", saying()]);
  } catch (e) {
    lastSaid.set(place, ["fault", e.message]);
  }
  focusAfterDraw = `said:${place}`;
  go(listHash(view), "palette");
}

function drawPalette() {
  const words = document.getElementById("palette-input").value.toLowerCase().split(/\s+/).filter(Boolean);
  paletteShown = paletteItems().filter((item) => {
    const text = `${item.kind} ${item.text} ${item.note}`.toLowerCase();
    return words.every((w) => text.includes(w));
  }).slice(0, PALETTE_MOST);
  paletteActive = Math.min(paletteActive, Math.max(0, paletteShown.length - 1));
  const list = document.getElementById("palette-list");
  list.replaceChildren(...paletteShown.map((item, i) => el("li", {
    id: `palette-option-${i}`, role: "option", "aria-selected": String(i === paletteActive), "data-index": i,
  }, el("span", { class: "tag", text: item.kind }), " ", el("span", { class: "palette-text", text: item.text }),
  item.note ? el("span", { class: "muted palette-note", text: item.note }) : null)));
  if (!paletteShown.length) {
    list.append(el("li", { class: "muted", role: "presentation", text: findError ? `Find could not read arete: ${findError}` : "Nothing matches." }));
  } else if (!findData) {
    list.append(el("li", { class: "muted", role: "presentation", text: findError ? `Only views and actions: ${findError}` : "Reading loops, runs, sessions and tickets" }));
  }
  const input = document.getElementById("palette-input");
  if (paletteShown.length) input.setAttribute("aria-activedescendant", `palette-option-${paletteActive}`);
  else input.removeAttribute("aria-activedescendant");
  document.getElementById(`palette-option-${paletteActive}`)?.scrollIntoView({ block: "nearest" });
}

async function openPalette() {
  const dialog = document.getElementById("palette");
  if (dialog.open) return;
  trackAction("find");
  paletteReturn = focusMark(document.activeElement);
  document.getElementById("palette-input").value = "";
  paletteActive = 0;
  dialog.showModal();
  document.getElementById("palette-input").focus();
  drawPalette();
  if (findData && Date.now() - findAt < FIND_FRESH_MS) return;
  try {
    findData = await call("/api/find");
    findAt = Date.now();
    findError = "";
  } catch (e) {
    findError = e.message;
  }
  if (dialog.open) drawPalette();
}

function choosePalette(index) {
  const item = paletteShown[index];
  if (!item) return;
  paletteReturn = null;
  document.getElementById("palette").close();
  if (item.view) focusAfterDraw = "main";
  item.run();
}

function paletteKeys(event) {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    if (!paletteShown.length) return;
    paletteActive = (paletteActive + (event.key === "ArrowDown" ? 1 : -1) + paletteShown.length) % paletteShown.length;
    drawPalette();
  } else if (event.key === "Enter") {
    event.preventDefault();
    choosePalette(paletteActive);
  }
}

/* Keys */

function inField(node) {
  return Boolean(node?.closest?.("input, textarea, select, [contenteditable]"));
}

function moveRow(delta) {
  if (route.detail) return step(delta);
  const links = [...currentSection().querySelectorAll("a[data-row]")].filter((a) => a.checkVisibility());
  if (!links.length) return;
  const at = links.indexOf(document.activeElement);
  const next = links[at < 0 ? (delta > 0 ? 0 : links.length - 1) : Math.max(0, Math.min(links.length - 1, at + delta))];
  next.focus();
}

function openKeys() {
  keysReturn = focusMark(document.activeElement);
  document.getElementById("keys").showModal();
  document.getElementById("keys-close").focus();
}

function onEscape(event) {
  if (document.querySelector("dialog[open]")) return;
  const panel = document.getElementById("console");
  if (!panel.hidden && panel.contains(document.activeElement)) {
    event.preventDefault();
    setConsole(false);
  } else if (route.detail && !inField(document.activeElement)) {
    event.preventDefault();
    closeDetail();
  } else if (!panel.hidden) {
    event.preventDefault();
    setConsole(false);
  }
}

function onKey(event) {
  if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if (!document.getElementById("keys").open) openPalette();
    return;
  }
  if (event.key === "Escape") return onEscape(event);
  if (event.metaKey || event.ctrlKey || event.altKey || event.defaultPrevented) return;
  if (inField(event.target) || document.querySelector("dialog[open]")) return;
  if (goPending) {
    clearTimeout(goPending);
    goPending = 0;
    const view = GO_KEYS[event.key.toLowerCase()];
    if (!view) return;
    event.preventDefault();
    trackAction("go-key");
    focusAfterDraw = "main";
    go(listHash(view), "key");
    return;
  }
  const keys = {
    "/": openPalette,
    g: () => { goPending = setTimeout(() => { goPending = 0; }, GO_WAIT_MS); },
    c: () => {
      const open = document.getElementById("console").hidden;
      setConsole(open, { focus: open });
    },
    "?": openKeys,
    j: () => moveRow(1),
    k: () => moveRow(-1),
  };
  const run = keys[event.key];
  if (!run) return;
  event.preventDefault();
  run();
}

function onClick(event) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const anchor = event.target.closest("a[href^='#']");
  if (anchor) {
    event.preventDefault();
    go(anchor.getAttribute("href"), anchor.closest("nav.side") ? "sidebar" : "link");
    return;
  }
  const row = event.target.closest("tr[data-href]");
  if (!row || event.target.closest("a, button, input, select, textarea, summary, label, details") || !getSelection().isCollapsed) return;
  go(row.dataset.href, "link");
}

/* Loaders: each fetches, then hands back the function that draws and says when to refresh next. */

const LOADERS = {
  live: async () => {
    const data = await call("/api/live");
    return () => drawLive(data);
  },
  runs: async (r) => {
    const q = r.detail ? lastQuery.runs || {} : r.query;
    const [list, detail] = await Promise.all([call(`/api/runs?${runsQuery(q)}`), r.detail ? settle(loadRunDetail(r.detail.id)) : null]);
    return () => Math.min(drawRuns(list, q), r.detail ? drawRunDetail("runs-detail", detail, "run") : SLOW_MS);
  },
  sessions: async (r) => {
    const [list, detail] = await Promise.all([call("/api/sessions"), r.detail ? settle(loadRunDetail(r.detail.id)) : null]);
    return () => Math.min(drawSessions(list), r.detail ? drawRunDetail("sessions-detail", detail, "session") : SLOW_MS);
  },
  loops: async (r) => {
    const [list, detail] = await Promise.all([call("/api/loops"),
      r.detail ? settle(call(`/api/loops/${encodeURIComponent(r.detail.id)}`)) : null]);
    return () => Math.min(drawLoops(list), r.detail ? drawLoopDetail(detail) : SLOW_MS);
  },
  triage: async (r) => {
    const data = await call("/api/triage");
    return () => {
      const editing = sending.size || document.activeElement?.closest("#triage-detail form");
      if (editing && !forceTriageDraw) return HELD;
      forceTriageDraw = false;
      return drawTriage(data, r);
    };
  },
  outcomes: async () => {
    const data = await call("/api/outcomes");
    return () => {
      outcomesData = data;
      return drawOutcomes();
    };
  },
  health: async () => {
    const data = await call("/api/health");
    return () => drawHealth(data);
  },
  settings: async () => {
    const fresh = await call("/api/settings");
    return () => {
      if (settingsData && Object.keys(settingsChanges()).length) return HELD;
      drawSettings(fresh);
      return SLOW_MS;
    };
  },
  usage: async (r) => {
    const days = USAGE_WINDOWS.includes(r.query.days) ? r.query.days : DEFAULT_USAGE_WINDOW;
    const data = await call(`/api/usage?days=${days}`);
    return () => drawUsage(data);
  },
};

/* Start */

function wireForms() {
  document.getElementById("settings").addEventListener("submit", saveSettings);
  document.getElementById("settings-undo").addEventListener("click", () => {
    if (settingsData) drawSettings(settingsData);
    document.getElementById("settings-result").hidden = true;
    refresh();
  });
  for (const [toggleId, formId, focusId] of [["launch-toggle", "launch", "launch-prompt"], ["loop-new-toggle", "loop-new", "loop-new-sentence"]]) {
    const toggle = document.getElementById(toggleId);
    toggle.dataset.label = toggle.textContent;
    toggle.addEventListener("click", () => {
      trackAction(toggleId);
      toggleForm(toggleId, formId, focusId);
    });
  }
  document.getElementById("launch").addEventListener("submit", launch);
  document.getElementById("loop-new").addEventListener("submit", draftLoop);
  document.getElementById("launch-device").addEventListener("change", repoFollowsDevice);
  document.getElementById("launch-reset").addEventListener("click", async () => {
    document.getElementById("launch").reset();
    document.getElementById("launch-result").hidden = true;
    try {
      fillChoices((await call("/api/sessions")).choices, true);
    } catch (e) {
      say("launch-result", "fault", e.message);
    }
  });
  for (const [fieldId, formId] of [["loop-new-sentence", "loop-new"], ["chat-text", "chat"], ["launch-prompt", "launch"]]) {
    document.getElementById(fieldId).addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) document.getElementById(formId).requestSubmit();
    });
  }
}

function wireConsole() {
  document.getElementById("console-open").addEventListener("click", () => {
    const open = document.getElementById("console").hidden;
    setConsole(open, { focus: open });
  });
  document.getElementById("console-close").addEventListener("click", () => setConsole(false));
  document.getElementById("chat").addEventListener("submit", sendChat);
  for (const [id, path, resultId] of [["chat-new", "/api/chat/new", "chat-result"],
    ["draft-file", "/api/chat/file", "draft-result"], ["draft-drop", "/api/chat/drop", "draft-result"]]) {
    document.getElementById(id).addEventListener("click", (event) => chatAction(path, event.currentTarget, resultId, id));
  }
}

function closeOnBackdrop(dialog) {
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  });
}

function wireDialogs() {
  const palette = document.getElementById("palette");
  const input = document.getElementById("palette-input");
  document.getElementById("find-open").addEventListener("click", openPalette);
  input.addEventListener("input", () => {
    paletteActive = 0;
    drawPalette();
  });
  input.addEventListener("keydown", paletteKeys);
  const list = document.getElementById("palette-list");
  list.addEventListener("mousedown", (event) => event.preventDefault());
  list.addEventListener("click", (event) => {
    const option = event.target.closest("[role=option]");
    if (option) choosePalette(Number(option.dataset.index));
  });
  palette.addEventListener("close", () => {
    refocus(paletteReturn);
    paletteReturn = null;
  });
  document.getElementById("palette-close").addEventListener("click", () => palette.close());
  closeOnBackdrop(palette);
  const keys = document.getElementById("keys");
  document.getElementById("keys-close").addEventListener("click", () => keys.close());
  closeOnBackdrop(keys);
  keys.addEventListener("close", () => refocus(keysReturn));
}

function makePanes() {
  for (const pane of document.querySelectorAll(".detail")) {
    pane.setAttribute("aria-labelledby", `${pane.id}-title`);
    pane.setAttribute("role", "region");
    pane.append(el("div", { class: "detail-head", id: `${pane.id}-head` }), el("div", { class: "detail-body", id: `${pane.id}-body` }), el("div", { id: `${pane.id}-talk` }));
  }
}

let resizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => route?.view === "outcomes" && drawOutcomes(), 150);
});
window.addEventListener("popstate", onHistory);
window.addEventListener("hashchange", onHistory);
window.addEventListener("pagehide", endStint);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    endStint();
    return;
  }
  startStint("return");
  refresh();
  pollChat();
});

document.addEventListener("DOMContentLoaded", () => {
  history.scrollRestoration = "manual";
  makePanes();
  wireForms();
  wireConsole();
  wireDialogs();
  document.addEventListener("keydown", onKey);
  document.addEventListener("click", onClick);
  document.addEventListener("pointermove", (e) => { mouseAt = e.pointerType === "mouse" ? { x: e.clientX, y: e.clientY } : null; }, { passive: true });
  document.documentElement.addEventListener("pointerleave", () => { mouseAt = null; });
  document.getElementById("skip").addEventListener("click", () => document.getElementById("main").focus());
  const asked = location.hash.replace(/^#/, "");
  const first = knownRoute(location.hash);
  history.replaceState({ from: "" }, "", first.hash);
  if (asked === "chat") setConsole(true, { focus: true, track: false });
  else {
    try {
      if (!PHONE.matches && localStorage.getItem("arete.console") === "open") setConsole(true, { track: false });
    } catch {
      // without storage the console starts closed
    }
  }
  applyRoute(first, "start");
});
