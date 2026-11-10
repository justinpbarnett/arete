from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from string import Template

from . import (
    auditor,
    config,
    diffguard,
    evals,
    gh,
    hook,
    jev,
    paths,
    record,
    runner,
    state,
    util,
)
from .heartbeat import mark_loop
from .pipeline import (
    make_worktree,
    prompt,
    remote_head,
    remove_worktree,
)
from .util import git

MAX_PROPOSALS = 8
MIN_CONFIDENCE = 0.6
HEADLESS_ENTRYPOINT = "sdk-cli"
LEARNABLE = ("In this digest of a person working with a coding agent, does the person correct the agent, interrupt it, "
             "reject a tool call, repeat an instruction they gave before, or state a lasting preference or fact about "
             "themselves, or does the agent hit the same failure more than once?")
MAX_MINER_DIFF = 200
GLOBAL_GLOBS = ["skills/**", "CLAUDE.md", "AGENTS.md"]
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


def session_digest(path: Path, since_ts: float) -> dict | None:
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


def collect(hours: float) -> list[dict]:
    since = util.now() - timedelta(hours=hours)
    since_ts = since.timestamp()
    out = []
    for f in paths.CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            if f.stat().st_mtime < since_ts:
                continue
        except OSError:
            continue
        d = session_digest(f, since_ts)
        if d:
            out.append(d)
    return sorted(out, key=lambda d: -d["signal"])


def worth_mining(cfg: config.Config, run_id: str, sessions: list[dict]) -> list[dict]:
    asked = [s for s in sessions if cfg.jev_allowed([cfg.repo_for_path(s["cwd"]) if s["cwd"] else None])]
    if not asked or not jev.ready(cfg):
        return sessions
    replies = jev.ask_many([("\n".join(s["items"]), {"learn": jev.yes_no(LEARNABLE)}) for s in asked], cfg,
                           trace=(run_id, "miner jev"))
    quiet = {s["session"] for s, r in zip(asked, replies, strict=True)
             if (a := r.answers.get("learn")) and a.value is False and a.sure(cfg.jev.confidence)}
    if quiet:
        print(f"{util.iso()} miner: JEV set aside {util.count(len(quiet), 'session')} with nothing to learn from")
    return [s for s in sessions if s["session"] not in quiet]


def render_digest(cfg: config.Config, sessions: list[dict], limit: int) -> tuple[str, int]:
    parts, used, n = [], 0, 0
    for s in sessions:
        repo = cfg.repo_for_path(s["cwd"]) if s["cwd"] else None
        head = f"## session {s['session']} in {s['cwd'] or '?'} (repo: {repo.name if repo else 'none'})"
        block = head + "\n" + "\n".join(s["items"]) + "\n"
        if used + len(block) > limit:
            block = block[: max(0, limit - used)]
            if len(block) < 400:
                break
        parts.append(block)
        used += len(block)
        n += 1
    return "\n".join(parts), n


def render_events(cfg: config.Config, hours: float) -> str:
    evs = [e for e in hook.read_events(hours) if not e.get("agent")]
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


def render_feedback(rows: list[dict], limit: int = 8000) -> str:
    if not rows:
        return "(no comments from the owner on arete PRs in this window)"
    final_state = {r["pr_url"]: (r["pr_state"] or "").lower() for r in rows}
    out, used, url = [], 0, None
    for n, r in enumerate(rows):
        lines = []
        if r["pr_url"] != url:
            url = r["pr_url"]
            lines += ["", f"### {r['repo']} {r['issue']}: {url} (PR {final_state[url]})"]
        lines.append(f"- {r['kind']}" + (f" on `{r['path']}`" if r.get("path") else "")
                     + f", {FEEDBACK_STATUS.get(r['status'], r['status'])}: {_short(r['body'] or '', 400)}")
        size = sum(len(line) + 1 for line in lines)
        if used + size > limit:
            out += ["", f"({util.count(len(rows) - n, 'more comment')} left out)"]
            break
        out += lines
        used += size
    return "\n".join(out).strip()


def sync_feedback() -> None:
    for p in state.proposals(status=["applied"]):
        if not p.get("pr_url"):
            continue
        try:
            pr = gh.pr_view(p["pr_url"])
        except gh.GhError:
            continue
        if pr.get("mergedAt"):
            state.set_proposal_status_by_pr(p["pr_url"], "merged")
        elif pr.get("state") == "CLOSED":
            state.set_proposal_status_by_pr(p["pr_url"], "rejected")


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


def allowed(cfg: config.Config, p: dict) -> tuple[bool, str]:
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
    p["repo"], p["file"] = repo.name, f
    return True, ""


def miner_branch() -> str:
    return f"arete/miner-{util.now():%Y%m%d}"


