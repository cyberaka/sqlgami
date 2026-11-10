"""Batch-5 red-team: FALSE ALARMS on correct BigQuery (static/term stage).

Scores correct, manager-scoped BigQuery against the hr_snapshot catalogue + terms with
Scorer('bigquery', mode='static'). A correct query scored <= 0.4 or flagged HIGH/ERROR is a
false alarm.

Finding (HIGH): the required-predicate check 'hierarchy_access' (match=or_chain over lvl_2_id..
lvl_7_id and the _fn levels) recognizes the scope when each level is matched with a literal
(lvl_2_id = 12003) but NOT when it is matched with an equivalent IN-subquery
(lvl_2_id IN (SELECT node_id FROM hierarchy_dim WHERE node_id = 12003 AND hierarchy_kind=...)).
Both scope identically; the IN-subquery form is idiomatic and appears in real dev candidates
(hr_snapshot-q027), yet it is flagged HIGH and the score collapses (0.52 -> 0.09). That is a
false alarm on a correct query.

The owner's other idioms (QUALIFY, UNNEST joins, ARRAY_AGG(STRUCT), SELECT * EXCEPT/REPLACE,
COUNTIF, COUNT(DISTINCT IF()), SAFE_DIVIDE, scalar-subquery date bounds, DECLARE/SET, view reads,
backticked dashed names) produce no HIGH false alarm -- guarded below.

False-alarm rate on the 224 dev candidates (correct-by-native-consensus, since dev gold is not in
the repo): 1/126 = 0.8% scored <= 0.4, all from this same required_predicate case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlgami import Scorer
from benchkit.suite import load_suite

SUITE = load_suite(Path(__file__).resolve().parents[1] / "bench" / "hr_snapshot")
_DDL = SUITE.schema_file("bigquery").read_text()
_CFG = SUITE.scorer_config("bigquery")
_CFG.pop("tenant_column", None)


@pytest.fixture(scope="module")
def scorer():
    return Scorer("bigquery", catalog=_DDL, mode="static",
                  terms=str(SUITE.terms_source("bigquery")), glossary=SUITE.path / "glossary.yaml", **_CFG)


_LEVELS = [f"lvl_{i}_id" for i in range(2, 8)]
_LEVELS_FN = [f"lvl_{i}_id_fn" for i in range(2, 8)]
_FACT = "`acme-people-prod.hr.emp_period_snapshot` AS e"


def _scope_literal() -> str:
    return "(" + " OR ".join([f"e.{c} = 12003" for c in _LEVELS + _LEVELS_FN]) + ")"


def _scope_in_subquery() -> str:
    def part(col, kind):
        return (f"e.{col} IN (SELECT node_id FROM `acme-people-prod.hr.hierarchy_dim` "
                f"WHERE node_id = 12003 AND hierarchy_kind = '{kind}')")
    return "(" + " OR ".join([part(c, "reporting") for c in _LEVELS] + [part(c, "functional") for c in _LEVELS_FN]) + ")"


def _high(report):
    return [f.code for f in report.findings if f.severity.value in ("high", "error")]


def test_required_predicate_recognizes_literal_scope(scorer):
    """Control: the literal OR-chain is recognized -- no required_predicate HIGH."""
    sql = f"SELECT COUNT(DISTINCT e.assoc_id) AS n FROM {_FACT} WHERE e.status_cd = 'A' AND {_scope_literal()}"
    assert "required_predicate" not in _high(scorer.score(sql, None, mode="static"))


def test_required_predicate_accepts_equivalent_in_subquery_scope(scorer):
    sql = f"SELECT COUNT(DISTINCT e.assoc_id) AS n FROM {_FACT} WHERE e.status_cd = 'A' AND {_scope_in_subquery()}"
    report = scorer.score(sql, None, mode="static")
    assert "required_predicate" not in _high(report) and report.score > 0.4


# --- the owner's idioms must not raise a HIGH false alarm on correct, scoped queries (guards) ---
_SCOPE = "(" + " OR ".join([f"e.lvl_{i}_id = 12003" for i in range(2, 8)] + [f"e.lvl_{i}_id_fn = 12003" for i in range(2, 8)]) + ")"
_CUR = ("e.period_end_dt = (SELECT ppe_dt FROM `acme-people-prod.hr.pay_period_dim` "
        "WHERE is_current_flag = TRUE AND freq_cd = 'B') AND e.snap_dt = e.period_end_dt")

IDIOMS = {
    "qualify_dedup": f"SELECT e.assoc_id FROM {_FACT} WHERE {_SCOPE} QUALIFY ROW_NUMBER() OVER (PARTITION BY e.assoc_id ORDER BY e.snap_dt DESC) = 1",
    "countif": f"SELECT COUNTIF(e.status_cd = 'A') AS n FROM {_FACT} WHERE {_CUR} AND {_SCOPE}",
    "count_distinct_if": f"SELECT COUNT(DISTINCT IF(e.status_cd = 'A', e.assoc_id, NULL)) AS n FROM {_FACT} WHERE {_CUR} AND {_SCOPE}",
    "safe_divide": f"SELECT SAFE_DIVIDE(COUNTIF(e.status_cd = 'A'), COUNT(*)) AS r FROM {_FACT} WHERE {_CUR} AND {_SCOPE}",
    "select_star_except": f"SELECT * EXCEPT(snap_dt) FROM {_FACT} WHERE {_CUR} AND {_SCOPE}",
    "select_star_replace": f"SELECT * REPLACE(UPPER(status_cd) AS status_cd) FROM {_FACT} WHERE {_CUR} AND {_SCOPE}",
    "cross_join_unnest": f"SELECT e.assoc_id, lv FROM {_FACT} CROSS JOIN UNNEST([e.lvl_2_id, e.lvl_3_id, e.lvl_4_id]) AS lv WHERE {_CUR} AND {_SCOPE}",
    "left_join_unnest_on_true": f"SELECT e.assoc_id, lv FROM {_FACT} LEFT JOIN UNNEST([e.lvl_2_id, e.lvl_3_id]) AS lv ON TRUE WHERE {_CUR} AND {_SCOPE}",
    "array_agg_struct": f"SELECT e.lvl_2_id, ARRAY_AGG(STRUCT(e.assoc_id, e.status_cd)) AS members FROM {_FACT} WHERE {_CUR} AND {_SCOPE} GROUP BY e.lvl_2_id",
    "declare_set_script": f"DECLARE cur DATE DEFAULT (SELECT ppe_dt FROM `acme-people-prod.hr.pay_period_dim` WHERE is_current_flag = TRUE AND freq_cd='B'); SELECT COUNTIF(e.status_cd='A') AS n FROM {_FACT} WHERE e.period_end_dt = cur AND e.snap_dt = e.period_end_dt AND {_SCOPE}",
    "view_read_scoped": f"SELECT COUNT(DISTINCT e.assoc_id) AS n FROM `acme-people-prod.hr.current_snapshot_v` AS e WHERE e.status_cd='A' AND {_SCOPE}",
}


@pytest.mark.parametrize("name,sql", list(IDIOMS.items()), ids=list(IDIOMS))
def test_owner_idiom_raises_no_high_false_alarm(name, sql, scorer):
    report = scorer.score(sql, None, mode="static")
    assert not _high(report), (name, _high(report), report.score)
