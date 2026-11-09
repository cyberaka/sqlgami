"""Postgres-only behaviour. Skipped unless SQLGAMI_TEST_PG_DSN names a test server."""

from __future__ import annotations

import pytest

from sqlgami import ConnectionFailed, Scorer, Severity, UnsafeQueryError
from sqlgami.engines import get_adapter

from conftest import PG_DSN

pytestmark = pytest.mark.skipif(not PG_DSN, reason="SQLGAMI_TEST_PG_DSN is not set")

BILLING = "sg_billing"
AUDIT = "sg_audit"

DDL = f"""
CREATE SCHEMA {BILLING};
CREATE SCHEMA {AUDIT};
CREATE TABLE {BILLING}.tenants (tenant_id INT PRIMARY KEY, name TEXT NOT NULL, deleted_at TIMESTAMPTZ);
CREATE TABLE {BILLING}."PlanFeatures" (plan_id INT NOT NULL, feature_id INT NOT NULL, quota INT,
    PRIMARY KEY (plan_id, feature_id));
CREATE TABLE {BILLING}.invoices (
    invoice_id INT PRIMARY KEY,
    tenant_id INT NOT NULL REFERENCES {BILLING}.tenants (tenant_id),
    invoice_number TEXT NOT NULL,
    total_cents BIGINT NOT NULL,
    status TEXT NOT NULL,
    "IsAutoBilled" BOOLEAN NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    meta JSONB,
    UNIQUE (tenant_id, invoice_number)
);
CREATE TABLE {BILLING}.usage_events (
    event_id BIGINT NOT NULL,
    tenant_id INT NOT NULL,
    event_at TIMESTAMPTZ NOT NULL,
    quantity INT NOT NULL,
    PRIMARY KEY (event_id, event_at)
) PARTITION BY RANGE (event_at);
CREATE TABLE {BILLING}.usage_events_2026_01 PARTITION OF {BILLING}.usage_events
    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE {BILLING}.usage_events_2026_02 PARTITION OF {BILLING}.usage_events
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
CREATE TABLE {AUDIT}.audit_log (audit_id INT PRIMARY KEY, tenant_id INT, "NewState" JSONB, note TEXT);
CREATE VIEW {BILLING}.paid_invoices_v AS
    SELECT invoice_id, tenant_id, total_cents FROM {BILLING}.invoices WHERE status = 'paid';
CREATE MATERIALIZED VIEW {BILLING}.monthly_revenue_mv AS
    SELECT tenant_id, date_trunc('month', issued_at) AS month, SUM(total_cents) AS revenue_cents
    FROM {BILLING}.invoices WHERE status = 'paid' GROUP BY 1, 2;
INSERT INTO {BILLING}.tenants VALUES (1, 'Acme', NULL), (2, 'Globex', NULL);
INSERT INTO {BILLING}."PlanFeatures" VALUES (1, 1, 10), (1, 2, NULL);
INSERT INTO {BILLING}.invoices VALUES
    (1, 1, 'INV-1', 10000, 'paid', TRUE, '2026-01-10 10:00+00', '{{"channel": "web"}}'),
    (2, 1, 'INV-2', 5000, 'open', FALSE, '2026-02-10 10:00+00', NULL),
    (3, 2, 'INV-1', 7000, 'paid', TRUE, '2026-01-20 10:00+00', '{{"channel": "api"}}');
INSERT INTO {BILLING}.usage_events VALUES
    (1, 1, '2026-01-05 00:00+00', 3), (2, 1, '2026-02-05 00:00+00', 4), (3, 2, '2026-02-06 00:00+00', 5);
INSERT INTO {AUDIT}.audit_log VALUES (1, 1, '{{"status": "void"}}', 'x'), (2, NULL, NULL, NULL);
CREATE TABLE {AUDIT}.side_effects (n INT);
CREATE FUNCTION {AUDIT}.sneaky() RETURNS INT LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO {AUDIT}.side_effects VALUES (1);
    RETURN 1;
END $$;
"""


@pytest.fixture(scope="module")
def pg():
    psycopg = pytest.importorskip("psycopg")
    conn = psycopg.connect(PG_DSN, autocommit=True)
    conn.execute(f"DROP SCHEMA IF EXISTS {BILLING} CASCADE")
    conn.execute(f"DROP SCHEMA IF EXISTS {AUDIT} CASCADE")
    conn.execute(DDL)
    conn.execute(f"REFRESH MATERIALIZED VIEW {BILLING}.monthly_revenue_mv")
    conn.execute("ANALYZE")
    conn.execute(f"SET search_path = {BILLING}, {AUDIT}")
    yield conn
    conn.rollback()
    conn.execute(f"DROP SCHEMA IF EXISTS {BILLING} CASCADE")
    conn.execute(f"DROP SCHEMA IF EXISTS {AUDIT} CASCADE")
    conn.close()


