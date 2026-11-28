import { webFetch } from "./ui-request.js";
import { cursor, newer } from "./ui-snapshot.js";

const path = "/api/v1/events";
const name = "arete-events-v1";
const retryDelay = 2000;
const recoveryDelay = 60000;
const leaseDuration = 40000;
const kinds = new Set(["run.started", "run.changed", "run.ended", "step.started", "step.ended",
  "turn.appended", "principal.changed", "task.created", "task.changed",
  "project.changed", "line.changed", "view.changed", "ref.changed",
  "inbox.raised", "inbox.resolved", "inbox.snoozed", "inbox.shelved", "inbox.woken", "inbox.changed"]);

function taskSubjectMatches(subject, id) {
  return subject === `task:${id}` || (/^T-\d+$/i.test(id) && subject === `task:${id.slice(2)}`);
}

export function boardTask(event) {
  const row = event.data;
  if (!row?.id) return null;
  if (event.kind.startsWith("task.")) return taskSubjectMatches(event.subject, String(row.id)) ? row : null;
  const task = row.board_task;
  return event.kind.startsWith("run.") && task?.id && task.id === row.issue && event.subject === `run:${row.id}` ? task : null;
}

function leaseStore() {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open(name, 1);
    open.onupgradeneeded = () => open.result.createObjectStore("leader");
    open.onsuccess = () => resolve(open.result);
    open.onerror = reject;
  });
}

function takeLease(db, tab, { closing, departed } = {}) {
  return new Promise((resolve, reject) => {
    const transaction = db.transaction("leader", "readwrite");
    const store = transaction.objectStore("leader");
    const read = store.get(name);
    let owned = false;
    read.onsuccess = () => {
      const current = read.result;
      if (closing) {
        if (current?.tab === tab) store.delete(name);
      } else if (!current || current.tab === tab || current.tab === departed || current.until <= Date.now()) {
        owned = true;
        store.put({ tab, until: Date.now() + leaseDuration }, name);
      }
    };
    transaction.oncomplete = () => resolve(owned);
    transaction.onabort = reject;
  });
}

export function openEvents(update) {
  const locks = globalThis.navigator?.locks && navigator.locks;
  const channel = globalThis.BroadcastChannel ? new BroadcastChannel(name) : null;
  const lifetime = new AbortController();
  const subscriptions = new Map();
  const tab = Math.random().toString(36).slice(2);
  let position, source, pending, timer, release, checkLease;
  let ready = false;
  let leader = false;
  let transport = null;
  let failures = 0;
  let recoverAt = 0;
  let lastPulse = 0;
  let run = "";

  function send(event) { channel?.postMessage(event); }

  function deliver(event) {
    if (event.kind === "reset") {
      ready = false;
      position = null;
    }
    const advanced = newer(event.id, position);
    if (advanced) position = event.id;
    if (["reset", "serve.beat", "transport"].includes(event.kind) || advanced && kinds.has(event.kind)) update(event);
  }

  function share(event) {
    send(event);
    deliver(event);
  }

  function status(next) {
    if (transport === next) return;
    transport = next;
    share({ kind: "transport", transport });
  }

  function listen() {
    if (!leader || !ready || lifetime.signal.aborted || source || pending || timer != null) return;
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

  function lead(owned) {
    if (owned === leader) return;
    leader = owned;
    if (owned) {
      failures = 0;
      listen();
    } else {
      stopSource();
      pending?.abort();
      clearTimeout(timer);
      timer = null;
      status(null);
    }
  }

  async function elect() {
    const db = await leaseStore();
    if (lifetime.signal.aborted) { db.close(); return; }
    let checking = false;
    let departed;
    checkLease = async (tabLeft) => {
      departed = tabLeft ?? departed;
      if (checking || lifetime.signal.aborted) return;
      checking = true;
      try {
        const last = departed;
        departed = undefined;
        const owned = await takeLease(db, tab, { departed: last });
        if (!lifetime.signal.aborted) lead(owned);
      } catch { lead(false); }
      finally {
        checking = false;
        if (departed) checkLease();
      }
    };
    const renew = setInterval(checkLease, retryDelay);
    release = () => {
      clearInterval(renew);
      takeLease(db, tab, { closing: true }).finally(() => db.close()).catch(() => {});
    };
    await checkLease();
  }

  function turns() { return [...new Set(subscriptions.values())].sort().join(","); }

  function url(timeout) {
    const query = new URLSearchParams({ since: position });
    const selected = turns();
    if (selected) query.set("turns", selected);
    if (timeout != null) query.set("timeout", timeout);
    return `${path}?${query}`;
  }

  function stream() {
    const connection = source = new EventSource(url());
    const receive = (message) => {
      if (source !== connection) return;
      let event;
      try { event = JSON.parse(message.data); } catch { return; }
      if (!event?.kind) return;
      failures = 0;
      if (event.kind === "reset") stopSource();
      if (event.kind === "serve.beat") checkLease?.();
      share({ ...event, id: message.lastEventId });
    };
    connection.onopen = () => { if (source === connection) status("stream"); };
    connection.onmessage = receive;
    connection.addEventListener("serve.beat", receive);
    connection.addEventListener("reset", receive);
    connection.onerror = () => {
      if (source !== connection) return;
      status(null);
      if (++failures >= 3) {
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
    const controller = pending = new AbortController();
    const expiry = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await webFetch(url(25), { cache: "no-store", signal: controller.signal,
        headers: { Accept: "application/json", "Last-Event-ID": position } });
      const result = await response.json();
      if (controller.signal.aborted || !leader) return;
      if (!response.ok || !cursor(result?.next) || !Array.isArray(result.events)) throw new Error("Malformed event response");
      checkLease?.();
      status("poll");
      if (result.reset) share({ kind: "reset" });
      else {
        for (const event of result.events) if (event) share(event);
        const pulse = Date.now() - lastPulse >= 15000;
        if (pulse) lastPulse = Date.now();
        share({ kind: pulse ? "serve.beat" : "cursor", id: result.next });
      }
    } catch {
      if (leader && !controller.signal.aborted) {
        status(null);
        later(retryDelay);
      }
    } finally {
      clearTimeout(expiry);
      pending = null;
      listen();
    }
  }

  function subscribe(id, selection) {
    const before = turns();
    if (selection) subscriptions.set(id, selection);
    else subscriptions.delete(id);
    if ((source || pending) && before !== turns()) {
      stopSource();
      pending?.abort();
      later(0);
    }
  }

  function announce() { send({ kind: "turns", tab, run }); }

  if (channel) {
    channel.onmessage = ({ data }) => {
      if (lifetime.signal.aborted) return;
      if (data.kind === "hello") {
        announce();
        if (leader) send({ kind: "transport", transport });
      } else if (data.kind === "turns") subscribe(data.tab, data.run);
      else if (data.kind === "vacant") checkLease?.(data.tab);
      else if (!leader) deliver(data);
    };
    send({ kind: "hello" });
  }
  const electOnHttp = () => elect().catch(() => {});
  if (locks) locks.request(name, { signal: lifetime.signal }, () => {
    if (lifetime.signal.aborted) return;
    return new Promise((resolve) => {
      release = resolve;
      lead(true);
    });
  }).catch(() => { if (!lifetime.signal.aborted) electOnHttp(); });
  else electOnHttp();

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
      announce();
    },
    close() {
      if (lifetime.signal.aborted) return;
      lifetime.abort();
      lead(false);
      run = "";
      announce();
      send({ kind: "vacant", tab });
      release?.();
      channel?.close();
    },
  };
}
