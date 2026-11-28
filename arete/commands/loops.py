from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import quote

from .. import client, config, paths
from ..store import db as foundation
from ..store import loops as loop_store
from ..store import runs as run_store
from .common import Parser, follow, loop_line, print_feed_line, print_table, serve_line, when


def events_from_file(file: str) -> list[dict] | None:
    from .. import control

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
    from .. import control

    if run_store.run_get(name_or_run, connect=foundation.reader):
        return name_or_run
    loop = control.find_loop(name_or_run)
    last = next(iter(run_store.runs(kind="loop", loop=loop.name, limit=1, firings=True, connect=foundation.reader)), None)
    if not last:
        raise control.NotFound(f"{loop.name} has not run yet")
    return last["id"]


def cmd_serve(args) -> int:
    from .. import serve

    serve.run(with_dashboard=not args.no_dashboard)


def loop_requeue(args) -> int:
    from .. import control

    try:
        item_id = int(args.id)
    except ValueError:
        raise control.Refused("arete loop requeue needs a numeric queue ID") from None
    with foundation.tx() as c:
        loop_store.requeue_item(c, item_id)
    print(f"queue item {item_id} is pending again")
    return 0


def loop_list(args) -> int:
    from .. import control

    print(serve_line(control.serve_beat()))
    for v in control.loop_views():
        print(loop_line(v))
    return 0


def loop_new(args) -> int:
    if args.run_id:
        from .. import control, scaffold, util

        control.require_worker("loop new")
        made = scaffold.draft(scaffold.DraftRequest(" ".join(args.words), args.name, args.repo, args.model),
                              run_id=args.run_id)
        print(f"drafted {made['name']} in {paths.short_path(made['dir'])} (off, {util.paid_note(made['paid'])})\n")
        print(made["loop_toml"])
        if made["note"]:
            print(f"note: {made['note']}")
        print(f"next: `arete loop try {made['name']}`, then `arete loop enable {made['name']}`")
        return 0
    res = client.call("POST", "/api/v1/loops", {
        "sentence": " ".join(args.words),
        "name": args.name or "",
        "repo": args.repo or "",
        "model": args.model or "",
    })
    run = client.field(res, "run")
    print(f"started draft {run['id']}")
    return 0


def loop_exec(args) -> int:
    from .. import control, loop_run

    control.require_worker("loop exec")
    return 0 if loop_run.carry_opened(args.run_id) in ("done", "skipped") else 1


def loop_try(args) -> int:
    body: dict[str, object] = {}
    if args.events:
        events = events_from_file(args.events)
        raw = json.dumps(events).encode("utf-8")
        body["events"] = client.put_blob(raw)
    res = client.call("POST", f"/api/v1/loops/{quote(args.name, safe='')}/try", body)
    run = client.field(res, "run")
    run_id = run["id"]
    follow(run_id, remote=True)
    ended = client.run_info(run_id)
    status = ended.get("status") or run.get("status") or "unknown"
    print(f"\ntry {run_id}: {status}, every emit held")
    return 0 if status in ("done", "skipped") else 1


def loop_switched(args, enabled: bool) -> int:
    done = client.call("POST", f"/api/v1/loops/{quote(args.name, safe='')}/{'enable' if enabled else 'disable'}")
    loop = client.field(done, "loop")
    print(f"{loop['name']}: {'on' if loop['enabled'] else 'off'}")
    return 0


def loop_enable(args) -> int:
    return loop_switched(args, True)


def loop_disable(args) -> int:
    return loop_switched(args, False)


def loop_start(args) -> int:
    body: dict[str, object] = {}
    if args.events:
        events = events_from_file(args.events)
        body["events"] = client.put_blob(json.dumps(events).encode("utf-8"))
    r = client.call("POST", f"/api/v1/loops/{quote(args.name, safe='')}/start", body)
    run = r["run"] if isinstance(r.get("run"), dict) else r
    run_id = run["id"]
    if args.follow:
        follow(run_id, remote=True, on_start=lambda: print(f"started {run_id}", flush=True))
    else:
        print(f"started {run_id}")
    return 0


def loop_stop(args) -> int:
    try:
        loop = client.call("GET", f"/api/loops/{quote(args.name, safe='')}")
    except client.Failed as e:
        if e.code != 404:
            raise
        return client.run_command(args.name, "stop")
    if not (target := client.field(loop, "running")):
        print("not running")
        return 0
    return client.run_command(target, "stop", key_scope=target)


