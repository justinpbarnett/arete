# arete

Agent loops that keep running and keep making the harness they run in better.

Arete is one global engine for every repo on the machine. Each repo gets a thin layer that says how to set up, what checks must pass, and how much the agents are trusted there. The layer is `.agents/` in the repo, or a folder outside it when the rest of the team does not use arete. Agents edit files. Arete's own code commits, pushes and opens PRs, so no agent ever holds git write access.

## Loops

| Loop | Command | When | What it does |
|------|---------|------|--------------|
| 0 heartbeat | `arete heartbeat` | hourly | Checks launchd jobs, log freshness, real probe calls, claude login expiry, the event hook, and disk. Each failing check opens one issue on `alert_repo`; recovery closes it. |
| 1 miner | `arete mine` | nightly | Reads the day's Claude Code transcripts, hook events and PR feedback, finds corrections and repeated friction, and opens harness PRs (CLAUDE.md, skills, `.agents/`, arete's own step prompts) in the repo where the fix belongs. Claude Code denies agent writes under `.claude/`, so a fix there is listed in `arete status` to make by hand. |
| 2 auditor | `arete audit` | nightly | Deterministic checks of skills, hooks and `.agents` configs, plus skills that fail or never get used. |
| 3 pipeline | `arete poll`, `arete run REPO TICKET` | every 5 min | Tickets labeled `agent`, from GitHub issues or Jira, go through plan, build, checks, fix, verify in use, review, learn, then a GitHub PR, then a watch on the PR's CI. Review comments on that PR start a revision. |
| 4 evals | `arete eval REPO`, `arete eval mine` | on demand, nightly | Runs eval tasks against a fixed commit and records the pass rate, so harness changes are measured, not guessed. `eval mine` writes new tasks from merged arete PRs. |
| 5 triage | `arete triage` | hourly | Reads your open tickets that do not have the `agent` label yet and says which are ready to build, what each one is missing, its size and a short plan. It never writes to a ticket. |

The event hook (`arete hook`) records skill use, agent spawns, tool failures, denials, interrupts and API stop failures to `~/.local/share/arete/events.jsonl`. The miner and auditor read it. Register it in `~/.claude/settings.json` for `PostToolUse` (matcher `Skill|Agent|Task|Bash`), `PostToolUseFailure` (matcher `*`) and `StopFailure`, with the command `[ -x "$HOME/.local/bin/arete" ] && "$HOME/.local/bin/arete" hook || true` and a 10 second timeout. Every path through it exits 0, so it never fails a session.

## Runs, sleep and the network

Poll does not wait for a run. It starts each run as its own detached `arete run` process and returns in about a second, so a long run never holds up the next poll. `max_parallel` sets how many runs go at once on the machine, manual runs included. A run frees its slot while it waits on its PR's CI, and takes one back before a CI fix step. A run that waits to take its slot back goes ahead of new runs, and `arete status` lists it as waiting for a run slot. Poll takes tickets round robin across repos, and a repo with a run going gets no second one.

A laptop sleeps and drops its network. When poll, triage or the heartbeat start offline, most often just after a wake, they wait up to 3 minutes for the network; if it does not come back they skip with an "offline" note and exit 0. A git or gh call that fails on a network error waits up to 30 minutes for the network, then tries once more. An agent step cut off by a dropped connection resumes its own session when the network is back. A run whose process died is put back in the queue with a comment on the ticket, and the next poll starts it again; a second death in a row fails the run as before. Heartbeat ages count only time awake, so a night asleep never reads as a stale log.

## Layout

