"""Boundary audit of the BigQuery → DuckDB translation (ADR 0003 B6 seeds).

``tests/data/bq_boundary_cases.jsonl`` holds self-contained BigQuery statements — week starts for
all seven days, year/month ends, leap days, NULLs, empty arrays, ties, offsets, float and NUMERIC
arithmetic, string functions, casts, windows, set operations — with the rows BigQuery returned
natively on the sandbox (``sqlgami-dev-1109``, 2026-11-10), normalised as the test normalises
DuckDB's. The contract is translate.py's: a statement the allow-list vouches for MUST give the
same rows through the translation; one it does not vouch for raises ``Untranslatable`` (a lost
check, acknowledged here as a pass — the day its construct is verified, the comparison starts
running for it automatically); a statement BigQuery rejects must not run to a value on DuckDB.

The 2026-11-10 audit found 45 disagreements (Sunday weeks, WEEK(<DAY>), DATE_TRUNC/DATE_ADD type
widening, EXTRACT(WEEK), LAST_DAY WEEK, UNNEST WITH OFFSET, float literal typing and rendering,
NUMERIC scale, ROUND(x, n), SUBSTR 0, SAFE_CAST of decimal strings, LIKE escapes, %e/%Q, GROUP BY
alias precedence, DATE - DATE); the rewrites in translate.py close them, and the four that remain
are divergences a translation cannot remove, kept as strict xfails so they stay visible. With
``SQLGAMI_BQ_LIVE=1`` the native rows are re-derived from the sandbox so the stored values cannot rot.
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

# id -> a divergence the translation cannot remove (strict xfail, so it stays visible): BigQuery
# rejects a statement DuckDB runs (its typing is stricter), or the engines' Unicode casing differs.
# A native dry run (contrib.bigquery) is the check for the first kind; see docs/guarantees.md.
DISAGREE = {
    **dict.fromkeys(["concat_null", "bool_null_logic", "union_all_distinct"],
                    "known divergence: BigQuery rejects the statement (untyped NULL operand, mixed set operators); DuckDB runs it to a value"),
    "trim_case": "known divergence: UPPER('straße') is STRASSE under ICU, STRAẞE in DuckDB",
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
