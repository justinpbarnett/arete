from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

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


def slot_note(run_id: str, in_slots: set[str], waiting: set[str]) -> str:
    from . import pipeline

    if run_id in in_slots:
        return ""
    if run_id in waiting:
        return ", waiting for a run slot"
    return ", waiting on CI without a run slot" if pipeline.freed_slot_for_ci(run_id) else ", without a run slot"


def cmd_status(args) -> int:
    from . import control, pipeline, state, tracker, triage, util

    cfg = config.load()
    hb = util.read_json(paths.HEARTBEAT_FILE, {})
    print(f"arete {__version__}  config {paths.CONFIG_FILE}")
    print(f"max_parallel {cfg.max_parallel}")
    print("models:" + "".join(f"\n  {role:<10} {cfg.model(role) or 'claude default'}" for role in config.MODEL_ROLES))
    if cfg.unused_model_keys():
        print(f"  [models] keys arete never reads: {', '.join(cfg.unused_model_keys())}")
    if hb:
        bad = [c for c in hb.get("checks", []) if not c["ok"]]
        print(f"heartbeat {hb['ts']}: {'all ok' if not bad else f'{len(bad)} failing'}")
        for c in bad:
            print(f"  {c['severity'].upper():<5} {c['key']:<38} {c['detail']}")
    else:
        print("heartbeat: never run")
    print(serve_line())
    marks = util.read_json(paths.STATE_DIR / "loops.json", {})
    for v in control.loop_views(cfg):
        failed = marks.get(v["name"], {})
        print(f"  {loop_line(v)}" + ("" if failed.get("ok", True) else f"  FAILED {failed.get('detail', '')}"))
    al = state.alerts()
    if al:
        print("open alerts:")
        for a in al:
            print(f"  {a['key']}  issue #{a['issue'] or '-'}  since {a['opened']}")
    rs = state.runs(limit=args.limit)
    if rs:
        print("recent runs:")
        rows = [(r["id"], r["kind"], tracker.run_ticket_name(r) if r["issue"] else (r.get("loop") or r["repo"] or ""), r["status"],
                 f"${r['cost_usd'] or 0:.2f}", r["pr_url"] or r["summary"] or "") for r in rs]
        widths = [max(len(row[i]) for row in rows) for i in range(5)]
        for row in rows:
            print(("  " + " ".join(f"{cell:<{w}}" for cell, w in zip(row, widths)) + " " + row[5])[:200])
    mem = state.proposals(status=["memory"], limit=10)
    if mem:
        print("memory facts the miner noticed (file them by hand, then `arete forget ID`):")
        for p in mem:
            print(f"  {p['id']:<4} {p['change']}"[:200])
    hand = state.proposals(status=["hand"], limit=10)
    if hand:
        print("harness fixes to make by hand, since Claude Code denies agent writes under .claude/ (then `arete forget ID`):")
        for p in hand:
            print(f"  {p['id']:<4} {p['repo']}/{p['file']}: {p['change']}"[:400])
    skipped = pipeline.skipped_tickets()
    if skipped:
        print("labeled tickets the pipeline will not run (`arete run REPO ID --force` runs one anyway):")
        for name, v in sorted(skipped.items()):
            print(f"  {name:<24} {v['why']}  since {v.get('since', '')}"[:220])
    waiting = state.feedback_waiting()
    if waiting:
        print(f"your PR comments waiting for a revision (a submitted review goes at once, other comments after "
              f"{pipeline.FEEDBACK_QUIET_MINUTES} quiet minutes):")
        for w in waiting:
            when = "review submitted" if w["at_once"] else f"last comment {w['last']}"
            print(f"  {tracker.run_ticket_name(w):<24} {util.count(w['n'], 'comment')}, {when}")
    print(triage.status_line())
    pipeline_repos = [f"{r.name} ({r.tracker})" for r in cfg.repos if r.pipeline]
    print(f"pipeline repos: {', '.join(pipeline_repos) or 'none'}")
    live = pipeline.live_pipeline_runs()
    in_slots = {r["id"] for r in pipeline.holding(live, "slot")}
    slot_waiters = {r["id"] for r in pipeline.holding(live, "slot-wait")}
    print(f"runs live: {len(live)}, in {len(in_slots)} of {cfg.max_parallel} run slots"
          + (f", {len(slot_waiters)} waiting for one" if slot_waiters else "")
          + "".join(f"\n  {tracker.run_ticket_name(r)}  since {r['started']}"
                    + slot_note(r["id"], in_slots, slot_waiters) for r in live))
    return 0


def cmd_forget(args) -> int:
    from . import state

    with state.db() as c:
        c.execute("UPDATE proposals SET status = 'filed' WHERE id = ? AND status IN ('memory', 'hand')", (args.id,))
    return 0


