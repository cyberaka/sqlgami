-- hr_snapshot — BigQuery
-- Owner's production shape: backticked 3-part project.dataset.table names,
-- DATE partitioning on the wide fact, clustering on hot hierarchy columns,
-- NOT ENFORCED keys. Same logical schema as the postgres and duckdb files;
-- canonicalises to the same fixture digest.

CREATE SCHEMA IF NOT EXISTS `acme-people-prod.hr`;
CREATE SCHEMA IF NOT EXISTS `acme-people-prod.hr_ref`;

-- ===========================================================================
-- hr_ref: small reference tables
-- ===========================================================================

CREATE TABLE `acme-people-prod.hr_ref.country_region` (
    country_cd  STRING NOT NULL,
    region      STRING NOT NULL,
    PRIMARY KEY (country_cd) NOT ENFORCED
);

CREATE TABLE `acme-people-prod.hr_ref.job_group_category` (
    job_grp     STRING NOT NULL,
    category    STRING NOT NULL,
    PRIMARY KEY (job_grp) NOT ENFORCED
);

CREATE TABLE `acme-people-prod.hr_ref.status_code_meaning` (
    status_cd   STRING NOT NULL,
    meaning     STRING NOT NULL,
    PRIMARY KEY (status_cd) NOT ENFORCED
);

-- ===========================================================================
-- hr: dims
-- ===========================================================================

CREATE TABLE `acme-people-prod.hr.pay_period_dim` (
    ppe_dt            DATE    NOT NULL,
    freq_cd           STRING  NOT NULL,
    period_start_dt   DATE    NOT NULL,
    period_seq        INT64   NOT NULL,
    is_current_flag   BOOL    NOT NULL,
    PRIMARY KEY (ppe_dt, freq_cd) NOT ENFORCED
)
CLUSTER BY freq_cd, period_seq;

CREATE TABLE `acme-people-prod.hr.hierarchy_dim` (
    lvl              INT64  NOT NULL,
    node_id          INT64  NOT NULL,
    node_name        STRING NOT NULL,
    parent_node_id   INT64,
    hierarchy_kind   STRING NOT NULL,
    PRIMARY KEY (hierarchy_kind, node_id) NOT ENFORCED
)
CLUSTER BY hierarchy_kind, lvl;

CREATE TABLE `acme-people-prod.hr.employee_dim` (
    assoc_id       INT64  NOT NULL,
    given_name     STRING NOT NULL,
    family_name    STRING NOT NULL,
    hire_dt        DATE   NOT NULL,
    term_dt        DATE,
    mgr_assoc_id   INT64,
    PRIMARY KEY (assoc_id) NOT ENFORCED,
    FOREIGN KEY (mgr_assoc_id) REFERENCES `acme-people-prod.hr.employee_dim`(assoc_id) NOT ENFORCED
)
CLUSTER BY assoc_id;

-- ===========================================================================
-- hr.emp_period_snapshot — the wide fact
-- One row per (ASSOC_ID, PERIOD_END_DT, SNAP_DT). Each snap_dt may restate
-- the current and the previous 2-3 periods, so each PERIOD_END_DT has up to
-- 3 (snap_dt) rows per associate. The correct row for a period is
-- SNAP_DT = PERIOD_END_DT. Partitioned daily by PERIOD_END_DT; clustered
-- on the hot scope columns.
-- ===========================================================================

CREATE TABLE `acme-people-prod.hr.emp_period_snapshot` (
    snap_dt            DATE   NOT NULL,
    period_end_dt      DATE   NOT NULL,
    assoc_id           INT64  NOT NULL,
    hire_dt            DATE   NOT NULL,
    term_dt            DATE,
    status_cd          STRING,
    invol_exit_ind     INT64,
    new_hire_ind       INT64  NOT NULL,
    exit_filt_ind      INT64  NOT NULL,
    turn_filt_ind      INT64  NOT NULL,
    region             STRING NOT NULL,
    segment            STRING NOT NULL,
    country            STRING NOT NULL,
    job_grp            STRING NOT NULL,
    assoc_type         STRING,
    lvl_2_id           INT64,
    lvl_3_id           INT64,
    lvl_4_id           INT64,
    lvl_5_id           INT64,
    lvl_6_id           INT64,
    lvl_7_id           INT64,
    lvl_2_id_fn        INT64,
    lvl_3_id_fn        INT64,
    lvl_4_id_fn        INT64,
    lvl_5_id_fn        INT64,
    lvl_6_id_fn        INT64,
    lvl_7_id_fn        INT64,
    PRIMARY KEY (assoc_id, period_end_dt, snap_dt) NOT ENFORCED
)
PARTITION BY period_end_dt
CLUSTER BY lvl_3_id, lvl_4_id, snap_dt;

-- ===========================================================================
-- Views
-- ===========================================================================

CREATE VIEW `acme-people-prod.hr.current_snapshot_v` AS
SELECT eps.*
  FROM `acme-people-prod.hr.emp_period_snapshot` eps
  JOIN `acme-people-prod.hr.pay_period_dim` pp
    ON pp.ppe_dt = eps.period_end_dt AND pp.freq_cd = 'B'
 WHERE pp.is_current_flag = TRUE
   AND eps.snap_dt = eps.period_end_dt;

