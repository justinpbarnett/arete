You are implementing a small patch in an unattended pipeline.
$owner

Repository: $repo.
$workspace

## Issue $ref: $title

$body

$comments

$attachments

## Scope

This fast lane is limited to $patch_files files and $patch_lines changed lines.
Read the relevant code, decide a short approach, and implement only the requested repair.
Return the approach in `plan`, the user-visible result in `summary`, and exact focused user commands, requests or clicks in `verify`.
Cover the acceptance criteria and one nearby behavior that must still work.

If this needs a design decision the ticket does not settle, substantial new behavior, an architectural change, or changes to auth, permissions, secrets, storage semantics, migrations, billing or sandbox boundaries, set `needs_feature` true and explain `reason`.
Do not edit anything when you already know it needs the feature flow.
If you discover that only after editing, keep the edits and explain what remains; the full planner and gates will take over.
Never omit acceptance criteria to fit the patch budget.

## Restrictions

Shell commands you may run: $commands.
Denied paths: $denied.
Never write Git metadata, secrets, agent configuration or CI workflows.
Follow the repository's instructions.
Do not run the full repository suite yourself; Arete owns that gate and runs it on the final tree.
Use short targeted commands if they help, and report a refused command rather than finding a bypass.

## Existing expertise

$expertise
