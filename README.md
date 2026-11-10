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
adapters for SQLite, DuckDB and Postgres, and BigQuery through the fixture
path (static checks, terms, and fixtures reached through a verified
translation — the BigQuery service is never called live; see below). Cosmos DB
comes later. The confidence number is a **provisional heuristic** until a
calibrator fitted on labelled data is loaded; reports say so with
`"calibrated": false`. The calibration mechanism exists; a fitted calibration
does not ship yet.

## Install

```
pip install sqlgami              # core: SQLite and static analysis
pip install "sqlgami[duckdb]"    # adds the DuckDB driver
pip install "sqlgami[postgres]"  # adds psycopg 3 for Postgres
pip install "sqlgami[bigquery]"  # offline only: dry runs and native reference runs in the sandbox
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
| `sample` | the above, plus one read-only run of the query and small probe queries that test findings against the data; anchor probes you pass in | yes |
| `full` | the above, plus the consistency and reconciliation stages using a generator or prober you supply | yes |

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
| `access_filter_widened` | the policy's OR chain is present but carries a further branch on another column, admitting rows the policy's columns would not |
| `or_chain_gap` | `LVL_2 = v OR LVL_3 = v OR LVL_5 = v` where the table also has `LVL_4` |
| `partition_filter` | a partitioned table read with no partition filter, or only a non-constant one |
| `duplicate_rows` | a plain listing that repeats rows because of a join |
| `inner_join_drops_rows` | an inner join that leaves out things with no related rows (raised when the question says "for every ...") |
| `scalar_subquery_rows` | a subquery used as one value that nothing limits to one row |
| `self_comparison`, `empty_string_comparison` | `x <> x`; `x = ''` where missing values are NULL |
| `date_arithmetic` | a bare number added to a date where the engine does not define that |
| `json_path` | a JSON path no row has (found in `sample` mode) |
| `window_order_ties` | `ROW_NUMBER()` or a running total ordered by columns that can tie |
| `unbounded_recursion` | a recursive CTE grown with `UNION ALL` and no depth bound, which never finishes on data that repeats a key |

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
| `order_missing` | sorted output asked for, no `ORDER BY` |
| `output_columns` | "Return (tenant_id, name, n_orders)" answered with another number of columns, or without the named ones |
| `column_order` | the question lists the columns in an order the SELECT does not follow |
| `column_default` | "country_code (UNKNOWN if none)" returned without a `COALESCE`, or "NULL when none" replaced by a value |
| `label_format` | labels the question defines ('Q1', 'Q2' or "like 'Q1 2026'") written another way |

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
`sample`, `consistency` and `question`.

Verdicts are `invalid`, `likely_wrong`, `uncertain`, `likely_right` and
`unverified`. A check that could not run is listed under `checks_not_run`
rather than counted as passed.

### Agreement between independent queries

The static checks judge a query by its shape. For complex SQL that is not
enough: on a held-out set of harder questions they barely separated wrong
queries that run from right ones. What does separate them is the answer.
If several independently written queries for the same question return the
same data, they are very likely right; if they agree with one another and not
with the candidate, the candidate is very likely wrong.

Give sqlgami the other attempts, and it runs each one read-only under the same
limits as the candidate and compares the result sets:

```python
report = scorer.sample(sql, question, alternatives=[other_sql_1, other_sql_2, other_sql_3])
report.signals["consistency"]   # how many could be compared, how many agree, each alternative's status
```

Or pass a function that writes them, and use `full` mode:

```python
def generator(question, schema_text, glossary, k):
    return [...]          # k SQL strings from the model of your choice

