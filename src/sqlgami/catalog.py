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
TABLE_FUNCTION = "table_function"  # CREATE TABLE FUNCTION f(p ...) AS SELECT ...: a view with parameters
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
    # Set once a look at the data showed too many distinct values to list.
    high_cardinality: bool = False

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
    # Columns the table is partitioned by. A query that does not filter on them scans everything.
    partition_columns: tuple[str, ...] = ()
    # For a periodic snapshot table (one row per thing per snapshot): the snapshot date column.
    snapshot_column: str | None = None
    # A table function's declared parameters, in order: (name, declared type).
    parameters: tuple[tuple[str, str], ...] = ()

    @property
    def is_view(self) -> bool:
        return self.kind in (VIEW, MATERIALIZED_VIEW)

    @property
    def is_table_function(self) -> bool:
        return self.kind == TABLE_FUNCTION

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
            "partition_columns": list(self.partition_columns),
            "snapshot_column": self.snapshot_column,
        }


@dataclass
class RequiredPredicate:
    """A rule that every query reading certain tables must filter on certain columns.

    ``match`` says how:

    * ``any``: at least one of the columns is pinned to a value;
    * ``all``: every one of them is;
    * ``or_chain``: the query holds one ``c1 = v OR c2 = v OR ...`` condition covering
      every listed column the table has. This is the shape of a hierarchy
      access filter ("this manager at any level").
    """

    name: str
    columns: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()  # empty means every table that has any of the columns
    match: str = "any"
    severity: str = "high"
    description: str = ""
    # For ``or_chain``: alternative column sets, any one of which satisfies the rule when an OR
    # chain covers all of it (a reporting hierarchy OR a functional one). ``columns`` is then
    # the union, kept for callers that read it.
    groups: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        self.groups = tuple(tuple(g) for g in self.groups)
        self.tables = tuple(self.tables)
        if self.groups and not self.columns:
            self.columns = tuple(dict.fromkeys(c for g in self.groups for c in g))
        self.columns = tuple(self.columns)
        if self.match not in ("any", "all", "or_chain"):
            raise CatalogError(f"required predicate {self.name!r}: unknown match {self.match!r}")
        if self.groups and self.match != "or_chain":
            raise CatalogError(f"required predicate {self.name!r}: groups are only for match 'or_chain'")
        if not self.columns:
            raise CatalogError(f"required predicate {self.name!r} names no columns")

    def column_groups(self) -> tuple[tuple[str, ...], ...]:
        return self.groups or (self.columns,)

    def applies_to(self, table: "Table") -> bool:
        if not self.tables:
            return True
        qualified = table.qualified_name.lower()
        names = {table.name.lower(), qualified}
        parts = qualified.split(".")
        if len(parts) >= 3:
            names.add(".".join(parts[-2:]))  # `project.dataset.table` is dataset.table to a rule written without the project
        return any(t.lower().strip("`") in names for t in self.tables)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": list(self.columns),
            "tables": list(self.tables),
            "match": self.match,
            "severity": self.severity,
            "description": self.description,
            "groups": [list(g) for g in self.groups],
        }

    @classmethod
    def coerce(cls, value: Any) -> "RequiredPredicate":
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            try:
                return cls(**value)
            except TypeError as exc:
                raise CatalogError(f"malformed required predicate: {exc}") from exc
        raise CatalogError("a required predicate must be a RequiredPredicate or a dictionary")


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
        Column name that marks soft-deleted rows.
    soft_delete_policy:
        ``advisory`` (the default): reading such a table without looking at the
        marker is a medium-severity finding. ``required``: your rule is that
        deleted rows are excluded unless a question asks for them, so the same
        omission is a high-severity policy violation.
    required_predicates:
        :class:`RequiredPredicate` rules (or dictionaries in the same shape), for
        filters such as access control that every query on a table must carry.
    snapshot_columns:
        ``{table name: column}`` for periodic snapshot tables, where each thing
        has one row per snapshot and a query must choose which snapshots it reads.
    default_project:
        BigQuery only: the project assumed for names written as ``dataset.table``.
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
        default_project: str | None = None,
        required_predicates: Iterable[Any] | None = None,
        snapshot_columns: dict[str, str] | None = None,
        soft_delete_policy: str = "advisory",
    ) -> None:
        self.dialect = dialect
        # BigQuery: the project assumed for names written as dataset.table.
        self.default_project = default_project
        self.required_predicates: list[RequiredPredicate] = [
            RequiredPredicate.coerce(p) for p in (required_predicates or [])
        ]
        # table name (bare or qualified) -> its snapshot date column
        self.snapshot_columns: dict[str, str] = {k.lower(): v for k, v in (snapshot_columns or {}).items()}
        self.default_schema = default_schema or default_schema_for(dialect)
        self.search_path: list[str] = list(search_path) if search_path else (
            [self.default_schema] if self.default_schema else []
        )
        self.tenant_column = tenant_column
        self.soft_delete_column = soft_delete_column
        # Host names for catalogue tables (```proj-x.hr.snapshot``` -> hr.snapshot), declared, never guessed.
        self.table_aliases: dict[str, str] = {}
        # Scalar SQL and JavaScript functions the host declared (CREATE FUNCTION), by qualified name.
        self.scalar_functions: dict[str, ScalarFunction] = {}
        if soft_delete_policy not in ("advisory", "required"):
            raise CatalogError("soft_delete_policy must be 'advisory' or 'required'")
        self.soft_delete_policy = soft_delete_policy
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
        """Key under which a stored table or schema name is filed."""
        if name is None:
            return ""
        if self.dialect == "bigquery" or self.case_sensitive_when_quoted:
            return name  # BigQuery dataset and table names are case-sensitive
        return name.lower()

    def reference_key(self, name: str | None, quoted: bool = False) -> str:
        """Key for a table or schema name as written in a query."""
        if name is None:
            return ""
        if self.dialect == "bigquery":
            return name
        if self.case_sensitive_when_quoted and quoted:
            return name
        return name.lower()

    def column_key(self, name: str | None) -> str:
        """Key under which a stored column name is filed."""
        if name is None:
            return ""
        return name if self.case_sensitive_when_quoted else name.lower()

    def column_reference_key(self, name: str | None, quoted: bool = False) -> str:
        """Key for a column name as written in a query. BigQuery column names ignore case."""
        if name is None:
            return ""
        if self.case_sensitive_when_quoted and quoted:
            return name
        return name.lower()

    def written(self, name: str) -> str:
        """How a stored name must be written in a query: quoted when its case would otherwise be lost."""
        if self.case_sensitive_when_quoted and name != name.lower():
            return '"' + name.replace('"', '""') + '"'
        return name

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
        if self.table_aliases:
            written = ".".join(p for p in (schema, name) if p).strip("`").lower()
            target = self.table_aliases.get(written)
            if target is not None:
                t_schema, _, t_name = target.rpartition(".")
                found = self._get((self.storage_key(t_schema) if t_schema else self.default_schema or "", self.reference_key(t_name)))
                if found is not None:
                    return found
        if self.dialect == "bigquery" and (not schema or "." not in schema):
            return self._resolve_bigquery(name_key, schema)
        if schema:
            found = self._get((self.reference_key(schema, schema_quoted), name_key))
            if found is None and self.dialect == "bigquery" and "." in schema and not self.default_project:
                # project.dataset.table against a catalogue declared without projects: the
                # catalogue is project-agnostic, so the dataset decides
                project, _, dataset = schema.partition(".")
                if not any("." in key[0] for key in self._ensure_index()):
                    found = self._get((self.reference_key(dataset, schema_quoted), name_key))
            return found
        for candidate in (list(search_path) if search_path else self.search_path):
            table = self._get((self.storage_key(candidate), name_key))
            if table is not None:
                return table
        return None

    def _resolve_bigquery(self, name_key: str, dataset: str | None) -> Table | None:
        """``dataset.table`` or a bare table name: fill in the project (and dataset) if unambiguous."""
        if dataset and self.default_project:
            found = self._get((f"{self.default_project}.{dataset}", name_key))
            if found is not None:
                return found
        if not dataset:
            for candidate in self.search_path:
                found = self._get((candidate, name_key))
                if found is not None:
                    return found
        matches = [
            key
            for key in self._ensure_index()
            if key[1] == name_key and (not dataset or key[0] == dataset or key[0].endswith("." + dataset))
        ]
        return self._get(matches[0]) if len(matches) == 1 else None

    def scalar_function(self, name: str) -> "ScalarFunction | None":
        """A declared scalar function by the name a query writes (```proj.ds.g```, ``ds.g`` or
        ``g`` when unambiguous); None when the host declared none of that name."""
        key = name.strip("`").lower()
        found = self.scalar_functions.get(key)
        if found is not None:
            return found
        parts = key.split(".")
        if len(parts) >= 3:
            found = self.scalar_functions.get(".".join(parts[-2:]))
            if found is not None:
                return found
        matches = [f for k, f in self.scalar_functions.items() if k.split(".")[-1] == parts[-1] and (len(parts) == 1 or k.endswith("." + ".".join(parts[-2:])) or k.endswith(key))]
        return matches[0] if len(matches) == 1 else None

    def column(self, table: Table, name: str, quoted: bool = False) -> Column | None:
        key = self.column_reference_key(name, quoted)
        for column in table.columns:
            if self.column_key(column.name) == key:
                return column
        return None

    def view_tree(self, table: Table, dialect: str) -> tuple[Any, tuple[str, ...]] | None:
        """A view's query parsed once per dialect, with its declared column list; callers copy
        the tree before changing it."""
        cache = self.__dict__.setdefault("_view_trees", {})
        key = (id(table), dialect, table.definition)
        if key not in cache:
            parsed = view_query_from_sql(table.definition or "", dialect)
            if parsed is None:
                cache[key] = None
            else:
                query_sql, view_columns = parsed
                try:
                    cache[key] = (sqlglot.parse_one(query_sql, dialect=dialect), tuple(view_columns))
                except Exception:
                    cache[key] = None
        return cache[key]

    def sqlglot_columns(self, table: Table, dialect: str) -> dict[str, str]:
        """``{column key: sqlglot type name}`` for a table, computed once per table and dialect:
        the same statement-independent mapping is needed on every call."""
        from .types import sqlglot_type_name

        cache = self.__dict__.setdefault("_sqlglot_columns", {})
        key = (id(table), dialect, len(table.columns))
        found = cache.get(key)
        if found is None:
            found = {self.column_key(c.name): sqlglot_type_name(c.type, dialect) for c in table.columns}
            cache[key] = found
        return found

    def column_keys(self, table: Table) -> set[str]:
        return {self.column_key(c.name) for c in table.columns}

    def unique_key_sets(self, table: Table) -> list[frozenset[str]]:
        return [frozenset(self.column_key(c) for c in key) for key in table.all_unique_keys()]

    def declare_unique(self, table: str, columns: Iterable[str], schema: str | None = None) -> None:
        """Tell sqlgami that these columns identify a row, where the database does not enforce it.

        Warehouses often have no enforced keys. Declaring them lets checks about
        repeated rows and single-row subqueries reason properly.
        """
        found = self.resolve(table, schema, quoted=True) or self.resolve(table, schema)
        if found is None:
            raise CatalogError(f"cannot declare a key on unknown table {table!r}")
        names = []
        for name in columns:
            column = self.column(found, name)
            if column is None:
                raise CatalogError(f"table {found.qualified_name} has no column {name!r}")
            names.append(column.name)
        key = tuple(names)
        if key and key != tuple(found.primary_key) and key not in found.unique_keys:
            found.unique_keys.append(key)
        found.keys_known = True

    def snapshot_column_of(self, table: Table) -> Column | None:
        """The snapshot date column of a periodic snapshot table, if one is declared for it."""
        name = table.snapshot_column
        if not name:
            name = self.snapshot_columns.get(table.qualified_name.lower()) or self.snapshot_columns.get(
                table.name.lower()
            )
        return self.column(table, name) if name else None

    def suggest_tables(self, name: str, limit: int = 3) -> list[str]:
        index = self._ensure_index()
        by_lower: dict[str, str] = {}
        for schema, table in index.values():
            by_lower.setdefault(table.lower(), ".".join(self.written(p) for p in (schema, table) if p))
        close = difflib.get_close_matches(name.lower(), list(by_lower), n=limit, cutoff=0.6)
        return [by_lower[c] for c in close]

    def suggest_columns(self, table: Table, name: str, limit: int = 3) -> list[str]:
        by_lower = {c.name.lower(): self.written(c.name) for c in table.columns}
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
            "soft_delete_policy": self.soft_delete_policy,
            "default_project": self.default_project,
            "required_predicates": [p.to_dict() for p in self.required_predicates],
            "snapshot_columns": dict(self.snapshot_columns),
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
            soft_delete_policy=data.get("soft_delete_policy") or "advisory",
            default_project=data.get("default_project"),
            required_predicates=data.get("required_predicates"),
            snapshot_columns=data.get("snapshot_columns"),
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
                    partition_columns=tuple(entry.get("partition_columns") or ()),
                    snapshot_column=entry.get("snapshot_column"),
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
                if statement.find(exp.PartitionedOfProperty) is not None:
                    continue  # a partition of another table is storage, not a table of its own
                table = _table_from_create(statement, dialect)
                if table is not None:
                    catalog.add(table)
            elif kind == "VIEW":
                view = _view_from_create(statement, dialect)
                if view is not None:
                    catalog.add(view)
            elif kind == "FUNCTION":
                function = _table_function_from_create(statement, dialect)
                if function is not None:
                    catalog.add(function)
                else:
                    scalar = _scalar_function_from_create(statement, dialect)
                    if scalar is not None:
                        catalog.scalar_functions[scalar.qualified_name.lower()] = scalar
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
                schema_name_of(target, dialect),
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


