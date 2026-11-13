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
let feedbackReading = 0;
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
  for (const name of FEEDBACK_TEXTS) feedbackBox(name).value = fields[name] ?? "";
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
  feedbackTimer = setTimeout(saveFeedback, FEEDBACK_SAVE_MS);
}

async function sendFeedbackFields() {
  if (!feedbackDirty) return;
  const sent = feedbackFields();
  let saved = false;
  feedbackSaving = true;
  try {
    const answered = await call("/api/capture", sent);
    saved = true;
    if (JSON.stringify(feedbackFields()) === JSON.stringify(sent)) {
      feedbackDirty = false;
      keepFeedbackHere(false);
    }
    drawFeedbackContext(answered.draft);
    sayFeedback("ok", `Draft saved at ${clock(new Date())}. It is kept, and nothing is filed.`);
  } catch (e) {
    sayFeedback("caution", `${failedFeedback(e)} The draft is kept on this device, and it saves again when the server answers.`);
  } finally {
    feedbackSaving = false;
    if (feedbackDirty && !feedbackTimer) feedbackTimer = setTimeout(saveFeedback, saved ? FEEDBACK_SAVE_MS : FEEDBACK_RETRY_MS);
  }
}

function saveFeedback() {
  clearTimeout(feedbackTimer);
  feedbackTimer = 0;
  return inFeedbackTurn(sendFeedbackFields);
}

function lockFeedback(locked) {
  feedbackReading += locked ? 1 : -1;
  const reading = feedbackReading > 0;
  for (const name of FEEDBACK_TEXTS) feedbackBox(name).readOnly = reading;
  for (const radio of document.querySelectorAll("input[name=feedback-kind]")) radio.disabled = reading;
}

function readFeedback() {
  const lock = !feedbackDirty;
  if (lock) lockFeedback(true);
  return inFeedbackTurn(async () => {
    try {
      const answered = await call("/api/capture");
      for (const name of FEEDBACK_TEXTS) {
        feedbackBox(name).maxLength = answered.limits[name];
        const line = feedbackBox(`${name}-limit`);
        line.textContent = `${name === "text" ? "Required" : "Optional"} · up to ${answered.limits[name].toLocaleString("en-US")} characters`;
      }
      if (feedbackDirty) return;
      putFeedback(answered.draft ?? FEEDBACK_EMPTY);
      keepFeedbackHere(false);
      drawFeedbackContext(answered.draft);
    } catch {
      // the copy kept on this device stays showing
    } finally {
      if (lock) lockFeedback(false);
    }
  });
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
  clearTimeout(feedbackTimer);
  feedbackTimer = 0;
  const before = JSON.stringify(feedbackFields());
  await inFeedbackTurn(async () => {
    try {
      await call("/api/capture/discard", {});
    } catch (e) {
      sayFeedback("caution", `The draft was not discarded: ${failedFeedback(e)} It is still kept.`);
      if (feedbackDirty) feedbackTimer = setTimeout(saveFeedback, FEEDBACK_RETRY_MS);
      return;
    }
    drawFeedbackContext(null);
    if (JSON.stringify(feedbackFields()) !== before) {
      sayFeedback("ok", "Discarded the draft. What you typed since is kept as a new draft.");
      return;
    }
    putFeedback(FEEDBACK_EMPTY);
    feedbackDirty = false;
    forgetFeedbackHere();
    sayFeedback("ok", "Discarded the draft.");
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
    saveFeedback();
  } else {
    readFeedback();
  }
}
