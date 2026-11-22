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

Queued events survive a failed run or a requested stop as a dead item. A budget exceeded during execution also makes the input dead; a budget hold before execution leaves it waiting without using a retry. A killed run or a signal without a stop request returns its input to the queue once. A second interruption makes it dead, even across serve restarts. Done and ordinary skipped runs consume their input.

`arete loop list` shows dead counts. `arete loop status NAME` lists each dead queue ID, run, event count, time and reason. `arete loop requeue ID` resets its retry count and returns the same events to the queue. Dead items expire after 56 days; waiting inputs and active claims do not expire. Empty schedule inputs are deleted on failure, stop or exhausted retries. Recovery coalesces an empty input with an already waiting fire of the same trigger.

| Kind | What one run does |
|---|---|
| `command` | Runs `[run] command`. Exit 0 is done. Events arrive on stdin as JSON lines. A stdout line that is a JSON object with `title` and `fingerprint` is a finding. |
| `watch` | Gives the events (or the output of `[run] script`) to an agent with `prompt.md`. The agent returns findings, a summary, and state for the next run. |
| `search` | Makes a worktree of `[run] repo` and asks the agent for a change that makes `[search] check` exit 0. Stops at the first hit. |

## loop.toml

```toml
kind = "watch"                 # command | watch | search
description = "One line on what the loop is for"
enabled = false                # a draft starts off; `arete loop enable NAME` turns it on

[trigger]
every = "1h"                   # or cron = "30 4 * * *" (minute hour day month weekday, local time); not both
run_at_load = false            # also fire once when serve starts
event = ["github.ci_red"]      # only these event types fire the loop, from a source or a webhook; all types when left out; "finding" takes findings from another loop's loop emit
webhook = false                # POST /hook/<name> fires the loop as type github.<X-GitHub-Event>, else webhook; `arete webhook secret NAME` prints the token; a delivery counts once by its `X-GitHub-Delivery` or `X-Request-Id`, and a post with neither counts once per 10 minutes by the hash of its body
group = "repo"                 # events that share this field wait in line one at a time
trust = "untrusted"            # refuse events below this trust: untrusted | internal | owner; a refused webhook gets 403

[[source]]                     # polled by serve; new events fire the loop; the first read only sets a baseline
name = "issues"                # a source's history follows its name; needed on each source when a loop has more than one
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
script = "./gather.sh"         # watch loops with no source: its output is the input; printing nothing skips the run
repo = "arete"                 # a config repo name or a path to a git repo; search needs one
model = "sonnet"               # an alias or claude-* runs on claude; provider/model (deepseek/deepseek-flash, ollama/gemma4) runs on pi
runner = ""                    # claude | pi, to force one
tools = ["Read", "Glob", "Grep"]       # Read Glob Grep LS, plus Edit MultiEdit Write Bash
device = "local"               # a device from ~/.config/arete/devices.toml; an exe device gives each run a sandbox of its own
timeout_minutes = 30
budget_usd = 0.50              # pi only: stop one run when it spends this
daily_budget_usd = 2.00        # pi only: refuse to start when the last 24 hours spent this
queue = ""                     # share a named queue with other loops; [serve.queues] sets how many run at once
stale_hours = 3                # heartbeat alerts when the last good run is older than this

[classify]                     # a cheap yes or no per event before the main agent reads it
prompt = "Is this error new and worth a look?"
model = "haiku"                # decides the events JEV is unsure of
jev = true                     # false sends every event to the model

[search]
check = "pytest -x tests/test_bug.py"  # exits 0 on a hit
iterations = 5
files = ["src/"]
proof = "repro.py"             # with a push emit: the scratch script that proves the bug; see Push below

[[emit]]                       # what the loop puts out; several are fine
type = "ticket"                # ticket | pr | push | loop
repo = "arete"                 # ticket: config repo name or owner/name
labels = ["agent"]             # ticket: `agent` hands the ticket to the pipeline
prefix = "[ci] "               # ticket: put before each title
approve = false                # true holds it until you approve it on the dashboard or with `arete approve ID`
# type = "pr":     draft = true, labels = [], title = ""; search only
# type = "push":   approve = { lines = 80, deletes = true, paths = ["src/auth/*"], reviewer = "opus" }, check = "pytest -x"; search only; see Push below
# type = "loop":   loop = "fix-it"; hands each finding to that loop as a finding event
```

## Source history