def schema_name_of(node: exp.Table, dialect: str) -> str | None:
    """The schema a table node names. For BigQuery that is ``project.dataset`` as one string."""
    db = node.db or None
    if dialect == "bigquery" and db and node.catalog:
        return f"{node.catalog}.{db}"
    return db


def _partition_columns(statement: exp.Create) -> tuple[str, ...]:
    properties = statement.args.get("properties")
    if properties is None:
        return ()
    names: list[str] = []
    for prop in properties.find_all(exp.PartitionedByProperty):
        for node in prop.find_all(exp.Column, exp.Identifier):
            name = node.name
            if name and name not in names and isinstance(node, (exp.Column, exp.Identifier)):
                if isinstance(node, exp.Identifier) and isinstance(node.parent, exp.Column):
                    continue
                names.append(name)
    return tuple(names)


def _table_from_create(statement: exp.Create, dialect: str) -> Table | None:
    schema_node = statement.this
    if not isinstance(schema_node, exp.Schema):
        # CREATE TABLE ... AS SELECT: the columns are not written down.
        target = schema_node if isinstance(schema_node, exp.Table) else None
        if target is None:
            return None
        return Table(name=target.name, schema=schema_name_of(target, dialect), keys_known=False)
    target = schema_node.this
    if not isinstance(target, exp.Table):
        return None
    table = Table(name=target.name, schema=schema_name_of(target, dialect))
    known = {c.name.lower() for c in schema_node.expressions if isinstance(c, exp.ColumnDef)}
    table.partition_columns = tuple(c for c in _partition_columns(statement) if c.lower() in known)
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
        schema=schema_name_of(target, dialect),
        kind=MATERIALIZED_VIEW if materialized else VIEW,
        definition=statement.expression.sql(dialect=dialect),
        view_columns=view_columns,
    )


