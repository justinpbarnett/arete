"use strict";

const FEED_KEEP_LINES = 2000;
let feedOf = { id: "", epoch: null, after: 0, lines: [] };
let feedRequest = 0;
let messagePage = { run: "", after: 0 };
let sessionSending = false;
const talkDrafts = new Map();

async function loadRunDetail(id) {
  const request = ++feedRequest;
  if (feedOf.id !== id) feedOf = { id, epoch: null, after: 0, lines: [] };
  if (messagePage.run !== id) messagePage = { run: id, after: 0 };
  const messages = messagePage.after ? `?messages_after=${messagePage.after}` : "";
  const board = await call(`/api/runs/${encodeURIComponent(id)}${messages}`);
  if (board.lines_from === "feed") {
    let after = feedOf.id === id ? feedOf.after : 0;
    let data = await call(`/api/feed/${encodeURIComponent(id)}?after=${after}`);
    if (request !== feedRequest || feedOf.id !== id) return { board, feed: { data, asked: after, request } };
    if (feedOf.epoch !== null && data.restore_epoch < feedOf.epoch) return { board, feed: { data, asked: after, request } };
    if (feedOf.epoch !== null && data.restore_epoch !== feedOf.epoch) {
      feedOf = { id, epoch: data.restore_epoch, after: 0, lines: [] };
      after = 0;
      data = await call(`/api/feed/${encodeURIComponent(id)}?after=0`);
    }
    return { board, feed: { data, asked: after, request } };
  }
  return { board, log: await call(`/api/log/${encodeURIComponent(id)}`) };
}

function mergeFeed(id, { data, asked, request }) {
  if (request !== feedRequest || feedOf.id !== id) return feedOf.id === id ? feedOf.lines : [];
  if (feedOf.epoch !== null && data.restore_epoch < feedOf.epoch) return feedOf.lines;
  if (feedOf.epoch !== data.restore_epoch) {
    feedOf = { id, epoch: data.restore_epoch, after: 0, lines: [] };
    if (asked !== 0) return feedOf.lines;
  }
  if (asked === feedOf.after) {
    const fresh = data.lines.filter((line) => line.id > feedOf.after);
    if (fresh.length) {
      feedOf.lines = [...feedOf.lines, ...fresh.map(feedLine)].slice(-FEED_KEEP_LINES);
      feedOf.after = fresh.at(-1).id;
    }
  }
  return feedOf.lines;
}

function paidWords(r, models) {
  if (r.paid > 0) return money(r.paid);
  return models.length && models.every(m => (m.billing || m.auth_mode) === "subscription") ? "nothing, it ran on the subscription" : "nothing paid";
}

function modelTable(models) {
  return table("", [["Model"], ["Runner", "c-kind"], ["Calls", "num c-n"], ["Tokens in", "num c-rate wide-only"], ["Tokens out", "num c-rate wide-only"], ["Paid", "num c-cost"]],
    models.map((m) => el("tr", {}, cell(wrapAtSlashes(m.model)), cell(m.runner), cell(m.steps, "num"), cell(m.tokens_in.toLocaleString(), "num"),
      cell(m.tokens_out.toLocaleString(), "num"), cell(["local", "replay"].includes(m.billing) ? m.billing : m.metered ? money(m.paid) : "subscription", "num"))), { stack: true });
}

function orchestrationDetail(board, place) {
  const data = board.orchestration;
  if (!data) return null;
  const run = board.run;
  const retry = (action, btn) => act(btn, place,
    `/api/runs/${encodeURIComponent(run.id)}/steps/${encodeURIComponent(action.id)}`, {},
    (result) => { if (result.run) go(detailHash("run", result.run), "action");
      return `Started ${action.id} alone. Dependent evidence must be refreshed before publication.`; }, { object: run });
  const eligible = (a) => data.mode === "orchestrate" && data.workspace_retained && run.status !== "running" && data.current_run === run.id
    && (!data.workers || data.workers.every(w => ["completed", "cancelled", "integrated"].includes(w.phase)))
    && !a.result.depends_on && (!data.workers || !a.op.startsWith("worker.") || a.result.effective)
    && ["succeeded", "failed", "cancelled", "interrupted"].includes(a.state)
    && ["worker.start", "worker.continue", "checks.run", "verification.run", "review.run"].includes(a.op);
  return [el("h3", { text: "Orchestration" }),
    el("p", { text: `${data.mode} · ${data.decisions} decisions · ${data.launches} controller launches · generation ${data.generation}` }),
    ...(data.capacity ? [
      el("h4", { text: "Workers" }),
      el("p", { text: `${data.capacity.active_builders} active builders · limit ${data.capacity.effective_builders} (${data.capacity.configured_builders} configured)` }),
      table("", [["Worker"], ["Current attempt"], ["State"], ["Question or disposition"]], data.workers.map(w =>
        el("tr", {}, cell(w.id), cell(w.attempt), cell(w.phase), cell(w.reason || w.question || ""))), { stack: true }),
      ...data.messages.map(m => foldText(`message:${run.id}:${m.id}`, `${m.kind} · ${m.worker} · ${m.state}`,
        [m.body, m.id, m.reply_to, ...m.history.map(e => `${e.at}  ${e.state}  ${e.action}`)].filter(Boolean).join("\n"))),
      ...(data.more_messages || messagePage.after ? [button(data.more_messages ? "Next messages" : "First messages",
        "message-page", () => { messagePage.after = data.more_messages ? data.message_cursor : 0; refresh(); })] : []),
    ] : []),
    table("", [["Action"], ["Operation"], ["State"], ["Result"], [""]], data.actions.map((a) => el("tr", {},
      cell(a.id), cell(a.op), cell(a.state),
      cell(foldText(`action:${run.id}:${a.id}`, "Inspect result and requested settings", JSON.stringify({ request: a.request, result: a.result }, null, 2))),
      cell(eligible(a) ? button("Rerun alone", "rerun-action", (b) => retry(a, b), { of: a.id }) : null))), { stack: true })];
}

