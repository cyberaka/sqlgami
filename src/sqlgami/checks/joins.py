"""Checks about joins: repeated rows, missing join conditions, collapsed outer joins."""

from __future__ import annotations

import re
from typing import Any, Iterable

from sqlglot import exp

from ..model import MAYBE, NO, YES, SelectModel, flatten_and, strip, walk_own  # noqa: F401
from ..report import Finding, Severity
from .base import PROBE_QUERY, PROBE_UNIQUE, CheckContext, Probe, check

_CROSS_JOIN_RE = re.compile(r"\bcross\s+join\b", re.IGNORECASE)

def _code_only(sql: str, dialect: str) -> str:
    """The statement with comments and string literals removed."""
    from ..template import CODE, segments

    return " ".join(text for kind, text in segments(sql, dialect) if kind == CODE)


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


def _derived_duplicates_query(ctx: CheckContext, source, columns: list[str]) -> exp.Expression | None:
    """A query that returns a row if the derived table ``source`` has two rows with the same ``columns``."""
    node = source.node
    if not isinstance(node, exp.Select) or source.scope is None or getattr(source.scope, "external_columns", None):
        return None
    inner = exp.Subquery(this=node.copy(), alias=exp.TableAlias(this=exp.to_identifier("sqlgami_d")))
    keys = [exp.column(exp.to_identifier(c, quoted=True), table=exp.to_identifier("sqlgami_d")) for c in columns]
    query = exp.select(exp.Literal.number(1)).from_(inner).group_by(*keys).having("COUNT(*) > 1").limit(1)
    for key in keys:
        query = query.where(exp.Not(this=exp.Is(this=key.copy(), expression=exp.Null())))
    root = ctx.analysis.resolved
    with_ = (root.args.get("with_") or root.args.get("with")) if root is not None else None
    if with_ is not None:
        query.set("with_", with_.copy())
    return query


def _join_grain_query(ctx: CheckContext, model: SelectModel, alias: str) -> tuple[exp.Expression, Any] | None:
    """A query that returns a row if, in this SELECT's own FROM and WHERE, two joined rows share
    one row of ``alias`` (by its key). No row means the join never repeats ``alias``: every filter
    counts, including those on the other tables, which static reasoning cannot weigh."""
    select = model.select
    if select.args.get("from_") is None and select.args.get("from") is None:
        return None
    if getattr(model.scope, "external_columns", None):
        return None  # correlated with an outer query: cannot run on its own
    info = model.key_info(alias)
    key = min((k for k in info.keys if k), key=len, default=None) if info.certain else None
    if key is None:
        return None
    source = model.sources[alias]
    table = source.table if source.kind == "table" else _a_base_table(model, source)
    if table is None:
        return None
    keys = [exp.column(exp.to_identifier(c, quoted=True), table=exp.to_identifier(alias, quoted=True)) for c in sorted(key)]
    query = exp.Select(expressions=[exp.Literal.number(1)])
    for arg in ("from_", "from", "joins", "where"):
        if select.args.get(arg) is not None:
            query.set(arg, select.args[arg].copy())
    query = query.group_by(*keys).having("COUNT(*) > 1").limit(1)
    root = ctx.analysis.resolved
    with_ = (root.args.get("with_") or root.args.get("with")) if root is not None else None
    if with_ is not None:
        query.set("with_", with_.copy())
    return query, table


def _a_base_table(model: SelectModel, source):
    """Some base table a derived source reads, for the probe's size check."""
    if source.scope is None:
        return None
    try:
        inner = model.cache.model(source.scope)
    except Exception:
        return None
    for alias, inner_source in inner.sources.items():
        if inner_source.kind == "table" and inner_source.table is not None:
            return inner_source.table
        if inner_source.kind == "derived":
            found = _a_base_table(inner, inner_source)
            if found is not None:
                return found
    return None


