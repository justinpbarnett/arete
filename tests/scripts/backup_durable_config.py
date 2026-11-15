
paths.CONFIG_FILE.write_text("max_parallel = 1\n")
os.chmod(paths.CONFIG_FILE, 0o640)
events = []
real = {"fcntl": fcntl.fcntl, "fsync": os.fsync, "replace": os.replace}
kind = lambda fd: "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
def spy_fcntl(fd, op, *a):
    if op == getattr(fcntl, "F_FULLFSYNC", None):
        events.append(("flush", kind(fd)))
    return real["fcntl"](fd, op, *a)
def spy_fsync(fd):
    events.append(("flush", kind(fd)))
    return real["fsync"](fd)
def spy_replace(a, b):
    events.append(("replace", ""))
    return real["replace"](a, b)
with mock.patch.object(fcntl, "fcntl", spy_fcntl), mock.patch.object(os, "fsync", spy_fsync), mock.patch.object(os, "replace", spy_replace):
    settings.write_checked('max_parallel = 2\n[backup]\ncopy_to = "h:/x"\n', lambda cfg: "")
    out["checked"] = list(events)
    events.clear()
    optimize.put(b"max_parallel = 3\n")
    out["put"] = list(events)
    events.clear()
    out["loaded"] = config.load().max_parallel
    kept = paths.CONFIG_FILE.read_bytes()
    refused = []
    for text, why in (("max_parallel = 9\n", "no thanks"), ("this is [not toml", "")):
        try:
            settings.write_checked(text, lambda cfg: why)
        except settings.Refused as e:
            refused.append(str(e))
    out |= {"refused": len(refused), "unchanged": paths.CONFIG_FILE.read_bytes() == kept, "events": list(events),
            "mode": stat.S_IMODE(paths.CONFIG_FILE.stat().st_mode), "left": sorted(p.name for p in paths.CONFIG_DIR.iterdir())}
settings.write_checked('[backup]\ncopy_to = "h:/x"\n', lambda cfg: "")
out["copy_to"] = config.load().backup.copy_to
bad = []
for table in ({"copy_to": 5}, {"other": 1}, ["x"], {"copy_to": " h:/x"}):
    try:
        config.backup_table(table)
    except (ValueError, TypeError):
        bad.append(table)
out |= {"bad": len(bad), "default": config.backup_table({}).copy_to}
