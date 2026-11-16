
import json, os, sys
from unittest import mock
from arete import serve
from arete.store import db as foundation
foundation.migrate()
os.chdir(sys.argv[1])
server = serve.Server()
server.code, server.code_seen = frozenset(), serve.code_stamp()
notes = []
server.say = notes.append
with mock.patch.object(serve.os, "execv", side_effect=AssertionError("restarted onto code that cannot load")):
    server.load_new_code()
print(json.dumps(notes))
