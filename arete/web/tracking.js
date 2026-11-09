"use strict";

const DAY_MS = 24 * 60 * 60 * 1000;
let stint = null;
const TAB = CARRIED?.tab ?? [...crypto.getRandomValues(new Uint8Array(8))].map((b) => b.toString(16).padStart(2, "0")).join("");

function deviceKind() {
  return PHONE.matches ? "phone" : "desktop";
}

function stintName() {
  return route.detail?.kind ?? route.view;
}

function report(row) {
  fetch("/api/usage", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json", "X-Arete": "1" },
    body: JSON.stringify({ tab: TAB, device: deviceKind(), ...row }) }).catch(() => {});
}

function startStint(via) {
  stint = { name: stintName(), via, at: performance.now() };
}

function endStint() {
  if (!stint) return;
  report({ kind: "visit", view: stint.name, via: stint.via, ms: Math.min(DAY_MS, Math.round(performance.now() - stint.at)) });
  stint = null;
}

function trackAction(name) {
  if (route) report({ kind: "action", name, view: stintName() });
}
