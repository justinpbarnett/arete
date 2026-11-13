from __future__ import annotations

import contextlib
import http.client
import json
import math
import os
import re
import socket
import statistics
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import accounts, config, devices, outbound, pi, util

SCENARIOS = ("ok", "oom", "overflow", "malformed", "offline")
SERVED = ("ok", "oom", "overflow", "malformed")
PROBES = ("text", "structured", "tool")
OUTCOMES = ("ok", "rejected", "malformed", "context_overflow", "out_of_memory", "timeout", "error")
COUNTS = ("requests", *OUTCOMES)
FAILURES = OUTCOMES[1:]
NAME = re.compile(r"[A-Za-z0-9_-]+")
OOM = re.compile(r"out of memory|\boom\b|insufficient memory|not enough memory|failed to allocate|"
                 r"requires more system memory", re.IGNORECASE)
OVERFLOW = re.compile(r"context length|context window|maximum context|context_length_exceeded|too many tokens|"
                      r"prompt is too long", re.IGNORECASE)
NOTE = "Measurements only: arete changed no route, tier or config, pulled no weights and provisioned no device."
TOP_KEYS = {"contexts", "concurrency", "max_tokens", "timeout_seconds", "max_seconds", "escalation", "candidates"}
TEXT_KEYS = ("route", "quantization", "runtime", "runtime_version", "hardware")
CANDIDATE_KEYS = {*TEXT_KEYS, "memory_gb", "window", "device"}
OOM_ESTIMATE = 16000
OVERFLOW_ESTIMATE = 8192
FIXTURE_GB = {"ok": 6.0, "oom": 23.5}
LOCAL_OLLAMA = "http://localhost:11434/v1"
SHORT_TIMEOUT = 5


class Refused(ValueError):
    pass


class Failure(Exception):
    def __init__(self, status: int, text: str, timed_out: bool = False):
        super().__init__(text)
        self.status = status
        self.text = text
        self.timed_out = timed_out


@dataclass
class Candidate:
    name: str
    route: str
    provider: str
    model: str
    base: str
    key: str
    quantization: str
    runtime: str
    runtime_version: str
    hardware: str
    memory_gb: float
    window: int
    device: str = ""

    @property
    def fixture(self) -> bool:
        return self.provider == "fixture"

    def manifest(self) -> dict:
        return {"route": self.route, "model": self.model, "quantization": self.quantization,
                "runtime": self.runtime, "runtime_version": self.runtime_version, "hardware": self.hardware,
                "memory_gb": self.memory_gb, "window": self.window, "device": self.device or None}


