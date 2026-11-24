import { Component } from "./vendor-preact.js";
import { runHash, taskHash } from "./ui-data.js";
import { Decisions, DetailLink, Panel, Read, Rows, Text, html } from "./ui-operations.js";
import { Empty, Fault, Gate } from "./ui-states.js";

const states = { "needs-info": "Needs you", recovering: "Recovering", failed: "Failed", running: "Running", pr: "In review", todo: "To do", done: "Done", closed: "Closed" };
const stateWord = (state) => states[state] ?? state;
const displayState = (task, recovering) => task.state === "needs-info" && recovering?.includes(task.id) ? "recovering" : task.state;
const listRow = (task) => ({ id: task.id, href: taskHash(task.id), title: task.title || task.id, context: [task.repo, task.id].filter(Boolean).join(" · ") });

export class TaskQueue extends Component {
  state = { query: "" };
  render({ resource, route, retry, recovering }, { query }) {
    const open = route.active === "my-work";
    return html`<${Read} what="Tasks" resource=${resource} retry=${retry}>${({ tasks }) => {
      const search = query.toLowerCase().trim();
      const enough = tasks.filter((task) => !(open && ["done", "closed"].includes(task.state))
        && `${task.title} ${task.repo} ${task.id}`.toLowerCase().includes(search));
      const statesInUse = [...new Set([...Object.keys(states), ...enough.map((task) => displayState(task, recovering))])];
      return html`<div class="ops-detail task-list" data-list="local-tasks">
        <label class="ops-field">Filter tasks<input type="search" data-search="" value=${query} placeholder="Title, repo or task ID" onInput=${(e) => this.setState({ query: e.target.value })} /></label>
        ${enough.length ? statesInUse.map((state) => {
        const rows = enough.filter((task) => displayState(task, recovering) === state).map(listRow);
        return rows.length ? html`<section key=${state} class="task-group"><h3>${stateWord(state)} <span>${rows.length}</span></h3>
          <${Rows} rows=${rows} open=${route.screen === "task" ? route.path[0] : ""} />
        </section>` : null;
      }) : html`<${Empty} what=${search ? "No matching tasks" : open ? "No open local tasks" : "No local tasks"} />`}
      </div>`;
    }}<//>`;
  }
}

export function TaskFocus({ route, resource, retry, recovering }) {
  if (route.screen !== "task" || !route.path[0]) return html`<div class="ops-detail"><h3>Local tasks</h3>
    <p>Choose a task to see its activity and runs. GitHub and Jira tickets are linked from their runs, not listed here.</p></div>`;
  return html`<${Read} what="Task" resource=${resource} retry=${retry}>${(task) => html`<article class="ops-detail">
    <header><p class="ops-kicker">${[task.ref || task.id, task.repo, stateWord(displayState(task, recovering))].filter(Boolean).join(" · ")}</p>
      <h3>${task.title || task.id}</h3><${DetailLink} href=${task.url}>Open source ticket<//></header>
    <${Text} text=${task.body} />
    ${task.children?.length ? html`<section class="ops-section"><h3>Child tasks</h3><${Rows} id="child-tasks" rows=${task.children.map(listRow)} /></section>` : null}
    <section class="ops-section"><h3>Runs</h3>
      <${Rows} id="task-runs" empty="No runs recorded" rows=${(task.runs ?? []).map((run) => ({ id: run.id, href: runHash(run.id),
        title: run.id, context: stamp(run.started), state: stateWord(run.status) }))} />
    </section>
    ${task.comments ? html`<section class="ops-section"><h3>Activity</h3>${task.comments.length
    ? task.comments.map((comment, i) => html`<article key=${i} class="ops-comment"><small>${comment.author} · ${stamp(comment.ts)}</small>
      <${Text} text=${comment.body} /></article>`)
    : html`<${Empty} what="No comments" />`}</section>` : null}
  </article>`}<//>`;
}

function stamp(value) {
  const time = Date.parse(value);
  return Number.isFinite(time) ? new Date(time).toLocaleString() : "";
}

