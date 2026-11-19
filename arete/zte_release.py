from __future__ import annotations

import http.client
import json
import os
import plistlib
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import alerts, codebase, config, install as service_install, paths, pilot, principals, slots, util, zte, zte_delivery
from .store import db, runs
from .store import zte as store

PROFILE = paths.ZTE_AUTHORITY / "zte-release.toml"
HEALTH_SECONDS = 120
RECOVERY_SECONDS = 180
RESTART_GRACE_SECONDS = 30
RECOVERY_HEALTH_SECONDS = 45
RELEASE_LOCK = "zte-host-release"


class DatabaseUnhealthy(pilot.Held):
    pass


@dataclass(frozen=True)
class Installation:
    root: Path
    endpoint: str
    label: str
    schema: int

    def to_receipt(self) -> dict:
        return {"endpoint": self.endpoint, "schema": self.schema, "rehearsal": self.label == "com.arete.zte-rehearsal"}

    @property
    def selector(self) -> Path:
        return self.root / "current.json"

    @property
    def service(self) -> str:
        return f"gui/{os.getuid()}/{self.label}"

    def runtime(self, sha: str) -> Path:
        pilot.require(pilot.SHA.fullmatch(sha), "invalid full managed release SHA")
        return self.root / "releases" / sha


def installation(receipt: dict | None = None) -> Installation:
    raw = receipt if receipt is not None else tomllib.loads(zte.private_bytes(PROFILE, 4096).decode())
    pilot.require(type(raw.get("rehearsal")) is bool, "the installation must identify its managed service")
    root = paths.ZTE_REHEARSAL_INSTALL if raw["rehearsal"] else paths.ZTE_LIVE_INSTALL
    label = "com.arete.zte-rehearsal" if raw["rehearsal"] else "com.arete.serve"
    endpoint = urlsplit(raw["endpoint"])
    pilot.require(paths.ON_MAC and db.LATEST == raw["schema"] and endpoint.scheme == "https" and endpoint.port
                  and endpoint.hostname and re.fullmatch(r"[a-z0-9][a-z0-9.-]*", endpoint.hostname)
                  and not any((endpoint.username, endpoint.password, endpoint.path, endpoint.query, endpoint.fragment))
                  and pilot.private(root, stat.S_ISDIR) and pilot.private(root / "releases", stat.S_ISDIR),
                  "the managed release profile is not approved for this host and schema")
    return Installation(root, raw["endpoint"], label, raw["schema"])


def command(argv: list[str], *, where: Path, timeout: float = 20, env: dict | None = None) -> str:
    # The host supervisor stages locked releases in directories protected from worker subprocesses.
    done = util.run_whole_tree(argv, timeout, None, cwd=where, text=True, errors="replace",
                              env=env, **util.own_group_args())
    pilot.require(done.returncode == 0, f"{Path(argv[0]).name} {argv[1]} failed for the managed installation")
    return done.stdout.strip()


def selected(install: Installation) -> str:
    data = json.loads(zte.private_bytes(install.selector, 512))
    sha = data["sha"]
    install.runtime(sha)
    return sha


def verify_source(root: Path, sha: str, tree: str = "", *, until: datetime | None = None) -> str:
    def git(*args: str) -> str:
        return util.git(list(args), root, timeout=activation_timeout(until, 30), retry=False)

    pilot.require(pilot.private(root, stat.S_ISDIR) and (root / ".git").is_dir()
                  and not (root / ".git").is_symlink() and git("rev-parse", "HEAD").strip() == sha
                  and not git("status", "--porcelain", "--untracked-files=all"),
                  "the managed source is missing, dirty or differs from the verified release")
    actual = git("rev-parse", "HEAD^{tree}").strip()
    pilot.require(not tree or actual == tree, "the managed source does not have the authorized tree")
    return actual


def ready_path(root: Path) -> Path:
    return root / ".git" / "arete-runtime.json"


