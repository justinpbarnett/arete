from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sys
import textwrap
from collections import Counter
from collections.abc import Iterable
from contextlib import closing
from typing import NoReturn

from . import __version__, config, paths

DEFAULT_CONFIG = """\
alert_repo = "you/arete-alerts"

global_repo = "dotfiles"

[models]
plan = "opus"
build = "opus"
review = "opus"
learn = "sonnet"
miner = "opus"
apply = "sonnet"
uat = "sonnet"
verify = "opus"
eval_mine = "sonnet"
triage = "haiku"

[miner]
lookback_hours = 24
max_digest_chars = 90000

[[repo]]
name = "dotfiles"
path = "~/dev/dotfiles"
github = "you/dotfiles"
public = true

[[repo]]
name = "web"
path = "~/dev/web"
github = "you/web"
pipeline = true

[[job]]
label = "com.arete.serve"
keepalive = true

[[probe]]
name = "claude"
command = ["claude", "-p", "--model", "haiku", "--no-session-persistence", "Reply with the single word ok"]
expect = "ok"
every_hours = 12

[[probe]]
name = "gh"
command = ["gh", "auth", "status"]
expect = "Logged in"
every_hours = 6
"""


def cmd_init(args) -> int:
    paths.ensure_dirs()
    paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if paths.CONFIG_FILE.exists() and not args.force:
        print(f"{paths.CONFIG_FILE} exists; pass --force to overwrite")
        return 1
    paths.CONFIG_FILE.write_text(DEFAULT_CONFIG)
    print(f"wrote {paths.CONFIG_FILE}")
    print(f"state in {paths.STATE_DIR}, logs in {paths.LOG_DIR}")
    return 0


def cmd_status(args) -> int:
    from . import board, control

    if args.json:
        return status_lines(args.run_id)
    if args.run_id:
        print("arete status --json RUN_ID prints one run's steps", file=sys.stderr)
        return 2
    cfg = config.load()
    views = control.loop_views(cfg)
    snap = board.snapshot(recent_runs=args.limit)
    print(f"arete {__version__}  config {paths.CONFIG_FILE}")
    print(f"max_parallel {cfg.max_parallel}, max_parallel_per_repo {cfg.max_parallel_per_repo}")
    print("models:" + "".join(f"\n  {role:<10} {cfg.model(role) or 'claude CLI'}" for role in config.MODEL_ROLES))
    if cfg.unused_model_keys():
        print(f"  [models] keys arete never reads: {', '.join(cfg.unused_model_keys())}")
    show(snap["checks"], "the heartbeat", print_checks)
    show(snap["serve"], "the serve beat", lambda beat: print(serve_line(beat)))
    marks = {m["name"]: m for m in snap["checks"].get("rows", {}).get("loops", [])}
    for v in views:
        mark = marks.get(v["name"], {})
        print(f"  {loop_line(v)}" + ("" if mark.get("ok", True) else f"  FAILED {mark.get('detail', '')}"))
    show(snap["alerts"], "the alerts", print_alerts)
    show(snap["recent"], "the recent runs", print_recent)
    show(snap["proposals"], "the miner proposals", print_proposals)
    show(snap["skipped"], "the skipped tickets", print_skipped)
    show(snap["waiting"], "the review feedback", print_waiting)
    show(snap["triage"], "triage", print_triage)
    pipeline_repos = [f"{r.name} ({r.tracker})" for r in cfg.repos if r.pipeline]
    print(f"pipeline repos: {', '.join(pipeline_repos) or 'none'}")
    show(snap["live"], "the live runs", print_live)
    return 0


def status_lines(run_id: str) -> int:
    from . import client

    def live() -> int:
        try:
            panel = client.call("GET", "/api/live")["live"]
        except client.Down:
            from . import board, state

            panel = board.panel(lambda: board.live(state.runs_marked_running(connect=state.reader)))
        if "error" in panel:
            client.emit({"error": panel["error"]})
            return 1
        for row in panel["rows"]["runs"]:
            client.emit(row)
        return 0

    def steps() -> int:
        try:
            read = list(client.events(run_id))
        except client.Down:
            if (found := local_lines(run_id, 0, False)) is None:
                client.emit({"error": f"no run {run_id}"})
                return 1
            read = list(found)
        for line in client.steps_of(run_id, read):
            client.emit(line)
        return 0

    return client.steered(steps if run_id else live)


def show(panel: dict, what: str, draw) -> None:
    if "error" in panel:
        print(f"{what} could not be read: {panel['error']}")
    else:
        draw(panel["rows"])


def print_table(rows: list[tuple[str, ...]], width: int, indent: str = "  ") -> None:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    for row in rows:
        print((indent + " ".join(f"{cell:<{w}}" for cell, w in zip(row, widths)) + " " + row[-1])[:width].rstrip())


def print_checks(h: dict) -> None:
    if not h["heartbeat"]:
        print("heartbeat: never run")
        return
    bad = [c for c in h["checks"] if not c["ok"]]
    print(f"heartbeat {when(h['heartbeat'])}: {f'{len(bad)} failing' if bad else 'all ok'}")
    for c in bad:
        print(f"  {c['severity'].upper():<5} {c['key']:<38} {c['detail']}")


def print_alerts(a: dict) -> None:
    if a["alerts"]:
        print("open alerts:")
    for x in a["alerts"]:
        issue = f"{x['repo']}#{x['issue']}" if x["issue"] else "none"
        print(f"  {x['key']}  issue {issue}  since {x['opened']}")


def print_recent(runs: list[dict]) -> None:
    from . import util

    if runs:
        print("recent runs:")
        print_table([(r["id"], r["kind"], r["ticket"], r["status"], util.paid_note(r["paid"]), r["pr_url"] or r["summary"])
                     for r in runs], 200)


def print_proposals(p: dict) -> None:
    if p["memory"]:
        print("memory facts the miner noticed (file them by hand, then `arete forget ID`):")
    for m in p["memory"]:
        print(f"  {m['id']:<4} {m['change']}"[:200])
    if p["hand"]:
        print("harness fixes to make by hand, since Claude Code denies agent writes under .claude/ (then `arete forget ID`):")
    for h in p["hand"]:
        print(f"  {h['id']:<4} {h['repo']}/{h['file']}: {h['change']}"[:400])
    if p["approve"]:
        print("global rules waiting for approval (`arete file-rule ID` writes and pushes one, `arete reject-rule ID` drops it):")
    for r in p["approve"]:
        writing = " (being written now)" if r["status"] == "filing" else ""
        print(f"  {r['id']:<4} {r['repo']}/{r['file']}{writing}: {r['change']}"[:400])


