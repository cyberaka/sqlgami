"""The term catalogue: loading, validation, matching, and judging SQL against declared terms."""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlgami import Scorer
from sqlgami.errors import ConfigurationError
from sqlgami.report import Severity
from sqlgami.terms import Catalogue, load_terms, normalise

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "terms" / "hr"
HR_DDL = """
CREATE TABLE hr_emp_period_snapshot (assoc_id INTEGER, period_end_dt DATE, snap_dt DATE, status_cd TEXT, hire_dt DATE,
    lvl_2_id INTEGER, lvl_3_id INTEGER, lvl_4_id INTEGER, lvl_5_id INTEGER, lvl_6_id INTEGER, lvl_7_id INTEGER,
    lvl_2_id_fn INTEGER, lvl_3_id_fn INTEGER, lvl_4_id_fn INTEGER, lvl_5_id_fn INTEGER, lvl_6_id_fn INTEGER, lvl_7_id_fn INTEGER,
    invol_exit_ind INTEGER, PRIMARY KEY (assoc_id, period_end_dt, snap_dt));
CREATE TABLE hr_pay_period_dim (ppe_dt DATE, freq_cd TEXT, period_seq INTEGER, is_current_flag BOOLEAN);
CREATE TABLE hr_employee_dim (assoc_id INTEGER PRIMARY KEY, hire_dt DATE, term_dt DATE);
"""
TERMS = """term,phrases,kind,table,sql,nulls,description
not_on_leave,not on leave;currently not on leave,filter,hr_emp_period_snapshot,status_cd IS DISTINCT FROM 'L',include,
last_n_pay_periods,last {n} pay periods,window,hr_pay_period_dim,period_seq BETWEEN -({n}-1) AND 0,,
n_pay_periods_ago,{n} pay periods ago,window,hr_pay_period_dim,period_seq = -{n},,
trailing_moving_average,trailing {n} period moving average,window,,ROWS BETWEEN {n}-1 PRECEDING AND CURRENT ROW,,
hire_date,hire date;hired in,source,,hr_employee_dim.hire_dt,,
my_organisation,my organisation,chain,hr_emp_period_snapshot,"lvl_2_id, lvl_3_id, lvl_4_id, lvl_5_id, lvl_6_id, lvl_7_id, lvl_2_id_fn, lvl_3_id_fn, lvl_4_id_fn, lvl_5_id_fn, lvl_6_id_fn, lvl_7_id_fn = {manager_id}",,
headcount,headcount,,,,,documentation only
"""
KPIS = """kpi,phrases,formula,format,reference_sql,description
basis_points,basis points;bp,CAST(FLOOR({rate} * 10000) AS BIGINT),integer floor,,
yoy_growth,year-over-year;yoy,({current} - {prior}) * 10000 / {prior},integer floor,,
"""
PARAMS = "name,type,example_values\nmanager_id,integer,12003\nrate,input,\ncurrent,input,\nprior,input,\n"
CHAIN = " OR ".join(f"s.lvl_{n}_id = {{manager_id}}" for n in range(2, 8)) + " OR " + " OR ".join(f"s.lvl_{n}_id_fn = {{manager_id}}" for n in range(2, 8))


def catalogue() -> Catalogue:
    return Catalogue.from_sheets({"terms": TERMS, "kpis": KPIS, "parameters": PARAMS})


def scorer() -> Scorer:
    return Scorer("duckdb", catalog=HR_DDL, terms=catalogue())


def _term_findings(report):
    return {f.code: f for f in report.findings if f.code.startswith("term_") or f.code == "undefined_term"}


# ------------------------------------------------------------------ loading and validation


def test_example_catalogue_loads_from_csv_folder_and_xlsx():
    folder = load_terms(EXAMPLE)
    assert {t.name for t in folder.terms} >= {"not_on_leave", "last_n_pay_periods", "my_organisation", "headcount"}
    assert folder.kpi("basis_points") is not None and folder.examples and folder.parameters
    pytest.importorskip("openpyxl")
    book = load_terms(EXAMPLE.parent / "hr_terms.xlsx")
    assert [t.name for t in book.terms] == [t.name for t in folder.terms]
    assert [k.formula for k in book.kpis] == [k.formula for k in folder.kpis]


