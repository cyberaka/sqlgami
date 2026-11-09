"""Output shape: the column list, defaults and label formats the question spells out."""

from __future__ import annotations

import pytest

from sqlgami import Scorer
from sqlgami.report import Severity
from sqlgami.shape import parse_column_list

from conftest import SHOP_DDL


def _codes(report, at_least=Severity.LOW):
    return sorted({f.code for f in report.findings_at_least(at_least)})


def _finding(report, code):
    return next((f for f in report.findings if f.code == code), None)


# ------------------------------------------------------------------ the parser


@pytest.mark.parametrize(
    "text,names,ordered",
    [
        ("Return (month_start, tenant_id, mrr_cents) sorted by tenant then month.", ["month_start", "tenant_id", "mrr_cents"], True),
        ("Columns in order: a_id, b_count, c_total.", ["a_id", "b_count", "c_total"], True),
        ("Include these columns in this exact order: tenant_id, tenant_name, rank_within (1 to 3).", ["tenant_id", "tenant_name", "rank_within"], True),
        ("Return tenant_id, name, country_code (UNKNOWN if none), rev_cents, and delta_rank = RANK over delta descending.",
         ["tenant_id", "name", "country_code", "rev_cents", "delta_rank"], False),
        ("Return one row per tenant with: tenant_id, inv_total (0 if only in B), and two flags only_inv/only_pay set when it appears once.",
         ["tenant_id", "inv_total", "only_inv", "only_pay"], False),
        ("Return exactly these columns in order: grouping_level ('quarter' | 'grand'), quarter_label (NULL when grouping_level does not include quarter), rev_cents.",
         ["grouping_level", "quarter_label", "rev_cents"], True),
    ],
)
def test_explicit_column_lists_are_parsed(text, names, ordered):
    listed = parse_column_list(text)
    assert listed is not None
    assert [i.name for i in listed.items] == names
    assert listed.ordered is ordered


def test_defaults_are_read_from_parentheticals():
    listed = parse_column_list(
        "Return tenant_id, country_code (UNKNOWN if the current address has none), top_plan_code "
        "(the open plan with the highest price; NULL if none), n_notifications (0 when there are none), "
        "has_open_sub (1 if the tenant has at least one open subscription, 0 otherwise)."
    )
    defaults = {i.name: i.default for i in listed.items}
    assert defaults["country_code"] == "UNKNOWN" and defaults["top_plan_code"] == "NULL"
    assert defaults["n_notifications"] == "0" and defaults["tenant_id"] is None


def test_prose_is_not_mistaken_for_a_column_list():
    assert parse_column_list("For each tenant return the row with tenant_name, the four columns above, n_events and rk.") is None
    assert parse_column_list("How many customers are in the US?") is None
    assert parse_column_list("List the customers and their orders.") is None


# ------------------------------------------------------------------ the checks


def _explain(sql, question):
    return Scorer("duckdb", catalog=SHOP_DDL).explain(sql, question)


def test_wrong_column_count_against_an_explicit_list():
    report = _explain(
        "SELECT c.id, c.name, c.country, COUNT(o.id) AS n FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id, c.name, c.country",
        "For every customer return (customer_id, name, n_orders).",
    )
    finding = _finding(report, "output_columns")
    assert finding is not None and finding.severity == Severity.MEDIUM
    assert finding.evidence["wanted"] == 3 and finding.evidence["returned"] == 4
    assert finding.evidence["explicit"] is True


def test_right_count_wrong_order_is_noted():
    sql = "SELECT c.name, c.id FROM customers c"
    insisted = _explain(sql, "Return these columns in this exact order: id, name.")
    assert _finding(insisted, "column_order").severity == Severity.MEDIUM
    listed = _explain(sql, "Return id, name for every customer.")
    assert _finding(listed, "column_order").severity == Severity.LOW
    assert "column_order" not in _codes(_explain("SELECT c.id, c.name FROM customers c", "Return (id, name)."))


def test_default_asked_for_but_not_applied():
    question = "For every customer return customer_id, total (0 if the customer has no orders)."
    bare = _explain(
        "SELECT c.id AS customer_id, SUM(o.total) AS total FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id",
        question,
    )
    assert _finding(bare, "column_default").severity == Severity.MEDIUM
    safe = _explain(
        "SELECT c.id AS customer_id, COALESCE(SUM(o.total), 0) AS total FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id",
        question,
    )
    assert "column_default" not in _codes(safe)
    counted = _explain(
        "SELECT c.id AS customer_id, (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS total FROM customers c",
        question,
    )
    assert "column_default" not in _codes(counted)  # a counting subquery is never NULL


def test_null_asked_for_but_a_value_substituted():
    report = _explain(
        "SELECT c.id AS customer_id, COALESCE(MAX(o.total), 0) AS biggest FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id",
        "Return customer_id, biggest (NULL when the customer has no orders).",
    )
    assert _finding(report, "column_default").severity == Severity.LOW


def test_defined_labels_must_not_be_embellished():
    question = "For each quarter (Q1 = Jan-Mar, Q2 = Apr-Jun) return (q_label, total)."
    sql_ok = "SELECT CASE WHEN EXTRACT(month FROM created_at) <= 3 THEN 'Q1' ELSE 'Q2' END AS q_label, SUM(total) AS total FROM orders GROUP BY 1"
    sql_long = "SELECT CASE WHEN EXTRACT(month FROM created_at) <= 3 THEN 'Q1 2024' ELSE 'Q2 2024' END AS q_label, SUM(total) AS total FROM orders GROUP BY 1"
    assert "label_format" not in _codes(_explain(sql_ok, question))
    finding = _finding(_explain(sql_long, question), "label_format")
    assert finding is not None and finding.severity == Severity.MEDIUM


def test_quoted_label_format_must_be_producible():
    question = "Return (period, total) with the period labelled like 'Q1 2024'."
    built = "SELECT 'Q' || CAST(EXTRACT(quarter FROM created_at) AS VARCHAR) || ' ' || CAST(EXTRACT(year FROM created_at) AS VARCHAR) AS period, SUM(total) AS total FROM orders GROUP BY 1"
    assert "label_format" not in _codes(_explain(built, question))
    short = "SELECT 'Q' || CAST(EXTRACT(quarter FROM created_at) AS VARCHAR) AS period, SUM(total) AS total FROM orders GROUP BY 1"
    assert _finding(_explain(short, question), "label_format") is not None


def test_shape_findings_are_not_softened_on_long_questions():
    long_question = (
        "For every customer who placed at least one order in 2024, considering only paid orders and "
        "excluding cancelled ones, and treating customers without a country as UNKNOWN, compute the total "
        "spend and the number of orders, then rank customers by spend descending with ties sharing a rank. "
        "Return (customer_id, name, spend, n_orders, spend_rank) sorted by rank then customer_id."
    )
    report = _explain("SELECT c.id AS customer_id, c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.id, c.name", long_question)
    assert _finding(report, "output_columns").severity == Severity.MEDIUM
