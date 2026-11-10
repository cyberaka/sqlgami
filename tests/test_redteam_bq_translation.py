"""Translation-fidelity red-team: BigQuery native vs ``sqlgami.translate`` -> DuckDB.

The contract (translate.py): ``translate`` transpiles only when every construct is on the
VERIFIED allow-list; anything else raises ``Untranslatable`` -> the construct comes out
``not_run``. So for a VERIFIED construct the DuckDB translation MUST reproduce the BigQuery
result; a disagreement there is HIGH. An unsupported construct must surface as ``not_run``, never
a silent wrong answer and never a crash counted as a pass.

Each case is a self-contained BigQuery query with inline data. The expected ``native`` values
were captured from the sandbox (sqlgami-dev-1109); the opt-in ``test_native_values_still_hold``
re-derives them so they cannot rot. The hermetic tests need only DuckDB (no network).

The jsonl the builder's B6 fuzz harness imports lives at tests/data/bq_translation_cases.jsonl.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import duckdb
import pytest

from sqlgami import translate as T
from sqlgami.translate import Untranslatable


def _duckdb_rows(sql: str):
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    return [tuple("" if c is None else str(c) for c in r) for r in rows]


def _native(rows):
    return [tuple(r) for r in rows]


# (id, bq_sql, native_rows) — translate ACCEPTS these; DuckDB must agree.
AGREE = [
    ("date_diff_week", "SELECT DATE_DIFF(DATE '2024-01-15', DATE '2024-01-07', WEEK) AS v", [["1"]]),
    ("safe_divide_zero", "SELECT CAST(SAFE_DIVIDE(1, 0) AS STRING) AS v", [[""]]),
    ("ifnull_null", "SELECT IFNULL(CAST(NULL AS INT64), -1) AS v", [["-1"]]),
    ("countif_nulls", "SELECT COUNTIF(x > 1) AS v FROM UNNEST([1, 2, NULL, 3]) AS x", [["2"]]),
    ("int_div_slash", "SELECT CAST(7 / 2 AS STRING) AS v", [["3.5"]]),
    ("div_func", "SELECT DIV(7, 2) AS v", [["3"]]),
    ("round_half", "SELECT CAST(ROUND(2.5) AS STRING) AS a, CAST(ROUND(-2.5) AS STRING) AS b", [["3", "-3"]]),
    ("numeric_round_half", "SELECT CAST(ROUND(CAST(2.5 AS NUMERIC)) AS STRING) AS v", [["3"]]),
    ("string_agg_order_nulls",
     "SELECT STRING_AGG(s, ',' ORDER BY s) AS v FROM UNNEST(['b','a',CAST(NULL AS STRING),'c']) AS s", [["a,b,c"]]),
    ("qualify_rank_ties", "SELECT v FROM UNNEST([1,1,2,3]) AS v QUALIFY RANK() OVER (ORDER BY v) <= 1 ORDER BY v",
     [["1"], ["1"]]),
    ("timestamp_at_tz_extract",
     "SELECT EXTRACT(HOUR FROM TIMESTAMP '2024-01-01 23:30:00+00' AT TIME ZONE 'Asia/Kolkata') AS v", [["5"]]),
]


@pytest.mark.parametrize("cid,sql,native", AGREE, ids=[c[0] for c in AGREE])
def test_translation_agrees_with_native(cid, sql, native):
    tr = T.translate(sql, "bigquery")  # must not raise: these are VERIFIED
    assert _duckdb_rows(tr.sql) == _native(native)


# translate ACCEPTS these (VERIFIED), but the DuckDB translation DISAGREES with BigQuery.
# Each is a confirmed HIGH: a construct the allow-list vouches for yet transpiles wrong.
DISAGREE = [
    pytest.param(
        "date_trunc_week_sunday",
        "SELECT CAST(DATE_TRUNC(DATE '2024-01-10', WEEK) AS STRING) AS v", [["2024-01-07"]],
        id="date_trunc_week_sunday",
    ),
    pytest.param(
        "date_trunc_week_monday",
        "SELECT CAST(DATE_TRUNC(DATE '2024-01-10', WEEK(MONDAY)) AS STRING) AS v", [["2024-01-08"]],
        id="date_trunc_week_monday",
    ),
    pytest.param(
        "unnest_with_offset",
        "SELECT e, off FROM UNNEST(['x','y','z']) AS e WITH OFFSET AS off ORDER BY off",
        [["x", "0"], ["y", "1"], ["z", "2"]],
        id="unnest_with_offset",
    ),
]


@pytest.mark.parametrize("cid,sql,native", DISAGREE)
def test_translation_disagreement_is_a_defect(cid, sql, native):
    tr = T.translate(sql, "bigquery")  # translate vouches for it (no Untranslatable)
    assert _duckdb_rows(tr.sql) == _native(native)


# Unsupported constructs must surface as not_run (Untranslatable), never a silent/crashing pass.
NOT_RUN = [
    ("array_agg_ignore_nulls",
     "SELECT ARRAY_TO_STRING(ARRAY_AGG(CAST(x AS STRING) IGNORE NULLS ORDER BY x), ',') AS v FROM UNNEST([3,1,NULL,2]) AS x"),
    ("datetime_literal", "SELECT CAST(DATETIME '2024-01-01 12:34:56' AS STRING) AS v"),
]


@pytest.mark.parametrize("cid,sql", NOT_RUN, ids=[c[0] for c in NOT_RUN])
def test_unsupported_construct_is_not_run(cid, sql):
    with pytest.raises(Untranslatable):
        T.translate(sql, "bigquery")


# --------------------------------------------------------------------------------------------
# Opt-in: re-derive every expected native value from the sandbox so the hard-coded values above
# cannot silently rot. Skips cleanly without the BigQuery sandbox.
# --------------------------------------------------------------------------------------------
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
    return [tuple(r) for r in rows[1:]]


@pytest.mark.skipif(not _bq_available(), reason="BigQuery sandbox / SQLGAMI_BQ_PROJECT=sqlgami-dev-1109 not set")
@pytest.mark.parametrize("cid,sql,native",
                         [(c[0], c[1], c[2]) for c in AGREE] + [(p.values[0], p.values[1], p.values[2]) for p in DISAGREE],
                         ids=[c[0] for c in AGREE] + [p.id for p in DISAGREE])
def test_native_values_still_hold(cid, sql, native):
    assert _bq_rows(sql) == _native(native)
