"""Read a bench suite from disk: suite.yaml, schemas, questions, bugs, equivalents."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = REPO_ROOT / "bench"
DATA_DIR = BENCH_DIR / ".data"
RUNS_DIR = BENCH_DIR / ".runs"

ENGINES = ("sqlite", "duckdb", "postgres", "bigquery", "cosmos")


class SuiteError(Exception):
    """The suite's files are missing or malformed."""


@dataclass
class Seed:
    seed: int
    profile: str

    @property
    def label(self) -> str:
        return f"{self.profile}-{self.seed}"


@dataclass
class Question:
    id: str
    question: str
    gold_sql: dict[str, str | None]
    ordered: bool = False
    answerable: Any = True  # True | False | "ambiguous"
    tenant: Any = None
    tags: list[str] = field(default_factory=list)
    traps: list[str] = field(default_factory=list)
    difficulty: str = ""
    notes: str = ""
    expected_answer_shape: dict[str, Any] | None = None
    # A hand-verified anchor: {"seed": int, "fact": str, "probe_sql": {engine: sql}, "expected": [[...]]}
    check: dict[str, Any] | None = None
    # Values for templated gold: {name: {"type": ..., "value": ...}}
    params: dict[str, Any] | None = None
    # Other accepted answers, for questions that can fairly be read more than one way:
    # [{"note": str, "sql": {engine: sql}}]
    gold_alternatives: list[dict[str, Any]] = field(default_factory=list)

    def gold(self, engine: str) -> str | None:
        return (self.gold_sql or {}).get(engine)

    def accepted(self, engine: str) -> list[tuple[str, str]]:
        """Every accepted answer for an engine as ``(label, sql)``; the first is the gold itself."""
        found: list[tuple[str, str]] = []
        gold = self.gold(engine)
        if gold:
            found.append(("gold", gold))
        for index, alternative in enumerate(self.gold_alternatives or []):
            sql = (alternative.get("sql") or {}).get(engine)
            if sql:
                found.append((f"alternative {index + 1}: {alternative.get('note', '')}".strip(": "), sql))
        return found

    def param_values(self) -> dict[str, Any]:
        """Template values in the plain ``{name: value}`` form sqlgami accepts."""
        values = {}
        for name, spec in (self.params or {}).items():
            values[name] = spec.get("value") if isinstance(spec, dict) else spec
        return values

    def render(self, sql: str) -> str:
        """Fill a templated statement the way the host application would, with ``str.format`` rules."""
        values = self.param_values()
        if not values:
            return sql
        from sqlgami.template import prepare

        prepared = prepare(sql, "duckdb", values)
        return prepared.executable_sql or sql


@dataclass
class Variant:
    """A planted bug or an equivalent rewrite of a question's gold SQL."""

    id: str
    question_id: str
    engine: str
    sql: str
    kind: str  # bug | equivalent
    description: str = ""
    bug_class: str | None = None
    expected_outcome: str | None = None  # error | wrong_on_all_seeds | wrong_on_nasty_only
    mutator: str | None = None


