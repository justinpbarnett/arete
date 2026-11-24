from tests.test_behavior import SECRET_LINUX_CLAUDE_FIXTURE, _SecretChildCase


class AuthorityLinuxFingerprintTests(_SecretChildCase):
    def test_fresh_routed_login_starts_and_defers_publication_until_claude_exits(self):
        self.run_secret_child(SECRET_LINUX_CLAUDE_FIXTURE + r"""
from arete import routing, runner

configured = paths.CONFIG_FILE.read_text()
paths.CONFIG_FILE.write_text('[secrets]\nstore = ""\n')
checkout = linux_checkout(paths.WORKTREES_DIR)
auth = accounts.Auth('subscription', 'claude', identity='fixture-login', provider='anthropic')
model = 'sonnet'
candidate = routing.Candidate(0, model, 'claude', 'anthropic', 'cli_login', 'free', '', 0, True, '', (), None, 'ready', '')
picked = runner.Picked(candidate, auth, None, model, '', {auth}, None)

def active(cmd, cwd, prompt, env, timeout, on_line):
    assert cmd[0] == '/fixture/claude' and not key.exists()
    on_line('{"type":"system"}\n')
    with case.assertRaisesRegex(secretstore.StoreError, 'Retry credential publication'):
        secretstore.fingerprint_key()
    paths.CONFIG_FILE.write_text(configured)
    with case.assertRaisesRegex(secretstore.StoreError, 'Retry credential publication'):
        secretstore.put('global', 'DEMO', 'synthetic-value', 'api_key', 'hand')
    assert not key.exists() and not store.exists()
    return subprocess.CompletedProcess(cmd, 0, '{"type":"result","result":"fixture"}', '')

with (linux_claude() as (which, stream), mock.patch.object(runner, 'pick', return_value=(picked, [])),
      mock.patch.object(runner.util, 'github_token', return_value='')):
    which.return_value, stream.side_effect = None, active
    result = runner.run('fixture prompt', checkout, runner='claude', model=model, cfg=config.Config(), tools=[])
    assert result.ok, result.error
    which.assert_not_called()
    stream.assert_called_once()
    with db.reader() as c:
        assert not secretstore.has_fingerprints(c)
    assert {secretstore.KEY_LOCAL, secretstore.KEY_REF}.isdisjoint(db.schema_metadata())

    assert secretstore.put('global', 'DEMO', 'synthetic-value', 'api_key', 'hand')['status'] == 'set'
""")


class AuthorityLinuxFingerprintMigrationTests(_SecretChildCase):
    def test_retired_fallback_requires_a_committed_valid_store_key(self):
        self.run_secret_child(SECRET_LINUX_CLAUDE_FIXTURE + r"""
configured = paths.CONFIG_FILE.read_text()
paths.CONFIG_FILE.write_text('[secrets]\nstore = ""\n')
original = b'F' * 32
secretstore.write_file(key, key.parent, original)
move_key = secretstore.fingerprint_key
assert move_key() == original
paths.CONFIG_FILE.write_text(configured)
settings = config.secret_settings()
ref = secretstore.Reference('pve_store', db.schema_metadata()['instance_tag'], secretstore.KEY_ITEM)
item, _ = secretstore.file_location(ref, settings)
checkout = linux_checkout(paths.WORKTREES_DIR)
key_info = sandbox.linux_key_info

def launch_after_probe(retire):
    def probe(path):
        info = key_info(path)
        if info is not None:
            retire()
        return info

    with mock.patch.object(sandbox, 'linux_key_info', probe):
        return run_claude(checkout)

with (linux_claude() as (_, stream),
      mock.patch.object(secretstore, 'fingerprint_key', side_effect=AssertionError('must not remint'))):
    store.mkdir()
    store.chmod(0o755)
    assert 'could not prepare the Linux secret store mount' in run_claude(checkout).error
    store.chmod(0o700)
    blocked = launch_after_probe(key.unlink)
    assert not blocked.ok and 'fingerprint_key_missing' in blocked.error
    assert secretstore.KEY_REF not in db.schema_metadata() and not item.exists() and not key.exists()
    assert 'fingerprint_key_missing' in run_claude(checkout).error
    stream.assert_not_called()

    secretstore.write_file(key, key.parent, original)
    assert run_claude(checkout, tools=['Bash']).ok
    assert_mounts(stream, checkout, *key_mask, *store_mask)
    stream.reset_mock()
    launched = launch_after_probe(lambda: move_key(settings))
    assert launched.ok, launched.error
    stream.assert_called_once()
    assert_mounts(stream, checkout, *store_mask)
    assert str(key) not in stream.call_args.args[0]
    assert item.read_bytes() == original and db.schema_metadata()[secretstore.KEY_REF] == str(ref)

    stream.reset_mock()
    item.write_bytes(b'invalid synthetic key')
    for present in (True, False):
        if not present:
            item.unlink()
        blocked = run_claude(checkout)
        assert not blocked.ok and 'fingerprint_key_missing' in blocked.error
        assert item.exists() == present and not key.exists()
    stream.assert_not_called()
""")
