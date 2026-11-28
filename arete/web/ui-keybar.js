import { h, Component } from "./vendor-preact.js";
import { effect } from "./vendor-signals.js";
import htm from "./vendor-htm.js";
import { mode, pane, pending, sheet, notice, connection, readRetry, keybarRevision, hints, sheetGroups, toggleSheet } from "./ui-keys.js";
import { Connectivity } from "./ui-states.js";

import { openFeedback } from "./ui-feedback.js";

const html = htm.bind(h);

function sendFeedback() {
  toggleSheet();
  openFeedback();
}

const caps = (keys) => keys.map((key) => html`<kbd class="cap" key=${key}>${key}</kbd>`);

export class KeyBar extends Component {
  state = { revision: 0 };
  componentDidMount() {
    this.unsubscribe = effect(() => {
      mode.value; pane.value; pending.value; sheet.value; notice.value; connection.value; readRetry.value; keybarRevision.value;
      this.setState({ revision: this.state.revision + 1 });
    });
  }
  componentWillUnmount() { this.unsubscribe(); }
  render() { return keyBar(); }
}

function keyBar() {
  const insert = mode.value === "INSERT";
  return html`<footer class="key-bar" aria-label="Key bar">
    <span class=${`mode${insert ? " insert" : ""}`} data-mode=${mode.value}>${mode.value}</span>
    <span class="hints">${hints().map(([keys, label]) => html`<span class="hint" key=${label}>${caps(keys)}<span>${label}</span></span>`)}</span>
    <span class="echo mono" aria-label="Pending key">${pending.value}</span>
    <${Connectivity} word=${connection.value} />
    <span class="notice" role="status">${notice.value}</span>
    <button type="button" class="help" data-act="help" aria-label="Key sheet" onClick=${toggleSheet}><kbd class="cap">?</kbd></button>
  </footer>`;
}

export function KeySheet() {
  if (!sheet.value) return null;
  return html`<div class="sheet-scrim" onClick=${(event) => { if (event.target === event.currentTarget) toggleSheet(); }}>
    <div class="key-sheet" role="dialog" aria-modal="true" aria-label="Keys" tabindex="-1">
      <header class="sheet-head"><span class="sheet-title">Keys</span>
        <span class="sheet-note">vim style · in a text box only Esc and Cmd+Enter act</span>
        <button type="button" class="close" data-act="close-sheet" onClick=${toggleSheet}><kbd class="cap">Esc</kbd></button>
      </header>
      ${sheetGroups.map(([title, entries]) => html`<section class="sheet-group" key=${title}>
        <h3>${title}</h3>
        <div class="sheet-grid">${entries.map(([keys, label]) => html`<div class="sheet-entry" key=${keys.join(" ")}>
          <span class="sheet-caps">${caps(keys)}</span><span>${label}</span></div>`)}</div>
      </section>`)}
      <section class="sheet-group">
        <h3>Feedback</h3>
        <button type="button" class="sheet-action" data-act="send-feedback" onClick=${sendFeedback}>Send feedback</button>
      </section>
    </div>
  </div>`;
}
