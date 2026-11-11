"use strict";

const FIND_FRESH_MS = 30000;
const PALETTE_MOST = 40;
let findData = null;
let findAt = 0;
let findError = "";
let paletteReturn = null;
let paletteShown = [];
let paletteActive = 0;

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
    ...found.sessions.map((s) => ({ kind: "Session", text: firstMessage(s.prompt, TITLE_CHARS), note: `${s.status} · ${s.id}`, run: () => go(detailHash("session", s.id), "palette") })),
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
