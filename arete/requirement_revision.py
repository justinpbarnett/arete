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


def criteria(body):
    shared, named = [], {}
    for line in body.splitlines():
        match = CRITERION.fullmatch(line)
        if not match:
            shared.append(line)
        elif match[1] in named:
            return [], {}
        else:
            named[match[1]] = line.strip()
    return shared, named


def unchanged_criteria(before, after):
    old_shared, old = criteria(before.body)
    new_shared, new = criteria(after.body)
    if before.title != after.title or before.owner_comments != after.owner_comments or old_shared != new_shared:
        return {}
    return {key: line for key, line in old.items() if new.get(key) == line}


def prior_answers(before, after, notes):
    if version(before) == version(after):
        return list(notes), [], []
    unchanged = unchanged_criteria(before, after)
    _, previous = criteria(before.body)
    _, current = criteria(after.body)
    supersedes = re.compile(r"\b(?:supersedes?|replaces?|overrides?) (?:the )?(?:earlier|previous|prior|old) "
                            r"(?:owner(?:'s)? )?(?:answer|decision|clarification)s?\b", re.I)
    replaced = {key for key, line in current.items() if key in previous and line != previous[key]
                and supersedes.search(line)}
    retained, retired, unresolved = [], [], []
    for note in notes:
        keys = set(re.findall(r"\bAC-\d+\b", note))
        if keys and keys <= unchanged.keys():
            retained.append(note)
        elif keys and keys <= replaced:
            retired.append(note)
        else:
            unresolved.append(note)
    return retained, retired, unresolved


def independent_workers(before, after, workers):
    """Keep a private duty only when all edits are confined to other named criteria."""
    unaffected = unchanged_criteria(before, after)
    keep = set()
    for wid, worker in workers.items():
        assignment = worker["data"]["effective"]
        check = assignment.get("acceptance_check", "").strip()
        if (check in unaffected or check in unaffected.values()) and not assignment.get("depends_on"):
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
