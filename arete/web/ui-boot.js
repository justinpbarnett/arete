import { h, render } from "./vendor-preact.js";
import { signal, effect, batch } from "./vendor-signals.js";
import { Shell } from "./ui-shell.js";
import { current, listen } from "./ui-router.js";
import { visit } from "./ui-data.js";
import { live, connectivity, readFault, runView, screenReads } from "./ui-live.js";
import { applyQueue } from "./ui-run.js";
import { startKeys, marks, screenKeys, pending, sheet, mode, connection, readRetry, activatePane } from "./ui-keys.js";
import { configurePalette, globalCommands } from "./ui-palette.js";
import { actions, dispatch } from "./ui-actions.js";
import { dismissMenu } from "./ui-menu.js";
import { startHooks } from "./ui-hooks.js";
import { feedbackCommand, startFeedback } from "./ui-feedback.js";
import { normalizePrimary } from "./ui-list.js";
import { pairedBrowser } from "./browser-auth.js";
import { browserPaired } from "./ui-notice.js";
import { initSettingsPrefs } from "./ui-settings.js";
import { triageAndonCount } from "./ui-triage.js";
import "./ui-triage-kinds.js";
import "./ui-incidents.js";

initSettingsPrefs();

const route = signal(current());
const connect = ({ screen, path }) => live(screen, ...path.slice(0, 2));
let feed = connect(route.value);
const snapshot = feed.snapshot;
const now = signal(Date.now());
const tab = [...crypto.getRandomValues(new Uint8Array(8))].map((byte) => byte.toString(16).padStart(2, "0")).join("");
const phone = matchMedia("(pointer: coarse) and (max-width: 1040px)");
let stint = null;
let via = "hash";

function start(source) {
  if (!document.hidden && !stint) stint = { view: `ui-${route.value.screen}`, via: source, at: performance.now() };
}

function finish() {
  if (!stint) return;
  const outgoing = stint;
  stint = null;
  visit({ tab, device: phone.matches ? "phone" : "desktop", view: outgoing.view, via: outgoing.via,
    ms: Math.max(0, Math.min(86400000, Math.round(performance.now() - outgoing.at))) });
}

const retry = () => feed.retry();

effect(() => applyQueue(runView.value));

effect(() => {
  const selected = route.value;
  const latest = snapshot.value;
  const runs = runView.value;
  triageAndonCount.value;
  const runFault = selected.screen === "run" && (runs.queueError || runs.detailError);
  connection.value = connectivity(latest, now.value);
  readRetry.value = readFault(selected.screen, latest) || runFault ? retry : null;
  render(h(Shell, { route: selected, snapshot: latest, resources: screenReads.value, now: now.value, retry, runs }), document.getElementById("app"));
  marks();
  normalizePrimary();
  document.title = `arete · ${selected.screen === "board" && selected.active === "my-work" ? "Open work" : selected.screen}`;
});

listen((selected) => {
  if (selected.hash === route.value.hash) return;
  finish();
  batch(() => {
    feed = connect(selected);
    route.value = selected;
  });
  const opensItem = selected.screen === "task" ? selected.path[0] : ["triage", "outbox"].includes(selected.screen) && selected.path[1];
  const grid = document.querySelector(".pane-grid");
  if (grid && selected.screen !== "run") grid.scrollTop = 0;
  if (opensItem) {
    const focus = document.getElementById("pane-focus");
    if (focus) { focus.scrollTop = 0; focus.focus({ preventScroll: true }); }
  } else if (selected.screen !== "run") activatePane("queue");
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
window.addEventListener("pageshow", () => {
  start("return");
  if (location.protocol === "https:") pairedBrowser().then(paired => {
    browserPaired.value = paired;
  }).catch(() => {});
});

setInterval(() => { now.value = Date.now(); }, 1000);
start("open");
const destinations = [
  ["▦", "lines, timeline, health"], ["!", "needs you"], ["○", "open local tasks"], ["▤", "open work by state"],
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
