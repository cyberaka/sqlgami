"""Patterns from production warehouse SQL, on a synthetic HR schema.

Every name here is invented. The shapes are what matters: multi-statement
scripts, template placeholders, dashed project names, periodic snapshot facts,
hierarchy access filters, scalar-subquery date bounds.
"""

from __future__ import annotations

import pytest

from sqlgami import Catalog, RequiredPredicate, Scorer, Severity

from conftest import make_db

FACT = "`acme-hr-prod.PEOPLE_SECURE.EMP_SNAPSHOT_FACT`"
PERIODS = "`acme-ref-prod.CALENDAR.PAY_PERIOD_DIM`"

DDL = """
CREATE TABLE `acme-hr-prod.PEOPLE_SECURE.EMP_SNAPSHOT_FACT` (
  EMP_ID INT64 NOT NULL, SNAP_DT DATE NOT NULL, PERIOD_END_DT DATE NOT NULL,
  STATUS_CD STRING, JOB_GRP_CD STRING, REGION_CD STRING, ACTIVE_IND INT64, UPDATED_TS TIMESTAMP,
  LVL_2_ID INT64, LVL_3_ID INT64, LVL_4_ID INT64, LVL_5_ID INT64,
  LVL_2_FN INT64, LVL_3_FN INT64, LVL_4_FN INT64, LVL_5_FN INT64,
  PRIMARY KEY (EMP_ID, PERIOD_END_DT, SNAP_DT) NOT ENFORCED
) PARTITION BY SNAP_DT;
CREATE TABLE `acme-ref-prod.CALENDAR.PAY_PERIOD_DIM` (
  PERIOD_END_DT DATE NOT NULL, CURRENT_PERIOD_FLG INT64, FISCAL_YR INT64,
  PRIMARY KEY (PERIOD_END_DT) NOT ENFORCED
);
CREATE TABLE `acme-hr-prod.PEOPLE.REGION_DIM` (
  REGION_CD STRING NOT NULL, REGION_NM STRING, PRIMARY KEY (REGION_CD) NOT ENFORCED
);
"""

ACCESS = RequiredPredicate(
    name="manager scope",
    tables=("EMP_SNAPSHOT_FACT",),
    columns=("LVL_2_ID", "LVL_3_ID", "LVL_4_ID", "LVL_5_ID"),
    match="or_chain",
)
CHAIN = "(f.LVL_2_ID = v_mgr OR f.LVL_3_ID = v_mgr OR f.LVL_4_ID = v_mgr OR f.LVL_5_ID = v_mgr)"
GRAIN = "f.SNAP_DT = f.PERIOD_END_DT AND f.SNAP_DT >= '2025-01-01'"


def scorer(**kwargs) -> Scorer:
    return Scorer("bigquery", catalog=Catalog.from_ddl(DDL, "bigquery"), **kwargs)


def codes(report, at_least: Severity = Severity.LOW) -> set[str]:
    return {f.code for f in report.findings_at_least(at_least)}


# ----------------------------------------------------------- names and case


def test_three_part_names_and_case_rules():
    catalog = Catalog.from_ddl(DDL, "bigquery")
    assert "acme-hr-prod.PEOPLE_SECURE.EMP_SNAPSHOT_FACT" in catalog.table_names()
    fact = catalog.resolve("EMP_SNAPSHOT_FACT", "acme-hr-prod.PEOPLE_SECURE")
    assert fact.primary_key == ("EMP_ID", "PERIOD_END_DT", "SNAP_DT") and fact.partition_columns == ("SNAP_DT",)
    assert catalog.resolve("EMP_SNAPSHOT_FACT", "PEOPLE_SECURE") is fact  # project filled in when unambiguous
    assert catalog.resolve("emp_snapshot_fact", "PEOPLE_SECURE") is None  # table names are case-sensitive
    assert catalog.column(fact, "emp_id") is catalog.column(fact, "EMP_ID")  # column names are not

    s = scorer()
    for sql in (
        f"SELECT f.emp_id FROM {FACT} f WHERE {GRAIN}",
        f"SELECT f.EMP_ID FROM `acme-hr-prod`.PEOPLE_SECURE.EMP_SNAPSHOT_FACT AS f WHERE {GRAIN}",
        f"SELECT f.Emp_Id FROM acme-hr-prod.PEOPLE_SECURE.EMP_SNAPSHOT_FACT f WHERE {GRAIN}",
    ):
        report = s.explain(sql)
        assert report.stage("schema").status.value == "passed", report.summary()
        assert report.tables == ["acme-hr-prod.PEOPLE_SECURE.EMP_SNAPSHOT_FACT"]
    wrong_case = s.explain("SELECT f.EMP_ID FROM `acme-hr-prod.PEOPLE_SECURE.emp_snapshot_fact` f")
    assert wrong_case.has("unknown_table")
    assert s.explain(f"SELECT f.NOPE FROM {FACT} f").has("unknown_column")
    two_projects = s.explain(
        f"SELECT r.REGION_NM, COUNT(DISTINCT f.EMP_ID) FROM {FACT} f "
        f"JOIN `acme-hr-prod.PEOPLE.REGION_DIM` r ON r.REGION_CD = f.REGION_CD "
        f"JOIN {PERIODS} p ON p.PERIOD_END_DT = f.PERIOD_END_DT WHERE {GRAIN} GROUP BY r.REGION_NM"
    )
    assert two_projects.stage("schema").status.value == "passed", two_projects.summary()
    assert len(two_projects.tables) == 3


# ------------------------------------------------------------------ scripts


