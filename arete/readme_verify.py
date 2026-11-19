from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from . import paths, pi_shell, pilot, record, routing, verify

if TYPE_CHECKING:
    from .pipeline import Run


def observed(run_id: str, filename: str, command: str, excerpt: str) -> bool:
    if not filename or Path(filename).name != filename:
        return False
    calls = [event for event in record.lines(paths.record_dir(run_id) / filename)
             if event.get("type") in {"tool_execution_start", "tool_execution_end"}]
    if [event["type"] for event in calls] != ["tool_execution_start", "tool_execution_end"]:
        return False
    start, end = calls
    return (start.get("toolName") == end.get("toolName") == pi_shell.TOOL
            and bool(start.get("toolCallId")) and start["toolCallId"] == end.get("toolCallId")
            and start.get("args", {}).get("command") == command and end.get("isError") is False
            and end.get("result", {}).get("content") == [{"type": "text", "text": f"{excerpt}\nExit code: 0"}])


def check(run: Run, source: Path, step: str, signal: pilot.Signal, tree: str) -> verify.Verified:
    try:
        text = signal.expected(pilot.readme(source, signal.base))
        pilot.require_exact_tree(source, signal, tree, text)
        stored = pilot.readme(source, tree).encode("utf-8")
        lines = stored.splitlines(keepends=True)
        index = next(i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == signal.after.encode("utf-8"))
        first, last = max(0, index - 3), min(len(lines), index + 4)
        excerpt = b"".join(lines[first:last]).decode("utf-8")
        pilot.require(len(excerpt.encode()) <= 8192, "the README excerpt is too large to verify")
        command = f"/usr/bin/sed -n '{first + 1},{last}p' README.md"
        with tempfile.TemporaryDirectory(prefix="readme-observation-", dir=run.dir) as folder:
            view = Path(folder)
            document = view / pilot.DOC
            document.write_bytes(stored)
            document.chmod(0o400)
            identity = document.stat()
            result = run.agent(step, f"Verify the exact candidate README as a reader. Call `{pi_shell.TOOL}` once "
                               f"with exactly this command and no prefixes or suffixes: `{command}`. "
                               f"Check that the heading is `{signal.after}` and "
                               "the nearby instructions are readable. No service or Git commands are needed. "
                               "Report what you actually observed; if the read fails, return blocked.",
                               view, role="verify", tools=["Bash"], allowed_tools=[f"Bash({command})"],
                               disallowed_tools=routing.NO_GIT_WRITES, json_schema=verify.SCHEMA, persist=False,
                               add_dirs=[], fallback=False)
            actual = document.lstat()
            pilot.require(os.path.samestat(actual, identity) and stat.S_ISREG(actual.st_mode) and actual.st_nlink == 1
                          and document.read_bytes() == stored and list(view.iterdir()) == [document],
                          "the verifier's candidate view changed")
            pilot.require(result.ok and result.runner == "pi" and result.auth_mode == "subscription"
                          and isinstance(result.structured, dict) and observed(run.id, result.record, command, excerpt),
                          "the native Pi subscription verifier did not return the exact read-only observation")
            return verify.Verified.from_agent(result.structured)
    except (pilot.Held, OSError, RuntimeError, ValueError, StopIteration) as error:
        return verify.Verified("blocked", "The exact candidate README could not be verified in use.",
                               finished=False, blocked_by=str(error))
