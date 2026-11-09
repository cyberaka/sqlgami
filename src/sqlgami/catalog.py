"""Schema information: tables, views, columns and keys.

A :class:`Catalog` can be filled three ways: from DDL text, from a plain
dictionary, or lazily from a live database through a loader. Lazy loading means
a catalog with hundreds of tables only fetches the handful a query touches.
"""

from __future__ import annotations

import difflib
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

import sqlglot
from sqlglot import exp

from .errors import CatalogError

TABLE = "table"
VIEW = "view"
MATERIALIZED_VIEW = "materialized_view"

# Dialects where a quoted identifier keeps its case and an unquoted one folds to lower case.
_QUOTED_CASE_SENSITIVE = {"postgres", "redshift"}

_DEFAULT_SCHEMA = {
    "sqlite": "main",
    "duckdb": "main",
    "postgres": "public",
}


def default_schema_for(dialect: str) -> str | None:
    return _DEFAULT_SCHEMA.get(dialect)


@dataclass
class Column:
    name: str
    type: str = ""
    nullable: bool | None = None  # None means not known
    # The complete set of distinct values, when someone has profiled the column.
    values: tuple[Any, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "type": self.type, "nullable": self.nullable}
        if self.values is not None:
            out["values"] = list(self.values)
        return out


@dataclass
class ForeignKey:
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]
    ref_schema: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": list(self.columns),
            "ref_schema": self.ref_schema,
            "ref_table": self.ref_table,
            "ref_columns": list(self.ref_columns),
        }


@dataclass
class Table:
    name: str
    schema: str | None = None
    columns: list[Column] = field(default_factory=list)
    primary_key: tuple[str, ...] = ()
    unique_keys: list[tuple[str, ...]] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    kind: str = TABLE
    definition: str | None = None  # the query text, for views
    view_columns: tuple[str, ...] = ()  # explicit column list of a view, if any
    row_count: int | None = None
    # False when key information could not be read at all (as opposed to "has no keys").
    keys_known: bool = True

    @property
    def is_view(self) -> bool:
        return self.kind in (VIEW, MATERIALIZED_VIEW)

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def all_unique_keys(self) -> list[tuple[str, ...]]:
        keys: list[tuple[str, ...]] = []
        if self.primary_key:
            keys.append(tuple(self.primary_key))
        for key in self.unique_keys:
            if key and tuple(key) not in keys:
                keys.append(tuple(key))
        return keys

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "name": self.name,
            "kind": self.kind,
            "columns": [c.to_dict() for c in self.columns],
            "primary_key": list(self.primary_key),
            "unique_keys": [list(k) for k in self.unique_keys],
            "foreign_keys": [fk.to_dict() for fk in self.foreign_keys],
            "definition": self.definition,
            "view_columns": list(self.view_columns),
            "row_count": self.row_count,
            "keys_known": self.keys_known,
        }


class CatalogLoader(Protocol):
    """Fetches schema information on demand, usually from a live database."""

    def list_tables(self) -> Iterable[tuple[str | None, str]]:
        """Every (schema, name) pair, tables and views alike. Must be cheap."""

    def load_table(self, schema: str | None, name: str) -> Table | None:
        """Full details of one table or view."""


