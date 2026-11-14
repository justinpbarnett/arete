import { h, Component } from "./vendor-preact.js";
import { signal, effect } from "./vendor-signals.js";
import { parse } from "./ui-router.js";
import { fuzzy } from "./ui-fuzzy.js";

export const pickerModes = {
  steps: ["Add a step…", "add"], triggers: ["Add a trigger…", "add"],
  prompts: ["Choose a prompt…", "choose"], roles: ["Choose a role…", "choose"],
  drop: ["Add a compatible step…", "add + wire"], wires: ["Wire to…", "wire"], create: ["Create…", "create"],
};
export const palette = signal(null);
const snapshots = new Map();
let navigation = [];
let opener = null;
let prepare = () => {};
let leave = () => {};

export function configurePalette(rows, beforeOpen = () => {}, beforeClose = () => {}) {
  navigation = rows.map((row) => ({ ...row, group: "Go to" }));
  prepare = beforeOpen;
  leave = beforeClose;
}

function source(state) {
  if (state.mode) return state.rows;
  const screen = parse(location.hash).screen;
  return [...navigation, ...[...snapshots.values()].flatMap((entry) => entry.items.map((row) => ({ ...row, group: "Items" }))),
    ...(snapshots.get(screen)?.commands ?? []).map((row) => ({ ...row, group: "Commands" }))];
}

function refresh(change = {}, reset = false) {
  const previous = palette.peek();
  if (!previous) return;
  const state = { ...previous, ...change };
  state.groups = fuzzy(source(state), state.query);
  state.matches = state.groups.flatMap(([, rows]) => rows);
  state.index = reset ? 0 : Math.max(0, state.matches.findIndex((row) => row.id === previous.matches[previous.index]?.id));
  palette.value = state;
}

export function register(screen, items = [], commands = []) {
  const entry = { items: [...items], commands: [...commands] };
  snapshots.set(screen, entry);
  refresh();
  return () => {
    if (snapshots.get(screen) !== entry) return;
    snapshots.delete(screen);
    refresh();
  };
}

export function openPalette({ mode = "", rows = [], current, onPick } = {}) {
  if (mode && !pickerModes[mode]) return false;
  if (!palette.peek()) opener = document.activeElement;
  prepare();
  let choices = rows.map((row) => ({ ...row }));
  if (mode === "prompts" && !choices.some((row) => row.id === "none")) {
    choices.unshift({ id: "none", title: "none", description: "a script step, no prompt", glyph: "○", group: "None" });
  }
  if (mode === "roles" && !choices.some((row) => row.id === "default")) {
    choices.unshift({ id: "default", title: "Default", description: "this step's own role", glyph: "○", group: "Default" });
  }
  if (mode === "prompts") {
    const order = ["None", "This ADW", "Other ADWs", ...new Set(choices.map((row) => row.group))];
    choices.sort((a, b) => order.indexOf(a.group) - order.indexOf(b.group));
  }
  if (current !== undefined) choices = choices.map((row) => ({ ...row, current: row.id === current }));
  const state = { mode, rows: choices, query: "", index: 0, matches: [], onPick };
  state.groups = fuzzy(source(state));
  state.matches = state.groups.flatMap(([, list]) => list);
  state.index = Math.max(0, state.matches.findIndex((row) => current !== undefined ? row.id === current : row.current));
  palette.value = state;
  return true;
}

export function closePalette() {
  if (!palette.peek()) return;
  palette.value = null;
  leave();
  const back = opener;
  opener = null;
  if (back?.isConnected) back.focus();
}

export function paletteRouteChanged() {
  if (palette.peek()?.mode) closePalette();
  else refresh();
}

export function queryPalette(query) { refresh({ query }, true); }

export function runPalette(index = palette.peek()?.index) {
  const state = palette.peek();
  const row = state?.matches[index];
  if (!row) return;
  closePalette();
  if (state.onPick) state.onPick(row);
  else if (row.run) row.run(row);
  else if (row.href) location.hash = row.href;
}

