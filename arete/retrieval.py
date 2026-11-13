from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, config, environment, evals, paths, pipeline, record, replay, routing, runner, util

SPLITS = ("train", "validation", "holdout")
KINDS = ("embedding", "rerank")
SEARCHES = ("exact", "hybrid")
NAME = re.compile(r"[A-Za-z0-9_-]+")
TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[./][A-Za-z0-9_]+)*")
TOKEN = re.compile(r"[a-z0-9]+")
RANGE = re.compile(r"(.+):(\d+)-(\d+)")
FUSION_K = 60
BM25_K1, BM25_B = 1.2, 0.75
CHECK_SECONDS = 120
SAMPLE_REPO = "retrieval-sample"
NO_LABELS = "no relevant files or passages"
NO_ACCEPTANCE = "no acceptance check"
LAST_LINE = "This is an offline measurement; it changes no retrieval setting."
GENERATION = ("runner", "model", "provider", "auth_mode", "effort", "tokens_in", "tokens_out", "cost_usd",
              "charged_usd", "reference_usd", "generation_seconds")


class Refused(ValueError):
    pass


class Unavailable(Exception):
    def __init__(self, why: str, adapter: str = "", cost: float | None = None):
        super().__init__(why)
        self.why, self.adapter, self.cost = why, adapter, cost


@dataclass
class CorpusSpec:
    revision: str = "HEAD"
    include: list[str] = field(default_factory=lambda: ["**"])
    exclude: list[str] = field(default_factory=list)
    passage_lines: int = 40
    max_files: int = 2000


@dataclass
class Adapter:
    name: str
    kind: str
    replay: str
    command: str
    route: str
    model: str
    version: str
    timeout: float


@dataclass
class Arm:
    name: str
    search: str
    embedding: str
    rerank: str


@dataclass
class Query:
    name: str
    text: str
    relevant: list[tuple[str, int, int]]
    accept: list[str]
    check: str
    split: str


@dataclass
class Generate:
    model: str
    runner: str
    effort: str
    timeout_minutes: float


@dataclass
class Suite:
    name: str
    fingerprint: str
    budget: int
    top_k: int
    spend: float
    corpus: CorpusSpec
    generate: Generate | None
    adapters: dict[str, Adapter]
    arms: dict[str, Arm]
    queries: list[Query]


@dataclass
class Passage:
    id: str
    path: str
    start: int
    end: int
    text: str

    @property
    def block(self) -> str:
        return f"### {self.id}\n{self.text}"


@dataclass
class Corpus:
    commit: str
    fingerprint: str
    files: int
    passages: list[Passage]


@dataclass
class Reply:
    data: dict
    cost: float | None
    seconds: float


@dataclass
class Embedded:
    vectors: dict
    why: str = ""
    adapter: str = ""
    seconds: float | None = 0.0
    cost: float | None = 0.0


@dataclass
class Found:
    ranking: list[str]
    seconds: float
    cost: float | None


def digest(raw: bytes | str) -> str:
    return hashlib.sha256(raw.encode() if isinstance(raw, str) else raw).hexdigest()[:12]


def unknown(table: dict, allowed: tuple[str, ...], where: str) -> None:
    if extra := sorted(set(table) - set(allowed)):
        raise Refused(f"{where}: unknown key `{extra[0]}`")


def subtable(data: dict, key: str, where: str) -> dict:
    found = data.get(key, {})
    if not isinstance(found, dict):
        raise Refused(f"{where}: `{key}` must be a table")
    return found


def named_tables(data: dict, key: str, where: str) -> dict[str, dict]:
    found = subtable(data, key, where)
    for name, table in found.items():
        if not NAME.fullmatch(name) or not isinstance(table, dict):
            raise Refused(f"[{key}.{name}]: a name of letters, digits, `_` and `-` and a table are required")
    return found


def text(table: dict, key: str, where: str, required: bool = False, choices: tuple[str, ...] = ()) -> str:
    value = table.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise Refused(f"{where}: `{key}` must be {'a non-empty ' if required else 'a '}string")
    if choices and value not in choices:
        raise Refused(f"{where}: `{key}` must be one of {', '.join(choices)}")
    return value


def number(table: dict, key: str, where: str, default: float | None = None, whole: bool = False) -> float:
    if key not in table:
        if default is None:
            raise Refused(f"{where}: `{key}` is required")
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0 or (whole and int(value) != value):
        raise Refused(f"{where}: `{key}` must be a {'whole ' if whole else ''}number above 0")
    return int(value) if whole else float(value)


