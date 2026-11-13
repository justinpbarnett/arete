from __future__ import annotations

import re
import secrets
import textwrap

from . import config, delivery, paths, pipeline, runner, state

SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
MOST_SENTENCES = 2
MOST_WORDS = 40
LONGEST_SUMMARY = 300
BRIEFING_SHOWN = 20000
TIMEOUT_SECONDS = 120
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
PATH_OR_COMMAND = re.compile(r"`|(?:^|\s)~?/\w|\w/[^\s/]+/")


class NoSummary(RuntimeError):
    pass


def write_plain_summary(approval: dict, cfg: config.Config) -> str:
    state.set_approval_note(approval["id"], "plain_summary", plain_summary(approval, cfg))
    return "plain summary written"


def plain_summary(approval: dict, cfg: config.Config) -> str:
    item = approval["emit"]["item"]
    text = pipeline.prompt("explain", loop=approval["loop"], repo=delivery.repo_of(approval) or "none",
                           mark=secrets.token_hex(8), action=approval["summary"],
                           briefing=delivery.emit_of(approval).briefing(item)[:BRIEFING_SHOWN])
    res = runner.run(text, paths.STATE_DIR, model=cfg.model("explain"), tools=[],
                     json_schema=SCHEMA, persist=False, timeout=TIMEOUT_SECONDS, wait_out_limits=False,
                     trace=("", f"plain summary #{approval['id']}"), cfg=cfg,
                     repo=delivery.repo_for(approval, cfg))
    said = str(res.structured.get("summary") or "").strip() if res.ok and isinstance(res.structured, dict) else ""
    if not said:
        raise NoSummary(res.error or "the answer held no summary")
    if PATH_OR_COMMAND.search(said):
        raise NoSummary(f"the summary named a path or a command: {said}")
    return within_limits(said)


def within_limits(said: str) -> str:
    first, *rest = SENTENCE_END.split(" ".join(said.split()))[:MOST_SENTENCES]
    kept = first
    for sentence in rest:
        if not fits_limits(f"{kept} {sentence}"):
            break
        kept = f"{kept} {sentence}"
    return kept if fits_limits(kept) else cut_to_limits(kept)


def fits_limits(text: str) -> bool:
    return len(text.split()) <= MOST_WORDS and len(text) <= LONGEST_SUMMARY


def cut_to_limits(text: str) -> str:
    return textwrap.shorten(" ".join(text.split()[:MOST_WORDS]) + " …", LONGEST_SUMMARY, placeholder=" …")
