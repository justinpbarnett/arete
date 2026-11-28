import { h, Component } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { askHash } from "./ui-data.js";
import { prepareWrite, sendWrite } from "./ui-write.js";
import { actions, dispatch } from "./ui-actions.js";
import { notice } from "./ui-notice.js";

const html = htm.bind(h);
const PLACEHOLDER = "tell the agent at its next turn";

export const steerUrl = (runId) => `/api/v1/runs/${encodeURIComponent(runId)}/steer`;
export const isRunningStatus = (status) => status === "running" || status === "queued";
export const canSteerRun = (run) => Boolean(run && isRunningStatus(run.status) && run.kind === "session");

export function formatFinished(iso) {
  const text = String(iso ?? "");
  return text.length >= 16 && text[10] === "T" ? text.slice(11, 16) : "";
}

export function waitingTarget(run, snap) {
  if (!run) return { label: "", href: "#triage" };
  const ask = snap?.questions?.data?.asks?.find((item) => item.run === run.id);
  if (ask) {
    const label = ask.questions?.[0]?.header || ask.ticket || ask.id || run.ticket || run.id;
    return { label, href: askHash(ask.id) };
  }
  const evidence = snap?.asks?.evidence?.rows?.find((item) => item.run_id === run.id);
  if (evidence) {
    return { label: evidence.ticket_ref || evidence.name || evidence.id, href: `#triage/evidence/${encodeURIComponent(evidence.id)}` };
  }
  const approval = snap?.asks?.approvals?.rows?.find((item) => item.run_id === run.id);
  if (approval) {
    return { label: approval.plain_summary || approval.what || `#${approval.id}`, href: `#triage/approval/${encodeURIComponent(approval.id)}` };
  }
  return { label: run.ticket || run.id, href: "#triage" };
}

export function pruneQueued(queued, turns) {
  if (!queued.length) return queued;
  const list = turns ?? [];
  return queued.filter((item) => !list.slice(item.seenTurns).some((turn) =>
    (turn.kind === "you" || turn.kind === "user") && String(turn.text ?? "").includes(item.text)));
}

export async function sendSteer(runId, rawText) {
  const text = String(rawText ?? "").trim();
  if (!text) return { ok: false, error: "Write a message first." };
  const result = await sendWrite(prepareWrite(steerUrl(runId), { text }));
  return { ok: true, result, text };
}

export class RunSteer extends Component {
  state = { text: "", busy: false, error: "", queued: [] };

  componentDidUpdate(previous) {
    const prevId = previous.view?.detail?.run?.id ?? "";
    const currId = this.props.view?.detail?.run?.id ?? "";
    if (prevId !== currId) {
      this.setState({ text: "", busy: false, error: "", queued: [] });
      return;
    }
    if (this.state.queued.length) {
      const remaining = pruneQueued(this.state.queued, this.props.view?.turns);
      if (remaining.length !== this.state.queued.length) this.setState({ queued: remaining });
    }
  }

  submit = async (event) => {
    event?.preventDefault?.();
    const run = this.props.view?.detail?.run;
    if (!run || !canSteerRun(run) || this.state.busy) return;
    const text = this.state.text.trim();
    if (!text) {
      this.setState({ error: "Write a message first." });
      this.input?.focus?.();
      return;
    }
    const seenTurns = (this.props.view?.turns ?? []).length;
    this.setState({ busy: true, error: "" });
    try {
      const outcome = await sendSteer(run.id, text);
      if (!outcome.ok) {
        this.setState({ busy: false, error: outcome.error });
        return;
      }
      const entry = { id: `${Date.now()}-${Math.random()}`, text: outcome.text, seenTurns };
      this.setState((prev) => ({ text: "", busy: false, error: "", queued: [...prev.queued, entry] }));
      this.input?.blur?.();
      notice.value = "Queued steer for next turn";
      this.props.retry?.();
    } catch (error) {
      this.setState({ busy: false, error: error?.message || "Could not queue steer" });
    }
  };

  render({ view, group, fault, elapsedText, costText, context, snapshot, statusWord }, { text, busy, error, queued }) {
    const run = view?.detail?.run;
    if (!run) return null;
    const stopButton = isRunningStatus(run.status)
      ? html`<button type="button" data-act="stop-run" disabled=${Boolean(run.stop_asked)}
          onClick=${() => dispatch(actions.stop, context)}>${run.stop_asked ? "Stopping" : "Stop the run"} <kbd class="cap">dd</kbd></button>`
      : null;

    if (group === "Done") {
      const hhmm = formatFinished(run.finished);
      const summary = hhmm ? `finished ${hhmm} · nothing to steer` : "finished · nothing to steer";
      const stats = [elapsedText, costText].filter(Boolean).join(" · ");
      return html`<footer class="run-steer quiet" data-group="Done" aria-label="Run steering">
        <span class="run-steer-done mono">✓ DONE</span>
        <span class="strip-text muted">${summary}</span>
        ${stats ? html`<span class="run-steer-stats mono muted">${stats}</span>` : null}
      </footer>`;
    }

    if (group === "Waiting on you") {
      const target = waitingTarget(run, snapshot);
      return html`<footer class="run-steer waiting" data-group="Waiting on you" aria-label="Run steering">
        <span class="strip-text run-steer-wait">◆ waits on ${target.label}</span>
        <span class="strip-actions">
          <button type="button" data-act="open-triage" onClick=${() => { if (globalThis.location) globalThis.location.hash = target.href; }}>
            Open ${target.label} <kbd class="cap">gx</kbd></button>
          ${stopButton}
        </span>
      </footer>`;
    }

    if (group === "Fault") {
      return html`<footer class="run-steer fault" data-group="Fault" aria-label="Run steering">
        <span class="strip-text">× ${fault || statusWord}</span>
        <span class="strip-actions">
          ${context?.run?.retry ? html`<button type="button" data-act="retry-run" onClick=${() => dispatch(context.run.retry, context)}>
            Retry <kbd class="cap">⇧r</kbd></button>` : null}
          ${stopButton}
        </span>
      </footer>`;
    }

    if (canSteerRun(run)) {
      const visibleQueued = pruneQueued(queued, view.turns);
      return html`<footer class="run-steer active" data-group=${group} aria-label="Run steering">
        <form class="ops-form run-steer-form" onSubmit=${this.submit}>
          <span class="run-steer-label mono">STEER</span>
          <textarea class="run-steer-input" rows="1" aria-label="Steer this run" placeholder=${PLACEHOLDER}
            value=${text} disabled=${busy} ref=${(el) => { this.input = el; }}
            onInput=${(event) => this.setState({ text: event.currentTarget.value, error: "" })} />
          <span class="strip-actions">
            <button type="submit" class="run-steer-send" data-act="steer-run" disabled=${busy}>
              ${busy ? "Sending…" : "Send at next turn"} <kbd class="cap">⌘↵</kbd></button>
            ${stopButton}
          </span>
        </form>
        ${error ? html`<p class="run-steer-error" role="alert">${error}</p>` : null}
        ${visibleQueued.length ? html`<div class="run-steer-queued" role="status">${visibleQueued.map((item) => html`<span key=${item.id}
          class="run-steer-chip"><span class="mono">QUEUED</span> ${item.text}</span>`)}</div>` : null}
      </footer>`;
    }

    return html`<footer class="run-steer no-steer" data-group=${group} aria-label="Run steering">
      <span class="strip-text muted">This run takes no steer</span>
      <span class="strip-actions">${stopButton}</span>
    </footer>`;
  }
}
