import { notice, mode } from "./ui-keys.js";

const KEPT = "arete.feedback";
const SAVE_MS = 600;
const RETRY_MS = 5000;
const READ_MS = 8000;
const NOTICE_MS = 8000;
const TEXTS = ["text", "expected", "actual", "steps"];
const EMPTY = { kind: "idea", text: "", expected: "", actual: "", steps: "" };
const ON_DEVICE = "The draft is kept on this device, and it saves again when the server answers.";
const MB = 1024 * 1024;
const TYPES = { "image/png": "PNG image", "image/jpeg": "JPEG image", "image/webp": "WebP image", "video/webm": "WebM video", "video/mp4": "MP4 video" };
const SOURCES = { screenshot: "Screenshot", recording: "Recording", attached: "Attached" };
const RECORDER_TYPES = ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm", "video/mp4"];
const NO_CAPTURE = "This browser cannot capture the screen here. Take a screenshot with the device and use Attach a file.";
const LABELS = { text: "Request", expected: "Expected", actual: "Actual", steps: "Steps" };
const KINDS = [["idea", "Idea"], ["adjustment", "Adjustment"], ["bug", "Bug"]];
const LENGTH_SLACK = 0.5;
const FLAGS = { credential: "a credential", personal: "private personal information", private_address: "a private address", agent_text: "text addressed to an agent" };
const CHIPS = { pass: "ok", hold: "caution", interrupted: "caution", fail: "fault" };
const CHIP_WORDS = { ok: "OK", caution: "Caution", fault: "Fault" };
const LOCKED = ["include", "context-again", "context-drop", "shot", "record", "attach", "discard"];
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), textarea:not([disabled]), summary, pre[tabindex], [tabindex="0"]';

export const feedbackCommand = { id: "send-feedback", title: "Send feedback", description: "an idea, an adjustment or a bug, with a screenshot or recording",
  glyph: "✎", run: () => openFeedback() };

let limits = { image_bytes: 5 * MB, video_bytes: 25 * MB, evidence_items: 6, evidence_bytes: 40 * MB, video_seconds: 30 };
let refs = {};
let dialog = null;
let returnTo = null;
let timer = 0;
let dirty = false;
let saving = false;
let read = false;
let discarding = false;
let saveWaits = null;
let base = { ...EMPTY };
let line = Promise.resolve();
let draft = null;
let filed = null;
let aside = false;
let capturing = false;
let stopRecording = null;
let pending = [];
let uploads = Promise.resolve();
let keys = 0;
let checksShown = "";
let polling = 0;
let noticeTimer = 0;
const items = new Map();

class NoAnswer extends Error {}

class ErrorAnswer extends Error {
  constructor(message, answer, status) {
    super(message);
    this.answer = answer;
    this.status = status;
  }
}

function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else if (key === "on") for (const [name, handler] of Object.entries(value)) node.addEventListener(name, handler);
    else node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...kids.flat(Infinity).filter((kid) => kid !== null && kid !== undefined && kid !== false));
  return node;
}

