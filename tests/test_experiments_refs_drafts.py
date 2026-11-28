from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from arete import api_check
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT

SOURCE = Path(__file__).resolve().parents[1]


def _copy_engine_repo(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        SOURCE / "arete",
        dest / "arete",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(SOURCE / "pyproject.toml", dest / "pyproject.toml")
    env = {
        **{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "USER", "LANG", "LC_ALL", "TZ")},
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "hand",
        "GIT_AUTHOR_EMAIL": "hand@example.test",
        "GIT_COMMITTER_NAME": "hand",
        "GIT_COMMITTER_EMAIL": "hand@example.test",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dest, env=env, check=True, timeout=CHILD_HANG_TIMEOUT)
    subprocess.run(["git", "add", "."], cwd=dest, env=env, check=True, timeout=CHILD_HANG_TIMEOUT)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "init engine copy"],
        cwd=dest,
        env=env,
        check=True,
        timeout=CHILD_HANG_TIMEOUT,
    )
    return dest


def _run_child(
    test_case: unittest.TestCase,
    body: str,
    *,
    copy_engine: bool = False,
    dirty_engine: bool = False,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="arete-exp-test-") as tmp:
        root = Path(tmp).resolve()
        env = api_check.isolated_env(root)
        env["ARETE_TEST_FAST_SYNC"] = "1"
        pkg_root = _copy_engine_repo(root / "engine-copy") if copy_engine else SOURCE
        if dirty_engine:
            target = pkg_root / "arete" / "util.py"
            target.write_text(target.read_text(encoding="utf-8") + "\n# initial dirty\n", encoding="utf-8")
        preamble = textwrap.dedent(f"""\
            import json, os, shutil, subprocess, sys, threading, time, tomllib
            from contextlib import contextmanager, redirect_stdout
            from io import StringIO
            from pathlib import Path
            from types import SimpleNamespace
            from unittest import mock

            from arete import (
                api, api_check, codebase, config, configrepo, contract, dashboard, drafts,
                evals, paths, pins, prices, principals, prompts, record, replay, runner, settings,
                tailnet, util,
            )
            from arete.lab import experiment, optimize
            from arete.store import (
                blobs as blob_store, db, runs as run_store, tokens as token_store,
            )

            replay.time.sleep = lambda _s: None
            root = Path({str(root)!r})
            engine_copy = Path({str(pkg_root)!r})
            GIT_ENV = {{
                **{{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "USER", "LANG", "LC_ALL", "TZ")}},
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "hand",
                "GIT_AUTHOR_EMAIL": "hand@example.test",
                "GIT_COMMITTER_NAME": "hand",
                "GIT_COMMITTER_EMAIL": "hand@example.test",
            }}

            def git_run(args, cwd):
                return subprocess.run(
                    ["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True, timeout=15
                )

            def setup_sample_env(folder_name="sample-fixture", min_pairs=3):
                sample_dir = root / folder_name
                sample_lines = optimize.sample(sample_dir)
                layer_dest = paths.CONFIG_DIR / "layers" / replay.SAMPLE_REPO
                if layer_dest.exists():
                    shutil.rmtree(layer_dest)
                shutil.copytree(sample_dir / "layer", layer_dest)
                rewritten = [
                    f"layer = {{json.dumps(str(layer_dest))}}" if line.startswith("layer = ") else line
                    for line in sample_lines
                    if not line.startswith("#")
                ]
                paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                paths.CONFIG_FILE.write_text("\\n".join(rewritten) + "\\n", encoding="utf-8")
                with redirect_stdout(StringIO()):
                    db.migrate()
                configrepo.init()
                prices.store(prices.read_file(sample_dir / "prices.json"))
                experiment.MIN_PAIRS = min_pairs
                cfg = config.load()
                repo = cfg.repo(replay.SAMPLE_REPO)
                head = git_run(["rev-parse", "HEAD"], repo.path).stdout.strip()
                return sample_dir, repo, head

            def fast_eval_checkout(repo_obj, base_sha):
                template_wt = root / "fast-eval-template"
                evals.checkout_without_origin_refs(repo_obj.path, base_sha, template_wt)
                evals.fetch_origin = lambda _r: None

                def _copy_checkout(_repo_path, _base, dest):
                    shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(template_wt, dest)
                    return base_sha

                evals.checkout_without_origin_refs = _copy_checkout

            def fast_single_repeat_stats():
                orig_pairing = experiment.pairing

                def _pairing(done, baseline, candidate, split, confidence):
                    out = orig_pairing(done, baseline, candidate, split, confidence)
                    if out["pairs"] >= 1 and out["candidate_only"] > out["baseline_only"]:
                        return {{**out, "interval": [0.5, 1.0], "p": 0.01}}
                    return out

                experiment.pairing = _pairing

            codebase.load()
        """)
        script = preamble + "\n" + textwrap.dedent(body) + "\nprint('BEHAVIOR_RESULT=' + json.dumps(result))\n"
        cmd = api_check.child_command(pkg_root, script)
        done = subprocess.run(
            cmd,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=CHILD_HANG_TIMEOUT,
        )
        test_case.assertEqual(done.returncode, 0, done.stdout + "\n" + done.stderr)
        lines = [line for line in done.stdout.splitlines() if line.startswith("BEHAVIOR_RESULT=")]
        test_case.assertEqual(len(lines), 1, done.stdout + "\n" + done.stderr)
        return json.loads(lines[0].partition("=")[2])


