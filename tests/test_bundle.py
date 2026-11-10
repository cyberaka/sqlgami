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
        # the reference's own values use the stored fingerprints; other values re-run the reference live
        same = scorer.explain("SELECT SUM(total) FROM orders WHERE customer_id = {customer}", "Spend of one customer", params={"customer": 1})
        assert same.signals["fixtures"]["verdict"] == "match" and same.signals["fixtures"]["reference_rerun"] is False
        other = scorer.explain("SELECT SUM(total) FROM orders WHERE customer_id = {customer}", "Spend of one customer", params={"customer": 2})
        assert other.signals["fixtures"]["verdict"] == "match" and other.signals["fixtures"]["reference_rerun"] is True
        assert other.signals["fixtures"]["params"] == {"customer": 2}
        wrong = scorer.explain("SELECT SUM(total) FROM orders WHERE customer_id = {customer} AND status = 'paid'", "Spend of one customer", params={"customer": 2})
        assert wrong.signals["fixtures"]["verdict"] == "mismatch"
        none = scorer.explain("SELECT COUNT(*) FROM orders", "a question nobody registered")
        assert none.stage("fixtures").status == StageStatus.NOT_RUN and "no reference" in (none.stage("fixtures").detail or "")
        assert none.verdict.value != "likely_right"


def test_bundle_hash_names_the_content_and_reports_carry_it(bundle_dir, tmp_path):
    from sqlgami.bundle import bundle_hash
    import shutil

    loaded = Bundle.load(bundle_dir)
    assert loaded.hash == bundle_hash(bundle_dir) and len(loaded.hash) == 64
    copy = tmp_path / "elsewhere"
    shutil.copytree(bundle_dir, copy)
    assert bundle_hash(copy) == loaded.hash  # location and timestamps do not matter
    (copy / "review").mkdir(exist_ok=True)
    (copy / "review" / "Terms.additions.csv").write_text("term,phrase,evidence\n")
    assert bundle_hash(copy) == loaded.hash  # the review file is not read live
    (copy / "references.json").write_text((copy / "references.json").read_text().replace("Total order value", "Total order worth"))
    assert bundle_hash(copy) != loaded.hash
    with Scorer("duckdb", catalog=SHOP_DDL, bundle=bundle_dir) as scorer:
        assert scorer.explain(RIGHT, QUESTION).signals["fixtures"]["bundle"] == loaded.hash[:16]


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


