The checks failed after your change. Attempt $attempt of $max_attempts.

$failures

Wait for every check you started to finish and read its result before you finish. Do not wait with chained sleeps, and
do not end your turn while a check is still running.

Fix the cause, not the symptom. A refused command or a sandbox limit is not a product bug: do not change product code
to get past it, such as by catching the error. Report it as blocked and name the command a human must run. Do not weaken, skip, or delete tests to make them pass.
Rerun the failing check yourself with one of these: $checks. Then finish with a one paragraph summary of the fix.
