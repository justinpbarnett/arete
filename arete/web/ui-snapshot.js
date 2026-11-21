export function cursor(value) {
  const match = /^(\d+)\.(\d+)$/.exec(value ?? "");
  return match ? [BigInt(match[1]), BigInt(match[2])] : null;
}

export function newer(value, previous) {
  const next = cursor(value);
  const last = cursor(previous);
  return Boolean(next && (!last || next[0] > last[0] || next[0] === last[0] && next[1] > last[1]));
}

export function sequences(value) {
  if (typeof value === "function") return value.seqs ?? [];
  if (!value || typeof value !== "object") return [];
  if (Array.isArray(value)) return value.flatMap(sequences);
  return [...(cursor(value.seq) ? [value.seq] : []),
    ...Object.entries(value).filter(([key]) => key !== "seq").flatMap(([, child]) => sequences(child))];
}

export async function coherent(readers, select = sequences) {
  let values = await Promise.all(readers.map((read) => read()));
  for (let round = 0; round <= 2; round++) {
    if (values.some((value) => value?.error)) return null;
    const groups = values.map(select);
    const seqs = groups.flat();
    if (!seqs.length) return null;
    const parsed = seqs.map(cursor);
    if (parsed.some((value) => !value)) return null;
    const epoch = parsed[0][0];
    const sameEpoch = parsed.every(([e]) => e === epoch);
    const newest = parsed.reduce((a, b) => a[1] > b[1] ? a : b)[1];
    if (sameEpoch && parsed.every(([, n]) => n === newest)
        && groups.every((group, i) => group.length || values[i] == null)) return values;
    if (round === 2) return null;
    values = await Promise.all(values.map((value, i) => !sameEpoch || !groups[i].length
      || groups[i].some((seq) => cursor(seq)[1] < newest) ? readers[i]() : value));
  }
  return null;
}
