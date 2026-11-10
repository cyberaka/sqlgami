"""Grain reasoning on production shapes: lookups, CTE keys, filters applied upstream.

Each rule here is tested in both directions: the shape that must stay quiet, and the
neighbouring shape that must still be caught.
"""

from __future__ import annotations

import pytest

from sqlgami import Scorer
from sqlgami.report import Severity

from conftest import SHOP_DDL


@pytest.fixture(params=["sqlite", "duckdb", "postgres"])
def dialect(request) -> str:
    return request.param


def _codes(report, at_least=Severity.MEDIUM):
    return sorted({f.code for f in report.findings_at_least(at_least)})


def _finding(report, code):
    return next((f for f in report.findings if f.code == code), None)


# ------------------------------------------------------------------ fan_out: derived lookups


def test_lookup_cte_unique_on_the_data_is_dismissed_by_a_probe(scorer):
    # profiles.customer_id has no declared key; one profile per customer in the data.
    sql = (
        "WITH prof AS (SELECT customer_id, bio FROM profiles) "
        "SELECT SUM(o.total) FROM orders o JOIN prof ON prof.customer_id = o.customer_id"
    )
    static = scorer.explain(sql)
    assert "fan_out" in _codes(static, Severity.LOW)  # statically unknown, so it is raised...
    sampled = scorer.sample(sql)
    finding = _finding(sampled, "fan_out")
    # ...and the data has no duplicates. Three rows prove little, so it stays a low note that says so.
    assert finding is not None and finding.severity == Severity.LOW
    assert finding.evidence.get("data_check") == "unique_observed" and finding.evidence.get("rows_probed") == 3
    assert "too few" in finding.message


def test_observed_uniqueness_over_enough_rows_is_a_low_note(engine, db):
    # A lookup with 200 rows, unique in the data, no key declared: LOW, with the count, not INFO.
    db.execute("CREATE TABLE tags (customer_id INTEGER, tag TEXT)")
    db.executemany("INSERT INTO tags VALUES (?, ?)", [(i, f"t{i}") for i in range(200)]) if engine == "sqlite" else [
        db.execute(f"INSERT INTO tags VALUES ({i}, 't{i}')") for i in range(200)
    ]
    if hasattr(db, "commit"):
        db.commit()
    with Scorer(engine, db=db) as scorer:
        report = scorer.sample("SELECT SUM(o.total) FROM orders o JOIN tags t ON t.customer_id = o.customer_id")
    finding = _finding(report, "fan_out")
    assert finding is not None and finding.severity == Severity.LOW
    assert finding.evidence["data_check"] == "unique_observed" and finding.evidence["rows_probed"] == 200
    assert "No key guarantees" in finding.message


def test_lookup_cte_with_duplicates_on_the_data_is_confirmed(scorer):
    # payments has several rows per order: a "lookup" on order_id repeats the orders.
    sql = (
        "WITH pay AS (SELECT order_id, amount FROM payments) "
        "SELECT SUM(o.total) FROM orders o JOIN pay ON pay.order_id = o.id"
    )
    sampled = scorer.sample(sql)
    finding = _finding(sampled, "fan_out")
    assert finding is not None and finding.severity == Severity.HIGH
    assert finding.evidence.get("data_check") == "duplicates"


def test_count_star_is_safe_once_one_table_is_not_repeated(scorer):
    # COUNT(*) over orders joined to a per-customer lookup counts orders, however many
    # orders a customer has. Only the lookup has to be unique.
    sql = (
        "WITH prof AS (SELECT customer_id, bio FROM profiles) "
        "SELECT prof.bio, COUNT(*) FROM orders o JOIN prof ON prof.customer_id = o.customer_id GROUP BY prof.bio"
    )
    sampled = scorer.sample(sql)
    assert "fan_out" not in _codes(sampled)


def test_join_on_an_expression_binds_the_column(dialect):
    # tm has one row per (customer_id, m); joining on customer_id and m = <expression of c>
    # picks at most one tm row per c row, twice over.
    sql = (
        "WITH tm AS (SELECT customer_id, EXTRACT(month FROM created_at) AS m, SUM(total) AS t "
        "FROM orders GROUP BY customer_id, EXTRACT(month FROM created_at)), "
        "c AS (SELECT customer_id, MIN(EXTRACT(month FROM created_at)) AS first_m FROM orders GROUP BY customer_id) "
        "SELECT c.first_m, COUNT(*) AS n, COUNT(t1.customer_id) AS n1, SUM(t2.t) AS s2 FROM c "
        "LEFT JOIN tm t1 ON t1.customer_id = c.customer_id AND t1.m = c.first_m + 1 "
        "LEFT JOIN tm t2 ON t2.customer_id = c.customer_id AND t2.m = c.first_m + 2 "
        "GROUP BY c.first_m"
    )
    report = Scorer(dialect, catalog=SHOP_DDL).explain(sql)
    assert "fan_out" not in _codes(report, Severity.LOW), report.summary()


