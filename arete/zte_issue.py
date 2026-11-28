from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import gh, pilot, tracker, util

if TYPE_CHECKING:
    from .zte_github import GitHub, Identity

LABEL = tracker.ZTE_LABEL
LABELS = frozenset({LABEL, "documentation"})
MAX_BODY_BYTES = 16 * 1024
ISSUE_PROOF = """
fragment RepositoryProof on Repository {
  databaseId id nameWithOwner owner { ...ActorId }
  defaultBranchRef { name }
}
fragment IssueProof on Issue {
  id number state stateReason closedAt
  blockedBy(first: 1) { totalCount }
  subIssues(first: 1) { totalCount }
  timelineItems(last: 1, itemTypes: [CLOSED_EVENT, REOPENED_EVENT]) {
    nodes {
      __typename
      ... on ClosedEvent {
        createdAt actor { ...ActorId }
        closer {
          __typename
          ... on PullRequest { id number mergeCommit { oid } }
          ... on Commit { oid }
        }
      }
    }
  }
}
fragment ActorId on Actor {
  ... on User { databaseId }
  ... on Bot { databaseId }
}
"""
ISSUE_QUERY = """
query ($owner: String!, $repo: String!, $issue: Int!) {
  repository(owner: $owner, name: $repo) {
    ...RepositoryProof
    issue(number: $issue) { ...IssueProof }
  }
}
""" + ISSUE_PROOF
CLOSING_QUERY = """
query ($owner: String!, $repo: String!, $issue: Int!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    ...RepositoryProof
    issue(number: $issue) { ...IssueProof }
    pullRequest(number: $pr) {
      id number body merged mergedAt mergedBy { ...ActorId }
      baseRefName headRefOid mergeCommit { oid } isCrossRepository
      closingIssuesReferences(first: 2) { totalCount nodes { id number } }
    }
  }
}
""" + ISSUE_PROOF


@dataclass(frozen=True)
class Claim:
    number: int
    node: str
    revision: str


def require_number(value: int) -> None:
    pilot.require(type(value) is int and 0 < value < 2**31, "invalid GitHub issue or PR number")


def issue_content(identity: Identity, number: int, row: dict) -> tuple:
    require_number(number)
    pilot.require(row.get("number") == number and "pull_request" not in row
                  and row.get("repository_url") == f"https://api.github.com/repos/{identity.name}"
                  and type(row.get("node_id")) is str and bool(row["node_id"])
                  and (row.get("user") or {}).get("id") == identity.owner,
                  "GitHub did not return the owner-authored issue in the approved repository")
    pilot.require(row.get("locked") is False and row.get("comments") == 0,
                  "the issue is locked or has discussion outside its pinned revision")
    raw_labels = row.get("labels")
    pilot.require(isinstance(raw_labels, list) and all(isinstance(label, dict)
                  and type(label.get("name")) is str for label in raw_labels), "GitHub did not return the issue labels")
    labels = sorted(label["name"] for label in raw_labels)
    pilot.require(LABEL in labels and set(labels) <= LABELS and len(set(labels)) == len(labels),
                  "the issue is not in the dedicated delivery queue")
    title, body = (row.get(key) for key in ("title", "body"))
    pilot.require(type(title) is str and 0 < len(title.encode()) <= 512
                  and type(body) is str and 0 < len(body.encode()) <= MAX_BODY_BYTES,
                  "the issue text is missing or exceeds the delivery bound")
    pilot.require(not tracker.github_waits(body, identity.name), "this lane does not admit issues with dependencies")
    return identity.number, identity.node, identity.owner, number, row["node_id"], title, body, labels


