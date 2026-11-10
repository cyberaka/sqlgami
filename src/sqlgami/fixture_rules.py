"""Declared column rules for synthetic fixtures (ADR 0002 follow-up): a host says what its data
must look like, in the ``fixtures:`` block, and the generator obeys. Nothing is inferred from
column names; a rule names its columns and is validated against the catalogue.

    fixtures:
      rules:
        - {kind: date_step, column: hr.pay_period_dim.ppe_dt, per: [freq_cd], step: {B: 14 days, W: 7 days}, start: 2024-01-05}
        - {kind: one_true, column: hr.pay_period_dim.is_current_flag, per: [freq_cd], at: middle}
        - {kind: sequence, column: hr.pay_period_dim.period_seq, order_by: ppe_dt, per: [freq_cd], zero_where: is_current_flag}
        - kind: grid
          table: hr.emp_period_snapshot
          per: {assoc_id: hr.employee_dim.assoc_id, period_end_dt: hr.pay_period_dim.ppe_dt}
          where: {hr.pay_period_dim.freq_cd: B}
          between: {column: period_end_dt, start: hr.employee_dim.hire_dt, end: hr.employee_dim.term_dt}
          copy: {hire_dt: hr.employee_dim.hire_dt, term_dt: hr.employee_dim.term_dt}
          snapshot: {column: snap_dt, equals: period_end_dt, restated_rate: 0.3}

``derive`` sets a column from a SQL expression over the row's other columns (``{kind: derive,
column: hr.pay_period_dim.period_start_dt, sql: "ppe_dt - INTERVAL 13 DAY"}``), evaluated by
DuckDB after the table is generated; ``date_step`` spaces a date column evenly within each group
(``anchor: 2026-06-20`` puts that date on the middle row so the dates run either side of it); ``one_true`` makes exactly one row
per group TRUE; ``sequence`` numbers rows consecutively in ``order_by`` order, zero at the row
where ``zero_where`` is TRUE (negative before, positive after); ``grid`` replaces a table's
generated rows with one row per combination of the named parent values (filtered, bounded by a
date range from the first parent), copying parent columns and generating the rest, with an
optional snapshot date equal to the period date plus a share of restated rows.
"""

from __future__ import annotations

import datetime as dt
import random
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .errors import ConfigurationError

KINDS = ("date_step", "one_true", "sequence", "grid", "derive")


@dataclass
class ColumnRule:
    kind: str
    table: str  # logical table name the rule acts on
    spec: dict[str, Any] = field(default_factory=dict)


def parse_rules(items: Sequence[dict[str, Any]] | None) -> list[ColumnRule]:
    rules: list[ColumnRule] = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            raise ConfigurationError(f"fixture rule {item!r}: kind must be one of {', '.join(KINDS)}")
        kind = str(item["kind"])
        if kind == "grid":
            table = str(item.get("table") or "").lower()
            if not table or not isinstance(item.get("per"), dict) or not item["per"]:
                raise ConfigurationError("a grid rule needs table: and per: {column: parent.table.column, ...}")
        else:
            column = str(item.get("column") or "")
            if column.count(".") < 1:
                raise ConfigurationError(f"fixture rule {kind}: column must be written table.column")
            table = column.rpartition(".")[0].lower()
        rules.append(ColumnRule(kind, table, {k: v for k, v in item.items() if k != "kind"}))
    return rules


def validate_rules(rules: Sequence[ColumnRule], columns_of: Callable[[str], list[str] | None]) -> list[str]:
    """Problems as text; ``columns_of(table)`` gives a table's column names or None."""
    problems: list[str] = []

    def check(table: str, column: str, what: str) -> None:
        names = columns_of(table)
        if names is None:
            problems.append(f"{what}: unknown table {table}")
        elif column.lower() not in [n.lower() for n in names]:
            problems.append(f"{what}: {table} has no column {column}")

    for rule in rules:
        spec = rule.spec
        if rule.kind != "grid":
            check(rule.table, spec["column"].rpartition(".")[2], f"{rule.kind} {spec['column']}")
            for per in spec.get("per") or []:
                check(rule.table, per, f"{rule.kind} per {per}")
            if rule.kind == "sequence":
                check(rule.table, str(spec.get("order_by") or ""), f"sequence order_by")
                if spec.get("zero_where"):
                    check(rule.table, str(spec["zero_where"]), "sequence zero_where")
            if rule.kind == "date_step" and not spec.get("step"):
                problems.append(f"date_step {spec['column']}: step is required ('14 days' or {{group: '14 days'}})")
            if rule.kind == "derive" and not str(spec.get("sql") or "").strip():
                problems.append(f"derive {spec['column']}: sql is required")
        else:
            for column, source in spec["per"].items():
                check(rule.table, column, f"grid per {column}")
                src_table, _, src_col = str(source).rpartition(".")
                check(src_table.lower(), src_col, f"grid per {column} source {source}")
            for column, source in (spec.get("copy") or {}).items():
                check(rule.table, column, f"grid copy {column}")
                src_table, _, src_col = str(source).rpartition(".")
                check(src_table.lower(), src_col, f"grid copy {column} source {source}")
            between = spec.get("between") or {}
            if between:
                check(rule.table, str(between.get("column") or ""), "grid between column")
                for edge in ("start", "end"):
                    if between.get(edge):
                        src_table, _, src_col = str(between[edge]).rpartition(".")
                        check(src_table.lower(), src_col, f"grid between {edge}")
            snapshot = spec.get("snapshot") or {}
            if snapshot:
                check(rule.table, str(snapshot.get("column") or ""), "grid snapshot column")
                check(rule.table, str(snapshot.get("equals") or ""), "grid snapshot equals")
    return problems


