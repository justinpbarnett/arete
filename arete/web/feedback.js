"use strict";

const FEEDBACK_KEPT = "arete.feedback";
const FEEDBACK_SAVE_MS = 600;
const FEEDBACK_RETRY_MS = 5000;
const FEEDBACK_TEXTS = ["text", "expected", "actual", "steps"];
const FEEDBACK_EMPTY = { kind: "idea", text: "", expected: "", actual: "", steps: "" };
let feedbackReturn = null;
let feedbackTimer = 0;
let feedbackDirty = false;
let feedbackSaving = false;
const FEEDBACK_READ_MS = 8000;
const FEEDBACK_ON_DEVICE = "The draft is kept on this device, and it saves again when the server answers.";
let feedbackRead = false;
let feedbackDiscarding = false;
let feedbackSaveWaits = null;
let feedbackBase = { ...FEEDBACK_EMPTY };
let feedbackLine = Promise.resolve();
const FEEDBACK_MB = 1024 * 1024;
const FEEDBACK_TYPES = { "image/png": "PNG image", "image/jpeg": "JPEG image", "image/webp": "WebP image", "video/webm": "WebM video", "video/mp4": "MP4 video" };
const FEEDBACK_SOURCES = { screenshot: "Screenshot", recording: "Recording", attached: "Attached" };
const FEEDBACK_RECORDER_TYPES = ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm", "video/mp4"];
const FEEDBACK_NO_CAPTURE = "This browser cannot capture the screen here. Take a screenshot with the device and use Attach a file.";
const FEEDBACK_LABELS = { text: "Request", expected: "Expected", actual: "Actual", steps: "Steps" };
const FEEDBACK_LENGTH_SLACK = 0.5;
const FEEDBACK_FLAGS = { credential: "a credential", personal: "private personal information", private_address: "a private address", agent_text: "text addressed to an agent" };
let feedbackLimits = { image_bytes: 5 * FEEDBACK_MB, video_bytes: 25 * FEEDBACK_MB, evidence_items: 6, evidence_bytes: 40 * FEEDBACK_MB, video_seconds: 30 };
let feedbackDraft = null;
let feedbackFiled = null;
let feedbackAside = false;
let feedbackCapturing = false;
let feedbackStopRecording = null;
let feedbackPending = [];
let feedbackUploads = Promise.resolve();
let feedbackKeys = 0;
const feedbackItems = new Map();

function inFeedbackTurn(work) {
  const turn = feedbackLine.then(work);
  feedbackLine = turn.catch(() => {});
  return turn;
}

function feedbackBox(name) {
  return document.getElementById(`feedback-${name}`);
}

function feedbackFields() {
  return {
    kind: document.querySelector("input[name=feedback-kind]:checked")?.value ?? "idea",
    ...Object.fromEntries(FEEDBACK_TEXTS.map((name) => [name, feedbackBox(name).value])),
  };
}

function putFeedback(fields) {
  const kind = fields.kind || "idea";
  for (const radio of document.querySelectorAll("input[name=feedback-kind]")) radio.checked = radio.value === kind;
  for (const name of FEEDBACK_TEXTS) {
    const value = fields[name] ?? "";
    if (feedbackBox(name).value !== value) feedbackBox(name).value = value;
  }
  feedbackBox("bug").hidden = kind !== "bug";
}

function keepFeedbackHere(unsaved) {
  try {
    localStorage.setItem(FEEDBACK_KEPT, JSON.stringify({ ...feedbackFields(), unsaved }));
  } catch {
    // without storage the draft is kept on arete only
  }
}

function keptFeedback() {
  try {
    return JSON.parse(localStorage.getItem(FEEDBACK_KEPT));
  } catch {
    return null;
  }
}

function forgetFeedbackHere() {
  try {
    localStorage.removeItem(FEEDBACK_KEPT);
  } catch {
    // nothing was kept
  }
}

function feedbackUnsaved() {
  return feedbackDirty || feedbackSaving || feedbackAside || feedbackCapturing || feedbackPending.length > 0;
}

