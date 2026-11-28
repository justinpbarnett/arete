from __future__ import annotations

import hashlib
import json
import shutil
import tomllib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import codebase, config, configrepo, paths, prices, principals, steps, util
from .store import blobs as blob_store
from .store import db as foundation
from .store import runs as run_store


@dataclass
class Resolution:
    kind: str
    engine_sha: str
    config_commit: str
    config_dir: Path
    schema_version: int
    cfg: config.Config
    layer: config.RepoLayer | None
    repo: config.Repo | None
    task_kind: str
    adw: dict | None
    settings: dict[str, steps.Settings]
    settings_provenance: dict[str, dict[str, dict]]
    settings_snapshots: dict[str, str]
    settings_sha: dict[str, str]
    prompt_texts: dict[str, str]
    prompts: dict[str, dict]
    schemas: dict[str, dict]
    loop_files: dict[str, str]
    expertise: dict | None
    prices: dict | None
    overrides: dict
    arm: dict | None
    harness: dict | None
    resolved_sha: str
    resolved_bytes: bytes
    blob_payloads: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    payer: str | None = None
    ab_held: dict[str, tuple[str, str, dict]] = field(default_factory=dict)

    @property
    def config(self) -> config.Config:
        return self.cfg

    def settings_for(self, role: str) -> steps.Settings | None:
        canonical = config.STEP_ALIASES.get(role, role)
        return self.settings.get(canonical)

    def settings_sha_for(self, role: str) -> str:
        canonical = config.STEP_ALIASES.get(role, role)
        return self.settings_sha.get(canonical, "")

    def schema_version_for(self, step_name: str) -> str:
        entry = self.schemas.get(step_name)
        return entry["id"] if isinstance(entry, dict) else ""

    def write_blobs(self) -> tuple[tuple[str, str, int], ...]:
        rows: list[tuple[str, str, int]] = []
        for sha, (blob_kind, data) in self.blob_payloads.items():
            blob_store.write_file(data, sha)
            rows.append((sha, blob_kind, len(data)))
        return tuple(rows)


_current: ContextVar[Resolution | None] = ContextVar("pins_current", default=None)


def current() -> Resolution | None:
    return _current.get()


@contextmanager
def use(resolution: Resolution):
    token = _current.set(resolution)
    try:
        yield resolution
    finally:
        _current.reset(token)


def active_config() -> config.Config:
    if (pin := _current.get()) is not None:
        return pin.cfg
    return config.load()


def relative_path(target: Path | str, *, repo_root: Path | None = None, config_dir: Path | None = None) -> str:
    raw = Path(target)
    if not raw.is_absolute():
        return raw.as_posix()
    resolved = raw.resolve()
    bases: list[tuple[str, Path]] = []
    if config_dir is not None:
        bases.append(("", config_dir.resolve()))
    bases.extend((
        ("", paths.CONFIG_DIR.resolve()),
        ("", paths.STATE_DIR.resolve()),
        ("", paths.WORKTREES_DIR.resolve()),
    ))
    if repo_root is not None:
        bases.append(("", repo_root.resolve()))
    bases.append(("", paths.PACKAGE_DIR.parent.resolve()))
    for prefix, base in bases:
        if resolved == base:
            return prefix or "."
        if resolved.is_relative_to(base):
            rel = resolved.relative_to(base).as_posix()
            return f"{prefix}/{rel}" if prefix else rel
    return raw.name


def config_checkout(commit: str) -> Path:
    dest = paths.STATE_DIR / "config-at" / commit
    if (dest / ".git").exists():
        return dest
    with util.lock(f"config-at-{commit[:12]}", wait=True):
        if (dest / ".git").exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        configrepo._git(["worktree", "add", "--detach", str(dest), commit], paths.CONFIG_DIR)
        return dest


def reap_config_worktrees() -> list[str]:
    root = paths.STATE_DIR / "config-at"
    if not root.is_dir() or not configrepo.is_repo():
        return []
    active = run_store.running_config_commits()
    removed: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name in active:
            continue
        with util.lock(f"config-at-{entry.name[:12]}", wait=False) as got:
            if not got:
                continue
            if entry.name in run_store.running_config_commits():
                continue
            configrepo._git(["worktree", "remove", "--force", str(entry)], paths.CONFIG_DIR, check=False)
            if entry.exists():
                shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    if removed:
        configrepo._git(["worktree", "prune"], paths.CONFIG_DIR, check=False)
    return removed


