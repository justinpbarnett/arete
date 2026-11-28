import { Component, h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { effect, signal } from "./vendor-signals.js";
import * as data from "./ui-data.js";
import { webFetch } from "./ui-request.js";
import { prepareWrite, sendWrite } from "./ui-write.js";
import { failed, notice } from "./ui-notice.js";
import { registerSubjectRenderer } from "./ui-triage.js";
import { registerHealthBlock } from "./ui-settings.js";

const html = htm.bind(h);

export const INCIDENTS_API = "/api/incidents/";
export const INCIDENTS_LIST_API = "/api/incidents";
export const INCIDENTS_SWITCH_API = "/api/incidents/switch";

const healthIncidentsState = signal({
  loaded: false,
  watcher: null,
  incidents: [],
  inboxByIncident: {},
  error: "",
  busyId: "",
  switchBusy: false,
  switchStatus: "",
  rowStatus: {},
});

export function incidentIdOf(item) {
  const subject = String(item?.subject || "");
  if (subject.startsWith("incident:")) return subject.slice("incident:".length);
  if (item?.detail?.id != null) return String(item.detail.id);
  return "";
}

export function formatCost(paid) {
  const value = Number(paid);
  if (!Number.isFinite(value)) return "$0.00";
  return `$${value.toFixed(2)}`;
}

export function latestRepairRun(incident) {
  const repairs = Array.isArray(incident?.repairs) ? incident.repairs : [];
  for (let i = repairs.length - 1; i >= 0; i -= 1) {
    const runs = Array.isArray(repairs[i]?.runs) ? repairs[i].runs : [];
    if (runs.length) return runs[runs.length - 1];
  }
  return null;
}

export async function retryIncident(incidentId, inboxItem) {
  if (inboxItem) {
    return sendWrite(prepareWrite(data.inboxResolve(inboxItem.id), { verb: "retry" }, { version: inboxItem.version }));
  }
  return sendWrite(prepareWrite(`${INCIDENTS_API}${encodeURIComponent(incidentId)}/retry`, {}));
}

export async function dismissIncident(incidentId, inboxItem) {
  if (inboxItem) {
    return sendWrite(prepareWrite(data.inboxResolve(inboxItem.id), { verb: "dismiss" }, { version: inboxItem.version }));
  }
  return sendWrite(prepareWrite(`${INCIDENTS_API}${encodeURIComponent(incidentId)}/dismiss`, {}));
}

export async function cancelIncident(incidentId) {
  return sendWrite(prepareWrite(`${INCIDENTS_API}${encodeURIComponent(incidentId)}/cancel`, {}));
}

export async function switchIncidentWatcher(on) {
  return sendWrite(prepareWrite(INCIDENTS_SWITCH_API, { on: Boolean(on) }));
}

export async function loadHealthIncidents() {
  try {
    const [incRes, inboxRes] = await Promise.all([
      webFetch(INCIDENTS_LIST_API, { cache: "no-store" }),
      webFetch("/api/v1/inbox?state=open&kind=alert", { cache: "no-store" }),
    ]);
    const incBody = await incRes.json().catch(() => null);
    const inboxBody = await inboxRes.json().catch(() => null);
    if (!incRes.ok || !incBody || typeof incBody !== "object") {
      throw new Error(incBody?.detail ?? incBody?.error ?? `HTTP ${incRes.status}`);
    }
    const inboxByIncident = {};
    for (const item of Array.isArray(inboxBody?.items) ? inboxBody.items : []) {
      const id = incidentIdOf(item);
      if (id) inboxByIncident[id] = item;
    }
    const incidents = (Array.isArray(incBody.incidents) ? incBody.incidents : [])
      .filter((row) => row.state === "open");
    const prev = healthIncidentsState.peek();
    healthIncidentsState.value = {
      ...prev,
      loaded: true,
      watcher: incBody.watcher ?? null,
      incidents,
      inboxByIncident,
      error: "",
    };
  } catch (error) {
    const prev = healthIncidentsState.peek();
    healthIncidentsState.value = {
      ...prev,
      loaded: true,
      error: error?.message || "Could not load incidents",
    };
  }
}

class IncidentTriageRenderer extends Component {
  state = { detail: null, loading: true, actionBusy: false, statusText: "", statusErr: false };

  componentDidMount() {
    this.loadDetail();
  }

  async loadDetail() {
    const incId = incidentIdOf(this.props.item);
    if (!incId) {
      this.setState({ loading: false });
      return;
    }
    try {
      const res = await webFetch(`${INCIDENTS_API}${encodeURIComponent(incId)}`, { cache: "no-store" });
      const body = await res.json().catch(() => null);
      if (res.ok && body?.incident) {
        this.setState({ detail: body.incident, loading: false });
      } else {
        this.setState({ loading: false });
      }
    } catch {
      this.setState({ loading: false });
    }
  }

  doRetry = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || this.state.actionBusy) return;
    if (!Array.isArray(item.verbs) || !item.verbs.includes("retry")) return;
    this.setState({ actionBusy: true, statusText: "", statusErr: false });
    try {
      const res = await helpers.resolve("retry", {}, { label: "Retried" });
      if (res) {
        this.setState({ statusText: "Retried", statusErr: false });
        helpers.setInlineStatus?.("Retried", false);
        loadHealthIncidents();
      }
    } finally {
      this.setState({ actionBusy: false });
    }
  };

  doDismiss = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || this.state.actionBusy) return;
    if (!Array.isArray(item.verbs) || !item.verbs.includes("dismiss")) return;
    this.setState({ actionBusy: true, statusText: "", statusErr: false });
    try {
      const res = await helpers.resolve("dismiss", {}, { label: "Dismissed" });
      if (res) {
        this.setState({ statusText: "Dismissed", statusErr: false });
        helpers.setInlineStatus?.("Dismissed", false);
        loadHealthIncidents();
      }
    } finally {
      this.setState({ actionBusy: false });
    }
  };

  doCancel = async () => {
    const { item, busy, helpers } = this.props;
    const incId = incidentIdOf(item);
    if (!incId || busy || this.state.actionBusy) return;
    this.setState({ actionBusy: true, statusText: "", statusErr: false });
    try {
      await cancelIncident(incId);
      this.setState({ statusText: "Cancelled", statusErr: false });
      helpers.setInlineStatus?.("Cancelled", false);
      notice.value = "Cancelled";
      helpers.advance();
      await helpers.refresh();
      loadHealthIncidents();
    } catch (error) {
      this.setState({ statusText: error?.message || "Cancel failed", statusErr: true });
      failed(error);
    } finally {
      this.setState({ actionBusy: false });
    }
  };

  render({ item, busy, helpers, registerItemKeys }, { detail, actionBusy, statusText, statusErr }) {
    const d = item.detail ?? {};
    const inc = detail ?? d;
    const incId = incidentIdOf(item);
    const repairRun = latestRepairRun(detail);
    const cost = detail?.paid ?? d.paid ?? 0;
    const verbs = Array.isArray(item.verbs) ? item.verbs : [];
    const canRetry = verbs.includes("retry");
    const canDismiss = verbs.includes("dismiss");
    const disabled = busy || actionBusy;

    const hints = [];
    if (canRetry) hints.push([["Cmd Enter", "⇧r"], "retry"]);
    if (canDismiss) hints.push([["dd"], "dismiss"]);
    if (repairRun?.id) hints.push([["gx"], "open run"]);

    registerItemKeys({
      itemId: item.id,
      submit: canRetry ? () => this.doRetry() : null,
      retry: canRetry ? () => this.doRetry() : null,
      destructive: canDismiss ? () => this.doDismiss() : null,
      openLink: repairRun?.id ? () => helpers.openHash(data.runHash(repairRun.id)) : null,
      hints,
    });

    return html`
      <div class="tr-sec">
        <div class="tr-sec-head"><span class="tr-lbl">Blocked incident #${incId}</span></div>
        <div class="tr-panel tr-pad">
          <div class="tr-kv">
            <span class="tr-lbl">Signature</span>
            <span class="tr-kv-v">${inc.signature || item.title}</span>
            <span class="tr-lbl">Class</span>
            <span class="tr-kv-v">${inc.class || "—"}</span>
            <span class="tr-lbl">Source</span>
            <span class="tr-kv-v">${inc.source || "—"}</span>
            <span class="tr-lbl">Waiting</span>
            <span class="tr-kv-v">${inc.waiting || "blocked"}</span>
            <span class="tr-lbl">Repair run</span>
            <span class="tr-kv-v">${repairRun?.id
              ? html`<a class="tr-link" href=${data.runHash(repairRun.id)}>${repairRun.id}</a>${repairRun.status ? ` · ${repairRun.status}` : ""}`
              : "none"}</span>
            <span class="tr-lbl">Cost</span>
            <span class="tr-kv-v">${formatCost(cost)}</span>
          </div>
        </div>
      </div>
      <div class="tr-acts">
        ${canRetry ? html`<button type="button" class="tr-btn tr-btn-primary" disabled=${disabled} onClick=${this.doRetry}>
          Retry <kbd class="tr-cap">⇧r</kbd>
        </button>` : null}
        ${canDismiss ? html`<button type="button" class="tr-btn" disabled=${disabled} onClick=${this.doDismiss}>
          Dismiss <kbd class="tr-cap">dd</kbd>
        </button>` : null}
        <button type="button" class="tr-btn tr-btn-ghost" disabled=${disabled} onClick=${this.doCancel}>
          Cancel
        </button>
      </div>
      ${statusText ? html`<p class=${`tr-status${statusErr ? " tr-status-err" : ""}`} role="status">${statusText}</p>` : null}
    `;
  }
}

