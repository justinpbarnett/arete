from tests.test_behavior import SECRET_LINUX_CLAUDE_FIXTURE, _SecretChildCase


class AuthorityLinuxFingerprintTests(_SecretChildCase):
    def test_fresh_routed_login_requires_confinement_and_enrolls_after_the_attempt(self):
        for confinement in ("usable", "missing", "refused"):
            with self.subTest(confinement=confinement):
                self.run_secret_child(f"confinement = {confinement!r}\n" + SECRET_LINUX_CLAUDE_FIXTURE + r"""
import json

from arete import routing, runner

configured = paths.CONFIG_FILE.read_text()
paths.CONFIG_FILE.write_text('[secrets]\nstore = ""\n')
checkout = linux_checkout(paths.WORKTREES_DIR)
auth = accounts.Auth('subscription', 'claude', identity='fixture-login', provider='anthropic')
model = 'sonnet'
candidate = routing.Candidate(0, model, 'claude', 'anthropic', 'cli_login', 'free', '', 0, True, '', (), None, 'ready', '')
picked = runner.Picked(candidate, auth, None, model, '', {auth}, None)

def active(cmd, cwd, prompt, env, timeout, on_line):
    assert cmd[0] == bwrap and not key.exists() and not store.exists()
    if confinement == 'refused':
        return subprocess.CompletedProcess(cmd, 1, '', 'bwrap: fixture namespace unavailable')
    assert cwd == str(checkout) and prompt == 'fixture prompt'
    assert json.loads(cmd[cmd.index('--settings') + 1])['hooks']['PreToolUse']
    session = cmd[cmd.index('--session-id') + 1]
    on_line(json.dumps({'type': 'system', 'session_id': session}) + '\n')
    with case.assertRaisesRegex(secretstore.StoreError, 'Retry credential publication'):
        secretstore.fingerprint_key()
    paths.CONFIG_FILE.write_text(configured)
    with case.assertRaisesRegex(secretstore.StoreError, 'Retry credential publication'):
        secretstore.put('global', 'DEMO', 'synthetic-value', 'api_key', 'hand')
    assert not key.exists() and not store.exists()
    return subprocess.CompletedProcess(cmd, 0, json.dumps({
        'type': 'result', 'result': 'fixture', 'session_id': session,
    }), '')

with (linux_claude() as (which, stream), mock.patch.object(runner, 'pick', return_value=(picked, [])),
      mock.patch.object(runner.util, 'github_token', return_value='')):
    which.return_value = None if confinement == 'missing' else bwrap
    stream.side_effect = active
    result = runner.run('fixture prompt', checkout, runner='claude', model=model, cfg=config.Config(), tools=['Read'])
    if confinement == 'missing':
        assert not result.ok and 'requires bubblewrap' in result.error
        stream.assert_not_called()
    else:
        stream.assert_called_once()
        assert_mounts(stream, checkout)
        if confinement == 'refused':
            assert not result.ok and 'namespace unavailable' in result.error
        else:
            assert result.ok and result.text == 'fixture', result.error
            argv = stream.call_args.args[0]
            assert result.session.id == argv[argv.index('--session-id') + 1]
    with db.reader() as c:
        account, = c.execute("SELECT id, holder FROM accounts")
        assert account['id'] == 'anthropic:subscription' and account['holder'].startswith('u:')
    if confinement == 'usable':
        assert secretstore.KEY_REF in db.schema_metadata() and not key.exists()
        assert secretstore.put('global', 'DEMO', 'synthetic-value', 'api_key', 'hand')['status'] == 'set'
    else:
        assert secretstore.KEY_LOCAL in db.schema_metadata() and key.exists() and not store.exists()
""")

    def test_busy_account_enrollment_retries_after_the_last_worker_exits(self):
        self.run_secret_child(SECRET_LINUX_CLAUDE_FIXTURE + r"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

paths.CONFIG_FILE.write_text('[secrets]\nstore = ""\n')
auth = accounts.Auth('subscription', 'claude', identity='fixture-wait', provider='anthropic')
activation = secretstore.credential_activation
waiting = Event()

@contextmanager
def observed(**options):
    if options.get('wait'):
        waiting.set()
    with activation(**options):
        yield

with linux_claude(), mock.patch.object(secretstore, 'credential_activation', observed), ThreadPoolExecutor(1) as pool:
    with activation():
        assert accounts.record_resolved(auth)
        retry = pool.submit(accounts.record_resolved, auth, wait_for_activation=True)
        assert waiting.wait(3)
        with db.tx(timeout=1) as c:
            assert c.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0
    assert not retry.result(timeout=3)
    with db.reader() as c:
        assert [r[0] for r in c.execute("SELECT id FROM accounts")] == ['anthropic:subscription']
""")

    def test_kernel_refusal_does_not_retry_an_existing_key_without_a_namespace(self):
        self.run_secret_child(SECRET_LINUX_CLAUDE_FIXTURE + r"""
paths.CONFIG_FILE.write_text('[secrets]\nstore = ""\n')
checkout = linux_checkout(paths.WORKTREES_DIR)
secretstore.fingerprint_key()
with linux_claude(error='fixture: namespace unavailable') as (_, stream):
    result = run_claude(checkout)
    assert not result.ok and 'namespace unavailable' in result.error
    stream.assert_called_once()
    assert_mounts(stream, checkout, *key_mask)
""")


class AuthorityLinuxCheckoutMountTests(_SecretChildCase):
    def test_shared_memory_checkout_mount_preserves_fresh_devices_and_credential_masks(self):
        self.run_secret_child(SECRET_LINUX_CLAUDE_FIXTURE + r"""
checkout = Path('/dev/shm') / Path.cwd().name / 'checkout'
secretstore.write_file(key, key.parent, b'F' * 32)
with linux_claude() as (_, stream):
    assert run_claude(checkout).ok
    stream.assert_called_once()
    checkout_mount = ['--bind', str(checkout), str(checkout)]
    assert_mounts(stream, checkout, *checkout_mount, *key_mask, *store_mask)
""")

    def test_linked_and_secondary_checkouts_with_redirected_session_and_hook_state(self):
        self.run_secret_child(SECRET_LINUX_CLAUDE_FIXTURE + r"""
import json
from io import StringIO

from arete import tool_events, workspace
from arete.store import workspaces

shm = Path.cwd().resolve()
source, secondary, profile = (shm / name for name in ('source', 'secondary', 'profile'))
checkout = paths.WORKTREES_DIR / 'linked'

def git(cwd, *argv):
    return subprocess.run(['git', '-C', str(cwd), *argv], check=True, capture_output=True, text=True).stdout

for repo, text in ((source, 'original'), (secondary, 'secondary checkout')):
    repo.mkdir()
    git(repo, 'init', '-q')
    (repo / 'notes.txt').write_text(text + '\n')
    git(repo, 'add', 'notes.txt')
    git(repo, '-c', 'user.email=fixture@example.invalid', '-c', 'user.name=Fixture', 'commit', '-qm', 'fixture')
linked = workspace.Checkout('', 'fixture/source', source, checkout, '')
workspace.add_worktree(linked, 'fixture-linked', 'HEAD')
(checkout / 'notes.txt').write_text('linked edit\n')
secretstore.write_file(key, key.parent, b'F' * 32)

def binds(*roots):
    return [arg for p in sorted(roots) for arg in ('--bind', str(p), str(p))]

def worker(cmd, cwd, prompt, env, timeout, on_line):
    masks = [*binds(checkout, secondary, source / '.git', profile, state), *key_mask, *store_mask]
    assert cmd[cmd.index('--bind'):cmd.index('--') + 2] == linux_mounts(checkout, *masks)
    assert cmd[cmd.index('--add-dir') + 1] == str(secondary)
    assert git(cwd, 'status', '--short') == ' M notes.txt\n'
    assert '+linked edit' in git(cwd, 'diff', '--', 'notes.txt')
    assert git(secondary, 'show', 'HEAD:notes.txt') == 'secondary checkout\n'

    resumed = '--resume' in cmd
    session = cmd[cmd.index('--resume' if resumed else '--session-id') + 1]
    assert Path(env['CLAUDE_CONFIG_DIR']) == profile and profile.is_dir()
    project = profile / 'projects' / 'fixture'
    project.mkdir(parents=True, exist_ok=True)
    transcript = project / (session + '.jsonl')
    if resumed:
        assert json.loads(transcript.read_text()) == {'session_id': session}
    else:
        transcript.write_text(json.dumps({'session_id': session}) + '\n')
    payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Skill', 'tool_input': {'skill': 'fixture'},
               'session_id': session, 'cwd': cwd}
    assert Path(env['ARETE_STATE_DIR']) == paths.EVENTS_FILE.parent
    with mock.patch.object(tool_events.sys, 'stdin', StringIO(json.dumps(payload))):
        assert tool_events.main() == 0
    result = json.dumps({'type': 'result', 'result': 'fixture', 'session_id': session})
    on_line(result + '\n')
    return subprocess.CompletedProcess(cmd, 0, result, '')

