"""Batch-6 red-team: false alarms found by scoring the real BigQuery GOLD (reference answers).

With the real dev gold (holdout w4/w4v/w4v2/w5), every BigQuery gold was scored statically with
its question text + params. Two HIGH false alarms fire on correct reference answers:

1. required_predicate (extends batch 5): the hierarchy_access or_chain matcher recognizes a
   literal OR-chain and an IN-(column list), but NOT an UNNEST-pivot scope -- gold q031 explodes
   the 12 level columns into one node_id via UNNEST([STRUCT(...lvl_x...)]) and filters
   x.node_id = manager. That is equivalent scoping, yet it is flagged HIGH (score 0.10).

2. term_rank (new): gold q025 answers a "top N ranks" question correctly with
   QUALIFY DENSE_RANK() OVER (ORDER BY active_n DESC) <= 3, but it also has an unrelated
   LIMIT 1 inside a current-period scalar subquery (period_end_dt = (SELECT ppe_dt ... LIMIT 1)).
   term_rank conflates that LIMIT with the ranking construct and reports "the query uses a plain
   row limit" -> HIGH (score 0.12). Remove the incidental LIMIT 1 and the HIGH disappears.

Measured gold scores (static, excluding 13 native-only control-flow scripts): n=26, median 0.536,
min 0.103, max 0.588 -- no gold reaches 'confident'; 5 golds score <= 0.4, all from the two
findings above (q025/a/b/c term_rank, q031 required_predicate).
Real-gold false-alarm rate on the 224 dev candidates: 1/63 correct-by-gold scored <= 0.4
(the q027 required_predicate IN-subquery case from batch 5).
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
                  terms=str(SUITE.terms_source("bigquery")), glossary=str(SUITE.path / "glossary.yaml"), **_CFG)


def _high(report):
    return [f.code for f in report.findings if f.severity.value in ("high", "error")]


_SCOPE_LITERAL = "(" + " OR ".join([f"eps.lvl_{i}_id = 12003" for i in range(2, 8)] + [f"eps.lvl_{i}_id_fn = 12003" for i in range(2, 8)]) + ")"
_FACT = "`acme-people-prod.hr.emp_period_snapshot` AS eps"


# --- required_predicate: UNNEST-pivot scope (the q031 gold idiom) -------------------------------
def _unnest_pivot_scope_sql() -> str:
    structs = ", ".join([f"STRUCT(eps.lvl_{i}_id AS node_id)" for i in range(2, 8)]
                        + [f"STRUCT(eps.lvl_{i}_id_fn AS node_id)" for i in range(2, 8)])
    return (f"SELECT COUNT(DISTINCT eps.assoc_id) AS n FROM {_FACT}, UNNEST([{structs}]) x "
            f"WHERE eps.status_cd = 'A' AND x.node_id = 12003")


def test_required_predicate_accepts_unnest_pivot_scope(scorer):
    report = scorer.score(_unnest_pivot_scope_sql(), None, mode="static")
    assert "required_predicate" not in _high(report)


def test_required_predicate_control_literal_or_chain(scorer):
    sql = f"SELECT COUNT(DISTINCT eps.assoc_id) AS n FROM {_FACT} WHERE eps.status_cd = 'A' AND {_SCOPE_LITERAL}"
    assert "required_predicate" not in _high(scorer.score(sql, None, mode="static"))


# --- term_rank: an incidental LIMIT 1 in a scalar subquery misread as the ranking construct -----
_TOPN_QUESTION = ("Which LVL_3 reporting nodes hold the most currently-active people? Return the top "
                  "three ranks by active headcount; ties at a rank are included. Fields: lvl_3_id, "
                  "active_n, headcount_rank. Sort by headcount_rank then lvl_3_id.")


def _dense_rank_topn(with_incidental_limit: bool) -> str:
    cur = (" eps.snap_dt = eps.period_end_dt AND eps.period_end_dt = (SELECT ppe_dt FROM "
           "`acme-people-prod.hr.pay_period_dim` WHERE freq_cd = 'B' AND is_current_flag = TRUE LIMIT 1) AND"
           if with_incidental_limit else "")
    per = (f"SELECT eps.lvl_3_id, COUNT(DISTINCT eps.assoc_id) AS active_n FROM {_FACT} "
           f"WHERE{cur} eps.status_cd = 'A' AND {_SCOPE_LITERAL} GROUP BY eps.lvl_3_id")
    return (f"WITH per_node AS ({per}) SELECT lvl_3_id, active_n, "
            "DENSE_RANK() OVER (ORDER BY active_n DESC) AS headcount_rank FROM per_node "
            "QUALIFY DENSE_RANK() OVER (ORDER BY active_n DESC) <= 3 ORDER BY headcount_rank, lvl_3_id")


def test_term_rank_control_dense_rank_without_incidental_limit(scorer):
    """A top-N with QUALIFY DENSE_RANK() and no stray LIMIT is correctly NOT flagged term_rank."""
    report = scorer.score(_dense_rank_topn(False), _TOPN_QUESTION, mode="static", params={"manager_id": 12003})
    assert "term_rank" not in _high(report)


def test_term_rank_not_misled_by_limit_in_scalar_subquery(scorer):
    report = scorer.score(_dense_rank_topn(True), _TOPN_QUESTION, mode="static", params={"manager_id": 12003})
    assert "term_rank" not in _high(report)