def test_join_without_the_second_key_column_still_fans_out(dialect):
    # Joined on customer_id alone, tm has several rows per c row, so c's values are repeated.
    sql = (
        "WITH tm AS (SELECT customer_id, EXTRACT(month FROM created_at) AS m, SUM(total) AS t "
        "FROM orders GROUP BY customer_id, EXTRACT(month FROM created_at)), "
        "c AS (SELECT customer_id, MIN(EXTRACT(month FROM created_at)) AS first_m, COUNT(*) AS n FROM orders GROUP BY customer_id) "
        "SELECT c.first_m, SUM(c.n) FROM c LEFT JOIN tm t1 ON t1.customer_id = c.customer_id GROUP BY c.first_m"
    )
    report = Scorer(dialect, catalog=SHOP_DDL).explain(sql)
    assert "fan_out" in _codes(report), report.summary()


# --------------------------------------------------------------- fan_out: inside a CTE


def test_rate_looked_up_per_row_inside_a_cte_is_not_a_fan_out(dialect):
    # Each item carries its order's... no: each order carries its customer's country; a value
    # applied once per row of the CTE's own grain, and the aggregate also reads a grain column.
    sql = (
        "WITH x AS (SELECT o.id, o.total, c.country FROM orders o JOIN customers c ON c.id = o.customer_id) "
        "SELECT country, SUM(total) FROM x GROUP BY country"
    )
    assert "fan_out" not in _codes(Scorer(dialect, catalog=SHOP_DDL).explain(sql), Severity.LOW)


def test_parent_total_summed_per_child_row_is_still_a_fan_out(dialect):
    # The CTE's rows have no key of their own (a range join), and total is a parent's own figure,
    # not a per-row rate: summing it once per payment is a fan-out.
    sql = (
        "WITH x AS (SELECT p.id AS pay_id, p.amount, o.total FROM payments p JOIN orders o ON p.paid_at >= o.created_at) "
        "SELECT SUM(total) FROM x"
    )
    report = Scorer(dialect, catalog=SHOP_DDL).explain(sql)
    assert "fan_out" in _codes(report), report.summary()


def test_price_times_quantity_inside_a_cte_is_fine(dialect):
    sql = (
        "WITH x AS (SELECT i.id, i.qty, o.total AS order_total, i.price FROM items i JOIN orders o ON o.id = i.order_id) "
        "SELECT SUM(price * qty) FROM x"
    )
    assert "fan_out" not in _codes(Scorer(dialect, catalog=SHOP_DDL).explain(sql), Severity.LOW)


# ----------------------------------------------------- soft_deleted_rows: lookups, upstream filters

LIVE_DDL = SHOP_DDL


def _policy_scorer(dialect, question=None):
    return Scorer(dialect, catalog=LIVE_DDL, soft_delete_column="deleted_at", soft_delete_policy="required")


def test_table_filtered_upstream_is_anchored_through_the_cte_key(dialect):
    # rev only holds live customers' totals; joining customers back on its key adds no deleted ones.
    sql = (
        "WITH rev AS (SELECT o.customer_id, SUM(o.total) AS t FROM orders o "
        "JOIN customers c ON c.id = o.customer_id AND c.deleted_at IS NULL GROUP BY o.customer_id) "
        "SELECT r.customer_id, c.name, r.t FROM rev r JOIN customers c ON c.id = r.customer_id"
    )
    assert "soft_deleted_rows" not in _codes(_policy_scorer(dialect).explain(sql), Severity.LOW)


def test_cte_consumed_through_a_filtered_table_is_anchored(dialect):
    sql = (
        "WITH cc AS (SELECT c.id, c.country FROM customers c) "
        "SELECT c.id, c.name, cc.country FROM customers c JOIN cc ON cc.id = c.id WHERE c.deleted_at IS NULL"
    )
    assert "soft_deleted_rows" not in _codes(_policy_scorer(dialect).explain(sql), Severity.LOW)


def test_lookup_only_use_is_a_note_not_a_violation(dialect):
    # customers is only used to classify orders by country; its rows are not listed or counted.
    sql = (
        "WITH cc AS (SELECT c.id, c.country FROM customers c) "
        "SELECT cc.country, SUM(o.total) FROM orders o JOIN cc ON cc.id = o.customer_id GROUP BY cc.country"
    )
    report = _policy_scorer(dialect).explain(sql)
    finding = _finding(report, "soft_deleted_rows")
    assert finding is not None and finding.severity == Severity.LOW and finding.evidence["lookup"] is True