function sayFeedback(state, sentence) {
  sayIn(feedbackBox("result"), state, sentence);
}

function failedFeedback(e) {
  return e instanceof ErrorAnswer ? withPeriod(e.message) : "arete did not answer.";
}

function drawFeedbackFiled() {
  const line = feedbackBox("filed");
  line.hidden = !feedbackFiled;
  if (feedbackFiled) line.replaceChildren("Filed ", link(feedbackFiled.url, `#${feedbackFiled.number} ${feedbackFiled.title}`));
}

function forgetFeedbackFiled() {
  feedbackFiled = null;
  drawFeedbackFiled();
}

function takeFeedbackAnswer(answered, reading = false) {
  const held = feedbackDraft?.id;
  const filed = answered.filed;
  const sawFiling = Boolean(filed) && held !== undefined && filed.id === held;
  if (filed && (!answered.draft || sawFiling)) feedbackFiled = filed;
  if (sawFiling || reading) mergeFeedback(answered.draft);
  else drawFeedbackDraft(answered.draft);
  drawFeedbackFiled();
  if (sawFiling) feedbackBox("filed").focus();
}

function onFeedbackInput() {
  forgetFeedbackFiled();
  feedbackDirty = true;
  keepFeedbackHere(true);
  feedbackBox("bug").hidden = feedbackFields().kind !== "bug";
  drawFeedbackText();
  clearTimeout(feedbackTimer);
  feedbackTimer = 0;
  saveFeedbackLater(FEEDBACK_SAVE_MS);
}

function saveFeedbackLater(ms) {
  if (!feedbackTimer) feedbackTimer = setTimeout(saveFeedback, ms);
}

function feedbackAnswer() {
  const stop = new AbortController();
  let timer = 0;
  const late = new Promise((_, refuse) => {
    timer = setTimeout(() => {
      stop.abort();
      refuse(new NoAnswer("arete did not answer in time."));
    }, FEEDBACK_READ_MS);
  });
  return Promise.race([call("/api/capture", undefined, stop.signal), late]).finally(() => clearTimeout(timer));
}

function mergedFeedbackText(typed, base, held) {
  if (typed === base) return held;
  if (held === base || !held || typed === held) return typed;
  return typed ? `${held}\n\n${typed}` : held;
}

function mergeFeedback(draft) {
  const held = Object.fromEntries(Object.keys(FEEDBACK_EMPTY).map((name) => [name, draft?.[name] || FEEDBACK_EMPTY[name]]));
  const typed = feedbackFields();
  const merged = { kind: typed.kind === feedbackBase.kind ? held.kind : typed.kind };
  for (const name of FEEDBACK_TEXTS) merged[name] = mergedFeedbackText(typed[name], feedbackBase[name], held[name]);
  putFeedback(merged);
  feedbackBase = held;
  feedbackRead = true;
  feedbackDirty = JSON.stringify(merged) !== JSON.stringify(held);
  keepFeedbackHere(feedbackDirty);
  drawFeedbackDraft(draft);
}

async function readFeedbackDraft() {
  try {
    const answered = await feedbackAnswer();
    noteFeedbackLimits(answered.limits);
    for (const name of FEEDBACK_TEXTS) {
      feedbackBox(name).maxLength = answered.limits[name];
      const line = feedbackBox(`${name}-limit`);
      line.textContent = `${name === "text" ? "Required" : "Optional"} · up to ${answered.limits[name].toLocaleString("en-US")} characters`;
    }
    if (feedbackDiscarding) return;
    takeFeedbackAnswer(answered, true);
    feedbackBox("result").hidden = true;
  } catch (e) {
    sayFeedback("caution", `${failedFeedback(e)} ${feedbackDirty ? FEEDBACK_ON_DEVICE : "This shows the copy kept on this device."}`);
  }
}

function readFeedback() {
  return inFeedbackTurn(async () => {
    await readFeedbackDraft();
    if (feedbackDirty) saveFeedbackLater(feedbackRead ? FEEDBACK_SAVE_MS : FEEDBACK_RETRY_MS);
  });
}

