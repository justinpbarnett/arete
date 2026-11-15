
import json
import sys
from pathlib import Path

from arete import config, paths, research, runner, state, triage, util

root = Path(sys.argv[1])
paths.ensure_dirs()
state.migrate()
cfg = config.load()
calls = []
real = runner.run
def spy(*args, **kwargs):
    calls.append(kwargs["trace"][1])
    return real(*args, **kwargs)
runner.run = spy
def put(name, structured, cost=0):
    (root / "replay" / f"{name}.json").write_text(json.dumps({"structured": structured, "cost_usd": cost}))
for n in range(1, 52):
    put(f"triage-T-{n}", {"kind": "research", "ready": False, "questions": [], "assumptions": [],
        "size": "S", "repos": [], "plan": [], "why": "Find the answer in a report."})
put("research-T-1", {"summary": "The answer.", "report": "A durable report body."}, .75)
def candidate(n, updated=None):
    ref = f"T-{n}"
    return triage.Candidate(ref, ref, f"Title {n}", f"https://example.test/{ref}",
                            updated or f"2026-11-08T20:10:{n:02d}Z")
class FakeSource(triage.JiraSource):
    def candidates(self):
        return sorted(items, key=lambda c: triage.moment(c.updated), reverse=True)[:triage.PER_SOURCE]
    def ticket(self, c):
        return triage.TicketText("Status: Open", "Find the answer and write a detailed report.", "", c.updated)
src = FakeSource("T")
items = [candidate(1)]
active = [src]
triage.sources = lambda cfg: active
triage.publish = lambda *args: "not posted (test)"
def refresh():
    assert triage.refresh(cfg, False) == 0
def tidy():
    with util.lock("research", wait=True):
        research.tidy()
def run(**kwargs):
    with util.lock("research", wait=True):
        return research.research(cfg, triage.board_row("T-1"), **kwargs)[0]
def cli(ref):
    import subprocess
    p = subprocess.run([sys.executable, "-m", "arete.cli", "research", ref],
                       capture_output=True, text=True, timeout=30)
    return [p.returncode, p.stdout + p.stderr]
out = {}
refresh()
out["first"] = run(force=True)
original = state.research_get("T-1")
text = research.report_file("T-1").read_text()
out["initial_cli"] = cli("T-1")
items = [candidate(n) for n in range(1, 52)]
refresh()
with util.lock("research", wait=True):
    state.research_set("T-3", status=research.ANSWERED, answered_by="T-1", error="already answered")
tidy()
out["window"] = {"absent": triage.board_row("T-1") is None,
    "same_row": state.research_get("T-1") == original, "row": state.research_get("T-1"),
    "shown": research.shown("T-1"), "linked": research.shown("T-3"), "text": text,
    "listing": research.listing(), "cli": cli("T-1"), "linked_cli": cli("T-3")}
items = [candidate(1)]
refresh()
out["return"] = {"due": [r["ref"] for r in research.due()], "outcome": run()}
items = [candidate(1, "2026-11-09T20:10:01Z")]
refresh()
out["changed_stamp"] = run()
active = []
refresh()
before_off = state.research_rows()
tidy()
out["off"] = {"board": state.triage_rows(), "same_rows": before_off == state.research_rows(),
              "refs": [r["ref"] for r in state.research_rows()]}
checkpoint = len(calls)
active = [src]
refresh()
out["on"] = {"calls": calls[checkpoint:], "due": [r["ref"] for r in research.due()],
             "shown": research.shown("T-1"), "cli": cli("T-1")}
files = {
    "T-9": "# T-9: Title\n\nhttps://example.test/T-9\n\nResearched 2026-11-08T20:10:55Z for $0.50.\n\n**Summary:** S\n\nOld body.\n",
    "T-10": "# T-10: Current\n\nhttps://example.test/T-10\n\nResearched 2026-11-08T20:10:55Z ($0.40 paid).\n\n**Summary:** New\n\nNew body.\n",
    "T-11": "# T-11: Free\n\nhttps://example.test/T-11\n\nResearched today (nothing paid).\n\nFree body.\n",
    "T-12": "# T-12: Sparse\n\nBody only.\n",
    "notes": "# Notes\n\nNot a report.\n",
    "T-8": "# T-7: Wrong file\n\nBody.\n",
    "T-13": "# Home report title\n\nFiled body.\n",
}
for ref, body in files.items():
    (research.REPORTS_DIR / f"{ref}.md").write_text(body)
with util.lock("research", wait=True):
    state.research_set("T-20", status=research.RUNNING)
tidy()
adopted = {ref: state.research_get(ref) for ref in files}
out["orphans"] = {"files": files, "rows": adopted, "wrong_ref": state.research_get("T-7"),
    "shown": research.shown("T-9"), "running": state.research_get("T-20"),
    "cli": cli("T-9"), "current_cli": cli("T-10"), "unknown_cli": cli("T-99")}
before = state.research_rows()
tidy()
out["idempotent"] = before == state.research_rows()
out["calls"] = calls
print("RESULT" + json.dumps(out))
