import { askHash, runHash } from "./ui-data.js";
import { AskForm } from "./ui-asks.js";
import { notice } from "./ui-notice.js";
import { Decisions, DetailLink, Operation, Rows, Text, html } from "./ui-operations.js";
import { webFetch } from "./ui-request.js";
import { Empty, Fault, Loading } from "./ui-states.js";

const outboxKinds = new Set(["approval", "draft"]);
const option = (label, path, confirm, say, body) => ({ label, path, confirm, say, body });
const stated = (message) => () => message;

function uploadKey() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
}

class EvidenceForm extends Operation {
  upload = async (event) => {
    event.preventDefault();
    const { item, disabled, retry } = this.props;
    if (this.state.busy || disabled) return;
    const input = event.currentTarget.querySelector('input[type="file"]');
    const file = input?.files?.[0];
    if (!file) {
      this.setState({ error: "Choose a file to upload first." });
      input?.focus();
      return;
    }
    this.setState({ busy: true, error: "" });
    try {
      const response = await webFetch(`/api/v1/inbox/${encodeURIComponent(item.inbox_id)}/evidence`, {
        method: "POST",
        headers: {
          "X-Arete": "1",
          "Idempotency-Key": uploadKey(),
          "Content-Type": "application/octet-stream",
          "X-Evidence-Filename": file.name || item.name || "evidence",
        },
        body: file,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        this.setState({ error: payload.detail || payload.error || `Upload failed (${response.status})` });
        return;
      }
      notice.value = payload.result?.run
        ? `Private copy saved · resumed ${payload.result.run}`
        : "Private copy saved inside Arete";
    } catch (fault) {
      this.setState({ error: fault?.message || "Upload failed" });
    } finally {
      try { await retry(); }
      finally { this.setState({ busy: false }); }
    }
  };

  render({ item, disabled, retry }, { busy, error }) {
    const resolvePath = `/api/v1/inbox/${encodeURIComponent(item.inbox_id)}/resolve`;
    const why = [
      `Reference: ${item.name || item.ref_key} (${item.ref_kind})`,
      item.failure_detail ? `Reason: ${item.failure_detail}` : "",
      item.last_error ? `Last check: ${item.last_error}` : "",
    ].filter(Boolean).join("\n");
    const options = [
      option("Access restored", resolvePath, `Re-verify access to ${item.name || item.ref_key} and resume?`,
        (r) => r.result?.resumed?.run ? `Access verified · resumed ${r.result.resumed.run}` : "Access verified",
        { base_version: item.version, verb: "retry" }),
      option("Dismiss", resolvePath, "Stop waiting for this missing evidence?", stated("Dismissed evidence request"),
        { base_version: item.version, verb: "dismiss" }),
    ];
    return html`<section class="ops-detail">
      <header>
        <p class="ops-kicker">Private Evidence · ${item.repo || "No repo"}</p>
        <h3>${item.ticket_ref || item.ticket}: ${item.name || item.ref_key}</h3>
      </header>
      <${Text} text=${why} />
      <p class="muted">Private to Arete — uploaded copies and access details stay inside Arete and are never posted to the team ticket.</p>
      <div class="ops-links">
        ${item.run_id ? html`<a href=${runHash(item.run_id)}>Open run</a>` : null}
        <${DetailLink} href=${item.original_url}>Original link<//>
      </div>
      <form class="ops-form" onSubmit=${this.upload} aria-label="Upload private evidence copy">
        <fieldset disabled=${busy || disabled}>
          <label class="ops-field">Upload private copy
            <input name="file" type="file" />
          </label>
        </fieldset>
        <div class="ops-actions">
          <button type="submit" disabled=${busy || disabled}>Upload private copy & resume</button>
        </div>
        ${error ? html`<p class="ops-error" role="alert">${error}</p>` : null}
      </form>
      <${Decisions} options=${options} retry=${retry} disabled=${busy || disabled} />
    </section>`;
  }
}

function inbox(snapshot, screen) {
  const { asks, questions } = snapshot;
  const rows = [];
  const add = (kind, id, title, state, item) => {
    if (screen === "outbox" && !outboxKinds.has(kind)) return;
    rows.push({ id: `${kind}/${id}`, href: `#${screen}/${kind}/${encodeURIComponent(id)}`, title, state, item, kind, context: item.repo });
  };
  for (const e of asks?.evidence?.rows ?? []) add("evidence", e.id, `${e.ticket_ref || e.ticket}: ${e.name}`, "Evidence", e);
  for (const a of questions?.data?.asks ?? []) add("ask", a.id, a.questions[0]?.header || a.questions[0]?.question || a.ticket || a.run, "Question", a);
  for (const a of asks?.approvals?.rows ?? []) add("approval", a.id, a.plain_summary || a.what, a.failed ? "Failed" : "Approval", a);
  for (const s of asks?.skipped?.rows ?? []) add("skipped", s.ticket, s.ticket, "Skipped", s);
  for (const r of asks?.ready?.rows ?? []) add("ready", r.ref, r.title || r.ref, "Ready", r);
  for (const [kind, label] of [["approve", "Rule"], ["hand", "By hand"], ["memory", "Memory"]]) {
    for (const p of asks?.proposals?.rows?.[kind] ?? []) add(kind, p.id, p.file || p.change, label, p);
  }
  if (asks?.draft) add("draft", asks.draft.chat, asks.draft.title, "Draft", asks.draft);
  return rows;
}

function failures(snapshot, outbox) {
  const unavailable = [];
  if (!outbox && snapshot.questions?.error) unavailable.push(["Questions", snapshot.questions.error]);
  if (snapshot.errors.live) unavailable.push([outbox ? "Outbox" : "Inbox", snapshot.errors.live]);
  const parts = [["approvals", "Approvals"]];
  if (!outbox) parts.push(["skipped", "Skipped work"], ["ready", "Ready tickets"], ["proposals", "Proposals"]);
  for (const [name, label] of parts) {
    const error = snapshot.asks?.[name]?.error;
    if (error) unavailable.push([label, { message: error }]);
  }
  return unavailable;
}

export function selectedInbox(route) { return route.path.slice(0, 2).join("/"); }

export function InboxQueue({ snapshot, route, retry }) {
  const outbox = route.screen === "outbox";
  const rows = inbox(snapshot, route.screen);
  const unavailable = failures(snapshot, outbox);
  const loading = (!snapshot.asks && !snapshot.errors.live) || (!outbox && !snapshot.questions);
  return html`<div class="ops-inbox">
    ${unavailable.map(([what, fault]) => html`<${Fault} key=${what} what=${what} fault=${fault} retry=${retry} />`)}
    ${loading ? html`<${Loading} what=${outbox ? "outbox" : "inbox"} />` : null}
    ${rows.length || (!loading && !unavailable.length) ? html`<${Rows} id=${route.screen} rows=${rows} open=${selectedInbox(route)}
      empty=${outbox ? "No deliveries waiting for review" : "Nothing needs you"} />` : null}
    ${outbox ? html`<div class="ops-section ops-links"><a href="/#chat">Open the console in classic</a></div>`
      : html`<div class="ops-section ops-links"><a href="/#triage">Full ticket triage in classic</a><a href="/#incidents">Incidents in classic</a></div>`}
  </div>`;
}

function detailFor({ kind, item: x }) {
  switch (kind) {
    case "approval": {
      const path = `/api/approvals/${x.id}`;
      return { body: x.detail, why: x.failed || x.held, note: x.review?.by ? `Review by ${x.review.by}: ${x.review.said}` : "", run: x.run_id,
        options: [option(x.failed ? "Try again" : "Approve", path, x.approving || "Approve this delivery?", (r) => `Approved #${x.id}${r.result ? ` · ${r.result}` : ""}`, { approve: true }),
          option("Reject", path, "Reject without sending? Any work held for this delivery may be discarded.", stated(`Rejected #${x.id}`), { approve: false })] };
    }
    case "ready":
      return { why: x.verdict, url: x.url, options: [option("Send", `/api/triage/${encodeURIComponent(x.ref)}/send`,
        `Run screening and write to ${x.ref}?`, (r) => r.said || `Sent ${x.ref}`, { questions: x.questions, answers: x.questions.map(() => ""), repo: x.repo })] };
    case "skipped":
      return { why: x.why, url: x.url, options: x.can_run ? [option("Run anyway", `/api/skipped/${encodeURIComponent(x.ticket)}/run`,
        `Start ${x.ticket} despite the blocker above?`, (r) => r.why || `Started ${x.ticket}`)] : [] };
    case "draft":
      return { body: x.body, why: `This issue is a draft for ${x.repo}. It has not reached GitHub.`, options: [
        option("File issue", `/api/chats/${x.chat}/file`, "File this draft as a GitHub issue?",
          (r) => r.messages?.at(-1)?.text || "Filed the issue", { draft: x.fingerprint }),
        option("Drop draft", `/api/chats/${x.chat}/drop`, "Discard this draft without sending it?", stated("Dropped the draft"))] };
    case "approve":
      return { body: x.change, options: [
        option(x.status === "filing" ? "Try again" : "Approve rule", `/api/proposals/${x.id}/approve`,
          `Write this rule to ${x.repo}/${x.file} and push it?`, stated("Rule approved for writing")),
        ...(x.status === "filing" ? [] : [option("Reject rule", `/api/proposals/${x.id}/reject`, "Reject this proposed rule?", stated("Rejected the proposed rule"))])] };
    default:
      return { body: x.change, why: kind === "hand" ? "Review the proposal and make any approved change yourself." : "File this fact by hand before marking it done.",
        options: [option("Mark done", `/api/proposals/${x.id}/done`, "Mark this proposal done without making any changes?", stated("Marked done"))] };
  }
}

export function InboxFocus({ snapshot, route, retry }) {
  const selected = selectedInbox(route);
  if (!selected) return html`<${Empty} what="Choose an item to review" />`;
  const outbox = route.screen === "outbox";
  const row = inbox(snapshot, route.screen).find((x) => x.id === selected);
  if (!row) {
    const fault = failures(snapshot, outbox)[0];
    if (fault) return html`<${Fault} what=${fault[0]} fault=${fault[1]} retry=${retry} />`;
    if (!snapshot.asks || (!outbox && !snapshot.questions)) return html`<${Loading} what="item" />`;
    return html`<div class="ops-detail"><p>This item is no longer in the ${outbox ? "outbox" : "inbox"}.</p></div>`;
  }
  if (row.kind === "ask") return html`<${AskForm} key=${row.id} ask=${row.item} retry=${retry} disabled=${Boolean(snapshot.questions.error)} />`;
  if (row.kind === "evidence") return html`<${EvidenceForm} key=${row.id} item=${row.item} retry=${retry} disabled=${Boolean(snapshot.errors.live)} />`;
  const detail = detailFor(row);
  const section = ["approve", "hand", "memory"].includes(row.kind) ? "proposals" : row.kind === "approval" ? "approvals" : row.kind;
  return html`<section class="ops-detail" key=${row.id}>
    <header><p class="ops-kicker">${row.state} · ${row.context || "No repo"}</p><h3>${row.title}</h3></header>
    <${Text} text=${detail.why} /><${Text} text=${detail.note} /><${Text} text=${detail.body} />
    <div class="ops-links">
      ${detail.run ? html`<a href=${runHash(detail.run)}>Open run</a>` : null}
      <${DetailLink} href=${detail.url}>Open source ticket<//>
    </div>
    <${Decisions} options=${detail.options} retry=${retry} disabled=${Boolean(snapshot.errors.live || snapshot.asks?.[section]?.error)} />
  </section>`;
}

export function RunQuestions({ resource, run, retry }) {
  if (resource?.error) return html`<${Fault} what="Questions" fault=${resource.error} retry=${retry} />`;
  const waiting = resource?.data?.asks.filter((a) => a.run === run);
  return waiting?.length ? html`<section class="ops-section ops-links" aria-label="Open questions">
    ${waiting.map((ask, i) => html`<a key=${ask.id} href=${askHash(ask.id)}>${waiting.length > 1 ? `Answer question ${i + 1}` : "Answer question"}</a>`)}
  </section>` : null;
}
