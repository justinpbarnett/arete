import { pairedBrowser, pairingHelp } from "./browser-auth.js";
import { webFetch } from "./ui-request.js";
import { prepareWrite, sendWrite } from "./ui-write.js";

const el = id => document.getElementById(id);
const code = el("claim-form").elements.code;
const notice = el("credential-status");
const tokens = "/api/v1/tokens";
const pairings = "/api/v1/auth/pairings";
const write = (path, body) => sendWrite(prepareWrite(path, body));

function reveal(id, value = "") {
  el(id).hidden = !value;
  el(id).querySelector("input").value = value;
}

function show(paired) {
  el("claim").hidden = paired && !code.value;
  el("manage").hidden = !paired;
}

let busy = false;
function action(callback) {
  return async event => {
    event?.preventDefault();
    if (busy) return;
    busy = true;
    notice.textContent = "";
    try {
      await callback(event);
    } catch (error) {
      notice.textContent = error.message;
    } finally {
      busy = false;
    }
  };
}

async function update({ text } = {}) {
  reveal("issued", text === "[shown once]" ? "" : text);
  if (text === "[shown once]") notice.textContent = "This token was already shown.";
  const response = await webFetch(tokens, { cache: "no-store" });
  if (!response.ok) {
    if (response.status === 401) show(false);
    throw new Error(response.status === 401 ? pairingHelp() : `Could not load tokens (${response.status}).`);
  }
  const { tokens: entries } = await response.json();
  el("token-list").replaceChildren(...entries.map(token => {
    const row = document.createElement("li");
    row.textContent = `${token.label} · ${token.scopes.join(", ")} `;
    if (token.revoked_at || (token.expires && Date.parse(token.expires) <= Date.now())) return row;
    for (const verb of token.grace_until ? ["revoke"] : ["rotate", "revoke"]) {
      const button = document.createElement("button");
      button.textContent = verb === "rotate" ? "Rotate" : "Revoke";
      button.onclick = action(async () => update(await write(`${tokens}/${encodeURIComponent(token.id)}/${verb}`)));
      row.append(button);
    }
    return row;
  }));
}

el("claim-form").onsubmit = action(async () => {
  await write(`${pairings}/claim`, { code: code.value });
  code.value = "";
  show(true);
  await update();
});
el("start-pair").onclick = action(async () => {
  const result = await write(pairings);
  reveal("pair-link", result.link);
  el("pair-expiry").textContent = `Code ${result.code} · expires ${new Date(result.expires).toLocaleString()}`;
});
el("sign-out").onclick = action(async () => {
  await write("/api/v1/auth/logout");
  clearSecrets();
  show(false);
});
el("issue-form").onsubmit = action(async event => {
  const data = new FormData(event.currentTarget);
  const scopes = data.get("scopes").split(",").map(scope => scope.trim()).filter(Boolean);
  await update(await write(tokens, { label: data.get("label"), profile: data.get("profile"), scopes }));
});

function clearSecrets() {
  reveal("issued");
  reveal("pair-link");
  code.value = "";
}

el("hide-token").onclick = () => reveal("issued");
window.addEventListener("pagehide", clearSecrets);
const fragment = location.hash.slice(1);
if (fragment) history.replaceState(null, "", location.pathname);
if (location.protocol === "https:") {
  if (/^[0-9]{6}$/.test(fragment)) code.value = fragment;
  action(async () => {
    const paired = await pairedBrowser(webFetch);
    show(paired);
    if (paired) await update();
    else notice.textContent = pairingHelp();
  })();
} else notice.textContent = pairingHelp();
