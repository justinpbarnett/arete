
import contextlib, hashlib, io, json, os, sys, threading
from datetime import timedelta
from pathlib import Path
from unittest import mock
from arete import config, jira, loops, paths, serve, sources, state, util
root = Path(os.environ["IDENT_ROOT"])
cfg = config.load()
server = serve.Server()
returned = []
real_poll = sources.poll
def recording(*a, **k):
    got = real_poll(*a, **k)
    returned.append(got)
    return got
sources.poll = recording
out = {}
def block(target, **more):
    lines = ["[[source]]", 'type = "command"', "command = " + json.dumps(["cat", str(target)])]
    return "\n".join(lines + [f"{k} = {v}" for k, v in more.items()]) + "\n"
def load(name, *blocks):
    folder = paths.CONFIG_DIR / "loops" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "loop.toml").write_text('kind = "command"\n[run]\ncommand = "true"\n' + "".join(blocks))
    return loops.load(folder, "private", "", cfg)
def write(path, *rows):
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in rows))
def row(key, at=None):
    return {"key": key, "title": key, "data": {"at": at} if at else {}}
def poll(loop, index=0):
    returned.clear()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        server.poll_source(loop, index)
    events = returned[0][0] if returned else []
    return {"ran": bool(returned), "keys": [e["key"] for e in events], "titles": [e["title"] for e in events],
            "lines": buffer.getvalue().splitlines()}
def sql(query, *args):
    with state.db() as c:
        return [tuple(r) for r in c.execute(query, args)]
def seen(sid):
    return sorted(r[0] for r in sql("SELECT key FROM seen WHERE source = ?", sid))
def queued(loop):
    return [[e["key"] for e in json.loads(r[0])] for r in sql("SELECT payload FROM queue WHERE loop = ? ORDER BY id", loop)]
def rows_for(sid):
    return {t: len(sql(f"SELECT 1 FROM {t} WHERE source = ?", sid)) for t in ("cursors", "seen", "source_owners")}
def quoted(text):
    return json.dumps(text)

data = root / "plain.jsonl"
write(data, row("p1"), row("p2"))
loop = load("plain", block(data, timeout="60", trust=quoted("internal"), every=quoted("1m")))
first = poll(loop)
write(data, row("p1"), row("p2"), row("p3"))
loop = load("plain", block(data, timeout="30", trust=quoted("owner"), every=quoted("10m")))
edited = poll(loop)
out["plain"] = {"first": first, "edited": edited, "again": poll(loop), "seen": seen("plain#0"),
                "queued": queued("plain")}

class Fake:
    def __init__(self):
        self.books = {}
    def search(self, jql, fields, limit=50):
        if jql not in self.books:
            raise RuntimeError("Error in the JQL Query: " + jql)
        return self.books[jql]
    def browse_url(self, key):
        return "https://jira.invalid/browse/" + key
def ticket(key, updated):
    return {"key": key, "fields": {"summary": key, "updated": updated, "status": {"name": "Ready"}}}
def jira_block(jql):
    return '[[source]]\ntype = "jira"\njql = ' + quoted(jql) + "\n"
fake = Fake()
with mock.patch.object(jira.Client, "from_env", lambda *a, **k: fake):
    old = util.iso(util.now() - timedelta(days=2))
    fake.books["status = Ready"] = [ticket("ABC-1", old)]
    loop = load("outage", jira_block("status = Ready"))
    base = poll(loop)
    good, owner = state.cursor("outage#0"), state.source_owner("outage#0")
    loop = load("outage", jira_block("status = Refused"))
    failed = [poll(loop), poll(loop)]
    bad, owner_after = state.cursor("outage#0"), state.source_owner("outage#0")
    stamp = util.parse_iso(good["last_ok"])
    after = util.iso(stamp + timedelta(seconds=30))
    month = util.iso(stamp - timedelta(days=30))
    fake.books["status = Fixed"] = ([ticket("ABC-1", old)] + [ticket(f"ABC-{n}", after) for n in range(2, 7)]
                                    + [ticket("ABC-7", month)])
    loop = load("outage", jira_block("status = Fixed"))
    fixed = poll(loop)
    out["outage"] = {"base": base, "failed": failed, "good": good, "bad": bad, "owner": owner,
                     "owner_after": owner_after, "fixed": fixed,
                     "further": poll(loop), "queued": queued("outage"), "seen": seen("outage#0")}

