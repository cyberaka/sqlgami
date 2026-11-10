"""Offline expansion: what a model proposes enters the bundle only after deterministic re-matching."""

from __future__ import annotations

import json

import pytest

from conftest import SHOP_DDL
from sqlgami import Scorer
from sqlgami.bundle import Bundle, Reference, compile_bundle
from sqlgami.expand import accept_phrase, accept_variant, expand
from sqlgami.report import StageStatus
from sqlgami.terms import Catalogue

duckdb = pytest.importorskip("duckdb")

TERMS = (
    "term,phrases,kind,table,sql,nulls,description\n"
    "paid,paid,filter,orders,status = 'paid',,an order whose status is paid\n"
    "top_n,top {n};largest {n},rank,,LIMIT {n},,the n largest\n"
    "spend,spend;order value,,,,,documentation\n"
)
TOP3_SQL = "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.status = 'paid' GROUP BY c.name ORDER BY spend DESC LIMIT {n}"
EXAMPLES = (
    "id,question,params,correct_sql,expected_result,dataset\n"
    f'top3,"The top 3 customers by paid spend",,"{TOP3_SQL}",,\n'
    "count_paid,How many paid orders are there?,,\"SELECT COUNT(*) FROM orders WHERE status = 'paid'\",,\n"
)


def _catalogue() -> Catalogue:
    return Catalogue.from_sheets({"terms": TERMS, "examples": EXAMPLES})


def _references(cat: Catalogue) -> list[Reference]:
    refs = []
    for e in cat.examples:
        r = Reference(e.id, e.question, e.correct_sql)
        r.terms = cat.pattern(e.question)
        from sqlgami.bundle import slot_values

        r.slots = slot_values(cat.match(e.question))
        refs.append(r)
    return refs


QUESTIONS = [("top3", "The top 3 customers by paid spend"), ("count_paid", "How many paid orders are there?")]


def test_a_phrase_is_kept_only_when_every_question_re_matches_the_same_way():
    cat = _catalogue()
    kept, evidence = accept_phrase(cat, "paid", "settled", QUESTIONS)
    assert kept is not None and evidence == "top3"
    assert kept.pattern("The top 3 customers by settled spend") == cat.pattern("The top 3 customers by paid spend")
    # a generic word would match questions that are not about the term
    assert accept_phrase(cat, "paid", "customers", QUESTIONS)[1] == "occurs in top3 where it does not stand for the term"
    # a wording that drops the number slot cannot stand for "top {n}"
    assert accept_phrase(cat, "top_n", "the biggest", QUESTIONS)[1] == "placeholders differ from the term's existing wordings"
    kept, evidence = accept_phrase(cat, "top_n", "{n} highest", QUESTIONS)
    assert kept is not None and evidence == "top3"
    assert accept_phrase(cat, "paid", "order value", QUESTIONS)[1] == "already a wording of spend"
    assert accept_phrase(cat, "paid", "paid or not; whatever", QUESTIONS)[1] == "contains punctuation"


def test_a_variant_is_kept_only_when_it_binds_the_same_slots_as_claimed():
    cat = _catalogue()
    top3 = _references(cat)[0]
    ok, why = accept_variant(cat, top3, "Which are the largest 5 customers by paid order value?", {"n": 5}, set())
    assert ok is not None and ok.slots == {"n": 5} and ok.variant_of == "top3" and ok.sql == TOP3_SQL, why
    # the model says 5, the text says 3: never trusted
    assert accept_variant(cat, top3, "The largest 3 customers by paid spend", {"n": 5}, set())[1].startswith("claims slots")
    # a paraphrase that silently drops a term is another question
    assert accept_variant(cat, top3, "The largest 5 customers by spend", {"n": 5}, set())[1] == "does not re-match the reference's pattern"
    # a number the reference does not state
    assert accept_variant(cat, top3, "The top 5 customers by paid spend in 2 regions", {"n": 5}, set())[1] == "states numbers the reference does not"
    fixed = Reference("fixed", "The top 2 paid orders", "SELECT id FROM orders WHERE status = 'paid' ORDER BY total DESC LIMIT 2")
    fixed.terms, fixed.slots = cat.pattern(fixed.question), {"n": 2}
    assert accept_variant(cat, fixed, "The top 4 paid orders", {"n": 4}, set())[1].startswith("the reference is fixed at n=2")