def _rebase_path(path: Path | None, live_root: Path, pinned_root: Path) -> Path | None:
    if path is None:
        return None
    resolved = path.resolve()
    if resolved == live_root:
        return pinned_root
    if resolved.is_relative_to(live_root):
        return pinned_root / resolved.relative_to(live_root)
    return path


def _rebase_cfg(cfg: config.Config, live_root: Path, pinned_root: Path) -> config.Config:
    repos = [
        replace(r, layer=_rebase_path(r.layer, live_root, pinned_root))
        for r in cfg.repos
    ]
    loop_dirs = [_rebase_path(d, live_root, pinned_root) or d for d in cfg.loop_dirs]
    adws = _rebase_path(cfg.adws, live_root, pinned_root)
    return replace(cfg, repos=repos, loop_dirs=loop_dirs, adws=adws)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def schema_digest(schema_obj: dict) -> str:
    return hashlib.sha256(_canonical_json_bytes(schema_obj)).hexdigest()


def schema_id(name: str, sha: str) -> str:
    return f"schema/{name}@{sha[:12]}"


def known_step_schemas() -> dict[str, dict]:
    from . import chat, patching, pipeline, review

    return {
        "plan": pipeline.PLAN_SCHEMA,
        "patch": patching.SCHEMA,
        "revise": pipeline.REVISE_SCHEMA,
        "review": review.REVIEW_SCHEMA,
        "learn": pipeline.LEARN_SCHEMA,
        "ci_fix": pipeline.CI_FIX_SCHEMA,
        "resolve": pipeline.RESOLVE_SCHEMA,
        "chat": chat.REPLY_SCHEMA,
    }


def step_schema_version(step_name: str) -> str:
    schemas = known_step_schemas()
    obj = schemas.get(step_name)
    if obj is None:
        return ""
    return schema_id(step_name, schema_digest(obj))


def identify_schema(schema_obj: dict, fallback_name: str = "output") -> tuple[str, str, str]:
    target_sha = schema_digest(schema_obj)
    for name, known in known_step_schemas().items():
        if schema_digest(known) == target_sha:
            return name, schema_id(name, target_sha), target_sha
    clean = fallback_name or "output"
    return clean, schema_id(clean, target_sha), target_sha


def _load_prompts(engine_sha: str, prompts_dir: Path | None, prompts_level: str, prompts_from: str,
                  *, repo_root: Path | None = None, config_dir: Path | None = None) -> tuple[dict[str, str], dict[str, dict]]:
    texts: dict[str, str] = {}
    meta: dict[str, dict] = {}
    if paths.PROMPTS_DIR.is_dir():
        for file in sorted(paths.PROMPTS_DIR.glob("*.md")):
            try:
                raw = file.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            sha = hashlib.sha256(raw).hexdigest()
            texts[file.stem] = text
            meta[file.stem] = {
                "id": f"prompt/{file.stem}@{sha[:12]}",
                "level": "engine",
                "from": f"arete/prompts/{file.name}@{engine_sha}",
                "sha256": sha,
            }
    if prompts_dir is not None and prompts_dir.resolve() != paths.PROMPTS_DIR.resolve() and prompts_dir.is_dir():
        level = prompts_level or "run"
        base_from = prompts_from or relative_path(prompts_dir, repo_root=repo_root, config_dir=config_dir)
        for file in sorted(prompts_dir.glob("*.md")):
            try:
                raw = file.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            sha = hashlib.sha256(raw).hexdigest()
            texts[file.stem] = text
            meta[file.stem] = {
                "id": f"prompt/{file.stem}@{sha[:12]}",
                "level": level,
                "from": f"{base_from}/{file.name}" if base_from and not base_from.endswith(file.name) else base_from,
                "sha256": sha,
            }
    return texts, meta


