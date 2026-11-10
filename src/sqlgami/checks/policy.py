"""Checks about filters a query is expected to carry.

Tenant isolation, soft deletes, history tables, snapshot tables, partition
filters and access-control rules all ask the same question of a different
column: "is this column dealt with wherever its table is read?". The tracer
below answers it, following the column through derived tables, CTEs and
expanded views, so a filter applied outside a view counts for the base table
inside it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

from sqlglot import exp
from sqlglot.optimizer.scope import Scope

from ..catalog import Column, RequiredPredicate, Table
from ..model import NO, ColRef, SelectModel, strip, walk_own, unalias
from ..report import Finding, Severity
from .base import CheckContext, check

CONSTRAINED = "constrained"  # the column must be pinned to a value (or to a column that is)
MENTIONED = "mentioned"  # the column must at least be looked at
GRAIN = "grain"  # the column must be pinned, equated to another column, or grouped by


@dataclass
class Miss:
    model: SelectModel
    alias: str
    table: Table
    column: Column
    indirect: bool = False  # joined to something that is filtered, but not on a key
    bridge: bool = False  # only passed through on the way to another table
    in_view: bool = False  # the table is read inside a view body, not by the query's author
    counted: bool = True  # its rows are what the query counts or lists (not a parent it merely joins to)
    # The table sits in a derived table that the query uses only to look values up: joined at most
    # one row per row of a source with many rows, and not grouped or listed by.
    lookup: bool = False


def inside_view(node: exp.Expression) -> bool:
    current: exp.Expression | None = node
    while current is not None:
        if isinstance(current, exp.Subquery) and current.meta.get("sqlgami_view"):
            return True
        current = current.parent
    return False


def _single_column_key(model: SelectModel, alias: str, column: str) -> bool:
    return frozenset({column}) in model.key_info(alias).keys


class _Tracer:
    def __init__(
        self,
        ctx: CheckContext,
        columns_for: Callable[[Table], list[Column]],
        mode: str,
    ) -> None:
        self.ctx = ctx
        self.columns_for = columns_for
        self.mode = mode
        self.misses: dict[int, Miss] = {}
        self.satisfied: set[int] = set()
        self._visited: set[tuple[int, frozenset[str], frozenset[str]]] = set()
        self._live: dict[int, frozenset[str]] = {}

    @classmethod
    def for_names(cls, ctx: CheckContext, names: Iterable[str], mode: str, applies=None) -> "_Tracer":
        wanted = tuple(names)

        def columns_for(table: Table) -> list[Column]:
            if applies is not None and not applies(table):
                return []
            found = (ctx.catalog.column(table, name) for name in wanted)
            return [c for c in found if c is not None]

        return cls(ctx, columns_for, mode)

    def run(self) -> list[Miss]:
        root = self.ctx.analysis.root_scope
        if root is not None:
            self._visit(root, frozenset(), frozenset())
        return [miss for key, miss in self.misses.items() if key not in self.satisfied]

    # ------------------------------------------------------------ traversal

    def _visit(
        self,
        scope: Scope,
        external: frozenset[str],
        outer: frozenset[ColRef],
        live: frozenset[str] = frozenset(),
        lookup: bool = False,
    ) -> None:
        """``live``: output columns of this query that the consumer ties to anchored keys, so the
        rows that matter are already the filtered ones. ``lookup``: the consumer only looks values up here."""
        key = (id(scope), external, live)
        if key in self._visited:
            return
        self._visited.add(key)
        expression = scope.expression
        if isinstance(expression, exp.SetOperation):
            names = list(expression.named_selects)
            positions = [i for i, name in enumerate(names) if name in external]
            for branch in scope.union_scopes:
                branch_names = (
                    list(branch.expression.named_selects) if isinstance(branch.expression, exp.Query) else []
                )
                mapped = frozenset(branch_names[i] for i in positions if i < len(branch_names))
                live_positions = [i for i, name in enumerate(names) if name in live]
                mapped_live = frozenset(branch_names[i] for i in live_positions if i < len(branch_names))
                self._visit(branch, mapped, outer, mapped_live, lookup)
            return
        if not isinstance(expression, exp.Select):
            return
        model = self.ctx.cache.model(scope)
        covered = self._covered(model, external, outer)
        anchored = self._anchored(model, covered, outer, self._anchored_by_live(model, live))

        for alias, source in model.sources.items():
            if source.kind == "table" and source.table is not None:
                columns = self.columns_for(source.table)
                if not columns:
                    continue
                node_id = id(source.node)
                if alias in anchored:
                    self.satisfied.add(node_id)
                    continue
                self.misses.setdefault(
                    node_id,
                    Miss(
                        model,
                        alias,
                        source.table,
                        columns[0],
                        indirect=self._loosely_linked(model, alias, anchored),
                        bridge=self._is_bridge(model, alias),
                        in_view=inside_view(source.node),
                        counted=len(model.sources) == 1
                        or model.multiplied(alias).state == NO
                        or self._lists_rows(model, alias),
                        # Looked up only: through a consumer that only looks this query's values up, or
                        # joined here at most one row per row of the table that brings the rows, with
                        # its own key neither listed nor grouped by.
                        lookup=(lookup and (len(model.sources) == 1 or model.multiplied(alias).state == NO)) or self._is_lookup(model, alias),
                    ),
                )
            elif source.kind == "derived" and source.scope is not None:
                names = frozenset(column for (a, column) in covered if a == alias)
                inner_live = self._live_names_for(model, alias, anchored)
                self._visit(
                    source.scope,
                    names,
                    outer | covered if source.lateral else outer,
                    inner_live,
                    lookup or self._is_lookup(model, alias),
                )

        for sub in scope.subquery_scopes:
            self._visit(sub, frozenset(), outer | covered)

    def _own_refs(self, model: SelectModel, alias: str) -> list[ColRef]:
        source = model.sources[alias]
        if source.table is None:
            return []
        return [(alias, self.ctx.catalog.column_key(c.name)) for c in self.columns_for(source.table)]

    def _anchored(
        self,
        model: SelectModel,
        covered: frozenset[ColRef],
        outer: frozenset[ColRef],
        given: set[str] | None = None,
    ) -> set[str]:
        """Sources whose rows are limited the way this check requires.

        A table is anchored when its own column is covered. It is also anchored
        when it is tied by a single-column key to an anchored source: the join
        then picks out rows that belong to the anchored ones, so no separate
        filter is needed. (A join on part of a composite key gives no such
        assurance, and stays a finding.) A derived table is anchored on a column
        when that column holds only keys of rows already filtered inside it.
        """
        anchored: set[str] = set(given or ())
        for alias, source in model.sources.items():
            if source.kind == "table":
                if any(ref in covered for ref in self._own_refs(model, alias)):
                    anchored.add(alias)
            elif any(a == alias for (a, _) in covered):
                anchored.add(alias)  # a derived table with a covered column; checked inside as well
        if self.mode == MENTIONED:
            anchored |= self._anchored_by_derived_keys(model, anchored)
            return anchored | self._key_semi_joined(model, anchored, outer)
        changed = True
        while changed:
            changed = False
            for a, b, conjunct in model.equalities:
                for near, far in ((a, b), (b, a)):
                    if near[0] in anchored or far[0] not in anchored:
                        continue
                    if model.sources[near[0]].kind != "table":
                        continue
                    if not model.filters(near, conjunct):
                        continue  # the preserved side of an outer join is not narrowed by its ON clause
                    if _single_column_key(model, near[0], near[1]) or _single_column_key(model, far[0], far[1]):
                        anchored.add(near[0])
                        changed = True
            more = self._anchored_by_derived_keys(model, anchored) - anchored
            if more:
                anchored |= more
                changed = True
        return anchored | self._key_semi_joined(model, anchored, outer)

    # --------------------------------------------- filters applied inside a derived table

    def _live_keys(self, scope: Scope) -> frozenset[str]:
        """Output columns of a derived table that hold only keys of rows it has already filtered.

        ``WITH rev AS (SELECT i.tenant_id ... FROM invoices i JOIN tenants t ON t.tenant_id = i.tenant_id
        AND t.deleted_at IS NULL ...)``: rev.tenant_id names live tenants only.
        """
        cached = self._live.get(id(scope))
        if cached is not None:
            return cached
        self._live[id(scope)] = frozenset()  # guards recursion
        found: set[str] = set()
        expression = scope.expression
        if isinstance(expression, exp.SetOperation):
            branches = [self._live_keys(b) for b in scope.union_scopes]
            if branches:
                names = list(expression.named_selects)
                common = set.intersection(*[set(b) for b in branches]) if all(branches) else set()
                found = {n for n in names if n in common}
        elif isinstance(expression, exp.Select):
            try:
                model = self.ctx.cache.model(scope)
                anchored = self._anchored(model, self._covered(model, frozenset(), frozenset()), frozenset())
            except Exception:
                anchored = set()
            keyed: set[ColRef] = set()
            for alias in anchored:
                source = model.sources[alias]
                if source.kind == "table" and source.table is not None:
                    for key in model.key_info(alias).keys:
                        if len(key) == 1:
                            keyed.add((alias, next(iter(key))))
                elif source.kind == "derived" and source.scope is not None:
                    for name in self._live_keys(source.scope):
                        keyed.add((alias, name))
            for ref in list(keyed):
                keyed.update(model.class_members(ref))
            for projection in model.select.expressions:
                inner = strip(unalias(projection))
                if isinstance(inner, exp.Column) and (inner.table, inner.name) in keyed and projection.alias_or_name:
                    found.add(projection.alias_or_name)
        self._live[id(scope)] = frozenset(found)
        return self._live[id(scope)]

    def _anchored_by_derived_keys(self, model: SelectModel, anchored: set[str]) -> set[str]:
        """Tables joined on their single-column key to a derived table's live keys."""
        found: set[str] = set()
        for a, b, conjunct in model.equalities:
            for near, far in ((a, b), (b, a)):
                if near[0] in anchored or near[0] in found:
                    continue
                source_far = model.sources.get(far[0])
                if source_far is None or source_far.kind != "derived" or source_far.scope is None:
                    continue
                if far[1] not in self._live_keys(source_far.scope):
                    continue
                if not model.filters(near, conjunct):
                    continue
                if model.sources[near[0]].kind == "table" and _single_column_key(model, near[0], near[1]):
                    found.add(near[0])
        return found

    def _anchored_by_live(self, model: SelectModel, live: frozenset[str]) -> set[str]:
        """Inside a derived table: sources whose single-column key is a column the consumer ties to live keys."""
        found: set[str] = set()
        for name in live:
            defining = model.projection(name)
            if defining is None:
                continue
            inner = strip(defining)
            if not isinstance(inner, exp.Column) or inner.table not in model.sources:
                continue
            for ref in model.class_members((inner.table, inner.name)):
                source = model.sources.get(ref[0])
                if source is None:
                    continue
                if source.kind == "table" and _single_column_key(model, ref[0], ref[1]):
                    found.add(ref[0])
        return found

    def _live_names_for(self, model: SelectModel, alias: str, anchored: set[str]) -> frozenset[str]:
        """Columns of derived ``alias`` equated to an anchored source's single-column key."""
        names: set[str] = set()
        for a, b, conjunct in model.equalities:
            for mine, theirs in ((a, b), (b, a)):
                if mine[0] != alias or theirs[0] not in anchored or theirs[0] == alias:
                    continue
                if not model.filters(mine, conjunct):
                    continue
                other = model.sources[theirs[0]]
                if other.kind == "table" and _single_column_key(model, theirs[0], theirs[1]):
                    names.add(mine[1])
                elif other.kind == "derived" and other.scope is not None and theirs[1] in self._live_keys(other.scope):
                    names.add(mine[1])
        return frozenset(names)

    def _is_lookup(self, model: SelectModel, alias: str) -> bool:
        """``alias`` is joined at most one row per row of a source that has many rows, and no column of
        it is grouped by: the query looks values up in it and does not list or count its rows."""
        others = set(model.sources) - {alias}
        if not others or not model.joins_at_most_one(alias, others):
            return False
        # Grouping by its key lists its rows (one per tenant); grouping by an attribute (region)
        # only classifies the other table's rows.
        grouped = {ref[1] for ref in model.group_refs() if ref[0] == alias}
        if grouped and any(key and key <= grouped for key in model.key_info(alias).keys):
            return False
        if model.group is None:
            for projection in model.select.expressions:
                inner = strip(unalias(projection))
                if isinstance(inner, exp.Column) and inner.table == alias:
                    if any(key and key <= {inner.name} for key in model.key_info(alias).keys):
                        return False  # its key is listed: rows per tenant
        # Some other source brings the rows (it is not itself joined at most one per row of the
        # rest); whether a third table repeats that source is a fan-out question, not this one.
        return any(not model.joins_at_most_one(other, set(model.sources) - {other}) for other in others)

    def _key_semi_joined(self, model: SelectModel, anchored: set[str], outer: frozenset[ColRef]) -> set[str]:
        """``t.key IN (SELECT key FROM t WHERE <the filter>)``: the outer rows are the filtered ones."""
        found: set[str] = set()
        for conjunct in model.conjuncts:
            expr = conjunct.expr
            if not isinstance(expr, exp.In) or expr.args.get("query") is None:
                continue
            column = strip(expr.this)
            if not isinstance(column, exp.Column) or column.table not in model.sources:
                continue
            alias = column.table
            source = model.sources[alias]
            if alias in anchored or source.table is None:
                continue
            if not _single_column_key(model, alias, column.name):
                continue
            query = expr.args["query"]
            inner = query.this if isinstance(query, exp.Subquery) else query
            sub_scope = next((s for s in model.scope.subquery_scopes if s.expression is inner), None)
            if sub_scope is None or not isinstance(inner, exp.Select) or len(inner.expressions) != 1:
                continue
            sub_model = self.ctx.cache.model(sub_scope)
            projected = strip(inner.expressions[0].unalias())
            if not isinstance(projected, exp.Column) or projected.table not in sub_model.sources:
                continue
            base = sub_model.base_column(projected)
            if base is None or base[0] is not source.table or base[1] != column.name:
                continue
            sub_covered = self._covered(sub_model, frozenset(), outer)
            if projected.table in self._anchored(sub_model, sub_covered, outer):
                found.add(alias)
        return found

    def _covered(
        self, model: SelectModel, external: frozenset[str], outer: frozenset[ColRef]
    ) -> frozenset[ColRef]:
        seeds: set[ColRef] = set()
        for name in external:
            defining = model.projection(name)
            if defining is None:
                continue
            inner = strip(defining)
            if isinstance(inner, exp.Column) and inner.table in model.sources:
                seeds.add((inner.table, inner.name))
        if self.mode == MENTIONED:
            for node in model.own():
                if isinstance(node, exp.Column) and node.table in model.sources:
                    if not _is_plain_projection(node):
                        seeds.add((node.table, node.name))
            return frozenset(seeds)

        for ref in model.restricted:
            value = model.bound.get(ref)
            if value is not None:
                inner = strip(value)
                if isinstance(inner, exp.Column) and inner.table and (inner.table, inner.name) not in outer:
                    continue  # tied to an outer column that is itself not pinned
            seeds.add(ref)
        if self.mode == GRAIN:
            seeds |= model.self_equal
            seeds |= model.group_refs()
            for a, b, _ in model.equalities:
                seeds.update((a, b))
            # SELECT MAX(snapshot_date) FROM ...: looking up which snapshot to use, not counting rows.
            for aggregate in model.own_aggregates():
                if isinstance(aggregate, (exp.Max, exp.Min)):
                    inner = strip(aggregate.this)
                    if isinstance(inner, exp.Column) and inner.table in model.sources:
                        seeds.add((inner.table, inner.name))
        covered = set(seeds)
        changed = True
        while changed:
            changed = False
            for a, b, conjunct in model.equalities:
                for src, dst in ((a, b), (b, a)):
                    if src in covered and dst not in covered and model.filters(dst, conjunct):
                        covered.add(dst)
                        changed = True
        return frozenset(covered)

    def _loosely_linked(self, model: SelectModel, alias: str, anchored: set[str]) -> bool:
        """Joined to an anchored source, but not on a key that would make the link safe."""
        for a, b, _ in model.equalities:
            if a[0] == alias and b[0] in anchored:
                return True
            if b[0] == alias and a[0] in anchored:
                return True
        return False

    def _lists_rows(self, model: SelectModel, alias: str) -> bool:
        """The table's own columns come out as result rows (selected outside any aggregate, or
        grouped by), so a deleted row of it is literally a row of the answer."""
        nodes: list[exp.Expression] = list(model.select.expressions)
        if model.group is not None:
            nodes.extend(model.group.expressions)
        for node in nodes:
            for column in walk_own(node):
                if not (isinstance(column, exp.Column) and column.table == alias):
                    continue
                parent = column.parent
                inside_aggregate = False
                while parent is not None and parent is not node.parent:
                    if isinstance(parent, exp.AggFunc):
                        inside_aggregate = True
                        break
                    parent = parent.parent
                if not inside_aggregate:
                    return True
        return False

    def _is_bridge(self, model: SelectModel, alias: str) -> bool:
        """A table none of whose columns are returned or grouped by, reached through its key."""
        nodes: list[exp.Expression] = list(model.select.expressions)
        if model.group is not None:
            nodes.extend(model.group.expressions)
        for node in nodes:
            for column in walk_own(node):
                if isinstance(column, exp.Column) and column.table == alias:
                    return False
        others = set(model.sources) - {alias}
        return bool(others) and model.joins_at_most_one(alias, others)


