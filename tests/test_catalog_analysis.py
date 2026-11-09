"""Catalog loading, name resolution, view expansion and the hard gates."""

from __future__ import annotations

import json

import pytest

from sqlgami import Catalog, CatalogError, Column, Scorer, Table
from sqlgami.analysis import MUTATION, QUERY, analyse

from conftest import SHOP_DDL


def test_from_ddl_reads_keys_views_and_nullability():
    catalog = Catalog.from_ddl(
        SHOP_DDL
        + """
        CREATE TABLE line (a INT NOT NULL, b INT NOT NULL, c TEXT, PRIMARY KEY (a, b),
                           CONSTRAINT u UNIQUE (c), FOREIGN KEY (a) REFERENCES orders (id));
        CREATE UNIQUE INDEX line_c_b ON line (c, b);
        """,
        "postgres",
    )
    customers = catalog.resolve("customers")
    assert customers.primary_key == ("id",)
    assert catalog.column(customers, "name").nullable is False
    assert catalog.column(customers, "country").nullable is True
    line = catalog.resolve("line")
    assert line.primary_key == ("a", "b")
    assert ("c",) in line.unique_keys and ("c", "b") in line.unique_keys
    assert line.foreign_keys[0].ref_table == "orders"
    view = catalog.resolve("big_customers")
    assert view.is_view and view.view_columns == ("cid", "spend")
    assert "order_totals" in view.definition


def test_catalog_round_trips_through_a_dictionary():
    catalog = Catalog.from_ddl(SHOP_DDL, "duckdb", tenant_column="tenant_id")
    for name in catalog.table_names():
        catalog.resolve(name.split(".")[-1])
    data = json.loads(json.dumps(catalog.to_dict()))
    again = Catalog.from_dict(data)
    assert again.tenant_column == "tenant_id"
    assert again.table_names() == catalog.table_names()
    assert again.resolve("orders").foreign_keys[0].ref_table == "customers"
    sql = "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name"
    assert Scorer("duckdb", catalog=data).explain(sql).stage("schema").status.value == "passed"


def test_malformed_catalog_data_raises_catalog_error():
    with pytest.raises(CatalogError):
        Catalog.from_dict({"dialect": "duckdb", "tables": [{"columns": []}]})
    with pytest.raises(CatalogError):
        Catalog.from_dict({"tables": []})


class CountingLoader:
    """A catalog of many tables that records which ones are actually fetched."""

    def __init__(self, count: int) -> None:
        self.names = [f"t{i:03d}" for i in range(count)]
        self.loaded: list[str] = []
        self.listings = 0

    def list_tables(self):
        self.listings += 1
        return [("main", name) for name in self.names]

    def load_table(self, schema, name):
        self.loaded.append(name)
        return Table(
            name=name,
            schema=schema,
            columns=[Column("id", "INTEGER", False), Column("parent_id", "INTEGER"), Column("v", "TEXT")],
            primary_key=("id",),
        )


def test_large_catalog_loads_only_the_tables_a_query_touches():
    loader = CountingLoader(500)
    catalog = Catalog("duckdb", loader=loader)
    scorer = Scorer("duckdb", catalog=catalog)
    sql = "SELECT a.v, b.v FROM t007 a JOIN t123 b ON b.id = a.parent_id"
    for _ in range(3):
        report = scorer.explain(sql)
        assert report.stage("schema").status.value == "passed"
    assert sorted(loader.loaded) == ["t007", "t123"]
    assert loader.listings == 1
    assert len(catalog) == 500 and catalog.loaded_count() == 2
    missing = scorer.explain("SELECT * FROM t999x")
    assert missing.has("unknown_table")
    assert loader.loaded.count("t007") == 1


def test_postgres_identifier_case_rules():
    ddl = 'CREATE TABLE billing."PlanFeatures" (id INT PRIMARY KEY, "IsOn" BOOLEAN, plan_id INT);'
    scorer = Scorer("postgres", catalog=Catalog.from_ddl(ddl, "postgres", search_path=["billing"]))
    ok = scorer.explain('SELECT "IsOn", PLAN_ID FROM "PlanFeatures"')
    assert ok.stage("schema").status.value == "passed", ok.summary()
    unquoted = scorer.explain("SELECT plan_id FROM PlanFeatures")
    assert unquoted.has("unknown_table")
    # The hint shows the name the way it has to be written.
    assert 'billing."PlanFeatures"' in next(f.message for f in unquoted.findings if f.code == "unknown_table")
    assert scorer.explain('SELECT IsOn FROM "PlanFeatures"').has("unknown_column")
    assert scorer.explain('SELECT plan_id FROM billing."PlanFeatures"').stage("schema").status.value == "passed"


