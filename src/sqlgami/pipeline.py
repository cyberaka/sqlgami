"""The chain of checks, cheapest first, stopping as soon as the outcome is settled.

    parse -> statement -> schema -> static -> dry_run -> sample

Each stage is recorded in the report whether it ran, failed or was skipped, so
a reader can see exactly what the number rests on.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlglot import exp

from . import analysis as A
from . import consistency, reconcile
from . import template
from . import types as T
from .catalog import Catalog, Table
from .checks import CheckContext, Probe, run_static_checks
from .checks.base import PROBE_HAS_NULL, PROBE_QUERY, PROBE_TIMESTAMP_FORMAT, PROBE_UNIQUE, PROBE_VALUE
from .checks.predicates import _ISO_DATE_RE, judge_value, value_finding
from .engines.base import INVALID, TIMEOUT, Adapter, DryRunResult, ExecutionResult
from .errors import AdapterError, SqlgamiError, UnsafeQueryError
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

# A uniqueness probe over fewer rows than this says nothing about production data.
SMALL_TABLE_ROWS = 50


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
    # Consistency stage: how many alternatives to ask a generator for, how many to run at most,
    # and the largest result that is compared.
    # One sample per source by default: in our tests extra samples of the same model lowered
    # precision, while a second model raised it.
    consistency_k: int = 1
    consistency_max_alternatives: int = 8
    consistency_rows: int = 5000
    # "confirm": alternatives that agree support the candidate; disagreement is a doubt.
    # "judge": two or more independent sources agreeing against the candidate is strong
    # evidence against it. Only for alternatives at least as capable as the candidate's author.
    consistency_role: str = "confirm"
    # Reconciliation stage: anchor queries with a declared relation to the result.
    reconcile_max_probes: int = 12
    reconcile_budget_s: float = 30.0
    # The most time the whole consistency stage may take, however many alternatives there are.
    consistency_budget_s: float = 30.0
    # True when the question asks for rows in a particular order: an alternative that returns
    # the same rows in another order then counts as a different answer.
    consistency_ordered: bool = False
    # Functions the host vouches for (its own user-defined functions, say). A statement that
    # calls anything outside the engine's built-in allow-list and this list is not sent to
    # the database.
    allowed_functions: tuple[str, ...] = ()
    # System relations (catalogues, settings, sessions) a statement may read. None by default.
    allowed_tables: tuple[str, ...] = ()
    # How reliable each alternative-writing source is, as its share of right answers measured on
    # a labelled set (0..1), keyed by source name or a distinctive part of it. Weighs the
    # consistency stage: agreement from weak sources earns little, two reliable sources agreeing
    # against the candidate is a verdict.
    source_weights: dict[str, float] = field(default_factory=dict)
    # Wall-clock bound on everything one report sends to the database, all stages together.
    # Statements that would overrun it are cut short or not sent; the report says so.
    budget_s: float = 120.0


class _Clock:
    def __init__(self) -> None:
        self.started = time.perf_counter()

    def lap(self) -> float:
        now = time.perf_counter()
        elapsed = (now - self.started) * 1000
        self.started = now
        return elapsed


BUDGET_SPENT = "the time budget for this report is spent"


class _Bounded:
    """An adapter under one wall-clock budget for the whole report.

    Every statement sqlgami sends (dry run, sample, probes, alternatives, anchors) gets at most
    the time left in the budget; once it is spent, statements are not sent at all and the
    callers see a timeout. The engine adapters enforce their own per-statement bound, so the two
    together keep a ``score()`` call from outliving ``Options.budget_s`` by more than one grace
    period.
    """

    def __init__(self, adapter: Adapter, budget_s: float) -> None:
        self._adapter = adapter
        self._deadline = time.monotonic() + budget_s
        self.spent = False  # set once a statement was refused or cut short for lack of time

    def remaining(self) -> float:
        return self._deadline - time.monotonic()

    def _clamp(self, timeout_s: float) -> float | None:
        left = self.remaining()
        if left <= 0.05:
            self.spent = True
            return None
        if left < timeout_s:
            self.spent = True
            return left
        return timeout_s

    def dry_run(self, sql: str) -> DryRunResult:
        if self._clamp(10.0) is None:
            return DryRunResult(ok=False, error=BUDGET_SPENT, error_kind=TIMEOUT)
        return self._adapter.dry_run(sql)

    def execute(self, sql: str, *, max_rows: int, timeout_s: float) -> ExecutionResult:
        limit = self._clamp(timeout_s)
        if limit is None:
            return ExecutionResult(ok=False, error=BUDGET_SPENT, error_kind=TIMEOUT)
        return self._adapter.execute(sql, max_rows=max_rows, timeout_s=limit)

    def query(self, expression: exp.Expression, *, max_rows: int = 1000, timeout_s: float = 10.0) -> list[tuple]:
        limit = self._clamp(timeout_s)
        if limit is None:
            raise AdapterError(BUDGET_SPENT)
        return self._adapter.query(expression, max_rows=max_rows, timeout_s=limit)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)


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
    alternatives: Sequence[str] | None = None,
    generator: Any = None,
    probes: Sequence[Any] | None = None,
    prober: Any = None,
) -> Report:
    options = options or Options()
    report = Report(engine=engine, mode=mode, sql=sql, question=question)
    _run(report, sql, dialect, mode, catalog, adapter, question, glossary, options, params, alternatives, generator, probes, prober)
    report.findings = _dedupe(report.findings)
    scored = score_report(report, calibrator)
    report.confidence = scored.confidence
    report.verdict = scored.verdict
    return report


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """One risk, one finding: the same check on the same text is reported once, with a count."""
    kept: dict[tuple[str, str, str], Finding] = {}
    for finding in findings:
        if finding.snippet is None:
            key = (finding.code, "", finding.message)
        else:
            key = (finding.code, " ".join(finding.snippet.split()).lower(), "")
        first = kept.get(key)
        if first is None:
            kept[key] = finding
            continue
        first.evidence["occurrences"] = first.evidence.get("occurrences", 1) + 1
        if finding.severity.rank > first.severity.rank:
            first.severity = finding.severity
    return list(kept.values())


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
    alternatives: Sequence[str] | None = None,
    generator: Any = None,
    probes: Sequence[Any] | None = None,
    prober: Any = None,
) -> None:
    clock = _Clock()
    bounded: _Bounded | None = _Bounded(adapter, options.budget_s) if adapter is not None else None
    adapter = bounded  # type: ignore[assignment]  # every stage below talks to the database through the budget

    # ------------------------------------------------------------- parse
    prepared = template.prepare(sql, dialect, params)
    if prepared.params:
        report.signals["template_parameters"] = [p.to_dict() for p in prepared.params]
    if prepared.disabled_filters:
        report.signals["disabled_filters"] = list(prepared.disabled_filters)
    analysis = A.analyse(prepared.analysis_sql, catalog, dialect, options.allowed_functions, options.allowed_tables)
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
        _not_run(report, ("statement", "schema", "static", "dry_run", "sample", "consistency", "reconciliation", "question"), "the statement does not parse")
        return
    if analysis.kind == A.EMPTY:
        report.findings.append(Finding("empty_statement", Severity.ERROR, "There is no statement to check.", "parse"))
        _stage(report, "parse", StageStatus.FAILED, "empty input", elapsed)
        _not_run(report, ("statement", "schema", "static", "dry_run", "sample", "consistency", "reconciliation", "question"), "empty input")
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
            f"The query reaches outside the tables it should read, or can stall the database "
            f"({names}). It was not sent to the database.",
            "statement",
            evidence={"functions": analysis.unsafe_functions},
        )
    if problem is not None:
        report.findings.append(problem)
        _stage(report, "statement", StageStatus.FAILED, problem.message, clock.lap())
        _not_run(report, ("schema", "static", "dry_run", "sample", "consistency", "reconciliation", "question"), "the statement is not a plain read-only query")
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
        ctx = CheckContext(analysis, catalog, question, params=params)
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
    if analysis.unknown_tables and catalog is not None:
        # The catalogue is the allow-list of what a query may read: a table outside it is not
        # sent, whatever the database would say about it.
        names = ", ".join(dict.fromkeys(item["name"] for item in analysis.unknown_tables))
        blocked = f"the query reads {names}, which the catalogue does not list"
    elif prepared.executable_sql is None:
        missing = ", ".join(p.name for p in prepared.params if not p.supplied)
        blocked = f"the SQL is a template and no value was given for: {missing}"
    elif analysis.script_statements and not getattr(adapter, "supports_scripts", False):
        blocked = "the SQL is a multi-statement script, which is not sent to this engine"
    elif analysis.unlisted_functions and adapter is not None and mode != "static":
        # Not on the allow-list. Ask the catalogue (never the statement itself) whether the
        # function exists at all: a made-up name is an error, a real one is merely unvouched for.
        missing = []
        for name in analysis.unlisted_functions:
            if "." not in name and adapter.function_exists(name) is False:
                missing.append(name)
        if missing:
            names = ", ".join(missing)
            report.findings.append(
                Finding(
                    "unknown_function",
                    Severity.ERROR,
                    f"Function {names} does not exist in this database, so the query cannot run.",
                    "dry_run",
                    evidence={"functions": missing},
                )
            )
            _stage(report, "dry_run", StageStatus.FAILED, f"function {names} does not exist")
            _stage(report, "sample", StageStatus.NOT_RUN, "the query cannot run")
            _not_run(report, ("consistency", "reconciliation", "question"), "the query cannot run")
            return
        names = ", ".join(analysis.unlisted_functions)
        blocked = f"the query calls a function sqlgami does not know to be read-only ({names})"
        report.findings.append(
            Finding(
                "unlisted_function",
                Severity.LOW,
                f"The query calls {names}, which is not one of this engine's built-in functions that "
                "sqlgami knows to be read-only. It was not sent to the database, so it is unverified "
                "beyond the static checks.",
                "statement",
                suggestion="If the function only reads data, name it in allowed_functions.",
                evidence={"functions": list(analysis.unlisted_functions)},
            )
        )
    if blocked is not None and adapter is not None and mode != "static":
        _stage(report, "dry_run", StageStatus.SKIPPED, blocked)
        _stage(report, "sample", StageStatus.SKIPPED, blocked + "; unverified beyond static checks")
    else:
        _database_stages(
            report, prepared.executable_sql or sql, mode, adapter, analysis, ctx, schema_findings, options, clock
        )
    _consistency_stage(
        report,
        prepared.executable_sql or sql,
        dialect,
        mode,
        catalog,
        adapter,
        question,
        glossary,
        options,
        alternatives,
        generator,
        params,
    )
    _reconcile_stage(report, prepared.executable_sql or sql, dialect, mode, catalog, adapter, question, glossary, options, probes, prober, params)
    _question_stage(report, ctx, adapter, question, glossary, mode, options)
    if bounded is not None and bounded.spent:
        report.findings.append(
            Finding(
                "budget_spent",
                Severity.LOW,
                f"Checking stopped at the time budget of {options.budget_s:g}s: some statements were "
                "cut short or not sent, so the result is verified less than it could be.",
                "sample",
                evidence={"budget_s": options.budget_s},
            )
        )


def _consistency_stage(
    report: Report,
    sql: str,
    dialect: str,
    mode: str,
    catalog: Catalog | None,
    adapter: Adapter | None,
    question: str | None,
    glossary: str | None,
    options: Options,
    alternatives: Sequence[str] | None,
    generator: Any,
    params: dict[str, Any] | None,
) -> None:
    """Compare the candidate's result with independently written queries for the same question."""
    name = "consistency"
    if alternatives is None and (generator is None or mode != "full"):
        why = "no alternative queries were supplied" if mode == "full" else f"mode is {mode}"
        _stage(report, name, StageStatus.NOT_RUN, why)
        return
    sample = report.stage("sample")
    if adapter is None or sample is None or sample.status != StageStatus.PASSED:
        _stage(report, name, StageStatus.NOT_RUN, "the query itself was not run")
        return
    clock = _Clock()
    if alternatives is None:
        if not question or not question.strip():
            _stage(report, name, StageStatus.NOT_RUN, "a generator needs the question")
            return
        try:
            schema_text = consistency.describe_schema(catalog, report.tables) if catalog is not None else ""
            alternatives = list(generator(question, schema_text, glossary, options.consistency_k) or [])
        except Exception as exc:  # the generator is the host's code; its failure is not ours to raise
            logger.debug("generator failed", exc_info=True)
            _stage(report, name, StageStatus.SKIPPED, f"the generator failed ({type(exc).__name__})", clock.lap())
            return
    rendered: list[Any] = []
    for item in alternatives:
        alt, source = consistency.split_alternative(item)
        if alt is None:
            continue
        if params:
            alt = template.prepare(alt, dialect, params).executable_sql or alt
        rendered.append((alt, source) if source is not None else alt)
    outcome = consistency.run(
        sql,
        rendered,
        dialect=dialect,
        catalog=catalog,
        adapter=adapter,
        max_rows=options.consistency_rows,
        timeout_s=options.timeout_s,
        cost_ceiling=options.cost_ceiling,
        max_alternatives=options.consistency_max_alternatives,
        role=options.consistency_role,
        budget_s=options.consistency_budget_s,
        ordered=options.consistency_ordered,
        allowed_functions=options.allowed_functions,
        allowed_tables=options.allowed_tables,
        weights=options.source_weights or None,
    )
    report.signals["consistency"] = outcome.to_dict()
    if not outcome.ran:
        _stage(report, name, StageStatus.SKIPPED, outcome.reason, clock.lap())
        return
    report.findings.extend(outcome.findings)
    _stage(
        report,
        name,
        StageStatus.PASSED,
        f"{outcome.agreeing} of {outcome.comparable} alternative(s) return the same result",
        clock.lap(),
    )