async function sendFeedbackFields() {
  if (!feedbackDirty || feedbackDiscarding) return;
  if (!feedbackRead) {
    await readFeedbackDraft();
    if (!feedbackRead) return saveFeedbackLater(FEEDBACK_RETRY_MS);
    if (!feedbackDirty) return;
  }
  const sent = feedbackFields();
  let saved = false;
  feedbackSaving = true;
  try {
    const answered = await call("/api/capture", sent);
    saved = true;
    feedbackBase = sent;
    if (JSON.stringify(feedbackFields()) === JSON.stringify(sent)) {
      feedbackDirty = false;
      keepFeedbackHere(false);
    }
    drawFeedbackDraft(answered.draft);
    sayFeedback("ok", `Draft saved at ${clock(new Date())}. It is kept, and nothing is filed.`);
  } catch (e) {
    sayFeedback("caution", `${failedFeedback(e)} ${FEEDBACK_ON_DEVICE}`);
  } finally {
    feedbackSaving = false;
    if (feedbackDirty) saveFeedbackLater(saved ? FEEDBACK_SAVE_MS : FEEDBACK_RETRY_MS);
  }
}

function saveFeedback() {
  clearTimeout(feedbackTimer);
  feedbackTimer = 0;
  feedbackSaveWaits ||= inFeedbackTurn(() => {
    feedbackSaveWaits = null;
    return sendFeedbackFields();
  });
  return feedbackSaveWaits;
}

function drawFeedbackContext(draft) {
  const json = draft?.context || "";
  feedbackBox("include").checked = Boolean(json);
  feedbackBox("context").hidden = !json;
  feedbackBox("context-json").textContent = json;
}

function drawFeedbackDraft(draft) {
  const held = feedbackDraft;
  if (draft && held && draft.id === held.id && draft.revision < held.revision) return drawFeedbackList();
  feedbackDraft = draft;
  drawFeedbackContext(draft);
  drawFeedbackText();
  drawFeedbackChecks(draft);
  drawFeedbackList();
}

function noteFeedbackLimits(limits) {
  if (limits) feedbackLimits = { ...feedbackLimits, ...limits };
}

function feedbackSize(bytes) {
  return bytes >= FEEDBACK_MB ? `${(bytes / FEEDBACK_MB).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function sayEvidence(state, sentence) {
  sayIn(feedbackBox("evidence-said"), state, sentence);
}

function drawFeedbackText() {
  const sending = feedbackDraft?.outgoing;
  const written = Boolean(feedbackDraft?.text?.trim());
  feedbackBox("sending-title").textContent = written ? sending.title : "";
  feedbackBox("sending-text").textContent = written ? sending.body : "Nothing written yet.";
  feedbackBox("sending-source").textContent = !written ? "" : sending.source === "writer" ? "Written by the writer from your words." : sending.note;
}

const FEEDBACK_CHIPS = { pass: "ok", hold: "caution", interrupted: "caution", fail: "fault" };
let feedbackChecksShown = "";
let feedbackPolling = 0;

function feedbackCheckRow(check) {
  const words = el("div", { class: "check-words" }, el("strong", { text: check.title }), " ", el("span", { text: check.reason || "Not run yet." }));
  if (check.stale) words.append(" ", el("em", { text: "Stale, Submit runs it again." }));
  if (check.detail.version) words.append(el("br"), el("span", { class: "muted", text: `Vision: ${check.detail.path}, version ${check.detail.version}` }));
  for (const match of check.detail.matches ?? []) {
    words.append(el("br"), link(match.url, `#${match.number} ${match.title}`), el("span", { class: "muted", text: ` · ${match.state} · chance ${match.chance.toFixed(2)}` }));
  }
  const chip = stateChip(FEEDBACK_CHIPS[check.result]);
  return el("li", {}, chip ?? el("span", { class: "state", text: check.result === "running" ? "Running" : "Not run" }), " ", words);
}

