---
schema: 1
inputs: [attempt, checks, denied, failures, max_attempts]
---
A verifier used the product with your change, and it does not work yet. Attempt $attempt of $max_attempts.

$failures

Denied paths: $denied.

Fix the cause in the code so the user gets what the issue asks for. Do not change what the verifier checks, and
do not add tests to stand in for the product working.
Arete runs the full check gate after your repair: $checks.
Do not duplicate the full suite here.
Use a focused user-level reproduction when helpful and report its real result.
Wait for every check you start to finish and read its output before you finish. Do not wait with chained sleeps, and do not end your turn while a check is still running.
Finish with a short summary of the fix and what the user will now see.
