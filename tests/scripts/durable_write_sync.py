
folder = Path(sys.argv[1]) / "w"
folder.mkdir()
run = pipeline.Run.__new__(pipeline.Run)
run.dir = folder
real = {"fcntl": fcntl.fcntl, "fsync": os.fsync, "replace": os.replace}
def trial(call, fullsync_fails=False):
    events = []
    def fcntl_spy(fd, op, *a):
        events.append("fullsync")
        if fullsync_fails:
            raise OSError("no")
        return real["fcntl"](fd, op, *a)
    def fsync_spy(fd):
        events.append("fsync")
        return real["fsync"](fd)
    def replace_spy(a, b):
        events.append("replace")
        return real["replace"](a, b)
    with mock.patch.object(util.fcntl, "fcntl", fcntl_spy), mock.patch.object(util.os, "fsync", fsync_spy), \
            mock.patch.object(util.os, "replace", replace_spy):
        call()
    return events
out = {"save": trial(lambda: run.save("s.txt", "plain text")),
       "json": trial(lambda: util.write_json(folder / "j.json", {"x": 1})),
       "fallback": trial(lambda: util.write_json(folder / "k.json", {"x": 2}), True)}
with mock.patch.object(util.fcntl, "F_FULLFSYNC", create=True), mock.patch.object(util, "fcntl", mock.Mock(spec=[])), \
        mock.patch.object(util.os, "fsync") as fsync:
    util.write_json(folder / "m.json", {"x": 3})
    out["missing_constant_fsyncs"] = fsync.call_count
out["modes"] = [oct((folder / n).stat().st_mode & 0o777) for n in ("s.txt", "j.json")]
out["text"] = (folder / "s.txt").read_text()
out["json_back"] = json.loads((folder / "j.json").read_text())
print("RESULT:" + json.dumps(out))