function drawFeedbackChecks(draft) {
  const checks = draft?.checks ?? [];
  const shown = JSON.stringify([checks, draft?.summary, draft?.jev_cost_usd, draft?.inspect_cost_usd, draft?.vision,
    draft?.delivery, draft?.status, draft?.working]);
  const checking = draft?.status === "checking";
  const inFlight = checking || (draft?.status === "filing" && draft.working);
  const submit = feedbackBox("submit");
  submit.disabled = inFlight;
  submit.textContent = checking ? "Checking…" : inFlight ? "Filing…" : "Submit";
  if (inFlight) pollFeedbackChecks();
  if (shown === feedbackChecksShown) return;
  feedbackChecksShown = shown;
  feedbackBox("checks").replaceChildren(...checks.map(feedbackCheckRow));
  feedbackBox("checks-cost").textContent = [
    draft?.jev_cost_usd > 0 ? `JEV cost so far: ${money(draft.jev_cost_usd)}` : "",
    draft?.inspect_cost_usd > 0 ? `Evidence inspection cost so far: ${money(draft.inspect_cost_usd)}` : "",
  ].filter(Boolean).join(" · ");
  feedbackBox("ready").textContent = draft && checks.some((c) => c.result !== "none") ? draft.summary : "";
  if (draft && !draft.vision.version && checks.some((c) => c.result !== "none")) feedbackBox("ready").append(" ", draft.vision.setting);
}

function pollFeedbackChecks() {
  if (feedbackPolling) return;
  feedbackPolling = setTimeout(async function again() {
    feedbackPolling = 0;
    try {
      takeFeedbackAnswer(await call("/api/capture"));
    } catch {
      feedbackPolling = setTimeout(again, FEEDBACK_RETRY_MS);
    }
  }, 1000);
}

function submitFeedback() {
  return inFeedbackTurn(async () => {
    await feedbackUploads;
    await sendFeedbackFields();
    if (feedbackDirty || feedbackPending.length || !feedbackDraft) {
      return sayFeedback("caution", feedbackDraft ? "The draft is not saved yet, or a file is still sending. Submit again once it is." : "Write the request before you submit it.");
    }
    try {
      takeFeedbackAnswer(await call("/api/capture/submit", { id: feedbackDraft.id, revision: feedbackDraft.revision }));
      feedbackBox("result").hidden = true;
    } catch (e) {
      if (e instanceof ErrorAnswer && e.status === 409) {
        await readFeedbackDraft();
        sayFeedback("caution", "The draft changed, so it was read again. Press Submit to check it.");
      } else {
        sayFeedback("caution", `The request was not submitted: ${failedFeedback(e)}`);
      }
    }
  });
}

function evidenceDetails(row, seen) {
  const seconds = row.seconds ?? (Number.isFinite(seen) ? seen : null);
  return [FEEDBACK_TYPES[row.type] || row.type, feedbackSize(row.bytes), seconds === null ? null : `${Math.round(seconds)} s`,
    FEEDBACK_SOURCES[row.source] || row.source].filter(Boolean).join(" · ");
}

function checkEvidenceLength(row, video, details) {
  const seconds = video.duration;
  if (!Number.isFinite(seconds)) return;
  const most = feedbackLimits.video_seconds;
  if (seconds > most + FEEDBACK_LENGTH_SLACK) {
    removeFeedbackEvidence(row, `${row.name} is ${Math.round(seconds)} s long and the limit is ${most} s, so it was removed.`);
  } else {
    details.textContent = evidenceDetails(row, seconds);
  }
}

function stagedEvidenceItem(row) {
  const src = `/api/capture/evidence/${row.id}`;
  const details = el("span", { text: evidenceDetails(row) });
  const media = row.kind === "image" ? el("img", { src, alt: row.name }) : el("video", { controls: true, preload: "metadata", src });
  if (row.kind === "video") media.addEventListener("loadedmetadata", () => checkEvidenceLength(row, media, details));
  const remove = button("Remove", "feedback-remove", () => removeFeedbackEvidence(row, `Removed ${row.name}.`, remove), { of: String(row.id) });
  const inspection = el("span", { class: "evidence-inspection" });
  const uploaded = el("span", { class: "evidence-uploaded" });
  return el("li", {}, media, el("div", { class: "evidence-words" }, el("strong", { text: row.name }), el("br"), details, el("br"), inspection, uploaded), remove);
}

