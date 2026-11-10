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
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.schema import MappingSchema

from . import safety
from .catalog import Catalog, Table, schema_name_of, view_query_from_sql
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
    # Calls sqlgami does not know to be read-only (a user-defined function, say). Analysed, never run.
    unlisted_functions: list[str] = field(default_factory=list)
    string_identifiers: list[str] = field(default_factory=list)
    types_annotated: bool = False
    # For a script (DECLARE ...; SET ...; SELECT ...): what came before the query.
    natural_joins: list[str] = field(default_factory=list)  # NATURAL joins as written, for reporting
    script_statements: list[str] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)  # lower-cased name -> declared type
    shadowed_variables: list[str] = field(default_factory=list)

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


def analyse(
    sql: str,
    catalog: Catalog | None,
    dialect: str | None = None,
    allowed_functions: Iterable[str] = (),
    allowed_tables: Iterable[str] = (),
) -> Analysis:
    """Parse ``sql`` and, if a catalog is given, resolve it against the schema."""
    dialect = dialect or (catalog.dialect if catalog is not None else None)
    if not dialect:
        raise ValueError("a dialect is required")
    analysis = Analysis(sql=sql, dialect=dialect)

    tokens: list[Any] | None = None
    try:
        # Tokenise once: the parser and the safety text net both read the same tokens.
        engine = Dialect.get_or_raise(dialect)
        tokens = engine.tokenize(sql)
        statements = [s for s in engine.parser().parse(tokens, sql) if s is not None]
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
    if len(statements) > 1 and dialect in _SCRIPT_DIALECTS and _is_script(statements):
        # A script: variable declarations and assignments, then the query that answers the question.
        for statement in statements[:-1]:
            analysis.script_statements.append(type(statement).__name__.upper())
            if isinstance(statement, exp.Declare):
                for item in statement.expressions:
                    kind = item.args.get("kind")
                    names = item.this if isinstance(item.this, list) else [item.this]
                    for name in names:
                        if name is not None:
                            analysis.variables[name.name.lower()] = kind.sql(dialect=dialect) if kind else ""
        analysis.script_statements.append("SELECT")
        statements = statements[-1:]
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
    if dialect == "sqlite":
        _sqlite_scalar_extremes(tree)
    analysis.tree = tree
    analysis.mutation_kinds = _mutation_kinds(tree)
    if analysis.mutation_kinds:
        analysis.kind = MUTATION
        return analysis
    if not isinstance(tree, exp.Query):
        analysis.kind = OTHER
        return analysis
    analysis.kind = QUERY
    review = safety.review(sql, tree, dialect, allowed_functions, allowed_tables, tokens=tokens)
    analysis.unsafe_functions = review.refused
    analysis.unlisted_functions = review.unlisted

    if catalog is not None:
        _Resolver(analysis, catalog).run()
    return analysis


def _sqlite_scalar_extremes(tree: exp.Expression) -> None:
    """SQLite's MAX(a, b, ...) and MIN(a, b, ...) with several arguments pick the greatest or least
    value of a row: they are GREATEST and LEAST, not aggregates. Rewritten so every check sees them
    for what they are; SQLite's generator writes GREATEST back as MAX, so snippets are unchanged."""
    for node in list(tree.find_all((exp.Max, exp.Min))):
        if node.expressions and node.this is not None:
            kind = exp.Greatest if isinstance(node, exp.Max) else exp.Least
            node.replace(kind(this=node.this, expressions=list(node.expressions)))


_SCRIPT_DIALECTS = {"bigquery"}


