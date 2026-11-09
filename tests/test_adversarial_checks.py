"""Adversarial cases for the static checks, written by the independent checker.

Four lists, each run under every dialect the case names (all three by default):

MISSED        SQL that is wrong in the way a check describes, and the check stays silent.
FALSE_ALARMS  correct SQL that a check reports anyway.
STILL_FIRES   wrong SQL in an awkward shape that the check does catch today.
STILL_QUIET   correct SQL in an awkward shape that the check leaves alone today.

The first two are known defects. They are marked ``xfail(strict=True)``, so the suite
stays green now and turns red for a case the moment it is fixed: delete that case's
entry from the list (or move it to the matching STILL_ list) in the same change.
``pytest --runxfail tests/test_adversarial_checks.py`` shows them as plain failures.

The last two are regression cases: they pass today and should keep passing.

Every statement was run on SQLite 3.35.5, DuckDB 1.5.6 and Postgres 16.15 to confirm
that it runs (or is rejected) as the comment says. No database is needed here: the
catalog comes from DDL text, as in test_checks.py.
"""

from __future__ import annotations

import pytest

from sqlgami import Catalog, Scorer, Severity

from conftest import SHOP_DDL

DIALECTS = ("sqlite", "duckdb", "postgres")

HISTORY_DDL = """
CREATE TABLE users (user_id INT PRIMARY KEY, email TEXT NOT NULL, deleted_at TIMESTAMP);
CREATE TABLE memberships (user_id INT NOT NULL, tenant_id INT NOT NULL, role TEXT, left_at TIMESTAMP,
                          PRIMARY KEY (user_id, tenant_id));
CREATE TABLE tax_rates (tax_rate_id INT PRIMARY KEY, jurisdiction_id INT NOT NULL, pct INT NOT NULL,
                        effective_from DATE NOT NULL, effective_to DATE);
CREATE TABLE jurisdictions (jurisdiction_id INT PRIMARY KEY, code TEXT NOT NULL);
CREATE VIEW current_rates_v AS SELECT tax_rate_id, jurisdiction_id, pct FROM tax_rates WHERE effective_to IS NULL;
"""

EXTRA_DDL = """
CREATE TABLE accounts (id INT PRIMARY KEY, email TEXT NOT NULL UNIQUE, plan TEXT);
CREATE TABLE coupons (coupon_id INT PRIMARY KEY, code TEXT NOT NULL UNIQUE, valid_from DATE NOT NULL, valid_until DATE);
CREATE TABLE customer_snapshots (customer_id INT NOT NULL, plan TEXT, dbt_valid_from TIMESTAMP NOT NULL, dbt_valid_to TIMESTAMP);
CREATE TABLE price_history (product_id INT NOT NULL, price INT NOT NULL, start_date DATE NOT NULL, end_date DATE);
CREATE TABLE tax_rates_x (tax_rate_id INT PRIMARY KEY, jurisdiction_id INT NOT NULL, pct INT NOT NULL,
                          effective_from DATE NOT NULL, effective_to DATE);
CREATE TABLE invoice_lines (line_id INT PRIMARY KEY, amount INT NOT NULL,
                            tax_rate_id INT NOT NULL REFERENCES tax_rates_x (tax_rate_id));
"""

_DDL = {"shop": SHOP_DDL, "members": HISTORY_DDL, "extra": EXTRA_DDL}

# Each case: (check code, name, sql, options[, why]).
# options: dialects=(...), ddl="shop" | "members" | "extra", hints={Scorer keyword hints},
#          values=True to declare customers.country as holding exactly US, DE, IN.