scorer = Scorer("postgres", db=conn, generator=generator)
report = scorer.score(sql, question, mode="full")
```

| Outcome | Effect |
|---|---|
| the candidate matches most sources | the score rises into the upper band (0.9 and above is only reachable this way) |
| the sources match each other, not the candidate | `consistency_disagreement`: medium severity by default, high in the `judge` role |
| one source, and it differs | `consistency_disagreement`, medium severity |
| nothing matches anything | `consistency_no_consensus`: the question is probably ambiguous |

Votes are counted per source, not per query. Tag an alternative with who
wrote it, `(sql, "model-a")`, and several samples from one model become one
voice, voting with that model's most common answer. Untagged alternatives are
each their own voice. `sqlgami.consistency.combine(gen_a, gen_b)` draws from
several generators and tags them for you.

Two settings in `Options` matter:

- `consistency_role`: `confirm` (default) or `judge`. With `confirm`, agreement
  supports the candidate and disagreement is only a doubt. With `judge`, two
  or more independent sources agreeing against the candidate is treated as
  strong evidence it is wrong. Choose `judge` only when the alternatives come
  from something at least as capable as whatever wrote the candidate: in our
  tests, two mid-strength open models disagreed with a stronger model's query
  that was in fact right about two times in three.
- `consistency_k`: samples asked of each generator, 1 by default. Extra
  samples of the same model lowered precision in our tests; a second model
  raised it.

Alternatives are held to the same rules as the query under review: one that is
not a single read-only query, names something that does not exist, or calls a
function outside the allow-list (see Safety) is never run. The whole stage has
a time budget (`consistency_budget_s`, 30 seconds by default).

How results are compared:

- Row order is ignored, unless you set `consistency_ordered=True` because the
  question asks for an order.
- Columns may come in another order, but one reordering must work for every
  row. When both results use the same column names, the names decide which
  column is which, so two queries that swap the values of `paid` and `open`
  do not agree.
- Numbers agree to nine significant digits, and anything within 1e-9 of zero
  is zero.
- Two results that are both empty, or hold nothing but NULL, are not counted:
  they would agree whatever the queries were looking for.
- A query whose result can change from run to run (`LIMIT` with no `ORDER BY`
  that actually cuts rows off, `random()`, a sample of rows) is not compared.

The value of this signal depends on how independent and how capable the
alternatives are. Samples from the same model and prompt as the candidate tend
to share its mistakes; a different model, or a different prompt, is better.
If you know how often each source is right (measure it on labelled queries of
your own), say so with `Scorer(..., source_weights={"gemma": 0.2,
"opus": 0.8})` (names match by substring). Then agreement is credited in
proportion to the agreeing sources' reliability, sources right less than half
the time can add little together, a lone disagreement from such a source is
only a note, and two reliable sources that agree with each other against the
query are a HIGH finding whatever the `consistency_role`.

### Reconciliation: anchor queries with a declared relation

The consistency stage asks "does another query give the same answer?". The
reconciliation stage asks something a weaker model can get right: "what
simple number must this answer agree with?" A probe is a one-value query plus
a relation to the candidate's result, for example `SELECT SUM(total) FROM
orders` with `equals_sum` over the answer's `spend` column, or `SELECT
COUNT(DISTINCT customer_id) FROM orders` with `equals_row_count`. Relations:
`equals_row_count`, `equals_sum`, `equals_count_distinct`, `equals_value`
(one answer row chosen by a `filter`), `equals_max`, `equals_min`,
`upper_bound`, `lower_bound`. Pass probes directly as
`scorer.sample(sql, question, probes=[...])`, or give the scorer a
`prober(question, schema_text, glossary, answer_columns)` that proposes them
in `full` mode (`sqlgami.contrib.hf.HFProber` is one). Probes are held to the
same gate as everything else and are never run if they are not single
read-only queries over known tables and functions. An anchor the result
violates is `reconciliation_mismatch` (MEDIUM; HIGH when two independent
sources proposed the same anchor value); anchors it satisfies add support.

sqlgami ships no model and calls none on its own. `sqlgami.contrib.hf` is an
optional generator for Hugging Face Inference, using only the standard
library:

```python
from sqlgami.contrib.hf import HFGenerator

