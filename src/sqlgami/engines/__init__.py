"""Database adapters. Each one is imported only when asked for, so its driver stays optional."""

from __future__ import annotations

from typing import Any

from ..errors import ConfigurationError
from .base import Adapter, DryRunResult, ExecutionResult, assert_read_only

__all__ = ["Adapter", "DryRunResult", "ExecutionResult", "assert_read_only", "get_adapter", "SUPPORTED"]

# engine name -> (module, class, sqlglot dialect)
_ADAPTERS = {
    "sqlite": ("sqlgami.engines.sqlite", "SQLiteAdapter", "sqlite"),
    "duckdb": ("sqlgami.engines.duckdb", "DuckDBAdapter", "duckdb"),
}

# Engines sqlgami can analyse statically even where no adapter exists yet.
_DIALECTS = {
    "sqlite": "sqlite",
    "duckdb": "duckdb",
    "postgres": "postgres",
    "postgresql": "postgres",
    "bigquery": "bigquery",
}

SUPPORTED = sorted(_ADAPTERS)


def dialect_for(engine: str) -> str:
    try:
        return _DIALECTS[engine.lower()]
    except KeyError:
        raise ConfigurationError(
            f"unknown engine {engine!r}; expected one of {', '.join(sorted(_DIALECTS))}"
        ) from None


def get_adapter(engine: str, db: Any) -> Adapter:
    """Build the adapter for ``engine`` around a connection or a path."""
    key = engine.lower()
    if key not in _ADAPTERS:
        raise ConfigurationError(
            f"no database adapter for {engine!r} yet; adapters exist for {', '.join(SUPPORTED)}. "
            "Static analysis still works with a catalog and no connection."
        )
    module_name, class_name, _ = _ADAPTERS[key]
    import importlib

    module = importlib.import_module(module_name)
    return getattr(module, class_name)(db)
