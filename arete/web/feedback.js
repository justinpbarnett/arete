"use strict";

const FEEDBACK_KEPT = "arete.feedback";
const FEEDBACK_SAVE_MS = 600;
const FEEDBACK_RETRY_MS = 5000;
const FEEDBACK_TEXTS = ["text", "expected", "actual", "steps"];
const FEEDBACK_EMPTY = { kind: "idea", text: "", expected: "", actual: "", steps: "" };
let feedbackReturn = null;
let feedbackTimer = 0;
let feedbackDirty = false;
let feedbackSaving = false;
const FEEDBACK_READ_MS = 8000;
const FEEDBACK_ON_DEVICE = "The draft is kept on this device, and it saves again when the server answers.";
let feedbackRead = false;
let feedbackDiscarding = false;
let feedbackSaveWaits = null;
let feedbackBase = { ...FEEDBACK_EMPTY };
let feedbackLine = Promise.resolve();

function inFeedbackTurn(work) {
  const turn = feedbackLine.then(work);
  feedbackLine = turn.catch(() => {});
  return turn;
}

function feedbackBox(name) {
  return document.getElementById(`feedback-${name}`);
}

function feedbackFields() {
  return {
    kind: document.querySelector("input[name=feedback-kind]:checked")?.value ?? "idea",
    ...Object.fromEntries(FEEDBACK_TEXTS.map((name) => [name, feedbackBox(name).value])),
  };
}

function putFeedback(fields) {
  const kind = fields.kind || "idea";
  for (const radio of document.querySelectorAll("input[name=feedback-kind]")) radio.checked = radio.value === kind;
  for (const name of FEEDBACK_TEXTS) {
    const value = fields[name] ?? "";
    if (feedbackBox(name).value !== value) feedbackBox(name).value = value;
  }
  feedbackBox("bug").hidden = kind !== "bug";
}

function keepFeedbackHere(unsaved) {
  try {
    localStorage.setItem(FEEDBACK_KEPT, JSON.stringify({ ...feedbackFields(), unsaved }));
  } catch {
    // without storage the draft is kept on arete only
  }
}

function keptFeedback() {
  try {
    return JSON.parse(localStorage.getItem(FEEDBACK_KEPT));
  } catch {
    return null;
  }
}

function forgetFeedbackHere() {
  try {
    localStorage.removeItem(FEEDBACK_KEPT);
  } catch {
    // nothing was kept
  }
}

function feedbackUnsaved() {
  return feedbackDirty || feedbackSaving;
}

function sayFeedback(state, sentence) {
  sayIn(feedbackBox("result"), state, sentence);
}

function failedFeedback(e) {
  return e instanceof ErrorAnswer ? withPeriod(e.message) : "arete did not answer.";
}

function onFeedbackInput() {
  feedbackDirty = true;
  keepFeedbackHere(true);
  feedbackBox("bug").hidden = feedbackFields().kind !== "bug";
  clearTimeout(feedbackTimer);
  feedbackTimer = 0;
  saveFeedbackLater(FEEDBACK_SAVE_MS);
}

function saveFeedbackLater(ms) {
  if (!feedbackTimer) feedbackTimer = setTimeout(saveFeedback, ms);
}

function feedbackAnswer() {
  const stop = new AbortController();
  let timer = 0;
  const late = new Promise((_, refuse) => {
    timer = setTimeout(() => {
      stop.abort();
      refuse(new NoAnswer("arete did not answer in time."));
    }, FEEDBACK_READ_MS);
  });
  return Promise.race([call("/api/capture", undefined, stop.signal), late]).finally(() => clearTimeout(timer));
}

function mergedFeedbackText(typed, base, held) {
  if (typed === base) return held;
  if (held === base || !held || typed === held) return typed;
  return typed ? `${held}\n\n${typed}` : held;
}

function mergeFeedback(draft) {
  const held = Object.fromEntries(Object.keys(FEEDBACK_EMPTY).map((name) => [name, draft?.[name] || FEEDBACK_EMPTY[name]]));
  const typed = feedbackFields();
  const merged = { kind: typed.kind === feedbackBase.kind ? held.kind : typed.kind };
  for (const name of FEEDBACK_TEXTS) merged[name] = mergedFeedbackText(typed[name], feedbackBase[name], held[name]);
  putFeedback(merged);
  feedbackBase = held;
  feedbackRead = true;
  feedbackDirty = JSON.stringify(merged) !== JSON.stringify(held);
  keepFeedbackHere(feedbackDirty);
  drawFeedbackContext(draft);
}

async function readFeedbackDraft() {
  try {
    const answered = await feedbackAnswer();
    for (const name of FEEDBACK_TEXTS) {
      feedbackBox(name).maxLength = answered.limits[name];
      const line = feedbackBox(`${name}-limit`);
      line.textContent = `${name === "text" ? "Required" : "Optional"} · up to ${answered.limits[name].toLocaleString("en-US")} characters`;
    }
    if (feedbackDiscarding) return;
    mergeFeedback(answered.draft);
    feedbackBox("result").hidden = true;
  } catch (e) {
    sayFeedback("caution", `${failedFeedback(e)} ${feedbackDirty ? FEEDBACK_ON_DEVICE : "This shows the copy kept on this device."}`);
  }
}

function readFeedback() {
  return inFeedbackTurn(async () => {
    await readFeedbackDraft();
    if (feedbackDirty) saveFeedbackLater(feedbackRead ? FEEDBACK_SAVE_MS : FEEDBACK_RETRY_MS);
  });
}