def _uniqueness_probes(
    ctx: CheckContext, model: SelectModel, finding: Finding, culprits: list[str], aliases: list[str]
) -> None:
    """Ask the data whether the join columns of each repeating source are in fact unique.

    The aggregate is safe once any one of the tables it reads turns out not to be repeated,
    so the probes are grouped by the table read: a group holds when every source that
    repeats that table is unique on its join columns. A base table is asked directly; a
    derived table, CTE or expanded view is asked by running it as a subquery and grouping
    by its join columns.
    """
    planned: list[Probe] = []
    for alias in aliases:
        repeaters = model.multiplied(alias, per_group=True).culprits or culprits
        group: list[Probe] = []
        for culprit in repeaters:
            source = model.sources.get(culprit)
            if source is None:
                return
            others = (set(model.sources) - {culprit}) | {alias}
            columns = sorted(model.bound_columns(culprit, others, per_group=True))
            if not columns:
                return
            if source.kind == "table" and source.table is not None:
                group.append(Probe(PROBE_UNIQUE, source.table, tuple(columns), finding, group=alias))
            elif source.kind == "derived":
                query = _derived_duplicates_query(ctx, source, columns)
                table = _a_base_table(model, source)
                if query is None or table is None:
                    return
                group.append(Probe(PROBE_UNIQUE, table, tuple(columns), finding, query=query, snippet=culprit, group=alias))
            else:
                return  # cannot probe this kind of source; leave the finding as it is
        planned.extend(group)
        # Independently of the culprits: does the whole join, with all its filters, ever repeat a
        # row of this table? A separate group, so either answer can clear the finding.
        grain = _join_grain_query(ctx, model, alias)
        if grain is not None:
            query, table = grain
            key = sorted(min((k for k in model.key_info(alias).keys if k), key=len))
            planned.append(Probe(PROBE_UNIQUE, table, tuple(key), finding, query=query, snippet=f"join:{alias}", group=f"{alias}:join"))
    ctx.probes.extend(planned)


def _repeated_inside(model: SelectModel, column: exp.Column, depth: int = 0):
    """Follow a column into the subquery or CTE it comes from, looking for a join that repeats its rows.

    Returns ``(inner model, alias, culprits)`` for the first level at which the
    column's own table is repeated by a join and nothing (GROUP BY, DISTINCT)
    undoes it before the rows come out.
    """
    source = model.sources.get(column.table)
    if source is None or source.kind != "derived" or source.scope is None or depth > 32:
        return None
    if not isinstance(source.scope.expression, exp.Select):
        return None
    inner = model.cache.model(source.scope)
    if inner.is_aggregated or inner.select.args.get("distinct") is not None:
        return None
    defining = inner.projection(column.name)
    if defining is None:
        return None
    defining = strip(defining)
    if not isinstance(defining, exp.Column) or defining.table not in inner.sources:
        return None
    if len(inner.sources) >= 2:
        repeated = inner.multiplied(defining.table)
        # If the subquery returns a key of the rows it is really about (one row per
        # subscription, each carrying its plan's price), repeating the looked-up value is
        # the point. Only a subquery whose rows have no identity of their own is suspect.
        if repeated.state == YES and not inner.output_keys().keys:
            looked_up = inner.sources[defining.table]
            leaf = looked_up.kind == "table" and looked_up.table is not None and not looked_up.table.foreign_keys
            per_row_value = leaf or bool(_RATE_LIKE.search(defining.name))
            if not (per_row_value and _projects_a_key_of(inner, repeated.culprits)):
                return inner, defining.table, repeated.culprits
    return _repeated_inside(inner, defining, depth + 1)


# A looked-up value that is meant to apply once per row of the table that refers to it (a plan's
# price per subscription, a tax rate per invoice), as opposed to a parent's own total.
_RATE_LIKE = re.compile(r"(price|rate|cost|fee|weight|pct|percent|ratio|multiplier|factor|unit_|per_|coefficient|tax)", re.I)


def _projects_a_key_of(inner: SelectModel, aliases: list[str]) -> bool:
    """The subquery returns a whole key of one of ``aliases``: its rows are about that source's rows
    (one row per subscription, each carrying its plan's price), so repeating a looked-up value is the
    point. Only trusted when the value is a per-row rate (see _RATE_LIKE) or the looked-up table is a
    leaf with no foreign keys of its own: a parent fact's total repeated per child row is still a fan-out."""
    for alias in aliases:
        for key in inner.key_info(alias).keys:
            if key and all(inner._projection_name_for_ref((alias, column)) for column in key):
                return True
    return False


