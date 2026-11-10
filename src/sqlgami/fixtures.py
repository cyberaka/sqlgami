"""Small, adversarial, bit-exact fixture datasets built from a schema and declared rules.

A fixture is a tiny database in the shape of the host's schema — keys and foreign keys from
the catalogue, declared value domains, NULL rates, duplicates and restatements, ties, empty
groups, edge dates, the parameter values the host's questions are asked with, and "critical
rows" the host writes down verbatim (the rows a known bug needs to diverge on). Built offline,
stored with the compiled bundle (ADR 0002), loaded into an in-memory DuckDB at scoring time,
so a candidate's result can be compared with a precomputed reference in a few milliseconds.

Everything is deterministic: the same schema, rules, profile and seed give the same rows,
byte for byte. Fixtures are synthetic by default; building them from real data is a separate,
explicit choice (not done here).

Rules, as YAML/dict (the ``fixtures:`` block of a compile config) — every key optional:

```yaml
fixtures:
  seed: 1
  profiles: [clean, nulls, duplicates, ties, sparse, edges]   # one fixture each
  rows: {hr.emp_period_snapshot: 400, hr.employee_dim: 60}     # per-table row counts
  values: {hr.emp_period_snapshot.status_cd: [A, L, T]}         # value domains
  null_rate: {hr.emp_period_snapshot.status_cd: 0.05}           # per column, 0..1
  dates: {start: 2024-01-01, end: 2025-12-31}
  params: {manager_id: [12003, 52002]}                           # values questions are asked with
  param_columns: {manager_id: [hr.emp_period_snapshot.lvl_3_id, hr.emp_period_snapshot.lvl_7_id_fn]}
  critical_rows:                                                 # rows that must exist, verbatim
    hr.employee_dim: [{assoc_id: 12028, hire_dt: 2021-03-01, term_dt: null}]
```
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from sqlglot import exp

from . import types as T
from .catalog import Catalog, Table
from .errors import ConfigurationError

PROFILES = ("clean", "nulls", "duplicates", "ties", "sparse", "edges")
DEFAULT_PARENT_ROWS = 12
DEFAULT_CHILD_ROWS = 40


@dataclass
class FixtureRules:
    seed: int = 1
    profiles: tuple[str, ...] = PROFILES
    rows: dict[str, int] = field(default_factory=dict)
    values: dict[str, list[Any]] = field(default_factory=dict)
    null_rate: dict[str, float] = field(default_factory=dict)
    dates: tuple[dt.date, dt.date] = (dt.date(2024, 1, 1), dt.date(2025, 12, 31))
    params: dict[str, list[Any]] = field(default_factory=dict)
    param_columns: dict[str, list[str]] = field(default_factory=dict)
    critical_rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    column_rules: list[Any] = field(default_factory=list)  # declared date_step / one_true / sequence / grid rules

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FixtureRules":
        data = dict(data or {})
        dates = data.get("dates") or {}
        start = _date(dates.get("start")) if dates.get("start") else dt.date(2024, 1, 1)
        end = _date(dates.get("end")) if dates.get("end") else dt.date(2025, 12, 31)
        profiles = tuple(str(p) for p in (data.get("profiles") or PROFILES))
        unknown = [p for p in profiles if p not in PROFILES]
        if unknown:
            raise ConfigurationError(f"unknown fixture profile(s) {unknown}; known: {', '.join(PROFILES)}")
        return cls(
            seed=int(data.get("seed", 1)),
            profiles=profiles,
            rows={str(k).lower(): int(v) for k, v in (data.get("rows") or {}).items()},
            values={str(k).lower(): list(v) for k, v in (data.get("values") or {}).items()},
            null_rate={str(k).lower(): float(v) for k, v in (data.get("null_rate") or {}).items()},
            dates=(start, end),
            params={str(k): list(v) if isinstance(v, (list, tuple)) else [v] for k, v in (data.get("params") or {}).items()},
            param_columns={str(k): [str(c).lower() for c in v] for k, v in (data.get("param_columns") or {}).items()},
            critical_rows={str(k).lower(): [dict(r) for r in v] for k, v in (data.get("critical_rows") or {}).items()},
            column_rules=__import__("sqlgami.fixture_rules", fromlist=["parse_rules"]).parse_rules(data.get("rules")),
        )


@dataclass
class Fixture:
    name: str
    profile: str
    seed: int
    tables: dict[str, tuple[list[str], list[tuple]]]  # qualified table name -> (columns, rows)
    ddl: str  # DuckDB DDL that creates the tables
    types: dict[str, list[str]] = field(default_factory=dict)  # qualified table name -> DuckDB column types
    # The CSV text of each table as written to disk: the stored truth, hashed and loaded as is.
    texts: dict[str, str] = field(default_factory=dict)

    def row_count(self) -> int:
        return sum(len(rows) for _, rows in self.tables.values())

    def text_of(self, name: str) -> str:
        if name not in self.texts:
            columns, rows = self.tables[name]
            self.texts[name] = _csv_text(columns, rows)
        return self.texts[name]

    def digest(self) -> str:
        """A content hash of the tables' DDL and CSV text: the same logical schema and rules give
        the same digest on any machine, from any dialect's DDL file, whether built or read back.
        The views' text is hashed separately (:meth:`views_digest`): two dialect files may spell
        an equivalent view differently."""
        h = hashlib.sha256()
        for statement in self.ddl.split(";\n"):
            if statement.strip() and not statement.lstrip().upper().startswith("CREATE VIEW"):
                h.update(statement.strip().encode("utf-8"))
        for name in sorted(self.tables):
            h.update(name.encode("utf-8"))
            h.update(self.text_of(name).encode("utf-8"))
        return h.hexdigest()

    def views_digest(self) -> str:
        h = hashlib.sha256()
        for statement in self.ddl.split(";\n"):
            if statement.lstrip().upper().startswith("CREATE VIEW"):
                h.update(statement.strip().encode("utf-8"))
        return h.hexdigest()

    def connect(self) -> Any:
        """A fresh in-memory DuckDB holding this fixture."""
        try:
            import duckdb  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ConfigurationError("fixtures need the duckdb package: pip install sqlgami[duckdb]") from exc
        conn = duckdb.connect()
        # Fixture time is UTC whatever the host machine's zone: a bare TIMESTAMP literal means
        # the same instant here as it does in BigQuery, and results do not depend on where
        # the bundle is loaded.
        conn.execute("SET TimeZone = 'UTC'")
        for statement in self.ddl.split(";\n"):
            if statement.strip():
                conn.execute(statement)
        import os
        import tempfile

        # Bulk-load through CSV: DuckDB's reader is many times faster than bound inserts.
        with tempfile.TemporaryDirectory(prefix="sqlgami-fixture-") as folder:
            for name, (columns, rows) in self.tables.items():
                if not rows:
                    continue
                path = os.path.join(folder, name.replace(".", "_") + ".csv")
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(self.text_of(name))
                conn.execute(
                    f"INSERT INTO {name} ({', '.join(columns)}) SELECT * FROM read_csv(?, header = true, nullstr = '', "
                    f"columns = {{{', '.join(f'{c!r}: {t!r}' for c, t in zip(columns, self._column_types(name)))}}})",
                    [path],
                )
        return conn

    def _column_types(self, table: str) -> list[str]:
        """Declared DuckDB types of a table's columns."""
        return self.types.get(table) or ["VARCHAR"] * len(self.tables[table][0])

    def write(self, folder: Any) -> None:
        """Write the fixture as a folder of CSV files plus its DDL and manifest."""
        from pathlib import Path

        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "schema.sql").write_text(self.ddl, encoding="utf-8")
        for name in self.tables:
            (folder / f"{name}.csv").write_text(self.text_of(name), encoding="utf-8")
        (folder / "fixture.json").write_text(
            json.dumps({"name": self.name, "profile": self.profile, "seed": self.seed, "rows": self.row_count(), "digest": self.digest(),
                        "views_digest": self.views_digest(), "tables": {n: len(r) for n, (_, r) in self.tables.items()}, "types": self.types}, indent=1),
            encoding="utf-8",
        )

    @classmethod
    def read(cls, folder: Any) -> "Fixture":
        from pathlib import Path

        folder = Path(folder)
        meta = json.loads((folder / "fixture.json").read_text(encoding="utf-8"))
        ddl = (folder / "schema.sql").read_text(encoding="utf-8")
        tables: dict[str, tuple[list[str], list[tuple]]] = {}
        texts: dict[str, str] = {}
        for name in meta["tables"]:
            text = (folder / f"{name}.csv").read_text(encoding="utf-8")
            texts[name] = text
            reader = csv.reader(io.StringIO(text))
            columns = next(reader)
            rows = [tuple(_from_csv(v) for v in row) for row in reader]
            tables[name] = (columns, rows)
        fixture = cls(meta["name"], meta["profile"], int(meta["seed"]), tables, ddl, {k: list(v) for k, v in (meta.get("types") or {}).items()}, texts)
        if fixture.digest() != meta.get("digest"):
            raise ConfigurationError(f"fixture {folder} does not match its recorded digest; it was edited or corrupted")
        return fixture


