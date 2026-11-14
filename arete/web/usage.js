"use strict";

const WAYS_IN = {
  start: "Opening the dashboard", sidebar: "The sidebar", link: "A link or a row on a page", palette: "Find",
  key: "G then a letter", back: "Back or Forward", close: "Closing an open item", step: "Previous or Next",
  return: "Coming back to the tab",
};
const ACTION_WORDS = {
  approve: "Approve or Try again", reject: "Reject", stop: "Stop a run", "send-ready": "Send a ready ticket",
  "run-anyway": "Run a skipped ticket anyway", done: "Mark done", "file-draft": "File a drafted issue from Live",
  "drop-draft": "Drop a drafted issue from Live", "open-console": "Open the console from Live",
  jump: "Jump to a section", close: "Close an open item", previous: "Previous", next: "Next",
  "filter-status": "Runs by status", "filter-kind": "Runs by kind", "filter-repo": "Runs by repo", "filter-loop": "Runs by loop",
  "runs-more": "Show more runs", "launch-toggle": "New session", launch: "Launch a session", "session-send": "Send to a session",
  "loop-new-toggle": "New loop", "draft-loop": "Draft a loop", start: "Start a loop", try: "Try a loop", switch: "Turn a loop on or off",
  "serve-install": "Install or restart serve", "filter-groups": "Triage group", "triage-refresh": "Refresh triage",
  research: "Research a ticket", "ticket-send": "Send a ticket to the pipeline", probe: "Probe a device", "checks-run": "Run the checks",
  "settings-save": "Save the settings", "filter-window": "Usage window", "console-open": "Open the console",
  "chat-send": "Send in the console", "chat-new": "New chat", "draft-file": "File a drafted issue from the console",
  "draft-drop": "Drop a drafted issue from the console", find: "Find", "feedback-open": "Open feedback", "feedback-submit": "Submit feedback for checks","feedback-screenshot": "Take a screenshot for feedback",
  "feedback-record": "Record a video for feedback", "feedback-attach": "Attach a file to feedback", "feedback-remove": "Remove feedback evidence", "go-key": "G then a letter",
};
const USAGE_WINDOWS = ["7", "30", "90"];
const DEFAULT_USAGE_WINDOW = "30";

function viewWords(name) {
  return VIEWS[name]?.label ?? (ITEM_LABELS[name] ? `${ITEM_LABELS[name]} pages` : name);
}

function msWords(ms) {
  return duration(Math.round(ms / 1000));
}

function drawUsage(u) {
  const windowDays = String(u.days);
  fill("usage-body",
    seg("Window", USAGE_WINDOWS.map((d) => [d, [figure(d), " days"]]), windowDays,
      (d) => go(viewHash("usage", { days: d === DEFAULT_USAGE_WINDOW ? "" : d }), "link", { replace: true })),
    quiet(u.first ? ["Counted since ", zonedClock(u.first), `, over the last ${u.days} days. A visit is one stay on a view or an open item; time counts only while the tab shows.`]
      : `Nothing was counted in the last ${u.days} days.`),
    heading(1, "Views"),
    u.views.length ? table("Most visited first, with the median stay", [["View"], ["Visits", "num c-n"], ["Desktop", "num c-n wide-only"], ["Phone", "num c-n"], ["Time", "num c-duration wide-only"], ["Stay", "num c-duration"]],
      u.views.map((v) => el("tr", {}, cell(viewWords(v.view)), cell(v.visits, "num"), cell(v.desktop, "num"), cell(v.phone, "num"),
        cell(msWords(v.ms), "num"), cell(msWords(v.median_ms), "num"))))
      : quiet("No visit yet."),
    heading(2, "Ways in"),
    u.ways_in.length ? table("", [["Way in"], ["Visits", "num c-n"]],
      u.ways_in.map((w) => el("tr", {}, cell(WAYS_IN[w.via] ?? w.via), cell(w.visits, "num"))))
      : quiet("No visit yet."),
    heading(3, "Actions"),
    u.actions.length ? table("Buttons and keys you pressed", [["Action"], ["Count", "num c-n"], ["Desktop", "num c-n wide-only"], ["Phone", "num c-n"]],
      u.actions.map((a) => el("tr", {}, cell(ACTION_WORDS[a.name] ?? el("code", { text: a.name })), cell(a.count, "num"), cell(a.desktop, "num"), cell(a.phone, "num"))))
      : quiet("No action yet."),
    quiet(`Usage keeps view and button names with counts and times only, never what you type or what a page shows, and drops anything older than ${u.keep_days} days.`));
  return SLOW_MS;
}
