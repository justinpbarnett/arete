from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from pathlib import Path

from . import config, emits, environment, loops, paths, push, scrub, secretstore, senders, util
from .store import db as foundation
from .store import outbox as outbox_store

_NON_FF_RE = re.compile(
    r"non-fast-forward|fetch first|\[rejected\]|failed to push some refs|stale info",
    re.IGNORECASE,
)
_FORBIDDEN_HOST_KEYS = frozenset(("workdir", "file", "env"))


class PushError(RuntimeError):
    pass


class PushUnsure(PushError):
    pass


class PushRefused(PushError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class SecretScanError(PushError):
    pass


def _secret_entries() -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    if secretstore.configured_registry() and not foundation.isolated_busy():
        with contextlib.suppress(
            OSError,
            sqlite3.Error,
            foundation.NoDatabase,
            foundation.IncompatibleSchema,
            secretstore.StoreError,
            secretstore.StoreMissing,
            RuntimeError,
            ValueError,
        ):
            for row, value in secretstore.entries():
                if not isinstance(value, str) or not value:
                    continue
                addr = secretstore.address(row["scope"], row["name"])
                key = (addr, value)
                if key not in seen:
                    seen.add(key)
                    entries.append(key)
    redactions = scrub.load()
    for form, (_marker, row) in redactions.labelled.items():
        addr = secretstore.address(row["scope"], row["name"])
        key = (addr, form)
        if key not in seen:
            seen.add(key)
            entries.append(key)
    return entries


def scan_text_for_secrets(text: str) -> str | None:
    if not text:
        return None
    for addr, value in _secret_entries():
        if value and value in text:
            return addr
    return None


def scan_commit_diff(repo_path: Path, head: str, *, base: str = "", start: str = "") -> None:
    diff_text = ""
    if start:
        try:
            diff_text = util.git(["diff", "--no-renames", f"{start}..{head}"], repo_path, check=True)
        except RuntimeError:
            diff_text = ""
    if not diff_text and base:
        for ref in (f"origin/{base}", base):
            try:
                diff_text = util.git(["diff", "--no-renames", f"{ref}..{head}"], repo_path, check=True)
                break
            except RuntimeError:
                continue
    if not diff_text:
        try:
            diff_text = util.git(["show", "--format=", "--no-renames", head], repo_path, check=False)
        except RuntimeError:
            diff_text = ""
    if hit := scan_text_for_secrets(diff_text):
        raise SecretScanError(f"secret scan blocked push: diff contains {hit}")


def parse_push_payload(body: str) -> dict[str, object]:
    try:
        parsed = json.loads(body)
    except ValueError as error:
        raise ValueError(f"push payload must be JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("push payload must be a JSON object")
    for forbidden in _FORBIDDEN_HOST_KEYS:
        if forbidden in parsed:
            raise ValueError(f"push payload must not hold host key {forbidden!r}")
    mode = str(parsed.get("mode") or "land")
    if mode not in ("land", "branch"):
        raise ValueError(f"unsupported push mode: {mode!r}")
    repo = str(parsed.get("repo") or "").strip()
    branch = str(parsed.get("branch") or "").strip()
    base = str(parsed.get("base") or "").strip()
    head = str(parsed.get("head") or "").strip()
    if not repo:
        raise ValueError("push payload requires a configured repo name")
    if not head:
        raise ValueError("push payload requires a head sha")
    if mode == "land" and not base:
        raise ValueError("land push payload requires base")
    if mode == "branch" and not branch:
        raise ValueError("branch push payload requires branch")
    return dict(parsed)


def resolve_repo_and_head(cfg: config.Config, repo_name: str, head: str) -> config.Repo:
    repo = emits.config_repo(cfg, repo_name)
    if repo is None:
        raise PushError(f"repo {repo_name!r} is not configured on this host (head {head})")
    if not repo.path.is_dir():
        raise PushError(f"repo {repo_name!r} checkout is missing on this host (head {head})")
    try:
        util.git(["cat-file", "-e", f"{head}^{{commit}}"], repo.path, check=True)
    except RuntimeError as error:
        raise PushError(f"head {head} is missing from repo {repo_name!r}") from error
    return repo


def _record_external_id(item: dict, external_id: str) -> None:
    item_id = item.get("id")
    attempt_n = item.get("attempt_n")
    if item_id is not None and attempt_n is not None:
        outbox_store.record_attempt_external_id(int(item_id), int(attempt_n), external_id)


def _after_push_hook(item: dict, head: str) -> None:
    """Hook point for crash-recovery tests; a no-op in production."""
    _ = (item, head)


def push_pr_branch(
    cfg: config.Config,
    *,
    repo_name: str,
    base: str,
    branch: str,
    head: str,
    lease: str = "",
    use_lease: bool = True,
    force: bool = False,
    start: str = "",
    item: dict | None = None,
    timeout: int = 600,
) -> str:
    util.validate_git_ref(branch, "branch")
    repo = resolve_repo_and_head(cfg, repo_name, head)
    if "origin" not in util.git(["remote"], repo.path, check=False).split():
        raise PushError(f"{repo.name} has no origin remote to push to")
    scan_commit_diff(repo.path, head, base=base, start=start)
    if item is not None:
        _record_external_id(item, head)
    ref = f"refs/heads/{branch}"
    wt_name = f"push-branch-{item['id'] if item and item.get('id') is not None else head[:12]}"
    with push.branch_worktree(repo.path, head, wt_name) as wt:
        argv = ["push", "-q", "--no-follow-tags", "--recurse-submodules=no"]
        if force:
            argv.append("--force")
        elif use_lease:
            argv.append(f"--force-with-lease={ref}:{lease}")
        argv += ["origin", f"{head}:{ref}"]
        try:
            util.git(argv, wt, timeout=timeout)
        except RuntimeError as error:
            err_text = str(error)
            if _NON_FF_RE.search(err_text):
                raise PushRefused(err_text, retryable=False) from error
            raise PushUnsure(err_text) from error
    if item is not None:
        _after_push_hook(item, head)
    return head


def _land_push(item: dict, payload: dict[str, object], cfg: config.Config) -> senders.SendResult:
    repo_name = str(payload["repo"])
    base = str(payload["base"])
    branch = str(payload.get("branch") or "")
    head = str(payload["head"])
    start = str(payload.get("start") or "")
    check_cmd = str(payload.get("check") or "")
    require_check = bool(payload.get("require_check", True))
    timeout = int(payload.get("timeout") or 600)
    loop_name = str(payload.get("loop") or item.get("loop") or "")
    run_id = str(item.get("run_id") or "")

    if require_check and loops.blank(check_cmd):
        raise PushError(
            "it has no check to run after the rebase, so it cannot land: reject it. Its loop now "
            "loads only with a push check, so every later push from it has one"
        )
    configured = emits.config_repo(cfg, repo_name)
    if configured is not None and require_check and (reason := push.policy_refusal(configured, base)):
        raise PushError(reason)
    repo = resolve_repo_and_head(cfg, repo_name, head)
    if require_check and (reason := push.policy_refusal(repo, base)):
        raise PushError(reason)
    if "origin" not in util.git(["remote"], repo.path, check=False).split():
        raise PushError(f"{paths.short_path(repo.path)} has no origin to push to")

    wt_name = f"push-{item.get('id') or run_id or head[:12]}"
    with push.branch_worktree(repo.path, head, wt_name) as wt:
        util.git(["fetch", "-q", "origin", base], repo.path)
        util.git(["reset", "-q", "--hard"], wt)
        util.git(["clean", "-qfd"], wt)
        try:
            push.rebase_onto(wt, base)
        except push.NoLongerRebases as error:
            raise PushError(str(error)) from error
        push_head = util.git(["rev-parse", "HEAD"], wt).strip()
        scan_commit_diff(wt, push_head, base=base, start=start)
        if check_cmd:
            env_sets: dict[str, str] = {}
            if loop_name:
                try:
                    env_sets = loops.check_env(loops.find(loop_name, cfg), run_id)
                except (FileNotFoundError, ValueError, RuntimeError):
                    env_sets = {}
            gate = environment.sh(check_cmd, cfg, repo, sets=env_sets, cwd=wt, timeout=timeout)
            if gate.returncode != 0:
                raise PushError(
                    f"`{check_cmd}` exits {gate.returncode} after the rebase: "
                    f"{util.last_line((gate.stdout or '') + (gate.stderr or ''), 300)}"
                )
        _record_external_id(item, push_head)
        try:
            util.git(["push", "-q", "origin", f"{push_head}:refs/heads/{base}"], wt)
        except RuntimeError as error:
            err_text = str(error)
            if _NON_FF_RE.search(err_text):
                raise PushRefused(err_text, retryable=True) from error
            raise PushUnsure(err_text) from error
        _after_push_hook(item, push_head)

    util.git(["fetch", "-q", "origin", base], repo.path, check=False)
    push.fast_forward(repo.path, base, push_head)
    if branch:
        util.delete_branch(repo.path, branch)
    return senders.SendResult(external_id=push_head, external_url=f"git:{repo.name}@{base}:{push_head[:10]}")


def _push_send(item: dict, body: str) -> senders.SendResult:
    payload = parse_push_payload(body)
    cfg = config.load()
    if payload.get("mode", "land") == "branch":
        head = push_pr_branch(
            cfg,
            repo_name=str(payload["repo"]),
            base=str(payload.get("base") or ""),
            branch=str(payload["branch"]),
            head=str(payload["head"]),
            lease=str(payload.get("lease") or ""),
            use_lease=bool(payload.get("use_lease", True)),
            force=bool(payload.get("force", False)),
            start=str(payload.get("start") or ""),
            item=item,
            timeout=int(payload.get("timeout") or 600),
        )
        return senders.SendResult(
            external_id=head,
            external_url=f"git:{payload['repo']}@{payload['branch']}:{head[:10]}",
        )
    return _land_push(item, payload, cfg)


def _push_look(item: dict, since_iso: str) -> senders.LookResult:
    payload = parse_push_payload(str(item.get("body") or "{}"))
    cfg = config.load()
    repo_name = str(payload["repo"])
    head = str(item.get("prev_external_id") or payload.get("head") or "")
    repo = emits.config_repo(cfg, repo_name)
    if repo is None or not repo.path.is_dir():
        return senders.LookResult(
            found=False,
            external_id=None,
            external_url=None,
            summary=f"push look: repo {repo_name!r} unavailable",
        )
    if not head:
        return senders.LookResult(
            found=False,
            external_id=None,
            external_url=None,
            summary=f"push look: no external_id for {repo_name}",
        )
    mode = str(payload.get("mode") or "land")
    if mode == "branch":
        branch = str(payload["branch"])
        remote_line = util.git(["ls-remote", "origin", f"refs/heads/{branch}"], repo.path, check=False).strip()
        remote_sha = remote_line.split()[0] if remote_line else ""
        if remote_sha == head:
            return senders.LookResult(
                found=True,
                external_id=head,
                external_url=f"git:{repo.name}@{branch}:{head[:10]}",
                summary=f"ls-remote origin refs/heads/{branch}: matched {head[:10]}",
            )
        return senders.LookResult(
            found=False,
            external_id=None,
            external_url=None,
            summary=f"ls-remote origin refs/heads/{branch}: {remote_sha[:10] or 'missing'} != {head[:10]}",
        )

    base = str(payload["base"])
    util.git(["fetch", "-q", "origin", base], repo.path)
    check = util.sh(["git", "merge-base", "--is-ancestor", head, f"origin/{base}"], cwd=repo.path)
    if check.returncode == 0:
        push.fast_forward(repo.path, base, head)
        if branch := str(payload.get("branch") or ""):
            util.delete_branch(repo.path, branch)
        return senders.LookResult(
            found=True,
            external_id=head,
            external_url=f"git:{repo.name}@{base}:{head[:10]}",
            summary=f"merge-base --is-ancestor {head[:10]} origin/{base}: found",
        )
    return senders.LookResult(
        found=False,
        external_id=None,
        external_url=None,
        summary=f"merge-base --is-ancestor {head[:10]} origin/{base}: not ancestor",
    )


def _push_preview(item: dict, body: str) -> str:
    payload = parse_push_payload(body)
    target_ref = payload.get("base") if payload.get("mode", "land") == "land" else payload.get("branch")
    return f"push {payload['repo']} {str(payload['head'])[:10]} -> {target_ref}"


def cleanup_rejected(item: dict, body: str) -> None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return
    if not isinstance(parsed, dict):
        return
    branch = str(parsed.get("branch") or "").strip()
    repo_name = str(parsed.get("name") or parsed.get("repo") or "").strip()
    if not branch or not repo_name:
        return
    with contextlib.suppress(Exception):
        cfg = config.load()
        repo = emits.config_repo(cfg, repo_name)
        if repo is not None and repo.path.is_dir():
            util.delete_branch(repo.path, branch)


def register_all() -> None:
    senders.register(senders.Sender("git", "push", _push_send, _push_look, _push_preview))
