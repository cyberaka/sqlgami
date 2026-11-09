-- saas_billing — PostgreSQL 16
-- DDL only. No data. Loader fills tables from generate.py output.
-- Session TimeZone pinned to UTC by the loader; gold SQL assumes that.

CREATE SCHEMA billing;
CREATE SCHEMA audit;

-- ===========================================================================
-- billing: reference / small catalogues
-- ===========================================================================

CREATE TABLE billing.currencies (
    currency_code      TEXT        PRIMARY KEY,
    name               TEXT        NOT NULL,
    minor_unit_scale   INTEGER     NOT NULL
);

CREATE TABLE billing.exchange_rates (
    from_ccy      TEXT        NOT NULL REFERENCES billing.currencies(currency_code),
    to_ccy        TEXT        NOT NULL REFERENCES billing.currencies(currency_code),
    rate_date     DATE        NOT NULL,
    rate_e8       BIGINT      NOT NULL,
    source        TEXT        NOT NULL,
    PRIMARY KEY (from_ccy, to_ccy, rate_date)
);

CREATE TABLE billing.tax_jurisdictions (
    jurisdiction_id   BIGINT      PRIMARY KEY,
    name              TEXT        NOT NULL,
    code              TEXT        NOT NULL,
    parent_id         BIGINT      NULL REFERENCES billing.tax_jurisdictions(jurisdiction_id),
    level             TEXT        NOT NULL
);

CREATE TABLE billing.tax_rates (
    tax_rate_id       BIGINT      PRIMARY KEY,
    jurisdiction_id   BIGINT      NOT NULL REFERENCES billing.tax_jurisdictions(jurisdiction_id),
    pct_e6            BIGINT      NOT NULL,
    effective_from    DATE        NOT NULL,
    effective_to      DATE        NULL
);

CREATE TABLE billing.usage_meters (
    meter_id      BIGINT      PRIMARY KEY,
    code          TEXT        NOT NULL UNIQUE,
    display_name  TEXT        NOT NULL,
    unit          TEXT        NOT NULL,
    aggregation   TEXT        NOT NULL
);

