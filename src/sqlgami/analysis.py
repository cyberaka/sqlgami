"""Parse a statement, resolve it against the catalog and expand views.

The result is an :class:`Analysis`: the parsed tree as written, plus a second
tree in which every table name is resolved, every view is replaced by its
definition (views on views included), every column is qualified, and types are
annotated where they can be. The static checks read the second tree, so they see
through views to the base tables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.schema import MappingSchema

from .catalog import Catalog, Table, view_query_from_sql
from .types import sqlglot_type_name

logger = logging.getLogger("sqlgami")

MAX_VIEW_DEPTH = 32

QUERY = "query"
MUTATION = "mutation"
OTHER = "other"
EMPTY = "empty"
MULTIPLE = "multiple"

_MUTATING_NODES: tuple[type, ...] = tuple(
    getattr(exp, name)
    for name in (
        "Insert",
        "Update",
        "Delete",
        "Merge",
        "Create",
        "Drop",
        "Alter",
        "AlterTable",
        "TruncateTable",
        "Copy",
        "Into",
        "Grant",
        "Revoke",
        "Analyze",
        "Refresh",
    )
    if hasattr(exp, name)
)

_NON_QUERY_ROOTS: tuple[type, ...] = tuple(
    getattr(exp, name)
    for name in (
        "Command",
        "Set",
        "Pragma",
        "Attach",
        "Detach",
        "Use",
        "Transaction",
        "Commit",
        "Rollback",
        "Describe",
        "Show",
        "Kill",
        "Install",
        "LoadData",
    )
    if hasattr(exp, name)
)

# Columns every row has without being declared.
_IMPLICIT_COLUMNS = {
    "sqlite": {"rowid", "oid", "_rowid_"},
    "duckdb": {"rowid"},
    "postgres": {"ctid", "xmin", "xmax", "cmin", "cmax", "tableoid"},
}


@dataclass
class Analysis:
    sql: str
    dialect: str
    kind: str = EMPTY
    statement_count: int = 0
    parse_error: str | None = None
    tree: exp.Expression | None = None  # as written
    resolved: exp.Expression | None = None  # views expanded, columns qualified
    root_scope: Scope | None = None
    mutation_kinds: list[str] = field(default_factory=list)
    unknown_tables: list[dict[str, Any]] = field(default_factory=list)
    unknown_columns: list[dict[str, Any]] = field(default_factory=list)
    ambiguous_columns: list[dict[str, Any]] = field(default_factory=list)
    resolve_error: str | None = None
    tables: dict[tuple[str, str], Table] = field(default_factory=dict)
    views_used: list[str] = field(default_factory=list)
    table_functions: list[str] = field(default_factory=list)
    unsafe_functions: list[str] = field(default_factory=list)
    string_identifiers: list[str] = field(default_factory=list)
    types_annotated: bool = False

    @property
    def parsed(self) -> bool:
        return self.parse_error is None and self.tree is not None

    @property
    def is_query(self) -> bool:
        return self.kind == QUERY

    @property
    def schema_ok(self) -> bool:
        return (
            self.resolved is not None
            and not self.unknown_tables
            and not self.unknown_columns
            and not self.ambiguous_columns
        )

    def table_for(self, node: exp.Table) -> Table | None:
        """The catalog table behind a resolved ``exp.Table`` node."""
        return self.tables.get((node.db, node.name))

    def scopes(self) -> list[Scope]:
        if self.root_scope is None:
            return []
        return list(self.root_scope.traverse())

    def select_scopes(self) -> list[Scope]:
        return [s for s in self.scopes() if isinstance(s.expression, exp.Select)]


def analyse(sql: str, catalog: Catalog | None, dialect: str | None = None) -> Analysis:
    """Parse ``sql`` and, if a catalog is given, resolve it against the schema."""
    dialect = dialect or (catalog.dialect if catalog is not None else None)
    if not dialect:
        raise ValueError("a dialect is required")
    analysis = Analysis(sql=sql, dialect=dialect)

    try:
        statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s is not None]
    except SqlglotError as exc:
        analysis.parse_error = _clean_error(exc)
        return analysis
    except RecursionError:
        analysis.parse_error = "statement is nested too deeply to parse"
        return analysis

    analysis.statement_count = len(statements)
    if not statements:
        analysis.kind = EMPTY
        return analysis
    if len(statements) > 1:
        analysis.kind = MULTIPLE
        analysis.tree = statements[0]
        analysis.mutation_kinds = sorted(
            {kind for statement in statements for kind in _mutation_kinds(statement)}
        )
        return analysis

    tree = statements[0]
    while isinstance(tree, exp.Subquery) and not tree.alias and isinstance(tree.this, exp.Query):
        tree = tree.this  # a query wrapped in parentheses
    analysis.tree = tree
    analysis.mutation_kinds = _mutation_kinds(tree)
    if analysis.mutation_kinds:
        analysis.kind = MUTATION
        return analysis
    if not isinstance(tree, exp.Query):
        analysis.kind = OTHER
        return analysis
    analysis.kind = QUERY
    analysis.unsafe_functions = _unsafe_functions(tree)

    if catalog is not None:
        _Resolver(analysis, catalog).run()
    return analysis


# Functions that reach outside the database (files, network, other servers) or stall it.
_UNSAFE_FUNCTIONS = {
    "read_csv", "read_csv_auto", "read_parquet", "parquet_scan", "read_json", "read_json_auto",
    "read_json_objects", "read_ndjson", "read_ndjson_auto", "read_text", "read_blob", "glob",
    "sqlite_scan", "postgres_scan", "postgres_query", "mysql_scan", "mysql_query", "iceberg_scan",
    "delta_scan", "read_xlsx", "st_read", "getenv", "query", "query_table",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export",
    "dblink", "dblink_exec", "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_terminate_backend",
    "pg_cancel_backend", "set_config", "nextval", "setval", "pg_advisory_lock",
    "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer",
}


def _unsafe_functions(tree: exp.Expression) -> list[str]:
    found: set[str] = set()
    for node in tree.find_all(exp.Func):
        name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
        name = (name or "").lower()
        if name in _UNSAFE_FUNCTIONS:
            found.add(name)
    for node in tree.find_all(exp.Table):
        # DuckDB reads a file when a quoted path stands where a table name should.
        ident = node.this
        if isinstance(ident, exp.Identifier) and ident.args.get("quoted") and not node.db:
            text = ident.name.lower()
            if "/" in text or text.endswith((".csv", ".parquet", ".json", ".ndjson", ".tsv", ".xlsx", ".db")):
                found.add(f"file {ident.name!r}")
    return sorted(found)


def _clean_error(exc: Exception) -> str:
    text = str(exc)
    # sqlglot underlines the offending token with terminal escape codes.
    for code in ("\x1b[4m", "\x1b[0m"):
        text = text.replace(code, "")
    return " ".join(text.split())


def _mutation_kinds(tree: exp.Expression) -> list[str]:
    kinds: set[str] = set()
    if isinstance(tree, _NON_QUERY_ROOTS):
        kinds.add(type(tree).__name__.upper())
    for node in tree.walk():
        if isinstance(node, _MUTATING_NODES):
            name = type(node).__name__.upper()
            kinds.add("SELECT INTO" if name == "INTO" else name)
    return sorted(kinds)


class _Resolver:
    def __init__(self, analysis: Analysis, catalog: Catalog) -> None:
        self.analysis = analysis
        self.catalog = catalog
        self.dialect = analysis.dialect

    def run(self) -> None:
        analysis = self.analysis
        tree = analysis.tree.copy()
        try:
            self._resolve_tables(tree, None, ())
        except OptimizeError as exc:
            analysis.resolve_error = _clean_error(exc)
            return
        except RecursionError:
            analysis.resolve_error = "statement is nested too deeply to analyse"
            return
        if analysis.unknown_tables:
            return
        if self.dialect == "sqlite":
            self._sqlite_string_identifiers(tree)

        schema = self._mapping_schema()
        try:
            resolved = qualify(
                tree,
                dialect=self.dialect,
                schema=schema,
                validate_qualify_columns=False,
                allow_partial_qualification=True,
                quote_identifiers=False,
                identify=False,
                infer_schema=False,
            )
            root_scope = build_scope(resolved)
        except (SqlglotError, RecursionError, KeyError, AttributeError, ValueError) as exc:
            analysis.resolve_error = _clean_error(exc)
            return
        analysis.resolved = resolved
        analysis.root_scope = root_scope
        try:
            self._validate_columns(root_scope)
        except (SqlglotError, RecursionError) as exc:
            analysis.resolve_error = _clean_error(exc)
            return
        if analysis.unknown_columns or analysis.ambiguous_columns:
            return
        try:
            annotate_types(resolved, schema=schema, dialect=self.dialect)
            analysis.types_annotated = True
        except Exception as exc:  # type inference is a bonus, never a failure
            logger.debug("type annotation failed: %s", exc)

    # ------------------------------------------------------------- tables

    def _resolve_tables(
        self,
        tree: exp.Expression,
        view_schema: str | None,
        stack: tuple[str, ...],
    ) -> None:
        root = build_scope(tree)
        if root is None:
            return
        search_path = None
        if view_schema:
            search_path = [view_schema] + [s for s in self.catalog.search_path if s != view_schema]
        seen: set[int] = set()
        nodes: list[exp.Table] = []
        for scope in root.traverse():
            for source in scope.sources.values():
                if isinstance(source, exp.Table) and id(source) not in seen:
                    seen.add(id(source))
                    nodes.append(source)
        for node in nodes:
            self._resolve_table(node, search_path, stack)

    def _resolve_table(
        self,
        node: exp.Table,
        search_path: list[str] | None,
        stack: tuple[str, ...],
    ) -> None:
        analysis, catalog = self.analysis, self.catalog
        ident = node.this
        if not isinstance(ident, exp.Identifier):
            analysis.table_functions.append(node.sql(dialect=self.dialect))
            return
        name = ident.name
        db_ident = node.args.get("db")
        table = catalog.resolve(
            name,
            db_ident.name if isinstance(db_ident, exp.Identifier) else None,
            quoted=bool(ident.args.get("quoted")),
            schema_quoted=bool(isinstance(db_ident, exp.Identifier) and db_ident.args.get("quoted")),
            search_path=search_path,
        )
        if table is None:
            written = node.copy()
            written.set("alias", None)
            analysis.unknown_tables.append(
                {
                    "name": written.sql(dialect=self.dialect),
                    "suggestions": catalog.suggest_tables(name),
                    "inside_view": stack[-1] if stack else None,
                }
            )
            return

        alias = node.args.get("alias")
        if table.is_view:
            qualified = table.qualified_name
            if qualified in stack:
                raise OptimizeError(f"view {qualified} refers to itself")
            if len(stack) >= MAX_VIEW_DEPTH:
                raise OptimizeError(f"views are nested more than {MAX_VIEW_DEPTH} deep")
            parsed = view_query_from_sql(table.definition or "", self.dialect)
            if parsed is None:
                raise OptimizeError(f"the definition of view {qualified} could not be parsed")
            query_sql, view_columns = parsed
            inner = sqlglot.parse_one(query_sql, dialect=self.dialect)
            self._resolve_tables(inner, table.schema, stack + (qualified,))
            if qualified not in analysis.views_used:
                analysis.views_used.append(qualified)
            if alias is None or not alias.this:
                alias_ident = exp.to_identifier(name, quoted=bool(ident.args.get("quoted")))
                columns = list(alias.columns) if alias is not None else []
            else:
                alias_ident = alias.this.copy()
                columns = [c.copy() for c in alias.columns]
            if not columns:
                columns = [exp.to_identifier(c, quoted=True) for c in (view_columns or table.view_columns)]
            subquery = exp.Subquery(
                this=inner,
                alias=exp.TableAlias(this=alias_ident, columns=columns or None),
            )
            subquery.meta["sqlgami_view"] = qualified
            node.replace(subquery)
            return

        schema_key = catalog.storage_key(table.schema)
        name_key = catalog.storage_key(table.name)
        if alias is None or not alias.this:
            keep = exp.TableAlias(this=exp.to_identifier(name, quoted=bool(ident.args.get("quoted"))))
            if alias is not None and alias.columns:
                keep.set("columns", [c.copy() for c in alias.columns])
            node.set("alias", keep)
        node.set("this", exp.to_identifier(name_key, quoted=True))
        node.set("db", exp.to_identifier(schema_key, quoted=True))
        node.set("catalog", None)
        analysis.tables[(schema_key, name_key)] = table

    def _mapping_schema(self) -> MappingSchema:
        catalog = self.catalog
        mapping: dict[str, dict[str, dict[str, str]]] = {}
        for (schema_key, name_key), table in self.analysis.tables.items():
            columns = {
                catalog.storage_key(c.name): sqlglot_type_name(c.type, self.dialect)
                for c in table.columns
            }
            mapping.setdefault(schema_key, {})[name_key] = columns
        return MappingSchema(mapping or None, dialect=self.dialect, normalize=False)

    # ------------------------------------------------ SQLite "string" quirk

    def _sqlite_string_identifiers(self, tree: exp.Expression) -> None:
        """SQLite reads a double-quoted name that matches no column as a string literal."""
        known: set[str] = set()
        for table in self.analysis.tables.values():
            known |= {c.name.lower() for c in table.columns}
        for select in tree.find_all(exp.Select):
            for projection in select.expressions:
                if projection.alias:
                    known.add(projection.alias.lower())
        for cte in tree.find_all(exp.CTE):
            alias = cte.args.get("alias")
            if alias is not None:
                known |= {c.name.lower() for c in alias.columns}
        for alias in tree.find_all(exp.TableAlias):
            known |= {c.name.lower() for c in alias.columns}
        for column in list(tree.find_all(exp.Column)):
            ident = column.this
            if column.table or not isinstance(ident, exp.Identifier) or not ident.args.get("quoted"):
                continue
            if ident.name.lower() in known:
                continue
            self.analysis.string_identifiers.append(ident.name)
            column.replace(exp.Literal.string(ident.name))

    # ------------------------------------------------------------ columns

    def _validate_columns(self, root: Scope) -> None:
        for scope in root.traverse():
            if not isinstance(scope.expression, exp.Select):
                continue
            select = scope.expression
            aliases = {name for name in select.named_selects if name}
            local = _local_sources(scope)
            for column in scope.columns:
                if isinstance(column.this, exp.Star):
                    continue
                name = column.name
                qualifier = column.table
                if not qualifier:
                    self._check_bare_column(scope, column, name, aliases, local)
                    continue
                source = scope.sources.get(qualifier)
                if source is None:
                    source = _outer_source(scope, qualifier)
                if source is None:
                    if scope.pivots or _inside(column, (exp.Pivot, exp.Unnest, exp.Lateral)):
                        continue
                    self.analysis.unknown_columns.append(
                        {
                            "column": column.sql(dialect=self.dialect),
                            "reason": f"no table or alias named {qualifier!r} is in scope",
                            "suggestions": sorted(local)[:5],
                        }
                    )
                    continue
                known = self._source_columns(source)
                if known is None or name in known:
                    continue
                if name in _IMPLICIT_COLUMNS.get(self.dialect, ()) and isinstance(source, exp.Table):
                    continue
                self.analysis.unknown_columns.append(
                    {
                        "column": column.sql(dialect=self.dialect),
                        "reason": f"{_describe_source(self, source, qualifier)} has no column {name!r}",
                        "suggestions": _close(name, known),
                    }
                )

    def _check_bare_column(
        self,
        scope: Scope,
        column: exp.Column,
        name: str,
        aliases: set[str],
        local: dict[str, Any],
    ) -> None:
        if name in aliases and _inside(column, (exp.Order, exp.Group, exp.Having, exp.Qualify, exp.Where, exp.Select)):
            # A reference to a select-list alias, which the engine resolves itself.
            if not _is_own_projection(column, name):
                return
        if scope.pivots or _inside(column, (exp.Pivot, exp.Unnest, exp.Lateral, exp.Lambda)):
            return
        if name in _IMPLICIT_COLUMNS.get(self.dialect, ()):
            return
        holders: list[str] = []
        unknown_shape = False
        candidates: set[str] = set()
        chain: list[dict[str, Any]] = [local]
        parent = scope.parent
        while parent is not None:
            chain.append(_local_sources(parent))
            parent = parent.parent
        for level in chain:
            for alias, source in level.items():
                known = self._source_columns(source)
                if known is None:
                    unknown_shape = True
                    continue
                candidates |= known
                if name in known:
                    holders.append(alias)
            if holders:
                break
        if unknown_shape and not holders:
            return
        if len(holders) > 1:
            self.analysis.ambiguous_columns.append(
                {"column": name, "sources": sorted(holders)}
            )
        elif not holders:
            if not chain[0] and not candidates:
                return  # no FROM clause at all; nothing to resolve against
            self.analysis.unknown_columns.append(
                {
                    "column": name,
                    "reason": "no table in the query has this column",
                    "suggestions": _close(name, candidates),
                }
            )

    def _source_columns(self, source: Any) -> set[str] | None:
        """Column names a source exposes, or ``None`` when that cannot be known."""
        if isinstance(source, exp.Table):
            table = self.analysis.table_for(source)
            if table is None or not table.columns:
                return None
            return self.catalog.column_keys(table)
        if isinstance(source, Scope):
            expression = source.expression
            if isinstance(expression, exp.Query):
                names = list(expression.named_selects)
                if not names or any(n == "*" or not n for n in names):
                    return None
                if expression.is_star:
                    return None
                return set(names)
            return None
        return None


def _local_sources(scope: Scope) -> dict[str, Any]:
    try:
        return {name: pair[1] for name, pair in scope.selected_sources.items()}
    except OptimizeError:
        raise
    except Exception:
        return dict(scope.sources)


def _outer_source(scope: Scope, qualifier: str) -> Any:
    parent = scope.parent
    while parent is not None:
        source = parent.sources.get(qualifier)
        if source is not None:
            return source
        parent = parent.parent
    return None


def _inside(node: exp.Expression, kinds: tuple[type, ...]) -> bool:
    return node.find_ancestor(*kinds) is not None


def _is_own_projection(column: exp.Column, name: str) -> bool:
    """True when the bare column *is* the projection that carries this alias."""
    parent = column.parent
    if isinstance(parent, exp.Alias) and parent.alias == name and isinstance(parent.parent, exp.Select):
        return True
    return isinstance(parent, exp.Select)


def _describe_source(resolver: _Resolver, source: Any, qualifier: str) -> str:
    if isinstance(source, exp.Table):
        table = resolver.analysis.table_for(source)
        if table is not None:
            return f"table {table.qualified_name}"
    return f"{qualifier!r}"


def _close(name: str, candidates: Iterable[str]) -> list[str]:
    import difflib

    return difflib.get_close_matches(name, sorted(candidates), n=3, cutoff=0.6)
