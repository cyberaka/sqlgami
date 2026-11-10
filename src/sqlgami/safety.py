"""What a statement may call and still be sent to a database.

A read-only query can still do harm through the functions it calls: read a file,
change a setting, take a lock, advance a sequence, reload the server's
configuration. A list of names to refuse can never be complete, so sqlgami works
the other way round. For SQLite, DuckDB and Postgres it keeps a list of built-in
functions that only compute a value from their arguments and the rows read, and a
statement is sent to the database only if every function it calls is on that list
(or on the host's own ``allowed_functions``).

Three outcomes for each function call:

* allowed: on the engine's list, or one the host has vouched for;
* refused: a built-in known to reach outside the tables;
* unlisted: not known at all, such as a user-defined function. The statement is
  still analysed, but not sent to the database until the host allows the name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from . import _functions as F

# Engines with an allow-list. For any other dialect only the refuse-list below applies,
# and sqlgami has no adapter that would run the statement.
_LISTS: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]] = {
    # dialect: (allowed anywhere, also allowed where a table is expected, refused)
    "sqlite": (F.SQLITE_ALLOWED, F.SQLITE_TABLE_FUNCTIONS, F.SQLITE_REFUSED),
    "duckdb": (F.DUCKDB_ALLOWED, F.DUCKDB_TABLE_FUNCTIONS, F.DUCKDB_REFUSED),
    "postgres": (F.POSTGRES_ALLOWED, frozenset(), F.POSTGRES_REFUSED),
}
# On these engines only a table function may stand where a table is expected.
_TABLE_POSITION_IS_SEPARATE = {"sqlite", "duckdb"}
# Schemas whose functions are the engine's own.
_SYSTEM_SCHEMAS = {"postgres": {"pg_catalog"}, "duckdb": {"main", "system", "pg_catalog"}, "sqlite": {"main"}}

# Refused on every dialect, listed or not.
REFUSED_EVERYWHERE = frozenset(
    {
        "read_csv", "read_csv_auto", "read_parquet", "parquet_scan", "read_json", "read_json_auto",
        "read_json_objects", "read_ndjson", "read_ndjson_auto", "read_text", "read_blob", "sniff_csv",
        "sqlite_scan", "postgres_scan", "postgres_query", "mysql_scan", "mysql_query", "iceberg_scan",
        "delta_scan", "read_xlsx", "st_read", "getenv", "query", "query_table",
        "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export",
        "dblink", "dblink_exec", "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_terminate_backend",
        "pg_cancel_backend", "pg_reload_conf", "set_config", "current_setting", "nextval", "setval",
        "pg_advisory_lock", "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer",
    }
)

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_NOT_NAMES = (TokenType.STRING, TokenType.NUMBER, TokenType.R_PAREN, TokenType.R_BRACKET, TokenType.PARAMETER,
              TokenType.PLACEHOLDER)

# Typed nodes that may stand where a table is expected.
_TABLE_TYPED = frozenset({"Unnest", "GenerateSeries", "ExplodingGenerateSeries", "Explode", "Posexplode", "JSONTable"})

# Calls whose value changes from one run to the next.
_RANDOM_TYPED = ("Rand", "Randn", "Randstr", "Uuid", "Random")
_RANDOM_NAMES = frozenset(
    {
        "random", "randomblob", "random_normal", "gen_random_uuid", "uuid", "uuidv4", "uuidv7", "newid",
        "clock_timestamp", "timeofday", "array_sample", "array_shuffle", "rand", "generate_uuid",
    }
)

_FILE_SUFFIXES = (".csv", ".parquet", ".json", ".ndjson", ".jsonl", ".tsv", ".xlsx", ".db", ".duckdb", ".txt", ".gz")


@dataclass
class FunctionReview:
    refused: list[str] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refused and not self.unlisted


def has_allow_list(dialect: str | None) -> bool:
    return dialect in _LISTS


def _in_table_position(node: exp.Expression) -> bool:
    return isinstance(node.parent, (exp.Table, exp.Lateral))


def _qualifier(node: exp.Expression) -> str | None:
    """The schema a call is written with (``pg_catalog.lower(x)``), or ``None``."""
    parent = node.parent
    if isinstance(parent, exp.Dot) and parent.expression is node:
        left = parent.this
        return left.name.lower() if isinstance(left, (exp.Identifier, exp.Column)) and left.name else "?"
    return None


def review_functions(tree: exp.Expression, dialect: str | None, allowed: Iterable[str] = ()) -> FunctionReview:
    """Sort every function call in ``tree`` into allowed, refused or unlisted."""
    host = {name.lower() for name in allowed}
    lists = _LISTS.get(dialect or "")
    tainted = F.TAINTED_TYPED.get(dialect or "", frozenset())
    refused: set[str] = set()
    unlisted: set[str] = set()

    for node in tree.find_all(exp.Func):
        if isinstance(node, exp.Anonymous):
            name = (node.name or "").lower()
            qualifier = _qualifier(node)
            if name in host or (qualifier and f"{qualifier}.{name}" in host):
                continue
            if name in REFUSED_EVERYWHERE:
                refused.add(name)
                continue
            if lists is None:
                continue
            everywhere, tables_only, known_bad = lists
            if qualifier is not None and qualifier not in _SYSTEM_SCHEMAS.get(dialect or "", set()):
                unlisted.add(f"{qualifier}.{name}")  # somebody's own function, whatever it is called
                continue
            if _in_table_position(node) and dialect in _TABLE_POSITION_IS_SEPARATE:
                good = name in tables_only
            else:
                good = name in everywhere or name in tables_only
            if good:
                continue
            (refused if name in known_bad else unlisted).add(name or "?")
            continue
        cls = type(node).__name__
        if cls in F.REFUSED_TYPED or cls in tainted:
            refused.add(node.sql_name().lower())
        elif lists is not None and cls not in F.SAFE_TYPED:
            unlisted.add(node.sql_name().lower())
        elif dialect in _TABLE_POSITION_IS_SEPARATE and _in_table_position(node) and cls not in _TABLE_TYPED:
            unlisted.add(node.sql_name().lower())  # a scalar function where a table function belongs

    for node in tree.find_all(exp.Table):
        ident = node.this
        alias = node.args.get("alias")
        if (
            lists is not None
            and isinstance(ident, exp.Identifier)
            and isinstance(alias, exp.TableAlias)
            and alias.this is None
            and alias.columns
        ):
            # name('x') in FROM, read by the parser as a table with a column alias and no
            # alias name. The engine would call a function of that name.
            name = ident.name.lower()
            if name not in host and name not in lists[1]:
                (refused if name in lists[2] or name in REFUSED_EVERYWHERE else unlisted).add(name)
        # DuckDB reads a file when a quoted path stands where a table name should.
        if isinstance(ident, exp.Identifier) and ident.args.get("quoted") and not node.db:
            text = ident.name.lower()
            if "/" in text or "\\" in text or text.endswith(_FILE_SUFFIXES) or "://" in text:
                refused.add(f"file {ident.name!r}")
    return FunctionReview(sorted(refused), sorted(unlisted))


def _defined_names(tree: exp.Expression) -> set[str]:
    """Names the statement itself gives a column list to: ``WITH r(n) AS ...``, ``... AS t(a, b)``."""
    names: set[str] = set()
    for alias in tree.find_all(exp.TableAlias):
        if alias.this is not None and alias.name:
            names.add(alias.name.lower())
    for cte in tree.find_all(exp.CTE):
        if cte.alias:
            names.add(cte.alias.lower())
    return names


def review_text(sql: str, tree: exp.Expression, dialect: str | None, allowed: Iterable[str] = ()) -> FunctionReview:
    """A second net under :func:`review_functions`, working from the text instead of the tree.

    No built-in that sqlgami refuses may be written directly before ``(``, wherever
    it stands and whatever the parser made of it. This catches a call the parser
    read as something else, such as ``FROM show('x')`` read as a table named
    ``show`` with a column alias.
    """
    lists = _LISTS.get(dialect or "")
    if lists is None:
        return FunctionReview()
    everywhere, tables_only, known_bad = lists
    host = {name.lower() for name in allowed}
    host |= {name.rsplit(".", 1)[-1] for name in host}
    try:
        tokens = sqlglot.tokenize(sql, read=dialect)
    except SqlglotError:
        return FunctionReview()
    defined = _defined_names(tree)
    refused: set[str] = set()
    for index in range(len(tokens) - 1):
        token = tokens[index]
        if tokens[index + 1].token_type != TokenType.L_PAREN or token.token_type in _NOT_NAMES:
            continue
        if not _NAME.match(token.text):
            continue
        name = token.text.lower()
        if name in host or name in defined or name in everywhere:
            continue
        if name in known_bad or name in REFUSED_EVERYWHERE:
            refused.add(name)
    return FunctionReview(sorted(refused), [])


# Relations an engine keeps about itself: settings, credentials, sessions, file paths. A query
# that reads them is not answering a business question, so they are refused unless the host
# names them in ``allowed_tables``. Matched on the lower-cased schema and name.
_SYSTEM_TABLE_SCHEMAS = {
    "postgres": {"pg_catalog", "information_schema", "pg_toast"},
    "duckdb": {"information_schema", "pg_catalog", "system"},
    "sqlite": set(),
    "bigquery": {"information_schema"},
}
_SYSTEM_TABLE_PREFIXES = {
    "postgres": ("pg_",),
    "duckdb": ("duckdb_", "pragma_", "sqlite_", "pg_"),
    "sqlite": ("sqlite_", "pragma_"),
    "bigquery": (),
}


def review_tables(tree: exp.Expression, dialect: str | None, allowed: Iterable[str] = ()) -> list[str]:
    """Reads of the engine's own catalogues and settings, written as plain table names.

    ``SELECT * FROM pragma_database_list`` (SQLite, no parentheses) or ``FROM pg_shadow``
    (Postgres) are table reads to the parser, so the function gate never sees them. Names the
    statement itself defines (CTEs, aliases) are not tables. ``allowed`` may hold ``name``,
    ``schema.name`` or ``schema.*``.
    """
    schemas = _SYSTEM_TABLE_SCHEMAS.get(dialect or "")
    prefixes = _SYSTEM_TABLE_PREFIXES.get(dialect or "", ())
    if schemas is None and not prefixes:
        return []
    host = {name.lower() for name in allowed}
    # Only a CTE can stand in for a table name. An alias never does: in
    # ``FROM t AS sqlite_master, sqlite_master m`` the second name is the real catalogue.
    ctes = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}
    refused: set[str] = set()
    for node in tree.find_all(exp.Table):
        if not isinstance(node.this, exp.Identifier):
            continue
        name = node.name.lower()
        schema = (node.db or "").lower()
        if not schema and name in ctes:
            continue
        full = f"{schema}.{name}" if schema else name
        if full in host or name in host or (schema and f"{schema}.*" in host):
            continue
        if (schema and schemas and schema in schemas) or name.startswith(prefixes) or name.upper() == "INFORMATION_SCHEMA" or schema.upper() == "INFORMATION_SCHEMA":
            refused.add(f"table {full}")
    return sorted(refused)


def review(
    sql: str,
    tree: exp.Expression,
    dialect: str | None,
    allowed: Iterable[str] = (),
    allowed_tables: Iterable[str] = (),
) -> FunctionReview:
    """Both nets together: the parsed tree, then the text; and the tables the statement reads."""
    first = review_functions(tree, dialect, allowed)
    second = review_text(sql, tree, dialect, allowed)
    refused = sorted(set(first.refused) | set(second.refused) | set(review_tables(tree, dialect, allowed_tables)))
    unlisted = sorted((set(first.unlisted) | set(second.unlisted)) - set(refused))
    # "schema.name" from the tree and bare "name" from the text are one call.
    qualified = {name.rsplit(".", 1)[-1] for name in unlisted if "." in name}
    unlisted = [name for name in unlisted if "." in name or name not in qualified]
    return FunctionReview(refused, unlisted)


def nondeterministic(tree: exp.Expression) -> list[str]:
    """Reasons the statement's result can differ between two runs on the same data."""
    found: set[str] = set()
    for node in tree.find_all(exp.Func):
        if isinstance(node, exp.Anonymous):
            if (node.name or "").lower() in _RANDOM_NAMES:
                found.add(f"{node.name.lower()}()")
        elif type(node).__name__ in _RANDOM_TYPED:
            found.add(f"{node.sql_name().lower()}()")
    if any(True for _ in tree.find_all(exp.TableSample)):
        found.add("a random sample of rows")
    return sorted(found)