function inspectionLine(found) {
  if (!found) return "";
  if (!found.inspected) return `Not inspected: ${found.why}`;
  const flags = found.flags.map((f) => FEEDBACK_FLAGS[f] || f).join(", ");
  const verdict = flags ? `shows ${flags}` : "nothing sensitive found";
  return `Inspected by ${found.model}: ${verdict}. Saw: ${found.saw}`;
}

function pendingEvidenceItem(item) {
  const failed = item.state === "failed";
  const status = failed ? `Not staged: ${item.why}` : "Sending…";
  const again = el("button", { type: "button", "data-focus": `feedback-again:${item.key}`, text: "Try again", on: { click: () => uploadFeedbackItem(item) } });
  const remove = button("Remove", "feedback-remove", () => {
    feedbackPending = feedbackPending.filter((p) => p !== item);
    drawFeedbackList();
    feedbackBox("attach").focus();
  }, { of: `pending-${item.key}` });
  return el("li", {}, el("div", { class: "evidence-words" }, el("strong", { text: item.name }), el("br"),
    el("span", { text: `${feedbackSize(item.blob.size)} · ${status}` })), failed ? [again, remove] : null);
}

function drawFeedbackList() {
  const staged = feedbackDraft?.evidence ?? [];
  const json = feedbackDraft?.context || "";
  feedbackBox("sending-context").textContent = json
    ? `App state: Included, ${new TextEncoder().encode(json).length} bytes, shown above.` : "App state: Not included.";
  const ids = new Set(staged.map((row) => row.id));
  for (const id of [...feedbackItems.keys()]) if (!ids.has(id)) feedbackItems.delete(id);
  for (const row of staged) if (!feedbackItems.has(row.id)) feedbackItems.set(row.id, stagedEvidenceItem(row));
  for (const row of staged) {
    const item = feedbackItems.get(row.id);
    item.querySelector(".evidence-inspection").textContent = inspectionLine(row.inspection);
    item.querySelector(".evidence-uploaded").replaceChildren(...(row.link ? [" ", link(row.link, "Uploaded")] : []));
  }
  const wanted = [...staged.map((row) => feedbackItems.get(row.id)), ...feedbackPending.map(pendingEvidenceItem)];
  const list = feedbackBox("evidence");
  wanted.forEach((node, i) => {
    if (list.children[i] !== node) list.insertBefore(node, list.children[i] ?? null);
  });
  while (list.children.length > wanted.length) list.lastElementChild.remove();
  const bytes = staged.reduce((sum, row) => sum + row.bytes, 0);
  feedbackBox("evidence-total").textContent = `${staged.length} of ${feedbackLimits.evidence_items} items · ${(bytes / FEEDBACK_MB).toFixed(1)} of ${feedbackLimits.evidence_bytes / FEEDBACK_MB} MB`;
  lockFeedback(feedbackDraft?.status === "filing");
}

function lockFeedback(locked) {
  for (const radio of document.querySelectorAll("input[name=feedback-kind]")) radio.disabled = locked;
  for (const name of FEEDBACK_TEXTS) feedbackBox(name).readOnly = locked;
  const controls = ["include", "context-again", "context-drop", "shot", "record", "attach", "discard"];
  for (const id of controls) document.getElementById(`feedback-${id}`).disabled = locked;
  for (const remove of feedbackBox("evidence").querySelectorAll("button")) remove.disabled = locked;
}

function onFeedbackUploads(work) {
  const turn = feedbackUploads.then(work);
  feedbackUploads = turn.catch(() => {});
  return turn;
}

function queueFeedbackFiles(files) {
  const items = files.map((file) => ({ key: ++feedbackKeys, state: "sending", why: "", seconds: null, ...file }));
  feedbackPending.push(...items);
  drawFeedbackList();
  for (const item of items) uploadFeedbackItem(item);
}

