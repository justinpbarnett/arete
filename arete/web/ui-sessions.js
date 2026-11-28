import { h, Component } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { signal, effect } from "./vendor-signals.js";
import { openPalette } from "./ui-palette.js";
import { prepareWrite, sendWrite } from "./ui-write.js";
import { webFetch } from "./ui-request.js";
import { notice } from "./ui-notice.js";
import { runHash } from "./ui-data.js";

const html = htm.bind(h);

export const sessionBox = signal(null);
export const sessionChoices = signal(null);
export const sessionIdleHours = signal(null);
export const sessionRuns = signal([]);
let opener = null;

export async function fetchSessionChoices() {
  const response = await webFetch("/api/sessions", { cache: "no-store" });
  const body = await response.json().catch(() => null);
  if (!response.ok || !body?.choices) throw new Error(body?.error ?? body?.detail ?? `HTTP ${response.status}`);
  sessionChoices.value = body.choices;
  if (Number.isFinite(body.choices.idle_hours)) sessionIdleHours.value = body.choices.idle_hours;
  return body.choices;
}

export async function fetchSessionRuns() {
  const response = await webFetch("/api/v1/runs?kind=session", { cache: "no-store" });
  const body = await response.json().catch(() => null);
  if (!response.ok || !Array.isArray(body?.runs)) throw new Error(body?.error ?? body?.detail ?? `HTTP ${response.status}`);
  sessionRuns.value = body.runs;
  return body.runs;
}

export function validateSessionDraft(draft) {
  const prompt = String(draft?.prompt ?? "").trim();
  if (!prompt) return { error: "Write a prompt first." };
  const rawBudget = String(draft?.budget ?? "").trim();
  let budget_usd = null;
  if (rawBudget) {
    const parsed = Number(rawBudget);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      return { error: "Budget must be a number of dollars above zero, or empty for no limit." };
    }
    budget_usd = parsed;
  }
  const payload = {
    prompt,
    device: String(draft?.device || "local"),
    model: String(draft?.model ?? "").trim(),
    runner: String(draft?.runner ?? "").trim(),
    repo: String(draft?.repo ?? "").trim(),
    tools: Array.isArray(draft?.tools) ? [...draft.tools] : null,
    budget_usd,
  };
  return { payload };
}

export function openSessionBox() {
  if (!sessionBox.peek()) opener = globalThis.document?.activeElement ?? null;
  const cached = sessionChoices.peek();
  const defaultDevice = cached?.devices?.includes("local") ? "local" : (cached?.devices?.[0] ?? "local");
  sessionBox.value = {
    prompt: "",
    device: defaultDevice,
    model: "",
    runner: "",
    repo: "",
    tools: cached ? [...(cached.default_tools ?? [])] : null,
    budget: "",
    busy: false,
    error: "",
    choicesLoading: !cached,
    choicesError: "",
  };
  fetchSessionChoices().then((choices) => {
    const current = sessionBox.peek();
    if (!current) return;
    const device = choices.devices?.includes(current.device) ? current.device : (choices.devices?.[0] ?? "local");
    const tools = current.tools === null ? [...(choices.default_tools ?? [])] : current.tools;
    sessionBox.value = { ...current, device, tools, choicesLoading: false, choicesError: "" };
  }).catch((error) => {
    const current = sessionBox.peek();
    if (current) sessionBox.value = { ...current, choicesLoading: false, choicesError: error?.message || "Could not load session choices" };
  });
  fetchSessionRuns().catch(() => {});
}

export function closeSessionBox() {
  if (!sessionBox.peek()) return;
  sessionBox.value = null;
  const back = opener;
  opener = null;
  if (back?.isConnected) back.focus();
}

export async function submitSession() {
  const box = sessionBox.peek();
  if (!box || box.busy) return false;
  const check = validateSessionDraft(box);
  if (check.error) {
    sessionBox.value = { ...box, error: check.error };
    globalThis.document?.getElementById("session-prompt")?.focus();
    return false;
  }
  sessionBox.value = { ...box, busy: true, error: "" };
  try {
    const result = await sendWrite(prepareWrite("/api/v1/sessions", check.payload));
    const id = result?.run?.id || result?.session?.id || "";
    closeSessionBox();
    notice.value = id ? `Started session ${id}` : "Started session";
    if (id && globalThis.location) globalThis.location.hash = runHash(id);
    fetchSessionRuns().catch(() => {});
    return true;
  } catch (error) {
    const current = sessionBox.peek();
    if (current) sessionBox.value = { ...current, busy: false, error: error?.message || "Could not start session" };
    return false;
  }
}

export const sessionCommand = {
  id: "start-session",
  title: "Start a session",
  description: "launch an interactive agent session",
  glyph: "▷",
  run: openSessionBox,
};

function patchBox(change) {
  const current = sessionBox.peek();
  if (current) sessionBox.value = { ...current, ...change, error: "" };
}

function toggleTool(tool) {
  const current = sessionBox.peek();
  if (!current) return;
  const set = new Set(current.tools ?? sessionChoices.peek()?.default_tools ?? []);
  if (!set.delete(tool)) set.add(tool);
  patchBox({ tools: [...set] });
}

function pickFrom(mode, current, items, onPick) {
  openPalette({
    mode,
    current,
    rows: items,
    onPick: (row) => {
      onPick(row.id);
      globalThis.document?.getElementById("session-prompt")?.focus();
    },
  });
}

