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
        "(3, DATE '2024-02-29', NULL, 4, 'a', NULL), "
        "(4, DATE '2024-04-30', NULL, 1, 'c', NULL)) v(id, d, x, y, g, ts)"  # group c: x is only NULL
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
    ("SELECT id, DATE_SUB(d, INTERVAL 1 DAY) AS v FROM t ORDER BY id", [(1, D(2024, 3, 14)), (2, D(2024, 1, 30)), (3, D(2024, 2, 28)), (4, D(2024, 4, 29))], {"func:DATE_SUB", "part:DAY"}),
    ("SELECT id, DATE_ADD(d, INTERVAL 2 MONTH) AS v FROM t ORDER BY id", [(1, D(2024, 5, 15)), (2, D(2024, 3, 31)), (3, D(2024, 4, 29)), (4, D(2024, 6, 30))], {"func:DATE_ADD", "part:MONTH"}),
    ("SELECT id, DATE_TRUNC(d, MONTH) AS v FROM t ORDER BY id", [(1, D(2024, 3, 1)), (2, D(2024, 1, 1)), (3, D(2024, 2, 1)), (4, D(2024, 4, 1))], {"func:DATE_TRUNC"}),
    ("SELECT id, DATE_TRUNC(d, QUARTER) AS v FROM t ORDER BY id", [(1, D(2024, 1, 1)), (2, D(2024, 1, 1)), (3, D(2024, 1, 1)), (4, D(2024, 4, 1))], {"part:QUARTER"}),
    ("SELECT id, DATE_DIFF(d, DATE '2024-01-01', DAY) AS v FROM t ORDER BY id", [(1, 74), (2, 30), (3, 59), (4, 120)], {"func:DATEDIFF"}),  # first minus second
    ("SELECT DATE_DIFF(DATE '2024-03-15', DATE '2024-01-01', WEEK) AS v", [(10,)], {"part:WEEK"}),  # Sunday-week boundaries crossed between a Monday and a Friday ten weeks on
    ("SELECT DATE_DIFF(DATE '2024-03-15', DATE '2024-01-01', MONTH) AS v, DATE_DIFF(DATE '2025-01-01', DATE '2024-03-15', YEAR) AS w", [(2, 1)], {"part:YEAR"}),
    ("SELECT id, EXTRACT(MONTH FROM d) AS v, EXTRACT(YEAR FROM d) AS w FROM t ORDER BY id", [(1, 3, 2024), (2, 1, 2024), (3, 2, 2024), (4, 4, 2024)], {"func:EXTRACT"}),
    ("SELECT id, FORMAT_DATE('%Y-%m', d) AS v FROM t ORDER BY id", [(1, "2024-03"), (2, "2024-01"), (3, "2024-02"), (4, "2024-04")], {"func:TIME_TO_STR", "func:TS_OR_DS_TO_DATE"}),
    ("SELECT id, SAFE_DIVIDE(x, y) AS v FROM t ORDER BY id", [(1, 5), (2, None), (3, None), (4, None)], {"func:SAFE_DIVIDE"}),
    ("SELECT g, COUNTIF(x > 5) AS n FROM t GROUP BY g ORDER BY g", [("a", 1), ("b", 1), ("c", 0)], {"func:COUNT_IF"}),  # an all-NULL group is 0, not NULL
    ("SELECT COUNTIF(x > 5) AS n FROM t WHERE id > 100", [(0,)], set()),  # and so is an empty set
    ("SELECT COUNTIF(x > 5) AS v, COUNT(*) AS n, SUM(x) AS s, MIN(d) AS lo, MAX(d) AS hi, AVG(y) AS a FROM t", [(2, 4, 17, D(2024, 1, 31), D(2024, 4, 30), 1.75)], {"func:COUNT_IF", "func:COUNT", "func:SUM", "func:MIN", "func:MAX", "func:AVG"}),
    ("SELECT id, IFNULL(x, -1) AS v, COALESCE(x, y) AS w FROM t ORDER BY id", [(1, 10, 10), (2, 7, 7), (3, -1, 4), (4, -1, 1)], {"func:COALESCE"}),
    ("SELECT id, CASE WHEN x > 5 THEN 'big' WHEN x IS NULL THEN 'none' ELSE 'small' END AS v, CASE x WHEN 7 THEN 'seven' ELSE 'other' END AS w FROM t ORDER BY id", [(1, "big", "other"), (2, "big", "seven"), (3, "none", "other"), (4, "none", "other")], {"func:CASE"}),
    ("SELECT id, IF(x > 5, 'big', 'small') AS v FROM t ORDER BY id", [(1, "big"), (2, "big"), (3, "small"), (4, "small")], {"func:IF"}),
    ("SELECT id, g FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY d) = 1 ORDER BY id", [(2, "b"), (3, "a"), (4, "c")], {"clause:qualify", "clause:window", "func:ROW_NUMBER"}),
    ("SELECT id, RANK() OVER (ORDER BY y DESC) AS r, DENSE_RANK() OVER (ORDER BY y DESC) AS dr, LAG(id) OVER (ORDER BY id) AS p, LEAD(id) OVER (ORDER BY id) AS n FROM t ORDER BY id", [(1, 2, 2, None, 2), (2, 4, 4, 1, 3), (3, 1, 1, 2, 4), (4, 3, 3, 3, None)], {"func:RANK", "func:DENSE_RANK", "func:LAG", "func:LEAD"}),
    ("SELECT v FROM UNNEST([1, 2, 3]) AS v ORDER BY v", [(1,), (2,), (3,)], {"clause:unnest", "clause:array", "func:UNNEST", "func:ARRAY"}),
    ("SELECT ARRAY_LENGTH(ARRAY_AGG(STRUCT(id, g))) AS v FROM t", [(4,)], {"func:ARRAY_SIZE", "func:ARRAY_AGG", "func:STRUCT", "clause:struct"}),
    ("SELECT STRING_AGG(g, ',' ORDER BY id) AS v, ANY_VALUE(g) AS a FROM t", [("a,b,a,c", "a")], {"func:GROUP_CONCAT", "func:ANY_VALUE"}),  # APPROX_COUNT_DISTINCT is not verified: the HLL sketches differ between engines
    # the week starts BigQuery knows, in the type of the argument (a DATE stays a DATE)
    ("SELECT id, DATE_TRUNC(d, WEEK) AS su, DATE_TRUNC(d, WEEK(MONDAY)) AS mo, DATE_TRUNC(d, WEEK(SATURDAY)) AS sa, EXTRACT(WEEK FROM d) AS wk, LAST_DAY(d, WEEK) AS ld FROM t ORDER BY id", [(1, D(2024, 3, 10), D(2024, 3, 11), D(2024, 3, 9), 10, D(2024, 3, 16)), (2, D(2024, 1, 28), D(2024, 1, 29), D(2024, 1, 27), 4, D(2024, 2, 3)), (3, D(2024, 2, 25), D(2024, 2, 26), D(2024, 2, 24), 8, D(2024, 3, 2)), (4, D(2024, 4, 28), D(2024, 4, 29), D(2024, 4, 27), 17, D(2024, 5, 4))], set()),
    # UNNEST ... WITH OFFSET counts from 0
    ("SELECT e, off FROM UNNEST(['x', 'y', 'z']) AS e WITH OFFSET AS off WHERE off > 0 ORDER BY off", [("y", 1), ("z", 2)], set()),
    # LIKE escapes with a backslash; `/` is floating-point division that raises on zero; a subscript raises out of range
    ("SELECT id FROM t WHERE g LIKE 'a%' AND 'a_c' LIKE 'a\\\\_c' AND NOT ('axc' LIKE 'a\\\\_c') ORDER BY id", [(1,), (3,)], {"op:LIKE"}),
    ("SELECT CAST(7 / 2 AS STRING) AS a, CAST(6 / 2 AS STRING) AS b, SAFE_DIVIDE(1, 0) AS c, ['p', 'q'][OFFSET(1)] AS d, ['p', 'q'][SAFE_OFFSET(5)] AS e", [("3.5", "3", None, "q", None)], {"op:/", "op:[]"}),
    # FORMAT_DATE / PARSE_DATE specifiers proven to render alike
    ("SELECT FORMAT_DATE('%Y-%m-%d %y %b %B %a %A %j %U %W %V %u %w %%', DATE '2024-01-07') AS v", [("2024-01-07 24 Jan January Sun Sunday 007 01 01 01 7 0 %",)], {"fmt:%Y", "fmt:%m", "fmt:%d", "fmt:%y", "fmt:%b", "fmt:%B", "fmt:%a", "fmt:%A", "fmt:%j", "fmt:%U", "fmt:%W", "fmt:%V", "fmt:%u", "fmt:%w", "fmt:%%"}),
    # a float literal is FLOAT64, so ROUND rounds the binary value; CAST(FLOAT64 AS STRING) prints as BigQuery does; NUMERIC keeps scale 9
    ("SELECT CAST(ROUND(2.355, 2) AS STRING) AS a, CAST(ROUND(1.005, 2) AS STRING) AS b, CAST(0.1 + 0.2 AS STRING) AS c, CAST(CAST(10 AS NUMERIC) / 3 AS STRING) AS d, CAST(2.5 AS INT64) AS e, SAFE_CAST('1.5' AS INT64) AS f, SUBSTR('abcdef', 0, 2) AS g", [("2.35", "1", "0.30000000000000004", "3.333333333", 3, None, "ab")], set()),
    ("SELECT SAFE_CAST('x' AS INT64) AS v, CAST(7 AS INT64) AS w, CAST('2024-01-05' AS DATE) AS d, CAST(1.5 AS STRING) AS s", [(None, 7, D(2024, 1, 5), "1.5")], {"func:TRY_CAST", "func:CAST", "type:BIGINT", "type:DATE", "type:TEXT"}),
    ("SELECT CAST(x AS FLOAT64) / 4 AS v, CAST(TRUE AS BOOL) AS b, CAST(1 AS NUMERIC) AS n, CAST('2024-01-05 10:00:00' AS TIMESTAMP) AS ts FROM t WHERE id = 1", [(2.5, True, 1, dt.datetime(2024, 1, 5, 10, 0))], {"type:DOUBLE", "type:BOOLEAN", "type:DECIMAL", "type:TIMESTAMPTZ"}),
    ("SELECT id, LAST_DAY(d) AS v FROM t ORDER BY id", [(1, D(2024, 3, 31)), (2, D(2024, 1, 31)), (3, D(2024, 2, 29)), (4, D(2024, 4, 30))], {"func:LAST_DAY"}),
    ("SELECT ARRAY_LENGTH(GENERATE_DATE_ARRAY(DATE '2024-01-01', DATE '2024-01-10', INTERVAL 1 DAY)) AS v", [(10,)], {"func:GENERATE_DATE_ARRAY"}),
    ("SELECT TIMESTAMP_TRUNC(TIMESTAMP '2024-03-15 10:20:30', HOUR) AS v", [(dt.datetime(2024, 3, 15, 10, 0),)], {"func:TIMESTAMP_TRUNC", "part:HOUR"}),
    ("SELECT EXTRACT(MINUTE FROM TIMESTAMP '2024-03-15 10:20:30') AS m, EXTRACT(SECOND FROM TIMESTAMP '2024-03-15 10:20:30') AS s", [(20, 30)], {"part:MINUTE", "part:SECOND"}),
    ("SELECT DIV(7, 2) AS a, MOD(7, 2) AS b, CAST(FLOOR(7 * 10000 / 3) AS INT64) AS c, ROUND(2.345, 2) AS r, ABS(-3) AS ab, CEIL(1.2) AS ce", [(3, 1, 23333, 2.35, 3, 2)], {"func:FLOOR", "func:ROUND", "func:ABS", "func:CEIL"}),
    ("SELECT DATE('2024-01-05') AS d, PARSE_DATE('%Y%m%d', '20240105') AS p, DATE_DIFF(CURRENT_DATE(), CURRENT_DATE(), DAY) AS z, DATE(TIMESTAMP '2024-03-15 10:20:30') AS t", [(D(2024, 1, 5), D(2024, 1, 5), 0, D(2024, 3, 15))], {"func:DATE", "func:STR_TO_DATE", "func:CURRENT_DATE"}),
    ("SELECT LOWER('Ab') AS l, UPPER('Ab') AS u, TRIM(' x ') AS t, LENGTH('abc') AS n, CONCAT('a', 'b') AS c, SUBSTR('abcdef', 2, 3) AS s, STARTS_WITH('abc', 'ab') AS st, ENDS_WITH('abc', 'bc') AS en, REGEXP_CONTAINS('abc', 'b.') AS re", [("ab", "AB", "x", 3, "ab", "bcd", True, True, True)], {"func:LOWER", "func:UPPER", "func:TRIM", "func:LENGTH", "func:CONCAT", "func:SUBSTRING", "func:STARTS_WITH", "func:ENDS_WITH", "func:REGEXP_LIKE"}),
    ("SELECT NULLIF(3, 3) AS n, GREATEST(1, 5, 3) AS g, LEAST(1, 5, 3) AS l", [(None, 5, 1)], {"func:NULLIF", "func:GREATEST", "func:LEAST"}),
    ("SELECT FIRST_VALUE(id) OVER (ORDER BY d) AS f, LAST_VALUE(id) OVER (ORDER BY d ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS l FROM t ORDER BY id LIMIT 1", [(2, 4)], {"func:FIRST_VALUE", "func:LAST_VALUE"}),
    ("SELECT id FROM t WHERE d >= (SELECT MIN(d) FROM t) AND d < DATE_ADD((SELECT MIN(d) FROM t), INTERVAL 1 MONTH) ORDER BY id", [(2,)], set()),  # scalar-subquery date bounds; Jan 31 + 1 month clamps to Feb 29
    ("SELECT g, SUM(n) AS total FROM (SELECT g, d, COUNT(*) AS n FROM t GROUP BY g, d) GROUP BY g ORDER BY g", [("a", 2), ("b", 1), ("c", 1)], set()),  # two-level aggregation
    ("SELECT id FROM t AS a WHERE EXISTS (SELECT 1 FROM t AS b WHERE b.g = a.g AND b.id <> a.id) ORDER BY id", [(1,), (3,)], {"func:EXISTS"}),
    ("SELECT g, COUNT(DISTINCT IF(x > 5, id, NULL)) AS big FROM t GROUP BY g ORDER BY g", [("a", 1), ("b", 1), ("c", 0)], set()),  # IF() conditional aggregation
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
    assert manifest["dialect"] == "bigquery" and manifest["references"] == 1 and manifest["tables"]["proj-x.shop.orders"] == "shop.orders"
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


