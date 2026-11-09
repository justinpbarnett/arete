"use strict";

const VIEWS = ["live", "loops", "launch", "fleet", "health", "triage", "trends", "settings", "chat"];
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
let costData = null;
let settingsData = null;
const RUN_LIMITS = [["max-parallel", "max_parallel"], ["max-parallel-per-repo", "max_parallel_per_repo"]];
let chatSeen = "";
let lastGood = "";
const openQuestions = new Set();
const drafts = new Map();
const sendSaid = new Map();
const sending = new Set();
const HELD = Symbol("held");
let redrawTriage = false;
const lastSaid = new Map();
const going = { health: false, triage: false };
let loopNewKey = "";

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

function scrollersByPlace(box) {
  const found = new Map();
  let under = "";
  for (const node of box.querySelectorAll("h2, h3, summary, .wide, pre")) {
    if (!node.matches(".wide, pre")) {
      under = node.textContent;
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
  const focused = document.activeElement && box.contains(document.activeElement) ? document.activeElement.dataset.focus : "";
  const scrolled = [...scrollersByPlace(box)].filter(([, node]) => node.scrollLeft || node.scrollTop)
    .map(([place, node]) => [place, node.scrollLeft, node.scrollTop]);
  const old = [...box.childNodes];
  box.append(...nodes.flat(Infinity).filter(Boolean));
  // Focus moves to the copy while the old element is still there, so a keyboard focus ring carries over.
  if (focused) [...box.querySelectorAll(`[data-focus="${CSS.escape(focused)}"]`)].at(-1)?.focus({ preventScroll: true });
  for (const node of old) node.remove();
  const fresh = scrolled.length ? scrollersByPlace(box) : new Map();
  for (const [place, left, top] of scrolled) fresh.get(place)?.scrollTo(left, top);
}

function heading(n, title) {
  return el("h2", {}, el("span", { class: "n", text: String(n).padStart(2, "0") }), title);
}

function stateChip(state) {
  const word = { ok: "OK", caution: "Caution", fault: "Fault" }[state];
  return word ? el("span", { class: `state ${state}`, text: word }) : null;
}

function sayParts(state, sentence) {
  return [stateChip(state), stateChip(state) ? " " : "", sentence].filter(Boolean);
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

function sayAt(place, state, sentence) {
  lastSaid.set(place, [state, sentence]);
  const line = document.querySelector("main > section:not([hidden])")?.querySelector(`[data-said="${CSS.escape(place)}"]`);
  if (!line) return;
  sayIn(line, state, sentence);
  line.focus();
}

function fault(sentence) {
  return el("p", {}, sayParts("fault", sentence));
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
  return d.toDateString() === new Date().toDateString() ? time : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${time}`;
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
  const response = await fetch(path, options).catch(() => { throw new Error("The connection to arete failed."); });
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
    el("p", { class: "readout" }, String(live.slots), el("small", { text: `of ${live.max_parallel} slots busy${live.waiting ? `, ${live.waiting} waiting for one` : ""}` })),
    live.runs.length
      ? table("", [["Work"], ["Kind", "c-kind"], ["Started", "c-time"], ["Last step"], ["Time", "num c-time"], ["Cost", "num c-cost"], ["", "c-run-buttons"]],
        live.runs.map((r) => el("tr", {},
          cell(wholeWords(r.ticket)), cell(r.kind), cell(clock(r.started)), cell(r.slot || r.step || "starting"),
          cell(duration(r.seconds), "num"), cell(money(r.cost), "num"),
          cell([logButton(r.id), " ", button(r.stopping ? "Stopping" : "Stop", `stop:${r.id}`, (b) => stopLiveRun(r, b), r.stopping)], "nb"))))
      : quiet("Nothing is running."),
    saidLine("running"),
  ]));

  fill("live-rest",
    heading(2, "Skipped"),
    panelParts(snapshot.skipped, "The skipped tickets", (skipped) => [skipped.length
      ? table("Labeled tickets the pipeline will not run on its own", [["Ticket"], ["Why"], ["Since", "c-time"], ["", "c-run-anyway"]],
        skipped.map((s) => el("tr", {}, cell(link(s.url, s.ticket), "nb"), cell(s.why), cell(clock(s.since)), cell(skippedAction(s), "nb"))))
      : quiet("No labeled ticket is skipped."), saidLine("skipped")]),
    heading(3, "By hand"),
    panelParts(snapshot.proposals, "The fixes to make by hand", (p) => [handParts(p), saidLine("hand")]),
    heading(4, "Review feedback"),
    panelParts(snapshot.waiting, "The review feedback", (waiting) => waiting.tickets.length
      ? [el("p", {}, `Review feedback runs ${waiting.quiet_minutes} min after the last comment, or at once when a review is submitted.`),
        table("Waiting", [["Ticket"], ["Comments", "num c-time"], ["When"]],
          waiting.tickets.map((w) => el("tr", {}, cell(w.ticket), cell(w.comments, "num"),
            cell(`${w.at_once ? "review submitted" : "last comment"} ${clock(w.last)}`))))]
      : quiet("No review feedback is waiting.")),
    heading(5, "Recent runs"),
    panelParts(snapshot.recent, "Recent runs", (recent) => recent.length
      ? table("Newest first", [["Work"], ["Kind", "c-kind"], ["Status", "c-status"], ["Started", "c-time"], ["Time", "num c-time"], ["Cost", "num c-cost"], ["Result", "c-result"], ["", "c-log"]],
        recent.map((r) => el("tr", {},
          cell(wholeWords(r.ticket)), cell(r.kind), cell(runStatus(r.status), "nb"), cell(clock(r.started)),
          cell(duration(r.seconds), "num"), cell(money(r.cost), "num"),
          cell(r.pr_url ? pullLinks(r.pr_url) : r.summary, "muted-cell"),
          cell(logButton(r.id)))))
      : quiet("No run has started yet.")));
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

function stopLiveRun(run, btn) {
  return act(btn, "running", `/api/runs/${encodeURIComponent(run.id)}/stop`, {},
    (r) => stopWords(run.ticket, r, "It moves to Recent runs once it has."));
}

function skippedAction(s) {
  return s.can_run ? button("Run anyway", `run-anyway:${s.ticket}`, (b) => runSkipped(s.ticket, b))
    : el("span", { class: "muted", text: "no repo to run in yet" });
}

function runSkipped(ticket, btn) {
  return act(btn, "skipped", `/api/skipped/${encodeURIComponent(ticket)}/run`, {},
    () => `Started ${ticket} past its blocker at ${clock(new Date())}. It shows under Running now while it runs.`);
}

function handParts(p) {
  if (!p.hand.length && !p.memory.length) return quiet("Nothing waits for you to do by hand.");
  const done = (x, what) => cell(button("Mark done", `done:${x.id}`, (b) => markDone(x.id, what, b)));
  return [
    p.hand.length ? [el("p", { text: "Claude Code will not let an agent write under .claude/, so the miner leaves these harness fixes to you. Make each one, then mark it done." }),
      table("Harness fixes", [["File", "c-file"], ["Change"], ["", "c-done"]],
        p.hand.map((x) => el("tr", {}, cell(`${x.repo}/${x.file}`, "wrap-cell"), cell(x.change), done(x, `The fix to ${x.repo}/${x.file}`))))] : null,
    p.memory.length ? [el("p", { text: "The miner noticed these facts for an agent's memory. File each one by hand, then mark it done." }),
      table("Memory facts", [["Repo", "c-kind"], ["Fact"], ["", "c-done"]],
        p.memory.map((x) => el("tr", {}, cell(x.repo), cell(x.change), done(x, `The ${x.repo} fact`))))] : null,
  ];
}

function markDone(id, what, btn) {
  return act(btn, "hand", `/api/proposals/${id}/done`, {}, () => `${what} is marked done, so it leaves this list.`);
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
  going.health = Boolean(snapshot.checks?.rows?.running);
  fill("health",
    heading(1, "Checks"),
    panelParts(snapshot.checks, "The heartbeat", (h) => [checksSummary(h),
      el("div", { class: "actions" }, button(h.running ? "Checks running" : "Run the checks now", "checks-run", runChecks, h.running)),
      saidLine("checks"),
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

function runChecks(btn) {
  return act(btn, "checks", "/api/checks/run", {},
    () => `Started the checks at ${clock(new Date())}. Their results show here when they end.`);
}

function drawTriage(snapshot) {
  fill("triage", panelParts(snapshot.triage, "Triage", triageParts));
}

function refreshTriage(btn) {
  return act(btn, "triage-refresh", "/api/triage/refresh", {},
    () => `Started triage at ${clock(new Date())}. It reads every open ticket, so it can take a few minutes; the verdicts here change when it ends.`);
}

function researchTicket(r, btn) {
  return act(btn, `research:${r.ref}`, `/api/triage/${encodeURIComponent(r.ref)}/research`, { force: r.research.force },
    () => `Asked the research loop at ${clock(new Date())} to research ${r.ref}. Its verdict follows the research.`);
}

function triageParts(t) {
  going.triage = t.refreshing || t.researching;
  const parts = [quiet(`${t.refreshed ? `Triage last ran ${ago(t.refreshed)} (${clock(t.refreshed)}).` : "Triage has not run yet."} It reads every open ticket, says what kind it is and what it needs, sends ready code tickets to the pipeline and research tickets to the research agent.`),
    el("div", { class: "actions" }, button(t.refreshing ? "Triage running" : "Refresh triage now", "triage-refresh", refreshTriage, t.refreshing)),
    saidLine("triage-refresh")];
  if (t.researching) parts.push(quiet("The research loop is running, so the research buttons wait until it ends."));
  if (t.approved && t.approved.length) parts.push(quiet(`Sent to the pipeline on the last run: ${t.approved.join(", ")}.`));
  Object.entries(t.held || {}).forEach(([ref, why]) => parts.push(quiet(`Not sent this time: ${ref}: ${why}`)));
  t.groups.forEach((g, i) => {
    parts.push(heading(i + 1, g.standing));
    parts.push(g.rows.length
      ? [el("p", { text: `${g.rows.length} ${g.rows.length === 1 ? "ticket" : "tickets"}.` }),
        table("", [["Ticket", "c-ref"], ["Title"], ["Verdict", "c-verdict"]],
          g.rows.map((r) => el("tr", {},
            cell(link(r.url, r.ref.replace(/^[^/#\s]+\//, "")), "nb"),
            cell([r.title, ...triageFolds(r, t.researching)]),
            cell(r.prs ? [...wholeWords(r.verdict), el("br"), ...pullLinks(r.prs)] : wholeWords(r.verdict)))))]
      : quiet("None."));
  });
  return parts;
}

function researchOffer(r, researching) {
  return [
    r.research ? el("div", { class: "actions offer" },
      button(r.research.label, `research:${r.ref}`, (b) => researchTicket(r, b), researching),
      r.research.force ? el("span", { class: "muted", text: "This skips JEV's screen for instructions aimed at an agent, so read the ticket first." }) : null) : null,
    r.research || lastSaid.has(`research:${r.ref}`) ? saidLine(`research:${r.ref}`) : null,
  ];
}

function triageFolds(r, researching) {
  const list = (items) => el("ol", { class: "questions" }, items.map((q) => el("li", { text: q })));
  const answerable = r.send && r.questions.length;
  return [
    r.summary ? el("p", { class: "muted", text: r.summary }) : null,
    r.questions.length && !answerable ? fold(`triage:q:${r.ref}`, "Show the questions", list(r.questions)) : null,
    r.assumptions.length ? fold(`triage:a:${r.ref}`, "Show what the build assumes", list(r.assumptions)) : null,
    r.plan.length ? fold(`triage:p:${r.ref}`, "Show the plan", list(r.plan)) : null,
    r.report ? fold(`triage:r:${r.ref}`, "Show the report", el("pre", { class: "fold-text", text: r.report })) : null,
    answerable ? fold(`triage:q:${r.ref}`, "Answer the questions", sendForm(r)) : r.send ? sendForm(r) : null,
    ...researchOffer(r, researching),
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
  const picker = el("select", { id: id("repo"), "data-focus": id("repo"), on: { change: (e) => {
    drafts.set(draftKey(r.ref, "repo"), e.target.value);
    e.target.form.querySelector(".send-about").textContent = sendWords(r);
  } } }, el("option", { value: "", text: "Pick a repo" }), r.send.choices.map((c) => el("option", { value: c.repo, text: c.repo })));
  picker.value = pickedRepo(r);
  const box = (q, i) => el("textarea", { id: id(i), "data-focus": id(i), text: drafts.get(draftKey(r.ref, q)) || "", on: {
    input: (e) => drafts.set(draftKey(r.ref, q), e.target.value),
    keydown: (e) => e.key === "Enter" && (e.metaKey || e.ctrlKey) && e.target.form.requestSubmit(),
  } });
  const fields = r.questions.map((q, i) => field(isRepoQuestion(r, q) ? picker : box(q, i), q));
  if (r.send.pick && !r.send.repo_question) fields.push(field(picker, "The repo to build it in"));
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
  const result = document.getElementById("triage-result");
  result.hidden = true;
  sending.add(r.ref);
  sendSay(form, r, null, `Sending ${r.ref}. It screens the ticket and writes to it, so it can take a few seconds.`);
  form.querySelector("button[type=submit]").disabled = true;
  let failed = "";
  try {
    const answers = r.questions.map((q) => (isRepoQuestion(r, q) ? "" : drafts.get(draftKey(r.ref, q)) || ""));
    const done = await call(`/api/triage/${encodeURIComponent(r.ref)}/send`, { questions: r.questions, answers, repo });
    for (const key of drafts.keys()) if (key.startsWith(draftKey(r.ref, ""))) drafts.delete(key);
    sendSaid.delete(r.ref);
    openQuestions.delete(`triage:q:${r.ref}`);
    say("triage-result", "ok", done.said);
    result.focus();
  } catch (e) {
    failed = e.message;
    sendSay(form, r, "fault", failed);
  } finally {
    sending.delete(r.ref);
    form.querySelector("button[type=submit]").disabled = false;
  }
  redrawTriage = true;
  await refresh();
  if (failed && view === "triage" && !document.querySelector(`[data-focus="${CSS.escape(`send:${r.ref}:said`)}"]`)) {
    sendSaid.delete(r.ref);
    say("triage-result", "fault", failed);
    result.focus();
  }
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
      : quiet("No eval ran in this window."),
    costParts(costData));
}

function defaultModel(data, role) {
  return data.defaults[role] || "claude CLI";
}

function drawSettings(data) {
  settingsData = data;
  for (const [id, key] of RUN_LIMITS) {
    const input = document.getElementById(id);
    if (input.value !== String(data[key])) input.value = data[key];
  }
  document.getElementById("model-hint").textContent =
    `Each role takes ${data.aliases.join(", ")}, or a full claude model id. An empty role uses its default.`;
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

function say(id, state, sentence) {
  sayIn(document.getElementById(id), state, sentence);
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
      changes.max_parallel_per_repo ? `runs per repo ${saved.max_parallel_per_repo}` : "",
      ...Object.keys(changes.models || {}).map((role) => saved.models[role] ? `${role} ${saved.models[role]}`
        : `${role} back to its default (${defaultModel(saved, role)})`)].filter(Boolean);
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

const RUN_STATE = { done: "ok", pr: "ok", merged: "ok", noop: "ok", skipped: "ok", "dry-run": "ok", failed: "fault", interrupted: "fault", "needs-info": "caution", stopped: "caution" };
const feeds = {
  loop: { box: "loop-feed", run: "", after: 0, status: "" },
  draft: { box: "draft-feed", run: "", after: 0, status: "" },
  session: { box: "session-feed", run: "", after: 0, status: "" },
};
let openLoop = "";
let launchKey = "";
let remoteDevices = [];

function button(text, focus, onClick, disabled = false) {
  return el("button", { type: "button", "data-focus": focus, disabled, text, on: { click: (e) => onClick(e.currentTarget) } });
}

function runStatus(status) {
  return sayParts(RUN_STATE[status], status);
}

function secondsBetween(start, end) {
  return start ? Math.max(0, Math.round(((end ? new Date(end) : new Date()) - new Date(start)) / 1000)) : null;
}

function fact(name, value) {
  return value === "" || value === null || value === undefined ? null : [el("dt", { text: name }), el("dd", {}, value)];
}

function fold(key, summary, ...content) {
  return el("details", {
    open: openQuestions.has(key),
    on: { toggle: (e) => (e.target.open ? openQuestions.add(key) : openQuestions.delete(key)) },
  }, el("summary", { text: summary }), ...content);
}

function feedLine(line) {
  return `${clock(line.ts).padStart(5)}  ${line.kind.padEnd(7)}  ${line.text}`;
}

function runAbout(r) {
  const parts = [r.model && `${r.runner ? `${r.runner} ` : ""}${r.model}`, r.device && `on ${r.device}`,
    `started ${clock(r.started)}`, r.finished && `ended ${clock(r.finished)}`, money(r.cost_usd)].filter(Boolean);
  return [...runStatus(r.status), ` · ${parts.join(" · ")}`,
    r.status !== "running" && r.summary ? ` · ${r.summary}` : ""];
}

async function openFeed(which, runId) {
  const feed = feeds[which];
  Object.assign(feed, { run: runId, after: 0, status: "" });
  document.querySelector(`#${feed.box} .feed-lines`).textContent = "";
  for (const said of document.querySelectorAll(`#${feed.box} .feed-said`)) said.hidden = true;
  await refreshFeed(which);
  document.getElementById(feed.box).scrollIntoView({ block: "nearest" });
}

async function refreshFeed(which) {
  const feed = feeds[which];
  if (!feed.run) return;
  const box = document.getElementById(feed.box);
  const lines = box.querySelector(".feed-lines");
  box.hidden = false;
  try {
    const data = await call(`/api/feed/${encodeURIComponent(feed.run)}?after=${feed.after}`);
    if (feeds[which].run !== data.run.id) return;
    const stick = lines.scrollTop + lines.clientHeight >= lines.scrollHeight - 4;
    if (data.lines.length) {
      const text = data.lines.map(feedLine).join("\n");
      lines.textContent = feed.after ? `${lines.textContent}\n${text}` : text;
      feed.after = data.lines[data.lines.length - 1].id;
    } else if (!feed.after) {
      lines.textContent = "Nothing in the feed yet.";
    }
    feed.status = data.run.status;
    const title = which === "session" ? "Session" : data.run.kind === "draft" ? "Draft" : data.run.loop || "Feed";
    box.querySelector(".feed-title").textContent = `${title} · ${clock(data.run.started)}`;
    const about = box.querySelector(".feed-about");
    if (about) about.replaceChildren(...runAbout(data.run).filter(Boolean));
    else box.querySelector(".feed-title").after(el("p", { class: "muted feed-about" }, runAbout(data.run).filter(Boolean)));
    const stop = box.querySelector(".feed-stop");
    const ended = data.run.status !== "running";
    if (ended && document.activeElement === stop) box.querySelector(".feed-close").focus();
    stop.disabled = ended;
    if (which === "session") document.getElementById("session-go").disabled = data.run.status !== "running";
    if (stick) lines.scrollTop = lines.scrollHeight;
  } catch (e) {
    lines.textContent = `The feed could not be read: ${e.message}`;
  }
}

function closeFeed(which) {
  feeds[which].run = "";
  document.getElementById(feeds[which].box).hidden = true;
}

async function stopFeedRun(which, btn, resultId) {
  btn.disabled = true;
  try {
    const r = await call(`/api/runs/${encodeURIComponent(feeds[which].run)}/stop`, {});
    say(resultId, "ok", stopWords(feeds[which].run, r, "The feed says when it has."));
  } catch (e) {
    say(resultId, "fault", e.message);
  }
  document.getElementById(resultId).focus();
  await refreshFeed(which);
}

function serveLine(serve) {
  return [serveState(serve), !serve.at || !serve.alive
    ? el("div", { class: "actions" }, button(serve.at ? "Restart serve" : "Install serve", "serve-install", installServe)) : null,
  saidLine("serve")];
}

function serveState(serve) {
  if (!serve.at) return el("p", {}, stateChip("fault"), " Serve has never run on this machine, so no loop fires on its own. Installing it starts it and keeps it running.");
  if (!serve.alive) return el("p", {}, stateChip("fault"), ` Serve last beat ${ago(serve.at)} (${clock(serve.at)}), so no loop fires on its own until it runs again.`);
  const broken = Object.keys(serve.broken || {}).length;
  const errors = Object.keys(serve.poll_errors || {}).length;
  return el("p", {}, stateChip(broken || errors ? "caution" : "ok"),
    ` Serve is up since ${clock(serve.started)}: ${serve.loops} loops on, ${serve.off} off, ${serve.running} running, ${serve.pending} waiting.` +
    (broken ? ` ${broken} broken.` : "") + (errors ? ` ${errors} sources fail to poll; Fleet lists them.` : ""));
}

function installServe(btn) {
  sayAt("serve", null, "Installing serve. It loads the background job and readies each repo's labels, so it can take a minute.");
  return act(btn, "serve", "/api/serve/install", {}, (r) => r.restarting
    ? "Serve is restarting. This page stops answering for a few seconds and comes back once serve is up."
    : `Installed serve${r.said ? `: ${r.said}` : ""}.`);
}

function approvalRow(a) {
  return el("tr", {},
    cell(a.loop, "nb"),
    cell([a.what,
      a.failed ? el("div", {}, stateChip("fault"), ` Delivery failed: ${a.failed}`) : null,
      a.detail ? fold(`approval:${a.id}`, "Show the body", el("pre", { class: "fold-text", text: a.detail })) : null]),
    cell(clock(a.created)),
    cell([button(a.failed ? "Try again" : "Approve", `approve:${a.id}`, (b) => decide(a.id, true, b)), " ",
      button("Reject", `reject:${a.id}`, (b) => decide(a.id, false, b))], "nb"));
}

async function decide(id, yes, btn) {
  btn.disabled = true;
  if (yes) say("loops-result", null, `Approving #${id}. A push rebases and runs its check again before it lands, so it can take a minute.`);
  try {
    const r = await call(`/api/approvals/${id}`, { approve: yes });
    say("loops-result", "ok", yes ? `Approved #${id}: ${r.result}.` : `Rejected #${id}. Nothing was sent.`);
  } catch (e) {
    say("loops-result", "fault", e.message);
  }
  document.getElementById("loops-result").focus();
  refresh();
}

function lastRun(l) {
  if (l.running) return ["running", el("br"), button("Feed", `feed:${l.running}`, () => openFeed("loop", l.running))];
  if (!l.last) return "never";
  return [...runStatus(l.last.status), el("br"), el("span", { class: "muted", text: ago(l.last.started) })];
}

function loopRow(l) {
  return el("tr", {},
    cell([el("span", { class: "nb", text: l.name }), el("div", { class: "muted", text: l.error ? l.error : l.description })]),
    cell(l.error ? stateChip("fault") : l.enabled ? "on" : "off", "nb"),
    cell(l.kind),
    cell(l.trigger, "nb"),
    cell(lastRun(l), "nb"),
    cell(l.next_due ? clock(l.next_due) : "", "nb"),
    cell(money(l.spent_24h), "num"),
    cell(el("div", { class: "loop-buttons" },
      button("Open", `open:${l.name}`, () => openLoopDetail(l.name)),
      button("Start", `start:${l.name}`, (b) => loopAction(l.name, "start", b), Boolean(l.error || l.running)),
      button("Try", `try:${l.name}`, (b) => loopAction(l.name, "try", b), Boolean(l.error || l.running)),
      button(l.enabled ? "Turn off" : "Turn on", `switch:${l.name}`, (b) => loopAction(l.name, l.enabled ? "disable" : "enable", b), Boolean(l.error && !l.enabled)))));
}

function drawLoops(data, approvals) {
  fill("loops",
    heading(1, "Approvals"),
    approvals.length
      ? table("Held until you decide", [["Loop", "c-kind"], ["What it does"], ["Held", "c-time"], ["", "c-decide"]], approvals.map(approvalRow))
      : quiet("Nothing waits for you."),
    heading(2, "Loops"),
    serveLine(data.serve),
    data.loops.length
      ? table("Spent counts the last 24 hours", [["Loop", "c-loop"], ["On", "c-on"], ["Kind"], ["Trigger"], ["Last run", "c-status"], ["Next", "c-time"], ["Spent", "num"], [""]],
        data.loops.map(loopRow))
      : quiet("No loop is installed. Draft one from a sentence below."));
  fillLoopNew(data.draft);
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
  const btn = document.getElementById("loop-new-go");
  btn.disabled = true;
  try {
    const r = await call("/api/loops/new", { sentence: value("sentence"), name: value("name"), repo: value("repo"), model: value("model") });
    say("loop-new-result", "ok", `Started a draft at ${clock(r.started)}. Its feed is open below. The loop lands turned off, so try it before you turn it on.`);
    for (const id of ["sentence", "name"]) document.getElementById(`loop-new-${id}`).value = "";
    await openFeed("draft", r.id);
    refresh();
  } catch (e) {
    say("loop-new-result", "fault", e.message);
  } finally {
    btn.disabled = false;
  }
  document.getElementById("loop-new-result").focus();
}

async function loopAction(name, action, btn) {
  btn.disabled = true;
  try {
    const r = await call(`/api/loops/${encodeURIComponent(name)}/${action}`, {});
    if (action === "start" || action === "try") {
      say("loops-result", "ok", action === "start" ? `Started ${name} at ${clock(r.started)}. Its feed is open below.`
        : `Started a try of ${name} at ${clock(r.started)}. Its feed, open below, shows what each emit would do, and none is sent.`);
      await openFeed("loop", r.id);
    } else {
      if (r.enabled && !r.serve_up) say("loops-result", "caution", `${name} is on, but serve is down, so it fires once serve runs again.`);
      else say("loops-result", "ok", `${name} is ${r.enabled ? "on. Serve fires it from its next tick" : "off. A run already going finishes"}.`);
    }
  } catch (e) {
    say("loops-result", "fault", e.message);
  }
  document.getElementById("loops-result").focus();
  refresh();
}

async function openLoopDetail(name) {
  openLoop = name;
  await refreshLoopDetail();
  document.getElementById("loop-detail").scrollIntoView({ block: "start" });
}

async function refreshLoopDetail() {
  if (!openLoop) return;
  const box = document.getElementById("loop-detail");
  try {
    drawLoopDetail(await call(`/api/loops/${encodeURIComponent(openLoop)}`));
  } catch (e) {
    fill("loop-detail", heading(4, openLoop), fault(`The loop could not be read: ${e.message}`), closeLoopButton());
  }
  box.hidden = false;
}

function closeLoopButton() {
  return el("div", { class: "actions" }, button("Close the detail", "close-loop", () => {
    openLoop = "";
    document.getElementById("loop-detail").hidden = true;
  }));
}

function budgetWords(d) {
  if (d.metered === false) return "none, claude runs on the subscription";
  return [d.budget_usd ? `${money(d.budget_usd)} a run` : "", d.daily_budget_usd ? `${money(d.daily_budget_usd)} a day` : ""].filter(Boolean).join(", ") || "none";
}

function loopFacts(d) {
  return el("dl", { class: "w" },
    fact("Kind", d.kind), fact("Folder", el("code", { text: d.dir })), fact("Trigger", d.trigger),
    fact("Webhook", d.webhook_path ? el("code", { text: `POST ${d.webhook_path}` }) : ""),
    fact("Repo", d.repo), fact("Model", d.model ? `${d.model}${d.runner ? ` on ${d.runner}` : ""}` : ""),
    fact("Device", d.device), fact("Queue", d.queue === d.name ? "its own" : d.queue), fact("Budget", budgetWords(d)),
    fact("Emits", d.emits.join(", ") || "nothing"),
    fact("Next", d.next_due ? `${clock(d.next_due)}` : d.enabled ? "" : "off, so never on its own"),
    fact("Waiting", d.waiting.length ? `${d.waiting.length} in line` : ""), fact("Held", d.held));
}

function loopRuns(d) {
  return d.runs.length
    ? table("Newest first", [["Started", "c-time"], ["Trigger", "c-kind"], ["Status", "c-status"], ["Time", "num c-time"], ["Cost", "num c-cost"], ["Result", "c-result"], ["", "c-log"]],
      d.runs.map((r) => el("tr", {},
        cell(clock(r.started), "nb"), cell(r.trigger || ""), cell(runStatus(r.status), "nb"),
        cell(duration(secondsBetween(r.started, r.finished)), "num"), cell(money(r.cost_usd), "num"),
        cell(r.pr_url ? pullLinks(r.pr_url) : r.summary || "", "muted-cell"),
        cell(button("Feed", `feed:${r.id}`, () => openFeed("loop", r.id))))))
    : quiet("It has not run yet.");
}

function loopSources(d) {
  return d.cursors.length
    ? table("", [["Source"], ["Polled", "c-time"], ["State", "c-status"], ["Detail"]],
      d.cursors.map((c) => el("tr", {}, cell(c.source, "muted-cell"), cell(ago(c.polled), "nb"),
        cell(stateChip(c.ok ? "ok" : "fault")), cell(c.detail || (c.ok ? "reading" : "")))))
    : quiet(d.sources.length ? "Serve has not polled its sources yet." : "It has no source.");
}

function loopDigests(d) {
  return d.digests.length
    ? d.digests.map((g) => fold(`digest:${d.name}:${g.day}`, g.day, el("pre", { class: "fold-text", text: g.text })))
    : quiet("No digest yet.");
}

function drawLoopDetail(d) {
  const memory = Object.keys(d.state || {}).length ? JSON.stringify(d.state, null, 2) : "";
  fill("loop-detail",
    heading(4, d.name),
    d.error ? fault(`It does not load: ${d.error}`) : el("p", { text: d.description }),
    loopFacts(d),
    el("h3", { text: "Runs" }), loopRuns(d),
    el("h3", { text: "Findings" }),
    d.findings.length
      ? table("Each finding is reported once, by its fingerprint", [["Finding"], ["Seen", "num c-n"], ["First", "c-time"], ["Last", "c-time"]],
        d.findings.map((f) => el("tr", {}, cell(f.url ? link(f.url, f.title || f.fingerprint) : f.title || f.fingerprint), cell(f.seen, "num"),
          cell(clock(f.first_seen), "nb"), cell(clock(f.last_seen), "nb"))))
      : quiet("No finding yet."),
    el("h3", { text: "Sources" }), loopSources(d),
    d.metrics.length ? [el("h3", { text: "Metrics" }),
      table("The last 30 days", [["Metric"], ["Latest", "num c-cost"], ["When", "c-time"], ["Points", "num c-n"]],
        d.metrics.map((m) => el("tr", {}, cell(m.name), cell(Number(m.value).toLocaleString(), "num"), cell(clock(m.ts), "nb"), cell(m.points, "num"))))] : null,
    d.emits.some((e) => e.startsWith("digest")) ? [el("h3", { text: "Digests" }), loopDigests(d)] : null,
    el("h3", { text: "Memory" }),
    memory ? fold(`memory:${d.name}`, "Show what it keeps between runs", el("pre", { class: "fold-text", text: memory })) : quiet("It keeps nothing between runs yet."),
    d.prompt ? [el("h3", { text: "Prompt" }), fold(`prompt:${d.name}`, "Show the prompt", el("pre", { class: "fold-text", text: d.prompt }))] : null,
    closeLoopButton());
}

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

function sessionRow(s) {
  return el("tr", {},
    cell(clock(s.started), "nb"), cell(runStatus(s.status), "nb"),
    cell([s.model || "", el("br"), el("span", { class: "muted", text: [s.runner, s.device].filter(Boolean).join(" on ") })]),
    cell([s.prompt, s.repo ? el("div", { class: "muted", text: `${s.repo}${s.branch ? ` · ${s.branch}` : ""}` }) : null]),
    cell(money(s.cost_usd), "num"),
    cell(button("Open", `session:${s.id}`, () => openFeed("session", s.id))));
}

function drawLaunch(data) {
  fillChoices(data.choices);
  fill("sessions", heading(2, "Sessions"),
    data.sessions.length
      ? table("Newest first", [["Started", "c-time"], ["Status", "c-status"], ["Model", "c-model"], ["First message"], ["Cost", "num c-cost"], ["", "c-log"]],
        data.sessions.map(sessionRow))
      : quiet("No session yet."));
}

async function launch(event) {
  event.preventDefault();
  const prompt = document.getElementById("launch-prompt");
  const budget = document.getElementById("launch-budget").value.trim();
  if (!prompt.value.trim()) return say("launch-result", "caution", "Write the first message first.");
  if (budget && !(Number(budget) > 0)) return say("launch-result", "caution", "The budget is a number of dollars above zero, such as 0.50, or empty for no limit.");
  const values = launchValues();
  const btn = document.getElementById("launch-go");
  btn.disabled = true;
  try {
    const s = await call("/api/sessions", { prompt: prompt.value.trim(), device: values.device, model: document.getElementById("launch-model").value.trim(),
      runner: values.runner, repo: values.repo, tools: values.tools, budget_usd: budget ? Number(budget) : null });
    say("launch-result", "ok", `Launched: ${s.runner} ${s.model} on ${s.device}. Its feed is open below.`);
    prompt.value = "";
    await openFeed("session", s.id);
    refresh();
  } catch (e) {
    say("launch-result", "fault", e.message);
  } finally {
    btn.disabled = false;
  }
}

async function sendSession(event) {
  event.preventDefault();
  const box = document.getElementById("session-text");
  if (!box.value.trim()) return say("session-result", "caution", "Write a message first.");
  try {
    await call(`/api/sessions/${encodeURIComponent(feeds.session.run)}/send`, { text: box.value.trim() });
    box.value = "";
    say("session-result", "ok", "Sent. The answer shows in the feed when it comes.");
    await refreshFeed("session");
  } catch (e) {
    say("session-result", "fault", e.message);
  }
}

function probeWords(p) {
  if (!p) return "";
  return [p.system, p.detail].filter(Boolean).join(" · ");
}

function deviceRow(d) {
  const p = d.probe;
  return el("tr", {},
    cell([el("span", { class: "nb", text: d.name }), d.note ? el("div", { class: "muted", text: d.note }) : null]),
    cell(d.kind), cell(d.host || "this machine", "muted-cell"),
    cell(p ? [stateChip(p.ok ? "ok" : "fault"), el("br"), el("span", { class: "muted", text: ago(p.at) })] : "not probed", "nb"),
    cell(probeWords(p) || (d.error ? d.error : ""), "muted-cell"),
    cell([p ? (p.tools || []).join(", ") || "none found" : "", p?.gpus?.length ? el("div", { text: p.gpus.join(", ") }) : null]),
    cell(button("Probe", `probe:${d.name}`, (b) => probe(d.name, b))));
}

async function probe(name, btn) {
  btn.disabled = true;
  btn.textContent = "Probing";
  try {
    const p = await call(`/api/devices/${encodeURIComponent(name)}/probe`, {});
    say("fleet-result", p.ok ? "ok" : "fault", p.ok ? `${name} answered in ${p.seconds} s${p.system ? `: ${p.system}` : ""}.` : `${name} did not answer: ${p.detail || "no detail"}.`);
  } catch (e) {
    say("fleet-result", "fault", e.message);
  }
  refresh();
}

function drawFleet(loops, devices) {
  const s = loops.serve;
  const errors = Object.entries(s.poll_errors || {});
  const broken = Object.entries(s.broken || {});
  fill("fleet",
    heading(1, "Serve"),
    serveLine(s),
    s.at ? el("dl", { class: "w" }, fact("Process", String(s.pid)), fact("Last beat", `${ago(s.at)} (${clock(s.at)})`),
      fact("Ticks", `${s.ticks}, one every ${s.tick_seconds} s`), fact("Started", clock(s.started))) : null,
    errors.length ? table("Sources that fail to poll", [["Source"], ["Error"]], errors.map(([k, v]) => el("tr", {}, cell(k), cell(v, "muted-cell")))) : null,
    broken.length ? table("Loops that do not load", [["Loop"], ["Why"]], broken.map(([k, v]) => el("tr", {}, cell(k), cell(v, "muted-cell")))) : null,
    heading(2, "Devices"),
    el("p", { text: "Loops and sessions run on these. A probe checks the device answers and lists its tools; devices.toml in the config folder adds one." }),
    table("", [["Device", "c-device"], ["Kind", "c-kind"], ["Host", "c-ref"], ["State", "c-status"], ["System"], ["Tools"], ["", "c-probe"]], devices.map(deviceRow)));
}

function costTable(caption, rows) {
  return rows.length ? table(caption, [["Name"], ["Calls", "num c-n"], ["Tokens in", "num c-rate"], ["Tokens out", "num c-rate"], ["Cost", "num c-cost"]],
    rows.map((r) => el("tr", {}, cell(r.name), cell(r.steps, "num"), cell(r.tokens_in.toLocaleString(), "num"),
      cell(r.tokens_out.toLocaleString(), "num"), cell(money(r.cost_usd), "num")))) : quiet("No model call in this window.");
}

function costParts(c) {
  if (!c) return [];
  return [heading(5, "Where it went"),
    el("p", {}, `${money(c.total_usd)} in ${c.days} days, counted from each model call: pipeline runs, loops, sessions and chat alike.`),
    el("h3", { text: "By model" }), costTable("", c.by_model),
    el("h3", { text: "By runner" }), costTable("", c.by_runner),
    el("h3", { text: "By loop or kind" }), costTable("", c.by_loop)];
}

const LOADERS = {
  live: async () => {
    drawLive(await call("/api/state"));
    await refreshLog();
  },
  loops: async () => {
    const [loops, approvals] = await Promise.all([call("/api/loops"), call("/api/approvals")]);
    drawLoops(loops, approvals.approvals);
    await Promise.all([refreshLoopDetail(), refreshFeed("loop"), refreshFeed("draft")]);
  },
  launch: async () => {
    drawLaunch(await call("/api/sessions"));
    await refreshFeed("session");
  },
  fleet: async () => {
    const [loops, fleet] = await Promise.all([call("/api/loops"), call("/api/devices")]);
    drawFleet(loops, fleet.devices);
  },
  health: async () => drawHealth(await call("/api/state")),
  triage: async () => {
    const snapshot = await call("/api/state");
    if (!redrawTriage && (sending.size || document.activeElement?.closest("#triage form"))) return HELD;
    redrawTriage = false;
    drawTriage(snapshot);
  },
  trends: async () => {
    trendsData = await call("/api/trends");
    costData = trendsData.rows ? await call(`/api/costs?days=${trendsData.rows.days.length}`) : null;
    drawTrends();
  },
  settings: async () => {
    const fresh = await call("/api/settings");
    if (settingsData && Object.keys(settingsChanges()).length) return HELD;
    drawSettings(fresh);
  },
  chat: async () => drawChat(await call("/api/chat")),
};

function nextDelay() {
  if (view === "live" || view === "loops" || going[view]) return LIVE_MS;
  if (view === "launch") return feeds.session.run && feeds.session.status === "running" ? CHAT_BUSY_MS : SLOW_MS;
  if (view === "chat") return chatSeen.endsWith(":true") ? CHAT_BUSY_MS : SLOW_MS;
  return SLOW_MS;
}

function schedule(ms) {
  clearTimeout(timer);
  if (ms) timer = setTimeout(refresh, ms);
}

function status(state, sentence) {
  const line = document.getElementById("status");
  line.replaceChildren(`${dateWords(new Date())} · `, ...sayParts(state, sentence));
}

async function refresh() {
  clearTimeout(timer);
  const mine = ++refreshCount;
  if (document.hidden) return;
  try {
    const held = (await LOADERS[view]()) === HELD;
    if (mine !== refreshCount) return;
    if (!held) lastGood = clock(new Date());
    const label = document.querySelector(`.views [data-view="${view}"]`).textContent;
    const shown = held ? "held while you edit" : `updated ${new Date().toLocaleTimeString([], { hour12: false })}`;
    status("", `${label} · ${shown}${nextDelay() ? ` · refreshes every ${nextDelay() / 1000} s` : ""}`);
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
  for (const primary of document.querySelectorAll("[data-act]")) primary.classList.toggle("act", primary.closest("section").dataset.view === view);
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
  document.getElementById("launch").addEventListener("submit", launch);
  document.getElementById("loop-new").addEventListener("submit", draftLoop);
  document.getElementById("loop-new-sentence").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) document.getElementById("loop-new").requestSubmit();
  });
  document.getElementById("launch-device").addEventListener("change", repoFollowsDevice);
  document.getElementById("launch-reset").addEventListener("click", async () => {
    document.getElementById("launch").reset();
    document.getElementById("launch-result").hidden = true;
    fillChoices((await call("/api/sessions")).choices, true);
  });
  document.getElementById("session-send").addEventListener("submit", sendSession);
  document.getElementById("session-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) document.getElementById("session-send").requestSubmit();
  });
  for (const [which, resultId] of [["loop", "loop-feed-result"], ["draft", "draft-feed-result"], ["session", "session-result"]]) {
    const box = document.getElementById(feeds[which].box);
    box.querySelector(".feed-close").addEventListener("click", () => closeFeed(which));
    box.querySelector(".feed-stop").addEventListener("click", (event) => stopFeedRun(which, event.currentTarget, resultId));
  }
  for (const [id, path, resultId] of [["chat-new", "/api/chat/new", "chat-result"],
    ["draft-file", "/api/chat/file", "draft-result"], ["draft-drop", "/api/chat/drop", "draft-result"]]) {
    document.getElementById(id).addEventListener("click", (event) => chatAction(path, event.currentTarget, resultId));
  }
  document.getElementById("chat-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) document.getElementById("chat").requestSubmit();
  });
  show(location.hash.slice(1));
});
