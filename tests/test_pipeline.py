"""The pipeline against live SQLite and DuckDB databases: modes, probes, safety, report shape."""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from sqlgami import (
    ADVISORY,
    SCHEMA_VERSION,
    ConfigurationError,
    ConnectionFailed,
    Scorer,
    Severity,
    SqlgamiError,
    UnsafeQueryError,
)
from sqlgami.engines import get_adapter
from sqlgami.engines.base import assert_read_only

from conftest import make_db, schema_of

GOOD = (
    "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id "
    "GROUP BY c.name ORDER BY spend DESC"
)


def stages(report) -> dict[str, str]:
    return {s.name: s.status.value for s in report.stages}


# ------------------------------------------------------------------- modes


def test_default_mode_is_dry_with_a_connection(scorer):
    report = scorer.score(GOOD)
    assert report.mode == "dry"
    assert stages(report) == {
        "parse": "passed",
        "statement": "passed",
        "schema": "passed",
        "static": "passed",
        "dry_run": "passed",
        "sample": "not_run",
        "consistency": "not_run",
        "question": "not_run",
    }
    assert report.verdict.value == "uncertain"
    assert report.confidence.calibrated is False
    assert report.plan


def test_engine_version_is_reported(scorer):
    version = scorer.score(GOOD).signals["engine_version"]
    assert version and version[0].isdigit()
    assert "engine_version" not in scorer.explain(GOOD).signals


def test_static_mode_never_plans_or_runs(scorer):
    report = scorer.explain(GOOD)
    assert stages(report)["dry_run"] == "not_run" and stages(report)["sample"] == "not_run"
    assert report.plan is None and report.cost is None


def test_sample_mode_runs_the_query(scorer):
    report = scorer.sample(GOOD)
    assert stages(report)["sample"] == "passed"
    assert report.signals["rows_returned"] == 3
    assert report.verdict.value == "likely_right"
    assert report.cost.unit == "rows" and report.cost.value > 0
    assert report.score > scorer.score(GOOD).score > scorer.explain(GOOD).score


def test_modes_need_a_connection(engine):
    with pytest.raises(ConfigurationError):
        Scorer(engine, mode="dry")
    static_only = Scorer(engine, catalog="CREATE TABLE t (id INT PRIMARY KEY)")
    assert static_only.mode == "static"
    with pytest.raises(ConfigurationError):
        static_only.sample("SELECT id FROM t")
    with pytest.raises(ConfigurationError):
        static_only.score("SELECT id FROM t", mode="nonsense")
    with pytest.raises(ConfigurationError):
        Scorer("oracle9")


def test_cost_ceiling_blocks_execution(scorer):
    report = scorer.sample(GOOD, cost_ceiling=1)
    sample = report.stage("sample")
    assert sample.status.value == "skipped"
    assert "unverified beyond static and dry-run checks" in sample.detail
    assert "unverified beyond static and dry-run checks" in report.confidence.basis
    assert "rows_returned" not in report.signals


# ---------------------------------------------------------- database gates


def test_database_knows_what_static_analysis_cannot_see(scorer, engine):
    # The function is looked up in the database's catalogue; the statement itself is not sent.
    report = scorer.score("SELECT no_such_function(name) FROM customers")
    assert report.has("unknown_function")
    assert report.verdict.value == "invalid" and report.score <= 0.1
    assert stages(report)["dry_run"] == "failed" and stages(report)["sample"] == "not_run"


def test_unknown_column_is_confirmed_by_the_database(scorer):
    report = scorer.score("SELECT nme FROM customers")
    assert report.has("unknown_column") and report.has("dry_run_error")
    assert report.score <= 0.1


def test_runtime_error_is_a_gate():
    with Scorer("duckdb", db=make_db("duckdb")) as scorer:
        report = scorer.sample("SELECT CAST(name AS INTEGER) FROM customers")
        assert report.has("execution_error")
        assert report.verdict.value == "invalid"


def test_mutations_never_reach_the_database(engine, db):
    with Scorer(engine, db=db) as scorer:
        for sql in (
            "DELETE FROM customers",
            "UPDATE customers SET name = 'x'",
            "DROP TABLE payments",
            "INSERT INTO profiles VALUES (9, 'z')",
            "WITH d AS (DELETE FROM payments RETURNING id) SELECT * FROM d",
            "SELECT 1; DELETE FROM customers",
        ):
            report = scorer.score(sql, mode="sample")
            assert report.verdict.value == "invalid", sql
            assert stages(report)["dry_run"] == "not_run"
    cursor = db.execute("SELECT (SELECT COUNT(*) FROM customers), (SELECT COUNT(*) FROM payments)")
    assert tuple(cursor.fetchone()) == (4, 4)


