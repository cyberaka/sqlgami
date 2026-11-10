"""Native BigQuery ground truth (ADR 0003, B2/B3): load a suite's DuckDB seed databases into the
sandbox, run every BigQuery gold and planted bug natively, and compare each with (a) the DuckDB
gold on the same seed and (b) the BigQuery statement translated to DuckDB by the live path's
verified translation. Everything here is offline; nothing in ``score()`` calls it.

    python -m benchkit bq-native bench/.external/hr_snapshot_w4 --seeds 47 --out bench/.runs/bqnative/w4.json

Datasets are ``bench_<suite>_s<seed>_<schema>`` in the sandbox project, recorded in the ledger.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from . import build
from .compare import Outcome, run_sql, same_rows
from .suite import Question, Seed, Suite

_BQ_TYPES = {
    "BIGINT": "INT64", "INTEGER": "INT64", "INT": "INT64", "SMALLINT": "INT64", "TINYINT": "INT64", "HUGEINT": "NUMERIC",
    "VARCHAR": "STRING", "TEXT": "STRING", "DOUBLE": "FLOAT64", "FLOAT": "FLOAT64", "REAL": "FLOAT64",
    "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "TIMESTAMP WITH TIME ZONE": "TIMESTAMP", "BOOLEAN": "BOOL", "BLOB": "BYTES",
}


def _bq_type(duck_type: str) -> str:
    upper = duck_type.upper()
    if upper.startswith("DECIMAL") or upper.startswith("NUMERIC"):
        return "NUMERIC"
    return _BQ_TYPES.get(upper, "STRING")


@dataclass
class Comparison:
    id: str
    kind: str  # gold | gold_script | bug
    question_id: str
    seed: str
    origin: str = ""  # handwritten | transpiled | script (as the bench records it)
    native_ok: bool = False
    native_error: str | None = None
    native_rows: int | None = None
    vs_duckdb_gold: bool | None = None  # native BigQuery result == DuckDB gold on the same seed
    vs_duckdb_gold_why: str = ""
    vs_translation: bool | None = None  # native BigQuery result == the statement translated to DuckDB
    vs_translation_why: str = ""
    translation_error: str | None = None
    bytes_processed: int | None = None
    unstable: bool = False  # the statement ranks with ROW_NUMBER over ties: which rows win is not defined, so a disagreement is not a verdict


@dataclass
class Report:
    suite: str
    project: str
    seeds: list[str]
    datasets: list[str] = field(default_factory=list)
    comparisons: list[Comparison] = field(default_factory=list)

    def summary(self, seed: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for kind in ("gold", "gold_script", "bug"):
            rows = [c for c in self.comparisons if c.kind == kind and (seed is None or c.seed == seed)]
            if not rows:
                continue
            out[kind] = {
                "n": len(rows),
                "native_ok": sum(1 for c in rows if c.native_ok),
                "agree_with_duckdb_gold": sum(1 for c in rows if c.vs_duckdb_gold is True),
                "disagree_with_duckdb_gold": sum(1 for c in rows if c.vs_duckdb_gold is False),
                "agree_with_translation": sum(1 for c in rows if c.vs_translation is True),
                "disagree_with_translation": sum(1 for c in rows if c.vs_translation is False and not c.unstable),
                "unstable_tie_order": sum(1 for c in rows if c.unstable),
                "untranslatable": sum(1 for c in rows if c.translation_error and "untranslatable" in c.translation_error),
            }
            by_origin: dict[str, dict[str, int]] = {}
            for c in rows:
                entry = by_origin.setdefault(c.origin or "unknown", {"n": 0, "agree_with_duckdb_gold": 0})
                entry["n"] += 1
                entry["agree_with_duckdb_gold"] += int(c.vs_duckdb_gold is True)
            out[kind]["by_origin"] = by_origin
        return out


def _dataset(suite: Suite, seed: Seed, schema: str) -> str:
    return f"bench_{suite.name}_s{seed.seed}_{schema}"


def _rewrite(sql: str, project: str, suite: Suite, seed: Seed) -> str:
    """Point ```acme-people-prod.hr.table``` at the sandbox dataset for this seed. A statement
    sqlglot cannot parse (a planted syntax bug) is rewritten textually so the service judges it."""
    import re

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.ParseError:
        return re.sub(r"`([\w-]+)\.(\w+)\.(\w+)`", lambda m: f"`{project}.{_dataset(suite, seed, m.group(2).lower())}.{m.group(3)}`", sql)
    for table in tree.find_all(exp.Table):
        if table.db and table.catalog:
            table.set("catalog", exp.to_identifier(project, quoted=True))
            table.set("db", exp.to_identifier(_dataset(suite, seed, table.db.lower()), quoted=True))
    return tree.sql(dialect="bigquery")


def _rewrite_script(sql: str, project: str, suite: Suite, seed: Seed) -> str:
    """A DECLARE/SET script: rewrite every statement, keep the script shape. A script sqlglot
    cannot parse (a syntax error in a candidate) is rewritten textually so the service judges it."""
    import re

    parts = []
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except sqlglot.errors.ParseError:
        return re.sub(r"`([\w-]+)\.(\w+)\.(\w+)`", lambda m: f"`{project}.{_dataset(suite, seed, m.group(2).lower())}.{m.group(3)}`", sql)
    for statement in statements:
        if statement is None:
            continue
        for table in statement.find_all(exp.Table):
            if table.db and table.catalog:
                table.set("catalog", exp.to_identifier(project, quoted=True))
                table.set("db", exp.to_identifier(_dataset(suite, seed, table.db.lower()), quoted=True))
        parts.append(statement.sql(dialect="bigquery"))
    return ";\n".join(parts)


def load_seed(sandbox: Any, suite: Suite, seed: Seed, *, replace: bool = False) -> list[str]:
    """Create the seed's datasets and load its base tables and views from the DuckDB seed database."""
    conn = build.connect(suite, "duckdb", seed)
    datasets: list[str] = []
    tables = conn.execute(
        "SELECT table_schema, table_name, table_type FROM information_schema.tables "
        "WHERE table_schema NOT IN ('information_schema', 'pg_catalog', 'main') ORDER BY 1, 2"
    ).fetchall()
    for schema in sorted({t[0] for t in tables}):
        datasets.append(sandbox.ensure_dataset(_dataset(suite, seed, schema), note=f"{suite.name} seed {seed.seed} schema {schema}"))
    existing = {(d, t.table_id) for d in datasets for t in sandbox.client.list_tables(f"{sandbox.project}.{d}")}
    for schema, name, kind in tables:
        if kind != "BASE TABLE":
            continue
        dataset = _dataset(suite, seed, schema)
        if (dataset, name) in existing and not replace:
            continue
        columns = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [schema, name],
        ).fetchall()
        rows = conn.execute(f'SELECT * FROM "{schema}"."{name}"').fetchall()
        sandbox.load_table(dataset, name, [(c, _bq_type(t)) for c, t in columns], rows, replace=True)
    # Views from the suite's BigQuery DDL, re-pointed at the sandbox datasets (dependency order as written).
    ddl = suite.schema_file("bigquery").read_text(encoding="utf-8")
    for statement in build.split_statements(ddl):
        if statement.strip().upper().startswith("CREATE VIEW") or statement.strip().upper().startswith("CREATE OR REPLACE VIEW"):
            tree = sqlglot.parse_one(statement, read="bigquery")
            for table in tree.find_all(exp.Table):
                if table.db and table.catalog:
                    table.set("catalog", exp.to_identifier(sandbox.project, quoted=True))
                    table.set("db", exp.to_identifier(_dataset(suite, seed, table.db.lower()), quoted=True))
            tree.set("replace", True)
            result = sandbox.run(tree.sql(dialect="bigquery"))
            if not result.ok:
                raise RuntimeError(f"view failed: {result.error}")
    return datasets


