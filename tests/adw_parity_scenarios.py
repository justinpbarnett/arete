"""Deterministic replay scenarios for end-to-end ADW parity testing (#299)."""
from __future__ import annotations

import json
from pathlib import Path

from arete import replay, run_lines, util
from arete.store import runs as run_store

SCENARIOS = (
    "pass",
    "failed-check",
    "missing-verification",
    "review-converges",
    "review-fix-breaks-check",
    "verify-fix-breaks-check",
    "review-notes-only",
    "review-notes-dry-run",
)

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "adw_replay"

_KEPT_LABELS = ("runner", "model", "effort", "tier", "kind", "auth_mode", "atom", "ab", "ab_side")


def _write_json(path: Path, body: dict) -> None:
    path.write_text(json.dumps({**body, **replay.SAMPLE_USAGE}, indent=2) + "\n")


def _make_patch(repo: Path, content: str) -> str:
    notes = repo / replay.SAMPLE_FILE
    saved = notes.read_text()
    try:
        notes.write_text(content)
        return util.git(["diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/"], repo)
    finally:
        notes.write_text(saved)


def _make_patch_from(repo: Path, before: str, after: str) -> str:
    notes = repo / replay.SAMPLE_FILE
    saved = notes.read_text()
    try:
        notes.write_text(before)
        util.git(["add", replay.SAMPLE_FILE], repo)
        notes.write_text(after)
        return util.git(["diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/"], repo)
    finally:
        util.git(["reset", "-q", "HEAD", "--", replay.SAMPLE_FILE], repo)
        notes.write_text(saved)


