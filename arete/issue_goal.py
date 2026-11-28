from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import paths, util

SOURCE = Path(__file__).with_name("durable")
BUNDLE = ("issue.mjs", "refresh.mjs", "package.json", "package-lock.json")


def identity(repo, ticket):
    return hashlib.sha256(json.dumps([repo, str(ticket)]).encode()).hexdigest()


def directory(key):
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("invalid issue goal identity")
    return paths.STATE_DIR / "issue-goals" / key


def exists(repo, ticket):
    return (directory(identity(repo, ticket)) / "issue.sqlite").is_file()


def executable(name):
    from . import pi

    local = Path(pi.binary()).with_name(name)
    if local.is_absolute() and os.access(local, os.X_OK):
        return str(local)
    if found := shutil.which(name):
        return found
    raise ValueError(f"Pi Durable requires {name} on the serve host")


def runtime():
    if paths.ISOLATED:
        raise ValueError("Native issue goals require a trusted serve state directory")
    digest = hashlib.sha256()
    for name in BUNDLE:
        digest.update((SOURCE / name).read_bytes())
    cache = paths.STATE_DIR / "durable-runtime"
    installed = cache / digest.hexdigest()
    with util.lock("install-durable-runtime", wait=True):
        if not installed.exists():
            cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(dir=cache) as temp:
                stage = Path(temp) / "runtime"
                stage.mkdir()
                for name in BUNDLE:
                    shutil.copyfile(SOURCE / name, stage / name)
                try:
                    from . import environment

                    subprocess.run([executable("npm"), "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=stage,
                                   check=True, timeout=240, stdout=subprocess.DEVNULL,
                                   env=environment.build((), {"PATH": str(Path(executable("node")).parent) + os.pathsep
                                                              + os.environ.get("PATH", "")}))
                except subprocess.SubprocessError as error:
                    raise ValueError("Could not install the locked Pi Durable runtime") from error
                stage.rename(installed)
    return installed


def lifecycle(repo, ticket, state):
    if paths.ISOLATED:
        return
    key = identity(repo, ticket)
    folder = directory(key)
    if state != "start" and not (folder / "issue.sqlite").exists():
        return
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with util.lock("issue-goal-" + key, wait=True):
        try:
            subprocess.run([executable("node"), str(runtime() / "issue.mjs"), str(folder), state], timeout=30,
                           env={"PATH": os.environ["PATH"]}, check=True, stdout=subprocess.DEVNULL)
        except subprocess.SubprocessError as error:
            raise ValueError(f"Pi Durable could not record issue goal transition {state}") from error
