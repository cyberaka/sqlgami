"""DuckDB adapter. Needs the optional ``duckdb`` package (``pip install sqlgami[duckdb]``)."""

from __future__ import annotations

import threading
import time
from typing import Any, Iterable

from sqlglot import exp

from ..catalog import VIEW, Column, ForeignKey, Table
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

_INVALID_ERRORS = (
    "BinderException",
    "CatalogException",
    "ParserException",
    "SyntaxException",
    "ConversionException",
    "InvalidInputException",
    "InvalidTypeException",
    "TypeMismatchException",
    "OutOfRangeException",
    "NotImplementedException",
)


def _import_duckdb() -> Any:
    try:
        import duckdb  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise MissingDependencyError(
            "the DuckDB adapter needs the 'duckdb' package: pip install sqlgami[duckdb]"
        ) from exc
    return duckdb


class _Loader:
    def __init__(self, adapter: "DuckDBAdapter") -> None:
        self.adapter = adapter

    def list_tables(self) -> Iterable[tuple[str | None, str]]:
        rows = self.adapter._fetch(
            "SELECT schema_name, table_name FROM duckdb_tables() "
            "WHERE NOT internal AND database_name = current_database() "
            "UNION ALL "
            "SELECT schema_name, view_name FROM duckdb_views() "
            "WHERE NOT internal AND database_name = current_database()"
        )
        return [(row[0], row[1]) for row in rows]

    def load_table(self, schema: str | None, name: str) -> Table | None:
        schema = schema or "main"
        fetch = self.adapter._fetch
        column_rows = fetch(
            "SELECT column_name, data_type, is_nullable FROM duckdb_columns() "
            "WHERE database_name = current_database() AND schema_name = ? AND table_name = ? "
            "ORDER BY column_index",
            (schema, name),
        )
        columns = [Column(name=r[0], type=r[1] or "", nullable=bool(r[2])) for r in column_rows]
        view_rows = fetch(
            "SELECT sql FROM duckdb_views() "
            "WHERE database_name = current_database() AND schema_name = ? AND view_name = ?",
            (schema, name),
        )
        if view_rows:
            for column in columns:
                column.nullable = None
            return Table(name=name, schema=schema, columns=columns, kind=VIEW, definition=view_rows[0][0])
        table_rows = fetch(
            "SELECT estimated_size FROM duckdb_tables() "
            "WHERE database_name = current_database() AND schema_name = ? AND table_name = ?",
            (schema, name),
        )
        if not table_rows:
            return None
        table = Table(name=name, schema=schema, columns=columns)
        if table_rows[0][0] is not None:
            table.row_count = int(table_rows[0][0])
        for kind, cols, ref_table, ref_cols in fetch(
            "SELECT constraint_type, constraint_column_names, referenced_table, referenced_column_names "
            "FROM duckdb_constraints() "
            "WHERE database_name = current_database() AND schema_name = ? AND table_name = ?",
            (schema, name),
        ):
            cols = tuple(cols or ())
            if kind == "PRIMARY KEY":
                table.primary_key = cols
            elif kind == "UNIQUE" and cols:
                table.unique_keys.append(cols)
            elif kind == "FOREIGN KEY" and cols and ref_table:
                table.foreign_keys.append(
                    ForeignKey(columns=cols, ref_table=ref_table, ref_columns=tuple(ref_cols or ()), ref_schema=schema)
                )
        for is_unique, expressions in fetch(
            "SELECT is_unique, expressions FROM duckdb_indexes() "
            "WHERE database_name = current_database() AND schema_name = ? AND table_name = ?",
            (schema, name),
        ):
            if not is_unique or not expressions:
                continue
            parts = [p.strip().strip('"') for p in str(expressions).strip("[]").split(",")]
            known = {c.name for c in columns}
            if parts and all(p in known for p in parts) and tuple(parts) not in table.unique_keys:
                if tuple(parts) != table.primary_key:
                    table.unique_keys.append(tuple(parts))
        return table