class ExperimentArmCheckTests(unittest.TestCase):
    def test_arm_check_refuses_undeclared_key_and_records_arm_prompt_level(self):
        res = _run_child(self, r"""
            sample_dir, repo, head = setup_sample_env()
            # Verify m0062 migration recorded and user_version == db.LATEST
            with db.reader() as c:
                uv = c.execute("PRAGMA user_version").fetchone()[0]
                m63 = c.execute(
                    "SELECT name FROM schema_migrations WHERE name = 'm0063_runs_arm.py'"
                ).fetchone()
            assert uv == db.LATEST and m63 is not None, (uv, m63)

            # 1. Arm check refuses undeclared key settings.build.tools before any run row or trial line exists
            bad_manifest = root / "bad-vary.toml"
            bad_manifest.write_text(
                'name = "bad-vary"\n'
                f'repo = "{replay.SAMPLE_REPO}"\n'
                'atom = "build"\n'
                'repeats = 1\n'
                'vary = ["settings.build.effort"]\n\n'
                '[arms.baseline]\n\n'
                '[arms.cheap]\n'
                'effort = "low"\n'
                'tools = ["Read"]\n\n'
                '[tasks.word-train]\n'
                f'base = "{head}"\n'
                'split = "train"\n\n'
                '[accept]\n'
                'baseline = "baseline"\n'
                'split = "train"\n'
                'min_pairs = 3\n\n'
                '[limits]\n'
                'spend_usd = 5\n'
                'minutes = 10\n'
                'calls = 50\n'
                'tokens = 500000\n',
                encoding="utf-8",
            )
            out_bad = root / "bad-vary.jsonl"
            refused_msg = ""
            try:
                experiment.run(bad_manifest, str(out_bad))
            except experiment.Refused as exc:
                refused_msg = str(exc)
            assert "settings.build.tools" in refused_msg, refused_msg
            assert not out_bad.exists() or not experiment.events(out_bad)
            assert run_store.runs(kind="experiment", limit=10) == []

            # 2. Prompt arm commits onto refs/arete/arms/<exp>/<arm>, baseline commit is empty, prompt_level == 'arm'
            base_build = (paths.PROMPTS_DIR / "build.md").read_text(encoding="utf-8")
            custom_build = base_build + "\n<!-- arm-prompt-variant -->\n"
            good_manifest = root / "arm-prompt-exp.toml"
            good_manifest.write_text(
                'name = "arm-prompt-exp"\n'
                f'repo = "{replay.SAMPLE_REPO}"\n'
                'atom = "build"\n'
                'repeats = 1\n'
                'vary = ["prompts.build"]\n\n'
                '[arms.baseline]\n\n'
                '[arms.custom]\n'
                f'prompts = {{ build = {json.dumps(custom_build)} }}\n\n'
                '[tasks.word-train]\n'
                f'base = "{head}"\n'
                'split = "train"\n\n'
                '[accept]\n'
                'baseline = "baseline"\n'
                'split = "train"\n'
                'min_pairs = 3\n\n'
                '[limits]\n'
                'spend_usd = 5\n'
                'minutes = 10\n'
                'calls = 50\n'
                'tokens = 500000\n',
                encoding="utf-8",
            )
            out_good = root / "arm-prompt.jsonl"
            with redirect_stdout(StringIO()):
                rc = experiment.run(good_manifest, str(out_good))
            assert rc == 0, rc

            # Baseline commit is empty; arm commits are on refs/arete/arms/... and not on live
            base_ref = "refs/arete/arms/arm-prompt-exp/baseline"
            custom_ref = "refs/arete/arms/arm-prompt-exp/custom"
            base_commit = configrepo.resolve_ref(base_ref)
            custom_commit = configrepo.resolve_ref(custom_ref)
            assert base_commit and custom_commit
            assert configrepo.commit_changed_paths(base_commit) == []
            assert configrepo.commit_changed_paths(custom_commit) == ["prompts/build.md"]
            assert configrepo.live_bytes("prompts/build.md") is None

            # Verify steps for custom arm recorded prompt_level == 'arm' and runs.arm == '<exp>:<arm>'
            exp_runs = run_store.runs(kind="experiment", limit=10)
            assert len(exp_runs) == 2
            arms_seen = {r["arm"] for r in exp_runs}
            assert arms_seen == {"arm-prompt-exp:baseline", "arm-prompt-exp:custom"}, arms_seen
            custom_run = next(r for r in exp_runs if r["arm"] == "arm-prompt-exp:custom")
            build_step = next(s for s in run_store.steps(custom_run["id"]) if s["step"] == "build")
            assert build_step["prompt_level"] == "arm", build_step
            result = {"ok": True}
        """)
        self.assertTrue(res["ok"])

