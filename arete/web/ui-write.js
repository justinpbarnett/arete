export function prepareWrite(url, data = {}, object = null, options = {}) {
  const body = { ...data };
  delete body.base_version;
  if (object?.version != null) body.base_version = object.version;
  return Object.freeze({ url, method: options.method ?? "POST", body: JSON.stringify(body),
    key: crypto.randomUUID(), keepalive: Boolean(options.keepalive) });
}

function headers(request) {
  return { "X-Arete": "1", "Idempotency-Key": request.key, "Content-Type": request.type ?? "application/json" };
}

export function postBody(url, body, type) {
  return fetch(url, { method: "POST", headers: headers({ key: crypto.randomUUID(), type }), body });
}

export async function sendWrite(request) {
  let response;
  try {
    response = await fetch(request.url, { method: request.method, headers: headers(request), body: request.body,
      keepalive: request.keepalive });
  } catch (cause) {
    throw new Error(`Request failed: ${cause.message}`);
  }
  let data;
  try { data = await response.json(); }
  catch { throw new Error(`Invalid response (${response.status})`); }
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error(`Invalid response (${response.status})`);
  if (!response.ok) {
    const error = new Error(typeof data.error === "string" ? data.error : `Request failed (${response.status})`);
    error.status = response.status;
    error.conflict = response.status === 409 && (data.error === "version_conflict" || data.code === "version_conflict");
    error.current = data.current;
    throw error;
  }
  return data;
}