@pytest.mark.skipif(__import__("os").environ.get("SQLGAMI_BQ_LIVE") != "1", reason="native proof runs only with SQLGAMI_BQ_LIVE=1 against the sandbox")
def test_native_bigquery_agrees_with_the_translation_on_every_case(conn, tmp_path):
    """Ground truth for the allow-list (ADR 0003 B2): the same fixture rows in the sandbox, every
    case run natively, normalised rows equal to the DuckDB translation's."""
    from sqlgami.contrib.bigquery import BigQuerySandbox

    sandbox = BigQuerySandbox(ledger=tmp_path / "ledger.jsonl")
    dataset = sandbox.ensure_dataset("bench_translation", note="fidelity fixture for tests/test_bigquery_translation.py")
    rows = conn.execute("SELECT id, d, x, y, g, ts FROM t ORDER BY id").fetchall()
    sandbox.load_table(dataset, "t", [("id", "INT64"), ("d", "DATE"), ("x", "INT64"), ("y", "INT64"), ("g", "STRING"), ("ts", "TIMESTAMP")], rows)
    disagreements = []
    for sql, expected, verifies in CASES:
        native_sql = sql.replace(" FROM t", f" FROM `{sandbox.project}.{dataset}.t`").replace("(SELECT MIN(d) FROM t)", f"(SELECT MIN(d) FROM `{sandbox.project}.{dataset}.t`)")
        native = sandbox.run(native_sql)
        assert native.ok, (sql, native.error)
        translated = [tuple(_norm(v) for v in row) for row in conn.execute(translate(sql, "bigquery").sql).fetchall()]
        got = [tuple(_norm(v) for v in row) for row in native.rows]
        if sorted(map(repr, got)) != sorted(map(repr, translated)):
            disagreements.append((sql, got, translated))
    assert not disagreements, disagreements


