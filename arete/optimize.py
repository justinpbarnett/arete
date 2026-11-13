from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import stat
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from . import config, experiment, paths, replay, settings, steps, util
from .experiment import NAME, Refused, Totals, append, events

BASELINE = "baseline"
TOP_KEYS = ("name", "repo", "atom", "repeats", "cache_policy", "rounds", "tasks", "accept", "floor", "limits")
ROUND_KEYS = ("vary", "candidates")
ACCEPT_KEYS = ("min_pairs", "min_difference", "confidence")
FLOOR_KEYS = ("accept_rate", "max_drop", "min_saving")
LIMIT_KEYS = (*experiment.LIMIT_KEYS, "rounds")
VARIES = {"model": ("tier", "runner", "provider", "model", "effort"), "effort": ("effort",),
          "prompt": ("prompts", "instructions"), "context": ("context",), "tools": ("tools", "mcp")}
LATE = ("prompt", "context", "tools")
STEP_VALUES = ("tier", "runner", "provider", "model", "effort", "tools", "mcp", "instructions")
NOT_WRITTEN = ("prompts", "context")
NO_PROMOTION = "no promotion: "
SAVED = "config-before-apply.toml"
TOKENS = {"plan": (2000, 400), "build": (6000, 1600)}
SAMPLE_PRICES = {"scratch-a": {"input": 1.0, "output": 4.0}, "scratch-b": {"input": 3.0, "output": 12.0}}


