"""Cross-repo spec authorization, peer linking, and prerequisite coordination."""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from . import config, flows, gh, paths, release_proof, tracker, util, verify
from .store import runs, specs

AUTHORIZED_REPOS_LINE = re.compile(
    r"^(?:authorized|target)[ \t]+repos(?:itories)?[ \t]*:[ \t]*(.+)$", re.IGNORECASE
)
PEER_LINE = re.compile(
    r"^(linked|requires)[ \t]+spec[ \t]*:[ \t]*(.+)$", re.IGNORECASE
)
ARTIFACT_RE = re.compile(
    r"artifact=(@?[A-Za-z0-9][A-Za-z0-9._/-]{0,127}):([0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z.-]+)?)"
)
GH_URL_RE = re.compile(
    r"^https://github\.com/([a-z0-9-]+/[a-z0-9_.-]+)/issues/([1-9][0-9]*)/?$",
    re.IGNORECASE | re.ASCII,
)
GH_REF_RE = re.compile(
    r"^(?:([a-z0-9-]+/[a-z0-9_.-]+))?#([1-9][0-9]*)$",
    re.IGNORECASE | re.ASCII,
)
JIRA_URL_RE = re.compile(r"^https?://[^/\s]+/browse/([A-Z][A-Z0-9_]+-[1-9][0-9]*)/?$", re.IGNORECASE)
JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-[1-9][0-9]*$", re.IGNORECASE)
SPEC_LINKS_FILE = "spec-links.json"


def _breakdown_lines(body: str):
    from . import spec_graph

    return list(spec_graph.breakdown_lines(body or ""))


def resolve_repo(cfg: config.Config | None, token: str) -> config.Repo | None:
    if cfg is None or not token:
        return None
    low = token.strip().strip("`").casefold()
    for repo in cfg.repos:
        if repo.name.casefold() == low or (repo.github and repo.github.casefold() == low):
            return repo
    return None


def declared_repos(body: str) -> tuple[list[str], str]:
    lines = [m[1].strip() for line in _breakdown_lines(body)
             if (m := AUTHORIZED_REPOS_LINE.fullmatch(line.strip()))]
    if not lines:
        return [], ""
    if len(lines) > 1:
        return [], "use a single Authorized repos: line listing the explicitly authorized repositories"
    raw = [part.strip().strip("`") for part in lines[0].split(",")]
    if not raw or any(not part for part in raw):
        return [], "Authorized repos: must list non-empty comma-separated repository names or slugs"
    seen: set[str] = set()
    cleaned: list[str] = []
    for token in raw:
        low = token.casefold()
        if low in seen:
            return [], f"Authorized repos: repeats repository {token!r}"
        seen.add(low)
        cleaned.append(token)
    return cleaned, ""


def authorized_repos(cfg: config.Config, primary: config.Repo,
                     ticket: tracker.Ticket) -> tuple[list[config.Repo], str]:
    tokens, error = declared_repos(ticket.body)
    if error:
        return [], error
    if not tokens:
        return [primary], ""
    resolved: list[config.Repo] = []
    seen_names: set[str] = set()
    for token in tokens:
        repo = resolve_repo(cfg, token)
        if repo is None:
            return [], f"authorized repository {token!r} is not enrolled in config.toml"
        if repo.name in seen_names:
            return [], f"Authorized repos: repeats repository {repo.name!r}"
        if not repo.pipeline or not repo.parts:
            return [], f"authorized repository {repo.name} needs a configured pipeline checkout and PR target"
        seen_names.add(repo.name)
        resolved.append(repo)
    if primary.name not in seen_names:
        return [], f"Authorized repos: must include the spec's own repository {primary.name}"
    return resolved, ""