class DuckDBAdapter(Adapter):
    engine = "duckdb"
    dialect = "duckdb"

    def __init__(self, db: Any) -> None:
        duckdb = _import_duckdb()
        self._duckdb = duckdb
        self._owned = False
        if isinstance(db, (str, bytes)) or hasattr(db, "__fspath__"):
            path = str(db if not hasattr(db, "__fspath__") else db.__fspath__())
            try:
                self.conn = duckdb.connect(path, read_only=path != ":memory:")
            except duckdb.Error as exc:
                raise ConnectionFailed(f"could not open DuckDB database {path!r}: {exc}") from exc
            self._owned = True
        elif hasattr(db, "cursor") and hasattr(db, "execute"):
            # A separate cursor shares the database but not the caller's result state.
            try:
                self.conn = db.cursor()
            except duckdb.Error as exc:
                raise ConnectionFailed(f"could not use the DuckDB connection: {exc}") from exc
        else:
            raise ConnectionFailed("expected a DuckDB connection or a path to a database file")
        self._lock = threading.RLock()

    def loader(self) -> _Loader:
        return _Loader(self)

    # ------------------------------------------------------------- internals

    def _fetch(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            try:
                return rows_as_tuples(self.conn.execute(sql, params).fetchall())
            except self._duckdb.Error as exc:
                raise AdapterError(f"DuckDB metadata query failed: {exc}") from exc

    def _guarded(self, sql: str, max_rows: int | None, timeout_s: float) -> tuple[list[str], list[tuple], bool]:
        timed_out = threading.Event()

        def interrupt() -> None:
            timed_out.set()
            try:
                self.conn.interrupt()
            except Exception:
                pass

        timer = threading.Timer(timeout_s, interrupt)
        timer.daemon = True
        with self._lock:
            timer.start()
            try:
                cursor = self.conn.execute(sql)
                columns = [d[0] for d in cursor.description or []]
                if max_rows is None:
                    rows = cursor.fetchall()
                    truncated = False
                else:
                    rows = cursor.fetchmany(max_rows + 1)
                    truncated = len(rows) > max_rows
                    rows = rows[:max_rows]
                return columns, rows_as_tuples(rows), truncated
            except self._duckdb.Error as exc:
                if timed_out.is_set():
                    raise _Timeout(str(exc)) from exc
                raise
            finally:
                timer.cancel()

    def _error_kind(self, exc: Exception) -> str:
        if isinstance(exc, _Timeout):
            return TIMEOUT
        names = {cls.__name__ for cls in type(exc).__mro__}
        return INVALID if names & set(_INVALID_ERRORS) else OTHER

    # -------------------------------------------------------------- adapter

    def dry_run(self, sql: str) -> DryRunResult:
        assert_read_only(sql, self.dialect)
        try:
            _, rows, _ = self._guarded(f"EXPLAIN {sql}", None, 10.0)
        except (self._duckdb.Error, _Timeout) as exc:
            return DryRunResult(ok=False, error=_first_lines(str(exc)), error_kind=self._error_kind(exc))
        plan = "\n".join(str(row[-1]) for row in rows)
        return DryRunResult(ok=True, plan=plan)

    def execute(self, sql: str, *, max_rows: int, timeout_s: float) -> ExecutionResult:
        assert_read_only(sql, self.dialect)
        started = time.perf_counter()
        try:
            columns, rows, truncated = self._guarded(sql, max_rows, timeout_s)
        except (self._duckdb.Error, _Timeout) as exc:
            return ExecutionResult(
                ok=False,
                error=f"stopped after {timeout_s:g}s" if isinstance(exc, _Timeout) else _first_lines(str(exc)),
                error_kind=self._error_kind(exc),
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
        assert_read_only(sql, self.dialect)
        try:
            _, rows, _ = self._guarded(sql, max_rows, timeout_s)
        except (self._duckdb.Error, _Timeout) as exc:
            raise AdapterError(f"probe query failed: {_first_lines(str(exc))}") from exc
        return rows

    def close(self) -> None:
        try:
            self.conn.close()  # closes our own cursor; a borrowed connection stays open
        except Exception:
            pass


def _first_lines(text: str, limit: int = 400) -> str:
    text = " ".join(part.strip() for part in text.strip().splitlines() if part.strip())
    return text if len(text) <= limit else text[: limit - 3] + "..."


class _Timeout(Exception):
    pass
