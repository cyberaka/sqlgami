"""Checks about joins: repeated rows, missing join conditions, collapsed outer joins."""

from __future__ import annotations

import re
from typing import Iterable

from sqlglot import exp

from ..model import MAYBE, NO, YES, SelectModel, flatten_and, strip, walk_own  # noqa: F401
from ..report import Finding, Severity
from .base import PROBE_QUERY, PROBE_UNIQUE, CheckContext, Probe, check

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
    states = {alias: model.multiplied(alias, per_group=True) for alias in aliases}
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
        columns = sorted(model.bound_columns(culprit, visited | (set(model.sources) - {culprit}), per_group=True))
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
        if all(source.kind == "function" for source in model.sources.values()):
            continue  # generate_series and friends: combining them is the point
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
            Severity.INFO,
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
    for written in ctx.analysis.natural_joins:
        text = " ".join(written.split())
        yield Finding(
            "natural_join",
            Severity.MEDIUM,
            (
                "NATURAL JOIN matches on every column name the two tables share, which "
                "often includes columns such as id, name or created_at that were not "
                "meant as join keys."
            ),
            "static",
            text if len(text) <= 160 else text[:157] + "...",
            "Write the join condition out with ON.",
        )


# ------------------------------------------------------------ repeated rows


@check("duplicate_rows", description="A plain listing returns the same row several times because of a join.")
def duplicate_rows(ctx: CheckContext) -> Iterable[Finding]:
    root = ctx.analysis.root_scope
    if root is None or not isinstance(root.expression, exp.Select):
        return
    model = ctx.cache.model(root)
    select = model.select
    if len(model.sources) < 2 or model.is_aggregated or select.args.get("distinct") is not None:
        return
    if any(isinstance(n, exp.Window) for p in select.expressions for n in walk_own(p)):
        return
    projected = sorted({c.table for p in select.expressions for c in model.local_columns(p)})
    if not projected:
        return
    state, culprits, read = _repeat_state(model, projected)
    if state != YES:
        return
    shown = ", ".join(model.sources[a].label for a in read)
    by = ", ".join(model.sources[c].label for c in culprits)
    finding = ctx.finding(
        "duplicate_rows",
        Severity.LOW,
        f"Only columns of {shown} are returned, but the join to {by} can match several rows for "
        f"each of them, so the same {shown} row is listed once per match.",
        suggestion="Use SELECT DISTINCT, or test for a match with EXISTS instead of joining.",
        shown=read,
        repeated_by=culprits,
    )
    _uniqueness_probes(ctx, model, finding, culprits, read)
    yield finding


# ------------------------------------------------- rows an inner join drops


def _unmatched_probe(model: SelectModel, entity: str, other: str) -> exp.Expression | None:
    """A query that returns a row if some ``entity`` row has no partner in ``other``."""
    a, b = model.sources[entity], model.sources[other]
    if a.table is None or b.table is None:
        return None
    inner: list[exp.Expression] = []
    outer: list[exp.Expression] = []
    for conjunct in model.conjuncts:
        if conjunct.expr.find(exp.Select) is not None:
            return None
        aliases = conjunct.aliases
        if aliases <= {entity, other} and other in aliases:
            inner.append(conjunct.expr.copy())
        elif aliases == frozenset({entity}):
            outer.append(conjunct.expr.copy())
        elif aliases:
            return None
    if not inner:
        return None
    partner = exp.select(exp.Literal.number(1)).from_(b.node.copy())
    for condition in inner:
        partner = partner.where(condition)
    query = exp.select(exp.Literal.number(1)).from_(a.node.copy())
    for condition in outer:
        query = query.where(condition)
    return query.where(exp.Not(this=exp.Exists(this=partner))).limit(1)


