
import json, sys, time
from pathlib import Path
from arete import paths, state
from tests.harness import inherited_env, state_env
root = Path(sys.argv[1])
state.migrate()
def result(**fields):
    print("BEHAVIOR_RESULT=" + json.dumps(fields))
