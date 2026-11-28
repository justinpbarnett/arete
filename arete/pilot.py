from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.request import urlopen

from . import config, delegation, gh, jev, paths, principals, slots, steps, util, verify, workspace
from .review import Review
from .store import db, orchestration, runs, tasks

BRANCH = "main"
DOC = "README.md"
TASK_TITLE = "Correct one README line"
TASK_KIND = "fix"
CHECK = {"context": "arete-checks", "app_id": 15368}
SHA = re.compile(r"[0-9a-f]{40}\Z")
PILOT_TASK = re.compile(r"t-[0-9a-f]{32}\Z")


def required_check(cfg, repo, layer):
    return layer.required_check or (CHECK if repo.name == cfg.arete_repo else None)


class Held(RuntimeError):
    pass


def require(condition, why: str) -> None:
    if not condition:
        raise Held(why)


@dataclass(frozen=True)
class Signal:
    github: str
    base: str
    before: str
    after: str

    def expected(self, source: str) -> str:
        require(SHA.fullmatch(self.base), "the signal needs a full base SHA")
        require(self.before != self.after and all(0 < len(line) <= 160 and line.isprintable()
                                                  for line in (self.before, self.after)),
                "the pilot takes one printable README line replacement (up to 160 characters)")
        lines = source.splitlines(keepends=True)
        contents = [line.splitlines()[0] for line in lines]
        require(contents.count(self.before) == 1 and self.after not in contents,
                "the approved old line must appear once and the replacement must not already exist")
        index = contents.index(self.before)
        lines[index] = self.after + lines[index][len(self.before):]
        return "".join(lines)

    @property
    def task_id(self) -> str:
        return "t-" + orchestration.digest(asdict(self))[:32]

    def task(self) -> str:
        return (f"In {DOC}, replace this exact line:\n\n{self.before}\n\nwith:\n\n{self.after}\n\n"
                "Change no other bytes or paths. Verify both that the old line is gone and that the new line "
                "appears once by reading the actual README. Do not start or mutate a service.")


def candidate_time_left(until: float, cap: float) -> float:
    remaining = until - time.monotonic()
    require(remaining > 0, "ZTE deadline expired during candidate revalidation")
    return min(remaining, cap)


def candidate_git(repo: Path, args: tuple[str, ...], until: float, cap: float) -> subprocess.CompletedProcess:
    from . import sandbox

    candidate_time_left(until, cap)
    command = sandbox.protected_command(["git", *args], repo, ())
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "GIT_CONFIG_GLOBAL",
                                            "GIT_CONFIG_NOSYSTEM") if key in os.environ}
    env.update(GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0="core.fsmonitor", GIT_CONFIG_VALUE_0="false")
    until = min(until, time.monotonic() + cap)
    candidate_time_left(until, cap)
    with subprocess.Popen(command, cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, start_new_session=True) as process:
        try:
            output, _ = process.communicate(timeout=candidate_time_left(until, cap))
        except BaseException as error:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            if isinstance(error, subprocess.TimeoutExpired):
                raise Held(f"ZTE candidate revalidation timed out in git {args[0]}") from None
            raise
    candidate_time_left(until, cap)
    return subprocess.CompletedProcess(command, process.returncode, output)


def git(repo: Path, *args: str, until: float | None = None) -> str:
    if until is None:
        return util.git(list(args), repo, timeout=30)
    result = candidate_git(repo, args, until, 30)
    require(result.returncode == 0, f"ZTE candidate revalidation failed in git {args[0]}")
    return result.stdout.decode("utf-8")


def readme(repo: Path, ref: str, *, until: float | None = None) -> str:
    args = ("show", f"{ref}:{DOC}")
    if until is None:
        from . import environment

        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=10, check=False,
                                env=environment.build(()))
    else:
        result = candidate_git(repo, args, until, 10)
    require(result.returncode == 0, "the approved README blob is unavailable")
    return result.stdout.decode("utf-8")


def origin(github: str) -> str:
    row = gh.gh_json(["api", f"repos/{github}/branches/{BRANCH}"], timeout=20) or {}
    return row.get("commit", {}).get("sha", "")


def protected(github: str) -> None:
    row = gh.gh_json(["api", f"repos/{github}/branches/{BRANCH}/protection"], timeout=20) or {}
    status = row.get("required_status_checks") or {}
    require(status.get("strict") is True and CHECK in status.get("checks", [])
            and row.get("enforce_admins", {}).get("enabled") is True
            and row.get("allow_force_pushes", {}).get("enabled") is False
            and row.get("allow_deletions", {}).get("enabled") is False,
            "GitHub is not enforcing the expected strict Actions check for all actors")