def _is_plain_projection(column: exp.Column) -> bool:
    """A column that is merely selected, as opposed to tested or computed with."""
    node: exp.Expression = column
    if isinstance(node.parent, exp.Alias):
        node = node.parent
    return isinstance(node.parent, exp.Select) and node.arg_key == "expressions"


# ------------------------------------------------------------------ tenants


@check("missing_tenant_filter", description="A table holding several tenants' rows is read without a tenant filter.")
def missing_tenant_filter(ctx: CheckContext) -> Iterable[Finding]:
    column = ctx.catalog.tenant_column
    if not column:
        return
    for miss in _Tracer.for_names(ctx, (column,), CONSTRAINED).run():
        source = miss.model.sources[miss.alias]
        if miss.indirect:
            message = (
                f"{source.label} holds rows for every tenant and is not filtered on {column} "
                "itself; it is joined to a filtered table, but not on a key that is unique on "
                "its own. If key values repeat across tenants, rows from other tenants are included."
            )
            severity = Severity.MEDIUM
        else:
            message = (
                f"{source.label} holds rows for every tenant, and nothing limits {column} to "
                "one tenant, so the result mixes tenants' data."
            )
            severity = Severity.HIGH
        yield ctx.finding(
            "missing_tenant_filter",
            severity,
            message,
            suggestion=f"Filter {miss.alias}.{column}, or join on {column} as well as the key.",
            table=miss.table.qualified_name,
            alias=miss.alias,
            indirect=miss.indirect,
        )