def test_script_is_scored_on_its_final_select():
    s = scorer()
    report = s.explain(
        f"DECLARE v_mgr INT64 DEFAULT 1001;\n"
        f"DECLARE v_from DATE DEFAULT (SELECT MIN(PERIOD_END_DT) FROM {PERIODS});\n"
        f"SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN} AND f.PERIOD_END_DT >= v_from AND {CHAIN}"
    )
    assert report.stage("statement").status.value == "passed", report.summary()
    assert report.stage("schema").status.value == "passed", report.summary()
    assert report.signals["script"] == {
        "statements": ["DECLARE", "DECLARE", "SELECT"],
        "variables": {"v_mgr": "INT64", "v_from": "DATE"},
    }
    assert not report.has("unknown_column") and not report.has("multiple_statements")

    undeclared = s.explain(f"SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN} AND f.LVL_2_ID = v_mgr")
    assert undeclared.has("unknown_column")

    writes = s.explain(f"DECLARE v INT64 DEFAULT 1; DELETE FROM {FACT} WHERE EMP_ID = v; SELECT 1")
    assert writes.verdict.value == "invalid"
    two_queries = s.explain("SELECT 1; SELECT 2")
    assert two_queries.has("multiple_statements")
    # Scripts are a BigQuery feature; elsewhere several statements are still refused.
    assert Scorer("postgres").explain("DECLARE v INT DEFAULT 1; SELECT 1").verdict.value == "invalid"


def test_variable_shadowed_by_a_column():
    report = scorer().explain(
        f"DECLARE region_cd STRING DEFAULT 'EMEA';\n"
        f"SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN} AND f.REGION_CD = region_cd"
    )
    assert report.has("variable_shadowed")


# ---------------------------------------------------------------- templates


def test_template_placeholders_are_found_before_parsing():
    s = scorer()
    sql = (
        "DECLARE v_mgr INT64 DEFAULT {user_mgr_id};\n"
        f"SELECT f.EMP_ID FROM {FACT} f -- AND f.ACTIVE_IND = 1\n"
        f"WHERE {GRAIN} AND {CHAIN}\n"
        "  AND f.JOB_GRP_CD IN ('{groups_of_user}') /* f.REGION_CD = 'EMEA' */\n"
        "  AND f.STATUS_CD = '{status}'"
    )
    report = s.explain(sql)
    assert report.stage("schema").status.value == "passed", report.summary()
    params = {p["name"]: p for p in report.signals["template_parameters"]}
    assert set(params) == {"user_mgr_id", "groups_of_user", "status"}
    assert params["user_mgr_id"]["inferred_type"] == "INT64" and params["user_mgr_id"]["quoted"] is False
    assert params["groups_of_user"]["quoted"] is True
    assert report.signals["disabled_filters"] == ["AND f.ACTIVE_IND = 1", "f.REGION_CD = 'EMEA'"]
    finding = next(f for f in report.findings if f.code == "template_list_param")
    assert finding.severity == Severity.MEDIUM and finding.evidence["parameter"] == "groups_of_user"
    assert report.sql == sql  # the report shows the statement as it was given

    filled = s.explain(sql, params={"user_mgr_id": 7, "groups_of_user": "ENG,OPS", "status": "A"})
    finding = next(f for f in filled.findings if f.code == "template_list_param")
    assert finding.severity == Severity.HIGH and "ENG,OPS" in finding.message
    single = s.explain(sql, params={"user_mgr_id": 7, "groups_of_user": "ENG", "status": "A"})
    assert next(f for f in single.findings if f.code == "template_list_param").severity == Severity.MEDIUM
    assert all(p["supplied"] for p in single.signals["template_parameters"])

    plain = s.explain(f"SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN} AND f.STATUS_CD = '{{\"a\": 1}}'")
    assert "template_parameters" not in plain.signals  # a JSON literal is not a placeholder


@pytest.mark.parametrize("engine", ["sqlite", "duckdb"])
def test_a_template_runs_only_when_its_values_are_given(engine):
    with Scorer(engine, db=make_db(engine)) as s:
        sql = "SELECT name FROM customers WHERE country = '{country}' AND tenant_id = {tenant} ORDER BY name"
        unfilled = s.sample(sql)
        assert unfilled.stage("schema").status.value == "passed"
        assert unfilled.stage("dry_run").status.value == "skipped"
        assert "no value was given for: country, tenant" in unfilled.stage("sample").detail
        filled = s.sample(sql, params={"country": "US", "tenant": 1})
        assert filled.stage("sample").status.value == "passed", filled.summary()
        assert filled.signals["rows_returned"] == 1
        in_list = s.sample("SELECT name FROM customers WHERE country IN ('{codes}')", params={"codes": "US,DE"})
        assert in_list.has("template_list_param") and in_list.has("empty_result")
        assert in_list.score <= 0.4


# ----------------------------------------------------------- snapshot grain


