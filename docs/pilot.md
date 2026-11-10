# Pilot: sqlgami in shadow mode on a BigQuery snapshot schema

A shadow-mode pilot means: your application keeps doing what it does; sqlgami scores every
LLM-written statement on the side, you log the report next to what happened, and after a few
weeks you decide what the number is worth. Nothing in this setup sends data anywhere — no row
of your tables is read, no query runs on BigQuery, no model is called at scoring time.

sqlgami's report is advisory. It is not a guarantee of correctness and comes with no
warranty; the report says so itself.

## 1. Install

The repository is private; install from it over SSH (the machine needs read access):

```
python -m venv .venv && . .venv/bin/activate
pip install "sqlgami[pilot] @ git+ssh://git@github.com/cyberaka/sqlgami.git@main"
sqlgami --version
```

`[pilot]` brings DuckDB (the fixture engine, embedded, in-process), PyYAML (the compile
config) and openpyxl (reading a `.xlsx` workbook; CSV sheets need nothing). No BigQuery
client is installed or needed.

## 2. Start from the template

```
sqlgami init pilot
```

writes a BigQuery snapshot-style starting point into `pilot/`:

| file | what it is |
|---|---|
| `schema.sql` | your DDL — `CREATE TABLE \`project.dataset.table\``, keys may be `NOT ENFORCED` |
| `terms/Terms.csv`, `KPIs.csv`, `Parameters.csv`, `Examples.csv` | the term workbook |
| `sqlgami.yaml` | the compile config: schema, workbook, table map, policies, fixture rules |

Replace the DDL with yours (keep the three-part names your queries use), then edit the
workbook. If you prefer Excel, keep the same four sheet names in one `terms.xlsx` and point
`terms:` at it.

### The workbook

Every business word your questions use gets one row. A row is only three things: the name,
the wordings people use (`;`-separated, `{n}` where a number goes), and what it means in data:

| kind | `sql` says | example |
|---|---|---|
| `filter` | a condition on the table's rows | `status_cd = 'A'` |
| `window` | a range on a sequence column | `period_seq BETWEEN -({n}-1) AND 0` |
| `source` | the column a value must come from | `hr.employee_dim.hire_dt` |
| `chain` | an access chain the question scopes by | `lvl_2_id, lvl_3_id, … = {manager_id}` |
| `rank` | how a "top {n}" is cut | `DENSE_RANK() <= {n}` |

`nulls: include` says NULLs belong to the filter (`IS DISTINCT FROM`). `qualifiers` lists
single words a question may put inside a wording (`trailing 8 biweekly pay periods`). A row
with no `sql` is documentation only: it is matched, nothing is enforced, and the confidence
is capped at 0.75 until you define it. **Nothing is guessed**: a word not in the workbook is
reported as undefined, never interpreted.

`KPIs.csv` names measures (`headcount = COUNT(DISTINCT {assoc_id})`, `requires: active`).
`Examples.csv` holds questions with a correct query each — these become the references the
fixture stage compares candidates against. Write them as you would ask them, with `{n}` and
`{manager_id}` where the values vary.

### The table map

Fixtures are named `dataset.table`. Every three-part name in `schema.sql` is mapped
automatically (`` `my-project.hr.emp_period_snapshot` `` → `hr.emp_period_snapshot`). Add
`tables:` entries only for names your queries use that the DDL does not declare:

```yaml
tables:
  "`my-project.hr_views.current_snapshot`": hr.emp_period_snapshot
```

### Fixture rules — synthetic only, by default

`compile` builds small datasets from the schema and these declarations alone. Period and
snapshot tables need rules, or the synthetic data is meaningless (several "current" periods,
`period_seq` unrelated to dates). The template declares the usual ones:

```yaml
fixtures:
  rules:
    - {kind: date_step, column: hr.pay_period_dim.ppe_dt, per: [freq_cd], step: {B: 14 days, W: 7 days}, anchor: "2026-06-20"}
    - {kind: one_true,  column: hr.pay_period_dim.is_current_flag, per: [freq_cd], at: middle}
    - {kind: sequence,  column: hr.pay_period_dim.period_seq, order_by: ppe_dt, per: [freq_cd], zero_where: is_current_flag}
    - {kind: derive,    column: hr.pay_period_dim.period_start_dt, sql: "ppe_dt - INTERVAL 13 DAY"}
    - kind: grid        # one snapshot row per employee per biweekly period within tenure
      table: hr.emp_period_snapshot
      per: {assoc_id: hr.employee_dim.assoc_id, period_end_dt: hr.pay_period_dim.ppe_dt}
      where: {hr.pay_period_dim.freq_cd: B}
      between: {column: period_end_dt, start: hr.employee_dim.hire_dt, end: hr.employee_dim.term_dt}
      snapshot: {column: snap_dt, equals: period_end_dt, restated_rate: 0.3}
```

