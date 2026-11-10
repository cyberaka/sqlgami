# BigQuery red-team — handover

For the next stress-testing agent. Branch `checker/bq-redteam`, cut from `origin/main` @ `0551f10`.
Everything here is **tests only** — no `src/` changes. Each defect is a `xfail(strict=True)` test:
green now, and it flips to a failure the moment the builder closes the hole (prompting the marker's
removal). As of this writing the builder has landed no BigQuery fixes, so **every xfail below is
still open on main**.

Run the whole set: `.venv/bin/python -m pytest tests/test_redteam_bq_*.py -q`
(the `.venv` has sqlgami + sqlglot 28.10.1 + duckdb; `uv` is not installed — use `.venv/bin/python`).

## Sandbox rules (non-negotiable)
- BigQuery project **`sqlgami-dev-1109` only** (the `sqlgami-` sandbox, billing disabled). Pass
  `--project_id=sqlgami-dev-1109` explicitly; never rely on the ambient `gcloud` default.
- **Read-only** against the builder's `bench_hr_snapshot_s{seed}_hr[_ref]` datasets. Any dataset you
  create uses the **`bench_checker_`** prefix and is recorded for cleanup. This red-team created **no**
  datasets (inline-data queries + read-only reference runs only).
- Auth is ADC (`cyberaka@gmail.com`); no key files. INFORMATION_SCHEMA / JOBS reads are free.
- Never fetch held-out suites (see below).

## Batches, method, harness
Harnesses live in the session scratchpad (`scratchpad/bq/*.py`); they reuse sqlgami's own machinery
so the results mirror the real pipeline: `benchkit.suite.load_suite`, `bqnative._table_map` /
`_rewrite`, `benchkit.build.connect` (DuckDB fixtures incl. views), `sqlgami.translate` + the script
binder, and a `benchkit.compare.same_rows`-style normalized multiset compare.

- **Batch 1 — safety gate** (`test_redteam_bq_safety.py`, 16 xfail): the root cause for the whole
  function side is that `safety._LISTS` has **no `bigquery` entry**, so `review_functions` /
  `review_text` never mark a bigquery function refused/unlisted beyond `REFUSED_EVERYWHERE`, and the
  `_SYSTEM_TABLE_SCHEMAS["bigquery"]={"information_schema"}` guard never fires (sqlglot folds
  `INFORMATION_SCHEMA.X` into one quoted identifier). Sent-when-they-should-not-be: EXTERNAL_QUERY
  (federation, HIGH), every INFORMATION_SCHEMA form (HIGH, runtime-proven to disclose other jobs'
  query text), ML.PREDICT, unknown TVF/UDF incl. dashed/backtick, wildcard `_TABLE_SUFFIX`,
  `FOR SYSTEM_TIME AS OF`, SESSION_USER. Correctly contained by the *kind* gate: EXECUTE IMMEDIATE,
  CALL, BEGIN…END, CREATE TEMP FUNCTION/TABLE, EXPORT DATA, SET, DECLARE-smuggling scripts.
- **View/TVF bodies** (`test_redteam_bq_view_bodies.py`, 5 xfail + 3 skips): `safety.review` runs on
  the raw candidate tree (analysis.py:216) **before** the resolver expands views (220+), so a view
  body hiding EXTERNAL_QUERY / INFORMATION_SCHEMA / an unknown function is not refused — even through
  view→view→view nesting. TVF cases skip until **B4** (the catalogue can't hold `CREATE TABLE FUNCTION`
  bodies yet) and auto-activate when it lands.
- **Batch 2 — translation fidelity** (`test_redteam_bq_translation.py` +2/+3/+4b): native sandbox vs
  `translate`→DuckDB. Confirmed divergences on VERIFIED constructs: `DATE_TRUNC WEEK` (Sun vs Mon),
  `DATE_TRUNC WEEK(MONDAY)` (invalid DuckDB), `UNNEST … WITH OFFSET` (0- vs 1-based), DATE_ADD
  DATE→TIMESTAMP widening, NUMERIC→DOUBLE, LIKE backslash escaping, div-by-zero (BQ errors vs DuckDB
  `inf`), array OFFSET OOB (BQ errors vs NULL), a script var shadowed by a column (silent wrong bind),
  DECLARE STRUCT (broken SQL), TABLESAMPLE/APPROX_COUNT_DISTINCT (must be not_run), INTERVAL rendering.
  Correct `not_run`: REGEXP_EXTRACT/REPLACE, BYTE_LENGTH, SPLIT(''), BIGNUMERIC, COLLATE, DATETIME(ts,tz),
  JSON_*, TO_JSON_STRING, DATETIME_DIFF/TIMESTAMP_DIFF (the last two are a coverage gap worth closing).
- **Batch 4A — whole-query differential** (`test_redteam_bq_groupby_alias.py` + `bq_wholequery_4a_summary.json`):
  224 real LLM candidates over the HR schema, native vs translate→DuckDB on 3 seeds. **Translation is
  sound**: ~492 comparisons, 0 real value disagreements. One real bug: a `GROUP BY` naming a value that
  is both a SELECT alias and an in-scope column resolves to the alias in BigQuery but the column in
  DuckDB; the **script binder amplifies** it by rewriting `GROUP BY pp.ppe_dt` → the colliding alias
  `period_end_dt`, so a correct script errors on DuckDB.
