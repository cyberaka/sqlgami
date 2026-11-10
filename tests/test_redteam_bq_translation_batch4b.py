"""Translation-fidelity red-team, batch 4B: PIVOT/UNPIVOT, ROLLUP/CUBE/GROUPING SETS,
WITH RECURSIVE, LIMIT/OFFSET ties, TABLESAMPLE, JSON, TO_JSON_STRING, APPROX_*, DST boundaries,
INTERVAL, and DATETIME_DIFF/TIMESTAMP_DIFF units.

Same contract as the earlier batches. A VERIFIED/accepted construct's DuckDB translation must
reproduce BigQuery; a disagreement is a defect. Nondeterministic or approximate constructs must
come out not_run so the differential never compares a random/approximate result.

Native values captured from sandbox sqlgami-dev-1109; the opt-in runtime test re-derives them.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import duckdb
import pytest
import sqlglot

from sqlgami import analysis as A
from sqlgami import safety
from sqlgami import translate as T
from sqlgami.translate import Untranslatable


def _cell(c) -> str:
    if c is None:
        return ""
    if isinstance(c, bool):
        return "true" if c else "false"
    return str(c)


def _duckdb_rows(sql: str):
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    return [tuple(_cell(c) for c in r) for r in rows]


def _native(rows):
    return [tuple(str(c) for c in r) for r in rows]


def _tq(sql: str) -> str:
    return T.translate(sql, "bigquery").sql


# ---- AGREE: translate accepts and DuckDB matches BigQuery (guards) -----------------------------
AGREE = [
    ("pivot", "SELECT a1, b1 FROM (SELECT k, v FROM UNNEST([STRUCT('a' AS k, 1 AS v),('b',2),('a',3)])) "
              "PIVOT(SUM(v) FOR k IN ('a' AS a1, 'b' AS b1))", [["4", "2"]]),
    ("unpivot", "SELECT k, v FROM (SELECT 1 AS a, 2 AS b) UNPIVOT(v FOR k IN (a, b)) ORDER BY k",
     [["a", "1"], ["b", "2"]]),
    ("rollup", "SELECT CAST(k AS STRING) AS k, SUM(v) AS s FROM UNNEST([STRUCT('a' AS k,1 AS v),('a',2),('b',3)]) "
               "GROUP BY ROLLUP(k) ORDER BY k", [["", "6"], ["a", "3"], ["b", "3"]]),
    ("cube", "SELECT CAST(k AS STRING) AS k, CAST(g AS STRING) AS g, SUM(v) AS s FROM "
             "UNNEST([STRUCT('a' AS k,'x' AS g,1 AS v),('a','y',2),('b','x',3)]) GROUP BY CUBE(k, g) ORDER BY k, g",
     [["", "", "6"], ["", "x", "4"], ["", "y", "2"], ["a", "", "3"], ["a", "x", "1"], ["a", "y", "2"],
      ["b", "", "3"], ["b", "x", "3"]]),
    ("grouping_sets", "SELECT CAST(k AS STRING) AS k, SUM(v) AS s FROM UNNEST([STRUCT('a' AS k,1 AS v),('b',2)]) "
                      "GROUP BY GROUPING SETS(k, ()) ORDER BY k", [["", "3"], ["a", "1"], ["b", "2"]]),
    ("with_recursive", "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 3) "
                       "SELECT n FROM t ORDER BY n", [["1"], ["2"], ["3"]]),
    ("limit_offset_ties", "SELECT v FROM UNNEST([1,1,2,2,3]) AS v ORDER BY v LIMIT 2 OFFSET 1", [["1"], ["2"]]),
    ("dst_extract_local", "SELECT EXTRACT(HOUR FROM TIMESTAMP '2024-03-10 09:30:00+00' AT TIME ZONE "
                          "'America/New_York') AS v", [["5"]]),
    ("interval_add_date", "SELECT CAST(DATE '2024-01-01' + INTERVAL 1 DAY AS STRING) AS v",
     [["2024-01-02 00:00:00"]]),
]


@pytest.mark.parametrize("cid,sql,native", AGREE, ids=[c[0] for c in AGREE])
def test_translation_agrees_with_native(cid, sql, native):
    assert _duckdb_rows(_tq(sql)) == _native(native)


# ---- Unsupported constructs correctly come out not_run (acceptable). Common funcs here are a
# coverage gap worth noting, not a correctness bug.
NOT_RUN = [
    ("json_extract", "SELECT JSON_EXTRACT('{\"a\":1}', '$.a') AS v"),
    ("json_value", "SELECT JSON_VALUE('{\"a\":\"x\"}', '$.a') AS v"),
    ("json_query", "SELECT JSON_QUERY('{\"a\":{\"b\":2}}', '$.a') AS v"),
    ("to_json_string_struct", "SELECT TO_JSON_STRING(STRUCT(1 AS a, 2 AS b)) AS v"),
    ("to_json_string_select_as_struct", "SELECT TO_JSON_STRING(t) AS v FROM (SELECT AS STRUCT 1 AS a, 2 AS b) t"),
    ("approx_quantiles", "SELECT CAST(APPROX_QUANTILES(x, 2)[OFFSET(1)] AS STRING) AS v FROM UNNEST([1,2,3,4,5]) AS x"),
    ("timestamp_add", "SELECT CAST(TIMESTAMP_ADD(TIMESTAMP '2024-03-10 09:30:00+00', INTERVAL 1 HOUR) AS STRING) AS v"),
    ("datetime_diff_week", "SELECT DATETIME_DIFF(DATETIME '2024-01-15 00:00:00', DATETIME '2024-01-01 00:00:00', WEEK) AS v"),
    ("timestamp_diff_hour", "SELECT TIMESTAMP_DIFF(TIMESTAMP '2024-01-01 12:00:00+00', TIMESTAMP '2024-01-01 09:30:00+00', HOUR) AS v"),
    ("datetime_diff_month", "SELECT DATETIME_DIFF(DATETIME '2024-03-31 00:00:00', DATETIME '2024-01-31 00:00:00', MONTH) AS v"),
]


@pytest.mark.parametrize("cid,sql", NOT_RUN, ids=[c[0] for c in NOT_RUN])
def test_unsupported_construct_is_not_run(cid, sql):
    with pytest.raises(Untranslatable):
        T.translate(sql, "bigquery")


# ---- The detection exists: safety.nondeterministic() flags a TABLESAMPLE query (passing) -------
def test_tablesample_is_detected_as_nondeterministic():
    tree = sqlglot.parse_one("SELECT COUNT(*) FROM UNNEST([1,2,3]) AS v TABLESAMPLE SYSTEM (10 PERCENT)",
                             read="bigquery")
    assert safety.nondeterministic(tree)  # 'a random sample of rows'


# ---- DISAGREE / must-be-not_run (defects, xfail strict) ---------------------------------------
@pytest.mark.xfail(strict=True, reason="HIGH: TABLESAMPLE is nondeterministic (safety.nondeterministic "
                                       "flags it) yet translate accepts it to DuckDB USING SAMPLE, so the "
                                       "differential compares a random sample. It must come out not_run.")
def test_tablesample_translation_should_be_not_run():
    with pytest.raises(Untranslatable):
        T.translate("SELECT COUNT(*) FROM UNNEST([1,2,3]) AS v TABLESAMPLE SYSTEM (10 PERCENT)", "bigquery")


@pytest.mark.xfail(strict=True, reason="HIGH: APPROX_COUNT_DISTINCT maps to the verified APPROX_DISTINCT "
                                       "and is translated and compared EXACTLY; different HLL "
                                       "implementations diverge on real data. It must be not_run or "
                                       "approximate-aware.")
def test_approx_count_distinct_should_be_not_run():
    with pytest.raises(Untranslatable):
        T.translate("SELECT APPROX_COUNT_DISTINCT(x) AS v FROM UNNEST([1,1,2,3,3]) AS x", "bigquery")


@pytest.mark.xfail(strict=True, reason="MEDIUM: an INTERVAL-typed result renders differently -- BigQuery "
                                       "'0-0 0 1:30:0' vs DuckDB '01:30:00'. Row mismatch under same_rows.")
def test_interval_literal_rendering_diverges():
    assert _duckdb_rows(_tq("SELECT CAST(INTERVAL 90 MINUTE AS STRING) AS v")) == _native([["0-0 0 1:30:0"]])


# ---- opt-in: re-derive the agreement native values from the sandbox ----------------------------
def _bq_available() -> bool:
    return bool(shutil.which("bq")) and os.environ.get("SQLGAMI_BQ_PROJECT") == "sqlgami-dev-1109"


def _bq_rows(sql: str):
    import csv
    import io

    out = subprocess.run(
        ["bq", "--project_id", "sqlgami-dev-1109", "query", "--use_legacy_sql=false",
         "--format=csv", "--max_rows=1000", sql],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    rows = list(csv.reader(io.StringIO(out.stdout)))
    return [tuple(c for c in r) for r in rows[1:]]


@pytest.mark.skipif(not _bq_available(), reason="BigQuery sandbox / SQLGAMI_BQ_PROJECT=sqlgami-dev-1109 not set")
@pytest.mark.parametrize("cid,sql,native", AGREE, ids=[c[0] for c in AGREE])
def test_native_values_still_hold(cid, sql, native):
    assert _bq_rows(sql) == _native(native)