def check_origin(repo: Path, github: str, *, until: float | None = None) -> None:
    approved = ([f"git@github.com:{github}.git"], [f"https://github.com/{github}.git"])
    for flags in ((), ("--push",)):
        require(git(repo, "remote", "get-url", *flags, "--all", "origin", until=until).splitlines() in approved,
                "the checkout origin must have exactly one approved GitHub fetch and push URL")


def have_checkout(repo: Path, base: str) -> None:
    require(git(repo, "symbolic-ref", "--short", "HEAD").strip() == BRANCH
            and git(repo, "rev-parse", "HEAD").strip() == base
            and not git(repo, "status", "--porcelain"), "the deployment checkout changed or is dirty")


def require_exact_tree(repo: Path, signal: Signal, tree: str, expected: str, *, until: float | None = None) -> None:
    require(SHA.fullmatch(tree), "the candidate tree is not known")
    base_entry = git(repo, "ls-tree", signal.base, DOC, until=until).split()
    candidate_entry = git(repo, "ls-tree", tree, DOC, until=until).split()
    require(base_entry[:2] in (["100644", "blob"], ["100755", "blob"])
            and candidate_entry[:2] == base_entry[:2]
            and git(repo, "diff", "--name-status", "--no-renames", "--ignore-submodules=none", "--no-ext-diff",
                    signal.base, tree, until=until).strip() == f"M\t{DOC}"
            and readme(repo, tree, until=until) == expected, "the candidate is not the approved regular-file README edit")


def exact_edit(repo: Path, signal: Signal, head: str, expected: str, *, until: float | None = None) -> str:
    require(SHA.fullmatch(head), "the published head is not known")
    require(git(repo, "rev-list", "--parents", "-n", "1", head, until=until).split() == [head, signal.base],
            "the published head is not a single commit directly on the approved base")
    tree = git(repo, "rev-parse", f"{head}^{{tree}}", until=until).strip()
    require_exact_tree(repo, signal, tree, expected, until=until)
    return tree


def receipt_for(tid: str) -> Path:
    return paths.STATE_DIR / "pilot-doc-fix" / tid / "receipt.json"


def is_pilot_task(tid: str) -> bool:
    return bool(PILOT_TASK.fullmatch(tid))


def private(path: Path, expected_type) -> bool:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    return expected_type(st.st_mode) and st.st_uid == os.geteuid() and st.st_mode & 0o077 == 0


def task_matches(tid: str, repo: str, signal: Signal, state: str | None = None) -> bool:
    task = tasks.task_get(tid)
    return bool(task and task["repo"] == repo and task["body"] == signal.task()
                and task["title"] == TASK_TITLE and task["kind"] == TASK_KIND and not task["parent"]
                and (state is None or task["state"] == state))


def live_receipt(tid: str, repo: str) -> dict:
    path = receipt_for(tid)
    require(private(path.parent, stat.S_ISDIR) and private(path, stat.S_ISREG),
            "the one-shot task has no private local authorization receipt")
    receipt = util.read_json(path, {})
    require(isinstance(receipt, dict), "the one-shot task receipt is unreadable")
    deadline, pid = receipt.get("deadline"), receipt.get("owner_pid")
    require(receipt.get("task") == tid and receipt.get("repo") == repo and receipt.get("phase") == "waiting_for_poll"
            and type(deadline) in (int, float) and math.isfinite(deadline) and time.monotonic() < deadline
            and type(pid) is int and pid > 0 and receipt.get("owner_identity") is not None
            and delegation.identity_of(pid) == receipt["owner_identity"],
            "the one-shot task has no live unexpired authorization")
    signal = Signal(**receipt["signal"])
    require(signal.task_id == tid and task_matches(tid, repo, signal),
            "the one-shot task changed or no longer exists")
    return receipt


def task_admission(tid: str, repo: str) -> dict:
    receipt = live_receipt(tid, repo)
    require(not runs.ticket_runs(repo, tid, limit=1, include_queued=False), "the one-shot task already has a run")
    require(task_matches(tid, repo, Signal(**receipt["signal"]), "todo"), "the one-shot task is no longer queued")
    return receipt


