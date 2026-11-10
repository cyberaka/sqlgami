"""Red-team wave 3 (b): the hard time budget at 84023f7 (commit cef4700).

score() must return within the configured budget for slow, recursive or cross-join-bomb SQL on
every engine, and an abandoned statement must not corrupt later results on the same Scorer.

These run real queries in sample mode, so they are slow (a few seconds each) and gated: Postgres
runs only when SQLGAMI_TEST_PG_DSN is set. Timing is asserted with a generous margin to stay
non-flaky. No bypass was found here — these are regression guards that should keep passing.
"""

from __future__ import annotations

import time

import pytest

from sqlgami import Options, Scorer
from conftest import ENGINES, make_db

BUDGET = 3.0
MARGIN = 20.0  # generous: we only care that it does not run unbounded


def _big_table(engine, conn):
    if engine == "duckdb":
        conn.execute("CREATE TABLE big(x INTEGER)")
        conn.execute("INSERT INTO big SELECT * FROM range(3000)")
    elif engine == "postgres":
        conn.execute("CREATE TABLE big(x int)")
        conn.execute("INSERT INTO big SELECT generate_series(1, 3000)")
    else:
        conn.execute("CREATE TABLE big(x)")
        conn.executemany("INSERT INTO big VALUES (?)", [(i,) for i in range(3000)])
        conn.commit()


@pytest.mark.parametrize("engine", ENGINES)
def test_cross_join_bomb_returns_within_budget(engine):
    conn = make_db(engine)
    try:
        _big_table(engine, conn)
        bomb = "SELECT COUNT(*) FROM big a, big b, big c, big d"
        with Scorer(engine, db=conn, options=Options(budget_s=BUDGET, timeout_s=BUDGET)) as s:
            start = time.monotonic()
            report = s.sample(bomb)
            elapsed = time.monotonic() - start
        assert elapsed <= BUDGET + MARGIN, f"{engine}: {elapsed:.1f}s"
        assert report.verdict is not None
    finally:
        conn.close()


@pytest.mark.parametrize("engine", ENGINES)
def test_abandoned_statement_does_not_corrupt_the_next(engine):
    conn = make_db(engine)
    try:
        _big_table(engine, conn)
        conn.execute("CREATE TABLE small(a)" if engine == "sqlite" else "CREATE TABLE small(a int)")
        conn.execute("INSERT INTO small VALUES (1), (2), (3)")
        if engine == "sqlite":
            conn.commit()
        bomb = "SELECT COUNT(*) FROM big a, big b, big c, big d"
        with Scorer(engine, db=conn, options=Options(budget_s=BUDGET, timeout_s=BUDGET)) as s:
            s.sample(bomb)  # abandoned
            after = s.sample("SELECT SUM(a) FROM small")
        assert after.stage("sample").status.value == "passed", after.summary()
        assert conn.execute("SELECT SUM(a) FROM small").fetchone()[0] == 6
    finally:
        conn.close()


def test_infinite_recursion_returns_within_budget():
    # DuckDB is where recursion runs on a worker thread and is re-interrupted; verify it is bounded.
    import duckdb  # noqa: F401

    conn = make_db("duckdb")
    try:
        sql = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT COUNT(*) FROM r"
        with Scorer("duckdb", db=conn, options=Options(budget_s=BUDGET, timeout_s=BUDGET)) as s:
            start = time.monotonic()
            s.sample(sql)
            elapsed = time.monotonic() - start
        assert elapsed <= BUDGET + MARGIN, f"{elapsed:.1f}s"
    finally:
        conn.close()
