"""Red-team of the safety gate for the **bigquery** dialect.

BigQuery is a statically-analysed dialect (``engines.STATIC_ONLY``): sqlgami never runs it
through ``score()``, but ``contrib.bigquery`` DOES run statements natively on the sandbox as
reference runs and loads them as fixtures. So the gate's verdict decides what reaches the
sandbox and what counts as a pass — exactly the contract these tests probe.

The gate's shape matters here. ``safety._LISTS`` has entries for sqlite/duckdb/postgres but
**none for bigquery**, so ``has_allow_list("bigquery")`` is False. ``review_functions`` and
``review_text`` therefore refuse only names on ``REFUSED_EVERYWHERE``; every other function
passes. The only bigquery-specific guard is ``review_tables`` via
``_SYSTEM_TABLE_SCHEMAS["bigquery"] = {"information_schema"}`` — and the tests below show that
guard never actually fires, because sqlglot collapses ``INFORMATION_SCHEMA.X`` into a single
quoted identifier.

These tests are STATIC: they call ``analysis.analyse()`` and read the verdict. Nothing is
executed here. One opt-in runtime test (skipped without the BigQuery sandbox) proves the
INFORMATION_SCHEMA disclosure is real and not theoretical.

Legend:
* CONTAINED  — dangerous, correctly not sent (by the dialect-independent *kind* gate). Guards.
* SAFE       — legitimate reads that must still be sent (no over-blocking). Guards.
* BYPASS_*   — holes the checker found, closed since (BigQuery function allow-list, INFORMATION_SCHEMA
               on folded and region-qualified names, ML/AI/OBJ namespaces, EXTERNAL_QUERY everywhere,
               wildcard/time-travel/SESSION_USER policy findings, unknown TVF/UDF unlisted); plain tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from sqlgami import analysis as A

DIALECT = "bigquery"


def _sent(sql: str, allowed_tables=(), allowed_functions=()) -> bool:
    """True when the gate would send this statement to the engine / make it a fixture."""
    a = A.analyse(sql, None, DIALECT, allowed_functions=allowed_functions, allowed_tables=allowed_tables)
    return (
        a.kind == A.QUERY
        and not a.unsafe_functions
        and not a.unlisted_functions
        and not a.script_statements
    )


# --------------------------------------------------------------------------------------------
# CONTAINED: dangerous statements the *kind* gate stops (not a QUERY). These do not depend on a
# bigquery allow-list, so they are robust — kept as regression guards.
# --------------------------------------------------------------------------------------------
CONTAINED = [
    ("execute_immediate", "EXECUTE IMMEDIATE 'SELECT * FROM secrets'"),
    ("execute_immediate_concat", "EXECUTE IMMEDIATE CONCAT('SELECT * FROM ', 'secrets')"),
    ("call_procedure", "CALL mydataset.my_proc()"),
    ("begin_end_block", "BEGIN SELECT 1; END"),
    ("begin_exception_block", "BEGIN SELECT 1; EXCEPTION WHEN ERROR THEN SELECT 2; END"),
    ("create_temp_function_sql", "CREATE TEMP FUNCTION f(x INT64) AS (x + 1); SELECT f(1)"),
    ("create_temp_function_js", "CREATE TEMP FUNCTION f(x STRING) RETURNS STRING LANGUAGE js AS 'return x;'"),
    ("create_temp_table", "CREATE TEMP TABLE t AS SELECT 1 AS a"),
    ("export_data", "EXPORT DATA OPTIONS(uri='gs://b/out/*', format='CSV') AS SELECT * FROM t"),
    ("set_statement", "SET x = 1"),
    # DECLARE defaults that smuggle a dangerous body are stopped as a multi-statement script.
    ("declare_default_then_export", "DECLARE u STRING DEFAULT 'gs://b/o'; EXPORT DATA OPTIONS(uri=u, format='CSV') AS SELECT 1"),
    ("declare_default_then_execute", "DECLARE s STRING DEFAULT 'SELECT * FROM secrets'; EXECUTE IMMEDIATE s"),
]


@pytest.mark.parametrize("label,sql", CONTAINED, ids=[c[0] for c in CONTAINED])
def test_kind_gate_contains_scripts_and_ddl(label, sql):
    assert not _sent(sql)


# A handful of dangerous constructs are not sent *today* only because sqlglot currently fails to
# parse them into a statement (kind == EMPTY), NOT because the gate refused them. The protection
# is incidental: a parser that later understands these would let them through. Documented so the
# builder treats EMPTY-on-parse-failure as a non-guarantee, not a guard.
PARSE_INCIDENTAL = [
    ("load_data", "LOAD DATA INTO mydataset.t FROM FILES (format='CSV', uris=['gs://b/f.csv'])"),
    ("ml_generate_text", "SELECT ML.GENERATE_TEXT(MODEL `p.d.m`, 'hi')"),
    ("ml_weights", "SELECT * FROM ML.WEIGHTS(MODEL `p.d.m`)"),
    ("external_object_transform", "SELECT * FROM EXTERNAL_OBJECT_TRANSFORM(TABLE `p.d.t`, ['SIGNED_URL'])"),
]


@pytest.mark.parametrize("label,sql", PARSE_INCIDENTAL, ids=[c[0] for c in PARSE_INCIDENTAL])
def test_not_sent_today_but_only_via_parse_failure(label, sql):
    # Passes today. If one day it starts being *sent*, that is a real regression to investigate;
    # if it becomes contained by the gate proper (a non-EMPTY, non-QUERY kind), even better.
    a = A.analyse(sql, None, DIALECT)
    assert not _sent(sql)
    assert a.kind in (A.EMPTY, A.OTHER, A.MUTATION, A.MULTIPLE), (label, a.kind)


# --------------------------------------------------------------------------------------------
# SAFE: legitimate BigQuery reads near the allow-list edges that must still be sent.
# --------------------------------------------------------------------------------------------
SAFE = [
    ("plain", "SELECT a, b FROM `p.d.t` WHERE a > 1"),
    ("agg", "SELECT COUNT(*), SUM(x) FROM `p.d.t` GROUP BY g"),
    ("safe_divide", "SELECT SAFE_DIVIDE(a, b) FROM `p.d.t`"),
    ("date_diff_week", "SELECT DATE_DIFF(d1, d2, WEEK) FROM `p.d.t`"),
    ("date_trunc_week_monday", "SELECT DATE_TRUNC(d, WEEK(MONDAY)) FROM `p.d.t`"),
    ("qualify_ties", "SELECT a FROM `p.d.t` QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY a) = 1"),
    ("unnest_with_offset", "SELECT e, off FROM `p.d.t`, UNNEST(arr) AS e WITH OFFSET AS off"),
    ("string_agg", "SELECT STRING_AGG(name, ',' ORDER BY name) FROM `p.d.t`"),
    # a column merely named like a catalog is not a catalog read
    ("column_named_like_suffix", "SELECT _table_suffix_label FROM `p.d.t`"),
]


@pytest.mark.parametrize("label,sql", SAFE, ids=[c[0] for c in SAFE])
def test_safe_read_is_not_over_blocked(label, sql):
    assert _sent(sql)


# --------------------------------------------------------------------------------------------
# BYPASS — federation. EXTERNAL_QUERY runs arbitrary SQL against an external federated source
# (Cloud SQL / Spanner): the BigQuery analogue of postgres_query / dblink / mysql_query, which
# ARE on REFUSED_EVERYWHERE. The bigquery equivalent is not, and there is no bigquery allow-list
# to catch it, so it is sent. Severity: HIGH (reaches outside BigQuery entirely).
# --------------------------------------------------------------------------------------------
BYPASS_FEDERATION = [
    ("external_query_from", "SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT * FROM secrets')"),
    ("external_query_scalar", "SELECT EXTERNAL_QUERY('conn', 'SELECT 1')"),
]


@pytest.mark.parametrize("label,sql", BYPASS_FEDERATION, ids=[c[0] for c in BYPASS_FEDERATION])
def test_federation_function_is_not_sent(label, sql):
    assert not _sent(sql)


# --------------------------------------------------------------------------------------------
# BYPASS — system catalog. Every INFORMATION_SCHEMA form is sent. Root cause: sqlglot parses
# `INFORMATION_SCHEMA.TABLES` into ONE quoted identifier (this='INFORMATION_SCHEMA.TABLES',
# db=''), so review_tables' guards -- `schema in {"information_schema"}`, `name == "INFORMATION_
# SCHEMA"`, `schema == "INFORMATION_SCHEMA"` -- never match. `_SYSTEM_TABLE_SCHEMAS["bigquery"]`
# is dead code. `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT discloses other users' full query
# text and emails (proven by the runtime test below). Severity: HIGH (disclosure).
# --------------------------------------------------------------------------------------------
BYPASS_CATALOG = [
    ("info_schema_bare", "SELECT * FROM INFORMATION_SCHEMA.TABLES"),
    ("info_schema_region_jobs", "SELECT job_id, query FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT"),
    ("info_schema_dataset", "SELECT * FROM `p.d`.INFORMATION_SCHEMA.COLUMNS"),
    ("info_schema_dashed_project", "SELECT * FROM `my-proj`.INFORMATION_SCHEMA.SCHEMATA"),
    ("info_schema_backtick_whole_path", "SELECT * FROM `my-proj.region-us.INFORMATION_SCHEMA.JOBS`"),
]


@pytest.mark.parametrize("label,sql", BYPASS_CATALOG, ids=[c[0] for c in BYPASS_CATALOG])
def test_information_schema_is_not_sent(label, sql):
    assert not _sent(sql)


# --------------------------------------------------------------------------------------------
# BYPASS — ML. ML.PREDICT parses as a typed `Predict` node; with no bigquery allow-list it is in
# no refuse set and is sent. Other ML.* escape only via parse failure (see PARSE_INCIDENTAL).
# Severity: MEDIUM-HIGH (remote model inference / compute; some ML.* models call Vertex AI).
# --------------------------------------------------------------------------------------------
BYPASS_ML = [
    ("ml_predict", "SELECT * FROM ML.PREDICT(MODEL `p.d.m`, (SELECT * FROM `p.d.t`))"),
]


@pytest.mark.parametrize("label,sql", BYPASS_ML, ids=[c[0] for c in BYPASS_ML])
def test_ml_table_function_is_not_sent(label, sql):
    assert not _sent(sql)


# --------------------------------------------------------------------------------------------
# BYPASS — scope / disclosure (softer). These are legitimate BigQuery features, so blocking them
# outright is a policy call, not a clear-cut vulnerability. Reported so the builder can decide a
# bound. _TABLE_SUFFIX: unbounded wildcard scan across every table matching a prefix; SYSTEM_TIME
# AS OF: time-travel read of a table's past; SESSION_USER: identity disclosure.
# --------------------------------------------------------------------------------------------
BYPASS_SCOPE = [
    ("wildcard_table_suffix", "SELECT * FROM `p.d.events_*` WHERE _TABLE_SUFFIX BETWEEN '2020' AND '2099'"),
    ("wildcard_table_unfiltered", "SELECT _TABLE_SUFFIX, c FROM `p.d.events_*`"),
    ("system_time_as_of", "SELECT * FROM `p.d.t` FOR SYSTEM_TIME AS OF TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)"),
    ("session_user", "SELECT SESSION_USER()"),
]


@pytest.mark.parametrize("label,sql", BYPASS_SCOPE, ids=[c[0] for c in BYPASS_SCOPE])
def test_scope_widening_construct_is_not_sent(label, sql):
    assert not _sent(sql)


# --------------------------------------------------------------------------------------------
# BYPASS — unknown TVF / UDF. For sqlite/duckdb/postgres an unknown function name is classified
# `unlisted` (because that dialect has an allow-list), which stops the statement. For bigquery
# there is no allow-list, so review_functions hits `if lists is None: continue` and an unknown
# table-valued function or scalar UDF — including dashed-project and backtick-quoted calls — is
# neither refused nor unlisted, and is sent. An unknown TVF must never count as a pass.
# Severity: HIGH (a UDF/TVF body is opaque and may itself call EXTERNAL_QUERY, read a catalog,
# or have side effects; sqlgami cannot see inside it, so the safe default must be to not send).
# --------------------------------------------------------------------------------------------
BYPASS_UDF = [
    ("unknown_tvf_call", "SELECT * FROM mydataset.my_tvf(1)"),
    ("unknown_tvf_dashed_project", "SELECT * FROM `my-proj`.mydataset.my_tvf(1)"),
    ("unknown_tvf_backtick_path", "SELECT * FROM `my-proj.mydataset.my_tvf`(1)"),
    ("unknown_scalar_udf", "SELECT mydataset.my_udf(1) AS v"),
]


@pytest.mark.parametrize("label,sql", BYPASS_UDF, ids=[c[0] for c in BYPASS_UDF])
def test_unknown_tvf_or_udf_is_not_sent(label, sql):
    assert not _sent(sql)


# --------------------------------------------------------------------------------------------
# Runtime proof (opt-in): the INFORMATION_SCHEMA non-refusal has real consequences. Reads
# `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT on the sandbox (free, read-only) and shows it
# discloses other jobs' query text. Skips cleanly when the sandbox/credentials are absent.
# --------------------------------------------------------------------------------------------
def _bq_sandbox_available() -> bool:
    return bool(shutil.which("bq")) and bool(os.environ.get("SQLGAMI_BQ_PROJECT"))


@pytest.mark.skipif(not _bq_sandbox_available(), reason="BigQuery sandbox / SQLGAMI_BQ_PROJECT not configured")
def test_information_schema_really_discloses_query_text():
    project = os.environ["SQLGAMI_BQ_PROJECT"]
    assert project == "sqlgami-dev-1109", f"refusing to touch a non-sandbox project: {project!r}"
    # The gate sends this today (documented above) — here we show what it would expose.
    sql = ("SELECT query FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT "
           "WHERE query IS NOT NULL LIMIT 1")
    out = subprocess.run(
        ["bq", "--project_id", project, "query", "--use_legacy_sql=false", "--format=csv", "--max_rows=1", sql],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    # A non-empty query column back = another session's SQL text disclosed.
    assert len(out.stdout.strip().splitlines()) >= 2, out.stdout
