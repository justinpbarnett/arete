import { h, Component } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { Empty } from "./ui-states.js";
import { onStreamEvent } from "./ui-live.js";
import { webFetch } from "./ui-request.js";

const html = htm.bind(h);

const STATE_META = {
  done: { cls: "ok", glyph: "✓", word: "DONE", bar: "done" },
  running: { cls: "run", glyph: "●", word: "RUN", bar: "run" },
  waiting: { cls: "ny", glyph: "◆", word: "WAIT", bar: "ny" },
  queued: { cls: "queued", glyph: "○", word: "QUEUED", bar: "" },
  fault: { cls: "fault", glyph: "▲", word: "FAULT", bar: "fault" },
};

function normalizeStatus(status, ok) {
  if (status === "running") return "running";
  if (status === "queued") return "queued";
  if (status === "waiting" || status === "ask" || status === "needs-info") return "waiting";
  if (status === "done" || status === "pass") return "done";
  if (status === "fault" || status === "fail" || status === "stop" || status === "stopped" || status === "interrupted") return "fault";
  return ok ? "done" : "fault";
}

function duration(seconds) {
  if (!Number.isFinite(seconds)) return "";
  const value = Math.max(0, Math.floor(seconds));
  return value >= 3600 ? `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m`
    : value >= 60 ? `${Math.floor(value / 60)}m ${String(value % 60).padStart(2, "0")}s` : `${value}s`;
}

function money(value) {
  return Number.isFinite(value) && value > 0 ? `$${value.toFixed(2)}` : "";
}

function count(value) {
  if (!Number.isFinite(value)) return "0";
  return value >= 10000 ? `${(value / 1000).toFixed(0)}k`
    : value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(value);
}

