The security floor is incomplete until the protected companion edit in `guard.patch` is applied to `arete/guard.py`. The patch uses the `apply_patch` format. It supplies the shared login lists, Floor, launch checks, Claude hook settings and unconditional path/Keychain checks consumed by this build. No permissive fallback has been added for the missing APIs.

The builder did not change `arete/guard.py`, as required by the ticket's protected-file constraint. An owner with authority to edit that file must apply the patch, review it and resume the build before this change can be verified or merged.

All five required check commands were attempted. Each exited before running because line 2 of the supplied wrapper could not enter the worktree: `Not a directory`. Arete's subsequent checks step must run behavior, compile, imports, lint and web. The builder's `git diff --check` passed. The requested `git diff origin/main...HEAD` was inspected; there are no builder commits, so the pending work was also reviewed with `git diff`.

After applying the companion edit, run the ticket's isolated product probes, including actual Claude and Codex hooks, Pi tool handlers, replay launch refusals, chat/history commands and macOS confinement outside a nested sandbox. A nested `sandbox_apply` refusal is an unavailable probe, not evidence of confinement.
