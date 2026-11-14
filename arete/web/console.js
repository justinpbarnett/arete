"use strict";

const COARSE = matchMedia("(pointer: coarse)");
let chatSeen = "";
let chatLength = -1;
let chatBusy = false;
let chatTimer = 0;
let chatCount = 0;
let openChat = 0;
let chatPickKey = "";
let chatProjectsKey = "";
const unsent = new Map();
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
    const list = await call("/api/chats");
    if (mine !== chatCount) return;
    if (!list.chats.some((c) => c.id === openChat)) switchChat(list.chats[0]?.id || 0);
    const id = openChat;
    const shown = id ? await call(`/api/chats/${id}`) : null;
    if (mine !== chatCount || id !== openChat) return;
    drawChats(list);
    drawChat(shown);
    chatBusy = list.chats.some((c) => c.busy || c.running_sessions);
  } catch (e) {
    if (mine !== chatCount) return;
    say("chat-result", "fault", `The chat could not be read: ${e.message}`);
  }
  chatTimer = setTimeout(pollChat, chatBusy ? CHAT_BUSY_MS : SLOW_MS);
}

function pollSoon() {
  clearTimeout(chatTimer);
  chatTimer = setTimeout(pollChat, 0);
}

function sessionReport(text) {
  let found;
  try {
    found = JSON.parse(text);
  } catch {
    return null;
  }
  if (!found || typeof found !== "object" || Array.isArray(found) || typeof found.id !== "string") return null;
  const report = found.report && typeof found.report === "object" ? found.report : {};
  const listed = (v) => (Array.isArray(v) ? v : []);
  return {
    id: found.id,
    status: String(found.status || ""),
    note: String(found.note || ""),
    summary: String(report.summary || ""),
    checks: listed(report.checks).map((k) => ({ name: String(k?.name || ""), result: String(k?.result || ""), detail: String(k?.detail || "") })),
    questions: listed(report.open_questions).map(String),
    branch: String(report.branch || ""),
    commit: String(report.commit || ""),
    pr: String(found.pr_url || report.pr_url || ""),
    problems: listed(report.problems).map(String),
    paid: report.paid_usd || 0,
  };
}

function reportLines(r) {
  const lines = [el("p", {}, `${r.id} ended ${r.status}${r.paid ? ` · ${money(r.paid)} paid` : ""}`)];
  if (r.summary) lines.push(el("p", {}, r.summary));
  else if (r.note) lines.push(el("p", {}, r.note));
  if (r.checks.length) lines.push(el("ul", {}, r.checks.map((k) => el("li", {}, `${k.name}: ${k.result}${k.detail ? ` (${k.detail})` : ""}`))));
  if (r.questions.length) lines.push(el("p", {}, "Open questions"), el("ul", {}, r.questions.map((q) => el("li", {}, q))));
  if (r.branch) lines.push(el("p", {}, `Branch ${r.branch}${r.commit ? ` at ${r.commit.slice(0, 10)}` : ""}`));
  if (r.pr) lines.push(el("p", {}, link(r.pr, r.pr)));
  if (r.problems.length) lines.push(el("p", {}, "Problems"), el("ul", {}, r.problems.map((q) => el("li", {}, q))));
  return lines;
}

function messageBody(m) {
  const report = m.sender === "session" ? sessionReport(m.text) : null;
  return report ? el("div", { class: "report" }, reportLines(report)) : el("p", {}, withLinks(m.text));
}

async function stopChatSession(chat, id, btn) {
  btn.disabled = true;
  document.getElementById("chat-sessions-result").hidden = true;
  try {
    await call(`/api/chats/${chat}/sessions/${encodeURIComponent(id)}/stop`, {});
  } catch (e) {
    say("chat-sessions-result", "fault", e.message);
  }
  pollSoon();
}

function drawChatSessions(c) {
  const listed = c.sessions?.sessions || [];
  document.getElementById("chat-sessions").hidden = !listed.length;
  fill("chat-sessions-list", listed.map((s) => el("li", {},
    el("a", { href: detailHash("session", s.id), "data-focus": `chat-session:${s.id}` }, s.id),
    ` ${s.status} · ${s.prompt || "no first message"}${s.paid ? ` · ${money(s.paid)} paid` : ""} `,
    s.status === "running" ? button("Stop", "chat-session-stop", (btn) => stopChatSession(c.id, s.id, btn), { of: s.id }) : null)));
}

function chatLabel(c) {
  const note = c.busy ? "waiting on an answer" : c.running_sessions ? `${c.running_sessions} running` : c.drafted ? "a drafted issue waits" : "";
  return [clock(c.created), c.project || "no project", c.title || "Nothing asked yet", note].filter(Boolean).join(" · ");
}

function drawChats(list) {
  const projectsKey = JSON.stringify(list.projects);
  if (projectsKey !== chatProjectsKey) {
    chatProjectsKey = projectsKey;
    options("chat-project", "No project", list.projects, document.getElementById("chat-project").value);
  }
  const key = `${openChat}:${JSON.stringify(list.chats.map(chatLabel))}`;
  if (key === chatPickKey) return;
  chatPickKey = key;
  const pick = document.getElementById("chat-pick");
  fill("chat-pick", list.chats.length ? list.chats.map((c) => el("option", { value: String(c.id), text: chatLabel(c) }))
    : el("option", { value: "", text: "No chat yet" }));
  pick.value = openChat ? String(openChat) : "";
  pick.disabled = !list.chats.length;
}

