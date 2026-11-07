"use strict";

const VIEWS = ["live", "health", "triage", "trends", "settings", "chat"];
const LIVE_MS = 5000;
const SLOW_MS = 30000;
const CHAT_BUSY_MS = 3000;
const SVG = "http://www.w3.org/2000/svg";
const URL_IN_TEXT = /(https:\/\/[^\s<>"')\]]*[^\s<>"')\].,;:!?])/g;

let view = "live";
let timer = 0;
let refreshCount = 0;
let openLog = "";
let trendsData = null;
let settingsData = null;
let chatSeen = "";
let lastGood = "";
const openQuestions = new Set();

function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else if (key === "on") for (const [event, fn] of Object.entries(value)) node.addEventListener(event, fn);
    else node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
  return node;
}

function svg(tag, attrs = {}, text) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

function fill(id, ...nodes) {
  const box = document.getElementById(id);
  const focused = document.activeElement && box.contains(document.activeElement) ? document.activeElement.dataset.focus : "";
  box.replaceChildren(...nodes.flat(Infinity).filter(Boolean));
  if (focused) box.querySelector(`[data-focus="${CSS.escape(focused)}"]`)?.focus();
}

function heading(n, title) {
  return el("h2", {}, el("span", { class: "n", text: String(n).padStart(2, "0") }), title);
}

function stateChip(state) {
  const word = { ok: "OK", caution: "Caution", fault: "Fault" }[state];
  return word ? el("span", { class: `state ${state}`, text: word }) : null;
}

function fault(sentence) {
  return el("p", {}, stateChip("fault"), " ", sentence);
}

function quiet(sentence) {
  return el("p", { class: "muted", text: sentence });
}

function link(href, text) {
  return /^https:\/\//.test(href || "") ? el("a", { href, rel: "noreferrer", target: "_blank", text }) : document.createTextNode(text);
}

function wholeWords(text) {
  return text.split(" ").flatMap((word, i) => [i ? " " : null, el("span", { class: "nb", text: word })]).filter(Boolean);
}

function pullLinks(urls) {
  return urls.split(/\s+/).filter(Boolean).flatMap((url, i) => [i ? el("br") : null, link(url, url.replace("https://github.com/", ""))]).filter(Boolean);
}

function table(caption, heads, rows) {
  const head = el("tr", {}, heads.map(([label, cls]) => el("th", { class: cls, text: label })));
  return el("div", { class: "wide" },
    el("table", {}, caption ? el("caption", { text: caption }) : null, el("thead", {}, head), el("tbody", {}, rows)));
}

function cell(content, cls) {
  const td = el("td", { class: cls });
  td.append(...[content].flat().filter((c) => c !== null && c !== undefined).map((c) => (c instanceof Node ? c : document.createTextNode(String(c)))));
  return td;
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function dateWords(d) {
  return `${d.getFullYear()} ${pad(d.getMonth() + 1)} ${pad(d.getDate())}`;
}

function clock(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return d.toDateString() === new Date().toDateString() ? time : `${pad(d.getMonth() + 1)} ${pad(d.getDate())} ${time}`;
}

function ago(ts) {
  if (!ts) return "never";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(ts)) / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} h ago`;
  return `${Math.round(minutes / 1440)} d ago`;
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${pad(minutes % 60)} min`;
}

function money(n) {
  return `$${Number(n || 0).toFixed(2)}`;
}

async function call(path, body) {
  const options = body === undefined ? { cache: "no-store" }
    : { method: "POST", headers: { "Content-Type": "application/json", "X-Arete": "1" }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({ error: `The server answered ${response.status} with no JSON.` }));
  if (!response.ok) throw new Error(data.error || `The server answered ${response.status}.`);
  return data;
}

function panelParts(panel, what, draw) {
  return panel && "rows" in panel ? draw(panel.rows) : fault(`${what} could not be read: ${panel?.error || "no answer"}.`);
}

function percent(rate) {
  return rate === null || rate === undefined ? "" : `${Math.round(rate * 100)}%`;
}

function logButton(id) {
  return el("button", { type: "button", "data-focus": `log:${id}`, text: "Log", on: { click: () => showLog(id) } });
}

