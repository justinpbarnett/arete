import { sheet, toggleSheet, writable } from "./ui-keys.js";
import { openPalette, register, pickerModes } from "./ui-palette.js";

const named = { enter: "Enter", escape: "Escape", esc: "Escape", tab: "Tab", comma: ",", space: " ", plus: "+" };
const modifiers = { cmd: "metaKey", meta: "metaKey", ctrl: "ctrlKey", alt: "altKey", shift: "shiftKey" };
const focusable = 'textarea, input, select, button, a[href], [tabindex], [contenteditable]';
const wait = (ms = 40) => new Promise((done) => setTimeout(done, ms));
const root = document.documentElement.dataset;

class Failure extends Error {}

function report(kind, text) {
  console.warn(`replay ${kind}: ${String(text).slice(0, 200)}`);
}

function parseToken(token) {
  const text = token.replace(/ /g, "+");
  const lowered = text.toLowerCase();
  if (named[lowered]) return [{ key: named[lowered] }];
  if (text.length > 1 && text.includes("+") && !text.startsWith("+") && !text.endsWith("+")) {
    const parts = text.split("+");
    const last = parts.pop().toLowerCase();
    const flags = {};
    for (const part of parts) {
      const flag = modifiers[part.toLowerCase()];
      if (!flag) throw new Failure(`unknown modifier ${part}`);
      flags[flag] = true;
    }
    const key = named[last] ?? (flags.shiftKey ? last.toUpperCase() : last);
    return [{ key, ...flags }];
  }
  return [...text].map((key) => ({ key }));
}

function typed(target, key) {
  const multiline = target.tagName === "TEXTAREA" || target.isContentEditable;
  const text = key === "Enter" ? (multiline ? "\n" : "") : key.length === 1 ? key : "";
  if (!text) return;
  if (target.isContentEditable) document.execCommand("insertText", false, text);
  else {
    try { target.setRangeText(text, target.selectionStart, target.selectionEnd, "end"); } catch { target.value += text; }
  }
  target.dispatchEvent(new InputEvent("input", { bubbles: true, data: text, inputType: "insertText" }));
}

function tabbed(target, backwards) {
  const fields = [...document.querySelectorAll(focusable)].filter((el) => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length);
  fields[fields.indexOf(target) + (backwards ? -1 : 1)]?.focus();
}

async function press(spec) {
  const target = document.activeElement ?? document.body;
  const event = new KeyboardEvent("keydown", {
    ...spec, bubbles: true, cancelable: true, view: window,
    shiftKey: spec.shiftKey || /^[A-Z?]$/.test(spec.key),
  });
  target.dispatchEvent(event);
  if (!event.defaultPrevented && writable(target) && !spec.metaKey && !spec.ctrlKey) {
    if (spec.key === "Tab") tabbed(target, event.shiftKey);
    else typed(target, spec.key);
  }
  await wait();
}

async function replayKeys(value) {
  let count = 0;
  for (const token of value.split(",")) {
    if (!token) continue;
    for (const spec of parseToken(token)) {
      await press(spec);
      count += 1;
      root.uiReplayCount = String(count);
    }
  }
}

function resolve(act, nth) {
  return document.querySelectorAll(`[data-act="${CSS.escape(act)}"]`)[nth] ?? null;
}

async function replayClicks() {
  const seen = new Map();
  const targets = [...document.querySelectorAll("[data-act]")].map((el) => {
    const nth = seen.get(el.dataset.act) ?? 0;
    seen.set(el.dataset.act, nth + 1);
    return [el.dataset.act, nth];
  });
  const start = location.hash;
  let count = 0;
  for (const [act, nth] of targets) {
    if (sheet.value) toggleSheet();
    if (location.hash !== start) location.hash = start;
    await wait();
    const el = resolve(act, nth);
    if (!el) throw new Failure(`no element for data-act ${act}`);
    el.click();
    await wait();
    count += 1;
    root.uiReplayCount = String(count);
  }
  if (sheet.value) toggleSheet();
  if (location.hash !== start) location.hash = start;
  await wait();
}

function center(el) {
  const box = el.getBoundingClientRect();
  return [box.left + box.width / 2, box.top + box.height / 2];
}

function lookup(selector) {
  let el;
  try { el = document.querySelector(selector); } catch { throw new Failure(`invalid selector ${selector}`); }
  if (!el) throw new Failure(`nothing matches ${selector}`);
  return el;
}

function pointer(type, x, y, target) {
  const init = { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, button: 0,
    buttons: type === "up" ? 0 : 1, pointerId: 1, pointerType: "mouse", isPrimary: true };
  target.dispatchEvent(new PointerEvent(`pointer${type}`, init));
  target.dispatchEvent(new MouseEvent(`mouse${type}`, init));
}

