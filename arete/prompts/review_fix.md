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
Use a focused reproduction for other blockers or to narrow a failure after the configured rerun. Read its output
and exit code. Rerunning a failed check is not rerunning the entire publication gate; Arete owns that gate.
Name exactly what you ran and any environmental limitation; never report an unrun check as passed.

Do not expand scope beyond the issue. End with one line per blocker id: `B1 fixed: how` or `B1 left open: why`. The
next reviewer reads it.
