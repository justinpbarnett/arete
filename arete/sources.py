from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shlex

from . import config, cron, gh, jira, paths, state, util
from .loops import Loop, as_list, blank, github_remote

DEFAULT_EVERY = 300
GITHUB_WATCH = ("ci_red", "issue", "advisory")
LOG_PATTERN = r"ERROR|CRITICAL|FATAL|Exception|Traceback|panic:"
MAX_LOG_READ = 2_000_000
SIGNATURE_NOISE = [
    (re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?"), "<time>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.IGNORECASE), "<hex>"),
    (re.compile(r"(?<!\w)'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\d+"), "<n>"),
]


def source_id(loop: Loop, index: int) -> str:
    return f"{loop.name}#{index}"


def fingerprint(source: dict) -> str:
    settings = json.dumps({k: v for k, v in source.items() if k != "every"}, sort_keys=True, default=str)
    return hashlib.sha256(settings.encode()).hexdigest()


def every(source: dict) -> float:
    return cron.seconds(source["every"]) if source.get("every") else DEFAULT_EVERY


def event(type_: str, source: str, key, title: str, *, repo: str = "", url: str = "", trust: str = "untrusted",
          data: dict | None = None) -> dict:
    return {"type": type_, "source": source, "key": str(key), "repo": repo, "title": util.first_line(title, 300),
            "url": url, "trust": trust, "data": data or {}}


def metric_event(loop: Loop, value: float) -> dict:
    name, op, threshold = loop.trigger.metric
    return event("metric", f"{loop.name}#metric", f"{name}@{util.iso()}", f"{name} = {value:g}, {op} {threshold:g}",
                 trust="internal", data={"metric": name, "value": value, "rule": f"{op} {threshold:g}"})


def fire(loop: Loop, how: str, events: list[dict] | None = None) -> list[int]:
    if events is None:
        return [item] if (item := state.enqueue(loop.name, loop.queue_name, "", how)) else []
    batches: dict[str, list[dict]] = {}
    for e in events:
        if not loop.trigger.refusal(e):
            state.add_event(loop.name, e)
            batches.setdefault(loop.trigger.group_of(e), []).append(e)
    return [state.enqueue(loop.name, loop.queue_name, group, how, batch) for group, batch in batches.items()]


def github_name(cfg: config.Config, name: str) -> str:
    if (path := paths.expand(name)).is_absolute():
        return next((r.github for r in cfg.repos if r.path.resolve() == path.resolve()), "") or github_remote(path)
    if "/" in name:
        return name
    return cfg.repo(name).github


def poll(loop: Loop, index: int, cfg: config.Config) -> list[dict]:
    source = loop.sources[index]
    sid = source_id(loop, index)
    owner = fingerprint(source)
    known = state.cursor(sid)
    first = known is None or state.source_owner(sid) not in (None, owner)
    found, value = SOURCES[source["type"]].fetch(loop, source, cfg, sid, None if first else known["value"])
    seen = state.seen_keys(sid)
    fresh = [e for e in found if e["key"] not in seen]
    fresh = list({e["key"]: e for e in fresh}.values())
    state.mark_seen(sid, [e["key"] for e in fresh])
    state.set_cursor(sid, value, True, f"{len(found)} read, {len(fresh)} new{' (baseline)' if first else ''}")
    state.set_source_owner(sid, owner)
    return [] if first else fresh


def peek(loop: Loop, index: int, cfg: config.Config) -> list[dict]:
    source = loop.sources[index]
    found, _ = SOURCES[source["type"]].fetch(loop, source, cfg, source_id(loop, index), "{}")
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
                        "--json", "number,title,author,url,labels,body,createdAt"]
                if source.get("label"):
                    args += ["--label", source["label"]]
                for i in gh.gh_json(args) or []:
                    author = (i.get("author") or {}).get("login", "")
                    out.append(event("github.issue", sid, f"{repo}#{i['number']}", i["title"], repo=repo, url=i["url"],
                                     trust="owner" if author == me else "untrusted",
                                     data={"number": i["number"], "author": author,
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
                                                        .get("first_patched_version") or {}).get("identifier")}))
        return out, None


class Jira(Source):
    needs = "jql"

    def fetch(self, loop: Loop, source: dict, cfg: config.Config, sid: str, cursor) -> tuple[list[dict], object]:
        client = jira.Client.from_env(cfg.jira.env_file)
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

    def fetch(self, loop: Loop, source: dict, cfg: config.Config, sid: str, cursor) -> tuple[list[dict], object]:
        argv = shlex.split(source["command"]) if isinstance(source["command"], str) else list(source["command"])
        res = util.sh(argv, cwd=loop.dir, timeout=float(source.get("timeout", 120)))
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
        files = sorted(glob.glob(str(paths.expand(source["path"]))))
        out: dict[str, dict] = {}
        for name in files:
            try:
                info = os.stat(name)
            except OSError:
                continue
            size, inode = info.st_size, info.st_ino
            mark = offsets.get(name)
            start, known_inode = mark if isinstance(mark, list) else (mark, inode)
            if cursor is None or start is None:
                offsets[name] = [size, inode]
                if cursor is None:
                    continue
                start = 0
            if size < start or known_inode != inode:
                start = 0
            try:
                with open(name, "rb") as f:
                    f.seek(max(start, size - MAX_LOG_READ))
                    chunk = f.read(size - f.tell())
            except OSError:
                continue
            last_newline = chunk.rfind(b"\n")
            if last_newline == -1:
                offsets[name] = [start, inode]
                continue
            offsets[name] = [size - len(chunk) + last_newline + 1, inode]
            for raw in chunk[:last_newline].decode("utf-8", "replace").splitlines():
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


def signature(line: str) -> str:
    text = line.strip()
    for pattern, mark in SIGNATURE_NOISE:
        text = pattern.sub(mark, text)
    return text[:300]


SOURCES: dict[str, Source] = {"github": GitHub(), "jira": Jira(), "log": Log(), "command": Command()}
