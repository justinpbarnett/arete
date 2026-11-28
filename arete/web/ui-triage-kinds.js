import { Component, h } from "./vendor-preact.js";
import htm from "./vendor-htm.js";
import * as data from "./ui-data.js";
import { webFetch } from "./ui-request.js";
import { prepareWrite, sendWrite } from "./ui-write.js";
import { failed, notice, succeeded } from "./ui-notice.js";
import { registerKind } from "./ui-triage.js";
import { refreshKeybar } from "./ui-keys.js";

const html = htm.bind(h);

const TRACKER_NAMES = {
  jira: "Jira",
  github: "GitHub",
  slack: "Slack",
  asana: "Asana",
  email: "Email",
};

const FAIL_LINE_RE = /fail|error|cannot|refused|exception|traceback|assert|panic/i;

export function hasVerb(item, verb) {
  return Array.isArray(item?.verbs) && item.verbs.includes(verb);
}

export function canApproveRemember(item) {
  const allowed = Boolean(item?.detail?.remember_allowed ?? item?.detail?.rememberAllowed);
  return allowed && hasVerb(item, "approve_remember");
}

export function isFailingLine(line) {
  return FAIL_LINE_RE.test(String(line || ""));
}

export function textareaRows(text, minRows = 2, cols = 48) {
  const lines = String(text || "").split("\n");
  const total = lines.reduce((sum, line) => sum + Math.max(1, Math.ceil((line.length || 1) / cols)), 0);
  return Math.max(minRows, Math.min(18, total));
}

function lcs(a, b) {
  const n = a.length;
  const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { ops.push(["=", a[i]]); i += 1; j += 1; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push(["-", a[i]]); i += 1; }
    else { ops.push(["+", b[j]]); j += 1; }
  }
  while (i < n) { ops.push(["-", a[i]]); i += 1; }
  while (j < m) { ops.push(["+", b[j]]); j += 1; }
  return ops;
}

export function computeWordDiff(aiDraft, edited) {
  const ops = lcs(String(aiDraft || "").split(/(\s+)/), String(edited || "").split(/(\s+)/));
  const collapse = (mark) => {
    const filtered = ops.filter(([op]) => op === "=" || op === mark).map(([op, tok]) => [op, tok]);
    for (let i = 1; i < filtered.length - 1; i += 1) {
      if (filtered[i][0] === "=" && /^\s+$/.test(filtered[i][1])
          && filtered[i - 1][0] === mark && filtered[i + 1][0] === mark) {
        filtered[i][0] = mark;
      }
    }
    const runs = [];
    for (const [op, tok] of filtered) {
      if (!tok) continue;
      const last = runs[runs.length - 1];
      if (last && last[0] === op) last[1] += tok;
      else runs.push([op, tok]);
    }
    return runs;
  };
  return { removed: collapse("-"), added: collapse("+") };
}

export function WordDiff({ aiDraft, edited }) {
  const { removed, added } = computeWordDiff(aiDraft, edited);
  return html`<div class="tr-wd2">
    <div class="tr-wl">
      <span class="tr-lbl">AI draft</span>
      <div>${removed.map(([op, text], i) => op === "-" ? html`<del key=${i}>${text}</del>` : text)}</div>
    </div>
    <div class="tr-wl">
      <span class="tr-lbl">Your edit</span>
      <div>${added.map(([op, text], i) => op === "+" ? html`<ins key=${i}>${text}</ins>` : text)}</div>
    </div>
  </div>`;
}

export function parseUnifiedDiff(text) {
  return String(text || "").split("\n").map((line) => {
    if (/^(\+\+\+|---|diff )/.test(line)) return { cls: "tr-diff-head", mark: "", text: line };
    if (line.startsWith("@@")) return { cls: "tr-diff-hunk", mark: "", text: line };
    if (line.startsWith("+")) return { cls: "tr-diff-add", mark: "+", text: line.slice(1) };
    if (line.startsWith("-")) return { cls: "tr-diff-del", mark: "−", text: line.slice(1) };
    return { cls: "", mark: "", text: line.startsWith(" ") ? line.slice(1) : line };
  });
}

export function UnifiedDiff({ text }) {
  const lines = parseUnifiedDiff(text);
  return html`<pre class="tr-diff">${lines.map((line, i) => html`<span key=${i} class=${`tr-diff-line ${line.cls}`.trim()}>
    <i class="tr-diff-mark">${line.mark}</i><span class="tr-diff-text">${line.text || " "}</span>
  </span>`)}</pre>`;
}

function LastLines({ lines }) {
  if (!Array.isArray(lines) || !lines.length) return null;
  return html`<div class="tr-sec">
    <div class="tr-sec-head"><span class="tr-lbl">Last lines</span></div>
    <pre class="tr-term">${lines.map((line, i) => isFailingLine(line)
      ? html`<span key=${i} class="tr-bad">${line}</span>`
      : html`<span key=${i}>${line}\n</span>`)}</pre>
  </div>`;
}

function JidokaGrid({ jidoka, editable = false, fixValue = "", preventValue = "", onFix, onPrevent, disabled = false }) {
  const j = jidoka ?? {};
  const cells = [
    ["detect", j.detect || ""],
    ["stop", j.stop || ""],
    ["fix", editable ? fixValue : (j.fix || "")],
    ["prevent", editable ? preventValue : (j.prevent || "")],
  ];
  return html`<div class="tr-jid">
    ${cells.map(([key, value]) => html`<div key=${key} class="tr-jid-cell">
      <div class="tr-lbl">${key}</div>
      ${editable && (key === "fix" || key === "prevent")
        ? html`<textarea class="tr-ta tr-ta-fit" rows=${textareaRows(value, 2)} disabled=${disabled}
            value=${value} aria-label=${key}
            onInput=${(e) => (key === "fix" ? onFix : onPrevent)?.(e.currentTarget.value)}></textarea>`
        : html`<div class="tr-jid-v">${value || "—"}</div>`}
    </div>`)}
  </div>`;
}

