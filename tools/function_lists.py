"""Regenerate ``src/sqlgami/_functions.py``: which SQL functions a query may call.

    SQLGAMI_TEST_PG_DSN=... python tools/function_lists.py

sqlgami sends a statement to a database only when every function it calls is on
that engine's list. The lists are built here from each engine's own catalogue of
built-in functions, minus everything that does more than compute a value from
its arguments or from the rows read: files, the network, settings, sequences,
locks, session state, server administration, and catalogue introspection.

Run it with the engine versions the project is tested against, read the diff,
and commit the result. Needs duckdb, psycopg and a Postgres to ask.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

import sqlglot
from sqlglot import exp

OUT = Path(__file__).resolve().parent.parent / "src" / "sqlgami" / "_functions.py"

# ---------------------------------------------------------------- sqlglot's own function nodes

# Typed nodes that stand for something other than a computation.
TYPED_REFUSED = {
    "ReadCSV", "ReadParquet", "NextValueFor", "ToFile", "CurrentVersion", "CurrentSetting",
    "CurrentSession", "CurrentStatement", "CurrentTransaction", "CurrentIpAddress", "CurrentClient",
    "Host", "Seq1", "Seq2", "Seq4", "Seq8", "GetVariable", "Getenv", "Sleep",
}


def typed_classes() -> set[str]:
    found: set[str] = set()
    stack = [exp.Func]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub.__module__.startswith("sqlglot") and sub.__name__ not in found:
                found.add(sub.__name__)
                stack.append(sub)
    return found - {"Anonymous", "AnonymousAggFunc", "CombinedAggFunc", "CombinedParameterizedAgg", "ParameterizedAgg"}


# ---------------------------------------------------------------------------------- SQLite

SQLITE_INNOCUOUS = 0x200000
SQLITE_EXTRA = {
    "bm25", "highlight", "snippet", "matchinfo", "offsets",  # read a full-text index
    # Pure functions that some builds add.
    "soundex", "median", "percentile", "percentile_cont", "percentile_disc", "unistr", "unistr_quote", "regexp",
    "stddev", "variance",
}
SQLITE_DENY = re.compile(
    r"^(sqlite_|load_extension|fts3_tokenizer|fts5|changes|total_changes|last_insert_rowid|"
    r"rtree|geopoly_debug|optimize|match$)"
)
SQLITE_TABLE = {"json_each", "json_tree", "generate_series"}
# Maths functions are a compile-time option; present in most builds.
SQLITE_MATH = (
    "acos acosh asin asinh atan atan2 atanh ceil ceiling cos cosh degrees exp floor ln log log10 log2 "
    "mod pi pow power radians sin sinh sqrt tan tanh trunc"
).split()


def sqlite_lists() -> tuple[set[str], set[str], set[str]]:
    conn = sqlite3.connect(":memory:")
    rows = conn.execute("SELECT name, flags FROM pragma_function_list").fetchall()
    every = {name.lower() for name, _ in rows}
    innocuous = {name.lower() for name, flags in rows if flags & SQLITE_INNOCUOUS}
    allowed = {
        n
        for n in (innocuous | SQLITE_EXTRA | set(SQLITE_MATH))
        if not SQLITE_DENY.search(n) and re.fullmatch(r"[a-z_][a-z0-9_]*", n)
    }
    allowed |= {"cast", "exists"}  # grammar, not functions
    every = {n for n in every if re.fullmatch(r"[a-z_][a-z0-9_]*", n)}
    modules = {name.lower() for (name,) in conn.execute("SELECT name FROM pragma_module_list")}
    refused = (every | modules | {"readfile", "writefile", "edit", "fileio_ls", "fsdir", "lsdir"}) - allowed - SQLITE_TABLE
    refused |= {f"pragma_{name}" for name in ("table_info", "table_xinfo", "index_list", "database_list", "function_list",
                                              "module_list", "compile_options", "collation_list", "foreign_key_list",
                                              "index_info", "index_xinfo", "table_list", "pragma_list")}
    return allowed, set(SQLITE_TABLE), refused


# ---------------------------------------------------------------------------------- DuckDB

DUCKDB_DENY = re.compile(
    r"^(getenv|current_setting|getvariable|nextval|currval|setval|setseed|pg_|sleep_ms|write_log|error$|"
    r"txid_|current_transaction_id|current_query|current_connection_id|get_block_size|version$|stats$|"
    r"vector_type|in_search_path|has_|inet_|duckdb_|pragma_|read_|parquet_|sniff_|which_secret|test_|"
    r"json_execute_serialized_sql|json_serialize_|json_deserialize_|enable_|disable_|checkpoint|force_checkpoint|"
    r"add_parquet_key|parse_duckdb_log|__|query$|query_table$|glob$|summary$|col_description|obj_description|"
    r"shobj_description|format_pg_type|map_to_pg_oid|create_sort_key|allow_|remap_struct|system$|"
    r"register_|deregister_|destroy_|create_external)"
)
DUCKDB_KEEP = {"pg_typeof", "pg_size_pretty"}
# Part of DuckDB's grammar, so absent from duckdb_functions().
DUCKDB_SYNTAX = (
    "grouping_id grouping try unpack coalesce percentile_cont percentile_disc ifnull extract columns cast "
    "try_cast array exists nullif row struct map list position substring trim overlay"
).split()
DUCKDB_TABLE = {"generate_series", "range", "unnest", "json_each", "json_tree", "repeat_row", "generate_subscripts"}


def duckdb_lists() -> tuple[set[str], set[str], set[str]]:
    import duckdb

    conn = duckdb.connect()
    rows = conn.execute("SELECT lower(function_name), function_type FROM duckdb_functions()").fetchall()
    every = {name for name, _ in rows}
    scalar = {name for name, kind in rows if kind in ("scalar", "aggregate", "window", "macro")}
    scalar = {n for n in scalar if re.fullmatch(r"[a-z_][a-z0-9_]*", n)}  # operators such as + and ~~ are not calls
    allowed = {n for n in scalar if n in DUCKDB_KEEP or not DUCKDB_DENY.search(n)} | set(DUCKDB_SYNTAX)
    refused = {n for n in every if re.fullmatch(r"[a-z][a-z0-9_]*", n)} - allowed - DUCKDB_TABLE
    refused |= {
        "read_xlsx", "st_read", "sqlite_scan", "postgres_scan", "postgres_query", "mysql_scan", "mysql_query",
        "iceberg_scan", "delta_scan", "http_get", "http_post", "shell", "system",
    }
    return allowed, set(DUCKDB_TABLE), refused


# -------------------------------------------------------------------------------- Postgres

PG_DENY = re.compile(
    r"^(pg_|has_|txid_|binary_upgrade|lo_|loread|lowrite|current_setting|set_config|inet_client_|inet_server_|"
    r"version$|ts_stat|ts_rewrite|fmgr_|postgresql_fdw_validator|plpgsql_|amvalidate|makeaclitem|mxid_age|"
    r"querytree|satisfies_hash_partition|current_query|currtid|brin_|gin_|gist_|spg_|dblink|"
    r"(query|table|schema|database|cursor)_to_xml)"
)
PG_KEEP = {"pg_typeof", "pg_size_pretty", "pg_size_bytes", "pg_input_is_valid", "pg_column_size"}
PG_VOLATILE_OK = {"random", "random_normal", "gen_random_uuid", "clock_timestamp", "timeofday", "array_sample", "array_shuffle"}
# Written like function calls but part of the grammar, so absent from pg_proc.
PG_SYNTAX = (
    "coalesce nullif greatest least grouping xmlelement xmlforest xmlconcat xmlparse xmlpi xmlroot xmlserialize "
    "xmlexists json_object json_array json_arrayagg json_objectagg json_scalar json_serialize json_query "
    "json_value json_exists normalize overlay position substring trim extract cast treat row array any all some "
    "current_role current_catalog user localtime localtimestamp current_date current_time current_timestamp"
).split()


def postgres_lists(dsn: str) -> tuple[set[str], set[str]]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """
            SELECT p.proname, p.provolatile,
                   EXISTS (SELECT 1 FROM unnest(p.proargtypes::oid[]) a(t)
                           WHERE a.t IN ('internal'::regtype, 'cstring'::regtype)) AS internal_arg,
                   p.prorettype::regtype::text
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'pg_catalog'
            """
        ).fetchall()
    hidden = {"internal", "cstring", "trigger", "event_trigger", "language_handler", "fdw_handler",
              "index_am_handler", "table_am_handler", "tsm_handler"}
    callable_: set[str] = set()
    impure: set[str] = set()
    for name, volatility, internal_arg, returns in rows:
        if internal_arg or returns in hidden:
            continue
        callable_.add(name)
        if volatility not in ("i", "s"):
            impure.add(name)
    allowed = {
        n for n in callable_
        if n in PG_KEEP or n in PG_VOLATILE_OK or (n not in impure and not PG_DENY.search(n))
    }
    allowed |= set(PG_SYNTAX)
    refused = (callable_ - allowed) | {"dblink", "dblink_exec", "dblink_connect", "pg_read_server_files"}
    return allowed, refused


# ------------------------------------------------------------------------------- the check


def typed_hits(names: set[str], dialect: str) -> dict[str, set[str]]:
    """Which refused names sqlglot turns into a typed node (so the name is lost): class -> names."""
    hits: dict[str, set[str]] = {}
    for name in sorted(names):
        for args in ("", "'a'", "'a', 'b'", "'a', 'b', 'c'", "1", "1, 2"):
            for template in ("SELECT {f}({a})", "SELECT * FROM {f}({a})"):
                try:
                    tree = sqlglot.parse_one(template.format(f=name, a=args), dialect=dialect)
                except Exception:
                    continue
                for node in tree.find_all(exp.Func):
                    if not isinstance(node, exp.Anonymous) and not isinstance(node.parent, exp.Func):
                        hits.setdefault(type(node).__name__, set()).add(name)
    return hits


def block(name: str, values: set[str]) -> str:
    words = sorted(values)
    lines, line = [], ""
    for word in words:
        if len(line) + len(word) + 1 > 104:
            lines.append(line)
            line = ""
        line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    body = "\n".join(f'    "{text} "' for text in lines)
    return f"{name} = frozenset(\n    (\n{body}\n    ).split()\n)\n" if lines else f"{name}: frozenset[str] = frozenset()\n"


def main() -> int:
    dsn = os.environ.get("SQLGAMI_TEST_PG_DSN")
    if not dsn:
        print("set SQLGAMI_TEST_PG_DSN", file=sys.stderr)
        return 2
    import duckdb

    sqlite_ok, sqlite_table, sqlite_no = sqlite_lists()
    duck_ok, duck_table, duck_no = duckdb_lists()
    pg_ok, pg_no = postgres_lists(dsn)
    typed = typed_classes()

    tainted: dict[str, set[str]] = {}
    for dialect, refused, allowed in (("sqlite", sqlite_no, sqlite_ok | sqlite_table),
                                      ("duckdb", duck_no, duck_ok | duck_table), ("postgres", pg_no, pg_ok)):
        hits = typed_hits(refused, dialect)
        clean = typed_hits(allowed, dialect)
        for cls, names in sorted(hits.items()):
            shared = clean.get(cls, set())
            print(f"{dialect}: refused {sorted(names)} parse as {cls}" + (f"  (also from allowed {sorted(shared)[:6]})" if shared else ""))
            if cls not in TYPED_REFUSED:
                tainted.setdefault(dialect, set()).add(cls)

    conn = sqlite3.connect(":memory:")
    pg_version = "?"
    try:
        import psycopg

        with psycopg.connect(dsn) as pg:
            pg_version = pg.execute("SHOW server_version").fetchone()[0].split()[0]
    except Exception:
        pass
    header = (
        '"""Which SQL functions a statement may call and still be sent to a database.\n\n'
        "Generated by tools/function_lists.py; do not edit by hand.\n"
        f"Built from SQLite {conn.execute('select sqlite_version()').fetchone()[0]}, DuckDB {duckdb.__version__}, "
        f"Postgres {pg_version} and sqlglot {sqlglot.__version__}.\n"
        '"""\n\nfrom __future__ import annotations\n\n'
    )
    parts = [
        header,
        "# sqlglot function nodes that are plain computations.\n",
        block("SAFE_TYPED", typed - TYPED_REFUSED),
        "\n# sqlglot function nodes that reach outside the tables, on any engine.\n",
        block("REFUSED_TYPED", TYPED_REFUSED),
        "\n# Built-in functions that may be called, by engine.\n",
        block("SQLITE_ALLOWED", sqlite_ok),
        block("SQLITE_TABLE_FUNCTIONS", sqlite_table),
        block("DUCKDB_ALLOWED", duck_ok),
        block("DUCKDB_TABLE_FUNCTIONS", duck_table),
        block("POSTGRES_ALLOWED", pg_ok),
        "\n# Built-in functions that are known and refused, by engine.\n",
        block("SQLITE_REFUSED", sqlite_no),
        block("DUCKDB_REFUSED", duck_no),
        block("POSTGRES_REFUSED", pg_no),
        "\n# sqlglot nodes that, on one engine, stand for a refused function.\n",
        "TAINTED_TYPED: dict[str, frozenset[str]] = {\n"
        + "".join(f"    {d!r}: frozenset({sorted(c)!r}),\n" for d, c in sorted(tainted.items()))
        + "}\n",
    ]
    OUT.write_text("".join(parts), encoding="utf-8")
    print(f"sqlite {len(sqlite_ok)} allowed, {len(sqlite_no)} refused; duckdb {len(duck_ok)} / {len(duck_no)}; "
          f"postgres {len(pg_ok)} / {len(pg_no)}; typed {len(typed - TYPED_REFUSED)}")
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
