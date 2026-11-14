# Round 4 spec

Round 4 of the Arete dashboard prototypes is one page, `final.html`, meant to be the last round before the port to Arete's real web framework. Builders, reviewers and Justin read this file. It outranks the round 3 spec where they differ.

## Where it starts

`seams.py compose` built `final.html` from `round-03/base.html` with the Floor seam of round 3 C and the ADW canvas seam of round 3 A. Every other screen is the round 3 base, which Justin accepted without changes. With one page there are no seams to keep apart any more: edit any part of the file, but leave the screens below alone unless this spec names them.

## Justin's round 3 calls (2026-11-14)

- **Floor: C, as built.** "I actually like C the best. Only having the waterfall tiny in the lines list and then showing more detail in the side detail panel." The lines table keeps its tiny 14 day chart in each row and the detail pane keeps the selected line's 24 hour run chart. Do not change the Floor.
- **ADWs: A's Blueprint canvas, with four changes.** "I should be able to sideways (or free-form) scroll on the graph. I like option 'A' the best where it looks like unreal's blueprint system. Data nodes able to go in and out of each item. I like the minimap too. When I click on an element, the side details panel should show the info relevant for that as well as editable parameters for it (like what prompt to use, referenced from prompt list)."
- **Step picker: keep it.** "I like being able to fuzzy find options." The prompt picker below works the same way.
- **Clicks must work.** Round 3 D threw "adwCanvasGoNode is not defined" on click, because inline `onclick` handlers cannot reach functions inside the page's wrapper function. Every click in this page goes through the page's delegated `data-act` click handler or a listener added inside the wrapper. No inline `onclick`.

## The ADW canvas

The canvas is the focus pane's center on `#adws`. It draws the selected ADW from `MOCK.adws.items[i]` (`nodes`, `edges`, with each node's ports from its library entry in `MOCK.adws.library`).

### Free pan and zoom