@pytest.fixture
def scorer(pg):
    with Scorer("postgres", db=pg) as s:
        yield s


def stages(report) -> dict[str, str]:
    return {s.name: s.status.value for s in report.stages}


def test_search_path_and_schemas(pg, scorer):
    assert scorer.catalog.search_path == [BILLING, AUDIT]
    names = scorer.catalog.table_names()
    assert f"{BILLING}.invoices" in names and f"{AUDIT}.audit_log" in names
    assert not any(name.startswith("pg_catalog.") or name.startswith("information_schema.") for name in names)
    unqualified = scorer.sample("SELECT note FROM audit_log ORDER BY audit_id")
    assert stages(unqualified)["sample"] == "passed" and unqualified.tables == [f"{AUDIT}.audit_log"]


def test_introspection(pg):
    catalog = get_adapter("postgres", pg).catalog()
    invoices = catalog.resolve("invoices")
    assert invoices.primary_key == ("invoice_id",)
    assert ("tenant_id", "invoice_number") in invoices.unique_keys
    assert invoices.foreign_keys[0].ref_table == "tenants" and invoices.foreign_keys[0].ref_schema == BILLING
    assert catalog.column(invoices, "meta").type == "jsonb"
    assert catalog.column(invoices, "meta").nullable is True
    assert catalog.column(invoices, "IsAutoBilled", quoted=True).nullable is False
    assert invoices.row_count == 3
    features = catalog.resolve("PlanFeatures", quoted=True)
    assert features.primary_key == ("plan_id", "feature_id")
    assert catalog.resolve("PlanFeatures") is None  # unquoted folds to lower case
    events = catalog.resolve("usage_events")
    assert events.primary_key == ("event_id", "event_at") and events.row_count == 3
    view = catalog.resolve("paid_invoices_v")
    assert view.kind == "view" and "status" in view.definition
    mv = catalog.resolve("monthly_revenue_mv")
    assert mv.kind == "materialized_view" and "date_trunc" in mv.definition


def test_quoted_identifiers(scorer):
    ok = scorer.sample('SELECT invoice_id FROM invoices WHERE "IsAutoBilled" ORDER BY invoice_id')
    assert stages(ok)["sample"] == "passed" and ok.signals["rows_returned"] == 2
    folded = scorer.score("SELECT invoice_id FROM invoices WHERE IsAutoBilled")
    assert folded.has("unknown_column") and folded.has("dry_run_error") and folded.score <= 0.1
    table = scorer.score("SELECT plan_id FROM PlanFeatures")
    assert table.has("unknown_table") and table.score <= 0.1
    assert stages(scorer.sample('SELECT quota FROM "PlanFeatures" ORDER BY 1'))["sample"] == "passed"


def test_checks_see_through_views_and_materialized_views(pg):
    with Scorer("postgres", db=pg, tenant_column="tenant_id") as scoped:
        missing = scoped.score("SELECT SUM(revenue_cents) FROM monthly_revenue_mv")
        assert missing.has("missing_tenant_filter")
        assert missing.tables == [f"{BILLING}.invoices"]
        assert f"{BILLING}.monthly_revenue_mv" in missing.signals["views_expanded"]
        assert not scoped.score("SELECT SUM(revenue_cents) FROM monthly_revenue_mv WHERE tenant_id = 1").has(
            "missing_tenant_filter"
        )
        assert not scoped.score("SELECT SUM(total_cents) FROM paid_invoices_v WHERE tenant_id = 1").has(
            "missing_tenant_filter"
        )


def test_composite_unique_key_prevents_a_false_fan_out(scorer):
    # (tenant_id, invoice_number) is unique, so this join cannot repeat usage rows.
    fine = scorer.sample(
        "SELECT SUM(u.quantity) FROM usage_events u JOIN invoices i "
        "ON i.tenant_id = u.tenant_id AND i.invoice_number = 'INV-1'"
    )
    assert not fine.has("fan_out"), fine.summary()
    # invoice_number alone is not unique: INV-1 exists for two tenants.
    wrong = scorer.sample(
        "SELECT SUM(t.tenant_id) FROM tenants t JOIN invoices i ON i.invoice_number = 'INV-1'"
    )
    assert wrong.has("fan_out") or wrong.has("cartesian_join"), wrong.summary()


