"""Fixture datasets: deterministic, shaped by the schema and the declared rules, loadable in
milliseconds."""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import SHOP_DDL
from sqlgami import Catalog, fixtures
from sqlgami.errors import ConfigurationError

duckdb = pytest.importorskip("duckdb")


def _catalog() -> Catalog:
    return Catalog.from_ddl(SHOP_DDL, "duckdb")


def test_fixtures_are_bit_exact_and_follow_keys():
    one = fixtures.build(_catalog(), {"seed": 3, "profiles": ["clean"]})[0]
    two = fixtures.build(_catalog(), {"seed": 3, "profiles": ["clean"]})[0]
    assert one.digest() == two.digest() and one.tables == two.tables
    other = fixtures.build(_catalog(), {"seed": 4, "profiles": ["clean"]})[0]
    assert other.digest() != one.digest()
    conn = one.connect()
    # every order points at a customer; customer ids are unique
    assert conn.execute("SELECT COUNT(*) FROM orders o LEFT JOIN customers c ON c.id = o.customer_id WHERE c.id IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) - COUNT(DISTINCT id) FROM customers").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE total IS NULL").fetchone()[0] == 0  # clean: no NULLs


def test_profiles_inject_what_they_say():
    cat = _catalog()
    nulls = fixtures.build(cat, {"seed": 1, "profiles": ["nulls"]})[0].connect()
    assert nulls.execute("SELECT COUNT(*) FROM orders WHERE status IS NULL").fetchone()[0] > 0
    ties = fixtures.build(cat, {"seed": 1, "profiles": ["ties"]})[0].connect()
    assert ties.execute("SELECT COUNT(*) - COUNT(DISTINCT total) FROM orders").fetchone()[0] > 0
    sparse = fixtures.build(cat, {"seed": 1, "profiles": ["sparse"], "rows": {"customers": 12, "orders": 12}})[0].connect()
    assert sparse.execute("SELECT COUNT(*) FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)").fetchone()[0] > 0
    dup_catalog = Catalog.from_ddl("CREATE TABLE tags (customer_id INTEGER, tag TEXT)", "duckdb")
    dups = fixtures.build(dup_catalog, {"seed": 1, "profiles": ["duplicates"], "rows": {"tags": 60}})[0].connect()
    assert dups.execute("SELECT COUNT(*) - COUNT(DISTINCT (customer_id, tag)) FROM tags").fetchone()[0] > 0


def test_snapshot_restatements_and_critical_rows_and_parameters():
    ddl = (
        "CREATE TABLE snap (assoc_id INTEGER, period_end_dt DATE, snap_dt DATE, status_cd TEXT, lvl_3_id INTEGER, "
        "PRIMARY KEY (assoc_id, period_end_dt, snap_dt));"
    )
    cat = Catalog.from_ddl(ddl, "duckdb", snapshot_columns={"snap": "snap_dt"})
    rules = {
        "seed": 2,
        "profiles": ["duplicates"],
        "rows": {"snap": 100},
        "values": {"snap.status_cd": ["A", "L"]},
        "params": {"manager_id": [12003]},
        "param_columns": {"manager_id": ["snap.lvl_3_id"]},
        "critical_rows": {"snap": [{"assoc_id": 12028, "period_end_dt": "2024-03-01", "snap_dt": "2024-03-01", "status_cd": "L", "lvl_3_id": 12003}]},
    }
    fixture = fixtures.build(cat, rules)[0]
    conn = fixture.connect()
    assert conn.execute("SELECT COUNT(*) FROM snap WHERE snap_dt > period_end_dt").fetchone()[0] > 0  # restated rows
    assert conn.execute("SELECT status_cd FROM snap WHERE assoc_id = 12028 AND snap_dt = DATE '2024-03-01'").fetchone() == ("L",)
    assert conn.execute("SELECT COUNT(*) FROM snap WHERE lvl_3_id = 12003").fetchone()[0] >= 2
    assert set(r[0] for r in conn.execute("SELECT DISTINCT status_cd FROM snap WHERE status_cd IS NOT NULL").fetchall()) <= {"A", "L"}


def test_fixture_round_trips_through_csv_and_refuses_an_edited_copy(tmp_path):
    fixture = fixtures.build(_catalog(), {"seed": 5, "profiles": ["edges"]})[0]
    fixture.write(tmp_path / "f")
    back = fixtures.Fixture.read(tmp_path / "f")
    assert back.digest() == fixture.digest()  # the CSV text is the stored truth (an empty string reads back as NULL)
    path = tmp_path / "f" / "main.orders.csv"
    path.write_text(path.read_text().replace("\n", "\n", 1) + "999,1,2024-01-01,paid,1\n")
    with pytest.raises(ConfigurationError, match="digest"):
        fixtures.Fixture.read(tmp_path / "f")


def test_unknown_profile_is_refused():
    with pytest.raises(ConfigurationError, match="profile"):
        fixtures.build(_catalog(), {"profiles": ["chaos"]})


