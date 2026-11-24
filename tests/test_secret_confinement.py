from tests.test_behavior import _SecretChildCase


class AuthorityClaudeConfinementTests(_SecretChildCase):
    def test_linux_masks_fingerprint_state_without_requiring_a_store(self):
        self.run_secret_child(r"""
import subprocess

from arete import accounts, claude, sandbox

paths.CONFIG_FILE.write_text('[secrets]\nstore = ""\n')
state = paths.STATE_DIR.resolve()
key = paths.FINGERPRINT_KEY_FILE
checkout = state / 'worktrees' / 'checkout'
checkout.mkdir(parents=True)
extra = state / 'additional'
extra.mkdir()
ordinary = checkout / 'control.txt'
ordinary.write_text('ordinary fixture')
options = dict(auth=accounts.Auth(), tools=['Bash'], passing=[], persist=False, add_dirs=[extra])
fixture = subprocess.CompletedProcess([], 0, '{"type":"result","result":"fixture"}', '')
with (mock.patch.object(sandbox.sys, 'platform', 'linux'),
      mock.patch.object(sandbox.shutil, 'which', return_value=None) as which,
      mock.patch.object(claude, 'binary', return_value='/fixture/claude'),
      mock.patch.object(claude.child, 'stream', return_value=fixture) as stream):
    refused = claude.run('fixture', checkout, **options)
    assert not refused.ok and 'requires bubblewrap' in refused.error
    stream.assert_not_called()
    which.return_value = '/usr/bin/bwrap'
    for present in (False, True):
        with case.subTest(key_present=present):
            if present:
                key.write_bytes(b'F' * 32)
                key.chmod(0o600)
            assert claude.run('fixture', checkout, **options).ok
            argv = stream.call_args.args[0]
            assert argv[0] == '/usr/bin/bwrap'
            assert argv[argv.index('--tmpfs'):argv.index('--') + 2] == [
                '--tmpfs', str(state), '--bind', str(checkout), str(checkout), '--bind', str(extra), str(extra),
                '--remount-ro', str(state), '--chdir', str(checkout), '--', '/fixture/claude',
            ]
            assert key.exists() == present
            assert ordinary.read_text() == 'ordinary fixture'
    assert key.read_bytes() == b'F' * 32
""")

    def test_linux_keeps_store_mask_during_fallback_transition(self):
        self.run_secret_child(r"""
from arete import sandbox

store = config.secret_settings().store_dir
state = paths.STATE_DIR.resolve()
checkout = paths.WORKTREES_DIR.resolve() / 'checkout'
checkout.mkdir()
key = paths.FINGERPRINT_KEY_FILE
key.write_bytes(b'F' * 32)
key.chmod(0o600)
with (mock.patch.object(sandbox.sys, 'platform', 'linux'),
      mock.patch.object(sandbox.shutil, 'which', return_value='/usr/bin/bwrap')):
    argv = sandbox.claude_confinement(['/fixture/claude'], checkout)
assert argv[argv.index('--tmpfs'):] == [
    '--tmpfs', str(state), '--remount-ro', str(state), '--tmpfs', str(store), '--remount-ro', str(store),
    '--chdir', str(checkout), '--', '/fixture/claude',
]
assert (store.stat().st_mode & 0o777) == 0o700
assert key.read_bytes() == b'F' * 32
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
