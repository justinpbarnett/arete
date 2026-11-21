import { h, Component } from "./vendor-preact.js";
import { Menu } from "./ui-menu.js";
import { syncList, selectItem, selectedItem, pane } from "./ui-keys.js";
import { actions, perform } from "./ui-actions.js";
import { Empty } from "./ui-states.js";

export const shortCount = (n, total) => total == null ? String(n) : `${n}/${total}`;
export const longCount = (n, total) => total == null ? String(n) : `${n} of ${total}`;
const textOf = (row) => [row.id, row.title, row.state, row.repo, row.priority, row.number].join(" ").toLowerCase();
const compare = (a, b) => typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b));

export function createList(options) {
  const listeners = new Set();
  const state = { rows: [...options.rows], query: "", facets: {}, group: options.group ?? "", sort: options.sort ?? "source", toggle: true,
    detail: null, draft: false, stale: {}, parked: [] };
  for (const [key, values] of Object.entries(options.selected ?? {})) state.facets[key] = [...values];
  const update = (change) => {
    for (const listener of listeners) listener(true);
    Object.assign(state, change);
    for (const listener of listeners) listener(false);
  };
  const matches = (row, omit) => textOf(row).includes(state.query.toLowerCase())
    && (!options.toggle || state.toggle || options.toggle.matches(row))
    && Object.entries(state.facets).every(([key, values]) => key === omit || !values.length || values.includes(row[key]));
  const api = {
    options, state,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    setRows(rows) { update({ rows: [...rows] }); },
    setQuery(query) { update({ query }); },
    setSort(sort) { update({ sort }); },
    setGroup(group) { update({ group }); },
    setToggle(toggle) { update({ toggle }); },
    stale(id, current) { update({ stale: { ...state.stale, [id]: current ?? {} } }); },
    counts() {
      const high = state.rows.filter((row) => row.priority === "HIGH").length;
      const med = state.rows.filter((row) => row.priority === "MED").length;
      return { total: state.rows.length, high, med, needsYou: high + med };
    },
    resolve(row) {
      const visible = api.groups().flatMap(([, rows]) => rows);
      const at = visible.findIndex((one) => one.id === row.id);
      const next = visible[at + 1] ?? visible[at - 1];
      const index = state.rows.findIndex((one) => one.id === row.id);
      update({ rows: state.rows.filter((one) => one.id !== row.id) });
      return { next: next?.id, undo: () => {
        if (state.rows.some((one) => one.id === row.id)) return;
        const rows = [...state.rows];
        rows.splice(index, 0, row);
        update({ rows });
      } };
    },
    insert(row) {
      update({ rows: [{ ...row, new: true }, ...state.rows.filter((one) => one.id !== row.id)] });
      return { next: row.id, undo: () => update({ rows: state.rows.filter((one) => one.id !== row.id) }) };
    },
    park(row, group) {
      const transition = api.resolve(row);
      update({ parked: [...state.parked, { ...row, parked: group }] });
      return { ...transition, undo: () => {
        update({ parked: state.parked.filter((one) => one.id !== row.id) });
        transition.undo();
      } };
    },
    pick(key, value) {
      const values = state.facets[key] ?? [];
      update({ facets: { ...state.facets, [key]: values.includes(value) ? values.filter((v) => v !== value) : [...values, value] } });
    },
    facetCount(key, value) { return state.rows.filter((row) => matches(row, key) && row[key] === value).length; },
    rows() {
      const rows = state.rows.filter((row) => row.new || matches(row));
      const sort = options.sorts?.find((one) => one.id === state.sort);
      if (sort?.key) rows.sort((a, b) => (sort.direction ?? 1) * compare(a[sort.key], b[sort.key]) || compare(a.id, b.id));
      return [...rows.filter((row) => row.new), ...rows.filter((row) => !row.new)];
    },
    groups() {
      const rows = api.rows();
      if (!state.group) return [["", rows]];
      const buckets = new Map();
      for (const row of rows.filter((one) => !one.new)) {
        const key = row[state.group];
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(row);
      }
      const rank = { HIGH: 0, MED: 1, LOW: 2 };
      const groups = [...buckets].sort(([a], [b]) => state.group === "priority" ? compare(rank[a] ?? 3, rank[b] ?? 3) : compare(a, b));
      return rows.some((row) => row.new) ? [["New", rows.filter((row) => row.new)], ...groups] : groups;
    },
    open(row) { update({ detail: row, draft: false }); options.onOpen?.(row); },
    create() { update({ draft: true, detail: null }); options.onCreate?.(); },
    dismiss() {
      if (!state.draft && !state.detail) return false;
      update({ draft: false, detail: null });
      return true;
    },
  };
  return api;
}

