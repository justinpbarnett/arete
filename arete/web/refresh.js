"use strict";

let timer = 0;
let refreshCount = 0;
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
    const [draw, sidebar] = await Promise.all([LOADERS[at.view](at), call("/api/sidebar").catch(() => null)]);
    if (mine !== refreshCount || reloadsForNewCode()) return;
    if (sidebar) drawSidebar(sidebar);
    const drawn = draw();
    const held = drawn === HELD;
    const delay = held ? SLOW_MS : drawn || SLOW_MS;
    if (held) tickLiveAgos(currentSection().querySelectorAll("time.ago"));
    else lastGood.set(at.view, new Date());
    const shown = held ? "held while you edit" : `updated ${new Date().toLocaleTimeString([], { hour12: false })}`;
    status("", [label, shown, `refreshes every ${delay / 1000} s`].join(" · "), newCodeNote());
    document.querySelectorAll(".readout").forEach((r) => r.classList.remove("stale"));
    afterDraw();
    schedule(delay);
  } catch (e) {
    if (mine !== refreshCount || reloadsForNewCode()) return;
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
  const troubled = Object.keys(serve.broken || {}).length + Object.keys(serve.poll_errors || {}).length;
  return [stateChip(troubled ? "caution" : "ok"), " Serve up · ", el("span", { class: "nb", text: `${count(serve.loops, "loop", "loops")} on` })];
}

function dateWords(d) {
  return `${d.getFullYear()} ${pad(d.getMonth() + 1)} ${pad(d.getDate())}`;
}
