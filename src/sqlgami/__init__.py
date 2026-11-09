"""sqlgami: advisory confidence reports for LLM-written SQL.

    from sqlgami import Scorer

    scorer = Scorer("sqlite", db=connection)
    report = scorer.score(sql, question="How many orders shipped last month?")
    report.to_dict()

The report is an estimate, not a guarantee. See ``report.advisory``.
"""

from __future__ import annotations

import logging

from .catalog import Catalog, Column, ForeignKey, Table
from .errors import (
    AdapterError,
    CatalogError,
    ConfigurationError,
    ConnectionFailed,
    MissingDependencyError,
    SqlgamiError,
    UnsafeQueryError,
)
from .pipeline import Options
from .report import (
    ADVISORY,
    SCHEMA_VERSION,
    Confidence,
    CostEstimate,
    Finding,
    Report,
    Severity,
    StageResult,
    StageStatus,
    Verdict,
)
from .scorer import Scorer

__version__ = "0.1.0"

# A library must not emit log output unless the host application asks for it.
logging.getLogger("sqlgami").addHandler(logging.NullHandler())
# sqlglot warns through its own logger when it meets syntax it does not model. Without a
# handler Python would print that to stderr; with one, the host's logging setup decides.
logging.getLogger("sqlglot").addHandler(logging.NullHandler())

__all__ = [
    "ADVISORY",
    "SCHEMA_VERSION",
    "AdapterError",
    "Catalog",
    "CatalogError",
    "Column",
    "Confidence",
    "ConfigurationError",
    "ConnectionFailed",
    "CostEstimate",
    "Finding",
    "ForeignKey",
    "MissingDependencyError",
    "Options",
    "Report",
    "Scorer",
    "Severity",
    "SqlgamiError",
    "StageResult",
    "StageStatus",
    "Table",
    "UnsafeQueryError",
    "Verdict",
    "__version__",
]
