import { Component, h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { effect, signal } from "./vendor-signals.js";
import { ListHeader, createList } from "./ui-list.js";
import { selectItem, syncList } from "./ui-keys.js";
import { webFetch } from "./ui-request.js";
import { prepareWrite, sendWrite } from "./ui-write.js";
import { Empty, Fault, Loading } from "./ui-states.js";
import { safeLink } from "./ui-operations.js";
import { onLiveEvent } from "./ui-live.js";

const html = htm.bind(h);

const DENSITY_KEY = "arete:density";
const SOUND_KEY = "arete:sound";
const PUSH_SUB_KEY = "arete:push-sub-id";

export const SECTIONS = [
  ["health", "Health"],
  ["credentials", "Credentials"],
  ["connectors", "Connectors"],
  ["devices", "Devices"],
  ["models", "Models and routing"],
  ["lines", "Lines"],
  ["sources", "Sources"],
  ["attention", "Attention budget"],
  ["outbox rules", "Outbox rules"],
  ["intake rules", "Intake rules"],
  ["sound", "Sound"],
  ["display", "Display and density"],
  ["about", "About"],
];

const SECTION_ALIASES = {
  "outbox-rules": "outbox rules",
  "intake-rules": "intake rules",
};

const STUB_ISSUES = {
  credentials: "#422",
  connectors: "#420",
  devices: "#420",
  models: "#420",
  lines: "#420",
  sources: "#420",
  attention: "#420",
  "outbox rules": "#420",
  "intake rules": "#420",
};

const sectionRenderers = new Map();
const healthBlocks = new Map();

export function registerSettingsSection(id, renderer) {
  sectionRenderers.set(id, renderer);
}

export function registerHealthBlock(id, renderer) {
  healthBlocks.set(id, renderer);
}

export function resolveSection(raw) {
  const normalized = SECTION_ALIASES[raw] ?? raw;
  return SECTIONS.find(([id]) => id === normalized) ?? SECTIONS[0];
}

export const density = signal("compact");
export const sound = signal(false);

const healthState = signal({ data: null, error: null, loaded: false, received: 0 });
const metaState = signal({ data: null, error: null, loaded: false });
const secretsState = signal({ data: null, error: null, loaded: false });
const accountsState = signal({ data: null, error: null, loaded: false });
const triageState = signal({ data: null, error: null, loaded: false });
const settingsState = signal({ data: null, error: null, loaded: false });
const pushState = signal({ data: null, error: null, loaded: false });
const usageState = signal({ data: null, error: null, loaded: false });
const thisDeviceState = signal({ id: "", subId: "" });

function readStorage(key) {
  try {
    return globalThis.localStorage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function writeStorage(key, value) {
  try {
    if (value == null || value === "") globalThis.localStorage?.removeItem(key);
    else globalThis.localStorage?.setItem(key, value);
  } catch {
    return;
  }
}

export function setDensity(value) {
  const next = value === "comfortable" ? "comfortable" : "compact";
  density.value = next;
  try {
    globalThis.document?.documentElement?.setAttribute("data-density", next);
  } catch {
    return;
  }
  writeStorage(DENSITY_KEY, next);
}

export function setSound(on) {
  const next = Boolean(on);
  sound.value = next;
  writeStorage(SOUND_KEY, next ? "on" : "off");
}

export function initSettingsPrefs() {
  const savedDensity = readStorage(DENSITY_KEY);
  const nextDensity = savedDensity === "comfortable" ? "comfortable" : "compact";
  density.value = nextDensity;
  try {
    globalThis.document?.documentElement?.setAttribute("data-density", nextDensity);
  } catch {
    sound.value = readStorage(SOUND_KEY) === "on";
    return;
  }
  sound.value = readStorage(SOUND_KEY) === "on";
  const savedSub = readStorage(PUSH_SUB_KEY);
  if (savedSub) thisDeviceState.value = { ...thisDeviceState.peek(), subId: savedSub };
}

function pushReady() {
  return typeof navigator !== "undefined" && typeof window !== "undefined"
    && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window && window.isSecureContext;
}

function bytesOfBase64url(text) {
  const padded = (text + "=".repeat((4 - (text.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

async function deviceIdOf(endpoint) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint));
  return [...new Uint8Array(digest)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function thisSubscription() {
  const registration = await navigator.serviceWorker.getRegistration();
  return registration ? registration.pushManager.getSubscription() : null;
}

function deviceLabel() {
  const ua = navigator.userAgent;
  const found = [["iPhone", /iPhone/], ["iPad", /iPad/], ["Android", /Android/], ["Mac", /Macintosh/], ["Windows", /Windows/], ["Linux", /Linux/]]
    .find(([, pattern]) => pattern.test(ua));
  return found ? found[0] : "Device";
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return "";
  const value = Math.max(0, Math.floor(seconds));
  if (value >= 86400) return `${Math.floor(value / 86400)}d ${Math.floor((value % 86400) / 3600)}h`;
  if (value >= 3600) return `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`;
  if (value >= 60) return `${Math.floor(value / 60)}m ${value % 60}s`;
  return `${value}s`;
}

function formatStamp(value) {
  if (!value) return "";
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) return String(value);
  return new Date(ms).toLocaleString();
}

async function readJson(url) {
  try {
    const response = await webFetch(url, { cache: "no-store" });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      return { error: { status: response.status, message: body?.detail ?? body?.error ?? `HTTP ${response.status}` } };
    }
    if (!body || typeof body !== "object" || Array.isArray(body)) {
      return { error: { status: response.status, message: "Malformed response" } };
    }
    return { data: body };
  } catch (error) {
    return { error: { status: null, message: error?.message || "Network error" } };
  }
}

async function loadHealth() {
  const res = await readJson("/api/v1/health");
  const prev = healthState.peek();
  if (res.data?.health) {
    healthState.value = { data: res.data.health, error: null, loaded: true, received: Date.now() };
  } else {
    healthState.value = { data: prev.data, error: res.error ?? { status: null, message: "Malformed response" }, loaded: true, received: prev.received };
  }
}

async function loadMeta() {
  const res = await readJson("/api/v1/meta");
  const prev = metaState.peek();
  if (res.data?.server) {
    metaState.value = { data: res.data, error: null, loaded: true };
  } else {
    metaState.value = { data: prev.data, error: res.error ?? { status: null, message: "Malformed response" }, loaded: true };
  }
}

async function loadSecrets() {
  const res = await readJson("/api/v1/secrets");
  const prev = secretsState.peek();
  if (Array.isArray(res.data?.secrets)) {
    secretsState.value = { data: res.data.secrets, error: null, loaded: true };
  } else {
    secretsState.value = { data: prev.data, error: res.error ?? { status: null, message: "Malformed response" }, loaded: true };
  }
}

async function loadAccounts() {
  const res = await readJson("/api/v1/accounts");
  const prev = accountsState.peek();
  if (Array.isArray(res.data?.accounts)) {
    accountsState.value = { data: res.data.accounts, error: null, loaded: true };
  } else {
    accountsState.value = { data: prev.data, error: res.error ?? { status: null, message: "Malformed response" }, loaded: true };
  }
}

async function loadTriage() {
  const res = await readJson("/api/v1/screens/triage");
  const prev = triageState.peek();
  if (res.data?.counts && res.data?.by_priority) {
    triageState.value = { data: res.data, error: null, loaded: true };
  } else {
    triageState.value = { data: prev.data, error: res.error ?? { status: null, message: "Malformed response" }, loaded: true };
  }
}

async function loadSettings() {
  const res = await readJson("/api/v1/settings");
  const prev = settingsState.peek();
  if (res.data?.settings) {
    settingsState.value = { data: res.data.settings, error: null, loaded: true };
  } else {
    settingsState.value = { data: prev.data, error: res.error ?? { status: null, message: "Malformed response" }, loaded: true };
  }
}

async function loadPush() {
  const res = await readJson("/api/v1/push/subscriptions");
  const prev = pushState.peek();
  if (Array.isArray(res.data?.subscriptions)) {
    pushState.value = { data: res.data.subscriptions, error: null, loaded: true };
  } else {
    pushState.value = { data: prev.data, error: res.error ?? { status: null, message: "Malformed response" }, loaded: true };
  }
  if (!pushReady()) return;
  try {
    const sub = await thisSubscription();
    const id = sub ? await deviceIdOf(sub.endpoint) : "";
    const cur = thisDeviceState.peek();
    if (id !== cur.id) thisDeviceState.value = { ...cur, id };
  } catch {
    return;
  }
}

async function loadUsage() {
  const res = await readJson("/api/usage?days=1");
  const prev = usageState.peek();
  if (Array.isArray(res.data?.views) && Array.isArray(res.data?.actions)) {
    usageState.value = { data: res.data, error: null, loaded: true };
  } else {
    usageState.value = { data: prev.data, error: res.error ?? { status: null, message: "Malformed response" }, loaded: true };
  }
}

let secretsRefresh = null;

export function refreshSettingsSecrets() {
  if (!secretsRefresh) {
    secretsRefresh = Promise.all([loadSecrets(), loadTriage(), loadAccounts()]).finally(() => {
      secretsRefresh = null;
    });
  }
  return secretsRefresh;
}

function loadOverview() {
  return Promise.all([loadHealth(), loadMeta(), loadSecrets(), loadAccounts(), loadTriage(), loadSettings(), loadPush()]);
}

function missingSecrets() {
  const secrets = secretsState.value.data;
  return Array.isArray(secrets) ? secrets.filter((item) => !item.present) : [];
}

function connectorSummary() {
  const accounts = accountsState.value.data;
  if (!Array.isArray(accounts)) return null;
  const missingNames = new Set(missingSecrets().map((item) => item.name));
  const disconnected = accounts.filter((account) => account.secret_name && missingNames.has(account.secret_name));
  return { total: accounts.length, connected: accounts.length - disconnected.length, disconnected };
}

function navBadge(id) {
  if (id === "credentials") {
    const missing = missingSecrets();
    if (missing.length) return html`<span class="set-chip set-chip-ny">◆ ${missing.length} MISSING</span>`;
    return null;
  }
  if (id === "connectors") {
    const summary = connectorSummary();
    return summary ? html`<span class="set-nav-meta">${summary.connected}/${summary.total}</span>` : null;
  }
  if (id === "devices") {
    const devices = healthState.value.data?.devices?.rows;
    return Array.isArray(devices) ? html`<span class="set-nav-meta">${devices.length}</span>` : null;
  }
  if (id === "sound") {
    return html`<span class="set-nav-meta">${sound.value ? "on" : "off"}</span>`;
  }
  return null;
}

export class SettingsQueue extends Component {
  constructor(props) {
    super(props);
    this.list = createList({
      rows: SECTIONS.map(([id, title]) => ({ id, title })),
      total: SECTIONS.length,
    });
    this.state = { revision: 0 };
  }
  componentDidMount() {
    this.unsubList = this.list.subscribe((before) => {
      if (!before) this.setState({ revision: this.state.revision + 1 });
    });
    this.unsubSignals = effect(() => {
      sound.value;
      secretsState.value;
      accountsState.value;
      healthState.value;
      this.setState({ revision: this.state.revision + 1 });
    });
    const [activeId] = resolveSection(this.props.route?.path?.[0]);
    selectItem("settings-sections", activeId);
    if (this.base) syncList(this.base);
  }
  componentDidUpdate(prevProps) {
    const [prevId] = resolveSection(prevProps.route?.path?.[0]);
    const [nextId] = resolveSection(this.props.route?.path?.[0]);
    if (prevId !== nextId) selectItem("settings-sections", nextId);
    if (this.base) syncList(this.base);
  }
  componentWillUnmount() {
    this.unsubList?.();
    this.unsubSignals?.();
  }
  render({ route }) {
    const [activeId] = resolveSection(route?.path?.[0]);
    const rows = this.list.rows();
    return html`<div class="ui-list set-nav-list" data-list="settings-sections">
      <${ListHeader} controller=${this.list} queue=${true} id="settings-sections" />
      <div class="ui-list-scroll">
        <nav aria-label="Settings sections">
          ${rows.length ? rows.map((row) => html`
            <a key=${row.id} class="set-nav-row" data-row="" data-item=${row.id}
              href=${`#settings/${encodeURIComponent(row.id)}`}
              aria-current=${row.id === activeId ? "page" : undefined}>
              <span class="set-nav-title">${row.title}</span>
              ${navBadge(row.id)}
            </a>`) : html`<${Empty} what="No matching sections" />`}
        </nav>
      </div>
    </div>`;
  }
}

function SoundSwitch({ on, onToggle }) {
  return html`<button type="button" role="switch" class="set-swt" aria-checked=${on ? "true" : "false"}
    onClick=${() => onToggle(!on)}>
    <span class="set-swt-box" aria-hidden="true"><span class="set-swt-knob"></span></span>
    <span class="mono">${on ? "● ON" : "■ OFF"}</span>
  </button>`;
}

function DensityToggle({ value, verbose = false }) {
  const options = verbose
    ? [["compact", "Compact · 28px rows"], ["comfortable", "Comfortable · 32px rows"]]
    : [["compact", "Compact"], ["comfortable", "Comfortable"]];
  return html`<span class="set-seg" role="group" aria-label="Density">
    ${options.map(([id, label]) => html`<button key=${id} type="button"
      aria-pressed=${value === id ? "true" : "false"}
      onClick=${() => setDensity(id)}>${label}</button>`)}
  </span>`;
}

function routingPreviewRows() {
  const current = settingsState.value.data;
  if (!current) return [];
  const preferred = ["plan", "build", "review"];
  const byRole = new Map((current.roles ?? []).map((entry) => [entry.role, entry]));
  const picked = preferred.filter((name) => byRole.has(name)).map((name) => byRole.get(name));
  const source = picked.length ? picked : (current.roles ?? []).slice(0, 3);
  if (source.length) {
    return source.slice(0, 3).map((entry) => ({
      role: entry.role,
      model: entry.model || current.defaults?.[entry.role] || "default",
      runner: entry.runner || "",
    }));
  }
  return preferred.map((role) => ({
    role,
    model: current.models?.[role] || current.defaults?.[role] || "default",
    runner: "",
  }));
}

function serveIssues(serve) {
  if (!serve) return [];
  const configProblems = Array.isArray(serve.config_problems) && serve.config_problems.length
    ? serve.config_problems : (serve.config_problem ? [serve.config_problem] : []);
  return [
    ...configProblems,
    serve.code_problem,
    ...Object.entries(serve.failing ?? {}).map(([name, info]) => `${name}: ${info.error}`),
    ...Object.entries(serve.poll_errors ?? {}).map(([name, problem]) => `${name}: ${problem}`),
    ...Object.entries(serve.broken ?? {}).map(([name, problem]) => `${name}: ${problem}`),
  ].filter(Boolean);
}

class PushPanel extends Component {
  state = { busy: false, status: null };
  componentDidMount() {
    if (pushReady()) {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    }
  }
  subscribe = async () => {
    if (this.state.busy) return;
    if (!pushReady()) {
      this.setState({ status: { ok: false, text: "This page cannot receive pushes." } });
      return;
    }
    const vapidKey = metaState.peek().data?.push?.vapid_public_key;
    if (!vapidKey) {
      this.setState({ status: { ok: false, text: "Push VAPID public key is unavailable." } });
      return;
    }
    this.setState({ busy: true, status: null });
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        this.setState({ status: { ok: false, text: "Notifications are not allowed for this page, so nothing was subscribed." } });
        return;
      }
      await navigator.serviceWorker.register("/sw.js").catch(() => {});
      const registration = await navigator.serviceWorker.ready;
      const key = bytesOfBase64url(vapidKey);
      let sub = await registration.pushManager.getSubscription();
      const held = sub?.options?.applicationServerKey;
      if (sub && (!held || held.byteLength !== key.length || new Uint8Array(held).some((b, i) => b !== key[i]))) {
        await sub.unsubscribe();
        sub = null;
      }
      sub ||= await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      const json = sub.toJSON();
      const saved = await sendWrite(prepareWrite("/api/v1/push/subscriptions", {
        endpoint: json.endpoint,
        keys: json.keys,
        label: deviceLabel(),
      }));
      const id = await deviceIdOf(json.endpoint);
      const subId = saved?.subscription?.id ?? "";
      thisDeviceState.value = { id, subId };
      if (subId) writeStorage(PUSH_SUB_KEY, subId);
      await loadPush();
      const message = saved?.subscription
        ? "This device will get a push when an ask needs you."
        : "Subscription queued for owner confirmation.";
      this.setState({ status: { ok: true, text: message } });
    } catch (error) {
      this.setState({ status: { ok: false, text: error?.message || "Could not subscribe this device." } });
    } finally {
      this.setState({ busy: false });
    }
  };
  remove = async (subId) => {
    if (this.state.busy) return;
    this.setState({ busy: true, status: null });
    try {
      await sendWrite(prepareWrite(`/api/v1/push/subscriptions/${encodeURIComponent(subId)}`, {}, null, { method: "DELETE" }));
      if (subId === thisDeviceState.peek().subId && pushReady()) {
        (await thisSubscription())?.unsubscribe().catch(() => {});
        thisDeviceState.value = { id: "", subId: "" };
        writeStorage(PUSH_SUB_KEY, "");
      }
      await loadPush();
      this.setState({ status: { ok: true, text: "Removed the device." } });
    } catch (error) {
      this.setState({ status: { ok: false, text: error?.message || "Could not remove the device." } });
    } finally {
      this.setState({ busy: false });
    }
  };
  render(_props, { busy, status }) {
    const ready = pushReady();
    const subs = pushState.value;
    const vapidKey = metaState.value.data?.push?.vapid_public_key;
    const currentSubId = thisDeviceState.value.subId;
    return html`<section class="set-panel" aria-label="Push notifications">
      <div class="set-qh">
        <span class="set-lbl">Push notifications</span>
        <button type="button" class="set-btn" disabled=${busy || !ready || !vapidKey} onClick=${this.subscribe}>
          ${busy ? "Working…" : "Subscribe this device"}
        </button>
      </div>
      ${ready ? null : html`<p class="set-tnote">This page cannot receive pushes. On an iPhone, open the dashboard over HTTPS in Safari, add it to the Home Screen and open it from there.</p>`}
      ${status ? html`<p class=${`set-status${status.ok ? "" : " set-status-err"}`} role="status">${status.text}</p>` : null}
      ${subs.error ? html`<${Fault} what="Push subscriptions" fault=${subs.error} retry=${loadPush} />`
        : !subs.loaded ? html`<${Loading} what="push subscriptions" />`
          : subs.data?.length ? html`<div class="set-table-wrap"><table class="set-tbl">
            <thead><tr><th>Device</th><th>Endpoint</th><th>Added</th><th>Last push</th><th class="set-ra"></th></tr></thead>
            <tbody>${subs.data.map((entry) => html`<tr key=${entry.id}>
              <td>${entry.id === currentSubId ? `${entry.label} (this device)` : entry.label}</td>
              <td class="mono muted">${entry.endpoint_host}</td>
              <td class="mono muted">${formatStamp(entry.created)}</td>
              <td class="mono muted">${entry.last_success ? formatStamp(entry.last_success) : "never"}</td>
              <td class="set-ra"><button type="button" class="set-btn set-btn-ghost" disabled=${busy}
                onClick=${() => this.remove(entry.id)}>Remove</button></td>
            </tr>`)}</tbody>
          </table></div>` : html`<p class="set-tnote">No device is subscribed.</p>`}
    </section>`;
  }
}

class HealthSection extends Component {
  state = { checksBusy: false, checksStatus: null, installBusy: false, installStatus: null };
  runChecks = async () => {
    if (this.state.checksBusy) return;
    this.setState({ checksBusy: true, checksStatus: null });
    try {
      const result = await sendWrite(prepareWrite("/api/v1/health/check", {}));
      await loadHealth();
      this.props.retry?.();
      this.setState({ checksStatus: {
        ok: Boolean(result.ok),
        text: result.ok ? "Checks passed." : `Checks finished with exit code ${result.code ?? 1}.`,
      } });
    } catch (error) {
      this.setState({ checksStatus: { ok: false, text: error?.message || "Failed to run checks." } });
    } finally {
      this.setState({ checksBusy: false });
    }
  };
  installServe = async () => {
    if (this.state.installBusy) return;
    this.setState({ installBusy: true, installStatus: null });
    try {
      const result = await sendWrite(prepareWrite("/api/v1/serve/install", {}));
      await loadHealth();
      this.props.retry?.();
      const text = result.restarting ? "Serve is restarting to install." : (result.said || "Serve installed.");
      this.setState({ installStatus: { ok: true, text } });
    } catch (error) {
      this.setState({ installStatus: { ok: false, text: error?.message || "Failed to install serve." } });
    } finally {
      this.setState({ installBusy: false });
    }
  };
  render({ now, retry }, { checksBusy, checksStatus, installBusy, installStatus }) {
    const hs = healthState.value;
    const health = hs.data;
    const meta = metaState.value.data;
    const secrets = secretsState.value;
    const accounts = accountsState.value;
    const triage = triageState.value;
    const reload = () => { loadOverview(); retry?.(); };

    if (!health && hs.error) return html`<${Fault} what="Health" fault=${hs.error} retry=${reload} />`;
    if (!health) return html`<${Loading} what="Health" />`;

    const serve = health.serve ?? {};
    const startedMs = serve.started ? Date.parse(serve.started) : NaN;
    const uptime = Number.isFinite(startedMs) ? formatDuration((now - startedMs) / 1000) : "";
    const beatAge = Number.isFinite(serve.age_seconds)
      ? formatDuration(serve.age_seconds + Math.max(0, now - hs.received) / 1000) : "";
    const version = meta?.server?.version || serve.release || serve.engine || "";
    const host = globalThis.location?.host ?? "";
    const url = globalThis.location?.origin ?? "";
    const issues = serveIssues(serve);

    const checksPanel = health.checks ?? {};
    const checkRows = checksPanel.rows?.checks ?? [];
    const passedChecks = checkRows.filter((c) => c.ok).length;
    const totalChecks = checkRows.length;
    const checksRanAt = checksPanel.rows?.heartbeat ?? "";
    const checksRunning = Boolean(checksPanel.rows?.running);
    const alertRows = health.alerts?.rows?.alerts ?? [];
    const heldWorkspaces = health.workspaces?.rows ?? null;
    const incidentRows = health.incidents?.rows ?? [];

    const missing = missingSecrets();
    const connectors = connectorSummary();
    const devices = health.devices?.rows ?? [];
    const devicesUp = devices.filter((d) => d.probe?.ok).length;
    const devicesFault = devices.filter((d) => d.probe && !d.probe.ok).length;
    const devicesUnprobed = devices.filter((d) => !d.probe).length;
    const triageCounts = triage.data?.counts ?? null;
    const triageHigh = triage.data?.by_priority?.high?.length ?? 0;
    const triageMed = triage.data?.by_priority?.med?.length ?? 0;
    const routingRows = routingPreviewRows();

    return html`<div class="set-stack">
      ${hs.error ? html`<${Fault} what="Health" fault=${hs.error} retry=${reload} />` : null}
      <div class="set-hgrid">
        <section class="set-panel" aria-label="Serve">
          <div class="set-lbl set-tile-lbl">Serve</div>
          <div class="set-kv">
            <span class="set-lbl">State</span>
            <span class="set-kv-v"><span class=${`set-st ${serve.alive ? "set-st-up" : "set-st-fault"}`}>${serve.alive ? "● UP" : serve.at ? "■ DOWN" : "■ NOT RUNNING"}</span>${uptime ? ` ${uptime}` : ""}</span>
            <span class="set-lbl">Version</span>
            <span class="set-kv-v">${version || "unknown"}</span>
            <span class="set-lbl">Host</span>
            <span class="set-kv-v">${host}</span>
            <span class="set-lbl">URL</span>
            <span class="set-kv-v">${url}</span>
            <span class="set-lbl">Beat</span>
            <span class="set-kv-v">${beatAge ? `${beatAge} ago` : "none"}</span>
          </div>
          ${issues.map((issue, idx) => html`<p key=${idx} class="set-status set-status-err">${issue}</p>`)}
          <div class="set-acts">
            <button type="button" class="set-btn" disabled=${installBusy} onClick=${this.installServe}>
              ${installBusy ? "Installing serve…" : "Install serve"}
            </button>
          </div>
          ${installStatus ? html`<p class=${`set-status${installStatus.ok ? "" : " set-status-err"}`} role="status">${installStatus.text}</p>` : null}
        </section>

        <section class="set-panel" aria-label="Checks">
          <div class="set-lbl set-tile-lbl">Checks</div>
          ${checksPanel.error ? html`<${Fault} what="Checks" fault=${{ message: checksPanel.error }} retry=${reload} />` : html`
            <div class="set-tval">
              <span class=${`set-st ${passedChecks === totalChecks ? "set-st-ok" : "set-st-fault"}`}>${passedChecks === totalChecks ? "✓" : "×"}</span>
              <span>${passedChecks}/${totalChecks}</span>
            </div>
            <div class="set-tnote">${checksRanAt ? `ran ${formatStamp(checksRanAt)}` : "never run"}${checksRunning ? " · running" : ""}</div>
            <div class="set-acts">
              <button type="button" class="set-btn" disabled=${checksBusy || checksRunning} onClick=${this.runChecks}>
                ${checksBusy || checksRunning ? "Running checks…" : "Run checks"}
              </button>
            </div>
            ${checksStatus ? html`<p class=${`set-status${checksStatus.ok ? "" : " set-status-err"}`} role="status">${checksStatus.text}</p>` : null}
          `}
          <div class="set-lbl set-sub-lbl">Alerts</div>
          ${health.alerts?.error ? html`<p class="set-status set-status-err">${health.alerts.error}</p>`
            : alertRows.length ? alertRows.map((alert) => {
              const href = safeLink(alert.url);
              return html`<div key=${alert.key} class="set-line"><span class="set-st set-st-fault">▲ ALERT</span> ${alert.key}${alert.detail ? ` · ${alert.detail}` : ""}${href ? html` <a class="set-link mono" href=${href} target="_blank" rel="noopener noreferrer">${alert.repo || "link"}</a>` : null}</div>`;
            }) : html`<div class="set-tnote">none</div>`}
          <div class="set-lbl set-sub-lbl">Holds</div>
          ${health.workspaces?.error ? html`<p class="set-status set-status-err">${health.workspaces.error}</p>`
            : heldWorkspaces?.count ? html`<div class="set-line">${heldWorkspaces.count} held · ${(heldWorkspaces.rows ?? []).map((w) => w.name).join(", ")}</div>`
              : html`<div class="set-tnote">none</div>`}
        </section>

        <section class=${`set-panel${missing.length ? " set-panel-ny" : ""}`} aria-label="Credentials">
          <div class="set-lbl set-tile-lbl">Credentials</div>
          ${secrets.error ? html`<p class="set-status set-status-err">${secrets.error.message}</p>`
            : !secrets.loaded ? html`<div class="set-tnote">Loading credentials…</div>`
              : missing.length ? html`
                <div class="set-line-row">
                  <span class="set-chip set-chip-ny">◆ NEEDS YOU</span>
                  <span class="muted">${missing.length} missing</span>
                  <span class="mono">${missing.map((item) => item.name).join(", ")}</span>
                </div>
                <div class="set-acts"><a class="set-btn" href="#settings/credentials">Open the setting</a></div>
              ` : html`
                <div class="mono"><span class="set-st set-st-ok">✓</span> 0 missing · ${secrets.data.length} stored</div>
                <div class="set-acts"><a class="set-link" href="#settings/credentials">Open credentials ›</a></div>
              `}
        </section>

        <a class="set-panel set-panel-link" href="#settings/connectors" aria-label="Connectors">
          <div class="set-lbl set-tile-lbl">Connectors</div>
          ${accounts.error ? html`<p class="set-status set-status-err">${accounts.error.message}</p>`
            : !connectors ? html`<div class="set-tnote">Loading connectors…</div>` : html`
              <div class="mono">${connectors.connected} of ${connectors.total} connected</div>
              <div class="set-tnote">
                ${connectors.disconnected.length
                  ? connectors.disconnected.map((entry) => `◆ ${entry.service}: missing ${entry.secret_name}`).join(", ")
                  : connectors.total ? accounts.data.map((entry) => entry.service).join(", ") : "No connectors configured"}
              </div>
            `}
        </a>

        <a class="set-panel set-panel-link" href="#settings/devices" aria-label="Devices">
          <div class="set-lbl set-tile-lbl">Devices</div>
          ${health.devices?.error ? html`<p class="set-status set-status-err">${health.devices.error}</p>` : html`
            <div class="mono">${devicesUp} up${devicesFault ? ` · ${devicesFault} fault` : ""}${devicesUnprobed ? ` · ${devicesUnprobed} not probed` : ""}</div>
            <div class="set-tnote">${devices.length ? devices.map((device) => device.name).join(", ") : "No devices configured"}</div>
          `}
        </a>

        <a class="set-panel set-panel-link" href="#settings/attention" aria-label="Attention">
          <div class="set-lbl set-tile-lbl">Attention</div>
          ${triage.error ? html`<p class="set-status set-status-err">${triage.error.message}</p>`
            : !triageCounts ? html`<div class="set-tnote">Loading attention…</div>` : html`
              <div class="mono">${triageCounts.open ?? 0} standing · ${triageHigh} high · ${triageMed} med</div>
              <div class="set-tnote">${triageCounts.snoozed ?? 0} snoozed · ${triageCounts.shelved ?? 0} shelved${incidentRows.length ? ` · ${incidentRows.length} blocked` : ""}</div>
            `}
        </a>
      </div>

      ${[...healthBlocks.entries()].map(([blockId, renderBlock]) => html`<div key=${blockId}>${renderBlock({ health, now, retry: reload })}</div>`)}

      <div class="set-lbl set-sec-lbl">Quick settings</div>
      <div class="set-qgrid">
        <section class="set-panel" aria-label="Sound quick setting">
          <div class="set-qh"><span class="set-lbl">Sound</span><a class="set-link mono" href="#settings/sound">Sound ›</a></div>
          <${SoundSwitch} on=${sound.value} onToggle=${setSound} />
          <div class="set-tnote">Stored in this browser only</div>
        </section>

        <section class="set-panel" aria-label="Density quick setting">
          <div class="set-qh"><span class="set-lbl">Density</span><a class="set-link mono" href="#settings/display">Display ›</a></div>
          <${DensityToggle} value=${density.value} />
          <div class="set-tnote">${density.value === "compact" ? "28px rows" : "32px rows"} · dark theme · no motion</div>
        </section>

        <section class="set-panel" aria-label="Model routing quick setting">
          <div class="set-qh"><span class="set-lbl">Model routing</span><a class="set-link mono" href="#settings/models">Models ›</a></div>
          ${settingsState.value.error ? html`<p class="set-status set-status-err">${settingsState.value.error.message}</p>`
            : !settingsState.value.loaded ? html`<div class="set-tnote">Loading routing…</div>`
              : html`<table class="set-qtbl"><tbody>${routingRows.map((row) => html`<tr key=${row.role}>
                <td class="set-lbl">${row.role}</td>
                <td class="mono">${row.model}</td>
                <td class="mono muted">${row.runner}</td>
              </tr>`)}</tbody></table>`}
        </section>
      </div>

      <${PushPanel} />
    </div>`;
  }
}

function DisplaySection() {
  return html`<section class="set-panel" aria-label="Display and density">
    <div class="set-kv">
      <span class="set-lbl">Density</span>
      <span class="set-kv-v"><${DensityToggle} value=${density.value} verbose=${true} /></span>
      <span class="set-lbl">Theme</span>
      <span class="set-kv-v">dark</span>
      <span class="set-lbl">Motion</span>
      <span class="set-kv-v">none, by design</span>
    </div>
  </section>`;
}

function SoundSection() {
  return html`<section class="set-panel set-sound-bar" aria-label="Sound">
    <${SoundSwitch} on=${sound.value} onToggle=${setSound} />
    <span class="muted">Stored in this browser only. Turning sound on or off never posts to the server.</span>
  </section>`;
}

function AboutSection() {
  const meta = metaState.value;
  const usage = usageState.value;
  const server = meta.data?.server ?? null;
  const rows = server ? [
    ["NAME", "arete"],
    ["VERSION", server.version ?? ""],
    ["COMMIT", server.engine_sha ?? ""],
    ["STARTED", server.started ?? ""],
    ["PYTHON", server.python ?? ""],
    ["STATEDIR", server.state_dir ?? ""],
    ["CONFIGFILE", server.config_file ?? ""],
  ] : [];
  const views = usage.data?.views ?? [];
  const actions = usage.data?.actions ?? [];
  const totalVisits = views.reduce((sum, entry) => sum + (entry.visits ?? 0), 0);
  const totalActions = actions.reduce((sum, entry) => sum + (entry.count ?? 0), 0);

  return html`<div class="set-stack">
    <section class="set-panel" aria-label="About Arete">
      ${meta.error && !server ? html`<${Fault} what="Server metadata" fault=${meta.error} retry=${loadMeta} />`
        : !server ? html`<${Loading} what="server metadata" />`
          : html`<div class="set-kv">${rows.map(([label, value]) => html`
            <span key=${`${label}-k`} class="set-lbl">${label}</span>
            <span key=${`${label}-v`} class="set-kv-v">${value}</span>`)}</div>`}
    </section>

    <section class="set-panel" aria-label="Usage today">
      <div class="set-qh">
        <span class="set-lbl">Usage today</span>
        ${usage.data ? html`<span class="mono muted">${totalVisits} visits · ${totalActions} actions</span>` : null}
      </div>
      ${usage.error && !usage.data ? html`<${Fault} what="Usage" fault=${usage.error} retry=${loadUsage} />`
        : !usage.data ? html`<${Loading} what="usage" />`
          : html`<div class="set-stack">
            <div>
              <div class="set-lbl">Views</div>
              ${views.length ? html`<div class="set-table-wrap"><table class="set-tbl">
                <thead><tr><th>View</th><th class="set-num">Visits</th><th class="set-num">Desktop</th><th class="set-num">Phone</th><th class="set-num">Time</th></tr></thead>
                <tbody>${views.map((entry) => html`<tr key=${entry.view}>
                  <td class="mono">${entry.view}</td>
                  <td class="set-num">${entry.visits}</td>
                  <td class="set-num">${entry.desktop}</td>
                  <td class="set-num">${entry.phone}</td>
                  <td class="set-num">${formatDuration(Math.round((entry.ms ?? 0) / 1000))}</td>
                </tr>`)}</tbody>
              </table></div>` : html`<p class="set-tnote">No visits recorded today.</p>`}
            </div>
            <div>
              <div class="set-lbl">Actions</div>
              ${actions.length ? html`<div class="set-table-wrap"><table class="set-tbl">
                <thead><tr><th>Action</th><th class="set-num">Count</th><th class="set-num">Desktop</th><th class="set-num">Phone</th></tr></thead>
                <tbody>${actions.map((entry) => html`<tr key=${entry.name}>
                  <td class="mono">${entry.name}</td>
                  <td class="set-num">${entry.count}</td>
                  <td class="set-num">${entry.desktop}</td>
                  <td class="set-num">${entry.phone}</td>
                </tr>`)}</tbody>
              </table></div>` : html`<p class="set-tnote">No actions recorded today.</p>`}
            </div>
          </div>`}
    </section>
  </div>`;
}

function StubSection({ id, title }) {
  const issue = STUB_ISSUES[id] ?? "#420";
  return html`<section class="set-panel set-stub" aria-label=${title}>
    <p class="set-line">${title} is planned for ${issue}.</p>
    <div class="set-acts">
      <a class="set-link" href="/#settings">Configuration in classic</a>
      <a class="set-link" href="/#health">Health controls in classic</a>
      <a class="set-link" href="/#incidents">Incidents in classic</a>
    </div>
  </section>`;
}

export class SettingsFocus extends Component {
  state = { revision: 0 };
  componentDidMount() {
    this.unsub = effect(() => {
      density.value;
      sound.value;
      healthState.value;
      metaState.value;
      secretsState.value;
      accountsState.value;
      triageState.value;
      settingsState.value;
      pushState.value;
      usageState.value;
      thisDeviceState.value;
      this.setState({ revision: this.state.revision + 1 });
    });
    this.unsubLive = onLiveEvent((event) => {
      if (event.kind === "credential.changed" || event.kind.startsWith("inbox.") || event.kind === "reset") {
        refreshSettingsSecrets();
      }
    });
    loadOverview();
    const [sectionId] = resolveSection(this.props.route?.path?.[0]);
    if (sectionId === "about") loadUsage();
  }
  componentDidUpdate(prevProps) {
    const [prevId] = resolveSection(prevProps.route?.path?.[0]);
    const [nextId] = resolveSection(this.props.route?.path?.[0]);
    if (prevId !== nextId) {
      if (nextId === "health") loadOverview();
      if (nextId === "about") {
        if (!metaState.peek().data) loadMeta();
        loadUsage();
      }
    } else if (prevProps.resources?.health && prevProps.resources.health !== this.props.resources?.health) {
      loadHealth();
    }
  }
  componentWillUnmount() {
    this.unsub?.();
    this.unsubLive?.();
  }
  render({ route, now, retry }) {
    const [id, title] = resolveSection(route?.path?.[0]);
    const registered = sectionRenderers.get(id);
    let content;
    if (registered) {
      content = registered({ section: id, title, route, now, retry });
    } else if (id === "health") {
      content = html`<${HealthSection} now=${now} retry=${retry} />`;
    } else if (id === "display") {
      content = html`<${DisplaySection} />`;
    } else if (id === "sound") {
      content = html`<${SoundSection} />`;
    } else if (id === "about") {
      content = html`<${AboutSection} />`;
    } else {
      content = html`<${StubSection} id=${id} title=${title} />`;
    }
    return html`<div class="set-wrap" data-settings-section=${id}>
      <h2 class="set-title">${title}</h2>
      ${content}
    </div>`;
  }
}
