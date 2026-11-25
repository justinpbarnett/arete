from __future__ import annotations

import re
from urllib.parse import quote

from . import gh, jira, outbound, release_proof, tracker, util, vault, verify
from .store import orchestration, runs, specs, workspaces

GUIDANCE = (
    "Deliver this spec on its own ticket, with no child tickets or graph. Inspect the checkout if needed. "
    "Before implementation, use spec.define for one observable outcome and a reproducible procedure grounded in "
    "the spec and owner answers. If they leave a product decision open, ask one focused owner question instead "
    "of assuming an answer. Leave environment blank unless the owner requires proof at a named runtime. "
    "Run checks, independent review and in-use verification of the exact criterion. Do not request an "
    "unverified exception. Publication produces a candidate, not a delivery receipt. The engine checks landing "
    "and keeps the ticket open until it has evidence at the delivery point."
)
SHA = re.compile(r"[0-9a-f]{40}")


def definition(values: dict) -> dict:
    found = {key: verify.one_line(value) for key, value in values.items()}
    if not found["criterion"] or not found["procedure"] or any(len(v) > 1000 for v in found.values()):
        raise ValueError("spec.define needs one observable criterion and its verification procedure (up to 1000 characters)")
    return found


def describe(done: dict) -> str:
    return (f"## Definition of done\n\n{done['criterion']}\n\nObserve it by: {done['procedure']}"
            + (f"\n\nRequired runtime: {done['environment']}" if done.get("environment") else ""))


def behavior(report: verify.Verified, done: dict) -> dict:
    criterion = done["criterion"].casefold()
    if (report.verdict != "pass" or not report.finished or not report.behavior_reached or report.failed_checks
            or criterion not in {name.casefold() for name in report.observed_requirements()}):
        return {}
    for check in report.checks:
        if verify.one_line(check.get("what", "")).casefold() == criterion:
            how, saw = (verify.one_line(check.get(key, ""))[:2000] for key in ("how", "saw"))
            if how and saw:
                return {"how": how, "saw": saw}
    return {}


def source_version(ticket: tracker.Ticket) -> str:
    return orchestration.digest([ticket.title, ticket.body])


def landing_policy(tk: tracker.Tracker) -> list[list[str]]:
    return [list(pair) for pair in tk.cfg.landed.items()] if isinstance(tk, tracker.JiraTracker) else []


def candidate(job, done: dict, policy: str, acceptance: str, trees: dict) -> dict:
    observed = behavior(job.verified, done)
    if not observed or not job.review.approved() or not job.with_prs:
        raise ValueError("publication has no reviewed, observed spec outcome on its PR candidate")
    targets = []
    for c in job.checkouts:
        head = c.lease if c.pr else util.git(["rev-parse", "HEAD"], c.wt).strip()
        tree = util.git(["rev-parse", f"{head}^{{tree}}"], c.wt).strip() if SHA.fullmatch(head or "") else ""
        if (not SHA.fullmatch(tree) or tree != trees.get(c.prefix)
                or c.pr and gh.pr_repo(c.pr).casefold() != c.github.casefold()):
            raise ValueError("a release checkout does not match the verified spec tree")
        targets.append({"pr": c.pr, "repo": c.github, "base": c.base, "tree": tree, "head": head})
    return {"definition": done, "source": source_version(job.ticket), "policy": policy, "acceptance": acceptance,
            "landing": landing_policy(job.run.tracker), "targets": targets, "behavior": observed}


def revision(repo: str, ref: str) -> tuple[str, str]:
    row = gh.gh_json(["api", f"repos/{repo}/commits/{quote(ref, safe='')}"]) or {}
    sha, tree = row.get("sha", ""), ((row.get("commit") or {}).get("tree") or {}).get("sha", "")
    if not SHA.fullmatch(sha or "") or not SHA.fullmatch(tree or ""):
        raise gh.GhError(f"GitHub did not identify the commit and tree at {repo}:{ref}")
    return sha, tree


