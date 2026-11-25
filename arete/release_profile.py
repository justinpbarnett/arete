"""Protected repository-wide staging authority shared by individual ticket promises."""
from __future__ import annotations

import json
from pathlib import Path

from . import paths, release_proof as proof, serve_guard, util, zte

FIELDS = {"destination", "staging", "production", "production_branch", "identity_path", "immutable_host_suffix",
          "checks", "qa", "qa_require_revision"}
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
        if key == "qa":
            people = promise[key]
            proof.need(isinstance(people, list) and people and all(isinstance(who, str) and who in settings["qa"] for who in people),
                       "ticket QA authority exceeds the enrolled repository profile")
        else:
            proof.need(key in settings and settings[key] == promise[key], "ticket " + key + " differs from the enrolled staging profile")
    return {**settings, **promise}
