import { html, Operation, Text } from "./ui-operations.js";
import { runHash } from "./ui-data.js";

function Question({ question: q, index }) {
  const name = `q${index}`;
  return html`<fieldset class="ops-question">
    <legend>${q.header || `Question ${index + 1}`}</legend>
    <${Text} text=${q.question} />
    ${q.kind === "choice" ? html`
      ${q.options.map((option, i) => html`<label key=${i} class="ops-choice">
        <input name=${name} type=${q.multiple ? "checkbox" : "radio"} value=${i + 1} />
        <span>${option.label}${i === 0 ? html` <small>Recommended</small>` : null}
          ${option.description ? html`<span class="muted">${option.description}</span>` : null}</span>
      </label>`)}
      <label class="ops-field">Other <input name=${`${name}-other`} type="text" maxlength="20000" /></label>`
    : q.kind === "secret" ? html`
      <label class="ops-field">${q.name}<input name=${name} type="password" autocomplete="off" spellcheck="false" /></label>
      <label class="ops-choice"><input name=${`${name}-decline`} type="checkbox" /> Decline to provide a value</label>`
    : q.kind === "done" ? html`
      ${q.steps?.length ? html`<ol>${q.steps.map((step, i) => html`<li key=${i}>${step}</li>`)}</ol>` : null}
      <label class="ops-choice"><input name=${name} type="radio" value="done" /> Done</label>
      <label class="ops-choice"><input name=${name} type="radio" value="declined" /> Can't do this</label>
      <label class="ops-field">Note (optional) <input name=${`${name}-note`} type="text" maxlength="19980" /></label>`
    : html`<label class="ops-field">Answer <textarea name=${name} rows="4" maxlength="20000" /></label>`}
  </fieldset>`;
}

function answerFor(fields, q, index, recommended) {
  const name = `q${index}`;
  const value = fields.get(name);
  if (q.kind === "choice") {
    if (recommended) return null;
    const picks = fields.getAll(name).map(Number);
    const other = fields.get(`${name}-other`)?.trim();
    if (other) return { text: q.multiple ? [...picks.map((n) => q.options[n - 1].label), other].join(", ") : other };
    return picks.length ? { choice: q.multiple ? picks : picks[0] } : null;
  }
  if (q.kind === "secret") {
    if (fields.has(`${name}-decline`)) return { declined: true };
    return value ? { secret: value } : null;
  }
  if (q.kind === "done") {
    if (!value) return null;
    const note = fields.get(`${name}-note`)?.trim();
    const done = value === "done";
    return note ? { text: `${done ? "Done" : "Can't do this"}: ${note}` } : done ? { done: true } : { declined: true };
  }
  return value?.trim() ? { text: value } : null;
}

function askResult(result) {
  if (["waiting", "stopped"].includes(result.state)) return result.why || `Run ${result.run} is ${result.state}`;
  return result.run ? `Answer recorded · ${result.run}` : "Answer recorded";
}

export class AskForm extends Operation {
  submit = (event) => {
    event.preventDefault();
    const { ask, disabled } = this.props;
    if (this.state.busy || disabled || this.state.stopping) return;
    const form = event.currentTarget;
    const recommended = event.submitter?.value === "recommended";
    const fields = new FormData(form);
    const answers = ask.questions.map((q, i) => answerFor(fields, q, i, recommended));
    const missing = answers.findIndex((a, i) => a == null && !(recommended && ask.questions[i].kind === "choice"));
    if (missing !== -1) {
      this.setState({ error: `Answer ${ask.questions[missing].header || `question ${missing + 1}`} first.` });
      form.querySelectorAll(".ops-question")[missing]?.querySelector("input, textarea")?.focus();
      return;
    }
    const secrets = [...form.querySelectorAll('input[type="password"]')];
    this.send(this.path, { answers, recommended }, askResult, {
      clearSecret: secrets.length ? () => { for (const field of secrets) field.value = ""; } : undefined,
    });
  };
  render({ ask, disabled }, { busy, error, stopping }) {
    this.path = `/api/asks/${encodeURIComponent(ask.id)}/answer`;
    return html`<section class="ops-detail">
      <header><p class="ops-kicker">Question · ${ask.repo}</p><h3>${ask.ticket || ask.run}</h3>
        <p><a href=${runHash(ask.run)}>Open run</a>${ask.step ? ` · ${ask.step}` : ""}</p></header>
      <${Text} text=${ask.why} /><${Text} text=${ask.tried} />
      <form class="ops-form" onSubmit=${this.submit} autocomplete="off" aria-label="Answer questions">
        <fieldset disabled=${busy || disabled || stopping}>${ask.questions.map((q, i) => html`<${Question} key=${i} question=${q} index=${i} />`)}</fieldset>
        <div class="ops-actions">
          ${stopping ? html`<p>End this run without sending an answer?</p>
            <button type="button" disabled=${busy || disabled} onClick=${async () => {
    if (await this.send(this.path, { stop: true }, askResult)) this.setState({ stopping: false });
  }}>Confirm stop</button>
            <button type="button" disabled=${busy} onClick=${() => this.setState({ stopping: false, error: "" })}>Keep waiting</button>`
    : html`<button type="submit" disabled=${busy || disabled}>Submit answer</button>
            ${ask.questions.some((q) => q.kind === "choice") ? html`<button type="submit" value="recommended" disabled=${busy || disabled}>Use recommendations</button>` : null}
            <button type="button" disabled=${busy || disabled} onClick=${() => this.setState({ stopping: true, error: "" })}>Stop the run</button>`}
        </div>
        ${error ? html`<p class="ops-error" role="alert">${error}</p>` : null}
      </form>
    </section>`;
  }
}
