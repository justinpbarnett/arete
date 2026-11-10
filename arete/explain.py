from __future__ import annotations

import textwrap

from . import config, emits, paths, pipeline, runner, triage

SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
LONGEST_SUMMARY = 300
BRIEFING_SHOWN = 20000
TIMEOUT_SECONDS = 120


class NoSummary(RuntimeError):
    pass


def plain_summary(approval: dict, cfg: config.Config) -> str:
    emit, item = emits.EMITS[approval["emit"]["type"]], approval["emit"]["item"]
    text = pipeline.prompt("explain", loop=approval["loop"], repo=emits.repo_of(approval) or "none",
                           action=approval["summary"], briefing=emit.briefing(item)[:BRIEFING_SHOWN])
    res = runner.run(text, paths.STATE_DIR, model=cfg.model("explain"), tools=[], mcp_config=triage.NO_MCP_SERVERS,
                     json_schema=SCHEMA, persist=False, timeout=TIMEOUT_SECONDS, wait_out_limits=False,
                     trace=("", f"plain summary #{approval['id']}"), cfg=cfg)
    said = res.structured.get("summary") if res.ok and isinstance(res.structured, dict) else ""
    summary = textwrap.shorten(str(said or ""), LONGEST_SUMMARY, placeholder=" …")
    if not summary:
        raise NoSummary(res.error or "the answer held no summary")
    return summary