def loop_status(args) -> int:
    from .. import control, util

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
    for item in d["dead"]:
        inbox_note = f", inbox {item['inbox_id']}" if item.get("inbox_id") else ""
        print(f"  dead item {item['id']}: run {item['run_id']}, {util.count(item['events'], 'event')}{inbox_note}, "
              f"{item['dead_at']}: {item['dead_reason']}")
    if d["state"]:
        print(f"  memory: {json.dumps(d['state'])[:300]}")
    for r in d["runs"][:8]:
        print(f"  {r['id']:<34} {r['status']:<11} {r['trigger'] or '':<8} {util.paid_note(r['paid']):<12} "
              f"{util.first_line(r['summary'] or '', 90)}")
    for f in d["findings"][:8]:
        print(f"  finding {f['title'][:90]} {f['url'] or ''}")
    return 0


def loop_feed(args) -> int:
    run_id = latest_run(args.name)
    r = run_store.run_get(run_id, connect=foundation.reader)
    print(f"{run_id} {r['status']} {r.get('loop') or r['kind']}")
    if args.follow:
        follow(run_id)
    else:
        for line in run_store.feed(run_id, connect=foundation.reader):
            print_feed_line(line)
    return 0


def probe_words(probe: dict | None) -> tuple[str, str]:
    if not probe:
        return "not probed", ""
    seen = ("ok" if probe.get("ok") else "FAIL") + f" {when(probe.get('at'))}"
    return seen, ", ".join(x for x in (probe.get("system", ""), " ".join(probe.get("tools") or []),
                                       "; ".join(probe.get("gpus") or []), probe.get("detail", "")) if x)


def devices_list(args) -> int:
    from .. import api

    if getattr(args, "probe", ""):
        args.name = args.probe
        return devices_probe(args)
    if client.configured_server():
        reply = client.call("GET", "/api/v1/devices")
    else:
        try:
            reply = client.call("GET", "/api/v1/devices")
        except client.Down:
            reply = api.execute("device.list", {})
    fleet = reply.get("devices") or []
    print_table([(d["id"], d["kind"], d.get("host") or "", *probe_words(d.get("last_probe"))) for d in fleet], 220,
                indent="")
    return 0


def _follow_device_probe(watch: str, name: str) -> dict:
    sep = "&" if "?" in watch else "?"
    for _ in range(12):
        resp = client.request("GET", f"{watch}{sep}timeout=10", buffered=True,
                              extra_headers={"Accept": "application/json"})
        payload = client.decoded(resp, "GET", watch)
        for ev in payload.get("events") or []:
            if ev.get("kind") == "device.changed":
                data = ev.get("data") or {}
                if isinstance(data.get("probe"), dict):
                    return data["probe"]
                got = client.call("GET", f"/api/v1/devices/{quote(name, safe='')}")
                return got.get("device", {}).get("last_probe") or {"ok": bool(data.get("ok"))}
    got = client.call("GET", f"/api/v1/devices/{quote(name, safe='')}")
    return got.get("device", {}).get("last_probe") or {"ok": False, "detail": "probe timed out"}


def devices_probe(args) -> int:
    name = args.name
    path = f"/api/v1/devices/{quote(name, safe='')}/probe"
    reply = client.call("POST", path, {}, extra_headers={"Prefer": "wait=10"})
    if reply.get("accepted") and reply.get("watch"):
        info = _follow_device_probe(reply["watch"], name)
    else:
        info = reply.get("probe") or {}
    print(json.dumps(info, indent=2))
    return 0 if info.get("ok") else 1


