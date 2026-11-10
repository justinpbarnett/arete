"use strict";

function countStates(states) {
  return { faults: states.filter((s) => s === "fault").length, cautions: states.filter((s) => s === "caution").length };
}

function showAll(n, one, many) {
  return n === 1 ? `Show the ${one}` : `Show all ${n} ${many}`;
}

function checksSummary(h) {
  if (!h.heartbeat) return el("p", {}, stateChip("caution"), " The heartbeat has not run yet, so no check has a result.");
  const { faults, cautions } = countStates(h.checks.map((c) => c.state));
  const warnings = cautions ? `, ${cautions} with a warning` : "";
  return el("p", {}, stateChip(faults ? "fault" : cautions ? "caution" : "ok"), " ",
    `${h.checks.length} checks, ${faults} failing${warnings}. The last heartbeat came `, liveAgo(h.heartbeat), ` (${clock(h.heartbeat)}).`);
}

function runChecks(btn) {
  return act(btn, "checks", "/api/checks/run", {},
    () => `Started the checks at ${clock(new Date())}. Their results show here when they end.`);
}

function probeWords(p) {
  if (!p) return "";
  return [p.system, p.detail].filter(Boolean).join(" · ");
}

function deviceRow(d) {
  const p = d.probe;
  const host = d.host || "this machine";
  return el("tr", {},
    cell([el("span", { class: "nb", text: d.name }), d.note && d.note !== host ? el("div", { class: "muted", text: d.note }) : null]),
    cell(d.kind), cell(host, "muted-cell"),
    cell(p ? [stateChip(p.ok ? "ok" : "fault"), el("br"), el("span", { class: "muted" }, liveAgo(p.at))] : "not probed", "nb"),
    cell(probeWords(p) || d.error || "", "muted-cell"),
    cell([p ? (p.tools || []).join(", ") || "none found" : "", p?.gpus?.length ? el("div", { text: p.gpus.join(", ") }) : null]),
    cell(button("Probe", "probe", (b) => probe(d.name, b), { of: d.name }), "probe-cell"));
}

async function probe(name, btn) {
  btn.disabled = true;
  btn.textContent = "Probing";
  try {
    const p = await call(`/api/devices/${encodeURIComponent(name)}/probe`, {});
    sayAt("devices", p.ok ? "ok" : "fault", p.ok ? `${name} answered in ${p.seconds} s${p.system ? `: ${p.system}` : ""}.` : `${name} did not answer: ${p.detail || "no detail"}.`, { from: btn });
  } catch (e) {
    sayAt("devices", "fault", e.message, { from: btn });
  }
  refresh();
}

function failures(h) {
  const rows = [];
  const add = (state, what, name, detail) => rows.push({ state, row: el("tr", {}, cell(stateChip(state)), cell([el("span", { class: "muted", text: `${what} ` }), name]), cell(detail || "", "muted-cell")) });
  for (const a of h.alerts.rows?.alerts ?? []) add("fault", "Alert", a.key, [`opened ${clock(a.opened)}, last seen `, liveAgo(a.last_seen), a.url ? [" · ", link(a.url, `#${a.issue}`)] : null]);
  for (const c of h.checks.rows?.checks ?? []) if (c.state !== "ok") add(c.state, "Check", c.key, c.detail);
  for (const p of h.alerts.rows?.probes ?? []) if (!p.ok) add("fault", "Probe", p.name, p.detail);
  for (const l of h.checks.rows?.loops ?? []) if (!l.ok) add("fault", "Loop", l.name, l.detail);
  for (const [name, why] of Object.entries(h.serve.broken || {})) add("fault", "Loop file", name, why);
  for (const [source, error] of Object.entries(h.serve.poll_errors || {})) add("caution", "Source", source, error);
  for (const d of h.devices.rows ?? []) if (d.probe && !d.probe.ok) add("fault", "Device", d.name, probeWords(d.probe));
  return rows.sort((a, b) => (a.state === "fault" ? 0 : 1) - (b.state === "fault" ? 0 : 1));
}