def _adw_info(cfg: config.Config, adw_path: str, engine_sha: str, config_dir: Path,
              repo_root: Path | None) -> dict | None:
    if not adw_path:
        return None
    candidates = []
    if cfg.adws:
        candidates.append(cfg.adws / f"{adw_path}.py")
    candidates.append(paths.ADWS_DIR / f"{adw_path}.py")
    for candidate in candidates:
        if candidate.is_file():
            raw = candidate.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            return {
                "path": relative_path(candidate, repo_root=repo_root, config_dir=config_dir),
                "version": f"adw/{adw_path}@{sha[:12]}",
                "sha256": sha,
            }
    return {"path": adw_path, "version": f"adw/{adw_path}@{engine_sha[:12]}" if engine_sha else adw_path}


def _collect_loop_files(cfg: config.Config, loop_name: str, config_dir: Path,
                        repo_root: Path | None) -> dict[str, str]:
    if not loop_name:
        return {}
    from . import loops

    try:
        found = loops.find(loop_name, cfg)
    except loops.LoopError:
        return {}
    out: dict[str, str] = {}
    if found.dir.is_dir():
        for file in sorted(found.dir.rglob("*")):
            if file.is_file() and not file.is_symlink():
                try:
                    sha = hashlib.sha256(file.read_bytes()).hexdigest()
                except OSError:
                    continue
                out[relative_path(file, repo_root=repo_root, config_dir=config_dir)] = sha
    return out


def _collect_harness(repo: config.Repo | None, layer: config.RepoLayer | None, harness_source,
                     blobs_out: dict[str, tuple[str, bytes]]) -> dict | None:
    if harness_source is None or repo is None:
        return None
    from . import evals

    files_meta: dict[str, dict] = {}
    for rel in sorted(evals.harness_files(harness_source)):
        link = harness_source.link(rel)
        if link is not None:
            files_meta[rel] = {"link": link, "sha256": "", "executable": False}
        else:
            data = harness_source.read(rel)
            sha = blob_store.digest(data)
            blobs_out[sha] = ("inputs", data)
            files_meta[rel] = {"link": None, "sha256": sha, "executable": bool(harness_source.executable(rel))}

    layer_config_sha = ""
    if repo.layer and (repo.layer / "config.toml").is_file():
        data = (repo.layer / "config.toml").read_bytes()
        layer_config_sha = blob_store.digest(data)
        blobs_out[layer_config_sha] = ("inputs", data)
    elif isinstance(harness_source, evals.HarnessOverlay) and not repo.layer and evals.LAYER_CONFIG not in files_meta:
        local_layer = repo.path / evals.LAYER_CONFIG
        if local_layer.is_file():
            data = local_layer.read_bytes()
            layer_config_sha = blob_store.digest(data)
            blobs_out[layer_config_sha] = ("inputs", data)

    expertise_sha = ""
    expertise_file = layer.expertise_file if layer is not None else repo.layer_dir / "expertise.md"
    if expertise_file.is_file():
        data = expertise_file.read_bytes()
        expertise_sha = blob_store.digest(data)
        blobs_out[expertise_sha] = ("inputs", data)

    return {
        "label": getattr(harness_source, "label", ""),
        "files": files_meta,
        "layer_config": layer_config_sha or None,
        "expertise": expertise_sha or None,
    }


