"""Every statement sqlgami sends has a wall-clock bound the host can rely on, and one report
has a budget for all of them together. A statement the engine refuses to stop is abandoned."""

from __future__ import annotations

import time

import duckdb
import pytest

from sqlgami import Options, Scorer
from sqlgami.engines import duckdb as D
from sqlgami.report import StageStatus

SLOW = "SELECT SUM(a.i * b.i) FROM range(200000) a(i), range(200000) b(i)"  # tens of seconds


def _adapter():
    conn = duckdb.connect()
    conn.execute("CREATE TABLE t AS SELECT i AS id, i % 7 AS parent FROM range(2000) r(i)")
    return D.DuckDBAdapter(conn)


def test_duckdb_statement_stops_at_the_timeout_and_the_adapter_stays_usable():
    adapter = _adapter()
    started = time.perf_counter()
    result = adapter.execute(SLOW, max_rows=10, timeout_s=0.5)
    elapsed = time.perf_counter() - started
    assert not result.ok and result.error_kind == "timeout"
    assert elapsed < 0.5 + adapter.GRACE_S
    assert adapter.execute("SELECT 1", max_rows=10, timeout_s=5).rows == [(1,)]
    assert not adapter._abandoned
    adapter.close()


def test_duckdb_statement_that_ignores_the_interrupt_is_abandoned(monkeypatch):
    """Simulate a lost interrupt: the caller still gets a timeout after the grace period, and the
    next statement runs on a fresh cursor while the old one is nagged in the background."""
    adapter = _adapter()
    monkeypatch.setattr(D, "_interrupt", lambda conn: None)
    monkeypatch.setattr(adapter, "GRACE_S", 0.5)
    started = time.perf_counter()
    result = adapter.execute(
        "SELECT SUM(a.i * b.i) FROM range(40000) a(i), range(40000) b(i)", max_rows=10, timeout_s=0.3
    )
    elapsed = time.perf_counter() - started
    assert not result.ok and result.error_kind == "timeout" and "abandoned" in (result.error or "")
    assert elapsed < 2.0
    assert len(adapter._abandoned) == 1
    assert adapter.execute("SELECT COUNT(*) FROM t", max_rows=10, timeout_s=5).rows == [(2000,)]
    adapter.close()