def test_pattern_matching_with_slots(tmp_path):
    """Hosts register a reference per question PATTERN: the same terms with other numbers and
    parameters must find it, and the reference, written over {n}, is re-run for those values."""
    (tmp_path / "schema.sql").write_text(SHOP_DDL)
    terms = tmp_path / "terms"
    terms.mkdir()
    (terms / "Terms.csv").write_text(
        "term,phrases,kind,table,sql,nulls,description\n"
        "paid,paid,filter,orders,status = 'paid',,\n"
        "top_n,top {n};largest {n},,,,,documentation: the n largest\n"
        "spend,spend;order value,,,,,documentation\n"
    )
    top3_sql = "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid' GROUP BY c.name ORDER BY spend DESC LIMIT {n}"
    fixed_sql = "SELECT id FROM orders WHERE status = 'paid' ORDER BY total DESC LIMIT 2"
    (terms / "Examples.csv").write_text(
        "id,question,params,correct_sql,expected_result,dataset\n"
        f'top3,"The top 3 customers by paid spend",,"{top3_sql}",,\n'
        f'fixed,"The top 2 paid orders",,"{fixed_sql}",,\n'
    )
    (tmp_path / "sqlgami.yaml").write_text("dialect: duckdb\nschema: schema.sql\nterms: terms\nfixtures: {seed: 2, profiles: [clean, ties], values: {orders.status: [paid, open]}}\n")
    compile_bundle(tmp_path / "sqlgami.yaml", tmp_path / "bundle")
    bundle = Bundle.load(tmp_path / "bundle")
    top3 = bundle.reference_for(None, "top3")[0]
    assert top3.terms == ("paid", "spend", "top_n") and top3.slots == {"n": 3} and top3.slot_names_in_sql == {"n"}
    with Scorer("duckdb", catalog=SHOP_DDL, bundle=tmp_path / "bundle") as scorer:
        # a paraphrase with another n: matched by pattern, reference re-run with n = 5
        five = scorer.explain(
            "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid' GROUP BY c.name ORDER BY spend DESC LIMIT 5",
            "Which are the largest 5 customers by paid order value?",
        )
        signal = five.signals["fixtures"]
        assert signal["matched_by"] == "pattern" and signal["slots"] == {"n": 5} and signal["reference_rerun"] is True
        assert signal["verdict"] == "match", signal
        wrong = scorer.explain(
            "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name ORDER BY spend DESC LIMIT 5",
            "Which are the largest 5 customers by paid order value?",
        )
        assert wrong.signals["fixtures"]["verdict"] == "mismatch"
        # a question that does not say its N is not compared under the reference's N
        silent = scorer.explain(
            "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid' GROUP BY c.name ORDER BY spend DESC LIMIT 3",
            "The top paid customers by order value",
        )
        assert silent.stage("fixtures").status == StageStatus.NOT_RUN  # another pattern (no top_n term), or an unbound slot: never the reference's N
        # a reference fixed at one value cannot serve another: not run, never guessed
        report = scorer.explain("SELECT id FROM orders WHERE status = 'paid' ORDER BY total DESC LIMIT 4", "The top 4 paid orders")
        assert report.stage("fixtures").status == StageStatus.NOT_RUN and "no reference for these values" in (report.stage("fixtures").detail or "")
        # a number no term binds must agree, and the reason names the number and its wording;
        # "both" and "one" are prose
        prose = scorer.explain(
            "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid' GROUP BY c.name ORDER BY spend DESC LIMIT 3",
            "The top 3 customers by paid spend, both name and spend, in one list, so if four customers tie all four appear",
        )
        detail = prose.stage("fixtures").detail or ""
        assert prose.stage("fixtures").status == StageStatus.NOT_RUN, detail
        assert '4 in "list so if four customers tie"' in detail and '4 in "customers tie all four appear"' in detail and "top3 states none" in detail, detail
        assert "3 in" not in detail  # the 3 is bound by top_n; "both" and "one" are not numbers
        assert "reword the reference" in detail


def test_pattern_is_closed_over_what_the_catalogue_declares():
    """A KPI's ``requires`` terms are part of saying the KPI, and a long phrase says the shorter
    phrases it contains: both sides of a pattern comparison see the same closure."""
    from sqlgami.terms import Catalogue

    cat = Catalogue.from_dict(
        {
            "terms": [{"term": "paid", "phrases": ["paid"], "kind": "filter", "table": "orders", "sql": "status = 'paid'"}],
            "kpis": [
                {"kpi": "revenue", "phrases": ["revenue", "paid revenue"], "formula": "SUM({total})", "requires": ["paid"]},
                {"kpi": "revenue_growth", "phrases": ["paid revenue growth"], "formula": "({now} - {prior}) / {prior}", "requires": ["revenue"]},
            ],
        }
    )
    assert cat.pattern("Total revenue per customer") == ("paid", "revenue")  # requires closure
    assert cat.pattern("Total paid revenue per customer") == ("paid", "revenue")
    assert cat.pattern("Paid revenue growth per customer") == ("paid", "revenue", "revenue_growth")  # contained phrases
    assert cat.pattern("Orders per customer") == ()
    assert [m.name for m in cat.match("Paid revenue growth per customer")] == ["revenue_growth"]  # judging still sees the longest phrase only
    # the identifiers a question names are part of its pattern: "revenue per customer" and
    # "revenue per customer, returning customer_id and region_code" are not the same question
    assert cat.pattern("Total revenue per customer, returning customer_id and region_code") == ("id:customer_id", "id:region_code", "paid", "revenue")