def pickable(tid: str, repo: str) -> bool:
    if not is_pilot_task(tid):
        return True
    try:
        task_admission(tid, repo)
        return True
    except Held:
        return False


def scheduled_run(repo: str, tid: str, *, until: float | None = None) -> dict | None:
    if until is not None:
        candidate_time_left(until, 1)
    with db.one_read(timeout=0 if until is not None else None) as connection:
        if until is not None:
            candidate_time_left(until, 1)
        rows = runs.ticket_runs(repo, tid, limit=2, whole=True)
        require(len(rows) <= 1, "the local task had more than one delivery attempt")
        if not rows:
            return None
        run = rows[0]
        if until is not None:
            candidate_time_left(until, 1)
        start = connection.execute("SELECT actor FROM events_log WHERE kind = 'run.started' AND subject = ?",
                                   (f"run:{run['id']}",)).fetchone()
        require(start and start[0] == "rule:poll", "the run was not started by scheduled poll")
        require(not run["stop_asked"], "the owner stopped this run")
        return run


def published_proof(run: dict) -> tuple[dict, dict] | None:
    if run["status"] in ("running", "starting", "ci"):
        return None
    require(run["status"] == "pr", f"the run ended {run['status']}; inspect {run['id']}")
    row = orchestration.find(run["id"])
    require(row and row.get("root") == row.get("current_run") == run["id"] and row.get("generation") == 1,
            "the run does not have a fresh orchestrated lineage")
    if not run.get("finished") or util.pid_alive(row["owner"]):
        return None
    data = row["data"]
    lease = orchestration.Lease(run["id"], run["id"], 1)
    outcomes = orchestration.actions(lease)
    require(not orchestration.pending(lease) and any(a["op"] == "publication.request" and a["state"] == "succeeded"
                                                     for a in outcomes)
            and all(a["state"] in ("succeeded", "failed", "cancelled") and a["op"] != "owner.ask" for a in outcomes),
            "the run has no settled publication or has an unresolved producer or owner question")
    candidates = data.get("prepared", {}).get("checkouts", [])
    require(len(candidates) == 1 and data.get("mode") == "orchestrate"
            and data.get("published", {}).get("ending") == "pr"
            and data.get("published_tree") and not data.get("publication_intent") and not data.get("ci_failed")
            and not data.get("feedback") and not data.get("repair_source"), "a fresh single-repository PR did not settle")
    return data, candidates[0]


def require_evidence(data: dict, github: str, tree: str, commands: dict) -> None:
    gates = data.get("evidence", {})
    current_tree = orchestration.digest({github: tree})
    require(data.get("acceptance") and data.get("policy_sha")
            and all(gates.get(name, {}).get("tree") == current_tree
                    and gates[name].get("acceptance") == data["acceptance"]
                    and gates[name].get("policy") == data["policy_sha"] for name in ("checks", "verify", "review")),
            "check, verification and review evidence do not match the candidate tree and admitted policy")
    checks = gates["checks"]["result"]
    checked = checks.get("checked", {})
    require(commands and checks.get("full") is True and checked.get("commands") == commands
            and checked.get("tree") == tree, "full local checks did not pass on the exact tree")
    native = verify.Verified.stored(gates["verify"]["result"])
    require(native.verdict == "pass" and native.finished and native.checks and not native.failed_checks
            and native.native_trees == {"": tree}, "native behavioral verification did not pass on the exact tree")
    review = Review.stored(gates["review"]["result"])
    require(review.approved() and not review.failed and not review.blocking() and not review.waived
            and review.trees == {"": tree}, "independent review did not approve the exact tree")


def publication_head(repo: config.Repo, tid: str, run_id: str, checkout: workspace.Checkout) -> str:
    from .orchestration import policy_fingerprint

    receipt = live_receipt(tid, repo.name)
    signal = Signal(**receipt["signal"])
    require(checkout.github == signal.github and checkout.base == BRANCH and not checkout.dir and not checkout.resumes
            and checkout.src.resolve() == repo.path.resolve(), "the publication checkout is not the authorized checkout")
    cfg = config.load()
    layer = config.load_layer(repo.layer_dir)
    head = git(checkout.wt, "rev-parse", "HEAD").strip()
    tree = exact_edit(checkout.wt, signal, head, signal.expected(readme(checkout.wt, signal.base)))
    data = (orchestration.find(run_id) or {}).get("data", {})
    require(layer.factory_path == "orchestrated" and layer.trust_level == "pr"
            and receipt.get("policy") == policy_fingerprint(cfg, layer, ()) == data.get("policy_sha")
            and data.get("publication_intent", {}).get("tree") == orchestration.digest({signal.github: tree}),
            "the candidate does not match the agreed policy and publication intent")
    require_evidence(data, signal.github, tree, layer.checks)
    check_origin(checkout.wt, signal.github)
    run = scheduled_run(repo.name, tid)
    require(run and run["id"] == run_id and live_receipt(tid, repo.name) == receipt,
            "the approved run or publication authority changed")
    return head


