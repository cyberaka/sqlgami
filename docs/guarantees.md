# What a sqlgami report promises, and what it does not

sqlgami's report is advisory. It is not a guarantee of correctness and comes with no warranty;
every report says so itself. This page says, in plain words, what each part of a report rests
on, so that a number is read for what it is.

## The confidence value

The value in `confidence.score` orders statements from more to less trustworthy. Out of the box
it is a heuristic (`"calibrated": false`); with a calibrator fitted on your own labelled
statements it is an estimate of how often comparable statements were right in that data — and
only in data like it. Nothing turns it into a probability that *this* statement is right.

A few caps always hold: a statement that does not parse scores 0; one that cannot be run — an
unknown table or column, not a read-only query, rejected by the engine — never scores above
0.1; one with a high-severity finding never above 0.4; a question that uses a business term
the workbook does not define never above 0.75 unless the fixtures agreed. These caps are the
parts of the number that are rules rather than evidence.

## Each verdict, and what it rests on

| the report says | what was actually established |
|---|---|
| `statement: passed` | the text parses as one read-only query in the dialect; nothing about its meaning |
| `schema: passed` | every table and column it names exists in the catalogue you gave; views and table functions were expanded and read through |
| `static` findings | properties of the SQL text alone — joins that multiply rows, filters a policy requires, a window that reads the wrong grain — found by rules, each with a test; a rule that does not fire proves nothing |
| term findings | the words of the question matched rows of *your* workbook; what the workbook does not define is reported as undefined, never interpreted |
| `fixtures: match` | the statement and the reference the workbook names for this question gave the same rows on every fixture dataset of the bundle; the fixtures are small, synthetic unless you opted into real rows, and shaped by the rules you declared — agreement on them is evidence, not proof |
| `fixtures: mismatch` | they differed on at least one fixture; the rows that differ are in the report |
| `fixtures: not_run` | nothing was established, and the reason is given: no reference matches the question, the question's values have no stored reference, or the statement uses a construct the translation has not verified |
| `dry_run`, `sample`, `consistency` (live engines only) | the engine accepted the statement, returned rows, and returned the same rows twice |

A stage that did not run is listed under `checks_not_run`. A lost check lowers what the number
can say; it is never counted as a pass.

## BigQuery statements on DuckDB fixtures

A BigQuery statement never reaches BigQuery at scoring time. The fixture stage runs it on
DuckDB through a translation that is allowed only for constructs — functions, clauses, types,
date parts, format specifiers, operators — each proven, by a test run against the real service
on the sandbox, to give the same rows. The proof set is `tests/test_bigquery_translation.py`
and the 145-statement boundary audit in `tests/test_bigquery_boundary.py` (week starts for
every weekday, month ends, leap days, NULLs, empty arrays, ties, offsets, floating-point and
NUMERIC arithmetic, casts, windows, set operations). A construct outside the set makes the
stage `not_run: untranslatable: <construct>`.

What that proof does **not** cover, and what you should know:

- **BigQuery is stricter than DuckDB about types.** `CONCAT(name, id)` with an integer id,
  `'a' || NULL` with an untyped NULL, `'1' = 1`, a `UNION ALL` mixed with `UNION DISTINCT`
  without parentheses: BigQuery rejects these, DuckDB runs them. A candidate that would fail
  natively can therefore get a fixture verdict. The check for this class is a native dry run
  (`sqlgami.contrib.bigquery`, offline), not the fixtures.
- **Unicode casing** follows ICU in BigQuery (`UPPER('straße')` is `STRASSE`) and DuckDB's own
  tables (`STRAẞE`). Comparisons of accented or special-cased text may differ.
- **Floating point** is rendered as BigQuery renders it when cast to STRING, and `ROUND(x, n)`
  rounds the exact binary value as BigQuery does; `NUMERIC` keeps scale 9 where the operand's
  type is known from the schema or a cast. Where a column's type is unknown to the bundle
  (a view's column, an expression), the common case is assumed: `/` over FLOAT64, `DATE_ADD`
  over a DATE.
- **Nondeterministic statements** (`RAND()`, `GENERATE_UUID()`, `TABLESAMPLE`,
  `APPROX_COUNT_DISTINCT`) are `not_run`: two engines do not agree on them, and neither does
  one engine with itself.
- **The fixtures are not your data.** They are built from your schema and the rules you
  declared. Two statements that agree on them may disagree on a case the fixtures do not
  contain; the `rules:` and `rows:` of the compile config decide what they contain.

## The safety gate

A statement is sent to a database — or run on the fixtures — only if it is a single read-only
query, every function it calls is on the engine's list of built-ins that only compute (or one
you vouched for), every table it reads is in your catalogue, and the same holds for the body of
every view and table function it reads through. On BigQuery, federation (`EXTERNAL_QUERY`),
models (`ML.`, `AI.`), object tables (`OBJ.`), and `INFORMATION_SCHEMA` in any spelling are
refused; wildcards, time travel and `SESSION_USER` need an explicit opt-in. This is a strong
filter that rests on parsing the statement; it is not a sandbox. Give sqlgami a database role
that can only read what it should.

## What is not promised

- That a `likely_right` statement is right. The verdict says the evidence found nothing wrong.
- That a `likely_wrong` statement is wrong in the way the finding says; it says what was found.
- That the number is comparable across schemas, workbooks or question styles. It is comparable
  within one bundle, on questions like the ones its references cover.
- That any stage ran: read `checks_not_run`.