function uploadFeedbackItem(item) {
  return onFeedbackUploads(async () => {
    if (!feedbackPending.includes(item)) return;
    item.state = "sending";
    drawFeedbackList();
    const path = `/api/capture/evidence?name=${encodeURIComponent(item.name)}&source=${item.source}&seconds=${item.seconds ?? ""}`;
    try {
      const answered = await call(path, item.blob);
      noteFeedbackLimits(answered.limits);
      feedbackPending = feedbackPending.filter((p) => p !== item);
      drawFeedbackDraft(answered.draft);
      sayEvidence("ok", answered.existing ? "That file is already staged." : `Staged ${answered.evidence.name}.`);
    } catch (e) {
      item.state = "failed";
      item.why = failedFeedback(e);
      drawFeedbackList();
      sayEvidence("caution", `${item.name} was not staged: ${failedFeedback(e)}`);
    }
  });
}

function removeFeedbackEvidence(row, said, from) {
  const buttons = [...feedbackBox("evidence").querySelectorAll("button[data-focus^='feedback-remove']")];
  const at = from ? buttons.indexOf(from) : -1;
  return onFeedbackUploads(async () => {
    try {
      drawFeedbackDraft((await call(`/api/capture/evidence/${row.id}/drop`, {})).draft);
      sayEvidence("ok", said);
    } catch (e) {
      sayEvidence("caution", `${row.name} was not removed: ${failedFeedback(e)}`);
      return;
    }
    if (!from) return;
    const left = [...feedbackBox("evidence").querySelectorAll("button[data-focus^='feedback-remove']")];
    (left[Math.min(at, left.length - 1)] ?? feedbackBox("attach")).focus();
  });
}

function refusedFeedbackFile(file, count, bytes) {
  const label = FEEDBACK_TYPES[file.type];
  if (!label) return `${file.name} is not a PNG, JPEG or WebP image or a WebM or MP4 video, so it was not added.`;
  const image = file.type.startsWith("image/");
  const most = image ? feedbackLimits.image_bytes : feedbackLimits.video_bytes;
  if (file.size > most) return `${file.name} is ${feedbackSize(file.size)} and ${image ? "an image" : "a video"} is limited to ${most / FEEDBACK_MB} MB, so it was not added.`;
  if (count >= feedbackLimits.evidence_items) return `A draft holds at most ${feedbackLimits.evidence_items} items, so ${file.name} was not added. Remove one first.`;
  if (bytes + file.size > feedbackLimits.evidence_bytes) return `A draft holds at most ${feedbackLimits.evidence_bytes / FEEDBACK_MB} MB of evidence, so ${file.name} was not added.`;
  return "";
}

function attachFeedbackFiles(input) {
  const files = [...input.files];
  input.value = "";
  const staged = feedbackDraft?.evidence ?? [];
  let count = staged.length + feedbackPending.length;
  let bytes = staged.reduce((sum, row) => sum + row.bytes, 0) + feedbackPending.reduce((sum, item) => sum + item.blob.size, 0);
  const accepted = [];
  const refused = [];
  for (const file of files) {
    const why = refusedFeedbackFile(file, count, bytes);
    if (why) {
      refused.push(why);
      continue;
    }
    count++;
    bytes += file.size;
    accepted.push({ blob: file, name: file.name, source: "attached" });
  }
  if (refused.length) sayEvidence("caution", refused.join(" "));
  if (accepted.length) queueFeedbackFiles(accepted);
}

function stamp() {
  const now = new Date();
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}

function stepFeedbackAside() {
  feedbackAside = true;
  document.getElementById("feedback").close();
}

function returnToFeedback(focus) {
  feedbackAside = false;
  const dialog = document.getElementById("feedback");
  if (!dialog.open) dialog.showModal();
  (focus ?? (COARSE.matches ? document.getElementById("feedback-title") : feedbackBox("text"))).focus();
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
    if (blob && blob.size <= feedbackLimits.image_bytes) return { blob, name: `screenshot-${stamp()}.png`, source: "screenshot" };
  }
  throw new Error(`The screenshot is over ${feedbackLimits.image_bytes / FEEDBACK_MB} MB even scaled down, so it was not staged`);
}

