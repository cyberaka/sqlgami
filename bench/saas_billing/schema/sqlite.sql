-- saas_billing — SQLite
-- DDL only. No data. Loader fills tables from generate.py output.
-- SQLite has one database; the mechanical rule is <schema>_<table>
-- (quoted when mixed-case). Timestamps are TEXT ISO 8601 UTC with +00:00.
-- Booleans are INTEGER 0/1. JSON is TEXT (json1 extension is built in).
-- All gold SQL assumes the UTC session zone.

PRAGMA foreign_keys = ON;

-- ===========================================================================
-- billing: reference / small catalogues
-- ===========================================================================

CREATE TABLE billing_currencies (
    currency_code      TEXT        PRIMARY KEY,
    name               TEXT        NOT NULL,
    minor_unit_scale   INTEGER     NOT NULL
);

CREATE TABLE billing_exchange_rates (
    from_ccy      TEXT        NOT NULL REFERENCES billing_currencies(currency_code),
    to_ccy        TEXT        NOT NULL REFERENCES billing_currencies(currency_code),
    rate_date     TEXT        NOT NULL,
    rate_e8       INTEGER     NOT NULL,
    source        TEXT        NOT NULL,
    PRIMARY KEY (from_ccy, to_ccy, rate_date)
);

CREATE TABLE billing_tax_jurisdictions (
    jurisdiction_id   INTEGER     PRIMARY KEY,
    name              TEXT        NOT NULL,
    code              TEXT        NOT NULL,
    parent_id         INTEGER     NULL REFERENCES billing_tax_jurisdictions(jurisdiction_id),
    level             TEXT        NOT NULL
);

CREATE TABLE billing_tax_rates (
    tax_rate_id       INTEGER     PRIMARY KEY,
    jurisdiction_id   INTEGER     NOT NULL REFERENCES billing_tax_jurisdictions(jurisdiction_id),
    pct_e6            INTEGER     NOT NULL,
    effective_from    TEXT        NOT NULL,
    effective_to      TEXT        NULL
);

CREATE TABLE billing_usage_meters (
    meter_id      INTEGER     PRIMARY KEY,
    code          TEXT        NOT NULL UNIQUE,
    display_name  TEXT        NOT NULL,
    unit          TEXT        NOT NULL,
    aggregation   TEXT        NOT NULL
);

CREATE TABLE billing_plans (
    plan_id          INTEGER     PRIMARY KEY,
    code             TEXT        NOT NULL UNIQUE,
    display_name     TEXT        NOT NULL,
    billing_interval TEXT        NOT NULL,
    base_price_cents INTEGER     NOT NULL,
    currency_code    TEXT        NOT NULL REFERENCES billing_currencies(currency_code),
    is_active        INTEGER     NOT NULL,
    created_at       TEXT        NOT NULL
);

CREATE TABLE billing_features (
    feature_id    INTEGER     PRIMARY KEY,
    code          TEXT        NOT NULL UNIQUE,
    display_name  TEXT        NOT NULL,
    category      TEXT        NOT NULL
);

CREATE TABLE "billing_PlanFeatures" (
    plan_id       INTEGER     NOT NULL REFERENCES billing_plans(plan_id),
    feature_id    INTEGER     NOT NULL REFERENCES billing_features(feature_id),
    quota         INTEGER     NULL,
    PRIMARY KEY (plan_id, feature_id)
);

CREATE TABLE billing_usage_prices (
    usage_price_id    INTEGER     PRIMARY KEY,
    plan_id           INTEGER     NOT NULL REFERENCES billing_plans(plan_id),
    meter_id          INTEGER     NOT NULL REFERENCES billing_usage_meters(meter_id),
    unit_price_e4     INTEGER     NOT NULL,
    included_units    INTEGER     NOT NULL,
    effective_from    TEXT        NOT NULL,
    effective_to      TEXT        NULL
);

-- ===========================================================================
-- billing: tenants, users, roles
-- ===========================================================================

CREATE TABLE billing_tenants (
    tenant_id          INTEGER     PRIMARY KEY,
    name               TEXT        NOT NULL,
    slug               TEXT        NOT NULL UNIQUE,
    billing_timezone   TEXT        NOT NULL,
    currency_code      TEXT        NOT NULL REFERENCES billing_currencies(currency_code),
    created_at         TEXT        NOT NULL,
    deleted_at         TEXT        NULL
);

