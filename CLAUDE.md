# arete

- Other agent sessions work in this checkout at the same time, and `arete serve` runs from it and restarts itself whenever a `.py` or dashboard web file changes.
  Before your first edit, move into a worktree of your own (EnterWorktree, or `git worktree add`) and make every change there, never in the main checkout.
- When editing agent prompts, keep them aligned with `VERIFICATION_POLICY` in `arete/prompt_loader.py`.
  It is the shared source of truth for behavior-first verification and when automated tests are warranted.
- Keep live CLI credentials inside the Arete client during dashboard checks. Playwright can print request headers in errors.
  For a render check, read the dashboard with `arete.client.call` and pass only the nonsecret display fields to the browser.
  Exercise browser authentication on an isolated test instance with disposable credentials.
