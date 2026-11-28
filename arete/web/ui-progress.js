export function ticketProgressParts(progress, { element, link, stamp, pullLinks }) {
  const { activity, blocker, next_event, follow_up, prerequisites, prerequisite_issues, updates, timing, since, spec_graph } = progress;
  const unread = Object.entries(prerequisite_issues ?? {}).filter(([, read]) => !read).map(([ref]) => ref);
  const p = (...children) => element("p", {}, ...children);
  return [
    p(activity, since ? element("small", {}, ` · since ${stamp(since)}`) : null),
    blocker ? p(element("strong", {}, "Blocker:"), ` ${blocker}`) : null,
    next_event ? p(element("strong", {}, "Next:"), ` ${next_event}`) : null,
    timing ? element("section", { "aria-label": "Run lineage timing" }, timing.lines.map((line) => p(line))) : null,
    spec_graph?.children?.length ? element("ul", { "aria-label": "Spec graph children" }, spec_graph.children.map((child) => {
      const label = child.ticket ? `${child.ticket} (${child.key})` : child.key;
      const wait = child.wait_reason ? ` — ${child.wait_reason}` : "";
      return element("li", {}, `${label}: ${child.state}${wait}`);
    })) : null,
    prerequisites?.length ? element("ul", {}, prerequisites.map((pr) => {
      const label = pr.deployed_release ? "Deployed prerequisite" : pr.state === "merged"
        ? "Merged prerequisite (deployment not confirmed)" : `Prerequisite (${pr.state})`;
      return element("li", {}, `${label}: `, link(pr.url, pr.key));
    })) : null,
    unread.length ? p(`Prerequisite lookup not confirmed: ${unread.join(", ")}`) : null,
    follow_up?.prs?.length ? p(element("strong", {}, follow_up.draft ? "Draft follow-up:" : "Follow-up PR:"),
      " ", pullLinks(follow_up.prs)) : null,
    updates?.length ? element("ul", { "aria-label": "Recent ticket updates" }, updates.map((update) =>
      element("li", {}, element("small", {}, stamp(update.at)), ` ${update.message}`))) : null,
  ];
}
