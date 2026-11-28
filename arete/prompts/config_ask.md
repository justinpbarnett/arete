---
schema: 1
inputs: [current, feedback, instruction, item_name, kind, path]
---
You are drafting an update to an Arete configuration item.

Target: $kind/$item_name ($path)

## Instruction

$instruction

## Current content

$current

$feedback

## Rules

- Return the complete updated file content in `body` and a concise explanation in `why`.
- Preserve valid syntax, required fields, front matter, and declared placeholders required by this config kind.
- Keep the change minimal and strictly scoped to the instruction.
- Follow behavior-first principles: do not add unnecessary boilerplate or unrelated edits.