def test_snapshot_grain():
    s = scorer()
    base = f"SELECT f.REGION_CD, COUNT(DISTINCT f.EMP_ID) FROM {FACT} f"
    missing = s.explain(f"{base} WHERE f.PERIOD_END_DT >= '2025-01-01' AND f.SNAP_DT >= '2025-01-01' GROUP BY f.REGION_CD")
    finding = next(f for f in missing.findings if f.code == "snapshot_grain")
    assert finding.severity == Severity.MEDIUM  # recognised by shape, not declared
    for where in (
        "f.SNAP_DT = f.PERIOD_END_DT",
        "f.SNAP_DT = '2025-06-30'",
        f"f.SNAP_DT = (SELECT MAX(SNAP_DT) FROM {FACT})",
    ):
        ok = s.explain(f"{base} WHERE {where} AND f.SNAP_DT >= '2025-01-01' GROUP BY f.REGION_CD")
        assert not ok.has("snapshot_grain"), where + "\n" + ok.summary()
    grouped = s.explain(f"SELECT f.SNAP_DT, COUNT(*) FROM {FACT} f WHERE f.SNAP_DT >= '2025-01-01' GROUP BY f.SNAP_DT")
    assert not grouped.has("snapshot_grain")
    through_cte = s.explain(
        f"WITH x AS (SELECT EMP_ID, SNAP_DT, PERIOD_END_DT FROM {FACT} WHERE SNAP_DT >= '2025-01-01') "
        "SELECT COUNT(DISTINCT EMP_ID) FROM x WHERE x.SNAP_DT = x.PERIOD_END_DT"
    )
    assert not through_cte.has("snapshot_grain"), through_cte.summary()

    declared = scorer(snapshot_columns={"EMP_SNAPSHOT_FACT": "SNAP_DT"})
    report = declared.explain(f"{base} WHERE f.SNAP_DT >= '2025-01-01' GROUP BY f.REGION_CD")
    assert next(f for f in report.findings if f.code == "snapshot_grain").severity == Severity.HIGH
    assert report.score <= 0.4


# ------------------------------------------------------------ access filters


def test_required_access_filter():
    s = scorer(required_predicates=[ACCESS])
    declare = "DECLARE v_mgr INT64 DEFAULT 1001;\n"
    complete = s.explain(f"{declare}SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN} AND {CHAIN}")
    assert not complete.has("required_predicate"), complete.summary()

    none = s.explain(f"{declare}SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN}")
    finding = next(f for f in none.findings if f.code == "required_predicate")
    assert finding.severity == Severity.HIGH and "has none" in finding.message
    assert none.score <= 0.4

    partial = s.explain(
        f"{declare}SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN} "
        "AND (f.LVL_2_ID = v_mgr OR f.LVL_3_ID = v_mgr OR f.LVL_5_ID = v_mgr)"
    )
    finding = next(f for f in partial.findings if f.code == "required_predicate")
    assert finding.evidence["missing"] == ["lvl_4_id"] and "incomplete" in finding.message

    other_table = s.explain(f"SELECT PERIOD_END_DT FROM {PERIODS}")
    assert not other_table.has("required_predicate")

    simple_rule = scorer(required_predicates=[{"name": "region scope", "columns": ["REGION_CD"], "tables": ["EMP_SNAPSHOT_FACT"]}])
    assert simple_rule.explain(f"SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN}").has("required_predicate")
    assert not simple_rule.explain(f"SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN} AND f.REGION_CD = 'EMEA'").has(
        "required_predicate"
    )


def test_or_chain_that_skips_a_level():
    s = scorer()
    declare = "DECLARE v_mgr INT64 DEFAULT 1001;\n"
    gap = s.explain(
        f"{declare}SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN} "
        "AND (f.LVL_2_ID = v_mgr OR f.LVL_3_ID = v_mgr OR f.LVL_5_ID = v_mgr)"
    )
    finding = next(f for f in gap.findings if f.code == "or_chain_gap")
    assert finding.evidence["missing"] == ["lvl_4_id"]
    assert not s.explain(f"{declare}SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN} AND {CHAIN}").has("or_chain_gap")
    parallel = s.explain(
        f"{declare}SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN} AND ({CHAIN[1:-1]} "
        "OR f.LVL_2_FN = v_mgr OR f.LVL_3_FN = v_mgr OR f.LVL_5_FN = v_mgr)"
    )
    finding = next(f for f in parallel.findings if f.code == "or_chain_gap")
    assert finding.evidence["missing"] == ["lvl_4_fn"]
    different_values = s.explain(
        f"SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN} AND (f.LVL_2_ID = 1 OR f.LVL_3_ID = 2 OR f.LVL_5_ID = 3)"
    )
    assert not different_values.has("or_chain_gap")


# -------------------------------------------------------- scalar subqueries


def test_scalar_subquery_bounds():
    s = scorer()
    current = f"(SELECT PERIOD_END_DT FROM {PERIODS} WHERE CURRENT_PERIOD_FLG = 1)"
    risky = s.explain(
        f"SELECT COUNT(DISTINCT f.EMP_ID) FROM {FACT} f WHERE f.SNAP_DT = f.PERIOD_END_DT "
        f"AND f.PERIOD_END_DT BETWEEN ({current} - 14 * 25) AND {current}"
    )
    assert risky.stage("schema").status.value == "passed", risky.summary()
    found = [f for f in risky.findings if f.code == "scalar_subquery_rows"]
    # The same subquery appears as both bounds: one risk, reported once.
    assert len(found) == 1 and found[0].severity == Severity.MEDIUM and found[0].evidence["occurrences"] == 2
    assert not risky.has("date_arithmetic")  # DATE minus a number of days is defined in BigQuery
    partition = next(f for f in risky.findings if f.code == "partition_filter")
    assert partition.severity == Severity.LOW and partition.evidence["kind"] == "non_constant"

    for safe in (
        f"(SELECT MAX(PERIOD_END_DT) FROM {PERIODS} WHERE CURRENT_PERIOD_FLG = 1)",
        f"(SELECT PERIOD_END_DT FROM {PERIODS} WHERE PERIOD_END_DT = '2025-06-30')",
        f"(SELECT PERIOD_END_DT FROM {PERIODS} WHERE CURRENT_PERIOD_FLG = 1 ORDER BY 1 DESC LIMIT 1)",
    ):
        report = s.explain(f"SELECT f.EMP_ID FROM {FACT} f WHERE {GRAIN} AND f.PERIOD_END_DT = {safe}")
        assert not report.has("scalar_subquery_rows"), safe + "\n" + report.summary()


