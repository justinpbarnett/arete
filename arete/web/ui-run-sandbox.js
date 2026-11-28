import { h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { Empty, Fault, Loading } from "./ui-states.js";
import { webFetch } from "./ui-request.js";
import { notice } from "./ui-notice.js";

const html = htm.bind(h);

export const sandboxUrl = (runId) => `/api/v1/runs/${encodeURIComponent(runId)}/sandbox`;
export const diffUrl = (runId) => `/api/v1/runs/${encodeURIComponent(runId)}/diff`;

function stripPrefix(path) {
  return String(path ?? "").replace(/^[ab]\//, "").trim();
}

export function parseUnifiedDiff(diffText) {
  const text = String(diffText ?? "");
  if (!text.trim()) return [];
  const files = [];
  let current = null;
  const finish = () => {
    if (current && current.path) files.push(current);
    current = null;
  };
  for (const line of text.split("\n")) {
    if (line.startsWith("diff --git ")) {
      finish();
      const parts = line.slice(11).split(" ");
      const bPath = stripPrefix(parts.at(-1));
      current = { path: bPath, added: 0, removed: 0, status: "modified" };
      continue;
    }
    if (line.startsWith("new file mode ")) {
      if (current) current.status = "added";
      continue;
    }
    if (line.startsWith("deleted file mode ")) {
      if (current) current.status = "deleted";
      continue;
    }
    if (line.startsWith("--- ")) {
      const from = line.slice(4).trim();
      if (!current) current = { path: from === "/dev/null" ? "" : stripPrefix(from), added: 0, removed: 0, status: "modified" };
      if (from === "/dev/null") current.status = "added";
      continue;
    }
    if (line.startsWith("+++ ")) {
      const to = line.slice(4).trim();
      if (!current) current = { path: "", added: 0, removed: 0, status: "modified" };
      if (to === "/dev/null") current.status = "deleted";
      else current.path = stripPrefix(to);
      continue;
    }
    if (!current) continue;
    if (line.startsWith("+") && !line.startsWith("+++")) current.added += 1;
    else if (line.startsWith("-") && !line.startsWith("---")) current.removed += 1;
  }
  finish();
  return files;
}

export function extractCwd(sandbox) {
  if (!sandbox) return "";
  if (typeof sandbox.cwd === "string" && sandbox.cwd.trim()) return sandbox.cwd.trim();
  if (typeof sandbox.facts?.cwd === "string" && sandbox.facts.cwd.trim()) return sandbox.facts.cwd.trim();
  const cmd = String(sandbox.takeover ?? "");
  const match = cmd.match(/cd\s+(.+?)\s+&&\s+exec\s+\$SHELL/);
  if (match) return match[1].replace(/^'(.*)'$/, "$1").trim();
  return "";
}

export function hasSandboxData(sandbox, diff) {
  if (diff && String(diff).trim()) return true;
  if (!sandbox) return false;
  if (sandbox.lease) return true;
  if (sandbox.address) return true;
  if (sandbox.takeover) return true;
  if (sandbox.branch || sandbox.base) return true;
  if (sandbox.egress) return true;
  if (Array.isArray(sandbox.tail) && sandbox.tail.length > 0) return true;
  if (sandbox.device && sandbox.device !== "local") return true;
  return false;
}

export async function loadSandbox(runId, signal) {
  const [sbxRes, diffRes] = await Promise.all([
    webFetch(sandboxUrl(runId), { cache: "no-store", signal }).catch((error) => ({ ok: false, error })),
    webFetch(diffUrl(runId), { cache: "no-store", signal }).catch((error) => ({ ok: false, error })),
  ]);
  if (signal?.aborted) return null;
  let sandbox = null;
  let error = null;
  if (sbxRes.error) {
    error = { status: null, message: sbxRes.error.message || "Network error" };
  } else {
    const body = await sbxRes.json().catch(() => null);
    if (sbxRes.ok && body && typeof body === "object") sandbox = body;
    else error = { status: sbxRes.status, message: body?.detail ?? body?.error ?? `HTTP ${sbxRes.status}` };
  }
  let diff = "";
  if (!diffRes.error && diffRes.ok) {
    const body = await diffRes.json().catch(() => null);
    if (typeof body?.diff === "string") diff = body.diff;
  }
  const files = parseUnifiedDiff(diff);
  return { sandbox, diff, files, error };
}

export async function copyTakeover(command) {
  if (!command) return false;
  try {
    if (!globalThis.navigator?.clipboard?.writeText) throw new Error("Clipboard unavailable");
    await globalThis.navigator.clipboard.writeText(command);
    notice.value = "Copied take over command";
    return true;
  } catch {
    notice.value = "Could not copy take over command";
    return false;
  }
}

function probeTime(iso) {
  const text = String(iso ?? "");
  return text.length >= 19 && text[10] === "T" ? text.slice(11, 19) : text;
}

function diffLineClass(line) {
  if (line.startsWith("diff --git ") || line.startsWith("index ") || line.startsWith("--- ") || line.startsWith("+++ ")) return "diff-meta";
  if (line.startsWith("@@")) return "diff-hunk";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-del";
  return "diff-ctx";
}

export function RunSandbox({ state, retry }) {
  if (!state || (state.loading && !state.sandbox && !state.diff)) return h(Loading, { what: "sandbox" });
  if (state.error && !state.sandbox && !state.diff) return h(Fault, { what: "Sandbox", fault: state.error, retry });
  const { sandbox, diff, files = [] } = state;
  if (!hasSandboxData(sandbox, diff)) return h(Empty, { what: "No sandbox recorded" });
  const cwd = extractCwd(sandbox);
  const tail = Array.isArray(sandbox?.tail) ? sandbox.tail.slice(-14) : [];
  const probed = sandbox?.facts?.at ? probeTime(sandbox.facts.at) : "";
  const diffLines = diff ? String(diff).split("\n") : [];
  return html`<div class="run-sandbox">
    ${sandbox ? html`<section class="sandbox-section" aria-label="Device">
      <h3 class="sandbox-label mono">Device</h3>
      <div class="sandbox-card">
        <dl class="sandbox-kv">
          <dt class="mono">Device</dt>
          <dd class="mono">${sandbox.address || sandbox.device}${sandbox.facts?.ok ? html` <span class="sandbox-up">✓ UP</span>`
            : sandbox.facts ? html` <span class="sandbox-down">× DOWN</span>` : null}</dd>
          <dt class="mono">Egress</dt>
          <dd class="mono">${sandbox.egress || "default"}</dd>
          ${sandbox.branch ? html`<dt class="mono">Branch</dt><dd class="mono">${sandbox.branch}</dd>` : null}
          ${sandbox.base ? html`<dt class="mono">Base</dt><dd class="mono">${sandbox.base}</dd>` : null}
          ${cwd ? html`<dt class="mono">Cwd</dt><dd class="mono">${cwd}</dd>` : null}
          ${probed ? html`<dt class="mono">Probe</dt><dd class="mono">${probed}</dd>` : null}
        </dl>
        ${sandbox.takeover ? html`<div class="sandbox-takeover">
          <button type="button" data-act="copy-takeover" onClick=${() => copyTakeover(sandbox.takeover)}>Copy take over command</button>
          <code class="sandbox-cmd mono">${sandbox.takeover}</code>
        </div>` : null}
      </div>
    </section>` : null}
    ${files.length ? html`<section class="sandbox-section" aria-label="Files changed">
      <h3 class="sandbox-label mono">Files changed · ${files.length}</h3>
      <div class="sandbox-card sandbox-files">${files.map((file) => html`<div key=${file.path} class="sandbox-file">
        <span class="sandbox-file-path mono">${file.path}</span>
        <span class="sandbox-file-stat mono">
          <span class="diff-plus">+${file.added}</span>
          ${file.removed > 0 ? html` <span class="diff-minus">-${file.removed}</span>` : null}
          <span class="muted"> ${file.status}</span>
        </span>
      </div>`)}</div>
    </section>` : null}
    ${diffLines.length ? html`<section class="sandbox-section" aria-label="Diff so far">
      <h3 class="sandbox-label mono">Diff so far</h3>
      <pre class="sandbox-card sandbox-diff mono">${diffLines.map((line, index) => html`<span key=${index}
        class=${`diff-line ${diffLineClass(line)}`}>${line}${"\n"}</span>`)}</pre>
    </section>` : null}
    ${tail.length ? html`<section class="sandbox-section" aria-label="Terminal tail">
      <h3 class="sandbox-label mono">Terminal tail</h3>
      <pre class="sandbox-card sandbox-term mono">${tail.join("\n")}</pre>
    </section>` : null}
  </div>`;
}
