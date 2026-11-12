set positional-arguments

# Start a task from a prompt: prints the task, its run and why
adw prompt repo kind="feat":
    uv run arete task start --repo {{repo}} --kind {{kind}} -- "$1"

# Start a tracker item or a task that exists: prints the task, its run and why
adw-ticket repo ticket:
    uv run arete task run {{repo}} {{ticket}}

# Every live run, or one run's steps and its report
adw-status id="":
    uv run arete status --json {{id}}

# Follow a run until its report or its question
adw-wait id:
    uv run arete wait {{id}}

# A run's lines, or one step's lines
adw-logs id step="":
    uv run arete lines {{id}} --step={{step}}

# Answer a run's question; prints the run the answer starts
adw-answer id text:
    uv run arete answer {{id}} -- "$2"

# Continue a run at its first unfinished step, or from a named one
adw-resume id step="":
    uv run arete resume {{id}} --step={{step}}

# Run one step of a run alone, with key=value overrides
adw-step id step *sets:
    uv run arete step "$@"

# Stop a run
adw-stop id:
    uv run arete stop {{id}}

# Cost and time by step, kind and model, then the wait minutes
adw-costs days="":
    uv run arete costs --json --by step kind model {{ if days == "" { "" } else { "--days " + days } }}

# The step sheet with its resolved settings
adw-steps repo="":
    uv run arete steps --repo={{repo}}
