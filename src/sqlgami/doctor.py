"""``sqlgami doctor``: an offline check of a pilot's setup — the workbook, the fixture rules, the
table map, and which constructs in a sample of the host's own queries the fixture translation
cannot yet handle. Nothing is executed and nothing leaves the machine."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot

from .catalog import Catalog
from .errors import ConfigurationError
from .terms import Catalogue


@dataclass
class Diagnosis:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unverified: dict[str, list[str]] = field(default_factory=dict)  # construct -> statements (by name) that use it
    statements: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def text(self) -> str:
        lines = []
        for problem in self.problems:
            lines.append(f"PROBLEM  {problem}")
        for note in self.notes:
            lines.append(f"ok       {note}")
        if self.statements:
            lines.append(f"queries  {self.statements} read")
            if self.unverified:
                lines.append("         constructs the fixture translation has not verified (a query using one gets no fixture verdict, never a wrong one):")
                for construct, names in sorted(self.unverified.items()):
                    lines.append(f"           {construct:28} in {len(names)} statement(s): {', '.join(names[:4])}{' …' if len(names) > 4 else ''}")
            else:
                lines.append("         every construct used is on the verified allow-list")
        lines.append("result   " + ("ready" if self.ok else f"{len(self.problems)} problem(s) to fix"))
        return "\n".join(lines)


def diagnose(config_path: str | Path, queries: str | Path | None = None) -> Diagnosis:
    out = Diagnosis()
    path = Path(config_path)
    try:
        import yaml
    except ImportError as exc:
        raise ConfigurationError("the doctor needs pyyaml: pip install sqlgami[bundle]") from exc
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        out.problems.append(f"{path}: not readable as YAML: {exc}")
        return out
    base = path.parent
    dialect = str(config.get("dialect") or "duckdb")
    out.notes.append(f"dialect {dialect}")

    # schema
    catalog: Catalog | None = None
    schema = config.get("schema")
    if not schema:
        out.problems.append("config: schema: is missing (a file of CREATE TABLE statements)")
    else:
        schema_path = Path(schema) if Path(schema).is_absolute() else base / schema
        if not schema_path.exists():
            out.problems.append(f"schema file {schema_path} does not exist")
        else:
            try:
                catalog = Catalog.from_ddl(schema_path.read_text(encoding="utf-8"), dialect, snapshot_columns=config.get("snapshot_columns") or None)
                names = catalog.table_names()
                out.notes.append(f"schema: {len(names)} tables/views read from {schema_path.name}")
                if dialect == "bigquery" and not any(n.count(".") >= 2 for n in names):
                    out.notes.append("schema names are two-part; queries written with `project.dataset.table` need a tables: map")
            except Exception as exc:
                out.problems.append(f"schema does not parse as {dialect} DDL: {exc}")

    # workbook
    catalogue: Catalogue | None = None
    terms = config.get("terms")
    if not terms:
        out.notes.append("no terms: workbook declared (term checks and pattern matching are off)")
    else:
        terms_path = Path(terms) if Path(terms).is_absolute() else base / terms
        if not terms_path.exists():
            out.problems.append(f"workbook {terms_path} does not exist")
        else:
            try:
                catalogue = Catalogue.load(terms_path)
                issues = catalogue.validate(dialect)
                for issue in issues:
                    out.problems.append(f"workbook: {issue}")
                if not issues:
                    out.notes.append(f"workbook: {len(catalogue.terms)} terms, {len(catalogue.kpis)} KPIs, {len(catalogue.examples)} examples, {len(catalogue.parameters)} parameters")
                if catalog is not None:
                    known = {n.lower() for n in catalog.table_names()} | {".".join(n.lower().split(".")[-2:]) for n in catalog.table_names()} | {n.lower().split(".")[-1] for n in catalog.table_names()}
                    for term in catalogue.terms:
                        for t in (x.strip() for x in term.table.split(";") if x.strip() and x.strip() != "*"):
                            if t.lower() not in known:
                                out.problems.append(f"workbook: term {term.name} is declared on {t}, which the schema does not have")
                    undefined = [t.name for t in catalogue.terms if not t.enforceable]
                    if undefined:
                        out.notes.append(f"workbook: {len(undefined)} documentation-only term(s) ({', '.join(undefined[:5])}): matched, not enforced, confidence capped at 0.75")
            except Exception as exc:
                out.problems.append(f"workbook: {exc}")

    # fixture rules
    fixtures = config.get("fixtures") or {}
    if catalog is not None:
        from . import fixture_rules as R
        from .fixtures import FixtureRules, _tables

        try:
            rules = FixtureRules.from_dict(fixtures)
            tables = _tables(catalog)
            problems = R.validate_rules(rules.column_rules, lambda t: [c.name for c in tables[t].columns] if t in tables else None)
            out.problems.extend(f"fixture rule: {p}" for p in problems)
            for name in list(rules.rows) + [c.rpartition(".")[0] for c in rules.values] + [c.rpartition(".")[0] for c in rules.null_rate]:
                if name not in tables and name not in {t.split(".")[-1] for t in tables}:
                    out.problems.append(f"fixtures: {name} is not a table of the schema")
            if not problems:
                out.notes.append(f"fixtures: synthetic, {len(rules.profiles)} profile(s), {len(rules.column_rules)} declared rule(s)" + (", critical rows for " + ", ".join(rules.critical_rows) if rules.critical_rows else ""))
            if fixtures.get("subsample"):
                out.notes.append("fixtures: a subsample: recipe is present — benchkit --from-seeds only; the default compile stays synthetic")
        except ConfigurationError as exc:
            out.problems.append(f"fixtures: {exc}")

    # table map
    if catalog is not None:
        from .fixtures import logical_name

        logical = {logical_name(n) for n in catalog.table_names()}
        table_map = {str(k).strip("`").lower(): str(v) for k, v in (config.get("tables") or {}).items()}
        for source, target in table_map.items():
            if target.lower() not in logical:
                out.problems.append(f"tables: {source} -> {target}, but the schema has no table {target}")
        auto = sum(1 for n in catalog.table_names() if n.count(".") >= 2)
        out.notes.append(f"table map: {auto} three-part name(s) mapped automatically, {len(table_map)} declared")

    # expansion
    expansion = config.get("expansion") or {}
    if expansion:
        model = str(expansion.get("model") or "")
        if model.startswith("hf:"):
            from .contrib.hf import check_model

            try:
                check_model(model[3:])
                out.notes.append(f"expansion: {model} (offline; HF_TOKEN read from the environment at compile time)")
            except ConfigurationError as exc:
                out.problems.append(f"expansion: {exc}")
        elif model.startswith("claude:"):
            out.notes.append(f"expansion: {model} (offline; needs the anthropic package and ANTHROPIC_API_KEY at compile time)")
        else:
            out.problems.append(f"expansion: model {model!r} must be hf:<name> or claude:<name>")

    # the host's queries: which constructs the fixture translation would refuse
    if queries is not None:
        from .translate import VERIFIED, constructs_of
        from . import analysis as A

        allowed = VERIFIED.get(dialect, frozenset())
        files = sorted(Path(queries).glob("*.sql")) if Path(queries).is_dir() else [Path(queries)]
        for file in files:
            text = file.read_text(encoding="utf-8")
            try:
                statements = [st for st in sqlglot.parse(text, read=dialect) if st is not None]
            except Exception as exc:
                out.problems.append(f"{file.name}: does not parse as {dialect}: {str(exc)[:120]}")
                continue
            if len(statements) > 1 and dialect in A._SCRIPT_DIALECTS and A._is_script(statements):
                statements = [A.bind_script(statements, dialect, A._statement_defined_names(statements[-1])).query]
            for index, statement in enumerate(statements):
                out.statements += 1
                label = file.name if len(statements) == 1 else f"{file.name}#{index + 1}"
                for construct in sorted(constructs_of(statement) - allowed):
                    out.unverified.setdefault(construct, []).append(label)
    return out


def init_pilot(target: str | Path, force: bool = False) -> list[Path]:
    """Copy the pilot template (a BigQuery snapshot-style schema, workbook and config) into ``target``."""
    import shutil

    source = Path(__file__).parent / "templates" / "pilot"
    target = Path(target)
    written: list[Path] = []
    for file in sorted(p for p in source.rglob("*") if p.is_file()):
        dest = target / file.relative_to(source)
        if dest.exists() and not force:
            raise ConfigurationError(f"{dest} exists; use --force to overwrite")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(file, dest)
        written.append(dest)
    return written


def _json(diagnosis: Diagnosis) -> str:
    return json.dumps({"ok": diagnosis.ok, "problems": diagnosis.problems, "notes": diagnosis.notes, "statements": diagnosis.statements, "unverified": diagnosis.unverified}, indent=1)