SCRIPT_TERMS = BQ_TERMS + "recent,recent;last {n} days,window,shop.orders,created_at >= DATE_SUB(DATE '2024-07-01'{comma} INTERVAL {n} DAY),,\n".replace("{comma}", ",")


def test_declare_and_set_are_bound_into_the_final_query(bq_bundle):
    """DECLARE … DEFAULT, SET (incl. reassignment and tuple SET), a variable declared without a
    default and SET later, a variable used twice, an ARRAY used with IN UNNEST, and a scalar
    subquery default: all bound before the term checks and the fixture run."""
    from sqlgami import analysis as A

    script = """
    DECLARE start_dt DATE DEFAULT DATE_SUB(DATE '2024-07-01', INTERVAL 6 MONTH);
    DECLARE cutoff DATE;
    DECLARE regions ARRAY<STRING> DEFAULT ['north', 'south'];
    DECLARE min_total NUMERIC DEFAULT 5;
    SET cutoff = DATE '2024-07-01';
    SET min_total = min_total * 2;
    SET (lo, hi) = (1, 100);
    SELECT c.region, COUNTIF(o.status = 'paid') AS paid_n, SAFE_DIVIDE(SUM(o.total), COUNT(*)) AS avg_total
    FROM `proj-x.shop.orders` AS o JOIN `proj-x.shop.customers` AS c ON c.id = o.customer_id
    WHERE o.created_at >= start_dt AND o.created_at < cutoff AND o.total >= min_total AND o.total BETWEEN lo AND hi
      AND c.region IN UNNEST(regions)
    GROUP BY c.region
    """
    bound, unbound = A.bind_script_text(script, "bigquery")
    text = bound.sql(dialect="bigquery")
    assert unbound == []
    assert "start_dt" not in text and "cutoff" not in text and "min_total" not in text and "regions" not in text
    assert "INTERVAL" in text and "'2024-07-01'" in text and "(5 * 2)" in text and "BETWEEN 1 AND 100" in text and "['north', 'south']" in text
    # the fixture stage runs the bound query through the translation
    from sqlgami.bundle import _executable

    executable = _executable(script, "bigquery", {}, None, {"proj-x.shop.orders": "shop.orders", "proj-x.shop.customers": "shop.customers"})
    assert "shop.orders" in executable and "UNNEST" in executable
    # a variable the script never gives a value is not guessed
    bound, unbound = A.bind_script_text("DECLARE k INT64;\nSELECT id FROM `proj-x.shop.orders` WHERE customer_id = k", "bigquery")
    assert unbound == ["k"]
    from sqlgami.translate import Untranslatable

    with pytest.raises(Untranslatable, match="script variable k has no value"):
        _executable("DECLARE k INT64;\nSELECT id FROM `proj-x.shop.orders` WHERE customer_id = k", "bigquery", {}, None, {"proj-x.shop.orders": "shop.orders"})


