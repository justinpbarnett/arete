import { webFetch } from "./ui-request.js";
import { cursor, newer } from "./ui-snapshot.js";

const path = "/api/v1/events";
const name = "arete-events-v1";
const retryDelay = 2000;
const recoveryDelay = 60000;
const kinds = new Set(["run.started", "run.changed", "run.ended", "step.started", "step.ended",
  "turn.appended", "principal.changed"]);

export function openEvents(update) {
  const locks = globalThis.navigator?.locks && navigator.locks;
  const channel = locks && globalThis.BroadcastChannel ? new BroadcastChannel(name) : null;
  const lifetime = new AbortController();
  const subscriptions = new Map();
  const tab = Math.random().toString(36).slice(2);
  let position = null;
  let ready = false;
  let leader = false;
  let source = null;
  let pending = null;
  let timer = null;
  let failures = 0;
  let recoverAt = 0;
  let lastPulse = 0;
  let connected = false;
  let run = "";
  let release;

  function send(message) { channel?.postMessage(message); }

  function deliver(message) {
    if (message.type === "reset") {
      ready = false;
      position = null;
      update({ kind: "reset" });
      return;
    }
    const advanced = newer(message.id, position);
    if (advanced) position = message.id;
    if (message.type === "pulse") update(message.event ?? { kind: "serve.beat" });
    else if (message.type === "status") update(message.event);
    else if (advanced && kinds.has(message.event?.kind)) update(message.event);
  }

  function share(message) {
    send(message);
    deliver(message);
  }

  function status(up) {
    if (connected === up) return;
    connected = up;
    share({ type: "status", event: { kind: "transport", connected: up } });
  }

  function listen() {
    if (!leader || !ready || lifetime.signal.aborted || source || pending || timer !== null) return;
    if (!globalThis.EventSource || Date.now() < recoverAt) poll();
    else stream();
  }

  function later(delay) {
    clearTimeout(timer);
    timer = setTimeout(() => { timer = null; listen(); }, delay);
  }

  function stopSource() {
    source?.close();
    source = null;
  }

  function reset() {
    stopSource();
    share({ type: "reset" });
  }

  function turns() {
    return [...new Set(subscriptions.values())].sort().join(",");
  }

  function url(timeout) {
    const query = new URLSearchParams({ since: position });
    const selected = turns();
    if (selected) query.set("turns", selected);
    if (timeout != null) query.set("timeout", timeout);
    return `${path}?${query}`;
  }

  function stream() {
    const connection = new EventSource(url());
    source = connection;
    const receive = (type, message) => {
      if (source !== connection) return;
      let event;
      try { event = JSON.parse(message.data); } catch { return; }
      failures = 0;
      share({ type, event, id: message.lastEventId });
    };
    connection.onopen = () => { if (source === connection) status(true); };
    connection.onmessage = (message) => receive("event", message);
    connection.addEventListener("serve.beat", (message) => receive("pulse", message));
    connection.addEventListener("reset", () => {
      if (source === connection) reset();
    });
    connection.onerror = () => {
      if (source !== connection) return;
      status(false);
      failures += 1;
      if (failures >= 3) {
        stopSource();
        recoverAt = Date.now() + recoveryDelay;
        listen();
      } else if (connection.readyState === EventSource.CLOSED) {
        stopSource();
        later(retryDelay);
      }
    };
  }

  async function poll() {
    const controller = new AbortController();
    const expiry = setTimeout(() => controller.abort(), 30000);
    pending = controller;
    try {
      const response = await webFetch(url(25), { cache: "no-store", signal: controller.signal,
        headers: { Accept: "application/json", "Last-Event-ID": position } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      if (lifetime.signal.aborted) return;
      if (!cursor(result?.next) || !Array.isArray(result.events)) throw new Error("Malformed event response");
      status(true);
      if (result.reset) reset();
      else {
        for (const event of result.events) if (event) share({ type: "event", event, id: event.id });
        const pulse = Date.now() - lastPulse >= 15000;
        if (pulse) lastPulse = Date.now();
        share({ type: pulse ? "pulse" : "cursor", id: result.next });
      }
    } catch {
      if (!lifetime.signal.aborted) {
        status(false);
        later(retryDelay);
      }
    } finally {
      clearTimeout(expiry);
      pending = null;
      listen();
    }
  }

  function subscribe(id, selection) {
    const before = source && turns();
    if (selection) subscriptions.set(id, selection);
    else subscriptions.delete(id);
    if (source && before !== turns()) {
      stopSource();
      later(0);
    }
  }

  if (channel) {
    channel.onmessage = ({ data }) => {
      if (data.type === "hello") {
        send({ type: "turns", tab, run });
        if (leader) send({ type: "status", event: { kind: "transport", connected } });
      }
      else if (data.type === "turns") subscribe(data.tab, data.run);
      else if (!leader) deliver(data);
    };
    send({ type: "hello" });
    locks.request(name, { signal: lifetime.signal }, async () => {
      if (lifetime.signal.aborted) return;
      leader = true;
      failures = 0;
      await new Promise((resolve) => { release = resolve; listen(); });
    }).catch(() => {});
  } else leader = true;

  return {
    baseline(seq) {
      if (ready || !cursor(seq)) return;
      if (newer(seq, position)) position = seq;
      ready = true;
      listen();
    },
    select(id) {
      run = /^[\w][\w.-]*$/.test(id) ? id : "";
      subscribe(tab, run);
      send({ type: "turns", tab, run });
    },
    close() {
      lifetime.abort();
      stopSource();
      pending?.abort();
      clearTimeout(timer);
      send({ type: "turns", tab, run: "" });
      channel?.close();
      release?.();
    },
  };
}
