"""Owner-controlled, read-only acceptance checks against an identified running release."""
from __future__ import annotations

import hashlib
import http.client
import json
import subprocess
import sys
import tempfile
import re
import time
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit

from . import client, codebase, config, gh, paths, serve_guard, util, zte
from .store import runs as run_store

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}")
LIMIT = 1024 * 1024


class Held(ValueError):
    pass


def need(ok, why):
    if not ok:
        raise Held(why)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def key(repo, ticket):
    need(all(isinstance(i, str) and NAME.fullmatch(i) for i in (repo, ticket)), "invalid release goal identity")
    return digest([repo, ticket])


def policy_file(repo, ticket):
    key(repo, ticket)
    return paths.CONFIG_DIR / "release-checks" / repo / (ticket + ".json")


def state_dir(repo, ticket):
    return paths.STATE_DIR / "release-checks" / key(repo, ticket)


def goal(repo, ticket):
    file = policy_file(repo, ticket)
    try:
        data = json.loads(zte.private_bytes(file, 32768))
    except FileNotFoundError:
        return None
    from . import release_profile

    profile = release_profile.read(repo)
    if profile or "repository_profile" in data:
        if not profile or data.get("repository_profile") != digest(profile):
            return None
        data = release_profile.apply(profile, data["promise"])
    validate(data)
    return {**data, "repository_profile": digest(profile)} if profile else data


def server_url(value):
    need(isinstance(value, str), "a destination URL is required")
    url = urlsplit(value)
    need(bool(url.hostname) and url.path in ("", "/") and not (url.query or url.fragment or url.username or url.password)
         and (url.scheme == "https" or url.scheme == "http" and url.hostname in ("127.0.0.1", "::1")),
         "use an HTTPS origin, or HTTP on numeric loopback for a disposable service")
    return url


def steps(rows):
    need(isinstance(rows, list) and 1 <= len(rows) <= 16, "each scenario needs 1 to 16 assertions")
    for row in rows:
        need(isinstance(row, dict) and set(row) in ({"field", "equals"}, {"field", "count", "where"}),
             "an assertion needs a field and expected value or a filtered count")
        pointer(row["field"])
        if "count" in row:
            pointer(row["count"])
            need(isinstance(row["where"], dict) and 1 <= len(row["where"]) <= 8, "count needs criteria")
            for field in row["where"]:
                pointer(field)


def pointer(value):
    need(isinstance(value, str) and re.fullmatch(r"(?:/[A-Za-z0-9_.-]+){1,8}", value), "invalid field path")
    return value[1:].split("/")


