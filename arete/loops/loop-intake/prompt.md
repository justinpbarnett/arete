Each event is a GitHub issue labeled `loop` that Justin opened on the arete repo. It asks for a new loop in plain words.

For each issue, write one finding that a build agent can turn into a loop folder without asking anything:

- `title`: the loop name and what it does, under 80 characters, such as `ci-red: file a ticket when main goes red`.
- `fingerprint`: `loop-intake/` plus the issue key, such as `loop-intake/justinpbarnett/arete#12`, so the same issue is never specced twice.
- `location`: the issue URL.
- `summary`: the spec. Name the folder (`arete/loops/<name>/` for a general loop, `<repo>/.agents/loops/<name>/` for one that belongs to one repo). Give the kind, the trigger, each source, the model and runner, the tools, the budgets, stale_hours, and each emit, all in the terms of `arete/loops/README.md` in this repo. Say what prompt.md must tell the agent, and what `arete loop try <name>` should show before it is turned on. End with "Close #<number> when the loop merges."
- `fields`: `issue` (the issue number), `kind`, `trigger`, and `emits`.

Read `arete/loops/README.md` and the loops already in `arete/loops/` before you write the spec, so it uses only what the format supports and does not duplicate a loop that exists. When an issue asks for something the format cannot do, say what is missing in the summary and spec the closest loop that works.

Skip an issue already listed under the findings already reported.
