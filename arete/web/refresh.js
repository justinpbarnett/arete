"use strict";

let timer = 0;
let refreshCount = 0;
let refreshDelay = SLOW_MS;
const lastGood = new Map();

function schedule(ms) {
  clearTimeout(timer);
  if (ms) timer = setTimeout(refresh, ms);
}

function status(state, sentence, nextLine = "") {
  document.getElementById("status").replaceChildren(`${dateWords(new Date())} · `, ...sayParts(state, sentence), ...(nextLine ? [el("br"), nextLine] : []));
}

async function refresh() {
  clearTimeout(timer);
  const mine = ++refreshCount;
  if (document.hidden) return;
  const at = route;
  const label = VIEWS[at.view].label;
  try {
    const { coherent } = await import("./ui-snapshot.js");
    const auxiliary = (path) => () => call(path).catch(() => null);
    const group = await coherent([() => LOADERS[at.view](at), auxiliary("/api/sidebar"), auxiliary("/api/asks")]);
    if (mine !== refreshCount) return;
    if (reloadIfNewCode()) return;
    if (!group) { schedule(LIVE_MS); return; }
    const [draw, sidebar, asks] = group;
    const drawn = draw();
    const held = drawn === HELD;
    if (!held) {
      if (sidebar) drawSidebar(sidebar);
      else clearSidebar();
      if (asks) drawWaiting(asks.asks);
      else hideWaiting();
    }
    const partial = !sidebar || !asks;
    const delay = held ? SLOW_MS : partial ? LIVE_MS : drawn || SLOW_MS;
    refreshDelay = delay;
    if (held) tickLiveAgos(currentSection().querySelectorAll("time.ago"));
    else lastGood.set(at.view, new Date());
    const shown = held ? "held while you edit" : `updated ${new Date().toLocaleTimeString([], { hour12: false })}`;
    status(partial ? "fault" : "", [label, shown, ...(partial ? ["sidebar or questions unavailable"] : []),
      `refreshes every ${delay / 1000} s`].join(" · "), newCodeNote());
    document.querySelectorAll(".readout").forEach((r) => r.classList.remove("stale"));
    afterDraw();
    if (!held && asks) showAskedCard();
    schedule(delay);
  } catch (e) {
    if (mine !== refreshCount) return;
    if (!(e instanceof NoAnswer) && reloadIfNewCode()) return;
    const shownAt = clock(lastGood.get(at.view));
    status("fault", `Arete did not answer: ${e.message}${shownAt ? ` What shows came at ${shownAt}.` : ""}`);
    document.querySelectorAll(".readout").forEach((r) => r.classList.add("stale"));
    schedule(refreshDelay);
  }
}

function setCount(view, n, words) {
  const box = document.getElementById(`count-${view}`);
  const a = box.closest("a");
  box.textContent = n ? String(n) : "";
  if (n) a.setAttribute("aria-label", `${VIEWS[view].label}, ${words(n)}`);
  else a.removeAttribute("aria-label");
}

function clearSidebar() {
  for (const view of ["live", "runs", "triage"]) setCount(view, 0);
  for (const id of ["count-health", "count-console", "serve-line"]) {
    document.getElementById(id).textContent = "";
  }
}

function hideWaiting() {
  document.getElementById("waiting").hidden = true;
  setWaitingCount(0);
}

function drawSidebar(f) {
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
  const failing = Object.entries(serve.failing || {});
  const tickFails = failing.map(([key, f]) => `${key}: ${f.error}`).join("; ");
  if (serve.config_problem || serve.code_problem) return [stateChip("fault"), " Serve up, launching nothing · ", el("span", { class: "nb", text: [serve.config_problem || serve.code_problem, tickFails].filter(Boolean).join("; ") })];
  if (failing.length) return [stateChip(serve.stuck ? "fault" : "caution"), " Serve up, its tick fails · ", el("span", { class: "nb", text: tickFails })];
  const troubled = Object.keys(serve.broken || {}).length + Object.keys(serve.poll_errors || {}).length;
  return [stateChip(troubled ? "caution" : "ok"), " Serve up · ", el("span", { class: "nb", text: `${count(serve.loops, "loop", "loops")} on` })];
}

function dateWords(d) {
  return `${d.getFullYear()} ${pad(d.getMonth() + 1)} ${pad(d.getDate())}`;
}