def print_skipped(skipped: list[dict]) -> None:
    if skipped:
        print("labeled tickets the pipeline will not run (`arete run REPO ID --force` runs one anyway):")
    for s in skipped:
        print(f"  {s['ticket']:<24} {s['why']}  since {when(s['since'])}"[:220])


def print_waiting(w: dict) -> None:
    from . import util

    if w["tickets"]:
        print(f"your PR comments waiting for a revision (a submitted review goes at once, other comments after "
              f"{w['quiet_minutes']} quiet minutes):")
    for t in w["tickets"]:
        print(f"  {t['ticket']:<24} {util.count(t['comments'], 'comment')}, "
              + ("review submitted" if t["at_once"] else f"last comment {t['last']}"))


def print_triage(t: dict) -> None:
    from . import triage

    if not t["refreshed"]:
        print("triage: never refreshed (`arete triage --refresh`)")
        return
    errors = f"; FAILED {'; '.join(t['errors'])}" if t["errors"] else ""
    print(f"triage: {t['summary']}; refreshed {triage.stamp(t['refreshed'])}; board {t['posted']}{errors}"[:300])


def print_live(live: dict) -> None:
    from . import util

    print(f"runs live: {len(live['runs'])}, in {live['slots']} of {live['max_parallel']} run slots"
          + (f", {live['waiting']} waiting for one" if live["waiting"] else ""))
    if live["runs"]:
        print_table([(r["id"], r["kind"], r["ticket"], f"since {when(r['started'])}", util.paid_note(r["paid"]),
                      r["slot"] or r["step"]) for r in live["runs"]], 200)


def cmd_forget(args) -> int:
    from . import state

    if not state.forget_proposal(args.id):
        print(f"no open proposal #{args.id}")
        return 1
    return 0


def cmd_file_rule(args) -> int:
    from . import miner

    try:
        print(miner.file_rule(args.id))
    except LookupError as e:
        print(e)
        return 1
    return 0


def cmd_reject_rule(args) -> int:
    from . import state

    if not state.move_proposal(args.id, ("approve",), "rejected"):
        print(f"#{args.id} is not a rule waiting for approval")
        return 1
    return 0


def cmd_events(args) -> int:
    from . import hook

    evs = hook.read_events(args.hours)
    if args.raw:
        for e in evs[max(len(evs) - args.limit, 0):]:
            print(json.dumps(e))
        return 0
    c = Counter(f"{e.get('kind')}/{e.get('cls')}" for e in evs)
    print(f"{len(evs)} events in the last {args.hours:g}h")
    for k, n in c.most_common(30):
        print(f"  {n:>5}  {k}")
    return 0


def cmd_heartbeat(args) -> int:
    from . import heartbeat

    return heartbeat.run(dry_run=args.dry_run, force_probes=args.force_probes, quiet=args.quiet)


def cmd_hook(args) -> int:
    from . import hook

    return hook.main()


def cmd_guard(args) -> int:
    from . import guard

    return guard.main()


def cmd_codex_guard(args) -> int:
    from . import codex, runner

    return codex.guard_main(args.judge, runner.NO_GIT_WRITES)


def cmd_background(args) -> int:
    from . import background

    return background.main()


def cmd_ask(args) -> int:
    from . import ask

    return ask.main(args)


def cmd_jev(args) -> int:
    import json

    from . import verdicts

    data = verdicts.report(args.days, config.load())
    print(json.dumps(data, indent=2) if args.json else verdicts.render(data))
    return 0


def cmd_install(args) -> int:
    from . import install

    return install.install(dry_run=args.dry_run, platform=args.platform)


def cmd_uninstall(args) -> int:
    from . import install

    return install.uninstall()


def cmd_run(args) -> int:
    from . import client, pipeline

    if not (args.local or args.dry_run):
        try:
            started = client.call("POST", "/api/tasks", {"repo": args.repo, "ticket": args.issue, "force": args.force})
        except client.Down:
            started = None
        except client.Failed as e:
            return client.failed(e)
        if started is not None:
            return client.steered(lambda: run_followed(started))
    return pipeline.run_issue(args.repo, args.issue, dry_run=args.dry_run, force=args.force)


def run_followed(started: dict) -> int:
    from . import client, lines, pipeline

    client.emit(started)
    if not (run_id := started.get("run")):
        return 0 if started.get("state") == "queued" else 1
    try:
        for line in client.followed(run_id):
            client.emit(line)
            if line.get("event") == lines.END:
                return 0 if line.get("status") in pipeline.SUCCEEDED else 1
    except KeyboardInterrupt:
        print(f"detached; run {run_id} keeps going; `arete stop {run_id}` stops it", file=sys.stderr)
        return 130
    return 1


def task_shown(task_id: str, repo: str, one_line: bool) -> int:
    from urllib.parse import quote

    from . import client

    def show(found: dict) -> None:
        if one_line:
            client.emit(found)
        else:
            print(json.dumps(found, ensure_ascii=False, default=str, indent=2), flush=True)

    try:
        show(client.call("GET", f"/api/tasks/{quote(task_id, safe='')}"
                         + (f"?repo={quote(repo, safe='')}" if repo else "")))
        return 0
    except client.Down:
        from . import control, gh, jira, tasks

        try:
            show(tasks.view(task_id, repo))
        except (control.NotFound, control.Refused, gh.GhError, jira.JiraError) as e:
            client.emit({"error": str(e)})
            return 1
    return 0


def cmd_task(args) -> int:
    from . import chat, client, state

    if args.action == "start":
        return client.say("POST", "/api/tasks", {"repo": args.repo, "prompt": " ".join(args.prompt),
                                                 "title": args.title, "kind": args.kind})
    if args.action == "run":
        return client.say("POST", "/api/tasks", {"repo": args.repo, "ticket": args.ticket})
    if args.action == "show":
        return client.steered(lambda: task_shown(args.id, args.repo, args.json))
    if args.action == "add":
        body = " ".join(args.body).strip()
        if not body:
            print('arete task add --repo NAME "what to do"', file=sys.stderr)
            return 2
        config.load().repo(args.repo)
        print(state.task_add(args.repo, body, title=args.title.strip() or chat.title_of(body), kind=args.kind))
        return 0
    if args.action == "list":
        print_lines(json.dumps(row, ensure_ascii=False) for row in state.tasks(args.repo, args.state))
        return 0
    tid = args.id.strip().lower()
    if state.task_get(tid) is None:
        print(f"no task {tid}", file=sys.stderr)
        return 1
    if args.action == "reply":
        text = " ".join(args.text).strip()
        if not text:
            print('arete task reply ID "your reply"', file=sys.stderr)
            return 2
        state.task_comment(tid, "owner", text)
        print(f"{tid}: reply saved")
    else:
        state.task_set_state(tid, "closed")
        print(f"{tid}: closed")
    return 0