def test_validation_reports_sheet_row_and_column():
    bad = Catalogue.from_sheets(
        {
            "terms": "term,phrases,kind,table,sql,nulls,description\n"
            "leaver,leaver,filter,t,status_cd = = 'T',,\n"
            "dup,dup;dupes,filter,t,x = 1,maybe,\n"
            "dup,other,,,,,\n"
            ",no name,filter,t,x = 1,,\n"
            "nophrase,,filter,t,x = 1,,\n"
            "src,source col,source,,hire_dt,,\n",
            "kpis": "kpi,phrases,formula,format,reference_sql,description\nrate,rate,{leavers} / {headcount},integer,,\n",
            "examples": "id,question,params,correct_sql,expected_result,dataset\nex1,,,SELECT 1,,\n",
        }
    )
    problems = {str(p) for p in bad.validate()}
    assert any(p.startswith("Terms row 1: sql: does not parse") for p in problems), problems
    assert "Terms row 2: nulls: 'maybe' is not include, exclude or separate" in problems
    assert "Terms row 3: term: 'dup' is also on row 2" in problems
    assert "Terms row 4: term: is blank" in problems
    assert "Terms row 5: phrases: is blank; the term can never be matched" in problems
    assert "Terms row 6: sql: a source is written as table.column" in problems
    assert any(p.startswith("KPIs row 1: formula: unknown term 'leavers'") for p in problems)
    assert "Examples row 1: question: is blank" in problems
    with pytest.raises(ConfigurationError, match="problems"):
        Scorer("duckdb", catalog=HR_DDL, terms=bad)


def test_spreadsheet_values_stay_text():
    from sqlgami.terms import _cell_text

    assert _cell_text(12003.0) == "12003" and _cell_text("007") == "007" and _cell_text(None) == ""
    assert _cell_text(True) == "TRUE"


# ------------------------------------------------------------------ matching


def test_phrases_match_exactly_after_normalisation_and_bind_numbers():
    c = catalogue()
    found = {m.name: m for m in c.match("How many people in MY organisation are currently not-on-leave over the last 26 pay-periods, with a trailing 4-period moving average, year-over-year?")}
    assert set(found) == {"my_organisation", "not_on_leave", "last_n_pay_periods", "trailing_moving_average", "yoy_growth"}
    assert found["last_n_pay_periods"].bindings == {"n": "26"} and found["trailing_moving_average"].bindings == {"n": "4"}
    assert found["not_on_leave"].phrase == "currently not on leave"  # the longer phrase wins
    assert not c.match("How many orders were paid?")
    assert normalise("Pay-Periods Ago") == " pay periods ago " and normalise("deleted_at") == " deleted_at "


# ------------------------------------------------------------------ judging: filters


def test_filter_term_satisfied_contradicted_or_missing():
    base = f"SELECT COUNT(DISTINCT s.assoc_id) AS n FROM hr_emp_period_snapshot s WHERE ({CHAIN}) AND "
    question = "How many people in my organisation are not on leave?"
    sc = scorer()
    for ok in ("s.status_cd IS DISTINCT FROM 'L'", "COALESCE(s.status_cd, '') <> 'L'", "(s.status_cd <> 'L' OR s.status_cd IS NULL)"):
        report = sc.explain(base + ok, question, params={"manager_id": 7})
        assert not _term_findings(report), (ok, report.summary())
        assert [o["status"] for o in report.signals["terms"]["outcomes"] if o["term"] == "not_on_leave"] == ["satisfied"]
    for bad in ("s.status_cd <> 'L'", "s.status_cd != 'L'", "s.status_cd NOT IN ('L')", "NOT (s.status_cd = 'L')"):
        report = sc.explain(base + bad, question, params={"manager_id": 7})
        finding = _term_findings(report)["term_contradicted"]
        assert finding.severity == Severity.HIGH and "NULL" in finding.message, bad
        assert report.score <= 0.4
    opposite = sc.explain(base + "s.status_cd = 'L'", question, params={"manager_id": 7})
    assert _term_findings(opposite)["term_contradicted"].severity == Severity.HIGH
    missing = sc.explain(base + "s.snap_dt = s.period_end_dt", question, params={"manager_id": 7})
    assert _term_findings(missing)["term_missing"].severity == Severity.HIGH
    other = sc.explain(base + "s.status_cd IN ('A', 'T')", question, params={"manager_id": 7})
    assert _term_findings(other)["term_differs"].severity == Severity.LOW


