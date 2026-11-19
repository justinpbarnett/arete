from __future__ import annotations

import base64
import http.client
import json
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

from . import config, gh, pilot, util

WORKFLOW = ".github/workflows/ci.yml"
PERMISSIONS = {"contents": "write", "pull_requests": "write"}
LOG_TIME = r"[0-9.T:-]+Z "
RUN_STEP = re.compile(rf"(?m)^{LOG_TIME}##\[group\]Run ([^\r\n]+)\r?$")
CHECKOUT = re.compile(rf"(?m)^{LOG_TIME}\[command\][^\r\n]+/git log -1 --format=%H\r?\n"
                      rf"{LOG_TIME}([a-f0-9]{{40}})\r?$")
MERGE_SETTINGS = """query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) { id nameWithOwner squashMergeAllowed autoMergeAllowed }
}"""
MAX_RESPONSE = 1024 * 1024


def api(method: str, path: str, token: str, timeout: float, data: dict | None = None) -> dict:
    connection = http.client.HTTPSConnection("api.github.com", timeout=timeout)
    try:
        connection.request(method, path, json.dumps(data) if data is not None else None,
                           {"Authorization": f"Bearer {token}",
                           "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                           "User-Agent": "Arete-ZTE", "X-GitHub-Api-Version": "2022-11-28"})
        response = connection.getresponse()
        if response.status not in (200, 201):
            if method == "GET":
                raise gh.GhError(f"GitHub HTTP {response.status}; could not confirm ZTE App scope")
            raise gh.GhUnsure(f"GitHub HTTP {response.status}; reconcile the ZTE effect by reading")
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise gh.GhUnsure("GitHub response exceeded the ZTE receipt bound")
        return json.loads(raw)
    finally:
        connection.close()


def rows(pages, field: str = "") -> list:
    pilot.require(isinstance(pages, list) and bool(pages), "GitHub returned no paginated evidence")
    groups = [page.get(field) if isinstance(page, dict) else None for page in pages] if field else pages
    pilot.require(all(isinstance(group, list) for group in groups), "GitHub returned incomplete evidence")
    return [row for group in groups for row in group]


def without_urls(data):
    if isinstance(data, dict):
        return {key: without_urls(value) for key, value in data.items()
                if key != "url" and not key.endswith("_url")}
    if isinstance(data, list):
        return [without_urls(item) for item in data]
    return data


@dataclass(frozen=True)
class Identity:
    name: str
    number: int
    node: str
    owner: int
    bot: int
    workflow: int
    workflow_blob: str
    app: int
    installation: int


class GitHub:
    def __init__(self, identity: Identity, deadline: str, key: Path, *, observer: Path | None = None):
        self.identity, self.deadline, self.key = identity, deadline, key
        self.observer = observer
        self.route = f"repos/{identity.name}"

    def time_left(self, cap: int = 15) -> float:
        left = (util.parse_iso(self.deadline) - util.now()).total_seconds()
        pilot.require(left > 0, "ZTE GitHub deadline expired")
        return min(left, cap)

    def _gh_read(self, args: list[str], *, cap: int = 15) -> str:
        env = {"GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1"}
        if self.observer is not None:
            from .zte import private_bytes

            token = private_bytes(self.observer, 512).decode().strip()
            pilot.require(re.fullmatch(r"github_pat_[A-Za-z0-9_]{30,500}", token),
                          "the dedicated read-only GitHub observer is unavailable")
            env["GH_TOKEN"] = token
        result = util.sh(["gh", "api", "--hostname", "github.com", *args], timeout=self.time_left(cap), env=env)
        if result.returncode != 0:
            raise gh.GhError("GitHub API could not confirm exact ZTE evidence")
        return result.stdout

    def _get(self, endpoint: str, *, pages=False, cap: int = 15) -> str:
        args = ["--method", "GET", "--paginate", "--slurp"] if pages else ["--method", "GET"]
        return self._gh_read([*args, "--", endpoint], cap=cap)

    def read(self, suffix: str = "", *, pages=False):
        return json.loads(self._get(self.route + suffix, pages=pages))

    def bot_info(self, login: str) -> dict:
        return json.loads(self._get(f"users/{quote(login, safe='')}"))

    def require_merge_settings(self, repo: dict) -> None:
        squash, auto = repo.get("allow_squash_merge"), repo.get("allow_auto_merge")
        if squash is None or auto is None:
            owner, name = self.identity.name.split("/")
            reply = json.loads(self._gh_read(["graphql", "-f", f"query={MERGE_SETTINGS}",
                                              "-f", f"owner={owner}", "-f", f"name={name}"]))
            settings = reply["data"]["repository"]
            pilot.require((settings["id"], settings["nameWithOwner"]) == (self.identity.node, self.identity.name),
                          "GitHub returned merge settings for a different repository")
            reported = settings["squashMergeAllowed"], settings["autoMergeAllowed"]
            pilot.require(all(value is None or value is checked for value, checked in zip((squash, auto), reported)),
                          "GitHub reported conflicting merge settings")
            squash, auto = reported
        pilot.require(squash is True and auto is False, "GitHub must allow squash and disable deferred auto-merge")

    def snapshot(self) -> dict:
        repo = self.read()
        identity = self.identity
        pilot.require((repo.get("node_id"), repo.get("id"), repo.get("full_name"), (repo.get("owner") or {}).get("id"))
                      == (identity.node, identity.number, identity.name, identity.owner)
                      and repo.get("default_branch") == "main" and repo.get("private") is False
                      and not repo.get("archived"), "GitHub does not report the approved personal repository and main")
        self.require_merge_settings(repo)
        protection = self.read("/branches/main/protection")
        status = protection.get("required_status_checks") or {}
        pilot.require(status.get("strict") is True and status.get("checks") == [pilot.CHECK]
                      and protection.get("enforce_admins", {}).get("enabled") is True
                      and protection.get("allow_force_pushes", {}).get("enabled") is False
                      and protection.get("allow_deletions", {}).get("enabled") is False,
                      "the required strict GitHub protection is missing")
        contributors = rows(self.read("/collaborators?affiliation=all&per_page=100", pages=True))
        keys = rows(self.read("/keys?per_page=100", pages=True))
        rules = rows(self.read("/rulesets?includes_parents=true&per_page=100", pages=True))
        effective_rules = self.read("/rules/branches/main")
        pilot.require(not rules and effective_rules == [], "unexpected repository rules or bypass authority")
        hooks = rows(self.read("/hooks?per_page=100", pages=True))
        workflows = rows(self.read("/actions/workflows?per_page=100", pages=True), "workflows")
        enabled = [(row["id"], row["path"]) for row in workflows if row.get("state") == "active"]
        pilot.require(enabled == [(identity.workflow, WORKFLOW)], "the approved CI workflow is not the sole active workflow")
        actions = self.read("/actions/permissions")
        permissions = self.read("/actions/permissions/workflow")
        pilot.require(actions.get("enabled") is True and permissions.get("default_workflow_permissions") == "read"
                      and permissions.get("can_approve_pull_request_reviews") is False,
                      "GitHub Actions default permissions differ from the audited read-only baseline")
        return {"owner": identity.owner, "protection": without_urls(protection),
                "collaborators": sorted((row["id"], row["role_name"], row["permissions"]) for row in contributors),
                "deploy_keys": sorted((row["id"], row["read_only"]) for row in keys),
                "rulesets": sorted((row["id"], row["enforcement"]) for row in rules),
                "hooks": sorted((row["id"], row["active"], sorted(row["events"])) for row in hooks),
                "actions": without_urls(actions), "workflow_permissions": permissions, "workflow": identity.workflow}

    def tip(self) -> str:
        tip = self.read("/git/ref/heads/main")["object"]["sha"]
        pilot.require(pilot.SHA.fullmatch(tip), "GitHub did not report an exact main tip")
        return tip

    def commit(self, sha: str) -> dict:
        pilot.require(pilot.SHA.fullmatch(sha), "invalid GitHub commit ID")
        row = self.read(f"/git/commits/{sha}")
        pilot.require(row["sha"] == sha, "GitHub returned a different commit")
        return row

    def document(self, base: str) -> str:
        query = urlencode({"ref": base})
        ci = self.read(f"/contents/{WORKFLOW}?{query}")
        pilot.require(ci.get("type") == "file" and ci.get("sha") == self.identity.workflow_blob,
                      "main does not contain the approved CI workflow")
        tree = self.read(f"/git/trees/{self.commit(base)['tree']['sha']}")
        (entry,) = [row for row in tree["tree"] if row.get("path") == pilot.DOC]
        pilot.require(entry.get("type") == "blob" and entry.get("mode") in ("100644", "100755"),
                      "main README is not a regular tracked blob")
        blob = self.read(f"/git/blobs/{entry['sha']}")
        pilot.require(blob.get("encoding") == "base64" and blob.get("sha") == entry["sha"]
                      and 0 < blob.get("size", 0) <= MAX_RESPONSE, "README blob is not a bounded file")
        return base64.b64decode(blob["content"]).decode()

    def branch_head(self, branch: str) -> str:
        pilot.require(re.fullmatch(r"arete/t-[a-f0-9]{40}-[a-z0-9-]+", branch), "invalid ZTE branch")
        ref = f"refs/heads/{branch}"
        matching = self.read(f"/git/matching-refs/heads/{branch}")
        pilot.require(isinstance(matching, list), "invalid GitHub ref response")
        found = [row["object"]["sha"] for row in matching if row.get("ref") == ref]
        pilot.require(len(found) <= 1 and all(pilot.SHA.fullmatch(sha) for sha in found), "ambiguous ZTE branch")
        return found[0] if found else ""

    def branch_prs(self, branch: str) -> list:
        query = urlencode({"state": "all", "head": f"{self.identity.name.split('/')[0]}:{branch}", "per_page": 100})
        return rows(self.read(f"/pulls?{query}", pages=True))

    def pull(self, number: int) -> dict:
        pilot.require(type(number) is int and number > 0, "invalid ZTE pull request number")
        return self.read(f"/pulls/{number}")

    def require_pull(self, pull: dict, *, node: str, head: str, branch: str) -> None:
        src, dst = pull.get("head") or {}, pull.get("base") or {}
        pilot.require(pull.get("node_id") == node and pull.get("user", {}).get("id") == self.identity.bot
                      and src.get("sha") == head and src.get("ref") == branch and dst.get("ref") == "main"
                      and (src.get("repo") or {}).get("node_id") == self.identity.node
                      and (dst.get("repo") or {}).get("node_id") == self.identity.node,
                      "the GitHub PR no longer matches the reserved repository, producer, head and target")

    def readiness(self, pull: dict, base: str) -> bool:
        pilot.require(pull.get("state") == "open" and pull.get("merged") is False and pull.get("draft") is False
                      and pull.get("auto_merge") is None and pull["base"]["sha"] == base and self.tip() == base,
                      "the approved PR or current main changed before the squash")
        if pull["mergeable"] is None or (pull["mergeable"] is True and pull.get("mergeable_state") == "unknown"):
            return False
        pilot.require(pull["mergeable"] is True and pull.get("mergeable_state") == "clean",
                      "GitHub does not consider the exact PR clean and mergeable")
        return True

    def ci(self, head: str, tree: str, event: str, *, base: str = "", pr: int = 0, branch: str = "main") -> dict | None:
        pilot.require((event == "pull_request" and type(pr) is int and pr > 0 and pilot.SHA.fullmatch(base))
                      or (event == "push" and pr == 0 and branch == "main"), "invalid exact-CI target")
        ident = self.identity
        query = urlencode({"event": event, "head_sha": head, "per_page": 100})
        runs = rows(self.read(f"/actions/workflows/{ident.workflow}/runs?{query}", pages=True), "workflow_runs")
        candidates = [run for run in runs if (run.get("workflow_id"), run.get("head_sha"), run.get("event"))
                      == (ident.workflow, head, event) and run.get("head_branch") == branch
                      and run.get("path") == WORKFLOW and (run.get("repository") or {}).get("node_id") == ident.node
                      and (run.get("head_repository") or {}).get("node_id") == ident.node]
        if not candidates:
            return None
        run = max(candidates, key=lambda row: (row["run_number"], row["run_attempt"]))
        if run.get("status") != "completed":
            return None
        pilot.require(run.get("conclusion") == "success" and (event != "push" or run.get("head_branch") == "main"),
                      f"exact {event} CI did not succeed on the expected ref")
        jobs = rows(self.read(f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100",
                              pages=True), "jobs")
        pilot.require(len(jobs) == 1 and (jobs[0].get("name"), jobs[0].get("conclusion")) ==
                      (pilot.CHECK["context"], "success"), "CI did not run the approved job")
        checks = rows(self.read(f"/commits/{head}/check-runs?filter=latest&per_page=100", pages=True), "check_runs")
        statuses = rows(self.read(f"/commits/{head}/statuses?per_page=100", pages=True))
        latest = {}
        for status in statuses:
            latest.setdefault(status["context"], status["state"])
        pilot.require(all(state == "success" for state in latest.values()), "a hosted status is not passing")
        expected = [row for row in checks if row.get("head_sha") == head and row.get("name") == pilot.CHECK["context"]
                    and (row.get("app") or {}).get("id") == pilot.CHECK["app_id"]
                    and row.get("details_url") == jobs[0].get("html_url")]
        if not expected or any(row.get("status") != "completed" for row in checks):
            return None
        pilot.require(len(expected) == 1 and all(row.get("conclusion") == "success" for row in checks),
                      "hosted checks are not completely green")
        log = self._get(f"{self.route}/actions/jobs/{jobs[0]['id']}/logs", cap=25)
        steps = list(RUN_STEP.finditer(log))
        checkout = [i for i, step in enumerate(steps) if re.fullmatch(r"actions/checkout@[a-f0-9]{40}", step[1])]
        pilot.require(len(steps) > 1 and checkout == [0], "the approved CI job did not start with one pinned checkout")
        checkout_log = log[steps[0].end():steps[1].start()]
        tested = CHECKOUT.findall(checkout_log)
        pilot.require(len(tested) == 1, "the pinned checkout step did not report a unique tested commit")
        if pr:
            checkout_ref = re.escape(f"+{tested[0]}:refs/remotes/pull/{pr}/merge")
            fetched = rf"(?m)^{LOG_TIME}\[command\][^\r\n]+ fetch [^\r\n]+ {checkout_ref}\r?$"
            pilot.require(re.search(fetched, checkout_log), "the tested checkout is not the admitted PR merge ref")
        commit = self.commit(tested[0])
        pilot.require(commit.get("tree", {}).get("sha") == tree and (
            [parent["sha"] for parent in commit["parents"]] == [base, head] if event == "pull_request"
            else tested[0] == head), f"the {event} checkout is not the approved tested artifact")
        return {"run": run["id"], "attempt": run["run_attempt"], "check": expected[0]["id"],
                "tested": tested[0], "event": event, "head": head, "tree": tree}

    def token(self) -> str:
        import stat

        pilot.require(pilot.private(self.key.parent, stat.S_ISDIR) and pilot.private(self.key, stat.S_ISREG)
                      and self.key.lstat().st_nlink == 1, "the delivery signing key must be private")
        identity = self.identity
        app = config.GithubApp(app_id=str(identity.app), installation_id=str(identity.installation), private_key=self.key)
        jwt = gh.app_jwt(app)
        path = f"/app/installations/{identity.installation}"
        install = api("GET", path, jwt, self.time_left())
        allowed = {**PERMISSIONS, "metadata": "read"}
        pilot.require((install.get("id"), install.get("app_id"), install.get("target_id")) ==
                      (identity.installation, identity.app, identity.owner) and install.get("target_type") == "User"
                      and install.get("repository_selection") == "selected" and install.get("suspended_at") is None
                      and install.get("permissions") == allowed,
                      "the delivery App installation or permissions changed")
        slug = install.get("app_slug")
        pilot.require(isinstance(slug, str) and re.fullmatch(r"[a-z0-9-]{1,100}", slug),
                      "the delivery App installation did not identify its bot")
        login = slug + "[bot]"
        bot = self.bot_info(login)
        pilot.require((bot.get("login"), bot.get("type"), bot.get("id")) == (login, "Bot", identity.bot),
                      "the delivery App bot does not match the approved actor")
        reply = api("POST", f"{path}/access_tokens", jwt, self.time_left(),
                    {"repository_ids": [identity.number], "permissions": PERMISSIONS})
        token = reply.get("token")
        pilot.require(reply.get("permissions") == allowed and util.parse_iso(reply["expires_at"]) > util.now()
                      and isinstance(token, str) and token, "the delivery App token permissions changed")
        scope = api("GET", "/installation/repositories?per_page=2", token, self.time_left())
        pilot.require(scope.get("total_count") == 1 and [(r["id"], r["node_id"], r["full_name"])
                      for r in scope.get("repositories", [])] == [(identity.number, identity.node, identity.name)],
                      "the delivery App token did not prove the single approved repository")
        return token

    @staticmethod
    def git_env() -> dict:
        return {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"}

    @contextmanager
    def push_source(self, where: Path, head: str, tree: str, base: str):
        pilot.require(all(pilot.SHA.fullmatch(sha) for sha in (head, tree, base)), "invalid ZTE push source")
        with tempfile.TemporaryDirectory(prefix="arete-zte-push-") as directory:
            root = Path(directory)

            def git(*args):
                done = util.sh(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *args], cwd=root,
                               env=self.git_env(), inherit=False, timeout=self.time_left(25))
                pilot.require(done.returncode == 0, "could not isolate the candidate Git object before push")
                return done.stdout.strip()

            git("init", "--bare", "--quiet", "--template=")
            git("fetch", "--no-tags", "--no-recurse-submodules", "--", str(where.resolve()), head)
            pilot.require(git("rev-parse", "FETCH_HEAD", "FETCH_HEAD^{tree}").split() == [head, tree]
                          and git("rev-list", "--parents", "-n", "1", head).split() == [head, base],
                          "the isolated candidate does not match the admitted head, tree and parent")
            yield root

    def push(self, where: Path, branch: str, head: str, token: str) -> None:
        pilot.require(re.fullmatch(r"arete/t-[a-f0-9]{40}-[a-z0-9-]+", branch) and pilot.SHA.fullmatch(head),
                      "invalid ZTE push ref")
        ref = f"refs/heads/{branch}"
        url = f"https://github.com/{self.identity.name}.git"
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        environment = {**self.git_env(), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                       "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}
        result = util.sh(["/usr/bin/git", "-c", "http.followRedirects=false", "-c", "core.hooksPath=/dev/null",
                          "-c", "credential.helper=", "push", "--no-follow-tags", "--recurse-submodules=no",
                          f"--force-with-lease={ref}:", url, f"{head}:{ref}"], cwd=where, env=environment,
                         inherit=False, timeout=self.time_left(30))
        if result.returncode != 0:
            raise gh.GhUnsure("GitHub did not confirm the exact branch push; inspect without retrying")

    def create(self, branch: str, title: str, body: str, token: str) -> dict:
        return api("POST", f"/{self.route}/pulls", token, self.time_left(),
                   {"head": branch, "base": "main", "title": str(gh.outgoing(title)),
                    "body": str(gh.outgoing(body, mark=True)), "draft": False})

    def squash(self, number: int, head: str, token: str) -> dict:
        pilot.require(type(number) is int and number > 0 and pilot.SHA.fullmatch(head), "invalid squash request")
        return api("PUT", f"/{self.route}/pulls/{number}/merge", token, self.time_left(),
                   {"sha": head, "merge_method": "squash"})
