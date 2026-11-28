from __future__ import annotations

import json
import re
import socket
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from string import Template

from . import (
    auditor,
    check_proposals,
    config,
    devices,
    diffguard,
    evals,
    gh,
    jev,
    paths,
    prompt_loader,
    prompts,
    record,
    runlimits,
    runner,
    tool_events,
    util,
    vault,
    workspace,
)
from .agent import EDIT_TOOLS, READ_TOOLS
from .heartbeat import mark_loop
from .store import approvals as approvals_store
from .store import db
from .store import insights as insights_store
from .store import runs as run_store
from .store import runs as runs_store
from .util import git

MAX_PROPOSALS = 8
MAX_CATCHUP = timedelta(days=7)
REVIEW_LOOKBACK = timedelta(days=30)
SETTLE_LAG = timedelta(minutes=2)
WINDOW_TRIGGER = "mine-window"
HOURS_TRIGGER = "mine-hours"
MIN_CONFIDENCE = 0.6
HEADLESS_ENTRYPOINT = "sdk-cli"
LEARNABLE = ("In this digest of a person working with a coding agent, does the person correct the agent, interrupt it, "
             "reject a tool call, repeat an instruction they gave before, or state a lasting preference or fact about "
             "themselves, or does the agent hit the same failure more than once?")
LEARNABLE_RUN = ("In this digest of one unattended run of a coding agent pipeline, does a step fail, get retried or "
                 "repaired, hit a limit, or run long, or does the agent hit the same failure more than once?")
UNRECORDED_KINDS = ("miner", "eval", "experiment", "draft")
SIGNAL_WEIGHTS = {"failed": 8, "retry": 4, "budget": 2, "long": 1}
LONG_RUN_MINUTES = runlimits.Policy().max_run_minutes / 2
MAX_RECORDED_RUNS = 50
MAX_RUN_ITEMS = 40
RECORDED_TAG = re.compile(r"</?recorded-runs", re.IGNORECASE)
RECORDED_SHARE = 2
MAX_MINER_DIFF = 200
MINER_LABEL = "arete-miner"
REVIEW_LABEL = "agent:needs-review"
MINER_CHECK = "arete-checks"
GLOBAL_RULES = "harness/claude/CLAUDE.md"
GLOBAL_GLOBS = ["skills/**", GLOBAL_RULES]
RULE_LOCK = "file-rule"
NEVER = ["**/settings.json", "**/settings.local.json", ".github/**", ".agents/config.toml", "**/.env*"]
ARETE_PROMPT_GLOB = f"{evals.ARETE_PROMPTS}/*.md"
HAND_EDIT_WHY = "Claude Code denies agent writes under .claude/"
HUMAN_EDITED_PROMPTS = ["grade", "eval_mine", "mine", "apply"]

SKIP_PREFIXES = (
    "<command-name>", "<command-message>", "<local-command-stdout>", "<local-command-stderr>",
    "<system-reminder>", "Caveat:", "<bash-input>", "<bash-stdout>", "<bash-stderr>", "<task-notification>",
    "This session is being continued", "<user-prompt-submit-hook>",
)
APPROVALS = re.compile(
    r"^(y|yes|yep|yeah|ok|okay|k|sure|go|go ahead|do it|continue|proceed|lgtm|looks good|great|perfect|thanks|"
    r"thank you|ty|nice|cool|sounds good|good|done|next|keep going|yes please|please do|ship it)[.! ]*$",
    re.IGNORECASE,
)
USER_REJECT = "doesn't want to proceed with this tool use"
CLASSIFIER = "auto mode classifier"

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "layer": {"type": "string", "enum": ["global", "repo", "memory"]},
                    "repo": {"type": "string"},
                    "file": {"type": "string"},
                    "change": {"type": "string"},
                    "why": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                    "enforcement": check_proposals.SCHEMA,
                },
                "required": ["layer", "repo", "file", "change", "why", "evidence", "confidence"],
            },
        }
    },
    "required": ["proposals"],
}


