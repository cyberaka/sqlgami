"""Turn stage results and findings into a confidence estimate.

Hard gates come first: a statement that cannot run, or is not a read-only
query, is capped near zero whatever else is true. After the gates, the
remaining findings move a prior up or down in log-odds.

Until a calibration model fitted on labelled data is supplied, the number is a
provisional heuristic and the report says so (``calibrated: false``). It orders
queries from more to less trustworthy; it is not a measured probability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from .report import Confidence, Report, Severity, StageStatus, Verdict

# Caps applied by the gates.
CAP_INVALID = 0.0  # does not parse
CAP_CANNOT_RUN = 0.1  # unknown table or column, not a read-only query, engine rejects it
CAP_HARD_FINDING = 0.4  # a high-severity static finding
CAP_ENGINE_UNAVAILABLE = 0.5  # the engine could not plan or run it for unrelated reasons

# Starting point by how far the checks got. A query nobody has run cannot rate "likely right".
PRIOR = {"static": 0.62, "dry": 0.68, "sample": 0.76}
PRIOR_UNANALYSED = 0.5

# Log-odds penalty per finding.
WEIGHT = {
    Severity.HIGH: 1.6,
    Severity.MEDIUM: 0.75,
    Severity.LOW: 0.25,
    Severity.INFO: 0.0,
    Severity.ERROR: 0.0,  # errors act through the gates
}
REPEAT_DISCOUNT = 0.5  # second and later findings of the same kind count for less

STACK_DISCOUNT = 0.6  # each further medium or low finding counts for this much of the one before

# Positive evidence, in log-odds. Without it nothing could score in the upper band,
# and a host application could never let anything through unreviewed.
BONUS_QUESTION_AGREES = 0.40  # the question was given and none of its checks objected
BONUS_NOTHING_FOUND = 0.30  # no finding of any severity, and every check ran
BONUS_ROWS_RETURNED = 0.15  # the query ran and returned something

HALF_WIDTH = {"static": 0.20, "dry": 0.17, "sample": 0.12}

LIKELY_RIGHT_AT = 0.70
LIKELY_WRONG_AT = 0.40
# "Likely wrong" is a strong statement. It takes high-severity evidence or a failed run to make
# it; doubts of medium and low severity alone leave the score in the "uncertain" band. Scores
# below SOFT_KNEE are squeezed into (SOFT_FLOOR, SOFT_KNEE), which keeps their order.
SOFT_FLOOR = 0.42
SOFT_KNEE = 0.60

# Finding codes that mean the statement cannot produce an answer at all.
GATE_CODES = {
    "parse_error": CAP_INVALID,
    "empty_statement": CAP_INVALID,
    "not_read_only": CAP_CANNOT_RUN,
    "multiple_statements": CAP_CANNOT_RUN,
    "not_a_query": CAP_CANNOT_RUN,
    "unknown_table": CAP_CANNOT_RUN,
    "unknown_column": CAP_CANNOT_RUN,
    "ambiguous_column": CAP_CANNOT_RUN,
    "dry_run_error": CAP_CANNOT_RUN,
    "execution_error": CAP_CANNOT_RUN,
    "external_access": CAP_CANNOT_RUN,
}
SOFT_CAP_CODES = {
    "dry_run_unavailable": CAP_ENGINE_UNAVAILABLE,
    "execution_timeout": CAP_ENGINE_UNAVAILABLE,
    "execution_unavailable": CAP_ENGINE_UNAVAILABLE,
}


class Calibrator(Protocol):
    """Maps a provisional score to a measured probability."""

    def calibrate(self, raw: float, depth: str) -> float | None:
        """Return the calibrated probability, or ``None`` if this depth was not fitted."""


@dataclass
class Scored:
    confidence: Confidence
    verdict: Verdict


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def evidence_depth(report: Report) -> str:
    """How far the checks actually got: ``static``, ``dry`` or ``sample``."""
    executed = report.stage("sample")
    if executed is not None and executed.status == StageStatus.PASSED:
        return "sample"
    dry = report.stage("dry_run")
    if dry is not None and dry.status == StageStatus.PASSED:
        return "dry"
    return "static"


def _clean_bonus(report: Report, depth: str, analysed: bool) -> float:
    """Log-odds added for what went right. Only earned once the query has actually run."""
    if depth != "sample" or not analysed:
        return 0.0
    bonus = 0.0
    scored = [f for f in report.findings if f.severity != Severity.INFO]
    question = report.stage("question")
    if question is not None and question.status == StageStatus.PASSED:
        if not any(f.stage == "question" for f in scored):
            bonus += BONUS_QUESTION_AGREES
    if not scored and not report.checks_not_run:
        bonus += BONUS_NOTHING_FOUND
    if report.signals.get("rows_returned"):
        bonus += BONUS_ROWS_RETURNED
    return bonus


def score_report(report: Report, calibrator: Calibrator | None = None) -> Scored:
    findings = report.findings
    depth = evidence_depth(report)

    cap: float | None = None
    cap_reason: str | None = None

    def apply_cap(value: float, reason: str) -> None:
        nonlocal cap, cap_reason
        if cap is None or value < cap:
            cap, cap_reason = value, reason

    for finding in findings:
        if finding.severity == Severity.ERROR and finding.code in GATE_CODES:
            apply_cap(GATE_CODES[finding.code], finding.message)
        elif finding.code in SOFT_CAP_CODES:
            apply_cap(SOFT_CAP_CODES[finding.code], finding.message)
    for finding in findings:
        if finding.severity == Severity.HIGH:
            apply_cap(CAP_HARD_FINDING, f"high-severity finding: {finding.code}")
            break

    if cap is not None and cap <= CAP_CANNOT_RUN:
        value = 0.0 if cap == CAP_INVALID else cap / 2
        return Scored(
            Confidence(
                value=value,
                low=0.0,
                high=cap,
                calibrated=False,
                cap=cap,
                cap_reason=cap_reason,
                basis="hard gate: the statement cannot produce an answer as written",
                raw=value,
            ),
            Verdict.INVALID,
        )

    static = report.stage("static")
    analysed = static is not None and static.status == StageStatus.PASSED
    prior = PRIOR[depth] if analysed else min(PRIOR[depth], PRIOR_UNANALYSED)
    logit = _logit(prior)
    seen: dict[str, int] = {}
    softer = 0  # medium and low findings counted so far
    for finding in sorted(findings, key=lambda f: -f.severity.rank):
        weight = WEIGHT.get(finding.severity, 0.0)
        if not weight:
            continue
        count = seen.get(finding.code, 0)
        seen[finding.code] = count + 1
        weight *= REPEAT_DISCOUNT**count
        if finding.severity != Severity.HIGH:
            # Heuristic findings are rarely independent of one another: each further one
            # counts for less, so a pile of soft doubts cannot by itself amount to "likely wrong".
            weight *= STACK_DISCOUNT**softer
            softer += 1
        logit -= weight
    logit += _clean_bonus(report, depth, analysed)
    raw = _sigmoid(logit)
    strong = any(f.severity in (Severity.HIGH, Severity.ERROR) for f in findings)
    if not strong and raw < SOFT_KNEE:
        raw = SOFT_FLOOR + (raw / SOFT_KNEE) * (SOFT_KNEE - SOFT_FLOOR)
    if cap is not None:
        raw = min(raw, cap)

    calibrated = False
    value = raw
    if calibrator is not None:
        fitted = calibrator.calibrate(raw, depth)
        if fitted is not None:
            value = min(fitted, cap) if cap is not None else fitted
            calibrated = True

    width = HALF_WIDTH[depth]
    if not analysed:
        width += 0.10
    unconfirmed = sum(1 for f in findings if f.evidence.get("certainty") == "maybe")
    width += min(0.10, 0.03 * unconfirmed)
    width += min(0.10, 0.02 * len(report.checks_not_run))
    low = max(0.0, value - width)
    high = min(1.0, value + width)
    if cap is not None:
        high = min(high, cap)
        low = min(low, high)

    parts = [
        {
            "static": "static checks only: the query was not planned or run against a database",
            "dry": "static checks and the engine's planner: the query was not run",
            "sample": "static checks, the engine's planner and one read-only run",
        }[depth]
    ]
    if not analysed:
        parts.append("static checks did not run")
    sample = report.stage("sample")
    if sample is not None and sample.status == StageStatus.SKIPPED and report.mode in ("sample", "full"):
        parts.append("unverified beyond static and dry-run checks")
    parts.append("calibrated on labelled data" if calibrated else "provisional heuristic, not calibrated")
    basis = "; ".join(parts)

    if value >= LIKELY_RIGHT_AT:
        verdict = Verdict.LIKELY_RIGHT
    elif value <= LIKELY_WRONG_AT:
        verdict = Verdict.LIKELY_WRONG
    else:
        verdict = Verdict.UNCERTAIN
    return Scored(
        Confidence(
            value=value,
            low=low,
            high=high,
            calibrated=calibrated,
            cap=cap,
            cap_reason=cap_reason,
            basis=basis,
            raw=raw,
        ),
        verdict,
    )
