"use strict";

const STATUS_FILTERS = [["", "All"], ["running", "Running"], ["failed", "Failed"]];
const PAGE_SIZE = 25;

function drawRuns(data, q) {
  const limit = Number(q.limit) || PAGE_SIZE;
  const filter = (key, value) => go(viewHash("runs", { ...q, [key]: value, limit: "" }), "link", { replace: true });
  const filtered = ["status", "kind", "repo", "loop"].some((key) => q[key]);
  const paid = paidColumn(data.runs);
  fill("runs-list",
    el("div", { class: "filters" },
      seg("Status", STATUS_FILTERS, q.status || "", (v) => filter("status", v)),
      seg("Kind", [["", "Every kind"], ...withCurrent(data.facets.kind, q.kind).map((k) => [k, k])], q.kind || "", (v) => filter("kind", v)),
      el("div", { class: "fields" },
        picker("Repo", "runs-repo", data.facets.repo, q.repo, (v) => filter("repo", v)),
        picker("Loop", "runs-loop", data.facets.loop, q.loop, (v) => filter("loop", v)))),
    data.runs.length
      ? table("Running first, then newest first",
        [["Work", "c-work"], ["Kind", "c-kind wide-only"], ["Status", "c-status"], ["Started", "c-time"], ["Time", "num c-duration wide-only"], ...paid.head, ["Result", "c-result wide-only"]],
        data.runs.map((r) => linkedRow(detailHash("run", r.id),
          cell(rowLink("run", r.id, runName(r))), cell(r.kind), cell(runStatus(r.status), "nb wrap-narrow"), cell(clock(r.started), "nb wrap-narrow"),
          cell(duration(r.seconds), "num wrap-narrow"), paid.cell(r),
          cell(r.pr_url ? pullLinks(r.pr_url) : clampedSummary(r.summary), "muted-cell"))))
      : quiet(filtered ? "No run matches these filters." : "No run has started yet."),
    data.more ? el("div", { class: "actions" },
      button(`Show ${PAGE_SIZE} more`, "runs-more", () => go(viewHash("runs", { ...q, limit: String(limit + PAGE_SIZE) }), "link", { replace: true }))) : null);
  return data.runs.some((r) => r.status === "running") ? LIVE_MS : SLOW_MS;
}

function runsQuery(q) {
  return new URLSearchParams({ ...q, limit: q.limit || String(PAGE_SIZE) });
}

function withCurrent(values, current) {
  return current && !values.includes(current) ? [...values, current] : values;
}

function picker(label, id, values, current, onPick) {
  const select = el("select", { id, "data-focus": id, on: { change: (e) => {
    trackAction(`filter-${label.toLowerCase()}`);
    onPick(e.target.value);
  } } }, el("option", { value: "", text: `Any ${label.toLowerCase()}` }), withCurrent(values, current).map((v) => el("option", { value: v, text: v })));
  select.value = current || "";
  return el("div", { class: "field" }, el("label", { for: id, text: label }), select);
}