# ------------------------------------------------------------------ from a database (opt-in)


def from_connection(
    conn: Any,
    catalog: Catalog,
    name: str,
    *,
    where: dict[str, str] | None = None,
    limit: int | None = None,
    profile: str = "subsample",
    seed: int = 0,
) -> Fixture:
    """A fixture copied from a live DuckDB connection: every catalogue table, optionally cut
    down by a ``where`` predicate per table and a row ``limit``. An explicit choice: rows of
    real data end up in the bundle, so use it on data that may be stored there."""
    tables = _tables(catalog)
    order = _topological(tables)
    data: dict[str, tuple[list[str], list[tuple]]] = {}
    where = {k.lower(): v for k, v in (where or {}).items()}
    for qualified in order:
        table = tables[qualified]
        columns = [c.name.lower() for c in table.columns]  # fixtures are case-insensitive DuckDB: one spelling
        predicate = where.get(qualified) or where.get(table.name.lower())
        sql = f"SELECT {', '.join(columns)} FROM {qualified}"
        if predicate:
            sql += f" WHERE {predicate}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        try:
            rows = [tuple(r) for r in conn.execute(sql).fetchall()]
        except Exception as exc:
            raise ConfigurationError(f"could not read {qualified} for the fixture: {exc}") from exc
        data[qualified] = (columns, rows)
    ddl, types = _ddl(catalog, [tables[q] for q in order], [q for q in order])
    return Fixture(name, profile, seed, data, ddl, types)