function normalizeChoiceOptions(item) {
  const questions = Array.isArray(item.detail?.questions) ? item.detail.questions : [];
  const firstQ = questions[0] ?? null;
  let raw = [];
  if (Array.isArray(firstQ?.options) && firstQ.options.length) {
    raw = firstQ.options;
  } else if (Array.isArray(item.options) && item.options.length) {
    raw = Array.isArray(item.options[0]) ? item.options[0] : item.options;
  }
  const options = raw.map((opt, idx) => {
    if (typeof opt === "string") return { key: idx + 1, label: opt, description: "" };
    return {
      key: Number(opt.key) || (idx + 1),
      label: String(opt.label ?? opt.title ?? opt.value ?? `Option ${idx + 1}`),
      description: String(opt.description ?? ""),
    };
  });
  return {
    questionText: firstQ?.question || item.detail?.prompt || item.detail?.why || "",
    contextText: item.detail?.tried || item.detail?.context || "",
    options,
    otherKey: options.length + 1,
  };
}

export function choicePayload(item, pick, otherText) {
  const { options, otherKey } = normalizeChoiceOptions(item);
  if (pick == null) return null;
  if (pick === otherKey) {
    const text = String(otherText || "").trim();
    if (!text) return null;
    const verb = hasVerb(item, "choose") ? "choose" : "answer";
    return { verb, body: { answer: text }, label: `Answered ${item.id} with custom answer` };
  }
  const found = options.find((opt) => opt.key === pick);
  if (!found) return null;
  const verb = hasVerb(item, "choose") ? "choose" : "answer";
  return { verb, body: { option: found.key }, label: `Answered ${item.id} with option ${found.key}` };
}

class ChoiceRenderer extends Component {
  state = { pick: null, otherText: "" };

  selectOption = (n) => {
    const { options, otherKey } = normalizeChoiceOptions(this.props.item);
    const maxKey = otherKey;
    if (n >= 1 && n <= maxKey && (n === otherKey || options.some((opt) => opt.key === n))) {
      this.setState({ pick: n });
      refreshKeybar();
    }
  };

  submit = async () => {
    const { item, busy, helpers } = this.props;
    if (busy) return;
    const spec = choicePayload(item, this.state.pick, this.state.otherText);
    if (!spec) {
      helpers.setNotice("pick 1 to 4 first");
      return;
    }
    await helpers.resolve(spec.verb, spec.body, { label: spec.label });
  };

  render({ item, busy, registerItemKeys }, { pick, otherText }) {
    const { questionText, contextText, options, otherKey } = normalizeChoiceOptions(item);
    const maxHint = Math.min(4, otherKey);
    registerItemKeys({
      itemId: item.id,
      pick: (n) => this.selectOption(n),
      submit: () => this.submit(),
      hints: [
        [[`1..${maxHint}`], "pick"],
        [["i"], "other"],
        [["Cmd Enter"], pick ? `answer ${pick}` : "answer"],
      ],
    });
    const btnText = pick ? `Answer with option ${pick}` : "pick 1 to 4 first";
    return html`
      ${questionText ? html`<div class="tr-para">${questionText}</div>` : null}
      ${contextText ? html`<div class="tr-ctx">${contextText}</div>` : null}
      <div class="tr-sec">
        <div class="tr-sec-head"><span class="tr-lbl">Options</span></div>
        ${options.map((opt) => {
          const on = pick === opt.key;
          return html`<button key=${opt.key} type="button" class=${`tr-opt${on ? " on" : ""}`}
            aria-pressed=${on ? "true" : "false"} onClick=${() => this.selectOption(opt.key)}>
            <span class="tr-radio" aria-hidden="true">${on ? "◉" : "○"}</span>
            <span class="tr-opt-body">
              <span class="tr-opt-t">${opt.label}</span>
              ${opt.description ? html`<div class="tr-opt-d">${opt.description}</div>` : null}
            </span>
            <kbd class="tr-cap">${opt.key}</kbd>
          </button>`;
        })}
        <div class=${`tr-opt${pick === otherKey ? " on" : ""}`} onClick=${() => this.selectOption(otherKey)}>
          <span class="tr-radio" aria-hidden="true">${pick === otherKey ? "◉" : "○"}</span>
          <div class="tr-opt-body">
            <div class="tr-opt-t">Other</div>
            <div class="tr-opt-other">
              <input class="tr-inp" type="text" placeholder="Your own answer, in free text"
                value=${otherText} disabled=${busy}
                onFocus=${() => this.selectOption(otherKey)}
                onInput=${(e) => this.setState({ pick: otherKey, otherText: e.currentTarget.value })} />
            </div>
          </div>
          <kbd class="tr-cap">${otherKey}</kbd>
        </div>
      </div>
      <div class="tr-acts">
        <button type="button" class="tr-btn tr-btn-primary" disabled=${busy || !pick} onClick=${this.submit}>
          ${btnText} <kbd class="tr-cap">Cmd ↵</kbd>
        </button>
        ${pick ? null : html`<span class="tr-hint-txt">pick 1 to 4 first</span>`}
      </div>
    `;
  }
}

class ApprovalRenderer extends Component {
  state = { note: "", confirmingRule: false };

