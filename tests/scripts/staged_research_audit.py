
import json, os, sys, tempfile, time
from pathlib import Path
from arete import config, paths, research, research_audit, runner, steps
from arete.store import db as foundation
root = Path(sys.argv[1])
replay = root / "replay"
paths.ensure_dirs()
foundation.migrate()
calls = []
real = runner.run
def spy(*args, **kwargs):
    calls.append([kwargs["trace"][1], list(kwargs.get("tools") or []), list(kwargs.get("allowed_tools") or [])])
    return real(*args, **kwargs)
runner.run = spy
def put(name, structured, cost=0):
    (replay / f"{name}.json").write_text(json.dumps({"structured": structured, "cost_usd": cost}))
OLD = "As of March 2021 the limit is 10 requests per second."
NEW = "The limit was raised to 20 requests per second in June 2025."
BETA = "Customers reported occasional throttling during the beta."
def src(url, published, passage):
    return {"question": "What is the limit?", "url": url, "title": "Page", "published": published,
            "event_date": "", "passage": passage, "uncertainty": "none"}
def task(goal):
    return {"goal": goal, "queries": ["general terms"], "max_sources": 3}
def concl(claim, sid, critical=False):
    return {"claim": claim, "sources": [sid], "critical": critical}
def chk(c, verdict, sources, quote="", note="", correction=""):
    return {"claim": c, "verdict": verdict, "sources": sources, "quote": quote, "fetched": [], "note": note,
            "correction": correction}
def batch1(c3_quote=BETA):
    return {"checks": [
        chk("C1", "contradicted", ["S1", "S2"], NEW, "The 2021 page is outdated and the June 2025 page states 20.",
            "The limit was 10 requests per second as of March 2021, and a June 2025 page states 20 [S1] [S2]."),
        chk("C2", "unverifiable", ["S3"], "", "The passage reports throttling during the beta and says nothing of a fix."),
        chk("C3", "supported", ["S3"], c3_quote),
        chk("C4", "inference", ["S2"], "", "The page states one raise and announces no further one."),
        chk("C5", "supported", ["S2"], NEW)],
        "disagreements": [{"issue": "The 2021 page states 10 requests per second and the 2025 page states 20.",
                           "sources": ["S1", "S2"]}]}
BATCH2 = {"checks": [chk("C6", "supported", ["S2"], NEW)], "disagreements": []}
def seed(ref, synthesis=None):
    put(f"research-{ref}-plan", {"needs_search": True, "covered_by": [], "questions": ["What is the limit?"],
        "tasks": [task("one"), task("two"), task("three")], "source_criteria": ["primary"],
        "claims": ["The limit is known"]})
    put(f"research-{ref}-task-1-search", {"sources": [src("https://example.org/old", "2021-03-01", OLD),
                                                      src("https://example.net/new", "2025-06-01", NEW)], "notes": ""})
    put(f"research-{ref}-task-2-search", {"sources": [], "notes": ""})
    put(f"research-{ref}-task-3-search", {"sources": [src("https://example.com/beta", "2024-11-05", BETA)], "notes": ""})
    put(f"research-{ref}-synthesis", synthesis or {"summary": "The limit is 10 requests per second.", "name": "",
        "answer": "The limit is 10 requests per second [S1].",
        "conclusions": [concl("The limit is 10 requests per second.", "S1", True),
                        concl("Throttling is fully resolved.", "S3", True),
                        concl("Customers reported throttling during the beta.", "S3"),
                        concl("The vendor will raise the limit again.", "S2"),
                        concl("The limit was raised in June 2025.", "S2")],
        "inferences": [{"claim": "Capacity planning should assume 20 requests per second.",
                        "basis": "The newer page states 20.", "sources": ["S2"], "critical": False}],
        "contradictions": [], "gaps": [], "searches": []})
def drive(ref, cfg):
    os.environ["ARETE_RUN_ID"] = f"run-{ref}"
    values = {"ref": ref, "title": "Invented", "ticket": "Find the limit.", "related": "none", "repos": "none"}
    start = len(calls)
    with tempfile.TemporaryDirectory() as work:
        res, paid = research.staged(cfg, ref, values, research.filing(None, ""), work, [], [],
                                    time.monotonic(), False)
    evidence = research.research_search.evidence_file(ref)
    return {"ok": res.ok, "error": res.error, "structured": res.structured, "calls": calls[start:],
            "evidence": json.loads(evidence.read_text()) if evidence.exists() else {}}
cfg = config.load()
out = {}
seed("A")
put("research-A-batch-1-audit", batch1("Throttling was fully resolved during the beta."))
put("research-A-batch-1-audit2", batch1())
put("research-A-batch-2-audit", BATCH2)
out["a"] = drive("A", cfg)
seed("B")
put("research-B-batch-1-audit", batch1(), 4.996)
put("research-B-batch-2-audit", BATCH2)
out["b"] = drive("B", cfg)
seed("C")
for name in ("research-C-batch-1-audit", "research-C-batch-1-audit2"):
    put(name, {"checks": [{"claim": "C1", "verdict": "fine"}]})
put("research-C-batch-2-audit", BATCH2)
out["c"] = drive("C", cfg)
seed("D")
cfg.research_audit = False
out["d"] = drive("D", cfg)
cfg.research_audit = True
seed("E", {"summary": "The limit was raised in June 2025 and the vendor guarantees 99.99 percent uptime.", "name": "",
           "answer": "The vendor guarantees 99.99 percent uptime [S2].",
           "conclusions": [concl("The limit was raised in June 2025.", "S2", True),
                           concl("Customers reported throttling during the beta.", "S3")],
           "inferences": [], "contradictions": [], "gaps": [], "searches": []})
put("research-E-batch-1-audit", {"checks": [chk("C1", "supported", ["S2"], NEW), chk("C2", "supported", ["S3"], BETA)],
                                 "disagreements": []})
out["e"] = drive("E", cfg)
s = steps.resolve(config.load(), None, "research_audit")
known = {f"S{n}": {"url": u} for n, u in enumerate(
    ("https://localhost/a", "https://example.org/b", "https://10.0.0.1/c", "https://exa_mple.org/d",
     "https://foo.local/e"), 1)}
out["hosts"] = research_audit.hosts(s, [{"cited": list(known)}], known)
with paths.CONFIG_FILE.open("a") as f:
    f.write('\n[steps.research_audit]\ntools = ["Read", "Glob", "Grep", "WebSearch"]\n')
try:
    steps.resolve(config.load(), None, "research_audit")
    out["web"] = ""
except ValueError as e:
    out["web"] = str(e)
print("RESULT" + json.dumps(out))
