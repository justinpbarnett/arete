"use strict";

let outcomesData = null;

function niceMax(value, whole) {
  if (value <= 0) return 1;
  const stepSize = 10 ** Math.floor(Math.log10(value));
  const nice = [1, 2, 2.5, 5, 10].map((m) => m * stepSize).find((m) => m >= value && (!whole || Number.isInteger(m)));
  return nice ?? Math.ceil(value);
}

function dayLabel(day) {
  return day.slice(5).replace("-", " ");
}

function dayChart(days, width, height, top, label, dayTickGap) {
  const plot = { left: 58, right: width - 16, top, bottom: height - 32 };
  plot.pitch = (plot.right - plot.left) / days.length;
  plot.X = (i) => plot.left + i * plot.pitch;
  plot.chart = svg("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": label });
  days.forEach((d, i) => {
    if (i % 7 !== (days.length - 1) % 7) return;
    const x = plot.X(i) + plot.pitch / 2, y = plot.bottom + dayTickGap;
    plot.chart.append(svg("line", { class: "tick", x1: x, y1: y, x2: x, y2: y + 5 }));
    plot.chart.append(svg("text", { class: "fig", x, y: y + 20, "text-anchor": i === days.length - 1 ? "end" : "middle" }, dayLabel(d.day)));
  });
  return plot;
}

function valueTicks(plot, values, Y, words) {
  for (const v of values) {
    plot.chart.append(svg("line", { class: "tick", x1: plot.left - 5, y1: Y(v), x2: plot.left, y2: Y(v) }));
    plot.chart.append(svg("text", { class: "fig", x: plot.left - 9, y: Y(v) + 4, "text-anchor": "end" }, words(v)));
  }
}

function bar(plot, i, cls, y, height) {
  plot.chart.append(svg("rect", { class: cls, x: plot.X(i) + plot.pitch * 0.18, y, width: Math.max(1, plot.pitch * 0.64), height }));
}

function paidChart(days, width) {
  const plot = dayChart(days, width, 230, 30, `Paid per day for the last ${days.length} days, with today's bar filled`, 0);
  const { chart, left, right, top, bottom, pitch, X } = plot;
  const max = niceMax(Math.max(...days.map((d) => d.paid)), false);
  const Y = (v) => bottom - (v / max) * (bottom - top);
  valueTicks(plot, [0, max / 2, max], Y, (v) => money(v).replace(".00", ""));
  days.forEach((d, i) => d.paid > 0 && bar(plot, i, i === days.length - 1 ? "frame this" : "frame", Y(d.paid), bottom - Y(d.paid)));
  const today = days[days.length - 1];
  const label = `Today ${money(today.paid)}`;
  const under = days.slice(-Math.ceil((label.length * 7.4) / pitch) - 1);
  const clear = Math.min(...under.map((d) => Y(d.paid)));
  const labelY = Math.max(top - 14, clear - 8);
  chart.append(svg("text", { class: "fig", x: X(days.length - 1) + pitch * 0.82, y: labelY, "text-anchor": "end" }, label));
  const todayX = X(days.length - 1) + pitch / 2, todayTop = Y(today.paid) - 3;
  if (todayTop - labelY > 12) chart.append(svg("line", { class: "tick", x1: todayX, y1: labelY + 5, x2: todayX, y2: todayTop }));
  chart.append(svg("line", { class: "axis", x1: left, y1: top - 6, x2: left, y2: bottom }));
  chart.append(svg("line", { class: "axis", x1: left, y1: bottom, x2: right, y2: bottom }));
  return chart;
}

function outcomeChart(days, width) {
  const plot = dayChart(days, width, 240, 22, `Runs shipped above the line and runs failed below it, per day for the last ${days.length} days`, 6);
  const { chart, left, right, top, bottom } = plot;
  const max = niceMax(Math.max(1, ...days.map((d) => Math.max(d.shipped, d.failed))), true);
  const mid = (top + bottom) / 2;
  const Y = (v) => mid - (v / max) * (mid - top);
  valueTicks(plot, [-max, 0, max], Y, (v) => String(Math.abs(v)));
  days.forEach((d, i) => {
    if (d.shipped) bar(plot, i, "frame", Y(d.shipped), mid - Y(d.shipped));
    if (d.failed) bar(plot, i, "frame below", mid, Y(-d.failed) - mid);
  });
  chart.append(svg("line", { class: "axis", x1: left, y1: top - 4, x2: left, y2: bottom + 4 }));
  chart.append(svg("line", { class: "axis", x1: left, y1: mid, x2: right, y2: mid }));
  chart.append(svg("text", { class: "name", x: left + 8, y: top + 2 }, "SHIPPED"));
  chart.append(svg("text", { class: "name", x: left + 8, y: bottom - 2 }, "FAILED"));
  return chart;
}

