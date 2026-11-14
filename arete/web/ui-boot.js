import { h, render } from "./vendor-preact.js";
import { signal, effect } from "./vendor-signals.js";
import { Shell } from "./ui-shell.js";
import { current, listen } from "./ui-router.js";
import { shell, visit } from "./ui-data.js";
import { startKeys, marks } from "./ui-keys.js";
import { startHooks } from "./ui-hooks.js";

const route = signal(current());
const snapshot = signal({});
const now = signal(Date.now());
const tab = [...crypto.getRandomValues(new Uint8Array(8))].map((byte) => byte.toString(16).padStart(2, "0")).join("");
const phone = matchMedia("(pointer: coarse) and (max-width: 1040px)");
let stint = null;
let via = "hash";

function start(source) {
  if (!document.hidden && !stint) stint = { view: route.value.screen, via: source, at: performance.now() };
}

function finish() {
  if (!stint) return;
  const outgoing = stint;
  stint = null;
  visit({ tab, device: phone.matches ? "phone" : "desktop", view: outgoing.view, via: outgoing.via,
    ms: Math.max(0, Math.min(86400000, Math.round(performance.now() - outgoing.at))) });
}

effect(() => {
  const selected = route.value;
  render(h(Shell, { route: selected, snapshot: snapshot.value, now: now.value }), document.getElementById("app"));
  marks();
  document.title = `arete · ${selected.screen === "board" && selected.active === "my-work" ? "My work" : selected.screen}`;
});

listen((selected) => {
  if (selected.hash === route.value.hash) return;
  finish();
  route.value = selected;
  start(via);
  via = "hash";
});

document.addEventListener("click", (event) => {
  const link = event.target.closest?.(".side-nav a");
  if (link && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && event.button === 0
    && link.hash !== location.hash) via = "sidebar";
});
document.addEventListener("visibilitychange", () => document.hidden ? finish() : start("return"));
window.addEventListener("pagehide", finish);
window.addEventListener("pageshow", () => start("return"));

async function refresh() {
  snapshot.value = await shell();
  setTimeout(refresh, 10000);
}

setInterval(() => { now.value = Date.now(); }, 1000);
start("open");
refresh();
startKeys();
startHooks();