function healthSummary(h, failed) {
  const unread = [h.checks, h.alerts, h.devices].filter((p) => !("rows" in p)).length;
  const serveDown = !h.serve.at ? "serve has never run" : !h.serve.alive ? "serve is down" : "";
  const { faults, cautions } = countStates(failed.map((f) => f.state));
  const parts = [serveDown, faults ? count(faults, "thing fails", "things fail") : "", cautions ? count(cautions, "thing has a warning", "things have a warning") : "",
    unread ? `${count(unread, "part", "parts")} could not be read` : ""].filter(Boolean);
  return el("p", { class: "summary" }, stateChip(h.state), parts.length ? ` ${parts.join(", ")}.`.replace(/^ ./, (s) => s.toUpperCase()) : " Every alert, check, probe, device and loop answers.");
}

function tally(failing, what, after = "") {
  return el("p", {}, stateChip(failing ? "fault" : "ok"), ` ${what}, ${failing} failing.${after}`);
}

function drawHealth(h) {
  const failed = failures(h);
  const s = h.serve;
  const titles = ["Alerts and failures", "Serve", "Devices", "Checks", "Loops", "Probes"];
  fill("health-body",
    healthSummary(h, failed),
    jumps(titles),
    heading(1, titles[0]),
    failed.length ? table("Worst first", [["State", "c-status"], ["What"], ["Detail"]], failed.map((f) => f.row), { stack: true })
      : el("p", {}, stateChip("ok"), " No alert is open and nothing fails."),
    heading(2, titles[1]),
    serveLine(s),
    s.at ? el("dl", { class: "w" }, fact("Process", String(s.pid)), fact("Last beat", [liveAgo(s.at), ` (${clock(s.at)})`]),
      fact("Ticks", `${s.ticks}, one every ${s.tick_seconds} s`), fact("Started", clock(s.started))) : null,
    heading(3, titles[2]),
    el("p", { text: "Loops and sessions run on these. A probe checks the device answers and lists its tools; devices.toml in the config folder adds one." }),
    saidLine("devices"),
    panelParts(h.devices, "The devices", (devices) => table("", [["Device", "c-device"], ["Kind", "c-kind wide-only"], ["Host", "c-ref wide-only"], ["State", "c-status"], ["System", "wide-only"], ["Tools", "wide-only"], ["", "c-probe"]], devices.map(deviceRow), { stack: true })),
    heading(4, titles[3]),
    panelParts(h.checks, "The heartbeat", (c) => [checksSummary(c),
      el("div", { class: "actions" }, button(c.running ? "Checks running" : "Run the checks now", "checks-run", runChecks, { disabled: c.running })),
      saidLine("checks"),
      c.checks.length ? fold("health:checks", showAll(c.checks.length, "check", "checks"), table("", [["Check"], ["State", "c-status"], ["Detail"]],
        c.checks.map((x) => el("tr", {}, cell(x.key), cell(stateChip(x.state)), cell(x.detail || ""))))) : null]),
    heading(5, titles[4]),
    panelParts(h.checks, "The loops", (c) => (c.loops.length
      ? [tally(c.loops.filter((l) => !l.ok).length, count(c.loops.length, "loop", "loops"), " Each one reports on its own runs."),
        fold("health:loops", showAll(c.loops.length, "loop", "loops"), table("", [["Loop"], ["State", "c-status"], ["Last run", "c-time"], ["Detail", "wide-only"]],
          c.loops.map((l) => el("tr", {}, cell(l.name), cell(stateChip(l.ok ? "ok" : "fault")), cell(liveAgo(l.ts)), cell(l.detail || "")))))]
      : quiet("No loop has reported yet."))),
    heading(6, titles[5]),
    panelParts(h.alerts, "The probes", (a) => (a.probes.length
      ? [tally(a.probes.filter((p) => !p.ok).length, count(a.probes.length, "probe", "probes")),
        fold("health:probes", showAll(a.probes.length, "probe", "probes"), table("", [["Probe"], ["State", "c-status"], ["Last run", "c-time"], ["Last good", "c-time wide-only"], ["Detail", "wide-only"]],
          a.probes.map((p) => el("tr", {}, cell(p.name), cell(stateChip(p.ok ? "ok" : "fault")), cell(liveAgo(p.last_run)), cell(liveAgo(p.last_ok)), cell(p.detail || "")))))]
      : quiet("No probe has run yet."))));
  return h.checks.rows?.running ? LIVE_MS : SLOW_MS;
}
