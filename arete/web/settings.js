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
