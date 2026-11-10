"""Translation of a statement from its dialect to DuckDB for the fixture stage (ADR 0003, B1).

Fixtures live in DuckDB; a host's BigQuery (or Postgres, SQLite) statement is run on them
through sqlglot's transpilation — but only when every construct it uses is on the VERIFIED
allow-list for that dialect: functions, clauses, types and date parts whose translation a test
has shown to give the same result as the source engine on fixture data. A construct that is not
on the list raises :class:`Untranslatable`, and the fixture stage reports ``not_run`` with the
construct's name: a lost check, never a verdict. The list grows only by proof
(``tests/test_bigquery_translation.py``; entries confirmed against the real service through
the offline adapter are the ground truth).

A host also declares how its names map to the bundle's tables (``tables:`` in the compile
config): `` `proj-x.hr.emp_period_snapshot` `` → ``hr.emp_period_snapshot``. A three-part name
with no mapping is untranslatable; nothing is guessed from a name's tail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

import sqlglot
from sqlglot import exp

from .errors import SqlgamiError


class Untranslatable(SqlgamiError):
    """The statement uses a construct whose translation to DuckDB has not been verified."""

    def __init__(self, construct: str) -> None:
        super().__init__(f"untranslatable: {construct}")
        self.construct = construct


# Constructs whose DuckDB translation is verified equal on fixture data. Keyed by kind:
#   func:NAME   a function (sqlglot's canonical name, or the raw name for anonymous ones)
#   type:NAME   a type named in a CAST
#   part:NAME   a date/time part named in DATE_TRUNC, DATE_DIFF, EXTRACT and the like
#   clause:X    QUALIFY, UNNEST, STRUCT, ARRAY literals, window functions
CORE = frozenset(
    {
        # aggregates and windows
        "func:COUNT", "func:SUM", "func:MIN", "func:MAX", "func:AVG", "func:COUNT_IF", "func:ANY_VALUE",
        "func:ARRAY_AGG", "func:GROUP_CONCAT", "func:ROW_NUMBER", "func:RANK", "func:DENSE_RANK", "func:LAG", "func:LEAD",
        "func:FIRST_VALUE", "func:LAST_VALUE", "clause:window",
        # scalar
        "func:CAST", "func:TRY_CAST", "func:COALESCE", "func:IF", "func:CASE", "func:EXISTS", "func:NULLIF", "func:GREATEST", "func:LEAST", "func:ABS", "func:ROUND",
        "func:FLOOR", "func:CEIL", "func:SAFE_DIVIDE", "func:LOWER", "func:UPPER", "func:TRIM", "func:LENGTH",
        "func:CONCAT", "func:SUBSTRING", "func:STARTS_WITH", "func:ENDS_WITH", "func:REGEXP_LIKE",
        # dates
        "func:DATE_SUB", "func:DATE_ADD", "func:DATE_TRUNC", "func:TIMESTAMP_TRUNC", "func:DATEDIFF", "func:EXTRACT", "func:TIME_TO_STR",
        "func:LAST_DAY", "func:GENERATE_DATE_ARRAY", "func:DATE", "func:STR_TO_DATE", "func:TS_OR_DS_TO_DATE",
        "func:CURRENT_DATE",
        "part:DAY", "part:MONTH", "part:YEAR", "part:QUARTER", "part:HOUR", "part:MINUTE", "part:SECOND", "part:WEEK",
        # arrays and structs
        "func:ARRAY", "func:UNNEST", "func:STRUCT", "func:ARRAY_SIZE", "clause:unnest", "clause:struct", "clause:array",
        # clauses and operators whose semantics differ between engines and are rewritten (LIKE escapes,
        # division by zero, array subscripts)
        "clause:qualify", "op:LIKE", "op:/", "op:[]",
        # FORMAT_DATE / PARSE_DATE specifiers proven to render alike (%e is space-padded only in BigQuery;
        # a time specifier in FORMAT_DATE is printed verbatim by BigQuery, as 00 by DuckDB)
        "fmt:%Y", "fmt:%m", "fmt:%d", "fmt:%b", "fmt:%B", "fmt:%A", "fmt:%a", "fmt:%j", "fmt:%U", "fmt:%W", "fmt:%V",
        "fmt:%u", "fmt:%w", "fmt:%y", "fmt:%%",
        # types as sqlglot names them after parsing BigQuery (INT64 → BIGINT, STRING → TEXT, FLOAT64 → DOUBLE, BOOL → BOOLEAN, NUMERIC → DECIMAL)
        "type:BIGINT", "type:DOUBLE", "type:DECIMAL", "type:TEXT", "type:BOOLEAN", "type:DATE", "type:TIMESTAMPTZ",
    }
)
VERIFIED: dict[str, frozenset[str]] = {"bigquery": CORE, "postgres": CORE, "sqlite": CORE}

_OPERATORS = tuple(
    getattr(exp, name)
    for name in ("Connector", "Add", "Sub", "Mul", "Div", "IntDiv", "Mod", "Pow", "EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Is", "NullSafeEQ", "NullSafeNEQ", "BitwiseAnd", "BitwiseOr", "BitwiseXor", "DPipe", "Not", "Neg")
    if hasattr(exp, name)
)
_PART_HOLDERS = (exp.DateTrunc, exp.TimestampTrunc, exp.DateDiff, exp.Extract, exp.TsOrDsDiff, exp.DateAdd, exp.DateSub, exp.LastDay)
_FORMAT_HOLDERS = (exp.TimeToStr, exp.StrToDate, exp.StrToTime)
_FORMAT_SPEC = re.compile(r"%-?[A-Za-z%]")


@dataclass
class Translation:
    sql: str
    constructs: set[str] = field(default_factory=set)
    tables: dict[str, str] = field(default_factory=dict)  # source name -> fixture table used


def constructs_of(tree: exp.Expression) -> set[str]:
    """Every construct the statement uses, keyed as in :data:`CORE`."""
    found: set[str] = set()
    for node in tree.walk():
        if isinstance(node, exp.Anonymous):
            if isinstance(node.parent, exp.Table) and node.parent.this is node:
                continue  # a table function call: the table map decides whether it is known
            found.add("func:" + str(node.name).upper())
        elif isinstance(node, exp.Func) and not isinstance(node, _OPERATORS):
            found.add("func:" + node.sql_name().upper())  # operators (+, =, AND) are not constructs
        if isinstance(node, exp.Cast) and isinstance(node.to, exp.DataType):
            found.add("type:" + (node.to.this.value if isinstance(node.to.this, exp.DataType.Type) else str(node.to.this)).upper())
        if isinstance(node, _PART_HOLDERS):
            unit = node.this if isinstance(node, exp.Extract) else node.args.get("unit")
            if unit is not None:
                # WEEK(SUNDAY) is sqlglot's spelling of BigQuery's Sunday-based WEEK
                found.add("part:" + (unit.name if not isinstance(unit, exp.WeekStart) else "WEEK").upper().split("(")[0])
        if isinstance(node, exp.TimestampTrunc) and node.args.get("zone") is not None:
            found.add("clause:trunc_zone")  # TIMESTAMP_TRUNC(ts, DAY, 'zone')
        if isinstance(node, _FORMAT_HOLDERS):
            fmt = node.args.get("format")
            if isinstance(fmt, exp.Literal):
                for spec in _FORMAT_SPEC.findall(fmt.this):
                    found.add("fmt:" + spec)
            else:
                found.add("fmt:?")  # a format that is not a literal cannot be checked
        if isinstance(node, (exp.Like, exp.ILike)):
            found.add("op:LIKE")
        if isinstance(node, exp.Div):
            found.add("op:/")
        if isinstance(node, exp.Bracket):
            found.add("op:[]")
        if isinstance(node, exp.TableSample):
            found.add("clause:tablesample")  # a random sample: never equal twice
        if isinstance(node, exp.Qualify):
            found.add("clause:qualify")
        if isinstance(node, exp.Unnest):
            found.add("clause:unnest")
        if isinstance(node, exp.Struct):
            found.add("clause:struct")
        if isinstance(node, exp.Array):
            found.add("clause:array")
        if isinstance(node, exp.Window):
            found.add("clause:window")
    return found


def column_types_of(catalog: Any) -> dict[str, str]:
    """Lower-cased column name → declared type, over every table of ``catalog``; a name declared
    with different types in different tables is left out (the rewrites then assume the common
    case). Views carry no types."""
    seen: dict[str, str | None] = {}
    cached = getattr(catalog, "_column_types_cache", None)
    if cached is not None:
        return dict(cached)
    tables = []
    for key in list(catalog._ensure_index()):
        table = catalog._get(key)
        if table is not None:
            tables.append(table)
    for table in tables:
        for column in getattr(table, "columns", ()) or ():
            kind = (getattr(column, "type", "") or "").upper().split("(")[0].strip()
            if not kind:
                continue
            name = column.name.lower()
            if name in seen and seen[name] != kind:
                seen[name] = None
            elif name not in seen:
                seen[name] = kind
    out = {k: v for k, v in seen.items() if v}
    try:
        catalog._column_types_cache = out
    except Exception:
        pass
    return out


def _table_key(table: exp.Table) -> str:
    parts = [p for p in (table.catalog, table.db, table.name) if p]
    return ".".join(parts).lower()


def map_tables(tree: exp.Expression, tables: Mapping[str, str] | None) -> dict[str, str]:
    """Rewrite table references through the host's map (```proj.ds.t``` → ``ds.t``); a three-part
    name with no mapping is untranslatable. Returns what was mapped."""
    mapping = {str(k).strip("`").lower(): str(v) for k, v in (tables or {}).items()}
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    done: dict[str, str] = {}
    for table in list(tree.find_all(exp.Table)):
        call = table.this
        if isinstance(call, exp.Anonymous):
            # FROM `proj.ds.f`(args): the function maps like a table; its body lives in the
            # fixtures as a table macro of the mapped name
            key = str(call.name).strip("`").lower()
            target = mapping.get(key)
            if target is None and key.count(".") >= 2:
                raise Untranslatable(f"table function {key} has no fixture table declared under tables:")
            if target is not None:
                call.set("this", exp.to_identifier(target, quoted=False))
                done[key] = target
            continue
        key = _table_key(table)
        if not key or key in ctes:
            continue
        target = mapping.get(key)
        if target is None and table.catalog:
            raise Untranslatable(f"table {key} has no fixture table declared under tables:")
        if target is None:
            continue
        schema, _, name = target.rpartition(".")
        table.set("catalog", None)
        table.set("db", exp.to_identifier(schema) if schema else None)
        table.set("this", exp.to_identifier(name))
        done[key] = target
    return done


def inline_functions(tree: exp.Expression, functions: Mapping[str, Mapping[str, Any]], dialect: str) -> exp.Expression:
    """Replace calls of the host's scalar SQL functions by their bodies with the arguments bound
    (``functions``: qualified name -> {parameters: [[name, type], ...], body: expr, language}).
    A JavaScript function cannot run on the fixtures: :class:`Untranslatable`."""
    if not functions:
        return tree
    by_name = {str(k).strip("`").lower(): v for k, v in functions.items()}

    def lookup(name: str) -> Mapping[str, Any] | None:
        key = name.strip("`").lower()
        if key in by_name:
            return by_name[key]
        parts = key.split(".")
        if len(parts) >= 3 and ".".join(parts[-2:]) in by_name:
            return by_name[".".join(parts[-2:])]
        return None

    from .analysis import _substitute_variables

    for node in list(tree.find_all(exp.Anonymous)):
        if isinstance(node.parent, exp.Table) and node.parent.this is node:
            continue
        spec = lookup(str(node.name))
        if spec is None:
            continue
        if spec.get("language", "sql") != "sql" or not spec.get("body"):
            raise Untranslatable(f"{spec.get('language', 'javascript')} function {node.name}")
        params = [p[0] for p in spec.get("parameters") or []]
        if len(params) != len(node.expressions):
            raise Untranslatable(f"function {node.name} called with {len(node.expressions)} argument(s), declared with {len(params)}")
        body = sqlglot.parse_one(f"SELECT {spec['body']}", read=dialect).expressions[0]
        node.replace(exp.Paren(this=_substitute_variables(body, dict(zip(params, node.expressions)))))
    return tree


def translate(sql: str | exp.Expression, dialect: str, tables: Mapping[str, str] | None = None, target: str = "duckdb", functions: Mapping[str, Mapping[str, Any]] | None = None, column_types: Mapping[str, str] | None = None) -> Translation:
    """``sql`` (text, or a parsed tree) in ``dialect`` as DuckDB SQL, or :class:`Untranslatable`.
    ``column_types`` (lower-cased column name → type as the schema spells it) lets the rewrites tell
    a DATE column from a TIMESTAMP one and a NUMERIC column from a FLOAT64 one; without it they
    assume BigQuery's common case (DATE_ADD over a DATE, ``/`` over FLOAT64)."""
    if dialect == target:
        return Translation(sql if isinstance(sql, str) else sql.sql(dialect=target))
    tree = sqlglot.parse_one(sql, read=dialect) if isinstance(sql, str) else sql.copy()
    tree = inline_functions(tree, functions or {}, dialect)
    used = constructs_of(tree)
    allowed = VERIFIED.get(dialect, CORE)
    unverified = sorted(c for c in used if c not in allowed)
    if unverified:
        raise Untranslatable(", ".join(unverified))
    from .safety import nondeterministic

    random = nondeterministic(tree)
    if random:
        raise Untranslatable("nondeterministic: " + ", ".join(random))  # never equal twice, on any engine
    mapped = map_tables(tree, tables)
    tree = _rewrite(tree, dialect, column_types or {})
    return Translation(tree.sql(dialect=target), used, mapped)


