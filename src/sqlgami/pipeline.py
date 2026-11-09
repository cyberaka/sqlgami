"""The chain of checks, cheapest first, stopping as soon as the outcome is settled.

    parse -> statement -> schema -> static -> dry_run -> sample

Each stage is recorded in the report whether it ran, failed or was skipped, so
a reader can see exactly what the number rests on.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from . import analysis as A
from . import template
from . import types as T
from .catalog import Catalog, Table
from .checks import CheckContext, Probe, run_static_checks
from .checks.base import PROBE_HAS_NULL, PROBE_QUERY, PROBE_TIMESTAMP_FORMAT, PROBE_UNIQUE, PROBE_VALUE
from .checks.predicates import _ISO_DATE_RE, judge_value, value_finding
from .engines.base import INVALID, TIMEOUT, Adapter
from .errors import AdapterError, SqlgamiError
from .report import (
    CostEstimate,
    Finding,
    Report,
    Severity,
    SkippedCheck,
    StageResult,
    StageStatus,
)
from .question import adjust_findings, run_question_checks
from .scoring import Calibrator, score_report

logger = logging.getLogger("sqlgami")

MODES = ("static", "dry", "sample", "full")


@dataclass
class Options:
    sample_rows: int = 200
    timeout_s: float = 15.0
    cost_ceiling: float | None = 5_000_000  # in the engine's cost unit (rows for local engines)
    probe_row_ceiling: int = 2_000_000
    probe_timeout_s: float = 5.0
    max_probes: int = 24
    max_distinct_values: int = 200
    # With a question, low-cardinality text columns of the tables read are profiled (sample
    # mode only) so words in the question can be matched to stored values.
    profile_values: bool = True
    max_profile_columns: int = 40
    max_profile_values: int = 40


class _Clock:
    def __init__(self) -> None:
        self.started = time.perf_counter()

    def lap(self) -> float:
        now = time.perf_counter()
        elapsed = (now - self.started) * 1000
        self.started = now
        return elapsed


def run(
    sql: str,
    *,
    engine: str,
    dialect: str,
    mode: str,
    catalog: Catalog | None,
    adapter: Adapter | None,
    question: str | None = None,
    options: Options | None = None,
    calibrator: Calibrator | None = None,
    glossary: str | None = None,
    params: dict[str, Any] | None = None,
) -> Report:
    options = options or Options()
    report = Report(engine=engine, mode=mode, sql=sql, question=question)
    _run(report, sql, dialect, mode, catalog, adapter, question, glossary, options, params)
    scored = score_report(report, calibrator)
    report.confidence = scored.confidence
    report.verdict = scored.verdict
    return report


def _stage(report: Report, name: str, status: StageStatus, detail: str | None = None, ms: float | None = None) -> None:
    report.stages.append(StageResult(name, status, detail, ms))


def _not_run(report: Report, names: tuple[str, ...], why: str) -> None:
    for name in names:
        _stage(report, name, StageStatus.NOT_RUN, why)


def _run(
    report: Report,
    sql: str,
    dialect: str,
    mode: str,
    catalog: Catalog | None,
    adapter: Adapter | None,
    question: str | None,
    glossary: str | None,
    options: Options,
    params: dict[str, Any] | None = None,
) -> None:
    clock = _Clock()

    # ------------------------------------------------------------- parse
    prepared = template.prepare(sql, dialect, params)
    if prepared.params:
        report.signals["template_parameters"] = [p.to_dict() for p in prepared.params]
    if prepared.disabled_filters:
        report.signals["disabled_filters"] = list(prepared.disabled_filters)
    analysis = A.analyse(prepared.analysis_sql, catalog, dialect)
    if analysis.script_statements:
        report.signals["script"] = {
            "statements": list(analysis.script_statements),
            "variables": dict(analysis.variables),
        }
    elapsed = clock.lap()
    if analysis.parse_error is not None:
        report.findings.append(
            Finding("parse_error", Severity.ERROR, f"The statement does not parse: {analysis.parse_error}", "parse")
        )
        _stage(report, "parse", StageStatus.FAILED, analysis.parse_error, elapsed)
        _not_run(report, ("statement", "schema", "static", "dry_run", "sample", "question"), "the statement does not parse")
        return
    if analysis.kind == A.EMPTY:
        report.findings.append(Finding("empty_statement", Severity.ERROR, "There is no statement to check.", "parse"))
        _stage(report, "parse", StageStatus.FAILED, "empty input", elapsed)
        _not_run(report, ("statement", "schema", "static", "dry_run", "sample", "question"), "empty input")
        return
    _stage(report, "parse", StageStatus.PASSED, None, elapsed)

    # --------------------------------------------------------- statement
    problem: Finding | None = None
    if analysis.kind == A.MULTIPLE:
        problem = Finding(
            "multiple_statements",
            Severity.ERROR,
            f"There are {analysis.statement_count} statements; a question is answered by one query.",
            "statement",
        )
    elif analysis.kind == A.MUTATION:
        kinds = ", ".join(analysis.mutation_kinds)
        problem = Finding(
            "not_read_only",
            Severity.ERROR,
            f"The statement changes data or structure ({kinds}). Only read-only queries are scored, "
            "and it was not sent to the database.",
            "statement",
            evidence={"kinds": analysis.mutation_kinds},
        )
    elif analysis.kind != A.QUERY:
        problem = Finding(
            "not_a_query",
            Severity.ERROR,
            "The statement is not a query, so it cannot answer a question.",
            "statement",
        )
    elif analysis.unsafe_functions:
        names = ", ".join(analysis.unsafe_functions)
        problem = Finding(
            "external_access",
            Severity.ERROR,
            f"The query reaches outside the database or can stall it ({names}). It was not sent "
            "to the database.",
            "statement",
            evidence={"functions": analysis.unsafe_functions},
        )
    if problem is not None:
        report.findings.append(problem)
        _stage(report, "statement", StageStatus.FAILED, problem.message, clock.lap())
        _not_run(report, ("schema", "static", "dry_run", "sample", "question"), "the statement is not a plain read-only query")
        return
    _stage(report, "statement", StageStatus.PASSED, None, clock.lap())
    report.findings.extend(prepared.findings)
    for name in analysis.shadowed_variables:
        report.findings.append(
            Finding(
                "variable_shadowed",
                Severity.MEDIUM,
                f"The script declares a variable named {name}, but a table in the query has a column "
                f"of the same name, and the column wins. A condition such as {name} = {name} then "
                "compares the column with itself.",
                "statement",
                suggestion="Give the variable a name no column has.",
            )
        )

    # ------------------------------------------------------------ schema
    schema_passed = False
    schema_findings: list[Finding] = []
    if catalog is None:
        _stage(report, "schema", StageStatus.SKIPPED, "no schema or database connection was supplied")
    elif analysis.unknown_tables or analysis.unknown_columns or analysis.ambiguous_columns:
        for item in analysis.unknown_tables:
            hint = f" Did you mean {', '.join(item['suggestions'])}?" if item["suggestions"] else ""
            inside = f" (referenced inside view {item['inside_view']})" if item.get("inside_view") else ""
            schema_findings.append(
                Finding(
                    "unknown_table",
                    Severity.ERROR,
                    f"Table {item['name']} does not exist{inside}.{hint}",
                    "schema",
                    evidence=item,
                )
            )
        for item in analysis.unknown_columns:
            hint = f" Did you mean {', '.join(item['suggestions'])}?" if item["suggestions"] else ""
            schema_findings.append(
                Finding(
                    "unknown_column",
                    Severity.ERROR,
                    f"Column {item['column']} cannot be resolved: {item['reason']}.{hint}",
                    "schema",
                    evidence=item,
                )
            )
        for item in analysis.ambiguous_columns:
            schema_findings.append(
                Finding(
                    "ambiguous_column",
                    Severity.ERROR,
                    f"Column {item['column']} exists in more than one table ({', '.join(item['sources'])}) "
                    "and is written without a table name.",
                    "schema",
                    evidence=item,
                )
            )
        report.findings.extend(schema_findings)
        _stage(report, "schema", StageStatus.FAILED, schema_findings[0].message, clock.lap())
    elif analysis.resolved is None:
        detail = analysis.resolve_error or "the statement could not be analysed"
        report.findings.append(
            Finding(
                "analysis_incomplete",
                Severity.LOW,
                f"sqlgami could not analyse this statement's structure ({detail}), so the static "
                "checks did not run.",
                "schema",
            )
        )
        _stage(report, "schema", StageStatus.SKIPPED, detail, clock.lap())
    else:
        schema_passed = True
        _stage(report, "schema", StageStatus.PASSED, None, clock.lap())
        report.tables = sorted(t.qualified_name for t in analysis.tables.values())
        if analysis.views_used:
            report.signals["views_expanded"] = list(analysis.views_used)

    # ------------------------------------------------------------ static
    ctx: CheckContext | None = None
    if schema_passed and catalog is not None:
        ctx = CheckContext(analysis, catalog, question)
        findings, skipped = run_static_checks(ctx)
        report.findings.extend(findings)
        report.checks_not_run.extend(skipped)
        for name in dict.fromkeys(analysis.string_identifiers):
            report.findings.append(
                Finding(
                    "double_quoted_string",
                    Severity.LOW,
                    f'"{name}" is in double quotes, which mean a column name. No such column exists, '
                    "so SQLite falls back to reading it as text. Other engines reject it.",
                    "static",
                    suggestion=f"Write '{name}' with single quotes.",
                )
            )
        _stage(report, "static", StageStatus.PASSED, f"{len(findings)} finding(s)", clock.lap())
    else:
        _stage(report, "static", StageStatus.NOT_RUN, "needs a resolved schema")

    blocked: str | None = None
    if prepared.executable_sql is None:
        missing = ", ".join(p.name for p in prepared.params if not p.supplied)
        blocked = f"the SQL is a template and no value was given for: {missing}"
    elif analysis.script_statements and not getattr(adapter, "supports_scripts", False):
        blocked = "the SQL is a multi-statement script, which is not sent to this engine"
    if blocked is not None and adapter is not None and mode != "static":
        _stage(report, "dry_run", StageStatus.SKIPPED, blocked)
        _stage(report, "sample", StageStatus.SKIPPED, blocked + "; unverified beyond static checks")
    else:
        _database_stages(
            report, prepared.executable_sql or sql, mode, adapter, analysis, ctx, schema_findings, options, clock
        )
    _question_stage(report, ctx, adapter, question, glossary, mode, options)


def _question_stage(
    report: Report,
    ctx: CheckContext | None,
    adapter: Adapter | None,
    question: str | None,
    glossary: str | None,
    mode: str,
    options: Options,
) -> None:
    if not question or not question.strip():
        _stage(report, "question", StageStatus.NOT_RUN, "no question was supplied")
        return
    if ctx is None:
        _stage(report, "question", StageStatus.NOT_RUN, "needs a resolved schema")
        return
    if any(f.severity == Severity.ERROR for f in report.findings):
        _stage(report, "question", StageStatus.NOT_RUN, "the statement cannot run")
        return
    clock = _Clock()
    sample = report.stage("sample")
    ran = sample is not None and sample.status in (StageStatus.PASSED, StageStatus.SKIPPED)
    if adapter is not None and mode in ("sample", "full") and ran and options.profile_values:
        _profile_values(report, ctx, adapter, options)
    adjust_findings(report.findings, question)
    findings = run_question_checks(ctx, question, glossary)
    report.findings.extend(findings)
    _stage(report, "question", StageStatus.PASSED, f"{len(findings)} finding(s)", clock.lap())


def _profile_values(report: Report, ctx: CheckContext, adapter: Adapter, options: Options) -> None:
    """List the distinct values of low-cardinality text columns in the tables the query reads."""
    budget = options.max_profile_columns
    limit = options.max_profile_values
    for table in ctx.analysis.tables.values():
        if table.is_view or not _probe_allowed(adapter, table, options):
            continue
        for column in table.columns:
            if column.values is not None or column.high_cardinality:
                continue
            family = T.family_of_declared(column.type, ctx.dialect)
            if family not in (T.TEXT, T.UNKNOWN, T.OTHER):
                continue
            if budget <= 0:
                return
            budget -= 1
            try:
                rows = adapter.query(
                    exp.select(_column(column.name)).distinct().from_(adapter.table_ref(table)).limit(limit + 1),
                    max_rows=limit + 1,
                    timeout_s=options.probe_timeout_s,
                )
            except AdapterError as exc:
                logger.debug("value profile failed: %s", exc)
                column.high_cardinality = True
                continue
            if len(rows) <= limit:
                column.values = tuple(row[0] for row in rows)
            else:
                column.high_cardinality = True


def _database_stages(
    report: Report,
    sql: str,
    mode: str,
    adapter: Adapter | None,
    analysis: A.Analysis,
    ctx: CheckContext | None,
    schema_findings: list[Finding],
    options: Options,
    clock: _Clock,
) -> None:
    wants_dry = mode in ("dry", "sample", "full")
    wants_sample = mode in ("sample", "full")
    if not wants_dry:
        _not_run(report, ("dry_run", "sample"), f"mode is {mode}")
        return
    if adapter is None:
        _not_run(report, ("dry_run", "sample"), "no database connection was supplied")
        return

    try:
        engine_version = adapter.version()
    except Exception:
        engine_version = None
    if engine_version:
        report.signals["engine_version"] = engine_version
    dry = adapter.dry_run(sql)
    elapsed = clock.lap()
    report.plan = dry.plan
    report.findings.extend(dry.findings)
    if not dry.ok:
        if dry.error_kind == INVALID:
            report.findings.append(
                Finding(
                    "dry_run_error",
                    Severity.ERROR,
                    f"The database rejects the statement: {dry.error}",
                    "dry_run",
                )
            )
            _stage(report, "dry_run", StageStatus.FAILED, dry.error, elapsed)
            _not_run(report, ("sample",), "the database rejects the statement")
            return
        report.findings.append(
            Finding(
                "dry_run_unavailable",
                Severity.LOW,
                f"The database could not plan the statement for a reason unrelated to the SQL: {dry.error}",
                "dry_run",
            )
        )
        _stage(report, "dry_run", StageStatus.SKIPPED, dry.error, elapsed)
        _not_run(report, ("sample",), "the dry run did not complete")
        return
    _stage(report, "dry_run", StageStatus.PASSED, None, elapsed)

    if schema_findings:
        # The database is the authority on what exists. If it accepts the statement,
        # sqlgami's view of the schema was wrong or stale.
        for finding in schema_findings:
            finding.severity = Severity.INFO
            finding.message += " The database accepted the statement, so this is not treated as an error."
        schema_stage = report.stage("schema")
        if schema_stage is not None:
            schema_stage.status = StageStatus.SKIPPED
            schema_stage.detail = "schema information disagreed with the database; static checks did not run"

    cost = dry.cost or _row_cost(adapter, analysis)
    report.cost = cost
    can_run = dry.can_execute_safely
    if can_run is None:
        ceiling = options.cost_ceiling
        can_run = cost.value is None or ceiling is None or cost.value <= ceiling

    # ------------------------------------------------------------ sample
    if not wants_sample:
        _not_run(report, ("sample",), f"mode is {mode}")
        return
    if not can_run:
        _stage(
            report,
            "sample",
            StageStatus.SKIPPED,
            f"estimated cost {cost.value:g} {cost.unit} is above the ceiling of {options.cost_ceiling:g}; "
            "unverified beyond static and dry-run checks",
        )
    else:
        result = adapter.execute(sql, max_rows=options.sample_rows, timeout_s=options.timeout_s)
        elapsed = clock.lap()
        if not result.ok:
            if result.error_kind == INVALID:
                report.findings.append(
                    Finding(
                        "execution_error",
                        Severity.ERROR,
                        f"The query fails when run: {result.error}",
                        "sample",
                    )
                )
                _stage(report, "sample", StageStatus.FAILED, result.error, elapsed)
                return
            code = "execution_timeout" if result.error_kind == TIMEOUT else "execution_unavailable"
            report.findings.append(
                Finding(
                    code,
                    Severity.LOW,
                    f"The query could not be run to completion ({result.error}); unverified beyond "
                    "static and dry-run checks.",
                    "sample",
                )
            )
            _stage(report, "sample", StageStatus.SKIPPED, result.error, elapsed)
        else:
            _result_signals(report, result, ctx)
            for finding in report.findings:
                if finding.code == "scalar_subquery_rows" and not finding.evidence.get("lenient"):
                    finding.severity = Severity.LOW
                    finding.evidence["data_check"] = "ran"
                    finding.message += " The query ran, so it returned at most one row on today's data."
            detail = f"{len(result.rows)} row(s)" + (" (more available)" if result.truncated else "")
            _stage(report, "sample", StageStatus.PASSED, detail, elapsed)

    if ctx is not None and ctx.probes:
        _run_probes(report, ctx, adapter, options)


# ---------------------------------------------------------------------- cost


def _row_cost(adapter: Adapter, analysis: A.Analysis) -> CostEstimate:
    """Rows in the base tables the query reads: an upper bound on what a scan touches."""
    total = 0
    per_table: dict[str, int | None] = {}
    known = True
    for table in analysis.tables.values():
        try:
            count = adapter.row_count(table)
        except SqlgamiError:
            count = None
        per_table[table.qualified_name] = count
        if count is None:
            known = False
        else:
            total += count
    if not analysis.tables:
        return CostEstimate(unit=adapter.cost_unit, value=0, detail={"tables": {}})
    return CostEstimate(
        unit=adapter.cost_unit,
        value=float(total) if known else None,
        detail={"tables": per_table, "basis": "rows in the tables read"},
    )


# ------------------------------------------------------------------- results


def _result_signals(report: Report, result: Any, ctx: CheckContext | None) -> None:
    rows = result.rows
    report.signals["rows_returned"] = len(rows)
    try:
        report.signals["duplicate_rows_in_sample"] = len(rows) - len(set(rows))
    except TypeError:
        pass  # unhashable values (arrays, structs)
    report.signals["rows_truncated"] = result.truncated
    report.signals["columns"] = list(result.columns)

    single_row_aggregate = False
    if ctx is not None and ctx.analysis.root_scope is not None:
        root = ctx.analysis.root_scope
        if isinstance(root.expression, exp.Select):
            model = ctx.cache.model(root)
            single_row_aggregate = model.group is None and bool(model.own_aggregates())

    if not rows:
        report.findings.append(
            Finding(
                "empty_result",
                Severity.MEDIUM,
                "The query runs but returns no rows. A filter or join may be stricter than the "
                "question intends.",
                "sample",
            )
        )
        return
    if len(rows) == 1 and all(value is None for value in rows[0]):
        report.findings.append(
            Finding(
                "null_result",
                Severity.MEDIUM,
                "The query returns a single row of NULLs, which usually means no rows matched "
                "the filters.",
                "sample",
            )
        )
        return
    if single_row_aggregate and len(rows) == 1 and len(rows[0]) == 1 and rows[0][0] == 0:
        report.findings.append(
            Finding(
                "zero_result",
                Severity.LOW,
                "The aggregate comes out as 0, which may mean no rows matched the filters.",
                "sample",
            )
        )
    if len(rows) >= 5:
        empty = [
            result.columns[i]
            for i in range(len(result.columns))
            if all(row[i] is None for row in rows)
        ]
        if empty:
            report.findings.append(
                Finding(
                    "all_null_column",
                    Severity.LOW,
                    f"Column(s) {', '.join(map(str, empty))} are NULL in every sampled row.",
                    "sample",
                    evidence={"columns": empty},
                )
            )


# -------------------------------------------------------------------- probes


def _column(name: str) -> exp.Column:
    return exp.column(exp.to_identifier(name, quoted=True))


def _probe_allowed(adapter: Adapter, table: Table, options: Options) -> bool:
    try:
        count = adapter.row_count(table)
    except SqlgamiError:
        count = None
    return count is None or count <= options.probe_row_ceiling


def _run_probes(report: Report, ctx: CheckContext, adapter: Adapter, options: Options) -> None:
    """Check findings against the data: confirm them, dismiss them, or add new ones."""
    budget = options.max_probes
    unique_results: dict[int, list[bool | None]] = {}
    unique_findings: dict[int, Finding] = {}
    done: set[tuple] = set()
    skipped_for_size: set[str] = set()

    for probe in ctx.probes:
        key = (probe.kind, probe.table.qualified_name, probe.columns, probe.literal, probe.operator, id(probe.finding))
        if key in done:
            continue
        done.add(key)
        if probe.kind == PROBE_UNIQUE and probe.finding is not None:
            unique_results.setdefault(id(probe.finding), [])
            unique_findings[id(probe.finding)] = probe.finding
        if budget <= 0:
            report.checks_not_run.append(SkippedCheck(f"probe:{probe.kind}", "probe budget for this query is used up"))
            if probe.kind == PROBE_UNIQUE and probe.finding is not None:
                unique_results[id(probe.finding)].append(None)
            continue
        if not _probe_allowed(adapter, probe.table, options):
            skipped_for_size.add(probe.table.qualified_name)
            if probe.kind == PROBE_UNIQUE and probe.finding is not None:
                unique_results[id(probe.finding)].append(None)
            continue
        budget -= 1
        try:
            if probe.kind == PROBE_UNIQUE:
                outcome = _probe_unique(adapter, probe, options)
                if probe.finding is not None:
                    unique_results[id(probe.finding)].append(outcome)
            elif probe.kind == PROBE_HAS_NULL:
                _probe_has_null(adapter, probe, options)
            elif probe.kind == PROBE_VALUE:
                _probe_value(report, ctx, adapter, probe, options)
            elif probe.kind == PROBE_TIMESTAMP_FORMAT:
                _probe_timestamp_format(report, adapter, probe, options)
            elif probe.kind == PROBE_QUERY:
                _probe_query(adapter, probe, options)
        except AdapterError as exc:
            logger.debug("probe failed: %s", exc)
            report.checks_not_run.append(SkippedCheck(f"probe:{probe.kind}", str(exc)))
            if probe.kind == PROBE_UNIQUE and probe.finding is not None:
                unique_results[id(probe.finding)].append(None)

    for table in sorted(skipped_for_size):
        report.checks_not_run.append(
            SkippedCheck("probe", f"{table} is too large to probe within the row ceiling")
        )

    for finding_id, outcomes in unique_results.items():
        finding = unique_findings[finding_id]
        if outcomes and all(outcome is True for outcome in outcomes):
            finding.severity = Severity.INFO
            finding.evidence["data_check"] = "unique"
            finding.evidence["certainty"] = "dismissed"
            finding.message += (
                " Checked against the data: the join columns hold no repeated values at present, "
                "so nothing is double-counted today. No constraint enforces that."
            )
        elif any(outcome is False for outcome in outcomes):
            finding.evidence["data_check"] = "duplicates"
            if finding.evidence.get("certainty") == "maybe":
                finding.evidence["certainty"] = "yes"
                finding.severity = Severity.HIGH
            finding.message += " Checked against the data: the join columns do hold repeated values."


def _probe_unique(adapter: Adapter, probe: Probe, options: Options) -> bool:
    """True when the columns hold no repeated (non-NULL) combination."""
    columns = [_column(c) for c in probe.columns]
    query = (
        exp.select(exp.Literal.number(1))
        .from_(adapter.table_ref(probe.table))
        .group_by(*columns)
        .having("COUNT(*) > 1")
        .limit(1)
    )
    for column in columns:
        query = query.where(exp.Not(this=exp.Is(this=column.copy(), expression=exp.Null())))
    return not adapter.query(query, max_rows=1, timeout_s=options.probe_timeout_s)


def _probe_has_null(adapter: Adapter, probe: Probe, options: Options) -> None:
    column = _column(probe.columns[0])
    query = (
        exp.select(exp.Literal.number(1))
        .from_(adapter.table_ref(probe.table))
        .where(exp.Is(this=column, expression=exp.Null()))
        .limit(1)
    )
    has_null = bool(adapter.query(query, max_rows=1, timeout_s=options.probe_timeout_s))
    finding = probe.finding
    if finding is None:
        return
    if has_null:
        finding.severity = probe.confirm
        finding.evidence["data_check"] = "has_null"
        finding.message += " Checked against the data: the column does hold NULLs, so this returns no rows."
    else:
        finding.severity = probe.dismiss
        finding.evidence["data_check"] = "no_null"
        finding.message += " Checked against the data: the column holds no NULLs at present."


def _probe_query(adapter: Adapter, probe: Probe, options: Options) -> None:
    """Run a query a check prepared, and adjust its finding by whether a row comes back."""
    if probe.query is None or probe.finding is None:
        return
    rows = adapter.query(probe.query, max_rows=1, timeout_s=options.probe_timeout_s)
    outcome = probe.on_rows if rows else probe.on_empty
    if outcome is None:
        return
    severity, text, tag = outcome
    if severity is not None:
        probe.finding.severity = severity
    probe.finding.message += text
    probe.finding.evidence["data_check"] = tag


def _probe_value(report: Report, ctx: CheckContext, adapter: Adapter, probe: Probe, options: Options) -> None:
    table = probe.table
    column = ctx.catalog.column(table, probe.columns[0], quoted=True)
    if column is None:
        return
    literal = str(probe.literal)
    negated = probe.operator == "<>"
    ref = adapter.table_ref(table)
    if column.values is None and not column.high_cardinality:
        limit = options.max_distinct_values
        rows = adapter.query(
            exp.select(_column(column.name)).distinct().from_(ref).limit(limit + 1),
            max_rows=limit + 1,
            timeout_s=options.probe_timeout_s,
        )
        if len(rows) <= limit:
            column.values = tuple(row[0] for row in rows)  # remembered for later queries
        else:
            column.high_cardinality = True
    if column.values is not None:
        present, near = judge_value(column.values, literal)
    else:
        exists = adapter.query(
            exp.select(exp.Literal.number(1))
            .from_(ref)
            .where(exp.EQ(this=_column(column.name), expression=exp.Literal.string(literal)))
            .limit(1),
            max_rows=1,
            timeout_s=options.probe_timeout_s,
        )
        present = bool(exists)
        near = []
        if not present:
            folded = exp.Lower(this=exp.Trim(this=exp.cast(_column(column.name), "TEXT")))
            rows = adapter.query(
                exp.select(_column(column.name))
                .distinct()
                .from_(ref)
                .where(exp.EQ(this=folded, expression=exp.Literal.string(literal.strip().lower())))
                .limit(3),
                max_rows=3,
                timeout_s=options.probe_timeout_s,
            )
            near = [str(row[0]) for row in rows]
    if probe.finding is not None:
        # The check already reported; the data only confirms or weakens it.
        if present:
            probe.finding.severity = Severity.LOW
            probe.finding.evidence["data_check"] = "value_present"
            probe.finding.message += " Checked against the data: the column does hold this value in some rows."
        else:
            probe.finding.severity = Severity.HIGH
            probe.finding.evidence["data_check"] = "value_absent"
            probe.finding.message += " Checked against the data: no row holds this value, so the test matches nothing."
        return
    if not present:
        report.findings.append(value_finding(probe.snippet, table, column, literal, near, negated, "sample"))


_TEXT_DATE_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _probe_timestamp_format(report: Report, adapter: Adapter, probe: Probe, options: Options) -> None:
    """SQLite stores dates as text or numbers, so a literal must match the stored layout exactly."""
    column = _column(probe.columns[0])
    rows = adapter.query(
        exp.select(column)
        .from_(adapter.table_ref(probe.table))
        .where(exp.Not(this=exp.Is(this=column.copy(), expression=exp.Null())))
        .limit(1),
        max_rows=1,
        timeout_s=options.probe_timeout_s,
    )
    if not rows:
        return
    stored = rows[0][0]
    literal = str(probe.literal)
    where = f"{probe.table.name}.{probe.columns[0]}"
    problem: str | None = None
    if isinstance(stored, (int, float)):
        problem = (
            f"{where} stores numbers (for example {stored!r}), but the query compares it with the "
            f"date text {literal!r}. SQLite sorts every number before any text, so the comparison "
            "does not do what it says."
        )
    elif isinstance(stored, str):
        if not _TEXT_DATE_SHAPE.match(stored):
            problem = (
                f"{where} stores dates as text in a layout that does not sort by date (for example "
                f"{stored!r}), so comparing it with {literal!r} gives arbitrary results."
            )
        elif len(stored) > 10 and len(literal) > 10 and stored[10] != literal[10]:
            problem = (
                f"{where} stores timestamps as text like {stored!r}, but the query writes "
                f"{literal!r}. The separator after the date differs ({stored[10]!r} against "
                f"{literal[10]!r}), and SQLite compares text character by character, so rows on "
                "the boundary day fall on the wrong side."
            )
        elif len(stored) == 10 and len(literal) > 10 and _ISO_DATE_RE.match(literal):
            problem = (
                f"{where} stores plain dates like {stored!r}, but the query compares it with the "
                f"timestamp {literal!r}; as text the two never compare equal on the same day."
            )
    if problem is None:
        return
    key = ("timestamp_format", where)
    if any((f.code, f.evidence.get("column")) == key for f in report.findings):
        return
    report.findings.append(
        Finding(
            "timestamp_format",
            Severity.HIGH,
            problem,
            "sample",
            suggestion="Write the literal in exactly the layout the column stores.",
            evidence={"column": where, "stored_example": stored, "literal": literal},
        )
    )
