"use strict";

const FIRST_MESSAGE_CHARS = 160;
let launchKey = "";
let remoteDevices = [];

function launchValues() {
  return {
    device: document.getElementById("launch-device").value,
    runner: document.getElementById("launch-runner").value,
    repo: document.getElementById("launch-repo").value,
    tools: [...document.querySelectorAll("#launch-tools input:checked")].map((i) => i.value),
  };
}

function options(id, first, names, keep) {
  fill(id, first ? el("option", { value: "", text: first }) : null, names.map((n) => el("option", { value: n, text: n })));
  const box = document.getElementById(id);
  if (keep !== undefined && [...box.options].some((o) => o.value === keep)) box.value = keep;
}

function fillChoices(c, reset = false) {
  const key = JSON.stringify(c);
  if (key === launchKey && !reset) return;
  const keep = launchKey && !reset ? launchValues() : { device: "local", runner: "", repo: "", tools: c.default_tools };
  launchKey = key;
  remoteDevices = c.remote_devices;
  options("launch-device", "", c.devices, keep.device);
  options("launch-runner", "By the model", c.runners, keep.runner);
  options("launch-repo", "None", c.repos, keep.repo);
  fill("launch-models", c.models.map((m) => el("option", { value: m })));
  document.getElementById("launch-model").placeholder = `default: ${c.default_model}`;
  fill("launch-tools", el("legend", { text: "Tools" }), c.tools.map((t) => el("label", { class: "check" },
    el("input", { type: "checkbox", value: t, checked: keep.tools.includes(t) }), t)));
  document.getElementById("launch-about").textContent = "A session is an agent you talk to. It runs on the device and model you pick, " +
    `takes each message you send, and closes after ${c.idle_hours} hours with no message. A session on a repo works in a worktree of its own, which stays when it ends.`;
  repoFollowsDevice();
}

function repoFollowsDevice() {
  const repo = document.getElementById("launch-repo");
  const remote = remoteDevices.includes(document.getElementById("launch-device").value);
  if (remote) repo.value = "";
  repo.disabled = remote;
}

function firstMessage(text, chars = FIRST_MESSAGE_CHARS) {
  const flat = (text || "").replace(/\s+/g, " ").trim();
  return flat.length > chars ? `${flat.slice(0, chars - 1).trimEnd()}…` : flat || "no message";
}

function drawSessions(data) {
  fillChoices(data.choices);
  const paid = paidColumn(data.sessions);
  fill("sessions-list",
    data.sessions.length
      ? table("Newest first", [["Message"], ["Status", "c-status"], ["Started", "c-time"], ["Model", "c-model wide-only"], ...paid.head],
        data.sessions.map((s) => linkedRow(detailHash("session", s.id),
          cell([rowLink("session", s.id, firstMessage(s.prompt)), s.repo ? el("div", { class: "muted", text: `${s.repo}${s.branch ? ` · ${s.branch}` : ""}` }) : null], "wrap-cell"),
          cell(runStatus(s.status), "nb"), cell(clock(s.started), "nb"),
          cell([s.model || "", el("div", { class: "muted", text: [s.runner, s.device].filter(Boolean).join(" on ") })]),
          paid.cell(s))), { stack: true })
      : quiet("No session yet. New session starts one."));
  return data.sessions.some((s) => s.status === "running") ? LIVE_MS : SLOW_MS;
}

function openLaunch() {
  toggleForm("launch-toggle", "launch", "launch-prompt", true);
}

async function launch(event) {
  event.preventDefault();
  const prompt = document.getElementById("launch-prompt");
  const budget = document.getElementById("launch-budget").value.trim();
  if (!prompt.value.trim()) return say("launch-result", "caution", "Write the first message first.");
  if (budget && !(Number(budget) > 0)) return say("launch-result", "caution", "The budget is a number of dollars above zero, such as 0.50, or empty for no limit.");
  trackAction("launch");
  const values = launchValues();
  const btn = document.getElementById("launch-go");
  btn.disabled = true;
  try {
    const s = await call("/api/sessions", { prompt: prompt.value.trim(), device: values.device, model: document.getElementById("launch-model").value.trim(),
      runner: values.runner, repo: values.repo, tools: values.tools, budget_usd: budget ? Number(budget) : null });
    lastSaid.set(`detail:session:${s.id}`, ["ok", `Launched: ${s.runner} ${s.model} on ${s.device}. Its feed shows below as it works.`]);
    prompt.value = "";
    document.getElementById("launch-result").hidden = true;
    toggleForm("launch-toggle", "launch", "launch-prompt", false);
    go(detailHash("session", s.id), "link");
  } catch (e) {
    say("launch-result", "fault", e.message);
    document.getElementById("launch-result").focus();
  } finally {
    btn.disabled = false;
  }
}
