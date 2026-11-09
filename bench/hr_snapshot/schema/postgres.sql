-- hr_snapshot — PostgreSQL 16
-- Mirrors a production biweekly periodic-snapshot HR fact.
-- Postgres convention: uppercase column names are NOT quoted; they fold to lowercase.
-- Session TimeZone pinned to UTC by the loader.

CREATE SCHEMA hr;
CREATE SCHEMA hr_ref;

-- ===========================================================================
-- hr_ref: small reference tables
-- ===========================================================================

CREATE TABLE hr_ref.country_region (
    country_cd  TEXT PRIMARY KEY,
    region      TEXT NOT NULL
);

CREATE TABLE hr_ref.job_group_category (
    job_grp     TEXT PRIMARY KEY,
    category    TEXT NOT NULL
);

CREATE TABLE hr_ref.status_code_meaning (
    status_cd   TEXT PRIMARY KEY,
    meaning     TEXT NOT NULL
);

-- ===========================================================================
-- hr: dims
-- ===========================================================================

CREATE TABLE hr.pay_period_dim (
    ppe_dt            DATE        NOT NULL,
    freq_cd           TEXT        NOT NULL,
    period_start_dt   DATE        NOT NULL,
    period_seq        INTEGER     NOT NULL,
    is_current_flag   BOOLEAN     NOT NULL,
    PRIMARY KEY (ppe_dt, freq_cd)
);

CREATE TABLE hr.hierarchy_dim (
    lvl         INTEGER     NOT NULL,
    node_id     BIGINT      NOT NULL,
    node_name   TEXT        NOT NULL,
    parent_node_id BIGINT   NULL,
    hierarchy_kind TEXT     NOT NULL,
    PRIMARY KEY (hierarchy_kind, node_id)
);

CREATE TABLE hr.employee_dim (
    assoc_id       BIGINT      PRIMARY KEY,
    given_name     TEXT        NOT NULL,
    family_name    TEXT        NOT NULL,
    hire_dt        DATE        NOT NULL,
    term_dt        DATE        NULL,
    mgr_assoc_id   BIGINT      NULL REFERENCES hr.employee_dim(assoc_id)
);

-- ===========================================================================
-- hr.emp_period_snapshot — the wide fact
-- One row per (ASSOC_ID, PERIOD_END_DT, SNAP_DT). Each snapshot date may
-- restate the current and the previous 2-3 periods (so each PERIOD_END_DT
-- has multiple SNAP_DT rows). The correct row for a given period is
-- SNAP_DT = PERIOD_END_DT. Partitioned by PERIOD_END_DT quarterly.
-- ===========================================================================

CREATE TABLE hr.emp_period_snapshot (
    snap_dt            DATE        NOT NULL,
    period_end_dt      DATE        NOT NULL,
    assoc_id           BIGINT      NOT NULL,
    hire_dt            DATE        NOT NULL,
    term_dt            DATE        NULL,
    status_cd          TEXT        NULL,
    invol_exit_ind     INTEGER     NULL,
    new_hire_ind       INTEGER     NOT NULL,
    exit_filt_ind      INTEGER     NOT NULL,
    turn_filt_ind      INTEGER     NOT NULL,
    region             TEXT        NOT NULL,
    segment            TEXT        NOT NULL,
    country            TEXT        NOT NULL,
    job_grp            TEXT        NOT NULL,
    assoc_type         TEXT        NULL,
    lvl_2_id           BIGINT      NULL,
    lvl_3_id           BIGINT      NULL,
    lvl_4_id           BIGINT      NULL,
    lvl_5_id           BIGINT      NULL,
    lvl_6_id           BIGINT      NULL,
    lvl_7_id           BIGINT      NULL,
    lvl_2_id_fn        BIGINT      NULL,
    lvl_3_id_fn        BIGINT      NULL,
    lvl_4_id_fn        BIGINT      NULL,
    lvl_5_id_fn        BIGINT      NULL,
    lvl_6_id_fn        BIGINT      NULL,
    lvl_7_id_fn        BIGINT      NULL,
    PRIMARY KEY (assoc_id, period_end_dt, snap_dt)
) PARTITION BY RANGE (period_end_dt);

