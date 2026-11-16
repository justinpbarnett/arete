import json
import os
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from tests.harness import behavior_result, make_folders, run_script, scratch_env

MODES = "reset half-open stop timeout cut loop lost-refused lost-folder cleanup bytes watchdog contract local groups relative answered upload inspect".split()


def main():
    mode = sys.argv[1]
    assert mode in MODES, MODES
    sandbox = SOURCE / ".arete-sandbox"
    assert not sandbox.is_symlink()
    assert (sandbox / ".arete-owned").read_text().strip() == "arete-verification-scratch-v1"
    base = sandbox / "remote-turn-fixture"
    assert not base.is_symlink()
    base.mkdir(exist_ok=True)
    if mode == "inspect":
        saved = {str(p.parent): json.loads(p.read_text()) for p in sorted(base.glob("*/evidence.json"))}
        assert saved
        print("BEHAVIOR_RESULT=" + json.dumps(saved))
        return
    root = Path(tempfile.mkdtemp(prefix=mode + "-", dir=base))
    make_folders(root, "config", "state", "logs", "worktrees", "tmp", "home")
    env = scratch_env(root, ARETE_WORKTREES_DIR=str(root / "worktrees"), TMPDIR=str(root / "tmp"),
                      HOME=str(root / "home"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                      GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull)
    done = run_script("from tests.scripts.remote_turn import main; main()", root, str(root), mode, env=env, timeout=120)
    (root / "stdout.txt").write_text(done.stdout)
    (root / "stderr.txt").write_text(done.stderr)
    assert done.returncode == 0, f"{root}\n{done.stdout}\n{done.stderr}"
    evidence = behavior_result(done)
    evidence["evidence_dir"] = str(root)
    (root / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print("BEHAVIOR_RESULT=" + json.dumps(evidence))


if __name__ == "__main__":
    main()
