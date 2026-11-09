"""Checks about individual conditions: NULL handling, types, dates and filter values."""

from __future__ import annotations

import datetime as _dt
import difflib
import re
from typing import Any, Iterable, Iterator

from sqlglot import exp

from .. import types as T
from ..catalog import Column, Table
from ..model import SelectModel, strip, unalias, walk_own
from ..report import Finding, Severity
from .base import (
    PROBE_HAS_NULL,
    PROBE_TIMESTAMP_FORMAT,
    PROBE_VALUE,
    CheckContext,
    Probe,
    check,
)

_ORDERED = (exp.GT, exp.GTE, exp.LT, exp.LTE)
_EQUALITY = (exp.EQ, exp.NEQ)


def own_nodes(model: SelectModel, kinds: tuple[type, ...]) -> Iterator[exp.Expression]:
    for node in walk_own(model.select):
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
                    and other.name.strip().upper() == "NULL"
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


def _subquery_model(ctx: CheckContext, model: SelectModel, query: exp.Expression) -> SelectModel | None:
    inner = query.this if isinstance(query, exp.Subquery) else query
    if not isinstance(inner, exp.Select):
        return None
    for scope in model.scope.subquery_scopes:
        if scope.expression is inner:
            return ctx.cache.model(scope)
    return None


def _guarded_not_null(model: SelectModel, column: exp.Column) -> bool:
    for conjunct in model.conjuncts:
        expr = unparen(conjunct.expr)
        if isinstance(expr, exp.Not):
            inner = unparen(expr.this)
            if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
                target = strip(inner.this)
                if isinstance(target, exp.Column) and (target.table, target.name) == (column.table, column.name):
                    return True
    return False


@check("not_in_nullable", description="NOT IN is used against values that can be NULL.")
def not_in_nullable(ctx: CheckContext) -> Iterable[Finding]:
    for model in ctx.cache.models():
        for node in own_nodes(model, (exp.Not,)):
            inner = unparen(node.this)
            if not isinstance(inner, exp.In):
                continue
            if any(isinstance(unparen(v), exp.Null) for v in inner.expressions):
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
            query = inner.args.get("query")
            if query is None:
                continue
            sub = _subquery_model(ctx, model, query)
            if sub is None or len(sub.select.expressions) != 1:
                continue
            projected = strip(unalias(sub.select.expressions[0]))
            if not isinstance(projected, exp.Column) or projected.table not in sub.sources:
                continue
            if _guarded_not_null(sub, projected):
                continue
            info = column_info(ctx, sub, projected)
            source = sub.sources[projected.table]
            nullable: bool | None = info[1].nullable if info is not None else None
            if source.null_supplied:
                nullable = True
            if nullable is False:
                continue
            where = f"{info[0].name}.{info[1].name}" if info is not None else ctx.snippet(projected)
            finding = ctx.finding(
                "not_in_nullable",
                Severity.MEDIUM if nullable else Severity.LOW,
                (
                    f"NOT IN reads {where}, which "
                    + ("can be NULL" if nullable else "may be NULL (not declared either way)")
                    + ". If the subquery returns even one NULL, NOT IN is never true and the "
                    "whole condition matches no rows."
                ),
                node,
                suggestion="Use NOT EXISTS, or add IS NOT NULL inside the subquery.",
                column=where,
            )
            if info is not None and not source.null_supplied:
                ctx.probes.append(Probe(PROBE_HAS_NULL, info[0], (info[1].name,), finding))
            yield finding


# ------------------------------------------------------------------- types

_NUMBER_RE = re.compile(r"^\s*[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?\s*$")
_ISO_DATE_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(Z|[-+]\d{2}(?::?\d{2})?)?)?$"
)
_LOOSE_DATE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}([ T].*)?$")
_BOOL_WORDS = {"true", "false", "t", "f", "yes", "no", "y", "n", "1", "0", "on", "off"}


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
                column, other, _ = oriented
                if isinstance(other, exp.Neg):
                    other = other.this
                if not isinstance(other, exp.Literal):
                    continue
                if column_info(ctx, model, column) is None:
                    continue  # a computed column: its inferred type is too uncertain to judge on
                fcol = family(ctx, model, column)
                text = other.name
                problem: tuple[Severity, str] | None = None
                if other.is_string:
                    if fcol == T.NUMERIC and not _NUMBER_RE.match(text):
                        problem = (
                            Severity.HIGH,
                            f"a number column with the text {other.sql()}, which is not a number",
                        )
                    elif fcol in T.TEMPORAL and not _ISO_DATE_RE.match(text.strip()):
                        if dialect == "sqlite" or not re.search(r"\d", text):
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
                if has_time:
                    continue
                next_day = (day + _dt.timedelta(days=1)).isoformat()
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
                elif op == "<" and _last_day_of_month(day):
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
) -> Finding:
    where = f"{table.name}.{column.name}"
    hint = f" The closest stored value is {near[0]!r}." if near else ""
    return Finding(
        code="filter_value",
        severity=Severity.MEDIUM if negated else Severity.HIGH,
        message=(
            f"The filter uses {literal!r}, but {where} never holds that value."
            + hint
            + (" The condition therefore excludes nothing." if negated else " The condition therefore matches no rows.")
        ),
        stage=stage,
        snippet=ctx_snippet,
        suggestion=(f"Use {near[0]!r}." if near else "Check how this value is stored in the column."),
        evidence={"column": where, "literal": literal, "nearest": near},
    )


@check("filter_value", description="A filter uses a value the column never holds.")
def filter_value(ctx: CheckContext) -> Iterable[Finding]:
    seen: set[tuple[str, str, str]] = set()
    for model in ctx.cache.models():
        for node in own_nodes(model, _EQUALITY + (exp.In,)):
            negated = isinstance(node, exp.NEQ) or (
                isinstance(node, exp.In) and isinstance(node.parent, exp.Not)
            )
            for left, right, operator in _comparison_pairs(node):
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
                    if not present:
                        yield value_finding(
                            ctx.snippet(node), table, col, other.name, near, negated, "static"
                        )
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
                        )
                    )