# ------------------------------------------------------------------ building


def build(catalog: Catalog, rules: FixtureRules | dict[str, Any] | None = None, profiles: Sequence[str] | None = None) -> list[Fixture]:
    """One fixture per profile."""
    if not isinstance(rules, FixtureRules):
        rules = FixtureRules.from_dict(rules)
    return [build_one(catalog, rules, profile) for profile in (profiles or rules.profiles)]


def build_one(catalog: Catalog, rules: FixtureRules, profile: str) -> Fixture:
    if profile not in PROFILES:
        raise ConfigurationError(f"unknown fixture profile {profile!r}")
    from . import fixture_rules as R

    tables = _tables(catalog)
    grid_parents: dict[str, list[str]] = {}
    for rule in rules.column_rules:
        if rule.kind == "grid":
            sources = list(rule.spec["per"].values()) + list((rule.spec.get("copy") or {}).values()) + list((rule.spec.get("where") or {}).keys())
            grid_parents[rule.table] = sorted({str(v).rpartition(".")[0].lower() for v in sources})
    order = _topological(tables, grid_parents)
    problems = R.validate_rules(rules.column_rules, lambda t: [c.name for c in tables[t].columns] if t in tables else None)
    if problems:
        raise ConfigurationError("fixture rules: " + "; ".join(problems))
    rng = random.Random(f"sqlgami-fixture:{profile}:{rules.seed}")
    data: dict[str, tuple[list[str], list[tuple]]] = {}
    for qualified in order:
        table = tables[qualified]
        columns = [c.name.lower() for c in table.columns]  # fixtures are case-insensitive DuckDB: one spelling
        grid = next((r for r in rules.column_rules if r.kind == "grid" and r.table == qualified), None)
        rows = _rows_for(catalog, table, qualified, tables, data, rules, profile, rng, grid=grid)
        data[qualified] = (columns, rows)
    name = f"{profile}-s{rules.seed}"
    ddl, types = _ddl(catalog, [tables[q] for q in order], [q for q in order])
    return Fixture(name, profile, rules.seed, data, ddl, types)


