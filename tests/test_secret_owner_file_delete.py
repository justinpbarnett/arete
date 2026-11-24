from tests.test_behavior import SECRET_HTTP_FIXTURE, SECRET_OWNER_FILE_FIXTURE, _SecretChildCase


class SecretOwnerFileDeleteTests(_SecretChildCase):
    def test_paired_owner_cannot_delete_owner_file_with_missing_fingerprint_key(self):
        self.run_secret_child(SECRET_HTTP_FIXTURE + SECRET_OWNER_FILE_FIXTURE + r"""
cookie = browser_session()
file, value, row = seed_owner_file(actor)
meta = db.schema_metadata()
ref = secretstore.parse_reference(meta['fingerprint_key_ref'], meta['instance_tag'])
key_file, _ = secretstore.file_location(ref, config.secret_settings())
key_file.unlink()

route = root + '/global/OWNED'
with (mock.patch.object(secretstore, 'lookup_key', wraps=secretstore.lookup_key) as lookup,
      mock.patch.object(secretstore, 'pending_journals', wraps=secretstore.pending_journals) as pending):
    request('DELETE', route, token=bearer, key='cli-removal', status=403, reason='presence_required')
    request('DELETE', route, cookie=cookie, key='owner-removal', status=409, code='secret_internal')
    lookup.assert_not_called()
    pending.assert_not_called()

assert file.read_bytes() == value.encode()
assert not key_file.exists()
assert secretstore.get('global', 'OWNED') == row
assert not secretstore.history('global', 'OWNED')
assert db.schema_metadata() == meta
""")
