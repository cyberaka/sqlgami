"""A production-shaped warehouse query and its near misses, on synthetic names.

The shape: a script with a templated DECLARE, a periodic snapshot fact, period
bounds taken from scalar subqueries, a twelve-column OR-chain access filter,
and two levels of aggregation. The correct form must not be called likely wrong;
each broken variant must be.
"""

from __future__ import annotations

import pytest

from sqlgami import Catalog, RequiredPredicate, Scorer, Severity

from conftest import make_db

FACT = "acme-hr-prod.PEOPLE_SECURE.EMP_SNAPSHOT_FACT"
DDL = """
CREATE TABLE `acme-hr-prod.PEOPLE_SECURE.EMP_SNAPSHOT_FACT` (
  SNAP_DT DATE, PERIOD_END_DT DATE, EMP_ID INT64, HIRE_DT DATE, TERM_DT DATE, STATUS_CD STRING,
  INVOL_IND INT64, NEW_HIRE_IND INT64, EXIT_FILT_IND INT64, TURN_FILT_IND INT64, SOR STRING, SEGMENT STRING,
  ORG_TYPE STRING, ASSOC_TYPE_CD STRING, JOB_GRP STRING,
  LVL_2_ID INT64, LVL_3_ID INT64, LVL_4_ID INT64, LVL_5_ID INT64, LVL_6_ID INT64, LVL_7_ID INT64,
  LVL_2_ID_FN INT64, LVL_3_ID_FN INT64, LVL_4_ID_FN INT64, LVL_5_ID_FN INT64, LVL_6_ID_FN INT64, LVL_7_ID_FN INT64)
PARTITION BY PERIOD_END_DT;
CREATE TABLE `acme-hr-prod.PEOPLE_REF.PERIOD_DIM` (PPE DATE, FREQ STRING, CUR_FLAG INT64);
"""
CHAIN = " OR ".join(
    [f"F.LVL_{i}_ID = MGR_ID" for i in range(2, 8)] + [f"F.LVL_{i}_ID_FN = MGR_ID" for i in range(2, 8)]
)
AS_WRITTEN = f"""DECLARE MGR_ID INT64 DEFAULT {{mgr_id_of_user}};
SELECT SEGMENT
, ROUND(SAFE_DIVIDE(SUM(T90), SUM(HIRES)) * 100.00, 2) AS NEW_HIRE_90_DAY_TURNOVER
, ROUND(SAFE_DIVIDE(SUM(T180), SUM(HIRES)) * 100.00, 2) AS TURNOVER_180_DAY
FROM (
  SELECT F.PERIOD_END_DT, SEGMENT,
    SUM(CASE WHEN EXIT_FILT_IND = 1 AND DATE_DIFF(TERM_DT, HIRE_DT, day) <= 90 THEN 1 ELSE 0 END) AS T90,
    SUM(CASE WHEN EXIT_FILT_IND = 1 AND DATE_DIFF(TERM_DT, HIRE_DT, day) <= 180 THEN 1 ELSE 0 END) AS T180,
    SUM(NEW_HIRE_IND) AS HIRES
  FROM `acme-hr-prod.PEOPLE_SECURE.EMP_SNAPSHOT_FACT` F
  WHERE TURN_FILT_IND = 1 AND SOR = 'US' AND SEGMENT IN ('Retail') AND F.ORG_TYPE IS NOT NULL
    AND PERIOD_END_DT = SNAP_DT
    AND F.PERIOD_END_DT BETWEEN
      ((SELECT PPE FROM `acme-hr-prod.PEOPLE_REF.PERIOD_DIM` WHERE FREQ = 'B' AND CUR_FLAG = 1) - 14 * 25)
      AND (SELECT PPE FROM `acme-hr-prod.PEOPLE_REF.PERIOD_DIM` WHERE FREQ = 'B' AND CUR_FLAG = 1)
    -- leader hierarchy filter
    AND ({CHAIN})
    AND F.JOB_GRP IN ('{{job_groups_of_user}}')
  GROUP BY PERIOD_END_DT, SEGMENT
) X
GROUP BY SEGMENT;"""
CORRECT = AS_WRITTEN.replace("IN ('{job_groups_of_user}')", "IN ({job_groups_of_user})")
QUESTION = (
    "What is the 90-day and 180-day new hire turnover percentage by segment for my organization "
    "over the last year?"
)
RULES = [
    RequiredPredicate(
        name="leader_scope",
        tables=(FACT,),
        columns=tuple(f"LVL_{i}_ID{suffix}" for suffix in ("", "_FN") for i in range(2, 8)),
        match="or_chain",
    )
]