A source's history (which events it has seen, and where a log was read to) follows its `name`, or its position in the `[[source]]` list when it has none. A loop with more than one source needs a name on each: lowercase letters, digits and dashes, not only digits, not `webhook`, and not repeated in the loop.

The scope keys of a source say what it watches: github `repos`, `watch`, `label`, `branch`; jira `jql`, `on_update`; command `command`; log `path`, `pattern`. An edit to any other key (`every`, `timeout`, `trust`, `limit`, `repo`, `name`) keeps everything and the next poll carries on. An edit to a scope key keeps the seen keys, and the next poll fires the events stamped since the last good poll (less two minutes); older matches are taken as seen and named in serve.log. A log source keeps its offsets for files it read before and starts newly matched files at their end. A command row can carry `data.at` (an ISO time) so it can catch up across a command change; a row without a time is taken as seen.

A new name starts from a baseline. Naming a source that has no name keeps its history only when its scope keys stay the same in that change, so put a name in a change of its own. Adding or reordering sources is safe once each has a name: a named source picks up the history of the unnamed source with the same scope keys.

## What a run can read

A command loop, a watch loop's `[run] script`, and a search check run with these in the environment:

| Name | Holds |
|---|---|
| `ARETE_RUN_ID` | the run id |
| `ARETE_LOOP` | the loop name |
| `ARETE_LOOP_DIR` | the loop folder, for a script that keeps a file there, such as a cursor |
| `ARETE_SCRATCH` | search and the watch script: `runs/<run id>/scratch/` in the state folder. It keeps its files across tries and is never committed. The agent gets its path and may write there, so a check can run a file that the agent wrote, such as a repro |
| `ARETE_REPO_DIR` | the watch script: the checkout of `[run] repo`, empty without one |
| `ARETE_TRIAL` | a command loop: `1` under `arete loop try` |

A search agent that finds the goal cannot or should not be met changes nothing and ends with a line that starts `Stop:` and says why. The run ends as done with that reason, and nothing is emitted. The one exception is a search agent that stops while it narrows a fix the push screen or the proof check held: that fix goes to a person as an approval, with the reason in its report.

## Findings from another loop

A `loop` emit hands each finding to another loop, which fires once per finding. That loop takes them with `[trigger] event = ["finding"]`, and the finding reaches its agent under `## Input`, fenced as data. A finding carries the weakest trust among the events that led to it, so a finding read out of untrusted text stays untrusted. A run with no events, or fed only by its own script, hands on internal findings. A loop that takes findings and has an acting tool such as Edit needs `[trigger] trust = "internal"` or above, so a finding read out of untrusted text never reaches an agent that edits.

A finding handed to a loop that is off, or broken, waits for you as a failed delivery: turn the loop on and try it again, or reject it.
A finding that the loop refuses, for its trust or its event type, is only logged in the run's feed, even while the loop is off, because trying it again cannot change the answer.
A waiting finding that the loop refuses when you approve it or try it again is rejected, and the result says why.

## Push

A `push` emit lands a search run's change on the repo's default branch, with no PR. To land it, arete rebases the run's branch onto `origin/<base>`, runs the check again on the rebased tree, and pushes. When the push loses a race, arete fetches and tries once more. Then it fast forwards the repo's own checkout when that checkout is on the base branch, and deletes the run's branch.

`check` is the command that must pass after the rebase.
A search loop's push may leave it out, and then the `[search] check` runs.
A push with no check never lands.
On a public repo, the commit message, the title and the report name the loop and the run id only, never what the agent wrote.

A push waits for approval in these cases:

- Always, while the repo's `trust.level` is `pr`.
- Always, when it touches a path the repo denies: `.agents/config.toml`, `.github/workflows/`, `.env` files, `.claude/`, and the layer's `limits.deny`.
- When `approve = true`.
- When a rule in the `approve` table matches. `lines` holds a change over that many lines, `deletes` holds any file deletion, and `paths` holds a change to any file that matches one of its patterns.
- With JEV on, when JEV reads the diff as more than a narrow fix, at a chance at or over that question's line in `[jev.screen_lines]`:
  it weakens a protection (`weakens`, 0.5), exposes credentials or personal data (`secrets`, 0.7), sends data somewhere new or builds a command from outside text (`outside`, 0.7), does harm a revert will not undo (`irreversible`, 0.5), adds a capability (`feature`, 0.6), removes a behavior instead of fixing it (`removes`, 0.5), or renames something other code or config uses (`renames`, 0.5).
  A reach past the fix it names of `reach` (0.8 of 1) or more holds it too.