def mark_runtime(root: Path, sha: str, tree: str) -> None:
    python = root / ".venv/bin/python"
    expected = str(root / ".venv")
    observed = command([str(python), "-I", "-c", "import sys, arete; print(sys.prefix)"], where=root)
    pilot.require(observed == expected, "the locked runtime does not resolve inside its release directory")
    util.write_durably(ready_path(root), json.dumps({"sha": sha, "tree": tree}).encode(), mode=0o600)


def verify_runtime(install: Installation, sha: str, tree: str = "", *, until: datetime | None = None) -> Path:
    root = install.runtime(sha)
    actual = verify_source(root, sha, tree, until=until)
    pilot.require((root / ".venv/bin/python").is_file()
                  and json.loads(zte.private_bytes(ready_path(root), 512)) == {"sha": sha, "tree": actual},
                  "the managed runtime has no completed locked-install receipt")
    return root


def switch(install: Installation, previous: str, sha: str) -> None:
    pilot.require(selected(install) == previous, "the runtime selector moved outside the release worker")
    util.write_durably(install.selector, json.dumps({"sha": sha}).encode(), mode=0o600)


def activation_deadline(delivery) -> datetime:
    window = util.parse_iso(delivery.data["activation_after"]) + timedelta(seconds=HEALTH_SECONDS)
    return min(window, util.parse_iso(delivery.deadline))


def activation_time_left(until: datetime) -> float:
    remaining = (until - util.now()).total_seconds()
    pilot.require(remaining > 0, "the initial managed release health window expired")
    return remaining


def activation_timeout(until: datetime | None, cap: float) -> float:
    return cap if until is None else min(cap, activation_time_left(until))


def restart(install: Installation, *, until: datetime | None = None) -> None:
    command = ["/bin/launchctl", "kickstart", "-k", install.service]
    boot = subprocess.run(command, timeout=activation_timeout(until, 20), capture_output=True, check=False)
    pilot.require(boot.returncode == 0, "launchd could not restart the managed service")


def launch_job(service: str, *, until: datetime | None = None) -> str:
    job = subprocess.run(["/bin/launchctl", "print", service], timeout=activation_timeout(until, 4),
                         capture_output=True, text=True, check=False)
    pilot.require(job.returncode == 0, "the managed launchd job is not loaded")
    return job.stdout


def service_pid(install: Installation, *, until: datetime | None = None) -> int:
    match = re.search(r"^\s*pid = (\d+)$", launch_job(install.service, until=until), re.MULTILINE)
    return int(match[1]) if match else 0


def database(install: Installation, *, until: datetime | None = None) -> None:
    reason = "the unchanged managed database did not pass its schema and integrity check"
    try:
        with db.one_read(timeout=None if until is None else activation_timeout(until, 2)) as connection:
            if until is not None:
                connection.set_progress_handler(lambda: util.now() >= until, 1000)
            try:
                if (connection.execute("PRAGMA user_version").fetchone()[0] != install.schema
                        or connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"):
                    raise DatabaseUnhealthy(reason)
            finally:
                if until is not None:
                    connection.set_progress_handler(None, 0)
    except (OSError, sqlite3.Error) as error:
        if until is not None:
            activation_time_left(until)
        raise DatabaseUnhealthy(reason) from error


def stop_unhealthy_service(install: Installation) -> None:
    service = subprocess.run(["/bin/launchctl", "bootout", install.service], timeout=20,
                             capture_output=True, check=False)
    pilot.require(service.returncode == 0, "database unhealthy and launchd did not confirm service shutdown")


def executable_matches(root: Path, pid: int, *, until: datetime | None = None) -> bool:
    process = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], timeout=activation_timeout(until, 4),
                             capture_output=True, text=True, check=False)
    return process.returncode == 0 and process.stdout.strip() == f"{root / '.venv/bin/python'} -m arete.cli serve"