CREATE TABLE billing_users (
    user_id       INTEGER     PRIMARY KEY,
    email         TEXT        NOT NULL,
    display_name  TEXT        NOT NULL,
    created_at    TEXT        NOT NULL,
    deleted_at    TEXT        NULL
);

CREATE TABLE billing_roles (
    role_id       INTEGER     PRIMARY KEY,
    name          TEXT        NOT NULL UNIQUE,
    is_system     INTEGER     NOT NULL,
    permissions   TEXT        NOT NULL
);

CREATE TABLE billing_memberships (
    user_id       INTEGER     NOT NULL REFERENCES billing_users(user_id),
    tenant_id     INTEGER     NOT NULL REFERENCES billing_tenants(tenant_id),
    role_id       INTEGER     NOT NULL REFERENCES billing_roles(role_id),
    joined_at     TEXT        NOT NULL,
    left_at       TEXT        NULL,
    PRIMARY KEY (user_id, tenant_id)
);

CREATE TABLE billing_tenant_settings (
    tenant_id       INTEGER     PRIMARY KEY REFERENCES billing_tenants(tenant_id),
    settings        TEXT        NOT NULL,
    updated_at      TEXT        NOT NULL
);

CREATE TABLE billing_billing_contacts (
    contact_id     INTEGER     PRIMARY KEY,
    tenant_id      INTEGER     NOT NULL REFERENCES billing_tenants(tenant_id),
    name           TEXT        NOT NULL,
    email          TEXT        NOT NULL,
    is_primary     INTEGER     NOT NULL,
    created_at     TEXT        NOT NULL
);

CREATE TABLE billing_addresses (
    address_id        INTEGER     NOT NULL,
    tenant_id         INTEGER     NOT NULL REFERENCES billing_tenants(tenant_id),
    line1             TEXT        NOT NULL,
    line2             TEXT        NULL,
    city              TEXT        NOT NULL,
    region            TEXT        NOT NULL,
    postcode          TEXT        NOT NULL,
    country_code      TEXT        NOT NULL,
    jurisdiction_id   INTEGER     NOT NULL REFERENCES billing_tax_jurisdictions(jurisdiction_id),
    effective_from    TEXT        NOT NULL,
    effective_to      TEXT        NULL,
    PRIMARY KEY (address_id, effective_from)
);

-- ===========================================================================
-- billing: subscriptions and invoices
-- ===========================================================================

CREATE TABLE billing_subscriptions (
    subscription_id   INTEGER     PRIMARY KEY,
    tenant_id         INTEGER     NOT NULL REFERENCES billing_tenants(tenant_id),
    plan_id           INTEGER     NOT NULL REFERENCES billing_plans(plan_id),
    started_at        TEXT        NOT NULL,
    ended_at          TEXT        NULL,
    status            TEXT        NOT NULL,
    created_at        TEXT        NOT NULL,
    deleted_at        TEXT        NULL
);

CREATE TABLE billing_subscription_items (
    subscription_id   INTEGER     NOT NULL REFERENCES billing_subscriptions(subscription_id),
    item_seq          INTEGER     NOT NULL,
    meter_id          INTEGER     NULL REFERENCES billing_usage_meters(meter_id),
    quantity          INTEGER     NOT NULL,
    unit_price_e4     INTEGER     NOT NULL,
    created_at        TEXT        NOT NULL,
    PRIMARY KEY (subscription_id, item_seq)
);

CREATE TABLE billing_invoices (
    invoice_id         INTEGER     PRIMARY KEY,
    tenant_id          INTEGER     NOT NULL REFERENCES billing_tenants(tenant_id),
    subscription_id    INTEGER     NULL REFERENCES billing_subscriptions(subscription_id),
    invoice_number     TEXT        NOT NULL,
    issued_at          TEXT        NOT NULL,
    period_start       TEXT        NOT NULL,
    period_end         TEXT        NOT NULL,
    currency_code      TEXT        NOT NULL REFERENCES billing_currencies(currency_code),
    subtotal_cents     INTEGER     NOT NULL,
    tax_cents          INTEGER     NOT NULL,
    total_cents        INTEGER     NOT NULL,
    status             TEXT        NOT NULL,
    "IsAutoBilled"     INTEGER     NOT NULL,
    due_date           TEXT        NOT NULL,
    created_at         TEXT        NOT NULL,
    deleted_at         TEXT        NULL
);