- With JEV on and `[search] proof` set, when JEV doubts that the proof script tests the problem the finding describes: a chance of `[jev] proof_hold` (0.5) or more that it checks something else, or under it that it runs the code into the problem and checks for the wrong result. A missing proof, or one past 12000 characters, holds too, and the approval shows the proof.
- With JEV on, when the diff runs past the 12000 characters JEV reads, or when the JEV call fails.

In a search loop, the JEV screen runs as soon as a try makes the check pass, and a hold goes back to the agent before it waits for approval.
The agent's next try gets JEV's reason and the diff so far, and adjusts the change so the check still passes and only the fix remains.
Each adjusted change that passes the check replaces the held change as one commit and is screened again, and a try that fails the check goes back to the held change and its proof.
The run delivers the last change that passed the check: it lands when JEV clears it, and it waits for approval when the tries run out, the budget runs out, or the agent stops because the fix needs what JEV holds.
A proof check hold goes back to the agent the same way: the agent rewrites the proof, and may leave the code as it is.
Only a JEV hold goes back to the agent.
A rule in the list above, a diff past 12000 characters, and a failed JEV call wait for approval at once.

Before the first try of a search run fed by findings, JEV checks whether a commit that landed since the finding's run started, or a push of the same loop that waits for approval, already fixes the finding. At a chance of `[jev] fixed_skip` (0.7) or more, the run ends as done, already fixed by that change, and no agent runs. `arete jev report` shows what came of each verdict.

The approval says which rule held it. Approving it lands it then, and rejecting it deletes the branch. A push that fails to land, because it no longer rebases or the check fails after the rebase, stays listed as a failed approval with its error, so you can try it again or reject it.

`reviewer` in the `approve` table names a model, such as `"opus"`, that reviews each held push before a person does.
Serve gives it the change rebased onto the current `origin/<base>`, the reason the push was held, the agent's case for it, and read-only tools with git's read commands.
When it approves with high confidence, the push lands as if a person approved it, and the run's feed says that the reviewer approved it.
Any other answer leaves the push waiting, with the review beside it on the dashboard and in `arete approve`.
A push held by the trust level, by a path the repo denies or by `paths` always waits for a person, and its review only advises.
So does a push whose diff runs past the 60000 characters the reviewer reads.
`approve = true` holds every push for a person, with no review.
A change that newer commits on the base branch conflict with gets a reject from git that names the files, with no model call.
A review that fails is tried again after 15 minutes, none starts while the model's usage limit holds, and a failed approval gets none.

## Safety

A watch loop fed by a source or a webhook reads text from outside, so it may only have the reading tools Read, Glob, Grep and LS.
Its events reach the agent fenced as data, and a ticket it files is built from the finding fields, not from the event text.
With JEV on, an event JEV reads as instructions to an agent is dropped before the agent sees it, and a ticket drawn from outside text that reads that way waits for approval.
To act on what it finds, it emits a ticket labeled `agent` and the pipeline does the work under its own gates.
Agents never run git writes; search commits in its own worktree, and a `pr` or `push` emit pushes.
A loop run by pi gets Bash only on an `exe` device, whose sandbox holds no git credentials and is deleted after the run.

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

## Merged base use checks

`merge-check` ships disabled. It is a command loop covering configured pipeline repositories. Run
`arete loop try merge-check` before `arete loop enable merge-check`. Try holds every emit and uses
`arete merge-check --dry-run`: it samples the two latest real merged PRs per repo, even if already
discovered, without fetching, creating a worktree, running setup, starting a service or calling a model.
Empty history finishes skipped with zero model calls and no ticket or metric preview. There is no
prompt.md. A watch loop cannot do these checks under the source safety rules.

`arete merged --since-cursor` prints stable PR events from the complete merged pipeline history.
It reads the source's seen set; polling owns discovery and enqueueing. Command sources receive
`ARETE_LOOP`, `ARETE_LOOP_DIR`, `ARETE_SOURCE` and `ARETE_SOURCE_SAMPLE` (1 only for previews).
`data.at` is the recorded merge completion time. The first poll baselines history; late discoveries
remain discoverable because there is no timestamp watermark.