# ------------------------------------------------------------------ column rules


def _step(text: Any) -> dt.timedelta | tuple[str, int]:
    match = re.fullmatch(r"\s*(\d+)\s*(day|days|week|weeks|month|months)\s*", str(text))
    if not match:
        raise ConfigurationError(f"date_step: step {text!r} is not 'N days', 'N weeks' or 'N months'")
    n, unit = int(match.group(1)), match.group(2).rstrip("s")
    if unit == "day":
        return dt.timedelta(days=n)
    if unit == "week":
        return dt.timedelta(weeks=n)
    return ("month", n)


def _add(date: dt.date, step: dt.timedelta | tuple[str, int], times: int) -> dt.date:
    if isinstance(step, dt.timedelta):
        return date + step * times
    months = step[1] * times
    year, month = divmod(date.month - 1 + months, 12)
    return dt.date(date.year + year, month + 1, min(date.day, 28))


def apply_derived(rules: Sequence[ColumnRule], table: str, names: list[str], types: Sequence[str], rows: list[list[Any]]) -> None:
    """``derive`` rules: each column set from its SQL expression over the row, through DuckDB
    (the fixture engine), in the order declared."""
    derived = [r for r in rules if r.table == table and r.kind == "derive"]
    if not derived or not rows:
        return
    import duckdb

    conn = duckdb.connect()
    conn.execute("SET TimeZone = 'UTC'")
    conn.execute("CREATE TABLE t (" + ", ".join(f'"{n}" {t}' for n, t in zip(names, types)) + ")")
    conn.executemany("INSERT INTO t VALUES (" + ", ".join("?" for _ in names) + ")", [tuple(r) for r in rows])
    conn.execute("ALTER TABLE t ADD COLUMN __ord INTEGER")
    conn.execute("UPDATE t SET __ord = rowid")
    for rule in derived:
        column = rule.spec["column"].rpartition(".")[2].lower()
        try:
            conn.execute(f'UPDATE t SET "{column}" = ({rule.spec["sql"]})')
        except Exception as exc:
            raise ConfigurationError(f"derive {rule.spec['column']}: {exc}") from exc
    fresh = conn.execute("SELECT " + ", ".join(f'"{n}"' for n in names) + " FROM t ORDER BY __ord").fetchall()
    for i, row in enumerate(fresh):
        rows[i][:] = list(row)


def _groups(rows: list[list[Any]], names: list[str], per: Sequence[str]) -> dict[tuple, list[int]]:
    groups: dict[tuple, list[int]] = {}
    indexes = [names.index(p.lower()) for p in per]
    for i, row in enumerate(rows):
        groups.setdefault(tuple(row[j] for j in indexes), []).append(i)
    return groups