CREATE TABLE billing_invoice_line_items (
    line_item_id              INTEGER     PRIMARY KEY,
    invoice_id                INTEGER     NOT NULL REFERENCES billing_invoices(invoice_id),
    subscription_item_seq     INTEGER     NULL,
    subscription_id           INTEGER     NULL REFERENCES billing_subscriptions(subscription_id),
    description               TEXT        NOT NULL,
    quantity                  INTEGER     NOT NULL,
    unit_price_e4             INTEGER     NOT NULL,
    amount_cents              INTEGER     NOT NULL,
    tax_rate_id               INTEGER     NULL REFERENCES billing_tax_rates(tax_rate_id),
    coupon_redemption_id      INTEGER     NULL,
    created_at                TEXT        NOT NULL
);

CREATE TABLE billing_invoice_adjustments (
    adjustment_id     INTEGER     PRIMARY KEY,
    invoice_id        INTEGER     NOT NULL REFERENCES billing_invoices(invoice_id),
    kind              TEXT        NOT NULL,
    amount_cents      INTEGER     NOT NULL,
    reason            TEXT        NOT NULL,
    created_at        TEXT        NOT NULL
);

CREATE TABLE billing_payment_methods (
    payment_method_id  INTEGER     PRIMARY KEY,
    tenant_id          INTEGER     NOT NULL REFERENCES billing_tenants(tenant_id),
    kind               TEXT        NOT NULL,
    brand              TEXT        NULL,
    last4              TEXT        NULL,
    is_default         INTEGER     NOT NULL,
    created_at         TEXT        NOT NULL,
    deleted_at         TEXT        NULL
);

CREATE TABLE billing_payments (
    payment_id         INTEGER     PRIMARY KEY,
    tenant_id          INTEGER     NOT NULL REFERENCES billing_tenants(tenant_id),
    invoice_id         INTEGER     NULL REFERENCES billing_invoices(invoice_id),
    payment_method_id  INTEGER     NULL REFERENCES billing_payment_methods(payment_method_id),
    amount_cents       INTEGER     NOT NULL,
    currency_code      TEXT        NOT NULL REFERENCES billing_currencies(currency_code),
    status             TEXT        NOT NULL,
    paid_at            TEXT        NULL,
    created_at         TEXT        NOT NULL
);

CREATE TABLE billing_refunds (
    refund_id       INTEGER     PRIMARY KEY,
    payment_id      INTEGER     NULL REFERENCES billing_payments(payment_id),
    invoice_id      INTEGER     NULL REFERENCES billing_invoices(invoice_id),
    tenant_id       INTEGER     NOT NULL REFERENCES billing_tenants(tenant_id),
    amount_cents    INTEGER     NOT NULL,
    reason          TEXT        NOT NULL,
    created_at      TEXT        NOT NULL
);

CREATE TABLE billing_credits (
    credit_id       INTEGER     PRIMARY KEY,
    tenant_id       INTEGER     NOT NULL REFERENCES billing_tenants(tenant_id),
    subscription_id INTEGER     NULL REFERENCES billing_subscriptions(subscription_id),
    kind            TEXT        NOT NULL,
    amount_cents    INTEGER     NOT NULL,
    currency_code   TEXT        NOT NULL REFERENCES billing_currencies(currency_code),
    expires_at      TEXT        NULL,
    created_at      TEXT        NOT NULL
);

CREATE TABLE billing_credit_transactions (
    credit_tx_id    INTEGER     PRIMARY KEY,
    credit_id       INTEGER     NOT NULL REFERENCES billing_credits(credit_id),
    invoice_id      INTEGER     NULL REFERENCES billing_invoices(invoice_id),
    delta_cents     INTEGER     NOT NULL,
    created_at      TEXT        NOT NULL
);

CREATE TABLE billing_coupons (
    coupon_id       INTEGER     PRIMARY KEY,
    code            TEXT        NOT NULL UNIQUE,
    discount_pct_e6 INTEGER     NULL,
    discount_cents  INTEGER     NULL,
    valid_from      TEXT        NOT NULL,
    valid_to        TEXT        NULL,
    max_redemptions INTEGER     NULL,
    created_at      TEXT        NOT NULL
);