def cmd_events(args) -> int:
    from . import hook

    evs = hook.read_events(args.hours)
    if args.raw:
        for e in evs[-args.limit:]:
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


def cmd_install(args) -> int:
    from . import install

    return install.install(dry_run=args.dry_run, platform=args.platform)


def cmd_uninstall(args) -> int:
    from . import install

    return install.uninstall()


def cmd_run(args) -> int:
    from . import pipeline

    return pipeline.run_issue(args.repo, args.issue, dry_run=args.dry_run, force=args.force)


def cmd_jira_check(args) -> int:
    from . import jira, pipeline, tracker

    cfg = config.load()
    try:
        client = jira.Client.from_env(cfg.jira.env_file)
        me = client.me()
        print(f"jira {client.base} as {me.get('displayName') or me.get('emailAddress') or '?'}")
        routes = pipeline.jira_routes_by_project(cfg)
        for project, labels in sorted(routes.items()):
            jql = tracker.candidates_jql(project)
            n = len(client.search(jql, ["labels"], limit=100))
            print(f"  {project}: {n} labeled `{tracker.LABEL}` and waiting; routes {', '.join(sorted(labels))}")
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
        if args.prompts:
            print("arete eval --prompts DIR needs a REPO", file=sys.stderr)
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


def cmd_dashboard(args) -> int:
    from . import dashboard

    return dashboard.serve(args.port)

def refused(e: Exception) -> int:
    print(e, file=sys.stderr)
    return 1


def when(stamp: str | None) -> str:
    from . import util

    return util.parse_iso(stamp).astimezone().strftime("%m-%d %H:%M") if stamp else "-"


def serve_line() -> str:
    from . import control, util

    beat = control.serve_beat()
    if not beat:
        return "serve: never ran; `arete install` starts it"
    if not beat["alive"]:
        return f"serve: DOWN, last beat {when(beat.get('at'))}"
    return (f"serve: up, pid {beat['pid']}, {util.count(beat['loops'], 'loop')} on, {beat['off']} off, "
            f"{beat['running']} running, {beat['pending']} waiting" + (f", {len(beat['broken'])} broken" if beat.get("broken") else ""))


def loop_line(v: dict) -> str:
    switch = "BROKEN" if v["error"] else ("on" if v["enabled"] else "off")
    last = v["last"] or {}
    what = (f"running {v['running']}" if v["running"] else
            f"{last.get('status', 'never ran')} {when(last.get('finished') or last.get('started'))}" if last
            else "never ran")
    due = f"next {when(v['next_due'])}" if v.get("next_due") else ""
    return (f"{v['name']:<22} {switch:<6} {v['kind']:<7} {v['trigger']:<22} {what:<26} {due:<17}"
            f" ${v['spent_24h']:.2f}/24h" + (f"\n    {v['error']}" if v["error"] else ""))


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


def read_events(file: str) -> list[dict] | None:
    if not file:
        return None
    data = json.loads(paths.expand(file).read_text())
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
            print(serve_line())
            for v in control.loop_views():
                print(loop_line(v))
            return 0
        if args.action == "new":
            made = scaffold.draft(" ".join(args.words), name=args.name, repo=args.repo, model=args.model)
            print(f"drafted {made['name']} in {paths.short_path(made['dir'])} (off, ${made['cost_usd']:.2f})\n")
            print(made["loop_toml"])
            if made["note"]:
                print(f"note: {made['note']}")
            print(f"next: `arete loop try {made['name']}`, then `arete loop enable {made['name']}`")
            return 0
        if args.action == "exec":
            status = engine.carry_opened(args.run_id) if not args.name else engine.launch(
                args.name, item=args.fire, run_id=args.run_id, events=read_events(args.events), detach=False)["status"]
            return 0 if status in ("done", "skipped") else 1
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
            print(f"{done['name']}: {'on' if done['enabled'] else 'off'}")
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
                running = [r for r in state.runs_marked_running()
                           if r["kind"] == "loop" and r["loop"] == control.find_loop(target).name]
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
                print(f"  model {d['model']} {d['runner'] or ''} on {d['device']}, budget "
                      f"{d['budget_usd'] or '-'} per run, {d['daily_budget_usd'] or '-'} per day")
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
                print(f"  {r['id']:<34} {r['status']:<11} {r['trigger'] or '':<8} ${r['cost_usd'] or 0:.2f} "
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
        for d in control.fleet():
            p = d.get("probe") or {}
            seen = (("ok" if p.get("ok") else "FAIL") + f" {when(p.get('at'))}") if p else "not probed"
            extra = ", ".join(x for x in (p.get("system", ""), " ".join(p.get("tools") or []),
                                          "; ".join(p.get("gpus") or []), p.get("detail", "")) if x)
            print(f"{d['name']:<14} {d['kind']:<9} {d.get('host') or '':<22} {seen:<18} {extra}"[:220])
            if d.get("error"):
                print(f"  {d['error']}")
        return 0
    except devices.DeviceError as e:
        return refused(e)


def cmd_session(args) -> int:
    import select
    import threading

    from . import control, sessions, state

    try:
        if args.action == "start":
            tools = [t for t in args.tools.split(",") if t] if args.tools is not None else None
            s = sessions.launch(" ".join(args.words), device=args.device, model=args.model, run_with=args.runner,
                                tools=tools, repo=args.repo, budget_usd=args.budget)
            print(f"session {s['id']}: {s['runner']} {s['model']} on {s['device']} in {paths.short_path(s['cwd'])}"
                  + (f" on {s['branch']}" if s["branch"] else "") + f"; tools {', '.join(s['tools']) or 'none'}")
            if not args.attach:
                print(f"`arete session attach {s['id']}` follows it and sends what you type")
                return 0
            args.id = s["id"]
        if args.action in ("start", "attach"):
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
                  f"{s['device']:<10} ${s['cost_usd'] or 0:.2f}  {s['prompt'][:70]}")
        if not state.runs(kind="session", limit=1):
            print("no sessions yet; `arete session start \"what to do\"`")
        return 0
    except (control.NotFound, control.Busy, control.Refused) as e:
        return refused(e)


