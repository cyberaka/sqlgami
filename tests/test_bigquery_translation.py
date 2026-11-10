"""BigQuery → DuckDB for the fixture stage (ADR 0003, B1): every construct on the allow-list
has a case here whose translated result equals what BigQuery returns for the same data; a
construct without a case is untranslatable and the fixture stage says so. The expectations are
BigQuery's documented semantics; the offline adapter (B2) re-checks them against the service."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlglot

from sqlgami.translate import CORE, Translation, Untranslatable, constructs_of, map_tables, translate

duckdb = pytest.importorskip("duckdb")

D = dt.date


@pytest.fixture(scope="module")
def conn():
    c = duckdb.connect()
    c.execute("SET TimeZone = 'UTC'")  # as sqlgami's fixture connections: BigQuery reads a bare TIMESTAMP as UTC
    c.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES "
        "(1, DATE '2024-03-15', 10, 2, 'a', TIMESTAMP '2024-03-15 10:20:30'), "
        "(2, DATE '2024-01-31', 7, 0, 'b', TIMESTAMP '2024-01-31 23:59:59'), "
        "(3, DATE '2024-02-29', NULL, 4, 'a', NULL)) v(id, d, x, y, g, ts)"
    )
    return c


def _norm(value):
    from decimal import Decimal

    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, dt.datetime) and value.tzinfo is None and value.time() == dt.time():
        return value.date()  # DuckDB gives DATE ± INTERVAL as a midnight TIMESTAMP; BigQuery keeps DATE
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


CASES = [
    # (BigQuery SQL, expected rows in BigQuery semantics, constructs the case verifies)
    ("SELECT id, DATE_SUB(d, INTERVAL 1 DAY) AS v FROM t ORDER BY id", [(1, D(2024, 3, 14)), (2, D(2024, 1, 30)), (3, D(2024, 2, 28))], {"func:DATE_SUB", "part:DAY"}),
    ("SELECT id, DATE_ADD(d, INTERVAL 2 MONTH) AS v FROM t ORDER BY id", [(1, D(2024, 5, 15)), (2, D(2024, 3, 31)), (3, D(2024, 4, 29))], {"func:DATE_ADD", "part:MONTH"}),
    ("SELECT id, DATE_TRUNC(d, MONTH) AS v FROM t ORDER BY id", [(1, D(2024, 3, 1)), (2, D(2024, 1, 1)), (3, D(2024, 2, 1))], {"func:DATE_TRUNC"}),
    ("SELECT id, DATE_TRUNC(d, QUARTER) AS v FROM t ORDER BY id", [(1, D(2024, 1, 1)), (2, D(2024, 1, 1)), (3, D(2024, 1, 1))], {"part:QUARTER"}),
    ("SELECT id, DATE_DIFF(d, DATE '2024-01-01', DAY) AS v FROM t ORDER BY id", [(1, 74), (2, 30), (3, 59)], {"func:DATEDIFF"}),  # first minus second
    ("SELECT DATE_DIFF(DATE '2024-03-15', DATE '2024-01-01', WEEK) AS v", [(10,)], {"part:WEEK"}),  # Sunday-week boundaries crossed between a Monday and a Friday ten weeks on
    ("SELECT DATE_DIFF(DATE '2024-03-15', DATE '2024-01-01', MONTH) AS v, DATE_DIFF(DATE '2025-01-01', DATE '2024-03-15', YEAR) AS w", [(2, 1)], {"part:YEAR"}),
    ("SELECT id, EXTRACT(MONTH FROM d) AS v, EXTRACT(YEAR FROM d) AS w FROM t ORDER BY id", [(1, 3, 2024), (2, 1, 2024), (3, 2, 2024)], {"func:EXTRACT"}),
    ("SELECT id, FORMAT_DATE('%Y-%m', d) AS v FROM t ORDER BY id", [(1, "2024-03"), (2, "2024-01"), (3, "2024-02")], {"func:TIME_TO_STR", "func:TS_OR_DS_TO_DATE"}),
    ("SELECT id, SAFE_DIVIDE(x, y) AS v FROM t ORDER BY id", [(1, 5), (2, None), (3, None)], {"func:SAFE_DIVIDE"}),
    ("SELECT COUNTIF(x > 5) AS v, COUNT(*) AS n, SUM(x) AS s, MIN(d) AS lo, MAX(d) AS hi, AVG(y) AS a FROM t", [(2, 3, 17, D(2024, 1, 31), D(2024, 3, 15), 2)], {"func:COUNT_IF", "func:COUNT", "func:SUM", "func:MIN", "func:MAX", "func:AVG"}),
    ("SELECT id, IFNULL(x, -1) AS v, COALESCE(x, y) AS w FROM t ORDER BY id", [(1, 10, 10), (2, 7, 7), (3, -1, 4)], {"func:COALESCE"}),
    ("SELECT id, IF(x > 5, 'big', 'small') AS v FROM t ORDER BY id", [(1, "big"), (2, "big"), (3, "small")], {"func:IF"}),
    ("SELECT id, g FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY d) = 1 ORDER BY id", [(2, "b"), (3, "a")], {"clause:qualify", "clause:window", "func:ROW_NUMBER"}),
    ("SELECT id, RANK() OVER (ORDER BY y DESC) AS r, DENSE_RANK() OVER (ORDER BY y DESC) AS dr, LAG(id) OVER (ORDER BY id) AS p, LEAD(id) OVER (ORDER BY id) AS n FROM t ORDER BY id", [(1, 2, 2, None, 2), (2, 3, 3, 1, 3), (3, 1, 1, 2, None)], {"func:RANK", "func:DENSE_RANK", "func:LAG", "func:LEAD"}),
    ("SELECT v FROM UNNEST([1, 2, 3]) AS v ORDER BY v", [(1,), (2,), (3,)], {"clause:unnest", "clause:array", "func:UNNEST", "func:ARRAY"}),
    ("SELECT ARRAY_LENGTH(ARRAY_AGG(STRUCT(id, g))) AS v FROM t", [(3,)], {"func:ARRAY_SIZE", "func:ARRAY_AGG", "func:STRUCT", "clause:struct"}),
    ("SELECT STRING_AGG(g, ',' ORDER BY id) AS v, ANY_VALUE(g) AS a, APPROX_COUNT_DISTINCT(g) AS c FROM t", [("a,b,a", "a", 2)], {"func:GROUP_CONCAT", "func:ANY_VALUE", "func:APPROX_DISTINCT"}),
    ("SELECT SAFE_CAST('x' AS INT64) AS v, CAST(7 AS INT64) AS w, CAST('2024-01-05' AS DATE) AS d, CAST(1.5 AS STRING) AS s", [(None, 7, D(2024, 1, 5), "1.5")], {"func:TRY_CAST", "func:CAST", "type:BIGINT", "type:DATE", "type:TEXT"}),
    ("SELECT CAST(x AS FLOAT64) / 4 AS v, CAST(TRUE AS BOOL) AS b, CAST(1 AS NUMERIC) AS n, CAST('2024-01-05 10:00:00' AS TIMESTAMP) AS ts FROM t WHERE id = 1", [(2.5, True, 1, dt.datetime(2024, 1, 5, 10, 0))], {"type:DOUBLE", "type:BOOLEAN", "type:DECIMAL", "type:TIMESTAMPTZ"}),
    ("SELECT id, LAST_DAY(d) AS v FROM t ORDER BY id", [(1, D(2024, 3, 31)), (2, D(2024, 1, 31)), (3, D(2024, 2, 29))], {"func:LAST_DAY"}),
    ("SELECT ARRAY_LENGTH(GENERATE_DATE_ARRAY(DATE '2024-01-01', DATE '2024-01-10', INTERVAL 1 DAY)) AS v", [(10,)], {"func:GENERATE_DATE_ARRAY"}),
    ("SELECT TIMESTAMP_TRUNC(TIMESTAMP '2024-03-15 10:20:30', HOUR) AS v", [(dt.datetime(2024, 3, 15, 10, 0),)], {"func:TIMESTAMP_TRUNC", "part:HOUR"}),
    ("SELECT EXTRACT(MINUTE FROM TIMESTAMP '2024-03-15 10:20:30') AS m, EXTRACT(SECOND FROM TIMESTAMP '2024-03-15 10:20:30') AS s", [(20, 30)], {"part:MINUTE", "part:SECOND"}),
    ("SELECT DIV(7, 2) AS a, MOD(7, 2) AS b, CAST(FLOOR(7 * 10000 / 3) AS INT64) AS c, ROUND(2.345, 2) AS r, ABS(-3) AS ab, CEIL(1.2) AS ce", [(3, 1, 23333, 2.35, 3, 2)], {"func:FLOOR", "func:ROUND", "func:ABS", "func:CEIL"}),
    ("SELECT DATE('2024-01-05') AS d, PARSE_DATE('%Y%m%d', '20240105') AS p, DATE_DIFF(CURRENT_DATE(), CURRENT_DATE(), DAY) AS z, DATE(TIMESTAMP '2024-03-15 10:20:30') AS t", [(D(2024, 1, 5), D(2024, 1, 5), 0, D(2024, 3, 15))], {"func:DATE", "func:STR_TO_DATE", "func:CURRENT_DATE"}),
    ("SELECT LOWER('Ab') AS l, UPPER('Ab') AS u, TRIM(' x ') AS t, LENGTH('abc') AS n, CONCAT('a', 'b') AS c, SUBSTR('abcdef', 2, 3) AS s, STARTS_WITH('abc', 'ab') AS st, ENDS_WITH('abc', 'bc') AS en, REGEXP_CONTAINS('abc', 'b.') AS re", [("ab", "AB", "x", 3, "ab", "bcd", True, True, True)], {"func:LOWER", "func:UPPER", "func:TRIM", "func:LENGTH", "func:CONCAT", "func:SUBSTRING", "func:STARTS_WITH", "func:ENDS_WITH", "func:REGEXP_LIKE"}),
    ("SELECT NULLIF(3, 3) AS n, GREATEST(1, 5, 3) AS g, LEAST(1, 5, 3) AS l", [(None, 5, 1)], {"func:NULLIF", "func:GREATEST", "func:LEAST"}),
    ("SELECT FIRST_VALUE(id) OVER (ORDER BY d) AS f, LAST_VALUE(id) OVER (ORDER BY d ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS l FROM t ORDER BY id LIMIT 1", [(2, 1)], {"func:FIRST_VALUE", "func:LAST_VALUE"}),
    ("SELECT id FROM t WHERE d >= (SELECT MIN(d) FROM t) AND d < DATE_ADD((SELECT MIN(d) FROM t), INTERVAL 1 MONTH) ORDER BY id", [(2,)], set()),  # scalar-subquery date bounds; Jan 31 + 1 month clamps to Feb 29
    ("SELECT g, SUM(n) AS total FROM (SELECT g, d, COUNT(*) AS n FROM t GROUP BY g, d) GROUP BY g ORDER BY g", [("a", 2), ("b", 1)], set()),  # two-level aggregation
]


@pytest.mark.parametrize("sql,expected,verifies", CASES, ids=[c[2].__repr__()[:40] if c[2] else c[0][:30] for c in CASES])
def test_translated_result_equals_bigquery_semantics(conn, sql, expected, verifies):
    translation = translate(sql, "bigquery")
    rows = [tuple(_norm(v) for v in row) for row in conn.execute(translation.sql).fetchall()]
    assert rows == [tuple(_norm(v) for v in row) for row in expected], translation.sql
    assert verifies <= translation.constructs, (verifies - translation.constructs, translation.constructs)


def test_every_allow_listed_construct_has_a_case():
    covered = set().union(*(c[2] for c in CASES))
    assert covered >= CORE, sorted(CORE - covered)


def test_unverified_constructs_are_refused_by_name():
    with pytest.raises(Untranslatable, match="untranslatable: func:JSON_EXTRACT_SCALAR"):
        translate("SELECT JSON_EXTRACT_SCALAR(g, '$.a') FROM t", "bigquery")
    with pytest.raises(Untranslatable, match="part:ISOWEEK"):
        translate("SELECT DATE_TRUNC(d, ISOWEEK) FROM t", "bigquery")
    assert translate("SELECT 1", "duckdb") == Translation("SELECT 1")


def test_host_declared_table_map_rewrites_names_and_refuses_unmapped_projects():
    sql = "SELECT s.assoc_id FROM `my-proj.hr_raw.emp_period_snapshot` AS s JOIN hr.pay_period_dim p ON p.ppe_dt = s.period_end_dt"
    translation = translate(sql, "bigquery", tables={"`my-proj.hr_raw.emp_period_snapshot`": "hr.emp_period_snapshot"})
    assert translation.tables == {"my-proj.hr_raw.emp_period_snapshot": "hr.emp_period_snapshot"}
    assert '"hr"."emp_period_snapshot"' in translation.sql.replace('"', '"') or "hr.emp_period_snapshot" in translation.sql
    assert "my-proj" not in translation.sql
    with pytest.raises(Untranslatable, match="no fixture table declared"):
        translate(sql, "bigquery", tables={})
    # CTE names are never tables to map
    tree = sqlglot.parse_one("WITH cur AS (SELECT 1 AS a) SELECT a FROM cur", read="bigquery")
    assert map_tables(tree, {}) == {}
    assert constructs_of(tree) == set()


BQ_DDL = """
CREATE TABLE shop.customers (id INT64, name STRING, region STRING);
CREATE TABLE shop.orders (id INT64, customer_id INT64, total NUMERIC, status STRING, created_at DATE);
"""
BQ_TERMS = "term,phrases,kind,table,sql,nulls,description\npaid,paid,filter,shop.orders,status = 'paid',,\n"
REF = (
    "SELECT c.region, COUNTIF(o.status = 'paid') AS paid_n, SAFE_DIVIDE(SUM(o.total), COUNT(*)) AS avg_total "
    "FROM `proj-x.shop.orders` AS o JOIN `proj-x.shop.customers` AS c ON c.id = o.customer_id "
    "WHERE o.created_at >= DATE_SUB(DATE '2024-07-01', INTERVAL 6 MONTH) GROUP BY c.region"
)


@pytest.fixture(scope="module")
def bq_bundle(tmp_path_factory):
    from sqlgami.bundle import compile_bundle

    tmp = tmp_path_factory.mktemp("bq")
    (tmp / "schema.sql").write_text(BQ_DDL)
    (tmp / "terms").mkdir()
    (tmp / "terms" / "Terms.csv").write_text(BQ_TERMS)
    (tmp / "terms" / "Examples.csv").write_text(
        "id,question,params,correct_sql,expected_result,dataset\n"
        f'per_region,Paid order count and average order total per region over the last six months,,"{REF}",,\n'
    )
    (tmp / "sqlgami.yaml").write_text(
        "dialect: bigquery\nschema: schema.sql\nterms: terms\n"
        "tables: {'`proj-x.shop.orders`': shop.orders, '`proj-x.shop.customers`': shop.customers}\n"
        "fixtures: {seed: 3, profiles: [clean, nulls], rows: {shop.customers: 6, shop.orders: 40}, values: {shop.orders.status: [paid, open]}, dates: {start: '2024-01-01', end: '2024-12-31'}}\n"
    )
    compile_bundle(tmp / "sqlgami.yaml", tmp / "bundle")
    return tmp / "bundle"


def test_bigquery_statements_are_judged_on_the_fixtures_through_the_verified_translation(bq_bundle):
    from sqlgami import Scorer
    from sqlgami.report import StageStatus

    manifest = __import__("json").loads((bq_bundle / "manifest.json").read_text())
    assert manifest["dialect"] == "bigquery" and manifest["references"] == 1 and manifest["tables"]["`proj-x.shop.orders`"] == "shop.orders"
    question = "Paid order count and average order total per region over the last six months"
    with Scorer("bigquery", catalog=BQ_DDL, bundle=bq_bundle) as scorer:
        same = scorer.explain(REF, question)
        assert same.signals["fixtures"]["verdict"] == "match", same.summary()
        # the owner's other shapes: QUALIFY over a two-level aggregation, with a different result
        ranked = scorer.explain(
            "SELECT region, paid_n FROM (SELECT c.region, COUNTIF(o.status = 'paid') AS paid_n, SAFE_DIVIDE(SUM(o.total), COUNT(*)) AS avg_total "
            "FROM `proj-x.shop.orders` AS o JOIN `proj-x.shop.customers` AS c ON c.id = o.customer_id "
            "WHERE o.created_at >= DATE_SUB(DATE '2024-07-01', INTERVAL 6 MONTH) GROUP BY c.region) "
            "QUALIFY ROW_NUMBER() OVER (ORDER BY paid_n DESC) = 1",
            question,
        )
        assert ranked.signals["fixtures"]["verdict"] == "mismatch" and ranked.has("fixture_mismatch")
        # an unverified construct: the fixtures cannot judge; not_run names it, no verdict
        json_sql = REF.replace("c.region", "JSON_EXTRACT_SCALAR(c.name, '$.r')").replace("GROUP BY c.region", "GROUP BY 1")
        unverified = scorer.explain(json_sql, question)
        assert unverified.stage("fixtures").status == StageStatus.NOT_RUN
        assert "untranslatable: func:JSON_EXTRACT_SCALAR" in (unverified.stage("fixtures").detail or "")
        assert unverified.signals["fixtures"]["untranslatable"] == "func:JSON_EXTRACT_SCALAR" and not unverified.has("fixture_mismatch")
        # a project the host did not map
        other = scorer.explain(REF.replace("`proj-x.shop.customers`", "`other-proj.shop.customers`"), question)
        assert other.stage("fixtures").status == StageStatus.NOT_RUN and "no fixture table declared" in (other.stage("fixtures").detail or "")
