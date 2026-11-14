"use strict";

const INCIDENT_LIVE_PHASES = ["repairing", "verifying"];

function incidentState(i) {
  const word = i.phase || i.state;
  const chip = i.state === "resolved" ? "ok" : ["blocked", "waiting", "unsure"].includes(word) ? "caution" : null;
  return [chip ? stateChip(chip) : null, chip ? " " : null, el("span", { class: "nb", text: word })];
}

function switchWakes(on, place) {
  return (btn) => act(btn, place, "/api/incidents/switch", { on }, () => `Wakes and rechecks are ${on ? "on" : "off"}.`);
}

function watcherLine(w, place) {
  if (w.state === "disabled") {
    return el("p", {}, stateChip("caution"), " The on-call watcher is off. Set ", el("code", { text: "enabled = true" }), " under ", el("code", { text: "[oncall]" }),
      " in ", pathCode(w.config), " and serve starts scanning at its next tick. Incidents already stored still list here.");
  }
  if (w.state === "switched_off") {
    return [el("p", {}, stateChip("caution"), ` The watcher scans, but wakes, rechecks and restarts are switched off since ${zonedClock(w.switched)}. Turn wakes on here or run `,
      el("code", { text: "arete oncall on" }), "."),
    el("div", { class: "actions" }, button("Turn wakes on", "incident-switch", switchWakes(true, place), { of: "on" }))];
  }
  const scanned = w.last_scan ? [", last scan ", liveAgo(w.last_scan), w.ok ? "" : `, failed: ${w.detail}`] : ", no scan yet";
  return [el("p", {}, stateChip(w.last_scan && !w.ok ? "fault" : "ok"), " The on-call watcher is on", scanned, `, ${w.expected} expected events counted.`),
    el("div", { class: "actions" }, button("Turn wakes off", "incident-switch", switchWakes(false, place), { of: "off" }))];
}

function incidentLive(i) {
  return INCIDENT_LIVE_PHASES.includes(i.phase);
}

function incidentRow(i) {
  const id = String(i.id);
  return linkedRow(detailHash("incident", id),
    cell([rowLink("incident", id, `#${id}`), " ", clampedSummary(i.signature)]),
    cell(incidentState(i), "nb"),
    cell(i.class, "nb wide-only"),
    cell([String(i.count), el("div", { class: "muted" }, liveAgo(i.last_seen))], "nb"),
    cell(i.decision_line, "muted-cell wide-only"),
    cell(i.repair_line, "muted-cell"));
}

function drawIncidents(data) {
  fill("incidents-list",
    watcherLine(data.watcher, "incidents"),
    saidLine("incidents"),
    data.incidents.length
      ? table("Open first", [["Incident"], ["State", "c-status"], ["Class", "wide-only"], ["Seen", "c-time"], ["Decision", "wide-only"], ["Repair"]],
        data.incidents.map(incidentRow), { stack: true })
      : quiet("No incident yet."));
  return data.incidents.some(incidentLive) ? LIVE_MS : SLOW_MS;
}

function incidentAct(id, action, saying, btn, place) {
  return act(btn, place, `/api/incidents/${encodeURIComponent(id)}/${action}`, {}, saying);
}

function cancelWords(id) {
  return (r) => `Cancelled the repair of incident ${id}: ${r.stopped ? `stopped run ${r.stopped}` : "no run was live"}${r.cooldown_until ? `; cooling down until ${clock(r.cooldown_until)}` : ""}.`;
}

function incidentButtons(i, place) {
  const id = String(i.id);
  return [
    button("Dismiss", "incident-dismiss", (b) => incidentAct(id, "dismiss", () => `Dismissed incident ${id}. It is closed.`, b, place), { disabled: !i.can.dismiss }),
    button("Retry", "incident-retry", (b) => incidentAct(id, "retry",
      () => `Re-armed incident ${id}. Attempts, spend and the cooldown count from here; the next scan may start a repair.`, b, place), { disabled: !i.can.retry }),
    button("Cancel", "incident-cancel", (b) => incidentAct(id, "cancel", cancelWords(id), b, place), { disabled: !i.can.cancel }),
  ];
}

function incidentDecision(i) {
  if (!i.decision) return i.decision_line;
  return `${i.decision}, confidence ${Number(i.confidence).toFixed(2)}${i.injection ? " (injection)" : ""}${i.decided ? `, decided ${clock(i.decided)}` : ""}`;
}