function withPeriod(text) {
  return /[.?!]$/.test(text) ? text : `${text}.`;
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function clock(date) {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function money(n) {
  const value = Number(n || 0);
  return value > 0 && value < 0.005 ? "<$0.01" : `$${value.toFixed(2)}`;
}

function link(href, text) {
  return /^https:\/\//.test(href || "") ? el("a", { href, rel: "noreferrer", target: "_blank", text }) : document.createTextNode(text);
}

function chip(state) {
  return CHIP_WORDS[state] ? el("span", { class: `fb-chip ${state}`, text: CHIP_WORDS[state] }) : null;
}

function sayIn(target, state, sentence) {
  const mark = chip(state);
  target.replaceChildren(...[mark, mark ? " " : null, sentence].filter((part) => part !== null));
  target.hidden = false;
}

async function call(path, body, signal) {
  const changes = body !== undefined;
  const raw = body instanceof Blob;
  const options = changes ? { method: "POST", headers: { "Content-Type": raw ? "application/octet-stream" : "application/json", "X-Arete": "1" }, body: raw ? body : JSON.stringify(body) }
    : { cache: "no-store", signal };
  const response = await fetch(path, options).catch(() => { throw new NoAnswer("The connection to arete failed."); });
  const data = await response.json().catch(() => ({ error: `The server answered ${response.status} with no JSON.` }));
  if (!response.ok) throw new ErrorAnswer(data.error || `The server answered ${response.status}.`, data, response.status);
  return data;
}

function announce(sentence) {
  notice.value = sentence;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { notice.value = ""; }, NOTICE_MS);
}

function inTurn(work) {
  const turn = line.then(work);
  line = turn.catch(() => {});
  return turn;
}

function box(name) {
  return refs[name];
}

function fields() {
  return {
    kind: dialog.querySelector("input[name=ui-feedback-kind]:checked")?.value ?? "idea",
    ...Object.fromEntries(TEXTS.map((name) => [name, box(name).value])),
  };
}

function putFeedback(values) {
  const kind = values.kind || "idea";
  for (const radio of dialog.querySelectorAll("input[name=ui-feedback-kind]")) radio.checked = radio.value === kind;
  for (const name of TEXTS) {
    const value = values[name] ?? "";
    if (box(name).value !== value) box(name).value = value;
  }
  box("bug").hidden = kind !== "bug";
}

function keepHere(unsaved) {
  try {
    localStorage.setItem(KEPT, JSON.stringify({ ...fields(), unsaved }));
  } catch {
    // without storage the draft is kept on arete only
  }
}

function keptHere() {
  try {
    return JSON.parse(localStorage.getItem(KEPT));
  } catch {
    return null;
  }
}

function forgetHere() {
  try {
    localStorage.removeItem(KEPT);
  } catch {
    // nothing was kept
  }
}

function sayFeedback(state, sentence) {
  sayIn(box("result"), state, sentence);
}

function failedFeedback(e) {
  return e instanceof ErrorAnswer ? withPeriod(e.message) : "arete did not answer.";
}

function drawFiled() {
  const target = box("filed");
  target.hidden = !filed;
  if (filed) target.replaceChildren(`Filed capture ${filed.id} as `, link(filed.url, `#${filed.number} ${filed.title}`));
}

function forgetFiled() {
  filed = null;
  drawFiled();
}

function filedNotice(found) {
  return `Submitted capture ${found.id}${found.number ? `, filed as issue #${found.number}` : ""}.`;
}

function takeAnswer(answered, reading = false) {
  const held = draft?.id;
  const found = answered.filed;
  const sawFiling = Boolean(found) && held !== undefined && found.id === held;
  if (found && (!answered.draft || sawFiling)) filed = found;
  if (sawFiling || reading) mergeFeedback(answered.draft);
  else drawDraft(answered.draft);
  drawFiled();
  if (sawFiling) {
    announce(filedNotice(found));
    box("filed").focus();
  }
}

function onInput() {
  forgetFiled();
  dirty = true;
  keepHere(true);
  box("bug").hidden = fields().kind !== "bug";
  drawText();
  clearTimeout(timer);
  timer = 0;
  saveLater(SAVE_MS);
}

function saveLater(ms) {
  if (!timer) timer = setTimeout(saveFeedback, ms);
}

function answer() {
  const stop = new AbortController();
  let wait = 0;
  const late = new Promise((_, refuse) => {
    wait = setTimeout(() => {
      stop.abort();
      refuse(new NoAnswer("arete did not answer in time."));
    }, READ_MS);
  });
  return Promise.race([call("/api/capture", undefined, stop.signal), late]).finally(() => clearTimeout(wait));
}

function mergedText(typed, held, current) {
  if (typed === held) return current;
  if (current === held || !current || typed === current) return typed;
  return typed ? `${current}\n\n${typed}` : current;
}

function mergeFeedback(next) {
  const held = Object.fromEntries(Object.keys(EMPTY).map((name) => [name, next?.[name] || EMPTY[name]]));
  const typed = fields();
  const merged = { kind: typed.kind === base.kind ? held.kind : typed.kind };
  for (const name of TEXTS) merged[name] = mergedText(typed[name], base[name], held[name]);
  putFeedback(merged);
  base = held;
  read = true;
  dirty = JSON.stringify(merged) !== JSON.stringify(held);
  keepHere(dirty);
  drawDraft(next);
}

async function readDraft() {
  try {
    const answered = await answer();
    noteLimits(answered.limits);
    for (const name of TEXTS) {
      box(name).maxLength = answered.limits[name];
      box(`${name}-limit`).textContent = `${name === "text" ? "Required" : "Optional"} · up to ${answered.limits[name].toLocaleString("en-US")} characters`;
    }
    if (discarding) return;
    takeAnswer(answered, true);
    box("result").hidden = true;
  } catch (e) {
    sayFeedback("caution", `${failedFeedback(e)} ${dirty ? ON_DEVICE : "This shows the copy kept on this device."}`);
  }
}

function readFeedback() {
  return inTurn(async () => {
    await readDraft();
    if (dirty) saveLater(read ? SAVE_MS : RETRY_MS);
  });
}

async function sendFields() {
  if (!dirty || discarding) return;
  if (!read) {
    await readDraft();
    if (!read) return saveLater(RETRY_MS);
    if (!dirty) return;
  }
  const sent = fields();
  let saved = false;
  saving = true;
  try {
    const answered = await call("/api/capture", sent);
    saved = true;
    base = sent;
    if (JSON.stringify(fields()) === JSON.stringify(sent)) {
      dirty = false;
      keepHere(false);
    }
    drawDraft(answered.draft);
    sayFeedback("ok", `Draft saved at ${clock(new Date())}. It is kept, and nothing is filed.`);
  } catch (e) {
    sayFeedback("caution", `${failedFeedback(e)} ${ON_DEVICE}`);
  } finally {
    saving = false;
    if (dirty) saveLater(saved ? SAVE_MS : RETRY_MS);
  }
}

function saveFeedback() {
  clearTimeout(timer);
  timer = 0;
  saveWaits ||= inTurn(() => {
    saveWaits = null;
    return sendFields();
  });
  return saveWaits;
}

function drawContext(next) {
  const json = next?.context || "";
  box("include").checked = Boolean(json);
  box("context").hidden = !json;
  box("context-json").textContent = json;
}

function drawDraft(next) {
  const held = draft;
  if (next && held && next.id === held.id && next.revision < held.revision) return drawList();
  draft = next;
  drawContext(next);
  drawText();
  drawChecks(next);
  drawList();
}

function noteLimits(found) {
  if (found) limits = { ...limits, ...found };
  drawLimitsHelp();
}

function drawLimitsHelp() {
  box("limits-help").textContent = `PNG, JPEG or WebP images up to ${limits.image_bytes / MB} MB and WebM or MP4 videos up to ${limits.video_bytes / MB} MB and ${limits.video_seconds} seconds. `
    + `A draft holds ${limits.evidence_items} items and ${limits.evidence_bytes / MB} MB. Nothing is captured until you press a button, and staged files stay on this machine until Submit files the request.`;
}

function sizeOf(bytes) {
  return bytes >= MB ? `${(bytes / MB).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function sayEvidence(state, sentence) {
  sayIn(box("evidence-said"), state, sentence);
}

function drawText() {
  const sending = draft?.outgoing;
  const written = Boolean(draft?.text?.trim());
  box("sending-title").textContent = written ? sending.title : "";
  box("sending-text").textContent = written ? sending.body : "Nothing written yet.";
  box("sending-source").textContent = !written ? "" : sending.source === "writer" ? "Written by the writer from your words." : sending.note;
}

function checkRow(check) {
  const words = el("div", { class: "fb-words" }, el("strong", { text: check.title }), " ", el("span", { text: check.reason || "Not run yet." }));
  if (check.stale) words.append(" ", el("em", { text: "Stale, Submit runs it again." }));
  if (check.detail.version) words.append(el("br"), el("span", { class: "muted", text: `Vision: ${check.detail.path}, version ${check.detail.version}` }));
  for (const match of check.detail.matches ?? []) {
    words.append(el("br"), link(match.url, `#${match.number} ${match.title}`), el("span", { class: "muted", text: ` · ${match.state} · chance ${match.chance.toFixed(2)}` }));
  }
  return el("li", {}, chip(CHIPS[check.result]) ?? el("span", { class: "fb-chip", text: check.result === "running" ? "Running" : "Not run" }), " ", words);
}