def test_a_script_gets_a_fixture_verdict_and_its_terms_see_the_bound_values(bq_bundle):
    from sqlgami import Scorer
    from sqlgami.report import StageStatus

    question = "Paid order count and average order total per region over the last six months"
    script = (
        "DECLARE since DATE DEFAULT DATE_SUB(DATE '2024-07-01', INTERVAL 6 MONTH);\n"
        "SELECT c.region, COUNTIF(o.status = 'paid') AS paid_n, SAFE_DIVIDE(SUM(o.total), COUNT(*)) AS avg_total "
        "FROM `proj-x.shop.orders` AS o JOIN `proj-x.shop.customers` AS c ON c.id = o.customer_id "
        "WHERE o.created_at >= since GROUP BY c.region"
    )
    with Scorer("bigquery", catalog=BQ_DDL, bundle=bq_bundle) as scorer:
        report = scorer.explain(script, question)
        assert report.signals["script"]["statements"] == ["DECLARE", "SELECT"]
        assert report.signals["fixtures"]["verdict"] == "match", report.summary()
        assert report.stage("fixtures").status == StageStatus.PASSED
        # the same script with a shifted window differs from the reference: a verdict, not an error
        wrong = scorer.explain(script.replace("INTERVAL 6 MONTH", "INTERVAL 2 MONTH"), question)
        assert wrong.signals["fixtures"]["verdict"] == "mismatch"
        # an unbound variable: the fixtures cannot run it, and say so
        unbound = scorer.explain("DECLARE since DATE;\n" + script.split(";\n", 1)[1], question)
        assert unbound.stage("fixtures").status == StageStatus.NOT_RUN and "script variable since has no value" in (unbound.stage("fixtures").detail or "")