def _window_aggregates(model: SelectModel) -> list[exp.AggFunc]:
    found = []
    for projection in model.select.expressions:
        for node in walk_own(projection):
            if isinstance(node, exp.Window) and isinstance(node.this, exp.AggFunc):
                if not isinstance(node.this, _REPEAT_SAFE):
                    found.append(node.this)
    return found


@check("fan_out", description="An aggregate reads rows that a join has repeated.")
def fan_out(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        if all(source.kind == "function" for source in model.sources.values()):
            continue  # generate_series and friends: combining them is the point
        plain = [a for a in model.own_aggregates() if not isinstance(a, _REPEAT_SAFE)]
        windowed = _window_aggregates(model)
        if not plain and not windowed:
            continue
        pinned = _group_pins_one_row(model)
        reported: set[tuple[str, tuple[str, ...]]] = set()
        for agg in plain + windowed:
            is_window = any(agg is w for w in windowed)
            if _is_distinct(agg) or (pinned and not is_window):
                continue
            columns = model.local_columns(agg)
            aliases = sorted({c.table for c in columns})
            counts_rows = not aliases
            if counts_rows:
                aliases = sorted(model.sources)
            state, culprits, read = (NO, [], [])
            if len(model.sources) >= 2:
                state, culprits, read = _repeat_state(model, aliases)
            if state == NO:
                # Not repeated here. The rows may have been repeated already, inside a
                # subquery or CTE this SELECT reads from.
                traced = [(column, _repeated_inside(model, column)) for column in columns]
                if any(found is None for _, found in traced):
                    # The aggregate also reads a value at the rows' own grain (price * quantity,
                    # amount * rate): the looked-up value is applied once per row, which is the point.
                    continue
                for column, hidden in traced:
                    if hidden is None:
                        continue
                    inner, inner_alias, inner_culprits = hidden
                    key = ("inside", (column.table, column.name))
                    if key in reported:
                        break
                    reported.add(key)
                    by = ", ".join(inner.sources[c].label for c in inner_culprits) or "another table"
                    finding = ctx.finding(
                        "fan_out",
                        Severity.MEDIUM if isinstance(agg, exp.Count) else Severity.HIGH,
                        f"{ctx.snippet(agg)} reads {column.table}.{column.name}, which comes from "
                        f"{inner.sources[inner_alias].label} inside a subquery. There, the join to {by} "
                        "repeats each of those rows, and nothing removes the repeats before the "
                        "aggregate, so the same value is counted more than once.",
                        agg,
                        suggestion="Aggregate before joining, or do not join the repeating table in the subquery.",
                        reads=[column.table],
                        repeated_by=inner_culprits,
                        certainty=YES,
                        inside_subquery=True,
                    )
                    _uniqueness_probes(ctx, inner, finding, inner_culprits, [inner_alias])
                    yield finding
                    break
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
                    severity = Severity.MEDIUM if isinstance(agg, exp.Count) or is_window else Severity.HIGH
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

        for agg in model.own_aggregates():
            if not isinstance(agg, exp.Count) or not _is_distinct(agg):
                continue
            inner = agg.this.expressions if isinstance(agg.this, exp.Distinct) else [agg.this]
            columns = [strip(e) for e in inner]
            if len(columns) != 1 or not isinstance(columns[0], exp.Column) or columns[0].table not in model.sources:
                continue
            column = columns[0]
            info = model.key_info(column.table)
            if not info.certain or frozenset({column.name}) in info.keys:
                continue
            if model.multiplied(column.table, per_group=True).state != YES:
                continue
            yield ctx.finding(
                "distinct_masking",
                Severity.LOW,
                (
                    f"{ctx.snippet(agg)} counts distinct values of a column that is not a key of "
                    f"{model.sources[column.table].label}. DISTINCT hides the rows the join repeats, "
                    "but it also counts two different rows with the same value as one."
                ),
                agg,
                suggestion="Count the key column if rows are meant, or keep this if distinct values are meant.",
                reads=[column.table],
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
                if not model.is_single_row(a)
                and model.sources[a].kind != "function"
                and not model.joins_at_most_one(a, set())  # pinned to one row by its key
            ]
            if members:
                real.append(sorted(group))
        if len(real) < 2:
            continue
        explicit = bool(_CROSS_JOIN_RE.search(_code_only(ctx.analysis.sql, ctx.dialect))) and all(
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