def strings(table: dict, key: str, where: str, default: list[str] | None = None) -> list[str]:
    value = table.get(key, default or [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise Refused(f"{where}: `{key}` must be a list of non-empty strings")
    return value


def label(raw: str, where: str) -> tuple[str, int, int]:
    found = RANGE.fullmatch(raw)
    if not found:
        return raw, 0, 0
    start, end = int(found[2]), int(found[3])
    if not 1 <= start <= end:
        raise Refused(f"{where}: `relevant` range `{raw}` must run from a line to a later or equal line")
    return found[1], start, end


def adapter_of(name: str, table: dict) -> Adapter:
    where = f"[adapters.{name}]"
    unknown(table, ("kind", "replay", "command", "route", "model", "version", "timeout_seconds"), where)
    replay_file, command = text(table, "replay", where), text(table, "command", where)
    if bool(replay_file) == bool(command):
        raise Refused(f"{where}: exactly one of `replay` and `command` is required")
    return Adapter(name, text(table, "kind", where, True, KINDS), replay_file, command, text(table, "route", where),
                   text(table, "model", where), text(table, "version", where),
                   number(table, "timeout_seconds", where, 60))


def arm_of(name: str, table: dict, adapters: dict[str, Adapter]) -> Arm:
    where = f"[arms.{name}]"
    unknown(table, ("search", "embedding", "rerank"), where)
    arm = Arm(name, text(table, "search", where, True, SEARCHES), text(table, "embedding", where),
              text(table, "rerank", where))
    if arm.search == "exact" and (arm.embedding or arm.rerank):
        raise Refused(f"{where}: an exact arm takes no adapters")
    for key, kind in (("embedding", "embedding"), ("rerank", "rerank")):
        named = getattr(arm, key)
        if named and (named not in adapters or adapters[named].kind != kind):
            raise Refused(f"{where}: `{key}` names `{named}`, which is not an {kind} adapter in [adapters]")
    return arm


def query_of(name: str, table: dict) -> Query:
    where = f"[queries.{name}]"
    unknown(table, ("query", "relevant", "accept", "check", "split"), where)
    return Query(name, text(table, "query", where, True),
                 [label(r, where) for r in strings(table, "relevant", where)], strings(table, "accept", where),
                 text(table, "check", where), text(table, "split", where, True, SPLITS))


def corpus_spec_of(table: dict) -> CorpusSpec:
    where = "[corpus]"
    unknown(table, ("revision", "include", "exclude", "passage_lines", "max_files"), where)
    return CorpusSpec(text(table, "revision", where) or "HEAD", strings(table, "include", where, ["**"]),
                      strings(table, "exclude", where), int(number(table, "passage_lines", where, 40, True)),
                      int(number(table, "max_files", where, 2000, True)))


def generate_of(table: dict) -> Generate:
    where = "[generate]"
    unknown(table, ("model", "runner", "effort", "timeout_minutes"), where)
    return Generate(text(table, "model", where, True), text(table, "runner", where), text(table, "effort", where),
                    number(table, "timeout_minutes", where, 5))


def load_suite(path: Path, name: str) -> Suite:
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode())
    unknown(data, ("budget_tokens", "top_k", "spend_usd", "corpus", "generate", "adapters", "arms", "queries"),
            "the suite")
    adapters = {n: adapter_of(n, t) for n, t in named_tables(data, "adapters", "the suite").items()}
    arms = {n: arm_of(n, t, adapters) for n, t in named_tables(data, "arms", "the suite").items()}
    queries = [query_of(n, t) for n, t in named_tables(data, "queries", "the suite").items()]
    if not any(a.search == "exact" for a in arms.values()):
        raise Refused("[arms]: an exact arm is required")
    if not any(a.search == "hybrid" and not (a.embedding or a.rerank) for a in arms.values()):
        raise Refused("[arms]: a hybrid arm without adapters is required as the baseline")
    if not queries:
        raise Refused("[queries]: at least one query is required")
    generate = generate_of(subtable(data, "generate", "the suite")) if "generate" in data else None
    spend = number(data, "spend_usd", "the suite", 0) if generate else 0.0
    if generate and not spend:
        raise Refused("the suite: `spend_usd` is required with [generate]")
    return Suite(name, digest(raw), int(number(data, "budget_tokens", "the suite", whole=True)),
                 int(number(data, "top_k", "the suite", 20, True)), spend,
                 corpus_spec_of(subtable(data, "corpus", "the suite")), generate, adapters, arms, queries)


def wanted(path: str, spec: CorpusSpec) -> bool:
    return (not path.startswith(".agents/") and util.matches_any(path, spec.include)
            and not util.matches_any(path, spec.exclude))


def passages_of(path: str, raw: bytes, size: int) -> list[Passage]:
    lines = raw.decode().splitlines()
    found = []
    for first in range(0, len(lines), size):
        window = lines[first:first + size]
        body = "\n".join(window)
        if body.strip():
            last = first + len(window)
            found.append(Passage(f"{path}:{first + 1}-{last}", path, first + 1, last, body))
    return found


def fingerprint_of(files: dict[str, bytes], size: int) -> str:
    h = hashlib.sha256()
    for path in sorted(files):
        for part in (path.encode(), files[path]):
            h.update(len(part).to_bytes(8, "big"))
            h.update(part)
    h.update(str(size).encode())
    return h.hexdigest()[:12]


def build_corpus(repo: config.Repo, spec: CorpusSpec) -> Corpus:
    found = util.sh(["git", "rev-parse", "--verify", "--quiet", f"{spec.revision}^{{commit}}"], cwd=repo.path)
    commit = found.stdout.strip()
    if found.returncode != 0 or not commit:
        raise Refused(f"[corpus]: `revision` {spec.revision} does not resolve to a commit in {repo.name}")
    source = evals.Source(repo, commit)
    names = sorted(f for f in source.files() if wanted(f, spec))
    if len(names) > spec.max_files:
        raise Refused(f"[corpus]: {len(names)} files is more than `max_files` {spec.max_files}")
    files: dict[str, bytes] = {}
    for name in names:
        if source.link(name) is not None:
            continue
        raw = source.read(name)
        try:
            raw.decode()
        except UnicodeDecodeError:
            continue
        if b"\0" not in raw:
            files[name] = raw
    passages = [p for name, raw in files.items() for p in passages_of(name, raw, spec.passage_lines)]
    return Corpus(commit, fingerprint_of(files, spec.passage_lines), len(files), passages)


def is_name(term: str) -> bool:
    return any(c in "_./" for c in term) or any(c.isdigit() or c.isupper() for c in term[1:])


def exact_ranking(passages: list[Passage], query: str) -> list[str]:
    terms = list(dict.fromkeys(TERM.findall(query)))
    names = [t for t in terms if is_name(t)]
    words = [] if names else list(dict.fromkeys(t.lower() for t in terms if len(t) >= 3))
    scored = []
    for p in passages:
        counts = ([p.text.count(n) + p.path.count(n) for n in names] if names
                  else [len(re.findall(rf"\b{re.escape(w)}\b", p.text, re.IGNORECASE)) for w in words])
        hits = [c for c in counts if c]
        if hits:
            scored.append((-len(hits), -sum(hits), p.id))
    return [s[2] for s in sorted(scored)]


class Bm25:
    def __init__(self, passages: list[Passage]):
        self.ids = [p.id for p in passages]
        self.counts = [Counter(TOKEN.findall(p.text.lower())) for p in passages]
        self.lengths = [sum(c.values()) for c in self.counts]
        self.average = sum(self.lengths) / len(passages) if passages else 0.0
        self.frequency = Counter(t for c in self.counts for t in c)

    def ranking(self, query: str) -> list[str]:
        n, scored = len(self.ids), []
        for pid, counts, length in zip(self.ids, self.counts, self.lengths, strict=True):
            score = 0.0
            for term in TOKEN.findall(query.lower()):
                if not (f := counts[term]):
                    continue
                idf = math.log(1 + (n - self.frequency[term] + 0.5) / (self.frequency[term] + 0.5))
                score += idf * f * (BM25_K1 + 1) / (f + BM25_K1 * (1 - BM25_B + BM25_B * length / self.average))
            if score > 0:
                scored.append((-score, pid))
        return [s[1] for s in sorted(scored)]


def cosine(a: list[float], b: list[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0


def cosine_ranking(passages: list[Passage], vectors: dict, query_vector: list[float]) -> list[str]:
    return [i for _, i in sorted((-cosine(vectors[p.id], query_vector), p.id) for p in passages if p.id in vectors)]


def fuse(rankings: list[list[str]]) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for place, pid in enumerate(ranking, 1):
            scores[pid] = scores.get(pid, 0.0) + 1 / (FUSION_K + place)
    return sorted(scores, key=lambda i: (-scores[i], i))


def rerank_order(ranking: list[str], scores: dict) -> list[str]:
    scored = sorted((i for i in ranking if i in scores), key=lambda i: (-scores[i], i))
    return scored + [i for i in ranking if i not in scores]


def supply(by_id: dict[str, Passage], ranking: list[str], budget: int) -> tuple[list[Passage], int]:
    chosen, used = [], 0
    for pid in ranking:
        size = routing.request_tokens(by_id[pid].block, "", None)
        if used + size <= budget:
            chosen.append(by_id[pid])
            used += size
    return chosen, used


def recall_of(relevant: list[tuple[str, int, int]], supplied: list[Passage]) -> float | None:
    if not relevant:
        return None
    covered = sum(1 for path, start, end in relevant
                  if any(p.path == path and (not start or (p.start <= end and start <= p.end)) for p in supplied))
    return covered / len(relevant)


def finite(x) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def amount(data: dict, key: str, default: float | None) -> float | None:
    value = data.get(key)
    return float(value) if finite(value) and value >= 0 else default


def reply_of(data: dict) -> Reply:
    return Reply(data, amount(data, "cost_usd", None), amount(data, "seconds", 0.0) or 0.0)


class Hub:
    def __init__(self, suite: Suite, cfg: config.Config, repo: config.Repo, folder: Path, corpus: Corpus):
        self.suite, self.cfg, self.repo, self.folder, self.corpus = suite, cfg, repo, folder, corpus
        self.files: dict[Path, dict | Unavailable] = {}

    def read(self, adapter: Adapter) -> dict:
        path = self.folder / adapter.replay
        if path not in self.files:
            try:
                data = json.loads(path.read_text())
                if not isinstance(data, dict):
                    raise TypeError("not one JSON object")
                self.files[path] = data
            except FileNotFoundError:
                self.files[path] = Unavailable(f"adapter {adapter.name}: replay file {adapter.replay} is missing",
                                               adapter.name)
            except (OSError, ValueError, TypeError) as e:
                self.files[path] = Unavailable(f"adapter {adapter.name}: cannot use {adapter.replay}: {e}",
                                               adapter.name)
        if isinstance(found := self.files[path], Unavailable):
            raise found
        return found

    def command(self, adapter: Adapter, request: dict) -> dict:
        res = environment.sh(adapter.command, self.cfg, self.repo, cwd=self.folder, timeout=adapter.timeout,
                             input=json.dumps(request))
        if res.returncode == util.TIMED_OUT:
            raise Unavailable(f"adapter {adapter.name}: timed out after {adapter.timeout:g}s", adapter.name)
        if res.returncode != 0:
            raise Unavailable(f"adapter {adapter.name}: command exit {res.returncode}", adapter.name)
        try:
            data = json.loads(res.stdout)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise Unavailable(f"adapter {adapter.name}: command output is not a JSON object", adapter.name)
        return data

    def embed(self, adapter: Adapter, queries: list[Query]) -> Embedded:
        started = time.perf_counter()
        if adapter.replay:
            data = self.read(adapter)
        else:
            texts = [{"id": p.id, "text": p.text} for p in self.corpus.passages]
            texts += [{"id": f"query:{q.name}", "text": q.text} for q in queries]
            data = self.command(adapter, {"kind": "embedding", "texts": texts})
        reply = reply_of(data)
        if data.get("corpus") not in (None, self.corpus.fingerprint):
            raise Unavailable(f"adapter {adapter.name}: made for corpus {data['corpus']}, "
                              f"not {self.corpus.fingerprint}", adapter.name, reply.cost)
        vectors = data.get("vectors")
        if not isinstance(vectors, dict) or not vectors or not all(
                isinstance(v, list) and all(finite(x) for x in v) for v in vectors.values()):
            raise Unavailable(f"adapter {adapter.name}: the reply holds no usable vectors", adapter.name, reply.cost)
        lengths = sorted({len(v) for v in vectors.values()})
        if len(lengths) != 1 or lengths[0] == 0:
            raise Unavailable(f"adapter {adapter.name}: vectors must share one nonzero length, "
                              f"found lengths {', '.join(map(str, lengths))}", adapter.name, reply.cost)
        for q in queries:
            if f"query:{q.name}" not in vectors:
                raise Unavailable(f"adapter {adapter.name}: no vector for query {q.name}", adapter.name, reply.cost)
        return Embedded(vectors, seconds=time.perf_counter() - started + reply.seconds, cost=reply.cost)

    def rerank(self, adapter: Adapter, query: Query, candidates: list[Passage]) -> tuple[dict, Reply]:
        if adapter.replay:
            data = self.read(adapter)
            entry = (data.get("queries") or {}).get(query.name) if isinstance(data.get("queries"), dict) else None
        else:
            data = entry = self.command(adapter, {"kind": "rerank", "query_id": query.name, "query": query.text,
                                                  "passages": [{"id": p.id, "text": p.text} for p in candidates]})
        reply = reply_of(entry if isinstance(entry, dict) else {})
        if data.get("corpus") not in (None, self.corpus.fingerprint):
            raise Unavailable(f"adapter {adapter.name}: made for corpus {data['corpus']}, "
                              f"not {self.corpus.fingerprint}", adapter.name, reply.cost)
        scores = entry.get("scores") if isinstance(entry, dict) else None
        if not isinstance(scores, dict) or not all(finite(s) for s in scores.values()):
            raise Unavailable(f"adapter {adapter.name}: no scores for query {query.name}", adapter.name, reply.cost)
        return scores, reply


def adapter_entry(adapter: Adapter | None, folder: Path, broken: str = "") -> dict | None:
    if not adapter:
        return None
    try:
        seed = (folder / adapter.replay).read_bytes() if adapter.replay else adapter.command.encode()
    except OSError:
        seed = b""
    return {"adapter": adapter.name, "source": "replay" if adapter.replay else "command", "route": adapter.route,
            "model": adapter.model, "version": adapter.version, "fingerprint": digest(seed) if seed else "",
            "available": not broken, "why": broken}


class Experiment:
    def __init__(self, suite: Suite, queries: list[Query], cfg: config.Config, repo: config.Repo, corpus: Corpus,
                 folder: Path, run_id: str, run_dir: Path):
        self.suite, self.queries, self.cfg, self.repo, self.corpus = suite, queries, cfg, repo, corpus
        self.folder, self.run_id, self.run_dir = folder, run_id, run_dir
        self.hub = Hub(suite, cfg, repo, folder, corpus)
        self.bm25 = Bm25(corpus.passages)
        self.by_id = {p.id: p for p in corpus.passages}
        self.charged = 0.0
        self.broken: dict[tuple[str, str], str] = {}

    def index(self, arm: Arm) -> Embedded:
        if not arm.embedding:
            return Embedded({})
        try:
            return self.hub.embed(self.suite.adapters[arm.embedding], self.queries)
        except Unavailable as e:
            self.broken[(arm.name, e.adapter)] = e.why
            return Embedded({}, e.why, e.adapter, None, e.cost)

    def retrieve(self, arm: Arm, query: Query, index: Embedded) -> Found:
        started = time.perf_counter()
        ranking = exact_ranking(self.corpus.passages, query.text)
        if arm.search == "hybrid":
            lists = [ranking, self.bm25.ranking(query.text)]
            if arm.embedding:
                if index.why:
                    raise Unavailable(index.why, index.adapter, 0.0)
                lists.append(cosine_ranking(self.corpus.passages, index.vectors, index.vectors[f"query:{query.name}"]))
            ranking = fuse(lists)
        ranking = ranking[:self.suite.top_k]
        cost, extra = 0.0, 0.0
        if arm.rerank:
            scores, reply = self.hub.rerank(self.suite.adapters[arm.rerank], query, [self.by_id[i] for i in ranking])
            ranking, cost, extra = rerank_order(ranking, scores), reply.cost, reply.seconds
        return Found(ranking, time.perf_counter() - started + extra, cost)

    def blank(self, arm: Arm, query: Query) -> dict:
        return {"arm": arm.name, "query": query.name, "split": query.split, "status": "available", "why": "",
                "recall": None, "recall_why": "" if query.relevant else NO_LABELS, "labeled": len(query.relevant),
                "supplied": [], "tokens_supplied": None, "retrieval_seconds": None, "retrieval_cost_usd": None,
                "acceptance_why": "" if query.accept or query.check else NO_ACCEPTANCE,
                "accepted": None, "accepted_why": "", "failure": "", **dict.fromkeys(GENERATION)}

    def result(self, arm: Arm, query: Query, index: Embedded) -> dict:
        row = self.blank(arm, query)
        try:
            found = self.retrieve(arm, query, index)
        except Unavailable as e:
            self.broken.setdefault((arm.name, e.adapter), e.why)
            return {**row, "status": "unavailable", "why": e.why, "retrieval_cost_usd": e.cost}
        supplied, used = supply(self.by_id, found.ranking, self.suite.budget)
        row.update(recall=recall_of(query.relevant, supplied), supplied=[p.id for p in supplied],
                   tokens_supplied=used, retrieval_seconds=found.seconds, retrieval_cost_usd=found.cost)
        self.generate(row, arm, query, supplied)
        return row

    def judge(self, query: Query, answer: str) -> str:
        for needle in query.accept:
            if needle.lower() not in answer.lower():
                return f"the answer lacks `{needle}`"
        if query.check:
            res = environment.sh(query.check, self.cfg, self.repo, cwd=self.run_dir, input=answer,
                                 timeout=CHECK_SECONDS)
            if res.returncode != 0:
                return "check timed out" if res.returncode == util.TIMED_OUT else f"check exit {res.returncode}"
        return ""

    def generate(self, row: dict, arm: Arm, query: Query, supplied: list[Passage]) -> None:
        gen = self.suite.generate
        if not gen:
            row["accepted_why"] = "not run: the suite has no [generate]"
            return
        if self.charged >= self.suite.spend:
            row["accepted_why"] = row["failure"] = f"not run: the spend limit of ${self.suite.spend:g} was reached"
            return
        question = pipeline.prompt("retrieval_answer", question=query.text,
                                   context="\n\n".join(p.block for p in supplied))
        extra = {"effort": gen.effort} if gen.effort else {}
        started = time.perf_counter()
        res = runner.run(question, self.run_dir, model=gen.model, runner=gen.runner, tools=[], persist=False,
                         wait_out_limits=False, timeout=gen.timeout_minutes * 60,
                         max_budget_usd=self.suite.spend - self.charged,
                         trace=(self.run_id, f"retrieval {arm.name} {query.name}"), cfg=self.cfg, repo=self.repo,
                         **extra)
        used = res.runner or (runner.REPLAY if self.cfg.replay.all else gen.runner)
        charged = 0.0 if used == runner.REPLAY else res.paid_usd
        self.charged += charged
        row.update(runner=used, model=res.model or gen.model, provider=res.provider, auth_mode=res.auth_mode,
                   effort=res.effort or gen.effort, tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                   cost_usd=res.cost_usd, charged_usd=charged, reference_usd=res.reference_usd,
                   generation_seconds=res.api_seconds or time.perf_counter() - started)
        if not res.ok:
            row["failure"] = f"generation failed: {util.first_line(res.error)}"
            row["accepted"] = False if query.accept or query.check else None
            row["accepted_why"] = "" if query.accept or query.check else NO_ACCEPTANCE
        elif not (query.accept or query.check):
            row["accepted_why"] = NO_ACCEPTANCE
        else:
            row["failure"] = self.judge(query, res.text)
            row["accepted"] = not row["failure"]

    def execute(self) -> dict:
        arms, results = {}, []
        for arm in self.suite.arms.values():
            index = self.index(arm)
            results += [self.result(arm, q, index) for q in self.queries]
            arms[arm.name] = {
                "search": arm.search,
                "embedding": adapter_entry(self.suite.adapters.get(arm.embedding), self.folder,
                                           self.broken.get((arm.name, arm.embedding), "")),
                "rerank": adapter_entry(self.suite.adapters.get(arm.rerank), self.folder,
                                        self.broken.get((arm.name, arm.rerank), "")),
                "index_seconds": index.seconds, "index_cost_usd": index.cost}
        gen = self.suite.generate
        prompt_file = paths.PROMPTS_DIR / "retrieval_answer.md"
        return {
            "suite": self.suite.name, "repo": self.repo.name, "run": self.run_id, "arete_version": __version__,
            "suite_fingerprint": self.suite.fingerprint, "budget_tokens": self.suite.budget,
            "top_k": self.suite.top_k,
            "corpus": {"revision": self.suite.corpus.revision, "commit": self.corpus.commit,
                       "fingerprint": self.corpus.fingerprint, "files": self.corpus.files,
                       "passages": len(self.corpus.passages), "passage_lines": self.suite.corpus.passage_lines},
            "generate": {"model": gen.model, "runner": gen.runner, "effort": gen.effort,
                         "prompt_fingerprint": digest(prompt_file.read_bytes())} if gen else None,
            "arms": arms, "results": results, "summary": summarize(arms, results)}


def total(parts: list[float | None]) -> float | None:
    return None if any(p is None for p in parts) else sum(p for p in parts if p is not None)


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def scope_summary(arm: dict, rows: list[dict]) -> dict:
    live = [r for r in rows if r["status"] == "available"]
    recalls = [r["recall"] for r in live if r["recall"] is not None]
    checked = [r for r in live if r["accepted"] is not None]
    retrieval = total([arm["index_cost_usd"], *(r["retrieval_cost_usd"] for r in rows)])
    generation = sum(r["cost_usd"] or 0.0 for r in live)
    return {"queries": len(rows), "unavailable": len(rows) - len(live), "labeled": len(recalls),
            "recall": rounded(mean(recalls)), "accepted": sum(1 for r in checked if r["accepted"]),
            "checked": len(checked), "generated": sum(1 for r in live if r["runner"]),
            "tokens_supplied": rounded(mean([r["tokens_supplied"] for r in live])),
            "retrieval_seconds": rounded(mean([r["retrieval_seconds"] for r in live])),
            "retrieval_cost_usd": rounded(retrieval), "generation_cost_usd": rounded(generation),
            "total_cost_usd": rounded(None if retrieval is None else retrieval + generation),
            "charged_usd": rounded(sum(r["charged_usd"] or 0.0 for r in live))}


def summarize(arms: dict, results: list[dict]) -> dict:
    splits = [s for s in SPLITS if any(r["split"] == s for r in results)]
    return {name: {scope: scope_summary(arm, [r for r in results if r["arm"] == name
                                              and scope in ("all", r["split"])])
                   for scope in ["all", *splits]}
            for name, arm in arms.items()}


def money(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.4f}"


def table_row(arm: str, scope: str, s: dict) -> str:
    if s["unavailable"] == s["queries"]:
        recall = accepted = "unavailable"
    else:
        recall = f"{s['recall']:.2f}" if s["recall"] is not None else "no labels"
        accepted = (f"{s['accepted']}/{s['checked']}" if s["checked"]
                    else "no check" if s["generated"] else "not run")
    tokens = "-" if s["tokens_supplied"] is None else f"{s['tokens_supplied']:.0f}"
    seconds = "-" if s["retrieval_seconds"] is None else f"{s['retrieval_seconds']:.3f}"
    return (f"{arm:<16} {scope:<11} {s['queries']:>3} {s['unavailable']:>5} {recall:>11} {accepted:>11} {tokens:>7} "
            f"{seconds:>8} {money(s['retrieval_cost_usd']):>9} {money(s['generation_cost_usd']):>9} "
            f"{money(s['total_cost_usd']):>9}")


def unavailable_lines(results: list[dict]) -> list[str]:
    lines = []
    for arm in dict.fromkeys(r["arm"] for r in results):
        down = [r for r in results if r["arm"] == arm and r["status"] == "unavailable"]
        if down and len({r["why"] for r in down}) == 1:
            lines.append(f"  {arm}: {down[0]['why']}")
        else:
            lines += [f"  {arm} {r['query']}: {r['why']}" for r in down]
    return lines


def missing_lines(results: list[dict]) -> list[str]:
    lines = []
    for name in dict.fromkeys(r["query"] for r in results):
        first = next(r for r in results if r["query"] == name)
        reasons = [w for w in (first["recall_why"], first["acceptance_why"]) if w]
        if reasons:
            lines.append(f"  {name} ({first['split']}): {'; '.join(reasons)}")
    return lines


def report(data: dict, where: str) -> list[str]:
    corpus = data["corpus"]
    head = (f"{data['repo']} {data['suite']}  commit {corpus['commit'][:12]}  corpus {corpus['fingerprint']}  "
            f"files {corpus['files']}  passages {corpus['passages']}  budget {data['budget_tokens']} tokens  "
            f"top_k {data['top_k']}  run {data['run']}  results {where}")
    columns = (f"{'arm':<16} {'split':<11} {'qs':>3} {'unav':>5} {'recall':>11} {'accepted':>11} {'tokens':>7} "
               f"{'ret s':>8} {'retrieval':>9} {'generate':>9} {'total':>9}")
    lines = [head, "", columns]
    for arm, scopes in data["summary"].items():
        lines += [table_row(arm, scope, s) for scope, s in scopes.items()]
    results = data["results"]
    failures = [f"  {r['arm']} {r['query']} ({r['split']}): {r['failure']}" for r in results if r["failure"]]
    for title, body in (("Downstream failures:", failures), ("Unavailable:", unavailable_lines(results)),
                        ("Missing labels:", missing_lines(results))):
        if body:
            lines += ["", title, *body]
    return [*lines, "", LAST_LINE]


def dry_lines(suite: Suite, queries: list[Query], corpus: Corpus, folder: Path, cfg: config.Config) -> list[str]:
    head = (f"corpus: commit {corpus.commit} fingerprint {corpus.fingerprint} files {corpus.files} "
            f"passages {len(corpus.passages)}")
    lines = [head, f"budget: {suite.budget} tokens, top_k {suite.top_k}", "arms:"]
    for arm in suite.arms.values():
        parts = []
        for adapter in (suite.adapters.get(arm.embedding), suite.adapters.get(arm.rerank)):
            if adapter:
                found = "present" if (folder / adapter.replay).is_file() else "unavailable: file missing"
                how = f"replay {adapter.replay}, {found}" if adapter.replay else "command, not called in a dry run"
                parts.append(f"{adapter.kind} {adapter.name} ({how})")
        lines.append(f"  {arm.name}: {arm.search}" + (f" with {'; '.join(parts)}" if parts else ""))
    lines.append("queries:")
    for split in SPLITS:
        if names := [q.name for q in queries if q.split == split]:
            lines.append(f"  {split}: {', '.join(names)}")
    for q in queries:
        if not q.relevant:
            lines.append(f"  {q.name}: {NO_LABELS}")
        if not (q.accept or q.check):
            lines.append(f"  {q.name}: {NO_ACCEPTANCE}")
    gen = suite.generate
    lines.append("generation: not configured, downstream is not run" if not gen else
                 "generation: replay answers" if cfg.replay.all
                 else f"generation: model {gen.model}" + (f" on runner {gen.runner}" if gen.runner else ""))
    return [*lines, LAST_LINE]


def suites_in(folder: Path) -> list[str]:
    return sorted(p.stem for p in folder.glob("*.toml")) if folder.is_dir() else []


def run(repo_name: str, suite: str, split: str = "", dry_run: bool = False, as_json: bool = False,
        out: str = "") -> int:
    paths.ensure_dirs()
    cfg = config.load()
    repo = cfg.repo(repo_name)
    folder = repo.layer_dir / "retrieval"
    file = folder / f"{suite}.toml"
    if not NAME.fullmatch(suite) or not file.is_file():
        held = ", ".join(suites_in(folder)) or "none"
        raise Refused(f"no retrieval suite `{suite}` in {folder}; it holds: {held}")
    if split and split not in SPLITS:
        raise Refused(f"--split must be one of {', '.join(SPLITS)}")
    spec = load_suite(file, suite)
    queries = [q for q in spec.queries if not split or q.split == split]
    if not queries:
        raise Refused(f"the suite has no query in split {split}")
    corpus = build_corpus(repo, spec.corpus)
    if dry_run:
        print("\n".join(dry_lines(spec, queries, corpus, folder, cfg)))
        return 0
    run_id = record.start("eval", tag=f"retrieval-{repo.name}", repo=repo.name)
    with record.failed_on_crash(run_id):
        where = paths.RUNS_DIR / run_id
        run_dir = where / "retrieval"
        run_dir.mkdir(parents=True, exist_ok=True)
        data = Experiment(spec, queries, cfg, repo, corpus, folder, run_id, run_dir).execute()
        payload = json.dumps(data, indent=2) + "\n"
        (where / "retrieval.json").write_text(payload)
        if out:
            Path(paths.expand(out)).write_text(payload)
        record.settle(run_id, "done", summary=f"retrieval {suite}: {len(spec.arms)} arms, {len(queries)} queries")
    print(json.dumps(data) if as_json else "\n".join(report(data, str(where / "retrieval.json"))))
    return 0


SAMPLE_FILES = {
    "src/budget.py": '''"""Keep the context an agent receives inside a token budget."""


def trim_to_budget(blocks, limit):
    kept, used = [], 0
    for block in blocks:
        size = len(block) // 3 + 1
        if used + size > limit:
            continue
        kept.append(block)
        used += size
    return kept
''',
    "src/budget_report.py": '''"""Print how a budget was spent, so a budget can be trimmed later."""


def budget_report(spent, budget):
    left = budget - spent
    print(f"budget {budget}, spent {spent}, left {left}")
    if left < 0:
        print("over budget: trim the context by", -left)
    else:
        print("under budget: nothing to trim")
    return left
''',
    "src/ranker.py": '''"""Merge several ranked lists of passage ids into one list."""


def fuse_rankings(rankings, k=60):
    scores = {}
    for ranking in rankings:
        for place, name in enumerate(ranking, 1):
            scores[name] = scores.get(name, 0) + 1 / (k + place)
    return sorted(scores, key=lambda name: (-scores[name], name))
''',
    "docs/limits.md": """# Limits

The overnight dump writes at most 250 records.

A dump that would pass that number is cut short, and the
remainder is written by the next dump that runs.

Ask the platform team before the number is raised, since
the storage plan is sized around it.
""",
    "docs/exports.md": """# Exports

The nightly export copies each row of the orders table.
Every nightly export keeps a row for each order, and the
row cap guards the export when a row is too large.
A nightly export that stops early is run again by hand.
The export log shows each row and each cap that applied.
""",
    "docs/readme.md": """# Notes

This folder holds short notes for people who join the team.
Read them in order, ask questions early, and keep them
current when something changes. Each note is small on purpose
so that it can be read in a minute and changed in another.
""",
}
SAMPLE_QUERIES = {
    "budget-name": ("trim_to_budget", "src/budget.py", "trim_to_budget", "validation"),
    "nightly-cap": ("nightly export row cap", "docs/limits.md", "250", "holdout"),
    "fuse-name": ("fuse_rankings", "", "fuse_rankings", "train"),
}
SAMPLE_TARGETS = {"budget-name": "src/budget.py", "nightly-cap": "docs/limits.md", "fuse-name": "src/ranker.py"}
SAMPLE_ANSWERS = {
    "budget-name": "The function is trim_to_budget.",
    "fuse-name": "The function is fuse_rankings.",
}
SAMPLE_ARMS = {
    "exact": ("exact", "", ""), "hybrid": ("hybrid", "", ""), "hybrid-embed": ("hybrid", "embed", ""),
    "hybrid-rerank": ("hybrid", "embed", "rerank"), "hybrid-missing": ("hybrid", "", "rerank-missing"),
}
SAMPLE_ADAPTERS = {"embed": ("embedding", "embedding.json"), "rerank": ("rerank", "rerank.json"),
                   "rerank-missing": ("rerank", "missing.json")}


def sample_suite(commit: str, budget: int) -> str:
    quote = json.dumps
    lines = [f"budget_tokens = {budget}", "top_k = 20", "spend_usd = 1", "", "[corpus]",
             f"revision = {quote(commit)}", "", "[generate]", 'model = "sample-answerer"']
    for name, (kind, file) in SAMPLE_ADAPTERS.items():
        lines += ["", f"[adapters.{name}]", f"kind = {quote(kind)}", f"replay = {quote(file)}",
                  f"route = {quote(f'sample/{name}')}", f"model = {quote(f'sample-{name}-1')}", 'version = "2026-01"']
    for name, (search, embedding, rerank) in SAMPLE_ARMS.items():
        lines += ["", f"[arms.{name}]", f"search = {quote(search)}"]
        lines += [f"embedding = {quote(embedding)}"] if embedding else []
        lines += [f"rerank = {quote(rerank)}"] if rerank else []
    for name, (query, relevant, accept, split) in SAMPLE_QUERIES.items():
        lines += ["", f"[queries.{name}]", f"query = {quote(query)}"]
        lines += [f"relevant = [{quote(relevant)}]"] if relevant else []
        lines += [f"accept = [{quote(accept)}]", f"split = {quote(split)}"]
    return "\n".join(lines) + "\n"


def sample(where: Path) -> list[str]:
    where = Path(os.path.abspath(where))
    if where.exists() and any(where.iterdir()):
        raise FileExistsError(f"{where} is not empty")
    repo_dir, layer, answers = where / "repo", where / "layer", where / "replay"
    suites = layer / "retrieval"
    for made in (repo_dir, suites, answers):
        made.mkdir(parents=True)
    util.git(["init", "-q", "-b", "main"], repo_dir)
    for name, body in SAMPLE_FILES.items():
        (repo_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (repo_dir / name).write_text(body)
    util.git(["add", "."], repo_dir)
    util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-q", "-m", "first commit"], repo_dir)
    commit = util.git(["rev-parse", "HEAD"], repo_dir).strip()
    corpus = build_corpus(config.Repo(SAMPLE_REPO, repo_dir), CorpusSpec(revision=commit))
    sizes = sorted(routing.request_tokens(p.block, "", None) for p in corpus.passages)
    if sizes[0] + sizes[1] <= sizes[-1]:
        raise RuntimeError("the sample files differ too much in size for a budget that holds one passage")
    first = {p.path: p.id for p in reversed(corpus.passages)}
    dims = sorted(first)
    one_hot = {p.id: [1 if p.path == path else 0 for path in dims] for p in corpus.passages}
    vectors = {**one_hot, **{f"query:{n}": one_hot[first[file]] for n, file in SAMPLE_TARGETS.items()}}
    (suites / "sample.toml").write_text(sample_suite(commit, sizes[-1]))
    (suites / "embedding.json").write_text(json.dumps(
        {"corpus": corpus.fingerprint, "vectors": vectors, "cost_usd": 0.0001, "tokens": 180, "seconds": 0.05},
        indent=2) + "\n")
    scores = {n: {first[file]: 0.9} for n, file in SAMPLE_TARGETS.items()}
    scores["nightly-cap"][first["docs/exports.md"]] = 0.2
    (suites / "rerank.json").write_text(json.dumps(
        {"queries": {n: {"scores": s, "cost_usd": 0.0002, "seconds": 0.12} for n, s in scores.items()}},
        indent=2) + "\n")
    for arm in ("exact", "hybrid", "hybrid-embed", "hybrid-rerank"):
        for name in SAMPLE_QUERIES:
            said = ("At most 250 records." if arm == "hybrid-rerank" else "The context does not say.") \
                if name == "nightly-cap" else SAMPLE_ANSWERS[name]
            answer = {"text": said, **replay.SAMPLE_USAGE, "cost_usd": 0.001, "tokens_in": 120, "tokens_out": 12}
            (answers / f"retrieval-{arm}-{name}.json").write_text(json.dumps(answer, indent=2) + "\n")
    (layer / "config.toml").write_text('[git]\nbase = "main"\n')
    return [f"# wrote {repo_dir}, {layer} and {answers}",
            f"# add these lines to {paths.short_path(paths.CONFIG_FILE)}, best in a scratch {replay.SCRATCH_CONFIG}:",
            "", "[replay]", "all = true", f"dir = {json.dumps(str(answers))}", "",
            "[[repo]]", f"name = {json.dumps(SAMPLE_REPO)}", f"path = {json.dumps(str(repo_dir))}",
            f"layer = {json.dumps(str(layer))}", 'tracker = "local"', "jev = false"]