class IncidentsHealthPanel extends Component {
  state = { rev: 0 };

  componentDidMount() {
    this.unsub = effect(() => {
      healthIncidentsState.value;
      this.setState({ rev: this.state.rev + 1 });
    });
    loadHealthIncidents();
  }

  componentWillUnmount() {
    this.unsub?.();
  }

  toggleWatcher = async (nextOn) => {
    const cur = healthIncidentsState.peek();
    if (cur.switchBusy) return;
    healthIncidentsState.value = { ...cur, switchBusy: true, switchStatus: "" };
    try {
      await switchIncidentWatcher(nextOn);
      const word = nextOn ? "Wakes and rechecks are on." : "Wakes and rechecks are off.";
      notice.value = word;
      await loadHealthIncidents();
      this.props.retry?.();
      healthIncidentsState.value = { ...healthIncidentsState.peek(), switchBusy: false, switchStatus: word };
    } catch (error) {
      failed(error);
      healthIncidentsState.value = {
        ...healthIncidentsState.peek(),
        switchBusy: false,
        switchStatus: error?.message || "Failed to switch watcher",
      };
    }
  };

  act = async (incident, kind) => {
    const id = String(incident.id);
    const cur = healthIncidentsState.peek();
    if (cur.busyId) return;
    const inboxItem = cur.inboxByIncident[id] ?? null;
    healthIncidentsState.value = { ...cur, busyId: id };
    try {
      let label = "";
      if (kind === "retry") {
        await retryIncident(id, inboxItem);
        label = "Retried";
      } else if (kind === "dismiss") {
        await dismissIncident(id, inboxItem);
        label = "Dismissed";
      } else {
        await cancelIncident(id);
        label = "Cancelled";
      }
      notice.value = `${label} incident #${id}`;
      const next = healthIncidentsState.peek();
      healthIncidentsState.value = {
        ...next,
        busyId: "",
        rowStatus: { ...next.rowStatus, [id]: { text: label, error: false } },
      };
      await loadHealthIncidents();
      this.props.retry?.();
    } catch (error) {
      failed(error);
      const next = healthIncidentsState.peek();
      healthIncidentsState.value = {
        ...next,
        busyId: "",
        rowStatus: { ...next.rowStatus, [id]: { text: error?.message || "Action failed", error: true } },
      };
    }
  };