def _rewrite(tree: exp.Expression, dialect: str, column_types: Mapping[str, str]) -> exp.Expression:
    """Rewrites where sqlglot's translation is not the source engine's semantics. Each one is a
    case in the fidelity tests (``tests/test_bigquery_translation.py``,
    ``tests/test_bigquery_boundary.py``), proven against the real service."""
    if dialect == "bigquery":
        types = {str(k).lower(): str(v).upper() for k, v in column_types.items()}
        _bq_date_minus_date(tree, types)
        _bq_group_by_aliases(tree)
        _bq_float_literals(tree)
        _bq_numeric(tree, types)
        _bq_round_digits(tree, types)
        _bq_truncation(tree)
        _bq_week_number(tree)
        _bq_last_day_of_week(tree)
        _bq_date_arithmetic(tree, types)
        _bq_unnest_offsets(tree)
        _bq_like_escapes(tree)
        _bq_substr_position(tree)
        _bq_string_casts(tree, types)
        _bq_integer_casts(tree, types)  # after the string casts: the TEXT casts it writes are plain
        _bq_division(tree)
        _bq_array_subscripts(tree)
        # COUNTIF over no rows, or over rows where the condition is never TRUE, is 0 in BigQuery;
        # DuckDB's COUNT_IF gives NULL for an empty or all-NULL input.
        for node in list(tree.find_all(exp.CountIf)):
            if isinstance(node.parent, exp.Coalesce):
                continue
            node.replace(exp.Coalesce(this=node.copy(), expressions=[exp.Literal.number(0)]))
    return tree


