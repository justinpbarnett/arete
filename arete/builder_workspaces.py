"""Engine-owned Git objects and private working copies for orchestration workers."""
from __future__ import annotations

import os
import re
import shutil
import stat
import unicodedata
from pathlib import Path
from uuid import uuid4

from . import diffguard, factory, paths, sandbox, util

CLAIM = re.compile(r"[^\x00-\x1f\x7f*?\[\]\\]{1,512}")


def covers(parent, child):
    return child == parent or child.startswith(parent + "/")


def overlaps(left, right):
    left = [unicodedata.normalize("NFC", path).casefold() for path in left]
    right = [unicodedata.normalize("NFC", path).casefold() for path in right]
    return any(covers(a, b) or covers(b, a) for a in left for b in right)


def validate_claims(names, layer, checkouts):
    if not 1 <= len(names) <= 32:
        raise ValueError("builders need between 1 and 32 literal write_paths")
    prefixes = [c.dir for c in checkouts]
    for i, name in enumerate(names):
        if (not isinstance(name, str) or not CLAIM.fullmatch(name) or name != name.strip()
                or any(p in ("", ".", "..") or p.casefold() == ".git" for p in name.split("/"))
                or overlaps([name], names[:i])):
            raise ValueError("write_paths must be distinct literal checkout-relative files or directories")
        prefix = next((p for p in prefixes if not p or name.startswith(p + "/")), None)
        if prefix is None:
            raise ValueError(f"{name} must be inside an admitted checkout")
        relative = name.removeprefix(prefix + "/") if prefix else name
        if sandbox.secret_path(name) or any(util.matches_any(path, diffguard.denied_paths(layer))
                                            for path in (name, relative, name + "/", relative + "/")):
            raise ValueError(f"denied write claim: {name}")


def identity(path):
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or path.resolve() != path.parent.resolve() / path.name:
        raise ValueError(f"workspace identity changed: {path.name}")
    return [s.st_dev, s.st_ino]


def repository(token, n):
    return paths.STATE_DIR / "builder-objects" / token / str(n)


def folder(token):
    return paths.WORKTREES_DIR / f"candidate-{token}"


def pin(repo, wt, tree, ref):
    # A local fetch copies the entire tree, including blobs of accepted uncommitted files.
    commit = util.git(["-c", "user.name=Arete", "-c", "user.email=arete@localhost",
                       "commit-tree", tree, "-m", "Worker snapshot"], wt).strip()
    util.git(["-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
              str(wt), f"+{commit}:refs/arete/{ref}"], repo, retry=False)
    return commit


def private_git(repo, wt, args):
    return util.git([f"--git-dir={repo}", f"--work-tree={wt}", "-c", "core.hooksPath=/dev/null", *args],
                    wt, retry=False)


class MergeConflict(ValueError):
    pass


def create(checkouts, ws, pinned=None):
    token = uuid4().hex
    root = folder(token)
    root.mkdir(mode=0o700)
    data = {"token": token, "identity": identity(root), "parts": []}
    try:
        objects = repository(token, 0).parent
        objects.mkdir(mode=0o700, parents=True)
        data["objects_identity"] = identity(objects)
        materialize(data, checkouts, ws, pinned)
    except BaseException:
        release(data)
        raise
    return data


def release(data):
    token = data["token"]
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("invalid candidate token")
    root = folder(token)
    if root.exists():
        if identity(root) != data["identity"]:
            raise ValueError("candidate ownership changed before cleanup")
        shutil.rmtree(root)
    objects = repository(token, 0).parent
    if objects.exists():
        if identity(objects) != data.get("objects_identity"):
            raise ValueError("candidate object store changed before cleanup")
        shutil.rmtree(objects)


def unsupported_tree(repo, tree):
    return any(entry.startswith("160000 ") or sandbox.secret_path(entry.partition("\t")[2])
               for entry in util.git_records(["ls-tree", "-r", tree], repo))