function decisionParts(d, days) {
  const p = d.pushes;
  const own = p.auto + d.tickets.triage;
  const byYou = p.approved + p.rejected + d.tickets.owner;
  const total = own + byYou;
  return [
    total ? [
      el("p", { class: "readout" }, percent(own / total), el("small", { text: "went without you" })),
      el("p", {}, `${own} of ${total} decisions in ${days} days needed nothing from you: ${count(p.auto, "push", "pushes")} landed without an approval, ` +
        `and triage sent ${count(d.tickets.triage, "ticket", "tickets")} to the pipeline itself. You approved ${count(p.approved, "push", "pushes")}, ` +
        `rejected ${p.rejected}, and sent ${count(d.tickets.owner, "ticket", "tickets")} yourself.` +
        (p.waiting ? ` ${count(p.waiting, "push waits", "pushes wait")} for you on Live.` : "") +
        (p.other ? ` ${count(p.other, "more push", "more pushes")} ended another way, such as failing its check.` : "")),
    ] : quiet(`Nothing was pushed or sent in ${days} days.`),
    table("", [["What"], ["Without you", "num c-rate"], ["By you", "num c-n"], ["Rejected", "num c-n"], ["Waiting", "num c-n"]], [
      el("tr", {}, cell("Pushes"), cell(p.auto, "num"), cell(p.approved, "num"), cell(p.rejected, "num"), cell(p.waiting, "num")),
      el("tr", {}, cell("Tickets sent to the pipeline"), cell(d.tickets.triage, "num"), cell(d.tickets.owner, "num"), cell("", "num"), cell("", "num")),
    ], { stack: true }),
  ];
}

function paidTable(rows) {
  return rows.length ? table("", [["Name"], ["Calls", "num c-n"], ["Tokens in", "num c-rate wide-only"], ["Tokens out", "num c-rate wide-only"], ["Paid", "num c-cost"]],
    rows.map((r) => el("tr", {}, cell(wrapAtSlashes(r.name)), cell(r.steps, "num"), cell(r.tokens_in.toLocaleString(), "num"),
      cell(r.tokens_out.toLocaleString(), "num"), cell(money(r.cost_usd), "num")))) : quiet("No paid model call in this window.");
}

function paidParts(t, width) {
  const today = t.days[t.days.length - 1];
  return [
    el("p", { class: "readout" }, money(t.total_paid), el("small", { text: `paid in ${t.days.length} days` })),
    t.total_paid > 0 ? [
      el("p", {}, `Only model calls you pay for count here. A run on the claude CLI rides your subscription, so it adds nothing. Today so far: ${money(today.paid)}.`),
      paidChart(t.days, width),
      el("h3", { text: "By model" }), paidTable(t.by_model),
      el("h3", { text: "By runner" }), paidTable(t.by_runner),
      el("h3", { text: "By loop or kind" }), paidTable(t.by_loop),
    ] : quiet(`Nothing was paid in ${t.days.length} days: every model call ran on the subscription.`),
  ];
}

function drawOutcomes() {
  const panel = outcomesData;
  if (!panel) return SLOW_MS;
  if (!("rows" in panel)) {
    fill("outcomes", unreadFault("Outcomes", panel));
    return SLOW_MS;
  }
  const t = panel.rows;
  const width = Math.max(300, Math.floor(document.getElementById("outcomes").clientWidth || 300));
  const shipped = t.days.reduce((n, d) => n + d.shipped, 0);
  const failed = t.days.reduce((n, d) => n + d.failed, 0);
  const other = t.days.reduce((n, d) => n + d.other, 0);
  const paid = paidColumn(t.repos);
  const titles = ["Outcomes per day", "Without you or by you", "What you paid", "By repo", "Evals"];
  fill("outcomes",
    jumps(titles),
    heading(1, titles[0]),
    el("p", {}, `${shipped} pipeline runs shipped a pull request and ${failed} failed in ${t.days.length} days.` +
      (other ? ` ${other} more ended another way, such as needing information.` : "")),
    outcomeChart(t.days, width),
    heading(2, titles[1]),
    decisionParts(t.decisions, t.days.length),
    heading(3, titles[2]),
    paidParts(t, width),
    heading(4, titles[3]),
    t.repos.length ? table("",
      [["Repo"], ["Runs", "num c-n"], ["Shipped", "num c-n"], ["Failed", "num c-n wide-only"], ["Pass rate", "num c-rate"], ["Median time", "num c-duration wide-only"], ...paid.head],
      t.repos.map((r) => el("tr", {}, cell(r.repo), cell(r.runs, "num"), cell(r.shipped, "num"), cell(r.failed, "num"),
        cell(percent(r.pass_rate), "num"), cell(duration(r.median_seconds), "num"), paid.cell(r))))
      : quiet("No pipeline run finished in this window."),
    heading(5, titles[4]),
    t.evals.length ? table("Mined eval tasks", [["Repo"], ["Tasks", "num c-n"], ["Passed", "num c-n"], ["Pass rate", "num c-rate"], ["Last", "c-time wide-only"]],
      t.evals.map((e) => el("tr", {}, cell(e.repo), cell(e.tasks, "num"), cell(e.passed, "num"), cell(percent(e.pass_rate), "num"), cell(clock(e.last)))))
      : quiet("No eval ran in this window."));
  return SLOW_MS;
}

function percent(rate) {
  return rate === null || rate === undefined ? "" : `${Math.round(rate * 100)}%`;
}
