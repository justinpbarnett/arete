"""Checks, verification retries and publication measured from completed orchestrator actions."""

import statistics

from .. import util
from ._rows import json_list
from .db import reader


def check_verification(since: str, connect=reader) -> dict:
    # Include earlier receipts from active lineages so a resume or the window boundary
    # cannot turn an existing failure into a new incident.
    sql = """
        SELECT a.root, a.at, a.op, r.pr_url,
               json_extract(a.result, '$.ending') AS ending,
               json_extract(a.result, '$.passed') AS passed,
               coalesce(json_extract(a.result, '$.scope'),
                        CASE WHEN json_extract(a.result, '$.full') THEN 'full' END,
                        json_extract(a.request, '$.scope'), 'full') AS scope,
               coalesce(json_array_length(a.result, '$.checks_executed'),
                        json_extract(a.result, '$.report') LIKE '### %') AS checked,
               json_extract(a.result, '$.reused_checks') AS reused_checks,
               json_extract(a.result, '$.verdict') AS verdict,
               json_extract(a.result, '$.reused_verification') AS reused_verify,
               json_extract(a.result, '$.retry_blocked') AS suppressed,
               json_extract(a.result, '$.input_stamp.tree') AS tree,
               json_extract(a.result, '$.input_stamp.policy') AS policy,
               json_extract(a.result, '$.input_stamp.acceptance') AS acceptance,
               json_extract(a.result, '$.verification_environment') AS environment,
               json_extract(a.result, '$.verification_requirements') AS requirements,
               json_extract(a.result, '$.missing_capability') AS capability,
               json_extract(a.result, '$.missing_tools') AS missing_tools
        FROM orchestration_actions a JOIN runs r ON r.id = a.run
        WHERE r.kind = 'pipeline' AND r.status != 'dry-run' {dry_filter} AND a.state = 'succeeded'
          AND a.op IN ('checks.run','verification.run','publication.request')
          AND a.root IN (SELECT root FROM orchestration_actions WHERE at >= ? AND state = 'succeeded'
                         AND op IN ('checks.run','verification.run','publication.request'))
        ORDER BY a.root, a.at, a.rowid
    """
    with connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='orchestration_actions'").fetchone()
        if not exists:
            return summarize((), since)
        columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
        dry_filter = "AND r.dry_run = 0" if "dry_run" in columns else ""
        return summarize(db.execute(sql.format(dry_filter=dry_filter), (since,)), since)


def names(raw) -> tuple[str, ...]:
    return tuple(sorted(name.strip().casefold() for name in json_list(raw) if isinstance(name, str)))


def summarize(rows, since: str) -> dict:
    previous_blocks, failures, repair_seconds, published_prs = {}, {}, [], set()
    blocked_retries = suppressed = unknown_blocks = full_suites = 0
    for row in rows:
        root, at = row["root"], row["at"]
        in_window = at >= since
        if row["op"] == "publication.request":
            if in_window and row["ending"] == "pr":
                published_prs.update((row["pr_url"] or "").split())
        elif row["op"] == "checks.run":
            if row["reused_checks"] or not row["checked"] or row["passed"] is None:
                continue
            if in_window and row["scope"] == "full":
                full_suites += 1
            if not row["passed"]:
                failures.setdefault(root, at)
            elif row["scope"] in ("failed", "full") and root in failures:
                first_failed = failures.pop(root)
                if in_window:
                    repair_seconds.append((util.parse_iso(at) - util.parse_iso(first_failed)).total_seconds())
        elif row["reused_verify"]:
            if in_window and row["suppressed"]:
                suppressed += 1
        else:
            scope = (root, names(row["requirements"]))
            if row["verdict"] != "blocked":
                previous_blocks.pop(scope, None)
                continue
            blocker = names(row["missing_tools"]) or (row["capability"] or "").strip().casefold()
            inputs = (row["tree"], row["policy"], row["acceptance"], row["environment"], blocker)
            if not all(inputs):
                previous_blocks.pop(scope, None)
                if in_window:
                    unknown_blocks += 1
                continue
            if in_window and previous_blocks.get(scope) == inputs:
                blocked_retries += 1
            previous_blocks[scope] = inputs
    return {
        "repeated_blocked_verifications": blocked_retries,
        "suppressed_blocked_retries": suppressed,
        "unclassified_blocked_verifications": unknown_blocks,
        "full_suite_runs": full_suites,
        "published_prs": len(published_prs),
        "full_suite_runs_per_pr": round(full_suites / len(published_prs), 2) if published_prs else None,
        "median_repair_seconds": statistics.median(repair_seconds) if repair_seconds else None,
        "completed_repairs": len(repair_seconds),
        "open_check_failures": len(failures),
    }