def test_filter_inside_an_or_branch_is_reported_as_not_applied_to_every_row():
    sql = (
        "SELECT COUNT(DISTINCT s.assoc_id) AS n FROM hr_emp_period_snapshot s WHERE "
        + " OR ".join(f"s.lvl_{n}_id = {{manager_id}}" for n in range(2, 8))
        + " OR " + " OR ".join(f"s.lvl_{n}_id_fn = {{manager_id}}" for n in range(2, 7))
        + " OR s.lvl_7_id_fn = {manager_id} AND s.status_cd IS DISTINCT FROM 'L'"
    )
    report = scorer().explain(sql, "How many people in my organisation are not on leave?", params={"manager_id": 7})
    finding = _term_findings(report)["term_missing"]
    assert "inside an OR branch" in finding.message and finding.severity == Severity.HIGH


def test_term_scoped_to_a_table_the_query_does_not_read():
    report = scorer().explain(
        "SELECT COUNT(*) FROM hr_employee_dim WHERE term_dt IS NULL", "How many people are not on leave?"
    )
    assert _term_findings(report)["term_source_not_read"].severity == Severity.MEDIUM


# ------------------------------------------------------------------ judging: windows


@pytest.mark.parametrize(
    "where,expected",
    [
        ("pp.period_seq BETWEEN -25 AND 0", None),
        ("pp.period_seq >= -25 AND pp.period_seq <= 0", None),
        ("pp.period_seq > -26 AND pp.period_seq <= 0", None),
        ("pp.period_seq BETWEEN -26 AND 0", "27"),
        ("pp.period_seq BETWEEN -24 AND 0", "25"),
    ],
)
def test_window_term_counts_the_range(where, expected):
    sql = (
        "SELECT COUNT(*) AS n FROM hr_emp_period_snapshot s JOIN hr_pay_period_dim pp ON pp.ppe_dt = s.period_end_dt "
        f"WHERE ({CHAIN}) AND {where}"
    )
    report = scorer().explain(sql, "Active person-periods in my organisation over the last 26 pay periods", params={"manager_id": 7})
    findings = _term_findings(report)
    if expected is None:
        assert "term_window" not in findings, report.summary()
    else:
        assert findings["term_window"].severity == Severity.HIGH and expected in findings["term_window"].message


def test_window_constants_defined_in_a_cte_are_followed():
    sql = (
        "WITH params AS (SELECT -25 AS from_seq, 0 AS to_seq) "
        "SELECT COUNT(*) AS n FROM hr_emp_period_snapshot s JOIN hr_pay_period_dim pp ON pp.ppe_dt = s.period_end_dt "
        f"CROSS JOIN params WHERE ({CHAIN}) AND pp.period_seq BETWEEN params.from_seq AND params.to_seq"
    )
    report = scorer().explain(sql, "Person-periods in my organisation over the last 26 pay periods", params={"manager_id": 7})
    assert "term_window" not in _term_findings(report) and "term_missing" not in _term_findings(report), report.summary()


def test_point_window_and_frame():
    sql = (
        "SELECT COUNT(*) AS n FROM hr_emp_period_snapshot s JOIN hr_pay_period_dim pp ON pp.ppe_dt = s.period_end_dt "
        f"WHERE ({CHAIN}) AND pp.period_seq = -25"
    )
    report = scorer().explain(sql, "Headcount in my organisation 26 pay periods ago", params={"manager_id": 7})
    assert _term_findings(report)["term_window"].severity == Severity.HIGH
    frame = (
        "SELECT pp.period_seq, AVG(COUNT(*)) OVER (ORDER BY pp.period_seq ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS avg_n "
        f"FROM hr_emp_period_snapshot s JOIN hr_pay_period_dim pp ON pp.ppe_dt = s.period_end_dt WHERE ({CHAIN}) GROUP BY pp.period_seq"
    )
    report = scorer().explain(frame, "Trailing 4-period moving average of headcount in my organisation", params={"manager_id": 7})
    finding = _term_findings(report)["term_window"]
    assert finding.severity == Severity.HIGH and "5 rows" in finding.message
    good = frame.replace("4 PRECEDING", "3 PRECEDING")
    assert "term_window" not in _term_findings(scorer().explain(good, "Trailing 4-period moving average of headcount in my organisation", params={"manager_id": 7}))


