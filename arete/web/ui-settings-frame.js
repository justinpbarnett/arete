import { Component, h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import { Menu } from "./ui-menu.js";
import { longCount, normalizePrimary } from "./ui-list.js";
import { mode, selectItem, syncList } from "./ui-keys.js";
import { pushUndo } from "./ui-undo.js";
import { failed, notice, succeeded } from "./ui-notice.js";
import { Empty } from "./ui-states.js";
import { registerSettingsSection } from "./ui-settings.js";

const html = htm.bind(h);

const formRegistry = new Map();
let createPicker = null;

export function registerSettingsForm(kind, descriptor) {
  if (!descriptor) {
    formRegistry.delete(kind);
    return;
  }
  formRegistry.set(kind, { kind, ...descriptor });
}

export function getSettingsForm(kind) {
  return formRegistry.get(kind) ?? null;
}

export function listSettingsForms() {
  return [...formRegistry.values()];
}

export function registerCreatePicker(handler) {
  createPicker = typeof handler === "function" ? handler : null;
}

export function openSettingsCreate(options = {}) {
  if (options.fromKey && createPicker) {
    const handled = createPicker(options);
    if (handled !== false) return true;
  }
  if (typeof options.openLocal === "function") {
    options.openLocal(options.kind ?? null);
    return true;
  }
  return false;
}

const compareValues = (a, b) => {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a ?? "").localeCompare(String(b ?? ""), undefined, { numeric: true });
};

function facetValuesOf(facet, row) {
  const raw = typeof facet.value === "function" ? facet.value(row) : row?.[facet.key];
  if (Array.isArray(raw)) return raw.filter((v) => v != null && v !== "").map(String);
  return raw != null && raw !== "" ? [String(raw)] : [];
}

function defaultSearchText(row, columns = []) {
  const parts = [row?.id, row?.name, row?.title, row?.scope];
  for (const col of columns) {
    if (typeof col.text === "function") parts.push(col.text(row));
    else if (col.key && row?.[col.key] != null) parts.push(row[col.key]);
  }
  return parts.filter((part) => part != null && part !== "").join(" ").toLowerCase();
}

