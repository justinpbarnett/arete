"use strict";

const MARKS = "[data-focus], h2, h3";
const handlersOf = new WeakMap();

// A redraw that changes nothing keeps the shown node and hands it the fresh handlers,
// so a handler must reach its node through the event, never through a closure.
function listen(node, handlers) {
  handlersOf.set(node, handlers);
  for (const event of Object.keys(handlers)) node.addEventListener(event, (e) => handlersOf.get(node)[event](e));
}

function asNode(value) {
  return value instanceof Node ? value : document.createTextNode(String(value));
}

function elementsIn(nodes, selector = "*") {
  return nodes.flatMap((node) => (node instanceof Element ? [...(node.matches(selector) ? [node] : []), ...node.querySelectorAll(selector)] : []));
}

function tickLiveAgos(times) {
  for (const time of times) {
    const text = ago(time.dateTime);
    if (time.textContent !== text) time.textContent = text;
  }
}

function listenedEvents(node) {
  return Object.keys(handlersOf.get(node) || {}).join();
}

function sameLiveState(shownNode, freshNode) {
  return shownNode.value === freshNode.value && shownNode.checked === freshNode.checked
    && listenedEvents(shownNode) === listenedEvents(freshNode);
}

function keepShownIfUnchanged(shown, fresh) {
  if (shown.length !== fresh.length || shown.some((node, i) => !node.isEqualNode(fresh[i]))) return false;
  const shownElements = elementsIn(shown);
  const freshElements = elementsIn(fresh);
  if (!shownElements.every((node, i) => sameLiveState(node, freshElements[i]))) return false;
  shownElements.forEach((node, i) => {
    if (handlersOf.has(freshElements[i])) handlersOf.set(node, handlersOf.get(freshElements[i]));
  });
  return true;
}

function placeKey(node) {
  return node.dataset.focus || node.textContent;
}

class Marks {
  constructor(nodes) {
    this.nodes = elementsIn(nodes, MARKS);
    const seen = new Map();
    this.ids = this.nodes.map((node) => {
      const key = placeKey(node);
      seen.set(key, (seen.get(key) ?? -1) + 1);
      return `${key}#${seen.get(key)}`;
    });
  }

  idOf(node) {
    return this.ids[this.nodes.indexOf(node)];
  }

  nodeOf(id) {
    return this.nodes[this.ids.indexOf(id)];
  }
}

function idsKeptInOrder(before, after) {
  let start = 0;
  while (start < before.length && start < after.length && before[start] === after[start]) start++;
  let beforeEnd = before.length;
  let afterEnd = after.length;
  while (beforeEnd > start && afterEnd > start && before[beforeEnd - 1] === after[afterEnd - 1]) {
    beforeEnd--;
    afterEnd--;
  }
  const kept = new Set([...before.slice(0, start), ...before.slice(beforeEnd)]);
  const changedBefore = before.slice(start, beforeEnd);
  const changedAfter = after.slice(start, afterEnd);
  const width = changedAfter.length + 1;
  const longestFrom = new Uint32Array((changedBefore.length + 1) * width);
  for (let i = changedBefore.length - 1; i >= 0; i--) {
    for (let j = changedAfter.length - 1; j >= 0; j--) {
      longestFrom[i * width + j] = changedBefore[i] === changedAfter[j]
        ? longestFrom[(i + 1) * width + j + 1] + 1
        : Math.max(longestFrom[(i + 1) * width + j], longestFrom[i * width + j + 1]);
    }
  }
  for (let i = 0, j = 0; i < changedBefore.length && j < changedAfter.length;) {
    if (changedBefore[i] === changedAfter[j]) {
      kept.add(changedBefore[i]);
      i++;
      j++;
    } else if (longestFrom[(i + 1) * width + j] >= longestFrom[i * width + j + 1]) i++;
    else j++;
  }
  return kept;
}

function markHolding(node) {
  const element = node?.nodeType === Node.TEXT_NODE ? node.parentElement : node;
  return element?.closest?.(MARKS) ?? null;
}

