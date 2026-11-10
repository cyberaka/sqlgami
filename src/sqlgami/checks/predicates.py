"""Checks about individual conditions: NULL handling, types, dates and filter values."""

from __future__ import annotations

import datetime as _dt
import difflib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from sqlglot import exp
from sqlglot.optimizer.scope import Scope

from .. import types as T
from ..catalog import Column, Table
from ..model import SelectModel, strip, unalias, walk_own
from ..report import Finding, Severity
from .base import (
    PROBE_HAS_NULL,
    PROBE_QUERY,
    PROBE_TIMESTAMP_FORMAT,
    PROBE_VALUE,
    CheckContext,
    Probe,
    check,
)

_ORDERED = (exp.GT, exp.GTE, exp.LT, exp.LTE)
_EQUALITY = (exp.EQ, exp.NEQ)


def own_nodes(model: SelectModel, kinds: tuple[type, ...]) -> Iterator[exp.Expression]:
    for node in model.own():
        if isinstance(node, kinds):
            yield node


def unparen(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def column_info(ctx: CheckContext, model: SelectModel, node: exp.Expression) -> tuple[Table, Column] | None:
    """The catalog column a reference reads, following pass-through projections."""
    if not isinstance(node, exp.Column) or node.table not in model.sources:
        return None
    base = model.base_column(node)
    if base is None:
        return None
    table, name = base
    column = ctx.catalog.column(table, name, quoted=True)
    if column is None:
        return None
    return table, column


def family(ctx: CheckContext, model: SelectModel, node: exp.Expression) -> str:
    node = unparen(node)
    if isinstance(node, exp.Column):
        info = column_info(ctx, model, node)
        if info is not None and info[1].type:
            return T.family_of_declared(info[1].type, ctx.dialect)
        return T.family_of_datatype(node.type) if node.type is not None else T.UNKNOWN
    if isinstance(node, exp.Literal):
        return T.TEXT if node.is_string else T.NUMERIC
    if isinstance(node, exp.Boolean):
        return T.BOOLEAN
    if isinstance(node, exp.Null):
        return T.UNKNOWN
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return T.NUMERIC
    return T.family_of_datatype(node.type) if node.type is not None else T.UNKNOWN


def _comparison_pairs(node: exp.Expression) -> list[tuple[exp.Expression, exp.Expression, str]]:
    """(left, right, operator) pairs a predicate compares."""
    if isinstance(node, exp.Between):
        return [
            (node.this, node.args["low"], ">="),
            (node.this, node.args["high"], "<="),
        ]
    if isinstance(node, exp.In):
        if node.args.get("query") is not None:
            return []
        return [(node.this, value, "in") for value in node.expressions]
    operator = {
        exp.EQ: "=",
        exp.NEQ: "<>",
        exp.GT: ">",
        exp.GTE: ">=",
        exp.LT: "<",
        exp.LTE: "<=",
    }.get(type(node))
    if operator is None:
        return []
    return [(node.this, node.expression, operator)]


_FLIP = {">": "<", ">=": "<=", "<": ">", "<=": ">=", "=": "=", "<>": "<>", "in": "in"}


def _oriented(
    left: exp.Expression, right: exp.Expression, operator: str
) -> tuple[exp.Column, exp.Expression, str] | None:
    """Put the column on the left: ``(column, other, operator)``."""
    a, b = unparen(left), unparen(right)
    if isinstance(a, exp.Column) and not isinstance(b, exp.Column):
        return a, b, operator
    if isinstance(b, exp.Column) and not isinstance(a, exp.Column):
        return b, a, _FLIP[operator]
    return None


# ------------------------------------------------------------------- NULLs


@check("null_comparison", description="A value is compared to NULL with = or <>.")
def null_comparison(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        for node in own_nodes(model, _EQUALITY):
            sides = (unparen(node.this), unparen(node.expression))
            if any(isinstance(side, exp.Null) for side in sides):
                wanted = "IS NULL" if isinstance(node, exp.EQ) else "IS NOT NULL"
                yield ctx.finding(
                    "null_comparison",
                    Severity.HIGH,
                    (
                        f"{ctx.snippet(node)} compares with NULL using "
                        f"{'=' if isinstance(node, exp.EQ) else '<>'}. That is never true, so "
                        "the condition matches no rows."
                    ),
                    node,
                    suggestion=f"Use {wanted}.",
                )
                continue
            for column_side, other in (sides, sides[::-1]):
                if (
                    isinstance(column_side, exp.Column)
                    and isinstance(other, exp.Literal)
                    and other.is_string
                    # 'Null' in mixed case is a real surname; the placeholder is written in one case.
                    and other.name.strip() in ("NULL", "null")
                    and not _stored_value(ctx, model, column_side, other.name)
                ):
                    yield ctx.finding(
                        "null_comparison",
                        Severity.MEDIUM,
                        (
                            f"{ctx.snippet(node)} compares with the text 'NULL', which is a "
                            "four-letter string and not a missing value."
                        ),
                        node,
                        suggestion="Use IS NULL or IS NOT NULL if missing values are meant.",
                    )
                    break

        # CASE x WHEN NULL compares with =, so that branch is never taken.
        for node in own_nodes(model, (exp.Case,)):
            if node.this is None:
                continue
            for branch in node.args.get("ifs") or []:
                if isinstance(unparen(branch.this), exp.Null):
                    yield ctx.finding(
                        "null_comparison",
                        Severity.HIGH,
                        (
                            f"{ctx.snippet(node)} has a WHEN NULL branch. CASE compares with =, "
                            "which is never true for NULL, so that branch is never taken."
                        ),
                        node,
                        suggestion=f"Write CASE WHEN {ctx.snippet(node.this)} IS NULL THEN ... instead.",
                    )
                    break

        # x IN (..., NULL): the NULL in the list never matches anything.
        for node in own_nodes(model, (exp.In,)):
            if node.args.get("query") is not None or isinstance(node.parent, exp.Not):
                continue  # NOT IN with a NULL is not_in_nullable's finding
            values = [unparen(v) for v in node.expressions]
            if not any(isinstance(v, exp.Null) for v in values):
                continue
            only_null = all(isinstance(v, exp.Null) for v in values)
            yield ctx.finding(
                "null_comparison",
                Severity.HIGH if only_null else Severity.MEDIUM,
                (
                    f"{ctx.snippet(node)} has NULL in its list. IN compares with =, which is "
                    "never true for NULL, so "
                    + ("the condition matches no rows." if only_null else "rows holding NULL are not matched.")
                ),
                node,
                suggestion="Test IS NULL separately, joined with OR.",
            )


def _stored_value(ctx: CheckContext, model: SelectModel, column: exp.Expression, literal: str) -> bool:
    """True when the catalog lists ``literal`` among the values the column actually holds."""
    info = column_info(ctx, model, column)
    return info is not None and info[1].values is not None and literal in [str(v) for v in info[1].values]


def _query_scope(model: SelectModel, query: exp.Expression) -> Scope | None:
    """The scope of a subquery written inside ``model``'s SELECT."""
    inner = query.this if isinstance(query, exp.Subquery) else query
    for scope in model.scope.subquery_scopes:
        if scope.expression is inner:
            return scope
    return None


def _subquery_model(ctx: CheckContext, model: SelectModel, query: exp.Expression) -> SelectModel | None:
    scope = _query_scope(model, query)
    if scope is None or not isinstance(scope.expression, exp.Select):
        return None
    return ctx.cache.model(scope)


# ---- can a value be NULL?

_NEVER = "never"
_CAN = "can"
_UNKNOWN = "unknown"
_RANK = {_NEVER: 0, _UNKNOWN: 1, _CAN: 2}


@dataclass
class _Nullability:
    state: str
    what: str | None = None  # the value that can be NULL, for the message
    why: str | None = None  # how it comes to be NULL, when that is not simply "declared nullable"
    probe: tuple[Table, str] | None = None  # a base column whose NULLs would confirm it


_NOT_NULL = _Nullability(_NEVER)

# Predicates that are never true while one of their operands is NULL.
_NULL_REJECTING_BINARY = _EQUALITY + _ORDERED + tuple(
    getattr(exp, name) for name in ("Like", "ILike", "SimilarTo", "RegexpLike", "Glob") if hasattr(exp, name)
)
# Functions that never return NULL.
_NEVER_NULL_FUNCTIONS = tuple(
    getattr(exp, name)
    for name in ("Count", "CountIf", "RowNumber", "Rank", "DenseRank", "Ntile", "PercentRank", "CumeDist")
    if hasattr(exp, name)
)
# Window functions that are NULL where the window has no row to offer.
_EDGE_NULL_FUNCTIONS = tuple(
    getattr(exp, name) for name in ("Lag", "Lead", "NthValue") if hasattr(exp, name)
)
_NULL_TOLERANT_TESTS = tuple(
    getattr(exp, name) for name in ("Is", "Exists", "NullSafeEQ", "NullSafeNEQ") if hasattr(exp, name)
)


def _same_column(node: exp.Expression, column: exp.Column) -> bool:
    node = strip(node)
    return isinstance(node, exp.Column) and (node.table, node.name) == (column.table, column.name)


def _is_null_test(node: exp.Expression, column: exp.Column) -> bool:
    """``column IS NULL``."""
    node = unparen(node)
    return isinstance(node, exp.Is) and isinstance(node.expression, exp.Null) and _same_column(node.this, column)


def rejects_null(expr: exp.Expression, column: exp.Column) -> bool:
    """True when ``expr`` cannot be true for a row where ``column`` is NULL."""
    expr = unparen(expr)
    if isinstance(expr, exp.And):
        return rejects_null(expr.this, column) or rejects_null(expr.expression, column)
    if isinstance(expr, exp.Or):
        return rejects_null(expr.this, column) and rejects_null(expr.expression, column)
    if isinstance(expr, exp.Not):
        inner = unparen(expr.this)
        if isinstance(inner, exp.Is):
            return _is_null_test(inner, column)
        # NOT (x = 1), x NOT IN (...), x NOT BETWEEN ...: still unknown, never true, for NULL.
        if isinstance(inner, _NULL_REJECTING_BINARY + (exp.In, exp.Between)):
            return rejects_null(inner, column)
        return False
    if isinstance(expr, _NULL_REJECTING_BINARY):
        return _same_column(expr.this, column) or _same_column(expr.expression, column)
    if isinstance(expr, exp.Between):
        return any(_same_column(part, column) for part in (expr.this, expr.args["low"], expr.args["high"]))
    if isinstance(expr, exp.In):
        return _same_column(expr.this, column)
    return False


def _guarded(model: SelectModel, column: exp.Column, skip: exp.Expression | None = None) -> bool:
    """True when a filter of ``model`` already removes the rows where ``column`` is NULL."""
    source = model.sources.get(column.table)
    for conjunct in model.conjuncts:
        if skip is not None and unparen(conjunct.expr) is skip:
            continue
        if conjunct.origin != "where":
            # An outer join's ON clause does not remove rows, and a column from the optional
            # side of an outer join is NULL again for unmatched rows whatever ON says.
            if conjunct.outer_join or (source is not None and source.null_supplied):
                continue
        if rejects_null(conjunct.expr, column):
            return True
    return False


def _worst(parts: list[_Nullability]) -> _Nullability:
    worst = _NOT_NULL
    for part in parts:
        if _RANK[part.state] > _RANK[worst.state]:
            worst = part
    return worst


def _column_nullability(
    ctx: CheckContext, model: SelectModel, column: exp.Column, depth: int, skip: exp.Expression | None = None
) -> _Nullability:
    """``skip`` is a predicate to leave out when looking for a filter that removes the NULL rows."""
    source = model.sources.get(column.table)
    if source is None:
        return _NOT_NULL  # a column of an outer query: one value per evaluation, not ours to judge
    if _guarded(model, column, skip):
        return _NOT_NULL
    if source.null_supplied:
        return _Nullability(
            _CAN, ctx.snippet(column), "is NULL for rows the outer join found no match for"
        )
    if source.kind == "derived" and source.scope is not None:
        return _scope_nullability(ctx, source.scope, column.name, depth + 1)
    if source.table is None:
        return _Nullability(_UNKNOWN, ctx.snippet(column))
    declared = ctx.catalog.column(source.table, column.name, quoted=True)
    if declared is None:
        return _Nullability(_UNKNOWN, ctx.snippet(column))
    if declared.nullable is False:
        return _NOT_NULL
    return _Nullability(
        _CAN if declared.nullable else _UNKNOWN,
        f"{source.table.name}.{declared.name}",
        probe=(source.table, declared.name),
    )


def _scope_nullability(ctx: CheckContext, scope: Scope, name: str | None, depth: int, position: int = 0) -> _Nullability:
    """Nullability of one output column of a query, by name or (for set operations) position."""
    expression = scope.expression
    if depth > 32:
        return _Nullability(_UNKNOWN)
    if isinstance(expression, exp.SetOperation):
        if name is not None:
            names = list(expression.named_selects)
            position = names.index(name) if name in names else position
        branches = [_scope_nullability(ctx, branch, None, depth + 1, position) for branch in scope.union_scopes]
        if not branches:
            return _Nullability(_UNKNOWN)
        if isinstance(expression, exp.Except):
            return branches[0]  # only rows of the first branch come out
        if isinstance(expression, exp.Intersect):
            return _NOT_NULL if any(b.state == _NEVER for b in branches) else _worst(branches)
        return _worst(branches)
    if not isinstance(expression, exp.Select):
        return _Nullability(_UNKNOWN)
    model = ctx.cache.model(scope)
    if name is not None:
        defining = model.projection(name)
    else:
        projections = expression.expressions
        defining = unalias(projections[position]) if position < len(projections) else None
    if defining is None:
        return _Nullability(_UNKNOWN)
    return nullability(ctx, model, defining, depth)


def nullability(ctx: CheckContext, model: SelectModel, node: exp.Expression, depth: int = 0) -> _Nullability:
    """Whether the value ``node`` computes inside ``model`` can be NULL."""
    node = strip(unalias(node))
    if depth > 32:
        return _Nullability(_UNKNOWN)
    if isinstance(node, exp.Null):
        return _Nullability(_CAN, "NULL", "is NULL")
    if isinstance(node, (exp.Literal, exp.Boolean)):
        return _NOT_NULL
    if isinstance(node, exp.Column):
        return _column_nullability(ctx, model, node, depth)
    if isinstance(node, (exp.Select, exp.Subquery, exp.SetOperation)):
        return _Nullability(_UNKNOWN, ctx.snippet(node), "is NULL when the subquery returns no row")
    if isinstance(node, _NULL_TOLERANT_TESTS + _NEVER_NULL_FUNCTIONS):
        return _NOT_NULL
    text = ctx.snippet(node)

    def recurse(part: exp.Expression | None) -> _Nullability:
        return _NOT_NULL if part is None else nullability(ctx, model, part, depth + 1)

    if isinstance(node, exp.Window):
        inner = node.this
        while isinstance(inner, (exp.IgnoreNulls, exp.RespectNulls)):
            inner = inner.this
        if isinstance(inner, _NEVER_NULL_FUNCTIONS):
            return _NOT_NULL
        if isinstance(inner, _EDGE_NULL_FUNCTIONS):
            default = inner.args.get("default")
            if default is None or recurse(default).state != _NEVER:
                return _Nullability(_CAN, text, "is NULL where the window has no such row")
            return _Nullability(recurse(inner.this).state, text)
        argument = _worst([recurse(child) for child in inner.iter_expressions()])
        return _Nullability(argument.state, text) if argument.state != _NEVER else _NOT_NULL
    if isinstance(node, exp.Coalesce):
        parts = [recurse(part) for part in [node.this, *node.expressions]]
        if any(part.state == _NEVER for part in parts):
            return _NOT_NULL
        return _Nullability(_worst(parts).state, text)
    if isinstance(node, exp.Case):
        if node.args.get("default") is None:
            return _Nullability(_CAN, text, "is NULL when no branch matches")
        parts = [recurse(branch.args.get("true")) for branch in node.args.get("ifs") or []]
        parts.append(recurse(node.args.get("default")))
        worst = _worst(parts)
        return _Nullability(worst.state, text) if worst.state != _NEVER else _NOT_NULL
    if isinstance(node, exp.If):
        if node.args.get("false") is None:
            return _Nullability(_CAN, text, "is NULL when the condition is not met")
        worst = _worst([recurse(node.args.get("true")), recurse(node.args.get("false"))])
        return _Nullability(worst.state, text) if worst.state != _NEVER else _NOT_NULL
    if isinstance(node, exp.Nullif):
        return _Nullability(_CAN, text, "is NULL when its two arguments are equal")
    if isinstance(node, exp.AggFunc):
        argument = _worst([recurse(child) for child in node.iter_expressions()])
        if argument.state == _NEVER:
            return _NOT_NULL
        return _Nullability(argument.state, text, "is NULL when every value it reads is NULL")
    # Operators and ordinary functions pass a NULL operand through.
    parts = [recurse(child) for child in node.iter_expressions()]
    worst = _worst(parts)
    if worst.state == _NEVER:
        return _NOT_NULL
    return _Nullability(worst.state, worst.what or text, worst.why, worst.probe)


def _query_nullability(ctx: CheckContext, model: SelectModel, query: exp.Expression) -> _Nullability | None:
    """The worst case over the columns a NOT IN subquery returns; ``None`` when it cannot be read."""
    scope = _query_scope(model, query)
    if scope is None:
        return None
    expression = scope.expression
    if not isinstance(expression, (exp.Select, exp.SetOperation)):
        return None
    width = len(expression.selects) if isinstance(expression, exp.Query) else 0
    if width == 0:
        return None
    return _worst([_scope_nullability(ctx, scope, None, 0, position) for position in range(width)])


def _or_branches(node: exp.Expression) -> list[exp.Expression]:
    """The alternatives of the OR that ``node`` is a direct branch of (just ``node`` if none)."""
    top = node
    while isinstance(top.parent, (exp.Paren, exp.Or)):
        top = top.parent
    out: list[exp.Expression] = []
    stack = [top]
    while stack:
        current = unparen(stack.pop())
        if isinstance(current, exp.Or):
            stack.extend((current.this, current.expression))
        else:
            out.append(current)
    return out


def _bare(table_node: exp.Expression) -> exp.Expression:
    table = table_node.copy()
    table.set("alias", None)
    return table


def _not_in_forms(model: SelectModel) -> Iterator[tuple[exp.Expression, exp.Expression, list[exp.Expression], exp.Expression | None]]:
    """``(node, left side, listed values, subquery)`` for every NOT IN, however it is spelt."""
    for node in own_nodes(model, (exp.Not, exp.NEQ)):
        if isinstance(node, exp.Not):
            inner = unparen(node.this)
            if isinstance(inner, exp.In):
                yield node, inner.this, list(inner.expressions), inner.args.get("query")
        else:
            right = unparen(node.expression)
            if isinstance(right, exp.All):  # x <> ALL (subquery) is NOT IN by another name
                yield node, node.this, [], right.this


@check("not_in_nullable", description="NOT IN is used against values that can be NULL.")
def not_in_nullable(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        for node, left, values, query in _not_in_forms(model):
            if any(isinstance(unparen(v), exp.Null) for v in values):
                yield ctx.finding(
                    "not_in_nullable",
                    Severity.HIGH,
                    (
                        f"{ctx.snippet(node)} has NULL in its list. NOT IN with a NULL in the "
                        "list is never true, so the condition matches no rows."
                    ),
                    node,
                    suggestion="Remove NULL from the list and test IS NOT NULL separately.",
                )
                continue
            if query is None:
                yield from _not_in_list(ctx, model, node, left, values)
                continue
            result = _query_nullability(ctx, model, query)
            if result is None or result.state == _NEVER:
                continue
            certain = result.state == _CAN
            where = result.what or "a value"
            how = result.why or ("can be NULL" if certain else "may be NULL (not declared either way)")
            finding = ctx.finding(
                "not_in_nullable",
                Severity.MEDIUM if certain else Severity.LOW,
                (
                    f"NOT IN reads {where}, which {how}. If the subquery returns even one NULL, "
                    "NOT IN is never true and the whole condition matches no rows."
                ),
                node,
                suggestion="Use NOT EXISTS, or add IS NOT NULL inside the subquery.",
                column=where,
            )
            if result.probe is not None:
                ctx.probes.append(Probe(PROBE_HAS_NULL, result.probe[0], (result.probe[1],), finding))
            yield finding


def _not_in_list(
    ctx: CheckContext, model: SelectModel, node: exp.Expression, left: exp.Expression, values: list[exp.Expression]
) -> Iterable[Finding]:
    """NOT IN with a written-out list: a nullable value in the list, or a nullable column tested."""
    for value in values:
        listed = nullability(ctx, model, value)
        if listed.state != _CAN:
            continue
        yield ctx.finding(
            "not_in_nullable",
            Severity.MEDIUM,
            (
                f"{ctx.snippet(node)} lists {listed.what or ctx.snippet(value)}, which "
                f"{listed.why or 'can be NULL'}. For a row where it is NULL, NOT IN is never "
                "true and the row is left out."
            ),
            node,
            suggestion="Compare with that column separately and allow for NULL explicitly.",
            column=listed.what,
        )
        return

    column = strip(left)
    if not isinstance(column, exp.Column) or column.table not in model.sources:
        return
    if any(_is_null_test(branch, column) for branch in _or_branches(node)):
        return  # (x NOT IN (...) OR x IS NULL): NULLs are kept on purpose
    source = model.sources[column.table]
    # Another filter that already removes the NULL rows makes this one harmless.
    tested = _column_nullability(ctx, model, column, 0, skip=node)
    if tested.state != _CAN:
        return
    name = tested.what or ctx.snippet(column)
    finding = ctx.finding(
        "not_in_nullable",
        Severity.LOW,
        (
            f"{ctx.snippet(node)} tests {name}, which {tested.why or 'can be NULL'}. NOT IN is "
            "never true for a NULL, so rows where it is NULL are left out along with the listed values."
        ),
        node,
        suggestion=f"Add OR {ctx.snippet(column)} IS NULL if those rows are wanted.",
        column=name,
        variant="tested_column",
    )
    if tested.probe is not None and source.kind == "table":
        bare = exp.column(tested.probe[1], quoted=True)
        ctx.probes.append(
            Probe(
                PROBE_QUERY,
                tested.probe[0],
                (tested.probe[1],),
                finding,
                query=exp.select(exp.Literal.number(1))
                .from_(_bare(source.node))
                .where(exp.Is(this=bare, expression=exp.Null()))
                .limit(1),
                on_rows=(
                    Severity.MEDIUM,
                    " Checked against the data: the column does hold NULLs, and those rows are left out.",
                    "has_null",
                ),
                on_empty=(
                    Severity.INFO,
                    " Checked against the data: the column holds no NULLs at present.",
                    "no_null",
                ),
            )
        )
    yield finding


# ------------------------------------------------------------------- types

_NUMBER_RE = re.compile(r"^\s*[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?\s*$")
_ISO_DATE_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(Z|[-+]\d{2}(?::?\d{2})?)?)?$"
)
_LOOSE_DATE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}([ T].*)?$")
_BOOL_WORDS = {"true", "false", "t", "f", "yes", "no", "y", "n", "1", "0", "on", "off"}
# Words these engines accept as a date or timestamp.
_SPECIAL_TEMPORAL = {
    "postgres": {"epoch", "infinity", "-infinity", "now", "today", "tomorrow", "yesterday"},
    "redshift": {"epoch", "infinity", "-infinity", "now", "today", "tomorrow", "yesterday"},
    "duckdb": {"epoch", "infinity", "-infinity"},
}
# SQLite compares stored timestamp text with a literal as text, so a year or year-month
# works as the bound of a range.
_PREFIX_BOUND_RE = re.compile(r"^\d{4}(-\d{2})?$")
# A date written as a bare number, e.g. 20240131.
_NUMERIC_DATE_RE = re.compile(r"^(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")


