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
