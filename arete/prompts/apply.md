Apply these harness changes to the repository in your working directory. Each one was mined from real
sessions where an agent needed correcting. Make each change exactly where it says, in the file's existing
voice and format, as briefly as it can be said.

$proposals

Rules:

- Only touch the files named. Never touch settings.json, hooks, CI, or code.
- Before adding a line, check the file does not already say it; if it does, sharpen the existing line instead.
- If a target file does not exist and the change is a skill, create `SKILL.md` with the usual frontmatter
  (name, description) and a short body.
- If a change no longer makes sense once you see the file, skip it and say why.
- Do not run git. Finish with one line per proposal: applied or skipped, and why if skipped.