def test_partition_filter():
    s = scorer()
    none = s.explain(f"SELECT COUNT(DISTINCT f.EMP_ID) FROM {FACT} f WHERE f.REGION_CD = 'EMEA' GROUP BY f.SNAP_DT")
    finding = next(f for f in none.findings if f.code == "partition_filter")
    assert finding.severity == Severity.MEDIUM and finding.evidence["kind"] == "missing"
    constant = s.explain(f"SELECT f.EMP_ID FROM {FACT} f WHERE f.SNAP_DT = '2025-06-30'")
    assert not constant.has("partition_filter")


def test_date_arithmetic_depends_on_the_engine():
    ddl = "CREATE TABLE ev (id INT PRIMARY KEY, d DATE, ts TIMESTAMP);"
    sqlite = Scorer("sqlite", catalog=ddl).explain("SELECT id FROM ev WHERE d >= d - 30")
    assert next(f for f in sqlite.findings if f.code == "date_arithmetic").severity == Severity.HIGH
    postgres = Scorer("postgres", catalog=ddl)
    assert not postgres.explain("SELECT id FROM ev WHERE d >= CURRENT_DATE - 30").has("date_arithmetic")
    assert postgres.explain("SELECT id FROM ev WHERE ts >= ts - 30").has("date_arithmetic")
    assert not postgres.explain("SELECT id FROM ev WHERE ts >= ts - INTERVAL '30 days'").has("date_arithmetic")


def test_distinct_with_an_unpartitioned_window_does_not_misfire():
    report = scorer().explain(
        f"WITH ranked AS (SELECT DISTINCT p.*, DENSE_RANK() OVER (ORDER BY p.PERIOD_END_DT DESC) AS ranking "
        f"FROM {PERIODS} p) SELECT r.PERIOD_END_DT FROM ranked r WHERE r.ranking BETWEEN 1 AND 26",
        "Which are the last 26 pay periods?",
    )
    assert report.stage("schema").status.value == "passed", report.summary()
    assert not report.findings_at_least(Severity.MEDIUM), report.summary()


# -------------------------------------------- single-expression checks, engine-backed


def test_self_comparison_and_empty_string(engine, db):
    with Scorer(engine, db=db) as s:
        never = s.sample("SELECT COUNT(*) FROM customers WHERE deleted_at <> deleted_at")
        assert next(f for f in never.findings if f.code == "self_comparison").severity == Severity.HIGH
        assert never.score <= 0.4
        always = s.explain("SELECT COUNT(*) FROM customers c JOIN orders o ON o.customer_id = o.customer_id")
        assert always.has("self_comparison")
        assert not s.explain("SELECT COUNT(*) FROM customers c JOIN orders o ON o.customer_id = c.id").has("self_comparison")

        empty = s.sample("SELECT COUNT(*) FROM customers WHERE country <> ''")
        finding = next(f for f in empty.findings if f.code == "empty_string_comparison")
        assert finding.severity == Severity.HIGH and finding.evidence["data_check"] == "value_absent"
        not_null_column = s.sample("SELECT COUNT(*) FROM customers WHERE name <> ''")
        assert not not_null_column.has("empty_string_comparison")


def test_duplicate_rows_and_what_the_question_makes_of_them(engine, db):
    with Scorer(engine, db=db) as s:
        sql = "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.total > 10"
        plain = s.sample(sql)
        assert next(f for f in plain.findings if f.code == "duplicate_rows").severity == Severity.LOW
        assert plain.signals["duplicate_rows_in_sample"] > 0
        listing = s.sample(sql, "List the customers with an order over 10")
        assert next(f for f in listing.findings if f.code == "duplicate_rows").severity == Severity.MEDIUM
        distinct = s.sample(sql, "Return the distinct customer names with an order over 10")
        assert next(f for f in distinct.findings if f.code == "duplicate_rows").severity == Severity.HIGH
        for fine in (
            "SELECT DISTINCT c.name FROM customers c JOIN orders o ON o.customer_id = c.id",
            "SELECT c.name, o.id FROM customers c JOIN orders o ON o.customer_id = c.id",
            "SELECT o.id FROM customers c JOIN orders o ON o.customer_id = c.id",
            "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.id IS NULL",
            "SELECT c.name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)",
        ):
            assert not s.sample(fine).has("duplicate_rows"), fine


def test_inner_join_that_drops_rows_the_question_asked_for(engine, db):
    inner = "SELECT c.id, c.name, COUNT(o.id) AS n FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.id, c.name"
    left = inner.replace(" JOIN ", " LEFT JOIN ")
    with Scorer(engine, db=db) as s:
        quiet = s.sample(inner)
        finding = next(f for f in quiet.findings if f.code == "inner_join_drops_rows")
        assert finding.severity == Severity.LOW and finding.evidence["data_check"] == "unmatched"
        asked = s.sample(inner, "For every customer, how many orders do they have?")
        assert next(f for f in asked.findings if f.code == "inner_join_drops_rows").severity == Severity.HIGH
        assert asked.score <= 0.4
        assert not s.sample(left, "For every customer, how many orders do they have?").has("inner_join_drops_rows")
        static = s.explain(inner, "For every customer, how many orders do they have?")
        assert next(f for f in static.findings if f.code == "inner_join_drops_rows").severity == Severity.MEDIUM


