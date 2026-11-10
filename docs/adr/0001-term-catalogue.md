# ADR 0001: the term catalogue — business terms as host metadata

Status: proposed (awaiting the owner's OK). No code yet.

## Context

A question says "people not on leave", "the last 26 pay periods", "turnover in basis
points", "my organisation". What those mean in the data — which column, which literal,
whether NULL counts, how a window is counted, where a date comes from — is decided by the
data engineers, not by sqlgami and not by the person asking. Today sqlgami takes a free-text
`glossary` and reads it only as words to match against the question; the enforceable rules
live elsewhere (`required_predicates`, `snapshot_columns`, `soft_delete_column`,
`tenant_column`), each with its own shape. A query that contradicts a declared meaning
(`status_cd != 'L'` where the term says NULL status counts as not on leave; `BETWEEN -26 AND
0` for "the last 26 periods"; `vol_n` summing the involuntary flag) gets through.

The owner's direction: a host declares how each business term is represented in the data;
sqlgami judges candidates against those declarations and never guesses a meaning it was not
given.

## Decision

### 1. One concept: the term catalogue

A catalogue is a list of **terms**. It is given as YAML (a file or text) or as Python objects,
and `Scorer(terms=...)` accepts either. One term:

```yaml
terms:
  - name: not_on_leave
    phrases: ["not on leave", "currently not on leave", "excluding leave"]
    applies_to: [hr.emp_period_snapshot]            # tables (optional; else inferred from columns)
    predicate: "status_cd IS DISTINCT FROM 'L'"     # the data-level meaning
    nulls: include                                  # NULL status is not on leave
    dialects:                                       # optional per-dialect override of any field
      sqlite: { predicate: "status_cd IS NOT 'L'" }

  - name: basis_points
    phrases: ["basis points", "bp", "bps"]
    expression: "CAST(FLOOR({rate} * 10000) AS BIGINT)"   # {rate} is the term's one input
    unit: integer                                   # the output type the question expects

  - name: last_n_pay_periods
    phrases: ["last {n} pay periods", "past {n} pay periods", "trailing {n} periods"]
    window: { column: hr.pay_period_dim.period_seq, kind: inclusive_ending_now, size: "{n}" }
    # i.e. period_seq BETWEEN -({n}-1) AND 0

  - name: n_pay_periods_ago
    phrases: ["{n} pay periods ago", "{n} periods ago"]
    window: { column: hr.pay_period_dim.period_seq, kind: point, offset: "-{n}" }

  - name: hire_date
    phrases: ["hire date", "hired", "date of hire"]
    system_of_record: hr.employee_dim.hire_dt       # the column that holds the truth
    # a query that reads hire_dt from another table gets a note

  - name: my_organisation
    phrases: ["my organisation", "my org", "under me", "my team"]
    access_chain:                                   # the existing or_chain rule, now a term
      tables: [hr.emp_period_snapshot]
      columns: [lvl_2_id, …, lvl_7_id, lvl_2_id_fn, …, lvl_7_id_fn]
      value: "{manager_id}"
```

Field kinds, each optional, each parsed with sqlglot into an engine-neutral AST:

| field | meaning | checked how |
|---|---|---|
| `predicate` | a boolean condition the query must hold when the term is asked | equivalent predicate present (see 2) |
| `expression` | how a measure is computed from its input(s) | the projection the question names for the term is that expression over something, or a stated equivalent |
| `window` | how a time window is counted on a sequence or date column | the range the query puts on that column has the declared size/offset |
| `access_chain` | the OR-chain scope rule (today `required_predicates` with `match: or_chain`) | as today |
| `system_of_record` | which table.column holds a value | the query reads it from there (note otherwise) |
| `unit` / `format` | integer, percent, bp, date-only, text pattern | the output column's type and shape |
| `nulls` | `include` / `exclude` / `separate` | the predicate's NULL behaviour matches |
| `snapshot_column` | the per-row effective date of a periodic snapshot (today `snapshot_columns`) | as today (snapshot_grain) |
| `soft_delete` / `tenant` | today's `soft_delete_column` / `tenant_column` | as today |
| `dialects` | per-dialect overrides of any of the above | the override is used for that engine |

Phrases may contain `{n}` (a number) and `{param}` placeholders that bind to the question's
numbers and to template parameters. A term with no checkable field (only a `meaning:` prose
line) is **documentation**: it is matched in the question so that it is not reported as
undefined, but nothing is enforced.

### 2. Matching and checking

**Question → terms.** Exact phrase match first, on a normalised question (lower case, hyphens
and underscores as spaces, singular/plural folded, numbers bound to `{n}`). Then declared
synonyms. No embeddings and no guessing beyond that; the only fuzziness allowed is the
normalisation above, and every match is recorded in `report.signals["terms"]` as
`{term, phrase, span}` so a reader can see why it fired. A question phrase that looks like a
business term (a noun phrase that matches a glossary *heading* but no phrase, or is in a host
supplied `watch` list) and matches nothing is an **undefined term**.

**SQL → representation.** Never text matching. The term's AST is compared with the candidate's
through the existing machinery:

- `predicate`: the SelectModel already resolves conjuncts, bound columns, OR chains and
  CTE/view scoping. A term's predicate is satisfied when a conjunct over the same columns is
  *equivalent* under the normalisations we already use (`!=`/`<>`, `IS DISTINCT FROM`,
  `COALESCE(x, '') <> 'L'`, `NOT IN (...)`, value-IN-columns, lookup subqueries) and when its
  NULL behaviour agrees with `nulls`. `status_cd != 'L'` against `nulls: include` is a
  contradiction, not a near miss.
- `expression`: the projection matched to the term (by alias name, question column list, or
  the question's "return X" clause via the shape checks) is reduced with sqlglot's
  simplifier and compared structurally, allowing the obvious commutations; a missing
  `FLOOR`/`CAST` when `unit: integer` is a contradiction.
- `window`: the range on the declared column (BETWEEN, two inequalities, a frame clause, or
  date arithmetic) is read into (lower, upper, inclusive flags); its size or offset is compared
  with the term's. Off by one is the common contradiction.
- `access_chain`, `snapshot_column`, `soft_delete`, `tenant`: today's checks, unchanged, fed
  from the catalogue.
- `system_of_record`: the column resolution already tells which table a column came from.

A term is checked only when the question matched one of its phrases, or when the host marks
it `always: true` (a policy, like the access chain today).

### 3. Severity

- **Contradiction** of a declared term (the query does the opposite of the declaration): HIGH
  when the term is a predicate, access chain, window or NULL rule — these change which rows
  are counted; MEDIUM for `expression`, `unit`/`format` and `system_of_record` — the right
  rows, the wrong rendering or provenance. HIGH caps the score at 0.4 as today.
- **Absence** (the term is asked, nothing in the query addresses it): HIGH for predicates and
  access chains, MEDIUM otherwise.
- **Undefined term** (the question uses a phrase the catalogue lists under `watch`, or a
  glossary heading with no checkable representation): LOW `undefined_term` with the phrase
  named, and no positive bonus from the question stage for that term. sqlgami never invents a
  meaning; the note says what to declare.
- **Satisfied** terms are listed in `signals.terms` and count towards the question-agreement
  bonus; a question whose every matched term is satisfied earns more than one with none
  declared.

### 4. Folding in today's configuration

`required_predicates`, `snapshot_columns`, `soft_delete_column`, `soft_delete_policy` and
`tenant_column` become sugar: the Scorer builds terms from them (`access_chain`,
`snapshot_column`, `soft_delete`, `tenant`) so existing hosts and suites keep working, and
`terms` is the one place new rules go. The `sqlgami:` block in suite.yaml gains a `terms:`
list (or `terms_file:`) and the old keys stay valid. Internally `RequiredPredicate` becomes
one term kind rather than a separate class hierarchy; `Catalog.required_predicates` is
derived from the catalogue.

### 5. Bench glossaries become catalogues

Each bench glossary (`glossary.yaml`) grows a `terms:` section written by the suite's authors
in the format above; the `definitions:` prose stays as it is (frozen semantics; "not on
leave" stays as written). benchkit passes the glossary file as `Scorer(terms=...)` exactly as
a host would, so the feature is measured in host conditions. Suite authors may only declare
what the prose already says; the auditor checks the two agree. Terms with no representation
(deliberately, to test the undefined-term path) are allowed.

## Consequences

- One input for business meaning; the E checks (window_size, change_direction,
  label_definition_mismatch, glossary predicates, value_format, inequality_drops_nulls) are
  term kinds, not separate heuristics, and fire only on declared terms.
- Hosts without a catalogue lose nothing: today's behaviour is the empty catalogue plus the
  sugar above.
- New dependency on question–phrase matching being right; it is kept exact and explainable,
  and every match is in the report.
- Work: a `terms` module (parse, normalise, match), term kinds mapped onto existing checks,
  `undefined_term`, Scorer/suite plumbing, migration of the three bench glossaries, tests on
  dev only; wave 4 measures it.

## Open questions for the owner

1. Should an **undefined term** cap confidence (say at 0.7) or only add the LOW note?
2. When a term's phrase appears in the question but the catalogue scopes it to tables the
   query does not read, is that a contradiction (HIGH), absence (MEDIUM) or nothing?
3. Is per-dialect override enough, or do some hosts need per-schema (multi-tenant) catalogues?