def _reconcile_stage(
    report: Report,
    sql: str,
    dialect: str,
    mode: str,
    catalog: Catalog | None,
    adapter: Adapter | None,
    question: str | None,
    glossary: str | None,
    options: Options,
    probes: Sequence[Any] | None,
    prober: Any,
    params: dict[str, Any] | None,
) -> None:
    """Check the result against simple anchor queries that carry a declared relation to it."""
    name = "reconciliation"
    if probes is None and (prober is None or mode != "full"):
        why = "no anchor queries were supplied" if mode == "full" else f"mode is {mode}"
        _stage(report, name, StageStatus.NOT_RUN, why)
        return
    sample = report.stage("sample")
    if adapter is None or sample is None or sample.status != StageStatus.PASSED:
        _stage(report, name, StageStatus.NOT_RUN, "the query itself was not run")
        return
    clock = _Clock()
    base = adapter.execute(sql, max_rows=options.consistency_rows, timeout_s=options.timeout_s)
    if not base.ok or base.truncated:
        _stage(report, name, StageStatus.SKIPPED, "the result could not be fetched whole for comparison", clock.lap())
        return
    if probes is None:
        if not question or not question.strip():
            _stage(report, name, StageStatus.NOT_RUN, "a prober needs the question")
            return
        try:
            schema_text = consistency.describe_schema(catalog, report.tables) if catalog is not None else ""
            probes = list(prober(question, schema_text, glossary, list(base.columns)) or [])
        except Exception as exc:  # the prober is the host's code
            logger.debug("prober failed", exc_info=True)
            _stage(report, name, StageStatus.SKIPPED, f"the prober failed ({type(exc).__name__})", clock.lap())
            return
    specs = reconcile.parse_specs(probes)
    if params:
        for spec in specs:
            spec.sql = template.prepare(spec.sql, dialect, params).executable_sql or spec.sql
    outcome = reconcile.run(
        base.columns,
        base.rows,
        specs,
        dialect=dialect,
        catalog=catalog,
        adapter=adapter,
        timeout_s=options.timeout_s,
        budget_s=options.reconcile_budget_s,
        cost_ceiling=options.cost_ceiling,
        max_probes=options.reconcile_max_probes,
        allowed_functions=options.allowed_functions,
        allowed_tables=options.allowed_tables,
        weights=options.source_weights or None,
    )
    report.signals["reconciliation"] = outcome.to_dict()
    if not outcome.ran:
        _stage(report, name, StageStatus.SKIPPED, outcome.reason, clock.lap())
        return
    report.findings.extend(outcome.findings)
    _stage(
        report,
        name,
        StageStatus.PASSED,
        f"{outcome.satisfied} anchor(s) agree with the result, {outcome.violated} disagree",
        clock.lap(),
    )


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
    try:
        dry = adapter.dry_run(sql)
    except UnsafeQueryError as exc:
        # The statement as it would be sent (a template with its values filled in) is not one
        # read-only query: it does not parse, or it calls something the gate refuses.
        report.findings.append(
            Finding(
                "render_error",
                Severity.ERROR,
                f"With its parameters filled in, the statement cannot be sent to the database: {exc}",
                "dry_run",
            )
        )
        _stage(report, "dry_run", StageStatus.FAILED, str(exc)[:200], clock.lap())
        _not_run(report, ("sample",), "the statement cannot be sent")
        return
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
    if adapter.dialect not in ("sqlite", "mysql"):
        # These engines reject a query with an ungrouped column. This one was accepted,
        # so a static finding that predicted a rejection was mistaken.
        report.findings = [f for f in report.findings if f.code != "missing_group_by"]

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
                if finding.evidence.get("data_check"):
                    continue  # already settled by running the subquery itself
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
    # Uniqueness probes: finding -> group -> outcomes. A group holds when every outcome is True.
    unique_results: dict[int, dict[str | None, list[tuple[bool, int | None] | None]]] = {}
    unique_findings: dict[int, Finding] = {}
    done: set[tuple] = set()
    skipped_for_size: set[str] = set()

    def note_unique(probe: Probe, outcome: tuple[bool, int | None] | None) -> None:
        if probe.finding is not None:
            unique_results.setdefault(id(probe.finding), {}).setdefault(probe.group, []).append(outcome)
            unique_findings[id(probe.finding)] = probe.finding

    for probe in ctx.probes:
        key = (probe.kind, probe.table.qualified_name, probe.columns, probe.literal, probe.operator, id(probe.finding), probe.group, probe.snippet)
        if key in done:
            continue
        done.add(key)
        if probe.kind == PROBE_UNIQUE and probe.finding is not None:
            unique_results.setdefault(id(probe.finding), {}).setdefault(probe.group, [])
            unique_findings[id(probe.finding)] = probe.finding
        if budget <= 0:
            report.checks_not_run.append(SkippedCheck(f"probe:{probe.kind}", "probe budget for this query is used up"))
            if probe.kind == PROBE_UNIQUE:
                note_unique(probe, None)
            continue
        if not _probe_allowed(adapter, probe.table, options):
            skipped_for_size.add(probe.table.qualified_name)
            if probe.kind == PROBE_UNIQUE:
                note_unique(probe, None)
            continue
        budget -= 1
        try:
            if probe.kind == PROBE_UNIQUE:
                note_unique(probe, _probe_unique(adapter, probe, options, ctx))
            elif probe.kind == PROBE_HAS_NULL:
                _probe_has_null(adapter, probe, options)
            elif probe.kind == PROBE_VALUE:
                _probe_value(report, ctx, adapter, probe, options)
            elif probe.kind == PROBE_TIMESTAMP_FORMAT:
                _probe_timestamp_format(report, adapter, probe, options)
            elif probe.kind == PROBE_QUERY:
                _probe_query(adapter, probe, options, ctx)
        except AdapterError as exc:
            logger.debug("probe failed: %s", exc)
            report.checks_not_run.append(SkippedCheck(f"probe:{probe.kind}", str(exc)))
            if probe.kind == PROBE_UNIQUE:
                note_unique(probe, None)

    for table in sorted(skipped_for_size):
        report.checks_not_run.append(
            SkippedCheck("probe", f"{table} is too large to probe within the row ceiling")
        )

    for finding_id, groups in unique_results.items():
        _settle_uniqueness(unique_findings[finding_id], groups)