def scorer(**extra) -> Scorer:
    catalog = Catalog.from_ddl(DDL, "bigquery")
    return Scorer("bigquery", catalog=catalog, snapshot_columns={FACT: "SNAP_DT"}, required_predicates=RULES, **extra)


def test_the_correct_query_is_not_called_likely_wrong():
    report = scorer().explain(CORRECT, QUESTION)
    assert report.stage("schema").status.value == "passed", report.summary()
    assert not report.findings_at_least(Severity.HIGH), report.summary()
    assert report.verdict.value == "uncertain", report.summary()
    assert report.score > 0.42
    # Static-only reports say what was not done.
    assert "not planned or run" in report.confidence.basis
    # The subquery used for both bounds is one risk, reported once.
    scalar = [f for f in report.findings if f.code == "scalar_subquery_rows"]
    assert len(scalar) == 1 and scalar[0].evidence["occurrences"] == 2
    assert report.signals["script"]["variables"] == {"mgr_id": "INT64"}
    assert {p["name"] for p in report.signals["template_parameters"]} == {"mgr_id_of_user", "job_groups_of_user"}
    assert report.signals["disabled_filters"] == [] if "disabled_filters" in report.signals else True


def test_declaring_the_dimension_key_settles_the_scalar_subquery():
    catalog = Catalog.from_ddl(DDL, "bigquery")
    catalog.declare_unique("PERIOD_DIM", ["FREQ", "CUR_FLAG"], schema="acme-hr-prod.PEOPLE_REF")
    s = Scorer("bigquery", catalog=catalog, snapshot_columns={FACT: "SNAP_DT"}, required_predicates=RULES)
    report = s.explain(CORRECT, QUESTION)
    assert not report.has("scalar_subquery_rows"), report.summary()
    assert report.score > scorer().explain(CORRECT, QUESTION).score


@pytest.mark.parametrize(
    "name,sql,code",
    [
        ("quoted_list_param", AS_WRITTEN, "template_list_param"),
        ("no_snapshot_filter", CORRECT.replace("AND PERIOD_END_DT = SNAP_DT\n", ""), "snapshot_grain"),
        ("chain_missing_a_level", CORRECT.replace(" OR F.LVL_7_ID_FN = MGR_ID", ""), "required_predicate"),
        ("no_access_filter", CORRECT.replace(f"AND ({CHAIN})", ""), "required_predicate"),
    ],
)
def test_each_broken_variant_is_caught(name, sql, code):
    report = scorer().explain(sql, QUESTION)
    assert report.has(code), report.summary()
    assert report.score < scorer().explain(CORRECT, QUESTION).score, report.summary()


def test_high_severity_variants_are_likely_wrong():
    for sql in (
        CORRECT.replace("AND PERIOD_END_DT = SNAP_DT\n", ""),
        CORRECT.replace(" OR F.LVL_7_ID_FN = MGR_ID", ""),
    ):
        report = scorer().explain(sql, QUESTION)
        assert report.verdict.value == "likely_wrong" and report.score <= 0.4, report.summary()


def test_soft_doubts_alone_never_amount_to_likely_wrong(engine, db):
    with Scorer(engine, db=db) as s:
        # Three medium-severity doubts, no high one, and the query runs.
        report = s.sample(
            "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
            "WHERE o.status = 'open' AND o.order_date > '2024-01-01' LIMIT 3",
            "List the 5 customers with orders in 2023",
        )
        mediums = [f for f in report.findings if f.severity == Severity.MEDIUM]
        assert len(mediums) >= 3 and not report.findings_at_least(Severity.HIGH), report.summary()
        assert report.verdict.value == "uncertain" and 0.42 < report.score < 0.7, report.summary()


def test_scalar_subquery_is_checked_against_the_data(engine, db):
    one_row = "SELECT name FROM customers WHERE id = (SELECT customer_id FROM orders WHERE status = 'open')"
    several = "SELECT name FROM customers WHERE id = (SELECT customer_id FROM orders WHERE status = 'paid')"
    with Scorer(engine, db=db) as s:
        static = s.explain(one_row)
        assert next(f for f in static.findings if f.code == "scalar_subquery_rows").severity == Severity.MEDIUM
        checked = s.sample(one_row)
        finding = next(f for f in checked.findings if f.code == "scalar_subquery_rows")
        assert finding.severity == Severity.INFO and finding.evidence["data_check"] == "one_row"
        bad = s.sample(several)
        assert bad.score <= 0.4
        if bad.has("execution_error"):
            # An engine that refuses a multi-row scalar subquery has already settled it.
            assert bad.verdict.value == "invalid"
        else:
            finding = next(f for f in bad.findings if f.code == "scalar_subquery_rows")
            assert finding.severity == Severity.HIGH and finding.evidence["data_check"] == "several_rows"
