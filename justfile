set positional-arguments

# Start a task from a prompt: prints the task, its run and why
adw prompt repo kind="feat":
    uv run arete task start --repo {{quote(repo)}} --kind {{quote(kind)}} -- "$1"

# Start a tracker item or a task that exists: prints the task, its run and why
adw-ticket repo ticket:
    uv run arete task run {{quote(repo)}} {{quote(ticket)}}

# Every live run, or one run's steps and its report
adw-status id="":
    uv run arete status --json {{quote(id)}}

# Follow a run until its report or its question
adw-wait id:
    uv run arete wait {{quote(id)}}

# A run's lines, or one step's lines
adw-logs id step="":
    uv run arete lines {{quote(id)}} --step={{quote(step)}}

# Answer a parked run's text question; prints the run that continues it
adw-answer id text:
    uv run arete answer {{quote(id)}} -- "$2"

# Continue a run at its first unfinished step, or from a named one
adw-resume id step="":
    uv run arete resume {{quote(id)}} --step={{quote(step)}}

# Repair a held PR from its run's saved review; prints the repair run
adw-repair id:
    uv run arete repair {{quote(id)}}

# Run one step of a run alone, with key=value overrides
adw-step id step *sets:
    uv run arete step "$@"

# Stop a run
adw-stop id:
    uv run arete stop {{quote(id)}}

# Cost and time by step, kind and model, then the wait minutes
adw-costs days="":
    uv run arete costs --json --by step kind model {{ if days == "" { "" } else { "--days " + quote(days) } }}

# The step sheet with its resolved settings
adw-steps repo="":
    uv run arete steps --repo={{quote(repo)}}
