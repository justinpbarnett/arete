import sys
from importlib.util import find_spec
from pathlib import Path


def main() -> int:
    checked = False
    if sys.argv[1:2] == ["serve"]:
        from . import local_paths, serve_guard

        if (serve_guard.is_serve_start(sys.argv[1:]) and (package := find_spec("arete"))
                and package.submodule_search_locations):
            checkout = Path(package.submodule_search_locations[0]).resolve().parent
            home = Path.home()
            _, state, isolated = local_paths.local_dirs(home)
            serve_guard.startup(state, checkout, local_paths.log_dir(home, state, isolated))
            sys.pycache_prefix = "/dev/null"
            sys.dont_write_bytecode = True
            checked = True

    from arete import cli

    if checked:
        cli._serve_start_checked = True
    return cli.main()