# ---------------------------------------------------- required predicates


def _base_of(model: SelectModel, ref: ColRef) -> tuple[Table, str] | None:
    source = model.sources.get(ref[0])
    if source is None:
        return None
    if source.table is not None:
        return source.table, ref[1]
    return model.base_column(exp.column(ref[1], table=ref[0]))


@check("required_predicate", description="A filter that policy requires on a table is missing or incomplete.")
def required_predicate(ctx: CheckContext) -> Iterable[Finding]:
    for rule in ctx.catalog.required_predicates:
        severity = Severity(rule.severity) if rule.severity in ("high", "medium", "low") else Severity.HIGH
        what = rule.description or rule.name
        if rule.match == "or_chain":
            yield from _or_chain_rule(ctx, rule, severity, what)
            continue
        groups = [rule.columns] if rule.match == "any" else [(c,) for c in rule.columns]
        reported: set[tuple[str, str]] = set()
        for names in groups:
            for miss in _Tracer.for_names(ctx, names, CONSTRAINED, rule.applies_to).run():
                key = (miss.table.qualified_name, miss.column.name)
                if key in reported:
                    continue
                reported.add(key)
                source = miss.model.sources[miss.alias]
                listed = ", ".join(names)
                yield ctx.finding(
                    "required_predicate",
                    Severity.MEDIUM if miss.indirect and severity == Severity.HIGH else severity,
                    f"Policy \"{what}\" requires every query on {source.label} to pin {listed} to a "
                    "value, and this one does not.",
                    suggestion=f"Add the {listed} condition for {miss.alias}.",
                    rule=rule.name,
                    table=miss.table.qualified_name,
                    columns=list(names),
                )