function clock(ms) {
  if (!Number.isFinite(ms)) return "";
  const date = new Date(ms);
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function validBand(band) {
  return Array.isArray(band) && band.length === 2
    && Number.isFinite(Number(band[0])) && Number.isFinite(Number(band[1]))
    && Number(band[0]) >= 0 && Number(band[1]) >= Number(band[0])
    ? [Number(band[0]), Number(band[1])] : null;
}

export function bandWord(seconds, band, status) {
  const b = validBand(band);
  if (!b || !Number.isFinite(seconds)) return "";
  if (seconds > b[1]) return "SLOW";
  if (status === "done" && seconds < b[0]) return "FAST";
  return "NORMAL";
}

function rowKey(node, attempt) {
  return `${node || ""}#${attempt || 1}`;
}

export function mergeSteps(apiSteps, eventRows, summarySteps) {
  const notes = [];
  const stepEvents = [];
  for (const row of eventRows ?? []) {
    if (row.kind && row.kind !== "step") notes.push(row);
    else stepEvents.push(row);
  }
  const summaryByKey = new Map();
  for (const item of summarySteps ?? []) {
    const node = item.node || item.name || "";
    summaryByKey.set(rowKey(node, item.try || 1), item);
  }
  const merged = [];
  const byKey = new Map();

  for (const raw of apiSteps ?? []) {
    const node = raw.node || raw.name || "";
    const attempt = raw.try || 1;
    const key = rowKey(node, attempt);
    const summary = summaryByKey.get(key);
    const entry = {
      key: raw.id != null ? `id:${raw.id}` : key,
      matchKey: key,
      node,
      name: raw.name || node,
      attempt,
      status: normalizeStatus(raw.status, raw.ok),
      seconds: Number.isFinite(raw.seconds) && (raw.seconds > 0 || raw.status === "done") ? raw.seconds : null,
      cost: Number.isFinite(raw.cost_usd) ? raw.cost_usd : (Number.isFinite(summary?.cost_usd) ? summary.cost_usd : null),
      model: raw.model || summary?.model || "",
      runner: raw.runner || summary?.runner || "",
      turns: Number.isFinite(raw.turns) ? raw.turns : (Number.isFinite(summary?.turns) ? summary.turns : null),
      tokensIn: null,
      tokensOut: null,
      error: raw.error || "",
      queuedAt: raw.queued_at || raw.ts || null,
      waitingSince: raw.waiting_since || null,
      needsYou: Number(raw.needs_you || summary?.needs_you || 0),
      band: validBand(raw.band) ?? validBand(summary?.band),
    };
    merged.push(entry);
    byKey.set(key, entry);
  }

  for (const ev of stepEvents) {
    const node = ev.step || ev.node || "";
    const attempt = ev.attempt || ev.try || 1;
    const key = rowKey(node, attempt);
    const existing = byKey.get(key);
    const evStatus = normalizeStatus(ev.state, ev.state === "pass");
    if (existing) {
      if (existing.status === "queued" || existing.status === "running" || evStatus !== "running") {
        existing.status = evStatus;
      }
      if (Number.isFinite(ev.seconds) && (existing.seconds == null || ev.seconds > existing.seconds || evStatus !== "running")) {
        existing.seconds = ev.seconds;
      }
      if (Number.isFinite(ev.cost)) existing.cost = ev.cost;
      if (Number.isFinite(ev.turns)) existing.turns = ev.turns;
      if (ev.label && !existing.model) existing.model = ev.label;
      if (ev.tokensIn != null) existing.tokensIn = ev.tokensIn;
      if (ev.tokensOut != null) existing.tokensOut = ev.tokensOut;
      if (ev.error) existing.error = ev.error;
    } else {
      const summary = summaryByKey.get(key);
      const entry = {
        key: ev.key || key,
        matchKey: key,
        node,
        name: node,
        attempt,
        status: evStatus,
        seconds: Number.isFinite(ev.seconds) ? ev.seconds : null,
        cost: Number.isFinite(ev.cost) ? ev.cost : null,
        model: ev.label || summary?.model || "",
        runner: summary?.runner || "",
        turns: Number.isFinite(ev.turns) ? ev.turns : (Number.isFinite(summary?.turns) ? summary.turns : null),
        tokensIn: ev.tokensIn ?? null,
        tokensOut: ev.tokensOut ?? null,
        error: ev.error || "",
        queuedAt: ev.at || null,
        waitingSince: null,
        needsYou: Number(summary?.needs_you || 0),
        band: validBand(summary?.band),
      };
      merged.push(entry);
      byKey.set(key, entry);
    }
  }

  return { steps: merged, notes };
}

export function applyWaterfallEvent(state, event, runId, now = Date.now()) {
  if (!event?.kind || !runId) return state;
  const data = event.data ?? {};
  if (event.kind === "step.started" || event.kind === "step.ended") {
    const evRun = data.run || data.run_id || (typeof event.subject === "string" && event.subject.startsWith("step:")
      ? event.subject.slice(5).split("/")[0] : "");
    if (evRun !== runId) return state;
    const node = data.node || data.step || "";
    const attempt = data.try || 1;
    const key = rowKey(node, attempt);
    const nextSteps = [...state.apiSteps];
    const idx = nextSteps.findIndex((s) => rowKey(s.node || s.name, s.try || 1) === key);
    const status = event.kind === "step.started" ? "running"
      : normalizeStatus(data.result ?? data.status, data.ok || data.result === "pass");
    const patch = {
      node,
      name: node,
      try: attempt,
      status,
      seconds: Number.isFinite(data.seconds) ? data.seconds : (event.kind === "step.started" ? 0 : undefined),
      cost_usd: Number.isFinite(data.cost_usd) ? data.cost_usd : undefined,
      turns: Number.isFinite(data.turns) ? data.turns : undefined,
      model: data.model || undefined,
      error: data.error || undefined,
      queued_at: event.kind === "step.started" ? (data.queued_at || event.at || new Date(now).toISOString()) : undefined,
    };
    const cleaned = Object.fromEntries(Object.entries(patch).filter(([, v]) => v !== undefined));
    if (idx >= 0) nextSteps[idx] = { ...nextSteps[idx], ...cleaned };
    else nextSteps.push(cleaned);
    return { ...state, apiSteps: nextSteps };
  }
  if (event.kind === "turn.appended") {
    const evRun = data.run || data.run_id || "";
    if (evRun !== runId) return state;
    const stepName = data.step || data.node || "";
    const nextTurns = { ...state.liveTurns };
    const target = stepName || state.activeKey || "__active__";
    nextTurns[target] = (nextTurns[target] || 0) + 1;
    return { ...state, liveTurns: nextTurns };
  }
  if (event.kind === "run.ended" && (data.id === runId || event.subject === `run:${runId}`)) {
    return { ...state, eta: null, etaPartial: false };
  }
  return state;
}

export function computeWaterfall({ run, steps, eta, etaPartial, liveTurns, now }) {
  const currentNow = Number.isFinite(now) ? now : Date.now();
  const list = steps ?? [];
  const live = Boolean(run && !run.finished && ["running", "waiting", "needs-info", "queued"].includes(run.status));
  const startedMs = run?.started ? Date.parse(run.started) : NaN;
  const firstStepMs = list.map((s) => s.queuedAt ? Date.parse(s.queuedAt) : NaN).find(Number.isFinite);
  const doneSum = list.reduce((sum, s) => sum + (Number.isFinite(s.seconds) ? s.seconds : 0), 0);
  const t0 = Number.isFinite(startedMs) ? startedMs
    : Number.isFinite(firstStepMs) ? firstStepMs
    : currentNow - doneSum * 1000;

  let seqCursor = t0;
  const timed = list.map((step) => {
    const rawStart = step.queuedAt ? Date.parse(step.queuedAt) : NaN;
    let startMs = null;
    let endMs = null;
    let elapsedSec = step.seconds;
    if (step.status !== "queued") {
      startMs = Number.isFinite(rawStart) ? Math.max(t0, rawStart) : seqCursor;
      if (step.status === "running" && live) {
        const fromClock = Math.max(0, (currentNow - startMs) / 1000);
        elapsedSec = Number.isFinite(step.seconds) ? Math.max(step.seconds, fromClock) : fromClock;
        endMs = Math.max(startMs, startMs + elapsedSec * 1000, currentNow);
      } else {
        const durSec = Number.isFinite(step.seconds) ? Math.max(0, step.seconds) : 0;
        elapsedSec = step.seconds;
        endMs = startMs + durSec * 1000;
      }
      seqCursor = Math.max(seqCursor, endMs);
    }
    const extraTurns = (liveTurns?.[step.node] || 0) + (step.status === "running" ? (liveTurns?.__active__ || 0) : 0);
    const turns = step.turns != null || extraTurns > 0 ? (step.turns || 0) + extraTurns : null;
    return { ...step, startMs, endMs, elapsedSec, turns, bandRange: null, estRange: null, projectedSec: null };
  });

  let estCursor = currentNow;
  let hasEstimate = false;
  let partial = Boolean(etaPartial);

  const runningRow = timed.find((r) => r.status === "running");
  if (live && runningRow) {
    if (runningRow.band) {
      const [lo, hi] = runningRow.band;
      runningRow.bandRange = [runningRow.startMs + lo * 1000, runningRow.startMs + hi * 1000];
      const rem = Math.max(0, hi - (runningRow.elapsedSec || 0));
      if (rem > 0) {
        runningRow.estRange = [currentNow, currentNow + rem * 1000];
      }
      estCursor = currentNow + rem * 1000;
      hasEstimate = true;
    } else {
      partial = true;
    }
  }

  if (live) {
    for (const row of timed) {
      if (row.status !== "queued") continue;
      if (row.band) {
        const [lo, hi] = row.band;
        const mid = (lo + hi) / 2;
        row.bandRange = [estCursor + lo * 1000, estCursor + hi * 1000];
        row.estRange = [estCursor, estCursor + mid * 1000];
        row.projectedSec = mid;
        estCursor += mid * 1000;
        hasEstimate = true;
      } else {
        partial = true;
      }
    }
  }

  const parsedApiEta = eta ? Date.parse(eta) : NaN;
  const etaMs = live ? (hasEstimate ? estCursor : (Number.isFinite(parsedApiEta) ? parsedApiEta : null)) : null;
  const finishedMs = run?.finished ? Date.parse(run.finished) : NaN;
  let etaText = "";
  if (live && Number.isFinite(etaMs)) {
    const mins = Math.max(1, Math.round(Math.max(0, etaMs - currentNow) / 60000));
    etaText = `ETA ${clock(etaMs)} · about ${mins} min left${partial ? " · partial" : ""}`;
  } else if (Number.isFinite(finishedMs)) {
    etaText = `finished ${clock(finishedMs)}`;
  }

  const endpoints = [live ? currentNow : (Number.isFinite(finishedMs) ? finishedMs : t0)];
  if (Number.isFinite(etaMs)) endpoints.push(etaMs);
  for (const row of timed) {
    if (Number.isFinite(row.endMs)) endpoints.push(row.endMs);
    if (row.bandRange) endpoints.push(row.bandRange[1]);
    if (row.estRange) endpoints.push(row.estRange[1]);
  }
  const tEnd = Math.max(...endpoints);
  const span = Math.max(60000, tEnd - t0);
  const pos = (ms) => Math.max(0, Math.min(1000, ((ms - t0) / span) * 1000));

  const rows = timed.map((row) => {
    const meta = STATE_META[row.status] ?? STATE_META.done;
    const bar = row.startMs != null && row.endMs != null
      ? { x: pos(row.startMs), width: Math.max(4, pos(row.endMs) - pos(row.startMs)), cls: meta.bar }
      : null;
    const bandBox = row.bandRange
      ? { x: pos(row.bandRange[0]), width: Math.max(2, pos(row.bandRange[1]) - pos(row.bandRange[0])) }
      : null;
    const estBox = row.estRange
      ? { x: pos(row.estRange[0]), width: Math.max(4, pos(row.estRange[1]) - pos(row.estRange[0])) }
      : null;
    return { ...row, meta, bar, bandBox, estBox, bandLabel: bandWord(row.elapsedSec, row.band, row.status) };
  });

  const activeIdx = rows.findIndex((r) => r.status === "running" || r.status === "waiting" || r.status === "fault");
  const fallbackIdx = activeIdx >= 0 ? activeIdx : rows.length - 1;
  const activeRow = fallbackIdx >= 0 ? rows[fallbackIdx] : null;
  const totalTurns = rows.reduce((sum, r) => sum + (Number.isFinite(r.turns) ? r.turns : 0), 0);

  return {
    rows,
    etaMs,
    etaText,
    etaPartial: partial,
    nowX: live ? pos(currentNow) : null,
    live,
    activeStep: activeRow?.name ?? "",
    activeKey: activeRow?.key ?? "",
    activeIndex: fallbackIdx >= 0 ? fallbackIdx + 1 : 0,
    totalSteps: rows.length,
    activeBandWord: activeRow?.bandLabel ?? "",
    totalTurns,
  };
}

export class RunWaterfall extends Component {
  state = { apiSteps: [], summarySteps: [], eta: null, etaPartial: false, liveTurns: {}, selectedKey: "", loadedRun: "" };

  async fetchRunData(id) {
    if (!id) return;
    this.controller?.abort();
    const controller = this.controller = new AbortController();
    const encoded = encodeURIComponent(id);
    try {
      const [stepsRes, runRes] = await Promise.all([
        webFetch(`/api/v1/runs/${encoded}/steps`, { cache: "no-store", signal: controller.signal }).catch(() => null),
        webFetch(`/api/v1/runs/${encoded}`, { cache: "no-store", signal: controller.signal }).catch(() => null),
      ]);
      if (controller.signal.aborted) return;
      const stepsBody = stepsRes?.ok ? await stepsRes.json().catch(() => null) : null;
      const runBody = runRes?.ok ? await runRes.json().catch(() => null) : null;
      if (controller.signal.aborted) return;
      const apiSteps = Array.isArray(stepsBody?.steps) ? stepsBody.steps
        : Array.isArray(runBody?.steps) ? runBody.steps : [];
      const summarySteps = Array.isArray(runBody?.steps) ? runBody.steps : [];
      const eta = typeof runBody?.eta === "string" ? runBody.eta : null;
      const etaPartial = Boolean(runBody?.eta_partial);
      this.setState({ apiSteps, summarySteps, eta, etaPartial, liveTurns: {}, loadedRun: id });
    } catch {}
  }

  componentDidMount() {
    const id = this.currentRunId();
    this.fetchRunData(id);
    this.unsubscribe = onStreamEvent((event) => {
      const runId = this.currentRunId();
      if (!runId) return;
      if (event.kind === "reset") {
        this.fetchRunData(runId);
        return;
      }
      const next = applyWaterfallEvent(this.state, event, runId);
      if (next !== this.state) {
        this.setState(next);
        if (event.kind === "step.started" || event.kind === "step.ended" || event.kind === "run.ended") {
          this.fetchRunData(runId);
        }
      }
    });
  }

  componentDidUpdate(prevProps) {
    const prevId = prevProps.runId || prevProps.view?.detail?.run?.id || prevProps.view?.id || "";
    const nextId = this.currentRunId();
    if (nextId && nextId !== prevId) {
      this.setState({ apiSteps: [], summarySteps: [], eta: null, etaPartial: false, liveTurns: {}, selectedKey: "", loadedRun: "" });
      this.fetchRunData(nextId);
    }
  }

  componentWillUnmount() {
    this.controller?.abort();
    this.unsubscribe?.();
  }

  currentRunId() {
    return this.props.runId || this.props.view?.detail?.run?.id || this.props.view?.id || "";
  }

  reportSummary(calc) {
    if (!this.props.onSummary) return;
    const signature = `${calc.activeStep}|${calc.activeIndex}|${calc.totalSteps}|${calc.activeBandWord}|${calc.totalTurns}`;
    if (this.lastSummary !== signature) {
      this.lastSummary = signature;
      const summary = {
        stepName: calc.activeStep,
        stepIndex: calc.activeIndex,
        stepCount: calc.totalSteps,
        bandWord: calc.activeBandWord,
        turns: calc.totalTurns,
      };
      queueMicrotask(() => this.props.onSummary?.(summary));
    }
  }

  render({ view, now, compact = false, reduceSteps }, { apiSteps, summarySteps, eta, etaPartial, liveTurns, selectedKey }) {
    const run = view?.detail?.run;
    const eventRows = reduceSteps ? reduceSteps(view?.events ?? []) : [];
    const { steps, notes } = mergeSteps(apiSteps, eventRows, summarySteps);
    if (!steps.length && !notes.length) {
      return h(Empty, { what: run?.status === "running" ? "Waiting for the first step" : "No steps recorded for this run" });
    }
    const calc = computeWaterfall({ run, steps, eta, etaPartial, liveTurns, now });
    this.reportSummary(calc);
    const selected = selectedKey && calc.rows.some((r) => r.key === selectedKey) ? selectedKey : calc.activeKey;
    const cols = compact ? ["Step", "State", "Time", "Took", "Cost"] : ["Step", "State", "Time", "Took", "Cost", "Model", "Turns"];

    return html`<section class="wf-sec" aria-label="Run steps waterfall">
      ${compact ? null : html`<div class="wf-head-lbl">Steps · waterfall · normal range shaded</div>`}
      <div class="wf-panel">
        ${calc.etaText ? html`<div class="wf-etaline" data-eta="">
          <span class="wf-eta-text">${calc.etaText}</span>
          <span class="wf-legend">normal range shaded · estimate dashed</span>
        </div>` : null}
        <div class=${`wf-grid${compact ? " compact" : ""}`} role="grid" aria-label="Step timeline">
          <div class="wf-row wf-header" role="row">${cols.map((col, idx) =>
            html`<div key=${col} class=${`wf-cell wf-h${idx === 3 || idx === 4 || idx === 6 ? " num" : ""}`} role="columnheader"
              title=${col === "Time" ? "Shaded: normal range for the step. Dashed: estimate" : undefined}>${col}</div>`
          )}</div>
          ${calc.rows.map((row) => {
            const isSel = row.key === selected;
            const selCls = isSel ? " sel" : "";
            const took = row.elapsedSec != null ? duration(row.elapsedSec)
              : row.projectedSec != null ? html`<span class="muted">~${duration(row.projectedSec)}</span>` : "";
            const bandTitle = row.band ? `normal ${duration(row.band[0])} to ${duration(row.band[1])}` : "";
            const barTitle = `${row.name}: ${row.meta.word}${row.elapsedSec != null ? ` · ${duration(row.elapsedSec)}` : ""}${bandTitle ? ` · ${bandTitle}` : ""}`;
            return html`<div key=${row.key} class=${`wf-row${selCls}`} role="row" data-kind="step" data-step=${row.name}
              data-state=${row.status} data-selected=${isSel ? "true" : "false"} aria-selected=${isSel}
              onClick=${() => this.setState({ selectedKey: row.key })}>
              <div class="wf-cell wf-name mono" role="gridcell" data-label="Step">
                <span>${row.name}</span>${row.attempt > 1 ? html` <span class="muted">try ${row.attempt}</span>` : null}
              </div>
              <div class=${`wf-cell wf-state ${row.meta.cls}`} role="gridcell" data-label="State">
                <span class="wf-st-word">${row.meta.glyph} ${row.meta.word}</span>
                ${row.error ? html`<span class="muted wf-err" title=${row.error}>${row.error}</span>` : null}
              </div>
              <div class="wf-cell wf-trk" role="gridcell" data-label="Time" title=${barTitle}>
                <svg class="wf-svg" viewBox="0 0 1000 22" preserveAspectRatio="none" aria-hidden="true">
                  ${row.bandBox ? html`<rect class="wf-bnd" x=${row.bandBox.x.toFixed(1)} y="3" width=${row.bandBox.width.toFixed(1)} height="16" />` : null}
                  ${row.bandBox ? html`<line class="wf-bnd-edge" x1=${row.bandBox.x.toFixed(1)} x2=${row.bandBox.x.toFixed(1)} y1="3" y2="19" vector-effect="non-scaling-stroke" />` : null}
                  ${row.bandBox ? html`<line class="wf-bnd-edge" x1=${(row.bandBox.x + row.bandBox.width).toFixed(1)} x2=${(row.bandBox.x + row.bandBox.width).toFixed(1)} y1="3" y2="19" vector-effect="non-scaling-stroke" />` : null}
                  ${row.bar ? html`<rect class=${`wf-bar ${row.bar.cls}`} x=${row.bar.x.toFixed(1)} y="6" width=${row.bar.width.toFixed(1)} height="10" vector-effect="non-scaling-stroke" />` : null}
                  ${row.estBox ? html`<rect class="wf-bar est" x=${row.estBox.x.toFixed(1)} y="6" width=${row.estBox.width.toFixed(1)} height="10" vector-effect="non-scaling-stroke" />` : null}
                  ${calc.nowX != null ? html`<line class="wf-nowl" x1=${calc.nowX.toFixed(1)} x2=${calc.nowX.toFixed(1)} y1="0" y2="22" vector-effect="non-scaling-stroke" />` : null}
                </svg>
              </div>
              <div class="wf-cell num mono" role="gridcell" data-label="Took">${took}</div>
              <div class="wf-cell num mono" role="gridcell" data-label="Cost">${money(row.cost)}</div>
              ${compact ? null : html`<div class="wf-cell wf-mdl mono muted" role="gridcell" data-label="Model" title=${row.model}>
                ${row.model}${row.tokensIn != null || row.tokensOut != null ? html` <span>${count(row.tokensIn)} in · ${count(row.tokensOut)} out</span>` : null}
              </div>`}
              ${compact ? null : html`<div class="wf-cell num mono" role="gridcell" data-label="Turns">${row.turns ?? ""}</div>`}
            </div>`;
          })}
        </div>
        ${notes.length ? html`<div class="wf-notes">${notes.map((note) =>
          html`<div key=${note.key} class=${`wf-note ${note.kind}`} data-kind=${note.kind}>${note.kind === "error" ? "× error" : "report"}${note.step ? ` · ${note.step}` : ""} · ${note.text}</div>`
        )}</div>` : null}
      </div>
    </section>`;
  }
}
