-- A BigQuery snapshot-style schema, the shape sqlgami's pilot template is written for.
-- Replace `my-project.hr` with your own project and dataset; keep the three-part names
-- exactly as your queries write them. Keys may be NOT ENFORCED: sqlgami reads them as
-- the declared grain, which is what the fixtures are built on.

CREATE TABLE `my-project.hr.pay_period_dim` (
  ppe_dt          DATE    NOT NULL,   -- period end date
  period_start_dt DATE    NOT NULL,
  freq_cd         STRING  NOT NULL,   -- 'B' biweekly, 'W' weekly
  period_seq      INT64   NOT NULL,   -- 0 = current period, -1 = the one before, ...
  is_current_flag BOOL    NOT NULL,
  PRIMARY KEY (ppe_dt, freq_cd) NOT ENFORCED
);

CREATE TABLE `my-project.hr.employee_dim` (
  assoc_id   INT64  NOT NULL,
  hire_dt    DATE   NOT NULL,
  term_dt    DATE,
  PRIMARY KEY (assoc_id) NOT ENFORCED
);

CREATE TABLE `my-project.hr.emp_period_snapshot` (
  assoc_id       INT64  NOT NULL,
  period_end_dt  DATE   NOT NULL,
  snap_dt        DATE   NOT NULL,     -- snap_dt = period_end_dt is the authoritative row; later snap_dt rows are restatements
  status_cd      STRING,              -- 'A' active, 'L' leave, 'T' terminated
  assoc_type     STRING,              -- 'FTE', 'Contractor', ...
  segment        STRING,
  hire_dt        DATE,
  term_dt        DATE,
  lvl_2_id       INT64, lvl_3_id INT64, lvl_4_id INT64,
  lvl_2_id_fn    INT64, lvl_3_id_fn INT64, lvl_4_id_fn INT64,
  PRIMARY KEY (assoc_id, period_end_dt, snap_dt) NOT ENFORCED,
  FOREIGN KEY (assoc_id) REFERENCES `my-project.hr.employee_dim` (assoc_id) NOT ENFORCED
)
PARTITION BY period_end_dt
CLUSTER BY lvl_3_id;
