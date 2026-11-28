# Arete key map

One map for every prototype from round 2 on, and for the real port. Justin asked for vim style keys and one map he never has to second guess ("just need to pick one and stick with it"). Each letter has one meaning on every screen. Key caps show lowercase letters; a shifted key shows as ⇧r.

Version 2 (round 3, 2026-11-13): Justin dropped the Space leader ("Just use the letter"). A single letter in NORMAL mode now goes to a screen. Four letters already held actions, so those actions moved: retry from t to ⇧r, open a link from o to gx, Intake to n because i stays insert, Settings to , because s stays set aside. New: c creates an item in the focused list, and dd also removes one.

Version 3 (round 4, 2026-11-14): on the ADW canvas, `=` fits the whole graph in view and `zz` centers the selected node at 100%.

## Modes

- **NORMAL**: the default. Keys act on the focused item.
- **INSERT**: the caret is in a text box. Only Esc and Cmd+Enter are taken; every other key types. Enter adds a new line and never sends. Tab moves to the next box of the same item (native behavior).
- The key bar at the bottom shows the mode and the focused item's keys.

## Go to a screen

One letter, in NORMAL mode, from anywhere. The sidebar shows each letter in one aligned column; a row with no letter keeps a blank slot so the column stays straight.

| Key | Screen | Why this letter |
|---|---|---|
| f | Floor | floor |
| t | Triage | triage |
| w | My work | work |
| b | Board | board |
| r | Runs | runs |
| n | Intake | new; i is insert |
| o | Outbox | outbox |
| p | Prompts | prompts |
| a | ADWs | ADWs (loops are now ADWs with triggers) |
| v | Insights | view; i is insert |
| , | Settings | the Mac settings key; s is set aside |

Projects and saved views have no letter. They sit in the sidebar with a blank slot and in the palette.

## Move

| Key | Does |
|---|---|
| j, k | Next, previous item in the focused list |
| gg, G | First, last item |
| = | On an ADW graph, fit the whole graph in view (fit may zoom below 40% to show a wide graph, while wheel and pinch zoom stay between 40% and 150%) |
| zz | On an ADW graph, center the selected node at 100% |
| h, l | Focus the pane to the left, right (sidebar, queue, focus). On the board, h and l step between columns and leave the board pane only at its outer edge. On an ADW graph, h and l follow a wire to the upstream or downstream node |
| Enter | Open the selected item. On an ADW graph node, open its properties |
| - | Up one level: task to its board, run to its line on the Floor, a selected run on the Floor back to its line, a graph node back to its ADW |
| [, ] | Previous, next run on the run screen |
| gt, gT | Next, previous tab or display where a pane has them (focus pane tabs, Floor displays) |
| / | Search the focused list. Esc closes the search and keeps the text; Esc again clears it |
| gc | Chat with Arete: open or close the right chat dock |
| : or Cmd+K | Command palette, closed by default. Everything without a key lives here (snooze until a set time, revert a prompt, batch send, redirect, filter and sort presets) |
| ? | Key sheet |
| Esc | Leave INSERT, close the palette, a menu, the key sheet, the chat dock or a reason box, fold an expanded turn. Esc never changes screens |

## Act

| Key | Does |
|---|---|
| i | INSERT in the focused item's first text box: draft, answer, Other option, fix, steer, prompt editor, node property, token field, merge by hand |
| Cmd+Enter | The one submit key and the go key, in NORMAL and INSERT. It sends whatever the box holds, edited or not, or runs the item's primary action: approve, send, answer, continue, accept, apply, save, retry a fault. Never two versions of approve |
| 1 to 4 | Pick an option on a choice or conflict item. Typing in Other picks the Other option. Cmd+Enter submits |
| c | Create an item in the focused list: a device, credential, rule or connector in Settings, a prompt, an ADW, a node on an ADW graph (opens the step picker), a pasted intake item. Opens a one line form in INSERT; Cmd+Enter saves, Esc cancels |
| s | Set aside: snooze a triage item until tomorrow 09:00, or hold a line or a run. s again on a held line or run resumes it. Other snooze times are in the palette |
| ⇧s | Shelve, with a reason and a return time |
| dd | The destructive verb of the focused item: reject an approval, discard an outbox draft, decline a proposal, stop a run, remove a list item or a graph node. Reject, discard and decline open a one line reason box in INSERT (Cmd+Enter confirms, an empty reason is allowed, Esc cancels). Stop and remove act at once, with u to undo. The double press is the guard; no modal |
| ⇧r | Retry: retry the step on a stop item, retry now on a fault |
| m | Approve and remember, only on approval kinds that Settings allows |
| gx | Open the item's link: the run behind a fault, the setting behind a continue item, the source thread, a node's prompt |
| y | Copy the item's id and link |
| u | Undo the last action. The item returns to where it was |
| . | Repeat the last action on the new selection (dd then . reuses the last reason) |

## After an action

- The item leaves its group: a resolved item drops out, a snoozed or shelved one moves into the collapsed Snoozed or Shelved group. Every count recounts: group headers, page head, sidebar badge, andon.
- The selection moves to the next item, so Cmd+Enter, Cmd+Enter, s, dd clears a queue.
- A notice says what happened and ends with "u to undo", for example "Sent OB-301 to Slack · u to undo". It sits in the key bar row and takes no layout space, so the list never moves under the cursor.
- The andon counts HIGH and MED only: "10 need you · 3 high · 7 med". LOW items list in their own group and are not in the count.

## Triage order

HIGH, then MED, then LOW, then Snoozed and Shelved collapsed. Inside a group the item waiting longest is on top. #triage opens on TR-201.

## Retired keys

Gone since round 2: x, xx, z, e, the Space leader, ⇧h, ⇧t, ⇧m, ⇧d, ⇧b. Moved in version 2: t (retry, now ⇧r; t is Triage), o (open a link, now gx; o is Outbox). Keys that came back with a new meaning in version 2: w, b, r, a, v, n, c as screens or create, and ⇧r as retry.
