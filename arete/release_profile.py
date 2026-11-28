"""Protected repository-wide staging authority shared by individual ticket promises."""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from . import paths, release_proof as proof, serve_guard, util, zte

FIELDS = {"destination", "staging", "production", "production_branch", "identity_path", "immutable_host_suffix",
          "checks", "qa", "qa_require_revision", "promotion_mode",
          "qa_authority", "release_owners", "require_release_owner", "release_workflow"}
TARGET_FIELDS = ("github", "tracker", "jira_site", "jira_project", "jira_label", "target_branch", "landing", "unattended_environment")


def location(name):
    proof.need(isinstance(name, str) and proof.NAME.fullmatch(name), "invalid release profile identity")
    return paths.CONFIG_DIR / "release-profiles" / (name + ".json")


def exists(name):
    try:
        return location(name).lstat() is not None
    except FileNotFoundError:
        return False


def read(name):
    try:
        saved = json.loads(zte.private_bytes(location(name), 32768))
        proof.need(isinstance(saved, dict) and set(saved) == {"binding", "settings"} and
                   isinstance(saved["binding"], dict) and set(saved["binding"]) == set(TARGET_FIELDS),
                   "invalid protected staging profile")
        validate(saved["settings"])
        return saved
    except FileNotFoundError:
        return None


def binding(repo):
    return {key: getattr(repo, key) for key in TARGET_FIELDS}


def validate(settings):
    proof.need(isinstance(settings, dict) and set(settings) <= FIELDS and settings.get("destination") == "staging",
               "a staging profile requires repository delivery settings only")
    example = [{"field": "/available", "equals": True}]
    proof.validate({**settings, "path": "/available", "instructions": "Verify the enrolled goal.",
                    "recovery": example, "assertions": example})


def prepare(repo, source):
    settings = json.loads(Path(source).read_bytes())
    validate(settings)
    proof.need(repo.pipeline and repo.github and repo.landing == "pr" and repo.target_branch and
               repo.unattended_environment == "staging" and repo.target_branch != settings["production_branch"],
               "a release profile requires an enabled staging PR enrollment with a separate production branch")
    return {"binding": binding(repo), "settings": settings}


def store(repo, profile):
    serve_guard.write(location(repo.name), profile)


def install(repo, source):
    from . import config

    profile = prepare(repo, source)
    with util.lock("config", wait=True):
        proof.need(config.load_required().repo(repo.name) == repo, "the repository enrollment changed")
        store(repo, profile)
    return profile


def current(repo):
    profile = read(repo.name)
    if profile:
        proof.need(profile["binding"] == binding(repo) and repo.pipeline, "the staging profile no longer matches repository enrollment")
    return profile


def apply(profile, promise):
    settings = profile["settings"]
    proof.need(isinstance(promise, dict), "a ticket promise must be a JSON object")
    for key in FIELDS.intersection(promise):
        if key in ("qa", "release_owners"):
            people = promise[key]
            allowed = settings.get(key) or []
            proof.need(isinstance(people, list) and people and all(isinstance(who, str) and who in allowed for who in people),
                       f"ticket {key} authority exceeds the enrolled repository profile")
        else:
            proof.need(key in settings and settings[key] == promise[key], "ticket " + key + " differs from the enrolled staging profile")
    return {**settings, **promise}


def standing_file(name):
    return paths.CONFIG_DIR / "production-grants" / location(name).name


def read_grant(repo):
    file = standing_file(repo.name)
    try:
        file.lstat()
        record = json.loads(zte.private_bytes(file, 65536))
    except FileNotFoundError:
        return None
    fields = {"binding", "repository_profile", "after_issue", "promise", "production_checks", "generation", "id"}
    proof.need(isinstance(record, dict) and set(record) == fields and
               record["id"] == proof.digest({k: v for k, v in record.items() if k != "id"}) and
               type(record["after_issue"]) is int and record["after_issue"] >= 0,
               "invalid protected production grant")
    return record