function recordedClock(seconds) {
  return `${Math.floor(seconds / 60)}:${pad(Math.floor(seconds % 60))}`;
}

async function recordStream(stream) {
  const most = feedbackLimits.video_seconds;
  const mimeType = FEEDBACK_RECORDER_TYPES.find((type) => MediaRecorder.isTypeSupported(type));
  const recorder = new MediaRecorder(stream, { ...(mimeType ? { mimeType } : {}), videoBitsPerSecond: 4_000_000 });
  const chunks = [];
  recorder.addEventListener("dataavailable", (event) => {
    if (event.data.size) chunks.push(event.data);
  });
  const stopped = new Promise((resolve) => recorder.addEventListener("stop", resolve, { once: true }));
  const halt = () => {
    if (recorder.state !== "inactive") recorder.stop();
  };
  const bar = document.getElementById("feedback-recording");
  const started = performance.now();
  const elapsed = () => Math.min(most, (performance.now() - started) / 1000);
  const tick = () => {
    document.getElementById("feedback-recording-time").textContent = `Recording ${recordedClock(elapsed())} of ${recordedClock(most)}`;
  };
  stepFeedbackAside();
  tick();
  bar.hidden = false;
  document.getElementById("feedback-recording-stop").focus();
  feedbackStopRecording = halt;
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
    feedbackStopRecording = null;
    bar.hidden = true;
  }
  stopTracks(stream);
  const type = recorder.mimeType || mimeType || "video/webm";
  if (!chunks.length) throw new Error("The recording was empty");
  return { blob: new Blob(chunks, { type }), name: `recording-${stamp()}.${type.includes("mp4") ? "mp4" : "webm"}`, source: "recording",
    seconds: Math.round(elapsed() * 10) / 10 };
}

async function captureFeedbackScreen(recording) {
  if (feedbackCapturing) return;
  if (!navigator.mediaDevices?.getDisplayMedia || (recording && !window.MediaRecorder)) return sayEvidence("caution", FEEDBACK_NO_CAPTURE);
  feedbackCapturing = true;
  const asked = navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
  let stream = null;
  let focus = null;
  try {
    stream = await asked;
    if (!recording) stepFeedbackAside();
    const file = await (recording ? recordStream(stream) : framePng(stream));
    queueFeedbackFiles([file]);
  } catch (e) {
    focus = feedbackBox("attach");
    sayEvidence("caution", e.name === "NotAllowedError" ? "Nothing was captured. You can attach a file instead."
      : `The screen could not be captured (${e.name === "Error" ? e.message : e.name}). Nothing was captured, and you can attach a file instead.`);
  } finally {
    stopTracks(stream);
    feedbackCapturing = false;
    if (feedbackAside) returnToFeedback(focus);
    else focus?.focus();
  }
}

async function captureFeedbackContext() {
  clearTimeout(feedbackTimer);
  feedbackTimer = 0;
  await inFeedbackTurn(async () => {
    await sendFeedbackFields();
    try {
      const answered = await call("/api/capture/context", { view: route.view, detail: route.detail });
      drawFeedbackDraft(answered.draft);
      sayFeedback("ok", `Captured the app state at ${clock(new Date())}.`);
    } catch (e) {
      if (feedbackBox("context").hidden) feedbackBox("include").checked = false;
      sayFeedback("caution", `The app state was not added: ${failedFeedback(e)}`);
    }
  });
}

async function dropFeedbackContext() {
  await inFeedbackTurn(async () => {
    try {
      drawFeedbackDraft((await call("/api/capture/context/drop", {})).draft);
      sayFeedback("ok", "Removed the app state.");
    } catch (e) {
      feedbackBox("include").checked = !feedbackBox("context").hidden;
      sayFeedback("caution", `The app state was not removed: ${failedFeedback(e)}`);
    }
  });
  feedbackBox("include").focus();
}