function drawChecks(next) {
  const checks = next?.checks ?? [];
  const shown = JSON.stringify([checks, next?.summary, next?.jev_cost_usd, next?.inspect_cost_usd, next?.vision, next?.delivery, next?.status, next?.working]);
  const checking = next?.status === "checking";
  const inFlight = checking || (next?.status === "filing" && next.working);
  const submit = box("submit");
  submit.disabled = inFlight;
  submit.textContent = checking ? "Checking…" : inFlight ? "Filing…" : "Submit";
  if (inFlight) pollChecks();
  if (shown === checksShown) return;
  checksShown = shown;
  box("checks").replaceChildren(...checks.map(checkRow));
  box("checks-cost").textContent = [
    next?.jev_cost_usd > 0 ? `JEV cost so far: ${money(next.jev_cost_usd)}` : "",
    next?.inspect_cost_usd > 0 ? `Evidence inspection cost so far: ${money(next.inspect_cost_usd)}` : "",
  ].filter(Boolean).join(" · ");
  box("ready").textContent = next && checks.some((c) => c.result !== "none") ? next.summary : "";
  if (next && !next.vision.version && checks.some((c) => c.result !== "none")) box("ready").append(" ", next.vision.setting);
}

function pollChecks() {
  if (polling) return;
  polling = setTimeout(async function again() {
    polling = 0;
    try {
      takeAnswer(await call("/api/capture"));
    } catch {
      polling = setTimeout(again, RETRY_MS);
    }
  }, 1000);
}

