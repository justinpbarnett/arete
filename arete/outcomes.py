from __future__ import annotations

import re
import tomllib
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, timedelta
from pathlib import Path

from . import (
    config,
    gh,
    jev,
    jev_verdicts,
    jira,
    paths,
    pipeline,
    pr_recovery,
    prompt_loader,
    record,
    tracker,
    util,
    workspace,
)
from .store import insights as insights_store
from .store import runs as run_store
from .util import git

FEEDBACK_ASKS = "Does this comment ask for a change to the pull request, or ask a question the author should answer?"
UAT_SCHEMA = {
    "type": "object",
    "properties": {
        "before": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
    },
    "required": ["before", "steps"],
}
HUNK_LINES = 6
PR_SEEN_FILE = paths.STATE_DIR / "pr_seen.json"
PR_SEEN_SETTLE = timedelta(minutes=1)
REJECTED_PR_LISTEN = timedelta(days=7)


def owner_feedback(url: str, pr_state: str, repo_name: str, issue: str) -> list[dict]:
    owner = gh.me()
    talk = gh.pr_discussion(url)

    def from_owner(item: dict) -> bool:
        return bool(owner) and (item.get("user") or {}).get("login") == owner and not gh.has_arete_mark(item.get("body"))

    reviews = {r["id"]: r for r in talk.reviews}
    lines_in_review = Counter(c.get("pull_request_review_id") for c in talk.line_comments)
    base = {"pr_url": url, "repo": repo_name, "issue": issue, "pr_state": pr_state,
            "status": "new" if pr_state == "OPEN" else "recorded"}
    rows = [{**base, "kind": "review", "gh_id": r["id"], "body": r["body"], "at_once": 1, "created": r.get("submitted_at")}
            for r in talk.reviews if from_owner(r) and r.get("state") != "PENDING" and (r.get("body") or "").strip()]
    for c in talk.line_comments:
        review = reviews.get(c.get("pull_request_review_id")) or {}
        if not from_owner(c) or review.get("state") == "PENDING":
            continue
        grouped = bool((review.get("body") or "").strip()) or lines_in_review[c.get("pull_request_review_id")] > 1
        rows.append({**base, "kind": "line", "gh_id": c["id"], "body": c.get("body") or "", "path": c.get("path"),
                     "line": c.get("line") or c.get("original_line"),
                     "hunk": "\n".join((c.get("diff_hunk") or "").splitlines()[-HUNK_LINES:]),
                     "thread": c.get("in_reply_to_id") or c["id"], "at_once": int(grouped),
                     "created": c.get("created_at")})
    rows += [{**base, "kind": "comment", "gh_id": c["id"], "body": c.get("body") or "", "at_once": 0,
              "created": c.get("created_at")} for c in talk.comments if from_owner(c)]
    return rows


def feedback_text(row: dict) -> str:
    kind = (f"Kind: line comment on {row.get('path')} line {row.get('line')}\n\nCode:\n```\n{row.get('hunk') or ''}\n```"
            if row["kind"] == "line" else f"Kind: {row['kind']}")
    return f"A comment from the repo's owner on a pull request an agent opened.\n\n{kind}\n\nComment:\n{row['body']}"


def judge_feedback(cfg: config.Config, repo_name: str, url: str, rows: list[dict]) -> None:
    known = insights_store.feedback_keys(url)
    fresh = [row for row in rows if row["status"] == "new" and (row["kind"], row["gh_id"]) not in known]
    try:
        repo = cfg.repo(repo_name)
    except config.UnknownRepo:
        return
    if not fresh or not jev.ready(cfg, [repo]):
        return
    trace = ("", f"feedback {url} jev")
    replies = jev.ask_many([(feedback_text(row), {"asks": jev.yes_no(FEEDBACK_ASKS)}) for row in fresh], cfg,
                           trace=trace)
    for row, reply in zip(fresh, replies, strict=True):
        if (a := reply.answers.get("asks")) is None:
            continue
        score = jev_verdicts.scored(a.yes, cfg.jev.feedback_skip, over=False)
        skip = jev_verdicts.holds(score)
        row["asks"] = score["p"]
        if skip:
            row["status"] = "skipped"
        jev_verdicts.record(jev.traced_run(trace), "feedback", f"{url} {row['kind']} {row['gh_id']}", row["body"] or "",
                        {"asks": score}, f"asks for no change and no answer (p {a.yes:.2f})" if skip else "",
                        "skipped" if skip else "went on")
        print(f"{util.iso()} {url}: JEV reads your {row['kind']} comment {row['gh_id']} as asking for a change or an "
              f"answer at p {a.yes:.2f}" + (f", under the {cfg.jev.feedback_skip:g} skip line, so no run takes it"
                                           if skip else ""))