class Catalog:
    """Tables and views known to sqlgami, plus a few policy hints.

    Parameters
    ----------
    dialect:
        The sqlglot dialect name of the engine (``sqlite``, ``duckdb``, ``postgres``...).
    default_schema:
        Schema used for names written without one. Defaults to the engine's usual one.
    search_path:
        Schemas tried, in order, for unqualified names. Defaults to ``[default_schema]``.
    tenant_column:
        Column name that isolates tenants. When set, every table that has this
        column is expected to be filtered on it.
    soft_delete_column:
        Column name that marks soft-deleted rows, used for an advisory check.
    """

    def __init__(
        self,
        dialect: str,
        *,
        default_schema: str | None = None,
        search_path: Iterable[str] | None = None,
        tenant_column: str | None = None,
        soft_delete_column: str | None = None,
        loader: CatalogLoader | None = None,
    ) -> None:
        self.dialect = dialect
        self.default_schema = default_schema or default_schema_for(dialect)
        self.search_path: list[str] = list(search_path) if search_path else (
            [self.default_schema] if self.default_schema else []
        )
        self.tenant_column = tenant_column
        self.soft_delete_column = soft_delete_column
        self._loader = loader
        self._tables: dict[tuple[str, str], Table] = {}
        self._index: dict[tuple[str, str], tuple[str | None, str]] | None = None
        self._missing: set[tuple[str, str]] = set()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ names

    @property
    def case_sensitive_when_quoted(self) -> bool:
        return self.dialect in _QUOTED_CASE_SENSITIVE

    def storage_key(self, name: str | None) -> str:
        """Key under which a stored object name is filed."""
        if name is None:
            return ""
        return name if self.case_sensitive_when_quoted else name.lower()

    def reference_key(self, name: str | None, quoted: bool = False) -> str:
        """Key for a name as written in a query."""
        if name is None:
            return ""
        if self.case_sensitive_when_quoted and quoted:
            return name
        return name.lower()

    # ---------------------------------------------------------------- filling

    def add(self, table: Table) -> Table:
        if table.schema is None:
            table.schema = self.default_schema
        with self._lock:
            key = (self.storage_key(table.schema), self.storage_key(table.name))
            self._tables[key] = table
            self._missing.discard(key)
            if self._index is not None:
                self._index[key] = (table.schema, table.name)
        return table

    def _ensure_index(self) -> dict[tuple[str, str], tuple[str | None, str]]:
        with self._lock:
            if self._index is None:
                index: dict[tuple[str, str], tuple[str | None, str]] = {}
                if self._loader is not None:
                    try:
                        listed = list(self._loader.list_tables())
                    except CatalogError:
                        raise
                    except Exception as exc:  # pragma: no cover - driver specific
                        raise CatalogError(f"could not list tables: {exc}") from exc
                    for schema, name in listed:
                        schema = schema or self.default_schema
                        index[(self.storage_key(schema), self.storage_key(name))] = (schema, name)
                for key, table in self._tables.items():
                    index[key] = (table.schema, table.name)
                self._index = index
            return self._index

    def refresh(self) -> None:
        """Forget everything fetched through the loader."""
        with self._lock:
            if self._loader is not None:
                self._tables.clear()
            self._index = None
            self._missing.clear()

    # ---------------------------------------------------------------- lookups

    def table_names(self) -> list[str]:
        return sorted(
            f"{schema}.{name}" if schema else name for schema, name in self._ensure_index().values()
        )

    def __len__(self) -> int:
        return len(self._ensure_index())

    def loaded_count(self) -> int:
        return len(self._tables)

    def _get(self, key: tuple[str, str]) -> Table | None:
        with self._lock:
            table = self._tables.get(key)
            if table is not None:
                return table
            if key in self._missing:
                return None
            located = self._ensure_index().get(key)
            if located is None or self._loader is None:
                self._missing.add(key)
                return None
            schema, name = located
            try:
                table = self._loader.load_table(schema, name)
            except CatalogError:
                raise
            except Exception as exc:  # pragma: no cover - driver specific
                raise CatalogError(f"could not load {schema}.{name}: {exc}") from exc
            if table is None:
                self._missing.add(key)
                return None
            if table.schema is None:
                table.schema = schema
            self._tables[key] = table
            return table

    def resolve(
        self,
        name: str,
        schema: str | None = None,
        *,
        quoted: bool = False,
        schema_quoted: bool = False,
        search_path: Iterable[str] | None = None,
    ) -> Table | None:
        """Find a table or view by the name written in a query."""
        name_key = self.reference_key(name, quoted)
        if schema:
            return self._get((self.reference_key(schema, schema_quoted), name_key))
        for candidate in (list(search_path) if search_path else self.search_path):
            table = self._get((self.storage_key(candidate), name_key))
            if table is not None:
                return table
        return None

    def column(self, table: Table, name: str, quoted: bool = False) -> Column | None:
        key = self.reference_key(name, quoted)
        for column in table.columns:
            if self.storage_key(column.name) == key:
                return column
        return None

    def column_keys(self, table: Table) -> set[str]:
        return {self.storage_key(c.name) for c in table.columns}

    def unique_key_sets(self, table: Table) -> list[frozenset[str]]:
        return [frozenset(self.storage_key(c) for c in key) for key in table.all_unique_keys()]

    def suggest_tables(self, name: str, limit: int = 3) -> list[str]:
        index = self._ensure_index()
        by_lower: dict[str, str] = {}
        for schema, table in index.values():
            by_lower.setdefault(table.lower(), f"{schema}.{table}" if schema else table)
        close = difflib.get_close_matches(name.lower(), list(by_lower), n=limit, cutoff=0.6)
        return [by_lower[c] for c in close]

    def suggest_columns(self, table: Table, name: str, limit: int = 3) -> list[str]:
        by_lower = {c.name.lower(): c.name for c in table.columns}
        close = difflib.get_close_matches(name.lower(), list(by_lower), n=limit, cutoff=0.6)
        return [by_lower[c] for c in close]

    # --------------------------------------------------------- serialisation

    def to_dict(self) -> dict[str, Any]:
        """Everything loaded so far. Tables not yet fetched through the loader are omitted."""
        return {
            "dialect": self.dialect,
            "default_schema": self.default_schema,
            "search_path": list(self.search_path),
            "tenant_column": self.tenant_column,
            "soft_delete_column": self.soft_delete_column,
            "tables": [t.to_dict() for t in self._tables.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], dialect: str | None = None) -> "Catalog":
        if not isinstance(data, dict):
            raise CatalogError("catalog data must be a dictionary")
        dialect = dialect or data.get("dialect")
        if not dialect:
            raise CatalogError("catalog data does not name a dialect")
        catalog = cls(
            dialect,
            default_schema=data.get("default_schema"),
            search_path=data.get("search_path"),
            tenant_column=data.get("tenant_column"),
            soft_delete_column=data.get("soft_delete_column"),
        )
        for entry in data.get("tables", []):
            try:
                columns = [
                    Column(
                        name=c["name"],
                        type=c.get("type") or "",
                        nullable=c.get("nullable"),
                        values=tuple(c["values"]) if c.get("values") is not None else None,
                    )
                    if isinstance(c, dict)
                    else Column(name=str(c))
                    for c in entry.get("columns", [])
                ]
                table = Table(
                    name=entry["name"],
                    schema=entry.get("schema"),
                    columns=columns,
                    primary_key=tuple(entry.get("primary_key") or ()),
                    unique_keys=[tuple(k) for k in entry.get("unique_keys") or []],
                    foreign_keys=[
                        ForeignKey(
                            columns=tuple(fk["columns"]),
                            ref_table=fk["ref_table"],
                            ref_columns=tuple(fk.get("ref_columns") or ()),
                            ref_schema=fk.get("ref_schema"),
                        )
                        for fk in entry.get("foreign_keys") or []
                    ],
                    kind=entry.get("kind", TABLE),
                    definition=entry.get("definition"),
                    view_columns=tuple(entry.get("view_columns") or ()),
                    row_count=entry.get("row_count"),
                    keys_known=entry.get("keys_known", True),
                )
            except (KeyError, TypeError) as exc:
                raise CatalogError(f"malformed table entry in catalog data: {exc!r}") from exc
            catalog.add(table)
        return catalog

    @classmethod
    def from_ddl(cls, ddl: str, dialect: str, **kwargs: Any) -> "Catalog":
        """Build a catalog from ``CREATE TABLE`` / ``CREATE VIEW`` / ``CREATE UNIQUE INDEX`` text."""
        catalog = cls(dialect, **kwargs)
        try:
            statements = sqlglot.parse(ddl, dialect=dialect)
        except sqlglot.errors.SqlglotError as exc:
            raise CatalogError(f"could not parse DDL: {exc}") from exc
        pending_indexes: list[exp.Create] = []
        for statement in statements:
            if not isinstance(statement, exp.Create):
                continue
            kind = (statement.args.get("kind") or "").upper()
            if kind == "TABLE":
                table = _table_from_create(statement, dialect)
                if table is not None:
                    catalog.add(table)
            elif kind == "VIEW":
                view = _view_from_create(statement, dialect)
                if view is not None:
                    catalog.add(view)
            elif kind == "INDEX" and statement.args.get("unique"):
                pending_indexes.append(statement)
        for statement in pending_indexes:
            index = statement.this
            target = index.args.get("table") if isinstance(index, exp.Index) else None
            params = index.args.get("params") if isinstance(index, exp.Index) else None
            if target is None or params is None:
                continue
            table = catalog.resolve(
                target.name,
                target.db or None,
                quoted=_quoted(target.this),
                schema_quoted=_quoted(target.args.get("db")),
            )
            if table is None:
                continue
            columns = []
            for item in params.args.get("columns") or []:
                inner = item.this if isinstance(item, exp.Ordered) else item
                if isinstance(inner, exp.Column):
                    columns.append(inner.name)
                else:
                    columns = []
                    break
            if columns and not params.args.get("where") and not index.args.get("where"):
                table.unique_keys.append(tuple(columns))
        return catalog