def reached(data: dict) -> list[dict]:
    landed = []
    for target in data["targets"]:
        branch = data["landing"][-1][0] if data["landing"] else target["base"]
        merge = ""
        if target["pr"]:
            pr = gh.pr_view(target["pr"])
            merge = (pr.get("mergeCommit") or {}).get("oid", "")
            if (pr.get("state") != "MERGED" or not pr.get("mergedAt") or not SHA.fullmatch(merge or "")
                    or pr.get("headRefOid") != target["head"] or pr.get("baseRefName") != target["base"]):
                return []
        at, tree = revision(target["repo"], branch)
        if merge and merge != at:
            if not gh.branch_contains(target["repo"], at, merge):
                return []
            if branch == target["base"]:
                at, tree = revision(target["repo"], merge)
                if at != merge:
                    return []
        landed.append({**target, "branch": branch, "commit": at, "exact_tree": tree == target["tree"]})
    return landed


def comments(tk: tracker.Tracker, tid: str):
    if isinstance(tk, tracker.GitHubTracker):
        return [(c.get("body") or "", c.get("url") or "", c.get("createdAt") or "")
                for c in (gh.issue(tk.repo.github, int(tid)) or {}).get("comments", []) if tk.by_publisher(c, True)]
    if isinstance(tk, tracker.JiraTracker):
        me = tk.client.me().get("accountId")
        return [(c.get("body") or "", tk.client.comment_url(tid, c["id"]), c.get("created") or "")
                for c in tk.client.comments(tid) if me and (c.get("author") or {}).get("accountId") == me]
    return []


def save(claim: dict, *, state: str = "") -> None:
    after = state or claim["state"]
    if not specs.advance(claim["identity"], claim["run"], claim["state"], after, claim["data"]):
        raise tracker.TaskError("the spec delivery receipt changed")
    claim["state"] = after


def post_once(claim: dict, tk: tracker.Tracker, kind: str, body: str) -> str:
    data, tid = claim["data"], claim["issue"]
    notes = data.setdefault("notes", {})
    marker = "Arete spec receipt " + orchestration.digest([claim["root"], claim["run"], kind])[:32]
    if notes.get(kind):
        return notes[kind]
    if kind in notes:
        if found := next((url for text, url, _ in comments(tk, tid) if marker in text.splitlines() and url), ""):
            notes[kind] = found
            save(claim)
            return found
        raise tracker.TaskError(f"the spec {kind} comment has no confirmed receipt yet; reconcile it before resending")
    text = outbound.inert(outbound.screen(vault.hidden(f"{tracker.ARETE_TAG} {body}\n\n{marker}")))
    notes[kind] = ""
    save(claim)
    try:
        if isinstance(tk, tracker.GitHubTracker):
            url = gh.issue_comment(tk.repo.github, int(tid), text, return_url=True, single_attempt=True)
        else:
            url = tk.client.comment(tid, text, return_url=True, single_attempt=True)
    except (gh.GhUnsure, jira.JiraUnsure):
        raise
    except (gh.GhError, jira.JiraError):
        notes.pop(kind)
        save(claim)
        raise
    expected = (gh.issue_url(tk.repo.github, tid) + "#issuecomment-" if isinstance(tk, tracker.GitHubTracker)
                else tk.client.browse_url(tid) + "?focusedCommentId=")
    if not url or not url.casefold().startswith(expected.casefold()):
        raise tracker.TaskError(f"the tracker did not confirm the spec {kind} comment")
    notes[kind] = url
    save(claim)
    return url


def location(landed: list[dict]) -> str:
    return "\n".join(f"- `{t['repo']}:{t['branch']}` at `{t['commit']}`" + (f" ({t['pr']})" if t["pr"] else "")
                     for t in landed)


def public_text(tk: tracker.Tracker, text: str) -> str:
    return outbound.redact(text) if tk.repo.public else text


