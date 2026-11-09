"use strict";

let loopNewKey = "";

function serveState(serve) {
  if (!serve.at) return el("p", {}, stateChip("fault"), " Serve has never run on this machine, so no loop fires on its own. Installing it starts it and keeps it running.");
  if (!serve.alive) return el("p", {}, stateChip("fault"), " Serve last beat ", liveAgo(serve.at), ` (${clock(serve.at)}), so no loop fires on its own until it runs again.`);
  const broken = Object.keys(serve.broken || {}).length;
  const errors = Object.keys(serve.poll_errors || {}).length;
  return el("p", {}, stateChip(broken || errors ? "caution" : "ok"),
    ` Serve is up since ${clock(serve.started)}: ${serve.loops} loops on, ${serve.off} off, ${serve.running} running, ${serve.pending} waiting.` +
    (broken ? ` ${count(broken, "loop does", "loops do")} not load.` : "") + (errors ? ` ${count(errors, "source fails", "sources fail")} to poll.` : ""));
}

function serveLine(serve) {
  return [serveState(serve), !serve.at || !serve.alive
    ? el("div", { class: "actions" }, button(serve.at ? "Restart serve" : "Install serve", "serve-install", installServe)) : null,
  saidLine("serve")];
}

function installServe(btn) {
  sayAt("serve", null, "Installing serve. It loads the background job and readies each repo's labels, so it can take a minute.");
  return act(btn, "serve", "/api/serve/install", {}, (r) => (r.restarting
    ? "Serve is restarting. This page stops answering for a few seconds and comes back once serve is up."
    : `Installed serve${r.said ? `: ${r.said}` : ""}.`));
}

function loopLastRun(l) {
  if (l.running) return ["running", el("br"), itemLink("run", l.running, "Open the run", `running:${l.name}`)];
  if (!l.last) return "never";
  return [...runStatus(l.last.status), el("br"), el("a", { href: detailHash("run", l.last.id), "data-focus": `last:${l.name}`, class: "muted-link" }, liveAgo(l.last.started))];
}

function loopButtons(l, place) {
  return [
    button("Start", "start", (b) => loopAction(l.name, "start", b, place), { of: l.name, disabled: Boolean(l.error || l.running) }),
    button("Try", "try", (b) => loopAction(l.name, "try", b, place), { of: l.name, disabled: Boolean(l.error || l.running) }),
    button(l.enabled ? "Turn off" : "Turn on", "switch", (b) => loopAction(l.name, l.enabled ? "disable" : "enable", b, place), { of: l.name, disabled: Boolean(l.error && !l.enabled) }),
  ];
}

function loopRow(l) {
  return linkedRow(detailHash("loop", l.name),
    cell([rowLink("loop", l.name, l.name), el("div", { class: "muted", text: l.error || l.description })]),
    cell(l.error ? stateChip("fault") : l.enabled ? "on" : "off", "nb"),
    cell(l.kind),
    cell(l.trigger, "nb"),
    cell(loopLastRun(l), "nb"),
    cell(l.next_due ? clock(l.next_due) : "", "nb"),
    cell(paidMoney(l.spent_24h), "num"),
    cell(el("div", { class: "loop-buttons" }, loopButtons(l, "loops"))));
}

function drawLoops(data) {
  fill("loops-list",
    serveLine(data.serve),
    saidLine("loops"),
    data.loops.length
      ? table("", [["Loop", "c-loop"], ["On", "c-on"], ["Kind", "wide-only"], ["Trigger", "wide-only"], ["Last run", "c-status"], ["Next", "c-time wide-only"], ["Paid 24 h", "num nb wide-only"], ["", "c-loop-buttons beside"]],
        data.loops.map(loopRow), { stack: true })
      : quiet("No loop is installed. New loop drafts one from a sentence."));
  fillLoopNew(data.draft);
  return data.loops.some((l) => l.running) ? LIVE_MS : SLOW_MS;
}