def logical_name(qualified: str) -> str:
    """The fixture's name for a catalogue table: the logical ``schema.table``, lower-cased, with a
    BigQuery project stripped from a three-part name (```acme-prod.hr.snapshot``` is the same
    table as ``hr.snapshot`` in every other dialect file, and digests the same)."""
    name = qualified.strip("`").lower()
    parts = name.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 3 else name


def _tables(catalog: Catalog) -> dict[str, Table]:
    found: dict[str, Table] = {}
    for qualified in catalog.table_names():
        schema, _, name = qualified.rpartition(".")
        table = catalog.resolve(name, schema or None, quoted=True) or catalog.resolve(name, schema or None)
        if table is not None and not table.is_view and not table.is_table_function:
            found[logical_name(qualified)] = table
    return found


def _views(catalog: Catalog) -> dict[str, Table]:
    """Views and table functions: both are a body the fixture must carry (a view as CREATE VIEW,
    a table function as a DuckDB table macro)."""
    found: dict[str, Table] = {}
    for qualified in catalog.table_names():
        schema, _, name = qualified.rpartition(".")
        table = catalog.resolve(name, schema or None, quoted=True) or catalog.resolve(name, schema or None)
        if table is not None and (table.is_view or table.is_table_function) and table.definition:
            found[logical_name(qualified)] = table
    return found


UNTRANSLATABLE_VIEWS: dict[str, str] = {}  # logical view/function name -> the construct that kept it out of the last fixture build


def _view_ddl(catalog: Catalog) -> list[str]:
    """CREATE VIEW statements in an order DuckDB accepts (a view before the views that read it)."""
    from .catalog import view_query_from_sql

    views = _views(catalog)
    UNTRANSLATABLE_VIEWS.clear()
    pending: dict[str, str] = {}
    for qualified, table in views.items():
        parsed = view_query_from_sql(table.definition or "", catalog.dialect)
        if parsed is None:
            continue
        query, columns = parsed
        try:
            import sqlglot

            # Rendered through the parser for every dialect, so the same view reads the same;
            # a three-part name inside the view is the logical table.
            tree = sqlglot.parse_one(query, read=catalog.dialect)
            for node in tree.find_all(sqlglot.exp.Table):
                if node.catalog and node.db:
                    node.set("catalog", None)
                for part in (node.args.get("db"), node.args.get("this")):
                    if isinstance(part, sqlglot.exp.Identifier):
                        part.set("quoted", False)  # a backticked or double-quoted name is the same table
                        part.set("this", part.name.lower())
            if catalog.dialect != "duckdb":
                from .translate import Untranslatable, translate

                own = {q.strip("`").lower(): logical_name(q) for q in catalog.table_names() if q.count(".") >= 2}
                functions = {k: {"parameters": [list(p) for p in f.parameters], "body": f.body, "language": f.language} for k, f in getattr(catalog, "scalar_functions", {}).items()}
                try:
                    query = translate(tree, catalog.dialect, own or None, functions=functions or None).sql
                except Untranslatable as exc:
                    UNTRANSLATABLE_VIEWS[qualified] = exc.construct
                    continue
            else:
                query = tree.sql(dialect="duckdb")
        except Exception:
            continue
        if table.is_table_function:
            params = ", ".join(p for p, _ in table.parameters)
            pending[qualified] = f"CREATE MACRO {qualified}({params}) AS TABLE {query}"
            continue
        column_list = f" ({', '.join(columns)})" if columns else ""
        pending[qualified] = f"CREATE VIEW {qualified}{column_list} AS {query}"
    ordered: list[str] = []
    done: set[str] = set()
    for _ in range(len(pending) + 1):
        progressed = False
        for qualified, statement in pending.items():
            if qualified in done:
                continue
            text = statement.lower()
            needs = [other for other in pending if other != qualified and other not in done and (other in text or other.rpartition(".")[2] in text)]
            if not needs:
                ordered.append(statement)
                done.add(qualified)
                progressed = True
        if not progressed:
            break
    ordered.extend(statement for qualified, statement in pending.items() if qualified not in done)
    return ordered


