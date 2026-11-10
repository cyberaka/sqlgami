"""Run SQL on a bench database and compare result sets across seeds and engines.

Comparison is positional, and ignores row order unless the question asks for an
order. When both results use the same set of column names, the columns are
first put in the same order by name, so a candidate that lists the right
columns in another order is not wrong. Values are brought to a common form
first, because engines hand back the same data differently: SQLite returns a
timestamp as text and a boolean as 0/1, DuckDB returns a ``datetime`` and
``True``.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

_TIMESTAMP = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?\s*(Z|[-+]\d{2}(?::?\d{2})?)?$"
)


@dataclass
class Outcome:
    ok: bool
    rows: list[tuple] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    error: str | None = None


TIMEOUT_S = 60.0  # a statement that runs longer than this is reported as a timeout, not as wrong
TIMED_OUT = "timed out"


GRACE_S = 3.0  # after the timeout, how long DuckDB gets to honour the interrupt before the statement is abandoned


class Abandoned(Exception):
    """The engine ignored the interrupt; the connection is unusable and must be dropped."""


def run_sql(conn: Any, sql: str, max_rows: int = 100_000, timeout_s: float = TIMEOUT_S) -> Outcome:
    """Execute a statement on a read-only bench connection, within a time limit.

    SQLite stops through its progress handler and Postgres through statement_timeout, both
    reliable. DuckDB is asked to stop with interrupt(), which it has been seen to lose, so the
    statement runs on a worker thread and is asked again every quarter second for GRACE_S;
    if it still runs, :class:`Abandoned` is raised and the caller must drop the connection.
    """
    if hasattr(conn, "native_run"):
        return conn.native_run(sql)  # a BigQuery seed in the sandbox
    module = type(conn).__module__ or ""
    if "duckdb" in module:
        return _run_duckdb(conn, sql, max_rows, timeout_s)
    stop = _deadline(conn, timeout_s)
    try:
        return _fetch(conn, sql, max_rows)
    except Exception as exc:  # any engine error is a result here, not a crash
        return _failure(exc, stop.expired, timeout_s)
    finally:
        stop.cancel()


def _fetch(conn: Any, sql: str, max_rows: int) -> Outcome:
    cursor = conn.execute(sql)
    columns = [d[0] for d in cursor.description or []]
    rows = [tuple(r) for r in cursor.fetchmany(max_rows)]
    if hasattr(cursor, "close") and cursor is not conn:
        cursor.close()
    return Outcome(True, rows, columns)


def _failure(exc: Exception, expired: bool, timeout_s: float) -> Outcome:
    text = " ".join(str(exc).split())
    if expired or "interrupt" in text.lower() or "statement timeout" in text.lower() or "canceling statement" in text.lower():
        return Outcome(False, error=f"{TIMED_OUT} after {timeout_s:g}s")
    return Outcome(False, error=text[:300])


def _run_duckdb(conn: Any, sql: str, max_rows: int, timeout_s: float) -> Outcome:
    done = threading.Event()
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["outcome"] = _fetch(conn, sql, max_rows)
        except BaseException as exc:  # noqa: BLE001 - reported on the calling thread
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=work, name="benchkit-duckdb", daemon=True).start()
    expired = not done.wait(timeout_s)
    if expired:
        deadline = time.monotonic() + GRACE_S
        while not done.is_set() and time.monotonic() < deadline:
            _interrupt(conn)
            done.wait(0.25)
        if not done.is_set():

            def nag() -> None:
                while not done.wait(1.0):
                    _interrupt(conn)

            threading.Thread(target=nag, name="benchkit-duckdb-abandoned", daemon=True).start()
            raise Abandoned(f"{TIMED_OUT} after {timeout_s:g}s and the engine did not stop")
    if "error" in box:
        return _failure(box["error"], expired, timeout_s)
    return box["outcome"]


def _interrupt(conn: Any) -> None:
    try:
        conn.interrupt()
    except Exception:
        pass


class _Deadline:
    def __init__(self) -> None:
        self.expired = False
        self._restore: Any = None

    def cancel(self) -> None:
        if self._restore is not None:
            self._restore()


def _deadline(conn: Any, timeout_s: float) -> _Deadline:
    """Arrange for ``conn`` to give up after ``timeout_s`` seconds (SQLite, Postgres)."""
    stop = _Deadline()
    module = type(conn).__module__ or ""
    if module.startswith("sqlite3"):
        started = time.monotonic()

        def handler() -> int:
            if time.monotonic() - started > timeout_s:
                stop.expired = True
                return 1
            return 0

        conn.set_progress_handler(handler, 10_000)
        stop._restore = lambda: conn.set_progress_handler(None, 0)
    elif module.startswith("psycopg"):
        try:
            conn.execute(f"SET statement_timeout = {int(timeout_s * 1000)}")
        except Exception:
            pass
    return stop


def _timestamp_text(value: dt.datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def canonical(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        number = float(value)
        if math.isnan(number):
            return "NaN"
        if number.is_integer() and abs(number) < 1e15:
            return int(number)
        return number
    if isinstance(value, dt.datetime):
        return _timestamp_text(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    if isinstance(value, str):
        match = _TIMESTAMP.match(value)
        if match:
            try:
                text = value.strip().replace(" ", "T", 1)
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                offset = match.group(8)
                if offset and offset != "Z" and len(offset) == 3:
                    text += ":00"
                return _timestamp_text(dt.datetime.fromisoformat(text))
            except ValueError:
                return value
        if value[:1] in "{[":
            try:
                return json.dumps(json.loads(value), sort_keys=True, separators=(",", ":"))
            except ValueError:
                return value
        return value
    return str(value)


def _sort_key(row: tuple) -> tuple:
    key = []
    for value in row:
        if value is None:
            key.append((0, 0, ""))
        elif isinstance(value, (int, float)):
            key.append((1, round(float(value), 6), ""))
        else:
            key.append((2, 0, str(value)))
    return tuple(key)


def _cells_equal(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-6)
    return a == b


def column_order_by_name(a_columns: Sequence[str] | None, b_columns: Sequence[str] | None) -> list[int] | None:
    """The one permutation of ``b``'s columns that lines their names up with ``a``'s, or ``None``.

    Only when both sides name every column once and use the same set of names.
    """
    if not a_columns or not b_columns or len(a_columns) != len(b_columns):
        return None
    left = [str(c).lower() for c in a_columns]
    right = [str(c).lower() for c in b_columns]
    if len(set(left)) != len(left) or len(set(right)) != len(right) or set(left) != set(right):
        return None
    order = [right.index(name) for name in left]
    return None if order == list(range(len(order))) else order


def same_rows(
    a: Sequence[tuple],
    b: Sequence[tuple],
    ordered: bool = False,
    a_columns: Sequence[str] | None = None,
    b_columns: Sequence[str] | None = None,
) -> tuple[bool, str]:
    """Whether two result sets hold the same data. Returns ``(equal, reason)``."""
    if len(a) != len(b):
        return False, f"{len(a)} rows against {len(b)}"
    left = [tuple(canonical(v) for v in row) for row in a]
    right = [tuple(canonical(v) for v in row) for row in b]
    order = column_order_by_name(a_columns, b_columns)
    if order is not None:
        right = [tuple(row[i] for i in order) for row in right]
    if left and right and len(left[0]) != len(right[0]):
        return False, f"{len(left[0])} columns against {len(right[0])}"
    if not ordered:
        left.sort(key=_sort_key)
        right.sort(key=_sort_key)
    for index, (row_a, row_b) in enumerate(zip(left, right)):
        if len(row_a) != len(row_b):
            return False, f"row {index}: column counts differ"
        for column, (cell_a, cell_b) in enumerate(zip(row_a, row_b)):
            if not _cells_equal(cell_a, cell_b):
                return False, f"row {index} column {column}: {cell_a!r} against {cell_b!r}"
    return True, ""


def same_outcome(a: Outcome, b: Outcome, ordered: bool = False) -> tuple[bool, str]:
    if not a.ok or not b.ok:
        if not a.ok and not b.ok:
            return False, "both fail to run"
        return False, f"one fails to run: {a.error or b.error}"
    return same_rows(a.rows, b.rows, ordered, a.columns, b.columns)
