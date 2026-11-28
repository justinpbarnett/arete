import { h, Component } from "./vendor-preact.js";
import { signal, effect } from "./vendor-signals.js";
import htm from "./vendor-htm.js";
import { webFetch } from "./ui-request.js";
import { prepareWrite, sendPublication, sendWrite } from "./ui-write.js";
import { notice } from "./ui-notice.js";
import { safeLink } from "./ui-operations.js";

const html = htm.bind(h);

const BUSY_MS = 3000;
const IDLE_MS = 30000;
const OPEN_KEY = "arete.console";
const CHAT_KEY = "arete.console.chat";
const UNSENT_KEY = "arete.console.unsent";
const MAX_FILE_CHARS = 16384;
const MAX_ATTACH_CHARS = 32768;
const URL_IN_TEXT = /(https:\/\/[^\s<>"')\]]*[^\s<>"')\].,;:!?])/g;

function readStore(key, fallback = "") {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function writeStore(key, value) {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    return;
  }
}

function readUnsent() {
  try {
    const parsed = JSON.parse(localStorage.getItem(UNSENT_KEY) || "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function writeUnsent(map) {
  try {
    const kept = {};
    for (const [key, value] of Object.entries(map)) {
      if (typeof value === "string" && value) kept[key] = value;
    }
    if (Object.keys(kept).length) localStorage.setItem(UNSENT_KEY, JSON.stringify(kept));
    else localStorage.removeItem(UNSENT_KEY);
  } catch {
    return;
  }
}

function initialState() {
  return {
    chats: [],
    projects: [],
    openChat: 0,
    chat: null,
    loaded: false,
    busy: false,
    readError: "",
    unsent: {},
    attachments: {},
    newProject: "",
    sending: false,
    creating: false,
    chatResult: null,
    newResult: null,
    draftBusy: 0,
    draftResult: null,
    sessionPrompt: "",
    startingSession: false,
    stopping: new Set(),
    sessionResult: null,
    dropping: false,
  };
}

export const consoleOpen = signal(false);
export const consoleState = signal(initialState());
export const consoleCommand = {
  id: "chat-with-arete",
  title: "Chat with Arete",
  description: "open or close the right chat dock",
  key: "gc",
  glyph: "▸",
  run: () => openConsole(),
};

let pollTimer = 0;
let pollToken = 0;
let opener = null;
let started = false;
const pendingDrafts = new Set();
const stoppingSessions = new Set();

function patch(change) {
  consoleState.value = { ...consoleState.peek(), ...change };
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function clock(ts) {
  if (!ts) return "";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "";
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return date.toDateString() === new Date().toDateString() ? time : `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}`;
}

function money(n) {
  const value = Number(n || 0);
  return value > 0 && value < 0.005 ? "<$0.01" : `$${value.toFixed(2)}`;
}

function byteSize(n) {
  const bytes = Number(n || 0);
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

function withLinks(text) {
  return String(text ?? "").split(URL_IN_TEXT).map((part, index) => {
    if (index % 2 === 0) return part;
    const safe = safeLink(part);
    return safe ? html`<a key=${index} href=${safe} target="_blank" rel="noopener noreferrer">${part}</a>` : part;
  });
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
  const listed = (value) => (Array.isArray(value) ? value : []);
  return {
    id: found.id,
    status: String(found.status || ""),
    note: String(found.note || ""),
    summary: String(report.summary || ""),
    checks: listed(report.checks).map((item) => ({
      name: String(item?.name || ""),
      result: String(item?.result || ""),
      detail: String(item?.detail || ""),
    })),
    questions: listed(report.open_questions).map(String),
    branch: String(report.branch || ""),
    commit: String(report.commit || ""),
    pr: String(found.pr_url || report.pr_url || ""),
    problems: listed(report.problems).map(String),
    paid: report.paid_usd || 0,
  };
}

export function chatLabel(chat) {
  const note = chat.busy ? "waiting on an answer"
    : chat.running_sessions ? `${chat.running_sessions} running`
      : chat.drafted ? "a drafted issue waits" : "";
  return [clock(chat.created), chat.project || "no project", chat.title || "Nothing asked yet", note].filter(Boolean).join(" · ");
}

function chatAbout(chat) {
  const where = chat.project
    ? `on ${chat.project}, ${chat.folder ? `in ${chat.folder}` : "which left the config"}`
    : `no project, in arete's folder ${chat.folder}`;
  const paid = chat.unclassified_messages
    ? `${money(chat.spent)} known paid · older payment amounts unknown`
    : `${money(chat.spent)} paid so far`;
  const sessions = chat.sessions
    ? ` · ${chat.sessions.running} of ${chat.sessions.max_sessions} sessions running · ${money(Math.max(chat.sessions.left, 0))} of ${money(chat.sessions.budget_usd)} left`
    : "";
  return `Started ${clock(chat.created)} · ${where} · ${paid}${sessions}`;
}

function messageCost(message) {
  if (message.paid_usd != null) return message.paid_usd ? money(message.paid_usd) : "";
  return message.cost ? `${money(message.cost)} reported · payment unknown` : "";
}

async function readJson(url) {
  let response;
  try {
    response = await webFetch(url, { headers: { Accept: "application/json" } });
  } catch (cause) {
    throw new Error(`Request failed: ${cause.message}`);
  }
  const body = await response.json().catch(() => null);
  const valid = body && typeof body === "object" && !Array.isArray(body);
  if (!response.ok) {
    throw new Error(typeof body?.error === "string" ? body.error : `Request failed (${response.status})`);
  }
  if (!valid) throw new Error(`Invalid response (${response.status})`);
  return body;
}

function stopPolling() {
  clearTimeout(pollTimer);
  pollTimer = 0;
  pollToken += 1;
}

export function pollSoon() {
  clearTimeout(pollTimer);
  pollTimer = 0;
  if (!consoleOpen.peek() || globalThis.document?.hidden) return;
  pollTimer = setTimeout(() => { pollChat(); }, 0);
}

export async function pollChat() {
  clearTimeout(pollTimer);
  pollTimer = 0;
  const mine = ++pollToken;
  if (!consoleOpen.peek() || globalThis.document?.hidden) return;
  let busy = false;
  try {
    const list = await readJson("/api/chats");
    if (mine !== pollToken || !consoleOpen.peek()) return;
    const chats = Array.isArray(list.chats) ? list.chats : [];
    const projects = Array.isArray(list.projects) ? list.projects : [];
    let activeId = consoleState.peek().openChat;
    if (!chats.some((item) => item.id === activeId)) {
      const nextId = chats[0]?.id || 0;
      const previous = consoleState.peek();
      const unsent = { ...previous.unsent };
      if (!activeId && nextId && unsent["0"] && !unsent[String(nextId)]) {
        unsent[String(nextId)] = unsent["0"];
        delete unsent["0"];
        writeUnsent(unsent);
      }
      activeId = nextId;
      patch({ openChat: activeId, unsent, chat: null, draftResult: null, chatResult: null, sessionResult: null });
      writeStore(CHAT_KEY, activeId ? String(activeId) : "");
    }
    const shown = activeId ? await readJson(`/api/chats/${activeId}`) : null;
    if (mine !== pollToken || !consoleOpen.peek() || activeId !== consoleState.peek().openChat) return;
    busy = Boolean(shown?.busy || chats.some((item) => item.busy || item.running_sessions));
    patch({ chats, projects, chat: shown, loaded: true, busy, readError: "" });
  } catch (error) {
    if (mine !== pollToken || !consoleOpen.peek()) return;
    patch({ readError: `The chat could not be read: ${error.message}` });
  }
  if (mine !== pollToken || !consoleOpen.peek() || globalThis.document?.hidden) return;
  pollTimer = setTimeout(() => { pollChat(); }, busy ? BUSY_MS : IDLE_MS);
}

export function switchChat(id) {
  const nextId = Number(id || 0);
  const current = consoleState.peek();
  if (nextId === current.openChat) return;
  const unsent = { ...current.unsent };
  if (!current.openChat && nextId && unsent["0"] && !unsent[String(nextId)]) {
    unsent[String(nextId)] = unsent["0"];
    delete unsent["0"];
    writeUnsent(unsent);
  }
  patch({ openChat: nextId, unsent, chat: null, chatResult: null, draftResult: null, sessionResult: null });
  writeStore(CHAT_KEY, nextId ? String(nextId) : "");
  pollSoon();
}

export function setUnsentText(value) {
  const current = consoleState.peek();
  const key = String(current.openChat || 0);
  const next = { ...current.unsent };
  if (value) next[key] = value;
  else delete next[key];
  patch({ unsent: next });
  writeUnsent(next);
}

async function readDroppedFile(file) {
  const info = {
    name: String(file.name || "file"),
    size: Number(file.size || 0),
    type: String(file.type || ""),
    text: "",
    truncated: false,
  };
  const binary = /^(image|audio|video)\//i.test(info.type)
    || /\.(png|jpe?g|gif|webp|pdf|zip|gz|tar|wasm|mp4|webm|mov)$/i.test(info.name);
  if (!binary && typeof file.text === "function") {
    try {
      const raw = await file.text();
      if (!raw.includes("\0")) {
        info.truncated = raw.length > MAX_FILE_CHARS;
        info.text = raw.slice(0, MAX_FILE_CHARS);
      }
    } catch {
      info.text = "";
    }
  }
  return info;
}

export async function attachFiles(fileList) {
  const files = [...(fileList ?? [])].filter(Boolean);
  if (!files.length) return;
  const loaded = await Promise.all(files.map(readDroppedFile));
  const current = consoleState.peek();
  const key = String(current.openChat || 0);
  const existing = current.attachments[key] ?? [];
  patch({ attachments: { ...current.attachments, [key]: [...existing, ...loaded] }, chatResult: null });
}

export function removeAttachment(index) {
  const current = consoleState.peek();
  const key = String(current.openChat || 0);
  const existing = current.attachments[key] ?? [];
  const kept = existing.filter((_, at) => at !== index);
  const next = { ...current.attachments };
  if (kept.length) next[key] = kept;
  else delete next[key];
  patch({ attachments: next });
}

export function composeMessage(text, attached = []) {
  const parts = [];
  const trimmed = String(text ?? "").trim();
  if (trimmed) parts.push(trimmed);
  let remaining = MAX_ATTACH_CHARS;
  for (const file of attached) {
    const detail = [file.type, byteSize(file.size)].filter(Boolean).join(", ");
    const header = `[Attached file: ${file.name} (${detail})]`;
    if (file.text && remaining > 0) {
      const snippet = file.text.slice(0, remaining);
      remaining -= snippet.length;
      const fence = snippet.includes("```") ? "~~~~" : "```";
      const note = file.truncated || snippet.length < file.text.length ? "\n(truncated)" : "";
      parts.push(`${header}\n${fence}\n${snippet}${note}\n${fence}`);
    } else {
      parts.push(header);
    }
  }
  return parts.join("\n\n");
}

export async function sendChatMessage() {
  const state = consoleState.peek();
  const id = state.openChat;
  if (!id) {
    patch({ chatResult: { kind: "caution", text: "Start a chat first." } });
    return false;
  }
  const key = String(id);
  const text = composeMessage(state.unsent[key] ?? "", state.attachments[key] ?? []);
  if (!text) {
    patch({ chatResult: { kind: "caution", text: "Write a message first." } });
    return false;
  }
  patch({ sending: true, chatResult: null });
  try {
    const answered = await sendWrite(prepareWrite(`/api/v1/chats/${id}/messages`, { text }));
    const latest = consoleState.peek();
    const nextUnsent = { ...latest.unsent };
    delete nextUnsent[key];
    writeUnsent(nextUnsent);
    const nextAttachments = { ...latest.attachments };
    delete nextAttachments[key];
    const updated = answered.chat && latest.openChat === id
      ? { ...(latest.chat ?? {}), ...answered.chat, busy: Boolean(answered.chat.busy ?? true) }
      : latest.chat;
    patch({ unsent: nextUnsent, attachments: nextAttachments, chat: updated, busy: true });
    pollSoon();
    return true;
  } catch (error) {
    if (consoleState.peek().openChat === id) {
      patch({ chatResult: { kind: "fault", text: error.message } });
    }
    return false;
  } finally {
    patch({ sending: false });
  }
}

export async function createChat(project = consoleState.peek().newProject) {
  patch({ creating: true, newResult: null });
  try {
    const created = await sendWrite(prepareWrite("/api/v1/chats", { project: String(project || "") }));
    const id = Number(created.chat?.id ?? created.id ?? 0);
    if (id) {
      switchChat(id);
      await pollChat();
      globalThis.document?.getElementById("ui-chat-text")?.focus();
    }
    return id;
  } catch (error) {
    patch({ newResult: { kind: "fault", text: error.message } });
    return 0;
  } finally {
    patch({ creating: false });
  }
}

async function actOnDraft(action) {
  const state = consoleState.peek();
  const draft = state.chat?.draft;
  if (!draft || draft.chat !== state.openChat || pendingDrafts.has(draft.chat)) return false;
  const id = draft.chat;
  pendingDrafts.add(id);
  patch({ draftBusy: id, draftResult: null });
  try {
    const answered = action === "file"
      ? await sendPublication(`/api/chats/${id}/file`, { draft: draft.fingerprint })
      : await sendWrite(prepareWrite(`/api/v1/chats/${id}`, {}, null, { method: "DELETE" }));
    if (consoleState.peek().openChat === id) {
      const updated = action === "file" ? answered : (answered.chat ?? { ...state.chat, draft: null });
      const message = action === "file"
        ? (answered.messages?.at(-1)?.text || "Filed the issue.")
        : "Dropped the drafted issue.";
      patch({ chat: updated, draftResult: { kind: "ok", text: message } });
      notice.value = message;
    }
    return true;
  } catch (error) {
    if (consoleState.peek().openChat === id) patch({ draftResult: { kind: "fault", text: error.message } });
    return false;
  } finally {
    pendingDrafts.delete(id);
    patch({ draftBusy: 0 });
    if (consoleState.peek().openChat === id) {
      pollToken += 1;
      pollSoon();
    }
  }
}

export const fileChatDraft = () => actOnDraft("file");
export const dropChatDraft = () => actOnDraft("drop");

export async function startChatSession(promptText = consoleState.peek().sessionPrompt) {
  const id = consoleState.peek().openChat;
  const prompt = String(promptText ?? "").trim();
  if (!id || !prompt) {
    patch({ sessionResult: { kind: "caution", text: "Write a session prompt first." } });
    return false;
  }
  patch({ startingSession: true, sessionResult: null });
  try {
    const answered = await sendWrite(prepareWrite(`/api/v1/chats/${id}/sessions`, { prompt }));
    const startedId = answered.session?.id ?? answered.run?.id ?? "";
    const message = startedId ? `Started session ${startedId}.` : "Started a session.";
    patch({ sessionPrompt: "", sessionResult: { kind: "ok", text: message } });
    notice.value = message;
    pollSoon();
    return true;
  } catch (error) {
    patch({ sessionResult: { kind: "fault", text: error.message } });
    return false;
  } finally {
    patch({ startingSession: false });
  }
}

export async function stopChatSession(chatId, sessionId) {
  const key = `${chatId}:${sessionId}`;
  if (stoppingSessions.has(key)) return false;
  stoppingSessions.add(key);
  patch({ stopping: new Set(stoppingSessions), sessionResult: null });
  try {
    await sendWrite(prepareWrite(`/api/chats/${chatId}/sessions/${encodeURIComponent(sessionId)}/stop`, {}));
    notice.value = `Stopping session ${sessionId}.`;
    pollSoon();
    return true;
  } catch (error) {
    patch({ sessionResult: { kind: "fault", text: error.message } });
    return false;
  } finally {
    stoppingSessions.delete(key);
    patch({ stopping: new Set(stoppingSessions) });
  }
}

export function openConsole({ focus = true } = {}) {
  if (!consoleOpen.peek()) {
    opener = globalThis.document?.activeElement ?? null;
    consoleOpen.value = true;
    writeStore(OPEN_KEY, "open");
    pollChat();
  }
  if (focus) {
    queueMicrotask(() => {
      globalThis.document?.querySelector(".console-dock")?.focus({ preventScroll: true });
    });
  }
}

export function closeConsole() {
  if (!consoleOpen.peek()) return false;
  const dock = globalThis.document?.querySelector(".console-dock");
  const active = globalThis.document?.activeElement;
  const hadFocus = Boolean(dock && (dock.contains(active) || active === globalThis.document?.body));
  consoleOpen.value = false;
  writeStore(OPEN_KEY, "");
  stopPolling();
  const back = opener;
  opener = null;
  if (hadFocus && back?.isConnected) back.focus({ preventScroll: true });
  return true;
}

export function toggleConsole() {
  if (consoleOpen.peek()) closeConsole();
  else openConsole();
}

export function startConsole() {
  const open = readStore(OPEN_KEY) === "open";
  const savedChat = Number(readStore(CHAT_KEY) || 0);
  const openChat = Number.isInteger(savedChat) && savedChat > 0 ? savedChat : 0;
  patch({ openChat, unsent: readUnsent() });
  if (!started && globalThis.document?.addEventListener) {
    started = true;
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopPolling();
      else if (consoleOpen.peek()) pollSoon();
    });
  }
  if (open) {
    consoleOpen.value = true;
    pollChat();
  } else {
    consoleOpen.value = false;
    stopPolling();
  }
}

function MessageBody({ message }) {
  const report = message.sender === "session" ? sessionReport(message.text) : null;
  if (!report) return html`<p class="console-text">${withLinks(message.text)}</p>`;
  const prHref = safeLink(report.pr);
  return html`<div class="console-report">
    <p>${report.id} ended ${report.status}${report.paid ? ` · ${money(report.paid)} paid` : ""}</p>
    ${report.summary ? html`<p>${report.summary}</p>` : report.note ? html`<p>${report.note}</p>` : null}
    ${report.checks.length ? html`<ul>${report.checks.map((item, index) => html`<li key=${index}>${item.name}: ${item.result}${item.detail ? ` (${item.detail})` : ""}</li>`)}</ul>` : null}
    ${report.questions.length ? html`<p>Open questions</p><ul>${report.questions.map((item, index) => html`<li key=${index}>${item}</li>`)}</ul>` : null}
    ${report.branch ? html`<p>Branch ${report.branch}${report.commit ? ` at ${report.commit.slice(0, 10)}` : ""}</p>` : null}
    ${prHref ? html`<p><a href=${prHref} target="_blank" rel="noopener noreferrer">${report.pr}</a></p>` : null}
    ${report.problems.length ? html`<p>Problems</p><ul>${report.problems.map((item, index) => html`<li key=${index}>${item}</li>`)}</ul>` : null}
  </div>`;
}

function ResultLine({ result }) {
  if (!result?.text) return null;
  const cls = result.kind === "fault" ? "console-result fault" : result.kind === "caution" ? "console-result caution" : "console-result";
  return html`<p class=${cls} role=${result.kind === "fault" ? "alert" : "status"}>${withLinks(result.text)}</p>`;
}

export class ConsoleDock extends Component {
  state = { revision: 0 };
  dragDepth = 0;
  lastCount = -1;
  lastChat = 0;

  componentDidMount() {
    this.unsubscribe = effect(() => {
      consoleOpen.value;
      consoleState.value;
      this.setState({ revision: (this.state.revision ?? 0) + 1 });
    });
  }

  componentWillUnmount() {
    this.unsubscribe?.();
  }

  componentDidUpdate() {
    const chat = consoleState.peek().chat;
    const id = chat?.id ?? 0;
    const count = chat?.messages?.length ?? -1;
    if (id !== this.lastChat || count !== this.lastCount) {
      this.lastChat = id;
      this.lastCount = count;
      if (this.body && count > 0) this.body.scrollTop = this.body.scrollHeight;
    }
  }

  onDragEnter = (event) => {
    event.preventDefault();
    this.dragDepth += 1;
    if (!consoleState.peek().dropping) patch({ dropping: true });
  };

  onDragOver = (event) => {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  };

  onDragLeave = (event) => {
    event.preventDefault();
    this.dragDepth = Math.max(0, this.dragDepth - 1);
    if (this.dragDepth === 0 && consoleState.peek().dropping) patch({ dropping: false });
  };

  onDrop = (event) => {
    event.preventDefault();
    this.dragDepth = 0;
    patch({ dropping: false });
    attachFiles(event.dataTransfer?.files);
  };

  render() {
    if (!consoleOpen.peek()) return null;
    const state = consoleState.peek();
    const chat = state.chat;
    const key = String(state.openChat || 0);
    const unsentText = state.unsent[key] ?? "";
    const attached = state.attachments[key] ?? [];
    const sessions = chat?.sessions?.sessions ?? [];
    const draft = chat?.draft?.chat === state.openChat ? chat.draft : null;
    const asked = chat?.busy ? [...(chat.messages ?? [])].reverse().find((item) => item.sender === "you") : null;
    const workingText = asked?.ts
      ? `Working on it · sent ${clock(asked.ts)} · the answer shows here when it comes`
      : "Working on it · the answer shows here when it comes";

    return html`<aside class="console-dock frame" data-pane="console" data-drop=${state.dropping ? "true" : undefined}
      aria-label="Chat with Arete" tabindex="-1"
      onDragEnter=${this.onDragEnter} onDragOver=${this.onDragOver} onDragLeave=${this.onDragLeave} onDrop=${this.onDrop}>
      <header class="console-head">
        <h2 class="pane-label">Chat with Arete</h2>
        <button type="button" class="console-close" data-act="console-close" aria-label="Close chat" onClick=${() => closeConsole()}><kbd class="cap">Esc</kbd></button>
      </header>
      <div class="console-pick">
        <label class="console-label" for="ui-chat-pick">Chat</label>
        <select id="ui-chat-pick" class="console-select" value=${state.openChat ? String(state.openChat) : ""} disabled=${!state.chats.length}
          onChange=${(event) => switchChat(Number(event.currentTarget.value))}>
          ${state.chats.length
            ? state.chats.map((item) => html`<option key=${item.id} value=${String(item.id)} selected=${state.openChat === item.id}>${chatLabel(item)}</option>`)
            : html`<option value="" selected=${true}>No chat yet</option>`}
        </select>
        ${chat ? html`<p class="console-about">${chatAbout(chat)}</p>` : null}
        ${state.readError ? html`<p class="console-result fault" role="alert">${state.readError}</p>` : null}
      </div>
      <div class="console-body" ref=${(node) => { this.body = node; }}>
        ${chat ? html`<section class="console-sessions">
          ${sessions.length ? html`<h3 class="console-sub">Sessions of this chat</h3>
            <ul class="console-session-list">${sessions.map((session) => html`<li key=${session.id} class="console-session-row">
              <a class="console-link" href=${`#run/${encodeURIComponent(session.id)}`}>${session.id}</a>
              <span class="console-session-meta">${session.status} · ${session.prompt || "no first message"}${session.paid ? ` · ${money(session.paid)} paid` : ""}</span>
              ${session.status === "running" ? html`<button type="button" class="console-btn"
                disabled=${state.stopping.has(`${chat.id}:${session.id}`)}
                onClick=${() => stopChatSession(chat.id, session.id)}>Stop</button>` : null}
            </li>`)}</ul>` : null}
          <details class="console-session-start">
            <summary class="console-summary">Start a session</summary>
            <form class="console-session-form ops-form" noValidate onSubmit=${(event) => { event.preventDefault(); startChatSession(); }}>
              <input class="console-input" type="text" aria-label="Session prompt" placeholder="Ask a session to inspect or work"
                value=${state.sessionPrompt} onInput=${(event) => patch({ sessionPrompt: event.currentTarget.value })} />
              <button type="submit" class="console-btn" disabled=${state.startingSession}>Start session</button>
            </form>
          </details>
          <${ResultLine} result=${state.sessionResult} />
        </section>` : null}
        <div class="console-messages" aria-live="polite">
          ${!chat
            ? html`<p class="muted">${state.loaded ? "No chat yet. Pick a project, or none, under New chat on and press New chat." : "Reading chats…"}</p>`
            : chat.messages?.length
              ? chat.messages.map((message, index) => html`<div key=${message.id ?? index} class=${`console-msg from-${message.sender}`}>
                <div class="console-who">${[{ you: "You", tool: "Tool", session: "Session report" }[message.sender] || "Arete", clock(message.ts), messageCost(message)].filter(Boolean).join(" · ")}</div>
                <${MessageBody} message=${message} />
              </div>`)
              : html`<p class="muted">${chat.project ? `No messages yet. Ask about ${chat.project}'s code, its checks, or how arete works on it.` : "No messages yet. Ask what is running, why a run failed, or what to change."}</p>`}
        </div>
        ${chat?.busy ? html`<p class="console-working" role="status">${workingText}</p>` : null}
        ${draft ? html`<section class="console-draft">
          <h3 class="console-sub">Drafted issue</h3>
          <p class="muted">Nothing reaches GitHub until you file it.</p>
          <dl class="console-draft-fields">
            <dt>Title</dt><dd>${draft.title}</dd>
            <dt>Body</dt><dd><pre class="console-draft-body">${draft.body}</pre></dd>
          </dl>
          <div class="console-actions">
            <button type="button" class="console-btn" disabled=${state.draftBusy === draft.chat} onClick=${fileChatDraft}>File this issue</button>
            <button type="button" class="console-btn" disabled=${state.draftBusy === draft.chat} onClick=${dropChatDraft}>Drop</button>
          </div>
        </section>` : null}
        <${ResultLine} result=${state.draftResult} />
      </div>
      <div class="console-foot">
        <form class="console-send ops-form" noValidate onSubmit=${(event) => { event.preventDefault(); sendChatMessage(); }}>
          <label class="console-label" for="ui-chat-text">Message</label>
          <textarea id="ui-chat-text" class="console-textarea" rows="3" spellCheck=${true}
            placeholder="Ask Arete or drop a file here"
            value=${unsentText}
            onInput=${(event) => setUnsentText(event.currentTarget.value)}></textarea>
          ${attached.length ? html`<ul class="console-attachments" aria-label="Attached files">${attached.map((file, index) => html`<li key=${`${file.name}:${index}`} class="console-attachment">
            <span class="console-attachment-name">${file.name} · ${byteSize(file.size)}</span>
            <button type="button" class="console-btn" onClick=${() => removeAttachment(index)}>Remove</button>
          </li>`)}</ul>` : null}
          <div class="console-actions">
            <button type="submit" class="console-btn" disabled=${state.sending || !state.openChat || Boolean(chat?.busy)}>Send</button>
            <span class="muted console-hint">Cmd Enter sends · drop a file to attach</span>
          </div>
          <${ResultLine} result=${state.chatResult} />
        </form>
        <form class="console-new" noValidate onSubmit=${(event) => { event.preventDefault(); createChat(); }}>
          <label class="console-label" for="ui-chat-project">New chat on</label>
          <div class="console-new-row">
            <select id="ui-chat-project" class="console-select" value=${state.newProject}
              onChange=${(event) => patch({ newProject: event.currentTarget.value })}>
              <option value="" selected=${!state.newProject}>No project</option>
              ${state.projects.map((project) => html`<option key=${project} value=${project} selected=${state.newProject === project}>${project}</option>`)}
            </select>
            <button type="submit" class="console-btn" disabled=${state.creating}>New chat</button>
          </div>
          <${ResultLine} result=${state.newResult} />
        </form>
      </div>
    </aside>`;
  }
}
