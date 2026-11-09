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
    assert len(found) == 2 and found[0].severity == Severity.MEDIUM
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
    ):
        assert not scorer.sample(sql, question).has("comparison_operator"), sql


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
        # A table only passed through on the way to another is a lesser matter.
        bridge = required.sample(
            "SELECT o.id, o.total FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.total > 0 ORDER BY o.id"
        )
        assert next(f for f in bridge.findings if f.code == "soft_deleted_rows").severity == Severity.MEDIUM


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
