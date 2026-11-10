# ADR 0002: deterministic live scoring in 50 ms; LLMs offline only

Status: proposed (awaiting the owner's OK). No code until then.

## Context

sqlgami will run inside coding agents and application flows. The owner's requirement: a live
`score()` call is **deterministic**, makes **no LLM call**, and returns in **≤ 50 ms**. LLMs may
be used **offline** only — to write queries, make mock data, decide correct answers, build
evals. The consistency stage with a generator stays an optional extra, off by default, never
the primary signal; the strong-verifier line of work is dropped.

Measured at a11f22d on the 21 HR golds (median 19 lines; the owner's real SQL is 40–80):

| path | p50 | p90 | max |
|---|---|---|---|
| static `explain()` (catalogue + terms + rules, no database) | 11.8 ms | 20.8 ms | 36.9 ms |
| `sample()` against a 40k-row embedded DuckDB fixture (nasty-47) | 27.9 ms | 47.8 ms | 97.6 ms |

Where the static time goes (cProfile over 63 calls; shares of the static path):

| share | what | note |
|---|---|---|
| 42 % | `analysis._Resolver.run` — sqlglot `qualify` 14 %, `annotate_types` 9 %, building the sqlglot `MappingSchema` 8 %, type-name parsing (`types.sqlglot_type_name`) 8 % | the schema mapping and the type names are **the same for every call** on one Scorer: cacheable |
| 13 % | sqlglot `parse` (tokenizer 10 %) | 40 tokenizer calls per `explain()`: the statement, then type strings and snippet round-trips; one parse should do |
| 9 % | `safety.review` | the text net re-tokenises the statement |
| 26 % | static checks (`walk_own` 13 %, `predicates.own_nodes` 10 %) | repeated subtree walks per check; a per-SelectModel node index removes most |
| 9 % | question stage | fine |
| ~2 % | `.sql()` generation (2,238 calls per 63) | snippets and text keys generated eagerly; make them lazy |

So about a third of the static path is per-Scorer constant work redone per call, and another
quarter is repeated tree walking. 40–80-line statements scale roughly linearly in tokens and
nodes, which puts today's p90 at or over 50 ms for the owner's SQL.

## Decision

### 1. The latency budget is a feature

`Options.deadline_ms` (default 50). `score()` runs stages in a fixed order and checks the
clock between them; a stage that would start past the deadline is **reported as `not_run`
("deadline")** and never counted as a pass. The order is the value order:

1. parse, statement gate, catalogue resolution, static checks, terms (ADR 0001) — the live
   core, always run; the budget is sized so that this fits for 80-line SQL;
2. fixture evaluation (section 3c) — the strongest deterministic signal, runs while time
   remains, fixture by fixture;
3. `EXPLAIN` / sample on the host's own database — only when the host set `mode` to ask for it
   and time remains (an external database is not under our control);
4. consistency / reconciliation with a generator or prober — **never in the live profile**;
   on by explicit opt-in only (`Options.consistency = "off" | "if_time" | "always"`, default
   `off`). When on, it may make network calls and is outside the deadline by definition.

`report.timing` lists each stage's milliseconds and the deadline, so a host can see what was
skipped and why. `Options.budget_s` (the existing total wall-clock bound) stays as the hard
ceiling for the opt-in stages.

### 2. Make the static path fast, then guard it

Order of work, each measured before and after on the same set (the 21 HR golds, the billing
golds, and the production shapes in `tests/test_production_shape.py`, padded to 40–80 lines):

1. **Per-Scorer compiled catalogue**: the sqlglot `MappingSchema`, parsed column types, the
   compiled term catalogue (ADR 0001) and the required-predicate rules are built once in
   `Scorer.__init__` (or on first use) and reused; invalidated when the catalogue changes.
2. **One parse per statement**: the tree from `analyse()` is handed to the safety review and
   the adapters instead of re-parsing; the text net works on the token list already produced.
3. **Node index per SelectModel**: columns, aggregates, windows, literals and subqueries
   collected once per scope; checks read the index instead of walking.
4. **Lazy snippets**: `Finding.snippet` and text keys are generated when first read.
5. **Reuse the resolver's `qualify`/`annotate_types` output** across the static and question
   stages (already one tree; make sure nothing re-qualifies).

Target: static p90 ≤ 25 ms on 40–80-line SQL, leaving ≥ 25 ms for fixtures.

**Benchmark and guard**: `python -m benchkit latency <suite> [--lines 40-80]` prints p50/p90/
p99 per mode over the suite's golds and the production shapes, stretched to the requested
line range by the suite's own CTE/predicate padding; it writes `bench/.runs/latency.json`.
A test in CI runs the same set on the shop fixture and fails when static p90 exceeds the budget
(with a tolerance for CI machines, pinned in one constant).

### 3. The eval framework: references, fixtures, live comparison

The deterministic replacement for LLM alternatives is **reference answers on small fixtures**.

**3a. References.** A host registers verified reference queries or expected answers:

```python
Scorer(references=[
  {"key": "headcount_by_lvl3", "terms": ["headcount", "my_organisation"],   # matched by term set
   "sql": "...", "params": {"manager_id": 12003}},
  {"key": "q017", "question_id": "q017", "expected": {"fixture-s47": [[91]]}},  # by explicit key
])
```

A reference is matched to a live question **deterministically only**: by explicit key or
template id given by the host, or by the exact set of catalogue terms the question matched
(ADR 0001). No fuzzy matching: when nothing matches, there is no reference and the report
says so. Two references matching one question is a configuration error reported once.

**3b. Fixtures.** `sqlgami.fixtures` generates small adversarial datasets from the host's
schema plus declared rules, in the spirit of our nasty seeds: keys and foreign keys from the
catalogue; declared NULL rates, duplicate and restated rows, ties, empty groups, edge dates,
hierarchies (`features.yaml` from the fixture study is the first spec). Sherlock's study on
the HR suite: a fixture of **92 in-scope employees × 27 periods (~7k rows)** keeps 36 of 41
planted bugs diverging, the same as the full 40k rows; below ~60 employees bugs drop out; a
**library of 6 fixtures** (profiles s1, s42, s44, s45, s47, s48, each ≤ 100 employees) covers
all 41; the 28 held-out golds run on one fixture in DuckDB in-memory in ~5 ms average, worst
< 20 ms. Fixtures are built **offline**, stored with the bundle, and loaded into an in-memory
DuckDB at Scorer start (or lazily on first use), so the live path pays only the query.

Parameters: a live candidate comes with any `{manager_id}`. Fixtures are built **around the
parameter values the host declares** (`fixtures: {params: {manager_id: [12003, 52002]}}`); a
live value outside that set is **remapped deterministically** to the fixture-native value
with the same declared role (documented in the bundle: `param_map`), and the report says the
comparison was made under the mapped value. A parameter with no declared role is not
remapped; fixture evaluation is then `not_run` ("parameter not covered by fixtures").

**3c. Live comparison.** For a question with a reference, the candidate runs on each fixture
in turn (DuckDB in-memory, one connection per fixture, warm) while the deadline allows, and
its result **fingerprint** (sorted canonical rows, the same comparison rules as the
consistency stage: column binding by name or one permutation, numeric tolerance, order only
when the question asks for it) is compared with the **precomputed** reference fingerprint for
that fixture. Outcomes:

- match on every fixture run → strong support (a bonus of the size two strong independent
  sources earn today; the report lists the fixtures);
- mismatch on any fixture → `fixture_mismatch` HIGH, `likely_wrong`, with the differing rows
  (up to a bound) as evidence;
- a candidate that errors on a fixture → `execution_error` as today;
- out of time → the fixtures not reached are listed as `not_run`.

Fingerprints of references are **always precomputed offline** and stored; nothing about the
reference runs live. Candidate execution is the only live work, bounded by fixture size (the
bundle records each fixture's measured gold latency; live picks fixtures that fit the time
left, largest coverage first).

What fits in 50 ms (measured): static core 12–21 ms today (target ≤ 25 ms after section 2),
one 7k-row fixture query 5–20 ms; so 1–3 fixtures live on typical SQL, 6 on simple ones. What
must be precomputed: fixture data, reference fingerprints, term compilation, the parameter
map, and the bundle's integrity hash.

### 4. Offline train/compile, deterministic live

The owner's model: **training data → offline compile → deterministic live**.

Input (the host's training data): the term/KPI catalogue (ADR 0001), reference queries and/or
data points (expected answers on known data), the schema, fixture rules and parameter roles.

`sqlgami compile <config> -o bundle/` (also `sqlgami train`, same command) runs offline and
may use an LLM and the fixture generator to:

1. expand the catalogue into **variant questions and queries** per term/KPI;
2. build the **adversarial fixture datasets** from the schema and rules;
3. compute the **expected answers** of every reference on every fixture, and **verify** them
   against the host's own reference queries and data points — anything the LLM produced
   (variant queries, proposed answers) enters the bundle only when it agrees with the host's
   references on every fixture; disagreements are written to `bundle/rejected.jsonl` for the
   host to read;
4. write a **compiled, versioned bundle**: `manifest.json` (sqlgami version, schema hash,
   catalogue hash, fixture list with sizes and measured latencies, parameter map), terms as
   serialised ASTs, fixtures as Parquet/CSV plus DDL, reference fingerprints per fixture,
   the variant index. The host can review every file; the bundle is plain data.

Live `Scorer(bundle="bundle/")` loads the bundle and nothing else: no LLM, no network,
deterministic (same bundle + same SQL → same report; the report carries the bundle hash).
The schema hash is checked against the live catalogue; a mismatch is reported and fixture
evaluation is `not_run` until the bundle is recompiled.

LLM use in `compile` is through the same optional contrib interface as today (host-supplied
callable, allow-listed models, token never logged); `compile` works without any LLM — then
there are no generated variants, only the host's references.

**Expansion (implemented, `sqlgami.expand`).** With `expansion: {model: ...}` the compile
step asks the model for (a) further wordings of every term and KPI and (b) paraphrases of
every reference question, some at other slot values. Acceptance is deterministic and the
model's text is never trusted on its own: a wording enters only if, added to the catalogue,
no registered question changes its pattern or slots, it does not already occur in a
registered question outside the term's own span, and a question that uses the term
re-matches to the same terms and slots with the new wording substituted; a paraphrase
enters only if it re-matches the reference's pattern, binds the reference's slots with the
values the model claimed, states the same free numbers, and — when its values differ — the
reference is written over those slots and runs at them on every fixture. Accepted wordings
are written to `review/Terms.additions.csv` for the host to accept into the workbook (the
live catalogue is the host's); accepted paraphrases are stored as variant references
(`variant_of`), found by exact text and serving their slot values from stored results, so a
question at a compiled value needs no live re-run. Everything else is in `rejected.jsonl`
with its reason. Bench measurements made after accepting the review are reported as tuned
on the questions the model saw.

### 5. The term catalogue in the same cache

ADR 0001's compiled terms live in the per-Scorer compiled state (section 2.1) and, when a
bundle is used, are loaded from it pre-parsed. Term checks are part of the live core.

## Consequences

- Live scoring becomes a function of (SQL, question, catalogue, bundle) with a hard deadline;
  hosts can run it per keystroke in an agent loop.
- The library's quality on "runs-but-wrong" moves from LLM agreement to reference fixtures:
  deterministic, explainable (the differing rows), and dependent on the host declaring
  references — the bench measures both with and without a bundle (ADR 0001's rule 1).
- Work, in order: (i) latency benchmark + profile baseline (done above) and the per-Scorer
  compiled state; (ii) one-parse and node index; (iii) deadline option and `not_run`
  reporting; (iv) fixtures module and reference fingerprint comparison with hand-built
  fixtures from the bench seeds; (v) `compile` command (after the owner's OK on this ADR);
  (vi) CI guard.
- Dropped: strong-verifier measurement, further source-weight tuning (what is committed
  stays, off by default).

## Open questions for the owner

1. Default `deadline_ms`: 50 fixed, or 50 with `None` meaning "no deadline" for batch use?
2. May the bundle include fixtures derived from the host's real data (subsampled, as in the
   study), or synthetic only? Synthetic only is safer to ship; subsampled real data is more
   faithful.
3. Where do bundles live — next to the application (checked in), or fetched from a registry
   the host runs? v1 proposal: a directory the host points at, nothing else.
