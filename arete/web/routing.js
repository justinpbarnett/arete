"use strict";

const DETAIL_VIEW = { run: "runs", session: "sessions", loop: "loops", ticket: "triage", incident: "incidents" };
const OLD_HASHES = { launch: "#sessions", fleet: "#health", trends: "#outcomes", chat: "#live" };
const QUERY_RULES = {
  runs: {
    status: (v) => STATUS_FILTERS.some(([value]) => value === v), kind: Boolean, repo: Boolean, loop: Boolean,
    limit: (v) => /^[1-9]\d{0,2}$/.test(v),
  },
  triage: { group: Boolean },
  usage: { days: (v) => USAGE_WINDOWS.includes(v) },
};
let pendingWay = "";
let restoreAfterDraw = null;
let pendingDetailFocus = false;
let shownNeighbors = [null, null];

function viewHash(view, query = {}) {
  const kept = Object.entries(query).filter(([, value]) => value !== "" && value !== undefined && value !== null);
  return `#${view}${kept.length ? `?${new URLSearchParams(kept)}` : ""}`;
}

function detailHash(kind, id) {
  return `#${kind}/${encodeURIComponent(id)}`;
}

function listHash(view) {
  return viewHash(view, lastQuery[view] || {});
}

function rememberQuery(view, query) {
  lastQuery[view] = query;
  document.querySelector(`nav.side a[data-view="${view}"]`).href = listHash(view);
}

function settleQuery(query) {
  if (route.detail) return;
  const hash = viewHash(route.view, query);
  if (hash === route.hash) return;
  history.replaceState(history.state, "", hash);
  route = { ...route, query, hash };
  rememberQuery(route.view, query);
}