@dataclass
class Spec:
    candidates: list[Candidate]
    contexts: list[int] = field(default_factory=lambda: [8192, 32768])
    concurrency: list[int] = field(default_factory=lambda: [1, 2])
    max_tokens: int = 64
    timeout_seconds: float = 120
    max_seconds: float = 900
    escalation: dict | None = None


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def whole(value, low: int, high: int | None, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < low or (high is not None and value > high):
        raise Refused(f"{label} must be a whole number from {low}" + (f" to {high}" if high else ""))
    return value


def whole_list(value, low: int, high: int, label: str, most: int = 4) -> list[int]:
    if not isinstance(value, list) or not 1 <= len(value) <= most:
        raise Refused(f"{label} must list 1 to {most} whole numbers")
    return [whole(v, low, high, label) for v in value]


def ranged(value, low: float, high: float, label: str) -> float:
    if not number(value) or not low <= value <= high:
        raise Refused(f"{label} must be a number from {low} to {high}")
    return value


def read_manifest(path: Path) -> Spec:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Refused(f"manifest {path} does not exist") from None
    except tomllib.TOMLDecodeError as e:
        raise Refused(f"manifest {path} is not valid TOML: {e}") from None
    if unknown := sorted(set(data) - TOP_KEYS):
        raise Refused(f"unknown manifest key {unknown[0]}")
    table = accounts.mapping(data.get("candidates"))
    if not 1 <= len(table) <= 12:
        raise Refused("the manifest needs 1 to 12 [candidates.NAME] tables")
    spec = Spec([])
    if "contexts" in data:
        spec.contexts = whole_list(data["contexts"], 1024, 262144, "contexts")
    if "concurrency" in data:
        spec.concurrency = whole_list(data["concurrency"], 1, 16, "concurrency")
    if "max_tokens" in data:
        spec.max_tokens = whole(data["max_tokens"], 1, 1024, "max_tokens")
    if "timeout_seconds" in data:
        spec.timeout_seconds = ranged(data["timeout_seconds"], 1, 600, "timeout_seconds")
    if "max_seconds" in data:
        spec.max_seconds = ranged(data["max_seconds"], 1, 7200, "max_seconds")
    if "escalation" in data:
        spec.escalation = read_rates(data["escalation"])
    env = {**os.environ, **util.read_env_file(pi.KEYS_FILE)}
    spec.candidates = [read_candidate(name, accounts.mapping(raw), env) for name, raw in table.items()]
    return spec


def read_rates(raw) -> dict:
    raw = accounts.mapping(raw)
    keys = ("input_usd_per_mtok", "output_usd_per_mtok")
    if set(raw) != set(keys):
        raise Refused("[escalation] needs exactly input_usd_per_mtok and output_usd_per_mtok")
    return {k: ranged(raw[k], 0, float("inf"), f"escalation {k}") for k in keys}


def read_candidate(name: str, raw: dict, env: dict) -> Candidate:
    if not NAME.fullmatch(name):
        raise Refused(f"candidate {name}: the name may use letters, digits, - and _ only")
    if unknown := sorted(set(raw) - CANDIDATE_KEYS):
        raise Refused(f"candidate {name}: unknown key {unknown[0]}")
    for key in TEXT_KEYS:
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise Refused(f"candidate {name}: {key} must be a non-empty string")
    if not number(raw.get("memory_gb")) or raw["memory_gb"] <= 0:
        raise Refused(f"candidate {name}: memory_gb must be a number above 0")
    window = whole(raw.get("window"), 1024, None, f"candidate {name}: window")
    device = raw.get("device", "")
    if not isinstance(device, str):
        raise Refused(f"candidate {name}: device must be a string")
    if device:
        check_device(name, device)
    provider, _, model = raw["route"].partition("/")
    if not provider or not model:
        raise Refused(f"candidate {name}: route must be provider/model")
    base, key = resolve_endpoint(name, provider, model)
    if base is None:
        key = ""
    else:
        key = pi.configured_key(key, env)
    return Candidate(name, raw["route"], provider, model, base or "", key, raw["quantization"], raw["runtime"],
                     raw["runtime_version"], raw["hardware"], raw["memory_gb"], window, device)


def check_device(name: str, device: str) -> None:
    try:
        found = devices.get(device)
    except devices.DeviceError as e:
        raise Refused(f"candidate {name}: {util.first_line(str(e), 200)}") from None
    if found.kind not in ("local", "ssh"):
        raise Refused(f"candidate {name}: device {device} is a {found.kind} device and arete benchmark never "
                      "provisions a device")


def resolve_endpoint(name: str, provider: str, model: str) -> tuple[str | None, str]:
    if provider == "fixture":
        if model not in SCENARIOS:
            raise Refused(f"candidate {name}: fixture scenario must be one of {', '.join(SCENARIOS)}")
        return None, ""
    entry = accounts.mapping(accounts.mapping(pi.model_configuration().get("providers")).get(provider))
    base = entry.get("baseUrl")
    if not isinstance(base, str) and provider == "ollama" and not entry:
        base = LOCAL_OLLAMA
    if not isinstance(base, str) or urlsplit(base).scheme not in ("http", "https") or not urlsplit(base).hostname:
        raise Refused(f"candidate {name}: provider {provider} is not configured in Pi's models.json")
    if provider not in config.load().local_providers and not outbound.private_host(urlsplit(base).hostname):
        raise Refused(f"candidate {name}: provider {provider} is not a local endpoint; the benchmark calls only "
                      "local endpoints (list the provider under [pricing] local_providers if it is one)")
    return base.rstrip("/"), entry.get("apiKey", "")


def estimate(messages: list[dict]) -> int:
    return sum(len(str(m.get("content", "")).encode("utf-8")) for m in messages) // 3


def build_messages(context: int, tag: str, worker: int) -> tuple[list[dict], str]:
    head = f"Reference list {tag}. Each numbered line holds one code word.\n"
    tail_size = 40
    lines, size, number_ = [], len(head.encode()) + tail_size, 0
    while (size // 3) < context * 3 // 4:
        number_ += 1
        line = f"{number_:05d} word-{number_}\n"
        lines.append(line)
        size += len(line)
    asked = min(len(lines) // 2 + worker, len(lines))
    content = head + "".join(lines) + f"What is the code word on line {asked:05d}?"
    return [{"role": "user", "content": content}], f"word-{asked}"


def payload_for(candidate: Candidate, probe: str, messages: list[dict], max_tokens: int) -> dict:
    body = {"model": candidate.model, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True}, "max_tokens": max_tokens, "temperature": 0}
    if probe == "structured":
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "word", "strict": True, "schema": {"type": "object", "properties": {"word": {"type": "string"}},
                                                        "required": ["word"], "additionalProperties": False}}}
    if probe == "tool":
        body["tools"] = [{"type": "function", "function": {
            "name": "report_word", "description": "Report the code word.",
            "parameters": {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]}}}]
        body["tool_choice"] = "required"
    return body