def test_adapters_refuse_writes_directly(engine, db):
    adapter = get_adapter(engine, db)
    for call in (
        lambda: adapter.execute("DELETE FROM customers", max_rows=1, timeout_s=1),
        lambda: adapter.dry_run("DROP TABLE customers"),
        lambda: adapter.execute("SELECT 1; SELECT 2", max_rows=1, timeout_s=1),
    ):
        with pytest.raises(UnsafeQueryError):
            call()
    with pytest.raises(UnsafeQueryError):
        assert_read_only("PRAGMA writable_schema = 1", "sqlite")
    assert tuple(db.execute("SELECT COUNT(*) FROM customers").fetchone()) == (4,)


def test_sqlite_connection_is_left_writable_afterwards():
    conn = make_db("sqlite")
    with Scorer("sqlite", db=conn) as scorer:
        scorer.sample(GOOD)
    conn.execute("INSERT INTO profiles VALUES (9, 'still writable')")
    assert conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 4


def test_external_access_is_refused(scorer):
    for sql in (
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT readfile('/etc/passwd')",
        "SELECT pg_sleep(10)",
    ):
        report = scorer.score(sql, mode="sample")
        assert report.has("external_access"), sql
        assert stages(report)["dry_run"] == "not_run"
        assert report.score <= 0.1


def test_query_timeout_is_reported_not_raised():
    conn = make_db("sqlite")
    slow = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 100000000) "
        "SELECT COUNT(*) FROM n"
    )
    with Scorer("sqlite", db=conn, timeout_s=0.2) as scorer:
        report = scorer.sample(slow)
    assert report.has("execution_timeout")
    assert report.confidence.cap == 0.5
    assert conn.execute("SELECT 1").fetchone() == (1,)


def test_duckdb_timeout():
    slow = "SELECT COUNT(*) FROM range(100000000000) a, range(1000) b WHERE a.range % 7 = b.range"
    with Scorer("duckdb", db=make_db("duckdb"), timeout_s=0.3) as scorer:
        report = scorer.sample(slow)
    assert report.has("execution_timeout") or report.has("execution_unavailable"), report.summary()


# ------------------------------------------------------------------ probes


def test_fan_out_is_confirmed_or_dismissed_by_the_data(scorer):
    confirmed = scorer.sample(
        "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id "
        "JOIN items i ON i.order_id = o.id GROUP BY c.name"
    )
    finding = next(f for f in confirmed.findings if f.code == "fan_out")
    assert finding.severity == Severity.HIGH and finding.evidence["data_check"] == "duplicates"
    assert confirmed.verdict.value == "likely_wrong"

    # profiles has no declared key, but customer_id is unique in the data.
    dismissed = scorer.sample("SELECT SUM(o.total) FROM orders o JOIN profiles p ON p.customer_id = o.customer_id")
    finding = next(f for f in dismissed.findings if f.code == "fan_out")
    assert finding.severity == Severity.INFO and finding.evidence["data_check"] == "unique"
    assert dismissed.verdict.value == "likely_right"

    static = scorer.explain("SELECT SUM(o.total) FROM orders o JOIN profiles p ON p.customer_id = o.customer_id")
    finding = next(f for f in static.findings if f.code == "fan_out")
    assert finding.severity == Severity.LOW and finding.evidence["certainty"] == "maybe"


def test_undeclared_duplicates_are_raised_to_high(engine, db):
    db.execute("INSERT INTO profiles VALUES (1, 'duplicate')")
    with Scorer(engine, db=db) as scorer:
        report = scorer.sample("SELECT SUM(o.total) FROM orders o JOIN profiles p ON p.customer_id = o.customer_id")
    finding = next(f for f in report.findings if f.code == "fan_out")
    assert finding.severity == Severity.HIGH
    assert report.score <= 0.4