export function listContext(list, listId, id) {
  const item = list.state.rows.find((one) => one.id === id);
  const hash = location.hash;
  const originPane = pane.peek();
  const originItem = item?.id ?? selectedItem(listId);
  const adapter = list.options.actions;
  if (!adapter) return null;
  let successor;
  return {
    item, listId, compatible: adapter.compatible ?? listId, primary: adapter.primary,
    request: (action, reason) => adapter.request(action, item, reason),
    inverse: adapter.inverse ? (action, result, transition) => adapter.inverse(action, item, result, transition) : null,
    stale: (current) => list.stale(item?.id, current),
    commit(action, result, reason) {
      const transition = action.name === "create" ? list.insert(adapter.created(result, reason))
        : ["snooze", "shelve"].includes(action.name) ? list.park(item, action.name === "snooze" ? "Snoozed" : "Shelved") : list.resolve(item);
      successor = transition.next;
      selectItem(listId, transition.next, originPane);
      return { ...transition, label: adapter.label?.(action, item, result, reason) };
    },
    restore() {
      location.hash = hash;
      const selected = list.state.rows.find((row) => row.id === originItem)?.id
        ?? list.rows().find((row) => row.id === successor)?.id ?? list.rows()[0]?.id;
      selectItem(listId, selected, originPane);
    },
  };
}

function choice(label, checked, action, count) {
  return h("button", { type: "button", role: "menuitemcheckbox", "aria-checked": checked, onClick: action },
    h("span", null, `${checked ? "✓" : "　"} ${label}`), count == null ? null : h("b", null, count));
}

export function ListHeader({ controller: list, queue = false, id }) {
  const { options: o, state: s } = list;
  const n = list.rows().length;
  const group = o.groups?.find((one) => one.id === s.group)?.label ?? "None";
  const sort = o.sorts?.find((one) => one.id === s.sort)?.label ?? "Source order";
  const total = o.total;
  const chips = Object.entries(s.facets).flatMap(([key, values]) => values.map((value) =>
    h("button", { key: `${key}:${value}`, class: "ui-list-chip", title: `Remove ${value}`, "aria-label": `Remove ${value}`,
      onClick: () => list.pick(key, value) }, h("span", null, value), h("span", { "aria-hidden": true }, "×"))));
  return h("div", { class: "ui-list-header" },
    o.groups?.length ? h(Menu, { key: "group", label: `Group: ${group}`, title: "Group" }, (close) => [
      choice("None", !s.group, () => { list.setGroup(""); close(); }),
      ...o.groups.map((one) => choice(one.label, s.group === one.id, () => { list.setGroup(one.id); close(); }))]) : null,
    o.facets?.length ? h(Menu, { key: "filter", label: "Filter", title: "Filter" }, () => o.facets.map((facet) =>
      h("div", { key: facet.key }, h("div", { class: "ui-menu-label" }, facet.label),
        facet.values.map((value) => choice(value, (s.facets[facet.key] ?? []).includes(value),
          () => list.pick(facet.key, value), list.facetCount(facet.key, value)))))) : null,
    chips.length ? h("div", { key: "chips", class: "ui-list-chips" }, chips) : null,
    o.toggle ? h("button", { key: "toggle", type: "button", "aria-pressed": s.toggle, onClick: () => list.setToggle(!s.toggle) }, o.toggle.label) : null,
    h("input", { key: "search", type: "search", "data-search": "", "aria-label": "Search list", placeholder: "/ search", value: s.query,
      spellcheck: false, autoComplete: "off", onInput: (event) => list.setQuery(event.currentTarget.value) }),
    h("span", { key: "count", class: "ui-list-count", title: longCount(n, total) }, shortCount(n, total)),
    o.sorts?.length ? h(Menu, { key: "sort", label: queue ? "⇅" : "Sort", title: `Sort: ${sort}` }, (close) =>
      o.sorts.map((one) => choice(one.label, s.sort === one.id, () => { list.setSort(one.id); close(); }))) : null,
    o.onCreate ? h("button", { key: "new", type: "button", "data-primary-candidate": "",
      onClick: () => o.actions ? perform(actions.create, listContext(list, id)) : list.create() }, "[c] New") : null);
}

