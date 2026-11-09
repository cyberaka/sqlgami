"""Execution agreement: do independently written queries for the same question return the same data?

Static checks judge a query by its shape. This stage judges it by its answer.
The host supplies other attempts at the same question (or a function that
writes them); each is run read-only under the same limits as the candidate,
and the result sets are compared.

* The candidate matches most of the alternatives: strong support.
* The alternatives match one another but not the candidate: strong doubt.
* Nothing matches anything: the question is probably ambiguous.

How much this is worth depends on how independent and how capable the
alternatives are. Alternatives from the same model and prompt as the candidate
tend to share its mistakes.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Sequence

from . import analysis as A
from .catalog import Catalog
from .engines.base import Adapter
from .errors import SqlgamiError
from .report import Finding, Severity

logger = logging.getLogger("sqlgami")

# (question, schema description, glossary or None, how many) -> SQL statements
Generator = Callable[[str, str, "str | None", int], Sequence[str]]

AGREES = "agrees"
AGREES_REORDERED = "agrees_columns_reordered"
DIFFERS = "differs"
INVALID = "invalid"  # not a runnable read-only query against this schema
FAILED = "failed"  # the engine could not run it
SKIPPED = "skipped"  # too costly, or its result was cut off


@dataclass
class Alternative:
    sql: str
    status: str
    rows: int | None = None
    detail: str | None = None
    cluster: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "status": self.status,
            "rows": self.rows,
            "detail": self.detail,
            "cluster": self.cluster,
        }


@dataclass
class Outcome:
    ran: bool = False
    reason: str | None = None
    alternatives: list[Alternative] = field(default_factory=list)
    comparable: int = 0  # alternatives whose results could be compared
    agreeing: int = 0
    largest_other_cluster: int = 0  # size of the biggest group of alternatives that disagree with the candidate
    findings: list[Finding] = field(default_factory=list)

    @property
    def agreement(self) -> float | None:
        return self.agreeing / self.comparable if self.comparable else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparable": self.comparable,
            "agreeing": self.agreeing,
            "agreement": None if self.agreement is None else round(self.agreement, 4),
            "largest_disagreeing_cluster": self.largest_other_cluster,
            "alternatives": [a.to_dict() for a in self.alternatives],
        }


def _norm(value: Any) -> Any:
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
        return float(f"{number:.9g}")
    if isinstance(value, _dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return value.isoformat(timespec="seconds")
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, str):
        return value
    return repr(value)


def _key(value: Any) -> tuple:
    if value is None:
        return (0, "")
    if isinstance(value, (int, float)):
        return (1, f"{float(value):+.9e}")
    return (2, str(value))


def fingerprint(rows: Sequence[Sequence[Any]]) -> tuple[tuple, tuple]:
    """Two order-insensitive signatures of a result set.

    The first keeps column positions. The second also ignores column order, so
    ``SELECT a, b`` and ``SELECT b, a`` match.
    """
    normed = [tuple(_norm(v) for v in row) for row in rows]
    strict = tuple(sorted(normed, key=lambda r: tuple(_key(v) for v in r)))
    loose_rows = [tuple(sorted(row, key=_key)) for row in normed]
    loose = tuple(sorted(loose_rows, key=lambda r: tuple(_key(v) for v in r)))
    return strict, loose


def describe_schema(catalog: Catalog, focus: Sequence[str] = (), limit: int = 150) -> str:
    """A compact text description of the schema, for a generator's prompt."""
    names = catalog.table_names()
    lines: list[str] = []
    if len(names) > limit:
        wanted = set(focus)
        names = [n for n in names if n in wanted] or names[:limit]
    for qualified in names:
        schema, _, name = qualified.rpartition(".")
        table = catalog.resolve(name, schema or None, quoted=True) or catalog.resolve(name, schema or None)
        if table is None:
            continue
        if table.is_view:
            columns = ", ".join(c.name for c in table.columns) or "..."
            lines.append(f"VIEW {qualified} ({columns})")
            continue
        parts = []
        for column in table.columns:
            flags = "" if column.nullable is not False else " NOT NULL"
            parts.append(f"{column.name} {column.type}{flags}".strip())
        if table.primary_key:
            parts.append(f"PRIMARY KEY ({', '.join(table.primary_key)})")
        for fk in table.foreign_keys:
            parts.append(f"FOREIGN KEY ({', '.join(fk.columns)}) REFERENCES {fk.ref_table} ({', '.join(fk.ref_columns)})")
        lines.append(f"TABLE {qualified} ({', '.join(parts)})")
    return "\n".join(lines)