def at_delivery(claim: dict, tk: tracker.Tracker) -> dict:
    data = claim["data"]
    if data.get("graph"):
        from . import spec_graph_delivery
        return spec_graph_delivery.integrated(tk, claim)
    landed = reached(data)
    if not landed:
        return {}
    done = data["definition"]
    if release_proof.required(tk.repo.name, claim["issue"]):
        if len(landed) != 1 or landed[0]["repo"].casefold() != tk.repo.github.casefold():
            return {}
        proof = release_proof.evidence(tk.repo.name, claim["issue"], landed[0]["commit"])
        if not proof:
            return {}
        return {"method": "protected release observation", "landed": landed, "how": f"Observed {proof['path']} "
                f"on {proof['destination']} at {proof['revision']}", "saw": f"All protected assertions matched. "
                f"Receipt {proof['receipt_id']}."}
    if not done.get("environment") and all(t["exact_tree"] for t in landed):
        return {"method": "verified candidate tree matches the landed tree", "landed": landed, **data["behavior"]}
    if not (request := data.get("observation_request")):
        token = orchestration.digest([claim["root"], claim["run"], landed])[:24]
        request = data["observation_request"] = {"landed": landed, "token": token, "since": util.iso()}
        save(claim)
    where = public_text(tk, done.get("environment") or "the landed revision(s) below")
    definition_text = public_text(tk, describe(done))
    post_once(claim, tk, "observation", f"{definition_text}\n\nCandidate verification is not proof at {where}. "
              f"Please observe the outcome there for these landed revisions:\n\n{location(request['landed'])}\n\n"
              f"Reply with `Spec evidence {request['token']}` on its own first line, followed by `How:` and `Saw:` "
              "lines describing how you checked it and what matched the expected outcome. Do not include secrets.")
    for body, url, created in comments(tk, claim["issue"]):
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if (len(lines) != 3 or lines[0] != f"Spec evidence {request['token']}" or not url or not created
                or jira.parse_time(created) <= util.parse_iso(request["since"])):
            continue
        has_how, has_saw = lines[1].startswith("How:"), lines[2].startswith("Saw:")
        how, saw = (line.partition(":")[2].strip()[:2000] for line in lines[1:])
        if has_how and has_saw and how and saw:
            return {"method": "publisher's delivery-point observation", "landed": request["landed"],
                    "how": how, "saw": saw, "source": url}
    return {}


def settle(cfg, repo, claim: dict) -> None:
    tid, run, data = claim["issue"], claim["run"], claim["data"]
    if (runs.run_get(run) or {}).get("status") in (None, "running"):
        return
    tk = tracker.for_ticket(cfg, repo, tid)
    t = tk.get(tid)
    if (t.spec_identity != claim["identity"] or tracker.spec_blocker(cfg, tk, t, require_open=False)
            or source_version(t) != data["source"] or landing_policy(tk) != data["landing"]):
        return
    if claim["state"] == "published":
        observed = data.get("delivery") or at_delivery(claim, tk)
        if not observed:
            return
        data["delivery"] = observed
        save(claim)
        source = f"\n\nObservation: {observed['source']}" if observed.get("source") else ""
        definition_text = public_text(tk, describe(data["definition"]))
        outcome_text = public_text(tk, f"How: {observed['how']}\n\nSaw: {observed['saw']}")
        post_once(claim, tk, "delivery", f"{definition_text}\n\n## Delivery evidence\n\n"
                  f"Reviewed and verified in run `{run}` (spec run `{claim['root']}`). "
                  f"Evidence at delivery: {observed['method']}.\n\n{location(observed['landed'])}\n\n"
                  f"{outcome_text}{source}")
        save(claim, state="evidenced")
    if t.open:
        if isinstance(tk, tracker.GitHubTracker):
            gh.issue_close(repo.github, int(tid))
        elif data["landing"]:
            if error := tk.shift(tid, data["landing"][-1][1], keep_done=True).error:
                raise tracker.TaskError(error)
    save(claim, state="complete")


def sync(cfg) -> None:
    for pending in specs.pending():
        repo_name, tid = pending["repo"], pending["issue"]
        with workspaces.ticket_lock(repo_name, tid) as got:
            if not got:
                continue
            claim = specs.find(repo_name, tid)
            if claim.get("state") not in ("published", "evidenced"):
                continue
            try:
                repo = cfg.repo(repo_name)
                settle(cfg, repo, claim)
            except (KeyError, ValueError, OSError, gh.GhError, jira.JiraError, tracker.TaskError) as error:
                print(f"{util.iso()} {tracker.issue_name(repo_name, tid)}: spec delivery held: "
                      f"{outbound.sanitize(str(error), 300)}")
