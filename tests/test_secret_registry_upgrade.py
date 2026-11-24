import subprocess
import tempfile
import unittest
from pathlib import Path
from textwrap import indent

from arete import api_check
from tests.subprocess_helpers import CHILD_HANG_TIMEOUT
from tests.test_behavior import SECRET_HTTP_FIXTURE

BOOTSTRAP = """
from arete.store import db
from unittest import mock

chain = [migration for migration in db.migrations.load() if migration.n <= 26]
with mock.patch.object(db.migrations, 'load', return_value=chain):
    db.migrate()


def assert_schema(version):
    with db.reader() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == version
        assert conn.execute('SELECT MAX(n) FROM schema_migrations').fetchone()[0] == version
        exists = conn.execute("SELECT 1 FROM sqlite_schema WHERE name = 'secrets'").fetchone()
        assert bool(exists) == (version >= 27)
"""


class SecretRegistryUpgradeTests(unittest.TestCase):
    def run_schema_26(self, entry):
        with tempfile.TemporaryDirectory(prefix="arete-secret-upgrade-", dir=Path("/tmp").resolve()) as directory:
            root = Path(directory)
            env = {**api_check.isolated_env(root), "ARETE_DRIVE_FLUSH": "0"}
            (root / "config/config.toml").write_text('[secrets]\nstore = "pve_store"\nstore_dir = "../store"\n')
            script = "from arete import api_check\nwith api_check.offline():\n" + indent(
                BOOTSTRAP + SECRET_HTTP_FIXTURE + "\nassert_schema(26)\n" + entry, "    ")
            done = subprocess.run(api_check.child_command(api_check.SOURCE, script), cwd=root, env=env,
                                  capture_output=True, text=True, timeout=CHILD_HANG_TIMEOUT)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_first_direct_registry_get_upgrades_schema_26(self):
        self.run_schema_26("""
status, headers, result = request('GET', root, token=bearer)
assert status == 200, result
assert_schema(27)
contract.validate(result, contract.BY_NAME['secret.list']['result'])
assert result['secrets'] == []
assert result['seq'] == headers['Arete-Seq']
""")

    def test_first_registry_batch_upgrades_and_shares_one_snapshot(self):
        self.run_schema_26("""
from contextlib import closing
from arete import api, events, paths

route = '/api/v1/batch'
payload = json.dumps({'requests': [{'operation': op} for op in ('meta.get', 'secret.list', 'meta.get')]}).encode()

class ScratchHandler(dashboard.Handler):
    verify_controls = SimpleNamespace(poll=lambda *_: None)

status, _, refused = request('POST', route, payload, handler=ScratchHandler)
assert (status, refused['code']) == (401, 'unauthenticated'), refused
assert_schema(26)

db.enable_wal()
secret_read = api.secret_read

def read_during_commit(*args):
    result = secret_read(*args)
    with closing(db.connect_db(paths.DB_FILE, timeout=1)) as writer, writer:
        writer.execute('BEGIN IMMEDIATE')
        events.emit('fixture.changed', 'fixture:registry', cls='derived', data={}, connection=writer)
    return result

with mock.patch.object(api, 'secret_read', side_effect=read_during_commit):
    status, headers, result = request('POST', route, payload, token=bearer)
assert status == 200, result
assert_schema(27)
contract.validate(result, contract.BY_NAME['batch.run']['result'])
first, registry, last = result['results']
assert registry['secrets'] == []
assert first['schema']['version'] == 27
assert first == last
assert first['stream']['head'] == first['seq'] == registry['seq'] == result['seq'] == headers['Arete-Seq']
with db.reader() as conn:
    assert events.metadata(conn)['head'] != result['seq']
""")
