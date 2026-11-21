import os
import sys
from importlib.machinery import PathFinder
from importlib.util import module_from_spec, spec_from_file_location

directory = os.path.dirname(__file__)
sys.path.remove(directory)
source = os.path.join(os.path.dirname(os.path.dirname(directory)), "arete", "_test_stacks.py")
spec = spec_from_file_location("_arete_test_stacks", source)
stacks = module_from_spec(spec)
spec.loader.exec_module(stacks)
stacks.arm()

# Preserve the environment's own sitecustomize and the child's normal import paths.
spec = PathFinder.find_spec(__name__)
if spec is not None and spec.loader is not None:
    custom = module_from_spec(spec)
    sys.modules[__name__] = custom
    spec.loader.exec_module(custom)
