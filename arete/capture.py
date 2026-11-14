import hashlib
import json
import math
import os
import re
import shutil
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from . import (
    __version__,
    chat,
    config,
    gh,
    jev,
    outbound,
    paths,
    pipeline,
    runner,
    state,
    steps,
    tracker,
    util,
    verdicts,
)

MB = 1024 * 1024
MOST_IMAGE_BYTES = 5 * MB
MOST_VIDEO_BYTES = 25 * MB
MOST_ITEMS = 6
MOST_DRAFT_BYTES = 40 * MB
MOST_VIDEO_SECONDS = 30
MOST_NAME = 120
SOURCES = ("screenshot", "recording", "attached")
CAPTURES_DIR = paths.STATE_DIR / "captures"
FILES = threading.Lock()
MP4_BRANDS = {b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"dash", b"mmp4"}
NOT_EVIDENCE = "Evidence must be a PNG, JPEG or WebP image or a WebM or MP4 video, and this file is none of them."

KINDS = ("idea", "adjustment", "bug")
LIMITS = {"text": 4000, "expected": 2000, "actual": 2000, "steps": 2000}
MOST_CONTEXT_BYTES = 8 * 1024
MOST_ID = 120
VIEWS = ("live", "runs", "sessions", "loops", "triage", "outcomes", "health", "settings", "usage")
ITEM_KINDS = ("run", "session", "loop", "ticket")
RUN_ID = re.compile(r"\w[\w.-]*")
RUN_FACTS = ("kind", "status", "started", "finished")
EMPTY_DRAFT = {"kind": "idea", "text": "", "expected": "", "actual": "", "steps": ""}

CHECKS = ("vision", "duplicates", "actionable", "injection", "publication")
TITLES = {"vision": "Vision and scope", "duplicates": "Duplicates", "actionable": "Actionability",
          "injection": "Untrusted instructions", "publication": "Publication safety"}
SUBMITTING = threading.Lock()
DUPLICATES_FRESH_SECONDS = 600
MOST_ISSUES = 100
MOST_ISSUE_BODY = 6000
INTERRUPTED = "The dashboard restarted before this check finished. Submit again."
READY = "This request passed every check and is ready. Filing arrives with part 5, so nothing was filed."
VISION_QUESTION = ("Does the request fit what the app is for, according to the product vision? Pick whether the "
                   "vision supports the request, conflicts with it, or does not address it.")
VISION_OPTIONS = {"supports": "the vision supports the request", "conflicts": "the vision conflicts with the request",
                  "not_addressed": "the vision says nothing about the request"}
DUPLICATE_QUESTION = ("Does the existing issue ask for the same change or report the same problem as the new "
                      "request?")
ACTIONABLE_QUESTION = ("Could a developer start work on this request as written? Pick actionable, or the one detail "
                       "that is most missing.")
ACTIONABLE_OPTIONS = {"actionable": "a developer could start on it as written",
                      "what": "it does not say what should change", "where": "it does not say where it happens",
                      "expected": "it does not say what was expected", "reproduce": "it does not say how to reproduce it"}
MISSING = {"what": "what should change", "where": "where it happens", "expected": "what was expected",
           "reproduce": "how to reproduce it"}
SECRETS_QUESTION = ("Does this text hold a credential, such as a password, token or key, or private personal "
                    "information, such as an email address, phone number, home address or a person's private details?")
FLAGS = {"credential": "a credential", "personal": "private personal information",
         "private_address": "a private address", "agent_text": "text addressed to an agent"}
LEAKS = ("credential", "personal", "private_address")
MEDIA = ("image", "video")
READS_EVIDENCE = ("injection", "publication")
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "video/webm": "webm", "video/mp4": "mp4"}
EVIDENCE_SCHEMA = {"type": "object",
                   "properties": {"viewed": {"type": "boolean"}, **{k: {"type": "boolean"} for k in FLAGS},
                                  "saw": {"type": "string"}},
                   "required": ["viewed", *FLAGS, "saw"]}
NOT_INSPECTED_YET = "Not inspected yet. Submit inspects it."
ROUTE_CHANGED = "The model or data handling it was inspected under has changed. Submit inspects it again."


class Refused(ValueError):
    pass


class Conflict(RuntimeError):
    pass


class TooLarge(Refused):
    pass


class NotStaged(LookupError):
    pass