alias = shm / 'secondary-link'
alias.symlink_to(secondary, target_is_directory=True)
options = {'auth': accounts.Auth(), 'passing': [], 'add_dirs': [alias],
           'extra_env': {'CLAUDE_CONFIG_DIR': str(profile)}}
with mock.patch.object(sandbox, 'LINUX_SHM', shm), linux_claude() as (_, stream):
    # A Git link alone does not authorize an external mount.
    assert sandbox.linux_claude_mounts((checkout,), None) == binds(checkout, state)
    with case.assertRaisesRegex(sandbox.ConfinementUnavailable, 'shared device directory'):
        sandbox.linux_claude_mounts((shm,), None)
    workspaces.claim(checkout, 'fixture', '', '', checkouts=[linked])
    stream.side_effect = worker
    first = claude.run('fixture', checkout, **options)
    assert first.ok and first.session.id, first.error
    again = claude.run('fixture', checkout, resume=first.session, **options)
    assert again.ok and again.session.id == first.session.id, again.error
    assert stream.call_count == 2
    assert [e['session'] for e in tool_events.read_events()] == [first.session.id] * 2
""")

    def test_bare_and_linked_sources_restore_only_independently_authorized_metadata(self):
        self.run_secret_child(SECRET_LINUX_CLAUDE_FIXTURE + r"""