function drawLive(snapshot) {
  fill("live-now", heading(1, "Running now"), panelParts(snapshot.live, "The live runs", (live) => [
    el("p", { class: "readout" }, String(live.slots), el("small", { text: `of ${live.max_parallel} slots busy` })),
    live.runs.length
      ? table("", [["Ticket"], ["Kind", "c-kind"], ["Started", "c-time"], ["Last step"], ["Time", "num c-time"], ["Cost", "num c-cost"], ["", "c-log"]],
        live.runs.map((r) => el("tr", {},
          cell(wholeWords(r.ticket)), cell(r.kind), cell(clock(r.started)), cell(r.step || "starting"),
          cell(duration(r.seconds), "num"), cell(money(r.cost), "num"), cell(logButton(r.id)))))
      : quiet("Nothing is running."),
  ]));

  fill("live-rest",
    heading(2, "Skipped"),
    panelParts(snapshot.skipped, "The skipped tickets", (skipped) => skipped.length
      ? table("Labeled tickets the pipeline will not run", [["Ticket"], ["Why"], ["Since", "c-time"]],
        skipped.map((s) => el("tr", {}, cell(link(s.url, s.ticket)), cell(s.why), cell(clock(s.since)))))
      : quiet("No labeled ticket is skipped.")),
    heading(3, "Review feedback"),
    panelParts(snapshot.waiting, "The review feedback", (waiting) => waiting.tickets.length
      ? [el("p", {}, `Review feedback runs ${waiting.quiet_minutes} min after the last comment, or at once when a review is submitted.`),
        table("Waiting", [["Ticket"], ["Comments", "num c-time"], ["When"]],
          waiting.tickets.map((w) => el("tr", {}, cell(w.ticket), cell(w.comments, "num"),
            cell(`${w.at_once ? "review submitted" : "last comment"} ${clock(w.last)}`))))]
      : quiet("No review feedback is waiting.")),
    heading(4, "Recent runs"),
    panelParts(snapshot.recent, "Recent runs", (recent) => recent.length
      ? table("Newest first", [["Ticket"], ["Kind", "c-kind"], ["Status", "c-status"], ["Started", "c-time"], ["Time", "num c-time"], ["Cost", "num c-cost"], ["Result", "c-result"], ["", "c-log"]],
        recent.map((r) => el("tr", {},
          cell(wholeWords(r.ticket)), cell(r.kind), cell([stateChip(r.state), r.state ? " " : "", r.status], "nb"), cell(clock(r.started)),
          cell(duration(r.seconds), "num"), cell(money(r.cost), "num"),
          cell(r.pr_url ? pullLinks(r.pr_url) : r.summary, "muted-cell"),
          cell(logButton(r.id)))))
      : quiet("No run has started yet.")));
}

async function showLog(id) {
  openLog = id;
  await refreshLog();
  document.getElementById("log").scrollIntoView({ block: "nearest" });
}

async function refreshLog() {
  if (!openLog) return;
  const box = document.getElementById("log");
  const lines = document.getElementById("log-lines");
  document.getElementById("log-title").textContent = `Log · ${openLog}`;
  box.hidden = false;
  try {
    const data = await call(`/api/log/${encodeURIComponent(openLog)}`);
    const stick = lines.scrollTop + lines.clientHeight >= lines.scrollHeight - 4;
    lines.textContent = data.lines.length ? data.lines.join("\n") : "This run has no log yet.";
    if (stick) lines.scrollTop = lines.scrollHeight;
  } catch (e) {
    lines.textContent = `The log could not be read: ${e.message}`;
  }
}

function closeLog() {
  openLog = "";
  document.getElementById("log").hidden = true;
}

function checksSummary(h) {
  if (!h.heartbeat) return el("p", {}, stateChip("caution"), " The heartbeat has not run yet, so no check has a result.");
  const failing = h.checks.filter((c) => !c.ok).length;
  const state = !failing ? "ok" : h.checks.some((c) => c.state === "fault") ? "fault" : "caution";
  return el("p", {}, stateChip(state), " ",
    `${h.checks.length} checks, ${failing} failing. The last heartbeat came ${ago(h.heartbeat)} (${clock(h.heartbeat)}).`);
}

