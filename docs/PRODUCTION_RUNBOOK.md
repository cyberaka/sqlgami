# Production runbook — running sqlgami beside a live BigQuery warehouse

Written for a coding agent on a work laptop. Imperative, short. Read all of page one before
doing anything.

## Page one: what scoring does and does not do

- `Scorer.score()` **never connects to the warehouse**. It reads the SQL text, the DDL you
  exported, the term workbook and the compiled bundle, all local files. There is no network
  call at score time: no BigQuery, no model, no telemetry. Verify: run a score with the network
  off; it must behave identically.
- Scoring is **read-only and offline** by construction: the fixture stage runs the statement
  on small synthetic DuckDB tables inside the process, never on production tables.
- Nothing is written anywhere except the files you ask for (`bundle/`, your own logs).
- The report is advisory (`docs/guarantees.md`). A number is an ordering, not a probability.

## Data handling — rules, not preferences

- Fixtures are **synthetic only**: built from the schema plus the rules you declare. Do **not**
  call `sqlgami.fixtures.from_connection` and do not use `--from-seeds`/`subsample:` against
  production rows unless the owner's organisation has explicitly approved it in writing.
- No employer SQL, schema, terms, questions or results leave the laptop. The bundle, the
  workbook, the logs stay in the work repository, not in this one.
- When reporting back to the sqlgami maintainers, send **aggregates only**: counts, rates,
  latency percentiles, finding codes. Never a query, a table name, a column name, a value.
- Never commit real data, DDL or terms into the sqlgami repository. A reproduction for an issue
  is rewritten on a synthetic schema first.

## Expansion is OFF unless approved

The `expansion:` block of the compile config sends catalogue phrases and reference questions to
an external model (Hugging Face router or Anthropic). At work, **leave it out of the config**.
Enable it only with a model the organisation has approved, and only offline, at compile time.

Verify it is off: `grep -n expansion sqlgami.yaml` prints nothing; after a compile,
`python -c "import json;print(json.load(open('bundle/manifest.json'))['expansion'])"` prints
`{}` or `None`. `sqlgami doctor sqlgami.yaml` lists an `expansion:` line only when one is set.

Likewise the consistency stage with a model (`Scorer(generator=…)`, `prober=…`): off by
default; do not turn it on.

## The BigQuery helper guard

`sqlgami.contrib.bigquery` (dry runs, native runs, loading) refuses any project whose name does
not start with `sqlgami-`, before a client is built. This is intentional. **Do not edit or
bypass it at work.** Scoring does not need it; `benchkit` commands that use it are for the
maintainers' sandbox. If dry runs against the warehouse are wanted, that is a later change the
owner approves; it is not part of this release.

## Pilot, step by step

(a) **Export the DDL.** In the BigQuery console, per dataset (replace `project.dataset`):

```sql
SELECT ddl FROM `project.dataset`.INFORMATION_SCHEMA.TABLES ORDER BY table_name;
SELECT ddl FROM `project.dataset`.INFORMATION_SCHEMA.ROUTINES ORDER BY routine_name;
```

Save every `ddl` value, one statement after another, into `schema.sql` (tables and views
first, then routines). Keep the three-part names as they are.

(b) **Init and doctor.** `sqlgami init pilot` writes the template; replace `pilot/schema.sql`
with yours; `sqlgami doctor pilot/sqlgami.yaml --queries <folder of your agent's .sql files>`
reads your statements locally (never executes them) and lists every construct outside the
verified allow-list. A statement using one gets no fixture verdict (`not_run`), never a wrong
one. Note the list; it tells you how much of your SQL the fixture stage can judge.

(c) **Terms.** With the data team, write `Terms.csv`, `KPIs.csv`, `Parameters.csv` in the
organisation's words (`docs/pilot.md` §2). Numbers in wordings are `{n}` or a named numeric
parameter. Run `sqlgami doctor` until the workbook validates to `[]` (no problems).

(d) **References.** Write a correct query for each of the top 10–20 question patterns in
`Examples.csv`, templated over their slots, with the question as people ask it.

(e) **Fixture rules.** For every period/snapshot table declare `date_step`, `one_true`,
`sequence`, `grid` and `derive` rules (`docs/pilot.md` §2, "Fixture rules"); set `anchor` to the
real current period end; put the ids the questions use under `params:`. Without these the
synthetic data is meaningless.

(f) **Compile.** `sqlgami compile pilot/sqlgami.yaml -o pilot/bundle`. Read
`bundle/rejected.jsonl` (references the translation could not run) and the compile notes.
Commit the bundle to the **work** repository beside the workbook; rebuild when either changes.

(g) **Collect.** For every agent-written statement record the SQL, the question, the template
parameters, and later the outcome (accepted / edited / rejected; found wrong?).

(h) **Score in shadow mode.** One `Scorer("bigquery", catalog=<schema.sql text>,
bundle="pilot/bundle")` per process; `report = scorer.score(sql, question, params=…)`. Log
`score`, `verdict`, high finding codes, `signals.fixtures.verdict`, `timing.cut_by_deadline`,
`timing.total_ms`, `signals.fixtures.bundle`. Score each statement twice: at the default
50 ms deadline, and again with `Options(deadline_ms=None)` (every fixture runs), so the
deadline's cost can be seen. Change nothing in the application.

(i) **Label.** Have a reviewer label a sample (right / wrong), stratified by score band.

(j) **Compute.** Catch rate = wrong statements scored ≤ t / all wrong; false-alarm rate =
right statements scored ≤ t / all right; latency p50/p90/p99; share `cut_by_deadline`;
`not_run` reasons by count.

(k) **Choose a threshold** from (j) (`RELEASE.md` §7). Report the aggregates.

## Never

- Write to production, or run anything against it from sqlgami.
- Change the project guard, the safety gate, or the allow-list to make something pass.
- Turn on expansion or model-backed consistency without approval.
- Commit real data, DDL, terms, questions or results into the sqlgami repository.

## Known open issues to watch (details in `HANDOVER.md`)

- **H13 — false confidence:** on one held-out set, ten wrong candidates got a fixture "match"
  (four scored ≥ 0.8). Treat a high score as "nothing found", not "right"; the human label in
  step (i) is what measures this.
- **H14:** a `term_missing` HIGH can fire when a filter term is applied inside a subquery.
- **Long scripts at 50 ms:** the fixture stage is often cut (`cut_by_deadline`), leaving the
  score static-only (~0.5–0.6 for a clean statement). Use `Options(deadline_ms=None)` for a
  nightly batch; the two-tier API (H1) is not in this release.