`arete merge-check [REPO]` accepts JSON events on stdin and snapshots all unchecked merges for
each selected repo. It checks a detached worktree pinned to `origin/<base>`, reuses saved check
names and How instructions, and includes one earlier overlapping neighbour. Missing reports or
diff history block verification. Reports, manifests, server logs and evidence stay in the local run
folder. Findings include only PRs, run IDs and failed manifest check numbers/names; verifier prose
and page contents stay local. Completed passes emit only `{"value":1}`. Completed product failures
emit a deterministic finding and `{"value":0}`. Infrastructure failures preserve unchecked work and
use the usual failed/dead-input recovery (`arete loop status merge-check`, then `arete loop requeue ID`).

The consumer serializes checks using `.state/consumer.lock` under `ARETE_LOOP_DIR` (the shipped
loop directory for direct calls). `checked.json` tracks PR identities independently of source history;
`quota.json` reserves at most six attempts per repo per UTC day, before the product starts. Dry runs
and skips reserve nothing. A quota hold releases the input to its existing queue and dispatch waits
for the persisted daily reset. Each attempt has one logical verifier call, a 40-action runtime cutoff
and a 30-minute deadline, without repair or network recovery. Unresolved scratch ownership retains
the workspace and blocks the verdict.

Ticket emits can explicitly select `repo = "$finding.repo"` with internal-or-higher trigger trust.
Each finding must name a configured repository; missing and unknown repos are refused before
rendering or sending. Fixed repo emits keep their existing behavior. The shipped loop uses this
selector, prefix `[merge-check] ` and label `agent`.

The named queue defaults to capacity one. Its explicit equivalent in private configuration is:

```toml
[serve.queues]
merge-check = 1
```