def record_feedback(cfg: config.Config, r: dict, urls: list[str], prs: list[dict], seen: dict, fresh: dict) -> None:
    from . import unblock

    for url, pr in zip(urls, prs):
        updated = pr.get("updatedAt", "")
        fresh[url] = seen.get(url, "")
        if updated and seen.get(url) == updated:
            continue
        try:
            state = pr.get("state", "")
            rows = owner_feedback(url, state, r["repo"], str(r["issue"]))
            if rows and state == "OPEN":
                newest = run_store.newest_live_whole_run(r["repo"], r["issue"]) or {}
                eligible = rows
                if held := pr_recovery.blocked(newest):
                    repo = next((repo for repo in cfg.repos if repo.name == r["repo"]), None)
                    attached = unblock.recovery_checkouts(repo, newest)[1] if repo else {}
                    eligible = unblock.replies_after_hold(newest, held, rows, attached)
                if eligible:
                    judge_feedback(cfg, r["repo"], url, eligible)
            added = insights_store.add_feedback(rows)
        except gh.GhError as e:
            print(f"{util.iso()} {url}: could not read the review comments: {util.first_line(str(e), 300)}")
            continue
        fresh[url] = updated if util.iso(util.now() - PR_SEEN_SETTLE) > updated else ""
        if added:
            print(f"{util.iso()} {url}: {util.count(added, 'new comment')} from you")


def sync_outcomes(cfg: config.Config) -> None:
    from . import zte

    seen, fresh = util.read_json(PR_SEEN_FILE, {}), {}
    for r in run_store.runs_reading_pr_comments(util.iso(util.now() - REJECTED_PR_LISTEN)):
        if zte.reserved(str(r["issue"])):
            continue
        urls = r["pr_url"].split()
        try:
            prs = [gh.pr_view(url) for url in urls]
        except gh.GhError:
            continue
        record_feedback(cfg, r, urls, prs, seen, fresh)
        closed = all(pr.get("state") in ("MERGED", "CLOSED") for pr in prs)
        merged = all(pr.get("mergedAt") for pr in prs)
        if r["status"] == "rejected" and not merged:
            if not closed:
                record.move(r["id"], "pr", expect=("rejected",))
            continue
        if not closed:
            continue
        for url, pr in zip(urls, prs):
            insights_store.close_feedback(url, pr["state"])
        closed_at = max((util.iso(util.parse_iso(pr["closedAt"]).astimezone(UTC))
                         for pr in prs if pr.get("closedAt")), default=util.iso())
        record.move(r["id"], "merged" if merged else "rejected", expect=(r["status"],), finished=closed_at)
    util.write_json(PR_SEEN_FILE, fresh)
    for repo in cfg.repos:
        if repo.pipeline and repo.github and not repo.layer:
            try:
                _maybe_propose_trust(repo)
            except (RuntimeError, OSError) as e:
                print(f"{util.iso()} {repo.name}: trust proposal failed: {util.first_line(str(e), 300)}")


CATCH_UP = ("pr", "needs-info", "failed")


def sync_moves(cfg: config.Config) -> None:
    if not any(cfg.jira.transitions.get(s) for s in CATCH_UP):
        return
    seen: set[tuple[str, str]] = set()
    for r in run_store.runs(kind="pipeline", limit=200):
        key = (r["repo"] or "", str(r["issue"]))
        if key in seen:
            continue
        seen.add(key)
        status = r["status"]
        target = cfg.jira.transitions.get(status, "") if status in CATCH_UP else ""
        if not target or r.get("moved") == status:
            continue
        try:
            repo = cfg.repo(r["repo"])
        except KeyError:
            continue
        tk = tracker.for_ticket(cfg, repo, str(r["issue"]))
        if not isinstance(tk, tracker.JiraTracker):
            continue
        try:
            m = tk.shift(str(r["issue"]), target, keep_done=True)
        except jira.JiraError as e:
            print(f"{util.iso()} {tracker.run_ticket_name(r)}: could not move it to {target}, trying next poll: "
                  f"{util.first_line(str(e), 300)}")
            continue
        record.note(r["id"], moved=status)
        if m.error or m.moved:
            print(f"{util.iso()} {tracker.run_ticket_name(r)}: "
                  + (f"not moved: {util.first_line(m.error, 300)}" if m.error else f"{status}, moved to {target}"))


