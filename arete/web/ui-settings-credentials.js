import { Component, h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { webFetch } from "./ui-request.js";
import { prepareTextWrite, prepareWrite, sendWrite } from "./ui-write.js";
import { onLiveEvent } from "./ui-live.js";
import { notice, failed, succeeded } from "./ui-notice.js";
import { Fault, Loading } from "./ui-states.js";
import { registerSettingsSection, refreshSettingsSecrets } from "./ui-settings.js";
import { SettingsTableFrame, registerSettingsForm } from "./ui-settings-frame.js";

const html = htm.bind(h);

export const SECRETS_API = "/api/v1/secrets";
export const CREDENTIAL_UNDO_NOTICE = "A removed credential cannot be restored. Add it again.";
const SCOPE_PATTERN = /^(?:global|(?:repo|line|device|connector):[A-Za-z0-9][A-Za-z0-9_.-]{0,63})$/;
const NAME_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/;
const REPO_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

export function secretRoute(scope, name, suffix = "") {
  return `${SECRETS_API}/${encodeURIComponent(scope)}/${encodeURIComponent(name)}${suffix}`;
}

export function isSecureOrigin() {
  if (typeof globalThis.window === "undefined") return true;
  return Boolean(globalThis.window.isSecureContext || globalThis.location?.protocol === "https:");
}

export function validateCredentialInput(scope, name, value) {
  const cleanScope = String(scope ?? "").trim() || "global";
  const cleanName = String(name ?? "").trim();
  if (!SCOPE_PATTERN.test(cleanScope)) {
    return "Scope must be global or repo:, line:, device:, connector: followed by an identifier.";
  }
  const namePattern = cleanScope.startsWith("repo:") ? REPO_NAME_PATTERN : NAME_PATTERN;
  if (!namePattern.test(cleanName)) {
    return cleanScope.startsWith("repo:")
      ? "Enter a valid credential name."
      : "Name must start with an uppercase letter and use A-Z, 0-9, or _.";
  }
  if (typeof value !== "string" || !value.length) {
    return "Enter a credential value.";
  }
  return "";
}

export function credentialStatus(item) {
  if (!item?.present) return "missing";
  if (item.check_ok === false) return "failed";
  if (item.check_ok === true) return "valid";
  return "set";
}

function shortRunLabel(id) {
  const text = String(id ?? "");
  return text.length > 10 ? text.slice(-6) : text;
}

export function waitingLinksFor(triageItem) {
  if (!triageItem) return [];
  const detail = triageItem.detail ?? {};
  const waiting = Array.isArray(detail.waiting) ? detail.waiting : [];
  const links = [];
  const seen = new Set();
  const pushLink = (kind, id, label, href) => {
    const key = `${kind}:${id}`;
    if (!id || seen.has(key)) return;
    seen.add(key);
    links.push({ kind, id, label, href });
  };
  for (const subject of waiting) {
    if (typeof subject !== "string") continue;
    if (subject.startsWith("run:")) {
      const runId = subject.slice(4);
      pushLink("run", runId, `run ${shortRunLabel(runId)}`, `#run/${encodeURIComponent(runId)}`);
    } else if (subject.startsWith("outbox:")) {
      const obId = subject.slice(7);
      const label = /^OB-/i.test(obId) ? obId : `OB-${obId}`;
      pushLink("outbox", obId, label, `#outbox/${encodeURIComponent(obId)}`);
    } else if (subject.startsWith("task:")) {
      const taskId = subject.slice(5);
      pushLink("task", taskId, taskId, `#task/${encodeURIComponent(taskId)}`);
    }
  }
  if (triageItem.run_id) {
    pushLink("run", triageItem.run_id, `run ${shortRunLabel(triageItem.run_id)}`, `#run/${encodeURIComponent(triageItem.run_id)}`);
  }
  if (triageItem.id) {
    pushLink("triage", triageItem.id, triageItem.id, `#triage/${encodeURIComponent(triageItem.id)}`);
  }
  return links;
}

function extractContinueItems(triageData) {
  const found = [];
  const groups = triageData?.by_priority ?? {};
  for (const bucket of [groups.high, groups.med, groups.low, triageData?.items]) {
    if (!Array.isArray(bucket)) continue;
    for (const item of bucket) {
      if (item?.kind === "continue" && typeof item.subject === "string" && item.subject.startsWith("secret:")) {
        found.push(item);
      }
    }
  }
  return found;
}

export function buildCredentialRows(secrets = [], triageData = null, accounts = []) {
  const triageBySubject = new Map();
  for (const item of extractContinueItems(triageData)) {
    triageBySubject.set(item.subject, item);
  }
  const accountsBySecret = new Map();
  for (const account of Array.isArray(accounts) ? accounts : []) {
    if (!account?.secret_name) continue;
    const list = accountsBySecret.get(account.secret_name) ?? [];
    list.push(account);
    accountsBySecret.set(account.secret_name, list);
  }
  return (Array.isArray(secrets) ? secrets : []).map((raw) => {
    const scope = raw.scope || "global";
    const name = raw.name || "";
    const subject = raw.subject || `secret:${scope}/${name}`;
    const triageItem = triageBySubject.get(subject) ?? null;
    const waitingLinks = waitingLinksFor(triageItem);
    const linkedAccounts = accountsBySecret.get(name) ?? [];
    const uses = [];
    for (const entry of Array.isArray(raw.uses) ? raw.uses : []) {
      if (entry && !uses.includes(entry)) uses.push(entry);
    }
    if (raw.service && !uses.includes(raw.service)) uses.push(raw.service);
    for (const account of linkedAccounts) {
      const label = [account.service, account.usage].filter(Boolean).join(" ");
      if (label && !uses.includes(label)) uses.push(label);
    }
    const status = credentialStatus(raw);
    const detail = triageItem?.detail ?? {};
    const missingSince = raw.missing_since ?? detail.missing_since ?? null;
    const lastUsed = raw.last_used ?? raw.last_checked ?? null;
    return {
      ...raw,
      id: `${scope}/${name}`,
      subject,
      scope,
      name,
      status,
      uses,
      usedByText: uses.length ? uses.join(", ") : "none",
      lastUsed,
      missingSince,
      triageId: triageItem?.id ?? null,
      waitingLinks,
      rowClass: status === "missing" ? "set-row-ny" : "",
    };
  });
}

function formatAge(stamp, now = Date.now()) {
  if (!stamp) return "none";
  const ms = Date.parse(stamp);
  if (!Number.isFinite(ms)) return String(stamp);
  const seconds = Math.max(0, Math.floor((now - ms) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) {
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    return mins ? `${hours}h ${mins}m` : `${hours}h`;
  }
  return `${Math.floor(seconds / 86400)}d`;
}

function formatDate(stamp) {
  if (!stamp) return "none";
  const text = String(stamp);
  return /^\d{4}-\d{2}-\d{2}/.test(text) ? text.slice(0, 10) : text;
}

function formatStamp(stamp) {
  if (!stamp) return "";
  const ms = Date.parse(stamp);
  if (!Number.isFinite(ms)) return String(stamp);
  return new Date(ms).toLocaleString();
}

export function describeWriteError(error) {
  if (!error) return "Request failed.";
  if (error.status === 426 || error.code === "tls_required") {
    return "HTTPS is required to save a credential. Open Arete over HTTPS from a paired browser session.";
  }
  if (error.code === "forbidden" && error.reason === "presence_required") {
    return "A paired owner browser session with the owner present is required for this credential action.";
  }
  if (error.code === "secret_internal") {
    return "This credential is managed by its owning service and cannot be edited here.";
  }
  if (error.code === "secret_store_missing") {
    return "No secret store is configured on this instance.";
  }
  return error.message || "Request failed.";
}

export async function submitCredentialValue(scope, name, value) {
  const problem = validateCredentialInput(scope, name, value);
  if (problem) throw new Error(problem);
  const cleanScope = String(scope).trim() || "global";
  const cleanName = String(name).trim();
  const request = prepareTextWrite(secretRoute(cleanScope, cleanName, "/value"), value, {
    method: "PUT",
    type: "text/plain",
  });
  return sendWrite(request);
}

export async function checkCredential(scope, name) {
  const request = prepareWrite(secretRoute(scope, name, "/check"), {}, null, { method: "POST" });
  return sendWrite(request);
}

export async function removeCredential(scope, name) {
  const request = prepareWrite(secretRoute(scope, name), {}, null, { method: "DELETE" });
  return sendWrite(request);
}

async function readJson(url) {
  try {
    const response = await webFetch(url, { cache: "no-store" });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      return { error: { status: response.status, code: body?.code ?? null, message: body?.detail ?? body?.error ?? `HTTP ${response.status}` } };
    }
    if (!body || typeof body !== "object" || Array.isArray(body)) {
      return { error: { status: response.status, message: "Malformed response" } };
    }
    return { data: body };
  } catch (error) {
    return { error: { status: null, message: error?.message || "Network error" } };
  }
}

class NeedsYouBlock extends Component {
  state = { busy: false, error: "", status: "" };
  componentWillUnmount() {
    this.clearInput();
  }
  componentDidUpdate(prevProps) {
    if (prevProps.row?.id !== this.props.row?.id) {
      this.clearInput();
      this.setState({ error: "", status: "" });
    }
  }
  clearInput() {
    if (this.input) this.input.value = "";
  }
  submit = async (event) => {
    event.preventDefault();
    const { row, onSaved } = this.props;
    if (!row || this.state.busy) return;
    const raw = this.input ? this.input.value : "";
    this.clearInput();
    if (!raw) {
      this.setState({ error: `Paste a value for ${row.name} before saving.`, status: "" });
      this.input?.focus?.();
      return;
    }
    this.setState({ busy: true, error: "", status: "" });
    try {
      const result = await submitCredentialValue(row.scope, row.name, raw);
      this.clearInput();
      const pending = Boolean(result?.pending);
      const message = pending
        ? `Queued ${row.name} for owner confirmation in Triage.`
        : `Added ${row.name}`;
      notice.value = message;
      this.setState({ status: message, error: "" });
      await onSaved?.(row, result);
    } catch (error) {
      this.clearInput();
      const message = describeWriteError(error);
      this.setState({ error: message, status: "" });
      failed(new Error(message));
    } finally {
      this.clearInput();
      this.setState({ busy: false });
    }
  };
  render({ row }, { busy, error, status }) {
    const secure = isSecureOrigin();
    return html`<section class="set-waitbox" aria-label=${`Needs you: ${row.name}`}>
      <div class="set-wait-head">
        <span class="set-chip set-chip-ny">◆ NEEDS YOU</span>
        <span class="mono set-cred-name">${row.name}</span>
        <span class="muted set-wait-meta">
          ${row.missingSince ? `missing since ${formatStamp(row.missingSince)} · ` : ""}${row.scope}
        </span>
      </div>
      ${row.waitingLinks.length ? html`<div class="set-wait-line">
        <span class="set-line">Waiting on it: ${row.waitingLinks.map((link, index) => html`
          ${index > 0 ? " · " : ""}
          <a key=${`${link.kind}:${link.id}`} class="set-link mono" href=${link.href}>${link.label}</a>`)}</span>
      </div>` : null}
      <form class="ops-form set-cred-form" onSubmit=${this.submit} aria-label=${`Add ${row.name}`}>
        <input ref=${(el) => { this.input = el; }} class="set-inp set-cred-inp" type="password"
          autocomplete="off" spellcheck=${false} disabled=${busy}
          placeholder=${`Paste ${row.name}`} aria-label=${`Paste ${row.name}`} />
        <button type="submit" class="set-btn set-btn-primary" disabled=${busy}>
          <span>${busy ? "Saving…" : "Add credential"}</span><kbd class="cap">⌘↵</kbd>
        </button>
        <span class="mono muted set-cred-hint">i to type · ⌘↵ adds</span>
      </form>
      ${secure ? null : html`<p class="set-tnote">Secret writes require HTTPS and a paired owner session.</p>`}
      ${error ? html`<p class="set-status set-status-err" role="alert">${error}</p>` : null}
      ${status ? html`<p class="set-status" role="status">${status}</p>` : null}
    </section>`;
  }
}

class StoredCredentialBar extends Component {
  state = { checking: false, saving: false, error: "", status: "" };
  componentWillUnmount() {
    this.clearInput();
  }
  componentDidUpdate(prevProps) {
    if (prevProps.row?.id !== this.props.row?.id) {
      this.clearInput();
      this.setState({ error: "", status: "" });
    }
  }
  clearInput() {
    if (this.input) this.input.value = "";
  }
  runCheck = async () => {
    const { row, onUpdated } = this.props;
    if (!row || this.state.checking) return;
    this.setState({ checking: true, error: "", status: "" });
    try {
      const result = await checkCredential(row.scope, row.name);
      const ok = result?.secret?.check_ok;
      const text = ok === true ? `${row.name}: check passed`
        : ok === false ? `${row.name}: check failed`
          : `${row.name}: checked`;
      succeeded(text, false);
      this.setState({ status: text, error: "" });
      await onUpdated?.(result?.secret ?? null);
    } catch (error) {
      const message = describeWriteError(error);
      this.setState({ error: message, status: "" });
      failed(new Error(message));
    } finally {
      this.setState({ checking: false });
    }
  };
  rotate = async (event) => {
    event.preventDefault();
    const { row, onUpdated } = this.props;
    if (!row || this.state.saving) return;
    const raw = this.input ? this.input.value : "";
    this.clearInput();
    if (!raw) {
      this.setState({ error: `Paste a new value for ${row.name} before saving.`, status: "" });
      this.input?.focus?.();
      return;
    }
    this.setState({ saving: true, error: "", status: "" });
    try {
      const result = await submitCredentialValue(row.scope, row.name, raw);
      this.clearInput();
      const text = result?.pending ? `Queued ${row.name} update for owner confirmation.` : `Updated ${row.name}`;
      succeeded(text, false);
      this.setState({ status: text, error: "" });
      await onUpdated?.(result?.secret ?? null);
    } catch (error) {
      this.clearInput();
      const message = describeWriteError(error);
      this.setState({ error: message, status: "" });
      failed(new Error(message));
    } finally {
      this.clearInput();
      this.setState({ saving: false });
    }
  };
  render({ row }, { checking, saving, error, status }) {
    return html`<section class="set-panel set-cred-detail" aria-label=${`Selected credential ${row.name}`}>
      <div class="set-qh">
        <div class="set-line-row">
          <span class="mono set-cred-name">${row.name}</span>
          <span class="muted">${row.scope}</span>
          ${row.last_checked ? html`<span class="mono muted">${`checked ${formatStamp(row.last_checked)}`}</span>` : null}
        </div>
        <button type="button" class="set-btn" disabled=${checking} onClick=${this.runCheck}>
          ${checking ? "Checking…" : "Check credential"}
        </button>
      </div>
      <form class="ops-form set-cred-form" onSubmit=${this.rotate} aria-label=${`Update ${row.name}`}>
        <input ref=${(el) => { this.input = el; }} class="set-inp set-cred-inp" type="password"
          autocomplete="off" spellcheck=${false} disabled=${saving}
          placeholder=${`Replace ${row.name}`} aria-label=${`Replace ${row.name}`} />
        <button type="submit" class="set-btn" disabled=${saving}>
          <span>${saving ? "Saving…" : "Update value"}</span><kbd class="cap">⌘↵</kbd>
        </button>
      </form>
      ${error ? html`<p class="set-status set-status-err" role="alert">${error}</p>` : null}
      ${status ? html`<p class="set-status" role="status">${status}</p>` : null}
    </section>`;
  }
}

export function createCredentialFormSpec(onSaved) {
  return {
    kind: "credential",
    section: "credentials",
    noun: "credential",
    label: "NEW CREDENTIAL",
    submitLabel: "Add credential",
    sensitive: true,
    fields: [
      { name: "name", label: "Name", type: "text", placeholder: "Name (e.g. ASANA_TOKEN)", className: "set-cred-name-inp" },
      { name: "scope", label: "Scope", type: "text", defaultValue: "global", placeholder: "Scope (global)", className: "set-cred-scope-inp" },
      { name: "value", label: "Value", type: "password", placeholder: "Paste secret value", className: "set-cred-val-inp" },
    ],
    validate(values) {
      return validateCredentialInput(values.scope || "global", values.name, values.value);
    },
    async submit(values) {
      const scope = String(values.scope ?? "").trim() || "global";
      const name = String(values.name ?? "").trim();
      const value = values.value;
      values.value = "";
      try {
        const result = await submitCredentialValue(scope, name, value);
        const text = result?.pending
          ? `Queued ${name} for owner confirmation in Triage.`
          : `Added ${name}`;
        succeeded(text, false);
        await onSaved?.(result);
        return result;
      } catch (error) {
        throw new Error(describeWriteError(error));
      }
    },
  };
}

let activeCredentialsReload = null;

registerSettingsForm("credential", createCredentialFormSpec(async () => {
  await Promise.all([activeCredentialsReload?.(), refreshSettingsSecrets()]);
}));

function renderStatusCell(row) {
  if (row.status === "missing") return html`<span class="set-chip set-chip-ny">◆ MISSING</span>`;
  if (row.status === "failed") return html`<span class="set-st set-st-fault">▲ FAILED</span>`;
  if (row.status === "valid") return html`<span class="set-st set-st-ok">✓ VALID</span>`;
  return html`<span class="set-st set-st-ok">✓ SET</span>`;
}

const STATUS_ORDER = { missing: 0, failed: 1, set: 2, valid: 3 };

export class CredentialsSection extends Component {
  state = { loaded: false, secrets: [], triage: null, accounts: [], error: null };
  componentDidMount() {
    activeCredentialsReload = this.load;
    this.unsubLive = onLiveEvent((event) => {
      if (event.kind === "credential.changed" || event.kind.startsWith("inbox.") || event.kind === "reset") {
        this.load();
      }
    });
    this.load();
  }
  componentWillUnmount() {
    if (activeCredentialsReload === this.load) activeCredentialsReload = null;
    this.unsubLive?.();
  }
  load = async () => {
    if (this.loading) {
      this.reloadQueued = true;
      return;
    }
    this.loading = true;
    try {
      const [secretsRes, triageRes, accountsRes] = await Promise.all([
        readJson(SECRETS_API),
        readJson("/api/v1/screens/triage"),
        readJson("/api/v1/accounts"),
      ]);
      if (Array.isArray(secretsRes.data?.secrets)) {
        this.setState({
          loaded: true,
          secrets: secretsRes.data.secrets,
          triage: triageRes.data ?? this.state.triage,
          accounts: Array.isArray(accountsRes.data?.accounts) ? accountsRes.data.accounts : this.state.accounts,
          error: null,
        });
      } else {
        this.setState({
          loaded: true,
          error: secretsRes.error ?? { status: null, message: "Malformed response" },
        });
      }
    } finally {
      this.loading = false;
      if (this.reloadQueued) {
        this.reloadQueued = false;
        this.load();
      }
    }
  };
  afterWrite = async () => {
    await Promise.all([this.load(), refreshSettingsSecrets()]);
    this.props.retry?.();
  };
  render({ now }, { loaded, secrets, triage, accounts, error }) {
    if (!loaded && !error) return html`<${Loading} what="credentials" />`;
    if (error && !secrets.length) {
      return html`<${Fault} what="Credentials" fault=${error} retry=${this.load} />`;
    }
    const rows = buildCredentialRows(secrets, triage, accounts);
    const missing = rows.filter((row) => row.status === "missing");
    const columns = [
      { id: "name", label: "Name", key: "name", cellClass: "mono set-cred-name set-cell-ell", title: (r) => r.name },
      { id: "status", label: "Status", key: "status", render: renderStatusCell },
      { id: "scope", label: "Scope", key: "scope", cellClass: "muted set-cell-ell", title: (r) => r.scope },
      { id: "used_by", label: "Used by", key: "usedByText", cellClass: "muted set-cell-ell", title: (r) => r.usedByText },
      { id: "last_used", label: "Last used", cellClass: "mono muted set-cell-ell", render: (r) => formatAge(r.lastUsed, now) },
      { id: "rotated", label: "Rotated", cellClass: "mono muted", render: (r) => formatDate(r.rotated) },
      { id: "expires", label: "Expires", cellClass: "mono muted", render: (r) => formatDate(r.expires) },
    ];
    const facets = [
      { key: "status", label: "Status", value: (r) => r.status },
      { key: "scope", label: "Scope", value: (r) => r.scope },
    ];
    const sorts = [
      { id: "source", label: "Source order" },
      { id: "name", label: "Name", key: "name" },
      { id: "status", label: "Status", compare: (a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) },
      { id: "last_used", label: "Last used", value: (r) => r.lastUsed || "", direction: -1 },
    ];
    const remove = {
      noun: "credential",
      requireReason: true,
      reasonPlaceholder: "Reason for removing this credential (optional)",
      undoable: false,
      undoNotice: CREDENTIAL_UNDO_NOTICE,
      onRemove: async (row) => {
        try {
          const result = await removeCredential(row.scope, row.name);
          return {
            label: result?.pending ? `Queued removal of ${row.name}` : `Removed credential ${row.name}`,
            undoable: false,
            undoNotice: CREDENTIAL_UNDO_NOTICE,
          };
        } catch (err) {
          throw new Error(describeWriteError(err));
        }
      },
    };
    return html`<div class="set-stack">
      ${error ? html`<${Fault} what="Credentials" fault=${error} retry=${this.load} />` : null}
      <${SettingsTableFrame}
        id="credentials"
        noun="credential"
        tableClass="set-tbl-credentials"
        columns=${columns}
        rows=${rows}
        facets=${facets}
        sorts=${sorts}
        formKind="credential"
        initialSelected=${(list) => list.find((row) => row.status === "missing")?.id ?? list[0]?.id ?? null}
        searchText=${(row) => [row.name, row.status, row.scope, row.usedByText, row.service].filter(Boolean).join(" ")}
        remove=${remove}
        empty="No credentials configured."
        onCreated=${this.afterWrite}
        onRemoved=${this.afterWrite}
        below=${(selected) => html`<div class="set-stack">
          ${missing.map((row) => html`<${NeedsYouBlock} key=${row.id} row=${row} onSaved=${this.afterWrite} />`)}
          ${selected && selected.status !== "missing"
            ? html`<${StoredCredentialBar} key=${selected.id} row=${selected} onUpdated=${this.afterWrite} />`
            : null}
        </div>`}
      />
    </div>`;
  }
}

registerSettingsSection("credentials", ({ now, retry }) => html`<${CredentialsSection} now=${now} retry=${retry} />`);
