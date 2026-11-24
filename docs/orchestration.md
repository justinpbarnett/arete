# Orchestration

New delivery runs use `adws/orchestrate.py` when their layer omits `[factory] path`.
The controller chooses bounded actions and supervises delegates; Arete retains permissions, workspaces, budgets, evidence, Git, PRs, tracker updates and recovery.
A separate planner, learning pass and documentation pass are optional, not hidden prerequisites.
An explicitly invoked fixed or custom script keeps its strategy.

## Select or revert

Set the layer deliberately:

```toml
[factory]
path = "orchestrated" # or "classic" or "adw"

[orchestration]
max_decisions = 64
max_actions = 128
max_workers = 3
progress_seconds = 30
```

Changing the default does not rewrite private layers pinned to `adw`.
Changing only the default path affects new work, not the recorded mode of an interrupted run, owner answer or PR-feedback continuation.
Changes to admitted permissions, routes or other policy still require reconciliation before continuation.
Automatic PR-feedback runs continue the existing controller lineage, accepted per-run settings and remaining decision/action allowances instead of starting a fresh controller under current defaults.
When no owner-answer or interrupted continuation takes precedence, explicit `--force` without `--continue` requests a new owner-directed run; existing cumulative run, verification and repair accounting still applies.
Built-in orchestration resolves from the installation even when the configured custom ADW folder has no `orchestrate.py`.
There is no silent fallback to a fixed strategy after orchestration starts.