def carried_by_newer(rows: list[dict], i: int) -> bool:
    row = rows[i]
    prs = set((row.get("pr_url") or "").split())
    return bool(prs) and any(r["status"] == "merged" and r["repo"] == row["repo"] and r["issue"] == row["issue"]
                             and prs <= set((r.get("pr_url") or "").split()) for r in rows[:i])


def sync_landings(cfg: config.Config) -> None:
    branches = list(cfg.jira.landed)
    if not branches:
        return
    rows = run_store.runs(kind="pipeline", limit=200)
    for i, r in enumerate(rows):
        if (r["status"] != "merged" or not r.get("pr_url") or r.get("landed") == branches[-1]
                or carried_by_newer(rows, i)):
            continue
        try:
            repo = cfg.repo(r["repo"])
        except KeyError:
            continue
        if repo.tracker != "jira" or tracker.is_task_id(r["issue"]):
            continue
        try:
            reached = furthest_branch(branches, r["pr_url"].split(), str(r["issue"]))
            progress = [older.get("landed") for older in rows[i:]
                        if older["status"] == "merged" and older["repo"] == r["repo"] and older["issue"] == r["issue"]
                        and set((older.get("pr_url") or "").split()) == set(r["pr_url"].split())]
            already = max((branches.index(b) for b in progress if b in branches), default=-1)
            if reached is None or branches.index(reached) <= already:
                continue
            target = cfg.jira.landed[reached]
            tk = tracker.for_repo(cfg, repo)
            err = tk.move_to(str(r["issue"]), target)
            if reached != branches[-1]:
                run = pipeline.Run(r["id"], repo, paths.RUNS_DIR / r["id"], tracker=tk, tid=str(r["issue"]), writes_lines=False)
                moved = "" if err else f" and moved to {target}"
                if say_err := run.say(f"This reached `{reached}`{moved}. To test it there:\n\n"
                                      + uat_steps(cfg, run, r, reached)):
                    raise pipeline.RetryNextPoll(say_err)
        except (gh.GhError, jira.JiraError, pipeline.RetryNextPoll) as e:
            print(f"{util.iso()} {tracker.run_ticket_name(r)}: landing check failed, trying next poll: {util.first_line(str(e), 300)}")
            continue
        record.note(r["id"], landed=reached)
        print(f"{util.iso()} {tracker.run_ticket_name(r)}: landed on {reached}, " + (err or f"moved to {target}"))


def uat_steps(cfg: config.Config, run: pipeline.Run, row: dict, branch: str) -> str:
    saved = run.dir / f"uat-{tracker.safe_filename(branch)}.md"
    if saved.exists():
        return saved.read_text()
    run.dir.mkdir(parents=True, exist_ok=True)
    tries_file = run.dir / f"uat-{tracker.safe_filename(branch)}.tries"
    tries = int(tries_file.read_text() or 0) + 1 if tries_file.exists() else 1
    t = run.tracker.get(run.tid)
    try:
        run.cfg, run.layer, run.kind = cfg, config.load_layer(run.repo.layer_dir), t.kind
        most = run.settings("uat").tries
    except (ValueError, TypeError) as e:
        raise pipeline.RetryNextPoll(f"{paths.short_path(run.repo.layer_dir / 'config.toml')}: {e}") from e
    urls = row["pr_url"].split()
    share = pipeline.MAX_DIFF_IN_PROMPT // len(urls)
    prs = "\n\n".join(f"### {url}\n\n{gh.pr_body(url).strip()}\n\n```diff\n{cut(gh.pr_diff(url), share)}\n```"
                       for url in urls)
    res = run.agent("uat", prompt_loader.prompt("uat", ref=t.ref, title=t.title, ticket="\n\n".join(filter(None, [t.body, t.comments])),
                                  prs=prs, branch=branch),
                    run.repo.path, role="uat", json_schema=UAT_SCHEMA, persist=False)
    found = res.structured if res.ok and isinstance(res.structured, dict) else {}
    listed = [re.sub(r"^\s*\d+[.)]\s*", "", s).strip() for s in found.get("steps", []) if s.strip()]
    failure = util.first_line(res.error or "none came back")
    if listed:
        before = found.get("before", "").strip()
        text = (f"**Before you start:** {before}\n\n" if before else "") + "\n".join(
            f"{i}. {s}" for i, s in enumerate(listed, 1))
    elif tries < most:
        tries_file.write_text(str(tries))
        raise pipeline.RetryNextPoll(f"no test steps came back, try {tries} of {most}: {failure}")
    else:
        text = (f"I could not write the steps ({failure}); "
                f"the PR shows what changed: {' '.join(urls)}")
    saved.write_text(text)
    return text


def cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "\n... (cut)"


def furthest_branch(branches: list[str], pr_urls: list[str], key: str) -> str | None:
    merges = [(gh.pr_repo(url), (gh.pr_view(url).get("mergeCommit") or {}).get("oid", "")) for url in pr_urls]
    for branch in reversed(branches):
        if all(branch_holds_work(repo, branch, sha, key) for repo, sha in merges):
            return branch
    return None


def branch_holds_work(repo: str, branch: str, merge_sha: str, key: str) -> bool:
    return bool(merge_sha) and gh.branch_contains(repo, branch, merge_sha) or gh.merged_pr_names(repo, branch, key)


def streak(repo_name: str, since: str = "") -> int:
    n = 0
    rows = run_store.runs(kind="pipeline", repo=repo_name, limit=100)
    for i, r in enumerate(rows):
        if (r["status"] == "merged" and (not since or (r["finished"] or "") > since)
                and not carried_by_newer(rows, i)):
            n += 1
        elif r["status"] in ("rejected", "failed"):
            break
    return n


def layer_is_committed(root: Path) -> bool:
    return bool(git(["ls-files", ".agents/config.toml"], root, check=False).strip())


def with_automerge_trust(text: str) -> str | None:
    data = tomllib.loads(text)
    if data.get("trust", {}).get("level") == "automerge":
        return text
    want = data | {"trust": data.get("trust", {}) | {"level": "automerge"}}
    for new in automerge_trust_edits(text if not text or text.endswith("\n") else text + "\n"):
        try:
            if tomllib.loads(new) == want:
                return new
        except tomllib.TOMLDecodeError:
            continue
    return None


def automerge_trust_edits(text: str) -> Iterator[str]:
    for m in re.finditer(r"""\blevel[ \t]*=[ \t]*("[^"\n]*"|'[^'\n]*')""", text):
        yield text[:m.start(1)] + '"automerge"' + text[m.end(1):]
    for m in re.finditer(r"(?m)^[ \t]*\[[ \t]*trust[ \t]*\][^\n]*\n", text):
        yield text[:m.end()] + 'level = "automerge"\n' + text[m.end():]
    yield text + '\n[trust]\nlevel = "automerge"\n'


def _maybe_propose_trust(repo: config.Repo) -> None:
    layer = config.load_layer(repo.layer_dir)
    if layer.trust_level != "pr" or streak(repo.name) < layer.automerge_after or not layer_is_committed(repo.path):
        return
    branch = "arete/trust-automerge"
    if gh.open_pr_url(repo.github, branch):
        return
    if gh.closed_pr_heads(repo.github, branch):
        closed_at = gh.pr_view(branch, repo.github).get("closedAt") or ""
        if streak(repo.name, since=closed_at) < layer.automerge_after:
            return
    base = repo.target_branch or layer.base_branch or gh.default_branch(repo.github)
    labels = ["arete-trust"] if repo.tracker == "github" else []
    wt = workspace.make_worktree(repo, base, branch, "trust")
    try:
        f = wt / ".agents" / "config.toml"
        if not f.exists():
            return
        text = f.read_text()
        new = with_automerge_trust(text)
        if new is None:
            raise RuntimeError(f".agents/config.toml on {base} sets trust in a form arete cannot edit, "
                               'so set trust.level = "automerge" there by hand')
        if new == text:
            return
        f.write_text(new)
        util.git_as_owner(["commit", "-q", "-am", "chore(agents): raise trust to automerge"], wt)
        git(["push", "-q", "--force-with-lease", "-u", "origin", branch], wt)
        for lb in labels:
            gh.ensure_label(repo.github, lb, "0e8a16", "Trust level change proposed by arete")
        n = streak(repo.name)
        gh.pr_create(repo.github, branch, base, "chore(agents): raise trust to automerge",
                     f"The last {n} arete PRs in this repo merged with none rejected or failed in between.\n\n"
                     "Merging this lets arete enable automerge on PRs that pass checks and review. "
                     "Close it to keep every PR waiting for you.", labels=labels)
    finally:
        workspace.remove_worktree(repo, wt)
