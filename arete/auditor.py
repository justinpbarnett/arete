from __future__ import annotations

import json
import re
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from . import alerts, config, hook, paths, state, tracker, util
from .heartbeat import mark_loop

AUDIT_DIR = paths.STATE_DIR / "audit"
MAX_SKILL_LINES = 500
MAX_DESCRIPTION = 1024

LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)\s]*)?(?:\s[^)]*)?\)")
HOOK_PATHS = [
    (re.compile(r"(?:\$HOME|\$\{HOME\}|~)(/[^\s\"';|&):]+)"), lambda m: paths.HOME / m.group(1).lstrip("/")),
    (re.compile(r"(?<![\w$}~./-])(/(?:Users|opt|usr/local)/[^\s\"';|&):]+)"), lambda m: Path(m.group(1))),
]
PLUGIN_ROOT = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}(/[^\s\"';|&):]+)")


@dataclass
class Finding:
    check: str
    severity: Literal["fail", "warn", "info"]
    subject: str
    detail: str


def skill_files(cfg: config.Config) -> list[Path]:
    roots = [paths.HOME / ".claude" / "skills"]
    for r in cfg.repos:
        roots += [r.path / ".claude" / "skills", r.path / ".agents" / "skills"]
    seen, out = set(), []
    for root in roots:
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("SKILL.md")):
            real = f.resolve()
            if real not in seen and not {"node_modules", "synced"} & set(f.parts) and "retired" not in str(f):
                seen.add(real)
                out.append(f)
    return out


def frontmatter(text: str) -> dict | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fm = {}
    key = None
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            key = m.group(1)
            fm[key] = m.group(2).strip().strip("\"'")
        elif key and line.startswith((" ", "\t")):
            fm[key] = (fm[key] + " " + line.strip()).strip()
    return fm


def check_skills(cfg: config.Config) -> tuple[list[Finding], list[str]]:
    out, names = [], []
    for f in skill_files(cfg):
        rel = paths.short_path(f)
        text = f.read_text(errors="replace")
        fm = frontmatter(text)
        if fm is None:
            out.append(Finding("skills", "warn", rel, "no frontmatter; only a typed /command runs it"))
            continue
        name = fm.get("name") or f.parent.name
        names.append(name)
        if not fm.get("description"):
            out.append(Finding("skills", "warn", rel, "no description; only a typed /command runs it"))
        elif len(fm["description"]) > MAX_DESCRIPTION:
            out.append(Finding("skills", "warn", rel, f"description is {len(fm['description'])} chars (limit {MAX_DESCRIPTION})"))
        if fm.get("name") and fm["name"] != f.parent.name:
            out.append(Finding("skills", "warn", rel, f"name `{fm['name']}` differs from its folder `{f.parent.name}`"))
        lines = len(text.splitlines())
        if lines > MAX_SKILL_LINES:
            out.append(Finding("skills", "warn", rel, f"{lines} lines; move detail into reference files"))
        for target in _dead_links(text, f.parent):
            out.append(Finding("skills", "warn", rel, f"links to missing `{target}`"))
    return out, names


def _dead_links(text: str, base: Path) -> list[str]:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)", "", text)
    dead = []
    for m in LINK.finditer(text):
        t = m.group(1)
        if re.match(r"^[a-z]+:", t) or t.startswith(("/", "<", "$", "{")):
            continue
        if not (base / t).exists() and t not in dead:
            dead.append(t)
    return dead


def settings_files(cfg: config.Config) -> list[tuple[Path, Path | None]]:
    out = [(paths.CLAUDE_SETTINGS, None)]
    for r in cfg.repos:
        out += [(r.path / ".claude" / "settings.json", None), (r.path / ".claude" / "settings.local.json", None)]
    for root in enabled_plugins():
        out.append((root / "hooks" / "hooks.json", root))
    return [(f, p) for f, p in out if f.exists()]


