import { h, Component } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { createList, ListHeader } from "./ui-list.js";
import { syncList, selectItem, pane, activatePane } from "./ui-keys.js";
import { Empty, Fault, Loading } from "./ui-states.js";
import { dispatch, actions, runContext, runPageContext, repairThisRun, step as retryStep } from "./ui-actions.js";
import { register } from "./ui-palette.js";
import { webFetch } from "./ui-request.js";
import { notice } from "./ui-notice.js";
import { parse } from "./ui-router.js";
import { runHash as hashOf, taskHash } from "./ui-data.js";

const html = htm.bind(h);
export const GROUPS = ["Running", "Recovering", "Waiting on you", "Fault", "Done"];
const GLYPHS = { Running: "▷", Recovering: "▷", "Waiting on you": "◆", Fault: "×", Done: "✓" };
const FAULTS = ["failed", "interrupted", "stopped"];
const STEP_STATES = { running: "▷ RUNNING", pass: "✓ PASS", fail: "× FAIL", ask: "◆ ASK", stop: "■ STOP", interrupted: "■ INTERRUPTED" };
const taskOf = (run) => run?.issue && taskHash(run.issue, run.repo);
const safeLink = (url) => /^https?:\/\//i.test(String(url ?? "")) ? url : "";

export const groupOf = (status) => status === "running" ? "Running" : status === "needs-info" ? "Waiting on you"
  : FAULTS.includes(status) ? "Fault" : "Done";
export const wordOf = (status) => status === "stopped" ? "■ STOPPED" : `${GLYPHS[groupOf(status)]} ${status === "needs-info" ? "WAITING" : String(status).toUpperCase()}`;
const runGroup = (run) => run.ask_route === "unblock" ? "Recovering"
  : run.ask_route === "closed" ? "Done" : groupOf(run.status);
const runWord = (run) => run.ask_route === "unblock" ? "▷ RECOVERY"
  : run.ask_route === "closed" ? "✓ QUESTION CLOSED" : wordOf(run.status);

export function duration(seconds) {
  if (!Number.isFinite(seconds)) return "";
  const value = Math.max(0, Math.floor(seconds));
  return value >= 3600 ? `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m`
    : value >= 60 ? `${Math.floor(value / 60)}m ${value % 60}s` : `${value}s`;
}

const money = (value) => Number.isFinite(value) ? `$${value.toFixed(2)}` : "";
const count = (value) => Number.isFinite(value) ? (value >= 10000 ? `${(value / 1000).toFixed(0)}k` : value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(value)) : "0";

function elapsed(run, now) {
  const started = Date.parse(run.started);
  if (!Number.isFinite(started)) return null;
  const finished = run.finished ? Date.parse(run.finished) : now;
  return Number.isFinite(finished) ? (finished - started) / 1000 : null;
}

function rowOf(run) {
  const seconds = Number.isFinite(run.seconds) ? run.seconds : elapsed(run, Date.now());
  return { id: run.id, title: [run.ticket || run.id, run.summary].filter(Boolean).join(" · "), state: runWord(run), status: run.status,
    group: runGroup(run), repo: run.repo ?? "", priority: "", number: duration(seconds), url: safeLink(run.pr_url) };
}

const queue = createList({ rows: [], empty: "No runs yet", facets: [], sorts: [{ id: "source", label: "Newest first" }, { id: "id", label: "Id", key: "id" }],
  total: null });
queue.options.facets = [{ key: "group", label: "Group", values: GROUPS }, { key: "repo", label: "Repository", values: [] }];

export function applyQueue(view) {
  const rows = (view.queue ?? []).map(rowOf);
  if (view.detail?.run && view.id === view.detail.run.id && !rows.some((row) => row.id === view.id)) rows.unshift(rowOf(view.detail.run));
  queue.options.facets[1].values = [...new Set(rows.map((row) => row.repo).filter(Boolean))].sort();
  if (JSON.stringify(rows) !== JSON.stringify(queue.state.rows)) queue.setRows(rows);
  const route = parse(location.hash);
  const first = GROUPS.flatMap((name) => queue.rows().filter((row) => row.group === name))[0];
  if (route.screen === "run" && !route.path[0] && first) location.replace(hashOf(first.id));
}

let focus = null;

const counts = () => Object.fromEntries(GROUPS.map((name) => [name, queue.rows().filter((row) => row.group === name).length]));

