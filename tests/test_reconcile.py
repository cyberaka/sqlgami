"""Reconciliation probes: anchor queries with a declared relation to the candidate's result."""

from __future__ import annotations

import json

import pytest

from sqlgami import Options, Scorer
from sqlgami import reconcile as R
from sqlgami.report import Severity

QUESTION = "Total order value per customer"
RIGHT = "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name"
FAN_OUT = (
    "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id "
    "JOIN items i ON i.order_id = o.id GROUP BY c.name"
)
TOTAL = {"probe_sql": "SELECT SUM(total) FROM orders", "relation": "equals_sum", "column": "spend",
         "description": "total of all orders"}
CUSTOMERS = {"probe_sql": "SELECT COUNT(DISTINCT customer_id) FROM orders", "relation": "equals_row_count",
             "description": "customers with orders"}


def _signal(report):
    return report.signals.get("reconciliation") or {}


# ------------------------------------------------------------------ the evaluator


COLUMNS = ["name", "spend", "n"]
ROWS = [("Ada", 150.0, 2), ("Bo", 75.5, 1), ("Cy", 40.0, 2)]


@pytest.mark.parametrize(
    "spec,anchor,status,observed",
    [
        (R.ProbeSpec("x", "equals_row_count"), 3, R.SATISFIED, 3),
        (R.ProbeSpec("x", "equals_row_count"), 4, R.VIOLATED, 3),
        (R.ProbeSpec("x", "equals_sum", "spend"), 265.5, R.SATISFIED, 265.5),
        (R.ProbeSpec("x", "equals_sum", "SPEND"), 265.49, R.SATISFIED, 265.5),  # within rounding tolerance
        (R.ProbeSpec("x", "equals_sum", "spend"), 300, R.VIOLATED, 265.5),
        (R.ProbeSpec("x", "equals_sum", "spend", {"name": "Ada"}), 150, R.SATISFIED, 150.0),
        (R.ProbeSpec("x", "equals_count_distinct", "n"), 2, R.SATISFIED, 2),
        (R.ProbeSpec("x", "equals_value", "spend", {"name": "Bo"}), 75.5, R.SATISFIED, 75.5),
        (R.ProbeSpec("x", "equals_value", "spend", {"name": "Bo"}), 80, R.VIOLATED, 75.5),
        (R.ProbeSpec("x", "equals_max", "spend"), 150, R.SATISFIED, 150.0),
        (R.ProbeSpec("x", "equals_min", "n"), 1, R.SATISFIED, 1.0),
        (R.ProbeSpec("x", "upper_bound", "spend"), 200, R.SATISFIED, 150.0),
        (R.ProbeSpec("x", "upper_bound", "spend"), 100, R.VIOLATED, 150.0),
        (R.ProbeSpec("x", "lower_bound", "spend"), 50, R.VIOLATED, 40.0),
        (R.ProbeSpec("x", "equals_value", "spend", {"name": "Zed"}), 1, R.NOT_APPLICABLE, None),
        (R.ProbeSpec("x", "equals_sum", "nope"), 1, R.NOT_APPLICABLE, None),
        (R.ProbeSpec("x", "equals_sum", "name"), 1, R.NOT_APPLICABLE, None),  # not numbers
        (R.ProbeSpec("x", "equals_row_count"), "seven", R.NOT_APPLICABLE, 3),
    ],
)
def test_relations(spec, anchor, status, observed):
    got_status, got_observed, _ = R.evaluate_relation(spec, anchor, COLUMNS, ROWS)
    assert got_status == status
    if observed is not None:
        assert got_observed == pytest.approx(observed)


def test_specs_are_parsed_from_json_and_malformed_ones_dropped():
    text = json.dumps([TOTAL, {"probe_sql": "SELECT 1", "relation": "equals_sum"}, {"relation": "equals_row_count"}, "junk"])
    specs = R.parse_specs(R.extract_json("Here you go:\n```json\n" + text + "\n```"), source="m")
    assert [s.relation for s in specs] == ["equals_sum"] and specs[0].source == "m"
    assert R.parse_specs(["not json"]) == []
    assert R.ProbeSpec("a", "equals_sum", "Spend", {"Name": "Ada"}).target == R.ProbeSpec("b", "equals_sum", "spend", {"name": "Ada"}).target


# ------------------------------------------------------------------ through the scorer


