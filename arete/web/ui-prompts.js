import { Component, h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { signal } from "./vendor-signals.js";
import { prompts as promptsMeta } from "./ui-data.js";
import { createList, ListHeader } from "./ui-list.js";
import { pane, selectItem, syncList } from "./ui-keys.js";
import { notice } from "./ui-notice.js";
import { webFetch } from "./ui-request.js";
import { Empty, Fault, Gate, Loading } from "./ui-states.js";

const html = htm.bind(h);
const TIMEOUT_MS = 3000;
const REFRESH_MS = 5000;
const PROMPT_LIST_URL = "/api/v1/config/prompt";
const DRAFTS_URL = "/api/v1/drafts";
const PENDING_STATES = new Set(["draft", "valid", "invalid"]);
const BUILTIN_ADWS = ["feature", "patch", "repair", "revise"];
const DEFAULT_STEP_PROMPTS = {
  plan: "plan",
  patch: "patch",
  revise: "revise",
  repair: "repair",
  implement: "build",
  verify: "verify",
  review: "review",
  learn: "learn",
  document: "document",
  ci: "ci_fix",
};

export const NEW_PROMPT_NOTICE = "A new prompt needs an engine call bound to it, and config may only override a prompt the engine has";

export function notifyNewPrompt() {
  notice.value = NEW_PROMPT_NOTICE;
}

export const promptPath = (route) => (route?.path ?? []).filter(Boolean).join("/");

export function promptFileName(path, name = "") {
  const clean = String(path || "").split("/").filter(Boolean).pop();
  if (clean) return clean;
  return name ? (name.includes(".") ? name : `${name}.md`) : "";
}

export const isLocalOverride = (item) => Boolean(item?.overrides || item?.override);

export function versionLabel(item) {
  if (typeof item?.version === "string" && item.version) {
    return item.version.startsWith("v") ? item.version : `v${item.version}`;
  }
  if (Number.isInteger(item?.version)) return `v${item.version}`;
  return "";
}

export function versionNumber(item) {
  if (typeof item?.version === "number" && Number.isFinite(item.version)) return item.version;
  const match = String(item?.version ?? "").match(/\d+/);
  return match ? Number(match[0]) : 0;
}

export function normalizePromptName(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  const base = raw.split("/").filter(Boolean).pop() ?? raw;
  return base.replace(/\.md$/i, "");
}

export function promptCounts(items, drafts) {
  const list = Array.isArray(items) ? items : [];
  const pendingDrafts = (Array.isArray(drafts) ? drafts : [])
    .filter((draft) => draft?.kind === "prompt" && PENDING_STATES.has(draft.state));
  return {
    total: list.length,
    overrides: list.filter(isLocalOverride).length,
    pending: pendingDrafts.length,
  };
}

function pendingByPrompt(drafts) {
  const counts = new Map();
  for (const draft of Array.isArray(drafts) ? drafts : []) {
    if (draft?.kind !== "prompt" || !PENDING_STATES.has(draft.state)) continue;
    const key = draft.name || normalizePromptName(draft.path);
    if (!key) continue;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return counts;
}

export function matchPromptItem(items, routePath) {
  if (!routePath || !Array.isArray(items) || !items.length) return null;
  const exact = items.find((item) => item.path === routePath);
  if (exact) return exact;
  const byOverride = items.find((item) => item.overrides === routePath);
  if (byOverride) return byOverride;
  const stripped = routePath.replace(/^arete\//, "");
  const byPrefixed = items.find((item) => item.path === stripped || `arete/${item.path}` === routePath);
  if (byPrefixed) return byPrefixed;
  const nameMatch = routePath.match(/^(?:arete\/)?(?:prompts\/)?([^/]+?)(?:\.md)?$/);
  if (nameMatch) {
    const wanted = nameMatch[1];
    return items.find((item) => item.name === wanted) ?? null;
  }
  return null;
}

export function parseLoopTarget(routePath, loops = []) {
  if (!routePath) return null;
  const found = (Array.isArray(loops) ? loops : []).find(
    (item) => item.path === routePath || `arete/${item.path}` === routePath || item.path === routePath.replace(/^arete\//, ""),
  );
  if (found) {
    return {
      kind: "loop",
      name: found.name,
      file: promptFileName(found.path, "loop.toml"),
      path: found.path,
      level: found.level || "",
      item: found,
    };
  }
  const loopMatch = routePath.match(/^(arete\/)?(?:layers\/([^/]+)\/)?loops\/([^/]+)\/(.+)$/);
  if (loopMatch) {
    const [, enginePrefix, repoName, loopName, subfile] = loopMatch;
    const name = repoName ? `${repoName.replaceAll(".", "..")}.${loopName}` : loopName;
    const isScript = /\.(?:py|sh|js|mjs)$/i.test(subfile);
    return {
      kind: isScript ? "script" : "loop",
      name,
      file: subfile,
      path: routePath,
      level: enginePrefix ? "engine" : "",
      item: null,
    };
  }
  if (/^(?:arete\/)?(?:scripts|tools)\//.test(routePath) || /\.(?:py|sh|toml)$/i.test(routePath)) {
    return {
      kind: "script",
      name: "",
      file: promptFileName(routePath),
      path: routePath,
      level: "",
      item: null,
    };
  }
  return null;
}

function stepPromptMap(librarySteps) {
  const map = { ...DEFAULT_STEP_PROMPTS };
  for (const step of Array.isArray(librarySteps) ? librarySteps : []) {
    if (step?.id && step.prompt) map[step.id] = normalizePromptName(step.prompt);
  }
  return map;
}

function parseAdwTomlUses(content, stepMap) {
  const uses = [];
  if (typeof content !== "string" || !content) return uses;
  const nodeBlocks = content.split(/^\[\[node\]\]\s*$/m).slice(1);
  if (nodeBlocks.length) {
    for (const block of nodeBlocks) {
      const section = block.split(/^\[\[/m)[0];
      const stepMatch = section.match(/^\s*step\s*=\s*"([^"]+)"/m);
      const promptMatch = section.match(/^\s*prompt\s*=\s*"([^"]+)"/m);
      const idMatch = section.match(/^\s*id\s*=\s*"([^"]+)"/m);
      const promptName = promptMatch ? normalizePromptName(promptMatch[1]) : (stepMatch ? stepMap[stepMatch[1]] ?? "" : "");
      if (promptName) uses.push({ prompt: promptName, step: stepMatch?.[1] || idMatch?.[1] || "" });
    }
    return uses;
  }
  for (const match of content.matchAll(/\bdo\s*=\s*"([^"]+)"/g)) {
    const stepId = match[1];
    const promptName = stepMap[stepId] ?? "";
    if (promptName) uses.push({ prompt: promptName, step: stepId });
  }
  return uses;
}

export function extractUsedBy(adws, librarySteps = []) {
  const stepMap = stepPromptMap(librarySteps);
  const byPrompt = new Map();
  const add = (promptName, label) => {
    if (!promptName || !label) return;
    if (!byPrompt.has(promptName)) byPrompt.set(promptName, []);
    const list = byPrompt.get(promptName);
    if (!list.includes(label)) list.push(label);
  };
  for (const adw of Array.isArray(adws) ? adws : []) {
    const adwName = adw?.name || "";
    if (!adwName) continue;
    if (Array.isArray(adw.nodes)) {
      for (const node of adw.nodes) {
        const direct = node?.prompt ?? node?.config?.prompt;
        const stepId = node?.step ?? node?.do ?? node?.id ?? "";
        const promptName = direct ? normalizePromptName(direct) : (stepId ? stepMap[stepId] ?? "" : "");
        add(promptName, stepId ? `${adwName} · ${stepId}` : adwName);
      }
      continue;
    }
    for (const use of parseAdwTomlUses(adw.content, stepMap)) {
      add(use.prompt, use.step ? `${adwName} · ${use.step}` : adwName);
    }
  }
  return Object.fromEntries([...byPrompt.entries()].map(([key, values]) => [key, values.sort()]));
}

export function extractLoopMeta(detail, target) {
  const content = typeof detail?.content === "string" ? detail.content : "";
  const descMatch = content.match(/^\s*description\s*=\s*"([^"]*)"/m);
  const description = detail?.description || descMatch?.[1] || (target?.kind === "script" ? "Read-only script file" : "Read-only loop file");
  const usedBy = Array.isArray(detail?.usedBy) && detail.usedBy.length
    ? detail.usedBy.join(", ")
    : target?.name ? `loop ${target.name}` : "—";
  const lines = content ? content.split("\n").length : (detail?.lines ?? "—");
  const updated = detail?.updated || detail?.at || detail?.content_version || "—";
  return { description, usedBy, lines: String(lines), updated };
}

let openPromptId = "";

export const promptList = createList({
  rows: [],
  empty: "No prompt files found",
  sort: "name",
  total: 0,
  facets: [
    { key: "overrideFlag", label: "Overrides", values: ["Local overrides"] },
    { key: "pendingFlag", label: "Pending", values: ["Pending changes"] },
  ],
  sorts: [
    { id: "name", label: "Name", key: "title" },
    { id: "version", label: "Version", key: "versionNum", direction: -1 },
  ],
});

const baseListRows = promptList.rows.bind(promptList);
promptList.rows = () => {
  const found = baseListRows();
  const query = promptList.state.query.trim();
  if (!query || !openPromptId) return found;
  if (found.some((row) => row.id === openPromptId)) return found;
  const open = promptList.state.rows.find((row) => row.id === openPromptId);
  if (!open) return found;
  const matchesFacets = Object.entries(promptList.state.facets)
    .every(([key, values]) => !values.length || values.includes(open[key]));
  if (!matchesFacets) return found;
  const merged = [...found, open];
  const sort = promptList.options.sorts?.find((one) => one.id === promptList.state.sort);
  if (sort?.key) {
    merged.sort((a, b) => {
      const av = a[sort.key];
      const bv = b[sort.key];
      const cmp = typeof av === "number" && typeof bv === "number" ? av - bv : String(av).localeCompare(String(bv));
      return (sort.direction ?? 1) * cmp || String(a.id).localeCompare(String(b.id));
    });
  }
  return merged;
};

const blankState = () => ({
  items: [],
  loops: [],
  drafts: [],
  usedBy: {},
  selectedPath: "",
  selectedKind: "prompt",
  target: null,
  detail: null,
  listError: null,
  detailError: null,
  settled: false,
  detailLoading: false,
});

export const promptsView = signal(blankState());

const etagCache = new Map();
const adwCache = new Map();
let libraryStepsCache = null;
let activeController = null;
let refreshTimer = null;
let currentRoute = null;
let readGeneration = 0;

function routes() {
  return {
    list: PROMPT_LIST_URL,
    drafts: DRAFTS_URL,
    ...promptsMeta(),
  };
}

async function fetchJson(url, signal, conditional = false) {
  const controller = new AbortController();
  if (signal?.aborted) return { aborted: true };
  const abort = () => controller.abort();
  const expiry = setTimeout(abort, TIMEOUT_MS);
  signal?.addEventListener("abort", abort, { once: true });
  const cached = conditional ? etagCache.get(url) : null;
  const headers = cached?.etag ? { "If-None-Match": cached.etag } : {};
  try {
    const response = await webFetch(url, { headers, cache: "no-store", signal: controller.signal });
    if (response.status === 304 && cached) return { data: cached.data, unchanged: true };
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      return { error: { status: response.status, message: body?.detail ?? body?.error ?? `HTTP ${response.status}` } };
    }
    if (!body || typeof body !== "object") return { error: { status: response.status, message: "Malformed response" } };
    const etag = response.headers.get("ETag");
    if (conditional && etag) {
      etagCache.delete(url);
      etagCache.set(url, { etag, data: body });
      if (etagCache.size > 48) etagCache.delete(etagCache.keys().next().value);
    }
    return { data: body };
  } catch (error) {
    if (signal?.aborted) return { aborted: true };
    return { error: { status: null, message: error?.name === "AbortError" ? "Request timed out" : "Network error" } };
  } finally {
    clearTimeout(expiry);
    signal?.removeEventListener("abort", abort);
  }
}