class Watching extends Component {
  state = { revision: 0 };
  componentDidMount() {
    this.unsubscribe = queue.subscribe((before) => { if (!before) this.setState({ revision: this.state.revision + 1 }); });
    this.settle?.();
  }
  componentDidUpdate() { this.settle?.(); }
  componentWillUnmount() { this.unsubscribe(); }
}

export class RunSubtitle extends Watching {
  render() {
    const found = counts();
    return `${found.Running} running · ${found["Waiting on you"]} waiting on you · ${found.Fault} fault`;
  }
}

export class RunQueue extends Watching {
  settle() {
    this.base.uiContext = () => focus?.actionOf() ?? null;
    syncList(this.base);
    if (this.props.shown && this.shown !== this.props.shown) {
      this.shown = this.props.shown;
      selectItem("run-queue", this.props.shown, pane.peek());
    }
  }
  render({ shown, more }) {
    const rows = queue.rows();
    return h("div", { class: "ui-list run-queue", "data-list": "run-queue" },
      h(ListHeader, { controller: queue, queue: true, id: "run-queue" }),
      h("div", { class: "ui-list-scroll" },
        rows.length ? GROUPS.map((name) => {
          const found = rows.filter((row) => row.group === name);
          return found.length ? h("div", { key: name, "data-group": name },
            h("div", { class: "ui-list-group" }, `${name} · ${found.length}`),
            found.map((row) => h("a", { key: row.id, class: `run-row${row.id === shown ? " selected" : ""}`, href: hashOf(row.id), "data-row": "", "data-item": row.id,
              "data-group": name, "aria-current": row.id === shown ? "page" : undefined,
              onClick: (event) => {
                if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey
                  && globalThis.matchMedia?.("(width < 900px)").matches) activatePane("focus");
              } },
            h("span", { class: `run-state ${name === "Fault" ? "fault" : ""}` }, row.state),
            h("span", { class: "run-title", title: row.title }, row.title),
            h("span", { class: "run-time" }, row.number)))) : null;
        }) : h(Empty, { what: queue.state.rows.length ? "No matching runs. Clear search or remove a filter." : "No runs yet" }),
        more ? h("p", { class: "ui-state", "data-state": "truncated" }, "Showing the latest runs only") : null));
  }
}

const labelOf = (event) => [event.runner, event.provider, event.model, event.effort].filter(Boolean).join(" · ");

export function reduceSteps(events) {
  const rows = [];
  const open = new Map();
  for (const event of events) {
    const key = `${event.step}#${event.try ?? 1}`;
    if (event.event === "step_start") {
      if (open.has(key)) {
        const row = open.get(key);
        row.label = labelOf(event) || row.label;
        continue;
      }
      const row = { kind: "step", key: `s${event.seq}`, step: String(event.step ?? ""), attempt: event.try ?? 1, label: labelOf(event), state: "running",
        seconds: null, cost: null, tokensIn: null, tokensOut: null, turns: null, error: "" };
      rows.push(row);
      open.set(key, row);
    } else if (event.event === "step_end") {
      const row = open.get(key);
      if (!row) continue;
      open.delete(key);
      Object.assign(row, { state: event.result ?? (event.ok ? "pass" : "fail"), label: labelOf(event) || row.label, seconds: event.seconds ?? null,
        cost: event.cost_usd ?? null, tokensIn: event.tokens_in ?? null, tokensOut: event.tokens_out ?? null, turns: event.turns ?? null, error: event.error ?? "" });
    } else if (event.event === "heartbeat") {
      const row = [...open.values()].find((one) => one.step === event.step);
      if (row && Number.isFinite(event.seconds)) row.seconds = event.seconds;
    } else if (event.event === "error") rows.push({ kind: "error", key: `e${event.seq}`, step: event.step ?? "", text: String(event.message ?? "") });
    else if (event.event === "report") {
      if (event.status && event.status !== "running") {
        for (const row of open.values()) Object.assign(row, { state: "interrupted", error: "No completion receipt; usage may be unknown" });
        open.clear();
      }
      rows.push({ kind: "report", key: `r${event.seq}`, step: "", text: String(event.status ?? "report") });
    }
  }
  return rows;
}

function failure(view) {
  const found = [...view.events].reverse().find((event) => event.event === "error" || (event.event === "step_end" && event.error));
  return found?.message ?? found?.error ?? view.detail?.run?.summary ?? "";
}

