import { signal } from "./vendor-signals.js";

export const notice = signal("");
export function succeeded(label, undoable = true) { notice.value = undoable ? `${label} · u to undo` : label; }
export function failed(error) { notice.value = `Failed: ${error.message ?? error}`; }