def _run_pre_trial_drift_scenario(test_case: unittest.TestCase, scenario: str) -> None:
    res = _run_child(
        test_case,
        f"""
            scenario = {scenario!r}
            sample_dir, repo, head = setup_sample_env()
            manifest_path = root / "drift-exp.toml"
            manifest_path.write_text(
                'name = "drift-exp"\\n'
                f'repo = "{{replay.SAMPLE_REPO}}"\\n'
                'atom = "build"\\n'
                'repeats = 1\\n'
                'vary = ["settings.build.effort"]\\n\\n'
                '[arms.baseline]\\n\\n'
                '[arms.cheap]\\n'
                'effort = "low"\\n\\n'
                '[tasks.word-train]\\n'
                f'base = "{{head}}"\\n'
                'split = "train"\\n\\n'
                '[accept]\\n'
                'baseline = "baseline"\\n'
                'split = "train"\\n'
                'min_pairs = 3\\n\\n'
                '[limits]\\n'
                'spend_usd = 5\\n'
                'minutes = 10\\n'
                'calls = 50\\n'
                'tokens = 500000\\n',
                encoding="utf-8",
            )
            out_file = root / "drift-exp.jsonl"
            orig_run_trial = experiment.run_trial
            calls = [0]

            def wrapped_trial(*a, **kw):
                why = orig_run_trial(*a, **kw)
                calls[0] += 1
                if calls[0] == 1:
                    if scenario == "prompt":
                        bp = engine_copy / "arete" / "prompts" / "build.md"
                        bp.write_text(bp.read_text(encoding="utf-8") + "\\n# drifted prompt\\n", encoding="utf-8")
                    elif scenario == "engine_commit":
                        mod = engine_copy / "arete" / "util.py"
                        mod.write_text(mod.read_text(encoding="utf-8") + "\\n# committed drift\\n", encoding="utf-8")
                        git_run(["add", "arete/util.py"], engine_copy)
                        git_run(["-c", "commit.gpgsign=false", "commit", "-qm", "drift commit"], engine_copy)
                    elif scenario == "engine_dirty":
                        mod = engine_copy / "arete" / "util.py"
                        mod.write_text(mod.read_text(encoding="utf-8") + "\\n# second dirty edit\\n", encoding="utf-8")
                    elif scenario == "harness":
                        (repo.path / "CLAUDE.md").write_text("# drifted harness\\n", encoding="utf-8")
                return why

            with mock.patch.object(experiment, "run_trial", side_effect=wrapped_trial), redirect_stdout(StringIO()):
                rc = experiment.run(manifest_path, str(out_file))
            assert rc != 0, rc
            evs = experiment.events(out_file)
            stop = next((e for e in reversed(evs) if e.get("event") == "stop"), None)
            assert stop is not None, evs
            expected_key = {{
                "prompt": "prompts.build",
                "engine_commit": "engine_sha",
                "engine_dirty": "engine_sha",
                "harness": "harness",
            }}[scenario]
            assert expected_key in stop["why"], (scenario, stop)
            exp_runs = run_store.runs(kind="experiment", limit=10)
            assert len(exp_runs) == 1, (scenario, exp_runs)
            result = {{"ok": True, "stop": stop["why"]}}
        """,
        copy_engine=(scenario != "harness"),
        dirty_engine=(scenario == "engine_dirty"),
    )
    test_case.assertTrue(res["ok"])


class ExperimentPreTrialDriftInputsTests(unittest.TestCase):
    def test_pre_trial_drift_detects_prompt_and_harness(self):
        for scenario in ("prompt", "harness"):
            with self.subTest(scenario=scenario):
                _run_pre_trial_drift_scenario(self, scenario)


class ExperimentPreTrialDriftEngineTests(unittest.TestCase):
    def test_pre_trial_drift_detects_engine_commit_and_dirty_edit(self):
        for scenario in ("engine_commit", "engine_dirty"):
            with self.subTest(scenario=scenario):
                _run_pre_trial_drift_scenario(self, scenario)


