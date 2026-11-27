"use strict";

async function loadScreen(view, fallback, id = "") {
  const { screen } = await import("./ui-data.js");
  try {
    const data = await call(screen(view, id));
    return { ...data.view, seq: data.seq };
  } catch (error) {
    if (error.status !== 404) throw error;
    return call(fallback);
  }
}

async function ticketProgressRenderer() {
  const { ticketProgressParts } = await import("./ui-progress.js");
  return (progress) => ticketProgressParts(progress, {
    element: el, link, stamp: zonedClock, pullLinks: (urls) => pullLinks(urls.join(" ")),
  });
}

const LOADERS = {
  live: async () => {
    const data = await loadScreen("live", "/api/live");
    const renderProgress = data.ticket_progress && await ticketProgressRenderer();
    return staged(() => drawLive(data, renderProgress), data);
  },
  runs: async (r) => {
    const q = r.detail ? lastQuery.runs || {} : r.query;
    const [list, detail] = await Promise.all([call(`/api/runs?${runsQuery(q)}`), r.detail ? settle(loadRunDetail(r.detail.id)) : null]);
    return staged(() => Math.min(drawRuns(list, q), r.detail ? drawRunDetail("runs-detail", detail, "run") : SLOW_MS), list, detail);
  },
  sessions: async (r) => {
    const [list, detail] = await Promise.all([call("/api/sessions"), r.detail ? settle(loadRunDetail(r.detail.id)) : null]);
    return staged(() => Math.min(drawSessions(list), r.detail ? drawRunDetail("sessions-detail", detail, "session") : SLOW_MS), list, detail);
  },
  loops: async (r) => {
    const [list, detail] = await Promise.all([call("/api/loops"),
      r.detail ? settle(call(`/api/loops/${encodeURIComponent(r.detail.id)}`)) : null]);
    return staged(() => Math.min(drawLoops(list), r.detail ? drawLoopDetail(detail) : SLOW_MS), list, detail);
  },
  triage: async (r) => {
    const data = await call("/api/triage");
    return staged(() => {
      const editing = sending.size || document.activeElement?.closest("#triage-detail form");
      if (editing && !forceTriageDraw) return HELD;
      forceTriageDraw = false;
      return drawTriage(data, r);
    }, data);
  },
  outcomes: async () => {
    const data = await call("/api/outcomes");
    return staged(() => {
      outcomesData = data;
      return drawOutcomes();
    }, data);
  },
  health: async () => {
    const data = await call("/api/health");
    return staged(() => drawHealth(data), data);
  },
  incidents: async (r) => {
    const [list, detail] = await Promise.all([call("/api/incidents"),
      r.detail ? settle(call(`/api/incidents/${encodeURIComponent(r.detail.id)}`)) : null]);
    return staged(() => Math.min(drawIncidents(list), r.detail ? drawIncidentDetail(detail) : SLOW_MS), list, detail);
  },
  settings: async () => {
    const [fresh, push] = await Promise.all([call("/api/settings"), call("/api/push").catch(() => null)]);
    return staged(() => {
      if (settingsData && Object.keys(settingsChanges()).length) return HELD;
      if (push) drawPush(push);
      drawSettings(fresh);
      return SLOW_MS;
    }, fresh, push);
  },
  usage: async (r) => {
    const data = await call(`/api/usage?days=${r.query.days || DEFAULT_USAGE_WINDOW}`);
    return staged(() => drawUsage(data), data);
  },
};

async function staged(draw, ...parts) {
  const { sequences } = await import("./ui-snapshot.js");
  draw.seqs = sequences(parts);
  return draw;
}

function settle(promise) {
  return promise.then((value) => ({ value }), (e) => ({ error: e.message, seq: e.answer?.seq }));
}