def test_group_by_survives_script_parsing():
    """sqlglot's multi-statement parse turns GROUP BY pp.ppe_dt into the projection alias; where a
    joined table also has a column of that name, the bare alias would bind to the column."""
    from sqlgami import analysis as A

    script = (
        "DECLARE n INT64 DEFAULT 1;\n"
        "SELECT pp.period_seq, pp.ppe_dt AS period_end_dt, COUNT(*) AS c FROM periods AS pp LEFT JOIN snap AS e ON e.period_end_dt = pp.ppe_dt "
        "WHERE pp.period_seq >= n GROUP BY pp.period_seq, pp.ppe_dt"
    )
    bound, _ = A.bind_script_text(script, "bigquery")
    assert bound.args["group"].sql() == "GROUP BY pp.period_seq, pp.ppe_dt"


def test_policies_and_terms_declared_without_a_project_apply_to_three_part_names():
    """A rule or term written for hr.snap applies to `proj-x.hr.snap`, and a view read by its
    three-part name is expanded to the base table the term is declared on."""
    from sqlgami import Scorer
    from sqlgami.terms import Catalogue

    ddl = """
    CREATE TABLE `proj-x.hr.snap` (assoc_id INT64, status_cd STRING, lvl_2_id INT64, lvl_3_id INT64, lvl_2_id_fn INT64, lvl_3_id_fn INT64);
    CREATE VIEW `proj-x.hr.current_v` AS SELECT * FROM `proj-x.hr.snap` WHERE status_cd = 'A';
    """
    chain = {"name": "access", "tables": ["hr.snap"], "match": "or_chain", "columns": ["lvl_2_id", "lvl_3_id", "lvl_2_id_fn", "lvl_3_id_fn"]}
    terms = Catalogue.from_sheets({"terms": "term,phrases,kind,table,sql,nulls,description\nactive,active,filter,hr.snap,status_cd = 'A',,\nmy_org,my organisation,chain,hr.snap,\"lvl_2_id, lvl_3_id, lvl_2_id_fn, lvl_3_id_fn = {manager_id}\",,\n", "parameters": "name,type,example_values\nmanager_id,integer,7\n"})
    sc = Scorer("bigquery", catalog=ddl, required_predicates=[chain], terms=terms)
    full = "SELECT COUNT(DISTINCT s.assoc_id) FROM `proj-x.hr.current_v` AS s WHERE s.status_cd = 'A' AND {manager_id} IN (s.lvl_2_id, s.lvl_3_id, s.lvl_2_id_fn, s.lvl_3_id_fn)"
    report = sc.explain(full, "active headcount of my organisation", params={"manager_id": 7})
    assert not report.has("required_predicate") and not report.has("term_source_not_read"), report.summary()
    assert [o["status"] for o in report.signals["terms"]["outcomes"] if o["term"] in ("active", "my_org")] == ["satisfied", "satisfied"]
    none = sc.explain("SELECT COUNT(DISTINCT s.assoc_id) FROM `proj-x.hr.current_v` AS s WHERE s.status_cd = 'A'", "active headcount of my organisation", params={"manager_id": 7})
    assert none.has("required_predicate")


def test_every_term_kind_and_policy_applies_to_three_part_names():
    """Every kind of term (filter, window, source, chain, rank) and every policy shape (or_chain,
    all, any) declared on dataset.table applies to `project.dataset.table`, and the catalogue's own
    tables in a bench suite are all reached that way."""
    from sqlgami import Catalog, Scorer
    from sqlgami.catalog import RequiredPredicate
    from sqlgami.terms import Catalogue

    ddl = """
    CREATE TABLE `proj-x.hr.snap` (assoc_id INT64, status_cd STRING, hire_dt DATE, lvl_2_id INT64, lvl_3_id INT64, period_seq INT64, tenant_id INT64, deleted_at DATE);
    CREATE TABLE `proj-x.hr.periods` (ppe_dt DATE, period_seq INT64);
    """
    catalog = Catalog.from_ddl(ddl, "bigquery")
    for rule in (
        RequiredPredicate("chain", ("lvl_2_id", "lvl_3_id"), ("hr.snap",), "or_chain", "high", ""),
        RequiredPredicate("tenant", ("tenant_id",), ("hr.snap",), "all", "high", ""),
        RequiredPredicate("live", ("deleted_at",), ("`hr.snap`",), "any", "medium", ""),
    ):
        table = next(t for t in (catalog.resolve("snap", "proj-x.hr"),) if t is not None)
        assert rule.applies_to(table), rule.name
    terms = Catalogue.from_sheets({"terms": (
        "term,phrases,kind,table,sql,nulls,description\n"
        "active,active,filter,hr.snap,status_cd = 'A',,\n"
        "last_n,last {n} periods,window,hr.periods,period_seq BETWEEN -({n}-1) AND 0,,\n"
        "hire,hire date,source,,hr.snap.hire_dt,,\n"
        "my_org,my organisation,chain,hr.snap,\"lvl_2_id, lvl_3_id = {manager_id}\",,\n"
        "top,top {n},rank,,LIMIT {n},,\n"
    ), "parameters": "name,type,example_values\nmanager_id,integer,7\n"})
    sc = Scorer("bigquery", catalog=ddl, terms=terms, required_predicates=[{"name": "chain", "tables": ["hr.snap"], "match": "or_chain", "columns": ["lvl_2_id", "lvl_3_id"]}])
    sql = ("SELECT s.assoc_id, s.hire_dt FROM `proj-x.hr.snap` AS s JOIN `proj-x.hr.periods` AS p ON p.ppe_dt = s.hire_dt "
           "WHERE s.status_cd = 'A' AND p.period_seq BETWEEN -7 AND 0 AND {manager_id} IN (s.lvl_2_id, s.lvl_3_id) ORDER BY s.hire_dt DESC LIMIT 5")
    report = sc.explain(sql, "The top 5 active people in my organisation by hire date over the last 8 periods", params={"manager_id": 7})
    outcomes = {o["term"]: o["status"] for o in report.signals["terms"]["outcomes"]}
    assert outcomes == {"active": "satisfied", "last_n": "satisfied", "hire": "satisfied", "my_org": "satisfied", "top": "satisfied"}, outcomes
    assert not report.has("term_source_not_read") and not report.has("required_predicate")
    # the policy still bites when the chain is absent
    assert sc.explain(sql.replace(" AND {manager_id} IN (s.lvl_2_id, s.lvl_3_id)", ""), "active people", params={"manager_id": 7}).has("required_predicate")