def test_wording_of_bounds(scorer):
    for question, sql, wanted in (
        ("How many orders have a total greater than 50?", "SELECT COUNT(*) FROM orders WHERE total >= 50", ">"),
        ("How many orders have a total of at least 50?", "SELECT COUNT(*) FROM orders WHERE total > 50", ">="),
        ("How many orders have a total under 50?", "SELECT COUNT(*) FROM orders WHERE total <= 50", "<"),
        ("How many orders were created after 2024-01-31?", "SELECT COUNT(*) FROM orders WHERE created_at >= '2024-01-31'", ">"),
    ):
        report = scorer.sample(sql, question)
        finding = next(f for f in report.findings if f.code == "comparison_operator")
        assert finding.evidence["wanted"] == wanted, report.summary()
    opposite = scorer.sample("SELECT COUNT(*) FROM orders WHERE total < 50", "How many orders have a total greater than 50?")
    assert next(f for f in opposite.findings if f.code == "comparison_operator").severity == Severity.HIGH
    for question, sql in (
        ("How many orders have a total greater than 50?", "SELECT COUNT(*) FROM orders WHERE total > 50"),
        ("How many orders have a total greater than 50?", "SELECT COUNT(*) FROM orders WHERE 50 < total"),
        ("How many orders have a total of 50 or more?", "SELECT COUNT(*) FROM orders WHERE total >= 50"),
        # a bound word does not reach across a clause break: "before as-of; 0 otherwise" is not "before 0"
        ("Flag customers with an order placed at or before 2024-01-31; 0 otherwise.", "SELECT c.id, CASE WHEN COUNT(o.id) > 0 THEN 1 ELSE 0 END FROM customers c LEFT JOIN orders o ON o.customer_id = c.id AND o.created_at <= '2024-01-31' GROUP BY c.id"),
    ):
        assert not scorer.sample(sql, question).has("comparison_operator"), sql


def test_a_number_the_question_uses_twice_is_only_a_note(scorer):
    """'the 180-day turnover ... at least 180 days to attrit': <= 180 is about the first 180, not the second."""
    question = "What is the 180-day turnover among orders placed at least 180 days ago? Return the count."
    report = scorer.sample("SELECT COUNT(*) FROM orders WHERE total <= 180", question)
    finding = next(f for f in report.findings if f.code == "comparison_operator")
    assert finding.severity == Severity.LOW and "more than once" in finding.message
    # The wording honoured by one comparison is not contradicted by another on the same number.
    honoured = scorer.sample(
        "SELECT COUNT(*) FROM orders WHERE total >= 50 AND quantity <= 50",
        "How many orders have a total of at least 50?",
    )
    assert not honoured.has("comparison_operator")


def test_partial_match_wording_and_totals(scorer):
    exact = scorer.sample("SELECT name FROM customers WHERE name = 'A'", "Which customers have a name that starts with A?")
    assert exact.has("pattern_match_missing")
    like = scorer.sample("SELECT name FROM customers WHERE name LIKE 'A%' ORDER BY name", "Which customers have a name that starts with A?")
    assert not like.has("pattern_match_missing")
    counted = scorer.sample("SELECT status, COUNT(total) FROM orders GROUP BY status", "Total order value per status")
    assert counted.has("aggregate_mismatch")
    summed = scorer.sample("SELECT status, SUM(total) FROM orders GROUP BY status", "Total order value per status")
    assert not summed.has("aggregate_mismatch")
    assert not scorer.sample("SELECT COUNT(*) FROM orders", "What is the total number of orders?").has("aggregate_mismatch")


def test_at_least_one_is_not_a_ranking(scorer):
    report = scorer.sample(
        "SELECT DISTINCT c.name FROM customers c JOIN orders o ON o.customer_id = c.id ORDER BY c.name",
        "List customers that have at least one order",
    )
    assert not report.has("top_n"), report.summary()
    assert scorer.sample("SELECT name FROM customers ORDER BY name", "List the first 2 customers by name").has("top_n")


def test_flag_column_named_in_the_question():
    ddl = "CREATE TABLE plans (plan_id INT PRIMARY KEY, code TEXT NOT NULL, is_active BOOLEAN NOT NULL);" \
          "CREATE TABLE subs (sub_id INT PRIMARY KEY, plan_id INT NOT NULL, status TEXT);"
    s = Scorer("postgres", catalog=ddl)
    assert s.explain("SELECT code FROM plans ORDER BY code", "List all active plans").has("question_flag_unused")
    assert not s.explain("SELECT code FROM plans WHERE is_active ORDER BY code", "List all active plans").has("question_flag_unused")
    joined = s.explain(
        "SELECT p.code, COUNT(*) FROM plans p JOIN subs s ON s.plan_id = p.plan_id WHERE s.status = 'active' GROUP BY p.code",
        "Count active subs per plan",
    )
    assert not joined.has("question_flag_unused"), joined.summary()


# ------------------------------------------------------------ policy details