def test_lookup_that_lists_rows_per_entity_stays_a_violation(dialect):
    # Grouping by the lookup's key lists customers: a deleted customer in the output is the classic bug.
    sql = (
        "WITH cc AS (SELECT c.id, c.country FROM customers c) "
        "SELECT cc.id, SUM(o.total) FROM orders o JOIN cc ON cc.id = o.customer_id GROUP BY cc.id"
    )
    report = _policy_scorer(dialect).explain(sql)
    finding = _finding(report, "soft_deleted_rows")
    assert finding is not None and finding.severity == Severity.HIGH


def test_lookup_the_question_asks_to_filter_is_at_least_medium(dialect):
    sql = (
        "WITH cc AS (SELECT c.id, c.country FROM customers c) "
        "SELECT cc.country, SUM(o.total) FROM orders o JOIN cc ON cc.id = o.customer_id GROUP BY cc.country"
    )
    report = _policy_scorer(dialect).explain(sql, "Total order value by country of non-deleted customers")
    finding = _finding(report, "soft_deleted_rows")
    assert finding is not None and finding.severity.rank >= Severity.MEDIUM.rank


def test_plain_unfiltered_table_at_the_grain_is_still_high(dialect):
    report = _policy_scorer(dialect).explain("SELECT c.id, c.name FROM customers c")
    assert _finding(report, "soft_deleted_rows").severity == Severity.HIGH


def test_join_grain_probe_sees_filters_on_other_tables():
    """Whether a join repeats a person depends on how many periods are current: a filter on
    another table, which static reasoning cannot weigh. The probe runs the query's own FROM and
    WHERE and asks whether any person row comes back twice; its answer decides either way."""
    import duckdb

    conn = duckdb.connect()
    conn.execute("CREATE TABLE people (assoc_id INTEGER PRIMARY KEY, salary INTEGER)")
    conn.execute("CREATE TABLE snap (assoc_id INTEGER, period_end_dt DATE, PRIMARY KEY (assoc_id, period_end_dt))")
    conn.execute("CREATE TABLE periods (ppe_dt DATE PRIMARY KEY, is_current BOOLEAN)")
    conn.execute("INSERT INTO people SELECT i, 100 + i FROM range(30) r(i)")
    conn.execute("INSERT INTO snap SELECT i % 30, DATE '2024-01-01' + INTERVAL (14 * (i // 30)) DAY FROM range(300) r(i)")
    conn.execute("INSERT INTO periods SELECT DATE '2024-01-01' + INTERVAL (14 * i) DAY, i = 9 FROM range(10) r(i)")
    sql = (
        "SELECT SUM(p.salary) FROM people p JOIN snap s ON s.assoc_id = p.assoc_id "
        "JOIN periods pp ON pp.ppe_dt = s.period_end_dt WHERE pp.is_current"
    )
    with Scorer("duckdb", db=conn) as scorer:
        static = scorer.explain(sql, "total salary of current people")
        assert next(f for f in static.findings if f.code == "fan_out").severity == Severity.HIGH
        one = scorer.sample(sql, "total salary of current people")
        finding = next(f for f in one.findings if f.code == "fan_out")
        assert finding.severity == Severity.LOW and finding.evidence["data_check"] == "unique_observed"
        conn.execute("UPDATE periods SET is_current = TRUE WHERE ppe_dt = DATE '2024-01-01'")
        two = scorer.sample(sql, "total salary of current people")
        finding = next(f for f in two.findings if f.code == "fan_out")
        assert finding.severity == Severity.HIGH and finding.evidence["data_check"] == "duplicates"


def test_probe_queries_carry_the_template_parameters():
    """A derived table filtered by {manager_id} is probed with the value filled in, not refused."""
    import duckdb

    conn = duckdb.connect()
    conn.execute("CREATE TABLE snap (assoc_id INTEGER, period_end_dt DATE, lvl_2_id INTEGER, PRIMARY KEY (assoc_id, period_end_dt))")
    conn.execute("CREATE TABLE people (assoc_id INTEGER PRIMARY KEY, salary INTEGER)")
    conn.execute("INSERT INTO people SELECT i, 100 FROM range(20) r(i)")
    conn.execute("INSERT INTO snap SELECT i % 20, DATE '2024-01-01' + INTERVAL (14 * (i // 20)) DAY, 7 FROM range(100) r(i)")
    sql = (
        "WITH org AS (SELECT DISTINCT assoc_id FROM snap WHERE lvl_2_id = {manager_id}) "
        "SELECT SUM(p.salary) FROM people p JOIN org o ON o.assoc_id = p.assoc_id"
    )
    with Scorer("duckdb", db=conn) as scorer:
        report = scorer.sample(sql, "salary of my organisation", params={"manager_id": 7})
    fan = [f for f in report.findings if f.code == "fan_out"]
    assert not fan or fan[0].severity == Severity.LOW, report.summary()
    assert not [c for c in report.checks_not_run if "parameter" in c.reason], report.checks_not_run