function submitFeedback() {
  return inTurn(async () => {
    await uploads;
    await sendFields();
    if (dirty || pending.length || !draft) {
      return sayFeedback("caution", draft ? "The draft is not saved yet, or a file is still sending. Submit again once it is." : "Write the request before you submit it.");
    }
    try {
      takeAnswer(await call("/api/capture/submit", { id: draft.id, revision: draft.revision }));
      box("result").hidden = true;
    } catch (e) {
      if (e instanceof ErrorAnswer && e.status === 409) {
        await readDraft();
        sayFeedback("caution", "The draft changed, so it was read again. Press Submit to check it.");
      } else {
        sayFeedback("caution", `The request was not submitted: ${failedFeedback(e)}`);
      }
    }
  });
}

function evidenceDetails(row, seen) {
  const seconds = row.seconds ?? (Number.isFinite(seen) ? seen : null);
  return [TYPES[row.type] || row.type, sizeOf(row.bytes), seconds === null ? null : `${Math.round(seconds)} s`, SOURCES[row.source] || row.source].filter(Boolean).join(" · ");
}

function checkEvidenceLength(row, video, details) {
  const seconds = video.duration;
  if (!Number.isFinite(seconds)) return;
  const most = limits.video_seconds;
  if (seconds > most + LENGTH_SLACK) removeEvidence(row, `${row.name} is ${Math.round(seconds)} s long and the limit is ${most} s, so it was removed.`);
  else details.textContent = evidenceDetails(row, seconds);
}

function stagedItem(row) {
  const src = `/api/capture/evidence/${row.id}`;
  const details = el("span", { text: evidenceDetails(row) });
  const media = row.kind === "image" ? el("img", { src, alt: row.name }) : el("video", { controls: true, preload: "metadata", src });
  if (row.kind === "video") media.addEventListener("loadedmetadata", () => checkEvidenceLength(row, media, details));
  const remove = el("button", { type: "button", class: "fb-remove", text: "Remove", on: { click: () => removeEvidence(row, `Removed ${row.name}.`, remove) } });
  return el("li", {}, media, el("div", { class: "fb-words" }, el("strong", { text: row.name }), el("br"), details, el("br"),
    el("span", { class: "fb-inspection" }), el("span", { class: "fb-uploaded" })), remove);
}

function inspectionLine(found) {
  if (!found) return "";
  if (!found.inspected) return `Not inspected: ${found.why}`;
  const flags = found.flags.map((f) => FLAGS[f] || f).join(", ");
  return `Inspected by ${found.model}: ${flags ? `shows ${flags}` : "nothing sensitive found"}. Saw: ${found.saw}`;
}

function pendingItem(item) {
  const failed = item.state === "failed";
  const again = el("button", { type: "button", text: "Try again", on: { click: () => uploadItem(item) } });
  const remove = el("button", { type: "button", class: "fb-remove", text: "Remove", on: { click: () => {
    pending = pending.filter((p) => p !== item);
    drawList();
    box("attach").focus();
  } } });
  return el("li", {}, el("div", { class: "fb-words" }, el("strong", { text: item.name }), el("br"),
    el("span", { text: `${sizeOf(item.blob.size)} · ${failed ? `Not staged: ${item.why}` : "Sending…"}` })), failed ? [again, remove] : null);
}