def check_repo_scope(cfg: config.Config, primary: config.Repo, ticket: tracker.Ticket,
                     targets: Iterable[str | Path | config.Repo]) -> tuple[list[config.Repo], list[str]]:
    authorized, error = authorized_repos(cfg, primary, ticket)
    if error:
        authorized = [primary]
    allowed_names = {r.name.casefold() for r in authorized}
    allowed_slugs = {r.github.casefold() for r in authorized if r.github}
    for r in authorized:
        allowed_slugs.update(p.github.casefold() for p in r.parts if p.github)
    allowed_paths = [r.path.resolve() for r in authorized]

    unauthorized: list[str] = []
    seen_bad: set[str] = set()

    def note(label: str) -> None:
        key = label.casefold()
        if key not in seen_bad:
            seen_bad.add(key)
            unauthorized.append(label)

    for target in targets:
        if isinstance(target, config.Repo):
            if target.name.casefold() not in allowed_names:
                note(target.name)
            continue
        if isinstance(target, Path):
            resolved = target.resolve()
            if any(resolved == root or resolved.is_relative_to(root) for root in allowed_paths):
                continue
            matched = next((r.name for r in cfg.repos
                            if resolved == r.path.resolve() or resolved.is_relative_to(r.path.resolve())), "")
            note(matched or str(target))
            continue
        text = str(target).strip()
        if not text:
            continue
        low = text.casefold()
        if low in allowed_names or low in allowed_slugs:
            continue
        if found := resolve_repo(cfg, text):
            if found.name.casefold() not in allowed_names:
                note(found.name)
            continue
        note(text)
    return authorized, unauthorized


def write_path_repos(cfg: config.Config | None, write_paths: Iterable[str]) -> list[str]:
    """Identify enrolled repository names targeted by worker write_paths."""
    if cfg is None or not write_paths:
        return []
    by_name = {r.name.casefold(): r.name for r in cfg.repos}
    by_path = [(r.path.resolve(), r.name) for r in cfg.repos]
    found: list[str] = []
    seen: set[str] = set()
    for raw in write_paths:
        text = str(raw or "").strip()
        if not text:
            continue
        p = Path(text)
        if p.is_absolute():
            resolved = p.resolve()
            for root, name in by_path:
                if resolved == root or resolved.is_relative_to(root):
                    if name.casefold() not in seen:
                        seen.add(name.casefold())
                        found.append(name)
                    break
            continue
        head = text.lstrip("./").split("/", 1)[0].casefold()
        if head in by_name and head not in seen:
            seen.add(head)
            found.append(by_name[head])
    return found


def expansion_ask_request(authorized: list[config.Repo], unauthorized: list[str], *, why: str = "") -> dict:
    current = ", ".join(r.name for r in authorized)
    requested = ", ".join(unauthorized)
    return {
        "questions": [{
            "header": "Repo scope",
            "kind": "choice",
            "question": (
                f"This spec explicitly authorizes changes in {current}. "
                f"Touching {requested} is outside the published repository scope. "
                f"Should Arete expand the spec's authorized repositories to include {requested} "
                f"before making any change there?"
            ),
            "options": [
                {
                    "label": "Keep current authorized repos (recommended)",
                    "description": (
                        f"Recommended. Stop without changing {requested} and keep work within {current}."
                    ),
                },
                {
                    "label": f"Expand scope to include {requested}",
                    "description": (
                        f"Authorize changes in {requested} by updating the spec's Authorized repos declaration."
                    ),
                },
            ],
        }],
        "why": why or f"Work requested changes in unauthorized repository {requested}, outside {current}.",
        "tried": f"Verified target repositories against the spec's explicit authorization ({current}).",
    }


def _links_path() -> Path:
    return paths.STATE_DIR / SPEC_LINKS_FILE


def _read_links() -> dict[str, list[dict]]:
    raw = util.read_json(_links_path(), {})
    return raw if isinstance(raw, dict) else {}


def _link_key(repo: str, issue: str) -> str:
    return f"{repo}:{issue}"


