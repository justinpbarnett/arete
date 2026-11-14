import { h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import * as data from "./ui-data.js";

const html = htm.bind(h);
const navigation = [
  ["floor", "Floor", "f", "▦", "#floor"],
  ["triage", "Triage", "t", "!", "#triage"],
  ["my-work", "My work", "w", "○", "#board/open-work"],
  ["board", "Board", "b", "▤", "#board"],
  ["runs", "Runs", "r", "▷", "#run"],
  ["intake", "Intake", "n", "↓", "#intake"],
  ["outbox", "Outbox", "o", "↑", "#outbox"],
  ["prompts", "Prompts", "p", "≡", "#prompts"],
  ["adws", "ADWs", "a", "◇", "#adws"],
  ["insights", "Insights", "v", "▥", "#insights"],
  ["settings", "Settings", ",", "⚙", "#settings/health"],
];

function duration(seconds) {
  if (!Number.isFinite(seconds)) return "";
  const value = Math.max(0, Math.floor(seconds));
  return value >= 3600 ? `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m`
    : value >= 60 ? `${Math.floor(value / 60)}m ${value % 60}s` : `${value}s`;
}

function Sidebar({ route, snapshot, now }) {
  const serve = snapshot.serve;
  const started = serve?.started ? Date.parse(serve.started) : NaN;
  const uptime = Number.isFinite(started) ? duration((now - started) / 1000) : "";
  const age = Number.isFinite(serve?.age_seconds)
    ? duration(serve.age_seconds + Math.max(0, now - snapshot.received) / 1000) : "";
  return html`<aside class="sidebar frame" data-pane="sidebar" aria-label="Sidebar">
    <div class="side-top"><span class="mark">A</span><span class="wordmark">arete</span><span class="version" aria-label="Server version"></span></div>
    <div class="search" role="search"><kbd>:</kbd><span>Search</span><kbd>Cmd K</kbd></div>
    <nav class="side-nav" aria-label="Screens">${navigation.map(([id, label, key, glyph, href]) => {
      const count = id === "triage" ? snapshot.needsYou : id === "runs" ? snapshot.running : null;
      return html`<a key=${id} class=${`nav-row${route.active === id ? " selected" : ""}`} href=${href}
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

export function Shell({ route, snapshot, now }) {
  const page = data[route.screen](route);
  const slots = route.screen === "floor" ? html`<p class="slots mono">Slots <span>${snapshot.slots}</span> / <span>${snapshot.capacity}</span></p>` : null;
  const extra = page.extra ? html`<${Pane} name="auxiliary" label=${page.extra} />` : null;
  return html`<div class="ui-app" data-screen=${route.screen}>
    <${Sidebar} route=${route} snapshot=${snapshot} now=${now} />
    <main class="main">
      <div class="top-bar" aria-label="Top bar"></div>
      <header class="page-head">
        <div class="head-row"><h1>${page.title}</h1><div class="head-tools"></div></div>
        <p class="subtitle">${page.subtitle}</p>
        ${page.tabs ? html`<div class="tabs-slot" aria-label="Tabs"></div>` : null}
      </header>
      <div class="pane-grid">
        <${Pane} name="queue" label=${page.queue}>${slots}<//>
        ${route.screen === "run" ? html`<div class="run-focus"><${Pane} name="focus" label=${page.focus} />${extra}</div>`
          : html`<${Pane} name="focus" label=${page.focus} />${extra}`}
      </div>
      <footer class="key-bar" aria-label="Key bar"></footer>
    </main>
  </div>`;
}