@dataclass
class Suite:
    name: str
    path: Path
    config: dict[str, Any]
    engines: list[str]
    seeds: list[Seed]
    questions: list[Question] = field(default_factory=list)
    bugs: list[Variant] = field(default_factory=list)
    equivalents: list[Variant] = field(default_factory=list)

    @property
    def tenant_column(self) -> str | None:
        return self.config.get("tenant_column")

    @property
    def soft_delete_column(self) -> str | None:
        return self.config.get("soft_delete_column")

    @property
    def soft_delete_policy(self) -> str:
        """``required`` when the suite declares that deleted rows are excluded by default."""
        declared = (self.config.get("policy") or {}).get("soft_delete")
        if declared in ("required", "advisory"):
            return declared
        glossary = self.path / "glossary.yaml"
        if glossary.exists() and "soft_delete_default" in glossary.read_text(encoding="utf-8"):
            return "required"
        return "advisory"

    def scorer_config(self, engine: str | None = None) -> dict[str, Any]:
        """Keyword arguments for ``Scorer`` that the suite declares under ``sqlgami:`` in suite.yaml.

        Table names are written as ``schema.table``; for an engine that flattens them (SQLite:
        ``schema_table``) they are translated.
        """
        block = self.config.get("sqlgami") or {}
        out: dict[str, Any] = {}

        def name(table: str) -> str:
            if engine != "sqlite":
                return table
            schema, _, bare = table.rpartition(".")
            return f"{schema}_{bare}" if schema else bare  # SQLite has one namespace: schema_table
        if self.tenant_column:
            out["tenant_column"] = self.tenant_column
        if self.soft_delete_column:
            out["soft_delete_column"] = self.soft_delete_column
            out["soft_delete_policy"] = self.soft_delete_policy
        if block.get("snapshot_columns"):
            out["snapshot_columns"] = {name(k): v for k, v in block["snapshot_columns"].items()}
        if block.get("required_predicates"):
            rules = []
            for rule in block["required_predicates"]:
                rule = dict(rule)
                if rule.get("tables"):
                    rule["tables"] = [name(t) for t in rule["tables"]]
                rules.append(rule)
            out["required_predicates"] = rules
        for key in ("default_project", "allowed_functions"):
            if block.get(key) is not None:
                out[key] = block[key]
        return out

    def schema_file(self, engine: str) -> Path:
        return self.path / "schema" / f"{engine}.sql"

    def question(self, question_id: str) -> Question | None:
        for question in self.questions:
            if question.id == question_id:
                return question
        return None

    def clean_seeds(self) -> list[Seed]:
        return [s for s in self.seeds if s.profile == "clean"]

    def nasty_seeds(self) -> list[Seed]:
        return [s for s in self.seeds if s.profile != "clean"]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SuiteError(f"{path.name} line {number}: {exc}") from exc
    return rows


def load_suite(name_or_path: str | Path) -> Suite:
    path = Path(name_or_path)
    if not path.is_dir():
        path = BENCH_DIR / str(name_or_path)
    config_file = path / "suite.yaml"
    if not config_file.exists():
        raise SuiteError(f"no suite.yaml in {path}")
    try:
        config = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SuiteError(f"{config_file} is not valid YAML: {exc}") from exc
    try:
        engines = list(config["engines"])
        seeds = [Seed(int(s["seed"]), str(s["profile"])) for s in config["seeds"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise SuiteError(f"suite.yaml is missing engines or seeds: {exc!r}") from exc
    unknown = [e for e in engines if e not in ENGINES]
    if unknown:
        raise SuiteError(f"suite.yaml names unknown engines: {unknown}")
    suite = Suite(name=config.get("name") or path.name, path=path, config=config, engines=engines, seeds=seeds)

    for row in _read_jsonl(path / "questions.jsonl"):
        try:
            suite.questions.append(
                Question(
                    id=row["id"],
                    question=row["question"],
                    gold_sql=row.get("gold_sql") or {},
                    ordered=bool(row.get("ordered", False)),
                    answerable=row.get("answerable", True),
                    tenant=row.get("tenant"),
                    tags=list(row.get("tags") or []),
                    traps=list(row.get("traps") or []),
                    difficulty=row.get("difficulty") or "",
                    notes=row.get("notes") or "",
                    expected_answer_shape=row.get("expected_answer_shape"),
                    check=row.get("check"),
                    params=row.get("params"),
                    gold_alternatives=list(row.get("gold_alternatives") or []),
                )
            )
        except KeyError as exc:
            raise SuiteError(f"questions.jsonl: a question is missing {exc}") from exc
    for filename, kind, target in (
        ("bugs.jsonl", "bug", suite.bugs),
        ("equivalents.jsonl", "equivalent", suite.equivalents),
    ):
        for row in _read_jsonl(path / filename):
            try:
                target.append(
                    Variant(
                        id=row["id"],
                        question_id=row["question_id"],
                        engine=row["engine"],
                        sql=row["sql"],
                        kind=kind,
                        description=row.get("description") or "",
                        bug_class=row.get("bug_class"),
                        expected_outcome=row.get("expected_outcome"),
                        mutator=row.get("mutator"),
                    )
                )
            except KeyError as exc:
                raise SuiteError(f"{filename}: an entry is missing {exc}") from exc
    return suite
