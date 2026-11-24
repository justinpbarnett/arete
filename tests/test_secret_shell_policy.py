from tests.test_behavior import SECRET_GUARD_FIXTURE, _SecretChildCase


class AuthoritySecretShellPolicyTests(_SecretChildCase):
    def test_searches_comments_and_literal_data_are_admitted(self):
        self.run_secret_child(SECRET_GUARD_FIXTURE + r"""
import subprocess

arithmetic = {
    "printf '%s\\n' $((((2 << 3) >> 1) + $((1 << 1))))": '10\n',
    "(( counter = (2 << 3) >> 1 )); printf '%s\\n' \"$counter\"": '8\n',
}
for command, output in arithmetic.items():
    result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', command],
                            env={'HOME': str(Path.home())}, capture_output=True, text=True, check=True, timeout=5)
    assert result.stdout == output, command

for command in (
    "sed -n '/security/p' README.md",
    "bash -lc \"sed -n '/security/p' README.md\"",
    "printf '%s\\n' ';' 'security' '$(security)' '`security`'",
    r"printf '%s\n' \; security",
    "printf '%s\\n' ok # ; security -i",
    "cat <<'TEXT'\nsecurity -i\n$(security)\nTEXT",
    "cat <<-TEXT\n\tsecurity -i\n\tTEXT\nprintf '%s\\n' ok",
    "cat <<< 'security -i'",
    r"$'pr\x69ntf' '%s\n' $'\073' security",
    "$'security' $'verify-cert' -c public.crt",
    'security find-certificate -a -p',
    '/usr/bin/security -q find-certificate -c fixture -p',
    "printf '%s\\n' \"$'security'\"",
    "printf '%s\\n' \"$((2 << 3))\"",
    *arithmetic,
    "printf '%s\\n' \"$((2 << $(printf 3)))\"",
    "cat <<TEXT\n$((2 << 3))\nTEXT",
):
    with case.subTest(command=command):
        assert check('Bash', command=command) == '', command
        assert codex_check(command) == '', command
        with (mock.patch.object(pi_shell.scratch, 'spawn', side_effect=AssertionError('shell must not run')),
              mock.patch.object(pi_shell.Shell, 'profile', side_effect=RuntimeError('admitted'))):
            with case.assertRaisesRegex(RuntimeError, 'admitted'):
                shell.execute({'command': command}, Event())
assert codex.KeychainShell(r"pre$'it\'s\040ok\q'post").word(0) == "preit's ok\\qpost"
""")

    def test_keychain_command_positions_remain_denied(self):
        self.run_secret_child(SECRET_GUARD_FIXTURE + r"""
for command in (
    'security',
    'security -i',
    'security find-generic-password -s ' + ref.service,
    "$'security' -i",
    r"env $'/usr/bin/\163ecu\x72'ity $'\x2di'",
    r"$'\u0073ecurity\0text'",
    "printf '%s\\n' 'text'; 'security' -i",
    'printf "%s\\n" "$(security -i)"',
    'printf "%s\\n" "$((2 << $(security -i)))"',
    'printf "%s\\n" "$((security -i); printf 2)"',
    "cat <<'TEXT'\nsecurity is just data here\nTEXT\nsecurity -i",
    "bash <<'SCRIPT'\nsecurity -i\nSCRIPT",
):
    with case.subTest(command=command):
        assert check('Bash', command=command) == 'deny', command
        assert 'security keychain items' in codex_check(command), command
        with (mock.patch.object(pi_shell.scratch, 'spawn', side_effect=AssertionError('shell must not run')),
              mock.patch.object(pi_shell.Shell, 'profile', side_effect=AssertionError('denial required'))):
            with case.assertRaisesRegex(ValueError, 'security keychain items'):
                shell.execute({'command': command}, Event())
assert codex.git_write('git status', []) == ''
assert codex.git_write('git commit', []) == 'git commit'
assert check('Bash', command=f'cat {path}') == 'deny'
""")

    def test_generated_executable_grants_exclude_protected_paths(self):
        self.run_secret_child(r"""
import shlex
import subprocess
import tomllib

from arete import codex, guard

store = config.secret_settings().store_dir
work = Path.cwd() / 'workspace'
work.mkdir()

def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o700)
    return path

nested = executable(store / 'global' / 'DEMO_KEY')
ordinary = executable(store.with_name('store-public') / 'tool')
closed = executable(paths.CONFIG_DIR / 'tools' / 'tool')
exact = executable(work / 'private-tool')
into_store = work / 'into-store'
into_store.symlink_to(nested)
from_store = store / 'from-store'
from_store.symlink_to(ordinary)
blocked = (nested, closed, exact, into_store, from_store)
rules = [f'Bash({shlex.quote(str(path))})' for path in (*blocked, ordinary)]
assert codex.executables(rules, [store, paths.CONFIG_DIR, exact]) == {p: 'read' for p in codex.both(ordinary)}
with mock.patch.object(codex.util, 'sh', return_value=subprocess.CompletedProcess([], 1)):
    argv = codex.profile(work, [], True, [exact], rules, floor=guard.floor('', work, ()))
access = tomllib.loads(argv[-1])['permissions'][codex.WRITE_PROFILE]['filesystem']
assert all(access[p] == 'deny' for p in (*codex.both(store), *codex.both(exact)))
assert all(access.get(str(path)) != 'read' for path in blocked)
assert all(access[p] == 'read' for p in codex.both(ordinary))
""")