function applyRows(items, drafts, activeId) {
  const openChanged = openPromptId !== activeId;
  openPromptId = activeId;
  const pending = pendingByPrompt(drafts);
  const rows = items.map((item) => {
    const override = isLocalOverride(item);
    const pendingCount = pending.get(item.name) ?? 0;
    const title = promptFileName(item.path, item.name);
    return {
      id: item.path,
      path: item.path,
      name: item.name,
      title,
      state: override ? "local" : "default",
      repo: item.level ?? "",
      priority: "",
      number: versionLabel(item),
      version: versionLabel(item),
      versionNum: versionNumber(item),
      override,
      overrideFlag: override ? "Local overrides" : "",
      pending: pendingCount,
      pendingFlag: pendingCount > 0 ? "Pending changes" : "",
      item,
    };
  });
  promptList.options.total = rows.length;
  if (openChanged || JSON.stringify(rows) !== JSON.stringify(promptList.state.rows)) promptList.setRows(rows);
}

async function readUsedBy(api, signal) {
  const [adwListRes, libRes] = await Promise.all([
    fetchJson(api.adws, signal, false),
    libraryStepsCache ? Promise.resolve({ data: { item: { content: { step: libraryStepsCache } } } })
      : fetchJson(api.library, signal, false),
  ]);
  if (signal?.aborted) return null;
  const librarySteps = Array.isArray(libRes.data?.item?.content?.step) ? libRes.data.item.content.step : [];
  if (librarySteps.length) libraryStepsCache = librarySteps;
  const listed = Array.isArray(adwListRes.data?.items) ? adwListRes.data.items : [];
  const names = listed.length ? listed : (librarySteps.length ? BUILTIN_ADWS.map((name) => ({ name })) : []);
  const detailed = await Promise.all(names.map(async (entry) => {
    if (Array.isArray(entry.nodes) || typeof entry.content === "string") return entry;
    const cacheKey = `${entry.name}:${entry.content_version || entry.version || ""}`;
    if (adwCache.has(cacheKey)) return adwCache.get(cacheKey);
    const res = await fetchJson(api.adw(entry.name), signal, true);
    const item = res.data?.item;
    if (item) adwCache.set(cacheKey, item);
    return item ?? null;
  }));
  if (signal?.aborted) return null;
  return extractUsedBy(detailed.filter(Boolean), librarySteps);
}