class ExperimentMidTrialDriftTests(unittest.TestCase):
    def test_mid_trial_engine_commit_drifts_trial_and_excludes_from_summary_and_evidence(self):
        res = _run_child(self, r"""
            sample_dir, repo, head = setup_sample_env()
            manifest_path = root / "mid-drift.toml"
            manifest_path.write_text(
                'name = "mid-drift"\n'
                f'repo = "{replay.SAMPLE_REPO}"\n'
                'atom = "build"\n'
                'repeats = 1\n'
                'vary = ["settings.build.effort"]\n\n'
                '[arms.baseline]\n\n'
                '[arms.cheap]\n'
                'effort = "low"\n\n'
                '[tasks.word-train]\n'
                f'base = "{head}"\n'
                'split = "train"\n\n'
                '[accept]\n'
                'baseline = "baseline"\n'
                'split = "train"\n'
                'min_pairs = 3\n\n'
                '[limits]\n'
                'spend_usd = 5\n'
                'minutes = 10\n'
                'calls = 50\n'
                'tokens = 500000\n',
                encoding="utf-8",
            )
            out_file = root / "mid-drift.jsonl"
            orig_runner = runner.run
            committed = [False]

            def wrapped_runner(prompt_text, cwd, **kw):
                step_name = kw.get("trace", ("", ""))[1]
                if step_name == "build" and not committed[0]:
                    committed[0] = True
                    mod = engine_copy / "arete" / "util.py"
                    mod.write_text(mod.read_text(encoding="utf-8") + "\n# mid-trial commit\n", encoding="utf-8")
                    git_run(["add", "arete/util.py"], engine_copy)
                    git_run(["-c", "commit.gpgsign=false", "commit", "-qm", "mid trial drift"], engine_copy)
                return orig_runner(prompt_text, cwd, **kw)

            with mock.patch.object(runner, "run", side_effect=wrapped_runner), redirect_stdout(StringIO()):
                rc = experiment.run(manifest_path, str(out_file))
            assert rc != 0, rc
            evs = experiment.events(out_file)
            trials = [e for e in evs if e.get("event") == "trial"]
            assert len(trials) == 1, trials
            t = trials[0]
            assert t["status"] == "drifted", t
            assert "engine_sha" in (t.get("drift") or []), t
            stop = next((e for e in reversed(evs) if e.get("event") == "stop"), None)
            assert stop is not None and "engine_sha" in stop["why"], stop
            assert len(run_store.runs(kind="experiment", limit=10)) == 1

            # Summary counts drifted trial in no arm summary, pair, or done total
            summ = experiment.summary(out_file)
            assert summ["done"] == 0 and summ["drifted"] == 1, summ
            for arm_stats in summ["arms"].values():
                assert arm_stats["attempts"] == 0 and arm_stats["done"] == 0, arm_stats
            for split_pairs in summ["pairs"].values():
                for pair in split_pairs:
                    assert pair["pairs"] == 0, pair

            # Evidence collection excludes drifted trial run id
            trial_dir = root / "ev-check"
            trial_dir.mkdir()
            shutil.copy2(out_file, trial_dir / "phase.jsonl")
            collected = optimize._trial_run_ids(trial_dir, [])
            assert t["run"] not in collected, (t["run"], collected)
            result = {"ok": True}
        """, copy_engine=True)
        self.assertTrue(res["ok"])


class ExperimentResumeRefusalTests(unittest.TestCase):
    def test_resume_refuses_engine_commit_and_grader_prompt_edit(self):
        for kind in ("engine_sha", "grader"):
            with self.subTest(kind=kind):
                res = _run_child(self, f"""
                    kind = {kind!r}
                    sample_dir, repo, head = setup_sample_env()
                    manifest_path = root / "resume-exp.toml"
                    manifest_path.write_text(
                        'name = "resume-exp"\\n'
                        f'repo = "{{replay.SAMPLE_REPO}}"\\n'
                        'atom = "build"\\n'
                        'repeats = 1\\n'
                        'vary = ["settings.build.effort"]\\n\\n'
                        '[arms.baseline]\\n\\n'
                        '[arms.cheap]\\n'
                        'effort = "low"\\n\\n'
                        '[tasks.word-train]\\n'
                        f'base = "{{head}}"\\n'
                        'split = "train"\\n\\n'
                        '[accept]\\n'
                        'baseline = "baseline"\\n'
                        'split = "train"\\n'
                        'min_pairs = 3\\n\\n'
                        '[limits]\\n'
                        'spend_usd = 5\\n'
                        'minutes = 10\\n'
                        'calls = 2\\n'
                        'tokens = 500000\\n',
                        encoding="utf-8",
                    )
                    out_file = root / "resume-exp.jsonl"
                    with redirect_stdout(StringIO()):
                        rc1 = experiment.run(manifest_path, str(out_file))
                    assert rc1 == experiment.LIMIT_EXIT, rc1
                    runs_before = len(run_store.runs(kind="experiment", limit=10))
                    assert runs_before == 1, runs_before

                    # Raise calls limit so resume would otherwise launch trial 2
                    manifest_path.write_text(
                        manifest_path.read_text(encoding="utf-8").replace("calls = 2", "calls = 50"),
                        encoding="utf-8",
                    )
                    if kind == "engine_sha":
                        mod = engine_copy / "arete" / "util.py"
                        mod.write_text(mod.read_text(encoding="utf-8") + "\\n# resume engine change\\n", encoding="utf-8")
                        git_run(["add", "arete/util.py"], engine_copy)
                        git_run(["-c", "commit.gpgsign=false", "commit", "-qm", "engine move"], engine_copy)
                    else:
                        gp = engine_copy / "arete" / "prompts" / "grade.md"
                        gp.write_text(gp.read_text(encoding="utf-8") + "\\n# grader edit\\n", encoding="utf-8")

                    refused = ""
                    try:
                        with redirect_stdout(StringIO()):
                            experiment.run(manifest_path, str(out_file))
                    except experiment.Refused as exc:
                        refused = str(exc)
                    assert kind in refused, (kind, refused)
                    assert len(run_store.runs(kind="experiment", limit=10)) == runs_before
                    result = {{"ok": True, "refused": refused}}
                """, copy_engine=True)
                self.assertTrue(res["ok"])


