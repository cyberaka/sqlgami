"""Checks about grouping, ordering and aggregates of aggregates."""

from __future__ import annotations

from typing import Iterable

from sqlglot import exp

from ..model import SelectModel, is_window_function, strip, unalias, walk_own
from ..report import Finding, Severity
from .base import CheckContext, check

# Engines that run a query with ungrouped columns and pick values from an arbitrary row.
_LENIENT_GROUP_BY = {"sqlite", "mysql"}

# Which grouped key lets a table's other columns be selected without grouping them.
#   any      a primary key or any unique key: the value is then the same for the whole group
#   primary  the primary key only, which is all Postgres infers from
# Every other engine, DuckDB included, wants each selected column grouped or aggregated.
_KEY_DEPENDENCY = {"sqlite": "any", "mysql": "any", "postgres": "primary"}


def _unparen(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _is_scalar_extreme(node: exp.Expression, dialect: str) -> bool:
    """SQLite's MAX(a, b) and MIN(a, b): with several arguments they return the greater or lesser one."""
    return dialect == "sqlite" and isinstance(node, (exp.Max, exp.Min)) and bool(node.expressions)


def _aggregates(model: SelectModel, dialect: str) -> list[exp.AggFunc]:
    """The SELECT's own aggregate calls, leaving out SQLite's scalar MAX and MIN."""
    return [a for a in model.own_aggregates() if not _is_scalar_extreme(a, dialect)]


def _determining_keys(model: SelectModel, alias: str, dialect: str) -> list[frozenset[str]]:
    rule = _KEY_DEPENDENCY.get(dialect)
    if rule is None:
        return []
    keys = [key for key in model.key_info(alias).keys if key]
    if rule == "any":
        return keys
    table = model.sources[alias].table
    if model.sources[alias].kind != "table" or table is None or not table.primary_key:
        return []
    primary = {name.lower() for name in table.primary_key}
    return [key for key in keys if {name.lower() for name in key} == primary]


def _outer_references(model: SelectModel, query: exp.Expression) -> list[exp.Column]:
    """Columns of ``model``'s own sources that a nested query reads (a correlated subquery)."""
    inner = query.this if isinstance(query, exp.Subquery) else query
    for scope in model.scope.subquery_scopes:
        if scope.expression is inner:
            return [c for c in scope.external_columns if c.table in model.sources]
    return []


def _ungrouped_columns(model: SelectModel, dialect: str) -> list[exp.Column]:
    select = model.select
    group = model.group
    group_exprs: list[exp.Expression] = []
    grouped: set[tuple[str, str]] = set()
    if group is not None:
        plain: list[exp.Expression] = []
        for expr in group.expressions:
            expr = _unparen(expr)
            if isinstance(expr, exp.Tuple):
                plain.extend(_unparen(e) for e in expr.expressions)
            else:
                plain.append(expr)
        group_exprs = list(plain)
        # Only a column grouped as itself is grouped. GROUP BY UPPER(status) groups the
        # upper-cased text, and status on its own still varies inside a group.
        for expr in plain:
            if isinstance(expr, exp.Column) and expr.table in model.sources:
                grouped.add((expr.table, expr.name))
        for key in ("rollup", "cube", "grouping_sets"):
            for part in group.args.get(key) or []:
                if not isinstance(part, exp.Expression):
                    continue
                group_exprs.extend(_unparen(e) for e in part.expressions)
                for column in part.find_all(exp.Column):
                    if column.table in model.sources:
                        grouped.add((column.table, column.name))

    # Grouping by a table's key fixes every other column of that table, on engines that infer it.
    determined: set[str] = set()
    for alias in model.sources:
        columns = {name for (a, name) in grouped if a == alias}
        if any(key <= columns for key in _determining_keys(model, alias, dialect)):
            determined.add(alias)

    offenders: list[exp.Column] = []

    def judge(column: exp.Column) -> None:
        if column.table in model.sources and (column.table, column.name) not in grouped:
            if column.table not in determined:
                offenders.append(column)

    def visit(node: exp.Expression) -> None:
        inner = _unparen(node)
        if any(inner == g for g in group_exprs):
            return
        if isinstance(node, (exp.Select, exp.SetOperation, exp.Subquery)):
            # A correlated subquery is evaluated once per group, so the outer columns
            # it reads must be grouped like any other selected column.
            for column in _outer_references(model, node):
                judge(column)
            return
        if isinstance(node, exp.AggFunc) and not is_window_function(node) and not _is_scalar_extreme(node, dialect):
            return
        if isinstance(node, (exp.Filter, exp.WithinGroup)) and isinstance(node.this, exp.AggFunc):
            if not is_window_function(node.this):
                return  # COUNT(*) FILTER (WHERE status = 'paid'): the condition is part of the aggregate
        if isinstance(node, exp.Column):
            judge(node)
            return
        for child in node.iter_expressions():
            visit(child)

    for projection in select.expressions:
        visit(unalias(projection))
    for key in ("having", "order", "qualify"):
        part = select.args.get(key)
        if part is not None:
            visit(part)
    return offenders


def _single_min_or_max(model: SelectModel) -> bool:
    """SQLite: with exactly one MIN() or MAX(), bare columns come from the row holding that value."""
    extremes = [a for a in _aggregates(model, "sqlite") if isinstance(a, (exp.Min, exp.Max))]
    return len(extremes) == 1


@check("missing_group_by", description="A selected column is neither grouped nor aggregated.")
def missing_group_by(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        group = model.group
        if group is None and not _aggregates(model, ctx.dialect):
            continue
        if group is not None and group.args.get("all"):
            continue
        offenders = _ungrouped_columns(model, ctx.dialect)
        if not offenders:
            continue
        if ctx.dialect == "sqlite" and _single_min_or_max(model):
            continue  # documented SQLite behaviour, and the usual way to fetch the latest row there
        names = []
        for column in offenders:
            text = ctx.snippet(column)
            if text and text not in names:
                names.append(text)
        listed = ", ".join(names[:5])
        if ctx.dialect in _LENIENT_GROUP_BY:
            message = (
                f"{listed} is selected alongside an aggregate but is not in GROUP BY. "
                f"{ctx.dialect} runs this and fills the column from an arbitrary row of "
                "each group, so the value shown does not belong with the aggregate."
            )
        else:
            message = (
                f"{listed} is selected alongside an aggregate but is not in GROUP BY. "
                "This engine rejects the query."
            )
        yield ctx.finding(
            "missing_group_by",
            Severity.HIGH,
            message,
            offenders[0],
            suggestion="Add the column to GROUP BY, or wrap it in an aggregate.",
            columns=names,
        )


# ------------------------------------------------------------- averages of averages


def _has_own_aggregate(node: exp.Expression, kinds: tuple[type, ...]) -> bool:
    return any(isinstance(n, kinds) and not is_window_function(n) for n in walk_own(node))


def _from_aggregate(model: SelectModel, expr: exp.Expression, depth: int = 0) -> bool:
    """True when ``expr`` is computed from an aggregate, here or in a derived table it reads."""
    if depth > 32:
        return False
    if _has_own_aggregate(expr, (exp.AggFunc,)):
        return True
    for column in model.local_columns(expr):
        inner_model, defining = model.origin(column)
        if defining is None or isinstance(defining, exp.Column) or inner_model is model:
            continue
        if _from_aggregate(inner_model, defining, depth + 1):
            return True
    return False


def _summary_kind(model: SelectModel, expr: exp.Expression, depth: int = 0) -> tuple[str, exp.Expression] | None:
    """``("averages" | "ratios", expression)`` when ``expr`` is already a per-group average or ratio.

    A ratio means one aggregate divided by another, such as SUM(x) / COUNT(*). An aggregate
    divided by a constant (SUM(cents) / 100.0) is a change of unit, not a ratio.
    """
    if depth > 32:
        return None
    if _has_own_aggregate(expr, (exp.Avg,)):
        return "averages", expr
    for node in walk_own(expr):
        if (
            isinstance(node, exp.Div)
            and _from_aggregate(model, node.this, depth)
            and _from_aggregate(model, node.expression, depth)
        ):
            return "ratios", expr
    for column in model.local_columns(expr):
        inner_model, defining = model.origin(column)
        if defining is None or isinstance(defining, exp.Column) or inner_model is model:
            continue
        found = _summary_kind(inner_model, defining, depth + 1)
        if found is not None:
            return found
    return None


@check("avg_of_avg", description="An average is taken over values that are already averages or ratios.")
def avg_of_avg(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        for agg in model.own_aggregates():
            if not isinstance(agg, exp.Avg) or agg.this is None:
                continue
            found = _summary_kind(model, agg.this)
            if found is None:
                continue
            kind, defining = found
            yield ctx.finding(
                "avg_of_avg",
                Severity.MEDIUM,
                (
                    f"{ctx.snippet(agg)} averages values that are themselves per-group "
                    f"{kind} ({ctx.snippet(defining)}). Every group then counts equally, "
                    "however many rows it holds, which differs from the average over all rows."
                ),
                agg,
                suggestion="Compute the overall figure from sums and counts of the underlying rows.",
                inner=ctx.snippet(defining),
            )

        # The same thing written by hand: SUM(per_group_average) / COUNT(*).
        for node in walk_own(model.select):
            if not isinstance(node, exp.Div):
                continue
            if not _has_own_aggregate(node.expression, (exp.Count,)):
                continue
            for total in walk_own(node.this):
                if not isinstance(total, exp.Sum) or is_window_function(total) or total.this is None:
                    continue
                found = _summary_kind(model, total.this)
                if found is None:
                    continue
                kind, defining = found
                yield ctx.finding(
                    "avg_of_avg",
                    Severity.MEDIUM,
                    (
                        f"{ctx.snippet(node)} adds up per-group {kind} ({ctx.snippet(defining)}) and "
                        "divides by the number of groups, which is an average of them. Every group "
                        "then counts equally, however many rows it holds."
                    ),
                    node,
                    suggestion="Compute the overall figure from sums and counts of the underlying rows.",
                    inner=ctx.snippet(defining),
                )
                break


# ------------------------------------------------------------- rows cut off with no order


def _row_limit(query: exp.Expression) -> exp.Expression | None:
    return query.args.get("limit") or query.args.get("fetch")


def _is_zero(limit: exp.Expression | None) -> bool:
    if limit is None:
        return False
    count = limit.args.get("expression") or limit.args.get("count")
    return isinstance(count, exp.Literal) and not count.is_string and count.name in ("0", "0.0")


def _orders_nothing(order: exp.Expression | None) -> bool:
    """True when there is no ORDER BY, or it sorts by something that is the same for every row."""
    if order is None:
        return True
    for ordered in order.expressions:
        key = _unparen(ordered.this if isinstance(ordered, exp.Ordered) else ordered)
        if isinstance(key, exp.Literal) and not key.is_string:
            return False  # a position in the select list
        for node in key.walk():
            # A column, a function call (random() included) or a nested query can vary by row.
            if isinstance(node, (exp.Column, exp.Func, exp.Window, exp.Select, exp.Star, exp.Placeholder)):
                return False
    return True


def _in_exists(query: exp.Expression) -> bool:
    parent = query.parent
    return isinstance(parent, exp.Exists) or (
        isinstance(parent, exp.Subquery) and isinstance(parent.parent, exp.Exists)
    )


def _constant_output(select: exp.Select) -> bool:
    """Every selected value is a constant, so it does not matter which rows are kept."""
    for projection in select.expressions:
        for node in unalias(projection).walk():
            if isinstance(node, (exp.Column, exp.Star, exp.AggFunc, exp.Window, exp.Select)):
                return False
    return bool(select.expressions)


def _at_most_one_row(model: SelectModel) -> bool:
    """Filters pin every source to a single row (a whole key compared with constants)."""
    pinned: set[str] = set()
    changed = True
    while changed:
        changed = False
        for alias in model.sources:
            if alias in pinned:
                continue
            if model.is_single_row(alias) or model.joins_at_most_one(alias, set(pinned)):
                pinned.add(alias)
                changed = True
    return bool(model.sources) and pinned == set(model.sources)


def _cut_finding(ctx: CheckContext, query: exp.Expression, nested: bool) -> Finding | None:
    """The finding for a query that cuts rows off (LIMIT, FETCH, OFFSET) without a real ORDER BY."""
    limit = _row_limit(query)
    offset = query.args.get("offset")
    if limit is None and offset is None:
        return None
    if _is_zero(limit) or _in_exists(query):
        return None
    order = query.args.get("order")
    if not _orders_nothing(order):
        return None
    word = "LIMIT" if limit is not None else "OFFSET"
    if order is None:
        reason = f"{word} is applied with no ORDER BY"
    else:
        reason = f"{word} is applied, and {ctx.snippet(order)} is the same for every row"
    return ctx.finding(
        "limit_without_order",
        Severity.MEDIUM if nested else Severity.LOW,
        (
            f"{reason}, so which rows are "
            + ("kept" if limit is not None else "skipped")
            + " is up to the engine and can change from run to run."
            + (" Here the limited rows feed the rest of the query." if nested else "")
        ),
        limit if limit is not None else offset,
        suggestion="Add an ORDER BY that says which rows are wanted.",
        nested=nested,
    )


def _unordered_row_numbers(select: exp.Select) -> dict[str, exp.Window]:
    """Output name -> window, for each selected ROW_NUMBER() that has no ORDER BY."""
    found: dict[str, exp.Window] = {}
    for projection in select.expressions:
        window = _unparen(unalias(projection))
        if (
            isinstance(window, exp.Window)
            and isinstance(window.this, exp.RowNumber)
            and window.args.get("order") is None
            and projection.alias_or_name
        ):
            found[projection.alias_or_name] = window
    return found


_ROW_FILTERS = (exp.EQ, exp.LT, exp.LTE, exp.In, exp.Between)


def _one_of(node: exp.Expression, keys: list[exp.Expression]) -> bool:
    """``node`` is one of ``keys``; a column matches by name when only one of the two is qualified."""
    for key in keys:
        if node == key:
            return True
        if isinstance(node, exp.Column) and isinstance(key, exp.Column) and node.name == key.name:
            if not node.table or not key.table or node.table == key.table:
                return True
    return False


def _row_number_finding(ctx: CheckContext, window: exp.Window, filter_text: str | None) -> Finding:
    return ctx.finding(
        "limit_without_order",
        Severity.MEDIUM,
        (
            f"{ctx.snippet(window)} numbers the rows with no ORDER BY, so which row gets each "
            f"number is up to the engine, and {filter_text} then keeps an arbitrary row."
        ),
        window,
        suggestion="Add ORDER BY inside OVER (...) to say which row is wanted.",
        nested=True,
    )


@check("limit_without_order", description="Rows are cut off with LIMIT but no ORDER BY says which rows.")
def limit_without_order(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        select = model.select
        nested = model.scope.parent is not None
        order = select.args.get("order")

        one_row = (model.group is None and bool(_aggregates(model, ctx.dialect))) or _at_most_one_row(model)
        if model.sources and not one_row and not _constant_output(select):
            finding = _cut_finding(ctx, select, nested)
            if finding is not None:
                yield finding

        # DISTINCT ON keeps one row per group: with no ORDER BY, an arbitrary one.
        distinct = select.args.get("distinct")
        on = distinct.args.get("on") if distinct is not None else None
        if on is not None and _orders_nothing(order):
            keys = [_unparen(e) for e in (on.expressions if isinstance(on, exp.Tuple) else [on])]
            if any(not _one_of(_unparen(unalias(p)), keys) for p in select.expressions):
                yield ctx.finding(
                    "limit_without_order",
                    Severity.MEDIUM if nested else Severity.LOW,
                    (
                        f"DISTINCT ON keeps one row for each {ctx.snippet(on)}, and there is no "
                        "ORDER BY to say which, so the other columns come from an arbitrary row."
                    ),
                    distinct,
                    suggestion=(
                        "Add an ORDER BY that starts with the DISTINCT ON columns and then says "
                        "which row is wanted."
                    ),
                    nested=nested,
                )

        # ROW_NUMBER() with no ORDER BY, then a filter on the number: LIMIT per group with no order.
        qualify = select.args.get("qualify")
        if qualify is not None:
            for window in qualify.find_all(exp.Window):
                if isinstance(window.this, exp.RowNumber) and window.args.get("order") is None:
                    yield _row_number_finding(ctx, window, ctx.snippet(qualify.this))
                    break
        for alias, source in model.sources.items():
            if source.kind != "derived" or source.scope is None:
                continue
            inner = source.scope.expression
            if not isinstance(inner, exp.Select):
                continue
            numbers = _unordered_row_numbers(inner)
            if not numbers:
                continue
            for conjunct in model.conjuncts:
                expr = _unparen(conjunct.expr)
                if not isinstance(expr, _ROW_FILTERS):
                    continue
                sides = [expr.this] + ([expr.expression] if isinstance(expr, (exp.EQ, exp.LT, exp.LTE)) else [])
                hit = next(
                    (
                        strip(s)
                        for s in sides
                        if isinstance(strip(s), exp.Column) and strip(s).table == alias and strip(s).name in numbers
                    ),
                    None,
                )
                if hit is not None:
                    yield _row_number_finding(ctx, numbers[hit.name], ctx.snippet(expr))
                    break

    # A LIMIT written after a UNION belongs to the whole set operation.
    for scope in ctx.analysis.scopes():
        query = scope.expression
        if isinstance(query, exp.SetOperation):
            finding = _cut_finding(ctx, query, scope.parent is not None)
            if finding is not None:
                yield finding