def owner_merge(github: str, number: int, head: str, ready_at: str = "") -> str:
    row = gh.gh_json(["api", f"repos/{github}/pulls/{number}"], timeout=20) or {}
    base_ref, head_ref = row.get("base") or {}, row.get("head") or {}
    require(base_ref.get("ref") == BRANCH and (base_ref.get("repo") or {}).get("full_name") == github
            and head_ref.get("sha") == head and (head_ref.get("repo") or {}).get("full_name") == github,
            "GitHub no longer reports the authorized PR head and target")
    if (row.get("state") == "open" and row.get("merged") is False and row.get("draft") is False
            and row.get("auto_merge") is None):
        return ""
    commit, when, actor = row.get("merge_commit_sha") or "", row.get("merged_at"), row.get("merged_by") or {}
    require(row.get("state") == "closed" and row.get("merged") is True and SHA.fullmatch(commit)
            and actor.get("type") == "User" and actor.get("login") == github.split("/", 1)[0]
            and when and ready_at and util.parse_iso(when) > util.parse_iso(ready_at),
            "only the GitHub repository owner's merge after the handoff can authorize deployment")
    return commit


def healthy() -> str:
    beat = util.read_json(paths.STATE_DIR / "serve.json", {})
    server = util.read_json(paths.STATE_DIR / "server.json", {})
    require(beat.get("tick_ok") is True and beat.get("schema") and beat.get("at")
            and util.now().timestamp() - util.parse_iso(beat["at"]).timestamp() < 65
            and not any(beat.get(key) for key in ("broken", "failing", "poll_errors", "config_problem", "code_problem"))
            and server.get("scheme") == "https" and server.get("state") == str(paths.STATE_DIR)
            and server.get("config") == str(paths.CONFIG_FILE) and server.get("pid") == beat.get("pid")
            and util.pid_alive(server.get("pid")), "the local service is not healthy")
    with urlopen(f"https://{server['host']}:{server['port']}/api/meta", timeout=10) as response:
        require(response.status == 200 and json.load(response)["schema"]["version"] == beat["schema"],
                "the dashboard API did not confirm the healthy schema")
    return beat["at"]


def wait_for(deadline: float, check, why: str, *, interval: float = 2):
    while time.monotonic() < deadline:
        if (found := check()) and time.monotonic() < deadline:
            return found
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    raise Held(why)


def wait_healthy(deadline: float, deployed_at: str) -> None:
    last_failure = ""

    def fresh():
        nonlocal last_failure
        try:
            return healthy() > deployed_at
        except (Held, OSError, ValueError, KeyError) as error:
            last_failure = str(error)
            return False

    try:
        wait_for(deadline, fresh, "no healthy service heartbeat after deployment")
    except Held as error:
        raise Held(f"{error}: {last_failure}" if last_failure else str(error)) from error