CREATE TABLE billing.plans (
    plan_id         BIGINT      PRIMARY KEY,
    code            TEXT        NOT NULL UNIQUE,
    display_name    TEXT        NOT NULL,
    billing_interval TEXT       NOT NULL,
    base_price_cents BIGINT     NOT NULL,
    currency_code   TEXT        NOT NULL REFERENCES billing.currencies(currency_code),
    is_active       BOOLEAN     NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE billing.features (
    feature_id    BIGINT      PRIMARY KEY,
    code          TEXT        NOT NULL UNIQUE,
    display_name  TEXT        NOT NULL,
    category      TEXT        NOT NULL
);

-- Quoted mixed-case table, PlanFeatures: plan ↔ feature M2M with optional quota
CREATE TABLE billing."PlanFeatures" (
    plan_id       BIGINT      NOT NULL REFERENCES billing.plans(plan_id),
    feature_id    BIGINT      NOT NULL REFERENCES billing.features(feature_id),
    quota         BIGINT      NULL,
    PRIMARY KEY (plan_id, feature_id)
);

CREATE TABLE billing.usage_prices (
    usage_price_id    BIGINT      PRIMARY KEY,
    plan_id           BIGINT      NOT NULL REFERENCES billing.plans(plan_id),
    meter_id          BIGINT      NOT NULL REFERENCES billing.usage_meters(meter_id),
    unit_price_e4     BIGINT      NOT NULL,
    included_units    BIGINT      NOT NULL,
    effective_from    DATE        NOT NULL,
    effective_to      DATE        NULL
);

-- ===========================================================================
-- billing: tenants, users, roles
-- ===========================================================================

CREATE TABLE billing.tenants (
    tenant_id          BIGINT      PRIMARY KEY,
    name               TEXT        NOT NULL,
    slug               TEXT        NOT NULL UNIQUE,
    billing_timezone   TEXT        NOT NULL,
    currency_code      TEXT        NOT NULL REFERENCES billing.currencies(currency_code),
    created_at         TIMESTAMPTZ NOT NULL,
    deleted_at         TIMESTAMPTZ NULL
);

CREATE TABLE billing.users (
    user_id       BIGINT      PRIMARY KEY,
    email         TEXT        NOT NULL,
    display_name  TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL,
    deleted_at    TIMESTAMPTZ NULL
);

CREATE TABLE billing.roles (
    role_id       BIGINT      PRIMARY KEY,
    name          TEXT        NOT NULL UNIQUE,
    is_system     BOOLEAN     NOT NULL,
    permissions   JSONB       NOT NULL
);

CREATE TABLE billing.memberships (
    user_id       BIGINT      NOT NULL REFERENCES billing.users(user_id),
    tenant_id     BIGINT      NOT NULL REFERENCES billing.tenants(tenant_id),
    role_id       BIGINT      NOT NULL REFERENCES billing.roles(role_id),
    joined_at     TIMESTAMPTZ NOT NULL,
    left_at       TIMESTAMPTZ NULL,
    PRIMARY KEY (user_id, tenant_id)
);

CREATE TABLE billing.tenant_settings (
    tenant_id       BIGINT      PRIMARY KEY REFERENCES billing.tenants(tenant_id),
    settings        JSONB       NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE billing.billing_contacts (
    contact_id     BIGINT      PRIMARY KEY,
    tenant_id      BIGINT      NOT NULL REFERENCES billing.tenants(tenant_id),
    name           TEXT        NOT NULL,
    email          TEXT        NOT NULL,
    is_primary     BOOLEAN     NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL
);

CREATE TABLE billing.addresses (
    address_id        BIGINT      NOT NULL,
    tenant_id         BIGINT      NOT NULL REFERENCES billing.tenants(tenant_id),
    line1             TEXT        NOT NULL,
    line2             TEXT        NULL,
    city              TEXT        NOT NULL,
    region            TEXT        NOT NULL,
    postcode          TEXT        NOT NULL,
    country_code      TEXT        NOT NULL,
    jurisdiction_id   BIGINT      NOT NULL REFERENCES billing.tax_jurisdictions(jurisdiction_id),
    effective_from    DATE        NOT NULL,
    effective_to      DATE        NULL,
    PRIMARY KEY (address_id, effective_from)
);

-- ===========================================================================
-- billing: subscriptions and invoices
-- ===========================================================================

CREATE TABLE billing.subscriptions (
    subscription_id   BIGINT      PRIMARY KEY,
    tenant_id         BIGINT      NOT NULL REFERENCES billing.tenants(tenant_id),
    plan_id           BIGINT      NOT NULL REFERENCES billing.plans(plan_id),
    started_at        TIMESTAMPTZ NOT NULL,
    ended_at          TIMESTAMPTZ NULL,
    status            TEXT        NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    deleted_at        TIMESTAMPTZ NULL
);

CREATE TABLE billing.subscription_items (
    subscription_id   BIGINT      NOT NULL REFERENCES billing.subscriptions(subscription_id),
    item_seq          INTEGER     NOT NULL,
    meter_id          BIGINT      NULL REFERENCES billing.usage_meters(meter_id),
    quantity          BIGINT      NOT NULL,
    unit_price_e4     BIGINT      NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (subscription_id, item_seq)
);

CREATE TABLE billing.invoices (
    invoice_id         BIGINT      PRIMARY KEY,
    tenant_id          BIGINT      NOT NULL REFERENCES billing.tenants(tenant_id),
    subscription_id    BIGINT      NULL REFERENCES billing.subscriptions(subscription_id),
    invoice_number     TEXT        NOT NULL,
    issued_at          TIMESTAMPTZ NOT NULL,
    period_start       DATE        NOT NULL,
    period_end         DATE        NOT NULL,
    currency_code      TEXT        NOT NULL REFERENCES billing.currencies(currency_code),
    subtotal_cents     BIGINT      NOT NULL,
    tax_cents          BIGINT      NOT NULL,
    total_cents        BIGINT      NOT NULL,
    status             TEXT        NOT NULL,
    "IsAutoBilled"     BOOLEAN     NOT NULL,
    due_date           DATE        NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL,
    deleted_at         TIMESTAMPTZ NULL
);

CREATE TABLE billing.invoice_line_items (
    line_item_id              BIGINT      PRIMARY KEY,
    invoice_id                BIGINT      NOT NULL REFERENCES billing.invoices(invoice_id),
    subscription_item_seq     INTEGER     NULL,
    subscription_id           BIGINT      NULL REFERENCES billing.subscriptions(subscription_id),
    description               TEXT        NOT NULL,
    quantity                  BIGINT      NOT NULL,
    unit_price_e4             BIGINT      NOT NULL,
    amount_cents              BIGINT      NOT NULL,
    tax_rate_id               BIGINT      NULL REFERENCES billing.tax_rates(tax_rate_id),
    coupon_redemption_id      BIGINT      NULL,
    created_at                TIMESTAMPTZ NOT NULL
);

CREATE TABLE billing.invoice_adjustments (
    adjustment_id     BIGINT      PRIMARY KEY,
    invoice_id        BIGINT      NOT NULL REFERENCES billing.invoices(invoice_id),
    kind              TEXT        NOT NULL,
    amount_cents      BIGINT      NOT NULL,
    reason            TEXT        NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL
);

CREATE TABLE billing.payment_methods (
    payment_method_id  BIGINT      PRIMARY KEY,
    tenant_id          BIGINT      NOT NULL REFERENCES billing.tenants(tenant_id),
    kind               TEXT        NOT NULL,
    brand              TEXT        NULL,
    last4              TEXT        NULL,
    is_default         BOOLEAN     NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL,
    deleted_at         TIMESTAMPTZ NULL
);

CREATE TABLE billing.payments (
    payment_id         BIGINT      PRIMARY KEY,
    tenant_id          BIGINT      NOT NULL REFERENCES billing.tenants(tenant_id),
    invoice_id         BIGINT      NULL REFERENCES billing.invoices(invoice_id),
    payment_method_id  BIGINT      NULL REFERENCES billing.payment_methods(payment_method_id),
    amount_cents       BIGINT      NOT NULL,
    currency_code      TEXT        NOT NULL REFERENCES billing.currencies(currency_code),
    status             TEXT        NOT NULL,
    paid_at            TIMESTAMPTZ NULL,
    created_at         TIMESTAMPTZ NOT NULL
);

CREATE TABLE billing.refunds (
    refund_id       BIGINT      PRIMARY KEY,
    payment_id      BIGINT      NULL REFERENCES billing.payments(payment_id),
    invoice_id      BIGINT      NULL REFERENCES billing.invoices(invoice_id),
    tenant_id       BIGINT      NOT NULL REFERENCES billing.tenants(tenant_id),
    amount_cents    BIGINT      NOT NULL,
    reason          TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE billing.credits (
    credit_id       BIGINT      PRIMARY KEY,
    tenant_id       BIGINT      NOT NULL REFERENCES billing.tenants(tenant_id),
    subscription_id BIGINT      NULL REFERENCES billing.subscriptions(subscription_id),
    kind            TEXT        NOT NULL,
    amount_cents    BIGINT      NOT NULL,
    currency_code   TEXT        NOT NULL REFERENCES billing.currencies(currency_code),
    expires_at      TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE billing.credit_transactions (
    credit_tx_id    BIGINT      PRIMARY KEY,
    credit_id       BIGINT      NOT NULL REFERENCES billing.credits(credit_id),
    invoice_id      BIGINT      NULL REFERENCES billing.invoices(invoice_id),
    delta_cents     BIGINT      NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE billing.coupons (
    coupon_id       BIGINT      PRIMARY KEY,
    code            TEXT        NOT NULL UNIQUE,
    discount_pct_e6 BIGINT      NULL,
    discount_cents  BIGINT      NULL,
    valid_from      DATE        NOT NULL,
    valid_to        DATE        NULL,
    max_redemptions INTEGER     NULL,
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE billing.coupon_redemptions (
    coupon_id       BIGINT      NOT NULL REFERENCES billing.coupons(coupon_id),
    invoice_id      BIGINT      NOT NULL REFERENCES billing.invoices(invoice_id),
    seq             INTEGER     NOT NULL,
    amount_cents    BIGINT      NOT NULL,
    redeemed_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (coupon_id, invoice_id, seq)
);

CREATE TABLE billing.promotions (
    promotion_id    BIGINT      PRIMARY KEY,
    name            TEXT        NOT NULL,
    starts_on       DATE        NOT NULL,
    ends_on         DATE        NULL,
    budget_cents    BIGINT      NULL,
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE billing.promotion_applications (
    promotion_id    BIGINT      NOT NULL REFERENCES billing.promotions(promotion_id),
    invoice_id      BIGINT      NOT NULL REFERENCES billing.invoices(invoice_id),
    amount_cents    BIGINT      NOT NULL,
    applied_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (promotion_id, invoice_id)
);

-- ===========================================================================
-- billing: usage_events, partitioned (Postgres only; DuckDB/SQLite use plain)
-- ===========================================================================

CREATE TABLE billing.usage_events (
    event_id        BIGINT      NOT NULL,
    tenant_id       BIGINT      NOT NULL,
    subscription_id BIGINT      NULL,
    meter_id        BIGINT      NOT NULL,
    event_at        TIMESTAMPTZ NOT NULL,
    quantity        BIGINT      NOT NULL,
    payload         JSONB       NULL,
    PRIMARY KEY (event_id, event_at)
) PARTITION BY RANGE (event_at);

CREATE TABLE billing.usage_events_2026_01 PARTITION OF billing.usage_events
    FOR VALUES FROM ('2026-01-01 00:00:00+00') TO ('2026-02-01 00:00:00+00');
CREATE TABLE billing.usage_events_2026_02 PARTITION OF billing.usage_events
    FOR VALUES FROM ('2026-02-01 00:00:00+00') TO ('2026-03-01 00:00:00+00');
CREATE TABLE billing.usage_events_2026_03 PARTITION OF billing.usage_events
    FOR VALUES FROM ('2026-03-01 00:00:00+00') TO ('2026-04-01 00:00:00+00');
CREATE TABLE billing.usage_events_2026_04 PARTITION OF billing.usage_events
    FOR VALUES FROM ('2026-04-01 00:00:00+00') TO ('2026-05-01 00:00:00+00');
CREATE TABLE billing.usage_events_2026_05 PARTITION OF billing.usage_events
    FOR VALUES FROM ('2026-05-01 00:00:00+00') TO ('2026-06-01 00:00:00+00');
CREATE TABLE billing.usage_events_2026_06 PARTITION OF billing.usage_events
    FOR VALUES FROM ('2026-06-01 00:00:00+00') TO ('2026-07-01 00:00:00+00');

-- ===========================================================================
-- audit schema
-- ===========================================================================

CREATE TABLE audit.audit_log (
    audit_id        BIGINT      PRIMARY KEY,
    tenant_id       BIGINT      NULL,
    actor_user_id   BIGINT      NULL,
    entity_kind     TEXT        NOT NULL,
    entity_id       BIGINT      NOT NULL,
    action          TEXT        NOT NULL,
    "OldState"      JSONB       NULL,
    "NewState"      JSONB       NULL,
    occurred_at     TIMESTAMPTZ NOT NULL
);

CREATE TABLE audit.disputes (
    dispute_id      BIGINT      PRIMARY KEY,
    tenant_id       BIGINT      NOT NULL,
    invoice_id      BIGINT      NULL,
    payment_id      BIGINT      NULL,
    status          TEXT        NOT NULL,
    reason          TEXT        NOT NULL,
    evidence        JSONB       NULL,
    opened_at       TIMESTAMPTZ NOT NULL,
    resolved_at     TIMESTAMPTZ NULL
);

CREATE TABLE audit.dispute_events (
    dispute_event_id  BIGINT      PRIMARY KEY,
    dispute_id        BIGINT      NOT NULL REFERENCES audit.disputes(dispute_id),
    event_kind        TEXT        NOT NULL,
    note              TEXT        NULL,
    occurred_at       TIMESTAMPTZ NOT NULL
);

CREATE TABLE audit.email_events (
    email_event_id    BIGINT      PRIMARY KEY,
    tenant_id         BIGINT      NULL,
    to_address        TEXT        NOT NULL,
    template          TEXT        NOT NULL,
    status            TEXT        NOT NULL,
    sent_at           TIMESTAMPTZ NOT NULL
);

CREATE TABLE audit.notifications (
    notification_id   BIGINT      PRIMARY KEY,
    tenant_id         BIGINT      NOT NULL,
    user_id           BIGINT      NULL,
    invoice_id        BIGINT      NULL,
    channel           TEXT        NOT NULL,
    status            TEXT        NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    read_at           TIMESTAMPTZ NULL
);

CREATE TABLE audit.webhook_endpoints (
    endpoint_id    BIGINT      PRIMARY KEY,
    tenant_id      BIGINT      NOT NULL,
    url            TEXT        NOT NULL,
    secret         TEXT        NOT NULL,
    is_active      BOOLEAN     NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL
);

CREATE TABLE audit.webhook_deliveries (
    delivery_id    BIGINT      PRIMARY KEY,
    endpoint_id    BIGINT      NOT NULL REFERENCES audit.webhook_endpoints(endpoint_id),
    event_kind     TEXT        NOT NULL,
    status         TEXT        NOT NULL,
    attempt        INTEGER     NOT NULL,
    body           JSONB       NOT NULL,
    response_code  INTEGER     NULL,
    delivered_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE audit.api_keys (
    api_key_id     BIGINT      PRIMARY KEY,
    tenant_id      BIGINT      NOT NULL,
    label          TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL,
    revoked_at     TIMESTAMPTZ NULL
);

CREATE TABLE audit.api_requests (
    api_request_id BIGINT      PRIMARY KEY,
    tenant_id      BIGINT      NULL,
    api_key_id     BIGINT      NULL REFERENCES audit.api_keys(api_key_id),
    method         TEXT        NOT NULL,
    path           TEXT        NOT NULL,
    status_code    INTEGER     NOT NULL,
    latency_ms     INTEGER     NOT NULL,
    occurred_at    TIMESTAMPTZ NOT NULL
);

CREATE TABLE audit.feature_flag_evaluations (
    eval_id        BIGINT      PRIMARY KEY,
    tenant_id      BIGINT      NOT NULL,
    user_id        BIGINT      NULL,
    flag_key       TEXT        NOT NULL,
    variant        TEXT        NOT NULL,
    evaluated_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE audit.background_jobs (
    job_id         BIGINT      PRIMARY KEY,
    tenant_id      BIGINT      NULL,
    kind           TEXT        NOT NULL,
    status         TEXT        NOT NULL,
    enqueued_at    TIMESTAMPTZ NOT NULL,
    started_at     TIMESTAMPTZ NULL,
    finished_at    TIMESTAMPTZ NULL,
    error          TEXT        NULL
);

-- ===========================================================================
-- Views (tables must come before their consumers)
-- ===========================================================================

CREATE VIEW billing.active_subscriptions_v AS
SELECT s.subscription_id,
       s.tenant_id,
       s.plan_id,
       s.started_at,
       s.ended_at,
       s.status
  FROM billing.subscriptions s
 WHERE s.deleted_at IS NULL
   AND s.status IN ('active', 'past_due')
   AND (s.ended_at IS NULL OR s.ended_at > TIMESTAMPTZ '2026-06-30 00:00:00+00');

CREATE VIEW billing.current_addresses_v AS
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
  FROM billing.addresses a
 WHERE a.effective_to IS NULL;

CREATE VIEW billing.current_tax_rates_v AS
SELECT t.tax_rate_id,
       t.jurisdiction_id,
       t.pct_e6,
       t.effective_from
  FROM billing.tax_rates t
 WHERE t.effective_to IS NULL;

CREATE VIEW billing.tenant_billing_details_v AS
SELECT ca.tenant_id,
       ca.address_id,
       ca.city,
       ca.region,
       ca.country_code,
       ca.jurisdiction_id,
       ctr.tax_rate_id,
       ctr.pct_e6
  FROM billing.current_addresses_v ca
  LEFT JOIN billing.current_tax_rates_v ctr
    ON ctr.jurisdiction_id = ca.jurisdiction_id;

CREATE VIEW billing.invoice_totals_v AS
SELECT i.invoice_id,
       i.tenant_id,
       i.currency_code,
       i.subtotal_cents,
       i.tax_cents,
       i.total_cents,
       COALESCE(SUM(li.amount_cents), 0) AS line_items_sum_cents,
       COUNT(li.line_item_id)            AS line_items_count
  FROM billing.invoices i
  LEFT JOIN billing.invoice_line_items li
    ON li.invoice_id = i.invoice_id
 WHERE i.deleted_at IS NULL
 GROUP BY i.invoice_id, i.tenant_id, i.currency_code, i.subtotal_cents, i.tax_cents, i.total_cents;

CREATE VIEW billing.tenant_mrr_v AS
SELECT s.tenant_id,
       s.subscription_id,
       p.plan_id,
       p.base_price_cents AS mrr_cents,
       p.currency_code
  FROM billing.active_subscriptions_v s
  JOIN billing.plans p ON p.plan_id = s.plan_id
 WHERE p.billing_interval = 'month';

CREATE VIEW billing.mrr_by_month_v AS
SELECT DATE_TRUNC('month', s.started_at)::date AS month_start,
       t.tenant_id,
       SUM(m.mrr_cents) AS tenant_mrr_cents
  FROM billing.tenant_mrr_v m
  JOIN billing.subscriptions s ON s.subscription_id = m.subscription_id
  JOIN billing.tenants t       ON t.tenant_id = m.tenant_id
 WHERE t.deleted_at IS NULL
 GROUP BY DATE_TRUNC('month', s.started_at), t.tenant_id;

CREATE VIEW billing.at_risk_accounts_v AS
SELECT t.tenant_id,
       t.name,
       COUNT(DISTINCT d.dispute_id)                 AS open_disputes,
       COALESCE(SUM(CASE WHEN i.status = 'overdue'
                         THEN i.total_cents ELSE 0 END), 0) AS overdue_cents
  FROM billing.tenants t
  LEFT JOIN audit.disputes d       ON d.tenant_id = t.tenant_id AND d.status = 'open'
  LEFT JOIN billing.invoices i      ON i.tenant_id = t.tenant_id AND i.deleted_at IS NULL
 WHERE t.deleted_at IS NULL
 GROUP BY t.tenant_id, t.name;

CREATE VIEW billing.credit_balance_v AS
SELECT c.credit_id,
       c.tenant_id,
       c.currency_code,
       c.amount_cents + COALESCE(SUM(tx.delta_cents), 0) AS balance_cents,
       c.expires_at
  FROM billing.credits c
  LEFT JOIN billing.credit_transactions tx ON tx.credit_id = c.credit_id
 GROUP BY c.credit_id, c.tenant_id, c.currency_code, c.amount_cents, c.expires_at;

CREATE VIEW billing.usage_this_month_v AS
SELECT ue.tenant_id,
       ue.meter_id,
       SUM(ue.quantity) AS units
  FROM billing.usage_events ue
 WHERE ue.event_at >= TIMESTAMPTZ '2026-06-01 00:00:00+00'
   AND ue.event_at <  TIMESTAMPTZ '2026-07-01 00:00:00+00'
 GROUP BY ue.tenant_id, ue.meter_id;

CREATE VIEW audit.recent_webhook_failures_v AS
SELECT wd.delivery_id,
       we.tenant_id,
       we.endpoint_id,
       wd.event_kind,
       wd.response_code,
       wd.delivered_at
  FROM audit.webhook_deliveries wd
  JOIN audit.webhook_endpoints we ON we.endpoint_id = wd.endpoint_id
 WHERE wd.status = 'failed'
   AND wd.delivered_at >= TIMESTAMPTZ '2026-06-01 00:00:00+00';

CREATE VIEW audit.open_disputes_v AS
SELECT d.dispute_id,
       d.tenant_id,
       d.invoice_id,
       d.reason,
       d.opened_at,
       COUNT(de.dispute_event_id) AS event_count
  FROM audit.disputes d
  LEFT JOIN audit.dispute_events de ON de.dispute_id = d.dispute_id
 WHERE d.status = 'open'
 GROUP BY d.dispute_id, d.tenant_id, d.invoice_id, d.reason, d.opened_at;

-- Postgres-only materialized view
CREATE MATERIALIZED VIEW billing.monthly_revenue_mv AS
SELECT DATE_TRUNC('month', i.issued_at)::date AS month_start,
       i.tenant_id,
       i.currency_code,
       SUM(i.total_cents) AS revenue_cents
  FROM billing.invoices i
 WHERE i.deleted_at IS NULL
   AND i.status IN ('paid', 'partial')
 GROUP BY DATE_TRUNC('month', i.issued_at), i.tenant_id, i.currency_code
 WITH NO DATA;

-- Loader runs REFRESH MATERIALIZED VIEW billing.monthly_revenue_mv after insert.

-- Indexes helpful for test queries
CREATE INDEX idx_invoices_tenant_issued  ON billing.invoices(tenant_id, issued_at);
CREATE INDEX idx_subs_tenant             ON billing.subscriptions(tenant_id);
CREATE INDEX idx_line_items_invoice      ON billing.invoice_line_items(invoice_id);
CREATE INDEX idx_usage_events_tenant     ON billing.usage_events(tenant_id, event_at);
CREATE INDEX idx_audit_log_entity        ON audit.audit_log(entity_kind, entity_id);