def _has_cast(node: exp.Expression) -> bool:
    node = unparen(node)
    return isinstance(node, (exp.Cast, exp.TryCast))


@check("type_mismatch", needs_types=True, description="Values of unlike types are compared.")
def type_mismatch(ctx: CheckContext) -> Iterable[Finding]:
    dialect = ctx.dialect
    for model in ctx.cache.models():
        for node in own_nodes(model, _EQUALITY + _ORDERED + (exp.In, exp.Between)):
            for left, right, operator in _comparison_pairs(node):
                if _has_cast(left) or _has_cast(right):
                    continue
                a, b = unparen(left), unparen(right)
                if isinstance(a, exp.Column) and isinstance(b, exp.Column):
                    fa, fb = family(ctx, model, a), family(ctx, model, b)
                    comparable = {T.NUMERIC, T.TEXT, T.DATE, T.TIMESTAMP, T.BOOLEAN}
                    if fa == fb or fa not in comparable or fb not in comparable:
                        continue
                    if {fa, fb} == {T.DATE, T.TIMESTAMP}:
                        if operator not in ("=", "<>", "in"):
                            continue
                        yield ctx.finding(
                            "type_mismatch",
                            Severity.MEDIUM,
                            (
                                f"{ctx.snippet(node)} tests a date against a timestamp for "
                                "equality. They are equal only when the timestamp is exactly "
                                "midnight, so most rows will not match."
                            ),
                            node,
                            suggestion="Cast the timestamp to a date, or compare with a range.",
                        )
                        continue
                    if dialect == "sqlite" and T.BOOLEAN in (fa, fb) and T.NUMERIC in (fa, fb):
                        continue
                    yield ctx.finding(
                        "type_mismatch",
                        Severity.MEDIUM,
                        (
                            f"{ctx.snippet(node)} compares a {fa} column with a {fb} column. "
                            + (
                                "SQLite compares them without converting, so they rarely match."
                                if dialect == "sqlite"
                                else "The engine either rejects this or converts one side implicitly."
                            )
                        ),
                        node,
                        suggestion="Check that these are the intended columns; cast one side if so.",
                        left=fa,
                        right=fb,
                    )
                    continue

                oriented = _oriented(left, right, operator)
                if oriented is None:
                    continue
                column, other, op = oriented
                if isinstance(other, exp.Neg):
                    other = other.this
                if column_info(ctx, model, column) is None:
                    continue  # a computed column: its inferred type is too uncertain to judge on
                fcol = family(ctx, model, column)
                if not isinstance(other, exp.Literal):
                    # A column against a computed value of the other kind, e.g. id = name || ''.
                    # SQLite converts between the two where it can, so it is left alone there.
                    fother = family(ctx, model, other)
                    if (
                        dialect != "sqlite"
                        and {fcol, fother} == {T.NUMERIC, T.TEXT}
                        and not isinstance(other, (exp.Null, exp.Placeholder, exp.Parameter, exp.Subquery))
                        and not any(isinstance(n, (exp.Cast, exp.TryCast)) for n in walk_own(other))
                    ):
                        yield ctx.finding(
                            "type_mismatch",
                            Severity.HIGH if dialect == "postgres" else Severity.MEDIUM,
                            (
                                f"{ctx.snippet(node)} compares a {fcol} column with a {fother} value. "
                                "The engine either rejects this or converts one side implicitly."
                            ),
                            node,
                            suggestion="Check that these are the intended columns; cast one side if so.",
                            left=fcol,
                            right=fother,
                        )
                    continue
                text = other.name
                problem: tuple[Severity, str] | None = None
                if other.is_string:
                    if fcol == T.NUMERIC and not _NUMBER_RE.match(text):
                        problem = (
                            Severity.HIGH,
                            f"a number column with the text {other.sql()}, which is not a number",
                        )
                    elif fcol in T.TEMPORAL and not _ISO_DATE_RE.match(text.strip()):
                        word = text.strip().lower()
                        if word in _SPECIAL_TEMPORAL.get(dialect, ()):
                            pass  # 'now', 'infinity' and the like are valid input here
                        elif dialect == "sqlite" and op in ("<", "<=", ">", ">=") and _PREFIX_BOUND_RE.match(word):
                            pass  # '2024-01' is a sound range bound for text timestamps
                        elif dialect == "sqlite" or not re.search(r"\d", text):
                            problem = (
                                Severity.HIGH if dialect == "sqlite" else Severity.MEDIUM,
                                f"a {fcol} column with {other.sql()}, which is not a date in "
                                "YYYY-MM-DD form",
                            )
                    elif fcol == T.BOOLEAN and text.strip().lower() not in _BOOL_WORDS:
                        problem = (Severity.MEDIUM, f"a boolean column with the text {other.sql()}")
                else:
                    if fcol == T.TEXT and dialect != "sqlite":
                        problem = (
                            Severity.HIGH if dialect == "postgres" else Severity.MEDIUM,
                            f"a text column with the number {text}",
                        )
                    elif fcol in T.TEMPORAL:
                        try:
                            small = abs(float(text)) < 100000
                        except ValueError:
                            small = False
                        if small or dialect != "sqlite":
                            problem = (Severity.HIGH, f"a {fcol} column with the number {text}")
                        elif _NUMERIC_DATE_RE.match(text):
                            # Too large to be a day count and not an epoch time: a date without quotes.
                            problem = (
                                Severity.HIGH,
                                f"a {fcol} column with the number {text}, which looks like a date "
                                "written without quotes. SQLite orders every number before any "
                                "timestamp text, so the comparison does not look at the date",
                            )
                if problem is None:
                    continue
                severity, what = problem
                yield ctx.finding(
                    "type_mismatch",
                    severity,
                    f"{ctx.snippet(node)} compares {what}.",
                    node,
                    suggestion="Compare like with like: fix the literal or the column.",
                    column_family=fcol,
                )


