A reviewer read your diff and found blockers:

$problems

Fix each blocker so the path it names works. If a blocker is outside what the issue asks, or needs a tool you do not
have (for example a check in use you cannot run), change nothing for it and say why. Never leave a security or data
loss blocker for that reason.

The reviewer also left notes. Fix a note only when the fix is small and inside the issue; leave the rest.

$notes

Do not expand scope beyond the issue. End with one line per blocker id: `B1 fixed: how` or `B1 left open: why`. The
next reviewer reads it.