async function readDetail(api, routePath, items, loops, signal) {
  const loopTarget = parseLoopTarget(routePath, loops);
  if (loopTarget) {
    if (!loopTarget.name) {
      return { kind: loopTarget.kind, target: loopTarget, detail: { kind: loopTarget.kind, path: loopTarget.path, content: "" }, error: null };
    }
    let res = await fetchJson(api.loopFile(loopTarget.name, loopTarget.file, loopTarget.level), signal, true);
    if (res.error?.status === 404 && !loopTarget.level) {
      res = await fetchJson(api.loopFile(loopTarget.name, loopTarget.file, "engine"), signal, true);
    }
    return { kind: loopTarget.kind, target: loopTarget, detail: res.data?.item ?? null, error: res.error ?? null };
  }
  const matched = matchPromptItem(items, routePath);
  const name = matched?.name || normalizePromptName(routePath);
  if (!name) return { kind: "prompt", target: null, detail: null, error: null };
  const level = matched?.level || "";
  let res = await fetchJson(api.item(name, level), signal, true);
  if (res.error?.status === 404 && level) {
    res = await fetchJson(api.item(name, ""), signal, true);
  }
  return { kind: "prompt", target: matched, detail: res.data?.item ?? null, error: res.error ?? null };
}

async function loadPrompts(route, background = false) {
  const api = routes();
  const routePath = promptPath(route);
  const generation = ++readGeneration;
  activeController?.abort();
  const controller = activeController = new AbortController();
  const { signal: abortSignal } = controller;
  const previous = promptsView.peek();
  const pathChanged = routePath !== previous.selectedPath;
  if (!background && pathChanged) {
    promptsView.value = { ...previous, selectedPath: routePath, detailError: null, detailLoading: Boolean(routePath) };
  }
  const [listRes, draftsRes, loopsRes, usedBy] = await Promise.all([
    fetchJson(api.list, abortSignal, false),
    fetchJson(api.drafts, abortSignal, false),
    fetchJson(api.loops, abortSignal, false),
    readUsedBy(api, abortSignal),
  ]);
  if (abortSignal.aborted || generation !== readGeneration) return;
  const items = Array.isArray(listRes.data?.items) ? listRes.data.items : previous.items;
  const drafts = Array.isArray(draftsRes.data?.drafts) ? draftsRes.data.drafts : previous.drafts;
  const loops = Array.isArray(loopsRes.data?.items) ? loopsRes.data.items : previous.loops;
  const listError = listRes.error ?? null;
  const matched = matchPromptItem(items, routePath);
  const activeId = matched?.path ?? routePath;
  applyRows(items, drafts, activeId);
  if (!routePath && items.length) {
    const first = promptList.rows()[0] ?? items[0];
    if (first && typeof location !== "undefined") {
      promptsView.value = { ...promptsView.peek(), items, loops, drafts, usedBy: usedBy ?? previous.usedBy, listError, settled: true };
      location.replace(api.hash(first.path));
      return;
    }
  }
  const resolved = routePath ? await readDetail(api, routePath, items, loops, abortSignal)
    : { kind: "prompt", target: null, detail: null, error: null };
  if (abortSignal.aborted || generation !== readGeneration) return;
  promptsView.value = {
    items,
    loops,
    drafts,
    usedBy: usedBy ?? previous.usedBy,
    selectedPath: routePath,
    selectedKind: resolved.kind,
    target: resolved.target,
    detail: resolved.detail ?? (pathChanged ? null : previous.detail),
    listError,
    detailError: resolved.error,
    settled: true,
    detailLoading: false,
  };
  if (pathChanged && typeof document !== "undefined") {
    const focus = document.getElementById("pane-focus");
    if (focus) focus.scrollTop = 0;
  }
}

