"""Identify the accepted tracker text independently of automation activity."""
from __future__ import annotations

import re

from . import gh, tracker
from .store import orchestration as ledger


def version(ticket: tracker.Ticket) -> str:
    # The title/body-only representation also reads existing spec publication receipts.
    comments = ticket.owner_comments if ticket.owner_comments is not None else ticket.comments
    return ledger.digest([ticket.title, ticket.body or "(no description)", *([comments] if comments else [])])


CRITERION = re.compile(r"^\s*(?:[-*]\s*)?(?:\[[ xX]\]\s*)?(AC-\d+)\s*[:—-]\s*.+$")


def independent_workers(before, after, workers):
    """Keep a private duty only when all edits are confined to other named criteria."""
    if before.title != after.title or before.owner_comments != after.owner_comments:
        return set()

    def split(body):
        shared, criteria = [], {}
        for line in body.splitlines():
            match = CRITERION.fullmatch(line)
            if not match:
                shared.append(line)
            elif match[1] in criteria:
                return [], {}
            else:
                criteria[match[1]] = line.strip()
        return shared, criteria

    old_shared, old = split(before.body)
    new_shared, new = split(after.body)
    if old_shared != new_shared or not old or not new:
        return set()
    unaffected = {k for k in old.keys() & new.keys() if old[k] == new[k]}
    keep = set()
    for wid, worker in workers.items():
        assignment = worker["data"]["effective"]
        check = assignment.get("acceptance_check", "").strip()
        if (check in unaffected or any(check == old[k] for k in unaffected)) and not assignment.get("depends_on"):
            keep.add(wid)
    return keep


def github_conflict(tk: tracker.GitHubTracker, ticket: tracker.Ticket) -> str:
    """Bind the newly read title, body and comments to their content editors."""
    owner, repo = tk.repo.github.split("/")
    query = """query($owner:String!,$repo:String!,$number:Int!,$endCursor:String) {
      repository(owner:$owner,name:$repo) { issue(number:$number) {
        title body author {login} editor {login} lastEditedAt
        timelineItems(last:1,itemTypes:[RENAMED_TITLE_EVENT]) {
          nodes {... on RenamedTitleEvent {currentTitle actor {login}}}
        }
        comments(first:100,after:$endCursor) {nodes {body createdAt lastEditedAt author {login} editor {login}}
          pageInfo {hasNextPage endCursor}}
      }}
    }"""
    pages = gh.gh_json(["api", "graphql", "--paginate", "--slurp", "-f", gh.outgoing(f"query={query}"),
                        "-f", gh.outgoing(f"owner={owner}"),
                        "-f", gh.outgoing(f"repo={repo}"), "-F", gh.outgoing(f"number={int(ticket.id)}")]) or []
    publisher = (gh.me() if ticket.is_spec else tk.owner).casefold()

    def authorized(actor):
        return bool(publisher and (actor or {}).get("login", "").casefold() == publisher)

    comments = []
    for page in pages:
        item = ((page.get("data") or {}).get("repository") or {}).get("issue")
        if (not item or not authorized(item.get("author")) or item["title"].strip() != ticket.title
                or (item["body"] or "(no description)") != ticket.body):
            return "the issue changed while the new requirements were being read"
        renamed = item["timelineItems"]["nodes"]
        if renamed and (renamed[0]["currentTitle"] != item["title"] or not authorized(renamed[0]["actor"])):
            return "the tracker does not identify the authorized owner as the editor of the current title"
        if item["lastEditedAt"] and not authorized(item["editor"]):
            return "the tracker does not identify the authorized owner as the editor of the current description"
        for c in item["comments"]["nodes"]:
            if authorized(c.get("author")) and c["body"].strip() and not tracker.from_arete(c["body"]) and not gh.has_arete_mark(c["body"]):
                comments.append(c)
    if not pages or any(c["lastEditedAt"] and not authorized(c["editor"]) for c in comments[-tracker.MAX_COMMENTS:]):
        return "the tracker could not confirm the authorship of the current requirement comments"
    expected = tracker.scope_comments([(c["author"]["login"] if ticket.is_spec else tk.owner, c["createdAt"], c["body"])
                                       for c in comments])
    if expected != ticket.owner_comments:
        return "the owner comments changed while the new requirements were being read"
    return ""