def health(install: Installation, sha: str, *, after: str = "", old_pid: int = 0,
           until: datetime | None = None) -> dict:
    root = verify_runtime(install, sha, until=until)
    beat = util.read_json(paths.STATE_DIR / "serve.json", {})
    server = util.read_json(paths.STATE_DIR / "server.json", {})
    pid = beat.get("pid")
    pilot.require(beat.get("engine") == server.get("engine") == sha[:12] and pid == server.get("pid")
                  and pid != old_pid and util.pid_alive(pid) and beat.get("schema") == install.schema
                  and beat.get("tick_ok") is True and beat.get("at", "") > after
                  and 0 <= (util.now() - util.parse_iso(beat["at"])).total_seconds() < 65
                  and not any(beat.get(k) for k in ("broken", "failing", "poll_errors", "config_ignored",
                                                   "config_problem", "code_problem", "migration_error")),
                  "managed service has no matching healthy heartbeat")
    target = urlsplit(install.endpoint)
    pilot.require((server.get("scheme"), server.get("host"), server.get("port"), server.get("state"),
                   server.get("config")) == ("https", target.hostname, target.port, str(paths.STATE_DIR),
                                              str(paths.CONFIG_FILE)), "managed service address differs from the profile")
    pilot.require(service_pid(install, until=until) == pid and executable_matches(root, pid, until=until),
                  "heartbeat PID is not the selected managed launchd executable")
    connection = http.client.HTTPSConnection(target.hostname, target.port, timeout=activation_timeout(until, 8))
    try:
        connection.request("GET", "/api/meta", headers={"Accept": "application/json"})
        connection.sock.settimeout(activation_timeout(until, 8))
        response = connection.getresponse()
        if until is not None:
            activation_time_left(until)
        pilot.require(response.status == 200 and response.getheader("X-Arete-Engine") == sha[:12]
                      and json.loads(response.read(2**18))["schema"]["version"] == install.schema,
                      "authenticated HTTPS did not confirm the selected engine and schema")
        if until is not None:
            activation_time_left(until)
    finally:
        connection.close()
    return {"at": beat["at"], "pid": pid, "engine": sha, "schema": install.schema, "https_status": 200}


def failure_reason(error: Exception) -> str:
    return util.first_line(str(error), 200) if isinstance(error, pilot.Held) else type(error).__name__


def fresh_health(install: Installation, sha: str, after: str, old_pid: int, check, *, seconds=None,
                 until: datetime | None = None) -> list[dict]:
    limit = time.monotonic() + activation_timeout(until, HEALTH_SECONDS if seconds is None else seconds)
    evidence = []
    reason = "no second heartbeat from the same process"
    while time.monotonic() < limit:
        check()
        try:
            beat = health(install, sha, after=after, old_pid=old_pid, until=until)
        except (OSError, RuntimeError, ValueError, KeyError, http.client.HTTPException, subprocess.SubprocessError) as error:
            reason = failure_reason(error)
            evidence.clear()
        else:
            reason = "no second heartbeat from the same process"
            if not evidence or beat["pid"] != evidence[-1]["pid"]:
                evidence = [beat]
            elif beat["at"] != evidence[-1]["at"]:
                database(install, until=until)
                check()
                return [evidence[-1], beat]
        time.sleep(activation_timeout(until, min(2, max(0, limit - time.monotonic()))))
    if until is not None:
        activation_time_left(until)
    raise pilot.Held(f"no two fresh authenticated HTTPS health beats for the managed release: {reason}")