MISSED = [
    # fan_out
    (
        'fan_out',
        'derived_join_then_sum',
        "SELECT SUM(x.total) FROM (SELECT o.total FROM orders o JOIN items i ON i.order_id = o.id ) x",
        {},
        "the join repeats each order once per item inside the derived table; the outer SUM has one source so the check skips it (shop data: 365.5 instead of 265.5)",
    ),
    (
        'fan_out',
        'cte_join_then_sum',
        "WITH x AS (SELECT o.id, o.total FROM orders o JOIN items i ON i.order_id = o.id ) SELECT SUM(total) FROM x",
        {},
        "same repeated rows, hidden in a CTE",
    ),
    (
        'fan_out',
        'cte_star_join_then_sum',
        "WITH x AS (SELECT o.*, i.qty FROM orders o JOIN items i ON i.order_id = o.id ) SELECT SUM(total) FROM x",
        {},
        "same repeated rows, hidden in a CTE that selects o.*",
    ),
    (
        'fan_out',
        'window_sum',
        "SELECT DISTINCT c.name, SUM(o.total) OVER (PARTITION BY c.id) FROM customers c JOIN orders o ON o.customer_id = c.id JOIN items i ON i.order_id = o.id",
        {},
        "a window SUM over joined rows is inflated exactly like a grouped SUM; window aggregates are not examined",
    ),
    # outer_join_nullified
    (
        'outer_join_nullified',
        'coalesce_other_value',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE COALESCE(o.status, 'none') = 'paid'",
        {},
        "COALESCE(o.status, 'none') = 'paid' is false for unmatched rows; any COALESCE is treated as keeping them",
    ),
    (
        'outer_join_nullified',
        'coalesce_zero_gt_zero',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE COALESCE(o.total, 0) > 0",
        {},
        "COALESCE(o.total, 0) > 0 is false for unmatched rows",
    ),
    (
        'outer_join_nullified',
        'filter_outside_cte',
        "WITH x AS (SELECT c.name, o.status FROM customers c LEFT JOIN orders o ON o.customer_id = c.id ) SELECT name FROM x WHERE status = 'paid'",
        {},
        "the filter on the outer-joined column sits outside the CTE that does the LEFT JOIN (shop data: 2 of 4 customers survive)",
    ),
    (
        'outer_join_nullified',
        'filter_outside_cte_star',
        "WITH x AS (SELECT c.name, o.* FROM customers c LEFT JOIN orders o ON o.customer_id = c.id ) SELECT name FROM x WHERE status = 'paid'",
        {},
        "as above, with o.* in the CTE",
    ),
    (
        'outer_join_nullified',
        'filter_outside_derived',
        "SELECT name FROM (SELECT c.name, o.status FROM customers c LEFT JOIN orders o ON o.customer_id = c.id ) x WHERE x.status = 'paid'",
        {},
        "as above, with a derived table",
    ),
    (
        'outer_join_nullified',
        'having_on_null_side',
        "SELECT c.name, SUM(o.total) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.name HAVING SUM(o.total) > 10",
        {},
        "HAVING SUM(o.total) > 10 drops every customer without orders",
    ),
    (
        'outer_join_nullified',
        'is_not_distinct_from',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.status IS NOT DISTINCT FROM 'paid'",
        {'dialects': ('duckdb', 'postgres')},
        "IS NOT DISTINCT FROM 'paid' is false for NULL, so unmatched rows are dropped",
    ),
    (
        'outer_join_nullified',
        'case_predicate',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE CASE WHEN o.status = 'paid' THEN 1 ELSE 0 END = 1",
        {},
        "the CASE yields 0 for unmatched rows, so they are dropped; any CASE is treated as keeping them",
    ),
    (
        'outer_join_nullified',
        'exists_on_null_side',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE EXISTS (SELECT 1 FROM items i WHERE i.order_id = o.id)",
        {},
        "the correlated EXISTS is false when o.id is NULL",
    ),
    # not_in_nullable
    (
        'not_in_nullable',
        'union_subquery',
        "SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers UNION SELECT customer_id FROM profiles)",
        {},
        "both UNION branches read nullable columns; a set operation in the subquery is skipped",
    ),
    (
        'not_in_nullable',
        'neq_all',
        "SELECT name FROM customers WHERE id <> ALL (SELECT referrer_id FROM customers)",
        {'dialects': ('duckdb', 'postgres')},
        "<> ALL (subquery) is NOT IN by another name",
    ),
    (
        'not_in_nullable',
        'expression_projection',
        "SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id + 0 FROM customers)",
        {},
        "referrer_id + 0 is still NULL where referrer_id is (shop data: returns no rows; NOT EXISTS returns 3)",
    ),
    (
        'not_in_nullable',
        'aggregate_projection',
        "SELECT name FROM customers WHERE id NOT IN (SELECT MAX(referrer_id) FROM customers GROUP BY country)",
        {},
        "MAX(referrer_id) is NULL for a group with no referrers",
    ),
    (
        'not_in_nullable',
        'column_in_list',
        "SELECT name FROM customers WHERE id NOT IN (1, referrer_id)",
        {},
        "a nullable column in the IN list makes NOT IN unknown for those rows",
    ),
    (
        'not_in_nullable',
        'row_value',
        "SELECT name FROM customers WHERE (id, tenant_id) NOT IN (SELECT referrer_id, tenant_id FROM customers)",
        {},
        "a row-value NOT IN is skipped although one projected column is nullable",
    ),
    (
        'not_in_nullable',
        'case_projection',
        "SELECT name FROM customers WHERE id NOT IN (SELECT CASE WHEN country = 'US' THEN id END FROM customers)",
        {},
        "a CASE without ELSE yields NULL",
    ),
    (
        'not_in_nullable',
        'window_projection',
        "SELECT name FROM customers WHERE id NOT IN (SELECT LAG(id) OVER (ORDER BY id) FROM customers)",
        {},
        "LAG() yields NULL for the first row",
    ),
    # missing_group_by
    (
        'missing_group_by',
        'grouped_by_function_of_column',
        "SELECT o.status, COUNT(*) FROM orders o GROUP BY UPPER(o.status)",
        {},
        "grouping by UPPER(status) does not group status; DuckDB and Postgres reject it, SQLite picks an arbitrary row",
    ),
    (
        'missing_group_by',
        'grouped_by_cast_of_column',
        "SELECT o.created_at, COUNT(*) FROM orders o GROUP BY CAST(o.created_at AS DATE)",
        {},
        "grouping by CAST(created_at AS DATE) does not group created_at; DuckDB and Postgres reject it",
    ),
    (
        'missing_group_by',
        'grouped_by_arithmetic',
        "SELECT i.qty, COUNT(*) FROM items i GROUP BY i.qty / 10",
        {},
        "grouping by qty / 10 does not group qty; DuckDB and Postgres reject it",
    ),
    (
        'missing_group_by',
        'correlated_scalar_subquery',
        "SELECT c.country, (SELECT MAX(o.total) FROM orders o WHERE o.customer_id = c.id) AS m, COUNT(*) FROM customers c GROUP BY c.country",
        {},
        "the scalar subquery reads ungrouped c.id; DuckDB and Postgres reject it",
    ),
    (
        'missing_group_by',
        'unique_not_pk',
        "SELECT email, plan, COUNT(*) FROM accounts GROUP BY email",
        {'ddl': 'extra', 'dialects': ('postgres', 'duckdb')},
        "a UNIQUE column is not a primary key: Postgres and DuckDB both reject selecting plan",
    ),
    (
        'missing_group_by',
        'pk_dependency_duckdb',
        "SELECT c.id, c.name, COUNT(o.id) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.id",
        {'dialects': ('duckdb',)},
        "DuckDB does not infer other columns from a grouped primary key and rejects this (Postgres accepts it)",
    ),
    # limit_without_order
    (
        'limit_without_order',
        'union_limit',
        "SELECT id FROM customers UNION ALL SELECT id FROM orders LIMIT 3",
        {},
        "the LIMIT belongs to the UNION, which is not examined",
    ),
    (
        'limit_without_order',
        'order_by_constant',
        "SELECT name FROM customers ORDER BY NULL LIMIT 2",
        {'dialects': ('sqlite',)},
        "ORDER BY NULL orders nothing",
    ),
    (
        'limit_without_order',
        'row_number_no_order',
        "SELECT x.id FROM (SELECT o.id, ROW_NUMBER() OVER (PARTITION BY o.customer_id) AS rn FROM orders o) x WHERE x.rn = 1",
        {},
        "ROW_NUMBER() without ORDER BY then rn = 1 keeps an arbitrary row per customer: LIMIT 1 per group with no order",
    ),
    (
        'limit_without_order',
        'distinct_on_no_order',
        "SELECT DISTINCT ON (customer_id) customer_id, total FROM orders",
        {'dialects': ('duckdb', 'postgres')},
        "DISTINCT ON without ORDER BY keeps an arbitrary row per customer",
    ),
    (
        'limit_without_order',
        'offset_only',
        "SELECT name FROM customers OFFSET 2",
        {'dialects': ('duckdb', 'postgres')},
        "OFFSET with no ORDER BY skips arbitrary rows",
    ),
    (
        'limit_without_order',
        'parenthesised_union_limit',
        "(SELECT id FROM customers) UNION ALL (SELECT id FROM orders) LIMIT 3",
        {'dialects': ('duckdb', 'postgres')},
        "the LIMIT belongs to the UNION, which is not examined",
    ),
    (
        'limit_without_order',
        'order_by_same_for_all',
        "SELECT name FROM customers c ORDER BY 1 = 1 LIMIT 2",
        {},
        "ORDER BY 1 = 1 is the same for every row and orders nothing",
    ),
    # history_rows
    (
        'history_rows',
        'start_only_no_upper_bound',
        "SELECT pct FROM tax_rates WHERE effective_from <= '2026-03-01'",
        {'ddl': 'members'},
        "effective_from <= date with no upper bound returns every version that started by then",
    ),
    (
        'history_rows',
        'order_by_marker_only',
        "SELECT jurisdiction_id, pct FROM tax_rates ORDER BY effective_from",
        {'ddl': 'members'},
        "sorting by effective_from does not choose a version",
    ),
    (
        'history_rows',
        'marker_computed_not_filtered',
        "SELECT jurisdiction_id, pct, effective_to IS NULL AS is_current FROM tax_rates",
        {'ddl': 'members'},
        "effective_to is turned into a flag that nothing filters on",
    ),
    (
        'history_rows',
        'row_number_without_rn_filter',
        "SELECT pct FROM (SELECT pct, ROW_NUMBER() OVER (PARTITION BY jurisdiction_id ORDER BY effective_from DESC) rn FROM tax_rates) x",
        {'ddl': 'members'},
        "the row number is computed but rn = 1 is never applied",
    ),
    (
        'history_rows',
        'dbt_snapshot_columns',
        "SELECT customer_id, plan FROM customer_snapshots",
        {'ddl': 'extra'},
        "dbt_valid_from / dbt_valid_to are the dbt snapshot convention and are not recognised",
    ),
    (
        'history_rows',
        'start_end_date_pair',
        "SELECT product_id, price FROM price_history",
        {'ddl': 'extra'},
        "start_date / end_date is a common history pair and is not recognised",
    ),
    # distinct_masking
    (
        'distinct_masking',
        'count_distinct_non_key',
        "SELECT COUNT(DISTINCT c.name) FROM customers c JOIN orders o ON o.customer_id = c.id",
        {},
        "COUNT(DISTINCT name) hides the repeated rows but merges customers who share a name",
    ),
    (
        'distinct_masking',
        'group_by_as_dedupe',
        "SELECT SUM(t) FROM (SELECT o.total AS t FROM orders o JOIN items i ON i.order_id = o.id GROUP BY o.total) x",
        {},
        "GROUP BY total removes the repeats but also merges different orders with the same total",
    ),
    # cartesian_join
    # null_comparison
    (
        'null_comparison',
        'case_when_null',
        "SELECT name, CASE country WHEN NULL THEN 'unknown' ELSE country END FROM customers",
        {},
        "CASE x WHEN NULL compares with = and never matches",
    ),
    (
        'null_comparison',
        'in_null_list',
        "SELECT name FROM customers WHERE country IN (NULL)",
        {},
        "IN (NULL) is never true",
    ),
    # type_mismatch
    (
        'type_mismatch',
        'sqlite_int_vs_timestamp_text',
        "SELECT id FROM orders WHERE created_at > 20240101",
        {'dialects': ('sqlite',)},
        "SQLite compares the integer with timestamp text by storage class, so every row matches (shop data: 5 of 5); DuckDB and Postgres reject it",
    ),
    (
        'type_mismatch',
        'join_through_expression',
        "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.name || ''",
        {},
        "an integer key is compared with text; Postgres and DuckDB reject it and SQLite matches nothing",
    ),
    # date_boundary
    (
        'date_boundary',
        'end_of_day_seconds',
        "SELECT id FROM orders WHERE created_at <= '2024-01-31 23:59:59'",
        {},
        "<= '... 23:59:59' leaves out the last fractional second of the day",
    ),
    # filter_value
    (
        'filter_value',
        'lower_vs_upper_literal',
        "SELECT * FROM customers WHERE LOWER(country) = 'US'",
        {'values': True},
        "LOWER(country) can never equal the upper-case 'US'",
    ),
    (
        'filter_value',
        'like_no_wildcard',
        "SELECT * FROM customers WHERE country LIKE 'usa'",
        {'values': True},
        "LIKE with no wildcard is an equality test against a value the column never holds",
    ),
    # avg_of_avg
    (
        'avg_of_avg',
        'avg_of_ratio_columns',
        "WITH a AS (SELECT customer_id, SUM(total) AS s, COUNT(*) AS n FROM orders GROUP BY customer_id) SELECT AVG(s / n) FROM a",
        {},
        "AVG(s / n) averages per-customer ratios; the aggregates are one level down",
    ),
    (
        'avg_of_avg',
        'two_level_ratio',
        "WITH a AS (SELECT customer_id, SUM(total) AS s, COUNT(*) AS n FROM orders GROUP BY customer_id), b AS (SELECT customer_id, s / n AS r FROM a) SELECT AVG(r) FROM b",
        {},
        "the ratio is computed in a second CTE from aggregates in the first",
    ),
    (
        'avg_of_avg',
        'rounded_ratio',
        "WITH a AS (SELECT customer_id, ROUND(SUM(total) * 1.0 / COUNT(*), 2) AS r FROM orders GROUP BY customer_id) SELECT AVG(r) FROM a",
        {},
        "ROUND() around the ratio hides it",
    ),
    (
        'avg_of_avg',
        'manual_sum_over_count',
        "WITH a AS (SELECT customer_id, AVG(total) AS r FROM orders GROUP BY customer_id) SELECT SUM(r) / COUNT(*) FROM a",
        {},
        "SUM(avg) / COUNT(*) is an average of averages written by hand",
    ),
    # soft_deleted_rows
    (
        'soft_deleted_rows',
        'order_by_only',
        "SELECT name FROM customers ORDER BY deleted_at",
        {'hints': {'soft_delete_column': 'deleted_at'}},
        "sorting by deleted_at does not exclude deleted rows",
    ),
    (
        'soft_deleted_rows',
        'computed_flag_never_filtered',
        "WITH c AS (SELECT id, name, deleted_at IS NULL AS live FROM customers) SELECT name FROM c",
        {'hints': {'soft_delete_column': 'deleted_at'}},
        "the live flag is computed in the CTE but never filtered on",
    ),
]