def materialize(data, checkouts, ws, pinned):
    token = data["token"]
    root = folder(token)
    for n, c in enumerate(checkouts):
        if (c.dir and (Path(c.dir).is_absolute() or ".." in Path(c.dir).parts)
                or c.wt.resolve() != ws.resolve() / c.dir):
            raise ValueError("unsupported candidate checkout layout")
        source = repository(pinned["token"], n) if pinned else c.wt
        tree = pinned["parts"][n]["tree"] if pinned else factory.fingerprint(c.wt)
        if not tree:
            raise ValueError("cannot snapshot accepted delivery files")
        if unsupported_tree(source, tree):
            raise ValueError("private candidates require checkouts without submodules or tracked credentials")
        repo = repository(token, n)
        repo.mkdir(parents=True, mode=0o700)
        util.git(["init", "--bare", "-q"], repo)
        base = pin(repo, source, tree, "base")
        target = root / c.dir
        target.mkdir(exist_ok=True, parents=True)
        private_git(repo, target, ["read-tree", "--reset", "-u", tree])
        data["parts"].append({"dir": c.dir, "base": base, "tree": tree, "checkout": identity(c.wt),
                              "candidate": identity(target), "git": util.git(["rev-parse", "--absolute-git-dir"],
                              c.wt).strip(), "head": util.git(["rev-parse", "HEAD"], c.wt).strip()})


def parts(data, checkouts):
    root = folder(data["token"])
    if identity(repository(data["token"], 0).parent) != data["objects_identity"]:
        raise ValueError("candidate object store identity changed")
    if identity(root) != data["identity"] or [c.dir for c in checkouts] != [p["dir"] for p in data["parts"]]:
        raise ValueError("candidate ownership or checkout set changed")
    if len(checkouts) > 1 or checkouts[0].dir:
        targets = {root / c.dir for c in checkouts}
        for base, dirs, files in os.walk(root):
            parent = Path(base)
            if files or any(not any(target.is_relative_to(parent / d) for target in targets) for d in dirs):
                raise ValueError("candidate changed files outside its assigned checkouts")
            dirs[:] = [d for d in dirs if parent / d not in targets]
    for n, (part, c) in enumerate(zip(data["parts"], checkouts, strict=True)):
        wt = root / c.dir
        if (wt.resolve() != root.resolve() / c.dir or identity(wt) != part["candidate"]
                or identity(c.wt) != part["checkout"]
                or util.git(["rev-parse", "--absolute-git-dir"], c.wt).strip() != part["git"]
                or util.git(["rev-parse", "HEAD"], c.wt).strip() != part["head"]):
            raise ValueError("candidate or delivery checkout identity changed")
        yield part, c, wt, repository(data["token"], n)


def check_files(wt):
    for root, dirs, files in os.walk(wt):
        for name in [*dirs, *files]:
            path = Path(root) / name
            s = path.lstat()
            if (name.casefold() == ".git" or stat.S_ISREG(s.st_mode) and s.st_nlink != 1
                    or not (stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode) or stat.S_ISLNK(s.st_mode))):
                raise ValueError(f"unsupported candidate file: {path.relative_to(wt)}")
            if stat.S_ISLNK(s.st_mode) and not path.resolve().is_relative_to(wt.resolve()):
                raise ValueError(f"candidate link escapes its checkout: {path.relative_to(wt)}")