def test_pilot_template_initialises_passes_the_doctor_and_compiles(tmp_path):
    """The shipped pilot template is a working shadow-mode setup: init, doctor (clean), compile
    synthetic-only, score with the bundle; the doctor names unverified constructs in a query file."""
    from sqlgami import Scorer
    from sqlgami.doctor import diagnose, init_pilot

    written = init_pilot(tmp_path / "pilot")
    assert {p.name for p in written} >= {"schema.sql", "sqlgami.yaml", "Terms.csv", "Examples.csv"}
    (tmp_path / "pilot" / "q.sql").write_text("SELECT JSON_EXTRACT_SCALAR(segment, '$.a') FROM `my-project.hr.emp_period_snapshot`")
    diagnosis = diagnose(tmp_path / "pilot" / "sqlgami.yaml", tmp_path / "pilot" / "q.sql")
    assert diagnosis.ok, diagnosis.problems
    assert diagnosis.unverified == {"func:JSON_EXTRACT_SCALAR": ["q.sql"]}
    from sqlgami.bundle import compile_bundle

    bundle = compile_bundle(tmp_path / "pilot" / "sqlgami.yaml", tmp_path / "pilot" / "bundle")
    assert bundle.manifest["references"] == 2 and bundle.manifest["rejected"] == 0
    with Scorer("bigquery", catalog=(tmp_path / "pilot" / "schema.sql").read_text(), bundle=tmp_path / "pilot" / "bundle") as scorer:
        report = scorer.score("DECLARE mgr INT64 DEFAULT 12003;\nSELECT COUNT(DISTINCT s.assoc_id) AS active_n FROM `my-project.hr.emp_period_snapshot` s JOIN `my-project.hr.pay_period_dim` p ON p.ppe_dt = s.period_end_dt AND p.freq_cd = 'B' WHERE p.is_current_flag AND s.snap_dt = s.period_end_dt AND s.status_cd = 'A' AND mgr IN (s.lvl_2_id, s.lvl_3_id, s.lvl_4_id, s.lvl_2_id_fn, s.lvl_3_id_fn, s.lvl_4_id_fn)", "What is the current active headcount of my organisation?")
        assert report.signals["fixtures"]["verdict"] == "match" and report.verdict.value == "likely_right"
    # a broken workbook is a named problem, not a crash
    (tmp_path / "pilot" / "terms" / "Terms.csv").write_text("term,phrases,kind,table,sql,nulls,description\nactive,active,filtr,hr.emp_period_snapshot,status_cd = 'A',,\n")
    broken = diagnose(tmp_path / "pilot" / "sqlgami.yaml")
    assert not broken.ok and any("kind" in p for p in broken.problems)


TVF_DDL = """
CREATE TABLE `proj-x.shop.customers` (id INT64, name STRING, region STRING, PRIMARY KEY (id) NOT ENFORCED);
CREATE TABLE `proj-x.shop.orders` (id INT64, customer_id INT64, total NUMERIC, status STRING, created_at DATE, PRIMARY KEY (id) NOT ENFORCED, FOREIGN KEY (customer_id) REFERENCES `proj-x.shop.customers` (id) NOT ENFORCED);
CREATE VIEW `proj-x.shop.paid_orders` AS SELECT * FROM `proj-x.shop.orders` WHERE status = 'paid';
CREATE VIEW `proj-x.shop_reports.recent_paid` AS SELECT o.id, o.customer_id, o.total, o.created_at FROM `proj-x.shop.paid_orders` AS o WHERE o.created_at >= DATE '2024-01-01';
CREATE TABLE FUNCTION `proj-x.shop.region_orders`(which STRING, since DATE) AS
  SELECT o.id, o.customer_id, o.total, c.region FROM `proj-x.shop.orders` AS o JOIN `proj-x.shop.customers` AS c ON c.id = o.customer_id
  WHERE c.region = which AND o.created_at >= since;
CREATE TABLE FUNCTION `proj-x.shop.tagged`(tags ARRAY<STRING>) AS
  SELECT c.id, t FROM `proj-x.shop.customers` AS c, UNNEST(tags) AS t;
"""


