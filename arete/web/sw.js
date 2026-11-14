const ICON = "/icon-192.png";
const WAITING_TAG = "arete-waiting";

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

function showWaiting() {
  return self.registration.showNotification("Arete is waiting on you", { tag: WAITING_TAG, icon: ICON });
}

function showAsk(a) {
  return self.registration.showNotification(`${a.repo} ${a.ticket}`, {
    body: a.questions?.[0]?.header || "", tag: a.id, icon: ICON, data: { ask: a.id, reminded: a.reminded || "" } });
}

async function setBadge(count) {
  try {
    await (count ? self.navigator.setAppBadge?.(count) : self.navigator.clearAppBadge?.());
  } catch {
    // a badge is a courtesy, so a refusal changes nothing
  }
}

async function showAsks(asks) {
  await setBadge(asks.length);
  const open = new Set(asks.map((a) => a.id));
  const shown = await self.registration.getNotifications();
  for (const n of shown) if (n.tag !== WAITING_TAG && !open.has(n.tag)) n.close();
  if (!asks.length) {
    return self.registration.showNotification("Arete", { body: "Nothing is waiting on you.", tag: WAITING_TAG, icon: ICON });
  }
  const seen = new Map(shown.map((n) => [n.tag, n.data?.reminded || ""]));
  const fresh = asks.filter((a) => !seen.has(a.id) || seen.get(a.id) !== (a.reminded || ""));
  await Promise.all((fresh.length ? fresh : [asks[asks.length - 1]]).map(showAsk));
}

async function onPush() {
  let asks;
  try {
    const reply = await fetch("/api/asks", { cache: "no-store" });
    if (!reply.ok) throw new Error("not ok");
    asks = (await reply.json()).asks;
    if (!Array.isArray(asks)) throw new Error("no asks");
  } catch {
    return showWaiting();
  }
  try {
    return await showAsks(asks);
  } catch {
    return showWaiting();
  }
}

self.addEventListener("push", (event) => {
  event.waitUntil(onPush());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const ask = event.notification.data?.ask;
  const target = ask ? `/#ask/${encodeURIComponent(ask)}` : "/";
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    if (!windows.length) return self.clients.openWindow(target);
    if (ask) windows[0].postMessage({ ask });
    return windows[0].focus();
  })());
});
