# docs/ui: the new dashboard design

The reference for porting the web dashboard to its new design. Every port issue cites a file in this folder. It is a static prototype on mock data, not served by Arete and not part of the package.

## Open it

Open `final.html` in a browser straight from disk. No server, no build step. An empty hash opens the Floor; `#triage`, `#board`, `#task`, `#run`, `#intake`, `#outbox`, `#prompts`, `#adws`, `#insights` and `#settings` open the other screens, and an id may follow after a slash (`#task/T-117`, `#adws/feature/checks`).

Open `index.html` for the shot gallery: 178 lossless WebP shots in `shots/`, grouped by screen, each captioned with the URL suffix that produced it. `shots.tsv` lists the same shots as file, URL suffix, window size, group and caption.

## Files

| File | What it holds |
|---|---|
| `final.html` | The whole prototype: every screen, the key layer, the palette and the ADW canvas |
| `spec.md` | The design calls the prototype carries |
| `keymap.md` | The one key map the port takes |
| `tokens.css` | Colors, type sizes and spacing; IBM Plex Sans from Google Fonts and JetBrains Mono from `fonts/` |
| `fonts/` | JetBrains Mono woff2 files and their OFL license |
| `mock.js` | `window.MOCK`, the linked mock data every screen reads |
| `switcher.js` | The bottom pill, the red error box and the `?clicks`, `?drag=` and `?wheel=` test hooks |
| `manifest.js` | The screen list the pill reads |
| `index.html`, `shots.tsv`, `shots/` | The shot gallery |

## Mock data

The clock stops at 2026-11-13 15:40 Eastern. Names and shapes come from this repo and its public issues. People, Jira tickets, Slack threads, emails, meeting transcripts and LLM chats are synthetic, emails use `example` domains, hosts are placeholders, and the client project `trailhead` is fictional.

## Test hooks

`?keys=j,j,enter` replays keys. `?clicks` clicks every clickable element on the screen in turn and reports any error in the red box. `?drag=<from>><to>` drags from one CSS selector to another or by an offset such as `+200,-40`. `?wheel=<selector>,<dx>,<dy>[,ctrl]` sends one wheel event. Lines in `spec.md` that run `snap.sh` or `shoot.sh` name the prototype's screenshot tools, which are not in this repo.
