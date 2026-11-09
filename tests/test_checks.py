"""Each static check, on SQL that should trip it and SQL that should not.

These run without a database: the catalog comes from DDL text, and every case
runs under the SQLite, DuckDB and Postgres dialects.
"""

from __future__ import annotations

import pytest

from sqlgami import Catalog, Scorer, Severity

from conftest import SHOP_DDL

DIALECTS = ("sqlite", "duckdb", "postgres")

# (name, sql, finding code expected) -- the check must fire.
POSITIVE = [
    (
        "fan_out_sum",
        "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id "
        "JOIN items i ON i.order_id = o.id GROUP BY c.name",
        "fan_out",
    ),
    (
        "fan_out_chasm_count",
        "SELECT o.id, COUNT(*) FROM orders o JOIN items i ON i.order_id = o.id "
        "JOIN payments p ON p.order_id = o.id GROUP BY o.id",
        "fan_out",
    ),
    (
        "fan_out_chasm_sum",
        "SELECT SUM(p.amount) FROM orders o JOIN items i ON i.order_id = o.id "
        "JOIN payments p ON p.order_id = o.id",
        "fan_out",
    ),
    (
        "fan_out_through_view",
        "SELECT SUM(o.total) FROM orders o JOIN order_totals t ON t.customer_id = o.customer_id",
        "fan_out",
    ),
    (
        "fan_out_in_cte",
        "WITH x AS (SELECT o.customer_id, SUM(o.total) AS s FROM orders o "
        "JOIN items i ON i.order_id = o.id GROUP BY o.customer_id) SELECT * FROM x",
        "fan_out",
    ),
    (
        "fan_out_avg",
        "SELECT AVG(o.total) FROM orders o JOIN payments p ON p.order_id = o.id",
        "fan_out",
    ),
    (
        "sum_distinct",
        "SELECT SUM(DISTINCT o.total) FROM orders o JOIN items i ON i.order_id = o.id",
        "distinct_masking",
    ),
    (
        "missing_group_by",
        "SELECT c.name, c.country, COUNT(*) FROM customers c GROUP BY c.country",
        "missing_group_by",
    ),
    (
        "bare_column_with_aggregate",
        "SELECT name, COUNT(*) FROM customers",
        "missing_group_by",
    ),
    (
        "outer_join_where",
        "SELECT c.name, COUNT(o.id) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
        "WHERE o.status = 'paid' GROUP BY c.name",
        "outer_join_nullified",
    ),
    (
        "outer_join_then_inner",
        "SELECT c.name, i.sku FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
        "JOIN items i ON i.order_id = o.id",
        "outer_join_nullified",
    ),
    (
        "right_join_where",
        "SELECT c.name FROM orders o RIGHT JOIN customers c ON o.customer_id = c.id WHERE o.total > 10",
        "outer_join_nullified",
    ),
    (
        "cartesian_comma",
        "SELECT c.name, o.total FROM customers c, orders o WHERE o.total > 100",
        "cartesian_join",
    ),
    (
        "cartesian_join_no_on",
        "SELECT c.name, o.total FROM customers c CROSS JOIN orders o JOIN items i ON i.qty = 1",
        "cartesian_join",
    ),
    ("eq_null", "SELECT * FROM customers WHERE deleted_at = NULL", "null_comparison"),
    ("neq_null", "SELECT * FROM customers WHERE deleted_at <> NULL", "null_comparison"),
    ("string_null", "SELECT * FROM customers WHERE country = 'NULL'", "null_comparison"),
    (
        "not_in_nullable",
        "SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers)",
        "not_in_nullable",
    ),
    (
        "not_in_null_literal",
        "SELECT name FROM customers WHERE id NOT IN (1, NULL)",
        "not_in_nullable",
    ),
    ("limit_no_order", "SELECT name FROM customers LIMIT 5", "limit_without_order"),
    (
        "limit_no_order_nested",
        "SELECT * FROM orders WHERE customer_id = (SELECT id FROM customers LIMIT 1)",
        "limit_without_order",
    ),
    ("avg_of_avg_view", "SELECT AVG(avg_total) FROM cust_avg", "avg_of_avg"),
    (
        "avg_of_avg_cte",
        "WITH per AS (SELECT customer_id, AVG(total) AS a FROM orders GROUP BY customer_id) "
        "SELECT AVG(a) FROM per",
        "avg_of_avg",
    ),
    (
        "avg_of_ratio",
        "WITH per AS (SELECT customer_id, SUM(total) / COUNT(*) AS r FROM orders GROUP BY customer_id) "
        "SELECT AVG(r) FROM per",
        "avg_of_avg",
    ),
    ("number_vs_text", "SELECT * FROM orders WHERE total = 'high'", "type_mismatch"),
    ("timestamp_vs_number", "SELECT * FROM orders WHERE created_at > 2023", "type_mismatch"),
    (
        "int_vs_text_join",
        "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.name",
        "type_mismatch",
    ),
    (
        "date_equals_timestamp",
        "SELECT * FROM orders o WHERE o.order_date = o.created_at",
        "type_mismatch",
    ),
    (
        "between_timestamp",
        "SELECT * FROM orders WHERE created_at BETWEEN '2024-01-01' AND '2024-01-31'",
        "date_boundary",
    ),
    (
        "lte_timestamp",
        "SELECT * FROM orders WHERE created_at >= '2024-01-01' AND created_at <= '2024-01-31'",
        "date_boundary",
    ),
    (
        "lt_last_day",
        "SELECT * FROM orders WHERE order_date >= '2024-01-01' AND order_date < '2024-01-31'",
        "date_boundary",
    ),
    ("gt_first_day", "SELECT * FROM orders WHERE order_date > '2024-01-01'", "date_boundary"),
    ("impossible_date", "SELECT * FROM orders WHERE order_date <= '2023-02-29'", "date_boundary"),
    (
        "between_first_to_first",
        "SELECT * FROM orders WHERE order_date BETWEEN '2024-01-01' AND '2024-02-01'",
        "date_boundary",
    ),
    (
        "natural_join",
        "SELECT * FROM orders NATURAL JOIN customers",
        "natural_join",
    ),
]