def _whole_number(node: exp.Expression) -> int | None:
    node = unparen(node)
    if isinstance(node, exp.Literal) and not node.is_string and node.name.isdigit():
        return int(node.name)
    return None


def _scaled_before_dividing(numerator: exp.Expression) -> bool:
    """``100 * a / b``: multiplying by 100 or more first is how a whole-number percentage is written."""
    numerator = unparen(numerator)
    if not isinstance(numerator, exp.Mul):
        return False
    for side in (numerator.this, numerator.expression):
        scale = _whole_number(side)
        if scale is not None and scale >= 100 and scale % 100 == 0:
            return True
    return False


def _deliberate_bucketing(model: SelectModel, node: exp.Div) -> bool:
    """``(qty / 10) * 10``, or ``GROUP BY qty / 10``: dropping the fraction is the point."""
    divisor = _whole_number(node.expression)
    if divisor is None:
        return False
    outer: exp.Expression = node
    while isinstance(outer.parent, exp.Paren):
        outer = outer.parent
    parent = outer.parent
    if isinstance(parent, exp.Mul):
        other = parent.expression if parent.this is outer else parent.this
        if _whole_number(other) == divisor:
            return True
    group = model.group
    if group is not None and any(unparen(expr) == node for expr in group.expressions):
        return True
    return False