function drawList() {
  const staged = draft?.evidence ?? [];
  const json = draft?.context || "";
  box("sending-context").textContent = json ? `App state: Included, ${new TextEncoder().encode(json).length} bytes, shown above.` : "App state: Not included.";
  const ids = new Set(staged.map((row) => row.id));
  for (const id of [...items.keys()]) if (!ids.has(id)) items.delete(id);
  for (const row of staged) if (!items.has(row.id)) items.set(row.id, stagedItem(row));
  for (const row of staged) {
    const item = items.get(row.id);
    item.querySelector(".fb-inspection").textContent = inspectionLine(row.inspection);
    item.querySelector(".fb-uploaded").replaceChildren(...(row.link ? [" ", link(row.link, "Uploaded")] : []));
  }
  const wanted = [...staged.map((row) => items.get(row.id)), ...pending.map(pendingItem)];
  const list = box("evidence");
  wanted.forEach((node, i) => {
    if (list.children[i] !== node) list.insertBefore(node, list.children[i] ?? null);
  });
  while (list.children.length > wanted.length) list.lastElementChild.remove();
  const bytes = staged.reduce((sum, row) => sum + row.bytes, 0);
  box("evidence-total").textContent = `${staged.length} of ${limits.evidence_items} items · ${(bytes / MB).toFixed(1)} of ${limits.evidence_bytes / MB} MB`;
  lockFeedback(draft?.status === "filing");
}

function lockFeedback(locked) {
  for (const radio of dialog.querySelectorAll("input[name=ui-feedback-kind]")) radio.disabled = locked;
  for (const name of TEXTS) box(name).readOnly = locked;
  for (const id of LOCKED) box(id).disabled = locked;
  for (const remove of box("evidence").querySelectorAll("button")) remove.disabled = locked;
}

function onUploads(work) {
  const turn = uploads.then(work);
  uploads = turn.catch(() => {});
  return turn;
}

function queueFiles(files) {
  const added = files.map((file) => ({ key: ++keys, state: "sending", why: "", seconds: null, ...file }));
  pending.push(...added);
  drawList();
  for (const item of added) uploadItem(item);
}

function uploadItem(item) {
  return onUploads(async () => {
    if (!pending.includes(item)) return;
    item.state = "sending";
    drawList();
    const path = `/api/capture/evidence?name=${encodeURIComponent(item.name)}&source=${item.source}&seconds=${item.seconds ?? ""}`;
    try {
      const answered = await call(path, item.blob);
      noteLimits(answered.limits);
      pending = pending.filter((p) => p !== item);
      drawDraft(answered.draft);
      sayEvidence("ok", answered.existing ? "That file is already staged." : `Staged ${answered.evidence.name}.`);
    } catch (e) {
      item.state = "failed";
      item.why = failedFeedback(e);
      drawList();
      sayEvidence("caution", `${item.name} was not staged: ${failedFeedback(e)}`);
    }
  });
}

function removeEvidence(row, said, from) {
  const buttons = () => [...box("evidence").querySelectorAll("button.fb-remove")];
  const at = from ? buttons().indexOf(from) : -1;
  return onUploads(async () => {
    try {
      drawDraft((await call(`/api/capture/evidence/${row.id}/drop`, {})).draft);
      sayEvidence("ok", said);
    } catch (e) {
      sayEvidence("caution", `${row.name} was not removed: ${failedFeedback(e)}`);
      return;
    }
    if (!from) return;
    const left = buttons();
    (left[Math.min(at, left.length - 1)] ?? box("attach")).focus();
  });
}

function refusedFile(file, count, bytes) {
  if (!TYPES[file.type]) return `${file.name} is not a PNG, JPEG or WebP image or a WebM or MP4 video, so it was not added.`;
  const image = file.type.startsWith("image/");
  const most = image ? limits.image_bytes : limits.video_bytes;
  if (file.size > most) return `${file.name} is ${sizeOf(file.size)} and ${image ? "an image" : "a video"} is limited to ${most / MB} MB, so it was not added.`;
  if (count >= limits.evidence_items) return `A draft holds at most ${limits.evidence_items} items, so ${file.name} was not added. Remove one first.`;
  if (bytes + file.size > limits.evidence_bytes) return `A draft holds at most ${limits.evidence_bytes / MB} MB of evidence, so ${file.name} was not added.`;
  return "";
}

