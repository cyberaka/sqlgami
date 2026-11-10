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
        "func:COUNT", "func:SUM", "func:MIN", "func:MAX", "func:AVG", "func:COUNT_IF", "func:ANY_VALUE", "func:APPROX_DISTINCT",
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
        # clauses and types
        "clause:qualify",
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
_PART_HOLDERS = (exp.DateTrunc, exp.TimestampTrunc, exp.DateDiff, exp.Extract, exp.TsOrDsDiff, exp.DateAdd, exp.DateSub)


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


def translate(sql: str | exp.Expression, dialect: str, tables: Mapping[str, str] | None = None, target: str = "duckdb", functions: Mapping[str, Mapping[str, Any]] | None = None) -> Translation:
    """``sql`` (text, or a parsed tree) in ``dialect`` as DuckDB SQL, or :class:`Untranslatable`."""
    if dialect == target:
        return Translation(sql if isinstance(sql, str) else sql.sql(dialect=target))
    tree = sqlglot.parse_one(sql, read=dialect) if isinstance(sql, str) else sql.copy()
    tree = inline_functions(tree, functions or {}, dialect)
    used = constructs_of(tree)
    allowed = VERIFIED.get(dialect, CORE)
    unverified = sorted(c for c in used if c not in allowed)
    if unverified:
        raise Untranslatable(", ".join(unverified))
    mapped = map_tables(tree, tables)
    tree = _rewrite(tree, dialect)
    return Translation(tree.sql(dialect=target), used, mapped)


def _rewrite(tree: exp.Expression, dialect: str) -> exp.Expression:
    """Rewrites where sqlglot's translation is not the source engine's semantics (each one is a
    case in the fidelity tests, proven natively)."""
    if dialect == "bigquery":
        # COUNTIF over no rows, or over rows where the condition is never TRUE, is 0 in BigQuery;
        # DuckDB's COUNT_IF gives NULL for an empty or all-NULL input.
        for node in list(tree.find_all(exp.CountIf)):
            if isinstance(node.parent, exp.Coalesce):
                continue
            node.replace(exp.Coalesce(this=node.copy(), expressions=[exp.Literal.number(0)]))
    return tree
