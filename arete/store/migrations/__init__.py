import ast
import hashlib
import re
import runpy
from dataclasses import dataclass
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
STORE_FOLDER = FOLDER.parent
PHASES = {"expand", "backfill", "contract"}
NAME = re.compile(r"m([0-9]{4})_([a-z]+)_([a-z][a-z0-9_]*).py")


class Refused(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    n: int
    name: str
    area: str
    phase: str
    sha256: str
    sql: str | None
    apply: object


def owners():
    found = {}
    for path in STORE_FOLDER.glob("*.py"):
        if path.stem.startswith("_"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                    and any(isinstance(t, ast.Name) and t.id == "TABLES" for t in node.targets)):
                tables = ast.literal_eval(node.value)
                if isinstance(tables, dict):
                    found[path.stem] = tables
    return found


def area_owner(area, name=""):
    if area == "work" and name == "m0004_work_event_routing.py":
        area = "loops"
    owner = "db" if area == "foundation" else area
    return owner if owner in owners() else None


def load(folder=None):
    folder = FOLDER if folder is None else Path(folder)
    found = {}
    for path in sorted(folder.glob("m*.py")):
        match = NAME.fullmatch(path.name)
        if not match:
            raise Refused(f"{path.name}: expected mNNNN_area_name.py")
        n, area = int(match[1]), match[2]
        if area_owner(area, path.name) is None:
            raise Refused(f"{path.name}: unknown area {area}")
        if n in found:
            raise Refused(f"{found[n].name}, {path.name}: repeated migration {n}")
        data = path.read_bytes()
        namespace = runpy.run_path(str(path), run_name=f"arete.store.migrations.{path.stem}")
        phase, sql, apply = namespace.get("PHASE"), namespace.get("SQL"), namespace.get("apply")
        if phase not in PHASES:
            raise Refused(f"{path.name}: unknown phase {phase!r}")
        if (("SQL" in namespace) + ("apply" in namespace) != 1
                or not (isinstance(sql, str) or callable(apply))):
            raise Refused(f"{path.name}: provide exactly one SQL string or apply(conn)")
        found[n] = Migration(n, path.name, "loops" if area == "work" else area, phase, hashlib.sha256(data).hexdigest(), sql, apply)
    if not found or sorted(found) != list(range(1, max(found) + 1)):
        names = ", ".join(m.name for m in found.values())
        raise Refused(f"{names}: migration numbers must run from 1 without gaps")
    highest = found[max(found)].name
    try:
        latest = (folder / "latest.txt").read_text().splitlines()
    except OSError as e:
        raise Refused(f"latest.txt: {e}; expected {highest}") from e
    if latest != [highest]:
        raise Refused(f"latest.txt names {latest!r}; expected {highest}")
    return list(found.values())


def validate(conn, chain, version):
    if not version:
        return
    try:
        rows = list(conn.execute("SELECT n, name, sha256, phase FROM schema_migrations ORDER BY n"))
    except Exception as e:
        raise Refused(f"user_version {version} has no schema_migrations ledger") from e
    by_n = {m.n: m for m in chain}
    highest = max(by_n, default=0)
    if [r[0] for r in rows] != list(range(1, version + 1)):
        raise Refused(f"schema_migrations disagrees with user_version {version}")
    for n, name, digest, phase in rows:
        if n > highest:
            if phase != "expand":
                raise Refused(f"{name}: {phase} migration is past this code's migration chain")
            continue
        migration = by_n.get(n)
        if migration is None or migration.name != name or migration.sha256 != digest:
            expected = migration.name if migration else "missing migration file"
            raise Refused(f"{name}, {expected}: applied migration sha256 differs")