def cmd_jira_check(args) -> int:
    from . import jira, pipeline, tracker

    cfg = config.load()
    try:
        client = jira.Client.from_env(cfg.jira.env_file)
        me = client.me()
        print(f"jira {client.base} as {me.get('displayName') or me.get('emailAddress') or '?'}")
        routes = pipeline.jira_routes_by_project(cfg)
        targets = [(f"[jira.transitions] {k}", k, v) for k, v in cfg.jira.transitions.items()]
        targets += [(f"[jira.research] {k}", f"research {k}", v) for k, v in cfg.jira.research.items()]
        targets += [(f"[jira.landed] {k}", f"landed on {k}", v) for k, v in cfg.jira.landed.items()]
        for project, labels in sorted(routes.items()):
            jql = tracker.candidates_jql(project)
            n = len(client.search(jql, ["labels"], limit=100))
            print(f"  {project}: {n} labeled `{tracker.LABEL}` and waiting; routes {', '.join(sorted(labels))}")
            known = {s.lower() for s in client.statuses(project)}
            for key, _, name in targets:
                if name.lower() not in known:
                    print(f"  {project}: no status named '{name}' for {key}; it moves a ticket only where a "
                          "transition has that name")
            if targets:
                print(f"  {project}: moves " + ", ".join(f"{when} to {name}" for _, when, name in targets))
    except jira.JiraError as e:
        print(f"jira failed: {e}")
        return 1
    print("jira ok")
    return 0


def cmd_poll(args) -> int:
    from . import pipeline

    return pipeline.poll(dry_run=args.dry_run)


def cmd_mine(args) -> int:
    from . import miner

    return miner.run(hours=args.hours, dry_run=args.dry_run, apply=not args.no_apply)


def cmd_audit(args) -> int:
    from . import auditor

    return auditor.run(dry_run=args.dry_run)


def cmd_eval(args) -> int:
    from . import eval_mine, evals

    words = args.target
    if words[:1] == ["mine"] and len(words) <= 2:
        return eval_mine.run(words[1] if len(words) == 2 else "", limit=args.limit, dry_run=args.dry_run)
    if len(words) > 1:
        print("usage: arete eval [REPO] or arete eval mine [REPO]", file=sys.stderr)
        return 2
    if not words:
        if given := [flag for flag, value in (("--task", args.task), ("--ref", args.ref), ("--prompts", args.prompts)) if value]:
            print(f"arete eval {' '.join(given)} needs a REPO", file=sys.stderr)
            return 2
        return evals.run_all(dry_run=args.dry_run)
    prompts_dir = paths.expand(args.prompts).resolve() if args.prompts else None
    prompts = evals.Prompts(prompts_dir, paths.short_path(prompts_dir)) if prompts_dir else None
    return evals.run(words[0], task=args.task, ref=args.ref, dry_run=args.dry_run, prompts=prompts)


def cmd_triage(args) -> int:
    from . import triage

    if args.refresh or args.dry_run:
        code = triage.run(dry_run=args.dry_run)
        if args.dry_run:
            return code
    else:
        code = 0
    if args.ref:
        shown = triage.ticket(args.ref)
        if shown is None:
            print(f"no triage row for {args.ref}; `arete triage` lists them", file=sys.stderr)
            return 1
        print(shown)
    else:
        print(triage.board())
    return code


def cmd_research(args) -> int:
    from . import research

    if args.next or (args.dry_run and not args.ref):
        return research.next_due(dry_run=args.dry_run, asked=piped_events())
    if args.ref and args.run:
        return research.run_now(args.ref, force=args.force, dry_run=args.dry_run)
    if args.ref:
        shown = research.shown(args.ref)
        if shown is None:
            print(f"no research for {args.ref}; `arete research` lists them", file=sys.stderr)
            return 1
        print(shown)
        return 0
    print(research.listing())
    return 0


def cmd_dashboard(args) -> int:
    from . import dashboard

    return dashboard.serve(args.port)

def refused(e: Exception) -> int:
    print(e, file=sys.stderr)
    return 1


def when(stamp: str | None) -> str:
    from . import util

    return util.parse_iso(stamp).astimezone().strftime("%m-%d %H:%M") if stamp else "-"


def serve_line(beat: dict) -> str:
    from . import util

    if not beat:
        return "serve: never ran; `arete install` starts it"
    if not beat["alive"]:
        return f"serve: DOWN, {'its process is gone, ' if beat['process_gone'] else ''}last beat {when(beat.get('at'))}"
    return (f"serve: up, pid {beat['pid']}, {util.count(beat['loops'], 'loop')} on, {beat['off']} off, "
            f"{beat['running']} running, {beat['pending']} waiting" + (f", {len(beat['broken'])} broken" if beat.get("broken") else ""))


def loop_line(v: dict) -> str:
    from . import util

    switch = "BROKEN" if v["error"] else ("on" if v["enabled"] else "off")
    last = v["last"] or {}
    what = (f"running {v['running']}" if v["running"] else
            f"{last.get('status', 'never ran')} {when(last.get('finished') or last.get('started'))}" if last
            else "never ran")
    due = f"next {when(v['next_due'])}" if v.get("next_due") else ""
    return (f"{v['name']:<22} {switch:<6} {v['kind']:<7} {v['trigger']:<22} {what:<26} {due:<17}"
            f" {util.paid_note(v['spent_24h'])} in 24 h" + (f"\n    {v['error']}" if v["error"] else ""))


def print_feed_line(line: dict) -> None:
    text = line["text"].replace("\n", "\n    ")
    print(f"{when(line['ts'])[6:]} [{line['kind']}] {text}", flush=True)


def follow(run_id: str, alive=None, after: int = 0, send=None) -> int:
    import time

    from . import state

    while True:
        for line in state.feed(run_id, after):
            print_feed_line(line)
            after = line["id"]
        r = state.run_get(run_id) or {}
        if not (alive() if alive else r.get("status") == "running"):
            for line in state.feed(run_id, after):
                print_feed_line(line)
            return after
        if send:
            send()
        time.sleep(1)


def piped_events() -> list[dict]:
    if not os.environ.get("ARETE_LOOP") or sys.stdin is None:
        return []
    return [json.loads(line) for line in sys.stdin if line.strip()]


