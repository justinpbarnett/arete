from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shlex
from datetime import UTC, datetime, timedelta

from . import accounts, config, cron, gh, paths, util
from .loops import Loop, as_list, blank, github_remote
from .store import db as foundation
from .store import loops as loop_store
from .store import sources as source_store

DEFAULT_EVERY = 300
GITHUB_WATCH = ("ci_red", "issue", "advisory")
LOG_PATTERN = r"ERROR|CRITICAL|FATAL|Exception|Traceback|panic:"
MAX_LOG_READ = 2_000_000
CATCH_UP_SLACK = timedelta(minutes=2)
SHOWN_KEYS = 5
SIGNATURE_NOISE = [
    (re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?"), "<time>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.IGNORECASE), "<hex>"),
    (re.compile(r"(?<!\w)'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\d+"), "<n>"),
]


def slot_id(loop: Loop, index: int) -> str:
    return f"{loop.name}#{index}"


def source_id(loop: Loop, index: int) -> str:
    name = loop.sources[index].get("name")
    return f"{loop.name}#{name}" if name else slot_id(loop, index)


def fingerprint(source: dict) -> str:
    scope = {"type": source.get("type")} | {k: source.get(k) for k in SOURCES[source["type"]].scope}
    return "scope:" + hashlib.sha256(json.dumps(scope, sort_keys=True, default=str).encode()).hexdigest()


def legacy_fingerprint(source: dict) -> str:
    settings = json.dumps({k: v for k, v in source.items() if k not in ("every", "name")}, sort_keys=True,
                          default=str)
    return hashlib.sha256(settings.encode()).hexdigest()


def same_source(owner: str | None, source: dict) -> bool:
    return owner is None or owner in (fingerprint(source), legacy_fingerprint(source))


def scope_changed(owner: str | None, source: dict) -> bool:
    return owner is not None and owner.startswith("scope:") and owner != fingerprint(source)


def opaque(owner: str | None) -> bool:
    return owner is not None and not owner.startswith("scope:")


def arrived(e: dict) -> datetime | None:
    data = e.get("data")
    stamp = data.get("at") or data.get("updated") if isinstance(data, dict) else None
    try:
        moment = util.parse_iso(str(stamp))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def every(source: dict) -> float:
    return cron.seconds(source["every"]) if source.get("every") else DEFAULT_EVERY


def event(type_: str, source: str, key, title: str, *, repo: str = "", url: str = "", trust: str = "untrusted",
          data: dict | None = None) -> dict:
    return {"type": type_, "source": source, "key": str(key), "repo": repo, "title": util.first_line(title, 300),
            "url": url, "trust": trust, "data": data or {}}


def fire(loop: Loop, how: str, events: list[dict] | None = None) -> list[int]:
    with foundation.tx():
        if events is None:
            return [item] if (item := loop_store.enqueue(loop.name, loop.queue_name, "", how)) else []
        batches: dict[str, list[dict]] = {}
        for e in events:
            if not loop.trigger.refusal(e):
                loop_store.add_event(loop.name, e)
                batches.setdefault(loop.trigger.group_of(e), []).append(e)
        return [loop_store.enqueue(loop.name, loop.queue_name, group, how, batch)
                for group, batch in batches.items()]


def github_name(cfg: config.Config, name: str) -> str:
    if (path := paths.expand(name)).is_absolute():
        return next((r.github for r in cfg.repos if r.path.resolve() == path.resolve()), "") or github_remote(path)
    if "/" in name:
        return name
    return cfg.repo(name).github


def is_slot(loop: Loop, sid: str) -> bool:
    return re.fullmatch(re.escape(loop.name) + r"#\d+", sid) is not None


def slot_cursors(loop: Loop) -> list[str]:
    return [c["source"] for c in source_store.cursors() if is_slot(loop, c["source"])]


def adopt_history(loop: Loop, index: int, source: dict, sid: str) -> None:
    if not source.get("name") or source_store.cursor(sid):
        return
    own = slot_id(loop, index)
    owner = source_store.source_owner(own)
    if source_store.cursor(own) and (same_source(owner, source) or opaque(owner) and len(loop.sources) == 1):
        source_store.adopt_source(own, sid)
        return
    for old in slot_cursors(loop):
        if (owner := source_store.source_owner(old)) is not None and same_source(owner, source):
            source_store.adopt_source(old, sid)
            return