FALSE_ALARMS = [
    # fan_out
    # outer_join_nullified
    (
        'outer_join_nullified',
        'nested_inner_join',
        "SELECT c.name, i.sku FROM customers c LEFT JOIN (orders o JOIN items i ON i.order_id = o.id) ON o.customer_id = c.id",
        {},
        "the inner join is inside the parentheses on the nullable side; every customer is still returned (shop data: 4 of 4)",
    ),
    # not_in_nullable
    (
        'not_in_nullable',
        'guard_greater_than',
        "SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers WHERE referrer_id > 0)",
        {},
        "referrer_id > 0 already excludes NULL",
    ),
    (
        'not_in_nullable',
        'guard_inner_join_on_column',
        "SELECT name FROM customers WHERE id NOT IN (SELECT c2.referrer_id FROM customers c2 JOIN customers r ON r.id = c2.referrer_id)",
        {},
        "the inner join on referrer_id already excludes NULL",
    ),
    (
        'not_in_nullable',
        'guard_in_list',
        "SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers WHERE referrer_id IN (1, 2, 3))",
        {},
        "referrer_id IN (...) already excludes NULL",
    ),
    (
        'not_in_nullable',
        'guard_inside_cte',
        "WITH r AS (SELECT referrer_id FROM customers WHERE referrer_id IS NOT NULL) SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM r)",
        {},
        "the IS NOT NULL guard is inside the CTE the subquery reads",
    ),
    (
        'not_in_nullable',
        'guard_inside_derived',
        "SELECT name FROM customers WHERE id NOT IN (SELECT x.referrer_id FROM (SELECT referrer_id FROM customers WHERE referrer_id IS NOT NULL) x)",
        {},
        "the IS NOT NULL guard is inside the derived table the subquery reads",
    ),
    (
        'not_in_nullable',
        'guard_inside_cte_star',
        "WITH r AS (SELECT * FROM customers WHERE referrer_id IS NOT NULL) SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM r)",
        {},
        "the IS NOT NULL guard is inside a SELECT * CTE",
    ),
    (
        'not_in_nullable',
        'guard_equals_literal',
        "SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers WHERE referrer_id = 1)",
        {},
        "referrer_id = 1 already excludes NULL",
    ),
    (
        'not_in_nullable',
        'guard_between',
        "SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers WHERE referrer_id BETWEEN 1 AND 10)",
        {},
        "BETWEEN already excludes NULL",
    ),
    (
        'not_in_nullable',
        'guard_correlated_eq',
        "SELECT c.name FROM customers c WHERE c.id NOT IN (SELECT c2.referrer_id FROM customers c2 WHERE c2.referrer_id = c.id)",
        {},
        "the correlated equality already excludes NULL",
    ),
    (
        'not_in_nullable',
        'guard_join_to_not_null_column',
        "SELECT sku FROM items WHERE order_id NOT IN (SELECT pr.customer_id FROM profiles pr JOIN payments p ON p.order_id = pr.customer_id)",
        {},
        "the inner join on the projected column already excludes NULL",
    ),
    # missing_group_by
    (
        'missing_group_by',
        'filter_clause',
        "SELECT o.customer_id, COUNT(*) FILTER (WHERE o.status = 'paid') FROM orders o GROUP BY o.customer_id",
        {},
        "the column is inside the aggregate's FILTER clause, which is part of the aggregate",
    ),
    (
        'missing_group_by',
        'within_group',
        "SELECT o.customer_id, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY o.total) FROM orders o GROUP BY o.customer_id",
        {'dialects': ('duckdb', 'postgres')},
        "the column is inside WITHIN GROUP (ORDER BY ...), which is part of the aggregate",
    ),
    (
        'missing_group_by',
        'sqlite_bare_column_with_max',
        "SELECT o.customer_id, o.status, MAX(o.created_at) FROM orders o GROUP BY o.customer_id",
        {'dialects': ('sqlite',)},
        "SQLite documents that a bare column beside a single MAX() comes from the row holding the maximum; this is the usual latest-row idiom there",
    ),
    # limit_without_order
    (
        'limit_without_order',
        'pk_pinned',
        "SELECT name FROM customers WHERE id = 1 LIMIT 1",
        {},
        "the primary key is pinned, so there is at most one row",
    ),
    (
        'limit_without_order',
        'existence_probe',
        "SELECT 1 FROM orders WHERE status = 'paid' LIMIT 1",
        {},
        "only the constant 1 is selected, so every candidate row gives the same answer",
    ),
    (
        'limit_without_order',
        'limit_zero',
        "SELECT * FROM orders LIMIT 0",
        {},
        "LIMIT 0 returns no rows whatever the order",
    ),
    (
        'limit_without_order',
        'scalar_existence',
        "SELECT c.name, (SELECT 1 FROM orders o WHERE o.customer_id = c.id LIMIT 1) IS NOT NULL AS has_orders FROM customers c",
        {},
        "the subquery selects the constant 1 and is only tested for IS NOT NULL",
    ),
    (
        'limit_without_order',
        'composite_key_pinned',
        "SELECT role FROM memberships WHERE user_id = 1 AND tenant_id = 2 LIMIT 1",
        {'ddl': 'members'},
        "the whole composite key is pinned, so there is at most one row",
    ),
    # history_rows
    (
        'history_rows',
        'pk_lookup',
        "SELECT pct FROM tax_rates WHERE tax_rate_id = 5",
        {'ddl': 'members'},
        "the primary key pins exactly one version",
    ),
    (
        'history_rows',
        'fk_to_specific_version',
        "SELECT l.amount, r.pct FROM invoice_lines l JOIN tax_rates_x r ON r.tax_rate_id = l.tax_rate_id",
        {'ddl': 'extra'},
        "the invoice line references the version it was taxed under by primary key",
    ),
    # distinct_masking
    (
        'distinct_masking',
        'distinct_dimension_list',
        "SELECT DISTINCT o.status FROM orders o JOIN items i ON i.order_id = o.id WHERE i.sku = 'A'",
        {},
        "the distinct statuses of orders containing an item are exactly what is asked for",
    ),
    # cartesian_join
    (
        'cartesian_join',
        'both_pinned_by_pk',
        "SELECT c.name, o.total FROM customers c, orders o WHERE c.id = 1 AND o.id = 10",
        {},
        "both tables are pinned to one row by primary key, so the result is one row",
    ),
    # null_comparison
    (
        'null_comparison',
        'surname_null',
        "SELECT id FROM customers WHERE name = 'Null'",
        {},
        "'Null' is a real surname and a legitimate text value",
    ),
    # type_mismatch
    (
        'type_mismatch',
        'sqlite_month_prefix_bound',
        "SELECT id FROM orders WHERE created_at >= '2024-01' AND created_at < '2024-02'",
        {'dialects': ('sqlite',)},
        "'2024-01' is a valid lower bound for text timestamps in SQLite (shop data: same rows as '2024-01-01')",
    ),
    (
        'type_mismatch',
        'postgres_special_date_input',
        "SELECT id FROM orders WHERE created_at < 'now'",
        {'dialects': ('postgres',)},
        "'now' is a documented Postgres timestamp input",
    ),
    (
        'type_mismatch',
        'postgres_infinity',
        "SELECT id FROM orders WHERE created_at < 'infinity'",
        {'dialects': ('postgres',)},
        "'infinity' is a documented Postgres timestamp input",
    ),
    # integer_division
    (
        'integer_division',
        'bucketing',
        "SELECT (i.qty / 10) * 10 AS bucket, COUNT(*) FROM items i GROUP BY (i.qty / 10) * 10",
        {'dialects': ('sqlite', 'postgres')},
        "(qty / 10) * 10 is deliberate integer bucketing",
    ),
    # date_boundary
    (
        'date_boundary',
        'single_day_window',
        "SELECT id FROM orders WHERE created_at >= '2024-01-30' AND created_at < '2024-01-31'",
        {},
        ">= the 30th and < the 31st is the correct half-open range for one day",
    ),
    # filter_value
    (
        'filter_value',
        'in_list_partly_present',
        "SELECT * FROM customers WHERE country IN ('US', 'CA')",
        {'values': True},
        "'US' is stored, so the IN list does match rows; the message says it matches none",
    ),
    # avg_of_avg
    (
        'avg_of_avg',
        'unit_conversion_not_ratio',
        "WITH per AS (SELECT customer_id, SUM(total) / 100.0 AS spend FROM orders GROUP BY customer_id) SELECT AVG(spend) FROM per",
        {},
        "SUM(total) / 100.0 converts units; the outer AVG is the average spend per customer",
    ),
    # missing_tenant_filter
    (
        'missing_tenant_filter',
        'conditional_aggregate',
        "SELECT SUM(CASE WHEN tenant_id = 7 THEN total ELSE 0 END) FROM orders",
        {'hints': {'tenant_column': 'tenant_id'}},
        "only tenant 7's rows contribute to the sum",
    ),
    (
        'missing_tenant_filter',
        'filter_clause',
        "SELECT SUM(total) FILTER (WHERE tenant_id = 7) FROM orders",
        {'hints': {'tenant_column': 'tenant_id'}},
        "only tenant 7's rows pass the aggregate FILTER",
    ),
]

