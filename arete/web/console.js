"use strict";

const COARSE = matchMedia("(pointer: coarse)");
let chatSeen = "";
let chatLength = -1;
let chatBusy = false;
let chatTimer = 0;
let chatCount = 0;
let consoleReturn = null;

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
    el("div", { class: "who" }, withFigures([m.from === "you" ? "You" : "Arete", clock(m.ts), data.metered && m.cost ? money(m.cost) : ""].filter(Boolean).join(" · "))),
    el("p", {}, withLinks(m.text))))
    : quiet("No messages yet. Ask what is running, why a run failed, or what to change."));
  if (grew) {
    const body = document.getElementById("console-body");
    body.scrollTop = body.scrollHeight;
    putBackConsoleScroll(body);
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