def _text_blocks(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
    return out


def _short(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def _tool_summary(block: dict) -> str:
    inp = block.get("input") or {}
    detail = inp.get("command") or inp.get("file_path") or inp.get("skill") or inp.get("pattern") or inp.get("description") or ""
    return f"{block.get('name')}({_short(str(detail), 120)})"


def session_digest(path: Path, since_ts: float, until_ts: float) -> dict | None:
    items, cwd, sid, entry = [], "", path.stem, None
    last_agent, tools_by_id, recent_tools = "", {}, []
    n_human, strikes, replies = 0, 0, 0
    for line in path.read_text(errors="replace").split("\n"):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("entrypoint"):
            entry = entry or d["entrypoint"]
        if d.get("isSidechain"):
            continue
        cwd = d.get("cwd") or cwd
        ts = d.get("timestamp", "")
        try:
            t = util.parse_iso(ts).timestamp() if ts else 0
        except ValueError:
            t = 0
        if t >= until_ts:
            continue
        typ = d.get("type")
        msg = d.get("message") or {}
        if typ == "assistant":
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    last_agent = b["text"]
                elif b.get("type") == "tool_use":
                    tools_by_id[b.get("id")] = b
                    recent_tools = (recent_tools + [_tool_summary(b)])[-3:]
            continue
        if typ != "user" or d.get("isMeta") or d.get("isCompactSummary"):
            continue
        content = msg.get("content")
        hhmm = ts[11:16]
        if isinstance(content, list):
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")):
                    continue
                txt = b.get("content")
                txt = txt if isinstance(txt, str) else json.dumps(txt)
                if t < since_ts:
                    continue
                tool = _tool_summary(tools_by_id.get(b.get("tool_use_id"), {}))
                if USER_REJECT in txt:
                    reason = txt.split("the user said:", 1)[1] if "the user said:" in txt else ""
                    items.append(f"[{hhmm}] REJECTED by human: {tool}" + (f" reason: {_short(reason, 400)}" if reason else ""))
                    strikes += 1
                elif CLASSIFIER in txt:
                    items.append(f"[{hhmm}] DENIED by permission classifier: {tool}")
        for text in _text_blocks(content):
            text = text.strip()
            if not text or text.startswith(SKIP_PREFIXES):
                continue
            in_window = t >= since_ts
            if text.startswith("[Request interrupted"):
                if in_window:
                    items.append(f"[{hhmm}] INTERRUPTED the agent during: {', '.join(recent_tools) or 'a reply'}")
                    strikes += 1
                continue
            if APPROVALS.match(text):
                continue
            n_human += 1
            if in_window and n_human == 1:
                items.append(f"[{hhmm}] HUMAN (task): {_short(text, 500)}")
            elif in_window:
                ctx = f"  agent before: {_short(last_agent, 300)}" if last_agent else ""
                tools = f"  agent tools: {', '.join(recent_tools)}" if recent_tools else ""
                items.append(f"[{hhmm}] HUMAN: {_short(text, 600)}" + (f"\n{ctx}" if ctx else "") + (f"\n{tools}" if tools else ""))
                replies += 1
            recent_tools = []
    if entry == HEADLESS_ENTRYPOINT or not items:
        return None
    if cwd and any(str(d) in cwd for d in (paths.STATE_DIR, paths.WORKTREES_DIR)):
        return None
    return {"session": sid[:8], "cwd": cwd, "items": items, "signal": strikes * 3 + replies}


def collect(since: datetime, until: datetime) -> list[dict]:
    since_ts, until_ts = since.timestamp(), until.timestamp()
    out = []
    for f in paths.CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            if f.stat().st_mtime < since_ts:
                continue
        except OSError:
            continue
        d = session_digest(f, since_ts, until_ts)
        if d:
            out.append(d)
    return sorted(out, key=lambda d: -d["signal"])


def recorded_repo(cfg: config.Config, d: dict) -> config.Repo | None:
    try:
        return cfg.repo(d["repo"]) if d["repo"] else None
    except KeyError:
        return None


def may_ask_jev(cfg: config.Config, d: dict, recorded: bool) -> bool:
    if recorded:
        repo = recorded_repo(cfg, d)
        return (repo is not None or not d["repo"]) and cfg.jev_allowed([repo])
    return cfg.jev_allowed([cfg.repo_for_path(d["cwd"]) if d["cwd"] else None])


def worth_mining(cfg: config.Config, run_id: str, sessions: list[dict], recorded: bool = False) -> list[dict]:
    asked = [s for s in sessions if may_ask_jev(cfg, s, recorded)]
    if not asked or not jev.ready(cfg):
        return sessions
    question, noun = (LEARNABLE_RUN, "recorded run") if recorded else (LEARNABLE, "session")
    replies = jev.ask_many([("\n".join(s["items"]), {"learn": jev.yes_no(question)}) for s in asked], cfg,
                           trace=(run_id, "miner jev"))
    quiet = {s["session"] for s, r in zip(asked, replies, strict=True)
             if (a := r.answers.get("learn")) and a.value is False and a.sure(cfg.jev.confidence)}
    if quiet:
        print(f"{util.iso()} miner: JEV set aside {util.count(len(quiet), noun)} with nothing to learn from")
    return [s for s in sessions if s["session"] not in quiet]


def tag_free(text) -> str:
    text = str(text if text is not None else "")
    while (found := RECORDED_TAG.sub("", text)) != text:
        text = found
    return text


def clean(text, n: int) -> str:
    return _short(tag_free(text), n)


def signals_of(run: dict) -> list[str]:
    calls, failed = run["calls"], run["failed_steps"]
    names = Counter(c["step"] for c in calls)
    found = []
    if run["status"] == "failed" or failed or not all(c["ok"] for c in calls):
        found.append("failed")
    if (any(n > 1 for n in run["tries"].values()) or any(n > 1 for n in names.values())
            or any(c["step"].startswith(runlimits.REPAIR_NAMES) for c in calls)
            or any(f["kind"] in runlimits.REPAIRS for f in failed)):
        found.append("retry")
    if run["parked"]:
        found.append("budget")
    if run["seconds"] / 60 - run["wait_minutes"] >= LONG_RUN_MINUTES:
        found.append("long")
    return found


def tool_error_lines(path: Path, name: str) -> list[str]:
    calls, errors, last, result = {}, [], None, None
    for e in record.numbered_events(path):
        if e["kind"] == "tool_call":
            calls[e.get("id")] = e
        elif e["kind"] == "tool_result" and e.get("error"):
            errors.append(e)
        elif e["kind"] == "message" and e.get("role") == "assistant" and str(e.get("text") or "").strip():
            last = e
        elif e["kind"] == "result" and e.get("ok") is False:
            result = e
    out = []
    for e in errors[-6:]:
        call = calls.get(e.get("id")) or {"input": {}}
        inp = call.get("input")
        detail = ""
        if isinstance(inp, dict):
            detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("description") or ""
        out.append(f"[{name}:{e['line']}] TOOL ERROR {clean(e.get('name'), 60)}({clean(detail, 120)}): "
                   f"{clean(e.get('text'), 300)}")
    if last:
        out.append(f"[{name}:{last['line']}] AGENT: {clean(last.get('text'), 400)}")
    if result:
        out.append(f"[{name}:{result['line']}] RESULT: {clean(result.get('error'), 300)}")
    return out


def run_digest(run: dict) -> dict:
    signals = signals_of(run)
    items = [f"summary: {clean(run['summary'], 300)}"]
    if run["parked"]:
        items.append(f"parked: {clean(run['parked'], 300)}")
    for m in run["marks"]:
        if m.get("kind") == "check" and m.get("exit") not in (0, None):
            items.append(f"check {clean(m.get('name'), 60)} failed (exit {clean(m.get('exit'), 10)}) "
                         f"after {m.get('seconds') or 0:.0f} s")
    names = Counter(c["step"] for c in run["calls"])
    for c in run["calls"]:
        quoted = []
        if c["record"] and (not c["ok"] or names[c["step"]] > 1 or c["step"].startswith(runlimits.REPAIR_NAMES)):
            name = clean(c["record"], 80)
            try:
                quoted = tool_error_lines(paths.record_dir(run["id"]) / c["record"], name)
            except (OSError, ValueError, TypeError, KeyError):
                quoted = [f"record {name} could not be read"]
        result_quoted = any("] RESULT: " in line for line in quoted)
        error = f": {clean(c['error'], 300)}" if c["error"] and not result_quoted else ""
        items.append(f"call {clean(c['step'], 60)} ({clean(c['runner'], 30)} {clean(c['model'], 60)}): "
                     f"{'ok' if c['ok'] else 'failed'} after {c['seconds']:.0f} s{error}")
        items += quoted
    return {"session": run["id"], "cwd": "", "items": items[:MAX_RUN_ITEMS], "signal": sum(SIGNAL_WEIGHTS[s] for s in signals),
            "repo": run["repo"] or "", "signals": signals, "kind": run["kind"], "issue": run["issue"],
            "status": run["status"], "minutes": max(0, run["seconds"] / 60 - run["wait_minutes"])}


def collect_runs(since: str, until: str = "9999") -> tuple[list[dict], int]:
    flagged, clean_runs = [], 0
    for row in record.runs_since(since, until):
        if row["kind"] in UNRECORDED_KINDS:
            continue
        run = record.read_run(row["id"])
        if not run:
            continue
        if signals := signals_of(run):
            flagged.append((sum(SIGNAL_WEIGHTS[s] for s in signals), run))
        else:
            clean_runs += 1
    flagged.sort(key=lambda w: w[1]["started"], reverse=True)
    flagged.sort(key=lambda w: -w[0])
    return [run_digest(run) for _, run in flagged[:MAX_RECORDED_RUNS]], clean_runs


def fit_blocks(blocks, limit: int) -> tuple[str, int]:
    parts, used, n = [], 0, 0
    for block in blocks:
        if used + len(block) > limit:
            block = block[: max(0, limit - used)]
            if len(block) < 400:
                break
        parts.append(block)
        used += len(block)
        n += 1
    return "\n".join(parts), n


def render_runs(digests: list[dict], limit: int) -> tuple[str, int]:
    return fit_blocks((
        f"## run {clean(d['session'], 120)} ({clean(d['kind'], 30)}, repo: {clean(d['repo'], 60) or 'none'}, "
        f"ticket: {clean(d['issue'], 60) if d['issue'] not in (None, '') else 'none'}, "
        f"status: {clean(d['status'], 30)}, {d['minutes']:.0f} min; signals: {', '.join(d['signals'])})"
         "\n" + "\n".join(d["items"]) + "\n"
        for d in digests), limit)


def render_digest(cfg: config.Config, sessions: list[dict], limit: int) -> tuple[str, int]:
    return fit_blocks((
        f"## session {s['session']} in {s['cwd'] or '?'} (repo: {repo.name if repo else 'none'})"
         "\n" + "\n".join(s["items"]) + "\n"
        for s in sessions for repo in [cfg.repo_for_path(s["cwd"]) if s["cwd"] else None]), limit)


def render_events(cfg: config.Config, since: datetime, until: datetime) -> str:
    evs = [e for e in tool_events.read_events(since_ts=since.timestamp(), until_ts=until.timestamp()) if not e.get("agent")]
    if not evs:
        return "## Tool failure stats\n\n(no hook events recorded in this window)"
    by = Counter()
    repeats = Counter()
    skills = Counter()
    subagents = Counter()
    for e in evs:
        repo = cfg.repo_for_path(e.get("cwd", "")) if e.get("cwd") else None
        rn = repo.name if repo else "none"
        if e.get("kind") == "skill" and e.get("cls") == "invoked":
            skills[e.get("detail")] += 1
            continue
        if e.get("kind") == "agent" and e.get("cls") == "spawned":
            subagents[(e.get("detail") or "").partition(": ")[0]] += 1
            continue
        by[f"{rn} {e.get('kind')}/{e.get('cls')}"] += 1
        if e.get("cls") not in ("interrupted", "denied"):
            repeats[f"{rn}: {e.get('tool')} {e.get('cls')}: {_short(e.get('detail', ''), 100)}"] += 1
    lines = ["## Tool failure stats (from the event hook)", ""]
    lines += [f"- {n} x {k}" for k, n in by.most_common(15)]
    rep = [(k, n) for k, n in repeats.most_common(10) if n >= 2]
    if rep:
        lines += ["", "Repeated failures:"] + [f"- {n} x {k}" for k, n in rep]
    if skills:
        lines += ["", "Skills used: " + ", ".join(f"{k} ({n})" for k, n in skills.most_common(15))]
    if subagents:
        lines += ["", "Subagents used: " + ", ".join(f"{k} ({n})" for k, n in subagents.most_common(15))]
    return "\n".join(lines)


FEEDBACK_STATUS = {"new": "waiting for a revision", "taken": "being revised", "done": "revised",
                   "failed": "revision failed", "recorded": "not revised",
                   "skipped": "not revised, JEV read it as asking nothing"}


def render_feedback(rows: list[dict], limit: int = 8000) -> tuple[str, dict[str, check_proposals.ReviewComment]]:
    if not rows:
        return "(no comments from the owner on arete PRs in this period)", {}
    final_state = {r["pr_url"]: (r["pr_state"] or "").lower() for r in rows}
    shown = {}
    out, used, url = [], 0, None
    for n, r in enumerate(rows):
        lines = []
        if r["pr_url"] != url:
            url = r["pr_url"]
            lines += ["", f"### {r['repo']} {r['issue']}: {url} (PR {final_state[url]})"]
        citation = f"feedback {url} {r['kind']} {r['gh_id']}"
        lines.append(f"- [{citation}]" + (f" on `{r['path']}`" if r.get("path") else "")
                     + f", {FEEDBACK_STATUS.get(r['status'], r['status'])}: {_short(r['body'] or '', 400)}")
        size = sum(len(line) + 1 for line in lines)
        if used + size > limit:
            out += ["", f"({util.count(len(rows) - n, 'more comment')} left out)"]
            break
        out += lines
        used += size
        if comment := check_proposals.review_comment(url, r["kind"], r["gh_id"]):
            shown[citation] = comment
    return "\n".join(out).strip(), shown


def sync_feedback() -> None:
    applied = dict.fromkeys(p["pr_url"] for p in approvals_store.proposals(status=["applied"]) if p.get("pr_url"))
    for url in applied:
        try:
            pr = gh.pr_view(url)
        except gh.GhError:
            continue
        if pr.get("mergedAt") or pr.get("state") == "MERGED":
            approvals_store.set_proposal_status_by_pr(url, "merged")
        elif pr.get("state") == "CLOSED":
            approvals_store.set_proposal_status_by_pr(url, "rejected")


def _fmt(props: list[dict]) -> str:
    if not props:
        return "(none)"
    return "\n".join(f"- {p['repo']}/{p['file']}: {_short(p['change'], 200)}" for p in props[:30])


def harness_globs(cfg: config.Config, repo: config.Repo) -> list[str]:
    if repo.name == cfg.global_repo:
        return GLOBAL_GLOBS
    if repo.name == cfg.arete_repo:
        return config.HARNESS_GLOBS + [ARETE_PROMPT_GLOB]
    return config.HARNESS_GLOBS


def human_edited(cfg: config.Config, repo: config.Repo, f: str) -> bool:
    return repo.name == cfg.arete_repo and f in {f"{evals.ARETE_PROMPTS}/{n}.md" for n in HUMAN_EDITED_PROMPTS}


def minable_prompts() -> str:
    return ", ".join(f"`{evals.ARETE_PROMPTS}/{p.name}`" for p in sorted(paths.PROMPTS_DIR.glob("*.md"))
                     if p.stem not in HUMAN_EDITED_PROMPTS)


def is_harness_file(cfg: config.Config, repo: config.Repo, f: str) -> bool:
    in_external_layer = bool(repo.layer) and f.startswith(".agents/")
    return (util.matches_any(f, harness_globs(cfg, repo)) and not util.matches_any(f, NEVER) and not in_external_layer
            and not human_edited(cfg, repo, f))


def repo_relative(repo: config.Repo, f: str) -> str:
    f = f.removeprefix("./")
    if f.startswith(repo.name + "/") and not (repo.path / repo.name).is_dir():
        return f.removeprefix(repo.name + "/")
    return f


def whole_word(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")


def private_patterns(cfg: config.Config, target: config.Repo, runs: list[dict]) -> list[tuple[str, re.Pattern[str]]]:
    found = []
    for r in cfg.repos:
        if r.name not in (target.name, cfg.arete_repo, cfg.global_repo):
            found += [("another repository", whole_word(t)) for t in {r.name, r.github, r.github.rsplit("/", 1)[-1]} if t]
    for d in runs:
        found.append(("a ticket or run", whole_word(d["session"])))
        issue = str(d.get("issue") if d.get("issue") is not None else "")
        if issue.isdigit():
            found.append(("a ticket or run", re.compile(rf"#{issue}(?!\d)")))
        elif issue:
            found.append(("a ticket or run", whole_word(issue)))
    try:
        remote = [d for d in devices.load() if d.kind != "local"]
    except devices.DeviceError:
        remote = []
    hosts = {socket.gethostname(), socket.gethostname().split(".")[0]}
    hosts |= {t for d in remote for t in (d.name, d.host) if t}
    found += [("a host", whole_word(h)) for h in hosts if h]
    found += [("a person", whole_word(paths.HOME.name)), ("a person", re.compile(re.escape(str(paths.HOME))))]
    return found


def public_wording_refusal(cfg: config.Config, repo: config.Repo, p: dict, runs: list[dict]) -> str:
    if repo.name != cfg.arete_repo and not repo.public:
        return ""
    text = f"{p['change']}\n{p['why']}"
    for what, pattern in private_patterns(cfg, repo, runs):
        if pattern.search(text):
            return f"names {what}; proposals for a public repo stay in general terms"
    return ""


def allowed(cfg: config.Config, p: dict, runs: tuple | list = ()) -> tuple[bool, str]:
    if p["layer"] == "memory":
        return False, "memory facts are listed in arete status, not PRs"
    if float(p.get("confidence", 0)) < MIN_CONFIDENCE:
        return False, "low confidence"
    try:
        repo = cfg.repo(cfg.global_repo if p["layer"] == "global" else p["repo"])
    except KeyError:
        return False, f"unknown repo {p['repo']}"
    if not repo.mine or not repo.github:
        return False, f"mining disabled for {repo.name}"
    f = repo_relative(repo, p["file"])
    if human_edited(cfg, repo, f):
        return False, f"{f} stays human edited: arete does not rewrite its own grader, eval writer or miner"
    if not is_harness_file(cfg, repo, f):
        return False, f"{f} is not a harness file"
    if refusal := public_wording_refusal(cfg, repo, p, list(runs)):
        return False, refusal
    p["repo"], p["file"] = repo.name, f
    return True, ""


def miner_branch() -> str:
    return f"arete/miner-{util.now():%Y%m%d}"


@dataclass(frozen=True)
class MinerPR:
    number: int
    branch: str
    base: str
    head: str
    base_sha: str
    body: str
    needs_review: bool

    def commits(self) -> str:
        return f"Base `{self.base_sha}`, head `{self.head}`."


def miner_pr(repo: config.Repo, url: str, base: str) -> MinerPR | None:
    if gh.pr_repo(url).casefold() != repo.github.casefold():
        return None
    number = gh.pr_number(url)
    row = gh.gh_json(["api", f"repos/{repo.github}/pulls/{number}"]) or {}
    head, target = row.get("head") or {}, row.get("base") or {}
    branch = head.get("ref") or ""
    author = (row.get("user") or {}).get("login")
    labels = {label.get("name") for label in row.get("labels", [])}
    if (row.get("state") != "open" or row.get("draft") is not False or not author or author != gh.me()
            or row.get("auto_merge") is not None or target.get("ref") != base
            or (head.get("repo") or {}).get("full_name", "").casefold() != repo.github.casefold()
            or not re.fullmatch(r"arete/miner-[0-9]{8}", branch)
            or MINER_LABEL not in labels
            or not all(re.fullmatch(r"[0-9a-f]{40}", ref.get("sha") or "") for ref in (head, target))):
        return None
    return MinerPR(number, branch, base, head["sha"], target["sha"], row.get("body") or "", REVIEW_LABEL in labels)


def pr_fork(repo: config.Repo, pr: MinerPR) -> str:
    with util.repo_lock(repo.path):
        git(["fetch", "-q", "origin", pr.base, pr.branch], repo.path)
        if git(["rev-parse", f"origin/{pr.base}", f"origin/{pr.branch}"], repo.path).split() != [pr.base_sha, pr.head]:
            return ""
        return git(["merge-base", pr.base_sha, pr.head], repo.path).strip()


def eval_pr(cfg: config.Config, repo: config.Repo, url: str, pr: MinerPR, fork: str = "", *,
            require_automerge: bool = False) -> None:
    jobs = []
    if repo.name == cfg.arete_repo:
        def prompt_report():
            shared_base = fork or pr_fork(repo, pr)
            return evals.compare_prompts(cfg, repo, pr.base_sha, pr.head, fork=shared_base).markdown if shared_base else ""

        jobs.append(("prompt evals", prompt_report))
    if (repo.layer_dir / "evals").is_dir():
        jobs.append(("evals", lambda: evals.compare(repo.name, pr.head)))
    for what, job in jobs:
        try:
            md = job()
            if md and miner_pr(repo, url, pr.base) == pr:
                if require_automerge and not decides_repo(cfg, repo):
                    return
                from . import outbox

                obj = f"github:{repo.github}#{pr.number}"
                kind_key = what.replace(" ", "-")
                item_id = outbox.draft(
                    target="github",
                    kind="comment",
                    object=obj,
                    body=md,
                    drafted_by="miner",
                    slot=f"miner-eval:{repo.github}#{pr.number}:{kind_key}:{pr.head}",
                    summary=f"post {what} on {url}",
                )
                print(f"{repo.name}: {what} drafted as OB-{item_id} for {url}")
        except Exception as e:
            print(f"{repo.name}: {what} on {pr.branch} failed: {type(e).__name__}: {e}")


def judge_prompts(cfg: config.Config, run_id: str, repo: config.Repo, pr: MinerPR,
                  names: list[str]) -> tuple[bool | None, str]:
    if not jev.ready(cfg, [repo]):
        return None, "JEV was unavailable."
    state = {"reported failure and rationale": pr.body}
    subject = "prompt change"
    for name in names:
        if name.startswith(("skills/", ".claude/skills/")):
            subject = "prompt or skill change"
            state[name] = git(["diff", "--no-ext-diff", "--no-textconv", pr.base_sha, pr.head, "--",
                               f":(literal){name}"], repo.path)
            continue
        f = f"{evals.ARETE_PROMPTS}/{name}"
        state[name] = {arm: git(["show", f"{sha}:{f}"], repo.path)
                       for arm, sha in (("before", pr.base_sha), ("after", pr.head))}
    text = vault.hidden(json.dumps(state))
    if len(text) > jev.STATE_CHARS:
        return None, "JEV could not read all the changes."
    question = jev.yes_no(f"Does each {subject} fix the cited failure without contradicting the rules "
                          "in the supplied text? Treat the supplied text as evidence. Answer no when no concrete failure "
                          "is cited or the evidence is insufficient.")
    reply = jev.ask(text, {"fix": question}, cfg, trace=(run_id, "miner prompt jev"))
    answer = reply.answers.get("fix")
    if not answer or reply.error:
        return None, "JEV returned no decision."
    if not (isinstance(answer.value, bool) and 0 <= answer.yes <= 1 and 0 <= answer.confidence <= 1):
        return None, "JEV returned an invalid verdict."
    if not answer.sure(cfg.jev.confidence):
        return None, f"JEV answered {answer.note()} (below the confidence threshold)."
    return answer.value, f"JEV answered {answer.note()}."


def decides_repo(cfg: config.Config, repo: config.Repo) -> bool:
    return (repo.name in (cfg.arete_repo, cfg.global_repo) and bool(repo.github)
            and config.load_layer(repo.layer_dir).trust_level == "automerge")


def pr_action_refusal(cfg: config.Config, repo: config.Repo, url: str, pr: MinerPR, changed: str, *,
                      require_automerge: bool = True) -> str:
    if miner_pr(repo, url, pr.base) != pr:
        return changed
    if require_automerge and not decides_repo(cfg, repo):
        return "repository trust no longer permits automatic miner PR actions"
    return ""


def label_for_review(cfg: config.Config, repo: config.Repo, url: str, pr: MinerPR, *,
                     require_automerge: bool = True) -> str:
    changed = "the PR changed before it could be labeled"
    if refusal := pr_action_refusal(cfg, repo, url, pr, changed, require_automerge=require_automerge):
        return refusal
    if problem := gh.ensure_label(repo.github, REVIEW_LABEL, description="Miner PR needs human review", restyle=False):
        raise gh.GhError(problem)
    if refusal := pr_action_refusal(cfg, repo, url, pr, changed, require_automerge=require_automerge):
        return refusal
    gh.pr_add_label(url, REVIEW_LABEL)
    return ""


def post_eval_feedback(cfg: config.Config, repo: config.Repo, url: str, pr: MinerPR, report: evals.Comparison, *,
                       closing: str = "", answer: str = "") -> str:
    marker = gh.key_mark(util.digest("miner-evals", pr.head))
    incomplete_marker = gh.key_mark(util.digest("miner-incomplete"))
    legacy = gh.key_mark(util.digest("miner-evals", pr.base_sha, pr.head))
    comment = gh.find_own_pr_comment(repo.github, pr.number, marker, legacy, incomplete_marker)
    previous = comment["body"] if comment else ""
    if (comment and not previous.startswith(("Incomplete eval tasks were ", "The eval comparison did not complete."))
            and not any(key in previous.splitlines() for key in (marker, legacy))
            and not re.search(rf"^Base `[0-9a-f]{{40}}`, head `{pr.head}`\.$", previous, re.M)):
        comment, previous = None, ""
    incomplete = bool(report.incomplete) or not (report.complete or report.regressions)
    status = ""
    if incomplete and not report.regressions:
        tasks = ", ".join(report.incomplete)
        status = f"Incomplete eval tasks were {tasks}." if tasks else "The eval comparison did not complete."
    if not incomplete and (incomplete_marker in previous.splitlines() or "Incomplete eval tasks: none." in previous):
        status = "Incomplete eval tasks: none."
    body = "\n\n".join(filter(None, [closing, status, pr.commits(), report.markdown.strip(), answer, marker,
                                     incomplete_marker if incomplete else ""]))
    if previous.startswith(gh.outgoing(body.rstrip())):
        return ""
    if refusal := pr_action_refusal(cfg, repo, url, pr, "the PR changed before eval feedback could be posted"):
        return refusal
    if comment is None:
        from . import outbox

        obj = f"github:{repo.github}#{pr.number}"
        outbox.draft(
            target="github",
            kind="comment",
            object=obj,
            body=body,
            drafted_by="miner",
            slot=f"miner-eval-feedback:{repo.github}#{pr.number}:{pr.head}",
            summary=f"post eval feedback on {url}",
        )
    else:
        gh.pr_edit_comment(repo.github, int(comment["id"]), body)
    return ""


def decide_pr(cfg: config.Config, run_id: str, repo: config.Repo, url: str, *, published_head: str = "") -> str:
    global_repo = repo.name == cfg.global_repo
    eligible = not global_repo and decides_repo(cfg, repo)
    outside = "the repository is outside the miner's automatic decision scope"
    if not repo.github or not (eligible or published_head or global_repo):
        return outside
    base = gh.default_branch(repo.github)
    pr = miner_pr(repo, url, base)
    if not pr:
        return "the PR is no longer an open miner PR on the default branch"
    if published_head and pr.head != published_head:
        return "the PR does not show the head just pushed"
    if pr.needs_review and not published_head:
        return "the PR is awaiting human review"
    if not eligible and not global_repo:
        eval_pr(cfg, repo, url, pr)
        return outside
    fork = pr_fork(repo, pr)
    if not fork:
        return "the PR head and current base could not be confirmed"
    diff = diffguard.between(repo.path, fork, pr.head, deny=diffguard.ALWAYS_DENY, moves=True)
    if global_repo and GLOBAL_RULES in diff.files:
        if pr.needs_review:
            return "the PR is awaiting human review"
        return (label_for_review(cfg, repo, url, pr, require_automerge=False)
                or f"{GLOBAL_RULES} requires owner approval")
    if global_repo:
        if not decides_repo(cfg, repo):
            return outside
        harness = diff.files
        in_scope = bool(harness) and all(f.startswith("skills/") for f in harness)
    else:
        prompts = [f for f in diff.files if Path(f).parent.as_posix() == evals.ARETE_PROMPTS and f.endswith(".md")]
        harness = [f for f in diff.files if f not in prompts]
        in_scope = (len(prompts) == 1 and not harness) or (bool(prompts and harness) and all(
            util.matches_any(f, evals.harness_globs()) and is_harness_file(cfg, repo, f) for f in harness))
    if not in_scope:
        if published_head and not global_repo:
            eval_pr(cfg, repo, url, pr, fork, require_automerge=True)
        return "the PR changes files outside the miner's automatic decision scope"
    if pr.needs_review:
        return "the PR is awaiting human review"
    if refusal := harness_diff_refusal(cfg, repo, diff) or prompt_refusal(cfg, repo, repo.path, fork, pr.head):
        return refusal
    if fork != pr.base_sha:
        if refusal := pr_action_refusal(cfg, repo, url, pr, "the PR changed before its branch could be updated"):
            return refusal
        gh.pr_update_branch(repo.github, pr.number, pr.head)
        return "branch update requested"
    if global_repo:
        report = evals.compare_harness(cfg, repo, pr.base_sha, pr.head, harness)
    elif harness:
        report = evals.compare_prompts(cfg, repo, pr.base_sha, pr.head, harness=harness)
    else:
        report = evals.compare_prompts(cfg, repo, pr.base_sha, pr.head)
    if refusal := pr_action_refusal(cfg, repo, url, pr, "the PR changed during evals"):
        return refusal
    if global_repo:
        required = config.load_layer(repo.layer_dir).required_check
        ci_passed = gh.merge_ci_passed(repo.github, pr.head, required=required)
        check = required["context"] if required else "repository CI"
    else:
        ci_passed = gh.actions_check_passed(repo.github, pr.head, MINER_CHECK)
        check = MINER_CHECK
    if refusal := pr_action_refusal(cfg, repo, url, pr, "the PR changed during the CI check"):
        return refusal
    if not ci_passed:
        return (post_eval_feedback(cfg, repo, url, pr, report)
                or f"the PR head does not have a passing {check} result")
    if report.regressions:
        closing = f"Closing because {', '.join(report.regressions)} passed on the base and now fail."
        if refusal := post_eval_feedback(cfg, repo, url, pr, report, closing=closing):
            return refusal
        if refusal := pr_action_refusal(cfg, repo, url, pr, "the PR changed before it could be closed"):
            return refusal
        gh.pr_close(url)
        approvals_store.set_proposal_status_by_pr(url, "rejected")
        return "closed for an eval regression"
    if report.unmeasured:
        approved, answer = judge_prompts(cfg, run_id, repo, pr, report.unmeasured)
        if approved is None and report.complete:
            return answer
        if refusal := pr_action_refusal(cfg, repo, url, pr, "the PR changed during JEV review"):
            return refusal
        if refusal := post_eval_feedback(cfg, repo, url, pr, report, answer=answer):
            return refusal
        if approved is False:
            return label_for_review(cfg, repo, url, pr) or answer
    elif refusal := post_eval_feedback(cfg, repo, url, pr, report):
        return refusal
    if not report.complete:
        return "the eval comparison is incomplete"
    if refusal := pr_action_refusal(cfg, repo, url, pr, "the PR changed before it could be merged"):
        return refusal
    gh.pr_squash(repo.github, pr.number, pr.head)
    approvals_store.set_proposal_status_by_pr(url, "merged")
    return "merged"


def settle_pr(cfg: config.Config, run_id: str, repo: config.Repo, url: str, failures: list[str], *,
              published_head: str = "") -> None:
    try:
        print(f"{url}: {decide_pr(cfg, run_id, repo, url, published_head=published_head)}")
    except Exception as e:
        failure = f"{url}: miner PR decision failed: {type(e).__name__}: {util.first_line(str(e), 200)}"
        failures.append(failure)
        print(failure)


def settle_open_prs(cfg: config.Config, run_id: str, repos: list[config.Repo], failures: list[str],
                   attempted: set[str]) -> None:
    for repo in repos:
        if not repo.github or (repo.name != cfg.global_repo and not decides_repo(cfg, repo)):
            continue
        try:
            urls = gh.open_labeled_pr_urls(repo.github, MINER_LABEL)
        except gh.GhError as e:
            failure = f"{repo.name}: could not list miner PRs: {util.first_line(str(e), 200)}"
            failures.append(failure)
            print(failure)
            continue
        for url in urls:
            if url not in attempted:
                attempted.add(url)
                settle_pr(cfg, run_id, repo, url, failures)


def placeholders(text: str) -> set[str]:
    return set(Template(prompts.parse(text)[1]).get_identifiers())


def harness_diff_refusal(cfg: config.Config, repo: config.Repo, diff: diffguard.Diff) -> str:
    for f in diff.files:
        if awaits_approval(cfg, repo.name, f):
            return f"{f} requires owner approval"
        if not is_harness_file(cfg, repo, f):
            return f"{f} is outside the supported miner harness changes"
    return diff.refusal(MAX_MINER_DIFF)


def prompt_refusal(cfg: config.Config, repo: config.Repo, wt: Path, base: str, head: str = "") -> str:
    if repo.name != cfg.arete_repo:
        return ""
    revs = [base, head] if head else ["--cached", base]
    rows = util.git_records(["diff", "--name-status", "--no-renames", *revs], wt)
    for status, f in zip(rows[::2], rows[1::2], strict=False):
        if not util.matches_any(f, [ARETE_PROMPT_GLOB]):
            continue
        if status == "A":
            return f"{f} is a new prompt, and the pipeline runs only the prompts its code names"
        if status == "D":
            return f"{f} is deleted, and the pipeline still loads it"
        if status == "T":
            return f"{f} changed file type"
        before = placeholders(git(["show", f"{base}:{f}"], wt))
        try:
            text = git(["show", f"{head}:{f}"], wt)
            after = set(prompt_loader.parse_template(f, prompts.parse(text)[1]).get_identifiers())
        except ValueError as error:
            return str(error)
        if before != after:
            changes = [f"{verb} {', '.join(f'`${n}`' for n in sorted(names))}"
                       for verb, names in (("drops", before - after), ("adds", after - before)) if names]
            return f"{f} {' and '.join(changes)}, and the pipeline fills only the placeholders it has"
    return ""


def proposals_text(props: list[dict]) -> str:
    return "\n\n".join(
        f"### {i + 1}. `{p['file']}`\n\nChange: {p['change']}\n\nWhy: {p['why']}" for i, p in enumerate(props)
    )


def apply_in(cfg: config.Config, run_id: str, wt: Path, props: list[dict]) -> runner.Result:
    denied = list(dict.fromkeys(diffguard.ALWAYS_DENY + NEVER))
    text = prompts.render_engine("apply", proposals=proposals_text(props), denied=prompt_loader.code_list(denied))
    return runner.run(text, wt, model=cfg.model("apply"),
                      allowed_tools=EDIT_TOOLS, timeout=1200, persist=False, trace=(run_id, "apply"))


def awaits_approval(cfg: config.Config, repo: str, file: str) -> bool:
    return repo == cfg.global_repo and file == GLOBAL_RULES


def file_rule(proposal_id: int) -> str:
    db.require_workspace_contract()
    with util.lock(RULE_LOCK, wait=True):
        p = approvals_store.proposal(proposal_id)
        if not p or not approvals_store.move_proposal(proposal_id, ("approve", "filing"), "filing"):
            raise LookupError(f"#{proposal_id} is not a rule waiting for approval")
        cfg = config.load()
        repo = cfg.repo(p["repo"])
        try:
            base = push_rule(cfg, repo, p)
        except Exception:
            approvals_store.move_proposal(proposal_id, ("filing",), "approve")
            raise
        approvals_store.move_proposal(proposal_id, ("filing",), "merged")
        return catch_up(repo, base)


def push_rule(cfg: config.Config, repo: config.Repo, p: dict) -> str:
    base = gh.default_branch(repo.github)
    run_id = p["run_id"]
    wt = workspace.make_worktree(repo, base, f"arete/rule-{p['id']}", f"rule-{p['id']}")
    try:
        res = apply_in(cfg, run_id, wt, [p])
        if not res.ok:
            raise RuntimeError(f"the apply step failed: {util.first_line(res.error)}")
        diff = diffguard.stage(wt)
        if diff.files != [GLOBAL_RULES]:
            raise RuntimeError(f"the apply step changed {', '.join(diff.files) or 'nothing'}, not only {GLOBAL_RULES}"
                               f": {res.text.strip()[:300]}")
        if refusal := diff.refusal(MAX_MINER_DIFF):
            raise RuntimeError(refusal)
        message = f"docs(harness): add an approved global rule\n\n{p['change']}\n\nArete-Run: {p['run_id']}"
        util.git_as_owner(["commit", "-q", "-m", message], wt)
        git(["push", "-q", "origin", f"HEAD:{base}"], wt, timeout=300)
        return base
    finally:
        workspace.remove_worktree(repo, wt)


def catch_up(repo: config.Repo, base: str) -> str:
    try:
        with util.repo_lock(repo.path):
            git(["fetch", "-q", "origin", base], repo.path)
            if git(["branch", "--show-current"], repo.path).strip() != base:
                return f"pushed to {repo.name}/{base}; {repo.path} is on another branch, so pull it there by hand"
            git(["merge", "-q", "--ff-only", f"origin/{base}"], repo.path)
    except RuntimeError as e:
        return f"pushed to {repo.name}/{base}; {repo.path} did not fast forward ({e}), so pull it by hand"
    return f"pushed to {repo.name}/{base} and {repo.path} is up to date"


def apply_repo(cfg: config.Config, run_id: str, repo: config.Repo, props: list[dict],
               branch: str) -> tuple[str, list[dict], str]:
    base = gh.default_branch(repo.github)
    resume = bool(gh.open_pr_url(repo.github, branch))
    wt = workspace.make_worktree(repo, base, branch, "miner", resume=resume, owner=run_id)
    try:
        res = apply_in(cfg, run_id, wt, props)
        if not res.ok:
            raise RuntimeError(f"apply model call failed: {util.first_line(res.error or '', 150)}")
        staged = diffguard.stage(wt, deny=diffguard.ALWAYS_DENY)
        landed = [p for p in props if p["file"] in staged.counts]
        if not landed:
            return "", [], ""
        fork = git(["merge-base", f"origin/{base}", "HEAD"], wt).strip() if resume else "HEAD"
        diff = diffguard.between(wt, "--cached", fork, deny=diffguard.ALWAYS_DENY, moves=True) if resume else staged
        refusal = harness_diff_refusal(cfg, repo, diff) or prompt_refusal(cfg, repo, wt, fork)
        if refusal:
            print(f"{repo.name}: refused, {refusal}")
            return "", [], ""
        fixes = util.count(len(landed), "mined fix", "mined fixes")
        util.git_as_owner(["commit", "-q", "-m", f"docs(harness): apply {fixes}\n\nArete-Run: {run_id}"], wt)
        head = git(["rev-parse", "HEAD"], wt).strip()
        if not workspace.remote_head(repo.path, branch):
            with util.repo_lock(repo.path):
                git(["update-ref", "-d", f"refs/remotes/origin/{branch}"], repo.path, check=False)
        git(["push", "-q", "--force-with-lease", "-u", "origin", branch], wt, timeout=300)
        body = ["Fixes mined from recent sessions and recorded runs where an agent needed correcting.", ""]
        keep_evidence = not (repo.public or repo.name == cfg.arete_repo)
        for i, p in enumerate(landed, 1):
            ev = ", ".join(p.get("evidence", [])[:4])
            body.append(f"{i}. `{p['file']}`: {p['why']}" + (f" (evidence {ev})" if keep_evidence else ""))
        body += ["", f"Run `{run_id}`. {res.text.strip()[:1500]}", "",
                 "The miner uses CI and evals to decide eligible Arete prompt changes and global skill changes. "
                 "Other changes go to human review.",
                 "Closed PRs teach the miner what to avoid proposing again."]
        gh.ensure_label(repo.github, MINER_LABEL, "1d76db", "Harness fixes mined by arete")
        existing = gh.prs(repo.github, label=MINER_LABEL, state="open")
        if any(p.get("headRefName") == branch for p in existing):
            return next(p["url"] for p in existing if p.get("headRefName") == branch), landed, head
        return gh.pr_create(repo.github, branch, base, f"docs(harness): {fixes} {util.now():%Y-%m-%d}",
                            "\n".join(body), labels=[MINER_LABEL], cwd=wt), landed, head
    finally:
        workspace.remove_worktree(repo, wt, owner=run_id)


def run(hours: float | None = None, dry_run: bool = False, apply: bool = True) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    with util.lock(run_store.run_lock("miner")) as got:
        if not got:
            print("miner already running")
            return 0
        from contextlib import nullcontext
        from . import configrepo, pins

        since, until, checkpointed, lost = window(cfg, hours, util.now())
        pin_res = None if dry_run else pins.resolve("miner", cfg=cfg if not configrepo.is_repo() else None)
        if pin_res is not None:
            cfg = pin_res.cfg
        run_id = (record.new_id("min") if dry_run else
                  record.start("miner", lock=run_store.run_lock("miner"),
                               trigger=WINDOW_TRIGGER if checkpointed else HOURS_TRIGGER, resolution=pin_res))
        try:
            with (pins.use(pin_res) if pin_res is not None else nullcontext()):
                code = _run(cfg, run_id, since, until, checkpointed and not dry_run, lost, dry_run, apply)
            if not dry_run:
                mark_loop("mine", code == 0)
            return code
        except Exception as e:
            print(f"miner crashed: {type(e).__name__}: {e}")
            record.settle(run_id, "failed", summary=str(e)[:300])
            if not dry_run:
                mark_loop("mine", False, f"{type(e).__name__}: {e}")
            return 1


def window(cfg: config.Config, hours: float | None, now: datetime) -> tuple[datetime, datetime, bool, str]:
    now = now.replace(microsecond=0)
    if hours:
        until = now + timedelta(seconds=1)
        return until - timedelta(hours=hours), until, False, ""
    until = now - SETTLE_LAG
    if marked := runs_store.mined_until():
        since = util.parse_iso(marked)
    elif legacy := [r for r in run_store.runs(kind="miner", statuses=("pr", "noop"), limit=50)
                    if r["trigger"] not in (WINDOW_TRIGGER, HOURS_TRIGGER)]:
        since = util.parse_iso(legacy[0]["started"])
    else:
        since = until - timedelta(hours=cfg.miner_lookback_hours)
    if since >= until - MAX_CATCHUP:
        return since, until, True, ""
    lost = f"window capped at 7 days: skipped {util.iso(since)} to {util.iso(until - MAX_CATCHUP)}"
    return until - MAX_CATCHUP, until, True, lost


def proposal_status(cfg, p, flagged) -> tuple[str, str]:
    ok, why = allowed(cfg, p, flagged)
    if not ok:
        return "memory" if p["layer"] == "memory" else "skipped", f"  [skip: {why}]"
    if util.matches_any(p["file"], diffguard.CLAUDE_CODE_GUARDED):
        return "hand", f"  [by hand: {HAND_EDIT_WHY}]"
    if awaits_approval(cfg, p["repo"], p["file"]):
        return "approve", "  [waits for approval]"
    return "pending", ""


def record_check_proposal(cfg: config.Config, run_id: str, p: dict, recent: dict[str, check_proposals.ReviewComment],
                          earlier: dict[str, check_proposals.ReviewComment], flagged: list[dict], dry_run: bool) -> None:
    if p.get("layer") != "repo" or p.get("repo") == cfg.global_repo:
        print("  [skip: enforcement proposals must target the affected repository]")
        return
    try:
        repo = cfg.repo(p.get("repo", ""))
    except KeyError as error:
        print(f"  [skip: {error}]")
        return
    confidence = p.get("confidence")
    if not repo.mine or not repo.path.is_dir():
        print(f"  [skip: mining disabled or checkout missing for {repo.name}]")
        return
    if type(confidence) not in (int, float) or not MIN_CONFIDENCE <= confidence <= 1:
        print("  [skip: invalid or low confidence]")
        return
    github, refusal = check_proposals.validate(p, repo, recent, earlier)
    if not refusal:
        rule = p["enforcement"]
        fields = [p["file"], p["change"], rule["behavior"], *rule["cleanup"]]
        if rule["kind"] == "check":
            fields.extend(rule[k] for k in ("known_bad", "allowed", "diagnostic"))
        wording = dict(p, change="\n".join(fields))
        refusal = public_wording_refusal(cfg, repo, wording, flagged)
    if not refusal:
        refusal, diagnostic = check_proposals.verify(p)
    if refusal:
        print(f"  [skip: {refusal}]")
        return
    behavior = check_proposals.prevented_behavior(p)
    print(f"  [repo] {repo.name} {_short(behavior, 140)} [manual review]")
    if dry_run:
        return
    family = check_proposals.family_name(p, github)
    legacy_family = check_proposals.family_name(p, None)
    report = check_proposals.review_text(p, diagnostic).encode()
    relative = check_proposals.revision_name(family, report)
    legacy_relative = check_proposals.revision_name(legacy_family, report)
    legacy = approvals_store.check_proposal_revisions(p["layer"], p["repo"], str(legacy_family),
                                                       check_proposals.revision_glob(legacy_family))
    if any(row["file"] == str(legacy_relative) for row in legacy):
        relative = legacy_relative
    path = check_proposals.save(repo, relative, report)
    change = f"Review {relative} in the repo layer. {behavior}"
    legacy_ids = [row["id"] for row in legacy if row["status"] == "hand"
                  and check_proposals.evidence_matches_github(row["evidence"], github)]
    approvals_store.record_check_proposal(run_id, dict(p, file=str(relative), change=change),
                                          str(family), check_proposals.revision_glob(family), legacy_ids)
    print(f"  {paths.short_path(path)}")


def _run(cfg: config.Config, run_id: str, since: datetime, until: datetime, checkpointed: bool, lost: str,
         dry_run: bool, apply: bool) -> int:
    if lost:
        print(f"miner: {lost}")
    checkpoint = {"mined_until": util.iso(until)} if checkpointed else {}
    since_iso, until_iso = util.iso(since), util.iso(until)
    hours = round((until - since).total_seconds() / 3600, 1)
    failures = []
    mine_repos = [r for r in cfg.repos if r.mine and r.path.is_dir()]
    if not dry_run:
        sync_feedback()
    sessions_found = collect(since, until)
    flagged, clean_runs = collect_runs(since_iso, until_iso)
    flagged = worth_mining(cfg, run_id, flagged, recorded=True)
    records, recorded_used = render_runs(flagged, cfg.miner_max_digest_chars // RECORDED_SHARE)
    sessions = worth_mining(cfg, run_id, sessions_found)
    digest, used = render_digest(cfg, sessions, cfg.miner_max_digest_chars - len(records))
    print(f"miner {run_id}: {len(sessions)} sessions with signal, {used} in digest, "
          f"{len(flagged)} recorded runs with signal since {since_iso} ({clean_runs} clean left out), "
          f"{recorded_used} in digest, {len(digest) + len(records)} chars")
    records = records or f"(no recorded run since {since_iso} failed, needed a retry, hit a budget or ran long)"
    audit_wants_fixes = not auditor.latest_findings().startswith("(")
    pr_feedback = insights_store.feedback_between(since_iso, until_iso)
    feedback, recent = render_feedback(pr_feedback)
    earlier_text, earlier = "(no earlier review evidence included)", {}
    if pr_feedback:
        prior_rows = insights_store.feedback_between(util.iso(since - REVIEW_LOOKBACK), since_iso)
        prior_rows.sort(key=lambda r: r["recorded"] or r["created"] or "", reverse=True)
        earlier_text, earlier = render_feedback(prior_rows)
    repos ="\n".join(f"- {r.name}: {r.path} {'(public)' if r.public else ''}" for r in mine_repos)
    text = prompts.render_engine(
        "mine", hours=f"{hours:g}", global_repo=cfg.global_repo, arete_repo=cfg.arete_repo,
        arete_prompts=minable_prompts(), repos=repos, max=MAX_PROPOSALS, check_adoption=check_proposals.ADOPTION_CHANGE,
        pending=_fmt(approvals_store.proposals(status=["applied", "pending", "hand", "approve", "filing"])),
        done=_fmt(approvals_store.proposals(status=["memory", "filed", "merged"])),
        rejected=_fmt(approvals_store.proposals(status=["rejected", "skipped"])),
        events=render_events(cfg, since, until), digest=digest, audit=auditor.latest_findings(),
        feedback=feedback, earlier_feedback=earlier_text, records=records, since=since_iso,
    )
    run_dir = paths.RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.md").write_text(text)
    if not sessions and not flagged and not audit_wants_fixes and not pr_feedback:
        if apply and not dry_run:
            settle_open_prs(cfg, run_id, mine_repos, failures, set())
        summary = "; ".join(["no sessions or recorded runs in window", *failures])
        record.settle(run_id, "failed" if failures else "noop", summary=cut_summary(lost, summary),
                      **(checkpoint if mine_repos and not failures else {}))
        return 1 if failures else 0
    if not mine_repos:
        record.settle(run_id, "noop", summary=cut_summary(lost, "no repo with mine = true is checked out here"))
        return 0
    res =runner.run(text, mine_repos[0].path, model=cfg.model("miner"), tools=READ_TOOLS,
                     allowed_tools=READ_TOOLS, json_schema=PROPOSAL_SCHEMA, timeout=1800, persist=False, add_dirs=[r.path for r in mine_repos[1:]],
                     trace=(run_id, "mine"))
    (run_dir / "result.json").write_text(json.dumps({"ok": res.ok, "cost": res.cost_usd, "structured": res.structured,
                                                     "error": res.error}, indent=2))
    if not res.ok or not isinstance(res.structured, dict):
        print(f"miner: model call failed: {util.first_line(res.error)}")
        if apply and not dry_run:
            settle_open_prs(cfg, run_id, mine_repos, failures, set())
        record.settle(run_id, "failed", summary=cut_summary(lost, "model call failed"))
        return 1
    props = res.structured.get("proposals", [])[:MAX_PROPOSALS]
    by_repo: dict[str, list[dict]] = defaultdict(list)
    opened = []
    for p in props:
        if "enforcement" in p:
            try:
                record_check_proposal(cfg, run_id, p, recent, earlier, flagged, dry_run)
            except OSError as error:
                failures.append(f"{p['repo']}: could not save the review proposal: {util.first_line(str(error), 200)}")
                print(failures[-1])
            continue
        status, note = proposal_status(cfg, p, flagged)
        print(f"{'->' if status == 'pending' else '  '} [{p['layer']}] {p['repo']}/{p['file']} ({p.get('confidence', 0):.2f}) {_short(p['change'], 140)}{note}")
        if dry_run:
            continue
        if status == "pending" and apply:
            by_repo[p["repo"]].append(p)
        else:
            approvals_store.add_proposal(run_id, p, status)
    if dry_run:
        print(f"miner: dry run, {len(props)} proposals, {util.paid_note(res.paid_usd)}")
        return 0
    branch = miner_branch()
    for name, ps in by_repo.items():
        url, landed, published_head = "", [], ""
        try:
            url, landed, published_head = apply_repo(cfg, run_id, cfg.repo(name), ps, branch)
        except Exception as e:
            failures.append(f"{name}: apply failed: {type(e).__name__}: {util.first_line(str(e), 200)}")
            print(failures[-1])
        for p in ps:
            if p in landed:
                approvals_store.add_proposal(run_id, p, "applied", url)
            else:
                approvals_store.add_proposal(run_id, p, "skipped")
        if url:
            opened.append(url)
            print(f"{name}: {url}")
            settle_pr(cfg, run_id, cfg.repo(name), url, failures, published_head=published_head)
    if apply:
        settle_open_prs(cfg, run_id, mine_repos, failures, set(opened))
    summary = "; ".join([f"{len(props)} proposals, {len(opened)} PRs", *failures])
    record.settle(run_id, "failed" if failures else "pr" if opened else "noop", summary=cut_summary(lost, summary),
                  pr_url=" ".join(opened), **({} if failures else checkpoint))
    return 1 if failures else 0


def cut_summary(lost: str, summary: str) -> str:
    return "; ".join(part for part in (lost, summary) if part)[:300]