def test_case_insensitive_engines_ignore_case():
    ddl = 'CREATE TABLE "PlanFeatures" (id INT PRIMARY KEY, "IsOn" BOOLEAN);'
    for dialect in ("sqlite", "duckdb"):
        scorer = Scorer(dialect, catalog=Catalog.from_ddl(ddl, dialect))
        report = scorer.explain("SELECT ISON FROM planfeatures WHERE Id = 1")
        assert report.stage("schema").status.value == "passed", report.summary()


def test_multiple_schemas_and_search_path():
    ddl = """
    CREATE TABLE billing.invoices (id INT PRIMARY KEY, tenant_id INT);
    CREATE TABLE audit.invoices (id INT PRIMARY KEY, note TEXT);
    CREATE VIEW billing.inv_v AS SELECT id, tenant_id FROM invoices;
    """
    catalog = Catalog.from_ddl(ddl, "postgres", search_path=["audit", "billing"])
    scorer = Scorer("postgres", catalog=catalog)
    assert scorer.explain("SELECT note FROM invoices").stage("schema").status.value == "passed"
    assert scorer.explain("SELECT tenant_id FROM invoices").has("unknown_column")
    assert scorer.explain("SELECT tenant_id FROM billing.invoices").stage("schema").status.value == "passed"
    # A view resolves unqualified names from its own schema first.
    report = scorer.explain("SELECT tenant_id FROM billing.inv_v")
    assert report.stage("schema").status.value == "passed", report.summary()
    assert report.tables == ["billing.invoices"]


def test_views_are_expanded_to_base_tables():
    catalog = Catalog.from_ddl(SHOP_DDL, "duckdb")
    analysis = analyse("SELECT cid, spend FROM big_customers WHERE spend > 10", catalog)
    assert analysis.schema_ok
    assert analysis.views_used == ["main.order_totals", "main.big_customers"] or set(analysis.views_used) == {
        "main.order_totals",
        "main.big_customers",
    }
    assert {t.name for t in analysis.tables.values()} == {"orders", "items"}
    report = Scorer("duckdb", catalog=catalog).explain("SELECT spendx FROM big_customers")
    assert report.has("unknown_column")


def test_view_cycle_and_broken_view_do_not_crash():
    ddl = """
    CREATE TABLE t (id INT PRIMARY KEY);
    CREATE VIEW a AS SELECT * FROM b;
    CREATE VIEW b AS SELECT * FROM a;
    CREATE VIEW broken AS SELECT * FROM missing_table;
    """
    scorer = Scorer("duckdb", catalog=Catalog.from_ddl(ddl, "duckdb"))
    cyclic = scorer.explain("SELECT * FROM a")
    assert cyclic.has("analysis_incomplete")
    broken = scorer.explain("SELECT * FROM broken")
    finding = next(f for f in broken.findings if f.code == "unknown_table")
    assert "inside view" in finding.message


@pytest.mark.parametrize(
    "sql,kind",
    [
        ("SELECT 1", QUERY),
        ("(SELECT 1)", QUERY),
        ("WITH x AS (SELECT 1 AS a) SELECT a FROM x", QUERY),
        ("SELECT 1 UNION SELECT 2", QUERY),
        ("DELETE FROM orders", MUTATION),
        ("UPDATE orders SET total = 0", MUTATION),
        ("INSERT INTO orders SELECT * FROM orders", MUTATION),
        ("DROP TABLE orders", MUTATION),
        ("CREATE TABLE x AS SELECT 1", MUTATION),
        ("WITH d AS (DELETE FROM orders RETURNING id) SELECT * FROM d", MUTATION),
        ("SELECT * INTO backup FROM orders", MUTATION),
        ("TRUNCATE TABLE orders", MUTATION),
    ],
)
def test_statement_classification(sql, kind):
    assert analyse(sql, None, "postgres").kind == kind