# ------------------------------------------------------------------------- BigQuery rewrites
#
# DuckDB's TIME_BUCKET keeps the type of its argument (DATE stays DATE, TIMESTAMP stays TIMESTAMP)
# where DATE_TRUNC and DATE ± INTERVAL widen a DATE to TIMESTAMP, and it takes an origin, which
# is how a week may start on any day. BigQuery's weeks start on Sunday unless WEEK(<DAY>) says
# otherwise; 2000-01-02 is a Sunday.
_WEEK_ORIGIN = {"SUNDAY": "2000-01-02", "MONDAY": "2000-01-03", "TUESDAY": "2000-01-04", "WEDNESDAY": "2000-01-05",
                "THURSDAY": "2000-01-06", "FRIDAY": "2000-01-07", "SATURDAY": "2000-01-08"}
_BUCKETS = {"SECOND": (1, "SECOND"), "MINUTE": (1, "MINUTE"), "HOUR": (1, "HOUR"), "DAY": (1, "DAY"), "WEEK": (7, "DAY"),
            "MONTH": (1, "MONTH"), "QUARTER": (3, "MONTH"), "YEAR": (1, "YEAR")}
_DATE_TYPES = {"DATE"}
_TIMESTAMP_TYPES = {"TIMESTAMP", "TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE", "DATETIME", "TIMESTAMP_TZ"}
_DECIMAL_TYPES = {"NUMERIC", "DECIMAL", "BIGNUMERIC", "BIGDECIMAL"}
_DOUBLE_TYPES = {"FLOAT64", "DOUBLE", "FLOAT", "REAL", "DOUBLE PRECISION"}
_TEXT_LIKE = (exp.Concat, exp.DPipe, exp.Lower, exp.Upper, exp.Trim, exp.Substring, exp.TimeToStr, exp.Replace, exp.Repeat,
              exp.Left, exp.Right, exp.Pad, exp.RegexpReplace, exp.RegexpExtract, exp.Initcap, exp.Reverse, exp.GroupConcat,
              exp.ArrayToString)