  approve = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || !hasVerb(item, "approve")) return;
    const note = this.state.note.trim();
    const payload = note ? { answer: note } : {};
    await helpers.resolve("approve", payload, { label: `Approved ${item.id}` });
  };

  reject = () => {
    const { item, busy, helpers } = this.props;
    if (busy || !hasVerb(item, "reject")) return;
    helpers.openReason(`Reject ${item.id}`, "Reason for rejection (optional)", async (reason) => {
      await helpers.resolve("reject", { reason }, { label: `Rejected ${item.id}` });
    });
  };

  openRemember = () => {
    const { item, busy } = this.props;
    if (busy || !canApproveRemember(item)) return;
    this.setState({ confirmingRule: true });
  };

  confirmRemember = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || !canApproveRemember(item)) return;
    const rule = item.detail?.remember_rule ?? item.detail?.rule_draft ?? null;
    try {
      if (rule?.kind && rule?.name && rule?.body) {
        const draftUrl = `/api/v1/config/${encodeURIComponent(rule.kind)}/${encodeURIComponent(rule.name)}/draft`;
        await sendWrite(prepareWrite(draftUrl, {
          body: rule.body,
          why: rule.why || `Remembered from ${item.id}`,
          check: true,
        }, null, { method: "PUT" }));
      }
      const note = this.state.note.trim();
      const payload = note ? { answer: note } : {};
      await helpers.resolve("approve_remember", payload, { label: `Approved ${item.id} and remembered rule` });
      this.setState({ confirmingRule: false });
    } catch (error) {
      failed(error);
    }
  };

  render({ item, busy, registerItemKeys }, { note, confirmingRule }) {
    const d = item.detail ?? {};
    const rememberAllowed = canApproveRemember(item);
    const canApprove = hasVerb(item, "approve");
    const canReject = hasVerb(item, "reject");
    const effectText = d.approving || d.effect || d.plain_summary || d.what
      || (d.command ? `Runs ${d.command}${d.actor ? ` requested by ${d.actor}` : ""}` : item.title);
    const rule = d.remember_rule ?? d.rule_draft ?? null;
    const hints = [];
    if (canApprove) hints.push([["Cmd Enter"], "approve"]);
    if (canReject) hints.push([["dd"], "reject"]);
    if (rememberAllowed) hints.push([["m"], "approve + remember"]);
    hints.push([["i"], "note"]);

    registerItemKeys({
      itemId: item.id,
      submit: confirmingRule ? () => this.confirmRemember() : canApprove ? () => this.approve() : null,
      destructive: canReject ? () => this.reject() : null,
      remember: rememberAllowed ? () => this.openRemember() : null,
      hints,
    });

    return html`
      ${d.failed ? html`<div class="tr-sec"><div class="tr-panel tr-pad"><span class="tr-st tr-st-fault">▲ Failed</span> ${d.failed}</div></div>` : null}
      ${d.held ? html`<div class="tr-ctx">${d.held}</div>` : null}
      <div class="tr-sec">
        <div class="tr-sec-head"><span class="tr-lbl">What approving does</span></div>
        <div class="tr-panel tr-pad tr-para">${effectText}</div>
      </div>
      ${d.detail ? html`<div class="tr-sec"><div class="tr-sec-head"><span class="tr-lbl">Details</span></div><pre class="tr-term">${d.detail}</pre></div>` : null}
      ${d.args ? html`<div class="tr-sec"><div class="tr-sec-head"><span class="tr-lbl">Arguments</span></div><pre class="tr-term">${JSON.stringify(d.args, null, 2)}</pre></div>` : null}
      <div class="tr-sec">
        <div class="tr-sec-head"><span class="tr-lbl">Note for the run, optional</span></div>
        <textarea class="tr-ta" rows="2" placeholder="Sent with your answer, for example: plan only, no merge"
          value=${note} disabled=${busy} onInput=${(e) => this.setState({ note: e.currentTarget.value })}></textarea>
      </div>
      ${confirmingRule && rememberAllowed ? html`<div class="tr-drawer">
        <div class="tr-lbl">Confirm rule config draft</div>
        ${rule?.body ? html`<pre class="tr-term">${rule.body}</pre>`
          : html`<div class="tr-para">Save approval rule for ${item.line || item.subject} and approve ${item.id}.</div>`}
        <div class="tr-acts">
          <button type="button" class="tr-btn tr-btn-primary" disabled=${busy} onClick=${this.confirmRemember}>
            Confirm approve and remember <kbd class="tr-cap">Cmd ↵</kbd>
          </button>
          <button type="button" class="tr-btn tr-btn-ghost" disabled=${busy}
            onClick=${() => this.setState({ confirmingRule: false })}>Cancel</button>
        </div>
      </div>` : null}
      <div class="tr-acts">
        ${canApprove ? html`<button type="button" class="tr-btn tr-btn-primary" disabled=${busy} onClick=${this.approve}>
          Approve <kbd class="tr-cap">Cmd ↵</kbd>
        </button>` : null}
        ${canReject ? html`<button type="button" class="tr-btn" disabled=${busy} onClick=${this.reject}>
          Reject <kbd class="tr-cap">dd</kbd>
        </button>` : null}
        ${rememberAllowed ? html`<button type="button" class="tr-btn" disabled=${busy} onClick=${this.openRemember}>
          Approve and remember <kbd class="tr-cap">m</kbd>
        </button>` : null}
      </div>
      ${rememberAllowed ? null : html`<div class="tr-ctx">Approve and remember is off for this rule${d.remember_reason ? `: ${d.remember_reason}` : "."}</div>`}
    `;
  }
}

class FreeformRenderer extends Component {
  state = { answer: "" };

  submit = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || !hasVerb(item, "answer")) return;
    const text = this.state.answer.trim();
    if (!text) {
      helpers.setNotice("Write an answer first");
      return;
    }
    const step = item.detail?.step || item.detail?.node || "the paused step";
    await helpers.resolve("answer", { answer: text }, {
      label: `Answered ${item.id} · run resumes at ${step}`,
    });
  };

  render({ item, busy, registerItemKeys }, { answer }) {
    const d = item.detail ?? {};
    const questions = Array.isArray(d.questions) ? d.questions : [];
    const prompt = questions[0]?.question || d.why || d.summary || "";
    const step = d.step || d.node || "the paused step";
    registerItemKeys({
      itemId: item.id,
      submit: hasVerb(item, "answer") ? () => this.submit() : null,
      hints: [
        [["i"], "answer"],
        ...(hasVerb(item, "answer") ? [[["Cmd Enter"], "send answer"]] : []),
      ],
    });
    return html`
      ${prompt ? html`<div class="tr-para">${prompt}</div>` : null}
      ${d.tried ? html`<div class="tr-ctx">${d.tried}</div>` : null}
      <div class="tr-sec">
        <div class="tr-sec-head"><span class="tr-lbl">Your answer · resumes at ${step}</span></div>
        <textarea class="tr-ta" rows="5" placeholder=${`Answer the agent; it resumes at ${step} with this`}
          value=${answer} disabled=${busy} onInput=${(e) => this.setState({ answer: e.currentTarget.value })}></textarea>
      </div>
      <div class="tr-acts">
        <button type="button" class="tr-btn tr-btn-primary" disabled=${busy || !hasVerb(item, "answer")} onClick=${this.submit}>
          Send answer <kbd class="tr-cap">Cmd ↵</kbd>
        </button>
      </div>
    `;
  }
}

class ProposalRenderer extends Component {
  state = { loadedDiff: "", loaded: false };

  componentDidMount() {
    this.loadDiffIfNeeded();
  }