class Halt(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


@dataclass
class Search:
    fields: dict
    rounds: list[dict]
    previews: list[dict]
    snapshot: dict
    design: dict
    digest: str
    repo: str
    environment: str
    task_evidence: dict


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def table_name(raw: dict, atom: str) -> str:
    held = raw.get("steps", {})
    alias = next((a for a, role in config.STEP_ALIASES.items() if role == atom and a in held), "")
    return f"steps.{alias or atom}"


def last(found: list[dict], event: str) -> dict | None:
    return next((e for e in reversed(found) if e.get("event") == event), None)


def read_rounds(data: dict) -> list[dict]:
    found = data.get("rounds")
    if not isinstance(found, list) or not found:
        raise Refused("rounds is required: add at least one [[rounds]] table")
    names, rounds = set(), []
    for n, entry in enumerate(found, 1):
        where = f"rounds[{n}]"
        if not isinstance(entry, dict):
            raise Refused(f"{where} must be a table")
        experiment.unknown(entry, ROUND_KEYS, where)
        vary = experiment.text(entry, "vary", f"{where}.")
        if vary not in VARIES:
            raise Refused(f"{where}.vary must be one of {', '.join(VARIES)}, not {vary!r}")
        candidates = {}
        for name, keys in experiment.section(entry, "candidates", f"{where}.").items():
            if not NAME.fullmatch(name) or name == BASELINE or name in names:
                raise Refused(f"{where}.candidates.{name}: a name takes letters, digits, _ and -, is not "
                              f"{BASELINE!r} and is used once in the plan")
            if not isinstance(keys, dict) or not keys:
                raise Refused(f"{where}.candidates.{name} must be a table with entries")
            experiment.unknown(keys, VARIES[vary], f"{where}.candidates.{name} (vary {vary})")
            names.add(name)
            candidates[name] = deepcopy(keys)
        rounds.append({"vary": vary, "candidates": candidates})
    varies = [r["vary"] for r in rounds]
    if varies[0] not in ("model", "effort"):
        raise Refused("the first round varies model or effort")
    for at, vary in enumerate(varies):
        if vary in ("model", "effort") and any(earlier in LATE for earlier in varies[:at]):
            raise Refused(f"rounds[{at + 1}]: model and effort come before prompt, context and tools")
    return rounds


def read_floor(table: dict) -> dict:
    experiment.unknown(table, FLOOR_KEYS, "floor")
    found = {key: float(experiment.number(table, key, "floor.")) for key in ("accept_rate", "max_drop")}
    for key, value in found.items():
        if not 0 <= value <= 1:
            raise Refused(f"floor.{key} must be from 0 to 1, not {value}")
    saving = float(experiment.number(table, "min_saving", "floor.", 0.1))
    if not 0 < saving < 1:
        raise Refused(f"floor.min_saving must be above 0 and under 1, not {saving}")
    return {**found, "min_saving": saving}


def read_limits(table: dict) -> dict:
    experiment.unknown(table, LIMIT_KEYS, "limits")
    found = experiment.read_limits({k: v for k, v in table.items() if k != "rounds"})
    rounds = experiment.number(table, "rounds", "limits.", whole=True)
    if rounds < 1:
        raise Refused(f"limits.rounds must be 1 or more, not {rounds}")
    return {**found, "rounds": rounds}


def read_tasks(table: dict) -> dict:
    tasks = {}
    for name, entry in table.items():
        if not isinstance(entry, dict):
            raise Refused(f"tasks.{name} must be a table")
        experiment.unknown(entry, experiment.TASK_KEYS, f"tasks.{name}")
        tasks[name] = dict(entry)
    if missing := [s for s in experiment.SPLITS if not any(t.get("split") == s for t in tasks.values())]:
        raise Refused(f"tasks: no task has the split {', '.join(missing)}; each of "
                      f"{', '.join(experiment.SPLITS)} needs one")
    return tasks


def manifest(fields: dict, tag: str, split: str, arms: dict, champion: str) -> dict:
    return {"name": f"{fields['name']}-{tag}", "repo": fields["repo"], "atom": fields["atom"],
            "repeats": fields["repeats"], "cache_policy": fields["cache_policy"], "arms": arms,
            "tasks": {n: t for n, t in fields["tasks"].items() if t.get("split") == split},
            "accept": {**fields["accept"], "baseline": champion, "split": split},
            "limits": {k: fields["limits"][k] for k in experiment.LIMIT_KEYS}}


def snapshot(exp: experiment.Experiment) -> dict:
    data = paths.CONFIG_FILE.read_bytes() if paths.CONFIG_FILE.exists() else b""
    table = table_name(tomllib.loads(data.decode()), exp.atom)
    resolved = exp.settings[BASELINE]
    return {"config": paths.short_path(paths.CONFIG_FILE), "sha256": sha(data), "table": table,
            "raw": tomllib.loads(data.decode()).get("steps", {}).get(table.removeprefix("steps."), {}),
            "resolved": {k: list(v) if isinstance(v, tuple) else v
                         for k in ("runner", "model", "provider", "tier", "effort", "tools", "mcp", "instructions")
                         for v in [getattr(resolved, k)]},
            "settings": exp.design["arms"][BASELINE]["settings"],
            "inheritors": exp.design["arms"][BASELINE].get("inheritors", {})}


def load(path: Path) -> Search:
    data = tomllib.loads(path.read_text())
    experiment.unknown(data, TOP_KEYS, "the plan")
    name = experiment.text(data, "name")
    if not NAME.fullmatch(name):
        raise Refused("name takes only letters, digits, _ and -")
    accept_table = data.get("accept", {})
    if not isinstance(accept_table, dict):
        raise Refused("accept must be a table")
    experiment.unknown(accept_table, ACCEPT_KEYS, "accept")
    tasks = read_tasks(experiment.section(data, "tasks"))
    fields = {"name": name, "repo": experiment.text(data, "repo"), "atom": experiment.text(data, "atom"),
              "repeats": experiment.number(data, "repeats", whole=True),
              "cache_policy": experiment.text(data, "cache_policy", "", experiment.DEFAULT_CACHE_POLICY),
              "tasks": tasks, "floor": read_floor(experiment.section(data, "floor")),
              "limits": read_limits(experiment.section(data, "limits"))}
    rounds = read_rounds(data)
    fields["accept"] = experiment.read_accept({**accept_table, "baseline": BASELINE, "split": "validation"},
                                              {BASELINE: {}}, {n: t["split"] for n, t in tasks.items()})
    built = [experiment.build(manifest(fields, "check", "train", {BASELINE: {}, **r["candidates"]}, BASELINE),
                              follow=True) for r in rounds]
    others = [experiment.build(manifest(fields, "check", split, {BASELINE: {}, **rounds[0]["candidates"]}, BASELINE),
                               follow=True) for split in ("validation", "holdout")]
    base = snapshot(built[0])
    evidence_of = {n: t for exp in (built[0], *others) for n, t in exp.design["tasks"].items()}
    env = environment(built[0])
    design = {**{k: v for k, v in fields.items() if k != "limits"}, "rounds": rounds, "baseline": base["settings"],
              "inheritors": base["inheritors"], "environment": env, "task_evidence": evidence_of}
    previews = [{n: exp.settings[n] for n in r["candidates"]} for r, exp in zip(rounds, built, strict=True)]
    return Search(fields, rounds, previews, base, design, experiment.sha_json(design)[:12], built[0].repo.name,
                  env, evidence_of)


def family(atom: str) -> set[str]:
    return {atom, *[r for r, b in config.INHERITS.items() if b == atom]}


def environment(exp: experiment.Experiment) -> str:
    live = {r: settled_now(exp.cfg, exp.layer, r) for r in family(exp.atom)}
    return experiment.sha_json({"roles": roles_of(exp.cfg, exp.layer, family(exp.atom)), "live": live,
                                "grader": exp.design["grader"], "fingerprints": exp.design["fingerprints"]})


def check_digest(s: Search, trail: Path, found: list[dict]) -> None:
    began = next((e for e in found if e.get("event") == "search"), None)
    if began and began.get("digest") != s.digest:
        raise Refused(f"the plan or the baseline settings changed since {paths.short_path(trail)} was started; "
                      "pass --out for a new folder")


def folder_of(s: Search, out: str) -> Path:
    return paths.expand(out) if out else paths.STATE_DIR / "experiments" / s.fields["name"]


def most_trials(s: Search) -> int:
    tasks = {sp: sum(t["split"] == sp for t in s.fields["tasks"].values()) for sp in experiment.SPLITS}
    repeats, total = s.fields["repeats"], 2 * tasks["holdout"] * s.fields["repeats"]
    for r in s.rounds[:s.fields["limits"]["rounds"]]:
        arms = 1 + len(r["candidates"])
        total += arms * (tasks["train"] + tasks["validation"]) * repeats
    return total


def describe(resolved) -> str:
    get = resolved.get if isinstance(resolved, dict) else lambda k: getattr(resolved, k)
    return f"runner {get('runner')}, model {get('model')}, effort {get('effort') or 'default'}"


def dry_run(s: Search, folder: Path) -> int:
    f = s.fields
    out = [f"search {f['name']} (plan {s.digest}): atom {f['atom']} on {s.repo}", f"trail folder: {paths.short_path(folder)}",
           f"baseline: {describe(s.snapshot['resolved'])}, config {s.snapshot['config']}, settings {s.snapshot['settings'][:12]}"]
    for n, (r, preview) in enumerate(zip(s.rounds, s.previews, strict=True), 1):
        out.append(f"round {n} (vary {r['vary']})" + ("" if n <= f["limits"]["rounds"] else " (beyond limits.rounds, not run)"))
        for name, keys in r["candidates"].items():
            changed = ", ".join(f"{k}={json.dumps(v)}" for k, v in keys.items())
            out.append(f"  candidate {name}: {describe(preview[name])}; changes {changed}")
    out += ["tasks: " + ", ".join(f"{sp} {sum(t['split'] == sp for t in f['tasks'].values())}" for sp in experiment.SPLITS)
            + f", {f['repeats']} repeats",
            (f"accept: at least {max(experiment.MIN_PAIRS, f['accept']['min_pairs'])} pairs, a difference above "
             f"{f['accept']['min_difference']:g} at {f['accept']['confidence']:g} confidence"),
            (f"floor: accept_rate {f['floor']['accept_rate']:g}, max_drop {f['floor']['max_drop']:g}, "
             f"min_saving {f['floor']['min_saving']:g}"),
            "limits: " + ", ".join(f"{k} {f['limits'][k]:g}" for k in LIMIT_KEYS),
            f"at most {util.count(most_trials(s), 'trial')}, none started by this dry run"]
    print("\n".join(out))
    return 0


def economics(a: dict) -> dict:
    k = a["accepted"]
    return {"charged": a["charged_per_accepted"], "reference": a["reference_per_accepted"],
            "repairs": round(a["repairs"] / k, 4) if k else None,
            "seconds": round(a["wall_seconds"] / k, 2) if k else None}


def cheaper(candidate: dict, champion: dict, saving: float) -> bool:
    a, b = economics(candidate), economics(champion)
    if None in (*a.values(), *b.values()):
        return False
    return all(a[k] <= b[k] for k in a) and a["charged"] + a["reference"] <= (b["charged"] + b["reference"]) * (1 - saving)


def decide(candidate: dict, champion: dict, pair: dict, verdict: dict, floor: dict) -> dict:
    def no(why: str) -> dict:
        return {"promote": False, "basis": "", "reason": NO_PROMOTION + why}

    if verdict["state"] == "tiny":
        return no("insufficient evidence")
    if None in (candidate["charged_usd"], candidate["reference_usd"], champion["charged_usd"],
                champion["reference_usd"]):
        return no("missing costs")
    if verdict["state"] == "holds":
        return no("quality regression")
    if (candidate["rate"] or 0) < floor["accept_rate"]:
        return no("under the quality floor")
    if verdict["state"] == "wins":
        return {"promote": True, "basis": "quality", "reason": "promote on quality"}
    if pair["interval"][0] <= -floor["max_drop"] or not cheaper(candidate, champion, floor["min_saving"]):
        return no("noisy tie")
    return {"promote": True, "basis": "economics", "reason": "promote on economics"}


def evidence(arm: dict, other: dict, pair: dict, verdict: dict) -> dict:
    return {"accepted": arm["accepted"], "done": arm["done"], "other_accepted": other["accepted"],
            "other_done": other["done"], "pairs": pair["pairs"], "difference": pair["difference"],
            "interval": pair["interval"], "verdict": verdict["text"], "economics": economics(arm),
            "other_economics": economics(other)}


def carried(folder: Path, own: Path) -> Totals:
    used = Totals()
    for file in sorted(folder.glob("*.jsonl")):
        if file not in (own, folder / "trail.jsonl"):
            used = used.plus(experiment.totals_of(events(file)))
    return used


def phase(s: Search, folder: Path, tag: str, split: str, arms: dict, champion: str) -> Path:
    file = folder / f"{tag}.jsonl"
    exp = experiment.build(manifest(s.fields, tag, split, arms, champion), carried(folder, file), follow=True)
    if environment(exp) != s.environment or any(s.task_evidence.get(n) != t for n, t in exp.design["tasks"].items()):
        raise Refused("the fixed steps, grader, harness, prompts, tasks or live baseline changed since the search "
                      "began; pass --out for a new folder")
    if code := experiment.drive(exp, file):
        why = next((e["why"] for e in reversed(events(file)) if e.get("event") == "stop"), "stopped")
        append(folder / "trail.jsonl", {"event": "stop", "phase": tag, "why": why, "code": code, "at": util.iso()})
        raise Halt(code)
    return file


def settled(file: Path, arm: str) -> dict:
    design = next(e for e in events(file) if e.get("event") == "manifest")["design"]
    return {"settings": design["arms"][arm]["settings"], "inheritors": design["arms"][arm]["inheritors"]}


def train_view(file: Path) -> dict[str, dict]:
    found = events(file)
    view = {}
    for p in experiment.summary(file)["pairs"]["train"]:
        failing = [f for e in found if e.get("event") == "trial" and e.get("arm") == p["candidate"]
                   and e.get("status") == "done" and not e.get("accepted") for f in e.get("failures", [])]
        view[p["candidate"]] = {"pairs": p["pairs"], "fixed": p["candidate_only"], "broke": p["baseline_only"],
                                "proposed": bool(p["pairs"]) and p["candidate_only"] >= p["baseline_only"],
                                "failures": list(dict.fromkeys(failing))[:5]}
    return view


def rank(name: str, order: int, found: dict) -> tuple:
    arm = found["arms"][name]
    cost = (arm["charged_per_accepted"] or 0) + (arm["reference_per_accepted"] or 0) if arm["accepted"] else float("inf")
    return -(arm["rate"] or 0), cost, order


def play(s: Search, folder: Path, n: int, rnd: dict, champion: dict) -> dict:
    held, name = champion["overrides"], champion["name"]
    candidates = {c: {**held, **keys} for c, keys in rnd["candidates"].items()}
    train = train_view(phase(s, folder, f"r{n}-train", "train", {name: held, **candidates}, name))
    proposals = [c for c in rnd["candidates"] if train[c]["proposed"]]
    seen = {c: {"keys": keys, "train": train[c], "validation": None, "decision": None}
            for c, keys in rnd["candidates"].items()}
    won, after = "", champion
    if proposals:
        file = phase(s, folder, f"r{n}-validation", "validation",
                     {name: held, **{c: candidates[c] for c in proposals}}, name)
        found = experiment.summary(file)
        pairs = {p["candidate"]: p for p in found["pairs"]["validation"]}
        for c in proposals:
            verdict = found["verdict"]["candidates"][c]
            seen[c]["decision"] = decide(found["arms"][c], found["arms"][name], pairs[c], verdict, s.fields["floor"])
            seen[c]["validation"] = evidence(found["arms"][c], found["arms"][name], pairs[c], verdict)
        winners = [c for c in proposals if seen[c]["decision"]["promote"]]
        if winners:
            won = min(winners, key=lambda c: rank(c, list(rnd["candidates"]).index(c), found))
            after = {"name": won, "overrides": candidates[won], **settled(file, won), "round": n}
    return {"event": "round", "n": n, "vary": rnd["vary"], "before": name, "promoted": won, "champion": after,
            "candidates": seen, "at": util.iso()}


def score_holdout(s: Search, folder: Path, champion: dict) -> dict:
    name, floor = champion["name"], s.fields["floor"]
    file = phase(s, folder, "holdout", "holdout", {BASELINE: {}, name: champion["overrides"]}, BASELINE)
    found = experiment.summary(file)
    arm, other, pair = found["arms"][name], found["arms"][BASELINE], found["pairs"]["holdout"][0]
    costs = (arm["charged_usd"], arm["reference_usd"], other["charged_usd"], other["reference_usd"])
    if pair["pairs"] < max(experiment.MIN_PAIRS, s.fields["accept"]["min_pairs"]):
        why = "insufficient evidence"
    elif None in costs:
        why = "missing costs"
    elif (arm["rate"] or 0) < floor["accept_rate"] or pair["difference"] < -floor["max_drop"]:
        why = "holdout regression"
    else:
        why = ""
    return {"event": "holdout", "champion": name, "confirmed": not why, "reason": NO_PROMOTION + why if why else "confirmed",
            **evidence(arm, other, pair, found["verdict"]["candidates"][name]), "at": util.iso()}


def climb(s: Search, folder: Path) -> int:
    trail = folder / "trail.jsonl"
    found = events(trail)
    if not any(e.get("event") == "search" for e in found):
        append(trail, {"event": "search", "digest": s.digest, "design": s.design, "baseline": s.snapshot,
                       "at": util.iso()})
    if last(found, "end"):
        print(render(trail, folder))
        return 0
    scored = last(found, "holdout")
    champion = {"name": BASELINE, "overrides": {}, "settings": s.snapshot["settings"],
                "inheritors": s.snapshot["inheritors"], "round": 0}
    try:
        for n, rnd in enumerate(s.rounds[:s.fields["limits"]["rounds"]], 1):
            if earlier := next((e for e in found if e.get("event") == "round" and e.get("n") == n), None):
                champion = earlier["champion"]
                continue
            if scored:
                break
            result = play(s, folder, n, rnd, champion)
            append(trail, result)
            champion = result["champion"]
        if not last(events(trail), "end"):
            finish(s, folder, champion)
    except Halt as halted:
        return halted.code
    print(render(trail, folder))
    return 0


def finish(s: Search, folder: Path, champion: dict) -> None:
    trail = folder / "trail.jsonl"
    end = {"event": "end", "champion": champion["name"], "overrides": champion["overrides"],
           "settings": champion["settings"], "inheritors": champion["inheritors"], "round": champion["round"],
           "at": util.iso()}
    if champion["name"] == BASELINE:
        append(trail, {**end, "promotable": False, "why": "the baseline holds"})
        return
    held = last(events(trail), "holdout")
    if held and held["champion"] != champion["name"]:
        raise Refused(f"the holdout in {paths.short_path(trail)} was scored for {held['champion']}, not "
                      f"{champion['name']}; pass --out for a new folder")
    if held is None:
        held = score_holdout(s, folder, champion)
        append(trail, held)
    append(trail, {**end, "promotable": held["confirmed"], "why": "" if held["confirmed"]
                   else held["reason"].removeprefix(NO_PROMOTION)})


def run(plan: Path, out: str = "", dry: bool = False) -> int:
    s = load(plan)
    folder = folder_of(s, out)
    check_digest(s, folder / "trail.jsonl", events(folder / "trail.jsonl"))
    if dry:
        return dry_run(s, folder)
    return experiment.exclusive(s.repo, lambda: climb(s, folder))


def figure(value) -> str:
    return "unknown" if value is None else f"{value:g}"


def shown(e: dict) -> str:
    return (f"charged {experiment.money(e['charged'])}, reference {experiment.money(e['reference'])}, "
            f"repairs {figure(e['repairs'])}, seconds {figure(e['seconds'])}")


def render(trail: Path, folder: Path) -> str:
    found = events(trail)
    begun, end, held = last(found, "search"), last(found, "end"), last(found, "holdout")
    design, base = begun["design"], begun["baseline"]
    out = [f"search {design['name']} (plan {begun['digest']}): atom {design['atom']} on {design['repo']}",
           f"baseline: {describe(base['resolved'])}"]
    for r in (e for e in found if e.get("event") == "round"):
        out.append(f"round {r['n']} ({r['vary']}), champion {r['before']}:")
        for name, c in r["candidates"].items():
            out.append(f"  {name}: train fixed {c['train']['fixed']}, broke {c['train']['broke']}: "
                       f"{'proposed' if c['train']['proposed'] else 'dropped'}")
            if v := c["validation"]:
                out += [(f"    validation accepted {v['accepted']} of {v['done']} against {v['other_accepted']} of "
                         f"{v['other_done']}: {c['decision']['reason']}"),
                        f"    per accepted task: {shown(v['economics'])}",
                        f"    champion per accepted task: {shown(v['other_economics'])}"]
        out.append(f"  promoted: {r['promoted'] or 'none'}")
    if held:
        out.append(f"holdout (scored once): {held['champion']} accepted {held['accepted']} of {held['done']} against "
                   f"{BASELINE} {held['other_accepted']} of {held['other_done']}: {held['reason']}")
    if end:
        out.append(f"result: promote {end['champion']} over {BASELINE}" if end["promotable"]
                   else f"result: {NO_PROMOTION}{end['why']}")
        out.append(f"next: arete experiment promote {paths.short_path(folder)}")
    return "\n".join(out)


def trail_of(folder: Path) -> tuple[Path, list[dict]]:
    trail = folder / "trail.jsonl"
    found = events(trail)
    if not last(found, "search"):
        raise Refused(f"{paths.short_path(trail)} holds no search")
    return trail, found


def live(search: dict) -> tuple[config.Config, config.RepoLayer]:
    try:
        cfg = config.load()
        return cfg, config.load_layer(cfg.repo(search["design"]["repo"]).layer_dir)
    except (ValueError, OSError) as e:
        raise Refused(f"the live config does not load: {e}") from e


def settled_now(cfg: config.Config, layer: config.RepoLayer, role: str) -> str:
    try:
        return experiment.effective(cfg, steps.resolve(cfg, layer, role, ""))
    except ValueError as e:
        return str(e)


def collapse(lines: list[str], table: str, key: str) -> None:
    start, end = settings.table_span(lines, table)
    if start is None:
        return
    for i in range(start, end):
        if not (m := settings.key_line(key).match(lines[i])):
            continue
        for j in range(i, end):
            try:
                tomllib.loads("".join(lines[i:j + 1]))
            except tomllib.TOMLDecodeError:
                continue
            lines[i:j + 1] = [f"{m.group(1)}[]{m.group(3) or chr(10)}"]
            return
        return


def write_key(lines: list[str], table: str, key: str, value) -> None:
    collapse(lines, table, key)
    if value is None:
        settings.drop_key(lines, table, key)
    else:
        settings.set_key(lines, table, key, json.dumps(value))


def edited(text: str, table: str, keys: dict) -> str:
    lines = text.splitlines(keepends=True)
    for key, value in keys.items():
        write_key(lines, table, key, value)
    return "".join(lines)


def undeployable(end: dict) -> str:
    if extras := [k for k in end["overrides"] if k in NOT_WRITTEN]:
        return (f"the winner also needs {', '.join(extras)}, which apply never writes, so the step settings alone "
                "were never confirmed by the search")
    return ""


def held_table(data: dict, table: str) -> dict:
    return data.get("steps", {}).get(table.removeprefix("steps."), {})


def put(data: bytes) -> None:
    draft = paths.CONFIG_FILE.with_suffix(".toml.tmp")
    mode = stat.S_IMODE(paths.CONFIG_FILE.stat().st_mode)
    draft.touch(mode=mode)
    draft.chmod(mode)
    draft.write_bytes(data)
    draft.replace(paths.CONFIG_FILE)


def restored(before: bytes, saved: Path) -> str:
    try:
        put(before)
    except OSError as e:
        return f"config.toml could not be restored ({e}); the original bytes are in {paths.short_path(saved)}"
    return "config.toml was restored"


def keep(file: Path, data: bytes) -> None:
    file.touch(mode=0o600)
    file.chmod(0o600)
    file.write_bytes(data)


def standing(found: list[dict]) -> dict | None:
    current = None
    for e in found:
        if e.get("event") == "apply" and e.get("status") == "applied":
            current = e
        elif e.get("event") == "rollback":
            current = None
    return current


def rollback_command(folder: Path) -> str:
    return f"arete experiment promote {paths.short_path(folder)} --rollback"


def promote(folder: Path, apply: bool = False, rollback: bool = False) -> int:
    trail, found = trail_of(folder)
    if rollback:
        return roll_back(folder, trail, found)
    end = last(found, "end")
    if end is None:
        if apply:
            raise Refused("the search has not finished")
        print(f"the search has not finished: {paths.short_path(trail)} has no end line; run arete experiment "
              "optimize again to continue it")
        return 0
    if apply:
        return apply_winner(folder, trail, found, end)
    return preview(folder, found, end)


def preview(folder: Path, found: list[dict], end: dict) -> int:
    search = last(found, "search")
    if not end["promotable"]:
        print(f"{NO_PROMOTION}{end['why']}; there is nothing to apply")
        return 0
    if why := undeployable(end):
        print(f"{end['champion']} won, but apply is refused: {why}\ndry run: nothing was written")
        return 0
    held = last(found, "holdout")
    won = next(e for e in found if e.get("event") == "round" and e.get("n") == end["round"])
    basis = won["candidates"][end["champion"]]["decision"]["basis"]
    atom = search["design"]["atom"]
    text = paths.CONFIG_FILE.read_text()
    table = table_name(tomllib.loads(text), atom)
    keys = {k: v for k, v in end["overrides"].items() if k in STEP_VALUES}
    after = edited(text, table, keys)
    cfg, layer = live(search)
    measured = search["baseline"]["inheritors"]
    follows = [f"{r} ({'measured by the trials' if r in measured else 'not measured'})"
               for r, b in config.INHERITS.items() if b == atom
               and not steps.resolve(cfg, layer, r, "").explicit_route]
    out = [(f"promote {end['champion']} over {BASELINE}: chosen in round {end['round']} on {basis}, holdout "
            f"{held['reason']} ({held['accepted']} of {held['done']} against {held['other_accepted']} of "
            f"{held['other_done']})"),
           f"config: {paths.short_path(paths.CONFIG_FILE)}, table [{table}]"]
    out += [line for line in difflib.unified_diff(text.splitlines(), after.splitlines(),
                                                  paths.short_path(paths.CONFIG_FILE), "after promote", lineterm="")]
    if after == text:
        out.append("no change: config.toml already holds these values")
    if follows:
        out.append(f"also follows by inheritance: {', '.join(follows)}")
    if applied := standing(found):
        out += [f"applied {applied['at']}", f"roll back with: {rollback_command(folder)}"]
    out.append("dry run: nothing was written")
    print("\n".join(out))
    return 0


def roles_of(cfg: config.Config, layer: config.RepoLayer, skip: set[str]) -> dict:
    return {role: settled_now(cfg, layer, role) for role in config.STEP_ROLES if role not in skip}


def refusal_for(cfg: config.Config, layer: config.RepoLayer, atom: str, wanted: dict):
    skip = family(atom)
    held = roles_of(cfg, layer, skip)

    def refusal(new: config.Config) -> str:
        if settled_now(new, layer, atom) != wanted["settings"] or \
                {r: settled_now(new, layer, r) for r in wanted["inheritors"]} != wanted["inheritors"]:
            return (f"the new config does not give {atom} and its measured inheritors the settings the search "
                    f"chose, so a layer [steps.{atom}] entry may set the same key")
        return "" if roles_of(new, layer, skip) == held else "the new config changes steps outside the atom"

    return refusal


def apply_winner(folder: Path, trail: Path, found: list[dict], end: dict) -> int:
    search = last(found, "search")
    atom = search["design"]["atom"]
    if not end["promotable"]:
        raise Refused(f"{NO_PROMOTION}{end['why']}; there is nothing to apply")
    if why := undeployable(end):
        raise Refused(f"apply refused: {why}")
    keys = {k: v for k, v in end["overrides"].items() if k in STEP_VALUES}
    with util.lock("config", wait=True):
        cfg, layer = live(search)
        now = settled_now(cfg, layer, atom)
        inheritors = {r: settled_now(cfg, layer, r) for r in search["baseline"]["inheritors"]}
        if now == end["settings"] and inheritors == end["inheritors"]:
            print(f"already applied: {atom} in {paths.short_path(paths.CONFIG_FILE)} has the settings of "
                  f"{end['champion']}; nothing was written")
            return 0
        if now != search["baseline"]["settings"] or inheritors != search["baseline"]["inheritors"]:
            raise Refused(f"settings for {atom} changed since the search began; run a new search")
        before = paths.CONFIG_FILE.read_bytes()
        old = tomllib.loads(before.decode())
        table = table_name(old, atom)
        text = edited(before.decode(), table, keys)
        outcome, replaced = "config.toml is unchanged", False
        try:
            keep(folder / SAVED, before)
            expected = deepcopy(old)
            expected.setdefault("steps", {}).setdefault(table.removeprefix("steps."), {}).update(keys)
            if tomllib.loads(text) != expected:
                raise Refused("the edit would change more than the chosen keys")
            settings.write_checked(text, refusal_for(cfg, layer, atom, end))
            replaced = True
            if paths.CONFIG_FILE.read_bytes() != text.encode():
                raise Refused("it read back differently from what was written")
            changed = {k: {"before": held_table(old, table).get(k), "after": v} for k, v in keys.items()}
            append(trail, {"event": "apply", "status": "applied", "table": table, "keys": changed,
                           "before_sha": sha(before), "after_sha": sha(text.encode()), "at": util.iso()})
        except (OSError, settings.Refused, Refused) as e:
            if replaced:
                outcome = restored(before, folder / SAVED)
            with contextlib.suppress(OSError):
                append(trail, {"event": "apply", "status": "failed", "why": str(e), "at": util.iso()})
            raise Refused(f"apply failed: {e}; {outcome}") from e
    print(f"applied: [{table}] " + ", ".join(f"{k} {json.dumps(c['before'])} -> {json.dumps(c['after'])}"
                                             for k, c in changed.items()))
    print(f"roll back with: {rollback_command(folder)}")
    return 0


def roll_back(folder: Path, trail: Path, found: list[dict]) -> int:
    applied = standing(found)
    if applied is None:
        raise Refused("nothing applied from this trail")
    saved = folder / SAVED
    with util.lock("config", wait=True):
        current = paths.CONFIG_FILE.read_bytes()
        try:
            if sha(current) == applied["after_sha"] and saved.is_file():
                put(saved.read_bytes())
                how = "restored the saved config.toml byte for byte"
            else:
                table, lines = applied["table"], current.decode().splitlines(keepends=True)
                for key, c in applied["keys"].items():
                    write_key(lines, table, key, c["before"])
                text = "".join(lines)
                got = held_table(tomllib.loads(text), table)
                if any(got.get(k) != c["before"] for k, c in applied["keys"].items()):
                    raise Refused("the keys would not read back as they were")
                settings.write_checked(text, lambda cfg: "")
                how = "config.toml changed since the apply, so only the applied keys were put back"
        except (OSError, settings.Refused) as e:
            raise Refused(f"rollback failed: {e}; config.toml is unchanged") from e
    append(trail, {"event": "rollback", "how": how, "at": util.iso()})
    print(f"rolled back: {how}")
    return 0


def answers(where: Path, share: float, patch: str = "") -> None:
    where.mkdir(parents=True, exist_ok=True)
    for step, (tokens_in, tokens_out) in TOKENS.items():
        answer = {**replay.SAMPLE_ANSWERS[step], **replay.SAMPLE_USAGE, "tokens_in": int(tokens_in * share),
                  "tokens_out": int(tokens_out * share)}
        (where / f"{step}.json").write_text(json.dumps(answer, indent=2) + "\n")
    if patch:
        (where / "build.patch").write_text(patch)


def sample(folder: Path) -> list[str]:
    lines = replay.sample(folder)
    where = Path(os.path.abspath(folder))
    repo, layer, replies = where / "repo", where / "layer", where / "replay"
    head = util.git(["rev-parse", "HEAD"], repo).strip()
    patch = (replies / "build.patch").read_text()
    (layer / "evals").mkdir()
    for split in experiment.SPLITS:
        (layer / "evals" / f"word-{split}.toml").write_text(
            f"title = {json.dumps(f'Add the word to the notes ({split})')}\n"
            f"prompt = {json.dumps(f'Add the word {replay.SAMPLE_WORD} to {replay.SAMPLE_FILE} on a line of its own.')}\n"
            f"check = {json.dumps(replay.SAMPLE_CHECK)}\n")
    answers(replies / BASELINE, 1)
    answers(replies / "stronger", 1, patch)
    answers(replies / "twin", 1, patch)
    answers(replies / "cheap", 0.25, patch)
    for repeat in (1, 2, 3):
        answers(replies / "cheap" / "word-validation" / str(repeat), 0.25)
    observed = (util.now() - timedelta(minutes=1)).isoformat()
    (where / "prices.json").write_text(json.dumps({
        "schema": 1, "source": "replay sample", "observed": observed,
        "routes": [{"route": name, "kind": "marketplace", "provider": "scratch", "model": name, "currency": "USD",
                    "unit": "per_million_tokens", "rates": [rates]} for name, rates in SAMPLE_PRICES.items()]},
        indent=2) + "\n")
    tasks = "".join(f'\n[tasks.word-{split}]\nbase = "{head}"\nsplit = "{split}"\n' for split in experiment.SPLITS)
    (where / "plan.toml").write_text(
        'name = "sample-search"\nrepo = "' + replay.SAMPLE_REPO + '"\natom = "build"\nrepeats = 6\n\n'
        '[[rounds]]\nvary = "model"\n[rounds.candidates.stronger]\nmodel = "scratch-b"\n\n'
        '[[rounds]]\nvary = "effort"\n[rounds.candidates.cheap]\neffort = "low"\n'
        '[rounds.candidates.twin]\neffort = "high"\n' + tasks +
        "\n[accept]\nmin_pairs = 6\n\n[floor]\naccept_rate = 0.8\nmax_drop = 0.2\nmin_saving = 0.1\n\n"
        "[limits]\nspend_usd = 5\nminutes = 60\ncalls = 2000\ntokens = 5000000\nrounds = 2\n")
    return [*lines, "", "[models]", 'plan = "scratch-a"', "", "[steps.build]", 'model = "scratch-a"',
            'effort = "medium"', "", "[steps.check_repair]", "tries = 1", "",
            f"# next: arete prices import {paths.short_path(where / 'prices.json')}",
            f"# then: arete experiment optimize {paths.short_path(where / 'plan.toml')} --dry-run",
            f"# then: arete experiment optimize {paths.short_path(where / 'plan.toml')}",
            f"# then: arete experiment promote {paths.short_path(paths.STATE_DIR / 'experiments' / 'sample-search')}"]
