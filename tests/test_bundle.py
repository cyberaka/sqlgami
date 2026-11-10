"""The compiled bundle: fixtures plus reference fingerprints, built offline, loaded live."""

from __future__ import annotations

import json
import time

import pytest

from conftest import SHOP_DDL
from sqlgami import Options, Scorer
from sqlgami.bundle import Bundle, compile_bundle, schema_hash
from sqlgami.errors import ConfigurationError
from sqlgami.report import Severity, StageStatus

duckdb = pytest.importorskip("duckdb")

RIGHT = "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name"
FANNED = RIGHT.replace("GROUP BY", "JOIN items i ON i.order_id = o.id GROUP BY")
PAID = RIGHT.replace("GROUP BY", "WHERE o.status = 'paid' GROUP BY")
QUESTION = "Total order value per customer"


@pytest.fixture(scope="module")
def bundle_dir(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("bundle")
    (tmp / "schema.sql").write_text(SHOP_DDL)
    terms = tmp / "terms"
    terms.mkdir()
    (terms / "Terms.csv").write_text("term,phrases,kind,table,sql,nulls,description\npaid,paid,filter,orders,status = 'paid',,\nspend,order value;spend,,,,,documentation\n")
    (terms / "Parameters.csv").write_text("name,type,example_values\ncustomer,integer,1\n")
    (terms / "Examples.csv").write_text(
        "id,question,params,correct_sql,expected_result,dataset\n"
        f'ex1,{QUESTION},,"{RIGHT}",,\n'
        "ex2,How many paid orders are there?,,\"SELECT COUNT(*) FROM orders WHERE status = 'paid'\",,\n"
        "ex3,Spend of one customer,customer=1,\"SELECT SUM(total) FROM orders WHERE customer_id = {customer}\",,\n"
        'bad,Broken reference,,"SELECT nope FROM orders",,\n'
    )
    (tmp / "sqlgami.yaml").write_text(
        "dialect: duckdb\nschema: schema.sql\nterms: terms\n"
        "fixtures: {seed: 1, profiles: [clean, nulls, duplicates, sparse], values: {orders.status: [paid, open, cancelled]}, params: {customer: [1]}, param_columns: {customer: [orders.customer_id]}}\n"
    )
    compile_bundle(tmp / "sqlgami.yaml", tmp / "bundle")
    return tmp / "bundle"


def test_compile_writes_a_reviewable_bundle_and_rejects_a_failing_reference(bundle_dir):
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    assert manifest["references"] == 3 and manifest["rejected"] == 1
    assert {e["name"] for e in manifest["fixtures"]} == {"clean-s1", "nulls-s1", "duplicates-s1", "sparse-s1"}
    assert all(e["reference_ms"] is not None for e in manifest["fixtures"])
    rejected = [json.loads(l) for l in (bundle_dir / "rejected.jsonl").read_text().splitlines()]
    assert rejected[0]["key"] == "bad" and "nope" in rejected[0]["problems"][0]
    assert (bundle_dir / "schema.sql").exists() and (bundle_dir / "terms" / "Terms.csv").exists()
    for entry in manifest["fixtures"]:
        assert (bundle_dir / "fixtures" / entry["name"] / "fixture.json").exists()
    bundle = Bundle.load(bundle_dir)
    assert bundle.reference_for(QUESTION)[1] == "question" and bundle.reference_for(None, "ex2")[1] == "key"
    assert bundle.reference_for("Something else")[0] is None
    assert bundle.terms_path() is not None


@pytest.mark.parametrize(
    "sql,verdict,matched",
    [(RIGHT, "match", 4), (FANNED, "mismatch", 0), (PAID, "mismatch", 0)],
)
def test_candidates_are_compared_with_the_reference_on_the_fixtures(bundle_dir, sql, verdict, matched):
    with Scorer("duckdb", catalog=SHOP_DDL, bundle=bundle_dir) as scorer:
        report = scorer.explain(sql, QUESTION)
    signal = report.signals["fixtures"]
    assert signal["verdict"] == verdict and signal["matched_by"] == "question"
    assert report.stage("fixtures").status == StageStatus.PASSED
    if verdict == "match":
        assert signal["matched"] == matched and report.verdict.value == "likely_right" and report.score >= 0.9
        assert "fixture dataset" in report.confidence.basis
    else:
        finding = next(f for f in report.findings if f.code == "fixture_mismatch")
        assert finding.severity == Severity.HIGH and report.verdict.value == "likely_wrong"
        assert finding.evidence["differing"]["only_in_candidate"] or finding.evidence["differing"]["only_in_reference"]
        assert [f["status"] for f in signal["fixtures"]] == ["mismatch"]  # early exit: a mismatch is a verdict


def test_reference_by_key_and_parameters_and_no_reference(bundle_dir):
    with Scorer("duckdb", catalog=SHOP_DDL, bundle=bundle_dir) as scorer:
        by_key = scorer.explain("SELECT COUNT(*) FROM orders WHERE status = 'paid'", "paid orders, worded differently", reference="ex2")
        assert by_key.signals["fixtures"]["verdict"] == "match" and by_key.signals["fixtures"]["matched_by"] == "key"
        # the candidate runs under the reference's parameter values, whatever the caller passed
        with_params = scorer.explain("SELECT SUM(total) FROM orders WHERE customer_id = {customer}", "Spend of one customer", params={"customer": 999})
        assert with_params.signals["fixtures"]["verdict"] == "match" and with_params.signals["fixtures"]["params"] == {"customer": 1}
        none = scorer.explain("SELECT COUNT(*) FROM orders", "a question nobody registered")
        assert none.stage("fixtures").status == StageStatus.NOT_RUN and "no reference" in (none.stage("fixtures").detail or "")
        assert none.verdict.value != "likely_right"


def test_bundle_is_tied_to_the_schema_and_the_dialect(bundle_dir):
    with pytest.raises(ConfigurationError, match="different schema"):
        Scorer("duckdb", catalog=SHOP_DDL + "\nCREATE TABLE extra (id INTEGER);", bundle=bundle_dir)
    with pytest.raises(ConfigurationError, match="compiled for duckdb"):
        Scorer("sqlite", catalog=SHOP_DDL, bundle=bundle_dir)
    assert schema_hash(Scorer("duckdb", catalog=SHOP_DDL).catalog) == Bundle.load(bundle_dir).schema_hash


def test_fixtures_respect_the_deadline(bundle_dir):
    with Scorer("duckdb", catalog=SHOP_DDL, bundle=bundle_dir, options=Options(deadline_ms=0.0001)) as scorer:
        report = scorer.explain(RIGHT, QUESTION)
    assert report.stage("fixtures").status == StageStatus.SKIPPED
    assert "fixtures" in report.timing["cut_by_deadline"]
    assert report.verdict.value != "likely_right"
    with Scorer("duckdb", catalog=SHOP_DDL, bundle=bundle_dir) as scorer:  # the default 50 ms is enough here
        scorer.explain(RIGHT, QUESTION)
        started = time.perf_counter()
        report = scorer.explain(RIGHT, QUESTION)
        assert (time.perf_counter() - started) * 1000 < 50 + 250
        assert report.signals["fixtures"]["matched"] == 4