def enabled_plugins() -> list[Path]:
    installed = util.read_json(paths.HOME / ".claude" / "plugins" / "installed_plugins.json", {})
    try:
        enabled = json.loads(paths.CLAUDE_SETTINGS.read_text()).get("enabledPlugins") or {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        enabled = {}
    out = []
    for name, installs in (installed.get("plugins") or {}).items():
        if enabled.get(name):
            out += [Path(i["installPath"]) for i in installs if i.get("installPath")]
    return out


def hook_commands(data: dict) -> list[tuple[str, str]]:
    out = []
    for event, groups in (data.get("hooks") or {}).items():
        for g in groups or []:
            for h in g.get("hooks") or []:
                if h.get("type", "command") == "command" and h.get("command"):
                    out.append((event, h["command"]))
    return out


def check_hooks(cfg: config.Config) -> list[Finding]:
    out = []
    for f, plugin_root in settings_files(cfg):
        rel = paths.short_path(f)
        try:
            commands = hook_commands(json.loads(f.read_text()))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            out.append(Finding("hooks", "fail", rel, f"does not parse: {e}"))
            continue
        except (AttributeError, TypeError) as e:
            out.append(Finding("hooks", "fail", rel, f"hooks are not in the shape Claude Code reads: {e}"))
            continue
        for event, cmd in commands:
            missing = []
            for rx, resolve in HOOK_PATHS:
                for m in rx.finditer(cmd):
                    p = resolve(m)
                    if not p.exists():
                        missing.append(paths.short_path(p))
            if plugin_root:
                for m in PLUGIN_ROOT.finditer(cmd):
                    p = plugin_root / m.group(1).lstrip("/")
                    if not p.exists():
                        missing.append(paths.short_path(p))
            for p in dict.fromkeys(missing):
                out.append(Finding("hooks", "fail", rel, f"{event} hook runs `{p}`, which does not exist"))
    return out


def check_repos(cfg: config.Config) -> list[Finding]:
    out = []
    for r in cfg.repos:
        if not r.path.is_dir():
            out.append(Finding("repos", "warn", r.name, f"{paths.short_path(r.path)} is not on disk"))
            continue
        f = r.layer_dir / "config.toml"
        if f.exists():
            try:
                data = tomllib.loads(f.read_text())
                extra = set(data) - config.LAYER_SECTIONS
                if extra:
                    out.append(Finding("agents-config", "warn", paths.short_path(f), f"unknown sections: {', '.join(sorted(extra))}"))
                if r.pipeline and not data.get("checks"):
                    out.append(Finding("agents-config", "warn", paths.short_path(f), "pipeline is on but no checks are set"))
                research_dir = config.load_layer(r.layer_dir).research_dir
                if research_dir and not (r.path / research_dir).is_dir():
                    out.append(Finding("agents-config", "warn", paths.short_path(f),
                                       f"research dir {research_dir} is not a folder in {paths.short_path(r.path)}"))
            except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as e:
                out.append(Finding("agents-config", "fail", paths.short_path(f), f"does not parse: {e}"))
            except (ValueError, TypeError, AttributeError) as e:
                out.append(Finding("agents-config", "fail", paths.short_path(f),
                                   f"is not in the shape arete reads: {type(e).__name__}: {e}"))
        elif r.pipeline:
            out.append(Finding("agents-config", "fail", r.name, f"pipeline is on but {paths.short_path(f)} is missing"))
        for doc in ["CLAUDE.md", "AGENTS.md"]:
            d = r.path / doc
            if d.exists():
                for t in _dead_links(d.read_text(errors="replace"), r.path):
                    out.append(Finding("links", "warn", paths.short_path(d), f"links to missing `{t}`"))
    return out


def check_events(cfg: config.Config, skill_names: list[str]) -> list[Finding]:
    out = []
    week = [e for e in hook.read_events(24 * 7) if not e.get("agent")]
    fails = Counter(e.get("detail") for e in week if e.get("kind") == "skill" and e.get("cls") == "failure")
    for name, n in fails.most_common():
        out.append(Finding("failures", "warn", f"skill {name}", f"failed {n} times this week"))
    rep = Counter()
    for e in week:
        if e.get("cls") in ("interrupted", "denied", "invoked", "spawned"):
            continue
        repo = cfg.repo_for_path(e.get("cwd", "")) if e.get("cwd") else None
        rep[(repo.name if repo else "other", e.get("tool"), e.get("cls"), e.get("detail", "")[:100])] += 1
    for (repo, tool, cls, detail), n in rep.most_common(10):
        if n >= 3:
            out.append(Finding("failures", "warn", f"{repo} {tool}", f"{cls} {n} times this week: {detail}"))
    month = hook.read_events(24 * 30)
    if month:
        span = (util.now() - util.parse_iso(month[0]["ts"])).days
        if span >= 21:
            used = {(e.get("detail") or "").lstrip("/").rsplit(":", 1)[-1] for e in month if e.get("kind") == "skill"}
            idle = sorted(set(skill_names) - used)
            if idle:
                out.append(Finding("skills", "info", "unused", f"not invoked in {span} days: {', '.join(idle)}"))
    return out


def run_stats() -> list[str]:
    week_ago = util.iso(util.now() - timedelta(days=7))
    rs = [r for r in state.runs(limit=500) if (r.get("started") or "") >= week_ago]
    lines = []
    by = Counter((r["kind"], r["status"]) for r in rs)
    for kind in sorted({k for k, _ in by}):
        parts = ", ".join(f"{n} {s}" for (k, s), n in sorted(by.items()) if k == kind)
        lines.append(f"- {kind}: {parts}")
    props = Counter(p["status"] for p in state.proposals(limit=500))
    if props:
        lines.append("- miner proposals all time: " + ", ".join(f"{n} {s}" for s, n in props.most_common()))
    for r in rs:
        if r["status"] == "failed" and r["kind"] == "pipeline":
            where = tracker.run_ticket_name(r)
            lines.append(f"- failed {where}: {util.first_line(r.get('summary') or '', 160)}")
    return lines


def audit(cfg: config.Config) -> tuple[list[Finding], list[str]]:
    skills, names = check_skills(cfg)
    findings = skills + check_hooks(cfg) + check_repos(cfg) + check_events(cfg, names)
    return findings, run_stats()


def report(findings: list[Finding], stats: list[str]) -> str:
    order = {"fail": 0, "warn": 1, "info": 2}
    lines = [f"# Harness audit {util.now():%Y-%m-%d}", ""]
    if not findings:
        lines.append("Nothing to fix.")
    for f in sorted(findings, key=lambda f: (order[f.severity], f.check, f.subject)):
        lines.append(f"- {f.severity.upper()} [{f.check}] {f.subject}: {f.detail}")
    if stats:
        lines += ["", "## Arete runs, last 7 days", ""] + stats
    return "\n".join(lines) + "\n"


def latest_findings(max_chars: int = 4000) -> str:
    f = AUDIT_DIR / "latest.md"
    if not f.exists():
        return "(no audit yet)"
    lines = [ln for ln in f.read_text().splitlines() if ln.startswith(("- FAIL", "- WARN"))]
    return "\n".join(lines)[:max_chars] or "(nothing to fix)"


def run(dry_run: bool = False) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    with util.lock("audit") as got:
        if not got:
            print("audit already running")
            return 0
        try:
            findings, stats = audit(cfg)
        except Exception as e:
            print(f"audit crashed: {type(e).__name__}: {e}")
            if not dry_run:
                mark_loop("audit", False, f"{type(e).__name__}: {e}")
            return 1
        text = report(findings, stats)
        fails = [f for f in findings if f.severity == "fail"]
        print(f"{util.iso()} audit: {len(fails)} fail, {sum(f.severity == 'warn' for f in findings)} warn")
        if dry_run:
            print(text)
            return 0
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        (AUDIT_DIR / f"{util.now():%Y-%m-%d}.md").write_text(text)
        (AUDIT_DIR / "latest.md").write_text(text)
        for old in sorted(AUDIT_DIR.glob("20*.md"))[:-30]:
            old.unlink()
        fails_per_check = Counter(f.check for f in fails)
        for check, n in fails_per_check.items():
            first = next(f for f in fails if f.check == check)
            msg = alerts.raise_alert(cfg.alert_repo, f"audit:{check}", f"audit: {check} broken",
                                     f"{n} broken; first: {first.subject}", dry_run)
            if msg:
                print(msg)
        for a in state.alerts():
            if a["key"].startswith("audit:") and a["key"][6:] not in fails_per_check:
                msg = alerts.clear_alert(cfg.alert_repo, a["key"], dry_run)
                if msg:
                    print(msg)
        mark_loop("audit", True, f"{len(fails)} fail")
        return 0
