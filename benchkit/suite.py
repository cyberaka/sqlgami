"""Read a bench suite from disk: suite.yaml, schemas, questions, bugs, equivalents."""

from __future__ import annotations

import json
import os
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

    def terms_source(self, engine: str | None = None) -> Path | None:
        """The suite's term catalogue as a host would supply it: a ``terms/`` folder of CSV sheets
        (``terms-<engine>/`` when an engine needs different table names), or ``terms.xlsx``."""
        for name in ((f"terms-{engine}", "terms") if engine else ("terms",)):
            folder = self.path / name
            if folder.is_dir() and any(folder.glob("*.csv")):
                return folder
        book = self.path / "terms.xlsx"
        return book if book.exists() else None

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


SYNC_FILE = ".source.json"
SYNCED_FILES = ("questions.jsonl", "bugs.jsonl", "equivalents.jsonl", "suite.yaml", "glossary.yaml", "generate.py")


def _md5(path: Path) -> str:
    import hashlib

    return hashlib.md5(path.read_bytes()).hexdigest()


def sync_suite(source: str | Path, dest: str | Path, *, keep_sqlgami_block: bool = True) -> dict[str, str]:
    """Copy a suite's files from ``source`` (a shared or held-out folder) into ``dest`` and record
    where they came from with their checksums, so a stale copy is refused later.

    The ``sqlgami:`` block of the destination's suite.yaml is kept when the source has none (it is
    the scorer's configuration, which the suite's author may not carry)."""
    import shutil

    source, dest = Path(source).expanduser().resolve(), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    recorded: dict[str, str] = {}
    for name in SYNCED_FILES:
        src = source / name
        if not src.exists():
            continue
        if name == "suite.yaml" and keep_sqlgami_block and (dest / name).exists():
            text = src.read_text(encoding="utf-8")
            if "\nsqlgami:" not in text:
                old = (dest / name).read_text(encoding="utf-8")
                marker = old.find("\nsqlgami:")
                if marker >= 0:
                    text = text.rstrip("\n") + "\n" + old[marker:]
            (dest / name).write_text(text, encoding="utf-8")
        else:
            shutil.copy(src, dest / name)
        recorded[name] = _md5(src)
    if (source / "schema").is_dir():
        shutil.copytree(source / "schema", dest / "schema", dirs_exist_ok=True)
    if (source / "terms").is_dir():
        # The term catalogue is the bench author's too: copied whole and checksummed per sheet.
        shutil.copytree(source / "terms", dest / "terms", dirs_exist_ok=True)
        for sheet in sorted(p for p in (source / "terms").iterdir() if p.is_file()):
            recorded[f"terms/{sheet.name}"] = _md5(sheet)
    record_file = dest / SYNC_FILE
    sources: list[dict[str, Any]] = []
    if record_file.exists():
        try:
            previous = json.loads(record_file.read_text(encoding="utf-8"))
            sources = [r for r in previous.get("sources", []) if r.get("source") != str(source)]
        except ValueError:
            sources = []
    # A file now taken from this source is no longer checked against an earlier one.
    for record in sources:
        record["files"] = {n: d for n, d in record.get("files", {}).items() if n not in recorded}
    sources.append({"source": str(source), "files": recorded})
    record_file.write_text(json.dumps({"sources": sources}, indent=1), encoding="utf-8")
    return recorded


def check_sync(path: Path) -> None:
    """Refuse a suite copy whose source has moved on since it was synced."""
    record_file = path / SYNC_FILE
    if not record_file.exists() or os.environ.get("BENCHKIT_ALLOW_STALE"):
        return  # BENCHKIT_ALLOW_STALE=1: comparing two commits on one frozen copy, knowingly
    try:
        record = json.loads(record_file.read_text(encoding="utf-8"))
        sources = [(Path(r["source"]), dict(r["files"])) for r in record["sources"]]
    except (ValueError, KeyError, TypeError) as exc:
        raise SuiteError(f"{record_file} is unreadable: {exc}") from exc
    for source, files in sources:
        if not source.is_dir():
            continue  # the source is not reachable from here; nothing to compare with
        stale = [name for name, digest in files.items() if (source / name).exists() and _md5(source / name) != digest]
        if stale:
            raise SuiteError(
                f"{path} is a stale copy: {', '.join(stale)} changed in {source} since the sync. "
                f"Run: python -m benchkit sync {source} {path}"
            )


def load_suite(name_or_path: str | Path) -> Suite:
    path = Path(name_or_path)
    if not path.is_dir():
        path = BENCH_DIR / str(name_or_path)
    config_file = path / "suite.yaml"
    if not config_file.exists():
        raise SuiteError(f"no suite.yaml in {path}")
    check_sync(path)
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
                # A row carries its engine's SQL in "sql" and, optionally, the same variant for
                # other engines in sql_<engine> ("sql_bigquery"): one Variant per engine.
                versions = [(row["engine"], row["sql"])] + [
                    (key[4:], value) for key, value in row.items() if key.startswith("sql_") and isinstance(value, str) and value.strip() and key[4:] != row["engine"]
                ]
                for engine, sql in versions:
                    target.append(
                        Variant(
                            id=row["id"],
                            question_id=row["question_id"],
                            engine=engine,
                            sql=sql,
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