def test_not_in_probe(scorer, engine, db):
    report = scorer.sample("SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers)")
    finding = next(f for f in report.findings if f.code == "not_in_nullable")
    assert finding.severity == Severity.HIGH and finding.evidence["data_check"] == "has_null"
    assert report.has("empty_result")

    db.execute("UPDATE customers SET referrer_id = 1 WHERE referrer_id IS NULL")
    with Scorer(engine, db=db) as fresh:
        report = fresh.sample("SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers)")
    finding = next(f for f in report.findings if f.code == "not_in_nullable")
    assert finding.severity == Severity.LOW and finding.evidence["data_check"] == "no_null"


def test_filter_value_grounding(scorer):
    wrong_case = scorer.sample("SELECT COUNT(*) FROM customers WHERE country = 'usa'")
    finding = next(f for f in wrong_case.findings if f.code == "filter_value")
    assert finding.severity == Severity.HIGH
    assert finding.evidence["nearest"] == ["US"]
    assert wrong_case.score <= 0.4

    assert not scorer.sample("SELECT COUNT(*) FROM customers WHERE country = 'US'").has("filter_value")
    # The values were remembered, so the static check can now use them without a connection.
    assert scorer.explain("SELECT COUNT(*) FROM customers WHERE country = 'Germany'").has("filter_value")
    never = scorer.sample("SELECT name FROM customers WHERE country <> 'XX'")
    finding = next(f for f in never.findings if f.code == "filter_value")
    assert finding.severity == Severity.MEDIUM


def test_in_list_with_some_values_absent(scorer):
    # 'US' is stored and 'FR' is not: the list still matches, and 'FR' is not a misspelling of anything.
    fine = scorer.sample("SELECT COUNT(*) FROM customers WHERE country IN ('US', 'FR')")
    assert not fine.has("filter_value"), fine.summary()
    # 'us' looks like a misspelling of a stored value: worth saying, but the list still matches.
    near = scorer.sample("SELECT COUNT(*) FROM customers WHERE country IN ('DE', 'us')")
    finding = next(f for f in near.findings if f.code == "filter_value")
    assert finding.severity == Severity.MEDIUM and finding.evidence["nearest"] == ["US"]
    nothing = scorer.sample("SELECT COUNT(*) FROM customers WHERE country IN ('FR', 'ES')")
    assert next(f for f in nothing.findings if f.code == "filter_value").severity == Severity.HIGH


def test_filter_value_on_high_cardinality_column(engine, db):
    from sqlgami import Options

    with Scorer(engine, db=db, options=Options(max_distinct_values=2)) as scorer:
        report = scorer.sample("SELECT COUNT(*) FROM customers WHERE name = 'ada'")
        finding = next(f for f in report.findings if f.code == "filter_value")
        assert finding.evidence["nearest"] == ["Ada"]
        assert not scorer.sample("SELECT COUNT(*) FROM customers WHERE name = 'Ada'").has("filter_value")
        table = scorer.catalog.resolve("customers")
        assert scorer.catalog.column(table, "name").values is None


def test_sqlite_timestamp_layout_mismatch():
    with Scorer("sqlite", db=make_db("sqlite")) as scorer:
        bad = scorer.sample(
            "SELECT COUNT(*) FROM orders WHERE created_at >= '2024-01-05T00:00:00' AND created_at < '2024-02-01T00:00:00'"
        )
        assert bad.has("timestamp_format")
        good = scorer.sample(
            "SELECT COUNT(*) FROM orders WHERE created_at >= '2024-01-05 00:00:00' AND created_at < '2024-02-01 00:00:00'"
        )
        assert not good.has("timestamp_format")


def test_result_signals(scorer):
    empty = scorer.sample("SELECT * FROM orders WHERE total > 100000 ORDER BY id")
    assert empty.has("empty_result") and empty.verdict.value == "uncertain"
    null = scorer.sample("SELECT SUM(total) FROM orders WHERE total > 100000")
    assert null.has("null_result")
    zero = scorer.sample("SELECT COUNT(*) FROM orders WHERE total > 100000")
    assert zero.has("zero_result")
    all_null = scorer.sample("SELECT o.id, o.total, NULL AS note FROM orders o ORDER BY o.id")
    assert all_null.has("all_null_column")


