import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def require(value, message):
    if not value:
        raise AssertionError(message)


def git(root, *args, check=True):
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=check)
    return result.stdout.strip()


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def commit(root, message):
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


ORIGINAL = 'def value():\n    return "original"\n\nANCHOR = "retained"\n'
EDITED = ORIGINAL.replace('"original"', '"branch"')
MOVED = 'from new import value\n\nANCHOR = "retained"\n'
NEW = 'STRUCTURE = "base"\n\ndef value():\n    return "original"\n'
COMBINED = NEW.replace('"original"', '"branch"')


def prepare(root, branch_moved=False, denied_file=""):
    git(root, "init", "-q", "-b", "start")
    git(root, "config", "user.name", "Resolver Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", "/dev/null")
    write(root, "old.py", ORIGINAL)
    write(root, "unrelated.txt", "unchanged\n")
    ancestor = commit(root, "ancestor")
    git(root, "checkout", "-qb", "branch")
    write(root, "old.py", MOVED if branch_moved else EDITED)
    if branch_moved:
        write(root, "new.py", NEW.replace('"base"', '"branch"'))
    branch = commit(root, "branch work")
    git(root, "checkout", "-qb", "incoming", ancestor)
    write(root, "old.py", ORIGINAL.replace('"original"', '"base"') if branch_moved else MOVED)
    if not branch_moved:
        write(root, "new.py", NEW)
    if denied_file:
        write(root, denied_file, "incoming only\n")
    incoming = commit(root, "incoming work")
    git(root, "checkout", "-q", "branch")
    result = subprocess.run(["git", "merge", "--no-commit", "--no-ff", incoming], cwd=root,
                            text=True, capture_output=True)
    require(result.returncode == 1, f"expected a real conflict: {result.stdout} {result.stderr}")
    return ancestor, branch, incoming


class Resolver:
    def __init__(self, root, edits):
        self.root = root
        self.edits = edits
        self.calls = 0
        self.sessions = {}
        self.repo = SimpleNamespace(name="fixture")
        self.owner_note = "Offline deterministic resolver."

    def settings(self, role):
        return SimpleNamespace(tries=1)

    def agent(self, name, text, cwd, **kwargs):
        self.calls += 1
        require(cwd == self.root, "resolver cwd differs")
        self.root.parent.joinpath("prompt.md").write_text(text)
        require("git diff --name-only HEAD...MERGE_HEAD" in text, "missing incoming discovery command")
        require("only when the branch's work must follow code the base moved" in text, "missing scope limit")
        require("Never run git commands that write" in text, "missing git restriction")
        require("Never edit `.agents/`, CI workflows, or env files" in text, "missing protection")
        for path, content in self.edits.items():
            write(self.root, path, content)
        return SimpleNamespace(ok=True, structured={"resolved": True, "summary": "Preserved both sides."}, text="")