def eval_pr(cfg: config.Config, repo: config.Repo, url: str, branch: str) -> None:
    jobs = []
    if repo.name == cfg.arete_repo:
        jobs.append(("prompt evals", lambda: evals.compare_prompts(cfg, repo, branch)))
    if (repo.layer_dir / "evals").is_dir():
        jobs.append(("evals", lambda: evals.compare(repo.name, branch)))
    for what, job in jobs:
        try:
            md = job()
            if md:
                gh.pr_comment(url, md)
                print(f"{repo.name}: {what} posted to {url}")
        except Exception as e:
            print(f"{repo.name}: {what} on {branch} failed: {type(e).__name__}: {e}")


def placeholders(text: str) -> set[str]:
    return set(Template(text).get_identifiers())


def prompt_refusal(cfg: config.Config, repo: config.Repo, wt: Path) -> str:
    if repo.name != cfg.arete_repo:
        return ""
    rows = util.git_records(["diff", "--cached", "--name-status", "--no-renames", "HEAD"], wt)
    for status, f in zip(rows[::2], rows[1::2]):
        if not util.matches_any(f, [ARETE_PROMPT_GLOB]):
            continue
        if status == "A":
            return f"{f} is a new prompt, and the pipeline runs only the prompts its code names"
        if status == "D":
            return f"{f} is deleted, and the pipeline still loads it"
        before, after = placeholders(git(["show", f"HEAD:{f}"], wt)), placeholders((wt / f).read_text())
        if before != after:
            changes = [f"{verb} {', '.join(f'`${n}`' for n in sorted(names))}"
                       for verb, names in (("drops", before - after), ("adds", after - before)) if names]
            return f"{f} {' and '.join(changes)}, and the pipeline fills only the placeholders it has"
    return ""


def apply_repo(cfg: config.Config, run_id: str, repo: config.Repo, props: list[dict],
               branch: str) -> tuple[str, list[dict]]:
    base = gh.default_branch(repo.github)
    wt = make_worktree(repo, base, branch, "miner", resume=bool(gh.open_pr_url(repo.github, branch)))
    try:
        text = "\n\n".join(
            f"### {i + 1}. `{p['file']}`\n\nChange: {p['change']}\n\nWhy: {p['why']}" for i, p in enumerate(props)
        )
        res = runner.run(prompt("apply", proposals=text), wt, model=cfg.model("apply"),
                         allowed_tools=runner.EDIT_TOOLS, timeout=1200, persist=False, trace=(run_id, "apply"))
        if not res.ok:
            return "", []
        diff = diffguard.stage(wt)
        landed = [p for p in props if p["file"] in diff.counts]
        if not landed:
            return "", []
        bad = [f for f in diff.files if not is_harness_file(cfg, repo, f)]
        refusal = (f"touched {', '.join(bad[:5])}" if bad else diff.refusal(MAX_MINER_DIFF)
                   or prompt_refusal(cfg, repo, wt))
        if refusal:
            print(f"{repo.name}: refused, {refusal}")
            return "", []
        fixes = util.count(len(landed), "mined fix", "mined fixes")
        git(["commit", "-q", "-m", f"docs(harness): apply {fixes}\n\nArete-Run: {run_id}"], wt)
        if not remote_head(repo.path, branch):
            with util.repo_lock(repo.path):
                git(["update-ref", "-d", f"refs/remotes/origin/{branch}"], repo.path, check=False)
        git(["push", "-q", "--force-with-lease", "-u", "origin", branch], wt, timeout=300)
        body = ["Fixes mined from recent sessions where an agent needed correcting.", ""]
        for i, p in enumerate(landed, 1):
            ev = ", ".join(p.get("evidence", [])[:4])
            body.append(f"{i}. `{p['file']}`: {p['why']}" + ("" if repo.public else f" (sessions {ev})"))
        if repo.name == cfg.arete_repo and any(util.matches_any(f, [ARETE_PROMPT_GLOB]) for f in diff.files):
            body += ["", "The pipeline reads its prompts from the arete checkout, so pull that checkout after the merge."]
        body += ["", f"Run `{run_id}`. {res.text.strip()[:1500]}", "",
                 "Merge what helps, close what does not: closed PRs teach the miner what not to propose."]
        gh.ensure_label(repo.github, "arete-miner", "1d76db", "Harness fixes mined by arete")
        existing = gh.prs(repo.github, label="arete-miner", state="open")
        if any(p.get("headRefName") == branch for p in existing):
            return next(p["url"] for p in existing if p.get("headRefName") == branch), landed
        return gh.pr_create(repo.github, branch, base, f"docs(harness): {fixes} {util.now():%Y-%m-%d}",
                            "\n".join(body), labels=["arete-miner"], cwd=wt), landed
    finally:
        remove_worktree(repo, wt)