# ------------------------------------------------------------------ judging: source, KPIs, undefined


def test_source_term_flags_a_column_read_from_the_wrong_table():
    question = "People in my organisation hired in the last 13 pay periods"
    wrong = (
        "SELECT COUNT(*) AS n FROM hr_emp_period_snapshot s JOIN hr_pay_period_dim pp ON pp.ppe_dt = s.period_end_dt "
        f"WHERE ({CHAIN}) AND pp.period_seq BETWEEN -12 AND 0 AND s.hire_dt >= pp.ppe_dt"
    )
    report = scorer().explain(wrong, question, params={"manager_id": 7})
    assert _term_findings(report)["term_source"].severity == Severity.MEDIUM
    right = wrong.replace("s.hire_dt", "e.hire_dt").replace("FROM hr_emp_period_snapshot s", "FROM hr_emp_period_snapshot s JOIN hr_employee_dim e ON e.assoc_id = s.assoc_id")
    assert "term_source" not in _term_findings(scorer().explain(right, question, params={"manager_id": 7}))


def test_kpi_format_and_direction():
    question = "Year-over-year growth of headcount in my organisation, in basis points"
    good = (
        "WITH cur AS (SELECT COUNT(*) AS n FROM hr_emp_period_snapshot s WHERE " + CHAIN + "), "
        "prior AS (SELECT COUNT(*) AS n FROM hr_emp_period_snapshot s WHERE " + CHAIN + ") "
        "SELECT CAST(FLOOR((cur.n - prior.n) * 10000.0 / prior.n) AS BIGINT) AS growth_bp FROM cur, prior"
    )
    sc = scorer()
    assert not {k for k in _term_findings(sc.explain(good, question, params={"manager_id": 7})) if k != "undefined_term"}
    decimals = good.replace("CAST(FLOOR((cur.n - prior.n) * 10000.0 / prior.n) AS BIGINT)", "(cur.n - prior.n) * 10000.0 / prior.n")
    assert _term_findings(sc.explain(decimals, question, params={"manager_id": 7}))["term_format"].severity == Severity.MEDIUM
    rounded = good.replace("FLOOR", "ROUND")
    assert "floored" in _term_findings(sc.explain(rounded, question, params={"manager_id": 7}))["term_format"].message
    reversed_ = good.replace("(cur.n - prior.n)", "(prior.n - cur.n)")
    finding = _term_findings(sc.explain(reversed_, question, params={"manager_id": 7}))["term_formula"]
    assert finding.severity == Severity.MEDIUM and "other way round" in finding.message


def test_documentation_only_term_is_undefined_and_caps_confidence():
    sql = f"SELECT COUNT(DISTINCT s.assoc_id) AS headcount FROM hr_emp_period_snapshot s WHERE ({CHAIN}) AND s.status_cd = 'A'"
    report = scorer().explain(sql, "Current active headcount in my organisation", params={"manager_id": 7})
    finding = _term_findings(report)["undefined_term"]
    assert finding.severity == Severity.LOW and "headcount" in finding.message
    assert report.confidence.cap == 0.75 and "headcount" in (report.confidence.cap_reason or "")
    plain = Scorer("duckdb", catalog=HR_DDL).explain(sql, "Current active headcount in my organisation", params={"manager_id": 7})
    assert "terms" not in plain.signals  # no catalogue, no term findings, nothing capped for words