The `orchestrate` role inherits planning settings unless explicitly configured under `[steps.orchestrate]`.
Its effective tools, instructions, MCP servers and additional directories are empty.
For repositories enrolled in [durable issue orchestration](../README.md#durable-issue-orchestration), live issue decisions and monitor turns use one Pi Durable conversation across runs.
Every other controller decision uses a fresh session.
Pi's controller profile uses `--no-tools`, `--no-context-files`, `--no-skills`, `--no-extensions`, `--no-prompt-templates` and `--no-mcp`.
Only the engine's guard extension is explicitly loaded.
The CLI controller profile uses a fresh, nonpersistent session with internal retry, compaction and cache warming disabled.
The guard records the actual tool/context inventory and exits before a second provider request or any unexpected capability can execute.
Ordinary Pi workers retain their existing tool and session behavior.
Claude and Codex remain worker-only: the current Claude profile does not disable instruction auto-discovery, and Codex has no verified tool-free profile.
An inherited Claude planning route therefore needs an explicitly eligible Pi controller route or it refuses before inference.
Do not treat a replay or the installed CLI's help as an adversarial native-runner isolation check.
A bounded native probe on Pi's `openai/gpt-6.1-sol` subscription route confirmed an empty tool/context/skill inventory, no tool execution and an unchanged sentinel.
Repeat this native boundary check after changing the installed Pi adapter or controller profile.
Use the current Pi `openai` provider for that subscription, not its deprecated `openai-codex` provider.

The isolated Python delegation subprocess is a trusted Arete SDK broker, not the model producer.
Like the main engine, it applies the selected runner's model confinement rather than inheriting a generic subprocess sandbox; nested macOS profiles prevent native startup.
Its fixed engine entrypoint, isolated imports, immutable policy checks, parent-admitted launches and supervised process group remain separate from model tools and execution.
The model retains the runner's filesystem confinement, JEV/tool checks and closed-context restrictions.

## Follow and control

Use the ordinary task-start, status, wait, lines, stop, answer, resume, records and cost surfaces.
The controller has no owner-question tool; its validated `owner.ask` operation uses the existing owner-question lifecycle.
Steering queues durable next-turn guidance and does not claim to interrupt an active tool call.
Cancellation retains edits and requires the worker process group to become quiescent before another producer can take its place.
A controller `run.wait` returns on worker completion, failure, a new progress report, or the applicable timeout.
Use `timeout_seconds: 0` to follow active workers until the earliest remaining worker step timeout or the run deadline.
A positive timeout sets an additional bound and the default is 60 seconds.
Early wakeups for progress are spaced by `progress_seconds`, with routine tool activity and heartbeats still available in records and viewers.
Stop and the run deadline interrupt the wait promptly.

A decision containing one wait that blocks and times out without a worker change releases its decision charge.
A wait with no active workers returns immediately and spends a decision.
Decisions with multiple calls spend a decision even when a wait in the batch times out.

Run details include an additive `orchestration` object with lineage, generation, state version, controller decisions/launches and stable action IDs.
Both run viewers list those actions, their results and effective requested settings.
The new viewer places them in the Actions tab and follows a standalone rerun to its new run.
An ended run does not leave unreceipted steps shown as running; their usage remains explicitly uncertain.
Raw model records and step-cost rows retain the actual runner/model/effort, including fallbacks, separately from controller overhead.
`orchestrate-dNNNN` names controller calls; `worker-dNNNN-N` names delegate calls.

Adaptive reruns use action IDs, not ambiguous phase names:

```sh
uv run python adws/orchestrate.py REPO TICKET --continue RUN --action d0003-0
uv run python adws/orchestrate.py REPO TICKET --continue RUN --action d0003-0 --only-action
```

The existing task resume/step APIs and the viewers' “Rerun alone” control accept the same durable IDs.
Only finished worker, check, verification and review actions with retained workspaces are rerunnable.
After publication removes the workspace, viewers hide standalone reruns and the API refuses them before starting a run.
A full controller continuation can reconstruct completely removed, settled published work through the existing guarded branch/worktree recovery path; this does not make a standalone action eligible or grant cleanup authority.
Incomplete retained workspaces and legacy controllers without recorded invocation settings refuse for reconciliation instead of discarding edits or guessing past authority.
A standalone action retains its accepted settings, invalidates dependent evidence and cannot publish.
To select different approved settings, resume the controller instead.
Existing latest-run, version, purged-workspace and PR-head checks still apply.

## Gates and limits

The catalog is an allowlist of role routes, registered prompts, tool bundles and opaque context references.
The operation registry in `arete/orchestration_api.py` is the authoritative API reference.
Operation descriptions expose the same enum and numeric constraints used for admission.
Model entries identify their permitted operations and worker prompts; a refused route reports approved IDs for the requested roles.
Decisions carry engine-issued turn/state identities and at most eight strictly validated calls.
Unknown fields, unauthorized routes/context, oversized payloads and invalid dependencies are rejected before any part of a batch dispatches.
Repeated identical receipts are idempotent; conflicting content under the same identity is refused.
Catalog prices that are unavailable remain unknown rather than zero.

Read-only delegates may overlap within `max_workers`; implementation and mutation-capable gates serialize.
Every provider launch requests parent admission, including route fallback.
The controller records all inference launches.
Issue goals have unlimited cumulative decisions, actions, verification time, active runtime and repair cycles.
Other runs charge fallback launches against the launch ceiling.
An exempt wait releases one launch charge, and malformed responses consume admitted work.
Writing again consumes a semantic repair allowance regardless of the worker's name.
Existing run/verification deadlines, data/payment policy and device/run admission remain authoritative.
Adaptive structured review/verification excludes transports with hidden ungateable schema-restatement launches.
Pi returns its structured answer from the admitted invocation or fails that call without launching a restatement.
Its structured calls disable automatic retry, compaction and cache warming; an engine-authorized retry still needs normal admission.

### Pi CLI verification

Local Pi verification can execute the step's Bash grants on macOS through a private descriptor to a trusted engine-side shell broker.
The broker is privileged engine code, not a model-controlled process or an unrestricted host shell.
It validates the command, applies the existing directory/secret/Keychain/JEV guard and git-mutation refusals, then launches the command inside a separate filesystem profile.
The model cannot choose the profile, working directory, environment or additional writable roots.
Patterned grants accept one simple command, not shell chaining, substitution or redirection; a denied form must be rewritten within the existing grants, not retried unchanged.
Commands receive no provider credentials or loader hooks and cannot read engine authority, configuration, peer worktrees or login stores.
Recursive secret exclusions remain active while ordinary directory creation and cleanup work.
Reads are limited to the granted workspace/directories, the installed Arete/Python/Node/uv runtimes and operating-system-managed tool/library/configuration roots, not arbitrary host directories.
Hard-linked regular files in granted workspaces and mutable runtime read roots are excluded and commands cannot create hard links; hard-link-dependent fixtures are unsupported on this route.
Owner-configured `[agent] read_roots` in a repository layer can admit absolute paths for additional read-only command runtimes, such as a verification wrapper outside the workspace.
These roots receive the same recursive-secret, hard-link and protected-path restrictions, never a write grant or trusted cleanup authority.
They are part of the pinned layer policy and cannot be supplied by a model's shell request.
Runtime roots must be outside all agent-writable trees, including shared temporary and runner-support directories; their canonical targets are pinned before model execution and a changed target is refused.
Additional writable project directories require existing engine-admitted directory grants.
Commands use the workspace for temporary storage and cannot access shared system temporary files outside their granted roots.
Commands cannot inspect host processes or send signals outside themselves and their children.
Each command is bounded by 120 seconds, the admitted invocation's deadline and 32 KiB of output; interruption closes the channel and kills the command group.
The Pi process retains its existing confinement and file-tool guard, including protected-file identities supplied before entering confinement.
The broker copies verification producer context so command groups retain the existing scratch lifetime accounting.
This grants neither trusted fixture ownership nor browser/MCP tools, production server access, publication authority or a broader default rollout.

Publication requires current full checks, independent review, applicable in-use verification and a stable, quiescent delivery tree.
Evidence binds tree, policy, original acceptance inputs, producer and observation time.
An operation completing is not proof its product verdict passed.
Existing refactor skips, missing verification configuration, blocked verification and held-draft review dispositions remain explicit.
CI failure returns to the controller for an explicit diagnosis/repair, after reacquiring the run slot.
A dry run retains its local diff/report/workspace and performs no publication or tracker write.

## Recovery

SQLite orchestration tables are authoritative; run lines and viewer state are projections.
A successor fences the old generation, checks producer identities and restores durable workspace authority and matching results.
Completed evidence remains usable only for its matching tree, policy and owner acceptance inputs.
Interrupted inference may have unknown usage, which is not an unlaunched free call.

A changed admitted controller/catalog/policy, missing durable preparation, live prior producer or uncertain publication effect refuses automatic continuation.
Inspect retained records and workspace/branch/PR ownership before reconciling such a run.
The implementation does not promise exactly-once external publication.
Starting another run or deleting state is not a safe substitute for reconciliation.

## Whole-workflow comparison

`arete experiment run MANIFEST --dry-run` validates a design without calls or writes.
A normal experiment run is a separately authorized model trial, not ordinary development verification.
Use a distinct workflow manifest rather than `atom = "orchestrate"`:

```toml
name = "delivery-topology"
repo = "your-configured-repo"
executor = "workflow"
repeats = 6
cache_policy = "provider-default"

[arms.fixed]
executor = "fixed"

[arms.single]
executor = "single-worker"

[arms.adaptive]
executor = "orchestrated"

[tasks.existing-task-name]
split = "validation"
base = "FULL_BASE_COMMIT"

[accept]
baseline = "fixed"
split = "validation"
min_pairs = 6

[limits]
calls = 300
tokens = 3000000
spend_usd = 100
minutes = 120
```

Use pre-existing task names/base commits and the manifest's supported cache policy; these values are illustrative, not a trial authorization.
All arms use fresh sanitized workspaces, a local disposable origin, the shared repository authority and the production delivery executors in dry-run mode.
The single-worker executor omits planning/controller inference while retaining independent engine gates.
Hidden acceptance files are installed only after candidate execution finishes, and grading stays independent and fixed.
Candidate tools are denied the original source repository and parent experiment state/config/worktrees.
Whole-workflow arms share registered prompts/context; atom-only prompt/context overrides and optimizers are refused.

Shared finite experiment limits govern admitted calls, concurrent token/spend reservations and elapsed time.
A grant only narrows a launch's existing limits.
Codex still requires sufficient reserved headroom rather than an invented exact token/dollar stop.
Missing or uncertain usage stops the experiment for reconciliation and leaves charged/reference totals unknown, with retained receipts.
Results include total actual usage, controller/worker breakdowns, owner interventions, accepted outcomes and cost per acceptance, including failed attempts.
Legacy manifests without `executor` retain their atom behavior and design shape.
Replay demonstrates execution mechanics, not native isolation, model quality or an economic advantage for orchestration.
