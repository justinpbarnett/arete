from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path

from . import config, jev, paths, sandbox, util

POLICY_DIR = "ARETE_JEV_POLICY_DIR"
POLICY_REQUEST = re.compile(r"[0-9a-f]{32}\.request")
POLICY_LIMIT = 1024 * (jev.MAX_FILES + 1) * 8
POLICY_INTERVAL = 0.02
POLICY_TIMEOUT = 2

NOTE = ("You can run `{ask}`, which puts a question to JEV, a fast classifier that costs next to nothing, over text you "
        "pipe in or over files you name, and gives an answer and a confidence for each. Use it to sort or check many "
        "things before you spend reads on them: `{ask} \"Does this file check permissions?\" src -r --only yes` lists "
        "the files that do; `<test command> 2>&1 | tail -200 | {ask} \"Why does this run fail?\" --choice "
        "\"code=the code under test is wrong\" \"test=the test itself is wrong\" \"setup=the environment or setup is "
        "wrong\"` sorts a failure before you dig in; `git diff | {ask} \"Does this diff do only what the task asks?\"` "
        "checks your work. Give --score with levels from lowest to highest for a rating. Put the paths before --choice "
        "or --score. JEV cannot count, compare dates or reason through negation, and it leans to the first choice, so "
        "read the code yourself before you change it or rest a decision on an answer.")


def command() -> str:
    return "arete ask" if shutil.which("arete") else f"{shlex.quote(str(Path(sys.executable).parent / 'arete'))} ask"


def rule() -> str:
    return f"Bash({command()} *)"


def note() -> str:
    return NOTE.format(ask=command())


def question(text: str, choices: list[str], levels: list[str]) -> dict:
    if choices and levels:
        raise ValueError("give --choice or --score, not both")
    if len(choices) == 1 or len(levels) == 1:
        raise ValueError("give at least two choices or levels")
    if choices:
        options = {label.strip(): about.strip() or label.strip()
                   for label, _, about in (c.partition("=") for c in choices)}
        return jev.pick(text, options)
    return jev.rate(text, levels) if levels else jev.yes_no(text)


def label(a: jev.Answer, q: dict) -> str:
    if q["type"] == "noul":
        return "yes" if a.value else "no"
    if q["type"] == "choice":
        return str(a.value)
    levels = q["criteria"]
    return levels[round(float(a.value) * (len(levels) - 1))]


@contextmanager
def serve_policy(snapshot: tuple[Path, str], directory: Path):
    with tempfile.TemporaryDirectory(prefix=".arete-ask-", dir=directory.resolve()) as mailbox:
        with paths.directory_fd(Path(mailbox)) as fd:
            with util.repeating(POLICY_INTERVAL, lambda: answer_policy_requests(fd, snapshot), "arete-ask-policy"):
                yield mailbox


def read_policy_message(name: str, directory: int):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > POLICY_LIMIT:
            raise ValueError("invalid JEV policy message")
        return json.loads(file.read(POLICY_LIMIT + 1))


def answer_policy_requests(directory: int, snapshot: tuple[Path, str]) -> None:
    for name in os.listdir(directory):
        if POLICY_REQUEST.fullmatch(name):
            with suppress(OSError):
                answer_policy_request(name, directory, snapshot)


def answer_policy_request(name: str, directory: int, snapshot: tuple[Path, str]) -> None:
    from . import delegation

    try:
        requested = read_policy_message(name, directory)
        if not (isinstance(requested, list) and 0 < len(requested) <= jev.MAX_FILES + 1
                and all(isinstance(path, str) and Path(path).is_absolute() for path in requested)):
            raise ValueError("invalid JEV policy request")
        cfg, _, _ = delegation.read_snapshot(*snapshot)
        model = cfg.jev.model if allowed(cfg, requested) else None
        reply = {"model": model}
    except (OSError, ValueError, RecursionError):
        reply = {"error": "could not verify the accepted JEV settings"}
    response = name.removesuffix(".request") + ".response"
    try:
        fd = os.open(response, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "w") as file:
            json.dump(reply, file)
    finally:
        os.unlink(name, dir_fd=directory)


def allowed(cfg: config.Config, inputs: list[Path] | list[str]) -> bool:
    return cfg.jev_allowed(list(map(cfg.repo_for_work_dir, inputs)))


