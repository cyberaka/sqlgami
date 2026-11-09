"""Checks on single expressions: self-comparisons, empty strings, JSON paths, scalar subqueries, date arithmetic."""

from __future__ import annotations

from typing import Iterable

from sqlglot import exp

from .. import types as T
from ..model import SelectModel, strip, walk_own
from ..report import Finding, Severity
from .base import PROBE_QUERY, PROBE_VALUE, CheckContext, Probe, check
from .predicates import column_info, family, own_nodes, unparen

_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


@check("self_comparison", description="A value is compared with itself.")
def self_comparison(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        for node in own_nodes(model, _COMPARISONS):
            left, right = strip(node.this), strip(node.expression)
            if left != right or not any(isinstance(n, exp.Column) for n in walk_own(left)):
                continue
            if any(isinstance(n, (exp.Rand, exp.Anonymous)) for n in walk_own(left)):
                continue
            never = isinstance(node, (exp.NEQ, exp.GT, exp.LT))
            yield ctx.finding(
                "self_comparison",
                Severity.HIGH if never else Severity.MEDIUM,
                f"{ctx.snippet(node)} compares a value with itself, which is "
                + ("never true, so the condition matches no rows." if never else "true for every row that is not NULL.")
                + " A different column or a NULL test was probably meant.",
                node,
                suggestion="Use IS NULL / IS NOT NULL, or compare with the intended column.",
            )


@check("empty_string_comparison", description="A column is compared with an empty string where missing values are NULL.")
def empty_string_comparison(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        for node in own_nodes(model, (exp.EQ, exp.NEQ)):
            for column, literal in ((node.this, node.expression), (node.expression, node.this)):
                column, literal = unparen(column), unparen(literal)
                if not isinstance(column, exp.Column) or not isinstance(literal, exp.Literal):
                    continue
                if not literal.is_string or literal.name != "":
                    continue
                info = column_info(ctx, model, column)
                fam = family(ctx, model, column)
                if fam in (T.NUMERIC, T.DATE, T.TIMESTAMP, T.BOOLEAN):
                    yield ctx.finding(
                        "empty_string_comparison",
                        Severity.HIGH,
                        f"{ctx.snippet(node)} compares a {fam} column with an empty string. A {fam} "
                        "value is never an empty string; a missing value is NULL.",
                        node,
                        suggestion="Use IS NULL or IS NOT NULL.",
                    )
                    break
                if info is None or info[1].nullable is False:
                    break
                finding = ctx.finding(
                    "empty_string_comparison",
                    Severity.MEDIUM,
                    f"{ctx.snippet(node)} tests for an empty string, but {info[0].name}.{info[1].name} "
                    "can be NULL, and a NULL is neither equal nor unequal to ''. Rows where the "
                    "value is missing are skipped either way.",
                    node,
                    suggestion="Test IS NULL / IS NOT NULL, alone or alongside the empty-string test.",
                    column=f"{info[0].name}.{info[1].name}",
                )
                ctx.probes.append(Probe(PROBE_VALUE, info[0], (info[1].name,), finding, literal=""))
                yield finding
                break


_JSON_NODES = tuple(
    getattr(exp, name)
    for name in ("JSONExtract", "JSONExtractScalar", "JSONBExtract", "JSONBExtractScalar")
    if hasattr(exp, name)
)


@check("json_path", description="A JSON path that no row has.")
def json_path(ctx: CheckContext) -> Iterable[Finding]:
    """Nothing can be said statically about a JSON path; the data can say whether it exists."""
    seen: set[str] = set()
    for model in ctx.cache.models():
        for node in own_nodes(model, _JSON_NODES):
            if isinstance(node.parent, _JSON_NODES):
                continue  # report the full path once, at its outermost step
            columns = [c for c in walk_own(node) if isinstance(c, exp.Column)]
            if len(columns) != 1 or columns[0].table not in model.sources:
                continue
            source = model.sources[columns[0].table]
            if source.table is None:
                continue
            text = ctx.snippet(node) or ""
            key = f"{source.table.qualified_name}:{text}"
            if key in seen:
                continue
            seen.add(key)
            probe_expr = node.copy()
            for column in probe_expr.find_all(exp.Column):
                column.set("table", None)
            query = (
                exp.select(exp.Literal.number(1))
                .from_(_bare_table(source.node))
                .where(exp.Not(this=exp.Is(this=probe_expr, expression=exp.Null())))
                .limit(1)
            )
            finding = ctx.finding(
                "json_path",
                Severity.INFO,
                f"{text} reads a path inside a JSON value.",
                node,
                table=source.table.qualified_name,
            )
            ctx.probes.append(
                Probe(
                    PROBE_QUERY,
                    source.table,
                    (),
                    finding,
                    query=query,
                    on_rows=(None, " Checked against the data: some rows have this path.", "path_present"),
                    on_empty=(
                        Severity.HIGH,
                        " Checked against the data: no row has anything at this path, so the "
                        "expression is NULL everywhere. The key name is probably wrong.",
                        "path_absent",
                    ),
                )
            )
            yield finding


def _bare_table(node: exp.Expression) -> exp.Expression:
    table = node.copy()
    table.set("alias", None)
    return table


_SCALAR_PARENTS_EXCLUDED = (exp.From, exp.Join, exp.Exists, exp.Lateral, exp.Unnest, exp.CTE, exp.SetOperation)


@check("scalar_subquery_rows", description="A subquery used as a single value may return several rows.")
def scalar_subquery_rows(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        for sub_scope in model.scope.subquery_scopes:
            inner = sub_scope.expression
            node = inner.parent
            if not isinstance(node, exp.Subquery):
                continue
            parent = node.parent
            if isinstance(parent, _SCALAR_PARENTS_EXCLUDED):
                continue
            if isinstance(parent, exp.In) and parent.args.get("query") is node:
                continue
            if isinstance(parent, (exp.Any, exp.All)):
                continue
            keys = ctx.cache.output_keys(sub_scope)
            if frozenset() in keys.keys:
                continue
            lenient = ctx.dialect in ("sqlite", "mysql")
            yield ctx.finding(
                "scalar_subquery_rows",
                Severity.MEDIUM,
                f"{ctx.snippet(node, 120)} is used as a single value, but nothing in the schema says it "
                "returns one row: it has no aggregate, no LIMIT 1, and its filter is not on a unique key. "
                + (
                    f"{ctx.dialect} silently takes the first row."
                    if lenient
                    else "If it ever returns two rows the whole query fails."
                ),
                node,
                suggestion="Aggregate it (MAX/MIN), add LIMIT 1 with an ORDER BY, or filter on a unique key.",
                lenient=lenient,
            )


def _is_whole_number(node: exp.Expression) -> bool:
    """A number literal, or arithmetic over number literals (14 * 25)."""
    node = unparen(node)
    if isinstance(node, exp.Literal):
        return not node.is_string and node.name.replace("-", "").isdigit()
    if isinstance(node, (exp.Mul, exp.Add, exp.Sub)):
        return _is_whole_number(node.this) and _is_whole_number(node.expression)
    if isinstance(node, exp.Neg):
        return _is_whole_number(node.this)
    return False


def temporal_family(ctx: CheckContext, model: SelectModel, node: exp.Expression) -> str | None:
    """``date`` or ``timestamp`` if the expression is a point in time, looking inside scalar subqueries."""
    node = unparen(node)
    if isinstance(node, exp.Subquery) and isinstance(node.this, exp.Select) and node.this.expressions:
        sub_scope = next((s for s in model.scope.subquery_scopes if s.expression is node.this), None)
        if sub_scope is None:
            return None
        return temporal_family(ctx, ctx.cache.model(sub_scope), node.this.expressions[0].unalias())
    if isinstance(node, (exp.Max, exp.Min)):
        return temporal_family(ctx, model, node.this)
    if isinstance(node, (exp.CurrentDate,)):
        return T.DATE
    if isinstance(node, (exp.CurrentTimestamp,)):
        return T.TIMESTAMP
    fam = family(ctx, model, node)
    return fam if fam in T.TEMPORAL else None


@check("date_arithmetic", description="A date has a bare number added to or subtracted from it.")
def date_arithmetic(ctx: CheckContext) -> Iterable[Finding]:
    dialect = ctx.dialect
    for model in ctx.cache.models():
        for node in own_nodes(model, (exp.Add, exp.Sub)):
            for moment, number in ((node.this, node.expression), (node.expression, node.this)):
                if not _is_whole_number(number):
                    continue
                kind = temporal_family(ctx, model, moment)
                if kind is None:
                    continue
                if dialect == "sqlite":
                    severity, why = Severity.HIGH, (
                        "SQLite stores dates as text or numbers and has no date arithmetic operator, so "
                        "this subtracts from whatever number the text starts with."
                    )
                elif kind == T.TIMESTAMP:
                    severity, why = Severity.MEDIUM, (
                        "Adding a bare number to a timestamp is not defined in this engine; it needs an INTERVAL."
                    )
                else:
                    break  # DATE plus or minus an integer is a number of days here
                yield ctx.finding(
                    "date_arithmetic",
                    severity,
                    f"{ctx.snippet(node)} adds or subtracts a bare number on a {kind}. {why}",
                    node,
                    suggestion="Use the engine's date function or an INTERVAL.",
                )
                break
