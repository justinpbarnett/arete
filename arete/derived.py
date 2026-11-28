from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Literal

from . import paths, util
from .store import db as foundation
from .store import derived as derived_store
from .store import events

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EMPTY_COLUMNS: Mapping[str, tuple[str, ...]] = {}


class DerivedError(RuntimeError):
    pass


class UnavailableStoreError(DerivedError):
    pass


@dataclass(frozen=True)
class DiffReport:
    diff_rows: int
    sample_ids: tuple[str, ...] = ()
    detail: str = ""
    source_mark: str | None = None


@dataclass(frozen=True)
class CheckRecord:
    name: str
    code_version: int
    source_mark: str
    diff_rows: int


@dataclass(frozen=True)
class CheckOutcome:
    name: str
    ok: bool
    diff_rows: int
    sample_ids: tuple[str, ...] = ()
    detail: str = ""
    record: CheckRecord | None = None


@dataclass(frozen=True)
class Provider:
    name: str
    code_version: int
    source_mark: Callable[[sqlite3.Connection, str], str]
    diff: Callable[[sqlite3.Connection, str], DiffReport]
    rebuild: Callable[[sqlite3.Connection, str], str | None]
    required_tables: tuple[str, ...] = ()
    required_columns: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: _EMPTY_COLUMNS)
    is_available: Callable[[sqlite3.Connection], bool] | None = None
    enumerate_names: Callable[[sqlite3.Connection], Sequence[str]] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("derived provider name must be a non-empty string")
        if type(self.code_version) is not int or self.code_version < 1:
            raise ValueError(f"derived provider {self.name!r}: code_version must be an int >= 1")
        if self.enumerate_names is not None and not self.name.endswith(":"):
            raise ValueError(f"derived family provider {self.name!r} must end with ':'")
        if self.name.endswith(":") and self.enumerate_names is None:
            raise ValueError(f"derived family provider {self.name!r} requires enumerate_names")

    @property
    def is_family(self) -> bool:
        return self.name.endswith(":")

    def matches(self, store_name: str) -> bool:
        if self.is_family:
            return store_name.startswith(self.name) and len(store_name) > len(self.name)
        return store_name == self.name


@dataclass(frozen=True)
class ConsumerSpec:
    consumer: str
    required: tuple[str, ...]
    require_in_doctor: bool = True


StoreState = Literal[
    "fresh",
    "missing_provider",
    "unavailable_source",
    "unchecked",
    "stale_code_version",
    "stale_source_mark",
    "drifted",
    "error",
]


@dataclass(frozen=True)
class StoreStatus:
    name: str
    state: StoreState
    fresh: bool
    code_version: int | None = None
    recorded_code_version: int | None = None
    source_mark: str = ""
    recorded_source_mark: str = ""
    rebuilt: str | None = None
    checked: str | None = None
    diff_rows: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class CoverageReport:
    consumer: str
    expected: tuple[str, ...]
    complete: bool
    stale: bool
    missing: tuple[str, ...] = ()
    unchecked: tuple[str, ...] = ()
    stale_stores: tuple[str, ...] = ()
    drifted: tuple[str, ...] = ()
    stores: tuple[StoreStatus, ...] = ()


@dataclass(frozen=True)
class DoctorTarget:
    name: str
    provider: Provider | None
    available: bool
    required_by: tuple[str, ...] = ()