def read_events(file: str) -> list[dict] | None:
    from . import control

    if not file:
        return None
    path = paths.expand(file)
    try:
        data = json.loads(path.read_text())
    except OSError as e:
        raise control.Refused(f"--events {paths.short_path(path)}: {e.strerror or e}") from None
    except ValueError as e:
        raise control.Refused(f"--events {paths.short_path(path)} is not JSON: {e}") from None
    return data if isinstance(data, list) else [data]


def latest_run(name_or_run: str) -> str:
    from . import control, state

    if state.run_get(name_or_run):
        return name_or_run
    loop = control.find_loop(name_or_run)
    last = next(iter(state.runs(kind="loop", loop=loop.name, limit=1, firings=True)), None)
    if not last:
        raise control.NotFound(f"{loop.name} has not run yet")
    return last["id"]


def cmd_serve(args) -> int:
    from . import serve

    serve.run(with_dashboard=not args.no_dashboard)


def cmd_loop(args) -> int:
    from . import control, engine, loops, scaffold, state, util

    try:
        if args.action == "list":
            print(serve_line(control.serve_beat()))
            for v in control.loop_views():
                print(loop_line(v))
            return 0
        if args.action == "new":
            made = scaffold.draft(scaffold.DraftRequest(" ".join(args.words), args.name, args.repo, args.model),
                                  run_id=args.run_id)
            print(f"drafted {made['name']} in {paths.short_path(made['dir'])} (off, {util.paid_note(made['paid'])})\n")
            print(made["loop_toml"])
            if made["note"]:
                print(f"note: {made['note']}")
            print(f"next: `arete loop try {made['name']}`, then `arete loop enable {made['name']}`")
            return 0
        if args.action == "exec":
            if not args.run_id:
                print("arete loop exec needs --run-id; serve and `arete loop start` pass it", file=sys.stderr)
                return 2
            return 0 if engine.carry_opened(args.run_id) in ("done", "skipped") else 1
        if not args.name:
            print(f"arete loop {args.action} needs a NAME", file=sys.stderr)
            return 2
        if args.action == "try":
            result = scaffold.try_loop(args.name, read_events(args.events), follow=follow)
            print(f"\ntry {result['id']}: {result['status']}, every emit held"
                  + (f", {util.count(result['events'], 'sample event')}" if result["events"] else ""))
            return 0 if result["status"] in ("done", "skipped") else 1
        if args.action in ("enable", "disable"):
            done = control.switch(args.name, args.action == "enable")
            print(f"{done['name']}: {'on' if done['enabled'] else 'off'}"
                  + ("; serve is down, so it fires once serve runs again" if done["enabled"] and not done["serve_up"]
                     else ""))
            return 0
        if args.action == "start":
            r = engine.launch(args.name, events=read_events(args.events))
            print(f"started {r['id']}")
            if args.follow:
                follow(r["id"])
            return 0
        if args.action == "stop":
            target = args.name
            if not state.run_get(target):
                loop = control.find_loop(target)
                running = [r for r in state.runs_marked_running() if r["kind"] == "loop" and r["loop"] == loop.name]
                if not running:
                    raise control.Refused(f"{target} has no run going")
                target = running[0]["id"]
            print(json.dumps(control.stop_run(target)))
            return 0
        if args.action == "status":
            d = control.loop_detail(args.name)
            print(loop_line(d))
            print(f"  {d['description']}\n  dir {d['dir']}, origin {d['origin']}"
                  + (f", repo {d['repo']}" if d["repo"] else ""))
            if d["model"]:
                budget = (f"budget {d['budget_usd'] or '-'} per run, {d['daily_budget_usd'] or '-'} per day"
                          if d["metered"] else "no budget, it runs on a subscription")
                print(f"  model {d['model']} {d['runner'] or ''} on {d['device']}, {budget}")
            if d["held"]:
                print(f"  {d['held']}")
            if d["emits"]:
                print(f"  emits {', '.join(d['emits'])}")
            if d["webhook_path"]:
                print(f"  webhook POST {d['webhook_path']}")
            for c in d["cursors"]:
                print(f"  source {c['source']}: {'ok' if c['ok'] else 'FAILING'} {c['detail'] or ''} at {when(c['polled'])}")
            if d["waiting"]:
                print(f"  waiting: {util.count(len(d['waiting']), 'firing')}")
            if d["state"]:
                print(f"  memory: {json.dumps(d['state'])[:300]}")
            for r in d["runs"][:8]:
                print(f"  {r['id']:<34} {r['status']:<11} {r['trigger'] or '':<8} {util.paid_note(r['paid']):<12} "
                      f"{util.first_line(r['summary'] or '', 90)}")
            for f in d["findings"][:8]:
                print(f"  finding {f['title'][:90]} {f['url'] or ''}")
            return 0
        if args.action == "feed":
            run_id = latest_run(args.name)
            r = state.run_get(run_id)
            print(f"{run_id} {r['status']} {r.get('loop') or r['kind']}")
            if args.follow:
                follow(run_id)
            else:
                for line in state.feed(run_id):
                    print_feed_line(line)
            return 0
    except (control.NotFound, control.Busy, control.Refused, loops.LoopError) as e:
        return refused(e)
    return 2


def cmd_devices(args) -> int:
    from . import control, devices

    try:
        if args.probe:
            info = devices.probe(args.probe)
            print(json.dumps(info, indent=2))
            return 0 if info["ok"] else 1
        fleet = control.fleet()
        print_table([(d["name"], d["kind"], d.get("host") or "", *probe_words(d.get("probe"))) for d in fleet], 220,
                    indent="")
        for d in fleet:
            if d.get("error"):
                print(f"  {d['error']}")
        return 0
    except devices.DeviceError as e:
        return refused(e)


def probe_words(probe: dict | None) -> tuple[str, str]:
    if not probe:
        return "not probed", ""
    seen = ("ok" if probe.get("ok") else "FAIL") + f" {when(probe.get('at'))}"
    return seen, ", ".join(x for x in (probe.get("system", ""), " ".join(probe.get("tools") or []),
                                       "; ".join(probe.get("gpus") or []), probe.get("detail", "")) if x)