def _quoted(identifier: Any) -> bool:
    return bool(isinstance(identifier, exp.Identifier) and identifier.args.get("quoted"))


def _table_from_create(statement: exp.Create, dialect: str) -> Table | None:
    schema_node = statement.this
    if not isinstance(schema_node, exp.Schema):
        # CREATE TABLE ... AS SELECT: the columns are not written down.
        target = schema_node if isinstance(schema_node, exp.Table) else None
        if target is None:
            return None
        return Table(name=target.name, schema=target.db or None, keys_known=False)
    target = schema_node.this
    if not isinstance(target, exp.Table):
        return None
    table = Table(name=target.name, schema=target.db or None)
    for item in schema_node.expressions:
        if isinstance(item, exp.ColumnDef):
            kind = item.args.get("kind")
            column = Column(name=item.name, type=kind.sql(dialect=dialect) if kind else "", nullable=True)
            for constraint in item.constraints:
                ckind = constraint.kind
                if isinstance(ckind, exp.PrimaryKeyColumnConstraint):
                    table.primary_key = (column.name,)
                    column.nullable = False
                elif isinstance(ckind, exp.NotNullColumnConstraint):
                    if not ckind.args.get("allow_null"):
                        column.nullable = False
                elif isinstance(ckind, exp.UniqueColumnConstraint):
                    table.unique_keys.append((column.name,))
                elif isinstance(ckind, exp.Reference):
                    fk = _foreign_key((column.name,), ckind)
                    if fk is not None:
                        table.foreign_keys.append(fk)
            table.columns.append(column)
        else:
            _apply_table_constraint(table, item)
    for name in table.primary_key:
        for column in table.columns:
            if column.name.lower() == name.lower():
                column.nullable = False
    return table


