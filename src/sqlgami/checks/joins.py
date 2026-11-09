"""Checks about joins: repeated rows, missing join conditions, collapsed outer joins."""

from __future__ import annotations

import re
from typing import Iterable

from sqlglot import exp

from ..model import MAYBE, NO, YES, SelectModel, flatten_and, strip, walk_own
from ..report import Finding, Severity
from .base import PROBE_UNIQUE, CheckContext, Probe, check

_CROSS_JOIN_RE = re.compile(r"\bcross\s+join\b", re.IGNORECASE)

# Aggregates whose result does not change when input rows are repeated.
_REPEAT_SAFE = tuple(
    getattr(exp, name)
    for name in (
        "Min",
        "Max",
        "AnyValue",
        "LogicalAnd",
        "LogicalOr",
        "BitwiseAndAgg",
        "BitwiseOrAgg",
        "ApproxDistinct",
        "ArgMax",
        "ArgMin",
        "First",
        "Last",
        "FirstValue",
        "LastValue",
        "ArrayUniqueAgg",
    )
    if hasattr(exp, name)
)


def _is_distinct(agg: exp.AggFunc) -> bool:
    return isinstance(agg.this, exp.Distinct) or bool(agg.args.get("distinct"))


def _group_pins_one_row(model: SelectModel) -> bool:
    """True when GROUP BY contains a key of a source the joins do not repeat.

    Each group then holds one row, so aggregates cannot be inflated.
    """
    group = model.group
    if group is None:
        return False
    grouped: dict[str, set[str]] = {}
    for expr in group.expressions:
        inner = strip(expr)
        if isinstance(inner, exp.Column) and inner.table in model.sources:
            grouped.setdefault(inner.table, set()).add(inner.name)
    for alias, columns in grouped.items():
        if model.multiplied(alias).state != NO:
            continue
        if any(key <= columns for key in model.key_info(alias).keys):
            return True
    return False


def _repeat_state(model: SelectModel, aliases: Iterable[str]) -> tuple[str, list[str], list[str]]:
    """Combine the per-source answers for one aggregate.

    The aggregate is safe when at least one of the sources it reads is not
    repeated: the joined rows are then at that source's grain.
    """
    aliases = list(aliases)
    states = {alias: model.multiplied(alias) for alias in aliases}
    if not states or any(m.state == NO for m in states.values()):
        return NO, [], []
    culprits: list[str] = []
    for m in states.values():
        for culprit in m.culprits:
            if culprit not in culprits:
                culprits.append(culprit)
    state = YES if all(m.state == YES for m in states.values()) else MAYBE
    return state, culprits, aliases


def _uniqueness_probes(
    ctx: CheckContext, model: SelectModel, finding: Finding, culprits: list[str], aliases: list[str]
) -> None:
    """Ask the data whether the join columns of each repeating source are in fact unique."""
    visited = set(aliases)
    for culprit in culprits:
        source = model.sources.get(culprit)
        if source is None or source.table is None:
            return  # cannot probe a derived source; leave the finding as it is
    for culprit in culprits:
        source = model.sources[culprit]
        columns = sorted(model.bound_columns(culprit, visited | (set(model.sources) - {culprit})))
        if not columns:
            return
        ctx.probes.append(Probe(PROBE_UNIQUE, source.table, tuple(columns), finding))