def test_soft_delete_policy_levels(engine, db):
    sql = "SELECT COUNT(*) FROM customers"
    with Scorer(engine, db=db, soft_delete_column="deleted_at") as advisory:
        assert next(f for f in advisory.sample(sql).findings if f.code == "soft_deleted_rows").severity == Severity.MEDIUM
        explicit = advisory.sample(sql, "How many non-deleted customers are there?")
        assert next(f for f in explicit.findings if f.code == "soft_deleted_rows").severity == Severity.HIGH
        assert not advisory.sample(sql, "How many customers are there, including deleted ones?").has("soft_deleted_rows")
    with Scorer(engine, db=db, soft_delete_column="deleted_at", soft_delete_policy="required") as required:
        report = required.sample(sql)
        assert next(f for f in report.findings if f.code == "soft_deleted_rows").severity == Severity.HIGH
        assert report.score <= 0.4
        assert not required.sample("SELECT COUNT(*) FROM customers WHERE deleted_at IS NULL").has("soft_deleted_rows")
        # A parent or pass-through table is a lesser matter: the rule is about the rows being counted.
        bridge = required.sample(
            "SELECT o.id, o.total FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.total > 0 ORDER BY o.id"
        )
        assert next(f for f in bridge.findings if f.code == "soft_deleted_rows").severity == Severity.LOW
        # A parent whose rows come out as result rows (grouped by its key) is listed, deleted ones
        # included: that is the rule's concern, however many orders hang off each.
        parent = required.sample(
            "SELECT c.id, COUNT(o.id) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.id ORDER BY c.id"
        )
        assert next(f for f in parent.findings if f.code == "soft_deleted_rows").severity == Severity.HIGH
        # A parent that only filters, with nothing of its own in the output, stays a lesser matter.
        unlisted = required.sample(
            "SELECT COUNT(o.id) FROM customers c JOIN orders o ON o.customer_id = c.id WHERE c.name <> ''"
        )
        assert next(f for f in unlisted.findings if f.code == "soft_deleted_rows").severity == Severity.LOW
        named = required.sample(
            "SELECT c.id, COUNT(o.id) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.id ORDER BY c.id",
            "For each non-deleted customer, how many orders do they have?",
        )
        assert next(f for f in named.findings if f.code == "soft_deleted_rows").severity == Severity.HIGH


def test_validity_dates_alone_do_not_make_a_history_table():
    ddl = """
    CREATE TABLE coupons (coupon_id INT PRIMARY KEY, code TEXT, valid_from DATE, valid_to DATE);
    CREATE TABLE prices (item_id INT NOT NULL, valid_from DATE NOT NULL, valid_to DATE, cents INT,
                         PRIMARY KEY (item_id, valid_from));
    """
    s = Scorer("duckdb", catalog=ddl)
    assert not s.explain("SELECT code FROM coupons ORDER BY code").has("history_rows")
    assert s.explain("SELECT item_id, cents FROM prices").has("history_rows")
    assert not s.explain("SELECT item_id, cents FROM prices WHERE valid_to IS NULL").has("history_rows")


def test_filters_inside_a_view_are_the_view_authors_business():
    ddl = """
    CREATE TABLE users (id INT PRIMARY KEY, name TEXT, deleted_at TIMESTAMP);
    CREATE TABLE logins (id INT PRIMARY KEY, user_id INT NOT NULL, at TIMESTAMP);
    CREATE VIEW login_names_v AS SELECT l.id, u.name FROM logins l JOIN users u ON u.id = l.user_id;
    """
    s = Scorer("postgres", catalog=ddl, soft_delete_column="deleted_at", soft_delete_policy="required")
    assert not s.explain("SELECT name FROM login_names_v ORDER BY id").has("soft_deleted_rows")
    assert s.explain("SELECT u.name FROM users u ORDER BY u.id").has("soft_deleted_rows")


def test_sorting_and_listed_outputs(scorer):
    unsorted = scorer.sample("SELECT name FROM customers", "List customer names, sorted alphabetically.")
    assert next(f for f in unsorted.findings if f.code == "order_missing").severity == Severity.HIGH
    assert not scorer.sample("SELECT name FROM customers ORDER BY name", "List customer names, sorted alphabetically.").has("order_missing")
    assert not scorer.sample("SELECT COUNT(*) FROM customers", "Count customers. Sort by name.").has("order_missing")

    extra = scorer.sample("SELECT id, name, country FROM customers ORDER BY id", "Return id and name. Sort by id.")
    finding = next(f for f in extra.findings if f.code == "output_columns")
    assert (finding.evidence["wanted"], finding.evidence["returned"]) == (2, 3)
    for question, sql, wanted in (
        ("Which customers are in the US? Return name, sorted by name.", "SELECT id, name FROM customers ORDER BY name", 1),
        ("List name of every customer in the US, sorted by name.", "SELECT name, country FROM customers ORDER BY name", 1),
        ("List all customers with their name and country, sorted by name.", "SELECT id, name, country FROM customers ORDER BY name", 2),
    ):
        finding = next(f for f in scorer.sample(sql, question).findings if f.code == "output_columns")
        assert finding.evidence["wanted"] == wanted, question
    for question, sql in (
        ("Return id and name. Sort by id.", "SELECT id, name FROM customers ORDER BY id"),
        ("List all customers with their name and country, sorted by name.", "SELECT name, country FROM customers ORDER BY name"),
        ("List the customers of every tenant that have an order", "SELECT c.name, c.tenant_id FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id) ORDER BY 1"),
        ("Return id, name, and country, sorted by id.", "SELECT id, name, country FROM customers ORDER BY id"),
        ("Return the customer name.", "SELECT name FROM customers ORDER BY name"),
        ("How many customers are there?", "SELECT COUNT(*), MAX(id) FROM customers"),
    ):
        assert not scorer.sample(sql, question).has("output_columns"), question


def test_row_numbers_among_ties(scorer):
    loose = scorer.sample("SELECT id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY status) AS seq FROM orders ORDER BY id")
    assert next(f for f in loose.findings if f.code == "window_order_ties").severity == Severity.LOW
    tight = scorer.sample("SELECT id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY status, id) AS seq FROM orders ORDER BY id")
    assert not tight.has("window_order_ties")


def test_top_n_query_is_not_blamed_for_dropping_empty_groups(scorer):
    report = scorer.sample(
        "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name ORDER BY COUNT(*) DESC, c.name LIMIT 1",
        "Across all tenants, which customer has the most orders?",
    )
    assert not report.findings_at_least(Severity.MEDIUM), report.summary()


