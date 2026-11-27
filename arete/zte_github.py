from __future__ import annotations

import base64
import json
import re
import selectors
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

from . import config, gh, pilot, util

if TYPE_CHECKING:
    from .zte_issue import Claim

WORKFLOW = ".github/workflows/ci.yml"
PERMISSIONS = {"contents": "write", "pull_requests": "write"}
ISSUE_PERMISSIONS = {"issues": "write"}
LOG_TIME = r"[0-9.T:-]+Z "
RUN_STEP = re.compile(rf"(?m)^{LOG_TIME}##\[group\]Run ([^\r\n]+)\r?$")
CHECKOUT = re.compile(rf"(?m)^{LOG_TIME}\[command\][^\r\n]+/git log -1 --format=%H\r?\n"
                      rf"{LOG_TIME}([a-f0-9]{{40}})\r?$")
MERGE_SETTINGS = """query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) { id nameWithOwner squashMergeAllowed autoMergeAllowed }
}"""
MAX_RESPONSE = 1024 * 1024


def api(method: str, path: str, token: str, timeout: float, data: dict | None = None, *, result_type=dict):
    deadline = time.monotonic() + timeout
    pilot.require(path.startswith("/") and token.isascii() and token.isprintable(), "invalid ZTE API request")
    headers = (f"Authorization: Bearer {token}\nAccept: application/vnd.github+json\n"
               "Content-Type: application/json\nX-GitHub-Api-Version: 2022-11-28\n").encode()
    with tempfile.TemporaryFile() as auth, tempfile.TemporaryFile() as payload:
        auth.write(headers)
        auth.seek(0)
        body = []
        if data is not None:
            payload.write(json.dumps(data).encode())
            payload.seek(0)
            body = ["--data-binary", f"@/dev/fd/{payload.fileno()}"]
        remaining = deadline - time.monotonic()
        pilot.require(remaining >= 0.001, "ZTE GitHub deadline expired before request")
        command = [
            "/usr/bin/curl", "--disable", "--silent", "--http1.1", "--globoff", "--noproxy", "*", "--proto", "=https",
            "--max-time", str(remaining), "--max-filesize", str(MAX_RESPONSE), "--user-agent", "Arete-ZTE",
            "--header", "@-", "--request", method, "--write-out", "\n%{http_code}", *body, f"https://api.github.com{path}",
        ]
        raw, _, status = api_response(command, auth, payload.fileno(), deadline).rpartition(b"\n")
    if status not in (b"200", b"201"):
        label = status.decode("ascii") if len(status) == 3 and status.isdigit() else "unknown"
        if method == "GET":
            raise gh.GhError(f"GitHub HTTP {label}; could not confirm ZTE App scope")
        raise gh.GhUnsure(f"GitHub HTTP {label}; reconcile the ZTE effect by reading")
    pilot.require(len(raw) <= MAX_RESPONSE, "GitHub response exceeded the ZTE receipt bound")
    result = json.loads(raw)
    pilot.require(isinstance(result, result_type), "GitHub returned an unexpected ZTE receipt")
    return result


def api_response(command, auth, payload_fd: int, deadline: float) -> bytes:
    unconfirmed = "GitHub transport did not confirm the ZTE response in time; reconcile by reading"
    try:
        with subprocess.Popen(command, stdin=auth, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              env={}, pass_fds=(payload_fd,)) as curl:
            try:
                stream = curl.stdout
                data = bytearray()
                limit = MAX_RESPONSE + len(b"\n200")
                with selectors.DefaultSelector() as readable:
                    readable.register(stream, selectors.EVENT_READ)
                    while (left := deadline - time.monotonic()) > 0 and readable.select(left):
                        chunk = stream.read1(min(64 * 1024, limit + 1 - len(data)))
                        if not chunk:
                            if curl.wait(timeout=max(0, deadline - time.monotonic())) == 0 and time.monotonic() < deadline:
                                return bytes(data)
                            break
                        data.extend(chunk)
                        if len(data) > limit:
                            raise gh.GhUnsure("GitHub response exceeded the ZTE receipt bound")
                raise gh.GhUnsure(unconfirmed)
            finally:
                if curl.poll() is None:
                    curl.kill()
    except (OSError, subprocess.SubprocessError):
        raise gh.GhUnsure(unconfirmed) from None


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


