"""Red-team of the safety gate: can anything with a side effect, file/network access, or
settings/credential disclosure get past it and reach the database?

The gate's contract (src/sqlgami/safety.py + analysis.analyse): a statement is sent to an
engine only if it parses as exactly one read-only query (kind == QUERY) whose every function
call is on that engine's allow-list. A refused built-in -> external_access; an unknown name ->
unlisted_function/unknown_function; a write/DDL/multi-statement -> blocked by kind. Any of
those means the statement is NOT sent.

These tests are STATIC: they call analysis.analyse() and read the verdict. Nothing dangerous is
executed. The one runtime check (pragma disclosure) uses a throwaway temp database with no
sensitive content, purely to show the parser-differential is real and not theoretical.

CONTAINED / SAFE are regression guards. The BYPASS cases were confirmed holes when this file
was written (plain table reads the function gate never saw); the table gate in
safety.review_tables closes them, and they are kept here as regression guards.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from sqlgami import analysis as A


def _sent(sql: str, dialect: str) -> bool:
    """True when the gate would send this statement to the engine."""
    a = A.analyse(sql, None, dialect)
    return a.kind == A.QUERY and not a.unsafe_functions and not a.unlisted_functions and not a.script_statements


# ---- statements that must NEVER be sent. (label, sql, dialect) ----
CONTAINED = [
    # parser differentials: a function the parser might read as a table or something else
    ("show_as_table", "SELECT * FROM show('x')", "duckdb"),
    ("pragma_fn_args", "SELECT * FROM pragma_table_info('t')", "sqlite"),
    ("glob_fn", "SELECT * FROM glob('/tmp/*')", "duckdb"),
    ("read_csv_from", "SELECT * FROM read_csv('/tmp/x.csv')", "duckdb"),
    ("replacement_scan_parquet", "SELECT * FROM '/tmp/x.parquet'", "duckdb"),
    ("replacement_scan_csv", "SELECT * FROM '/tmp/x.csv'", "duckdb"),
    # quoting / casing tricks on a refused builtin
    ("read_csv_scalar", "SELECT read_csv('/tmp/x.csv') AS c", "duckdb"),
    ("read_csv_mixedcase", "SELECT ReAd_CsV('/tmp/x.csv') AS c", "duckdb"),
    ("read_csv_quoted", 'SELECT "read_csv"(\'/tmp/x.csv\') AS c', "duckdb"),
    ("pg_sleep_uppercase", "SELECT PG_SLEEP(5)", "postgres"),
    # schema-qualified calls
    ("pg_catalog_read_file", "SELECT pg_catalog.pg_read_file('/tmp/x')", "postgres"),
    ("public_dblink", "SELECT public.dblink('h', 'SELECT 1')", "postgres"),
    ("main_read_csv", "SELECT main.read_csv('/tmp/x.csv')", "duckdb"),
    # table functions in FROM / LATERAL
    ("lateral_read_csv", "SELECT * FROM t, LATERAL read_csv('/tmp/x.csv')", "duckdb"),
    # settings / secrets disclosure via function
    ("current_setting", "SELECT current_setting('data_directory')", "postgres"),
    ("duckdb_settings_fn", "SELECT * FROM duckdb_settings()", "duckdb"),
    ("duckdb_secrets_fn", "SELECT * FROM duckdb_secrets()", "duckdb"),
    ("getenv", "SELECT getenv('HOME')", "duckdb"),
    # side-effect statements (blocked by kind)
    ("copy_program", "COPY t TO PROGRAM 'id'", "postgres"),
    ("copy_from_program", "COPY t FROM PROGRAM 'curl http://x'", "postgres"),
    ("attach", "ATTACH '/tmp/x.duckdb'", "duckdb"),
    ("pragma_write", "PRAGMA writable_schema = 1", "sqlite"),
    ("set_external", "SET enable_external_access = true", "duckdb"),
    ("install", "INSTALL httpfs", "duckdb"),
    ("call", "CALL pragma_version()", "duckdb"),
    ("export", "EXPORT DATABASE '/tmp/e'", "duckdb"),
    # CTE / comment hiding and multiple statements
    ("cte_then_copy", "WITH x AS (SELECT 1) SELECT * FROM x; COPY t TO PROGRAM 'id'", "postgres"),
    ("select_then_copy", "SELECT 1; COPY t TO PROGRAM 'id'", "postgres"),
    ("select_then_pragma", "SELECT 1; PRAGMA writable_schema = 1", "sqlite"),
    ("data_modifying_cte", "WITH d AS (DELETE FROM t RETURNING id) SELECT * FROM d", "postgres"),
]


@pytest.mark.parametrize("label,sql,dialect", CONTAINED, ids=[c[0] for c in CONTAINED])
def test_dangerous_statement_is_not_sent(label, sql, dialect):
    assert not _sent(sql, dialect)


# ---- plain reads that must still be sent (no over-blocking). ----
SAFE = [
    ("plain", "SELECT a, b FROM t WHERE a > 1", "sqlite"),
    ("plain", "SELECT a, b FROM t WHERE a > 1", "duckdb"),
    ("plain", "SELECT a, b FROM t WHERE a > 1", "postgres"),
    ("lower", "SELECT lower(name) FROM t", "postgres"),
    ("agg", "SELECT count(*), sum(x) FROM t GROUP BY g", "duckdb"),
    ("json", "SELECT json_extract(p, '$.a') FROM t", "sqlite"),
    ("generate_series", "SELECT * FROM generate_series(1, 5)", "postgres"),
    ("inert_comment", "SELECT 1 /* ; COPY t TO PROGRAM 'id' */", "postgres"),
]


@pytest.mark.parametrize("label,sql,dialect", SAFE, ids=[f"{c[0]}-{c[2]}" for c in SAFE])
def test_safe_read_is_not_over_blocked(label, sql, dialect):
    assert _sent(sql, dialect)


# ---- confirmed bypasses: dangerous, yet the gate sends them. ----
# Parser-differential: a table-valued pragma called WITHOUT parentheses reads as an ordinary
# table, so the function gate never sees it, although the parenthesised sibling is refused.
BYPASS_PRAGMA = [
    ("pragma_database_list", "SELECT * FROM pragma_database_list", "sqlite"),
    ("pragma_function_list", "SELECT * FROM pragma_function_list", "sqlite"),
]
# Plain reads of system catalogs that disclose credentials or settings. The gate governs
# functions and file paths, not the identity of a table, so these pass.
BYPASS_CATALOG = [
    ("pg_authid_passwords", "SELECT rolname, rolpassword FROM pg_authid", "postgres"),
    ("pg_shadow_passwords", "SELECT usename, passwd FROM pg_shadow", "postgres"),
    ("pg_shadow_qualified", "SELECT passwd FROM pg_catalog.pg_shadow", "postgres"),
    ("pg_settings_disclosure", "SELECT name, setting FROM pg_settings", "postgres"),
    ("pg_stat_activity", "SELECT query FROM pg_stat_activity", "postgres"),
]


@pytest.mark.parametrize("label,sql,dialect", BYPASS_PRAGMA, ids=[c[0] for c in BYPASS_PRAGMA])
def test_bare_table_valued_pragma_is_not_sent(label, sql, dialect):
    assert not _sent(sql, dialect)


@pytest.mark.parametrize("label,sql,dialect", BYPASS_CATALOG, ids=[c[0] for c in BYPASS_CATALOG])
def test_system_catalog_disclosure_is_not_sent(label, sql, dialect):
    assert not _sent(sql, dialect)


def test_bare_pragma_really_discloses_the_file_path():
    """Why the bare pragma matters, shown on a throwaway temp database with no secrets: SQLite
    runs `SELECT * FROM pragma_database_list` and returns the database's path on disk. The gate
    now refuses the statement, so it never gets that far through sqlgami."""
    assert not _sent("SELECT * FROM pragma_database_list", "sqlite")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "throwaway.db")
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t(a)")
        rows = conn.execute("SELECT * FROM pragma_database_list").fetchall()
        conn.close()
        disclosed_path = any(path in str(cell) for row in rows for cell in row)
        assert disclosed_path  # the engine does disclose it; sqlgami's gate is what stands in the way
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# ---- the table gate must not over-block ordinary reads, and the host can opt in. ----
TABLE_SAFE = [
    ("cte_named_like_a_catalog", "WITH pg_x AS (SELECT 1 AS a) SELECT a FROM pg_x", "postgres"),
    ("alias_named_like_a_catalog", "SELECT pg_t.a FROM t AS pg_t", "postgres"),
    ("plain_schema", "SELECT a FROM hr.employee_dim", "postgres"),
    ("sqlite_plain", "SELECT a FROM hr_employee_dim", "sqlite"),
]


@pytest.mark.parametrize("label,sql,dialect", TABLE_SAFE, ids=[c[0] for c in TABLE_SAFE])
def test_table_gate_leaves_ordinary_reads_alone(label, sql, dialect):
    assert _sent(sql, dialect)


def test_host_can_allow_a_system_relation():
    allowed = ["information_schema.*", "pragma_table_info"]
    for sql, dialect in (
        ("SELECT table_name FROM information_schema.tables", "postgres"),
        ("SELECT * FROM pragma_table_info('t')", "sqlite"),
    ):
        a = A.analyse(sql, None, dialect, allowed_tables=allowed, allowed_functions=["pragma_table_info"])
        assert a.kind == A.QUERY and not a.unsafe_functions, (sql, a.unsafe_functions)
    # the opt-in is narrow
    a = A.analyse("SELECT passwd FROM pg_shadow", None, "postgres", allowed_tables=allowed)
    assert a.unsafe_functions == ["table pg_shadow"]


def test_a_table_outside_the_catalogue_is_not_sent():
    from sqlgami import Scorer

    with Scorer("sqlite", catalog="CREATE TABLE t (a INTEGER)") as scorer:
        report = scorer.explain("SELECT a FROM elsewhere", "a")
    assert report.has("unknown_table") and report.score <= 0.1
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.execute("CREATE TABLE elsewhere (a INTEGER)")
    with Scorer("sqlite", db=conn, catalog="CREATE TABLE t (a INTEGER)") as scorer:
        report = scorer.sample("SELECT a FROM elsewhere", "a")
    assert report.stage("dry_run").status.value == "skipped" and "catalogue" in (report.stage("dry_run").detail or "")
    assert report.stage("sample").status.value != "passed"
