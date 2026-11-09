"""Shared plumbing for the static checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from sqlglot import exp

from ..analysis import Analysis
from ..catalog import Catalog, Table
from ..model import ModelCache
from ..report import Finding, Severity

# Probe kinds, resolved against real data in the sample stage.
PROBE_UNIQUE = "unique"
PROBE_HAS_NULL = "has_null"
PROBE_VALUE = "value"
PROBE_TIMESTAMP_FORMAT = "timestamp_format"
PROBE_QUERY = "query"  # run a ready-made query; act on whether it returns a row


@dataclass
class Probe:
    """A question about the data that would confirm or dismiss a finding."""

    kind: str
    table: Table
    columns: tuple[str, ...]
    finding: Finding | None = None
    literal: Any = None
    operator: str = "="
    snippet: str | None = None
    # For PROBE_HAS_NULL: the severity when NULLs are found, and when they are not.
    confirm: Severity = Severity.HIGH
    dismiss: Severity = Severity.LOW
    # For PROBE_QUERY: the query, and what to do with the finding in each outcome.
    # Each outcome is (new severity or None to leave it, text appended to the message, data_check tag).
    query: Any = None
    min_rows: int = 1  # "rows" means at least this many came back
    # For PROBE_VALUE on one value of an IN list: the other values listed with it.
    siblings: tuple[str, ...] = ()
    on_rows: tuple[Severity | None, str, str] | None = None
    on_empty: tuple[Severity | None, str, str] | None = None


@dataclass
class CheckContext:
    analysis: Analysis
    catalog: Catalog
    question: str | None = None
    cache: ModelCache = field(init=False)
    probes: list[Probe] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cache = ModelCache(self.analysis, self.catalog)

    @property
    def dialect(self) -> str:
        return self.analysis.dialect

    def snippet(self, node: exp.Expression | None, limit: int = 160) -> str | None:
        if node is None:
            return None
        try:
            text = node.sql(dialect=self.dialect)
        except Exception:
            return None
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def finding(
        self,
        code: str,
        severity: Severity,
        message: str,
        node: exp.Expression | None = None,
        suggestion: str | None = None,
        **evidence: Any,
    ) -> Finding:
        return Finding(
            code=code,
            severity=severity,
            message=message,
            stage="static",
            snippet=self.snippet(node),
            suggestion=suggestion,
            evidence=evidence,
        )


CheckFn = Callable[[CheckContext], Iterable[Finding]]


@dataclass
class Check:
    code: str
    run: CheckFn
    needs_types: bool = False
    description: str = ""


REGISTRY: list[Check] = []


def check(code: str, *, needs_types: bool = False, description: str = "") -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        REGISTRY.append(Check(code, fn, needs_types, description or (fn.__doc__ or "").strip()))
        return fn

    return register
