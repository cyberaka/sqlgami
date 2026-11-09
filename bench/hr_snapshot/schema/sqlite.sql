-- hr_snapshot — SQLite
-- Naming rule: <schema>_<table> (no ATTACH; one DB)

CREATE TABLE hr_ref_country_region (
    country_cd  TEXT PRIMARY KEY,
    region      TEXT NOT NULL
);

CREATE TABLE hr_ref_job_group_category (
    job_grp     TEXT PRIMARY KEY,
    category    TEXT NOT NULL
);

CREATE TABLE hr_ref_status_code_meaning (
    status_cd   TEXT PRIMARY KEY,
    meaning     TEXT NOT NULL
);

CREATE TABLE hr_pay_period_dim (
    ppe_dt            TEXT        NOT NULL,     -- 'YYYY-MM-DD'
    freq_cd           TEXT        NOT NULL,     -- 'B' or 'W'
    period_start_dt   TEXT        NOT NULL,
    period_seq        INTEGER     NOT NULL,
    is_current_flag   INTEGER     NOT NULL,     -- 0/1
    PRIMARY KEY (ppe_dt, freq_cd)
);

CREATE TABLE hr_hierarchy_dim (
    lvl            INTEGER   NOT NULL,
    node_id        INTEGER   NOT NULL,
    node_name      TEXT      NOT NULL,
    parent_node_id INTEGER   NULL,
    hierarchy_kind TEXT      NOT NULL,          -- 'reporting' or 'functional'
    PRIMARY KEY (hierarchy_kind, node_id)
);

CREATE TABLE hr_employee_dim (
    assoc_id       INTEGER   PRIMARY KEY,
    given_name     TEXT      NOT NULL,
    family_name    TEXT      NOT NULL,
    hire_dt        TEXT      NOT NULL,
    term_dt        TEXT      NULL,
    mgr_assoc_id   INTEGER   NULL REFERENCES hr_employee_dim(assoc_id)
);

CREATE TABLE hr_emp_period_snapshot (
    snap_dt            TEXT        NOT NULL,
    period_end_dt      TEXT        NOT NULL,
    assoc_id           INTEGER     NOT NULL,
    hire_dt            TEXT        NOT NULL,
    term_dt            TEXT        NULL,
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
    lvl_2_id           INTEGER     NULL,
    lvl_3_id           INTEGER     NULL,
    lvl_4_id           INTEGER     NULL,
    lvl_5_id           INTEGER     NULL,
    lvl_6_id           INTEGER     NULL,
    lvl_7_id           INTEGER     NULL,
    lvl_2_id_fn        INTEGER     NULL,
    lvl_3_id_fn        INTEGER     NULL,
    lvl_4_id_fn        INTEGER     NULL,
    lvl_5_id_fn        INTEGER     NULL,
    lvl_6_id_fn        INTEGER     NULL,
    lvl_7_id_fn        INTEGER     NULL,
    PRIMARY KEY (assoc_id, period_end_dt, snap_dt)
);

CREATE INDEX idx_eps_snap_period ON hr_emp_period_snapshot(snap_dt, period_end_dt);
CREATE INDEX idx_eps_lvl3 ON hr_emp_period_snapshot(lvl_3_id);
CREATE INDEX idx_eps_lvl4 ON hr_emp_period_snapshot(lvl_4_id);

-- Views
CREATE VIEW hr_current_snapshot_v AS
SELECT eps.*
  FROM hr_emp_period_snapshot eps
  JOIN hr_pay_period_dim pp ON pp.ppe_dt = eps.period_end_dt AND pp.freq_cd = 'B'
 WHERE pp.is_current_flag = 1
   AND eps.snap_dt = eps.period_end_dt;

CREATE VIEW hr_new_hires_last_26p_v AS
SELECT eps.*
  FROM hr_emp_period_snapshot eps
  JOIN hr_pay_period_dim pp ON pp.ppe_dt = eps.period_end_dt AND pp.freq_cd = 'B'
 WHERE eps.new_hire_ind = 1
   AND pp.period_seq BETWEEN -25 AND 0
   AND eps.snap_dt = eps.period_end_dt;

CREATE VIEW hr_active_headcount_by_period_v AS
SELECT eps.period_end_dt, COUNT(*) AS headcount
  FROM hr_emp_period_snapshot eps
 WHERE eps.status_cd = 'A'
   AND eps.snap_dt = eps.period_end_dt
 GROUP BY eps.period_end_dt;

CREATE VIEW hr_turnover_26p_v AS
SELECT eps.*
  FROM hr_emp_period_snapshot eps
  JOIN hr_pay_period_dim pp ON pp.ppe_dt = eps.period_end_dt AND pp.freq_cd = 'B'
 WHERE eps.turn_filt_ind = 1
   AND pp.period_seq BETWEEN -25 AND 0
   AND eps.snap_dt = eps.period_end_dt;

CREATE VIEW hr_vol_turnover_26p_v AS
SELECT * FROM hr_turnover_26p_v WHERE invol_exit_ind = 0;

CREATE VIEW hr_invol_turnover_26p_v AS
SELECT * FROM hr_turnover_26p_v WHERE invol_exit_ind = 1;

CREATE VIEW hr_orphaned_hierarchy_v AS
SELECT eps.assoc_id, eps.period_end_dt, eps.lvl_5_id
  FROM hr_emp_period_snapshot eps
 WHERE eps.snap_dt = eps.period_end_dt
   AND eps.lvl_5_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM hr_hierarchy_dim h
                    WHERE h.hierarchy_kind = 'reporting'
                      AND h.lvl = 5
                      AND h.node_id = eps.lvl_5_id);

CREATE VIEW hr_active_at_current_period_by_region_v AS
SELECT eps.region, COUNT(*) AS headcount
  FROM hr_current_snapshot_v eps
 WHERE eps.status_cd = 'A'
 GROUP BY eps.region;