@pytest.mark.parametrize("dialect", ["sqlite", "duckdb", "postgres"])
def test_hard_gates(dialect):
    scorer = Scorer(dialect, catalog=Catalog.from_ddl(SHOP_DDL, dialect))

    parse = scorer.explain("SELEC * FROM orders")
    assert parse.verdict.value == "invalid" and parse.score == 0.0 and parse.has("parse_error")

    for sql, code in [
        ("DELETE FROM orders WHERE id = 1", "not_read_only"),
        ("SELECT 1; SELECT 2", "multiple_statements"),
        ("SELECT * FROM ordrs", "unknown_table"),
        ("SELECT totl FROM orders", "unknown_column"),
        ("SELECT o.totl FROM orders o", "unknown_column"),
        ("SELECT id FROM orders o JOIN customers c ON c.id = o.customer_id", "ambiguous_column"),
        ("SELECT x.id FROM orders o", "unknown_column"),
    ]:
        report = scorer.explain(sql)
        assert report.has(code), report.summary()
        assert report.verdict.value == "invalid"
        assert report.score <= 0.1

    assert scorer.explain("").has("empty_statement")
    assert scorer.explain("   -- nothing\n").has("empty_statement")

    suggestion = next(f for f in scorer.explain("SELECT * FROM ordrs").findings if f.code == "unknown_table")
    assert "orders" in suggestion.message


def test_high_finding_caps_at_point_four():
    scorer = Scorer("duckdb", catalog=Catalog.from_ddl(SHOP_DDL, "duckdb"))
    report = scorer.explain("SELECT * FROM customers WHERE deleted_at = NULL")
    assert report.confidence.cap == 0.4
    assert report.score <= 0.4 and report.confidence.high <= 0.4
    assert report.verdict.value == "likely_wrong"


def test_sqlite_double_quoted_string_is_not_an_unknown_column():
    scorer = Scorer("sqlite", catalog=Catalog.from_ddl(SHOP_DDL, "sqlite"))
    report = scorer.explain('SELECT name FROM customers WHERE country = "US"')
    assert report.stage("schema").status.value == "passed"
    assert report.has("double_quoted_string")
    assert not report.has("unknown_column")
    quoted_column = scorer.explain('SELECT "name" FROM customers WHERE "country" = \'US\'')
    assert not quoted_column.has("double_quoted_string")


def test_no_schema_means_parse_only():
    report = Scorer("duckdb").explain("SELECT a FROM whatever")
    assert report.stage("schema").status.value == "skipped"
    assert report.stage("static").status.value == "not_run"
    assert report.verdict.value == "uncertain"
    assert Scorer("duckdb").explain("DROP TABLE x").verdict.value == "invalid"


def test_complex_constructs_resolve():
    scorer = Scorer("postgres", catalog=Catalog.from_ddl(SHOP_DDL, "postgres"))
    queries = [
        "SELECT c.name, l.total FROM customers c CROSS JOIN LATERAL "
        "(SELECT o.total FROM orders o WHERE o.customer_id = c.id ORDER BY o.created_at DESC LIMIT 1) l",
        "SELECT id, total, RANK() OVER (PARTITION BY customer_id ORDER BY total DESC) FROM orders",
        "SELECT status, COUNT(*) FILTER (WHERE total > 50) FROM orders GROUP BY status",
        "SELECT customer_id FROM orders EXCEPT SELECT id FROM customers",
        "SELECT o.id FROM orders o WHERE o.total > ALL (SELECT i.price FROM items i WHERE i.order_id = o.id)",
        "SELECT DISTINCT ON (customer_id) customer_id, total FROM orders ORDER BY customer_id, created_at DESC",
        "SELECT date_trunc('month', created_at) AS m, SUM(total) FROM orders GROUP BY 1 ORDER BY 1",
        "SELECT * FROM orders o WHERE (o.customer_id, o.status) IN (SELECT id, 'paid' FROM customers)",
        "SELECT c.name, COALESCE(t.amount, 0) FROM customers c LEFT JOIN order_totals t ON t.customer_id = c.id",
        "WITH RECURSIVE chain AS (SELECT id, referrer_id FROM customers WHERE id = 3 UNION ALL "
        "SELECT c.id, c.referrer_id FROM customers c JOIN chain ON chain.referrer_id = c.id) SELECT * FROM chain",
    ]
    for sql in queries:
        report = scorer.explain(sql)
        assert report.stage("schema").status.value == "passed", sql + "\n" + report.summary()
        assert not [c for c in report.checks_not_run if "internal error" in c.reason], report.summary()