function marksInUse() {
  const selection = getSelection();
  return [document.activeElement, selection.isCollapsed ? null : selection.anchorNode].map(markHolding).filter(Boolean);
}

function middleOf(node) {
  const { top, bottom } = node.getBoundingClientRect();
  return (top + bottom) / 2;
}

function scrollingPane(node) {
  const pane = node.closest(".detail");
  return pane && getComputedStyle(pane).overflowY === "auto" ? pane : null;
}

function placeIn(box, marks, fresh) {
  const pane = scrollingPane(box);
  const section = box.closest("main > section");
  if (!section) return { pane: null, x: scrollX, y: scrollY, anchors: [], vacated: null };
  const scope = pane ?? section;
  const panes = pane ? [] : [...section.querySelectorAll(".detail")].filter((p) => getComputedStyle(p).overflowY === "auto");
  const { top: shownTop, bottom: shownBottom } = pane ? pane.getBoundingClientRect() : { top: 0, bottom: innerHeight };
  const y = scrolledIn(pane);
  const laidOut = [...scope.querySelectorAll(MARKS)]
    .filter((node) => !panes.some((p) => p.contains(node)) && node.checkVisibility())
    .map((node) => {
      const { top, bottom } = node.getBoundingClientRect();
      return { node, top, bottom };
    });
  const inUse = marksInUse();
  const hovered = mouseAt && document.elementFromPoint(mouseAt.x, mouseAt.y);
  const hovering = Boolean(hovered && scope.contains(hovered) && !panes.some((p) => p.contains(hovered)));
  const fromMouse = ({ top, bottom }) => Math.abs((top + bottom) / 2 - mouseAt.y);
  const inView = laidOut.filter(({ top, bottom }) => bottom > shownTop && top < shownBottom)
    .sort(hovering ? (a, b) => fromMouse(a) - fromMouse(b) : (a, b) => inUse.includes(b.node) - inUse.includes(a.node));
  const anchors = !y && !hovering ? [] : inView.length ? inView : laidOut.filter(({ bottom }) => bottom <= shownTop).slice(-1);
  const vacated = vacatedSpot(box, fresh);
  return {
    pane, x: scrollX, y,
    anchors: anchors.map(({ node, top }) => ({ node, id: box.contains(node) ? marks.idOf(node) : null, top })),
    vacated: vacated && vacated.pressY > shownTop && vacated.pressY < shownBottom ? vacated : null,
  };
}

function followAnchor(anchor, freshMarks, keptIds) {
  const node = anchor.id ? freshMarks.nodeOf(anchor.id) : anchor.node.isConnected ? anchor.node : null;
  if (!node?.checkVisibility()) return null;
  return { drift: node.getBoundingClientRect().top - anchor.top, stayed: !anchor.id || keptIds.has(anchor.id) };
}

function followAnchors(place, shownMarks, freshMarks) {
  const keptIds = idsKeptInOrder(shownMarks.ids, freshMarks.ids);
  const followed = place.anchors.map((anchor) => followAnchor(anchor, freshMarks, keptIds)).filter(Boolean);
  return followed.find(({ stayed }) => stayed) ?? followed[0];
}

function scrolledIn(pane) {
  return pane ? pane.scrollTop : scrollY;
}

function scrollPaneOrPage(pane, y, x = scrollX) {
  if (pane) {
    if (pane.scrollTop !== y) pane.scrollTop = y;
  } else if (scrollX !== x || scrollY !== y) scrollTo(x, y);
}

function scrollPaneOrPageBy(pane, by, x = scrollX) {
  scrollPaneOrPage(pane, scrolledIn(pane) + by, x);
}

function keepPlace(place, shownMarks, freshMarks) {
  const { line, pressY } = place.vacated ?? {};
  if (line?.checkVisibility()) {
    putLineAt(line, pressY, place.x);
    return;
  }
  const anchor = followAnchors(place, shownMarks, freshMarks);
  if (anchor) scrollPaneOrPageBy(place.pane, anchor.drift, place.x);
  else scrollPaneOrPage(place.pane, place.y, place.x);
}