def test_probe_budget_and_row_ceiling(engine, db):
    from sqlgami import Options

    sql = "SELECT SUM(o.total) FROM orders o JOIN profiles p ON p.customer_id = o.customer_id"
    with Scorer(engine, db=db, options=Options(max_probes=0)) as scorer:
        report = scorer.sample(sql)
        assert any(c.code.startswith("probe") for c in report.checks_not_run)
        assert next(f for f in report.findings if f.code == "fan_out").severity == Severity.LOW
    with Scorer(engine, db=db, options=Options(probe_row_ceiling=1)) as scorer:
        report = scorer.sample(sql)
        assert any("too large to probe" in c.reason for c in report.checks_not_run)


# ------------------------------------------------------------ introspection


def test_introspection_reads_keys_and_views(engine, db):
    db.execute("CREATE UNIQUE INDEX profiles_bio ON profiles (bio)")
    adapter = get_adapter(engine, db)
    catalog = adapter.catalog()
    orders = catalog.resolve("orders")
    assert orders.primary_key == ("id",)
    assert catalog.column(orders, "customer_id").nullable is False
    assert catalog.column(orders, "total").nullable is True
    assert orders.foreign_keys and orders.foreign_keys[0].ref_table == "customers"
    profiles = catalog.resolve("profiles")
    assert profiles.primary_key == () and ("bio",) in profiles.unique_keys
    view = catalog.resolve("big_customers")
    assert view.is_view and "order_totals" in view.definition
    assert catalog.resolve("ORDERS") is orders
    assert catalog.resolve("nope") is None
    schema = schema_of(engine, db)
    assert f"{schema}.orders" in catalog.table_names() and f"{schema}.big_customers" in catalog.table_names()


def test_view_on_view_through_a_live_database(scorer, engine, db):
    schema = schema_of(engine, db)
    report = scorer.sample(
        "SELECT c.name, b.spend FROM big_customers b JOIN customers c ON c.id = b.cid ORDER BY b.spend DESC"
    )
    assert stages(report)["schema"] == "passed"
    assert not report.findings_at_least(Severity.MEDIUM), report.summary()
    assert report.tables == [f"{schema}.customers", f"{schema}.items", f"{schema}.orders"]
    assert set(report.signals["views_expanded"]) == {f"{schema}.big_customers", f"{schema}.order_totals"}


def test_open_by_path_is_read_only(tmp_path, engine):
    if engine == "postgres":
        pytest.skip("file databases only")
    path = tmp_path / f"shop.{engine}"
    if engine == "sqlite":
        conn = sqlite3.connect(path)
        conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t VALUES (1, 'a');")
        conn.commit()
        conn.close()
    else:
        duckdb = pytest.importorskip("duckdb")
        conn = duckdb.connect(str(path))
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t VALUES (1, 'a');")
        conn.close()
    with Scorer(engine, db=str(path)) as scorer:
        report = scorer.sample("SELECT v FROM t ORDER BY id")
        assert report.signals["rows_returned"] == 1
        with pytest.raises(Exception):
            scorer.adapter.conn.execute("INSERT INTO t VALUES (2, 'b')")
    with pytest.raises(ConnectionFailed):
        Scorer(engine, db=str(tmp_path / "missing" / "nope.db"))


def test_bad_db_argument(engine):
    with pytest.raises(SqlgamiError):
        Scorer(engine, db=12345)


# ------------------------------------------------------------ report shape

EXPECTED_KEYS = {
    "schema_version",
    "advisory",
    "engine",
    "mode",
    "question",
    "sql",
    "verdict",
    "confidence",
    "stages",
    "findings",
    "checks_not_run",
    "cost",
    "signals",
    "tables",
    "plan",
}

BANNED_WORDS = ("guaranteed", "certified", "accurate")


def test_report_is_json_and_keeps_its_shape(scorer):
    report = scorer.sample(
        "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id "
        "JOIN items i ON i.order_id = o.id WHERE c.country = 'usa' GROUP BY c.name",
        question="Total order value per US customer",
    )
    data = json.loads(report.to_json())
    assert set(data) == EXPECTED_KEYS
    assert data["schema_version"] == SCHEMA_VERSION == "1.0"
    assert data["advisory"] == ADVISORY
    assert data["question"] == "Total order value per US customer"
    assert set(data["confidence"]) == {"value", "raw", "low", "high", "calibrated", "cap", "cap_reason", "basis"}
    assert 0.0 <= data["confidence"]["low"] <= data["confidence"]["value"] <= data["confidence"]["high"] <= 1.0
    for finding in data["findings"]:
        assert set(finding) == {"code", "severity", "message", "stage", "snippet", "suggestion", "evidence"}
        assert finding["severity"] in {"error", "high", "medium", "low", "info"}
    for stage in data["stages"]:
        assert set(stage) == {"name", "status", "detail", "duration_ms"}
    assert [s["name"] for s in data["stages"]] == ["parse", "statement", "schema", "static", "dry_run", "sample", "consistency", "question"]
    assert data == report.to_dict() or json.loads(json.dumps(report.to_dict())) == data