  async loadDiffIfNeeded() {
    const { item } = this.props;
    const d = item.detail ?? {};
    if (d.diff || d.change || d.body) return;
    try {
      const res = await webFetch(data.inboxItem(item.id), { cache: "no-store" });
      const body = await res.json().catch(() => null);
      const subj = body?.subject_object;
      const text = subj?.diff || subj?.body || "";
      if (text) this.setState({ loadedDiff: text, loaded: true });
    } catch {
      return;
    }
  }

  accept = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || !hasVerb(item, "accept")) return;
    await helpers.resolve("accept", {}, { label: `Accepted ${item.id}` });
  };

  decline = () => {
    const { item, busy, helpers } = this.props;
    if (busy || !hasVerb(item, "decline")) return;
    helpers.openReason(`Decline ${item.id}`, "Reason for declining (optional)", async (reason) => {
      await helpers.resolve("decline", { reason }, { label: `Declined ${item.id}` });
    });
  };

  render({ item, busy, registerItemKeys }, { loadedDiff }) {
    const d = item.detail ?? {};
    const why = d.why || d.summary || "";
    const diffText = d.diff || d.change || d.body || loadedDiff || "";
    const canAccept = hasVerb(item, "accept");
    const canDecline = hasVerb(item, "decline");
    const hints = [];
    if (canAccept) hints.push([["Cmd Enter"], "accept"]);
    if (canDecline) hints.push([["dd"], "decline"]);
    registerItemKeys({
      itemId: item.id,
      submit: canAccept ? () => this.accept() : null,
      destructive: canDecline ? () => this.decline() : null,
      hints,
    });
    return html`
      ${why ? html`<div class="tr-sec"><div class="tr-sec-head"><span class="tr-lbl">Why</span></div><div class="tr-para">${why}</div></div>` : null}
      ${diffText ? html`<div class="tr-sec"><div class="tr-sec-head"><span class="tr-lbl">Change</span></div><${UnifiedDiff} text=${diffText} /></div>` : null}
      <div class="tr-acts">
        ${canAccept ? html`<button type="button" class="tr-btn tr-btn-primary" disabled=${busy} onClick=${this.accept}>
          Accept <kbd class="tr-cap">Cmd ↵</kbd>
        </button>` : null}
        ${canDecline ? html`<button type="button" class="tr-btn" disabled=${busy} onClick=${this.decline}>
          Decline <kbd class="tr-cap">dd</kbd>
        </button>` : null}
      </div>
    `;
  }
}

export async function runSecretContinue(item, helpers) {
  const d = item.detail ?? {};
  const subject = String(item.subject || "");
  let scope = d.scope || "global";
  let name = d.name || "";
  if (subject.startsWith("secret:")) {
    const rest = subject.slice("secret:".length);
    const slash = rest.indexOf("/");
    if (slash > 0) {
      scope = scope || rest.slice(0, slash);
      name = name || rest.slice(slash + 1);
    }
  }
  if (name) {
    const checkUrl = `/api/v1/secrets/${encodeURIComponent(scope)}/${encodeURIComponent(name)}/check`;
    let checkRes;
    try {
      checkRes = await sendWrite(prepareWrite(checkUrl, {}));
    } catch (error) {
      const msg = `${name} check failed (${error.message}); the run stays paused`;
      helpers.setNotice(msg);
      helpers.setInlineStatus?.(msg, true);
      return { ok: false, checked: false };
    }
    const sec = checkRes?.secret ?? checkRes;
    const passed = Boolean(sec?.check_ok) && sec?.present !== false && sec?.status !== "missing";
    if (!passed) {
      const msg = `${name} check did not pass — the run stays paused`;
      helpers.setNotice(msg);
      helpers.setInlineStatus?.(msg, true);
      return { ok: false, checked: true };
    }
  }
  if (!hasVerb(item, "continue")) return { ok: false, checked: true };
  const resolved = await helpers.resolve("continue", {}, {
    label: name ? `Continued ${item.id} · ${name} verified` : `Continued ${item.id}`,
  });
  return { ok: Boolean(resolved), checked: true, resolved };
}

function waitingEntryOf(raw) {
  if (raw && typeof raw === "object") {
    const kind = raw.kind || "run";
    const id = String(raw.id || "");
    const href = kind === "outbox" ? data.outboxHash(id) : data.runHash(id);
    return { kind, id, href, state: raw.state || (kind === "outbox" ? "FAILED" : "PAUSED"), text: raw.text || "" };
  }
  const text = String(raw || "");
  if (text.startsWith("run:")) {
    const id = text.slice(4);
    return { kind: "run", id, href: data.runHash(id), state: "PAUSED", text: "waiting on credential" };
  }
  if (text.startsWith("outbox:")) {
    const id = `OB-${text.slice(7)}`;
    return { kind: "outbox", id, href: data.outboxHash(id), state: "FAILED", text: "delivery waiting on credential" };
  }
  return { kind: "item", id: text, href: "", state: "WAITING", text: "" };
}