def last_good_poll(known: dict) -> datetime | None:
    stamp = known.get("last_ok") or (known.get("polled") if known.get("ok") else None)
    if not stamp:
        return None
    moment = util.parse_iso(stamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def shown(keys: list[str]) -> str:
    return ", ".join(keys[:SHOWN_KEYS]) + (", ..." if len(keys) > SHOWN_KEYS else "")


def poll(loop: Loop, index: int, cfg: config.Config, say=lambda _: None) -> tuple[list[dict], list[int]]:
    source = loop.sources[index]
    sid = source_id(loop, index)
    kind = SOURCES[source["type"]]
    adopt_history(loop, index, source, sid)
    started = util.iso()
    owner = fingerprint(source)
    known = source_store.cursor(sid)
    first = known is None
    held = source_store.source_owner(sid)
    changed = not first and scope_changed(held, source)
    unsure = not first and not changed and not same_source(held, source)
    cursor = None if first else kind.rescope(source, known["value"]) if changed or unsure else known["value"]
    found, value = kind.fetch(loop, source, cfg, sid, cursor)
    unique = {e["key"]: e for e in found}
    with foundation.tx():
        claimed = set(source_store.claim_seen(sid, list(unique)))
        fresh = [e for k, e in unique.items() if k in claimed]
        if first:
            forwarded, older = [], fresh
        elif changed or unsure:
            forwarded, older = kind.catch_up(fresh, last_good_poll(known), untimed=unsure)
        else:
            forwarded, older = fresh, []
        told = changed or unsure and bool(older)
        items = fire(loop, "event", forwarded)
        detail = f"{len(found)} read, {len(fresh)} new"
        if first:
            detail += " (baseline)"
        elif told:
            detail += f", settings changed, {len(older)} older taken as seen"
        source_store.set_cursor(sid, value, True, detail, started=started)
        source_store.set_source_owner(sid, owner)
    if first:
        say(f"{sid}: first read, {len(fresh)} existing events taken as the baseline"
            + (f": {shown([e['key'] for e in fresh])}" if fresh else ""))
    elif told:
        say(f"{sid}: source settings changed; {len(forwarded)} events since the last good poll go on, "
            f"{len(older)} older taken as seen" + (f": {shown([e['key'] for e in older])}" if older else ""))
    return ([], []) if first else (forwarded, items)


def peek(loop: Loop, index: int, cfg: config.Config) -> list[dict]:
    source = loop.sources[index]
    kind = SOURCES[source["type"]]
    if isinstance(kind, Command):
        found, _ = kind.fetch(loop, source, cfg, source_id(loop, index), "{}", sample=True)
    else:
        found, _ = kind.fetch(loop, source, cfg, source_id(loop, index), "{}")
    return found


def repo_problems(cfg: config.Config, names: list, what: str) -> list[str]:
    problems = []
    for name in names:
        try:
            if not github_name(cfg, str(name)):
                problems.append(f"{what} repo '{name}' has no github in the config or GitHub remote")
        except config.UnknownRepo:
            problems.append(f"{what} repo '{name}' is not a repo in the config or owner/name")
    return problems


class Source:
    needs = ""
    scope: tuple[str, ...] = ()

    def rescope(self, source: dict, cursor):
        return cursor

    def catch_up(self, fresh: list[dict], since: datetime | None, untimed: bool = False) -> tuple[list[dict], list[dict]]:
        cutoff = since - CATCH_UP_SLACK if since else None
        forwarded = [e for e in fresh if ((at := arrived(e)) is None and untimed) or (cutoff and at and at >= cutoff)]
        return forwarded, [e for e in fresh if e not in forwarded]

    def problems(self, source: dict, loop: Loop, cfg: config.Config) -> list[str]:
        problems = [f"a {source['type']} source needs {self.needs}"] if self.needs and blank(source.get(self.needs)) \
            else []
        try:
            every(source)
        except cron.BadSchedule as e:
            problems.append(str(e))
        return problems

    def fetch(self, loop: Loop, source: dict, cfg: config.Config, sid: str, cursor) -> tuple[list[dict], object]:
        raise NotImplementedError


class GitHub(Source):
    scope = ("repos", "watch", "label", "branch")

    def repos(self, source: dict, loop: Loop) -> list[str]:
        return as_list(source.get("repos")) or ([loop.repo] if loop.repo else [])

    def watches(self, source: dict) -> list[str]:
        return as_list(source.get("watch")) or ["ci_red"]

    def problems(self, source: dict, loop: Loop, cfg: config.Config) -> list[str]:
        problems = super().problems(source, loop, cfg)
        problems += [f"github watch '{w}' is not one of {', '.join(GITHUB_WATCH)}" for w in self.watches(source)
                     if w not in GITHUB_WATCH]
        names = self.repos(source, loop)
        if not names:
            problems.append("a github source needs repos or [run] repo")
        return problems + repo_problems(cfg, names, "github source")

    def fetch(self, loop: Loop, source: dict, cfg: config.Config, sid: str, cursor) -> tuple[list[dict], object]:
        watch = self.watches(source)
        me = gh.me() if "issue" in watch else ""
        out: list[dict] = []
        for name in self.repos(source, loop):
            repo = github_name(cfg, name)
            if "ci_red" in watch:
                branch = source.get("branch") or gh.default_branch(repo)
                runs = gh.gh_json(["run", "list", "-R", repo, "--branch", branch, "--status", "failure",
                                   "--limit", "10",
                                   "--json", "databaseId,displayTitle,workflowName,headSha,url,createdAt,event"]) or []
                out += [event("github.ci_red", sid, r["databaseId"], f"CI red on {repo}@{branch}: {r['workflowName']}",
                              repo=repo, url=r["url"], trust="internal",
                              data={"workflow": r["workflowName"], "sha": r["headSha"], "branch": branch,
                                    "commit_title": r["displayTitle"], "at": r["createdAt"], "run_id": r["databaseId"]})
                        for r in runs]
            if "issue" in watch:
                args = ["issue", "list", "-R", repo, "--state", "open", "--limit", "30",
                        "--json", "number,title,author,url,labels,body,createdAt,updatedAt"]
                if source.get("label"):
                    args += ["--label", source["label"]]
                for i in gh.gh_json(args) or []:
                    author = (i.get("author") or {}).get("login", "")
                    out.append(event("github.issue", sid, f"{repo}#{i['number']}", i["title"], repo=repo, url=i["url"],
                                     trust="owner" if author == me else "untrusted",
                                     data={"number": i["number"], "author": author, "updated": i.get("updatedAt"),
                                           "body": (i.get("body") or "")[:6000],
                                           "labels": [label["name"] for label in i.get("labels", [])]}))
            if "advisory" in watch:
                for a in gh.open_dependabot_alerts(repo):
                    adv, dep = a.get("security_advisory") or {}, a.get("dependency") or {}
                    pkg = (dep.get("package") or {}).get("name", "")
                    out.append(event("github.advisory", sid, f"advisory:{repo}#{a['number']}",
                                     f"{pkg}: {adv.get('summary', '')}", repo=repo, url=a.get("html_url", ""),
                                     trust="untrusted",
                                     data={"package": pkg, "severity": adv.get("severity"), "ghsa": adv.get("ghsa_id"),
                                           "manifest": dep.get("manifest_path"),
                                           "fixed_in": ((a.get("security_vulnerability") or {})
                                                        .get("first_patched_version") or {}).get("identifier"),
                                           "at": a.get("created_at")}))
        return out, None


class Jira(Source):
    needs = "jql"
    scope = ("jql", "on_update")

    def fetch(self, loop: Loop, source: dict, cfg: config.Config, sid: str, cursor) -> tuple[list[dict], object]:
        client = accounts.jira_client(cfg.jira.env_file)
        fields = ["summary", "status", "reporter", "updated", "description", "labels"]
        out = []
        for issue in client.search(source["jql"], fields, limit=int(source.get("limit", 50))):
            f = issue.get("fields") or {}
            key = issue["key"] + (f"@{f.get('updated')}" if source.get("on_update") else "")
            out.append(event("jira.issue", sid, key, f"{issue['key']}: {f.get('summary', '')}",
                             repo=source.get("repo", loop.repo), url=client.browse_url(issue["key"]),
                             trust="untrusted",
                             data={"key": issue["key"], "status": (f.get("status") or {}).get("name"),
                                   "reporter": (f.get("reporter") or {}).get("displayName"),
                                   "updated": f.get("updated"), "labels": f.get("labels"),
                                   "description": json.dumps(f.get("description"))[:6000]}))
        return out, None


class Command(Source):
    needs = "command"
    scope = ("command",)

    def fetch(self, loop: Loop, source: dict, cfg: config.Config, sid: str, cursor,
              sample: bool = False) -> tuple[list[dict], object]:
        argv = shlex.split(source["command"]) if isinstance(source["command"], str) else list(source["command"])
        res = util.sh(argv, cwd=loop.dir, timeout=float(source.get("timeout", 120)),
                      env={"ARETE_LOOP": loop.name, "ARETE_LOOP_DIR": str(loop.dir), "ARETE_SOURCE": sid,
                           "ARETE_SOURCE_SAMPLE": "1" if sample else ""})
        if res.returncode != 0:
            raise RuntimeError(f"exit {res.returncode}: {util.first_line(res.stderr or res.stdout, 300)}")
        out = []
        for line in res.stdout.splitlines():
            if not line.strip().startswith("{"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = row.get("key") or row.get("title") or hashlib.sha1(line.strip().encode()).hexdigest()[:16]
            out.append(event(row.get("type", "command"), sid, key,
                             str(row.get("title") or ""), repo=row.get("repo", loop.repo), url=row.get("url", ""),
                             trust=source.get("trust", "internal"), data=row.get("data") or {}))
        return out, None


class Log(Source):
    needs = "path"
    scope = ("path", "pattern")

    def rescope(self, source: dict, cursor):
        if not cursor:
            return cursor
        offsets = json.loads(cursor)
        for name in log_files(source):
            if name not in offsets:
                try:
                    info = os.stat(name)
                except OSError:
                    continue
                offsets[name] = [info.st_size, info.st_ino]
        return json.dumps(offsets)

    def catch_up(self, fresh: list[dict], since: datetime | None, untimed: bool = False) -> tuple[list[dict], list[dict]]:
        return fresh, []

    def problems(self, source: dict, loop: Loop, cfg: config.Config) -> list[str]:
        problems = super().problems(source, loop, cfg)
        try:
            re.compile(source.get("pattern") or LOG_PATTERN)
        except re.error as e:
            problems.append(f"log source pattern '{source['pattern']}' is not a regex: {e}")
        return problems

    def fetch(self, loop: Loop, source: dict, cfg: config.Config, sid: str, cursor) -> tuple[list[dict], object]:
        offsets: dict[str, list[int] | int] = json.loads(cursor) if cursor else {}
        pattern = re.compile(source.get("pattern") or LOG_PATTERN)
        files = log_files(source)
        out: dict[str, dict] = {}
        for name in files:
            try:
                info = os.stat(name)
            except OSError:
                continue
            size, inode = info.st_size, info.st_ino
            start = log_start(offsets.get(name), size, inode)
            if cursor is None or start is None:
                offsets[name] = [size, inode]
                if cursor is None:
                    continue
                start = 0
            if (read := read_lines(name, max(start, size - MAX_LOG_READ), size)) is None:
                continue
            found, end = read
            offsets[name] = [start if end is None else end, inode]
            for raw in found:
                if not pattern.search(raw):
                    continue
                sig = signature(raw)
                key = hashlib.sha1(sig.encode()).hexdigest()[:16]
                if key in out:
                    out[key]["data"]["count"] += 1
                    continue
                out[key] = event("log.error", sid, key, sig, trust="untrusted",
                                 data={"file": name, "sample": raw.strip()[:1500], "signature": sig, "count": 1})
        return list(out.values()), json.dumps({k: v for k, v in offsets.items() if k in files})


def log_files(source: dict) -> list[str]:
    return sorted(glob.glob(str(paths.expand(source["path"]))))


def log_start(mark, size: int, inode: int) -> int | None:
    start, known = mark if isinstance(mark, list) else (mark, inode)
    if start is None:
        return None
    return 0 if size < start or known != inode else start


def read_lines(name: str, start: int, end: int) -> tuple[list[str], int | None] | None:
    try:
        with open(name, "rb") as f:
            f.seek(start)
            chunk = f.read(max(0, end - start))
    except OSError:
        return None
    last_newline = chunk.rfind(b"\n")
    if last_newline == -1:
        return [], None
    return chunk[:last_newline].decode("utf-8", "replace").splitlines(), start + last_newline + 1


def signature(line: str) -> str:
    text = line.strip()
    for pattern, mark in SIGNATURE_NOISE:
        text = pattern.sub(mark, text)
    return text[:300]


SOURCES: dict[str, Source] = {"github": GitHub(), "jira": Jira(), "log": Log(), "command": Command()}
