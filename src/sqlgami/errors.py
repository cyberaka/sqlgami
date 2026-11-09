"""Exceptions raised by sqlgami.

Bad SQL is never an exception: it comes back as a normal report. Exceptions are
reserved for misuse, configuration problems and infrastructure failures, so a
host application can catch :class:`SqlgamiError` and carry on.
"""

from __future__ import annotations


class SqlgamiError(Exception):
    """Base class for every error sqlgami raises."""


class ConfigurationError(SqlgamiError):
    """The scorer, catalog or adapter was set up incorrectly."""


class MissingDependencyError(ConfigurationError):
    """An optional driver is needed but not installed."""


class CatalogError(SqlgamiError):
    """Schema information is missing, malformed or could not be loaded."""


class AdapterError(SqlgamiError):
    """A database adapter failed for a reason unrelated to the SQL under review."""


class ConnectionFailed(AdapterError):
    """The database could not be reached or opened."""


class UnsafeQueryError(SqlgamiError):
    """sqlgami refused to send a statement to the database because it is not read-only."""