Set `anchor` to your real current period end date, so a query that hard-codes it still
means something on the fixtures. Put the ids your questions use under `params:` and the
columns they select on under `param_columns:`; rows that must exist verbatim (a manager's own
`hierarchy_dim` row) go under `critical_rows:`. Cutting fixtures from real data is possible
(`sqlgami.fixtures.from_connection`) but is an explicit opt-in: those rows then live in the
bundle, with whatever they contain. The pilot does not need it.

## 3. Check, then compile

```
sqlgami doctor pilot/sqlgami.yaml --queries pilot/sample_queries/
```

reads the config, validates the workbook sheet by sheet (row and column named), the fixture
rules against the schema, and the table map; with `--queries` it reads your own `.sql` files
locally — never executing them — and lists every construct the fixture translation has not
verified. A query using such a construct gets **no fixture verdict** (the stage is
`not_run: untranslatable: func:X`), never a wrong one; the static and term checks still run.
`DECLARE … DEFAULT` / `SET` scripts are read as parameters and the final `SELECT` is scored.

```
sqlgami compile pilot/sqlgami.yaml -o pilot/bundle
```

writes `pilot/bundle/`: the fixtures as CSV, the references' results on each fixture, a copy
of the workbook, a manifest with a content hash. It is plain data; keep it next to the
workbook, rebuild it whenever either changes (the live scorer refuses a bundle built for a
different schema). Optional, offline only: an `expansion:` block lets a model propose further
wordings and paraphrases — what it proposes is kept only when it re-matches deterministically,
accepted wordings go to `bundle/review/Terms.additions.csv` for you to paste into the workbook,
and the live path never sees the model.

## 4. Score, log, don't act

```python
from sqlgami import Scorer

scorer = Scorer("bigquery", catalog=open("pilot/schema.sql").read(), bundle="pilot/bundle")

report = scorer.score(sql, question, params={"manager_id": 12003})   # ≤ 50 ms, deterministic
log.info("sqlgami", extra={
    "score": report.score,                 # 0..1, advisory
    "verdict": report.verdict.value,       # likely_right | uncertain | likely_wrong
    "high": [f.code for f in report.findings if f.severity.value == "high"],
    "fixtures": report.signals.get("fixtures", {}).get("verdict"),   # match | mismatch | unverified
    "cut": report.timing["cut_by_deadline"],
    "ms": report.timing["total_ms"],
    "bundle": report.signals.get("fixtures", {}).get("bundle"),
})
```

One `Scorer` per process; it loads the bundle once. `report.to_json()` has everything,
including why a stage did not run. Keep the default 50 ms deadline for the application path;
for a nightly batch over the day's statements use `Options(deadline_ms=None)` and
`mode="static"` — on BigQuery there is no live database stage, the fixtures are the data.

What to log beside it: whether the statement was accepted, edited or rejected by the person
who asked, and (if you can) whether the number it produced was later found wrong. That is the
label the pilot is for.

## 5. After a few weeks: choosing a threshold

Take the logged `(score, outcome)` pairs and look at two numbers for a candidate threshold
`t`: of the statements scored **below** `t`, how many were in fact wrong (what you would have
caught); of those scored **at or above** `t`, how many were wrong (what slipped through).
In our benchmarks the scores have been well ordered — wrong statements that run and return
plausible rows mostly sit at or below 0.4, and nothing wrong has sat at or above 0.8 when a
fixture verdict was behind it — but your schema, your workbook and your users' questions
decide, so read your own numbers before trusting ours.

Things to look at before the threshold:

- `undefined_term` notes: the words people used that the workbook does not define. Define
  them; each one lifts the 0.75 cap for that question.
- `fixtures: not_run` reasons: "no reference matches this question" means a question pattern
  you have no example for — add one to `Examples.csv`; "no reference for these values"
  means a number in the question no term binds — give the term a `{n}`.
- `untranslatable` reasons: constructs the pilot's queries use that the fixture translation
  has not verified. Tell us which; verification is a proof against the real service and is
  added per construct.
- The share of calls `cut_by_deadline`: if the fixture stage is often cut, the fixtures are
  heavier than they need to be (`rows:`), or the machine is loaded.

Nothing above changes the application's behaviour; the pilot ends with a number you can
defend and a workbook that says, in your organisation's words, what the data means.
