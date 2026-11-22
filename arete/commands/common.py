from __future__ import annotations

import os
import sys
from collections.abc import Iterable

from ..codebase import serve_revisions as serve_revisions
from .arguments import Parser as Parser
from .arguments import status_arguments as status_arguments
from .arguments import steering as steering


def when(stamp: str | None) -> str:
    from .. import util

    return util.parse_iso(stamp).astimezone().strftime("%m-%d %H:%M") if stamp else "-"


def print_lines(lines: Iterable[str], *, flush: bool = False) -> None:
    try:
        for line in lines:
            print(line, flush=flush)
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


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
    from .. import util

    if runs:
        print("recent runs:")
        print_table([(r["id"], r["kind"], r["ticket"], r["status"], util.paid_note(r["paid"]), r["pr_url"] or r["summary"])
                     for r in runs], 200)


def print_records(s: dict, limits) -> None:
    from .. import record, util

    kept = f" (kept {limits.days:g} days, up to {limits.max_gb:g} GB)" if limits else ""
    if not s["oldest"]:
        print(f"records: none{kept}, {s['pins']} pinned")
        return
    print(f"records: {record.size_text(s['bytes'])} in {util.count(s['records'], 'record')}{kept}, "
          f"oldest {s['oldest']['run_id']} written {when(s['oldest']['written'])}, {s['pins']} pinned")


def print_proposals(p: dict) -> None:
    if p["memory"]:
        print("memory facts the miner noticed (file them by hand, then `arete forget ID`):")
    for m in p["memory"]:
        print(f"  {m['id']:<4} {m['change']}"[:200])
    if p["hand"]:
        print("miner proposals to review by hand (use `arete forget ID` when finished):")
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
    from .. import util

    if w["tickets"]:
        print(f"your PR comments waiting for a revision (a submitted review goes at once, other comments after "
              f"{w['quiet_minutes']} quiet minutes):")
    for t in w["tickets"]:
        print(f"  {t['ticket']:<24} {util.count(t['comments'], 'comment')}, "
              + ("review submitted" if t["at_once"] else f"last comment {t['last']}"))


def print_triage(t: dict) -> None:
    from .. import triage

    if not t["refreshed"]:
        print("triage: never refreshed (`arete triage --refresh`)")
        return
    errors = f"; FAILED {'; '.join(t['errors'])}" if t["errors"] else ""
    print(f"triage: {t['summary']}; refreshed {triage.stamp(t['refreshed'])}; board {t['posted']}{errors}"[:300])


def print_live(live: dict) -> None:
    from .. import util

    print(f"runs live: {len(live['runs'])}, in {live['slots']} of {live['max_parallel']} run slots"
          + (f", {live['waiting']} waiting for one" if live["waiting"] else ""))
    if live["runs"]:
        print_table([(r["id"], r["kind"], r["ticket"], f"since {when(r['started'])}", util.paid_note(r["paid"]),
                      r["slot"] or r["step"]) for r in live["runs"]], 200)


def serve_line(beat: dict) -> str:
    from .. import util

    if not beat:
        return "serve: never ran; `arete install` starts it"
    if not beat["alive"]:
        return f"serve: DOWN, {'its process is gone, ' if beat['process_gone'] else ''}last beat {when(beat.get('at'))}"
    paused = f"; config does not load: {beat['config_problem']}" if beat.get("config_problem") else ""
    paused +=f"; new code paused: {beat['code_problem']}" if beat.get("code_problem") else ""
    failing = "".join(f"; tick fails at {key}: {found['error']}" for key, found in (beat.get("failing") or {}).items())
    code = serve_revisions(beat)
    running, pending = ("unknown" if beat[k] is None else beat[k] for k in ("running", "pending"))
    return (f"serve: up, pid {beat['pid']}, {util.count(beat['loops'], 'loop')} on, {beat['off']} off, "
            f"{running} running, {pending} waiting"
            + (f", {len(beat['broken'])} broken" if beat.get("broken") else "")
            + (f", {code}" if code else "") + paused + failing)


def loop_line(v: dict) -> str:
    from .. import util

    switch = "BROKEN" if v["error"] else ("on" if v["enabled"] else "off")
    last = v["last"] or {}
    what = (f"running {v['running']}" if v["running"] else
            f"{last.get('status', 'never ran')} {when(last.get('finished') or last.get('started'))}" if last
            else "never ran")
    due = f"next {when(v['next_due'])}" if v.get("next_due") else ""
    dead = len(v["dead"]) if isinstance(v.get("dead"), list) else v.get("dead", 0)
    return (f"{v['name']:<22} {switch:<6} {v['kind']:<7} {v['trigger']:<22} {what:<26} {due:<17}"
            f" {util.paid_note(v['spent_24h'])} in 24 h" + (f", {dead} dead" if dead else "")
            + (f"\n    {v['error']}" if v["error"] else ""))


def feed_line(line: dict) -> str:
    from .. import client

    text = client.field(line, "text", "feed.lines.text", kind=str).replace("\n", "\n    ")
    try:
        stamp = when(line["ts"])
    except (TypeError, ValueError) as e:
        raise client.Failed({"error": "server answer has an invalid field feed.lines.ts"}) from e
    return f"{stamp[6:]} [{line['kind']}] {text}"


def print_feed_line(line: dict) -> None:
    print(feed_line(line), flush=True)


def follow(run_id: str, alive=None, after: int = 0, send=None, *, remote=False, on_start=None) -> int:
    import time
    from urllib.parse import quote

    from .. import client, control

    epoch = None

    def drain():
        nonlocal after, epoch, on_start
        while True:
            found = (client.call("GET", f"/api/feed/{quote(run_id, safe='')}?after={after}") if remote
                     else control.feed(run_id, after))
            current = client.field(found, "restore_epoch", kind=int)
            if epoch != current:
                changed = epoch is not None
                epoch = current
                if changed:
                    after = 0
                    continue
            status = (client.field(client.run_info(run_id), "status", "run.status", kind=str) if remote
                      else (found["run"] or {}).get("status"))
            lines = [(client.field(line, "id", "feed.lines.id", kind=int), feed_line(line))
                     for line in client.field(found, "lines", kind=list)]
            if on_start is not None:
                on_start()
                on_start = None
            for after, rendered in lines:
                print(rendered, flush=True)
            return status == "running"

    while True:
        running = drain()
        if not (alive() if alive else running):
            drain()
            return after
        if send:
            send()
        time.sleep(1)