function drawHealth(snapshot) {
  fill("health",
    heading(1, "Checks"),
    panelParts(snapshot.checks, "The heartbeat", (h) => [checksSummary(h),
      h.checks.length ? table("", [["Check"], ["State", "c-status"], ["Detail"]],
        h.checks.map((c) => el("tr", {}, cell(c.key), cell(stateChip(c.state)), cell(c.detail || "")))) : ""]),
    heading(2, "Loops"),
    panelParts(snapshot.checks, "The loops", (h) => h.loops.length
      ? table("", [["Loop"], ["State", "c-status"], ["Last run", "c-time"], ["Detail"]],
        h.loops.map((l) => el("tr", {}, cell(l.name), cell(stateChip(l.ok ? "ok" : "fault")), cell(ago(l.ts)), cell(l.detail || ""))))
      : quiet("No loop has reported yet.")),
    heading(3, "Probes"),
    panelParts(snapshot.alerts, "The probes", (a) => a.probes.length
      ? table("", [["Probe"], ["State", "c-status"], ["Last run", "c-time"], ["Last good", "c-time"], ["Detail"]],
        a.probes.map((p) => el("tr", {}, cell(p.name), cell(stateChip(p.ok ? "ok" : "fault")), cell(ago(p.last_run)), cell(ago(p.last_ok)), cell(p.detail || ""))))
      : quiet("No probe has run yet.")),
    heading(4, "Alerts"),
    panelParts(snapshot.alerts, "The alerts", (a) => a.alerts.length
      ? table("Open alerts", [["Alert"], ["Opened", "c-time"], ["Last seen", "c-time"], ["Issue"]],
        a.alerts.map((x) => el("tr", {}, cell(x.key), cell(clock(x.opened)), cell(ago(x.last_seen)), cell(x.url ? link(x.url, `#${x.issue}`) : ""))))
      : el("p", {}, stateChip("ok"), " No alert is open.")));
}

function questionFold(ref, questions) {
  return el("details", {
    open: openQuestions.has(ref),
    on: { toggle: (e) => (e.target.open ? openQuestions.add(ref) : openQuestions.delete(ref)) },
  }, el("summary", { text: "Show the questions" }), el("ol", { class: "questions" }, questions.map((q) => el("li", { text: q }))));
}

function drawTriage(snapshot) {
  fill("triage", panelParts(snapshot.triage, "Triage", triageParts));
}

function triageParts(t) {
  const parts = [quiet(`${t.refreshed ? `Triage last ran ${ago(t.refreshed)} (${clock(t.refreshed)}).` : "Triage has not run yet."} It reads every open ticket and says what each one needs.`)];
  t.groups.forEach((g, i) => {
    parts.push(heading(i + 1, g.standing));
    parts.push(g.rows.length
      ? [el("p", { text: `${g.rows.length} ${g.rows.length === 1 ? "ticket" : "tickets"}.` }),
        table("", [["Ticket", "c-ref"], ["Title"], ["Verdict", "c-verdict"]],
          g.rows.map((r) => el("tr", {},
            cell(link(r.url, r.ref), "nb"),
            cell([r.title, r.questions.length ? questionFold(r.ref, r.questions) : null]),
            cell(wholeWords(r.verdict)))))]
      : quiet("None."));
  });
  return parts;
}

