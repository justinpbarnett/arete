"""Prospective, goal-level delivery observations. No write or tracker access on report."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import timedelta

from . import gh, paths, util

SCHEMA = """
CREATE TABLE IF NOT EXISTS cohorts(
 name TEXT PRIMARY KEY, repo TEXT NOT NULL, destination TEXT NOT NULL, workload TEXT NOT NULL,
 slug TEXT NOT NULL, label TEXT NOT NULL, opens TEXT NOT NULL, closes TEXT NOT NULL,
 deadline_hours INTEGER NOT NULL, declared TEXT NOT NULL, scanned TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS members(
 cohort TEXT NOT NULL REFERENCES cohorts(name), ticket TEXT NOT NULL, root TEXT NOT NULL,
 arrived TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(cohort,ticket));
CREATE TABLE IF NOT EXISTS observations(
 repo TEXT NOT NULL, ticket TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL,
 evidence TEXT NOT NULL, detail TEXT NOT NULL, destination TEXT NOT NULL DEFAULT '',
 revision TEXT NOT NULL DEFAULT '', PRIMARY KEY(repo,ticket,kind,evidence,destination,revision));
"""
OWNER_EVENTS = {"touch", "audit", "failed", "regression", "pending"}
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class UnknownCohort(ValueError):
    pass


def location():
    return paths.STATE_DIR / "cohorts.sqlite"


@contextmanager
def ledger(*, write=False, create=False):
    file = location()
    if create:
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    mode = "rwc" if create else "rw" if write else "ro"
    with closing(sqlite3.connect(f"{file.as_uri()}?mode={mode}", uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if create:
            conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        with conn:
            yield conn


def key(text):
    if not isinstance(text, str) or not KEY.fullmatch(text):
        raise ValueError(f"invalid identifier {text!r}")
    return text


def timestamp(value):
    dt = util.parse_iso(value)
    if dt.tzinfo is None:
        raise ValueError("timestamp requires an offset")
    return util.iso(dt.astimezone(util.now().tzinfo))


def declare(name, repo, destination, workload, *, slug="", label="", tickets=(), opens="", closes, hours):
    now = util.iso()
    opens, closes = timestamp(opens or now), timestamp(closes)
    tickets = sorted({key(str(t)) for t in tickets})
    if opens < now or closes <= opens or hours < 1:
        raise ValueError("declare before the window, with a later end and positive delivery deadline")
    if not (destination.strip() and workload.strip()) or bool(tickets) == bool(label):
        raise ValueError("supply the work class, destination, and either a whole batch or a GitHub label")
    if label and (not re.fullmatch(r"[\w.-]+/[\w.-]+", slug) or any(x in (".", "..") for x in slug.split("/"))):
        raise ValueError("label intake needs an owner/repository")
    with ledger(write=True, create=True) as conn:
        conn.execute(
            "INSERT INTO cohorts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (key(name), key(repo), destination, workload, slug, label, opens, closes, hours, now, ""),
        )
        conn.executemany(
            "INSERT INTO members VALUES(?,?,?,?,?)",
            [(name, ticket, ticket, opens, "declared batch") for ticket in tickets],
        )


def active():
    if not location().exists():
        return []
    with ledger() as conn:
        return [
            dict(c)
            for c in conn.execute(
                "SELECT * FROM cohorts WHERE label!='' AND scanned<closes AND opens<=?", (util.iso(),)
            )
        ]


def arrivals(cohort, cutoff):
    """Replay the public tracker event stream, including closed issues and removed labels."""
    since = cohort["scanned"] or cohort["opens"]
    floor = max(cohort["opens"], util.iso(util.parse_iso(since) - timedelta(minutes=5)))
    slug = cohort["slug"]
    page = 1
    while True:
        rows = gh.gh_json(["api", f"/repos/{slug}/issues/events?per_page=100&page={page}"])
        if not isinstance(rows, list):
            raise ValueError("invalid GitHub issue-event page")
        for event in rows:
            at = timestamp(event["created_at"])
            if not (floor <= at < cutoff) or event["event"] != "labeled":
                continue
            issue = event.get("issue") or {}
            if (
                "pull_request" in issue
                or event.get("label", {}).get("name") != cohort["label"]
                or (issue.get("user") or {}).get("login", "").casefold() != slug.split("/")[0].casefold()
            ):
                continue
            yield str(int(issue["number"])), at, str(int(event["id"]))
        if len(rows) < 100 or timestamp(rows[-1]["created_at"]) < floor:
            return
        page += 1


def sync():
    for cohort in active():
        # Wait for tracker propagation before advancing the durable cursor.
        cutoff = min(cohort["closes"], util.iso(util.now() - timedelta(seconds=30)))
        if cutoff <= (cohort["scanned"] or cohort["opens"]):
            continue
        found = sorted(arrivals(cohort, cutoff), key=lambda item: item[1])
        with ledger(write=True) as conn:
            conn.executemany(
                """INSERT INTO members VALUES(?,?,?,?,?) ON CONFLICT(cohort,ticket) DO UPDATE SET
                             arrived=excluded.arrived, source=excluded.source WHERE members.source='replacement'
                             OR excluded.arrived<members.arrived""",
                [(cohort["name"], ticket, ticket, at, f"github-event:{event}") for ticket, at, event in found],
            )
            conn.execute("UPDATE cohorts SET scanned=max(scanned,?) WHERE name=?", (cutoff, cohort["name"]))


def member(conn, cohort, ticket):
    row = conn.execute("SELECT * FROM members WHERE cohort=? AND ticket=?", (cohort, ticket)).fetchone()
    if not row:
        raise ValueError(f"{ticket} is outside cohort {cohort}")
    return row


def replacement(name, ticket, successor):
    successor = key(successor)
    with ledger(write=True) as conn:
        original = member(conn, name, ticket)
        prior = conn.execute("SELECT * FROM members WHERE cohort=? AND ticket=?", (name, successor)).fetchone()
        if prior and prior["root"] != original["root"]:
            if prior["root"] != successor:
                raise ValueError("successor belongs to another established lineage")
            conn.execute("UPDATE members SET root=? WHERE cohort=? AND root=?", (original["root"], name, successor))
        elif not prior:
            conn.execute(
                "INSERT INTO members VALUES(?,?,?,?,?)", (name, successor, original["root"], util.iso(), "replacement")
            )


def insert(conn, repo, ticket, kind, at, evidence, detail, destination="", revision=""):
    if not evidence.strip() or not detail.strip():
        raise ValueError("give the observed reason and a durable evidence reference")
    conn.execute(
        "INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?)",
        (repo, ticket, kind, at, evidence, detail, destination, revision),
    )


def audit_signature(name, ids, receipt):
    return hashlib.sha256(json.dumps([name, sorted(ids), receipt], separators=(",", ":")).encode()).hexdigest()


def note(name, ticket, kind, evidence, detail, at=""):
    if kind not in OWNER_EVENTS:
        raise ValueError("unsupported observation")
    at = timestamp(at or util.iso())
    with ledger(write=True) as conn:
        root = member(conn, name, ticket)["root"]
        repo, target = conn.execute("SELECT repo,destination FROM cohorts WHERE name=?", (name,)).fetchone()
        if at > util.iso():
            raise ValueError("cannot record an observation in the future")
        if kind == "audit":
            ids = [row[0] for row in conn.execute("SELECT ticket FROM members WHERE cohort=? AND root=?", (name, root))]
            receipt = conn.execute(
                """SELECT evidence FROM observations WHERE repo=? AND kind='verified'
                                  AND destination=? AND ticket IN (SELECT value FROM json_each(?)) AND at<=?
                                  ORDER BY at DESC,rowid DESC LIMIT 1""",
                (repo, target, json.dumps(ids), at),
            ).fetchone()
            if not receipt:
                raise ValueError("audit the lineage after observing its delivered release")
            insert(conn, repo, ticket, kind, at, evidence, detail, target, audit_signature(name, ids, receipt[0]))
        else:
            insert(conn, repo, ticket, kind, at, evidence, detail)


def record_release(repo, ticket, revision, receipt_id, destination, *, regression=False):
    """Called after a protected release probe settles. Admission may be seen on a later poll."""
    if not location().exists():
        return
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision) or not destination:
        raise ValueError("release observation needs an exact revision and destination")
    with ledger(write=True) as conn:
        if conn.execute("SELECT 1 FROM cohorts WHERE repo=? AND destination=?", (repo, destination)).fetchone():
            kind = "regression" if regression else "verified"
            insert(
                conn, repo, str(ticket), kind, util.iso(), receipt_id, "protected release probe", destination, revision
            )


def record_production_qa(repo, ticket, revision, comment):
    """An accepted QA direction is a human intervention only on the path to production."""
    with ledger(write=True) as conn:
        if not conn.execute("SELECT 1 FROM cohorts WHERE repo=? AND destination='production'", (repo,)).fetchone():
            return False
        insert(conn, repo, str(ticket), "touch", comment["at"], comment["url"],
               f"Production QA approved by {comment['actor']}", "production", revision)
        return True


def owner_intervention(result, observation):
    """Only record a newly advanced run under an authenticated human principal."""
    from . import principals

    if not location().is_file() or not result or not result.get("run") or result.get("why") == "already running":
        return
    repo, ticket, run = result.get("repo"), result.get("task"), result["run"]
    try:
        if not repo or not ticket or not principals.is_person(principals.current()):
            return
        at = util.iso()
        with ledger(write=True) as conn:
            if conn.execute("SELECT 1 FROM cohorts WHERE repo=? AND opens<=?", (repo, at)).fetchone():
                insert(conn, repo, str(ticket), "touch", at, f"owner:{observation}:{run}", observation)
    except (OSError, sqlite3.Error):
        # The operation already succeeded. Unattributed history remains unknown.
        logging.getLogger(__name__).exception("Could not record cohort intervention for run %s", run)


def facts(repo, tickets):
    if not paths.DB_FILE.is_file():
        return [], [], ""
    from .store import db

    marks = ",".join("?" for _ in tickets)
    args = (repo, *tickets)
    with db.reader() as conn:
        runs = [
            dict(r)
            for r in conn.execute(
                f"SELECT id,started,status,pr_url FROM runs WHERE repo=? AND issue IN ({marks}) "
                "AND kind='pipeline' AND dry_run=0 ORDER BY started DESC,rowid DESC",
                args,
            )
        ]
        answers = [
            dict(r)
            for r in conn.execute(
                f"SELECT a.id,a.answered FROM asks a JOIN runs r ON a.run=r.id "
                f"WHERE r.repo=? AND r.issue IN ({marks}) AND r.kind='pipeline' AND r.dry_run=0 "
                "AND a.status='answered' AND a.recovery_route='owner' AND a.taken_by!='' ORDER BY a.answered",
                args,
            )
        ]
        wait = conn.execute(
            f"SELECT cause, phase FROM unblock_cases WHERE repo=? AND ticket IN ({marks}) "
            "AND phase NOT IN ('resolved','cancelled') ORDER BY updated DESC LIMIT 1",
            args,
        ).fetchone()
    return runs, answers, (f"{wait['phase']}: {wait['cause']}" if wait else "")


def report(name):
    now = util.now()
    with ledger() as conn:
        stored = conn.execute("SELECT * FROM cohorts WHERE name=?", (name,)).fetchone()
        if not stored:
            raise UnknownCohort(f"no cohort {name}")
        cohort = dict(stored)
        groups = {}
        for m in conn.execute("SELECT * FROM members WHERE cohort=? ORDER BY arrived,ticket", (name,)):
            groups.setdefault(m["root"], []).append(dict(m))
        goals = []
        for root, members in groups.items():
            ids = [m["ticket"] for m in members]
            arrived = min(m["arrived"] for m in members if m["source"] != "replacement")
            marks = ",".join("?" for _ in ids)
            obs = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM observations WHERE repo=? AND ticket IN ({marks}) "
                    "AND (destination='' OR destination=?) ORDER BY at,rowid",
                    (cohort["repo"], *ids, cohort["destination"]),
                )
            ]
            runs, answers, wait = facts(cohort["repo"], ids)
            obs += [
                dict(kind="touch", at=a["answered"], evidence="ask:" + a["id"], detail="owner answer consumed")
                for a in answers
                if a["answered"]
            ]
            obs = sorted((r for r in obs if r["at"] >= arrived or r["kind"] == "touch"), key=lambda r: r["at"])
            deliveries = [r for r in obs if r["kind"] == "verified"]
            verified = deliveries[-1] if deliveries else None
            terminal = next((r for r in reversed(obs) if r["kind"] in ("failed", "verified", "pending")), None)
            status = {"verified": "verified", "failed": "failed"}.get(terminal["kind"] if terminal else "", "pending")
            through = verified["at"] if status == "verified" else util.iso(now)
            first = next((r for r in obs if r["kind"] == "touch" and r["at"] <= through), None)
            # An empty trace has no authority to establish absence of manual tracker/PR actions.
            audited = status == "verified" and any(
                r["kind"] == "audit"
                and r["at"] >= verified["at"]
                and r["revision"] == audit_signature(name, ids, verified["evidence"])
                for r in obs
            )
            provenance = "touched" if first else "untouched" if audited else "unknown"
            age = int((now - util.parse_iso(arrived)).total_seconds())
            deadline = util.parse_iso(arrived) + timedelta(hours=cohort["deadline_hours"])
            reason = (
                terminal["detail"]
                if terminal
                else wait
                or (
                    "last attempt " + runs[0]["status"] + "; awaiting delivery evidence"
                    if runs
                    else "waiting for first run"
                )
            )
            goals.append(
                dict(
                    root=root,
                    tickets=ids,
                    arrived=arrived,
                    status=status,
                    terminal_evidence=terminal["evidence"] if terminal else None,
                    reason=reason,
                    age_seconds=max(0, age),
                    deadline=util.iso(deadline),
                    overdue=(util.parse_iso(verified["at"]) if verified else now) > deadline,
                    provenance=provenance,
                    first_touch=first,
                    release=verified,
                    regressions=[
                        r for r in obs if verified and r["at"] >= deliveries[0]["at"] and r["kind"] == "regression"
                    ],
                    pull_requests=sorted({url for run in runs for url in (run["pr_url"] or "").split()}),
                    attempts=len(runs),
                )
            )
    counts = {s: dict.fromkeys(("touched", "untouched", "unknown"), 0) for s in ("verified", "failed", "pending")}
    for g in goals:
        counts[g["status"]][g["provenance"]] += 1
    return dict(
        cohort=cohort,
        observed=util.iso(now),
        arrivals=sum(m["source"] != "replacement" for group in groups.values() for m in group),
        goals=len(goals),
        outcomes=counts,
        members=goals,
        intake_complete=not cohort["label"] or cohort["scanned"] >= cohort["closes"],
    )


def candidates(name):
    result = report(name)
    selected = []
    for goal in result["members"]:
        for event in [goal["first_touch"], *goal["regressions"]]:
            if event:
                selected.append(
                    dict(goal=goal["root"], kind=event["kind"], evidence=event["evidence"], detail=event["detail"])
                )
        if goal["status"] == "failed":
            selected.append(
                dict(goal=goal["root"], kind="failed", detail=goal["reason"], evidence=goal["terminal_evidence"])
            )
    return selected