function attachFiles(input) {
  const files = [...input.files];
  input.value = "";
  const staged = draft?.evidence ?? [];
  let count = staged.length + pending.length;
  let bytes = staged.reduce((sum, row) => sum + row.bytes, 0) + pending.reduce((sum, item) => sum + item.blob.size, 0);
  const accepted = [];
  const refused = [];
  for (const file of files) {
    const why = refusedFile(file, count, bytes);
    if (why) {
      refused.push(why);
      continue;
    }
    count++;
    bytes += file.size;
    accepted.push({ blob: file, name: file.name, source: "attached" });
  }
  if (refused.length) sayEvidence("caution", refused.join(" "));
  if (accepted.length) queueFiles(accepted);
}

function stamp() {
  const now = new Date();
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}

function stepAside() {
  aside = true;
  dialog.close();
}

function returnToFeedback(focus) {
  aside = false;
  if (!dialog.open) dialog.showModal();
  (focus ?? (matchMedia("(pointer: coarse)").matches ? box("title") : box("text"))).focus();
}

function stopTracks(stream) {
  for (const track of stream?.getTracks() ?? []) track.stop();
}

function pausedFor(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function nextFrames() {
  return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}

function canvasPng(canvas) {
  return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
}

async function framePng(stream) {
  const video = document.createElement("video");
  video.muted = true;
  video.srcObject = stream;
  await video.play();
  await nextFrames();
  await pausedFor(250);
  const full = document.createElement("canvas");
  full.width = video.videoWidth;
  full.height = video.videoHeight;
  full.getContext("2d").drawImage(video, 0, 0);
  stopTracks(stream);
  video.srcObject = null;
  for (const scale of [1, 0.75, 0.5]) {
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(full.width * scale));
    canvas.height = Math.max(1, Math.round(full.height * scale));
    canvas.getContext("2d").drawImage(full, 0, 0, canvas.width, canvas.height);
    const blob = await canvasPng(canvas);
    if (blob && blob.size <= limits.image_bytes) return { blob, name: `screenshot-${stamp()}.png`, source: "screenshot" };
  }
  throw new Error(`The screenshot is over ${limits.image_bytes / MB} MB even scaled down, so it was not staged`);
}

function recordedClock(seconds) {
  return `${Math.floor(seconds / 60)}:${pad(Math.floor(seconds % 60))}`;
}

async function recordStream(stream) {
  const most = limits.video_seconds;
  const mimeType = RECORDER_TYPES.find((type) => MediaRecorder.isTypeSupported(type));
  const recorder = new MediaRecorder(stream, { ...(mimeType ? { mimeType } : {}), videoBitsPerSecond: 4_000_000 });
  const chunks = [];
  recorder.addEventListener("dataavailable", (event) => {
    if (event.data.size) chunks.push(event.data);
  });
  const stopped = new Promise((resolve) => recorder.addEventListener("stop", resolve, { once: true }));
  const halt = () => {
    if (recorder.state !== "inactive") recorder.stop();
  };
  const bar = box("recording");
  const started = performance.now();
  const elapsed = () => Math.min(most, (performance.now() - started) / 1000);
  const tick = () => {
    box("recording-time").textContent = `Recording ${recordedClock(elapsed())} of ${recordedClock(most)}`;
  };
  stepAside();
  tick();
  bar.hidden = false;
  box("recording-stop").focus();
  stopRecording = halt;
  recorder.addEventListener("error", halt);
  stream.getVideoTracks()[0]?.addEventListener("ended", halt);
  recorder.start(1000);
  const ticker = setInterval(tick, 250);
  const limit = setTimeout(halt, most * 1000);
  try {
    await stopped;
  } finally {
    clearInterval(ticker);
    clearTimeout(limit);
    stopRecording = null;
    bar.hidden = true;
  }
  stopTracks(stream);
  const type = recorder.mimeType || mimeType || "video/webm";
  if (!chunks.length) throw new Error("The recording was empty");
  return { blob: new Blob(chunks, { type }), name: `recording-${stamp()}.${type.includes("mp4") ? "mp4" : "webm"}`, source: "recording", seconds: Math.round(elapsed() * 10) / 10 };
}

