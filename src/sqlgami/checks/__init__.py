"""Static checks. Importing this package registers every check."""

from __future__ import annotations

import logging

from ..report import Finding, SkippedCheck
from .base import REGISTRY, Check, CheckContext, Probe
from . import aggregation, joins, policy, predicates  # noqa: F401  (registration)

logger = logging.getLogger("sqlgami")

__all__ = ["REGISTRY", "Check", "CheckContext", "Probe", "run_static_checks", "check_codes"]


def check_codes() -> list[str]:
    return [c.code for c in REGISTRY]


def run_static_checks(ctx: CheckContext) -> tuple[list[Finding], list[SkippedCheck]]:
    """Run every registered check. A check that fails internally is reported as not run."""
    findings: list[Finding] = []
    skipped: list[SkippedCheck] = []
    for item in REGISTRY:
        if item.needs_types and not ctx.analysis.types_annotated:
            skipped.append(SkippedCheck(item.code, "column types could not be worked out"))
            continue
        try:
            findings.extend(item.run(ctx) or [])
        except RecursionError:
            skipped.append(SkippedCheck(item.code, "statement is nested too deeply"))
        except Exception as exc:  # a check must never break scoring
            logger.debug("check %s failed", item.code, exc_info=True)
            skipped.append(SkippedCheck(item.code, f"internal error: {type(exc).__name__}: {exc}"))
    return findings, skipped