_BOOL_LIKE = (exp.Boolean, exp.Is, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.And, exp.Or, exp.Not, exp.In, exp.Exists,
              exp.Like, exp.Escape, exp.Between, exp.RegexpLike, exp.StartsWith, exp.EndsWith)


def _unit_of(node: exp.Expression) -> tuple[str | None, str | None]:
    """``(part, week_start)`` of a date part argument: ``WEEK`` → ('WEEK', None), ``WEEK(MONDAY)`` →
    ('WEEK', 'MONDAY')."""
    unit = node.this if isinstance(node, exp.Extract) else node.args.get("unit")
    if unit is None:
        return None, None
    if isinstance(unit, exp.WeekStart):
        return "WEEK", unit.this.name.upper()
    name = unit.name.upper() if isinstance(unit, (exp.Var, exp.Literal, exp.Identifier, exp.Column)) else str(unit).upper()
    if name.startswith("WEEK(") and name.endswith(")"):
        return "WEEK", name[5:-1]
    return name, None


def _interval(n: int, unit: str) -> exp.Interval:
    return exp.Interval(this=exp.Literal.number(n), unit=exp.Var(this=unit))


def _date_literal(text: str) -> exp.Cast:
    return exp.Cast(this=exp.Literal.string(text), to=exp.DataType.build("DATE"))


def _bucket(value: exp.Expression, part: str, week_start: str | None) -> exp.Expression:
    """``TIME_BUCKET(INTERVAL n unit, value[, origin])``: the truncation BigQuery means, in the type
    the value has."""
    n, unit = _BUCKETS[part]
    args = [_interval(n, unit), value]
    if part == "WEEK":
        args.append(_date_literal(_WEEK_ORIGIN[week_start or "SUNDAY"]))
    return exp.Anonymous(this="TIME_BUCKET", expressions=args)