async function captureScreen(recording) {
  if (capturing) return;
  if (!navigator.mediaDevices?.getDisplayMedia || (recording && !window.MediaRecorder)) return sayEvidence("caution", NO_CAPTURE);
  capturing = true;
  const asked = navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
  let stream = null;
  let focus = null;
  try {
    stream = await asked;
    if (!recording) stepAside();
    queueFiles([await (recording ? recordStream(stream) : framePng(stream))]);
  } catch (e) {
    focus = box("attach");
    sayEvidence("caution", e.name === "NotAllowedError" ? "Nothing was captured. You can attach a file instead."
      : `The screen could not be captured (${e.name === "Error" ? e.message : e.name}). Nothing was captured, and you can attach a file instead.`);
  } finally {
    stopTracks(stream);
    capturing = false;
    if (aside) returnToFeedback(focus);
    else focus?.focus();
  }
}

async function captureContext() {
  clearTimeout(timer);
  timer = 0;
  await inTurn(async () => {
    await sendFields();
    try {
      const answered = await call("/api/capture/context", { screen: location.hash || "#floor" });
      drawDraft(answered.draft);
      sayFeedback("ok", `Captured the app state at ${clock(new Date())}.`);
    } catch (e) {
      if (box("context").hidden) box("include").checked = false;
      sayFeedback("caution", `The app state was not added: ${failedFeedback(e)}`);
    }
  });
}

async function dropContext() {
  await inTurn(async () => {
    try {
      drawDraft((await call("/api/capture/context/drop", {})).draft);
      sayFeedback("ok", "Removed the app state.");
    } catch (e) {
      box("include").checked = !box("context").hidden;
      sayFeedback("caution", `The app state was not removed: ${failedFeedback(e)}`);
    }
  });
  box("include").focus();
}

async function discardFeedback() {
  if (discarding) return;
  discarding = true;
  forgetFiled();
  clearTimeout(timer);
  timer = 0;
  const clicked = fields();
  pending = [];
  drawList();
  await uploads;
  await inTurn(async () => {
    let failed = null;
    try {
      await call("/api/capture/discard", {});
    } catch (e) {
      failed = e;
    }
    discarding = false;
    if (failed) {
      sayFeedback("caution", `The draft was not discarded: ${failedFeedback(failed)} It is still kept.`);
      if (dirty) saveLater(RETRY_MS);
      return;
    }
    const typed = fields();
    const since = Object.fromEntries(Object.keys(EMPTY).map((name) => [name, typed[name] === clicked[name] ? EMPTY[name] : typed[name]]));
    putFeedback(since);
    base = { ...EMPTY };
    read = true;
    drawDraft(null);
    dirty = JSON.stringify(since) !== JSON.stringify(EMPTY);
    if (!dirty) {
      forgetHere();
      sayFeedback("ok", "Discarded the draft.");
      return;
    }
    keepHere(true);
    saveLater(SAVE_MS);
    sayFeedback("ok", "Discarded the draft. What you typed since is kept on this device and saves as a new draft.");
  });
}

export function openFeedback() {
  if (!dialog || dialog.open || aside) return;
  returnTo = document.activeElement;
  dialog.showModal();
  (matchMedia("(pointer: coarse)").matches ? box("title") : box("text")).focus();
  if (!dirty) readFeedback();
}

function trapFocus(event) {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    submitFeedback();
    return;
  }
  if (event.key !== "Tab") return;
  const found = [...dialog.querySelectorAll(FOCUSABLE)].filter((node) => !node.hidden && !node.closest("[hidden]") && node.getClientRects().length > 0);
  if (!found.length) return;
  const first = found[0];
  const last = found[found.length - 1];
  if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function field(name, rows, required) {
  const area = el("textarea", { id: `ui-feedback-${name}`, rows, "aria-required": required ? "true" : null, "aria-describedby": `ui-feedback-${name}-limit`, on: { input: onInput } });
  const limit = el("p", { class: "muted fb-limit", id: `ui-feedback-${name}-limit`, text: `${required ? "Required" : "Optional"} · up to ${required ? "4,000" : "2,000"} characters` });
  refs[name] = area;
  refs[`${name}-limit`] = limit;
  return el("div", { class: "fb-field" }, el("label", { for: `ui-feedback-${name}`, text: LABELS[name] }), area, limit);
}

function named(name, node) {
  refs[name] = node;
  return node;
}