def record_link(repo: str, issue: str, peer_repo: str, peer_issue: str, *,
                url: str = "", peer_url: str = "", role: str = "",
                requires: bool = False, need: str = "merge",
                artifact: dict | None = None) -> dict:
    entry = {
        "repo": repo,
        "issue": str(issue),
        "url": url,
        "peer_repo": peer_repo,
        "peer_issue": str(peer_issue),
        "peer_url": peer_url,
        "role": role,
        "requires": bool(requires),
        "need": "artifact" if artifact else (need or "merge"),
        **({"artifact": dict(artifact)} if artifact else {}),
    }
    key = _link_key(repo, str(issue))
    with util.lock("spec-links", wait=True):
        data = _read_links()
        rows = [row for row in data.get(key, [])
                if (row.get("peer_repo"), str(row.get("peer_issue"))) != (peer_repo, str(peer_issue))]
        rows.append(entry)
        data[key] = rows
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        util.write_json(_links_path(), data)
    return entry


def links_for(repo: str, issue: str) -> list[dict]:
    rows = _read_links().get(_link_key(repo, str(issue)), [])
    return [dict(row) for row in rows if isinstance(row, dict)]


def parse_need(specifier: str) -> tuple[str, dict | None]:
    point = specifier.strip().lstrip("@")
    if not point or point == "merge":
        return "merge", None
    if point == "release":
        return "release", None
    if match := ARTIFACT_RE.fullmatch(point):
        return "artifact", {"name": match[1], "version": match[2]}
    raise ValueError("use @merge, @release or @artifact=package:version for a peer spec prerequisite")


def parse_peer_target(token: str, *, cfg: config.Config | None = None,
                      repo_hint: str = "") -> tuple[str, str, str]:
    """Return (peer_repo_name_or_slug, peer_issue_id, peer_url)."""
    raw = token.strip().strip("`")
    if match := GH_URL_RE.fullmatch(raw):
        slug, number = match[1].casefold(), match[2]
        resolved = resolve_repo(cfg, slug)
        return (resolved.name if resolved else (repo_hint or slug)), number, raw
    if match := GH_REF_RE.fullmatch(raw):
        slug, number = (match[1] or "").casefold(), match[2]
        resolved = resolve_repo(cfg, slug or repo_hint)
        repo_name = resolved.name if resolved else (repo_hint or slug)
        url = gh.issue_url(resolved.github, number) if resolved and resolved.github else (
            gh.issue_url(slug, number) if slug else ""
        )
        return repo_name, number, url
    if match := JIRA_URL_RE.fullmatch(raw):
        key = match[1].upper()
        resolved = resolve_repo(cfg, repo_hint) if repo_hint else None
        return (resolved.name if resolved else repo_hint), key, raw
    if JIRA_KEY_RE.fullmatch(raw):
        key = raw.upper()
        resolved = resolve_repo(cfg, repo_hint) if repo_hint else None
        return (resolved.name if resolved else repo_hint), key, ""
    return "", "", ""


def parse_links(body: str, *, cfg: config.Config | None = None) -> list[dict]:
    found: list[dict] = []
    for raw_line in _breakdown_lines(body):
        match = PEER_LINE.fullmatch(raw_line.strip())
        if not match:
            continue
        kind, rest = match[1].lower(), match[2].strip()
        parts = rest.split()
        if not parts:
            continue
        repo_hint = ""
        target_token = parts[0]
        extra = parts[1:]
        if not parse_peer_target(target_token.split("@", 1)[0], cfg=cfg)[1] and len(parts) >= 2:
            repo_hint = parts[0].strip("`")
            target_token = parts[1]
            extra = parts[2:]
        base_token, sep, inline_need = target_token.partition("@")
        need_spec = inline_need.strip("`") if sep else ""
        if not need_spec and extra:
            cand = extra[0].strip("`")
            if cand.startswith("@"):
                need_spec = cand[1:]
        peer_repo, peer_issue, peer_url = parse_peer_target(base_token, cfg=cfg, repo_hint=repo_hint)
        if not peer_repo or not peer_issue:
            continue
        requires = kind == "requires" or bool(need_spec)
        try:
            need, artifact = parse_need(need_spec) if need_spec else ("merge", None)
        except ValueError:
            continue
        found.append({
            "peer_repo": peer_repo,
            "peer_issue": peer_issue,
            "peer_url": peer_url,
            "requires": requires,
            "need": need,
            **({"artifact": artifact} if artifact else {}),
        })
    return found


