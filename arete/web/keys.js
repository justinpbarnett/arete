"use strict";

const GO_KEYS = Object.fromEntries(Object.entries(VIEWS).map(([view, meta]) => [meta.key, view]));
const GO_WAIT_MS = 1500;
let keysReturn = null;
let goPending = 0;

function inField(node) {
  return Boolean(node?.closest?.("input, textarea, select, [contenteditable]"));
}

function openKeys() {
  keysReturn = focusMark(document.activeElement);
  document.getElementById("keys").showModal();
  document.getElementById("keys-close").focus();
}

function onEscape(event) {
  if (document.querySelector("dialog[open]")) return;
  const panel = document.getElementById("console");
  if (!panel.hidden && panel.contains(document.activeElement)) {
    event.preventDefault();
    setConsole(false);
  } else if (route.detail && !inField(document.activeElement)) {
    event.preventDefault();
    closeDetail();
  } else if (!panel.hidden) {
    event.preventDefault();
    setConsole(false);
  }
}

function onKey(event) {
  if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if (!document.getElementById("keys").open) openPalette();
    return;
  }
  if (event.key === "Escape") return onEscape(event);
  if (event.metaKey || event.ctrlKey || event.altKey || event.defaultPrevented) return;
  if (inField(event.target) || document.querySelector("dialog[open]")) return;
  if (goPending) {
    clearTimeout(goPending);
    goPending = 0;
    const view = GO_KEYS[event.key.toLowerCase()];
    if (!view) return;
    event.preventDefault();
    trackAction("go-key");
    focusAfterDraw = "main";
    go(listHash(view), "key");
    return;
  }
  const keys = {
    "/": openPalette,
    g: () => { goPending = setTimeout(() => { goPending = 0; }, GO_WAIT_MS); },
    c: () => {
      const open = document.getElementById("console").hidden;
      setConsole(open, { focus: open });
    },
    "?": openKeys,
  };
  const run = keys[event.key];
  if (!run) return;
  event.preventDefault();
  run();
}

function onClick(event) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const anchor = event.target.closest("a[href^='#']");
  if (anchor) {
    event.preventDefault();
    go(anchor.getAttribute("href"), anchor.closest("nav.side") ? "sidebar" : "link");
    return;
  }
  const row = event.target.closest("tr[data-href]");
  if (!row || event.target.closest("a, button, input, select, textarea, summary, label, details") || !getSelection().isCollapsed) return;
  go(row.dataset.href, "link");
}
