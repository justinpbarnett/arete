# Loop folders

A loop is a folder that holds `loop.toml`, a `prompt.md` for an agent loop, and any script it calls.
`arete serve` reads loops from three places:

| Where | Name | Use |
|---|---|---|
| `arete/loops/<name>/` in this repo | `<name>` | loops that ship with arete |
| `~/.config/arete/loops/<name>/` (`[serve] loop_dirs`) | `<name>` | private loops; `arete loop new` drafts here |
| `<repo>/.agents/loops/<name>/` for each repo in the config | `<repo>/<name>` | loops that belong to one repo |

A folder name is lowercase letters, digits and dashes.
A folder that does not validate shows as broken in `arete loop list` and on the dashboard, and the rest still run.
`arete loop enable NAME` and `arete loop disable NAME` turn a loop on and off without editing its file; the switch lives in `~/.config/arete/loop-switches.json`.

## Kinds

| Kind | What one run does |
|---|---|
| `command` | Runs `[run] command`. Exit 0 is done. Events arrive on stdin as JSON lines. A stdout line that is a JSON object with `title` and `fingerprint` is a finding; one with a number `value` is the metric value. |
| `watch` | Gives the events (or the output of `[run] script`) to an agent with `prompt.md`. The agent returns findings, a summary, and state for the next run. |
| `climb` | Makes a worktree of `[run] repo`, measures `[climb] measure`, and asks the agent for changes that move the number toward `goal`. Keeps each change that helps and reverts each one that does not. |
| `search` | Makes a worktree of `[run] repo` and asks the agent for a change that makes `[search] check` exit 0. Stops at the first hit. |

## loop.toml

```toml
kind = "watch"                 # command | watch | climb | search
description = "One line on what the loop is for"
enabled = false                # a draft starts off; `arete loop enable NAME` turns it on

[trigger]
every = "1h"                   # or cron = "30 4 * * *" (minute hour day month weekday, local time); not both
run_at_load = false            # also fire once when serve starts
event = ["github.ci_red"]      # only these event types fire the loop, from a source or a webhook; all types when left out
webhook = false                # POST /hook/<name> fires the loop as type github.<X-GitHub-Event>, else webhook; `arete webhook secret NAME` prints the token
metric = "pass_rate < 0.8"     # fire when the latest value of a metric crosses the rule; a metric event is internal and passes the event list
group = "repo"                 # events that share this field wait in line one at a time
trust = "untrusted"            # refuse events below this trust: untrusted | internal | owner; a refused webhook gets 403

[[source]]                     # polled by serve; new events fire the loop; the first read only sets a baseline
type = "github"                # github | jira | log | command
repos = ["arete"]              # config repo names or owner/name; [run] repo when left out
watch = ["ci_red", "issue", "advisory"]
label = "bug"                  # issue: only issues with this label
branch = "main"                # ci_red: the default branch when left out
every = "5m"                   # how often to poll; 5m when left out

# type = "jira":    jql = "project = ABC AND status = Open", limit = 50, on_update = false
# type = "log":     path = "~/logs/*.log", pattern = "ERROR|Traceback" (a regex); one event per new error signature
# type = "command": command = "./check.sh", timeout = 120, trust = "internal"; each JSON line it prints is an event

[run]
command = "arete poll"         # command loops: what to run, split into words as a shell would; pipes go in a script; ./NAME runs NAME from the loop folder
try_command = "arete poll --dry-run"   # command loops: what `arete loop try` runs instead
script = "./gather.sh"         # watch loops with no source: its output is the input
repo = "arete"                 # a config repo name or a path to a git repo; climb and search need one
model = "sonnet"               # an alias or claude-* runs on claude; provider/model (deepseek/deepseek-flash, ollama/gemma4) runs on pi
runner = ""                    # claude | pi, to force one
tools = ["Read", "Glob", "Grep"]       # Read Glob Grep LS, plus Edit MultiEdit Write Bash
device = "local"               # a device from ~/.config/arete/devices.toml
timeout_minutes = 30
budget_usd = 0.50              # stop one run when it spends this
daily_budget_usd = 2.00        # refuse to start when the last 24 hours spent this
queue = ""                     # share a named queue with other loops; [serve.queues] sets how many run at once
stale_hours = 3                # heartbeat alerts when the last good run is older than this

[classify]                     # a cheap yes or no per event before the main agent reads it
prompt = "Is this error new and worth a look?"
model = "haiku"

[climb]
measure = "python bench.py"    # prints a number
goal = "max"                   # max | min
iterations = 5
files = ["src/"]               # where the agent may change code

[search]
check = "pytest -x tests/test_bug.py"  # exits 0 on a hit
iterations = 5
files = ["src/"]

[[emit]]                       # what the loop puts out; several are fine
type = "ticket"                # ticket | pr | digest | notify | metric
repo = "arete"                 # ticket: config repo name or owner/name
labels = ["agent"]             # ticket: `agent` hands the ticket to the pipeline
prefix = "[ci] "               # ticket: put before each title
approve = false                # true holds it until you approve it on the dashboard or with `arete approve ID`
# type = "pr":     draft = true, labels = [], title = ""; climb and search only
# type = "digest": appends to ~/.local/share/arete/digests/<loop>/<day>.md
# type = "notify": a desktop notification
# type = "metric": name = "open_ci_red"; the value is the finding count or the command's `value`
```

## Safety

A watch loop fed by a source or a webhook reads text from outside, so it may only have the reading tools Read, Glob, Grep and LS.
Its events reach the agent fenced as data, and a ticket it files is built from the finding fields, not from the event text.
To act on what it finds, it emits a ticket labeled `agent` and the pipeline does the work under its own gates.
Agents never run git writes; climb and search commit in their own worktree, and a `pr` emit pushes.

## prompt.md

The prompt says what to look for and when a finding is worth reporting.
Arete adds the loop memory, the findings already reported (so the same fingerprint is not reported twice), the events, and the answer format.
A finding has `title`, `summary`, `fingerprint` (the same every time the same problem is found), and optional `severity`, `location`, `repo`, `evidence` and `fields`.

## Commands

```
arete loop new "one sentence"     # draft a disabled loop in the private folder and validate it
arete loop try NAME               # run once now with every emit held; event loops read a sample of real events
arete loop enable NAME            # turn it on
arete loop list | status NAME | start NAME | stop RUN | feed RUN -f
```