def cmd_session(args) -> int:
    import select
    import threading

    from . import control, sessions, state, util

    try:
        if args.action == "start":
            tools = [t.strip() for t in args.tools.split(",") if t.strip()] if args.tools is not None else None
            s = sessions.launch(" ".join(args.words), device=args.device, model=args.model, run_with=args.runner,
                                tools=tools, repo=args.repo, budget_usd=args.budget)
            print(f"session {s['id']}: {s['runner']} {s['model']} on {s['device']} in {paths.short_path(s['cwd'])}"
                  + (f" on {s['branch']}" if s["branch"] else "") + f"; tools {', '.join(s['tools']) or 'none'}")
            if not args.attach:
                print(f"`arete session attach {s['id']}` follows it and sends what you type")
                return 0
            args.id = s["id"]
        if args.action in ("start", "attach"):
            r = state.run_get(args.id)
            if not r or r["kind"] != "session":
                raise control.NotFound(f"no session {args.id}")
            stop = threading.Event()
            print("following; type a line and press enter to send it, ctrl-c to detach", flush=True)

            def send_typed() -> None:
                if stop.is_set() or not sys.stdin.isatty() and sys.stdin.closed:
                    return
                ready, _, _ = select.select([sys.stdin], [], [], 0)
                if ready:
                    line = sys.stdin.readline()
                    if not line:
                        stop.set()
                    elif line.strip():
                        sessions.send(args.id, line)
            try:
                follow(args.id, send=send_typed)
            except KeyboardInterrupt:
                print(f"\ndetached; the session keeps running. `arete session stop {args.id}` ends it")
            return 0
        if args.action == "send":
            print(json.dumps(sessions.send(args.id, " ".join(args.words))))
            return 0
        if args.action == "stop":
            print(json.dumps(control.stop_run(args.id)))
            return 0
        if args.action == "exec":
            return 0 if sessions.execute(args.id) in ("done", "stopped", "busy") else 1
        for s in sessions.views():
            print(f"{s['id']:<28} {s['status']:<11} {s['runner'] or '':<6} {s['model'] or '':<24} "
                  f"{s['device'] or '':<10} {util.paid_note(s['paid']):<12}  {s['prompt'][:70]}")
        if not state.runs(kind="session", limit=1):
            print("no sessions yet; `arete session start \"what to do\"`")
        return 0
    except (control.NotFound, control.Busy, control.Refused) as e:
        return refused(e)


def approval_notes(a: dict) -> list[str]:
    failed = [a["failed"], f"`arete approve {a['id']}` tries again, `--reject` drops it"]
    review = a["review"]
    return [*(failed if a["failed"] else []),
            *(note for note in (f"held: {a['held']}" if a["held"] else "",
                                f"review by {review['by']}: {review['said']}" if review else "") if note)]


def cmd_approve(args) -> int:
    from . import control, delivery

    try:
        if args.id is None:
            pending = delivery.waiting()
            for a in pending:
                print(textwrap.fill(f"#{a['id']:<4} {a['loop']:<20} {a['repo'] or 'none':<12} {when(a['created'])}  "
                                    f"{a['plain_summary'] or a['what']}", shutil.get_terminal_size().columns,
                                    subsequent_indent=" " * 6, break_on_hyphens=False, max_lines=4, placeholder=" …"))
                for line in approval_notes(a):
                    print(textwrap.fill(line, shutil.get_terminal_size().columns, initial_indent=" " * 6,
                                        subsequent_indent=" " * 8, break_on_hyphens=False))
            if not pending:
                print("nothing waiting for approval")
            return 0
        print(json.dumps(control.decide(args.id, not args.reject)))
        return 0
    except (control.NotFound, control.Refused) as e:
        return refused(e)


def cmd_webhook(args) -> int:
    from . import control, dashboard, hooks

    try:
        loop = control.working_loop(args.name)
    except (control.NotFound, control.Refused) as e:
        return refused(e)
    if not loop.trigger.webhook:
        print(f"{loop.name} has no webhook; set `webhook = true` under [trigger] in {paths.short_path(loop.file)}",
              file=sys.stderr)
        return 1
    key = hooks.secret(loop.name)
    node = dashboard.this_node(config.load().dashboard_owner)
    host = node.addresses[0] if node else "<this machine's tailnet address>"
    print(f"POST http://{host}:{config.load().dashboard_port}{hooks.hook_path(loop.name)}")
    print(f"X-Arete-Token: {key}")
    print("GitHub: use the same value as the webhook secret; arete checks X-Hub-Signature-256")
    return 0


def cmd_costs(args) -> int:
    from . import control, util

    if args.by or args.json:
        return print_step_costs(args.by or ["step"], args.days, args.json)
    try:
        c = control.costs(args.days)
    except control.Refused as e:
        return refused(e)
    print(f"{util.paid_note(c['total_usd']).capitalize()} in the last {c['days']:g} days; subscription calls count nothing")
    for group in ("model", "runner", "loop"):
        print(f"by {group}:")
        for row in c[f"by_{group}"][:12]:
            steps = "step " if row["steps"] == 1 else "steps"
            print(f"  {row['name']:<36} ${row['cost_usd']:>8.2f}  {row['steps']:>5} {steps}  "
                  f"{row['tokens_in']:>10,} in  {row['tokens_out']:>9,} out")
    return 0


def print_step_costs(groupings: list[str], days: float | None, as_json: bool) -> int:
    from . import control

    try:
        found = [(by, control.step_costs(by, days)) for by in groupings]
    except control.Refused as e:
        return refused(e)
    c = found[-1][1]
    waits = {"wait_minutes": c["wait_minutes"], "days": c["days"], "since": c["since"]}
    if as_json:
        print_lines([*(json.dumps(g) for _, each in found for g in each["groups"]), json.dumps(waits)])
        return 0
    for by, each in found:
        print(f"Every step by {by} in the last {each['days']:g} days. Cost is what each call reported; "
              "subscription calls count here too. Paid totals exclude them. `in` counts the input no cache served.")
        rows = [(by, "runs", "calls", "cost", "in", "cache rd", "cache wr", "out", "min", "api min")]
        rows += [(g["name"], str(g["runs"]), str(g["calls"]), f"${g['cost_usd']:.2f}",
                  f"{g['tokens_in'] - g['cache_read'] - g['cache_write']:,}", f"{g['cache_read']:,}",
                  f"{g['cache_write']:,}", f"{g['tokens_out']:,}", f"{g['minutes']:.1f}", f"{g['api_minutes']:.1f}")
                 for g in each["groups"]]
        print_table(rows, 200)
    print("waits (minutes): " + "  ".join(f"{cause} {m:.1f}" for cause, m in c["wait_minutes"].items()))
    return 0


def cmd_steps(args) -> int:
    from . import steps

    where = ""
    try:
        cfg = config.load()
        layer_dir = cfg.repo(args.repo).layer_dir if args.repo else None
        where = f"{paths.short_path(layer_dir / 'config.toml')}: " if layer_dir else ""
        layer = config.load_layer(layer_dir) if layer_dir else None
        steps.check(cfg, layer)
        sheet = [steps.resolve(cfg, layer, role, args.kind) for role in config.MODEL_ROLES]
    except (ValueError, TypeError) as e:
        print(f"{where}{e}", file=sys.stderr)
        return 1
    print_lines(json.dumps(dataclasses.asdict(s)) for s in sheet)
    return 0