def standing(repo, ticket):
    """The current repository authority for this GitHub issue, independent of any QA comment."""
    ticket = str(ticket)
    if repo.tracker != "github" or not re.fullmatch(r"[1-9][0-9]{0,18}", ticket):
        return None
    grant = read_grant(repo)
    if not grant or int(ticket) <= grant["after_issue"] or grant["binding"] != binding(repo):
        return None
    profile = current(repo)
    if not profile or proof.digest(profile) != grant["repository_profile"]:
        return None
    try:
        promise = json.loads(zte.private_bytes(proof.policy_file(repo.name, ticket), 32768))
    except FileNotFoundError:
        return grant
    proof.need(isinstance(promise, dict), "invalid protected ticket promise")
    # A previous grant cannot lend its unmodified, already-staged promise to a new grant.
    if (promise.get("repository_profile") != grant["repository_profile"] or
            promise.get("standing_grant", grant["id"]) != grant["id"]):
        return None
    return grant


def set_standing(repo, after_issue, promise_file, observer_file=None, checks_file=None):
    from . import config

    proof.need(type(after_issue) is int and after_issue >= 0, "provide --after-issue with the last existing GitHub number")
    promise = json.loads(Path(promise_file).read_bytes())
    proof.need(isinstance(promise, dict) and not set(promise).intersection(FIELDS | {"revision"}),
               "use a reusable ticket promise without repository settings or a pinned revision")
    with util.lock("config", wait=True):
        proof.need(config.load_required().repo(repo.name) == repo, "the repository enrollment changed")
        profile = current(repo)
        proof.need(repo.tracker == "github" and profile and profile["settings"].get("checks") and
                   profile["settings"].get("immutable_host_suffix"),
                   "standing production requires a GitHub staging profile with immutable previews and required checks")
        value = apply(profile, promise)
        proof.validate(value)
        checks = (json.loads(Path(checks_file).read_bytes()) if checks_file else profile["settings"]["checks"]["staging"])
        proof.validate_checks(checks)
        proof.install_observer(value, observer_file)
        expected = {"binding": binding(repo), "repository_profile": proof.digest(profile), "after_issue": after_issue,
                    "promise": promise, "production_checks": checks}
        previous = read_grant(repo)
        if previous and all(previous[k] == v for k, v in expected.items()):
            return previous
        record = {**expected, "generation": secrets.token_hex(16)}
        record["id"] = proof.digest(record)
        frozen = {"repository_profile": record["repository_profile"], "standing_grant": record["id"], "promise": promise}
        proof.need(len(json.dumps(frozen).encode()) <= 32768 and len(json.dumps(record).encode()) <= 65536,
                   "the reusable promise or production grant exceeds its storage limit")
        serve_guard.write(standing_file(repo.name), record)
        return record


def revoke_standing(repo):
    file = standing_file(repo.name)
    with util.lock("config", wait=True):
        try:
            file.unlink()
        except FileNotFoundError:
            return
        serve_guard.flush_directory(file.parent)


def standing_status(repo, profile):
    grant = read_grant(repo)
    if not grant:
        return None
    active = bool(profile and grant["binding"] == binding(repo) and grant["repository_profile"] == proof.digest(profile))
    return {"active": active, **{field: grant[field] for field in ("id", "after_issue", "promise", "production_checks")}}


def admit(repo, ticket, run_id=""):
    """Freeze the fallback promise on the first real factory admission and preserve explicit enrollment."""
    grant = standing(repo, ticket)
    if run_id and repo.tracker == "github" and ticket.isascii() and ticket.isdecimal() and exists(repo.name):
        proof.new_release_run(repo, ticket, run_id)
    if not grant:
        return
    file = proof.policy_file(repo.name, ticket)
    with util.lock("release-" + proof.key(repo.name, ticket), wait=True):
        try:
            file.lstat()
            return
        except FileNotFoundError:
            pass
        with util.lock("config", wait=True):
            if standing(repo, ticket) == grant:
                serve_guard.write(file, {"repository_profile": grant["repository_profile"], "standing_grant": grant["id"],
                                        "promise": grant["promise"]})
