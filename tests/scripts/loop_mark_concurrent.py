
root = Path(sys.argv[1])
child = ("import sys, time\nfrom pathlib import Path\nfrom arete import heartbeat\nstop, ready = Path(sys.argv[1]), Path(sys.argv[2])\n"
         "n = 0\nwhile not stop.exists():\n    heartbeat.mark_loop('research', True, 'nothing due')\n    n += 1\n"
         "    if n == 3: ready.write_text('x')\n")
trials = []
for i in range(20):
    stop, ready = root / f"stop{i}", root / f"ready{i}"
    research = subprocess.Popen([sys.executable, "-c", child, str(stop), str(ready)])
    try:
        deadline = time.time() + 60
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.01)
        poll = subprocess.run([sys.executable, "-c", "from arete import heartbeat\nheartbeat.mark_loop('poll', False, 'gh: HTTP 502')"])
        time.sleep(0.05)
    finally:
        stop.write_text("x")
        code = research.wait(timeout=60)
    marks = state.loop_marks()
    shown = {c["name"]: c for c in board.checks()["loops"]}
    trials.append([poll.returncode, code, marks["poll"]["ok"], marks["poll"]["detail"], marks["research"]["ok"],
                   shown["poll"]["ok"]])
    state.set_loop_state("poll", mark_ok=None, mark_detail="", mark_at=None)
print("RESULT:" + json.dumps({"trials": trials, "legacy": (paths.STATE_DIR / "loops.json").exists()}))