def run(hours: float | None = None, dry_run: bool = False, apply: bool = True) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    hours = hours or cfg.miner_lookback_hours
    with util.lock(state.run_lock("miner")) as got:
        if not got:
            print("miner already running")
            return 0
        run_id = record.new_id("min") if dry_run else record.start("miner", lock=state.run_lock("miner"))
        try:
            code = _run(cfg, run_id, hours, dry_run, apply)
            if not dry_run:
                mark_loop("mine", code == 0)
            return code
        except Exception as e:
            print(f"miner crashed: {type(e).__name__}: {e}")
            record.settle(run_id, "failed", summary=str(e)[:300])
            if not dry_run:
                mark_loop("mine", False, f"{type(e).__name__}: {e}")
            return 1


def _run(cfg: config.Config, run_id: str, hours: float, dry_run: bool, apply: bool) -> int:
    if not dry_run:
        sync_feedback()
    sessions = worth_mining(cfg, run_id, collect(hours))
    digest, used = render_digest(cfg, sessions, cfg.miner_max_digest_chars)
    print(f"{util.iso()} miner: {len(sessions)} sessions with signal, {used} in digest, {len(digest)} chars")
    audit_wants_fixes = not auditor.latest_findings().startswith("(")
    pr_feedback = state.feedback_since(util.iso(util.now() - timedelta(hours=hours)))
    if not sessions and not audit_wants_fixes and not pr_feedback:
        record.settle(run_id, "noop", summary="no human sessions in window")
        return 0
    mine_repos = [r for r in cfg.repos if r.mine and r.path.is_dir()]
    if not mine_repos:
        record.settle(run_id, "noop", summary="no repo with mine = true is checked out here")
        return 0
    repos = "\n".join(f"- {r.name}: {r.path} {'(public)' if r.public else ''}" for r in mine_repos)
    text = prompt(
        "mine", hours=f"{hours:g}", global_repo=cfg.global_repo, arete_repo=cfg.arete_repo,
        arete_prompts=minable_prompts(), repos=repos, max=MAX_PROPOSALS,
        pending=_fmt(state.proposals(status=["applied", "pending", "hand"])),
        rejected=_fmt(state.proposals(status=["rejected"])),
        events=render_events(cfg, hours), digest=digest, audit=auditor.latest_findings(),
        feedback=render_feedback(pr_feedback),
    )
    run_dir = paths.RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.md").write_text(text)
    res = runner.run(text, mine_repos[0].path, model=cfg.model("miner"), allowed_tools=runner.READ_TOOLS,
                     json_schema=PROPOSAL_SCHEMA, timeout=1800, persist=False, add_dirs=[r.path for r in mine_repos[1:]],
                     trace=(run_id, "mine"))
    (run_dir / "result.json").write_text(json.dumps({"ok": res.ok, "cost": res.cost_usd, "structured": res.structured,
                                                     "error": res.error}, indent=2))
    if not res.ok or not isinstance(res.structured, dict):
        print(f"miner: model call failed: {util.first_line(res.error)}")
        record.settle(run_id, "failed", summary="model call failed")
        return 1
    props = res.structured.get("proposals", [])[:MAX_PROPOSALS]
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for p in props:
        ok, why = allowed(cfg, p)
        by_hand = ok and util.matches_any(p["file"], diffguard.CLAUDE_CODE_GUARDED)
        mark = "->" if ok and not by_hand else "  "
        note = f"  [skip: {why}]" if not ok else f"  [by hand: {HAND_EDIT_WHY}]" if by_hand else ""
        print(f"{mark} [{p['layer']}] {p['repo']}/{p['file']} ({p.get('confidence', 0):.2f}) {_short(p['change'], 140)}{note}")
        if dry_run:
            continue
        if by_hand:
            state.add_proposal(run_id, p, "hand")
        elif ok and apply:
            by_repo[p["repo"]].append(p)
        else:
            state.add_proposal(run_id, p, "memory" if p["layer"] == "memory" else "pending" if ok else "skipped")
    if dry_run:
        print(f"miner: dry run, {len(props)} proposals, {util.paid_note(res.paid_usd)}")
        return 0
    opened, failures = [], []
    branch = miner_branch()
    for name, ps in by_repo.items():
        url, landed = "", []
        try:
            url, landed = apply_repo(cfg, run_id, cfg.repo(name), ps, branch)
        except Exception as e:
            failures.append(f"{name}: apply failed: {type(e).__name__}: {util.first_line(str(e), 200)}")
            print(failures[-1])
        for p in ps:
            if p in landed:
                state.add_proposal(run_id, p, "applied", url)
            else:
                state.add_proposal(run_id, p, "skipped")
        if url:
            opened.append(url)
            print(f"{name}: {url}")
            if name != cfg.global_repo:
                eval_pr(cfg, cfg.repo(name), url, branch)
    summary = "; ".join([f"{len(props)} proposals, {len(opened)} PRs", *failures])
    record.settle(run_id, "failed" if failures else "pr" if opened else "noop", summary=summary[:300],
                  pr_url=" ".join(opened))
    return 1 if failures else 0
