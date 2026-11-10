"""SQLite adapter. Uses only the standard library."""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Iterable

from sqlglot import exp

from ..catalog import MATERIALIZED_VIEW, TABLE, VIEW, Column, ForeignKey, Table  # noqa: F401
from ..errors import AdapterError, ConnectionFailed
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

_NOT_ABOUT_THE_SQL = ("locked", "busy", "disk", "unable to open", "readonly", "out of memory", "malformed")


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class _Loader:
    def __init__(self, adapter: "SQLiteAdapter") -> None:
        self.adapter = adapter

    def _schemas(self) -> list[str]:
        rows = self.adapter._fetch("PRAGMA database_list")
        return [row[1] for row in rows] or ["main"]

    def list_tables(self) -> Iterable[tuple[str | None, str]]:
        found: list[tuple[str | None, str]] = []
        for schema in self._schemas():
            master = "sqlite_temp_master" if schema == "temp" else f"{_quote(schema)}.sqlite_master"
            try:
                rows = self.adapter._fetch(
                    f"SELECT name FROM {master} WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
                )
            except AdapterError:
                continue
            found.extend((schema, row[0]) for row in rows)
        return found

    def load_table(self, schema: str | None, name: str) -> Table | None:
        schema = schema or "main"
        master = "sqlite_temp_master" if schema == "temp" else f"{_quote(schema)}.sqlite_master"
        rows = self.adapter._fetch(
            f"SELECT type, sql FROM {master} WHERE name = ? AND type IN ('table', 'view')", (name,)
        )
        if not rows:
            return None
        kind, sql = rows[0]
        prefix = f"PRAGMA {_quote(schema)}."
        info = self.adapter._fetch(f"{prefix}table_xinfo({_quote(name)})")
        columns: list[Column] = []
        pk: list[tuple[int, str]] = []
        for _cid, col_name, col_type, notnull, _default, pk_pos, *rest in info:
            hidden = rest[0] if rest else 0
            if hidden == 1:
                continue  # hidden column of a virtual table
            columns.append(
                Column(
                    name=col_name,
                    type=col_type or "",
                    nullable=None if kind == "view" else not (notnull or pk_pos),
                )
            )
            if pk_pos:
                pk.append((pk_pos, col_name))
        table = Table(name=name, schema=schema, columns=columns)
        if kind == "view":
            table.kind = VIEW
            table.definition = sql
            return table
        table.primary_key = tuple(col for _, col in sorted(pk))
        for _seq, index_name, unique, *rest in self.adapter._fetch(f"{prefix}index_list({_quote(name)})"):
            partial = rest[1] if len(rest) > 1 else 0
            if not unique or partial:
                continue
            cols = [row[2] for row in self.adapter._fetch(f"{prefix}index_info({_quote(index_name)})")]
            if cols and all(c is not None for c in cols) and tuple(cols) != table.primary_key:
                table.unique_keys.append(tuple(cols))
        grouped: dict[int, list[tuple[int, str, str, str | None]]] = {}
        for row in self.adapter._fetch(f"{prefix}foreign_key_list({_quote(name)})"):
            fk_id, seq, ref_table, col_from, col_to = row[0], row[1], row[2], row[3], row[4]
            grouped.setdefault(fk_id, []).append((seq, ref_table, col_from, col_to))
        for parts in grouped.values():
            parts.sort()
            table.foreign_keys.append(
                ForeignKey(
                    columns=tuple(p[2] for p in parts),
                    ref_table=parts[0][1],
                    ref_columns=tuple(p[3] for p in parts if p[3] is not None),
                    ref_schema=schema,
                )
            )
        return table