function Steps({ view }) {
  const steps = reduceSteps(view.events);
  if (!steps.length) return h(Empty, { what: view.detail.run.status === "running" ? "Waiting for the first step" : "No steps recorded for this run" });
  return html`<div class="run-steps-scroll" tabindex="0" role="region" aria-label="Run steps"><table class="run-steps"><thead><tr><th>Step</th><th>State</th><th>Took</th><th>Cost</th><th>Model</th><th>Turns</th></tr></thead>
    <tbody>${steps.map((row) => row.kind !== "step" ? html`<tr key=${row.key} class=${`note ${row.kind}`} data-kind=${row.kind}>
      <td colspan="6">${row.kind === "error" ? "× error" : "report"}${row.step ? ` · ${row.step}` : ""} · ${row.text}</td></tr>`
    : html`<tr key=${row.key} data-kind="step" data-state=${row.state}>
      <td class="step-name">${row.step}${row.attempt > 1 ? html` <span class="muted">try ${row.attempt}</span>` : null}</td>
      <td class=${row.state === "fail" ? "fault" : ""}>${STEP_STATES[row.state] ?? row.state}${row.error ? html`<span class="muted step-error"> ${row.error}</span>` : null}</td>
      <td>${duration(row.seconds)}</td><td>${row.cost == null ? "" : money(row.cost)}</td>
      <td>${row.label}${row.tokensIn == null && row.tokensOut == null ? "" : html` <span class="muted">${count(row.tokensIn)} in · ${count(row.tokensOut)} out</span>`}</td>
      <td>${row.turns ?? ""}</td></tr>`)}</tbody></table></div>`;
}

export const stepsOf = (view) => reduceSteps(view.events).filter((row) => row.kind === "step").length;
const recordedTurns = (view) => view.events.reduce((sum, event) => sum + (event.event === "step_end" && Number.isFinite(event.turns) ? event.turns : 0), 0);

function Head({ view, now }) {
  const run = view.detail.run;
  const group = runGroup(run);
  const steps = stepsOf(view);
  const models = view.detail.models ?? [];
  const tokensIn = models.reduce((sum, model) => sum + (model.tokens_in || 0), 0);
  const tokensOut = models.reduce((sum, model) => sum + (model.tokens_out || 0), 0);
  const link = safeLink(run.pr_url);
  return html`<header class="run-head">
    <div class="run-head-line"><span class=${`run-state ${group === "Fault" ? "fault" : ""}`} data-state-word>${runWord(run)}</span>
      <span class="run-id mono">${run.id}</span>
      ${taskOf(run) ? html`<a class="run-task" href=${taskOf(run)}>${run.ticket || run.issue}</a>` : null}
      ${run.status === "running" ? html`<span class="run-live" aria-label="Live">● LIVE</span>` : null}</div>
    <h2 class="run-summary">${run.summary || run.id}</h2>
    ${view.detail.recovery ? html`<p class="run-meta">Automatic recovery: ${view.detail.recovery.phase.replaceAll("_", " ")}
      ${view.detail.recovery.detail ? ` · ${view.detail.recovery.detail}` : ""}</p>` : null}
    <p class="run-meta mono"><span>step ${steps} of ${steps}</span><span>${duration(elapsed(run, now))}</span><span>${money(run.paid)}</span>
      <span>${models.length ? `${count(tokensIn)} in · ${count(tokensOut)} out` : "no tokens recorded"}</span><span>${recordedTurns(view)} turns</span>
      ${link ? html`<a href=${link} target="_blank" rel="noopener noreferrer">PR</a>` : null}</p>
  </header>`;
}