_NEED_RANK = {"merge": 1, "release": 2, "artifact": 3}


def peer_requirements(cfg: config.Config | None, repo: config.Repo,
                      ticket: tracker.Ticket) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    candidates = [
        *(row for row in links_for(repo.name, str(ticket.id)) if row.get("requires")),
        *(row for row in parse_links(ticket.body, cfg=cfg) if row.get("requires")),
    ]
    for row in candidates:
        resolved = resolve_repo(cfg, row.get("peer_repo", ""))
        peer_name = resolved.name if resolved else row.get("peer_repo", "")
        peer_issue = str(row.get("peer_issue", ""))
        if not peer_name or not peer_issue:
            continue
        key = (peer_name, peer_issue)
        entry = {**row, "peer_repo": peer_name, "peer_issue": peer_issue, "requires": True}
        prior = merged.get(key)
        if prior is None or _NEED_RANK.get(entry.get("need", "merge"), 0) > _NEED_RANK.get(prior.get("need", "merge"), 0):
            merged[key] = entry
    return list(merged.values())


def peer_landed(peer_repo: config.Repo, peer_issue: str) -> list[dict]:
    from . import spec_delivery

    claim = specs.find(peer_repo.name, str(peer_issue))
    if claim:
        data = claim.get("data") or {}
        if landed := (data.get("delivery") or {}).get("landed"):
            return list(landed)
        if claim.get("state") in ("published", "evidenced", "complete") and data.get("targets"):
            try:
                if landed := spec_delivery.reached(data):
                    return landed
            except gh.GhError:
                return []
    run = runs.newest_live_whole_run(peer_repo.name, str(peer_issue)) or {}
    if run.get("status") not in ("merged", "done", "pr"):
        return []
    publication = flows.publication(run["id"])
    if not (publication.get("verified") or {}).get("verdict"):
        return []
    observed = verify.Verified.stored(publication["verified"])
    if (observed.verdict != "pass" or not observed.finished or not observed.behavior_reached
            or not observed.checks or observed.failed_checks
            or publication.get("draft_why") or publication.get("ci_stopped")):
        return []
    branch = peer_repo.target_branch or (gh.default_branch(peer_repo.github) if peer_repo.github else "")
    if pushed := release_proof.direct_push_revision(peer_repo, str(peer_issue), branch):
        return [{"repo": peer_repo.github, "branch": branch, "commit": pushed, "pr": "", "direct_push": True}]
    prs = publication.get("prs") or {}
    heads = publication.get("heads") or {}
    if peer_repo.github and (url := prs.get(peer_repo.github)):
        try:
            pr = gh.pr_view(url)
        except gh.GhError:
            return []
        merged = (pr.get("mergeCommit") or {}).get("oid", "")
        head = heads.get(peer_repo.github)
        if (head and pr.get("state") == "MERGED" and pr.get("mergedAt") and pr.get("headRefOid") == head
                and (not branch or pr.get("baseRefName") == branch)
                and spec_delivery.SHA.fullmatch(merged)
                and gh.branch_contains(peer_repo.github, branch or pr["baseRefName"], merged)):
            return [{"repo": peer_repo.github, "branch": branch or pr["baseRefName"], "commit": merged, "pr": url}]
    return []