def test_partitioned_table_and_jsonb(scorer):
    report = scorer.sample(
        "SELECT date_trunc('month', event_at) AS m, SUM(quantity) FROM usage_events "
        "WHERE event_at >= '2026-01-01' AND event_at < '2026-03-01' GROUP BY 1 ORDER BY 1"
    )
    assert stages(report)["sample"] == "passed" and report.signals["rows_returned"] == 2
    assert not report.findings_at_least(Severity.MEDIUM), report.summary()
    assert report.cost.value == 3
    json = scorer.sample("SELECT audit_id FROM audit_log WHERE \"NewState\" ->> 'status' = 'void'")
    assert stages(json)["sample"] == "passed" and json.signals["rows_returned"] == 1
    assert not json.findings_at_least(Severity.MEDIUM), json.summary()


def test_plan_is_the_engines_json(scorer):
    report = scorer.dry_run("SELECT * FROM invoices WHERE tenant_id = 1")
    assert isinstance(report.plan, list) and "Plan" in report.plan[0]
    assert stages(report)["dry_run"] == "passed"


def test_a_function_that_writes_cannot_write(pg, scorer):
    report = scorer.sample(f"SELECT {AUDIT}.sneaky()")
    assert report.has("execution_error"), report.summary()
    assert "read-only" in next(f.message for f in report.findings if f.code == "execution_error")
    assert pg.execute(f"SELECT COUNT(*) FROM {AUDIT}.side_effects").fetchone()[0] == 0


def test_statement_timeout(pg):
    with Scorer("postgres", db=pg, timeout_s=0.3) as scorer:
        report = scorer.sample("SELECT COUNT(*) FROM generate_series(1, 200000000) AS g(i) WHERE i % 7 = 3")
    assert report.has("execution_timeout"), report.summary()
    assert report.confidence.cap == 0.5
    assert pg.execute("SELECT 1").fetchone() == (1,)


def test_callers_open_transaction_is_left_alone():
    import psycopg

    conn = psycopg.connect(PG_DSN)  # not autocommit: statements open a transaction
    try:
        conn.execute(f"SET search_path = {BILLING}, {AUDIT}")
        conn.execute("INSERT INTO tenants VALUES (99, 'Uncommitted', NULL)")
        with Scorer("postgres", db=conn) as scorer:
            bad = scorer.sample("SELECT nope FROM tenants")
            assert bad.has("unknown_column") and bad.verdict.value == "invalid"
            runtime = scorer.sample("SELECT 1 / (tenant_id - tenant_id) FROM tenants")
            assert runtime.has("execution_error")
            good = scorer.sample("SELECT COUNT(*) FROM tenants")
            assert stages(good)["sample"] == "passed"
        # The caller's transaction survived sqlgami's errors and still sees its own row.
        assert conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 3
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
    finally:
        conn.rollback()
        conn.close()


def test_connect_by_string_and_bad_string():
    with Scorer("postgresql", db=PG_DSN) as scorer:
        assert scorer.engine == "postgres"
        report = scorer.sample(f"SELECT name FROM {BILLING}.tenants ORDER BY tenant_id")
        assert report.signals["rows_returned"] == 2
        with pytest.raises(UnsafeQueryError):
            scorer.adapter.execute(f"DELETE FROM {BILLING}.tenants", max_rows=1, timeout_s=1)
    with pytest.raises(ConnectionFailed):
        Scorer("postgres", db="postgresql://nobody:wrong@127.0.0.1:1/none?connect_timeout=1")
    with pytest.raises(ConnectionFailed):
        Scorer("postgres", db=object())


def test_question_checks_and_value_profile(scorer):
    wrong = scorer.sample("SELECT SUM(total_cents) FROM invoices", "What is the total of paid invoices?")
    assert wrong.has("question_value_unused"), wrong.summary()
    right = scorer.sample("SELECT SUM(total_cents) FROM invoices WHERE status = 'paid'", "What is the total of paid invoices?")
    assert not [f for f in right.findings if f.stage == "question"], right.summary()
    grain = scorer.sample(
        "SELECT COUNT(*) FROM tenants t JOIN invoices i ON i.tenant_id = t.tenant_id",
        "How many tenants have invoices?",
    )
    assert grain.has("count_grain"), grain.summary()