def fixture_chunks(scenario: str, payload: dict) -> list[dict]:
    text = payload["messages"][-1]["content"]
    asked = re.search(r"code word on line (\d+)\?", text)
    found = re.search(rf"^{asked[1]} (\S+)$", text, re.MULTILINE) if asked else None
    word = found[1] if found else "unknown"
    broken = scenario == "malformed"
    if "tools" in payload:
        args = '{"word": ' if broken else json.dumps({"word": word})
        pieces = [args[i:i + 8] for i in range(0, len(args), 8)]
        first = {"index": 0, "id": "call_1", "function": {"name": "report_word", "arguments": ""}}
        deltas = [{"tool_calls": [first]}] + [
            {"tool_calls": [{"index": 0, "function": {"arguments": p}}]} for p in pieces]
    else:
        if "response_format" in payload:
            full = json.dumps({"word": word})
            full = full[:-4] if broken else full
        else:
            full = f"The code word is {word}."
        deltas = [{"content": p} for p in (full[i:i + 6] for i in range(0, len(full), 6))]
    chunks = [{"choices": [{"index": 0, "delta": d}]} for d in deltas]
    usage = {"prompt_tokens": estimate(payload["messages"]), "completion_tokens": len(chunks)}
    return [*chunks, {"choices": [], "usage": usage}]


def fixture_answer(scenario: str, payload: dict) -> tuple[int, object]:
    size = estimate(payload["messages"])
    if scenario == "oom" and size > OOM_ESTIMATE:
        return 500, {"error": {"message": "CUDA error: out of memory"}}
    if scenario == "overflow" and size > OVERFLOW_ESTIMATE:
        return 400, {"error": {"message": "This model's maximum context length is 8192 tokens",
                               "code": "context_length_exceeded"}}
    return 200, fixture_chunks(scenario, payload)


def fixture_memory(scenario: str) -> float | None:
    return FIXTURE_GB.get(scenario)