function fillLoopNew(d) {
  const key = JSON.stringify(d);
  if (key === loopNewKey) return;
  loopNewKey = key;
  options("loop-new-repo", "None", d.repos, document.getElementById("loop-new-repo").value);
  fill("loop-new-models", d.models.map((m) => el("option", { value: m })));
  document.getElementById("loop-new-model").placeholder = `default: ${d.default_model}`;
}

async function draftLoop(event) {
  event.preventDefault();
  const value = (id) => document.getElementById(`loop-new-${id}`).value.trim();
  if (!value("sentence")) {
    say("loop-new-result", "caution", "Say in a sentence what the loop should do first.");
    return document.getElementById("loop-new-sentence").focus();
  }
  trackAction("draft-loop");
  const btn = document.getElementById("loop-new-go");
  btn.disabled = true;
  try {
    const r = await call("/api/loops/new", { sentence: value("sentence"), name: value("name"), repo: value("repo"), model: value("model") });
    lastSaid.set(`detail:run:${r.id}`, ["ok", `Started a draft at ${clock(r.started)}. Its feed shows below. The loop lands turned off, so try it before you turn it on.`]);
    for (const id of ["sentence", "name"]) document.getElementById(`loop-new-${id}`).value = "";
    document.getElementById("loop-new-result").hidden = true;
    toggleForm("loop-new-toggle", "loop-new", "loop-new-sentence", false);
    go(detailHash("run", r.id), "link");
  } catch (e) {
    say("loop-new-result", "fault", e.message);
    document.getElementById("loop-new-result").focus();
  } finally {
    btn.disabled = false;
  }
}

async function loopAction(name, action, btn, place) {
  btn.disabled = true;
  try {
    const r = await call(`/api/loops/${encodeURIComponent(name)}/${action}`, {});
    if (action === "start" || action === "try") {
      lastSaid.set(`detail:run:${r.id}`, ["ok", action === "start" ? `Started ${name} at ${clock(r.started)}.`
        : `Started a try of ${name} at ${clock(r.started)}. The feed shows what each emit would do, and none is sent.`]);
      go(detailHash("run", r.id), "link");
      return;
    }
    if (r.enabled && !r.serve_up) sayAt(place, "caution", `${name} is on, but serve is down, so it fires once serve runs again.`);
    else sayAt(place, "ok", `${name} is ${r.enabled ? "on. Serve fires it from its next tick" : "off. A run already going finishes"}.`);
  } catch (e) {
    sayAt(place, "fault", e.message);
  }
  refresh();
}

function budgetWords(d) {
  if (d.metered === false) return "none, claude runs on the subscription";
  return [d.budget_usd ? `${money(d.budget_usd)} a run` : "", d.daily_budget_usd ? `${money(d.daily_budget_usd)} a day` : ""].filter(Boolean).join(", ") || "none";
}

function loopFacts(d) {
  return el("dl", { class: "w" },
    fact("On", d.enabled ? "on" : "off"), fact("Kind", d.kind), fact("Folder", el("code", { text: d.dir })), fact("Trigger", d.trigger),
    fact("Webhook", d.webhook_path ? el("code", { text: `POST ${d.webhook_path}` }) : ""),
    fact("Repo", d.repo), fact("Model", d.model ? `${d.model}${d.runner ? ` on ${d.runner}` : ""}` : ""),
    fact("Device", d.device), fact("Queue", d.queue === d.name ? "its own" : d.queue), fact("Budget", budgetWords(d)),
    fact("Paid 24 h", paidMoney(d.spent_24h)),
    fact("Emits", d.emits.join(", ") || "nothing"),
    fact("Next", d.next_due ? `${clock(d.next_due)}` : d.enabled ? "" : "off, so never on its own"),
    fact("Waiting", d.waiting.length ? `${d.waiting.length} in line` : ""), fact("Held", d.held));
}

