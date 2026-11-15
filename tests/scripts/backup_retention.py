
seed()
folder = paths.backups_dir()
folder.mkdir(parents=True)
today = util.now().date()
planted = []
for d in range(1, 21):
    for hour in (6, 18):
        planted.append(f"arete-{today - timedelta(days=d):%Y%m%d}T{hour:02d}0000Z-hand.db")
        (folder / planted[-1]).write_bytes(b"x")
(folder / "arete.db.before-restore-1").write_bytes(b"x")
new = backup.snapshot("prune")
everyone = sorted(planted + [new.name], key=lambda n: (n[6:21], n))
by_day = {}
for n in everyone:
    by_day[n[6:14]] = n
first = f"{today - timedelta(days=13):%Y%m%d}"
expected = sorted(set(everyone[-24:]) | {n for day, n in by_day.items() if day >= first})
out |= {"left": sorted(p.name for p in folder.glob("arete-*.db")), "expected": expected,
        "other": (folder / "arete.db.before-restore-1").exists()}
