"""The function allow-list: what may be sent to a database, and what happens to the rest."""

from __future__ import annotations

import re

import pytest
import sqlglot

from sqlgami import Options, Scorer, UnsafeQueryError
from sqlgami import _functions as F
from sqlgami import safety
from sqlgami.engines.base import assert_read_only

from conftest import PG_DSN

LISTS = {
    "sqlite": (F.SQLITE_ALLOWED, F.SQLITE_TABLE_FUNCTIONS),
    "duckdb": (F.DUCKDB_ALLOWED, F.DUCKDB_TABLE_FUNCTIONS),
    "postgres": (F.POSTGRES_ALLOWED, frozenset()),
}


@pytest.fixture(params=["sqlite", "duckdb", "postgres"])
def dialect(request) -> str:
    return request.param


def review(sql: str, dialect: str, allowed=(), allowed_tables=()):
    return safety.review(sql, sqlglot.parse_one(sql, dialect=dialect), dialect, allowed, allowed_tables)


# ----------------------------------------------------------------- ordinary SQL goes through

ORDINARY = [
    "SELECT c.name, COUNT(*), SUM(o.total), AVG(o.total), MIN(o.total), MAX(o.total) FROM customers c "
    "JOIN orders o ON o.customer_id = c.id GROUP BY c.name",
    "SELECT COALESCE(country, 'none'), NULLIF(name, ''), UPPER(name), LOWER(name), LENGTH(name), TRIM(name) FROM customers",
    "SELECT CAST(total AS INTEGER), ROUND(total, 1), ABS(total) FROM orders",
    "SELECT name, ROW_NUMBER() OVER (ORDER BY id), RANK() OVER (ORDER BY id), LAG(id) OVER (ORDER BY id) FROM customers",
    "SELECT CASE WHEN total > 50 THEN 'big' ELSE 'small' END, SUBSTR(status, 1, 2), REPLACE(status, 'a', 'b') FROM orders",
    "SELECT name FROM customers WHERE name LIKE 'A%' AND id IN (SELECT customer_id FROM orders)",
    "SELECT CURRENT_DATE, CURRENT_TIMESTAMP",
]


@pytest.mark.parametrize("sql", ORDINARY, ids=[s[:40] for s in ORDINARY])
def test_ordinary_sql_is_all_on_the_list(dialect, sql):
    assert review(sql, dialect).ok


PER_ENGINE_ORDINARY = [
    ("sqlite", "SELECT strftime('%Y-%m', created_at), date(created_at), julianday(created_at), IFNULL(total, 0) FROM orders"),
    ("sqlite", "SELECT group_concat(name, ','), printf('%d', id), instr(name, 'a') FROM customers"),
    ("sqlite", "SELECT j.value FROM customers c, json_each('[1,2]') AS j"),
    ("sqlite", "SELECT MAX(0, total), MIN(total, 10) FROM orders"),
    ("duckdb", "SELECT date_trunc('month', created_at), strftime(created_at, '%Y'), date_diff('day', created_at, now()) FROM orders"),
    ("duckdb", "SELECT string_agg(name, ','), list(name), arg_max(name, id), median(id), quantile_cont(id, 0.5) FROM customers"),
    ("duckdb", "SELECT * FROM range(3) AS r, generate_series(1, 2) AS g"),
    ("duckdb", "SELECT unnest([1, 2, 3]), epoch(created_at), regexp_matches(status, 'p.*') FROM orders"),
    ("postgres", "SELECT date_trunc('month', created_at), to_char(created_at, 'YYYY-MM'), age(created_at), EXTRACT(year FROM created_at) FROM orders"),
    ("postgres", "SELECT string_agg(name, ','), array_agg(name), percentile_cont(0.5) WITHIN GROUP (ORDER BY id) FROM customers"),
    ("postgres", "SELECT g FROM generate_series(1, 3) AS g"),
    ("postgres", "SELECT pg_catalog.lower(name), jsonb_build_object('a', id), regexp_replace(name, 'a', 'b') FROM customers"),
    ("postgres", "SELECT pg_typeof(id), now(), make_date(2024, 1, 1), width_bucket(id, 0, 10, 2) FROM customers"),
]


@pytest.mark.parametrize("dialect,sql", PER_ENGINE_ORDINARY, ids=[f"{d}:{s[:36]}" for d, s in PER_ENGINE_ORDINARY])
def test_engine_specific_functions_are_on_the_list(dialect, sql):
    found = review(sql, dialect)
    assert found.ok, (found.refused, found.unlisted)


# --------------------------------------------------------------------------- what is refused

