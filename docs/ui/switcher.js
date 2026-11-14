(function () {
  const MAX_Z = "2147483647";

  function errorBox() {
    let box = document.getElementById("proto-error");
    if (box) return box;
    box = document.createElement("div");
    box.id = "proto-error";
    box.setAttribute("role", "alert");
    Object.assign(box.style, {
      position: "fixed", top: "0", left: "0", right: "0", zIndex: MAX_Z, margin: "0", padding: "8px 12px",
      background: "#C8102E", color: "#FFFFFF", font: "13px/1.4 ui-monospace, Menlo, monospace", whiteSpace: "pre-wrap",
      maxHeight: "40vh", overflow: "auto", boxShadow: "0 2px 8px rgba(0,0,0,0.4)",
    });
    (document.body || document.documentElement).appendChild(box);
    return box;
  }

  let clicking = "";

  function reportError(message) {
    const text = String(message || "Unknown error") + (clicking ? " after clicking " + clicking : "");
    const root = document.documentElement;
    if (!root.dataset.protoError) root.dataset.protoError = text;
    const show = () => {
      const line = document.createElement("div");
      line.textContent = "PROTOTYPE ERROR: " + text;
      errorBox().appendChild(line);
    };
    if (document.body) show();
    else document.addEventListener("DOMContentLoaded", show, { once: true });
  }

  window.onerror = function (message, source, line, column, error) {
    const where = source ? " (" + String(source).split("#")[0].split("?")[0].split("/").pop() + ":" + line + ":" + column + ")" : "";
    reportError((error && error.stack ? String(error.message || message) : String(message)) + where);
    return false;
  };
  window.addEventListener("unhandledrejection", function (event) {
    const reason = event.reason;
    reportError("Unhandled rejection: " + (reason && reason.message ? reason.message : String(reason)));
  });

  const here = decodeURIComponent(location.pathname.split("/").pop() || "");

  function manifest() {
    const round = window.PROTO_ROUND || null;
    const variants = round && Array.isArray(round.variants) ? round.variants : [];
    const index = variants.findIndex(v => v.file === here);
    return { round, variants, index, current: index >= 0 ? variants[index] : null };
  }

  function screenLabel() {
    const { round } = manifest();
    const hash = location.hash.replace(/^#/, "");
    if (!hash) return "#" + (round && round.screens ? round.screens[0] : "floor");
    const slash = hash.indexOf("/");
    return slash < 0 ? "#" + hash : "#" + hash.slice(0, slash) + " / " + hash.slice(slash + 1);
  }

  function go(step) {
    const { variants, index } = manifest();
    if (!variants.length) return;
    const from = index >= 0 ? index : 0;
    const next = variants[(from + step + variants.length) % variants.length];
    location.href = encodeURI(next.file) + location.hash;
  }

  function typing(target) {
    if (!target || !target.tagName) return false;
    const tag = target.tagName.toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
  }

  document.addEventListener("keydown", function (event) {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (typing(document.activeElement) || typing(event.target)) return;
    if (event.key === "ArrowLeft") { event.preventDefault(); go(-1); }
    if (event.key === "ArrowRight") { event.preventDefault(); go(1); }
  });

  function button(text, title, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = text;
    b.title = title;
    Object.assign(b.style, {
      font: "600 13px/1 -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif", color: "#000000",
      background: "#F2F2F2", border: "1px solid #C8C8C8", borderRadius: "999px", width: "26px", height: "24px",
      padding: "0", margin: "0", cursor: "pointer", transition: "none",
    });
    b.addEventListener("click", onClick);
    return b;
  }

  function mount() {
    const { round, current } = manifest();
    const pill = document.createElement("div");
    pill.id = "proto-switcher";
    pill.setAttribute("aria-label", "Prototype switcher");
    Object.assign(pill.style, {
      position: "fixed", left: "50%", bottom: "12px", transform: "translateX(-50%)", zIndex: MAX_Z,
      display: "flex", alignItems: "center", gap: "8px", padding: "4px 6px", whiteSpace: "nowrap",
      background: "#FFFFFF", color: "#000000", borderRadius: "999px", boxShadow: "0 2px 10px rgba(0,0,0,0.35)",
      font: "12px/1.2 -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif",
      letterSpacing: "normal", textTransform: "none", transition: "none",
    });

    const tag = document.createElement("span");
    tag.textContent = "PROTOTYPE";
    Object.assign(tag.style, { font: "700 10px/1 -apple-system, BlinkMacSystemFont, sans-serif", letterSpacing: "0.08em", background: "#000000", color: "#FFFFFF", padding: "4px 6px", borderRadius: "999px" });

    const label = document.createElement("span");
    const name = current ? current.key + " · " + current.name : (here || "page") + " (not in manifest)";
    const roundText = round ? "Round " + round.round : "no manifest";
    const screen = document.createElement("strong");
    const refresh = () => { screen.textContent = screenLabel(); };
    refresh();
    window.addEventListener("hashchange", refresh);
    label.append(roundText + " · " + name + " · ", screen);

    const compare = document.createElement("a");
    compare.href = "index.html";
    compare.textContent = "shots";
    Object.assign(compare.style, { color: "#0000EE", textDecoration: "underline", font: "inherit" });

    pill.append(tag, button("‹", "Previous variant (left arrow)", () => go(-1)), label, button("›", "Next variant (right arrow)", () => go(1)), compare);
    document.body.appendChild(pill);
  }

  const CLICKABLE = '[data-act], [onclick], button, a[href^="#"], [data-go], [data-trow], [data-item], [data-node], [data-pin], [data-wire]';

  function describe(el) {
    const tag = el.tagName.toLowerCase();
    const act = el.dataset && el.dataset.act ? "[" + el.dataset.act + "]" : "";
    const text = (el.textContent || el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim().slice(0, 40);
    return tag + act + (text ? " \"" + text + "\"" : "");
  }

  function clickable() {
    return [...document.querySelectorAll(CLICKABLE)].filter(el => {
      if (el.closest("#proto-switcher, #proto-error")) return false;
      const href = el.getAttribute("href");
      if (el.tagName === "A" && href && !href.startsWith("#")) return false;
      return el.getClientRects().length > 0;
    });
  }

  function clickSweep() {
    const start = location.hash;
    const reset = () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
      if (location.hash !== start) {
        location.hash = start;
        window.dispatchEvent(new HashChangeEvent("hashchange"));
      }
    };
    let clicked = 0;
    for (let i = 0; i < 400; i++) {
      const els = clickable();
      if (i >= els.length) break;
      const el = els[i];
      clicking = describe(el);
      const init = { bubbles: true, cancelable: true, view: window };
      el.dispatchEvent(new MouseEvent("mousedown", init));
      el.dispatchEvent(new MouseEvent("mouseup", init));
      el.dispatchEvent(new MouseEvent("click", init));
      clicked++;
      clicking = describe(el) + ", then Escape and back to " + (start || "#");
      reset();
      clicking = "";
    }
    document.documentElement.dataset.clicks = String(clicked);
  }

  function center(el) {
    const r = el.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + r.height / 2];
  }

  function pointer(type, x, y, target, extra) {
    const el = target || document.elementFromPoint(x, y) || document.body;
    const init = Object.assign({ bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, button: 0, buttons: type.endsWith("up") ? 0 : 1, pointerId: 1, pointerType: "mouse", isPrimary: true }, extra || {});
    el.dispatchEvent(new PointerEvent("pointer" + type, init));
    el.dispatchEvent(new MouseEvent("mouse" + type, init));
    return el;
  }

  function replayDrag(spec) {
    spec.split("|").forEach(one => {
      const at = one.lastIndexOf(">");
      const fromSel = one.slice(0, at), toSpec = one.slice(at + 1);
      const from = document.querySelector(fromSel);
      if (!from) { reportError("drag replay: nothing matches " + fromSel); return; }
      const [x, y] = center(from);
      const off = /^([+-]?\d+),([+-]?\d+)$/.exec(toSpec);
      let tx, ty;
      if (off) { tx = x + Number(off[1]); ty = y + Number(off[2]); }
      else {
        const to = document.querySelector(toSpec);
        if (!to) { reportError("drag replay: nothing matches " + toSpec); return; }
        [tx, ty] = center(to);
      }
      clicking = "drag " + fromSel + " to " + toSpec;
      pointer("down", x, y, from);
      for (let i = 1; i <= 6; i++) pointer("move", x + (tx - x) * i / 6, y + (ty - y) * i / 6);
      pointer("up", tx, ty);
      clicking = "";
    });
  }

  function replayWheel(spec) {
    spec.split("|").forEach(one => {
      const m = /^(.*),([+-]?\d+),([+-]?\d+)(,ctrl)?$/.exec(one);
      if (!m) { reportError("wheel replay: use selector,dx,dy or selector,dx,dy,ctrl, not " + one); return; }
      const el = document.querySelector(m[1]);
      if (!el) { reportError("wheel replay: nothing matches " + m[1]); return; }
      const [x, y] = center(el);
      clicking = "wheel on " + m[1];
      el.dispatchEvent(new WheelEvent("wheel", { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, deltaX: Number(m[2]), deltaY: Number(m[3]), deltaMode: 0, ctrlKey: !!m[4] }));
      clicking = "";
    });
  }

  function param(name) {
    const m = new RegExp("[?&]" + name + "=([^&#]*)").exec(location.search);
    return m ? decodeURIComponent(m[1]) : null;
  }

  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount, { once: true });
  window.addEventListener("load", () => {
    const wheel = param("wheel"), drag = param("drag");
    if (wheel) setTimeout(() => replayWheel(wheel), 500);
    if (drag) setTimeout(() => replayDrag(drag), 600);
    if (/[?&]clicks\b/.test(location.search)) setTimeout(clickSweep, 800);
  }, { once: true });
})();
