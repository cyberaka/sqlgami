"""The consistency stage: comparing the candidate's result with independently written queries."""

from __future__ import annotations

import pytest

from sqlgami import Options, Scorer, Severity
from sqlgami.consistency import fingerprint

RIGHT = "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name"
SAME_1 = (
    "WITH t AS (SELECT customer_id, SUM(total) AS s FROM orders GROUP BY customer_id) "
    "SELECT c.name, t.s FROM customers c JOIN t ON t.customer_id = c.id"
)
SAME_2 = (
    "SELECT c.name, (SELECT SUM(o.total) FROM orders o WHERE o.customer_id = c.id) AS spend FROM customers c "
    "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)"
)
REORDERED = "SELECT SUM(o.total) AS spend, c.name FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name"
FANNED = (
    "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id "
    "JOIN items i ON i.order_id = o.id GROUP BY c.name"
)
ONLY_PAID = RIGHT.replace("GROUP BY", "WHERE o.status = 'paid' GROUP BY")
QUESTION = "Total order value per customer"


def consistency(report):
    return report.signals["consistency"]


def test_fingerprint_ignores_row_order_and_can_ignore_column_order():
    a = fingerprint([(1, "x"), (2, "y")])
    assert a[0] == fingerprint([(2, "y"), (1, "x")])[0]
    assert a[0] != fingerprint([("x", 1), ("y", 2)])[0]
    assert a[1] == fingerprint([("x", 1), ("y", 2)])[1]
    assert fingerprint([(1.0,), (None,)])[0] == fingerprint([(None,), (1,)])[0]
    assert fingerprint([(1,), (1,)])[0] != fingerprint([(1,)])[0]  # duplicates count


def test_agreement_lifts_the_score(scorer):
    alone = scorer.sample(RIGHT, QUESTION)
    backed = scorer.sample(RIGHT, QUESTION, alternatives=[SAME_1, SAME_2, REORDERED])
    assert backed.stage("consistency").status.value == "passed"
    assert consistency(backed)["comparable"] == 3 and consistency(backed)["agreeing"] == 3
    statuses = [a["status"] for a in consistency(backed)["alternatives"]]
    assert statuses == ["agrees", "agrees", "agrees_columns_reordered"]
    assert not [f for f in backed.findings if f.stage == "consistency"]
    assert backed.score > alone.score and backed.score >= 0.9
    assert "3 of 3 independent alternative queries" in backed.confidence.basis
    assert alone.stage("consistency").status.value == "not_run"


def test_alternatives_that_agree_with_each_other_but_not_the_candidate(scorer):
    report = scorer.sample(FANNED, QUESTION, alternatives=[RIGHT, SAME_1, SAME_2])
    finding = next(f for f in report.findings if f.code == "consistency_disagreement")
    assert finding.severity == Severity.HIGH and finding.evidence["largest_disagreeing_cluster"] == 3
    assert report.verdict.value == "likely_wrong"
    differing = [a for a in consistency(report)["alternatives"] if a["status"] == "differs"]
    assert len(differing) == 3 and len({a["cluster"] for a in differing}) == 1
    assert "same shape" in differing[0]["detail"] or "rows" in differing[0]["detail"]

    # The same disagreement catches a wrong query that no static check objects to.
    quiet = scorer.sample(ONLY_PAID, QUESTION)
    assert quiet.verdict.value == "likely_right"
    caught = scorer.sample(ONLY_PAID, QUESTION, alternatives=[RIGHT, SAME_1, SAME_2])
    assert caught.has("consistency_disagreement") and caught.score <= 0.4


def test_no_consensus_and_one_against_one(scorer):
    split = scorer.sample(RIGHT, QUESTION, alternatives=[FANNED, ONLY_PAID])
    finding = next(f for f in split.findings if f.code == "consistency_no_consensus")
    assert finding.severity == Severity.MEDIUM and "ambiguous" in finding.message
    assert split.verdict.value == "uncertain"

    single = scorer.sample(RIGHT, QUESTION, alternatives=[ONLY_PAID])
    assert next(f for f in single.findings if f.code == "consistency_disagreement").severity == Severity.MEDIUM
    assert single.verdict.value == "uncertain"

    majority = scorer.sample(RIGHT, QUESTION, alternatives=[SAME_1, SAME_2, FANNED])
    assert not [f for f in majority.findings if f.stage == "consistency"]
    assert consistency(majority)["agreeing"] == 2