CREATE TABLE billing_coupon_redemptions (
    coupon_id       INTEGER     NOT NULL REFERENCES billing_coupons(coupon_id),
    invoice_id      INTEGER     NOT NULL REFERENCES billing_invoices(invoice_id),
    seq             INTEGER     NOT NULL,
    amount_cents    INTEGER     NOT NULL,
    redeemed_at     TEXT        NOT NULL,
    PRIMARY KEY (coupon_id, invoice_id, seq)
);

CREATE TABLE billing_promotions (
    promotion_id    INTEGER     PRIMARY KEY,
    name            TEXT        NOT NULL,
    starts_on       TEXT        NOT NULL,
    ends_on         TEXT        NULL,
    budget_cents    INTEGER     NULL,
    created_at      TEXT        NOT NULL
);

CREATE TABLE billing_promotion_applications (
    promotion_id    INTEGER     NOT NULL REFERENCES billing_promotions(promotion_id),
    invoice_id      INTEGER     NOT NULL REFERENCES billing_invoices(invoice_id),
    amount_cents    INTEGER     NOT NULL,
    applied_at      TEXT        NOT NULL,
    PRIMARY KEY (promotion_id, invoice_id)
);

-- SQLite has no partitioning. Plain table; gold ignores partition semantics.
CREATE TABLE billing_usage_events (
    event_id        INTEGER     NOT NULL,
    tenant_id       INTEGER     NOT NULL,
    subscription_id INTEGER     NULL,
    meter_id        INTEGER     NOT NULL,
    event_at        TEXT        NOT NULL,
    quantity        INTEGER     NOT NULL,
    payload         TEXT        NULL,
    PRIMARY KEY (event_id, event_at)
);

-- ===========================================================================
-- audit schema (prefixed audit_<table>)
-- ===========================================================================

CREATE TABLE audit_audit_log (
    audit_id        INTEGER     PRIMARY KEY,
    tenant_id       INTEGER     NULL,
    actor_user_id   INTEGER     NULL,
    entity_kind     TEXT        NOT NULL,
    entity_id       INTEGER     NOT NULL,
    action          TEXT        NOT NULL,
    "OldState"      TEXT        NULL,
    "NewState"      TEXT        NULL,
    occurred_at     TEXT        NOT NULL
);

CREATE TABLE audit_disputes (
    dispute_id      INTEGER     PRIMARY KEY,
    tenant_id       INTEGER     NOT NULL,
    invoice_id      INTEGER     NULL,
    payment_id      INTEGER     NULL,
    status          TEXT        NOT NULL,
    reason          TEXT        NOT NULL,
    evidence        TEXT        NULL,
    opened_at       TEXT        NOT NULL,
    resolved_at     TEXT        NULL
);

CREATE TABLE audit_dispute_events (
    dispute_event_id  INTEGER     PRIMARY KEY,
    dispute_id        INTEGER     NOT NULL REFERENCES audit_disputes(dispute_id),
    event_kind        TEXT        NOT NULL,
    note              TEXT        NULL,
    occurred_at       TEXT        NOT NULL
);

CREATE TABLE audit_email_events (
    email_event_id    INTEGER     PRIMARY KEY,
    tenant_id         INTEGER     NULL,
    to_address        TEXT        NOT NULL,
    template          TEXT        NOT NULL,
    status            TEXT        NOT NULL,
    sent_at           TEXT        NOT NULL
);

CREATE TABLE audit_notifications (
    notification_id   INTEGER     PRIMARY KEY,
    tenant_id         INTEGER     NOT NULL,
    user_id           INTEGER     NULL,
    invoice_id        INTEGER     NULL,
    channel           TEXT        NOT NULL,
    status            TEXT        NOT NULL,
    created_at        TEXT        NOT NULL,
    read_at           TEXT        NULL
);

CREATE TABLE audit_webhook_endpoints (
    endpoint_id    INTEGER     PRIMARY KEY,
    tenant_id      INTEGER     NOT NULL,
    url            TEXT        NOT NULL,
    secret         TEXT        NOT NULL,
    is_active      INTEGER     NOT NULL,
    created_at     TEXT        NOT NULL
);