def _outcome(outcomes: list[tuple[bool, int | None] | None] | None) -> bool | None:
    """One answer for a group of probes: True when every probe found no repeats, False when any
    did, None when a probe could not run."""
    if not outcomes or any(o is None for o in outcomes):
        return None
    return all(o[0] for o in outcomes)  # type: ignore[index]


def _settle_uniqueness(finding: Finding, groups: dict[str | None, list]) -> None:
    """Weigh the uniqueness probes of one finding against each other.

    Groups named ``alias:join`` ran the query's own join with its WHERE and asked whether a row of
    that table came back twice; ``alias:join:unfiltered`` asked the same without the WHERE. The
    other groups asked each repeating table on its own.

    - Repeats in the filtered join, or in every table-level group: duplicates, confirmed.
    - No repeats in the filtered join while the unfiltered join does repeat: the query's own
      filters pin the grain. Downgrade, whatever the schema allows in general.
    - No repeats anywhere, and the schema says repeats are possible (certainty "yes"): the sample
      is merely sparse. The static severity stands, with a note.
    - No repeats anywhere, and no key says either way: observed uniqueness, LOW (MEDIUM when too
      few rows were there to tell).
    """
    joins = {g: _outcome(o) for g, o in groups.items() if g is not None and g.endswith(":join")}
    unfiltered = {g[: -len(":unfiltered")]: _outcome(o) for g, o in groups.items() if g is not None and g.endswith(":join:unfiltered")}
    tables = {g: o for g, o in groups.items() if g is None or not (g.endswith(":join") or g.endswith(":join:unfiltered"))}
    table_outcomes = {g: _outcome(o) for g, o in tables.items()}

    def rows_in(outcomes: list) -> int | None:
        counts = [o[1] for o in outcomes if o is not None]
        return min(counts) if counts and all(c is not None for c in counts) else None

    declared = finding.evidence.get("certainty") == "yes"
    pinned_by_filters = any(ok and unfiltered.get(g) is False for g, ok in joins.items())
    join_unique = any(ok for ok in joins.values())
    join_repeats = joins and all(ok is False for ok in joins.values() if ok is not None) and any(ok is False for ok in joins.values())
    tables_unique = [g for g, ok in table_outcomes.items() if ok]
    tables_repeat = tables and all(ok is False for ok in table_outcomes.values())

    if join_repeats or (not joins and tables_repeat):
        finding.evidence["data_check"] = "duplicates"
        if finding.evidence.get("certainty") == "maybe":
            finding.evidence["certainty"] = "yes"
            finding.severity = Severity.HIGH
        finding.message += " Checked against the data: the join does repeat rows of the table being aggregated."
        return
    if not join_unique and not tables_unique:
        return  # nothing conclusive could be run

    if pinned_by_filters:
        rows = min((r for r in (rows_in(groups[g]) for g, ok in joins.items() if ok) if r is not None), default=None)
        finding.severity = Severity.LOW
        finding.evidence["data_check"] = "pinned_by_filters"
        finding.evidence["certainty"] = "observed"
        finding.evidence["rows_probed"] = rows
        finding.message += (
            " Checked against the data: the join as written would repeat rows, but with this query's "
            "own filters each row comes through once"
            + (f" ({rows} rows)" if rows is not None else "")
            + ", so nothing is double-counted today."
        )
        return

    # No repeats anywhere that was looked at.
    counted = [rows_in(groups[g]) for g in tables_unique] or [rows_in(groups[g]) for g, ok in joins.items() if ok]
    rows = min((r for r in counted if r is not None), default=None)
    finding.evidence["rows_probed"] = rows
    if declared:
        # The schema permits repeats (a declared many-to-one); a sample without any proves only
        # that this sample is sparse.
        finding.evidence["data_check"] = "not_observed"
        finding.message += (
            " Not observed in the sample"
            + (f" ({rows} rows)" if rows is not None else "")
            + ", but the schema allows repeats, so the risk stands."
        )
        return
    finding.evidence["data_check"] = "unique_observed"
    if rows is not None and rows < SMALL_TABLE_ROWS:
        finding.evidence["certainty"] = "maybe"
        if finding.severity == Severity.HIGH:
            finding.severity = Severity.MEDIUM
        finding.message += (
            f" Checked against the data: no repeated values, but only {rows} rows were there to "
            "check, which is too few to tell, and no key enforces it."
        )
    else:
        finding.severity = Severity.LOW
        finding.evidence["certainty"] = "observed"
        finding.message += (
            " Checked against the data: the join columns hold no repeated values"
            + (f" among {rows} rows" if rows is not None else "")
            + ", so nothing is double-counted today. No key guarantees it stays that way."
        )


