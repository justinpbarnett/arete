import { h, Component } from "./vendor-preact.js";
import { signal, effect } from "./vendor-signals.js";
import { prepareWrite, sendWrite } from "./ui-write.js";
import { notice, succeeded, failed } from "./ui-notice.js";
import { run as runRoutes } from "./ui-data.js";
import { pushUndo } from "./ui-undo.js";

export const reasonBox = signal(null);
export const fixture = signal(null);
export const fixtureRevision = signal(0);
let last = null;
let replay = null;
const pending = new Set();

const verb = (name, label, key, reason = false, explanation = null) => {
  const action = { name, label, key, reason, explanation,
    run: (context, text) => dispatch(action, context, text),
    undo: explanation ? null : (context, result) => context.inverse?.(action, result) };
  return Object.freeze(action);
};
export const reject = () => verb("reject", "Reject", "dd", true);
export const discard = () => verb("discard", "Discard", "dd", true);
export const decline = () => verb("decline", "Decline", "dd", true);
export const remove = () => verb("remove", "Remove", "dd");
export const stop = () => verb("stop", "Stop", "dd", false, "Stopping a run cannot be undone");
export const create = () => verb("create", "Create", "c", true);
export const checks = () => verb("checks", "Run checks now", "", false, "Checks cannot be undone");
export const snooze = () => verb("snooze", "Snooze", "s");
export const shelve = () => verb("shelve", "Shelve", "S", true);
export const resume = () => verb("resume", "Resume", "s");
export const repair = () => verb("repair", "Repair", "R");
export const answer = () => verb("answer", "Answer", "Cmd Enter");
export const step = () => verb("steps", "Retry step", "R");
export const actions = Object.fromEntries([reject(), discard(), decline(), remove(), stop(), create(), checks(),
  snooze(), shelve(), resume(), repair(), answer(), step()].map((one) => [one.name, one]));
export const runAction = (name, label, key = "") => verb(name, label, key);

export async function dispatch(action, context = {}, reason = "") {
  const identity = `${action.name}:${context.listId ?? ""}:${context.item?.id ?? ""}`;
  if (pending.has(identity)) return false;
  pending.add(identity);
  let request;
  const hash = globalThis.location?.hash;
  try {
    if (!context.request && action.name !== "checks") throw new Error("No write adapter for this action");
    const spec = context.request ? context.request(action, reason) : { url: "/api/checks/run", data: {} };
    request = prepareWrite(spec.url, spec.data, context.item, spec.options);
    const result = await sendWrite(request);
    const transition = context.commit?.(action, result, reason) ?? {};
    const label = transition.label ?? (action.name === "checks" ? "Checks started" : `${action.label} ${context.item?.id ?? "item"}`);
    let inverseRequest = null;
    pushUndo({ label, explanation: action.explanation ?? (!context.inverse ? "This action cannot be undone" : null),
      inverse: action.explanation ? null : context.inverse ? async () => {
        if (!inverseRequest) {
          const spec = context.inverse(action, result, transition);
          inverseRequest = prepareWrite(spec.url, spec.data, spec.object, spec.options);
        }
        try {
          await sendWrite(inverseRequest);
        } catch (error) { if (error.conflict) context.stale?.(error.current); throw error; }
        transition.undo?.();
      } : null,
      restore: context.restore ?? (() => { if (hash != null) location.hash = hash; }) });
    replay = request;
    last = { action, reason, compatible: context.compatible };
    succeeded(action.explanation && action.name !== "checks" ? `${label} (cannot be undone)` : label);
    return true;
  } catch (error) {
    if (error.conflict) context.stale?.(error.current);
    failed(error);
    return false;
  } finally { pending.delete(identity); }
}

export function runContext(item, verb, data = {}, step = "") {
  const routes = runRoutes(item.id);
  if (!["stop", "answer", "resume", "repair", "steps"].includes(verb) || (verb === "steps" && !step)) throw new Error("Unknown run action");
  return { item, compatible: `run:${verb}`, request: () => ({ url: verb === "steps" ? routes.steps(step) : routes[verb], data }) };
}

export const retryRun = verb("retry", "Retry", "R");
export const repairThisRun = verb("repair-run", "Repair run", "");

const retryRoutes = { stop: "stop", retry: "resume", "repair-run": "repair" };

export function runPageContext(run, { refresh, go, openLink }) {
  const routes = runRoutes(run.id);
  const target = (action) => routes[retryRoutes[action.name]];
  const settled = (action, result) => {
    refresh();
    if (action.name !== "stop" && typeof result.run === "string" && result.run) go(result.run);
    return { label: action.name === "stop" ? `Asked ${run.id} to stop` : `${action.label} ${run.id}` };
  };
  return {
    item: { id: run.id, verb: "stop" }, compatible: `run-page:${run.id}`,
    request: (action) => ({ url: target(action), data: {} }),
    commit: settled,
    run: { retry: ["failed", "interrupted", "stopped"].includes(run.status) ? retryRun : null, open: openLink },
  };
}

export async function replayLast() {
  if (!replay || pending.has("replay")) return false;
  pending.add("replay");
  try { await sendWrite(replay); return true; }
  catch (error) { failed(error); return false; }
  finally { pending.delete("replay"); }
}

export function repeat(context) {
  if (!last || !context || !context.item || last.compatible !== context.compatible) { notice.value = "No compatible action to repeat"; return false; }
  return dispatch(last.action, context, last.reason);
}

export function openReason(action, context) {
  if (reasonBox.peek()) return;
  reasonBox.value = { action, context, text: "", label: `${action.label} ${action.name === "create" ? "item" : context.item.id}` };
}

export function cancelReason() { reasonBox.value = null; }
export async function confirmReason() {
  const box = reasonBox.peek();
  if (!box || box.busy) return;
  reasonBox.value = { ...box, busy: true };
  const ok = await dispatch(box.action, box.context, box.text);
  if (ok) cancelReason();
  else reasonBox.value = { ...box, busy: false };
}

export function perform(action, context) {
  if (!context) return;
  if (action.reason) openReason(action, context);
  else return dispatch(action, context);
}

export class ReasonBox extends Component {
  state = { revision: 0 };
  componentDidMount() {
    this.unsubscribe = effect(() => { reasonBox.value; this.setState({ revision: this.state.revision + 1 }); });
    this.focus();
  }
  componentDidUpdate() { this.focus(); }
  focus() {
    const box = reasonBox.peek();
    if (box && !this.wasOpen) this.base.querySelector("input")?.focus();
    this.wasOpen = Boolean(box);
  }
  componentWillUnmount() { this.unsubscribe(); this.base.querySelector?.("input")?.blur(); }
  render() {
    const box = reasonBox.value;
    if (!box) return null;
    return h("div", { class: "reason-box" }, h("label", { for: "action-reason" }, box.label),
      h("input", { id: "action-reason", value: box.text, readOnly: box.busy, autoComplete: "off",
        placeholder: box.action.name === "create" ? "Name" : "Reason (optional)",
        onInput: (event) => { reasonBox.value = { ...reasonBox.peek(), text: event.currentTarget.value }; } }),
      h("span", null, "Cmd+Enter confirm · Esc cancel"));
  }
}