def _store(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, dt.date):
        return {"$d": value.isoformat()}
    if isinstance(value, (bytes, bytearray)):
        return {"$b": bytes(value).hex()}
    if isinstance(value, (list, tuple)):  # an ARRAY, possibly of STRUCTs holding dates
        return [_store(v) for v in value]
    if isinstance(value, dict):  # a STRUCT
        return {"$s": {k: _store(v) for k, v in value.items()}}
    return value


def _revive(value: Any) -> Any:
    if isinstance(value, dict):
        if "$dt" in value:
            return dt.datetime.fromisoformat(value["$dt"])
        if "$d" in value:
            return dt.date.fromisoformat(value["$d"])
        if "$b" in value:
            return bytes.fromhex(value["$b"])
        if "$s" in value:
            return {k: _revive(v) for k, v in value["$s"].items()}
    if isinstance(value, list):
        return [_revive(v) for v in value]
    return value


def _native_outcome(result: Any) -> Outcome:
    if not result.ok:
        return Outcome(False, error=result.error)
    return Outcome(True, rows=[tuple(_plain(v) for v in row) for row in result.rows], columns=list(result.columns))


def _plain(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    if isinstance(value, (list, tuple)):
        return tuple(_plain(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _plain(v)) for k, v in value.items()))  # a STRUCT, comparable by its fields
    return value