def test_unusable_alternatives_are_never_run(engine, db):
    with Scorer(engine, db=db) as scorer:
        report = scorer.sample(
            RIGHT,
            QUESTION,
            alternatives=[
                "DELETE FROM customers",
                "SELECT nope FROM customers",
                "SELECT * FROM read_csv('/etc/passwd')",
                "SELEC broken",
                "SELECT 1; DROP TABLE payments",
                RIGHT,  # the candidate's own text is no evidence
                RIGHT.lower() + ";",
                SAME_1,
                None,
                "",
            ],
        )
        signal = consistency(report)
        assert signal["comparable"] == 1 and signal["agreeing"] == 1
        assert [a["status"] for a in signal["alternatives"]].count("invalid") == 5
    assert tuple(db.execute("SELECT (SELECT COUNT(*) FROM customers), (SELECT COUNT(*) FROM payments)").fetchone()) == (4, 4)


def test_stage_is_skipped_when_nothing_can_be_compared(scorer):
    nothing = scorer.sample(RIGHT, QUESTION, alternatives=["SELECT nope FROM customers"])
    assert nothing.stage("consistency").status.value == "skipped"
    assert not [f for f in nothing.findings if f.stage == "consistency"]
    same = scorer.sample(RIGHT, QUESTION, alternatives=[RIGHT])
    assert same.stage("consistency").status.value == "skipped"
    broken = scorer.sample("SELECT nme FROM customers", QUESTION, alternatives=[RIGHT])
    assert broken.stage("consistency").status.value == "not_run"
    assert scorer.explain(RIGHT, QUESTION, ).stage("consistency").status.value == "not_run"


def test_large_results_are_not_compared(engine, db):
    with Scorer(engine, db=db, options=Options(consistency_rows=2)) as scorer:
        report = scorer.sample(RIGHT, QUESTION, alternatives=[SAME_1])
    assert report.stage("consistency").status.value == "skipped"
    assert "too many to compare" in report.stage("consistency").detail


def test_generator_is_called_only_in_full_mode(engine, db):
    calls = []

    def generator(question, schema, glossary, k):
        calls.append((question, schema, glossary, k))
        return [SAME_1, SAME_2][:k]

    with Scorer(engine, db=db, generator=generator, options=Options(consistency_k=2)) as scorer:
        assert scorer.sample(RIGHT, QUESTION).stage("consistency").status.value == "not_run"
        assert not calls
        report = scorer.score(RIGHT, QUESTION, mode="full")
        assert consistency(report)["agreeing"] == 2 and report.score >= 0.9
        assert len(calls) == 1 and calls[0][0] == QUESTION and calls[0][3] == 2
        assert "customers" in calls[0][1] and "PRIMARY KEY" in calls[0][1]
        assert scorer.score(RIGHT, mode="full").stage("consistency").status.value == "not_run"  # no question


def test_a_failing_generator_does_not_break_scoring(engine, db):
    def generator(question, schema, glossary, k):
        raise RuntimeError("upstream model unavailable: secret-token-123")

    with Scorer(engine, db=db, generator=generator) as scorer:
        report = scorer.score(RIGHT, QUESTION, mode="full")
    stage = report.stage("consistency")
    assert stage.status.value == "skipped" and "RuntimeError" in stage.detail
    assert "secret-token-123" not in report.to_json()
    assert report.verdict.value == "likely_right"


def test_full_mode_without_a_generator_says_so(scorer):
    report = scorer.score(RIGHT, QUESTION, mode="full")
    stage = report.stage("consistency")
    assert stage.status.value == "not_run" and "no alternative queries" in stage.detail
