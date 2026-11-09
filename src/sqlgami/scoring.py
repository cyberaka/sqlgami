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

HALF_WIDTH = {"static": 0.20, "dry": 0.17, "sample": 0.12}

LIKELY_RIGHT_AT = 0.70
LIKELY_WRONG_AT = 0.40

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
    for finding in sorted(findings, key=lambda f: -f.severity.rank):
        weight = WEIGHT.get(finding.severity, 0.0)
        if not weight:
            continue
        count = seen.get(finding.code, 0)
        seen[finding.code] = count + 1
        logit -= weight * (REPEAT_DISCOUNT**count)
    raw = _sigmoid(logit)
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

    parts = [f"{depth} evidence"]
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