@check("fan_out", description="An aggregate reads rows that a join has repeated.")
def fan_out(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        if len(model.sources) < 2:
            continue
        aggregates = [a for a in model.own_aggregates() if not isinstance(a, _REPEAT_SAFE)]
        if not aggregates or _group_pins_one_row(model):
            continue
        reported: set[tuple[str, tuple[str, ...]]] = set()
        for agg in aggregates:
            if _is_distinct(agg):
                continue
            aliases = sorted({c.table for c in model.local_columns(agg)})
            counts_rows = not aliases
            if counts_rows:
                aliases = sorted(model.sources)
            state, culprits, read = _repeat_state(model, aliases)
            if state == NO:
                continue
            key = ("rows" if counts_rows else ",".join(read), tuple(culprits))
            if key in reported:
                continue
            reported.add(key)
            by = ", ".join(model.sources[c].label for c in culprits) or "another table"
            if counts_rows:
                message = (
                    f"{ctx.snippet(agg)} counts joined rows, and every table in this join is "
                    f"repeated by another one ({by}), so the count is of row combinations "
                    "rather than of any one thing."
                )
                severity = Severity.HIGH if state == YES else Severity.LOW
            else:
                read_label = ", ".join(model.sources[a].label for a in read)
                if state == YES:
                    message = (
                        f"{ctx.snippet(agg)} reads {read_label}, but the join to {by} can match "
                        f"several rows for each row of {read_label}, so the same value is "
                        "counted more than once."
                    )
                    severity = Severity.MEDIUM if isinstance(agg, exp.Count) else Severity.HIGH
                else:
                    message = (
                        f"{ctx.snippet(agg)} reads {read_label}; the join to {by} may repeat "
                        "those rows. No key is declared on the join columns, so this could "
                        "not be confirmed."
                    )
                    severity = Severity.LOW
            finding = ctx.finding(
                "fan_out",
                severity,
                message,
                agg,
                suggestion=(
                    "Aggregate each table at its own grain first (in a subquery or CTE), "
                    "then join the results."
                ),
                reads=read,
                repeated_by=culprits,
                certainty=state,
            )
            _uniqueness_probes(ctx, model, finding, culprits, read)
            yield finding


@check("distinct_masking", description="DISTINCT is used to hide rows repeated by a join.")
def distinct_masking(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        if len(model.sources) < 2:
            continue
        for agg in model.own_aggregates():
            if not _is_distinct(agg) or not isinstance(agg, (exp.Sum, exp.Avg)):
                continue
            aliases = sorted({c.table for c in model.local_columns(agg)})
            state, culprits, read = _repeat_state(model, aliases)
            if state == NO:
                continue
            finding = ctx.finding(
                "distinct_masking",
                Severity.HIGH if state == YES else Severity.LOW,
                (
                    f"{ctx.snippet(agg)} uses DISTINCT to undo rows repeated by the join, but "
                    "DISTINCT works on values: two different rows with the same value are "
                    "merged into one, so the total comes out too low."
                ),
                agg,
                suggestion="Aggregate before joining instead of applying DISTINCT inside the aggregate.",
                reads=read,
                repeated_by=culprits,
                certainty=state,
            )
            _uniqueness_probes(ctx, model, finding, culprits, read)
            yield finding

        select = model.select
        distinct = select.args.get("distinct")
        if distinct is None or distinct.args.get("on") is not None or model.is_aggregated:
            continue
        repeated = [a for a in model.sources if model.multiplied(a).state == YES]
        if not repeated:
            continue
        projected: dict[str, set[str]] = {}
        for projection in select.expressions:
            for column in model.local_columns(projection):
                projected.setdefault(column.table, set()).add(column.name)
        has_key = any(
            key <= projected.get(alias, set())
            for alias in model.sources
            for key in model.key_info(alias).keys
            if key
        )
        if has_key or not projected:
            continue
        yield ctx.finding(
            "distinct_masking",
            Severity.LOW,
            (
                "SELECT DISTINCT is removing rows repeated by a join, and no key column is "
                "selected, so different rows that share the selected values are merged too."
            ),
            suggestion="Select a key column, or filter with EXISTS instead of joining.",
            repeated=repeated,
        )


@check("cartesian_join", description="Tables are combined with no condition linking them.")
def cartesian_join(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        if len(model.sources) < 2:
            continue
        groups = model.components()
        if len(groups) < 2:
            continue
        real = []
        for group in groups:
            members = [
                a
                for a in group
                if not model.is_single_row(a) and model.sources[a].kind != "function"
            ]
            if members:
                real.append(sorted(group))
        if len(real) < 2:
            continue
        explicit = bool(_CROSS_JOIN_RE.search(ctx.analysis.sql)) and all(
            source.join is None or not source.join.args.get("on") for source in model.sources.values()
        )
        labels = [" + ".join(model.sources[a].label for a in group) for group in real]
        yield ctx.finding(
            "cartesian_join",
            Severity.LOW if explicit else Severity.HIGH,
            (
                "No condition links " + " to ".join(labels) + ", so every row of one is paired "
                "with every row of the other."
                + (" The query says CROSS JOIN, so this may be intended." if explicit else "")
            ),
            suggestion="Add the join condition, or confirm that every combination is wanted.",
            groups=real,
        )


# ------------------------------------------------------------------ outer joins

_NULL_TOLERANT = tuple(
    getattr(exp, name)
    for name in ("Coalesce", "Case", "If", "Nvl2", "Is", "Or", "NullSafeEQ", "NullSafeNEQ", "Exists")
    if hasattr(exp, name)
)
_COMPARISONS = tuple(
    getattr(exp, name)
    for name in (
        "EQ",
        "NEQ",
        "GT",
        "GTE",
        "LT",
        "LTE",
        "Like",
        "ILike",
        "SimilarTo",
        "RegexpLike",
        "Glob",
    )
    if hasattr(exp, name)
)


def _null_when_source_null(node: exp.Expression, alias: str) -> bool:
    """True when ``node`` evaluates to NULL whenever ``alias`` supplied no row."""
    columns = [c for c in walk_own(node) if isinstance(c, exp.Column) and c.table == alias]
    if not columns:
        return False
    return not any(isinstance(n, _NULL_TOLERANT) for n in walk_own(node))


def _rejects_null(expr: exp.Expression, alias: str) -> bool:
    """True when a predicate cannot be true for a row where ``alias`` is all NULL."""
    while isinstance(expr, exp.Paren):
        expr = expr.this
    if isinstance(expr, exp.And):
        return _rejects_null(expr.this, alias) or _rejects_null(expr.expression, alias)
    if isinstance(expr, exp.Or):
        return _rejects_null(expr.this, alias) and _rejects_null(expr.expression, alias)
    if isinstance(expr, exp.Not):
        inner = expr.this
        while isinstance(inner, exp.Paren):
            inner = inner.this
        if isinstance(inner, exp.Is):
            return isinstance(inner.expression, exp.Null) and _null_when_source_null(inner.this, alias)
        if isinstance(inner, _COMPARISONS + (exp.In, exp.Between)):
            return _rejects_null(inner, alias)
        return False
    if isinstance(expr, exp.Is):
        if isinstance(expr.expression, exp.Null):
            return False
        return _null_when_source_null(expr.this, alias)
    if isinstance(expr, _COMPARISONS):
        return _null_when_source_null(expr.this, alias) or _null_when_source_null(expr.expression, alias)
    if isinstance(expr, (exp.In, exp.Between)):
        return _null_when_source_null(expr.this, alias)
    if isinstance(expr, exp.Column):
        return expr.table == alias
    return False


@check("outer_join_nullified", description="A filter turns an outer join into an inner join.")
def outer_join_nullified(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        for alias, source in model.sources.items():
            if not source.null_supplied:
                continue
            culprits: list[exp.Expression] = []
            via_join = False
            for conjunct in model.conjuncts:
                if alias not in conjunct.aliases:
                    continue
                if conjunct.origin == "where":
                    if _rejects_null(conjunct.expr, alias):
                        culprits.append(conjunct.expr)
                elif not conjunct.outer_join and alias not in conjunct.join_aliases:
                    if _rejects_null(conjunct.expr, alias):
                        culprits.append(conjunct.expr)
                        via_join = True
            if not culprits:
                continue
            side = (source.join.side or "outer").upper() if source.join is not None else "OUTER"
            where = "a later inner join" if via_join else "the WHERE clause"
            yield ctx.finding(
                "outer_join_nullified",
                Severity.MEDIUM,
                (
                    f"{source.label} is outer-joined so that rows without a match are kept, but "
                    f"{where} tests {ctx.snippet(culprits[0])}, which is never true for those "
                    "rows. They are dropped, and the join behaves like an inner join."
                ),
                culprits[0],
                suggestion=(
                    "Move the condition into the ON clause of the outer join, or allow NULL "
                    f"explicitly, if unmatched rows should be kept. (Join side: {side}.)"
                ),
                source=alias,
                predicates=[ctx.snippet(c) for c in culprits],
            )


@check("natural_join", description="NATURAL JOIN joins on whatever column names happen to match.")
def natural_join(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        for record in model.joins:
            join = record.join
            if (join.method or "").upper() == "NATURAL":
                yield ctx.finding(
                    "natural_join",
                    Severity.MEDIUM,
                    (
                        "NATURAL JOIN matches on every column name the two tables share, which "
                        "often includes columns such as id, name or created_at that were not "
                        "meant as join keys."
                    ),
                    join,
                    suggestion="Write the join condition out with ON.",
                )