def cmd_webhook(args) -> int:
    from .. import api, scrub, tailnet

    path = f"/api/v1/loops/{quote(args.name, safe='')}"
    if args.rotate:
        reply = client.call("POST", path + "/webhook/rotate")
        print(client.field(reply, "text", kind=str), file=scrub.raw_stream(sys.stdout))
        return 0
    if remote := client.configured_server():
        reply = client.call("GET", path)
        base = client.address(remote)
    else:
        reply = api.execute("loop.get", {"id": args.name})
        base = tailnet.url(tailnet.this_node(), config.load().dashboard_port)
    detail = client.field(client.field(reply, "loop"), "detail")
    if not (hook := detail.get("webhook_path")):
        print(f"{args.name} has no webhook; set `webhook = true` under [trigger]", file=sys.stderr)
        return 1
    print(f"POST {base}{hook}")
    print(f"secret_present: {json.dumps(bool(detail.get('secret_present')))}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("loop", help="list, draft, try, switch, start, stop and follow loops")
    lsub = p.add_subparsers(dest="action", required=True, parser_class=Parser)
    a = lsub.add_parser("list", help="the serve beat and every loop")
    a.set_defaults(fn=loop_list, local_read=True)
    a = lsub.add_parser("new", help="draft a loop from one sentence")
    a.add_argument("words", nargs="+", metavar="WORDS", help="one sentence on what the loop does")
    a.add_argument("--name", default="", help="the loop name")
    a.add_argument("--repo", default="", help="the repo the loop works on")
    a.add_argument("--model", default="", help="the model that drafts it")
    a.add_argument("--run-id", default="", help=argparse.SUPPRESS)
    a.set_defaults(fn=loop_new)
    a = lsub.add_parser("try", help="run a loop once with every emit held")
    a.add_argument("name", metavar="NAME", help="a loop name")
    a.add_argument("--events", default="", metavar="FILE", help="a JSON list of events to feed it")
    a.set_defaults(fn=loop_try)
    a = lsub.add_parser("enable", help="switch a loop on")
    a.add_argument("name", metavar="NAME", help="a loop name")
    a.set_defaults(fn=loop_enable)
    a = lsub.add_parser("disable", help="switch a loop off")
    a.add_argument("name", metavar="NAME", help="a loop name")
    a.set_defaults(fn=loop_disable)
    a = lsub.add_parser("start", help="start a loop run now")
    a.add_argument("name", metavar="NAME", help="a loop name")
    a.add_argument("--events", default="", metavar="FILE", help="a JSON list of events to feed it")
    a.add_argument("-f", "--follow", action="store_true", help="keep printing until the run ends")
    a.set_defaults(fn=loop_start)
    a = lsub.add_parser("stop", help="stop a loop's live run")
    a.add_argument("name", metavar="NAME", help="a loop name, or a run id")
    a.set_defaults(fn=loop_stop)
    a = lsub.add_parser("status", help="one loop in detail")
    a.add_argument("name", metavar="NAME", help="a loop name")
    a.set_defaults(fn=loop_status, local_read=True)
    a = lsub.add_parser("feed", help="the feed of a loop's latest run, or of a run id")
    a.add_argument("name", metavar="NAME", help="a loop name, or a run id")
    a.add_argument("-f", "--follow", action="store_true", help="keep printing until the run ends")
    a.set_defaults(fn=loop_feed, local_read=True)
    a = lsub.add_parser("exec", help="run a started loop run; serve and `arete loop start` call it")
    a.add_argument("--run-id", required=True, help="the run to carry out")
    a.set_defaults(fn=loop_exec)
    a = lsub.add_parser("requeue", help="make a dead queue item pending again")
    a.add_argument("id", metavar="ID", help="a queue item number")
    a.set_defaults(fn=loop_requeue)

    p = sub.add_parser("serve", help="the daemon: fire loops on their triggers, run the queues, serve the dashboard")
    p.add_argument("--no-dashboard", action="store_true")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("devices", help="the devices loops and sessions run on, with their last probe")
    p.add_argument("--probe", metavar="NAME", default="", help="probe one device through serve and print what it found")
    dsub = p.add_subparsers(dest="action", required=False, parser_class=Parser)
    p.set_defaults(fn=devices_list, local_read=lambda args: not args.probe and not client.configured_server())
    a = dsub.add_parser("probe", help="probe one device and print what it found")
    a.add_argument("name", metavar="NAME", help="a device name")
    a.set_defaults(fn=devices_probe, local_read=False)

    p = sub.add_parser("webhook", help="show a loop's webhook status, or rotate its secret")
    p.add_argument("name", metavar="NAME")
    p.add_argument("--rotate", action="store_true", help="mint a new secret and show it once")
    p.set_defaults(fn=cmd_webhook, local_read=lambda args: not args.rotate and not client.configured_server())