import json

from arete import workspace
from arete.store import workspaces

shm = Path.cwd().resolve()
main, bare, linked_source, profile = (shm / name for name in ('main', 'mirror.git', 'linked-source', 'profile'))
linked_checkout, bare_checkout = (paths.WORKTREES_DIR / name for name in ('linked', 'bare'))

def git(cwd, *argv):
    return subprocess.run(['git', '-C', str(cwd), *argv], check=True, capture_output=True, text=True).stdout

main.mkdir()
git(main, 'init', '-q')
(main / 'notes.txt').write_text('fixture\n')
git(main, 'add', 'notes.txt')
git(main, '-c', 'user.email=fixture@example.invalid', '-c', 'user.name=Fixture', 'commit', '-qm', 'fixture')
git(shm, 'clone', '-q', '--bare', str(main), str(bare))
workspace.add_worktree(workspace.Checkout('', 'fixture/main', main, linked_source, ''), 'fixture-source', 'HEAD')
for source, checkout in ((bare, bare_checkout), (linked_source, linked_checkout)):
    entry = workspace.Checkout('', 'fixture/source', source, checkout, '')
    workspace.add_worktree(entry, 'fixture-' + checkout.name, 'HEAD')
    workspaces.claim(checkout, 'fixture', '', '', checkouts=[entry])
secretstore.write_file(key, key.parent, b'F' * 32)

options = {'add_dirs': [linked_checkout], 'extra_env': {'CLAUDE_CONFIG_DIR': str(profile)}}

def check_mounts(stream, *metadata):
    binds = [arg for root in sorted((bare_checkout, linked_checkout, profile, state, *metadata))
             for arg in ('--bind', str(root), str(root))]
    assert_mounts(stream, bare_checkout, *binds, *key_mask, *store_mask)

with mock.patch.object(sandbox, 'LINUX_SHM', shm), linux_claude() as (_, stream):
    assert run_claude(bare_checkout, **options).ok
    check_mounts(stream, bare)

    with paths.CONFIG_FILE.open('a') as file:
        file.write(f'\n[[repo]]\nname = "fixture-main"\npath = {json.dumps(str(main))}\n')

    def worker(cmd, cwd, prompt, env, timeout, on_line):
        for checkout in (Path(cwd), Path(cmd[cmd.index('--add-dir') + 1])):
            assert git(checkout, 'status', '--short') == ''
            assert git(checkout, 'show', 'HEAD:notes.txt') == 'fixture\n'
        return subprocess.CompletedProcess(cmd, 0, '{"type":"result","result":"fixture"}', '')

    stream.side_effect = worker
    assert run_claude(bare_checkout, **options).ok
    check_mounts(stream, bare, main / '.git')
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