REFUSED = [
    ("duckdb", "SELECT * FROM read_csv('/etc/passwd')", "read_csv"),
    ("duckdb", "SELECT * FROM sniff_csv('/etc/passwd')", "sniff_csv"),
    ("duckdb", "SELECT * FROM glob('/etc/*')", "glob"),
    ("duckdb", "SELECT * FROM duckdb_settings()", "duckdb_settings"),
    ("duckdb", "SELECT * FROM duckdb_secrets()", "duckdb_secrets"),
    ("duckdb", "SELECT current_setting('home_directory')", "current_setting"),
    ("duckdb", "SELECT getenv('HOME')", "getenv"),
    ("duckdb", "SELECT setseed(0.5)", "setseed"),
    ("duckdb", "SELECT nextval('s')", "nextval"),
    ("duckdb", "SELECT * FROM '/etc/passwd'", "file '/etc/passwd'"),
    ("duckdb", "SELECT * FROM 'data.parquet'", "file 'data.parquet'"),
    ("duckdb", "SELECT * FROM query('SELECT 1')", "query"),
    ("postgres", "SELECT pg_reload_conf()", "pg_reload_conf"),
    ("postgres", "SELECT pg_export_snapshot()", "pg_export_snapshot"),
    ("postgres", "SELECT txid_current()", "txid_current"),
    ("postgres", "SELECT current_setting('data_directory')", "current_setting"),
    ("postgres", "SELECT query_to_xml('DELETE FROM payments', true, false, '')", "query_to_xml"),
    ("postgres", "SELECT * FROM pg_ls_dir('/')", "pg_ls_dir"),
    ("postgres", "SELECT pg_catalog.pg_sleep(1)", "pg_sleep"),
    ("postgres", 'SELECT "pg_sleep"(1)', "pg_sleep"),
    ("postgres", "SELECT (SELECT 1 FROM pg_advisory_lock(1))", "pg_advisory_lock"),
    ("postgres", "SELECT name FROM customers WHERE id = (SELECT nextval('s'))", "nextval"),
    ("postgres", "SELECT version()", "version"),
    ("postgres", "SELECT ts_stat('SELECT 1')", "ts_stat"),
    ("postgres", "SELECT lo_get(1)", "lo_get"),
    ("sqlite", "SELECT load_extension('x')", "load_extension"),
    ("sqlite", "SELECT readfile('/etc/hosts')", "readfile"),
    ("sqlite", "SELECT * FROM pragma_table_info('customers')", "pragma_table_info"),
    ("sqlite", "SELECT sqlite_version()", "sqlite_version"),
    ("sqlite", "SELECT * FROM dbstat", None),  # a table, not a function: the schema gate's job
]


@pytest.mark.parametrize("dialect,sql,name", REFUSED, ids=[f"{d}:{s[:40]}" for d, s, _ in REFUSED])
def test_reaching_outside_the_tables_is_refused(dialect, sql, name):
    found = review(sql, dialect)
    if name is None:
        assert found.ok
        return
    assert name in found.refused, (found.refused, found.unlisted)
    with pytest.raises(UnsafeQueryError):
        assert_read_only(sql, dialect)


def test_a_table_function_cannot_hide_among_scalars():
    # glob is a harmless operator in SQLite and a directory listing in DuckDB's FROM.
    assert review("SELECT name FROM customers WHERE name GLOB 'A*'", "sqlite").ok
    assert review("SELECT * FROM glob('/etc/*')", "duckdb").refused == ["glob"]
    # On DuckDB a scalar function's name does not make a table function safe.
    assert not review("SELECT * FROM lower('x')", "duckdb").ok


@pytest.mark.parametrize("clause", ["FOR UPDATE", "FOR SHARE", "FOR NO KEY UPDATE"])
def test_row_locks_are_refused(clause):
    with pytest.raises(UnsafeQueryError):
        assert_read_only(f"SELECT * FROM customers {clause}", "postgres")


# ------------------------------------------------------------------- what is merely unknown


def test_unknown_function_is_unlisted_not_refused(dialect):
    found = review("SELECT my_udf(name) FROM customers", dialect)
    assert found.unlisted == ["my_udf"] and not found.refused
    assert review("SELECT my_udf(name) FROM customers", dialect, allowed=["MY_UDF"]).ok


def test_function_in_somebody_elses_schema_is_unlisted():
    found = review("SELECT reporting.lower(name) FROM customers", "postgres")
    assert found.unlisted == ["reporting.lower"]
    assert review("SELECT reporting.lower(name) FROM customers", "postgres", allowed=["reporting.lower"]).ok