@check("integer_division", needs_types=True, description="Two integers are divided, dropping the fraction.")
def integer_division(ctx: CheckContext) -> Iterable[Finding]:
    if ctx.dialect not in ("sqlite", "postgres", "redshift", "tsql"):
        return
    integers = exp.DataType.INTEGER_TYPES
    for model in ctx.cache.models():
        for node in own_nodes(model, (exp.Div,)):
            left, right = unparen(node.this), unparen(node.expression)
            lt, rt = left.type, right.type
            if lt is None or rt is None or lt.this not in integers or rt.this not in integers:
                continue
            if isinstance(left, exp.Literal) and isinstance(right, exp.Literal):
                continue
            if _scaled_before_dividing(left) or _deliberate_bucketing(model, node):
                continue
            yield ctx.finding(
                "integer_division",
                Severity.MEDIUM,
                (
                    f"{ctx.snippet(node)} divides one integer by another. {ctx.dialect} drops "
                    "the fraction, so a ratio or percentage comes out as 0 or a whole number."
                ),
                node,
                suggestion="Multiply by 1.0 or cast one side to a decimal type before dividing.",
            )


# ------------------------------------------------------------------- dates


def _parse_date_literal(text: str) -> tuple[_dt.date | None, bool, bool] | None:
    """``(date, has_time, valid)`` for an ISO-looking literal, else ``None``."""
    match = _ISO_DATE_RE.match(text.strip())
    if not match:
        return None
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    has_time = match.group(4) is not None and (
        match.group(4), match.group(5), match.group(6) or "00"
    ) != ("00", "00", "00")
    try:
        return _dt.date(year, month, day), has_time, True
    except ValueError:
        return None, has_time, False


