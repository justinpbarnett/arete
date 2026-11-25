"""Run Arete's owner-approved behavior check on a disposable GitHub-hosted Mac."""

from __future__ import annotations

import hashlib
import re
import time
import tomllib
from urllib.parse import urlencode

from . import check_repair, factory, gh, paths, pilot, util

REPO = "justinpbarnett/arete"
WORKFLOW = ".github/workflows/ci.yml"
PREFIX = "arete-check/"
MAX_WAIT = 12 * 60


def eligible(job, failures):
    run = job.run
    return (not paths.ISOLATED and getattr(run, "path", "") == "orchestrate" and not run.dry_run and run.repo.name == "arete"
            and run.repo.github == REPO and str(run.tid).isascii() and str(run.tid).isdigit()
            and len(job.checkouts) == 1 and job.checkouts[0].github == REPO and job.checkouts[0].base == "main"
            and job.layer.checks.get("behavior") in (check_repair.PARALLEL, check_repair.ARETE_SUITE)
            and len(failures) == 1 and failures[0]["name"] == "behavior"
            and failures[0].get("capability") == check_repair.MACOS_PROFILE)


def pin():
    try:
        policy = tomllib.loads((paths.CONFIG_DIR / "hosted-macos.toml").read_text())
    except FileNotFoundError:
        return ""
    value = policy.get("workflow_blob")
    pilot.require(set(policy) == {"repo", "workflow_blob"} and policy["repo"] == REPO and isinstance(value, str)
                  and pilot.SHA.fullmatch(value), "invalid hosted-macos.toml enrollment")
    return value


def current(result):
    receipt = result.get("hosted_check")
    if not receipt:
        return True
    try:
        return (receipt.get("status") == "success" and receipt.get("exit") == 0
                and receipt.get("workflow_blob") == pin() and bool(receipt.get("workflow_blob")))
    except (OSError, RuntimeError, ValueError):
        return False


def git(wt, *args, **kw):
    return util.git(list(args), wt, timeout=30, retry=False, **kw).strip()


def prepare(job, tree, workflow_blob, policy):
    checkout = job.checkouts[0]
    wt = checkout.wt
    pilot.check_origin(wt, REPO)
    pilot.protected(REPO)
    main = pilot.origin(REPO)
    pilot.require(pilot.SHA.fullmatch(main), "protected main is unavailable")
    git(wt, "fetch", "--no-tags", "--no-write-fetch-head", "origin", main)
    pilot.require(git(wt, "rev-parse", f"{main}:{WORKFLOW}") == workflow_blob,
                  "the enrolled workflow has changed on main")
    staged = job.b.restage(from_base=True)
    job.b.validate_diff(staged, "preparing the hosted check", from_base=True)
    base = staged.bases[REPO]
    pilot.require(git(wt, "merge-base", main, base) == git(wt, "rev-parse", base)
                  and tree == git(wt, "write-tree") == factory.fingerprint(wt),
                  "the candidate or its public comparison base changed")
    # Only the admitted tree and an already public parent go to GitHub. Local
    # history can contain discarded intermediate edits and must stay local.
    # The ordinary full suite still includes the candidate's new test cases.
    # Its discovery command must be the same one that the published base used.
    controls = ("tests/parallel.py", "tests/_parallel_site", "tests/__init__.py")
    pilot.require(not git(wt, "diff", "--name-only", base, tree, "--", *controls),
                  "the candidate changes the protected full-suite runner")
    witness_tree = git(wt, "rev-parse", f"{base}:tests")
    workflows = git(wt, "rev-parse", f"{main}:.github/workflows")
    key = hashlib.sha256(f"{tree}:{base}:{policy}:{workflows}".encode()).hexdigest()
    branch = PREFIX + key
    ref = "refs/heads/" + branch

    def remote():
        return git(wt, "ls-remote", "origin", ref).partition("\t")[0]

    marker = remote()
    if not marker:
        candidate = git(wt, *util.ARETE_IDENTITY, "commit-tree", tree, "-p", base, "-m", "Arete check candidate")
        # GitHub selects workflow files from the pushed tree. The protected
        # first parent supplies them; the checkout step selects the second.
        marker = git(wt, *util.ARETE_IDENTITY, "commit-tree", f"{main}^{{tree}}", "-p", main, "-p", candidate,
                     "-m", f"Arete candidate check {key}")
        if job.check_publication_targets:
            job.check_publication_targets()
        try:
            git(wt, "push", "-q", "--no-follow-tags", "--recurse-submodules=no", f"--force-with-lease={ref}:",
                "origin", f"{marker}:{ref}")
        except RuntimeError:
            # Unknown delivery or a competing launch: consume only the same
            # immutable request. An ordinary poll never force-updates the ref.
            marker = remote()
            if not marker:
                raise
    pilot.require(pilot.SHA.fullmatch(marker), "the verification ref is unavailable")
    git(wt, "fetch", "--no-tags", "--no-write-fetch-head", "origin", marker)
    parent = git(wt, "rev-parse", f"{marker}^1")
    candidate = git(wt, "rev-parse", f"{marker}^2")
    pilot.require(git(wt, "merge-base", parent, main) == parent
                  and git(wt, "show", "-s", "--format=%s", marker) == f"Arete candidate check {key}"
                  and git(wt, "rev-parse", f"{marker}^{{tree}}") == git(wt, "rev-parse", f"{parent}^{{tree}}")
                  and git(wt, "rev-parse", f"{parent}:.github/workflows") == workflows
                  and git(wt, "rev-parse", f"{candidate}^1") == base
                  and git(wt, "rev-parse", f"{candidate}^{{tree}}") == tree,
                  "the hosted check request does not match the admitted candidate and workflow")
    return {"repo": REPO, "branch": branch, "head": marker, "candidate": candidate, "tree": tree,
            "policy": policy, "workflow_blob": workflow_blob, "workflows_tree": workflows, "witness_base": base,
            "witness_tests": witness_tree, "status": "pending"}


