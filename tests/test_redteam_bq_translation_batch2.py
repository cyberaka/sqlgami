"""Translation-fidelity red-team, batch 2: date arithmetic, NUMERIC, EXTRACT week parts,
FORMAT_DATE, GENERATE_DATE_ARRAY, ARRAY/NULL, LIKE escaping, SAFE_CAST, timezone conversions,
integer overflow, and DECLARE/SET binding.

Same contract as batch 1 (see test_redteam_bq_translation.py): a VERIFIED construct's DuckDB
translation must reproduce the BigQuery result; a disagreement is a defect. An unsupported
construct must come out ``not_run`` (Untranslatable), never a silent wrong answer or a crash
counted as a pass. Native values were captured from sandbox sqlgami-dev-1109; the opt-in runtime
test re-derives them.

Booleans are normalised to BigQuery's spelling (true/false) so Python's True/False does not create
a spurious diff.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import duckdb
import pytest

from sqlgami import analysis as A
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


def _translate_query(sql: str) -> str:
    return T.translate(sql, "bigquery").sql


def _translate_script(sql: str) -> str:
    a = A.analyse(sql, None, "bigquery")
    assert a.bound_sql is not None, f"script did not bind: unbound={a.unbound_variables}"
    return T.translate(a.bound_sql, "bigquery").sql


# (id, mode, bq_sql, native_rows) -- translate ACCEPTS and DuckDB AGREES.
AGREE = [
    ("last_day", "q", "SELECT CAST(LAST_DAY(DATE '2024-02-10') AS STRING) AS v", [["2024-02-29"]]),
    ("extract_week", "q", "SELECT EXTRACT(WEEK FROM DATE '2024-01-07') AS v", [["1"]]),
    ("format_date", "q", "SELECT FORMAT_DATE('%Y-W%W-%u', DATE '2024-01-07') AS v", [["2024-W01-7"]]),
    ("generate_date_array_step", "q",
     "SELECT ARRAY_LENGTH(GENERATE_DATE_ARRAY(DATE '2024-01-01', DATE '2024-01-10', INTERVAL 2 DAY)) AS v", [["5"]]),
    ("array_length_with_null", "q", "SELECT ARRAY_LENGTH([1, NULL, 2]) AS v", [["3"]]),
    ("safe_cast_fail", "q", "SELECT CAST(SAFE_CAST('notanumber' AS INT64) AS STRING) AS v", [[""]]),
    ("date_from_ts_tz", "q",
     "SELECT CAST(DATE(TIMESTAMP '2024-01-01 23:30:00+00', 'Asia/Kolkata') AS STRING) AS v", [["2024-01-02"]]),
    ("like_wildcard_literal", "q", "SELECT ('axb' LIKE 'a\\\\_b') AS v", [["false"]]),
    ("declare_default_expr", "s", "DECLARE n INT64 DEFAULT 7 * 6; SELECT n AS v", [["42"]]),
]


@pytest.mark.parametrize("cid,mode,sql,native", AGREE, ids=[c[0] for c in AGREE])
def test_translation_agrees_with_native(cid, mode, sql, native):
    duck_sql = _translate_script(sql) if mode == "s" else _translate_query(sql)
    assert _duckdb_rows(duck_sql) == _native(native)


# translate ACCEPTS these but the DuckDB translation DISAGREES with BigQuery. Confirmed defects.
DISAGREE = [
    pytest.param(
        "date_add_month_end", "q",
        "SELECT CAST(DATE_ADD(DATE '2024-01-31', INTERVAL 1 MONTH) AS STRING) AS v", [["2024-02-29"]],
        marks=pytest.mark.xfail(strict=True, reason="MEDIUM: DATE_ADD on a DATE -- value is right "
                                "(month-end clamp to 2024-02-29) but DuckDB widens DATE+INTERVAL to "
                                "TIMESTAMP, so the string is '2024-02-29 00:00:00'. Type/row mismatch."),
        id="date_add_month_end",
    ),
    pytest.param(
        "date_add_leap_year", "q",
        "SELECT CAST(DATE_ADD(DATE '2024-02-29', INTERVAL 1 YEAR) AS STRING) AS v", [["2025-02-28"]],
        marks=pytest.mark.xfail(strict=True, reason="MEDIUM: DATE_ADD on a DATE widens to TIMESTAMP in "
                                "DuckDB ('2025-02-28 00:00:00' vs '2025-02-28')."),
        id="date_add_leap_year",
    ),
    pytest.param(
        "numeric_div", "q",
        "SELECT CAST(CAST(10 AS NUMERIC) / CAST(3 AS NUMERIC) AS STRING) AS v", [["3.333333333"]],
        marks=pytest.mark.xfail(strict=True, reason="MEDIUM-HIGH: BigQuery NUMERIC/NUMERIC keeps fixed "
                                "scale 9 (3.333333333); the DECIMAL translation yields a DOUBLE "
                                "(3.3333333333333335). Precision not preserved."),
        id="numeric_div",
    ),
    pytest.param(
        "like_escaped_underscore", "q",
        "SELECT ('a_b' LIKE 'a\\\\_b') AS v", [["true"]],
        marks=pytest.mark.xfail(strict=True, reason="HIGH: BigQuery LIKE treats backslash as an escape so "
                                "'a\\_b' matches the literal 'a_b' (true); DuckDB LIKE does not escape with "
                                "backslash by default, so it is false. Silent wrong filter."),
        id="like_escaped_underscore",
    ),
    pytest.param(
        "declare_set_month_end", "s",
        "DECLARE d DATE DEFAULT DATE '2024-01-31'; SET d = DATE_ADD(d, INTERVAL 1 MONTH); SELECT CAST(d AS STRING) AS v",
        [["2024-02-29"]],
        marks=pytest.mark.xfail(strict=True, reason="MEDIUM: DECLARE/SET binds correctly (d = 2024-02-29) "
                                "but inherits the DATE_ADD DATE->TIMESTAMP widening, so the string is "
                                "'2024-02-29 00:00:00'."),
        id="declare_set_month_end",
    ),
]


@pytest.mark.parametrize("cid,mode,sql,native", DISAGREE)
def test_translation_disagreement_is_a_defect(cid, mode, sql, native):
    duck_sql = _translate_script(sql) if mode == "s" else _translate_query(sql)
    assert _duckdb_rows(duck_sql) == _native(native)


# Unsupported constructs must surface as not_run (Untranslatable).
NOT_RUN = [
    ("extract_isoweek", "SELECT EXTRACT(ISOWEEK FROM DATE '2024-01-01') AS v"),
    ("extract_dayofweek", "SELECT EXTRACT(DAYOFWEEK FROM DATE '2024-01-07') AS v"),
    ("bignumeric_round", "SELECT CAST(ROUND(CAST(2.5 AS BIGNUMERIC)) AS STRING) AS v"),
    ("array_length_empty", "SELECT ARRAY_LENGTH(CAST([] AS ARRAY<INT64>)) AS v"),
    ("string_collation_ci", "SELECT ('abc' = COLLATE('ABC', 'und:ci')) AS v"),
    ("datetime_from_ts_tz", "SELECT CAST(DATETIME(TIMESTAMP '2024-01-01 23:30:00+00', 'Asia/Kolkata') AS STRING) AS v"),
]


@pytest.mark.parametrize("cid,sql", NOT_RUN, ids=[c[0] for c in NOT_RUN])
def test_unsupported_construct_is_not_run(cid, sql):
    with pytest.raises(Untranslatable):
        T.translate(sql, "bigquery")


def test_integer_overflow_is_rejected_not_silently_wrapped():
    """BigQuery raises on INT64 overflow. translate passes `+` through (an operator), and DuckDB
    also raises -- so it surfaces as an execution error, never a wrong value counted as a pass."""
    duck_sql = _translate_query("SELECT 9223372036854775807 + 1 AS v")
    with pytest.raises(Exception):  # DuckDB OutOfRangeException
        _duckdb_rows(duck_sql)


# --- opt-in: re-derive native (query cases only; scripts have a CSV header artifact) ------------
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
    return [tuple(c.lower() if c in ("true", "false", "True", "False") else c for c in r) for r in rows[1:]]


_QUERY_CASES = (
    [(c[0], c[2], c[3]) for c in AGREE if c[1] == "q"]
    + [(p.values[0], p.values[2], p.values[3]) for p in DISAGREE if p.values[1] == "q"]
)


@pytest.mark.skipif(not _bq_available(), reason="BigQuery sandbox / SQLGAMI_BQ_PROJECT=sqlgami-dev-1109 not set")
@pytest.mark.parametrize("cid,sql,native", _QUERY_CASES, ids=[c[0] for c in _QUERY_CASES])
def test_native_values_still_hold(cid, sql, native):
    assert _bq_rows(sql) == _native(native)
