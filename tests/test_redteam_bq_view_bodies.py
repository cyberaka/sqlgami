"""Red-team: a dangerous construct hidden in a VIEW or TABLE FUNCTION body.

When a candidate reads a view, ``analysis`` expands the view's body into the resolved tree
(views on views included). But ``safety.review`` runs at analysis.py:216 on the RAW candidate
tree, BEFORE the resolver expands views (analysis.py:220+). So the gate sees ``FROM my_view`` --
a plain table reference -- and never the ``EXTERNAL_QUERY`` / ``INFORMATION_SCHEMA`` / unknown
function sitting inside the view body. The expanded construct ends up in ``analysis.resolved``
yet is never re-reviewed.

Expected behaviour (what the builder's B4 -- TVF/view bodies in the catalogue -- should enforce):
reading a view or TVF whose body contains a blocked construct must be refused (not sent),
including through nesting (a view over a view over a TVF). The reachability tests below prove the
body really is pulled into the resolved tree, so the non-refusal has teeth.

TVF bodies are not held by the catalogue yet (``CREATE TABLE FUNCTION`` builds an empty catalog),
so those tests detect the gap at runtime and skip with a clear message until B4 lands; they then
activate automatically.
"""

from __future__ import annotations

import pytest

from sqlgami import analysis as A
from sqlgami.catalog import Catalog

DIALECT = "bigquery"

VIEW_DDL = """
CREATE TABLE base (id INT64, v INT64);
CREATE VIEW bad_ext AS SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT * FROM secrets');
CREATE VIEW bad_info AS SELECT job_id, query FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT;
CREATE VIEW bad_udf AS SELECT mydataset.my_udf(id) AS v FROM base;
CREATE VIEW wrap_ext AS SELECT * FROM bad_ext;
CREATE VIEW wrap_wrap AS SELECT * FROM wrap_ext;
CREATE VIEW good AS SELECT id, v FROM base;
"""

CAT = Catalog.from_ddl(VIEW_DDL, dialect=DIALECT)


def _sent(sql: str, catalog=CAT) -> bool:
    a = A.analyse(sql, catalog, DIALECT)
    return (
        a.kind == A.QUERY
        and not a.unsafe_functions
        and not a.unlisted_functions
        and not a.script_statements
    )


def _resolved_sql(sql: str, catalog=CAT) -> str:
    a = A.analyse(sql, catalog, DIALECT)
    return a.resolved.sql(dialect=DIALECT) if a.resolved is not None else ""


# --- reachability: the view body really is expanded into the resolved tree (passing) -----------
def test_view_body_external_query_reaches_the_resolved_tree():
    assert "EXTERNAL_QUERY" in _resolved_sql("SELECT * FROM bad_ext").upper()


def test_nested_view_body_reaches_the_resolved_tree():
    # view over view over view still pulls EXTERNAL_QUERY all the way in
    assert "EXTERNAL_QUERY" in _resolved_sql("SELECT * FROM wrap_wrap").upper()


def test_view_body_unknown_udf_reaches_the_resolved_tree():
    assert "MY_UDF" in _resolved_sql("SELECT * FROM bad_udf").upper()


# --- the gate must refuse a candidate that reads a view with a blocked body ------------------
BAD_VIEW_READS = [
    ("read_view_with_external_query", "SELECT * FROM bad_ext"),
    ("read_view_with_information_schema", "SELECT job_id, query FROM bad_info"),
    ("read_view_with_unknown_udf", "SELECT * FROM bad_udf"),
    ("read_view_over_view_external_query", "SELECT * FROM wrap_ext"),
    ("read_view_over_view_over_view_external_query", "SELECT * FROM wrap_wrap"),
]


@pytest.mark.parametrize("label,sql", BAD_VIEW_READS, ids=[c[0] for c in BAD_VIEW_READS])
def test_reading_a_view_with_a_blocked_body_is_not_sent(label, sql):
    assert not _sent(sql)


# --- no over-block: ordinary tables and benign views still go through (passing) -----------------
SAFE_READS = [
    ("plain_table", "SELECT id, v FROM base"),
    ("benign_view", "SELECT id, v FROM good"),
]


@pytest.mark.parametrize("label,sql", SAFE_READS, ids=[c[0] for c in SAFE_READS])
def test_benign_reads_are_not_over_blocked(label, sql):
    assert _sent(sql)


# --- TABLE FUNCTION bodies: write against the expected interface; skip until B4 holds them ------
TVF_DDL = """
CREATE TABLE base (id INT64, v INT64);
CREATE TABLE FUNCTION tvf_ext() AS SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT * FROM secrets');
CREATE TABLE FUNCTION tvf_info() AS SELECT query FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT;
CREATE VIEW view_over_tvf AS SELECT * FROM tvf_ext();
"""


def _tvf_catalog_holds_bodies() -> bool:
    """True once the catalogue can hold a CREATE TABLE FUNCTION body (B4)."""
    try:
        cat = Catalog.from_ddl(TVF_DDL, dialect=DIALECT)
    except Exception:  # noqa: BLE001 -- DDL not understood yet
        return False
    a = A.analyse("SELECT * FROM tvf_ext()", cat, DIALECT)
    # held if the TVF body is expanded into the resolved tree
    return a.resolved is not None and "EXTERNAL_QUERY" in a.resolved.sql(dialect=DIALECT).upper()


_TVF_READY = _tvf_catalog_holds_bodies()

TVF_READS = [
    ("call_tvf_with_external_query", "SELECT * FROM tvf_ext()"),
    ("call_tvf_with_information_schema", "SELECT * FROM tvf_info()"),
    ("view_over_tvf_with_external_query", "SELECT * FROM view_over_tvf"),
]


@pytest.mark.skipif(not _TVF_READY, reason="catalogue does not hold CREATE TABLE FUNCTION bodies yet (B4 pending)")
@pytest.mark.parametrize("label,sql", TVF_READS, ids=[c[0] for c in TVF_READS])
def test_calling_a_tvf_with_a_blocked_body_is_not_sent(label, sql):
    cat = Catalog.from_ddl(TVF_DDL, dialect=DIALECT)
    a = A.analyse(sql, cat, DIALECT)
    sent = a.kind == A.QUERY and not a.unsafe_functions and not a.unlisted_functions and not a.script_statements
    assert not sent
