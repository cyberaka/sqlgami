"""Reconciliation probes: simple anchor queries with a declared relation to the candidate's result.

A verifier that cannot write the whole production query can still write "total paid USD
revenue in June 2026" or "number of live tenants". Each such anchor comes with a relation
that ties it to the candidate's result:

    equals_row_count      the anchor's value is the number of rows the candidate returns
    equals_sum            ... the SUM of candidate column C (over rows matching an optional filter)
    equals_count_distinct ... the number of distinct values of candidate column C
    equals_value          ... the single value of candidate column C in the row(s) matching a filter
    equals_min / equals_max
    upper_bound           every value of candidate column C is <= the anchor's value
    lower_bound           every value of candidate column C is >= the anchor's value

sqlgami runs each probe under the same safety and cost rules as any alternative, evaluates
the relation against the candidate's result, and weighs it by how many independent sources
proposed the same probe target. A probe several sources agree on that the candidate violates
is strong evidence of wrong data; a satisfied probe adds modest support.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from sqlglot import exp

from . import analysis as A
from . import consistency as C
from .catalog import Catalog
from .engines.base import Adapter
from .errors import SqlgamiError
from .report import Finding, Severity

logger = logging.getLogger("sqlgami")

RELATIONS = (
    "equals_row_count",
    "equals_sum",
    "equals_count_distinct",
    "equals_value",
    "equals_min",
    "equals_max",
    "upper_bound",
    "lower_bound",
)
# Relations that name a candidate column.
_NEEDS_COLUMN = {r for r in RELATIONS if r != "equals_row_count"}

# (question, schema description, glossary or None, candidate output columns) -> probe specs
Prober = Callable[[str, str, "str | None", Sequence[str]], Sequence[Any]]

SATISFIED = "satisfied"
VIOLATED = "violated"
INVALID = "invalid"
FAILED = "failed"
SKIPPED = "skipped"
NOT_APPLICABLE = "not_applicable"


@dataclass
class ProbeSpec:
    sql: str
    relation: str
    column: str | None = None
    filter: dict[str, Any] | None = None  # {candidate column: value} equality filters on the candidate's rows
    description: str = ""
    source: str | None = None
    # Every source that proposed this same probe (the text is run once; the agreement counts).
    sources: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.source:
            self.sources.add(self.source)

    @property
    def target(self) -> tuple:
        """What the probe measures; two sources proposing the same target can agree or disagree."""
        filt = tuple(sorted((k.lower(), C._norm(v)) for k, v in (self.filter or {}).items()))
        return (self.relation, (self.column or "").lower(), filt)


@dataclass
class ProbeResult:
    spec: ProbeSpec
    status: str
    anchor: Any = None  # the value the probe returned
    observed: Any = None  # the candidate's value under the relation
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sql": self.spec.sql,
            "relation": self.spec.relation,
            "column": self.spec.column,
            "filter": self.spec.filter,
            "source": self.spec.source,
            "description": self.spec.description,
            "status": self.status,
            "anchor": _plain(self.anchor),
            "observed": _plain(self.observed),
            "detail": self.detail,
        }


@dataclass
class Outcome:
    ran: bool = False
    reason: str | None = None
    results: list[ProbeResult] = field(default_factory=list)
    satisfied: int = 0
    violated: int = 0
    # Per target: sources whose anchor values agree with one another, and the verdict.
    agreed_violations: int = 0  # targets where two or more sources agree on the anchor and the candidate violates it
    agreed_satisfied: int = 0
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "satisfied": self.satisfied,
            "violated": self.violated,
            "agreed_violations": self.agreed_violations,
            "agreed_satisfied": self.agreed_satisfied,
            "probes": [r.to_dict() for r in self.results],
        }


def _plain(value: Any) -> Any:
    value = C._norm(value)
    return value if isinstance(value, (int, float, str)) or value is None else str(value)


def parse_specs(items: Sequence[Any], source: str | None = None) -> list[ProbeSpec]:
    """Accept dicts, JSON text, or ProbeSpec objects; drop anything malformed."""
    specs: list[ProbeSpec] = []
    for item in items:
        if isinstance(item, ProbeSpec):
            specs.append(item)
            continue
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except ValueError:
                continue
        if isinstance(item, list):
            specs.extend(parse_specs(item, source))
            continue
        if not isinstance(item, dict):
            continue
        sql = item.get("probe_sql") or item.get("sql")
        relation = str(item.get("relation") or "").strip().lower()
        if not isinstance(sql, str) or not sql.strip() or relation not in RELATIONS:
            continue
        column = item.get("column") or item.get("candidate_column")
        if relation in _NEEDS_COLUMN and not isinstance(column, str):
            continue
        filt = item.get("filter")
        if filt is not None and not isinstance(filt, dict):
            continue
        specs.append(
            ProbeSpec(
                sql=sql.strip(),
                relation=relation,
                column=str(column).strip() if column else None,
                filter={str(k): v for k, v in (filt or {}).items()} or None,
                description=str(item.get("description") or item.get("fact") or ""),
                source=str(item.get("source") or source) if (item.get("source") or source) else None,
            )
        )
    return specs


def extract_json(text: str) -> list[Any]:
    """Pull a JSON array (or several objects) out of a model's reply."""
    if not text:
        return []
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    body = fenced.group(1) if fenced else text
    start = body.find("[")
    end = body.rfind("]")
    if start != -1 and end > start:
        try:
            parsed = json.loads(body[start : end + 1])
            return parsed if isinstance(parsed, list) else [parsed]
        except ValueError:
            pass
    found: list[Any] = []
    for match in re.finditer(r"\{.*?\}", body, re.DOTALL):
        try:
            found.append(json.loads(match.group(0)))
        except ValueError:
            continue
    return found


