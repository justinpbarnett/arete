import { h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import * as data from "./ui-data.js";
import { screenKeys, activatePane } from "./ui-keys.js";
import { KeyBar, KeySheet } from "./ui-keybar.js";
import { List } from "./ui-list.js";
import { Fault, Gate } from "./ui-states.js";
import { InboxFocus, InboxQueue, RunQuestions, selectedInbox } from "./ui-inbox.js";
import { FloorFocus, FloorQueue, TaskFocus, TaskQueue } from "./ui-work.js";
import { SettingsFocus, SettingsQueue } from "./ui-settings.js";
import { Palette, openPalette } from "./ui-palette.js";
import { ReasonBox, fixture, fixtureRevision, replayLast } from "./ui-actions.js";
import { RunFocus, RunQueue, RunSubtitle } from "./ui-run.js";
import { TopBar } from "./ui-topbar.js";
import { browserPaired, clientNotice, staleWrite } from "./ui-notice.js";

const html = htm.bind(h);
const glyphs = { floor: "▦", triage: "!", "my-work": "○", board: "▤", runs: "▷", intake: "↓", outbox: "↑", prompts: "≡", adws: "◇", insights: "▥", settings: "⚙" };
const navigation = screenKeys.map(([key, id, label, href]) => [id, label, key, glyphs[id], href]);

function duration(seconds) {
  if (!Number.isFinite(seconds)) return "";
  const value = Math.max(0, Math.floor(seconds));
  return value >= 3600 ? `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m`
    : value >= 60 ? `${Math.floor(value / 60)}m ${value % 60}s` : `${value}s`;
}

const classic = {
  intake: ["/#triage", "Open ticket intake in classic"],
  insights: ["/#outcomes", "Open outcomes in classic"],
};

function NotPorted({ screen }) {
  const link = classic[screen];
  return html`<div class="ops-detail"><h3>Not yet in the new dashboard</h3>
    ${link ? html`<a href=${link[0]}>${link[1]}</a>` : html`<p>${screen === "prompts"
    ? "Prompt files and overrides are still managed outside the dashboard." : "Edit ADWs in your checkout. Loop operations are available in classic."}</p>`}
    ${screen === "adws" ? html`<a href="/#loops">Open loops in classic</a>` : null}
    ${screen === "insights" ? html`<a href="/#usage">Open usage in classic</a>` : null}</div>`;
}

export function Sidebar({ route, snapshot, now }) {
  const serve = snapshot.serve;
  const started = serve?.started ? Date.parse(serve.started) : NaN;
  const uptime = Number.isFinite(started) ? duration((now - started) / 1000) : "";
  const age = Number.isFinite(serve?.age_seconds)
    ? duration(serve.age_seconds + Math.max(0, now - snapshot.received) / 1000) : "";
  const questions = snapshot.questions;
  const needs = snapshot.needsYou != null && questions?.data && !questions.error ? snapshot.needsYou + questions.data.asks.length : null;
  return html`<aside class="sidebar frame" data-pane="sidebar" aria-label="Sidebar">
    <div class="side-top"><span class="mark">A</span><span class="wordmark">arete</span><span class="version" aria-label="Server version"></span></div>
    <button type="button" class="search" data-act="palette" aria-label="Search (Cmd K)" title="Search (Cmd K)" onClick=${() => openPalette()}><kbd>:</kbd><span>Search</span><kbd>Cmd K</kbd></button>
    <nav class="side-nav" aria-label="Screens" data-list="screens">${navigation.map(([id, label, key, glyph, href]) => {
      const count = id === "triage" ? needs : id === "runs" ? snapshot.running : null;
      return html`<a key=${id} data-row data-item=${id} data-act=${`go-${id}`} class=${`nav-row${route.active === id ? " selected" : ""}`} href=${href}
        aria-label=${label} title=${`${label} (${key})`} aria-current=${route.active === id ? "page" : undefined}>
        <kbd class="key-slot">${key}</kbd><span class="glyph" aria-hidden="true">${glyph}</span>
        <span class="nav-label">${label}</span><span class=${`count${id === "triage" && count > 0 ? " accent" : ""}`}>${count || null}</span>
      </a>`;
    })}</nav>
    <footer class="side-foot">
      <p><span class=${serve?.alive ? "serve-up" : "muted"}>serve ${!serve ? "unknown" : serve.alive ? "up" : "down"}</span> <span>${uptime}</span></p>
      ${serve?.alive && Object.keys(serve.failing || {}).length ? html`<p class="serve-fails">tick fails at ${Object.entries(serve.failing).map(([key, f]) => `${key}: ${f.error}`).join("; ")}</p>` : null}
      ${age ? html`<p>serve beat <span>${age}</span></p>` : null}
      <p class="host">${location.host}</p>
      <p class="health">${snapshot.health}</p>
    </footer>
  </aside>`;
}

function Pane({ name, label, children }) {
  return html`<section id=${`pane-${name}`} class=${`pane frame ${name}`} data-pane=${name} aria-label=${label} tabindex="-1">
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

function panels({ route, snapshot, resources, now, retry, runs }) {
  switch (route.screen) {
    case "floor": return [html`${snapshot.slots == null ? null : html`<p class="slots mono">Slots ${snapshot.slots} / ${snapshot.capacity}</p>`}
      <${FloorQueue} snapshot=${snapshot} retry=${retry} />`, html`<${FloorFocus} snapshot=${snapshot} retry=${retry} />`];
    case "triage":
    case "outbox": return [html`<${InboxQueue} snapshot=${snapshot} route=${route} retry=${retry} />`,
      html`<${InboxFocus} snapshot=${snapshot} route=${route} retry=${retry} />`];
    case "task":
    case "board": return [html`<${TaskQueue} resource=${resources.tasks} route=${route} retry=${retry} recovering=${snapshot.live?.recovering_tasks} />`,
      html`<${TaskFocus} resource=${resources.task} route=${route} retry=${retry} recovering=${snapshot.live?.recovering_tasks} />`];
    case "run": return [runQueueBody({ view: runs, route, retry }), html`
      ${runs.detail?.run?.status === "needs-info" ? html`<${RunQuestions} resource=${snapshot.questions} run=${runs.id} retry=${retry} />` : null}
      <${RunFocus} key=${runs.id} view=${runs} now=${now} retry=${retry} />`];
    case "settings": return [html`<${SettingsQueue} route=${route} />`, html`<${SettingsFocus} route=${route} resources=${resources} now=${now} retry=${retry} />`];
    default: return [html`<${NotPorted} screen=${route.screen} />`, null];
  }
}

export function Shell({ route, snapshot, resources, now, retry, runs }) {
  fixtureRevision.value;
  const actionList = fixture.value;
  const counts = actionList?.counts();
  const page = route.screen === "run" ? runPage() : data[route.screen](route);
  const [queue, focus] = panels({ route, snapshot, resources, now, retry, runs });
  const parent = selectedInbox(route) && ["triage", "outbox"].includes(route.screen)
    ? [`#${route.screen}`, `Back to ${route.screen === "triage" ? "inbox" : "Outbox"}`]
    : route.screen === "task" && route.path[0] ? ["#board", "Back to Board"] : null;
  return html`<div class="ui-app" data-screen=${route.screen} data-detail=${parent ? "" : undefined}>
    <${Sidebar} route=${route} snapshot=${snapshot} now=${now} />
    <main class="main">
      <${TopBar} route=${route} snapshot=${snapshot} now=${now} runs=${runs} page=${page} />
      <header class="page-head">
        <div class="head-row"><h1>${page.title}</h1><div class="head-tools"><factory-switch></factory-switch>
          ${browserPaired.value ? html`<a class="ops-back" href="/pair">Pair</a>` : null}
          ${parent ? html`<a class="ops-back" href=${parent[0]}>← ${parent[1]}</a>` : null}
          ${route.screen === "run" ? html`<button type="button" class="run-back" aria-controls="pane-queue" onClick=${() => activatePane("queue")}>← Back to runs</button>` : null}
        </div></div>
        <p class="subtitle">${page.subtitle}</p>
        ${counts ? html`<p class="subtitle">${counts.total} open · ${counts.needsYou} need you · ${counts.high} high · ${counts.med} med</p>` : null}
      </header>
      ${clientNotice.value ? html`<p class="ui-state" role="status">${clientNotice.value}</p>` : null}
      ${staleWrite.value ? html`<p class="ui-state" data-state="stale" role="status">${staleWrite.value}</p>` : null}
      <div class=${`pane-grid${focus ? "" : " single"}`}>
        <${Pane} name="queue" label=${page.queue}>${actionList ? html`<${List} id="action-fixture" controller=${actionList} queue=${true} />` : queue}<//>
        ${focus ? html`<${Pane} name="focus" label=${page.focus}>${focus}<//>` : null}
      </div>
      <${KeyBar} />
      ${actionList ? html`<button class="fixture-replay" type="button" onClick=${replayLast}>Replay last request</button>` : null}
      <${ReasonBox} />
    </main>
    <${KeySheet} />
    <${Palette} />
  </div>`;
}