def _or_chain_rule(ctx: CheckContext, rule: RequiredPredicate, severity: Severity, what: str) -> Iterable[Finding]:
    catalog = ctx.catalog
    # table -> for each SELECT, the rule columns its OR chains cover
    chains: dict[str, list[tuple[set[str], exp.Expression]]] = {}
    for model in ctx.cache.models():
        per_table: dict[str, set[str]] = {}
        nodes: dict[str, exp.Expression] = {}
        for refs, same_value, node in model.or_groups:
            if not same_value:
                continue
            for ref in refs:
                # A branch on a column equated to another through a join (ON eps.lvl_2_id = h2.node_id
                # ... WHERE h2.node_id = 7 OR ...) is a branch on that column too: rows where the
                # branch holds have both equal to the value.
                for member in {ref, *model.class_members(ref)}:
                    base = _base_of(model, member)
                    if base is not None:
                        per_table.setdefault(base[0].qualified_name, set()).add(catalog.column_key(base[1]))
                        nodes.setdefault(base[0].qualified_name, node)
        for name, columns in per_table.items():
            chains.setdefault(name, []).append((columns, nodes[name]))
    for table in ctx.analysis.tables.values():
        if not rule.applies_to(table):
            continue
        # Any one group fully covered by one OR chain satisfies the rule.
        groups = []
        for names in rule.column_groups():
            required = {catalog.column_key(c.name) for c in (catalog.column(table, n) for n in names) if c}
            if required:
                groups.append(required)
        if not groups:
            continue
        satisfied = False
        widened: tuple[set[str], exp.Expression] | None = None
        best: set[str] = set()
        required = groups[0]
        for columns, node in chains.get(table.qualified_name, []):
            for group in groups:
                hit = columns & group
                if hit == group:
                    satisfied = True
                    extra = columns - set().union(*groups)
                    if extra and widened is None:
                        widened = (extra, node)
                # Report against the group the chain comes closest to.
                if len(hit) > len(best) or (len(hit) == len(best) and len(group) < len(required)):
                    best, required = hit, group
        if satisfied:
            if widened is not None:
                extra, node = widened
                yield ctx.finding(
                    "access_filter_widened",
                    Severity.MEDIUM,
                    f"The access filter on {table.name} required by policy \"{what}\" is present, but the same OR chain "
                    f"has a further branch on {_listed(extra)}, which admits rows the policy's columns alone would not "
                    "(for example the manager's own row through assoc_id).",
                    node,
                    suggestion="Keep the access filter to the policy's columns; add other conditions with AND.",
                    rule=rule.name,
                    table=table.qualified_name,
                    extra=sorted(extra),
                )
            continue
        if not best:
            yield ctx.finding(
                "required_predicate",
                severity,
                f"Policy \"{what}\" requires queries on {table.name} to carry an access filter over "
                f"{len(required)} columns ({_listed(required)}), joined with OR against one value. "
                "This query has none, so it returns rows the caller may not be entitled to see.",
                suggestion="Add the access filter.",
                rule=rule.name,
                table=table.qualified_name,
                missing=sorted(required),
            )
        else:
            missing = sorted(required - best)
            yield ctx.finding(
                "required_predicate",
                severity,
                f"The access filter on {table.name} required by policy \"{what}\" is incomplete: it "
                f"leaves out {', '.join(missing)}. Rows reachable only through "
                f"{'that column' if len(missing) == 1 else 'those columns'} are wrongly excluded or, "
                "if the chain was meant to restrict, the check is weaker than intended.",
                suggestion=f"Add {' and '.join(missing)} to the OR chain.",
                rule=rule.name,
                table=table.qualified_name,
                missing=missing,
            )