class ContinueRenderer extends Component {
  runContinue = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || !hasVerb(item, "continue")) return;
    await runSecretContinue(item, helpers);
  };

  runStop = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || !hasVerb(item, "stop")) return;
    await helpers.resolve("stop", {}, { label: `Stopped ${item.id}` });
  };

  render({ item, busy, helpers, registerItemKeys }) {
    const d = item.detail ?? {};
    const name = d.name || (String(item.subject || "").startsWith("secret:") ? item.subject.split("/").slice(1).join("/") : "");
    const scope = d.scope || "global";
    const checkOk = d.last_check?.ok === true;
    const fixRoute = d.fix?.route || "#settings/credentials";
    const fixLabel = d.fix?.label || "Settings › Credentials";
    const waitingRaw = Array.isArray(d.waiting) ? d.waiting
      : Array.isArray(d.group?.items) ? d.group.items
        : Array.isArray(d.group_members) ? d.group_members : [];
    const waiting = waitingRaw.map(waitingEntryOf);
    const jidoka = d.jidoka ?? {
      detect: name ? `${name} is missing in ${scope}` : item.title,
      stop: waiting.length ? `${waiting.length} waiting paused until credential is set` : "Paused waiting for prerequisite",
      fix: name ? `Add ${name} in ${fixLabel}, then run Continue` : "Complete prerequisite and continue",
      prevent: name ? `Verify ${name} with credential checks before starting runs` : "Verify prerequisites before launch",
    };

    const canContinue = hasVerb(item, "continue");
    const canStop = hasVerb(item, "stop");
    const hints = [];
    if (canContinue) hints.push([["Cmd Enter"], "continue"]);
    hints.push([["gx"], "open setting"]);
    if (canStop) hints.push([["dd"], "stop"]);

    registerItemKeys({
      itemId: item.id,
      submit: canContinue ? () => this.runContinue() : null,
      destructive: canStop ? () => this.runStop() : null,
      openLink: () => helpers.openHash(fixRoute),
      hints,
    });

    return html`
      ${name ? html`<div class="tr-sec">
        <div class="tr-sec-head"><span class="tr-lbl">Missing credential</span></div>
        <div class="tr-panel tr-pad">
          <div class="tr-kv">
            <span class="tr-lbl">Name</span>
            <span class="tr-kv-v tr-kv-strong">${name} <span class=${`tr-chip ${checkOk ? "tr-chip-ok" : "tr-chip-high"}`}>${checkOk ? "✓ SET" : "◆ MISSING"}</span></span>
            <span class="tr-lbl">Scope</span>
            <span class="tr-kv-v">${scope}</span>
            <span class="tr-lbl">Check</span>
            <span class="tr-kv-v"><span class=${`tr-st ${checkOk ? "tr-st-ok" : "tr-st-fault"}`}>${checkOk ? "✓ passed" : "▲ not passed"}</span>${d.last_check?.checked_at ? ` · ${helpers.formatStamp(d.last_check.checked_at)}` : ""}</span>
            <span class="tr-lbl">Fix in</span>
            <span class="tr-kv-v"><a class="tr-link" href=${fixRoute}>${fixLabel}</a></span>
          </div>
        </div>
      </div>` : null}
      ${waiting.length ? html`<div class="tr-sec">
        <div class="tr-sec-head"><span class="tr-lbl">Waiting on it · ${waiting.length}</span></div>
        <div class="tr-panel">
          ${waiting.map((entry, i) => entry.href
            ? html`<a key=${`${entry.id}-${i}`} class="tr-gitem" href=${entry.href}>
                <span class=${`tr-chip ${entry.kind === "outbox" ? "tr-chip-fault" : "tr-chip-high"}`}>${entry.kind === "outbox" ? "▲" : "◆"} ${entry.state}</span>
                <span class="mono tr-link">${entry.id}</span>
                <span>${entry.text}</span>
              </a>`
            : html`<div key=${`${entry.id}-${i}`} class="tr-gitem">
                <span class="tr-chip tr-chip-med">${entry.state}</span>
                <span class="mono">${entry.id}</span>
                <span>${entry.text}</span>
              </div>`)}
        </div>
      </div>` : null}
      <div class="tr-sec">
        <div class="tr-sec-head"><span class="tr-lbl">Jidoka</span></div>
        <${JidokaGrid} jidoka=${jidoka} />
      </div>
      <div class="tr-acts">
        ${canContinue ? html`<button type="button" class="tr-btn tr-btn-primary" disabled=${busy} onClick=${this.runContinue}>
          Continue <kbd class="tr-cap">Cmd ↵</kbd>
        </button>` : null}
        <a class="tr-btn" href=${fixRoute}>Open the setting <kbd class="tr-cap">gx</kbd></a>
        ${canStop ? html`<button type="button" class="tr-btn" disabled=${busy} onClick=${this.runStop}>
          Stop the run <kbd class="tr-cap">dd</kbd>
        </button>` : null}
      </div>
    `;
  }
}

export function stopActionSpec(item, verb, { fix = "", prevent = "" } = {}) {
  if (!hasVerb(item, verb)) return null;
  if (verb === "redirect") {
    const cleanFix = String(fix || "").trim();
    if (!cleanFix) return null;
    return { verb: "redirect", body: { fix: cleanFix, prevent: String(prevent || "").trim() }, label: `Redirected ${item.id} with fix` };
  }
  if (verb === "retry") return { verb: "retry", body: {}, label: `Retried step for ${item.id}` };
  if (verb === "stop") return { verb: "stop", body: {}, label: `Stopped run for ${item.id}` };
  return null;
}

class StopRenderer extends Component {
  constructor(props) {
    super(props);
    const j = props.item?.detail?.jidoka ?? {};
    this.state = { fix: j.fix ?? "", prevent: j.prevent ?? "" };
  }

  redirect = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || !hasVerb(item, "redirect")) return;
    const spec = stopActionSpec(item, "redirect", this.state);
    if (!spec) {
      helpers.setNotice("Enter a fix before redirecting");
      return;
    }
    await helpers.resolve(spec.verb, spec.body, { label: spec.label });
  };

  retryStep = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || !hasVerb(item, "retry")) return;
    const spec = stopActionSpec(item, "retry");
    if (spec) await helpers.resolve(spec.verb, spec.body, { label: spec.label });
  };

  stopRun = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || !hasVerb(item, "stop")) return;
    const spec = stopActionSpec(item, "stop");
    if (spec) await helpers.resolve(spec.verb, spec.body, { label: spec.label });
  };

  render({ item, busy, helpers, registerItemKeys }, { fix, prevent }) {
    const d = item.detail ?? {};
    const canRedirect = hasVerb(item, "redirect");
    const canRetry = hasVerb(item, "retry");
    const canStop = hasVerb(item, "stop");
    const runId = item.run_id || d.run_id || "";
    const hints = [];
    if (canRedirect) hints.push([["Cmd Enter"], "redirect"]);
    if (canRetry) hints.push([["⇧r"], "retry step"]);
    if (canStop) hints.push([["dd"], "stop run"]);
    if (runId) hints.push([["gx"], "open run"]);
    hints.push([["i"], "edit fix"]);

    registerItemKeys({
      itemId: item.id,
      submit: canRedirect ? () => this.redirect() : null,
      retry: canRetry ? () => this.retryStep() : null,
      destructive: canStop ? () => this.stopRun() : null,
      openLink: runId ? () => helpers.openHash(data.runHash(runId)) : null,
      hints,
    });

    return html`
      <div class="tr-sec">
        <div class="tr-sec-head"><span class="tr-lbl">Detect · stop · fix · prevent</span></div>
        <${JidokaGrid} jidoka=${d.jidoka} editable=${true} fixValue=${fix} preventValue=${prevent}
          disabled=${busy}
          onFix=${(v) => this.setState({ fix: v })}
          onPrevent=${(v) => this.setState({ prevent: v })} />
      </div>
      <${LastLines} lines=${d.last_lines} />
      <div class="tr-acts">
        ${canRedirect ? html`<button type="button" class="tr-btn tr-btn-primary" disabled=${busy} onClick=${this.redirect}>
          Redirect with this fix <kbd class="tr-cap">Cmd ↵</kbd>
        </button>` : null}
        ${canRetry ? html`<button type="button" class="tr-btn" disabled=${busy} onClick=${this.retryStep}>
          Retry the step <kbd class="tr-cap">⇧r</kbd>
        </button>` : null}
        ${canStop ? html`<button type="button" class="tr-btn" disabled=${busy} onClick=${this.stopRun}>
          Stop the run <kbd class="tr-cap">dd</kbd>
        </button>` : null}
      </div>
    `;
  }
}

