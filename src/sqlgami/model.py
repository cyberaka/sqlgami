"""A relational reading of each SELECT: its sources, join predicates and row grain.

The checks ask questions such as "can this join repeat rows of that table?".
:class:`SelectModel` answers them from declared keys, GROUP BY, DISTINCT and
``ROW_NUMBER() = 1`` filters, looking through derived tables, CTEs and expanded
views.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, ScopeType

from .analysis import Analysis
from .catalog import Catalog, Table

ColRef = tuple[str, str]  # (source alias, column name)

NO = "no"
YES = "yes"
MAYBE = "maybe"

_BOUNDARY = (exp.Select, exp.SetOperation)


def walk_own(node: exp.Expression) -> Iterator[exp.Expression]:
    """Walk ``node`` without descending into nested queries."""
    stack = [node]
    first = True
    while stack:
        current = stack.pop()
        if not first and isinstance(current, _BOUNDARY):
            continue
        first = False
        yield current
        for child in current.iter_expressions():
            stack.append(child)


def unalias(node: exp.Expression) -> exp.Expression:
    return node.this if isinstance(node, exp.Alias) else node


def strip(node: exp.Expression) -> exp.Expression:
    """Remove parentheses and casts, which do not change which column is meant."""
    while isinstance(node, (exp.Paren, exp.Cast, exp.TryCast)):
        node = node.this
    return node


def flatten_and(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    out: list[exp.Expression] = []
    stack = [node]
    while stack:
        current = stack.pop()
        while isinstance(current, exp.Paren):
            current = current.this
        if isinstance(current, exp.And):
            stack.append(current.expression)
            stack.append(current.this)
        else:
            out.append(current)
    return out


def from_clause(select: exp.Select) -> exp.Expression | None:
    return select.args.get("from_") or select.args.get("from")


@dataclass
class JoinRecord:
    join: exp.Join
    targets: list[str]  # aliases the join brings in (several for a parenthesised group)
    left: list[str]  # aliases already present to its left

    @property
    def side(self) -> str:
        return (self.join.side or "").upper()

    @property
    def is_outer(self) -> bool:
        return self.side in ("LEFT", "RIGHT", "FULL")


def _expand_join_target(node: exp.Expression) -> tuple[list[str], list[JoinRecord]]:
    """Aliases and joins inside one FROM item, which may be a parenthesised join group."""
    base = node
    while isinstance(base, exp.Subquery) and base.this is not None and not isinstance(base.this, exp.Query):
        base = base.this
    aliases = [base.alias_or_name]
    records: list[JoinRecord] = []
    for join in base.args.get("joins") or []:
        if join.this is None:
            continue
        targets, nested = _expand_join_target(join.this)
        records.extend(nested)
        records.append(JoinRecord(join, targets, list(aliases)))
        aliases.extend(targets)
    return aliases, records


def join_records(select: exp.Select) -> list[JoinRecord]:
    """Every join of a SELECT in order, including those nested in parentheses."""
    aliases: list[str] = []
    records: list[JoinRecord] = []
    from_ = from_clause(select)
    if from_ is not None and from_.this is not None:
        aliases, records = _expand_join_target(from_.this)
    for join in select.args.get("joins") or []:
        if join.this is None:
            continue
        targets, nested = _expand_join_target(join.this)
        records.extend(nested)
        records.append(JoinRecord(join, targets, list(aliases)))
        aliases.extend(targets)
    return records


@dataclass
class Source:
    alias: str
    kind: str  # table | derived | function
    node: exp.Expression
    table: Table | None = None
    scope: Scope | None = None
    join: exp.Join | None = None
    null_supplied: bool = False
    lateral: bool = False

    @property
    def label(self) -> str:
        if self.table is not None and self.table.name.lower() != self.alias.lower():
            return f"{self.table.name} ({self.alias})"
        return self.alias


@dataclass
class KeyInfo:
    keys: list[frozenset[str]] = field(default_factory=list)
    certain: bool = True  # False when key information is simply not available


@dataclass
class Multiplication:
    state: str = NO
    culprits: list[str] = field(default_factory=list)
    unlinked: list[str] = field(default_factory=list)


@dataclass
class Conjunct:
    expr: exp.Expression
    origin: str  # where | on
    join_aliases: frozenset[str] = frozenset()  # sources brought in by the join this ON belongs to
    outer_join: bool = False
    aliases: frozenset[str] = frozenset()


class ModelCache:
    """Builds one :class:`SelectModel` per SELECT scope and remembers it."""

    def __init__(self, analysis: Analysis, catalog: Catalog) -> None:
        self.analysis = analysis
        self.catalog = catalog
        self._models: dict[int, SelectModel] = {}
        self._keys: dict[int, KeyInfo] = {}

    def model(self, scope: Scope) -> "SelectModel":
        found = self._models.get(id(scope))
        if found is None:
            found = SelectModel(scope, self)
            self._models[id(scope)] = found
        return found

    def models(self) -> list["SelectModel"]:
        return [self.model(scope) for scope in self.analysis.select_scopes()]

    def output_keys(self, scope: Scope) -> KeyInfo:
        """Unique keys of a query's result, in terms of its output column names."""
        cached = self._keys.get(id(scope))
        if cached is not None:
            return cached
        self._keys[id(scope)] = KeyInfo([], False)  # guards against recursive CTEs
        expression = scope.expression
        if isinstance(expression, exp.Select):
            info = self.model(scope).output_keys()
        elif isinstance(expression, exp.Union) and expression.args.get("distinct"):
            names = [n for n in expression.named_selects if n and n != "*"]
            info = KeyInfo([frozenset(names)], True) if names else KeyInfo([], False)
        elif isinstance(expression, exp.SetOperation):
            info = KeyInfo([], True)
        else:
            info = KeyInfo([], False)
        self._keys[id(scope)] = info
        return info


