---
schema: 1
inputs: [attempt, checks, denied, failures, max_attempts]
---
The checks failed after your change. Attempt $attempt of $max_attempts.

$failures

Denied paths: $denied.

Wait for every check you started to finish and read its result before you finish. Do not wait with chained sleeps, and
do not end your turn while a check is still running.

Fix the cause, not the symptom. A refused command or a sandbox limit is not a product bug: do not change product code
to get past it, such as by catching the error. Do not weaken, skip, or delete tests to make them pass.
Configured checks: $checks. Report exactly what you ran and its result.
Then finish with a one paragraph summary of the fix.