def stage(data, checkouts, layer, claims):
    intent, aggregate = [], diffguard.Diff()
    for part, c, wt, repo in parts(data, checkouts):
        check_files(wt)
        private_git(repo, wt, ["read-tree", part["tree"]])
        private_git(repo, wt, ["add", "-A", "--", ".", *diffguard.NEVER_STAGED])
        candidate = private_git(repo, wt, ["write-tree"]).strip()
        delta = diffguard.between(repo, part["tree"], candidate, deny=diffguard.denied_paths(layer), prefix=c.prefix)
        if delta.denied or any(sandbox.secret_path(p) or not any(covers(scope, p) for scope in claims)
                               for p in delta.files):
            raise ValueError("candidate changed denied or unclaimed paths: " + ", ".join(delta.denied or delta.files))
        proposed = util.git([*util.ARETE_IDENTITY, "commit-tree", candidate, "-p", part["base"], "-m", "Candidate"],
                            repo).strip()
        util.git(["update-ref", "refs/arete/candidate", proposed], repo)
    for part, c, _wt, repo in parts(data, checkouts):
        proposed = util.git(["rev-parse", "refs/arete/candidate"], repo).strip()
        candidate = util.git(["rev-parse", proposed + "^{tree}"], repo).strip()
        pre = factory.fingerprint(c.wt)
        if not pre:
            raise ValueError("delivery preimage unavailable")
        accepted = pin(repo, c.wt, pre, "pre")
        merge = util.sh(["git", "merge-tree", "--write-tree", "--merge-base=" + part["base"], accepted, proposed], cwd=repo)
        if merge.returncode:
            raise MergeConflict("candidate conflicts with accepted delivery files: " + merge.stdout[41:1041].strip())
        post = merge.stdout.strip()
        util.git(["update-ref", "refs/arete/integration", post], repo)
        baseline = c.onto or c.base_commit or part["head"]
        baseline = util.git(["rev-parse", baseline + "^{tree}"], c.wt).strip()
        pin(repo, c.wt, baseline, "baseline")
        aggregate += diffguard.between(repo, baseline, post, deny=diffguard.denied_paths(layer), prefix=c.prefix, moves=True)
        intent.append({"dir": c.dir, "pre": pre, "post": post, "candidate": candidate})
    if why := aggregate.refusal(layer.max_diff_lines):
        raise ValueError(why)
    return intent


def rebase(data, checkouts, ws):
    # Retain the original candidate and materialize Git's merge result (including conflict markers)
    # on a new base. Each part of the replacement starts at the latest accepted delivery state.
    new = create(checkouts, ws)
    try:
        for n, (part, _, _, src) in enumerate(parts(data, checkouts)):
            target = folder(new["token"]) / part["dir"]
            repo = repository(new["token"], n)
            util.git(["fetch", "--quiet", "--no-tags", str(src), "refs/arete/candidate:refs/arete/prior"], repo,
                     retry=False)
            merge = util.sh(["git", "merge-tree", "--write-tree", "--merge-base=" + part["base"],
                             new["parts"][n]["base"], "refs/arete/prior"], cwd=repo)
            tree = merge.stdout.partition("\n")[0]
            if merge.returncode not in (0, 1) or not re.fullmatch(r"[0-9a-f]{40,64}", tree):
                raise ValueError("cannot materialize retained merge conflict")
            private_git(repo, target, ["read-tree", "--reset", "-u", tree])
    except BaseException:
        release(new)
        raise
    return new


def apply(data, checkouts, intent):
    locations = list(parts(data, checkouts))
    if len(intent) != len(locations):
        raise ValueError("integration receipt has a different checkout set")
    for item, (_, c, _, _) in zip(intent, locations, strict=True):
        if c.dir != item["dir"] or factory.fingerprint(c.wt) not in (item["pre"], item["post"]):
            raise ValueError(f"unexpected delivery files in {c.dir or '.'}; reconcile the retained trees")
    for item, (_, c, _, repo) in zip(intent, locations, strict=True):
        before = factory.fingerprint(c.wt)
        if before == item["post"]:
            continue
        if before != item["pre"]:
            raise ValueError("delivery preimage changed before integration")
        private_git(repo, c.wt, ["read-tree", before])
        private_git(repo, c.wt, ["update-index", "--refresh"])
        private_git(repo, c.wt, ["read-tree", "-m", "-u", before, item["post"]])
        if factory.fingerprint(c.wt) != item["post"]:
            raise ValueError("delivery postimage requires reconciliation")