export function stopPrompts() {
  activeController?.abort();
  activeController = null;
  if (refreshTimer != null) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
  currentRoute = null;
}

export function syncPrompts(route) {
  if (route?.screen !== "prompts") {
    stopPrompts();
    return;
  }
  const sameHash = currentRoute?.hash === route.hash;
  currentRoute = route;
  if (!sameHash) loadPrompts(route, false);
  if (refreshTimer == null && typeof setInterval !== "undefined") {
    refreshTimer = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      if (currentRoute?.screen === "prompts") loadPrompts(currentRoute, true);
    }, REFRESH_MS);
  }
}

export function retryPrompts() {
  if (currentRoute?.screen === "prompts") loadPrompts(currentRoute, false);
}

class WatchingPrompts extends Component {
  state = { revision: 0 };
  componentDidMount() {
    this.unsubscribe = promptList.subscribe((before) => {
      if (!before) this.setState({ revision: this.state.revision + 1 });
    });
    this.settle?.();
  }
  componentDidUpdate() { this.settle?.(); }
  componentWillUnmount() { this.unsubscribe?.(); }
}

export function PromptCrumbs({ route, view }) {
  const state = view ?? promptsView.value;
  const activePath = promptPath(route) || state.detail?.path;
  const name = activePath ? promptFileName(activePath, state.detail?.name) : "";
  return html`<nav class="pr-crumbs" aria-label="Breadcrumb">
    <a href="#floor">Arete</a>
    <span class="pr-crumb-sep" aria-hidden="true">/</span>
    <a href="#prompts">Prompts</a>
    ${name ? html`<span class="pr-crumb-sep" aria-hidden="true">/</span><span class="pr-crumb-cur mono">${name}</span>` : null}
  </nav>`;
}

