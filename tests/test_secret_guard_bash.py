from tests.test_behavior import SECRET_GUARD_FIXTURE, _SecretChildCase


class AuthoritySecretBashForTests(_SecretChildCase):
    def test_arithmetic_policy_and_output(self):
        self.run_secret_child(SECRET_GUARD_FIXTURE + r"""
import subprocess

header = 'for ((n=1; n<8; n=n<<1))'
body = '; do :; done'
prefixes = ('', 'time ', 'time -p ')
timings = (*prefixes, 'time -\\\np ')
standalone = '(( n = 1 << 3 ))'
allowed = {prefix + header + "; do printf '%s\\n' \"$n\"; done": '1\n2\n4\n' for prefix in timings}
allowed.update({prefix + standalone: '' for prefix in timings})
nested = header.replace('<<1', '<<$(security -i)') + body
malformed = (header[:-1] + body, standalone[:-2])
credential, unparsed = 'security keychain items', 'unparsed shell command'
cases = dict.fromkeys(allowed, '') | {
    **{prefix + nested: credential for prefix in prefixes},
    **{prefix + command: unparsed for prefix in timings for command in malformed},
    header.replace('for', "'for'", 1) + body: unparsed,
    'printf ' + header + body: unparsed,
    'time -p\\\n' + header + body: unparsed,
}
with (mock.patch.object(pi_shell.scratch, 'spawn', side_effect=AssertionError('policy checks must not spawn Bash')),
      mock.patch.object(pi_shell.Shell, 'profile', side_effect=RuntimeError('admitted'))):
    for command, reason in cases.items():
        case.assertEqual(check('Bash', command=command), 'deny' if reason else '', f'Claude: {command!r}')
        case.assertRegex(codex_check(command), reason or r'\A\Z', f'Codex: {command!r}')
        label = f'Pi: {command!r}'
        with case.assertRaisesRegex((ValueError, RuntimeError), reason or 'admitted', msg=label) as stopped:
            shell.execute({'command': command}, Event())
        case.assertIs(type(stopped.exception), ValueError if reason else RuntimeError, label)

for command, output in allowed.items():
    result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', command], cwd=work,
                            env={'HOME': str(Path.home())}, capture_output=True, text=True, timeout=5)
    case.assertEqual((result.returncode, result.stdout), (0, output), f'Bash: {command!r}; {result.stderr!r}')
""")
