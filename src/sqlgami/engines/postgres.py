"""Postgres adapter. Needs the optional ``psycopg`` package (``pip install sqlgami[postgres]``).

Every statement sqlgami sends runs inside its own transaction. When that
transaction is sqlgami's to open it is ``READ ONLY``, so even a function with
side effects cannot write. On a connection that is already inside the caller's
transaction, sqlgami works under a savepoint instead and leaves the caller's
transaction as it found it.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from sqlglot import exp

from ..catalog import MATERIALIZED_VIEW, VIEW, Catalog, Column, ForeignKey, Table
from ..errors import AdapterError, ConnectionFailed, MissingDependencyError
from .base import (
    INVALID,
    OTHER,
    TIMEOUT,
    Adapter,
    DryRunResult,
    ExecutionResult,
    assert_read_only,
    rows_as_tuples,
)

_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def _import_psycopg() -> Any:
    try:
        import psycopg  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise MissingDependencyError(
            "the Postgres adapter needs the 'psycopg' package: pip install sqlgami[postgres]"
        ) from exc
    return psycopg


def _error_kind(exc: Exception) -> str:
    state = getattr(exc, "sqlstate", None) or ""
    if state == "57014":  # query_canceled: the statement timeout fired
        return TIMEOUT
    if state == "42501":  # insufficient_privilege
        return OTHER
    if state[:2] in ("42", "22", "21", "2B", "0A", "44") or state in ("25006", "23505", "23503"):
        # syntax and name errors, data exceptions, cardinality violations, unsupported
        # features, and attempts to write inside a read-only transaction
        return INVALID
    return OTHER


def _message(exc: Exception) -> str:
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    text = primary or str(exc)
    hint = getattr(diag, "message_hint", None) if diag is not None else None
    if hint:
        text = f"{text} ({hint})"
    return " ".join(str(text).split())[:400]


class _Loader:
    def __init__(self, adapter: "PostgresAdapter") -> None:
        self.adapter = adapter

    def list_tables(self) -> Iterable[tuple[str | None, str]]:
        rows = self.adapter._fetch(
            "SELECT n.nspname, c.relname FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast') "
            "AND n.nspname NOT LIKE 'pg\\_temp\\_%' AND n.nspname NOT LIKE 'pg\\_toast\\_temp\\_%'"
        )
        return [(row[0], row[1]) for row in rows]

    def load_table(self, schema: str | None, name: str) -> Table | None:
        fetch = self.adapter._fetch
        head = fetch(
            "SELECT c.oid, c.relkind, c.reltuples, "
            "CASE WHEN c.relkind IN ('v', 'm') THEN pg_catalog.pg_get_viewdef(c.oid, true) END "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')",
            (schema, name),
        )
        if not head:
            return None
        oid, relkind, reltuples, definition = head[0]
        is_view = relkind in ("v", "m")
        columns = [
            Column(name=row[0], type=row[1] or "", nullable=None if is_view else not row[2])
            for row in fetch(
                "SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull "
                "FROM pg_catalog.pg_attribute a WHERE a.attrelid = %s AND a.attnum > 0 "
                "AND NOT a.attisdropped ORDER BY a.attnum",
                (oid,),
            )
        ]
        table = Table(name=name, schema=schema, columns=columns)
        if is_view:
            table.kind = MATERIALIZED_VIEW if relkind == "m" else VIEW
            table.definition = (definition or "").strip().rstrip(";")
            return table

        for is_primary, names in fetch(
            "SELECT i.indisprimary, ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum, ord) "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum ORDER BY k.ord) "
            "FROM pg_catalog.pg_index i WHERE i.indrelid = %s AND i.indisunique AND i.indisvalid "
            "AND i.indpred IS NULL AND i.indexprs IS NULL",
            (oid,),
        ):
            key = tuple(names or ())
            if not key:
                continue
            if is_primary:
                table.primary_key = key
            elif key not in table.unique_keys:
                table.unique_keys.append(key)
        for cols, ref_schema, ref_table, ref_cols in fetch(
            "SELECT ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord) "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum ORDER BY k.ord), "
            "rn.nspname, rc.relname, "
            "ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(attnum, ord) "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum ORDER BY k.ord) "
            "FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class rc ON rc.oid = con.confrelid "
            "JOIN pg_catalog.pg_namespace rn ON rn.oid = rc.relnamespace "
            "WHERE con.conrelid = %s AND con.contype = 'f'",
            (oid,),
        ):
            table.foreign_keys.append(
                ForeignKey(columns=tuple(cols), ref_table=ref_table, ref_columns=tuple(ref_cols), ref_schema=ref_schema)
            )

        if relkind == "p":
            # A partitioned parent holds no rows itself; its size is the sum of its partitions.
            parts = fetch(
                "SELECT SUM(GREATEST(c.reltuples, 0)), BOOL_AND(c.reltuples >= 0) "
                "FROM pg_catalog.pg_inherits i JOIN pg_catalog.pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = %s",
                (oid,),
            )
            if parts and parts[0][0] is not None and parts[0][1]:
                table.row_count = int(parts[0][0])
        elif reltuples is not None and reltuples >= 0:
            table.row_count = int(reltuples)
        return table


class PostgresAdapter(Adapter):
    engine = "postgres"
    dialect = "postgres"

    def __init__(self, db: Any) -> None:
        psycopg = _import_psycopg()
        self._psycopg = psycopg
        self._owned = False
        if isinstance(db, str):
            try:
                self.conn = psycopg.connect(db, autocommit=True)
            except psycopg.Error as exc:
                raise ConnectionFailed(f"could not connect to Postgres: {_message(exc)}") from exc
            self._owned = True
        elif isinstance(db, psycopg.Connection):
            self.conn = db
        else:
            raise ConnectionFailed("expected a psycopg connection or a connection string")
        self._version: str | None = None

    def loader(self) -> _Loader:
        return _Loader(self)

    def catalog(self, **hints: Any) -> Catalog:
        if "search_path" not in hints:
            hints["search_path"] = self.search_path()
        return Catalog(self.dialect, loader=self.loader(), **hints)

    def search_path(self) -> list[str]:
        """Schemas the session resolves unqualified names against, in order."""
        try:
            rows = self._fetch("SELECT pg_catalog.current_schemas(false)")
        except AdapterError:
            return ["public"]
        schemas = list(rows[0][0] or []) if rows else []
        return schemas or ["public"]

    # ------------------------------------------------------------- internals

    def _idle(self) -> bool:
        return self.conn.info.transaction_status == self._psycopg.pq.TransactionStatus.IDLE

    def _fetch(self, sql: str, params: tuple = ()) -> list[tuple]:
        psycopg = self._psycopg
        try:
            with self.conn.transaction():
                with self.conn.cursor() as cursor:
                    cursor.execute(sql, params or None)  # None: a literal % needs no escaping
                    return rows_as_tuples(cursor.fetchall())
        except psycopg.Error as exc:
            raise AdapterError(f"Postgres metadata query failed: {_message(exc)}") from exc

    def _guarded(self, sql: str, max_rows: int | None, timeout_s: float) -> tuple[list[str], list[tuple], bool]:
        """Run one statement in its own (sub)transaction with a time limit, read-only where possible."""
        owns_transaction = self._idle()
        timeout_ms = max(1, int(timeout_s * 1000))
        with self.conn.transaction(force_rollback=True):
            with self.conn.cursor() as setup:
                if owns_transaction:
                    setup.execute("SET TRANSACTION READ ONLY")
                setup.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            if max_rows is None:
                with self.conn.cursor() as cursor:
                    cursor.execute(sql)
                    columns = [d.name for d in cursor.description or []]
                    return columns, rows_as_tuples(cursor.fetchall()), False
            # A server-side cursor, so a huge result is never pulled into memory.
            with self.conn.cursor(name="sqlgami_cursor") as cursor:
                cursor.execute(sql)
                rows = cursor.fetchmany(max_rows + 1)
                columns = [d.name for d in cursor.description or []]
                truncated = len(rows) > max_rows
                return columns, rows_as_tuples(rows[:max_rows]), truncated

    # -------------------------------------------------------------- adapter

    def dry_run(self, sql: str) -> DryRunResult:
        assert_read_only(sql, self.dialect, self.allowed_functions)
        try:
            _, rows, _ = self._guarded(f"EXPLAIN (FORMAT JSON) {sql}", None, 15.0)
        except self._psycopg.Error as exc:
            return DryRunResult(ok=False, error=_message(exc), error_kind=_error_kind(exc))
        plan = rows[0][0] if rows else None
        return DryRunResult(ok=True, plan=plan)

    def execute(self, sql: str, *, max_rows: int, timeout_s: float) -> ExecutionResult:
        assert_read_only(sql, self.dialect, self.allowed_functions)
        started = time.perf_counter()
        try:
            columns, rows, truncated = self._guarded(sql, max_rows, timeout_s)
        except self._psycopg.Error as exc:
            kind = _error_kind(exc)
            return ExecutionResult(
                ok=False,
                error=f"stopped after {timeout_s:g}s" if kind == TIMEOUT else _message(exc),
                error_kind=kind,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        return ExecutionResult(
            ok=True,
            columns=columns,
            rows=rows,
            truncated=truncated,
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    def query(self, expression: exp.Expression, *, max_rows: int = 1000, timeout_s: float = 10.0) -> list[tuple]:
        sql = expression.sql(dialect=self.dialect)
        assert_read_only(sql, self.dialect, check_functions=False)  # built by sqlgami itself
        try:
            _, rows, _ = self._guarded(sql, max_rows, timeout_s)
        except self._psycopg.Error as exc:
            raise AdapterError(f"probe query failed: {_message(exc)}") from exc
        return rows

    def row_count(self, table: Table) -> int | None:
        """The planner's row estimate. Unknown until the table has been analysed."""
        return table.row_count

    def function_exists(self, name: str) -> bool | None:
        try:
            rows = self._fetch("SELECT 1 FROM pg_catalog.pg_proc WHERE proname = %s LIMIT 1", (name.lower(),))
        except Exception:
            return None
        return bool(rows)

    def version(self) -> str | None:
        if self._version is None:
            try:
                number = int(self.conn.info.server_version)
                self._version = f"{number // 10000}.{number % 10000}"
            except Exception:
                self._version = ""
        return self._version or None

    def close(self) -> None:
        if self._owned:
            try:
                self.conn.close()
            except Exception:
                pass
            self._owned = False