def _listed(names: Iterable[str], limit: int = 4) -> str:
    ordered = sorted(names)
    if len(ordered) <= limit:
        return ", ".join(ordered)
    return ", ".join(ordered[:limit]) + f" and {len(ordered) - limit} more"


_NUMBERED = re.compile(r"^(.*?)(\d+)(\D*)$")


@check("or_chain_gap", description="An OR chain over numbered columns skips one of them.")
def or_chain_gap(ctx: CheckContext) -> Iterable[Finding]:
    """``LVL_2_ID = v OR LVL_3_ID = v OR LVL_5_ID = v`` where the table also has LVL_4_ID."""
    for model in ctx.cache.models():
        for refs, same_value, node in model.or_groups:
            if not same_value or len(refs) < 3:
                continue
            families: dict[tuple[str, str, str], set[int]] = {}
            for alias, name in refs:
                match = _NUMBERED.match(name)
                if match:
                    families.setdefault((alias, match.group(1), match.group(3)), set()).add(int(match.group(2)))
            missing: list[str] = []
            for (alias, prefix, suffix), numbers in families.items():
                if len(numbers) < 2:
                    continue
                source = model.sources.get(alias)
                if source is None or source.table is None:
                    continue
                available = set()
                for column in source.table.columns:
                    match = _NUMBERED.match(ctx.catalog.column_key(column.name))
                    if match and (match.group(1), match.group(3)) == (prefix, suffix):
                        available.add(int(match.group(2)))
                wanted = set(numbers)
                # A parallel family in the same chain (LVL_n_ID beside LVL_n_FN) should cover the same levels.
                for (other_alias, other_prefix, other_suffix), other in families.items():
                    if other_alias == alias and other_prefix == prefix and other_suffix != suffix:
                        wanted |= other
                low, high = min(wanted), max(wanted)
                for number in sorted(available):
                    if low <= number <= high and number not in numbers:
                        missing.append(f"{prefix}{number}{suffix}")
            if missing:
                yield ctx.finding(
                    "or_chain_gap",
                    Severity.MEDIUM,
                    f"The OR chain covers a run of numbered columns but skips {', '.join(missing)}, "
                    "which the table also has. Rows that match only through the skipped column are left out.",
                    node,
                    suggestion=f"Add {' and '.join(missing)} to the chain, or confirm the gap is intended.",
                    missing=missing,
                )