def _type_of(expr: exp.Expression, types: Mapping[str, str]) -> str | None:
    """A coarse static type: 'DATE', 'TIMESTAMP', 'DECIMAL', 'DOUBLE', 'INT', 'TEXT', or None."""
    while isinstance(expr, (exp.Paren, exp.Alias)):
        expr = expr.this
    if isinstance(expr, exp.Cast):
        kind = expr.to.this
        name = kind.value if isinstance(kind, exp.DataType.Type) else str(kind)
        name = name.upper()
        if name in _DATE_TYPES:
            return "DATE"
        if name in _TIMESTAMP_TYPES:
            return "TIMESTAMP"
        if name in _DECIMAL_TYPES:
            return "DECIMAL"
        if name in _DOUBLE_TYPES:
            return "DOUBLE"
        if name in {"BIGINT", "INT", "INT64", "INTEGER", "SMALLINT", "TINYINT"}:
            return "INT"
        if name in {"TEXT", "STRING", "VARCHAR"}:
            return "TEXT"
        if name in {"BOOLEAN", "BOOL"}:
            return "BOOL"
        return None
    if isinstance(expr, exp.Literal):
        if expr.is_string:
            return "TEXT"
        return "DOUBLE" if ("." in expr.this or "e" in expr.this.lower()) else "INT"
    if isinstance(expr, exp.Column):
        declared = types.get(expr.name.lower())
        if declared is None:
            return None
        base = declared.split("(")[0].strip().upper()
        if base in _DATE_TYPES:
            return "DATE"
        if base in _TIMESTAMP_TYPES:
            return "TIMESTAMP"
        if base in _DECIMAL_TYPES:
            return "DECIMAL"
        if base in _DOUBLE_TYPES:
            return "DOUBLE"
        if base in {"INT64", "INT", "INTEGER", "BIGINT", "SMALLINT"}:
            return "INT"
        if base in {"STRING", "TEXT", "VARCHAR"}:
            return "TEXT"
        if base in {"BOOL", "BOOLEAN"}:
            return "BOOL"
        return None
    if isinstance(expr, (exp.Date, exp.CurrentDate, exp.DateAdd, exp.DateSub, exp.StrToDate, exp.TsOrDsToDate, exp.LastDay, exp.DateFromParts)):
        return "DATE"
    if isinstance(expr, (exp.CurrentTimestamp, exp.TimestampTrunc, exp.TimestampAdd, exp.TimestampSub, exp.Timestamp)):
        return "TIMESTAMP"
    if isinstance(expr, exp.DateTrunc):
        return _type_of(expr.this, types)
    if isinstance(expr, exp.Anonymous) and str(expr.name).upper() == "TIME_BUCKET" and len(expr.expressions) >= 2:
        return _type_of(expr.expressions[1], types)
    if isinstance(expr, (exp.Div, exp.SafeDivide, exp.Sqrt, exp.Pow, exp.Ln, exp.Log, exp.Exp)):
        return "DOUBLE"
    if isinstance(expr, exp.Avg):
        inner = _type_of(expr.this, types)
        return "DECIMAL" if inner == "DECIMAL" else "DOUBLE"
    if isinstance(expr, (exp.Sum, exp.Min, exp.Max, exp.Abs, exp.Round, exp.Floor, exp.Ceil, exp.Neg)):
        return _type_of(expr.this, types)
    if isinstance(expr, exp.Coalesce):
        return _type_of(expr.this, types)
    if isinstance(expr, exp.If):
        return _type_of(expr.args.get("true"), types) if expr.args.get("true") is not None else None
    if isinstance(expr, exp.Case):
        for when in expr.args.get("ifs") or []:
            kind = _type_of(when.args.get("true"), types)
            if kind is not None:
                return kind
        return _type_of(expr.args.get("default"), types) if expr.args.get("default") is not None else None
    if isinstance(expr, (exp.Add, exp.Sub, exp.Mul)):
        left, right = _type_of(expr.this, types), _type_of(expr.expression, types)
        if "DOUBLE" in (left, right):
            return "DOUBLE"
        if "DECIMAL" in (left, right):
            return "DECIMAL"
        if left == right:
            return left
        return None
    if isinstance(expr, (exp.Count, exp.CountIf, exp.RowNumber, exp.Rank, exp.DenseRank, exp.Length, exp.Extract, exp.DateDiff)):
        return "INT"
    if isinstance(expr, _TEXT_LIKE):
        return "TEXT"
    if isinstance(expr, _BOOL_LIKE):
        return "BOOL"
    return None


def _bq_float_literals(tree: exp.Expression) -> None:
    """``2.5`` is FLOAT64 in BigQuery; DuckDB reads it as DECIMAL(2,1), so ``ROUND(2.355, 2)`` and
    ``0.1 + 0.2`` come out exact where BigQuery's are binary floating point."""
    for node in list(tree.find_all(exp.Literal)):
        if node.is_string or not ("." in node.this or "e" in node.this.lower()):
            continue
        parent = node.parent
        if isinstance(parent, (exp.DataType, exp.Interval, exp.Limit, exp.Offset)) or (isinstance(parent, exp.Cast) and _type_of(parent, {}) in {"DECIMAL", "DOUBLE"}):
            continue
        node.replace(exp.Cast(this=node.copy(), to=exp.DataType.build("DOUBLE")))


def _bq_numeric(tree: exp.Expression, types: Mapping[str, str]) -> None:
    """NUMERIC is DECIMAL(38, 9); sqlglot's bare DECIMAL would be DuckDB's DECIMAL(18, 3). A NUMERIC
    divided by an integer or another NUMERIC stays NUMERIC (scale 9) in BigQuery; DuckDB divides
    to DOUBLE, so the quotient is cast back. AVG over NUMERIC likewise."""
    for node in list(tree.find_all(exp.DataType)):
        if node.this == exp.DataType.Type.DECIMAL and not node.expressions:
            node.set("expressions", [exp.DataTypeParam(this=exp.Literal.number(38)), exp.DataTypeParam(this=exp.Literal.number(9))])
    for node in list(tree.find_all(exp.Div)):
        left, right = _type_of(node.this, types), _type_of(node.expression, types)
        if "DECIMAL" in (left, right) and "DOUBLE" not in (left, right):
            node.replace(exp.Cast(this=node.copy(), to=exp.DataType.build("DECIMAL(38, 9)")))
    for node in list(tree.find_all(exp.Avg)):
        if _type_of(node.this, types) == "DECIMAL" and not isinstance(node.parent, exp.Cast):
            node.replace(exp.Cast(this=node.copy(), to=exp.DataType.build("DECIMAL(38, 9)")))


