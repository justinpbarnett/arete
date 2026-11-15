
seed()
cdir = paths.CONFIG_DIR
(cdir / "loop-switches.json").write_text("{}")
(cdir / "pi.env").write_text("PI_KEY=1")
(cdir / "loops/nested").mkdir(parents=True)
(cdir / "loops/a.toml").write_text("name = 1")
(cdir / "loops/nested/ok.toml").write_text("ok = 1")
(cdir / "loops/secret.env").write_text("S=1")
(cdir / "loops/nested/x.env").write_text("S=1")
(cdir / "loops/verification-authority").mkdir()
(cdir / "loops/verification-authority/token").write_text("T")
(root / "outside.txt").write_text("OUTSIDE-CONTENT")
(cdir / "loops/link.txt").symlink_to(root / "outside.txt")
layers = cdir / "layers"
layers.mkdir()
git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.invalid", "-c", "commit.gpgsign=false", *a],
                                cwd=layers, check=True, capture_output=True)
git("init", "-q", "-b", "main")
(layers / "a.txt").write_text("x")
git("add", ".")
git("commit", "-q", "-m", "fixture")
calls, codes = [], [1, 0, 1, 0]
def fake_rsync(cmd):
    calls.append({"argv": list(cmd), "closed": all(Path(x).is_file() for x in cmd[2:4])})
    return subprocess.CompletedProcess(cmd, codes.pop(0) if codes else 0, "", "refused")
rsync_only(fake_rsync)
last = lambda: json.loads((paths.backups_dir() / "last.json").read_text())
first_day = util.now().strftime("%Y-%m-%d")
steps = []
s = prune()
steps += [len(calls), last().get("copy_exit"), "copied_at" in last()]
prune(s)
stamp = last()["copied_at"]
steps += [len(calls), last()["copy_exit"], "copied_at" in last()]
prune(s)
prune()
steps += [len(calls), last()["copied_at"] == stamp]
real_now = util.now
util.now = lambda: real_now() + timedelta(days=1)
prune()
steps += [len(calls), last()["copied_at"] == stamp]
prune()
steps += [len(calls), last()["copied_at"] != stamp]
out["steps"] = steps
out["argv0"] = calls[0]["argv"]
out["closed"] = all(c["closed"] for c in calls)
out["days"] = sorted(p.name for p in paths.backups_dir().glob("config-*.tar.gz"))
out["first_day"] = first_day
archive = paths.backups_dir() / f"config-{first_day}.tar.gz"
with tarfile.open(archive) as tar:
    out["members"] = tar.getnames()
    (root / "layers.bundle").write_bytes(tar.extractfile("layers.bundle").read())
out["verify"] = subprocess.run(["git", "bundle", "verify", str(root / "layers.bundle")], cwd=layers, capture_output=True).returncode
out["heads"] = subprocess.run(["git", "bundle", "list-heads", str(root / "layers.bundle")], cwd=layers,
                              capture_output=True, text=True).stdout
def bundling():
    try:
        backup.layers_bundle()
    except RuntimeError as e:
        return str(e)
    return ""
(layers / "sub").mkdir()
(layers / "sub/pi.env").write_text("K=1")
git("add", "-f", ".")
git("commit", "-q", "-m", "secret")
git("rm", "-q", "-r", "sub")
git("commit", "-q", "-m", "removed")
out["history"] = bundling()
layers.rename(root / "real-layers")
layers.symlink_to(root / "real-layers")
out["linked"] = bundling()