- **Batch 5 — false alarms** (`test_redteam_bq_false_alarms.py`, 1 xfail): `required_predicate` (HIGH)
  false-fires when the hierarchy OR-chain uses `lvl_X_id IN (subquery)` instead of `= <literal>`. The
  owner's other idioms (QUALIFY, UNNEST joins, ARRAY_AGG(STRUCT), SELECT * EXCEPT/REPLACE, COUNTIF,
  COUNT(DISTINCT IF), SAFE_DIVIDE, scalar-subquery date bounds, DECLARE/SET, view reads, backtick-dashed)
  raise no HIGH — guarded.
- **Batch 6 — gold/bug scoring** (`test_redteam_bq_false_alarms_gold.py`, 2 xfail + `bq_gold_bug_batch6_summary.json`):
  scored real dev gold (static). Two HIGH false alarms on the owner's own gold: `required_predicate`
  misses the **UNNEST-pivot** scope (q031), and `term_rank` misreads an incidental `LIMIT 1` in a
  current-period scalar subquery as the ranking construct (q025). Measurements: golds median **0.536**,
  max 0.588 (none "confident"); static bug catch rate **8/15 (53%)** — value bugs escape static and need
  the fixtures stage; **real-gold** false-alarm rate on the 224 is **1/63 (1.6%)**.

## xfail inventory by area (40 total, all open on main)
- safety gate 16 · view bodies 5 (+3 TVF skips, B4) · translation 15 (b1 3, b2 5, b3 4, b4b 3) ·
  whole-query 1 · false alarms 3 (batch 5: 1, batch 6: 2).

## B6 case files and re-deriving native rows
`tests/data/bq_translation_cases*.jsonl` (`{id, construct, sql, native_rows, …}`) feed the builder's
differential fuzz (B6); `*_summary.json` carry the 4A/5/6 measurements. Native rows are re-derivable:
each translation test has an **opt-in runtime test** (`test_native_values_still_hold`) that re-runs the
cases on the sandbox and is **skipped unless the sandbox is configured** — set
`SQLGAMI_BQ_PROJECT=sqlgami-dev-1109` (the intended live gate; wire it to `SQLGAMI_BQ_LIVE` if the
builder standardizes that) and run e.g.
`SQLGAMI_BQ_PROJECT=sqlgami-dev-1109 .venv/bin/python -m pytest tests/test_redteam_bq_translation_batch2.py -q`.
The scratchpad harnesses (`scratchpad/bq/fidelity*.py`, `diff4a.py`, `batch6_*.py`) regenerate the
jsonl end to end.

## Harness pitfalls (learned the hard way)
- **`bq` multi-statement CSV headers**: a DECLARE script prints a per-statement header row that a naive
  CSV parse counts as data (`native_n == duck_n+1`). This produced 144 *false* "disagreements" in 4A
  until caught; strip the header (or take the final statement) for scripts.
- **Consensus label ≠ real gold**: batch 5 labelled candidate correctness by native-result consensus
  (dev gold wasn't in the repo) and got 126 "correct"; the real gold (batch 6) labelled **63**. Use the
  real gold; the consensus proxy is looser.
- **`glossary` must be a `str`**, not a `Path` — the question stage concatenates it; a `Path` throws
  `TypeError` only once a question string is passed (it's silent with `question=None`).
- Watch your own SQL: a duplicated alias (`FROM t AS e AS e`) surfaces as a sqlgami `parse_error` and
  looks like a false alarm — verify valid BigQuery with sqlglot + a `--dry_run` before reporting.
- `benchkit` isn't importable from `/tmp`; run harness scripts with
  `PYTHONPATH=/Users/cyberaka/projects/Github/sqlgami`.

## Held-out boundary
Dev/released (OK to use): holdout `hr_snapshot_w4, w4v, w4v2, w5` (gold in `questions.jsonl`
`gold_sql.bigquery` / `bigquery_script`; bugs in `bugs.jsonl` `sql_bigquery`). **Never fetch**
`w5v, w6, w6v, w7` or any `*_v` held-out variant.

## Highest-value next attacks
1. **TVF bodies once B4 lands** — the 3 skipped view-body tests activate automatically; extend to
   nested TVF-over-view-over-TVF and dashed/backtick TVF resolution.
2. **Long-form procedural scripts** — multi-`DECLARE` + `IF`/`WHILE`/`LOOP` + `SET` (the w5 native-only
   golds). Confirm the binder keeps declining (not_run) and never silently binds control flow.
3. **The fixtures catch-rate run** (started, stopped at 77% usage): `benchkit compile hr_snapshot
   --engine bigquery --from-seeds --references <w4/w4v2 golds>` then re-score gold+bugs with
   `Scorer(bundle=…)` at unlimited deadline **and** 50 ms — does the answer check catch the 7 static
   value-bug misses, and do golds clear 0.8? (The compile subsamples all nasty seeds, so it's slow;
   budget for it.)
4. **Latency under load** — `benchkit latency` p50/p90/p99 of `score()` per mode; watch the 50 ms deadline.
5. **The two-tier API** — static vs sample/fixtures parity; confirm a verdict doesn't flip unsoundly
   between tiers on the same candidate.