@pytest.fixture(scope="module")
def tvf_bundle(tmp_path_factory):
    from sqlgami.bundle import compile_bundle

    tmp = tmp_path_factory.mktemp("tvf")
    (tmp / "schema.sql").write_text(TVF_DDL)
    (tmp / "terms").mkdir()
    (tmp / "terms" / "Terms.csv").write_text("term,phrases,kind,table,sql,nulls,description\npaid,paid,filter,shop.orders,status = 'paid',,\n")
    (tmp / "terms" / "Examples.csv").write_text(
        "id,question,params,correct_sql,expected_result,dataset\n"
        'by_region,Paid order total per customer in the north since 2024,,"SELECT customer_id, SUM(total) AS spend FROM `proj-x.shop.region_orders`(\'north\', DATE \'2024-01-01\') GROUP BY customer_id",,\n'
        'recent,Recent paid order count,,"SELECT COUNT(*) AS n FROM `proj-x.shop_reports.recent_paid`",,\n'
    )
    (tmp / "sqlgami.yaml").write_text(
        "dialect: bigquery\nschema: schema.sql\nterms: terms\n"
        "fixtures: {seed: 4, profiles: [clean, nulls], rows: {shop.customers: 8, shop.orders: 60}, values: {shop.orders.status: [paid, open], shop.customers.region: [north, south]}, dates: {start: '2023-06-01', end: '2024-12-31'}}\n"
    )
    compile_bundle(tmp / "sqlgami.yaml", tmp / "bundle")
    return tmp / "bundle"


def test_table_functions_and_nested_views_are_read_expanded_gated_and_run_on_fixtures(tvf_bundle):
    """A CREATE TABLE FUNCTION is a view with parameters: declared from DDL, expanded with the
    caller's arguments so every check applies inside, refused when unknown or mis-called, and
    materialised in the fixtures as a table macro through the verified translation. Views over
    views and a cross-dataset view resolve through the logical name."""
    import json

    from sqlgami import Catalog, Scorer
    from sqlgami.report import StageStatus

    catalog = Catalog.from_ddl(TVF_DDL, "bigquery")
    fn = catalog.resolve("region_orders", "proj-x.shop")
    assert fn is not None and fn.is_table_function and fn.parameters == (("which", "STRING"), ("since", "DATE"))
    manifest = json.loads((tvf_bundle / "manifest.json").read_text())
    assert manifest["references"] == 2 and manifest["rejected"] == 0, manifest
    from sqlgami.fixtures import Fixture

    ddl = Fixture.read(tvf_bundle / "fixtures" / "clean-s4").ddl
    assert "CREATE MACRO shop.region_orders(which, since) AS TABLE" in ddl and "CREATE VIEW shop_reports.recent_paid" in ddl, ddl
    with Scorer("bigquery", catalog=TVF_DDL, bundle=tvf_bundle, required_predicates=[{"name": "paid_only", "tables": ["shop.orders"], "match": "all", "columns": ["status"]}]) as scorer:
        same = scorer.explain("SELECT customer_id, SUM(total) AS spend FROM `proj-x.shop.region_orders`('north', DATE '2024-01-01') GROUP BY customer_id", "Paid order total per customer in the north since 2024")
        assert same.signals["views_expanded"] == ["proj-x.shop.region_orders"]
        assert same.signals["fixtures"]["verdict"] == "match", same.summary()
        # another argument changes the result: a verdict through the macro, with the arguments
        other = scorer.explain("SELECT customer_id, SUM(total) AS spend FROM `proj-x.shop.region_orders`('south', DATE '2024-01-01') GROUP BY customer_id", "Paid order total per customer in the north since 2024")
        assert other.signals["fixtures"]["verdict"] == "mismatch"
        # the policy applies inside the body: the function filters region, not status, so the status policy is missing
        assert same.has("required_predicate")
        unknown = scorer.explain("SELECT COUNT(*) FROM `proj-x.shop.other_fn`('north') AS o", "orders in the north")
        assert unknown.has("unknown_table") and unknown.verdict.value == "invalid"
        arity = scorer.explain("SELECT COUNT(*) FROM `proj-x.shop.region_orders`('north') AS o", "orders in the north")
        assert arity.has("unknown_table") and "takes 2 argument(s)" in next(f.message for f in arity.findings if f.code == "unknown_table")
        nested = scorer.explain("SELECT COUNT(*) AS n FROM `proj-x.shop_reports.recent_paid`", "Recent paid order count")
        assert set(nested.signals["views_expanded"]) == {"proj-x.shop_reports.recent_paid", "proj-x.shop.paid_orders"} and nested.signals["fixtures"]["verdict"] == "match"
        arrays = scorer.explain("SELECT id, t FROM `proj-x.shop.tagged`(['vip', 'new']) ORDER BY id, t", "customers with tags")
        assert arrays.signals["views_expanded"] == ["proj-x.shop.tagged"] and arrays.stage("fixtures").status in (StageStatus.NOT_RUN, StageStatus.SKIPPED, StageStatus.PASSED)