def digest(fields: tuple) -> str:
    return hashlib.sha256(json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def admit(identity: Identity, number: int, row: dict) -> Claim:
    fields = issue_content(identity, number, row)
    updated = row.get("updated_at")
    pilot.require(row.get("state") == "open", "the issue is not open")
    pilot.require(type(updated) is str and len(updated) == 20 and updated.endswith("Z"),
                  "the issue revision is missing or exceeds the delivery bound")
    try:
        util.parse_iso(updated)
    except ValueError as error:
        raise pilot.Held("GitHub returned an invalid issue timestamp") from error
    return Claim(number, row["node_id"], digest((*fields, updated)))


def current(client: GitHub, claim: Claim, *, require_fresh: bool = False) -> str:
    require_number(claim.number)
    if require_fresh:
        require_closable(require_issue(client.identity, claim, client.issue_snapshot(claim.number)))
    row = client.read(f"/issues/{claim.number}")
    pilot.require(admit(client.identity, claim.number, row) == claim, "the approved GitHub issue revision changed")
    return digest(issue_content(client.identity, claim.number, row))


def closing_keyword(claim: Claim) -> str:
    require_number(claim.number)
    return f"Closes #{claim.number}"


def squash_title(pr_number: int) -> str:
    require_number(pr_number)
    return f"{pilot.TASK_TITLE} (#{pr_number})"


def reference(claim: Claim, *, explicit: bool = False) -> str:
    require_number(claim.number)
    return f"Approved issue: #{claim.number}" if explicit else closing_keyword(claim)


def pr_body(claim: Claim, *, explicit: bool = False) -> gh.Outgoing:
    return gh.outgoing(gh.marked("Verified one-line README correction."), link=reference(claim, explicit=explicit))


def require_issue(identity: Identity, claim: Claim, repo: dict) -> dict:
    require_number(claim.number)
    pilot.require((repo.get("databaseId"), repo.get("id"), repo.get("nameWithOwner"))
                  == (identity.number, identity.node, identity.name)
                  and repo.get("owner") == {"databaseId": identity.owner}
                  and repo.get("defaultBranchRef") == {"name": "main"},
                  "GitHub did not return the approved repository and default branch")
    issue = repo.get("issue") or {}
    pilot.require((issue.get("number"), issue.get("id")) == (claim.number, claim.node), "the GitHub issue changed")
    return issue


def binding(identity: Identity, claim: Claim, pr_number: int, pr_node: str, head: str, repo: dict, *,
            explicit: bool = False) -> tuple[dict, dict]:
    require_number(pr_number)
    issue = require_issue(identity, claim, repo)
    pull = repo.get("pullRequest") or {}
    pilot.require((pull.get("number"), pull.get("id"), pull.get("headRefOid")) == (pr_number, pr_node, head)
                  and pull.get("baseRefName") == "main" and pull.get("isCrossRepository") is False,
                  "GitHub did not return the approved PR, head and branch")
    expected = [] if explicit else [{"id": claim.node, "number": claim.number}]
    pilot.require(pull.get("closingIssuesReferences") == {"totalCount": len(expected), "nodes": expected},
                  "the PR closing references differ from the approved issue policy")
    return issue, pull


def require_link(identity: Identity, claim: Claim, pr_number: int, pr_node: str, head: str, repo: dict, *,
                 explicit: bool = False) -> None:
    issue, pull = binding(identity, claim, pr_number, pr_node, head, repo, explicit=explicit)
    pilot.require(issue.get("state") == "OPEN" and pull.get("merged") is False
                  and pull.get("body") == str(pr_body(claim, explicit=explicit)),
                  "the pre-merge issue or exact PR body changed")
    if explicit:
        require_closable(issue)


def require_merged(identity: Identity, claim: Claim, pr_number: int, pr_node: str, head: str, release: str,
                   repo: dict, *, explicit: bool = False) -> tuple[dict, dict]:
    issue, pull = binding(identity, claim, pr_number, pr_node, head, repo, explicit=explicit)
    pilot.require(pull.get("merged") is True and pull.get("mergedBy") == {"databaseId": identity.bot}
                  and pilot.SHA.fullmatch(release) and pull.get("mergeCommit") == {"oid": release}
                  and pull.get("mergedAt") and pull.get("body") == str(pr_body(claim, explicit=explicit)),
                  "GitHub did not report the approved squash and actor")
    return issue, pull


def require_closable(issue: dict) -> None:
    pilot.require(issue.get("state") == "OPEN" and issue.get("closedAt") is None
                  and issue.get("timelineItems") == {"nodes": []},
                  "the issue no longer has a fresh open lifecycle")
    require_no_dependencies(issue)


def require_no_dependencies(issue: dict) -> None:
    pilot.require(all(issue.get(field) == {"totalCount": 0} for field in ("blockedBy", "subIssues")),
                  "GitHub did not confirm that the issue has no prerequisites or subissues")


def require_completed(identity: Identity, claim: Claim, pr_number: int, pr_node: str, head: str, release: str,
                      repo: dict, *, sent_after: str = "", close_actor: int | None = None) -> dict:
    pilot.require((not sent_after and close_actor is None) or (sent_after and type(close_actor) is int
                  and close_actor > 0 and close_actor not in (identity.bot, identity.owner)),
                  "the issue close actor does not match the authorized mode")
    issue, pull = require_merged(identity, claim, pr_number, pr_node, head, release, repo, explicit=bool(sent_after))
    actor = {"databaseId": close_actor if sent_after else identity.bot}
    closed, merged = issue.get("closedAt"), pull["mergedAt"]
    pilot.require((issue.get("state"), issue.get("stateReason")) == ("CLOSED", "COMPLETED") and closed
                  and util.parse_iso(closed) >= util.parse_iso(merged),
                  "GitHub did not report a completed issue after the squash")
    events = (issue.get("timelineItems") or {}).get("nodes")
    pilot.require(isinstance(events, list) and len(events) == 1, "GitHub did not report the latest close or reopen event")
    event = events[0]
    pull_closer = {"__typename": "PullRequest", "id": pr_node, "number": pr_number, "mergeCommit": {"oid": release}}
    commit_closer = {"__typename": "Commit", "oid": release}
    pilot.require(event.get("__typename") == "ClosedEvent" and event.get("actor") == actor
                  and event.get("createdAt") and util.parse_iso(event["createdAt"]) >= util.parse_iso(closed),
                  "the latest issue event is not the approved App's close")
    if sent_after:
        require_no_dependencies(issue)
        pilot.require("closer" in event and event["closer"] is None
                      and util.parse_iso(closed) >= util.parse_iso(sent_after),
                      "the completed issue does not match the broker's close intent")
    else:
        pilot.require(event.get("closer") in (pull_closer, commit_closer),
                      "the latest issue close was not caused by the exact squash PR or its commit")
    return {"issue": claim.number, "issue_node": claim.node, "pr": pr_number, "pr_node": pr_node,
            "head": head, "release": release, "actor": actor["databaseId"], "closed_at": closed,
            "closed_event_at": event["createdAt"], **({"close_sent_after": sent_after} if sent_after else {})}