# ------------------------------------------------------------ soft deletes


_LIVE_WORDS = r"(non-?\s?deleted|not\s+deleted|undeleted|active|live|current|existing|surviving)"


def _question_asks_live(question: str | None, table_name: str) -> bool:
    """The question says "non-deleted tenants", "active customers" and the like about this table."""
    if not question:
        return False
    stem = re.sub(r"(ies|s)$", "", table_name.lower().rsplit(".", 1)[-1].rsplit("_", 1)[-1])
    if len(stem) < 3:
        return False
    pattern = _LIVE_WORDS + r"\W+(?:\w+\W+){0,2}?" + re.escape(stem[:-1] if stem.endswith("y") else stem)
    return re.search(pattern, question, re.IGNORECASE) is not None


@check("soft_deleted_rows", description="A table with a soft-delete marker is read without looking at it.")
def soft_deleted_rows(ctx: CheckContext) -> Iterable[Finding]:
    column = ctx.catalog.soft_delete_column
    if not column:
        return
    for miss in _Tracer.for_names(ctx, (column,), MENTIONED).run():
        if miss.in_view:
            continue  # the view's author decided what the view shows
        source = miss.model.sources[miss.alias]
        required = ctx.catalog.soft_delete_policy == "required"
        asked = _question_asks_live(ctx.question, miss.table.name)
        if miss.bridge or not miss.counted:
            # A parent or pass-through table: whether its own deletion matters is a convention
            # that differs between organisations, so this stays a low-key note.
            severity = Severity.LOW
        elif miss.lookup:
            # Used only to look values up for rows of another table, and not listed or grouped by:
            # the deleted rows are not themselves in the result. Unless the question asks for the
            # live ones, that is the usual convention (deletion is not transitive).
            severity = Severity.MEDIUM if asked else Severity.LOW
        else:
            severity = Severity.HIGH if required else Severity.MEDIUM
        yield ctx.finding(
            "soft_deleted_rows",
            severity,
            (
                f"{source.label} marks deleted rows with {column}, and the query never looks "
                "at that column, so deleted rows are counted along with live ones."
                + (" Here the table is only passed through on the way to another one." if miss.bridge else "")
                + (
                    " Here it is a parent of the rows being counted, not what is counted."
                    if not miss.counted and not miss.bridge
                    else ""
                )
                + (
                    " Here it is only used to look values up for rows of another table; its own "
                    "deleted rows are not listed or counted."
                    if miss.lookup and miss.counted and not miss.bridge
                    else ""
                )
                + (" The question asks for the live ones." if asked and miss.lookup else "")
                + (" Policy here is that deleted rows are excluded unless asked for." if required and not miss.lookup else "")
            ),
            suggestion=f"Filter on {miss.alias}.{column} unless deleted rows are wanted.",
            table=miss.table.qualified_name,
            alias=miss.alias,
            bridge=miss.bridge,
            counted=miss.counted,
            lookup=miss.lookup,
            table_name=miss.table.name,
            policy=ctx.catalog.soft_delete_policy,
        )


