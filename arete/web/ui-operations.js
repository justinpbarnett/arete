import { Component, h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { isPublication, prepareWrite, sendPublication, sendWrite } from "./ui-write.js";
import { notice } from "./ui-notice.js";
import { Empty, Fault, Loading } from "./ui-states.js";

export const html = htm.bind(h);
export const safeLink = (url) => typeof url === "string" && /^https?:\/\//i.test(url) ? url : undefined;

export function Read({ resource, what, retry, children }) {
  return html`${resource?.error ? html`<${Fault} what=${what} fault=${resource.error} retry=${retry} />` : null}
    ${resource?.data ? children(resource.data) : !resource?.error ? html`<${Loading} what=${what} />` : null}`;
}

export function Panel({ panel, title, retry, children }) {
  return html`<section class="ops-section"><h3>${title}</h3>
    ${panel?.rows != null ? children(panel.rows) : html`<${Fault} what=${title} fault=${{ message: panel?.error }} retry=${retry} />`}</section>`;
}

export function Rows({ id, rows, open, empty }) {
  if (!rows.length) return html`<${Empty} what=${empty} />`;
  return html`<div class="ops-rows" data-list=${id}>${rows.map((row) => html`
    <a key=${row.id} href=${row.href} data-row="" data-item=${row.id} aria-current=${row.id === open ? "page" : undefined}>
      <span class="ops-row-main"><span class="ops-row-title">${row.title}</span><small>${row.context}</small></span>
      <span class="ops-row-state">${row.state}</span>
    </a>`)}</div>`;
}

export function Text({ text }) {
  return text ? html`<p class="ops-text">${text}</p>` : null;
}

export function DetailLink({ href, children }) {
  const safe = safeLink(href);
  return safe ? html`<a href=${safe} target="_blank" rel="noopener noreferrer">${children}</a>` : null;
}

const sending = new Set();

export class Operation extends Component {
  state = { busy: false, error: "" };
  send = async (path, body, say, { clearSecret } = {}) => {
    if (sending.has(path)) return false;
    sending.add(path);
    this.setState({ busy: true, error: "" });
    try {
      const publication = isPublication(path, body);
      const request = publication ? null : prepareWrite(path, body);
      clearSecret?.();
      const result = publication ? await sendPublication(path, body) : await sendWrite(request);
      notice.value = say(result);
      return true;
    } catch (fault) {
      this.setState({ error: clearSecret ? "The answer could not be confirmed. Secret fields were cleared. Check the current state before trying again."
        : fault.status ? fault.message : `${fault.message}. Check the current state before trying again.` });
      return false;
    } finally {
      try { await this.props.retry(); }
      finally { this.setState({ busy: false }); sending.delete(path); }
    }
  };
}

export class Decisions extends Operation {
  select(label) {
    this.setState({ choice: label, error: "" }, () => {
      const target = label ? this.cancel : [...(this.root?.querySelectorAll("button") ?? [])].find((button) => button.textContent === this.last);
      target?.focus();
    });
  }
  render({ options, disabled = false }, { choice, busy, error }) {
    const picked = options.find((option) => option.label === choice);
    if (!options.length) return null;
    return html`<div class="ops-actions" ref=${(root) => { this.root = root; }}>
      ${picked ? html`<p>${picked.confirm}</p>
        <button type="button" disabled=${busy || disabled || picked.disabled} onClick=${async () => {
    if (await this.send(picked.path, picked.body ?? {}, picked.say)) this.select(null);
  }}>Confirm ${picked.label.toLowerCase()}</button>
        <button type="button" ref=${(button) => { this.cancel = button; }} disabled=${busy} onClick=${() => this.select(null)}>Cancel</button>`
    : options.map((option) => html`<button key=${option.label} type="button" disabled=${busy || disabled || option.disabled}
      onClick=${() => { this.last = option.label; this.select(option.label); }}>${option.label}</button>`)}
      ${error ? html`<p class="ops-error" role="alert">${error}</p>` : null}
    </div>`;
  }
}
