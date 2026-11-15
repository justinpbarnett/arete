from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arete import config, control, delivery, loop_run, loops, mergecheck, paths, record, run_lines, sources, state, util

ROOT = Path(__file__).resolve().parents[1] / ".arete-sandbox" / "merge-check-fixture"


def fixture_loop(cfg):
    loop = loops.load(loops.BUNDLED_LOOPS_DIR / "merge-check", "arete", "", cfg)
    source = {**loop.sources[0], "command": control.command(["merged", "--since-cursor"])}
    return replace(loop, dir=ROOT / "loop", sources=[source])


def setup():
    if ROOT.exists():
        raise ValueError(f"{ROOT} already exists; use a fresh disposable workspace")
    if paths.CONFIG_DIR.parent != Path.cwd() / ".arete-sandbox":
        raise ValueError("run only through the documented sandbox prefix")
    ROOT.mkdir(parents=True)
    (ROOT / "loop").mkdir()
    for name in ("fixture", "fixture-two"):
        repo, layer = ROOT / name, ROOT / (name + "-layer")
        repo.mkdir()
        layer.mkdir()
        util.git(["init", "-q", "-b", "main"], repo)
        (repo / "version").write_text("local-old\n")
        (repo / "product.py").write_text(
            "import http.server, pathlib, sys\n"
            "print('START ' + str(pathlib.Path.cwd()), flush=True)\n"
            "http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), "
            "http.server.SimpleHTTPRequestHandler).serve_forever()\n")
        util.git(["add", "-A"], repo)
        util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-qm", "local old base"], repo)
        origin = ROOT / (name + "-origin.git")
        util.git(["init", "-q", "--bare", "-b", "main", str(origin)], ROOT)
        util.git(["remote", "add", "origin", str(origin)], repo)
        (repo / "version").write_text("origin-new\n")
        util.git(["add", "version"], repo)
        util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-qm", "merged base"], repo)
        util.git(["push", "-q", "origin", "main"], repo)
        util.git(["reset", "--hard", "HEAD~1"], repo)
        (layer / "config.toml").write_text(
            '[git]\nbase = "main"\n[verify]\n'
            f'start = {json.dumps(f"{sys.executable} product.py {{port}} ")}\n'
            'url = "http://127.0.0.1:{port}"\nready = "version"\n')
    answers = ROOT / "answers"
    answers.mkdir()
    text = f'[replay]\nall = true\ndir = {json.dumps(str(answers))}\n[serve.queues]\nmerge-check = 1\n'
    for name in ("fixture", "fixture-two"):
        text += (f'[[repo]]\nname = "{name}"\npath = {json.dumps(str(ROOT / name))}\n'
                 f'layer = {json.dumps(str(ROOT / (name + "-layer")))}\n'
                 f'github = "offline/{name}"\npipeline = true\ntracker = "local"\njev = false\n')
    paths.CONFIG_FILE.write_text(text)
    shutil.copyfile(loops.BUNDLED_LOOPS_DIR / "merge-check" / "loop.toml", ROOT / "loop" / "loop.toml")
    print(ROOT)


def add(args):
    stamp = args.at or util.iso()
    run_id = args.id or f"fixture-{args.repo}-{args.pr}"
    record.start("pipeline", run_id=run_id, repo=args.repo, issue=str(args.pr),
                 pr_url="" if args.kind == "no-pr" else f"https://github.com/offline/{args.repo}/pull/{args.pr}")
    record.settle(run_id, "pr" if args.kind == "open" else "merged", finished=stamp)
    if args.kind == "nonpipeline":
        with state.db() as c:
            c.execute("UPDATE runs SET kind = 'loop' WHERE id = ?", (run_id,))
    folder = paths.RUNS_DIR / run_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "verify.md").write_text(
        "**pass**: PRIVATE_OLD_SUMMARY\n\n- **Passed**: Read version\n"
        "  - How: Open /version and check that it reads origin-new.\n  - Saw: PRIVATE_OLD_SAW\n")
    (folder / "diff.patch").write_text(f"--- a/{args.file}\n+++ b/{args.file}\n@@ -1 +1 @@\n-old\n+new\n")
    if args.continues:
        run_lines.write(run_id, "route", continues=args.continues)
        (folder / "verify.md").unlink()
        (folder / "diff.patch").unlink()
    print(run_id)