def test_chain_term_is_the_access_rule_when_the_question_asks_for_it():
    partial = "SELECT COUNT(*) FROM hr_emp_period_snapshot s WHERE " + " OR ".join(f"s.lvl_{n}_id = {{manager_id}}" for n in range(2, 8))
    report = scorer().explain(partial, "Headcount in my organisation", params={"manager_id": 7})
    finding = next(f for f in report.findings if f.code == "required_predicate")
    assert finding.severity == Severity.HIGH and "leaves out" in finding.message and finding.evidence["term"] == "my_organisation"
    # a question that does not ask for the term is not held to it (a policy that always applies
    # belongs in required_predicates)
    assert not scorer().explain(partial, "Headcount by reporting level", params={"manager_id": 7}).has("required_predicate")
    # a one-column chain is a plain filter: tenant_id = {tenant_id}
    one = Catalogue.from_sheets({"terms": TERMS + "tenant,tenant;for tenant,chain,,lvl_2_id = {manager_id},,\n", "kpis": KPIS, "parameters": PARAMS})
    sc = Scorer("duckdb", catalog=HR_DDL, terms=one)
    assert not {k for k in _term_findings(sc.explain("SELECT COUNT(*) FROM hr_emp_period_snapshot WHERE lvl_2_id = {manager_id}", "Rows for tenant 7", params={"manager_id": 7}))}
    assert "term_missing" in _term_findings(sc.explain("SELECT COUNT(*) FROM hr_emp_period_snapshot", "Rows for tenant 7", params={"manager_id": 7}))


def test_glossary_yaml_with_a_terms_section_is_a_catalogue():
    glossary = (
        "definitions:\n  headcount:\n    meaning: people\n"
        "terms:\n  - term: not_on_leave\n    phrases: not on leave\n    kind: filter\n    table: hr_emp_period_snapshot\n"
        "    sql: status_cd IS DISTINCT FROM 'L'\n    nulls: include\n"
    )
    sc = Scorer("duckdb", catalog=HR_DDL, glossary=glossary)
    assert sc.terms is not None and sc.terms.term("not_on_leave") is not None
    report = sc.explain("SELECT COUNT(*) FROM hr_emp_period_snapshot WHERE status_cd <> 'L'", "How many are not on leave?")
    assert _term_findings(report)["term_contradicted"].severity == Severity.HIGH


# ------------------------------------------------------------------ matching rules the audit asked for


def test_phrases_match_whole_tokens_only_and_spans_do_not_overlap():
    c = Catalogue.from_sheets(
        {
            "terms": "term,phrases,kind,table,sql,nulls,description\n"
            "voluntary,voluntary,filter,t,invol_exit_ind = 0,,\n"
            "involuntary,involuntary,filter,t,invol_exit_ind = 1,,\n"
            "on_leave,on leave,filter,t,status_cd = 'L',,\n"
            "not_on_leave,not on leave,filter,t,status_cd IS DISTINCT FROM 'L',include,\n"
            "soft_deleted,deleted;soft-deleted,filter,t,deleted_at IS NOT NULL,,\n"
            "not_deleted,non-deleted;not deleted;undeleted,filter,t,deleted_at IS NULL,,\n"
        }
    )
    names = [m.name for m in c.match("Involuntary leavers only")]
    assert names == ["involuntary"]  # "voluntary" is not matched inside "involuntary"
    names = [m.name for m in c.match("people not on leave")]
    assert names == ["not_on_leave"]  # the longer phrase consumes the span; "on leave" does not also fire
    for question in ("non-deleted tenants", "not deleted tenants", "undeleted tenants"):
        assert [m.name for m in c.match(question)] == ["not_deleted"], question
    assert [m.name for m in c.match("rows where deleted_at IS NULL")] == []  # an identifier is not the word
    assert [m.name for m in c.match("the deleted rows")] == ["soft_deleted"]


