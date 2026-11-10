"use strict";

const GAP_HOLD_MS = 1000;
const SLIDE_MS = 250;
const SLIDE_EASING = "cubic-bezier(0.33, 1, 0.68, 1)";
const ROW_SLIDE = "row-slide";
const REDUCED_MOTION = matchMedia("(prefers-reduced-motion: reduce)");
const CAN_HOVER = matchMedia("(any-hover: hover)");

const heldGaps = new Map();

async function answerPress(btn, place, answer) {
  btn.disabled = true;
  const press = Press.start(place, btn);
  const pressedOn = route;
  try {
    await answer();
  } catch (e) {
    sayAt(place, "fault", e.message);
  }
  const answerLeftTheView = route !== pressedOn;
  if (!answerLeftTheView) await refresh();
  press?.settle();
}

function act(btn, place, path, body, saying, { meanwhile = null } = {}) {
  return answerPress(btn, place, async () => {
    if (meanwhile) sayAt(place, null, meanwhile);
    sayAt(place, "ok", saying(await call(path, body)));
  });
}

function sayAt(place, state, sentence, { focus = true } = {}) {
  lastSaid.set(place, [state, sentence]);
  const press = pressAt(place);
  press?.noteLineSpoke();
  const line = shownSaidLine(place);
  if (!line) return;
  const box = line.closest("[id]");
  const control = press?.controlIn(box);
  const controlWas = control && middleOf(control);
  const standingInAt = line.dataset.standsFor && line.checkVisibility() ? middleOf(line) : null;
  sayIn(line, state, sentence);
  placeSaid(line, [box]);
  if (control) keepNodeAt(control, controlWas);
  else if (standingInAt !== null && line.dataset.standsFor) putLineAt(line, standingInAt);
  if (focus) line.focus();
  if (control) press.noteControlMoved(middleOf(control) - controlWas);
}

class Press {
  static start(place, control) {
    const gap = gapAt(place);
    gap.press = control.closest("tr") ? new Press(place, control, gap) : null;
    const closingMovesThePress = gap.press?.lineAbove || gap.room > 0;
    if (closingMovesThePress) gap.hold();
    else gap.close();
    const line = shownSaidLine(place);
    if (gap.press && line && !line.hidden) {
      const box = line.closest("[id]");
      slideRowsWhile(box, () => placeSaid(line, [box]));
    }
    return gap.press;
  }

  constructor(place, control, gap) {
    const row = control.closest("tr");
    const line = shownSaidLine(place);
    this.gap = gap;
    this.key = control.dataset.focus;
    this.laterKeys = rowKeysFrom(row, "nextElementSibling");
    this.earlierKeys = rowKeysFrom(row, "previousElementSibling");
    this.lineAbove = Boolean(line && !line.hidden && line.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING);
    this.time = performance.now();
    this.controlMovedBy = 0;
    this.lineShowsThisPress = false;
    this.settled = false;
  }

  controlIn(box) {
    return box.querySelector(`[data-focus="${CSS.escape(this.key)}"]`);
  }

  pressPointIn(box) {
    const control = this.controlIn(box);
    return control?.checkVisibility() ? middleOf(control) - this.controlMovedBy : null;
  }

  rowsIn(nodes) {
    const acted = rowHolding(nodes, this.key);
    const later = acted ? null : firstRowHolding(nodes, this.laterKeys);
    return {
      acted,
      neighbour: acted ?? later ?? firstRowHolding(nodes, this.earlierKeys),
      lineGoesBefore: Boolean(later || (acted && this.lineAbove)),
    };
  }

  noteLineSpoke() {
    this.lineShowsThisPress = true;
  }

  noteControlMoved(by) {
    this.controlMovedBy += by;
  }

  settle() {
    this.settled = true;
    if (this.gap.press === this) this.gap.hold();
  }
}

function pressAt(place) {
  return heldGaps.get(place)?.press ?? null;
}

function rowKeysFrom(row, step) {
  const keys = [];
  for (let other = row[step]; other; other = other[step]) {
    if (!other.matches(".said-row")) keys.push(other.querySelector("[data-focus]")?.dataset.focus);
  }
  return keys.filter(Boolean);
}

function rowHolding(nodes, key) {
  return key ? elementsIn(nodes, `[data-focus="${CSS.escape(key)}"]`)[0]?.closest("tr") : null;
}

