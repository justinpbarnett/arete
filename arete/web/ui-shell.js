import { h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import * as data from "./ui-data.js";
import { screenKeys } from "./ui-keys.js";
import { KeyBar, KeySheet } from "./ui-keybar.js";
import { List, createList, demoQueue, demoFocus } from "./ui-list.js";
import { Empty, Fault, Gate } from "./ui-states.js";
import { readFault } from "./ui-live.js";
import { Palette, openPalette } from "./ui-palette.js";
import { ReasonBox, fixture, fixtureRevision, replayLast } from "./ui-actions.js";
import { RunFocus, RunQueue, RunSubtitle } from "./ui-run.js";

const html = htm.bind(h);
const glyphs = { floor: "▦", triage: "!", "my-work": "○", board: "▤", runs: "▷", intake: "↓", outbox: "↑", prompts: "≡", adws: "◇", insights: "▥", settings: "⚙" };
const navigation = screenKeys.map(([key, id, label, href]) => [id, label, key, glyphs[id], href]);

function duration(seconds) {
  if (!Number.isFinite(seconds)) return "";
  const value = Math.max(0, Math.floor(seconds));
  return value >= 3600 ? `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m`
    : value >= 60 ? `${Math.floor(value / 60)}m ${value % 60}s` : `${value}s`;
}

const floorRuns = createList({ rows: [], empty: "No running work" });
const runRow = (run) => ({ id: run.id, title: [run.ticket, run.step].filter(Boolean).join(" · "), state: "Running",
  repo: run.repo, priority: "", number: `${run.seconds}s` });

export function applyRuns(runs) {
  const rows = (runs ?? []).map(runRow);
  if (JSON.stringify(rows) !== JSON.stringify(floorRuns.state.rows)) floorRuns.setRows(rows);
}

const askParts = [["approvals", "Approvals"], ["skipped", "Skipped"], ["proposals", "Proposals"], ["ready", "Ready"]];
const askSize = (part) => Array.isArray(part?.rows) ? part.rows.length : Object.values(part?.rows ?? {}).reduce((sum, rows) => sum + rows.length, 0);

function queueBody({ route, snapshot, retry, page, demo }) {
  const asks = route.screen === "triage" ? snapshot.asks : null;
  const fault = readFault(route.screen, snapshot);
  const cached = route.screen === "floor" ? snapshot.runs != null : snapshot.live != null;
  const missing = route.screen === "floor" ? "No running work" : route.screen === "triage" ? "Nothing needs you" : `No ${page.queue.toLowerCase()} to show`;
  const empty = demo ? false : route.screen === "floor" ? false : route.screen === "triage" ? asks != null && snapshot.needsYou === 0 : true;
  const body = demo ? demo : route.screen === "floor" ? html`<${List} id="floor-runs" controller=${floorRuns} queue=${true} />`
    : asks ? html`<p class="ui-summary">${askParts.map(([name, label]) => `${label} ${askSize(asks[name])}`).join(" · ")}</p>` : null;
  const beside = fault && cached;
  return html`${beside ? html`<${Fault} what=${page.queue} fault=${fault} retry=${retry} />` : null}
    <${Gate} pending=${snapshot.pending && !cached} fault=${beside ? null : fault} empty=${empty || (!body && cached)} what=${page.queue}
    missing=${missing} retry=${retry}>${body}<//>`;
}

function Sidebar({ route, snapshot, now }) {
  const serve = snapshot.serve;
  const started = serve?.started ? Date.parse(serve.started) : NaN;
  const uptime = Number.isFinite(started) ? duration((now - started) / 1000) : "";
  const age = Number.isFinite(serve?.age_seconds)
    ? duration(serve.age_seconds + Math.max(0, now - snapshot.received) / 1000) : "";
  return html`<aside class="sidebar frame" data-pane="sidebar" aria-label="Sidebar">
    <div class="side-top"><span class="mark">A</span><span class="wordmark">arete</span><span class="version" aria-label="Server version"></span></div>
    <button type="button" class="search" data-act="palette" onClick=${() => openPalette()}><kbd>:</kbd><span>Search</span><kbd>Cmd K</kbd></button>
    <nav class="side-nav" aria-label="Screens" data-list="screens">${navigation.map(([id, label, key, glyph, href]) => {
      const count = id === "triage" ? snapshot.needsYou : id === "runs" ? snapshot.running : null;
      return html`<a key=${id} data-row data-item=${id} data-act=${`go-${id}`} class=${`nav-row${route.active === id ? " selected" : ""}`} href=${href}
        aria-current=${route.active === id ? "page" : undefined}>
        <kbd class="key-slot">${key}</kbd><span class="glyph" aria-hidden="true">${glyph}</span>
        <span class="nav-label">${label}</span><span class=${`count${id === "triage" && count > 0 ? " accent" : ""}`}>${count}</span>
      </a>`;
    })}</nav>
    <footer class="side-foot">
      <p><span class=${serve?.alive ? "serve-up" : "muted"}>serve ${serve?.alive ? "up" : "down"}</span> <span>${uptime}</span></p>
      <p>heartbeat <span>${age}</span></p>
      <p class="host">${location.host}</p>
      <p aria-label="Checks passed over total">checks <span></span> / <span></span></p>
      <p class="health">${snapshot.health}</p>
    </footer>
  </aside>`;
}

function Pane({ name, label, children }) {
  return html`<section class=${`pane frame ${name}`} data-pane=${name} aria-label=${label}>
    <h2 class="pane-label">${label}</h2>${children}
  </section>`;
}

function runQueueBody({ view, route, retry }) {
  const cached = view.queue != null;
  return html`${view.queueError && cached ? html`<${Fault} what="Runs" fault=${view.queueError} retry=${retry} />` : null}
    <${Gate} pending=${!view.settled} fault=${cached ? null : view.queueError} what="Runs" retry=${retry}>
    <${RunQueue} shown=${route.path[0]} more=${view.more} /><//>`;
}

const runPage = () => ({ title: "Runs", subtitle: html`<${RunSubtitle} />`, queue: "Runs", focus: "Run" });

export function Shell({ route, snapshot, now, retry, runs }) {
  fixtureRevision.value;
  const actionList = fixture.value;
  const counts = actionList?.counts();
  if (counts) snapshot = { ...snapshot, needsYou: counts.needsYou };
  const page = route.screen === "run" ? runPage() : data[route.screen](route);
  const health = route.screen === "settings" && route.path[0] === "health";
  const slots = route.screen === "floor" ? html`<p class="slots mono">Slots <span>${snapshot.slots}</span> / <span>${snapshot.capacity}</span></p>` : null;
  const healthFocus = (body, what) => {
    const fault = readFault(route.screen, snapshot);
    const cached = snapshot.live != null;
    return html`${fault && cached ? html`<${Fault} what=${what} fault=${fault} retry=${retry} />` : null}
      <${Gate} pending=${snapshot.pending && !cached} fault=${cached ? null : fault} what=${what} retry=${retry}>${body}<//>`;
  };
  const nothing = (label) => html`<${Empty} what=${`No ${label.toLowerCase()} selected`} />`;
  const extra = page.extra ? html`<${Pane} name="auxiliary" label=${page.extra}>${nothing(page.extra)}<//>` : null;
  const queue = actionList ? html`<${List} id="action-fixture" controller=${actionList} queue=${true} />`
    : route.screen === "run" ? runQueueBody({ view: runs, route, retry }) : queueBody({ route, snapshot, retry, page, demo: health ? html`<${List} id="health-queue" controller=${demoQueue} queue=${true} />` : null });
  return html`<div class="ui-app" data-screen=${route.screen} data-health-demo=${health ? "" : undefined}>
    <${Sidebar} route=${route} snapshot=${snapshot} now=${now} />
    <main class="main">
      <div class="top-bar" aria-label="Top bar"></div>
      <header class="page-head">
        <div class="head-row"><h1>${page.title}</h1><div class="head-tools"></div></div>
        <p class="subtitle">${page.subtitle}</p>
        ${counts ? html`<p class="subtitle">${counts.total} open · ${counts.needsYou} need you · ${counts.high} high · ${counts.med} med</p>` : null}
        ${page.tabs ? html`<div class="tabs-slot" aria-label="Tabs"></div>` : null}
      </header>
      <div class="pane-grid">
        <${Pane} name="queue" label=${page.queue}>${slots}${queue}<//>
        ${route.screen === "run" ? html`<${Pane} name="focus" label=${page.focus}><${RunFocus} view=${runs} now=${now} retry=${retry} /><//>`
          : html`<${Pane} name="focus" label=${page.focus}>${health ? healthFocus(html`<${List} id="health-focus" controller=${demoFocus} />`, page.focus) : nothing(page.focus)}<//>${extra}`}
      </div>
      <${KeyBar} />
      ${actionList ? html`<button class="fixture-replay" type="button" onClick=${replayLast}>Replay last request</button>` : null}
      <${ReasonBox} />
    </main>
    <${KeySheet} />
    <${Palette} />
  </div>`;
}