STILL_FIRES = [
    (
        'cartesian_join',
        'or_true',
        "SELECT c.name, o.total FROM customers c JOIN orders o ON o.customer_id = c.id OR 1 = 1",
        {},
    ),
    # fan_out
    (
        'fan_out',
        'comma_join',
        "SELECT SUM(o.total) FROM orders o, items i WHERE i.order_id = o.id",
        {},
    ),
    (
        'fan_out',
        'using_many_to_many',
        "SELECT SUM(p.amount) FROM payments p JOIN items i USING (order_id)",
        {},
    ),
    (
        'fan_out',
        'range_join',
        "SELECT SUM(o.total) FROM orders o JOIN payments p ON p.paid_at >= o.created_at",
        {},
    ),
    (
        'fan_out',
        'self_join',
        "SELECT COUNT(c.id) FROM customers c JOIN customers r ON r.referrer_id = c.id",
        {},
    ),
    (
        'fan_out',
        'left_join_chain',
        "SELECT c.name, SUM(o.total) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id LEFT JOIN payments p ON p.order_id = o.id GROUP BY c.name",
        {},
    ),
    (
        'fan_out',
        'right_join',
        "SELECT SUM(o.total) FROM items i RIGHT JOIN orders o ON i.order_id = o.id",
        {},
    ),
    (
        'fan_out',
        'full_join',
        "SELECT SUM(o.total) FROM orders o FULL JOIN items i ON i.order_id = o.id",
        {},
    ),
    (
        'fan_out',
        'group_by_ordinal',
        "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id JOIN items i ON i.order_id = o.id GROUP BY 1",
        {},
    ),
    (
        'fan_out',
        'group_by_many_side_fk',
        "SELECT i.order_id, SUM(o.total) FROM orders o JOIN items i ON i.order_id = o.id GROUP BY i.order_id",
        {},
    ),
    (
        'fan_out',
        'group_by_many_side_attr',
        "SELECT i.sku, SUM(o.total) FROM orders o JOIN items i ON i.order_id = o.id GROUP BY i.sku",
        {},
    ),
    (
        'fan_out',
        'natural_join',
        "SELECT SUM(p.amount) FROM payments p NATURAL JOIN profiles pr JOIN items i ON i.order_id = p.order_id",
        {},
    ),
    (
        'fan_out',
        'per_group_second_membership',
        "SELECT m.tenant_id, COUNT(u.user_id) FROM memberships m JOIN users u ON u.user_id = m.user_id JOIN memberships m2 ON m2.user_id = u.user_id GROUP BY m.tenant_id",
        {'ddl': 'members'},
    ),
    (
        'fan_out',
        'per_group_expr_on_key',
        "SELECT m.tenant_id / 10, COUNT(u.user_id) FROM users u JOIN memberships m ON m.user_id = u.user_id GROUP BY m.tenant_id / 10",
        {'ddl': 'members'},
    ),
    (
        'fan_out',
        'per_group_role_ordinal',
        "SELECT m.role, COUNT(u.user_id) FROM users u JOIN memberships m ON m.user_id = u.user_id GROUP BY 1",
        {'ddl': 'members'},
    ),
    (
        'fan_out',
        'per_group_using',
        "SELECT m.role, COUNT(u.email) FROM users u JOIN memberships m USING (user_id) GROUP BY m.role",
        {'ddl': 'members'},
    ),
    (
        'fan_out',
        'generate_series_repeat',
        "SELECT SUM(o.total) FROM orders o CROSS JOIN generate_series(1, 2) AS g(n)",
        {'dialects': ('duckdb', 'postgres')},
    ),
    (
        'fan_out',
        'sum_in_having',
        "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id JOIN items i ON i.order_id = o.id GROUP BY c.name HAVING SUM(o.total) > 100",
        {},
    ),
    (
        'fan_out',
        'sum_in_order_by',
        "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id JOIN items i ON i.order_id = o.id GROUP BY c.name ORDER BY SUM(o.total) DESC",
        {},
    ),
    (
        'fan_out',
        'view_joined_twice',
        "SELECT SUM(t.amount) FROM order_totals t JOIN payments p ON p.order_id = t.order_id",
        {},
    ),
    # outer_join_nullified
    (
        'outer_join_nullified',
        'sqlite_is_value',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.status IS 'paid'",
        {'dialects': ('sqlite',)},
    ),
    (
        'outer_join_nullified',
        'using_join',
        "SELECT i.sku FROM items i LEFT JOIN payments p USING (order_id) WHERE p.amount > 10",
        {},
    ),
    (
        'outer_join_nullified',
        'full_join_right_filter',
        "SELECT c.name FROM customers c FULL JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid'",
        {},
    ),
    (
        'outer_join_nullified',
        'full_join_left_filter',
        "SELECT o.id FROM customers c FULL JOIN orders o ON o.customer_id = c.id WHERE c.country = 'US'",
        {},
    ),
    (
        'outer_join_nullified',
        'in_subquery',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.id IN (SELECT order_id FROM payments)",
        {},
    ),
    (
        'outer_join_nullified',
        'unqualified_column',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE status = 'paid'",
        {},
    ),
    (
        'outer_join_nullified',
        'second_left_join_filter',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id LEFT JOIN items i ON i.order_id = o.id WHERE i.qty > 1",
        {},
    ),
    (
        'outer_join_nullified',
        'function_wrapped',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE LOWER(o.status) = 'paid'",
        {},
    ),
    (
        'outer_join_nullified',
        'date_range',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.created_at >= '2024-01-01'",
        {},
    ),
    (
        'outer_join_nullified',
        'between',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.total BETWEEN 1 AND 100",
        {},
    ),
    (
        'outer_join_nullified',
        'like',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.status LIKE 'p%'",
        {},
    ),
    (
        'outer_join_nullified',
        'or_both_reject',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid' OR o.total > 50",
        {},
    ),
    (
        'outer_join_nullified',
        'cross_alias_compare',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.tenant_id = c.tenant_id",
        {},
    ),
    (
        'outer_join_nullified',
        'right_join_filter_left',
        "SELECT c.name FROM orders o RIGHT JOIN customers c ON o.customer_id = c.id WHERE o.status = 'paid'",
        {},
    ),
    (
        'outer_join_nullified',
        'not_in_list',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.status NOT IN ('cancelled')",
        {},
    ),
    (
        'outer_join_nullified',
        'inner_join_using_after',
        "SELECT i.sku FROM items i LEFT JOIN payments p ON p.order_id = i.order_id JOIN orders o ON o.id = p.order_id",
        {},
    ),
    (
        'outer_join_nullified',
        'view_then_filter',
        "SELECT c.name FROM customers c LEFT JOIN cust_avg a ON a.customer_id = c.id WHERE a.avg_total > 10",
        {},
    ),
    (
        'outer_join_nullified',
        'nullif_predicate',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE NULLIF(o.status, 'cancelled') IS NOT NULL",
        {},
    ),
    # not_in_nullable
    (
        'not_in_nullable',
        'basic_nullable',
        "SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers)",
        {},
    ),
    (
        'not_in_nullable',
        'through_cte',
        "WITH r AS (SELECT referrer_id FROM customers) SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM r)",
        {},
    ),
    (
        'not_in_nullable',
        'through_cte_star',
        "WITH r AS (SELECT * FROM customers) SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM r)",
        {},
    ),
    (
        'not_in_nullable',
        'null_supplied_side',
        "SELECT c.name FROM customers c WHERE c.id NOT IN (SELECT o.customer_id FROM customers c2 LEFT JOIN orders o ON o.customer_id = c2.id)",
        {},
    ),
    (
        'not_in_nullable',
        'not_paren_in',
        "SELECT name FROM customers WHERE NOT (id IN (SELECT referrer_id FROM customers))",
        {},
    ),
    (
        'not_in_nullable',
        'guard_inside_or',
        "SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers WHERE referrer_id IS NOT NULL OR country = 'US')",
        {},
    ),
    (
        'not_in_nullable',
        'guard_wrong_column',
        "SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers WHERE country IS NOT NULL)",
        {},
    ),
    (
        'not_in_nullable',
        'distinct_projection',
        "SELECT name FROM customers WHERE id NOT IN (SELECT DISTINCT referrer_id FROM customers)",
        {},
    ),
    (
        'not_in_nullable',
        'view_aggregate_column',
        "SELECT id FROM orders WHERE total NOT IN (SELECT avg_total FROM cust_avg)",
        {},
    ),
    (
        'not_in_nullable',
        'through_derived',
        "SELECT name FROM customers WHERE id NOT IN (SELECT x.referrer_id FROM (SELECT referrer_id FROM customers) x)",
        {},
    ),
    (
        'not_in_nullable',
        'unqualified_no_constraint',
        "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM profiles)",
        {},
    ),
    (
        'not_in_nullable',
        'limit_subquery',
        "SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers ORDER BY id LIMIT 3)",
        {},
    ),
    (
        'not_in_nullable',
        'in_join_on',
        "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id AND c.id NOT IN (SELECT referrer_id FROM customers)",
        {},
    ),
    (
        'not_in_nullable',
        'in_case_when',
        "SELECT name, CASE WHEN id NOT IN (SELECT referrer_id FROM customers) THEN 'leaf' ELSE 'referrer' END FROM customers",
        {},
    ),
    # missing_group_by
    (
        'missing_group_by',
        'order_by_ungrouped',
        "SELECT c.country, COUNT(*) FROM customers c GROUP BY c.country ORDER BY c.name",
        {},
    ),
    (
        'missing_group_by',
        'having_ungrouped',
        "SELECT c.country, COUNT(*) FROM customers c GROUP BY c.country HAVING c.name = 'Ada'",
        {},
    ),
    (
        'missing_group_by',
        'star_with_aggregate',
        "SELECT *, COUNT(*) FROM customers GROUP BY country",
        {},
    ),
    (
        'missing_group_by',
        'ordinal_misses_column',
        "SELECT c.country, c.name, COUNT(*) FROM customers c GROUP BY 1",
        {},
    ),
    (
        'missing_group_by',
        'cte_star',
        "WITH x AS (SELECT * FROM customers) SELECT name, COUNT(*) FROM x GROUP BY country",
        {},
    ),
    (
        'missing_group_by',
        'other_table_column',
        "SELECT c.id, o.status, COUNT(*) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.id",
        {},
    ),
    (
        'missing_group_by',
        'case_over_ungrouped',
        "SELECT CASE WHEN c.country = 'US' THEN c.name END, COUNT(*) FROM customers c GROUP BY c.country",
        {},
    ),
    (
        'missing_group_by',
        'using_join',
        "SELECT order_id, sku, COUNT(*) FROM items JOIN payments USING (order_id) GROUP BY order_id",
        {},
    ),
    (
        'missing_group_by',
        'alias_shadows_column',
        "SELECT UPPER(o.status) AS status, o.total, COUNT(*) FROM orders o GROUP BY 1",
        {},
    ),
    (
        'missing_group_by',
        'window_partition_ungrouped',
        "SELECT c.country, COUNT(*), ROW_NUMBER() OVER (PARTITION BY c.name ORDER BY c.country) FROM customers c GROUP BY c.country",
        {},
    ),
    (
        'missing_group_by',
        'aggregate_no_group_join',
        "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id",
        {},
    ),
    (
        'missing_group_by',
        'derived_table',
        "SELECT x.name, COUNT(*) FROM (SELECT name, country FROM customers) x GROUP BY x.country",
        {},
    ),
    (
        'missing_group_by',
        'right_join',
        "SELECT c.name, o.status, COUNT(*) FROM orders o RIGHT JOIN customers c ON o.customer_id = c.id GROUP BY c.name",
        {},
    ),
    # limit_without_order
    (
        'limit_without_order',
        'limit_offset',
        "SELECT name FROM customers LIMIT 2 OFFSET 2",
        {},
    ),
    (
        'limit_without_order',
        'fetch_first',
        "SELECT name FROM customers FETCH FIRST 2 ROWS ONLY",
        {'dialects': ('duckdb', 'postgres')},
    ),
    (
        'limit_without_order',
        'cte_limit',
        "WITH t AS (SELECT * FROM orders LIMIT 3) SELECT SUM(total) FROM t",
        {},
    ),
    (
        'limit_without_order',
        'scalar_subquery_limit',
        "SELECT c.name, (SELECT o.total FROM orders o WHERE o.customer_id = c.id LIMIT 1) FROM customers c",
        {},
    ),
    (
        'limit_without_order',
        'in_subquery_limit',
        "SELECT name FROM customers WHERE id IN (SELECT customer_id FROM orders LIMIT 2)",
        {'dialects': ('sqlite', 'duckdb', 'postgres')},
    ),
    (
        'limit_without_order',
        'grouped_limit',
        "SELECT customer_id, SUM(total) FROM orders GROUP BY customer_id LIMIT 1",
        {},
    ),
    (
        'limit_without_order',
        'order_only_in_subquery',
        "SELECT * FROM (SELECT id, total FROM orders ORDER BY total DESC) x LIMIT 3",
        {},
    ),
    (
        'limit_without_order',
        'order_in_cte_limit_outside',
        "WITH t AS (SELECT id, total FROM orders ORDER BY total DESC) SELECT * FROM t LIMIT 3",
        {},
    ),
    (
        'limit_without_order',
        'window_order_not_query_order',
        "SELECT name, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM customers LIMIT 2",
        {},
    ),
    # history_rows
    (
        'history_rows',
        'cte_star',
        "WITH r AS (SELECT * FROM tax_rates) SELECT j.code, r.pct FROM jurisdictions j JOIN r ON r.jurisdiction_id = j.jurisdiction_id",
        {'ddl': 'members'},
    ),
    (
        'history_rows',
        'using_join',
        "SELECT j.code, r.pct FROM jurisdictions j JOIN tax_rates r USING (jurisdiction_id)",
        {'ddl': 'members'},
    ),
    (
        'history_rows',
        'natural_join',
        "SELECT code, pct FROM jurisdictions NATURAL JOIN tax_rates",
        {'ddl': 'members'},
    ),
    (
        'history_rows',
        'right_join',
        "SELECT j.code, r.pct FROM tax_rates r RIGHT JOIN jurisdictions j ON r.jurisdiction_id = j.jurisdiction_id",
        {'ddl': 'members'},
    ),
    (
        'history_rows',
        'marker_in_select_list_of_join',
        "SELECT j.code, r.pct, r.effective_to FROM jurisdictions j JOIN tax_rates r ON r.jurisdiction_id = j.jurisdiction_id",
        {'ddl': 'members'},
    ),
    # cartesian_join
    (
        'cartesian_join',
        'on_true',
        "SELECT c.name, o.total FROM customers c JOIN orders o ON TRUE",
        {'dialects': ('duckdb', 'postgres')},
    ),
    (
        'cartesian_join',
        'on_one_equals_one',
        "SELECT c.name, o.total FROM customers c JOIN orders o ON 1 = 1",
        {},
    ),
    (
        'cartesian_join',
        'on_same_table_tautology',
        "SELECT c.name, o.total FROM customers c JOIN orders o ON o.customer_id = o.customer_id",
        {},
    ),
    (
        'cartesian_join',
        'on_single_table_filter',
        "SELECT c.name, o.total FROM customers c JOIN orders o ON o.total > 0",
        {},
    ),
    (
        'cartesian_join',
        'three_tables_one_unlinked',
        "SELECT c.name, o.total, p.amount FROM customers c JOIN orders o ON o.customer_id = c.id, payments p",
        {},
    ),
    # natural_join
    (
        'natural_join',
        'natural_left',
        "SELECT sku, bio FROM items NATURAL LEFT JOIN profiles",
        {},
    ),
    (
        'natural_join',
        'natural_inner',
        "SELECT sku, bio FROM items NATURAL INNER JOIN profiles",
        {},
    ),
    (
        'natural_join',
        'natural_full_outer',
        "SELECT sku, bio FROM items NATURAL FULL OUTER JOIN profiles",
        {},
    ),
    (
        'natural_join',
        'natural_in_cte',
        "WITH x AS (SELECT * FROM customers NATURAL JOIN orders) SELECT COUNT(*) FROM x",
        {},
    ),
    # null_comparison
    (
        'null_comparison',
        'bang_equals_null',
        "SELECT name FROM customers WHERE country != NULL",
        {},
    ),
    # integer_division
    (
        'integer_division',
        'through_cte',
        "WITH t AS (SELECT COUNT(*) AS n, SUM(qty) AS q FROM items) SELECT q / n FROM t",
        {'dialects': ('sqlite', 'postgres')},
    ),
    (
        'integer_division',
        'through_derived',
        "SELECT x.paid / x.n FROM (SELECT COUNT(*) AS n, COUNT(CASE WHEN status = 'paid' THEN 1 END) AS paid FROM orders) x",
        {'dialects': ('sqlite', 'postgres')},
    ),
    (
        'integer_division',
        'percent_mul_first',
        "SELECT 100 * COUNT(CASE WHEN status = 'paid' THEN 1 END) / COUNT(*) FROM orders",
        {'dialects': ('sqlite', 'postgres')},
    ),
    (
        'integer_division',
        'cast_after_divide',
        "SELECT CAST(COUNT(o.id) / COUNT(*) AS REAL) FROM orders o",
        {'dialects': ('sqlite', 'postgres')},
    ),
    (
        'integer_division',
        'sum_case_ratio',
        "SELECT SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) / COUNT(*) FROM orders",
        {'dialects': ('sqlite', 'postgres')},
    ),
    (
        'integer_division',
        'through_cte_star',
        "WITH t AS (SELECT * FROM items) SELECT SUM(qty) / COUNT(*) FROM t",
        {'dialects': ('sqlite', 'postgres')},
    ),
    # date_boundary
    (
        'date_boundary',
        'typed_date_literal',
        "SELECT id FROM orders WHERE created_at <= DATE '2024-01-31'",
        {'dialects': ('duckdb', 'postgres')},
    ),
    (
        'date_boundary',
        'cast_literal',
        "SELECT id FROM orders WHERE created_at <= CAST('2024-01-31' AS DATE)",
        {},
    ),
    (
        'date_boundary',
        'through_cte_alias',
        "WITH x AS (SELECT created_at AS ts FROM orders) SELECT COUNT(*) FROM x WHERE ts <= '2024-01-31'",
        {},
    ),
    # filter_value
    (
        'filter_value',
        'case_when_value',
        "SELECT SUM(CASE WHEN country = 'usa' THEN 1 ELSE 0 END) FROM customers",
        {'values': True},
    ),
    # avg_of_avg
    (
        'avg_of_avg',
        'cte_star',
        "WITH a AS (SELECT customer_id, AVG(total) AS r FROM orders GROUP BY customer_id), b AS (SELECT * FROM a) SELECT AVG(r) FROM b",
        {},
    ),
    # missing_tenant_filter
    (
        'missing_tenant_filter',
        'self_equality',
        "SELECT COUNT(*) FROM orders WHERE tenant_id = tenant_id",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    (
        'missing_tenant_filter',
        'or_true',
        "SELECT COUNT(*) FROM orders WHERE tenant_id = 7 OR 1 = 1",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    (
        'missing_tenant_filter',
        'or_other_filter',
        "SELECT COUNT(*) FROM orders WHERE tenant_id = 7 OR status = 'paid'",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    (
        'missing_tenant_filter',
        'not_equal',
        "SELECT COUNT(*) FROM orders WHERE tenant_id <> 7",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    (
        'missing_tenant_filter',
        'in_unfiltered_subquery',
        "SELECT COUNT(*) FROM orders WHERE tenant_id IN (SELECT tenant_id FROM customers)",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    (
        'missing_tenant_filter',
        'cte_star_unfiltered',
        "WITH o AS (SELECT * FROM orders) SELECT COUNT(*) FROM o",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    (
        'missing_tenant_filter',
        'using_join_other_key',
        "SELECT COUNT(*) FROM items i JOIN payments p USING (order_id) JOIN orders o ON o.id = i.order_id",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    (
        'missing_tenant_filter',
        'natural_join_unfiltered',
        "SELECT COUNT(*) FROM customers NATURAL JOIN orders",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    (
        'missing_tenant_filter',
        'left_join_on_preserved_side',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id AND o.tenant_id = c.tenant_id AND o.tenant_id = 7",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    # soft_deleted_rows
    (
        'soft_deleted_rows',
        'cte_star',
        "WITH c AS (SELECT * FROM customers) SELECT COUNT(*) FROM c",
        {'hints': {'soft_delete_column': 'deleted_at'}},
    ),
    (
        'soft_deleted_rows',
        'second_alias_unfiltered',
        "SELECT c.name, r.name FROM customers c JOIN customers r ON r.id = c.referrer_id WHERE c.deleted_at IS NULL",
        {'hints': {'soft_delete_column': 'deleted_at'}},
    ),
    (
        'soft_deleted_rows',
        'right_join',
        "SELECT c.name, o.total FROM orders o RIGHT JOIN customers c ON o.customer_id = c.id",
        {'hints': {'soft_delete_column': 'deleted_at'}},
    ),
]

STILL_QUIET = [
    (
        'missing_tenant_filter',
        'using_tenant_unqualified',
        "SELECT c.name, o.total FROM customers c JOIN orders o USING (tenant_id) WHERE tenant_id = 7 AND o.customer_id = c.id",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    (
        'fan_out',
        'view_grouped_by_pk_plus',
        "SELECT SUM(o.total) FROM orders o JOIN order_totals t ON t.order_id = o.id",
        {},
    ),
    # fixed false alarms, kept as regressions
    (
        'fan_out',
        'anti_join',
        "SELECT SUM(o.total) FROM orders o LEFT JOIN payments p ON p.order_id = o.id WHERE p.id IS NULL",
        {},
    ),
    (
        'history_rows',
        'validity_window_not_history',
        "SELECT COUNT(*) FROM coupons",
        {'ddl': 'extra'},
    ),
    # fan_out
    (
        'fan_out',
        'many_side_pinned_by_pk',
        "SELECT SUM(o.total) FROM orders o JOIN items i ON i.order_id = o.id WHERE i.id = 100",
        {},
    ),
    (
        'fan_out',
        'latest_row_rn1',
        "SELECT SUM(o.total) FROM orders o JOIN (SELECT order_id, amount, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY paid_at DESC) AS rn FROM payments) p ON p.order_id = o.id AND p.rn = 1",
        {},
    ),
    (
        'fan_out',
        'distinct_derived',
        "SELECT SUM(o.total) FROM orders o JOIN (SELECT DISTINCT order_id FROM items) i ON i.order_id = o.id",
        {},
    ),
    (
        'fan_out',
        'grouped_derived',
        "SELECT SUM(o.total) FROM orders o JOIN (SELECT order_id FROM items GROUP BY order_id) i ON i.order_id = o.id",
        {},
    ),
    (
        'fan_out',
        'group_by_many_side_pk',
        "SELECT i.id, SUM(o.total) FROM orders o JOIN items i ON i.order_id = o.id GROUP BY i.id",
        {},
    ),
    (
        'fan_out',
        'single_row_cross',
        "SELECT SUM(o.total) FROM orders o CROSS JOIN (SELECT MAX(order_date) AS d FROM orders) m WHERE o.order_date = m.d",
        {},
    ),
    (
        'fan_out',
        'count_star_child_rows',
        "SELECT c.name, COUNT(*) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name",
        {},
    ),
    (
        'fan_out',
        'composite_key_pinned',
        "SELECT COUNT(u.user_id) FROM users u JOIN memberships m ON m.user_id = u.user_id WHERE m.tenant_id = 7",
        {'ddl': 'members'},
    ),
    (
        'fan_out',
        'per_group_ordinal',
        "SELECT m.tenant_id, COUNT(u.user_id) FROM memberships m JOIN users u ON u.user_id = m.user_id GROUP BY 1",
        {'ddl': 'members'},
    ),
    (
        'fan_out',
        'per_group_using',
        "SELECT m.tenant_id, COUNT(u.email) FROM memberships m JOIN users u USING (user_id) GROUP BY m.tenant_id",
        {'ddl': 'members'},
    ),
    (
        'fan_out',
        'per_group_cte_star',
        "WITH mm AS (SELECT * FROM memberships) SELECT mm.tenant_id, COUNT(u.user_id) FROM mm JOIN users u ON u.user_id = mm.user_id GROUP BY mm.tenant_id",
        {'ddl': 'members'},
    ),
    (
        'fan_out',
        'parent_joins_only',
        "SELECT SUM(i.qty * i.price) FROM items i JOIN orders o ON o.id = i.order_id JOIN customers c ON c.id = o.customer_id",
        {},
    ),
    (
        'fan_out',
        'right_join_parent',
        "SELECT SUM(i.qty) FROM orders o RIGHT JOIN items i ON i.order_id = o.id",
        {},
    ),
    (
        'fan_out',
        'exists_semi_join',
        "SELECT SUM(o.total) FROM orders o WHERE EXISTS (SELECT 1 FROM items i WHERE i.order_id = o.id)",
        {},
    ),
    (
        'fan_out',
        'first_item_join',
        "SELECT SUM(o.total) FROM orders o JOIN (SELECT order_id, MIN(id) AS first_item FROM items GROUP BY order_id) f ON f.order_id = o.id JOIN items i ON i.id = f.first_item",
        {},
    ),
    # outer_join_nullified
    (
        'outer_join_nullified',
        'anti_join',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.id IS NULL",
        {},
    ),
    (
        'outer_join_nullified',
        'or_is_null',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid' OR o.id IS NULL",
        {},
    ),
    (
        'outer_join_nullified',
        'coalesce_keeps_null',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE COALESCE(o.status, 'none') <> 'cancelled'",
        {},
    ),
    (
        'outer_join_nullified',
        'right_join_filter_preserved',
        "SELECT c.name FROM orders o RIGHT JOIN customers c ON o.customer_id = c.id WHERE c.country = 'US'",
        {},
    ),
    (
        'outer_join_nullified',
        'is_distinct_from',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.status IS DISTINCT FROM 'cancelled'",
        {'dialects': ('duckdb', 'postgres')},
    ),
    (
        'outer_join_nullified',
        'case_keeps_null',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE CASE WHEN o.id IS NULL THEN 1 WHEN o.status = 'paid' THEN 1 ELSE 0 END = 1",
        {},
    ),
    (
        'outer_join_nullified',
        'filter_in_later_left_on',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id LEFT JOIN items i ON i.order_id = o.id AND o.status = 'paid'",
        {},
    ),
    (
        'outer_join_nullified',
        'inner_join_on_preserved',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id JOIN customers r ON r.id = c.referrer_id",
        {},
    ),
    (
        'outer_join_nullified',
        'conditional_count',
        "SELECT c.name, COUNT(CASE WHEN o.status = 'paid' THEN 1 END) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.name",
        {},
    ),
    (
        'outer_join_nullified',
        'is_not_false',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE (o.status = 'paid') IS NOT FALSE",
        {},
    ),
    (
        'outer_join_nullified',
        'is_null_or_range',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.total IS NULL OR o.total > 5",
        {},
    ),
    (
        'outer_join_nullified',
        'full_join_coalesce',
        "SELECT COALESCE(c.id, o.customer_id) FROM customers c FULL JOIN orders o ON o.customer_id = c.id WHERE COALESCE(c.tenant_id, o.tenant_id) = 1",
        {},
    ),
    (
        'outer_join_nullified',
        'not_exists_on_null_side',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE NOT EXISTS (SELECT 1 FROM items i WHERE i.order_id = o.id)",
        {},
    ),
    (
        'outer_join_nullified',
        'filter_clause',
        "SELECT c.name, COUNT(*) FILTER (WHERE o.status = 'paid') FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.name",
        {},
    ),
    (
        'outer_join_nullified',
        'using_filter_preserved',
        "SELECT i.sku FROM items i LEFT JOIN payments p USING (order_id) WHERE i.qty > 1",
        {},
    ),
    (
        'outer_join_nullified',
        'using_filter_join_column',
        "SELECT i.sku FROM items i LEFT JOIN payments p USING (order_id) WHERE order_id > 5",
        {},
    ),
    (
        'outer_join_nullified',
        'filter_in_cte_before_join',
        "WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT c.name, p.total FROM customers c LEFT JOIN paid p ON p.customer_id = c.id",
        {},
    ),
    (
        'outer_join_nullified',
        'filter_in_on_clause',
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id AND o.status = 'paid' WHERE c.country = 'US'",
        {},
    ),
    # not_in_nullable
    (
        'not_in_nullable',
        'coalesce_projection',
        "SELECT name FROM customers WHERE id NOT IN (SELECT COALESCE(referrer_id, -1) FROM customers)",
        {},
    ),
    (
        'not_in_nullable',
        'primary_key',
        "SELECT name FROM customers WHERE id NOT IN (SELECT id FROM customers WHERE country = 'US')",
        {},
    ),
    (
        'not_in_nullable',
        'not_null_fk',
        "SELECT name FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)",
        {},
    ),
    # missing_group_by
    (
        'missing_group_by',
        'group_by_alias',
        "SELECT UPPER(c.country) AS k, COUNT(*) FROM customers c GROUP BY k",
        {},
    ),
    (
        'missing_group_by',
        'group_by_ordinal',
        "SELECT UPPER(c.country), COUNT(*) FROM customers c GROUP BY 1",
        {},
    ),
    (
        'missing_group_by',
        'group_by_two_ordinals',
        "SELECT c.country, c.tenant_id, COUNT(*) FROM customers c GROUP BY 1, 2",
        {},
    ),
    (
        'missing_group_by',
        'grouped_expression_reused',
        "SELECT UPPER(c.country) || '-x', COUNT(*) FROM customers c GROUP BY UPPER(c.country)",
        {},
    ),
    (
        'missing_group_by',
        'using_join_column',
        "SELECT order_id, COUNT(*) FROM items JOIN payments USING (order_id) GROUP BY order_id",
        {},
    ),
    (
        'missing_group_by',
        'rollup',
        "SELECT c.country, COUNT(*) FROM customers c GROUP BY ROLLUP (c.country)",
        {'dialects': ('duckdb', 'postgres')},
    ),
    (
        'missing_group_by',
        'grouping_sets',
        "SELECT c.country, c.tenant_id, COUNT(*) FROM customers c GROUP BY GROUPING SETS ((c.country), (c.tenant_id))",
        {'dialects': ('duckdb', 'postgres')},
    ),
    (
        'missing_group_by',
        'aggregate_order_by',
        "SELECT c.country, STRING_AGG(c.name, ',' ORDER BY c.name) FROM customers c GROUP BY c.country",
        {'dialects': ('duckdb', 'postgres')},
    ),
    (
        'missing_group_by',
        'cte_star',
        "WITH x AS (SELECT * FROM customers) SELECT country, COUNT(*) FROM x GROUP BY country",
        {},
    ),
    (
        'missing_group_by',
        'uncorrelated_scalar_subquery',
        "SELECT c.country, COUNT(*) * 1.0 / (SELECT COUNT(*) FROM customers) FROM customers c GROUP BY c.country",
        {},
    ),
    (
        'missing_group_by',
        'window_over_aggregate',
        "SELECT c.country, COUNT(*), SUM(COUNT(*)) OVER (), RANK() OVER (ORDER BY COUNT(*) DESC) FROM customers c GROUP BY c.country",
        {},
    ),
    (
        'missing_group_by',
        'window_partition_by_group_key',
        "SELECT c.country, c.tenant_id, SUM(COUNT(*)) OVER (PARTITION BY c.country) FROM customers c GROUP BY c.country, c.tenant_id",
        {},
    ),
    (
        'missing_group_by',
        'grouped_case',
        "SELECT CASE WHEN c.country = 'US' THEN 'dom' ELSE 'intl' END AS k, COUNT(*) FROM customers c GROUP BY CASE WHEN c.country = 'US' THEN 'dom' ELSE 'intl' END",
        {},
    ),
    (
        'missing_group_by',
        'pk_dependency_left_join',
        "SELECT c.id, c.name, COUNT(o.id) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id",
        {'dialects': ('sqlite', 'postgres')},
    ),
    (
        'missing_group_by',
        'full_join_coalesce_key',
        "SELECT COALESCE(c.id, o.customer_id) AS k, COUNT(*) FROM customers c FULL JOIN orders o ON o.customer_id = c.id GROUP BY COALESCE(c.id, o.customer_id)",
        {},
    ),
    (
        'missing_group_by',
        'correlated_on_group_key',
        "SELECT c.country, (SELECT COUNT(*) FROM customers c2 WHERE c2.country = c.country) FROM customers c GROUP BY c.country",
        {},
    ),
    (
        'missing_group_by',
        'qualify_on_aggregate',
        "SELECT c.country, COUNT(*) AS n FROM customers c GROUP BY c.country QUALIFY ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC) = 1",
        {'dialects': ('duckdb',)},
    ),
    (
        'missing_group_by',
        'group_by_all',
        "SELECT c.country, c.tenant_id, COUNT(*) FROM customers c GROUP BY ALL",
        {'dialects': ('duckdb',)},
    ),
    (
        'missing_group_by',
        'distinct_aggregate_arg',
        "SELECT c.country, COUNT(DISTINCT c.name) FROM customers c GROUP BY c.country",
        {},
    ),
    # limit_without_order
    (
        'limit_without_order',
        'exists_subquery',
        "SELECT c.name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id LIMIT 1)",
        {},
    ),
    (
        'limit_without_order',
        'union_ordered',
        "SELECT id FROM customers UNION ALL SELECT id FROM orders ORDER BY id LIMIT 3",
        {},
    ),
    (
        'limit_without_order',
        'ungrouped_aggregate',
        "SELECT MAX(total), MIN(total) FROM orders LIMIT 1",
        {},
    ),
    (
        'limit_without_order',
        'ordered_by_ordinal',
        "SELECT name, country FROM customers ORDER BY 1 LIMIT 2",
        {},
    ),
    (
        'limit_without_order',
        'ordered_cte_star',
        "WITH t AS (SELECT * FROM orders) SELECT * FROM t ORDER BY id LIMIT 3",
        {},
    ),
    # history_rows
    (
        'history_rows',
        'as_of_between',
        "SELECT pct FROM tax_rates WHERE '2026-03-01' BETWEEN effective_from AND COALESCE(effective_to, '9999-12-31')",
        {'ddl': 'members'},
    ),
    (
        'history_rows',
        'current_view_cte',
        "WITH cur AS (SELECT * FROM tax_rates WHERE effective_to IS NULL) SELECT j.code, cur.pct FROM jurisdictions j JOIN cur USING (jurisdiction_id)",
        {'ddl': 'members'},
    ),
    (
        'history_rows',
        'latest_by_max_start',
        "SELECT r.pct FROM tax_rates r WHERE r.effective_from = (SELECT MAX(r2.effective_from) FROM tax_rates r2 WHERE r2.jurisdiction_id = r.jurisdiction_id)",
        {'ddl': 'members'},
    ),
    # distinct_masking
    (
        'distinct_masking',
        'distinct_with_key',
        "SELECT DISTINCT o.id, o.total FROM orders o JOIN items i ON i.order_id = o.id ",
        {},
    ),
    # cartesian_join
    (
        'cartesian_join',
        'using_links',
        "SELECT c.name, o.total FROM customers c JOIN orders o USING (tenant_id)",
        {},
    ),
    (
        'cartesian_join',
        'natural_links',
        "SELECT sku, amount FROM items NATURAL JOIN (SELECT order_id, amount FROM payments) p",
        {},
    ),
    (
        'cartesian_join',
        'single_row_derived',
        "SELECT o.id, o.total * 1.0 / t.n FROM orders o, (SELECT COUNT(*) AS n FROM orders) t",
        {},
    ),
    (
        'cartesian_join',
        'linked_in_where_via_expr',
        "SELECT c.name, o.total FROM customers c, orders o WHERE o.customer_id + 0 = c.id",
        {},
    ),
    # integer_division
    (
        'integer_division',
        'numeric_column',
        "SELECT o.total / 2 FROM orders o",
        {'dialects': ('sqlite', 'postgres')},
    ),
    # date_boundary
    (
        'date_boundary',
        'half_open_month',
        "SELECT id FROM orders WHERE created_at >= '2024-01-01' AND created_at < '2024-02-01'",
        {},
    ),
    # missing_tenant_filter
    (
        'missing_tenant_filter',
        'positional_parameter',
        "SELECT COUNT(*) FROM orders WHERE tenant_id = $1",
        {'hints': {'tenant_column': 'tenant_id'}, 'dialects': ('postgres', 'duckdb')},
    ),
    (
        'missing_tenant_filter',
        'named_parameter',
        "SELECT COUNT(*) FROM orders WHERE tenant_id = :tenant_id",
        {'hints': {'tenant_column': 'tenant_id'}, 'dialects': ('sqlite',)},
    ),
    (
        'missing_tenant_filter',
        'qmark_parameter',
        "SELECT COUNT(*) FROM orders WHERE tenant_id = ?",
        {'hints': {'tenant_column': 'tenant_id'}, 'dialects': ('sqlite', 'duckdb')},
    ),
    (
        'missing_tenant_filter',
        'using_tenant',
        "SELECT c.name, o.total FROM customers c JOIN orders o USING (tenant_id) WHERE c.tenant_id = 7 AND o.customer_id = c.id",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    (
        'missing_tenant_filter',
        'cte_star_filtered_outside',
        "WITH o AS (SELECT * FROM orders) SELECT COUNT(*) FROM o WHERE tenant_id = 7",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    (
        'missing_tenant_filter',
        'cte_star_filtered_inside',
        "WITH o AS (SELECT * FROM orders WHERE tenant_id = 7) SELECT COUNT(*) FROM o",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    (
        'missing_tenant_filter',
        'cast_on_column',
        "SELECT COUNT(*) FROM orders WHERE CAST(tenant_id AS TEXT) = '7'",
        {'hints': {'tenant_column': 'tenant_id'}},
    ),
    # soft_deleted_rows
    (
        'soft_deleted_rows',
        'cte_star_filtered_outside',
        "WITH c AS (SELECT * FROM customers) SELECT COUNT(*) FROM c WHERE deleted_at IS NULL",
        {'hints': {'soft_delete_column': 'deleted_at'}},
    ),
    (
        'soft_deleted_rows',
        'filter_in_join_on',
        "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id AND c.deleted_at IS NULL",
        {'hints': {'soft_delete_column': 'deleted_at'}},
    ),
    (
        'soft_deleted_rows',
        'using_join_filtered',
        "SELECT COUNT(*) FROM customers c JOIN orders o USING (tenant_id) WHERE c.deleted_at IS NULL",
        {'hints': {'soft_delete_column': 'deleted_at'}},
    ),
]


def _explain(dialect: str, sql: str, options: dict):
    catalog = Catalog.from_ddl(_DDL[options.get("ddl", "shop")], dialect)
    if options.get("values"):
        catalog.column(catalog.resolve("customers"), "country").values = ("US", "DE", "IN")
    return Scorer(dialect, catalog=catalog, **options.get("hints", {})).explain(sql)


def _params(cases: list, known_defect: bool) -> list:
    out = []
    for case in cases:
        code, name, sql, options = case[:4]
        marks = [pytest.mark.xfail(strict=True, reason=case[4])] if known_defect else []
        for dialect in options.get("dialects", DIALECTS):
            out.append(pytest.param(dialect, code, sql, options, id=f"{code}-{name}-{dialect}", marks=marks))
    return out


def _gates_passed(report) -> bool:
    return all(
        stage.status.value == "passed" for stage in report.stages if stage.name in ("parse", "safety", "schema", "static")
    )


@pytest.mark.parametrize(
    "dialect,code,sql,options", _params(MISSED + FALSE_ALARMS + STILL_FIRES + STILL_QUIET, known_defect=False)
)
def test_adversarial_sql_reaches_the_static_checks(dialect, code, sql, options):
    """Not xfail: a case that stops parsing or resolving must not hide behind its xfail mark."""
    report = _explain(dialect, sql, options)
    assert _gates_passed(report), report.summary()
    assert not [c for c in report.checks_not_run if "internal error" in c.reason], report.summary()


@pytest.mark.parametrize("dialect,code,sql,options", _params(MISSED, known_defect=True))
def test_known_miss(dialect, code, sql, options):
    report = _explain(dialect, sql, options)
    assert report.has(code), report.summary()


@pytest.mark.parametrize("dialect,code,sql,options", _params(FALSE_ALARMS, known_defect=True))
def test_known_false_alarm(dialect, code, sql, options):
    report = _explain(dialect, sql, options)
    assert not report.has(code), report.summary()


@pytest.mark.parametrize("dialect,code,sql,options", _params(STILL_FIRES, known_defect=False))
def test_still_fires(dialect, code, sql, options):
    report = _explain(dialect, sql, options)
    assert report.has(code), report.summary()


@pytest.mark.parametrize("dialect,code,sql,options", _params(STILL_QUIET, known_defect=False))
def test_still_quiet(dialect, code, sql, options):
    report = _explain(dialect, sql, options)
    assert not report.has(code), report.summary()


# ------------------------------------------------------------ one-off defects

NATURAL_JOIN_MERGED_COLUMN = [
    "SELECT order_id FROM items NATURAL JOIN (SELECT order_id, amount FROM payments) p",
    "SELECT order_id, COUNT(*) FROM items NATURAL JOIN (SELECT order_id, amount FROM payments) p GROUP BY order_id",
    "SELECT sku FROM items NATURAL JOIN (SELECT order_id, amount FROM payments) p WHERE order_id = 10",
]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("sql", NATURAL_JOIN_MERGED_COLUMN)
def test_natural_join_merged_column_is_not_ambiguous(dialect, sql):
    report = _explain(dialect, sql, {})
    assert not report.has("ambiguous_column"), report.summary()
    assert report.stage("schema").status.value == "passed", report.summary()
    assert report.has("natural_join"), report.summary()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_using_join_merged_column_is_not_ambiguous(dialect):
    report = _explain(dialect, "SELECT order_id FROM items JOIN payments USING (order_id)", {})
    assert report.stage("schema").status.value == "passed", report.summary()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "cartesian_join decides a cross join is deliberate by searching the raw SQL text for "
        "'cross join', so a comment containing those words downgrades an accidental comma join "
        "from high to low."
    ),
)
@pytest.mark.parametrize("dialect", DIALECTS)
def test_comment_does_not_make_a_comma_join_deliberate(dialect):
    report = _explain(dialect, "SELECT /* not a cross join */ c.name, o.total FROM customers c, orders o", {})
    finding = next(f for f in report.findings if f.code == "cartesian_join")
    assert finding.severity == Severity.HIGH, report.summary()


def test_comma_join_without_the_comment_is_high():
    report = _explain("duckdb", "SELECT c.name, o.total FROM customers c, orders o", {})
    finding = next(f for f in report.findings if f.code == "cartesian_join")
    assert finding.severity == Severity.HIGH, report.summary()


_TENANT = {"hints": {"tenant_column": "tenant_id"}}
_TENANT_LEFT = (
    "SELECT c.name, o.total FROM customers c LEFT JOIN orders o "
    "ON o.customer_id = c.id AND o.tenant_id = c.tenant_id WHERE c.tenant_id = 7"
)
_TENANT_RIGHT = (
    "SELECT c.name, o.total FROM orders o RIGHT JOIN customers c "
    "ON o.customer_id = c.id AND o.tenant_id = c.tenant_id WHERE c.tenant_id = 7"
)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_tenant_filter_carries_across_a_left_join(dialect):
    assert not _explain(dialect, _TENANT_LEFT, _TENANT).has("missing_tenant_filter")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_tenant_filter_carries_across_a_right_join(dialect):
    assert not _explain(dialect, _TENANT_RIGHT, _TENANT).has("missing_tenant_filter")
