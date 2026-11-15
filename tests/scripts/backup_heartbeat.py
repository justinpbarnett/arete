
seed()
install.serve_installed = lambda: True
cfg = config.load()
keyed = lambda c: {x.key: x for x in heartbeat.check_backups(c)}
snap = backup.snapshot("hand")
out["fresh"] = keyed(cfg)["backup:db"].ok
old = time.time() - 4 * 3600
os.utime(snap, (old, old))
out["stale"] = keyed(cfg)["backup:db"].ok
snap = backup.snapshot("hand")
os.truncate(snap, snap.stat().st_size // 2)
out["truncated"] = keyed(cfg)["backup:db"].ok
snap = backup.snapshot("hand")
backup.update_last(quick_check="row 1 missing from index")
out["recorded"] = keyed(cfg)["backup:db"].ok
empty = keyed(cfg)["backup:offsite"]
out["empty"] = [empty.ok, empty.severity, empty.detail]
away = dataclasses.replace(cfg, backup=config.BackupConfig("h:/x"))
out["never"] = keyed(away)["backup:offsite"].ok
backup.update_last(copied_at=util.iso(util.now() - timedelta(hours=49)))
out["old"] = keyed(away)["backup:offsite"].ok
backup.update_last(copied_at=util.iso())
out["recent"] = keyed(away)["backup:offsite"].ok