INFO_SCHEMA_DUMP = """
CREATE TABLE `proj-x.shop.customers`
(
  id INT64 NOT NULL,
  name STRING OPTIONS(description="display name"),
  region STRING
)
OPTIONS(
  description="customers",
  labels=[("team", "sales")]
);
CREATE TABLE `proj-x.shop.orders`
(
  id INT64 NOT NULL,
  customer_id INT64,
  total NUMERIC,
  status STRING,
  created_at DATE
)
PARTITION BY created_at
CLUSTER BY customer_id;
CREATE VIEW `proj-x.shop.paid_orders`
OPTIONS(
  description="paid only"
)
AS SELECT * FROM `proj-x.shop.orders` WHERE status = 'paid';
CREATE TABLE FUNCTION `proj-x.shop.region_orders`(which STRING) AS (
  SELECT o.id, o.customer_id, o.total FROM `proj-x.shop.paid_orders` AS o JOIN `proj-x.shop.customers` AS c ON c.id = o.customer_id WHERE c.region = which
);
CREATE TABLE FUNCTION `proj-x.shop_reports.big_region_orders`(which STRING, floor NUMERIC) AS (
  SELECT * FROM `proj-x.shop.region_orders`(which) WHERE total >= floor
);
CREATE VIEW `proj-x.shop_reports.north_big` AS SELECT customer_id, total FROM `proj-x.shop_reports.big_region_orders`('north', 50);
CREATE FUNCTION `proj-x.shop.pct`(a NUMERIC, b NUMERIC) RETURNS FLOAT64 AS (
  SAFE_DIVIDE(a, b) * 100
);
CREATE FUNCTION `proj-x.shop.shout`(x STRING) RETURNS STRING LANGUAGE js AS r\"\"\"
  return x.toUpperCase();
\"\"\";
"""


def test_information_schema_dump_nesting_and_scalar_functions(tmp_path):
    """BigQuery's own DDL (OPTIONS, PARTITION/CLUSTER, parenthesised bodies, LANGUAGE js) is read
    as a host would dump it; a view over a TVF over a TVF over a view resolves to the base tables;
    a scalar SQL function is inlined as a macro, a JavaScript one keeps the fixtures from running,
    an undeclared qualified function is an ERROR; nesting beyond the limit is a clear finding."""
    from sqlgami import Catalog, Scorer
    from sqlgami.bundle import compile_bundle
    from sqlgami.report import StageStatus

    catalog = Catalog.from_ddl(INFO_SCHEMA_DUMP, "bigquery")
    assert catalog.resolve("big_region_orders", "proj-x.shop_reports").is_table_function
    assert catalog.scalar_function("`proj-x.shop.pct`").body == "SAFE_DIVIDE(a, b) * 100" and catalog.scalar_function("proj-x.shop.shout").language == "js"
    (tmp_path / "schema.sql").write_text(INFO_SCHEMA_DUMP)
    (tmp_path / "sqlgami.yaml").write_text("dialect: bigquery\nschema: schema.sql\nfixtures: {seed: 4, profiles: [clean], rows: {shop.customers: 8, shop.orders: 60}, values: {shop.orders.status: [paid, open], shop.customers.region: [north, south]}}\nreferences:\n  - {key: nb, question: north big orders per customer, sql: \"SELECT customer_id, SUM(total) AS t FROM `proj-x.shop_reports.north_big` GROUP BY customer_id\"}\n")
    bundle = compile_bundle(tmp_path / "sqlgami.yaml", tmp_path / "bundle")
    assert bundle.manifest["references"] == 1 and bundle.manifest["rejected"] == 0 and "proj-x.shop.pct" in bundle.manifest["functions"]
    with Scorer("bigquery", catalog=INFO_SCHEMA_DUMP, bundle=tmp_path / "bundle") as scorer:
        nested = scorer.explain("SELECT customer_id, SUM(total) AS t FROM `proj-x.shop_reports.north_big` GROUP BY customer_id", "north big orders per customer")
        assert set(nested.signals["views_expanded"]) == {"proj-x.shop_reports.north_big", "proj-x.shop_reports.big_region_orders", "proj-x.shop.region_orders", "proj-x.shop.paid_orders"}
        assert nested.signals["fixtures"]["verdict"] == "match", nested.summary()
        inlined = scorer.explain("SELECT customer_id, `proj-x.shop.pct`(SUM(total), 1000) AS share FROM `proj-x.shop_reports.north_big` GROUP BY customer_id", "share of north big orders per customer")
        assert inlined.signals["functions"]["inlined"] == ["proj-x.shop.pct"] and not inlined.has("unknown_function")
        assert inlined.stage("fixtures").status in (StageStatus.PASSED, StageStatus.NOT_RUN) and "untranslatable" not in (inlined.stage("fixtures").detail or "")
        js = scorer.explain("SELECT `proj-x.shop.shout`(name) AS n FROM `proj-x.shop.customers`", "customer names in capitals")
        assert js.signals["functions"]["javascript"] == ["proj-x.shop.shout"]
        assert js.stage("fixtures").status == StageStatus.NOT_RUN and "javascript function proj-x.shop.shout" in (js.stage("fixtures").detail or "")
        unknown = scorer.explain("SELECT `proj-x.shop.mystery`(name) AS n FROM `proj-x.shop.customers`", "names")
        assert unknown.has("unknown_function") and unknown.verdict.value == "invalid"
    deep = INFO_SCHEMA_DUMP + "".join(f"CREATE VIEW `proj-x.shop.v{i}` AS SELECT * FROM `proj-x.shop.v{i-1 if i > 0 else 'x'}`;\n".replace("vx", "paid_orders") for i in range(0, 36))
    report = Scorer("bigquery", catalog=deep).explain("SELECT COUNT(*) FROM `proj-x.shop.v35`", "count")
    finding = next(f for f in report.findings if f.code == "view_nesting")
    assert finding.severity.value == "high" and "nested more than" in finding.message and report.score <= 0.4