def test_every_report_carries_the_advisory_and_avoids_overclaiming(scorer):
    assert "not a guarantee" in ADVISORY and "no warranty" in ADVISORY
    queries = [
        GOOD,
        "SELEC",
        "DELETE FROM orders",
        "SELECT nme FROM customers",
        "SELECT * FROM customers WHERE deleted_at = NULL",
        "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id "
        "JOIN items i ON i.order_id = o.id GROUP BY c.name",
        "SELECT name FROM customers WHERE id NOT IN (SELECT referrer_id FROM customers)",
        "SELECT COUNT(*) FROM customers WHERE country = 'usa'",
    ]
    for sql in queries:
        for mode in ("static", "dry", "sample"):
            report = scorer.score(sql, mode=mode)
            text = report.to_json().lower() + report.summary().lower()
            assert report.advisory == ADVISORY
            assert ADVISORY in report.summary()
            for word in BANNED_WORDS:
                assert word not in text.replace(sql.lower(), ""), (word, sql)


def test_scoring_the_same_sql_twice_gives_the_same_report(scorer):
    first = scorer.sample(GOOD).to_dict()
    second = scorer.sample(GOOD).to_dict()
    for data in (first, second):
        for stage in data["stages"]:
            stage["duration_ms"] = None
    assert first == second


# -------------------------------------------------------- library manners


