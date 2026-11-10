"""Red-team of the grain checks (fan_out, soft_deleted_rows) after the leniency rewrite.

Looking for FALSE NEGATIVES: SQL that is wrong in the way the check describes, where the new,
more lenient reasoning now stays silent (or reports too low a severity).

Static except where noted; the inflation claims were confirmed against the shop data in
conftest (true SUM of order totals that have items = 265.5; the flagged queries return 365.5).

CONTAINED cases pass today and guard against regressions. The FALSE-NEGATIVE cases are confirmed
holes, xfail(strict): each turns green when the check catches it. ``pytest --runxfail`` shows
them as plain failures.
"""

from __future__ import annotations

import pytest

from sqlgami import Catalog, Scorer, Severity
from conftest import SHOP_DDL

DIALECTS = ("sqlite", "duckdb", "postgres")


def _findings(sql: str, code: str, dialect: str, **hints):
    report = Scorer(dialect, catalog=Catalog.from_ddl(SHOP_DDL, dialect), **hints).explain(sql)
    assert report.stage("schema").status.value == "passed", report.summary()
    return [f for f in report.findings if f.code == code]


def _fires(sql: str, code: str, dialect: str, **hints) -> bool:
    return bool(_findings(sql, code, dialect, **hints))


def _max_severity(sql: str, code: str, dialect: str, **hints):
    fs = _findings(sql, code, dialect, **hints)
    return max((f.severity.rank for f in fs), default=-1)


# ------------------------------------------------------------------ fan_out

# A parent's total (orders.total) carried down to child-row grain (one row per item) inside a
# CTE/derived table/view that projects the child's key, then SUM()med. The sum counts each
# order's total once per item: inflated (365.5 vs a true 265.5 on the shop data). The check
# skips it because the subquery projects a key of its own grain, so it assumes the summed column
# is an attribute at that grain rather than a parent total copied down.
FANOUT_FALSE_NEGATIVES = [
    (
        "parent_total_via_cte_projects_child_key",
        "WITH x AS (SELECT i.id AS item_id, o.total AS order_total "
        "FROM orders o JOIN items i ON i.order_id = o.id) SELECT SUM(order_total) FROM x",
    ),
    (
        "parent_total_via_derived_table",
        "SELECT SUM(x.order_total) FROM "
        "(SELECT i.id AS item_id, o.total AS order_total FROM orders o JOIN items i ON i.order_id = o.id) x",
    ),
    (
        "parent_total_renamed_rate_like",  # 'unit_price' trips _RATE_LIKE as well as the key path
        "WITH x AS (SELECT i.id AS item_id, o.total AS unit_price "
        "FROM orders o JOIN items i ON i.order_id = o.id) SELECT SUM(unit_price) FROM x",
    ),
    (
        "parent_total_via_view_then_child_key",
        "WITH x AS (SELECT i.id AS item_id, t.amount AS order_amount "
        "FROM order_totals t JOIN items i ON i.order_id = t.order_id) SELECT SUM(order_amount) FROM x",
    ),
]


@pytest.mark.xfail(strict=True, reason="parent total carried to child-row grain in a key-projecting subquery is summed once per child and not flagged (365.5 vs true 265.5 on the shop data)")
@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("name,sql", FANOUT_FALSE_NEGATIVES, ids=[c[0] for c in FANOUT_FALSE_NEGATIVES])
def test_fan_out_misses_parent_total_at_child_grain(name, sql, dialect):
    assert _fires(sql, "fan_out", dialect)


# Guards: the same shape WITHOUT the child key is caught, a recursive-CTE fan-out is caught, and
# a genuine per-row value (price per item) is correctly left alone.
FANOUT_CONTAINED = [
    (
        "parent_total_no_child_key_fires",
        "WITH x AS (SELECT o.total AS order_total FROM orders o JOIN items i ON i.order_id = o.id) "
        "SELECT SUM(order_total) FROM x",
        True,
    ),
    (
        "rate_named_parent_total_no_key_fires",
        "WITH x AS (SELECT o.total AS unit_price FROM orders o JOIN items i ON i.order_id = o.id) "
        "SELECT SUM(unit_price) FROM x",
        True,
    ),
    (
        "recursive_cte_fanout_fires",
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
        "SELECT SUM(o.total) FROM orders o JOIN r ON 1 = 1",
        True,
    ),
    (
        "genuine_per_item_price_quiet",
        "WITH x AS (SELECT i.id, i.price FROM items i JOIN orders o ON o.id = i.order_id) "
        "SELECT SUM(price) FROM x",
        False,
    ),
]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("name,sql,should_fire", FANOUT_CONTAINED, ids=[c[0] for c in FANOUT_CONTAINED])
def test_fan_out_regression_guards(name, sql, should_fire, dialect):
    assert _fires(sql, "fan_out", dialect) is should_fire


# ---------------------------------------------------------- soft_deleted_rows

# The soft-deletable table's own key (and attributes) are grouped by / listed in the output, so a
# deleted row of that table appears in the result (confirmed on a dept/emp fixture: the deleted
# dept is a row in the output). The check classifies the table as counted=False ("a parent, not
# what is counted") and reports only LOW, although its deleted rows are literally in the result.
SOFT_DELETE_UNDERSEVERITY = [
    (
        "deleted_key_grouped_in_output",
        "SELECT c.id, c.name, COUNT(o.id) AS n FROM customers c JOIN orders o ON o.customer_id = c.id "
        "GROUP BY c.id, c.name",
    ),
    (
        "deleted_key_listed_in_output",
        "SELECT c.id, c.name, o.total FROM customers c JOIN orders o ON o.customer_id = c.id",
    ),
]


@pytest.mark.xfail(strict=True, reason="the soft-deletable table's key/attributes are in the output, so its deleted rows appear in the result, yet it is reported only LOW as an uncounted parent")
@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("name,sql", SOFT_DELETE_UNDERSEVERITY, ids=[c[0] for c in SOFT_DELETE_UNDERSEVERITY])
def test_soft_delete_underseverity_when_key_is_in_output(name, sql, dialect):
    assert _max_severity(sql, "soft_deleted_rows", dialect, soft_delete_column="deleted_at") >= Severity.MEDIUM.rank


@pytest.mark.parametrize("dialect", DIALECTS)
def test_soft_delete_filter_only_bridge_stays_low(dialect):
    # Correct behaviour to preserve: a soft-deletable table used only to filter, not listed, is LOW.
    sql = "SELECT o.id, o.total FROM orders o JOIN customers c ON c.id = o.customer_id"
    fs = _findings(sql, "soft_deleted_rows", dialect, soft_delete_column="deleted_at")
    assert fs and max(f.severity.rank for f in fs) == Severity.LOW.rank