class OptimizeSettingsPromotionTests(unittest.TestCase):
    def test_sample_search_pins_promote_activate_merge_conflict_and_rollback(self):
        res = _run_child(self, r"""
            sample_dir, repo, head = setup_sample_env(min_pairs=1)
            fast_eval_checkout(repo, head)
            fast_single_repeat_stats()
            plan = sample_dir / "plan.toml"
            plan.write_text(
                plan.read_text(encoding="utf-8")
                .replace("repeats = 6", "repeats = 1")
                .replace("min_pairs = 6", "min_pairs = 1")
                .replace("rounds = 2", "rounds = 1"),
                encoding="utf-8",
            )
            trail_dir = root / "sample-trail"
            with redirect_stdout(StringIO()):
                rc = optimize.run(plan, str(trail_dir))
            assert rc == 0, rc

            # Every trial line has resolved_sha and engine_sha matching its runs row,
            # runs.arm == '<exp>:<arm>', and NO trial line has 'fingerprints'
            all_trial_run_ids = []
            for jfile in sorted(trail_dir.glob("*.jsonl")):
                if jfile.name == "trail.jsonl":
                    continue
                m_ev = next(e for e in experiment.events(jfile) if e.get("event") == "manifest")
                exp_name = m_ev["design"]["name"]
                for ev in experiment.events(jfile):
                    if ev.get("event") != "trial":
                        continue
                    assert "fingerprints" not in ev, ev
                    rrow = run_store.run_get(ev["run"])
                    assert rrow is not None, ev["run"]
                    assert ev["resolved_sha"] and ev["resolved_sha"] == rrow["resolved_sha"], (ev, rrow)
                    assert ev["engine_sha"] and ev["engine_sha"] == rrow["engine_sha"], (ev, rrow)
                    assert rrow["arm"] == f"{exp_name}:{ev['arm']}", rrow
                    all_trial_run_ids.append(ev["run"])
            assert all_trial_run_ids

            # promote --apply leaves one valid gate B config_drafts row, config.toml unchanged,
            # and no config-before-apply.toml
            cfg_before = paths.CONFIG_FILE.read_bytes()
            live_before = configrepo.live_sha()
            tree_before = configrepo.tree_sha(live_before)

            with redirect_stdout(StringIO()):
                prc = optimize.promote(trail_dir, apply=True)
            assert prc == 0, prc
            assert paths.CONFIG_FILE.read_bytes() == cfg_before
            assert not (paths.CONFIG_DIR / "config-before-apply.toml").exists()

            draft_rows = drafts.list_drafts(state="valid")
            assert len(draft_rows) == 1, draft_rows
            d = draft_rows[0]
            assert d["gate"] == "B" and d["state"] == "valid", d
            assert isinstance(d["evidence"], dict), d["evidence"]
            assert d["evidence"]["experiment"] == "experiment:sample-search", d["evidence"]
            assert set(d["evidence"]["trials"]) >= set(all_trial_run_ids), d["evidence"]
            arm_commit = d["evidence"]["arm_commit"]
            assert arm_commit

            # Activate merges the arm commit so it is an ancestor of live
            live_ver = drafts._live_ver_for(d["kind"], d["name"], d["path"])
            act = drafts.activate(d["id"], "hand", live_ver)
            live_after = configrepo.live_sha()
            assert live_after == act["commit"]
            is_anc = subprocess.run(
                ["git", "merge-base", "--is-ancestor", arm_commit, "live"],
                cwd=paths.CONFIG_DIR,
                env=GIT_ENV,
                capture_output=True,
                timeout=15,
            )
            assert is_anc.returncode == 0, is_anc.stderr

            # Rollback adds a revert commit whose tree equals live before activation
            with redirect_stdout(StringIO()):
                rrc = optimize.promote(trail_dir, rollback=True)
            assert rrc == 0, rrc
            live_reverted = configrepo.live_sha()
            assert live_reverted != live_after
            assert configrepo.tree_sha(live_reverted) == tree_before

            # Activating another arm draft whose target moved since arm cut returns 409 version_conflict
            # Cut a fresh draft, mutate config.toml on live, then attempt activation
            with redirect_stdout(StringIO()):
                assert optimize.promote(trail_dir, apply=True) == 0
            d2 = drafts.list_drafts(state="valid")[0]
            cur_text, cur_blob = configrepo.read("config.toml")
            configrepo.commit(
                {"config.toml": cur_text + "\n# moved after arm cut\n"},
                {"config.toml": cur_blob},
                actor="hand",
                source="hand",
                note="move live after arm cut",
            )
            moved_ver = drafts._live_ver_for(d2["kind"], d2["name"], d2["path"])
            conflict_code = ""
            try:
                drafts.activate(d2["id"], "hand", moved_ver)
            except contract.Problem as exc:
                conflict_code = exc.code
            assert conflict_code == "version_conflict", conflict_code
            result = {"ok": True, "trials": len(all_trial_run_ids)}
        """)
        self.assertTrue(res["ok"])


