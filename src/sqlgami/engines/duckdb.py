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

    # After the timeout, how long the engine gets to honour the interrupt (asked repeatedly)
    # before the cursor is abandoned and the caller gets its answer anyway.
    GRACE_S = 3.0

    def __init__(self, db: Any) -> None:
        duckdb = _import_duckdb()
        self._duckdb = duckdb
        self._owned = False
        if isinstance(db, (str, bytes)) or hasattr(db, "__fspath__"):
            path = str(db if not hasattr(db, "__fspath__") else db.__fspath__())
            try:
                self._source = duckdb.connect(path, read_only=path != ":memory:")
            except duckdb.Error as exc:
                raise ConnectionFailed(f"could not open DuckDB database {path!r}: {exc}") from exc
            self._owned = True
        elif hasattr(db, "cursor") and hasattr(db, "execute"):
            self._source = db
        else:
            raise ConnectionFailed("expected a DuckDB connection or a path to a database file")
        # Work on a cursor of the source: it shares the database but not the caller's result
        # state, and it can be dropped for a fresh one if the engine ever ignores an interrupt.
        try:
            self.conn = self._source.cursor()
        except duckdb.Error as exc:
            raise ConnectionFailed(f"could not use the DuckDB connection: {exc}") from exc
        self._lock = threading.RLock()
        self._abandoned: list[Any] = []

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
        """Run ``sql`` under a wall-clock bound the caller can rely on.

        The statement runs on a worker thread. Past ``timeout_s`` the engine is asked to stop,
        again every quarter second for ``GRACE_S`` in case a request is lost (DuckDB drops an
        interrupt that lands between statements, and has been seen to ignore one inside a
        recursive CTE). If it still has not stopped, the cursor is abandoned: the caller gets a
        timeout now, a fresh cursor serves the next statement, and the old one is interrupted in
        the background until it gives up.
        """
        with self._lock:
            conn = self.conn
            done = threading.Event()
            box: dict[str, Any] = {}

            def work() -> None:
                try:
                    box["result"] = self._run(conn, sql, max_rows)
                except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
                    box["error"] = exc
                finally:
                    done.set()

            worker = threading.Thread(target=work, name="sqlgami-duckdb", daemon=True)
            worker.start()
            if done.wait(timeout_s):
                return self._unbox(box, None)
            deadline = time.monotonic() + self.GRACE_S
            while not done.is_set() and time.monotonic() < deadline:
                _interrupt(conn)
                done.wait(0.25)
            if done.is_set():
                return self._unbox(box, f"stopped after {timeout_s:g}s")
            self._abandon(conn, done)
            raise _Timeout(
                f"not stopped after {timeout_s:g}s; the statement was abandoned and the engine keeps being asked to drop it"
            )

    @staticmethod
    def _run(conn: Any, sql: str, max_rows: int | None) -> tuple[list[str], list[tuple], bool]:
        if max_rows is None:
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description or []]
            return columns, rows_as_tuples(cursor.fetchall()), False
        relation = conn.sql(sql)
        columns = list(relation.columns)
        # DuckDB needs the pytz package to hand time-zone-aware values to Python.
        # Reading those columns as text avoids depending on it.
        projection = ", ".join(
            f"CAST(#{i} AS VARCHAR)" if "TIME ZONE" in str(t).upper() else f"#{i}"
            for i, t in enumerate(relation.types, 1)
        )
        rows = relation.project(projection).limit(max_rows + 1).fetchall()
        truncated = len(rows) > max_rows
        return columns, rows_as_tuples(rows[:max_rows]), truncated

    def _unbox(self, box: dict[str, Any], timed_out: str | None) -> tuple[list[str], list[tuple], bool]:
        if "error" in box:
            exc = box["error"]
            if timed_out and isinstance(exc, self._duckdb.Error):
                raise _Timeout(timed_out) from exc
            raise exc
        return box["result"]

    def _abandon(self, conn: Any, done: threading.Event) -> None:
        """Give up on a cursor that ignores interrupts; keep asking it to stop in the background."""
        self._abandoned.append(conn)

        def nag() -> None:
            while not done.wait(1.0):
                _interrupt(conn)

        threading.Thread(target=nag, name="sqlgami-duckdb-abandoned", daemon=True).start()
        try:
            self.conn = self._source.cursor()
        except Exception as exc:  # the database itself is gone; later statements will say so
            raise AdapterError(f"could not reopen the DuckDB connection: {exc}") from exc

    def _error_kind(self, exc: Exception) -> str:
        if isinstance(exc, _Timeout):
            return TIMEOUT
        if "failed to import" in str(exc):
            return OTHER  # a missing Python module on the host, nothing to do with the SQL
        names = {cls.__name__ for cls in type(exc).__mro__}
        return INVALID if names & set(_INVALID_ERRORS) else OTHER

    # -------------------------------------------------------------- adapter

    def dry_run(self, sql: str) -> DryRunResult:
        assert_read_only(sql, self.dialect, self.allowed_functions)
        try:
            _, rows, _ = self._guarded(f"EXPLAIN {sql}", None, 10.0)
        except (self._duckdb.Error, _Timeout) as exc:
            return DryRunResult(ok=False, error=_first_lines(str(exc)), error_kind=self._error_kind(exc))
        plan = "\n".join(str(row[-1]) for row in rows)
        return DryRunResult(ok=True, plan=plan)

    def execute(self, sql: str, *, max_rows: int, timeout_s: float) -> ExecutionResult:
        assert_read_only(sql, self.dialect, self.allowed_functions)
        started = time.perf_counter()
        try:
            columns, rows, truncated = self._guarded(sql, max_rows, timeout_s)
        except (self._duckdb.Error, _Timeout) as exc:
            return ExecutionResult(
                ok=False,
                error=str(exc) if isinstance(exc, _Timeout) else _first_lines(str(exc)),
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
        assert_read_only(sql, self.dialect, check_functions=False)  # built by sqlgami itself
        try:
            _, rows, _ = self._guarded(sql, max_rows, timeout_s)
        except (self._duckdb.Error, _Timeout) as exc:
            raise AdapterError(f"probe query failed: {_first_lines(str(exc))}") from exc
        return rows

    def function_exists(self, name: str) -> bool | None:
        try:
            rows = self._fetch(
                "SELECT 1 FROM duckdb_functions() WHERE lower(function_name) = ? LIMIT 1", (name.lower(),)
            )
        except Exception:
            return None
        return bool(rows)

    def version(self) -> str | None:
        return getattr(self._duckdb, "__version__", None)

    def close(self) -> None:
        try:
            self.conn.close()  # closes our own cursor; a borrowed connection stays open
        except Exception:
            pass
        # An abandoned cursor may still be running; closing the database under it is not safe,
        # so a connection we opened ourselves is left to the process in that (rare) case.
        if self._owned and not self._abandoned:
            try:
                self._source.close()
            except Exception:
                pass


def _interrupt(conn: Any) -> None:
    try:
        conn.interrupt()
    except Exception:
        pass


def _first_lines(text: str, limit: int = 400) -> str:
    text = " ".join(part.strip() for part in text.strip().splitlines() if part.strip())
    return text if len(text) <= limit else text[: limit - 3] + "..."


class _Timeout(Exception):
    pass
