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
Built-in orchestration resolves from the installation even when the configured custom ADW folder has no `orchestrate.py`.
There is no silent fallback to a fixed strategy after orchestration starts.

The `orchestrate` role inherits planning settings unless explicitly configured under `[steps.orchestrate]`.
Its effective tools, instructions, MCP servers and additional directories are empty, with a fresh session for each decision.
Pi's controller profile uses `--no-tools`, `--no-context-files`, `--no-skills`, `--no-extensions`, `--no-prompt-templates` and `--no-mcp`.
Only the engine's guard extension is explicitly loaded.
Controller calls use a fresh, nonpersistent session with internal retry, compaction and cache warming disabled.
The guard records the actual tool/context inventory and exits before a second provider request or any unexpected capability can execute.
Ordinary Pi workers retain their existing tool and session behavior.
Claude and Codex remain worker-only: the current Claude profile does not disable instruction auto-discovery, and Codex has no verified tool-free profile.
An inherited Claude planning route therefore needs an explicitly eligible Pi controller route or it refuses before inference.
Do not treat a replay or the installed CLI's help as an adversarial native-runner isolation check.
A bounded native probe on Pi's `openai/gpt-6.1-sol` subscription route confirmed an empty tool/context/skill inventory, no tool execution and an unchanged sentinel.
Repeat this native boundary check after changing the installed Pi adapter or controller profile.
Use the current Pi `openai` provider for that subscription, not its deprecated `openai-codex` provider.

## Follow and control

Use the ordinary task-start, status, wait, lines, stop, answer, resume, records and cost surfaces.
The controller has no owner-question tool; its validated `owner.ask` operation uses the existing owner-question lifecycle.
Steering queues durable next-turn guidance and does not claim to interrupt an active tool call.
Cancellation retains edits and requires the worker process group to become quiescent before another producer can take its place.

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
A standalone action retains its accepted settings, invalidates dependent evidence and cannot publish.
To select different approved settings, resume the controller instead.
Existing latest-run, version, purged-workspace and PR-head checks still apply.

## Gates and limits

The catalog is an allowlist of role routes, registered prompts, tool bundles and opaque context references.
The operation registry in `arete/orchestration_api.py` is the authoritative API reference.
Decisions carry engine-issued turn/state identities and at most eight strictly validated calls.
Unknown fields, unauthorized routes/context, oversized payloads and invalid dependencies are rejected before any part of a batch dispatches.
Repeated identical receipts are idempotent; conflicting content under the same identity is refused.
Catalog prices that are unavailable remain unknown rather than zero.

Read-only delegates may overlap within `max_workers`; implementation and mutation-capable gates serialize.
Every provider launch requests parent admission, including route fallback.
Controller turns and actual controller launches have finite ceilings; malformed responses consume admitted work.
Writing again consumes a semantic repair allowance regardless of the worker's name.
Existing run/verification deadlines, data/payment policy and device/run admission remain authoritative.
Adaptive structured review/verification excludes transports with hidden ungateable schema-restatement launches.

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
