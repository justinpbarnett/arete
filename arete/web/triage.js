"use strict";

let forceTriageDraw = false;
const sendSaid = new Map();
const sending = new Set();

function refreshTriage(btn) {
  return act(btn, "triage-refresh", "/api/triage/refresh", {},
    () => `Started triage at ${clock(new Date())}. It reads every open ticket, so it can take a few minutes; the verdicts here change when it ends.`);
}

function researchTicket(r, btn) {
  return act(btn, `research:${r.ref}`, `/api/triage/${encodeURIComponent(r.ref)}/research`, { force: r.research.force },
    () => `Asked the research loop at ${clock(new Date())} to research ${r.ref}. Its verdict follows the research.`);
}

function researchOffer(r, researching) {
  return [
    r.research ? el("div", { class: "actions offer" },
      button(r.research.label, "research", (b) => researchTicket(r, b), { of: r.ref, disabled: researching }),
      r.research.force ? el("span", { class: "muted", text: "This skips JEV's screen for instructions aimed at an agent, so read the ticket first." }) : null) : null,
    r.research || lastSaid.has(`research:${r.ref}`) ? saidLine(`research:${r.ref}`) : null,
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
  const repoPicker = el("select", { id: id("repo"), "data-focus": id("repo"), on: { change: (e) => {
    drafts.set(draftKey(r.ref, "repo"), e.target.value);
    e.target.form.querySelector(".send-about").textContent = sendWords(r);
  } } }, el("option", { value: "", text: "Pick a repo" }), r.send.choices.map((c) => el("option", { value: c.repo, text: c.repo })));
  repoPicker.value = pickedRepo(r);
  const box = (q, i) => {
    const answer = el("textarea", { id: id(i), "data-focus": id(i), on: {
      input: (e) => drafts.set(draftKey(r.ref, q), e.target.value),
      keydown: submitOnCtrlEnter,
    } });
    answer.value = drafts.get(draftKey(r.ref, q)) || "";
    return answer;
  };
  const fields = r.questions.map((q, i) => field(isRepoQuestion(r, q) ? repoPicker : box(q, i), q));
  if (r.send.pick && !r.send.repo_question) fields.push(field(repoPicker, "The repo to build it in"));
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
  trackAction("ticket-send");
  sending.add(r.ref);
  sendSay(form, r, null, `Sending ${r.ref}. It screens the ticket and writes to it, so it can take a few seconds.`);
  form.querySelector("button[type=submit]").disabled = true;
  try {
    const answers = r.questions.map((q) => (isRepoQuestion(r, q) ? "" : drafts.get(draftKey(r.ref, q)) || ""));
    const done = await call(`/api/triage/${encodeURIComponent(r.ref)}/send`, { questions: r.questions, answers, repo });
    for (const key of drafts.keys()) if (key.startsWith(draftKey(r.ref, ""))) drafts.delete(key);
    sendSaid.delete(r.ref);
    lastSaid.set(`ticket:${r.ref}`, ["ok", done.said]);
    focusAfterDraw = `said:ticket:${r.ref}`;
  } catch (e) {
    sendSay(form, r, "fault", e.message);
  } finally {
    sending.delete(r.ref);
    form.querySelector("button[type=submit]").disabled = false;
  }
  forceTriageDraw = true;
  refresh();
}

function groupSlug(standing) {
  return standing.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function defaultGroup(groups) {
  const withRows = (slug) => groups.find((g) => g.slug === slug && g.rows.length);
  return withRows("ready-to-build") ?? withRows("with-questions") ?? groups.find((g) => g.rows.length) ?? groups[0];
}

function shortRef(ref) {
  return ref.replace(/^[^/#\s]+\//, "");
}

function drawTriage(panel, r) {
  if (!("rows" in panel)) {
    fill("triage-list", unreadFault("Triage", panel));
    if (r.detail) {
      fill("triage-detail-head", detailHead("triage-detail", "ticket", r.detail.id, r.detail.id, [], [null, null]));
      fill("triage-detail-body", unreadFault("Triage", panel));
    }
    return SLOW_MS;
  }
  const t = panel.rows;
  const groups = t.groups.map((g) => ({ ...g, slug: groupSlug(g.standing) }));
  const holder = r.detail && groups.find((g) => g.rows.some((row) => row.ref === r.detail.id));
  if (holder) rememberQuery("triage", { group: holder.slug });
  const q = r.detail ? lastQuery.triage || {} : r.query;
  const picked = groups.find((g) => g.slug === q.group) ?? defaultGroup(groups);
  if (picked) settleQuery({ group: picked.slug });
  const lastRan = t.refreshed ? ["Triage last ran ", liveAgo(t.refreshed), ` (${zonedClock(t.refreshed)}).`] : ["Triage has not run yet."];
  fill("triage-list",
    quiet(...lastRan, " It reads every open ticket, says what kind it is and what it needs, sends ready code tickets to the pipeline and research tickets to the research agent."),
    el("div", { class: "actions" }, button(t.refreshing ? "Triage running" : "Refresh triage now", "triage-refresh", refreshTriage, { disabled: t.refreshing })),
    saidLine("triage-refresh"),
    t.researching ? quiet("The research loop is running, so the research buttons wait until it ends.") : null,
    t.approved?.length ? quiet(`Sent to the pipeline on the last run: ${t.approved.join(", ")}.`) : null,
    Object.entries(t.held || {}).map(([ref, why]) => quiet(`Not sent this time: ${ref}: ${why}`)),
    seg("Groups", groups.map((g) => [g.slug, [g.standing, " ", figure(g.rows.length)]]), picked?.slug ?? "",
      (slug) => go(viewHash("triage", { group: slug }), "link", { replace: true })),
    picked ? [heading(groups.indexOf(picked) + 1, picked.standing),
      picked.rows.length
        ? table(count(picked.rows.length, "ticket", "tickets"), [["Ticket", "c-ref"], ["Title"], ["Verdict", "c-verdict wide-only"]],
          picked.rows.map((row) => linkedRow(detailHash("ticket", row.ref),
            cell(rowLink("ticket", row.ref, shortRef(row.ref)), "nb"),
            cell([row.title, row.summary ? el("div", { class: "muted", text: row.summary }) : null]),
            cell([wholeWords(row.verdict), row.prs ? [el("br"), pullLinks(row.prs)] : null]))))
        : quiet("None.")] : quiet("Triage has no groups yet."));
  if (r.detail) drawTicket(groups, r.detail.id, t.researching);
  return t.refreshing || t.researching ? LIVE_MS : SLOW_MS;
}

function drawTicket(groups, ref, researching) {
  const group = groups.find((g) => g.rows.some((row) => row.ref === ref));
  const r = group?.rows.find((row) => row.ref === ref);
  if (!r) {
    fill("triage-detail-head", detailHead("triage-detail", "ticket", ref, ref, [], [null, null]));
    fill("triage-detail-body", saidLine(`ticket:${ref}`), fault(`Triage has no open ticket ${ref}. It may have closed since.`));
    return;
  }
  const list = (items) => el("ol", { class: "questions" }, items.map((item) => el("li", { text: item })));
  const answerable = r.send && r.questions.length;
  fill("triage-detail-head", detailHead("triage-detail", "ticket", ref, r.title || ref, [], neighborsIn(group.rows.map((row) => row.ref), ref)));
  fill("triage-detail-body",
    saidLine(`ticket:${ref}`),
    el("dl", { class: "w" },
      fact("Ticket", link(r.url, ref)), fact("Group", group.standing), fact("Repo", r.repo), fact("Kind", r.kind), fact("Size", r.size),
      fact("Verdict", r.verdict), fact("Pull requests", r.prs ? pullLinks(r.prs) : "")),
    r.summary ? el("p", { text: r.summary }) : null,
    r.questions.length && !answerable ? [subheading("Questions"), list(r.questions)] : null,
    r.send ? [subheading(answerable ? "Answer and send" : "Send"), sendForm(r)] : null,
    r.assumptions.length ? fold(`triage:a:${ref}`, "Show what the build assumes", list(r.assumptions)) : null,
    r.plan.length ? fold(`triage:p:${ref}`, "Show the plan", list(r.plan)) : null,
    r.report ? foldText(`triage:r:${ref}`, "Show the report", r.report) : null,
    researchOffer(r, researching));
}
