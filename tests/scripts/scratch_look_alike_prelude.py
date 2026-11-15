
import sys
from pathlib import Path

t = Path(sys.argv[1])
from arete import scratch

scratch.AUTHORITY = t / "authority"