def execute(cfg: config.Config, repo: config.Repo, layer: config.RepoLayer, signal: Signal, expected: str,
            receipt: Path, minutes: int) -> None:
    from .orchestration import policy_fingerprint

    deadline = time.monotonic() + minutes * 60
    pinned_policy = policy_fingerprint(cfg, layer, ())
    row = {"signal": asdict(signal), "repo": repo.name, "task": signal.task_id, "phase": "claimed", "started": util.iso()}

    def record(phase, **values):
        row.update(values, phase=phase, updated=util.iso())
        util.write_durably(receipt, json.dumps(row, indent=2).encode(), mode=0o600)

    def in_time():
        require(time.monotonic() < deadline, "the pilot authorization expired")

    def check_policy():
        require(policy_fingerprint(config.load(), config.load_layer(repo.layer_dir), ()) == pinned_policy,
                "the owner config changed during the pilot")

    def check_base():
        check_policy()
        have_checkout(repo.path, signal.base)
        check_origin(repo.path, signal.github)
        require(origin(signal.github) == signal.base, "GitHub main moved off the approved base")
        protected(signal.github)
        in_time()

    record("claimed")
    try:
        check_base()
        healthy()
        require(jev.ready(cfg, [repo]), "JEV risk screening is unavailable")
        pid = os.getpid()
        identity = delegation.identity_of(pid)
        require(identity is not None, "the pilot watcher process cannot be identified")
        tid = signal.task_id
        record("creating_task", deadline=deadline, owner_pid=pid, owner_identity=identity, policy=pinned_policy)
        in_time()
        try:
            tasks.task_add(repo.name, signal.task(), title=TASK_TITLE, kind=TASK_KIND, reserved_id=tid)
        except sqlite3.IntegrityError as error:
            raise Held("could not create the reserved local task; inspect the existing task and receipt") from error
        in_time()
        record("waiting_for_poll")

        def attempt():
            run = scheduled_run(repo.name, tid)
            if run and (proof := published_proof(run)):
                return run, *proof
            return None

        run, data, checkout = wait_for(deadline, attempt, "no eligible published PR before the pilot deadline")
        url = checkout["pr"]
        require(checkout["github"] == signal.github and checkout["base"] == BRANCH and not checkout["dir"]
                and not checkout.get("resumes") and run["pr_url"] == url
                and re.fullmatch(rf"https://github\.com/{re.escape(signal.github)}/pull/[1-9][0-9]*", url),
                "the settled PR does not match the approved repository and checkout")
        number = int(url.rsplit("/", 1)[1])
        head = checkout["lease"]
        require(SHA.fullmatch(head), "the PR has no recorded head")
        record("reviewing", run=run["id"], pr=url, head=head)
        check_origin(repo.path, signal.github)
        git(repo.path, "fetch", "--no-write-fetch-head", "origin", f"refs/pull/{number}/head")
        tree = exact_edit(repo.path, signal, head, expected)
        require_evidence(data, signal.github, tree, layer.checks)
        require(data["policy_sha"] == pinned_policy and data.get("limits") == asdict(layer.orchestration)
                and data.get("published_tree") == orchestration.digest({signal.github: tree}),
                "the publication does not match the approved policy, orchestration limits and exact tree")
        _, hold = gh.merge_readiness(signal.github, number, head, BRANCH, signal.base, required=CHECK)
        require(not hold, hold)
        require(jev.ready(cfg, [repo]), "JEV risk screening is unavailable")
        in_time()
        patch = git(repo.path, "diff", "--no-ext-diff", "--unified=0", signal.base, head, "--", DOC)
        risk = jev.risk(patch, "Owner-authorized one-line README correction", cfg)
        require(not risk.hold, risk.hold)
        check_base()
        healthy()

        def check_run():
            current = scheduled_run(repo.name, tid)
            require(current and current["id"] == run["id"] and published_proof(current) == (data, checkout),
                    "the approved run or publication evidence changed")
            require(task_matches(tid, repo.name, signal, "pr"), "the local task changed or was closed")

        _, hold = gh.merge_readiness(signal.github, number, head, BRANCH, signal.base, required=CHECK)
        require(not hold, hold)
        owner_merge(signal.github, number, head)
        check_run()
        in_time()
        record("handoff_ready", tree=tree, risk=asdict(risk))
        print(f"Owner merge required: squash {url} onto {BRANCH} at {signal.base} (PR head {head}).", flush=True)
        ready_at = util.iso()
        record("waiting_for_owner_merge", handoff_at=ready_at)
        merged = wait_for(deadline, lambda: owner_merge(signal.github, number, head, ready_at),
                          "no matching owner merge before the pilot deadline", interval=5)
        record("merged", merged=merged)

        def ci_on_main():
            require(origin(signal.github) == merged, "main moved before deployment")
            return gh.merge_ci_passed(signal.github, merged, required=CHECK)

        wait_for(deadline, ci_on_main, "the merged commit has no passing CI before the deadline", interval=5)
        with util.repo_lock(repo.path):
            check_origin(repo.path, signal.github)
            git(repo.path, "fetch", "--no-tags", "--no-recurse-submodules", "origin", f"refs/heads/{BRANCH}")
            require(git(repo.path, "rev-parse", "FETCH_HEAD").strip() == merged
                    and git(repo.path, "rev-list", "--parents", "-n", "1", merged).split() == [merged, signal.base]
                    and git(repo.path, "rev-parse", f"{merged}^{{tree}}").strip() == tree,
                    "main no longer names the tested single-parent release tree")
        with slots.admitting(), util.repo_lock(repo.path):
            check_policy()
            have_checkout(repo.path, signal.base)
            require(not any(r["kind"] == "pipeline" for r in runs.runs_marked_running()),
                    "a pipeline is running; leave the checkout alone")
            healthy()
            require(origin(signal.github) == merged, "main moved before the checkout could be updated")
            protected(signal.github)
            require(gh.merge_ci_passed(signal.github, merged, required=CHECK), "release CI changed before deployment")
            check_run()
            record("deployment_requested")
            in_time()
            git(repo.path, "merge", "--ff-only", merged)
            have_checkout(repo.path, merged)
            deployed_at = util.iso()
            record("deployed_waiting_for_health", deployed_at=deployed_at)
        wait_healthy(deadline, deployed_at)
        record("healthy")
    except BaseException as error:
        record("held", stopped_at=row["phase"], why=str(error) or type(error).__name__)
        raise


