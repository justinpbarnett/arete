export function fuzzy(rows, query = "") {
  const needle = query.trim().toLowerCase();
  const groups = new Map();
  for (const row of rows) {
    const title = row.title.toLowerCase();
    let at = 0;
    for (const letter of title) if (letter === needle[at]) at += 1;
    if (at !== needle.length) continue;
    const score = !needle || title === needle ? 0 : title.includes(needle) ? 1 : 2;
    const group = row.group ?? "";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push({ row, score });
  }
  return [...groups].map(([group, matches]) => [group, matches.sort((a, b) => a.score - b.score).map(({ row }) => row)]);
}