def _bq_round_digits(tree: exp.Expression, types: Mapping[str, str]) -> None:
    """ROUND(x, n) over a FLOAT64 rounds the value's exact binary expansion half away from zero in
    BigQuery (ROUND(2.355, 2) is 2.35: the double is 2.35499…; ROUND(1.005, 2) is 1); DuckDB's
    rounds a scaled double and gives 2.36 and 1.01. The expansion is written out to 18 places
    and rounded as a DECIMAL. A NUMERIC or integer argument is already exact."""
    for node in list(tree.find_all(exp.Round)):
        digits = node.args.get("decimals")
        if digits is None:
            continue
        kind = _type_of(node.this, types)
        if kind in {"INT", "DECIMAL"}:
            continue
        value = node.this
        as_double = exp.Cast(this=value.copy(), to=exp.DataType.build("DOUBLE"))
        expansion = exp.Cast(this=exp.Anonymous(this="format", expressions=[exp.Literal.string("{:.18f}"), as_double.copy()]), to=exp.DataType.build("DECIMAL(38, 18)"))
        exact = exp.Cast(this=exp.Round(this=expansion, decimals=digits.copy()), to=exp.DataType.build("DOUBLE"))
        small = exp.LT(this=exp.Abs(this=as_double.copy()), expression=exp.Literal.number("1e18"))
        guarded = exp.Case(ifs=[exp.If(this=small, true=exact)], default=exp.Round(this=as_double.copy(), decimals=digits.copy()))
        if kind == "DOUBLE":
            node.replace(exp.Paren(this=guarded))
            continue
        is_float = exp.In(this=exp.Anonymous(this="typeof", expressions=[value.copy()]), expressions=[exp.Literal.string("DOUBLE"), exp.Literal.string("FLOAT")])
        node.replace(exp.Paren(this=exp.Case(ifs=[exp.If(this=is_float, true=guarded)], default=node.copy())))


def _bq_group_by_aliases(tree: exp.Expression) -> None:
    """``SELECT x AS g … GROUP BY g`` groups by the projection's alias in BigQuery even when a
    source column is also named g; DuckDB binds the column. The projected expression is put in."""
    for select in tree.find_all(exp.Select):
        group = select.args.get("group")
        if group is None:
            continue
        aliases = {p.alias.lower(): p.this for p in select.expressions if isinstance(p, exp.Alias) and p.alias}
        for item in list(group.expressions):
            if isinstance(item, exp.Column) and not item.table and item.name.lower() in aliases:
                projected = aliases[item.name.lower()]
                if not any(isinstance(n, exp.AggFunc) for n in projected.walk()):
                    item.replace(projected.copy())


def _bq_date_minus_date(tree: exp.Expression, types: Mapping[str, str]) -> None:
    """``DATE - DATE`` is an INTERVAL in BigQuery and an integer in DuckDB: not verified."""
    for node in tree.find_all(exp.Sub):
        if _type_of(node.this, types) == "DATE" and _type_of(node.expression, types) == "DATE":
            raise Untranslatable("op:DATE - DATE (an INTERVAL in BigQuery; use DATE_DIFF)")


def _bq_truncation(tree: exp.Expression) -> None:
    """DATE_TRUNC / TIMESTAMP_TRUNC keep the argument's type and start weeks on Sunday, or on the
    day WEEK(<DAY>) names."""
    for node in list(tree.find_all((exp.DateTrunc, exp.TimestampTrunc))):
        part, start = _unit_of(node)
        if part in _BUCKETS and node.args.get("zone") is None:
            node.replace(_bucket(node.this, part, start))


def _bq_week_number(tree: exp.Expression) -> None:
    """EXTRACT(WEEK FROM d) is the number of week starts (Sundays, or the WEEK(<DAY>) day) in the
    year up to d: dates before the first are week 0, so Jan 1 on a Sunday is week 1."""
    for node in list(tree.find_all(exp.Extract)):
        part, start = _unit_of(node)
        if part != "WEEK":
            continue
        value = node.expression
        year_start = _bucket(value.copy(), "YEAR", None)
        day_before = exp.Sub(this=year_start, expression=_interval(1, "DAY"))
        diff = exp.DateDiff(this=_bucket(value.copy(), "WEEK", start), expression=_bucket(day_before, "WEEK", start), unit=exp.Var(this="DAY"))
        node.replace(exp.Paren(this=exp.IntDiv(this=diff, expression=exp.Literal.number(7))))


def _bq_last_day_of_week(tree: exp.Expression) -> None:
    """LAST_DAY(d, WEEK[(<DAY>)]) is the day before the next week start; sqlglot's version counts
    from DuckDB's 0-based DAYOFWEEK and drops the week start."""
    for node in list(tree.find_all(exp.LastDay)):
        part, start = _unit_of(node)
        if part == "WEEK":
            node.replace(exp.Cast(this=exp.Add(this=_bucket(node.this, "WEEK", start), expression=_interval(6, "DAY")), to=exp.DataType.build("DATE")))


def _bq_date_arithmetic(tree: exp.Expression, types: Mapping[str, str]) -> None:
    """DATE_ADD / DATE_SUB over a DATE give a DATE; DuckDB's ``date + INTERVAL`` is a TIMESTAMP. The
    result is cast back unless the argument is known to be a timestamp (a TIMESTAMP column or
    cast), in which case BigQuery's result is a timestamp too."""
    for node in list(tree.find_all((exp.DateAdd, exp.DateSub))):
        if (isinstance(node.parent, exp.Cast) and _type_of(node.parent, {}) == "DATE") or _type_of(node.this, types) == "TIMESTAMP":
            continue
        node.replace(exp.Cast(this=node.copy(), to=exp.DataType.build("DATE")))


