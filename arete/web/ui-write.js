function idempotencyKey() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
}

export function prepareWrite(url, data = {}, object = null, options = {}) {
  const body = { ...data };
  delete body.base_version;
  if (object?.version != null) body.base_version = object.version;
  return Object.freeze({ url, method: options.method ?? "POST", body: JSON.stringify(body),
    key: idempotencyKey(), keepalive: Boolean(options.keepalive) });
}

const publications = new Map();
const PUBLICATION_PREFIX = "arete:pending-publication:";

function savedPublication(id) {
  try {
    const key = sessionStorage.getItem(id);
    return /^[0-9a-f]{32}$/.test(key ?? "") ? key : null;
  } catch {
    return null;
  }
}

function rememberPublication(id, key) {
  try {
    if (key) sessionStorage.setItem(id, key);
    else sessionStorage.removeItem(id);
  } catch {
    // The in-memory entry still covers retries until this page is closed.
  }
}

export function isPublication(path, body) {
  return /^\/api\/(?:triage\/[^/]+\/send|chats\/\d+\/file)$/.test(path)
    || (/^\/api\/approvals\/\d+$/.test(path) && body?.approve === true);
}

export async function sendPublication(path, body, onResponse) {
  const fresh = prepareWrite(path, body);
  const fingerprint = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify([path, fresh.body])));
  const id = PUBLICATION_PREFIX + Array.from(new Uint8Array(fingerprint), byte => byte.toString(16).padStart(2, "0")).join("");
  const request = publications.get(id) ?? Object.freeze({ ...fresh, key: savedPublication(id) ?? fresh.key });
  publications.set(id, request);
  rememberPublication(id, request.key);
  const result = await sendWrite(request, onResponse);
  if (publications.get(id) === request) {
    if (savedPublication(id) === request.key) rememberPublication(id, null);
    publications.delete(id);
  }
  return result;
}

function headers(request) {
  return { "X-Arete": "1", "Idempotency-Key": request.key, "Content-Type": request.type ?? "application/json" };
}

export function postBody(url, body, type) {
  return fetch(url, { method: "POST", headers: headers({ key: idempotencyKey(), type }), body });
}

export async function sendWrite(request, onResponse = null) {
  let response;
  try {
    response = await fetch(request.url, { method: request.method, headers: headers(request), body: request.body,
      keepalive: request.keepalive });
  } catch (cause) {
    throw new Error(`Request failed: ${cause.message}`);
  }
  onResponse?.(response);
  let data;
  try { data = await response.json(); }
  catch { throw new Error(`Invalid response (${response.status})`); }
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error(`Invalid response (${response.status})`);
  if (!response.ok) {
    const error = new Error(typeof data.error === "string" ? data.error : `Request failed (${response.status})`);
    error.status = response.status;
    error.conflict = response.status === 409 && (data.error === "version_conflict" || data.code === "version_conflict");
    error.current = data.current;
    error.answer = data;
    throw error;
  }
  return data;
}