def test_dialect_without_a_list_keeps_the_refuse_list():
    assert not safety.has_allow_list("snowflake")
    assert review("SELECT my_udf(x), IFF(a > b, a, b) FROM t", "snowflake").ok
    assert review("SELECT * FROM read_csv('x')", "snowflake").refused


def test_bigquery_has_its_own_list_and_gate():
    assert safety.has_allow_list("bigquery")
    assert review("SELECT SAFE_DIVIDE(a, b), COUNTIF(x > 1), DATE_TRUNC(d, WEEK(MONDAY)) FROM `p.d.t`", "bigquery").ok
    found = review("SELECT my_udf(x), ds.f(y), `p.ds.g`(z) FROM t", "bigquery")
    assert found.unlisted == ["`p.ds.g`", "ds.f", "my_udf"] or set(found.unlisted) == {"p.ds.g", "ds.f", "my_udf"}
    assert "ml.predict" in review("SELECT * FROM ML.PREDICT(MODEL `p.d.m`, (SELECT * FROM `p.d.t`))", "bigquery").refused
    assert "external_query" in review("SELECT * FROM EXTERNAL_QUERY('c', 'SELECT 1')", "bigquery").refused
    assert any("information_schema" in r for r in review("SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT", "bigquery").refused)
    assert any("wildcard" in r for r in review("SELECT * FROM `p.d.events_*`", "bigquery").refused)
    assert review("SELECT * FROM `p.d.events_*`", "bigquery", allowed_tables=["p.d.*"]).ok
    assert "session_user" in review("SELECT SESSION_USER()", "bigquery").refused
    assert review("SELECT SESSION_USER()", "bigquery", allowed=["session_user"]).ok
    assert any("SYSTEM_TIME" in r for r in review("SELECT * FROM `p.d.t` FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP()", "bigquery").refused)
    assert review("SELECT * FROM `p.d.t` FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP()", "bigquery", allowed=["system_time_as_of"]).ok


def test_a_call_the_parser_reads_as_a_table_is_still_caught():
    # sqlglot reads these as a table with a column alias; the engine would call a function.
    assert not review("SELECT * FROM show('a')", "duckdb").ok
    assert not review("SELECT * FROM match('a')", "sqlite").ok
    # Names the statement defines itself are not calls.
    assert review("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r", "sqlite").ok
    assert review("SELECT v.a FROM (VALUES (1), (2)) AS v(a)", "postgres").ok
    assert review("SELECT CAST(total AS DECIMAL(10, 2)), x IN (1, 2) FROM orders AS o(x, total)", "duckdb").ok


def _watch(scorer) -> list[str]:
    sent: list[str] = []
    real_execute, real_dry = scorer.adapter.execute, scorer.adapter.dry_run
    scorer.adapter.execute = lambda text, **kw: (sent.append(text), real_execute(text, **kw))[1]
    scorer.adapter.dry_run = lambda text: (sent.append(text), real_dry(text))[1]
    return sent


def test_made_up_function_is_reported_without_sending_the_query(engine, db):
    sql = "SELECT my_udf(name) FROM customers"
    with Scorer(engine, db=db) as scorer:
        sent = _watch(scorer)
        report = scorer.sample(sql, "names")
        assert sql not in sent
        assert report.has("unknown_function") and report.verdict.value == "invalid"
        assert report.stage("dry_run").status.value == "failed"
        assert not scorer.explain(sql, "names").has("unknown_function")  # static: no catalogue to ask


def test_existing_but_unlisted_function_is_analysed_and_not_sent():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.create_function("double_it", 1, lambda v: v * 2)
    sql = "SELECT double_it(x) AS d FROM t"
    with Scorer("sqlite", db=conn) as scorer:
        sent = _watch(scorer)
        report = scorer.sample(sql, "doubles")
        assert sql not in sent
        assert report.has("unlisted_function") and report.verdict.value != "invalid"
        assert report.stage("dry_run").status.value == "skipped" and report.stage("sample").status.value == "skipped"
        assert report.stage("static").status.value == "passed"
        assert not scorer.explain(sql, "doubles").has("unlisted_function")  # nothing would be sent anyway


def test_refused_function_is_gated(engine, db):
    sql = {
        "sqlite": "SELECT load_extension('x')",
        "duckdb": "SELECT * FROM sniff_csv('/etc/passwd')",
        "postgres": "SELECT pg_reload_conf()",
    }[engine]
    with Scorer(engine, db=db) as scorer:
        report = scorer.sample(sql, "anything")
        assert report.has("external_access") and report.verdict.value == "invalid"
        with pytest.raises(UnsafeQueryError):
            scorer.adapter.execute(sql, max_rows=5, timeout_s=5)
        with pytest.raises(UnsafeQueryError):
            scorer.adapter.dry_run(sql)