def resolve(kind: str, *, repo: config.Repo | str | None = None, layer: config.RepoLayer | Path | None = None,
            overrides: dict[str, dict] | None = None, arm: dict | None = None,
            task_kind: str = "", prompts_dir: Path | None = None, prompts_level: str = "",
            prompts_from: str = "", ab_ticket: tuple[str, str] | None = None,
            ab_held: dict[str, tuple[str, str, dict]] | None = None,
            harness_source=None, adw_path: str = "", loop_name: str = "",
            cfg: config.Config | None = None, engine_sha: str | None = None) -> Resolution:
    engine_sha = codebase.STAMP if engine_sha is None else engine_sha
    schema_version = foundation.LATEST
    config_commit = configrepo.live_sha() if configrepo.is_repo() else ""
    live_root = paths.CONFIG_DIR.resolve()
    if config_commit:
        config_dir = config_checkout(config_commit)
        pinned_cfg = _rebase_cfg(config.load(config_dir / "config.toml", base_dir=config_dir), live_root, config_dir)
    else:
        config_dir = live_root
        pinned_cfg = cfg if cfg is not None else config.load()

    resolved_repo: config.Repo | None = None
    if isinstance(repo, str) and repo:
        try:
            resolved_repo = pinned_cfg.repo(repo)
        except config.UnknownRepo:
            resolved_repo = None
    elif isinstance(repo, config.Repo):
        try:
            resolved_repo = pinned_cfg.repo(repo.name)
        except config.UnknownRepo:
            resolved_repo = replace(repo, layer=_rebase_path(repo.layer, live_root, config_dir))

    repo_root = resolved_repo.path if resolved_repo is not None else None

    resolved_layer: config.RepoLayer | None = None
    if isinstance(layer, config.RepoLayer):
        rebased = _rebase_path(layer.dir, live_root, config_dir)
        if config_commit and rebased is not None and rebased != layer.dir and (rebased / "config.toml").is_file():
            resolved_layer = config.load_layer(rebased)
        else:
            resolved_layer = layer
    elif isinstance(layer, Path):
        rebased = _rebase_path(layer, live_root, config_dir) or layer
        resolved_layer = config.load_layer(rebased)
    elif resolved_repo is not None:
        resolved_layer = config.load_layer(resolved_repo.layer_dir)

    layer_from = ""
    if resolved_layer is not None and resolved_layer.config_file.is_file():
        rel_layer = relative_path(resolved_layer.config_file, repo_root=repo_root, config_dir=config_dir)
        layer_from = f"{rel_layer}@{config_commit}" if config_commit else rel_layer

    arm_name = ""
    arm_atom = ""
    arm_step_settings: dict[str, dict] = {}
    if isinstance(arm, dict) and arm:
        arm_name = str(arm.get("name") or "arm")
        arm_atom = str(arm.get("atom") or "")
        if isinstance(arm.get("frozen"), dict):
            for k, v in arm["frozen"].items():
                if isinstance(v, dict):
                    arm_step_settings[config.STEP_ALIASES.get(k, k)] = dict(v)
        if arm_atom and isinstance(arm.get("settings"), dict):
            arm_step_settings[config.STEP_ALIASES.get(arm_atom, arm_atom)] = dict(arm["settings"])

    step_overrides = {config.STEP_ALIASES.get(k, k): dict(v) for k, v in (overrides or {}).items() if isinstance(v, dict)}
    held_map: dict[str, tuple[str, str, dict]] = dict(ab_held or {})

    resolved_settings: dict[str, steps.Settings] = {}
    settings_prov: dict[str, dict[str, dict]] = {}
    snapshots: dict[str, str] = {}
    settings_sha: dict[str, str] = {}

    for role in config.STEP_ROLES:
        base_role = config.INHERITS.get(role, "")
        own_override = step_overrides.get(role)
        inh_override = step_overrides.get(base_role) if base_role else None
        own_arm = arm_step_settings.get(role)
        if own_arm is None and base_role and base_role in arm_step_settings:
            inh_override = {**(arm_step_settings[base_role]), **(inh_override or {})}
        try:
            s, prov = steps.resolve(
                pinned_cfg, resolved_layer, role, task_kind,
                overrides=own_override, inherited=inh_override,
                ticket=ab_ticket, held=held_map.get(role),
                provenance=True, config_commit=config_commit, layer_from=layer_from,
                arm_name=arm_name if own_arm is not None else "",
                arm_overrides=own_arm, engine_sha=engine_sha,
            )
        except ValueError:
            continue
        resolved_settings[role] = s
        settings_prov[role] = prov
        if s.ab_name and role not in held_map:
            held_map[role] = (s.ab_name, s.ab_side, json.loads(s.ab_arm or "{}"))
        snap_body = _canonical_json_bytes(prov).decode("utf-8")
        snap_sha = hashlib.sha256(snap_body.encode("utf-8")).hexdigest()
        snapshots[snap_sha] = snap_body
        settings_sha[role] = snap_sha

    prompt_texts, prompts_meta = _load_prompts(
        engine_sha, prompts_dir, prompts_level, prompts_from,
        repo_root=repo_root, config_dir=config_dir,
    )

    schemas_meta: dict[str, dict] = {}
    for sname, sobj in known_step_schemas().items():
        ssha = schema_digest(sobj)
        schemas_meta[sname] = {
            "id": schema_id(sname, ssha),
            "level": "engine",
            "from": f"arete@{engine_sha}",
            "sha256": ssha,
        }

    blobs_out: dict[str, tuple[str, bytes]] = {}
    expertise_meta: dict | None = None
    if resolved_layer is not None and resolved_layer.expertise_file.is_file():
        exp_bytes = resolved_layer.expertise_file.read_bytes()
        exp_sha = blob_store.digest(exp_bytes)
        blobs_out[exp_sha] = ("inputs", exp_bytes)
        expertise_meta = {
            "path": relative_path(resolved_layer.expertise_file, repo_root=repo_root, config_dir=config_dir),
            "sha256": exp_sha,
        }

    prices_meta: dict | None = None
    if (loaded_prices := prices.load()) is not None:
        prices_meta = {
            "version": loaded_prices.get("version", ""),
            "source": loaded_prices.get("source", ""),
            "sha256": hashlib.sha256(_canonical_json_bytes(loaded_prices)).hexdigest(),
        }

    adw_meta = _adw_info(pinned_cfg, adw_path, engine_sha, config_dir, repo_root)
    loop_files = _collect_loop_files(pinned_cfg, loop_name, config_dir, repo_root)
    harness_meta = _collect_harness(resolved_repo, resolved_layer, harness_source, blobs_out)

    arm_summary = None
    if arm:
        arm_summary = {
            "name": arm_name,
            **({"atom": arm_atom} if arm_atom else {}),
            **({"settings": arm.get("settings")} if isinstance(arm.get("settings"), dict) else {}),
        }

    resolved_doc = {
        "adw": adw_meta,
        "arm": arm_summary,
        "config_commit": config_commit,
        "engine_sha": engine_sha,
        "expertise": expertise_meta,
        "harness": harness_meta,
        "kind": kind,
        "loop_files": loop_files,
        "overrides": {
            role: {k: {"value": v, "level": "run", "from": "--set"} for k, v in table.items()}
            for role, table in sorted(step_overrides.items())
        },
        "prices": prices_meta,
        "prompts": prompts_meta,
        "schema_version": schema_version,
        "schemas": schemas_meta,
        "settings": settings_prov,
    }
    resolved_bytes = _canonical_json_bytes(resolved_doc)
    resolved_sha = blob_store.digest(resolved_bytes)
    blobs_out[resolved_sha] = ("resolved", resolved_bytes)

    return Resolution(
        kind=kind,
        engine_sha=engine_sha,
        config_commit=config_commit,
        config_dir=config_dir,
        schema_version=schema_version,
        cfg=pinned_cfg,
        layer=resolved_layer,
        repo=resolved_repo,
        task_kind=task_kind,
        adw=adw_meta,
        settings=resolved_settings,
        settings_provenance=settings_prov,
        settings_snapshots=snapshots,
        settings_sha=settings_sha,
        prompt_texts=prompt_texts,
        prompts=prompts_meta,
        schemas=schemas_meta,
        loop_files=loop_files,
        expertise=expertise_meta,
        prices=prices_meta,
        overrides=step_overrides,
        arm=arm_summary,
        harness=harness_meta,
        resolved_sha=resolved_sha,
        resolved_bytes=resolved_bytes,
        blob_payloads=blobs_out,
        ab_held=held_map,
    )


def ensure_automation_payer() -> bool:
    if not configrepo.is_repo() or not paths.CONFIG_FILE.is_file():
        return False
    owner = principals.owner()
    if not owner or owner == "hand":
        return False
    with configrepo.writer_lock() as fence:
        text, base_sha = configrepo.read("config.toml")
        if text is None or base_sha is None:
            return False
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return False
        if "automation" in data:
            return False
        sep = "\n" if text.endswith("\n") or not text else "\n\n"
        updated = f'{text}{sep}[automation]\npayer = {json.dumps(owner)}\n'
        configrepo.commit(
            {"config.toml": updated},
            {"config.toml": base_sha},
            actor=owner,
            source="engine",
            note="set default automation payer to owner",
            fence=fence,
        )
        return True
