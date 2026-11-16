from __future__ import annotations

import json
import math
import time

from . import control, events, state


def serve(handler, query, code):
    try:
        return stream(handler, query, code)
    except (OSError, TimeoutError):
        handler.close_connection = True


def stream(handler, query, code):
    from .dashboard import Refusal

    try:
        epoch, seq = events.cursor(handler.headers.get("Last-Event-ID", query.get("since")))
        selected = events.filters(query)
        timeout = float(query.get("timeout", "25"))
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("invalid event timeout")
        timeout = min(25, timeout)
    except ValueError as e:
        raise Refusal(400, str(e)) from None
    accept = handler.headers.get("Accept", "")
    fmt = "json" if "application/json" in accept else "ndjson" if "application/x-ndjson" in accept else "sse"
    with state.reader(streaming=True) as c:
        instance = c.execute("SELECT value FROM schema_meta WHERE key = 'instance_id'").fetchone()[0]
        handler.send_response(200)
        handler.send_header("Content-Type", {"json": "application/json", "ndjson": "application/x-ndjson",
                                             "sse": "text/event-stream"}[fmt])
        handler.send_header("X-Arete-Code", code)
        for key, value in (("Cache-Control", "no-store"), ("X-Accel-Buffering", "no"),
                           ("Connection", "close"), ("Arete-Instance", instance)):
            handler.send_header(key, value)
        handler.end_headers()
        handler.close_connection = True
        handler.connection.settimeout(30)

        def write(value):
            handler.wfile.write(value)
            handler.wfile.flush()

        def message(kind, data, ident=None):
            if fmt == "ndjson":
                write((json.dumps(data) + "\n").encode())
            else:
                frame = (f"id: {ident}\n" if ident is not None else "")
                if kind in ("reset", "serve.beat") or query.get("named"):
                    frame += f"event: {kind}\n"
                write((frame + "data: " + json.dumps(data) + "\n\n").encode())

        started = time.monotonic()
        beat = started + 15
        try:
            if fmt == "sse":
                write(b"retry: 2000\n\n")
            while True:
                epoch, top, rows, reason, meta = events.scan(c, epoch, seq, selected)
                if reason:
                    reset = {"kind": "reset", "reason": reason}
                    if fmt == "json":
                        write(json.dumps({"events": [], "next": f"{epoch}.{seq or 0}", "reset": reset}).encode())
                    else:
                        message("reset", reset)
                    return
                if fmt == "json" and (rows or time.monotonic() - started >= timeout):
                    write(json.dumps({"events": [events.envelope(c, r, epoch) for r in rows],
                                      "next": f"{epoch}.{top}", "reset": None}).encode())
                    return
                if fmt != "json":
                    for row in rows:
                        message(row["kind"], events.envelope(c, row, epoch), f"{epoch}.{row['seq']}")
                        if time.monotonic() >= beat:
                            message("serve.beat", {**control.serve_beat(), "kind": "serve.beat", "code": code,
                                                   "head": meta["head"]}, f"{epoch}.{seq if seq is not None else top}")
                            beat = time.monotonic() + 15
                seq = top
                if fmt != "json" and time.monotonic() >= beat:
                    message("serve.beat", {**control.serve_beat(), "kind": "serve.beat", "code": code,
                                           "head": meta["head"]}, f"{epoch}.{seq}")
                    beat = time.monotonic() + 15
                time.sleep(0.25)
        except (OSError, TimeoutError):
            return
