"""Run an enrolled browser check against the candidate server owned by this verification."""
from __future__ import annotations

import subprocess

from . import config, factory, paths, release_profile, release_proof as proof, serve_guard, util, verify


def binding(work):
    run = getattr(work, "run", None)
    repo = getattr(run, "repo", None)
    if not isinstance(repo, config.Repo) or repo.unattended_environment != "staging":
        return None
    ticket = str(getattr(run, "tid", ""))
    if not ticket:
        return None
    policy = proof.goal(repo.name, ticket)
    proof.need(policy or not proof.policy_file(repo.name, ticket).is_file(), "the candidate's enrolled staging policy is stale")
    if not policy or policy["destination"] != "staging" or not policy.get("observer_sha256"):
        return None
    proof.need(repo.pipeline and repo.landing == "pr" and repo.target_branch
               and repo.target_branch != policy["production_branch"] and str(getattr(getattr(work, "ticket", None), "id", "")) == ticket,
               "the candidate does not belong to the enrolled staging ticket")
    release_profile.current(repo)
    checkouts = {}
    for checkout in getattr(work, "checkouts", ()):
        head = util.git(["rev-parse", "HEAD"], checkout.wt).strip()
        tree = factory.fingerprint(checkout.wt)
        proof.need(serve_guard.SHA.fullmatch(head) and serve_guard.SHA.fullmatch(tree)
                   and checkout.prefix not in checkouts, "the candidate checkout has no unambiguous revision and tree")
        checkouts[checkout.prefix] = {"repo": checkout.github, "revision": head, "tree": tree}
    owning = [c for c in checkouts.values() if c["repo"].casefold() == repo.github.casefold()]
    proof.need(len(owning) == 1 and (not policy.get("revision") or owning[0]["revision"] == policy["revision"]),
               "the candidate revision does not belong to this observer")
    return policy, {"repo": repo.name, "ticket": ticket, "revision": owning[0]["revision"], "checkouts": checkouts,
                    "policy": proof.digest(policy), "observer": policy["observer_sha256"], "path": policy["path"]}


def listener(proc, origin, configured):
    proof.need(isinstance(origin, str) and origin and isinstance(configured, str) and configured,
               "the protected candidate observer requires a configured, running local service")
    url = proof.server_url(origin)
    proof.need(proc and proc.poll() is None and configured.rstrip("/") == origin and url.scheme == "http"
               and url.hostname in ("127.0.0.1", "::1") and url.port and url.port not in paths.dashboard_ports(),
               "the observer requires the running verification server on numeric loopback")
    endpoint = f"[::1]:{url.port}" if url.hostname == "::1" else f"127.0.0.1:{url.port}"
    result = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", f"-iTCP:{url.port}", "-sTCP:LISTEN", "-Fpn"],
                            capture_output=True, text=True, timeout=10)
    lines = result.stdout.splitlines()
    owners = {int(line[1:]) for line in lines if line.startswith("p") and line[1:].isdigit()}
    expected = dict(util.process_tree(proc.pid))
    proof.need(result.returncode == 0 and owners and owners <= expected.keys()
               and {line[1:] for line in lines if line.startswith("n")} == {endpoint},
               "the loopback listener does not belong exclusively to the started candidate process")
    return sorted(owners)


def observe(work, step, enrolled, origin, configured, proc):
    policy, scope = enrolled
    receipt = {**scope, "run": work.run.id, "step": step, "origin": origin, "destination": "candidate"}
    checks = []
    try:
        proof.need(enrolled == binding(work), "candidate tree or enrolled policy changed during server startup")
        owners = listener(proc, origin, configured)
        document = proof.browser_observation(policy, origin, scope["revision"], "candidate")
        proof.need(owners == listener(proc, origin, configured) and enrolled == binding(work),
                   "the candidate listener, tree or enrolled policy changed during observation")
        observed = []
        for assertion in policy["assertions"]:
            try:
                observed.extend(proof.compare(document, [assertion]))
                passed, saw = True, "The protected observer matched the enrolled assertion."
            except AssertionError as error:
                passed, saw = False, str(error)
            check = {"what": assertion["field"], "how": policy["instructions"], "passed": passed, "saw": saw,
                     "evidence_file": str(work.run.dir / f"{step}-candidate-observation.json")}
            if not passed:
                check["cause"] = "change"
            checks.append(check)
        verdict = "pass" if all(c["passed"] for c in checks) else "fail"
        receipt.update(observed=observed, checks=checks, status=verdict)
        result = verify.Verified(verdict, "Protected browser observation on the current local candidate.", checks)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, proof.Held) else type(error).__name__
        receipt.update(status="blocked", reason=reason)
        result = verify.Verified("blocked", "The protected candidate observer could not verify this build.",
                                 blocked_by=reason, finished=False, behavior_reached=False)
    receipt["receipt_id"] = proof.digest(receipt)
    work.run.save(f"{step}-candidate-observation.json", receipt)
    return result
