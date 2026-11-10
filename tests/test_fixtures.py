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