export function FloorQueue({ snapshot, retry }) {
  const rows = snapshot.runs?.map((run) => ({ id: run.id, href: runHash(run.id), title: run.ticket || run.id,
    context: run.slot || run.step || run.repo || run.kind, state: run.stopping ? "Stopping" : "Running" }));
  const fault = snapshot.live?.live?.error ? { message: snapshot.live.live.error } : snapshot.errors.live;
  return html`${fault && rows ? html`<${Fault} what="Running work" fault=${fault} retry=${retry} />` : null}
    <${Gate} pending=${!rows && !fault} fault=${rows ? null : fault} what="running work" retry=${retry}>
      <${Rows} id="floor-runs" rows=${rows ?? []} empty="No running work" /><//>`;
}

export function TicketProgress({ progress }) {
  if (!progress) return null;
  const { activity, blocker, next_event, follow_up, prerequisites, prerequisite_issues, updates, timing } = progress;
  const unread = Object.entries(prerequisite_issues ?? {}).filter(([, read]) => !read).map(([ref]) => ref);
  return html`<div data-ticket-progress="">
    <p>${activity}${progress.since ? html` <small>· since ${stamp(progress.since)}</small>` : null}</p>
    ${blocker ? html`<p><strong>Blocker:</strong> ${blocker}</p>` : null}
    ${next_event ? html`<p><strong>Next:</strong> ${next_event}</p>` : null}
    ${timing ? html`<section aria-label="Run lineage timing">${timing.lines.map((line) => html`<p key=${line}>${line}</p>`)}</section>` : null}
    ${prerequisites?.length ? html`<ul>${prerequisites.map((pr) => html`<li key=${pr.key}>
      ${pr.deployed_release ? "Deployed prerequisite" : pr.state === "merged"
    ? "Merged prerequisite (deployment not confirmed)" : `Prerequisite (${pr.state})`}:
      <${DetailLink} href=${pr.url}>${pr.key}<//></li>`)}</ul>` : null}
    ${unread.length ? html`<p>Prerequisite lookup not confirmed: ${unread.join(", ")}</p>` : null}
    ${follow_up?.prs?.length ? html`<p><strong>${follow_up.draft ? "Draft follow-up" : "Follow-up PR"}:</strong>
      ${follow_up.prs.map((url) => html` <${DetailLink} key=${url} href=${url}>${url}<//>`)}</p>` : null}
    ${updates?.length ? html`<ul aria-label="Recent ticket updates">${updates.map((update, index) => html`
      <li key=${`${update.seq}:${index}`}><small>${stamp(update.at)}</small> ${update.message}</li>`)}</ul>` : null}
  </div>`;
}

