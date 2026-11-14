# arete

Agent loops that keep running and keep making the harness they run in better.

Arete is one global engine for every repo on the machine. Each repo gets a thin layer that says how to set up, what checks must pass, and how much the agents are trusted there. The layer is `.agents/` in the repo, or a folder outside it when the rest of the team does not use arete. Agents edit files. Arete's own code commits, pushes and opens PRs, so no agent ever holds git write access.

## Serve

`arete serve` is the one daemon. It fires each loop on its trigger (a cron line, an interval, a source with new events, a webhook), runs the fired loops through their queues, closes idle sessions, writes a beat to `serve.json` every 20 seconds, and serves the dashboard. `arete install` runs it at login: one launchd agent, `com.arete.serve`, on a Mac, or one systemd user unit, `arete-serve.service`, on Linux. It retires the old per loop agents (`com.arete.heartbeat`, `poll`, `mine`, `audit`, `eval`, `triage`, `dashboard`), since each of those now runs as a loop under serve. `arete install --dry-run` prints what it would write. A loop run killed with serve settles as interrupted, and serve picks up its queues again when it starts.

Serve restarts itself when its own code changes.
On each tick it looks at the `.py` files of the arete package and the dashboard's web files, and once a change has held still for one tick and serve has no source poll, dashboard request or chat message of its own in flight, it checks that the new code imports and then replaces itself with a fresh serve in the same process.
Runs and sessions are processes of their own, so they keep going across the restart, and the fresh serve reaps each one when it ends.
New code that does not import is named in the serve log, and serve keeps the code it started with.
A self restart is not a start, so loops with `run_at_load` do not fire again.
So a push to arete's own main, followed by a fast forward of the checkout serve runs from, reaches serve with no restart by hand.

## Loops

A loop is a folder with a `loop.toml`, a `prompt.md` when an agent does the work, and any script it calls. [arete/loops/README.md](arete/loops/README.md) is the whole format: the four kinds (command, watch, climb, search), triggers, sources, emits, budgets, the queue, the device it runs on, and the safety rules. Arete ships these:

| Loop | Kind | When | What it does |
|------|------|------|--------------|
| `heartbeat` | command | hourly, and when serve starts | Checks serve's beat, the loops, log freshness, real probe calls, claude login expiry, the event hook, and disk. Each failing check opens one issue on `alert_repo`; recovery closes it. |
| `mine` | command | nightly | Reads the day's Claude Code transcripts, hook events and PR feedback, plus arete's own recorded runs that failed, needed a retry, hit a budget or ran long, finds corrections and repeated friction, and opens harness PRs (CLAUDE.md, skills, `.agents/`, arete's own step prompts) in the repo where the fix belongs. Claude Code denies agent writes under `.claude/`, so a fix there is listed in `arete status` to make by hand. |
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

Every run of every kind, pipeline, loop, session, chat and draft, is a row in one runs table with its trigger, model, runner, device, cost and tokens. Each model call is a step under its run; a step row also keeps `first_context_tokens`, the input tokens of the call's first model turn with cache reads and writes, so the cost of what a step loads before it does any work shows, and 0 when the runner does not report it. A run's live feed holds its steps, the tools the agent calls, and a command loop's output as it prints.

An emit that changes something outside arete, a ticket, a PR or a push, can wait for approval: `arete approve` lists what waits, and `arete approve ID` approves one and `arete approve ID --reject` drops it.
A ticket, PR, push or digest that fails to deliver stays listed with its error, and so does a finding handed to a loop that is off.
Try again on the dashboard, or `arete approve ID`, delivers it again, so a finding whose ticket hits a GitHub outage is filed then instead of being lost.
A `push` emit lands a climb or search change on the default branch with no PR.
It waits for a person while the repo's `trust.level` is `pr`, and under `automerge` it waits only when it touches a path the repo denies, or when its `approve` table says so: too many lines, a deleted file, or a listed path.
With `reviewer` in that table, a model such as opus reviews each held push first, and a push it approves with high confidence lands, unless the trust level, a path the repo denies or a listed path keeps it for a person.
A push that fails to land stays as a failed approval.
A `loop` emit hands each finding to another loop, so one loop can find and another can fix.
[arete/loops/README.md](arete/loops/README.md) has the rules for both.
A loop with `[trigger] webhook = true` takes events at `/hook/<loop>`; `arete webhook secret NAME` prints the URL and its token.

## On-call

The watcher notices operational failures on its own and keeps one durable incident for each underlying symptom.
JEV decides whether an incident may wake a repair; no repair starts yet.

```toml
[oncall]
enabled = false
vision = "A payments service that must stay up"
scope = "Bug fixes in the payment worker only"
confidence = 0.8
injection = 0.5

[[oncall.source]]
path = "~/Library/Logs/myapp/*.log"
pattern = "ERROR|panic"
repo = "myapp"
check = "make check"
vision = "The checkout flow matters most"
scope = "Only the checkout module"
```

`enabled` defaults to false. With it off, `arete serve` does no on-call work and creates no cursor or incident.
`vision` and `scope` are owner text; a source's own `vision` or `scope` replaces the `[oncall]` one for incidents from that source.
`confidence` and `injection` are numbers from 0 to 1 and default to `[jev] confidence` and `[jev] injection`.
When on, serve scans on its tick, on the poll pool, so a slow scan never blocks the tick.
Each `[[oncall.source]]` needs a `path` (a glob is fine); `pattern` (a regex, default the log source's), `repo` (a `[[repo]]`) and `check` are optional.
`repo` and `check` are recorded for a later repair part and start nothing now.
When more than one `[[oncall.source]]` entry with different `vision` or `scope` text matches one file, JEV is asked only `injection` and `actionable`, and a result that would be `wake` is stored as `unsure` for the owner, so no entry's text can ignore or wake it alone.
An entry matches a file when its glob finds that file by any path, so a symlink, a hard link or another letter case names the same file, and it also matches the rotated `.1` copy of any file its glob names, which its watcher drains, even after the file itself was renamed away.
Which entries match a file is looked up when its lines are read: a listed file renamed, re-created or linked anew after the scan began is matched again before its lines are read, and a listed file no `[[oncall.source]]` glob finds any more is treated like a gone file, never as one under the `[oncall]` text.
An incident keeps the set of `vision` and `scope` its file was under when its lines were read and is decided under that set, so a file deleted, rotated or reached through a repointed link before the decision changes nothing.
Lines drained from a rotated `.1` copy are read under every entry that matches the copy and every entry that matches the file now at its old path, so a rotation never drops an entry the lines were written under.
Lines read from the rotated copy of a file that is gone, or from a listed file no glob finds any more, have no entry to match, so a result that would be `wake` is stored as `unsure`, unless the config holds only one set.
A result that would be `wake` is also stored as `unsure` when the set an incident was read under is no longer in the config.
An incident opened before incidents recorded their set joins a repeat of it, which records the set the repeat was read under.
This holds when the old key differs from the new one, for a run row's incident keyed without its repo and for a file under several entries keyed by one entry's path, as long as the incident's file is still under the repeat's set and both come from the same repo.
Decided before any repeat, such an incident is decided under the only set the config holds, or, when the config holds more than one, it is `unsure` with a reason that says it predates recorded sets.

**Sources.** Arete's own files are watched without being listed: the `*.log` files in the log folder, each live run's `run.jsonl`, `heartbeat.json` and serve's beat file.
Other application or service logs are watched only when listed as `[[oncall.source]]`.

**Cursors.** Each file has a durable cursor (`oncall:` in the `cursors` table) holding its offset, inode and a hash of the bytes before the offset, and what it last saw of the rotated `.1` file, so a rotation is found when nothing had been read yet and a drained `.1` is not read twice.
A first sight of a file reads nothing old; a file that appears later is read from its start.
A cursor is kept for a file that a glob lists again by the end of the scan, so a file renamed back or re-created while the scan ran is not read twice.
Rotation, including the copy-then-truncate of `arete install`'s log rotation, truncation and a restart neither replay old lines nor lose new ones.
A new `.1` that the current file still begins with is a copy waiting for its truncate only while the current file has not been written since the copy; otherwise the lines beyond the cursor are read from the `.1` and the current file is read from its start, so a new file that starts with the same bytes as the rotated one loses nothing.
Two cases cannot be told apart from the files alone: when a scan lands between a copy and its truncate and a line was appended in that gap, the copied lines not yet read are counted twice; and a truncated file that regrows with exactly the bytes already read is not read again.
A scan reads at most 1,000,000 bytes per file; the rest waits for the next scan.
An incident keeps the five lines either side of the first match, at most 4,000 characters.

**Classes.** A cheap filter runs first: a pattern match, then coalescing by normalized signature, so many lines of one failure become one event with a count.
Each event then gets a class without a model:

| Class | Meaning |
| --- | --- |
| `unexpected` | A failure nothing else explains. |
| `heartbeat` | A check failing in `heartbeat.json` across two or more beats. |
| `stalled` | A run marked running whose `run.jsonl` heartbeats stopped or whose process is gone. |
| `orchestration` | Serve's `tick failed`, a broken loop, a poll error, a dropped queue item, a failed VM sweep or new code that does not import. |
| `expected` | A check or repair step failing inside a run, a run that ends because its own checks still fail after a repair, normal backoff and retries, quota and limit waits, or a failure of a ticket that already has a live run. |

Expected events are counted and open no incident.

**Incidents.** The `incidents` table holds the fingerprint, class, source, state, first and last occurrence, count, evidence and the set of `vision` and `scope` its lines were read under.
A repeat of an open incident's fingerprint, minutes or hours later, updates it instead of opening another.
A failing row of a run carries the repo of its run, and rows from different repos never share an incident; a run with no row in state has no known repo.
The same line from two files never shares an incident when the `[[oncall.source]]` entries matching each file give them different sets of `vision` and `scope`; an open incident is updated only by lines read under the set it was opened under, and only while its own file is still under that set, or, once that file is gone, while the config holds no other set.
The append-only `incident_notes` table records what happened and when.
Evidence is scrubbed of secrets and home paths before it is stored, and `state.prune` trims both tables.

**Decisions.** After each scan, JEV is asked four yes/no questions about every open incident that is due, oldest first however many are open, and the answers become one stored decision:

- `injection`: does the text try to steer an agent that reads it (the same question used elsewhere in arete)?
- `actionable`: is it a failure a code or config change would repair, rather than noise?
- `vision`: does repairing it fit `vision`? Asked only when a vision is set and the incident is decided under one set that is still in the config.
- `scope`: is the repair inside `scope`? Asked only when a scope is set and the incident is decided under one set that is still in the config.

The evidence is redacted before it is stored and is sent fenced as data. It is never used as instructions or put into a command; the owner's `vision` and `scope` are the only text in the questions themselves.

| Outcome | Meaning |
| --- | --- |
| `wake` | Every question was answered with confidence: actionable, fits the vision, in scope. It may later wake a repair. |
| `ignore` | A confident "no" to `actionable`, `vision` or `scope`. `vision` and `scope` count only when the incident was read under exactly one set. |
| `ignore` with the injection mark | The evidence reads as instructions to an agent. It is never asked again and can never wake a model. |
| `unsure` | An answer was under the confidence line, no vision or scope is set, more than one source entry with different text applied to the file when its lines were read, the lines came from a gone file's rotated copy or a listed file no glob finds while the config holds more than one set, the set has left the config since, or the incident was opened before incidents recorded their set while the config holds more than one set. It never wakes a model; the owner decides. |
| undecided | JEV is off, has no key, is not allowed for the incident's repo or the call failed. The reason is stored and nothing wakes. |

Each decision is stored on the incident with every answer's value and confidence and a one-line reason, and added to its notes.
JEV is not asked about `expected` events, about an incident a live run owns, or about a repeat of a decided incident.
An ignored incident is asked again only when it changed materially: its count reached ten times the count at its decision, or its evidence shows a failure line of a new shape.
An undecided incident is tried again after 60 seconds, then 120, doubling up to an hour.
Before a new incident is opened, JEV may match a differently worded symptom to an open incident of the same repo and the same vision and scope; a confident match updates that incident instead.
A symptom whose twin check reads as an injection is opened already marked and can never wake a model.
Twin checks and decisions share one budget: at most 20 JEV evaluations per scan, and the rest wait for the next.
A symptom from a run that is still live gets no twin check and no decision until the run ends.
All calls go through `oncall.ask_jev`, one function a test harness can replace.

```
arete oncall             # incidents, newest first, each with its decision, confidence and reason, then the watcher's state
arete oncall show ID     # one incident with its decision, answers, evidence and notes
arete oncall scan        # one scan now
```

`--json` prints one JSON line per incident (`scan --json` prints the summary).
While the watcher is on, the heartbeat's `oncall:scan` check fails when the last scan crashed or is more than five minutes old.
The regression checks are in `tests/test_oncall.py` and run with `uv run python -m unittest discover -s tests -v`.

## Runners, devices and sessions

A model name picks its runner. A claude alias or a `claude-*` id runs on the `claude` CLI on a Claude subscription. A `provider/model` id, such as `deepseek/deepseek-flash` or `ollama/gemma4`, runs on `pi`. A `gpt-` or `codex-` name, in any case, runs on the `codex` CLI (see [Codex](#codex)); `openai/<model>` stays on pi. A `pi:` or `codex:` prefix forces that runner, and a `replay:` prefix answers from files (see [Replay](#replay)). Claude and pi both report tokens and cost per step, and codex reports tokens; `[pricing]` sets a price for a model whose runner reports none, and a provider in `local_providers` costs nothing. Budgets meter pi and Codex API-key spend. Codex has no per-call dollar cap. A run on claude has no dollar cap: the subscription carries it, and its cost is still recorded, though every sum arete shows you counts only what you pay. When claude reports a usage limit, the call moves to the next allowed route of its tier at once (see [Steps](#steps)); only when no route is eligible does it wait until the earliest limit resets, resuming its session where it can. The serve holds a queued loop only when no route of a required call is eligible, the heartbeat counts a held loop as healthy, and chat and loop drafts return the limit at once instead of waiting.

Local pi steps use the file builtins behind Read, Edit, Write, Grep, Glob and LS; executable tools are withheld. Arete explicitly loads its trusted tool guard with owner resource discovery disabled. The guard denies tool access to credential paths while pi's SDK backend can still read and refresh its credential store. Public runtime source stays readable, and tools cannot edit or overwrite it. If the guard cannot load, pi stops before calling the model.

`claude -p` stays open after its answer until each command the agent started in the background ends, so a server left running holds a step to its time limit and loses its answer.
Each claude step that can run commands is told to stop with TaskStop each background command that does not end on its own before it answers, even when its task asks to keep one running.
On a local step, `arete background` runs as a Stop hook: when the agent answers while a background command still runs, it names the command and has the agent stop it and answer again, or end its turn again when it waits for a command that will end.
A step that times out gets SIGTERM, and what is left of its processes 30 seconds later gets SIGKILL.

### Codex

The `codex` runner runs OpenAI models through the Codex CLI on your own `codex login`.
A step picks it with `runner = "codex"` in a step table or a tier, a `codex:<model>` name, or a bare `gpt-` or `codex-` name.
Each call runs `codex exec --json` with the folder as `-C`, the model as `-m`, the effort as `model_reasoning_effort` and the prompt on stdin.
The step's session keeps codex's thread id with runner `codex`, and a later round resumes it with `codex exec resume <thread>`. Switching runners starts a fresh thread; repair prompts carry the saved ticket, plan and builder summary. Claude sessions never resume in Codex.
Public PR reports and tracker comments pass through outbound redaction before publication; raw local evidence stays intact.
A ChatGPT login is a subscription, as claude is: the step shows `subscription`, counts nothing in paid sums, and still records its reported cost and tokens.
An API key login is metered, priced at the model's `[pricing]` price, since codex reports no cost.
Subscription holds follow the account and provider family: matching Codex and pi openai-codex accounts share holds. Different accounts stay independent.
The heartbeat checks `codex login status` beside `claude auth status` once any step, role or configured loop resolves to codex, and raises `codex:auth` when you are logged out.
Codex runs on the local device only; a loop or session that would run it on another device is refused in one line.

Arete runs codex with your `CODEX_HOME` (default `~/.codex`) in place and copies nothing out of it.
It passes `--ignore-user-config` and `--ignore-rules`, and turns off your own hooks, skills, memories, plugins, apps and MCP servers, so only the step's `mcp` servers load.
Arete disables automatic project instruction loading with `project_doc_max_bytes=0`. Codex 0.167.2 still loads global `CODEX_HOME/AGENTS.md` or `AGENTS.override.md` despite that setting and `--ignore-user-config`, and exposes no switch to exclude them from `exec`. If either exists, or arete cannot check, it withholds the call before starting codex and explains why; your setup and login stay untouched. `arete steps` includes this limitation in its codex notes.
When `instructions` holds `repo` (the codex default), arete supplies the repo's `AGENTS.md` files from the git root through the step's folder as developer instructions; `AGENTS.override.md` wins over `AGENTS.md` in each folder. Files above the repo and in `CODEX_HOME` stay out. `instructions = []` supplies none.
When a caller supplies MCP environment values, they stay in the driver environment; only their names reach Codex's `env_vars` configuration, and those names are excluded from shell commands. Reserved login/environment variables and conflicting server values are refused.
Only servers granted by `mcp__<server>` or `mcp__<server>__<tool>` rules are enabled. Exact tools receive `enabled_tools` and per-tool `approval_mode = "approve"`; an explicit whole-server grant sets `default_tools_approval_mode = "approve"`, while the shell keeps its own approval and network restrictions.
An absolute executable named as the first word of an allowed `Bash(...)` rule gets read and execute access to that exact file. Adjacent private files, secrets, login directories and `gh` remain denied. Project `.codex/config.toml` or `.codex/hooks.json` is refused because it can override isolation.

Codex always has a shell and file reads, so steps must include `Read`, `Glob` and `Grep`. It disables browser tools, subagents and web search; configured MCP servers stay separate.
Its sandbox and the `arete codex-guard` PreToolUse hook keep the promises claude's tool rules keep:

| Promise | claude | codex |
|---|---|---|
| No git writes | `--disallowedTools` rules, and no shell rule beyond read only git | The hook refuses every git command but a read (`status`, `diff`, `log`, `show`, `blame`, a branch or stash list and the like), and every `gh` command. It handles command strings and arrays, shell options such as `bash -lc`, wrappers such as `env`, and compound commands. This check applies wherever the git folder lives; the profile also keeps each writable root's `.git` and its resolved git/common directories read only, including inside temporary clones, and denies the `gh` executable. |
| A read only step writes nothing | no write tools | A `read_only` permission profile: no file writes and no network. |
| A build step writes only its worktree | write tools in the worktree | A `workspace_write` profile: the step's folder, its extra folders, `/tmp` and `$TMPDIR`, and no network. |
| Secret files stay out | the guard hook | The profile denies every guard secret name pattern, including `.env.*` and `id_rsa*`, and credential directories recursively across the filesystem, including skipped dependency folders and arbitrary depths. It also excludes `CODEX_HOME`, both the active and original arete configuration directories, Claude's and Pi's private directories and any additional known secret paths. Protection does not depend on file discovery or a command naming a secret. Matching example and template files are conservatively excluded too. The hook also refuses commands naming secrets and patches targeting them. |
| The guard's judge | `arete guard` with JEV | The same JEV judge, run by the hook. |
| A timeout stops the step | SIGTERM, then SIGKILL after 30 s | The same. |

Codex 0.167.2 on macOS cannot remove directories created under the workspace-write profile once the recursive secret denials are present: `rmdir`, `shutil.rmtree` and `TemporaryDirectory` cleanup fail with EPERM. The secret protections stay as they are. Steps that declare `directory_cleanup` (verify by default, inherited by eval_grade) probe this once per codex version with a fixed Python script under `codex sandbox`, launched the way a codex step launches, which makes no model call. `able` and `denied` are cached in `codex-cleanup.json` in the state folder and reused until the codex version, platform or script changes; `unchecked` is never cached, so the next selection probes again. `denied` and `unchecked` (a timeout, a missing binary, an unsupported platform, a placeholder secret that was not denied, mixed results, or a `sandbox_apply` refusal) both make the step's codex route `ineligible`. Selection then takes the next configured route that is ready and allowed by `payment_modes`; with `fallback = false` the step is refused without a call. Steps without the key, such as review and other read only codex work, never probe and keep codex. A repeated EPERM in verification on codex is this restriction: move the step to another runner or keep a fallback, and do not retry. The heartbeat shows `codex:cleanup` as a warning when any step uses codex. The probe runs only from an unconfined process (the service, the heartbeat or an operator shell); inside another seatbelt `codex sandbox` cannot start and the result is `unchecked`. `factory.fingerprint` now uses a single temporary index file, so it no longer fails on directory cleanup. Inside the codex sandbox `.git` stays read only, so fingerprint returns no tree id there, and the Git and replay lifecycle stays unsupported on codex.

A timed out codex call keeps any thread id already emitted, so a later fix can resume it. Network access is off inside the agent sandbox, so a verify command cannot `curl` the served app; a configured Playwright MCP server runs outside that sandbox, except under trusted scratch, where the only server allowed is the engine's pinned browser inside its own profile (see trusted scratch below).

Claude plan logins run only on the claude runner; Claude models on pi need an Anthropic Console key. For other providers, owner OAuth logins precede API-key routes. Before each local pi call, Arete resolves the provider and chooses an available login without an account hold, then a supported key from `~/.config/arete/pi.env` or Arete's environment; `pi.env` wins. A limited login moves to the next allowed route at once, a key included, unless `[routing] payment_modes` or `fallback = false` rules that out; a held login with no other allowed route waits. A provider missing both fails before launch and names the locations checked; unauthenticated local providers still work. Codex keeps using its ChatGPT login when available and stays isolated from ambient API keys.

Pi login calls share the owner's account, usage limits and refresh state through an auth symlink to the original `PI_CODING_AGENT_DIR/auth.json`, defaulting to `~/.pi/agent/auth.json`. Settings and model definitions come from that same owner directory. Arete never copies or rewrites that file. Separate `pi-login` and `pi-key` directories under Arete state hold private configuration views for each invocation; key views contain no logins, and login environments exclude API keys and credential overrides. Noncredential model settings survive. OAuth is unmetered only for providers explicitly marked as subscriptions by the installed SDK. Those calls have zero charged cost and no dollar cap, retaining reported cost, tokens and cache counts. OpenRouter OAuth yields a wallet API key and remains metered, as described in its [OAuth guide](https://openrouter.ai/docs/guides/overview/auth/oauth). Paid OAuth and key calls retain pricing and budget enforcement, checked against their selected route before launch. Remote pi calls are refused because Arete cannot validate or isolate the device's credentials; run pi locally or use the claude runner.

Installed Pi 1.5.2's `FileAuthStorageBackend` writes through the auth path in place with `writeFileSync`. Its `proper-lockfile` protocol resolves symlinks and atomically creates the canonical auth file's `.lock` directory, updates its timestamp and removes it on release. The login symlink therefore shares both refresh tokens and locking with owner Pi. Arete's macOS confinement allows those writes to the canonical file and lock only, without granting the owner directory; its trusted tool guard still denies credential reads, writes, aliases and searches. Explicit `--extension` loads under `--no-extensions`, and extension startup errors stop normal print mode before model execution.

`~/.config/arete/devices.toml` lists the machines loops and sessions run on, besides `local`:

```toml
[device.buildbox]
kind = "ssh"
host = "buildbox"
dir = "/root"
note = "Hetzner box"
```

`arete devices` lists them with their last probe; `arete devices probe NAME` checks the device answers and lists which of `claude`, `pi`, `codex`, `arete` and `ollama` it has.
Arete's ssh never forwards your ssh agent, so an agent on a device cannot use your keys.

A device of kind `exe` is a sandbox on [exe.dev](https://exe.dev).
Each run gets a VM of its own, works on it over ssh, and deletes it when the run ends.
Arete reaches exe.dev as `ssh exe.dev` with your own ssh key, so it stores no exe.dev key.
Every exe.dev VM shows exe.dev's own host key, so arete checks any `*.exe.xyz` host against your `known_hosts` line for `exe.dev`, and writes no line of its own.

```toml
[device.sandbox]
kind = "exe"
template = "arete-template"   # a VM to copy for each run; leave it out to start from exe.dev's own image
egress = "integrations"       # integrations | all; leave it out for "all", or for the template's own policy on a copy
note = "a fresh sandbox per run"
```

A sandbox is named `arete-` and its run id, and is tagged `arete`; a copy of a template also keeps the template's tags.
`keep = true` leaves the sandbox up after the run and logs the `ssh exe.dev rm NAME` that deletes it; exe.dev bills it until then.
It keeps only a sandbox that came up and passed the checks below, and arete deletes one that it refuses or that never answers.
A probe of an `exe` device makes a real sandbox, checks that it answers, and deletes it.

Agents in a sandbox run on pi, which reads `~/.pi/agent/models.json` on the VM; a loop or session that would run claude there is refused.
Make the template a regular VM with `ssh exe.dev new --name=arete-template`, and give it a `models.json` whose providers go through exe.dev integrations:

```json
{
  "providers": {
    "openrouter": { "baseUrl": "https://openrouter.int.exe.xyz/api/v1", "apiKey": "integration" },
    "exe-llm": { "baseUrl": "https://llm.int.exe.xyz/openai/v1", "api": "openai-responses", "apiKey": "integration",
                 "models": [{ "id": "gpt-5.4-mini" }] }
  }
}
```

exe.dev adds the real key at the integration, so the VM holds none, and `egress = "integrations"` lets the sandbox reach the integrations alone.
`exe-llm` is exe.dev's own LLM gateway, which every VM can reach; a loop or session asks for it as `exe-llm/gpt-5.4-mini`.
OpenRouter needs an integration that holds your key, attached to the `arete` tag.
This command reads the key from `~/.config/arete/pi.env` and hands it to exe.dev on stdin, so the key stays out of your shell history:

```
sed -nE 's/^(export )?OPENROUTER_API_KEY=//p' ~/.config/arete/pi.env | tail -n 1 | tr -d "\"'\n" | ssh exe.dev integrations add openrouter --name=openrouter --key=- --attach tag:arete
```

A model is then `openrouter/` and an OpenRouter model id.
Pi calls in these sandboxes are refused because Arete cannot validate their credentials.
A probe, a loop run or a session refuses a sandbox that could push, and deletes it, even on a `keep = true` device.
The refusal names what arete found and never prints a value.
Arete looks in the login's home and environment for:

- `~/.git-credentials`, `~/.config/git/credentials`, `~/.netrc` or a gh login, also under `$XDG_CONFIG_HOME` or `$GH_CONFIG_DIR`
- a git `credential.helper`, also one for a single URL, or a `url.insteadOf` that holds a credential
- `$GH_TOKEN`, `$GITHUB_TOKEN` or their `_ENTERPRISE_` forms, in the environment or set in a shell startup file or `/etc/environment`
- a private key anywhere under `~/.ssh`, or a `~/.ssh` that arete cannot read all of

It also refuses a sandbox that one of these integrations reaches:

- a `github` integration, unless it is `--readonly`
- a GitLab, Bitbucket, Forgejo, Gitea or GitHub integration from the catalog, which has no read-only form
- an `http-proxy` integration whose target is on a public forge: github.com, gitlab.com, bitbucket.org, codeberg.org, gitea.com, sr.ht, dev.azure.com, visualstudio.com or gitee.com
- an `http-proxy` integration whose target arete cannot read
- an `ssh-key` integration, since arete cannot tell which hosts take its key
- an `exe-dev` integration, which lets the sandbox use exe.dev as you and so attach a GitHub integration to itself

An integration reaches a sandbox through `auto:all`, a tag the sandbox carries (`arete`, and on a copy the template's tags), or `vm:` and the sandbox's name.
A team integration reaches it through its tags, and one that lists no attachments counts as reaching it.
An attachment of a form arete does not know counts as reaching it, such as a wildcard, a list, or a time-boxed grant written after the name.
Arete cannot tell whether other integrations can push, so keep these off what a sandbox reaches:

- an `http-proxy` to another forge, such as a self-hosted one
- an External Connection to a private network
- a VM-to-VM proxy to a VM that holds credentials
- cloud credentials through `wif` or a token-mint service
The check reads the login's own home, so give root and any other user on the template no git credentials either.

A device of kind `proxmox` clones a template VM on a Proxmox VE host for each run:

```toml
[device.pve]
kind = "proxmox"
host = "root@pve.lan"         # the Proxmox VE host, reached over ssh
template = "9000"             # the VM id of the template to clone
user = "root"                 # the login on the clone; root when left out
note = "a linked clone per run"
```

Each run gets a linked clone of the template, named `arete-` and its run id.
Arete starts the clone, asks the QEMU guest agent for its address, and reaches it over ssh with the Proxmox host as the jump host.
So the template needs `qemu-guest-agent` running and your ssh key in the login's `authorized_keys`.
Arete destroys the clone when the run ends, unless the device sets `keep = true`.
Before it destroys a clone, arete checks that the VM id still holds a VM of that name.
A probe of a `proxmox` device reads the template's config and makes no clone.

Arete writes each sandbox and proxmox clone to its state database before it asks for one, and strikes it off once a delete confirms it is gone.
`arete loop stop` and `arete session stop` delete the VM of a run that died before it could.
Every 5 minutes, and right after it reaps a dead run, `arete serve` deletes any VM still on that list whose run has ended, and any probe VM older than 10 minutes.
That catches a delete that failed, a create that got no answer or was stopped, and a probe that was killed.
A create that ssh could not send, because it could not reach or log in to the host, leaves nothing to delete, and arete strikes it off at once.
A create that the host refuses is struck off once a delete confirms that nothing is left.
A connection can also drop after the VM is made, so a create with no answer stays on the list for 10 minutes even when a delete finds nothing, since the host may still finish it.
A run whose VM could not be deleted says so in its summary, with the command that deletes it.
After 7 days of failed deletes, `arete serve` logs that command and stops trying.
A VM of a `keep = true` device leaves the list once it is up, so arete never deletes it.

A session is an agent you start by hand and talk to, on any device, model and tool set:

```
arete session start "look at why the nightly build is slow" --device buildbox --model sonnet --tools Read,Grep,Bash --attach
arete session send ID "now try the cache"
arete session attach ID | stop ID | list
arete session start --task [--repo NAME [--draft-pr]] "what to do"
arete session report ID
```

`--repo` gives a local session a new worktree of a config repo. A session on another device gets a fresh clone of the repo's `github` project in the device's `dir`, on a new branch `session/<id>` from the repo's base branch. Arete mints a GitHub App token for that one repository with `contents: read` only, sends it to the device on stdin, and the device uses it for the clone alone through a git credential helper: the token is never in the repo, a prompt, a log or a record, and the device keeps none, so it can never push. The session's work is committed on the device after each turn (paths the repo layer denies, such as `.github/workflows/**` and `.env`, are left out), carried back as a git bundle over ssh, and fetched into the config repo's own folder. The controller pushes `session/<id>` to `origin` with its own credentials, as a local session does; the push is held, and the work only fetched, when any commit of it touches a denied path, even if a later commit removes that path. An `exe` device needs egress that reaches github.com, and the controller needs `openssl` to sign for the app. Without `[github_app]` set (see [Global config](#global-config)), a remote session with a repo is refused and names the missing setting; a repo with no `github` starts only on `local`. A device with no repo starts in its `dir`. `--budget` caps the whole session in dollars when it runs on pi; a claude session has no cap. A turn after the first that cannot continue the session's agent session, because its route moved to another runner or account or no earlier turn kept one, starts a new agent whose prompt holds the first message, the end of the session log and the new message. A session with no message for `session_idle_hours` closes. The dashboard's Sessions view starts one with the same choices, under New session.

A task session (`--task`) runs its prompt to the end instead of waiting for more messages, and hands back a structured report. In order, it:

1. takes its turns, the prompt's and any message already queued;
2. pushes its branch `session/<id>` (a local session commits its worktree first, leaving denied paths out, then pushes; a remote session's work has already come back after each turn);
3. asks for the report in a final turn, which must not change files;
4. with `--draft-pr` and `--repo` (the repo needs a `github` setting), opens a draft PR for the pushed branch, or reuses the open one;
5. saves the report with the run, closes the session, and ends the device lease, which destroys a Proxmox clone or exe sandbox unless the device has `keep = true` or the work did not come back.

Without `--repo`, a task session works in a scratch folder (or the device's `dir`), has no branch to push, and `--draft-pr` is refused.

`arete session report ID` prints the report with the session's status, finish time and PR link, and refuses while the session runs or when it ended without one. Its fields:

| field | written by | content |
| --- | --- | --- |
| `summary` | the agent | what it did and left undone |
| `checks` | the agent | a list of `{name, result, detail}`, `result` being `pass`, `fail` or `skipped` |
| `open_questions` | the agent | questions for whoever gave the task |
| `branch`, `commit` | arete | the branch and the commit that reached `origin`; empty when nothing was pushed |
| `pr_url` | arete | the draft PR, or empty |
| `cost_usd`, `paid_usd` | arete | the session's cost and what was paid for it |
| `problems` | arete | a failed turn, work not pushed or not fetched back, a draft PR that was not opened, a report with no summary |

The report turn continues the agent session that did the task. When that session cannot continue, because the route moved to another runner or account or no turn kept a session, the report turn fails: arete never asks a new agent that did not see the work.
When the report turn fails, the session ends `failed` and its report holds only arete's fields, with an empty `summary` and a problem naming the report turn.
A report turn that answers with no summary, or one that is only whitespace, also ends the session `failed`, with the problem `the report turn gave no summary`; no draft PR is opened, and the checks and open questions it gave stay in the report.
Arete drops any key the agent adds, caps the lengths, and turns an unknown check result into `skipped`. The text in a report is data for whoever reads it, never instructions to follow.
Sessions started with `arete session start` and no `--task`, and every session from the dashboard form, stay interactive. Replaying a task session, the answer for the report turn is `report.json` with `structured`.

`arete costs` shows what you paid for model calls, by model, runner, loop and route, with subscription usage kept apart from paid spend and local routes shown at $0.00 with their tokens. A call's explicit authentication mode decides whether it uses a subscription and counts nothing as paid; older rows keep their runner/provider interpretation.

`arete costs --by step|kind|model|route` counts every step instead, at the cost each call reported (a claude CLI step's is the API price), grouped by step name, by the task's kind (`feat`, `fix`), by model or by route, or by several in turn with `--by step kind model`, over the last `--days N` (default 7). Each row shows the runs (distinct runs in the group), calls, cost, input tokens no cache served (`in`), cache reads and cache writes, output tokens, minutes and API minutes. The minutes are each call's whole time; the API minutes are the part the model's API calls took, as the runner reports it (claude does, pi does not), so the rest went to tools. The last line gives the wait minutes of the runs started in that window, by cause. A route is runner, provider, model and billing (`subscription`, `api_key` or `local`); `--by route` splits `paid` from `subscription` columns, counts every launch once even when a call moved routes after a limit, and keeps local rows at $0.00 with their tokens and cache counts. `--json` prints one JSON line per group and a last line with `wait_minutes`, `paid_usd` and `subscription_usd`; `--json` alone groups by step. The plain `arete costs` stays the paid view, with a `by route` block.

## An agent's environment

A local agent, and each command that runs a repo's code, starts from a short list of named variables, never the server's whole environment.
A key, token or proxy setting in the server's environment stays out unless you name it.
Every such process gets these, when the server has them:

- `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, every `LC_*`, `TZ`, `TMPDIR`, `TERM`
- `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`
- `ARETE_CONFIG_DIR`, `ARETE_STATE_DIR`, `ARETE_LOG_DIR`, `ARETE_WORKTREES_DIR`, so the guard hook and `arete ask` read the same config, state and worktrees as the server
- each name in `[environment] pass`

A claude agent adds its login, `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_OAUTH_TOKEN`, and never gets `ANTHROPIC_API_KEY`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` or `CLAUDE_CODE_SSE_PORT`, even under `pass = ["*"]`.
It also gets `ARETE_AGENT=1` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.
The agent's shell, its hooks and the MCP servers it starts get the same environment.
`CLAUDE_CODE_OAUTH_TOKEN` therefore reaches the agent's shell too; a login made with `claude auth login` stays in the CLI's own store instead.

A pi key call gets its selected supported provider credential, including configured `[environment.providers]` aliases and model credential environment references, from `~/.config/arete/pi.env` before the server's environment. Configured API-key templates retain their literals, all variable references and Pi's dollar escapes; missing variables fail before launch. Login calls exclude those credentials even under `pass = ["*"]`. Credential commands and header overrides cannot replace the selected route. An unqualified model needs a configured `defaultProvider`; Arete pins that provider and model before launch. Each call also gets `ARETE_AGENT=1` and its private `PI_CODING_AGENT_DIR`. SDK-managed Bedrock profiles/IAM, Vertex ADC and Cloudflare gateway headers keep their supported credential context without substituting a profile name or readiness marker as an API key. Anthropic header/federation credentials alone cannot satisfy its key requirement. Explicit credential-file paths stay protected from tools. `ANTHROPIC_OAUTH_TOKEN` is not a key source; a Claude plan token in any credential or environment value refuses a pi launch without printing the value.

A codex agent adds `CODEX_HOME`, and never gets `CODEX_API_KEY`, `OPENAI_API_KEY` or the parent session's names above, even under `pass = ["*"]`.
It also gets `ARETE_AGENT=1`.
The commands it runs get only the list above, `ARETE_AGENT` and `ARETE_RUN_ID`, through codex's `shell_environment_policy`, so `CODEX_HOME` stays out of them.

These commands start from the same list, plus the values arete sets for them, such as a loop check's `ARETE_RUN_ID` and `ARETE_SCRATCH`, or the verify start command's `ARETE_VERIFY_SCRATCH`:

- a layer's `[setup] commands`, `[checks]` and `[verify] start`
- a loop's check and measure commands, and a `push` emit's gate check
- an eval's setup commands and its held out check

Arete's own commands keep the server's environment: git, gh and ssh, the sources and the heartbeat's probes, the installer, `claude auth status` and `codex login status`.
A device other than `local` keeps today's environment, since the device is its own machine.

`[environment] pass` names more variables to pass, in `~/.config/arete/config.toml` for every repo, or in a repo's layer for that repo's agents and commands:

```toml
[environment]
pass = ["HTTPS_PROXY", "NODE_EXTRA_CA_CERTS"]

[environment.providers]
amazon-bedrock = ["AWS_*"]
```

A name may end in `*` to match a prefix, and `["*"]` passes everything but the parent session's names above.
A pi agent on `amazon-bedrock/...` gets its selected AWS bearer, profile, IAM, web-identity or container context and required region settings. Other credential sources stay outside that call. Profile/source recognition does not validate live credentials; unsupported no-signal instance metadata discovery is left to the installed SDK contract.
Arete reads a layer's `pass` from the repo's layer folder, never from a worktree, so an agent cannot widen its own environment, and a layer arete cannot read passes nothing more.
`[environment.providers]` goes in the engine config only.
A proxy, a Bedrock or Vertex login, or a CA certificate file that claude needs goes in `pass` too.

A setup or check command that used a variable from the server's environment may now miss it.
On each start, `arete serve` logs the server variables that no longer reach agents and these commands, by name, never by value; add the ones a command needs to `pass`.
Each step's `step_start` run line lists the names its agent gets (see [Run lines](#run-lines)).

This keeps a key out of an agent's reach only as far as the environment goes.
The agent runs as your user, so it can still read a file your user can; the guard's secret file rules are a second layer, and a device is the real wall.

## JEV

JEV is a classifier model from TypeSafe, reached through OpenRouter. It reads text and answers a yes or no question, picks one of a few labels, or rates on a scale, each with a confidence, fast and for $0.042 per million input tokens. Arete asks it first where a classifier can settle the question, and leaves the question to the model only when JEV is unsure:

- Loops: a loop fed by outside text drops each event JEV reads as instructions to an agent before the agent sees it, and a ticket emit drawn from outside text waits for approval on the same check. A `[classify]` step asks JEV first and sends only the events it is unsure of to the model. A finding JEV matches to a known one, even worded another way, counts as that finding, not a new one.
- Risk: the automerge of a pipeline PR, opened, pushed to or checked again by the run, waits for a person when JEV rates the diff risky (how much depends on what it changes, how near it comes to credentials, money or data, and how far it strays from its description), or reads it as a change a revert will not undo.
- Push screen: a `push` emit under `automerge` that no static rule holds lands only when JEV reads its diff as a narrow fix.
  It waits for approval when JEV's chance for one screen question reaches that question's line in `[jev.screen_lines]`: the diff weakens a protection, exposes credentials or personal data, sends data somewhere new or builds a command from outside text, does harm a revert will not undo, adds a capability, removes a behavior instead of fixing it, or renames something other code or config uses, or it reaches past the fix it names.
  The approval names each question that held it, with JEV's chance and the question's line.
  A diff longer than the 12000 characters JEV reads waits too.
  In a search loop, a JEV hold goes back to the agent with JEV's reason, and the agent narrows the change and tries again; only the last held change waits for approval.
  JEV judges a given diff once, so the push at the end of the run gets the verdict the loop saw.
- Proof check: a search loop with `[search] proof = "repro.py"` names the script in its scratch folder that proves the bug.
  When a try makes the check pass, JEV reads the finding, that script and the diff, and asks whether the script checks something other than the problem the finding describes, and whether it runs the code into that problem and checks for the wrong result.
  A chance of `proof_hold` or more on the first, or under `proof_hold` on the second, holds the push.
  In the run, that hold goes back to the agent like a push screen hold, and the agent rewrites the proof; at delivery it waits for approval, and the approval shows the proof.
  A missing proof, or one past the 12000 characters JEV reads, holds it too.
- Already fixed: before the first try of a search run fed by findings, JEV reads the finding against each commit that landed on the base branch since the run that found it started, up to 40, and against each push of the same loop that waits for approval. When JEV gives a chance of `fixed_skip` or more that one of them already fixes it, the run ends as done, already fixed by that change, with no agent call. Otherwise the run goes on and its log names the closest change.
- Verdicts: each push screen, proof, already fixed, research change, research answered and PR feedback verdict is stored with each question's chance, its line, and what came of it: landed, approved, rejected, replaced by a narrower try, waiting, failed, skipped, or went on. `arete jev report [--days N] [--json]` shows the holds per question and what came of them, the close holds and near misses within 0.1 of a line, the pushes JEV cleared that a person rejected, and the landed pushes a later commit reverted, so a line can move on evidence.
- Comparing decision models: `arete jev sample FOLDER` writes a made up comparison, and `arete jev compare SPEC [--out DIR] [--json]` replays endpoints over labeled examples without calling JEV or touching a setting.
  The spec is TOML with `min_answered`, `min_examples`, `[question.NAME]` for kind, duplicate, refresh and risk (`examples`, `ask`, `false_positive_cost`, `false_negative_cost`, `fallback_cost`) and `[endpoint.NAME]` (`transport`, `answers` or `command`, `model`, `response`, `timeout_seconds`, `usd_per_million_in`, `usd_per_million_out`, `[endpoint.NAME.lines]`).
  A labels row holds `id`, `text`, `label`, `split` (`tune` or `held_out`) and `reviewed`; rows without `reviewed` are left out, and a row with any other key, such as a prediction, is refused.
  An answers row holds `question`, `id`, `response`, and optionally `tokens_in`, `tokens_out`, `seconds`, `cost_usd` and `timeout`.
  Each call ends answered, abstained, timeout, malformed or failed, and a response is a probability or a yes, no or abstain verdict with a confidence.
  A call without an answer counts as a fallback for kind, as no for duplicate and as yes for refresh and risk, so no classifier can open what the code keeps shut.
  Lines are chosen on the tune split and scored on the held out split, and calibration is read from the held out answers.
  `proposal.json` holds the label counts, usage (calls, billed questions, tokens, repeated context, latency, cost), the tradeoff by line and at most one proposed line per endpoint and question; `predictions.jsonl` holds one row per call and never a label.
  A line belongs to one endpoint fingerprint and question and never carries over to another model.
  Nothing is applied: the `[jev]` lines change only when a person edits the config.
  Real endpoints go in through a `command` adapter that reads one JSON request on stdin and prints one JSON object with `response` and, when it has them, `tokens_in`, `tokens_out` and `cost_usd`; any other key makes the call malformed.
  The usage an adapter prints is kept whatever the outcome, also on a timeout, a nonzero exit, a missing `response` or an extra key, and a call that reports a token count or a cost counts as a billed question, without an answer unless it was answered.
  A usage value that is negative, over 1e9 or not a finite number is ignored, a response that is such a number is malformed, and a spec that holds such a number, or a `timeout_seconds` over 3600, is refused.
  The spec, labels and credentials stay outside the repo.
- Triage: JEV asks whether a ticket asks for a change to a repo and what kind of ticket it is. A ticket it gives a chance of a code change at or under `triage_skip` settles with no model call, as research when JEV is sure it is research and as work for a person otherwise; one under even odds settles the same way when JEV is sure of research or person. Before arete sends a ticket to the pipeline or the research agent, JEV screens its text for instructions to an agent, and a ticket it flags waits for you.
- Research: when a researched ticket changes, JEV reads what changed in its text against the report's summary and asks whether the change asks something different or adds a fact, decision, limit, deadline or resource the report should use. Under a chance of `research_rerun`, the report stands with no agent call. Before a ticket's first report, JEV reads it against the reports already done for its parent, its siblings and its children, and picks the one that already delivers what it asks, or none. At a chance of `research_answered` or more that one does, the ticket links that report instead of a new run. See [Triage](#triage).
- PR feedback: JEV asks of each new comment you leave on an arete PR whether it asks for a change or a question the author should answer, logs the chance, and stores it with the comment. A comment under `feedback_skip` starts no revision; at the default of 0, none is skipped.
- Miner: the digest leaves out the transcripts JEV is sure hold nothing to learn from, so it has room for the ones that do. The gate also leaves out recorded runs JEV is sure hold nothing to learn from; a run from a `jev = false` repo is never sent.
- Sessions: when a session's context passes `compact_tokens` and JEV is sure the new message starts a different task, the session compacts before it takes the turn.
- Guard: each local claude agent that can read files, write files or run commands runs `arete guard` as a PreToolUse hook. With no JEV call, it keeps the agent out of secret files (`.env`, keys, `~/.ssh`, cloud credentials), whether the agent reads or writes one or names one in a command. With JEV it also stops a command that would do harm git cannot undo (throw away uncommitted work, delete outside the working folder, force push, publish, drop data, send a message) and a command that would show or send a credential. A plain push or a new pull request goes through. The verify prompt tells the verifier to give a key it was not given a value that starts `arete-placeholder`, and that a request to localhost, 127.0.0.1 or [::1] reaches the product it started, so neither reads as a credential or an outside send. When anything goes wrong, it lets the call through. `[jev] guard = false` turns it off. A codex agent runs `arete codex-guard` as its PreToolUse hook instead, with the same judge; the secret file and git write rules there always run, and `guard = false` turns off only its judge (see [Codex](#codex)).
- `arete ask QUESTION [PATHS] [--choice A B] [--score LOW HIGH] [-r] [--only ANSWER] [--json]` asks JEV about piped text, or about each file, sixteen at a time. Agents that can run commands get it, with a short note on when it pays: sort files before reading them, sort a test failure before digging in, check a diff against its task. An agent may ask only about files inside its working folder, and never about secret files.

JEV uses the OpenRouter key the pi runner uses: `OPENROUTER_API_KEY` in `~/.config/arete/pi.env`, or in arete's environment.
The guard runs inside the agent, which no longer gets the server's environment (see [An agent's environment](#an-agents-environment)), so a key set only in arete's environment leaves the guard with no judge: it keeps only its secret file rules.
`arete serve` says so when it starts, and so does the run log of each claude step that can run commands; put the key in `pi.env` to give the guard its judge back.
With no key, with JEV off, or when a call fails, each step does what it did before JEV, with one exception: when the push screen's or the proof check's call fails, the push waits for approval, because a push lands with no PR to review.
When the already fixed check fails, the run goes on.
Each call is a step of its run on the `jev` runner, priced at what OpenRouter charged; a guard check is a step only when it stops something.
JEV sends the text it judges to OpenRouter and TypeSafe: events, tickets, diffs, transcripts, commands and files.
Set `jev = false` on a repo whose code or tickets must not leave the machine.

## Runs, sleep and the network

Poll starts detached runs and fills the available capacity round robin across repos, checking each ticket's dependencies, process budget and model routes before launch. Each poll examines at most 64 distinct tickets, so blocked tickets and failed starts cannot create an unbounded loop. A ticket selected in this poll stays excluded while its child process initializes. `max_parallel` sets how many runs go at once on the machine, manual runs included; an `arete experiment` trial holds a slot too, and starts only on a free one with no run waiting to take a slot back and no ready ticket passed over. A run frees its slot while it waits on its PR's CI, and takes one back before a CI fix step. A run that waits to take its slot back goes ahead of new runs: while it waits, no new run starts, even on a free slot. It reads `max_parallel` again every 30 seconds, so a raised limit lets it go at once. `arete status` lists it as waiting for a run slot, and a run in its CI wait as waiting on CI without a run slot. A repo with `max_parallel_per_repo` runs going, counting runs in their CI wait and manual runs, gets no new one from poll; `arete run` by hand is held only by `max_parallel`.

Pipeline budgets span repairs, continuations and individual step reruns. The defaults allow two repair calls, 60 minutes of total active run time and 20 minutes inside verification, including verification repairs and service waits. A deadline interrupts the current call through its existing cleanup and parks the ticket as failed, leaving its available workspace, branch and evidence in place and releasing its slot. `budget.json`, `parked.json` and the run's budget events record what was consumed and why it stopped. An interrupted verification phase counts through the run's recorded finish. The 60 minutes are active run time: a `limit` wait (a usage limit or credit hold) and a `slot` wait for a free run slot are left out, in the run and in the earlier runs of its chain. The deadline does not fire during such a wait and moves out by its length. `ci` waits and time inside verification, service waits included, still count, and `max_verify_minutes` keeps counting during a `limit` wait. An interrupted run counts through its recorded finish, less its `limit` and `slot` waits. `budget.json` and `parked.json` list the minutes left out by cause under `wait_minutes_left_out` (this run) and `earlier_wait_minutes_left_out` (earlier runs of the chain). Repeating an unchanged parked request starts no model call. Change the ticket's title or body, published branch head, or budget settings and retry with `arete run REPO TICKET --force` to begin a new budget. A forced run of a chain that did not park leaves out the repair calls, run minutes and verification minutes that earlier runs spent under a different title or body, writes one log line saying how much it left out, and lists those runs under `left_out` in `budget.json`. A run whose specification was not recorded still counts, and so does every run when the specification is unchanged or the run is a repair, a continuation or started by poll. Parking does not approve a review, remove a hold or enable a merge. A run that ends with its workspace left in place, parked or failed, commits its uncommitted edits to the local branch with an `Arete-Run: RUN_ID` trailer and pushes nothing; `parked.json` lists the kept head and the commit beneath it under `kept`, and the workspace record keeps them for any later run. The next run of the ticket, forced, resumed or prepared again, continues from that commit when the worktree still matches the record, merged with `origin/BRANCH` or the base when either moved, and tells the build that the worktree holds those edits; the diff limit, the checks and the review count them as part of the change, and the next push sends them. A worktree that was reset, moved to another branch or edited by hand is refused with what changed; remove the worktree and the local branch to start from origin.

A laptop sleeps and drops its network. When poll, triage or the heartbeat start offline, most often just after a wake, they wait up to 3 minutes for the network; if it does not come back they skip with an "offline" note and exit 0. A git or gh call that fails on a network error waits up to 30 minutes for the network, then tries once more. An agent step cut off by a dropped connection resumes its own session when the network is back. A run whose process died is put back in the queue with a comment on the ticket, and the next poll starts it again; a second death in a row fails the run as before. Heartbeat ages count only time awake, so a night asleep never reads as a stale log.

## Records

Every agent call writes its runner's raw output to `runs/<run_id>/record/<step>.jsonl.gz` under the state folder while it runs.
A call outside any run gets a run id of its own that starts with `call-`.
A second call with the same step name in one run gets `<step>-2.jsonl.gz`, then `-3`, and so on.
The first line is a header with the runner, model, `atom` (the step the call resolved, when it has one), device, repo, commit, folder, prompt, system prompt, tools, JSON schema, budget, timeout, the session it resumes, and the route it took: `credential`, `billing`, `requested_effort`, `tier`, `position`, `requested`, `launch` and `skipped`. The result line adds `credential`, `effort`, `requested_effort` and `skipped`, plus `cache_read`, `cache_write`, `quote` and `reference_usd`. `paid_usd` is the charged cost; `reference_usd` is the tokens at the retained `quote` (the snapshot rate applied, its source, version and route), null when no quote applied or a needed component is unknown. Each entry of `calls` carries the same.
Headers, results, timeline `agent` marks and SQLite step rows also carry `auth_mode`: `subscription` or `api_key`. Runner and provider names stay unchanged. An empty or absent mode uses the historical interpretation: pi and replay are metered, Claude is subscription, and Codex is subscription except for its `openai` provider. Old rows are not backfilled from today's login. A crashed or interrupted record keeps its captured mode without inventing usage.
When a call leaves the model to the runner's default, arete holds the header until the runner names the model it runs (claude's `init` event or pi's first reply), for at most 10 seconds, so the header names the model the default turned out to be.
Then come the runner's own lines unchanged: claude's `stream-json` events, pi's `--mode json` events, or codex's `exec --json` events after one `command` line with the argv arete ran.
A clock line with the seconds since the start goes in before a runner line whenever a second or more has passed since the last one.
The last line is the result: ok, error, text, structured answer, cost, tokens, first turn's input tokens, turns, model, session and seconds.
A call that crashes ends with a result whose error starts with `crashed:`, and a call stopped on request (a stop from the CLI or the dashboard, or ctrl-c) ends with the error `stopped by request`.
Neither has a cost or a turn count, in its result or in its timeline mark, because the runner never reported one.

`steps.jsonl.gz` in the same folder is the run's timeline, one line per mark: `create` (a fresh sandbox), `lease`, `clone`, `boot`, `release`, `worktree`, `setup`, `check`, `diff` (with the staged patch), `agent` (one per call, naming its record file and its `atom`) and `result` (the run's final status).
Each mark has its start time, and the marks that time something have their seconds.
Classic `step_end` lines and saved step results include `auth_mode`, joining distinct modes with `, ` when a step retries across modes. Saved results also retain the `auth_modes` list and aggregate `paid_usd`. Seam `step_end` lines join distinct modes like their other call labels; a joined value describes several calls, not one authentication mode. Individual results keep their own mode. A pi schema restatement keeps the original route, provider and configuration; its usage gets a separate step row, and the combined record result lists each call's mode, paid amount and usage in `calls`, alongside aggregate `auth_modes` and `paid_usd`.
The trusted Pi guard reports completed SDK credential refreshes through a private pipe, so restatement and pinned retries can follow token rotation even when the provider exposes no stable account identity. Refresh continuity stays within the invocation and outside records; unrelated login replacements are refused.

Subscription limit holds are qualified by subscription family, nonsecret account identity and limit scope. Matching Codex and pi openai-codex ChatGPT account IDs share `codex`. Claude's all-model, opus and sonnet limits remain distinct; Codex's limit covers its family. Different accounts never share a hold. An unknown identity can wait only within its own invocation. Old unqualified holds expire without becoming pi holds.
Discovery reads login metadata separately from launching an agent. Opaque Claude OAuth credentials can use a read-only profile lookup with a short timeout, a response-size bound and no redirects. Only nonsecret identity snapshots and one-way fingerprints are cached; credentials and profile bodies never enter records. A changed credential, account or login directory invalidates discovery, including on bounded wakeups while waiting. Only a successful subscription call clears older applicable holds for its captured account and scope; API-key success cannot clear them. Reset slack, session continuation and original-prompt retries still apply.

The files are gzip, not zstd, because arete runs on Python 3.13 with the standard library only, and zstd comes to the standard library in 3.14; the `.gz` suffix names the codec, so a switch later is a new suffix.
`zcat`, `gzip -dc` and `zless` read them.
Each line goes to disk as its own complete gzip member the moment it is written, so a timeout, a budget stop or a killed process leaves a file that these tools and `arete record` read in full up to its last line.

Before a line is written, arete replaces every secret it knows with `[redacted]`: the values of environment variables whose names hold TOKEN, SECRET, PASSW, API_KEY, ACCESS_KEY, PRIVATE_KEY or CREDENTIAL, `gh auth token`, and every value in `pi.env`.
It also reads the Jira env file, `~/.netrc`, `~/.pgpass`, `~/.npmrc`, `~/.pypirc`, `~/.aws/credentials`, and the secret files (such as `.env`) in the work folder and the folders the call may also use, down four levels and skipping `.git`, `node_modules` and virtual environments.
From `NAME=value` lines and from `name: value` pairs (YAML, JSON, `.pypirc`) it takes each value under a name like the ones above, and each other value of 8 or more characters that is not only letters.
From `.netrc` it takes the word after each `login`, `password` and `account`, but not the `machine` host, and from `.pgpass` the password field of each line.
Values shorter than 8 characters are left alone, and any PEM private key block is redacted wherever it shows up.
On a remote device arete cannot read the work folder, so only the local secrets and the PEM rule apply there.

`arete record RUN_ID` prints the timeline and lists the record files.
`arete record RUN_ID STEP` prints one call as normalized events, the same shape for both runners: `header`, `message`, `tool_call`, `tool_result`, `usage` and `result`, each with the seconds since the call started.
claude reports a resumed session's running cost, so the header keeps what the session had cost before the call, and the closing `usage` total subtracts it to give the call's own cost, as the result line does.
`--raw` prints the recorded lines as they were written.

`arete record RUN_ID STEP --follow` prints existing normalized events and follows new ones live.
`arete record RUN_ID STEP --raw -f` follows the stored lines instead. Each line is flushed even through a pipe.
Following requires STEP and waits for its file to appear when the run is known to be running.
It stops at that call's Arete result, even if the enclosing run is still active. If the call never records a result,
it drains available complete lines and stops when the run's status, run report or timeline says the run ended.
A missing run row alone does not mean the run ended. A run that ends without creating the requested step reports a missing-step error.
Following starts at the beginning and stays on the selected file: `arete record RUN_ID plan-2.jsonl.gz -f`
follows only the second attempt and never switches to another attempt.

The miner reads two sources: Claude Code session files on this machine, and the records of arete's own runs, since the last mine that ended `pr` or `noop` (or since `--hours` ago).
It reads only runs with a signal, worst first: `failed` (a failed run, call or step), `retry` (a step run more than once, or a repair), `budget` (the run parked on a limit) and `long` (active time of half the run limit or more).
Miner, eval, experiment and draft runs are left out.
A run's records go into the prompt as quoted data, one prefixed line each, to a call with Read, Glob and Grep only and a JSON schema as its only output, so text inside a record cannot reach a tool that acts.
Evidence cites `run <id> <record>:<lines>`, the raw line numbers `arete record RUN STEP --raw` prints.
A proposal for arete or a public repo that names another repository, ticket, run, host or the login name is skipped.

### Retention

The hourly prune in `arete serve` deletes records oldest first until both `[retention]` limits hold: `days` (default 30, counted from the record's last write) and `max_gb` (default 20, decimal GB, `10^9` bytes).
A record stays while it is pinned, while a pending proposal cites its run (as its own run or as a run id in its evidence) until the proposal is merged, rejected, skipped or filed, while its run is running, and for an hour after its last write.
Only the `record/` folder of a run is removed; `run.log`, `run.jsonl` and the other files stay.
`arete record pin RUN_ID` keeps a record; `arete record unpin RUN_ID` lets it age out like any other.
`arete status` shows a `records:` line with the size of the store, its oldest record and the number of pins.
When every record left is kept and the store is still over `max_gb`, the serve log says so on that hour's prune.

## Run lines

Each pipeline run also tells its story as JSON lines in `runs/<run_id>/run.jsonl` under the state folder, one object per line, so one file can be followed while the run works or read after it died.
Every line has `ts` (UTC, like `2026-11-11T12:00:00Z`), `run` (the run id), `seq` (1, 2, 3 and so on within the run) and `event`.
Lines are only appended, never rewritten, and each is flushed as it is written.
A writer holds a file lock while it appends, so several processes can write to one run and the lines stay whole and `seq` stays unique; `seq` goes on from the file, so it carries on after a restart.
A last line cut short by a dying process is ended as a garbage line by the next writer, and readers skip any line that does not parse.

| event | fields |
|---|---|
| `task` | `ticket`, `kind`, `repo`, `title`, and `path` (the name of the script in `adws/` that ran it, such as `feature`) for a run from a [script](#the-adw-scripts); when the run starts |
| `repair` | `source` (the run whose saved review this run repairs); in the new run that `POST /api/runs/RUN_ID/repair` starts. The `report` line gains `review.unresolved` (the last review still requests changes) and `held` (why the PR stays a draft, or empty) |
| `answer` | `asked_in` (the run that asked), `question`, `text` (the answer given through `POST /api/runs/RUN_ID/answer`, or empty); in the new run that the author's reply starts. A run that continues a parked ask writes one `answer` line per ask instead, with `asked_in`, `ask_id`, `step` and `answers`: one entry per question, holding `picked`, `text`, `done` or `declined`, and for a secret question only `name` and `declined` |
| `step_start` | `step`, `atom` (the step the call resolved), `runner`, `model`, `provider`, `auth_mode`, `credential`, `billing`, `effort`, `requested_effort`, `tier` (the route's tier, or empty), `position`, `requested`, `kind` (the task's kind), `launch` (1 for the first provider call of an agent call, higher after a fallback), `skipped` (the routes passed over, each with `runner`, `model`, `credential`, `state`, `detail`, `until`), `env` (the sorted names of the variables the agent starts with, never their values; see [An agent's environment](#an-agents-environment)); before each agent call. An agent call that falls back writes one `step_start` per launch. A [seam](#the-seam) step writes `step` and `try` for its own line (the step of a run of one step alone also writes `tree` and `fingerprint`, the folder as the step found it, when it is called with `tree`), and a seam step that calls an agent (`plan`, `implement`, `revise`, `verify`, `review`, `learn`) adds a line per launch with `step`, `try`, `call` (the agent call's name) and the route fields above; extra launches never change `try` |
| `heartbeat` | `step`, `seconds` so far; every 30 seconds while a step works (`lines.HEARTBEAT_SECONDS`) |
| `step_end` | the start fields plus `ok`, `cost_usd`, `tokens_in`, `tokens_out`, `turns`, `seconds`, `error`, `try`, `cache_read`, `cache_write`, `api_seconds`; the cost counts an attempt the network dropped. `tokens_in` includes the cache reads and writes. `try` is the number of this call of the step name in the run, counted per call and not per provider launch, so a call that fell back across two routes is still one try and the next call of `build` is try 2, and every `steps` row of one call carries that `try`; a seam step's `try` goes on from the runs it continues, while the `try` in the `steps` rows counts the calls of that agent step name within the run's own id, so a continued run's first `build` call has seam try 2 and row try 1; a step the pipeline numbers itself (`fix1`, `fix2`) starts at 1 under each name. `api_seconds` is the time the model's API calls took (0 on pi, which reports none), so `seconds` minus `api_seconds` is the time spent in tools. A seam step's line has `step`, `try`, `result`, `ok` (true for `pass` alone), `next`, `feedback`, `data`, `session`, then its agent calls' `runner`, `model`, `effort`, `tier`, `kind` and `atom` (the distinct values joined with `, `) and their summed `cost_usd`, `tokens_in`, `tokens_out`, `turns`, `cache_read`, `cache_write` and `api_seconds`, then `seconds`, `error`, and `tree` and `fingerprint` for a step called with `tree`. An agent call inside a seam step writes a `step_start` per launch but no `step_end` of its own; its row in the `steps` table stays |
| `route` | `continues` (the run this one continues), `after` (the last step it took from that run), `skipped` (how many steps it took), `redone` (how many steps at the start the run ran again itself before taking stored ones, 1 only after a failed `prepare`; such a run writes the full line once before that `prepare` with `skipped` 0, and the last full line counts), `only`, `again` (the step `--from` names, or empty), `renew` (`prepare` when the base moved and the flow prepares again, else empty). A step carried forward from the run it continues is written again as a `step_end` with `carried` set to that run and no cost. A continuing or `only` flow writes it twice: an opening line with `continues`, `only` and `again` when the flow starts, so a run that dies before its first step still names the run it continues, then the full line when it starts running steps |
| `wait` | `cause` (`limit`, `ci`, `slot` or `person`), `phase` (`start` or `end`), `at`; the end line adds `since` and `minutes`. `limit` is a usage limit or credit hold with no usable route left, `ci` the watch on CI, `slot` the wait for a free run slot: after the CI watch, and before the run started, from the first poll that passed the ready ticket over (for `max_parallel` or `max_parallel_per_repo`). `person` runs from the asking run's `question` line to the start of the run the reply starts, or to the first pass-over if there was one. The `slot` and `person` lines are the run's first lines. A wait whose end line never came is not counted |
| `question` | `question` (the comment), `summary`; when the run asks the ticket's author |
| `error` | `step` (the last step started, or empty), `message`; for a failed or interrupted run, not for a stop by request, and right after the `step_end` of a seam step that raised or returned something it may not |
| `report` | `task` (the ticket's id), `status` as the `runs` table holds it (`step` for a run of one step alone), `prs`, `summary`, `requirements` (each check in use: `what`, `passed`, and `evidence`, the path of its screenshot or of the verify step's `.md` report), `assumptions` (what the plan decided where the ticket left it open), `left_for_a_person`, `review` (`verdict`, derived from the problems: `request_changes` only when a blocker is open; `summary`; `problems`, the open blockers, each with `file`, `text`, `severity` and `category`; and `notes`, the problems that do not block, with the same fields), `question` (the summary of what the run asked), `steps` (for each step name in the run's `step_end` lines, its `tries`, `minutes` and `cost_usd`), `minutes`, `cost_usd`, `wait_minutes` (minutes by cause, for the causes the run waited on only, also kept on the run's row). A classic run and a seam run write the same fields, and a field the run never reached is empty (`""`, `[]`, or a `review` with empty fields), never absent. `requirements` and `review` are the last check in use and review. Always the last line, and the poll writes it for a run whose process died. `flow.respond` writes `status`, the fields it is given and `steps` |

Eval runs write the step events too, but no `task` or `report`.
Each line is also printed to the run's stdout.
`arete lines RUN_ID` prints the lines, `--from SEQ` starts after that `seq`, `--follow` keeps printing new lines until the `report` line, and `--step STEP` prints only that step's lines. It reads them from the server when the server is up, and from the file when it is not.
The dashboard serves the same lines at `GET /api/runs/RUN_ID/events`; see [API](#api).
The run log, the `<step>.json` files, the tables and the records stay as they were; the lines are an addition.

## The seam

`arete/factory.py` is one seam that a run's steps can go through. The steps in `arete/flows.py` run on it, in the order the [scripts in `adws/`](#the-adw-scripts) give, when a repo's layer sets `[factory] path = "adw"`, on any repo; every other run takes the classic pipeline and behaves as before.

- `factory.Outcome(result, next="", feedback="", data={}, session=NO_SESSION)` is what a step returns. `result` is `pass`, `fail`, `ask` or `stop`, and any other value raises. `next` names the step to run next, or is empty for the flow's own order. `data` is a JSON object. `outcome.passed` is true for `pass` alone.
- `@factory.step("name")` registers `fn(flow, *args, **kw) -> Outcome` in `factory.STEPS`. A name registers once.
- `factory.Flow(run_id, continues="", only="", ctx=None, labels=None, again="", restore=None, step_runs=None)` is the handle of one run, and `ctx` is whatever the steps need. `flow.step(name, *args, **kw)` writes `step_start`, calls the step and writes `step_end` (see Run lines). `labels(name)` returns the fields that step's `step_start` adds. `try` counts the calls of the step from 1 and goes on from the runs this one continues.
- A step that raises, returns anything but an `Outcome`, or returns `data` that JSON cannot write ends with `fail` and an `error` line, and the exception is raised again. An interrupt ends it with `stop` and no `error` line.
- `tree=<folder>` stores the folder's fingerprint in `step_end`: `git write-tree` over the tracked and untracked files, ignored ones left out, staged on a copy of the index so the real one is untouched. The step function gets `tree` too.
- `continues=<run id>` reads that run's lines and those of the runs it continued. Each `flow.step` call is matched in order against their `step_end` lines, and a call that meets a passing line of the same step returns the stored outcome without calling anything. A call also takes a failing line of the same step that the earlier run moved on from (no error, later lines follow) and gets its stored result, so a flow with a route back in its history follows the same route. A `prepare` line that did not pass is never taken, and a run whose own `prepare` did not pass adds nothing to the history. When the newest `prepare` in the chain failed, the flow runs `prepare` itself and goes on matching the steps after it against the newest run that passed `prepare`, under the same fingerprint rule. A line with a fingerprint matches only while the folder's fingerprint equals the last one recorded for it, so a run whose folder is gone or changed goes on after the last step that did not touch it. A run of one step alone that continued a run of the chain, and ended after the chain's last line, moves the fingerprint rule to the folder it left for the lines it took, and for the line of its own step when that step passed. It moves only lines that matched the folder its step found, in the order of the chain, and stops at the first that did not, so a later step it took without running, a step whose run alone failed and every step after it still run again on the changed folder. The first call that does not match writes the `route` line and every later call runs.
- `only=<step>` takes the steps before it from the stored outcomes, without comparing fingerprints, runs that step even if it passed, and raises `factory.OnlyDone` at every later call, which ends the run with the status `step`. It raises `factory.Refused` naming the step when an earlier step has no stored outcome it can take, and the refusal says so when the runs it continues never started the `only` step. When the folder of a stored step is gone, it calls `restore(line)` with that step's `step_end` line: an empty answer means the folder is back, and any other answer is the reason it is not, which the refusal gives. With no `restore`, a gone folder is refused. The flows in `arete/flows.py` make a removed folder again from the run's branch on origin and run the setup commands in it.
- `again=<step>` with `continues` takes no stored outcome for the last call of that step, so that call and every step after it run again; earlier calls of the step, such as the first `implement` before a fix round, are still taken.
- `flow.respond(status, **fields)` writes the `report` line.

Every agent step of an adw run has the `ask_owner` tool (claude and codex through the stdio MCP server `arete mcp-ask`, merged into the step's `mcp_config`; pi through the extension `arete/pi_ask.mjs`). A call holds 1 to 4 questions of kind `choice`, `text`, `secret` or `done`, plus `why` and `tried`; `arete/asks.py` validates it and the tool replies at once, never blocking. After the call, `pipeline.Run.agent` reads the ask from the call's record and ends the step with the `ask` result, its session on the `step_end` line, and a `question` line with `ask_id`, `step`, `questions` and `summary`. The run ends `needs-info`, parked: it keeps its work folder and the step's session, one row per ask goes into the `asks` table (`GET /api/asks` lists the open ones, oldest first), it frees its run slot and device lease, and one desktop notification fires. The tracker comment lists the questions and says to answer on the dashboard or with `arete answer RUN_ID`. An answer (`POST /api/asks/ID/answer`, `arete answer`, or a reply on the ticket when no question is a `secret`) continues the run with `--continue`: the steps before the ask come from stored outcomes, the asking step runs again and resumes its session with the answers as its next message, or, when the session cannot resume (the runner changed, or the session is gone), runs fresh with the ask and the answers in its prompt. When `origin/BASE` moved since the continued run prepared and the worktree still holds that run's kept edits, the answered run first merges the new base into them (a `base_merge` mark in the timeline, conflicts resolved as for a branch that continues), tells the step so, and then the asking step resumes; the steps before it keep their stored outcomes and every step after it runs on the merged code; a conflict arete cannot resolve fails the run with the conflict named and keeps the answers, so `arete resume` continues with them; with no kept edits or an unmoved base nothing changes. The continued run takes a run slot ahead of new runs, and the wait counts as a person wait. It records the run it continues before it waits for that slot, so a stop or crash during the wait keeps the answers: `arete resume` of the stopped run, or the poller's restart of the interrupted one, continues with them. A secret question accepts only a decline until arete stores secret values; the agent hears only that the owner declined. `{"stop": true}` ends the run `stopped` and removes its folder. After `[ask] expire_days` with no answer the run ends `needs-info` the old way and its folder is removed. An ask that reached `max_per_run`, a dry run and the classic path's plan question end the old way. When `[ask] settle` is on and every question is a `choice` with a recommended option, JEV is asked whether the run can take the recommendations without its owner; at 0.9 or more the step's session resumes in the same run, an `ask_settled` line is written, and the choices appear as assumptions in the report and the PR. Every valid `ask_owner` call of a turn is its own ask with its own `ask_id` and counts against `[ask] max_per_run`; a turn's asks settle together or go to the owner together, as one `question` line per ask (same `summary` and `question`) and one comment that lists every question, and `step_end` carries `data.ask_ids`. The plan step's ambiguity is an ask with one `text` question on this path. An ask from a call that keeps no session (`learn`, `document`) always goes to the owner; a codex call that holds trusted verification scratch runs without the tool and is told so.

### The adw flow

`[factory] path` in a layer is `classic` (the default) or `adw`; any other value fails the layer's load. The switch works on every repo. A repo with a `github` name gets its PRs, CI watch, ticket comment and merge as a classic run does. A repo with no `github` name needs a git remote `origin`, or `arete run` refuses it as before, and gets a pushed branch.

A run with open review feedback takes the revise flow (`adws/revise.py`); any other run takes the feature flow (`adws/feature.py`). The flows run these seam steps, each with the pipeline's own helpers and the agent call names of the classic run (`plan`, `build`, `revise`, `fix2`, `review_fix`, `ci_fix`, `verify`, `verify2`, `review`, `review2`, `learn`):

| step | does |
|---|---|
| `prepare` | Reads the ticket, holds a closed or blocked one, saves its images, picks the branch, makes the worktree, loads the layer, copies files and runs the setup commands. A revise run whose PR closed ends quietly. |
| `plan` | Feature flow. Plans; asks the author when the plan is ambiguous or over `max_diff_lines`. |
| `revise` | Revise flow, in place of `plan` and `implement`. Changes the code for the review comments and drafts a reply to each. With nothing staged it answers the comments with no code change, posts `No code changes for your review.` and pushes nothing. |
| `repair` | Repair flow, in place of `plan` and `implement`. Reads the saved internal review of the run named by `--repair`, checks that the PR head holds a commit of that run, and changes the code for the findings as `review_repair`. It fails with nothing staged, so the findings stay unresolved. |
| `implement` | The build (`build`), then a fix round after each failed check (`fix2` and up, `check_repair`), the review fixes (`review_fix`, `review_fix2`, `review_repair`), and inside `ci` the CI fix (`ci_fix` and up, `ci_repair`) with the red output. |
| `checks` | Runs the layer's checks. A fail goes back to `implement` until the `check_repair` step's `tries` (build's, unless set) run out; then learn runs without applying and the run fails. |
| `verify` | Checks the change in use, with its own rounds as in a classic run. |
| `review` | Reviews. Only a blocker (`issue`, `regression`, `security` or `data_loss`, with the path a real caller takes) requests changes; majors and minors go on the PR under "Review notes (not blocking)". Later reviews get the earlier blockers, the fixer's reply and what changed since, check those and regressions first, and mark each earlier blocker fixed, open or waived (never waived for security or data loss; a missing status counts as open). `request_changes` goes back to `implement`, then `checks`, `verify` and `review` again, until the review step's `tries` reviews are used (`review`, `review2`, ...). The last review that still requests changes ends `fail`, and the run still publishes a held draft whose report keeps `review.unresolved` and `held`. |
| `learn` | Rewrites `expertise.md` as a classic run does. |
| `document` | Only with `[document] enabled`: writes the PR `## Summary` text with a fresh writer (see [Document](#document)). Its outcome holds `text`, empty when the writer's text was refused. |
| `pr` | Commits the staged change with `git.subject` and the ticket ref and pushes it. With a `github` name it opens each PR, or pushes to the open one, with the classic body, sibling section, labels and draft rule; its outcome holds the PR links. A ready PR the run pushed to, checked again with nothing new to push, or found open on its branch when the prepare outcome named no PR, turns into a draft when the run is held. A found PR gets the new report as its body and keeps a CI section that says an earlier CI watch stopped. Without a `github` name it notes the branch on the run. |
| `ci` | With a `github` name, watches CI in rounds. A red check this change caused runs `implement` with the red output, then the push and the watch again. |
| `publish` | With a `github` name, moves the ticket to `pr`, posts the PR comment (or answers the review comments in a revise run), marks a draft PR it pushed to, checked again or found open ready when nothing holds it, and applies the triage hold, the risk hold and the merge. Without one, moves the task to `done`. |
| `respond` | Ends the run as the classic run would: the failure comment, the question (and its `question` line), the nothing to build comment, the review answers, or the `pr` or `done` ending. |

A dry run ends after `learn` (after `document` when it is on) with the status `dry-run`: it saves `diff.patch` and `pr_body.md`, the PR body with private strings screened, keeps the worktree and runs no `pr` step. It makes its worktree locally, so it skips the check for closed PRs on the branch; a revise dry run still reads the PR and its body.

Each step's `data` holds what a later step needs, so a run that restarts after an interrupted adw run continues it: it takes every passing step whose worktree is unchanged from the stored outcomes and runs the rest. A run interrupted in the CI watch never opens a second PR: it goes on at `ci`, and the CI round count starts again. The run's `report` line comes from `flow.respond`. The statuses `pr` and `done` count as success; the summary of `done` names the pushed branch.

### The adw scripts

The order of an adw run's steps lives in short scripts in `adws/` at the top of this checkout. Each one holds only the routing, calls the steps from `arete/flows.py` through `flows.step`, and ends with `factory.main(<function>)`, which gives it the same lock, run slot, `runs` row, stop signal and settling as `arete run`.

| script | holds |
|---|---|
| `adws/feature.py` | The feature flow: `prepare`, `plan`, `implement`, the `checks` and fix loop, `verify`, `review` and the review fix, `learn`, `document` when on, then `pr`, `ci` and `publish`. |
| `adws/revise.py` | The revise flow: the same order with `revise` in place of `plan` and `implement`. |
| `adws/repair.py` | The repair flow: the same order with `repair` in place of `plan` and `implement`, on the branch of an open held PR. |
| `adws/step.py` | Runs one step of an earlier run alone. |

```
python adws/feature.py <repo> <ticket> [--force] [--dry-run] [--continue RUN] [--from STEP] [--only STEP] [--set step.key=value ...]
python adws/revise.py  <repo> <ticket> [same options]
python adws/repair.py  <repo> <ticket> --repair RUN [--force] [--set step.key=value ...]
python adws/step.py    <run id> <step> [key=value ...]
```

Run them with the Python that has arete installed (`uv run python adws/feature.py ...` from the checkout).

- `--continue RUN` takes the passing steps of that earlier run of the same ticket (see `continues` in [The seam](#the-seam)). A run id of another repo or ticket is refused before any run starts.
- `--repair RUN` starts a new run on the open PR branch of that earlier run of the ticket, from the findings of its saved internal review (see [API](#api)). It does not go with `--continue`, `--from`, `--only` or `--dry-run`, and is refused when a later run of the ticket exists.
- `--from STEP` with `--continue` takes the stored outcomes up to the last call of that step, then runs it and every step after it again. It needs `--continue` and does not go with `--only`. When `origin/BASE` moved since the continued run prepared, `prepare` runs again (the route line says `renew`); if the worktree still holds that run's edits, arete merges the new base into them and carries the stored plan and build forward, so only the steps from the first check on run again.
- `--only STEP` runs that step alone and takes the steps before it from the stored outcomes; with no `--continue` there are none, so it refuses, naming the first step it could not take. Edits a step run leaves are kept in a local commit carrying its own `Arete-Run` trailer; a later `--continue` or `arete resume` of the run it continued accepts that commit as the chain's, takes the stored steps that still hold for those edits, runs the later ones again on them (see `continues` in [The seam](#the-seam)) and then publishes it, while `--only prepare` stays refused while such commits wait, as before. The run ends with the status `step`, exits 0 only when the step passed, and leaves the ticket and the tracker as they were. Its `runs` row holds the step in `only_step`, so the ticket's history looks past it: the poller still restarts an interrupted run, a second interruption still fails the ticket, and a run after a question still answers it. A step run that dies or is stopped is settled `interrupted` or `stopped` with no comment, no ticket move and no restart.
- `--set step.key=value` overrides one setting of one step for this run, with the keys and values of `[steps]` (see [Steps](#steps)); repeat it for more. The step names are the seam's (`plan`, `implement`, `revise`, `verify`, `review`, `learn`, `document`); `implement`, `revise` and `repair` set the build step, and `check_repair`, `review_repair` and `ci_repair` set the repair steps. A value `[steps]` would not take is refused before any run starts.
- `step.py <run id> <step> [key=value ...]` reads the script the earlier run used from the `path` on its `task` line and runs it with `--continue <run id> --only <step>`, each `key=value` as `--set <step>.key=value`. It refuses a run that did not come from a script, a step the run never started, and settings for a step that calls no agent. The step runs in the earlier run's work folder. A run that ended `done` or with a PR has removed it, so the step makes it again from the run's branch on origin and runs the setup commands; when the branch is not on origin, it refuses and names the folder.

`arete run` on a repo whose layer sets `[factory] path = "adw"` replaces itself with `adws/revise.py` when the ticket has review feedback due and `adws/feature.py` otherwise, run by the same Python. The poller, and a skipped ticket run from the dashboard, start runs the same way; `pipeline.run_command(repo, ticket)` returns the command they use. Every other repo starts `arete run` as before.

To change a flow, copy `adws/` to a folder of your own, edit the scripts there and set `[factory] adws = "<folder>"` in `~/.config/arete/config.toml` (a relative folder counts from the config's folder). A script defines a function named after its file (`feature` in `feature.py`), which takes the run's `factory.Flow`. When the folder or the script is missing, the run fails before it starts with `adws folder <folder> does not exist` or `no adw script <file>`.

## Layout

- Code: this repo, a uv project (Python pinned in `.python-version`, everything locked in `uv.lock`). Installed with `uv tool install --editable . --python 3.13`
- Config: `~/.config/arete/config.toml` (`arete init` writes a starting one)
- adw scripts: `adws/` here, or the folder `[factory] adws` names (see The adw scripts); `adws/README.md` is the guide for an agent that steers runs
- `justfile`: the `adw` recipes that steer runs through the server (see From the CLI)
- Loops: `arete/loops/` here, `~/.config/arete/loops/` for private loops, `<repo>/.agents/loops/` for a repo's own; switches in `~/.config/arete/loop-switches.json`
- Devices: `~/.config/arete/devices.toml`
- Provider keys for pi: `~/.config/arete/pi.env`, mode 600
- State: `~/.local/share/arete/` (sqlite, events, run logs, `experiments/`, `benchmarks/`, loop digests, `serve.json`, `server.json`, `price-snapshot.json`)
- Records: `~/.local/share/arete/runs/<run_id>/record/`, one gzip JSON lines file per agent call and the run's timeline `steps.jsonl.gz` (see Records)
- Run lines: `~/.local/share/arete/runs/<run_id>/run.jsonl`, one JSON line per run event (see Run lines)
- Worktrees: `~/Library/Developer/arete/worktrees/`, outside every dot folder. A `**/node_modules/**` ignore glob does not match across a dot segment, so a dev server in a worktree under `~/.local` watches all of node_modules and fails with EMFILE.
- Logs: `~/Library/Logs/arete/`
- Per repo: `config.toml`, `expertise.md` and `evals/` in the layer, `.agents/` in the repo or the folder that `layer` names

## Develop

`uv run arete status` runs from the checkout without the installed tool. `uv tool install --editable . --python 3.13 --force` refreshes the `arete` on PATH that launchd uses.

No runtime dependencies: stdlib only, so the tool install needs nothing from PyPI. Add one with `uv add`, never pip. The verifier's browser is `@playwright/mcp`, pinned in `arete/verify.py` and started with `npx`, so it needs node and Chrome on the machine. Under trusted scratch on the Codex runner the engine instead resolves that pinned version once, from the npm registry, into a private folder under its state (`browser-server/<version>`), checks it against the published integrity at every launch, and starts it with the engine's own `node` and no `npx`.

## Global config

```toml
[github_app]
app_id = "123456"
installation_id = "7890123"
private_key = "~/.config/arete/github-app.pem"
```

| Key | Meaning |
|-----|---------|
| `[github_app]` | `app_id`, `installation_id` and `private_key` (the app's PEM file, signed with `openssl`). Remote sessions with a repo clone with a token this app mints for that one repository. The app needs only Contents: read, and it must be installed on the repos those sessions use. |
| `alert_repo` | GitHub repo where each failing heartbeat check opens one issue. Bodies carry check names and a short status only. Until the repo exists, alerts fall back to macOS notifications. |
| `global_repo` | Name of the `[[repo]]` that holds global skills and settings. |
| `arete_repo` | Name of the `[[repo]]` that is this checkout. Its step prompts are harness files the miner may change. Default `arete`. |
| `max_parallel` | Most pipeline runs at once on the machine. Default 1. |
| `max_parallel_per_repo` | Most pipeline runs poll keeps going in one repo, runs in their CI wait included. Default 1. |
| `[models]` | Model per role: `plan`, `build` (also fix and revise), `verify`, `review`, `learn`, `uat`, `miner`, `apply`, `eval_mine`, `triage`, `research` (the reports for research tickets), `research_plan` (the planner of a staged research run), `research_search` (the search workers of a staged run), `research_synthesis` (the collator of a staged run; opus when left out), `research_audit` (the auditor of a staged run; opus when left out), `chat` (the dashboard console), `classify` (a loop's classify step, for the events JEV is unsure of), `loop` (a loop's agent, and the loop drafts from the dashboard), `session` (agent sessions), `explain` (the plain summary of each approval in Needs you), `document` (the concise writer of PR text, tickets and messages). A role left out uses the claude CLI default, except `triage`, `classify` and `explain`, which use haiku, `loop`, `session` and `research_search`, which use sonnet, and `research`, `research_plan` and `chat`, which use opus. `arete status` lists each role's model. |
| `[steps]`, `[kinds]`, `[tiers]`, `[factory]` | Each pipeline step's model, runner, effort, timeout, tries, tools, MCP servers, instructions and session. See [Steps](#steps). `[factory] adws` is the folder the adw scripts come from, default `adws/` in this checkout; see [The adw scripts](#the-adw-scripts). |
| `[mcp.<name>]` | An MCP server a step can name in its `mcp` list: `command` with optional `args`, or `url` for an HTTP server. Servers are defined here only, not in a layer. |
| `[miner]` | `lookback_hours` and `max_digest_chars` for the nightly digest of transcripts and recorded runs; recorded runs take up to half of `max_digest_chars`. |
| `[[repo]]` | Every repo arete knows: `name`, `path`, `github`, and the fields below. |
| `[[job]]` | launchd jobs the heartbeat watches, by `label`. A `keepalive` job must have a pid; others must have last exited 0. `log` with `max_age_hours` flags a stale log. |
| `[[probe]]` | Real calls the heartbeat makes every `every_hours`: a `command` whose output must contain `expect`. A real call, because an auth check can say ready while real calls fail on an expired token. |
| `[research]` | `planner` (default false) turns on the planning stage of a research run. `search` (default false, needs `planner = true`) runs the plan's tasks as bounded search workers; `search_workers` (default 3, from 1 to 8) is how many run at once and `search_share` (default 0.6, above 0 and under 1) the share of the run's remaining money and time they may use. `synthesis` (default false, needs `search = true`) has a collator with no web tools write the report from the workers' evidence. `audit` (default false, needs `synthesis = true`) has a fresh auditor check the collator's claims against their sources before the report is written. See [Triage](#triage). |
| `[ask]` | `max_per_run` (default 3) is how many asks an adw run may send; the next one ends the run `needs-info` and names the limit. `settle` (default true) lets JEV take the recommended options of an ask made only of `choice` questions, at 0.9 or more, without the owner. `expire_days` (default 7, a number above 0 and at most 3650) is how long a parked ask waits for an answer. See [The seam](#the-seam). |
| `[oncall]` | `enabled` (default false) turns on the log watcher; `vision`, `scope`, `confidence` and `injection` steer JEV's decision on each incident; `[[oncall.source]]` adds other logs, each with its own `vision` and `scope`. See [On-call](#on-call). |
| `[jira]` | See [Jira tickets](#jira-tickets). |
| `[dashboard]` | `port` (default 8796) and `owner`, the tailnet login the page opens for. Default: the login that owns this machine. See [Dashboard](#dashboard). |
| `[serve]` | `loop_dirs` (the private loop folders, default `~/.config/arete/loops`), `queues` (a table of queue name to how many runs at once, default 1), `daily_budget_usd` (most pi spend across all loops in a rolling 24 hours), `session_idle_hours` (default 2). |
| `[jev]` | `enabled` (default true), `model` (`jev-latest`), `confidence` (0.8, how sure JEV must be to settle a question alone; on a yes or no question, a chance of yes at 0.9 or more, or at 0.1 or less), `injection` (0.5, the chance of an injection at which an event drops or a ticket waits), `risk_hold` and `irreversible_hold` (0.5 each, the first for pipeline automerge only, the second also the guard's line for a command git cannot undo), `secrets_hold` (0.55, the guard's line for a command that shows or sends a credential), `triage_skip` (0.2, the chance of a code change at or under which triage settles a ticket as not code without the agent), `[jev.screen_lines]` (the chance at which each push screen question holds a push: `weakens` 0.5, `secrets` 0.7, `outside` 0.7, `irreversible` 0.5, `feature` 0.6, `removes` 0.5, `renames` 0.5, and `reach` 0.8, how far past the fix it names a push may reach before it waits), `proof_hold` (0.5, the proof check's line), `fixed_skip` (0.7, the chance at which a search run fed by findings ends as already fixed), `research_rerun` (0.4, the chance that a change to a researched ticket matters, under which its report stands), `research_answered` (0.8, the chance at which a ticket links a report done for its family instead of a new run) and `feedback_skip` (0.0, the chance that a PR comment asks for a change or an answer, under which it starts no revision; 0 logs only), `[jev.risk_weights]` (`blast` 0.4, `sensitivity` 0.4, `surprise` 0.2), `guard` (true) and `compact_tokens` (120000). See [JEV](#jev). |
| `[pricing]` | `local_providers` (default `["ollama"]`, priced at zero) and `[pricing.models."<model>"]` with `input` and `output` in USD per million tokens, for a model whose runner reports no cost; it outranks an imported [price snapshot](#price-snapshots) in route sorting. A component you leave out stays unknown in `arete steps`, not zero, and a call still charges it as zero. |
| `[routing]` | `fallback` (default true) and `payment_modes` (a list of `subscription`, `api_key` and `local`; default all three). The engine's and a repo layer's `fallback` are combined with AND, so either can turn it off; a layer's `payment_modes` can only restrict the engine's. `arete steps` shows the result, and runs act on it. `capabilities` (per-route tables) and `floors` (named model lists) feed the step requirements. See [Steps](#steps). |
| `[chat]` | `context_tokens` (default 40000, at least 100) is how many tokens of the chat's saved messages go into each turn before older ones are folded into a saved summary (the new message is not counted). `summary_model` (default `haiku`) writes that summary. |
| `[retention]` | `days` (default 30) and `max_gb` (default 20): records older than `days`, or the oldest ones beyond `max_gb` in total, are deleted by the hourly prune; pinned, cited, running and just-written records stay. Fractions are allowed. See [Records](#records). |
| `[replay]` | `all` (default false, sends every agent call to the replay runner) and `dir` (the folder of answers; `ARETE_REPLAY_DIR` wins over it). See [Replay](#replay). |
| `[environment]` | `pass`, the names of more server variables that local agents and the commands that run a repo's code get, each an exact name, a prefix ending in `*`, or `"*"` for all. `[environment.providers]` maps a pi provider to more names its agents get, such as `amazon-bedrock = ["AWS_*"]`. See [An agent's environment](#an-agents-environment). |

| Repo field | Default | Meaning |
|------------|---------|---------|
| `pipeline` | `false` | Poll this repo's tickets and run the build pipeline. |
| `mine` | `true` | The miner may open harness PRs here. |
| `public` | `false` | PR bodies cite run ids, never transcript text. Arete's own repo (`arete_repo`) is treated the same way. |
| `tracker` | `"github"` | `github` for issues in the repo, `jira` for tickets in Jira, `local` for [local tasks](#local-tasks) only; such a repo may have no `github`. PRs are on GitHub either way. |
| `jira_project` | `[jira] project` | Jira project for this repo. |
| `jira_label` | `repo-<github repo name>` | Route label that sends a Jira ticket to this repo. |
| `jev` | `true` | JEV may read this repo's events, tickets, diffs, transcripts and files. `false` keeps them off OpenRouter and TypeSafe. See [JEV](#jev). |
| `layer` | `<path>/.agents` | Folder that holds the repo's layer. Point it outside the repo to keep arete files out of a shared repo. |
| `[[repo.part]]` | none | One per git repo under `path`, for a product split over several repos: `dir` (relative to `path`) and `github`. See [Parts](#parts). |

## GitHub tickets

An open issue labeled `agent` that the repo owner opened. Issues labeled `bug` or `fix` are fixes; the rest are features. Arete tracks progress with `agent:running`, `agent:pr`, `agent:failed` and `agent:needs-info`, and the PR closes the issue, unless it leaves a change for a person. Remove `agent:failed` or `agent:needs-info` to send it back to the queue. When the plan finds nothing left to build and the builder changes nothing, arete pushes nothing: with an open PR it keeps the PR in `agent:pr` and says so on the ticket, and with none it asks in `agent:needs-info` whether the ticket is done. A ticket in `agent:needs-info` also goes back when you reply to arete's question in a comment: poll removes the label, and the next run reads the question and your answer. A parked ask with a secret question stays waiting: answer it on the dashboard or with `arete answer`. `arete install` creates all of these labels in each GitHub pipeline repo, so `agent` is there to pick from any GitHub client, the phone app included.

A body line that starts with `Waits on`, such as `Waits on #7.` or `Waits on #7 and #9`, holds the issue until every issue or PR of the same repo it names is closed or merged, and a number arete cannot read holds it too.
Poll skips an issue that waits on such a number, with the reason in the serve log and the dashboard's queue; `arete run` skips it too, and `--force` or Run on the queue runs it anyway.
A `#N` anywhere but a `Waits on` line holds nothing, and Jira tickets order work with their own links, not `Waits on` lines.

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

Progress shows as `agent-running`, `agent-pr`, `agent-failed` and `agent-needs-info`. A reply from you after arete's question takes `agent-needs-info` off, as on GitHub. An ask that holds a secret question is not answered by a reply; answer it on the dashboard or with `arete answer`. Bug, Fire, Incident and Problem issue types are fixes, as are tickets labeled `bug` or `fix`. PR titles end with the key, like `Fix login (ABC-12)`, PR bodies link the ticket, and these PRs get no arete labels. Tickets the approval rules hold back, and tickets with a missing or doubled route label, show in `arete status` with the reason. `arete run REPO KEY --force` runs one anyway.

`[jira] auto = true` lets [triage](#triage) add `agent` and the route label to the tickets it finds ready, at most `auto_per_refresh` (default 3) each refresh. Default `false`.

`[jira] sprint_only` (default `true`) limits that auto approval, and the research loop, to tickets in the open sprint or left over in a closed sprint and not moved to a future one. Triage still sorts every open ticket. Set it to `false` for a board without sprints.

`[jira] skip_types` lists issue types arete leaves alone, such as `["Trail"]`: triage never reads them, so they never reach the board, auto approval or the research loop. A ticket of a skipped type that you label `agent` yourself still runs.

Image attachments go to the agents as files they can open, up to 6 images and 5 MB each. The rest are named in the prompt, not downloaded. A failed download fails the run without labels, so the next poll tries again.

## Local tasks

A local task has no tracker issue: it lives in arete's state database. The `tasks` table holds its id, repo, title, body, kind (`feat` or `fix`), state, parent and times, and `task_comments` holds its comments, each by `owner` or `arete`. An id is `t-` and four hex digits, such as `t-1a2b`, longer when four clash. A task is `todo`, `running`, `pr`, `failed`, `needs-info`, `done` or `closed`. The pipeline's state labels are these states: where an issue gets `agent:needs-info`, a task moves to `needs-info`, and where the label comes off, the task goes back to `todo`.

```sh
arete task add --repo scratch --title "Paint the door" "Paint the front door blue."
arete task list --repo scratch --state todo
arete task show t-1a2b
arete task reply t-1a2b "A dark navy blue."
arete task close t-1a2b
```

`add` prints the new id. `--kind fix` makes a fix, and the title defaults to the body's first line. `list` prints one JSON line per task, oldest first, and both its filters are optional. `show` prints one JSON line: the task and its comments, with its `runs` and `children` as `GET /api/tasks/ID` gives them, from the server when it is up. `task start --repo NAME "PROMPT"` adds a task and starts it, and `task run REPO TICKET` starts one that exists; both go through the server (see [From the CLI](#from-the-cli)). `reply` stores your answer to arete's question, and `close` closes the task. Every task counts as yours. A task in `needs-info` whose newest comment is yours counts as replied, as an issue you answered does.

A body line such as `Waits on t-1a2b.` holds the task until `t-1a2b` is `done` or `closed`. A named task in any other state holds it, `failed` and `needs-info` included, so a task that failed holds the tasks that wait on it. A name that matches no task holds it too, and the reason says so.

A `t-` id gets the local tracker on any repo, wherever arete makes a tracker for a ticket, as in `arete run web t-1a2b`. An issue number or a Jira key gets the repo's own tracker. `tracker = "local"` makes local tasks a repo's only tracker. Such a repo may have no `github`: it loads, shows in `arete status` and on the dashboard, and gets no triage, but `arete run` refuses it with `has no github remote configured` unless its layer sets `[factory] path = "adw"` and the repo has a git remote `origin`. Then the run pushes a branch to `origin` and moves the task to `done`; see [The adw flow](#the-adw-flow). A PR for a task closes nothing and ends with the id, like `Paint the door (t-1a2b)`.

Poll picks local tasks in every `pipeline = true` repo that `arete run` would take, after the tracker's tickets, oldest first, replied tasks included, and a task whose wait still holds is skipped. A config whose repos all use `tracker = "local"` and have no `github` makes no network call when it polls. `POST /api/tasks` on the dashboard adds a task and starts it, or starts one that exists; see [API](#api).

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
max_repair_cycles = 2
max_run_minutes = 60
max_verify_minutes = 20
parallel_checks = false

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
publish_evidence = true
notes = "Sign up only with addresses at example.test."

[research]
dir = "docs/research"

[factory]
path = "classic"
```

| Key | Meaning |
|-----|---------|
| `setup.commands` | Run in each fresh worktree before the agent starts. |
| `setup.copy` | Files copied from the main checkout into each worktree. |
| `checks` | Every check must exit 0 before review. The agent may run them while it builds. |
| `limits` | Diff size, paths the change may not touch, fix attempts (which also cap the CI fix rounds), the timeout of each step and of the checks, and cumulative `max_repair_cycles`, `max_run_minutes` and `max_verify_minutes` process budgets. These budgets must be positive integers. `max_run_minutes` leaves out `limit` and `slot` waits. Checks remain serialized by default; `parallel_checks = true` explicitly allows a repo whose checks use isolated resources to overlap them across worktrees. `max_attempts` and `timeout_minutes` are the defaults for each step's `tries` and `timeout_minutes`, which [Steps](#steps) can set apart. Code enforces the diff size and the paths after the agent finishes. Every repo also denies `.agents/config.toml`, `.github/workflows/` and `.env` files. It denies `.claude/` too, since Claude Code refuses agent writes there. The planner names a change in a denied path for a human to make. The PR shows it under "Left for a person", and a rerun on an open PR writes, rewrites or removes that section to match its plan. The verifier and the reviewer are told that a person makes it, so its absence never fails the run. The CI fix step counts a red check that only that change fixes as not caused by the PR. |
| `agent.allow_bash` | Bash patterns the agent may run, on top of read only git and file commands. |
| `trust.level` | `pr`, or `automerge` once earned. |
| `trust.automerge_after` | Merged PRs in a row before arete opens a PR proposing automerge. |
| `git.base` | Branch PRs target. Defaults to the GitHub default branch. |
| `git.subject` | Commit subject and PR title. Fields: `{kind}` (`feat` or `fix`) and `{title}`. The ticket ref is appended to the commit subject, like `(#35)` or `(ABC-12)`, and to a Jira PR title; a GitHub PR links its issue with `Closes #35` instead, or with `Part of #35` when the plan leaves a change for a person. |
| `git.branch` | Branch name. Fields: `{kind}`, `{id}` (`35` or `ABC-12`) and `{slug}` (the title in short url safe form, cut at a word). |
| `verify.start` | Shell command, run in the worktree, that serves the change. `{port}` is a free port arete picks; `{port2}` and up give more. Leave it empty for a CLI or a library: the verifier then drives the change through Bash alone. `{scratch}` is the attempt's [scratch folder](#verify-in-use), and the start command also gets it as `ARETE_VERIFY_SCRATCH`. Leave `[verify]` out and nothing checks the change in use, and the PR says so. |
| `verify.url` | Where the served app answers. Default `http://localhost:{port}`. |
| `verify.ready`, `verify.ready_seconds` | Path arete waits on before the verifier starts, and how long. Any answer below 500 counts as ready. Defaults `/` and 180. |
| `verify.allow_bash` | Bash patterns the verifier may run, on top of `curl` and `echo`. |
| `verify.publish_evidence` | Boolean, default `true`. Set `false` to keep screenshots in the local run folder without uploading them or linking images in PRs. Verification and ordinary PR delivery still run. |
| `verify.notes` | What the verifier must know: how to sign in, what not to touch, what is shared. `{scratch}` is the attempt's scratch folder. |
| `research.dir` | Folder, from the repo root, where arete files the reports of this repo's research tickets as pull requests. Leave it out and the reports stay in arete. See [Triage](#triage). |
| `factory.path` | `classic` (default) or `adw`, which runs a ticket through the [adw flow](#the-adw-flow) on any repo. |
| `environment.pass` | More server variables this repo's local agents and commands get, on top of the engine's `[environment] pass`. Read from the layer folder, never from a worktree. A layer takes no `[environment.providers]`. See [An agent's environment](#an-agents-environment). |
| `document` | `[document]`: `enabled`, `voice`, `style` and `words` for the concise PR text writer, on top of the engine's `[document]`. Default off. See [Document](#document). |
| `routing.fallback` | `true` (default) or `false`. Effective only when the engine's `[routing] fallback` is also true. |
| `routing.payment_modes` | A list of `subscription`, `api_key` and `local`. It can only restrict the engine's list. |

A template with an unknown field falls back to the default. A layer may also hold `[steps]`, `[kinds]`, `[tiers]` and `[routing]`, which set its steps apart from the engine's; see [Steps](#steps). A layer in `.agents/` that is not committed yet still works: runs read it from the main checkout and lessons land there, but arete never proposes automerge until it is committed. A layer outside the repo is read in place, lessons land in its `expertise.md`, and the miner leaves the repo's `.agents/` alone. Arete never proposes automerge for it: raise `trust.level` by hand.

After a run that had something to learn from (a failed check, a fix, a failed check in use, or PR feedback), the learn step rewrites `expertise.md` whole, in at most 80 lines. It checks each lesson against the code in the worktree and drops a lesson the code now contradicts, with the reason in the run's report. A rewrite over the cap is not applied. A layer outside the repo is made a git repo, and arete commits each rewrite there, so its log shows what was learned and from which run. A clean run learns nothing.

## Steps

Each pipeline step reads its own model, runner, effort, timeout, tries, tools, MCP servers, instructions and session: plan, build (also revise, the fixes and the CI fixes), verify, review, learn, uat and document. `arete steps` prints what each step resolves to, one JSON line per step. `--repo NAME` reads that repo's layer too, and `--kind feat` or `--kind fix` resolves for that kind of ticket.

```toml
[tiers.default]
"S+" = [
  { model = "fable" },
  { model = "opus" },
  { runner = "codex", model = "gpt-5.4" },
  { runner = "pi", provider = "openrouter", model = "qwen/qwen3-coder" },
]
A = { runner = "claude", model = "sonnet" }

[pricing.models]
"openrouter/qwen/qwen3-coder" = { input = 0.22, output = 0.95 }

[factory]
tier_map = "default"

[steps.plan]
tier = "S+"
effort = "high"
timeout_minutes = 20
tries = 2

[steps.implement]
tier = "A"
timeout_minutes = 40

[kinds.fix.steps.plan]
effort = "medium"
```

A step is named by its `[models]` role. `implement` is a second name for `build`, and a table names the step one way, not both. Four more names set a step of their own and have no `[models]` key: `check_repair` (the fix rounds after failed checks, `fix2` and up, and after a failed check in use, `verify_fix`), `review_repair` (the review fixes, `review_fix` and up, and the `repair` step on a held draft), `ci_repair` (the CI fixes, `ci_fix` and up) and `eval_grade` (the eval grader). The three repair steps inherit `build` and `eval_grade` inherits `verify`, so a config that names none of them runs as before. Every call's record header, `step_start` and `step_end` lines and timeline `agent` mark carry `atom`, the name of the step it resolved, such as `build`, `check_repair` or `eval_grade`.

```toml
[steps.build]
model = "model-a"
effort = "low"

[steps.review_repair]
model = "model-b"
effort = "high"
```

| Key | Meaning |
|-----|---------|
| `tier` | A tier of the tier map in use, which sets `runner`, `provider` and `model`. |
| `model` | The model. It wins over a tier at any level, and routes as in [Runners](#runners-devices-and-sessions). |
| `runner` | `claude`, `pi`, `codex` or `replay`. It wins over the runner the model name picks, but not over a `claude:`, `pi:`, `codex:` or `replay:` prefix. |
| `provider` | For pi, the provider put before a model name that does not start with it. |
| `effort` | claude's `--effort` (`low`, `medium`, `high`, `xhigh`, `max`), pi's `--thinking` (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`), or codex's `model_reasoning_effort` (`low`, `medium`, `high`, `xhigh`, `max`, `ultra`). Left out, the runner's own default. |
| `budget_usd` | The most the step's call may spend, in dollars above 0, passed as the call's budget as before: pi enforces it, and claude and codex calls ignore it. Left out, no budget; a repair step or `eval_grade` inherits the budget of the step it inherits. |
| `timeout_minutes` | The step's time limit. Default `limits.timeout_minutes`; learn 15, uat 10, research_plan 10, research_search 10, research_synthesis 10, research_audit 10, document 10. The checks keep `limits.timeout_minutes`. |
| `tries` | For check_repair, the attempts at passing the checks, and for ci_repair, the CI fix rounds, both defaulting to build's; review_repair rounds stay bounded by review's `tries`; for review, the most reviews in one run (a failed review costs a try, and a final `request_changes` keeps the PR a held draft with its blockers unresolved); for verify, the rounds of verify in use; for uat, the tries at the test steps. Default `limits.max_attempts`; uat 3; for research_plan, the plans asked for before the run fails, default 2; for research_search, the calls a worker makes at one task, default 2; for research_synthesis, the answers asked for in each round, default 2; for research_audit, the answers asked for per batch of claims, default 2; for document, the writes asked for before the existing text is kept, default 2. The other steps run once. |
| `tools` | The built in tools the step may use, from `Read`, `Glob`, `Grep`, `LS`, `Edit`, `MultiEdit`, `Write` and `TodoWrite`. Default `Read`, `Glob`, `Grep`, `Edit`, `Write` and `TodoWrite` for build, `Read`, `Glob` and `Grep` for the rest. The layer's shell rules still apply on top. pi always grants `Read`, `Glob` and `Grep`, so a pi step lists them. |
| `mcp` | Names of `[mcp.<name>]` servers from `~/.config/arete/config.toml`; claude and codex load those and no others. Default `[]`. verify also gets its own browser server when the layer serves an app, and that server wins over one of the same name. pi takes no servers. codex refuses servers with `env` values. |
| `instructions` | Which standing instruction files load: `user` and `repo`. Default both on claude (`~/.claude/CLAUDE.md`, the repo's `CLAUDE.md` and `CLAUDE.local.md`), `[]` on pi, and `["repo"]` on codex (repo `AGENTS.md` or `AGENTS.override.md`, supplied as developer instructions with automatic project document loading disabled). codex refuses `user`; if the login has global instruction files that the CLI cannot exclude, the call is withheld automatically. On claude, a value short of both becomes `--setting-sources`, which also leaves out that source's `settings.json`. pi runs with `--no-context-files` and takes only `[]`. |
| `modalities`, `min_context`, `structured_output`, `data_handling`, `floor`, `directory_cleanup` | Requirements a route must meet: input modalities (list), minimum usable context in tokens (whole number, at least 1), structured output (true or false), the data handling labels the step allows (list), a `[routing.floors]` name, and directory cleanup on codex (true or false; default true for verify, inherited by eval_grade). Default none otherwise. See [Chains and routes](#chains-and-routes). |
| `session` | `resume` or `fresh`. With `resume`, a later round of the step (for build, a fix, review fix, verify fix or CI fix) continues the session of its earlier rounds; with `fresh`, each round is a new session whose prompt carries the failing output. Default `resume` for build, `fresh` for the rest. The repair steps share the builder's session; a repair that sets its own `tier`, `model` or `runner` (even one equal to build's) and finds the saved session on another runner starts fresh with the saved context. learn and uat keep no session, so they take only `fresh`. |

Levels, where the last one to set a key wins: the defaults above, `[steps]` in `~/.config/arete/config.toml`, `[kinds.<kind>.steps]` there, `[steps]` in the repo's layer, then `[kinds.<kind>.steps]` there. A kind is `feat` or `fix`. A level that names a `model` also drops the `runner` and `provider` that lower levels set. A repair step or `eval_grade` first resolves the step it inherits through all these levels and its per-run table, then applies its own tables in the same order; its own `tier` or `model` replaces the inherited route. A step with neither a model nor a tier takes its `[models]` role. `research_plan` reads its whole `[steps.research_plan]` table from the engine config, takes only read tools, no MCP servers and a fresh session. `research_search` reads its table from the engine config too, with default tools `WebSearch` and `WebFetch`, read only, no MCP servers and a fresh session; no other step may list a web tool. `research_synthesis` is read only with no MCP servers and a fresh session, and refuses a web tool. `research_audit` is read only with no MCP servers and a fresh session, with default tools `Read`, `Glob`, `Grep` and `WebFetch`, and refuses `WebSearch`. The other roles, such as triage, chat and the miner, read `[models]` alone; `arete steps` shows `tools`, `mcp`, `instructions` and `session` for them too, but only the pipeline steps use those four.

`[tiers.<map>]` lists tiers by name, and a name may be any quoted string, such as `"S+"`. A tier is one table or a nonempty list of tables, in order: `"S+" = [ { model = "opus" }, { runner = "codex", model = "gpt-5.4" } ]`. Every entry of every map is checked when the config loads, used or not. A tier entry that names an unknown runner, or a runner and model that cannot go together (such as `claude` with `gpt-5.4`, or `codex` with `opus`), fails with one line that starts `[tiers.<map>] <tier>:`, or `[tiers.<map>] <tier> entry <n>:` for a list. A layer's `[tiers.<map>]` adds to the engine's map of the same name, or replaces a tier it names again. `[factory] tier_map` names the map in use; it may be left out when there is one map. A repo layer's `tier_map` wins over the engine's choice and may supply the only selection. With several maps and no engine selection, validation of tiered steps waits for the repo layer; `arete steps` without `--repo` still requires an engine selection.

To move every tiered step between Claude and OpenAI, define maps with matching tier names:

```toml
[tiers.claude]
strong = { runner = "claude", model = "claude-opus-4-5" }
fast = { runner = "claude", model = "claude-sonnet-4-5" }

[tiers.openai]
strong = { runner = "codex", model = "gpt-5.4" }
fast = { runner = "codex", model = "gpt-5.4-mini" }

[factory]
tier_map = "openai"

[steps.plan]
tier = "strong"

[steps.build]
tier = "strong"

[steps.review]
tier = "fast"
```

Changing the one `tier_map` setting in the engine config switches all steps that name a tier. Set `[factory] tier_map = "openai"` in one repo's layer instead to switch only that repo. Explicit step models still win over tiers. Pick models available to your login from `codex debug models`.

A step with no tier takes its `[models]` role, so switch that role with a `codex:` model too:

```toml
[models]
learn = "codex:gpt-5.4-mini"
```

`openai/gpt-5.4-mini` keeps routing to pi on keys from `pi.env`. `arete steps --repo NAME` shows the resolved runners, models, instruction defaults and codex safety notes. For one adw run, `--set implement.runner=codex --set implement.model=gpt-5.4` overrides just build.

### Chains and routes

Each `arete steps` line also holds `chain`, `fallback` and `routes`. `chain` is the ordered list of models the step could use: the requested one first (position 0), then the other entries of its tier. A step that names a tier uses that tier. A step with a `model` or a `[models]` role looks first in the tier the step names, if that tier holds the model, then in the selected map's tiers in declaration order, and takes the first tier that holds it; duplicates are fine. A model outside every tier has a chain of itself. Models match by vendor and id, so `opus`, `claude-opus-5-5` and `anthropic/claude-opus-5-5` are one model and `openrouter/openai/gpt-5.4` is not `openai/gpt-5.4`. Each link carries the step's effort, or the nearest lower level its runner takes (its default when none is lower), and that runner's default `instructions` unless the step sets them. `fallback` is the `[routing] fallback` result, engine AND layer.

`routes` lists every credential path for every model in the chain, with `order`, `runner`, `model`, `credential`, `billing`, `price`, `state` and a one-line `detail`:

| credential | meaning |
|---|---|
| `cli_login` | the claude or codex CLI on its login (free) |
| `cli_key` | codex on an API key (paid) |
| `pi_login` | a pi subscription login, such as `openai-codex/<model>` (free); Claude models have none |
| `pi_oauth` | a pi OAuth login that pays per use, such as OpenRouter's (paid) |
| `pi_key` | a pi key, such as `anthropic/<model>` on an Anthropic Console key (paid) |
| `local` | a model on a `local_providers` provider or a local base URL (free) |

A step can declare what a route must offer (`modalities`, `min_context`, `structured_output`, `data_handling`, `floor`, `directory_cleanup`). Route capabilities come from owner tables, `[routing.capabilities."<model>"]` or `[routing.capabilities."<runner>:<model>"]`, keyed like `[pricing.models]`, with `modalities` (names), `context` (tokens), `structured_output` (true or false) and `data_handling` (one label of your own). A layer's table overrides the engine's field by field. They win over two provider sources: pi's `models.json` (`input` and `contextWindow`) and, for `openrouter/...` routes still missing a field, OpenRouter's public list (`architecture.input_modalities`, `context_length`, `supported_parameters`). Data handling comes only from your tables. An unknown capability that the step requires makes the route `ineligible`; its `detail` lists every unmet requirement. `routes` then also carries `capabilities`, each field with its `source` (`config`, `models.json` or `openrouter`), and `arete steps` adds `selection`: the route a run would take, the `skipped` routes with reasons, `requires`, `request_tokens` and `error`.

```toml
[routing.capabilities."replay:scratch-a"]
modalities = ["text"]
context = 8000
data_handling = "no_training"

[routing.floors]
strong = ["scratch-b"]

[steps.plan]
modalities = ["text", "image"]
min_context = 4000
data_handling = ["no_training"]
floor = "strong"
```

`floor` names a list in `[routing.floors]`, the models you approve for the step; alternatives outside it are `ineligible`, while the requested model is exempt. Arete holds no ranking of its own. `min_context` is checked against the larger of itself and the request's size, estimated as the UTF-8 bytes of prompt, system and schema divided by 3; `arete steps` previews with the step's own number.

A step with none of these keys runs the requested route as before. A step that declares any of them keeps its requested route when that meets them, otherwise takes the first `ready` alternative in `routes` order, otherwise fails with `no route meets step <step>'s requirements: ...` and no provider call. The call record's header holds `route` (the `selection` shape, for the actual request), so a skipped route's reason is in the record. The requirement check and the availability fallback below share one eligibility decision, and `[replay] all` does no selection; a step's own `replay` route is checked like any other.

States are `ready`, `excluded` (the device cannot run it), `incompatible` (the step's tools, `mcp` or `instructions` cannot load there), `ineligible` (the route does not meet the step's declared requirements), `refused`, `missing_login`, `expired_login` (a login that only expires soon is still `ready`), `missing_key`, `unavailable`, `held` (a stored usage limit for that account or model), `policy` (`[routing] payment_modes` leaves out the route's payment mode) and `budget_spent` (a paid route when the loop budget is used up; shown for `loop` and `classify`). Each route also carries a `payment_mode`, and each line a `payment_modes` list, the modes in force. Free routes come first, by requested model, tier position, then CLI before pi. Paid routes follow, by input plus output USD per million tokens, from `[pricing]`, then an imported [price snapshot](#price-snapshots), then pi's `models.json`, then OpenRouter's public model list. A route with a missing component, or no price, sorts last. `price` keeps `cache_read` and `cache_write` apart, names its `source` and carries the applied `quote`. Arete reads OpenRouter's list once, with a 5 second limit and no credentials, and caches it in `openrouter-prices.json` in the state folder for 6 hours; if the lookup fails, those prices are unknown. `routes` is empty under `[replay] all`, which does no route discovery.

Every agent call rebuilds its route list and launches the first route that is usable, in the order `routes` shows. A call that has a saved session tries that session's runner first. Each route passed over is recorded with its state and reason in `skipped`, in the call's record header, its `step_start` line and the result. A usage limit or spent credit on the route stores its hold and moves on to the next route at once, without using one of the step's tries; a missing login or key, a spent budget and a held route are skipped the same way. A failed agent, a failed check, a bad answer or a timeout stays on its route. A limit whose reset has already passed does not retry that route for a minute. A fallback is judged on the tools the call ends up with, `tools` when the call sets it and `allowed_tools` otherwise, less what `disallowed_tools` names; a pi or codex fallback that would grant a read tool the call lacks, and a codex fallback when the call denies anything beyond git writes and `gh`, is `incompatible`.

When no route is usable, the call waits only if some route holds a limit with a known end, then rebuilds the list at the earliest end; the wait counts as a `limit` wait, and `wait_out_limits = false` returns at once instead. With no end to wait for, the call fails with one line, `no route can run this call:`, naming every route and its reason. Turning `fallback` off, in the engine or in a layer, cuts the list to the requested route before any availability check, and a replayed call never looks at routes at all.

For the `"S+"` list above, `arete steps` shows a plan step like this, a representative sample (trimmed; each route also has `provider`, `tier`, `effort`, `instructions` and `price`):

```json
{"step": "plan", "tier": "S+", "chain": ["fable", "opus", "codex:gpt-5.4", "openrouter/qwen/qwen3-coder"],
 "fallback": true, "payment_modes": ["subscription", "api_key", "local"],
 "routes": [
  {"order": 1, "runner": "claude", "model": "fable", "credential": "cli_login", "billing": "free", "payment_mode": "subscription", "state": "ready"},
  {"order": 2, "runner": "claude", "model": "opus", "credential": "cli_login", "billing": "free", "payment_mode": "subscription", "state": "held", "detail": "claude usage limit reached, waiting until Thu Nov 12 18:00"},
  {"order": 3, "runner": "codex", "model": "gpt-5.4", "credential": "cli_login", "billing": "free", "payment_mode": "subscription", "state": "missing_login", "detail": "codex CLI is not logged in; run `codex login`"},
  {"order": 4, "runner": "pi", "model": "openrouter/qwen/qwen3-coder", "credential": "pi_oauth", "billing": "paid", "payment_mode": "api_key", "state": "ready"},
  {"order": 5, "runner": "pi", "model": "anthropic/claude-fable-5-2", "credential": "pi_key", "billing": "paid", "payment_mode": "api_key", "state": "missing_key", "detail": "no supported anthropic key in pi.env or the environment"}]}
```

Membership: a call's chain is the tier its model belongs to; a model in no tier has a chain of itself. Order is deterministic: free routes first by requested model, tier position and CLI before pi, then paid routes by quoted price. A route with an unknown quote (a missing component, or no `[pricing]`, `models.json` or OpenRouter price) sorts after every priced route, so list a price under `[pricing.models]` (or import a snapshot) where a paid route should sort by cost. A hold on a model (a per-model limit) skips only that model's routes; a hold on an account skips every model on that login, so the next route may be another model of the same tier or another account. An interrupted call that lands on the same runner and account continues its session; any other route restarts with the original prompt and a note, and the records and costs count each launch once. Effort adapts to each runner: a link keeps the step's effort when its runner takes it, else the nearest lower level, else the runner's default. `fallback = false`, in the engine or in a layer, cuts the list to the requested route, and a replayed call never discovers routes.

`[routing] payment_modes` restricts which billing sources a route may use: `subscription` (a CLI or pi subscription login), `api_key` (a native API key, a pi key, and a pi OAuth login that pays per use, such as OpenRouter's) and `local` (a local provider or base URL). Omitted means all three; a layer's list can only remove modes from the engine's, never add one back. The route's actual billing decides its mode, not where the credential is stored or whether a call reported zero cost. A route outside the list is `policy` with a detail naming `payment_modes`, before any launch, in previews, runtime selection, waits and every caller; a blocked route never justifies waiting for a hold. `["subscription"]` keeps subscription-to-subscription fallback working, such as claude to codex. A bad value fails the load in one line starting `[routing] payment_modes`.

A route in the same folder, runner and account resumes the stopped session with the carry-on message. Any other route starts a fresh session in the same folder and gets the original prompt, a note that an earlier attempt stopped at a limit, and that attempt's last message. A later retry stays on the runner its saved session used.

Config load and step resolution refuse, in one line that names the table: a `[routing]` key other than `fallback`, `payment_modes`, `capabilities` and `floors`, a `fallback` that is not true or false, a `payment_modes` that is not a nonempty list of distinct `subscription`, `api_key` and `local`, a capability table with an unknown key, a wrong type or `context` under 1, a floor that is not a nonempty list of model names, a step `floor` the `[routing.floors]` table lacks, a `min_context` under 1, a `structured_output` that is not true or false, a tier with an empty list or an entry that is not a table, an unknown step, kind or key, a tier the map does not have, a runner other than `claude`, `pi`, `codex` or `replay`, an effort the step's runner does not take (including `off` on codex), `tries` or `timeout_minutes` under 1, several tier maps with no engine or repo selection, a `tier_map` with no such map, a tool outside the `tools` list above (`Bash` and `mcp__` names included), a pi or codex step whose `tools` leave out a read tool (research_search excepted), a `[research]` key other than `planner`, `search`, `search_workers` and `search_share`, a `search` that is not true or false or is true with `planner` off, a `search_workers` that is not a whole number from 1 to 8, a `search_share` that is not a number above 0 and under 1, a web tool on any step but research_search, a research_search `tools` outside `WebSearch`, `WebFetch` and the read only tools, an `mcp` name with no `[mcp.<name>]`, any `mcp` or `instructions` on pi, `instructions = ["user"]` on codex, an `instructions` value other than `user` and `repo`, a `session` other than `resume` and `fresh`, `resume` for learn, uat, research_plan or document, a research_plan or document `tools` outside the read only tools, any `mcp` on research_plan or document, a `[document]` key other than `enabled`, `voice`, `style` and `words`, an `enabled` that is not true or false, a `style` that is not a string, a `voice` that is not a list of files inside the folder of the file that names them, a `words` entry that is not two whole numbers, low then high, for `message`, `ticket` or `pr`, and an `[mcp.<name>]` with an unknown key, with both or neither of `command` and `url`, or with `args` beside `url`. A loop or session whose every route is excluded or incompatible on its device, such as codex on an ssh or exe device with no other route in the tier, is refused before starting. A mistake in a layer fails the run at its start, naming the layer file, and `arete steps --repo NAME` shows it first.

### Price snapshots

An owner can import one normalized price and lifecycle snapshot: `arete prices import FILE`. It is a reference quote only. It orders paid routes and goes into records. It never changes charged cost, budgets, route state, eligibility, `payment_modes`, tiers or `config.toml`, and it fetches nothing. Rankings, candidate lists and real tariffs stay in your private layer or files.

One JSON object. Unknown keys, wrong types, negative, `NaN` or infinite prices, a name used twice and a rate with no component are refused in one line naming the place.

| Field | Meaning |
|---|---|
| `schema` | `1` |
| `source` | where the numbers came from (nonempty string) |
| `observed` | ISO time with a zone, not in the future |
| `routes` | nonempty list of route entries |

A route entry has `route` (named like `[pricing.models]`: `<runner>:<model>` or `<model>`), `kind` (`native` or `marketplace`, kept apart), `provider` and `model` (who serves and bills, and its model id), `currency` (three capitals), `unit` (`per_million_tokens`, `per_thousand_tokens` or `per_token`) and `rates` (nonempty list). Optional: `aliases` (more route names; each must have the same vendor as `route`), `retired` (ISO time with a zone), `replacement` (a route name), and `source` and `observed`, which override the top level. A rate has any of `input`, `output`, `cache_read`, `cache_write` (a component left out is unknown, never zero) and optionally `from` and `until` (ISO with a zone; from inclusive, until exclusive), `when` and `label`. `when.min_input_tokens` is a whole number of 1 or more; any other `when` key is kept but can never be proven.

```json
{"schema": 1, "source": "scratch-notes", "observed": "2026-11-12T00:00:00Z", "routes": [
  {"route": "pi:openrouter/acme/scratch-a", "kind": "marketplace", "provider": "openrouter",
   "model": "acme/scratch-a", "currency": "USD", "unit": "per_million_tokens",
   "rates": [{"input": 3, "output": 9, "cache_read": 0.3},
             {"input": 5, "output": 20, "when": {"min_input_tokens": 200000}, "label": "long context"}]},
  {"route": "replay:scratch-a", "kind": "native", "provider": "scratch", "model": "scratch-a",
   "currency": "USD", "unit": "per_million_tokens", "rates": [{"input": 2, "output": 10}]}]}
```

Names match a route exactly, with no fuzzy vendor or id matching, so a native and a marketplace route never share an entry.

- `arete prices diff [FILE]` reviews the stored snapshot, or diffs FILE against it, as one JSON line per finding and a summary line.
- `arete prices import FILE` prints the same, then stores the snapshot as `price-snapshot.json` in the state folder. A rejected file leaves the stored one untouched.
- `arete prices recompute RUN_ID STEP` recomputes a call's `reference_usd` from the quote its record retained, never from the stored snapshot.

A rate applies when its period holds the clock's time. A `min_input_tokens` rate applies when the request's input is known to reach it: the size estimate in previews, the call's input tokens in a record when it took one turn. The last applicable rate wins whole, and the `unproven` list in the quote names in-effect conditional rates left out. A route whose entry has no applicable rate has an unknown quote and sorts last.

Findings: `stale` (observed over 30 days ago, or no rate in effect), `retired` (with the replacement), `changed` (an applicable component moved by 1% or more or became known or unknown, a currency, unit, kind, provider, model or lifecycle field differs, or an alias moved), `added` and `dropped` (routes or aliases). All rate schedules are compared separately, including material component changes, additions, removals, order, conditions and effective periods, even for unconditional, expired or future rates. This comparison does not make an unproven rate applicable.

### Benchmark local routes

A model that fits in VRAM may still fail a real task. `arete benchmark run MANIFEST` measures endpoints already configured in Pi's `models.json` under 8K and 32K prompts and configurable concurrency. It never pulls weights, provisions a device or schedules load. Nothing runs unless the owner types the command, and the run is finite.

Each cell sends three probes per worker (plain text, structured JSON, a forced tool call) with synthetic prompts that carry a unique nonce, so a prefix cache cannot flatter first-token latency. The report holds, per candidate, the manifest metadata, observed memory, first-token and completion medians, tokens per second, context failures, structured-output and tool reliability, and quality as accepted over sent requests. Outcomes are `ok`, `rejected` (well-formed, wrong word), `malformed`, `context_overflow` (including silent truncation), `out_of_memory`, `timeout` and `error`.

```toml
contexts = [8192, 32768]      # 1 to 4 sizes, default shown
concurrency = [1, 2]          # 1 to 4 levels of 1..16, default shown
max_tokens = 64               # 1..1024
timeout_seconds = 120         # per request, 1..600
max_seconds = 900             # whole run, 1..7200; later cells are not_run

[escalation]                  # optional; both rates in USD per million tokens
input_usd_per_mtok = 1.0
output_usd_per_mtok = 2.0

[candidates.example]          # 1 to 12 candidates
route = "fixture/ok"          # provider/model from models.json, or fixture/<scenario>
quantization = "placeholder"
runtime = "placeholder"
runtime_version = "placeholder"
hardware = "placeholder"
memory_gb = 24
window = 32768                # context the runtime is configured with; larger cells are unsupported
# device = "name"             # optional devices.toml name (local or ssh only)
```

Real candidate lists and escalation rates belong in a private file such as `~/.config/arete/benchmark.toml` or a private layer, not in the repo.

Before any request, the endpoint must answer `GET /models` and list the model, and a named device must pass its probe. Otherwise the candidate is `unavailable`, sends nothing and is not scored, so an offline host never lowers quality. Observed memory comes only from an Ollama-style `/api/ps`; anything missing prints `unknown`. Zero API spend is reported beside wall time and the escalation cost: what the rejected or failed requests would cost at the `[escalation]` rates. Unsupported, not run and unavailable cells add nothing.

`arete benchmark fixture --port N --seconds S` serves loopback fixtures at `http://127.0.0.1:N/<scenario>/v1` for `S` seconds (1 to 3600): `ok`, `oom` (out of memory above an estimated 16000 prompt tokens), `overflow` (context error above 8192) and `malformed` (broken JSON and tool arguments). Routes `fixture/<scenario>` run the same scenarios in process, plus `fixture/offline`.

The finite opt-in command for a ready endpoint:

```
arete benchmark run ~/.config/arete/benchmark.toml --out ~/benchmarks/local.json
```

It is bounded by `max_seconds` and `timeout_seconds`, and by at most 12 candidates x 4 contexts x 4 levels.

## Replay

The `replay` runner answers each agent call from a file and calls no model, so a check of the pipeline's own flow runs in seconds and costs nothing. A step picks it like any runner: `runner = "replay"` in a step table or a tier, or a `replay:<name>` model. `[replay] all = true` in `~/.config/arete/config.toml` sends every agent call to it, whatever the step sheet says, and `arete steps` shows `replay` for every step.

```toml
[replay]
all = true
dir = "~/replay/answers"
```

`dir` is the folder of answers; `ARETE_REPLAY_DIR` wins over it. A relative folder is read from the folder arete runs in, not the step's working folder. A call traced as step `plan` reads `<dir>/plan.json`, named as its step row and record are. A second call of the same step in the same run reads `plan2.json`, a third `plan3.json`; a call with no run id always reads the first name. The file is one JSON object:

| Key | Meaning |
|-----|---------|
| `text` | The answer's text. |
| `structured` | The structured answer, for a step that passes a schema. |
| `ok`, `error` | Default true and empty. `ok = false` with no error fails with one that names the file. |
| `cost_usd`, `tokens_in`, `tokens_out`, `turns`, `first_context_tokens`, `cache_read`, `cache_write`, `api_seconds` | What the call reports. Default 0. |
| `session` | The session id the call reports when it resumes none. Default a new random id; a call that resumes a session reports that one. |
| `seconds` | How long the call sleeps before it answers. Default 0. |
| `tool_calls` | A list of `{"name", "input"}` the call records as tool calls before it answers; a call named `ask_owner` also records the tool's reply, so a replay step can ask. Any other call may add a string `result`, recorded as its tool result, and `error` (true or false) to mark that result as a failure. Any value that is not a list of objects with a `name` fails the step; leave the field out for no tool calls. |
| `stream` | A list of turns, each with `tokens_in`, `tokens_out` and `cost_usd`. The call's turns and usage become their sums, and the call reports one turn at a time before it answers, so a token or spend cap can stop it partway; a stopped call applies no patch. |

When `<dir>/<step>.patch` exists, arete applies it to the call's folder with `git apply` before it answers, even when the answer fails; this is how a replayed build changes files. No answer folder, a missing file, a file that is not one JSON object, a bad value and a patch that does not apply each fail the step with an error that names the folder setting or the file.

A replayed call is recorded like any other: its step row, its record header and its timeline `agent` mark say `replay`, the raw record holds one `replay` line naming the answer and patch files, and it costs what the file says, which `[pricing]` never changes. Usage limit holds, the login check, the guard, JEV rules and devices do not apply to it. JEV calls are not agent calls and still go to JEV. `all = true` also sends chat, triage, loops and sessions to replay, so turn it on in a scratch `ARETE_CONFIG_DIR` and `ARETE_STATE_DIR`.

`arete replay sample FOLDER` writes a fixture into a new or empty folder: `repo` (a git repo with one commit of `notes.txt`), `origin.git` (a bare repo set as its `origin`), `layer` (a layer whose one check greps `notes.txt` for `kumquat`) and `replay` (an answer for plan, build, verify, review, learn and document, and a `build.patch` that adds the word). It prints, as TOML, the `[replay]` and `[[repo]]` lines to add to the config; the repo has `tracker = "local"`, no `github` and no `pipeline`, so poll never picks it up; add `pipeline = true` with `--adw` and `arete poll` starts its `todo` tasks. `--adw` adds `[factory] path = "adw"` to the layer, so `arete run replay-sample t-...` on a task from `arete task add --repo replay-sample` runs the [adw flow](#the-adw-flow) and pushes the branch to `origin.git`. On scratch state `arete run` calls only a server started on that state (see [From the CLI](#from-the-cli)), so with none it runs in its own process, as before. The answers serve both paths. A `github` name added to the sample's `[[repo]]` makes the adw flow go on to the PR, CI and publish steps, which call GitHub.

## Verify in use

Run Arete's durable behavioral checks with `uv run python -m unittest discover -s tests -v`. They exercise complete offline workflows against a local tracker and a local bare Git origin, including rejected checks, blocked verification, rejected reviews and a run parked with scratch held. Writer regressions preserve warning and prerequisite bodies, complete commands and code indentation, and keep synthetic private voice text out of captured GitHub publication. Scratch checks cover finalization of created, imported, hard-linked and re-dated entries. Add the same command under `[checks]` in Arete's layer so these cases run before model verification on every candidate branch.

Configure cheap environment prerequisites under `[verify.preflight]`:

```toml
[verify]
preflight_seconds = 30

[verify.preflight]
python = "python3 -c 'import sqlite3, tempfile'"
git = "git --version"
```

These named probes run in the worktree with the layer's command environment before the app or verifier starts. They share one time budget, default 30 seconds and maximum 300. The first failure or timeout records the command, exit and output in `<step>-preflight.json` and `<step>-preflight.md`, then marks verification blocked without a model call or implementation repair. Successful prerequisite evidence stays in the run directory; the verifier still checks the product's behavior. Use `[checks]` for product assertions and `[verify.preflight]` for requirements such as an available runtime or fixture transport. Existing tool permissions, guards and route capability rules remain authoritative.

Checks passing is not the bar. After the checks, a verifier agent uses the change the way its user will: it starts the app with `verify.start`, waits for `verify.ready`, then drives it through a headless browser (Playwright, an isolated profile), `curl`, or the CLI, and reports each check it made with what it saw. Reading the code and running the test suite do not count as checks. The builder does not add tests unless the repo's own rules ask for them.

Eligible local Codex verification attempts get a fresh mode-700 `.arete-scratch-<16 hex>` folder in their worktree. Arete exclusively creates five empty fixture slots: `config/config.toml`, `state/arete.db`, `fixtures/config.toml`, `fixtures/fixture.db` and `out/output.txt`. Rewrite these in place for fake configuration, SQLite state and output. Files and folders the verifier or the service creates in scratch are verification output and are archived with the scratch root at finalization, without any ownership claim from the guard. An entry that existed before the attempt, a folder holding one at any depth, a file with more than one link, an entry on another device, or one that cannot be classified keeps the attempt pending, and the diagnostic names each such entry and its reason under `unknown_entries` and `reasons`. The test compares the APFS file id with the largest id of the layout the attempt recorded, never a timestamp, and links are never followed.

Harness authority and evidence live under the operating-system account's `.config/arete/verification-authority`, independently of HOME and Arete environment settings. Signed initialization intent is persisted before creating workspace resources, with progress checkpoints and lease release on failure. Records bind each run directory, step, attempt, workspace, original resources and lifecycle. Hook arguments only select this authority; hooks never create or adopt records. Repository commands, including checks and setup, exclude the authority through the shared command seam. A signed baseline is captured before repository execution; kernel process identities and ancestry distinguish unrelated concurrent runs while retaining unknown descendants. Producers wait behind a pipe gate until their process group is durably registered: engine death before release cannot start actor code, and polling waits for registered groups after release. Kernel process identities captured before release also detect descendants that leave their group. A new process whose origin cannot be ruled out conservatively keeps the original worktree retained until it exits; no unrelated process is killed. With the producer baseline, Arete records every job label loaded in the account's `gui` and `user` launchd domains, idle or running. A process that launchd started for one of those labels does not hold an attempt when the job is defined on the sealed system volume and runs the program it declares, as an Apple on demand agent does. A sealed job whose label is not in the baseline holds as a submitted launchd job, and as unclassified when the record has no label baseline. A producer that boots out a baseline label and bootstraps it again, or starts a loaded agent with `launchctl kickstart`, is not told apart from a system start. Submitted launchd jobs, orphans of a producer and processes that cannot be classified do hold it. The retained diagnostic and the run log name each holder with its pid, executable and reason. Legacy ambiguous producer records and relocated registered roots remain retained. Snapshot reads traverse checked directory descriptors without following links, validate original single-link file identities and enforce size limits. After producers stop, finalization snapshots the registered slots and journals a transaction to archive the whole scratch root in protected evidence. It seals the owned captured directories against new entries through existing directory handles and validates the complete tree again before marking the attempt `done`, releasing the clean worktree for removal or reuse. A run that ends while scratch is left still records the edits, git-ignored files and branch heads it left. The record leaves out engine scratch roots, which are the folders that a signed attempt record names. Arete records a folder that only has the scratch name, and removes it like other leftovers. A folder whose name a record names keeps the workspace retained in every state, and the park message names the held folders. Where arete cannot read the attempt records, every folder with the scratch name keeps the workspace retained. Once the attempt is `done` a forced run reuses the worktree as after a clean run, and a file changed after that record still stops it. The kept edits are committed to the local branch first, so the record names the kept commit and a forced run continues from it. A raced replacement is restored without overwriting a newly created source entry; incomplete restoration retains both resources and the worktree for retry. Entries the attempt did not create stay protected, and no recursive source deletion occurs. Ordinary polling retries failed snapshots and cleanup transactions after completion, cancellation or interruption. Previously authenticated snapshot-only attempts can finish using their registered identities; unsigned markers grant no recovery permission.

Trusted ownership context is restricted to local Codex confinement in isolated worktrees outside broad temporary-directory grants, with the registry, evidence and neighboring worktrees excluded through its existing permission profile. Service-backed attempts also receive scratch and substitute `{scratch}` in start commands and notes. Supported local services reuse the existing macOS confinement seam with workspace writes, neighboring-worktree exclusions and secret-file protections, without Pi credential-directory grants. Their environment drops credential values and directs Arete config, state, logs and worktrees into scratch. Startup failure, teardown and cancellation retain the same preservation lifecycle. Other runners continue ordinary verification without enhanced ownership hooks. Unsupported routes receive guidance to run ordinary checks and report unavailable required fixtures or services as blocked. A Codex verify with a verify URL on `localhost` or a loopback IP literal may use one browser server, the one `scratch.browser_command` builds: the pinned `@playwright/mcp` run by the engine's `node` in its own process group, inside a profile that writes only a `browser` folder under the run folder (mode 700, never published) and the run's evidence folder, hides the authority, the state folder except this run's evidence, its own `browser` folder and the pinned server folder (read only), the real Chrome profile, keychains and shell startup files, denies mach lookups outside an allowlist, and allows outbound network only to the app's TCP port on `localhost` and unix sockets under its folder, which the server reaches through a path relative to that folder so a long run folder still fits the socket name limit. Chrome gets `--no-sandbox` only there. `scratch.isolation` accepts a server list only when it is exactly that server, and any failure to build it fails the step rather than falling back. Ending the attempt kills the group and keeps the worktree when it cannot confirm that nothing able to write the browser folder, evidence or workspace remains. The mach allowlist is a candidate: a person must confirm it with a real headless Chrome run on a machine that does not itself run under confinement. Local ordinary producers exclude the authority store, including evidence; this does not grant them trusted fixture authority. Tracking a process group or isolating its environment grants no filesystem protection. These profiles also deny LaunchServices opens, Apple events and launchd job creation, deny connections to the tmux, ssh agent and docker sockets and to the port of the live dashboard or a running `arete serve`, and the service profile allows unix socket connections only under its workspace and the system temp folders. A verify URL on a dashboard port is refused as blocked. Engine fetches and pushes keep the ssh agent, and `arete install` and local notifications keep launch rights. The installed guard supplies authenticated facts about the five slots to JEV for the entire original command; it never replaces a hold, changes a threshold or exempts a command. Unsupported confinement and evaluation grades receive no enhanced ownership context; grades need separate lifecycle support before this can be enabled there.

Arete run with `ARETE_VERIFY_SCRATCH` set, or with `ARETE_CONFIG_DIR` or `ARETE_STATE_DIR` inside a scratch folder, such as `arete steps` or `arete dashboard` on scratch config and state, uses only that scratch folder: an unset config or state folder becomes its `config` or `state`, logs and worktrees go under the scratch state unless set, and arete refuses to start when the folders name no valid scratch container, two of them, a folder reached through a link, or this machine's config or state. This credential-isolation check grants no ownership authority. On macOS, isolated state pins a checked database descriptor through SQLite close, serializes connections, and uses memory journals for disposable fixtures. The walk to a scratch folder opens the folders above it for search only and without following links, so it passes a folder that it may not list, such as the worktrees folder inside the service profile. Existing sidecars, WAL databases, nested isolated access and unsupported platforms are refused; ordinary state databases keep their existing behavior. The guard hooks (`arete guard` and `arete codex-guard`) ignore these settings and judge exactly as they otherwise would, so a scratch setting that reaches a hook never turns off its secret-file rules or JEV. It uses no login, key or price lookup of the machine. It drops every credential-named environment variable (`OPENROUTER_API_KEY`, `GH_TOKEN`, `ANTHROPIC_API_KEY` and the like) when it starts. Claude and Codex report no login without asking their CLIs. Pi reads its logins, settings and models only from `pi-agent/` in a scratch state folder, and Pi and JEV read keys only from `pi.env` in a scratch config folder; each is read only as a plain file of this user inside a real scratch folder, reached without links or `..`, and anything else counts as missing. Jira credentials are refused, `gh auth token` is not asked, and OpenRouter prices come only from the state folder's cached `openrouter-prices.json`.

- Pass: the verification report goes in the PR body. By default, screenshots are pushed to the orphan branch `arete/evidence` and linked from the body. Set the repo layer’s `[verify] publish_evidence = false` to retain screenshots locally without uploading them; both classic and ADW runs still deliver the PR and its text report.
- Fail: the build session resumes with what failed, then the checks and the verifier run again, up to the verify step's `tries` (default `limits.max_attempts`).
- Blocked: the verifier could not check something (a service it may not reach, a sign in it cannot finish). A check that fails because the verifier's own tools could not run it (the verifier marks it `cause: environment`, such as a closed browser), and an app start whose command exited with a sandbox denial in its log (Operation not permitted, EPERM, `[Errno 1]`, sandbox-exec), are blocked too, never a failure that sends the builder to repair code that is not at fault; a failed check with `cause: change` or no cause stays a fail. The PR body says what it would take. It opens as a draft when the review did not approve, or when the blocker or summary names a guard, JEV or credential refusal; an approved review with any other blocked check leaves it ready. A run that pushes to an open PR of its branch, or continues an earlier run and verifies and reviews it again with nothing new to push, applies the same rule to it: held (review not approving, or a check in use blocked by a refusal) turns a ready PR into a draft with the same note; not held marks a draft PR ready for review and judges it for automerge as a PR the run opened (trust level, triage approval, the JEV risk hold, conflicts, GitHub's merge state). So does a run whose `pr` step finds the PR open on the branch because the prepare outcome named none, as when a continuation of a run that opened its own PR runs `pr` again. A PR whose head someone changed after the run pushed or checked it is left as it is and the run log says so; a PR that an earlier CI watch stopped follows the CI section; an approved repair still leaves its draft for a person; a revise run applies the rule to none of the PRs it pushed to, checked again or found, and automerges none of them.

Arete stops the server's whole process group after each verifier run, so no dev server outlives its step.

## Document

Generated tickets, messages and PR text read long and formal when they inherit the planner's voice. The `document` step is a writer with a fresh context: it sees only verified facts, the action wanted, acceptance conditions, the audience and channel, and optional voice examples, never the plan or any reasoning. It is off by default, so runs behave as before. Turn it on in the engine config or the repo's layer:

```toml
[document]
enabled = true
voice = ["voice.md"]
style = "Short sentences. Name the command."
words = { message = [20, 60], ticket = [80, 140] }
```

- `enabled`: run the writer on the PR text. The layer overrides the engine config key by key; `words` merges per kind.
- `voice`: files of example text, one example per line, read from the folder of the file that names them. A layer's `voice` counts only when the repo's `layer` is outside the repo, so voice samples are never committed to a public tree. Examples set the tone; the writer is refused if it reuses a line of 30 characters or more.
- `style`: free notes added to the brief.
- `words`: soft targets, `message` 20 to 60, `ticket` 80 to 140 and, if you set it, `pr`. A doc has none and is as short as completeness permits. Arete reports a text over its target and never cuts it.

In a run, the writer replaces the PR `## Summary` with a plain text of what changed, why it matters to the reviewer and what was checked. It runs after learn and before the PR is filed; the machine plan stays in its own `<details>` block. A writer failure never fails a run: when its text is refused after `tries` writes, or no answer comes, the build summary is used and the run log says `document: kept the build summary: <why>`. The accepted text is saved as `document.md` in the run folder.

`arete document BRIEF [--repo NAME] [--draft FILE] [--text]` runs the same writer by hand, whether or not `enabled` is set. `BRIEF` is a JSON file, or `-` for standard input, with `kind` (`message`, `ticket`, `pr` or `doc`), `facts` and `action` (both required), `acceptance`, `audience`, `channel`, `draft`, `ask`, `owner`, `deadline`, `keep` (strings that must survive character for character) and `drop` (strings the writer may remove). `--draft` reads the brief's `draft` from a file. It prints one JSON line with `kind`, `source`, `title`, `text`, `words`, `target`, `over_target`, `kept`, `faults`, `tries` and, for every kind but `doc`, `screened`, the text after the tracker screen; `--text` prints the text alone. The exit is 0 when the writer's text was accepted, 1 when not, and 2 for a bad brief or config.

Arete checks every text before it takes it, with plain code and not a second model: each inline code span and fenced or indented code line of the facts, action and acceptance of a message, ticket or doc must appear; so must each line of a warning, prerequisite or expected output section of a doc draft or its facts (a `#` or underlined heading's whole section, or a labelled line and the block after it), and every `keep` string. A message must hold the supplied `ask`, `owner` and `deadline`. A message or ticket that names an `@handle`, date, weekday or day word (`today`, `EOD`) that the brief lacks is refused. A text with a reasoning tag or a reused voice line is refused. With a `draft`, a refused answer leaves the draft whole; without one the text is empty and `source` is `fallback`. `source` is `writer`, `draft` or `fallback`; `kept` is how much of the draft survived, from 0 to 1.

### Tracker screen

Every GitHub write (`issue_create`, `pr_create`, comments, replies, closing comments and body edits) and every Jira comment passes a deterministic screen first. It replaces, and leaves everything else as it was:

- the origin of a URL on `localhost`, a loopback, private, link local or tailnet address, a `*.ts.net`, `*.local`, `*.internal` or `*.lan` name, a host with no dot, this machine's host name, a host in `devices.toml` or the host of a layer's `[verify] url`, with `[private address]`; the path stays;
- the same addresses and names without a scheme (`localhost:PORT`, `127.0.0.1`, a tailnet address, `name.ts.net`, a registered host) with `[private address]`;
- `/Users/NAME` and `/home/NAME` with `~`.

Public links such as `https://github.com/o/r/pull/1` and the local tracker are not touched.

### Compare writers

`arete writers` compares candidate routes for the writer atom (the `document` step) with team preference and plain-code factual checks. It reads a private layer outside the repo, so real messages, tickets and voice samples never land in a public tree, and it changes no route, tier or config.

- `<layer>/writers/config.toml`: `raters` (whole number from 1, default 2) and `[candidates.NAME]` tables, 2 to 26, each with one or more of `tier`, `runner`, `provider`, `model`, `effort`. Two candidates may not resolve to the same runner, model and effort.
- `<layer>/writers/cases/*.json`: 15 to 20 cases, each an `arete document` brief of kind `message`, `ticket` or `doc` plus `evidence`, a list with one entry for each acceptance condition: the phrases, any one of which shows the condition was met. Voice examples and soft word targets come from the layer's `[document]` settings.

Actions:

- `arete writers sample FOLDER` writes a synthetic replay fixture (repo, layer, 15 cases, 45 answers) and prints the `[replay]` and `[[repo]]` lines to add to a scratch config.
- `arete writers run REPO [--dry-run]` sends every case to every candidate once with the same prompt and the soft length targets, with no retry, and saves the run. Labels `A`, `B`, `C` are drawn at random for each case and kept only in the private run file. `--dry-run` lists the candidates and cases and calls nothing. The calls show in `arete costs` as `writer.<candidate>.<case>` steps.
- `arete writers export RUN [--out FILE]` prints the rating view: each case's brief and its texts under their labels, with empty `naturalness`, `brevity`, `clarity`, `edits` and `seconds`. It holds the private cases, so keep it private; the labels hide the candidates, and it carries no name, route, cost or fault.
- `arete writers rate RUN FILE [--rater NAME]` imports a filled view for one rater and replaces that rater's earlier import. Empty values are accepted and count as missing.
- `arete writers report RUN [--json]` prints the result by channel and for all.

Ratings: `naturalness`, `brevity` and `clarity` from 1 to 5, `edits` from 0 (none) to 3 (rewrite), and `seconds` spent making the text sendable. The preference is the mean of the three scales. The report gives each candidate's preference with a 95% interval over cases, edits, writing seconds, words, texts over target, charged and reference cost and latency. Reference cost is `unknown` when any call has none. Agreement is the share of text pairs, rated by two raters on all three scales, that both order the same way.

Missing requirements, invented facts and damaged commands are counted separately by code, never by a model or an AI detector: a missing requirement is a dropped keep string or warning, a left-out supplied value or an acceptance condition none of whose evidence phrases appears; an invented fact is a handle, date or day word the brief lacks; a damaged command is a changed or dropped code span or line. A candidate with any failed or flawed text in a channel cannot be proposed for that channel, however short and well rated it is.

The result is `unreviewed` until every text has all five values from at least `raters` raters; an unreviewed report proposes nothing. Once reviewed, the proposal is the eligible candidate with the highest preference, with its lead over the next one and whether the interval is clear of zero. Proposals only.

## CI

After the push, the run waits for the checks on the PR. It waits up to 30 minutes, or 3 minutes when no check reports at all. The run frees its `max_parallel` slot for the wait, so another ticket can start, and takes a slot back before a CI fix step. Arete compares each failed check with the same check on the head of the base branch.

- Red on the base too: the PR section notes the base result and its date. This PR did not cause it, so nothing changes.
- Passed on the base, skipped there, or no result there: a CI fix step reads the job log and the diff, and decides whether the PR caused the failure. When the base moved after the last push, arete first merges it into the branch, since CI tested the PR merged with that base. A merge that conflicts is undone, and the step works on the branch as it is, as it does when the fetch fails. A check that the PR did not cause gets a note with the reason. For a check that the PR did cause, the step fixes the code. Then the checks and the diff limit run again, arete pushes the fix, and the wait starts again. The `ci_repair` step's `tries` (build's, default `limits.max_attempts`) caps the rounds, and the diff limit counts the whole branch.

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

A repo with parts takes no `github` of its own and needs `tracker = "jira"`. Each run gets one folder with a worktree per part at its `dir`, all on the same branch, and the checks run from that folder. Each part continues from its open PR on the branch, or from commits already pushed to the branch with no PR yet, else starts fresh from the base. A part that holds edits an earlier run kept unpushed continues from them. When a part continues and its base moved since, arete merges the base into the branch first, so the plan and the build see what CI will test; a merge that conflicts is undone, and the run works on the branch as it is. A ticket can need several runs, and each run adds commits to the PRs already open; it keeps the branch of those PRs even when the ticket's title or type changes.

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

The grader is the pipeline's verifier, started on the layer's `[verify]`. It resolves `eval_grade`, which inherits verify; its timeout is the task's. It gets the task and the steps only, never the diff or the builder's summary, and a failed result names the step that failed. A task with both `check` and `verify` needs both to pass.

`arete eval mine [REPO]` writes these tasks from merged arete PRs, newest first, 3 at a time (`--limit N`). A read only writer step reads the ticket, the PR body with its evidence, and the diff, and writes the ask and the steps without naming the fix; it skips a change with no behavior a user meets. Arete keeps a task only when the grader passes it on the merge commit and fails it on the commit before, so the task tells a working change from a missing one. Each outcome is recorded, so a PR is mined once; a crash records nothing, and the next mine tries again. A kept task is committed in a layer outside the repo, or opened as a PR on `arete/evals-<name>` for a layer in `.agents/`. `--dry-run` lists what it would mine and skip, with no model call. Only repos whose layer has `[verify]` are mined.

`arete eval REPO --prompts DIR` runs the plan, build and fix steps with the prompts in `DIR`. The grader keeps the installed prompt, so the judge never moves with what it judges.

## Experiments

One eval run per arm cannot select a model. `arete experiment run MANIFEST` pairs two or more arms on the same eval tasks, repeats each pair, and `arete experiment summary FILE` says whether the difference is real. It runs the eval runner once per trial, never writes `config.toml` or a layer, and selects no live setting: the owner reads the summary and edits the config. Keep candidate lists, rankings, tariffs and real task samples in private configuration, not in the manifest you share.

```toml
name = "build-effort"            # letters, digits, _ and -; names the results file
repo = "replay-sample"           # a single repo with eval tasks in its layer
atom = "build"                   # plan, build or check_repair; verify and eval_grade stay fixed
repeats = 3                      # whole number, 1 or more
cache_policy = "provider-default"  # optional label, recorded on every trial

[arms.current]                   # two or more; keys: tier, runner, provider, model, effort, tools, mcp, instructions, prompts, context
model = "scratch-a"
effort = "medium"
[arms.stronger]
model = "scratch-b"
effort = "high"

[tasks.word]                     # an eval task of the repo's layer
base = "4f1c2ab"                 # 7 to 40 hex digits of a commit; the eval task's own base when left out
split = "validation"             # train, validation or holdout

[accept]                         # the rule, written before any trial runs
baseline = "current"
split = "validation"             # the only split that decides; validation by default
min_pairs = 6                    # never under 6
min_difference = 0.0             # 0 to 1
confidence = 0.95                # above 0.5, under 1

[limits]                         # all four required, finite, above 0
spend_usd = 5
minutes = 60
calls = 200
tokens = 2000000
```

An unknown key is refused. Each arm resolves through the step settings for `atom`, so a bad effort or model is refused by name. A task needs a `check` or `verify`, and its base must resolve to a commit. `--dry-run` validates the manifest and prints the results file, each arm's resolved runner, model, effort, tier and timeout, the fixed grader, each task with its full base, split, grader and hidden file count, the trial count and how many the file holds already, the acceptance rule and the limits. It writes no file and no row and makes no call. A manifest that changes the design (arms, tasks, atom, repeats, acceptance rule, prompts, harness, tools or grader) gets a new digest, and an existing file refuses it: pass `--out` for a new file. The limits are not part of the design, so they can be raised to resume.

A trial is one task, arm and repeat. It is its own `runs` row of kind `experiment`, with a fresh worktree and fresh sessions, and the arm's settings reach the targeted atom. The other builder steps are frozen: they keep their configured settings for every arm, and the repair step does not inherit the arm from build. A step with no model configured runs on the baseline arm's route (never its effort), the same for every arm. The design digest covers the effective settings of the arms, the frozen steps and the grader, including its tools and instructions. Graders, hidden files and prompts are the same for every arm. Within a repeat the arms are rotated per task, so none always goes first. A trial takes a run slot the way a pipeline run does, so `arete status` counts it and `arete run` is refused while it holds the last one. It starts only when a slot is free, no run waits to take a slot back, and the poller has passed no ready ticket over; otherwise it waits and says why, and queued production work is never delayed by it. The wait counts toward `minutes`. One experiment runs per repo at a time.

The results file is `experiments/<name>.jsonl` under the state folder, or the `--out` path: one JSON object per line, appended and synced, with unparsable lines skipped on read. Each line has an `event`:

| event | fields |
|---|---|
| `manifest` | `digest`, `design` (arms with their resolved runner, model, provider, tier and effort, tasks with split, full base and an evidence hash, the grader, the acceptance rule, prompt, context and tool fingerprints). First line only |
| `session` | `at`, `limits`, `pid`; one per invocation |
| `start` | `task`, `arm`, `repeat`, `attempt`, `run`, `at` |
| `trial` | `task`, `split`, `base`, `arm`, `repeat`, `attempt`, `run`, `status`, `accepted`, `failures`, `repairs`, `route` (runner, model, provider, tier, credential, billing, auth_mode, position, requested), `effort`, `requested_effort`, `fingerprints`, `endpoint`, `cache_policy`, `calls`, `tokens_in`, `tokens_out`, `cache_read`, `cache_write`, `cost_usd`, `charged_usd`, `reference_usd`, `reference_unknown_calls`, `api_seconds`, `wall_seconds`, `wait_seconds`, `checks`, `verify`, `hidden_check`, `blocking_findings`, `tool_errors`, `schema_errors` (the last six: repo checks, grader verdict, held out check, blocking review findings, tool results with an error and answers that lack a required key; `blocking_findings` is null when no review ran) |
| `wait` | `task`, `arm`, `repeat`, `attempt`, `seconds`, `at`; time spent waiting for a run slot, written as it passes so a stop or crash keeps it |
| `alive` | `run`, `seconds`; written every 10 seconds while a trial runs, so an interrupted trial's `wall_seconds` is its last one |
| `stop` | `why`, `at` |

`status` is `done` (the trial ran to its end, accepted or not), `capped` (a limit or an unroutable call stopped it) or `interrupted` (the process died or was stopped). Only `done` trials are paired. `charged_usd` is what the account paid, zero on replay and local routes; `reference_usd` prices the tokens at the imported route price. A missing price leaves `reference_usd` null, and the call counts in `reference_unknown_calls`: it is never read as zero.

The limits count every `trial` line in the file plus the trial running, across invocations: `spend_usd` against charged cost, `calls`, `tokens` (in plus out) and `minutes` (wall plus wait). The spend and token limits follow one rule: a limit is reached when usage is at or above it. It is checked before each trial, before each launch and after every agent call, whether the call succeeded or failed. A trial whose last call lands exactly on a limit is `capped`, never `done`, is in no pair, and runs nothing after that call; this holds for a call with no streamed usage too, such as a replay answer with only `tokens_in` and `tokens_out`, or a Claude result whose final usage is above its streamed count. The calls limit is checked before each trial and each launch, so a trial may use its last allowed call and still be `done`. A call, a repo check, setup and the held out check get only the time left, and a call only the dollars left. Spend, calls and tokens come from the stored step rows, read before every route launch, so a retry on another route cannot start past a limit, and every launch counts. The time limit is one deadline: the network wait, a retry after it, each repo check and a replay answer all stop at it. A trial that ends at or past the time limit is `capped`. Slot wait counts too, including a wait that ended in a stop. A reached limit writes a `stop` line, prints the reason and exits 3. A usage limit is never waited out.

Every launched call also gets the tokens and the charged dollars left, and is stopped as soon as its streamed usage reaches either one. The stored usage then passes the limit by no more than the last streamed turn, the trial is `capped` and never paired, and the next trial is refused. A route that cannot stop itself this way is refused before launch: a codex arm, frozen step or grader is refused by the manifest and the dry run with the reason `codex reports usage only when its turn ends, so it cannot stop a call at a token or spend limit`. Each `trial` line has `targeted_step`, `launched` or `never launched`; a trial that stopped before its targeted step launched still records the arm's requested runner, model and effort. The design digest hashes the definition of every MCP server a step loads (command and arguments, or URL), never storing them, so changing a server refuses the old file. A repair that cannot continue the builder's session, because `check_repair` runs on another runner, gets the task, the saved plan, the builder's summary and the current diff to inspect, then the check failures; a repair on the same runner resumes the session.

Pi launches a second call to restate an answer that lacks its structured output, and that call cannot be gated against the calls and time limits, so a pi route is refused for any structured call (the `plan` atom, a frozen `plan` step and the grader) with that reason. A routing refusal writes a record header with `refused` set, and `targeted_step` reads `never launched` for it. Usage a call streamed before an interruption (Ctrl-C or a stop) is written to its step row before the interruption propagates; a process killed outright keeps nothing of the call in flight. Usage a call streamed before it failed or timed out is kept in its stored step row, so later trials see the true remaining budget. Limitations: Claude repeats a message's usage as it streams and arete counts each message's growth, but a message's output tokens may still be reported short until it ends, so a Claude call can pass a token cap by that unreported output. A stopped call keeps no session. Replay is never charged, so the spend cap needs a charging route. Local claude routes are plan logins, so their dollar cap applies only when routing bills the route as paid.

Running the same command again resumes: a `done` trial is not repeated, and an earlier `start` with no `trial` line (a killed process) is settled as interrupted and run again as the next attempt. A `capped` trial runs again as well. With `[replay] dir` set, a trial reads its answers from the first folder holding a `.json` answer of `<dir>/<arm>/<task>/<repeat>`, `<dir>/<arm>/<task>`, `<dir>/<arm>` and `<dir>`, so two arms can be replayed over several repeats, with a failed trial, without a paid call.

The summary reads only the file, so `summary FILE` reproduces the text a run prints at its end byte for byte; `--json` prints the same numbers as one line. Per arm it shows accepted of done trials with a Wilson interval, repairs, calls, tokens, cache, api, wall and wait seconds, and charged and reference cost over every attempt, failed and interrupted ones included, in total and per accepted task. Zero accepted prints "no accepted task" and an unknown reference "unknown". Per split and candidate it shows the pairs where both arms are done, the candidate-only and baseline-only accepts, the difference with its interval (the paired difference plus or minus a normal margin and one pair) and the exact sign test. Only `accept.split` gives a verdict. There is no winner when the pairs are under `max(6, min_pairs)`, when the interval reaches `min_difference` or below and `-min_difference` or above, or when the sign test p exceeds `1 - confidence`. Otherwise a candidate wins when the interval's lower bound is above `min_difference`, and the baseline holds when the upper bound is below `-min_difference`. Several winning candidates are each listed and none is named the winner. Other splits are shown as not deciding.

`tools`, `mcp` and `instructions` are step settings. `prompts` names a folder holding `plan.md`, `build.md` and `fix.md` for the arm's builder prompts, and `context` a commit whose harness files (instructions, skills, settings) the arm's checkout takes. An arm with either gets its own prompt or context fingerprint in the design; an arm with neither keeps the digest it always had.

### Promoting a winner

Nothing here runs until you run the command: no loop, schedule, server hook or dashboard route starts a search, and merging this feature starts nothing.

`arete experiment optimize PLAN` improves one atom's settings one variable per round, on top of the paired trials above. The plan is private configuration, like a manifest:

```toml
name = "build-search"
repo = "replay-sample"
atom = "build"
repeats = 6

[[rounds]]
vary = "model"                   # model | effort | prompt | context | tools
[rounds.candidates.stronger]     # names are unique across rounds and never `baseline`
model = "scratch-b"              # only keys of the round's variable: model takes tier, runner, provider, model, effort

[[rounds]]
vary = "effort"
[rounds.candidates.cheap]
effort = "low"

[tasks.word-train]               # each of train, validation and holdout needs at least one task
base = "4f1c2ab"
split = "train"

[accept]                         # min_pairs, min_difference, confidence, as in a manifest
[floor]                          # the declared quality floor
accept_rate = 0.8                # a candidate must be accepted at least this often
max_drop = 0.2                   # and may not trail the champion by this much
min_saving = 0.1                 # economics must be lower by this share; optional, above 0 and under 1
[limits]                         # all five required; they cover the whole search
spend_usd = 5
minutes = 60
calls = 2000
tokens = 5000000
rounds = 2
```

The first round varies `model` or `effort`, and no such round follows a `prompt`, `context` or `tools` round. The baseline is the live configuration. Each round runs the champion and every candidate on the train tasks; a candidate is proposed when it fixes at least as many train tasks as it breaks and has pairs, and its failing train trials are recorded. The champion and the proposals then run on the validation tasks, where each is decided in this order: `no promotion: insufficient evidence` (a tiny sample), `no promotion: missing costs` (charged or reference cost unknown for either arm), `no promotion: quality regression`, `no promotion: under the quality floor` (accepted rate below `accept_rate`), `promote on quality` (a clear win), then for an overlap `no promotion: noisy tie` (the interval reaches `-max_drop`, or the candidate is not cheaper) or `promote on economics`. Cheaper means charged cost, reference cost, repairs and wall seconds per accepted task are each no worse than the champion's and charged plus reference cost per accepted task is lower by `min_saving`. The best promotable candidate (highest validation rate, then lowest cost per accepted task, then plan order) becomes the champion. A noisy tie is never a win. After the rounds, a champion that is not the baseline is run against the baseline on the holdout tasks once; the holdout never proposes or selects, and a second run does not score it again. It is confirmed only with enough pairs, known costs, a rate at or above `accept_rate` and a paired difference no worse than `-max_drop`.

`spend_usd`, `minutes`, `calls` and `tokens` count every trial of every phase in the folder, and `rounds` caps the rounds; a reached limit stops the search with exit 3, with no holdout and no end line, and running it again resumes. Every trial takes a run slot like any experiment trial, so queued production work keeps priority and the wait counts toward `minutes`. `--dry-run` prints the plan digest, the baseline, each round's candidates with their resolved runner, model and effort, the tasks per split, the floor, the limits and the most trials the search can run, and writes and calls nothing. A changed plan or baseline refuses an existing folder; pass `--out` for a new one. A search with an `end` line is final: running it again, even with a higher `rounds` limit, prints its result and runs nothing, and once the holdout is scored no further round runs. A `context` arm takes the commit's harness files, but every step's settings, the fixed grader included, keep resolving against the live layer, so the design records what runs. Search trials differ from plain experiment trials in one way: the repair step (`check_repair`) follows the arm the way it follows `build` in production, so the evidence covers what apply writes; its settings hash is recorded per arm. `review_repair` and `ci_repair` follow `build` too but no trial runs them, and the dry run lists them as not measured. Before each phase the fixed steps, grader, harness, prompts, tasks and live baseline are checked against the search's start, and a change refuses the phase (and a rerun of the folder); pass `--out` for a new one. The prompt, context and tools fingerprints of the arm a round selected are stored in its `round` line; every later phase (train and validation of the next round, holdout, a resume) and the validation after a train check that arm again before any trial and refuse when its prompts, context or `[mcp]` server definitions changed, so the holdout scores the exact candidate validation selected.

The trail is a folder, `experiments/<name>/` under the state folder or `--out`: `trail.jsonl` (events `search` with the digest, design and a baseline snapshot of the config path, its hash, the raw `[steps.<atom>]` table and the resolved settings; then `round`, `holdout`, `stop`, `end`, `apply`, `rollback`), the phase results `r<n>-train.jsonl`, `r<n>-validation.jsonl` and `holdout.jsonl` (ordinary results files, readable with `experiment summary`), and `config-before-apply.toml` (mode 0600) once an apply ran.

`arete experiment promote FOLDER` is a dry run: it prints the decision, its basis and the exact diff of `config.toml`, the roles that follow the atom by inheritance and whether apply is refused: a winner that needs `prompts` or `context` is never applied in part, since the step settings alone were not what the search confirmed. `--apply` writes only the winner's step keys into the private `config.toml` table `[steps.<atom>]` through a checked, atomic replace; it refuses a search that is not promotable or has not finished, or settings for the atom that changed since the search began, and says so when the winner is already applied. The new file must load, give the atom the winner's settings and leave every other role resolving as before; the file is read back afterwards. On any failure it records `apply` as failed and leaves `config.toml` unchanged (or restores the saved bytes). `--rollback` restores the saved bytes, or, when `config.toml` changed since, puts back only the applied keys. Limits: apply targets only the private `config.toml`, never a layer; a layer `[steps]` entry that sets the same key fails the read-back; `[kinds.*.steps]` entries still win for their tickets.

`arete experiment sample FOLDER` writes a replay fixture and prints a `config.toml` for a scratch config folder: three eval tasks, answers for a baseline, a stronger model, a cheaper one that regresses on validation and a twin of the stronger one, a price file for `arete prices import`, and `plan.toml`. Running it shows a winner promoted, a cheaper quality regression, a noisy tie, and apply and rollback of one `model` line.

## Implementation benchmark

`arete benchmark run MANIFEST` compares implementation routes on tasks of different difficulty instead of reading a general leaderboard. It is a recipe on the experiment runner above: the same pairing, limits, slots and resume, with these additions. The same command also measures local endpoints (see the endpoint benchmark); it tells the two apart by the manifest: `[candidates.NAME]` tables are endpoints, `[arms.NAME]` and `[tasks.NAME]` tables are this benchmark. For this benchmark `--out` names the results file. `arete benchmark report FILE [--json]` reads only the results file, and `arete benchmark sample FOLDER` writes a replay fixture; `fixture` stays the endpoint fixture server. The report changes no route, tier or config.

The manifest is an experiment manifest plus:

- `group` on every task, `ordinary` (features and bug fixes) or `difficult` (unfamiliar multi-file changes). Each group is reported apart and has its own recommendation, and needs at least one `holdout` task.
- `reference` on every task with a `check`: the commit that holds the accepted change.
- `review = true` at the top (the default) runs the fixed reviewer on each trial; `false` skips it.
- `[accept] min_difference` is required: the quality margin, declared before any trial runs. `split` is `holdout` and no other value is accepted, so only holdout tasks decide. `atom` defaults to `build`. `repeats` and `[limits]` stay required.

The plan, repair, grader and reviewer steps are fixed for every arm; only the `atom` takes the arm's route.

A task is proven before any trial runs. Its hidden check must pass on `reference` and fail on `base`, each in a clean checkout; the result is a `proof` event in the results file, and a task already proven is not checked again. A check that fails on the reference, or passes on the base, refuses the run with the task named and no trial runs. A mined task with only `verify` steps needs no `reference`, but only when its mined row is `accepted` for this repo and the task as it is now (title, prompt, steps, base and source) equals the task the mining run validated; an edited or copied task is refused. Any other task with only `verify` is refused: add a `check` with hidden files and a `reference`.

The time spent on proof, on the layer's setup commands and on the availability check counts against `limits.minutes`. Each hidden check and setup command gets at most the time left. When the time runs out the run stops and exits 3 before any trial starts. The time is saved as `setup` events, so running the same command again continues from the time already used and prints it; raise `limits.minutes` to go on.

An arm whose route has no credential, login or required tool is `unavailable`, with routing's reason, and runs no trial. That is never counted as a quality failure: the report says `unavailable, not judged` for it. The baseline being unavailable refuses the run. `[replay] all = true` sends every call to replay, so every arm is available; leave it out to have routing judge the arms. The check is written as an `availability` event.

Each `trial` line also records `checks` (the repo checks, `pass` or `fail`), `verify` (the grader's verdict, empty with no `verify` steps), `hidden_check`, `blocking_findings` (null when no review ran), `tool_errors`, `schema_errors`, `repairs` and `cost_usd`. The report adds the total cost over every attempt and per accepted task.

A trial is accepted only when its repo checks pass, its hidden check passes, its verify steps pass and the reviewer raised no blocker; `arete experiment` keeps its own rule.

The results file is `benchmarks/<name>.jsonl` under the state folder, or the `--out` path. It holds the experiment events plus `proof`, `availability` and `setup` (`seconds`, `at`; written every 10 seconds and when the phase ends). The report shows the total as `proof and setup`.

The quality rule: on the holdout tasks of a group, a candidate wins only when its pairs against the baseline reach `min_pairs` and the lower end of the difference interval is above `min_difference` and the sign test is significant at `confidence`. The report recommends the cheapest winner per accepted task, ties to the larger difference; with no winner it keeps the baseline.

The sample runs end to end on replay with no paid call:

```
arete benchmark sample /tmp/bench      # prints the [replay] and [[repo]] lines for a scratch config
arete benchmark run /tmp/bench/benchmark.toml --dry-run
arete benchmark run /tmp/bench/benchmark.toml
```

It runs 54 replay trials (three tasks, three arms, six repeats; the fourth arm is unavailable). Expect `strong` to win in `ordinary` over a baseline that fails one task, `cheap` to hold at the baseline on cost alone with no accepted task, `difficult` to keep the baseline, and `absent` to read `unavailable, not judged`.

For real routes, keep the manifest and the tasks in your own config and layer, then bound the run: run `--dry-run` first, set all four `[limits]`, and start small (few tasks, `repeats` at the experiment minimum). A run that reaches a limit stops and exits 3; the same command resumes after you raise it. A bounded invocation, with a manifest in your config folder and placeholder arm names only:

```
# ~/.config/arete/benchmarks/routes.toml
name = "routes"
repo = "my-repo"
repeats = 6
[arms.baseline]
tier = "<tier>"
[arms.candidate]
model = "<provider/model>"
[limits]
spend_usd = 20
minutes = 120
calls = 2000
tokens = 5000000

arete benchmark run ~/.config/arete/benchmarks/routes.toml --dry-run
arete benchmark run ~/.config/arete/benchmarks/routes.toml
```

The manifest also needs your `[tasks.NAME]` and `[accept]` tables. Tool errors read 0 on replay, since a replay record holds no tool results. Candidate ids, rankings, tariffs and task data belong in the owner's manifest and layer, never in this repository.

## Bounded workers

A cheap worker can look good per call and cost more once repairs and escalations are counted. `arete benchmark run MANIFEST` also runs this recipe on the experiment runner: a trial gives the worker route the task, repairs go through the existing `fix` loop, and an escalation is that same loop resolved to a stronger route through the existing router. The command tells it from the implementation benchmark by an arm table holding `attempts`, `no_progress` or `escalation`; `report FILE` tells it by the results file. `atom` is `build` and no other value is accepted. No planner runs: the task is the whole brief.

The manifest is an experiment manifest plus:

- on every `[arms.NAME]`: the worker route as usual, `attempts` (1 to 8, launches of that route per trial, first try plus repairs) and `no_progress` (`unchanged_patch`, `same_failure` or `either`). An optional `[arms.NAME.escalation]` holds route keys and its own `attempts` (1 to 8).
- on every `[tasks.NAME]`: `kind` (`patch`, `test`, `explore` or `extract`), `reference`, and optionally `base` and `split` (default `validation`). `[accept] baseline` names the arm the others are compared with.

The four kinds differ in what their gate checks: `patch` that the change is in the code, `test` that a test passes on the right code and fails on a mutant, `explore` that the answer file names the right path, and `extract` that the evidence file holds the source text. In each the gate is the task's own `check`, proven on `reference` and `base` before any trial, together with the layer's repo checks. A failed gate's output is what the next try and the escalation route see; the hidden files are removed after each gate run, so a later attempt never finds them.

An arm hands the task to its escalation route when the worker reaches its attempt limit, when the no-progress rule fires after a failed gate, when the worker route is unavailable, or when a worker call fails. A same-runner handoff resumes the session; another runner gets the task, the summary and the diff, as any repair does. The escalation route gets its own attempts and no further handoff. An arm with an unavailable worker route and an escalation route runs, with the escalation route taking the work; without one it is `unavailable, not judged` as before.

Each `trial` line adds `attempts` (every launch, with its endpoint, tokens, cost, applied tariff, gate result, whether the patch changed and seconds, including calls that never launched), `first_pass` (the first worker attempt passed its gate), `handoff` (the reason, or empty) and `unpriced_attempts`. The report counts every failed attempt and escalation in acceptance, seconds and cost, and shows first-pass success apart from eventual success, per arm and per kind. The tariff total uses the endpoint's applicable rate, pinned by the price snapshot's observation time. A launched attempt with no rate, or only a conditional rate that is not proven for the call, makes the total read `unknown`; unknown is never compared and never counted as free.

A trial keeps its launches in `worker-launches.json` in its run folder as it goes. When a killed run is resumed, its interrupted trial line is rebuilt from that file and the trial's records, so its attempts, handoff and costs still count. A launch whose call left no result reads unpriced, and a trial line with no attempt list makes the arm's tariff `unknown`, its worker and escalation parts included.

Nothing in `config.toml`, a layer, `tries` or routing changes: the escalation exists only inside the trial.

```
arete benchmark sample /tmp/workers --workers   # prints the [replay] and [[repo]] lines for a scratch config
arete prices import /tmp/workers/prices.json    # without it every tariff reads unknown
arete benchmark run /tmp/workers/workers.toml --dry-run
arete benchmark run /tmp/workers/workers.toml
arete benchmark report FILE [--json]
```

The sample replays an immediate success, a failed gate, an unchanged patch, an exhausted attempt limit and an unavailable worker, at placeholder tariffs from `prices.json`; `prices-conditional.json` makes the cheap route's only rate conditional. Real routes, tariffs and tasks stay in your private config and layer, never in this repository.

## Verifier suites

A verifier has to catch a regression as well as accept a working UI. `arete verifiers` runs candidate verifiers (arms) over known-good states, seeded regressions and real historical failures, each with numbered requirements and an expected label, then reports how often each arm got them wrong. It uses the pipeline's own verifier call and `[verify]` browser setup, and changes no prompt.

```toml
name = "counter"                 # letters, digits, _ and -
repo = "toy"                     # optional: a configured repo, for its layer, environment and [verify]
repeats = 2                      # trials per task and arm, default 1
max_false_pass_rate = 0.0        # required, 0 to 1: the most false passes you accept

[limits]                         # required, all above 0
minutes = 15                     # whole suite
spend_usd = 1.0                  # whole suite
timeout_minutes = 5              # one trial

[verify]                         # start, url, ready, ready_seconds, notes, allow_bash, preflight_seconds, [verify.preflight]
start = "python3 -m http.server {port} --bind 127.0.0.1"
url = "http://127.0.0.1:{port}"

[arms.careful]                   # tier, runner, provider, model, effort
model = "replay:scratch-a"

[tasks.reset-missing]
kind = "seeded_regression"       # known_good (expect pass), seeded_regression or historical_failure (expect fail)
expect = "fail"
title = "Counter page"
prompt = "The page is a counter with Add one and Reset."
requirements = ["The page shows Count: 0", "Add one makes it 1", "Reset sets it back to 0"]
fails = [3]                      # requirements that must fail; none for a pass
state = "ui/missing"             # a folder of plain files beside the manifest, or base = "4f1c2ab" (a commit of repo)
source = "https://github.com/acme/toy/pull/110"   # optional, recorded only
```

Unknown keys are refused. A `.git` folder or file at any depth in a state folder is refused when the suite loads: a state is plain files committed once in a new repository with no earlier history. `[verify]` is optional when `repo` is set and its layer has one; `publish_evidence` is read from that layer and is false otherwise.

**What a candidate sees.** The task title, the task prompt and the numbered requirements, in the pipeline's grader prompt. It never sees the task or arm name, the kind, the expected label, the failing requirement numbers, the source, a builder's summary or a diff, and none of them is written into its workspace. Every trial is a fresh observer: a new session, a fresh copy of the state (committed in a new git repo, or a checkout of `base` with the layer's `setup` run), its own app on a free port, and its own isolated browser and evidence folder. Trials number from 1 in the order task, repeat, arm; trial `n` is the step `verify-n`, so a replay answer is `verify-n.json`.

**Preflight.** Per arm, before any trial, arete checks that the arm can run here: `[verify.preflight]` commands (once, for every arm), whether its route can load the Playwright MCP server (Pi cannot, it runs with `--no-mcp`), and, for arms that really call a model, `npx` for the browser. An arm that cannot run is `unavailable` with the reason. It runs no trial and gets no quality numbers, so a missing browser never counts as a failed verdict. A trial whose verdict is `blocked`, whose call failed, whose app did not start or whose setup failed is `blocked`, never a false pass or false failure.

**Retained trials.** When a trial's scratch attempt is still unresolved (a producer is alive, the agent call raised or cleanup could not finish), arete keeps the trial worktree and `runs/<run>/trial-n/<run>/` in place. The trial is recorded as blocked whatever the verdict, with both paths in `retained`, and `evidence` points inside the kept folder. The remaining trials run, and `arete poll` finalizes the attempt. After poll prints `preserved and finalized verification scratch`, the two folders are no longer needed and the owner removes them.

**The report**, per arm:

- `judged`, `blocked`, `not run`: trials that gave a pass or fail, trials that could not, and planned trials never started (a limit stopped the suite).
- `false passes X of Y`: of the judged trials that expect fail, how many got `pass`. The false-pass rate is X/Y.
- `false failures X of Y`: of the judged known-good trials, how many got `fail`.
- `coverage X of Y requirements`: in judged trials, requirements named by at least one check. A check names requirement `k` when it starts `Step k` or `Requirement k`. `labels right` counts requirements whose seen result (`fail` if a naming check failed, else `pass`) equals the expected one.
- `accepted`: judged trials whose verdict equals the label. `cost per accepted verification` is all the arm's cost, blocked trials included, over `accepted`.
- seconds, cost and paid dollars, summed over the arm's trials.

The recommendation is the first arm that is available, has judged every expect-fail task at least once and has a false-pass rate at or below `max_false_pass_rate`, ordered by false-pass rate, false-failure rate, cost per accepted verification, mean seconds and name. With no such arm there is no recommendation. Arms that also qualify are listed.

`arete verifiers run SUITE` appends one JSON line at a time to `runs/<run>/results.jsonl`: `suite`, a `preflight` per arm, a `trial` per trial and a `stop` when the minutes or the spend limit ends it (exit 3). It prints the path and the report; `arete verifiers report FILE` prints the same report from the file, and `--json` prints it as one JSON line. Evidence for each trial is kept under `runs/<run>/<task>/<arm>/<repeat>/` (`evidence/`, `verify-n.json`, `verify-n.md`, the server log and `run.log`), never pushed, whatever the layer's `publish_evidence` says. `run --dry-run` lists the arms with their availability, the tasks, the requirement labels and the numbered trials, without a call or a write.

**Try it without a model.** `arete verifiers sample FOLDER` writes a tiny counter UI with one known-good state and two seeded regressions (no Reset button, Add one adds 2), `suite.toml`, `unavailable.toml` (a failing preflight probe) and 18 replay answers. Use a scratch `ARETE_CONFIG_DIR` and `ARETE_STATE_DIR`, then:

```
ARETE_REPLAY_DIR=FOLDER/replay arete verifiers run FOLDER/suite.toml
```

The scripted arms show a verifier that is right every time, one that passes a regression, one with a blocked trial and a failed call, and one that is unavailable.

**Against a real browser.** Copy the suite to private config with real arms and run it, bounded by its `[limits]`:

```
arete verifiers run ~/.config/arete/verifiers/counter.toml
```

Each arm sets `model` (or `tier`), such as `[arms.candidate] model = "claude-sonnet-5-5"`. The run needs node (`npx` starts the Playwright MCP server) and Chrome. An arm without them is `unavailable`, not rated. Calls are real and paid, and the run stops when `minutes` or `spend_usd` is reached. Candidate lists, rankings, route tariffs and real task samples belong in private config or a layer, not in this repo.

## Retrieval experiments

`arete retrieval run REPO SUITE` measures whether embeddings or reranking improve the context an agent gets, before any retrieval service exists. It is offline: it installs no service and changes no retrieval default. Every arm searches the same corpus revision and gets the same token budget. For each arm and query it reports recall, whether the downstream answer was accepted, tokens supplied, latency and total retrieval plus generation cost.

A suite is `retrieval/<suite>.toml` in the repo's layer, so queries, labels, adapter routes and tariffs stay in a private layer:

```toml
budget_tokens = 400        # required, the same for every arm
top_k = 20                 # candidates ranked before the budget cut
spend_usd = 1.0            # required with [generate]

[corpus]
revision = "4f1c2ab"       # any git rev, HEAD when left out; .agents/ is never read
include = ["**"]
exclude = []
passage_lines = 40
max_files = 2000           # a bigger corpus is refused

[generate]                 # without it downstream is reported as not run
model = "..."

[adapters.rerank]
kind = "rerank"            # embedding | rerank
replay = "rerank.json"     # or command = "..."; a path is relative to the suite's folder
route = "..."              # route, model and version are stored as written
model = "..."
version = "..."

[arms.hybrid-rerank]
search = "hybrid"          # exact | hybrid
embedding = "embed"        # hybrid only, optional
rerank = "rerank"          # hybrid only, optional

[queries.cap]
query = "nightly export row cap"
relevant = ["docs/limits.md", "src/a.py:10-30"]   # files or path:start-end lines
accept = ["250"]           # strings the answer must contain, any case
check = "grep -q 250"      # the answer on stdin, exit 0 accepts
split = "holdout"          # train | validation | holdout
```

The suite needs an `exact` arm and a `hybrid` arm with no adapters, the baseline. `--split NAME` runs one split, so a held out split can be scored apart from the ones used to tune. Exact search matches code names as case sensitive substrings and otherwise whole words; hybrid fuses exact, BM25 and, with an embedding adapter, cosine ranks by reciprocal rank; a rerank adapter then reorders the candidates. Passages fill the budget in ranked order and one that does not fit is skipped.

An adapter is a replay file or a command. An embedding reply is `{"vectors": {"<passage id>": [..], "query:<name>": [..]}, "corpus"?, "cost_usd"?, "seconds"?}`, where passage ids are `path:start-end`; `corpus` is the corpus fingerprint it was made for. A rerank reply is `{"queries": {"<name>": {"scores": {"<passage id>": 0.9}, "cost_usd"?, "seconds"?}}}`. A command gets the request as JSON on stdin (`{"kind": "embedding", "texts": [{"id", "text"}]}` or `{"kind": "rerank", "query_id", "query", "passages": [{"id", "text"}]}`) and prints the same shapes, a rerank one as the per query object.

Nothing is guessed. An adapter that is missing, unreadable, made for another corpus, timed out, exited non-zero or has no entry for a query makes that result `unavailable` with the reason; it never falls back to the baseline and never scores zero. Embedding vectors must all have one nonzero length, or the adapter is unavailable for every query of each arm that uses it. A query without `relevant` has no recall, and one without `accept` or `check` has no acceptance; both are listed under `Missing labels:`. An arm's retrieval cost counts its unavailable results too. A rejected reply keeps its `cost_usd`. The cost is `unknown` after a failed, timed out or non-JSON command, a reply without `cost_usd`, or a missing or unreadable replay file, and then retrieval and total read `unknown`. Recall, acceptance, tokens and latency count available results only. Generation stops at `spend_usd` of charged spend and the rest read `not run: the spend limit ... was reached`. Failed answers are listed under `Downstream failures:`; the exit code is still 0.

Limits: a passage with no vector is left out of the cosine ranking without a notice. Tokens are arete's byte estimate, not a tokenizer's count. Each arm counts its embedding index cost once, and every split row includes it. A replayed adapter's latency is the file's `seconds` plus local time. Each arm and query is answered once, so there is no variance estimate. Nothing here was run against a live embedding or rerank service.

Answers are generated with the `retrieval_answer` prompt on `[generate]`'s model. With `[replay] all = true` they come from `retrieval-<arm>-<query>.json` in the replay folder. The run is an `eval` run, so charged calls show in `arete costs`. The results file `retrieval.json` in the run's folder (and `--out FILE`) stores the corpus commit and fingerprint, the suite and prompt fingerprints, each adapter's route, model, version and fingerprint, and one result per arm and query. `--json` prints it, `--dry-run` checks the suite and corpus and calls nothing. `arete retrieval sample FOLDER` writes a tiny code and document corpus with distractors, exact name queries and replayed adapters, and prints the config lines to try it in a scratch `ARETE_CONFIG_DIR`.

## Prompt changes

Arete's own step prompts, `arete/prompts/*.md`, are harness too. When the miner sees the same problem in how arete's steps do their job across repos, it proposes a change to the plan, build, fix, verify, review or other step prompts in the `arete_repo`. The grader, the eval writer, the miner and apply prompts stay human edited. Apply refuses a change that adds or drops a `$placeholder`, or adds or deletes a prompt file.

Arete then runs each eval suite twice on the same day, once with main's prompts and once with the PR's, and comments on the PR with the pass rate of each, the tasks that changed, what was paid and run ids, and the changed prompts the evals do not exercise. Private repos show as "private repo N", never by name. These runs do not count toward the nightly baseline. The pipeline reads its prompts from this checkout, so pull it after you merge one of these PRs.

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

With `[research] planner = true` in the engine config, a run is staged. A fresh read only planner, the `research_plan` step, first returns a plan: 3 to 7 search tasks (each a goal, 1 to 3 general queries of at most 120 characters and a cap of 1 to 8 sources), the questions the report must answer, the criteria a source must meet and the claims the decision rests on, which the report must check. Arete validates the plan; a refused plan is asked for again, with the faults, up to `tries` times, and then fails the run with no search. A plan that says the family reports already cover the ticket, naming them, gives the report call no web tools. The planner and the report call share the $5 and 40 minutes, with $4 and 30 minutes kept for the report. Elapsed time, including usage limit waits, consumes that deadline. Staged native Claude and Codex calls require subscription billing; native API key calls receive no paid allowance because those runners cannot enforce it. Metered pi calls retain their budget monitor. An observed overspend or unknown charge fails the run and permits no further calls; incurred costs remain recorded, with only the known subtotal available when a charge is unknown. The calls are recorded as `research REF plan` and `research REF report` (replay files `research-REF-plan.json`, `research-REF-plan2.json` and `research-REF-report.json`). Screening, reuse of family reports, filing and reruns work as before, and the default, with the switch off, is the one call.

With `[research] search = true` too, the plan's tasks run as search workers between the planner and the report call. Up to `search_workers` run at once, in plan order, each a fresh `research_search` call with `WebSearch` and `WebFetch` and nothing else. The stage may use `search_share` of the money and time left after planning, and never the $1 and 10 minutes kept for the report; each worker gets an equal part of what the cap has left, and a worker that overspends or whose paid cost is unknown stops the run like an overspend in the planner. When the cap is reached, tasks not yet started are cancelled and the running workers finish inside their own allowance and deadline. Each worker call gets a timeout 60 seconds short of the worker's deadline, to leave room for routing and for the runner to stop the call; a call that times out fails its task and the other tasks go on, and only a call that returns after the deadline stops the run; the evidence records each worker's and the total usage. A runner with no web tools (pi and codex have none) is reported unavailable by `arete steps` and the run stops with no search, since arete grants a worker no tool in its place. A worker sees only its task's goal, queries, source limit and the plan's source criteria, never the ticket or the repos, and is told to search with general terms. Each source carries the question, URL, title, the publication and event dates the page states, the retrieval time, a passage copied from the page and its uncertainty; a date the page does not state stays empty and arete never fills one in, and a worker answer that is not a date stays only in its original answer. Sources keep their IDs (`S1`, `S2`, and so on, by canonical URL) across overlapping tasks and reruns. JEV screens every finding together with its source's URL, and each worker's notes, for text that steers an agent; a flagged note, or one JEV could not screen, is withheld from the report call, and with JEV off the notes go on unscreened and the run says so. The report call then has read tools only. Arete keeps the evidence at `~/.local/share/arete/research/REF.evidence.json`, and `arete research REF --evidence` prints it. The worker calls are recorded as `research REF task N search` (replay files `research-REF-task-N-search.json`).

With `[research] synthesis = true` too (it needs `search = true`), a collator, the `research_synthesis` step, writes the report in place of the report call. It has read tools only, no web tools and no MCP servers, its own route and model, and receives the plan, the passed-on findings and notes with their source IDs, and the gaps, as data; instructions inside them are not instructions to it. It answers in structured form: a lead, conclusions, inferences, contradictions, gaps and search requests. Arete refuses an answer, with the faults, and asks again up to `tries` times per round, when a conclusion has no source, any `[S<n>]` or source list names an ID it was not given, a contradiction names fewer than two sources, a critical inference has no source, or an inference has no basis. Each text the collator writes that reaches the report (the summary, the lead, each claim, basis, issue, gap question and reason, and search goal) must also be plain text, and the collator is told so: with each `[S<n>]` it was given set aside, it may hold no `[`, `]`, `<`, `&` or backslash, no control character other than a line break or tab, and no `://`, `www.`, `@`, or link scheme (`http`, `https`, `ftp`, `mailto`, `xmpp`, `file`, `data`, `javascript` or `vbscript`, then a colon right before a letter or slash), in any case; other words before a colon, such as `read:org`, pass. The summary goes as written into the pull request and the filed report, so it cites no ID at all. Arete then writes the report: the lead, then `Supported conclusions`, `Inference`, `Unresolved contradictions`, `Gaps` and `Sources`, each always present (`None.` when empty), every source ID written as an inline link to its URL and decision-critical entries marked. Source URLs are rebuilt from their parts without user details, every character other than letters, digits and `-.~!$&+,;=:@/?%` percent encoded and each `&` written as `&amp;`, so each stays one link to the URL as supplied, in a table too, and no character reference in the URL changes its host. Source titles have all punctuation escaped, or are written as code when they hold `://`, `www.`, `@` or such a link scheme, so a title never becomes a link. Search requests, at most 3 tasks, run as extra rounds through the same workers, the same source IDs and the same shared budget, and at most two rounds run; the collator is then called again with the merged evidence. A request made after the second round, or when the budget leaves no room, becomes an explicit gap that says why, as does one whose search returns no usable source. If a later round gives no valid answer, the last valid synthesis stands with a gap noting the new evidence was not synthesized; with no valid answer in the first round the run fails. The calls are recorded as `research REF synthesis` and `research REF gap N synthesis` (replay files `research-REF-synthesis.json`, `research-REF-synthesis2.json` and `research-REF-gap-N-synthesis.json`). `arete research REF --evidence` also shows gap-round tasks, each round's outcome and the collator's rounds, refusals, requests and cited IDs. Storage, summary, filing and reruns take the report as they take any other.

With `[research] audit = true` too (it needs `synthesis = true`), a fresh read-only auditor, the `research_audit` step, checks the collator's last valid synthesis before the report is written. It sees the claims (every conclusion, then every inference) and the passed-on sources with their URLs, dates and retained passages, and never the ticket, the repositories or the collator's lead. Claims go in batches of 5, decision-critical ones first, one call per batch. Each claim gets one verdict: `supported`, `contradicted`, `inference` or `unverifiable`, with the sources, a quote, a note and, where the quote supports a narrower claim, a correction. Arete refuses an answer, and asks again up to `tries` times, when a claim is missing or repeated, a verdict, source or note is missing, or a quote is not word for word in a retained passage of its sources (at least 20 characters, or the whole passage). A claim may be checked against a fresh fetch of its source instead: the auditor gets `WebFetch` limited, through `WebFetch(domain:HOST)` rules, to the public hosts of the sources its batch cites, lists at most 3 sources a call as fetched, and has no `WebSearch`; a runner without `WebFetch` checks the retained passages only. A supported claim stays; a claim with a correction is rewritten to it and marked; an `inference` verdict moves a conclusion to `Inference`; a contradicted or unverifiable claim without a correction becomes a gap that names the verdict and the auditor's note. Sources the auditor finds in disagreement join `Unresolved contradictions`. When any claim changed, the collator is called once more (`research_synthesis` settings, no web tools) to rewrite the lead and summary; if that is not possible, or the synthesis held no claims to audit, the lead and summary are replaced by a note (`**Audit note:**` in the lead) saying they were withheld because the audit did not cover them. The report gains an `Audit` section before `Sources`, with a line per claim, and the summary gains one sentence starting `Audit complete:`, `Audit incomplete:` or `Audit failed:`. The audit is `complete` only when every claim was checked; it is `incomplete` when the budget or a usage limit left claims unchecked, and `failed` when a batch got no valid answer. An incomplete or failed audit still publishes, with every unaudited claim marked `Not audited`; it never reads as a pass. The audit draws from the same $5 and 40 minute cap as the rest of the run; planning, search and gap rounds leave $0.50 and 5 minutes for it, and a call that cannot fit is not made. JEV screens each finding of the auditor as it screens search notes, and a flagged one leaves its claim not audited. The calls are recorded as `research REF batch N audit` and `research REF revision` (replay files `research-REF-batch-N-audit.json`, `research-REF-batch-N-audit2.json` and `research-REF-revision.json`). `arete research REF --evidence` shows each batch's tries, refusals and fetch hosts, and each claim's verdict, outcome, sources with their dates, quote, note and correction. A filed report is still reviewed by a person: its pull request never automerges.

When `alert_repo` is set and private, the board is also one issue there, "[arete] triage", labeled `arete-triage`, edited only when the board changes. A public or unreadable `alert_repo` gets nothing, and the output says why. Triage writes nothing to a ticket on its own except the two labels of an auto approval, and your answers reach a ticket only when you send it.

## Dashboard

`arete serve` serves one page from this machine to its owner over Tailscale, so it opens from a phone or another computer on the tailnet; `arete dashboard` serves it alone.
It listens only on the machine's tailnet addresses.
A request from any other tailnet login gets 403, and a change also needs the `X-Arete` header that the page sends.

A sidebar groups the views by job: Work (Live, Runs, Sessions, Loops, Triage), Watch (Outcomes, Health) and Settings (Settings, Usage), then Find and Console.
It marks the view you are on and shows what waits: Live counts what needs you, Runs what is running, Triage the tickets with questions, and Health shows its state when it is not OK.
Below 1040 px the sidebar wraps into a block above the page.
Every view, filter and open item has a URL, such as `#runs?status=failed`, `#triage?group=with-questions`, `#run/ID`, `#session/ID`, `#loop/NAME` and `#ticket/REF`, so reload, Back and Forward put you where you were.
An open page notices when the dashboard runs new code and reloads itself where you were, unless you have text not yet sent, settings not yet saved, Find or the key list open, text selected or an action still going.
Then the status line says what the new version waits for.
Close or Escape on an open item goes back to where you opened it, with focus on its row.
Ctrl or Cmd K, or `/`, opens Find, which goes to any view, run, loop, session, ticket or action.
`g` and a letter goes to a view, `c` opens or closes the console, and `?` lists the keys.

- Live: Running now, with each run's start, last step and time, and Stop; then Needs you, where each row names its repo: the pushes waiting for approval with Approve and Reject, each led by a plain summary that the `explain` model writes about a minute after it arrives, then the reason it was held and the reviewer's verdict, a failed delivery with Try again, the ready tickets one press sends to the pipeline, the skipped tickets with Run anyway, the console's drafted issue, and the harness fixes and memory facts the miner leaves for you to make by hand, each with Mark done.
  None of these buttons asks you to confirm.
  Below them are the tickets waiting on review comments.
- Runs: every run, the running ones first and then the newest, filtered by status, kind, repo and loop, with Show 25 more.
  A run's page has its facts, its log, Stop, its pull request and a message box.
- Sessions: the sessions, newest first.
  New session starts one on a device, model, runner, tool set, repo and budget, and a session's page follows its feed and takes your messages.
- Loops: every loop with its switch, kind, trigger, last run, next fire and what it paid in the last 24 hours, and a broken loop with what is wrong.
  A loop's page shows its facts, runs, findings, sources, memory, digests and metrics.
  Start runs it now and follows its live feed, where Stop ends the run.
  Try runs it the way `arete loop try NAME` does, so its feed shows what each emit would do and sends none.
  New loop drafts a loop from a sentence the way `arete loop new` does, with an optional name, repo and model, and its feed follows the draft.
  The approvals a loop waits for show on Live.
- Triage: the triage board, one group at a time, behind buttons that show each group's count.
  A ticket's page shows its questions, assumptions, plan, research report and pull requests.
  Answer a ticket's questions there and send it to the pipeline, which starts a poll.
  Refresh triage now starts a refresh.
  A research ticket offers Research it now, Research it again or Research it anyway, through the research loop; an offer that skips JEV's screen says so beside its button.
- Outcomes: for the last 30 days, the pipeline runs that shipped (a pull request, or a branch the adw flow pushed) and failed each day, the decisions that went without you against the ones you made, what you paid by day, model, runner and loop, the runs by repo, and the eval pass rate by repo.
- Health: the open alerts and failures first, then serve's beat with Install serve or Restart serve when serve is not running, each device with its last probe, tools and Probe, the heartbeat checks with Run the checks now, the probes, and the loops that report on their own.
- Settings: `max_parallel`, `max_parallel_per_repo` and the model for each role in `[models]`.
  A save changes only the lines that changed in `config.toml`, keeps the file's mode, and refuses a value arete cannot use.
  An empty model removes its line, so the role goes back to its default.
- Usage: the views you open, how you get to them, and the buttons and keys you press, on desktop and phone, over 7, 30 or 90 days.
  The page sends view, way and button names with counts and times only, never an id, typed text or page content, and the dashboard drops rows older than 90 days.

The console is a side panel that opens from any view with its button or `c` and stays open from view to view; on a phone it fills the screen.
It talks to a console agent on the `chat` model.
Every chat is kept in `arete.db`, and the Chat picker in the panel lists them newest first.
New chat starts one with no project, which works in arete's folder, or on one repo from the config, which works in that repo's folder and gets its layer's checks and expertise from arete's side.
The agent reads arete's code, config, state and logs, and its project's code when it has one, runs `arete status`, `arete events`, `git log` and `git show`, and reads arete's GitHub issues.
It never edits a file.
Ask it for a change to arete and it drafts an issue.
The draft shows in the panel and on Live, and nothing reaches GitHub until you press File this issue (Drop discards it).
Each call to the model stops at $2, and a reply on a paid runner shows what it cost.
Each turn is a fresh call to the model, built from the chat's own saved messages, so a chat can change its model or runner between messages.
The whole history is sent while it fits `[chat] context_tokens`; past that, arete sends a saved summary of the older messages and the recent ones word for word.
`[chat] summary_model` writes the summary, and its cost counts in the reply it was written for.
The budget counts the saved messages and the saved summary only.
The new message is always sent whole after them and never starts a summary by itself.
The saved summary is held to a quarter of the budget.
A saved message longer than the budget is summarized in pieces.
If the summary cannot be written, that turn leaves the older messages out and the next turn tries again.
The chat's own tool calls and their results are saved as short `Tool` messages, so a later turn knows what the chat already did.
Each chat waits on its own answer, so several chats can wait at once.
On the first start of this version, the old `chat.json` moves into one chat and stays as `chat.json.moved`.

Money counts only what you pay, on the dashboard and everywhere else arete shows a sum: the CLI, a run's end line in its feed, PR bodies, research reports and eval results.
Explicit subscription calls contribute no paid spend and retain all their tokens and reported cost. API-key calls contribute their cost even on a runner that also supports subscriptions. `arete costs` shows paid spend; `arete costs --by model` and other groupings show raw call cost and tokens, including subscription usage. Run details split the same model into subscription and metered rows when both occur, independent of call order. A zero-cost metered call says nothing paid without claiming subscription use. Chat stores each new reply's paid amount in `paid_usd` and its reported cost in `cost`, retaining full precision until display. Paid amounts show independently of the current login. Historical messages retain their old reported `cost` and a NULL `paid_usd`; the console labels those amounts as reported with payment unknown, rather than inferring billing from today's login. Chat paid totals sum only known paid amounts, while the stored `spent` total continues to track reported cost. Histories remain readable when a project leaves the configuration.

### API

These routes pass the same caller check as the page, and the POST needs the `X-Arete` header. A bad request gets 400, an unknown task or run 404, a ticket whose run is going on or no free slot 409, and a tracker that fails 502.

`POST /api/tasks` takes one of two JSON bodies:

- `{"repo": "web", "prompt": "...", "title": "...", "kind": "feat"}` adds a local task and starts it. `title` defaults to the prompt's first line that is not a `Waits on` line, cut to 80 characters, and `kind` is `feat` or `fix`, `feat` by default.
- `{"repo": "web", "ticket": "123"}` starts a tracker issue, a Jira key or a `t-` task that exists, past its waits, as `arete run --force` does. With `"force": false`, its `Waits on` line holds it and the run ends `skipped`, as `arete run` without `--force` does.

It answers `{"task", "repo", "run", "state", "why"}`. `state` is `running` with the run's id, or `queued` with an empty `run` when no slot is free (`max_parallel` or `max_parallel_per_repo`) or the prompt's `Waits on` line still holds; `why` says which. In a repo the poller does not pick, `why` names the task and the `ticket` body that starts it. A queued task in a `pipeline = true` repo starts at a later poll. A ticket whose run is going on answers that run's id with `already running` and never starts twice.

`GET /api/tasks/ID` returns the task: a local task's row with its `comments`, or a tracker item's `id`, `ref`, `title`, `url` and `state`, with `?repo=NAME` when no run names it yet. Both add `runs` (`id`, `status`, `started`, `finished`, `prs`, `cost_usd`, and `report`, the run's [`report` line](#run-lines), or `{}` while it has none), newest first, and `children`, the local tasks whose parent it is.

`GET /api/runs/RUN_ID/events` returns the run's [lines](#run-lines) as `application/x-ndjson`. `?from=SEQ` starts after that `seq`, and `?follow=1` keeps the response open and sends each new line until the `report` line, or until the run has stopped or its process has died and no line comes.

These routes steer a ticket's run. The first four answer `{"task", "repo", "run", "state", "why"}` as `POST /api/tasks` does, with the run they started:

- `POST /api/runs/RUN_ID/answer` with `{"text": "..."}` answers the question of a run that ended `needs-info`. It stores the answer on the ticket, as the owner's reply on a local task, a comment on a GitHub issue, which then loses its `needs-info` label, or a comment on a Jira ticket, and keeps it in the run's folder as `answer.md`. Then it starts the ticket's next run, whose `answer` line carries the `text`. A run of the ticket going on, or no free slot, refuses with 409 before the answer is stored. A label GitHub would not remove is named in `why`, and the run starts anyway.
- `GET /api/asks` lists the open asks, oldest first, each with `id`, `repo`, `ticket`, `run`, `step`, `asked`, `why`, `tried` and `questions`. Asks of a run that is not the ticket's latest are left out.
- `POST /api/asks/ASK_ID/answer` answers a parked ask with `{"answers": [...]}`, one entry per question (`{"choice": "label" or number or list"}`, `{"text": "..."}`, `{"done": true}` or `{"declined": true}`; `null` takes the first option of a choice when `"recommended": true` is set), with `{"recommended": true}` alone for an ask of choices only, or with `{"stop": true}`. A secret question takes only `{"declined": true}`: any other entry is refused, and nothing is stored or logged. An answer, once the run's last open ask has one, continues the run and prints the new run; 404 for no such ask, 400 for a closed ask, a run that is not the ticket's latest, or bad input, 409 while a run of the ticket is going.
- `POST /api/runs/RUN_ID/resume` also continues a `needs-info` run whose asks are all answered but whose continuation did not start (a failed launch after the answers were stored); asks that expire are retired together with any answers not yet used, so a later reply cannot continue on partial answers.
- `POST /api/runs/RUN_ID/resume` resumes a run that ended `interrupted`, `stopped` or `failed`, with the script in `adws/` it ran and `--continue`, so with no step it takes every step the old run finished, failures it moved past included, and goes on from where it stopped. A run that ended `failed` runs the step whose failure ended it again, with every step after it; when the base moved since, it prepares again, merges the new base into the edits the failed run kept and takes that run's plan and build as they were. Commits of the chain that are not on origin and that are not those kept edits stop it from preparing again: such a run takes the stored outcomes it can, and refuses before any model runs when it would have to prepare again. Kept commits of runs of one step alone that continued the chain count as the chain's, so the resume takes the stored steps that still hold for their edits and runs every later one again on them. `{"step": "verify"}` runs that step and every step after it again instead. A failed `prepare` is always run again, never taken from the stored outcomes; the steps after it are then taken from the newest run in the chain that passed `prepare`, and a step whose work folder that `prepare` made again with other content runs again. A run still marked running whose process is gone is settled first, as the poll would. A run of the classic path, a run of one step alone, and a step the run never started are refused.
- `POST /api/runs/RUN_ID/repair` repairs a held PR from the saved internal review of the run that left it. It starts a new run with `adws/repair.py` on the run's own PR branch and plan, with no plan step, a work folder made again from the PR's head when it was removed, and the configured build, verify and review settings; the run's `repair` line names the source and its first step's data the review evidence, and the findings are never posted as a GitHub comment or stored as owner feedback. It refuses a run that is not the ticket's latest, a run whose last review approved or saved no findings, a closed or merged PR, a PR branch holding no commit of that run (rewritten), and a run still going. A new commit on the branch on top of the run's is kept, and checks, verify and review run again on it. A second request while a repair of that run exists answers that run, so a retry after an uncertain launch starts nothing. The repair run follows the normal slot, lock and stop rules; one that fails or is stopped continues with `resume`. An approved repair leaves the PR a draft for a person to mark ready, and the merge rules are unchanged.
- `POST /api/runs/RUN_ID/steps/STEP` runs one step of a run that has ended alone, as `adws/step.py` does, and `{"set": {"model": "opus"}}` overrides that step's settings. It refuses a run that did not come from a script, a step the run never started, settings for a step that calls no agent or that `[steps]` would not take, and a run whose work folder is gone when its branch is not on origin; a gone folder whose branch is on origin is made again from it.
- `POST /api/runs/RUN_ID/stop` stops a running run as before, and the run ends with a `stopped` report.

Each refuses a run in the wrong state and names the state. `answer` and `resume` also refuse a run that is not the ticket's latest and name the run that came after it; runs of one step alone do not count. `steps` has no such rule.

```sh
curl -H 'X-Arete: 1' -d '{"repo": "web", "prompt": "Add a dark mode toggle."}' http://HOST:PORT/api/tasks
curl 'http://HOST:PORT/api/runs/RUN_ID/events?follow=1'
curl -H 'X-Arete: 1' -d '{"text": "Put the toggle in the header."}' http://HOST:PORT/api/runs/RUN_ID/answer
curl -H 'X-Arete: 1' -d '{"step": "verify"}' http://HOST:PORT/api/runs/RUN_ID/resume
```

### From the CLI

Each of these commands makes one call to the server and prints JSON and nothing else. All but `task show` print one JSON object per line; `task show` prints the task as indented JSON, and as one line with `--json`:

| command | route |
|---|---|
| `arete task start --repo NAME [--kind feat\|fix] [--title T] "PROMPT"` | `POST /api/tasks` with a prompt |
| `arete task run REPO TICKET` | `POST /api/tasks` with a ticket |
| `arete task show ID [--repo NAME] [--json]` | `GET /api/tasks/ID` |
| `arete wait RUN_ID` | `GET /api/runs/RUN_ID/events?follow=1`, printing only the `step_end`, `question`, `error` and `report` lines; it stops printing after a `question` line and returns at the `report` line |
| `arete lines RUN_ID [--step STEP] [--follow]` | `GET /api/runs/RUN_ID/events` |
| `arete answer RUN_ID "TEXT"` | `POST /api/runs/RUN_ID/answer`; for a parked ask whose questions are not secrets, it answers every question with the text |
| `arete answer RUN_ID [--ask ID] [--json BODY]` | `GET /api/asks`, then `POST /api/asks/ASK_ID/answer` for the run's oldest open ask; with no `--json` it prompts per question on stderr |
| `arete resume RUN_ID [--step STEP]` | `POST /api/runs/RUN_ID/resume` |
| `arete repair RUN_ID` | `POST /api/runs/RUN_ID/repair` |
| `arete step RUN_ID STEP [key=value ...]` | `POST /api/runs/RUN_ID/steps/STEP` |
| `arete stop RUN_ID` | `POST /api/runs/RUN_ID/stop` |
| `arete status --json [RUN_ID]` | `GET /api/live` for one line per live run; with a run id, `GET /api/runs/RUN_ID/events` for one line per step with `"event": "step"` (`state` is the step's result, or `running`), then the report |

They call only a server that works on their own state. Once it listens, `arete serve` or `arete dashboard` writes its addresses, port, process id, state folder and config file to `server.json` in the state folder. The commands call that server while its process is alive, it works on the same state folder and config file as they do, and its address answers within a second; when it does not answer, a command says so and names `arete serve` and `ARETE_SERVER`. `ARETE_SERVER=http://HOST:PORT` names a server and skips that check. So with a scratch `ARETE_STATE_DIR` or `ARETE_CONFIG_DIR` they never reach the real server: they call a server started on the scratch state, or none.

A failed command prints one line with `error` and no `event`, with `code` when the server answered with an HTTP status, and exits 1. A command line these commands cannot parse, such as a missing argument or an unknown flag, prints one line with `error` and `usage` and exits 1. A `step_end` line or a step row with an `error` field is a step's error, not the command's. When no server works on the state, the readers (`task show`, `lines` and `status --json`) read the local state instead, and the others print one `error` line and exit 1. When a stream of lines drops, as it does when serve restarts on new code, `wait`, `lines --follow` and `arete run` connect again and go on from the last line they read. They give up with an `error` line when the run ended with no `report` line, or when no line came for 2 minutes.

`arete run REPO TICKET` asks the server to start the run, as `POST /api/tasks` with `"force"` set by `--force`, prints the answer, then prints the run's lines until its `report` line. It exits 0 when the report's status counts as success, as before, and when the server queued the ticket instead of starting a run. Ctrl-c detaches and leaves the run going. With `--local` or `--dry-run`, or when no server works on its state, it runs in its own process as before. The server and the poller start a classic run with `--local`.

The `justfile` at the top of this checkout wraps these commands in `adw` recipes, one `uv run arete` call each; `just --list` lists them. [adws/README.md](adws/README.md) is the guide for an agent that steers runs with them.

## Rules

- Agents run through `claude -p`, `codex` or `pi`; subscription logins precede API keys. Claude execution never uses `--bare` or an `ANTHROPIC_API_KEY`, here or on a device.
- A claude run takes no dollar cap and moves to another allowed route on a subscription usage limit, waiting only when none is eligible; budgets count API-key spend and skip paid routes only. Codex has no per-call dollar cap.
- A loop fed by outside text gets only the reading tools, and its events reach the agent fenced as data.
- An agent on the `claude` CLI loads only the MCP servers its call names. The verifier names its browser and every other call names none, so the connectors and plugin servers of the account never load.
- Agents never run git write commands.
  Diff size and deny paths are enforced by code after the agent finishes, not by asking nicely.
- Trust starts at `pr`. Automerge only arrives through a human: a config PR they merge, or for a layer outside the repo, their own edit to `trust.level`. A `push` emit follows the same level: under `pr`, a person lands every push.
- Alert issues carry check names and one short status line, never log contents.
- PRs to public repos cite run ids, never transcript text.
- Triage writes to a ticket only to approve it: the labels of an auto approval, or the answers and labels you send from the dashboard. Its board goes only to a private repo.
- Each verifier server runs on its own port and is stopped with its whole process group after the step.
- A JEV answer settles a question alone only at or over `confidence`; under it, the model decides. A hold or a guard stop, which a person can undo, takes a lower line.
