"use strict";

const askSecrets = new Map();
const askSaid = new Map();
const askSending = new Set();
const askStopAsked = new Set();
const WAITING_CARDS = { ask: askCard };
let shownWaiting = [];
let askToShow = "";
let waitingCount = -1;
let waitingResultUntil = 0;

function askIdIn(asked) {
  const m = /^ask\/(.+)$/.exec(asked);
  if (!m) return "";
  try {
    return decodeURIComponent(m[1]);
  } catch {
    return "";
  }
}

function openAskLink(asked) {
  const id = askIdIn(asked);
  if (!id) return false;
  askToShow = id;
  if (route) refresh();
  return true;
}

function showAskedCard() {
  if (!askToShow) return;
  const card = document.querySelector(`#waiting [data-ask="${CSS.escape(askToShow)}"]`);
  askToShow = "";
  if (!card) return;
  card.scrollIntoView({ block: "start" });
  card.querySelector("h3")?.focus({ preventScroll: true });
}

function withWaitingCount(title) {
  return waitingCount > 0 ? `(${waitingCount}) ${title}` : title;
}

function setWaitingCount(n) {
  if (n === waitingCount) return;
  waitingCount = n;
  document.title = withWaitingCount(document.title.replace(/^\(\d+\) /, ""));
  const badge = n > 0 ? navigator.setAppBadge?.(n) : navigator.clearAppBadge?.();
  badge?.catch?.(() => {});
}

function askDraftKey(a, i, part) {
  return draftKey(`ask:${a.id}`, `${i}:${part}`);
}

function askDraft(a, i, part) {
  return drafts.get(askDraftKey(a, i, part)) ?? "";
}

function dropAskState(id) {
  for (const key of [...drafts.keys()]) if (key.startsWith(`ask:${id}\n`)) drafts.delete(key);
  for (const key of [...askSecrets.keys()]) if (key.startsWith(`${id}\n`)) askSecrets.delete(key);
  askSaid.delete(id);
  askStopAsked.delete(id);
}

function drawWaiting(asks) {
  shownWaiting = asks;
  const items = asks.map((a) => ({ kind: "ask", ask: a }));
  const open = new Set(asks.map((a) => a.id));
  for (const key of [...drafts.keys()]) if (key.startsWith("ask:") && !open.has(key.slice(4, key.indexOf("\n")))) drafts.delete(key);
  for (const key of [...askSecrets.keys()]) if (!open.has(key.split("\n")[0])) askSecrets.delete(key);
  for (const id of [...askSaid.keys(), ...askStopAsked]) if (!open.has(id)) dropAskState(id);
  document.getElementById("waiting").hidden = !items.length;
  document.getElementById("waiting-count").textContent = items.length ? String(items.length) : "";
  setWaitingCount(items.length);
  fill("waiting-list", items.map((item) => WAITING_CARDS[item.kind](item.ask)));
  if (Date.now() > waitingResultUntil) document.getElementById("waiting-result").hidden = true;
}

function redrawWaiting() {
  drawWaiting(shownWaiting);
}

function secretTyped() {
  return askSecrets.size > 0;
}

function recommendedMark(o, first) {
  return first && !/recommended/i.test(o.label) ? el("span", { class: "tag", text: "Recommended" }) : null;
}

function otherField(a, i) {
  const id = `ask:${a.id}:${i}:other`;
  const input = el("input", { type: "text", id, "data-focus": id, autocomplete: "off", on: { input: (e) => {
    drafts.set(askDraftKey(a, i, "other"), e.target.value);
    if (!e.target.value.trim() || a.questions[i].multiple) return;
    drafts.delete(askDraftKey(a, i, "pick"));
    for (const b of e.target.closest(".ask-q").querySelectorAll(".ask-options button")) b.setAttribute("aria-pressed", "false");
  } } });
  input.value = askDraft(a, i, "other");
  return el("div", { class: "field" }, el("label", { for: id, text: "Other" }), input);
}

function singleOptions(a, q, i) {
  const picked = askDraft(a, i, "pick");
  return el("div", { class: "ask-options" }, q.options.map((o, n) => el("div", { class: "ask-option" },
    el("button", { type: "button", "aria-pressed": String(picked === n + 1), "data-focus": `ask:${a.id}:${i}:pick:${n + 1}`,
      "data-recommended": n === 0 ? "" : null, disabled: askSending.has(a.id), on: { click: () => {
        drafts.set(askDraftKey(a, i, "pick"), n + 1);
        drafts.delete(askDraftKey(a, i, "other"));
        redrawWaiting();
      } } }, o.label),
    el("span", { class: "ask-note" }, recommendedMark(o, n === 0), o.description || ""))));
}