# (name, sql) -- correct SQL that must not produce any finding at medium severity or above.
NEGATIVE = [
    (
        "simple_group",
        "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name",
    ),
    (
        "aggregate_many_side",
        "SELECT o.id, SUM(i.qty * i.price) FROM orders o JOIN items i ON i.order_id = o.id GROUP BY o.id",
    ),
    (
        "mixed_grain_expression",
        "SELECT SUM(i.qty * o.total) FROM orders o JOIN items i ON i.order_id = o.id",
    ),
    (
        "pre_aggregated_view",
        "SELECT c.name, SUM(t.amount) FROM customers c JOIN order_totals t ON t.customer_id = c.id GROUP BY c.name",
    ),
    (
        "view_on_view",
        "SELECT c.name, b.spend FROM big_customers b JOIN customers c ON c.id = b.cid ORDER BY b.spend DESC",
    ),
    (
        "pre_aggregated_ctes",
        "WITH i AS (SELECT order_id, SUM(qty) q FROM items GROUP BY order_id), "
        "p AS (SELECT order_id, SUM(amount) a FROM payments GROUP BY order_id) "
        "SELECT o.id, i.q, p.a FROM orders o LEFT JOIN i ON i.order_id = o.id LEFT JOIN p ON p.order_id = o.id",
    ),
    (
        "sum_after_pre_aggregation",
        "WITH i AS (SELECT order_id, SUM(qty) q FROM items GROUP BY order_id), "
        "p AS (SELECT order_id, SUM(amount) a FROM payments GROUP BY order_id) "
        "SELECT SUM(o.total), SUM(i.q), SUM(p.a) FROM orders o LEFT JOIN i ON i.order_id = o.id "
        "LEFT JOIN p ON p.order_id = o.id",
    ),
    (
        "count_distinct",
        "SELECT COUNT(DISTINCT o.id) FROM orders o JOIN items i ON i.order_id = o.id",
    ),
    (
        "row_number_latest",
        "WITH r AS (SELECT p.order_id, p.amount, ROW_NUMBER() OVER (PARTITION BY p.order_id ORDER BY p.paid_at DESC) rn "
        "FROM payments p) SELECT SUM(o.total) FROM orders o JOIN r ON r.order_id = o.id WHERE r.rn = 1",
    ),
    (
        "min_max_over_join",
        "SELECT o.id, MAX(o.total), MIN(i.qty) FROM orders o JOIN items i ON i.order_id = o.id GROUP BY o.id",
    ),
    (
        "group_by_primary_key",
        "SELECT c.id, c.name, COUNT(o.id) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id",
    ),
    (
        "group_by_expression",
        "SELECT UPPER(country) AS k, COUNT(*) FROM customers GROUP BY UPPER(country)",
    ),
    (
        "group_by_position",
        "SELECT country, status, COUNT(*) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY 1, 2",
    ),
    (
        "anti_join",
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.id IS NULL",
    ),
    (
        "outer_join_filter_in_on",
        "SELECT c.name, COUNT(o.id) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
        "AND o.status = 'paid' GROUP BY c.name",
    ),
    (
        "outer_join_coalesce",
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE COALESCE(o.total, 0) < 10",
    ),
    (
        "outer_join_or_is_null",
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
        "WHERE o.status = 'paid' OR o.id IS NULL",
    ),
    ("comma_join_linked", "SELECT c.name, o.total FROM customers c, orders o WHERE o.customer_id = c.id"),
    (
        "cross_join_single_row",
        "SELECT o.id, o.total / t.grand FROM orders o CROSS JOIN (SELECT SUM(total) AS grand FROM orders) t",
    ),
    ("is_null", "SELECT * FROM customers WHERE deleted_at IS NULL"),
    (
        "not_in_not_null_column",
        "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)",
    ),
    (
        "not_in_guarded",
        "SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers WHERE referrer_id IS NOT NULL)",
    ),
    ("not_exists", "SELECT name FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)"),
    ("limit_with_order", "SELECT name FROM customers ORDER BY name LIMIT 5"),
    (
        "exists_with_limit",
        "SELECT c.name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id LIMIT 1)",
    ),
    ("overall_average", "SELECT AVG(total) FROM orders"),
    (
        "half_open_range",
        "SELECT * FROM orders WHERE created_at >= '2024-01-01' AND created_at < '2024-02-01'",
    ),
    ("date_between", "SELECT * FROM orders WHERE order_date BETWEEN '2024-01-01' AND '2024-01-31'"),
    ("numeric_string_literal", "SELECT * FROM orders WHERE total > '10'"),
    ("explicit_cast", "SELECT * FROM orders WHERE CAST(total AS TEXT) = 'high'"),
    (
        "window_running_total",
        "SELECT o.id, SUM(o.total) OVER (PARTITION BY o.customer_id ORDER BY o.created_at) FROM orders o",
    ),
    ("union_all", "SELECT id FROM customers UNION ALL SELECT customer_id FROM orders"),
    (
        "recursive_cte",
        "WITH RECURSIVE r(id, lvl) AS (SELECT id, 0 FROM customers WHERE referrer_id IS NULL "
        "UNION ALL SELECT c.id, r.lvl + 1 FROM customers c JOIN r ON c.referrer_id = r.id) "
        "SELECT lvl, COUNT(*) FROM r GROUP BY lvl",
    ),
    (
        "correlated_scalar_subquery",
        "SELECT c.name, (SELECT MAX(o.total) FROM orders o WHERE o.customer_id = c.id) AS top FROM customers c",
    ),
    (
        "in_subquery",
        "SELECT name FROM customers WHERE id IN (SELECT customer_id FROM orders WHERE total > 50)",
    ),
    (
        "case_aggregate",
        "SELECT status, SUM(CASE WHEN total > 50 THEN 1 ELSE 0 END) FROM orders GROUP BY status",
    ),
    (
        "having",
        "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id HAVING COUNT(*) > 1",
    ),
    (
        "self_join_on_key",
        "SELECT c.name, r.name FROM customers c LEFT JOIN customers r ON r.id = c.referrer_id",
    ),
]