def compare_suite(sandbox: Any, suite: Suite, seeds: list[Seed], *, include_bugs: bool = True, load: bool = True) -> Report:
    from sqlgami.bundle import schema_hash  # noqa: F401  (import check only: the live path's modules stay importable)
    from sqlgami.translate import Untranslatable, translate

    report = Report(suite=suite.name, project=sandbox.project, seeds=[s.label for s in seeds])
    origins = _origins(suite)
    for seed in seeds:
        if load:
            report.datasets.extend(load_seed(sandbox, suite, seed))
        duck = build.connect(suite, "duckdb", seed)
        duck.execute("SET TimeZone = 'UTC'")
        for question in suite.questions:
            gold_duck_sql = question.gold("duckdb")
            gold_duck = run_sql(duck, question.render(gold_duck_sql)) if gold_duck_sql else None
            statements: list[tuple[str, str, str]] = []
            if question.gold("bigquery"):
                statements.append((f"{question.id}:gold", "gold", question.gold("bigquery")))
            script = (question.gold_sql or {}).get("bigquery_script")
            if script:
                statements.append((f"{question.id}:gold_script", "gold_script", script))
            if include_bugs:
                for bug in suite.bugs:
                    if bug.engine == "bigquery" and bug.question_id == question.id:
                        statements.append((bug.id, "bug", bug.sql))
            for ident, kind, sql in statements:
                comp = Comparison(ident, kind, question.id, seed.label, origin=origins.get((ident.split(":")[0], kind), ""))
                rendered = question.render(sql)
                native_sql = _rewrite_script(rendered, sandbox.project, suite, seed) if kind == "gold_script" else _rewrite(rendered, sandbox.project, suite, seed)
                native = sandbox.run(native_sql)
                comp.native_ok, comp.native_error, comp.bytes_processed = native.ok, native.error, native.bytes_processed
                outcome = _native_outcome(native)
                comp.native_rows = len(outcome.rows) if outcome.ok else None
                if gold_duck is not None and outcome.ok and gold_duck.ok:
                    comp.vs_duckdb_gold, comp.vs_duckdb_gold_why = same_rows(gold_duck.rows, outcome.rows, question.ordered, gold_duck.columns, outcome.columns)
                elif gold_duck is not None and outcome.ok:
                    comp.vs_duckdb_gold, comp.vs_duckdb_gold_why = None, f"duckdb gold fails: {gold_duck.error}"
                if kind != "gold_script":
                    try:
                        translated = translate(rendered, "bigquery", tables=_table_map(suite)).sql
                        local = run_sql(duck, translated)
                        if outcome.ok and local.ok:
                            comp.vs_translation, comp.vs_translation_why = same_rows(outcome.rows, local.rows, question.ordered, outcome.columns, local.columns)
                            if comp.vs_translation is False and "ROW_NUMBER" in rendered.upper():
                                comp.unstable = True
                        elif not local.ok:
                            comp.translation_error = local.error
                    except Untranslatable as exc:
                        comp.translation_error = str(exc)
                    except sqlglot.errors.ParseError as exc:
                        comp.translation_error = f"does not parse: {str(exc)[:160]}"
                report.comparisons.append(comp)
    return report


