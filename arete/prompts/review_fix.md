A reviewer read your diff and found blockers:

$problems

Fix each blocker so the path it names works. If a blocker is outside what the issue asks, or needs a tool you do not
have (for example a check in use you cannot run), change nothing for it and say why. Never leave a security or data
loss blocker for that reason.

Shell commands can be refused. Do not get around a refusal with another interpreter or shell command. If a check
cannot run, say so on its line and do not report it as passed.

The reviewer also left notes. Fix a note only when the fix is small and inside the issue; leave the rest.

$notes

After your last edit, rerun every check that edit could affect, in the foreground, and read its output as well as its
exit code. Name in your summary any check you did not rerun and why.

Do not expand scope beyond the issue. End with one line per blocker id: `B1 fixed: how` or `B1 left open: why`. The
next reviewer reads it.