def cmd_approve(args) -> int:
    from . import control, emits

    try:
        if args.id is None:
            pending = emits.waiting()
            for a in pending:
                print(f"#{a['id']:<4} {a['loop']:<20} {when(a['created'])}  {a['what']}"[:240])
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
        loop = control.find_loop(args.name)
    except control.NotFound as e:
        return refused(e)
    if not loop.trigger.webhook:
        print(f"{loop.name} has no webhook; set `webhook = true` under [trigger] in {paths.short_path(loop.file)}",
              file=sys.stderr)
        return 1
    key = hooks.secret(loop.name)
    node = dashboard.this_node(config.load().dashboard_owner)
    host = node.addresses[0] if node else "<this machine's tailnet address>"
    print(f"POST http://{host}:{config.load().dashboard_port}{hooks.HOOK_PATH}{loop.name}")
    print(f"X-Arete-Token: {key}")
    print("GitHub: use the same value as the webhook secret; arete checks X-Hub-Signature-256")
    return 0


def cmd_costs(args) -> int:
    from . import control

    c = control.costs(args.days)
    print(f"${c['total_usd']:.2f} in the last {args.days:g} days")
    for group in ("model", "runner", "loop"):
        print(f"by {group}:")
        for row in c[f"by_{group}"][:12]:
            steps = "step " if row["steps"] == 1 else "steps"
            print(f"  {row['name']:<36} ${row['cost_usd']:>8.2f}  {row['steps']:>5} {steps}  "
                  f"{row['tokens_in']:>10,} in  {row['tokens_out']:>9,} out")
    return 0



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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arete", description="arete: agents that keep running and keep getting better", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"arete {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write the default global config")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("status", help="settings, heartbeat, alerts, recent runs")
    p.add_argument("--limit", type=int, default=10)
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
    p.add_argument("--fire", type=int, default=None, help=argparse.SUPPRESS)
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
    p.add_argument("--runner", default="", choices=("", "claude", "pi"))
    p.add_argument("--tools", default=None, help="comma separated; Read,Edit,Write,Glob,Grep,Bash by default")
    p.add_argument("--repo", default="", help="a repo name from the config or a path; work in a new worktree of it")
    p.add_argument("--budget", type=float, default=None, help="USD for the whole session")
    p.add_argument("--attach", action="store_true", help="start: follow it at once")
    p.set_defaults(fn=cmd_session_args)

    p = sub.add_parser("approve", help="list emits waiting for approval, or approve one by id")
    p.add_argument("id", type=int, nargs="?", default=None)
    p.add_argument("--reject", action="store_true")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("webhook", help="print the URL and token for a loop's webhook")
    p.add_argument("action", choices=("secret",))
    p.add_argument("name")
    p.set_defaults(fn=cmd_webhook)

    p = sub.add_parser("costs", help="model spend by model, runner and loop")
    p.add_argument("--days", type=float, default=7)
    p.set_defaults(fn=cmd_costs)

    p = sub.add_parser("run", help="loop 3: run the pipeline on one ticket")
    p.add_argument("repo")
    p.add_argument("issue", help="an issue number (35) or a Jira key (ABC-1234)")
    p.add_argument("--dry-run", action="store_true", help="plan and build locally, no push, PR or tracker writes")
    p.add_argument("--force", action="store_true", help="run a ticket the approval rules would skip")
    p.set_defaults(fn=cmd_run)

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