class OptimizePromptPromotionTests(unittest.TestCase):
    def test_prompt_round_winner_promotes_activates_and_rolls_back(self):
        res = _run_child(self, r"""
            sample_dir, repo, head = setup_sample_env(min_pairs=1)
            fast_eval_checkout(repo, head)
            fast_single_repeat_stats()
            prompt_dir = root / "winner-prompts"
            shutil.copytree(paths.PROMPTS_DIR, prompt_dir)
            (prompt_dir / "build.md").write_text(
                (prompt_dir / "build.md").read_text(encoding="utf-8") + "\n<!-- prompt-winner-v1 -->\n",
                encoding="utf-8",
            )
            patch = (sample_dir / "replay" / "build.patch").read_text(encoding="utf-8")
            # Baseline passes only word-train so R1 same-effort (0/1) is rejected on train and skips R1 validation.
            optimize.answers(sample_dir / "replay" / "baseline" / "word-train" / "1", 1, patch)
            optimize.answers(sample_dir / "replay" / "better-prompt", 1, patch)
            optimize.answers(sample_dir / "replay" / "same-effort", 1)

            tasks_toml = "".join(
                f'\n[tasks.word-{sp}]\nbase = "{head}"\nsplit = "{sp}"\n'
                for sp in experiment.SPLITS
            )
            prompt_plan = root / "prompt-search.toml"
            prompt_plan.write_text(
                'name = "prompt-search"\n'
                f'repo = "{replay.SAMPLE_REPO}"\n'
                'atom = "build"\n'
                'repeats = 1\n\n'
                '[[rounds]]\n'
                'vary = "effort"\n'
                '[rounds.candidates.same-effort]\n'
                'effort = "high"\n\n'
                '[[rounds]]\n'
                'vary = "prompt"\n'
                '[rounds.candidates.better-prompt]\n'
                f'prompts = {json.dumps(str(prompt_dir))}\n'
                + tasks_toml
                + '\n[accept]\nmin_pairs = 1\n\n'
                '[floor]\naccept_rate = 0.8\nmax_drop = 0.2\nmin_saving = 0.1\n\n'
                '[limits]\nspend_usd = 5\nminutes = 60\ncalls = 2000\ntokens = 5000000\nrounds = 2\n',
                encoding="utf-8",
            )
            trail_dir = root / "prompt-trail"
            with redirect_stdout(StringIO()):
                rc = optimize.run(prompt_plan, str(trail_dir))
            assert rc == 0, rc

            live_before = configrepo.live_sha()
            tree_before = configrepo.tree_sha(live_before)
            assert configrepo.live_bytes("prompts/build.md") is None

            # promote --apply creates valid gate B prompt draft, live prompts/build.md untouched
            with redirect_stdout(StringIO()):
                prc = optimize.promote(trail_dir, apply=True)
            assert prc == 0, prc
            assert configrepo.live_bytes("prompts/build.md") is None
            valid_drafts = drafts.list_drafts(state="valid")
            assert len(valid_drafts) == 1, valid_drafts
            pd = valid_drafts[0]
            assert pd["kind"] == "prompt" and pd["name"] == "build" and pd["gate"] == "B", pd
            assert pd["evidence"]["experiment"] == "experiment:prompt-search", pd["evidence"]
            assert pd["evidence"]["trials"], pd["evidence"]
            arm_commit = pd["evidence"]["arm_commit"]

            # config.activate makes arm commit an ancestor of live and writes prompts/build.md
            live_ver = drafts._live_ver_for("prompt", "build", "prompts/build.md")
            drafts.activate(pd["id"], "hand", live_ver)
            is_anc = subprocess.run(
                ["git", "merge-base", "--is-ancestor", arm_commit, "live"],
                cwd=paths.CONFIG_DIR,
                env=GIT_ENV,
                capture_output=True,
                timeout=15,
            )
            assert is_anc.returncode == 0
            assert configrepo.live_bytes("prompts/build.md") is not None

            # config.rollback adds a revert commit whose tree equals live before activation
            with redirect_stdout(StringIO()):
                rrc = optimize.promote(trail_dir, rollback=True)
            assert rrc == 0, rrc
            assert configrepo.tree_sha(configrepo.live_sha()) == tree_before
            assert configrepo.live_bytes("prompts/build.md") is None
            result = {"ok": True}
        """)
        self.assertTrue(res["ok"])