@dataclass(frozen=True)
class IssueApp:
    app: int
    installation: int
    bot: int

    def separate_from(self, identity: Identity) -> bool:
        return (all(type(value) is int and value > 0 for value in (self.app, self.installation, self.bot))
                and self.app != identity.app and self.installation != identity.installation
                and self.bot not in (identity.bot, identity.owner))


class GitHub:
    def __init__(self, identity: Identity, deadline: str, key: Path, *, observer: Path | None = None,
                 issue_authority: tuple[Claim, IssueApp] | None = None):
        self.identity, self.deadline, self.key = identity, deadline, key
        self.observer, self.issue_authority = observer, issue_authority
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

    def issue_binding(self, number: int, pr: int) -> dict:
        from . import zte_issue

        zte_issue.require_number(pr)
        return self.issue_graph(zte_issue.CLOSING_QUERY, number, "-F", f"pr={pr}")

    def issue_snapshot(self, number: int) -> dict:
        from . import zte_issue

        return self.issue_graph(zte_issue.ISSUE_QUERY, number)

    def issue_graph(self, query: str, number: int, *args: str) -> dict:
        from . import zte_issue

        zte_issue.require_number(number)
        owner, name = self.identity.name.split("/")
        reply = json.loads(self._gh_read(["graphql", "-f", f"query={query}", "-f", f"owner={owner}",
                                          "-f", f"repo={name}", "-F", f"issue={number}", *args]))
        pilot.require(not reply.get("errors") and isinstance((reply.get("data") or {}).get("repository"), dict),
                      "GitHub did not return complete issue closure evidence")
        return reply["data"]["repository"]

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
        identity = self.identity
        return self._installation_token(IssueApp(identity.app, identity.installation, identity.bot), self.key, PERMISSIONS)

    def require_issue_app(self, actor: IssueApp, key: Path) -> None:
        self._issue_token(actor, key, write=False)

    def issue_token(self, actor: IssueApp, key: Path) -> str:
        return self._issue_token(actor, key, write=True)

    def _issue_token(self, actor: IssueApp, key: Path, *, write: bool) -> str:
        pilot.require(self.permits_issue_effect(actor=actor) and actor.separate_from(self.identity) and key != self.key,
                      "issue closing requires the approved separate App for the pinned repository")
        return self._installation_token(actor, key, ISSUE_PERMISSIONS, single_installation=True, write=write,
                                        check_registration=self.issue_authority is not None)

    def permits_issue_effect(self, *, actor: IssueApp | None = None, number: int | None = None) -> bool:
        from . import zte

        identity = self.identity
        scope = identity.name, identity.number, identity.node, identity.owner
        if scope == zte.REHEARSAL:
            return True
        if scope != zte.CANONICAL or self.issue_authority is None:
            return False
        approved, approved_actor = self.issue_authority
        return (approved_actor.separate_from(identity) and approved_actor.app != zte.REHEARSAL_ISSUE_APP_ID
                and (actor is None or actor == approved_actor) and (number is None or number == approved.number))

    def require_issue_reference(self, claim: Claim, explicit: bool) -> None:
        from . import zte

        identity = self.identity
        scope = identity.name, identity.number, identity.node, identity.owner
        pilot.require(scope == zte.REHEARSAL or (explicit and self.issue_authority is not None
                      and self.permits_issue_effect(number=claim.number) and claim == self.issue_authority[0]),
                      "the issue reference is outside the approved post-health close authority")

    def _installation_token(self, actor: IssueApp, key: Path, permissions: dict, *, single_installation: bool = False,
                            write: bool = True, check_registration: bool = False) -> str:
        import stat

        pilot.require(pilot.private(key.parent, stat.S_ISDIR) and pilot.private(key, stat.S_ISREG)
                      and key.lstat().st_nlink == 1, "the App signing key must be private")
        identity = self.identity
        app = config.GithubApp(app_id=str(actor.app), installation_id=str(actor.installation))
        jwt = gh.app_jwt(app, timeout=self.time_left(30), key_file=key)
        path = f"/app/installations/{actor.installation}"
        allowed = {**permissions, "metadata": "read"}
        if check_registration:
            registration = api("GET", "/app", jwt, self.time_left())
            pilot.require(registration.get("id") == actor.app and registration.get("public") is not True
                          and (registration.get("owner") or {}).get("id") == identity.owner
                          and registration.get("permissions") == allowed and registration.get("events") == [],
                          "the issue App registration differs from the approved owner, scope or events")
        if single_installation:
            found = api("GET", "/app/installations?per_page=2", jwt, self.time_left(), result_type=list)
            pilot.require(len(found) == 1 and isinstance(found[0], dict), "the issue App must have exactly one installation")
            install = found[0]
        else:
            install = api("GET", path, jwt, self.time_left())
        pilot.require((install.get("id"), install.get("app_id"), install.get("target_id")) ==
                      (actor.installation, actor.app, identity.owner) and install.get("target_type") == "User"
                      and install.get("repository_selection") == "selected" and install.get("suspended_at") is None
                      and install.get("permissions") == allowed, "the App installation or permissions changed")
        slug = install.get("app_slug")
        pilot.require(isinstance(slug, str) and re.fullmatch(r"[a-z0-9-]{1,100}", slug),
                      "the App installation did not identify its bot")
        login = slug + "[bot]"
        bot = self.bot_info(login)
        pilot.require((bot.get("login"), bot.get("type"), bot.get("id")) == (login, "Bot", actor.bot),
                      "the App bot does not match the approved actor")
        if single_installation:
            observer = self._mint_installation_token(path, jwt, {"metadata": "read"})
            self._require_token_repository(observer)
            if not write:
                return observer
        token = self._mint_installation_token(path, jwt, permissions, repository_ids=[identity.number])
        self._require_token_repository(token)
        return token

    def _mint_installation_token(self, path: str, jwt: str, permissions: dict, **scope) -> str:
        reply = api("POST", f"{path}/access_tokens", jwt, self.time_left(), {"permissions": permissions, **scope})
        token = reply.get("token")
        pilot.require(reply.get("permissions") == {**permissions, "metadata": "read"}
                      and util.parse_iso(reply["expires_at"]) > util.now() and isinstance(token, str) and token,
                      "the App token permissions changed")
        return token

    def _require_token_repository(self, token: str) -> None:
        identity = self.identity
        scope = api("GET", "/installation/repositories?per_page=2", token, self.time_left())
        pilot.require(scope.get("total_count") == 1 and [(r["id"], r["node_id"], r["full_name"])
                      for r in scope.get("repositories", [])] == [(identity.number, identity.node, identity.name)],
                      "the App token did not prove the single approved repository")

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
                pilot.require(done.returncode == 0, f"could not isolate the candidate Git object before push ({args[0]})")
                return done.stdout.strip()

            git("init", "--bare", "--quiet", "--template=")
            git("fetch", "--depth=2", "--no-tags", "--no-recurse-submodules", "--", str(where.resolve()), head)
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

    def create(self, branch: str, title: str, body: str, token: str, *, issue=None, explicit: bool = False) -> dict:
        if issue is not None:
            from . import zte_issue

            self.require_issue_reference(issue, explicit)
            body = zte_issue.pr_body(issue, explicit=explicit)
        else:
            body = gh.outgoing(body, mark=True)
        return api("POST", f"/{self.route}/pulls", token, self.time_left(),
                   {"head": branch, "base": "main", "title": str(gh.outgoing(title)), "body": str(body), "draft": False})

    def close_issue(self, number: int, token: str) -> None:
        from . import zte_issue

        zte_issue.require_number(number)
        pilot.require(self.permits_issue_effect(number=number), "issue closure is outside the pinned authority")
        api("PATCH", f"/{self.route}/issues/{number}", token, self.time_left(),
            {"state": "closed", "state_reason": "completed"})

    def squash(self, number: int, head: str, token: str, *, issue=None, explicit: bool = False) -> dict:
        pilot.require(type(number) is int and number > 0 and pilot.SHA.fullmatch(head), "invalid squash request")
        fields = {"sha": head, "merge_method": "squash"}
        if issue is not None:
            from . import zte_issue

            self.require_issue_reference(issue, explicit)
            fields.update(commit_title=zte_issue.squash_title(number),
                          commit_message=zte_issue.reference(issue, explicit=explicit))
        return api("PUT", f"/{self.route}/pulls/{number}/merge", token, self.time_left(), fields)