def stage(install: Installation, delivery, client) -> Path:
    data = delivery.data
    sha, tree = data["release"], data["tree"]
    root = install.runtime(sha)
    if ready_path(root).exists():
        return verify_runtime(install, sha, tree)
    env = {"PATH": os.environ["PATH"], "HOME": str(Path.home()), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "UV_NO_PROGRESS": "1",
           "UV_NO_CONFIG": "1", "UV_PYTHON_DOWNLOADS": "never"}

    if not root.exists():
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=root.parent) as temporary:
            source = Path(temporary) / "source"
            source.mkdir(mode=0o700)

            def git(*args):
                return command(["/usr/bin/git", *args], where=source, timeout=client.time_left(90), env=env)

            git("init", "-q", "--template=", "--initial-branch=main")
            git("-c", "http.followRedirects=false", "fetch", "--no-tags", "--no-recurse-submodules",
                "--depth=2", f"https://github.com/{client.identity.name}.git", sha)
            pilot.require(git("rev-parse", "FETCH_HEAD") == sha and git("rev-parse", f"{sha}^{{tree}}") == tree
                          and git("rev-list", "--parents", "-n", "1", sha).split() == [sha, delivery.base],
                          "release objects do not match the reconciled squash")
            changed = git("diff", "--name-status", "--no-renames", delivery.base, sha)
            pilot.require(changed == "M\tREADME.md", "the managed release may only replace the authorized README")
            git("-c", "core.hooksPath=/dev/null", "checkout", "--detach", sha)
            verify_source(source, sha, tree)
            source.rename(root)
            util.flush_folder(root.parent)
    verify_source(root, sha, tree)
    interpreter = Path(sys.executable).resolve(strict=True)
    command(["uv", "sync", "--locked", "--no-dev", "--python", str(interpreter)], where=root,
            timeout=client.time_left(120), env=env)
    pilot.require((root / ".venv/bin/python").resolve() == interpreter,
                  "the locked release did not retain the supervisor's Python interpreter")
    mark_runtime(root, sha, tree)
    return verify_runtime(install, sha, tree)


def active(delivery, install: Installation):
    previous = delivery.data.get("last_good") or selected(install)
    policy = zte.active(delivery, engine=previous[:12])
    pilot.require(installation() == install and policy.rehearsal == install.to_receipt()["rehearsal"]
                  and zte.settings(config.load(), policy)[2] == delivery.data["config"],
                  "the release profile or admitted configuration changed")
    zte_delivery.check_stop(delivery, policy, engine=previous[:12])
    return policy


def activation_authority(delivery, install: Installation, until: datetime):
    with db.one_read(timeout=activation_timeout(until, 2)):
        policy = active(delivery, install)
    activation_time_left(until)
    return policy


def known_good(install: Installation, base: str) -> dict:
    pilot.require(selected(install) == base, "the selected known-good runtime is not the approved squash parent")
    launch_job(install.service + ".zte-release")
    database(install)
    return health(install, base)


def preflight(delivery, policy) -> None:
    install = installation()
    pilot.require(install.to_receipt() == delivery.data["approved_installation"]
                  and policy.rehearsal == install.to_receipt()["rehearsal"],
                  "the admitted managed release profile changed")
    try:
        known_good(install, delivery.base)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, http.client.HTTPException,
            subprocess.SubprocessError) as error:
        raise pilot.Held("the admitted last-good runtime or release worker is not healthy") from error


def restore(install: Installation, delivery, *, reason: str = "") -> None:
    previous, release = delivery.data["last_good"], delivery.data["release"]
    with util.lock(zte.INTERLOCK, wait=True):
        if delivery.phase == "deploy_intent":
            util.write_durably(zte.PAUSE_FILE, json.dumps({"at": util.iso(), "task": delivery.task}).encode(), mode=0o600)
            delivery = store.transition(delivery, "rollback_intent", restore_after=util.iso(), reason=reason)
        current = selected(install)
        pilot.require(current in (previous, release), "selector and recovery journal disagree; no runtime overwritten")
        known = verify_runtime(install, previous)
        if current == release:
            switch(install, release, previous)
    try:
        database(install)
        reconcile_recovery(install, delivery, previous, known)
    except DatabaseUnhealthy as error:
        stop_unhealthy_service(install)
        raise DatabaseUnhealthy("managed service stopped: database failed recovery integrity or schema checks") from error


