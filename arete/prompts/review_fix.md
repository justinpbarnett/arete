---
schema: 1
inputs: [denied, notes, problems]
---
A reviewer read your diff and found blockers:

$problems

Denied paths: $denied.

Fix each blocker so the path it names works. If a blocker is outside what the issue asks, or needs a tool you do not
have (for example a check in use you cannot run), change nothing for it and say why. Never leave a security or data
loss blocker for that reason.

Shell commands can be refused. Do not get around a refusal with another interpreter or shell command. If a check
cannot run, say so on its line and do not report it as passed.

The reviewer also left notes. Fix a note only when the fix is small and inside the issue; leave the rest.

$notes

When investigating a failed check, first rerun its configured command if available and permitted, even if it runs
the full suite. If it cannot run, report the blocker and name the configured command if known. Do not build a
replacement verifier environment.

Exercise each acceptance condition. For repeatable findings, reuse a focused regression or, if none fits, use
a disposable, non-sensitive scratch regression outside the patch (or narrow a failure after the configured rerun).
Use the approved checked-command path or report the gap; do not change check policy. Approval to run a check
does not authorize permanent coverage. Add or extend it only if an explicit task or repository instruction
requires coverage of a specific essential business rule; keep it focused, deterministic and fast. Read the output
and exit code; report the finding, candidate, command, outcome, and any environmental limitation. Never report an
unrun check as passed.

Arete rechecks recorded regressions before another full review, and still requires the full suite and applicable
in-use verification on the final tree. Rerunning a failed check is not rerunning the entire publication gate;
Arete owns that gate.

Do not expand scope beyond the issue. End with one line per blocker id: `B1 fixed: how` or `B1 left open: why`. The
next reviewer reads it.