async function replayDrag(value) {
  for (const one of value.split("|")) {
    const at = one.lastIndexOf(">");
    if (at < 1) throw new Failure(`drag needs from>to, not ${one}`);
    const from = lookup(one.slice(0, at));
    const destination = one.slice(at + 1).trim();
    const [x, y] = center(from);
    const offset = /^([+-]?\d+),([+-]?\d+)$/.exec(destination);
    const [tx, ty] = offset ? [x + Number(offset[1]), y + Number(offset[2])] : center(lookup(destination));
    pointer("down", x, y, from);
    for (let i = 1; i <= 6; i += 1) pointer("move", x + (tx - x) * i / 6, y + (ty - y) * i / 6, document.elementFromPoint(tx, ty) ?? from);
    pointer("up", tx, ty, document.elementFromPoint(tx, ty) ?? from);
    await wait();
  }
}

async function replayWheel(value) {
  for (const one of value.split("|")) {
    const match = /^(.*),([+-]?\d+),([+-]?\d+)(,ctrl)?$/.exec(one);
    if (!match) throw new Failure(`wheel needs selector,dx,dy[,ctrl], not ${one}`);
    const el = lookup(match[1]);
    const [x, y] = center(el);
    el.dispatchEvent(new WheelEvent("wheel", { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y,
      deltaX: Number(match[2]), deltaY: Number(match[3]), deltaMode: 0, ctrlKey: Boolean(match[4]) }));
    await wait();
  }
}

function readQuery() {
  const found = new Map();
  for (const pair of location.search.replace(/^\?/, "").split("&")) {
    if (!pair) continue;
    const at = pair.indexOf("=");
    const name = at < 0 ? pair : pair.slice(0, at);
    let value = at < 0 ? "" : pair.slice(at + 1);
    try { value = decodeURIComponent(value); } catch { value = pair.slice(at + 1); }
    found.set(name, value);
  }
  return found;
}

export function paletteFixture(mode) {
  const picked = (row) => { root.uiPaletteResult = row.id; };
  const sample = (id, title, group, description = "") => ({ id, title, group, description, run: picked });
  if (mode === "registry") {
    register("floor", [sample("sample-item", "Sample item <literal>", "Items", "<b>plain text</b>")],
      [sample("settings-sample", "Settings sample", "Commands"), sample("display-sample", "Display sample", "Commands")]);
    register("triage", [], [sample("triage-only", "Triage-only command", "Commands")]);
    openPalette();
    return;
  }
  if (!pickerModes[mode]) return;
  const fixtures = {
    steps: [sample("implement", "Implement", "Agent", "Build the change"), sample("review", "Review", "Agent"), sample("check", "Run checks", "Script")],
    triggers: [sample("manual", "Manual", "Triggers"), sample("issue", "Issue opened", "Triggers")],
    prompts: [sample("plan", "arete/prompts/plan.md", "This ADW"), sample("build", "arete/prompts/build.md", "This ADW", "Build the change"),
      sample("review", "arete/prompts/review.md", "Other ADWs"), sample("loop", "loops/maintain.md", "Loops"), sample("system", "arete/system.md", "Arete")],
    roles: [sample("builder", "Builder", "Roles"), sample("reviewer", "Reviewer", "Roles")],
    drop: [sample("implement", "Implement", "Agent", "Compatible input"), sample("check", "Run checks", "Script")],
    wires: [sample("review-input", "Review · change", "Inputs"), sample("check-input", "Run checks · source", "Inputs"), sample("done", "Done", "Flow")],
    create: [sample("device", "Device", "Create", "a VM or box that runs work"), sample("connector", "Connector", "Create", "an account linked to a system")],
  };
  openPalette({ mode, rows: fixtures[mode], current: mode === "prompts" ? "build" : mode === "roles" ? "default" : undefined, onPick: picked });
}

export async function startHooks() {
  const query = readQuery();
  paletteFixture(query.get("palette"));
  const jobs = [["keys", replayKeys], ["drag", replayDrag], ["wheel", replayWheel]]
    .filter(([name]) => query.has(name)).map(([name, run]) => [name, () => run(query.get(name))]);
  if (query.has("clicks")) jobs.push(["clicks", replayClicks]);
  if (!jobs.length) return;
  root.uiReplay = "running";
  root.uiReplayCount = "0";
  await wait(100);
  for (const [name, run] of jobs) {
    try {
      await run();
    } catch (error) {
      report(name, error instanceof Failure ? error.message : `unexpected ${error?.message ?? error}`);
      root.uiReplay = "failed";
      root.uiReplayKind = name;
      return;
    }
  }
  root.uiReplay = "done";
  root.uiReplayKind = jobs.map(([name]) => name).join(",");
}