export function PromptSubtitle({ view }) {
  const state = view ?? promptsView.value;
  if (!state.settled && !state.items.length) return "Prompt library";
  const { total, overrides, pending } = promptCounts(state.items, state.drafts);
  return `${total} ${total === 1 ? "prompt" : "prompts"} · ${overrides} local override${overrides === 1 ? "" : "s"} · ${pending} pending`;
}

export class PromptQueue extends WatchingPrompts {
  settle() {
    if (!this.base) return;
    this.base.uiCreate = notifyNewPrompt;
    syncList(this.base);
    const active = this.activeId();
    if (active && this.shown !== active) {
      this.shown = active;
      selectItem("prompts-list", active, pane.peek());
    }
  }
  activeId() {
    const state = this.props.view ?? promptsView.value;
    const routePath = promptPath(this.props.route);
    const matched = matchPromptItem(state.items, routePath);
    return matched?.path ?? routePath;
  }
  render({ route, view, retry }) {
    const state = view ?? promptsView.value;
    const api = routes();
    const active = this.activeId();
    const rows = promptList.rows();
    const total = promptList.state.rows.length;
    const query = promptList.state.query.trim().toLowerCase();
    const loopRows = (state.loops ?? []).filter((item) => !query || `${item.name} ${item.path}`.toLowerCase().includes(query) || item.path === active);
    const countLabel = rows.length === total ? String(rows.length) : `${rows.length} of ${total}`;
    const hasCached = state.items.length > 0;
    return html`<div class="ui-list pr-queue" data-list="prompts-list">
      <${ListHeader} controller=${promptList} queue=${true} id="prompts-list" />
      ${state.listError && hasCached ? html`<${Fault} what="Prompts" fault=${state.listError} retry=${retry} />` : null}
      <div class="ui-list-scroll">
        <${Gate} pending=${!state.settled} fault=${hasCached ? null : state.listError} what="Prompts" retry=${retry}>
          <div class="pr-group-head"><span>Prompts</span><span class="pr-group-count">${countLabel}</span></div>
          ${rows.length ? rows.map((row) => {
            const selected = row.path === active;
            return html`<a key=${row.path} class=${`pr-row${selected ? " selected" : ""}`} href=${api.hash(row.path)}
              title=${row.path} data-row="" data-item=${row.path} aria-current=${selected ? "page" : undefined}>
              <span class="pr-row-name">${row.title}</span>
              ${row.pending > 0 ? html`<span class="pr-chip pr-pending">◆ ${row.pending}</span>` : null}
              ${row.override ? html`<span class="pr-chip pr-plain">local</span>` : null}
              <span class="pr-row-ver">${row.version}</span>
            </a>`;
          }) : html`<${Empty} what=${total ? "No matching prompts. Clear search or remove a filter." : "No prompt files found"} />`}
          ${loopRows.length ? html`<div class="pr-group-head"><span>Loop files</span><span class="pr-group-count">${loopRows.length}</span></div>
            ${loopRows.map((item) => {
              const selected = item.path === active;
              return html`<a key=${item.path} class=${`pr-row${selected ? " selected" : ""}`} href=${api.hash(item.path)}
                title=${item.path} data-row="" data-item=${item.path} data-kind="loop" aria-current=${selected ? "page" : undefined}>
                <span class="pr-row-name">${item.name}/${promptFileName(item.path)}</span>
                <span class="pr-chip pr-plain">loop</span>
                <span class="pr-row-ver">${versionLabel(item)}</span>
              </a>`;
            })}` : null}
        <//>
      </div>
    </div>`;
  }
}