def peer_prerequisite_proof(cfg: config.Config, req: dict) -> dict | None:
    peer_repo = resolve_repo(cfg, req.get("peer_repo", ""))
    peer_issue = str(req.get("peer_issue", ""))
    if peer_repo is None or not peer_issue:
        return None
    landed = peer_landed(peer_repo, peer_issue)
    if not landed:
        return None
    target = next((row for row in landed
                   if peer_repo.github and row.get("repo", "").casefold() == peer_repo.github.casefold()),
                  landed[0])
    commit = target.get("commit", "")
    if not commit:
        return None
    artifact = req.get("artifact")
    need = "artifact" if artifact else (req.get("need") or "merge")
    if need == "merge":
        return {
            "peer_repo": peer_repo.name,
            "peer_issue": peer_issue,
            "github": target.get("repo", peer_repo.github),
            "branch": target.get("branch", ""),
            "commit": commit,
            "pr": target.get("pr", ""),
            "method": "merge",
        }
    receipt = (release_proof.evidence(peer_repo.name, peer_issue, commit)
               or release_proof.completion(peer_repo.name, peer_issue, commit))
    if not receipt:
        return None
    if artifact:
        fields = {f["field"]: f["value_sha256"] for f in receipt.get("observed", [])}
        if any(fields.get("/" + k) != release_proof.digest(v) for k, v in artifact.items()):
            return None
    return {
        "peer_repo": peer_repo.name,
        "peer_issue": peer_issue,
        "github": target.get("repo", peer_repo.github),
        "branch": target.get("branch", ""),
        "commit": commit,
        "pr": target.get("pr", ""),
        "method": "artifact" if artifact else "release",
        "receipt_id": receipt.get("receipt_id", ""),
        "destination": receipt.get("destination", ""),
        "path": receipt.get("path", ""),
        **({"artifact": dict(artifact)} if artifact else {}),
    }


def peer_blocker(cfg: config.Config, repo: config.Repo, ticket: tracker.Ticket) -> str:
    for req in peer_requirements(cfg, repo, ticket):
        if peer_prerequisite_proof(cfg, req) is None:
            peer_ref = tracker.issue_name(req["peer_repo"], req["peer_issue"])
            artifact = req.get("artifact")
            point = f"{artifact['name']} {artifact['version']}" if artifact else req.get("need", "merge")
            return f"waiting for linked spec {peer_ref} to reach its required {point}"
    return ""


def consumed_peers_context(cfg: config.Config, repo: config.Repo, ticket: tracker.Ticket) -> list[dict]:
    found = []
    for req in peer_requirements(cfg, repo, ticket):
        if proof := peer_prerequisite_proof(cfg, req):
            found.append(proof)
    return found


def format_consumed_peers(peers: list[dict]) -> str:
    if not peers:
        return ""
    lines = ["## Verified linked peer prerequisites", ""]
    for peer in peers:
        ref = tracker.issue_name(peer["peer_repo"], peer["peer_issue"])
        detail = f"`{peer['commit']}`"
        if artifact := peer.get("artifact"):
            detail = f"`{artifact['name']}=={artifact['version']}` at `{peer['commit']}`"
        if receipt := peer.get("receipt_id"):
            detail += f" (release receipt `{receipt}`)"
        lines.append(f"- {ref}: {detail}")
    return "\n".join(lines)


def satisfied_cross_repo_native(enrolled_repos: Iterable[config.Repo], repo: config.Repo,
                                tid: str, blockers: Iterable[int | str]) -> set[int | str]:
    repos_list = list(enrolled_repos)
    if not repos_list or not blockers:
        return set()
    stored = [row for row in links_for(repo.name, str(tid)) if row.get("requires")]
    if not stored:
        return set()
    cfg = config.Config(repos=repos_list)
    by_slug = {r.github.casefold(): r.name for r in repos_list if r.github}
    satisfied: set[int | str] = set()
    for blocker in blockers:
        if isinstance(blocker, str) and "#" in blocker:
            slug, number = blocker.rsplit("#", 1)
            peer_name = by_slug.get(slug.casefold(), "")
        elif isinstance(blocker, int):
            peer_name, number = repo.name, str(blocker)
        else:
            continue
        if not peer_name:
            continue
        for req in stored:
            if req.get("peer_repo") == peer_name and str(req.get("peer_issue")) == number:
                if peer_prerequisite_proof(cfg, req) is not None:
                    satisfied.add(blocker)
    return satisfied
