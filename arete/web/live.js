"use strict";

function drawLive(d) {
  fill("live-body",
    heading(1, "Running now"),
    panelParts(d.live, "The live runs", runningParts),
    panelParts(d.waiting, "The review feedback", upNextParts),
    heading(2, "Needs you"),
    needsParts(d.needs_you),
    saidLine("needs"));
  return LIVE_MS;
}

function runningParts(live) {
  if (!live.runs.length) return [quiet("Nothing is running."), saidLine("running")];
  const paid = paidColumn(live.runs, { onPhone: true });
  return [
    el("p", { class: "readout" }, String(live.slots), el("small", { text: `of ${live.max_parallel} pipeline slots busy${live.waiting ? `, ${live.waiting} waiting for one` : ""}` })),
    table("", [["Work", "c-work"], ["Kind", "c-kind wide-only"], ["Started", "c-time"], ["Last step"], ["Time", "num c-duration wide-only"], ...paid.head, ["", "c-run-buttons"]],
      live.runs.map((r) => linkedRow(detailHash("run", r.id),
        cell(rowLink("run", r.id, runName(r))), cell(r.kind), cell(clock(r.started), "nb"), cell(r.slot || r.step || "starting", "muted-cell"),
        cell(duration(r.seconds), "num"), paid.cell(r),
        cell(button(r.stopping ? "Stopping" : "Stop", "stop", (b) => stopLiveRun(r, b), { of: r.id, disabled: r.stopping })))), { stack: true }),
    saidLine("running"),
  ];
}

function upNextParts(waiting) {
  if (!waiting.tickets.length) return null;
  return [
    el("h3", { text: "Up next" }),
    el("p", {}, `Review feedback runs ${waiting.quiet_minutes} min after the last comment, or at once when a review is submitted.`),
    table("", [["Ticket"], ["Comments", "num c-n"], ["When"]],
      waiting.tickets.map((w) => el("tr", {}, cell(w.ticket), cell(w.comments, "num"),
        cell(w.at_once ? `review submitted ${clock(w.submitted)}` : `last comment ${clock(w.last)}`))), { stack: true }),
  ];
}

function stopLiveRun(run, btn) {
  return act(btn, "running", `/api/runs/${encodeURIComponent(run.id)}/stop`, {},
    (r) => stopWords(run.ticket, r, "It leaves this list once it has."));
}

const NEEDS_HEADS = [["Kind", "c-kind"], ["Repo", "c-repo"], ["What"], ["Since", "c-time"], ["", "c-decide"]];

function kindTag(text) {
  return el("span", { class: "tag", text });
}

function repoCell(repo) {
  return cell(repo || el("span", { class: "muted", text: "none" }), "nb");
}

function needsParts(n) {
  const rows = [];
  const faults = [];
  const take = (panel, what, toRows) => {
    if (panel && "rows" in panel) rows.push(...toRows(panel.rows));
    else faults.push(unreadFault(what, panel));
  };
  take(n.approvals, "The approvals", (approvals) => approvals.map((a) => approvalRow(a, "needs")));
  take(n.ready, "The ready tickets", (ready) => ready.map(readyRow));
  take(n.skipped, "The skipped tickets", (skipped) => skipped.map(skippedRow));
  if (n.draft) rows.push(draftRow(n.draft));
  let notes = [];
  take(n.proposals, "The fixes to make by hand", (p) => {
    notes = [
      p.hand.length ? quiet("By hand: Claude Code will not let an agent write under .claude/, so the miner leaves these harness fixes to you. Make each one, then mark it done.") : null,
      p.memory.length ? quiet("Memory: facts the miner noticed for an agent's memory. File each one by hand, then mark it done.") : null,
    ];
    return [...p.hand.map((x) => proposalRow(x, "By hand", x.file, `The fix to ${x.repo}/${x.file}`)),
      ...p.memory.map((x) => proposalRow(x, "Memory", "", `The ${x.repo} fact`))];
  });
  return [
    faults,
    rows.length
      ? table("", NEEDS_HEADS, rows, { stack: true })
      : faults.length ? null : el("p", {}, stateChip("ok"), " Nothing needs you."),
    notes,
  ];
}

