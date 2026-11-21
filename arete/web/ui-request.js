import { clientNotice } from "./ui-notice.js";

export async function webFetch(url, { headers, ...options } = {}) {
  const sent = new Headers(headers);
  sent.set("X-Arete-Client", "web/1.1");
  const response = await fetch(url, { ...options, headers: sent });
  if (response.status === 400) {
    const body = await response.clone().json().catch(() => null);
    if (body?.code === "client_too_old" || body?.error === "client_too_old") {
      clientNotice.value = "This dashboard is out of date. Reload the page to update it.";
    }
  }
  return response;
}