function firstRowHolding(nodes, keys) {
  for (const key of keys) {
    const row = rowHolding(nodes, key);
    if (row) return row;
  }
  return null;
}

function saidBesideRows(nodes) {
  for (const line of elementsIn(nodes, "p[data-said]")) placeSaid(line, nodes);
  return nodes.filter((node) => !node.closest?.("tr.said-row"));
}

function placeSaid(line, nodes) {
  delete line.dataset.standsFor;
  showGap(line, 0);
  const press = pressAt(line.dataset.said);
  if (!press) return;
  const { acted, neighbour, lineGoesBefore } = press.rowsIn(nodes);
  if (!acted) standInFor(press, line);
  if (line.hidden) return;
  if (neighbour) putBesideRow(line, neighbour, lineGoesBefore);
  showGap(line, press.gap.margin);
}

function standInFor(press, line) {
  if (!press.lineShowsThisPress) sayIn(line, null, "Waiting for the answer.");
  line.dataset.standsFor = press.key;
}

function putBesideRow(line, row, before) {
  const holder = line.closest("tr.said-row") ?? el("tr", { class: "said-row" }, el("td", {}, line));
  holder.cells[0].colSpan = row.cells.length;
  if (before) {
    if (holder.nextElementSibling !== row) row.before(holder);
  } else if (holder.previousElementSibling !== row) row.after(holder);
}

function holdsAPlace(line) {
  return Boolean(line.closest("tr.said-row") || line.dataset.standsFor);
}

function showGap(line, margin) {
  if (margin) line.style.marginTop = `${margin}px`;
  // Clearing marginTop alone leaves style="", and the next redraw's fresh line would then fail isEqualNode.
  else line.removeAttribute("style");
}

function restingTop(line) {
  const margin = line.style.marginTop;
  if (!margin) return line.getBoundingClientRect().top;
  line.style.marginTop = "";
  const top = line.getBoundingClientRect().top;
  line.style.marginTop = margin;
  return top;
}

function marginAbove(line) {
  let above = line.previousElementSibling;
  while (above && !above.checkVisibility()) above = above.previousElementSibling;
  return above ? restingTop(line) - above.getBoundingClientRect().bottom : 0;
}

function slideRowsWhile(box, change) {
  const shownAt = stopRows(box, () => SLIDE_MS);
  change();
  slideRowsFrom(box, shownAt);
}

function stopRowSlides(box) {
  return stopRows(box, (slide) => (slide ? SLIDE_MS - (slide.currentTime ?? 0) : 0));
}

function stopRows(box, slideTimeFor) {
  const shownAt = new Map();
  for (const row of box.querySelectorAll("tr")) {
    const slide = row.getAnimations().find((animation) => animation.id === ROW_SLIDE);
    const slideTime = slideTimeFor(slide);
    if (rowKey(row) && slideTime > 0) shownAt.set(rowKey(row), { bottom: row.getBoundingClientRect().bottom, slideTime });
    slide?.cancel();
  }
  return shownAt;
}

function slideRowsFrom(box, shownAt) {
  if (REDUCED_MOTION.matches) return;
  for (const row of box.querySelectorAll("tr")) {
    const was = shownAt.get(rowKey(row));
    const movedBy = was ? row.getBoundingClientRect().bottom - was.bottom : 0;
    if (Math.abs(movedBy) >= 1) {
      row.animate([{ transform: `translateY(${-movedBy}px)` }, { transform: "none" }], { id: ROW_SLIDE, duration: was.slideTime, easing: SLIDE_EASING });
    }
  }
}

function rowKey(row) {
  return row.querySelector("[data-focus]")?.dataset.focus;
}

function vacatedSpot(box, fresh) {
  const latestFirst = elementsIn(fresh, "p[data-stands-for]")
    .map((line) => ({ line, press: pressAt(line.dataset.said) }))
    .sort((a, b) => b.press.time - a.press.time);
  for (const { line, press } of latestFirst) {
    const pressY = press.pressPointIn(box);
    if (pressY !== null) return { line, pressY };
  }
  return null;
}