data, other = root / "edge.jsonl", root / "edge2.jsonl"
write(data, row("e0"))
loop = load("edge", block(data))
poll(loop)
last = util.parse_iso(state.cursor("edge#0")["last_ok"])
write(other, row("on-edge", util.iso(last - timedelta(minutes=2))),
      row("just-before", util.iso(last - timedelta(minutes=2, seconds=1))))
loop = load("edge", block(other))
out["edge"] = poll(loop)

data, other = root / "cmd.jsonl", root / "cmd2.jsonl"
write(data, row("k0"))
loop = load("cmdscope", block(data))
poll(loop)
write(other, row("untimed"), row("timed", util.iso()))
loop = load("cmdscope", block(other))
changed = poll(loop)
out["cmdscope"] = {"changed": changed, "repeat": poll(loop), "seen": seen("cmdscope#0"), "queued": queued("cmdscope")}

a, b = root / "a.jsonl", root / "b.jsonl"
write(a, row("a1"))
write(b, row("b1"))
loop = load("demo", block(a, name=quoted("a")), block(b, name=quoted("b")))
poll(loop, 0)
poll(loop, 1)
write(a, row("a1"), row("a5"))
write(b, row("b1"), row("b2"))
loop = load("demo", block(b, name=quoted("b")), block(a, name=quoted("a")))
out["reorder"] = {"first": poll(loop, 0), "second": poll(loop, 1), "a": seen("demo#a"), "b": seen("demo#b"),
                  "ids": [sources.source_id(loop, i) for i in range(2)], "slots": [rows_for("demo#0"), rows_for("demo#1")]}

f, n = root / "f.jsonl", root / "n.jsonl"
def adoption(name, below):
    write(f, row("f1"), row("f2"))
    write(n, row("n1"))
    poll(load(name, block(f)))
    write(f, row("f1"), row("f2"), row("f3"))
    old_block, new_block = block(f, name=quoted("old")), block(n, name=quoted("fresh"))
    loop = load(name, *((old_block, new_block) if below else (new_block, old_block)))
    got = {}
    for index in (0, 1):
        got[sources.source_id(loop, index).split("#")[1]] = poll(loop, index)
    return {"polls": got, "slot": rows_for(f"{name}#0"), "old_seen": seen(f"{name}#old"),
            "fresh_seen": seen(f"{name}#fresh")}
out["above"] = adoption("adopt", False)
out["below"] = adoption("adoptlow", True)

state.set_cursor("foreign#1", None, True, "old")
state.claim_seen("foreign#1", ["z1"])
solo = root / "solo.jsonl"
write(solo, row("s1"))
loop = load("foreign", block(solo, name=quoted("solo")))
out["foreign"] = {"first": poll(loop), "slot": rows_for("foreign#1"), "foreign_seen": seen("foreign#1"),
                  "solo_seen": seen("foreign#solo")}

data = root / "rename.jsonl"
write(data, row("r1"), row("r2"))
poll(load("rename", block(data, name=quoted("old"))))
loop = load("rename", block(data, name=quoted("new")))
out["rename"] = {"renamed": poll(loop), "new_seen": seen("rename#new"), "old_seen": seen("rename#old")}

watched = root / "watched"
watched.mkdir()
one, two = watched / "one.log", watched / "two.log"
def log_block(path, pattern=None):
    return '[[source]]\ntype = "log"\npath = ' + quoted(str(path)) + ("\npattern = " + quoted(pattern) if pattern else "") + "\n"
one.write_text("INFO started\n")
poll(load("logs", log_block(one)))
with one.open("a") as handle:
    handle.write("ERROR alpha widget exploded\n")
two.write_text("ERROR old beta gadget\n")
two_size = two.stat().st_size
loop = load("logs", log_block(watched / "*.log", "ERROR"))
scoped = poll(loop)
offsets = json.loads(state.cursor("logs#0")["value"])
with two.open("a") as handle:
    handle.write("ERROR gamma sprocket fresh\n")
out["logs"] = {"scoped": scoped, "offsets": offsets, "names": [str(one), str(two)], "two_size": two_size,
               "later": poll(loop)}

data = root / "legacy.jsonl"
write(data, row("l1"), row("l2"))
loop = load("legacy", block(data, timeout="60", every=quoted("1m")))
old_owner = hashlib.sha256(json.dumps({k: v for k, v in loop.sources[0].items() if k != "every"}, sort_keys=True,
                                      default=str).encode()).hexdigest()