def _temporal_kind(ctx: CheckContext, model: SelectModel, column: exp.Column) -> str | None:
    """``date`` or ``timestamp`` for a column that holds points in time, else ``None``."""
    fam = family(ctx, model, column)
    if fam in T.TEMPORAL:
        return fam
    if fam in (T.TEXT, T.UNKNOWN) and ctx.dialect == "sqlite":
        info = column_info(ctx, model, column)
        name = (info[1].name if info is not None else column.name).lower()
        if re.search(r"(_at|_time|_ts|timestamp|datetime)$", name):
            return T.TIMESTAMP
        if re.search(r"(_date|_day|_on|^date|_dt)$", name):
            return T.DATE
    return None


# Columns that mark when something stops. "end > date" is an as-of test, not the start of a range.
_END_COLUMN = re.compile(r"(_to|_until|_end|^end_|ended_|expires|expiry|expired|due_|_due|closed_|left_at|revoked)", re.IGNORECASE)


# '... 23:59:59', with or without a run of nines after it.
_END_OF_DAY_RE = re.compile(r"[ T]23:59:59(\.9+)?\s*(Z|[-+]\d{2}(:?\d{2})?)?$")


def _starts_mid_month(ctx: CheckContext, model: SelectModel, column: exp.Column) -> bool:
    """True when the same column also has a lower bound that is not the first of a month.

    ``>= '2024-01-30' AND < '2024-01-31'`` is one whole day, not a month cut short.
    """
    for conjunct in model.conjuncts:
        for left, right, operator in _comparison_pairs(unparen(conjunct.expr)):
            oriented = _oriented(left, right, operator)
            if oriented is None:
                continue
            other_column, value, op = oriented
            if op not in (">", ">=") or not _same_column(other_column, column):
                continue
            value = strip(value)
            if not isinstance(value, exp.Literal) or not value.is_string:
                continue
            parsed = _parse_date_literal(value.name)
            if parsed is not None and parsed[0] is not None and parsed[0].day != 1:
                return True
    return False