class OptimizePromptMutationTests(unittest.TestCase):
    def test_refuses_mutated_winning_prompt_between_train_and_validation(self):
        res = _run_child(self, r"""
            sample_dir, repo, head = setup_sample_env(min_pairs=1)
            fast_eval_checkout(repo, head)
            prompt_dir = root / "winner-prompts"
            shutil.copytree(paths.PROMPTS_DIR, prompt_dir)
            (prompt_dir / "build.md").write_text(
                (prompt_dir / "build.md").read_text(encoding="utf-8") + "\n<!-- prompt-winner-v1 -->\n",
                encoding="utf-8",
            )
            patch = (sample_dir / "replay" / "build.patch").read_text(encoding="utf-8")
            # Baseline passes only word-train so R1 same-effort (0/1) is rejected on train and skips R1 validation.
            optimize.answers(sample_dir / "replay" / "baseline" / "word-train" / "1", 1, patch)
            optimize.answers(sample_dir / "replay" / "better-prompt", 1, patch)
            optimize.answers(sample_dir / "replay" / "same-effort", 1)

            tasks_toml = "".join(
                f'\n[tasks.word-{sp}]\nbase = "{head}"\nsplit = "{sp}"\n'
                for sp in experiment.SPLITS
            )
            prompt_plan = root / "prompt-search.toml"
            prompt_plan.write_text(
                'name = "prompt-search"\n'
                f'repo = "{replay.SAMPLE_REPO}"\n'
                'atom = "build"\n'
                'repeats = 1\n\n'
                '[[rounds]]\n'
                'vary = "effort"\n'
                '[rounds.candidates.same-effort]\n'
                'effort = "high"\n\n'
                '[[rounds]]\n'
                'vary = "prompt"\n'
                '[rounds.candidates.better-prompt]\n'
                f'prompts = {json.dumps(str(prompt_dir))}\n'
                + tasks_toml
                + '\n[accept]\nmin_pairs = 3\nconfidence = 0.70\n\n'
                '[floor]\naccept_rate = 0.8\nmax_drop = 0.2\nmin_saving = 0.1\n\n'
                '[limits]\nspend_usd = 5\nminutes = 60\ncalls = 2000\ntokens = 5000000\nrounds = 2\n',
                encoding="utf-8",
            )
            mut_trail = root / "mut-prompt-trail"
            orig_phase = optimize.phase
            mutated = [False]

            def mutate_before_val(s, folder, tag, split, arms, champion, pinned=None):
                if tag == "r2-validation" and not mutated[0]:
                    mutated[0] = True
                    (prompt_dir / "build.md").write_text(
                        (prompt_dir / "build.md").read_text(encoding="utf-8") + "\n<!-- mutated -->\n",
                        encoding="utf-8",
                    )
                return orig_phase(s, folder, tag, split, arms, champion, pinned)

            refused_msg = ""
            try:
                with mock.patch.object(optimize, "phase", side_effect=mutate_before_val), redirect_stdout(StringIO()):
                    optimize.run(prompt_plan, str(mut_trail))
            except experiment.Refused as exc:
                refused_msg = str(exc)
            assert "better-prompt" in refused_msg and "resolution" in refused_msg, refused_msg
            result = {"ok": True}
        """)
        self.assertTrue(res["ok"])


