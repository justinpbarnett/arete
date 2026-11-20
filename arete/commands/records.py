from __future__ import annotations

import json
import sys
from contextlib import closing

from .. import config, paths
from .common import print_lines


def cmd_backup(args) -> int:
    from .. import backup

    return backup.command(args.check, args.restore)


def cmd_document(args) -> int:
    from pathlib import Path

    from .. import document, outbound

    try:
        raw = sys.stdin.read() if args.brief == "-" else Path(args.brief).read_text()
        data = json.loads(raw)
        if args.draft and isinstance(data, dict):
            data["draft"] = Path(args.draft).read_text()
        brief = document.brief_from(data)
        cfg = config.load()
        repo = cfg.repo(args.repo) if args.repo else None
        layer = config.load_layer(repo.layer_dir) if repo else None
        written = document.write(brief, cfg, repo=repo, layer=layer)
    except (ValueError, TypeError, OSError, config.UnknownRepo) as e:
        print(f"arete document: {e}", file=sys.stderr)
        return 2
    if args.text:
        sys.stdout.write(written.text if written.text.endswith("\n") else written.text + "\n")
    else:
        shown = {"kind": brief.kind, "source": written.source, "title": written.title, "text": written.text,
                 "words": written.words, "target": written.target,
                 "over_target": bool(written.target and written.words > written.target[1]), "kept": written.kept,
                 "faults": list(written.faults), "tries": written.tries}
        if brief.kind != "doc":
            shown["screened"] = outbound.screen(written.text)
        print(json.dumps(shown))
    return 0 if written.source == "writer" else 1


def cmd_record(args) -> int:
    from .. import client, record

    if args.run_id in ("pin", "unpin"):
        if not args.step:
            print("usage: arete record pin|unpin RUN_ID", file=sys.stderr)
            return 2
        client.call("POST", client.run_path(args.step, args.run_id, prefix="/api/v1"))
        print(f"pinned {args.step}: its record stays until `arete record unpin {args.step}`" if args.run_id == "pin"
              else f"unpinned {args.step}: its record ages out like any other")
        return 0
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


def cmd_secrets(args) -> int:
    from .. import vault

    words = args.words
    if len(words) == 1:
        names = sorted(vault.read(config.load().repo(words[0]).name))
        print("\n".join(names), end="\n" if names else "")
        if not names:
            print(f"{words[0]} has no stored secrets", file=sys.stderr)
        return 0
    if len(words) == 3 and words[0] == "rm":
        repo = config.load().repo(words[1])
        if vault.remove(repo.name, words[2]):
            print(f"removed {words[2]} from {words[1]}")
            return 0
        print(f"{words[1]} has no stored secret {words[2]}", file=sys.stderr)
        return 1
    print("usage: arete secrets REPO | arete secrets rm REPO NAME", file=sys.stderr)
    return 2


def cmd_rotate_authority(args) -> int:
    from .. import scratch

    try:
        records, pins = scratch.rotate()
    except scratch.RotationRefused as e:
        for line in str(e).splitlines():
            print(f"refused: {line}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"cannot rotate the verification signing secret: {e}", file=sys.stderr)
        return 1
    print(f"re-signed records: {records}, pins: {pins}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("backup", help="snapshot arete.db, check the newest snapshot, or restore one with serve stopped")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check the newest snapshot and compare its row counts with the live file")
    mode.add_argument("--restore", metavar="FILE", help="replace arete.db with a snapshot; refused while serve or a run is live")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("record", help="a run's timeline and record files, or one agent call's record as normalized "
                                      "events; `pin RUN_ID` and `unpin RUN_ID` keep a record from rotating out")
    p.add_argument("run_id", help="a run id, or pin RUN_ID or unpin RUN_ID")
    p.add_argument("step", nargs="?", default="", help="a record file name, with or without .jsonl.gz")
    p.add_argument("--raw", action="store_true", help="print the recorded lines as they were written")
    p.add_argument("-f", "--follow", action="store_true", help="follow a STEP until its Arete result or the run ends")
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("document", help="write a short message, ticket, PR text or doc edit from a JSON brief of "
                                        "verified facts; prints one JSON line")
    p.add_argument("brief", metavar="BRIEF", help="a JSON file, or - for standard input")
    p.add_argument("--repo", default="", help="use this repo's layer, its [document] settings and voice files")
    p.add_argument("--draft", default="", metavar="FILE", help="an existing text to keep, edited as little as possible")
    p.add_argument("--text", action="store_true", help="print the text alone")
    p.set_defaults(fn=cmd_document)

    p = sub.add_parser("secrets", help="list the names of the secrets stored for a repo (REPO), or remove one "
                                       "(rm REPO NAME); it never prints a value")
    p.add_argument("words", nargs="+", metavar="[rm] REPO [NAME]")
    p.set_defaults(fn=cmd_secrets)

    p = sub.add_parser("rotate-authority", help="re-sign every scratch record and the browser pin under a new "
                                                "verification signing secret, then swap it in; refuses while a run, "
                                                "an attempt lease or an engine lock is live")
    p.set_defaults(fn=cmd_rotate_authority)