def test_a_template_that_renders_into_nonsense_is_a_finding_not_a_crash(scorer):
    # '{codes}' inside a plain comparison: filled with a list it reads "country = 'US', 'DE'".
    report = scorer.sample("SELECT name FROM customers WHERE country = {codes}", "names", params={"codes": ["US", "DE"]})
    # Either the gate refuses the rendered text, or the engine's planner rejects it; both are gates.
    assert (report.has("render_error") or report.has("dry_run_error")) and report.verdict.value == "invalid"
    assert report.stage("dry_run").status.value == "failed" and report.stage("sample").status.value == "not_run"



HIER_DDL = """
CREATE TABLE snap (
    assoc_id INTEGER, period_end_dt DATE, snap_dt DATE, status_cd TEXT,
    lvl_2_id INTEGER, lvl_3_id INTEGER, lvl_4_id INTEGER, lvl_5_id INTEGER, lvl_6_id INTEGER, lvl_7_id INTEGER,
    lvl_2_id_fn INTEGER, lvl_3_id_fn INTEGER, lvl_4_id_fn INTEGER, lvl_5_id_fn INTEGER, lvl_6_id_fn INTEGER, lvl_7_id_fn INTEGER,
    PRIMARY KEY (assoc_id, period_end_dt, snap_dt)
);
"""
_REPORTING = ", ".join(f"s.lvl_{n}_id" for n in range(2, 8))
_FUNCTIONAL = ", ".join(f"s.lvl_{n}_id_fn" for n in range(2, 8))
_RULE = {
    "name": "hierarchy_access",
    "tables": ["snap"],
    "groups": [[f"lvl_{n}_id" for n in range(2, 8)], [f"lvl_{n}_id_fn" for n in range(2, 8)]],
    "match": "or_chain",
}


def _hier(sql: str):
    return Scorer("duckdb", catalog=HIER_DDL, required_predicates=[_RULE]).explain(sql, "headcount under a manager")


@pytest.mark.parametrize(
    "where",
    [
        f"{{manager_id}} IN ({_REPORTING})",  # value IN column list, one set
        f"{{manager_id}} IN ({_FUNCTIONAL})",  # the other set
        f"{{manager_id}} IN ({_REPORTING}) OR {{manager_id}} IN ({_FUNCTIONAL})",  # OR'ed IN lists across the sets
        f"{{manager_id}} IN ({_REPORTING}, {_FUNCTIONAL})",  # one IN list mixing both sets
        " OR ".join(f"s.lvl_{n}_id = {{manager_id}}" for n in range(2, 8)),  # the plain OR chain
        f"12003 IN ({_REPORTING})",  # a literal rather than a parameter
    ],
)
def test_value_in_column_list_satisfies_an_or_chain_rule(where):
    report = _hier(f"SELECT COUNT(*) FROM snap s WHERE s.snap_dt = s.period_end_dt AND ({where})")
    assert not report.has("required_predicate"), report.summary()


@pytest.mark.parametrize(
    "where",
    [
        ", ".join(f"s.lvl_{n}_id" for n in range(2, 7)),  # lvl_7 left out
        ", ".join(f"s.lvl_{n}_id" for n in (2, 3, 4)),  # half the levels
        ", ".join(f"s.lvl_{n}_id_fn" for n in range(3, 8)),  # _fn set without lvl_2
    ],
)
def test_incomplete_column_list_still_fires(where):
    report = _hier(f"SELECT COUNT(*) FROM snap s WHERE s.snap_dt = s.period_end_dt AND {{manager_id}} IN ({where})")
    finding = next(f for f in report.findings if f.code == "required_predicate")
    assert finding.severity == Severity.HIGH and "leaves out" in finding.message


def test_in_list_of_values_is_not_an_access_chain():
    report = _hier("SELECT COUNT(*) FROM snap s WHERE s.lvl_3_id IN (1, 2, 3)")
    assert report.has("required_predicate")


def test_lookup_subqueries_for_the_same_value_form_one_chain():
    """Each branch compares a level with (SELECT node_id FROM dim WHERE node_id = {m}): that is {m}."""
    ddl = HIER_DDL + "CREATE TABLE dim (node_id INTEGER, kind TEXT);"
    branches = " OR ".join(
        f"s.lvl_{n}_id = (SELECT node_id FROM dim WHERE kind = 'reporting' AND node_id = {{manager_id}})" for n in range(2, 8)
    )
    report = Scorer("duckdb", catalog=ddl, required_predicates=[_RULE]).explain(
        f"SELECT COUNT(*) FROM snap s WHERE {branches}", "headcount under a manager"
    )
    assert not report.has("required_predicate"), report.summary()


def test_scalar_subquery_is_a_constant_for_key_reasoning():
    """period_end_dt bound to an uncorrelated scalar subquery drops out of the key, so a derived
    table projecting the rest of the key is unique on it and no fan-out is suspected."""
    ddl = (
        "CREATE TABLE snap (assoc_id INTEGER, period_end_dt DATE, snap_dt DATE, amount INTEGER, PRIMARY KEY (assoc_id, period_end_dt, snap_dt));"
        "CREATE TABLE periods (ppe_dt DATE PRIMARY KEY, is_current BOOLEAN);"
        "CREATE TABLE people (assoc_id INTEGER PRIMARY KEY, name TEXT, salary INTEGER);"
    )
    sql = (
        "WITH cur AS (SELECT assoc_id, amount FROM snap WHERE snap_dt = period_end_dt "
        "AND period_end_dt = (SELECT ppe_dt FROM periods WHERE is_current)) "
        "SELECT c.assoc_id, SUM(p.salary) FROM people p JOIN cur c ON c.assoc_id = p.assoc_id GROUP BY c.assoc_id"
    )
    report = Scorer("duckdb", catalog=ddl).explain(sql, "amount per person this period")
    assert not [f for f in report.findings if f.code == "fan_out" and f.severity == Severity.HIGH], report.summary()
    correlated = sql.replace("WHERE is_current", "WHERE is_current AND ppe_dt = snap.period_end_dt")
    assert Scorer("duckdb", catalog=ddl).explain(correlated, "amount per person").has("fan_out")


