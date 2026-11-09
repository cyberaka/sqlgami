"""Checks about grouping, ordering and aggregates of aggregates."""

from __future__ import annotations

from typing import Iterable

from sqlglot import exp

from ..model import SelectModel, is_window_function, strip, unalias, walk_own
from ..report import Finding, Severity
from .base import CheckContext, check

# Engines that run a query with ungrouped columns and pick values from an arbitrary row.
_LENIENT_GROUP_BY = {"sqlite", "mysql"}


def _ungrouped_columns(model: SelectModel) -> list[exp.Column]:
    select = model.select
    group = model.group
    group_exprs: list[exp.Expression] = []
    grouped: set[tuple[str, str]] = set()
    if group is not None:
        for expr in group.find_all(exp.Column):
            if expr.table in model.sources:
                grouped.add((expr.table, expr.name))
        group_exprs = [strip(e) for e in group.expressions]
        for key in ("rollup", "cube", "grouping_sets"):
            for part in group.args.get(key) or []:
                if isinstance(part, exp.Expression):
                    group_exprs.extend(strip(e) for e in part.expressions)

    # Grouping by a table's whole key fixes every other column of that table.
    determined: set[str] = set()
    for alias in model.sources:
        columns = {name for (a, name) in grouped if a == alias}
        if any(key and key <= columns for key in model.key_info(alias).keys):
            determined.add(alias)

    offenders: list[exp.Column] = []

    def visit(node: exp.Expression) -> None:
        inner = strip(node)
        if any(inner == g for g in group_exprs):
            return
        if isinstance(node, (exp.Select, exp.SetOperation, exp.Subquery)):
            return
        if isinstance(node, exp.AggFunc) and not is_window_function(node):
            return
        if isinstance(node, exp.Column):
            if node.table in model.sources and (node.table, node.name) not in grouped:
                if node.table not in determined:
                    offenders.append(node)
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


@check("missing_group_by", description="A selected column is neither grouped nor aggregated.")
def missing_group_by(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        group = model.group
        if group is None and not model.own_aggregates():
            continue
        if group is not None and group.args.get("all"):
            continue
        offenders = _ungrouped_columns(model)
        if not offenders:
            continue
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


def _has_own_aggregate(node: exp.Expression, kinds: tuple[type, ...]) -> bool:
    return any(isinstance(n, kinds) and not is_window_function(n) for n in walk_own(node))


@check("avg_of_avg", description="An average is taken over values that are already averages or ratios.")
def avg_of_avg(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        for agg in model.own_aggregates():
            if not isinstance(agg, exp.Avg):
                continue
            for column in model.local_columns(agg):
                inner_model, defining = model.origin(column)
                if defining is None or isinstance(defining, exp.Column) or inner_model is model:
                    continue
                if _has_own_aggregate(defining, (exp.Avg,)):
                    kind = "averages"
                elif isinstance(strip(defining), exp.Div) and _has_own_aggregate(defining, (exp.AggFunc,)):
                    kind = "ratios"
                else:
                    continue
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
                break


def _row_limit(select: exp.Select) -> exp.Expression | None:
    return select.args.get("limit") or select.args.get("fetch")


@check("limit_without_order", description="Rows are cut off with LIMIT but no ORDER BY says which rows.")
def limit_without_order(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        select = model.select
        limit = _row_limit(select)
        if limit is None or select.args.get("order") is not None:
            continue
        if model.group is None and model.own_aggregates():
            continue  # one row anyway
        if not model.sources:
            continue
        parent = select.parent
        if isinstance(parent, exp.Exists) or (
            isinstance(parent, exp.Subquery) and isinstance(parent.parent, exp.Exists)
        ):
            continue
        nested = model.scope.parent is not None
        yield ctx.finding(
            "limit_without_order",
            Severity.MEDIUM if nested else Severity.LOW,
            (
                "LIMIT is applied with no ORDER BY, so which rows are kept is up to the engine "
                "and can change from run to run."
                + (" Here the limited rows feed the rest of the query." if nested else "")
            ),
            limit,
            suggestion="Add an ORDER BY that says which rows are wanted.",
            nested=nested,
        )
