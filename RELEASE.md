# sqlgami v0.1.0-rc1 — first release for a production test (BigQuery, shadow mode)

**Running it beside a live warehouse? Read [`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md)
first** — page one is what scoring never does; then the step-by-step pilot.

This is the first build meant to run beside a real application. The recommended way to try it
is **shadow mode**: score every LLM-written BigQuery statement on the side, log the report, change
nothing in the application; after a few weeks the logged scores and outcomes say what the number
is worth for that schema and those users. It is a release candidate for testing, not a product.
The repository is private; nothing here is published.

sqlgami's report is advisory. It is not a guarantee of correctness and comes with no warranty;
the report says so itself. What a report does and does not promise is written in one page:
[`docs/guarantees.md`](docs/guarantees.md). Read it before reading a number. Overview
presentation: [`docs/pitch/sqlgami-pitch.html`](docs/pitch/sqlgami-pitch.html).

## 1. What it is

A Python library. You give it a SQL statement, the question it answers, and your schema; it
returns a report (a plain data object, JSON-serialisable) with a confidence value in 0..1, a
verdict, findings with codes and messages, and a list of the checks it could not run. Nothing in
the live path calls a model or a database: the live `score()` is deterministic and bounded by a
deadline (50 ms by default). The evidence behind the number comes from four places: the SQL text
(parse, static checks), your schema (names, keys, views, table functions), your **term workbook**
(what the business words mean in data), and a **bundle** compiled offline — small synthetic
fixture datasets plus the results of your reference queries on them, against which a candidate's
results are compared.

## 2. Supported engines and constructs

- **BigQuery** (the production target of this release): static checks, term checks, and fixture
  verdicts through a verified translation to DuckDB. The BigQuery service is never called at
  scoring time. Tables and **views** (including views over views and cross-dataset views) are
  read and expanded. **Table-valued functions** (`CREATE TABLE FUNCTION`) and **scalar SQL UDFs**
  are read from the DDL, expanded and run on the fixtures through the same translation; this
  path is tested on synthetic schemas and the sandbox's TVFs, **not yet on a production TVF
  library** — treat it as beta, and read `fixtures: not_run` reasons for TVF-reading
  statements. JavaScript UDFs leave the fixture stage `not_run`. Scripts (`DECLARE … DEFAULT`,
  `SET`, final `SELECT`) are bound and scored as their final query; `IF`/`WHILE` control flow
  is not run (`not_run`).
- The translation vouches only for constructs on an allow-list, each proven natively on the
  sandbox (`tests/test_bigquery_translation.py`, `tests/test_bigquery_boundary.py`). Not on the
  list today, so statements using them get **no fixture verdict** (never a wrong one):
  `ISOWEEK`, `DATETIME`, `TIMESTAMP_ADD/SUB/DIFF`, `DATETIME_DIFF`, JSON functions,
  `APPROX_COUNT_DISTINCT`, `ARRAY_AGG(… IGNORE NULLS …)`, `ARRAY_AGG(… LIMIT n)`, `ANY_VALUE(…
  HAVING MAX …)`, `TABLESAMPLE`, `RAND()`, `FORMAT_DATE` specifiers outside the proven set
  (`%e`, `%Q`, time specifiers), `DATE - DATE`, `TIMESTAMP_TRUNC` with a time zone. `sqlgami
  doctor --queries` lists which of *your* statements use an unverified construct.
- DuckDB, SQLite and Postgres have live adapters (dry run, sample, consistency) and are used
  for development and the bench; they are not the target of this release.

## 3. Install

```
python -m venv .venv && . .venv/bin/activate
pip install "sqlgami[pilot] @ git+ssh://git@github.com/<owner>/sqlgami.git@v0.1.0-rc1"
sqlgami --version
```

`[pilot]` brings DuckDB (the fixture engine, embedded), PyYAML and openpyxl. No BigQuery client
is installed or needed to score. Python ≥ 3.10.

## 4. Setting up: schema, workbook, references, fixtures, compile

Step by step in [`docs/pilot.md`](docs/pilot.md); the short form:

1. **Schema.** Export your DDL from BigQuery — the `ddl` column of
   `` `project.dataset`.INFORMATION_SCHEMA.TABLES `` (and `…ROUTINES` for TVFs and UDFs) —
   concatenate the statements into `schema.sql`. Keep the three-part names your queries use; every
   three-part name maps to a fixture table automatically; `tables:` in the config covers names the
   DDL does not declare. `OPTIONS(…)`, `PARTITION BY`, `CLUSTER BY`, `NOT ENFORCED` keys are read.
2. **Term workbook** (`terms/*.csv` or one `.xlsx`): one row per business word — its wordings,
   its kind (`filter`, `window`, `source`, `chain`, `rank`, or a formula/KPI) and what it means
   in data. Numbers in wordings are `{n}`, or a named numeric slot declared in `Parameters.csv`
   (`{n_months_back}`, `{tolerance_pp}` — digits, decimals or number words bind). Nothing is
   guessed: a word not in the workbook is reported as undefined and caps the score at 0.75.
3. **References** (`Examples.csv`, or `references:` in the config): questions with a correct
   query each, templated over their slots. These are what a candidate's fixture results are
   compared against. A reference using an unverified construct is rejected at compile and listed
   in `bundle/rejected.jsonl`.
4. **Fixture rules** (`fixtures:` in the config): period tables need `date_step`, `one_true`,
   `sequence`, `derive` and `grid` rules or the synthetic data means nothing; `unique_keys:`
   states uniqueness facts the database does not enforce (a `one_true` rule is one too).
5. `sqlgami doctor sqlgami.yaml --queries your_queries/` — validates every sheet, rule and the
   table map, and lists unverified constructs in your own queries. Then
   `sqlgami compile sqlgami.yaml -o bundle` — writes the fixtures, the references' results, the
   workbook copy and a manifest with a content hash. Optional, offline: an `expansion:` block
   lets a model (Llama/Mistral/Gemma/Phi/Granite/gpt-oss/Nemotron via Hugging Face, or Claude)
   propose further wordings and paraphrases; what it proposes is kept only when it re-matches
   deterministically, and the live path never sees the model.

## 5. Scoring, and the report fields

```python
from sqlgami import Scorer
scorer = Scorer("bigquery", catalog=open("schema.sql").read(), bundle="bundle")   # one per process
report = scorer.score(sql, question, params={"manager_id": 12003})              # ≤ 50 ms
```

`report.score` (0..1), `report.verdict` (`invalid`, `likely_wrong`, `uncertain`, `likely_right`,
`unverified`), `report.findings` (code, severity `error|high|medium|low|info`, message,
suggestion, snippet, evidence), `report.stages` (parse → statement → schema → static → fixtures
→ dry_run → sample → consistency → reconciliation → question; each `passed`, `failed`,
`not_run` or `skipped` with a detail), `report.checks_not_run`, `report.signals["fixtures"]`
(`verdict` match/mismatch/unverified, `reference`, `matched_by` key/question/pattern, the `slots`
and `params` used, per-fixture status, `bundle` hash), `report.signals["terms"]` (every matched
term's outcome), `report.timing` (`total_ms`, `cut_by_deadline`), `report.confidence`
(`value`, `raw`, `calibrated` — **false** in this release: the number is a provisional
heuristic that orders statements, not a probability; `cap` and `cap_reason` when a gate
capped it). `report.to_json()` has everything.

Caps that are rules, not evidence: does not parse → 0; cannot run (unknown table or column, not a
read-only query) → ≤ 0.1; a high-severity finding → ≤ 0.4; an undefined business term → ≤ 0.75
unless the fixtures agreed.

### The 50 ms deadline

The default `deadline_ms=50` bounds the whole call. Parse, static and term checks always run;
the fixture stage runs inside what is left (each fixture dataset is tried with a per-fixture
timeout and abandoned at the deadline). When the deadline cuts the fixtures, the stage is
`skipped` with "no fixture could be run in time", `report.timing["cut_by_deadline"]` is true,
and the score rests on static evidence only (typically 0.5–0.6 for a clean statement — the
prior of a statement nobody has run). On a loaded machine or a 60–80-line script this happens
in a noticeable share of calls (measured: 6–33 % over 50 ms at p99 88 ms). For a nightly batch
use `Options(deadline_ms=None)`: every fixture then runs. A two-tier API (`score` with a
deadline, then a continuation that finishes the fixtures off the request path, cached per
statement and bundle) is the first item on the handover list and is **not** in this release.

## 6. Trying it: shadow mode

1. Install, set up and compile as above; commit the bundle beside the workbook; rebuild when
   either changes (the scorer refuses a bundle built for another schema).
2. Score every statement the application sends, log `score`, `verdict`, the high findings,
   `signals.fixtures.verdict`, `timing.cut_by_deadline`, `timing.total_ms`, the bundle hash — and,
   beside it, whether the person accepted, edited or rejected the statement, and (if known)
   whether its number was later found wrong. That outcome is the label the trial is for.
3. Change nothing in the application's behaviour.

## 7. Choosing a threshold, after a few weeks

With the logged `(score, outcome)` pairs, for a candidate threshold `t` look at two numbers: of
the statements scored below `t`, how many were wrong (caught); of those at or above `t`, how many
were wrong (slipped through). In the benchmarks the scores have been well ordered (§9), but your
schema, workbook and users decide; read your own numbers first. Then look at `undefined_term`
notes (words to define), `fixtures: not_run` reasons (patterns with no reference; numbers no
term binds; unverified constructs), and the share of calls `cut_by_deadline`. A calibrator
fitted on your labels (`IsotonicCalibrator.fit`) turns the ordering into a rate for your data.

## 8. What is not guaranteed; known limitations; open defects

Not guaranteed: see [`docs/guarantees.md`](docs/guarantees.md). In short: a `likely_right`
verdict means the evidence found nothing wrong on small synthetic fixtures built from your
schema and rules; the number is not calibrated; a stage that did not run is listed, never counted
as passed.

Known limitations of this release:

- **L1 — static-only ceiling.** Without a fixture verdict a correct statement scores about 0.5–0.6
  (dev median 0.536 on gold, static); only a fixture `match` lifts it to ~0.8. If the fixture
  stage is often `not_run` (no reference for the pattern, unverified construct, deadline), a
  trial measures the static checks alone.
- **L2 — unverified constructs** (§2) leave BigQuery statements without a fixture verdict. The
  w5 references using `ARRAY_AGG … IGNORE NULLS`, `ARRAY_AGG … LIMIT` and `ISOWEEK` were
  rejected at compile for this reason.
- **L3 — paraphrase variants of templated references do not compile** (their `{slot}` tokens enter
  the question pattern as identifiers), so a bundle built from templated references carries no
  model-generated paraphrases (handover item H3).
- **L4 — BigQuery is stricter than DuckDB about types** (untyped NULL operands, string/number
  `CONCAT`, mixed set operators): a statement BigQuery rejects can still get a fixture verdict.
  The offline dry run (`sqlgami.contrib.bigquery`, sandbox only) is the check for that class.
- **L5 — Unicode casing** (`UPPER('ß')`) follows ICU in BigQuery and not in DuckDB.
- **L6 — TVF/UDF support** is tested on synthetic schemas and the fixture path end to end
  (TVF over TVF, view over TVF, wrong and swapped arguments) and measured on the held-out TVF
  set (§9: AUROC 0.868 with the bundle, 0.838 on TVF-calling candidates; the labels were made
  after the tag on datasets holding the routines).
- **L7 — calibration** does not ship; `calibrated` is false.
- **L8 — the catalogue decides.** A question whose numbers no term binds, or whose wording no
  reference pattern matches, gets no fixture verdict; the workbook is the host's, and its gaps
  show up as `not_run` reasons, not as guesses.

Open defects with IDs are in [`HANDOVER.md`](HANDOVER.md) §6 (H-numbers) and the red-team
inventory in §5; the `xfail(strict=True)` tests in `tests/` are the executable list.

## 9. Measured numbers (how, and what)

All on the HR snapshot bench (a periodic-snapshot schema: fact × pay periods × hierarchy; see
`HANDOVER.md` §3 for the layout), BigQuery dialect, labels from native BigQuery on the sandbox,
static mode (no live engine), scored **once** per held-out set at the stated commit and bundle.

- **Translation fidelity.** Native BigQuery vs the DuckDB translation on the same fixture
  rows: w4 77/77 golds, 44/44 scripts, 121/121 planted bugs; w4v 231/231; w4v2 231/231 agree
  (0 disagreements, 0 unstable) at `aa9b2ad`. The 145-case boundary audit: 77 agree, 20 not
  verified (by design), 4 known divergences (L4, L5), everything else closed.
- **Held-out w4v2 (21 variants, 168 plain + 56 style-rewritten candidates, 6 models)** at
  `8609d96` with the frozen w4 bundle: AUROC 0.97; correct ≤ 0.4: 0/88; wrong ≥ 0.8: 1/80;
  fixture verdicts agree with native labels 98 % (106/107 pooled).
- **Dev gold, static:** median 0.536 (max 0.588) — the static ceiling (L1). Planted-bug catch
  at ≤ 0.4: 8/15 static; the misses are value bugs that need fixtures. False alarms (correct
  candidate ≤ 0.4) on the 224 dev candidates: 1/63 with real gold before the batch-5/6 fixes,
  0/88 after.
- **Held-out w5v — the honest BigQuery figure of this release** (21 variants, 168 candidates:
  33 correct, 135 wrong; the 42 style-rewritten candidates were all wrong natively; bundle
  `601bedb8…`). **First contact at `aa9b2ad`** (the code before three defects this set revealed were fixed;
  reproduced exactly, deterministic, cached labels): AUROC 0.818 without the bundle (plain
  0.820), 0.800 with it unlimited, 0.814 at 50 ms; correct ≤ 0.4: 5/33; wrong ≤ 0.4: 66/135;
  wrong ≥ 0.8: 0/135 without the bundle, 4/135 with it. **After fixes found on this set, at
  `438710c`:** AUROC 0.863 / 0.842 / 0.859; correct ≤ 0.4: 2/33; the rest unchanged. The
  first-contact number is the one to cite as honest; the second is tuned on this set.
  Wrong ≥ 0.8 with the bundle: fixture "match" verdicts on wrong candidates for q031v1-a/b and
  q032v1-b (10 such matches in all, 4 above 0.8). The variant and its base reference ask the
  same output (identical column lists), so the cause is not the match key: either the synthetic
  fixtures lack the rows that would separate the candidate from the gold, or the variant gold
  differs natively from the base gold the reference was compiled from. Not established; the
  cached native rows can settle it (H13). This is the worst failure mode — false confidence —
  and it is open in this release.
  Fixture stage: 14 match, 4 mismatch, 22 "no reference for these values" (a bare "1" in the
  question no term binds), 73 "no reference matches this question", 43 "cannot run" (the q032
  variants are `IF`-control-flow scripts the live path does not run; 3 candidates do not parse).
  Of the 21 variant golds, 5 score ≤ 0.4: three are those control-flow scripts, two (q029v1-a/b)
  carry a `term_missing` HIGH where the `active` filter is applied in a subquery scope — a false
  alarm (H14). Median score of a correct candidate 0.57 (the static ceiling, L1). Latency with the
  bundle at 50 ms: p50 27 / p90 53 / p99 70 ms, 18/168 cut by the deadline; without it p50 21 /
  p99 64 ms. This is lower than w4v2 (0.97): w5v is a harder set (scripts, calendar-month
  bucketing, ARRAY_AGG projections, pivots) and six of its eleven base references could not
  compile (L2), so most candidates had no fixture to be judged on.
- **Held-out w6v (TVF/view set, 19 variants after the author excluded q040v1-a/c, 152
  candidates) at `438710c` + bundle `dfb5c050…`, scored once, labelled afterwards** (post-tag
  doc update: the scores are the one-shot run's; the bench author relabelled them natively on
  datasets that hold the TVFs, views and UDF — 11 seeds, multiset compare against the variant
  gold, 0 gold errors; `W6V_RESULT.md` in the bench archive): 95 correct / 57 wrong. Without
  the bundle: AUROC **0.708**; correct ≤ 0.4: 3/95; wrong ≤ 0.4: 25/57; wrong ≥ 0.8: 0/57. With
  the bundle: AUROC **0.868**; correct ≤ 0.4: 3/95; wrong ≤ 0.4: 31/57; wrong ≥ 0.8: 1/57.
  TVF-calling candidates (88): 0.604 without the bundle → 0.838 with it. Fixture stage: 55
  match, 7 mismatch, 70 no reference, 12 fixed values, 3 runaway; latency with the bundle at
  50 ms p50 21 / p90 33 / p99 40 ms, 64/152 cut by the deadline (TVF expansion makes the
  statements heavier). The tag's text said "NOT MEASURED" because the maintainers' seed
  datasets held no routines (H16); the labels above come from the bench author's datasets.
- **Latency** on 60–80-line BigQuery scripts with the bundle: p50 29–36 ms, p99 53–88 ms;
  parse alone 17–26 ms.

## 10. Reporting issues

Open an issue in this (private) repository with: the SQL, the question, the schema (or the
table names involved), the bundle hash from `signals.fixtures.bundle`, and `report.to_json()`.
Say what you expected. A false alarm is as important as a miss: the bench's false-alarm set
(`tests/test_adversarial_checks.py`, `tests/test_redteam_*`) is where it will land. Translation
disagreements need the native rows (the sandbox project only; never a production project).

No employer, customer or product names belong in issues, tests or fixtures.

Author: Abhinav Anand — https://abhinav-anand.com/ · blog https://www.cyberaka.com/ ·
cyberaka@cyberaka.com
