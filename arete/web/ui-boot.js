import { h, render } from "./vendor-preact.js";
import { signal, effect } from "./vendor-signals.js";
import { Shell, applyRuns } from "./ui-shell.js";
import { current, listen } from "./ui-router.js";
import { visit } from "./ui-data.js";
import { live, connectivity, readFault, runView } from "./ui-live.js";
import { applyQueue } from "./ui-run.js";
import { startKeys, marks, screenKeys, pending, sheet, mode, connection, readRetry } from "./ui-keys.js";
import { configurePalette, globalCommands } from "./ui-palette.js";
import { actions, dispatch } from "./ui-actions.js";
import { dismissMenu } from "./ui-menu.js";
import { startHooks } from "./ui-hooks.js";
import { feedbackCommand, startFeedback } from "./ui-feedback.js";
import { normalizePrimary } from "./ui-list.js";

const route = signal(current());
let feed = live(route.value.screen, route.value.path[0]);
const snapshot = feed.snapshot;
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

const retry = () => feed.retry();

effect(() => applyRuns(snapshot.value.runs));
effect(() => applyQueue(runView.value));

effect(() => {
  const selected = route.value;
  const latest = snapshot.value;
  const runs = runView.value;
  const runFault = selected.screen === "run" && (runs.queueError || runs.detailError);
  connection.value = connectivity(latest, now.value);
  readRetry.value = readFault(selected.screen, latest) || runFault ? retry : null;
  render(h(Shell, { route: selected, snapshot: latest, now: now.value, retry, runs }), document.getElementById("app"));
  marks();
  normalizePrimary();
  document.title = `arete · ${selected.screen === "board" && selected.active === "my-work" ? "My work" : selected.screen}`;
});

listen((selected) => {
  if (selected.hash === route.value.hash) return;
  finish();
  feed = live(selected.screen, selected.path[0]);
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

setInterval(() => { now.value = Date.now(); }, 1000);
start("open");
const destinations = [
  ["▦", "lines, timeline, health"], ["!", "needs you"], ["○", "open work owned by you"], ["▤", "open work by state"],
  ["▷", "running work"], ["↓", "incoming items"], ["↑", "drafts and sends"], ["≡", "prompt files"],
  ["◇", "workflows"], ["▥", "charts"], ["⚙", "bridges, devices, rules"],
];
configurePalette(screenKeys.map(([key, id, title, href], index) => ({ key, id, title, href,
  glyph: destinations[index][0], description: destinations[index][1] })), () => {
  pending.value = "";
  sheet.value = false;
  dismissMenu();
}, () => { mode.value = "NORMAL"; }, [feedbackCommand]);
startFeedback();
startKeys();
globalCommands([{ id: "run-checks", title: "Run checks now", run: () => dispatch(actions.checks) }]);
startHooks();
