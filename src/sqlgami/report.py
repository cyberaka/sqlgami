"""The report data model.

Everything here is a plain dataclass that converts to JSON-serialisable
dictionaries. The shape is versioned by :data:`SCHEMA_VERSION`; fields are only
ever added within a major version.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = "1.0"

ADVISORY = (
    "Advisory only. This report is an estimate of how likely the SQL is to answer "
    "the question. It is not a guarantee, and it comes with no warranty. Review "
    "the SQL and its results before relying on them."
)


class Severity(str, Enum):
    """How much a finding should lower confidence."""

    ERROR = "error"  # the statement cannot run, or must not be run
    HIGH = "high"  # very likely returns wrong data
    MEDIUM = "medium"  # plausibly returns wrong data
    LOW = "low"  # worth a look
    INFO = "info"  # context only, does not lower confidence

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    Severity.ERROR: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}


class Verdict(str, Enum):
    INVALID = "invalid"  # cannot run, or is not a read-only query
    LIKELY_WRONG = "likely_wrong"
    UNCERTAIN = "uncertain"
    LIKELY_RIGHT = "likely_right"
    UNVERIFIED = "unverified"  # too little was checked to give a number


class StageStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_RUN = "not_run"


@dataclass
class Finding:
    """One thing a check noticed about the SQL."""

    code: str
    severity: Severity
    message: str
    stage: str = "static"
    snippet: str | None = None
    suggestion: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "stage": self.stage,
            "snippet": self.snippet,
            "suggestion": self.suggestion,
            "evidence": _plain(self.evidence),
        }


@dataclass
class CostEstimate:
    """What running the query is expected to cost, in the engine's own unit."""

    unit: str = "unknown"  # rows | bytes | request_units | unknown
    value: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"unit": self.unit, "value": self.value, "detail": _plain(self.detail)}


@dataclass
class StageResult:
    name: str
    status: StageStatus
    detail: str | None = None
    duration_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "duration_ms": None if self.duration_ms is None else round(self.duration_ms, 3),
        }


@dataclass
class Confidence:
    """The confidence estimate.

    ``value`` is ``None`` when sqlgami declines to give a number. ``calibrated``
    is true only when a fitted calibration model produced the value; otherwise
    the value is a provisional heuristic and must be read as a rough ordering.
    """

    value: float | None = None
    low: float | None = None
    high: float | None = None
    calibrated: bool = False
    cap: float | None = None
    cap_reason: str | None = None
    basis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": _round(self.value),
            "low": _round(self.low),
            "high": _round(self.high),
            "calibrated": self.calibrated,
            "cap": self.cap,
            "cap_reason": self.cap_reason,
            "basis": self.basis,
        }


@dataclass
class SkippedCheck:
    code: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "reason": self.reason}


@dataclass
class Report:
    """The result of scoring one SQL statement."""

    engine: str
    mode: str
    sql: str
    question: str | None = None
    verdict: Verdict = Verdict.UNVERIFIED
    confidence: Confidence = field(default_factory=Confidence)
    stages: list[StageResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    checks_not_run: list[SkippedCheck] = field(default_factory=list)
    cost: CostEstimate | None = None
    signals: dict[str, Any] = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)
    plan: Any = None
    schema_version: str = SCHEMA_VERSION
    advisory: str = ADVISORY

    @property
    def score(self) -> float | None:
        return self.confidence.value

    def findings_at_least(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity.rank >= severity.rank]

    def has(self, code: str) -> bool:
        return any(f.code == code for f in self.findings)

    def stage(self, name: str) -> StageResult | None:
        for stage in self.stages:
            if stage.name == name:
                return stage
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "advisory": self.advisory,
            "engine": self.engine,
            "mode": self.mode,
            "question": self.question,
            "sql": self.sql,
            "verdict": self.verdict.value,
            "confidence": self.confidence.to_dict(),
            "stages": [s.to_dict() for s in self.stages],
            "findings": [f.to_dict() for f in self.findings],
            "checks_not_run": [c.to_dict() for c in self.checks_not_run],
            "cost": None if self.cost is None else self.cost.to_dict(),
            "signals": _plain(self.signals),
            "tables": list(self.tables),
            "plan": _plain(self.plan),
        }

    def to_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("ensure_ascii", False)
        return json.dumps(self.to_dict(), **kwargs)

    def summary(self) -> str:
        """A short plain-text rendering, for logs and the CLI."""
        conf = self.confidence
        if conf.value is None:
            head = f"{self.verdict.value}: no confidence number ({conf.basis})"
        else:
            kind = "calibrated" if conf.calibrated else "provisional, not calibrated"
            head = (
                f"{self.verdict.value}: confidence {conf.value:.2f} "
                f"(range {conf.low:.2f}-{conf.high:.2f}, {kind})"
            )
        lines = [head]
        if conf.cap_reason:
            lines.append(f"  capped at {conf.cap}: {conf.cap_reason}")
        for stage in self.stages:
            detail = f" - {stage.detail}" if stage.detail else ""
            lines.append(f"  [{stage.status.value:>7}] {stage.name}{detail}")
        order = sorted(self.findings, key=lambda f: -f.severity.rank)
        for finding in order:
            lines.append(f"  {finding.severity.value.upper():<6} {finding.code}: {finding.message}")
        for skipped in self.checks_not_run:
            lines.append(f"  NOT CHECKED {skipped.code}: {skipped.reason}")
        lines.append(self.advisory)
        return "\n".join(lines)


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


def _plain(value: Any) -> Any:
    """Coerce a value into something ``json.dumps`` accepts."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_plain(v) for v in value]
        if isinstance(value, (set, frozenset)):
            items.sort(key=repr)
        return items
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)