def _is_script(statements: list[exp.Expression]) -> bool:
    """Declarations and assignments followed by exactly one query."""
    *head, last = statements
    if not isinstance(last, exp.Query) or _mutation_kinds(last):
        return False
    for statement in head:
        if not isinstance(statement, (exp.Declare, exp.Set)):
            return False
        if any(isinstance(node, _MUTATING_NODES) for node in statement.walk()):
            return False
    return True


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
        elif isinstance(node, exp.Lock):
            kinds.add("FOR UPDATE" if node.args.get("update") else "FOR SHARE")  # asks for row locks
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
        if analysis.variables:
            self._bind_variables(tree)
        try:
            self._expand_natural_joins(tree)
        except (SqlglotError, RecursionError, KeyError, AttributeError):
            pass  # left as written; the engine remains the judge of validity

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
            schema_name_of(node, self.dialect) if isinstance(db_ident, exp.Identifier) else None,
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
            parsed = self.catalog.view_tree(table, self.dialect)
            if parsed is None:
                raise OptimizeError(f"the definition of view {qualified} could not be parsed")
            inner, view_columns = parsed[0].copy(), parsed[1]
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
            # In a parenthesised join group the joins hang off the first table; keep them.
            for key in ("joins", "pivots", "laterals"):
                carried = node.args.get(key)
                if carried:
                    subquery.set(key, carried)
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
            mapping.setdefault(schema_key, {})[name_key] = catalog.sqlglot_columns(table, self.dialect)
        return MappingSchema(mapping or None, dialect=self.dialect, normalize=False)

    # ---------------------------------------------------------- NATURAL JOIN

    def _expand_natural_joins(self, tree: exp.Expression) -> None:
        """Rewrite ``a NATURAL JOIN b`` as ``a JOIN b USING (shared columns)``.

        A NATURAL JOIN merges the columns the two sides share, so the shared
        name is legal without a table qualifier. Spelling the join out lets the
        usual USING handling resolve it, instead of calling the column ambiguous.
        """
        naturals = [j for j in tree.find_all(exp.Join) if (j.method or "").upper() == "NATURAL"]
        if not naturals:
            return
        root = build_scope(tree)
        if root is None:
            return
        for scope in root.traverse():
            select = scope.expression
            if not isinstance(select, exp.Select):
                continue
            seen: list[str] = []
            first = select.args.get("from_") or select.args.get("from")
            if first is not None and first.this is not None:
                seen.append(first.this.alias_or_name)
            for join in select.args.get("joins") or []:
                alias = join.this.alias_or_name if join.this is not None else ""
                if (join.method or "").upper() == "NATURAL":
                    self.analysis.natural_joins.append(join.sql(dialect=self.dialect))
                    right = self._columns_of(scope, alias)
                    left: set[str] = set()
                    for other in seen:
                        left |= self._columns_of(scope, other) or set()
                    shared = sorted((right or set()) & left)
                    join.set("method", None)
                    if shared:
                        join.set("using", [exp.to_identifier(name) for name in shared])
                seen.append(alias)

    def _columns_of(self, scope: Scope, alias: str) -> set[str] | None:
        source = scope.sources.get(alias)
        if isinstance(source, exp.Table):
            table = self.analysis.table_for(source)
            if table is None:
                return None
            return {c.name.lower() for c in table.columns}
        if isinstance(source, Scope) and isinstance(source.expression, exp.Query):
            names = [n for n in source.expression.named_selects if n and n != "*"]
            if source.expression.is_star:
                # SELECT * FROM t: the names are those of the tables underneath.
                names = []
                for inner in source.sources.values():
                    if isinstance(inner, exp.Table):
                        table = self.analysis.table_for(inner)
                        if table is not None:
                            names.extend(c.name for c in table.columns)
            return {n.lower() for n in names}
        return None

    # ------------------------------------------------------ script variables

    def _bind_variables(self, tree: exp.Expression) -> None:
        """Turn references to declared script variables into parameters.

        A column of a table in the query takes precedence over a variable of the
        same name, as it does in the engine; that case is recorded, since it is
        rarely what the author meant.
        """
        analysis = self.analysis
        column_names: set[str] = set()
        for table in analysis.tables.values():
            column_names |= {c.name.lower() for c in table.columns}
        for column in list(tree.find_all(exp.Column)):
            if column.table:
                continue
            name = column.name.lower()
            if name not in analysis.variables:
                continue
            if name in column_names:
                if name not in analysis.shadowed_variables:
                    analysis.shadowed_variables.append(name)
                continue
            column.replace(exp.Parameter(this=exp.Var(this=column.name)))

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