class ExperimentApiRouteTests(unittest.TestCase):
    def test_experiment_api_routes_wait_idempotency_scope_and_events(self):
        res = _run_child(self, r"""
            sample_dir, repo, head = setup_sample_env()
            # Commit experiments/sample.toml through configrepo.commit -> emits config.experiment.changed
            manifest_toml = (
                'name = "sample"\n'
                f'repo = "{replay.SAMPLE_REPO}"\n'
                'atom = "build"\n'
                'repeats = 1\n'
                'vary = ["settings.build.effort"]\n\n'
                '[arms.baseline]\n\n'
                '[arms.cheap]\n'
                'effort = "low"\n\n'
                '[tasks.word-train]\n'
                f'base = "{head}"\n'
                'split = "train"\n\n'
                '[accept]\n'
                'baseline = "baseline"\n'
                'split = "train"\n'
                'min_pairs = 3\n\n'
                '[limits]\n'
                'spend_usd = 5\n'
                'minutes = 10\n'
                'calls = 50\n'
                'tokens = 500000\n'
            )
            _, base_blob = configrepo.read("experiments/sample.toml")
            configrepo.commit(
                {"experiments/sample.toml": manifest_toml},
                {"experiments/sample.toml": base_blob},
                actor="hand",
                source="hand",
                note="add sample experiment",
            )

            # Verify events_log holds config.experiment.changed
            with db.reader() as c:
                ev_rows = c.execute(
                    "SELECT kind, subject FROM events_log WHERE kind = 'config.experiment.changed'"
                ).fetchall()
            assert len(ev_rows) >= 1 and ev_rows[-1]["subject"] == "config:experiment/sample", [dict(r) for r in ev_rows]

            # Start test API server with run:start token and read-only token
            owner = principals.seed_owner(api_check.LOGIN)
            with db.tx() as c:
                token_store.ensure_key(c)
                _, start_tok = token_store.issue(owner, "Exp start", ["read", "run:start"])
                _, read_tok = token_store.issue(owner, "Read only", ["read"])
            node = SimpleNamespace(names={api_check.ADDRESS})
            logins = tailnet.Logins(tailnet.ScratchHost())
            logins.of = lambda _: api_check.LOGIN
            with mock.patch("socket.getfqdn", return_value=api_check.ADDRESS):
                srv = tailnet.Server(api_check.ADDRESS, 0, node, logins, False, dashboard.Handler, dashboard.Redirect)

            gate = threading.Event()
            orig_drive = experiment.drive

            def gated_drive(exp, path):
                gate.wait(timeout=5)
                return orig_drive(exp, path)

            with srv:
                t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
                t.start()
                try:
                    anon = api_check.Check(srv.server_port, root, [], token=None)
                    read_only = api_check.Check(srv.server_port, root, [], token=read_tok)
                    starter = api_check.Check(srv.server_port, root, [], token=start_tok)

                    # Unauthenticated GET /api/v1/experiments behaves like other reads (401)
                    anon.request("GET", "/api/v1/experiments", status=401)

                    # Read-scoped GET /api/v1/experiments lists experiment:sample with its arms
                    listed = read_only.read("/api/v1/experiments")["experiments"]
                    found = next((e for e in listed if e["id"] == "experiment:sample"), None)
                    assert found is not None, listed
                    assert [a["name"] for a in found["arms"]] == ["baseline", "cheap"], found["arms"]

                    # Token without run:start gets 403
                    read_only.request(
                        "POST",
                        "/api/v1/experiments/sample/run",
                        {"mode": "run"},
                        headers={"Idempotency-Key": "exp-forbid-1"},
                        status=403,
                    )

                    # POST with run:start and Prefer: wait=0.2 answers 202 with run id,
                    # same Idempotency-Key returns same run id
                    with mock.patch.object(experiment, "drive", side_effect=gated_drive):
                        _, body1 = starter.request(
                            "POST",
                            "/api/v1/experiments/sample/run",
                            {"mode": "run"},
                            headers={"Idempotency-Key": "exp-run-idem-1", "Prefer": "wait=0.2"},
                            status=202,
                        )
                        ans1 = json.loads(body1)
                        assert ans1.get("accepted") is True, ans1
                        run_id = ans1["run"]["id"]
                        assert run_id

                        _, body2 = starter.request(
                            "POST",
                            "/api/v1/experiments/sample/run",
                            {"mode": "run"},
                            headers={"Idempotency-Key": "exp-run-idem-1", "Prefer": "wait=0.2"},
                            status=202,
                        )
                        ans2 = json.loads(body2)
                        assert ans2["run"]["id"] == run_id, ans2

                        # Release experiment worker and wait until GET /api/v1/experiments/sample returns results
                        gate.set()
                        deadline = time.monotonic() + 10
                        got_results = None
                        while time.monotonic() < deadline:
                            detail = read_only.read("/api/v1/experiments/sample")["experiment"]
                            if detail.get("results") and detail["results"].get("done") == 2:
                                got_results = detail["results"]
                                break
                            time.sleep(0.05)
                        assert got_results is not None, detail
                        assert got_results["done"] == 2 and got_results["name"] == "sample", got_results
                finally:
                    gate.set()
                    srv.shutdown()
                    t.join(timeout=5)
            result = {"ok": True}
        """)
        self.assertTrue(res["ok"])


if __name__ == "__main__":
    unittest.main()
