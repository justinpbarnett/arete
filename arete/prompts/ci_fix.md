Arete committed your change, pushed it and opened the PR. CI then ran on the PR, and these checks failed.
Round $round of $max_rounds.

$failures

For each check, first decide whether this PR caused the failure. CI also fails for reasons outside the PR: a shared
test database or service that drifted, a flaky test, an expired secret, an outage. The base result helps, and its
date matters: a check that passed on the base weeks ago may fail there now. Compare what failed with what the PR
changes: $diff. Run each command on its own, with no pipe or `&&`, since Bash allows only the listed commands.

- Caused by the PR: $when_caused.
- Not caused by the PR: change nothing for that check.
- Caused by the PR, but the only way to pass is to undo what the ticket asked for, or to change the CI setup: change
  nothing for that check, and say so in its reason, so a person can decide.

Answer with one entry per check, named exactly as its heading above. Give each a reason of one or two sentences
that names the evidence, such as the error line and the file it points to. The summary says what you changed, or
that you changed nothing.
