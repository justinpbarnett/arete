import { h } from "./vendor-preact.js";

const glyphs = { loading: "…", empty: "○", error: "×", offline: "⊘", stale: "◔" };

function line(kind, children, extra = {}) {
  return h("p", { class: `ui-state ${kind}`, "data-state": kind, ...extra },
    h("span", { class: "ui-state-glyph", "aria-hidden": "true" }, glyphs[kind]), h("span", { class: "ui-state-text" }, children));
}

export const Loading = ({ what }) => line("loading", `Loading ${what}`, { role: "status" });

export const Empty = ({ what }) => line("empty", what);

export function faultText(fault) {
  const status = fault?.status ? `HTTP ${fault.status}` : "";
  return [status, fault?.message && fault.message !== status ? fault.message : ""].filter(Boolean).join(" · ") || "Unavailable";
}

export function Fault({ what, fault, retry }) {
  return line("error", [`${what} unavailable · ${faultText(fault)} `,
    retry ? h("button", { type: "button", class: "ui-state-retry", "data-act": "retry-read", onClick: retry }, h("kbd", { class: "cap" }, "⇧r"), " Retry") : null],
  { role: "alert" });
}

export const Offline = () => line("offline", "OFFLINE · showing the last data");

export const Stale = () => line("stale", "STALE · serve heartbeat is old");

export function Connectivity({ word }) {
  return word ? h("span", { class: `ui-conn ${word.toLowerCase()}`, "data-conn": word, role: "status" },
    h("span", { "aria-hidden": "true" }, glyphs[word.toLowerCase()]), ` ${word}`) : null;
}

export function Gate({ pending, fault, empty, what, missing, retry, children }) {
  if (fault) return h(Fault, { what, fault, retry });
  if (pending) return h(Loading, { what });
  if (empty) return h(Empty, { what: missing });
  return children;
}
