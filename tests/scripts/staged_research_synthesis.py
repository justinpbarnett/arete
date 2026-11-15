
import json, os, sys, time
from pathlib import Path
from arete import config, paths, research, runner, steps
root = Path(sys.argv[1])
replay = root / "replay"
paths.ensure_dirs()
calls = []
real = runner.run
def spy(*args, **kwargs):
    calls.append([kwargs["trace"][1], list(kwargs.get("tools") or [])])
    return real(*args, **kwargs)
runner.run = spy
def put(name, structured, cost=0):
    (replay / f"{name}.json").write_text(json.dumps({"structured": structured, "cost_usd": cost}))
def source(url, claim):
    return {"question": "q", "url": url, "title": "Page " + url[-1], "published": "2025-01-02", "event_date": "",
            "passage": claim, "uncertainty": "none"}
def found(*sources):
    return {"sources": list(sources), "notes": ""}
def task(goal):
    return {"goal": goal, "queries": ["general terms"], "max_sources": 3}
def seed(ref, cost=0, search=True):
    put(f"research-{ref}-plan", {"needs_search": True, "covered_by": [], "questions": ["What is the limit?"],
        "tasks": [task("one"), task("two"), task("three")], "source_criteria": ["primary"],
        "claims": ["The limit is known"]})
    put(f"research-{ref}-task-1-search", found(source("https://example.org/a", "limit is 10"),
                                              source("https://example.net/b", "limit is 20")), cost)
    put(f"research-{ref}-task-2-search", found(), cost)
    put(f"research-{ref}-task-3-search", found(source("https://example.com/c", "limit is documented")), cost)
def answer(**kw):
    base = {"summary": "Short.", "name": "", "answer": "The limit is disputed [S1].", "conclusions": [],
            "inferences": [], "contradictions": [], "gaps": [], "searches": []}
    return {**base, **kw}
def good(**kw):
    return answer(**{"conclusions": [{"claim": "Documented", "sources": ["S3"], "critical": True}], **kw})
def ask(goal):
    return {"goal": goal, "queries": ["more general terms"], "max_sources": 2}
def drive(ref, run_id, cfg):
    os.environ["ARETE_RUN_ID"] = run_id
    values = {"ref": ref, "title": "Invented", "ticket": "Find the limit.", "related": "none", "repos": "none"}
    start = len(calls)
    res, paid = research.staged(cfg, ref, values, research.filing(None, ""), research.REPORTS_DIR, [], [],
                                time.monotonic(), False)
    evidence = research.research_search.evidence_file(ref)
    return {"ok": res.ok, "error": res.error, "structured": res.structured, "paid": paid,
            "calls": calls[start:], "evidence": json.loads(evidence.read_text()) if evidence.exists() else {}}
cfg = config.load()
out = {}
seed("A")
put("research-A-synthesis", good(answer="The limit is 10 [S1]; see [the note](\f//outside.example/).",
                                 conclusions=[{"claim": "Bad", "sources": ["S9"], "critical": True}]))
put("research-A-synthesis2", good(contradictions=[{"issue": "Limit differs", "sources": ["S1", "S2"]}],
                                  searches=[ask("resolve")]))
put("research-A-task-4-search", found(source("https://example.org/a/?utm_source=x", "limit is 10 again"),
                                      source("https://example.org/d", "limit is 15")))
put("research-A-gap-1-synthesis", good(answer="Resolved [S4]. Cited [S1].", conclusions=[
    {"claim": "Found", "sources": ["S3", "S4"], "critical": True}],
    contradictions=[{"issue": "Limit differs", "sources": ["S1", "S2"]}]))
out["a"] = drive("A", "run-a", cfg)
seed("B")
for n in ("synthesis", "gap-1-synthesis", "gap-2-synthesis"):
    put(f"research-B-{n}", good(searches=[ask(f"more {n}")]))
put("research-B-task-4-search", found(source("https://example.org/e", "e")))
put("research-B-task-5-search", found(source("https://example.org/f", "f")))
out["b"] = drive("B", "run-b", cfg)
seed("C", cost=0.9)
put("research-C-synthesis", good(searches=[ask("late")]), 1.298)
out["c"] = drive("C", "run-c", cfg)
seed("D")
put("research-D-report", {"summary": "S", "report": "Plain report."})
cfg.research_synthesis = False
out["d"] = drive("D", "run-d", cfg)
config.CONFIG_FILE = paths.CONFIG_FILE
with paths.CONFIG_FILE.open("a") as f:
    f.write('\n[steps.research_synthesis]\ntools = ["Read", "Glob", "Grep", "WebSearch"]\n')
try:
    steps.resolve(config.load(), None, "research_synthesis")
    out["web"] = ""
except ValueError as e:
    out["web"] = str(e)
out["home"] = os.environ.get("HOME", "")
print("RESULT" + json.dumps(out))