def _bq_unnest_offsets(tree: exp.Expression) -> None:
    """``UNNEST(a) AS e WITH OFFSET AS off`` numbers from 0; DuckDB's WITH ORDINALITY from 1. Every
    reference to the offset in its own scope becomes ``(off - 1)``, aliased back where it is
    projected."""
    for unnest in list(tree.find_all(exp.Unnest)):
        offset = unnest.args.get("offset")
        if offset is None:
            continue
        name = (offset.name if isinstance(offset, exp.Expression) else "offset").lower()
        scope = unnest.find_ancestor(exp.Select)
        if scope is None:
            continue
        for column in list(scope.find_all(exp.Column)):
            if column.name.lower() != name or column.table:
                continue
            inner = column.find_ancestor(exp.Select)
            if inner is not scope and _defines_name(inner, name):
                continue  # a subquery's own column of that name
            shifted = exp.Paren(this=exp.Sub(this=column.copy(), expression=exp.Literal.number(1)))
            if column.parent is scope and column.arg_key == "expressions":
                column.replace(exp.alias_(shifted, name))
            else:
                column.replace(shifted)


def _defines_name(select: exp.Expression | None, name: str) -> bool:
    if select is None:
        return False
    for unnest in select.find_all(exp.Unnest):
        offset = unnest.args.get("offset")
        if offset is not None and (offset.name if isinstance(offset, exp.Expression) else "offset").lower() == name:
            return True
    for alias in select.find_all(exp.TableAlias):
        if any(c.name.lower() == name for c in alias.columns):
            return True
    return False


def _bq_like_escapes(tree: exp.Expression) -> None:
    """A backslash escapes ``_``, ``%`` and itself in a BigQuery LIKE pattern; DuckDB needs ESCAPE."""
    for node in list(tree.find_all(exp.Like)):
        if not isinstance(node.parent, exp.Escape):
            node.replace(exp.Escape(this=node.copy(), expression=exp.Literal.string("\\")))


def _bq_substr_position(tree: exp.Expression) -> None:
    """SUBSTR(s, 0, n) reads position 0 as 1 in BigQuery; DuckDB counts the empty position."""
    for node in list(tree.find_all(exp.Substring)):
        start = node.args.get("start")
        if start is None:
            continue
        if isinstance(start, exp.Literal) and not start.is_string:
            if start.this == "0":
                start.replace(exp.Literal.number(1))
            continue
        if isinstance(start, exp.Neg):
            continue
        start.replace(exp.If(this=exp.EQ(this=start.copy(), expression=exp.Literal.number(0)), true=exp.Literal.number(1), false=start.copy()))


_INTEGER_TEXT = r"^\s*[-+]?[0-9]+\s*$"


def _bq_integer_casts(tree: exp.Expression, types: Mapping[str, str]) -> None:
    """CAST(x AS INT64) rounds half away from zero in BigQuery (2.5 → 3); DuckDB rounds a DOUBLE
    half to even (2). SAFE_CAST('1.5' AS INT64) is NULL in BigQuery (a string must be an integer
    literal) and CAST('1.5' AS INT64) an error; DuckDB rounds to 2. Where the operand's type is
    known the right form is emitted; otherwise it is decided per row through its text."""
    for node in list(tree.find_all((exp.TryCast, exp.Cast))):
        if _type_of(node, {}) != "INT":
            continue
        kind = _type_of(node.this, types)
        if kind in {"INT", "DECIMAL", "DATE", "TIMESTAMP"}:
            continue
        value = node.this
        if kind == "DOUBLE":
            node.set("this", exp.Round(this=value))
            continue
        if kind == "BOOL":
            node.set("this", exp.If(this=value, true=exp.Literal.number(1), false=exp.Literal.number(0)))
            continue
        safe = isinstance(node, exp.TryCast)
        text = exp.Cast(this=value.copy(), to=exp.DataType.build("TEXT"))
        is_text = exp.EQ(this=exp.Anonymous(this="typeof", expressions=[value.copy()]), expression=exp.Literal.string("VARCHAR"))
        is_float = exp.In(this=exp.Anonymous(this="typeof", expressions=[value.copy()]), expressions=[exp.Literal.string("DOUBLE"), exp.Literal.string("FLOAT")])
        not_integer = exp.Not(this=exp.Anonymous(this="regexp_matches", expressions=[text.copy(), exp.Literal.string(_INTEGER_TEXT)]))
        rounded = exp.Cast(this=exp.Cast(this=exp.Round(this=exp.TryCast(this=text.copy(), to=exp.DataType.build("DOUBLE"))), to=exp.DataType.build("BIGINT")), to=exp.DataType.build("TEXT"))
        fallback = exp.Null() if safe else exp.Anonymous(this="error", expressions=[exp.Literal.string("invalid integer string")])
        is_bool = exp.EQ(this=exp.Anonymous(this="typeof", expressions=[value.copy()]), expression=exp.Literal.string("BOOLEAN"))
        as_bit = exp.If(this=exp.TryCast(this=text.copy(), to=exp.DataType.build("BOOLEAN")), true=exp.Literal.string("1"), false=exp.Literal.string("0"))
        decided = exp.Case(ifs=[exp.If(this=is_float, true=rounded), exp.If(this=is_bool, true=as_bit), exp.If(this=exp.And(this=is_text, expression=not_integer), true=fallback)], default=text)
        node.set("this", decided)