def _last_day_of_month(day: _dt.date) -> bool:
    return (day + _dt.timedelta(days=1)).month != day.month


@check("date_boundary", description="A date range starts or stops one step away from where it should.")
def date_boundary(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        for node in own_nodes(model, _EQUALITY + _ORDERED + (exp.Between, exp.In)):
            pairs = _comparison_pairs(node)
            between_low: _dt.date | None = None
            if isinstance(node, exp.Between):
                low = unparen(node.args["low"])
                if isinstance(low, exp.Literal) and low.is_string:
                    parsed_low = _parse_date_literal(low.name)
                    if parsed_low and parsed_low[0] is not None:
                        between_low = parsed_low[0]
            for left, right, operator in pairs:
                oriented = _oriented(left, right, operator)
                if oriented is None:
                    continue
                column, other, op = oriented
                other = strip(other)
                if not isinstance(other, exp.Literal) or not other.is_string:
                    continue
                kind = _temporal_kind(ctx, model, column)
                if kind is None:
                    continue
                text = other.name
                parsed = _parse_date_literal(text)
                info = column_info(ctx, model, column)
                if parsed is None:
                    if _LOOSE_DATE_RE.match(text.strip()) and ctx.dialect == "sqlite":
                        yield ctx.finding(
                            "date_boundary",
                            Severity.HIGH,
                            (
                                f"{ctx.snippet(node)} uses the date {other.sql()} without leading "
                                "zeros. SQLite compares dates as text, so this sorts in the wrong "
                                "place."
                            ),
                            node,
                            suggestion="Write the date as YYYY-MM-DD with two-digit month and day.",
                        )
                    continue
                day, has_time, valid = parsed
                if not valid or day is None:
                    yield ctx.finding(
                        "date_boundary",
                        Severity.HIGH,
                        f"{ctx.snippet(node)} uses {other.sql()}, which is not a real calendar date.",
                        node,
                        suggestion="Use the true last day of the month, or a < first-day-of-next-month bound.",
                    )
                    continue
                if info is not None and ctx.dialect == "sqlite":
                    ctx.probes.append(
                        Probe(PROBE_TIMESTAMP_FORMAT, info[0], (info[1].name,), None, literal=text)
                    )
                next_day = (day + _dt.timedelta(days=1)).isoformat()
                if has_time:
                    if op == "<=" and kind == T.TIMESTAMP and _END_OF_DAY_RE.search(text.strip()):
                        yield ctx.finding(
                            "date_boundary",
                            Severity.LOW,
                            (
                                f"{ctx.snippet(node)} stops at the last whole second of "
                                f"{day.isoformat()}, so a row stamped in the fraction of a second "
                                "after that is left out."
                            ),
                            node,
                            suggestion=f"Use < '{next_day}' to include the whole of {day.isoformat()}.",
                            bound=text,
                        )
                    continue
                if op == "<=" and kind == T.TIMESTAMP:
                    first = day.day == 1
                    yield ctx.finding(
                        "date_boundary",
                        Severity.LOW if first else Severity.MEDIUM,
                        (
                            f"{ctx.snippet(node)} stops at the start of {day.isoformat()} "
                            "because the column holds times as well as dates. "
                            + (
                                "Only rows stamped exactly at midnight on that day are included."
                                if first
                                else "Everything later that day is left out."
                            )
                        ),
                        node,
                        suggestion=f"Use < '{next_day}' to include the whole of {day.isoformat()}.",
                        bound=text,
                    )
                elif op == "<=" and kind == T.DATE and day.day == 1 and between_low is not None and between_low.day == 1:
                    yield ctx.finding(
                        "date_boundary",
                        Severity.MEDIUM,
                        (
                            f"{ctx.snippet(node)} runs from the first of one month to the first "
                            "of another, inclusive, so it takes in one day of the following period."
                        ),
                        node,
                        suggestion=f"End the range with < '{day.isoformat()}'.",
                        bound=text,
                    )
                elif op == "<" and _last_day_of_month(day) and not _starts_mid_month(ctx, model, column):
                    yield ctx.finding(
                        "date_boundary",
                        Severity.MEDIUM,
                        (
                            f"{ctx.snippet(node)} stops before {day.isoformat()}, the last day "
                            "of the month, so that day is left out."
                        ),
                        node,
                        suggestion=f"Use < '{next_day}'.",
                        bound=text,
                    )
                elif op == ">" and day.day == 1 and kind == T.DATE and not _END_COLUMN.search(column.name):
                    yield ctx.finding(
                        "date_boundary",
                        Severity.MEDIUM,
                        (
                            f"{ctx.snippet(node)} starts after {day.isoformat()}, so the first "
                            "day of the period is left out."
                        ),
                        node,
                        suggestion=f"Use >= '{day.isoformat()}'.",
                        bound=text,
                    )


# ------------------------------------------------------------ filter values


def judge_value(values: Iterable[Any], literal: str) -> tuple[bool, list[str]]:
    """Whether ``literal`` is among ``values``, and the nearest stored values if not."""
    stored = [v for v in values if v is not None]
    as_text = [str(v) for v in stored]
    if literal in as_text:
        return True, []
    folded = literal.strip().casefold()
    near = [v for v in as_text if v.strip().casefold() == folded]
    if not near:
        by_folded: dict[str, str] = {}
        for value in as_text:
            by_folded.setdefault(value.strip().casefold(), value)
        close = difflib.get_close_matches(folded, list(by_folded), n=3, cutoff=0.6)
        near = [by_folded[c] for c in close]
    return False, near


def value_finding(
    ctx_snippet: str | None,
    table: Table,
    column: Column,
    literal: str,
    near: list[str],
    negated: bool,
    stage: str,
    partial: bool = False,
) -> Finding:
    """``partial``: other values in the same IN list are stored, so the list as a whole still matches."""
    where = f"{table.name}.{column.name}"
    hint = f" The closest stored value is {near[0]!r}." if near else ""
    if partial:
        effect = " The other listed values still match, so the condition returns fewer rows than intended."
    elif negated:
        effect = " The condition therefore excludes nothing."
    else:
        effect = " The condition therefore matches no rows."
    return Finding(
        code="filter_value",
        severity=Severity.MEDIUM if negated or partial else Severity.HIGH,
        message=f"The filter uses {literal!r}, but {where} never holds that value." + hint + effect,
        stage=stage,
        snippet=ctx_snippet,
        suggestion=(f"Use {near[0]!r}." if near else "Check how this value is stored in the column."),
        evidence={"column": where, "literal": literal, "nearest": near},
    )


def _case_folded_mismatch(left: exp.Expression, right: exp.Expression) -> tuple[str, exp.Literal] | None:
    """``LOWER(x) = 'US'``: a case-folded value against a literal in the other case."""
    a, b = unparen(left), unparen(right)
    for folded, other in ((a, b), (b, a)):
        if not isinstance(folded, (exp.Lower, exp.Upper)):
            continue
        if not (isinstance(other, exp.Literal) and other.is_string):
            continue
        if isinstance(folded, exp.Lower) and other.name != other.name.lower():
            return "lower-case", other
        if isinstance(folded, exp.Upper) and other.name != other.name.upper():
            return "upper-case", other
    return None


def _any_listed_value_stored(ctx: CheckContext, model: SelectModel, node: exp.In) -> bool:
    column = unparen(node.this)
    info = column_info(ctx, model, column) if isinstance(column, exp.Column) else None
    if info is None or info[1].values is None:
        return False
    for value in node.expressions:
        value = unparen(value)
        if isinstance(value, exp.Literal) and value.is_string and judge_value(info[1].values, value.name)[0]:
            return True
    return False


@check("filter_value", description="A filter uses a value the column never holds.")
def filter_value(ctx: CheckContext) -> Iterable[Finding]:
    seen: set[tuple[str, str, str]] = set()
    for model in ctx.cache.models():
        for node in own_nodes(model, _EQUALITY + (exp.In, exp.Like)):
            negated = isinstance(node, exp.NEQ) or (
                isinstance(node, (exp.In, exp.Like)) and isinstance(node.parent, exp.Not)
            )
            pairs = _comparison_pairs(node)
            if isinstance(node, exp.Like):
                pattern = unparen(node.expression)
                if not (isinstance(pattern, exp.Literal) and pattern.is_string) or re.search(r"[%_\\]", pattern.name):
                    continue
                pairs = [(node.this, node.expression, "=")]  # LIKE with no wildcard is an equality test
            # When part of an IN list is stored, the list still matches; see below.
            some_listed_value_is_stored = isinstance(node, exp.In) and not negated and _any_listed_value_stored(
                ctx, model, node
            )
            for left, right, operator in pairs:
                folded = _case_folded_mismatch(left, right)
                if folded is not None:
                    function, literal = folded
                    yield ctx.finding(
                        "filter_value",
                        Severity.MEDIUM if negated else Severity.HIGH,
                        (
                            f"{ctx.snippet(node)} compares {function} text with {literal.sql()}, "
                            f"which is not all {function}. The two can never be equal, so the "
                            + ("condition excludes nothing." if negated else "condition matches no rows.")
                        ),
                        node,
                        suggestion=f"Write the literal in {function} as well.",
                        literal=literal.name,
                    )
                    continue
                oriented = _oriented(left, right, operator)
                if oriented is None:
                    continue
                column, other, _ = oriented
                if not isinstance(other, exp.Literal) or not other.is_string:
                    continue
                info = column_info(ctx, model, column)
                if info is None:
                    continue
                table, col = info
                fam = T.family_of_declared(col.type, ctx.dialect)
                if fam in (T.NUMERIC, T.DATE, T.TIMESTAMP, T.TIME, T.BOOLEAN, T.JSON, T.BINARY):
                    continue
                if _temporal_kind(ctx, model, column) is not None:
                    continue
                key = (table.qualified_name, col.name, other.name)
                if key in seen:
                    continue
                seen.add(key)
                if col.values is not None:
                    present, near = judge_value(col.values, other.name)
                    if present:
                        continue
                    if some_listed_value_is_stored:
                        # 'US' in ('US', 'CA') does match. Only a listed value that looks like a
                        # misspelling of a stored one is worth reporting.
                        if near:
                            yield value_finding(
                                ctx.snippet(node), table, col, other.name, near, negated, "static", partial=True
                            )
                        continue
                    yield value_finding(ctx.snippet(node), table, col, other.name, near, negated, "static")
                else:
                    ctx.probes.append(
                        Probe(
                            PROBE_VALUE,
                            table,
                            (col.name,),
                            None,
                            literal=other.name,
                            operator="<>" if negated else "=",
                            snippet=ctx.snippet(node),
                            siblings=tuple(
                                v.name
                                for v in (node.expressions if isinstance(node, exp.In) and not negated else [])
                                if isinstance(v, exp.Literal) and v.is_string and v.name != other.name
                            ),
                        )
                    )
