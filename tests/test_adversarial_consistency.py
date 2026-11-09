"""Adversarial cases for the consistency stage, written by the independent checker.

The stage runs other queries written for the same question and counts how many return
what the candidate returns. These cases ask five things of it:

1. An alternative that is not a plain read of the schema is never sent to the engine.
2. A result that is not deterministic is not counted as agreement or as disagreement.
3. Numbers that differ only by floating-point noise agree; the tolerance is stated here.
4. Columns may be reordered, but values may not move between columns.
5. Two results that are both empty (or both a lone NULL) are not evidence of anything.

Known defects are ``xfail(strict=True)``: the suite stays green, and a case turns red when
it is fixed, at which point its mark should be removed. ``pytest --runxfail`` shows them as
plain failures. Everything else here passes today and should keep passing.

No unsafe statement is really executed by these tests: the adapter is wrapped so that the
statement under test is recorded and refused instead of being run.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sqlgami import Options, Scorer, UnsafeQueryError
from sqlgami import consistency as C
from sqlgami.consistency import fingerprint
from sqlgami.engines.base import ExecutionResult

QUESTION = "Total order value per customer"
RIGHT = "SELECT c.name, SUM(o.total) AS spend FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name"


def _signal(report) -> dict:
    return report.signals.get("consistency") or {}


def _statuses(report) -> list[str]:
    return [a["status"] for a in _signal(report).get("alternatives", [])]


def _counts(db) -> tuple:
    row = db.execute(
        "SELECT (SELECT COUNT(*) FROM customers), (SELECT COUNT(*) FROM orders), (SELECT COUNT(*) FROM payments)"
    ).fetchone()
    return tuple(row)


def _sent_to_engine(engine: str, db, alternative: str) -> bool:
    """Score RIGHT with one alternative and say whether that alternative reached the engine.

    The alternative itself is refused by the wrapper, so nothing unsafe actually runs.
    """
    sent: list[str] = []
    with Scorer(engine, db=db, options=Options(timeout_s=5.0)) as scorer:
        real = scorer.adapter.execute

        def execute(sql: str, **kwargs):
            sent.append(sql)
            if sql == alternative:
                return ExecutionResult(ok=False, error="refused by the test")
            return real(sql, **kwargs)

        scorer.adapter.execute = execute
        scorer.sample(RIGHT, QUESTION, alternatives=[alternative])
    return alternative in sent


# ------------------------------------------------------------------ 1. unsafe alternatives

# (engines, sql): statements that must never be sent. All of these are refused today.
NEVER_SENT = [
    (None, "DELETE FROM customers"),
    (None, "UPDATE customers SET name = 'x'"),
    (None, "DROP TABLE payments"),
    (None, "INSERT INTO profiles VALUES (9, 'x')"),
    (None, "CREATE TABLE zz AS SELECT * FROM customers"),
    (None, "SELECT 1; DROP TABLE payments"),
    (None, "SELECT name FROM customers; -- then\nDELETE FROM customers"),
    (None, "/* looks harmless */ DELETE FROM customers"),
    (None, "WITH d AS (SELECT 1) DELETE FROM customers"),
    (None, "EXPLAIN SELECT * FROM customers"),
    (("sqlite",), "PRAGMA writable_schema = 1"),
    (("sqlite",), "ATTACH DATABASE '/tmp/sqlgami_never.db' AS x"),
    (("sqlite",), "SELECT load_extension('x')"),
    (("sqlite",), "VACUUM INTO '/tmp/sqlgami_never_copy.db'"),
    (("sqlite",), "SELECT writefile('/tmp/sqlgami_never_w', 'y')"),
    (("sqlite",), "SELECT readfile('/etc/hosts')"),
    (("sqlite",), "REPLACE INTO profiles VALUES (9, 'x')"),
    (("duckdb",), "SELECT * FROM read_csv('/etc/passwd')"),
    (("duckdb",), "SELECT * FROM '/etc/passwd'"),
    (("duckdb",), "SELECT * FROM read_text('/etc/hosts')"),
    (("duckdb",), "SELECT * FROM read_blob('/etc/hosts')"),
    (("duckdb",), "SELECT * FROM glob('/etc/*')"),
    (("duckdb",), "COPY customers TO '/tmp/sqlgami_never.csv'"),
    (("duckdb",), "COPY (SELECT 1) TO '/tmp/sqlgami_never.csv'"),
    (("duckdb",), "INSTALL httpfs"),
    (("duckdb",), "ATTACH '/tmp/sqlgami_never.duckdb'"),
    (("duckdb",), "EXPORT DATABASE '/tmp/sqlgami_never_export'"),
    (("duckdb",), "SET enable_external_access = true"),
    (("duckdb",), "SELECT getenv('HOME')"),
    (("duckdb",), "SELECT * FROM read_parquet('https://example.com/x.parquet')"),
    (("duckdb", "postgres"), "EXPLAIN ANALYZE DELETE FROM customers"),
    (("postgres",), "SELECT pg_sleep(30)"),
    (("postgres",), "SELECT pg_read_file('/etc/passwd')"),
    (("postgres",), "SELECT pg_ls_dir('/')"),
    (("postgres",), "SELECT lo_import('/etc/passwd')"),
    (("postgres",), "COPY customers TO PROGRAM 'id'"),
    (("postgres",), "WITH d AS (DELETE FROM payments RETURNING id) SELECT * FROM d"),
    (("postgres",), "SELECT * INTO zz FROM customers"),
    (("postgres",), "SELECT set_config('statement_timeout', '0', false)"),
    (("postgres",), "SELECT pg_terminate_backend(pg_backend_pid())"),
    (("postgres",), "SELECT pg_advisory_lock(1)"),
    (("postgres",), "DO $$ BEGIN DELETE FROM payments; END $$"),
    (("postgres",), "SELECT usename, passwd FROM pg_shadow"),
]


@pytest.mark.parametrize("engines,alternative", NEVER_SENT, ids=[sql[:48] for _, sql in NEVER_SENT])
def test_unsafe_alternative_is_never_sent(engine, db, engines, alternative):
    if engines is not None and engine not in engines:
        pytest.skip("statement belongs to another engine")
    before = _counts(db)
    assert not _sent_to_engine(engine, db, alternative)
    assert _counts(db) == before


# (engine, sql, why it matters). Each passes the gate today and is sent to the engine.
# The same list guards the candidate itself (analysis._UNSAFE_FUNCTIONS is a list of names
# to refuse, so anything not on it gets through).
SENT_TODAY = [
    (
        "postgres",
        "SELECT pg_reload_conf()",
        "makes the server re-read its configuration files; it ran and returned true for a superuser",
    ),
    ("postgres", "SELECT pg_export_snapshot()", "exports a snapshot: writes a file in the data directory"),
    ("postgres", "SELECT txid_current()", "forces a transaction id to be assigned"),
    ("postgres", "SELECT current_setting('data_directory')", "reveals where the server keeps its files"),
    (
        "postgres",
        "SELECT * FROM customers FOR UPDATE",
        "asks for row locks; only the READ ONLY transaction stops it, the gate does not",
    ),
    (
        "postgres",
        "SELECT query_to_xml('DELETE FROM payments', true, false, '')",
        "runs SQL held in a string; only the READ ONLY transaction stops the DELETE",
    ),
    ("duckdb", "SELECT current_setting('home_directory')", "reveals a path on the host"),
    ("duckdb", "SELECT * FROM duckdb_settings()", "lists every setting, paths and all"),
    ("duckdb", "SELECT * FROM duckdb_secrets()", "lists the stored secrets"),
    ("duckdb", "SELECT setseed(0.5)", "changes session state: later random() calls are affected"),
]


@pytest.mark.parametrize("target,alternative,why", SENT_TODAY, ids=[sql[:48] for _, sql, _ in SENT_TODAY])
def test_statement_outside_the_schema_is_not_sent(engine, db, target, alternative, why, request):
    if engine != target:
        pytest.skip("statement belongs to another engine")
    request.applymarker(pytest.mark.xfail(strict=True, reason=why))
    assert not _sent_to_engine(engine, db, alternative)


def test_duckdb_sniff_csv_does_not_open_a_file(engine, db, tmp_path, request):
    if engine != "duckdb":
        pytest.skip("DuckDB only")
    request.applymarker(
        pytest.mark.xfail(
            strict=True,
            reason=(
                "read_csv is refused but sniff_csv is not on the list: it opens the named file "
                "(it ran against /etc/passwd and returned a row)"
            ),
        )
    )
    path = tmp_path / "outside.csv"
    path.write_text("secret,value\nx,1\n")
    assert not _sent_to_engine(engine, db, f"SELECT * FROM sniff_csv('{path}')")


def test_runaway_alternative_is_stopped_and_scoring_carries_on(engine, db):
    endless = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT COUNT(*) FROM r"
    same = (
        "WITH t AS (SELECT customer_id, SUM(total) AS s FROM orders GROUP BY customer_id) "
        "SELECT c.name, t.s FROM customers c JOIN t ON t.customer_id = c.id"
    )
    with Scorer(engine, db=db, options=Options(timeout_s=1.0)) as scorer:
        report = scorer.sample(RIGHT, QUESTION, alternatives=[endless, same])
    assert _statuses(report) == ["failed", "agrees"]
    assert _signal(report)["comparable"] == 1


def test_unsafe_candidate_given_straight_to_run_is_refused(engine, db):
    """Through Scorer the candidate is gated first. Called directly, run() raises and changes nothing."""
    before = _counts(db)
    with Scorer(engine, db=db) as scorer:
        with pytest.raises(UnsafeQueryError):
            C.run(
                "DELETE FROM customers",
                ["SELECT name FROM customers"],
                dialect=scorer.dialect,
                catalog=scorer.catalog,
                adapter=scorer.adapter,
            )
    assert _counts(db) == before


# ------------------------------------------------------------- 2. non-deterministic results


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Both queries cut the table off with LIMIT and no ORDER BY. On one engine and one "
        "plan they return the same arbitrary rows, which is counted as agreement and lifts "
        "the score (0.81 to 0.90 on all three engines)."
    ),
)
def test_limit_without_order_is_not_counted_as_agreement(scorer):
    alone = scorer.sample("SELECT name FROM customers LIMIT 2", QUESTION)
    backed = scorer.sample(
        "SELECT name FROM customers LIMIT 2", QUESTION, alternatives=["SELECT c.name FROM customers c LIMIT 2"]
    )
    assert _signal(backed).get("agreeing", 0) == 0
    assert backed.score <= alone.score


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Two queries that are the same apart from an alias both select random(). Their "
        "values differ on every run, which is reported as consistency_disagreement and "
        "lowers the score (0.88 to 0.72)."
    ),
)
def test_random_values_are_not_counted_as_disagreement(scorer):
    report = scorer.sample(
        "SELECT name, random() AS r FROM customers",
        QUESTION,
        alternatives=["SELECT c.name, random() AS r FROM customers c"],
    )
    assert not [f for f in report.findings if f.stage == "consistency"]
    assert _signal(report).get("agreeing", 0) == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The candidate picks two rows at random. Whether the alternative 'agrees' is luck: "
        "it agreed on DuckDB and Postgres and disagreed on SQLite in one run. Neither "
        "outcome says anything about the query."
    ),
)
def test_randomly_chosen_rows_are_not_compared(scorer):
    report = scorer.sample(
        "SELECT name FROM customers ORDER BY random() LIMIT 2",
        QUESTION,
        alternatives=["SELECT name FROM customers ORDER BY name LIMIT 2"],
    )
    assert _signal(report).get("comparable", 0) == 0
    assert not [f for f in report.findings if f.stage == "consistency"]


# ------------------------------------------------------------------------ 3. float noise


def test_tolerance_is_nine_significant_digits():
    """What agrees today: numbers equal to nine significant digits, whatever type carries them."""
    same = lambda a, b: fingerprint([(a,)])[0] == fingerprint([(b,)])[0]  # noqa: E731
    assert same(0.1 + 0.2, 0.3)
    assert same(100.0000001, 100.0)  # differs in the tenth significant digit
    assert not same(100.00001, 100.0)  # differs in the eighth
    assert same(2.0, 2) and same(Decimal("100.00"), 100) and same(Decimal("53.10"), 53.1)
    assert same(True, 1)
    assert not same("1", 1)  # text is not a number
    assert same(float("nan"), float("nan"))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The tolerance is relative only. 0.1 + 0.2 - 0.3 is 5.6e-17 in binary floating point "
        "and 0 in exact arithmetic; with no absolute tolerance near zero the two differ."
    ),
)
def test_noise_around_zero_agrees_with_zero():
    assert fingerprint([(0.1 + 0.2 - 0.3,)])[0] == fingerprint([(0.0,)])[0]


def test_noise_around_zero_in_sql(engine, db, request):
    """SQLite computes 0.1 + 0.2 - 0.3 in binary floating point; DuckDB and Postgres use decimals."""
    if engine == "sqlite":
        request.applymarker(
            pytest.mark.xfail(
                strict=True,
                reason="on SQLite the candidate returns 5.6e-17, the alternative 0.0, and they are reported as differing",
            )
        )
    with Scorer(engine, db=db) as scorer:
        report = scorer.sample("SELECT 0.1 + 0.2 - 0.3 AS d", QUESTION, alternatives=["SELECT 0.0 AS d"])
    assert _statuses(report) == ["agrees"]


def test_two_ways_of_writing_an_average_agree(scorer):
    report = scorer.sample(
        "SELECT AVG(total) FROM orders", QUESTION, alternatives=["SELECT SUM(o.total) / COUNT(*) FROM orders o"]
    )
    assert _statuses(report) == ["agrees"]


# ----------------------------------------------------------------------- 4. column order


def test_columns_in_another_order_agree(scorer):
    reordered = "SELECT SUM(o.total) AS spend, c.name FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name"
    report = scorer.sample(RIGHT, QUESTION, alternatives=[reordered])
    assert _statuses(report) == ["agrees_columns_reordered"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The column-order-free signature sorts the values inside each row, so it cannot tell "
        "a column swap from values that moved between columns. [(1, 2), (3, 4)] and "
        "[(2, 1), (3, 4)] match although no reordering of columns turns one into the other."
    ),
)
def test_values_swapped_in_one_row_only_do_not_match():
    assert fingerprint([(1, 2), (3, 4)])[1] != fingerprint([(2, 1), (3, 4)])[1]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Both queries name their columns paid and open_n, and one has the two conditions the "
        "wrong way round: (3, 1) against (1, 3). Column names are ignored, so this is "
        "reported as agrees_columns_reordered and lifts the score (0.73 to 0.84)."
    ),
)
def test_same_column_names_with_swapped_values_disagree(scorer):
    paid_first = (
        "SELECT SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS paid, "
        "SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_n FROM orders"
    )
    mislabelled = (
        "SELECT SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS paid, "
        "SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS open_n FROM orders"
    )
    report = scorer.sample(paid_first, QUESTION, alternatives=[mislabelled])
    assert _statuses(report) == ["differs"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Row order is ignored even when both queries say ORDER BY and say the opposite. For "
        "a question that asks for a sorted list the two answers differ, and plain 'agrees' "
        "says they do not."
    ),
)
def test_opposite_sort_orders_are_not_plain_agreement(scorer):
    report = scorer.sample(
        "SELECT name FROM customers ORDER BY name",
        "List customer names in alphabetical order",
        alternatives=["SELECT c.name FROM customers c ORDER BY c.name DESC"],
    )
    assert _statuses(report) != ["agrees"]


# ---------------------------------------------------------------------- 5. empty results


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Both queries return no rows, for different reasons and with different columns. An "
        "empty result equals any other empty result, so this counts as agreement and lifts "
        "the score (0.31 to 0.40)."
    ),
)
def test_two_empty_results_are_not_agreement(scorer):
    candidate = "SELECT name FROM customers WHERE country = 'XX'"
    alone = scorer.sample(candidate, QUESTION)
    backed = scorer.sample(
        candidate, QUESTION, alternatives=["SELECT id, name, country FROM customers WHERE country = 'YY'"]
    )
    assert _signal(backed).get("agreeing", 0) == 0
    assert backed.score <= alone.score


@pytest.mark.xfail(
    strict=True,
    reason=(
        "SUM over no rows and MAX over no rows are both a single NULL. That is counted as "
        "agreement and lifts the score (0.23 to 0.38)."
    ),
)
def test_two_lone_nulls_are_not_agreement(scorer):
    candidate = "SELECT SUM(total) FROM orders WHERE status = 'zzz'"
    alone = scorer.sample(candidate, QUESTION)
    backed = scorer.sample(
        candidate, QUESTION, alternatives=["SELECT MAX(o.total) FROM orders o WHERE o.status = 'yyy'"]
    )
    assert _signal(backed).get("agreeing", 0) == 0
    assert backed.score <= alone.score


# ------------------------------------------------------------- which alternatives are compared


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Alternatives are compared with the candidate's text after lower-casing all of it, "
        "string literals included. country = 'us' is dropped as 'the candidate again' although "
        "it returns no rows where country = 'US' returns two."
    ),
)
def test_literal_in_another_case_is_a_different_query(scorer):
    report = scorer.sample(
        "SELECT name FROM customers WHERE country = 'US'",
        QUESTION,
        alternatives=["SELECT name FROM customers WHERE country = 'us'"],
    )
    assert report.stage("consistency").status.value == "passed"
    assert _statuses(report) == ["differs"]


def test_same_text_in_another_letter_case_is_the_candidate_again(scorer):
    report = scorer.sample(RIGHT, QUESTION, alternatives=[RIGHT.replace("SELECT", "select").replace("FROM", "from")])
    assert report.stage("consistency").status.value == "skipped"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "When there are more alternatives than consistency_max_alternatives the list is cut "
        "from the front. Four samples from one model fill the limit and the two other models "
        "are never run, so one voice is counted instead of three."
    ),
)
def test_the_limit_keeps_every_source_in_play(engine, db):
    alternatives = [(f"SELECT c.name FROM customers c WHERE {i} = {i}", "model-a") for i in range(4)]
    alternatives += [
        ("SELECT name FROM customers WHERE id > 0", "model-b"),
        ("SELECT name FROM customers WHERE id >= 1", "model-c"),
    ]
    with Scorer(engine, db=db, options=Options(consistency_max_alternatives=4)) as scorer:
        report = scorer.sample("SELECT name FROM customers", QUESTION, alternatives=alternatives)
    assert _signal(report)["sources"] == 3