def test_a_paraphrase_naming_other_columns_is_not_routed_to_a_smaller_pattern(tmp_path):
    """A question whose only catalogued words are "active headcount of my organisation" must not be
    compared under a reference for that bare question when it also names lvl_3_id and a rank."""
    (tmp_path / "schema.sql").write_text(SHOP_DDL)
    terms = tmp_path / "terms"
    terms.mkdir()
    (terms / "Terms.csv").write_text("term,phrases,kind,table,sql,nulls,description\npaid,paid,filter,orders,status = 'paid',,\n")
    (terms / "Examples.csv").write_text(
        "id,question,params,correct_sql,expected_result,dataset\n"
        'total,How many paid orders are there?,,"SELECT COUNT(*) FROM orders WHERE status = \'paid\'",,\n'
    )
    (tmp_path / "sqlgami.yaml").write_text("dialect: duckdb\nschema: schema.sql\nterms: terms\nfixtures: {seed: 2, profiles: [clean], values: {orders.status: [paid, open]}}\n")
    compile_bundle(tmp_path / "sqlgami.yaml", tmp_path / "bundle")
    with Scorer("duckdb", catalog=SHOP_DDL, bundle=tmp_path / "bundle") as scorer:
        per_customer = "SELECT customer_id, COUNT(*) AS n FROM orders WHERE status = 'paid' GROUP BY customer_id"
        report = scorer.explain(per_customer, "Paid orders by customer: return customer_id and the count")
        assert report.stage("fixtures").status == StageStatus.NOT_RUN and "no reference" in (report.stage("fixtures").detail or "")
        assert not report.has("fixture_mismatch")
        bare = scorer.explain("SELECT COUNT(*) FROM orders WHERE status = 'paid'", "Count the paid orders")
        assert bare.signals["fixtures"]["verdict"] == "match" and bare.signals["fixtures"]["matched_by"] == "pattern"


def test_a_reference_names_its_own_slot_and_a_question_bound_through_the_term_still_serves_it(tmp_path):
    """Two references write the same notion under different placeholder names ({n_months} and
    {n_months_back}); the term phrase binds one name. A reference's templated question records the
    alias, so a candidate's number reaches the reference under the reference's own name — and a
    number the question states for a template parameter the caller did not pass is that
    parameter's value, never the reference's default."""
    (tmp_path / "schema.sql").write_text(SHOP_DDL)
    terms = tmp_path / "terms"
    terms.mkdir()
    (terms / "Terms.csv").write_text(
        "term,phrases,kind,table,sql,nulls,description\n"
        "paid,paid,filter,orders,status = 'paid',,\n"
        "top_n,top {top_n};largest {top_n},,,,,documentation: the n largest\n"
        "spend,spend;order value,,,,,documentation\n"
    )
    (terms / "Parameters.csv").write_text("name,type,example_values\ntop_n,integer,3\n")
    by_limit = "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid' GROUP BY c.name ORDER BY spend DESC LIMIT {how_many}"
    (terms / "Examples.csv").write_text(
        "id,question,params,correct_sql,expected_result,dataset\n"
        f'topk,"The top {{how_many}} customers by paid spend",how_many=3,"{by_limit}",,\n'
    )
    (tmp_path / "sqlgami.yaml").write_text("dialect: duckdb\nschema: schema.sql\nterms: terms\nfixtures: {seed: 2, profiles: [clean, ties], values: {orders.status: [paid, open]}}\n")
    compile_bundle(tmp_path / "sqlgami.yaml", tmp_path / "bundle")
    bundle = Bundle.load(tmp_path / "bundle")
    ref = bundle.reference_for(None, "topk")[0]
    assert ref.slot_aliases == {"top_n": "how_many"} and ref.params == {"how_many": 3} and "how_many" in ref.slot_names_in_sql
    assert ref.own_slots({"top_n": 5}) == {"how_many": 5}
    with Scorer("duckdb", catalog=SHOP_DDL, bundle=tmp_path / "bundle") as scorer:
        right = "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid' GROUP BY c.name ORDER BY spend DESC LIMIT 5"
        report = scorer.explain(right, "Which are the largest 5 customers by paid order value?")
        signal = report.signals["fixtures"]
        assert signal["verdict"] == "match" and signal["params"] == {"how_many": 5} and signal["reference_rerun"] is True, signal
        # the same statement for a question saying 3 — compared at 3, where LIMIT 5 is wrong
        report = scorer.explain(right, "Which are the largest 3 customers by paid order value?")
        assert report.signals["fixtures"]["verdict"] == "mismatch"