function PromptViewer({ content }) {
  const lines = String(content ?? "").split("\n");
  return html`<section class="pr-sec">
    <div class="pr-sec-label">Text</div>
    <div class="pr-viewer" data-viewer="prompt" role="region" aria-label="Prompt text">
      ${lines.map((line, index) => html`<div class="pr-line" key=${index}>
        <span class="pr-ln" aria-hidden="true">${index + 1}</span>
        <span class="pr-code">${line}</span>
      </div>`)}
    </div>
  </section>`;
}

function NonPromptPanel({ detail, target }) {
  const meta = extractLoopMeta(detail, target);
  const path = detail?.path || target?.path || "";
  const title = promptFileName(path, detail?.name || target?.name);
  const ver = versionLabel(detail);
  const kind = detail?.kind || target?.kind || "file";
  return html`<article class="pr-main" data-readonly="true">
    <header class="pr-head">
      <div class="pr-meta">
        <span class="pr-chip pr-plain">read only</span>
        <span>${path}</span>
        ${ver ? html`<span>${ver}</span>` : null}
        <span>${kind}</span>
      </div>
      <h2 class="pr-title">${title}</h2>
    </header>
    <div class="pr-readonly">
      <dl class="pr-kv">
        <dt>Description</dt><dd>${meta.description}</dd>
        <dt>Used by</dt><dd>${meta.usedBy}</dd>
        <dt>Lines</dt><dd>${meta.lines}</dd>
        <dt>Updated</dt><dd>${meta.updated}</dd>
      </dl>
    </div>
    ${typeof detail?.content === "string" && detail.content ? html`<${PromptViewer} content=${detail.content} />` : null}
  </article>`;
}

