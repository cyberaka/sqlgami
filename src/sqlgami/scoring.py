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
# The candidate is in the majority among independent alternatives: one, two, three or more agree.
BONUS_CONSISTENT = (0.7, 1.2, 1.6)  # counted in distinct sources (models), not samples
BONUS_SAME_SOURCE_REPEAT = 0.15
BONUS_SPLIT_VOTE = 0.2
# Anchor queries (reconciliation) that the result satisfies, none violated: one, several, or
# several that two independent sources agreed on.
BONUS_RECONCILED = (0.4, 0.6, 0.9)
# The candidate's result equals the reference's on one / several of the bundle's fixtures.
BONUS_FIXTURES = (1.2, 1.6)
# With source weights (their known share of right answers) the agreement bonus is scaled by the
# agreeing sources' average weight, and sources known to be wrong more often than not can
# together add at most this much: a wrong query agreeing with a wrong alternative is common.
BONUS_WEAK_AGREEMENT_CAP = 0.4
WEAK_SOURCE = 0.5

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
    "unknown_function": CAP_CANNOT_RUN,
    "render_error": CAP_CANNOT_RUN,
    "execution_error": CAP_CANNOT_RUN,
    "external_access": CAP_CANNOT_RUN,
}
SOFT_CAP_CODES = {
    "dry_run_unavailable": CAP_ENGINE_UNAVAILABLE,
    "execution_timeout": CAP_ENGINE_UNAVAILABLE,
    "execution_unavailable": CAP_ENGINE_UNAVAILABLE,
}
# A business term the question uses that the host's catalogue does not define in data terms:
# sqlgami cannot confirm the query reads it correctly, so confidence stays below this unless
# reliable independent sources agree with the answer (that is evidence about the answer, not
# about the words).
CAP_UNDEFINED_TERM = 0.75


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
    """How far the checks actually got: ``static``, ``dry`` or ``sample``. A run on the bundle's
    fixtures against a reference is a run: it counts as ``sample`` depth."""
    executed = report.stage("sample")
    if executed is not None and executed.status == StageStatus.PASSED:
        return "sample"
    if (report.signals.get("fixtures") or {}).get("verdict") in ("match", "mismatch"):
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
    bonus += _consistency_bonus(report)
    bonus += _reconciliation_bonus(report)
    bonus += _fixture_bonus(report)
    return bonus


def _fixture_bonus(report: Report) -> float:
    """The candidate matched the reference on the bundle's fixtures: the strongest
    deterministic support there is, worth what two reliable independent sources earn."""
    signal = report.signals.get("fixtures") or {}
    if signal.get("verdict") != "match":
        return 0.0
    matched = int(signal.get("matched") or 0)
    return BONUS_FIXTURES[1] if matched >= 2 else BONUS_FIXTURES[0] if matched == 1 else 0.0


def _reconciliation_bonus(report: Report) -> float:
    signal = report.signals.get("reconciliation") or {}
    satisfied, violated = signal.get("satisfied") or 0, signal.get("violated") or 0
    if not satisfied or violated:
        return 0.0
    if signal.get("agreed_satisfied"):
        return BONUS_RECONCILED[2]
    return BONUS_RECONCILED[1] if satisfied >= 2 else BONUS_RECONCILED[0]


def _reliable_agreement(report: Report) -> bool:
    """Independent sources known to be reliable agree with the result (weighted consistency), or
    two sources agreed on anchors the result satisfies."""
    signal = report.signals.get("consistency") or {}
    if signal.get("weighted") and float(signal.get("agreeing_weight") or 0) >= 1.2:
        rival = signal.get("largest_disagreeing_cluster_sources") or 0
        if (signal.get("agreeing_sources") or 0) > rival:
            return True
    anchors = report.signals.get("reconciliation") or {}
    if anchors.get("agreed_satisfied") and not anchors.get("violated"):
        return True
    # The host's own verified reference agrees with the result on the bundle's fixtures.
    return (report.signals.get("fixtures") or {}).get("verdict") == "match"


def _consistency_bonus(report: Report) -> float:
    """Support from independently written queries that return the same result."""
    signal = report.signals.get("consistency") or {}
    sources, agreeing = signal.get("sources") or 0, signal.get("agreeing_sources") or 0
    rival = signal.get("largest_disagreeing_cluster_sources") or 0
    if not sources or not agreeing or agreeing * 2 < sources or agreeing < rival:
        return 0.0
    if rival and agreeing == rival:
        return BONUS_SPLIT_VOTE  # as many sources against as for: slight support at most
    bonus = BONUS_CONSISTENT[min(agreeing, len(BONUS_CONSISTENT)) - 1]
    if agreeing == 1 and (signal.get("agreeing") or 0) >= 2:
        bonus += BONUS_SAME_SOURCE_REPEAT  # several samples of one source agree: a little more
    if signal.get("weighted"):
        weight = float(signal.get("agreeing_weight") or 0.0)
        average = weight / agreeing
        bonus *= min(1.0, average / 0.75)  # sources right three times in four earn the full bonus
        if average < WEAK_SOURCE:
            bonus = min(bonus, BONUS_WEAK_AGREEMENT_CAP)
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
    if any(f.code == "undefined_term" for f in findings) and not _reliable_agreement(report):
        names = sorted({str(f.evidence.get("term") or "?") for f in findings if f.code == "undefined_term"})
        apply_cap(CAP_UNDEFINED_TERM, f"the catalogue does not define: {', '.join(names)}")

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

    fixtures = report.signals.get("fixtures") or {}
    ran_on_host = (sample := report.stage("sample")) is not None and sample.status == StageStatus.PASSED
    parts = [
        {
            "static": "static checks only: the query was not planned or run against a database",
            "dry": "static checks and the engine's planner: the query was not run",
            "sample": "static checks, the engine's planner and one read-only run" if ran_on_host else "static checks and runs on the bundle's fixture datasets",
        }[depth]
    ]
    if fixtures.get("verdict") == "match":
        parts.append(f"the result equals the reference's on {fixtures.get('matched')} fixture dataset(s)")
    elif fixtures.get("verdict") == "mismatch":
        parts.append("the result differs from the reference's on a fixture dataset")
    if not analysed:
        parts.append("static checks did not run")
    sample = report.stage("sample")
    if sample is not None and sample.status == StageStatus.SKIPPED and report.mode in ("sample", "full"):
        parts.append("unverified beyond static and dry-run checks")
    agreement = report.signals.get("consistency") or {}
    if agreement.get("comparable"):
        parts.append(
            f"{agreement.get('agreeing', 0)} of {agreement['comparable']} alternative queries, from "
            f"{agreement.get('sources', 0)} independent source(s), returned the same result"
        )
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