export function createSettingsTable(options = {}) {
  const listeners = new Set();
  const state = {
    rows: [...(options.rows ?? [])],
    query: "",
    facets: {},
    sort: options.sort ?? options.sorts?.[0]?.id ?? "source",
    selectedId: null,
    stale: {},
  };
  for (const [key, values] of Object.entries(options.selected ?? {})) {
    state.facets[key] = [...values];
  }

  const emit = () => {
    for (const listener of listeners) listener();
  };

  const update = (patch) => {
    Object.assign(state, patch);
    emit();
  };

  const searchOf = (row) => (typeof options.searchText === "function"
    ? String(options.searchText(row) ?? "").toLowerCase()
    : defaultSearchText(row, options.columns));

  const matches = (row, omitKey = null) => {
    const query = state.query.trim().toLowerCase();
    if (query && !searchOf(row).includes(query)) return false;
    for (const facet of options.facets ?? []) {
      if (facet.key === omitKey) continue;
      const picked = state.facets[facet.key] ?? [];
      if (!picked.length) continue;
      const values = facetValuesOf(facet, row);
      if (!values.some((v) => picked.includes(v))) return false;
    }
    return true;
  };

  const sortRows = (rows) => {
    const def = (options.sorts ?? []).find((item) => item.id === state.sort);
    if (!def || def.id === "source") return rows;
    const copy = [...rows];
    copy.sort((a, b) => {
      if (typeof def.compare === "function") return def.compare(a, b) || compareValues(a.id, b.id);
      const av = typeof def.value === "function" ? def.value(a) : a?.[def.key ?? def.id];
      const bv = typeof def.value === "function" ? def.value(b) : b?.[def.key ?? def.id];
      return (def.direction ?? 1) * compareValues(av, bv) || compareValues(a.id, b.id);
    });
    return copy;
  };

  const api = {
    options,
    state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setOptions(nextOptions) {
      options = { ...options, ...nextOptions };
      api.options = options;
      if (Array.isArray(nextOptions.rows)) {
        state.rows = [...nextOptions.rows];
        const visible = api.rows();
        if (!visible.some((row) => row.id === state.selectedId)) {
          const picked = typeof options.initialSelected === "function" ? options.initialSelected(visible) : null;
          state.selectedId = (picked && visible.some((row) => row.id === picked) ? picked : visible[0]?.id) ?? null;
        }
      }
      emit();
    },
    setRows(rows) {
      api.setOptions({ rows });
    },
    setQuery(query) {
      update({ query: String(query ?? "") });
    },
    setSort(sort) {
      update({ sort });
    },
    pick(key, value) {
      const current = state.facets[key] ?? [];
      const next = current.includes(value) ? current.filter((v) => v !== value) : [...current, value];
      update({ facets: { ...state.facets, [key]: next } });
    },
    clearFilters() {
      update({ query: "", facets: {} });
    },
    facetValues(facet) {
      if (Array.isArray(facet.values) && facet.values.length) return facet.values;
      const seen = new Set();
      for (const row of state.rows) {
        for (const value of facetValuesOf(facet, row)) seen.add(value);
      }
      return [...seen];
    },
    facetCount(key, value) {
      const facet = (options.facets ?? []).find((item) => item.key === key);
      if (!facet) return 0;
      return state.rows.filter((row) => matches(row, key) && facetValuesOf(facet, row).includes(value)).length;
    },
    rows() {
      return sortRows(state.rows.filter((row) => matches(row)));
    },
    total() {
      return state.rows.length;
    },
    countText() {
      return longCount(api.rows().length, api.total());
    },
    select(id) {
      if (state.selectedId === id) return;
      update({ selectedId: id });
    },
    selectedRow() {
      const visible = api.rows();
      return visible.find((row) => row.id === state.selectedId) ?? visible[0] ?? null;
    },
    move(delta) {
      const visible = api.rows();
      if (!visible.length) return null;
      const at = Math.max(0, visible.findIndex((row) => row.id === state.selectedId));
      const next = visible[Math.max(0, Math.min(visible.length - 1, at + delta))];
      if (next) update({ selectedId: next.id });
      return next ?? null;
    },
    markStale(id, error) {
      const detail = error?.message || "This item changed. Refresh before trying again.";
      update({ stale: { ...state.stale, [id]: { message: detail, current: error?.current ?? null } } });
    },
    clearStale(id) {
      if (!Object.hasOwn(state.stale, id)) return;
      const next = { ...state.stale };
      delete next[id];
      update({ stale: next });
    },
  };

  const initialVisible = api.rows();
  const initialPick = typeof options.initialSelected === "function" ? options.initialSelected(initialVisible) : null;
  state.selectedId = (initialPick && initialVisible.some((row) => row.id === initialPick) ? initialPick : initialVisible[0]?.id) ?? null;

  return api;
}

export async function runTableRemove(table, row, reason, removePolicy = {}) {
  if (!row) return { ok: false, refused: true };
  if (row.removable === false) {
    const text = row.removeTitle || "This item cannot be removed.";
    notice.value = text;
    return { ok: false, refused: true, message: text };
  }
  if (typeof removePolicy.onRemove !== "function") return { ok: false, refused: true };
  table.clearStale(row.id);
  try {
    const outcome = await removePolicy.onRemove(row, reason ?? "") ?? {};
    const label = outcome.label || `Removed ${removePolicy.noun || "item"} ${row.name || row.id}`;
    const undoable = outcome.undoable ?? removePolicy.undoable ?? (typeof outcome.undo === "function" || typeof outcome.undo?.inverse === "function");
    const explanation = outcome.undoNotice ?? removePolicy.undoNotice ?? "This action cannot be undone.";
    const restore = outcome.restore ?? removePolicy.restore ?? null;
    if (undoable && (typeof outcome.undo === "function" || typeof outcome.undo?.inverse === "function")) {
      const inverseFn = typeof outcome.undo === "function" ? outcome.undo : outcome.undo.inverse;
      pushUndo({
        label,
        explanation: null,
        inverse: async () => {
          try {
            await inverseFn();
          } catch (error) {
            if (error?.conflict) table.markStale(row.id, error);
            throw error;
          }
        },
        restore,
      });
      succeeded(label, true);
    } else {
      pushUndo({
        label,
        explanation,
        inverse: null,
        restore,
      });
      succeeded(`${label} (cannot be undone)`, false);
    }
    return { ok: true, outcome };
  } catch (error) {
    if (error?.conflict) table.markStale(row.id, error);
    failed(error);
    return { ok: false, error };
  }
}

function menuChoice(label, checked, onClick, count) {
  return html`<button type="button" role="menuitemcheckbox" aria-checked=${checked ? "true" : "false"} onClick=${onClick}>
    <span>${`${checked ? "✓" : "　"} ${label}`}</span>
    ${count == null ? null : html`<b>${count}</b>`}
  </button>`;
}