@dataclass
class ScalarFunction:
    """``CREATE FUNCTION ds.g(x INT64) RETURNS INT64 AS (expr)``, or a JavaScript one."""

    name: str
    schema: str | None
    parameters: tuple[tuple[str, str], ...] = ()
    body: str | None = None  # the SQL expression, for a SQL function; None for JavaScript
    language: str = "sql"

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


def _scalar_function_from_create(statement: exp.Create, dialect: str) -> ScalarFunction | None:
    target = statement.this
    if not isinstance(target, exp.UserDefinedFunction) or not isinstance(target.this, exp.Table):
        return None
    parameters = tuple(
        (item.name.lower(), item.args["kind"].sql(dialect=dialect) if item.args.get("kind") is not None else "")
        for item in (target.expressions or []) if isinstance(item, exp.ColumnDef)
    )
    properties = statement.args.get("properties")
    language = "sql"
    if properties is not None:
        for prop in properties.expressions:
            if isinstance(prop, exp.LanguageProperty):
                language = prop.name.lower()
    body = statement.expression
    if language != "sql" or body is None or isinstance(body, exp.RawString):
        return ScalarFunction(target.this.name, schema_name_of(target.this, dialect), parameters, None, language if language != "sql" else "js")
    while isinstance(body, exp.Paren):
        body = body.this
    return ScalarFunction(target.this.name, schema_name_of(target.this, dialect), parameters, body.sql(dialect=dialect), "sql")


def _table_function_from_create(statement: exp.Create, dialect: str) -> Table | None:
    """``CREATE TABLE FUNCTION ds.f(p INT64, …) AS SELECT …`` (BigQuery's own DDL wraps the body in
    parentheses): a view with parameters. A scalar function (``RETURNS … AS (expr)``) is not a
    table and is left out."""
    target = statement.this
    expression = statement.expression
    while isinstance(expression, (exp.Subquery, exp.Paren)) and isinstance(expression.this, exp.Expression):
        expression = expression.this
    if not isinstance(target, exp.UserDefinedFunction) or not isinstance(expression, exp.Query):
        return None
    statement = statement.copy()
    statement.set("expression", expression)
    name = target.this
    if not isinstance(name, exp.Table):
        return None
    parameters: list[tuple[str, str]] = []
    for item in target.expressions or []:
        if isinstance(item, exp.ColumnDef):
            kind = item.args.get("kind")
            parameters.append((item.name.lower(), kind.sql(dialect=dialect) if kind is not None else ""))
    return Table(
        name=name.name,
        schema=schema_name_of(name, dialect),
        kind=TABLE_FUNCTION,
        definition=statement.expression.sql(dialect=dialect),
        parameters=tuple(parameters),
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