# The digests below are pinned: a fixture must be a function of the committed code and the
# rules alone, on every machine. A change in generation is a deliberate, visible change here.
PINNED_RULES = {
    "seed": 7,
    "profiles": ["clean", "nulls"],
    "rows": {"customers": 5, "orders": 8, "items": 12, "payments": 6, "profiles": 5},
    "values": {"orders.status": ["paid", "open"]},
    "params": {"customer": [2]},
    "param_columns": {"customer": ["orders.customer_id"]},
    "critical_rows": {"customers": [{"id": 99, "name": "Zed", "tenant_id": 1}]},
}
PINNED_DIGESTS = {
    "clean": "d032b057450215d5de0815cb1d4de90487b0b6b4052900760fb0635173f47798",
    "nulls": "10939405dd79da5954f2bba11e98eed3705aa6dd3e5a9275cd2c897bcdb6254a",
}


@pytest.mark.parametrize("profile", sorted(PINNED_DIGESTS))
def test_fixture_digest_is_pinned(profile):
    fixture = fixtures.build(_catalog(), PINNED_RULES, profiles=[profile])[0]
    assert fixture.digest() == PINNED_DIGESTS[profile], (
        f"{profile} fixture changed: {fixture.digest()}; if the generator changed on purpose, update the pin"
    )


def test_fixtures_depend_on_logical_schema_only_not_the_dialect_spelling():
    """The same schema written for two dialects (SERIAL vs INTEGER, TIMESTAMPTZ vs TIMESTAMP WITH
    TIME ZONE, mixed-case identifiers, a partition declared as a table) gives the same fixture."""
    duck = """
    CREATE TABLE "Shop"."Customers" (id INTEGER PRIMARY KEY, "Name" VARCHAR NOT NULL, created_at TIMESTAMPTZ);
    CREATE TABLE "Shop"."Orders" (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES "Shop"."Customers"(id), total DECIMAL(10,2), status VARCHAR);
    CREATE VIEW "Shop".paid_v AS SELECT id FROM "Shop"."Orders" WHERE status = 'paid';
    """
    pg = """
    CREATE TABLE shop.customers (id SERIAL PRIMARY KEY, name TEXT NOT NULL, created_at TIMESTAMP WITH TIME ZONE);
    CREATE TABLE shop.orders (id SERIAL PRIMARY KEY, customer_id INT REFERENCES shop.customers(id), total NUMERIC(10,2), status TEXT) PARTITION BY RANGE (id);
    CREATE TABLE shop.orders_p1 PARTITION OF shop.orders FOR VALUES FROM (1) TO (100);
    CREATE VIEW shop.paid_v AS SELECT id FROM shop.orders WHERE status = 'paid';
    """
    bq = """
    CREATE TABLE `acme-prod.shop.customers` (id INT64 NOT NULL, name STRING NOT NULL, created_at TIMESTAMP, PRIMARY KEY (id) NOT ENFORCED);
    CREATE TABLE `acme-prod.shop.orders` (id INT64 NOT NULL, customer_id INT64, total NUMERIC(10,2), status STRING,
      PRIMARY KEY (id) NOT ENFORCED, FOREIGN KEY (customer_id) REFERENCES `acme-prod.shop.customers` (id) NOT ENFORCED)
      PARTITION BY RANGE_BUCKET(id, GENERATE_ARRAY(0, 1000, 100)) CLUSTER BY customer_id;
    CREATE VIEW `acme-prod.shop.paid_v` AS SELECT id FROM `acme-prod.shop.orders` WHERE status = 'paid';
    """
    rules = {"seed": 3, "profiles": ["clean"], "rows": {"shop.customers": 4, "shop.orders": 6}}
    a = fixtures.build(Catalog.from_ddl(duck, "duckdb"), rules)[0]
    b = fixtures.build(Catalog.from_ddl(pg, "postgres"), rules)[0]
    c = fixtures.build(Catalog.from_ddl(bq, "bigquery"), rules)[0]
    assert a.digest() == b.digest(), (a.ddl, b.ddl)
    assert a.digest() == c.digest(), (a.ddl, c.ddl)  # a three-part BigQuery name is the logical schema.table
    assert a.views_digest() == c.views_digest(), (a.ddl, c.ddl)
    assert set(a.tables) == set(c.tables) == {"shop.customers", "shop.orders"}


