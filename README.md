# sqlgami

sqlgami reads a SQL statement that a language model wrote for a plain-English
question and reports how much confidence the statement deserves, and why.

It is a Python library that other applications embed. You give it the SQL, the
schema or a database connection, and optionally the question. It returns a
report as a plain data object that converts to JSON. What to show, and whether
to show anything, is up to your application.

> **Advisory only.** A sqlgami report is an estimate. It is not a guarantee
> that a statement is right or wrong, and it comes with no warranty (see
> sections 7 and 8 of the [license](LICENSE)). Review SQL and its results
> before relying on them. Every report carries this notice in its `advisory`
> field.

## Status

Early and private. Working today: the core, the static and question checks,
and adapters for SQLite, DuckDB and Postgres. BigQuery SQL can be analysed
statically; its adapter comes next, then Cosmos DB. The confidence number is a **provisional heuristic** until a
calibrator fitted on labelled data is loaded; reports say so with
`"calibrated": false`. The calibration mechanism exists; a fitted calibration
does not ship yet.

## Install

```
pip install sqlgami              # core: SQLite and static analysis
pip install "sqlgami[duckdb]"    # adds the DuckDB driver
pip install "sqlgami[postgres]"  # adds psycopg 3 for Postgres
```

The core depends on one package, [sqlglot](https://github.com/tobymao/sqlglot).
Database drivers are optional extras. The core imports no web, UI or LLM
framework.

## Use

```python
import sqlite3
from sqlgami import Scorer

conn = sqlite3.connect("shop.db")
scorer = Scorer("sqlite", db=conn)

report = scorer.sample(
    "SELECT c.name, SUM(o.total) FROM customers c "
    "JOIN orders o ON o.customer_id = c.id "
    "JOIN items i ON i.order_id = o.id GROUP BY c.name",
    question="Total order value per customer",
)

report.verdict           # Verdict.LIKELY_WRONG
report.score             # 0.4
report.findings[0].code  # "fan_out"
report.to_dict()         # JSON-serialisable
print(report.summary())  # short text form
```

Without a connection, pass the schema instead and sqlgami makes no database
calls at all:

```python
scorer = Scorer("postgres", catalog=open("schema.sql").read())
report = scorer.explain("SELECT * FROM orders WHERE shipped_at = NULL")
```

`catalog` accepts DDL text (`CREATE TABLE`, `CREATE VIEW`, `CREATE UNIQUE
INDEX`), a dictionary, or a `Catalog` object.

### How deep to check

| Mode | What runs | Touches the database |
|---|---|---|
| `static` | parse, read-only check, schema resolution, static checks | schema lookups only, and only if you passed a connection |
| `dry` | the above, plus the engine's own planner (`EXPLAIN`) | yes, without running the query |
| `sample` | the above, plus one read-only run of the query and small probe queries that test findings against the data | yes |
| `full` | reserved for the optional LLM-backed checks | yes |

The default is `dry` when you pass a connection and `static` when you do not.
`scorer.explain()`, `scorer.dry_run()` and `scorer.sample()` pick a mode
directly.

A query is only run in `sample` mode, and only if its estimated cost is under
`cost_ceiling`. Above the ceiling the report says "unverified beyond static and
dry-run checks".

### What it looks for

Hard gates, which cap the score near zero:

- the statement does not parse
- it is not a single read-only query (`INSERT`, `UPDATE`, `DELETE`, DDL,
  several statements, `SELECT ... INTO`, a writing CTE)
- it names a table or column that does not exist, or a column that is ambiguous
- it reaches outside the database (`read_csv`, `pg_read_file`, `dblink`, ...)
- the database rejects it, or it fails when run

Static checks, each reported as a finding with a severity:

| Code | Catches |
|---|---|
| `fan_out` | a `SUM`, `AVG` or `COUNT` over rows that a join has repeated |
| `distinct_masking` | `DISTINCT` used to paper over repeated rows |
| `cartesian_join` | tables combined with nothing linking them |
| `outer_join_nullified` | a `WHERE` or later inner join that turns an outer join into an inner join |
| `missing_group_by` | a selected column that is neither grouped nor aggregated |
| `avg_of_avg` | an average of per-group averages or ratios |
| `limit_without_order` | `LIMIT` with no `ORDER BY` |
| `null_comparison` | `= NULL`, `<> NULL`, or comparing with the text `'NULL'` |
| `not_in_nullable` | `NOT IN` against values that can be NULL |
| `type_mismatch` | comparisons between unlike types |
| `integer_division` | a ratio computed with integer division |
| `date_boundary` | date ranges that start or stop one step off, and impossible dates |
| `filter_value` | a filter on a value the column never holds, such as `'usa'` for `'US'` |
| `missing_tenant_filter` | a multi-tenant table read without a tenant filter (needs `tenant_column`) |
| `soft_deleted_rows` | a soft-delete marker that is never looked at (needs `soft_delete_column`) |
| `history_rows` | a table that keeps old versions of rows (`effective_to`, `valid_to`, `is_current`) read without choosing a version |
| `natural_join` | `NATURAL JOIN` |
| `snapshot_grain` | a periodic snapshot table read without choosing which snapshots, so everything is counted once per snapshot |
| `required_predicate` | a filter your policy requires on a table (see below) is missing or incomplete |
| `or_chain_gap` | `LVL_2 = v OR LVL_3 = v OR LVL_5 = v` where the table also has `LVL_4` |
| `partition_filter` | a partitioned table read with no partition filter, or only a non-constant one |
| `duplicate_rows` | a plain listing that repeats rows because of a join |
| `inner_join_drops_rows` | an inner join that leaves out things with no related rows (raised when the question says "for every ...") |
| `scalar_subquery_rows` | a subquery used as one value that nothing limits to one row |
| `self_comparison`, `empty_string_comparison` | `x <> x`; `x = ''` where missing values are NULL |
| `date_arithmetic` | a bare number added to a date where the engine does not define that |
| `json_path` | a JSON path no row has (found in `sample` mode) |

When you pass the question, sqlgami also compares it with the SQL. These checks
are word-and-structure matching with no language model involved:

| Code | Catches |
|---|---|
| `count_grain` | "how many customers" answered by counting rows of a join that repeats customers |
| `question_value_unused` | the question names a stored value (such as "paid") that the SQL never filters on |
| `filter_not_in_question` | a filter on a value the question and glossary never mention |
| `top_n`, `order_direction` | "top 5" without `LIMIT 5`, or sorted the wrong way |
| `aggregate_mismatch` | "average" or "median" with no such computation |
| `number_unused` | a number in the question that appears nowhere in the SQL |
| `grouping_missing` | "per customer" answered with a single aggregate |
| `negation_missing` | "not", "never", "without" with no negation in the SQL |
| `time_filter_missing` | a year or period in the question with no date condition |
| `comparison_operator` | "greater than 50" written as `>= 50`, "at least" as `>`, and so on |
| `question_flag_unused` | "active plans" where the table has an `is_active` column the SQL ignores |
| `pattern_match_missing` | "starts with" answered by an exact comparison |

The question also firms up earlier findings: a missing soft-delete filter is
raised when the question says "non-deleted" and dropped when it asks about
deleted rows; a history table read without choosing a version is raised when
the question says "current".

Matching the question's words to stored values needs the values, which sqlgami
reads in `sample` mode (low-cardinality text columns of the tables the query
uses, cached for later calls). Pass `glossary=` to `Scorer` so that values your
business definitions imply are not reported as unasked-for filters.

Views are expanded before the checks run, including views built on other
views, so a check sees the base tables a view reads.

In `sample` mode sqlgami tests several findings against the data. A suspected
fan-out on columns with no declared key is confirmed if the columns hold
repeated values and dismissed if they do not. A `NOT IN` warning is confirmed
if the column really holds NULLs. Filter values are looked up in the column.

### The report

```json
{
  "schema_version": "1.0",
  "advisory": "Advisory only. ...",
  "engine": "sqlite",
  "mode": "sample",
  "question": "Total order value per customer",
  "sql": "SELECT ...",
  "verdict": "likely_wrong",
  "confidence": {
    "value": 0.4, "raw": 0.4, "low": 0.28, "high": 0.4,
    "calibrated": false,
    "cap": 0.4, "cap_reason": "high-severity finding: fan_out",
    "basis": "sample evidence; provisional heuristic, not calibrated"
  },
  "stages": [{"name": "parse", "status": "passed", "detail": null, "duration_ms": 1.2}],
  "findings": [{"code": "fan_out", "severity": "high", "message": "...", "stage": "static",
                "snippet": "SUM(o.total)", "suggestion": "...", "evidence": {}}],
  "checks_not_run": [],
  "cost": {"unit": "rows", "value": 123.0, "detail": {}},
  "signals": {"rows_returned": 3},
  "tables": ["main.customers", "main.items", "main.orders"],
  "plan": []
}
```

Stages, in order, are `parse`, `statement`, `schema`, `static`, `dry_run`,
`sample` and `question`.

Verdicts are `invalid`, `likely_wrong`, `uncertain`, `likely_right` and
`unverified`. A check that could not run is listed under `checks_not_run`
rather than counted as passed.

### Policies

Some filters are rules of your organisation, not properties of the SQL. Tell
the scorer about them and a query that breaks one is reported at high severity:

```python
from sqlgami import RequiredPredicate, Scorer

scorer = Scorer(
    "bigquery",
    catalog=ddl,
    tenant_column="tenant_id",              # every multi-tenant table must be pinned to one tenant
    soft_delete_column="deleted_at",
    soft_delete_policy="required",          # deleted rows are excluded unless a question asks for them
    snapshot_columns={"EMP_SNAPSHOT_FACT": "SNAP_DT"},
    required_predicates=[
        RequiredPredicate(
            name="manager scope",
            tables=("EMP_SNAPSHOT_FACT",),
            columns=("LVL_2_ID", "LVL_3_ID", "LVL_4_ID", "LVL_5_ID"),
            match="or_chain",               # one OR chain against one value must cover all of them
        )
    ],
)
```

`match` is `any` (one of the columns pinned to a value), `all`, or `or_chain`.
With `soft_delete_policy="advisory"`, the default, a missing soft-delete filter
is a medium-severity finding instead.

### Templates and scripts

SQL kept as a Python format string can be scored as it is. `{name}`
placeholders are found before parsing and analysed as query parameters:

```python
report = scorer.score("SELECT ... WHERE mgr_id = {mgr_id} AND grp IN ('{groups}')")
report.signals["template_parameters"]   # names, whether quoted, inferred types
report = scorer.score(sql, params={"mgr_id": 7, "groups": "ENG,OPS"})
```

A template is never sent to the database unless every placeholder has a value.
`IN ('{groups}')` is reported as `template_list_param`: filled with `ENG,OPS`
it becomes one string and matches nothing.

On BigQuery, a script of `DECLARE` and `SET` statements followed by one query
is scored on that query, with the declared variables resolved. Filters that
are commented out are listed under `signals["disabled_filters"]`.

BigQuery support is static analysis only for now: three-part
`project.dataset.table` names (dashes in the project included), case-sensitive
table names and case-insensitive column names. The BigQuery adapter is next.

### Calibration

Out of the box the confidence value is a heuristic: it ranks statements from
more to less trustworthy, and reports mark it `"calibrated": false`. To make
the number mean something measurable, fit a calibrator on statements whose
correctness you know and load it:

```python
from sqlgami import IsotonicCalibrator, Scorer

calibrator = IsotonicCalibrator.fit(samples)   # (provisional score, was it correct, depth) triples
calibrator.save("calibration.json")

scorer = Scorer("postgres", db=conn, calibrator="calibration.json")
```

With a calibrator loaded, a value of 0.8 means that about 80% of comparable
statements in the fitting data were correct, the report says
`"calibrated": true`, and the provisional score stays available as
`confidence.raw`. Hard gates and caps still apply on top. A calibration only
carries over to workloads that resemble the data it was fitted on, so it does
not turn the report into a guarantee. Depths with too few labelled examples
are left uncalibrated.

`uv run python -m benchkit calibrate <suite>... --out calibration.json` fits
one from bench suites and prints its error on questions held out of the fit.

### Safety

- sqlgami sends the database only read-only work: the statement under review
  (after it has been parsed and confirmed to be a single query), `EXPLAIN` of
  it, schema lookups, and small probe queries it builds itself.
- A database opened by path is opened read-only. On a SQLite connection you
  pass in, writes are disabled for the duration of each call and restored
  afterwards.
- On Postgres every statement runs in its own `READ ONLY` transaction, so a
  function with side effects cannot write. If the connection you pass in is
  already inside a transaction, sqlgami works under a savepoint and leaves your
  transaction as it found it; the statement gate still applies, but the
  read-only setting is then yours to make.
- Queries run under a time limit and return a bounded number of rows.
- Nothing is sent to a database, a network service or an LLM unless you pass a
  connection or function for it.

### Errors

Bad SQL is never an exception; it comes back as a report. Exceptions mean
misuse or infrastructure trouble, and all derive from `sqlgami.SqlgamiError`:
`ConfigurationError`, `MissingDependencyError`, `CatalogError`, `AdapterError`,
`ConnectionFailed`, `UnsafeQueryError`.

The library keeps no global state and configures no logging. It logs to the
`sqlgami` logger, which is silent unless your application attaches a handler.

## Command line

A thin wrapper over the same API, using the standard library only:

```
sqlgami score --engine sqlite --db shop.db --mode sample "SELECT ..."
sqlgami score --engine postgres --schema schema.sql --sql-file q.sql --json
sqlgami score --engine duckdb --db shop.duckdb --fail-under 0.5 "SELECT ..."
```

## Development

```
uv sync
uv run pytest
```

Postgres tests are skipped unless a test server is named. Any throwaway
Postgres 16 will do:

```
SQLGAMI_TEST_PG_DSN=postgresql://USER:PASSWORD@127.0.0.1:5440/DBNAME uv run pytest
```

The tests create and drop their own schemas in that database.

### Bench

`bench/` holds test suites: realistic schemas, deterministic data generators,
questions with gold SQL, planted bugs and equivalent rewrites. `benchkit/` is
the tooling around them. It is not part of the installed package.

```
uv run python -m benchkit build saas_billing       # generate data, build SQLite and DuckDB databases
uv run python -m benchkit validate saas_billing    # check gold, bugs and equivalents against each other
uv run python -m benchkit evaluate saas_billing    # score everything with sqlgami, print metrics
uv run python -m benchkit label saas_billing bench/.runs/<run-id>   # label candidates.jsonl by execution
```

SQLite and DuckDB databases are files. Postgres databases are built only when
`SQLGAMI_BENCH_PG_DSN` names a server; one database per suite and seed is
created there, named `sqlgami_bench_<suite>_<seed>`. `validate --strict` fails
on an empty suite and on engines the suite declares but the machine cannot
check.

A suite can be named by its directory under `bench/` or by a path. Generated
data, downloaded benchmarks and run output live under `bench/.data/`,
`bench/.external/` and `bench/.runs/`, which are not committed.

A statement is labelled correct only if it returns what the gold SQL returns
on every data seed, including one seed built to be hostile (NULLs, duplicates,
several tenants, soft-deleted rows, boundary dates).

## License

Apache License 2.0. See [LICENSE](LICENSE).