def test_satisfied_probes_support_and_violated_probes_catch(scorer):
    alone = scorer.sample(RIGHT, QUESTION)
    backed = scorer.sample(RIGHT, QUESTION, probes=[TOTAL, CUSTOMERS])
    assert backed.stage("reconciliation").status.value == "passed"
    assert _signal(backed)["satisfied"] == 2 and _signal(backed)["violated"] == 0
    assert backed.score > alone.score
    assert not [f for f in backed.findings if f.stage == "reconciliation"]

    caught = scorer.sample(FAN_OUT, QUESTION, probes=[TOTAL, CUSTOMERS])
    finding = next(f for f in caught.findings if f.code == "reconciliation_mismatch")
    assert finding.severity == Severity.MEDIUM  # one source
    assert _signal(caught)["violated"] == 1 and _signal(caught)["satisfied"] == 1


def test_two_sources_agreeing_on_a_violated_anchor_is_high(scorer):
    probes = [dict(TOTAL, source="model-a"), dict(TOTAL, source="model-b")]
    caught = scorer.sample(FAN_OUT, QUESTION, probes=probes)
    finding = next(f for f in caught.findings if f.code == "reconciliation_mismatch")
    assert finding.severity == Severity.HIGH and caught.score <= 0.4
    assert _signal(caught)["agreed_violations"] == 1


def test_unsafe_or_multi_value_probes_are_not_used(scorer):
    probes = [
        {"probe_sql": "DELETE FROM orders", "relation": "equals_row_count"},
        {"probe_sql": "SELECT total FROM orders", "relation": "equals_sum", "column": "spend"},  # many rows
        {"probe_sql": "SELECT SUM(total) FROM nowhere", "relation": "equals_sum", "column": "spend"},
    ]
    report = scorer.sample(RIGHT, QUESTION, probes=probes)
    statuses = [p["status"] for p in _signal(report)["probes"]]
    assert statuses == ["invalid", "not_applicable", "invalid"]
    assert report.stage("reconciliation").status.value == "skipped"


def test_prober_is_called_in_full_mode_with_the_answer_columns(engine, db):
    seen = {}

    def prober(question, schema, glossary, columns):
        seen["columns"] = list(columns)
        return [TOTAL]

    with Scorer(engine, db=db, prober=prober) as scorer:
        report = scorer.score(RIGHT, QUESTION, mode="full")
        assert seen["columns"] == ["name", "spend"]
        assert _signal(report)["satisfied"] == 1
        assert scorer.sample(RIGHT, QUESTION).stage("reconciliation").status.value == "not_run"


def test_template_parameters_are_filled_into_probes(engine, db):
    with Scorer(engine, db=db) as scorer:
        report = scorer.sample(
            "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id "
            "WHERE c.tenant_id = {tenant} GROUP BY c.name",
            QUESTION,
            params={"tenant": 1},
            probes=[{"probe_sql": "SELECT SUM(o.total) FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.tenant_id = {tenant}",
                     "relation": "equals_sum", "column": "spend"}],
        )
    assert _signal(report)["satisfied"] == 1


def test_weak_sources_anchors_are_notes_and_differing_anchors_are_not_agreement(scorer_factory):
    """Anchors from a source usually wrong: a violated one is a note. Two sources each violating
    with DIFFERENT anchors is not agreement (no HIGH); the same anchor from two reliable sources is."""
    weak_a = {"probe_sql": "SELECT SUM(total) * 2 FROM orders", "relation": "equals_sum", "column": "spend", "source": "weak-a"}
    weak_b = {"probe_sql": "SELECT COUNT(*) FROM orders", "relation": "equals_row_count", "source": "weak-b"}
    with scorer_factory(source_weights={"weak": 0.2, "strong": 0.85}) as scorer:
        noted = scorer.sample(RIGHT, QUESTION, probes=[weak_a, weak_b])
        finding = next(f for f in noted.findings if f.code == "reconciliation_mismatch")
        assert finding.severity == Severity.LOW
        strong = [dict(weak_a, source="strong-a"), dict(weak_b, source="strong-b")]
        doubted = scorer.sample(RIGHT, QUESTION, probes=strong)
        assert next(f for f in doubted.findings if f.code == "reconciliation_mismatch").severity == Severity.MEDIUM
        agreed = [dict(weak_a, source="strong-a"), dict(weak_a, source="strong-b")]
        verdict = scorer.sample(RIGHT, QUESTION, probes=agreed)
        assert next(f for f in verdict.findings if f.code == "reconciliation_mismatch").severity == Severity.HIGH
        weak_agreed = [dict(weak_a, source="weak-a"), dict(weak_a, source="weak-b")]
        still = scorer.sample(RIGHT, QUESTION, probes=weak_agreed)
        assert next(f for f in still.findings if f.code == "reconciliation_mismatch").severity == Severity.MEDIUM