def reconcile_recovery(install: Installation, delivery, previous: str, known: Path) -> None:
    after = delivery.data["restore_after"]
    until = util.parse_iso(after) + timedelta(seconds=RECOVERY_SECONDS)
    pilot.require(util.now() < until, "bounded recovery window expired")
    pid = service_pid(install)
    if pid and executable_matches(known, pid):
        try:
            beats = fresh_health(install, previous, after, 0, lambda: None,
                                 seconds=min(RECOVERY_HEALTH_SECONDS, (until - util.now()).total_seconds()))
            database(install)
            final = health(install, previous, after=after)
        except DatabaseUnhealthy:
            raise
        except (OSError, RuntimeError, ValueError, KeyError, http.client.HTTPException, subprocess.SubprocessError):
            pass
        else:
            with db.tx():
                database(install)
                pilot.require(selected(install) == previous, "the selected runtime changed during recovery")
                confirmed = final["pid"] == beats[-1]["pid"] and util.pid_alive(final["pid"])
                if confirmed:
                    store.transition(delivery, "rolled_back", recovery_health=beats, recovery_completion_health=final)
            if confirmed:
                alerts.notify("Arete ZTE rolled back", f"Restored the healthy runtime {previous[:12]}; delivery paused.")
                return
    attempt = delivery.data.get("restart_recovery") or delivery.data.get("restart_restore")
    if attempt and util.now() < util.parse_iso(attempt) + timedelta(seconds=RESTART_GRACE_SECONDS):
        return
    pilot.require(util.now() < until and not delivery.data.get("restart_recovery"),
                  "last-good runtime did not recover within the bounded restart budget")
    pilot.require(selected(install) == previous, "the selected runtime changed before the recovery restart")
    database(install)
    key = "restart_recovery" if attempt else "restart_restore"
    delivery = store.annotate(delivery, **{key: util.iso()})
    database(install)
    try:
        restart(install)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        store.annotate(delivery, **{f"{key}_error": util.first_line(str(error), 160)})


def deploy(install: Installation, delivery) -> None:
    policy = active(delivery, install)
    client = policy.client(delivery.deadline)
    prior = delivery.base
    sha, tree = delivery.data["release"], delivery.data["tree"]
    pilot.require(install.to_receipt() == delivery.data["approved_installation"],
                  "the admitted managed release profile changed")
    zte_delivery.require_landing(delivery, client, sha)
    stage(install, delivery, client)
    if runs.runs_marked_running():
        return
    verify_runtime(install, sha, tree)
    policy.audit(client)
    pilot.require(client.ci(sha, tree, "push") == delivery.data["release_ci"], "release CI evidence changed")
    before = known_good(install, prior)
    zte_delivery.require_landing(delivery, client, sha)
    with slots.admitting(wait=False) as slot:
        if not slot:
            return
        with util.lock(zte.INTERLOCK) as got:
            if not got or runs.runs_marked_running():
                return
            active(delivery, install)
            pilot.require(selected(install) == prior and util.pid_alive(before["pid"]),
                          "the last-good runtime changed before activation")
            intent = store.transition(delivery, "deploy_intent", last_good=prior, activation_after=util.iso(),
                                      old_pid=before["pid"], installation=install.to_receipt())
            until = activation_deadline(intent)
            activation_authority(intent, install, until)
            switch(install, prior, sha)
    with util.lock(zte.INTERLOCK) as got:
        pilot.require(got, "the Stop interlock is busy before the managed restart")
        activation_authority(intent, install, until)
        pilot.require(selected(install) == sha, "the selected runtime changed before the managed restart")
        restart(install, until=until)
    finish(install, intent)


def finish(install: Installation, delivery) -> None:
    data = delivery.data
    sha = data["release"]
    until = activation_deadline(delivery)

    def check():
        return activation_authority(delivery, install, until)

    pilot.require(selected(install) == sha, "the deployment intent has no active candidate selector")
    beats = fresh_health(install, sha, data["activation_after"], data["old_pid"], check, until=until)
    policy = check()
    zte_delivery.require_landing(delivery, policy.client(util.iso(until)), sha)
    final = health(install, sha, after=data["activation_after"], old_pid=data["old_pid"], until=until)
    with util.lock(zte.INTERLOCK) as got:
        if not got:
            return
        with db.tx(timeout=activation_timeout(until, 2)):
            check()
            database(install, until=until)
            pilot.require(selected(install) == sha, "the selected runtime changed during validation")
            pilot.require(final["pid"] == beats[-1]["pid"] and util.pid_alive(final["pid"]),
                          "the validated runtime exited or restarted before completion")
            activation_time_left(until)
            store.complete(delivery, data["pr_task_version"], release_health=beats, completion_health=final)
            activation_time_left(until)


