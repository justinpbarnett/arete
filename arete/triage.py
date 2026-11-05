from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import alerts, claude, config, gh, jira, paths, state, tracker, util
from .heartbeat import mark_loop
from .pipeline import READ_TOOLS, prompt

ISSUE_TITLE = "[arete] triage"
ISSUE_LABEL = "arete-triage"
SKIP_LABELS = {tracker.LABEL, alerts.LABEL, ISSUE_LABEL}
MAX_CALLS = 20
PER_SOURCE = 50
BUDGET_USD = 0.30
TIMEOUT_SECONDS = 300
MAX_PLAN_STEPS = 6
MAX_ISSUE_CHARS = 60000
META_FILE = paths.STATE_DIR / "triage.json"
PROMPTS_DIR = paths.STATE_DIR / "triage"
SEARCH_FIELDS = ["summary", "updated", "labels"]
JIRA_FIELDS = ["summary", "description", "status", "labels", "issuetype", "parent"]
SIZES = ("S", "M", "L")
CODE_CHANGE, NOT_CODE = "a code change", "something else"
SCHEMA = {
    "type": "object",
    "properties": {
        "asks_for": {"type": "string", "enum": [CODE_CHANGE, NOT_CODE]},
        "ready": {"type": "boolean"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "size": {"type": "string", "enum": list(SIZES)},
        "repo": {"type": "string"},
        "plan": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_PLAN_STEPS},
        "why": {"type": "string"},
    },
    "required": ["asks_for", "ready", "questions", "size", "repo", "plan", "why"],
}
WAITING = f"waiting: more than {MAX_CALLS} tickets were due in one refresh"
EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


@dataclass
class Source:
    tracker: str
    key: str
    repos: list[config.Repo]

    @property
    def name(self) -> str:
        return f"Jira {self.key or '(no project)'}" if self.tracker == "jira" else f"GitHub {self.key}"

    def jira(self, cfg: config.Config) -> tracker.JiraTracker:
        return tracker.JiraTracker(self.repos[0], cfg.jira)

    def github(self) -> tracker.GitHubTracker:
        return tracker.GitHubTracker(self.repos[0])


@dataclass
class Candidate:
    ref: str
    id: str
    title: str
    url: str
    updated: str


def sources(cfg: config.Config) -> list[Source]:
    found: dict[tuple[str, str], Source] = {}
    for r in cfg.repos:
        if not (r.pipeline and r.parts):
            continue
        key = ("jira", r.jira_project or cfg.jira.project) if r.tracker == "jira" else ("github", r.github)
        found.setdefault(key, Source(*key, [])).repos.append(r)
    return list(found.values())