@check("inner_join_drops_rows", description="An inner join leaves out things that have no related rows.")
def inner_join_drops_rows(ctx: CheckContext) -> Iterable[Finding]:
    """``... FROM tenants t JOIN disputes d ... GROUP BY t.id``: tenants with no disputes vanish.

    Whether that matters depends on the question ("for every tenant ..."), so this
    is informational on its own; the question stage raises it when the wording asks
    for every one.
    """
    root = ctx.analysis.root_scope
    if root is None or not isinstance(root.expression, exp.Select):
        return
    model = ctx.cache.model(root)
    group = model.group
    if group is None or len(model.sources) != 2:
        return
    grouped = {strip(e).table for e in group.expressions if isinstance(strip(e), exp.Column)}
    grouped &= set(model.sources)
    if len(grouped) != 1:
        return
    entity = next(iter(grouped))
    other = next(a for a in model.sources if a != entity)
    aggregates = model.own_aggregates()
    if not aggregates:
        return
    for agg in aggregates:
        tables = {c.table for c in model.local_columns(agg)}
        if tables - {other}:
            return
    if model.sources[other].null_supplied or model.sources[entity].null_supplied:
        return  # already an outer join
    if model.multiplied(entity).state == NO:
        return  # at most one partner each: nothing is being counted
    finding = ctx.finding(
        "inner_join_drops_rows",
        Severity.INFO,
        f"{model.sources[entity].label} is inner-joined to {model.sources[other].label} and then "
        f"aggregated, so any {model.sources[entity].label} row with no match is missing from the "
        "result instead of appearing with a zero.",
        suggestion="Use a LEFT JOIN (with COALESCE for sums) if every row should appear.",
        entity=entity,
        other=other,
        entity_table=model.sources[entity].table.name if model.sources[entity].table is not None else entity,
        limited=model.select.args.get("limit") is not None,
        counts_only=all(isinstance(a, exp.Count) for a in aggregates),
    )
    query = _unmatched_probe(model, entity, other)
    if query is not None:
        ctx.probes.append(
            Probe(
                PROBE_QUERY,
                model.sources[entity].table,
                (),
                finding,
                query=query,
                on_rows=(Severity.LOW, " Checked against the data: some rows do have no match, and are dropped.", "unmatched"),
                on_empty=(None, " Checked against the data: every row has a match at present.", "all_matched"),
            )
        )
    yield finding


# ------------------------------------------------------- order among ties

_ORDER_SENSITIVE = tuple(
    getattr(exp, name)
    for name in ("RowNumber", "Sum", "Count", "Avg", "FirstValue", "LastValue", "Lag", "Lead", "NthValue", "Ntile")
    if hasattr(exp, name)
)


@check("window_order_ties", description="A window's ORDER BY does not settle the order of tied rows.")
def window_order_ties(ctx: CheckContext) -> Iterable[Finding]:
    """``ROW_NUMBER() OVER (PARTITION BY t ORDER BY issued_at)``: rows with the same issued_at get arbitrary numbers."""
    for model in ctx.cache.models():
        for projection in model.select.expressions:
            for window in walk_own(projection):
                if not isinstance(window, exp.Window) or not isinstance(window.this, _ORDER_SENSITIVE):
                    continue
                order = window.args.get("order")
                if order is None or not order.expressions:
                    continue
                fixed: dict[str, set[str]] = {}
                complex_key = False
                for part in list(window.args.get("partition_by") or []) + [o.this for o in order.expressions]:
                    inner = strip(part)
                    if isinstance(inner, exp.Column) and inner.table in model.sources:
                        fixed.setdefault(inner.table, set()).add(inner.name)
                    else:
                        complex_key = True
                if complex_key or len(model.sources) != 1:
                    continue
                alias = next(iter(model.sources))
                info = model.key_info(alias)
                if not info.certain or any(key <= fixed.get(alias, set()) for key in info.keys):
                    continue
                yield ctx.finding(
                    "window_order_ties",
                    Severity.LOW,
                    f"{ctx.snippet(window, 120)} orders by columns that can repeat within a partition, so rows "
                    "that tie are numbered or accumulated in an arbitrary order that can change between runs.",
                    window,
                    suggestion="Add a unique column, such as the key, as the last ORDER BY term.",
                )