class FaultRenderer extends Component {
  retryNow = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || !hasVerb(item, "retry")) return;
    await helpers.resolve("retry", {}, { label: `Retried ${item.id} now` });
  };

  dismiss = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || !hasVerb(item, "dismiss")) return;
    await helpers.resolve("dismiss", {}, { label: `Dismissed ${item.id}` });
  };

  render({ item, busy, helpers, registerItemKeys }) {
    const d = item.detail ?? {};
    const attempts = Number(d.attempts) || 1;
    const errorText = d.error || item.title;
    const runId = item.run_id || d.run_id || "";
    const shelvedReason = d.shelved_reason || item.shelve_reason || "";
    const shelveUntil = d.shelve_until || item.shelve_until || "";
    const canRetry = hasVerb(item, "retry");
    const canDismiss = hasVerb(item, "dismiss");
    const hints = [];
    if (canRetry) hints.push([["Cmd Enter", "⇧r"], "retry now"]);
    if (runId) hints.push([["gx"], "open run"]);
    if (canDismiss) hints.push([["dd"], "dismiss"]);

    registerItemKeys({
      itemId: item.id,
      submit: canRetry ? () => this.retryNow() : null,
      retry: canRetry ? () => this.retryNow() : null,
      destructive: canDismiss ? () => this.dismiss() : null,
      openLink: runId ? () => helpers.openHash(data.runHash(runId)) : null,
      hints,
    });

    return html`
      <div class="tr-sec">
        <div class="tr-sec-head"><span class="tr-lbl">Error · ${attempts} ${attempts === 1 ? "attempt" : "attempts"}</span></div>
        <pre class="tr-term"><span class="tr-bad">▲ ${errorText}</span></pre>
      </div>
      <${LastLines} lines=${d.last_lines} />
      ${shelvedReason ? html`<div class="tr-sec">
        <div class="tr-sec-head"><span class="tr-lbl">■ Shelved</span></div>
        <div class="tr-panel tr-pad">${shelvedReason}${shelveUntil ? ` · returns ${helpers.formatStamp(shelveUntil)}` : ""}</div>
      </div>` : null}
      <div class="tr-acts">
        ${canRetry ? html`<button type="button" class="tr-btn tr-btn-primary" disabled=${busy} onClick=${this.retryNow}>
          Retry now <kbd class="tr-cap">Cmd ↵</kbd><kbd class="tr-cap">⇧r</kbd>
        </button>` : null}
        ${runId ? html`<a class="tr-btn" href=${data.runHash(runId)}>Open the run <kbd class="tr-cap">gx</kbd></a>` : null}
        ${canDismiss ? html`<button type="button" class="tr-btn tr-btn-ghost" disabled=${busy} onClick=${this.dismiss}>
          Dismiss <kbd class="tr-cap">dd</kbd>
        </button>` : null}
      </div>
    `;
  }
}

export function outboxIdsFor(item) {
  const d = item.detail ?? {};
  const list = [];
  if (Array.isArray(d.group?.items)) {
    for (const entry of d.group.items) {
      const id = typeof entry === "string" ? entry : entry?.id;
      if (id) list.push(String(id));
    }
  } else if (Array.isArray(d.outbox_ids) && d.outbox_ids.length) {
    for (const id of d.outbox_ids) list.push(String(id));
  } else if (Array.isArray(d.group_members) && d.group_members.length) {
    for (const m of d.group_members) {
      const s = String(m);
      list.push(s.startsWith("outbox:") ? `OB-${s.slice(7)}` : s);
    }
  }
  if (!list.length) {
    if (item.outbox_id) list.push(String(item.outbox_id));
    else if (d.outbox_id) list.push(String(d.outbox_id));
    else if (String(item.subject || "").startsWith("outbox:")) list.push(`OB-${item.subject.slice(7)}`);
  }
  return [...new Set(list)];
}

export function formatSentNotice(drafts) {
  if (!drafts.length) return "Sent outbox draft";
  if (drafts.length === 1) {
    const ob = drafts[0];
    const target = TRACKER_NAMES[String(ob.target || "").toLowerCase()] || ob.target || "";
    return target ? `Sent ${ob.id} to ${target}` : `Sent ${ob.id}`;
  }
  return `Sent ${drafts.map((ob) => ob.id).join(" and ")}`;
}