def stream_http(candidate: Candidate, payload: dict, timeout: float) -> Iterator[dict]:
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if candidate.key:
        headers["Authorization"] = f"Bearer {candidate.key}"
    target = urlsplit(candidate.base + "/chat/completions")
    deadline = time.monotonic() + timeout
    connect = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    conn = connect(target.hostname, target.port, timeout=timeout)
    expired = threading.Event()

    held: list[socket.socket] = []

    def abort() -> None:
        expired.set()
        for sock in held:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        conn.close()

    def check_deadline() -> None:
        if expired.is_set() or time.monotonic() > deadline:
            raise Failure(0, "request deadline passed", True)

    watchdog = threading.Timer(timeout, abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        conn.connect()
        held.append(conn.sock)
        conn.sock.settimeout(max(deadline - time.monotonic(), 0.01))
        conn.request("POST", target.path, json.dumps(payload).encode(), headers)
        response = conn.getresponse()
        check_deadline()
        if response.status >= 400:
            body = response.read(4000).decode("utf-8", "replace")
            check_deadline()
            raise Failure(response.status, body)
        for raw in response:
            check_deadline()
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                check_deadline()
                return
            chunk = json.loads(data)
            if isinstance(chunk, dict) and chunk.get("error"):
                raise Failure(200, json.dumps(chunk["error"]))
            yield chunk
        check_deadline()
    except TimeoutError:
        raise Failure(0, "request timed out", True) from None
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise Failure(0, str(e), expired.is_set()) from None
    finally:
        watchdog.cancel()
        conn.close()


def stream_fixture(candidate: Candidate, payload: dict) -> Iterator[dict]:
    status, body = fixture_answer(candidate.model, payload)
    if status != 200:
        raise Failure(status, json.dumps(body))
    yield from body


def parsed_object(text: str):
    try:
        found = json.loads(text)
    except ValueError:
        return None
    return found if isinstance(found, dict) and isinstance(found.get("word"), str) else None


def judge(probe: str, content: str, calls: dict, word: str) -> str:
    if probe == "text":
        return "ok" if word in content else "rejected"
    if probe == "structured":
        found = parsed_object(content)
    else:
        call = next((c for c in calls.values() if c["name"] == "report_word"), None)
        found = parsed_object(call["arguments"]) if call else None
    if found is None:
        return "malformed"
    return "ok" if found["word"] == word else "rejected"


def classify_failure(e: Failure) -> str:
    if OOM.search(e.text):
        return "out_of_memory"
    if OVERFLOW.search(e.text):
        return "context_overflow"
    return "timeout" if e.timed_out else "error"


def request_once(candidate: Candidate, spec: Spec, probe: str, messages: list[dict], word: str) -> dict:
    payload = payload_for(candidate, probe, messages, spec.max_tokens)
    sent = estimate(messages)
    started = time.monotonic()
    first, content, calls, usage, shaped = None, "", {}, {}, True
    try:
        chunks = (stream_fixture(candidate, payload) if candidate.fixture
                  else stream_http(candidate, payload, spec.timeout_seconds))
        for chunk in chunks:
            try:
                usage = accounts.mapping(chunk.get("usage")) or usage
                for choice in chunk.get("choices") or []:
                    delta = accounts.mapping(choice.get("delta"))
                    piece = delta.get("content")
                    for call in delta.get("tool_calls") or []:
                        slot = calls.setdefault(call.get("index", 0), {"name": "", "arguments": ""})
                        function = accounts.mapping(call.get("function"))
                        for part in ("name", "arguments"):
                            value = function.get(part) or ""
                            if not isinstance(value, str):
                                raise TypeError(part)
                            slot[part] += value
                    if (piece or delta.get("tool_calls")) and first is None:
                        first = time.monotonic() - started
                    content += piece if isinstance(piece, str) else ""
            except (TypeError, AttributeError):
                shaped = False
    except Failure as e:
        return {"outcome": classify_failure(e), "estimate": sent, "first": None, "seconds": None,
                "prompt_tokens": None, "completion_tokens": None}
    seconds = time.monotonic() - started
    prompt_tokens, completion_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if isinstance(prompt_tokens, int) and prompt_tokens < sent // 2:
        outcome = "context_overflow"
    else:
        outcome = judge(probe, content, calls, word) if shaped else "malformed"
    return {"outcome": outcome, "estimate": sent, "first": first, "seconds": seconds,
            "prompt_tokens": prompt_tokens if isinstance(prompt_tokens, int) else None,
            "completion_tokens": completion_tokens if isinstance(completion_tokens, int) else None, "probe": probe}


def worker_probes(candidate: Candidate, spec: Spec, context: int, level: int, worker: int) -> list[dict]:
    found = []
    for probe in PROBES:
        tag = f"{candidate.name}-{context}-{level}-{worker}-{probe}-{time.monotonic_ns()}"
        messages, word = build_messages(context, tag, worker)
        found.append({**request_once(candidate, spec, probe, messages, word), "probe": probe})
    return found


def blank_cell(context: int, level: int, status: str, reason: str = "") -> dict:
    return {"context": context, "concurrency": level, "status": status, "reason": reason,
            **dict.fromkeys(COUNTS, 0), "tokens_in": None, "first_token_seconds": None,
            "completion_seconds": None, "tokens_per_second": None, "wall_seconds": 0.0}


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def run_cell(candidate: Candidate, spec: Spec, context: int, level: int) -> tuple[dict, list[dict]]:
    started = time.monotonic()
    with ThreadPoolExecutor(level) as pool:
        futures = [pool.submit(worker_probes, candidate, spec, context, level, w) for w in range(level)]
        results = [r for f in futures for r in f.result()]
    wall = time.monotonic() - started
    cell = blank_cell(context, level, "ran")
    cell["requests"] = len(results)
    for r in results:
        cell[r["outcome"]] += 1
    reported = [r["prompt_tokens"] for r in results if r["prompt_tokens"] is not None]
    produced = [r["completion_tokens"] for r in results if r["completion_tokens"] is not None]
    cell["tokens_in"] = int(statistics.median_low(reported)) if reported else None
    cell["first_token_seconds"] = median([r["first"] for r in results if r["first"] is not None])
    cell["completion_seconds"] = median([r["seconds"] for r in results if r["seconds"] is not None])
    cell["tokens_per_second"] = round(sum(produced) / wall, 1) if produced and wall > 0 else None
    cell["wall_seconds"] = round(wall, 3)
    return cell, results


def fetch_json(url: str, key: str = ""):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.build_opener(accounts.NoRedirect).open(request, timeout=SHORT_TIMEOUT) as response:
        return json.loads(response.read(1 << 20))


def preflight_endpoint(candidate: Candidate) -> str:
    if candidate.fixture:
        return "endpoint did not answer: connection refused" if candidate.model == "offline" else ""
    try:
        body = fetch_json(candidate.base + "/models", candidate.key)
    except urllib.error.HTTPError as e:
        return f"endpoint did not answer: status {e.code}"
    except urllib.error.URLError as e:
        return f"endpoint did not answer: {util.first_line(str(e.reason), 120)}"
    except (OSError, ValueError) as e:
        return f"endpoint did not answer: {util.first_line(str(e), 120)}"
    listed = accounts.mapping(body).get("data")
    if isinstance(listed, list):
        ids = [accounts.mapping(m).get("id") for m in listed]
        if candidate.model not in ids:
            return f"the endpoint does not list model {candidate.model}; arete benchmark never pulls weights"
    return ""


def preflight_device(candidate: Candidate) -> str:
    if not candidate.device:
        return "none"
    try:
        return "ready" if devices.probe(candidate.device, timeout=20).get("ok") else "unavailable"
    except devices.DeviceError:
        return "unavailable"


def observed_gb(candidate: Candidate) -> float | None:
    if candidate.fixture:
        return fixture_memory(candidate.model)
    root = candidate.base.removesuffix("/v1")
    try:
        listed = accounts.mapping(fetch_json(root.rstrip("/") + "/api/ps", candidate.key)).get("models")
        sizes = []
        for entry in listed if isinstance(listed, list) else []:
            entry = accounts.mapping(entry)
            names = (entry.get("name"), entry.get("model"))
            if any(isinstance(n, str) and (n == candidate.model or n.startswith(candidate.model + ":"))
                   for n in names):
                size = entry.get("size_vram")
                if not number(size):
                    size = entry.get("size")
                if number(size):
                    sizes.append(size)
        return round(max(sizes) / 1e9, 2) if sizes else None
    except (OSError, ValueError):
        return None


def rate_cost(spec: Spec, failed: list[int]) -> float | None:
    if spec.escalation is None:
        return None
    rates = spec.escalation
    total = sum(e * rates["input_usd_per_mtok"] + spec.max_tokens * rates["output_usd_per_mtok"] for e in failed)
    return round(total / 1e6, 6)


def run_candidate(candidate: Candidate, spec: Spec, started: float) -> dict:
    began = time.monotonic()
    endpoint_problem = preflight_endpoint(candidate)
    device_state = preflight_device(candidate)
    device_down = device_state == "unavailable"
    available = not endpoint_problem and not device_down
    cells, failed, peak = [], [], None
    totals = dict.fromkeys(COUNTS, 0)
    structured, tools = {"ok": 0, "requests": 0}, {"ok": 0, "requests": 0}
    for context in spec.contexts:
        for level in spec.concurrency:
            if not available:
                cells.append(blank_cell(context, level, "not_run", "candidate unavailable"))
            elif context > candidate.window:
                cells.append(blank_cell(context, level, "unsupported", f"window is {candidate.window}"))
            elif time.monotonic() - started >= spec.max_seconds:
                cells.append(blank_cell(context, level, "not_run", "time budget"))
            else:
                cell, results = run_cell(candidate, spec, context, level)
                cells.append(cell)
                for r in results:
                    totals["requests"] += 1
                    totals[r["outcome"]] += 1
                    if r["outcome"] != "ok":
                        failed.append(r["estimate"])
                    group = structured if r["probe"] == "structured" else tools if r["probe"] == "tool" else None
                    if group is not None:
                        group["requests"] += 1
                        group["ok"] += r["outcome"] == "ok"
                if (gb := observed_gb(candidate)) is not None:
                    peak = gb if peak is None else max(peak, gb)
    detail = endpoint_problem or ("device did not answer the readiness probe" if device_down else "")
    sent = totals["requests"]
    return {"manifest": candidate.manifest(), "status": "ran" if available else "unavailable", "detail": detail,
            "preflight": {"endpoint": "unavailable" if endpoint_problem else "ready", "device": device_state},
            "observed_memory_gb": peak, "wall_seconds": round(time.monotonic() - began, 3), "api_spend_usd": 0.0,
            "totals": totals, "quality": {"score": round(totals["ok"] / sent, 3) if sent else None,
                                          "accepted": totals["ok"], "requests": sent},
            "structured": structured, "tools": tools,
            "escalation": {"requests": len(failed), "cost_usd": rate_cost(spec, failed)}, "cells": cells}


def run(spec: Spec) -> dict:
    stamp = util.now()
    started = time.monotonic()
    found = {c.name: run_candidate(c, spec, started) for c in spec.candidates}
    costs = [c["escalation"]["cost_usd"] for c in found.values()]
    return {"benchmark": stamp.strftime("b-%Y%m%d-%H%M%S"), "started": util.iso(stamp),
            "wall_seconds": round(time.monotonic() - started, 3), "api_spend_usd": 0.0, "note": NOTE,
            "settings": {"contexts": spec.contexts, "concurrency": spec.concurrency, "max_tokens": spec.max_tokens,
                         "timeout_seconds": spec.timeout_seconds, "max_seconds": spec.max_seconds,
                         "escalation": spec.escalation},
            "escalation_cost_usd": round(sum(costs), 6) if spec.escalation else None, "candidates": found}


def shown(value, template: str = "{}") -> str:
    return "unknown" if value is None else template.format(value)


def report_lines(report: dict) -> list[str]:
    lines = [f"benchmark {report['benchmark']} started {report['started']}", report["note"]]
    ran = 0
    for name, c in report["candidates"].items():
        m = c["manifest"]
        ran += c["status"] == "ran"
        detail = f", {c['detail']}" if c["status"] == "unavailable" and c["detail"] else ""
        lines.append(f"{name}: {c['status']}, {m['route']}, {m['quantization']}, {m['runtime']} "
                     f"{m['runtime_version']}, {m['hardware']} {m['memory_gb']} GB, window {m['window']}{detail}")
        score = c["quality"]["score"]
        quality = "unknown (not scored)" if score is None else f"{score}"
        failures = [f"{k} {c['totals'][k]}" for k in FAILURES if c["totals"][k]]
        gb = c["observed_memory_gb"]
        memory = "memory unknown" if gb is None else f"memory {gb} GB observed"
        e = c["escalation"]
        escalation = (f"escalation {e['requests']} requests ${e['cost_usd']:.4f}" if e["cost_usd"] is not None
                      else f"escalation {e['requests']} requests, cost unknown")
        parts = [f"quality {quality}", f"structured {c['structured']['ok']} of {c['structured']['requests']}",
                 f"tools {c['tools']['ok']} of {c['tools']['requests']}", *failures, memory,
                 f"wall {c['wall_seconds']} s", "API spend $0.0000", escalation]
        lines.append("  " + ", ".join(parts))
        for cell in c["cells"]:
            lines.append(f"    {cell['context']} ctx x{cell['concurrency']}: {cell['status']}, "
                         f"requests {cell['requests']}, ok {cell['ok']}, "
                         f"tokens_in {shown(cell['tokens_in'])}, "
                         f"first token {shown(cell['first_token_seconds'], '{:.3f} s')}, "
                         f"completion {shown(cell['completion_seconds'], '{:.3f} s')}, "
                         f"{shown(cell['tokens_per_second'], '{:.1f} tokens/s')}, wall {cell['wall_seconds']:.3f} s")
    cost = report["escalation_cost_usd"]
    lines.append(f"summary: {len(report['candidates'])} candidates, {ran} ran, "
                 f"{len(report['candidates']) - ran} unavailable, wall {report['wall_seconds']:.1f} s, "
                 f"API spend $0.0000, escalation cost " + ("unknown" if cost is None else f"${cost:.4f}"))
    return lines


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:
        pass

    def route(self) -> tuple[str, str] | None:
        parts = self.path.split("?")[0].strip("/").split("/", 1)
        if len(parts) == 2 and parts[0] in SERVED:
            return parts[0], parts[1]
        return None

    def reply(self, status: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        found = self.route()
        if found and found[1] == "v1/models":
            self.reply(200, json.dumps({"data": [{"id": found[0]}]}).encode())
        elif found and found[1] == "api/ps" and fixture_memory(found[0]) is not None:
            size = int(fixture_memory(found[0]) * 1e9)
            self.reply(200, json.dumps({"models": [{"name": found[0], "size_vram": size}]}).encode())
        else:
            self.reply(404, b"{}")

    def do_POST(self) -> None:
        found = self.route()
        if not found or found[1] != "v1/chat/completions":
            self.reply(404, b"{}")
            return
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            status, body = fixture_answer(found[0], payload)
        except (ValueError, KeyError, TypeError, IndexError):
            self.reply(400, b'{"error": {"message": "bad request"}}')
            return
        if status != 200:
            self.reply(status, json.dumps(body).encode())
            return
        stream = "".join(f"data: {json.dumps(c)}\n\n" for c in body) + "data: [DONE]\n\n"
        self.reply(200, stream.encode(), "text/event-stream")


def serve_fixture(port: int, seconds: float) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = util.iso(datetime.fromtimestamp(time.time() + seconds, UTC))
    urls = ", ".join(f"http://127.0.0.1:{port}/{s}/v1" for s in SERVED)
    print(f"fixture endpoints: {urls}; stops at {stop}",
          flush=True)
    time.sleep(seconds)
    server.shutdown()
    server.server_close()
    return 0