def test_import_pulls_in_no_optional_or_ui_dependency():
    code = textwrap.dedent(
        """
        import sys
        import sqlgami
        from sqlgami import Scorer
        Scorer("sqlite", catalog="CREATE TABLE t (id INT PRIMARY KEY)").explain("SELECT id FROM t")
        banned = ["duckdb", "psycopg", "psycopg2", "google", "lark", "flask", "fastapi", "django",
                  "streamlit", "requests", "httpx", "aiohttp", "urllib3", "anthropic", "openai",
                  "pandas", "numpy", "mcp", "click", "rich", "pydantic"]
        loaded = [name for name in banned if name in sys.modules]
        third_party = sorted({m.split(".")[0] for m, mod in sys.modules.items()
                              if getattr(mod, "__file__", None) and "site-packages" in (mod.__file__ or "")
                              and not m.startswith("_")})
        print(loaded, third_party)
        """
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    loaded, third_party = out.strip().split("] [")
    assert loaded == "[", out
    assert third_party.rstrip("]").replace("'", "").split(", ") == ["sqlglot"], out


def test_library_does_not_configure_logging_or_print(capsys, caplog):
    root_handlers = list(logging.getLogger().handlers)
    with Scorer("sqlite", db=make_db("sqlite")) as scorer:
        scorer.sample("SELECT nme FROM customers")
        scorer.sample(GOOD)
    assert logging.getLogger().handlers == root_handlers
    handlers = logging.getLogger("sqlgami").handlers
    assert handlers and all(isinstance(h, logging.NullHandler) for h in handlers)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_two_scorers_are_independent():
    a = Scorer("sqlite", db=make_db("sqlite"), tenant_column="tenant_id")
    b = Scorer("sqlite", db=make_db("sqlite"))
    sql = "SELECT COUNT(*) FROM orders"
    assert a.score(sql).has("missing_tenant_filter")
    assert not b.score(sql).has("missing_tenant_filter")
    a.close()
    b.close()


# --------------------------------------------------------------------- CLI


def test_cli(tmp_path, capsys):
    from sqlgami.cli import main

    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    from conftest import SHOP_DATA, SHOP_DDL

    conn.executescript(SHOP_DDL + SHOP_DATA)
    conn.commit()
    conn.close()

    assert main(["score", "--engine", "sqlite", "--db", str(path), "--mode", "sample", GOOD]) == 0
    out = capsys.readouterr().out
    assert "likely_right" in out and ADVISORY in out

    assert main(["score", "--engine", "sqlite", "--db", str(path), "--json", "SELECT nme FROM customers"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == "invalid" and data["advisory"] == ADVISORY

    schema = tmp_path / "schema.sql"
    schema.write_text(SHOP_DDL)
    query = tmp_path / "q.sql"
    query.write_text("SELECT * FROM customers WHERE deleted_at = NULL")
    code = main(
        ["score", "--engine", "postgres", "--schema", str(schema), "--sql-file", str(query), "--fail-under", "0.5"]
    )
    assert code == 1
    assert "null_comparison" in capsys.readouterr().out

    assert main(["score", "--engine", "sqlite", "--db", str(tmp_path / "nope" / "x.db"), "SELECT 1"]) == 2
    assert "sqlgami:" in capsys.readouterr().err


# ------------------------------------------------- regressions from the bench


def test_duckdb_stored_view_text_with_nested_join_groups():
    """DuckDB rewrites view bodies with every join wrapped in parentheses."""
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, gone TIMESTAMP);
        CREATE TABLE d (id INTEGER PRIMARY KEY, t_id INTEGER, status TEXT);
        CREATE TABLE i (id INTEGER PRIMARY KEY, t_id INTEGER, cents INTEGER);
        CREATE TABLE p (id INTEGER PRIMARY KEY, label TEXT);
        INSERT INTO t VALUES (1, 'a', NULL), (2, 'b', NULL);
        INSERT INTO d VALUES (1, 1, 'open'), (2, 1, 'open');
        INSERT INTO i VALUES (1, 1, 100), (2, 2, 50);
        INSERT INTO p VALUES (1, 'x');
        CREATE VIEW chasm_v AS
            SELECT t.id, COUNT(DISTINCT d.id) AS n, SUM(i.cents) AS cents
            FROM t LEFT JOIN d ON d.t_id = t.id AND d.status = 'open'
                   LEFT JOIN i ON i.t_id = t.id
            WHERE t.gone IS NULL GROUP BY t.id;
        CREATE VIEW live_v AS SELECT id, name FROM t WHERE gone IS NULL;
        CREATE VIEW on_view_v AS SELECT l.id, l.name, i.cents FROM live_v l JOIN i ON i.t_id = l.id;
        """
    )
    stored = conn.execute("SELECT sql FROM duckdb_views() WHERE view_name = 'chasm_v'").fetchone()[0]
    assert "((" in stored  # the shape this test exists for
    with Scorer("duckdb", db=conn) as scorer:
        chasm = scorer.sample("SELECT * FROM chasm_v ORDER BY id")
        assert stages(chasm)["schema"] == "passed"
        assert chasm.has("fan_out") and not chasm.has("cartesian_join"), chasm.summary()
        on_view = scorer.sample("SELECT * FROM on_view_v ORDER BY id")
        assert stages(on_view)["schema"] == "passed", on_view.summary()
        assert not on_view.findings_at_least(Severity.MEDIUM), on_view.summary()
        grouped = scorer.sample(
            "SELECT t.name, x.label FROM (t JOIN d ON d.t_id = t.id) CROSS JOIN p AS x ORDER BY 1"
        )
        assert stages(grouped)["schema"] == "passed", grouped.summary()


def test_duckdb_time_zone_columns_need_no_extra_package():
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE e (id INTEGER PRIMARY KEY, seen_at TIMESTAMPTZ); "
        "INSERT INTO e VALUES (1, '2026-03-08 10:00:00+00'), (2, NULL);"
    )
    with Scorer("duckdb", db=conn) as scorer:
        report = scorer.sample("SELECT id, seen_at, id AS id FROM e ORDER BY id")
    assert stages(report)["sample"] == "passed", report.summary()
    assert report.signals["rows_returned"] == 2
    assert report.signals["columns"][1] == "seen_at" and len(report.signals["columns"]) == 3


def test_sqlite_cost_counts_rows_not_ids():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE big_ids (id INTEGER PRIMARY KEY, v TEXT);"
        "INSERT INTO big_ids VALUES (9000000000, 'a'), (9000000001, 'b');"
    )
    with Scorer("sqlite", db=conn) as scorer:
        report = scorer.sample("SELECT v FROM big_ids ORDER BY id")
    assert report.cost.value == 2
    assert stages(report)["sample"] == "passed"