def test_or_chain_against_a_column_pinned_to_the_parameter():
    """JOIN hierarchy_dim h ON lvl_2_id = h.node_id OR ... WHERE h.node_id = {manager_id}: every
    branch compares with a column pinned to the parameter one clause later; that is the chain."""
    ddl = HIER_DDL + "CREATE TABLE hierarchy_dim (node_id INTEGER, hierarchy_kind TEXT);"
    chain = " OR ".join(f"s.lvl_{n}_id = h.node_id" for n in range(2, 8)) + " OR " + " OR ".join(f"s.lvl_{n}_id_fn = h.node_id" for n in range(2, 8))
    both_chains = {"name": "hierarchy_access", "tables": ["snap"], "match": "or_chain",
                   "columns": [f"lvl_{n}_id" for n in range(2, 8)] + [f"lvl_{n}_id_fn" for n in range(2, 8)]}
    sc = Scorer("duckdb", catalog=ddl, required_predicates=[both_chains])
    full = sc.explain(f"SELECT COUNT(DISTINCT s.assoc_id) FROM snap s JOIN hierarchy_dim h ON {chain} WHERE h.node_id = {{manager_id}}", "headcount under a manager", params={"manager_id": 7})
    assert not full.has("required_predicate"), full.summary()
    partial = " OR ".join(f"s.lvl_{n}_id = h.node_id" for n in range(2, 8))
    short = sc.explain(f"SELECT COUNT(DISTINCT s.assoc_id) FROM snap s JOIN hierarchy_dim h ON {partial} WHERE h.node_id = {{manager_id}}", "headcount under a manager", params={"manager_id": 7})
    finding = next(f for f in short.findings if f.code == "required_predicate")
    assert finding.severity == Severity.HIGH and "leaves out" in finding.message
    unpinned = sc.explain(f"SELECT COUNT(DISTINCT s.assoc_id) FROM snap s JOIN hierarchy_dim h ON {chain}", "headcount under a manager")
    assert unpinned.has("required_predicate")  # h.node_id is not pinned to anything: no access filter


def test_or_chain_over_columns_each_joined_to_a_level():
    """One hierarchy_dim join per level (ON s.lvl_2_id = h2.node_id ...) and the chain written over
    the joined columns (h2.node_id = {m} OR h3.node_id = {m} ...): each branch is a branch on the
    level it is equated to. A chain that leaves out the functional levels is still incomplete."""
    ddl = HIER_DDL + "CREATE TABLE hierarchy_dim (node_id INTEGER, hierarchy_kind TEXT);"
    levels = [f"lvl_{n}_id" for n in range(2, 8)] + [f"lvl_{n}_id_fn" for n in range(2, 8)]
    joins = " ".join(f"JOIN hierarchy_dim h_{c} ON s.{c} = h_{c}.node_id" for c in levels)
    both_chains = {"name": "hierarchy_access", "tables": ["snap"], "match": "or_chain", "columns": levels}
    sc = Scorer("duckdb", catalog=ddl, required_predicates=[both_chains])
    chain = " OR ".join(f"h_{c}.node_id = {{manager_id}}" for c in levels)
    full = sc.explain(f"SELECT COUNT(DISTINCT s.assoc_id) FROM snap s {joins} WHERE ({chain})", "headcount under a manager", params={"manager_id": 7})
    assert not full.has("required_predicate"), full.summary()
    reporting_only = " OR ".join(f"h_{c}.node_id = {{manager_id}}" for c in levels[:6])
    short = sc.explain(f"SELECT COUNT(DISTINCT s.assoc_id) FROM snap s {joins} WHERE ({reporting_only})", "headcount under a manager", params={"manager_id": 7})
    finding = next(f for f in short.findings if f.code == "required_predicate")
    assert finding.severity == Severity.HIGH and "leaves out" in finding.message


def test_distinct_entity_keys_from_a_snapshot_are_not_a_grain_problem():
    ddl = "CREATE TABLE snap (assoc_id INTEGER, snap_dt DATE, period_end_dt DATE, status_cd TEXT, lvl_2_id INTEGER, PRIMARY KEY (assoc_id, period_end_dt, snap_dt));"
    sc = Scorer("duckdb", catalog=ddl, snapshot_columns={"snap": "snap_dt"})
    keys = sc.explain("WITH org AS (SELECT DISTINCT assoc_id FROM snap WHERE lvl_2_id = 7) SELECT COUNT(*) FROM org", "headcount")
    assert not keys.has("snapshot_grain"), keys.summary()
    with_status = sc.explain("WITH org AS (SELECT DISTINCT assoc_id, status_cd FROM snap WHERE lvl_2_id = 7) SELECT COUNT(*) FROM org", "headcount")
    assert with_status.has("snapshot_grain")  # status varies across snapshots: one person, several rows
    assert sc.explain("SELECT COUNT(*) FROM snap WHERE lvl_2_id = 7", "headcount").has("snapshot_grain")
