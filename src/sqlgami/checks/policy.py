"""Checks driven by catalog policy hints: tenant isolation and soft deletes.

Both follow a column through derived tables, CTEs and expanded views, so a
filter applied outside a view counts for the base table inside it.
"""

from __future__ import annotations

from typing import Iterable

from sqlglot import exp
from sqlglot.optimizer.scope import Scope

from ..catalog import Table
from ..model import ColRef, SelectModel, strip, unalias, walk_own
from ..report import Finding, Severity
from .base import CheckContext, check

CONSTRAINED = "constrained"  # the column must be pinned to a value
MENTIONED = "mentioned"  # the column must at least be looked at


class _Tracer:
    def __init__(self, ctx: CheckContext, column_name: str | tuple[str, ...], mode: str) -> None:
        self.ctx = ctx
        # Several names mean "any one of these": the first that a table has is reported,
        # and covering any of them satisfies the check.
        self.column_names: tuple[str, ...] = (column_name,) if isinstance(column_name, str) else tuple(column_name)
        self.mode = mode
        self.misses: dict[int, tuple[SelectModel, str, Table, bool]] = {}
        self.satisfied: set[int] = set()
        self._visited: set[tuple[int, frozenset[str]]] = set()

    def run(self) -> list[tuple[SelectModel, str, Table, bool]]:
        root = self.ctx.analysis.root_scope
        if root is not None:
            self._visit(root, frozenset(), frozenset())
        return [miss for key, miss in self.misses.items() if key not in self.satisfied]

    def _visit(self, scope: Scope, external: frozenset[str], outer: frozenset[ColRef]) -> None:
        key = (id(scope), external)
        if key in self._visited:
            return
        self._visited.add(key)
        expression = scope.expression
        if isinstance(expression, exp.SetOperation):
            names = list(expression.named_selects)
            positions = [i for i, name in enumerate(names) if name in external]
            for branch in scope.union_scopes:
                branch_names = (
                    list(branch.expression.named_selects)
                    if isinstance(branch.expression, exp.Query)
                    else []
                )
                mapped = frozenset(branch_names[i] for i in positions if i < len(branch_names))
                self._visit(branch, mapped, outer)
            return
        if not isinstance(expression, exp.Select):
            return
        model = self.ctx.cache.model(scope)
        covered = self._covered(model, external, outer)

        for alias, source in model.sources.items():
            if source.kind == "table" and source.table is not None:
                present = [
                    c for c in (self.ctx.catalog.column(source.table, n) for n in self.column_names) if c is not None
                ]
                if not present:
                    continue
                refs = [(alias, self.ctx.catalog.storage_key(c.name)) for c in present]
                node_id = id(source.node)
                if any(ref in covered for ref in refs):
                    self.satisfied.add(node_id)
                else:
                    self.misses.setdefault(
                        node_id, (model, alias, source.table, self._indirect(model, alias, covered))
                    )
            elif source.kind == "derived" and source.scope is not None:
                names = frozenset(column for (a, column) in covered if a == alias)
                self._visit(source.scope, names, outer | covered if source.lateral else outer)

        for sub in scope.subquery_scopes:
            self._visit(sub, frozenset(), outer | covered)

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
            for node in walk_own(model.select):
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

    def _indirect(self, model: SelectModel, alias: str, covered: frozenset[ColRef]) -> bool:
        """True when the table is at least joined to something that is filtered."""
        covered_aliases = {a for (a, _) in covered}
        for a, b, _ in model.equalities:
            if a[0] == alias and b[0] in covered_aliases:
                return True
            if b[0] == alias and a[0] in covered_aliases:
                return True
        return False


def _is_plain_projection(column: exp.Column) -> bool:
    """A column that is merely selected, as opposed to tested or computed with."""
    node: exp.Expression = column
    if isinstance(node.parent, exp.Alias):
        node = node.parent
    return isinstance(node.parent, exp.Select) and node.arg_key == "expressions"


@check("missing_tenant_filter", description="A table holding several tenants' rows is read without a tenant filter.")
def missing_tenant_filter(ctx: CheckContext) -> Iterable[Finding]:
    column = ctx.catalog.tenant_column
    if not column:
        return
    for model, alias, table, indirect in _Tracer(ctx, column, CONSTRAINED).run():
        source = model.sources[alias]
        if indirect:
            message = (
                f"{source.label} holds rows for every tenant and is not filtered on {column} "
                "itself; it is only joined to a table that is. If key values repeat across "
                "tenants, rows from other tenants are included."
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
            suggestion=f"Filter {alias}.{column}, or join on {column} as well as the key.",
            table=table.qualified_name,
            alias=alias,
            indirect=indirect,
        )


@check("soft_deleted_rows", description="A table with a soft-delete marker is read without looking at it.")
def soft_deleted_rows(ctx: CheckContext) -> Iterable[Finding]:
    column = ctx.catalog.soft_delete_column
    if not column:
        return
    for model, alias, table, _ in _Tracer(ctx, column, MENTIONED).run():
        source = model.sources[alias]
        yield ctx.finding(
            "soft_deleted_rows",
            Severity.MEDIUM,
            (
                f"{source.label} marks deleted rows with {column}, and the query never looks "
                "at that column, so deleted rows are counted along with live ones."
            ),
            suggestion=f"Filter on {alias}.{column} unless deleted rows are wanted.",
            table=table.qualified_name,
            alias=alias,
        )


# Columns that say which version of a row is in force. A table with one of the first group
# keeps history: several rows per thing, one current.
_VERSION_END = ("effective_to", "valid_to", "valid_until", "effective_until", "effective_end", "valid_end", "superseded_at")
_VERSION_FLAGS = ("is_current", "current_flag", "is_latest", "is_active_version")
_VERSION_START = ("effective_from", "valid_from", "effective_start", "valid_start")


@check("history_rows", description="A table that keeps old versions of rows is read without choosing a version.")
def history_rows(ctx: CheckContext) -> Iterable[Finding]:
    markers = _VERSION_END + _VERSION_FLAGS
    for model, alias, table, _ in _Tracer(ctx, markers + _VERSION_START, MENTIONED).run():
        marker = next((ctx.catalog.column(table, n) for n in markers if ctx.catalog.column(table, n) is not None), None)
        if marker is None:
            continue  # only a start date: not clearly a history table
        source = model.sources[alias]
        yield ctx.finding(
            "history_rows",
            Severity.MEDIUM,
            (
                f"{source.label} keeps every past version of a row ({marker.name} marks when a "
                "version stops applying), and the query never looks at that column, so old "
                "versions are read alongside the current one."
            ),
            suggestion=f"Filter {alias}.{marker.name} IS NULL for current rows, or pick the version in force on a date.",
            table=table.qualified_name,
            alias=alias,
            marker=marker.name,
        )