  render() {
    const st = healthIncidentsState.value;
    const watcher = st.watcher;
    const watcherOn = watcher?.state === "on" || watcher?.state === "active" || (watcher != null && watcher.state !== "switched_off" && watcher.state !== "disabled");
    const watcherDisabled = watcher?.state === "disabled";

    return html`<section class="set-panel" aria-label="Blocked incidents">
      <div class="tr-inc-head">
        <span class="set-lbl">Incidents</span>
        ${watcher && !watcherDisabled ? html`<button type="button" role="switch" class="set-swt"
          aria-checked=${watcherOn ? "true" : "false"} disabled=${st.switchBusy}
          onClick=${() => this.toggleWatcher(!watcherOn)}>
          <span class="set-swt-box" aria-hidden="true"><span class="set-swt-knob"></span></span>
          <span class="mono">${watcherOn ? "● WATCHER ON" : "■ WATCHER OFF"}</span>
        </button>` : watcherDisabled ? html`<span class="set-tnote">Watcher disabled in config</span>` : null}
      </div>
      ${st.switchStatus ? html`<p class="set-status" role="status">${st.switchStatus}</p>` : null}
      ${st.error ? html`<p class="set-status set-status-err" role="alert">${st.error}</p>` : null}
      ${!st.loaded ? html`<p class="set-tnote">Loading incidents…</p>`
        : st.incidents.length ? html`<div class="set-table-wrap"><table class="set-tbl">
          <thead>
            <tr>
              <th>Incident</th>
              <th>State</th>
              <th>Repair</th>
              <th class="set-ra">Actions</th>
            </tr>
          </thead>
          <tbody>
            ${st.incidents.map((inc) => {
              const id = String(inc.id);
              const rowBusy = st.busyId === id;
              const status = st.rowStatus[id];
              const inboxItem = st.inboxByIncident[id] ?? null;
              return html`<tr key=${id}>
                <td>
                  ${inboxItem
                    ? html`<a class="set-link mono" href=${data.triageHash(inboxItem.id)}>#${id}</a>`
                    : html`<span class="mono">#${id}</span>`}
                  ${" "}${inc.signature || ""}
                  ${status ? html`<div class=${`tr-inc-msg${status.error ? " set-status-err" : ""}`} role="status">${status.text}</div>` : null}
                </td>
                <td class="mono">${inc.phase || inc.state}</td>
                <td class="mono muted">${inc.repair_line || (inc.waiting || "—")}</td>
                <td class="set-ra">
                  <span class="tr-inc-acts">
                    <button type="button" class="set-btn" disabled=${rowBusy} onClick=${() => this.act(inc, "retry")}>Retry</button>
                    <button type="button" class="set-btn set-btn-ghost" disabled=${rowBusy} onClick=${() => this.act(inc, "dismiss")}>Dismiss</button>
                    <button type="button" class="set-btn set-btn-ghost" disabled=${rowBusy} onClick=${() => this.act(inc, "cancel")}>Cancel</button>
                  </span>
                </td>
              </tr>`;
            })}
          </tbody>
        </table></div>` : html`<p class="set-tnote">No open incidents.</p>`}
    </section>`;
  }
}

export function IncidentsHealthBlock({ retry }) {
  return html`<${IncidentsHealthPanel} retry=${retry} />`;
}

registerSubjectRenderer("incident:", IncidentTriageRenderer);
registerHealthBlock("incidents", IncidentsHealthBlock);