class Workers extends Component {
  state = { page: null, error: "" };
  async next(cursor) {
    if (!cursor) return this.setState({ page: null });
    try {
      const res = await webFetch(`/api/runs/${encodeURIComponent(this.props.id)}?messages_after=${cursor}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      this.setState({ page: (await res.json()).orchestration, error: "" });
    } catch (error) { this.setState({ error: error.message }); }
  }
  render({ data }, { page, error }) {
    const { messages, more_messages, message_cursor } = page || data;
    return html`<section><h4>Workers</h4>
      <p>${data.capacity.active_builders} active builders · limit ${data.capacity.effective_builders} (${data.capacity.configured_builders} configured)</p>
      <ul>${data.workers.map(w => html`<li key=${w.id}><strong>${w.id}</strong> · ${w.phase} · attempt ${w.attempt}
        ${w.reason || w.question || ""}</li>`)}</ul>
      ${messages.map(m => html`<details key=${m.id}><summary>${m.kind} · ${m.worker} · ${m.state}</summary>
        <p>${m.body}</p><pre>${[m.id, m.reply_to, ...m.history.map(e => `${e.at}  ${e.state}  ${e.action}`)].filter(Boolean).join("\n")}</pre></details>`)}
      ${more_messages || page ? html`<button onClick=${() => this.next(more_messages ? message_cursor : 0)}>${more_messages ? "Next messages" : "First messages"}</button>` : null}
      ${error ? html`<p role="alert">${error}</p>` : null}</section>`;
  }
}

function Orchestration({ view, retry }) {
  const data = view.detail.orchestration;
  if (!data) return null;
  const run = view.detail.run;
  const rerun = (id) => dispatch(retryStep(), { ...runContext(run, "steps", {}, id), stale: retry,
    commit: (_, result) => { retry(); if (result.run) location.hash = hashOf(result.run); return { label: `Started ${id} alone; dependent evidence must be refreshed` }; } });
  const eligible = (a) => data.action_rerun_available && run.status !== "running"
    && !a.result.depends_on && (!data.workers || !a.op.startsWith("worker.") || a.result.effective)
    && ["succeeded", "failed", "cancelled", "interrupted"].includes(a.state)
    && ["worker.start", "worker.continue", "checks.run", "verification.run", "review.run"].includes(a.op);
  return html`<section class="run-orchestration" aria-label="Orchestration"><h3>Orchestration</h3>
    <p class="mono">${data.mode} · ${data.decisions} decisions · ${data.launches} controller launches · generation ${data.generation}</p>
    ${data.capacity ? html`<${Workers} key=${run.id} id=${run.id} data=${data} />` : null}
    <div class="run-steps-scroll" tabindex="0" role="region" aria-label="Adaptive actions"><table class="run-steps">
      <thead><tr><th>Action</th><th>Operation</th><th>State</th><th>Result</th><th></th></tr></thead>
      <tbody>${data.actions.map((a) => html`<tr key=${a.id}><td data-label="Action">${a.id}</td><td data-label="Operation">${a.op}</td><td data-label="State">${a.state}</td>
        <td data-label="Result"><details><summary>Result and requested settings</summary><pre>${JSON.stringify({ request: a.request, result: a.result }, null, 2)}</pre></details></td>
        <td data-label="">${eligible(a) ? html`<button type="button" onClick=${() => rerun(a.id)}>Rerun alone</button>` : null}</td></tr>`)}</tbody>
    </table></div></section>`;
}

function turnTime(turn) { return turn.at ? String(turn.at).slice(11, 19) : ""; }

export class RunFocus extends Component {
  state = { tab: "steps", open: new Set() };
  go = (id) => { location.hash = hashOf(id); };
  link = () => {
    const run = this.props.view.detail?.run;
    const url = safeLink(run?.pr_url);
    if (url) window.open(url, "_blank", "noopener,noreferrer");
    else if (taskOf(run)) location.hash = taskOf(run);
    else notice.value = "Nothing to open for this run";
  };
  openTask = () => { const href = taskOf(this.props.view.detail?.run); if (href) location.hash = href; };
  actionOf = () => {
    const run = this.props.view.detail?.run;
    if (!run) return null;
    return runPageContext(run, { refresh: () => this.props.retry(), go: this.go, openLink: this.link });
  };
  repair = () => { const context = this.actionOf(); if (context) return dispatch(repairThisRun, context); };
  fold = () => {
    if (!this.state.open.size) return false;
    this.setState({ open: new Set() });
    return true;
  };
  toggle = (key) => {
    const open = new Set(this.state.open);
    if (!open.delete(key)) open.add(key);
    this.setState({ open });
  };
  componentDidMount() { focus = this; this.sync(); }
  componentDidUpdate() { focus = this; this.sync(); }
  componentWillUnmount() {
    if (focus === this) focus = null;
    this.unregister?.();
  }
  sync() {
    const id = this.props.view.detail?.run?.id ?? "";
    if (this.registered !== id) {
      this.unregister?.();
      this.registered = id;
      this.unregister = id ? register("run", [], [{ id: "repair-run", title: "Repair run", description: "post repair for the open run", glyph: "◇", run: this.repair }]) : null;
    }
  }
  render({ view, now, retry }) {
    if (!view.detail) {
      if (view.detailError) return h(Fault, { what: "Run", fault: view.detailError, retry });
      return view.id ? h(Loading, { what: "run" }) : view.settled && !(view.queue ?? []).length ? h(Empty, { what: "No runs yet" }) : h(Loading, { what: "runs" });
    }
    const run = view.detail.run;
    const group = runGroup(run);
    const { tab, open } = this.state;
    const context = this.actionOf();
    const turns = view.turns;
    const recorded = Math.max(turns.length, recordedTurns(view));
    const tabs = [["steps", "Steps"], ...(view.detail.orchestration ? [["actions", `Actions (${view.detail.orchestration.actions.length})`]] : []),
      ["turns", `Turns (${turns.length} of ${recorded})`], ["sandbox", "Sandbox (0)"]];
    const fault = failure(view);
    const key = (turn) => `${run.id}|${turn.id}`;
    return html`<div class="run-page" data-run=${run.id}>
      ${view.detailError ? html`<${Fault} what="Run" fault=${view.detailError} retry=${retry} />` : null}
      <${Head} view=${view} now=${now} />
      <div class="run-tabs" role="tablist" aria-label="Run">${tabs.map(([name, label]) => html`<button key=${name} type="button" role="tab" id=${`run-tab-${name}`}
        aria-selected=${tab === name} aria-controls="run-panel" tabindex=${tab === name ? 0 : -1} onClick=${() => this.setState({ tab: name })}>${label}</button>`)}</div>
      <div class=${`run-strip ${group === "Fault" ? "fault" : ""}`} data-group=${group}>
        ${group === "Fault" ? html`<span class="strip-text">× ${fault || wordOf(run.status)}</span>` : null}
        ${group === "Waiting on you" ? html`<span class="strip-text">◆ waits on ${run.ticket || run.id}</span>` : null}
        <span class="strip-actions">
          ${group === "Waiting on you" && taskOf(run) ? html`<button type="button" data-act="open-task" onClick=${this.openTask}><kbd class="cap">gx</kbd> Open task</button>` : null}
          ${context?.run.retry ? html`<button type="button" data-act="retry-run" onClick=${() => dispatch(context.run.retry, context)}><kbd class="cap">⇧r</kbd> Retry</button>` : null}
          ${run.status === "running" ? html`<button type="button" data-act="stop-run" disabled=${Boolean(run.stop_asked)} onClick=${() => dispatch(actions.stop, context)}><kbd class="cap">dd</kbd> ${run.stop_asked ? "Stopping" : "Stop the run"}</button>` : null}
        </span>
      </div>
      <div class="run-panel" id="run-panel" role="tabpanel" aria-labelledby=${`run-tab-${tab}`}
        data-list=${tab === "turns" && turns.length ? undefined : "run-panel"} ref=${(el) => { if (el) el.uiContext = this.actionOf; }}>
        ${tab === "steps" ? html`${view.eventsError ? html`<${Fault} what="Steps" fault=${view.eventsError} retry=${retry} />` : null}<${Steps} view=${view} />`
    : tab === "actions" ? html`<${Orchestration} view=${view} retry=${retry} />`
    : tab === "sandbox" ? html`<${Empty} what="No sandbox recorded" />`
    : html`${view.turnsError ? html`<${Fault} what="Turns" fault=${view.turnsError} retry=${retry} />` : null}
        ${turns.length ? html`<div class="run-turns" data-list="run-turns" ref=${(el) => { if (el) { el.uiContext = this.actionOf; el.uiDismiss = this.fold; } }}>
          ${turns.map((turn) => html`<div key=${turn.id} class=${`run-turn${open.has(key(turn)) ? " open" : ""}`} data-row="" data-item=${turn.id}
            aria-expanded=${open.has(key(turn))} onClick=${() => this.toggle(key(turn))}>
            <span class="turn-time mono">${turnTime(turn)}</span><span class="turn-kind muted">${turn.kind}</span>
            <span class="turn-text">${open.has(key(turn)) ? turn.text : turn.text.split("\n")[0]}</span></div>`)}</div>`
        : view.turnsError ? null : html`<${Empty} what="No turns recorded for this run" />`}`}
      </div>
    </div>`;
  }
}
