"""Translation-fidelity red-team, batch 3: NULL ordering, regex, strings, errors-vs-NULL,
script control flow, and STRUCT / SELECT * EXCEPT|REPLACE.

Same contract as batches 1-2 (test_redteam_bq_translation.py). A VERIFIED/accepted construct's
DuckDB translation must reproduce BigQuery; a disagreement is a defect. Unsupported constructs,
and script control flow the binder cannot faithfully handle, must come out not_run -- never a
silent wrong answer or a value where BigQuery raises.

Native values captured from sandbox sqlgami-dev-1109; the opt-in runtime test re-derives the
query-mode ones. Booleans normalised to BigQuery spelling; script results compared to the final
row (BigQuery's CSV prints a per-statement header).
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
    assert a.bound_sql is not None, f"did not bind: kind={a.kind} unbound={a.unbound_variables}"
    return T.translate(a.bound_sql, "bigquery").sql


# ---- AGREE: translate accepts, DuckDB matches BigQuery (guards) --------------------------------
AGREE = [
    # 1 NULL ordering -- translate emits NULLS FIRST/LAST explicitly to match BigQuery defaults
    ("order_by_asc_nulls", "q", "SELECT CAST(x AS STRING) AS v FROM UNNEST([2,NULL,1]) AS x ORDER BY x",
     [[""], ["1"], ["2"]]),
    ("order_by_desc_nulls", "q", "SELECT CAST(x AS STRING) AS v FROM UNNEST([2,NULL,1]) AS x ORDER BY x DESC",
     [["2"], ["1"], [""]]),
    ("window_rownum_nulls", "q",
     "SELECT CAST(x AS STRING) AS x, ROW_NUMBER() OVER (ORDER BY x) AS rn FROM UNNEST([2,NULL,1]) AS x ORDER BY rn",
     [["", "1"], ["1", "2"], ["2", "3"]]),
    ("string_agg_order_nullkey", "q",
     "SELECT STRING_AGG(lbl, ',' ORDER BY k) AS v FROM UNNEST([STRUCT('b' AS lbl, 2 AS k),('a',NULL),('c',1)])",
     [["a,c,b"]]),
    # 2 regex (REGEXP_CONTAINS is verified; RE2 \w is ASCII-only, matching DuckDB)
    ("regexp_contains_anchor", "q", "SELECT REGEXP_CONTAINS('abc', r'^a') AS v", [["true"]]),
    ("regexp_contains_unicode_w", "q", "SELECT REGEXP_CONTAINS('café', r'^\\w+$') AS v", [["false"]]),
    ("regexp_contains_digit", "q", "SELECT REGEXP_CONTAINS('a9', r'\\d') AS v", [["true"]]),
    # 3 strings
    ("substr_negative", "q", "SELECT SUBSTR('hello', -2) AS v", [["lo"]]),
    ("substr_zero", "q", "SELECT SUBSTR('hello', 0) AS v", [["hello"]]),
    ("length_unicode", "q", "SELECT LENGTH('café') AS v", [["4"]]),
    ("trim_chars", "q", "SELECT TRIM('xxabcxx', 'x') AS v", [["abc"]]),
    ("lower_nonascii", "q", "SELECT LOWER('CAFÉ') AS v", [["café"]]),
    ("concat_null", "q", "SELECT CONCAT('a', CAST(NULL AS STRING), 'b') AS v", [[""]]),  # || propagates NULL like BQ
    # 4 errors vs NULL -- SAFE_OFFSET returns NULL, matching BigQuery
    ("array_safe_offset_oob", "q", "SELECT CAST([1,2][SAFE_OFFSET(5)] AS STRING) AS v", [[""]]),
    # 5 script: the binder correctly leaves a variable name inside a string literal alone
    ("var_in_string_literal", "s", "DECLARE x INT64 DEFAULT 5; SELECT 'x marks x' AS v", [["x marks x"]]),
    # 6 struct / select-star modifiers
    ("struct_field_access", "q", "SELECT STRUCT(1 AS a, 2 AS b).a AS v", [["1"]]),
    ("array_struct_unnest_dotted", "q",
     "SELECT s.a AS v FROM UNNEST([STRUCT(1 AS a), STRUCT(2 AS a)]) AS s ORDER BY v", [["1"], ["2"]]),
    ("select_star_except", "q", "SELECT * EXCEPT(b) FROM (SELECT 1 AS a, 2 AS b, 3 AS c)", [["1", "3"]]),
    ("select_star_replace", "q", "SELECT * REPLACE(a * 10 AS a) FROM (SELECT 1 AS a, 2 AS b)", [["10", "2"]]),
]


@pytest.mark.parametrize("cid,mode,sql,native", AGREE, ids=[c[0] for c in AGREE])
def test_translation_agrees_with_native(cid, mode, sql, native):
    duck_sql = _translate_script(sql) if mode == "s" else _translate_query(sql)
    assert _duckdb_rows(duck_sql) == _native(native)


# ---- Unsupported constructs must surface as not_run (Untranslatable) ---------------------------
NOT_RUN = [
    ("regexp_extract_group", "SELECT REGEXP_EXTRACT('a123b', r'([0-9]+)') AS v"),
    ("regexp_replace_backref", "SELECT REGEXP_REPLACE('2024-01', r'(\\d+)-(\\d+)', r'\\2/\\1') AS v"),
    ("regexp_extract_lazy", "SELECT REGEXP_EXTRACT('<a><b>', r'<(.+?)>') AS v"),
    ("byte_length_unicode", "SELECT BYTE_LENGTH('café') AS v"),
    ("split_empty_delim", "SELECT ARRAY_LENGTH(SPLIT('abc', '')) AS v"),
]


@pytest.mark.parametrize("cid,sql", NOT_RUN, ids=[c[0] for c in NOT_RUN])
def test_unsupported_construct_is_not_run(cid, sql):
    with pytest.raises(Untranslatable):
        T.translate(sql, "bigquery")


# ---- Script control flow the binder must NOT pretend to handle: it declines (bound_sql is None).
# BigQuery runs these imperatively; sqlgami cannot, so it must not emit a single bound query.
CONTROL_FLOW = [
    ("if_in_script", "DECLARE x INT64 DEFAULT 1; IF x = 1 THEN SET x = 2; END IF; SELECT x AS v"),
    ("while_loop", "DECLARE i INT64 DEFAULT 0; WHILE i < 3 DO SET i = i + 1; END WHILE; SELECT i AS v"),
    ("set_inside_if", "DECLARE x INT64 DEFAULT 5; IF true THEN SET x = 9; END IF; SELECT x AS v"),
]


@pytest.mark.parametrize("cid,sql", CONTROL_FLOW, ids=[c[0] for c in CONTROL_FLOW])
def test_control_flow_script_is_not_bound(cid, sql):
    a = A.analyse(sql, None, "bigquery")
    # not_run: no single sendable bound query (binder does not pretend to execute control flow)
    assert a.bound_sql is None
    assert not (a.kind == A.QUERY and not a.unsafe_functions and not a.unlisted_functions)


def test_cast_failure_is_rejected_by_both_engines():
    """BigQuery raises on CAST('x' AS INT64); the DuckDB translation also raises -> consistent."""
    with pytest.raises(Exception):
        _duckdb_rows(_translate_query("SELECT CAST('x' AS INT64) AS v"))


# ---- DISAGREE: accepted/bound, yet the DuckDB result diverges from BigQuery (defects) ----------
@pytest.mark.xfail(strict=True, reason="HIGH: BigQuery raises on 1/0; the DuckDB translation returns "
                                       "inf instead of erroring -- silent wrong value.")
def test_division_by_zero_diverges():
    # BigQuery errors; faithful behaviour is to error, not to yield a value.
    with pytest.raises(Exception):
        _duckdb_rows(_translate_query("SELECT 1 / 0 AS v"))


@pytest.mark.xfail(strict=True, reason="MEDIUM-HIGH: BigQuery raises on OFFSET out of range; the DuckDB "
                                       "translation returns NULL instead of erroring.")
def test_array_offset_out_of_range_diverges():
    with pytest.raises(Exception):
        _duckdb_rows(_translate_query("SELECT [1,2][OFFSET(5)] AS v"))


@pytest.mark.xfail(strict=True, reason="HIGH: a column shadows a same-named script variable; BigQuery "
                                       "uses the column (1,2) but the binder substitutes the variable "
                                       "value (99) into the SELECT -- silent wrong binding.")
def test_variable_shadowed_by_column_binds_wrong():
    duck_sql = _translate_script(
        "DECLARE v INT64 DEFAULT 99; SELECT v FROM UNNEST([1,2]) AS v ORDER BY v")
    assert _duckdb_rows(duck_sql) == _native([["1"], ["2"]])


@pytest.mark.xfail(strict=True, reason="MEDIUM: a DECLARE of a STRUCT is neither bound nor declined; the "
                                       "binder leaves an unresolved reference (s.a) and emits broken SQL.")
def test_declare_struct_neither_binds_nor_not_run():
    duck_sql = _translate_script("DECLARE s STRUCT<a INT64> DEFAULT STRUCT(1 AS a); SELECT s.a AS v")
    assert _duckdb_rows(duck_sql) == _native([["1"]])


# ---- opt-in: re-derive query-mode native values (non-error, non-script) -----------------------
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
    return [tuple(c.lower() if c in ("True", "False") else c for c in r) for r in rows[1:]]


_QUERY_AGREE = [(c[0], c[2], c[3]) for c in AGREE if c[1] == "q"]


@pytest.mark.skipif(not _bq_available(), reason="BigQuery sandbox / SQLGAMI_BQ_PROJECT=sqlgami-dev-1109 not set")
@pytest.mark.parametrize("cid,sql,native", _QUERY_AGREE, ids=[c[0] for c in _QUERY_AGREE])
def test_native_values_still_hold(cid, sql, native):
    assert _bq_rows(sql) == _native(native)