def result(receipt):
    params = urlencode({"event": "push", "branch": receipt["branch"], "head_sha": receipt["head"]})
    page = gh.gh_json(["api", f"repos/{REPO}/actions/workflows/ci.yml/runs?{params}"], retry=False, timeout=20)
    rows = page["workflow_runs"]
    if not rows:
        return
    pilot.require(len(rows) == 1 and page["total_count"] == 1, "ambiguous hosted check")
    row = rows[0]
    run, attempt = row["id"], row["run_attempt"]
    pilot.require(row["event"] == "push" and row["head_sha"] == receipt["head"] and row["path"] == WORKFLOW
                  and row["head_branch"] == receipt["branch"] and row["head_repository"]["full_name"] == REPO
                  and type(run) is int and type(attempt) is int and run > 0 and attempt > 0,
                  "GitHub returned a different hosted check")
    receipt.update(run=run, attempt=attempt, url=f"https://github.com/{REPO}/actions/runs/{run}/attempts/{attempt}")
    if row["status"] == "completed":
        receipt["status"] = row["conclusion"]
        if row["conclusion"] in ("success", "failure"):
            jobs = gh.workflow_attempt_jobs(REPO, receipt["head"], run, attempt, retry=False, timeout=20)
            pilot.require(len(jobs) == 1 and jobs[0]["name"] == "arete-checks" and "macos-15" in jobs[0]["labels"]
                          and jobs[0]["conclusion"] == row["conclusion"], "the hosted macOS job is unidentified")
            passed = {s["name"] for s in jobs[0]["steps"] if s["conclusion"] == "success"}
            exits = [int(m[1]) for name in passed if (m := re.fullmatch(r"Behavior suite exit (\d{1,3})", name))]
            if len(exits) == 1 and exits[0] <= 255:
                receipt["exit"] = exits[0]
            if row["conclusion"] == "success":
                required = {"Select check candidate", "Existing behavior checks", "Observe the protected worker isolation check",
                            "Python lint, compilation and imports", "JavaScript syntax"}
                pilot.require(receipt.get("exit") == 0 and required <= passed, "the full hosted macOS check did not run")


def check(job, tree, failures):
    if not eligible(job, failures):
        return None
    receipt = {"status": "pending", "tree": tree}
    try:
        workflow_blob = pin()
        if not workflow_blob:
            return None
        policy = job.policy("checks", {"quick": False})
        receipt = prepare(job, tree, workflow_blob, policy)
        until = time.monotonic() + min(MAX_WAIT, job.run.time_for(MAX_WAIT))
        with job.run.waiting("ci"):
            while True:
                result(receipt)
                job.run.save("hosted-check.json", receipt)
                if receipt["status"] != "pending" or time.monotonic() >= until:
                    break
                time.sleep(min(10, max(0, until - time.monotonic())))
        pilot.require(workflow_blob == pin() and policy == job.policy("checks", {"quick": False})
                      and tree == factory.fingerprint(job.ws), "the tree, workflow enrollment or check policy changed")
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        receipt.update(status="blocked", error=util.first_line(str(error), 360))
        receipt.pop("exit", None)
        job.run.save("hosted-check.json", receipt)
    return receipt