export function ListRow({ row, onOpen }) {
  const glyph = { "Needs you": "!", Running: "▷", Done: "✓", Fault: "×" }[row.state] ?? "○";
  return h("div", { class: "ui-list-row", "data-row": "", "data-item": row.id, onClick: () => onOpen(row) },
    h("span", { class: "ui-list-state" }, `${glyph} ${row.state}`),
    h("span", { class: "ui-list-title", title: row.title }, row.new ? `+ ${row.title} · new` : row.title),
    h("span", { class: "ui-list-number" }, row.number));
}

export function normalizePrimary(root = document) {
  const buttons = [...root.querySelectorAll(".ui-app button.primary, .ui-app button[data-primary-candidate]")];
  const chosen = buttons.find((button) => !button.closest('[data-pane="queue"]'));
  for (const button of buttons) {
    button.setAttribute("data-primary-candidate", "");
    button.classList.toggle("primary", button === chosen);
  }
}

export class List extends Component {
  state = { revision: 0 };
  remember = () => {
    const el = this.base.querySelector(".ui-list-scroll");
    this.offset = [el.scrollTop, el.scrollLeft];
  };
  componentDidMount() {
    this.unsubscribe = this.props.controller.subscribe((before) => {
      if (before) this.remember();
      else this.setState({ revision: (this.state.revision ?? 0) + 1 });
    });
    this.commit();
  }
  componentDidUpdate() { this.commit(); }
  componentWillUnmount() { this.unsubscribe(); }
  commit() {
    const list = this.props.controller;
    this.base.uiCreate = list.options.onCreate ? () => list.create() : null;
    if (list.options.actions) {
      this.base.uiContext = (id) => listContext(list, this.props.id, id);
      this.base.uiCreate = () => perform(actions.create, listContext(list, this.props.id));
    }
    this.base.uiDismiss = () => list.dismiss();
    syncList(this.base);
    if (this.offset) {
      const el = this.base.querySelector(".ui-list-scroll");
      [el.scrollTop, el.scrollLeft] = this.offset;
      this.offset = null;
    }
    normalizePrimary();
    if (list.state.draft && !this.wasDraft) this.base.querySelector(".ui-list-draft input")?.focus();
    this.wasDraft = list.state.draft;
  }
  render({ controller: list, id, queue }) {
    const { detail, draft } = list.state;
    return h("div", { class: "ui-list", "data-list": id },
      h(ListHeader, { controller: list, queue, id }),
      h("div", { class: "ui-list-scroll" }, list.rows().length ? list.groups().map(([name, rows]) =>
        h("div", { key: name }, name && h("div", { class: "ui-list-group" }, `${name} · ${rows.length}`),
          rows.map((row) => h("div", { key: row.id }, h(ListRow, { row, onOpen: (one) => list.open(one) }),
            Object.hasOwn(list.state.stale, row.id) ? h("div", { class: "ui-stale", role: "alert" },
              `Stale · current: ${JSON.stringify(list.state.stale[row.id])}`) : null))))
        : h(Empty, { what: list.state.rows.length || !list.options.empty ? "No matching items. Clear search or remove a filter." : list.options.empty }),
        ["Snoozed", "Shelved"].map((group) => {
          const rows = list.state.parked.filter((row) => row.parked === group);
          return rows.length ? h("details", { key: group }, h("summary", { class: "ui-list-group" }, `${group} · ${rows.length}`),
            rows.map((row) => h(ListRow, { key: row.id, row, onOpen: (one) => list.open(one) }))) : null;
        })),
      draft ? h("div", { class: "ui-list-draft" }, h("label", null, "New demo item ", h("input", { placeholder: "Title" })),
        h("button", { onClick: () => list.dismiss() }, "Close")) : null,
      detail ? h("div", { class: "ui-list-detail" }, h("strong", null, detail.title),
        h("p", null, `${detail.id} · ${detail.state} · ${detail.repo} · ${detail.priority}`),
        h("button", { onClick: () => list.dismiss() }, "Close")) : null);
  }
}
