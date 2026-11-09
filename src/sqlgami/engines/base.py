"""The interface every database adapter implements.

An adapter never sends anything but a single read-only query to the database:
the candidate SQL (after :func:`assert_read_only`), ``EXPLAIN`` of it, schema
lookups, and small probe queries that sqlgami builds itself.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .. import safety
from ..analysis import _mutation_kinds
from ..catalog import Catalog, CatalogLoader, Table
from ..errors import UnsafeQueryError
from ..report import CostEstimate, Finding

# Why a statement failed against the database.
INVALID = "invalid"  # the engine says the SQL itself is wrong
TIMEOUT = "timeout"
OTHER = "other"  # permissions, resources, connectivity: says nothing about the SQL


@dataclass
class DryRunResult:
    ok: bool
    error: str | None = None
    error_kind: str | None = None
    cost: CostEstimate | None = None
    can_execute_safely: bool | None = None
    plan: Any = None
    findings: list[Finding] = field(default_factory=list)


@dataclass
class ExecutionResult:
    ok: bool
    error: str | None = None
    error_kind: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False
    duration_ms: float | None = None


def assert_read_only(
    sql: str,
    dialect: str,
    allowed_functions: Iterable[str] = (),
    *,
    check_functions: bool = True,
) -> exp.Expression:
    """Parse ``sql`` and refuse it unless it is exactly one read-only query.

    With ``check_functions`` (the default) every function it calls must also be on the
    engine's allow-list or in ``allowed_functions``: see :mod:`sqlgami.safety`.
    """
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except SqlglotError as exc:
        raise UnsafeQueryError(f"statement could not be parsed, so it was not sent: {exc}") from exc
    if len(statements) != 1:
        raise UnsafeQueryError("exactly one statement may be sent to the database")
    statement = statements[0]
    while isinstance(statement, exp.Subquery) and isinstance(statement.this, exp.Query):
        statement = statement.this
    kinds = _mutation_kinds(statement)
    if kinds or not isinstance(statement, exp.Query):
        what = ", ".join(kinds) or type(statement).__name__.upper()
        raise UnsafeQueryError(f"only read-only queries are sent to the database (found {what})")
    if check_functions:
        review = safety.review(sql, statement, dialect, allowed_functions)
        if review.refused:
            raise UnsafeQueryError(
                f"the query reaches outside the tables ({', '.join(review.refused)}), so it was not sent"
            )
        if review.unlisted:
            raise UnsafeQueryError(
                f"the query calls a function not known to be read-only ({', '.join(review.unlisted)}), "
                "so it was not sent; pass allowed_functions to vouch for it"
            )
    return statement


class Adapter(abc.ABC):
    """One database engine."""

    engine: str = ""
    dialect: str = ""
    cost_unit: str = "rows"
    # Functions the host vouches for, beyond the engine's built-in allow-list.
    allowed_functions: frozenset[str] = frozenset()

    @abc.abstractmethod
    def loader(self) -> CatalogLoader:
        """A loader that reads schema information from the live database."""

    def catalog(self, **hints: Any) -> Catalog:
        """A lazily filled catalog backed by the live database."""
        return Catalog(self.dialect, loader=self.loader(), **hints)

    @abc.abstractmethod
    def dry_run(self, sql: str) -> DryRunResult:
        """Ask the engine to plan the query without running it."""

    @abc.abstractmethod
    def execute(self, sql: str, *, max_rows: int, timeout_s: float) -> ExecutionResult:
        """Run the candidate query read-only and return at most ``max_rows`` rows."""

    @abc.abstractmethod
    def query(self, expression: exp.Expression, *, max_rows: int = 1000, timeout_s: float = 10.0) -> list[tuple]:
        """Run a small query built by sqlgami itself. Raises :class:`AdapterError` on failure."""

    def table_ref(self, table: Table) -> exp.Table:
        """An AST node naming ``table`` in this engine."""
        return exp.Table(
            this=exp.to_identifier(table.name, quoted=True),
            db=exp.to_identifier(table.schema, quoted=True) if table.schema else None,
        )

    def row_count(self, table: Table) -> int | None:
        """A cheap estimate of the number of rows, or ``None``."""
        return table.row_count

    def version(self) -> str | None:
        """The version of the engine being talked to, for the report."""
        return None

    def function_exists(self, name: str) -> bool | None:
        """Whether the database has a function of this name, or ``None`` if that cannot be told."""
        return None

    def close(self) -> None:
        """Release anything the adapter opened itself. Borrowed connections are left alone."""

    def __enter__(self) -> "Adapter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def rows_as_tuples(rows: Sequence[Any]) -> list[tuple]:
    return [tuple(row) for row in rows]