def test_conditions_inside_conditional_aggregation_count():
    sql = (
        "SELECT COUNT(DISTINCT CASE WHEN s.status_cd = 'A' THEN s.assoc_id END) AS active_headcount, "
        "SUM(CASE WHEN s.invol_exit_ind = 1 THEN 1 ELSE 0 END) AS invol_n, "
        "COUNT(*) FILTER (WHERE s.invol_exit_ind = 0) AS vol_n "
        f"FROM hr_emp_period_snapshot s WHERE ({CHAIN})"
    )
    terms = TERMS + "active,active;active headcount,filter,hr_emp_period_snapshot,status_cd = 'A',,\n" \
        "voluntary,voluntary,filter,hr_emp_period_snapshot,invol_exit_ind = 0,,\n" \
        "involuntary,involuntary,filter,hr_emp_period_snapshot,invol_exit_ind = 1,,\n"
    cat = Catalogue.from_sheets({"terms": terms, "kpis": KPIS, "parameters": PARAMS})
    sc = Scorer("duckdb", catalog=HR_DDL, terms=cat)
    report = sc.explain(sql, "Active headcount and voluntary and involuntary leavers in my organisation", params={"manager_id": 7})
    assert not {k for k in _term_findings(report) if k != "undefined_term"}, report.summary()
    # the contradiction check applies in those positions too
    bad = sql.replace("CASE WHEN s.status_cd = 'A'", "CASE WHEN s.status_cd != 'L'")
    report = sc.explain(bad, "Headcount not on leave in my organisation", params={"manager_id": 7})
    assert _term_findings(report)["term_contradicted"].severity == Severity.HIGH


def test_a_negated_phrase_expects_the_inverse_and_a_sibling_term_is_a_note():
    terms = (
        "term,phrases,kind,table,sql,nulls,description\n"
        "soft_deleted,soft-deleted;deleted,filter,hr_emp_period_snapshot,hire_dt IS NOT NULL,,\n"
        "not_deleted,non-deleted,filter,hr_emp_period_snapshot,hire_dt IS NULL,,\n"
    )
    cat = Catalogue.from_sheets({"terms": terms})
    sc = Scorer("duckdb", catalog=HR_DDL, terms=cat)
    excluded = sc.explain("SELECT COUNT(*) FROM hr_emp_period_snapshot WHERE hire_dt IS NULL", "How many rows, excluding soft-deleted ones?")
    assert not {k for k in _term_findings(excluded) if k != "term_differs"}, excluded.summary()
    mentioned = sc.explain("SELECT COUNT(*) FROM hr_emp_period_snapshot WHERE hire_dt IS NULL", "How many rows (NULL when the row is soft-deleted)?")
    assert _term_findings(mentioned)["term_differs"].severity == Severity.LOW  # the query applies the sibling not_deleted
    asked = sc.explain("SELECT COUNT(*) FROM hr_emp_period_snapshot WHERE hire_dt IS NULL", "How many soft-deleted rows are there?")
    assert "term_differs" in _term_findings(asked) and "term_contradicted" not in _term_findings(asked)


# ------------------------------------------------------------------ rank terms


RANK_TERMS = TERMS + "top_dense,top {n} dense-ranks;top {n} dense ranks,rank,,DENSE_RANK() <= {n},,\ntop_n,top {n};{n} largest,rank,,LIMIT {n},,\n"
PER_NODE = "SELECT s.lvl_3_id, COUNT(DISTINCT s.assoc_id) AS active_n FROM hr_emp_period_snapshot s WHERE s.status_cd = 'A' GROUP BY s.lvl_3_id"


def _rank_scorer() -> Scorer:
    return Scorer("duckdb", catalog=HR_DDL, terms=Catalogue.from_sheets({"terms": RANK_TERMS, "kpis": KPIS, "parameters": PARAMS}))