# ---------------------------------------------------------- history tables

# Columns that say which version of a row is in force.
_VERSION_END_STRONG = ("effective_to", "effective_until", "effective_end", "superseded_at")
_VERSION_END_WEAK = ("valid_to", "valid_until", "valid_end")
_VERSION_FLAGS = ("is_current", "current_flag", "is_latest", "is_active_version")
_VERSION_START = ("effective_from", "valid_from", "effective_start", "valid_start")


def _version_marker(ctx: CheckContext, table: Table) -> Column | None:
    """The column that marks a row version as superseded, if the table keeps history.

    ``valid_to`` on its own often just says when a thing (a coupon, an offer)
    stops applying. It counts as a version marker only when the table's key
    includes a start date, which is what makes several versions of one thing possible.
    """
    catalog = ctx.catalog
    for name in _VERSION_END_STRONG + _VERSION_FLAGS:
        column = catalog.column(table, name)
        if column is not None:
            return column
    starts = {catalog.column_key(c.name) for c in (catalog.column(table, n) for n in _VERSION_START) if c}
    if starts and any(key & starts for key in catalog.unique_key_sets(table)):
        for name in _VERSION_END_WEAK:
            column = catalog.column(table, name)
            if column is not None:
                return column
    return None


@check("history_rows", description="A table that keeps old versions of rows is read without choosing a version.")
def history_rows(ctx: CheckContext) -> Iterable[Finding]:
    def columns_for(table: Table) -> list[Column]:
        marker = _version_marker(ctx, table)
        if marker is None:
            return []
        extra = [ctx.catalog.column(table, n) for n in _VERSION_START + _VERSION_FLAGS]
        return [marker] + [c for c in extra if c is not None and c is not marker]

    for miss in _Tracer(ctx, columns_for, MENTIONED).run():
        if miss.in_view:
            continue
        source = miss.model.sources[miss.alias]
        marker = miss.column
        yield ctx.finding(
            "history_rows",
            Severity.MEDIUM,
            (
                f"{source.label} keeps every past version of a row ({marker.name} marks when a "
                "version stops applying), and the query never looks at that column, so old "
                "versions are read alongside the current one."
            ),
            suggestion=f"Filter {miss.alias}.{marker.name} IS NULL for current rows, or pick the version in force on a date.",
            table=miss.table.qualified_name,
            alias=miss.alias,
            marker=marker.name,
        )


# --------------------------------------------------------- snapshot tables

_SNAPSHOT_NAMES = ("snap_dt", "snapshot_dt", "snapshot_date", "snap_date", "as_of_dt", "as_of_date")


@check("snapshot_grain", description="A periodic snapshot table is read without choosing which snapshots.")
def snapshot_grain(ctx: CheckContext) -> Iterable[Finding]:
    declared: dict[str, bool] = {}

    def columns_for(table: Table) -> list[Column]:
        column = ctx.catalog.snapshot_column_of(table)
        if column is not None:
            declared[table.qualified_name] = True
            return [column]
        # Not declared: recognise the usual shape, a snapshot date that is part of the key.
        keys = ctx.catalog.unique_key_sets(table)
        for name in _SNAPSHOT_NAMES:
            column = ctx.catalog.column(table, name)
            if column is not None and any(ctx.catalog.column_key(column.name) in key for key in keys):
                declared[table.qualified_name] = False
                return [column]
        return []

    for miss in _Tracer(ctx, columns_for, GRAIN).run():
        source = miss.model.sources[miss.alias]
        if _distinct_entity_keys(ctx, miss):
            continue  # SELECT DISTINCT assoc_id FROM snapshot: one row per thing whatever the snapshots
        sure = declared.get(miss.table.qualified_name, False)
        distinct_keys = _counts_distinct_entity_keys(ctx, miss)
        yield ctx.finding(
            "snapshot_grain",
            Severity.MEDIUM if distinct_keys or not sure else Severity.HIGH,
            (
                f"{source.label} holds one row per thing for every snapshot ({miss.column.name}). "
                "The query neither fixes the snapshot, nor ties it to another date column, nor "
                "groups by it, so every thing is counted once per snapshot."
                + (
                    " Here the only aggregate over it is a COUNT(DISTINCT key), which collapses the repeats; "
                    "what remains is that a row from another snapshot (a restated status, say) can still pass the filters."
                    if distinct_keys
                    else ""
                )
            ),
            suggestion=(
                f"Pick the snapshot: for example {miss.alias}.{miss.column.name} = a period-end column, "
                "or the latest snapshot date."
            ),
            table=miss.table.qualified_name,
            alias=miss.alias,
            column=miss.column.name,
        )