CREATE TABLE audit_webhook_deliveries (
    delivery_id    INTEGER     PRIMARY KEY,
    endpoint_id    INTEGER     NOT NULL REFERENCES audit_webhook_endpoints(endpoint_id),
    event_kind     TEXT        NOT NULL,
    status         TEXT        NOT NULL,
    attempt        INTEGER     NOT NULL,
    body           TEXT        NOT NULL,
    response_code  INTEGER     NULL,
    delivered_at   TEXT        NOT NULL
);

CREATE TABLE audit_api_keys (
    api_key_id     INTEGER     PRIMARY KEY,
    tenant_id      INTEGER     NOT NULL,
    label          TEXT        NOT NULL,
    created_at     TEXT        NOT NULL,
    revoked_at     TEXT        NULL
);

CREATE TABLE audit_api_requests (
    api_request_id INTEGER     PRIMARY KEY,
    tenant_id      INTEGER     NULL,
    api_key_id     INTEGER     NULL REFERENCES audit_api_keys(api_key_id),
    method         TEXT        NOT NULL,
    path           TEXT        NOT NULL,
    status_code    INTEGER     NOT NULL,
    latency_ms     INTEGER     NOT NULL,
    occurred_at    TEXT        NOT NULL
);

CREATE TABLE audit_feature_flag_evaluations (
    eval_id        INTEGER     PRIMARY KEY,
    tenant_id      INTEGER     NOT NULL,
    user_id        INTEGER     NULL,
    flag_key       TEXT        NOT NULL,
    variant        TEXT        NOT NULL,
    evaluated_at   TEXT        NOT NULL
);

CREATE TABLE audit_background_jobs (
    job_id         INTEGER     PRIMARY KEY,
    tenant_id      INTEGER     NULL,
    kind           TEXT        NOT NULL,
    status         TEXT        NOT NULL,
    enqueued_at    TEXT        NOT NULL,
    started_at     TEXT        NULL,
    finished_at    TEXT        NULL,
    error          TEXT        NULL
);

-- ===========================================================================
-- Views
-- ===========================================================================

CREATE VIEW billing_active_subscriptions_v AS
SELECT s.subscription_id,
       s.tenant_id,
       s.plan_id,
       s.started_at,
       s.ended_at,
       s.status
  FROM billing_subscriptions s
 WHERE s.deleted_at IS NULL
   AND s.status IN ('active', 'past_due')
   AND (s.ended_at IS NULL OR s.ended_at > '2026-06-30T00:00:00+00:00');

CREATE VIEW billing_current_addresses_v AS
SELECT a.address_id,
       a.tenant_id,
       a.line1,
       a.line2,
       a.city,
       a.region,
       a.postcode,
       a.country_code,
       a.jurisdiction_id,
       a.effective_from
  FROM billing_addresses a
 WHERE a.effective_to IS NULL;

CREATE VIEW billing_current_tax_rates_v AS
SELECT t.tax_rate_id,
       t.jurisdiction_id,
       t.pct_e6,
       t.effective_from
  FROM billing_tax_rates t
 WHERE t.effective_to IS NULL;

CREATE VIEW billing_tenant_billing_details_v AS
SELECT ca.tenant_id,
       ca.address_id,
       ca.city,
       ca.region,
       ca.country_code,
       ca.jurisdiction_id,
       ctr.tax_rate_id,
       ctr.pct_e6
  FROM billing_current_addresses_v ca
  LEFT JOIN billing_current_tax_rates_v ctr
    ON ctr.jurisdiction_id = ca.jurisdiction_id;

CREATE VIEW billing_invoice_totals_v AS
SELECT i.invoice_id,
       i.tenant_id,
       i.currency_code,
       i.subtotal_cents,
       i.tax_cents,
       i.total_cents,
       COALESCE(SUM(li.amount_cents), 0) AS line_items_sum_cents,
       COUNT(li.line_item_id)            AS line_items_count
  FROM billing_invoices i
  LEFT JOIN billing_invoice_line_items li
    ON li.invoice_id = i.invoice_id
 WHERE i.deleted_at IS NULL
 GROUP BY i.invoice_id, i.tenant_id, i.currency_code, i.subtotal_cents, i.tax_cents, i.total_cents;