def _ident(name: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return foundation.quoted(name)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not table_exists(conn, table):
        return False
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({_ident(table)})")}
    return column in cols


def provider_available(conn: sqlite3.Connection, provider: Provider) -> bool:
    if not derived_store.available(conn):
        return False
    for table in provider.required_tables:
        if not table_exists(conn, table):
            return False
    for table, columns in provider.required_columns.items():
        if not table_exists(conn, table):
            return False
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({_ident(table)})")}
        if not all(col in existing for col in columns):
            return False
    if provider.is_available is not None:
        return bool(provider.is_available(conn))
    return True


def exact_table_diff(
    conn: sqlite3.Connection,
    live_table: str,
    temp_table: str,
    columns: Sequence[str],
    *,
    key_columns: Sequence[str] | None = None,
    where: str = "",
    params: Sequence[object] = (),
    sample_limit: int = 5,
    source_mark: str | None = None,
) -> DiffReport:
    if not columns:
        raise ValueError("exact_table_diff requires at least one column")
    live_q = _ident(live_table)
    temp_q = _ident(temp_table)
    cols_q = ", ".join(_ident(c) for c in columns)
    keys = tuple(columns) if key_columns is None else tuple(key_columns)
    if not keys:
        raise ValueError("exact_table_diff requires at least one key column")
    keys_q = ", ".join(_ident(c) for c in keys)
    where_sql = f" WHERE {where}" if where else ""
    base_sql = (
        f"SELECT {keys_q} FROM ("  # noqa: S608
        f"  SELECT {cols_q} FROM main.{live_q}{where_sql}"
        f"  EXCEPT"
        f"  SELECT {cols_q} FROM temp.{temp_q}"
        f") UNION "
        f"SELECT {keys_q} FROM ("
        f"  SELECT {cols_q} FROM temp.{temp_q}"
        f"  EXCEPT"
        f"  SELECT {cols_q} FROM main.{live_q}{where_sql}"
        f")"
    )
    query_params = (*params, *params)
    count = int(conn.execute(f"SELECT COUNT(*) FROM ({base_sql})", query_params).fetchone()[0])  # noqa: S608
    if count == 0:
        return DiffReport(diff_rows=0, sample_ids=(), detail="matches source", source_mark=source_mark)
    sample_rows = conn.execute(
        f"SELECT {keys_q} FROM ({base_sql}) ORDER BY {keys_q} LIMIT ?",  # noqa: S608
        (*query_params, sample_limit),
    ).fetchall()
    samples = tuple(":".join("" if v is None else str(v) for v in row) for row in sample_rows)
    return DiffReport(
        diff_rows=count,
        sample_ids=samples,
        detail=f"{count} diff {'row' if count == 1 else 'rows'}",
        source_mark=source_mark,
    )


class Registry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._providers: dict[str, Provider] = {}
        self._consumers: dict[str, ConsumerSpec] = {}

    def register(self, provider: Provider, *, replace: bool = False) -> Provider:
        with self._lock:
            if not replace and provider.name in self._providers:
                raise ValueError(f"derived provider {provider.name!r} is already registered")
            self._providers[provider.name] = provider
            return provider

    def register_if_available(
        self,
        conn: sqlite3.Connection,
        provider: Provider,
        *,
        replace: bool = False,
    ) -> bool:
        if not provider_available(conn, provider):
            return False
        self.register(provider, replace=replace)
        return True

    def unregister(self, name: str) -> Provider | None:
        with self._lock:
            return self._providers.pop(name, None)

    @contextmanager
    def registered(self, *providers: Provider, replace: bool = True) -> Iterator[None]:
        saved: dict[str, Provider | None] = {}
        with self._lock:
            for provider in providers:
                saved[provider.name] = self._providers.get(provider.name)
                self.register(provider, replace=replace)
        try:
            yield
        finally:
            with self._lock:
                for name, prev in saved.items():
                    if prev is None:
                        self._providers.pop(name, None)
                    else:
                        self._providers[name] = prev

    def providers(self) -> tuple[Provider, ...]:
        with self._lock:
            return tuple(self._providers[k] for k in sorted(self._providers))

    def active_providers(self, conn: sqlite3.Connection) -> tuple[Provider, ...]:
        return tuple(p for p in self.providers() if provider_available(conn, p))

    def resolve(self, name: str) -> Provider | None:
        with self._lock:
            if exact := self._providers.get(name):
                return exact
            families = [p for p in self._providers.values() if p.is_family and p.matches(name)]
            if not families:
                return None
            return max(families, key=lambda p: len(p.name))

    def resolve_active(self, conn: sqlite3.Connection, name: str) -> Provider | None:
        provider = self.resolve(name)
        if provider is None or not provider_available(conn, provider):
            return None
        return provider

    def expect_coverage(
        self,
        consumer: str,
        required: Sequence[str],
        *,
        require_in_doctor: bool = True,
    ) -> ConsumerSpec:
        if not isinstance(consumer, str) or not consumer.strip():
            raise ValueError("consumer name must be a non-empty string")
        names = tuple(dict.fromkeys(required))
        spec = ConsumerSpec(consumer=consumer, required=names, require_in_doctor=require_in_doctor)
        with self._lock:
            self._consumers[consumer] = spec
        return spec

    def clear_consumer(self, consumer: str) -> ConsumerSpec | None:
        with self._lock:
            return self._consumers.pop(consumer, None)

    @contextmanager
    def expected(
        self,
        consumer: str,
        required: Sequence[str],
        *,
        require_in_doctor: bool = True,
    ) -> Iterator[ConsumerSpec]:
        with self._lock:
            prev = self._consumers.get(consumer)
            spec = self.expect_coverage(consumer, required, require_in_doctor=require_in_doctor)
        try:
            yield spec
        finally:
            with self._lock:
                if prev is None:
                    self._consumers.pop(consumer, None)
                else:
                    self._consumers[consumer] = prev

    def expected_coverage(self, consumer: str | None = None) -> dict[str, ConsumerSpec]:
        with self._lock:
            if consumer is not None:
                spec = self._consumers.get(consumer)
                return {consumer: spec} if spec is not None else {}
            return {k: self._consumers[k] for k in sorted(self._consumers)}

    def required_doctor_names(self) -> tuple[str, ...]:
        with self._lock:
            ordered = {
                name
                for spec in self._consumers.values()
                if spec.require_in_doctor
                for name in spec.required
            }
            return tuple(sorted(ordered))

    def _concrete_names_for(
        self,
        conn: sqlite3.Connection,
        provider: Provider,
        existing_names: Sequence[str],
    ) -> tuple[str, ...]:
        if not provider.is_family:
            return (provider.name,)
        enumerated = list(provider.enumerate_names(conn)) if provider.enumerate_names is not None else []
        matched = [n for n in existing_names if provider.matches(n)]
        return tuple(sorted(set(enumerated) | set(matched)))

    def store_status(self, conn: sqlite3.Connection, name: str) -> StoreStatus:
        provider = self.resolve(name)
        if provider is None:
            return StoreStatus(name=name, state="missing_provider", fresh=False, detail="provider not registered")
        if not provider_available(conn, provider):
            return StoreStatus(
                name=name,
                state="unavailable_source",
                fresh=False,
                code_version=provider.code_version,
                detail="source tables or producer unavailable",
            )
        row = derived_store.get(name, conn)

        def _row_status(state: StoreState, *, fresh: bool = False, mark: str = "", detail: str = "") -> StoreStatus:
            return StoreStatus(
                name=name,
                state=state,
                fresh=fresh,
                code_version=provider.code_version,
                recorded_code_version=row["code_version"] if row else None,
                source_mark=mark,
                recorded_source_mark=row["source_mark"] if row else "",
                rebuilt=row["rebuilt"] if row else None,
                checked=row["checked"] if row else None,
                diff_rows=row["diff_rows"] if row else None,
                detail=detail,
            )

        if row is None or not row.get("checked"):
            return _row_status("unchecked", detail="derived store has not been checked")
        try:
            current_mark = provider.source_mark(conn, name)
        except Exception as exc:
            return _row_status("error", detail=f"{type(exc).__name__}: {exc}")
        if row["code_version"] != provider.code_version:
            return _row_status(
                "stale_code_version",
                mark=current_mark,
                detail=f"code_version {row['code_version']} != {provider.code_version}",
            )
        if row["source_mark"] != current_mark:
            return _row_status(
                "stale_source_mark",
                mark=current_mark,
                detail=f"source_mark {row['source_mark']!r} != {current_mark!r}",
            )
        if row["diff_rows"] != 0:
            return _row_status("drifted", mark=current_mark, detail=f"{row['diff_rows']} diff rows recorded")
        return _row_status("fresh", fresh=True, mark=current_mark, detail="fresh")

    def coverage(self, conn: sqlite3.Connection, consumer: str) -> CoverageReport:
        with self._lock:
            spec = self._consumers.get(consumer)
        if spec is None or not spec.required:
            return CoverageReport(
                consumer=consumer,
                expected=(),
                complete=False,
                stale=True,
            )
        existing_names = [r["name"] for r in derived_store.all_rows(conn)] if derived_store.available(conn) else []
        statuses: list[StoreStatus] = []
        for req in spec.required:
            if req.endswith(":"):
                provider = self.resolve(req)
                if provider is None:
                    statuses.append(
                        StoreStatus(name=req, state="missing_provider", fresh=False, detail="provider not registered")
                    )
                    continue
                if not provider_available(conn, provider):
                    statuses.append(
                        StoreStatus(
                            name=req,
                            state="unavailable_source",
                            fresh=False,
                            code_version=provider.code_version,
                            detail="source tables or producer unavailable",
                        )
                    )
                    continue
                concrete = self._concrete_names_for(conn, provider, existing_names)
                if not concrete:
                    statuses.append(
                        StoreStatus(
                            name=req,
                            state="unchecked",
                            fresh=False,
                            code_version=provider.code_version,
                            detail="no concrete stores enumerated or checked for family",
                        )
                    )
                    continue
                for name in concrete:
                    statuses.append(self.store_status(conn, name))
            else:
                statuses.append(self.store_status(conn, req))
        missing = tuple(s.name for s in statuses if s.state in ("missing_provider", "unavailable_source"))
        unchecked = tuple(s.name for s in statuses if s.state == "unchecked")
        stale_stores = tuple(s.name for s in statuses if s.state in ("stale_code_version", "stale_source_mark"))
        drifted = tuple(s.name for s in statuses if s.state in ("drifted", "error"))
        complete = bool(statuses) and all(s.fresh for s in statuses)
        return CoverageReport(
            consumer=consumer,
            expected=spec.required,
            complete=complete,
            stale=not complete,
            missing=missing,
            unchecked=unchecked,
            stale_stores=stale_stores,
            drifted=drifted,
            stores=tuple(statuses),
        )

    def is_stale(self, conn: sqlite3.Connection, consumer: str) -> bool:
        return self.coverage(conn, consumer).stale

    def doctor_targets(self, conn: sqlite3.Connection) -> list[DoctorTarget]:
        existing_names = [r["name"] for r in derived_store.all_rows(conn)] if derived_store.available(conn) else []
        targets: dict[str, tuple[Provider | None, bool, set[str]]] = {}
        active_by_family: dict[str, list[str]] = {}
        for provider in self.active_providers(conn):
            concrete = self._concrete_names_for(conn, provider, existing_names)
            if provider.is_family:
                active_by_family[provider.name] = list(concrete)
            for name in concrete:
                targets[name] = (provider, True, set())

        with self._lock:
            specs = [spec for spec in self._consumers.values() if spec.require_in_doctor]

        for spec in specs:
            for req in spec.required:
                if req.endswith(":"):
                    concrete = active_by_family.get(req)
                    if concrete:
                        for name in concrete:
                            targets[name][2].add(spec.consumer)
                    else:
                        provider = self.resolve(req)
                        entry = targets.setdefault(req, (provider, False, set()))
                        entry[2].add(spec.consumer)
                elif req in targets:
                    targets[req][2].add(spec.consumer)
                else:
                    provider = self.resolve(req)
                    entry = targets.setdefault(req, (provider, False, set()))
                    entry[2].add(spec.consumer)

        return [
            DoctorTarget(name=name, provider=provider, available=avail, required_by=tuple(sorted(consumers)))
            for name, (provider, avail, consumers) in sorted(targets.items())
        ]

    def _consumers_requiring(self, store_name: str) -> tuple[str, ...]:
        with self._lock:
            matched = [
                spec.consumer
                for spec in self._consumers.values()
                if spec.require_in_doctor
                and any(
                    req == store_name or (req.endswith(":") and store_name.startswith(req))
                    for req in spec.required
                )
            ]
            return tuple(sorted(matched))

    def check_store(
        self,
        conn: sqlite3.Connection,
        store_name: str,
        *,
        required_by: Sequence[str] | None = None,
    ) -> CheckOutcome:
        consumers = self._consumers_requiring(store_name) if required_by is None else tuple(required_by)
        prefix = f"incomplete coverage for {', '.join(consumers)}: " if consumers else ""
        if not derived_store.available(conn):
            return CheckOutcome(
                name=store_name,
                ok=False,
                diff_rows=1,
                sample_ids=(store_name,),
                detail=f"{prefix}derived table unavailable on this schema",
            )
        provider = self.resolve(store_name)
        if provider is None:
            return CheckOutcome(
                name=store_name,
                ok=False,
                diff_rows=1,
                sample_ids=(store_name,),
                detail=f"{prefix}provider not registered",
            )
        if not provider_available(conn, provider):
            return CheckOutcome(
                name=store_name,
                ok=False,
                diff_rows=1,
                sample_ids=(store_name,),
                detail=f"{prefix}source tables or producer unavailable",
            )
        if provider.is_family and store_name == provider.name:
            return CheckOutcome(
                name=store_name,
                ok=False,
                diff_rows=1,
                sample_ids=(store_name,),
                detail=f"{prefix}no concrete stores available for family",
            )
        conn.execute("SAVEPOINT derived_check")
        try:
            report = provider.diff(conn, store_name)
            if type(report.diff_rows) is not int or report.diff_rows < 0:
                raise ValueError(f"derived provider {store_name!r} returned invalid diff_rows {report.diff_rows!r}")
            mark = provider.source_mark(conn, store_name) if report.source_mark is None else report.source_mark
        finally:
            conn.execute("ROLLBACK TO SAVEPOINT derived_check")
            conn.execute("RELEASE SAVEPOINT derived_check")
        detail = report.detail or (
            f"{report.diff_rows} diff {'row' if report.diff_rows == 1 else 'rows'}"
            if report.diff_rows
            else "matches source"
        )
        return CheckOutcome(
            name=store_name,
            ok=report.diff_rows == 0,
            diff_rows=report.diff_rows,
            sample_ids=tuple(report.sample_ids),
            detail=detail,
            record=CheckRecord(
                name=store_name,
                code_version=provider.code_version,
                source_mark=mark,
                diff_rows=report.diff_rows,
            ),
        )

    def rebuild_store(
        self,
        store_name: str,
        *,
        emit_reset: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> dict:
        ctx = nullcontext(conn) if conn is not None else foundation.tx()
        with ctx as c:
            if not derived_store.available(c):
                raise UnavailableStoreError(f"derived table unavailable; cannot rebuild {store_name!r}")
            provider = self.resolve_active(c, store_name)
            if provider is None:
                raise UnavailableStoreError(f"derived provider {store_name!r} is not registered or unavailable")
            if provider.is_family and store_name == provider.name:
                raise UnavailableStoreError(f"cannot rebuild family prefix {store_name!r} without concrete target")
            existing = derived_store.get(store_name, c)
            old_version = existing["code_version"] if existing is not None else None
            new_mark = provider.rebuild(c, store_name)
            if new_mark is None:
                new_mark = provider.source_mark(c, store_name)
            row = derived_store.record_rebuilt(
                c,
                store_name,
                code_version=provider.code_version,
                source_mark=new_mark,
            )
            if emit_reset:
                seq = events.emit(
                    "reset",
                    f"derived:{store_name}",
                    cls="durable",
                    ver=provider.code_version,
                    data={
                        "reason": "derived.rebuilt",
                        "store": store_name,
                        "from_version": old_version,
                        "code_version": provider.code_version,
                        "source_mark": row["source_mark"],
                        "rebuilt": row["rebuilt"],
                    },
                    connection=c,
                )
                return {**row, "reset_seq": seq}
            return row

    def rebuild_stale_on_startup(
        self,
        *,
        contracts_waiting: bool = False,
        say: Callable[[str], None] | None = None,
    ) -> list[dict]:
        if contracts_waiting or not paths.DB_FILE.exists():
            return []
        candidates: list[tuple[str, Provider]] = []
        with foundation.reader() as c:
            if not derived_store.available(c):
                return []
            for row in derived_store.all_rows(c):
                provider = self.resolve_active(c, row["name"])
                if provider is None:
                    continue
                if provider.code_version > row["code_version"]:
                    candidates.append((row["name"], provider))
        if not candidates:
            return []
        rebuilt: list[dict] = []
        for store_name, provider in candidates:
            with foundation.tx() as c:
                if not derived_store.available(c) or not provider_available(c, provider):
                    continue
                current = derived_store.get(store_name, c)
                if current is None or current["code_version"] >= provider.code_version:
                    continue
                result = self.rebuild_store(store_name, emit_reset=True, conn=c)
            if say is not None:
                say(
                    f"derived:{store_name}: rebuilt from v{current['code_version']} to v{result['code_version']}"
                )
            rebuilt.append(result)
        return rebuilt

    def record_audit_results(self, results: Sequence[object]) -> int:
        records = [
            rec
            for item in results
            if isinstance((rec := getattr(item, "derived_check", None)), CheckRecord)
        ]
        if not records:
            return 0
        now = util.iso()
        with foundation.tx() as c:
            if not derived_store.available(c):
                return 0
            for rec in records:
                derived_store.record_checked(
                    c,
                    rec.name,
                    code_version=rec.code_version,
                    source_mark=rec.source_mark,
                    diff_rows=rec.diff_rows,
                    checked=now,
                )
            return len(records)


REGISTRY = Registry()


def register(provider: Provider, *, replace: bool = False) -> Provider:
    return REGISTRY.register(provider, replace=replace)


def register_if_available(conn: sqlite3.Connection, provider: Provider, *, replace: bool = False) -> bool:
    return REGISTRY.register_if_available(conn, provider, replace=replace)


def unregister(name: str) -> Provider | None:
    return REGISTRY.unregister(name)


def registered(*providers: Provider, replace: bool = True):
    return REGISTRY.registered(*providers, replace=replace)


def providers() -> tuple[Provider, ...]:
    return REGISTRY.providers()


def active_providers(conn: sqlite3.Connection) -> tuple[Provider, ...]:
    return REGISTRY.active_providers(conn)


def resolve(name: str) -> Provider | None:
    return REGISTRY.resolve(name)


def resolve_active(conn: sqlite3.Connection, name: str) -> Provider | None:
    return REGISTRY.resolve_active(conn, name)


def expect_coverage(consumer: str, required: Sequence[str], *, require_in_doctor: bool = True) -> ConsumerSpec:
    return REGISTRY.expect_coverage(consumer, required, require_in_doctor=require_in_doctor)


def clear_consumer(consumer: str) -> ConsumerSpec | None:
    return REGISTRY.clear_consumer(consumer)


def expected(consumer: str, required: Sequence[str], *, require_in_doctor: bool = True):
    return REGISTRY.expected(consumer, required, require_in_doctor=require_in_doctor)


def expected_coverage(consumer: str | None = None) -> dict[str, ConsumerSpec]:
    return REGISTRY.expected_coverage(consumer)


def required_doctor_names() -> tuple[str, ...]:
    return REGISTRY.required_doctor_names()


def store_status(conn: sqlite3.Connection, name: str) -> StoreStatus:
    return REGISTRY.store_status(conn, name)


def coverage(conn: sqlite3.Connection, consumer: str) -> CoverageReport:
    return REGISTRY.coverage(conn, consumer)


def is_stale(conn: sqlite3.Connection, consumer: str) -> bool:
    return REGISTRY.is_stale(conn, consumer)


def charts_stale(conn: sqlite3.Connection, consumer: str = "charts") -> bool:
    return REGISTRY.is_stale(conn, consumer)


def doctor_targets(conn: sqlite3.Connection) -> list[DoctorTarget]:
    return REGISTRY.doctor_targets(conn)


def check_store(
    conn: sqlite3.Connection,
    store_name: str,
    *,
    required_by: Sequence[str] | None = None,
) -> CheckOutcome:
    return REGISTRY.check_store(conn, store_name, required_by=required_by)


def rebuild_store(store_name: str, *, emit_reset: bool = False, conn: sqlite3.Connection | None = None) -> dict:
    return REGISTRY.rebuild_store(store_name, emit_reset=emit_reset, conn=conn)


def rebuild_stale_on_startup(
    *,
    contracts_waiting: bool = False,
    say: Callable[[str], None] | None = None,
) -> list[dict]:
    return REGISTRY.rebuild_stale_on_startup(contracts_waiting=contracts_waiting, say=say)


def record_audit_results(results: Sequence[object]) -> int:
    return REGISTRY.record_audit_results(results)
