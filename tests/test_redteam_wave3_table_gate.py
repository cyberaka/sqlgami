"""Red-team wave 3 (c): the table gate (safety.review_tables) at main 84023f7.

The gate refuses reads of the engine's own catalogues (pg_catalog/pg_*/sqlite_*/pragma_*/
duckdb_*/information_schema) unless the host names them in allowed_tables. These tests try to
smuggle such a read past it. All classification is static (analysis.analyse, no execution); one
runtime check uses a throwaway temp DB with no secrets.

CONTAINED cases are regression guards that pass today. The BYPASS case is a confirmed hole,
xfail(strict).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile

import pytest

from sqlgami import analysis as A


def _sent(sql: str, dialect: str) -> bool:
    a = A.analyse(sql, None, dialect)
    return a.kind == A.QUERY and not a.unsafe_functions and not a.unlisted_functions and not a.script_statements


CONTAINED = [
    ("cte_body_reads_catalog", "WITH x AS (SELECT * FROM pg_shadow) SELECT * FROM x", "postgres"),
    ("subquery_reads_catalog", "SELECT * FROM (SELECT passwd FROM pg_shadow) q", "postgres"),
    ("union_branch_catalog", "SELECT 1 AS a UNION ALL SELECT passwd FROM pg_shadow", "postgres"),
    ("join_catalog", "SELECT t.x FROM t JOIN pg_authid a ON a.oid = t.id", "postgres"),
    ("quoted_schema", 'SELECT * FROM "pg_catalog"."pg_shadow"', "postgres"),
    ("uppercase_qualified", "SELECT * FROM PG_CATALOG.PG_SHADOW", "postgres"),
    ("mixed_case_quoted_name", 'SELECT * FROM pg_catalog."Pg_Shadow"', "postgres"),
    ("only_keyword", "SELECT * FROM ONLY pg_shadow", "postgres"),
    ("information_schema", "SELECT * FROM information_schema.tables", "postgres"),
    ("sqlite_master_main", "SELECT sql FROM main.sqlite_master", "sqlite"),
    ("sqlite_master_temp", "SELECT sql FROM temp.sqlite_master", "sqlite"),
    ("sqlite_schema_alias", "SELECT sql FROM sqlite_schema", "sqlite"),
    ("sqlite_master_quoted", 'SELECT sql FROM "sqlite_master"', "sqlite"),
    ("bare_pragma", "SELECT * FROM pragma_database_list", "sqlite"),
    ("duckdb_settings_bare", "SELECT * FROM duckdb_settings", "duckdb"),
    ("duckdb_secrets_bare", "SELECT * FROM duckdb_secrets", "duckdb"),
    ("duckdb_databases_bare", "SELECT * FROM duckdb_databases", "duckdb"),
    ("duckdb_pg_catalog", "SELECT * FROM pg_catalog.pg_type", "duckdb"),
]


@pytest.mark.parametrize("label,sql,dialect", CONTAINED, ids=[c[0] for c in CONTAINED])
def test_system_table_read_is_not_sent(label, sql, dialect):
    assert not _sent(sql, dialect)


SAFE = [
    ("user_table", "SELECT a FROM my_table", "postgres"),
    ("cte_named_like_catalog_reads_itself", "WITH pg_shadow AS (SELECT 1 AS passwd) SELECT * FROM pg_shadow", "postgres"),
    ("user_table_join", "SELECT a.x FROM orders a JOIN items b ON b.order_id = a.id", "sqlite"),
]


@pytest.mark.parametrize("label,sql,dialect", SAFE, ids=[c[0] for c in SAFE])
def test_user_tables_and_shadowing_ctes_pass(label, sql, dialect):
    assert _sent(sql, dialect)


# BYPASS: aliasing a decoy table AS a system-table name poisons the global _defined_names set
# (which includes table-alias names, not just CTE names), so a real unqualified read of that
# system table in the same statement is skipped by review_tables and sent.
BYPASS_ALIAS_SHADOW = [
    ("alias_decoy_then_real_sqlite", "SELECT m.name, m.sql FROM t AS sqlite_master, sqlite_master m", "sqlite"),
    ("alias_decoy_then_real_pg", "SELECT s.* FROM users AS pg_shadow, pg_shadow s", "postgres"),
    ("alias_decoy_then_real_duck", "SELECT d.* FROM t AS duckdb_settings, duckdb_settings d", "duckdb"),
]


@pytest.mark.xfail(strict=True, reason="a table aliased AS a system-table name adds that name to the global defined-names set, so a real read of the system table in the same statement is skipped and sent")
@pytest.mark.parametrize("label,sql,dialect", BYPASS_ALIAS_SHADOW, ids=[c[0] for c in BYPASS_ALIAS_SHADOW])
def test_alias_shadowing_does_not_smuggle_a_catalog_read(label, sql, dialect):
    assert not _sent(sql, dialect)


def test_alias_shadow_really_discloses_sqlite_master():
    """Runtime proof on a throwaway temp DB: the aliased form reads the real sqlite_master."""
    sql = "SELECT m.name, m.sql FROM t AS sqlite_master, sqlite_master m WHERE m.type = 'table'"
    assert _sent(sql, "sqlite"), "precondition: the gate sends it today"
    assert not _sent("SELECT name, sql FROM sqlite_master", "sqlite"), "the direct form is blocked"
    tmp = tempfile.mkdtemp()
    try:
        conn = sqlite3.connect(os.path.join(tmp, "throwaway.db"))
        conn.executescript("CREATE TABLE t(x); INSERT INTO t VALUES (1); CREATE TABLE secret_users(name, pw);")
        names = [r[0] for r in conn.execute(sql).fetchall()]
        conn.close()
        if "secret_users" in names:
            pytest.xfail("the aliased form is sent and discloses the real sqlite_master (lists secret_users)")
        assert "secret_users" not in names
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
