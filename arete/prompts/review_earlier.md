## Earlier review

The last review found these blockers:

$blockers

The builder then changed the code and replied:

$reply

What changed since that review:

```diff
$fixed
```

Check these first:

1. Each earlier blocker. In `earlier`, give its id, a `status` and a short `why`: `fixed` when its path works now,
   `open` when it does not, `waived` only when fixing it is outside what the issue asks or outside what the builder's
   tools can do. A `security` or `data_loss` blocker is never waived. Do not list earlier blockers again in
   `problems`.
2. The change since that review, for regressions. Something it broke is a `regression` blocker in `problems`.
3. Then the rest of the change. A new problem blocks only when it meets one of the four tests; otherwise it is a note.
