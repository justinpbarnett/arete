
import contextlib, json, os, shlex, shutil, subprocess, sys, urllib.request
from pathlib import Path
from arete import config, devices, gh, record, sessions, state, util
root = Path(sys.argv[1])
VALUE = "fake-clone-value-0001"
shim_dir, shim_log = root / "device-bin", root / "shim.log"
shim_dir.mkdir()
(shim_dir / "git").write_text('#!/bin/sh\n{ printf "%s\\037" "${ARETE_FAKE_CALL:-agent}"; '
    'if test -n "$ARETE_GIT_TOKEN"; then printf "token\\037"; else printf "\\037"; fi; '
    'for a in "$@"; do printf "%s\\037" "$a"; done; printf "\\036"; } >> "$ARETE_SHIM_LOG"\n'
    f'exec {shlex.quote(shutil.which("git"))} "$@"\n')
(shim_dir / "git").chmod(0o755)
device_path = f"{shim_dir}{os.pathsep}{os.environ['PATH']}"
def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()
hub, seed, here, home = root / "hub.git", root / "seed", root / "controller", root / "device"
home.mkdir()
subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(hub)], check=True)
git("clone", "-q", str(hub), str(seed), cwd=root)
(seed / "a.txt").write_text("one\n")
git("add", "-A", cwd=seed)
git("commit", "-q", "-m", "first", cwd=seed)
git("push", "-q", "origin", "HEAD:main", cwd=seed)
git("clone", "-q", str(hub), str(here), cwd=root)
(root / "app-key").write_text("not a key\n")
calls, stdins, pushes, asked = [], [], [], []
scenario = sys.argv[2]
agent_env = {"PATH": device_path, "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1", "ARETE_SHIM_LOG": str(shim_log)}
def dev(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, env=agent_env,
                          check=True, capture_output=True, text=True).stdout.strip()
class Reply:
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return json.dumps({"token": VALUE, "permissions": {"contents": "read"}, "repository_selection": "selected",
                           "repositories": [{"full_name": "octo/widgets"}]}).encode()
urllib.request.urlopen = lambda request, timeout=None: asked.append(json.loads(request.data)) or Reply()
gh.key_signature = lambda app, text: b"signature"
gh.default_branch = lambda repo: "main"
real_sh = util.sh
def sh(cmd, *a, **k):
    if not (isinstance(cmd, list) and cmd[:1] == ["ssh"]):
        if "push" in cmd:
            pushes.append(cmd)
            if scenario == "task-retry" and len(pushes) <= 2:
                return subprocess.CompletedProcess(cmd, 1, "", "fatal: network down")
        return real_sh(cmd, *a, **k)
    calls.append(cmd[-1])
    stdins.append(k.get("input") or "")
    if scenario == "bundle-fails" and "git bundle create" in cmd[-1]:
        return subprocess.CompletedProcess(cmd, 1, "", "fatal: the device went away")
    script = cmd[-1].replace("https://github.com/octo/widgets.git", hub.as_uri())
    script = script.replace("GIT_ALLOW_PROTOCOL=https", "GIT_ALLOW_PROTOCOL=https:file")
    env = {**agent_env, "ARETE_FAKE_CALL": str(len(calls) - 1)}
    return subprocess.run(["sh", "-c", script], cwd=home, env=env, input=k.get("input") or "", capture_output=True, text=True)
util.sh = sh
box = devices.Device("box", "ssh", host="dev@box.test", dir=str(home / "work"))
devices.get = lambda name: box
cfg = config.Config(repos=[config.Repo("widgets", here, github="octo/widgets")],
                    session_idle_hours=2 if scenario.startswith("task") else 0,
                    github_app=config.GithubApp("12345", "67890", root / "app-key"))