def _bq_division(tree: exp.Expression) -> None:
    """``x / 0`` (and DIV, MOD by zero) raise in BigQuery; DuckDB gives inf or NULL. The divisor is
    guarded so the statement fails on the fixtures as it would natively."""
    for node in list(tree.find_all((exp.Div, exp.IntDiv, exp.Mod))):
        divisor = node.expression
        if isinstance(divisor, exp.Literal) and not divisor.is_string and float(divisor.this) != 0:
            continue
        if isinstance(divisor, exp.Cast) and isinstance(divisor.this, exp.Literal) and not divisor.this.is_string and float(divisor.this.this) != 0:
            continue
        guarded = exp.Case(ifs=[exp.If(this=exp.EQ(this=divisor.copy(), expression=exp.Literal.number(0)), true=exp.Anonymous(this="error", expressions=[exp.Literal.string("division by zero")]))], default=divisor.copy())
        divisor.replace(guarded)


def _bq_array_subscripts(tree: exp.Expression) -> None:
    """``arr[i]`` and ``arr[OFFSET(i)]`` are 0-based, ``arr[ORDINAL(i)]`` 1-based, and all three raise
    when out of range in BigQuery; DuckDB gives NULL. SAFE_OFFSET / SAFE_ORDINAL are NULL on both.
    The 1-based position is computed here, so sqlglot has no literal left to shift."""
    for node in list(tree.find_all(exp.Bracket)):
        if len(node.expressions) != 1:
            continue
        index = node.expressions[0]
        base = node.args.get("offset") or 0
        position = exp.Paren(this=exp.Add(this=index.copy(), expression=exp.Literal.number(1))) if base == 0 else index.copy()
        if node.args.get("safe"):
            guarded = position
        else:
            out_of_range = exp.Or(this=exp.LT(this=position.copy(), expression=exp.Literal.number(1)), expression=exp.GT(this=position.copy(), expression=exp.ArraySize(this=node.this.copy())))
            guarded = exp.Case(ifs=[exp.If(this=out_of_range, true=exp.Anonymous(this="error", expressions=[exp.Literal.string("array index out of bounds")]))], default=position)
        node.set("expressions", [guarded])
        node.set("offset", 1)  # already 1-based: nothing for the generator to shift
        node.set("safe", None)


def _bq_string_casts(tree: exp.Expression, types: Mapping[str, str]) -> None:
    """CAST(x AS STRING) of a FLOAT64 prints the shortest of %.15g and %.17g that round-trips
    ('3', '0.33333333333333331'); DuckDB prints '3.0' and the shortest repr. A NUMERIC prints
    without trailing zeros ('0.3', not '0.300000000'); an INTERVAL as 'Y-M D H:M:S'. Where the
    operand's type is known the one right form is emitted; otherwise the form is chosen per row
    (every branch binds for any type because it reads the value back through its text)."""
    for node in list(tree.find_all(exp.Cast)):
        if _type_of(node, {}) != "TEXT":
            continue
        kind = _type_of(node.this, types)
        if kind in {"TEXT", "DATE", "TIMESTAMP", "INT", "BOOL"}:
            continue
        value = node.this
        plain = exp.Cast(this=value.copy(), to=exp.DataType.build("TEXT"))
        as_float = exp.TryCast(this=plain.copy(), to=exp.DataType.build("DOUBLE"))

        def fmt(precision: int) -> exp.Expression:
            return exp.Anonymous(this="format", expressions=[exp.Literal.string("{:.%dg}" % precision), as_float.copy()])

        as_double = exp.Case(ifs=[exp.If(this=exp.EQ(this=exp.Cast(this=fmt(15), to=exp.DataType.build("DOUBLE")), expression=as_float.copy()), true=fmt(15))], default=fmt(17))
        if kind == "DOUBLE":
            node.replace(exp.Paren(this=as_double))
            continue
        trimmed = exp.Anonymous(this="rtrim", expressions=[exp.Anonymous(this="rtrim", expressions=[plain.copy(), exp.Literal.string("0")]), exp.Literal.string(".")])
        as_decimal = exp.Case(ifs=[exp.If(this=exp.Like(this=plain.copy(), expression=exp.Literal.string("%.%")), true=trimmed)], default=plain.copy())
        if kind == "DECIMAL":
            node.replace(exp.Paren(this=as_decimal))
            continue
        as_interval_value = exp.TryCast(this=plain.copy(), to=exp.DataType.build("INTERVAL"))
        parts = [exp.Anonymous(this="date_part", expressions=[exp.Literal.string(p), as_interval_value.copy()]) for p in ("year", "month", "day", "hour", "minute", "second")]
        as_interval = exp.Anonymous(this="format", expressions=[exp.Literal.string("{}-{} {} {}:{}:{}")] + parts)

        def typeof() -> exp.Expression:
            return exp.Anonymous(this="typeof", expressions=[value.copy()])

        rewritten = exp.Case(
            ifs=[
                exp.If(this=exp.In(this=typeof(), expressions=[exp.Literal.string("DOUBLE"), exp.Literal.string("FLOAT")]), true=as_double),
                exp.If(this=exp.Like(this=typeof(), expression=exp.Literal.string("DECIMAL%")), true=as_decimal),
                exp.If(this=exp.EQ(this=typeof(), expression=exp.Literal.string("INTERVAL")), true=as_interval),
            ],
            default=plain,
        )
        node.replace(exp.Paren(this=rewritten))
