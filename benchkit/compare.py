"""Run SQL on a bench database and compare result sets across seeds and engines.

Comparison ignores column names always, and row order unless the question asks
for an order. Values are brought to a common form first, because engines hand
back the same data differently: SQLite returns a timestamp as text and a
boolean as 0/1, DuckDB returns a ``datetime`` and ``True``.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
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


def run_sql(conn: Any, sql: str, max_rows: int = 100_000) -> Outcome:
    """Execute a statement on a read-only bench connection."""
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        rows = [tuple(r) for r in cursor.fetchmany(max_rows)]
        if hasattr(cursor, "close") and cursor is not conn:
            cursor.close()
        return Outcome(True, rows, columns)
    except Exception as exc:  # any engine error is a result here, not a crash
        return Outcome(False, error=" ".join(str(exc).split())[:300])


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


def same_rows(a: Sequence[tuple], b: Sequence[tuple], ordered: bool = False) -> tuple[bool, str]:
    """Whether two result sets hold the same data. Returns ``(equal, reason)``."""
    if len(a) != len(b):
        return False, f"{len(a)} rows against {len(b)}"
    left = [tuple(canonical(v) for v in row) for row in a]
    right = [tuple(canonical(v) for v in row) for row in b]
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
    return same_rows(a.rows, b.rows, ordered)