function parseRoute(hash) {
  const raw = (hash || "").replace(/^#/, "") || "live";
  const cut = raw.indexOf("?");
  const path = cut < 0 ? raw : raw.slice(0, cut);
  if (VIEWS[path]) {
    const rules = QUERY_RULES[path] ?? {};
    const query = Object.fromEntries([...new URLSearchParams(cut < 0 ? "" : raw.slice(cut + 1))]
      .filter(([key, value]) => Object.hasOwn(rules, key) && rules[key](value)));
    return { view: path, query, detail: null, hash: viewHash(path, query) };
  }
  const slash = path.indexOf("/");
  const kind = path.slice(0, slash);
  if (slash < 0 || !DETAIL_VIEW[kind]) return null;
  let id;
  try {
    id = decodeURIComponent(path.slice(slash + 1));
  } catch {
    return null;
  }
  if (!id) return null;
  return { view: DETAIL_VIEW[kind], query: {}, detail: { kind, id }, hash: detailHash(kind, id) };
}

function knownRoute(hash) {
  if (route && askIdIn((hash || "").replace(/^#/, ""))) return route;
  const old = OLD_HASHES[(hash || "").replace(/^#/, "")];
  return parseRoute(old ?? hash) ?? parseRoute("#live");
}

function go(hash, way, { replace = false } = {}) {
  if (openAskLink((hash || "").replace(/^#/, ""))) return;
  const next = parseRoute(hash);
  if (!next) return;
  if (next.hash === route.hash) {
    refresh();
    return;
  }
  history.replaceState({ ...history.state, focus: document.activeElement?.dataset?.focus || "", scroll: scrollY }, "");
  if (replace) history.replaceState({ from: history.state?.from ?? "" }, "", next.hash);
  else history.pushState({ from: route.hash }, "", next.hash);
  applyRoute(next, way);
}

function onHistory(event) {
  const state = event.type === "popstate" ? event.state : history.state;
  const asked = location.hash.replace(/^#/, "");
  const next = knownRoute(location.hash);
  if (next.hash !== location.hash) history.replaceState(state ?? { from: "" }, "", next.hash);
  if (asked === "chat") setConsole(true, { focus: true });
  openAskLink(asked);
  const way = pendingWay || (state ? "back" : "link");
  pendingWay = "";
  if (next.hash === route.hash) return;
  if (way !== "link" && state && "scroll" in state) restoreAfterDraw = { scroll: state.scroll, focus: state.focus };
  applyRoute(next, way);
}

function applyRoute(next, way) {
  const viewChanged = !route || next.view !== route.view;
  const closing = route?.detail && !next.detail && !viewChanged && (way === "close" || way === "back");
  if (closing) focusAfterDraw ||= `row:${route.detail.kind}:${route.detail.id}`;
  if (!next.detail) rememberQuery(next.view, next.query);
  route = next;
  if (stint?.name !== stintName()) {
    endStint();
    startStint(way);
  }
  show(viewChanged, way);
}

function currentSection() {
  return document.querySelector(`main > section[data-view="${route.view}"]`);
}

function currentPane() {
  return currentSection().querySelector(".detail");
}

function show(viewChanged, way) {
  const { view, detail } = route;
  const meta = VIEWS[view];
  document.title = withWaitingCount(detail ? `Arete · ${ITEM_LABELS[detail.kind]} ${detail.id}` : `Arete · ${meta.label}`);
  document.getElementById("title").textContent = meta.label;
  document.getElementById("sub").textContent = meta.sub;
  for (const a of document.querySelectorAll("nav.side a[data-view]")) {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  for (const section of document.querySelectorAll("main > section")) section.hidden = section.dataset.view !== view;
  for (const primary of document.querySelectorAll("[data-act]")) primary.classList.toggle("act", primary.closest("section")?.dataset.view === view);
  const section = currentSection();
  const split = section.querySelector(".split");
  let detailOpened = false;
  if (split) {
    split.classList.toggle("has-detail", Boolean(detail));
    const pane = split.querySelector(".detail");
    pane.hidden = !detail;
    const shows = detail ? `${detail.kind}:${detail.id}` : "";
    if (pane.dataset.shows !== shows) {
      detailOpened = Boolean(detail) && !pane.dataset.shows;
      pane.dataset.shows = shows;
      if (!detail) document.getElementById(`${pane.id}-head`).replaceChildren();
      document.getElementById(`${pane.id}-body`).replaceChildren(detail ? quiet("Loading") : "");
      document.getElementById(`${pane.id}-talk`).replaceChildren();
      pane.scrollTop = 0;
      pendingDetailFocus = Boolean(detail) && way !== "start" && way !== "return";
    }
  }
  for (const a of section.querySelectorAll("a[data-row]")) {
    const open = detail?.kind === a.dataset.row && detail.id === a.dataset.id;
    if (open) a.setAttribute("aria-current", "true");
    else a.removeAttribute("aria-current");
  }
  const listGone = split && getComputedStyle(split.querySelector(".list")).display === "none";
  if (!restoreAfterDraw && (viewChanged || (detailOpened && listGone))) scrollTo(0, 0);
  refresh();
}

function closeDetail() {
  if (!route.detail) return;
  if (history.state?.from && parseRoute(history.state.from)) {
    pendingWay = "close";
    history.back();
  } else {
    go(listHash(route.view), "close");
  }
}

function step(delta) {
  const id = shownNeighbors[delta < 0 ? 0 : 1];
  if (id && route.detail) go(detailHash(route.detail.kind, id), "step", { replace: true });
}

function rowIds(kind) {
  const list = currentSection().querySelector(".list");
  return list ? [...new Set([...list.querySelectorAll(`a[data-row="${kind}"]`)].map((a) => a.dataset.id))] : [];
}

function neighborsIn(ids, id) {
  const at = ids.indexOf(id);
  return at < 0 ? [null, null] : [ids[at - 1] ?? null, ids[at + 1] ?? null];
}

function detailHead(pane, kind, id, title, extra = [], neighbors = neighborsIn(rowIds(kind), id)) {
  shownNeighbors = neighbors;
  return [
    el("div", { class: "kicker", text: ITEM_LABELS[kind] }),
    el("h2", { id: `${pane}-title`, "data-focus": "detail-title", tabindex: "-1", text: title }),
    id !== title ? el("p", { class: "item-id muted", text: id }) : null,
    el("div", { class: "actions row-buttons" },
      button("Close", "close", closeDetail),
      button("Previous", "previous", () => step(-1), { disabled: !neighbors[0] }),
      button("Next", "next", () => step(1), { disabled: !neighbors[1] }),
      extra),
  ];
}

function focusByKey(key) {
  const node = key === "main" ? null : currentSection().querySelector(`[data-focus="${CSS.escape(key)}"]`)
    ?? document.querySelector(`nav.side [data-focus="${CSS.escape(key)}"]`);
  const target = node && node.checkVisibility() ? node : document.getElementById("main");
  target.focus({ preventScroll: true });
  const { top, bottom } = target.getBoundingClientRect();
  if (target !== document.getElementById("main") && (bottom < 0 || top > innerHeight)) target.scrollIntoView({ block: "center" });
}

function focusMark(node) {
  return node && node !== document.body ? { node, key: node.dataset?.focus ?? "" } : null;
}

function firstShown(selector) {
  return [...document.querySelectorAll(selector)].find((node) => node.checkVisibility());
}

function refocus(mark, fallback = null) {
  const again = mark?.node.isConnected ? mark.node : mark?.key ? firstShown(`[data-focus="${CSS.escape(mark.key)}"]`) : null;
  (again ?? fallback)?.focus();
}

function afterDraw() {
  putBackPlace();
  if (restoreAfterDraw) {
    const { scroll, focus } = restoreAfterDraw;
    restoreAfterDraw = null;
    if (typeof scroll === "number") scrollTo(0, scroll);
    if (!focusAfterDraw && focus) focusAfterDraw = focus;
  }
  if (pendingDetailFocus) {
    pendingDetailFocus = false;
    const pane = currentPane();
    if (pane && !pane.hidden && !pane.contains(document.activeElement) && !focusAfterDraw) focusByKey("detail-title");
  }
  if (focusAfterDraw) {
    const key = focusAfterDraw;
    focusAfterDraw = "";
    focusByKey(key);
  }
}