class SQLiteAdapter(Adapter):
    engine = "sqlite"
    dialect = "sqlite"
    # Tables are counted only up to this many rows; beyond it the count reads as "at least".
    row_count_bound = 20_000_000

    def __init__(self, db: Any) -> None:
        self._owned = False
        if isinstance(db, sqlite3.Connection):
            self.conn = db
        elif isinstance(db, (str, bytes)) or hasattr(db, "__fspath__"):
            path = str(db if not hasattr(db, "__fspath__") else db.__fspath__())
            try:
                if path == ":memory:":
                    self.conn = sqlite3.connect(path)
                else:
                    uri = path if path.startswith("file:") else f"file:{path}?mode=ro"
                    self.conn = sqlite3.connect(uri, uri=True)
            except sqlite3.Error as exc:
                raise ConnectionFailed(f"could not open SQLite database {path!r}: {exc}") from exc
            self._owned = True
        else:
            raise ConnectionFailed("expected a sqlite3.Connection or a path to a database file")
        self._row_counts: dict[tuple[str | None, str], int | None] = {}

    def loader(self) -> _Loader:
        return _Loader(self)

    # ------------------------------------------------------------- internals

    def _fetch(self, sql: str, params: tuple = ()) -> list[tuple]:
        try:
            cursor = self.conn.execute(sql, params)
            try:
                return rows_as_tuples(cursor.fetchall())
            finally:
                cursor.close()
        except sqlite3.Error as exc:
            raise AdapterError(f"SQLite metadata query failed: {exc}") from exc

    def _guarded(self, sql: str, max_rows: int | None, timeout_s: float) -> tuple[list[str], list[tuple], bool]:
        """Run one statement with writes disabled and a time limit."""
        conn = self.conn
        deadline = time.monotonic() + timeout_s
        timed_out = False

        def watchdog() -> int:
            nonlocal timed_out
            if time.monotonic() > deadline:
                timed_out = True
                return 1
            return 0

        previous = conn.execute("PRAGMA query_only").fetchone()
        conn.execute("PRAGMA query_only = ON")
        conn.set_progress_handler(watchdog, 5000)
        try:
            cursor = conn.execute(sql)
            try:
                columns = [d[0] for d in cursor.description or []]
                if max_rows is None:
                    rows = cursor.fetchall()
                    truncated = False
                else:
                    rows = cursor.fetchmany(max_rows + 1)
                    truncated = len(rows) > max_rows
                    rows = rows[:max_rows]
                return columns, rows_as_tuples(rows), truncated
            finally:
                cursor.close()
        except sqlite3.Error as exc:
            if timed_out:
                raise _Timeout(str(exc)) from exc
            raise
        finally:
            conn.set_progress_handler(None, 0)
            if previous is not None and not previous[0]:
                conn.execute("PRAGMA query_only = OFF")

    @staticmethod
    def _error_kind(exc: Exception) -> str:
        if isinstance(exc, _Timeout):
            return TIMEOUT
        text = str(exc).lower()
        if any(marker in text for marker in _NOT_ABOUT_THE_SQL):
            return OTHER
        return INVALID

    # -------------------------------------------------------------- adapter

    def dry_run(self, sql: str) -> DryRunResult:
        assert_read_only(sql, self.dialect, self.allowed_functions, allowed_tables=self.allowed_tables)
        try:
            _, rows, _ = self._guarded(f"EXPLAIN QUERY PLAN {sql}", None, 10.0)
        except (sqlite3.Error, _Timeout) as exc:
            return DryRunResult(ok=False, error=str(exc), error_kind=self._error_kind(exc))
        plan = [str(row[-1]) for row in rows]
        return DryRunResult(ok=True, plan=plan)

    def execute(self, sql: str, *, max_rows: int, timeout_s: float) -> ExecutionResult:
        assert_read_only(sql, self.dialect, self.allowed_functions, allowed_tables=self.allowed_tables)
        started = time.perf_counter()
        try:
            columns, rows, truncated = self._guarded(sql, max_rows, timeout_s)
        except (sqlite3.Error, _Timeout) as exc:
            return ExecutionResult(
                ok=False,
                error=str(exc) if not isinstance(exc, _Timeout) else f"stopped after {timeout_s:g}s",
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
        except (sqlite3.Error, _Timeout) as exc:
            raise AdapterError(f"probe query failed: {exc}") from exc
        return rows

    def row_count(self, table: Table) -> int | None:
        """Rows in a table, counted up to a bound so a huge table costs a fixed amount."""
        key = (table.schema, table.name)
        if key not in self._row_counts:
            count: int | None = None
            if not table.is_view:
                schema = _quote(table.schema or "main")
                bound = self.row_count_bound
                try:
                    row = self._fetch(
                        f"SELECT COUNT(*) FROM (SELECT 1 FROM {schema}.{_quote(table.name)} LIMIT {bound + 1})"
                    )
                    count = int(row[0][0])
                except (AdapterError, TypeError, ValueError):
                    count = None
            self._row_counts[key] = count
        return self._row_counts[key]

    def function_exists(self, name: str) -> bool | None:
        try:
            rows = self._fetch(
                "SELECT 1 FROM pragma_function_list WHERE lower(name) = ? "
                "UNION ALL SELECT 1 FROM pragma_module_list WHERE lower(name) = ? LIMIT 1",
                (name.lower(), name.lower()),
            )
        except Exception:
            return None
        return bool(rows)

    def version(self) -> str | None:
        # The SQLite library in this process, which decides what syntax is accepted
        # (RIGHT and FULL JOIN, for example, need 3.39 or later).
        return sqlite3.sqlite_version

    def close(self) -> None:
        if self._owned:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self._owned = False


class _Timeout(Exception):
    pass
