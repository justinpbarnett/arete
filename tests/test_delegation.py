import json
import unittest
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from arete import config, delegation, steps
from arete.store.orchestration import digest

CONTROLLER_SETTINGS = '''\
max_parallel = 4
max_parallel_per_repo = 2

[jev]
risk_hold = 0.7
irreversible_hold = 0.8
secrets_hold = 0.9

'''


class WorkerPolicyTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory(ignore_cleanup_errors=True)))
        self.config_file = root / "config.toml"
        self.layer_dir = root / "layer"
        self.settings = f'''\
[[repo]]
name = "example"
path = "{root}"

[steps.build]
runner = "codex"
model = "gpt-5.4"
'''
        self.config_file.write_text(self.settings)
        cfg = config.load(self.config_file, strict=True)
        layer = config.load_layer(self.layer_dir)
        self.launch = self.request(cfg, layer)

        full = asdict(cfg)
        del full["max_parallel"], full["max_parallel_per_repo"]
        del full["jev"]["risk_hold"], full["jev"]["irreversible_hold"], full["jev"]["secrets_hold"]
        self.full_launch = {**self.launch, "config_sha": digest(json.loads(json.dumps(full, default=str)))}

    def request(self, cfg, layer):
        repo = cfg.repo("example")
        run = SimpleNamespace(id="test-run", repo=repo, kind="", closed_context=())
        job = SimpleNamespace(run=run, ws=repo.path, cfg=cfg, layer=layer)
        setting = steps.resolve(cfg, layer, "build")
        return delegation.request(job, "respond", "Read the fixture", setting, (), [])

    def test_controller_settings_edit_keeps_worker_policy(self):
        self.config_file.write_text(CONTROLLER_SETTINGS + self.settings)
        cfg = config.load(self.config_file, strict=True)
        layer = config.load_layer(self.layer_dir)
        for launch in (self.launch, self.full_launch):
            with self.subTest(config_sha=launch["config_sha"]):
                delegation.check_policy(launch, cfg, layer)

    def test_step_model_edit_changes_worker_policy(self):
        self.config_file.write_text(self.settings.replace('model = "gpt-5.4"', 'model = "gpt-5.4-mini"'))
        cfg = config.load(self.config_file, strict=True)
        layer = config.load_layer(self.layer_dir)
        for launch in (self.launch, self.full_launch):
            with self.subTest(config_sha=launch["config_sha"]):
                with self.assertRaises(delegation.PolicyChanged):
                    delegation.check_policy(launch, cfg, layer)

    def legacy_requests(self):
        self.config_file.write_text(CONTROLLER_SETTINGS + self.settings)
        cfg = config.load(self.config_file, strict=True)
        layer = config.load_layer(self.layer_dir)
        launch = self.request(cfg, layer)
        for serialize in (delegation.explicit, asdict):
            legacy_sha = digest(json.loads(json.dumps(serialize(cfg), default=str)))
            self.assertNotEqual(legacy_sha, launch["config_sha"])
            yield {**launch, "config_sha": legacy_sha}

    def test_legacy_requests_accept_unchanged_settings(self):
        launches = list(self.legacy_requests())
        cfg = config.load(self.config_file, strict=True)
        layer = config.load_layer(self.layer_dir)
        for launch in launches:
            with self.subTest(config_sha=launch["config_sha"]):
                delegation.check_policy(launch, cfg, layer)

    def test_legacy_requests_reject_settings_edits(self):
        launches = list(self.legacy_requests())
        layer = config.load_layer(self.layer_dir)
        original = self.config_file.read_text()
        for before, after in (("gpt-5.4", "gpt-5.4-mini"), ("risk_hold = 0.7", "risk_hold = 0.6")):
            self.config_file.write_text(original.replace(before, after))
            cfg = config.load(self.config_file, strict=True)
            for launch in launches:
                with self.subTest(edit=after, config_sha=launch["config_sha"]):
                    with self.assertRaises(delegation.PolicyChanged):
                        delegation.check_policy(launch, cfg, layer)

    def test_layer_edit_changes_worker_policy(self):
        launches = list(self.legacy_requests())
        cfg = config.load(self.config_file, strict=True)
        self.layer_dir.mkdir()
        (self.layer_dir / "config.toml").write_text("[limits]\nmax_attempts = 4\n")
        layer = config.load_layer(self.layer_dir, strict=True)
        for launch in (self.launch, self.full_launch, *launches):
            with self.subTest(config_sha=launch["config_sha"]):
                with self.assertRaises(delegation.PolicyChanged):
                    delegation.check_policy(launch, cfg, layer)