export function SettingsTableHeader({ controller, hasCreate, onCreate, createLabel = "New" }) {
  const { options, state } = controller;
  const facets = options.facets ?? [];
  const sorts = options.sorts ?? [];
  const sortLabel = sorts.find((item) => item.id === state.sort)?.label ?? "Source order";
  const chips = Object.entries(state.facets).flatMap(([key, values]) => values.map((value) => html`
    <button key=${`${key}:${value}`} type="button" class="ui-list-chip"
      title=${`Remove ${value}`} aria-label=${`Remove ${value}`}
      onClick=${() => controller.pick(key, value)}>
      <span>${value}</span><span aria-hidden="true">×</span>
    </button>`));
  const count = controller.countText();
  return html`<div class="ui-list-header set-tbl-head">
    ${facets.length ? html`<${Menu} key="filter" label="Filter" title="Filter">
      ${() => facets.map((facet) => html`<div key=${facet.key}>
        <div class="ui-menu-label">${facet.label}</div>
        ${controller.facetValues(facet).map((value) => menuChoice(
          value,
          (state.facets[facet.key] ?? []).includes(value),
          () => controller.pick(facet.key, value),
          controller.facetCount(facet.key, value),
        ))}
      </div>`)}
    <//>` : null}
    ${chips.length ? html`<div key="chips" class="ui-list-chips">${chips}</div>` : null}
    <input key="search" type="search" data-search="" aria-label="Search table" placeholder="/ search"
      value=${state.query} spellcheck=${false} autocomplete="off"
      onInput=${(event) => controller.setQuery(event.currentTarget.value)} />
    <span key="count" class="ui-list-count" data-count="" title=${count}>${count}</span>
    ${sorts.length ? html`<${Menu} key="sort" label="Sort" title=${`Sort: ${sortLabel}`}>
      ${(close) => sorts.map((item) => menuChoice(
        item.label,
        state.sort === item.id,
        () => { controller.setSort(item.id); close(); },
        null,
      ))}
    <//>` : null}
    ${hasCreate ? html`<button key="new" type="button" class="set-new-btn" data-act="new" onClick=${onCreate}>
      <span>${createLabel}</span><kbd class="cap">c</kbd>
    </button>` : null}
  </div>`;
}

export class SettingsInsertForm extends Component {
  constructor(props) {
    super(props);
    this.inputs = new Map();
    this.state = { busy: false, error: "", values: this.initialValues(props.form) };
  }
  initialValues(form) {
    const values = {};
    for (const field of form?.fields ?? []) {
      if (field.type === "password") continue;
      values[field.name] = field.defaultValue ?? "";
    }
    return values;
  }
  componentDidMount() {
    this.focusFirst();
    mode.value = "INSERT";
  }
  componentWillUnmount() {
    this.clearSensitive();
  }
  focusFirst() {
    const first = (this.props.form?.fields ?? [])[0];
    const el = first ? this.inputs.get(first.name) : this.base?.querySelector("input");
    el?.focus?.();
  }
  clearSensitive() {
    const form = this.props.form;
    for (const field of form?.fields ?? []) {
      if (field.type === "password") {
        const el = this.inputs.get(field.name);
        if (el) el.value = "";
      }
    }
  }
  cancel = () => {
    this.clearSensitive();
    globalThis.document?.activeElement?.blur?.();
    mode.value = "NORMAL";
    this.props.onCancel?.();
  };
  submit = async (event) => {
    event.preventDefault();
    const { form, onSaved } = this.props;
    if (!form || this.state.busy) return;
    const values = {};
    for (const field of form.fields ?? []) {
      const el = this.inputs.get(field.name);
      if (field.type === "password") {
        values[field.name] = el ? el.value : "";
      } else {
        values[field.name] = el ? el.value : (this.state.values[field.name] ?? "");
      }
    }
    this.clearSensitive();
    if (typeof form.validate === "function") {
      const problem = form.validate(values);
      if (problem) {
        this.setState({ error: problem });
        return;
      }
    }
    this.setState({ busy: true, error: "" });
    try {
      const result = await form.submit(values);
      this.clearSensitive();
      globalThis.document?.activeElement?.blur?.();
      mode.value = "NORMAL";
      onSaved?.(result);
    } catch (error) {
      this.clearSensitive();
      this.setState({ error: error?.message || "Save failed." });
    } finally {
      this.clearSensitive();
      this.setState({ busy: false });
    }
  };
  render({ form }, { busy, error, values }) {
    if (!form) return null;
    if (typeof form.render === "function") {
      return form.render({ busy, error, onCancel: this.cancel, onSaved: this.props.onSaved });
    }
    const label = form.label || `NEW ${(form.noun || form.kind || "ITEM").toUpperCase()}`;
    const submitLabel = form.submitLabel || "Save";
    return html`<form class="ops-form set-create-bar" onSubmit=${this.submit} aria-label=${label}>
      <span class="set-lbl set-bar-lbl">${label}</span>
      ${(form.fields ?? []).map((field) => {
        const isSecret = field.type === "password";
        const inputProps = {
          key: field.name,
          name: field.name,
          type: field.type || "text",
          class: `set-inp set-bar-inp${field.className ? ` ${field.className}` : ""}`,
          placeholder: field.placeholder || field.label || field.name,
          "aria-label": field.ariaLabel || field.label || field.name,
          autocomplete: "off",
          spellcheck: false,
          disabled: busy,
          ref: (el) => { if (el) this.inputs.set(field.name, el); else this.inputs.delete(field.name); },
        };
        if (isSecret) {
          return html`<input ...${inputProps} />`;
        }
        return html`<input ...${inputProps} value=${values[field.name] ?? ""}
          onInput=${(event) => this.setState({ values: { ...this.state.values, [field.name]: event.currentTarget.value }, error: "" })} />`;
      })}
      <button type="submit" class="set-btn set-btn-primary" disabled=${busy}>
        <span>${busy ? "Saving…" : submitLabel}</span><kbd class="cap">⌘↵</kbd>
      </button>
      <button type="button" class="set-btn set-btn-ghost" disabled=${busy} onClick=${this.cancel}>
        <span>Cancel</span><kbd class="cap">Esc</kbd>
      </button>
      <span class="mono muted set-bar-hint">⌘↵ saves, Esc cancels</span>
      ${error ? html`<span class="set-status-err set-bar-err" role="alert">${error}</span>` : null}
    </form>`;
  }
}

class SettingsReasonBar extends Component {
  state = { text: "", busy: false, error: "" };
  componentDidMount() {
    this.input?.focus?.();
    mode.value = "INSERT";
  }
  cancel = () => {
    this.input?.blur?.();
    mode.value = "NORMAL";
    this.props.onCancel?.();
  };
  submit = async (event) => {
    event.preventDefault();
    if (this.state.busy) return;
    this.setState({ busy: true, error: "" });
    const outcome = await this.props.onConfirm(this.state.text);
    if (outcome?.ok) {
      this.input?.blur?.();
      mode.value = "NORMAL";
      this.props.onCancel?.();
      return;
    }
    this.setState({ busy: false, error: outcome?.error?.message || outcome?.message || "Remove failed." });
  };
  render({ row, placeholder }, { text, busy, error }) {
    const label = `REMOVE ${row?.name || row?.id || "ITEM"}`;
    return html`<form class="ops-form set-reason-bar" onSubmit=${this.submit} aria-label=${label}>
      <span class="set-lbl set-bar-lbl">${label}</span>
      <input ref=${(el) => { this.input = el; }} type="text" class="set-inp set-bar-inp set-bar-grow"
        value=${text} autocomplete="off" spellcheck=${false} disabled=${busy}
        placeholder=${placeholder || "Reason (optional)"} aria-label="Reason for removal"
        onInput=${(event) => this.setState({ text: event.currentTarget.value, error: "" })} />
      <button type="submit" class="set-btn set-btn-primary" disabled=${busy}>
        <span>${busy ? "Removing…" : "Remove"}</span><kbd class="cap">⌘↵</kbd>
      </button>
      <button type="button" class="set-btn set-btn-ghost" disabled=${busy} onClick=${this.cancel}>
        <span>Cancel</span><kbd class="cap">Esc</kbd>
      </button>
      <span class="mono muted set-bar-hint">⌘↵ confirms, Esc cancels</span>
      ${error ? html`<span class="set-status-err set-bar-err" role="alert">${error}</span>` : null}
    </form>`;
  }
}

export class SettingsTableFrame extends Component {
  constructor(props) {
    super(props);
    this.controller = props.controller ?? createSettingsTable({
      rows: props.rows ?? [],
      columns: props.columns ?? [],
      facets: props.facets ?? [],
      sorts: props.sorts ?? [],
      searchText: props.searchText,
      initialSelected: props.initialSelected,
    });
    this.state = { revision: 0, createOpen: false, activeFormKind: null, removeTarget: null };
  }
  listId() {
    return `settings-${this.props.id}`;
  }
  resolvedForm(kind = null) {
    const lookup = kind ?? this.state.activeFormKind ?? this.props.formKind;
    if (lookup && getSettingsForm(lookup)) return getSettingsForm(lookup);
    return this.props.form ?? null;
  }
  componentDidMount() {
    this.unsubscribe = this.controller.subscribe(() => {
      this.setState({ revision: this.state.revision + 1 });
    });
    const selected = this.controller.selectedRow();
    if (selected && this.props.autoFocus !== false) {
      selectItem(this.listId(), selected.id, "focus");
    }
    this.commitDom();
  }
  componentDidUpdate(prevProps) {
    if (!this.props.controller && (prevProps.rows !== this.props.rows || prevProps.facets !== this.props.facets
      || prevProps.sorts !== this.props.sorts || prevProps.initialSelected !== this.props.initialSelected)) {
      this.controller.setOptions({
        rows: this.props.rows ?? [],
        columns: this.props.columns ?? [],
        facets: this.props.facets ?? [],
        sorts: this.props.sorts ?? [],
        searchText: this.props.searchText,
        initialSelected: this.props.initialSelected,
      });
    }
    if (prevProps.id !== this.props.id) {
      this.setState({ createOpen: false, activeFormKind: null, removeTarget: null });
    }
    this.commitDom();
  }
  componentWillUnmount() {
    this.unsubscribe?.();
  }
  handleSelect = (id) => {
    this.controller.select(id);
    const row = this.controller.rows().find((item) => item.id === id) ?? null;
    this.props.onSelect?.(row);
  };
  openLocalForm = (kind = null) => {
    const form = this.resolvedForm(kind);
    if (!form) {
      this.props.onCreate?.();
      return;
    }
    this.setState({ createOpen: true, activeFormKind: kind ?? this.props.formKind ?? null, removeTarget: null });
  };
  triggerCreate = (fromKey = false) => {
    openSettingsCreate({
      kind: this.props.formKind ?? this.props.form?.kind ?? null,
      section: this.props.id,
      fromKey,
      openLocal: this.openLocalForm,
    });
  };
  dismissActive = () => {
    if (this.state.createOpen || this.state.removeTarget) {
      this.setState({ createOpen: false, activeFormKind: null, removeTarget: null });
      return true;
    }
    return false;
  };
  requestRemove = (row) => {
    if (!row || !this.props.remove) return;
    if (row.removable === false) {
      notice.value = row.removeTitle || "This item cannot be removed.";
      return;
    }
    this.handleSelect(row.id);
    if (this.props.remove.requireReason) {
      this.setState({ removeTarget: row, createOpen: false });
      return;
    }
    this.executeRemove(row, "");
  };
  executeRemove = async (row, reason) => {
    const result = await runTableRemove(this.controller, row, reason, {
      noun: this.props.noun,
      ...this.props.remove,
    });
    if (result.ok) this.props.onRemoved?.(row, result.outcome);
    return result;
  };
  commitDom() {
    if (!this.base) return;
    const hasCreate = Boolean(this.resolvedForm() || this.props.onCreate);
    this.base.uiCreate = hasCreate ? () => this.triggerCreate(true) : null;
    this.base.uiDismiss = () => this.dismissActive();
    this.base.uiSelect = (id) => this.handleSelect(id);
    this.base.uiContext = (id) => {
      const rows = this.controller.rows();
      const row = rows.find((item) => item.id === id) ?? this.controller.selectedRow();
      return {
        item: row,
        listId: this.listId(),
        remove: this.props.remove && row ? () => this.requestRemove(row) : null,
        onPrimary: this.props.onPrimary ? () => this.props.onPrimary(row) : null,
      };
    };
    const selected = this.controller.selectedRow();
    if (selected) {
      const currentRows = [...this.base.querySelectorAll("[data-row]")];
      if (currentRows.some((el) => el.dataset.item === selected.id) && !this.base.querySelector("[data-cursor]")) {
        selectItem(this.listId(), selected.id, this.props.autoFocus === false ? "queue" : "focus");
      }
    }
    syncList(this.base);
    normalizePrimary();
  }
  render(props, { createOpen, removeTarget }) {
    const { columns = [], remove, empty = "No items.", below, tableClass = "", rowActions } = props;
    const rows = this.controller.rows();
    const selected = this.controller.selectedRow();
    const form = this.resolvedForm();
    const hasCreate = Boolean(form || props.onCreate);
    const hasActions = Boolean(remove || rowActions);
    const colCount = columns.length + (hasActions ? 1 : 0);

    return html`<div class="set-frame" data-list=${this.listId()}>
      <div class=${`set-panel set-tbl-panel${tableClass ? ` ${tableClass}` : ""}`}>
        <${SettingsTableHeader} controller=${this.controller} hasCreate=${hasCreate}
          onCreate=${() => this.triggerCreate(false)} createLabel=${props.createLabel || "New"} />
        <div class="set-table-wrap set-frame-wrap">
          <table class=${`set-tbl set-frame-tbl${tableClass ? ` ${tableClass}-tbl` : ""}`}>
            <thead>
              <tr>
                ${columns.map((col) => html`<th key=${col.id} class=${`${col.numeric ? "set-num " : ""}${col.headerClass || ""}`.trim()}>${col.label}</th>`)}
                ${hasActions ? html`<th class="set-ra"></th>` : null}
              </tr>
            </thead>
            <tbody>
              ${rows.length ? rows.flatMap((row) => {
                const isSelected = selected?.id === row.id;
                const staleInfo = this.controller.state.stale[row.id];
                const trClass = `set-frame-row${isSelected ? " set-row-sel" : ""}${row.rowClass ? ` ${row.rowClass}` : ""}`;
                const mainRow = html`<tr key=${row.id} class=${trClass} data-row="" data-item=${row.id}
                  aria-selected=${isSelected ? "true" : "false"}
                  onClick=${() => this.handleSelect(row.id)}>
                  ${columns.map((col) => html`<td key=${col.id} class=${`${col.numeric ? "set-num " : ""}${col.cellClass || ""}`.trim()}
                    title=${typeof col.title === "function" ? col.title(row) : undefined}>
                    ${col.render ? col.render(row) : row[col.key ?? col.id]}
                  </td>`)}
                  ${hasActions ? html`<td class="set-ra">
                    <span class="set-row-acts">
                      ${rowActions ? rowActions(row) : null}
                      ${remove ? html`<button type="button" class="set-btn set-btn-ghost set-rm-btn"
                        disabled=${row.removable === false}
                        title=${row.removeTitle || `Remove ${row.name || row.id}`}
                        onClick=${(event) => { event.stopPropagation(); this.requestRemove(row); }}>
                        <span>Remove</span><kbd class="cap">dd</kbd>
                      </button>` : null}
                    </span>
                  </td>` : null}
                </tr>`;
                if (!staleInfo) return [mainRow];
                return [mainRow, html`<tr key=${`${row.id}:stale`} class="set-stale-row">
                  <td colSpan=${colCount}>
                    <div class="ui-stale" role="alert">
                      ${`Stale write refused: ${staleInfo.message}`}
                      ${staleInfo.current ? ` · current: ${JSON.stringify(staleInfo.current)}` : ""}
                    </div>
                  </td>
                </tr>`];
              }) : html`<tr><td colSpan=${colCount} class="set-empty-cell">
                <${Empty} what=${this.controller.total() ? "No matching items. Clear search or remove a filter." : empty} />
              </td></tr>`}
            </tbody>
          </table>
        </div>
      </div>
      ${typeof below === "function" ? below(selected, {
        controller: this.controller,
        openCreate: () => this.triggerCreate(false),
        requestRemove: this.requestRemove,
      }) : null}
      ${createOpen && form ? html`<${SettingsInsertForm} form=${form}
        onCancel=${() => this.setState({ createOpen: false, activeFormKind: null })}
        onSaved=${(result) => {
          this.setState({ createOpen: false, activeFormKind: null });
          props.onCreated?.(result);
        }} />` : null}
      ${removeTarget ? html`<${SettingsReasonBar} row=${removeTarget}
        placeholder=${remove?.reasonPlaceholder}
        onCancel=${() => this.setState({ removeTarget: null })}
        onConfirm=${(reason) => this.executeRemove(removeTarget, reason)} />` : null}
    </div>`;
  }
}

export function registerSettingsTableSection(id, config) {
  if (config?.form) registerSettingsForm(config.form.kind ?? id, { section: id, ...config.form });
  registerSettingsSection(id, (context) => {
    const props = typeof config === "function" ? config(context) : config;
    return html`<${SettingsTableFrame} id=${id} ...${props} />`;
  });
}