def cmd_record(args) -> int:
    from . import record

    if args.follow and not args.step:
        print("arete record --follow requires STEP", file=sys.stderr)
        return 2
    folder = paths.record_dir(args.run_id)
    if not folder.is_dir() and not args.follow:
        print(f"no record for run {args.run_id}", file=sys.stderr)
        return 1
    if not args.step:
        timeline = folder / record.TIMELINE
        print_lines(record.raw_lines(timeline) if timeline.is_file() else [])
        print_lines(f.name for f in record.record_files(args.run_id))
        return 0
    if args.follow:
        with closing(record.follow(args.run_id, args.step)) as source:
            try:
                print_lines(source if args.raw else (json.dumps(e, ensure_ascii=False)
                            for e in record.normalized(record.entries(source))), flush=True)
                return 0
            except FileNotFoundError:
                pass
    elif path := record.find_record(args.run_id, args.step):
        print_lines(record.raw_lines(path) if args.raw else (json.dumps(e, ensure_ascii=False)
                    for e in record.events(path)))
        return 0
    if not folder.is_dir() and not record.run_status(args.run_id):
        print(f"no record for run {args.run_id}", file=sys.stderr)
        return 1
    names = ", ".join(f.name for f in record.record_files(args.run_id)) or "none"
    print(f"no step {args.step} in run {args.run_id}; its records: {names}", file=sys.stderr)
    return 1


def local_lines(run_id: str, start: int, follow: bool) -> Iterable[dict] | None:
    from . import lines, state

    if not lines.path(run_id).exists() and not state.run_get(run_id):
        return None
    return (lines.follow(run_id, start) if follow
            else (line for line in lines.read(run_id) if lines.seq_of(line) > start))


def cmd_lines(args) -> int:
    from . import client

    def printed() -> int:
        try:
            found = (client.followed if args.follow else client.events)(args.run_id, args.start)
        except client.Down:
            if (found := local_lines(args.run_id, args.start, args.follow)) is None:
                client.emit({"error": f"no run {args.run_id}"})
                return 1
        for line in found:
            if not args.step or line.get("step") == args.step:
                client.emit(line)
        return 0

    return client.steered(printed)


def cmd_wait(args) -> int:
    from . import client

    return client.steered(lambda: client.wait(args.run_id))


def cmd_answer(args) -> int:
    from . import client

    return client.say("POST", client.run_path(args.run_id, "answer"), {"text": " ".join(args.text)})


def cmd_resume(args) -> int:
    from . import client

    return client.say("POST", client.run_path(args.run_id, "resume"), {"step": args.step} if args.step else {})


def cmd_step(args) -> int:
    from urllib.parse import quote

    from . import client

    sets = {}
    for setting in args.sets:
        key, eq, value = setting.partition("=")
        if not eq or not key.strip():
            client.emit({"error": f"write each setting as key=value, not '{setting}'"})
            return 1
        sets[key.strip()] = value
    return client.say("POST", client.run_path(args.run_id, f"steps/{quote(args.step, safe='')}"), {"set": sets})


def cmd_stop(args) -> int:
    from . import client

    return client.say("POST", client.run_path(args.run_id, "stop"))