state.set_cursor("legacy#0", None, True, "1 read, 0 new")
state.claim_seen("legacy#0", ["l1"])
state.set_source_owner("legacy#0", old_owner)
sql("UPDATE cursors SET last_ok = NULL, polled = '2026-01-01T00:00:00Z' WHERE source = 'legacy#0'")
legacy = poll(loop)
state.set_cursor("legacy#9", None, True, "x")
sql("UPDATE cursors SET last_ok = NULL, polled = '2026-01-01T00:00:00Z' WHERE source = 'legacy#9'")
state.set_cursor("legacy#9", None, False, "boom")
kept = state.cursor("legacy#9")["last_ok"]
state.ensure_schema()
state.ensure_schema()
out["legacy"] = {"poll": legacy, "owner": state.source_owner("legacy#0"), "kept": kept, "seen": seen("legacy#0"),
                 "queued": queued("legacy"), "old_owner": old_owner}

data = root / "legacy-edit.jsonl"
write(data, row("a1"), row("a2"))
old_settings = load("legacyedit", block(data, timeout="60"))
legacy_owner = hashlib.sha256(json.dumps({k: v for k, v in old_settings.sources[0].items() if k != "every"},
                                         sort_keys=True, default=str).encode()).hexdigest()
state.set_cursor("legacyedit#0", None, True, "2 read, 0 new")
state.claim_seen("legacyedit#0", ["a1", "a2"])
state.set_source_owner("legacyedit#0", legacy_owner)
write(data, row("a1"), row("a2"), row("a3"))
loop = load("legacyedit", block(data, timeout="30"))
out["legacy_edit"] = {"poll": poll(loop), "seen": seen("legacyedit#0"), "queued": queued("legacyedit")}

def refused(name, *blocks):
    try:
        load(name, *blocks)
    except loops.LoopError as e:
        return str(e)
    return None
out["names"] = {
    "unnamed": refused("check-one", block(a), block(b)),
    "repeated": refused("check-two", block(a, name=quoted("x")), block(b, name=quoted("x"))),
    "webhook": refused("check-three", block(a, name=quoted("webhook"))),
    "metric": refused("check-four", block(a, name=quoted("metric"))),
    "digits": refused("check-five", block(a, name=quoted("1"))),
    "characters": refused("check-six", block(a, name=quoted("Bad Name"))),
    "number": refused("check-seven", block(a, name="5")),
    "single": refused("check-eight", block(a)),
    "distinct": refused("check-nine", block(a, name=quoted("x")), block(b, name=quoted("y")))}

data = root / "retry.jsonl"
write(data, row("r1"))
loop = load("retry", block(data))
poll(loop)
write(data, row("r1"), row("r2"))
before = {"cursor": state.cursor("retry#0"), "owner": state.source_owner("retry#0")}
real_enqueue = state.enqueue
def broken(*args, **kwargs):
    raise RuntimeError("queue is down")
state.enqueue = broken
failed = poll(loop)
state.enqueue = real_enqueue
after = {"cursor": state.cursor("retry#0"), "owner": state.source_owner("retry#0")}
out["retry"] = {"failed": failed, "before": before, "after": after, "seen": seen("retry#0"),
                "queued_after_failure": queued("retry"), "healed": poll(loop), "queued": queued("retry"),
                "third": poll(loop)}

data = root / "race.jsonl"
write(data, row("r1"))
loop = load("race", block(data))
poll(loop)
write(data, row("r1"), row("r2"))
gate, reached = threading.Event(), threading.Event()
real_fetch = sources.Command.fetch
def slow(self, *args, **kwargs):
    got = real_fetch(self, *args, **kwargs)
    reached.set()
    gate.wait(30)
    return got
sources.Command.fetch = slow
server.tried.clear()
with contextlib.redirect_stdout(io.StringIO()):
    server.poll(loop)
    reached.wait(30)
    named = load("race", block(data, name=quoted("named")))
    server.tried.clear()
    server.poll(named)
    named_started = "race#named" in server.polls
    gate.set()
    server.polls["race#0"].result(30)
    sources.Command.fetch = real_fetch
    server.tried.clear()
    server.poll(named)
    server.polls["race#named"].result(30)
out["race"] = {"named_started": named_started, "queued": queued("race"), "slot": rows_for("race#0"),
               "named_seen": seen("race#named")}

server.pool.shutdown(wait=False)
for pool in server.job_pools.values():
    pool.shutdown(wait=False)
print("BEHAVIOR_RESULT=" + json.dumps(out))