def ask_config(here: Path, files: list[Path]) -> config.Config | None:
    from .delegation import PolicyChanged

    if POLICY_DIR not in os.environ:
        cfg = config.load()
        return cfg if allowed(cfg, [here, *files]) else None
    try:
        model = read_ask_model(here, files)
    except (OSError, ValueError, RecursionError) as error:
        raise PolicyChanged("could not verify the accepted JEV settings") from error
    return None if model is None else config.Config(jev=config.JevConfig(model=model))


def read_ask_model(here: Path, files: list[Path]) -> str | None:
    directory = Path(os.environ[POLICY_DIR])
    if not directory.is_absolute():
        raise ValueError("invalid JEV policy directory")
    nonce = uuid.uuid4().hex
    request, response = directory / f"{nonce}.request", directory / f"{nonce}.response"
    with paths.directory_fd(directory) as fd:
        try:
            util.write_bytes_atomic(request, json.dumps([str(here), *(str(f.absolute()) for f in files)]).encode())
            deadline = time.monotonic() + POLICY_TIMEOUT
            while request.exists() and time.monotonic() < deadline:
                time.sleep(POLICY_INTERVAL)
            reply = read_policy_message(response.name, fd)
        finally:
            request.unlink(missing_ok=True)
            response.unlink(missing_ok=True)
    if not isinstance(reply, dict) or "model" not in reply:
        raise ValueError("JEV policy unavailable")
    model = reply["model"]
    if model is not None and not isinstance(model, str):
        raise ValueError("invalid JEV model")
    return model


def main(args) -> int:
    from .delegation import PolicyChanged

    try:
        q = question(args.question, args.choice or [], args.score or [])
        files = jev.files_matching(args.paths, args.recursive) if args.paths else []
    except ValueError as e:
        return fail(str(e))
    if args.paths and not files:
        return fail("no files match")
    here = Path.cwd().resolve()
    inside = os.environ.get("ARETE_AGENT") != "1"
    refused = {f: "looks like a secret file" if sandbox.secret_path(f) else "outside the working directory"
               for f in files if sandbox.secret_path(f) or not (inside or here in f.resolve().parents)}
    files = [f for f in files if f not in refused]
    try:
        cfg = ask_config(here, files)
    except PolicyChanged:
        return fail("could not verify the accepted JEV settings")
    if cfg is None:
        return fail("JEV is off for this repo in the arete config")
    if not jev.key():
        return fail(f"no {jev.KEY_NAME}; put it in {paths.short_path(jev.KEY_FILE)}")
    if args.paths:
        rows = asked_files(files, q, cfg) + [{"path": str(f), "skipped": why} for f, why in refused.items()]
    elif sys.stdin.isatty():
        return fail("give paths, or pipe the text in")
    else:
        rows = [row("", jev.ask(sys.stdin.read(), {"q": q}, cfg, trace=("", "ask")), q)]
    if args.only:
        rows = [r for r in rows if r.get("answer") == args.only or "error" in r]
    if args.json:
        print(json.dumps(rows if args.paths else next(iter(rows), None), indent=2))
    else:
        for r in rows:
            print("\t".join(filter(None, [r["path"], shown(r)])))
    return 1 if any("error" in r for r in rows) else 0


def asked_files(files: list[Path], q: dict, cfg: config.Config) -> list[dict]:
    texts = [(f, jev.readable(f)) for f in files]
    readable = [(f, t) for f, t in texts if t is not None]
    replies = jev.ask_many([(f"File: {f}\n\n{t}", {"q": q}) for f, t in readable], cfg, trace=("", "ask"))
    return ([row(str(f), r, q) for (f, _), r in zip(readable, replies, strict=True)]
            + [{"path": str(f), "skipped": "binary or unreadable"} for f, t in texts if t is None])


def row(path: str, reply: jev.Reply, q: dict) -> dict:
    a = reply.answers.get("q")
    if a is None:
        return {"path": path, "error": reply.error or "no answer"}
    return {"path": path, "answer": label(a, q), "confidence": round(a.confidence, 3),
            "probabilities": {k: round(v, 3) for k, v in a.probabilities.items()}}


def shown(r: dict) -> str:
    if "answer" in r:
        return f"{r['answer']} (confidence {r['confidence']:.2f})"
    return f"skipped: {r['skipped']}" if "skipped" in r else f"error: {r['error']}"


def fail(why: str) -> int:
    print(f"arete ask: {why}", file=sys.stderr)
    return 2