def view(connect=state.db) -> dict:
    limits = {**LIMITS, "context_bytes": MOST_CONTEXT_BYTES, "image_bytes": MOST_IMAGE_BYTES, "video_bytes": MOST_VIDEO_BYTES,
              "evidence_items": MOST_ITEMS, "evidence_bytes": MOST_DRAFT_BYTES, "video_seconds": MOST_VIDEO_SECONDS}
    draft = state.capture_draft(connect)
    return {"draft": shown(draft) if draft else None, "limits": limits, "kinds": list(KINDS)}


def sniffed(data: bytes) -> tuple[str, str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg", "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "image/webp", "webp"
    if data.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in data[:64]:
        return "video", "video/webm", "webm"
    if data[4:8] == b"ftyp":
        size = int.from_bytes(data[:4], "big")
        head = data[8:min(max(size, 16), 256)]
        brands = {head[:4], *(head[i:i + 4] for i in range(8, len(head) - 3, 4))}
        if brands & MP4_BRANDS:
            return "video", "video/mp4", "mp4"
    raise Refused(NOT_EVIDENCE)


def megabytes(size: int) -> str:
    return f"{size / MB:.1f} MB"


def checked_seconds(raw: str) -> float | None:
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or not 0 <= value <= 86400:
        raise Refused("The length must be a number of seconds from 0 to 86,400.")
    return round(value, 1)


def stage(data: bytes, name: str, source: str, seconds: str) -> dict:
    source = source or "attached"
    if source not in SOURCES:
        raise Refused(f"The source must be one of {', '.join(SOURCES)}.")
    length = checked_seconds(seconds)
    kind, mime, extension = sniffed(data)
    most, label = (MOST_IMAGE_BYTES, "An image") if kind == "image" else (MOST_VIDEO_BYTES, "A video")
    if len(data) > most:
        raise TooLarge(f"{label} is limited to {most // MB} MB and this one has {megabytes(len(data))}.")
    digest = hashlib.sha256(data).hexdigest()
    row = {"name": tracker.safe_filename(name)[:MOST_NAME] if name.strip() else f"{source}.{extension}", "kind": kind,
           "type": mime, "bytes": len(data), "sha256": digest, "source": source,
           "seconds": length if kind == "video" else None}

    def admit(held: list[dict]) -> None:
        if len(held) >= MOST_ITEMS:
            raise Refused(f"A draft holds at most {MOST_ITEMS} items and this one has {len(held)}. Remove one first.")
        used = sum(r["bytes"] for r in held)
        if used + len(data) > MOST_DRAFT_BYTES:
            raise TooLarge(f"A draft holds at most {MOST_DRAFT_BYTES // MB} MB of evidence; it has {megabytes(used)} "
                           f"and this file adds {megabytes(len(data))}.")

    def keep(draft_id: int) -> None:
        folder = CAPTURES_DIR / str(draft_id)
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / f".{digest}.{os.getpid()}.part"
        temp.write_bytes(data)
        os.replace(temp, folder / digest)

    with FILES:
        staged, existing = state.capture_stage(row, admit, keep)
    return {**view(), "evidence": staged, "existing": existing}


def staged_file(evidence_id: int) -> tuple[Path, dict]:
    row = state.capture_evidence(evidence_id, state.reader)
    path = CAPTURES_DIR / str(row["capture"]) / row["sha256"] if row else None
    if not path or not path.is_file():
        raise NotStaged(f"No staged evidence has the id {evidence_id}.")
    return path, row


def drop_evidence(evidence_id: int) -> dict:
    with FILES:
        row = state.capture_drop_evidence(evidence_id)
        if row:
            (CAPTURES_DIR / str(row["capture"]) / row["sha256"]).unlink(missing_ok=True)
    return view()


def checked_fields(body: dict) -> dict:
    fields = {k: body[k] for k in ("kind", *LIMITS) if k in body}
    if not fields:
        raise Refused(f"Send at least one of kind, {', '.join(LIMITS)}.")
    if "kind" in fields and fields["kind"] not in KINDS:
        raise Refused(f"The kind must be one of {', '.join(KINDS)}.")
    for name, most in LIMITS.items():
        if name not in fields:
            continue
        if not isinstance(fields[name], str):
            raise Refused(f"The {name} must be text.")
        if len(fields[name]) > most:
            raise Refused(f"The {name} is limited to {most:,} characters and this one has {len(fields[name]):,}.")
    return fields


def save(body: dict) -> dict:
    fields = checked_fields(body)
    held = state.capture_draft(state.reader) or EMPTY_DRAFT
    if any(held[k] != v for k, v in fields.items()):
        state.capture_save(**fields)
    return view()


def safe(value) -> str:
    return outbound.screen(outbound.sanitize(str(value or "")))


def run_facts(run_id: str) -> dict:
    run = state.run_get(run_id, connect=state.reader)
    if not run:
        return {}
    rows = state.steps(run_id, connect=state.reader)
    facts = {key: run.get(key) for key in RUN_FACTS}
    facts["step"] = rows[-1]["step"] if rows else ""
    facts["error"] = next((r["error"] for r in reversed(rows) if r.get("error")), "")
    if not facts["error"] and run.get("status") in ("failed", "interrupted"):
        facts["error"] = run.get("summary")
    return {key: safe(value) for key, value in facts.items() if value}


def snapshot(view_name: str, detail, code: str) -> str:
    if view_name not in VIEWS:
        raise Refused(f"The view must be one of {', '.join(VIEWS)}.")
    if detail is not None and not isinstance(detail, dict):
        raise Refused("The open item must be an object with a kind and an id.")
    snap = {"captured": util.iso(), "app": {"version": __version__, "code": code}, "view": view_name}
    if detail:
        kind, item_id = detail.get("kind"), detail.get("id")
        if kind not in ITEM_KINDS:
            raise Refused(f"The open item's kind must be one of {', '.join(ITEM_KINDS)}.")
        if not isinstance(item_id, str) or not item_id or len(item_id) > MOST_ID:
            raise Refused(f"The open item's id must be text of 1 to {MOST_ID} characters.")
        snap["item"] = {"kind": kind, "id": safe(item_id)}
        facts = run_facts(item_id) if kind in ("run", "session") and RUN_ID.fullmatch(item_id) else {}
        if facts:
            snap["run"] = facts
    text = json.dumps(snap, indent=2, ensure_ascii=False)
    if len(text.encode()) > MOST_CONTEXT_BYTES:
        raise Refused("The app state is over 8 KB, so it was not added.")
    return text


def add_context(body: dict, code: str) -> dict:
    text = snapshot(str(body.get("view") or ""), body.get("detail"), code)
    state.capture_save(context=text)
    return view()


def drop_context() -> dict:
    draft = state.capture_draft(state.reader)
    if draft and draft["context"]:
        state.capture_save(context="")
    return view()


def discard() -> dict:
    with FILES:
        draft_id = state.capture_discard()
        if draft_id is not None:
            shutil.rmtree(CAPTURES_DIR / str(draft_id), ignore_errors=True)
    return view()


def fields_of(draft: dict) -> list[tuple[str, str]]:
    fields = [("Request", draft["text"])]
    if draft["kind"] == "bug":
        fields += [("Expected", draft["expected"]), ("Actual", draft["actual"]), ("Steps", draft["steps"])]
    return fields


def filing_text(draft: dict) -> tuple[str, str]:
    parts = [draft["text"].strip()]
    parts += [f"## {label}\n{value.strip()}" for label, value in fields_of(draft)[1:] if value.strip()]
    if draft["context"]:
        parts.append(f"<details>\n<summary>App state</summary>\n\n```json\n{draft['context']}\n```\n\n</details>")
    if draft["evidence"]:
        parts.append("## Evidence\n" + "\n".join(f"- {e['name']}" for e in draft["evidence"]))
    return chat.title_of(draft["text"]), "\n\n".join(parts)


def outgoing(draft: dict) -> dict:
    title, body = filing_text(draft)
    return {"title": outbound.screen(title), "body": str(gh.outgoing(body))}


def request_text(draft: dict) -> str:
    return "\n\n".join(f"{label}:\n{value.strip()}" for label, value in fields_of(draft) if value.strip())


def evidence_names(draft: dict) -> list[str]:
    return [e["name"] for e in draft["evidence"]]


def vision_of(cfg: config.Config) -> dict:
    found = {"path": "", "version": "", "text": "", "setting": ""}
    if not cfg.capture_vision:
        found["setting"] = (f'No vision file is set. Add vision = "vision.md" under [capture] in '
                            f'{paths.short_path(paths.CONFIG_FILE)}, naming a file in that folder that says what the '
                            f'app is for.')
        return found
    path = Path(cfg.capture_vision)
    found["path"] = paths.short_path(path)
    try:
        data = paths.read_confined(path)
        modified = datetime.fromtimestamp(os.stat(path).st_mtime).strftime("%Y-%m-%d")
    except OSError as e:
        found["setting"] = (f"The vision file {found['path']} cannot be read ({e.strerror or type(e).__name__}). "
                            f"Fix [capture] vision in the config file.")
        return found
    found["text"] = data.decode(errors="replace").strip()
    if not found["text"]:
        found["setting"] = (f"The vision file {found['path']} is empty. Write what the app is for in it, or point "
                            f"[capture] vision at another file.")
        return found
    found["version"] = f"{hashlib.sha256(data).hexdigest()[:12]} of {modified}"
    return found


def digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


def evidence_route(cfg: config.Config) -> tuple[steps.Settings | None, str]:
    try:
        return steps.resolve(cfg, None, "capture_evidence"), ""
    except (ValueError, TypeError) as e:
        return None, util.first_line(str(e)) or type(e).__name__


def route_print(route: tuple[steps.Settings | None, str]) -> str:
    s, error = route
    return digest(s.runner, s.model, s.requires.data_handling) if s else digest(error)


def item_print(row: dict, route: tuple[steps.Settings | None, str]) -> str:
    return digest(row["sha256"], route_print(route))


def media_of(draft: dict) -> list[dict]:
    return [e for e in draft["evidence"] if e["kind"] in MEDIA]


def fresh(row: dict, route: tuple[steps.Settings | None, str]) -> bool:
    found = row["inspection"]
    return found.get("state") == "inspected" and found.get("print") == item_print(row, route)


def inspection_view(row: dict, route: tuple[steps.Settings | None, str]) -> dict:
    found = row["inspection"]
    if fresh(row, route):
        return {"inspected": True, "model": found["model"], "runner": found["runner"], "flags": found["flags"],
                "saw": found["saw"], "why": ""}
    if not found:
        why = NOT_INSPECTED_YET
    elif found.get("print") != item_print(row, route):
        why = ROUTE_CHANGED
    else:
        why = found["why"]
    return {"inspected": False, "model": "", "runner": "", "flags": [], "saw": "", "why": why}


def outcome(print_: str, result: str, why: str = "", **found) -> dict:
    return {"state": result, "print": print_, "why": why, "checked": util.iso(), **found}


def complete(answer: dict) -> bool:
    return (all(type(answer.get(k)) is bool for k in ("viewed", *FLAGS)) and isinstance(answer.get("saw"), str))


def judged(res, row: dict, print_: str) -> dict:
    answer = res.structured if res.ok and isinstance(res.structured, dict) else {}
    if complete(answer) and answer["viewed"]:
        saw = util.first_line(safe(answer["saw"]), 200) or "nothing described"
        return outcome(print_, "inspected", model=res.model, runner=res.runner,
                       flags=[k for k in FLAGS if answer[k]], saw=saw)
    ineligible = [s for s in res.skipped if s["state"] == "ineligible"]
    if runner.NO_ROUTE in res.error and ineligible:
        reasons = "; ".join(f"{s['model']} ({s['runner']}): {s['detail']}" for s in ineligible)
        return outcome(print_, "held", f"No route can inspect a {row['kind']}: {reasons}.")
    why = util.first_line(res.error) or ("the model could not view the file" if complete(answer)
                                         else "the answer was incomplete")
    return outcome(print_, "unavailable", f"Inspection is unavailable ({why}).")


def look_at(row: dict, draft_id: int, s: steps.Settings, print_: str, cfg: config.Config) -> tuple[dict, float]:
    folder = CAPTURES_DIR / str(draft_id)
    viewable = folder / f"{row['sha256']}.{EXTENSIONS[row['type']]}"
    kept = tuple(m for m in s.requires.modalities if m not in MEDIA)
    settings = replace(s, requires=replace(s.requires, modalities=(*kept, row["kind"]), structured_output=True))
    text = pipeline.prompt("capture_evidence", file=viewable.name, kind=row["kind"])
    try:
        shutil.copyfile(folder / row["sha256"], viewable)
        res = runner.run(text, folder, **s.call, tools=list(s.tools), allowed_tools=list(s.tools),
                         instructions=list(s.instructions), json_schema=EVIDENCE_SCHEMA, persist=False,
                         wait_out_limits=False, hard_cap=True, tier=s.tier,
                         trace=("", f"capture_evidence {row['sha256'][:12]}"), cfg=cfg, settings=settings)
    finally:
        viewable.unlink(missing_ok=True)
    return judged(res, row, print_), res.paid_usd


def inspect_item(row: dict, draft_id: int, route: tuple[steps.Settings | None, str], cfg: config.Config) -> None:
    s, error = route
    print_ = item_print(row, route)
    if s:
        inspection, cost = look_at(row, draft_id, s, print_, cfg)
    else:
        inspection, cost = outcome(print_, "unavailable", f"Inspection is unavailable ({error})."), 0.0
    state.capture_inspected(row["id"], row["sha256"], inspection, cost)
    row["inspection"] = inspection


def inspect_media(draft: dict, cfg: config.Config) -> None:
    route = evidence_route(cfg)
    for row in media_of(draft):
        if not fresh(row, route):
            inspect_item(row, draft["id"], route, cfg)


def fingerprints(draft: dict, cfg: config.Config) -> dict[str, str]:
    bug = draft["kind"] == "bug"
    request = [draft["kind"], draft["text"], *(draft[k] for k in ("expected", "actual", "steps") if bug)]
    model = cfg.jev.model
    items = [(e["name"], e["kind"], e["sha256"]) for e in draft["evidence"]]
    route = [route_print(evidence_route(cfg))] if media_of(draft) else []
    sure = cfg.jev.confidence
    return {"vision": digest(request, vision_of(cfg)["version"], model, sure),
            "duplicates": digest(request, chat.arete_repo(cfg).github, model, sure),
            "actionable": digest(request, model, sure),
            "injection": digest(request, draft["context"], items, model, cfg.jev.injection, *route),
            "publication": digest(request, draft["context"], items, model, cfg.jev.secrets_hold, *route)}


def stale(row: dict, print_: str, name: str) -> bool:
    if row["result"] == "running":
        return False
    if row["fingerprint"] != print_:
        return True
    return (name == "duplicates" and bool(row["checked"])
            and util.now() - util.parse_iso(row["checked"]) > timedelta(seconds=DUPLICATES_FRESH_SECONDS))


def due_checks(draft: dict, prints: dict[str, str], route: tuple[steps.Settings | None, str]) -> list[str]:
    rows = draft["checks"]
    unread = any(not fresh(e, route) for e in media_of(draft))
    return [n for n in CHECKS if n not in rows or rows[n]["result"] != "pass" or stale(rows[n], prints[n], n)
            or (unread and n in READS_EVIDENCE)]


def shown(draft: dict) -> dict:
    cfg = config.load()
    prints = fingerprints(draft, cfg)
    checks = []
    for name in CHECKS:
        row = draft["checks"].get(name)
        checks.append({"name": name, "title": TITLES[name], "result": row["result"] if row else "none",
                       "reason": row["reason"] if row else "", "stale": bool(row) and stale(row, prints[name], name),
                       "checked": row["checked"] if row else "", "detail": row["detail"] if row else {}})
    vision = vision_of(cfg)
    route = evidence_route(cfg)
    evidence = [{**e, "inspection": inspection_view(e, route)} for e in draft["evidence"]]
    ready = draft["status"] == "draft" and all(c["result"] == "pass" and not c["stale"] for c in checks)
    return {**{k: v for k, v in draft.items() if k not in ("checks", "evidence")}, "evidence": evidence,
            "checks": checks, "outgoing": outgoing(draft),
            "vision": {k: vision[k] for k in ("path", "version", "setting")},
            "jev_cost_usd": draft.get("jev_cost_usd") or 0.0,
            "inspect_cost_usd": draft.get("inspect_cost_usd") or 0.0, "ready": ready,
            "summary": summary(draft["status"], checks, ready)}


def summary(status: str, checks: list[dict], ready: bool) -> str:
    if ready:
        return READY
    if status == "checking":
        return "Running the checks. This updates when they finish."
    if all(c["result"] == "none" for c in checks):
        return "Press Submit to check this request before it can be filed."
    counts = {"failed": sum(c["result"] == "fail" for c in checks),
              "held": sum(c["result"] in ("hold", "interrupted") for c in checks),
              "stale": sum(c["stale"] for c in checks)}
    found = ", ".join(f"{n} {word}" for word, n in counts.items() if n)
    return f"Checks: {found}. Revise and Submit again." if found else "Press Submit to run the checks again."


def allowed(cfg: config.Config) -> bool:
    return cfg.jev_allowed([chat.arete_repo(cfg)])


JEV_OFF = "jev is off for the arete repo in the config"


def asked(cfg: config.Config, text: str, questions: dict[str, dict]) -> jev.Reply:
    if not allowed(cfg):
        return jev.Reply(error=JEV_OFF)
    return jev.ask(outbound.redact(text), questions, cfg)


def unavailable(reply: jev.Reply, detail: dict | None = None) -> tuple[str, str, dict, float]:
    why = reply.error or "its reply left the question out"
    return ("hold", f"Not checked: JEV is unavailable ({why}). Submit again when it answers.", detail or {},
            reply.cost_usd)


def chance_of(answer: jev.Answer, option: str) -> float:
    if option in answer.probabilities:
        return answer.probabilities[option]
    return answer.confidence if answer.value == option else 0.0


def keep_verdict(draft: dict, subject: str, scores: dict[str, dict], held: str) -> None:
    verdicts.record(f"capture-{draft['id']}", "capture", subject, chat.title_of(draft["text"]), scores, held)


def check_vision(draft: dict, cfg: config.Config) -> tuple[str, str, dict, float]:
    vision = vision_of(cfg)
    detail = {"path": vision["path"], "version": vision["version"]}
    if vision["setting"]:
        return "hold", vision["setting"], detail, 0.0
    reply = asked(cfg, f"Product vision:\n{vision['text']}\n\n{request_text(draft)}",
                  {"vision": jev.pick(VISION_QUESTION, VISION_OPTIONS)})
    answer = reply.answers.get("vision")
    if not answer:
        return unavailable(reply, detail)
    line = cfg.jev.confidence
    sure = answer.sure(line)
    if answer.value == "supports" and sure:
        result, reason = "pass", f"The vision supports this request (confidence {answer.confidence:.2f})."
    elif answer.value == "conflicts" and sure:
        result, reason = "fail", f"The vision conflicts with this request (confidence {answer.confidence:.2f})."
    else:
        result = "hold"
        reason = (f"JEV could not tell whether the vision supports this request: it picked {answer.value} at "
                  f"confidence {answer.confidence:.2f}, and {line:g} is needed.")
    scores = {"supports": verdicts.scored(chance_of(answer, "supports"), line, over=False),
              "conflicts": verdicts.scored(chance_of(answer, "conflicts"), line)}
    keep_verdict(draft, "vision", scores, "" if result == "pass" else reason)
    return result, reason, detail, reply.cost_usd


def check_duplicates(draft: dict, cfg: config.Config) -> tuple[str, str, dict, float]:
    repo = chat.arete_repo(cfg).github
    if not repo:
        reason = ("Not checked: the arete repo has no github setting. Add github = \"owner/name\" to its [[repo]] "
                  "in the config file.")
        return "hold", reason, {}, 0.0
    try:
        listed = gh.recent_issues(repo, MOST_ISSUES)
    except (gh.GhError, OSError, ValueError) as e:
        return "hold", f"Not checked: GitHub did not answer ({util.first_line(str(e)) or type(e).__name__}).", {}, 0.0
    issues = [i for i in listed if isinstance(i, dict) and "number" in i
              and (i.get("state") == "OPEN" or i.get("stateReason") == "COMPLETED")]
    if not issues:
        return "pass", "No open or completed issues to compare with.", {"matches": []}, 0.0
    if not allowed(cfg):
        return unavailable(jev.Reply(error=JEV_OFF))
    title = chat.title_of(draft["text"])
    items = [(outbound.redact(f"New request:\n{title}\n\n{request_text(draft)}\n\nExisting issue #{i['number']} "
                              f"({str(i.get('state', '')).lower()}): {i.get('title', '')}\n\n"
                              f"{str(i.get('body') or '')[:MOST_ISSUE_BODY]}"),
              {"duplicate": jev.yes_no(DUPLICATE_QUESTION)}) for i in issues]
    replies = jev.ask_many(items, cfg)
    cost = sum(r.cost_usd for r in replies)
    line = cfg.jev.confidence
    sure_yes, sure_no = (1 + line) / 2, (1 - line) / 2
    chances = [(i, a.yes) for i, r in zip(issues, replies, strict=True) if (a := r.answers.get("duplicate"))]
    for issue, p in chances:
        keep_verdict(draft, f"duplicate #{issue['number']}", {"duplicate": verdicts.scored(p, sure_no)},
                     f"possible duplicate (p {p:.2f})" if p >= sure_no else "")

    def entry(issue: dict, p: float) -> dict:
        return {"number": issue["number"], "title": issue.get("title", ""), "state": str(issue.get("state", "")).lower(),
                "chance": round(p, 2), "url": gh.issue_url(repo, issue["number"])}

    if found := [entry(i, p) for i, p in chances if p >= sure_yes]:
        names = ", ".join(f"#{m['number']} (chance {m['chance']:.2f})" for m in found)
        return "fail", f"This looks like an issue already filed: {names}.", {"matches": found}, cost
    if len(chances) < len(issues):
        error = next((r.error for r in replies if r.error), "")
        reason = (f"Not checked: JEV is unavailable for {len(issues) - len(chances)} of {len(issues)} issues "
                  f"({error or 'its reply left the question out'}). Submit again when it answers.")
        return "hold", reason, {}, cost
    issue, p = max(chances, key=lambda c: c[1])
    if p > sure_no:
        near = entry(issue, p)
        reason = (f"Issue #{near['number']} may be the same request (chance {p:.2f}); JEV is not sure either way. "
                  f"Check it, then Submit again.")
        return "hold", reason, {"matches": [near]}, cost
    return "pass", f"Compared with {util.count(len(issues), 'issue')}; none looks like this request.", {"matches": []}, cost


def check_actionable(draft: dict, cfg: config.Config) -> tuple[str, str, dict, float]:
    reply = asked(cfg, request_text(draft), {"actionable": jev.pick(ACTIONABLE_QUESTION, ACTIONABLE_OPTIONS)})
    answer = reply.answers.get("actionable")
    if not answer:
        return unavailable(reply)
    line = cfg.jev.confidence
    scores = {"actionable": verdicts.scored(chance_of(answer, "actionable"), line, over=False)}
    if answer.value == "actionable" and answer.sure(line):
        keep_verdict(draft, "actionable", scores, "")
        return "pass", f"A developer could start on this as written (confidence {answer.confidence:.2f}).", {}, reply.cost_usd
    ranked = sorted(MISSING, key=lambda k: chance_of(answer, k), reverse=True)
    missing = answer.value if answer.value in MISSING else ranked[0] if chance_of(answer, ranked[0]) > 0 else "what"
    reason = f"Not actionable yet: it does not say {MISSING[missing]}. Add that and Submit again."
    keep_verdict(draft, "actionable", scores, reason)
    return "hold", reason, {"missing": missing}, reply.cost_usd


def evidence_detail(draft: dict, route: tuple[steps.Settings | None, str]) -> dict:
    media = media_of(draft)
    return {"evidence": [{"name": e["name"], **inspection_view(e, route)} for e in media]} if media else {}


def check_injection(draft: dict, cfg: config.Config) -> tuple[str, str, dict, float]:
    route = evidence_route(cfg)
    media = media_of(draft)
    detail = evidence_detail(draft, route)
    if addressed := [e for e in media if fresh(e, route) and "agent_text" in e["inspection"]["flags"]]:
        seen = "; ".join(f"{e['name']} holds text addressed to an agent ({e['inspection']['saw']})" for e in addressed)
        return "fail", f"{seen}. Remove it and Submit again.", detail, 0.0
    unread = [e["name"] for e in media if not fresh(e, route)]
    names = evidence_names(draft)
    text = "\n\n".join(filter(None, [request_text(draft), f"App state:\n{draft['context']}" if draft["context"] else "",
                                     "Evidence names:\n" + "\n".join(names) if names else ""]))
    reply = asked(cfg, text, {"injection": jev.yes_no(jev.INJECTION)})
    answer = reply.answers.get("injection")
    if not answer:
        return unavailable(reply)
    line = cfg.jev.injection
    scores = {"injection": verdicts.scored(answer.yes, line)}
    if answer.yes >= line:
        reason = f"This text reads as instructions for an agent (p {answer.yes:.2f}, line {line:g}). Describe the problem only."
        keep_verdict(draft, "injection", scores, reason)
        return "fail", reason, detail, reply.cost_usd
    keep_verdict(draft, "injection", scores, "")
    passed = f"No instructions for an agent found (p {answer.yes:.2f}, line {line:g})."
    if unread:
        passed += f" {', '.join(unread)} was not read, and Publication safety holds it."
    return "pass", passed, detail, reply.cost_usd


def secret_fields(draft: dict) -> list[str]:
    fields = fields_of(draft) + ([("App state", draft["context"])] if draft["context"] else [])
    return [label for label, text in fields
            if any(not m.group(0).startswith("/Users/") for m in outbound.SECRETISH.finditer(text))]


def check_publication(draft: dict, cfg: config.Config) -> tuple[str, str, dict, float]:
    if found := secret_fields(draft):
        return ("fail", f"This looks like it holds a key or token in: {', '.join(found)}. Remove it and Submit again.",
                {"fields": found}, 0.0)
    route = evidence_route(cfg)
    media = media_of(draft)
    detail = evidence_detail(draft, route)
    sending = outgoing(draft)
    reply = asked(cfg, f"{sending['title']}\n\n{sending['body']}", {"secrets": jev.yes_no(SECRETS_QUESTION)})
    answer = reply.answers.get("secrets")
    line = cfg.jev.secrets_hold
    scores, held_text = {}, ""
    if not answer:
        held_text = unavailable(reply)[1]
    else:
        scores = {"secrets": verdicts.scored(answer.yes, line)}
        if answer.yes >= line:
            held_text = (f"JEV thinks this text holds a credential or private personal information (p "
                         f"{answer.yes:.2f}, line {line:g}). Remove it and Submit again.")
    leaks = [f"{e['name']} shows {', '.join(FLAGS[k] for k in LEAKS if k in e['inspection']['flags'])} "
             f"({e['inspection']['saw']})" for e in media
             if fresh(e, route) and any(k in e["inspection"]["flags"] for k in LEAKS)]
    unseen = [f"{e['name']}: {inspection_view(e, route)['why']}" for e in media if not fresh(e, route)]
    if leaks:
        result, reason = "fail", f"{'; '.join(leaks)}. Remove it and Submit again."
    elif held_text or unseen:
        said = f"Not inspected, so it stays staged: {' '.join(unseen)}" if unseen else ""
        result, reason = "hold", " ".join(filter(None, [held_text, said]))
    else:
        result = "pass"
        reason = (f"No credential or private information found in the text (p {answer.yes:.2f}, line {line:g})."
                  + (f" {util.count(len(media), 'item')} inspected, nothing sensitive found." if media else ""))
    if answer:
        keep_verdict(draft, "publication", scores, "" if result == "pass" else reason)
    return result, reason, detail, reply.cost_usd


RUNNERS = {"vision": check_vision, "duplicates": check_duplicates, "actionable": check_actionable,
           "injection": check_injection, "publication": check_publication}


def run_checks(draft: dict, cfg: config.Config, due: list[str]) -> None:
    inspected = False
    try:
        for name in due:
            try:
                if name in READS_EVIDENCE and not inspected:
                    inspected = True
                    inspect_media(draft, cfg)
                result, reason, detail, cost = RUNNERS[name](draft, cfg)
            except Exception as e:
                result, reason, detail, cost = "hold", f"Not checked: {util.first_line(str(e)) or type(e).__name__}", {}, 0.0
            if not state.capture_check_done(draft["id"], name, result, reason, detail, cost):
                return
    finally:
        state.capture_end_checks(draft["id"], INTERRUPTED)


def submit(body: dict) -> dict:
    revision = body.get("revision") if isinstance(body, dict) else None
    if type(revision) is not int:
        raise Refused("Send the draft's revision as a whole number.")
    cfg = config.load()
    with SUBMITTING:
        draft = state.capture_draft(state.reader)
        if not draft:
            raise Conflict("There is no draft to check.")
        if draft["status"] == "checking":
            raise Conflict("The checks are still running.")
        if draft["revision"] != revision:
            raise Conflict("The draft changed since this page last read it. Read it again, then Submit.")
        if not draft["text"].strip():
            raise Refused("Write the request before you submit it.")
        prints = fingerprints(draft, cfg)
        due = due_checks(draft, prints, evidence_route(cfg))
        if due:
            if not state.capture_begin_checks(draft["id"], revision, {n: prints[n] for n in due}):
                raise Conflict("The draft changed since this page last read it. Read it again, then Submit.")
            threading.Thread(target=run_checks, args=(draft, cfg, due), daemon=True).start()
    return view()


def recover() -> None:
    state.capture_end_checks(None, INTERRUPTED)


def checking() -> bool:
    return state.capture_checking()
