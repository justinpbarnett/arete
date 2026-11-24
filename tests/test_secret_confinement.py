from tests.test_behavior import SECRET_LINUX_CLAUDE_FIXTURE, _SecretChildCase


class AuthorityClaudeConfinementTests(_SecretChildCase):
    def test_linux_unconfigured_startup_needs_confinement_only_with_a_fallback_key(self):
        self.run_secret_child(SECRET_LINUX_CLAUDE_FIXTURE + r"""
paths.CONFIG_FILE.write_text('[secrets]\nstore = ""\n')
checkout = linux_checkout()
extra = state / 'additional'
extra.mkdir()
ordinary, state_control = checkout / 'control.txt', state / 'control.txt'
ordinary.write_text('ordinary fixture')
state_control.write_text('unrelated state fixture')
options = dict(add_dirs=[extra], tools=['Bash'])
with linux_claude() as (which, stream):
    baseline = ['/fixture/claude']
    assert sandbox.claude_confinement(baseline, checkout, (extra,)) == baseline
    which.assert_not_called()
    assert not key.exists() and not store.exists()
    assert {secretstore.KEY_LOCAL, secretstore.KEY_REF}.isdisjoint(db.schema_metadata())

    original = b'F' * 32
    secretstore.write_file(key, key.parent, original)
    assert_missing_bwrap(which, stream, checkout, **options)
    assert run_claude(checkout, **options).ok
    stream.assert_called_once()
    assert_mounts(stream, checkout, *key_mask)
    assert ordinary.read_text() == 'ordinary fixture'
    assert state_control.read_text() == 'unrelated state fixture'
    assert key.read_bytes() == original and key.stat().st_mode & 0o777 == 0o600
""")

    def test_macos_profile_denies_fingerprint_even_before_it_exists(self):
        self.run_secret_child(r"""
from arete import sandbox

settings = paths.CONFIG_FILE.read_text()
store = config.secret_settings().store_dir
key = paths.FINGERPRINT_KEY_FILE
checkout = paths.STATE_DIR / 'worktrees' / 'checkout'
checkout.mkdir(parents=True)
ordinary = checkout / 'control.txt'
ordinary.write_text('ordinary fixture')
assert not key.exists()

with (mock.patch.object(sandbox, 'AUTHORITY', paths.CONFIG_DIR / 'fixture-authority'),
      mock.patch.object(sandbox, 'can_confine', return_value=True),
      mock.patch.object(sandbox, 'sandboxed', return_value=False)):
    for configured in (False, True):
        paths.CONFIG_FILE.write_text(settings if configured else '[secrets]\nstore = ""\n')
        argv = sandbox.protected_command(['/fixture/claude'], checkout)
        assert argv[0] == '/usr/bin/sandbox-exec' and argv[-1] == '/fixture/claude'
        profile = argv[argv.index('-p') + 1]
        hidden = {}
        for arg in argv:
            if arg.startswith('H') and '=' in arg:
                param, path = arg.split('=', 1)
                hidden[param] = Path(path)
                assert f'(deny file-read* file-write* file-link (subpath (param "{param}")))' in profile
        assert sandbox.real_path(key) in hidden.values()
        assert (store in hidden.values()) == configured
        assert not any(ordinary.resolve().is_relative_to(p) for p in hidden.values())
        assert '(allow default)' in profile and '(deny file-write* (subpath "/"))' not in profile

check = mock.Mock(return_value=0)
with (mock.patch.object(sandbox.ctypes, 'CDLL', return_value=mock.Mock(sandbox_check=check)),
      mock.patch.object(sandbox, 'authority_roots', return_value=(key,))):
    assert not sandbox.already_protects_authority()
check.assert_called_once()
assert check.call_args.args[-1].value.decode() == str(sandbox.real_path(key))
""")