function incidentFacts(i) {
  return el("dl", { class: "w" },
    fact("State", [i.state, i.phase ? ` · ${i.phase}` : "", i.waiting ? ` · ${i.waiting}` : ""].join("")),
    fact("Class", i.class), fact("Source", pathCode(i.source)), fact("Repo", i.repo),
    fact("First seen", zonedClock(i.first_seen)), fact("Last seen", zonedClock(i.last_seen)),
    fact("Occurrences", String(i.count)), fact("Decision", incidentDecision(i)), fact("Reason", i.reason),
    fact("Recovery", i.recovery), fact("Closed", zonedClock(i.closed)), fact("Paid", paidMoney(i.paid)));
}

function incidentAnswers(i) {
  const rows = Object.entries(i.answers || {});
  return rows.length
    ? table("What the decision asked", [["Question"], ["Answer", "c-status"], ["Confidence", "num c-n"]],
      rows.map(([name, a]) => el("tr", {}, cell(name), cell(a.value === true ? "yes" : a.value === false ? "no" : String(a.value), "nb"),
        cell(a.confidence === undefined ? "" : Number(a.confidence).toFixed(2), "num"))))
    : quiet("No answers kept.");
}

function incidentEvidence(i) {
  if (!i.evidence) return quiet(i.state === "open" ? "No evidence kept." : "Evidence and notes were pruned after it closed.");
  return el("pre", { class: "fold-text", tabindex: "0", "data-focus": "evidence", text: i.evidence });
}

function incidentRunRows(i) {
  return i.repairs.flatMap((x) => {
    const attempt = x.attempt ? `${x.attempt} of ${i.max_attempts}` : "before a re-arm";
    if (!x.runs.length) return [el("tr", {}, cell(attempt, "nb"), cell(x.task || "no task"), cell(quiet(x.task ? "no run yet" : "no run was started")), cell(""), cell(""), cell(x.outcome || "", "nb"))];
    return x.runs.map((run) => linkedRow(detailHash("run", run.id), cell(attempt, "nb"), cell(x.task),
      cell(itemLink("run", run.id, run.id, `incident-run:${run.id}`), "nb"), cell(runStatus(run.status), "nb"),
      cell(clock(run.started), "nb"), cell([paidMoney(run.paid) || money(0), x.outcome ? ` · ${x.outcome}` : ""], "nb")));
  });
}

function incidentRepairs(i) {
  return i.repairs.length
    ? table("", [["Attempt", "c-status"], ["Task"], ["Run", "c-time"], ["Status", "c-status"], ["Started", "c-time"], ["Paid and outcome", "c-cost"]], incidentRunRows(i))
    : quiet("No repair has started.");
}

function incidentNotes(i) {
  const shown = i.notes.length;
  return i.notes.length
    ? [i.notes_total > shown ? quiet(`The newest ${shown} of ${i.notes_total}.`) : null,
      table("Oldest first", [["When", "c-time"], ["Note"]], i.notes.map((n) => el("tr", {}, cell(zonedClock(n.ts), "nb"), cell(n.note || ""))))]
    : quiet("No notes kept.");
}

function drawIncidentDetail(result) {
  const id = route.detail.id;
  const place = `detail:incident:${id}`;
  if (result.error) {
    fill("incidents-detail-head", detailHead("incidents-detail", "incident", id, `#${id}`));
    fill("incidents-detail-body", unreadFault("The incident", result));
    return SLOW_MS;
  }
  const { incident: i, watcher: w } = result.value;
  const sections = ["Answers", "Evidence", "Repair runs", "Notes"];
  fill("incidents-detail-head", detailHead("incidents-detail", "incident", id, `#${id} ${i.signature}`.slice(0, 120), incidentButtons(i, place)));
  fill("incidents-detail-body",
    saidLine(place),
    watcherLine(w, place),
    incidentFacts(i),
    jumps(sections),
    subheading(sections[0]), incidentAnswers(i),
    subheading(sections[1]), incidentEvidence(i),
    subheading(sections[2]), incidentRepairs(i),
    subheading(sections[3]), incidentNotes(i));
  return incidentLive(i) ? LIVE_MS : SLOW_MS;
}
