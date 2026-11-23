from __future__ import annotations

import json
import math
import time

from . import control, events
from .credential_http import StreamEpochChanged, problem
from .store import db as foundation
from .store.tokens import STREAM_EPOCH_CHANGED


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
    def start(content_type, instance, head, length=None):
        handler.read_seq = head
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("X-Arete-Code", code)
        if length is not None:
            handler.send_header("Content-Length", str(length))
        for key, value in (("Cache-Control", "no-store"), ("X-Accel-Buffering", "no"),
                           ("Connection", "close"), ("Arete-Instance", instance)):
            handler.send_header(key, value)
        handler.stream_headers()
        handler.connection.settimeout(30)

    started = time.monotonic()
    if fmt == "json":
        while True:
            if issue := handler.stream_problem():
                raise problem("unauthenticated" if issue == STREAM_EPOCH_CHANGED else issue)
            if handler.verify_controls is not None:
                handler.verify_controls.poll()
            with foundation.one_read() as c:
                instance = foundation.metadata(c)["instance_id"]
                epoch, top, rows, reason, meta = events.scan(c, epoch, seq, selected)
                if reason:
                    data = {"events": [], "next": f"{epoch}.{seq or 0}", "reset": {"kind": "reset", "reason": reason}}
                elif rows or time.monotonic() - started >= timeout:
                    data = {"events": [events.envelope(c, row, epoch) for row in rows], "next": meta["head"], "reset": None}
                else:
                    data = None
            if data is not None:
                block = json.dumps(data).encode()
                start("application/json", instance, meta["head"], len(block))
                handler.stream_write(block)
                return
            seq = top
            time.sleep(min(.25, max(0, timeout - (time.monotonic() - started))))

    with foundation.one_read() as c:
        instance = foundation.metadata(c)["instance_id"]
        head = events.metadata(c)["head"]
    if epoch is None:
        epoch, head_seq = events.cursor(head)
        if fmt == "sse" and seq is None:
            seq = head_seq
    with handler.event_connection() as dropped, foundation.reader(streaming=True) as c:
        start("application/x-ndjson" if fmt == "ndjson" else "text/event-stream", instance, head)

        def message(kind, data, ident=None):
            if fmt == "ndjson":
                frame = json.dumps(data) + "\n"
            else:
                frame = (f"id: {ident}\n" if ident is not None else "")
                if kind in ("reset", "serve.beat") or query.get("named"):
                    frame += f"event: {kind}\n"
                frame += "data: " + json.dumps(data) + "\n\n"
            handler.stream_write(frame.encode(), epoch_reset=kind == "reset" and data["reason"] == "epoch")

        beat = started + 15
        try:
            if fmt == "sse":
                handler.stream_write(b"retry: 2000\n\n")
            while dropped is None or not dropped():
                handler.check_stream_credential()
                epoch, top, rows, reason, meta = events.scan(c, epoch, seq, selected)
                if reason:
                    message("reset", {"kind": "reset", "reason": reason})
                    return
                for row in rows:
                    handler.check_stream_credential()
                    position = f"{epoch}.{row['seq']}"
                    message(row["kind"], events.envelope(c, row, epoch), position)
                    if time.monotonic() >= beat:
                        message("serve.beat", {**control.serve_beat(), "kind": "serve.beat", "code": code,
                                               "head": meta["head"]}, position)
                        beat = time.monotonic() + 15
                seq = top
                if time.monotonic() >= beat:
                    message("serve.beat", {**control.serve_beat(), "kind": "serve.beat", "code": code,
                                           "head": meta["head"]}, f"{epoch}.{seq}")
                    beat = time.monotonic() + 15
                time.sleep(0.25)
        except StreamEpochChanged:
            message("reset", {"kind": "reset", "reason": "epoch"})
        except (OSError, TimeoutError):
            return