CREATE VIEW `acme-people-prod.hr.new_hires_last_26p_v` AS
SELECT eps.*
  FROM `acme-people-prod.hr.emp_period_snapshot` eps
  JOIN `acme-people-prod.hr.pay_period_dim` pp
    ON pp.ppe_dt = eps.period_end_dt AND pp.freq_cd = 'B'
 WHERE eps.new_hire_ind = 1
   AND pp.period_seq BETWEEN -25 AND 0
   AND eps.snap_dt = eps.period_end_dt;

CREATE VIEW `acme-people-prod.hr.active_headcount_by_period_v` AS
SELECT eps.period_end_dt, COUNT(*) AS headcount
  FROM `acme-people-prod.hr.emp_period_snapshot` eps
 WHERE eps.status_cd = 'A'
   AND eps.snap_dt = eps.period_end_dt
 GROUP BY eps.period_end_dt;

CREATE VIEW `acme-people-prod.hr.turnover_26p_v` AS
SELECT eps.*
  FROM `acme-people-prod.hr.emp_period_snapshot` eps
  JOIN `acme-people-prod.hr.pay_period_dim` pp
    ON pp.ppe_dt = eps.period_end_dt AND pp.freq_cd = 'B'
 WHERE eps.turn_filt_ind = 1
   AND pp.period_seq BETWEEN -25 AND 0
   AND eps.snap_dt = eps.period_end_dt;

CREATE VIEW `acme-people-prod.hr.vol_turnover_26p_v` AS
SELECT * FROM `acme-people-prod.hr.turnover_26p_v`
 WHERE invol_exit_ind = 0;

CREATE VIEW `acme-people-prod.hr.invol_turnover_26p_v` AS
SELECT * FROM `acme-people-prod.hr.turnover_26p_v`
 WHERE invol_exit_ind = 1;

CREATE VIEW `acme-people-prod.hr.orphaned_hierarchy_v` AS
SELECT eps.assoc_id, eps.period_end_dt, eps.lvl_5_id
  FROM `acme-people-prod.hr.emp_period_snapshot` eps
 WHERE eps.snap_dt = eps.period_end_dt
   AND eps.lvl_5_id IS NOT NULL
   AND NOT EXISTS (
         SELECT 1 FROM `acme-people-prod.hr.hierarchy_dim` h
          WHERE h.hierarchy_kind = 'reporting'
            AND h.lvl = 5
            AND h.node_id = eps.lvl_5_id
       );

CREATE VIEW `acme-people-prod.hr.active_at_current_period_by_region_v` AS
SELECT eps.region, COUNT(*) AS headcount
  FROM `acme-people-prod.hr.current_snapshot_v` eps
 WHERE eps.status_cd = 'A'
 GROUP BY eps.region;

-- ---------------------------------------------------------------------------
-- w6 additions: 4 TVFs + 4 views + 1 scalar UDF, across two datasets.
-- Covers: TVF-calls-TVF, view-over-TVF (with a fixed arg), a view in a
-- sibling hr_mart dataset reading hr tables, and a basis-points helper UDF.
-- Bugs in w6 land INSIDE the TVF/view call rather than in the surrounding
-- SQL.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE TABLE FUNCTION `acme-people-prod.hr.org_roster`(manager_id INT64, as_of DATE)
AS (
  SELECT eps.*
    FROM `acme-people-prod.hr.emp_period_snapshot` eps
   WHERE eps.period_end_dt = as_of
     AND eps.snap_dt = eps.period_end_dt
     AND (manager_id IN (eps.lvl_2_id, eps.lvl_3_id, eps.lvl_4_id, eps.lvl_5_id, eps.lvl_6_id, eps.lvl_7_id)
          OR manager_id IN (eps.lvl_2_id_fn, eps.lvl_3_id_fn, eps.lvl_4_id_fn, eps.lvl_5_id_fn, eps.lvl_6_id_fn, eps.lvl_7_id_fn))
);

CREATE OR REPLACE TABLE FUNCTION `acme-people-prod.hr.period_window`(n INT64)
AS (
  SELECT period_seq, period_start_dt, ppe_dt AS period_end_dt
    FROM `acme-people-prod.hr.pay_period_dim`
   WHERE freq_cd = 'B' AND period_seq BETWEEN -(n - 1) AND 0
);

