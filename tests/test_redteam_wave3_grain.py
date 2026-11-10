"""Red-team wave 3 (a): the sample-mode JOIN-grain probe and key-reasoning leniency at 84023f7.

The probe (commit e98b09b) runs the query and checks whether a row of the aggregated table comes
back more than once; uncorrelated scalar subqueries are treated as constants for key reasoning.
These tests look for false negatives: a real fan-out that the probe lets through, or a severity
dropped because of a sparse sample.

Each case executes against a throwaway in-memory database and states the true vs returned sum.
"""

from __future__ import annotations

import sqlite3

import pytest

from sqlgami import Catalog, Scorer


def _fan_out(scorer_db, ddl, sql, dialect="sqlite", mode="sample"):
    cat = Catalog.from_ddl(ddl, dialect)
    with Scorer(dialect, db=scorer_db, catalog=cat) as s:
        report = getattr(s, mode)(sql)
    return [(f.severity.value, f.evidence.get("data_check")) for f in report.findings if f.code == "fan_out"]


FK_DDL = (
    "CREATE TABLE orders(id INTEGER PRIMARY KEY, total NUMERIC);"
    "CREATE TABLE items(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), qty INTEGER);"
)
FANOUT_SQL = "SELECT SUM(o.total) FROM orders o JOIN items i ON i.order_id = o.id"


def _db(ddl, data):
    db = sqlite3.connect(":memory:")
    db.executescript(ddl + data)
    return db


def test_static_fan_out_on_declared_fk_is_high():
    # No database: the declared FK means items can repeat orders, so SUM(o.total) is HIGH.
    cat = Catalog.from_ddl(FK_DDL, "sqlite")
    report = Scorer("sqlite", catalog=cat).explain(FANOUT_SQL)
    sev = [f.severity.value for f in report.findings if f.code == "fan_out"]
    assert sev == ["high"], report.summary()


def test_sparse_sample_does_not_downgrade_a_declared_fk_fanout():
    sparse = _db(FK_DDL, "INSERT INTO orders VALUES (1,100),(2,200),(3,300);"
                         "INSERT INTO items VALUES (10,1,1),(11,2,1),(12,3,1);")
    try:
        sev = {s for s, _ in _fan_out(sparse, FK_DDL, FANOUT_SQL)}
        assert "high" in sev
    finally:
        sparse.close()


def test_realistic_sample_fan_out_is_high_and_inflation_is_real():
    real = _db(FK_DDL, "INSERT INTO orders VALUES (1,100),(2,200);"
                       "INSERT INTO items VALUES (10,1,1),(11,1,1),(12,1,1),(13,2,1);")
    try:
        assert real.execute(FANOUT_SQL).fetchone()[0] != real.execute("SELECT SUM(total) FROM orders").fetchone()[0]
        assert ("high", "duplicates") in _fan_out(real, FK_DDL, FANOUT_SQL)
    finally:
        real.close()


# Key-reasoning leniency that held up (guards): a scalar subquery that is genuinely single-valued
# pins one row (correctly quiet), and a non-single-valued "= (subquery)" is not silently trusted.
def test_single_valued_subquery_pin_is_respected():
    db = _db(FK_DDL, "INSERT INTO orders VALUES (1,100),(2,200);"
                     "INSERT INTO items VALUES (10,1,1),(11,1,1),(12,2,1);")
    try:
        pinned = "SELECT SUM(o.total) FROM orders o JOIN items i ON i.order_id = o.id WHERE i.id = (SELECT MAX(id) FROM items)"
        assert not _fan_out(db, FK_DDL, pinned), "a one-row pin removes the fan-out"
    finally:
        db.close()