async function sendFeedbackFields() {
  if (!feedbackDirty || feedbackDiscarding) return;
  if (!feedbackRead) {
    await readFeedbackDraft();
    if (!feedbackRead) return saveFeedbackLater(FEEDBACK_RETRY_MS);
    if (!feedbackDirty) return;
  }
  const sent = feedbackFields();
  let saved = false;
  feedbackSaving = true;
  try {
    const answered = await call("/api/capture", sent);
    saved = true;
    feedbackBase = sent;
    if (JSON.stringify(feedbackFields()) === JSON.stringify(sent)) {
      feedbackDirty = false;
      keepFeedbackHere(false);
    }
    drawFeedbackContext(answered.draft);
    sayFeedback("ok", `Draft saved at ${clock(new Date())}. It is kept, and nothing is filed.`);
  } catch (e) {
    sayFeedback("caution", `${failedFeedback(e)} ${FEEDBACK_ON_DEVICE}`);
  } finally {
    feedbackSaving = false;
    if (feedbackDirty) saveFeedbackLater(saved ? FEEDBACK_SAVE_MS : FEEDBACK_RETRY_MS);
  }
}

function saveFeedback() {
  clearTimeout(feedbackTimer);
  feedbackTimer = 0;
  feedbackSaveWaits ||= inFeedbackTurn(() => {
    feedbackSaveWaits = null;
    return sendFeedbackFields();
  });
  return feedbackSaveWaits;
}

function drawFeedbackContext(draft) {
  const json = draft?.context || "";
  feedbackBox("include").checked = Boolean(json);
  feedbackBox("context").hidden = !json;
  feedbackBox("context-json").textContent = json;
}

async function captureFeedbackContext() {
  clearTimeout(feedbackTimer);
  feedbackTimer = 0;
  await inFeedbackTurn(async () => {
    await sendFeedbackFields();
    try {
      const answered = await call("/api/capture/context", { view: route.view, detail: route.detail });
      drawFeedbackContext(answered.draft);
      sayFeedback("ok", `Captured the app state at ${clock(new Date())}.`);
    } catch (e) {
      if (feedbackBox("context").hidden) feedbackBox("include").checked = false;
      sayFeedback("caution", `The app state was not added: ${failedFeedback(e)}`);
    }
  });
}

async function dropFeedbackContext() {
  await inFeedbackTurn(async () => {
    try {
      drawFeedbackContext((await call("/api/capture/context/drop", {})).draft);
      sayFeedback("ok", "Removed the app state.");
    } catch (e) {
      feedbackBox("include").checked = !feedbackBox("context").hidden;
      sayFeedback("caution", `The app state was not removed: ${failedFeedback(e)}`);
    }
  });
  feedbackBox("include").focus();
}

async function discardFeedback() {
  if (feedbackDiscarding) return;
  feedbackDiscarding = true;
  clearTimeout(feedbackTimer);
  feedbackTimer = 0;
  const clicked = feedbackFields();
  await inFeedbackTurn(async () => {
    let failed = null;
    try {
      await call("/api/capture/discard", {});
    } catch (e) {
      failed = e;
    }
    feedbackDiscarding = false;
    if (failed) {
      sayFeedback("caution", `The draft was not discarded: ${failedFeedback(failed)} It is still kept.`);
      if (feedbackDirty) saveFeedbackLater(FEEDBACK_RETRY_MS);
      return;
    }
    const typed = feedbackFields();
    const since = Object.fromEntries(Object.keys(FEEDBACK_EMPTY).map((name) => [name, typed[name] === clicked[name] ? FEEDBACK_EMPTY[name] : typed[name]]));
    putFeedback(since);
    feedbackBase = { ...FEEDBACK_EMPTY };
    feedbackRead = true;
    drawFeedbackContext(null);
    feedbackDirty = JSON.stringify(since) !== JSON.stringify(FEEDBACK_EMPTY);
    if (!feedbackDirty) {
      forgetFeedbackHere();
      sayFeedback("ok", "Discarded the draft.");
      return;
    }
    keepFeedbackHere(true);
    saveFeedbackLater(FEEDBACK_SAVE_MS);
    sayFeedback("ok", "Discarded the draft. What you typed since is kept on this device and saves as a new draft.");
  });
}

function openFeedback() {
  const dialog = document.getElementById("feedback");
  if (dialog.open) return;
  trackAction("feedback-open");
  feedbackReturn = focusMark(document.activeElement);
  dialog.showModal();
  (COARSE.matches ? document.getElementById("feedback-title") : feedbackBox("text")).focus();
  if (!feedbackDirty) readFeedback();
}

function wireFeedback() {
  const dialog = document.getElementById("feedback");
  document.getElementById("feedback-open").addEventListener("click", openFeedback);
  document.getElementById("feedback-close").addEventListener("click", () => dialog.close());
  for (const name of FEEDBACK_TEXTS) feedbackBox(name).addEventListener("input", onFeedbackInput);
  for (const radio of document.querySelectorAll("input[name=feedback-kind]")) radio.addEventListener("change", onFeedbackInput);
  feedbackBox("include").addEventListener("change", (event) => (event.currentTarget.checked ? captureFeedbackContext() : dropFeedbackContext()));
  feedbackBox("context-again").addEventListener("click", captureFeedbackContext);
  feedbackBox("context-drop").addEventListener("click", dropFeedbackContext);
  document.getElementById("feedback-discard").addEventListener("click", discardFeedback);
  dialog.addEventListener("close", () => {
    if (feedbackDirty) saveFeedback();
    refocus(feedbackReturn);
    feedbackReturn = null;
  });
  const kept = keptFeedback();
  if (kept) putFeedback(kept);
  if (kept?.unsaved) {
    feedbackDirty = true;
    feedbackRead = true;
    saveFeedback();
  } else {
    feedbackBase = feedbackFields();
    readFeedback();
  }
}