export function PromptFocus({ route, view, retry }) {
  const state = view ?? promptsView.value;
  const routePath = promptPath(route);
  if (!routePath) {
    if (!state.settled) return html`<div class="pr-main"><${Loading} what="prompt" /></div>`;
    return html`<div class="pr-main"><${Empty} what="Select a prompt from the list" /></div>`;
  }
  if (state.detailLoading && !state.detail) {
    return html`<div class="pr-main"><${Loading} what=${promptFileName(routePath)} /></div>`;
  }
  if (state.detailError && !state.detail) {
    return html`<div class="pr-main"><${Fault} what=${promptFileName(routePath)} fault=${state.detailError} retry=${retry} /></div>`;
  }
  if (!state.detail) {
    return html`<div class="pr-main"><${Empty} what="Select a prompt from the list" /></div>`;
  }
  if (state.selectedKind !== "prompt" || (state.detail.kind && state.detail.kind !== "prompt")) {
    return html`<${NonPromptPanel} detail=${state.detail} target=${state.target} />`;
  }
  const detail = state.detail;
  const override = isLocalOverride(detail);
  const ver = versionLabel(detail);
  const title = promptFileName(detail.path, detail.name);
  const used = state.usedBy?.[detail.name] ?? state.usedBy?.[normalizePromptName(detail.path)] ?? [];
  const runsText = Number.isFinite(detail.runs7d) ? `${detail.runs7d} runs in 7d` : "0 runs in 7d";
  const passText = Number.isFinite(detail.passRate) ? `pass ${detail.passRate}%` : "pass —";
  return html`<article class="pr-main" data-prompt=${detail.name || title}>
    ${state.detailError ? html`<${Fault} what=${title} fault=${state.detailError} retry=${retry} />` : null}
    <header class="pr-head">
      <div class="pr-meta">
        ${override ? html`<span class="pr-chip pr-plain">■ local override</span>` : html`<span class="pr-chip pr-plain">default</span>`}
        <span>${detail.path}</span>
        ${ver ? html`<span>live ${ver}</span>` : null}
        ${detail.drift ? html`<span>↑ ahead of main</span>` : null}
        <span>${detail.kind || "prompt"}</span>
      </div>
      <h2 class="pr-title">${title}</h2>
      <div class="pr-meta">
        <span>used by ${used.length ? used.join(", ") : "—"}</span>
        <span>${runsText}</span>
        <span>${passText}</span>
      </div>
    </header>
    <${PromptViewer} content=${detail.content} />
  </article>`;
}