function buildDialog() {
  const kinds = el("fieldset", { class: "fb-kinds" }, el("legend", { text: "Kind" }), KINDS.map(([value, label]) => el("label", { class: "fb-check" },
    el("input", { type: "radio", name: "ui-feedback-kind", value, checked: value === "idea", on: { change: onInput } }), label)));
  const click = (name, handler) => el("button", { type: "button", id: `ui-feedback-${name}`, on: { click: handler } });
  const button = (name, text, handler) => named(name, Object.assign(click(name, handler), { textContent: text }));
  const include = named("include", el("input", { type: "checkbox", id: "ui-feedback-include", on: { change: (event) => (event.currentTarget.checked ? captureContext() : dropContext()) } }));
  const file = named("file", el("input", { type: "file", accept: "image/png,image/jpeg,image/webp,video/webm,video/mp4", multiple: true, hidden: true, on: { change: (event) => attachFiles(event.currentTarget) } }));
  dialog = el("dialog", { class: "ui-feedback", "aria-labelledby": "ui-feedback-title", on: { keydown: trapFocus, close: onClose } },
    named("title", el("h2", { id: "ui-feedback-title", tabindex: "-1", text: "Send feedback" })),
    el("p", { class: "muted", text: "The draft is kept as you type, on arete and on this device. Submit files it as a capture." }),
    kinds, field("text", 5, true),
    named("bug", el("div", { class: "fb-bug", hidden: true }, field("expected", 3, false), field("actual", 3, false), field("steps", 3, false))),
    el("label", { class: "fb-check" }, include, "Include app state"),
    el("p", { class: "muted fb-limit", text: "arete adds its version, the screen you are on and the open item's id. It never adds page content, config, logs or transcripts. Up to 8 KB." }),
    named("context", el("div", { class: "fb-context", hidden: true }, named("context-json", el("pre", { tabindex: "0" })),
      el("div", { class: "fb-actions" }, button("context-again", "Capture again", captureContext), button("context-drop", "Remove", dropContext)))),
    el("div", { class: "fb-actions" }, button("shot", "Take a screenshot", () => captureScreen(false)), button("record", "Record a video", () => captureScreen(true)),
      button("attach", "Attach a file", () => file.click()), file),
    named("limits-help", el("p", { class: "muted fb-limit" })),
    named("evidence-said", el("p", { "aria-live": "polite", tabindex: "-1", hidden: true })),
    el("h3", { text: "What will be sent" }),
    el("div", { class: "fb-sending" }, el("p", {}, named("sending-title", el("strong"))), named("sending-text", el("pre")),
      named("sending-source", el("p", { class: "muted" })), named("sending-context", el("p")), named("evidence", el("ul", { class: "fb-evidence" })),
      named("evidence-total", el("p", { class: "muted" }))),
    el("h3", { text: "Checks" }), named("checks", el("ul", { class: "fb-checks" })), named("checks-cost", el("p", { class: "muted" })),
    named("ready", el("p", { "aria-live": "polite" })), named("result", el("p", { "aria-live": "polite", tabindex: "-1", hidden: true })),
    named("filed", el("p", { "aria-live": "polite", tabindex: "-1", hidden: true })),
    el("div", { class: "fb-actions" }, button("submit", "Submit", submitFeedback), button("close", "Close", () => dialog.close()), button("discard", "Discard", discardFeedback),
      el("span", { class: "muted", text: "Esc closes · Cmd Enter submits · the draft is kept" })));
}

function onClose() {
  if (dirty) saveFeedback();
  if (aside) return;
  mode.value = "NORMAL";
  if (returnTo?.isConnected) returnTo.focus();
  returnTo = null;
}

function buildRecording() {
  return el("div", { class: "ui-feedback-recording", role: "status", hidden: true },
    named("recording-time", el("span", { text: "Recording 0:00 of 0:30" })),
    named("recording-stop", el("button", { type: "button", text: "Stop", on: { click: () => stopRecording?.() } })));
}

export function startFeedback() {
  refs = {};
  buildDialog();
  const bar = named("recording", buildRecording());
  document.body.append(dialog, bar);
  drawLimitsHelp();
  const kept = keptHere();
  if (kept) putFeedback(kept);
  if (kept?.unsaved) {
    dirty = true;
    read = true;
    saveFeedback();
  } else {
    base = fields();
    readFeedback();
  }
}