def _parent_of(fk: Any, tables: dict[str, Table], qualified: str) -> str | None:
    schema = qualified.rpartition(".")[0]
    candidates = [f"{(fk.ref_schema or schema)}.{fk.ref_table}".lower(), fk.ref_table.lower()]
    for c in candidates:
        if c in tables:
            return c
    return None


def _topological(tables: dict[str, Table], extra_parents: dict[str, list[str]] | None = None) -> list[str]:
    """Parents before children: by foreign key, and by the parents a grid rule draws from."""
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str, stack: tuple[str, ...]) -> None:
        if name in seen or name in stack:
            return
        parents = [_parent_of(fk, tables, name) for fk in tables[name].foreign_keys] + list((extra_parents or {}).get(name, []))
        for parent in parents:
            if parent and parent != name and parent in tables:
                visit(parent, stack + (name,))
        seen.add(name)
        order.append(name)

    for name in sorted(tables):
        visit(name, ())
    return order


def _rows_for(
    catalog: Catalog,
    table: Table,
    qualified: str,
    tables: dict[str, Table],
    data: dict[str, tuple[list[str], list[tuple]]],
    rules: FixtureRules,
    profile: str,
    rng: random.Random,
    grid: Any = None,
) -> list[tuple]:
    from . import fixture_rules as R

    columns = list(table.columns)
    names = [c.name.lower() for c in columns]
    is_parent = any(_parent_of(fk, tables, q) == qualified for q, t in tables.items() for fk in t.foreign_keys)
    count = rules.rows.get(qualified, rules.rows.get(table.name.lower(), DEFAULT_PARENT_ROWS if is_parent else DEFAULT_CHILD_ROWS))
    fk_of: dict[str, tuple[str, str]] = {}  # column -> (parent table, parent column)
    for fk in table.foreign_keys:
        parent = _parent_of(fk, tables, qualified)
        if parent is None or parent not in data:
            continue
        for col, ref in zip(fk.columns, fk.ref_columns):
            fk_of[col.lower()] = (parent, ref.lower())
    key_columns = {c.lower() for c in table.primary_key}
    snapshot_column = catalog.snapshot_columns.get(qualified) or catalog.snapshot_columns.get(table.name.lower())
    rows: list[list[Any]] = []
    seen_keys: set[tuple] = set()
    parent_pool: dict[str, list[Any]] = {}
    for col, (parent, ref) in fk_of.items():
        pcols, prows = data[parent]
        if ref in [c.lower() for c in pcols]:
            index = [c.lower() for c in pcols].index(ref)
            pool = [r[index] for r in prows if r[index] is not None]
            if profile == "sparse" and len(pool) > 2:
                pool = pool[: max(1, (len(pool) * 2) // 3)]  # some parents get no children
            parent_pool[col] = pool
    attempts = 0
    base = 0  # generated rows, not counting duplicates and restatements
    if grid is not None:
        # The host declared the table's grain: one row per combination of parent values.
        by_name = {c.name.lower(): c for c in columns}

        def generate(name: str, index: int) -> Any:
            column = by_name[name]
            value = _value(column, name, f"{qualified}.{name}", index, rules, profile, rng, catalog, table)
            rate = rules.null_rate.get(f"{qualified}.{name}", rules.null_rate.get(f"{table.name.lower()}.{name}"))
            if rate is None:
                rate = 0.0 if column.nullable is False else (0.2 if profile == "nulls" else 0.0 if profile == "clean" else 0.05)
            return None if value is not None and rate and rng.random() < rate else value

        rows = R.grid_rows(grid, names, data, generate, rng)
        count = 0  # nothing else generated
    while base < count and attempts < count * 10:
        attempts += 1
        row: list[Any] = []
        for position, column in enumerate(columns):
            key = column.name.lower()  # one spelling whatever the dialect's case rules
            qualified_column = f"{qualified}.{key}"
            value: Any
            if key in fk_of and parent_pool.get(key):
                value = rng.choice(parent_pool[key])
            elif key in key_columns and len(key_columns) == 1 and _family(column, catalog) == T.NUMERIC:
                value = base + 1
            else:
                value = _value(column, key, qualified_column, base, rules, profile, rng, catalog, table)
            if value is not None and key not in key_columns and key not in fk_of:
                rate = rules.null_rate.get(qualified_column, rules.null_rate.get(f"{table.name.lower()}.{key}"))
                if rate is None:
                    rate = 0.0 if column.nullable is False else (0.2 if profile == "nulls" else 0.0 if profile == "clean" else 0.05)
                if rate and rng.random() < rate:
                    value = None
            row.append(value)
        if key_columns:
            key = tuple(row[names.index(c.name.lower())] for c in columns if c.name.lower() in key_columns)
            if key in seen_keys:
                continue
            seen_keys.add(key)
        rows.append(row)
        base += 1
        if profile == "duplicates" and not key_columns and rng.random() < 0.15:
            rows.append(list(row))  # an exact duplicate where no key forbids it
        if profile == "duplicates" and snapshot_column and snapshot_column.lower() in [n.lower() for n in names] and rng.random() < 0.3:
            # a restatement: the same row taken again on a later snapshot date
            restated = list(row)
            index = [n.lower() for n in names].index(snapshot_column.lower())
            if isinstance(restated[index], dt.date):
                restated[index] = restated[index] + dt.timedelta(days=14)
                restated_key = tuple(restated[names.index(c.name.lower())] for c in columns if c.name.lower() in key_columns)
                if not key_columns or restated_key not in seen_keys:
                    seen_keys.add(restated_key)
                    rows.append(restated)
    R.apply_column_rules(rules.column_rules, qualified, names, rows)
    R.apply_derived(rules.column_rules, qualified, names, [_fixture_type(c.type, catalog.dialect) for c in columns], rows)
    if key_columns and any(r.kind != "grid" and r.table == qualified for r in rules.column_rules):
        keys = [tuple(r[names.index(c.name.lower())] for c in columns if c.name.lower() in key_columns) for r in rows]
        if len(set(keys)) != len(keys):
            raise ConfigurationError(f"fixture rules on {qualified} produce duplicate keys {sorted(key_columns)}: add the grouping column to the key or to per:")
    # the parameter values questions are asked with must occur in the columns they select on
    for param, values in rules.params.items():
        for qualified_column in rules.param_columns.get(param, []):
            tname, _, cname = qualified_column.rpartition(".")
            if tname not in (qualified, table.name.lower()) or cname not in [n.lower() for n in names]:
                continue
            index = [n.lower() for n in names].index(cname)
            for k, value in enumerate(values):
                for j in range(k, len(rows), max(1, len(values) * 3)):
                    rows[j][index] = value
    # critical rows, verbatim, replacing a generated row with the same key when there is one
    for critical in rules.critical_rows.get(qualified, []) + rules.critical_rows.get(table.name.lower(), []):
        row = [critical.get(n, critical.get(n.lower())) for n in names]
        row = [_coerce(v, c, catalog) for v, c in zip(row, columns)]
        if key_columns:
            key = tuple(row[names.index(c.name.lower())] for c in columns if c.name.lower() in key_columns)
            rows = [r for r in rows if tuple(r[names.index(c.name.lower())] for c in columns if c.name.lower() in key_columns) != key]
        rows.append(row)
    return [tuple(r) for r in rows]


def _family(column: Any, catalog: Catalog) -> str:
    return T.family_of_declared(column.type, catalog.dialect)


def _value(column: Any, key: str, qualified_column: str, index: int, rules: FixtureRules, profile: str, rng: random.Random, catalog: Catalog, table: Table) -> Any:
    domain = rules.values.get(qualified_column) or rules.values.get(f"{table.name.lower()}.{key}") or rules.values.get(key)
    if domain:
        return rng.choice(domain)
    family = _family(column, catalog)
    lowered = key.lower()
    if family == T.BOOLEAN:
        return rng.random() < 0.7
    if family in (T.DATE, T.TIMESTAMP):
        start, end = rules.dates
        span = (end - start).days
        if profile == "edges" and rng.random() < 0.3:
            day = rng.choice([start, end, start + dt.timedelta(days=1), end - dt.timedelta(days=1)])
        else:
            day = start + dt.timedelta(days=rng.randrange(span + 1))
        if family == T.TIMESTAMP:
            return dt.datetime.combine(day, dt.time(rng.randrange(24), rng.randrange(60)))
        return day
    if family == T.NUMERIC:
        if lowered.endswith(("_id", "id")) or lowered.endswith(("_seq", "_cd", "_ind", "_flag")):
            if lowered.endswith(("_ind", "_flag")):
                return rng.choice([0, 1])
            return rng.randrange(1, 60)
        if profile == "ties":
            return rng.choice([10, 20, 20, 50, 50, 50, 100])
        if profile == "edges" and rng.random() < 0.2:
            return rng.choice([0, -1, 0])
        if "rate" in lowered or "pct" in lowered or "amount" in lowered or "total" in lowered or "price" in lowered:
            return round(rng.uniform(0, 1000), 2)
        return rng.randrange(0, 1000)
    # text and the rest
    if lowered.endswith("_cd") or lowered == "status" or lowered.endswith("_type") or lowered.endswith("type"):
        return rng.choice(["A", "B", "C"])
    if profile == "edges" and rng.random() < 0.1:
        return ""
    return f"{key}_{index + 1}"


def _coerce(value: Any, column: Any, catalog: Catalog) -> Any:
    if value is None or value == "":
        return None
    family = _family(column, catalog)
    if family == T.DATE and isinstance(value, str):
        return _date(value)
    if family == T.TIMESTAMP and isinstance(value, str):
        return dt.datetime.fromisoformat(value)
    if family == T.NUMERIC and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return float(value)
    if family == T.BOOLEAN and isinstance(value, str):
        return value.strip().lower() in ("1", "true", "t", "yes")
    return value


def _date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


_CANONICAL_TYPES = {
    # one DDL spelling per family, so the same logical schema digests the same from every dialect file
    "BIGINT": "INT", "INT": "INT", "INTEGER": "INT", "SMALLINT": "INT", "TINYINT": "INT", "MEDIUMINT": "INT", "INT128": "INT", "UBIGINT": "INT", "UINT": "INT",
    "VARCHAR": "VARCHAR", "TEXT": "VARCHAR", "CHAR": "VARCHAR", "NCHAR": "VARCHAR", "NVARCHAR": "VARCHAR",
    "DOUBLE": "DOUBLE", "FLOAT": "DOUBLE", "REAL": "DOUBLE",
    "BOOLEAN": "BOOLEAN",
}


def _fixture_type(declared: str, dialect: str) -> str:
    """The DuckDB type the fixture declares for a column: sqlglot's reading of the declared
    type in the catalogue's own dialect (BigQuery's TIMESTAMP is an instant, TIMESTAMPTZ), with
    integer, text, floating and boolean families each spelled one way."""
    from .types import parse_type, sqlglot_type_name

    data_type = parse_type(declared, dialect) or parse_type(declared, "duckdb")
    if data_type is not None and isinstance(data_type.this, exp.DataType.Type):
        canonical = _CANONICAL_TYPES.get(data_type.this.value)
        if canonical:
            return canonical
    if data_type is not None:
        return data_type.sql(dialect="duckdb")
    name = sqlglot_type_name(declared, "duckdb")
    return "VARCHAR" if name == "UNKNOWN" else name


def _ddl(catalog: Catalog, tables: Sequence[Table], qualified: Sequence[str]) -> tuple[str, dict[str, list[str]]]:

    statements: list[str] = []
    types: dict[str, list[str]] = {}
    schemas = sorted({q.rpartition(".")[0] for q in qualified if "." in q} | {v.rpartition(".")[0] for v in _views(catalog) if "." in v})
    for schema in schemas:
        statements.append(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    for table, name in zip(tables, qualified):
        parts = []
        declared: list[str] = []
        for column in table.columns:
            type_name = _fixture_type(column.type, catalog.dialect)
            parts.append(f"{column.name.lower()} {type_name}")
            declared.append(type_name)
        statements.append(f"CREATE TABLE {name} ({', '.join(parts)})")
        types[name] = declared
    statements.extend(_view_ddl(catalog))  # the schema's views, so a query that reads them runs too
    return ";\n".join(statements) + ";\n", types


# ------------------------------------------------------------------ CSV


def _to_csv(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def _from_csv(text: str) -> Any:
    if text == "":
        return None
    if text in ("true", "false"):
        return text == "true"
    try:
        if "." in text or "e" in text.lower():
            return float(text)
        return int(text)
    except ValueError:
        pass
    try:
        if len(text) == 10:
            return dt.date.fromisoformat(text)
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return text


def _csv_text(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_to_csv(v) for v in row])
    return out.getvalue()
