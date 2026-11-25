from __future__ import annotations

import json
import re
from pathlib import Path

from . import config, gh, jira, paths, refusals, settings, tracker, util


class Refused(refusals.Invalid, ValueError):
    pass


def unused(cfg: config.Config, name: str, checkout: Path, github: str) -> None:
    if any(r.name == name or r.path.resolve() == checkout or github and r.github.casefold() == github.casefold()
           for r in cfg.repos):
        raise Refused("That repo name, checkout or GitHub repository is already enrolled; config.toml is unchanged.")


def config_snapshot() -> bytes | None:
    try:
        return paths.read_confined(paths.CONFIG_FILE)
    except FileNotFoundError:
        return None


def enroll(name: str, checkout: Path, *, target_branch: str, landing: str = "pr",
           unattended_environment: str = "none", destination: str = "", jira_project: str = "",
           jira_label: str = "", pipeline: bool = False, choose_project=None) -> config.Repo:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise Refused("Use a repo name containing letters, numbers, hyphens or underscores.")
    checkout = paths.expand(checkout).resolve()
    if not checkout.is_dir():
        raise Refused("Enrollment needs the root of a local Git checkout.")
    root = util.git(["rev-parse", "--show-toplevel"], checkout, check=False).strip()
    if not root or Path(root).resolve() != checkout:
        raise Refused("Enrollment needs the root of a local Git checkout.")
    github = gh.origin_project(util.git(["remote", "get-url", "origin"], checkout, check=False).strip())
    with util.lock("config", wait=True):
        original = config_snapshot()
        cfg = config.load()
        unused(cfg, name, checkout, github)
    owner = github.partition("/")[0]
    team = owner and owner == cfg.jira.github_owner.casefold()
    destination = destination or ("jira" if jira_project or team else "github")
    if destination not in ("github", "jira"):
        raise Refused("Enrollment needs a GitHub or Jira tracker destination.")
    if not github and (destination == "github" or pipeline and landing == "pr"):
        raise Refused("GitHub issues and PR delivery need a github.com origin remote.")
    if destination != "jira" and (jira_project or jira_label):
        raise Refused("A Jira project or route label requires the Jira tracker.")
    row = {"name": name, "path": str(checkout), "github": github, "tracker": destination,
           "target_branch": target_branch, "landing": landing, "unattended_environment": unattended_environment,
           "pipeline": pipeline}
    config.repo_table(row)
    if destination == "jira":
        project = jira_project or cfg.jira.project
        if not project and choose_project:
            project = choose_project()
        if not isinstance(project, str) or not project.strip():
            raise Refused("No Jira project selected; enroll with --jira-project KEY. config.toml is unchanged.")
        if jira_label and not re.fullmatch(r"[A-Za-z0-9_.-]+", jira_label):
            raise Refused("Use one Jira route label containing letters, numbers, dots, underscores or hyphens.")
        client = jira.Client.from_env(cfg.jira.env_file)
        project = client.project_key(project.strip().upper())
        route = jira_label or f"repo-{name}"
        if any(r.tracker == "jira" and r.jira_site in ("", client.base)
               and tracker.jira_project(r, cfg.jira).casefold() == project.casefold()
               and tracker.route_label(r).casefold() == route.casefold() for r in cfg.repos):
            raise Refused("That Jira project and route label already belong to an enrolled repo.")
        row.update(jira_project=project, jira_site=client.base, jira_label=route)
    expected = config.repo_table(row)

    def verify(saved):
        return ("Enrollment did not read back as requested; config.toml is unchanged."
                if saved.repos != [*cfg.repos, expected] else "")

    with util.lock("config", wait=True):
        if config_snapshot() != original:
            raise Refused("config.toml changed during enrollment; try again.")
        text = (original or b"").decode()
        if text and not text.endswith("\n"):
            text += "\n"
        record = {key: json.dumps(value, ensure_ascii=False) for key, value in row.items()}
        settings.write_checked(text + "\n[[repo]]\n" + "".join(f"{key} = {value}\n" for key, value in record.items()), verify)
    return expected


def show(cfg: config.Config, repo: config.Repo) -> dict:
    layer = config.load_layer(repo.layer_dir) if not repo.target_branch or not repo.landing else None
    destination = {"tracker": repo.tracker}
    if repo.tracker == "github":
        destination["github"] = repo.github
    elif repo.tracker == "jira":
        destination.update(project=tracker.jira_project(repo, cfg.jira), route=tracker.route_label(repo),
                           site=repo.jira_site or None)
    return {"name": repo.name, "destination": destination,
            "target_branch": {"value": repo.target_branch or layer.base_branch or None,
                              "source": "repo" if repo.target_branch else "legacy"},
            "landing": {"value": repo.landing or None, "source": "repo" if repo.landing else "legacy",
                        **({"trust": layer.trust_level} if not repo.landing else {})},
            "unattended_environment": {"value": repo.unattended_environment or None,
                                       "source": "repo" if repo.unattended_environment else "legacy"}}