function loopRuns(d) {
  if (!d.runs.length) return quiet("It has not run yet.");
  const paid = paidColumn(d.runs, { onPhone: true });
  return table("Newest first", [["Started", "c-time"], ["Trigger", "c-kind wide-only"], ["Status", "c-status"], ["Time", "num c-duration wide-only"], ...paid.head, ["Result", "c-result wide-only"]],
    d.runs.map((r) => linkedRow(detailHash("run", r.id),
      cell(itemLink("run", r.id, clock(r.started), `loop-run:${r.id}`), "nb"), cell(r.trigger || ""), cell(runStatus(r.status), "nb"),
      cell(duration(secondsBetween(r.started, r.finished)), "num"), paid.cell(r),
      cell(r.pr_url ? pullLinks(r.pr_url) : clampedSummary(r.summary), "muted-cell"))));
}

function loopSources(d) {
  return d.cursors.length
    ? table("", [["Source"], ["Polled", "c-time"], ["State", "c-status"], ["Detail"]],
      d.cursors.map((c) => el("tr", {}, cell(c.source, "muted-cell"), cell(liveAgo(c.polled), "nb"),
        cell(stateChip(c.ok ? "ok" : "fault")), cell(c.detail || (c.ok ? "reading" : "")))))
    : quiet(d.sources.length ? "Serve has not polled its sources yet." : "It has no source.");
}

function loopDigests(d) {
  return d.digests.length
    ? d.digests.map((g) => foldText(`digest:${d.name}:${g.day}`, g.day, g.text))
    : quiet("No digest yet.");
}

function subheading(title) {
  return el("h3", { "data-jump": title, tabindex: "-1", text: title });
}

function drawLoopDetail(result) {
  const name = route.detail.id;
  const place = `detail:loop:${name}`;
  if (result.error) {
    fill("loops-detail-head", detailHead("loops-detail", "loop", name, name));
    fill("loops-detail-body", fault(`The loop could not be read: ${result.error}`));
    return SLOW_MS;
  }
  const d = result.value;
  const memory = Object.keys(d.state || {}).length ? JSON.stringify(d.state, null, 2) : "";
  const sections = [
    ["Runs", loopRuns(d)],
    ["Findings", d.findings.length
      ? table("Each finding is reported once, by its fingerprint", [["Finding"], ["Seen", "num c-n"], ["First", "c-time wide-only"], ["Last", "c-time"]],
        d.findings.map((f) => el("tr", {}, cell(f.url ? link(f.url, f.title || f.fingerprint) : f.title || f.fingerprint), cell(f.seen, "num"),
          cell(clock(f.first_seen), "nb"), cell(clock(f.last_seen), "nb"))))
      : quiet("No finding yet.")],
    ["Sources", loopSources(d)],
    d.metrics.length ? ["Metrics", table("The last 30 days", [["Metric"], ["Latest", "num c-cost"], ["When", "c-time"], ["Points", "num c-n wide-only"]],
      d.metrics.map((m) => el("tr", {}, cell(m.name), cell(Number(m.value).toLocaleString(undefined, { maximumFractionDigits: 20 }), "num"), cell(clock(m.ts), "nb"), cell(m.points, "num"))))] : null,
    d.emits.some((e) => e.startsWith("digest")) ? ["Digests", loopDigests(d)] : null,
    ["Memory", memory ? foldText(`memory:${d.name}`, "Show what it keeps between runs", memory) : quiet("It keeps nothing between runs yet.")],
    d.prompt ? ["Prompt", foldText(`prompt:${d.name}`, "Show the prompt", d.prompt)] : null,
  ].filter(Boolean);
  fill("loops-detail-head", detailHead("loops-detail", "loop", name, d.name, loopButtons(d, place)));
  fill("loops-detail-body",
    saidLine(place),
    d.error ? fault(`It does not load: ${d.error}`) : el("p", { text: d.description }),
    loopFacts(d),
    jumps(sections.map(([title]) => title)),
    sections.map(([title, body]) => [subheading(title), body]));
  return d.running ? LIVE_MS : SLOW_MS;
}

function openLoopNew() {
  toggleForm("loop-new-toggle", "loop-new", "loop-new-sentence", true);
}
