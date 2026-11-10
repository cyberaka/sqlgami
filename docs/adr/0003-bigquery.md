# ADR 0003: BigQuery — live scoring stays deterministic; the real service is offline only

Status: proposed, 2026-11-10 (the owner's direction via watson: "that's what will be used to
judge sqlgami"; Postgres second). Builds on ADR 0002; nothing here loosens it.

## Context

The owner's production SQL is BigQuery: `DECLARE` scripts, `{template}` placeholders,
backticked `` `dashed-project.dataset.table` `` names, `QUALIFY`, `SAFE_DIVIDE`, the
`DATE_SUB/DATE_ADD/DATE_TRUNC/DATE_DIFF` argument orders, `EXTRACT`, `FORMAT_DATE`,
`UNNEST`/`ARRAY`/`STRUCT`, `COUNTIF`, `IFNULL`, scalar-subquery date bounds, two-level
aggregation. sqlgami already parses the dialect (sqlglot) and resolves
`project.dataset.table` names in the catalogue; the fixture stage, the term checks and the
bench have only been exercised on DuckDB/SQLite/Postgres.

A BigQuery call is never deterministic or ≤ 50 ms, so it has no place in the live path.
Fixtures are the live evidence; BigQuery is where their ground truth is established.

## Decision

**B1 — live path (deterministic, ≤ 50 ms, no network).**
1. The catalogue carries a host-declared map from BigQuery names to bundle tables:
   `tables: {"`proj-x.hr.emp_period_snapshot`": hr.emp_period_snapshot, ...}` in the compile
   config and the manifest; an unmapped table is `not_run` ("no fixture table for …"), never
   guessed from a name's tail.
2. The fixture stage runs BigQuery SQL on DuckDB only through a **verified translation**:
   sqlglot's `bigquery → duckdb` transpilation plus sqlgami rewrites, gated by an
   **allow-list of constructs** (functions, argument orders, clauses). A construct is on the
   list only when a test proves equal results between native BigQuery (B2) and the
   translation on the same fixture data. Anything outside the list makes the fixture stage
   `not_run` with `untranslatable: <construct>` — a lost check, never a verdict.
3. Static checks and the term catalogue run on the bigquery dialect as they do on the others
   (dialect-specific `sql_bigquery` column where a term's SQL differs).
4. Scripts: `DECLARE`/`SET` are read as parameters; the scored statement is the final
   `SELECT`; `{template}` placeholders bind as today.

**B2 — the real service, offline only.** An optional adapter (`sqlgami[bigquery]`,
`google-cloud-bigquery`, ADC, no key files) for dry runs (validity, bytes billed) and for
running references natively. Hard guard in code, not configuration: the project is read from
`SQLGAMI_BQ_PROJECT`, must start with `sqlgami-`, and the only accepted value is
`sqlgami-dev-1109` (sandbox, billing disabled); any other value raises before a client is
built. Datasets are created with a `bench_` prefix and recorded in a manifest for cleanup;
tables expire in 60 days. Uses: load fixtures and seed tables, run the golds natively, and
prove the DuckDB translation equal — the ground truth for B1's allow-list. Never called by
`score()`.

**B3 — bench.** `benchkit --engine bigquery`: golds in the BigQuery dialect, labels from
native BigQuery results on the sandbox. Every score is reported twice — labels from native
BigQuery, fixture verdicts through the DuckDB translation — with their agreement rate.

## Consequences

- No new live dependency; a host without the extra gets exactly today's behaviour plus the
  translation, which needs nothing but sqlglot.
- The allow-list starts empty and grows only by proof; coverage, not safety, is what the
  tests buy. Reports say plainly which construct kept a fixture from running.
- Cost: sandbox only, load jobs free, queries within the free quota; nothing can be billed.
- Order of work: B1 fidelity tests (test-only) → B2 sandbox load → B1 → B3.

## Addendum (2026-11-10): tables, views and table-valued functions; native fixtures; fuzzing

The owner's production SQL reads tables, views and `CREATE TABLE FUNCTION` TVFs, called as
`FROM `proj.ds.f`(12003, DATE '…')`. BigQuery is the judge; DuckDB is a validation aid.

**B4 — TVFs and views, end to end.**
- The catalogue reads `CREATE TABLE FUNCTION ds.f(p INT64, …) AS SELECT …` from DDL as it
  reads views: a table whose definition is the body and whose parameters are declared.
- A call `FROM f(args)` is expanded like a view, with the arguments bound to the parameters
  the way script variables are (literals inline, scalar subqueries as subqueries) — so grain,
  terms, policies and every static check apply inside the body with the caller's values.
  Wrong arity or an unknown function is an ERROR finding (`unknown_table`), never a pass.
- The safety gate treats a TVF body as a view body: the tables it reads go through the same
  table gate; a function the catalogue does not declare is not sent anywhere.
- Fixtures: a TVF becomes a DuckDB table macro (`CREATE MACRO ds.f(p, …) AS TABLE <body>`)
  through the verified translation, so the call runs on the fixtures as written; the body's
  constructs are checked against the allow-list like a statement's, and a TVF whose body is
  untranslatable makes every statement reading it `not_run: untranslatable`.
- Views over views keep their dependency order; a view body with UNNEST/ARRAY is translated
  under the same allow-list; a cross-dataset reference (`other-proj.ds.v` over `proj.hr.t`)
  resolves through the logical name and the table map.
- Native proof: the HR TVFs defined in the sandbox and run natively against the translation.

**B5 — native fixture mode.** The same fixtures (byte-identical CSVs, views and TVFs) can be
loaded into sandbox datasets once per bundle (`bench_fx_<bundlehash>_<fixture>_<schema>`,
recorded in the ledger). A batch scorer runs candidate and reference natively on those
datasets with the identical verdict logic (fingerprints, no_evidence, mismatch rows). The
live 50 ms path stays on DuckDB; the bench reports, for every item, the DuckDB fixture
verdict beside the native fixture verdict and their agreement; any disagreement fails the
run (CI exit 1). Guard unchanged: `sqlgami-dev-1109` only.

**B6 — differential fuzzing.** A seeded generator builds BigQuery expressions and small
queries from the allow-listed constructs over fixture tables with random NULLs, ties and
dates; each is run translated on DuckDB and, in the on-demand sandbox job, natively; results
must be equal (a disagreement names the construct and the seed). The CI job without a
sandbox checks that every generated statement translates and runs on DuckDB deterministically;
the native job is the proof. Findings from sqlgami-checker's red-teaming of translation edges
are added as fixed cases.

**B7 — guarantees.** `docs/guarantees.md`: one page, plain language, what a report does and
does not promise, and what each verdict rests on.

Order of work: B4 → B5 → B6 → B7. Nothing here changes the allow-list before w5 is scored.