def test_non_terminating_recursive_cte_is_a_static_note_and_a_timeout_not_a_hang():
    """The exact shape an LLM wrote on the hr snapshot: a recursive CTE over a table that holds
    each key many times, grown with UNION ALL. It never finishes; the report must."""
    conn = duckdb.connect()
    conn.execute("CREATE SCHEMA hr")
    conn.execute(
        "CREATE TABLE hr.emp_period_snapshot (assoc_id INTEGER, period_end_dt DATE, snap_dt DATE, status_cd TEXT, "
        "lvl_2_id INTEGER, lvl_3_id INTEGER, lvl_4_id INTEGER, lvl_5_id INTEGER, lvl_6_id INTEGER, lvl_7_id INTEGER, "
        "lvl_2_id_fn INTEGER, lvl_3_id_fn INTEGER, lvl_4_id_fn INTEGER, lvl_5_id_fn INTEGER, lvl_6_id_fn INTEGER, "
        "lvl_7_id_fn INTEGER)"
    )
    conn.execute("CREATE TABLE hr.pay_period_dim (ppe_dt DATE, freq_cd TEXT, period_seq INTEGER, is_current_flag BOOLEAN)")
    conn.execute(
        "INSERT INTO hr.emp_period_snapshot SELECT 100 + (i % 40), DATE '2024-01-12' + INTERVAL (14 * (i // 40)) DAY, "
        "DATE '2024-01-12' + INTERVAL (14 * (i // 40)) DAY, 'A', 7, 100 + ((i + 1) % 40), 100 + ((i + 2) % 40), "
        "100 + ((i + 3) % 40), 100 + ((i + 4) % 40), 100 + ((i + 5) % 40), 7, 7, 7, 7, 7, 7 FROM range(1200) r(i)"
    )
    conn.execute(
        "INSERT INTO hr.pay_period_dim SELECT DATE '2024-01-12' + INTERVAL (14 * i) DAY, 'B', 1 + i, i = 29 FROM range(30) r(i)"
    )
    sql = """WITH RECURSIVE my_organisation AS (
  SELECT {manager_id} AS assoc_id
  UNION ALL
  SELECT eps.assoc_id
  FROM hr.emp_period_snapshot eps
  JOIN my_organisation m ON
    eps.lvl_2_id = m.assoc_id OR eps.lvl_3_id = m.assoc_id OR eps.lvl_4_id = m.assoc_id OR
    eps.lvl_5_id = m.assoc_id OR eps.lvl_6_id = m.assoc_id OR eps.lvl_7_id = m.assoc_id OR
    eps.lvl_2_id_fn = m.assoc_id OR eps.lvl_3_id_fn = m.assoc_id OR eps.lvl_4_id_fn = m.assoc_id OR
    eps.lvl_5_id_fn = m.assoc_id OR eps.lvl_6_id_fn = m.assoc_id OR eps.lvl_7_id_fn = m.assoc_id
)
SELECT COUNT(*) AS active_person_periods
FROM hr.emp_period_snapshot eps
JOIN my_organisation m ON eps.assoc_id = m.assoc_id
JOIN hr.pay_period_dim pp ON pp.ppe_dt = eps.period_end_dt AND pp.freq_cd = 'B'
WHERE eps.status_cd = 'A'
  AND eps.snap_dt = eps.period_end_dt
  AND pp.period_seq BETWEEN (SELECT period_seq - 25 FROM hr.pay_period_dim WHERE is_current_flag = TRUE AND freq_cd = 'B')
                        AND (SELECT period_seq FROM hr.pay_period_dim WHERE is_current_flag = TRUE AND freq_cd = 'B');"""
    options = Options(timeout_s=1.0, budget_s=8.0, consistency_budget_s=2.0)
    with Scorer("duckdb", db=conn, options=options) as scorer:
        started = time.perf_counter()
        report = scorer.score(sql, "active person-periods in my organisation over the last 26 pay periods",
                              mode="sample", params={"manager_id": 7})
        elapsed = time.perf_counter() - started
    assert elapsed < options.budget_s + D.DuckDBAdapter.GRACE_S + 2.0, elapsed
    assert report.stage("parse").status == StageStatus.PASSED  # {manager_id} as a projection parses
    assert report.has("unbounded_recursion")
    sample = report.stage("sample")
    assert sample is not None and sample.status != StageStatus.PASSED
    assert report.has("execution_timeout") or report.has("budget_spent")
    assert report.score <= 0.65  # a query that did not run is not confirmed by anything


def test_report_budget_cuts_database_work_short(engine, db):
    with Scorer(engine, db=db, options=Options(budget_s=0.0001)) as scorer:
        report = scorer.sample("SELECT COUNT(*) FROM orders", "how many orders")
    assert report.has("budget_spent")
    assert report.stage("sample").status != StageStatus.PASSED
    assert report.stage("dry_run").status != StageStatus.PASSED


def test_budget_untouched_when_there_is_time(scorer):
    report = scorer.sample("SELECT COUNT(*) FROM orders", "how many orders")
    assert not report.has("budget_spent")
    assert report.stage("sample").status == StageStatus.PASSED


@pytest.mark.parametrize(
    "sql,flagged",
    [
        ("WITH RECURSIVE o AS (SELECT 1 AS id UNION ALL SELECT t.id FROM t JOIN o ON t.parent = o.id) SELECT COUNT(*) FROM o", True),
        ("WITH RECURSIVE o AS (SELECT 1 AS id UNION SELECT t.id FROM t JOIN o ON t.parent = o.id) SELECT COUNT(*) FROM o", False),
        ("WITH RECURSIVE o AS (SELECT 1 AS id, 0 AS d UNION ALL SELECT t.id, o.d + 1 FROM t JOIN o ON t.parent = o.id WHERE o.d < 10) SELECT COUNT(*) FROM o", False),
        ("WITH o AS (SELECT id FROM t) SELECT COUNT(*) FROM o", False),
    ],
)
def test_unbounded_recursion_note(sql, flagged):
    with Scorer("duckdb", catalog="CREATE TABLE t (id INTEGER, parent INTEGER)") as scorer:
        report = scorer.explain(sql, "tree size")
    assert report.has("unbounded_recursion") is flagged
