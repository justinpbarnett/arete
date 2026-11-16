
import json, sys, time
from pathlib import Path
from arete import paths
from arete.store import _rows as _rows_store
from arete.store import approvals as approvals_store
from arete.store import asks as asks_store
from arete.store import chats as chats_store
from arete.store import db as foundation
from arete.store import devices as devices_store
from arete.store import incidents as incidents_store
from arete.store import insights as insights_store
from arete.store import loops as loops_store
from arete.store import push as push_store
from arete.store import runs as runs_store
from arete.store import sources as sources_store
from arete.store import tasks as tasks_store
from arete.store import triage as triage_store
from tests.harness import inherited_env, state_env
root = Path(sys.argv[1])
foundation.migrate()
def result(**fields):
    print("BEHAVIOR_RESULT=" + json.dumps(fields))
