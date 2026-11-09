-- hr_snapshot — DuckDB
-- ICU needed only if gold queries do AT TIME ZONE; not used in this suite's gold.

CREATE SCHEMA hr;
CREATE SCHEMA hr_ref;

CREATE TABLE hr_ref.country_region (
    country_cd  VARCHAR PRIMARY KEY,
    region      VARCHAR NOT NULL
);

CREATE TABLE hr_ref.job_group_category (
    job_grp     VARCHAR PRIMARY KEY,
    category    VARCHAR NOT NULL
);

CREATE TABLE hr_ref.status_code_meaning (
    status_cd   VARCHAR PRIMARY KEY,
    meaning     VARCHAR NOT NULL
);

CREATE TABLE hr.pay_period_dim (
    ppe_dt            DATE        NOT NULL,
    freq_cd           VARCHAR     NOT NULL,
    period_start_dt   DATE        NOT NULL,
    period_seq        INTEGER     NOT NULL,
    is_current_flag   BOOLEAN     NOT NULL,
    PRIMARY KEY (ppe_dt, freq_cd)
);

CREATE TABLE hr.hierarchy_dim (
    lvl         INTEGER     NOT NULL,
    node_id     BIGINT      NOT NULL,
    node_name   VARCHAR     NOT NULL,
    parent_node_id BIGINT   NULL,
    hierarchy_kind VARCHAR  NOT NULL,
    PRIMARY KEY (hierarchy_kind, node_id)
);

CREATE TABLE hr.employee_dim (
    assoc_id       BIGINT      PRIMARY KEY,
    given_name     VARCHAR     NOT NULL,
    family_name    VARCHAR     NOT NULL,
    hire_dt        DATE        NOT NULL,
    term_dt        DATE        NULL,
    mgr_assoc_id   BIGINT      NULL
);

CREATE TABLE hr.emp_period_snapshot (
    snap_dt            DATE        NOT NULL,
    period_end_dt      DATE        NOT NULL,
    assoc_id           BIGINT      NOT NULL,
    hire_dt            DATE        NOT NULL,
    term_dt            DATE        NULL,
    status_cd          VARCHAR     NULL,
    invol_exit_ind     INTEGER     NULL,
    new_hire_ind       INTEGER     NOT NULL,
    exit_filt_ind      INTEGER     NOT NULL,
    turn_filt_ind      INTEGER     NOT NULL,
    region             VARCHAR     NOT NULL,
    segment            VARCHAR     NOT NULL,
    country            VARCHAR     NOT NULL,
    job_grp            VARCHAR     NOT NULL,
    assoc_type         VARCHAR     NULL,
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
);

CREATE INDEX idx_eps_snap_period ON hr.emp_period_snapshot(snap_dt, period_end_dt);
CREATE INDEX idx_eps_lvl3 ON hr.emp_period_snapshot(lvl_3_id);
CREATE INDEX idx_eps_lvl4 ON hr.emp_period_snapshot(lvl_4_id);

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

CREATE VIEW hr.vol_turnover_26p_v AS SELECT * FROM hr.turnover_26p_v WHERE invol_exit_ind = 0;
CREATE VIEW hr.invol_turnover_26p_v AS SELECT * FROM hr.turnover_26p_v WHERE invol_exit_ind = 1;

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