def moment(ts: str) -> datetime:
    try:
        when = jira.parse_time(ts)
    except ValueError:
        return EARLIEST
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def natural(ref: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", ref)]


def read_candidates(cfg: config.Config, src: Source) -> list[Candidate]:
    found = jira_candidates(cfg, src) if src.tracker == "jira" else github_candidates(src)
    return sorted(found, key=lambda c: moment(c.updated), reverse=True)


def jira_candidates(cfg: config.Config, src: Source) -> list[Candidate]:
    if not src.key:
        raise jira.JiraError("no Jira project; set [jira] project or jira_project on the repo")
    client = src.jira(cfg).client
    jql = (f'project = "{src.key}" AND assignee = currentUser() AND statusCategory != Done '
           f'AND (labels is EMPTY OR labels not in ("{tracker.LABEL}")) ORDER BY updated DESC')
    out = []
    for i in client.search(jql, SEARCH_FIELDS, limit=PER_SOURCE):
        f = i.get("fields") or {}
        if tracker.LABEL not in (f.get("labels") or []):
            out.append(Candidate(i["key"], i["key"], (f.get("summary") or "").strip(), client.browse_url(i["key"]),
                                 f.get("updated") or ""))
    return out


def github_candidates(src: Source) -> list[Candidate]:
    if not src.key:
        raise gh.GhError(f"{src.repos[0].name} has no github repo set")
    tk = src.github()
    out = []
    for i in gh.issues(src.key, author=tk.owner, limit=PER_SOURCE):
        names = {lb["name"] for lb in i.get("labels", [])}
        if names & SKIP_LABELS or not tk.by_owner(i):
            continue
        out.append(Candidate(f"{tk.repo.name}#{i['number']}", str(i["number"]), i["title"].strip(), i.get("url", ""),
                             i.get("updatedAt") or ""))
    return out


def ticket_text(cfg: config.Config, src: Source, c: Candidate) -> str:
    if src.tracker == "github":
        t = src.github().get(c.id)
        return "\n\n".join(p for p in (f"Labels: {', '.join(t.labels) or 'none'}", t.body, t.comments) if p)
    client = src.jira(cfg).client
    f = client.issue(c.id, JIRA_FIELDS).get("fields") or {}
    facts = [f"Type: {(f.get('issuetype') or {}).get('name', '')}",
             f"Status: {(f.get('status') or {}).get('name', '')}",
             f"Labels: {', '.join(f.get('labels') or []) or 'none'}"]
    parent = f.get("parent") or {}
    if parent.get("key"):
        facts.append(f"Part of {parent['key']}: {(parent.get('fields') or {}).get('summary', '')}")
    comments = [((cm.get("author") or {}).get("displayName", "someone"), cm.get("created", ""), cm.get("body") or "")
                for cm in client.comments(c.id)]
    parts = ["\n".join(facts), f.get("description") or "(no description)", tracker.comments_text(comments)]
    return "\n\n".join(p for p in parts if p)


def repos_text(src: Source) -> str:
    lines = []
    for r in src.repos:
        route = f", route label `{tracker.route_label(r)}`" if src.tracker == "jira" else ""
        limit = config.load_layer(r.layer_dir).max_diff_lines
        lines.append(f"- `{r.name}` at {r.path}{route}; one run changes at most {limit} lines")
    return "\n".join(lines)


def repo_rule(src: Source) -> str:
    if src.tracker == "github":
        return f"always `{src.repos[0].name}`."
    names = ", ".join(f"`{r.name}`" for r in src.repos)
    return (f"the one repository the change belongs in, one of {names}, or an empty string when the ticket and the "
            "code do not settle it. A ticket that already carries a route label belongs to that label's repository.")


def prompt_file(ref: str) -> Path:
    return PROMPTS_DIR / f"{tracker.safe_filename(ref)}.md"


def read_rules(dirs: list[Path]) -> list[str]:
    return [f"{tool}(/{d}/**)" for d in dict.fromkeys(p for d in dirs for p in (d, d.resolve())) for tool in READ_TOOLS]


def triage_one(cfg: config.Config, src: Source, c: Candidate) -> tuple[dict | None, float, str]:
    text = prompt("triage", repos=repos_text(src), ref=c.ref, title=c.title, ticket=ticket_text(cfg, src, c),
                  repo_rule=repo_rule(src))
    dirs = list(dict.fromkeys(r.path for r in src.repos if r.path.is_dir()))
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    res = claude.run(text, dirs[0] if dirs else PROMPTS_DIR, model=cfg.model("triage") or "haiku",
                     allowed_tools=read_rules(dirs), json_schema=SCHEMA, max_budget_usd=BUDGET_USD,
                     timeout=TIMEOUT_SECONDS, persist=False, add_dirs=dirs[1:])
    answer = res.structured if res.ok and isinstance(res.structured, dict) else None
    prompt_file(c.ref).write_text(f"{text}\n\n## Triage result (${res.cost_usd:.3f})\n\n```json\n"
                                  f"{json.dumps(answer or {'error': res.error}, indent=2)}\n```\n")
    return answer, res.cost_usd, "" if answer else util.first_line(res.error or "no answer")


def settle(src: Source, c: Candidate, answer: dict, cost: float) -> dict:
    by_name = {r.name: r for r in src.repos}
    repo = src.repos[0].name if src.tracker == "github" else str(answer.get("repo") or "").strip().strip("`")
    repo = repo if repo in by_name else ""
    why = str(answer.get("why") or "").strip()
    questions = [q.strip() for q in answer.get("questions") or [] if q.strip()]
    plan = [re.sub(r"^\s*(\d+[.)]|[-*])\s*", "", step).strip() for step in answer.get("plan") or []]
    plan = [p for p in plan if p][:MAX_PLAN_STEPS]
    if answer.get("asks_for") == NOT_CODE and not (answer.get("ready") or questions or plan):
        return row(src, c, updated=c.updated, ready=0, code=0, repo=repo, why=why, cost=cost)
    size = answer.get("size") if answer.get("size") in SIZES else "M"
    if not repo:
        questions.append(f"Which repo is it for: {', '.join(by_name)}?")
    if size == "L":
        questions.append("It is too big for one pull request. Can you split it into smaller tickets?")
    route = tracker.route_label(by_name[repo]) if src.tracker == "jira" and repo else ""
    return row(src, c, updated=c.updated, ready=int(bool(answer.get("ready")) and not questions), code=1, repo=repo,
               route=route, size=size, questions=questions, plan=plan, why=why, cost=cost)


def row(src: Source, c: Candidate, **fields) -> dict:
    base = {"ref": c.ref, "source": src.name, "tracker": src.tracker, "repo": "", "route": "", "title": c.title,
            "url": c.url, "updated": "", "ready": None, "code": None, "size": "", "questions": [], "plan": [], "why": "",
            "cost": 0.0, "ts": util.iso()}
    return {**base, **fields}


def kept(src: Source, c: Candidate, prev: dict | None, reason: str) -> dict:
    if prev and prev["ready"] is not None:
        return {**prev, "source": src.name, "title": c.title, "url": c.url}
    return row(src, c, why=reason)


def due(c: Candidate, prev: dict | None) -> bool:
    return not prev or prev["ready"] is None or prev["updated"] != c.updated


def rows_now() -> list[dict]:
    return sorted(state.triage_rows(), key=lambda r: natural(r["ref"]))


def run(dry_run: bool = False) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    with util.lock("triage") as got:
        if not got:
            print(f"{util.iso()} triage already running")
            return 0
        if not util.online():
            print(f"{util.iso()} offline, waiting up to {util.WAKE_NETWORK_WAIT_MINUTES} minutes for the network", flush=True)
            if not util.wait_for_network(util.WAKE_NETWORK_WAIT_MINUTES):
                print(f"{util.iso()} still offline, skipped this triage")
                if not dry_run:
                    mark_loop("triage", True, "offline")
                return 0
        return refresh(cfg, dry_run)


def refresh(cfg: config.Config, dry_run: bool) -> int:
    old = {r["ref"]: r for r in state.triage_rows()}
    srcs = sources(cfg)
    read: dict[str, list[Candidate]] = {}
    errors: list[str] = []
    for src in srcs:
        try:
            read[src.name] = read_candidates(cfg, src)
        except tracker.ERRORS as e:
            errors.append(f"{src.name}: {e}")
            print(f"{util.iso()} {errors[-1]}")
    todo = sorted(((src, c) for src in srcs for c in read.get(src.name, []) if due(c, old.get(c.ref))),
                  key=lambda sc: moment(sc[1].updated), reverse=True)
    now, later = todo[:MAX_CALLS], todo[MAX_CALLS:]
    if dry_run:
        preview(srcs, read, old, now, later)
        return 1 if errors else 0

    answered: dict[str, dict] = {}
    failed: dict[str, str] = {}
    spent = 0.0
    for src, c in now:
        print(f"{util.iso()} {c.ref}: triaging", flush=True)
        try:
            answer, cost, error = triage_one(cfg, src, c)
        except tracker.ERRORS as e:
            answer, cost, error = None, 0.0, f"could not read it: {e}"
        spent += cost
        if answer:
            answered[c.ref] = settle(src, c, answer, cost)
            print(f"{util.iso()} {c.ref}: {verdict(answered[c.ref])} (${cost:.3f})")
        else:
            failed[c.ref] = error
            print(f"{util.iso()} {c.ref}: triage failed: {error}")
    waiting = [c.ref for _, c in later]
    for src in srcs:
        if src.name in read:
            state.triage_replace(src.name, [
                answered.get(c.ref) or kept(src, c, old.get(c.ref),
                                            f"triage failed: {failed[c.ref]}" if c.ref in failed else WAITING)
                for c in read[src.name]])
    state.triage_prune([s.name for s in srcs])
    keep = {prompt_file(r["ref"]).name for r in state.triage_rows()}
    for f in PROMPTS_DIR.glob("*.md"):
        if f.name not in keep:
            f.unlink()

    meta = util.read_json(META_FILE, {})
    meta.update(refreshed=util.iso(), errors=errors, waiting=waiting, failed=failed)
    problems = list(errors)
    try:
        posted = publish(cfg, rows_now(), meta)
    except (gh.GhError, BoardRefused) as e:
        problems.append(f"posting the board: {e}")
        posted = f"not posted: {e}"
    meta.update(errors=errors, posted=posted)
    util.write_json(META_FILE, meta)
    print(f"{util.iso()} triage: {util.count(len(now), 'model call')} (${spent:.3f}), "
          f"{len(failed)} failed, {len(waiting)} waiting; board {posted}")
    if waiting:
        print(f"waiting for the next refresh: {', '.join(waiting)}")
    mark_loop("triage", not problems, "; ".join(problems))
    return 1 if problems else 0


def preview(srcs: list[Source], read: dict[str, list[Candidate]], old: dict[str, dict],
            now: list[tuple[Source, Candidate]], later: list[tuple[Source, Candidate]]) -> None:
    for src in srcs:
        if src.name not in read:
            continue
        print(f"{src.name}: {util.count(len(read[src.name]), 'candidate')}")
        for c in read[src.name]:
            prev = old.get(c.ref)
            seen = ("new" if not prev else "waiting" if prev["why"] == WAITING else "retry" if prev["ready"] is None
                    else "changed" if due(c, prev) else "unchanged")
            print(f"  {c.ref:<14} {seen:<9} {c.updated[:19]:<19}  {c.title}"[:160])
    print(f"would triage {len(now)}: {', '.join(c.ref for _, c in now) or 'none'}")
    if later:
        print(f"would leave waiting for the next refresh: {', '.join(c.ref for _, c in later)}")
    print("dry run: no model calls, nothing written")


@dataclass
class Groups:
    ready: list[dict]
    with_questions: list[dict]
    not_code: list[dict]
    not_triaged: list[dict]


def groups(rows: list[dict]) -> Groups:
    return Groups([r for r in rows if r["ready"] == 1], [r for r in rows if r["ready"] == 0 and r["code"] != 0],
                  [r for r in rows if r["code"] == 0], [r for r in rows if r["ready"] is None])


def labels_to_add(r: dict) -> list[str]:
    return [tracker.LABEL, r["route"]] if r["route"] else [tracker.LABEL]


def verdict(r: dict) -> str:
    if r["ready"] is None:
        return "not triaged"
    if r["code"] == 0:
        return "not code work"
    if r["ready"]:
        return f"ready, {r['size']}, add {' '.join(labels_to_add(r))}"
    return f"not ready, {r['size']}, {util.count(len(r['questions']), 'question')}"


def summary(rows: list[dict]) -> str:
    g = groups(rows)
    return (f"{util.count(len(rows), 'ticket')}: {len(g.ready)} ready, {len(g.with_questions)} with questions, "
            f"{len(g.not_code)} not code work, {len(g.not_triaged)} not triaged")


def status_line() -> str:
    meta = util.read_json(META_FILE, {})
    if not meta.get("refreshed"):
        return "triage: never refreshed (`arete triage --refresh`)"
    errors = f"; FAILED {'; '.join(meta['errors'])}" if meta.get("errors") else ""
    return f"triage: {summary(rows_now())}; refreshed {meta['refreshed']}; board {meta.get('posted', '')}{errors}"[:300]


def board() -> str:
    meta = util.read_json(META_FILE, {})
    rows = rows_now()
    if not meta.get("refreshed") and not rows:
        return "no triage yet; run `arete triage --refresh`"
    lines = [f"Triage of {summary(rows)}. Refreshed {meta.get('refreshed', 'never')}."]
    lines += [f"Not read this time: {e}" for e in meta.get("errors", [])]
    if meta.get("waiting"):
        lines.append(f"Waiting for the next refresh: {', '.join(meta['waiting'])}")
    g = groups(rows)
    width = max(len(r["ref"]) for r in rows) if rows else 0
    repo_width = max((len(r["repo"]) for r in rows), default=0)
    if g.ready:
        lines += ["", "Ready: add the labels to approve"]
        lines += [f"  {r['ref']:<{width}}  {r['size']}  {r['repo']:<{repo_width}}  {' '.join(labels_to_add(r)):<16}  "
                  f"{r['title']}" for r in g.ready]
    if g.with_questions:
        lines += ["", "With questions"]
        for r in g.with_questions:
            lines.append(f"  {r['ref']:<{width}}  {r['size']}  {r['repo'] or '?':<{repo_width}}  {r['title']}")
            lines += [f"      - {q}" for q in r["questions"]] or [f"      {r['why']}"]
    if g.not_code:
        lines += ["", "Not code work: nothing for the pipeline to build"]
        for r in g.not_code:
            lines += [f"  {r['ref']:<{width}}  {r['title']}", f"      {r['why']}"]
    if g.not_triaged:
        lines += ["", "Not triaged"]
        lines += [f"  {r['ref']:<{width}}  {r['title']}  ({r['why']})" for r in g.not_triaged]
    lines += ["", "`arete triage REF` shows one ticket in full."]
    return "\n".join(lines)


def ticket(ref: str) -> str | None:
    r = next((r for r in state.triage_rows() if r["ref"].lower() == ref.strip().lower()), None)
    if not r:
        return None
    lines = [f"{r['ref']}: {r['title']}", r["url"], f"{r['source']}, updated {r['updated'] or 'unknown'}"]
    if r["ready"] is None:
        return "\n".join(lines + [f"Not triaged: {r['why']}"])
    lines[-1] += f", triaged {r['ts']} for ${r['cost'] or 0:.3f}"
    if r["code"] == 0:
        return "\n".join(lines + ["Not code work: nothing for the pipeline to build.", f"Why: {r['why']}"])
    lines.append(f"Ready. Approve it: add {' and '.join(f'`{lb}`' for lb in labels_to_add(r))}." if r["ready"]
                 else "Not ready.")
    lines += [f"Size {r['size']}, repo {r['repo'] or 'not settled'}", f"Why: {r['why']}"]
    if r["questions"]:
        lines += ["Questions:"] + [f"  - {q}" for q in r["questions"]]
    if r["plan"]:
        lines += ["Plan:"] + [f"  {n}. {step}" for n, step in enumerate(r["plan"], 1)]
    return "\n".join(lines)


def issue_header(changed: str) -> str:
    return (f"Last change {changed}. Arete refreshes this every hour from your open tickets that have no "
            f"`{tracker.LABEL}` label, and edits it only when something changes. To approve a ready ticket, add the "
            "labels it names. `arete triage REF` on the Mac shows one ticket in full.")


def quiet(text: str) -> str:
    return re.sub(r"(@|#|GH-|github\.com/)(?=\w)", "\\1\u200b", " ".join(text.split()), flags=re.IGNORECASE)


def issue_link(r: dict) -> str:
    return f"[{r['ref']}]({r['url']})" if r["tracker"] == "jira" and r["url"] else f"`{r['ref']}`"


def issue_block(r: dict, with_plan: bool) -> str:
    title = quiet(r["title"])
    if r["ready"] is None or r["code"] == 0:
        return f"- {issue_link(r)}: {title} ({quiet(r['why'])})"
    lines = [f"- {issue_link(r)} **{r['size']}** in `{r['repo'] or '?'}`: {title}"]
    if r["ready"]:
        lines.append(f"  Add {' and '.join(f'`{lb}`' for lb in labels_to_add(r))}.")
    else:
        lines += [f"  - {quiet(q)}" for q in r["questions"]] or [f"  {quiet(r['why'])}"]
    if with_plan and r["plan"]:
        steps = "\n".join(f"  {n}. {quiet(step)}" for n, step in enumerate(r["plan"], 1))
        lines.append(f"\n  <details><summary>Plan</summary>\n\n{steps}\n\n  </details>\n")
    return "\n".join(lines)


def issue_sections(rows: list[dict], with_plan: bool = True, limit: int | None = None) -> str:
    g = groups(rows)
    shown = 0
    out = []
    for heading, part in (("Ready", g.ready), ("With questions", g.with_questions), ("Not code work", g.not_code),
                          ("Not triaged", g.not_triaged)):
        blocks = [issue_block(r, with_plan) for r in part]
        if limit is not None:
            blocks = blocks[:max(0, limit - shown)]
        shown += len(blocks)
        if blocks:
            out.append(f"## {heading} ({len(part)})\n\n" + "\n".join(blocks))
    if shown < len(rows):
        out.append(f"({len(rows) - shown} more tickets left out to fit; `arete triage` on the Mac lists them all.)")
    return "\n\n".join(out) or "No open tickets without the `agent` label."


def fitted_sections(rows: list[dict]) -> str:
    text = issue_sections(rows)
    if len(text) > MAX_ISSUE_CHARS:
        text = issue_sections(rows, with_plan=False)
    keep = len(rows)
    while len(text) > MAX_ISSUE_CHARS and keep > 0:
        keep = keep * 9 // 10
        text = issue_sections(rows, with_plan=False, limit=keep)
    return text


class BoardRefused(Exception):
    pass


def publish(cfg: config.Config, rows: list[dict], meta: dict) -> str:
    repo = cfg.alert_repo
    if not repo:
        return "local only: alert_repo is empty"
    private = gh.repo_private(repo)
    if private is None:
        raise BoardRefused(f"cannot read {repo}")
    if not private:
        raise BoardRefused(f"{repo} is public and the board is private")
    sections = fitted_sections(rows)
    digest = hashlib.sha256(sections.encode()).hexdigest()[:16]
    if meta.get("repo") != repo:
        meta.update(repo=repo, issue=None, digest="")
    changed = digest != meta.get("digest")
    if changed or not meta.get("changed"):
        meta["changed"] = util.iso()
    body = f"{issue_header(meta['changed'])}\n\n{sections}\n"
    number = meta.get("issue") or find_issue(repo)
    issue_state = gh.existing_issue_state(repo, number) if number else ""
    if not issue_state:
        gh.ensure_label(repo, ISSUE_LABEL, "c5def5", "Arete triage board")
        number = gh.issue_create(repo, ISSUE_TITLE, body, [ISSUE_LABEL])
        said = f"opened {repo}#{number}"
    elif not changed:
        said = f"unchanged at {repo}#{number}"
    else:
        gh.issue_edit_body(repo, number, body)
        said = f"updated {repo}#{number}"
        if issue_state == "CLOSED":
            gh.issue_reopen(repo, number)
            said += " and reopened it"
    meta.update(issue=number, digest=digest)
    return said


def find_issue(repo: str) -> int | None:
    found = [i for i in gh.issues(repo, ISSUE_LABEL, state="all", limit=10) if i["title"] == ISSUE_TITLE]
    return int(found[0]["number"]) if found else None