def _apply_table_constraint(table: Table, item: exp.Expression) -> None:
    if isinstance(item, exp.Constraint):
        for inner in item.expressions:
            _apply_table_constraint(table, inner)
        return
    if isinstance(item, exp.PrimaryKey):
        names = _names(item.expressions)
        if names:
            table.primary_key = names
    elif isinstance(item, exp.UniqueColumnConstraint):
        target = item.this
        names = _names(target.expressions) if isinstance(target, exp.Schema) else _names([target])
        if names:
            table.unique_keys.append(names)
    elif isinstance(item, exp.ForeignKey):
        reference = item.args.get("reference")
        names = _names(item.expressions)
        if names and isinstance(reference, exp.Reference):
            fk = _foreign_key(names, reference)
            if fk is not None:
                table.foreign_keys.append(fk)


def _names(items: Iterable[Any]) -> tuple[str, ...]:
    names: list[str] = []
    for item in items or []:
        if isinstance(item, exp.Ordered):
            item = item.this
        if isinstance(item, (exp.Identifier, exp.Column)):
            names.append(item.name)
        elif item is not None and getattr(item, "name", ""):
            names.append(item.name)
    return tuple(names)


def _foreign_key(columns: tuple[str, ...], reference: exp.Reference) -> ForeignKey | None:
    target = reference.this
    ref_columns: tuple[str, ...] = ()
    if isinstance(target, exp.Schema):
        ref_columns = _names(target.expressions)
        target = target.this
    if not isinstance(target, exp.Table):
        return None
    return ForeignKey(
        columns=columns,
        ref_table=target.name,
        ref_columns=ref_columns,
        ref_schema=target.db or None,
    )


def _view_from_create(statement: exp.Create, dialect: str) -> Table | None:
    target = statement.this
    view_columns: tuple[str, ...] = ()
    if isinstance(target, exp.Schema):
        view_columns = _names(target.expressions)
        target = target.this
    if not isinstance(target, exp.Table) or statement.expression is None:
        return None
    properties = statement.args.get("properties")
    materialized = bool(properties and properties.find(exp.MaterializedProperty))
    return Table(
        name=target.name,
        schema=target.db or None,
        kind=MATERIALIZED_VIEW if materialized else VIEW,
        definition=statement.expression.sql(dialect=dialect),
        view_columns=view_columns,
    )


def view_query_from_sql(text: str, dialect: str) -> tuple[str, tuple[str, ...]] | None:
    """Pull the query (and any explicit column list) out of a ``CREATE VIEW`` statement.

    Accepts a bare query too, and returns it unchanged.
    """
    try:
        statement = sqlglot.parse_one(text, dialect=dialect)
    except sqlglot.errors.SqlglotError:
        return None
    if isinstance(statement, exp.Create):
        view = _view_from_create(statement, dialect)
        if view is None or view.definition is None:
            return None
        return view.definition, view.view_columns
    if isinstance(statement, exp.Query):
        return statement.sql(dialect=dialect), ()
    return None