def _scorer(dialect: str, **hints) -> Scorer:
    return Scorer(dialect, catalog=Catalog.from_ddl(SHOP_DDL, dialect), **hints)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("name,sql,code", POSITIVE, ids=[p[0] for p in POSITIVE])
def test_check_fires(dialect, name, sql, code):
    report = _scorer(dialect).explain(sql)
    assert report.stage("schema").status.value == "passed", report.summary()
    assert report.has(code), report.summary()
    assert not [c for c in report.checks_not_run if "internal error" in c.reason], report.summary()


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("name,sql", NEGATIVE, ids=[n[0] for n in NEGATIVE])
def test_correct_sql_raises_no_alarm(dialect, name, sql):
    report = _scorer(dialect).explain(sql)
    assert report.stage("schema").status.value == "passed", report.summary()
    assert not report.findings_at_least(Severity.MEDIUM), report.summary()
    assert not [c for c in report.checks_not_run if "internal error" in c.reason], report.summary()


def test_integer_division_only_where_the_engine_truncates():
    sql = "SELECT COUNT(o.id) / COUNT(*) FROM orders o"
    assert _scorer("sqlite").explain(sql).has("integer_division")
    assert _scorer("postgres").explain(sql).has("integer_division")
    assert not _scorer("duckdb").explain(sql).has("integer_division")
    assert not _scorer("sqlite").explain("SELECT COUNT(o.id) * 1.0 / COUNT(*) FROM orders o").has("integer_division")