function textPoint(box, marks, node, offset) {
  const point = document.createRange();
  point.setStart(node, offset);
  const mark = marks.nodes.findLast((m) => point.comparePoint(m, 0) <= 0);
  const before = document.createRange();
  before.setStart(mark ?? box, 0);
  before.setEnd(node, offset);
  return { id: mark ? marks.idOf(mark) : null, chars: before.toString().length };
}

function textPointNode(box, marks, point) {
  const from = point.id ? marks.nodeOf(point.id) : box;
  if (!from) return null;
  const texts = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
  texts.currentNode = from;
  let left = point.chars;
  for (let text = texts.nextNode(); text; text = texts.nextNode()) {
    if (left <= text.length) return [text, left];
    left -= text.length;
  }
  return null;
}

function selectionIn(box, marks) {
  const selection = getSelection();
  if (selection.isCollapsed || !box.contains(selection.anchorNode) || !box.contains(selection.focusNode)) return null;
  return {
    text: selection.toString(),
    anchor: textPoint(box, marks, selection.anchorNode, selection.anchorOffset),
    focus: textPoint(box, marks, selection.focusNode, selection.focusOffset),
  };
}

function keepSelection(box, kept, marks) {
  const anchor = kept && textPointNode(box, marks, kept.anchor);
  const focus = kept && textPointNode(box, marks, kept.focus);
  if (!anchor || !focus) return;
  const selection = getSelection();
  selection.setBaseAndExtent(...anchor, ...focus);
  if (selection.toString() !== kept.text) selection.removeAllRanges();
}

function caretIn(field) {
  try {
    return field?.matches("input, textarea") && field.selectionStart !== null
      ? [field.selectionStart, field.selectionEnd, field.selectionDirection] : null;
  } catch {
    return null;
  }
}

function keepCaret(field, caret) {
  try {
    if (caret && field?.matches("input, textarea")) field.setSelectionRange(...caret);
  } catch {
    // a field type without a caret, such as a number, keeps whatever the browser gave it
  }
}

function scrollersByPlace(box) {
  const found = new Map();
  let under = "";
  for (const node of box.querySelectorAll("h2, h3, summary, .wide, .stack, pre")) {
    if (!node.matches(".wide, .stack, pre")) {
      under = placeKey(node);
      continue;
    }
    let place = `${under}|${node.className}`;
    while (found.has(place)) place += "+";
    found.set(place, node);
  }
  return found;
}

function fill(id, ...nodes) {
  const box = document.getElementById(id);
  const fresh = saidBesideRows(nodes.flat(Infinity).filter(Boolean).map(asNode));
  const shown = [...box.childNodes];
  tickLiveAgos(elementsIn([...shown, ...fresh], "time.ago"));
  if (keepShownIfUnchanged(shown, fresh)) return;
  const rowSlides = stopRowSlides(box);
  const shownMarks = new Marks(shown);
  const place = placeIn(box, shownMarks, fresh);
  const focusedId = shownMarks.idOf(document.activeElement);
  const caret = focusedId ? caretIn(document.activeElement) : null;
  const selected = selectionIn(box, shownMarks);
  const scrolled = [...scrollersByPlace(box)].filter(([, node]) => node.scrollLeft || node.scrollTop)
    .map(([key, node]) => [key, node.scrollLeft, node.scrollTop]);
  box.append(...fresh);
  const freshMarks = new Marks(fresh);
  // Focus moves to the copy while the old element is still there, so a keyboard focus ring carries over.
  const refocused = freshMarks.nodeOf(focusedId);
  refocused?.focus({ preventScroll: true });
  keepCaret(refocused, caret);
  for (const node of shown) node.remove();
  const rebuilt = scrolled.length ? scrollersByPlace(box) : new Map();
  for (const [key, left, top] of scrolled) rebuilt.get(key)?.scrollTo(left, top);
  keepSelection(box, selected, freshMarks);
  keepPlace(place, shownMarks, freshMarks);
  slideRowsFrom(box, rowSlides);
}
