"use strict";

function wireForms() {
  document.getElementById("settings-form").addEventListener("submit", saveSettings);
  document.getElementById("settings-undo").addEventListener("click", () => {
    if (settingsData) drawSettings(settingsData);
    document.getElementById("settings-result").hidden = true;
    refresh();
  });
  for (const { toggle: toggleId, form, focus } of TOGGLE_FORMS) {
    const toggle = document.getElementById(toggleId);
    toggle.dataset.label = toggle.textContent;
    toggle.addEventListener("click", () => {
      trackAction(toggleId);
      toggleForm(toggleId, form, focus);
    });
  }
  document.getElementById("launch").addEventListener("submit", launch);
  document.getElementById("loop-new").addEventListener("submit", draftLoop);
  document.getElementById("launch-device").addEventListener("change", repoFollowsDevice);
  document.getElementById("launch-reset").addEventListener("click", async () => {
    document.getElementById("launch").reset();
    document.getElementById("launch-result").hidden = true;
    try {
      fillChoices((await call("/api/sessions")).choices, true);
    } catch (e) {
      say("launch-result", "fault", e.message);
    }
  });
  for (const id of ["loop-new-sentence", "chat-text", "launch-prompt"]) document.getElementById(id).addEventListener("keydown", submitOnCtrlEnter);
}

function wireConsole() {
  document.getElementById("console-open").addEventListener("click", () => {
    const open = document.getElementById("console").hidden;
    setConsole(open, { focus: open });
  });
  document.getElementById("console-close").addEventListener("click", () => setConsole(false));
  document.getElementById("chat").addEventListener("submit", sendChat);
  document.getElementById("chat-new-form").addEventListener("submit", newChat);
  document.getElementById("chat-pick").addEventListener("change", (event) => {
    switchChat(Number(event.currentTarget.value));
    pollSoon();
  });
  for (const [id, action] of [["draft-file", "file"], ["draft-drop", "drop"]]) {
    document.getElementById(id).addEventListener("click", (event) => draftAction(action, event.currentTarget));
  }
}

function closeOnBackdrop(dialog) {
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  });
}

function wireDialogs() {
  const palette = document.getElementById("palette");
  const input = document.getElementById("palette-input");
  document.getElementById("find-open").addEventListener("click", openPalette);
  input.addEventListener("input", () => {
    paletteActive = 0;
    drawPalette();
  });
  input.addEventListener("keydown", paletteKeys);
  const list = document.getElementById("palette-list");
  list.addEventListener("mousedown", (event) => event.preventDefault());
  list.addEventListener("click", (event) => {
    const option = event.target.closest("[role=option]");
    if (option) choosePalette(Number(option.dataset.index));
  });
  palette.addEventListener("close", () => {
    refocus(paletteReturn);
    paletteReturn = null;
  });
  document.getElementById("palette-close").addEventListener("click", () => palette.close());
  closeOnBackdrop(palette);
  const keys = document.getElementById("keys");
  document.getElementById("keys-close").addEventListener("click", () => keys.close());
  closeOnBackdrop(keys);
  keys.addEventListener("close", () => refocus(keysReturn));
}

function makeNav() {
  const groups = [...new Set(Object.values(VIEWS).map((meta) => meta.group))];
  document.querySelector("nav.side").prepend(...groups.map((group) => {
    const id = `group-${group.toLowerCase()}`;
    return el("div", { class: "group" },
      el("div", { class: "group-name kicker", id, text: group }),
      el("ul", { class: "links", "aria-labelledby": id },
        Object.entries(VIEWS).filter(([, meta]) => meta.group === group).map(([view, meta]) => el("li", {},
          el("a", { class: "btn", href: `#${view}`, "data-view": view }, el("span", { text: meta.label }), el("span", { class: "count", id: `count-${view}` }))))));
  }));
  document.getElementById("keys-console").before(...Object.values(VIEWS).flatMap((meta) => [
    el("dt", {}, el("kbd", { text: "G" }), " then ", el("kbd", { text: meta.key.toUpperCase() })), el("dd", { text: meta.label })]));
}

function makePanes() {
  for (const pane of document.querySelectorAll(".detail")) {
    pane.setAttribute("aria-labelledby", `${pane.id}-title`);
    pane.setAttribute("role", "region");
    pane.append(el("div", { class: "detail-head", id: `${pane.id}-head` }), el("div", { class: "detail-body", id: `${pane.id}-body` }), el("div", { id: `${pane.id}-talk` }));
  }
}

let resizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => route?.view === "outcomes" && drawOutcomes(), 150);
});
window.addEventListener("popstate", onHistory);
window.addEventListener("hashchange", onHistory);
window.addEventListener("pagehide", endStint);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    endStint();
    return;
  }
  startStint("return");
  refresh();
  pollChat();
});

document.addEventListener("DOMContentLoaded", () => {
  history.scrollRestoration = "manual";
  makeNav();
  makePanes();
  wireForms();
  wireConsole();
  wireDialogs();
  document.addEventListener("keydown", onKey);
  document.addEventListener("click", onClick);
  const noteMouseAt = (e) => {
    pointerKnown = true;
    mouseAt = e.pointerType === "mouse" ? { x: e.clientX, y: e.clientY } : null;
  };
  for (const event of ["pointermove", "pointerdown", "pointerover"]) document.addEventListener(event, noteMouseAt, { passive: true });
  document.documentElement.addEventListener("pointerleave", () => {
    pointerKnown = true;
    mouseAt = null;
  });
  document.getElementById("skip").addEventListener("click", () => document.getElementById("main").focus());
  const asked = location.hash.replace(/^#/, "");
  const first = knownRoute(location.hash);
  history.replaceState({ from: history.state?.from ?? "" }, "", first.hash);
  if (asked === "chat") setConsole(true, { focus: true, track: false });
  else if (CARRIED) setConsole(CARRIED.console, { track: false });
  else {
    try {
      if (!PHONE.matches && localStorage.getItem("arete.console") === "open") setConsole(true, { track: false });
    } catch {
      // without storage the console starts closed
    }
  }
  resumeCarried();
  applyRoute(first, CARRIED ? "return" : "start");
});
