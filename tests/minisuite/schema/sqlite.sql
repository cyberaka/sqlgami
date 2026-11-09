-- minisuite, SQLite
PRAGMA foreign_keys = ON;
CREATE TABLE shop_customers (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    country TEXT,
    is_vip INTEGER NOT NULL,
    deleted_at TEXT
);
CREATE TABLE shop_orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES shop_customers(id),
    tenant_id INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE VIEW shop_live_customers_v AS
    SELECT id, tenant_id, name, country FROM shop_customers WHERE deleted_at IS NULL;