def pending(cfg, repo):
    rows = [r for r in mergecheck.merged(cfg) if r["repo"] == repo]
    checked = mergecheck.read_json(ROOT / "loop" / ".state" / "checked.json", {}).get(repo, [])
    return rows, [r for r in rows if r["key"] not in checked]


def answer(args, cfg):
    rows, batch = pending(cfg, args.repo)
    p = mergecheck.plan(cfg, cfg.repo(args.repo), rows, batch, "fixture-plan")
    checks = [{"what": f"{n}. {s.name}", "how": "PRIVATE_HOW", "saw": "PRIVATE_SAW_PAGE",
               "passed": args.verdict != "fail"} for n, s in enumerate(p.steps, 1)]
    if args.verdict == "incomplete":
        checks = checks[:-1]
    if args.verdict == "unknown":
        checks[0]["what"] = "999. invented"
    result = {"seconds": args.seconds, "tokens_in": 10, "tokens_out": 10,
              "structured": {"verdict": "fail" if args.verdict == "fail" else "pass",
                             "summary": "PRIVATE_SUMMARY", "checks": checks}}
    if args.verdict == "limit":
        result["tool_calls"] = [{"name": "Read", "input": {"path": "version"}} for _ in range(45)]
        result["stream"] = [{"tokens_in": 10, "tokens_out": 10}]
    if args.verdict == "unavailable":
        result = {"ok": False, "error": "fixture verifier unavailable"}
    (ROOT / "answers" / "verify.json").write_text(json.dumps(result))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["setup", "add", "event", "answer", "check", "poll", "quota", "inspect",
                                       "emit", "emit-dynamic", "fault", "poll-ordinary", "ordinary", "dispatch"])
    ap.add_argument("--repo", default="fixture")
    ap.add_argument("--pr", type=int, default=1)
    ap.add_argument("--id", default="")
    ap.add_argument("--at", default="")
    ap.add_argument("--file", default="version")
    ap.add_argument("--kind", choices=["merged", "open", "no-pr", "nonpipeline"], default="merged")
    ap.add_argument("--continues", default="")
    ap.add_argument("--verdict", default="pass", choices=["pass", "fail", "incomplete", "unknown", "limit", "unavailable"])
    ap.add_argument("--seconds", type=int, default=0)
    ap.add_argument("--yesterday", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scope-change", action="store_true")
    ap.add_argument("--fault", choices=["report", "malformed", "diff", "verify", "preflight", "start", "restore"])
    args = ap.parse_args()
    if args.action == "setup":
        setup()
        return 0
    if not (ROOT / "loop").is_dir() or paths.CONFIG_DIR.parent != ROOT.parent:
        raise ValueError("set up this fixture through the sandbox first")
    os.environ["ARETE_LOOP_DIR"] = str(ROOT / "loop")
    cfg = config.load()
    if args.action == "add":
        add(args)
    elif args.action == "event":
        event = next(e for e in mergecheck.events(cfg, False) if e["repo"] == args.repo and e["pr"] == args.pr)
        print(json.dumps({**event, "trust": "internal"}))
    elif args.action == "answer":
        answer(args, cfg)
    elif args.action == "check":
        supplied = mergecheck.read_events(sys.stdin.read())
        return mergecheck.consume(cfg, args.repo, supplied, args.dry_run)
    elif args.action == "poll":
        loop = fixture_loop(cfg)
        if args.scope_change:
            loop.sources[0]["command"] += ["--since-cursor"]
        print(json.dumps(sources.poll(loop, 0, cfg, say=print)))
    elif args.action == "ordinary":
        print(json.dumps({"key": args.id or "ordinary", "title": "ordinary", "repo": "fixture"}))
    elif args.action == "poll-ordinary":
        loop = fixture_loop(cfg)
        source = {"name": "ordinary", "type": "command", "command": [sys.executable, str(Path(__file__).resolve()),
                  "ordinary", "--id", args.id or "ordinary"], "trust": "internal"}
        print(json.dumps(sources.poll(replace(loop, name="fixture-ordinary", sources=[source]), 0, cfg, say=print)))
    elif args.action == "dispatch":
        with util.lock(state.loop_lock("merge-check")) as taken:
            if not taken:
                print("merge-check queue busy")
                return 0
            loop = fixture_loop(cfg)
            loop.emits = [{**spec, "approve": True} if spec["type"] == "ticket" else spec for spec in loop.emits]
            item = next((state.queue_item(i["id"]) for i in state.pending() if i["loop"] == loop.name), None)
            if not item:
                print("no pending input")
                return 0
            if any(mergecheck.held_until(loop.dir, e["repo"]) for e in item["payload"]):
                print("quota hold; no subprocess")
                return 0
            run_id = record.new_id("fixture-dispatch")
            record.start("loop", run_id=run_id, loop=loop.name, loop_kind="command", queue="merge-check")
            if not state.take_queue_item(item["id"], run_id):
                raise ValueError("queue item already claimed")
            run = loop_run.LoopRun(loop, run_id, cfg, events=item["payload"])
            print(loop_run.carry(run))
    elif args.action == "quota":
        folder = ROOT / "loop" / ".state"
        with mergecheck.locked(folder):
            data = mergecheck.read_json(folder / "quota.json", {})
            data[args.repo] = {"day": str(datetime.now(UTC).date() - timedelta(days=int(args.yesterday))), "attempts": 6}
            mergecheck.save_json(folder / "quota.json", data)
    elif args.action == "inspect":
        print(json.dumps({"queue": state.pending(), "dead": state.dead_items(), "steps": state.runs(),
                          "quota": mergecheck.read_json(ROOT / "loop" / ".state" / "quota.json", {}),
                          "checked": mergecheck.read_json(ROOT / "loop" / ".state" / "checked.json", {}),
                          "starts": [line for log in paths.RUNS_DIR.glob("merge-check-*/verify-server.log")
                                     for line in log.read_text().splitlines() if line.startswith("START ")]}, default=str))
    elif args.action in ("emit", "emit-dynamic"):
        loop = replace(fixture_loop(cfg), emits=[{"type": "ticket", "repo": args.repo,
                                                "approve": True}, {"type": "metric", "name": "fixture_metric"}])
        if args.action == "emit-dynamic":
            loop.emits[0]["repo"] = "$finding.repo"
        delivery.deliver(loop, "fixture-emit", [{"title": "Fixture", "summary": "Held local fixture",
                                               "fingerprint": "fixture", "repo": args.repo}], {"value": 2, "trust": "internal"},
                         trial=True, log=lambda kind, text: print(kind, text), cfg=cfg)
    elif args.action == "fault":
        run_id = args.id or f"fixture-{args.repo}-{args.pr}"
        folder = paths.RUNS_DIR / run_id
        layer = ROOT / (args.repo + "-layer") / "config.toml"
        files = [folder / "verify.md", folder / "diff.patch", layer]
        if args.fault == "restore":
            for path in files:
                if path.with_suffix(path.suffix + ".bak").exists():
                    shutil.copyfile(path.with_suffix(path.suffix + ".bak"), path)
        else:
            target = files[1] if args.fault == "diff" else files[2] if args.fault in ("verify", "preflight", "start") else files[0]
            shutil.copyfile(target, target.with_suffix(target.suffix + ".bak"))
            if args.fault in ("report", "diff"):
                target.unlink()
            elif args.fault == "malformed":
                target.write_text("**pass**: no checks\n")
            elif args.fault == "verify":
                target.write_text('[git]\nbase = "main"\n')
            elif args.fault == "preflight":
                target.write_text(target.read_text() + '\n[verify.preflight]\nfixture = "exit 1"\n')
            else:
                target.write_text('[git]\nbase = "main"\n[verify]\nstart = "exit 1"\nurl = "http://127.0.0.1:{port}"\n')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
