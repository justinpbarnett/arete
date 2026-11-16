
import json, os, secrets, subprocess, sys, time
from pathlib import Path
t = Path(sys.argv[1])
from arete import pipeline, sandbox, scratch, paths
sandbox.AUTHORITY = t / "authority"
