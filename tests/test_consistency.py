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
    assert "3 of 3 alternative queries, from 3 independent source(s)" in backed.confidence.basis
    assert alone.stage("consistency").status.value == "not_run"


@pytest.fixture
def judge(engine, db):
    with Scorer(engine, db=db, options=Options(consistency_role="judge")) as s:
        yield s


def test_by_default_disagreement_is_a_doubt_not_a_verdict(scorer):
    report = scorer.sample(RIGHT, QUESTION, alternatives=[FANNED, FANNED + " ORDER BY 1", ONLY_PAID])
    finding = next(f for f in report.findings if f.code == "consistency_disagreement")
    assert finding.severity == Severity.MEDIUM and finding.evidence["role"] == "confirm"
    assert "depends on how capable" in finding.message
    assert report.verdict.value == "uncertain"


def test_alternatives_that_agree_with_each_other_but_not_the_candidate(judge):
    scorer = judge
    report = scorer.sample(FANNED, QUESTION, alternatives=[RIGHT, SAME_1, SAME_2])
    finding = next(f for f in report.findings if f.code == "consistency_disagreement")
    assert finding.severity == Severity.HIGH and finding.evidence["largest_disagreeing_cluster_sources"] == 3
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
        assert consistency(report)["agreeing_sources"] == 2  # unlabelled alternatives are each their own voice
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


def test_each_source_votes_with_its_most_common_answer(judge):
    # Model a: three samples say RIGHT, one says FANNED. Model b: the same. The stray samples
    # happen to coincide, but each model's vote is its majority, which supports the candidate.
    alternatives = [
        (SAME_1, "a"), (SAME_2, "a"), (REORDERED, "a"), (FANNED, "a"),
        (SAME_1, "b"), (SAME_2, "b"), (FANNED + " ORDER BY 1", "b"),
    ]
    report = judge.sample(RIGHT, QUESTION, alternatives=alternatives)
    signal = consistency(report)
    assert signal["sources"] == 2 and signal["agreeing_sources"] == 2
    assert signal["largest_disagreeing_cluster_sources"] == 0
    assert not [f for f in report.findings if f.stage == "consistency"]
    # Turned round: the candidate matches only the stray samples.
    against = judge.sample(FANNED, QUESTION, alternatives=[(a, s) for a, s in alternatives if a != FANNED] + [(FANNED + " ORDER BY 2", "a")])
    assert consistency(against)["agreeing_sources"] == 0
    assert next(f for f in against.findings if f.code == "consistency_disagreement").severity == Severity.HIGH


def test_samples_from_one_source_are_one_voice(judge):
    scorer = judge
    # Three samples of one model agree with each other and not with the candidate: a doubt, not a verdict.
    one_model = scorer.sample(
        FANNED, QUESTION, alternatives=[(RIGHT, "model-a"), (SAME_1, "model-a"), (SAME_2, "model-a")]
    )
    finding = next(f for f in one_model.findings if f.code == "consistency_disagreement")
    assert finding.severity == Severity.MEDIUM and "one other source" in finding.message
    assert consistency(one_model)["sources"] == 1 and consistency(one_model)["comparable"] == 3
    assert one_model.verdict.value != "invalid"

    # The same three queries from two different models: independent agreement, high severity.
    two_models = scorer.sample(
        FANNED, QUESTION, alternatives=[(RIGHT, "model-a"), (SAME_1, "model-b"), {"sql": SAME_2, "source": "model-a"}]
    )
    finding = next(f for f in two_models.findings if f.code == "consistency_disagreement")
    assert finding.severity == Severity.HIGH and finding.evidence["largest_disagreeing_cluster_sources"] == 2

    # Support counts the same way: three agreeing samples of one model lift less than two models.
    same_source = scorer.sample(RIGHT, QUESTION, alternatives=[(SAME_1, "model-a"), (SAME_2, "model-a")])
    two_sources = scorer.sample(RIGHT, QUESTION, alternatives=[(SAME_1, "model-a"), (SAME_2, "model-b")])
    assert consistency(same_source)["agreeing_sources"] == 1 and consistency(two_sources)["agreeing_sources"] == 2
    assert scorer.sample(RIGHT, QUESTION).score < same_source.score < two_sources.score


def test_combined_generators_tag_their_alternatives(engine, db):
    from sqlgami.consistency import combine

    def model_a(question, schema, glossary, k):
        return [SAME_1, SAME_1 + " ORDER BY c.name"][:k]

    def model_b(question, schema, glossary, k):
        return [SAME_2]

    def broken(question, schema, glossary, k):
        raise RuntimeError("down")

    generator = combine(model_a, model_b, broken, names=["a", "b", "c"])
    with Scorer(engine, db=db, generator=generator, options=Options(consistency_k=2)) as scorer:
        report = scorer.score(RIGHT, QUESTION, mode="full")
    signal = consistency(report)
    assert signal["sources"] == 2 and signal["agreeing_sources"] == 2 and signal["comparable"] == 3
    assert {a["source"] for a in signal["alternatives"]} == {"a", "b"}


# ------------------------------------------------------------------ source weights


def test_two_reliable_sources_agreeing_against_the_candidate_is_a_verdict(engine, db):
    weights = {"strong-a": 0.85, "strong-b": 0.8, "weak": 0.25}
    with Scorer(engine, db=db, source_weights=weights) as scorer:
        caught = scorer.sample(ONLY_PAID, QUESTION, alternatives=[(RIGHT, "strong-a"), (SAME_1, "strong-b")])
        finding = next(f for f in caught.findings if f.code == "consistency_disagreement")
        assert finding.severity == Severity.HIGH and caught.score <= 0.4
        # the same two queries from weak sources: a doubt, as before, not a verdict
        doubted = scorer.sample(ONLY_PAID, QUESTION, alternatives=[(RIGHT, "weak"), (SAME_1, "weak-2")])
        finding = next(f for f in doubted.findings if f.code == "consistency_disagreement")
        assert finding.severity == Severity.MEDIUM


def test_one_weak_source_disagreeing_costs_little(engine, db):
    with Scorer(engine, db=db, source_weights={"weak": 0.25}) as scorer:
        alone = scorer.sample(RIGHT, QUESTION)
        doubted = scorer.sample(RIGHT, QUESTION, alternatives=[(ONLY_PAID, "weak")])
        finding = next(f for f in doubted.findings if f.code == "consistency_disagreement")
        assert finding.severity == Severity.LOW
        assert alone.score - doubted.score < 0.08
    with Scorer(engine, db=db) as unweighted:
        plain = unweighted.sample(RIGHT, QUESTION, alternatives=[(ONLY_PAID, "weak")])
        assert next(f for f in plain.findings if f.code == "consistency_disagreement").severity == Severity.MEDIUM


def test_agreement_from_weak_sources_is_capped(engine, db):
    with Scorer(engine, db=db, source_weights={"strong": 0.85, "weak": 0.25}) as scorer:
        alone = scorer.sample(RIGHT, QUESTION)
        weak = scorer.sample(RIGHT, QUESTION, alternatives=[(SAME_1, "weak-1"), (SAME_2, "weak-2")])
        strong = scorer.sample(RIGHT, QUESTION, alternatives=[(SAME_1, "strong-1"), (SAME_2, "strong-2")])
        assert alone.score < weak.score < strong.score
        assert weak.score - alone.score < 0.12, (alone.score, weak.score)