- Code: this repo, a uv project (Python pinned in `.python-version`, everything locked in `uv.lock`). Installed with `uv tool install --editable . --python 3.13`
- Config: `~/.config/arete/config.toml` (`arete init` writes a starting one)
- State: `~/.local/share/arete/` (sqlite, events, run logs)
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
| `[models]` | Model per role: `plan`, `build` (also fix and revise), `verify`, `review`, `learn`, `uat`, `miner`, `apply`, `eval_mine`, `triage`, `chat` (the dashboard console). A role left out uses the claude CLI default, except `triage`, which uses haiku, and `chat`, which uses opus. `arete status` lists each role's model. |
| `[miner]` | `lookback_hours` and `max_digest_chars` for the nightly transcript digest. |
| `[[repo]]` | Every repo arete knows: `name`, `path`, `github`, and the fields below. |
| `[[job]]` | launchd jobs the heartbeat watches, by `label`. A `keepalive` job must have a pid; others must have last exited 0. `log` with `max_age_hours` flags a stale log. |
| `[[probe]]` | Real calls the heartbeat makes every `every_hours`: a `command` whose output must contain `expect`. A real call, because an auth check can say ready while real calls fail on an expired token. |
| `[jira]` | See [Jira tickets](#jira-tickets). |
| `[dashboard]` | `port` (default 8796) and `owner`, the tailnet login the page opens for. Default: the login that owns this machine. See [Dashboard](#dashboard). |

| Repo field | Default | Meaning |
|------------|---------|---------|
| `pipeline` | `false` | Poll this repo's tickets and run the build pipeline. |
| `mine` | `true` | The miner may open harness PRs here. |
| `public` | `false` | PR bodies cite run ids, never transcript text. |
| `tracker` | `"github"` | `github` for issues in the repo, `jira` for tickets in Jira. PRs are on GitHub either way. |
| `jira_project` | `[jira] project` | Jira project for this repo. |
| `jira_label` | `repo-<github repo name>` | Route label that sends a Jira ticket to this repo. |
| `layer` | `<path>/.agents` | Folder that holds the repo's layer. Point it outside the repo to keep arete files out of a shared repo. |
| `[[repo.part]]` | none | One per git repo under `path`, for a product split over several repos: `dir` (relative to `path`) and `github`. See [Parts](#parts). |

## GitHub tickets

An open issue labeled `agent` that the repo owner opened. Issues labeled `bug` or `fix` are fixes; the rest are features. Arete tracks progress with `agent:running`, `agent:pr`, `agent:failed` and `agent:needs-info`, and the PR closes the issue. Remove `agent:failed` or `agent:needs-info` to send it back to the queue. A ticket in `agent:needs-info` also goes back when you reply to arete's question in a comment: poll removes the label, and the next run reads the question and your answer. `arete install` creates all of these labels in each GitHub pipeline repo, so `agent` is there to pick from any GitHub client, the phone app included.

## Jira tickets

```toml
[jira]
env_file = "~/.env"
project = "ABC"

[jira.transitions]
running = "In Progress"

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

`JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN` come from the environment, else from `env_file`. `[jira.transitions]` moves tickets as the pipeline moves them: each of `running`, `pr`, `failed` and `needs-info` maps to a transition name or a target status name. A state left out only sets labels and comments. `[jira.landed]` moves a ticket once its merged PRs reach a branch, by the same kind of name. List branches in the order work travels: each poll moves the ticket to the last listed branch that holds the work of every PR, and never back. A branch holds a PR's work when it contains the merge commit, or when another PR naming the ticket key merged into it, as a port of the change to `main` does. With the table above, a PR merged into staging moves its ticket to UAT, and the release that brings staging into main moves it to Done. On every move but the one to the last listed branch, arete also posts numbered steps a person can follow to test the change by hand there. A read only step writes them from the ticket, the PR body and the PR diff. If it fails, the next poll tries again with no second comment; after 3 tries arete posts a short note that names the PRs instead. `arete jira-check` reads Jira with the token and counts the tickets waiting in each project.

A ticket runs when all of these hold:

- It carries `agent` plus exactly one route label, `repo-web` above.
- It is assigned to you and not Done.
- You added `agent` yourself, and nobody else has edited the summary or description since you last touched its labels.

Touching the labels is the approval: whatever the ticket says then, you have read. After it, only your own edits, comments and attachments reach the agent; other people's comments and attachments count only when added before the approval. Arete's own label edits go out under your token too, so they are told apart: they only swap `agent-*` state labels and always add one, and they never count as approval. Removing a state label to retry is your edit, so it approves the ticket as it reads now.

Progress shows as `agent-running`, `agent-pr`, `agent-failed` and `agent-needs-info`. A reply from you after arete's question takes `agent-needs-info` off, as on GitHub. Bug, Fire, Incident and Problem issue types are fixes, as are tickets labeled `bug` or `fix`. PR titles end with the key, like `Fix login (ABC-12)`, PR bodies link the ticket, and these PRs get no arete labels. Tickets the approval rules hold back, and tickets with a missing or doubled route label, show in `arete status` with the reason. `arete run REPO KEY --force` runs one anyway.

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
```

| Key | Meaning |
|-----|---------|
| `setup.commands` | Run in each fresh worktree before the agent starts. |
| `setup.copy` | Files copied from the main checkout into each worktree. |
| `checks` | Every check must exit 0 before review. The agent may run them while it builds. |
| `limits` | Diff size, paths the change may not touch, fix attempts (which also cap the CI fix rounds), and the run timeout. Code enforces them after the agent finishes. Every repo also denies `.agents/config.toml`, `.github/workflows/` and `.env` files. It denies `.claude/` too, since Claude Code refuses agent writes there. The planner names a change in a denied path for a human to make. The PR shows it under "Left for a person", and the verifier and the reviewer are told that a person makes it, so its absence never fails the run. |
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
- Passed on the base, skipped there, or no result there: a CI fix step reads the job log and the diff, and decides whether the PR caused the failure. When the base moved after the last push, arete first merges it into the branch, since CI tested the PR merged with that base. A merge that conflicts is undone, and the step works on the branch as it is. A check that the PR did not cause gets a note with the reason. For a check that the PR did cause, the step fixes the code. Then the checks and the diff limit run again, arete pushes the fix, and the wait starts again. `limits.max_attempts` caps the rounds, and the diff limit counts the whole branch.

After each round, arete writes a `## CI` section into the PR body. It shows what passed, each red check with its base result and reason, and the earlier rounds. When the PR still causes a red check after the last round, or the fix step fails, arete turns the PR to a draft and fails the run. The section then ends with `Arete stopped:` and the reason. The PR stays open. Review comments on it start a revision, as on any arete PR. A later run that is approved and green marks it ready again. A run gets the status `pr` only when its PR caused no red check.

## PR feedback

Review comments on an arete PR are the next request. Poll reads the PR's reviews, line comments and conversation comments from the repo owner. A submitted review starts a revision at once; loose comments start one after 10 quiet minutes, so a batch goes in one run. Pending reviews and arete's own comments, which carry a hidden marker, are left out.

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

- Ready: the pipeline could build it as written. The board says what to add: `agent`, plus the route label for Jira.
- Questions: what is missing before it could be.
- Size S, M or L. L means split it, so it is never ready.
- The repo it belongs in, and a plan of up to 6 steps.
- Not code work: a meeting, research, a decision, an account, a check done by hand. One line with why, and nothing more.

The step may read only inside the repos it is given, has no MCP servers, and costs at most $0.30 a ticket. A ticket gets a new call only when its updated time changes, at most 20 calls a refresh; the rest wait for the next. `--refresh` triages what changed, then shows the board; `--refresh --dry-run` lists what it would triage with no model call and no writes; `arete triage REF` shows one ticket in full. A launchd job refreshes hourly.

When `alert_repo` is set and private, the board is also one issue there, "[arete] triage", labeled `arete-triage`, edited only when the board changes. A public or unreadable `alert_repo` gets nothing, and the output says why. Triage never adds `agent` and never writes to a ticket: the label is your approval, and the board only tells you where it would pay off.

## Dashboard

`arete dashboard` serves one page from this machine to its owner over Tailscale, so it opens from a phone or another computer on the tailnet. It listens only on the machine's tailnet addresses. A request from any other tailnet login gets 403, and a change also needs the `X-Arete` header that the page sends. `arete install` adds it as the launchd job `com.arete.dashboard`, which launchd starts again when it stops, and the default config has the heartbeat watch it.

- Live: the runs going now, with their start time, the last log line and the cost so far; the labeled tickets the pipeline skips; the tickets waiting on review comments; and the last 25 runs. A run's log opens below the table.
- Health: the heartbeat checks, the probes, the loops, and the open alerts.
- Triage: the triage board, with each ticket's questions folded under it.
- Trends: 30 days of runs and cost by day, of runs, pass rate, median time and cost by repo, and of eval pass rate by repo.
- Settings: `max_parallel` and the model for each role in `[models]`. A save changes only the lines that changed in `config.toml`, keeps the file's mode, and refuses a value arete cannot use. An empty model removes its line, so the role goes back to its default.
- Chat: a console agent on the `chat` model. It reads arete's code, config, state and logs, runs `arete status`, `arete events`, `git log` and `git show`, and reads arete's GitHub issues. It never edits a file. Ask it for a change and it drafts an issue. The draft shows under the chat, and nothing reaches GitHub until you press File this issue (Drop discards it). Each call to the model stops at $2, and each reply shows what it cost. Messages continue one session until New chat.

## Rules

- Agents run through `claude -p` on a Claude subscription. Never the API SDK, never `--bare`.
- Agents never run git write commands. Diff size and deny paths are enforced by code after the agent finishes, not by asking nicely.
- Trust starts at `pr`. Automerge only arrives through a human: a config PR they merge, or for a layer outside the repo, their own edit to `trust.level`.
- Alert issues carry check names and one short status line, never log contents.
- PRs to public repos cite run ids, never transcript text.
- Triage reads tickets and never writes to them. Its board goes only to a private repo.
- Each verifier server runs on its own port and is stopped with its whole process group after the step.