def test_host_can_vouch_for_its_own_function():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (2), (3)")
    conn.create_function("double_it", 1, lambda v: v * 2)
    sql = "SELECT double_it(x) AS d FROM t ORDER BY x"
    with Scorer("sqlite", db=conn) as scorer:
        assert scorer.sample(sql).stage("sample").status.value == "skipped"
    with Scorer("sqlite", db=conn, allowed_functions=["double_it"]) as scorer:
        report = scorer.sample(sql)
        assert report.stage("sample").status.value == "passed" and not report.has("unlisted_function")
    with Scorer("sqlite", db=conn, options=Options(allowed_functions=("double_it",))) as scorer:
        assert scorer.sample(sql).stage("sample").status.value == "passed"


# ---------------------------------------------- the lists against each engine's own catalogue


def _calls(name: str) -> list[str]:
    forms = []
    for args in ("", "'a'", "'a', 'b'", "1", "1, 2", "'a', 'b', 'c'"):
        forms.append(f"SELECT {name}({args})")
        forms.append(f"SELECT * FROM {name}({args})")
    return forms


def _assert_everything_else_is_stopped(dialect: str, names: set[str]) -> None:
    allowed, tables = LISTS[dialect]
    escaped = []
    for name in sorted(names):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", name) or name in allowed or name in tables:
            continue
        for sql in _calls(name):
            try:
                tree = sqlglot.parse_one(sql, dialect=dialect)
            except Exception:
                continue  # not SQL as far as the gate is concerned, so it is never sent
            if safety.review(sql, tree, dialect).ok:
                escaped.append(sql)
    assert not escaped, escaped[:20]


def test_every_other_sqlite_builtin_is_stopped():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    names = {row[0].lower() for row in conn.execute("SELECT name FROM pragma_function_list")}
    names |= {row[0].lower() for row in conn.execute("SELECT name FROM pragma_module_list")}
    _assert_everything_else_is_stopped("sqlite", names)


def test_every_other_duckdb_builtin_is_stopped():
    duckdb = pytest.importorskip("duckdb")
    names = {row[0].lower() for row in duckdb.connect().execute("SELECT function_name FROM duckdb_functions()").fetchall()}
    _assert_everything_else_is_stopped("duckdb", names)


@pytest.mark.skipif(not PG_DSN, reason="needs SQLGAMI_TEST_PG_DSN")
def test_every_other_postgres_builtin_is_stopped():
    import psycopg

    with psycopg.connect(PG_DSN) as conn:
        names = {row[0] for row in conn.execute("SELECT proname FROM pg_proc").fetchall()}
    _assert_everything_else_is_stopped("postgres", names)


def test_volatile_postgres_functions_are_not_on_the_list():
    for name in ("nextval", "setval", "set_config", "pg_sleep", "pg_notify", "lo_import", "pg_read_file",
                 "pg_terminate_backend", "pg_advisory_lock", "txid_current", "pg_reload_conf", "dblink"):
        assert name not in F.POSTGRES_ALLOWED


# ------------------------------------------------------------------------- run-to-run changes


def test_nondeterministic_calls_are_named():
    assert safety.nondeterministic(sqlglot.parse_one("SELECT random(), name FROM customers", dialect="sqlite"))
    assert safety.nondeterministic(sqlglot.parse_one("SELECT gen_random_uuid()", dialect="postgres"))
    assert safety.nondeterministic(sqlglot.parse_one("SELECT * FROM customers TABLESAMPLE BERNOULLI (10)", dialect="postgres"))
    assert not safety.nondeterministic(sqlglot.parse_one("SELECT name, now() FROM customers", dialect="postgres"))


def test_an_allowed_function_this_build_lacks_is_caught_by_the_dry_run():
    """The allow-list says what is safe to send, not what exists: SQLite built without the math
    functions rejects FLOOR at prepare time, and that is a dry_run_error, never a pass."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x REAL)")
    with Scorer("sqlite", db=conn, allowed_functions=["fakefloor"]) as scorer:  # vouched for, but absent
        report = scorer.dry_run("SELECT CAST(fakefloor(x) AS INTEGER) FROM t", "x rounded down")
    assert report.has("dry_run_error") and "no such function" in report.summary().lower() or report.score <= 0.1
    assert report.stage("dry_run").status.value == "failed"
