"""The public entry point: :class:`Scorer`."""

from __future__ import annotations

from typing import Any

from .catalog import Catalog
from .engines import dialect_for, get_adapter
from .engines.base import Adapter
from .errors import ConfigurationError
from .pipeline import MODES, Options, run
from .report import Report
from .scoring import Calibrator


class Scorer:
    """Scores SQL written for one database.

    Parameters
    ----------
    engine:
        ``"sqlite"``, ``"duckdb"``, ``"postgres"`` or ``"bigquery"``.
    db:
        A live connection, a path to a database file, or an :class:`Adapter`.
        Optional. Passing one is what allows sqlgami to contact the database;
        without it nothing leaves the process and only static checks run.
    catalog:
        Schema information, as a :class:`Catalog`, a dictionary in the
        ``Catalog.to_dict`` shape, or DDL text. Optional when ``db`` is given,
        in which case the schema is read from the database on demand.
    mode:
        Default depth of checking: ``static``, ``dry``, ``sample`` or ``full``.
        Defaults to ``dry`` with a connection and ``static`` without.
    cost_ceiling:
        The most a sample run may cost, in the engine's unit (rows scanned for
        SQLite and DuckDB). Above it the query is not run.
    tenant_column, soft_delete_column:
        Policy hints, applied to the catalog.

    A scorer holds no global state and configures no logging. It is safe to
    create several. One scorer should be used from one thread at a time.
    """

    def __init__(
        self,
        engine: str,
        db: Any = None,
        catalog: Catalog | dict | str | None = None,
        *,
        mode: str | None = None,
        cost_ceiling: float | None = None,
        sample_rows: int | None = None,
        timeout_s: float | None = None,
        tenant_column: str | None = None,
        soft_delete_column: str | None = None,
        calibrator: Calibrator | None = None,
        options: Options | None = None,
    ) -> None:
        self.engine = engine.lower()
        self.dialect = dialect_for(self.engine)
        self._owns_adapter = False
        if db is None:
            self.adapter: Adapter | None = None
        elif isinstance(db, Adapter):
            self.adapter = db
        else:
            self.adapter = get_adapter(self.engine, db)
            self._owns_adapter = True

        hints = {"tenant_column": tenant_column, "soft_delete_column": soft_delete_column}
        if catalog is None:
            self.catalog: Catalog | None = (
                self.adapter.catalog(**hints) if self.adapter is not None else None
            )
        elif isinstance(catalog, Catalog):
            self.catalog = catalog
        elif isinstance(catalog, dict):
            self.catalog = Catalog.from_dict(catalog, self.dialect)
        elif isinstance(catalog, str):
            self.catalog = Catalog.from_ddl(catalog, self.dialect)
        else:
            raise ConfigurationError("catalog must be a Catalog, a dictionary, DDL text or None")
        if self.catalog is not None:
            if tenant_column is not None:
                self.catalog.tenant_column = tenant_column
            if soft_delete_column is not None:
                self.catalog.soft_delete_column = soft_delete_column

        self.options = options or Options()
        if cost_ceiling is not None:
            self.options.cost_ceiling = cost_ceiling
        if sample_rows is not None:
            self.options.sample_rows = sample_rows
        if timeout_s is not None:
            self.options.timeout_s = timeout_s
        self.calibrator = calibrator

        default = "dry" if self.adapter is not None else "static"
        self.mode = self._check_mode(mode or default)

    def _check_mode(self, mode: str) -> str:
        if mode not in MODES:
            raise ConfigurationError(f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
        if mode != "static" and self.adapter is None:
            raise ConfigurationError(
                f"mode {mode!r} needs a database connection; pass db=..., or use mode='static'"
            )
        return mode

    # ---------------------------------------------------------------- API

    def score(
        self,
        sql: str,
        question: str | None = None,
        *,
        mode: str | None = None,
        cost_ceiling: float | None = None,
    ) -> Report:
        """Check ``sql`` and return a :class:`Report`.

        Bad SQL never raises: it comes back as a report. Exceptions (all
        subclasses of ``SqlgamiError``) mean the scorer was misused or the
        database could not be reached.
        """
        if not isinstance(sql, str):
            raise ConfigurationError("sql must be a string")
        chosen = self._check_mode(mode or self.mode)
        options = self.options
        if cost_ceiling is not None:
            options = Options(**{**options.__dict__, "cost_ceiling": cost_ceiling})
        return run(
            sql,
            engine=self.engine,
            dialect=self.dialect,
            mode=chosen,
            catalog=self.catalog,
            adapter=self.adapter,
            question=question,
            options=options,
            calibrator=self.calibrator,
        )

    def explain(self, sql: str, question: str | None = None) -> Report:
        """Static checks only. Never contacts the database for the query itself."""
        return self.score(sql, question, mode="static")

    def dry_run(self, sql: str, question: str | None = None) -> Report:
        """Static checks plus the engine's own planner. Does not run the query."""
        return self.score(sql, question, mode="dry")

    def sample(self, sql: str, question: str | None = None, cost_ceiling: float | None = None) -> Report:
        """Also runs the query read-only, if its estimated cost is under the ceiling."""
        return self.score(sql, question, mode="sample", cost_ceiling=cost_ceiling)

    # ----------------------------------------------------------- lifecycle

    def close(self) -> None:
        if self.adapter is not None and self._owns_adapter:
            self.adapter.close()

    def __enter__(self) -> "Scorer":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