function putLineAt(line, y, x = scrollX) {
  const pane = scrollingPane(line);
  const neededScroll = scrolledIn(pane) + middleOf(line) - y;
  const furthest = furthestScrollIn(pane);
  if (neededScroll < 0) gapAt(line.dataset.said).widen(line, -neededScroll);
  else if (neededScroll > furthest) gapAt(line.dataset.said).lengthen(pane, neededScroll - furthest);
  scrollPaneOrPage(pane, Math.max(neededScroll, 0), x);
}

function keepNodeAt(node, y) {
  scrollPaneOrPageBy(scrollingPane(node), middleOf(node) - y);
}

class HeldGap {
  constructor(place) {
    this.place = place;
    this.press = null;
    this.margin = 0;
    this.room = 0;
    this.roomPane = null;
    this.holdTimer = 0;
    this.pointerWatch = null;
    this.slideFrame = 0;
  }

  widen(line, by) {
    // The line's top margin first fills the margin above it that the two collapse into, and only then moves the line.
    this.margin = Math.max(this.margin, marginAbove(line)) + by;
    showGap(line, this.margin);
    this.hold();
  }

  lengthen(pane, by) {
    this.roomPane = pane;
    this.room += by;
    showRoomBelow(pane);
    this.hold();
  }

  hold() {
    this.stopClosing();
    const pressInFlight = this.press && !this.press.settled;
    if ((this.margin || this.room) && !pressInFlight) this.holdTimer = setTimeout(() => this.closeOncePointerClears(), GAP_HOLD_MS);
  }

  stopClosing() {
    clearTimeout(this.holdTimer);
    this.pointerWatch?.abort();
    this.pointerWatch = null;
    cancelAnimationFrame(this.slideFrame);
  }

  closeOncePointerClears() {
    if (!this.wouldSlideUnderPointer()) {
      this.close();
      return;
    }
    this.pointerWatch = new AbortController();
    const closeIfClear = () => {
      if (!this.wouldSlideUnderPointer()) this.close();
    };
    document.addEventListener("pointermove", closeIfClear, { passive: true, signal: this.pointerWatch.signal });
    document.documentElement.addEventListener("pointerleave", closeIfClear, { signal: this.pointerWatch.signal });
  }

  wouldSlideUnderPointer() {
    const line = shownSaidLine(this.place);
    if (!line?.checkVisibility()) return false;
    const mouseMayRestUnseen = !pointerKnown && CAN_HOVER.matches;
    if (mouseMayRestUnseen) return true;
    if (!mouseAt) return false;
    const sliding = scrollingPane(line) ?? line.closest(".page");
    if (!sliding.contains(document.elementFromPoint(mouseAt.x, mouseAt.y))) return false;
    const roomPullsAllBack = this.room > furthestScrollIn(this.roomPane) - scrolledIn(this.roomPane);
    const marginPullsBelowUp = this.margin > 0 && mouseAt.y >= restingTop(line);
    return roomPullsAllBack || marginPullsBelowUp;
  }

  close() {
    this.stopClosing();
    const { margin, room } = this;
    const line = shownSaidLine(this.place);
    const marginAtRest = line?.checkVisibility() ? Math.min(margin, marginAbove(line)) : 0;
    if ((margin <= marginAtRest && !room) || REDUCED_MOTION.matches) {
      this.show(0, 0);
      return;
    }
    let start = null;
    const step = (now) => {
      start ??= now;
      const toGo = (1 - Math.min((now - start) / SLIDE_MS, 1)) ** 3;
      this.show(toGo ? marginAtRest + (margin - marginAtRest) * toGo : 0, room * toGo);
      if (toGo) this.slideFrame = requestAnimationFrame(step);
    };
    this.slideFrame = requestAnimationFrame(step);
  }

  show(margin, room) {
    this.margin = margin;
    this.room = room;
    const line = shownSaidLine(this.place);
    if (line && holdsAPlace(line)) showGap(line, margin);
    showRoomBelow(this.roomPane);
  }
}

function showRoomBelow(pane) {
  const room = [...heldGaps.values()].filter((gap) => gap.roomPane === pane).reduce((sum, gap) => sum + gap.room, 0);
  const holder = pane ?? document.body;
  if (room) holder.style.setProperty("--room-below", `${room}px`);
  else holder.style.removeProperty("--room-below");
}

function gapAt(place) {
  if (!heldGaps.has(place)) heldGaps.set(place, new HeldGap(place));
  return heldGaps.get(place);
}