HR_SHAPE = """
CREATE TABLE hr.pay_period_dim (ppe_dt DATE, freq_cd TEXT, period_seq INTEGER, is_current_flag BOOLEAN, PRIMARY KEY (ppe_dt, freq_cd));
CREATE TABLE hr.employee_dim (assoc_id INTEGER PRIMARY KEY, hire_dt DATE, term_dt DATE);
CREATE TABLE hr.emp_period_snapshot (assoc_id INTEGER REFERENCES hr.employee_dim(assoc_id), period_end_dt DATE, snap_dt DATE, status_cd TEXT, hire_dt DATE, term_dt DATE,
    PRIMARY KEY (assoc_id, period_end_dt, snap_dt));
"""
HR_RULES = {
    "seed": 5,
    "profiles": ["clean"],
    "rows": {"hr.pay_period_dim": 30, "hr.employee_dim": 12},
    "values": {"hr.pay_period_dim.freq_cd": ["B", "W"], "hr.emp_period_snapshot.status_cd": ["A", "L", "T"]},
    "rules": [
        {"kind": "date_step", "column": "hr.pay_period_dim.ppe_dt", "per": ["freq_cd"], "step": {"B": "14 days", "W": "7 days"}, "start": "2024-01-05"},
        {"kind": "one_true", "column": "hr.pay_period_dim.is_current_flag", "per": ["freq_cd"], "at": "middle"},
        {"kind": "sequence", "column": "hr.pay_period_dim.period_seq", "order_by": "ppe_dt", "per": ["freq_cd"], "zero_where": "is_current_flag"},
        {
            "kind": "grid",
            "table": "hr.emp_period_snapshot",
            "per": {"assoc_id": "hr.employee_dim.assoc_id", "period_end_dt": "hr.pay_period_dim.ppe_dt"},
            "where": {"hr.pay_period_dim.freq_cd": "B"},
            "between": {"column": "period_end_dt", "start": "hr.employee_dim.hire_dt", "end": "hr.employee_dim.term_dt"},
            "copy": {"hire_dt": "hr.employee_dim.hire_dt", "term_dt": "hr.employee_dim.term_dt"},
            "snapshot": {"column": "snap_dt", "equals": "period_end_dt", "restated_rate": 0.3},
        },
    ],
}


def test_declared_rules_shape_period_and_snapshot_tables():
    cat = Catalog.from_ddl(HR_SHAPE, "duckdb", snapshot_columns={"hr.emp_period_snapshot": "snap_dt"})
    fixture = fixtures.build(cat, HR_RULES)[0]
    conn = fixture.connect()
    # exactly one current period per frequency, 14-day biweekly steps, period_seq consecutive and 0 at the current one
    assert conn.execute("SELECT freq_cd, COUNT(*) FILTER (WHERE is_current_flag) FROM hr.pay_period_dim GROUP BY freq_cd ORDER BY 1").fetchall() == [("B", 1), ("W", 1)]
    gaps = conn.execute("SELECT DISTINCT ppe_dt - LAG(ppe_dt) OVER (ORDER BY ppe_dt) FROM hr.pay_period_dim WHERE freq_cd = 'B' QUALIFY LAG(ppe_dt) OVER (ORDER BY ppe_dt) IS NOT NULL").fetchall()
    assert gaps == [(14,)]  # DuckDB DATE - DATE is days
    assert conn.execute("SELECT period_seq FROM hr.pay_period_dim WHERE freq_cd = 'B' AND is_current_flag").fetchone() == (0,)
    seqs = [r[0] for r in conn.execute("SELECT period_seq FROM hr.pay_period_dim WHERE freq_cd = 'B' ORDER BY ppe_dt").fetchall()]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))) and seqs[0] < 0 < seqs[-1]
    # the snapshot grid: biweekly periods only, inside each employee's tenure, snap_dt = period_end_dt plus restatements
    assert conn.execute("SELECT COUNT(*) FROM hr.emp_period_snapshot s WHERE s.period_end_dt NOT IN (SELECT ppe_dt FROM hr.pay_period_dim WHERE freq_cd = 'B')").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM hr.emp_period_snapshot s JOIN hr.employee_dim e USING (assoc_id) WHERE s.period_end_dt < e.hire_dt OR (e.term_dt IS NOT NULL AND s.period_end_dt > e.term_dt)").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM hr.emp_period_snapshot WHERE snap_dt = period_end_dt").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM hr.emp_period_snapshot WHERE snap_dt > period_end_dt").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM hr.emp_period_snapshot WHERE hire_dt IS NULL").fetchone()[0] == 0  # copied from the parent
    # "headcount at the current period" is a real number on this fixture, not zero
    assert conn.execute("SELECT COUNT(DISTINCT assoc_id) FROM hr.emp_period_snapshot s JOIN hr.pay_period_dim p ON p.ppe_dt = s.period_end_dt WHERE p.is_current_flag AND p.freq_cd = 'B' AND s.snap_dt = s.period_end_dt").fetchone()[0] > 0
    # deterministic
    assert fixtures.build(cat, HR_RULES)[0].digest() == fixture.digest()


def test_rules_are_validated_against_the_catalogue():
    cat = Catalog.from_ddl(HR_SHAPE, "duckdb")
    with pytest.raises(ConfigurationError, match="no column nope"):
        fixtures.build(cat, {"profiles": ["clean"], "rules": [{"kind": "one_true", "column": "hr.pay_period_dim.nope"}]})
    with pytest.raises(ConfigurationError, match="kind must be one of"):
        fixtures.build(cat, {"profiles": ["clean"], "rules": [{"kind": "magic", "column": "hr.pay_period_dim.ppe_dt"}]})
