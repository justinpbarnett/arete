import { notice, failed } from "./ui-notice.js";

const entries = [];
let busy = false;
export function pushUndo(entry) { entries.push(entry); }
export function undoDepth() { return entries.length; }
export async function undo() {
  if (busy) return;
  const entry = entries.at(-1);
  if (!entry) { notice.value = "Nothing to undo"; return; }
  busy = true;
  try {
    if (entry.inverse) await entry.inverse();
    const at = entries.lastIndexOf(entry);
    if (at >= 0) entries.splice(at, 1);
    entry.restore?.();
    notice.value = entry.explanation ?? `Undid: ${entry.label}`;
  } catch (error) { failed(error); }
  finally { busy = false; }
}