function multipleOptions(a, q, i) {
  const picks = askDraft(a, i, "picks") || [];
  return el("div", { class: "ask-options" }, q.options.map((o, n) => {
    const box = el("input", { type: "checkbox", "data-n": n + 1, "data-focus": `ask:${a.id}:${i}:pick:${n + 1}`, disabled: askSending.has(a.id), on: { change: (e) => {
      const checked = [...e.target.closest(".ask-options").querySelectorAll("input:checked")].map((c) => Number(c.dataset.n));
      drafts.set(askDraftKey(a, i, "picks"), checked);
    } } });
    box.checked = picks.includes(n + 1);
    return el("label", { class: "check" }, box, el("span", {}, o.label, " ", recommendedMark(o, n === 0), o.description ? el("span", { class: "muted", text: ` ${o.description}` }) : null));
  }));
}

function textControl(a, q, i) {
  const id = `ask:${a.id}:${i}:text`;
  const box = el("textarea", { id, "data-focus": id, "aria-label": q.header || q.question, on: {
    input: (e) => drafts.set(askDraftKey(a, i, "text"), e.target.value),
    keydown: submitOnCtrlEnter,
  } });
  box.value = askDraft(a, i, "text");
  return box;
}

function secretControl(a, q, i) {
  const id = `ask:${a.id}:${i}:secret`;
  const key = `${a.id}\n${i}`;
  const declined = Boolean(askDraft(a, i, "declined"));
  const field = el("input", { type: "password", id, "data-focus": id, autocomplete: "off", autocapitalize: "off", spellcheck: "false",
    disabled: declined, on: { input: (e) => {
      if (e.target.value) askSecrets.set(key, e.target.value);
      else askSecrets.delete(key);
    } } });
  field.value = declined ? "" : askSecrets.get(key) ?? "";
  const decline = el("input", { type: "checkbox", "data-focus": `ask:${a.id}:${i}:decline`, on: { change: (e) => {
    if (e.target.checked) {
      drafts.set(askDraftKey(a, i, "declined"), "1");
      askSecrets.delete(key);
    } else drafts.delete(askDraftKey(a, i, "declined"));
    redrawWaiting();
  } } });
  decline.checked = declined;
  return [
    el("div", { class: "field" }, el("label", { for: id, text: `Value of ${q.name}, stored for ${a.repo}` }), field),
    el("p", { class: "muted", text: `The value is stored for ${a.repo} and is not kept in the page.` }),
    el("label", { class: "check" }, decline, "Decline: give no value"),
  ];
}

function doneControl(a, q, i) {
  const did = askDraft(a, i, "did");
  const choose = (value) => () => {
    drafts.set(askDraftKey(a, i, "did"), value);
    redrawWaiting();
  };
  const note = el("input", { type: "text", id: `ask:${a.id}:${i}:note`, "data-focus": `ask:${a.id}:${i}:note`, autocomplete: "off", on: {
    input: (e) => drafts.set(askDraftKey(a, i, "note"), e.target.value),
  } });
  note.value = askDraft(a, i, "note");
  return [
    q.steps?.length ? el("ol", { class: "steps" }, q.steps.map((s) => el("li", { text: s }))) : null,
    el("div", { class: "seg", role: "group", "aria-label": `${q.header || "Step"}: outcome` },
      el("button", { type: "button", "aria-pressed": String(did === "done"), "data-focus": `ask:${a.id}:${i}:done`, disabled: askSending.has(a.id), on: { click: choose("done") } }, "Done"),
      el("button", { type: "button", "aria-pressed": String(did === "cant"), "data-focus": `ask:${a.id}:${i}:cant`, disabled: askSending.has(a.id), on: { click: choose("cant") } }, "Can't do this")),
    el("div", { class: "field" }, el("label", { for: note.id, text: "Note (optional)" }), note),
  ];
}

function questionControl(a, q, i) {
  if (q.kind === "choice") {
    return [q.multiple ? multipleOptions(a, q, i) : singleOptions(a, q, i), otherField(a, i)];
  }
  if (q.kind === "secret") return secretControl(a, q, i);
  if (q.kind === "done") return doneControl(a, q, i);
  return textControl(a, q, i);
}

function askEntry(a, q, i) {
  const other = askDraft(a, i, "other").trim();
  if (q.kind === "choice") {
    if (q.multiple) {
      const picks = askDraft(a, i, "picks") || [];
      if (other) return { text: [...picks.map((n) => q.options[n - 1].label), other].join(", ") };
      return picks.length ? { choice: picks } : null;
    }
    if (other) return { text: other };
    const pick = askDraft(a, i, "pick");
    return pick ? { choice: pick } : null;
  }
  if (q.kind === "secret") {
    if (askDraft(a, i, "declined")) return { declined: true };
    const value = askSecrets.get(`${a.id}\n${i}`);
    return value ? { secret: value } : null;
  }
  if (q.kind === "done") {
    const did = askDraft(a, i, "did");
    const note = askDraft(a, i, "note").trim();
    if (!did) return null;
    if (did === "done") return note ? { text: `Done: ${note}` } : { done: true };
    return note ? { text: `Can't do this: ${note}` } : { declined: true };
  }
  const text = askDraft(a, i, "text");
  return text.trim() ? { text } : null;
}

