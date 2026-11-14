"use strict";

const PAGE_CODE = document.querySelector('meta[name="arete-code"]').content;
const HANDOFF = "arete.handoff";
const NOTHING_CARRIED = { scroll: 0, paneScroll: 0, boxes: [], consoleScroll: null, focus: "", console: false, forms: [], fields: [],
  folds: [], answers: [], messages: [], said: [], sendSaid: [], results: [], queries: {} };
const CARRIED = takeHandoff();
let servedCode = PAGE_CODE;
let placeToPutBack = null;
let consoleScrollToPutBack = null;

function takeHandoff() {
  try {
    const carried = JSON.parse(sessionStorage.getItem(HANDOFF));
    sessionStorage.removeItem(HANDOFF);
    return carried && { ...NOTHING_CARRIED, ...carried };
  } catch {
    return null;
  }
}

function noteServedCode(response) {
  servedCode = response.headers.get("X-Arete-Code") || servedCode;
}

function newCodeServed() {
  return servedCode !== PAGE_CODE;
}

function newCodeWaitsFor() {
  if (changesInFlight) return "your last action ends";
  if (document.querySelector("dialog[open]")) return "you close the dialog";
  if (feedbackUnsaved()) return "your feedback draft is saved";
  if (settingsData && Object.keys(settingsChanges()).length) return "you save or undo the settings";
  if (secretTyped()) return "you send or clear the secret you typed";
  if (unsentText()) return "you send or clear what you typed";
  if (!getSelection().isCollapsed) return "you clear the selection";
  return "";
}

function textFields(within = document) {
  return [...within.querySelectorAll("textarea, input[type=text]")];
}

function unsentText() {
  return textFields().some((field) => field.value.trim() && field.value !== field.dataset.sent && !field.closest("#settings-form, dialog"));
}

function markSent(formId) {
  for (const field of textFields(document.getElementById(formId))) field.dataset.sent = field.value;
}

function newCodeNote() {
  const waitsFor = newCodeServed() ? newCodeWaitsFor() : "";
  return waitsFor ? `The new version loads once ${waitsFor}.` : "";
}

function reloadIfNewCode() {
  if (!newCodeServed() || newCodeWaitsFor()) return false;
  try {
    sessionStorage.setItem(HANDOFF, JSON.stringify(placeNow()));
  } catch {
    // with no storage the new version still loads, on the same view but at its top
  }
  location.reload();
  return true;
}

function placeNow() {
  return {
    tab: TAB,
    scroll: scrollY,
    paneScroll: route.detail ? currentPane().scrollTop : 0,
    boxes: [...scrollersByPlace(currentSection())].map(([key, node]) => ({ key, ...boxScroll(node) })),
    consoleScroll: boxScroll(document.getElementById("console-body")),
    focus: focusSelector(document.activeElement),
    console: !document.getElementById("console").hidden,
    forms: TOGGLE_FORMS.map(({ form }) => form).filter((form) => !document.getElementById(form).hidden),
    fields: TOGGLE_FORMS.flatMap(({ form }) => textFields(document.getElementById(form)))
      .filter((field) => field.value).map(({ id, value, dataset }) => ({ id, value, sent: dataset.sent })),
    folds: [...openQuestions],
    answers: [...drafts],
    messages: [...talkDrafts],
    said: saidAsText(lastSaid),
    sendSaid: saidAsText(sendSaid),
    results: saidAsText(saidById).filter(([id]) => !document.getElementById(id).hidden),
    queries: lastQuery,
  };
}

function boxScroll(node) {
  return { left: node.scrollLeft, top: node.scrollTop, atEnd: node.scrollTop + node.clientHeight >= node.scrollHeight - 4 };
}

function scrollBoxBack(node, { left, top, atEnd }) {
  node.scrollTo(left, atEnd ? node.scrollHeight : top);
}

function focusSelector(node) {
  if (node?.dataset?.focus) return `[data-focus="${CSS.escape(node.dataset.focus)}"]`;
  if (node?.id) return `#${CSS.escape(node.id)}`;
  if (node?.matches("nav.side a[data-view]")) return `nav.side a[data-view="${CSS.escape(node.dataset.view)}"]`;
  return "";
}

function saidAsText(said) {
  return [...said].filter(([, [, sentence]]) => typeof sentence === "string");
}

function resumeCarried() {
  if (!CARRIED) return;
  Object.assign(lastQuery, CARRIED.queries);
  for (const key of CARRIED.folds) openQuestions.add(key);
  for (const [key, text] of CARRIED.answers) drafts.set(key, text);
  for (const [id, text] of CARRIED.messages) talkDrafts.set(id, text);
  for (const [place, said] of CARRIED.said) lastSaid.set(place, said);
  for (const [ref, said] of CARRIED.sendSaid) sendSaid.set(ref, said);
  for (const [id, [state, sentence]] of CARRIED.results) if (document.getElementById(id)) say(id, state, sentence);
  for (const { toggle, form } of TOGGLE_FORMS) if (CARRIED.forms.includes(form)) showForm(toggle, form, true);
  for (const { id, value, sent } of CARRIED.fields) putFieldBack(document.getElementById(id), value, sent);
  consoleScrollToPutBack = CARRIED.consoleScroll;
  placeToPutBack = CARRIED;
}

function putFieldBack(field, value, sent) {
  if (!field) return;
  field.value = value;
  if (sent !== undefined) field.dataset.sent = sent;
}

function putBackPlace() {
  if (!placeToPutBack) return;
  const { scroll, paneScroll, boxes, focus } = placeToPutBack;
  placeToPutBack = null;
  if (focus) firstShown(focus)?.focus({ preventScroll: true });
  scrollTo(0, scroll);
  if (route.detail) currentPane().scrollTop = paneScroll;
  const rebuilt = scrollersByPlace(currentSection());
  for (const { key, ...scrolled } of boxes) if (rebuilt.has(key)) scrollBoxBack(rebuilt.get(key), scrolled);
}

function putBackConsoleScroll(body) {
  if (!consoleScrollToPutBack) return;
  scrollBoxBack(body, consoleScrollToPutBack);
  consoleScrollToPutBack = null;
}