def test_missing_group_by_wording_depends_on_engine():
    sql = "SELECT name, COUNT(*) FROM customers"
    sqlite = next(f for f in _scorer("sqlite").explain(sql).findings if f.code == "missing_group_by")
    postgres = next(f for f in _scorer("postgres").explain(sql).findings if f.code == "missing_group_by")
    assert "arbitrary row" in sqlite.message
    assert "rejects" in postgres.message


def test_explicit_cross_join_is_low_severity():
    report = _scorer("duckdb").explain("SELECT c.name, o.total FROM customers c CROSS JOIN orders o")
    finding = next(f for f in report.findings if f.code == "cartesian_join")
    assert finding.severity == Severity.LOW


def test_filter_value_uses_known_values():
    catalog = Catalog.from_ddl(SHOP_DDL, "duckdb")
    table = catalog.resolve("customers")
    catalog.column(table, "country").values = ("US", "DE", "IN")
    scorer = Scorer("duckdb", catalog=catalog)
    bad = scorer.explain("SELECT * FROM customers WHERE country = 'us'")
    finding = next(f for f in bad.findings if f.code == "filter_value")
    assert finding.severity == Severity.HIGH
    assert finding.evidence["nearest"] == ["US"]
    assert not scorer.explain("SELECT * FROM customers WHERE country = 'US'").has("filter_value")
    assert not scorer.explain("SELECT * FROM customers WHERE country IN ('US', 'DE')").has("filter_value")
    through_view = scorer.explain(
        "WITH c AS (SELECT id, country AS land FROM customers) SELECT * FROM c WHERE land = 'Germany'"
    )
    assert through_view.has("filter_value")


# ------------------------------------------------------------------ tenants

TENANT_OK = [
    "SELECT COUNT(*) FROM orders WHERE tenant_id = 7",
    "SELECT COUNT(*) FROM orders WHERE tenant_id IN (7, 8)",
    "SELECT c.name, o.total FROM customers c JOIN orders o ON o.customer_id = c.id "
    "AND o.tenant_id = c.tenant_id WHERE c.tenant_id = 7",
    "SELECT SUM(amount) FROM order_totals WHERE tenant_id = 7",
    "WITH x AS (SELECT tenant_id AS t, total FROM orders) SELECT SUM(total) FROM x WHERE t = 7",
    "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id AND o.tenant_id = 7 "
    "WHERE c.tenant_id = 7",
    "SELECT name FROM customers c WHERE c.tenant_id = 7 AND EXISTS "
    "(SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.tenant_id = c.tenant_id)",
    "SELECT id FROM customers WHERE tenant_id = 7 UNION ALL SELECT id FROM orders WHERE tenant_id = 7",
    "SELECT * FROM (SELECT id, tenant_id FROM customers UNION ALL SELECT id, tenant_id FROM orders) u "
    "WHERE u.tenant_id = 7",
]

TENANT_BAD = [
    ("SELECT COUNT(*) FROM orders", Severity.HIGH),
    ("SELECT SUM(amount) FROM order_totals", Severity.HIGH),
    ("SELECT COUNT(*) FROM orders WHERE tenant_id > 0", Severity.HIGH),
    (
        "SELECT c.name, o.total FROM customers c JOIN orders o ON o.customer_id = c.id WHERE c.tenant_id = 7",
        Severity.MEDIUM,
    ),
    (
        "SELECT name FROM customers c WHERE EXISTS "
        "(SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.tenant_id = c.tenant_id)",
        Severity.HIGH,
    ),
    (
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
        "AND c.tenant_id = 7 AND o.tenant_id = 7",
        Severity.MEDIUM,
    ),
    ("SELECT id FROM customers WHERE tenant_id = 7 UNION ALL SELECT id FROM orders", Severity.HIGH),
]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("sql", TENANT_OK)
def test_tenant_filter_present(dialect, sql):
    report = _scorer(dialect, tenant_column="tenant_id").explain(sql)
    assert not report.has("missing_tenant_filter"), report.summary()


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("sql,severity", TENANT_BAD)
def test_tenant_filter_missing(dialect, sql, severity):
    report = _scorer(dialect, tenant_column="tenant_id").explain(sql)
    found = [f for f in report.findings if f.code == "missing_tenant_filter"]
    assert found, report.summary()
    assert max(f.severity.rank for f in found) == severity.rank, report.summary()