def claim(signal: Signal) -> Path:
    receipt = receipt_for(signal.task_id)
    try:
        receipt.parent.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        raise Held(f"this one-shot signal is already spent: {receipt}") from None
    util.flush_folder(receipt.parent.parent)
    util.flush_folder(paths.STATE_DIR)
    return receipt


def cmd_pilot(args) -> int:
    try:
        if args.execute:
            require(not paths.ISOLATED and principals.current() == principals.owner() and principals.current() != "hand",
                    "run the one-shot pilot as the local Arete owner")
        cfg = config.load()
        repo = cfg.repo(cfg.arete_repo)
        require(repo.path.resolve() == paths.PACKAGE_DIR.parent.resolve() and repo.pipeline and len(repo.parts) == 1
                and not repo.parts[0].dir and repo.parts[0].github == repo.github and repo.github,
                "run from the installed Arete pipeline checkout")
        layer = config.load_layer(repo.layer_dir)
        require(layer.trust_level == "pr" and layer.factory_path == "orchestrated" and layer.verify and layer.checks,
                "the pilot requires an orchestrated PR-trust layer with verification and local checks")
        signal = Signal(repo.github, args.base, args.old_line, args.new_line)
        require(SHA.fullmatch(signal.base), "use a full approved base SHA")
        have_checkout(repo.path, signal.base)
        expected = signal.expected(readme(repo.path, signal.base))
        if not args.execute:
            print("Signal matches; dry run created no task and made no merge or deployment request.")
            return 0
        controller = steps.resolve(cfg, layer, "orchestrate", "fix")
        require((controller.runner, controller.model, controller.effort) == ("pi", "openai/gpt-6.2-sol", "low")
                and not controller.fallback and not steps.experiment_for(cfg, layer, "orchestrate", "fix"),
                "the pilot requires the fixed Pi OpenAI controller at low effort with no model fallback")
        limits = layer.orchestration
        require(repo.layer and not cfg.replay.all
                and (layer.routing.payment_modes or cfg.routing.payment_modes) == ("subscription",)
                and limits.max_workers == 1 and limits.max_decisions <= 16 and limits.max_actions <= 64
                and layer.max_run_minutes <= 40 and layer.max_verify_minutes <= 15 and layer.max_repair_cycles <= 1,
                "the one-shot pilot needs the bounded subscription-only layer")
        with util.lock("pilot-doc-fix") as locked:
            require(locked, "another pilot is already active")
            receipt = claim(signal)
            print(f"One-shot record: {receipt}", flush=True)
            execute(cfg, repo, layer, signal, expected, receipt, args.minutes)
        print("Deployed the owner-merged local task; the service is healthy.")
        return 0
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f"Pilot held: {str(error) or type(error).__name__}", file=sys.stderr)
        return 1


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("pilot-doc-fix", help="one-shot owner authorization for an exact README correction")
    parser.add_argument("--base", required=True, help="full approved main commit")
    parser.add_argument("--old-line", required=True)
    parser.add_argument("--new-line", required=True)
    parser.add_argument("--minutes", type=int, default=40, choices=range(1, 41), metavar="1..40")
    parser.add_argument("--execute", action="store_true", help="consume this authorization; otherwise dry run")
    parser.set_defaults(fn=cmd_pilot)