function niceMax(value, whole) {
  if (value <= 0) return 1;
  const step = 10 ** Math.floor(Math.log10(value));
  const nice = [1, 2, 2.5, 5, 10].map((m) => m * step).find((m) => m >= value && (!whole || Number.isInteger(m)));
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

function costChart(days, width) {
  const frame = dayChart(days, width, 230, 30, `Cost per day for the last ${days.length} days, with today's bar filled`, 0);
  const { chart, left, right, top, bottom, pitch, X } = frame;
  const max = niceMax(Math.max(...days.map((d) => d.cost)), false);
  const Y = (v) => bottom - (v / max) * (bottom - top);
  valueTicks(frame, [0, max / 2, max], Y, (v) => money(v).replace(".00", ""));
  days.forEach((d, i) => d.cost > 0 && bar(frame, i, i === days.length - 1 ? "frame this" : "frame", Y(d.cost), bottom - Y(d.cost)));
  const today = days[days.length - 1];
  const label = `Today ${money(today.cost)}`;
  const under = days.slice(-Math.ceil((label.length * 7.4) / pitch) - 1);
  const clear = Math.min(...under.map((d) => Y(d.cost)));
  chart.append(svg("text", { class: "fig", x: X(days.length - 1) + pitch * 0.82, y: Math.max(top - 14, clear - 8), "text-anchor": "end" }, label));
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

function drawTrends() {
  if (!trendsData) return;
  if (trendsData.error) {
    fill("trends", heading(1, "Trends"), fault(`Trends could not be read: ${trendsData.error}.`));
    return;
  }
  const t = trendsData.rows;
  const box = document.getElementById("trends");
  const width = Math.max(300, Math.floor(box.clientWidth || 300));
  const shipped = t.days.reduce((n, d) => n + d.shipped, 0);
  const failed = t.days.reduce((n, d) => n + d.failed, 0);
  const other = t.days.reduce((n, d) => n + d.other, 0);
  fill("trends",
    heading(1, "Cost per day"),
    el("p", { class: "readout" }, money(t.total_cost), el("small", { text: `in ${t.days.length} days` })),
    el("p", {}, `Every run counts, pipeline and upkeep alike, on the day it started. Today so far: ${money(t.days[t.days.length - 1].cost)}.`),
    costChart(t.days, width),
    heading(2, "Outcomes per day"),
    el("p", {}, `${shipped} pipeline runs shipped a pull request and ${failed} failed in ${t.days.length} days.` +
      (other ? ` ${other} more ended another way, such as needing information.` : "")),
    outcomeChart(t.days, width),
    heading(3, "By repo"),
    t.repos.length ? table("",
      [["Repo"], ["Runs", "num c-n"], ["Shipped", "num c-n"], ["Failed", "num c-n"], ["Pass rate", "num c-rate"], ["Median time", "num c-time"], ["Cost", "num c-cost"]],
      t.repos.map((r) => el("tr", {}, cell(r.repo), cell(r.runs, "num"), cell(r.shipped, "num"), cell(r.failed, "num"),
        cell(percent(r.pass_rate), "num"), cell(duration(r.median_seconds), "num"), cell(money(r.cost), "num"))))
      : quiet("No pipeline run finished in this window."),
    heading(4, "Evals"),
    t.evals.length ? table("Mined eval tasks", [["Repo"], ["Tasks", "num c-n"], ["Passed", "num c-n"], ["Pass rate", "num c-rate"], ["Last", "c-time"]],
      t.evals.map((e) => el("tr", {}, cell(e.repo), cell(e.tasks, "num"), cell(e.passed, "num"), cell(percent(e.pass_rate), "num"), cell(clock(e.last)))))
      : quiet("No eval ran in this window."));
}

function defaultModel(data, role) {
  return `default: ${data.defaults[role] || "claude default"}`;
}

function drawSettings(data) {
  settingsData = data;
  const parallel = document.getElementById("max-parallel");
  if (parallel.value !== String(data.max_parallel)) parallel.value = data.max_parallel;
  document.getElementById("model-hint").textContent =
    `Each role takes ${data.aliases.join(", ")}, or a full claude model id. An empty role uses its default.`;
  fill("model-names", data.aliases.map((alias) => el("option", { value: alias })));
  fill("model-fields", Object.entries(data.models).map(([role, model]) => el("div", { class: "field" },
    el("label", { for: `model-${role}`, text: role.replace("_", " ") }),
    el("input", { id: `model-${role}`, "data-role": role, "data-focus": `model-${role}`, type: "text", value: model,
      placeholder: defaultModel(data, role), list: "model-names", autocomplete: "off", spellcheck: "false" }))));
}

function settingsChanges() {
  const changes = {};
  const parallel = document.getElementById("max-parallel").value.trim();
  if (parallel !== String(settingsData.max_parallel)) changes.max_parallel = parallel;
  const models = {};
  for (const input of document.querySelectorAll("#model-fields input")) {
    if (input.value.trim() !== settingsData.models[input.dataset.role]) models[input.dataset.role] = input.value.trim();
  }
  if (Object.keys(models).length) changes.models = models;
  return changes;
}

function say(id, state, sentence) {
  const line = document.getElementById(id);
  line.replaceChildren(...[stateChip(state), " ", sentence].filter(Boolean));
  line.hidden = false;
}

async function saveSettings(event) {
  event.preventDefault();
  const changes = settingsChanges();
  if (!Object.keys(changes).length) return say("settings-result", "ok", "Nothing changed, so nothing was saved.");
  const button = document.getElementById("settings-save");
  button.disabled = true;
  try {
    const saved = await call("/api/settings", changes);
    drawSettings(saved);
    const words = [changes.max_parallel ? `runs at once ${saved.max_parallel}` : "",
      ...Object.keys(changes.models || {}).map((role) => saved.models[role] ? `${role} ${saved.models[role]}`
        : `${role} back to its default (${saved.defaults[role] || "claude default"})`)].filter(Boolean);
    say("settings-result", "ok", `Saved ${words.join(", ")}. The pipeline takes it at its next poll.`);
  } catch (e) {
    say("settings-result", "fault", e.message);
  } finally {
    button.disabled = false;
  }
}

function withLinks(text) {
  return text.split(URL_IN_TEXT).map((part, i) => (i % 2 ? link(part, part) : document.createTextNode(part)));
}

function drawChat(data) {
  const seen = `${data.messages.length}:${data.busy}`;
  const send = document.getElementById("chat-send");
  send.disabled = data.busy;
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
  chatSeen = seen;
  fill("messages", data.messages.length ? data.messages.map((m) => el("div", { class: `msg from-${m.from}` },
    el("div", { class: "who", text: [m.from === "you" ? "You" : "Arete", clock(m.ts), m.cost ? money(m.cost) : ""].filter(Boolean).join(" · ") }),
    el("p", {}, withLinks(m.text))))
    : quiet("No messages yet. Ask what is running, why a run failed, or what to change."));
  document.querySelector("#messages .msg:last-child")?.scrollIntoView({ block: "nearest" });
}

async function sendChat(event) {
  event.preventDefault();
  const box = document.getElementById("chat-text");
  const text = box.value.trim();
  if (!text) return say("chat-result", "caution", "Write a message first.");
  document.getElementById("chat-result").hidden = true;
  try {
    drawChat(await call("/api/chat", { text }));
    box.value = "";
    schedule(CHAT_BUSY_MS);
  } catch (e) {
    say("chat-result", "fault", e.message);
  }
}

async function chatAction(path, button, resultId) {
  document.getElementById(resultId).hidden = true;
  button.disabled = true;
  const answered = await call(path, {}).catch((e) => say(resultId, "fault", e.message));
  button.disabled = false;
  if (answered) drawChat(answered);
}

const LOADERS = {
  live: async () => {
    drawLive(await call("/api/state"));
    await refreshLog();
  },
  health: async () => drawHealth(await call("/api/state")),
  triage: async () => drawTriage(await call("/api/state")),
  trends: async () => {
    trendsData = await call("/api/trends");
    drawTrends();
  },
  settings: async () => {
    const fresh = await call("/api/settings");
    if (!settingsData || !Object.keys(settingsChanges()).length) drawSettings(fresh);
  },
  chat: async () => drawChat(await call("/api/chat")),
};

function nextDelay() {
  if (view === "live") return LIVE_MS;
  if (view === "chat") return chatSeen.endsWith(":true") ? CHAT_BUSY_MS : SLOW_MS;
  return SLOW_MS;
}

function schedule(ms) {
  clearTimeout(timer);
  if (ms) timer = setTimeout(refresh, ms);
}

function status(state, sentence) {
  const line = document.getElementById("status");
  line.replaceChildren(`${dateWords(new Date())} · `, ...[stateChip(state), state ? " " : "", sentence].filter(Boolean));
}

async function refresh() {
  clearTimeout(timer);
  const mine = ++refreshCount;
  if (document.hidden) return;
  try {
    await LOADERS[view]();
    if (mine !== refreshCount) return;
    lastGood = clock(new Date().toISOString());
    const label = document.querySelector(`.views [data-view="${view}"]`).textContent;
    status("", `${label} · updated ${new Date().toLocaleTimeString([], { hour12: false })}${nextDelay() ? ` · refreshes every ${nextDelay() / 1000} s` : ""}`);
    document.querySelectorAll(".readout").forEach((r) => r.classList.remove("stale"));
  } catch (e) {
    if (mine !== refreshCount) return;
    status("fault", `Arete did not answer: ${e.message}${lastGood ? ` What shows came at ${lastGood}.` : ""}`);
    document.querySelectorAll(".readout").forEach((r) => r.classList.add("stale"));
  }
  if (mine === refreshCount) schedule(nextDelay());
}

function show(name) {
  view = VIEWS.includes(name) ? name : "live";
  for (const button of document.querySelectorAll(".views [data-view]")) button.setAttribute("aria-pressed", String(button.dataset.view === view));
  for (const section of document.querySelectorAll("main > section")) section.hidden = section.dataset.view !== view;
  refresh();
}

let resizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => view === "trends" && drawTrends(), 150);
});
window.addEventListener("hashchange", () => show(location.hash.slice(1)));
document.addEventListener("visibilitychange", () => !document.hidden && refresh());

document.addEventListener("DOMContentLoaded", () => {
  for (const button of document.querySelectorAll(".views [data-view]")) {
    button.addEventListener("click", () => {
      if (location.hash.slice(1) === button.dataset.view) show(button.dataset.view);
      else location.hash = button.dataset.view;
    });
  }
  document.getElementById("log-close").addEventListener("click", closeLog);
  document.getElementById("settings").addEventListener("submit", saveSettings);
  document.getElementById("settings-undo").addEventListener("click", () => {
    if (settingsData) drawSettings(settingsData);
    document.getElementById("settings-result").hidden = true;
    refresh();
  });
  document.getElementById("chat").addEventListener("submit", sendChat);
  for (const [id, path, resultId] of [["chat-new", "/api/chat/new", "chat-result"],
    ["draft-file", "/api/chat/file", "draft-result"], ["draft-drop", "/api/chat/drop", "draft-result"]]) {
    document.getElementById(id).addEventListener("click", (event) => chatAction(path, event.currentTarget, resultId));
  }
  document.getElementById("chat-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) document.getElementById("chat").requestSubmit();
  });
  show(location.hash.slice(1));
});