generator = HFGenerator("meta-llama/Llama-3.3-70B-Instruct", dialect="postgres")  # token from HF_TOKEN
```

It has no default model, accepts only models from an allowlist of families
(see `ALLOWED_PREFIXES` in the module), and never logs or prints the token.
Using it sends your question, a description of your schema and your glossary
to the provider.

### Business terms: the term catalogue

"Not on leave", "the last 26 pay periods", "turnover in basis points" mean
whatever your data engineers say they mean. sqlgami ships no vocabulary of its
own: you declare your terms and KPIs in a spreadsheet (one `.xlsx` with the
sheets below, or the same sheets as CSV files in a folder — the form that
diffs in git), and every question phrase that matches a term is checked
against the term's data-level meaning. A term the catalogue does not define in
data terms is reported as `undefined_term` and caps confidence at 0.75:
sqlgami never guesses what a word means.

`Terms.csv` (one row per term; `phrases` separated by `;`, `{n}` a number from
the question, `{manager_id}` a template parameter; an optional `qualifiers`
column lists single words a question may put inside a wording without
changing it — `biweekly;authoritative` lets "trailing 8 biweekly pay periods"
match "trailing {n} pay periods"; nothing is inferred without it):

```csv
term,phrases,kind,table,sql,nulls,description
not_on_leave,not on leave,filter,hr.emp_period_snapshot,status_cd IS DISTINCT FROM 'L',include,NULL status is not on leave
last_n_pay_periods,last {n} pay periods,window,hr.pay_period_dim,period_seq BETWEEN -({n}-1) AND 0,,
n_pay_periods_ago,{n} pay periods ago,window,hr.pay_period_dim,period_seq = -{n},,
hire_date,hire date;hired in,source,,hr.employee_dim.hire_dt,,the snapshot's copy may be stale
my_organisation,my organisation;my org,chain,hr.emp_period_snapshot,"lvl_2_id, lvl_3_id, lvl_2_id_fn, lvl_3_id_fn = {manager_id}",,
top_dense_ranks,top {n} dense-ranks;top {n} dense ranks,rank,,DENSE_RANK() <= {n},,ties kept
headcount,headcount,,,,,documentation only: matched but not enforced
```

`KPIs.csv`: `kpi,phrases,formula,format,requires,reference_sql,description`, for
example `basis_points,basis points;bp,CAST(FLOOR({rate} * 10000) AS BIGINT),integer floor,,,`;
`requires` lists the terms a KPI needs (`revenue` requires `paid_or_partial;not_deleted`),
each checked as if the question had asked for it.
`Parameters.csv` names template parameters and formula inputs
(`manager_id,integer,12003` / `rate,input,`); `Examples.csv` holds training
questions with their correct SQL for the offline compile step.

```python
scorer = Scorer("duckdb", db=conn, terms="metadata/terms/")   # or "metadata/terms.xlsx"
```

Kinds and what is checked, by structure rather than text: `filter` — an
equivalent condition on that column is present, NULL behaviour included
(`status_cd != 'L'` against `IS DISTINCT FROM 'L'` with `nulls: include` is a
HIGH `term_contradicted`, a missing one `term_missing`, a different filter on
the column `term_differs`); `window` — the query's range on the column has
the declared size or offset (`BETWEEN -26 AND 0` for "last 26 periods" is
`term_window`, frames `ROWS BETWEEN n PRECEDING` too); `source` — the column
is read from the table named (`term_source`); `chain` — an access OR-chain
over the listed columns (judged as a required predicate); `rank` — a "top
{n}" is cut the declared way (`LIMIT {n}`, `DENSE_RANK() <= {n}`, `RANK() <=
{n}` or `ROW_NUMBER() <= {n}`, through CTE columns and QUALIFY) at the number
the question states: another number, another tie rule or no cut at all is a
HIGH `term_rank`; KPI `format`
`integer`/`floor` — the output column named for the KPI is cast or floored and
not rounded (`term_format`), and a formula `({current} - {prior}) ...` is not
subtracted the other way round (`term_formula`). Every match and outcome is in
`report.signals["terms"]`. `Catalogue.load(path).validate()` reports problems
by sheet, row and column; a catalogue with problems is refused. Reading
`.xlsx` needs `pip install sqlgami[excel]`; CSV needs nothing. The HR example
lives in `examples/terms/`.

### Reference fixtures: the compiled bundle

The strongest deterministic signal is a run against a reference. Offline,
`sqlgami compile config.yaml -o bundle/` (also `sqlgami train`) builds small
adversarial fixture datasets from your schema and declared rules (keys and
foreign keys from the catalogue, value domains, NULL rates, duplicates and
restatements, ties, empty groups, edge dates, the parameter values your
questions use, and rows you write down verbatim), runs every reference query —
the `Examples` sheet of the term catalogue plus any `references:` in the config
— on every fixture, and writes a plain, versioned folder: `manifest.json`,
`schema.sql`, `terms/`, `fixtures/<name>/*.csv`, `references.json`,
`rejected.jsonl` (references whose query failed on a fixture; nothing enters
the bundle unverified). No model is called unless you ask for the optional
expansion step:

```yaml
expansion: {model: "hf:meta-llama/Llama-3.3-70B-Instruct", per_term: 6, per_example: 4}
```

Then — offline, and only here — a model proposes further wordings for every
term and KPI and paraphrases of every reference question, some with other
numbers. Nothing enters on its word: a wording is kept only if, added to the
catalogue, no registered question changes its pattern or slots, it does not
already occur in a question where it means something else, and a question
that uses the term re-matches to the same terms and slots with the new
wording in its place; a paraphrase only if it re-matches the reference's
pattern, binds the slots the model claims it does, states the same other
numbers, and the reference runs at those values on every fixture. Accepted
wordings go to `bundle/review/Terms.additions.csv` for you to paste into the
workbook (the live catalogue stays yours); accepted paraphrases become
variants in `references.json`, matched by text and serving their numbers
without a live re-run; everything else goes to `rejected.jsonl` with the
reason. Models: `hf:<name>` (allow-listed families, `HF_TOKEN`) or
`claude:<name>` (`pip install anthropic`, `ANTHROPIC_API_KEY`).

```yaml
dialect: duckdb
schema: schema/duckdb.sql
terms: metadata/terms/
fixtures:
  seed: 1
  profiles: [clean, nulls, duplicates, ties, sparse, edges]
  rows: {hr.emp_period_snapshot: 400}
  values: {hr.emp_period_snapshot.status_cd: [A, L, T]}
  params: {manager_id: [12003]}
  param_columns: {manager_id: [hr.emp_period_snapshot.lvl_3_id]}
```

Live, `Scorer("duckdb", db=conn, bundle="bundle/")` loads only the bundle (its
schema hash must match the catalogue; the fixtures go into in-memory DuckDB
once, at construction). For a question with a reference — chosen by
`score(..., reference="key")`, else by the exact question text, else by the
question's *pattern* — the set of catalogue terms it matches, closed over what
the catalogue declares (a KPI's `requires`, the shorter phrases a matched
phrase contains), plus the identifiers it names outside those phrases
(`period_seq`, `lvl_3_id`: exact tokens, so a question naming other columns
is another question) — when that pattern belongs to exactly one reference;
two references with one pattern are reported as ambiguous and neither is used —
the candidate runs on the fixtures while the deadline allows, under the
question's own slot and parameter values, and its result fingerprint is
compared with the stored one: a match on the fixtures run is
`likely_right`-grade support; a mismatch is `fixture_mismatch` (HIGH,
`likely_wrong`) with the differing rows as evidence, and the remaining
fixtures are not run. A reference that returns no rows on a fixture proves
nothing there and is skipped. Values are never guessed: a reference written
over `{n}` is re-run live for a question that binds another `n`; a reference
fixed at one value, a question that binds no value for a slot, or a question
and reference that state different numbers no slot accounts for ("top five"
with no rank term) is `not_run` with the reason. `compile` lists references
whose text states such a number ("if four nodes tie…"), so the host can
reword them or write the number as a slot. Fixtures can also be
cut from a real database (`sqlgami.fixtures.from_connection`); that is an
explicit choice, since those rows then live in the bundle.

#### BigQuery

A BigQuery statement is scored the same way, with no call to the service:
the static and term checks run on the bigquery dialect, and the fixture stage
runs the statement on the DuckDB fixtures through a **verified translation**
(`sqlgami.translate`). Verified means: every function, clause, type and date
part the statement uses is on an allow-list whose entries each have a test
proving the translated result equals BigQuery's on fixture data
(`tests/test_bigquery_translation.py`; the same cases are re-run against the
real service in the sandbox with `SQLGAMI_BQ_LIVE=1`). A construct that is not
on the list leaves the fixture stage `not_run` with `untranslatable:
func:JSON_EXTRACT_SCALAR`, never a verdict. Your `` `project.dataset.table` ``
names map to the fixture tables through `tables:` in the compile config
(every three-part name in your own DDL is mapped automatically), and the
live catalogue resolves them the same way. Fixture connections run in UTC, as
BigQuery does. Translations the proof forced: `COUNTIF` becomes
`COALESCE(COUNT_IF(…), 0)` (DuckDB gives NULL for an all-NULL group).

`sqlgami.contrib.bigquery` (`pip install "sqlgami[bigquery]"`) is an
**offline** adapter for dry runs and native reference runs; it refuses any
project other than the sqlgami sandbox before a client is built, uses
application-default credentials only, and records every dataset it creates.
`score()` never uses it.

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
- A read-only query can still do harm through what it calls: read a file,
  change a setting, take a lock. So a statement is sent only if every function
  it calls is on an allow-list of that engine's built-in functions that do
  nothing but compute (about 150 for SQLite, 900 for DuckDB, 1,550 for
  Postgres; see `sqlgami/_functions.py`). The rule covers the query under
  review and every alternative query alike.
  - A built-in that reaches outside the tables (`read_csv`, `pg_read_file`,
    `current_setting`, `nextval`, ...) is reported as `external_access` and the
    statement is not sent.
  - A function that does not exist is reported as `unknown_function`, from the
    database's catalogue, without sending the statement.
  - A function that exists but is not on the list, such as one of your own,
    is reported as `unlisted_function`: the statement is analysed but not run.
    Vouch for it with `Scorer(..., allowed_functions=["my_schema.my_function"])`.
  - `SELECT ... FOR UPDATE` and `FOR SHARE` are refused, as are DuckDB file
    paths written where a table name belongs.
- Which tables a query may read is an allow-list too. With a catalog (DDL or a
  connection), a table the catalog does not list is `unknown_table` and the
  statement is not sent. The engine's own relations are refused by default
  whatever the catalog says: `pg_catalog.*`, `information_schema.*`, bare
  `pg_*` names, `sqlite_*` and `pragma_*` (with or without parentheses),
  `duckdb_*`; a read of `pg_shadow` or `pragma_database_list` is
  `external_access`. Vouch for what you need with
  `Scorer(..., allowed_tables=["information_schema.*"])`.
- These checks rest on parsing the statement. They are a strong filter, not a
  sandbox: give sqlgami a database role that can only read what it should.
- A database opened by path is opened read-only. On a SQLite connection you
  pass in, writes are disabled for the duration of each call and restored
  afterwards.
- On Postgres every statement runs in its own `READ ONLY` transaction, so a
  function with side effects cannot write. If the connection you pass in is
  already inside a transaction, sqlgami works under a savepoint and leaves your
  transaction as it found it; the statement gate still applies, but the
  read-only setting is then yours to make.
- Every statement runs under a time limit (`Options.timeout_s`, 15 seconds for
  the query under review, less for probes) and returns a bounded number of
  rows, and one report has a budget for all of its statements together
  (`Options.budget_s`, 120 seconds). The bound is enforced, not hoped for:
  SQLite stops through its progress handler and Postgres through
  `statement_timeout`; DuckDB runs each statement on a worker thread, is asked
  to stop with `interrupt()` repeatedly for a grace period, and if it still runs
  (DuckDB has been seen to ignore the request inside a recursive CTE) the
  statement is abandoned and a fresh cursor serves the next one. Under a live
  deadline the grace is the few milliseconds the call can spare, so the cursor
  is abandoned at the deadline rather than waited for, and the fixture stage
  keeps 15 % of the deadline back for the stages after it. A statement
  that was cut short is a `timeout` outcome and never counts as verified; a
  report that ran out of budget carries a `budget_spent` note.
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

### Latency

`score()` is meant to run inside agent loops: the static path (parse, gate,
catalogue, checks, terms) is measured by `python -m benchkit latency <suite>
--mode static --mode sample --stretch 40-80`, which pads the suite's gold
statements to production size and prints p50/p90/p99 per mode;
`tests/test_latency.py` guards the static p90 in CI. On a developer machine
the static path runs in about 10-20 ms for 40-80-line statements.

### Bench

`bench/` holds test suites: realistic schemas, deterministic data generators,
questions with gold SQL, planted bugs and equivalent rewrites. `benchkit/` is
the tooling around them. It is not part of the installed package.

```
uv run python -m benchkit build saas_billing       # generate data, build SQLite and DuckDB databases
uv run python -m benchkit validate saas_billing    # check gold, bugs and equivalents against each other
uv run python -m benchkit evaluate saas_billing    # score everything with sqlgami, print metrics
uv run python -m benchkit label saas_billing bench/.runs/<run-id>   # label candidates.jsonl by execution
uv run python -m benchkit evaluate saas_billing --engine duckdb --mode sample \
    --candidates bench/.runs/<run-id>/candidates.jsonl --consistency   # other generators' SQL as alternatives
```

`--verifiers NAME` (repeatable) limits which generators may act as
alternatives, to measure a realistic setup.

```
uv run python -m benchkit audit saas_billing bench/.runs/<run-id>/candidates.jsonl
```

audits the gold itself: where two strong generators (by default, any whose
name contains `claude`; `--strong` changes that) return the same result as
each other on every seed and differ from gold, the gold or the wording is the
suspect. `validate --candidates FILE` runs the same audit after the other
checks. Results are compared positionally, except that when both sides name
every column once with the same names, the columns are first lined up by
name.

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