- The graph lays out at its natural size, nodes at their `x` and `y` from the mock (A's layout), and is never squeezed to fit the pane. The "5 more" and "10 more" paging buttons go.
- **Pan in both directions**: two finger trackpad scroll and the mouse wheel pan the view (deltaX sideways, deltaY up and down; Shift with a mouse wheel pans sideways). Dragging on empty canvas pans it, with a grab cursor while dragging. Panning is a transform on an inner layer behind `overflow: hidden`, so the page never scrolls sideways and `data-wide` stays empty.
- **Zoom**: pinch (a wheel event with `ctrlKey`) and Ctrl with the mouse wheel zoom around the pointer, from 40% to 150%. Fit may zoom below 40% to show a wide graph, down to 25%; a graph too wide for that starts at the left edge of the pane (the 17 node Feature graph needs 14% to fit whole, so it fills 25% from the left), while wheel and pinch zoom stay between 40% and 150%. The zoom readout shows the fitted value. The opening view fits the first three columns of the graph, between 50% and 100% (about 85% at 1600, about 50% at 1280), so Plan and its pins show. A small zoom readout sits in a corner of the canvas ("100%"); clicking it fits the graph.
- **Keys**: `=` fits the whole graph in view; `zz` centers the selected node at 100%. When `h`, `j`, `k` or `l` moves the selection off screen, the view pans just enough to show it, as in A.
- **Opening view**: an ADW opens at the fitted zoom above with its triggers at the left edge and the graph's vertical middle in the middle of the pane. `#adws/<adw>/<node>` opens with that node centered.
- Do not use `setPointerCapture`. Listen on `window` for `pointermove` and `pointerup` while a drag runs, so the test hooks can drive it.
- The canvas element carries `data-view="x,y,zoom"` (rounded) after every pan or zoom, so a headless check can read it.

### Minimap

- Bottom right of the canvas, about 200 by 120 px, over a solid panel: the whole graph small, nodes as boxes (a needs you node in yellow, a faulted one in red, the selected one in strong ink, the rest gray), and the current view as a rectangle.
- Click in the minimap to center the view there. Drag the view rectangle to pan.
- The minimap is two dimensional and keeps the graph's aspect ratio, unlike A's flat strip.

### Nodes and pins

- Every node shows **all** its pins from its library entry, not only the wired ones. Inputs on the left edge, outputs on the right, as in Unreal Engine Blueprints.
- **Flow pins** are ▶ (an `in` on the left; `done`, `pass`, `fail`, `feedback` outcomes on the right). **Data pins** are ● with the type glyph and word from `MOCK.adws.portTypes` (for example `≡ plan`, `± diff`, `⋔ work tree`). A data pin that is not wired is a hollow ○.
- An input data pin with a fixed value from `config.inputs` shows the value as a small inline chip on the node beside the pin, as Blueprint shows literals.
- Trigger nodes stay event nodes at the far left with their own header ("◆ ON intake accepted") and only output pins.
- Wires: pass, fail, retry and data wires differ by line style and label, never by color alone (A's styles and labelled U shaped back wires stay). Data wires run pin to pin between data pins of the same type.

### Selecting by click

Every element of the canvas is clickable, and clicking selects it:

| Click on | Selects | Marked by |
|---|---|---|
| a node's header or body | the node | strong ink border |
| a pin | the pin, on its node | strong ink ring on the pin |
| a wire (a wide invisible hit path over it) | the wire | thicker stroke in strong ink |
| a trigger node | the trigger | strong ink border |
| empty canvas (a click without a drag) | nothing, the ADW itself | |

Clicking a node changes the hash to `#adws/<adw>/<node>` as Enter does. Pins and wires add a third part: `#adws/<adw>/<node>/pin/<port>` and `#adws/<adw>/wire/<edge id>`. Elements carry `data-node`, `data-pin` and `data-wire` attributes so the click sweep reaches them. Yellow stays for needs you and red for faults; selection is never colored.

### Making and removing wires

- Drag from an output pin to an input pin of the same type to wire them. While dragging, a wire follows the pointer, pins that take the type get a strong ring, and the rest dim. A flow output goes to a flow `in`. Dropping on a pin of the wrong type does nothing and the notice says why ("plan does not take a diff").
- Dropping a data or flow drag on empty canvas opens the step picker filtered to steps with a matching input, and places the chosen step there, wired, as Blueprint does.
- Dragging from a wired input pin lifts its wire off; dropping it on empty canvas removes it.
- Keys: with a pin selected, `c` opens a picker of the pins it can wire to (fuzzy find, as the step picker). With a wire selected, `dd` removes it at once with u to undo.

## The right panel

The right panel shows the selected element. Every field that can change is editable in place: `i` or a click puts the caret in it (INSERT), Cmd+Enter or leaving the field keeps the value. Each change is a draft.

| Selected | The panel shows |
|---|---|
| nothing | The ADW, as now: Triggers, Versions, Stats, and the ask an agent chat with the pending proposal |
| a step node | Name (editable), kind and library step with its about line, **Prompt** (see below), Model (a menu of the routing roles in `M.settings.modelRouting.roles`, with the role default named), Tries, Timeout in minutes, Run when (a condition, empty means always), Normal duration from `MOCK.stepBands`, then **Inputs**: one row per input pin with its type glyph and word and its source, either a wire from an upstream pin (a link that selects that wire) or a fixed value (editable), or "not wired" with `c wire it`. Then **Outputs**: one row per output pin and where it goes. Then the node's last 7 days (runs, pass rate, median), and Remove node (dd) |
| a trigger node | Trigger kind with its glyph and word, its source, each param from its library entry (editable, with the hint as placeholder, such as Jira project key or a cron schedule), its output pins, runs in the last 7 days, and Remove trigger (dd) |
| a pin | Node and pin name, in or out, type glyph and word, what it connects to (each a link that selects that wire), for an input its fixed value (editable) when it has no wire, and the pins it could wire to |
| a wire | Kind (pass, fail, retry or data), from node and pin to node and pin (each a link that selects it), the data type for a data wire, the label, and for a retry wire the number of tries (editable). Remove wire (dd) |

### The prompt picker

- The Prompt field shows the chosen prompt file's path (for example `arete/prompts/build.md`) with its version, or "none, a script step" for a script step.
- Enter or a click on the field opens the prompt picker: the same palette as the step picker, listing every prompt file from `MOCK.prompts.tree`, grouped by its group, with fuzzy find on the path and the file's description. Ctrl+N and Ctrl+P move, Enter picks, Esc closes.
- `gx` on the field opens that prompt on the Prompts screen.
- Under the field, the first three lines of the chosen prompt as a muted preview.

### Drafts and versions

- Any change on the canvas or in the panel (a field, a wire, a node added or removed) makes a draft of the ADW. The page head then reads "ADW live v14 · draft v15 · 3 changes" in yellow, because the draft waits on Justin.
- Cmd+Enter with the canvas or the panel focused (and not in a field) publishes the draft as the new live version: the notice reads "Published feature v15 · u to undo" and Versions gains the row. The palette has "Discard draft".

## Keys on the ADWs screen

As in `keymap.md`, plus `=` (fit the graph) and `zz` (center the selection) on the canvas. The key bar lists them when the canvas has focus. Add both to `keymap.md` under Move, with the version line noting round 4.

## Everything else

The other ten screens and the Floor stay as built. Keep the style rules: gray until abnormal, hazard yellow only for needs you, red only for faults, green only for added text in diffs, no blue, a glyph and a word for every state, no motion. No hyphen or dash between words in UI copy. No code comments.

## Test hooks

- `?keys=` replays keys (in the page). `?clicks`, `?drag=` and `?wheel=` come from `switcher.js`; see `README.md`.
- Examples a builder or reviewer runs with `sh ../snap.sh final.html "<suffix>" 1600x1000 <png>`:
  - `?wheel=.adwvp,400,120#adws/feature` pans right and down. `.adwvp` is the canvas viewport and carries `data-view="x,y,zoom"` (x and y are the layer's translate in px, zoom is a percent, all rounded). The pannable inner layer is `.adwlayer`.
  - `?wheel=.adwvp,0,-300,ctrl#adws/feature` zooms in (to 150%).
  - `?drag=.adwvp>-300,0#adws/feature` drags the canvas. A drag pans from anywhere except a pin, the minimap and the zoom readout; a click without a drag selects.
  - `?drag=.adwmini>-60,0#adws/feature` is a minimap click and drag. The view rectangle is `.adwvf`, the readout button `.adwzoom` (click fits the graph).
  - `?keys=%3D#adws/feature` fits the graph; `?keys=l,l,z,z#adws/feature` follows wires twice and centers the selection at 100%.
  - Attributes: `[data-node="<node id>"]`, `[data-pin="<node id>:<port>:in"]` or `:out`, `[data-wire="<edge id>"]`. A wire label chip carries the same `data-wire`.
  - `?drag=[data-pin="t_manual:task:out"]>[data-pin="plan:task:in"]#adws/feature` makes a data wire and selects it (a pin is `[data-pin="<node>:<port>:out"]`, output pins are the drag source; both pins must be on screen). `?drag=[data-pin="implement:diff:out"]>[data-pin="plan:tree:in"]#adws/feature` shows the notice "tree does not take a diff".
  - `?drag=[data-pin="implement:diff:out"]>+60,+170#adws/feature/implement/pin/diff` drops on empty canvas and opens the step picker. `?drag=[data-pin="implement:plan:in"]>-120,+200#adws/feature/implement` lifts a wired input's wire and removes it on empty canvas.
  - `?keys=c#adws/feature/plan/pin/task` opens the picker of pins it can wire to; `?keys=d,d#adws/feature/wire/plan_plan_implement_plan` removes that wire (`u` undoes).
  - `?clicks#adws/feature/plan` clicks every element with the plan node open.
  - Hashes: `#adws/feature/implement`, `#adws/feature/implement/pin/diff`, `#adws/feature/wire/plan_done_implement`.

## Done means

- `sh ../shoot.sh round-04 final.html F` prints OK with a click count for all eleven screens: no ERR, no WIDE, no CLICK.
- At 1600 by 1000 and 1280 by 800, `#adws`, `#adws/feature/plan`, a pin, a wire, a trigger, the prompt picker open, a wheel pan, a pinch zoom, a canvas drag, a minimap click and a pin to pin drag each shoot OK and show what this spec says.
- Every click target on the canvas selects what the table above says, and the right panel shows that element's fields.

## Test hook notes

- Shift+wheel panning cannot be driven by `?wheel=` (it supports Ctrl only), so it is not part of the hook checks.
- `?drag=sel>+0,+0` (zero offset) sends a synthetic click on release, so clicks can be driven.
- At 1280 wide, pin to pin drags past Prepare need the route hash, for example `#adws/feature/plan`.
- The prompt picker lists prompt files only, opens on the current prompt, and has a none row.
