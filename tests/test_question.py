"""Question-versus-SQL checks: each pair is one question with a wrong and a right SQL."""

from __future__ import annotations

import pytest

from sqlgami import Scorer, Severity

from conftest import make_db

# (question, wrong SQL, finding code expected on it, right SQL)
PAIRS = [
    (
        "How many customers have placed paid orders?",
        "SELECT COUNT(*) FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid'",
        "count_grain",
        "SELECT COUNT(DISTINCT c.id) FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid'",
    ),
    (
        "What is the total value of paid orders per customer?",
        "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name",
        "question_value_unused",
        "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id "
        "WHERE o.status = 'paid' GROUP BY c.name",
    ),
    (
        "Top 3 customers by total order value",
        "SELECT c.name, SUM(o.total) s FROM customers c JOIN orders o ON o.customer_id = c.id "
        "GROUP BY c.name ORDER BY s DESC LIMIT 5",
        "top_n",
        "SELECT c.name, SUM(o.total) s FROM customers c JOIN orders o ON o.customer_id = c.id "
        "GROUP BY c.name ORDER BY s DESC LIMIT 3",
    ),
    (
        "Which 2 orders have the highest total?",
        "SELECT id, total FROM orders ORDER BY total ASC, id LIMIT 2",
        "order_direction",
        "SELECT id, total FROM orders ORDER BY total DESC, id LIMIT 2",
    ),
    (
        "What is the average order total?",
        "SELECT SUM(total) FROM orders",
        "aggregate_mismatch",
        "SELECT AVG(total) FROM orders",
    ),
    (
        "What is the average order total?",
        "SELECT MAX(total) FROM orders",
        "aggregate_mismatch",
        "SELECT SUM(total) * 1.0 / COUNT(*) FROM orders",
    ),
    (
        "How many orders have a total over 60?",
        "SELECT COUNT(*) FROM orders",
        "number_unused",
        "SELECT COUNT(*) FROM orders WHERE total > 60",
    ),
    (
        "How many orders were created in 2024?",
        "SELECT COUNT(*) FROM orders",
        "time_filter_missing",
        "SELECT COUNT(*) FROM orders WHERE created_at >= '2024-01-01' AND created_at < '2025-01-01'",
    ),
    (
        "How many orders were placed last month?",
        "SELECT COUNT(*) FROM orders",
        "time_filter_missing",
        "SELECT COUNT(*) FROM orders WHERE created_at >= '2024-01-01' AND created_at < '2024-02-01'",
    ),
    (
        "Total order value per customer",
        "SELECT SUM(total) FROM orders",
        "grouping_missing",
        "SELECT customer_id, SUM(total) FROM orders GROUP BY customer_id",
    ),
    (
        "List customers who are not in the US",
        "SELECT name FROM customers WHERE country = 'US'",
        "negation_missing",
        "SELECT name FROM customers WHERE country <> 'US' ORDER BY name",
    ),
    (
        "Which customers have never placed an order?",
        "SELECT DISTINCT c.name FROM customers c JOIN orders o ON o.customer_id = c.id",
        "negation_missing",
        "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.id IS NULL",
    ),
]


@pytest.mark.parametrize("pair", PAIRS, ids=[f"{p[2]}-{i}" for i, p in enumerate(PAIRS)])
def test_question_checks(engine, db, pair):
    question, wrong, code, right = pair
    with Scorer(engine, db=db) as scorer:
        bad = scorer.sample(wrong, question)
        good = scorer.sample(right, question)
    assert bad.has(code), bad.summary()
    assert bad.stage("question").status.value == "passed"
    question_findings = [f for f in good.findings if f.stage == "question"]
    assert not question_findings, good.summary()
    assert good.score > bad.score


def test_no_question_means_no_question_stage(scorer):
    report = scorer.sample("SELECT COUNT(*) FROM orders")
    assert report.stage("question").status.value == "not_run"
    assert not [f for f in report.findings if f.stage == "question"]
    assert scorer.sample("SELECT COUNT(*) FROM orders", "   ").stage("question").status.value == "not_run"


def test_value_linking_needs_sample_mode_or_known_values(engine, db):
    question = "What is the total value of paid orders?"
    sql = "SELECT SUM(total) FROM orders"
    with Scorer(engine, db=db) as scorer:
        assert not scorer.explain(sql, question).has("question_value_unused")
        assert not scorer.dry_run(sql, question).has("question_value_unused")
        assert scorer.sample(sql, question).has("question_value_unused")
        # The profile is remembered, so later static calls benefit from it.
        assert scorer.explain(sql, question).has("question_value_unused")


def test_a_breakdown_by_the_column_is_not_a_missing_filter(scorer):
    report = scorer.sample("SELECT status, COUNT(*) FROM orders GROUP BY status", "Show paid and open order counts by status")
    assert not report.has("question_value_unused"), report.summary()


def test_short_codes_match_only_as_capitalised_words(scorer):
    # 'IN' and 'DE' are country values; the lower-case words "in" and "de" must not match them.
    report = scorer.sample("SELECT COUNT(*) FROM customers", "How many customers are in the system?")
    assert not report.has("question_value_unused"), report.summary()
    report = scorer.sample("SELECT COUNT(*) FROM customers", "How many customers are in DE?")
    finding = next(f for f in report.findings if f.code == "question_value_unused")
    assert finding.evidence["value"] == "DE"


def test_filter_the_question_never_asked_for(engine, db):
    sql = "SELECT COUNT(*) FROM orders WHERE status = 'cancelled'"
    with Scorer(engine, db=db) as scorer:
        report = scorer.sample(sql, "How many orders are there?")
        finding = next(f for f in report.findings if f.code == "filter_not_in_question")
        assert finding.severity == Severity.LOW
        assert not scorer.sample(sql, "How many cancelled orders are there?").has("filter_not_in_question")
    with Scorer(engine, db=db, glossary={"dead order": "an order whose status is cancelled"}) as scorer:
        assert not scorer.sample(sql, "How many dead orders are there?").has("filter_not_in_question")


def test_question_checks_do_not_run_on_invalid_sql(scorer):
    report = scorer.sample("SELECT nme FROM customers", "How many customers are there?")
    assert report.stage("question").status.value == "not_run"


def test_scaled_numbers_count_as_used(scorer):
    # A question in whole units against a column stored in cents.
    report = scorer.sample("SELECT COUNT(*) FROM orders WHERE total * 100 > 6000", "How many orders are over 60?")
    assert not report.has("number_unused"), report.summary()
