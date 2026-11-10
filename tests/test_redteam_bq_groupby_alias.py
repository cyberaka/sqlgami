"""Batch-4A finding: GROUP BY on a name that is both a SELECT alias and an in-scope column.

Found by the whole-query differential over 224 real LLM BigQuery candidates (native sandbox vs
sqlgami.translate / script-binder -> DuckDB, 3 seeds). The translation layer proved sound: every
plain candidate and every script *value* that sqlgami gives a verdict on agreed with native
BigQuery (~492 comparisons). The one real defect was 24 candidates (hr_snapshot-q022 family, both
script generators, all 3 seeds) whose translated DuckDB SQL raised a BinderException while
BigQuery ran fine.

Root cause (two parts):
1. BigQuery and DuckDB resolve ``GROUP BY <name>`` differently when <name> is BOTH a SELECT
   alias and a column in scope: BigQuery binds the alias; DuckDB binds the column. So
   ``SELECT a.v AS k ... CROSS JOIN (SELECT 2 AS k) b GROUP BY k`` returns a row on BigQuery but
   errors on DuckDB (``v`` not grouped). sqlgami's translation passes the GROUP BY through
   verbatim, so it inherits the divergence.
2. sqlgami's SCRIPT BINDER amplifies it: for q022 it rewrote the unambiguous
   ``GROUP BY pp.ppe_dt`` into ``GROUP BY period_end_dt`` (the SELECT alias), and
   ``period_end_dt`` also names a column of the joined emp_period_snapshot -- turning a query that
   would have translated cleanly into the ambiguous, DuckDB-failing form. (The plain,
   non-script version of the same query keeps ``pp.ppe_dt`` and agrees with BigQuery.)

The test below is the hermetic core (part 1). Fix direction: translate a GROUP BY alias-reference
to an unambiguous form (the underlying expression, or an ordinal) so DuckDB groups as BigQuery
does; and have the binder not rewrite a GROUP BY column into a colliding alias.
"""

from __future__ import annotations

import duckdb
import pytest

from sqlgami import analysis as A
from sqlgami import translate as T


def _duckdb_rows(sql: str):
    con = duckdb.connect(":memory:")
    try:
        return [tuple("" if c is None else str(c) for c in r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()


# BigQuery resolves `GROUP BY k` to the SELECT alias (a.v) and returns one row: k=1, n=1
# (confirmed natively on the sandbox). DuckDB resolves `k` to column b.k and errors.
_BQ = "SELECT a.v AS k, COUNT(*) AS n FROM (SELECT 1 AS v) a CROSS JOIN (SELECT 2 AS k) b GROUP BY k"


@pytest.mark.xfail(strict=True, reason="HIGH: GROUP BY names a SELECT alias that collides with an "
                                       "in-scope column; BigQuery groups by the alias (returns 1,1) but "
                                       "the DuckDB translation groups by the column and errors.")
def test_group_by_alias_colliding_with_column_matches_bigquery():
    duck_sql = T.translate(_BQ, "bigquery").sql
    assert _duckdb_rows(duck_sql) == [("1", "1")]


def test_plain_translation_is_sound_when_group_by_is_unambiguous():
    """Control: when the GROUP BY names the column unambiguously, the translation runs and matches
    BigQuery. (This is why the plain, non-script candidates all agreed.)"""
    bq = "SELECT a.v AS k, COUNT(*) AS n FROM (SELECT 1 AS v) a CROSS JOIN (SELECT 2 AS c) b GROUP BY a.v"
    assert _duckdb_rows(T.translate(bq, "bigquery").sql) == [("1", "1")]


def test_script_binder_rewrites_group_by_column_to_colliding_alias():
    """The binder amplification (hermetic: analyse only). sqlgami rewrites the unambiguous
    GROUP BY column into the SELECT alias, which on the real q022 schema collides with a joined
    column. This documents the mechanism that produced the 24 DuckDB errors."""
    script = (
        "DECLARE cur_seq INT64 DEFAULT (SELECT period_seq FROM `acme-people-prod.hr.pay_period_dim` "
        "WHERE freq_cd = 'B' AND is_current_flag = TRUE);\n"
        "SELECT pp.period_seq, pp.ppe_dt AS period_end_dt, "
        "COUNT(DISTINCT IF(e.status_cd = 'A', e.assoc_id, NULL)) AS active_n\n"
        "FROM `acme-people-prod.hr.pay_period_dim` pp\n"
        "LEFT JOIN `acme-people-prod.hr.emp_period_snapshot` e ON e.period_end_dt = pp.ppe_dt\n"
        "WHERE pp.freq_cd = 'B' AND pp.period_seq BETWEEN cur_seq - 7 AND cur_seq\n"
        "GROUP BY pp.period_seq, pp.ppe_dt ORDER BY pp.period_seq"
    )
    a = A.analyse(script, None, "bigquery")
    assert a.bound_sql is not None
    group_by = a.bound_sql[a.bound_sql.upper().rindex("GROUP BY"):]
    # the binder emitted the alias (period_end_dt), not the original column pp.ppe_dt
    assert "period_end_dt" in group_by and "ppe_dt" not in group_by, group_by