def _counts_distinct_entity_keys(ctx: CheckContext, miss: Miss) -> bool:
    """Every aggregate that reads the snapshot table is a COUNT(DISTINCT ...) over its entity key
    (the key less the snapshot date), and nothing else of the table is projected or grouped by:
    the per-snapshot repeats cannot inflate the count."""
    model = miss.model
    aggregates = model.own_aggregates()
    if not aggregates:
        return False
    snapshot = ctx.catalog.column_key(miss.column.name)
    keys = [set(k) - {snapshot} for k in ctx.catalog.unique_key_sets(miss.table)]
    if not keys:
        return False
    reads_alias = False
    for agg in aggregates:
        columns = [c for c in agg.find_all(exp.Column) if c.table == miss.alias]
        if not columns:
            continue
        reads_alias = True
        if not isinstance(agg, exp.Count) or not isinstance(agg.this, exp.Distinct):
            return False
        counted = {ctx.catalog.column_key(c.name) for c in columns}
        if not any(counted <= key for key in keys):
            return False
    if not reads_alias:
        return False
    for projection in model.select.expressions:
        for node in walk_own(projection):
            if isinstance(node, exp.Column) and node.table == miss.alias and node.find_ancestor(exp.AggFunc) is None:
                return False  # a value of the table at row grain: which snapshot's?
    group = model.select.args.get("group")
    if group is not None and any(isinstance(c, exp.Column) and c.table == miss.alias for e in group.expressions for c in e.find_all(exp.Column)):
        return False
    return True


def _distinct_entity_keys(ctx: CheckContext, miss: Miss) -> bool:
    """The SELECT is DISTINCT over nothing but key columns of the snapshot table other than the
    snapshot date itself: the per-snapshot repeats collapse, nothing is counted per snapshot."""
    select = miss.model.select
    if select.args.get("distinct") is None or miss.model.own_aggregates():
        return False
    snapshot = ctx.catalog.column_key(miss.column.name)
    keys = [set(k) - {snapshot} for k in ctx.catalog.unique_key_sets(miss.table)]
    if not keys:
        return False
    projected: set[str] = set()
    for projection in select.expressions:
        node = strip(unalias(projection))
        if not isinstance(node, exp.Column) or node.table != miss.alias:
            return False
        projected.add(ctx.catalog.column_key(node.name))
    return any(projected and projected <= key for key in keys)


# ------------------------------------------------------- partition filters


@check("partition_filter", description="A partitioned table is read without a usable partition filter.")
def partition_filter(ctx: CheckContext) -> Iterable[Finding]:
    catalog = ctx.catalog
    for model in ctx.cache.models():
        for alias, source in model.sources.items():
            table = source.table
            if table is None or not table.partition_columns:
                continue
            keys = {catalog.column_key(c) for c in table.partition_columns}
            constant = False
            variable: list[exp.Expression] = []
            for conjunct in model.conjuncts:
                columns = [c for c in model.local_columns(conjunct.expr) if c.table == alias and c.name in keys]
                if not columns:
                    continue
                others = [
                    c for c in conjunct.expr.find_all(exp.Column) if not (c.table == alias and c.name in keys)
                ]
                if not others and conjunct.expr.find(exp.Select) is None:
                    constant = True
                else:
                    variable.append(conjunct.expr)
            if constant:
                continue
            looked_up = any(
                isinstance(a, (exp.Max, exp.Min))
                and isinstance(strip(a.this), exp.Column)
                and strip(a.this).table == alias
                and strip(a.this).name in keys
                for a in model.own_aggregates()
            )
            if looked_up and not variable:
                continue  # MAX(partition column): the engine answers this from partition metadata
            if inside_view(source.node) and not variable:
                continue  # reported for the outer query only if it adds no filter of its own
            if variable:
                yield ctx.finding(
                    "partition_filter",
                    Severity.LOW,
                    f"{source.label} is partitioned by {', '.join(table.partition_columns)}, and the only "
                    f"conditions on it ({ctx.snippet(variable[0])}) depend on a subquery or another "
                    "column. The engine may not be able to skip partitions, so the query can scan "
                    "the whole table.",
                    variable[0],
                    suggestion="Add a constant bound on the partition column as well.",
                    table=table.qualified_name,
                    kind="non_constant",
                )
            else:
                yield ctx.finding(
                    "partition_filter",
                    Severity.MEDIUM,
                    f"{source.label} is partitioned by {', '.join(table.partition_columns)}, and the "
                    "query puts no condition on it, so every partition is scanned.",
                    suggestion="Filter on the partition column.",
                    table=table.qualified_name,
                    kind="missing",
                )