async function discardFeedback() {
  if (feedbackDiscarding) return;
  feedbackDiscarding = true;
  forgetFeedbackFiled();
  clearTimeout(feedbackTimer);
  feedbackTimer = 0;
  const clicked = feedbackFields();
  feedbackPending = [];
  drawFeedbackList();
  await feedbackUploads;
  await inFeedbackTurn(async () => {
    let failed = null;
    try {
      await call("/api/capture/discard", {});
    } catch (e) {
      failed = e;
    }
    feedbackDiscarding = false;
    if (failed) {
      sayFeedback("caution", `The draft was not discarded: ${failedFeedback(failed)} It is still kept.`);
      if (feedbackDirty) saveFeedbackLater(FEEDBACK_RETRY_MS);
      return;
    }
    const typed = feedbackFields();
    const since = Object.fromEntries(Object.keys(FEEDBACK_EMPTY).map((name) => [name, typed[name] === clicked[name] ? FEEDBACK_EMPTY[name] : typed[name]]));
    putFeedback(since);
    feedbackBase = { ...FEEDBACK_EMPTY };
    feedbackRead = true;
    drawFeedbackDraft(null);
    feedbackDirty =JSON.stringify(since) !== JSON.stringify(FEEDBACK_EMPTY);
    if (!feedbackDirty) {
      forgetFeedbackHere();
      sayFeedback("ok", "Discarded the draft.");
      return;
    }
    keepFeedbackHere(true);
    saveFeedbackLater(FEEDBACK_SAVE_MS);
    sayFeedback("ok", "Discarded the draft. What you typed since is kept on this device and saves as a new draft.");
  });
}

function openFeedback() {
  const dialog = document.getElementById("feedback");
  if (dialog.open || feedbackAside) return;
  trackAction("feedback-open");
  feedbackReturn = focusMark(document.activeElement);
  dialog.showModal();
  (COARSE.matches ? document.getElementById("feedback-title") : feedbackBox("text")).focus();
  if (!feedbackDirty) readFeedback();
}

function wireFeedback() {
  const dialog = document.getElementById("feedback");
  document.getElementById("feedback-open").addEventListener("click", openFeedback);
  document.getElementById("feedback-close").addEventListener("click", () => dialog.close());
  for (const name of FEEDBACK_TEXTS) feedbackBox(name).addEventListener("input", onFeedbackInput);
  for (const radio of document.querySelectorAll("input[name=feedback-kind]")) radio.addEventListener("change", onFeedbackInput);
  feedbackBox("include").addEventListener("change", (event) => (event.currentTarget.checked ? captureFeedbackContext() : dropFeedbackContext()));
  feedbackBox("context-again").addEventListener("click", captureFeedbackContext);
  feedbackBox("context-drop").addEventListener("click", dropFeedbackContext);
  document.getElementById("feedback-discard").addEventListener("click", discardFeedback);
  feedbackBox("submit").addEventListener("click", () => {
    trackAction("feedback-submit");
    submitFeedback();
  });
  document.getElementById("feedback-shot").addEventListener("click", () => {
    trackAction("feedback-screenshot");
    captureFeedbackScreen(false);
  });
  document.getElementById("feedback-record").addEventListener("click", () => {
    trackAction("feedback-record");
    captureFeedbackScreen(true);
  });
  feedbackBox("attach").addEventListener("click", () => {
    trackAction("feedback-attach");
    feedbackBox("file").click();
  });
  feedbackBox("file").addEventListener("change", (event) => attachFeedbackFiles(event.currentTarget));
  document.getElementById("feedback-recording-stop").addEventListener("click", () => feedbackStopRecording?.());
  dialog.addEventListener("close", () => {
    if (feedbackDirty) saveFeedback();
    if (feedbackAside) return;
    refocus(feedbackReturn);
    feedbackReturn = null;
  });
  const kept = keptFeedback();
  if (kept) putFeedback(kept);
  if (kept?.unsaved) {
    feedbackDirty = true;
    feedbackRead = true;
    saveFeedback();
  } else {
    feedbackBase = feedbackFields();
    readFeedback();
  }
}