def setup_scenario(root: Path, scenario: str) -> tuple[Path, Path, Path, list[str]]:
    """Build an isolated replay.sample workspace for one scenario.

    Returns (repo_dir, layer_dir, answers_dir, extra_cli_flags).
    """
    sample_dir = root / "sample"
    replay.sample(sample_dir, adw=True)
    repo = sample_dir / "repo"
    layer = sample_dir / "layer"
    answers = sample_dir / "replay"
    flags: list[str] = []

    base_text = replay.SAMPLE_TEXT
    good_text = f"{base_text}{replay.SAMPLE_WORD}\n"
    good2_text = f"{good_text}second note\n"
    broken_text = f"{base_text}broken\n"
    repaired_text = f"{broken_text}{replay.SAMPLE_WORD}\n"

    if scenario == "pass":
        pass
    elif scenario == "failed-check":
        (answers / "build.patch").write_text(_make_patch(repo, broken_text))
        for name in ("fix2", "fix3"):
            _write_json(answers / f"{name}.json", {"text": "Tried fixing."})
    elif scenario == "missing-verification":
        (layer / "config.toml").write_text(
            f'[checks]\nword = {json.dumps(replay.SAMPLE_CHECK)}\n\n'
            '[git]\nbase = "main"\n\n'
            '[factory]\npath = "adw"\n'
        )
    elif scenario == "review-converges":
        _write_json(answers / "review.json", {
            "text": "Add a second note line.",
            "structured": {
                "summary": "Needs a second note.",
                "problems": [{
                    "severity": "blocker",
                    "file": replay.SAMPLE_FILE,
                    "problem": "Add a second note.",
                    "verification": "review",
                    "acceptance": "notes.txt includes a second note line.",
                }],
            },
        })
        _write_json(answers / "review_fix.json", {"text": "Added second note."})
        (answers / "review_fix.patch").write_text(_make_patch_from(repo, good_text, good2_text))
        _write_json(answers / "review2.json", {
            "text": "Fixed and approved.",
            "structured": {
                "summary": "Approved after fix.",
                "problems": [],
                "earlier": [{"id": "B1", "status": "fixed", "why": "Added second note line."}],
            },
        })
        _write_json(answers / "verify2.json", replay.SAMPLE_ANSWERS["verify"])
    elif scenario == "review-fix-breaks-check":
        _write_json(answers / "review.json", {
            "text": "Adjust the note.",
            "structured": {
                "summary": "Adjust the note.",
                "problems": [{
                    "severity": "blocker",
                    "file": replay.SAMPLE_FILE,
                    "problem": "Adjust the note.",
                    "verification": "review",
                    "acceptance": "notes.txt includes the adjusted note.",
                }],
            },
        })
        _write_json(answers / "review_fix.json", {"text": "Edited note."})
        (answers / "review_fix.patch").write_text(_make_patch_from(repo, good_text, broken_text))
        _write_json(answers / "check_repair1_2.json", {"text": "Restored kumquat."})
        (answers / "check_repair1_2.patch").write_text(_make_patch_from(repo, broken_text, repaired_text))
        _write_json(answers / "review2.json", {
            "text": "Fixed and approved.",
            "structured": {
                "summary": "Approved after check repair.",
                "problems": [],
                "earlier": [{"id": "B1", "status": "fixed", "why": "Adjusted note and restored kumquat."}],
            },
        })
        _write_json(answers / "verify2.json", replay.SAMPLE_ANSWERS["verify"])
    elif scenario == "verify-fix-breaks-check":
        (layer / "config.toml").write_text(
            '[limits]\nmax_repair_cycles = 6\n\n'
            f'[checks]\nword = {json.dumps(replay.SAMPLE_CHECK)}\n\n'
            '[git]\nbase = "main"\n\n'
            f'[verify]\nnotes = {json.dumps(replay.SAMPLE_NOTES)}\n\n'
            '[factory]\npath = "adw"\n'
        )
        _write_json(answers / "verify.json", {
            "text": "Check failed.",
            "structured": {
                "verdict": "fail",
                "summary": "Output format needs a fix.",
                "checks": [{"what": "format", "how": "grep", "saw": "wrong", "passed": False}],
            },
        })
        _write_json(answers / "verify_fix.json", {"text": "Adjusted file during verify."})
        (answers / "verify_fix.patch").write_text(_make_patch_from(repo, good_text, broken_text))
        _write_json(answers / "check_repair.json", {"text": "Restored kumquat after verify_fix."})
        (answers / "check_repair.patch").write_text(_make_patch_from(repo, broken_text, repaired_text))
        _write_json(answers / "verify2.json", replay.SAMPLE_ANSWERS["verify"])
        _write_json(answers / "review2.json", replay.SAMPLE_ANSWERS["review"])
        _write_json(answers / "verify3.json", replay.SAMPLE_ANSWERS["verify"])
    elif scenario in ("review-notes-only", "review-notes-dry-run"):
        _write_json(answers / "review.json", {
            "text": "Approved with minor note.",
            "structured": {
                "summary": "Approved with a minor note.",
                "problems": [{"severity": "minor", "file": replay.SAMPLE_FILE, "problem": "Nice concise change."}],
            },
        })
        if scenario == "review-notes-dry-run":
            flags.append("--dry-run")
    else:
        raise ValueError(f"unknown scenario {scenario}")

    return repo, layer, answers, flags


def normalize_run(run_id: str, tid: str = "") -> dict:
    row = run_store.run_get(run_id)
    assert row is not None, f"missing run {run_id}"
    events = []
    for line in run_lines.read(run_id):
        ev = line.get("event")
        if ev == "step_start":
            entry = {"event": "step_start", "step": line.get("step"), "try": line.get("try")}
            for label in _KEPT_LABELS:
                if label in line:
                    entry[label] = line[label]
            events.append(entry)
        elif ev == "step_end":
            entry = {
                "event": "step_end",
                "step": line.get("step"),
                "try": line.get("try"),
                "result": line.get("result"),
                "ok": line.get("ok"),
            }
            for label in _KEPT_LABELS:
                if label in line:
                    entry[label] = line[label]
            events.append(entry)
    step_names = [s["step"] for s in run_store.steps(run_id)]
    summary = str(row["summary"] or "")
    if tid:
        summary = summary.replace(tid, "<tid>")
    return {
        "status": row["status"],
        "summary": summary,
        "steps": step_names,
        "events": events,
    }
