"""Boundary audit of the BigQuery → DuckDB translation (ADR 0003 B6 seeds).

``tests/data/bq_boundary_cases.jsonl`` holds self-contained BigQuery statements — week starts for
all seven days, year/month ends, leap days, NULLs, empty arrays, ties, offsets, float and NUMERIC
arithmetic, string functions, casts, windows, set operations — with the rows BigQuery returned
natively on the sandbox (``sqlgami-dev-1109``, 2026-11-10), normalised as the test normalises
DuckDB's. The contract is translate.py's: a statement the allow-list vouches for MUST give the
same rows through the translation; one it does not vouch for raises ``Untranslatable`` (a lost
check, acknowledged here as a pass — the day its construct is verified, the comparison starts
running for it automatically); a statement BigQuery rejects must not run to a value on DuckDB.

Cases the translation still gets wrong are strict xfails: they turn red the moment a fix lands,
so the marker comes off with the fix. With ``SQLGAMI_BQ_LIVE=1`` the native rows are re-derived
from the sandbox so the stored values cannot rot.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import os
from pathlib import Path

import duckdb
import pytest

from sqlgami import translate as T

CASES_PATH = Path(__file__).parent / "data" / "bq_boundary_cases.jsonl"
CASES = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]

# id -> why the translation disagrees today (strict xfail; remove with the fix)
DISAGREE = {
    **dict.fromkeys(["trunc_week_sun", "trunc_week_sunday_explicit", "ts_trunc_week", "date_trunc_on_timestamp"],
                    "DATE_TRUNC(d, WEEK) is Sunday-based in BigQuery; DuckDB's DATE_TRUNC('week') is Monday-based"),
    **dict.fromkeys(["trunc_week_monday", "trunc_week_tuesday", "trunc_week_wednesday", "trunc_week_thursday",
                     "trunc_week_friday", "trunc_week_saturday", "ts_trunc_week_monday"],
                    "DATE_TRUNC(d, WEEK(<DAY>)) is emitted as DATE_TRUNC(WEEK(DAY), d), invalid DuckDB SQL"),
    **dict.fromkeys(["trunc_month", "trunc_quarter", "trunc_year"],
                    "DATE_TRUNC on a DATE comes back as TIMESTAMP in DuckDB (value right, type wrong)"),
    **dict.fromkeys(["add_month_end", "add_quarter", "add_week_day"],
                    "DATE_ADD/DATE_SUB on a DATE widens to TIMESTAMP in DuckDB (value right, type wrong)"),
    "extract_week": "EXTRACT(WEEK) is the Sunday-based week number [0, 53] in BigQuery; DuckDB gives the ISO week",
    "extract_week_monday": "EXTRACT(WEEK(MONDAY)) is emitted verbatim, a DuckDB syntax error",
    "last_day_week": "LAST_DAY(d, WEEK) is off by one (DuckDB's DAYOFWEEK is 0-based) and LAST_DAY(d, WEEK(MONDAY)) silently becomes the month end",
    **dict.fromkeys(["unnest_offset", "unnest_offset_noas", "unnest_offset_expr", "unnest_offset_subquery",
                     "unnest_offset_join", "unnest_offset_correlated", "unnest_offset_nulls"],
                    "UNNEST ... WITH OFFSET is 0-based; DuckDB's WITH ORDINALITY is 1-based"),
    **dict.fromkeys(["round_digits", "float_sum", "int_div", "avg_int", "cast_bool_string", "safe_divide", "int_div_dec", "numeric_sum"],
                    "a numeric literal with a point is FLOAT64 in BigQuery but DECIMAL in DuckDB, and CAST(FLOAT64/NUMERIC AS STRING) renders differently ('3' vs '3.0', %.15g/%.17g vs shortest, NUMERIC scale)"),
    "substr": "SUBSTR(s, 0, n) treats position 0 as 1 in BigQuery; DuckDB counts it",
    "cast_string_num": "SAFE_CAST('1.5' AS INT64) is NULL in BigQuery (not an integer string); DuckDB's TRY_CAST rounds to 2",
    "like": "LIKE treats a backslash as an escape in BigQuery; DuckDB needs ESCAPE '\\'",
    "format_date_e": "FORMAT_DATE %e is space-padded in BigQuery (DuckDB drops the padding) and %Q is not a DuckDB specifier",
    "trim_case": "UPPER('straße') is STRASSE under ICU, STRAẞE in DuckDB",
    "date_minus_date": "DATE - DATE is an INTERVAL in BigQuery, an integer in DuckDB",
    "group_by_alias_shadow": "GROUP BY <alias> resolves to the projected expression in BigQuery when a source column has the alias's name; DuckDB binds the column",
    **dict.fromkeys(["concat_null", "bool_null_logic", "union_all_distinct"],
                    "BigQuery rejects the statement (untyped NULL operand, mixed set operators); DuckDB runs it to a value"),
}


def _norm(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (float, decimal.Decimal)):
        f = float(v)
        return str(int(f)) if f == int(f) and abs(f) < 1e15 else f"{f:.10g}"
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(dt.timezone.utc)
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_norm(x) for x in v) + "]"
    return str(v)


def _duckdb_rows(sql: str):
    con = duckdb.connect(":memory:")
    try:
        con.execute("SET TimeZone='UTC'")
        return [tuple(_norm(c) for c in r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()


def _params():
    for case in CASES:
        marks = [pytest.mark.xfail(strict=True, reason=DISAGREE[case["id"]])] if case["id"] in DISAGREE else []
        yield pytest.param(case, id=case["id"], marks=marks)


@pytest.mark.parametrize("case", list(_params()))
def test_translation_matches_native_bigquery(case):
    try:
        translated = T.translate(case["sql"], "bigquery").sql
    except T.Untranslatable:
        return  # not vouched for: the fixture stage reports not_run, never a verdict
    if "native_error" in case:
        with pytest.raises(Exception):
            _duckdb_rows(translated)
        return
    assert _duckdb_rows(translated) == [tuple(r) for r in case["native_rows"]]


@pytest.mark.skipif(os.environ.get("SQLGAMI_BQ_LIVE") != "1", reason="re-derives the native rows on the sandbox only with SQLGAMI_BQ_LIVE=1")
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_native_rows_still_hold(case):
    from sqlgami.contrib.bigquery import BigQuerySandbox

    result = BigQuerySandbox().run(case["sql"])
    if "native_error" in case:
        assert not result.ok
        return
    assert result.ok, result.error
    assert [tuple(_norm(v) for v in r) for r in result.rows] == [tuple(r) for r in case["native_rows"]]
