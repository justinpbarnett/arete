"use strict";

const VIEWS = {
  live: { label: "Live", group: "Work", key: "l", sub: "What runs now, and what waits for you." },
  runs: { label: "Runs", group: "Work", key: "r", sub: "Every run of every kind, the running ones first, then the newest." },
  sessions: { label: "Sessions", group: "Work", key: "s", sub: "Agents you talk to, on the device and model you pick." },
  loops: { label: "Loops", group: "Work", key: "o", sub: "The jobs serve fires on a schedule, from a source, or from a webhook." },
  triage: { label: "Triage", group: "Work", key: "t", sub: "Every open ticket, grouped by what it needs before it can be built." },
  outcomes: { label: "Outcomes", group: "Watch", key: "m", sub: "What shipped, what went without you, and what you paid." },
  health: { label: "Health", group: "Watch", key: "h", sub: "Alerts and failures first, then serve, the devices, the checks and the loops." },
  settings: { label: "Settings", group: "Settings", key: "e", sub: "How many runs go at once, and the model for each role." },
  usage: { label: "Usage", group: "Settings", key: "u", sub: "Which views you open, how you reach them, and what you press." },
};
const ITEM_LABELS = { run: "Run", session: "Session", loop: "Loop", ticket: "Ticket" };
const LIVE_MS = 5000;
const SLOW_MS = 30000;
const CHAT_BUSY_MS = 3000;
const TITLE_CHARS = 90;
const PHONE = matchMedia("(pointer: coarse) and (max-width: 1040px)");
const SVG = "http://www.w3.org/2000/svg";
const URL_IN_TEXT = /(https:\/\/[^\s<>"')\]]*[^\s<>"')\].,;:!?])/g;
const HELD = Symbol("held");
const TOGGLE_FORMS = [{ toggle: "launch-toggle", form: "launch", focus: "launch-prompt" }, { toggle: "loop-new-toggle", form: "loop-new", focus: "loop-new-sentence" }];
const RUN_STATE = { done: "ok", pr: "ok", merged: "ok", noop: "ok", skipped: "ok", "dry-run": "ok", failed: "fault", interrupted: "fault", "needs-info": "caution", stopped: "caution" };

let route = null;
const lastQuery = {};
let focusAfterDraw = "";
let mouseAt = null;
const openQuestions = new Set();
const drafts = new Map();
const lastSaid = new Map();
const saidById = new Map();
const saidBeside = new Map();
const VACATED_SPOT_MS = 1000;
let vacatedSpot = null;
let changesInFlight = 0;

class NoAnswer extends Error {}

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

function capitalized(text) {
  return text.replace(/^./, (first) => first.toUpperCase());
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

function shownSaidLine(place) {
  return currentSection().querySelector(`[data-said="${CSS.escape(place)}"]`);
}

function sayAt(place, state, sentence, { focus = true } = {}) {
  lastSaid.set(place, [state, sentence]);
  const line = shownSaidLine(place);
  if (!line) return;
  sayIn(line, state, sentence);
  placeSaid(line, [line.closest("[id]")]);
  if (focus) line.focus();
}

function keepSaidBeside(place, control) {
  const row = control.closest("tr");
  if (!row) {
    saidBeside.delete(place);
    return;
  }
  const line = shownSaidLine(place);
  saidBeside.set(place, {
    pressed: control.dataset.focus,
    later: rowKeysFrom(row, "nextElementSibling"),
    earlier: rowKeysFrom(row, "previousElementSibling"),
    above: Boolean(line && !line.hidden && line.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING),
  });
}

function rowKeysFrom(row, step) {
  const keys = [];
  for (let other = row[step]; other; other = other[step]) {
    if (!other.matches(".said-row")) keys.push(other.querySelector("[data-focus]")?.dataset.focus);
  }
  return keys.filter(Boolean);
}

function rowHolding(nodes, key) {
  return key ? elementsIn(nodes, `[data-focus="${CSS.escape(key)}"]`)[0]?.closest("tr") : null;
}

function firstRowHolding(nodes, keys) {
  for (const key of keys) {
    const row = rowHolding(nodes, key);
    if (row) return row;
  }
  return null;
}

function placeSaid(line, nodes) {
  delete line.dataset.standsFor;
  const beside = saidBeside.get(line.dataset.said);
  if (!beside || line.hidden) return;
  const acted = rowHolding(nodes, beside.pressed);
  if (!acted) guardVacatedSpot(beside);
  const later = acted ? null : firstRowHolding(nodes, beside.later);
  const neighbour = acted ?? later ?? firstRowHolding(nodes, beside.earlier);
  if (!neighbour) return;
  const holder = line.closest("tr.said-row") ?? el("tr", { class: "said-row" }, el("td", {}, line));
  holder.cells[0].colSpan = neighbour.cells.length;
  if (later || (acted && beside.above)) neighbour.before(holder);
  else neighbour.after(holder);
  if (!acted) line.dataset.standsFor = beside.pressed;
}

function guardVacatedSpot(beside) {
  if (beside.vacated) return;
  beside.vacated = true;
  const { left, top, right, bottom } = rowHolding([currentSection()], beside.pressed)?.getBoundingClientRect() ?? {};
  if (bottom > top) vacatedSpot = { left, top, right, bottom, until: performance.now() + VACATED_SPOT_MS };
}

function forgetVacatedSpot() {
  vacatedSpot = null;
}

function ignorePressOnVacatedSpot(e) {
  const spot = vacatedSpot;
  if (!spot || !e.detail || performance.now() > spot.until) return;
  if (e.clientX < spot.left || e.clientX > spot.right || e.clientY < spot.top || e.clientY > spot.bottom) return;
  e.preventDefault();
  e.stopImmediatePropagation();
}

function saidBesideRows(nodes) {
  for (const line of elementsIn(nodes, "p[data-said]")) placeSaid(line, nodes);
  return nodes.filter((node) => !node.closest?.("tr.said-row"));
}

function say(id, state, sentence) {
  saidById.set(id, [state, sentence]);
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

function wrapAtSlashes(text) {
  return text.split(/(?<=\/)/).flatMap((part, i) => [i ? el("wbr") : null, el("span", { class: "id-part", text: part })]);
}

function pullLinks(urls) {
  const shortName = (url) => url.replace(/^https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)$/, "$1#$2").replace("https://github.com/", "");
  return urls.split(/\s+/).filter(Boolean).flatMap((url, i) => [i ? el("br") : null, link(url, shortName(url))]).filter(Boolean);
}

function table(caption, heads, rows, { stack = false } = {}) {
  const head = el("tr", {}, heads.map(([label, cls]) => el("th", { class: cls, scope: "col", text: label })));
  const carried = heads.map(([, cls]) => {
    const classes = (cls || "").split(" ");
    const short = ["c-time", "c-duration", "c-status"].some((c) => classes.includes(c));
    return [...classes.filter((c) => c === "wide-only" || c === "beside"), ...(short ? ["nb"] : [])];
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
  return el("a", { href: detailHash(kind, id), "data-focus": `row:${kind}:${id}`, "data-row": kind, "data-id": id, "aria-current": open ? "true" : null }, text);
}

function runName(r) {
  return r.ticket ? wholeWords(r.ticket) : r.id;
}

function clampedSummary(text) {
  return text ? el("span", { class: "clamp", title: text, text }) : "";
}

function itemLink(kind, id, text, focus) {
  return el("a", { href: detailHash(kind, id), "data-focus": focus, text });
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

function paidColumn(rows, { onPhone = false } = {}) {
  const shown = rows.some((r) => r.paid > 0);
  return { head: shown ? [["Paid", onPhone ? "num c-cost" : "num c-cost wide-only"]] : [], cell: (r) => (shown ? cell(paidMoney(r.paid), "num") : null) };
}

function submitOnCtrlEnter(event) {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) event.target.form.requestSubmit();
}

function count(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

async function call(path, body) {
  const changes = body !== undefined;
  const options = changes ? { method: "POST", headers: { "Content-Type": "application/json", "X-Arete": "1" }, body: JSON.stringify(body) }
    : { cache: "no-store" };
  if (changes) changesInFlight++;
  try {
    const response = await fetch(path, options).catch(() => { throw new NoAnswer("The connection to arete failed."); });
    noteServedCode(response);
    const data = await response.json().catch(() => ({ error: `The server answered ${response.status} with no JSON.` }));
    if (!response.ok) throw new Error(data.error || `The server answered ${response.status}.`);
    return data;
  } finally {
    // the caller says its result in the microtasks after this returns, so the change still counts until then
    if (changes) setTimeout(() => changesInFlight--);
  }
}

function unreadFault(what, failed) {
  const why = failed?.error || "no answer";
  return fault(`${what} could not be read: ${why}${why.endsWith(".") ? "" : "."}`);
}

function panelParts(panel, what, draw) {
  return panel && "rows" in panel ? draw(panel.rows) : unreadFault(what, panel);
}

function button(text, action, onClick, { of = "", disabled = false } = {}) {
  return el("button", { type: "button", "data-focus": of === "" ? action : `${action}:${of}`, disabled, text, on: { click: (e) => {
    trackAction(action);
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

function jumps(titles) {
  return el("nav", { class: "jumps", "aria-label": "Sections" }, titles.map((title) => button(title, "jump", () => jumpTo(title), { of: title })));
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
  return sayParts(RUN_STATE[status], el("span", { class: "nb", text: status }));
}

function secondsBetween(start, end) {
  return start ? Math.max(0, Math.round(((end ? new Date(end) : new Date()) - new Date(start)) / 1000)) : null;
}

function stopWords(what, r, after) {
  if (r.was !== "gone") return `Asked ${what} to stop. ${after}`;
  return r.ticket ? `${what} had already ended; the next pipeline poll marks it stopped and tells its ticket.` : `${what} had already ended, so it is marked stopped.`;
}

async function act(btn, place, path, body, saying, { meanwhile = null } = {}) {
  btn.disabled = true;
  keepSaidBeside(place, btn);
  if (meanwhile) sayAt(place, null, meanwhile);
  try {
    sayAt(place, "ok", saying(await call(path, body)));
  } catch (e) {
    sayAt(place, "fault", e.message);
  }
  refresh();
}

function toggleForm(toggleId, formId, focusId, open = document.getElementById(formId).hidden) {
  showForm(toggleId, formId, open);
  if (open) document.getElementById(focusId).focus();
}

function showForm(toggleId, formId, open) {
  const toggle = document.getElementById(toggleId);
  document.getElementById(formId).hidden = !open;
  toggle.setAttribute("aria-expanded", String(open));
  toggle.textContent = open ? "Close the form" : toggle.dataset.label;
}
