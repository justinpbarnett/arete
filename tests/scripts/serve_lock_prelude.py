
import json
import sys
from pathlib import Path

from arete import state

root = Path(sys.argv[1])
state.migrate()
def result(**fields):
    print("BEHAVIOR_RESULT=" + json.dumps(fields))