export async function approveOutboxDrafts(item, loadedDrafts, editedTexts, helpers) {
  if (!loadedDrafts.length) {
    if (hasVerb(item, "approve")) {
      return Boolean(await helpers.resolve("approve", {}, { label: `Approved ${item.id}` }));
    }
    return false;
  }
  const approvedList = [];
  for (const entry of loadedDrafts) {
    let ob = entry.item;
    const latestText = entry.latestBody;
    const currentText = editedTexts[ob.id] ?? latestText;
    if (ob.state === "failed") {
      const res = await sendWrite(prepareWrite(`/api/v1/outbox/${encodeURIComponent(ob.id)}/retry`, {}, { version: ob.version }));
      approvedList.push(res.item ?? ob);
      continue;
    }
    if (currentText !== latestText) {
      const patchRes = await sendWrite(prepareWrite(`/api/v1/outbox/${encodeURIComponent(ob.id)}`, {
        base_rev: ob.rev,
        body: currentText,
      }, { version: ob.version }, { method: "PATCH" }));
      ob = patchRes.item ?? { ...ob, rev: ob.rev + 1, version: ob.version + 1 };
    }
    if (ob.audience !== "self" && !ob.opened_at) {
      const openRes = await sendWrite(prepareWrite(`/api/v1/outbox/${encodeURIComponent(ob.id)}/open`, {}, { version: ob.version }));
      ob = openRes.item ?? { ...ob, version: ob.version + 1, opened_at: new Date().toISOString() };
    }
    const approveRes = await sendWrite(prepareWrite(`/api/v1/outbox/${encodeURIComponent(ob.id)}/approve`, {
      rev: ob.rev,
    }, { version: ob.version }));
    approvedList.push(approveRes.item ?? ob);
  }
  const msg = formatSentNotice(approvedList);
  helpers.advance();
  succeeded(msg, false);
  helpers.refresh();
  return true;
}

class OutboxRenderer extends Component {
  state = { loading: true, error: "", drafts: [], texts: {}, saving: false };

  componentDidMount() {
    this.loadDrafts();
  }

  async loadDrafts() {
    const ids = outboxIdsFor(this.props.item);
    if (!ids.length) {
      this.setState({ loading: false, drafts: [] });
      return;
    }
    try {
      const loaded = await Promise.all(ids.map(async (id) => {
        const res = await webFetch(`/api/v1/outbox/${encodeURIComponent(id)}`, { cache: "no-store" });
        const body = await res.json().catch(() => null);
        if (!res.ok || !body?.item) {
          throw new Error(body?.detail ?? body?.error ?? `Could not load ${id} (${res.status})`);
        }
        const revisions = Array.isArray(body.revisions) ? body.revisions : [];
        const aiBody = revisions[0]?.body ?? body.item.summary ?? "";
        const latestBody = revisions[revisions.length - 1]?.body ?? aiBody;
        return { item: body.item, revisions, aiBody, latestBody };
      }));
      const texts = {};
      for (const entry of loaded) texts[entry.item.id] = entry.latestBody;
      this.setState({ loading: false, error: "", drafts: loaded, texts });
    } catch (error) {
      this.setState({ loading: false, error: error?.message || "Failed to load draft" });
    }
  }

  approveAll = async () => {
    const { item, busy, helpers } = this.props;
    if (busy || this.state.saving) return;
    this.setState({ saving: true });
    try {
      await approveOutboxDrafts(item, this.state.drafts, this.state.texts, helpers);
    } catch (error) {
      failed(error);
    } finally {
      this.setState({ saving: false });
    }
  };

  discard = () => {
    const { item, busy, helpers } = this.props;
    if (busy || this.state.saving || !hasVerb(item, "reject")) return;
    helpers.openReason(`Discard ${item.id}`, "Reason for discarding (optional)", async (reason) => {
      this.setState({ saving: true });
      try {
        if (this.state.drafts.length > 1) {
          for (const entry of this.state.drafts) {
            await sendWrite(prepareWrite(`/api/v1/outbox/${encodeURIComponent(entry.item.id)}/reject`, {
              reason,
            }, { version: entry.item.version }));
          }
          helpers.advance();
          succeeded(`Discarded ${this.state.drafts.map((d) => d.item.id).join(" and ")}`, false);
          helpers.refresh();
        } else {
          await helpers.resolve("reject", { reason }, { label: `Discarded ${item.id}` });
        }
      } catch (error) {
        failed(error);
      } finally {
        this.setState({ saving: false });
      }
    });
  };

  render({ item, busy, registerItemKeys }, { loading, error, drafts, texts, saving }) {
    const disabled = busy || saving;
    const canApprove = hasVerb(item, "approve");
    const canReject = hasVerb(item, "reject");
    const count = drafts.length;
    const primaryLabel = count > 1 ? `Approve and send all ${count}` : "Approve and send";
    const hints = [];
    if (canApprove) hints.push([["Cmd Enter"], count > 1 ? `send all ${count}` : "approve + send"]);
    if (canReject) hints.push([["dd"], "discard"]);
    hints.push([["i"], "edit draft"]);

    registerItemKeys({
      itemId: item.id,
      submit: canApprove ? () => this.approveAll() : null,
      destructive: canReject ? () => this.discard() : null,
      hints,
    });

    if (loading) return html`<div class="tr-sec"><p class="tr-hint-txt">Loading outbox draft…</p></div>`;
    if (error && !drafts.length) {
      return html`<div class="tr-sec">
        <p class="tr-status tr-status-err" role="alert">${error}</p>
        <div class="tr-acts">
          ${canApprove ? html`<button type="button" class="tr-btn tr-btn-primary" disabled=${disabled} onClick=${this.approveAll}>Approve and send <kbd class="tr-cap">Cmd ↵</kbd></button>` : null}
          ${canReject ? html`<button type="button" class="tr-btn" disabled=${disabled} onClick=${this.discard}>Discard <kbd class="tr-cap">dd</kbd></button>` : null}
        </div>
      </div>`;
    }

    return html`
      <div class="tr-sec">
        ${drafts.map((entry) => {
          const ob = entry.item;
          const text = texts[ob.id] ?? entry.latestBody;
          const isEdited = text !== entry.aiBody || entry.revisions.length > 1;
          const sys = TRACKER_NAMES[String(ob.target || "").toLowerCase()] || ob.target || "Destination";
          return html`<div key=${ob.id} class="tr-panel tr-pad tr-ob-card">
            <div class="tr-kv">
              <span class="tr-lbl">To</span>
              <span class="tr-kv-v">${sys}${ob.object ? ` · ${ob.object}` : ""}</span>
              <span class="tr-lbl">Audience</span>
              <span class="tr-kv-v">${ob.audience || "—"}</span>
              <span class="tr-lbl">Posts as</span>
              <span class="tr-kv-v">${ob.posts_as || ob.drafted_by || "—"}</span>
              <span class="tr-lbl">State</span>
              <span class="tr-kv-v">
                <span class=${`tr-chip ${ob.state === "failed" ? "tr-chip-fault" : "tr-chip-med"}`}>${ob.state}</span>
                ${" "}<a class="tr-link" href=${data.outboxHash(ob.id)}>${ob.id}</a>
                ${ob.task_id ? html` · <a class="tr-link" href=${data.taskHash(ob.task_id)}>${ob.task_id}</a>` : null}
              </span>
            </div>
            ${isEdited ? html`<div class="tr-lbl tr-ob-sub">AI draft, then your edit</div><${WordDiff} aiDraft=${entry.aiBody} edited=${text} />` : null}
            <div class="tr-lbl tr-ob-sub">${isEdited ? "Your edit, will send" : "Draft, will send"}</div>
            <textarea class="tr-ta" rows=${count > 1 ? 3 : 4} disabled=${disabled}
              aria-label=${`Draft ${ob.id}`} value=${text}
              onInput=${(e) => this.setState({ texts: { ...this.state.texts, [ob.id]: e.currentTarget.value } })}></textarea>
          </div>`;
        })}
      </div>
      <div class="tr-acts">
        ${canApprove ? html`<button type="button" class="tr-btn tr-btn-primary" disabled=${disabled} onClick=${this.approveAll}>
          ${primaryLabel} <kbd class="tr-cap">Cmd ↵</kbd>
        </button>` : null}
        ${canReject ? html`<button type="button" class="tr-btn" disabled=${disabled} onClick=${this.discard}>
          Discard <kbd class="tr-cap">dd</kbd>
        </button>` : null}
      </div>
    `;
  }
}