def apply_column_rules(rules: Sequence[ColumnRule], table: str, names: list[str], rows: list[list[Any]]) -> None:
    """Apply date_step, one_true and sequence rules declared on ``table`` to ``rows`` in place.
    Applied in the order declared, so a sequence can anchor on a one_true set just before."""
    for rule in rules:
        if rule.table != table or rule.kind == "grid":
            continue
        spec = rule.spec
        column = spec["column"].rpartition(".")[2].lower()
        col = names.index(column)
        per = [str(p) for p in (spec.get("per") or [])]
        if rule.kind == "date_step":
            start = spec.get("start")
            anchor = spec.get("anchor")
            steps = spec["step"]
            for key, members in _groups(rows, names, per).items():
                step = _step(steps.get(str(key[0]), next(iter(steps.values()))) if isinstance(steps, dict) and key else steps if not isinstance(steps, dict) else next(iter(steps.values())))
                if anchor:
                    # the middle row carries the anchor date; earlier rows run back from it
                    middle = len(members) // 2
                    for n, i in enumerate(members):
                        rows[i][col] = _add(dt.date.fromisoformat(str(anchor)), step, n - middle)
                else:
                    start_date = dt.date.fromisoformat(str(start)) if start else dt.date(2024, 1, 5)
                    for n, i in enumerate(members):
                        rows[i][col] = _add(start_date, step, n)
        elif rule.kind == "one_true":
            at = str(spec.get("at") or "middle")
            for members in _groups(rows, names, per).values():
                for i in members:
                    rows[i][col] = False
                if members:
                    pick = members[0] if at == "first" else members[-1] if at == "last" else members[len(members) // 2]
                    rows[pick][col] = True
        elif rule.kind == "sequence":
            order_col = names.index(str(spec["order_by"]).lower())
            zero_col = names.index(str(spec["zero_where"]).lower()) if spec.get("zero_where") else None
            for members in _groups(rows, names, per).values():
                ordered = sorted(members, key=lambda i: (rows[i][order_col] is None, rows[i][order_col]))
                zero_at = next((k for k, i in enumerate(ordered) if zero_col is not None and rows[i][zero_col] is True), None)
                for k, i in enumerate(ordered):
                    rows[i][col] = k - zero_at if zero_at is not None else k + 1


# ------------------------------------------------------------------ grid


def grid_rows(
    rule: ColumnRule,
    names: list[str],
    data: dict[str, tuple[list[str], list[tuple]]],
    generate: Callable[[str, int], Any],
    rng: random.Random,
) -> list[list[Any]]:
    """Rows for a grid table: one per combination of the parents' values named in ``per``,
    filtered by ``where``, bounded by ``between``; ``copy`` columns taken from the first parent,
    every other column from ``generate(column, index)``."""
    spec = rule.spec
    per: dict[str, str] = {str(k).lower(): str(v) for k, v in spec["per"].items()}
    parents: list[tuple[str, str, str]] = []  # (table column, parent table, parent column)
    for column, source in per.items():
        src_table, _, src_col = source.rpartition(".")
        parents.append((column, src_table.lower(), src_col.lower()))
    where: dict[str, Any] = {str(k).lower(): v for k, v in (spec.get("where") or {}).items()}
    copy: dict[str, str] = {str(k).lower(): str(v) for k, v in (spec.get("copy") or {}).items()}
    between = spec.get("between") or {}
    snapshot = spec.get("snapshot") or {}

    def parent_rows(table: str) -> list[dict[str, Any]]:
        if table not in data:
            raise ConfigurationError(f"grid {rule.table}: parent {table} must be generated first (declare its rows or foreign keys)")
        cols, rws = data[table]
        found = [dict(zip([c.lower() for c in cols], r)) for r in rws]
        for key, value in where.items():
            t, _, c = key.rpartition(".")
            if t.lower() == table:
                found = [r for r in found if str(r.get(c)) == str(value)]
        return found

    first_col, first_table, first_key = parents[0]
    first = parent_rows(first_table)
    others = [(col, parent_rows(t), c) for col, t, c in parents[1:]]
    rows: list[list[Any]] = []
    index = 0
    for p1 in first:
        combos: list[dict[str, Any]] = [{first_col: p1[first_key]}]
        for col, prow_list, c in others:
            combos = [{**combo, col: prow[c]} for combo in combos for prow in prow_list]
        for combo in combos:
            if between:
                date_value = combo.get(str(between.get("column")).lower())
                start = p1.get(str(between.get("start", "")).rpartition(".")[2].lower()) if between.get("start") else None
                end = p1.get(str(between.get("end", "")).rpartition(".")[2].lower()) if between.get("end") else None
                if isinstance(date_value, dt.date) and ((isinstance(start, dt.date) and date_value < start) or (isinstance(end, dt.date) and date_value > end)):
                    continue
            row: list[Any] = []
            for name in names:
                if name in combo:
                    row.append(combo[name])
                elif name in copy:
                    row.append(p1.get(copy[name].rpartition(".")[2].lower()))
                elif snapshot and name == str(snapshot.get("column")).lower():
                    row.append(combo.get(str(snapshot.get("equals")).lower()))
                else:
                    row.append(generate(name, index))
            rows.append(row)
            index += 1
            rate = float(snapshot.get("restated_rate") or 0.0) if snapshot else 0.0
            if rate and rng.random() < rate:
                restated = list(row)
                snap_col = names.index(str(snapshot["column"]).lower())
                if isinstance(restated[snap_col], dt.date):
                    restated[snap_col] = restated[snap_col] + dt.timedelta(days=14)
                    rows.append(restated)
    return rows
