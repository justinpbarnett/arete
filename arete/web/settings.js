"use strict";

const RUN_LIMITS = [["max-parallel", "max_parallel"], ["max-parallel-per-repo", "max_parallel_per_repo"]];
let settingsData = null;

function defaultModel(data, role) {
  return data.defaults[role] || "claude CLI";
}

function drawSettings(data) {
  settingsData = data;
  for (const [id, key] of RUN_LIMITS) {
    const input = document.getElementById(id);
    if (input.value !== String(data[key])) input.value = data[key];
  }
  fill("model-hint", `Each role takes ${data.aliases.join(", ")}, or a full claude model id. An empty role uses its default.`);
  fill("model-names", data.aliases.map((alias) => el("option", { value: alias })));
  fill("model-fields", Object.entries(data.models).map(([role, model]) => el("div", { class: "field" },
    el("label", { for: `model-${role}`, text: role.replace("_", " ") }),
    el("input", { id: `model-${role}`, "data-role": role, "data-focus": `model-${role}`, type: "text", value: model,
      placeholder: `default: ${defaultModel(data, role)}`, list: "model-names", autocomplete: "off", spellcheck: "false" }))));
}

let pushData = null;
let thisDeviceId = "";

function pushReady() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window && window.isSecureContext;
}

function bytesOfBase64url(text) {
  const padded = (text + "=".repeat((4 - (text.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

async function deviceIdOf(endpoint) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint));
  return [...new Uint8Array(digest)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function thisSubscription() {
  const registration = await navigator.serviceWorker.getRegistration();
  return registration ? registration.pushManager.getSubscription() : null;
}

function deviceLabel() {
  const ua = navigator.userAgent;
  const found = [["iPhone", /iPhone/], ["iPad", /iPad/], ["Android", /Android/], ["Mac", /Macintosh/], ["Windows", /Windows/], ["Linux", /Linux/]]
    .find(([, pattern]) => pattern.test(ua));
  return found ? found[0] : "Device";
}

function paintPush() {
  const ready = pushReady();
  document.getElementById("push-allow").disabled = !ready;
  const devices = pushData.devices;
  fill("push-devices",
    ready ? null : quiet("This page cannot receive pushes. On an iPhone, open the dashboard over HTTPS in Safari, add it to the Home Screen and open it from there."),
    devices.length ? table("Subscribed devices", [["Device"], ["Added", "c-time"], ["Last push", "c-time"], [""]],
      devices.map((d) => el("tr", {}, cell(d.id === thisDeviceId ? `${d.label} (this device)` : d.label), cell(liveAgo(d.created), "c-time"),
        cell(liveAgo(d.last_success), "c-time"), cell(button("Remove", "push-remove", () => removePush(d.id), { of: d.id })))))
      : quiet("No device is subscribed."));
}

function drawPush(data) {
  pushData = data;
  paintPush();
  if (!pushReady()) return;
  thisSubscription().then((sub) => (sub ? deviceIdOf(sub.endpoint) : "")).then((id) => {
    if (id === thisDeviceId) return;
    thisDeviceId = id;
    paintPush();
  }).catch(() => {});
}

async function allowPush() {
  trackAction("push-allow");
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return say("push-result", "fault", "Notifications are not allowed for this page, so nothing was subscribed.");
  try {
    const registration = await navigator.serviceWorker.ready;
    const key = bytesOfBase64url(pushData.key);
    let sub = await registration.pushManager.getSubscription();
    const held = sub?.options?.applicationServerKey;
    if (sub && (!held || held.byteLength !== key.length || new Uint8Array(held).some((b, i) => b !== key[i]))) {
      await sub.unsubscribe();
      sub = null;
    }
    sub ||= await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    const json = sub.toJSON();
    const saved = await call("/api/push/subscribe", { endpoint: json.endpoint, keys: json.keys, label: deviceLabel() });
    thisDeviceId = await deviceIdOf(json.endpoint);
    drawPush(saved);
    say("push-result", "ok", "This device will get a push when an ask needs you.");
  } catch (e) {
    say("push-result", "fault", e.message);
  }
}

async function removePush(id) {
  try {
    const saved = await call("/api/push/remove", { id });
    if (id === thisDeviceId) {
      (await thisSubscription())?.unsubscribe();
      thisDeviceId = "";
    }
    drawPush(saved);
    say("push-result", "ok", "Removed the device.");
  } catch (e) {
    say("push-result", "fault", e.message);
  }
}

async function testPush() {
  trackAction("push-test");
  try {
    const got = await call("/api/push/test", {});
    drawPush(got);
    const extra = [got.removed ? `${got.removed} removed because its push service no longer knows it` : "", got.failed ? `${got.failed} failed` : ""].filter(Boolean);
    say("push-result", got.failed ? "fault" : "ok", `Sent a test to ${got.sent} ${got.sent === 1 ? "device" : "devices"}${extra.length ? `, ${extra.join(", ")}` : ""}.`);
  } catch (e) {
    say("push-result", "fault", e.message);
  }
}

function settingsChanges() {
  const changes = {};
  for (const [id, key] of RUN_LIMITS) {
    const value = document.getElementById(id).value.trim();
    if (value !== String(settingsData[key])) changes[key] = value;
  }
  const models = {};
  for (const input of document.querySelectorAll("#model-fields input")) {
    if (input.value.trim() !== settingsData.models[input.dataset.role]) models[input.dataset.role] = input.value.trim();
  }
  if (Object.keys(models).length) changes.models = models;
  return changes;
}

async function saveSettings(event) {
  event.preventDefault();
  const changes = settingsChanges();
  if (!Object.keys(changes).length) return say("settings-result", "ok", "Nothing changed, so nothing was saved.");
  trackAction("settings-save");
  const saveButton = document.getElementById("settings-save");
  saveButton.disabled = true;
  try {
    const saved = await call("/api/settings", changes);
    drawSettings(saved);
    const words = [changes.max_parallel ? `runs at once ${saved.max_parallel}` : "",
      changes.max_parallel_per_repo ? `runs per repo ${saved.max_parallel_per_repo}` : "",
      ...Object.keys(changes.models || {}).map((role) => (saved.models[role] ? `${role} ${saved.models[role]}`
        : `${role} back to its default (${defaultModel(saved, role)})`))].filter(Boolean);
    say("settings-result", "ok", `Saved ${words.join(", ")}. The pipeline takes it at its next poll.`);
  } catch (e) {
    say("settings-result", "fault", e.message);
  } finally {
    saveButton.disabled = false;
  }
}
