export function pairingHelp(protocol = location.protocol) {
  return protocol === "https:"
    ? "To pair this browser, open /pair and use the code from ‘arete pair’ on the serve host or from Pair a device in a paired browser."
    : "Browser pairing needs HTTPS. Set https = true in [dashboard] on the serve host.";
}

export async function pairedBrowser(fetcher = fetch) {
  const request = () => fetcher("/api/v1/meta", { cache: "no-store" });
  let response = await request();
  if (response.status === 401) response = await request();
  if (!response.ok) throw new Error(`Could not check browser pairing (${response.status}).`);
  return response.headers.get("Arete-Session") === "paired";
}

const classic = globalThis.document?.getElementById?.("browser-auth-link");
if (classic && location.protocol === "https:") {
  window.addEventListener("pageshow", () => {
    pairedBrowser().then(paired => { classic.hidden = !paired; }).catch(() => {});
  });
}