function drawRunDetail(pane, result, kind) {
  const id = route.detail.id;
  const place = `detail:${kind}:${id}`;
  if (result.error) {
    fill(`${pane}-head`, detailHead(pane, kind, id, id));
    fill(`${pane}-body`, unreadFault(`This ${kind}`, result));
    fill(`${pane}-talk`);
    return SLOW_MS;
  }
  const { board, feed, log } = result.value;
  const r = board.run;
  const running = r.status === "running";
  const lines = feed ? mergeFeed(id, feed) : log.lines;
  const shownLines = document.querySelector(`#${pane}-body pre.lines`);
  const stick = !shownLines || shownLines.scrollTop + shownLines.clientHeight >= shownLines.scrollHeight - 4;
  const s = board.session;
  fill(`${pane}-head`, detailHead(pane, kind, id, s?.prompt ? firstMessage(s.prompt, TITLE_CHARS) : r.ticket || id,
    running ? button(r.stop_asked ? "Stopping" : "Stop", "stop", (b) => stopRun(r, place, b), { of: id, disabled: Boolean(r.stop_asked) }) : null));
  fill(`${pane}-body`,
    saidLine(place),
    el("dl", { class: "w" },
      fact("Status", runStatus(r.status)), fact("Kind", r.kind), fact("Repo", r.repo), fact("Loop", r.loop),
      fact("Model", modelOnRunner(r.model, r.runner)), fact("Device", r.device),
      fact("Started", r.started ? [zonedClock(r.started), " · ", liveAgo(r.started)] : ""),
      fact("Time", duration(secondsBetween(r.started, r.finished))),
      fact("Paid", paidWords(r, board.models)),
      fact("Pull request", r.pr_url ? pullLinks(r.pr_url) : ""),
      fact("Summary", r.summary ? withLinks(r.summary) : "")),
    board.approvals.length ? [el("h3", { text: "Waiting for you" }),
      table("", NEEDS_HEADS, board.approvals.map((a) => approvalRow(a, place)), { stack: true })] : null,
    s ? [el("h3", { text: "Session" }), el("dl", { class: "w" },
      fact("Folder", s.cwd ? pathCode(s.cwd) : ""), fact("Repo", s.repo), fact("Tools", s.tools.join(", ") || "none")),
    s.prompt ? foldText(`prompt:${id}`, "Show the first message", s.prompt) : null] : null,
    board.models.length ? [el("h3", { text: "Models" }), modelTable(board.models)] : null,
    orchestrationDetail(board, place),
    el("h3", { text: feed ? "Feed" : "Log" }),
    el("pre", { class: `lines ${feed ? "feed" : "log"}`, tabindex: "0", "data-focus": "lines", "aria-label": feed ? "The feed" : "The log" },
      lines.length ? lines.map((text) => el("span", { text })) : feed ? "Nothing in the feed yet." : "This run has no log yet."));
  if (stick) {
    const fresh = document.querySelector(`#${pane}-body pre.lines`);
    fresh.scrollTop = fresh.scrollHeight;
  }
  fill(`${pane}-talk`, r.kind === "session" && running ? talkForm(pane, id) : null);
  if (r.kind === "session" && running) return CHAT_BUSY_MS;
  return running ? LIVE_MS : SLOW_MS;
}

function stopRun(run, place, btn) {
  return act(btn, place, `/api/runs/${encodeURIComponent(run.id)}/stop`, {}, (r) => stopWords(run.id, r, "The feed says when it has."), { object: run });
}

function talkForm(pane, id) {
  const textId = `${pane}-text`;
  const text = el("textarea", { id: textId, rows: "3", spellcheck: "true", "data-focus": textId, on: {
    input: (e) => talkDrafts.set(id, e.target.value),
    keydown: submitOnCtrlEnter,
  } });
  text.value = talkDrafts.get(id) || "";
  return el("form", { class: "talk", novalidate: true, "aria-label": "Send the session a message", on: { submit: (e) => sendSession(e, id) } },
    el("div", { class: "field" }, el("label", { for: textId, text: "Message" }), text),
    el("div", { class: "actions" },
      el("button", { type: "submit", "data-focus": `${pane}-send`, disabled: sessionSending, text: "Send" }),
      el("span", { class: "muted keys", text: "Ctrl or Cmd with Enter sends" })),
    saidLine(`talk:${id}`));
}

async function sendSession(event, id) {
  event.preventDefault();
  if (sessionSending) return;
  const form = event.currentTarget;
  const box = form.querySelector("textarea");
  const text = box.value.trim();
  if (!text) return sayAt(`talk:${id}`, "caution", "Write a message first.", { focus: false });
  trackAction("session-send");
  sessionSending = true;
  form.querySelector("button[type=submit]").disabled = true;
  try {
    await call(`/api/sessions/${encodeURIComponent(id)}/send`, { text });
    talkDrafts.delete(id);
    box.value = "";
    sayAt(`talk:${id}`, "ok", "Sent. The answer shows in the feed when it comes.", { focus: false });
  } catch (e) {
    sayAt(`talk:${id}`, "fault", e.message, { focus: false });
  } finally {
    sessionSending = false;
  }
  refresh();
}

function feedLine(line) {
  return `${clock(line.ts).padStart(5)}  ${line.kind.padEnd(7)}  ${line.text}`;
}
