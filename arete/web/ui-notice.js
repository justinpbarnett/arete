import { signal } from "./vendor-signals.js";

export const notice = signal("");
export function succeeded(label) { notice.value = `${label} · u to undo`; }
export function failed(error) { notice.value = `Failed: ${error.message ?? error}`; }
