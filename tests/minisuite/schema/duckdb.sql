-- minisuite, DuckDB
INSTALL icu;
LOAD icu;
CREATE SCHEMA shop;
CREATE TABLE shop.customers (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    country TEXT,
    is_vip BOOLEAN NOT NULL,
    deleted_at TIMESTAMPTZ
);
CREATE TABLE shop.orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES shop.customers(id),
    tenant_id INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE VIEW shop.live_customers_v AS
    SELECT id, tenant_id, name, country FROM shop.customers WHERE deleted_at IS NULL;
