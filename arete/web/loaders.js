"use strict";

const LOADERS = {
  live: async () => {
    const data = await call("/api/live");
    return () => drawLive(data);
  },
  runs: async (r) => {
    const q = r.detail ? lastQuery.runs || {} : r.query;
    const [list, detail] = await Promise.all([call(`/api/runs?${runsQuery(q)}`), r.detail ? settle(loadRunDetail(r.detail.id)) : null]);
    return () => Math.min(drawRuns(list, q), r.detail ? drawRunDetail("runs-detail", detail, "run") : SLOW_MS);
  },
  sessions: async (r) => {
    const [list, detail] = await Promise.all([call("/api/sessions"), r.detail ? settle(loadRunDetail(r.detail.id)) : null]);
    return () => Math.min(drawSessions(list), r.detail ? drawRunDetail("sessions-detail", detail, "session") : SLOW_MS);
  },
  loops: async (r) => {
    const [list, detail] = await Promise.all([call("/api/loops"),
      r.detail ? settle(call(`/api/loops/${encodeURIComponent(r.detail.id)}`)) : null]);
    return () => Math.min(drawLoops(list), r.detail ? drawLoopDetail(detail) : SLOW_MS);
  },
  triage: async (r) => {
    const data = await call("/api/triage");
    return () => {
      const editing = sending.size || document.activeElement?.closest("#triage-detail form");
      if (editing && !forceTriageDraw) return HELD;
      forceTriageDraw = false;
      return drawTriage(data, r);
    };
  },
  outcomes: async () => {
    const data = await call("/api/outcomes");
    return () => {
      outcomesData = data;
      return drawOutcomes();
    };
  },
  health: async () => {
    const data = await call("/api/health");
    return () => drawHealth(data);
  },
  settings: async () => {
    const fresh = await call("/api/settings");
    return () => {
      if (settingsData && Object.keys(settingsChanges()).length) return HELD;
      drawSettings(fresh);
      return SLOW_MS;
    };
  },
  usage: async (r) => {
    const data = await call(`/api/usage?days=${r.query.days || DEFAULT_USAGE_WINDOW}`);
    return () => drawUsage(data);
  },
};

function settle(promise) {
  return promise.then((value) => ({ value }), (e) => ({ error: e.message }));
}