config.load = lambda path=None: cfg
def take_turn(spec, cfg, device, prompt, turn, resume):
    work = Path(spec.cwd)
    if scenario == "env-added-then-deleted":
        (work / ".env").write_text("SECRET=1\n")
        dev("add", "-f", ".env", cwd=work)
        dev("commit", "-q", "-m", "add env", cwd=work)
        dev("rm", "-q", ".env", cwd=work)
        dev("commit", "-q", "-m", "remove env", cwd=work)
    if scenario == "orphan-side-merged":
        dev("checkout", "-q", "--orphan", "side", cwd=work)
        dev("rm", "-rqf", ".", cwd=work)
        (work / ".github/workflows").mkdir(parents=True)
        (work / ".github/workflows/ci.yml").write_text("on: push\n")
        dev("add", "-f", ".github/workflows/ci.yml", cwd=work)
        dev("commit", "-q", "-m", "side ci", cwd=work)
        dev("rm", "-q", "-f", ".github/workflows/ci.yml", cwd=work)
        (work / "side.txt").write_text("side\n")
        dev("add", "side.txt", cwd=work)
        dev("commit", "-q", "-m", "side done", cwd=work)
        dev("checkout", "-q", "session/ses-remote", cwd=work)
        dev("merge", "-q", "--no-ff", "--no-commit", "--allow-unrelated-histories", "side", cwd=work)
        (work / "config").mkdir()
        (work / "config/.env.local").write_text("SECRET=2\n")
        dev("add", "-f", "config/.env.local", cwd=work)
        dev("commit", "-q", "-m", "merge side", cwd=work)
        dev("rm", "-q", "config/.env.local", cwd=work)
        dev("commit", "-q", "-m", "remove env local", cwd=work)
    (work / "notes.txt").write_text("from the device\n")
    return resume
sessions.take_turn = take_turn
pushes_when_asked, lease_exits = [], []
def ask_report(spec, cfg, device, turn, resume):
    pushes_when_asked.append(len(pushes))
    return {"summary": "did it", "checks": [{"name": "t", "result": "pass", "detail": ""}], "open_questions": []}, ""
real_lease = devices.lease
@contextlib.contextmanager
def lease(*a, **k):
    with real_lease(*a, **k) as device:
        yield device
    lease_exits.append(True)
if scenario.startswith("task"):
    sessions.ask_report = ask_report
    devices.lease = lease
if scenario == "local":
    local = sessions.repo_named("widgets", devices.LOCAL, cfg)
    spec = sessions.Spec("ses-remote", "hello", repo="widgets")
    sessions.workspace(spec, devices.LOCAL, local)
    print(json.dumps({"cwd": spec.cwd, "branch": spec.branch, "github": spec.github, "asked": asked, "calls": calls,
                      "worktrees": git("worktree", "list", "--porcelain", cwd=here),
                      "scratch": str(root / "worktrees")}))
    sys.exit(0)
repo = sessions.repo_named("widgets", box, cfg)
spec = sessions.Spec("ses-remote", "hello", device="box", repo="widgets", task=scenario.startswith("task"))
sessions.workspace(spec, box, repo)
sessions.save_spec(spec)
record.start("session", run_id=spec.id, lock=sessions.session_lock(spec.id), pid=None, device="box")
status = sessions.execute(spec.id)
tip = git("rev-parse", "session/ses-remote", cwd=Path(spec.cwd))
def ref_in(folder):
    return subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/heads/session/ses-remote"], cwd=folder,
                          capture_output=True, text=True).stdout.strip()
saved = sessions.load_spec(spec.id)
def device_config(*args):
    return subprocess.run(["git", "config", *args], cwd=spec.cwd, capture_output=True, text=True).stdout
shim_text = shim_log.read_text() if shim_log.exists() else ""
records = []
for text in shim_text.split("\036"):
    if text:
        fields = text.split("\037")[:-1]
        records.append({"call": fields[0], "token": fields[1] == "token", "args": fields[2:]})
device_leaks = [str(f.relative_to(home)) for f in home.rglob("*") if f.is_file() and VALUE.encode() in f.read_bytes()]
print(json.dumps({"status": status, "hub": ref_in(hub), "tip": tip, "asked": asked,
                  "records": records, "token_calls": [i for i, text in enumerate(stdins) if VALUE in text],
                  "in_scripts": [c for c in calls if VALUE in c], "device_leaks": device_leaks,
                  "helpers": device_config("--get-all", "credential.helper"),
                  "origin_url": device_config("--get", "remote.origin.url").strip(),
                  "home_files": [n for n in (".gitconfig", ".git-credentials") if (home / n).exists()],
                  "fetched": saved.fetched, "held": saved.held, "controller_branch": ref_in(here),
                  "tree": git("ls-tree", "-r", "--name-only", tip, cwd=Path(spec.cwd)).split(), "calls": calls,
                  "pushes": pushes, "pushed": saved.pushed, "unreturned": saved.unreturned,
                  "summary": state.run_get(spec.id)["summary"], "report": state.report(spec.id),
                  "pushes_when_asked": pushes_when_asked, "lease_exits": lease_exits,
                  "leaks": [str(f) for d in ("state", "logs") for f in (root / d).rglob("*")
                            if f.is_file() and VALUE.encode() in f.read_bytes()]}))