function askCaution(a, header) {
  askSaid.set(a.id, ["caution", `Answer "${header}" first.`]);
  redrawWaiting();
  focusAskSaid(a.id);
}

function focusAskSaid(id) {
  document.querySelector(`#waiting [data-focus="${CSS.escape(`ask-said:${id}`)}"]`)?.focus();
}

function submitAsk(a, recommended) {
  const answers = [];
  for (const [i, q] of a.questions.entries()) {
    if (recommended && q.kind === "choice") {
      answers.push(null);
      continue;
    }
    const entry = askEntry(a, q, i);
    if (!entry) return askCaution(a, q.header || q.question);
    answers.push(entry);
  }
  return postAsk(a, recommended ? { recommended: true, answers } : { answers });
}

function askResult(r) {
  if (r.state === "waiting" || r.state === "stopped") return r.why || `Run ${r.run} is ${r.state}.`;
  return ["Answered. Run ", itemLink("run", r.run, r.run), " carries on."];
}

async function postAsk(a, body) {
  const typedSecret = [...askSecrets.keys()].some((k) => k.startsWith(`${a.id}\n`));
  askSending.add(a.id);
  askSaid.delete(a.id);
  redrawWaiting();
  try {
    const r = await call(`/api/asks/${encodeURIComponent(a.id)}/answer`, body);
    dropAskState(a.id);
    shownWaiting = shownWaiting.filter((x) => x.id !== a.id);
    say("waiting-result", "ok", askResult(r));
    waitingResultUntil = Date.now() + 60000;
  } catch (e) {
    askSaid.set(a.id, ["fault", `${e.message}${typedSecret ? " The secret field was cleared, so type it again." : ""}`]);
  } finally {
    for (const key of [...askSecrets.keys()]) if (key.startsWith(`${a.id}\n`)) askSecrets.delete(key);
    askSending.delete(a.id);
    askStopAsked.delete(a.id);
    redrawWaiting();
    await refresh();
    focusAskSaid(a.id);
  }
}

function askActions(a) {
  const busy = askSending.has(a.id);
  if (askStopAsked.has(a.id)) {
    return el("div", { class: "actions" },
      el("span", { text: `Stop run ${a.run}? It ends stopped and takes no answer.` }),
      button("Keep waiting", "ask-stop-cancel", () => {
        askStopAsked.delete(a.id);
        redrawWaiting();
      }, { of: a.id, disabled: busy }),
      button("Yes, stop the run", "ask-stop-confirm", () => postAsk(a, { stop: true }), { of: a.id, disabled: busy }));
  }
  return el("div", { class: "actions" },
    el("button", { type: "submit", "data-focus": `ask-submit:${a.id}`, disabled: busy, text: "Submit" }),
    a.questions.some((q) => q.kind === "choice")
      ? button("Go with the recommendations", "ask-recommended", () => submitAsk(a, true), { of: a.id, disabled: busy }) : null,
    button("Stop the run", "ask-stop", () => {
      askStopAsked.add(a.id);
      redrawWaiting();
      document.querySelector(`#waiting [data-focus="${CSS.escape(`ask-stop-cancel:${a.id}`)}"]`)?.focus();
    }, { of: a.id, disabled: busy }));
}

function askCard(a) {
  const said = askSaid.get(a.id);
  return el("form", { class: "ask ask-card", "data-ask": a.id, novalidate: true, "aria-label": `Questions from run ${a.run}`, on: { submit: (e) => {
    e.preventDefault();
    trackAction("ask-submit");
    submitAsk(a, false);
  } } },
    el("h3", { tabindex: "-1", "data-focus": `ask:${a.id}`, text: `${a.repo} ${a.ticket}` }),
    el("dl", { class: "w" },
      fact("Repo", a.repo), fact("Ticket", a.ticket),
      fact("Run", itemLink("run", a.run, a.run, `ask-run:${a.id}`)), fact("Step", a.step),
      fact("Asked", [liveAgo(a.asked), ` · ${clock(a.asked)}`]),
      fact("Why", a.why), fact("Tried", a.tried)),
    a.questions.map((q, i) => el("div", { class: "ask-q" },
      q.header ? el("div", { class: "kicker", text: q.header }) : null,
      el("p", { class: "ask-text", text: q.question }),
      questionControl(a, q, i))),
    el("p", { class: "said", "aria-live": "polite", tabindex: "-1", "data-focus": `ask-said:${a.id}`, hidden: !said }, said ? sayParts(...said) : null),
    askActions(a));
}