def cmd_replay(args) -> int:
    from . import replay

    try:
        lines = replay.sample(paths.expand(args.folder), adw=args.adw)
    except (OSError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1
    print_lines(lines)
    return 0


def print_lines(lines: Iterable[str], *, flush: bool = False) -> None:
    try:
        for line in lines:
            print(line, flush=flush)
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def cmd_loop_args(args) -> int:
    words = list(args.words)
    if args.action == "new":
        words = [args.name, *words] if args.name else words
        args.name = args.new_name
        if not words:
            print('arete loop new "one sentence on what the loop does"', file=sys.stderr)
            return 2
    args.words = words
    return cmd_loop(args)


def cmd_devices_args(args) -> int:
    if (args.probe, bool(args.target)) not in (("", False), ("probe", True)):
        print("usage: arete devices [probe NAME]", file=sys.stderr)
        return 2
    args.probe = args.target
    return cmd_devices(args)


def cmd_session_args(args) -> int:
    words = list(args.words)
    if args.action in ("attach", "send", "stop", "exec"):
        if not words:
            print(f"arete session {args.action} needs a session id", file=sys.stderr)
            return 2
        args.id, words = words[0], words[1:]
    args.words = words
    if args.action == "start" and not words:
        print('arete session start "what to do" [--device NAME --model M --repo R]', file=sys.stderr)
        return 2
    if args.action == "send" and not words:
        print("arete session send ID \"text\"", file=sys.stderr)
        return 2
    return cmd_session(args)


class Parser(argparse.ArgumentParser):
    json_errors = False

    def parse_known_args(self, args=None, namespace=None):
        found, extra = super().parse_known_args(args, namespace)
        if self.json_errors and extra:
            self.error(f"unrecognized arguments: {' '.join(extra)}")
        return found, extra

    def error(self, message: str) -> NoReturn:
        if not self.json_errors:
            super().error(message)
        print(json.dumps({"error": message, "usage": self.format_usage().strip()}), flush=True)
        sys.exit(1)


def steering(p: Parser) -> Parser:
    p.json_errors = True
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arete", description="arete: agents that keep running and keep getting better", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"arete {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=Parser)

    p = sub.add_parser("init", help="write the default global config")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = steering(sub.add_parser("status", help="settings, heartbeat, alerts, recent runs; with --json, the live runs "
                                               "or one run's steps as JSON lines"))
    p.add_argument("run_id", nargs="?", default="", help="with --json: this run's steps, then its report")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true", help="one JSON line per live run, or per step of RUN_ID")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("events", help="summarize hook events")
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--raw", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_events)

    p = sub.add_parser("heartbeat", help="loop 0: check jobs, probes, auth, hook, disk")
    p.add_argument("--dry-run", action="store_true", help="print actions instead of opening or closing alerts")
    p.add_argument("--force-probes", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_heartbeat)

    p = sub.add_parser("hook", help="Claude Code event hook (reads a payload on stdin)")
    p.set_defaults(fn=cmd_hook)

    p = sub.add_parser("guard", help="PreToolUse hook for arete's agents: keeps them out of secret files and, with JEV, "
                                     "stops shell commands git cannot undo (reads a payload on stdin)")
    p.set_defaults(fn=cmd_guard)

    p = sub.add_parser("codex-guard", help="PreToolUse hook for arete's codex agents: refuses git writes and secret "
                                           "files and, with --judge, asks JEV like guard (reads a payload on stdin)")
    p.add_argument("--judge", action="store_true")
    p.set_defaults(fn=cmd_codex_guard)

    p = sub.add_parser("background", help="Stop hook for arete's claude agents: an agent that answers while commands it "
                                          "started in the background still run is told to stop them and answer again "
                                          "(reads a payload on stdin)")
    p.set_defaults(fn=cmd_background)

    p = sub.add_parser("ask", help="put a yes or no question, a choice or a rating to JEV over piped text or files")
    p.add_argument("question")
    p.add_argument("paths", nargs="*", help="files, folders or globs; each file gets its own answer (default: stdin)")
    p.add_argument("--choice", nargs="+", metavar="LABEL[=ABOUT]", help="answer with one of these labels")
    p.add_argument("--score", nargs="+", metavar="LEVEL", help="rate on these levels, lowest first")
    p.add_argument("-r", "--recursive", action="store_true", help="look inside folders all the way down")
    p.add_argument("--only", metavar="ANSWER", help="print only the rows with this answer, and any errors")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("jev", help="what came of JEV's verdicts: holds by question, close holds, near misses, reverts")
    p.add_argument("action", choices=("report",))
    p.add_argument("--days", type=float, default=7, help="how far back to read (default 7)")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(fn=cmd_jev)

    p = sub.add_parser("install", help="run arete serve at login: one launchd agent on a Mac, one systemd user unit on "
                                       "Linux; retires the old per loop agents")
    p.add_argument("--dry-run", action="store_true", help="print the agent or unit, change nothing")
    p.add_argument("--platform", choices=("mac", "linux"), default="", help="with --dry-run: print for this platform")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("uninstall", help="stop arete serve and remove its agent or unit")
    p.set_defaults(fn=cmd_uninstall)

    p = sub.add_parser("serve", help="the daemon: fire loops on their triggers, run the queues, serve the dashboard")
    p.add_argument("--no-dashboard", action="store_true")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("loop", help="list, draft, try, switch, start, stop and follow loops")
    p.add_argument("action", choices=("list", "new", "try", "enable", "disable", "start", "stop", "status", "feed",
                                      "exec"))
    p.add_argument("name", nargs="?", default="", help="a loop name; a run id also works for stop and feed")
    p.add_argument("words", nargs="*", help=argparse.SUPPRESS)
    p.add_argument("--name", dest="new_name", default="", help="new: the loop name")
    p.add_argument("--repo", default="", help="new: the repo the loop works on")
    p.add_argument("--model", default="", help="new: the model that drafts it")
    p.add_argument("--events", default="", metavar="FILE", help="try, start: a JSON list of events to feed it")
    p.add_argument("-f", "--follow", action="store_true", help="feed, start: keep printing until the run ends")
    p.add_argument("--run-id", default="", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_loop_args)

    p = sub.add_parser("devices", help="the devices loops and sessions run on, with their last probe")
    p.add_argument("probe", nargs="?", default="", metavar="probe NAME")
    p.add_argument("target", nargs="?", default="", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_devices_args)

    p = sub.add_parser("session", help="start, follow, message and stop agent sessions on any device")
    p.add_argument("action", choices=("start", "attach", "send", "list", "stop", "exec"))
    p.add_argument("words", nargs="*", help="start: the first message; others: the session id, then send: the text")
    p.add_argument("--device", default="local")
    p.add_argument("--model", default="")
    p.add_argument("--runner", default="", help="claude, pi or codex; by the model when left out")
    p.add_argument("--tools", default=None, help="comma separated; Read,Edit,Write,Glob,Grep,Bash by default")
    p.add_argument("--repo", default="", help="a repo name from the config; work in a new worktree of it on a device of kind local")
    p.add_argument("--budget", type=float, default=None, help="USD for the whole session")
    p.add_argument("--attach", action="store_true", help="start: follow it at once")
    p.set_defaults(fn=cmd_session_args)

    p = sub.add_parser("approve", help="list emits waiting for approval, or approve one by id; "
                                       "approving a failed one tries the delivery again")
    p.add_argument("id", type=int, nargs="?", default=None)
    p.add_argument("--reject", action="store_true")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("webhook", help="print the URL and token for a loop's webhook")
    p.add_argument("action", choices=("secret",))
    p.add_argument("name")
    p.set_defaults(fn=cmd_webhook)

    p = steering(sub.add_parser("costs", help="what you paid for model calls, by model, runner and loop; with --by, "
                                              "every step by step, task kind or model, and the wait minutes by cause"))
    p.add_argument("--days", type=float, default=None)
    p.add_argument("--by", nargs="+", choices=("step", "kind", "model"), help="one grouping or several, in order")
    p.add_argument("--json", action="store_true", help="one JSON line per group, then one with the wait minutes")
    p.set_defaults(fn=cmd_costs)

    p = steering(sub.add_parser("steps", help="the model, runner, effort, timeout, tries, tools, mcp servers, "
                                              "instructions and session each step resolves to, one JSON line per step"))
    p.add_argument("--repo", default="", help="read this repo's layer too")
    p.add_argument("--kind", default="", choices=config.KINDS, help="resolve for this kind of ticket")
    p.set_defaults(fn=cmd_steps)

    p = sub.add_parser("record", help="a run's timeline and record files, or one agent call's record as normalized "
                                      "events")
    p.add_argument("run_id")
    p.add_argument("step", nargs="?", default="", help="a record file name, with or without .jsonl.gz")
    p.add_argument("--raw", action="store_true", help="print the recorded lines as they were written")
    p.add_argument("-f", "--follow", action="store_true", help="follow a STEP until its Arete result or the run ends")
    p.set_defaults(fn=cmd_record)

    p = steering(sub.add_parser("lines", help="a run's run.jsonl, one JSON line per event: task, steps, heartbeats, "
                                              "questions, errors and the report"))
    p.add_argument("run_id")
    p.add_argument("-f", "--follow", action="store_true", help="keep printing new lines until the report line")
    p.add_argument("--from", dest="start", type=int, default=0, metavar="SEQ",
                   help="start after the line with this seq")
    p.add_argument("--step", default="", help="print only this step's lines")
    p.set_defaults(fn=cmd_lines)

    p = steering(sub.add_parser("wait", help="follow a run through the server, printing its step_end, question, "
                                             "error and report lines, until the report or a question"))
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_wait)

    p = steering(sub.add_parser("answer", help="answer the question of a run that ended needs-info, through the "
                                               "server, which starts the next run"))
    p.add_argument("run_id")
    p.add_argument("text", nargs="+")
    p.set_defaults(fn=cmd_answer)

    p = steering(sub.add_parser("resume", help="continue a run that ended interrupted, stopped or failed, through the "
                                               "server"))
    p.add_argument("run_id")
    p.add_argument("--step", default="", help="run this step and every step after it again")
    p.set_defaults(fn=cmd_resume)

    p = steering(sub.add_parser("step", help="run one step of a run that ended alone, through the server"))
    p.add_argument("run_id")
    p.add_argument("step")
    p.add_argument("sets", nargs="*", metavar="KEY=VALUE", help="override one of the step's settings")
    p.set_defaults(fn=cmd_step)

    p = steering(sub.add_parser("stop", help="stop a running run, through the server"))
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("replay", help="answer agent steps from files; `replay sample FOLDER` writes a fixture repo, its "
                                      "bare origin, a layer and an answer per ticket step")
    p.add_argument("action", choices=("sample",))
    p.add_argument("folder")
    p.add_argument("--adw", action="store_true", help="turn on [factory] path = \"adw\" in the sample layer")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("run", help="loop 3: run the pipeline on one ticket")
    p.add_argument("repo")
    p.add_argument("issue", help="an issue number (35), a Jira key (ABC-1234) or a task id (t-1a2b)")
    p.add_argument("--dry-run", action="store_true", help="plan and build locally, no push, PR or tracker writes")
    p.add_argument("--force", action="store_true", help="run a ticket the approval rules would skip")
    p.add_argument("--local", action="store_true", help="run in this process even when the server is up; the server "
                                                        "and the poller start runs this way")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("task", help="local tasks, which have no tracker issue: add, list, show, reply to and close them; "
                                    "start one from a prompt or a ticket through the server")
    tsub = p.add_subparsers(dest="action", required=True, parser_class=Parser)
    t = tsub.add_parser("add", help="add a task to a repo and print its id")
    t.add_argument("--repo", required=True, help="a repo name from the config")
    t.add_argument("--title", default="", help="the body's first line by default")
    t.add_argument("--kind", default="feat", choices=config.KINDS)
    t.add_argument("body", nargs="+", help="what to do")
    t = tsub.add_parser("list", help="one JSON line per task, oldest first")
    t.add_argument("--repo", default="")
    t.add_argument("--state", default="", choices=config.TASK_STATES)
    t = steering(tsub.add_parser("show", help="a task or a tracker item with its runs and children, as indented JSON"))
    t.add_argument("id")
    t.add_argument("--repo", default="", help="the repo of a tracker item no run names yet")
    t.add_argument("--json", action="store_true", help="print it as one JSON line")
    t = tsub.add_parser("reply", help="answer a task's question as its owner")
    t.add_argument("id")
    t.add_argument("text", nargs="+")
    t = tsub.add_parser("close", help="close a task")
    t.add_argument("id")
    t = steering(tsub.add_parser("start", help="add a task from a prompt and start it through the server; one JSON "
                                               "line"))
    t.add_argument("--repo", required=True, help="a repo name from the config")
    t.add_argument("--kind", default="feat", choices=config.KINDS)
    t.add_argument("--title", default="", help="the prompt's first line by default")
    t.add_argument("prompt", nargs="+", help="what to do")
    t = steering(tsub.add_parser("run", help="start a tracker item or a task through the server, past its waits; one "
                                             "JSON line"))
    t.add_argument("repo")
    t.add_argument("ticket", help="an issue number, a Jira key or a task id")
    p.set_defaults(fn=cmd_task)

    p = sub.add_parser("poll", help="loop 3: run the pipeline on the oldest ready ticket in each pipeline repo")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_poll)

    p = sub.add_parser("mine", help="loop 1: mine transcripts into harness PRs")
    p.add_argument("--hours", type=float, default=None)
    p.add_argument("--dry-run", action="store_true", help="print proposals, change nothing")
    p.add_argument("--no-apply", action="store_true", help="record proposals but open no PRs")
    p.set_defaults(fn=cmd_mine)

    p = sub.add_parser("forget", help="mark a memory fact or hand edit from the miner as done")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_forget)

    p = sub.add_parser("file-rule", help="write an approved global rule from the miner into the global CLAUDE.md and push it")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_file_rule)

    p = sub.add_parser("reject-rule", help="drop a global rule from the miner, so it is not proposed again")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_reject_rule)

    p = sub.add_parser("jira-check", help="read Jira with the configured token and count waiting tickets")
    p.set_defaults(fn=cmd_jira_check)

    p = sub.add_parser("audit", help="loop 2: deterministic harness audit")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("eval", help="loop 4: run eval tasks (all repos with evals when REPO is left out); "
                                    "`eval mine [REPO]` writes tasks from merged arete PRs")
    p.add_argument("target", nargs="*", metavar="REPO | mine [REPO]")
    p.add_argument("--task", default="")
    p.add_argument("--ref", default="", help="take the harness and tasks from this git ref, not the working copy")
    p.add_argument("--prompts", default="", metavar="DIR",
                   help="run the plan, build and fix steps with the prompts in DIR; the grader keeps the installed one")
    p.add_argument("--limit", type=int, default=3, help="with mine: the most merged PRs to mine in one go")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("triage", help="loop 5: the triage board of your open tickets without the agent label")
    p.add_argument("ref", nargs="?", default="", metavar="REF", help="show one ticket in full")
    p.add_argument("--refresh", action="store_true", help="triage what changed, then show the board")
    p.add_argument("--dry-run", action="store_true", help="list what a refresh would triage; no model calls, no writes")
    p.set_defaults(fn=cmd_triage)

    p = sub.add_parser("research", help="the reports arete writes for research tickets on the triage board")
    p.add_argument("ref", nargs="?", default="", metavar="REF", help="print one ticket's report")
    p.add_argument("--run", action="store_true", help="with REF: research it now")
    p.add_argument("--force", action="store_true", help="with --run: skip the JEV injection screen")
    p.add_argument("--next", action="store_true", help="research the next ticket due; what the research loop runs")
    p.add_argument("--dry-run", action="store_true",
                   help="say which ticket is due next, or with REF --run whether it would run; no model calls, no writes")
    p.set_defaults(fn=cmd_research)

    p = sub.add_parser("dashboard", help="serve the dashboard to its owner on the tailnet")
    p.add_argument("--port", type=int, default=None, help="the port to listen on; the config's [dashboard] port by default")
    p.set_defaults(fn=cmd_dashboard)

    args = ap.parse_args(argv)
    try:
        return args.fn(args) or 0
    except config.UnknownRepo as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