export function FloorFocus({ snapshot, retry }) {
  const feedback = snapshot.live?.waiting;
  const progress = snapshot.live?.ticket_progress;
  return html`<div class="ops-detail">
    <section class="ops-section"><h3>Needs you</h3>
      <p>${snapshot.needsYou == null || !snapshot.questions?.data || snapshot.questions.error ? "See the inbox for current work." :
    `${snapshot.needsYou + snapshot.questions.data.asks.length} items in the inbox`}</p>
      <a href="#triage">Open Triage</a></section>
    ${progress && (progress.error || progress.rows?.length) ? html`<${Panel} panel=${progress} title="Ticket progress" retry=${retry}>
      ${(rows) => rows.map((row) => html`<article key=${`${row.repo}:${row.issue}`}>
        <h4><a href=${row.run ? runHash(row.run) : taskHash(row.issue, row.repo)}>${row.ticket}</a></h4>
        <${TicketProgress} progress=${row} />
      </article>`)}<//>` : null}
    ${snapshot.live?.recovery?.rows?.length ? html`<section class="ops-section"><h3>Automatic recovery</h3>
      <${Rows} id="recovery-queue" rows=${snapshot.live.recovery.rows.map((item) => ({
        id: `${item.repo}/${item.ticket}`, href: item.source ? runHash(item.source) : taskHash(item.ticket, item.repo),
        title: `${item.repo} ${item.ticket}`, context: item.detail || item.cause, state: item.phase.replaceAll("_", " ")
      }))} /></section>` : null}
    <section class="ops-section"><h3>Health</h3><p>${snapshot.health ?? "Not yet read"}</p><a href="#settings/health">Open Health</a></section>
    ${feedback ? html`<${Panel} panel=${feedback} title="Waiting for review feedback" retry=${retry}>${({ tickets, quiet_minutes }) => tickets.length
    ? html`<div><p>Runs start ${quiet_minutes} minutes after the last comment, or when a review is submitted.</p>
      <ul>${tickets.map((ticket) => html`<li key=${ticket.ticket}>${ticket.ticket} · ${ticket.comments} comments</li>`)}</ul></div>`
    : html`<${Empty} what="No review feedback waiting" />`}<//>` : null}
  </div>`;
}

function HealthRows({ rows, empty }) {
  return rows.length ? html`<ul class="ops-health-rows">${rows.map((row) => html`<li key=${row.name}>
    <div><strong>${row.name}</strong><${Text} text=${row.detail} />${row.href ? html`<a href=${row.href}>Open details</a>` : null}</div>
    <small class=${`ops-row-state ${row.state === "fault" ? "ops-error" : ""}`}>${row.state}</small>
  </li>`)}</ul>` : html`<${Empty} what=${empty} />`;
}

export function HealthFocus({ resource, retry }) {
  return html`<${Read} what="Health" resource=${resource} retry=${retry}>${(health) => {
    const { serve } = health;
    const issues = [serve.config_problem, serve.code_problem,
      ...Object.entries(serve.failing ?? {}).map(([name, problem]) => `${name}: ${problem.error}`),
      ...Object.entries(serve.poll_errors ?? {}).map(([name, problem]) => `${name}: ${problem}`),
      ...Object.entries(serve.broken ?? {}).map(([name, problem]) => `${name}: ${problem}`)].filter(Boolean);
    const checks = { label: "Run checks", confirm: "Start a heartbeat check now?", path: "/api/checks/run",
      disabled: health.checks.rows?.running, say: () => "Checks started" };
    return html`<div class="ops-detail">
      <section class="ops-section"><h3>Serve · ${health.state}</h3>
        <p>${serve.alive ? "Running" : "Not reporting as running"}${serve.at ? ` · Last beat ${stamp(serve.at)}` : " · No beat recorded"}</p>
        ${issues.map((issue, i) => html`<p key=${i} class="ops-error">${issue}</p>`)}</section>
      <${Panel} panel=${health.checks} title="Checks" retry=${retry}>${(panel) => html`
        <p>${panel.heartbeat ? `Last heartbeat ${stamp(panel.heartbeat)}` : "No heartbeat results yet"}${panel.running ? " · Checks running" : ""}</p>
        <${Decisions} options=${[checks]} disabled=${Boolean(resource.error)} retry=${retry} />
        <${HealthRows} rows=${panel.checks.map((c) => ({ name: c.key, state: c.state, detail: c.detail }))} empty="No checks recorded" />
      `}<//>
      <${Panel} panel=${health.alerts} title="Alerts" retry=${retry}>${({ alerts }) => html`<${HealthRows}
        rows=${alerts.map((a) => ({ name: a.key, state: "fault", detail: a.detail }))} empty="No open alerts" />`}<//>
      <${Panel} panel=${health.incidents} title="Blocked incidents" retry=${retry}>${(rows) => html`<${HealthRows}
        rows=${rows.map((i) => ({ name: `Incident #${i.id}`, detail: [i.waiting, i.signature].filter(Boolean).join(" · "),
          state: "blocked", href: `/#incident/${encodeURIComponent(i.id)}` }))} empty="No blocked incidents" />`}<//>
      <${Panel} panel=${health.devices} title="Devices" retry=${retry}>${(devices) => html`<${HealthRows}
        rows=${devices.map((d) => ({ name: d.name, state: d.probe ? (d.probe.ok ? "ok" : "fault") : "not probed", detail: d.probe?.detail || d.note || d.host }))}
        empty="No devices" />`}<//>
      <${Panel} panel=${health.workspaces} title="Held workspaces" retry=${retry}>${(w) => html`<${HealthRows}
        rows=${w.rows.map((entry) => ({ name: entry.name, state: entry.blocked ? "blocked" : "held", detail: entry.why,
          href: entry.run ? runHash(entry.run) : null }))} empty="No held workspaces" />`}<//>
    </div>`;
  }}<//>`;
}