function approvalRow(a, place) {
  const asked = `${a.loop}: ${a.what}`;
  const details = [a.plain_summary ? asked : "", a.detail].filter(Boolean).join("\n\n");
  return el("tr", {},
    cell(kindTag(a.failed ? "Failed" : "Approval")),
    repoCell(a.repo),
    cell([el("div", { text: a.plain_summary || asked }),
      a.failed ? el("div", {}, stateChip("fault"), ` Delivery failed: ${a.failed}`) : null,
      a.held ? el("div", { class: "muted", text: `Held: ${a.held}.` }) : null,
      reviewLine(a),
      details ? foldText(`approval:${a.id}`, "Show the details", details) : null,
      a.run_id && place === "needs" ? el("div", {}, itemLink("run", a.run_id, "Open the run that asked", `asked:${a.id}`)) : null]),
    cell(liveAgo(a.created), "nb"),
    cell(el("div", { class: "row-buttons" },
      button(a.failed ? "Try again" : "Approve", "approve", (b) => decide(a.id, true, b, place), { of: a.id }),
      button("Reject", "reject", (b) => decide(a.id, false, b, place), { of: a.id }))));
}

function reviewLine(a) {
  const r = a.review || {};
  if (r.decision) return el("div", {}, el("span", { class: "muted", text: `Review by ${r.by}: ` }), `${r.decision}, ${r.confidence} confidence. ${r.reason}`);
  return a.reviewer && !a.failed ? el("div", { class: "muted", text: `Review by ${a.reviewer}: not read yet.` }) : null;
}

function decide(id, yes, btn, place) {
  if (yes) sayAt(place, null, `Approving #${id}. A push rebases and runs its check again before it lands, so it can take a minute.`);
  return act(btn, place, `/api/approvals/${id}`, { approve: yes },
    (r) => (yes ? `Approved #${id}: ${r.result}.` : `Rejected #${id}. Nothing was sent.`));
}

function readyRow(r) {
  return el("tr", {},
    cell(kindTag("Ready")),
    repoCell(r.repo),
    cell([el("div", {}, rowLink("ticket", r.ref, r.ref.replace(/^[^/#\s]+\//, "")), `: ${r.title}`), el("div", { class: "muted", text: r.verdict })]),
    cell(""),
    cell(el("div", { class: "row-buttons" }, button("Send", "send-ready", (b) => sendReady(r, b), { of: r.ref }))));
}

function sendReady(r, btn) {
  sayAt("needs", null, `Sending ${r.ref}. It screens the ticket and writes to it, so it can take a few seconds.`);
  return act(btn, "needs", `/api/triage/${encodeURIComponent(r.ref)}/send`,
    { questions: r.questions, answers: r.questions.map(() => ""), repo: r.repo }, (d) => d.said);
}

function skippedRow(s) {
  return el("tr", {},
    cell(kindTag("Skipped")),
    repoCell(s.repo),
    cell([el("div", {}, link(s.url, s.ticket)), el("div", { class: "muted", text: s.why })]),
    cell(liveAgo(s.since), "nb"),
    cell(s.can_run ? el("div", { class: "row-buttons" }, button("Run anyway", "run-anyway", (b) => runSkipped(s.ticket, b), { of: s.ticket }))
      : el("span", { class: "muted", text: "no repo to run in yet" })));
}

function runSkipped(ticket, btn) {
  return act(btn, "needs", `/api/skipped/${encodeURIComponent(ticket)}/run`, {},
    () => `Started ${ticket} past its blocker at ${clock(new Date())}. It shows under Running now while it runs.`);
}

function draftRow(draft) {
  return el("tr", {},
    cell(kindTag("Draft")),
    repoCell(draft.repo),
    cell([el("div", { text: draft.title }), el("div", { class: "muted", text: `The console drafted this issue on ${draft.repo}. Nothing reaches GitHub until you file it.` }),
      foldText("draft:body", "Show the body", draft.body)]),
    cell(""),
    cell(el("div", { class: "row-buttons" },
      button("File this issue", "file-draft", fileDraft),
      button("Drop", "drop-draft", dropDraft),
      button("Open the console", "open-console", () => setConsole(true, { focus: true })))));
}

function fileDraft(btn) {
  return act(btn, "needs", "/api/chat/file", {}, (view) => {
    drawChat(view);
    return view.messages.at(-1)?.text || "Filed the issue.";
  });
}

function dropDraft(btn) {
  return act(btn, "needs", "/api/chat/drop", {}, (view) => {
    drawChat(view);
    return "Dropped the drafted issue. Nothing reached GitHub.";
  });
}

function proposalRow(x, kind, file, what) {
  return el("tr", {},
    cell(kindTag(kind)),
    repoCell(x.repo),
    cell([file ? el("div", { class: "wrap-cell", text: file }) : null, el("div", { class: file ? "muted" : "", text: x.change })]),
    cell(""),
    cell(el("div", { class: "row-buttons" }, button("Mark done", "done", (b) => markDone(x.id, what, b), { of: x.id }))));
}

function markDone(id, what, btn) {
  return act(btn, "needs", `/api/proposals/${id}/done`, {}, () => `${what} is marked done, so it leaves this list.`);
}
