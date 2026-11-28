from __future__ import annotations

import json

from . import config, loops, senders, sources


class LoopPermanentError(RuntimeError):
    pass


class LoopTemporaryError(RuntimeError):
    pass


class EmailUnavailable(RuntimeError):
    pass


def parse_loop_payload(body: str) -> dict[str, object]:
    try:
        parsed = json.loads(body)
    except ValueError as error:
        raise ValueError(f"loop payload must be JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("loop payload must be a JSON object")
    target_loop = str(parsed.get("loop") or "").strip()
    event = parsed.get("event")
    if not target_loop or not isinstance(event, dict):
        raise ValueError("loop payload requires 'loop' and 'event'")
    return {
        "loop": target_loop,
        "from_loop": str(parsed.get("from_loop") or ""),
        "event": event,
    }


def deliver_in_tx(c, item: dict, body: str) -> senders.SendResult:
    payload = parse_loop_payload(body)
    cfg = config.load()
    target_name = str(payload["loop"])
    from_loop = str(payload["from_loop"] or item.get("loop") or "outbox")
    event = dict(payload["event"])
    try:
        target = loops.find(target_name, cfg)
    except (FileNotFoundError, ValueError) as error:
        raise LoopPermanentError(str(error)) from error
    if target.error:
        raise LoopTemporaryError(f"the loop {target.name} is off: {target.error}")
    if why := target.trigger.refusal(event):
        raise LoopPermanentError(f"{target.name} {why}")
    if not target.takes_findings:
        raise LoopPermanentError(f'{target.name} takes findings only with [trigger] event = ["{loops.FINDING_EVENT}"]')
    if not target.enabled:
        raise LoopTemporaryError(f"the loop {target.name} is off")
    queued = sources.fire(target, f"finding from {from_loop}", [event])
    queue_id = str(queued[0]) if queued else f"{target.name}:{event.get('id', 'queued')}"
    return senders.SendResult(external_id=queue_id, external_url=f"loop:{target.name}#{queue_id}")


def _loop_send(item: dict, body: str) -> senders.SendResult:
    return deliver_in_tx(None, item, body)


def _loop_look(item: dict, since_iso: str) -> senders.LookResult:
    return senders.LookResult(
        found=False,
        external_id=None,
        external_url=None,
        summary="loop delivery is transactional; no external lookup needed",
    )


def _loop_preview(item: dict, body: str) -> str:
    payload = parse_loop_payload(body)
    title = str(payload["event"].get("title") or "")
    return f"loop {payload['loop']}: {title}".strip()


def _email_send(item: dict, body: str) -> senders.SendResult:
    raise EmailUnavailable("no email connector")


def _email_look(item: dict, since_iso: str) -> senders.LookResult:
    return senders.LookResult(
        found=False,
        external_id=None,
        external_url=None,
        summary="no email connector",
    )


def _email_preview(item: dict, body: str) -> str:
    return body


def register_all() -> None:
    senders.register(senders.Sender("loop", "loop", _loop_send, _loop_look, _loop_preview))
    senders.register(senders.Sender("email", "email", _email_send, _email_look, _email_preview))