def validate(data):
    need(isinstance(data, dict) and set(data) <= {"destination", "path", "assertions", "recovery", "revision",
         "staging", "production", "qa", "instructions", "identity_path", "observer_sha256",
         "production_branch", "immutable_host_suffix", "checks", "qa_require_revision"} and data.get("destination") in ("arete-serve", "staging", "production"),
         "invalid release policy or unattended destination")
    path = data.get("path")
    need(isinstance(path, str) and re.fullmatch(r"/[a-zA-Z0-9/_.~%-]*", path) and "//" not in path
         and "%" not in path and not any(part in (".", "..") for part in path.split("/")), "invalid read-only path")
    steps(data.get("assertions"))
    steps(data.get("recovery"))
    if "revision" in data:
        need(isinstance(data["revision"], str) and serve_guard.SHA.fullmatch(data["revision"]), "invalid exact revision")
    if "observer_sha256" in data:
        need(re.fullmatch(r"[a-f0-9]{64}", data["observer_sha256"]) is not None, "invalid observer fingerprint")
    if data["destination"] == "arete-serve":
        need(path.startswith("/api/") and not ({"staging", "production", "qa", "observer_sha256", "production_branch", "identity_path", "immutable_host_suffix", "checks", "qa_require_revision"} & set(data)), "invalid Arete client route")
    else:
        identity = data.get("identity_path")
        need(isinstance(identity, str) and re.fullmatch(r"/[A-Za-z0-9/_.-]+", identity)
             and not ({".", "..", ""} & set(identity[1:].split("/"))), "provide a revision identity JSON path")
        for env in {data["destination"], "production"}:
            server_url(data.get(env))
        need(isinstance(data.get("instructions"), str) and 0 < len(data["instructions"]) <= 500,
             "record the user action QA should perform")
        if data["destination"] == "staging":
            need(config.valid_target_branch(data.get("production_branch")), "invalid production branch")
            need(type(data.get("qa_require_revision", False)) is bool, "qa_require_revision must be a boolean")
            if "checks" in data:
                gates = data["checks"]
                need(isinstance(gates, dict) and set(gates) == {"pr", "staging"}, "declare PR and staging checks")
                for gate in gates.values():
                    need(isinstance(gate, dict) and set(gate) == {"app_id", "required", "allow_skipped"} and
                         type(gate["app_id"]) is int and gate["app_id"] > 0, "bind checks to a GitHub app ID")
                    for names in (gate["required"], gate["allow_skipped"]):
                        need(isinstance(names, list) and len(names) <= 30 and all(isinstance(n, str) and 0 < len(n) <= 100
                             for n in names), "checks need explicit job names")
                    need(gate["required"] and not set(gate["required"]).intersection(gate["allow_skipped"]),
                         "require a passing job and use distinct optional job names")
            if suffix := data.get("immutable_host_suffix"):
                need(isinstance(suffix, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]+", suffix)
                     and server_url(data["staging"]).hostname.endswith("." + suffix), "invalid preview hostname suffix")
            need(isinstance(data.get("qa"), list) and bool(data["qa"])
                 and all(isinstance(s, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", s) for s in data["qa"]),
                 "configure the authorized QA tracker accounts")
            need(data["staging"].rstrip("/") != data["production"].rstrip("/"), "use distinct staging and production origins")


def install(repo, ticket, source, observer_file=None):
    from . import release_profile

    promise = json.loads(Path(source).read_bytes())
    profile = release_profile.current(repo)
    value = release_profile.apply(profile, promise) if profile else promise
    validate(value)
    file = policy_file(repo.name, ticket)
    if value["destination"] == "arete-serve":
        need(config.load_required().arete_repo == repo.name, "use arete-serve only on the Arete repository")
    else:
        need(repo.pipeline and repo.target_branch and repo.landing and repo.unattended_environment == value["destination"],
             "the goal must use an enabled pipeline consistent with the saved environment ceiling")
        if value["destination"] == "staging":
            need(repo.landing == "pr" and repo.target_branch != value["production_branch"], "use PR landing on a separate staging branch")
    if observer_file:
        program = Path(observer_file).read_bytes()
        need(len(program) <= 65536 and hashlib.sha256(program).hexdigest() == value.get("observer_sha256"),
             "protected observer does not match the policy fingerprint")
        dest = observer_location(value["observer_sha256"])
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        util.write_bytes_atomic(dest, program)
    if "observer_sha256" in value:
        observer_bytes(value)
    with util.lock("release-" + key(repo.name, ticket), wait=True):
        need(release_profile.current(repo) == profile, "the release profile changed during enrollment")
        serve_guard.write(file, {"repository_profile": digest(profile), "promise": promise} if profile else value)
    return {"repo": repo.name, "ticket": ticket, "policy": digest({**value, "repository_profile": digest(profile)} if profile else value),
            "destination": value["destination"]}


def lookup(document, field):
    value = document
    for part in pointer(field):
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise AssertionError(f"field {field} is absent")
    return value


def compare(document, rows):
    observed = []
    for row in rows:
        field = row["field"]
        actual = lookup(document, field)
        if "count" in row:
            members = lookup(document, row["count"])
            if not isinstance(members, list):
                raise AssertionError(f"{row['count']} is not an array")
            expected = sum(all(type(v := lookup(item, f)) is type(w) and v == w for f, w in row["where"].items())
                           for item in members)
        else:
            expected = row["equals"]
        if type(actual) is not type(expected) or actual != expected:
            raise AssertionError(f"wrong value at {field}")
        # Persist only expected-value fingerprints, never arbitrary response contents.
        observed.append({"field": field, "value_sha256": digest(expected)})
    return observed


def observer_location(sha):
    return paths.CONFIG_DIR / "release-observers" / (sha + ".py")


def observer_bytes(data):
    sha = data["observer_sha256"]
    program = zte.private_bytes(observer_location(sha), 65536)
    need(hashlib.sha256(program).hexdigest() == sha, "protected observer fingerprint changed")
    return program


def http_json(origin, path):
    url = server_url(origin)
    transport = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    conn = transport(url.hostname, url.port, timeout=8)
    try:
        conn.request("GET", path, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
        response = conn.getresponse()
        need(response.status == 200, "the deployed endpoint is not ready")
        raw = response.read(LIMIT + 1)
        need(len(raw) <= LIMIT, "release response exceeded the size limit")
        return json.loads(raw)
    finally:
        conn.close()


def deployed_revision(data, origin):
    value = http_json(origin, data["identity_path"])
    revision = value.get("revision") if isinstance(value, dict) else None
    need(isinstance(revision, str) and serve_guard.SHA.fullmatch(revision), "the deployment did not identify its revision")
    return revision


def browser_observation(data, origin, revision, destination):
    program = observer_bytes(data)
    with tempfile.TemporaryDirectory(prefix="arete-release-observer-") as folder:
        env = {"HOME": folder, "TMPDIR": folder, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
        result = util.run_whole_tree([sys.executable, "-I", "-c", program.decode()], 120,
                                     json.dumps({"origin": origin, "path": data["path"], "revision": revision,
                                                 "destination": destination}).encode(),
                                     cwd=folder, env=env, graceful_timeout=True)
    need(result.returncode == 0 and len(result.stdout) <= LIMIT, "protected browser observer could not complete")
    return json.loads(result.stdout)


def fetch(data, revision, destination, origin=None):
    path = data["path"]
    if destination == "arete-serve":
        deadline = time.monotonic() + 8
        # The local installation must agree with the engine stamp on this response.
        need(codebase.RELEASE == revision, "the running Arete engine has a different release")
        where = client.server(deadline, where=client.local_server()[0])
        response = client.request("GET", path, where=where, deadline=deadline)
        try:
            need(response.status == 200 and response.getheader("X-Arete-Engine") == revision[:12],
                 "the normal client did not observe the selected running engine")
            raw = response.read(LIMIT + 1)
        finally:
            client.close_response(response)
    else:
        origin = origin or data[destination]
        need(deployed_revision(data, origin) == revision, "the environment is serving a different release")
        document = (browser_observation(data, origin, revision, destination) if "observer_sha256" in data
                    else http_json(origin, path))
        need(deployed_revision(data, origin) == revision, "the destination changed while observing behavior")
        return document
    need(len(raw) <= LIMIT, "release response exceeded the size limit")
    return json.loads(raw)


def observe(repo, ticket, revision, *, destination=None, recovery=False, pinned="", origin=None, check_destination=None):
    need(isinstance(revision, str) and serve_guard.SHA.fullmatch(revision), "a full release revision is required")
    data = goal(repo, ticket)
    need(data, "no protected release check is enrolled for this goal")
    policy = digest(data)
    need(not pinned or pinned == policy, "the protected release policy changed")
    destination = destination or data["destination"]
    need(destination == data["destination"] or data["destination"] == "staging" and destination == "production",
         "destination is outside this policy")
    need(destination != "staging" or check_destination, "use the verified staging deployment for an observation")
    proof = {"repo": repo, "ticket": ticket, "revision": revision, "destination": destination,
             "policy": policy, "path": data["path"], "recovery": recovery}
    if origin:
        url = server_url(origin)
        suffix = data.get("immutable_host_suffix")
        need(destination == "staging" and suffix and url.scheme == "https" and url.port is None
             and url.hostname.endswith("." + suffix), "untrusted immutable preview URL")
        proof["origin"] = origin
    try:
        if check_destination:
            check_destination()
        body = fetch(data, revision, destination, origin)
        if check_destination:
            check_destination()
        proof["observed"] = compare(body, data["recovery" if recovery else "assertions"])
        proof["status"] = "verified"
    except AssertionError as error:
        proof.update(status="failed", reason=str(error))
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired, http.client.HTTPException, client.Broken, client.Down, client.Failed):
        proof.update(status="blocked", reason="could not identify or observe the requested release")
    rid = digest(proof)
    proof["receipt_id"], proof["at"] = rid, util.iso()
    label = "recovery" if recovery else "behavior"
    location = state_dir(repo, ticket) / revision / destination / label
    with util.lock("release-" + key(repo, ticket), wait=True):
        need(digest(goal(repo, ticket)) == policy, "policy changed during observation")
        if not (record := serve_guard.read(location / (rid + ".json"))):
            serve_guard.write(location / (rid + ".json"), proof)
            record = proof
        previous = serve_guard.read(location / "latest.json")
        serve_guard.write(location / "latest.json", record)
    if not recovery:
        try:
            from . import cohorts
        except ImportError:
            pass
        else:
            if proof["status"] == "verified" or (proof["status"] == "failed" and previous and previous["status"] == "verified"):
                cohorts.record_release(repo, ticket, revision, rid, destination, regression=proof["status"] == "failed")
    return record


def evidence(repo: str, ticket: str, revision: str) -> dict | None:
    data = goal(repo, ticket)
    if not data or not serve_guard.SHA.fullmatch(revision or ""):
        return None
    location = state_dir(repo, ticket) / revision
    landed = serve_guard.read(location / "landed.json")
    if landed and landed["policy"] == digest(data):
        revision = landed["revision"]
        location = state_dir(repo, ticket) / revision
    proof = serve_guard.read(location / data["destination"] / "behavior" / "latest.json")
    if proof and (proof.get("status"), proof.get("policy"), proof.get("revision")) == ("verified", digest(data), revision):
        return proof
    return None


def completion(repo: str, ticket: str, revision: str) -> dict | None:
    data = goal(repo, ticket)
    if not data:
        return None
    if data["destination"] != "staging":
        return evidence(repo, ticket, revision)
    state = serve_guard.read(state_dir(repo, ticket) / "promotion.json") or {}
    if (state.get("phase"), state.get("revision"), state.get("policy")) != ("production_verified", revision, digest(data)):
        return None
    receipt = serve_guard.read(state_dir(repo, ticket) / revision / "production" / "behavior" / "latest.json") or {}
    return receipt if receipt.get("status") == "verified" and receipt.get("receipt_id") == state.get("production_receipt") else None


def for_delivery(repo, ticket, revision, environment):
    data = goal(repo, ticket)
    if not data:
        return None
    where = (environment or "").strip().rstrip("/").casefold()
    if data["destination"] == "staging":
        if where in ("production", data["production"].rstrip("/").casefold()):
            return completion(repo, ticket, revision)
        if where not in ("", "staging", data["staging"].rstrip("/").casefold()):
            return None
    elif where:
        names = {data["destination"], str(data.get(data["destination"], "")).rstrip("/").casefold()}
        if data["destination"] == "arete-serve":
            names.add("production")
        if where not in names:
            return None
    return evidence(repo, ticket, revision)


def enrolled(repo):
    folder = paths.CONFIG_DIR / "release-checks" / repo
    return sorted(file.stem for file in folder.glob("*.json"))


def missing_staging_promise(repo, ticket):
    from . import release_profile

    if not isinstance(repo, config.Repo) or not release_profile.exists(repo.name):
        return ""
    release_profile.current(repo)
    if not ticket or not goal(repo.name, str(ticket)):
        return "Awaiting a protected ticket promise for the enrolled staging profile"
    return ""


def pr_check_options(repo, ticket, github, base):
    if not ticket or not isinstance(repo, config.Repo) or repo.github != github or not repo.pipeline or repo.target_branch != base:
        return {}
    if repo.unattended_environment == "staging" and required(repo.name, ticket):
        data = goal(repo.name, str(ticket))
        if data and data["destination"] == "staging" and data.get("checks") and data["production_branch"] != base:
            return {"stage_checks": data["checks"]["pr"]}
    return {}


def merge_revision(repo, ticket, base):
    row = run_store.newest_live_whole_run(repo.name, ticket) or {}
    links = (row.get("pr_url") or "").split()
    if len(links) == 1 and gh.pr_repo(links[0]).lower() == repo.github.lower():
        pr = gh.pr_view(links[0])
        if pr.get("state") == "MERGED" and pr.get("baseRefName") == base:
            return (pr.get("mergeCommit") or {}).get("oid", "")
    return ""


def on_tip(repo, base, tip, previous=None):
    selected = []
    for ticket in enrolled(repo.name):
        data = goal(repo.name, ticket)
        if not data or data["destination"] != "arete-serve":
            continue
        revision = data.get("revision") or merge_revision(repo, ticket, base)
        interval = False
        if previous and revision and revision != tip and serve_guard.SHA.fullmatch(revision):
            def contains(ref):
                return util.sh(["git", "--no-replace-objects", "merge-base", "--is-ancestor", revision, ref],
                               cwd=repo.path, timeout=10).returncode == 0
            interval = contains(tip) and not contains(previous)
        if revision == tip or interval:
            selected.append({"ticket": ticket, "policy": digest(data), "change": revision})
    return selected


def known_good(repo, previous, checks):
    return all(observe(repo, row["ticket"], previous, recovery=True, pinned=row["policy"])["status"] == "verified"
               for row in checks)


def assess(repo, receipt):
    statuses = []
    for check in receipt.release_checks:
        try:
            result = observe(repo, check["ticket"], receipt.tip, pinned=check["policy"])
        except (Held, PermissionError):
            return "blocked"
        if result["status"] == "verified" and check.get("change") != receipt.tip:
            serve_guard.write(state_dir(repo, check["ticket"]) / check["change"] / "landed.json",
                              {"revision": receipt.tip, "policy": check["policy"]})
        statuses.append(result["status"])
    return "failed" if "failed" in statuses else "blocked" if "blocked" in statuses else "verified"


def restore_or_hold(checkout, receipt, say):
    reason = "the protected release check failed"
    try:
        if serve_guard.rollback(paths.STATE_DIR, checkout, paths.LOG_DIR, receipt, reason):
            return
        why = serve_guard.rollback_schema_refusal(paths.STATE_DIR, checkout, receipt.previous)
    except (OSError, RuntimeError) as error:
        why = str(error)
    held = replace(receipt, settled="release_held", restore_status=why or "checkout identity changed")
    serve_guard.write(serve_guard.hold_path(paths.STATE_DIR, receipt.tip), {**asdict(held), "reason": reason})
    serve_guard.write(paths.STATE_DIR / serve_guard.RECEIPT, asdict(held))
    say(f"deploy: held {receipt.tip}: {held.restore_status}")


def confirm_restore(repo, checkout, started_head, completed):
    if not completed:
        return
    raw = serve_guard.read(paths.STATE_DIR / serve_guard.RECEIPT) or {}
    if (raw.get("settled") != "rolled_back" or raw.get("checkout") != str(checkout) or not raw.get("release_checks")
            or raw.get("restore_status") == "verified" or raw["previous"][:12] != started_head):
        return
    outcome = "verified" if known_good(repo, raw["previous"], raw["release_checks"]) else "unverified"
    if raw.get("restore_status") != outcome:
        serve_guard.write(paths.STATE_DIR / serve_guard.RECEIPT, {**raw, "restore_status": outcome})


def required(repo, ticket):
    from . import release_profile

    # A missing ticket promise or malformed enrolled file still withholds tracker closure.
    if release_profile.exists(repo):
        return True
    try:
        return policy_file(repo, str(ticket)).lstat() is not None
    except FileNotFoundError:
        return False


def progress(repo, ticket):
    data = goal(repo.name, ticket)
    if not data:
        if missing_staging_promise(repo, ticket):
            return {"repo": repo.name, "ticket": ticket, "destination": "staging", "status": "waiting_for_promise",
                    "revision": "", "evidence": None, "completion": None}
        return None
    revision = data.get("revision") or merge_revision(repo, ticket, repo.target_branch or gh.default_branch(repo.github))
    proof = evidence(repo.name, ticket, revision) if revision else None
    last = (serve_guard.read(state_dir(repo.name, ticket) / revision / data["destination"] / "behavior" / "latest.json")
            if revision else None)
    return {"repo": repo.name, "ticket": ticket, "destination": data["destination"], "revision": revision,
            "policy": digest(data), "evidence": proof, "last_observation": last,
            "promotion": serve_guard.read(state_dir(repo.name, ticket) / "promotion.json"),
            "completion": completion(repo.name, ticket, revision) if revision else None}


def finish_verified(cfg, repo, ticket, revision, delivered=None):
    from . import tracker, unblock

    proof = delivered or evidence(repo.name, ticket, revision)
    if not proof:
        return
    tk = tracker.for_ticket(cfg, repo, ticket)
    item = tk.get(ticket)
    if "spec" in item.labels or item.blocker or not unblock.approval(tk, item)[0]:
        return
    reason = f"Verified {proof['path']} on {proof['destination']} at {revision}. Receipt {proof['receipt_id']}."
    if isinstance(tk, tracker.GitHubTracker):
        tk.require_ordinary(ticket)
        gh.issue_close(repo.github, int(ticket), gh.marked(reason))
    elif isinstance(tk, tracker.JiraTracker):
        data = goal(repo.name, ticket)
        branch = data.get("production_branch", repo.target_branch) if proof["destination"] == "production" else repo.target_branch
        tk.move_to(ticket, cfg.jira.landed.get(branch, ""))


def advance(cfg):
    for repo in cfg.repos:
        for ticket in enrolled(repo.name):
            try:
                data = goal(repo.name, ticket)
                if data and data["destination"] == "staging":
                    from . import release_promotion

                    promotion = release_promotion.advance(cfg, repo, ticket)
                    if promotion.get("phase") == "production_verified":
                        revision = promotion["revision"]
                        if receipt := completion(repo.name, ticket, revision):
                            finish_verified(cfg, repo, ticket, revision, receipt)
                    continue
                row = progress(repo, ticket)
                if row and row["evidence"] and row["destination"] == "arete-serve":
                    finish_verified(cfg, repo, ticket, row["revision"])
            except (ValueError, OSError, RuntimeError) as error:
                print(f"release {repo.name}/{ticket}: {type(error).__name__}", flush=True)


def command(args):
    cfg = config.load_required()
    repo = cfg.repo(args.repo)
    if args.action == "enroll":
        answer = install(repo, args.ticket, args.policy_file, args.observer_file)
    elif args.action == "observe":
        data = goal(repo.name, args.ticket)
        need(data is not None, "release goal not enrolled")
        current = data.get("revision") or merge_revision(repo, args.ticket, repo.target_branch or gh.default_branch(repo.github))
        need(current == args.revision, "proposed release differs from the goal's revision")
        if data["destination"] == "staging":
            from . import release_promotion

            answer = release_promotion.observe_staging(repo, args.ticket, current)
        else:
            answer = observe(repo.name, args.ticket, current)
    else:
        answer = progress(repo, args.ticket)
    print(json.dumps(answer, sort_keys=True))
    return 0 if answer is not None and answer.get("status", "verified") == "verified" else 1
