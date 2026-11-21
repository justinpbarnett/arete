Review the effect of catching an already approved PR up to a moved base.

Repository $repo. The checkout under review is `$checkout`.

## Issue $ref

$title

$body

$comments

$attachments

## Plan

$plan

## Checks on the merged tree

$checks

## Checked in use on the merged tree

$evidence

## Earlier approving review

This review approved the pushed head `$approved_head`. The checkout is at the merged candidate `$merged_head`.

```json
$earlier
```

## Approved diff

Use the approved changes as context for judging the merge.

```diff
$approved
```

## Base changes

Changes from the common ancestor `$fork` to the incoming base `$base_head`.

```diff
$base
```

## Conflict-resolution changes

Edits relative to Git's automatic merge, including any edits made by the resolver outside the conflicted files.

```diff
$resolution
```

$sources

Use the supplied patches for historical comparisons. They omit protected paths and binary payloads. Use read-only
tools to inspect current source files outside protected paths. Refer to binary changes by their file paths.

1. Does the merged tree keep the behavior of the approved change?
2. Do the base changes break, conflict with, or duplicate the approved change?
3. Did conflict resolution change behavior?

## Findings

For a blocker, describe the real caller's path and what goes wrong. Set `severity` to `blocker` and `category` to
`issue`, `regression`, `security`, or `data_loss` according to the failure it causes. In `attribution`, cite the
repository-relative `file` and a changed `line` in the specified diff. Use `"after"` for an added line and
`"before"` for a removed line in `side`. The default is `"after"`. In the base or resolution delta you can also
cite a rename, a file-mode change, or a binary change using only the relevant location fields below.

- For a rename, use the exact `rename from` path in `old_file` and the exact `rename to` path in `file`.
  Use decoded repository-relative paths and omit other location fields.
- For a file-mode change, use `file`, `old_mode` and `new_mode` as shown by Git. For an addition or deletion,
  use `"000000"` for the missing side and Git's `new file mode` or `deleted file mode` for the present side.
  If Git shows a type change as a deletion and addition at the same path, use both modes.
- For a binary change marked `Binary files … differ`, use `file` and set `binary` to `true`.

Set `part` to the diff that contains the cited location.

- `base` for a change in the base delta.
- `resolution` for a change in the conflict-resolution delta.
- `approved` for a changed line in the approved diff that depends on a base or resolution change.
  Cite that change in `base_change` or `resolution_change`, respectively, and explain the dependency in
  `dependency`.

List other findings as `major` or `minor` notes. Findings without valid attribution go in the PR body as notes
found on catch-up. A missing check in use is a note. If the checks show a failure, identify the change that caused
it. The pipeline approves when there are no attributed blockers. Keep `summary` to three sentences.