@pytest.mark.parametrize(
    "sql,status,code",
    [
        # the cut the catalogue asks for, at the question's number, through a CTE column
        (f"WITH n AS ({PER_NODE}), r AS (SELECT n.*, DENSE_RANK() OVER (ORDER BY active_n DESC) AS rk FROM n) SELECT lvl_3_id, active_n, rk FROM r WHERE rk <= 3 ORDER BY rk, lvl_3_id", "satisfied", None),
        (f"WITH n AS ({PER_NODE}) SELECT lvl_3_id, active_n FROM n QUALIFY DENSE_RANK() OVER (ORDER BY active_n DESC) <= 3", "satisfied", None),
        (f"WITH n AS ({PER_NODE}), r AS (SELECT n.*, DENSE_RANK() OVER (ORDER BY active_n DESC) AS rk FROM n) SELECT * FROM r WHERE rk < 4", "satisfied", None),
        # the wrong number
        (f"WITH n AS ({PER_NODE}), r AS (SELECT n.*, DENSE_RANK() OVER (ORDER BY active_n DESC) AS rk FROM n) SELECT * FROM r WHERE rk <= 5", "contradicted", "term_rank"),
        # another tie rule: RANK, ROW_NUMBER or a plain LIMIT drop or skip ties
        (f"WITH n AS ({PER_NODE}), r AS (SELECT n.*, RANK() OVER (ORDER BY active_n DESC) AS rk FROM n) SELECT * FROM r WHERE rk <= 3", "contradicted", "term_rank"),
        (f"WITH n AS ({PER_NODE}) SELECT * FROM n ORDER BY active_n DESC LIMIT 3", "contradicted", "term_rank"),
        (f"WITH n AS ({PER_NODE}), r AS (SELECT n.*, DENSE_RANK() OVER (ORDER BY active_n DESC) AS rk FROM n) SELECT * FROM r WHERE rk <= 3 LIMIT 3", "contradicted", "term_rank"),
        # no cut at all
        (f"WITH n AS ({PER_NODE}) SELECT * FROM n ORDER BY active_n DESC", "missing", "term_rank"),
    ],
)
def test_rank_term_checks_the_cut_and_the_tie_rule(sql, status, code):
    report = _rank_scorer().explain(sql, "Rank the LVL_3 nodes by active headcount and return the top 3 dense-ranks, keeping ties")
    outcome = next(o for o in report.signals["terms"]["outcomes"] if o["term"] == "top_dense")
    assert outcome["status"] == status, (outcome, report.summary())
    findings = _term_findings(report)
    if code is None:
        assert "term_rank" not in findings
    else:
        assert findings[code].severity == Severity.HIGH and report.score <= 0.4
        assert "DENSE_RANK" in findings[code].message


def test_limit_rank_term_accepts_a_limit_or_a_row_number_and_binds_number_words():
    sc = _rank_scorer()
    ok = sc.explain(f"WITH n AS ({PER_NODE}) SELECT * FROM n ORDER BY active_n DESC LIMIT 5", "The five largest LVL_3 nodes by active headcount")
    assert next(o for o in ok.signals["terms"]["outcomes"] if o["term"] == "top_n")["status"] == "satisfied"
    rn = sc.explain(f"WITH n AS ({PER_NODE}), r AS (SELECT n.*, ROW_NUMBER() OVER (ORDER BY active_n DESC) AS rn FROM n) SELECT * FROM r WHERE rn <= 5", "The five largest LVL_3 nodes by active headcount")
    assert next(o for o in rn.signals["terms"]["outcomes"] if o["term"] == "top_n")["status"] == "satisfied"
    wrong = sc.explain(f"WITH n AS ({PER_NODE}) SELECT * FROM n ORDER BY active_n DESC LIMIT 3", "The five largest LVL_3 nodes by active headcount")
    assert _term_findings(wrong)["term_rank"].severity == Severity.HIGH
    # a LIMIT bound by a parameter is judged through the call's parameters, else left unverified
    by_param = sc.explain(f"WITH n AS ({PER_NODE}) SELECT * FROM n ORDER BY active_n DESC LIMIT {{n}}", "The top 5 LVL_3 nodes by active headcount", params={"n": 5})
    assert next(o for o in by_param.signals["terms"]["outcomes"] if o["term"] == "top_n")["status"] == "satisfied"
    unknown = sc.explain(f"WITH n AS ({PER_NODE}) SELECT * FROM n ORDER BY active_n DESC LIMIT {{k}}", "The top 5 LVL_3 nodes by active headcount")
    assert next(o for o in unknown.signals["terms"]["outcomes"] if o["term"] == "top_n")["status"] == "unverified"
    assert "term_rank" not in _term_findings(unknown)


def test_rank_term_sql_is_validated():
    problems = Catalogue.from_sheets({"terms": TERMS + "top_n,top {n},rank,,ORDER BY x DESC,,\n"}).validate()
    assert any("rank is written as" in str(p) and "sql" in str(p) for p in problems), problems
