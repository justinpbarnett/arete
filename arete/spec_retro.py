"""Review one published parent spec with evidence from its own delivery runs."""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from dataclasses import replace

from . import emits, gh, jira, loops, miner, outbound, paths, record, sources, tracker, util
from .store import insights, runs as run_store


GITHUB_SPECS = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    issues(labels: ["spec"], states: CLOSED, first: 100, after: $endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes { number title body url closedAt parent { id } author { login } }
    }
  }
}
"""


def closed_event(sid, repo, ref, title, body, url, closed_at):
    key = f"{repo.github.lower()}#{ref}" if repo.tracker == "github" else ref
    return sources.event("spec.closed", sid, key, title, repo=repo.name, url=url, trust="internal",
                         data={"tracker": repo.tracker, "ref": ref, "at": closed_at,
                               "description": (body or "")[:12000]})


def github_specs(repos, sid):
    login = gh.me().casefold()
    if not login:
        raise gh.GhError("Cannot identify the publisher of GitHub specs")
    for repo in repos:
        owner, name = repo.github.split("/", 1)
        pages = gh.gh_json(["api", "graphql", "--paginate", "--slurp",
                            "-f", gh.outgoing(f"query={GITHUB_SPECS}"),
                            "-f", gh.outgoing(f"owner={owner}"), "-f", gh.outgoing(f"name={name}")])
        for page in pages:
            for issue in page["data"]["repository"]["issues"]["nodes"]:
                if issue["parent"] or (issue["author"] or {}).get("login", "").casefold() != login:
                    continue
                yield closed_event(sid, repo, str(issue["number"]), issue["title"], issue["body"], issue["url"],
                                   issue["closedAt"])


def jira_specs(repos, sid, cfg):
    projects = {}
    for repo in repos:
        project = repo.jira_project or cfg.jira.project
        if not project:
            raise jira.JiraError(f"Missing Jira project for {repo.name}")
        projects.setdefault(project, []).append(repo)
    for project, targets in projects.items():
        client = tracker.JiraTracker(targets[0], cfg.jira).client
        jql = f'project = {json.dumps(project)} AND creator = currentUser() AND labels = "spec" AND statusCategory = Done'
        rows = client.search(jql, ["summary", "description", "issuetype", "labels", "parent",
                                   "statuscategorychangedate"], limit=sys.maxsize)
        parent_labels = {}
        for row in rows:
            fields = row["fields"]
            if fields["issuetype"].get("subtask"):
                continue
            parent = (fields.get("parent") or {}).get("key")
            if parent:
                if parent not in parent_labels:
                    parent_labels[parent] = client.issue(parent, ["labels"])["fields"].get("labels") or []
                if "spec" in parent_labels[parent]:
                    continue
            labels = set(fields.get("labels") or [])
            routed = [r for r in targets if tracker.route_label(r) in labels]
            if not routed and len(targets) == 1 and not any(label.startswith("repo-") for label in labels):
                routed = targets
            if len(routed) != 1:
                continue
            key = row["key"]
            yield closed_event(sid, routed[0], key, fields["summary"], fields["description"], client.browse_url(key),
                               fields.get("statuscategorychangedate") or "")


def closed_specs(cfg, kind, sid):
    repos = [r for r in cfg.repos if r.pipeline and r.mine and r.tracker == kind]
    if kind == "github":
        repos = [r for r in repos if r.github]
        return list(github_specs(repos, sid)) if repos else []
    return list(jira_specs(repos, sid, cfg))


def require_source(cfg, repo, event):
    ref = event["data"]["ref"]
    if repo.tracker == "jira":
        if (not jira.KEY_RE.fullmatch(ref) or ref.split("-", 1)[0] != (repo.jira_project or cfg.jira.project)
                or event["url"] != tracker.JiraTracker(repo, cfg.jira).client.browse_url(ref)):
            raise ValueError("Jira spec moved outside its configured project or site")
    elif (event["key"] != f"{repo.github.casefold()}#{int(ref)}"
          or event["url"].casefold() != gh.issue_url(repo.github, int(ref)).casefold()):
        raise ValueError("GitHub spec moved outside its configured repository")


def ticket_scopes(cfg, repo, event):
    ref = event["data"]["ref"]
    scopes = [(repo.name, ref)]
    eligible = [r for r in cfg.repos if r.pipeline and r.mine and r.tracker == repo.tracker]
    if repo.tracker == "jira":
        client = tracker.JiraTracker(repo, cfg.jira).client
        for row in client.search(f'parent = "{ref}"', ["labels"], limit=sys.maxsize):
            project = row["key"].split("-", 1)[0]
            labels = set(row["fields"].get("labels") or [])
            candidates = [r for r in eligible if (r.jira_project or cfg.jira.project) == project]
            routed = [r for r in candidates if tracker.route_label(r) in labels]
            if len(routed) == 1:
                scopes.append((routed[0].name, row["key"]))
            elif not routed and not any(label.startswith("repo-") for label in labels):
                # Jira keys identify a ticket across a project. Query matching configured repos for its actual runs.
                scopes.extend((r.name, row["key"]) for r in candidates)
    else:
        number = int(ref)
        by_slug = {r.github.casefold(): r.name for r in eligible}
        for child in gh.api_list(f"repos/{repo.github}/issues/{number}/sub_issues"):
            slug = child["repository_url"].rsplit("/repos/", 1)[-1].casefold()
            if slug in by_slug:
                scopes.append((by_slug[slug], str(child["number"])))
    return list(dict.fromkeys(scopes))


# Only these engine-owned names and counts may cross from a private repo to the factory's review.
STAGES = ("plan", "build", "check", "verify", "review", "repair", "publish", "orchestrate", "delegate", "document")


def observations(cfg, repo, event):
    scopes = ticket_scopes(cfg, repo, event)
    runs, product, signals, failure_stages, repeated_stages = [], [], Counter(), Counter(), Counter()
    review_count = 0
    for name, ref in scopes:
        reviews = insights.ticket_feedback(name, ref)
        review_count += len(reviews)
        if name == repo.name and reviews:
            product.append(miner.render_feedback(reviews)[0])
        for row in run_store.ticket_runs(name, ref, limit=sys.maxsize):
            if row.get("dry_run") or not row.get("finished"):
                continue
            run = record.read_run(row["id"])
            if run is None:
                continue
            signals.update(miner.signals_of(run))
            for stage in STAGES:
                calls = [c for c in run["calls"] if c["step"] == stage]
                if len(calls) > 1:
                    repeated_stages[stage] += 1
                if any(not c["ok"] for c in calls) or any(f["step"] == stage for f in run["failed_steps"]):
                    failure_stages[stage] += 1
            runs.append({"session": run["id"], "issue": ref})
            if name == repo.name:
                digest = miner.run_digest(run)
                product.append("\n".join(digest["items"]))
    totals = {"finished_runs": len(runs), "owner_review_comments": review_count, "signals": dict(signals),
              "runs_with_failed_stage": dict(failure_stages), "runs_with_repeated_stage": dict(repeated_stages)}
    evidence, included = miner.fit_blocks(product, cfg.miner_max_digest_chars)
    return runs, totals, outbound.redact(outbound.screen(evidence)), len(product) - included


def review(run, event, folder, schema):
    repo = run.cfg.repo(event["repo"])
    if not repo.mine or repo.tracker != event["data"]["tracker"] or not repo.pipeline:
        return []
    require_source(run.cfg, repo, event)
    # A failed or interrupted issue publication may requeue this event with a new run ID.
    # Replay exactly the previously accepted findings, including an empty successful retro.
    identity = [event["source"], repo.name, event["key"], event["url"]]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    saved = paths.STATE_DIR / "spec-retro" / f"{key}.json"
    with util.lock(f"spec-retro-{key}", wait=True):
        if saved.is_file():
            snapshot = json.loads(saved.read_text())
            for name, destination in snapshot["destinations"].items():
                if destination != emits.configured_ticket_target(run.cfg, name) or not run.cfg.repo(name).mine:
                    raise ValueError("A retrospective finding's destination changed since the review")
            return snapshot["findings"]
        findings = assess(run, event, repo, folder, schema)
        if not run.trial:
            names = {f["repo"] for f in findings}
            targets = {n: emits.configured_ticket_target(run.cfg, n) for n in names}
            snapshot = {"spec": identity, "findings": findings, "destinations": targets}
            util.write_durably(saved, json.dumps(snapshot).encode(), mode=0o600)
        return findings


def assess(run, event, repo, folder, schema):
    factory = next((r for r in run.cfg.repos if r.name == run.cfg.arete_repo and r.mine), None)
    public = repo.name == run.cfg.arete_repo or repo.public or (
        repo.tracker == "github" and gh.repo_private(repo.github) is not True)
    rows, totals, evidence, omitted = observations(run.cfg, repo, event)
    if public:
        for _, pattern in miner.private_patterns(run.cfg, repo, rows):
            evidence = pattern.sub("[redacted]", evidence)
    product = {"spec": event["title"], "description": event["data"]["description"], "delivery": totals,
               "recorded_observations": evidence, "omitted_evidence_blocks": omitted}
    jobs = [(repo, "product", product)]
    if factory and repo.name != factory.name and (totals["signals"] or totals["owner_review_comments"]):
        # This fresh call receives fixed stage/signal names and counts, never private text or identifiers.
        jobs.append((factory, "factory", totals))
    findings = []
    for target, role, material in jobs:
        prompt = (f"{loops.prompt_text(run.loop)}\nReview the {role}.\n\n"
                  f"<evidence>\n{json.dumps(material, ensure_ascii=False)}\n</evidence>")
        scoped = replace(run, loop=replace(run.loop, repo=target.name))
        working = folder / role
        working.mkdir(parents=True, exist_ok=True)
        res = scoped.agent(f"retro-{role}", prompt, working, tools=[], instructions=[], mcp_config="", add_dirs=[],
                           json_schema=schema)
        if not res.ok or not isinstance(res.structured, dict) or not isinstance(res.structured.get("findings"), list):
            raise RuntimeError(f"The {role} retrospective did not return findings")
        for finding in res.structured["findings"]:
            keys = ("title", "summary", "fingerprint")
            if not isinstance(finding, dict) or not all(isinstance(finding.get(k), str) and finding[k].strip()
                                                        for k in keys):
                raise ValueError("A retrospective finding needs a title, summary and fingerprint")
            # Discard model-chosen destinations and any optional fields that could carry unreviewed output.
            clean = {k: outbound.redact(outbound.screen(finding[k])) for k in keys}
            if public or role == "factory":
                public_target = replace(target, public=True)
                wording = {"change": clean["title"], "why": f"{clean['summary']}\n{clean['fingerprint']}"}
                if reason := miner.public_wording_refusal(run.cfg, public_target, wording, rows):
                    raise ValueError(f"Public retrospective finding {reason}")
            clean["repo"] = target.name
            clean["fingerprint"] = f"{target.name}/{clean['fingerprint']}"
            if role == "product":
                clean["evidence"] = [event["url"]]
            findings.append(clean)
    return findings