export class SessionBox extends Component {
  state = { revision: 0 };

  componentDidMount() {
    this.unsubscribe = effect(() => {
      sessionBox.value;
      sessionChoices.value;
      this.setState({ revision: (this.state.revision ?? 0) + 1 });
    });
    this.focus();
  }

  componentDidUpdate() { this.focus(); }

  focus() {
    const open = Boolean(sessionBox.peek());
    if (open && !this.wasOpen) this.base?.querySelector?.("#session-prompt")?.focus();
    this.wasOpen = open;
  }

  componentWillUnmount() {
    this.unsubscribe?.();
  }

  render() {
    const box = sessionBox.value;
    if (!box) return null;
    const choices = sessionChoices.value;
    const devices = choices?.devices ?? ["local"];
    const models = choices?.models ?? [];
    const runners = choices?.runners ?? [];
    const repos = choices?.repos ?? [];
    const tools = choices?.tools ?? [];
    const selectedTools = box.tools ?? choices?.default_tools ?? [];
    const defaultModel = choices?.default_model || "default";
    const idleHours = choices?.idle_hours ?? sessionIdleHours.value;

    const pickDevice = () => pickFrom("device", box.device,
      devices.map((name) => ({ id: name, title: name, group: "Devices", glyph: "○" })),
      (id) => patchBox({ device: id }));
    const pickModel = () => pickFrom("model", box.model || "__default__", [
      { id: "__default__", title: `Default (${defaultModel})`, description: "configured session model", group: "Default", glyph: "○" },
      ...models.map((name) => ({ id: name, title: name, group: "Models", glyph: "◇" })),
    ], (id) => patchBox({ model: id === "__default__" ? "" : id }));
    const pickRunner = () => pickFrom("runner", box.runner || "__auto__", [
      { id: "__auto__", title: "By the model", description: "choose runner from model", group: "Default", glyph: "○" },
      ...runners.map((name) => ({ id: name, title: name, group: "Runners", glyph: "▷" })),
    ], (id) => patchBox({ runner: id === "__auto__" ? "" : id }));
    const pickRepo = () => pickFrom("repo", box.repo || "__none__", [
      { id: "__none__", title: "None", description: "no repository worktree", group: "Default", glyph: "○" },
      ...repos.map((name) => ({ id: name, title: name, group: "Repositories", glyph: "▤" })),
    ], (id) => patchBox({ repo: id === "__none__" ? "" : id }));

    return html`<section class="session-box" role="dialog" aria-label="Start a session">
      <form class="session-box-form" onSubmit=${(event) => { event.preventDefault(); submitSession(); }}>
        <div class="session-box-main">
          <label class="session-box-label mono" for="session-prompt">SESSION</label>
          <input id="session-prompt" class="session-box-prompt" type="text" autoComplete="off"
            placeholder="What should the session work on?" value=${box.prompt} readOnly=${box.busy}
            onInput=${(event) => patchBox({ prompt: event.currentTarget.value })} />
          <button type="submit" class="session-box-go" data-act="start-session-submit" disabled=${box.busy}>
            ${box.busy ? "Starting…" : "Start session"} <kbd class="cap">⌘↵</kbd></button>
          <button type="button" class="session-box-cancel" data-act="start-session-cancel" onClick=${closeSessionBox}>
            Cancel <kbd class="cap">Esc</kbd></button>
        </div>
        <div class="session-box-pickers">
          <button type="button" class="session-picker" data-picker="device" disabled=${box.busy} onClick=${pickDevice}>
            <span class="muted">Device</span> <span class="mono">${box.device}</span></button>
          <button type="button" class="session-picker" data-picker="model" disabled=${box.busy} onClick=${pickModel}>
            <span class="muted">Model</span> <span class="mono">${box.model || defaultModel}</span></button>
          <button type="button" class="session-picker" data-picker="runner" disabled=${box.busy} onClick=${pickRunner}>
            <span class="muted">Runner</span> <span class="mono">${box.runner || "By the model"}</span></button>
          <button type="button" class="session-picker" data-picker="repo" disabled=${box.busy} onClick=${pickRepo}>
            <span class="muted">Repo</span> <span class="mono">${box.repo || "None"}</span></button>
          <label class="session-budget">
            <span class="muted">Budget $</span>
            <input id="session-budget" type="text" inputMode="decimal" autoComplete="off" placeholder="none"
              value=${box.budget} readOnly=${box.busy}
              onInput=${(event) => patchBox({ budget: event.currentTarget.value })} />
          </label>
          ${idleHours != null ? html`<span class="session-idle muted">Closes after ${idleHours}h idle</span>` : null}
        </div>
        ${tools.length ? html`<div class="session-box-tools" role="group" aria-label="Tools">
          <span class="muted">Tools</span>
          ${tools.map((tool) => html`<label key=${tool} class="session-tool">
            <input type="checkbox" checked=${selectedTools.includes(tool)} disabled=${box.busy}
              onChange=${() => toggleTool(tool)} />
            <span class="mono">${tool}</span>
          </label>`)}
        </div>` : null}
        ${box.choicesError ? html`<p class="session-box-note muted">${box.choicesError}</p>` : null}
        ${box.error ? html`<p class="session-box-error" role="alert">${box.error}</p>` : null}
      </form>
    </section>`;
  }
}
