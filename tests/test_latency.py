"""The static path has a latency budget (ADR 0002). This guard runs production-shape SQL
stretched to 40-80 lines through ``explain()`` and fails when p90 exceeds the budget, with a
tolerance for slower CI machines pinned in one place."""

from __future__ import annotations

import time

import pytest

from benchkit.latency import percentiles, stretch
from conftest import SHOP_DDL
from sqlgami import Catalog, RequiredPredicate, Scorer

STATIC_P90_BUDGET_MS = 25.0  # the target of ADR 0002 §2 on a developer machine
CI_TOLERANCE = 2.5  # CI runners are slower and noisier; the guard is budget × tolerance

PROD_DDL = """
CREATE TABLE emp_snapshot_fact (snap_dt DATE, period_end_dt DATE, emp_id INTEGER, hire_dt DATE, term_dt DATE, status_cd TEXT,
  invol_ind INTEGER, new_hire_ind INTEGER, exit_filt_ind INTEGER, turn_filt_ind INTEGER, sor TEXT, segment TEXT, org_type TEXT,
  job_grp TEXT, lvl_2_id INTEGER, lvl_3_id INTEGER, lvl_4_id INTEGER, lvl_5_id INTEGER, lvl_6_id INTEGER, lvl_7_id INTEGER,
  lvl_2_id_fn INTEGER, lvl_3_id_fn INTEGER, lvl_4_id_fn INTEGER, lvl_5_id_fn INTEGER, lvl_6_id_fn INTEGER, lvl_7_id_fn INTEGER,
  PRIMARY KEY (emp_id, period_end_dt, snap_dt));
CREATE TABLE period_dim (ppe DATE PRIMARY KEY, freq TEXT, cur_flag INTEGER);
"""
CHAIN = " OR ".join([f"f.lvl_{i}_id = {{mgr_id}}" for i in range(2, 8)] + [f"f.lvl_{i}_id_fn = {{mgr_id}}" for i in range(2, 8)])
PROD_SQL = f"""SELECT segment
, ROUND(SUM(t90) * 100.0 / NULLIF(SUM(hires), 0), 2) AS new_hire_90_day_turnover
, ROUND(SUM(t180) * 100.0 / NULLIF(SUM(hires), 0), 2) AS turnover_180_day
FROM (
  SELECT f.period_end_dt, segment,
    SUM(CASE WHEN exit_filt_ind = 1 AND DATE_DIFF('day', hire_dt, term_dt) <= 90 THEN 1 ELSE 0 END) AS t90,
    SUM(CASE WHEN exit_filt_ind = 1 AND DATE_DIFF('day', hire_dt, term_dt) <= 180 THEN 1 ELSE 0 END) AS t180,
    SUM(new_hire_ind) AS hires
  FROM emp_snapshot_fact f
  WHERE turn_filt_ind = 1 AND sor = 'US' AND segment IN ('Retail') AND f.org_type IS NOT NULL
    AND period_end_dt = snap_dt
    AND f.period_end_dt BETWEEN
      ((SELECT ppe FROM period_dim WHERE freq = 'B' AND cur_flag = 1) - INTERVAL 350 DAY)
      AND (SELECT ppe FROM period_dim WHERE freq = 'B' AND cur_flag = 1)
    AND ({CHAIN})
    AND f.job_grp IN ({{job_groups}})
  GROUP BY period_end_dt, segment
) x
GROUP BY segment"""
SHOP_SQL = [
    "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid' GROUP BY c.name ORDER BY spend DESC",
    "WITH t AS (SELECT customer_id, SUM(total) AS s FROM orders GROUP BY customer_id) SELECT c.name, t.s FROM customers c JOIN t ON t.customer_id = c.id",
    "SELECT o.id, o.total, COUNT(i.id) AS items FROM orders o LEFT JOIN items i ON i.order_id = o.id GROUP BY o.id, o.total",
]
QUESTION = "What is the 90-day and 180-day new hire turnover percentage by segment for my organisation over the last year?"


def _samples() -> list[float]:
    prod = Scorer(
        "duckdb",
        catalog=Catalog.from_ddl(PROD_DDL, "duckdb"),
        snapshot_columns={"emp_snapshot_fact": "snap_dt"},
        required_predicates=[RequiredPredicate("leader_scope", tuple(f"lvl_{i}_id{s}" for s in ("", "_fn") for i in range(2, 8)), ("emp_snapshot_fact",), "or_chain")],
    )
    shop = Scorer("duckdb", catalog=SHOP_DDL)
    work = [(prod, stretch(PROD_SQL, (40, 80)), QUESTION, {"mgr_id": 7, "job_groups": ["A", "B"]})]
    work += [(shop, stretch(sql, (40, 80)), "Total order value per customer", None) for sql in SHOP_SQL]
    for scorer, sql, question, params in work:
        scorer.explain(sql, question, params=params)  # warm
    samples = []
    for _ in range(5):
        for scorer, sql, question, params in work:
            started = time.perf_counter()
            report = scorer.explain(sql, question, params=params)
            samples.append((time.perf_counter() - started) * 1000)
            assert report.stage("static").status.value == "passed", report.summary()
    return samples


def test_static_path_stays_within_budget():
    stats = percentiles(_samples())
    assert stats["p90"] <= STATIC_P90_BUDGET_MS * CI_TOLERANCE, stats


def test_stretch_keeps_the_statement_and_its_answer_shape():
    padded = stretch(SHOP_SQL[0], (40, 80))
    assert 40 <= padded.count("\n") + 1 <= 80 and padded.rstrip().endswith("ORDER BY spend DESC")
    assert stretch("WITH a AS (SELECT 1 AS x) SELECT x FROM a", (10, 12)).count("WITH") == 1
