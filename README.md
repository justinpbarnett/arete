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

## Runners, devices and sessions

A model name picks its runner. A claude alias or a `claude-*` id runs on the `claude` CLI on a Claude subscription. A `provider/model` id, such as `deepseek/deepseek-flash` or `ollama/gemma4`, runs on `pi`. A `gpt-` or `codex-` name, in any case, runs on the `codex` CLI (see [Codex](#codex)); `openai/<model>` stays on pi. A `pi:` or `codex:` prefix forces that runner, and a `replay:` prefix answers from files (see [Replay](#replay)). Claude and pi both report tokens and cost per step, and codex reports tokens; `[pricing]` sets a price for a model whose runner reports none, and a provider in `local_providers` costs nothing. Budgets meter pi and Codex API-key spend. Codex has no per-call dollar cap. A run on claude has no dollar cap: the subscription carries it, and its cost is still recorded, though every sum arete shows you counts only what you pay. When claude reports a usage limit, the run waits until the limit resets and then goes on, resuming its session where it can. The serve holds queued loops that need the held model, the heartbeat counts a held loop as healthy, and chat and loop drafts return the limit at once instead of waiting.

Local pi steps use the file builtins behind Read, Edit, Write, Grep, Glob and LS; executable tools are withheld. Arete explicitly loads its trusted tool guard with owner resource discovery disabled. The guard denies tool access to credential paths while pi's SDK backend can still read and refresh its credential store. Public runtime source stays readable, and tools cannot edit or overwrite it. If the guard cannot load, pi stops before calling the model. Remote runs, including disposable sandboxes, keep their existing tool behavior.

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
When codex reports a usage limit, codex steps wait under a hold of their own, so a codex limit never holds a claude step and a claude limit never holds a codex step.
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

A timed out codex call keeps any thread id already emitted, so a later fix can resume it. Network access is off inside the agent sandbox, so a verify command cannot `curl` the served app; a configured Playwright MCP server runs outside that sandbox.

Arete runs pi with a pi folder of its own, `~/.local/share/arete/pi-agent/`. It links `models.json` and `settings.json` from `~/.pi/agent/` and holds no logins. Keys for pi's providers go in `~/.config/arete/pi.env` as `NAME=value` lines, such as `OPENROUTER_API_KEY=...`. A loop then spends on a key you give arete, such as one with a weekly limit, and never on your own pi logins. A provider with no key there falls back to a key set in arete's own environment, and fails the step when there is none; the launchd serve sets no keys. A device other than `local` runs its own pi with its own logins.

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
In a sandbox, pi gets Bash, Edit and Write, and runs without the rules against git writes, because the VM holds nothing to push with and is deleted after the run.
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
```

`--repo` gives it a new worktree of a config repo; a device of any kind but `local` takes no repo and starts in its `dir`. `--budget` caps the whole session in dollars when it runs on pi; a claude session has no cap. A session with no message for `session_idle_hours` closes. The dashboard's Sessions view starts one with the same choices, under New session.

`arete costs` shows what you paid for model calls, by model, runner and loop. A call's explicit authentication mode decides whether it uses a subscription and counts nothing there; older rows keep their runner/provider interpretation.

`arete costs --by step|kind|model` counts every step instead, at the cost each call reported (a claude CLI step's is the API price), grouped by step name, by the task's kind (`feat`, `fix`) or by model, or by several in turn with `--by step kind model`, over the last `--days N` (default 7). Each row shows the runs (distinct runs in the group), calls, cost, input tokens no cache served (`in`), cache reads and cache writes, output tokens, minutes and API minutes. The minutes are each call's whole time; the API minutes are the part the model's API calls took, as the runner reports it (claude does, pi does not), so the rest went to tools. The last line gives the wait minutes of the runs started in that window, by cause. `--json` prints one JSON line per group and a last line with `wait_minutes`; `--json` alone groups by step. The plain `arete costs` stays the paid view.

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

A pi agent adds the keys of the provider its model names: the part before `/`, such as `openrouter` in `openrouter/qwen3-coder`, or pi's `defaultProvider` in `~/.pi/agent/settings.json` for a model with no `/`.
It gets each variable whose name starts with the provider's name in capitals and `_`, such as `OPENROUTER_*` (a `-` or other sign in the name becomes `_`), plus the names `[environment.providers]` lists for that provider.
It reads them from `~/.config/arete/pi.env` first, and from the server's environment when `pi.env` lacks one.
When arete cannot tell the provider, the agent gets every name in `pi.env`.
It also gets `ARETE_AGENT=1` and `PI_CODING_AGENT_DIR`.

A codex agent adds `CODEX_HOME`, and never gets `CODEX_API_KEY`, `OPENAI_API_KEY` or the parent session's names above, even under `pass = ["*"]`.
It also gets `ARETE_AGENT=1`.
The commands it runs get only the list above, `ARETE_AGENT` and `ARETE_RUN_ID`, through codex's `shell_environment_policy`, so `CODEX_HOME` stays out of them.

These commands start from the same list, plus the values arete sets for them, such as a loop check's `ARETE_RUN_ID` and `ARETE_SCRATCH`:

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
Here a pi agent on `amazon-bedrock/...` also gets every `AWS_*` variable, and no other agent does.
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
- Risk: the automerge of a pipeline PR waits for a person when JEV rates the diff risky (how much depends on what it changes, how near it comes to credentials, money or data, and how far it strays from its description), or reads it as a change a revert will not undo.
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
- Triage: JEV asks whether a ticket asks for a change to a repo and what kind of ticket it is. A ticket it gives a chance of a code change at or under `triage_skip` settles with no model call, as research when JEV is sure it is research and as work for a person otherwise; one under even odds settles the same way when JEV is sure of research or person. Before arete sends a ticket to the pipeline or the research agent, JEV screens its text for instructions to an agent, and a ticket it flags waits for you.
- Research: when a researched ticket changes, JEV reads what changed in its text against the report's summary and asks whether the change asks something different or adds a fact, decision, limit, deadline or resource the report should use. Under a chance of `research_rerun`, the report stands with no agent call. Before a ticket's first report, JEV reads it against the reports already done for its parent, its siblings and its children, and picks the one that already delivers what it asks, or none. At a chance of `research_answered` or more that one does, the ticket links that report instead of a new run. See [Triage](#triage).
- PR feedback: JEV asks of each new comment you leave on an arete PR whether it asks for a change or a question the author should answer, logs the chance, and stores it with the comment. A comment under `feedback_skip` starts no revision; at the default of 0, none is skipped.
- Miner: the digest leaves out the transcripts JEV is sure hold nothing to learn from, so it has room for the ones that do.
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

Poll does not wait for a run. It starts each run as its own detached `arete run` process and returns in about a second, so a long run never holds up the next poll. `max_parallel` sets how many runs go at once on the machine, manual runs included. A run frees its slot while it waits on its PR's CI, and takes one back before a CI fix step. A run that waits to take its slot back goes ahead of new runs: while it waits, no new run starts, even on a free slot. It reads `max_parallel` again every 30 seconds, so a raised limit lets it go at once. `arete status` lists it as waiting for a run slot, and a run in its CI wait as waiting on CI without a run slot. Poll takes tickets round robin across repos and starts at most one run per repo each time. A repo with `max_parallel_per_repo` runs going, counting runs in their CI wait and manual runs, gets no new one from poll; `arete run` by hand is held only by `max_parallel`.

A laptop sleeps and drops its network. When poll, triage or the heartbeat start offline, most often just after a wake, they wait up to 3 minutes for the network; if it does not come back they skip with an "offline" note and exit 0. A git or gh call that fails on a network error waits up to 30 minutes for the network, then tries once more. An agent step cut off by a dropped connection resumes its own session when the network is back. A run whose process died is put back in the queue with a comment on the ticket, and the next poll starts it again; a second death in a row fails the run as before. Heartbeat ages count only time awake, so a night asleep never reads as a stale log.

## Records

Every agent call writes its runner's raw output to `runs/<run_id>/record/<step>.jsonl.gz` under the state folder while it runs.
A call outside any run gets a run id of its own that starts with `call-`.
A second call with the same step name in one run gets `<step>-2.jsonl.gz`, then `-3`, and so on.
The first line is a header with the runner, model, device, repo, commit, folder, prompt, system prompt, tools, JSON schema, budget, timeout and the session it resumes.
Headers, results, timeline `agent` marks and SQLite step rows also carry `auth_mode`: `subscription` or `api_key`. Runner and provider names stay unchanged. An empty or absent mode uses the historical interpretation: pi and replay are metered, Claude is subscription, and Codex is subscription except for its `openai` provider. Old rows are not backfilled from today's login. A crashed or interrupted record keeps its captured mode without inventing usage.
When a call leaves the model to the runner's default, arete holds the header until the runner names the model it runs (claude's `init` event or pi's first reply), for at most 10 seconds, so the header names the model the default turned out to be.
Then come the runner's own lines unchanged: claude's `stream-json` events, pi's `--mode json` events, or codex's `exec --json` events after one `command` line with the argv arete ran.
A clock line with the seconds since the start goes in before a runner line whenever a second or more has passed since the last one.
The last line is the result: ok, error, text, structured answer, cost, tokens, first turn's input tokens, turns, model, session and seconds.
A call that crashes ends with a result whose error starts with `crashed:`, and a call stopped on request (a stop from the CLI or the dashboard, or ctrl-c) ends with the error `stopped by request`.
Neither has a cost or a turn count, in its result or in its timeline mark, because the runner never reported one.

`steps.jsonl.gz` in the same folder is the run's timeline, one line per mark: `create` (a fresh sandbox), `lease`, `clone`, `boot`, `release`, `worktree`, `setup`, `check`, `diff` (with the staged patch), `agent` (one per call, naming its record file) and `result` (the run's final status).
Each mark has its start time, and the marks that time something have their seconds.
Classic `step_end` lines and saved step results include `auth_mode`, joining distinct modes with `, ` when a step retries across modes. Saved results also retain the `auth_modes` list and aggregate `paid_usd`. Seam `step_end` lines join distinct modes like their other call labels; a joined value describes several calls, not one authentication mode. Individual results keep their own mode. A pi schema restatement resolves authentication again before launching; its usage gets a separate step row, and the combined record result lists each call's mode, paid amount and usage in `calls`, alongside aggregate `auth_modes` and `paid_usd`.

Subscription limit holds are qualified by subscription family, nonsecret account identity and limit scope. Matching Claude and pi anthropic account UUIDs share the `claude` family; matching Codex and pi openai-codex ChatGPT account IDs share `codex`. Claude's all-model, opus and sonnet limits remain distinct; Codex's limit covers its family. Different accounts never share a hold. An unknown identity can wait only within its own invocation. Old unqualified holds expire without becoming pi holds.
Discovery reads login metadata separately from launching an agent. Opaque Claude OAuth credentials can use a read-only profile lookup with a short timeout, a response-size bound and no redirects. Only nonsecret identity snapshots and one-way fingerprints are cached; credentials and profile bodies never enter records. A changed credential, account or login directory invalidates discovery, including on bounded wakeups while waiting. Only a successful subscription call clears older applicable holds for its captured account and scope; API-key success cannot clear them. Reset slack, session continuation and original-prompt retries still apply. This foundation does not start using the owner's pi login or change replay execution.

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

## Run lines

Each pipeline run also tells its story as JSON lines in `runs/<run_id>/run.jsonl` under the state folder, one object per line, so one file can be followed while the run works or read after it died.
Every line has `ts` (UTC, like `2026-11-11T12:00:00Z`), `run` (the run id), `seq` (1, 2, 3 and so on within the run) and `event`.
Lines are only appended, never rewritten, and each is flushed as it is written.
A writer holds a file lock while it appends, so several processes can write to one run and the lines stay whole and `seq` stays unique; `seq` goes on from the file, so it carries on after a restart.
A last line cut short by a dying process is ended as a garbage line by the next writer, and readers skip any line that does not parse.

| event | fields |
|---|---|
| `task` | `ticket`, `kind`, `repo`, `title`, and `path` (the name of the script in `adws/` that ran it, such as `feature`) for a run from a [script](#the-adw-scripts); when the run starts |
| `answer` | `asked_in` (the run that asked), `question`, `text` (the answer given through `POST /api/runs/RUN_ID/answer`, or empty); in the new run that the author's reply starts |
| `step_start` | `step`, `runner`, `model`, `effort`, `tier` (the step's tier, or empty), `kind` (the task's kind), `env` (the sorted names of the variables the agent starts with, never their values; see [An agent's environment](#an-agents-environment)); before each agent call. A [seam](#the-seam) step writes `step` and `try` instead, and a seam step that calls an agent (`plan`, `implement`, `revise`, `verify`, `review`, `learn`) adds the `runner`, `model`, `effort`, `tier`, `kind` and `env` its settings resolve to, `--set` overrides included |
| `heartbeat` | `step`, `seconds` so far; every 30 seconds while a step works (`lines.HEARTBEAT_SECONDS`) |
| `step_end` | the start fields plus `ok`, `cost_usd`, `tokens_in`, `tokens_out`, `turns`, `seconds`, `error`, `try`, `cache_read`, `cache_write`, `api_seconds`; the cost counts an attempt the network dropped. `tokens_in` includes the cache reads and writes. `try` is how many calls of this step name the run has recorded, so the second call of `build` has try 2; a step the pipeline numbers itself (`fix1`, `fix2`) starts at 1 under each name. `api_seconds` is the time the model's API calls took (0 on pi, which reports none), so `seconds` minus `api_seconds` is the time spent in tools. A seam step's line has `step`, `try`, `result`, `ok` (true for `pass` alone), `next`, `feedback`, `data`, `session`, then its agent calls' `runner`, `model`, `effort`, `tier` and `kind` (the distinct values joined with `, `) and their summed `cost_usd`, `tokens_in`, `tokens_out`, `turns`, `cache_read`, `cache_write` and `api_seconds`, then `seconds`, `error`, and `tree` and `fingerprint` for a step called with `tree`. An agent call inside a seam step writes no `step_start` or `step_end` of its own; its row in the `steps` table stays |
| `route` | `continues` (the run this one continues), `after` (the last step it took from that run), `skipped` (how many steps it took), `only`, `again` (the step `--from` names, or empty). A continuing or `only` flow writes it twice: an opening line with `continues`, `only` and `again` when the flow starts, so a run that dies before its first step still names the run it continues, then the full line when it starts running steps |
| `wait` | `cause` (`limit`, `ci`, `slot` or `person`), `phase` (`start` or `end`), `at`; the end line adds `since` and `minutes`. `limit` is a claude usage limit hold, `ci` the watch on CI, `slot` the wait for a free run slot: after the CI watch, and before the run started, from the first poll that passed the ready ticket over (for `max_parallel` or `max_parallel_per_repo`). `person` runs from the asking run's `question` line to the start of the run the reply starts, or to the first pass-over if there was one. The `slot` and `person` lines are the run's first lines. A wait whose end line never came is not counted |
| `question` | `question` (the comment), `summary`; when the run asks the ticket's author |
| `error` | `step` (the last step started, or empty), `message`; for a failed or interrupted run, not for a stop by request, and right after the `step_end` of a seam step that raised or returned something it may not |
| `report` | `task` (the ticket's id), `status` as the `runs` table holds it (`step` for a run of one step alone), `prs`, `summary`, `requirements` (each check in use: `what`, `passed`, and `evidence`, the path of its screenshot or of the verify step's `.md` report), `assumptions` (what the plan decided where the ticket left it open), `left_for_a_person`, `review` (`verdict`, `summary`, and `problems`, each with `file`, `text` and `severity`), `question` (the summary of what the run asked), `steps` (for each step name in the run's `step_end` lines, its `tries`, `minutes` and `cost_usd`), `minutes`, `cost_usd`, `wait_minutes` (minutes by cause, for the causes the run waited on only, also kept on the run's row). A classic run and a seam run write the same fields, and a field the run never reached is empty (`""`, `[]`, or a `review` with empty fields), never absent. `requirements` and `review` are the last check in use and review. Always the last line, and the poll writes it for a run whose process died. `flow.respond` writes `status`, the fields it is given and `steps` |

Eval runs write the step events too, but no `task` or `report`.
Each line is also printed to the run's stdout.
`arete lines RUN_ID` prints the lines, `--from SEQ` starts after that `seq`, `--follow` keeps printing new lines until the `report` line, and `--step STEP` prints only that step's lines. It reads them from the server when the server is up, and from the file when it is not.
The dashboard serves the same lines at `GET /api/runs/RUN_ID/events`; see [API](#api).
The run log, the `<step>.json` files, the tables and the records stay as they were; the lines are an addition.

## The seam

`arete/factory.py` is one seam that a run's steps can go through. The steps in `arete/flows.py` run on it, in the order the [scripts in `adws/`](#the-adw-scripts) give, when a repo's layer sets `[factory] path = "adw"`, on any repo; every other run takes the classic pipeline and behaves as before.

- `factory.Outcome(result, next="", feedback="", data={}, session=NO_SESSION)` is what a step returns. `result` is `pass`, `fail`, `ask` or `stop`, and any other value raises. `next` names the step to run next, or is empty for the flow's own order. `data` is a JSON object. `outcome.passed` is true for `pass` alone.
- `@factory.step("name")` registers `fn(flow, *args, **kw) -> Outcome` in `factory.STEPS`. A name registers once.
- `factory.Flow(run_id, continues="", only="", ctx=None, labels=None, again="", restore=None)` is the handle of one run, and `ctx` is whatever the steps need. `flow.step(name, *args, **kw)` writes `step_start`, calls the step and writes `step_end` (see Run lines). `labels(name)` returns the fields that step's `step_start` adds. `try` counts the calls of the step from 1 and goes on from the runs this one continues.
- A step that raises, returns anything but an `Outcome`, or returns `data` that JSON cannot write ends with `fail` and an `error` line, and the exception is raised again. An interrupt ends it with `stop` and no `error` line.
- `tree=<folder>` stores the folder's fingerprint in `step_end`: `git write-tree` over the tracked and untracked files, ignored ones left out, staged on a copy of the index so the real one is untouched. The step function gets `tree` too.
- `continues=<run id>` reads that run's lines and those of the runs it continued. Each `flow.step` call is matched in order against their `step_end` lines, and a call that meets a passing line of the same step returns the stored outcome without calling anything. A call also takes a failing line of the same step that the earlier run moved on from (no error, later lines follow) and gets its stored result, so a flow with a route back in its history follows the same route. A line with a fingerprint matches only while the folder's fingerprint equals the last one recorded for it, so a run whose folder is gone or changed goes on after the last step that did not touch it. The first call that does not match writes the `route` line and every later call runs.
- `only=<step>` takes the steps before it from the stored outcomes, without comparing fingerprints, runs that step even if it passed, and raises `factory.OnlyDone` at every later call, which ends the run with the status `step`. It raises `factory.Refused` naming the step when an earlier step has no stored outcome it can take, and the refusal says so when the runs it continues never started the `only` step. When the folder of a stored step is gone, it calls `restore(line)` with that step's `step_end` line: an empty answer means the folder is back, and any other answer is the reason it is not, which the refusal gives. With no `restore`, a gone folder is refused. The flows in `arete/flows.py` make a removed folder again from the run's branch on origin and run the setup commands in it.
- `again=<step>` with `continues` takes no stored outcome for the last call of that step, so that call and every step after it run again; earlier calls of the step, such as the first `implement` before a fix round, are still taken.
- `flow.respond(status, **fields)` writes the `report` line.

### The adw flow

`[factory] path` in a layer is `classic` (the default) or `adw`; any other value fails the layer's load. The switch works on every repo. A repo with a `github` name gets its PRs, CI watch, ticket comment and merge as a classic run does. A repo with no `github` name needs a git remote `origin`, or `arete run` refuses it as before, and gets a pushed branch.

A run with open review feedback takes the revise flow (`adws/revise.py`); any other run takes the feature flow (`adws/feature.py`). The flows run these seam steps, each with the pipeline's own helpers and the agent call names of the classic run (`plan`, `build`, `revise`, `fix2`, `review_fix`, `ci_fix`, `verify`, `verify2`, `review`, `review2`, `learn`):

| step | does |
|---|---|
| `prepare` | Reads the ticket, holds a closed or blocked one, saves its images, picks the branch, makes the worktree, loads the layer, copies files and runs the setup commands. A revise run whose PR closed ends quietly. |
| `plan` | Feature flow. Plans; asks the author when the plan is ambiguous or over `max_diff_lines`. |
| `revise` | Revise flow, in place of `plan` and `implement`. Changes the code for the review comments and drafts a reply to each. With nothing staged it answers the comments with no code change, posts `No code changes for your review.` and pushes nothing. |
| `implement` | The build, then a fix round after each failed check (`fix2` and up), the review fix, and inside `ci` the CI fix (`ci_fix` and up) with the red output. |
| `checks` | Runs the layer's checks. A fail goes back to `implement` until the build step's `tries` run out; then learn runs without applying and the run fails. |
| `verify` | Checks the change in use, with its own rounds as in a classic run. |
| `review` | Reviews. `request_changes` goes back to `implement` once, then `checks`, `verify` and `review` again. |
| `learn` | Rewrites `expertise.md` as a classic run does. |
| `pr` | Commits the staged change with `git.subject` and the ticket ref and pushes it. With a `github` name it opens each PR, or pushes to the open one, with the classic body, sibling section, labels and draft rule; its outcome holds the PR links. Without one it notes the branch on the run. |
| `ci` | With a `github` name, watches CI in rounds. A red check this change caused runs `implement` with the red output, then the push and the watch again. |
| `publish` | With a `github` name, moves the ticket to `pr`, posts the PR comment (or answers the review comments in a revise run), and applies the triage hold, the risk hold and the merge. Without one, moves the task to `done`. |
| `respond` | Ends the run as the classic run would: the failure comment, the question (and its `question` line), the nothing to build comment, the review answers, or the `pr` or `done` ending. |

A dry run ends after `learn` with the status `dry-run`: it saves `diff.patch`, keeps the worktree and runs no `pr` step. It makes its worktree locally, so it skips the check for closed PRs on the branch; a revise dry run still reads the PR and its body.

Each step's `data` holds what a later step needs, so a run that restarts after an interrupted adw run continues it: it takes every passing step whose worktree is unchanged from the stored outcomes and runs the rest. A run interrupted in the CI watch never opens a second PR: it goes on at `ci`, and the CI round count starts again. The run's `report` line comes from `flow.respond`. The statuses `pr` and `done` count as success; the summary of `done` names the pushed branch.

### The adw scripts

The order of an adw run's steps lives in short scripts in `adws/` at the top of this checkout. Each one holds only the routing, calls the steps from `arete/flows.py` through `flows.step`, and ends with `factory.main(<function>)`, which gives it the same lock, run slot, `runs` row, stop signal and settling as `arete run`.

| script | holds |
|---|---|
| `adws/feature.py` | The feature flow: `prepare`, `plan`, `implement`, the `checks` and fix loop, `verify`, `review` and the review fix, `learn`, then `pr`, `ci` and `publish`. |
| `adws/revise.py` | The revise flow: the same order with `revise` in place of `plan` and `implement`. |
| `adws/step.py` | Runs one step of an earlier run alone. |

```
python adws/feature.py <repo> <ticket> [--force] [--dry-run] [--continue RUN] [--from STEP] [--only STEP] [--set step.key=value ...]
python adws/revise.py  <repo> <ticket> [same options]
python adws/step.py    <run id> <step> [key=value ...]
```

Run them with the Python that has arete installed (`uv run python adws/feature.py ...` from the checkout).

- `--continue RUN` takes the passing steps of that earlier run of the same ticket (see `continues` in [The seam](#the-seam)). A run id of another repo or ticket is refused before any run starts.
- `--from STEP` with `--continue` takes the stored outcomes up to the last call of that step, then runs it and every step after it again. It needs `--continue` and does not go with `--only`.
- `--only STEP` runs that step alone and takes the steps before it from the stored outcomes; with no `--continue` there are none, so it refuses, naming the first step it could not take. The run ends with the status `step`, exits 0 only when the step passed, and leaves the ticket and the tracker as they were. Its `runs` row holds the step in `only_step`, so the ticket's history looks past it: the poller still restarts an interrupted run, a second interruption still fails the ticket, and a run after a question still answers it. A step run that dies or is stopped is settled `interrupted` or `stopped` with no comment, no ticket move and no restart.
- `--set step.key=value` overrides one setting of one step for this run, with the keys and values of `[steps]` (see [Steps](#steps)); repeat it for more. The step names are the seam's (`plan`, `implement`, `revise`, `verify`, `review`, `learn`); `implement` and `revise` both set the build step. A value `[steps]` would not take is refused before any run starts.
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
- State: `~/.local/share/arete/` (sqlite, events, run logs, loop digests, `serve.json`, `server.json`)
- Records: `~/.local/share/arete/runs/<run_id>/record/`, one gzip JSON lines file per agent call and the run's timeline `steps.jsonl.gz` (see Records)
- Run lines: `~/.local/share/arete/runs/<run_id>/run.jsonl`, one JSON line per run event (see Run lines)
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
| `[models]` | Model per role: `plan`, `build` (also fix and revise), `verify`, `review`, `learn`, `uat`, `miner`, `apply`, `eval_mine`, `triage`, `research` (the reports for research tickets), `chat` (the dashboard console), `classify` (a loop's classify step, for the events JEV is unsure of), `loop` (a loop's agent, and the loop drafts from the dashboard), `session` (agent sessions), `explain` (the plain summary of each approval in Needs you). A role left out uses the claude CLI default, except `triage`, `classify` and `explain`, which use haiku, `loop` and `session`, which use sonnet, and `research` and `chat`, which use opus. `arete status` lists each role's model. |
| `[steps]`, `[kinds]`, `[tiers]`, `[factory]` | Each pipeline step's model, runner, effort, timeout, tries, tools, MCP servers, instructions and session. See [Steps](#steps). `[factory] adws` is the folder the adw scripts come from, default `adws/` in this checkout; see [The adw scripts](#the-adw-scripts). |
| `[mcp.<name>]` | An MCP server a step can name in its `mcp` list: `command` with optional `args`, or `url` for an HTTP server. Servers are defined here only, not in a layer. |
| `[miner]` | `lookback_hours` and `max_digest_chars` for the nightly transcript digest. |
| `[[repo]]` | Every repo arete knows: `name`, `path`, `github`, and the fields below. |
| `[[job]]` | launchd jobs the heartbeat watches, by `label`. A `keepalive` job must have a pid; others must have last exited 0. `log` with `max_age_hours` flags a stale log. |
| `[[probe]]` | Real calls the heartbeat makes every `every_hours`: a `command` whose output must contain `expect`. A real call, because an auth check can say ready while real calls fail on an expired token. |
| `[jira]` | See [Jira tickets](#jira-tickets). |
| `[dashboard]` | `port` (default 8796) and `owner`, the tailnet login the page opens for. Default: the login that owns this machine. See [Dashboard](#dashboard). |
| `[serve]` | `loop_dirs` (the private loop folders, default `~/.config/arete/loops`), `queues` (a table of queue name to how many runs at once, default 1), `daily_budget_usd` (most pi spend across all loops in a rolling 24 hours), `session_idle_hours` (default 2). |
| `[jev]` | `enabled` (default true), `model` (`jev-latest`), `confidence` (0.8, how sure JEV must be to settle a question alone; on a yes or no question, a chance of yes at 0.9 or more, or at 0.1 or less), `injection` (0.5, the chance of an injection at which an event drops or a ticket waits), `risk_hold` and `irreversible_hold` (0.5 each, the first for pipeline automerge only, the second also the guard's line for a command git cannot undo), `secrets_hold` (0.55, the guard's line for a command that shows or sends a credential), `triage_skip` (0.2, the chance of a code change at or under which triage settles a ticket as not code without the agent), `[jev.screen_lines]` (the chance at which each push screen question holds a push: `weakens` 0.5, `secrets` 0.7, `outside` 0.7, `irreversible` 0.5, `feature` 0.6, `removes` 0.5, `renames` 0.5, and `reach` 0.8, how far past the fix it names a push may reach before it waits), `proof_hold` (0.5, the proof check's line), `fixed_skip` (0.7, the chance at which a search run fed by findings ends as already fixed), `research_rerun` (0.4, the chance that a change to a researched ticket matters, under which its report stands), `research_answered` (0.8, the chance at which a ticket links a report done for its family instead of a new run) and `feedback_skip` (0.0, the chance that a PR comment asks for a change or an answer, under which it starts no revision; 0 logs only), `[jev.risk_weights]` (`blast` 0.4, `sensitivity` 0.4, `surprise` 0.2), `guard` (true) and `compact_tokens` (120000). See [JEV](#jev). |
| `[pricing]` | `local_providers` (default `["ollama"]`, priced at zero) and `[pricing.models."<model>"]` with `input` and `output` in USD per million tokens, for a model whose runner reports no cost. |
| `[replay]` | `all` (default false, sends every agent call to the replay runner) and `dir` (the folder of answers; `ARETE_REPLAY_DIR` wins over it). See [Replay](#replay). |
| `[environment]` | `pass`, the names of more server variables that local agents and the commands that run a repo's code get, each an exact name, a prefix ending in `*`, or `"*"` for all. `[environment.providers]` maps a pi provider to more names its agents get, such as `amazon-bedrock = ["AWS_*"]`. See [An agent's environment](#an-agents-environment). |

| Repo field | Default | Meaning |
|------------|---------|---------|
| `pipeline` | `false` | Poll this repo's tickets and run the build pipeline. |
| `mine` | `true` | The miner may open harness PRs here. |
| `public` | `false` | PR bodies cite run ids, never transcript text. |
| `tracker` | `"github"` | `github` for issues in the repo, `jira` for tickets in Jira, `local` for [local tasks](#local-tasks) only; such a repo may have no `github`. PRs are on GitHub either way. |
| `jira_project` | `[jira] project` | Jira project for this repo. |
| `jira_label` | `repo-<github repo name>` | Route label that sends a Jira ticket to this repo. |
| `jev` | `true` | JEV may read this repo's events, tickets, diffs, transcripts and files. `false` keeps them off OpenRouter and TypeSafe. See [JEV](#jev). |
| `layer` | `<path>/.agents` | Folder that holds the repo's layer. Point it outside the repo to keep arete files out of a shared repo. |
| `[[repo.part]]` | none | One per git repo under `path`, for a product split over several repos: `dir` (relative to `path`) and `github`. See [Parts](#parts). |

## GitHub tickets

An open issue labeled `agent` that the repo owner opened. Issues labeled `bug` or `fix` are fixes; the rest are features. Arete tracks progress with `agent:running`, `agent:pr`, `agent:failed` and `agent:needs-info`, and the PR closes the issue, unless it leaves a change for a person. Remove `agent:failed` or `agent:needs-info` to send it back to the queue. When the plan finds nothing left to build and the builder changes nothing, arete pushes nothing: with an open PR it keeps the PR in `agent:pr` and says so on the ticket, and with none it asks in `agent:needs-info` whether the ticket is done. A ticket in `agent:needs-info` also goes back when you reply to arete's question in a comment: poll removes the label, and the next run reads the question and your answer. `arete install` creates all of these labels in each GitHub pipeline repo, so `agent` is there to pick from any GitHub client, the phone app included.

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

Progress shows as `agent-running`, `agent-pr`, `agent-failed` and `agent-needs-info`. A reply from you after arete's question takes `agent-needs-info` off, as on GitHub. Bug, Fire, Incident and Problem issue types are fixes, as are tickets labeled `bug` or `fix`. PR titles end with the key, like `Fix login (ABC-12)`, PR bodies link the ticket, and these PRs get no arete labels. Tickets the approval rules hold back, and tickets with a missing or doubled route label, show in `arete status` with the reason. `arete run REPO KEY --force` runs one anyway.

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
| `limits` | Diff size, paths the change may not touch, fix attempts (which also cap the CI fix rounds), and the timeout of each step and of the checks. `max_attempts` and `timeout_minutes` are the defaults for each step's `tries` and `timeout_minutes`, which [Steps](#steps) can set apart. Code enforces the diff size and the paths after the agent finishes. Every repo also denies `.agents/config.toml`, `.github/workflows/` and `.env` files. It denies `.claude/` too, since Claude Code refuses agent writes there. The planner names a change in a denied path for a human to make. The PR shows it under "Left for a person", and a rerun on an open PR writes, rewrites or removes that section to match its plan. The verifier and the reviewer are told that a person makes it, so its absence never fails the run. The CI fix step counts a red check that only that change fixes as not caused by the PR. |
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
| `verify.publish_evidence` | Boolean, default `true`. Set `false` to keep screenshots in the local run folder without uploading them or linking images in PRs. Verification and ordinary PR delivery still run. |
| `verify.notes` | What the verifier must know: how to sign in, what not to touch, what is shared. |
| `research.dir` | Folder, from the repo root, where arete files the reports of this repo's research tickets as pull requests. Leave it out and the reports stay in arete. See [Triage](#triage). |
| `factory.path` | `classic` (default) or `adw`, which runs a ticket through the [adw flow](#the-adw-flow) on any repo. |
| `environment.pass` | More server variables this repo's local agents and commands get, on top of the engine's `[environment] pass`. Read from the layer folder, never from a worktree. A layer takes no `[environment.providers]`. See [An agent's environment](#an-agents-environment). |

A template with an unknown field falls back to the default. A layer may also hold `[steps]`, `[kinds]` and `[tiers]`, which set its steps apart from the engine's; see [Steps](#steps). A layer in `.agents/` that is not committed yet still works: runs read it from the main checkout and lessons land there, but arete never proposes automerge until it is committed. A layer outside the repo is read in place, lessons land in its `expertise.md`, and the miner leaves the repo's `.agents/` alone. Arete never proposes automerge for it: raise `trust.level` by hand.

After a run that had something to learn from (a failed check, a fix, a failed check in use, or PR feedback), the learn step rewrites `expertise.md` whole, in at most 80 lines. It checks each lesson against the code in the worktree and drops a lesson the code now contradicts, with the reason in the run's report. A rewrite over the cap is not applied. A layer outside the repo is made a git repo, and arete commits each rewrite there, so its log shows what was learned and from which run. A clean run learns nothing.

## Steps

Each pipeline step reads its own model, runner, effort, timeout, tries, tools, MCP servers, instructions and session: plan, build (also revise, the fixes and the CI fixes), verify, review, learn and uat. `arete steps` prints what each step resolves to, one JSON line per step. `--repo NAME` reads that repo's layer too, and `--kind feat` or `--kind fix` resolves for that kind of ticket.

```toml
[tiers.default]
S = { runner = "claude", model = "opus" }
A = { runner = "claude", model = "sonnet" }
C = { runner = "pi", provider = "openrouter", model = "qwen/qwen3-coder" }

[factory]
tier_map = "default"

[steps.plan]
tier = "S"
effort = "high"
timeout_minutes = 20
tries = 2

[steps.implement]
tier = "A"
timeout_minutes = 40

[kinds.fix.steps.plan]
effort = "medium"
```

A step is named by its `[models]` role. `implement` is a second name for `build`, and a table names the step one way, not both.

| Key | Meaning |
|-----|---------|
| `tier` | A tier of the tier map in use, which sets `runner`, `provider` and `model`. |
| `model` | The model. It wins over a tier at any level, and routes as in [Runners](#runners-devices-and-sessions). |
| `runner` | `claude`, `pi`, `codex` or `replay`. It wins over the runner the model name picks, but not over a `claude:`, `pi:`, `codex:` or `replay:` prefix. |
| `provider` | For pi, the provider put before a model name that does not start with it. |
| `effort` | claude's `--effort` (`low`, `medium`, `high`, `xhigh`, `max`), pi's `--thinking` (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`), or codex's `model_reasoning_effort` (`low`, `medium`, `high`, `xhigh`, `max`, `ultra`). Left out, the runner's own default. |
| `timeout_minutes` | The step's time limit. Default `limits.timeout_minutes`; learn 15, uat 10. The checks keep `limits.timeout_minutes`. |
| `tries` | For build, the fix attempts after failed checks and the CI fix rounds; for verify, the rounds of verify in use; for uat, the tries at the test steps. Default `limits.max_attempts`; uat 3. The other steps run once. |
| `tools` | The built in tools the step may use, from `Read`, `Glob`, `Grep`, `LS`, `Edit`, `MultiEdit`, `Write` and `TodoWrite`. Default `Read`, `Glob`, `Grep`, `Edit`, `Write` and `TodoWrite` for build, `Read`, `Glob` and `Grep` for the rest. The layer's shell rules still apply on top. pi always grants `Read`, `Glob` and `Grep`, so a pi step lists them. |
| `mcp` | Names of `[mcp.<name>]` servers from `~/.config/arete/config.toml`; claude and codex load those and no others. Default `[]`. verify also gets its own browser server when the layer serves an app, and that server wins over one of the same name. pi takes no servers. codex refuses servers with `env` values. |
| `instructions` | Which standing instruction files load: `user` and `repo`. Default both on claude (`~/.claude/CLAUDE.md`, the repo's `CLAUDE.md` and `CLAUDE.local.md`), `[]` on pi, and `["repo"]` on codex (repo `AGENTS.md` or `AGENTS.override.md`, supplied as developer instructions with automatic project document loading disabled). codex refuses `user`; if the login has global instruction files that the CLI cannot exclude, the call is withheld automatically. On claude, a value short of both becomes `--setting-sources`, which also leaves out that source's `settings.json`. pi runs with `--no-context-files` and takes only `[]`. |
| `session` | `resume` or `fresh`. With `resume`, a later round of the step (for build, a fix, review fix, verify fix or CI fix) continues the session of its earlier rounds; with `fresh`, each round is a new session whose prompt carries the failing output. Default `resume` for build, `fresh` for the rest. learn and uat keep no session, so they take only `fresh`. |

Levels, where the last one to set a key wins: the defaults above, `[steps]` in `~/.config/arete/config.toml`, `[kinds.<kind>.steps]` there, `[steps]` in the repo's layer, then `[kinds.<kind>.steps]` there. A kind is `feat` or `fix`. A level that names a `model` also drops the `runner` and `provider` that lower levels set. A step with neither a model nor a tier takes its `[models]` role. The other roles, such as triage, chat and the miner, read `[models]` alone; `arete steps` shows `tools`, `mcp`, `instructions` and `session` for them too, but only the pipeline steps use those four.

`[tiers.<map>]` lists tiers by name. A layer's `[tiers.<map>]` adds to the engine's map of the same name, or replaces a tier it names again. `[factory] tier_map` names the map in use; it may be left out when there is one map. A repo layer's `tier_map` wins over the engine's choice and may supply the only selection. With several maps and no engine selection, validation of tiered steps waits for the repo layer; `arete steps` without `--repo` still requires an engine selection.

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

Config load and step resolution refuse, in one line that names the table: an unknown step, kind or key, a tier the map does not have, a runner other than `claude`, `pi`, `codex` or `replay`, an effort the step's runner does not take (including `off` on codex), `tries` or `timeout_minutes` under 1, several tier maps with no engine or repo selection, a `tier_map` with no such map, a tool outside the `tools` list above (`Bash` and `mcp__` names included), a pi or codex step whose `tools` leave out a read tool, an `mcp` name with no `[mcp.<name>]`, any `mcp` or `instructions` on pi, `instructions = ["user"]` on codex, an `instructions` value other than `user` and `repo`, a `session` other than `resume` and `fresh`, `resume` for learn or uat, and an `[mcp.<name>]` with an unknown key, with both or neither of `command` and `url`, or with `args` beside `url`. Codex on an ssh or exe device is refused before starting a loop or session. A mistake in a layer fails the run at its start, naming the layer file, and `arete steps --repo NAME` shows it first.

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

When `<dir>/<step>.patch` exists, arete applies it to the call's folder with `git apply` before it answers, even when the answer fails; this is how a replayed build changes files. No answer folder, a missing file, a file that is not one JSON object, a bad value and a patch that does not apply each fail the step with an error that names the folder setting or the file.

A replayed call is recorded like any other: its step row, its record header and its timeline `agent` mark say `replay`, the raw record holds one `replay` line naming the answer and patch files, and it costs what the file says, which `[pricing]` never changes. Usage limit holds, the login check, the guard, JEV rules and devices do not apply to it. JEV calls are not agent calls and still go to JEV. `all = true` also sends chat, triage, loops and sessions to replay, so turn it on in a scratch `ARETE_CONFIG_DIR` and `ARETE_STATE_DIR`.

`arete replay sample FOLDER` writes a fixture into a new or empty folder: `repo` (a git repo with one commit of `notes.txt`), `origin.git` (a bare repo set as its `origin`), `layer` (a layer whose one check greps `notes.txt` for `kumquat`) and `replay` (an answer for plan, build, verify, review and learn, and a `build.patch` that adds the word). It prints, as TOML, the `[replay]` and `[[repo]]` lines to add to the config; the repo has `tracker = "local"`, no `github` and no `pipeline`, so poll never picks it up; add `pipeline = true` with `--adw` and `arete poll` starts its `todo` tasks. `--adw` adds `[factory] path = "adw"` to the layer, so `arete run replay-sample t-...` on a task from `arete task add --repo replay-sample` runs the [adw flow](#the-adw-flow) and pushes the branch to `origin.git`. On scratch state `arete run` calls only a server started on that state (see [From the CLI](#from-the-cli)), so with none it runs in its own process, as before. The answers serve both paths. A `github` name added to the sample's `[[repo]]` makes the adw flow go on to the PR, CI and publish steps, which call GitHub.

## Verify in use

Checks passing is not the bar. After the checks, a verifier agent uses the change the way its user will: it starts the app with `verify.start`, waits for `verify.ready`, then drives it through a headless browser (Playwright, an isolated profile), `curl`, or the CLI, and reports each check it made with what it saw. Reading the code and running the test suite do not count as checks. The builder does not add tests unless the repo's own rules ask for them.

- Pass: the verification report goes in the PR body. By default, screenshots are pushed to the orphan branch `arete/evidence` and linked from the body. Set the repo layer’s `[verify] publish_evidence = false` to retain screenshots locally without uploading them; both classic and ADW runs still deliver the PR and its text report.
- Fail: the build session resumes with what failed, then the checks and the verifier run again, up to the verify step's `tries` (default `limits.max_attempts`).
- Blocked: the verifier could not check something (a service it may not reach, a sign in it cannot finish). The PR opens as a draft and says what it would take.

Arete stops the server's whole process group after each verifier run, so no dev server outlives its step.

## CI

After the push, the run waits for the checks on the PR. It waits up to 30 minutes, or 3 minutes when no check reports at all. The run frees its `max_parallel` slot for the wait, so another ticket can start, and takes a slot back before a CI fix step. Arete compares each failed check with the same check on the head of the base branch.

- Red on the base too: the PR section notes the base result and its date. This PR did not cause it, so nothing changes.
- Passed on the base, skipped there, or no result there: a CI fix step reads the job log and the diff, and decides whether the PR caused the failure. When the base moved after the last push, arete first merges it into the branch, since CI tested the PR merged with that base. A merge that conflicts is undone, and the step works on the branch as it is, as it does when the fetch fails. A check that the PR did not cause gets a note with the reason. For a check that the PR did cause, the step fixes the code. Then the checks and the diff limit run again, arete pushes the fix, and the wait starts again. The build step's `tries` (default `limits.max_attempts`) caps the rounds, and the diff limit counts the whole branch.

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
Each chat continues its own session and waits on its own answer, so several chats can wait at once.
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

These routes steer a ticket's run. The first three answer `{"task", "repo", "run", "state", "why"}` as `POST /api/tasks` does, with the run they started:

- `POST /api/runs/RUN_ID/answer` with `{"text": "..."}` answers the question of a run that ended `needs-info`. It stores the answer on the ticket, as the owner's reply on a local task, a comment on a GitHub issue, which then loses its `needs-info` label, or a comment on a Jira ticket, and keeps it in the run's folder as `answer.md`. Then it starts the ticket's next run, whose `answer` line carries the `text`. A run of the ticket going on, or no free slot, refuses with 409 before the answer is stored. A label GitHub would not remove is named in `why`, and the run starts anyway.
- `POST /api/runs/RUN_ID/resume` resumes a run that ended `interrupted`, `stopped` or `failed`, with the script in `adws/` it ran and `--continue`, so with no step it takes every step the old run finished, failures it moved past included, and goes on from where it stopped. A run that ended `failed` runs the step whose failure ended it again, with every step after it. `{"step": "verify"}` runs that step and every step after it again instead. A run still marked running whose process is gone is settled first, as the poll would. A run of the classic path, a run of one step alone, and a step the run never started are refused.
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
| `arete answer RUN_ID "TEXT"` | `POST /api/runs/RUN_ID/answer` |
| `arete resume RUN_ID [--step STEP]` | `POST /api/runs/RUN_ID/resume` |
| `arete step RUN_ID STEP [key=value ...]` | `POST /api/runs/RUN_ID/steps/STEP` |
| `arete stop RUN_ID` | `POST /api/runs/RUN_ID/stop` |
| `arete status --json [RUN_ID]` | `GET /api/live` for one line per live run; with a run id, `GET /api/runs/RUN_ID/events` for one line per step with `"event": "step"` (`state` is the step's result, or `running`), then the report |

They call only a server that works on their own state. Once it listens, `arete serve` or `arete dashboard` writes its addresses, port, process id, state folder and config file to `server.json` in the state folder. The commands call that server while its process is alive, it works on the same state folder and config file as they do, and its address answers within a second; when it does not answer, a command says so and names `arete serve` and `ARETE_SERVER`. `ARETE_SERVER=http://HOST:PORT` names a server and skips that check. So with a scratch `ARETE_STATE_DIR` or `ARETE_CONFIG_DIR` they never reach the real server: they call a server started on the scratch state, or none.

A failed command prints one line with `error` and no `event`, with `code` when the server answered with an HTTP status, and exits 1. A command line these commands cannot parse, such as a missing argument or an unknown flag, prints one line with `error` and `usage` and exits 1. A `step_end` line or a step row with an `error` field is a step's error, not the command's. When no server works on the state, the readers (`task show`, `lines` and `status --json`) read the local state instead, and the others print one `error` line and exit 1. When a stream of lines drops, as it does when serve restarts on new code, `wait`, `lines --follow` and `arete run` connect again and go on from the last line they read. They give up with an `error` line when the run ended with no `report` line, or when no line came for 2 minutes.

`arete run REPO TICKET` asks the server to start the run, as `POST /api/tasks` with `"force"` set by `--force`, prints the answer, then prints the run's lines until its `report` line. It exits 0 when the report's status counts as success, as before, and when the server queued the ticket instead of starting a run. Ctrl-c detaches and leaves the run going. With `--local` or `--dry-run`, or when no server works on its state, it runs in its own process as before. The server and the poller start a classic run with `--local`.

The `justfile` at the top of this checkout wraps these commands in `adw` recipes, one `uv run arete` call each; `just --list` lists them. [adws/README.md](adws/README.md) is the guide for an agent that steers runs with them.

## Rules

- Agents run through `claude -p` on a Claude subscription, or through `pi` for other providers and local models. Never the API SDK, never `--bare`, never an `ANTHROPIC_API_KEY`, here or on a device.
- A claude run takes no dollar cap and waits out a subscription usage limit; budgets count API-key spend. Codex has no per-call dollar cap.
- A loop fed by outside text gets only the reading tools, and its events reach the agent fenced as data.
- An agent on the `claude` CLI loads only the MCP servers its call names. The verifier names its browser and every other call names none, so the connectors and plugin servers of the account never load.
- Agents never run git write commands, except pi in an exe.dev sandbox, which arete refuses when it could push and deletes after its run.
  Diff size and deny paths are enforced by code after the agent finishes, not by asking nicely.
- Trust starts at `pr`. Automerge only arrives through a human: a config PR they merge, or for a layer outside the repo, their own edit to `trust.level`. A `push` emit follows the same level: under `pr`, a person lands every push.
- Alert issues carry check names and one short status line, never log contents.
- PRs to public repos cite run ids, never transcript text.
- Triage writes to a ticket only to approve it: the labels of an auto approval, or the answers and labels you send from the dashboard. Its board goes only to a private repo.
- Each verifier server runs on its own port and is stopped with its whole process group after the step.
- A JEV answer settles a question alone only at or over `confidence`; under it, the model decides. A hold or a guard stop, which a person can undo, takes a lower line.