def exercise(folder, name, branch_moved=False, explicit=False, extra="", deny=(), prefix="", protected=False):
    from arete import config, pipeline, tracker, workspace

    case = folder / name
    root = case / "repo"
    root.mkdir(parents=True)
    ancestor, branch, incoming = prepare(root, branch_moved, extra if deny and not protected else "")
    own = git(root, "diff", "--name-only", f"{incoming}...HEAD").splitlines()
    incoming_paths = git(root, "diff", "--name-only", f"HEAD...{incoming}").splitlines()
    require(("new.py" in own) == branch_moved, "new file on wrong side of branch diff")
    require(("new.py" in incoming_paths) != branch_moved, "new file on wrong side of incoming diff")
    print(f"{name}: branch paths={own}; incoming paths={incoming_paths}")
    combined = NEW.replace('"base"', '"branch"').replace('"original"', '"base"') if branch_moved else COMBINED
    edits = {"old.py": MOVED, "new.py": combined}
    if extra:
        edits[extra] = "resolver changed this\n"
    resolver = Resolver(root, edits)
    checkout = workspace.Checkout(prefix, "fixture/repo", root, root, "main",
                                  base_commit=ancestor if explicit else incoming)
    layer = config.RepoLayer(case, deny=list(deny))
    conflicts = pipeline.unmerged_paths(root)
    require("old.py" in conflicts, f"missing old-module conflict: {conflicts}")
    incoming_arg = ("kept-edits", incoming) if explicit else None
    try:
        pipeline.resolve_conflicts(resolver, checkout, conflicts, layer,
                                   tracker.Ticket("564", "564", "Carry work across a move"), incoming_arg)
    except workspace.RunFailed as error:
        require(protected or extra, f"unexpected refusal: {error}")
        message = str(error)
        expected = "conflicts in protected paths" if protected else "the resolver edited files outside the conflict"
        require(expected in message, f"wrong refusal: {message}")
        require(("old.py" if protected else extra) in message, "refusal omitted path")
        require(git(root, "rev-parse", "HEAD") == branch, "refusal created a merge commit")
        require(resolver.calls == (0 if protected else 1), "wrong resolver call count")
        print(f"{name}: RunFailed: {message}; agent calls={resolver.calls}; HEAD unchanged={branch}")
        return
    require(not protected and not extra, "expected refusal, merge succeeded")
    require(resolver.calls == 1, "wrong resolver call count")
    parents = git(root, "show", "-s", "--format=%P", "HEAD").split()
    require(parents == [branch, incoming], f"incorrect merge parents: {parents}")
    require(not pipeline.unmerged_paths(root), "unresolved paths remain")
    require(not git(root, "status", "--porcelain"), "merge left dirty files")
    require((root / "old.py").read_text() == MOVED, "old module lost base structure")
    content = (root / "new.py").read_text()
    require(content == combined, "new module lost either side's work")
    namespace = {}
    exec(compile(content, "new.py", "exec"), namespace)
    require(namespace["value"]() == ("base" if branch_moved else "branch"), "wrong function behavior")
    require(namespace["STRUCTURE"] == ("branch" if branch_moved else "base"), "wrong module structure")
    prompt = (case / "prompt.md").read_text()
    own_list = prompt.split("## Issue", 1)[0].split("Files this branch already changed", 1)[1]
    require(("- `new.py`" in own_list) == branch_moved, "incoming list leaked into branch list")
    print(f"{name}: merge parents={parents}; changed paths={git(root, 'diff', '--name-only', branch, 'HEAD')}")
    print(f"new.py:\n{content}old.py:\n{MOVED}agent calls={resolver.calls}; clean merge; prompt={case / 'prompt.md'}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=["base-moved", "unrelated-edit", "protected", "branch-moved"])
    args = parser.parse_args()
    sandbox = REPO / ".arete-sandbox"
    state = os.environ.get("ARETE_STATE_DIR", "")
    require(state and Path(state).resolve().is_relative_to(sandbox.resolve()),
            "invoke through the supplied verification sandbox")
    require(not sandbox.is_symlink(), "linked sandbox refused")
    root = sandbox / "resolve-fixture"
    require(not root.is_symlink(), "linked fixture root refused")
    root.mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=args.scenario + "-", dir=root))
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    if args.scenario == "base-moved":
        exercise(folder, "default")
        exercise(folder, "explicit-incoming", explicit=True)
    elif args.scenario == "unrelated-edit":
        exercise(folder, "unrelated", extra="unrelated.txt")
    elif args.scenario == "protected":
        exercise(folder, "protected-conflict", deny=["old.py"], protected=True)
        exercise(folder, "protected-incoming", extra="denied.txt", deny=["denied.txt"])
        exercise(folder, "protected-prefixed-incoming", extra="denied.txt", deny=["component/denied.txt"],
                 prefix="component")
    else:
        exercise(folder, "branch-moved", branch_moved=True)
    print(f"PASS {args.scenario}: {folder}")


if __name__ == "__main__":
    main()