def test_tenant_check_is_off_without_the_hint():
    assert not _scorer("duckdb").explain("SELECT COUNT(*) FROM orders").has("missing_tenant_filter")


def test_soft_delete():
    scorer = _scorer("duckdb", soft_delete_column="deleted_at")
    assert scorer.explain("SELECT COUNT(*) FROM customers").has("soft_deleted_rows")
    assert not scorer.explain("SELECT COUNT(*) FROM customers WHERE deleted_at IS NULL").has("soft_deleted_rows")
    assert not scorer.explain(
        "WITH live AS (SELECT * FROM customers WHERE deleted_at IS NULL) SELECT COUNT(*) FROM live"
    ).has("soft_deleted_rows")
    assert not scorer.explain("SELECT COUNT(*) FROM orders").has("soft_deleted_rows")


# ------------------------------------------------- grouping, history, as-of

MEMBERS_DDL = """
CREATE TABLE users (user_id INT PRIMARY KEY, email TEXT NOT NULL, deleted_at TIMESTAMP);
CREATE TABLE memberships (user_id INT NOT NULL, tenant_id INT NOT NULL, role TEXT, left_at TIMESTAMP,
                          PRIMARY KEY (user_id, tenant_id));
CREATE TABLE tax_rates (tax_rate_id INT PRIMARY KEY, jurisdiction_id INT NOT NULL, pct INT NOT NULL,
                        effective_from DATE NOT NULL, effective_to DATE);
CREATE TABLE jurisdictions (jurisdiction_id INT PRIMARY KEY, code TEXT NOT NULL);
CREATE VIEW current_rates_v AS SELECT tax_rate_id, jurisdiction_id, pct FROM tax_rates WHERE effective_to IS NULL;
"""


@pytest.mark.parametrize("dialect", DIALECTS)
def test_fan_out_is_judged_inside_each_group(dialect):
    scorer = Scorer(dialect, catalog=Catalog.from_ddl(MEMBERS_DDL, dialect))
    # A user belongs to many tenants, but only once to each: per tenant, no user repeats.
    per_tenant = scorer.explain(
        "SELECT m.tenant_id, COUNT(u.user_id) FROM memberships m JOIN users u ON u.user_id = m.user_id "
        "GROUP BY m.tenant_id"
    )
    assert not per_tenant.has("fan_out"), per_tenant.summary()
    overall = scorer.explain(
        "SELECT COUNT(u.user_id) FROM users u JOIN memberships m ON m.user_id = u.user_id"
    )
    assert overall.has("fan_out"), overall.summary()
    by_role = scorer.explain(
        "SELECT m.role, COUNT(u.user_id) FROM users u JOIN memberships m ON m.user_id = u.user_id GROUP BY m.role"
    )
    assert by_role.has("fan_out"), by_role.summary()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_history_table_read_without_choosing_a_version(dialect):
    scorer = Scorer(dialect, catalog=Catalog.from_ddl(MEMBERS_DDL, dialect))
    naive = scorer.explain(
        "SELECT j.code, r.pct FROM jurisdictions j JOIN tax_rates r ON r.jurisdiction_id = j.jurisdiction_id"
    )
    finding = next(f for f in naive.findings if f.code == "history_rows")
    assert finding.evidence["marker"] == "effective_to"
    for sql in (
        "SELECT j.code, r.pct FROM jurisdictions j JOIN tax_rates r ON r.jurisdiction_id = j.jurisdiction_id "
        "WHERE r.effective_to IS NULL",
        "SELECT j.code, r.pct FROM jurisdictions j JOIN current_rates_v r ON r.jurisdiction_id = j.jurisdiction_id",
        "SELECT pct FROM tax_rates WHERE effective_from <= '2026-03-01' "
        "AND (effective_to IS NULL OR effective_to > '2026-03-01')",
        "SELECT pct FROM (SELECT pct, ROW_NUMBER() OVER (PARTITION BY jurisdiction_id ORDER BY effective_from DESC) rn "
        "FROM tax_rates) x WHERE rn = 1",
        "SELECT code FROM jurisdictions",
    ):
        report = scorer.explain(sql)
        assert not report.has("history_rows"), sql + "\n" + report.summary()
        assert not report.has("date_boundary"), sql + "\n" + report.summary()
