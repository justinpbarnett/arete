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

function itemTag(item) {
  return String(item.id || "").toLowerCase();
}

function askIdOf(item) {
  return typeof item.subject === "string" && item.subject.startsWith("ask:") ? item.subject.slice(4) : "";
}

function showItem(item) {
  const tag = itemTag(item);
  const ask = askIdOf(item);
  const title = item.line && item.task_id ? `${item.line} ${item.task_id}` : item.title || item.id;
  const body = item.title || "";
  return self.registration.showNotification(title, {
    body,
    tag,
    icon: ICON,
    data: { item: item.id, subject: item.subject || "", ask, reminded: item.reminded || "" },
  });
}

async function setBadge(count) {
  try {
    await (count ? self.navigator.setAppBadge?.(count) : self.navigator.clearAppBadge?.());
  } catch {
    // a badge is a courtesy, so a refusal changes nothing
  }
}

async function showItems(items) {
  await setBadge(items.length);
  const open = new Set(items.map(itemTag));
  const shown = await self.registration.getNotifications();
  for (const n of shown) if (n.tag !== WAITING_TAG && !open.has(n.tag)) n.close();
  if (!items.length) {
    return self.registration.showNotification("Arete", { body: "Nothing is waiting on you.", tag: WAITING_TAG, icon: ICON });
  }
  const seen = new Map(shown.map((n) => [n.tag, n.data?.reminded || ""]));
  const fresh = items.filter((item) => {
    const tag = itemTag(item);
    return !seen.has(tag) || seen.get(tag) !== (item.reminded || "");
  });
  await Promise.all((fresh.length ? fresh : [items[items.length - 1]]).map(showItem));
}

async function onPush() {
  let items;
  try {
    const reply = await fetch("/api/v1/inbox?priority=1", { cache: "no-store" });
    if (!reply.ok) throw new Error("not ok");
    items = (await reply.json()).items;
    if (!Array.isArray(items)) throw new Error("no items");
  } catch {
    return showWaiting();
  }
  try {
    return await showItems(items);
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