# ------------------------------------------------------------------ evaluation


def _column_index(columns: Sequence[str], name: str) -> int | None:
    wanted = name.strip().lower().strip('"`')
    lowered = [c.lower() for c in columns]
    if wanted in lowered:
        return lowered.index(wanted)
    loose = [re.sub(r"[^a-z0-9]", "", c) for c in lowered]
    key = re.sub(r"[^a-z0-9]", "", wanted)
    if key and loose.count(key) == 1:
        return loose.index(key)
    return None


def _same(a: Any, b: Any) -> bool:
    a, b = C._norm(a), C._norm(b)
    if a is None or b is None:
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-6)
    return str(a).strip().lower() == str(b).strip().lower()


def _number(value: Any) -> float | None:
    value = C._norm(value)
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def evaluate_relation(spec: ProbeSpec, anchor: Any, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> tuple[str, Any, str | None]:
    """``(SATISFIED | VIOLATED | NOT_APPLICABLE, observed value, detail)``."""
    selected = list(rows)
    if spec.filter:
        for name, value in spec.filter.items():
            index = _column_index(columns, name)
            if index is None:
                return NOT_APPLICABLE, None, f"the candidate has no column named {name}"
            selected = [r for r in selected if _same(r[index], value)]
        if not selected and spec.relation != "equals_row_count":
            return NOT_APPLICABLE, None, "no candidate row matches the probe's filter"
    if spec.relation == "equals_row_count":
        observed = len(selected)
        target = _number(anchor)
        if target is None:
            return NOT_APPLICABLE, observed, "the probe did not return a number"
        return (SATISFIED if _same(observed, target) else VIOLATED), observed, None
    index = _column_index(columns, spec.column or "")
    if index is None:
        return NOT_APPLICABLE, None, f"the candidate has no column named {spec.column}"
    values = [r[index] for r in selected]
    numbers = [n for n in (_number(v) for v in values if v is not None) if n is not None]
    if spec.relation == "equals_count_distinct":
        observed = len({C._norm(v) for v in values if v is not None})
        target = _number(anchor)
        if target is None:
            return NOT_APPLICABLE, observed, "the probe did not return a number"
        return (SATISFIED if _same(observed, target) else VIOLATED), observed, None
    if spec.relation == "equals_value":
        distinct = {C._norm(v) for v in values}
        if len(distinct) != 1:
            return NOT_APPLICABLE, None, f"the filter selects {len(selected)} rows with {len(distinct)} different values"
        observed = next(iter(distinct))
        return (SATISFIED if _same(observed, anchor) else VIOLATED), observed, None
    if not numbers:
        if spec.relation in ("equals_sum", "equals_min", "equals_max", "upper_bound", "lower_bound") and not values:
            return NOT_APPLICABLE, None, "the candidate returned no rows to compare"
        return NOT_APPLICABLE, None, "the column holds no numbers"
    target = _number(anchor)
    if target is None:
        return NOT_APPLICABLE, None, "the probe did not return a number"
    if spec.relation == "equals_sum":
        observed = sum(numbers)
        tolerance = max(1e-6, 0.005 * len(numbers))  # rounded cents and the like
        ok = math.isclose(observed, target, rel_tol=1e-6, abs_tol=tolerance)
        return (SATISFIED if ok else VIOLATED), observed, None
    if spec.relation == "equals_min":
        observed = min(numbers)
        return (SATISFIED if _same(observed, target) else VIOLATED), observed, None
    if spec.relation == "equals_max":
        observed = max(numbers)
        return (SATISFIED if _same(observed, target) else VIOLATED), observed, None
    if spec.relation == "upper_bound":
        observed = max(numbers)
        return (SATISFIED if observed <= target + 1e-6 else VIOLATED), observed, None
    if spec.relation == "lower_bound":
        observed = min(numbers)
        return (SATISFIED if observed >= target - 1e-6 else VIOLATED), observed, None
    return NOT_APPLICABLE, None, "unknown relation"


# ------------------------------------------------------------------ running probes


def run(
    base_columns: Sequence[str],
    base_rows: Sequence[Sequence[Any]],
    specs: Sequence[ProbeSpec],
    *,
    dialect: str,
    catalog: Catalog | None,
    adapter: Adapter,
    timeout_s: float = 15.0,
    budget_s: float | None = 30.0,
    cost_ceiling: float | None = None,
    max_probes: int = 12,
    allowed_functions: Sequence[str] = (),
    allowed_tables: Sequence[str] = (),
) -> Outcome:
    """Run each probe, evaluate its relation against the candidate's result, and weigh agreement."""
    import time

    outcome = Outcome()
    started = time.perf_counter()
    seen: dict[tuple[str, tuple], ProbeSpec] = {}
    unique: list[ProbeSpec] = []
    for spec in specs:
        key = (C._text_key(spec.sql), spec.target)
        first = seen.get(key)
        if first is not None:
            first.sources |= spec.sources  # the same probe from another source: one run, two voices
            continue
        seen[key] = spec
        unique.append(spec)
    unique = unique[:max_probes]
    if not unique:
        outcome.reason = "no usable probe was supplied"
        return outcome

    for spec in unique:
        result = ProbeResult(spec, INVALID)
        outcome.results.append(result)
        try:
            checked = A.analyse(spec.sql, catalog, dialect, allowed_functions, allowed_tables)
        except Exception as exc:
            result.detail = f"could not be analysed: {type(exc).__name__}"
            continue
        if not checked.is_query or checked.unsafe_functions or checked.script_statements or checked.unlisted_functions:
            result.detail = "not a single read-only query over known functions"
            continue
        if catalog is not None and (checked.unknown_tables or checked.unknown_columns):
            result.detail = "names a table or column that does not exist"
            continue
        if cost_ceiling is not None and checked.tables:
            total, known = 0, True
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
                result.status, result.detail = SKIPPED, "estimated cost is above the ceiling"
                continue
        remaining = timeout_s if budget_s is None else min(timeout_s, budget_s - (time.perf_counter() - started))
        if remaining <= 0.05:
            result.status, result.detail = SKIPPED, "the time allowed for probes ran out"
            continue
        try:
            executed = adapter.execute(spec.sql, max_rows=2, timeout_s=remaining)
        except SqlgamiError as exc:
            result.status, result.detail = INVALID, str(exc)[:160]
            continue
        if not executed.ok:
            result.status, result.detail = FAILED, (executed.error or "")[:160]
            continue
        if len(executed.rows) != 1 or len(executed.rows[0]) != 1:
            result.status = NOT_APPLICABLE
            result.detail = f"a probe must return one value; this returned {len(executed.rows)} row(s) of {len(executed.rows[0]) if executed.rows else 0} column(s)"
            continue
        result.anchor = executed.rows[0][0]
        status, observed, detail = evaluate_relation(spec, result.anchor, base_columns, base_rows)
        result.status, result.observed, result.detail = status, observed, detail
        if status == SATISFIED:
            outcome.satisfied += 1
        elif status == VIOLATED:
            outcome.violated += 1

    comparable = [r for r in outcome.results if r.status in (SATISFIED, VIOLATED)]
    outcome.ran = bool(comparable)
    if not outcome.ran:
        outcome.reason = "no probe could be run and compared"
        return outcome
    # Per target: which sources agree on the anchor's value.
    by_target: dict[tuple, list[ProbeResult]] = {}
    for result in comparable:
        by_target.setdefault(result.spec.target, []).append(result)
    for target, results in by_target.items():
        sources: set[str] = set()
        for r in results:
            sources |= r.spec.sources or {f"#{id(r)}"}
        values = [C._norm(r.anchor) for r in results]
        agree = len(sources) >= 2 and all(_same(v, values[0]) for v in values)
        if agree and all(r.status == VIOLATED for r in results):
            outcome.agreed_violations += 1
        elif agree and all(r.status == SATISFIED for r in results):
            outcome.agreed_satisfied += 1
    outcome.findings = _judge(outcome)
    return outcome


def _describe(result: ProbeResult) -> str:
    spec = result.spec
    what = spec.description or spec.sql[:120]
    rel = {
        "equals_row_count": "the number of rows",
        "equals_sum": f"the sum of {spec.column}",
        "equals_count_distinct": f"the number of distinct {spec.column}",
        "equals_value": f"the value of {spec.column}",
        "equals_min": f"the smallest {spec.column}",
        "equals_max": f"the largest {spec.column}",
        "upper_bound": f"an upper bound on {spec.column}",
        "lower_bound": f"a lower bound on {spec.column}",
    }[spec.relation]
    filt = f" where {', '.join(f'{k} = {v!r}' for k, v in spec.filter.items())}" if spec.filter else ""
    return f"{what!s} gives {_plain(result.anchor)!r}, but {rel}{filt} in this query's result is {_plain(result.observed)!r}"


def _judge(outcome: Outcome) -> list[Finding]:
    violated = [r for r in outcome.results if r.status == VIOLATED]
    if not violated:
        return []
    agreed = outcome.agreed_violations > 0
    sources: set[str] = set()
    for r in violated:
        sources |= r.spec.sources
    detail = "; ".join(_describe(r) for r in violated[:3])
    return [
        Finding(
            "reconciliation_mismatch",
            Severity.HIGH if agreed or len(sources) >= 2 else Severity.MEDIUM,
            f"{len(violated)} of {outcome.satisfied + len(violated)} anchor quer"
            f"{'y' if outcome.satisfied + len(violated) == 1 else 'ies'} disagree with this query's result: {detail}."
            + (" Two independent sources agree on the anchor's value." if agreed else ""),
            "reconciliation",
            suggestion="Compare the query with the anchor queries listed under signals.reconciliation.",
            evidence={
                "violated": len(violated),
                "satisfied": outcome.satisfied,
                "agreed_violations": outcome.agreed_violations,
                "sources": sorted(sources),
            },
        )
    ]