class SelectModel:
    def __init__(self, scope: Scope, cache: ModelCache) -> None:
        self.scope = scope
        self.cache = cache
        self.analysis = cache.analysis
        self.catalog = cache.catalog
        self.select: exp.Select = scope.expression
        self.sources: dict[str, Source] = {}
        self.conjuncts: list[Conjunct] = []
        self.equalities: list[tuple[ColRef, ColRef, Conjunct]] = []
        self.bound: dict[ColRef, exp.Expression] = {}
        self.restricted: set[ColRef] = set()
        self.links: list[frozenset[str]] = []
        self._parent: dict[ColRef, ColRef] = {}
        self._mult: dict[str, Multiplication] = {}
        self._key_info: dict[str, KeyInfo] = {}
        self._build_sources()
        self._build_predicates()

    # ------------------------------------------------------------ sources

    def _build_sources(self) -> None:
        select = self.select
        try:
            selected = dict(self.scope.selected_sources)
        except Exception:
            selected = {}
        self.joins = join_records(select)
        joins_by_alias: dict[str, exp.Join] = {}
        null_supplied: set[str] = set()
        for record in self.joins:
            for alias in record.targets:
                joins_by_alias[alias] = record.join
            if record.side == "LEFT":
                null_supplied.update(record.targets)
            elif record.side == "RIGHT":
                null_supplied.update(record.left)
            elif record.side == "FULL":
                null_supplied.update(record.targets)
                null_supplied.update(record.left)

        for alias, (node, source) in selected.items():
            join = joins_by_alias.get(alias)
            lateral = isinstance(node, exp.Lateral) or bool(
                node is not None and node.find_ancestor(exp.Lateral) is not None
            )
            if isinstance(source, exp.Table):
                table = self.analysis.table_for(source)
                kind = "table" if isinstance(source.this, exp.Identifier) else "function"
                entry = Source(alias, kind, source, table=table, join=join)
            elif isinstance(source, Scope):
                if source.scope_type == ScopeType.UDTF or not isinstance(source.expression, exp.Query):
                    entry = Source(alias, "function", source.expression, join=join, lateral=True)
                else:
                    entry = Source(alias, "derived", source.expression, scope=source, join=join)
            else:
                entry = Source(alias, "function", node, join=join)
            entry.null_supplied = alias in null_supplied
            entry.lateral = entry.lateral or lateral
            self.sources[alias] = entry

    # --------------------------------------------------------- predicates

    def local_columns(self, node: exp.Expression) -> list[exp.Column]:
        return [
            c
            for c in walk_own(node)
            if isinstance(c, exp.Column) and c.table in self.sources
        ]

    def aliases_in(self, node: exp.Expression) -> frozenset[str]:
        """Local sources a predicate mentions, including inside its subqueries."""
        found = {c.table for c in node.find_all(exp.Column) if c.table in self.sources}
        return frozenset(found)

    def _is_constant(self, node: exp.Expression) -> bool:
        """True when ``node`` has one value for the whole SELECT (no local column in it)."""
        for inner in node.find_all(exp.Column):
            if inner.table in self.sources or not inner.table:
                return False
        return not any(isinstance(n, (exp.AggFunc, exp.Window)) for n in walk_own(node))

    def _build_predicates(self) -> None:
        select = self.select
        where = select.args.get("where")
        for expr in flatten_and(where.this if where is not None else None):
            self.conjuncts.append(Conjunct(expr, "where"))
        for record in self.joins:
            targets = frozenset(record.targets)
            for expr in flatten_and(record.join.args.get("on")):
                self.conjuncts.append(Conjunct(expr, "on", targets, record.is_outer))
            if (record.join.method or "").upper() == "NATURAL" or record.join.args.get("using"):
                # The join condition is implied by column names; treat the sources as linked.
                self.links.append(frozenset(record.targets) | frozenset(record.left))

        for conjunct in self.conjuncts:
            expr = conjunct.expr
            conjunct.aliases = self.aliases_in(expr)
            if len(conjunct.aliases) >= 2:
                self.links.append(conjunct.aliases)
            self._record(conjunct)
        # A derived table or table function that mentions a sibling is tied to it (LATERAL, UNNEST).
        for alias, source in self.sources.items():
            if source.kind == "table":
                continue
            mentioned = {
                c.table
                for c in source.node.find_all(exp.Column)
                if c.table in self.sources and c.table != alias
            }
            if mentioned:
                self.links.append(frozenset(mentioned | {alias}))

    def _record(self, conjunct: Conjunct) -> None:
        expr = conjunct.expr
        if isinstance(expr, exp.EQ):
            left, right = strip(expr.this), strip(expr.expression)
            left_col = self._local_ref(left)
            right_col = self._local_ref(right)
            if left_col and right_col:
                if left_col[0] != right_col[0]:
                    self.equalities.append((left_col, right_col, conjunct))
                    self._union(left_col, right_col)
            elif left_col and self._is_constant(right):
                self._bind(left_col, right, conjunct)
            elif right_col and self._is_constant(left):
                self._bind(right_col, left, conjunct)
        elif isinstance(expr, exp.In) and not expr.args.get("query"):
            ref = self._local_ref(strip(expr.this))
            values = expr.expressions
            if ref and values and all(self._is_constant(v) for v in values):
                if len(values) == 1:
                    self._bind(ref, values[0], conjunct)
                elif self.filters(ref, conjunct):
                    self.restricted.add(ref)
        elif isinstance(expr, exp.Is):
            ref = self._local_ref(strip(expr.this))
            if ref and isinstance(expr.expression, exp.Boolean):
                self._bind(ref, expr.expression, conjunct)
        elif isinstance(expr, exp.Column):
            ref = self._local_ref(expr)
            if ref:
                self._bind(ref, exp.true(), conjunct)
        elif isinstance(expr, exp.Not) and isinstance(strip(expr.this), exp.Column):
            ref = self._local_ref(strip(expr.this))
            if ref:
                self._bind(ref, exp.false(), conjunct)

    def _local_ref(self, node: exp.Expression) -> ColRef | None:
        if isinstance(node, exp.Column) and node.table in self.sources:
            return (node.table, node.name)
        return None

    def filters(self, ref: ColRef, conjunct: Conjunct) -> bool:
        """Whether a single-source predicate really removes rows of ``ref``'s source.

        In the ON clause of an outer join a condition on the preserved side does
        not filter that side, it only decides which rows find a partner.
        """
        if conjunct.origin != "on" or not conjunct.outer_join:
            return True
        source = self.sources.get(ref[0])
        return bool(source is not None and source.null_supplied and ref[0] in conjunct.join_aliases)

    def _bind(self, ref: ColRef, value: exp.Expression, conjunct: Conjunct) -> None:
        if not self.filters(ref, conjunct):
            return
        self.bound.setdefault(ref, value)
        self.restricted.add(ref)

    # ---------------------------------------------------------- union-find

    def _find(self, ref: ColRef) -> ColRef:
        parent = self._parent.setdefault(ref, ref)
        while parent != ref:
            grand = self._parent.setdefault(parent, parent)
            self._parent[ref] = grand
            ref, parent = parent, grand
        return ref

    def _union(self, a: ColRef, b: ColRef) -> None:
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            self._parent[ra] = rb

    def same_class(self, a: ColRef, b: ColRef) -> bool:
        return self._find(a) == self._find(b)

    def class_members(self, ref: ColRef) -> list[ColRef]:
        root = self._find(ref)
        return [member for member in list(self._parent) if self._find(member) == root]

    # ------------------------------------------------------------- shape

    @property
    def group(self) -> exp.Group | None:
        return self.select.args.get("group")

    def own_aggregates(self) -> list[exp.AggFunc]:
        """Aggregate calls that belong to this SELECT (not to a nested query or a window)."""
        found: list[exp.AggFunc] = []
        parts: list[exp.Expression] = list(self.select.expressions)
        for key in ("having", "order", "qualify"):
            part = self.select.args.get(key)
            if part is not None:
                parts.append(part)
        for part in parts:
            for node in walk_own(part):
                if isinstance(node, exp.AggFunc) and not is_window_function(node):
                    found.append(node)
        return found

    @property
    def is_aggregated(self) -> bool:
        return self.group is not None or bool(self.own_aggregates())

    def projection_names(self) -> list[str]:
        return [p.alias_or_name for p in self.select.expressions]

    def projection(self, name: str) -> exp.Expression | None:
        for p in self.select.expressions:
            if p.alias_or_name == name:
                return unalias(p)
        return None

    # --------------------------------------------------------------- keys

    def key_info(self, alias: str) -> KeyInfo:
        cached = self._key_info.get(alias)
        if cached is not None:
            return cached
        source = self.sources[alias]
        if source.kind == "table":
            table = source.table
            if table is None or not table.keys_known:
                info = KeyInfo([], False)
            else:
                keys = self.catalog.unique_key_sets(table)
                info = KeyInfo(keys, bool(keys))
        elif source.kind == "derived" and source.scope is not None:
            base = self.cache.output_keys(source.scope)
            extra = self._row_number_keys(alias, source)
            info = KeyInfo(list(base.keys) + extra, base.certain or bool(extra))
        else:
            info = KeyInfo([], False)
        self._key_info[alias] = info
        return info

    def _row_number_keys(self, alias: str, source: Source) -> list[frozenset[str]]:
        """``WHERE rn = 1`` over ``ROW_NUMBER() OVER (PARTITION BY k)`` makes ``k`` unique."""
        inner = source.node
        if not isinstance(inner, exp.Select):
            return []
        keys: list[frozenset[str]] = []
        for (ref_alias, column), value in self.bound.items():
            if ref_alias != alias or not _is_one(value):
                continue
            for projection in inner.expressions:
                if projection.alias_or_name != column:
                    continue
                key = _row_number_partition(unalias(projection), inner)
                if key is not None:
                    keys.append(key)
        return keys

    def bound_columns(self, alias: str, visited: set[str]) -> set[str]:
        """Columns of ``alias`` pinned to one value per row of the ``visited`` sources."""
        columns: set[str] = set()
        candidates = {ref for ref in self._parent if ref[0] == alias}
        candidates |= {ref for ref in self.bound if ref[0] == alias}
        for ref in candidates:
            if ref in self.bound:
                columns.add(ref[1])
                continue
            for member in self.class_members(ref):
                if member in self.bound or (member[0] != alias and member[0] in visited):
                    columns.add(ref[1])
                    break
        return columns

    def joins_at_most_one(self, alias: str, visited: set[str]) -> bool:
        info = self.key_info(alias)
        if not info.keys:
            return False
        bound = self.bound_columns(alias, visited)
        return any(key <= bound for key in info.keys)

    def multiplied(self, alias: str) -> Multiplication:
        """Whether the other sources can repeat rows of ``alias`` in the join result."""
        cached = self._mult.get(alias)
        if cached is not None:
            return cached
        visited = {alias}
        changed = True
        while changed:
            changed = False
            for other in self.sources:
                if other not in visited and self.joins_at_most_one(other, visited):
                    visited.add(other)
                    changed = True
        result = Multiplication()
        for other, source in self.sources.items():
            if other in visited:
                continue
            linked = any(other in link and (link & visited) for link in self.links)
            if not linked:
                reach = self._reachable(other)
                if not (reach & visited):
                    result.unlinked.append(other)
                    result.culprits.append(other)
                    result.state = YES
                continue
            result.culprits.append(other)
            info = self.key_info(other)
            if info.certain and source.kind != "function":
                result.state = YES
            elif result.state != YES:
                result.state = MAYBE
        self._mult[alias] = result
        return result

    def _reachable(self, alias: str) -> set[str]:
        reach = {alias}
        changed = True
        while changed:
            changed = False
            for link in self.links:
                if link & reach and not link <= reach:
                    reach |= link
                    changed = True
        return reach

    def components(self) -> list[set[str]]:
        """Groups of sources tied together by at least one predicate."""
        remaining = set(self.sources)
        groups: list[set[str]] = []
        while remaining:
            start = sorted(remaining)[0]
            group = self._reachable(start) & set(self.sources)
            groups.append(group)
            remaining -= group
        return groups

    def is_single_row(self, alias: str) -> bool:
        return frozenset() in self.key_info(alias).keys

    def output_keys(self) -> KeyInfo:
        select = self.select
        names = self.projection_names()
        usable = [n for n in names if n and n != "*"]
        group = self.group

        limit = select.args.get("limit")
        if limit is not None and _is_one(limit.expression):
            return KeyInfo([frozenset()], True)
        if group is None and self.own_aggregates():
            return KeyInfo([frozenset()], True)

        keys: list[frozenset[str]] = []
        certain = True

        if group is not None:
            if group.args.get("rollup") or group.args.get("cube") or group.args.get("grouping_sets"):
                return KeyInfo([], True)
            if group.args.get("all"):
                plain = [
                    p.alias_or_name
                    for p in select.expressions
                    if not any(isinstance(n, exp.AggFunc) for n in walk_own(p))
                ]
                return KeyInfo([frozenset(plain)], True) if plain else KeyInfo([frozenset()], True)
            mapped: list[str] = []
            for expr in group.expressions:
                name = self._projection_name_for(expr)
                if name is None:
                    return KeyInfo([], False)
                mapped.append(name)
            return KeyInfo([frozenset(mapped)], True)

        distinct = select.args.get("distinct")
        if distinct is not None:
            on = distinct.args.get("on")
            if on is not None:
                exprs = on.expressions if isinstance(on, exp.Tuple) else [on]
                mapped = [self._projection_name_for(e) for e in exprs]
                if all(mapped):
                    keys.append(frozenset(mapped))
            elif usable and len(usable) == len(names):
                keys.append(frozenset(usable))

        qualify = select.args.get("qualify")
        if qualify is not None:
            for expr in flatten_and(qualify.this):
                if isinstance(expr, exp.EQ):
                    for window, value in ((expr.this, expr.expression), (expr.expression, expr.this)):
                        if _is_one(value):
                            key = _row_number_partition(strip(window), select)
                            if key is not None:
                                keys.append(key)

        for alias in self.sources:
            if self.multiplied(alias).state != NO:
                continue
            info = self.key_info(alias)
            if not info.certain:
                certain = False
            for key in info.keys:
                mapped = []
                for column in key:
                    name = self._projection_name_for_ref((alias, column))
                    if name is None:
                        break
                    mapped.append(name)
                else:
                    keys.append(frozenset(mapped))
        if not self.sources:
            return KeyInfo([frozenset()], True)  # SELECT without FROM yields one row
        if not keys:
            certain = certain and all(self.key_info(a).certain for a in self.sources)
        return KeyInfo(keys, certain or bool(keys))

    def _projection_name_for(self, expr: exp.Expression) -> str | None:
        target = strip(expr)
        for projection in self.select.expressions:
            if strip(unalias(projection)) == target:
                return projection.alias_or_name or None
        return None

    def _projection_name_for_ref(self, ref: ColRef) -> str | None:
        for projection in self.select.expressions:
            inner = strip(unalias(projection))
            if isinstance(inner, exp.Column) and (inner.table, inner.name) == ref:
                return projection.alias_or_name or None
        # A column equated to a projected one identifies the same value.
        for member in self.class_members(ref):
            if member == ref:
                continue
            for projection in self.select.expressions:
                inner = strip(unalias(projection))
                if isinstance(inner, exp.Column) and (inner.table, inner.name) == member:
                    return projection.alias_or_name or None
        return None

    # ------------------------------------------------------------ lineage

    def origin(self, column: exp.Column, depth: int = 0) -> tuple[Any, exp.Expression | None]:
        """Follow a column into derived tables to the expression that computes it.

        Returns ``(model, expression)`` where ``expression`` is the defining
        expression inside ``model``'s SELECT, or ``(self, column)`` for a base
        table column.
        """
        source = self.sources.get(column.table)
        if source is None or source.kind != "derived" or source.scope is None or depth > 64:
            return self, column
        inner_scope = source.scope
        expression = inner_scope.expression
        if not isinstance(expression, exp.Select):
            return self, column
        inner_model = self.cache.model(inner_scope)
        defining = inner_model.projection(column.name)
        if defining is None:
            return self, column
        if isinstance(defining, exp.Column) and defining.table in inner_model.sources:
            return inner_model.origin(defining, depth + 1)
        return inner_model, defining

    def base_column(self, column: exp.Column) -> tuple[Table, str] | None:
        """The base table and column a reference ultimately reads, when it is a plain pass-through."""
        model, expression = self.origin(column)
        if isinstance(expression, exp.Column):
            source = model.sources.get(expression.table)
            if source is not None and source.table is not None:
                return source.table, expression.name
        return None


def is_window_function(node: exp.Expression) -> bool:
    """True when an aggregate call is used as a window function (``SUM(x) OVER (...)``)."""
    current = node
    parent = current.parent
    wrappers = (exp.Filter, exp.IgnoreNulls, exp.RespectNulls, exp.WithinGroup)
    while isinstance(parent, wrappers) and parent.this is current:
        current, parent = parent, parent.parent
    return isinstance(parent, exp.Window) and parent.this is current


def _is_one(node: exp.Expression | None) -> bool:
    if node is None:
        return False
    node = strip(node)
    return isinstance(node, exp.Literal) and not node.is_string and node.name in ("1", "1.0")


def _row_number_partition(node: exp.Expression, select: exp.Select) -> frozenset[str] | None:
    """Output names of the PARTITION BY columns of a ``ROW_NUMBER()`` window, if all are projected."""
    if not isinstance(node, exp.Window) or not isinstance(node.this, exp.RowNumber):
        return None
    names: list[str] = []
    for part in node.args.get("partition_by") or []:
        target = strip(part)
        found = None
        for projection in select.expressions:
            if strip(unalias(projection)) == target:
                found = projection.alias_or_name
                break
        if not found:
            return None
        names.append(found)
    return frozenset(names)
