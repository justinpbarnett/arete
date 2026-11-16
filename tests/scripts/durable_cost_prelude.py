
import contextlib, gzip, io, json, os, sqlite3, sys
from pathlib import Path
from unittest import mock
from arete.store import runs as run_store
from arete import accounts, agent, claude, cli, codex, config, devices, gh, heartbeat, paths, pi, record, runner, scrub, state, util
state.migrate()
root, mode = Path(sys.argv[1]), sys.argv[2]
def forbidden(*a, **kw):
    raise AssertionError("offline cost test attempted a provider or credential lookup")
claude.run = codex.run = pi.run = forbidden
accounts.read = util.github_token = forbidden
original_sh = util.sh
def local_sh(cmd, *a, **kw):
    if isinstance(cmd, list) and cmd[:1] == ["git"]:
        return original_sh(cmd, *a, **kw)
    return forbidden(cmd, *a, **kw)
util.sh = local_sh
scrub.secrets = lambda *a, **kw: ()
cfg = config.Config()
def call(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()
def rows(rid):
    with state.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM steps WHERE run_id = ? ORDER BY id", (rid,))]
def marks(rid):
    f = paths.record_dir(rid) / record.TIMELINE
    return [v for line in gzip.decompress(f.read_bytes()).decode().splitlines()
            if (v := json.loads(line))["kind"] == "agent"] if f.exists() else []
def attempt(rid, which="claude", **kw):
    return runner.attempt(which, "test", "offline", root, agent.Session(), cfg, (rid, "build"), rid,
                          devices.LOCAL, kw, auth=accounts.Auth(mode="api_key"))
def result():
    return agent.Result(ok=True, cost_usd=2.5)