function switchChat(id) {
  const box = document.getElementById("chat-text");
  unsent.set(openChat, box.value);
  box.value = unsent.get(id) || "";
  openChat = id;
  chatSeen = "";
  chatLength = -1;
  chatPickKey = "";
  for (const line of ["chat-result", "draft-result"]) document.getElementById(line).hidden = true;
}

function chatAbout(c) {
  const where = c.project ? `on ${c.project}, ${c.folder ? `in ${c.folder}` : "which left the config"}` : `no project, in arete's folder ${c.folder}`;
  const paid = c.unclassified_messages ? `${money(c.spent)} known paid · older payment amounts unknown` : `${money(c.spent)} paid so far`;
  const s = c.sessions;
  const sessions = s ? ` · ${s.running} of ${s.max_sessions} sessions running · ${money(Math.max(s.left, 0))} of ${money(s.budget_usd)} left` : "";
  return `Started ${clock(c.created)} · ${where} · ${paid}${sessions}`;
}

function messageCost(m) {
  if (m.paid_usd != null) return m.paid_usd ? money(m.paid_usd) : "";
  return m.cost ? `${money(m.cost)} reported · payment unknown` : "";
}

function drawChat(c) {
  const about = document.getElementById("chat-about");
  const working = document.getElementById("working");
  document.getElementById("chat").hidden = !c;
  about.hidden = !c;
  working.hidden = !c?.busy;
  document.getElementById("draft").hidden = !c?.draft;
  if (!c?.draft) document.getElementById("draft-result").hidden = true;
  if (!c) {
    chatSeen = "";
    document.getElementById("chat-sessions").hidden = true;
    return fill("messages", quiet("No chat yet. Pick a project, or none, under New chat on and press New chat."));
  }
  if (c.id !== openChat) return;
  about.textContent = chatAbout(c);
  document.getElementById("chat-send").disabled = c.busy;
  if (c.draft) {
    document.getElementById("draft-title").textContent = c.draft.title;
    document.getElementById("draft-body").textContent = c.draft.body;
  }
  if (c.busy) {
    const asked = [...c.messages].reverse().find((m) => m.sender === "you");
    working.textContent = `Working on it · sent ${clock(asked?.ts)} · the answer shows here when it comes`;
  }
  drawChatSessions(c);
  const seen = `${c.id}:${JSON.stringify(c.messages)}:${c.busy}:${JSON.stringify(c.sessions)}`;
  if (seen === chatSeen) return;
  const grew = c.messages.length !== chatLength;
  chatSeen = seen;
  chatLength = c.messages.length;
  fill("messages", c.messages.length ? c.messages.map((m) => el("div", { class: `msg from-${m.sender}` },
    el("div", { class: "who" }, withFigures([{ you: "You", tool: "Tool", session: "Session report" }[m.sender] || "Arete", clock(m.ts), messageCost(m)].filter(Boolean).join(" · "))),
    messageBody(m)))
    : quiet(c.project ? `No messages yet. Ask about ${c.project}'s code, its checks, or how arete works on it.`
      : "No messages yet. Ask what is running, why a run failed, or what to change."));
  if (grew) {
    const body = document.getElementById("console-body");
    body.scrollTop = body.scrollHeight;
    putBackConsoleScroll(body);
  }
}

async function sendChat(event) {
  event.preventDefault();
  const id = openChat;
  if (!id) return say("chat-result", "caution", "Start a chat first.");
  const box = document.getElementById("chat-text");
  const text = box.value.trim();
  if (!text) return say("chat-result", "caution", "Write a message first.");
  trackAction("chat-send");
  document.getElementById("chat-result").hidden = true;
  try {
    await call(`/api/chats/${id}/send`, { text });
    if (id === openChat) box.value = "";
    else unsent.delete(id);
  } catch (e) {
    if (id === openChat) say("chat-result", "fault", e.message);
  }
  pollSoon();
}

async function newChat(event) {
  event.preventDefault();
  trackAction("chat-new");
  const btn = document.getElementById("chat-new");
  btn.disabled = true;
  document.getElementById("chat-new-result").hidden = true;
  try {
    const c = await call("/api/chats/new", { project: document.getElementById("chat-project").value });
    switchChat(c.id);
    await pollChat();
    document.getElementById("chat-text").focus();
  } catch (e) {
    say("chat-new-result", "fault", e.message);
  } finally {
    btn.disabled = false;
  }
}

async function draftAction(action, btn) {
  trackAction(btn.id);
  document.getElementById("draft-result").hidden = true;
  btn.disabled = true;
  try {
    const answered = await call(`/api/chats/${openChat}/${action}`, {});
    drawChat(answered);
    if (action === "file") say("draft-result", "ok", answered.messages.at(-1)?.text || "Filed the issue.");
  } catch (e) {
    say("draft-result", "fault", e.message);
  }
  btn.disabled = false;
  pollSoon();
}