def _fill_parameters(query: exp.Expression, params: dict[str, Any] | None) -> exp.Expression:
    """A probe built from a templated statement carries its parameters; give them their values."""
    markers = list(query.find_all(exp.Placeholder, exp.Parameter))
    if not markers:
        return query
    query = query.copy()
    for marker in query.find_all(exp.Placeholder, exp.Parameter):
        name = marker.name if isinstance(marker.this, str) else getattr(marker.this, "name", None)
        if not name or params is None or name not in params:
            raise AdapterError(f"the probe needs a value for parameter {name or '?'}")
        marker.replace(template.literal_node(params[name]))
    return query


def _probe_unique(adapter: Adapter, probe: Probe, options: Options, ctx: CheckContext) -> tuple[bool, int | None]:
    """``(no repeated non-NULL combination, rows looked at)``."""
    if probe.query is not None:  # a derived table, run as written and grouped by its join columns
        query = _fill_parameters(probe.query, ctx.params)
        unique = not adapter.query(query, max_rows=1, timeout_s=options.probe_timeout_s)
        rows = None
        if unique:
            inner = query.args.get("from_") or query.args.get("from")
            source = inner.this if inner is not None else None
            if source is not None:
                # How many rows were looked at: the derived table's rows, or, for a probe over a
                # whole join, the join's rows up to a bound.
                rows_seen = exp.Select(expressions=[exp.Literal.number(1)])
                for arg in ("from_", "from", "joins", "where"):
                    if query.args.get(arg) is not None:
                        rows_seen.set(arg, query.args[arg].copy())
                count = exp.select(exp.Count(this=exp.Star())).from_(
                    exp.Subquery(this=rows_seen.limit(SMALL_TABLE_ROWS * 20), alias=exp.TableAlias(this=exp.to_identifier("sqlgami_n")))
                )
                with_ = query.args.get("with_") or query.args.get("with")
                if with_ is not None:
                    count.set("with_", with_.copy())
                try:
                    rows = int(adapter.query(count, max_rows=1, timeout_s=options.probe_timeout_s)[0][0])
                except (AdapterError, IndexError, TypeError, ValueError):
                    rows = None
        return unique, rows
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
    unique = not adapter.query(query, max_rows=1, timeout_s=options.probe_timeout_s)
    rows = None
    if unique:
        try:
            rows = adapter.row_count(probe.table)
        except SqlgamiError:
            rows = None
        if rows is None:  # no statistics: count, but only up to a bound
            bounded = exp.select(exp.Count(this=exp.Star())).from_(
                exp.Subquery(
                    this=exp.select(exp.Literal.number(1)).from_(adapter.table_ref(probe.table)).limit(SMALL_TABLE_ROWS * 20),
                    alias=exp.TableAlias(this=exp.to_identifier("sqlgami_n")),
                )
            )
            try:
                rows = int(adapter.query(bounded, max_rows=1, timeout_s=options.probe_timeout_s)[0][0])
            except (AdapterError, IndexError, TypeError, ValueError):
                rows = None
    return unique, rows


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


def _probe_query(adapter: Adapter, probe: Probe, options: Options, ctx: CheckContext) -> None:
    """Run a query a check prepared, and adjust its finding by whether a row comes back."""
    if probe.query is None or probe.finding is None:
        return
    rows = adapter.query(_fill_parameters(probe.query, ctx.params), max_rows=probe.min_rows, timeout_s=options.probe_timeout_s)
    outcome = probe.on_rows if len(rows) >= probe.min_rows else probe.on_empty
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
        partial = False
        for sibling in probe.siblings:
            # Another value in the same IN list is stored, so the list as a whole still matches.
            if column.values is not None:
                partial = judge_value(column.values, sibling)[0]
            else:
                partial = bool(
                    adapter.query(
                        exp.select(exp.Literal.number(1))
                        .from_(ref)
                        .where(exp.EQ(this=_column(column.name), expression=exp.Literal.string(sibling)))
                        .limit(1),
                        max_rows=1,
                        timeout_s=options.probe_timeout_s,
                    )
                )
            if partial:
                break
        if partial and not near:
            return  # an unremarkable value that simply has no rows; the list still matches
        report.findings.append(
            value_finding(probe.snippet, table, column, literal, near, negated, "sample", partial=partial)
        )


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