def tick() -> int:
    with util.lock(RELEASE_LOCK) as got:
        if not got:
            return 0
        delivery = store.latest()
        if not delivery or delivery.phase not in ("release_ready", "deploy_intent", "rollback_intent"):
            return 0
        try:
            install = installation(delivery.data.get("installation"))
            if delivery.phase == "release_ready":
                deploy(install, delivery)
            elif delivery.phase == "deploy_intent":
                finish(install, delivery)
            else:
                restore(install, delivery)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error, http.client.HTTPException,
                subprocess.SubprocessError) as error:
            current = store.find(delivery.task)
            if current and current.phase == "deploy_intent":
                try:
                    restore(installation(current.data.get("installation")), current, reason=failure_reason(error))
                except (OSError, RuntimeError, ValueError, TypeError, KeyError, http.client.HTTPException,
                        subprocess.SubprocessError) as rollback_error:
                    zte.hold(store.find(delivery.task), rollback_error, uncertain=True)
                    return 1
            elif current and current.phase not in store.TERMINAL:
                zte.hold(current, error, uncertain=current.phase == "rollback_intent")
                return 1
    return 0


def launch(receipt: dict | None = None) -> None:
    install = installation(receipt)
    sha = selected(install)
    runtime = verify_runtime(install, sha)
    os.chdir(runtime)
    python = runtime / ".venv/bin/python"
    os.execv(python, [str(python), "-m", "arete.cli", "serve"])


def write_launch_agents() -> None:
    install = installation()
    supervisor = codebase.CHECKOUT.resolve()
    pilot.require(not supervisor.is_relative_to(install.root / "releases")
                  and pilot.private(supervisor, stat.S_ISDIR),
                  "generate the service agents from the private pinned supervisor outside selectable runtimes")
    python = supervisor / ".venv/bin/python"
    pilot.require(python.is_file(), "the pinned supervisor does not have a locked Python runtime")
    verify_runtime(install, selected(install))
    destination = install.root / "launch-agents"
    destination.mkdir(mode=0o700, exist_ok=True)
    pilot.require(pilot.private(destination, stat.S_ISDIR), "the launch agent output directory is not private")
    bootstrap = f"import sys; sys.path.insert(0, {str(supervisor)!r}); "
    launcher = bootstrap + f"from arete.zte_release import launch; launch({install.to_receipt()!r})"
    worker = bootstrap + "from arete.zte_release import main; raise SystemExit(main())"
    env = {"HOME": str(paths.HOME), "PATH": service_install.build_path(), "ARETE_CONFIG_DIR": str(paths.CONFIG_DIR),
           "ARETE_STATE_DIR": str(paths.STATE_DIR), "ARETE_LOG_DIR": str(paths.LOG_DIR),
           "ARETE_WORKTREES_DIR": str(paths.WORKTREES_DIR)}
    for label, role, scheduling in ((install.label, "launch", {"KeepAlive": True, "ThrottleInterval": 10}),
            (install.label + ".zte-release", "tick", {"StartInterval": 10})):
        log = str(paths.LOG_DIR / ("serve.log" if role == "launch" else "zte-release.log"))
        program = [str(python), "-I", "-c", launcher] if role == "launch" else [str(python), "-I", "-c", worker, role]
        profile = {"Label": label, "ProgramArguments": program,
                   "EnvironmentVariables": env, "WorkingDirectory": str(supervisor), "RunAtLoad": True,
                   "StandardOutPath": log, "StandardErrorPath": log, "ProcessType": "Standard", **scheduling}
        target = destination / (label + ".plist")
        util.write_durably(target, plistlib.dumps(profile), mode=0o600)
        print(target)


def main() -> int:
    codebase.load()
    if sys.argv[1:] == ["launch"]:
        launch()
    elif sys.argv[1:] == ["tick"]:
        with principals.acting("rule:zte-release"):
            return tick()
    elif sys.argv[1:] == ["write-launch-agents"]:
        pilot.require(not any(os.environ.get(k) for k in ("ARETE_AGENT", "ARETE_ACTOR", "ARETE_RUN_ID")),
                      "only a local operator can prepare managed launch agents")
        write_launch_agents()
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
