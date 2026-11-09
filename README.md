# arete

Agent loops that keep running and keep making the harness they run in better.

Arete is one global engine for every repo on the machine. Each repo gets a thin layer that says how to set up, what checks must pass, and how much the agents are trusted there. The layer is `.agents/` in the repo, or a folder outside it when the rest of the team does not use arete. Agents edit files. Arete's own code commits, pushes and opens PRs, so no agent ever holds git write access.

## Serve

`arete serve` is the one daemon. It fires each loop on its trigger (a cron line, an interval, a source with new events, a webhook), runs the fired loops through their queues, closes idle sessions, writes a beat to `serve.json` every 20 seconds, and serves the dashboard. `arete install` runs it at login: one launchd agent, `com.arete.serve`, on a Mac, or one systemd user unit, `arete-serve.service`, on Linux. It retires the old per loop agents (`com.arete.heartbeat`, `poll`, `mine`, `audit`, `eval`, `triage`, `dashboard`), since each of those now runs as a loop under serve. `arete install --dry-run` prints what it would write. A loop run killed with serve settles as interrupted, and serve picks up its queues again when it starts.

Serve restarts itself when its own code changes. On each tick it looks at the `.py` files of the arete package, and once a change has held still for one tick and serve has no source poll, dashboard request or chat message of its own in flight, it checks that the new code imports and then replaces itself with a fresh serve in the same process. Runs and sessions are processes of their own, so they keep going across the restart, and the fresh serve reaps each one when it ends. New code that does not import is named in the serve log, and serve keeps the code it started with. A self restart is not a start, so loops with `run_at_load` do not fire again. So a push to arete's own main, followed by a fast forward of the checkout serve runs from, reaches serve with no restart by hand.

## Loops

A loop is a folder with a `loop.toml`, a `prompt.md` when an agent does the work, and any script it calls. [arete/loops/README.md](arete/loops/README.md) is the whole format: the four kinds (command, watch, climb, search), triggers, sources, emits, budgets, the queue, the device it runs on, and the safety rules. Arete ships these:

| Loop | Kind | When | What it does |
|------|------|------|--------------|
| `heartbeat` | command | hourly, and when serve starts | Checks serve's beat, the loops, log freshness, real probe calls, claude login expiry, the event hook, and disk. Each failing check opens one issue on `alert_repo`; recovery closes it. |
| `mine` | command | nightly | Reads the day's Claude Code transcripts, hook events and PR feedback, finds corrections and repeated friction, and opens harness PRs (CLAUDE.md, skills, `.agents/`, arete's own step prompts) in the repo where the fix belongs. Claude Code denies agent writes under `.claude/`, so a fix there is listed in `arete status` to make by hand. |
| `audit` | command | nightly | Deterministic checks of skills, hooks and `.agents` configs, plus skills that fail or never get used. |
| `poll` | command | every 5 min | Tickets labeled `agent`, from GitHub issues or Jira, go through plan, build, checks, fix, verify in use, review, learn, then a GitHub PR, then a watch on the PR's CI. Review comments on that PR start a revision. `arete run REPO TICKET` runs one ticket by hand. |
| `eval` | command | weekly | Runs eval tasks against a fixed commit and records the pass rate, so harness changes are measured, not guessed. `arete eval mine` writes new tasks from merged arete PRs. |
| `triage` | command | every 15 min | Reads your open tickets that do not have the `agent` label yet and sorts each one: a fix or a feature with its repo, size, plan and assumptions, a research ticket, or work for a person. With `[jira] auto` it sends ready Jira tickets in the sprint to the pipeline. See [Triage](#triage). |
| `research` | command | every 10 min | Researches the next research ticket on the triage board and writes a report for you to review. See [Triage](#triage). |
| `loop-intake` | watch | on each new issue | Turns an issue labeled `loop` that you opened on the arete repo into an `agent` ticket that specs the new loop folder, so the pipeline builds it. |

A new loop needs no code in arete:

```
arete loop new "watch the API log for new tracebacks and file a ticket for each"
arete loop try NAME       # one run now with every emit held; an event loop reads a sample of real events
arete loop enable NAME
```

`loop new` has a model draft the folder into `~/.config/arete/loops/`, validates it, and tries again on a problem. The draft starts off. `arete loop list`, `status NAME`, `start NAME`, `stop RUN` and `feed RUN -f` cover the rest, and so does the dashboard's Loops view.

Every run of every kind, pipeline, loop, session, chat and draft, is a row in one runs table with its trigger, model, runner, device, cost and tokens. Each model call is a step under its run. A run's live feed holds its steps, the tools the agent calls, and a command loop's output as it prints.

An emit that changes something outside arete, a ticket, a PR or a push, can wait for approval: `arete approve` lists what waits, and `arete approve ID` approves one and `arete approve ID --reject` drops it. A delivery that fails stays listed with its error, and approving it again tries the delivery again. A `push` emit lands a climb or search change on the default branch with no PR. It waits for a person while the repo's `trust.level` is `pr`, and under `automerge` it waits only when it touches a path the repo denies, or when its `approve` table says so: too many lines, a deleted file, or a listed path. A push that fails to land stays as a failed approval. A `loop` emit hands each finding to another loop, so one loop can find and another can fix. [arete/loops/README.md](arete/loops/README.md) has the rules for both. A loop with `[trigger] webhook = true` takes events at `/hook/<loop>`; `arete webhook secret NAME` prints the URL and its token.

## Runners, devices and sessions

A model name picks its runner. A claude alias or a `claude-*` id runs on the `claude` CLI on a Claude subscription. A `provider/model` id, such as `deepseek/deepseek-flash` or `ollama/gemma4`, runs on `pi`. A `pi:` prefix forces pi. Both report tokens and cost per step; `[pricing]` sets a price for a model whose runner reports none, and a provider in `local_providers` costs nothing. Budgets meter pi spend only. A run on claude has no dollar cap: the subscription carries it, and its cost is still recorded, for display only. When claude reports a usage limit, the run waits until the limit resets and then goes on, resuming its session where it can. The serve holds queued loops that need the held model, the heartbeat counts a held loop as healthy, and chat and loop drafts return the limit at once instead of waiting.

`claude -p` stays open after its answer until each command the agent started in the background ends, so a server left running holds a step to its time limit and loses its answer.
Each claude step that can run commands is told to stop with TaskStop each background command that does not end on its own before it answers, even when its task asks to keep one running.
On a local step, `arete background` runs as a Stop hook: when the agent answers while a background command still runs, it names the command and has the agent stop it and answer again, or end its turn again when it waits for a command that will end.
A step that times out gets SIGTERM, and what is left of its processes 30 seconds later gets SIGKILL.

Arete runs pi with a pi folder of its own, `~/.local/share/arete/pi-agent/`. It links `models.json` and `settings.json` from `~/.pi/agent/` and holds no logins. Keys for pi's providers go in `~/.config/arete/pi.env` as `NAME=value` lines, such as `OPENROUTER_API_KEY=...`. A loop then spends on a key you give arete, such as one with a weekly limit, and never on your own pi logins. A provider with no key there falls back to a key set in arete's own environment, and fails the step when there is none; the launchd serve sets no keys. A device other than `local` runs its own pi with its own logins.

`~/.config/arete/devices.toml` lists the machines loops and sessions run on, besides `local`:

```toml
[device.buildbox]
kind = "ssh"
host = "buildbox"
dir = "/root"
note = "Hetzner box"
```

`arete devices` lists them with their last probe; `arete devices probe NAME` checks the device answers and lists which of `claude`, `pi`, `arete` and `ollama` it has.

A session is an agent you start by hand and talk to, on any device, model and tool set:

```
arete session start "look at why the nightly build is slow" --device buildbox --model sonnet --tools Read,Grep,Bash --attach
arete session send ID "now try the cache"
arete session attach ID | stop ID | list
```

`--repo` gives it a new worktree of a config repo; a device of any kind but `local` takes no repo and starts in its `dir`. `--budget` caps the whole session in dollars when it runs on pi; a claude session has no cap. A session with no message for `session_idle_hours` closes. The dashboard's Launch view starts one with the same choices.

`arete costs` shows model spend by model, runner and loop.

## JEV

JEV is a classifier model from TypeSafe, reached through OpenRouter. It reads text and answers a yes or no question, picks one of a few labels, or rates on a scale, each with a confidence, fast and for $0.042 per million input tokens. Arete asks it first where a classifier can settle the question, and leaves the question to the model only when JEV is unsure:

- Loops: a loop fed by outside text drops each event JEV reads as instructions to an agent before the agent sees it, and a ticket emit drawn from outside text waits for approval on the same check. A `[classify]` step asks JEV first and sends only the events it is unsure of to the model. A finding JEV matches to a known one, even worded another way, counts as that finding, not a new one.
- Risk: the automerge of a pipeline PR waits for a person when JEV rates the diff risky (how much depends on what it changes, how near it comes to credentials, money or data, and how far it strays from its description), or reads it as a change a revert will not undo.
- Push screen: a `push` emit under `automerge` that no static rule holds lands only when JEV reads its diff as a narrow fix. It waits for a person when JEV gives a chance of `screen_hold` or more that the diff weakens a protection, exposes credentials or personal data, sends data somewhere new or builds a command from outside text, adds a capability, removes a behavior instead of fixing it, or renames something other code or config uses; a chance of `irreversible_hold` or more that it does harm a revert will not undo; or rates how far it reaches past the fix it names at `screen_reach` or more. A diff longer than the 12000 characters JEV reads waits too. In a search loop, a JEV hold goes back to the agent with JEV's reason, and the agent narrows the change and tries again; only the last held change reaches a person. JEV judges a given diff once, so the push at the end of the run gets the verdict the loop saw.
- Proof check: a search loop with `[search] proof = "repro.py"` names the script in its scratch folder that proves the bug. When a try makes the check pass, JEV reads the finding, that script and the diff, and asks whether the script checks something other than the problem the finding describes, and whether it runs the code into that problem and checks for the wrong result. A chance of `proof_hold` or more on the first, or under `proof_hold` on the second, holds the push. In the run, that hold goes back to the agent like a push screen hold, and the agent rewrites the proof; at delivery it waits for a person, and the approval shows the proof. A missing proof, or one past the 12000 characters JEV reads, holds it too.
- Already fixed: before the first try of a search run fed by findings, JEV reads the finding against each commit that landed on the base branch since the run that found it started, up to 40, and against each push of the same loop that waits for approval. When JEV gives a chance of `fixed_skip` or more that one of them already fixes it, the run ends as done, already fixed by that change, with no agent call. Otherwise the run goes on and its log names the closest change.
- Verdicts: each push screen, proof, already fixed, research change, research answered and PR feedback verdict is stored with each question's chance, its line, and what came of it: landed, approved, rejected, replaced by a narrower try, waiting, failed, skipped, or went on. `arete jev report [--days N] [--json]` shows the holds per question and what came of them, the close holds and near misses within 0.1 of a line, the pushes JEV cleared that a person rejected, and the landed pushes a later commit reverted, so a line can move on evidence.
- Triage: JEV asks whether a ticket asks for a change to a repo and what kind of ticket it is. A ticket it gives a chance of a code change at or under `triage_skip` settles with no model call, as research when JEV is sure it is research and as work for a person otherwise; one under even odds settles the same way when JEV is sure of research or person. Before arete sends a ticket to the pipeline or the research agent, JEV screens its text for instructions to an agent, and a ticket it flags waits for you.
- Research: when a researched ticket changes, JEV reads what changed in its text against the report's summary and asks whether the change asks something different or adds a fact, decision, limit, deadline or resource the report should use. Under a chance of `research_rerun`, the report stands with no agent call. Before a ticket's first report, JEV reads it against the reports already done for its parent, its siblings and its children, and picks the one that already delivers what it asks, or none. At a chance of `research_answered` or more that one does, the ticket links that report instead of a new run. See [Triage](#triage).
- PR feedback: JEV asks of each new comment you leave on an arete PR whether it asks for a change or a question the author should answer, logs the chance, and stores it with the comment. A comment under `feedback_skip` starts no revision; at the default of 0, none is skipped.
- Miner: the digest leaves out the transcripts JEV is sure hold nothing to learn from, so it has room for the ones that do.
- Sessions: when a session's context passes `compact_tokens` and JEV is sure the new message starts a different task, the session compacts before it takes the turn.
- Guard: each local claude agent that can read files, write files or run commands runs `arete guard` as a PreToolUse hook. With no JEV call, it keeps the agent out of secret files (`.env`, keys, `~/.ssh`, cloud credentials), whether the agent reads or writes one or names one in a command. With JEV it also stops a command that would do harm git cannot undo (throw away uncommitted work, delete outside the working folder, force push, publish, drop data, send a message) and a command that would show or send a credential. A plain push or a new pull request goes through. When anything goes wrong, it lets the call through. `[jev] guard = false` turns it off.
- `arete ask QUESTION [PATHS] [--choice A B] [--score LOW HIGH] [-r] [--only ANSWER] [--json]` asks JEV about piped text, or about each file, sixteen at a time. Agents that can run commands get it, with a short note on when it pays: sort files before reading them, sort a test failure before digging in, check a diff against its task. An agent may ask only about files inside its working folder, and never about secret files.

JEV uses the OpenRouter key the pi runner uses: `OPENROUTER_API_KEY` in `~/.config/arete/pi.env`, or in arete's environment. With no key, with JEV off, or when a call fails, each step does what it did before JEV, with one exception: when the push screen's or the proof check's call fails, the push waits for a person, because a push lands with no review. When the already fixed check fails, the run goes on. Each call is a step of its run on the `jev` runner, priced at what OpenRouter charged; a guard check is a step only when it stops something. JEV sends the text it judges to OpenRouter and TypeSafe: events, tickets, diffs, transcripts, commands and files. Set `jev = false` on a repo whose code or tickets must not leave the machine.

## Runs, sleep and the network

Poll does not wait for a run. It starts each run as its own detached `arete run` process and returns in about a second, so a long run never holds up the next poll. `max_parallel` sets how many runs go at once on the machine, manual runs included. A run frees its slot while it waits on its PR's CI, and takes one back before a CI fix step. A run that waits to take its slot back goes ahead of new runs: while it waits, no new run starts, even on a free slot. It reads `max_parallel` again every 30 seconds, so a raised limit lets it go at once. `arete status` lists it as waiting for a run slot, and a run in its CI wait as waiting on CI without a run slot. Poll takes tickets round robin across repos and starts at most one run per repo each time. A repo with `max_parallel_per_repo` runs going, counting runs in their CI wait and manual runs, gets no new one from poll; `arete run` by hand is held only by `max_parallel`.

A laptop sleeps and drops its network. When poll, triage or the heartbeat start offline, most often just after a wake, they wait up to 3 minutes for the network; if it does not come back they skip with an "offline" note and exit 0. A git or gh call that fails on a network error waits up to 30 minutes for the network, then tries once more. An agent step cut off by a dropped connection resumes its own session when the network is back. A run whose process died is put back in the queue with a comment on the ticket, and the next poll starts it again; a second death in a row fails the run as before. Heartbeat ages count only time awake, so a night asleep never reads as a stale log.

## Layout

- Code: this repo, a uv project (Python pinned in `.python-version`, everything locked in `uv.lock`). Installed with `uv tool install --editable . --python 3.13`
- Config: `~/.config/arete/config.toml` (`arete init` writes a starting one)
- Loops: `arete/loops/` here, `~/.config/arete/loops/` for private loops, `<repo>/.agents/loops/` for a repo's own; switches in `~/.config/arete/loop-switches.json`
- Devices: `~/.config/arete/devices.toml`
- Provider keys for pi: `~/.config/arete/pi.env`, mode 600
- State: `~/.local/share/arete/` (sqlite, events, run logs, loop digests, `serve.json`)
- Worktrees: `~/Library/Developer/arete/worktrees/`, outside every dot folder. A `**/node_modules/**` ignore glob does not match across a dot segment, so a dev server in a worktree under `~/.local` watches all of node_modules and fails with EMFILE.
- Logs: `~/Library/Logs/arete/`
- Per repo: `config.toml`, `expertise.md` and `evals/` in the layer, `.agents/` in the repo or the folder that `layer` names

## Develop

`uv run arete status` runs from the checkout without the installed tool. `uv tool install --editable . --python 3.13 --force` refreshes the `arete` on PATH that launchd uses.

No runtime dependencies: stdlib only, so the tool install needs nothing from PyPI. Add one with `uv add`, never pip. The verifier's browser is `@playwright/mcp`, pinned in `arete/verify.py` and started with `npx`, so it needs node and Chrome on the machine.

## Global config

| Key | Meaning |
|-----|---------|
| `alert_repo` | GitHub repo where each failing heartbeat check opens one issue. Bodies carry check names and a short status only. Until the repo exists, alerts fall back to macOS notifications. |
| `global_repo` | Name of the `[[repo]]` that holds global skills and settings. |
| `arete_repo` | Name of the `[[repo]]` that is this checkout. Its step prompts are harness files the miner may change. Default `arete`. |
| `max_parallel` | Most pipeline runs at once on the machine. Default 1. |
| `max_parallel_per_repo` | Most pipeline runs poll keeps going in one repo, runs in their CI wait included. Default 1. |
| `[models]` | Model per role: `plan`, `build` (also fix and revise), `verify`, `review`, `learn`, `uat`, `miner`, `apply`, `eval_mine`, `triage`, `research` (the reports for research tickets), `chat` (the dashboard console). A role left out uses the claude CLI default, except `triage`, which uses haiku, and `research` and `chat`, which use opus. `arete status` lists each role's model. |
| `[miner]` | `lookback_hours` and `max_digest_chars` for the nightly transcript digest. |
| `[[repo]]` | Every repo arete knows: `name`, `path`, `github`, and the fields below. |
| `[[job]]` | launchd jobs the heartbeat watches, by `label`. A `keepalive` job must have a pid; others must have last exited 0. `log` with `max_age_hours` flags a stale log. |
| `[[probe]]` | Real calls the heartbeat makes every `every_hours`: a `command` whose output must contain `expect`. A real call, because an auth check can say ready while real calls fail on an expired token. |
| `[jira]` | See [Jira tickets](#jira-tickets). |
| `[dashboard]` | `port` (default 8796) and `owner`, the tailnet login the page opens for. Default: the login that owns this machine. See [Dashboard](#dashboard). |
| `[serve]` | `loop_dirs` (the private loop folders, default `~/.config/arete/loops`), `queues` (a table of queue name to how many runs at once, default 1), `daily_budget_usd` (most pi spend across all loops in a rolling 24 hours), `session_idle_hours` (default 2). |
| `[jev]` | `enabled` (default true), `model` (`jev-latest`), `confidence` (0.8, how sure JEV must be to settle a question alone; on a yes or no question, a chance of yes at 0.9 or more, or at 0.1 or less), `injection` (0.5, the chance of an injection at which an event drops or a ticket waits), `risk_hold` and `irreversible_hold` (0.5 each, the first for pipeline automerge only, the second also the push screen's line for harm a revert will not undo and the guard's line for a command git cannot undo), `secrets_hold` (0.55, the guard's line for a command that shows or sends a credential), `triage_skip` (0.2, the chance of a code change at or under which triage settles a ticket as not code without the agent), `screen_hold` (0.4, the chance at which a push screen question holds a push), `screen_reach` (0.6, how far past the fix it names a push may reach before it waits), `proof_hold` (0.5, the proof check's line), `fixed_skip` (0.7, the chance at which a search run fed by findings ends as already fixed), `research_rerun` (0.4, the chance that a change to a researched ticket matters, under which its report stands), `research_answered` (0.8, the chance at which a ticket links a report done for its family instead of a new run) and `feedback_skip` (0.0, the chance that a PR comment asks for a change or an answer, under which it starts no revision; 0 logs only), `[jev.risk_weights]` (`blast` 0.4, `sensitivity` 0.4, `surprise` 0.2), `guard` (true) and `compact_tokens` (120000). See [JEV](#jev). |
| `[pricing]` | `local_providers` (default `["ollama"]`, priced at zero) and `[pricing.models."<model>"]` with `input` and `output` in USD per million tokens, for a model whose runner reports no cost. |

| Repo field | Default | Meaning |
|------------|---------|---------|
| `pipeline` | `false` | Poll this repo's tickets and run the build pipeline. |
| `mine` | `true` | The miner may open harness PRs here. |
| `public` | `false` | PR bodies cite run ids, never transcript text. |
| `tracker` | `"github"` | `github` for issues in the repo, `jira` for tickets in Jira. PRs are on GitHub either way. |
| `jira_project` | `[jira] project` | Jira project for this repo. |
| `jira_label` | `repo-<github repo name>` | Route label that sends a Jira ticket to this repo. |
| `jev` | `true` | JEV may read this repo's events, tickets, diffs, transcripts and files. `false` keeps them off OpenRouter and TypeSafe. See [JEV](#jev). |
| `layer` | `<path>/.agents` | Folder that holds the repo's layer. Point it outside the repo to keep arete files out of a shared repo. |
| `[[repo.part]]` | none | One per git repo under `path`, for a product split over several repos: `dir` (relative to `path`) and `github`. See [Parts](#parts). |

## GitHub tickets

An open issue labeled `agent` that the repo owner opened. Issues labeled `bug` or `fix` are fixes; the rest are features. Arete tracks progress with `agent:running`, `agent:pr`, `agent:failed` and `agent:needs-info`, and the PR closes the issue, unless it leaves a change for a person. Remove `agent:failed` or `agent:needs-info` to send it back to the queue. When the plan finds nothing left to build and the builder changes nothing, arete pushes nothing: with an open PR it keeps the PR in `agent:pr` and says so on the ticket, and with none it asks in `agent:needs-info` whether the ticket is done. A ticket in `agent:needs-info` also goes back when you reply to arete's question in a comment: poll removes the label, and the next run reads the question and your answer. `arete install` creates all of these labels in each GitHub pipeline repo, so `agent` is there to pick from any GitHub client, the phone app included.

## Jira tickets

```toml
[jira]
env_file = "~/.env"
project = "ABC"

[jira.transitions]
running = "In Progress"
needs-info = "Needs Clarification"
pr = "In QA"

[jira.landed]
staging = "UAT"
main = "Done"

[[repo]]
name = "web"
path = "~/dev/web"
github = "acme/web"
pipeline = true
tracker = "jira"
mine = false
```

`JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN` come from the environment, else from `env_file`. `[jira.transitions]` moves tickets as arete works them: each of `running`, `pr`, `failed` and `needs-info` maps to a target status name or a transition name. A name that is a status in the project moves a ticket only into that status, by whatever transition leads there; any other name is read as the name of a transition. The pipeline moves a ticket to `running` when a run starts, a revision included, to `pr` once its PRs are open and CI passes, to `needs-info` when it asks a question on the ticket, and to `failed` when it gives up. Research tickets move only through `[jira.research]`; see [Triage](#triage). A state left out only sets labels and comments. Each poll also catches up a ticket whose latest run sits in `pr`, `needs-info` or `failed` but was never moved there, as after you first map a state: it moves the ticket once, never out of a status in Jira's done category such as UAT, and never again after a person moves it. With the tables above, a ticket arete picks up goes to In Progress, a question sends it to Needs Clarification, an open PR to In QA, the PR merged into staging to UAT, and the release into main to Done. `[jira.landed]` moves a ticket once its merged PRs reach a branch, by the same kind of name. List branches in the order work travels: each poll moves the ticket to the last listed branch that holds the work of every PR, and never back. A branch holds a PR's work when it contains the merge commit, or when another PR naming the ticket key merged into it, as a port of the change to `main` does. With the table above, a PR merged into staging moves its ticket to UAT, and the release that brings staging into main moves it to Done. On every move but the one to the last listed branch, arete also posts numbered steps a person can follow to test the change by hand there. A read only step writes them from the ticket, the PR body and the PR diff. If it fails, the next poll tries again with no second comment; after 3 tries arete posts a short note that names the PRs instead. `arete jira-check` reads Jira with the token, counts the tickets waiting in each project, lists the moves configured, and names each target that is not a status in the project, since such a target moves a ticket only where a transition has that name. Arete takes one transition per move, so the workflow must allow each move it makes directly from the status the ticket is in.

A ticket runs when all of these hold:

- It carries `agent` plus exactly one route label, `repo-web` above.
- It is assigned to you and not Done.
- You added `agent` yourself, and nobody else has edited the summary or description since you last touched its labels.

Touching the labels is the approval: whatever the ticket says then, you have read. After it, only your own edits, comments and attachments reach the agent; other people's comments and attachments count only when added before the approval. Arete's own label edits go out under your token too, so they are told apart: they only swap `agent-*` state labels and always add one, and they never count as approval. Removing a state label to retry is your edit, so it approves the ticket as it reads now.

Progress shows as `agent-running`, `agent-pr`, `agent-failed` and `agent-needs-info`. A reply from you after arete's question takes `agent-needs-info` off, as on GitHub. Bug, Fire, Incident and Problem issue types are fixes, as are tickets labeled `bug` or `fix`. PR titles end with the key, like `Fix login (ABC-12)`, PR bodies link the ticket, and these PRs get no arete labels. Tickets the approval rules hold back, and tickets with a missing or doubled route label, show in `arete status` with the reason. `arete run REPO KEY --force` runs one anyway.

`[jira] auto = true` lets [triage](#triage) add `agent` and the route label to the tickets it finds ready, at most `auto_per_refresh` (default 3) each refresh. Default `false`.

`[jira] sprint_only` (default `true`) limits that auto approval, and the research loop, to tickets in the open sprint or left over in a closed sprint and not moved to a future one. Triage still sorts every open ticket. Set it to `false` for a board without sprints.

`[jira] skip_types` lists issue types arete leaves alone, such as `["Trail"]`: triage never reads them, so they never reach the board, auto approval or the research loop. A ticket of a skipped type that you label `agent` yourself still runs.

Image attachments go to the agents as files they can open, up to 6 images and 5 MB each. The rest are named in the prompt, not downloaded. A failed download fails the run without labels, so the next poll tries again.

## Per repo layer

```toml
[setup]
commands = ["pnpm install --frozen-lockfile"]
copy = [".env.local"]

[checks]
typecheck = "pnpm typecheck"
test = "pnpm test"

[limits]
max_diff_lines = 400
deny = ["drizzle/migrations/**", ".github/**"]
max_attempts = 3
timeout_minutes = 45

[agent]
allow_bash = ["pnpm *", "npx vitest*"]

[trust]
level = "pr"
automerge_after = 5

[git]
base = "main"
subject = "{kind}: {title}"
branch = "arete/{id}-{slug}"

[verify]
start = "PORT={port} pnpm dev"
url = "http://localhost:{port}"
ready = "/"
ready_seconds = 180
notes = "Sign up only with addresses at example.test."

[research]
dir = "docs/research"
```

| Key | Meaning |
|-----|---------|
| `setup.commands` | Run in each fresh worktree before the agent starts. |
| `setup.copy` | Files copied from the main checkout into each worktree. |
| `checks` | Every check must exit 0 before review. The agent may run them while it builds. |
| `limits` | Diff size, paths the change may not touch, fix attempts (which also cap the CI fix rounds), and the run timeout. Code enforces them after the agent finishes. Every repo also denies `.agents/config.toml`, `.github/workflows/` and `.env` files. It denies `.claude/` too, since Claude Code refuses agent writes there. The planner names a change in a denied path for a human to make. The PR shows it under "Left for a person", and a rerun on an open PR writes, rewrites or removes that section to match its plan. The verifier and the reviewer are told that a person makes it, so its absence never fails the run. The CI fix step counts a red check that only that change fixes as not caused by the PR. |
| `agent.allow_bash` | Bash patterns the agent may run, on top of read only git and file commands. |
| `trust.level` | `pr`, or `automerge` once earned. |
| `trust.automerge_after` | Merged PRs in a row before arete opens a PR proposing automerge. |
| `git.base` | Branch PRs target. Defaults to the GitHub default branch. |
| `git.subject` | Commit subject and PR title. Fields: `{kind}` (`feat` or `fix`) and `{title}`. The ticket ref is appended to the commit subject, like `(#35)` or `(ABC-12)`, and to a Jira PR title; a GitHub PR links its issue with `Closes #35` instead, or with `Part of #35` when the plan leaves a change for a person. |
| `git.branch` | Branch name. Fields: `{kind}`, `{id}` (`35` or `ABC-12`) and `{slug}` (the title in short url safe form, cut at a word). |
| `verify.start` | Shell command, run in the worktree, that serves the change. `{port}` is a free port arete picks; `{port2}` and up give more. Leave it empty for a CLI or a library: the verifier then drives the change through Bash alone. Leave `[verify]` out and nothing checks the change in use, and the PR says so. |
| `verify.url` | Where the served app answers. Default `http://localhost:{port}`. |
| `verify.ready`, `verify.ready_seconds` | Path arete waits on before the verifier starts, and how long. Any answer below 500 counts as ready. Defaults `/` and 180. |
| `verify.allow_bash` | Bash patterns the verifier may run, on top of `curl` and `echo`. |
| `verify.notes` | What the verifier must know: how to sign in, what not to touch, what is shared. |
| `research.dir` | Folder, from the repo root, where arete files the reports of this repo's research tickets as pull requests. Leave it out and the reports stay in arete. See [Triage](#triage). |

A template with an unknown field falls back to the default. A layer in `.agents/` that is not committed yet still works: runs read it from the main checkout and lessons land there, but arete never proposes automerge until it is committed. A layer outside the repo is read in place, lessons land in its `expertise.md`, and the miner leaves the repo's `.agents/` alone. Arete never proposes automerge for it: raise `trust.level` by hand.

After a run that had something to learn from (a failed check, a fix, a failed check in use, or PR feedback), the learn step rewrites `expertise.md` whole, in at most 80 lines. It checks each lesson against the code in the worktree and drops a lesson the code now contradicts, with the reason in the run's report. A rewrite over the cap is not applied. A layer outside the repo is made a git repo, and arete commits each rewrite there, so its log shows what was learned and from which run. A clean run learns nothing.

## Verify in use

Checks passing is not the bar. After the checks, a verifier agent uses the change the way its user will: it starts the app with `verify.start`, waits for `verify.ready`, then drives it through a headless browser (Playwright, an isolated profile), `curl`, or the CLI, and reports each check it made with what it saw. Reading the code and running the test suite do not count as checks. The builder does not add tests unless the repo's own rules ask for them.

- Pass: the evidence goes in the PR body. Screenshots are pushed to the orphan branch `arete/evidence` and linked from the body.
- Fail: the build session resumes with what failed, then the checks and the verifier run again, up to `limits.max_attempts`.
- Blocked: the verifier could not check something (a service it may not reach, a sign in it cannot finish). The PR opens as a draft and says what it would take.

Arete stops the server's whole process group after each verifier run, so no dev server outlives its step.

## CI

After the push, the run waits for the checks on the PR. It waits up to 30 minutes, or 3 minutes when no check reports at all. The run frees its `max_parallel` slot for the wait, so another ticket can start, and takes a slot back before a CI fix step. Arete compares each failed check with the same check on the head of the base branch.

- Red on the base too: the PR section notes the base result and its date. This PR did not cause it, so nothing changes.
- Passed on the base, skipped there, or no result there: a CI fix step reads the job log and the diff, and decides whether the PR caused the failure. When the base moved after the last push, arete first merges it into the branch, since CI tested the PR merged with that base. A merge that conflicts is undone, and the step works on the branch as it is, as it does when the fetch fails. A check that the PR did not cause gets a note with the reason. For a check that the PR did cause, the step fixes the code. Then the checks and the diff limit run again, arete pushes the fix, and the wait starts again. `limits.max_attempts` caps the rounds, and the diff limit counts the whole branch.

After each round, arete writes a `## CI` section into the PR body. It shows what passed, each red check with its base result and reason, and the earlier rounds. When the PR still causes a red check after the last round, or the fix step fails, arete turns the PR to a draft and fails the run. The section then ends with `Arete stopped:` and the reason. The PR stays open. Review comments on it start a revision, as on any arete PR. A later run that is approved and green marks it ready again. A run gets the status `pr` only when its PR caused no red check.

## PR feedback

Review comments on an arete PR are the next request. Poll reads the PR's reviews, line comments and conversation comments from the repo owner. A submitted review starts a revision at once; loose comments start one after 10 quiet minutes, so a batch goes in one run. Pending reviews and arete's own comments, which carry a hidden marker, are left out. With [JEV](#jev) on, poll logs JEV's chance that each new comment asks for a change or an answer, and a comment under `[jev] feedback_skip` is kept as skipped and starts no revision.

A revision continues the PR's branch, with a base that moved merged in first: a revise step (no new plan), the checks, the fixes, verify in use, review, and learn with the comments as the story. Arete pushes to the same PR, answers each line comment in its own thread, and posts one overall reply. Comments on a PR closed without a merge are still read for 7 days, and a reopened PR goes back to `pr`. The miner reads all of it: a comment you had to make twice is a harness fix.

## Parts

```toml
[[repo]]
name = "shop"
path = "~/dev/shop"
pipeline = true
tracker = "jira"
layer = "~/.config/arete/layers/shop"

[[repo.part]]
dir = "app/client"
github = "acme/shop-client"

[[repo.part]]
dir = "app/server"
github = "acme/shop-server"
```

A repo with parts takes no `github` of its own and needs `tracker = "jira"`. Each run gets one folder with a worktree per part at its `dir`, all on the same branch, and the checks run from that folder. Each part continues from its open PR on the branch, or from commits already pushed to the branch with no PR yet, else starts fresh from the base. When a part continues and its base moved since, arete merges the base into the branch first, so the plan and the build see what CI will test; a merge that conflicts is undone, and the run works on the branch as it is. A ticket can need several runs, and each run adds commits to the PRs already open; it keeps the branch of those PRs even when the ticket's title or type changes.

Arete commits and opens a PR only in the parts the change touched, and every PR body links the others. Before it pushes, it checks that no part's branch moved since the run started. If one did, it pushes nothing and says so on the ticket; remove `agent-failed` to run again on top of the new commits. The run counts as merged when all its PRs merge, and the run counts as rejected when they are all closed and one did not merge.

## Evals

A task is `evals/<name>.toml` in the layer:

```toml
title = "Recipes written in the library can carry a photo"
base = "4f1c2ab"
prompt = '''The task, written like an issue.'''
check = "pnpm vitest run src/lib/recipes/photo.test.ts"
timeout_minutes = 30
```

`base` is the commit to start from, the remote default branch when left out. Files in `evals/<name>/` are hidden from the agent and copied in only after it finishes, so a held out test decides pass or fail. Each task runs the pipeline's own plan, build and fix steps in a fresh worktree, with the harness (CLAUDE.md, skills, expertise) taken from the working copy, or from a git ref with `arete eval REPO --ref REF` to compare a harness PR against main. A layer outside the repo is read in place. Evals run on single repos, not on repos with parts.

A task can be graded in use instead of by a check script:

```toml
title = "Add a --world option"
source = "https://github.com/acme/toy/pull/110"
base = "ba1a769"
prompt = '''The ask, as a person files it.'''
verify = '''
1. Run `python3 toy.py --world`. Expected: one line, `hello world`.
2. Run `python3 toy.py`. Expected: one line, `toy`.
'''
```

The grader is the pipeline's verifier, started on the layer's `[verify]`. It gets the task and the steps only, never the diff or the builder's summary, and a failed result names the step that failed. A task with both `check` and `verify` needs both to pass.

`arete eval mine [REPO]` writes these tasks from merged arete PRs, newest first, 3 at a time (`--limit N`). A read only writer step reads the ticket, the PR body with its evidence, and the diff, and writes the ask and the steps without naming the fix; it skips a change with no behavior a user meets. Arete keeps a task only when the grader passes it on the merge commit and fails it on the commit before, so the task tells a working change from a missing one. Each outcome is recorded, so a PR is mined once; a crash records nothing, and the next mine tries again. A kept task is committed in a layer outside the repo, or opened as a PR on `arete/evals-<name>` for a layer in `.agents/`. `--dry-run` lists what it would mine and skip, with no model call. Only repos whose layer has `[verify]` are mined.

`arete eval REPO --prompts DIR` runs the plan, build and fix steps with the prompts in `DIR`. The grader keeps the installed prompt, so the judge never moves with what it judges.

## Prompt changes

Arete's own step prompts, `arete/prompts/*.md`, are harness too. When the miner sees the same problem in how arete's steps do their job across repos, it proposes a change to the plan, build, fix, verify, review or other step prompts in the `arete_repo`. The grader, the eval writer, the miner and apply prompts stay human edited. Apply refuses a change that adds or drops a `$placeholder`, or adds or deletes a prompt file.

Arete then runs each eval suite twice on the same day, once with main's prompts and once with the PR's, and comments on the PR with the pass rate of each, the tasks that changed, cost and run ids, and the changed prompts the evals do not exercise. Private repos show as "private repo N", never by name. These runs do not count toward the nightly baseline. The pipeline reads its prompts from this checkout, so pull it after you merge one of these PRs.

## Triage

`arete triage` shows a board of your open tickets that are not yet labeled `agent`: Jira tickets assigned to you in the projects of your Jira pipeline repos, and issues you opened in your GitHub pipeline repos. For each one, a read only haiku step looks at the ticket and the repo code and answers:

- Kind: `fix`, `feature`, `research` (find something out and report it) or `person` (a meeting, a decision, an account, a check done by hand, a change in an outside tool). A ticket that asks for research and then a change that depends on it is research.
- Repos: the one it belongs in. A code ticket that names none, or needs more than one, gets a question to name it or split it.
- Ready: the pipeline could build it as written.
- Questions: only real blockers. The ticket does not say what to change and the code does not settle it, an attachment or copy it mentions is missing, a business decision changes the whole build, it needs an account or setup outside the repos, or it says it is parked, blocked or a duplicate.
- Assumptions: the small choices the build will make where the ticket leaves them open, such as wording, placement or a default. They are not questions; the pipeline's plan reads them, and the PR lists them for you to check.
- Size S, M or L. L means split it, so it is never ready.
- A plan of up to 6 steps.

The step may read only inside the repos it is given, has no MCP servers, and costs at most $0.30 a ticket. A ticket gets a new call only when its updated time changes, at most 20 calls a refresh; the rest wait for the next. With [JEV](#jev) on, JEV settles many research and person tickets with no model call, and those do not count toward the 20. The board puts each ticket under one heading: Ready to build, Sent to the pipeline, Research, With questions, Needs a person, Not triaged. `--refresh` triages what changed, then shows the board; `--refresh --dry-run` lists what it would triage and approve with no model call and no writes; `arete triage REF` shows one ticket in full. The `triage` loop refreshes every 15 minutes under serve.

With `[jira] auto = true`, each refresh sends up to `auto_per_refresh` ready Jira tickets, newest first, to the pipeline by adding `agent` and the route label. That is the approval the pipeline needs, so the next poll builds the ticket, tests it and opens a PR, and the ticket gets the pipeline's usual labels and comments. A ticket goes only when it is in the sprint scope of `[jira] sprint_only`, its repo is a pipeline repo with JEV on, JEV finds no instructions to an agent in its text, and it has not changed since triage read it; otherwise the board says why it waits, and marks a ready ticket outside the sprint as such. A PR from a ticket triage sent never automerges, whatever the repo's trust level, and its body lists what triage assumed. Triage never sends a GitHub issue on its own: send it from the dashboard, or add `agent` yourself. Sent tickets stay on the board for 7 days with their pipeline status and PR.

You can also send a ticket yourself from the dashboard's Triage view.
A ticket with questions has an Answer the questions fold, with a box for each question and Send to the pipeline under them, and a ready ticket has the button alone.
When triage did not settle the repo, you pick it from the pipeline repos of the ticket's source.
Send posts your answers, leaving out blank ones, to the ticket as one comment from you under "Answers to triage's questions".
Then it adds `agent`, and the route label on Jira, takes the ticket off the board and starts a poll.
With JEV on for the repo, JEV screens the ticket first, and a ticket it flags waits for you to add the labels by hand.
A ticket that changed since triage read it, or that has new questions since the page loaded, is not sent, and the page says why.
The pipeline takes a ticket you sent as your approval: the plan reads your answers, which win over what triage assumed, and the PR follows the repo's trust level.

Research tickets go to the `research` loop, one at a time, newest first. With `[jira] sprint_only` on, the loop takes only Jira tickets in the sprint scope, and the board names `arete research REF --run` for the rest. An agent on the `research` model reads the ticket, its parent ticket, the reports already done for its parent, its siblings (the tickets under the same parent) and its children, and the repos it names, searches the web, and writes a report: the answer first, then the evidence, with a link for each outside fact. It may read files and the web and change nothing; it searches with general terms, never with text from the ticket or the code, and stops at $5 or 40 minutes. It cites a family report by ticket key where that report already answers part of the ticket, and spends its own report on the rest; each family report it gets is cut at 6000 characters. Before a ticket's first report, two checks can settle it with no agent call. A parent ticket whose description has fewer than six words once links and markup are gone, and whose children are research tickets, waits as a parent, because their reports cover it. With JEV on, a ticket that JEV reads as already answered by a family report (see [JEV](#jev)) links that report, and the board, the dashboard and `arete research REF` show it. JEV screens the ticket next, and a ticket it flags, or any ticket while JEV is off, waits as held. The report goes to `~/.local/share/arete/research/REF.md`, the board shows its summary, and the dashboard shows it whole. When a repo the ticket names, or its source's only repo, has a GitHub remote and a layer with `research.dir`, arete also files the report in that folder. The agent reads the reports beside it and writes the new one the same way, under a short file name it picks, and arete commits it on `arete/research-REF` as `docs(research): TITLE` and opens a pull request that closes the ticket. That pull request never automerges, since the report carries web content no person has read. A report researched again keeps its file name, replaces the branch's commit and updates the open pull request. A filing that fails fails the run, and the research loop files the report again on its next tick with no agent call. The board, the dashboard and `arete research` name the file and link the pull request. A ticket researches again only when its text changes: its summary, description or comments, not its status, sprint or labels. That holds for a report that failed too: the loop tries a failed ticket again only once its text changes, and `arete research REF --run` tries it again now. When the text did change, JEV judges whether the report needs the change, and the report stands when it does not. When the JEV call fails, the ticket researches again. Research writes nothing to a ticket but its status, and that only with `[jira.research]` set; a filed report's pull request links it the way a pipeline pull request does. Left out, arete never moves a research ticket, so you can edit the report and attach it to the ticket before you move it yourself. `[jira.research]` maps `running`, `failed`, `pr` and `done` to targets read the way `[jira.transitions]` reads them, and with it set the loop moves a Jira research ticket to `running` when it starts researching, to `failed` when the research fails, to `pr` while the pull request of a filed report is open, and to `done` once the report is delivered: written, when there is no folder to file it in, or merged. A move that Jira refuses shows on the board after the report, and the loop does not try it again; a move that fails to reach Jira tries again on the next tick. When nobody else changed the ticket since triage read it, arete's own move does not count as a change to it, so it starts no new triage or research. The first tick after you set `done` or `pr` also moves the research tickets whose reports are already delivered. A research ticket arete moves to `done` leaves your open tickets, and the board keeps it under Research for 7 days with its report, as it keeps sent tickets. `arete research` lists the research tickets and where each stands, `arete research REF` prints a report, `arete research REF --run` researches one now, `--force` skips the JEV screen, the parent hold and the answered check, and `arete research --dry-run` says what the loop would do next.

When `alert_repo` is set and private, the board is also one issue there, "[arete] triage", labeled `arete-triage`, edited only when the board changes. A public or unreadable `alert_repo` gets nothing, and the output says why. Triage writes nothing to a ticket on its own except the two labels of an auto approval, and your answers reach a ticket only when you send it.

## Dashboard

`arete serve` serves one page from this machine to its owner over Tailscale, so it opens from a phone or another computer on the tailnet; `arete dashboard` serves it alone. It listens only on the machine's tailnet addresses. A request from any other tailnet login gets 403, and a change also needs the `X-Arete` header that the page sends.

- Live: the runs going now, with their start time, the last log line and the cost so far, and Stop for each; the labeled tickets the pipeline skips, with Run anyway to start one past what holds it; the harness fixes and memory facts the miner leaves for you to make by hand, each with Mark done; the tickets waiting on review comments; and the last 25 runs of every kind.
  A run's log opens below the table.
- Loops: what waits for your approval, with Approve and Reject, and a failed delivery with its error and Try again; then every loop with its switch, kind, trigger, last run, next fire and spend in the last 24 hours, and a broken loop with what is wrong.
  Open shows a loop's facts, runs, findings, sources, memory, digests and metrics.
  Start runs it now and follows its live feed, where Stop ends the run.
  Try runs it the way `arete loop try NAME` does, so its feed shows what each emit would do and sends none.
  Below the loops, a sentence drafts a new loop the way `arete loop new` does, with an optional name, repo and model, and its feed follows the draft.
  The serve line has Install serve or Restart serve when serve is not running.
- Launch: start a session on a device, model, runner, tool set, repo and budget, then talk to it and follow its feed.
- Fleet: serve's beat, with Install serve or Restart serve when serve is not running, and each device with its last probe and tools; Probe checks one again.
- Health: the heartbeat checks, the probes, the loops that report on their own, and the open alerts.
  Run the checks now starts the heartbeat.
- Triage: the triage board, with each ticket's questions, assumptions and plan folded under it, each research report in full, and each sent ticket's pipeline status and PR.
  Answer a ticket's questions there and send it to the pipeline, which starts a poll.
  Refresh triage now starts a refresh.
  A research ticket offers Research it now, Research it again or Research it anyway, through the research loop; an offer that skips JEV's screen says so beside its button.
- Trends: 30 days of cost and outcomes by day, runs by repo, eval pass rate by repo, and where the money went, by model, by runner and by loop.
- Settings: `max_parallel`, `max_parallel_per_repo` and the model for each role in `[models]`. A save changes only the lines that changed in `config.toml`, keeps the file's mode, and refuses a value arete cannot use. An empty model removes its line, so the role goes back to its default.
- Chat: console agents on the `chat` model.
  Every chat is kept in `arete.db` and listed newest first, and Open shows any of them.
  New chat starts one with no project, which works in arete's folder as the console, or on one repo from the config, which works in that repo's folder and gets its layer's checks and expertise file in its prompt.
  A chat reads arete's code, config, state and logs, and its project's code when it has one, runs `arete status`, `arete events`, `git log` and `git show`, and reads arete's GitHub issues.
  It never edits a file.
  Ask it for a change to arete and it drafts an issue on arete, which shows under that chat; nothing reaches GitHub until you press File this issue, and Drop discards it.
  Each call to the model stops at $2, and each reply shows what it cost.
  Each chat continues its own session and waits on its own answer, so several chats can wait at once.
  On the first start of this version, the old `chat.json` moves into one chat and stays as `chat.json.moved`.

## Rules

- Agents run through `claude -p` on a Claude subscription, or through `pi` for other providers and local models. Never the API SDK, never `--bare`, never an `ANTHROPIC_API_KEY`, here or on a device.
- A claude run takes no dollar cap and waits out a usage limit; budgets apply to pi alone.
- A loop fed by outside text gets only the reading tools, and its events reach the agent fenced as data.
- Agents never run git write commands. Diff size and deny paths are enforced by code after the agent finishes, not by asking nicely.
- Trust starts at `pr`. Automerge only arrives through a human: a config PR they merge, or for a layer outside the repo, their own edit to `trust.level`. A `push` emit follows the same level: under `pr`, a person lands every push.
- Alert issues carry check names and one short status line, never log contents.
- PRs to public repos cite run ids, never transcript text.
- Triage writes to a ticket only to approve it: the labels of an auto approval, or the answers and labels you send from the dashboard. Its board goes only to a private repo.
- Each verifier server runs on its own port and is stopped with its whole process group after the step.
- A JEV answer settles a question alone only at or over `confidence`; under it, the model decides. A hold or a guard stop, which a person can undo, takes a lower line.