CREATE VIEW billing_tenant_mrr_v AS
SELECT s.tenant_id,
       s.subscription_id,
       p.plan_id,
       p.base_price_cents AS mrr_cents,
       p.currency_code
  FROM billing_active_subscriptions_v s
  JOIN billing_plans p ON p.plan_id = s.plan_id
 WHERE p.billing_interval = 'month';

CREATE VIEW billing_mrr_by_month_v AS
SELECT substr(s.started_at, 1, 7) || '-01' AS month_start,
       t.tenant_id,
       SUM(m.mrr_cents) AS tenant_mrr_cents
  FROM billing_tenant_mrr_v m
  JOIN billing_subscriptions s ON s.subscription_id = m.subscription_id
  JOIN billing_tenants t       ON t.tenant_id = m.tenant_id
 WHERE t.deleted_at IS NULL
 GROUP BY substr(s.started_at, 1, 7), t.tenant_id;

CREATE VIEW billing_at_risk_accounts_v AS
SELECT t.tenant_id,
       t.name,
       COALESCE(d.open_disputes, 0) AS open_disputes,
       COALESCE(i.overdue_cents,  0) AS overdue_cents
  FROM billing_tenants t
  LEFT JOIN (SELECT tenant_id, COUNT(*) AS open_disputes
               FROM audit_disputes
              WHERE status = 'open'
              GROUP BY tenant_id) d ON d.tenant_id = t.tenant_id
  LEFT JOIN (SELECT tenant_id, SUM(total_cents) AS overdue_cents
               FROM billing_invoices
              WHERE status = 'overdue' AND deleted_at IS NULL
              GROUP BY tenant_id) i ON i.tenant_id = t.tenant_id
 WHERE t.deleted_at IS NULL;

CREATE VIEW billing_credit_balance_v AS
SELECT c.credit_id,
       c.tenant_id,
       c.currency_code,
       c.amount_cents + COALESCE(SUM(tx.delta_cents), 0) AS balance_cents,
       c.expires_at
  FROM billing_credits c
  LEFT JOIN billing_credit_transactions tx ON tx.credit_id = c.credit_id
 GROUP BY c.credit_id, c.tenant_id, c.currency_code, c.amount_cents, c.expires_at;

CREATE VIEW billing_usage_this_month_v AS
SELECT ue.tenant_id,
       ue.meter_id,
       SUM(ue.quantity) AS units
  FROM billing_usage_events ue
 WHERE ue.event_at >= '2026-06-01T00:00:00+00:00'
   AND ue.event_at <  '2026-07-01T00:00:00+00:00'
 GROUP BY ue.tenant_id, ue.meter_id;

CREATE VIEW audit_recent_webhook_failures_v AS
SELECT wd.delivery_id,
       we.tenant_id,
       we.endpoint_id,
       wd.event_kind,
       wd.response_code,
       wd.delivered_at
  FROM audit_webhook_deliveries wd
  JOIN audit_webhook_endpoints we ON we.endpoint_id = wd.endpoint_id
 WHERE wd.status = 'failed'
   AND wd.delivered_at >= '2026-06-01T00:00:00+00:00';

CREATE VIEW audit_open_disputes_v AS
SELECT d.dispute_id,
       d.tenant_id,
       d.invoice_id,
       d.reason,
       d.opened_at,
       COUNT(de.dispute_event_id) AS event_count
  FROM audit_disputes d
  LEFT JOIN audit_dispute_events de ON de.dispute_id = d.dispute_id
 WHERE d.status = 'open'
 GROUP BY d.dispute_id, d.tenant_id, d.invoice_id, d.reason, d.opened_at;

-- SQLite has no materialized view; gold for materialized_view-tagged
-- questions is null on sqlite.

CREATE INDEX idx_invoices_tenant_issued  ON billing_invoices(tenant_id, issued_at);
CREATE INDEX idx_subs_tenant             ON billing_subscriptions(tenant_id);
CREATE INDEX idx_line_items_invoice      ON billing_invoice_line_items(invoice_id);
CREATE INDEX idx_usage_events_tenant     ON billing_usage_events(tenant_id, event_at);
CREATE INDEX idx_audit_log_entity        ON audit_audit_log(entity_kind, entity_id);
