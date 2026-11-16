
import dataclasses, os, re
from datetime import UTC, datetime
T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
GOOD = {"ok": True, "structured": {"proposals": []}}
BAD = {"ok": False, "error": "model call failed"}
def at(h):
    return T0 + timedelta(hours=h)
def mark(h):
    return util.iso(at(h) - timedelta(minutes=2))
def reply(answer):
    (root / "sample/replay/mine.json").write_text(json.dumps(answer))
reply(GOOD)
def correction(when, label):
    folder = paths.CLAUDE_PROJECTS_DIR / "proj"
    folder.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "timestamp": util.iso(when - timedelta(minutes=1)), "cwd": "/tmp/x",
             "message": {"content": "Fix the header"}},
            {"type": "user", "timestamp": util.iso(when), "cwd": "/tmp/x",
             "message": {"content": f"No, I told you never to touch the footer [tag:{label}]"}}]
    f = folder / f"s{label}.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    os.utime(f, (when.timestamp(), when.timestamp()))
def event(when):
    paths.EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with paths.EVENTS_FILE.open("a") as f:
        f.write(json.dumps({"ts": util.iso(when), "kind": "tool", "cls": "error", "tool": "Bash", "detail": "x"}) + "\n")
def both(h):
    correction(at(h), f"{h:g}")
    event(at(h))
def failed_run(when, tag):
    with mock.patch.object(util, "now", return_value=when):
        rid = record.start("pipeline", tag=tag, repo="replay-sample", issue=tag)
        call(rid, "build", ok=False, error="nope")
        record.settle(rid, "failed", summary=tag)
    return rid
def mine(h, hours=None, dry_run=False, apply=False):
    out = io.StringIO()
    with mock.patch.object(util, "now", return_value=at(h)), contextlib.redirect_stdout(out):
        code = miner.run(hours=hours, dry_run=dry_run, apply=apply)
    text = out.getvalue()
    found = re.search(r"miner ([^\s:]+): \d+ sessions", text)
    prompt = (paths.RUNS_DIR / found.group(1) / "prompt.md").read_text() if found else ""
    row = {} if dry_run else next(iter(state.runs(kind="miner", limit=1)), {})
    return {"code": code, "out": text, "prompt": prompt, "status": row.get("status"),
            "mined_until": row.get("mined_until"), "summary": row.get("summary"),
            "heard": sorted(re.findall(r"\[tag:([^\]]+)\]", prompt)),
            "events": sum(int(n) for n in re.findall(r"^- (\d+) x none tool/error", prompt, re.M))}
def nums(m):
    return sorted(float(x) for x in m["heard"])
def slim(m):
    return {k: v for k, v in m.items() if k != "prompt"}
