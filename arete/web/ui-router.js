export const screens = ["floor", "triage", "board", "task", "run", "intake", "outbox", "prompts", "adws", "insights", "settings"];

export function parse(hash) {
  const raw = hash.replace(/^#/, "").split("/");
  const screen = screens.includes(raw[0]) ? raw[0] : "floor";
  const parts = raw.slice(1).map((part) => {
    try { return decodeURIComponent(part); } catch { return part; }
  });
  const path = screen === "floor" ? [] : parts;
  if (screen === "settings" && !path[0]) path[0] = "health";
  const canonical = "#" + [screen, ...path.map(encodeURIComponent)].join("/");
  const active = screen === "task" ? "board" : screen === "run" ? "runs"
    : screen === "board" && path[0] === "open-work" ? "my-work" : screen;
  return { screen, path, hash: canonical, active };
}

export function current() {
  const route = parse(location.hash);
  if (location.hash !== route.hash) history.replaceState(null, "", route.hash);
  return route;
}

export function listen(onRoute) {
  const changed = () => onRoute(current());
  window.addEventListener("hashchange", changed);
  return () => window.removeEventListener("hashchange", changed);
}