def run(
    sql: str,
    alternatives: Sequence[str],
    *,
    dialect: str,
    catalog: Catalog | None,
    adapter: Adapter,
    max_rows: int = 5000,
    timeout_s: float = 15.0,
    cost_ceiling: float | None = None,
    max_alternatives: int = 8,
) -> Outcome:
    """Run the candidate and each alternative, and compare what they return."""
    outcome = Outcome()
    seen: set[str] = set()
    unique: list[str] = []
    own = " ".join(sql.split()).rstrip(";").lower()
    for alt in alternatives:
        if not isinstance(alt, str) or not alt.strip():
            continue
        text = " ".join(alt.split()).rstrip(";").lower()
        if text == own or text in seen:
            continue  # the candidate again, or a repeat: no independent evidence
        seen.add(text)
        unique.append(alt)
    unique = unique[:max_alternatives]
    if not unique:
        outcome.reason = "no alternative differs from the candidate's own text"
        return outcome

    base = adapter.execute(sql, max_rows=max_rows, timeout_s=timeout_s)
    if not base.ok:
        outcome.reason = f"the candidate could not be run for comparison: {base.error}"
        return outcome
    if base.truncated:
        outcome.reason = f"the candidate returns more than {max_rows} rows, too many to compare"
        return outcome
    base_strict, base_loose = fingerprint(base.rows)

    clusters: dict[tuple, int] = {}
    cluster_sizes: dict[int, int] = {}
    for alt in unique:
        entry = Alternative(sql=alt, status=INVALID)
        outcome.alternatives.append(entry)
        try:
            checked = A.analyse(alt, catalog, dialect)
        except Exception as exc:  # an alternative must never break scoring
            entry.detail = f"could not be analysed: {type(exc).__name__}"
            continue
        if not checked.is_query or checked.unsafe_functions or checked.script_statements:
            entry.detail = "not a single read-only query"
            continue
        if catalog is not None and (checked.unknown_tables or checked.unknown_columns):
            entry.detail = "names a table or column that does not exist"
            continue
        if cost_ceiling is not None and checked.tables:
            total = 0
            known = True
            for table in checked.tables.values():
                try:
                    count = adapter.row_count(table)
                except SqlgamiError:
                    count = None
                if count is None:
                    known = False
                    break
                total += count
            if known and total > cost_ceiling:
                entry.status, entry.detail = SKIPPED, "estimated cost is above the ceiling"
                continue
        try:
            result = adapter.execute(alt, max_rows=max_rows, timeout_s=timeout_s)
        except SqlgamiError as exc:
            entry.status, entry.detail = INVALID, str(exc)[:160]
            continue
        if not result.ok:
            entry.status, entry.detail = FAILED, (result.error or "")[:160]
            continue
        entry.rows = len(result.rows)
        if result.truncated:
            entry.status, entry.detail = SKIPPED, "result too large to compare"
            continue
        strict, loose = fingerprint(result.rows)
        outcome.comparable += 1
        if strict == base_strict:
            entry.status = AGREES
            outcome.agreeing += 1
        elif loose == base_loose:
            entry.status = AGREES_REORDERED
            outcome.agreeing += 1
        else:
            entry.status = DIFFERS
            number = clusters.setdefault(loose, len(clusters) + 1)
            entry.cluster = number
            cluster_sizes[number] = cluster_sizes.get(number, 0) + 1
            entry.detail = _difference(base.rows, result.rows)

    outcome.ran = outcome.comparable > 0
    if not outcome.ran:
        outcome.reason = "no alternative could be run and compared"
        return outcome
    outcome.largest_other_cluster = max(cluster_sizes.values(), default=0)
    outcome.findings = _judge(outcome)
    return outcome


def _difference(base: Sequence[Sequence[Any]], other: Sequence[Sequence[Any]]) -> str:
    if len(base) != len(other):
        return f"returns {len(other)} rows where the candidate returns {len(base)}"
    if base and other and len(base[0]) != len(other[0]):
        return f"returns {len(other[0])} columns where the candidate returns {len(base[0])}"
    return "same shape, different values"


def _judge(outcome: Outcome) -> list[Finding]:
    n, agree, rival = outcome.comparable, outcome.agreeing, outcome.largest_other_cluster
    evidence = {"comparable": n, "agreeing": agree, "largest_disagreeing_cluster": rival}
    if agree * 2 >= n and agree >= rival:
        return []  # the candidate is in the largest group: support, expressed through the score
    if rival >= 2 and rival > agree:
        return [
            Finding(
                "consistency_disagreement",
                Severity.HIGH,
                f"{rival} of {n} independently written queries for this question return the same result as "
                f"one another, and it is not what this query returns"
                + (f" ({agree} agree with this query)." if agree else "."),
                "consistency",
                suggestion="Compare this query with the alternatives listed under signals.consistency.",
                evidence=evidence,
            )
        ]
    if n == 1:
        return [
            Finding(
                "consistency_disagreement",
                Severity.MEDIUM,
                "The one independently written query for this question returns a different result. "
                "One of the two is wrong, or the question can be read two ways.",
                "consistency",
                evidence=evidence,
            )
        ]
    return [
        Finding(
            "consistency_no_consensus",
            Severity.MEDIUM,
            f"{n} independently written queries for this question were compared and there is no clear "
            f"majority among their results ({agree} match this query). The question may be ambiguous.",
            "consistency",
            suggestion="Tighten the question, or review the alternatives listed under signals.consistency.",
            evidence=evidence,
        )
    ]
