"use strict";

const PAGE_CODE = document.querySelector('meta[name="arete-code"]').content;
const HANDOFF = "arete.handoff.1";
const NOTHING_CARRIED = { scroll: 0, paneScroll: 0, boxes: [], focus: "", focusId: "", console: false, forms: [], folds: [],
  answers: [], messages: [], said: [], sendSaid: [] };
const CARRIED = takeHandoff();
let servedCode = PAGE_CODE;
let changesInFlight = 0;
let placeToPutBack = null;

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

function newCodeWaitsFor() {
  if (changesInFlight) return "your last action ends";
  if (document.querySelector("dialog[open]")) return "you close the dialog";
  if (settingsData && Object.keys(settingsChanges()).length) return "you save or undo the settings";
  if (unsentText()) return "you send or clear what you typed";
  if (!getSelection().isCollapsed) return "you clear the selection";
  return "";
}

function unsentText() {
  const typed = (field) => field.value.trim() && !field.closest("#settings, dialog");
  const messages = [...document.querySelectorAll("textarea")];
  // a send clears its message but keeps the form's other fields for the next one
  const otherFieldsOnScreen = [...document.querySelectorAll("input[type=text]")].filter((field) => field.checkVisibility());
  return [...messages, ...otherFieldsOnScreen].some(typed);
}

function newCodeNote() {
  const waitsFor = servedCode === PAGE_CODE ? "" : newCodeWaitsFor();
  return waitsFor ? `The new version loads once ${waitsFor}.` : "";
}

function reloadsForNewCode() {
  if (servedCode === PAGE_CODE || newCodeWaitsFor()) return false;
  try {
    sessionStorage.setItem(HANDOFF, JSON.stringify(placeNow()));
  } catch {
    // with no storage the new version still loads, on the same view but at its top
  }
  location.reload();
  return true;
}

function placeNow() {
  const focused = document.activeElement;
  return {
    tab: TAB,
    scroll: scrollY,
    paneScroll: route.detail ? currentPane().scrollTop : 0,
    boxes: scrolledIn(currentSection()),
    focus: focused?.dataset?.focus || "",
    focusId: focused?.id || "",
    console: !document.getElementById("console").hidden,
    forms: TOGGLE_FORMS.map(([, formId]) => formId).filter((formId) => !document.getElementById(formId).hidden),
    folds: [...openQuestions],
    answers: [...drafts],
    messages: [...talkDrafts],
    said: sentencesIn(lastSaid),
    sendSaid: sentencesIn(sendSaid),
  };
}

function sentencesIn(said) {
  return [...said].filter(([, [, sentence]]) => typeof sentence === "string");
}

function resumeCarried() {
  if (!CARRIED) return;
  for (const key of CARRIED.folds) openQuestions.add(key);
  for (const [key, text] of CARRIED.answers) drafts.set(key, text);
  for (const [id, text] of CARRIED.messages) talkDrafts.set(id, text);
  for (const [place, said] of CARRIED.said) lastSaid.set(place, said);
  for (const [ref, said] of CARRIED.sendSaid) sendSaid.set(ref, said);
  for (const [toggleId, formId] of TOGGLE_FORMS) if (CARRIED.forms.includes(formId)) showForm(toggleId, formId, true);
  placeToPutBack = CARRIED;
}

function putBackPlace() {
  if (!placeToPutBack) return;
  const { scroll, paneScroll, boxes, focus, focusId } = placeToPutBack;
  placeToPutBack = null;
  const focused = focus ? [...document.querySelectorAll(`[data-focus="${CSS.escape(focus)}"]`)].find((node) => node.checkVisibility())
    : focusId ? document.getElementById(focusId) : null;
  focused?.focus({ preventScroll: true });
  scrollTo(0, scroll);
  if (route.detail) currentPane().scrollTop = paneScroll;
  scrollBack(currentSection(), boxes);
}
