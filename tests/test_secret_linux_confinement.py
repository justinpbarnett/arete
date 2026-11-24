from tests.test_behavior import SECRET_LINUX_CLAUDE_FIXTURE, _SecretChildCase


class AuthorityLinuxFingerprintTests(_SecretChildCase):
    def test_fallback_mask_covers_only_the_key_with_or_without_a_store(self):
        self.run_secret_child(SECRET_LINUX_CLAUDE_FIXTURE + r"""
configured = paths.CONFIG_FILE.read_text()
paths.CONFIG_FILE.write_text('[secrets]\nstore = ""\n')
checkout = linux_checkout()
sessions = state / 'claude-sessions'
sessions.mkdir()
assert not key.exists()
with linux_claude():
    for settings, store_mount in [('', []), (configured, store_mask)]:
        if settings:
            paths.CONFIG_FILE.write_text(settings)
        argv = sandbox.claude_confinement(['/fixture/claude'], checkout, (sessions,))
        assert argv[argv.index('--bind'):] == linux_mounts(checkout, *key_mask, *store_mount)
        if not settings:
            original = key.read_bytes()
            assert len(original) == 32 and key.stat().st_mode & 0o777 == 0o600
            assert db.schema_metadata()[secretstore.KEY_LOCAL] == '1'
        assert key.read_bytes() == original
assert store.stat().st_mode & 0o777 == 0o700
""")

    def test_unavailable_confinement_does_not_retry_claude_or_remint_a_missing_registered_key(self):
        self.run_secret_child(SECRET_LINUX_CLAUDE_FIXTURE + r"""
paths.CONFIG_FILE.write_text('[secrets]\nstore = ""\n')
checkout = linux_checkout(paths.WORKTREES_DIR)
with linux_claude(error='fixture: namespace unavailable') as (which, stream):
    assert_missing_bwrap(which, stream, checkout)
    assert not key.exists()

    result = run_claude(checkout)
    assert not result.ok and 'namespace unavailable' in result.error
    stream.assert_called_once()
    assert_mounts(stream, checkout, *key_mask)

    stream.reset_mock()
    key.unlink()
    result = run_claude(checkout)
    assert not result.ok and 'fingerprint_key_missing' in result.error
    stream.assert_not_called()
    assert not key.exists()
""")

    def test_unpreparable_store_mask_prevents_claude_launch(self):
        self.run_secret_child(SECRET_LINUX_CLAUDE_FIXTURE + r"""
store.mkdir()
store.chmod(0o755)
checkout = linux_checkout(paths.WORKTREES_DIR)
with linux_claude() as (_, stream):
    result = run_claude(checkout)
    assert not result.ok and 'could not prepare the Linux secret store mount' in result.error
    stream.assert_not_called()
    assert not key.exists() and not any(store.iterdir())
""")


class AuthorityLinuxFingerprintMigrationTests(_SecretChildCase):
    def test_retired_fallback_requires_a_committed_valid_store_key(self):
        self.run_secret_child(SECRET_LINUX_CLAUDE_FIXTURE + r"""
configured = paths.CONFIG_FILE.read_text()
paths.CONFIG_FILE.write_text('[secrets]\nstore = ""\n')
move_key = secretstore.fingerprint_key
original = move_key()
paths.CONFIG_FILE.write_text(configured)
settings = config.secret_settings()
ref = secretstore.Reference('pve_store', db.schema_metadata()['instance_tag'], secretstore.KEY_ITEM)
item, _ = secretstore.file_location(ref, settings)
checkout = linux_checkout(paths.WORKTREES_DIR)
lstat = Path.lstat

def launch_after_probe(retire):
    actions = [retire]

    def probe(path):
        info = lstat(path)
        if path == key and actions:
            actions.pop()()
        return info

    with mock.patch.object(Path, 'lstat', probe):
        result = run_claude(checkout)
    assert not actions and not key.exists()
    return result

with (linux_claude() as (_, stream),
      mock.patch.object(secretstore, 'fingerprint_key', side_effect=AssertionError('must not remint'))):
    blocked = launch_after_probe(key.unlink)
    assert not blocked.ok and 'fingerprint_key_missing' in blocked.error
    assert secretstore.KEY_REF not in db.schema_metadata() and not item.exists()
    stream.assert_not_called()

    secretstore.write_file(key, key.parent, original)
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