See the [offline fixture recipe](../../README.md#offline-merge-check-fixture) for disposable origins,
replay answers, blocked recovery and quota checks.

## Release journeys

`release-journeys` ships disabled. Run it after the merged release has been built and installed.
Put the selection in `release-journeys.toml` in the Arete config directory and run
`arete loop try release-journeys` to exercise the build and preview findings. Enable the loop for
manual starts with `arete loop enable release-journeys`, then start it after each release with
`arete loop start release-journeys`. A release job can run `arete release-journeys --config FILE` directly.

```toml
python = "/absolute/path/to/release/bin/python"
wheel = "/absolute/path/to/arete-0.1.0-py3-none-any.whl"
build = "0.1.0 sha256.0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
journeys = ["first-run", "config-protection"]
```

Set `build` to the output of that interpreter's `-I -B -m arete.releasebuild WHEEL`. Install the
wheel with a tool that records its archive SHA-256 in `direct_url.json`, such as pip installing
directly from the wheel file. Keep the wheel and its installation immutable throughout the run.
The probe checks the file's SHA-256 against the installation provenance and verifies the
installed package and metadata against the archive. Each attempt, including confirmation
replays, records the archive SHA-256 and the version from the wheel's `METADATA`.

| Journey | Actions and invariants |
|---|---|
| `first-run` | Run `init`, validate the generated settings, then run `migrate` and `status --json --offline`, which reads the local store and needs no running server. Check that migration creates state and status shows zero live runs. |
| `config-protection` | Seed `max_parallel = 2`, run `init`, then `init --force`. Check that the first call refuses and preserves the exact settings and that the forced call replaces them. Validate the replacement settings. |

Each attempt starts with fresh scratch config and state. A run allows at most two journeys, 16 release
actions including identity probes, and 60 seconds of cumulative action time. Each release action
has at most 10 seconds and 64 KiB of captured stdout. The controller kills the action process group
at its deadline, and the group also dies if the controller exits. The report keeps command
arguments, exit codes, checked invariants and the build identity. Product output and fixture
contents are discarded.
The `validate-settings` action runs the selected build's settings loader in the release interpreter
and requires the generated file. Use a build that includes this validation contract. The
`first-run` journey requires macOS for confined SQLite. On other hosts it records an environment
block after the identity probe. `config-protection` can run without confined SQLite support.

A successful action that leaves an expected file missing can fail an invariant. An unsafe or
unreadable scratch file and malformed selection TOML produce environment blocks. Controller
file operations run outside the action budget. Scratch removal follows the durable report
write, and a removal failure logs a warning. An action timeout is recorded on the attempt.
If action teardown also fails, the report keeps both causes and the first failure decides
the attempt's outcome. A failed identity probe is a startup outcome, an unverified wheel or
build mismatch is an environment outcome, and unexpected action exits and command I/O errors
are infrastructure outcomes.

An invariant has to fail twice on the same pinned build before it becomes a finding. Inconsistent
replays are inconclusive. Findings are coalesced by journey and invariant across builds. Reports
remain under `release-journeys/runs/` in the Arete state directory. The first report for each
fingerprint is linked from `release-journeys/findings/` after its report is durable. A report or
finding write failure exits 1 and saves `infrastructure.json` beside the run report when storage
permits. An already saved run report is retained. A trial writes a run report and previews findings
without reserving their fingerprints. The shipped loop keeps findings local for human review.
A completed run exits 0, including a confirmed product failure. A blocked run exits 1 unless it also
has a confirmed finding to report. Read the per-journey outcomes when a run reports `partial`.

### Release verification recipe

On a macOS verification host, use the verification step's approved sandbox and disposable config,
state, and log directories. Use Python 3.13 and an owned scratch directory that permits wheel
installation and journey teardown. Record an infrastructure block if the sandbox denies a
required filesystem operation.

```sh
git log -1 --format=%H
release_python=$(command -v python3.13)
journey_dir=$(mktemp -d "$PWD/.arete-release-journeys.XXXXXX")
uv build --wheel --offline --python "$release_python" --out-dir "$journey_dir/wheels"
wheel=$(echo "$journey_dir"/wheels/arete-*.whl)
shasum -a 256 "$wheel"
"$release_python" -m venv "$journey_dir/release"
"$journey_dir/release/bin/python" -m pip install --no-index --no-deps "$wheel"
"$journey_dir/release/bin/python" -I -B -m arete.releasebuild "$wheel"
```

Write the selection above to `$journey_dir/release-journeys.toml` using that interpreter's
absolute path, the absolute wheel path, and the observed build. Run the installed command with
disposable config, state and logs.

```sh
env -i PATH=/usr/bin:/bin HOME="$journey_dir" TMPDIR="$journey_dir" \
  ARETE_CONFIG_DIR="$journey_dir/config" ARETE_STATE_DIR="$journey_dir/state" \
  ARETE_LOG_DIR="$journey_dir/logs" XDG_STATE_HOME="$journey_dir/client" \
  "$journey_dir/release/bin/python" -I -B -m arete.cli release-journeys --config "$journey_dir/release-journeys.toml"
```

Open the reported path under `$journey_dir/state/release-journeys/` and verify that both journeys
passed, every observed identity matches the pin and `shasum` output, all nine release actions are
accounted for, and the report has no findings. Keep the source commit, installed identity, and report
together for each artifact.

For a regression check, build a second wheel from a disposable copy of the same revision with the
existing-file guard in `arete/commands/setup.py`'s `cmd_init` refusing even when `--force` is
passed. Install it in a second virtual environment under the same checkout as the candidate
environment using the same offline procedure. Record its archive digest and installed identity.
Confirm that the two installed identities differ. Select the seeded interpreter and wheel with
the candidate pin for `config-protection`. Expect an environment block after a single identity action, the
seeded identity recorded as observed, and no finding.

Set the seed's identity in `$journey_dir/config/release-journeys.toml` and select only
`config-protection`. Migrate the disposable state before running the loop commands. The
`first-run` journey migrates its own scratch database.

```sh
disposable_arete() {
  env -i PATH=/usr/bin:/bin HOME="$journey_dir" TMPDIR="$journey_dir" \
    ARETE_CONFIG_DIR="$journey_dir/config" ARETE_STATE_DIR="$journey_dir/state" \
    ARETE_LOG_DIR="$journey_dir/logs" XDG_STATE_HOME="$journey_dir/client" \
    "$journey_dir/release/bin/python" -I -B -m arete.cli "$@"
}

disposable_arete migrate &&
  disposable_arete loop try release-journeys &&
  disposable_arete loop status release-journeys &&
  disposable_arete loop status merge-check &&
  disposable_arete merge-check --dry-run </dev/null
```

The bundled trial should preview one finding, show the seed's identity on both fresh attempts,
and leave the canonical findings directory empty. Both loops should show as off in untouched
disposable settings. With empty merge-check history and no configured repositories, the dry-run
should report nothing merged and zero model calls. Check that it creates no release journey run.

Run the seeded selection directly twice with the same disposable state. Expect two matching
`forced-init-replaces-config` failures attributed to the seed in each report, one finding
emitted on the first run, and one canonical finding pointing to that run's report. Retain the
reports as needed and remove only the disposable resources created for this verification.