CREATE OR REPLACE TABLE FUNCTION `acme-people-prod.hr.leavers`(manager_id INT64, n_periods INT64)
AS (
  SELECT eps.assoc_id, eps.period_end_dt, eps.invol_exit_ind,
         CASE WHEN eps.invol_exit_ind = 1 THEN 'involuntary'
              WHEN eps.invol_exit_ind = 0 THEN 'voluntary'
              ELSE 'unknown'
         END AS leave_kind
    FROM `acme-people-prod.hr.emp_period_snapshot` eps
    JOIN `acme-people-prod.hr.pay_period_dim` pp
      ON pp.ppe_dt = eps.period_end_dt AND pp.freq_cd = 'B'
   WHERE eps.turn_filt_ind = 1
     AND eps.snap_dt = eps.period_end_dt
     AND pp.period_seq BETWEEN -(n_periods - 1) AND 0
     AND (manager_id IN (eps.lvl_2_id, eps.lvl_3_id, eps.lvl_4_id, eps.lvl_5_id, eps.lvl_6_id, eps.lvl_7_id)
          OR manager_id IN (eps.lvl_2_id_fn, eps.lvl_3_id_fn, eps.lvl_4_id_fn, eps.lvl_5_id_fn, eps.lvl_6_id_fn, eps.lvl_7_id_fn))
);

CREATE OR REPLACE VIEW `acme-people-prod.hr.chain_level_coverage_v`
AS
  SELECT eps.assoc_id, eps.period_end_dt, x.chain, x.lvl, x.node_id
    FROM `acme-people-prod.hr.emp_period_snapshot` eps,
         UNNEST([
           STRUCT('reporting'  AS chain, 2 AS lvl, eps.lvl_2_id    AS node_id),
           STRUCT('reporting',       3, eps.lvl_3_id),
           STRUCT('reporting',       4, eps.lvl_4_id),
           STRUCT('reporting',       5, eps.lvl_5_id),
           STRUCT('reporting',       6, eps.lvl_6_id),
           STRUCT('reporting',       7, eps.lvl_7_id),
           STRUCT('functional', 2, eps.lvl_2_id_fn),
           STRUCT('functional', 3, eps.lvl_3_id_fn),
           STRUCT('functional', 4, eps.lvl_4_id_fn),
           STRUCT('functional', 5, eps.lvl_5_id_fn),
           STRUCT('functional', 6, eps.lvl_6_id_fn),
           STRUCT('functional', 7, eps.lvl_7_id_fn)
         ]) AS x
   WHERE eps.snap_dt = eps.period_end_dt;

CREATE OR REPLACE VIEW `acme-people-prod.hr.active_headcount_by_period_by_lvl_3_v`
AS
  SELECT eps.lvl_3_id, eps.period_end_dt,
         COUNT(DISTINCT IF(eps.status_cd = 'A', eps.assoc_id, NULL)) AS active_n
    FROM `acme-people-prod.hr.emp_period_snapshot` eps
    JOIN `acme-people-prod.hr.pay_period_dim` pp
      ON pp.ppe_dt = eps.period_end_dt AND pp.freq_cd = 'B'
   WHERE eps.snap_dt = eps.period_end_dt
     AND eps.lvl_3_id IS NOT NULL
     AND pp.period_seq BETWEEN -25 AND 0
   GROUP BY eps.lvl_3_id, eps.period_end_dt;

CREATE OR REPLACE VIEW `acme-people-prod.hr.active_by_lvl_3_current_v`
AS
  SELECT v.lvl_3_id, v.active_n
    FROM `acme-people-prod.hr.active_headcount_by_period_by_lvl_3_v` v
    JOIN `acme-people-prod.hr.pay_period_dim` pp
      ON pp.ppe_dt = v.period_end_dt AND pp.freq_cd = 'B'
   WHERE pp.is_current_flag = TRUE;

CREATE OR REPLACE TABLE FUNCTION `acme-people-prod.hr.active_headcount_trend`(manager_id INT64, n INT64)
AS (
  SELECT pw.period_seq, pw.period_end_dt,
         (SELECT COUNT(DISTINCT eps.assoc_id)
            FROM `acme-people-prod.hr.org_roster`(manager_id, pw.period_end_dt) eps
           WHERE eps.status_cd = 'A') AS active_n
    FROM `acme-people-prod.hr.period_window`(n) pw
);

-- NOTE: current_pay_period_v resolves the current period via period_window(1),
-- i.e. the biweekly row with period_seq = 0. The alternative surface,
-- pay_period_dim.is_current_flag = TRUE, is kept consistent with period_seq=0
-- by the fixture rules (see glossary: current_pay_period), so the two surfaces
-- always agree in-scope. Callers should treat them as interchangeable; this
-- view is the one to use when invoking via a TVF chain.
CREATE OR REPLACE VIEW `acme-people-prod.hr.current_pay_period_v`
AS
  SELECT period_seq, period_start_dt, period_end_dt
    FROM `acme-people-prod.hr.period_window`(1);

CREATE OR REPLACE FUNCTION `acme-people-prod.hr.bp`(x FLOAT64)
AS (CAST(FLOOR(x * 10000) AS INT64));

CREATE OR REPLACE VIEW `acme-people-prod.hr_mart.active_headcount_by_period_v`
AS
  SELECT eps.period_end_dt, COUNT(*) AS headcount
    FROM `acme-people-prod.hr.emp_period_snapshot` eps
   WHERE eps.snap_dt = eps.period_end_dt
     AND eps.status_cd = 'A'
   GROUP BY eps.period_end_dt;

