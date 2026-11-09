"""The public entry point: :class:`Scorer`."""

from __future__ import annotations

import os
from typing import Any, Sequence

from .catalog import Catalog, RequiredPredicate
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
    soft_delete_policy:
        ``advisory`` (default) or ``required``. With ``required``, reading a table
        without its soft-delete filter is a high-severity policy violation.
    calibrator:
        A fitted calibrator, or the path of a calibration file written by
        ``IsotonicCalibrator.save``. Without one, scores are provisional.
    required_predicates:
        Rules for filters that every query on certain tables must carry, such as
        access control. See :class:`sqlgami.RequiredPredicate`.
    snapshot_columns:
        ``{table: column}`` for periodic snapshot tables.
    generator:
        Optional function ``(question, schema_text, glossary, k) -> list of SQL`` that
        writes alternative queries for the consistency stage in ``full`` mode. sqlgami
        ships no model and calls none unless you pass this.
    allowed_functions:
        Names of functions you vouch for as read-only, such as your own user-defined
        functions. sqlgami sends a statement to the database only if every function it
        calls is a built-in known to be read-only or is named here.
    glossary:
        Business definitions, as text or a ``{term: meaning}`` dictionary. Values
        named here are not reported as filters the question never asked for.

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
        soft_delete_policy: str | None = None,
        calibrator: Calibrator | str | os.PathLike | None = None,
        options: Options | None = None,
        glossary: str | dict | None = None,
        required_predicates: list | None = None,
        snapshot_columns: dict[str, str] | None = None,
        default_project: str | None = None,
        generator: Any = None,
        allowed_functions: Sequence[str] | None = None,
    ) -> None:
        self.dialect = dialect_for(engine.lower())
        self.engine = "postgres" if self.dialect == "postgres" else engine.lower()
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
            if soft_delete_policy is not None:
                if soft_delete_policy not in ("advisory", "required"):
                    raise ConfigurationError("soft_delete_policy must be 'advisory' or 'required'")
                self.catalog.soft_delete_policy = soft_delete_policy
            if required_predicates:
                self.catalog.required_predicates.extend(RequiredPredicate.coerce(p) for p in required_predicates)
            if snapshot_columns:
                self.catalog.snapshot_columns.update({k.lower(): v for k, v in snapshot_columns.items()})
            if default_project is not None:
                self.catalog.default_project = default_project

        self.options = options or Options()
        if allowed_functions:
            self.options.allowed_functions = tuple(dict.fromkeys([*self.options.allowed_functions, *allowed_functions]))
        if self.adapter is not None and self.options.allowed_functions:
            self.adapter.allowed_functions = frozenset(self.adapter.allowed_functions) | {
                name.lower() for name in self.options.allowed_functions
            }
        if cost_ceiling is not None:
            self.options.cost_ceiling = cost_ceiling
        if sample_rows is not None:
            self.options.sample_rows = sample_rows
        if timeout_s is not None:
            self.options.timeout_s = timeout_s
        if isinstance(calibrator, (str, os.PathLike)):
            from .calibration import IsotonicCalibrator

            calibrator = IsotonicCalibrator.load(calibrator)
        self.calibrator = calibrator
        self.generator = generator
        if isinstance(glossary, dict):
            glossary = "\n".join(f"{term}: {meaning}" for term, meaning in glossary.items())
        self.glossary: str | None = glossary

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
        params: dict[str, Any] | None = None,
        alternatives: Sequence[str] | None = None,
    ) -> Report:
        """Check ``sql`` and return a :class:`Report`.

        ``alternatives`` are other queries written for the same question (by
        another model, another prompt, or other samples). When given, and the
        query can be run, each is run read-only and its result compared with the
        candidate's. In ``full`` mode a ``generator`` passed to the scorer is
        asked for them instead.

        ``sql`` may be a template with ``{name}`` placeholders. Pass ``params``
        to give their values; the SQL is then filled in the way ``str.format``
        would and can be sent to the database. Without values the template is
        still analysed, but never run.

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
            glossary=self.glossary,
            params=params,
            alternatives=alternatives,
            generator=self.generator,
        )

    def explain(self, sql: str, question: str | None = None, params: dict[str, Any] | None = None) -> Report:
        """Static checks only. Never contacts the database for the query itself."""
        return self.score(sql, question, mode="static", params=params)

    def dry_run(self, sql: str, question: str | None = None, params: dict[str, Any] | None = None) -> Report:
        """Static checks plus the engine's own planner. Does not run the query."""
        return self.score(sql, question, mode="dry", params=params)

    def sample(
        self,
        sql: str,
        question: str | None = None,
        cost_ceiling: float | None = None,
        params: dict[str, Any] | None = None,
        alternatives: Sequence[str] | None = None,
    ) -> Report:
        """Also runs the query read-only, if its estimated cost is under the ceiling.

        Pass ``alternatives`` to compare its result with other queries written for the
        same question.
        """
        return self.score(
            sql, question, mode="sample", cost_ceiling=cost_ceiling, params=params, alternatives=alternatives
        )

    # ----------------------------------------------------------- lifecycle

    def close(self) -> None:
        if self.adapter is not None and self._owns_adapter:
            self.adapter.close()

    def __enter__(self) -> "Scorer":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