CREATE TABLE hr.emp_period_snapshot_2025_h2 PARTITION OF hr.emp_period_snapshot
    FOR VALUES FROM ('2025-07-01') TO ('2026-01-01');
CREATE TABLE hr.emp_period_snapshot_2026_h1 PARTITION OF hr.emp_period_snapshot
    FOR VALUES FROM ('2026-01-01') TO ('2026-07-01');
CREATE TABLE hr.emp_period_snapshot_2026_h2 PARTITION OF hr.emp_period_snapshot
    FOR VALUES FROM ('2026-07-01') TO ('2027-01-01');
CREATE TABLE hr.emp_period_snapshot_2027_h1 PARTITION OF hr.emp_period_snapshot
    FOR VALUES FROM ('2027-01-01') TO ('2027-07-01');

CREATE INDEX idx_eps_snap_period ON hr.emp_period_snapshot(snap_dt, period_end_dt);
CREATE INDEX idx_eps_lvl3 ON hr.emp_period_snapshot(lvl_3_id);
CREATE INDEX idx_eps_lvl4 ON hr.emp_period_snapshot(lvl_4_id);

-- ===========================================================================
-- Views
-- ===========================================================================

CREATE VIEW hr.current_snapshot_v AS
SELECT eps.*
  FROM hr.emp_period_snapshot eps
  JOIN hr.pay_period_dim pp ON pp.ppe_dt = eps.period_end_dt AND pp.freq_cd = 'B'
 WHERE pp.is_current_flag = TRUE
   AND eps.snap_dt = eps.period_end_dt;

CREATE VIEW hr.new_hires_last_26p_v AS
SELECT eps.*
  FROM hr.emp_period_snapshot eps
  JOIN hr.pay_period_dim pp ON pp.ppe_dt = eps.period_end_dt AND pp.freq_cd = 'B'
 WHERE eps.new_hire_ind = 1
   AND pp.period_seq BETWEEN -25 AND 0
   AND eps.snap_dt = eps.period_end_dt;

CREATE VIEW hr.active_headcount_by_period_v AS
SELECT eps.period_end_dt, COUNT(*) AS headcount
  FROM hr.emp_period_snapshot eps
 WHERE eps.status_cd = 'A'
   AND eps.snap_dt = eps.period_end_dt
 GROUP BY eps.period_end_dt;

CREATE VIEW hr.turnover_26p_v AS
SELECT eps.*
  FROM hr.emp_period_snapshot eps
  JOIN hr.pay_period_dim pp ON pp.ppe_dt = eps.period_end_dt AND pp.freq_cd = 'B'
 WHERE eps.turn_filt_ind = 1
   AND pp.period_seq BETWEEN -25 AND 0
   AND eps.snap_dt = eps.period_end_dt;

CREATE VIEW hr.vol_turnover_26p_v AS
SELECT *
  FROM hr.turnover_26p_v
 WHERE invol_exit_ind = 0;

CREATE VIEW hr.invol_turnover_26p_v AS
SELECT *
  FROM hr.turnover_26p_v
 WHERE invol_exit_ind = 1;

CREATE VIEW hr.orphaned_hierarchy_v AS
SELECT eps.assoc_id, eps.period_end_dt, eps.lvl_5_id
  FROM hr.emp_period_snapshot eps
 WHERE eps.snap_dt = eps.period_end_dt
   AND eps.lvl_5_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM hr.hierarchy_dim h
                    WHERE h.hierarchy_kind = 'reporting'
                      AND h.lvl = 5
                      AND h.node_id = eps.lvl_5_id);

CREATE VIEW hr.active_at_current_period_by_region_v AS
SELECT eps.region, COUNT(*) AS headcount
  FROM hr.current_snapshot_v eps
 WHERE eps.status_cd = 'A'
 GROUP BY eps.region;

-- Postgres materialized view: monthly headcount rollup, refreshed by the loader.
CREATE MATERIALIZED VIEW hr.monthly_headcount_mv AS
SELECT DATE_TRUNC('month', eps.period_end_dt)::date AS month_start,
       eps.region,
       COUNT(*) AS headcount
  FROM hr.emp_period_snapshot eps
 WHERE eps.status_cd = 'A'
   AND eps.snap_dt = eps.period_end_dt
 GROUP BY DATE_TRUNC('month', eps.period_end_dt), eps.region
WITH NO DATA;