def _table_map(suite: Suite) -> dict[str, str]:
    from sqlgami import Catalog
    from sqlgami.fixtures import logical_name

    catalog = Catalog.from_ddl(suite.schema_file("bigquery").read_text(encoding="utf-8"), "bigquery")
    return {q.strip("`").lower(): logical_name(q) for q in catalog.table_names() if q.count(".") >= 2}


def _origins(suite: Suite) -> dict[tuple[str, str], str]:
    """The bench's record of where each BigQuery statement came from (handwritten or transpiled)."""
    found: dict[tuple[str, str], str] = {}
    for filename, kind in (("questions.jsonl", "gold"), ("bugs.jsonl", "bug")):
        path = suite.path / filename
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            origin = row.get("bigquery_origin") or ""
            found[(row["id"], kind)] = origin
            if kind == "gold":
                found[(row["id"], "gold_script")] = "script"
    return found


def write_report(report: Report, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"summary": report.summary(), "per_seed": {s: report.summary(s) for s in report.seeds}, **asdict(report)}, indent=1, default=str),
        encoding="utf-8",
    )


class SeedConnection:
    """What ``build.connect(suite, "bigquery", seed)`` returns: the sandbox datasets holding one
    seed, with a ``native_run`` the bench runner uses in place of a cursor. Results are cached
    per statement, so labelling a candidate on a seed costs one query."""

    def __init__(self, sandbox: Any, suite: Suite, seed: Seed) -> None:
        self.sandbox, self.suite, self.seed = sandbox, suite, seed
        self._cache: dict[str, Outcome] = {}
        loaded = {t.table_id for t in sandbox.client.list_tables(f"{sandbox.project}.{_dataset(suite, seed, 'hr')}")} if _dataset(suite, seed, "hr") in sandbox.list_bench_datasets() else set()
        if not loaded:
            load_seed(sandbox, suite, seed)

    def native_run(self, sql: str) -> Outcome:
        if sql not in self._cache:
            self._cache[sql] = self._stored(sql)
        return self._cache[sql]

    def _stored(self, sql: str) -> Outcome:
        """Native results are kept on disk per seed and statement: a statement is run on the
        service once, whatever the number of bench runs that label it."""
        import hashlib

        folder = Path("bench/.runs/bigquery/cache") / f"{self.suite.name}_s{self.seed.seed}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (hashlib.sha256(sql.encode("utf-8")).hexdigest() + ".json")
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return Outcome(data["ok"], rows=[tuple(_revive(v) for v in r) for r in data.get("rows", [])], columns=data.get("columns", []), error=data.get("error"))
        is_script = sql.lstrip().upper().startswith("DECLARE")
        native_sql = _rewrite_script(sql, self.sandbox.project, self.suite, self.seed) if is_script else _rewrite(sql, self.sandbox.project, self.suite, self.seed)
        outcome = _native_outcome(self.sandbox.run(native_sql))
        path.write_text(json.dumps({"ok": outcome.ok, "columns": outcome.columns, "rows": [[_store(v) for v in r] for r in outcome.rows], "error": outcome.error}), encoding="utf-8")
        return outcome

    def close(self) -> None:
        pass
