from tests.test_behavior import SECRET_HTTP_FIXTURE, _SecretChildCase


class SecretRegistryUpgradeTests(_SecretChildCase):
    def run_schema_26(self, entry):
        self.run_secret_child(SECRET_HTTP_FIXTURE + '\nassert_schema(26)\n' + entry, schema=26)

    def test_first_direct_registry_get_upgrades_schema_26(self):
        self.run_schema_26("""
headers, result = request('GET', root, token=bearer)
assert_schema(db.WORKSPACE_CONTRACT_SCHEMA)
assert_shape('secret.list', result)
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

request('POST', route, payload, handler=ScratchHandler, status=401, code='unauthenticated')
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
    headers, result = request('POST', route, payload, token=bearer)
assert_schema(db.WORKSPACE_CONTRACT_SCHEMA)
assert_shape('batch.run', result)
first, registry, last = result['results']
assert registry['secrets'] == []
assert first['schema']['version'] == db.WORKSPACE_CONTRACT_SCHEMA
assert first == last
assert first['stream']['head'] == first['seq'] == registry['seq'] == result['seq'] == headers['Arete-Seq']
with db.reader() as conn:
    assert events.metadata(conn)['head'] != result['seq']
""")
