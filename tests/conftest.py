"""A small shop database, built identically in SQLite and DuckDB."""

from __future__ import annotations

import itertools
import os
import sqlite3

import pytest

SHOP_DDL = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT,
    tenant_id INTEGER NOT NULL,
    deleted_at TIMESTAMP,
    referrer_id INTEGER
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    total NUMERIC(12,2),
    created_at TIMESTAMP,
    order_date DATE,
    status TEXT,
    tenant_id INTEGER NOT NULL
);
CREATE TABLE items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    sku TEXT,
    qty INTEGER,
    price NUMERIC(12,2)
);
CREATE TABLE payments (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    amount NUMERIC(12,2),
    paid_at TIMESTAMP
);
CREATE TABLE profiles (
    customer_id INTEGER,
    bio TEXT
);
CREATE VIEW order_totals AS
    SELECT o.id AS order_id, o.customer_id, o.tenant_id, SUM(i.qty * i.price) AS amount
    FROM orders o JOIN items i ON i.order_id = o.id
    GROUP BY o.id, o.customer_id, o.tenant_id;
CREATE VIEW big_customers (cid, spend) AS
    SELECT customer_id, SUM(amount) FROM order_totals GROUP BY customer_id;
CREATE VIEW cust_avg AS
    SELECT customer_id, AVG(total) AS avg_total FROM orders GROUP BY customer_id;
"""

SHOP_DATA = """
INSERT INTO customers VALUES
    (1, 'Ada', 'US', 1, NULL, NULL),
    (2, 'Bo', 'DE', 1, NULL, 1),
    (3, 'Cy', 'IN', 2, NULL, 1),
    (4, 'Di', 'US', 2, '2024-03-01 10:00:00', NULL);
INSERT INTO orders VALUES
    (10, 1, 100.00, '2024-01-05 09:30:00', '2024-01-05', 'paid', 1),
    (11, 1, 50.00, '2024-01-31 18:45:00', '2024-01-31', 'paid', 1),
    (12, 2, 75.50, '2024-02-01 00:00:00', '2024-02-01', 'open', 1),
    (13, 3, 20.00, '2024-02-10 12:00:00', '2024-02-10', 'cancelled', 2),
    (14, 3, 20.00, '2024-02-11 12:00:00', '2024-02-11', 'paid', 2);
INSERT INTO items VALUES
    (100, 10, 'A', 2, 25.00),
    (101, 10, 'B', 1, 50.00),
    (102, 11, 'A', 2, 25.00),
    (103, 12, 'C', 1, 75.50),
    (104, 13, 'A', 1, 20.00),
    (105, 14, 'A', 1, 20.00);
INSERT INTO payments VALUES
    (1000, 10, 60.00, '2024-01-05 10:00:00'),
    (1001, 10, 40.00, '2024-01-06 10:00:00'),
    (1002, 11, 50.00, '2024-02-01 08:00:00'),
    (1003, 14, 20.00, '2024-02-11 13:00:00');
INSERT INTO profiles VALUES (1, 'first'), (2, 'second'), (3, 'third');
"""

# Postgres tests run only when a test server is named, for example a local container:
#   SQLGAMI_TEST_PG_DSN=postgresql://user:password@127.0.0.1:5440/dbname
PG_DSN = os.environ.get("SQLGAMI_TEST_PG_DSN")

ENGINES = ("sqlite", "duckdb") + (("postgres",) if PG_DSN else ())

_pg_schema_counter = itertools.count()


def make_sqlite() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SHOP_DDL + SHOP_DATA)
    conn.commit()
    return conn


def make_duckdb():
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(":memory:")
    conn.execute(SHOP_DDL + SHOP_DATA)
    return conn


def make_postgres():
    """A connection whose search_path is a fresh schema holding the shop tables."""
    psycopg = pytest.importorskip("psycopg")
    conn = psycopg.connect(PG_DSN, autocommit=True)
    schema = f"sqlgami_test_{os.getpid()}_{next(_pg_schema_counter)}"
    conn.execute(f'CREATE SCHEMA "{schema}"')
    conn.execute(f'SET search_path = "{schema}"')
    conn.execute(SHOP_DDL)
    conn.execute(SHOP_DATA)
    conn.execute("ANALYZE")
    conn.sqlgami_test_schema = schema
    return conn


def drop_db(conn) -> None:
    schema = getattr(conn, "sqlgami_test_schema", None)
    try:
        if schema:
            conn.rollback()
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        conn.close()


def schema_of(engine: str, conn) -> str:
    """The schema the shop tables live in for this connection."""
    return getattr(conn, "sqlgami_test_schema", "main")


def make_db(engine: str):
    if engine == "sqlite":
        return make_sqlite()
    if engine == "duckdb":
        return make_duckdb()
    return make_postgres()


@pytest.fixture(params=ENGINES)
def engine(request) -> str:
    return request.param


@pytest.fixture
def db(engine):
    conn = make_db(engine)
    yield conn
    drop_db(conn)


@pytest.fixture
def scorer(engine, db):
    from sqlgami import Scorer

    with Scorer(engine, db=db) as s:
        yield s