export function conflictResolution(item, pick) {
  if (pick === 1 && hasVerb(item, "keep_local")) {
    return { kind: "resolve", verb: "keep_local", label: `Kept local values for ${item.id}` };
  }
  if (pick === 2 && hasVerb(item, "take_remote")) {
    return { kind: "resolve", verb: "take_remote", label: `Took remote values for ${item.id}` };
  }
  if (pick === 3) {
    const taskId = item.task_id || item.detail?.task || "";
    return { kind: "navigate", hash: taskId ? data.taskHash(taskId) : "#board" };
  }
  return null;
}

class ConflictRenderer extends Component {
  state = { pick: null };

  select = (n) => {
    if (n >= 1 && n <= 3) {
      this.setState({ pick: n });
      refreshKeybar();
    }
  };

  apply = async () => {
    const { item, busy, helpers } = this.props;
    if (busy) return;
    const plan = conflictResolution(item, this.state.pick);
    if (!plan) {
      helpers.setNotice("pick 1 to 3 first");
      return;
    }
    if (plan.kind === "navigate") {
      helpers.openHash(plan.hash);
      return;
    }
    await helpers.resolve(plan.verb, {}, { label: plan.label });
  };

  render({ item, busy, helpers, registerItemKeys }, { pick }) {
    const d = item.detail ?? {};
    const sys = TRACKER_NAMES[String(d.system || "jira").toLowerCase()] || d.system || "Jira";
    const fields = Array.isArray(d.fields) ? d.fields : [];
    const options = [
      [1, "Keep local", `Arete's values win and go back to ${sys}`, hasVerb(item, "keep_local")],
      [2, "Take remote", `${sys}'s values replace Arete's`, hasVerb(item, "take_remote")],
      [3, "Merge by hand", "Open the task and pick each field yourself", true],
    ];
    const applyLabels = { 1: "Apply keep local", 2: "Apply take remote", 3: "Apply merge by hand" };

    registerItemKeys({
      itemId: item.id,
      pick: (n) => this.select(n),
      submit: () => this.apply(),
      hints: [
        [["1..3"], "pick"],
        [["Cmd Enter"], pick ? applyLabels[pick].toLowerCase() : "apply"],
      ],
    });

    return html`
      <div class="tr-sec">
        <div class="tr-sec-head"><span class="tr-lbl">Local against remote</span></div>
        <div class="tr-panel tr-table-wrap">
          <table class="tr-tbl">
            <thead>
              <tr>
                <th class="tr-lbl">Field</th>
                <th class="tr-lbl">Arete</th>
                <th class="tr-lbl">${sys}</th>
                <th class="tr-lbl">Changed by</th>
              </tr>
            </thead>
            <tbody>
              ${fields.map((f, i) => html`<tr key=${`${f.field}-${i}`}>
                <td class="mono">${f.field}</td>
                <td>${f.local ?? ""}</td>
                <td>${f.remote ?? ""}</td>
                <td class="mono muted">${[f.remote_by || f.remoteBy, f.remote_at || f.remoteAt ? helpers.formatStamp(f.remote_at || f.remoteAt) : ""].filter(Boolean).join(" · ")}</td>
              </tr>`)}
            </tbody>
          </table>
        </div>
      </div>
      <div class="tr-sec">
        <div class="tr-sec-head"><span class="tr-lbl">Resolve</span></div>
        ${options.map(([key, title, desc, enabled]) => {
          const on = pick === key;
          return html`<button key=${key} type="button" class=${`tr-opt${on ? " on" : ""}`}
            disabled=${busy || !enabled} aria-pressed=${on ? "true" : "false"}
            onClick=${() => this.select(key)}>
            <span class="tr-radio" aria-hidden="true">${on ? "◉" : "○"}</span>
            <span class="tr-opt-body">
              <span class="tr-opt-t">${title}</span>
              <div class="tr-opt-d">${desc}</div>
            </span>
            <kbd class="tr-cap">${key}</kbd>
          </button>`;
        })}
      </div>
      <div class="tr-acts">
        <button type="button" class="tr-btn tr-btn-primary" disabled=${busy || !pick} onClick=${this.apply}>
          ${pick ? applyLabels[pick] : "Apply"} <kbd class="tr-cap">Cmd ↵</kbd>
        </button>
        ${pick ? null : html`<span class="tr-hint-txt">pick 1 to 3 first</span>`}
      </div>
    `;
  }
}

registerKind("choice", ChoiceRenderer);
registerKind("approval", ApprovalRenderer);
registerKind("freeform", FreeformRenderer);
registerKind("proposal", ProposalRenderer);
registerKind("continue", ContinueRenderer);
registerKind("stop", StopRenderer);
registerKind("fault", FaultRenderer);
registerKind("outbox", OutboxRenderer);
registerKind("conflict", ConflictRenderer);