def _fake_chat(messages):
    text = messages[-1]["content"]
    if text.startswith("Term: paid"):
        return json.dumps(["settled", "customers", "fully paid"])
    if text.startswith("Term: top_n"):
        return json.dumps(["{n} highest", "the biggest"])
    if text.startswith("Term: spend"):
        return json.dumps(["amount spent"])
    if text.startswith("Question: The top 3"):
        return json.dumps(
            [
                {"question": "Which are the 5 highest customers by settled order value?", "slots": {"n": 5}},
                {"question": "The largest 3 customers by paid spend", "slots": {"n": 5}},
                {"question": "The top 3 customers by paid spend", "slots": {"n": 3}},
            ]
        )
    if text.startswith("Question: How many paid"):
        return json.dumps([{"question": "Count the settled orders", "slots": {}}])
    return "[]"


def test_expand_accepts_and_rejects_with_reasons():
    cat = _catalogue()
    result = expand(cat, _references(cat), _fake_chat, model="fake", slot_choices={"n": [5, 10]})
    assert [(p.term, p.phrase) for p in result.phrases] == [("paid", "settled"), ("paid", "fully paid"), ("top_n", "{n} highest"), ("spend", "amount spent")]
    assert [(v.variant_of, v.slots) for v in result.variants] == [("top3", {"n": 5}), ("count_paid", {})]
    reasons = {(r["kind"], r["text"]): r["reason"] for r in result.rejected}
    assert reasons[("phrase", "customers")].startswith("occurs in top3")
    assert reasons[("phrase", "the biggest")] == "placeholders differ from the term's existing wordings"
    assert reasons[("variant", "The largest 3 customers by paid spend")].startswith("claims slots")
    assert reasons[("variant", "The top 3 customers by paid spend")] == "duplicate"
    assert "term,phrase,evidence\npaid,settled,top3\n" in result.review_csv()


def test_compiled_variants_serve_their_values_without_a_live_rerun(tmp_path):
    (tmp_path / "schema.sql").write_text(SHOP_DDL)
    terms = tmp_path / "terms"
    terms.mkdir()
    (terms / "Terms.csv").write_text(TERMS)
    (terms / "Examples.csv").write_text(EXAMPLES)
    (tmp_path / "sqlgami.yaml").write_text("dialect: duckdb\nschema: schema.sql\nterms: terms\nfixtures: {seed: 2, profiles: [clean, ties], values: {orders.status: [paid, open]}}\n")
    bundle = compile_bundle(tmp_path / "sqlgami.yaml", tmp_path / "bundle", chat=_fake_chat)
    manifest = json.loads((tmp_path / "bundle" / "manifest.json").read_text())
    assert manifest["expansion"]["phrases"][0] == {"term": "paid", "phrase": "settled", "evidence": "top3"}
    assert (tmp_path / "bundle" / "review" / "Terms.additions.csv").read_text().startswith("term,phrase,evidence\n")
    loaded = Bundle.load(tmp_path / "bundle")
    variant = loaded.reference_for(None, "top3~1")[0]
    assert variant.variant_of == "top3" and variant.slots == {"n": 5} and variant.results
    assert loaded.stored_for(loaded.reference_for(None, "top3")[0], {"n": 5}, {}) is variant
    # the live catalogue is still the host's: "settled" is not a phrase until the host accepts it
    assert loaded.reference_for("Which are the 5 highest customers by settled order value?")[1] == "question"
    with Scorer("duckdb", catalog=SHOP_DDL, bundle=tmp_path / "bundle") as scorer:
        by_text = scorer.explain(TOP3_SQL.replace("{n}", "5"), "Which are the 5 highest customers by settled order value?")
        assert by_text.signals["fixtures"]["verdict"] == "match" and by_text.signals["fixtures"]["reference_rerun"] is False
        # a different wording with n = 5 is matched by pattern and served by the variant's stored results
        by_pattern = scorer.explain(TOP3_SQL.replace("{n}", "5"), "Which are the largest 5 customers by paid order value?")
        signal = by_pattern.signals["fixtures"]
        assert signal["matched_by"] == "pattern" and signal["reference"] == "top3~1" and signal["reference_rerun"] is False and signal["verdict"] == "match"
        # n = 7 has no variant: the reference is re-run live
        seven = scorer.explain(TOP3_SQL.replace("{n}", "7"), "The top 7 customers by paid spend")
        assert seven.signals["fixtures"]["reference_rerun"] is True and seven.signals["fixtures"]["verdict"] == "match"
        assert seven.stage("fixtures").status == StageStatus.PASSED