export function paletteKey(event, writable = false) {
  const key = event.key;
  if ((event.metaKey && key.toLowerCase() === "k") || (!palette.peek() && key === ":" && !writable && !event.ctrlKey && !event.altKey && !event.metaKey)) {
    openPalette();
    event.preventDefault();
    return true;
  }
  const state = palette.peek();
  if (!state) return false;
  if (key === "Escape") closePalette();
  else if (key === "Enter") runPalette();
  else if ((!event.metaKey && !event.altKey && (key === "j" || key === "k") && !event.ctrlKey)
    || (event.ctrlKey && (key === "n" || key === "p")) || key === "ArrowDown" || key === "ArrowUp") {
    const delta = key === "j" || key === "n" || key === "ArrowDown" ? 1 : -1;
    palette.value = { ...state, index: Math.max(0, Math.min(state.matches.length - 1, state.index + delta)) };
  } else if (key === "Tab") document.querySelector(".ui-palette input")?.focus();
  else return true;
  event.preventDefault();
  return true;
}

export class Palette extends Component {
  state = { revision: 0 };
  componentDidMount() {
    this.unsubscribe = effect(() => { palette.value; this.setState({ revision: (this.state.revision ?? 0) + 1 }); });
  }
  componentWillUnmount() { this.unsubscribe(); }
  componentDidUpdate() {
    const state = palette.peek();
    if (state && !this.wasOpen) this.base.querySelector("input")?.focus();
    if (state) this.base.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
    this.wasOpen = Boolean(state);
  }
  render() {
    const state = palette.peek();
    if (!state) return null;
    let index = 0;
    const label = pickerModes[state.mode]?.[0] ?? "Go to, find an item, run a command…";
    return h("div", { class: "ui-palette-scrim", onClick: (event) => { if (event.target === event.currentTarget) closePalette(); } },
      h("section", { class: "ui-palette", role: "dialog", "aria-modal": "true", "aria-label": label },
        h("div", { class: "ui-palette-head" }, h("span", { "aria-hidden": "true" }, ":"),
          h("input", { value: state.query, placeholder: label, "aria-label": label, role: "combobox", "aria-expanded": "true",
            "aria-controls": "palette-results", "aria-activedescendant": state.matches.length ? `palette-row-${state.index}` : undefined,
            spellcheck: false, autoComplete: "off", onInput: (event) => queryPalette(event.currentTarget.value) }), h("kbd", null, "Esc")),
        h("div", { class: "ui-palette-results", id: "palette-results", role: "listbox", "aria-label": "Results" },
          state.groups.map(([group, rows]) => h("div", { key: group, role: "group", "aria-label": group },
            h("div", { class: "ui-palette-group" }, group, h("span", null, rows.length)),
            rows.map((row) => {
              const at = index++;
              return h("div", { key: row.id, id: `palette-row-${at}`, class: "ui-palette-row", role: "option",
                "aria-selected": state.index === at, onClick: () => runPalette(at) },
              h("span", { class: "ui-palette-glyph", "aria-hidden": "true" }, row.glyph ?? (group === "Go to" ? "→" : "◇")),
              h("span", { class: "ui-palette-title" }, row.title),
              h("span", { class: "ui-palette-description" }, row.description),
              row.current ? h("span", { class: "muted" }, "current") : null,
              row.key ? h("kbd", null, row.key) : null);
            }))),
          !state.matches.length && h("p", { class: "ui-palette-empty" }, "No matching results")),
        h("footer", { class: "ui-palette-foot" }, h("kbd", null, "^N ^P"), " move", h("kbd", null, "Enter"),
          ` ${pickerModes[state.mode]?.[1] ?? "run"}`, h("kbd", null, "Esc"), " close", h("span", { class: "ui-palette-open" }, ": or Cmd K open"))));
  }
}
